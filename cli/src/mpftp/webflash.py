"""
Flashing a board from the browser interface (``python -m mpftp``).

The page's Firmware button gets an image one of two ways: it picks a file on
the computer and sends its bytes here in chunks, or it picks a board and
version from the official catalog (``mpftp.firmware_download``) and this
module downloads it. Either way the image is checked before the board is
touched, then flashed by the CLI's own engine (``python -m mpftp.firmware
flash``, the same one ``mpftp firmware flash`` and the VS Code extension use):

* an ESP ``.bin`` with esptool, after letting go of the board;
* a ``.uf2`` by copying it to the board's UF2 bootloader drive. When no drive
  is mounted and a board is connected, the board is asked to restart into its
  bootloader first (``machine.bootloader()``), which is how an rp2 or samd
  board running MicroPython gets there.

Output and a progress figure stream back to the tab that asked, and the reply
says whether the page should reconnect.

The methods, all answered by the server (never the sidecar):

``firmware_upload``
    ``{name, size, offset, data_b64}``: one chunk, in order. The first chunk
    (offset 0) starts a new upload for this tab, replacing any earlier one.
``firmware_catalog``
    ``{force?}``: the boards the catalog can flash, grouped by port.
``firmware_board``
    ``{board, force?}``: one board's flashable files, every variant.
``firmware_fetch``
    ``{board, url}``: download one of that board's files; it becomes this
    tab's image, as if it had been uploaded. Streams ``firmware_progress``.
``firmware_volumes``
    ``{}``: the UF2 bootloader drives mounted now.
``firmware_flash``
    ``{device?, erase?, volume?, port?}``: flash this tab's image, to
    ``device`` (the connected board's port by default) for a ``.bin`` or to
    ``volume`` for a ``.uf2``. Streams ``firmware_log`` and
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

from . import firmware, uf2

METHODS = frozenset(
    {
        "firmware_upload",
        "firmware_catalog",
        "firmware_board",
        "firmware_fetch",
        "firmware_volumes",
        "firmware_flash",
    }
)
#: Methods that touch the network, the disk or a board: answered on a thread.
_THREADED = frozenset(METHODS - {"firmware_upload"})

#: The largest image accepted. ESP flash tops out at 32 MB on the parts that
#: run MicroPython; anything bigger is not a firmware image.
MAX_IMAGE_BYTES = 32 * 1024 * 1024

ESPRESSIF_VID = 0x303A
#: The ROM's (and the app's) USB-Serial/JTAG peripheral. esptool resets it
#: into the download mode itself, so the port can be flashed as it is.
USB_SERIAL_JTAG_PID = 0x1001

UF2_MAGIC = b"UF2\n"

UF2_HINT = "For a UF2 board, choose its .uf2 instead."

NO_UF2_DRIVE = (
    "no UF2 drive found. Start the board's bootloader (hold BOOTSEL or BOOT while "
    "plugging it in, or double-tap reset) and Flash again."
)

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
    #: False for a download: the file is the firmware cache's, not ours to delete.
    owned: bool = True

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
        raise Refused(f"{name} is a UF2 file, not an ESP image for esptool.")
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
        handlers: dict[str, Callable[[], Any]] = {
            "firmware_upload": lambda: self.upload(ws, params),
            "firmware_catalog": lambda: self.catalog(params),
            "firmware_board": lambda: self.board(params),
            "firmware_fetch": lambda: self.fetch(ws, params),
            "firmware_volumes": lambda: self.volumes(),
            "firmware_flash": lambda: self.flash(ws, params),
        }
        fn = handlers.get(method)
        if fn is None:
            self._reply(ws, req_id, ("error", f"unknown method {method}"))
        elif method in _THREADED:
            threading.Thread(target=lambda: self._reply(ws, req_id, self._call(fn)), daemon=True).start()
        else:
            self._reply(ws, req_id, self._call(fn))

    def forget_tab(self, ws: Any) -> None:
        with self._lock:
            upload = self._uploads.pop(ws, None)
        if upload and upload.owned:
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
                if upload and upload.owned:
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

    # --- the official catalog ---------------------------------------------------

    #: The catalog module; tests put a fake in its place.
    catalog_module: Any = None

    def _catalog(self) -> Any:
        if self.catalog_module is None:
            from . import firmware_download

            self.catalog_module = firmware_download
        return self.catalog_module

    def catalog(self, params: dict[str, Any]) -> dict[str, Any]:
        return self._catalog().catalog_tree(force=bool(params.get("force")))

    def _board_entry(self, params: dict[str, Any]) -> dict[str, Any]:
        board = str(params.get("board") or "")
        entry = self._catalog().find_board(board, force=bool(params.get("force")))
        if not entry:
            raise Refused(f"{board or 'that board'} isn't in the firmware catalog")
        return entry

    def board(self, params: dict[str, Any]) -> dict[str, Any]:
        """One board's flashable files: every variant, every version."""
        fd = self._catalog()
        entry = self._board_entry(params)
        files = fd.flashable_files(entry, fd.board_files(entry, force=bool(params.get("force"))))
        return {
            "board": entry["board"],
            "title": entry["title"],
            "port": entry["port"],
            "family": entry["family"],
            "flasher": fd.FLASHERS.get(entry["port"], ""),
            "info_url": entry["info_url"],
            "source": entry["source"],
            "variantNotes": dict(entry.get("variants") or {}),
            "preferred": fd.preferred_artifact_kind(entry),
            "files": files,
        }

    def fetch(self, ws: Any, params: dict[str, Any]) -> dict[str, Any]:
        """Download one of a board's catalog files; it becomes the tab's image.

        Only a URL the board's catalog entry lists is fetched, so the page
        can't point this server at anything else.
        """
        fd = self._catalog()
        entry = self._board_entry(params)
        url = str(params.get("url") or "")
        chosen = next((f for f in fd.board_files(entry) if f["url"] == url), None)
        if chosen is None:
            raise Refused(f"{url or 'that file'} isn't one of {entry['board']}'s firmware files")

        def progress(done: int, total: int) -> None:
            self._notify(
                ws,
                "firmware_progress",
                {"text": f"Downloading {chosen['name']}…", "percent": 100.0 * done / total if total else None},
            )

        path = fd.download_file(url, progress=progress)
        size = path.stat().st_size
        with self._lock:
            old = self._uploads.get(ws)
            if old and old.owned:
                _unlink(old.path)
            self._uploads[ws] = Upload(name=chosen["name"], size=size, path=path, received=size, owned=False)
        return {"name": chosen["name"], "size": size, "kind": chosen["kind"], "path": str(path)}

    def find_volumes(self) -> list[dict[str, Any]]:
        return uf2.find_uf2_volumes(firmware.HOST)

    def volumes(self) -> list[dict[str, Any]]:
        return [{"path": v["path"], "board_id": v.get("board_id", "")} for v in self.find_volumes()]

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
        if _is_uf2(upload.path, upload.name):
            try:
                return self._flash_uf2(upload, params, log, status)
            finally:
                self.forget_tab(ws)
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

    def _flash_uf2(
        self, upload: Upload, params: dict[str, Any], log: Callable[[str], None], status: Callable[..., None]
    ) -> dict[str, Any]:
        """Copy a .uf2 to a bootloader drive, starting the bootloader if need be."""
        try:
            meta = uf2.parse_uf2(upload.path)
        except (uf2.Uf2Error, OSError) as e:
            raise Refused(f"{upload.name} isn't a usable UF2 file: {e}") from e
        families = "/".join(meta["family_names"]) or "no family"
        log(f"[mpftp] {upload.name}: UF2, {meta['blocks']} blocks, {families}")
        connected = self.connected()
        restarted = ""
        volume = str(params.get("volume") or "").strip()
        if not volume:
            found = self.find_volumes()
            if not found and connected and not is_network_device(connected):
                status("Restarting the board into its UF2 bootloader…")
                log(f"[mpftp] no UF2 drive mounted: asking the board on {connected} to start its bootloader")
                try:
                    self.request("bootloader", timeout=5)
                except Exception:
                    pass  # expected: the board drops off the bus as it resets
                self._disconnect(connected, log)
                restarted = connected
                found = self._wait_for_volumes()
            if not found:
                raise Refused(NO_UF2_DRIVE)
            if len(found) > 1:
                names = ", ".join(f"{v['path']} ({v.get('board_id') or 'unknown'})" for v in found)
                raise Refused(f"several UF2 drives are mounted ({names}): choose the one to flash")
            volume = str(found[0]["path"])
        status(f"Copying {upload.name} to {volume}…", 0.0)
        argv = [
            sys.executable, "-m", "mpftp.firmware", "flash",
            "--port", str(params.get("port") or "uf2"),
            "--artifact", str(upload.path),
            "--uf2", "--device", volume,
        ]
        result = run_engine(argv, lambda line: log(line) if line.strip() else None)
        out: dict[str, Any] = {
            "device": restarted or volume,
            "port": volume,
            "chip": families,
            "offset": "UF2",
            "reconnect": bool(restarted),
        }
        if result.get("ok"):
            status("Flashed. The board is restarting.", 100.0)
            return {"ok": True, **out}
        status("Flash failed", None)
        return {"ok": False, "error": str(result.get("error") or "the copy failed"), **out}

    def _wait_for_volumes(self) -> list[dict[str, Any]]:
        deadline = time.monotonic() + self.PORT_WAIT_S
        while time.monotonic() < deadline:
            self.sleep(0.7)
            found = self.find_volumes()
            if found:
                return found
        return []

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


def _is_uf2(path: Path, name: str) -> bool:
    if name.lower().endswith(".uf2"):
        return True
    try:
        with open(path, "rb") as f:
            return f.read(4) == UF2_MAGIC
    except OSError:
        return False


def _is_native_usb(info: Optional[dict[str, Any]]) -> bool:
    return bool(info) and info.get("vid") == ESPRESSIF_VID


def _unlink(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        pass
