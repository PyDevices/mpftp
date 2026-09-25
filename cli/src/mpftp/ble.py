"""Reach a board over Bluetooth Low Energy: ``-d ble://NAME``.

The board runs ``bledev.repl`` (pydevices' ``bledev`` package), which serves
the REPL over the Nordic UART service behind a password, the way WebREPL does
over Wi-Fi. :class:`BleSerial` dresses that link up as a pyserial port and
:func:`open_transport` hands it to mpremote's ``SerialTransport``, so exec,
raw REPL, interrupt and every ``fs_*`` operation work unchanged.

When the board also serves files (``bledev.filetransfer``, CircuitPython's BLE
file-transfer protocol), uploads and downloads go through that instead of the
REPL: it's 19 to 35 times faster (measured in docs/plans/ble.md). A board
without it still works, over the REPL alone.

bleak is the only dependency beyond the standard library, and only for BLE:
``python -m pip install bleak`` into the Python the sidecar runs on (on WSL,
the Windows one, because Windows owns the radio).
"""

from __future__ import annotations

import asyncio
import os
import struct
import threading
import time
from typing import Any, Optional

SCHEME = "ble://"

NUS_SERVICE = "6e400001-b5a3-f393-e0a9-e50e24dcca9e"
NUS_RX = "6e400002-b5a3-f393-e0a9-e50e24dcca9e"  # the central writes
NUS_TX = "6e400003-b5a3-f393-e0a9-e50e24dcca9e"  # the board notifies

FT_SERVICE = "0000febb-0000-1000-8000-00805f9b34fb"
FT_TRANSFER = "adaf0200-4669-6c65-5472-616e73666572"
FT_AUTH = "adaf0300-4669-6c65-5472-616e73666572"

#: ``bledev.repl`` refuses shorter passwords and buffers at most 64 bytes.
MIN_PASSWORD_LEN = 4
MAX_PASSWORD_LEN = 64

_PASSWORD_PROMPT = b"Password: "
_CONNECTED = b"bledev REPL connected"
_DENIED = b"Access denied"

#: As for WebREPL: a blocking read gives up after this long, because a link
#: that went quiet may never say so.
STALL_TIMEOUT = 20.0

#: How much of a file one READ asks for. The board streams it from flash, so
#: bigger only means fewer round trips.
READ_CHUNK = 64 * 1024

# The file-transfer protocol's commands and statuses (bledev.filetransfer).
_READ, _READ_DATA, _READ_PACING = 0x10, 0x11, 0x12
_WRITE, _WRITE_PACING, _WRITE_DATA = 0x20, 0x21, 0x22
_OK = 0x01

#: Test hook, to prove the SHA-256 check behind put/get can fail:
#: ``MPFTP_BLE_PLANT=put`` flips one bit in one uploaded chunk,
#: ``MPFTP_BLE_PLANT=get`` one bit in one downloaded chunk.
PLANT_ENV = "MPFTP_BLE_PLANT"

#: ``MPFTP_BLE_FILES=repl`` moves files over the raw REPL even when the board
#: serves file transfer (for comparing the two).
FILES_ENV = "MPFTP_BLE_FILES"


class BleError(OSError):
    """The BLE link failed or dropped. Says "port is closed" when the link is
    gone, so the sidecar treats it the way it treats a pulled USB cable."""


class BleAuthError(RuntimeError):
    """The board refused the password. Never retried."""


def is_ble_device(device: Optional[str]) -> bool:
    return bool(device) and device.strip().lower().startswith(SCHEME)


def parse_device(device: str) -> str:
    """``ble://NAME`` (the advertised name) or ``ble://AA:BB:CC:DD:EE:FF`` -> the part after ``ble://``."""
    target = device.strip()[len(SCHEME) :].strip("/")
    if not target:
        raise ValueError(f"{device}: expected ble://NAME, the name the board advertises")
    if "@" in target:
        raise ValueError(
            f"{device}: don't put the password in the address; set MPFTP_BLE_PASSWORD "
            "or blePassword in ~/.mpftp/config.json"
        )
    return target


_NO_PASSWORD = (
    "no BLE REPL password. Set MPFTP_BLE_PASSWORD, or blePassword in "
    "~/.mpftp/config.json, or save one with `mpftp wifi password ble://NAME`"
)

#: ATT errors that mean "pair first" (insufficient authentication, encryption).
_NEEDS_PAIRING = (0x05, 0x0F)


def check_password(password: Optional[str], device: str) -> Optional[str]:
    """The password, checked; ``None`` when there's none, which is fine for a
    board that pairing unlocks (``bledev.repl.start(pairing="passkey",
    password=False)``). One that asks for a password without one is refused
    at login."""
    if not password:
        return None
    if not MIN_PASSWORD_LEN <= len(password) <= MAX_PASSWORD_LEN or "\r" in password or "\n" in password:
        raise BleAuthError(
            f"{device}: a bledev.repl password is {MIN_PASSWORD_LEN} to {MAX_PASSWORD_LEN} "
            "characters with no line breaks"
        )
    return password


def _refused_for_pairing(e: BaseException) -> bool:
    """Whether a failed write was the board asking for pairing (ATT 0x05/0x0F)."""
    cause: Any = e
    while cause is not None:
        try:
            from bleak.exc import BleakGATTProtocolError

            if isinstance(cause, BleakGATTProtocolError) and int(cause.args[0]) in _NEEDS_PAIRING:
                return True
        except ImportError:
            pass
        text = str(cause).lower()
        if "insufficient authentication" in text or "insufficient encryption" in text:
            return True
        cause = cause.__cause__
    return False


def _need_bleak() -> Any:
    try:
        import bleak
    except ImportError as e:
        import sys

        raise BleError(
            "ble:// needs bleak in the Python the sidecar runs on: "
            f"{sys.executable} -m pip install --user bleak"
        ) from e
    return bleak


def scan(timeout: float = 5.0) -> list[dict[str, Any]]:
    """Boards advertising the REPL (the Nordic UART service), strongest first.

    Each row: ``name``, ``address``, ``rssi``, ``files`` (it advertises the
    file-transfer service too) and ``device``, the ``ble://`` address to
    connect with: the advertised name, else the Bluetooth address.
    """
    _need_bleak()
    from bleak import BleakScanner

    async def discover() -> dict[str, Any]:
        return await BleakScanner.discover(timeout=timeout, return_adv=True)

    found = asyncio.run(discover())
    rows: list[dict[str, Any]] = []
    for address, (device, adv) in found.items():
        uuids = {str(u).lower() for u in (getattr(adv, "service_uuids", None) or [])}
        if NUS_SERVICE not in uuids:
            continue
        name = getattr(adv, "local_name", None) or getattr(device, "name", None) or ""
        rows.append(
            {
                "name": name,
                "address": address,
                "rssi": getattr(adv, "rssi", None),
                "files": FT_SERVICE in uuids,
                "device": SCHEME + (name or address),
            }
        )
    rows.sort(key=lambda r: -(r["rssi"] if isinstance(r["rssi"], int) else -999))
    return rows


class BleSerial:
    """The pyserial surface mpremote's ``SerialTransport`` uses, over bledev.repl.

    ``open()`` finds the board by its advertised name, connects, and logs in,
    so a wrong password or a board that isn't advertising fails there, with a
    message. bleak runs on its own event loop in a background thread; the
    board's notifications land in a buffer that ``read`` takes from.
    """

    def __init__(
        self,
        device: str,
        password: Optional[str],
        *,
        timeout: Optional[float] = None,
        scan_timeout: float = 10.0,
        login_timeout: float = 8.0,
        throughput: bool = True,
    ) -> None:
        self.port = device
        self.target = parse_device(device)
        self._password = check_password(password, device)
        self.timeout = timeout
        self.write_timeout: Optional[float] = None
        self.scan_timeout = scan_timeout
        self.login_timeout = login_timeout
        self.throughput = throughput
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._client: Any = None
        self._cond = threading.Condition()
        self._rx = bytearray()  # the REPL stream
        self._frx = bytearray()  # file-transfer answers
        self._closed_reason: Optional[str] = None
        self._wlock = threading.Lock()
        #: REPL bytes received so far, as WebSocketSerial counts them, so the
        #: sidecar can tell whether the board answered an interrupt.
        self.rx_total = 0
        self.mtu = 23
        self.has_files = False
        self._plant = os.environ.get(PLANT_ENV, "").strip().lower()
        self._planted = False
        self.dtr = True
        self.rts = True

    # --- pyserial compatibility -------------------------------------------

    @property
    def is_open(self) -> bool:
        return self._client is not None and self._closed_reason is None

    def isOpen(self) -> bool:  # noqa: N802 (pyserial spelling)
        return self.is_open

    def setDTR(self, value: bool = True) -> None:  # noqa: N802
        self.dtr = value

    def setRTS(self, value: bool = True) -> None:  # noqa: N802
        self.rts = value

    def flush(self) -> None:
        pass

    def reset_input_buffer(self) -> None:
        with self._cond:
            self._rx.clear()

    flushInput = reset_input_buffer  # noqa: N815

    @property
    def in_waiting(self) -> int:
        return self.inWaiting()

    def inWaiting(self) -> int:  # noqa: N802
        with self._cond:
            if not self._rx and self._closed_reason:
                raise BleError(self._closed_message())
            return len(self._rx)

    def read(self, size: int = 1) -> bytes:
        limit = STALL_TIMEOUT if self.timeout is None else self.timeout
        deadline = time.monotonic() + limit
        with self._cond:
            while len(self._rx) < size:
                if self._closed_reason:
                    if self._rx:
                        break
                    raise BleError(self._closed_message())
                left = deadline - time.monotonic()
                if left <= 0:
                    if self.timeout is None:
                        raise BleError(
                            f"{self.port}: no data for {STALL_TIMEOUT:.0f} s; "
                            "port is closed (the BLE link stalled)"
                        )
                    break
                self._cond.wait(min(left, 0.5))
            out = bytes(self._rx[:size])
            del self._rx[:size]
            return out

    def write(self, data: bytes) -> int:
        data = bytes(data)
        if data:
            self._write_char(NUS_RX, data)
        return len(data)

    def close(self) -> None:
        loop, client = self._loop, self._client
        if loop is None:
            return
        self._closed_reason = self._closed_reason or "closed by mpftp"
        if client is not None:
            try:
                asyncio.run_coroutine_threadsafe(client.disconnect(), loop).result(5)
            except Exception:
                pass
        loop.call_soon_threadsafe(loop.stop)
        if self._thread is not None:
            self._thread.join(timeout=5)
        self._client = None
        self._loop = None

    # --- connection -------------------------------------------------------

    def open(self) -> None:
        _need_bleak()
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._loop.run_forever, name="mpftp-ble", daemon=True)
        self._thread.start()
        try:
            self._run(self._connect(), self.scan_timeout + 30)
            self._login()
        except BaseException:
            self._closed_reason = self._closed_reason or "open failed"
            self.close()
            raise

    def _run(self, coro: Any, timeout: float) -> Any:
        if self._loop is None:
            raise BleError(self._closed_message())
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        try:
            return future.result(timeout)
        except TimeoutError as e:
            future.cancel()
            raise BleError(f"{self.port}: the BLE operation timed out after {timeout:.0f} s; port is closed") from e

    async def _connect(self) -> None:
        from bleak import BleakClient, BleakScanner

        target = self.target
        wanted = target.upper()

        def match(device: Any, adv: Any) -> bool:
            return (adv.local_name or device.name or "") == target or (device.address or "").upper() == wanted

        device = await BleakScanner.find_device_by_filter(match, timeout=self.scan_timeout)
        if device is None:
            raise BleError(
                f"{self.port}: nothing is advertising as {target!r} within {self.scan_timeout:.0f} s. "
                "Is bledev.repl.start() running on the board (from main.py), and is nobody "
                "else connected to it? It takes one client at a time."
            )

        def dropped(_client: Any) -> None:
            with self._cond:
                self._closed_reason = self._closed_reason or "the board disconnected"
                self._cond.notify_all()

        client = BleakClient(device, disconnected_callback=dropped)
        try:
            await client.connect()
        except Exception as e:
            raise BleError(f"{self.port}: could not connect to {target!r}: {e}") from e
        self._client = client
        if self.throughput:
            self._request_throughput(client)
        # Windows exchanges the MTU itself; give it a moment to move off 23.
        for _ in range(50):
            if int(getattr(client, "mtu_size", 23) or 23) > 23:
                break
            await asyncio.sleep(0.02)
        self.mtu = int(getattr(client, "mtu_size", 23) or 23)
        services = client.services
        if services.get_characteristic(NUS_RX) is None or services.get_characteristic(NUS_TX) is None:
            await client.disconnect()
            raise BleError(f"{self.port}: {target!r} doesn't serve the REPL (no Nordic UART service)")
        await client.start_notify(NUS_TX, self._on_repl)
        if services.get_characteristic(FT_TRANSFER) is not None:
            await client.start_notify(FT_TRANSFER, self._on_files)
            self.has_files = True

    @staticmethod
    def _request_throughput(client: Any) -> bool:
        """Ask Windows for a short connection interval (WinRT only).

        It roughly doubles what the board can send us (measured in docs/plans/ble.md).
        bleak doesn't expose it, so this reaches into its WinRT backend.
        """
        requester = getattr(getattr(client, "_backend", None), "_requester", None)
        if requester is None:
            return False
        try:
            from winrt.windows.devices.bluetooth import BluetoothLEPreferredConnectionParameters

            requester.request_preferred_connection_parameters(
                BluetoothLEPreferredConnectionParameters.throughput_optimized
            )
            return True
        except Exception:
            return False

    def _on_repl(self, _sender: Any, data: bytearray) -> None:
        with self._cond:
            self._rx += data
            self.rx_total += len(data)
            self._cond.notify_all()

    def _on_files(self, _sender: Any, data: bytearray) -> None:
        with self._cond:
            self._frx += data
            self._cond.notify_all()

    def _write_char(self, uuid: str, data: bytes, response: bool = False) -> None:
        if self._closed_reason or self._client is None:
            raise BleError(self._closed_message())
        size = max(20, self.mtu - 3)
        limit = self.write_timeout or STALL_TIMEOUT

        async def send() -> None:
            for i in range(0, len(data), size):
                await self._client.write_gatt_char(uuid, data[i : i + size], response=response)

        with self._wlock:
            try:
                self._run(send(), limit + len(data) / 2000)
            except BleError:
                raise
            except Exception as e:
                if self._closed_reason:
                    raise BleError(self._closed_message()) from e
                raise BleError(f"{self.port}: BLE write failed: {e}") from e

    def _login(self) -> None:
        # Asks for the prompt again if we subscribed after it went out. A
        # board started with pairing refuses it until this host has paired.
        self._first_write()
        seen = self._read_until_any((_PASSWORD_PROMPT, _CONNECTED), self.login_timeout, "the password prompt")
        if _CONNECTED in seen:
            # Pairing was the lock: no password asked.
            self._read_until_any((b">>> ",), self.login_timeout, "the prompt after login")
            return
        if self._password is None:
            self._closed_reason = "no password"
            raise BleAuthError(f"{self.port}: the board asks for a password: {_NO_PASSWORD}")
        self.write(self._password.encode("utf-8") + b"\r")
        seen = self._read_until_any((_CONNECTED, _DENIED), self.login_timeout, "the login reply")
        if _DENIED in seen:
            self._closed_reason = "password rejected"
            raise BleAuthError(
                f"{self.port}: the board rejected the BLE REPL password "
                "(check MPFTP_BLE_PASSWORD / blePassword)"
            )
        # The REPL login unlocks files on the same connection too.
        self._read_until_any((b">>> ",), self.login_timeout, "the prompt after login")

    def _first_write(self) -> None:
        # With a response: a write without one to a characteristic the board
        # protects is dropped without a word, so an unpaired computer would
        # just wait for a prompt that never comes.
        try:
            self._write_char(NUS_RX, b"\r", response=True)
            return
        except BleError as e:
            if self._closed_reason or not _refused_for_pairing(e):
                raise self._explain_drop(e) from e
        # The board wants pairing. "Just works" needs nobody: a board without
        # a display. One that shows a passkey needs a person, once.
        try:
            self._pair()
        except Exception as e:
            raise BleAuthError(
                f"{self.port}: the board wants this computer paired and just works wasn't enough: "
                "it shows a passkey. Pair once with `python -m bledev.bleak pair NAME` (or Windows "
                f"Settings > Bluetooth > Add device), then try again. ({e})"
            ) from e
        try:
            self._write_char(NUS_RX, b"\r", response=True)
        except BleError as e:
            if _refused_for_pairing(e):
                # Just works was too weak for this board. Don't leave that
                # pairing behind: it would only get in the way of the real one.
                try:
                    self._unpair()
                except Exception:
                    pass
                raise BleAuthError(
                    f"{self.port}: the board wants a passkey pairing, which needs a person once: "
                    "pair with `python -m bledev.bleak pair NAME` (or Windows Settings > Bluetooth "
                    "> Add device), typing in the passkey the board shows, then try again."
                ) from e
            raise self._explain_drop(e) from e

    def _pair(self) -> None:
        """Pair through the OS: bleak's, which on Windows answers just works."""
        self._run(self._client.pair(), 40)

    def _unpair(self) -> None:
        self._run(self._client.unpair(), 20)

    def _explain_drop(self, e: BleError) -> BleError:
        """A link that falls at the first write, on a computer paired with the
        board, is the board refusing the computer's keys: it lost its bond."""
        if self._closed_reason and self._windows_paired():
            return BleError(
                f"{self.port}: the board hung up on this computer's stored keys; it has probably lost "
                "its bond (a chip erase). Unpair it (`python -m bledev.bleak unpair NAME`, or Windows "
                f"Settings > Bluetooth > Remove device) and pair again. Port is closed. ({e})"
            )
        return e

    def _windows_paired(self) -> bool:
        requester = getattr(getattr(self._client, "_backend", None), "_requester", None)
        try:
            return bool(requester.device_information.pairing.is_paired)
        except Exception:
            return False

    def _read_until_any(self, needles: tuple[bytes, ...], limit: float, what: str) -> bytes:
        deadline = time.monotonic() + limit
        with self._cond:
            while True:
                data = bytes(self._rx)
                for n in needles:
                    at = data.find(n)
                    if at >= 0:
                        del self._rx[: at + len(n)]
                        return data[: at + len(n)]
                if self._closed_reason:
                    raise BleError(f"{self.port}: the board closed the link before {what} (it said {data[-80:]!r})")
                left = deadline - time.monotonic()
                if left <= 0:
                    raise BleError(f"{self.port}: {what} didn't come within {limit:.0f} s (got {data[-80:]!r})")
                self._cond.wait(min(left, 0.5))

    def _closed_message(self) -> str:
        return f"{self.port}: port is closed ({self._closed_reason or 'not open'})"

    # --- file transfer ----------------------------------------------------

    def _frecv(self, n: int, timeout: float = STALL_TIMEOUT) -> bytes:
        deadline = time.monotonic() + timeout
        with self._cond:
            while len(self._frx) < n:
                if self._closed_reason:
                    raise BleError(self._closed_message())
                left = deadline - time.monotonic()
                if left <= 0:
                    raise BleError(f"{self.port}: file transfer stalled for {timeout:.0f} s; port is closed")
                self._cond.wait(min(left, 0.5))
            out = bytes(self._frx[:n])
            del self._frx[:n]
            return out

    def _fsend(self, data: bytes) -> None:
        self._write_char(FT_TRANSFER, data)

    def _flip(self, data: bytes, which: str) -> bytes:
        if self._plant == which and not self._planted and len(data) > 100:
            self._planted = True
            return data[:-1] + bytes((data[-1] ^ 0x10,))
        return data

    def files_read(self, path: str) -> bytes:
        """A whole file, over the file-transfer service. Raises OSError on a board error."""
        raw = path.encode("utf-8")
        with self._cond:
            self._frx.clear()
        self._fsend(struct.pack("<BBHII", _READ, 0, len(raw), 0, READ_CHUNK) + raw)
        out = bytearray()
        while True:
            cmd, status, _, at, total, size = struct.unpack("<BBHIII", self._frecv(16))
            if cmd != _READ_DATA:
                raise BleError(f"{self.port}: file transfer out of step (got 0x{cmd:02x}); port is closed")
            if status != _OK:
                raise OSError(2, f"{path}: the board can't read it (status 0x{status:02x})")
            out += self._flip(self._frecv(size), "get")
            at += size
            if at >= total or not size:
                return bytes(out)
            self._fsend(struct.pack("<BBHII", _READ_PACING, _OK, 0, at, min(READ_CHUNK, total - at)))

    def files_write(self, path: str, data: bytes) -> None:
        """Write a whole file over the file-transfer service. Raises OSError on a board error."""
        raw = path.encode("utf-8")
        total = len(data)
        with self._cond:
            self._frx.clear()
        self._fsend(struct.pack("<BBHIQI", _WRITE, 0, len(raw), 0, time.time_ns(), total) + raw)
        while True:
            cmd, status, _, at, _when, free = struct.unpack("<BBHIQI", self._frecv(20))
            if cmd != _WRITE_PACING:
                raise BleError(f"{self.port}: file transfer out of step (got 0x{cmd:02x}); port is closed")
            if status != _OK:
                raise OSError(5, f"{path}: the board refused the write (status 0x{status:02x})")
            if not free:
                if at != total:
                    raise OSError(5, f"{path}: the board stopped at {at} of {total} bytes")
                return
            chunk = self._flip(bytes(data[at : at + free]), "put")
            self._fsend(struct.pack("<BBHII", _WRITE_DATA, _OK, 0, at, len(chunk)) + chunk)


_TRANSPORT_CLASS: Any = None


def _transport_class() -> Any:
    """Build the ``SerialTransport`` subclass once mpremote is importable."""
    global _TRANSPORT_CLASS
    if _TRANSPORT_CLASS is not None:
        return _TRANSPORT_CLASS

    from mpremote.transport_serial import SerialTransport

    class BleTransport(SerialTransport):
        """mpremote's serial transport with a bledev.repl link as its port.

        Commands use raw-paste, whose flow control keeps the board's input
        buffer from overflowing. Files go over the file-transfer service when
        the board has one, and over the REPL otherwise.
        """

        def __init__(self, link: BleSerial) -> None:  # pyserial is not used
            self.in_raw_repl = False
            self.use_raw_paste = True
            self.device_name = link.port
            self.mounted = False
            self.serial = link

        def _files(self) -> bool:
            return self.serial.has_files and os.environ.get(FILES_ENV, "").lower() != "repl"

        def fs_writefile(self, dest, data, chunk_size=256, progress_callback=None):
            if not self._files():
                return super().fs_writefile(dest, data, chunk_size, progress_callback)
            try:
                self.serial.files_write(dest, bytes(data))
            except OSError as e:
                if isinstance(e, BleError):
                    raise
                # Let the REPL say what's wrong, in mpremote's own words.
                return super().fs_writefile(dest, data, chunk_size, progress_callback)
            if progress_callback:
                progress_callback(len(data), len(data))

        def fs_readfile(self, src, chunk_size=256, progress_callback=None):
            if not self._files():
                return super().fs_readfile(src, chunk_size, progress_callback)
            try:
                data = self.serial.files_read(src)
            except OSError as e:
                if isinstance(e, BleError):
                    raise
                return super().fs_readfile(src, chunk_size, progress_callback)
            if progress_callback:
                progress_callback(len(data), len(data))
            return bytearray(data)

    _TRANSPORT_CLASS = BleTransport
    return BleTransport


def open_transport(device: str, password: Optional[str], **kwargs: Any) -> Any:
    """An mpremote ``SerialTransport`` whose "serial port" is a BLE REPL."""
    cls = _transport_class()
    link = BleSerial(device, password, **kwargs)
    link.open()
    return cls(link)
