"""
Flashing an ESP board from the browser interface (``python -m mpftp``).

The page's Firmware button picks a ``.bin`` from the computer and sends its
bytes here in chunks; this module writes them to a temp file, checks the
image, lets go of the board, and runs the CLI's own esptool path
(``python -m mpftp.firmware flash``, the same engine ``mpftp firmware flash``
and the VS Code extension's Firmware panel use). esptool's output and a
progress figure stream back to the tab that asked, and the reply says whether
the page should reconnect.

Only esptool: a UF2 board flashes by copying the ``.uf2`` onto its drive,
which needs no help from us.

The methods, all answered by the server (never the sidecar):

``firmware_upload``
    ``{name, size, offset, data_b64}``: one chunk, in order. The first chunk
    (offset 0) starts a new upload for this tab, replacing any earlier one.
``firmware_flash``
    ``{device?, erase?}``: flash the finished upload to ``device`` (the
    connected board's port by default). Streams ``firmware_log`` and
    ``firmware_progress`` notifications to the asking tab; the result is
    ``{ok, error?, needErase?, reconnect?, device, chip, offset}``.
"""

from __future__ import annotations

import base64
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

from . import firmware

METHODS = frozenset({"firmware_upload", "firmware_flash"})

#: The largest image accepted. ESP flash tops out at 32 MB on the parts that
#: run MicroPython; anything bigger is not a firmware image.
MAX_IMAGE_BYTES = 32 * 1024 * 1024

ESPRESSIF_VID = 0x303A
#: The ROM's (and the app's) USB-Serial/JTAG peripheral. esptool resets it
#: into the download mode itself, so the port can be flashed as it is.
USB_SERIAL_JTAG_PID = 0x1001

UF2_MAGIC = b"UF2\n"

UF2_HINT = "For UF2 boards, drag the .uf2 onto the board's drive."

#: esptool v5 prints "Writing at 0x00010000 [=====>    ]  12.3% 1/2 bytes...";
#: v4 printed "Writing at 0x00010000... (12 %)".
_PROGRESS = re.compile(r"^(Writing|Erasing|Reading|Dumping) at (0x[0-9a-fA-F]+)\D.*?(\d+(?:\.\d+)?)\s*%")

#: Failures a slower baud rate won't fix.
_NOT_A_BAUD_PROBLEM = re.compile(
    r"Wrong --chip|This chip is|could not open port|PermissionError|"
    r"Access is denied|FileNotFoundError|No such file|Partition table on the device",
    re.IGNORECASE,
)


class Refused(Exception):
    """A flash that must not start; the message says why, for the dialog."""


@dataclass
class Upload:
    name: str
    size: int
    path: Path
    received: int = 0

    @property
    def complete(self) -> bool:
        return self.received == self.size


def check_image(path: Path, name: str) -> tuple[str, str]:
    """The chip a firmware file is for and the offset it goes to, or Refused.

    Runs before the board is touched, so a wrong file costs nothing.
    """
    try:
        with open(path, "rb") as f:
            head = f.read(64)
    except OSError as e:
        raise Refused(f"couldn't read {name}: {e}") from e
    if name.lower().endswith(".uf2") or head.startswith(UF2_MAGIC):
        raise Refused(f"{name} is a UF2 file. This flashes ESP boards with esptool. {UF2_HINT}")
    if not head or head[0] != 0xE9:
        raise Refused(
            f"{name} isn't an ESP firmware image. Choose the board's firmware .bin, "
            "the combined image that starts with the bootloader."
        )
    chip = firmware.esp32_image_family(path)
    if not chip:
        raise Refused(f"{name} doesn't say which ESP chip it is for, so it can't be flashed from here.")
    offset = firmware.esp32_flash_offset_for_family(chip)
    wrong = firmware.app_image_at_bootloader_error(path, offset)
    if wrong:
        raise Refused(wrong)
    return chip, offset


def is_network_device(device: str) -> bool:
    return bool(re.match(r"^(wss?|ble)://", device, re.IGNORECASE))


def engine_argv(artifact: Path, chip: str, device: str, erase: bool, baud: int) -> list[str]:
    """``python -m mpftp.firmware flash`` for one downloaded image."""
    argv = [
        sys.executable,
        "-m",
        "mpftp.firmware",
        "flash",
        "--port",
        "esp32",
        "--artifact",
        str(artifact),
        "--family",
        chip,
        "--device",
        device,
        "--baud",
        str(baud),
    ]
    if erase:
        argv.append("--erase")
    try:
        from . import config

        esptool = config.resolve("esptoolCommand")
    except Exception:
        esptool = ""
    if esptool:
        argv += ["--esptool", str(esptool)]
    return argv


def run_engine(argv: list[str], on_line: Callable[[str], None]) -> dict[str, Any]:
    """Run the firmware engine, passing each log line on; returns its result."""
    proc = subprocess.Popen(
        argv,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        text=True,
        bufsize=1,
        **firmware._no_window_kwargs(),
    )
    result: dict[str, Any] = {}
    assert proc.stdout
    for raw in proc.stdout:
        line = raw.rstrip("\n")
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            on_line(line)
            continue
        if not isinstance(msg, dict):
            on_line(line)
        elif msg.get("type") == "log":
            on_line(str(msg.get("line", "")))
        elif msg.get("type") == "result":
            result = msg
    proc.stdout.close()
    proc.wait()
    return result or {"ok": proc.returncode == 0, "error": f"the flash engine exited {proc.returncode}"}


class WebFlasher:
    """The browser's Firmware button, server side. One flash at a time.

    ``request`` is a sidecar call (``SidecarRelay.request``), ``send`` writes
    one message to one tab, and ``connected`` names the board the panel is
    connected to ("" when none).
    """

    METHODS = METHODS

    #: How long the board's port gets to come back after it is let go.
    PORT_WAIT_S = 15.0
    #: Thonny waits about 3 s after closing the REPL before esptool opens the
    #: port; on Windows a port reopened at once can still be busy.
    SETTLE_S = 1.0

    def __init__(
        self,
        request: Callable[..., Any],
        send: Callable[[Any, dict[str, Any]], bool],
        connected: Callable[[], str],
    ) -> None:
        self.request = request
        self.send = send
        self.connected = connected
        self._uploads: dict[Any, Upload] = {}
        self._lock = threading.Lock()
        self._busy = False
        self.sleep = time.sleep

    # --- the relay's entry point -------------------------------------------

    def handle(self, ws: Any, req_id: Any, method: str, params: dict[str, Any]) -> None:
        if method == "firmware_upload":
            self._reply(ws, req_id, self._call(lambda: self.upload(ws, params)))
        elif method == "firmware_flash":
            threading.Thread(
                target=lambda: self._reply(ws, req_id, self._call(lambda: self.flash(ws, params))),
                daemon=True,
            ).start()
        else:
            self._reply(ws, req_id, ("error", f"unknown method {method}"))

    def forget_tab(self, ws: Any) -> None:
        with self._lock:
            upload = self._uploads.pop(ws, None)
        if upload:
            _unlink(upload.path)

    @staticmethod
    def _call(fn: Callable[[], Any]) -> tuple[str, Any]:
        try:
            return "result", fn()
        except Exception as e:
            return "error", str(e)

    def _reply(self, ws: Any, req_id: Any, outcome: tuple[str, Any]) -> None:
        kind, value = outcome
        msg = {"type": kind, "id": req_id}
        msg["result" if kind == "result" else "error"] = value
        self.send(ws, msg)

    def _notify(self, ws: Any, method: str, params: dict[str, Any]) -> None:
        self.send(ws, {"type": "notify", "method": method, "params": params})

    # --- upload ----------------------------------------------------------------

    def upload(self, ws: Any, params: dict[str, Any]) -> dict[str, Any]:
        name = os.path.basename(str(params.get("name") or "firmware.bin"))
        size = int(params.get("size") or 0)
        offset = int(params.get("offset") or 0)
        data = base64.b64decode(str(params.get("data_b64") or ""))
        if size <= 0 or size > MAX_IMAGE_BYTES:
            raise ValueError(f"{name} is {size} bytes; a firmware image is 1 to {MAX_IMAGE_BYTES} bytes")
        with self._lock:
            upload = self._uploads.get(ws)
            if offset == 0:
                if upload:
                    _unlink(upload.path)
                fd, path = tempfile.mkstemp(prefix="mpftp-fw-", suffix=Path(name).suffix or ".bin")
                os.close(fd)
                upload = Upload(name=name, size=size, path=Path(path))
                self._uploads[ws] = upload
            if upload is None or upload.name != name or upload.size != size or offset != upload.received:
                raise ValueError("firmware upload out of order; choose the file again")
            if upload.received + len(data) > size:
                raise ValueError(f"{name} is bigger than the {size} bytes announced")
            with open(upload.path, "ab") as f:
                f.write(data)
            upload.received += len(data)
            return {"received": upload.received, "size": size}

    # --- flash -----------------------------------------------------------------

    def flash(self, ws: Any, params: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            if self._busy:
                raise Refused("a flash is already running")
            self._busy = True
            upload = self._uploads.get(ws)
        try:
            return self._flash(ws, upload, params)
        except Refused as e:
            return {"ok": False, "error": str(e)}
        finally:
            with self._lock:
                self._busy = False

    def _flash(self, ws: Any, upload: Optional[Upload], params: dict[str, Any]) -> dict[str, Any]:
        def log(line: str) -> None:
            self._notify(ws, "firmware_log", {"line": line})

        def status(text: str, percent: Optional[float] = None) -> None:
            self._notify(ws, "firmware_progress", {"text": text, "percent": percent})

        if upload is None or not upload.complete:
            raise Refused("choose a firmware file first")
        chip, offset = check_image(upload.path, upload.name)
        connected = self.connected()
        device = str(params.get("device") or connected or "").strip()
        if not device:
            raise Refused("no port: connect to the board, or type its serial port")
        if is_network_device(device):
            raise Refused(
                f"{device} is a network connection. Flashing needs the board's USB serial "
                "port: plug it in and choose that port."
            )
        erase = bool(params.get("erase"))
        log(f"[mpftp] {upload.name}: {chip} image, {upload.size} bytes, written at {offset}")

        was_connected = bool(connected) and connected == device
        flash_port = device
        if was_connected:
            self._check_platform(device)
            flash_port = self._release_board(device, log, status)

        status(f"Flashing {chip} on {flash_port}…", 0.0)
        native = _is_native_usb(self._port_info(flash_port))
        try:
            result = self._run(upload.path, chip, flash_port, erase, log, status, native)
        finally:
            self.forget_tab(ws)
        out: dict[str, Any] = {
            "device": device,
            "port": flash_port,
            "chip": chip,
            "offset": offset,
            "reconnect": was_connected,
        }
        if result.get("ok"):
            status("Flashed. The board is restarting.", 100.0)
            return {"ok": True, **out}
        error = str(result.get("error") or "the flash failed")
        status("Flash failed", None)
        return {"ok": False, "error": error, "needErase": result.get("needEraseConfirm") is not None, **out}

    def _check_platform(self, device: str) -> None:
        """Refuse a connected board that says it isn't an ESP chip."""
        try:
            reply = self.request("eval", {"expr": "__import__('sys').platform"}, timeout=10) or {}
            platform = str(reply.get("value") or "").strip("'\"")
        except Exception:
            return  # no answer is no evidence; esptool's --chip check still runs
        if platform and "esp" not in platform.lower():
            raise Refused(
                f"the board on {device} is {platform}, not an ESP chip. This flashes ESP "
                f"boards with esptool. {UF2_HINT}"
            )

    def _port_info(self, device: str) -> Optional[dict[str, Any]]:
        try:
            ports = self.request("list_ports", timeout=10) or []
        except Exception:
            return None
        for p in ports:
            if isinstance(p, dict) and p.get("device") == device:
                return p
        return None

    def _release_board(self, device: str, log: Callable[[str], None], status: Callable[..., None]) -> str:
        """Let go of the board and return the port esptool should open.

        A board on a USB-UART bridge, or on the chip's USB-Serial/JTAG, keeps
        its port: esptool's own reset puts it in download mode. A board whose
        REPL is MicroPython's TinyUSB CDC (an S2 or S3 on native USB) can't be
        reset from the host that way, so it is asked to enter its bootloader,
        and the ROM's download port, which enumerates as a new port, is
        flashed instead (as the extension's Firmware panel does).
        """
        try:
            ports = [p for p in self.request("list_ports", timeout=10) or [] if isinstance(p, dict)]
        except Exception:
            ports = []
        ports_before = {p.get("device") for p in ports}
        info = next((p for p in ports if p.get("device") == device), None)
        if info and _is_native_usb(info) and info.get("pid") != USB_SERIAL_JTAG_PID:
            status("Putting the board in download mode…")
            log(f"[mpftp] {device} is the board's own USB: asking it to enter the bootloader")
            try:
                self.request("bootloader", timeout=5)
            except Exception:
                pass  # expected: the board drops off the bus as it resets
            self._disconnect(device, log)
            rom = self._wait_for_download_port(ports_before, device)
            if not rom:
                raise Refused(
                    "no download port appeared. Hold BOOT and tap RESET on the board, "
                    "then choose its port and Flash again."
                )
            log(f"[mpftp] ROM download port: {rom}")
            return rom
        status(f"Releasing {device}…")
        self._disconnect(device, log)
        self._wait_for_port(device)
        return device

    def _disconnect(self, device: str, log: Callable[[str], None]) -> None:
        for method in ("repl_stop", "disconnect"):
            try:
                self.request(method, timeout=10)
            except Exception:
                pass
        log(f"[mpftp] disconnected {device} for flashing")

    def _wait_for_port(self, device: str) -> None:
        deadline = time.monotonic() + self.PORT_WAIT_S
        while time.monotonic() < deadline:
            if self._port_info(device) is not None:
                break
            self.sleep(0.5)
        self.sleep(self.SETTLE_S)

    def _wait_for_download_port(self, before: set, original: str) -> Optional[str]:
        deadline = time.monotonic() + self.PORT_WAIT_S
        while time.monotonic() < deadline:
            self.sleep(0.7)
            try:
                ports = self.request("list_ports", timeout=10) or []
            except Exception:
                continue
            for p in ports:
                if p.get("vid") == ESPRESSIF_VID and p.get("pid") == USB_SERIAL_JTAG_PID:
                    self.sleep(self.SETTLE_S)
                    return str(p["device"])
            for p in ports:
                if p.get("vid") == ESPRESSIF_VID and p.get("device") not in before and p.get("device") != original:
                    self.sleep(self.SETTLE_S)
                    return str(p["device"])
        return None

    def _run(
        self,
        artifact: Path,
        chip: str,
        port: str,
        erase: bool,
        log: Callable[[str], None],
        status: Callable[..., None],
        native: bool,
    ) -> dict[str, Any]:
        output: list[str] = []

        def on_line(line: str) -> None:
            output.append(line)
            m = _PROGRESS.match(line.strip())
            if m:
                verb = "Erasing" if m.group(1) == "Erasing" else "Writing"
                status(f"{verb} at {m.group(2)}", float(m.group(3)))
                return
            text = line.strip()
            if not text:
                return  # esptool pads its progress with blank lines
            log(line)
            if not text.startswith(("[mpftp]", "$")):
                status(text[:80])

        # Thonny starts at 115200 and leaves faster rates to the user; mpftp
        # starts at 460800, which most bridges manage, and falls back to
        # 115200 once when a UART link drops mid-write. Native USB has no
        # baud rate to blame.
        result = run_engine(engine_argv(artifact, chip, port, erase, 460800), on_line)
        if result.get("ok") or result.get("needEraseConfirm") is not None:
            return result
        said = "\n".join(output) + "\n" + str(result.get("error") or "")
        if not native and not _NOT_A_BAUD_PROBLEM.search(said):
            log("[mpftp] retrying at 115200 baud")
            output.clear()
            result = run_engine(engine_argv(artifact, chip, port, erase, 115200), on_line)
            if result.get("ok") or result.get("needEraseConfirm") is not None:
                return result
        return _explain(result, output, port)


def _explain(result: dict[str, Any], output: list[str], port: str) -> dict[str, Any]:
    """Put the reason esptool gave into the error, not just its exit code."""
    if result.get("ok"):
        return result
    text = "\n".join(output)
    if re.search(r"could not open port|PermissionError|Access is denied", text, re.IGNORECASE):
        result["error"] = (
            f"couldn't open {port}: another program has it (a serial monitor, Thonny, "
            "VS Code). Close it and Flash again."
        )
    else:
        m = re.search(r"(?:A fatal error occurred|A serial exception error occurred|Error):?\s*(.+)", text)
        if m and str(result.get("error", "")).startswith("esptool failed"):
            result["error"] = f"{result['error']}: {m.group(1).strip()}"
    return result


def _is_native_usb(info: Optional[dict[str, Any]]) -> bool:
    return bool(info) and info.get("vid") == ESPRESSIF_VID


def _unlink(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        pass
