"""The File Transfer panel's host for the browser interface (``python -m mpftp``).

The browser page runs the VS Code extension's own panel script
(``extension/media/ftp.js``). That script talks to its host with
``postMessage({type: ...})`` and redraws from the ``state`` and ``status``
messages it gets back. In VS Code the host is ``FtpViewProvider.ts``; here it
is :class:`PanelHost`, a port of that class's message handling, so the two
hosts behave the same way:

* board operations go to the shared sidecar (``SidecarRelay.request``);
* the local disk is this process's own file access, starting in the
  directory ``python -m mpftp`` was launched from;
* the dialogs VS Code would show (an input box, a "Delete?" confirmation, a
  notification) are asked of the browser tab that sent the message, which
  answers over the same WebSocket.

Connecting, disconnecting, the REPL and the ``mpftp.*`` commands in the
panel's menu belong to the page itself (``ui/src/host.ts``), the way they
belong to the extension in VS Code; the relay tells this class when a
connection comes or goes.

Messages to the page are ``{"type": "panel", "msg": ...}`` (handed to ftp.js
as a window "message" event) and ``{"type": "panel_host", ...}`` (handled by
the page: ask, info, error, open, openRepl, reply). Messages from the page are
``{"panel": ...}`` and ``{"panelAnswer": id, "value": ...}``.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import shutil
import threading
import time
from typing import Any, Callable, Optional


def should_skip_transfer_entry(name: str) -> bool:
    """Skip VCS/env/bytecode noise: .git, .venv, __pycache__, *.pyc, etc."""
    if not name or name in (".", ".."):
        return True
    if name.startswith("."):
        return True
    if name == "__pycache__":
        return True
    lower = name.lower()
    return lower.endswith(".pyc") or lower.endswith(".pyo")


def join_remote(base: str, name: str) -> str:
    if not base or base == "/":
        return "/" + name.lstrip("/")
    return base.rstrip("/") + "/" + name


def normalize_remote(path: str, base: str = "/") -> str:
    """An absolute board path: a relative one starts from ``base``; ``.``,
    ``..``, doubled and trailing slashes are folded away."""
    path = path.replace("\\", "/")
    if not path.startswith("/"):
        path = join_remote(base or "/", path)
    parts: list[str] = []
    for part in path.split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            if parts:
                parts.pop()
            continue
        parts.append(part)
    return "/" + "/".join(parts)


def short_device_name(device: str) -> str:
    """COM4, ttyACM0 (not the full /dev path)."""
    s = device.strip()
    if "/" in s or "\\" in s:
        if "://" in s:
            return s
        return os.path.basename(s.replace("\\", "/"))
    return s


def _plural_entries(n: int) -> str:
    return f"entr{'y' if n == 1 else 'ies'}"


class SidecarError(RuntimeError):
    """An error reply from the sidecar."""


class TransferMonitor:
    """Per-file transfer progress, reporting every ``tick`` seconds whether the
    work is still moving or looks hung (no file finished for ``stall``)."""

    def __init__(
        self,
        verb: str,
        total: int,
        on_update: Callable[[str, str], None],
        stall: float = 15.0,
        tick: float = 2.0,
    ) -> None:
        self.verb = verb
        self.total = total
        self.on_update = on_update
        self.stall = stall
        self.tick = tick
        self.done = 0
        self.current = ""
        self.started_at = time.monotonic()
        self.last_activity = time.monotonic()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        self.started_at = self.last_activity = time.monotonic()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        self.emit()

    def _loop(self) -> None:
        while not self._stop.wait(self.tick):
            self.emit()

    def begin_file(self, label: str) -> None:
        self.current = label
        self.last_activity = time.monotonic()
        self.emit()

    def finish_file(self) -> None:
        self.done += 1
        self.last_activity = time.monotonic()
        self.emit()

    def finish(self, summary: str) -> None:
        self.dispose()
        self.on_update(summary, "done")

    def dispose(self) -> None:
        self._stop.set()

    def emit(self) -> None:
        if self._stop.is_set():
            return
        now = time.monotonic()
        idle = now - self.last_activity
        elapsed = max(0, round(now - self.started_at))
        file_part = self.current or "…"
        counts = f"{self.done}/{self.total}" if self.total > 0 else f"{self.done}"
        if idle >= self.stall and self.current:
            self.on_update(
                f"{self.verb} {file_part} ({counts}) — no activity {round(idle)}s "
                f"(may be hung) · {elapsed}s total",
                "stalled",
            )
            return
        self.on_update(f"{self.verb} {file_part} ({counts}) — in progress · {elapsed}s", "active")


class PanelHost:
    """Answers the File Transfer panel's messages, as FtpViewProvider.ts does."""

    def __init__(
        self,
        request: Callable[..., Any],
        send: Callable[[Any, dict[str, Any]], bool],
        tabs: Callable[[], list[Any]],
        local_path: str,
    ) -> None:
        #: ``request(method, params=None)`` -> sidecar result, raises SidecarError.
        self.request = request
        #: ``send(tab, message)`` -> False when that tab is gone.
        self.send = send
        #: Every connected tab.
        self.tabs = tabs
        self.local_path = local_path
        self.remote_path = "/"
        self.connected_device = ""
        self.interpreter = ""
        self.device_info = ""
        self.transfer_busy = False
        self._settle: Optional[threading.Timer] = None
        self._status_lock = threading.Lock()
        self._asks: dict[int, tuple[Any, threading.Event, list[Any]]] = {}
        self._next_ask = 1
        self._asks_lock = threading.Lock()

    # --- settings --------------------------------------------------------

    @staticmethod
    def _setting(name: str, default: Any) -> Any:
        try:
            from . import config

            return config.resolve(name)
        except Exception:
            return default

    # --- connection, from the relay ------------------------------------------

    @property
    def connected(self) -> bool:
        return bool(self.connected_device)

    def set_connection(self, device: str, interpreter: str = "") -> None:
        """The relay saw a connect/resume succeed (device) or the session end ("")."""
        self.connected_device = device or ""
        self.interpreter = interpreter if device else ""
        threading.Thread(target=self._on_connection_change, daemon=True).start()

    def _on_connection_change(self) -> None:
        try:
            if self.connected:
                self.refresh_device_info()
            else:
                self.device_info = ""
            self.push_state()
            self.show_idle_status()
        except Exception as e:  # never let a background refresh kill the thread loudly
            self.status(str(e))

    def refresh_device_info(self) -> None:
        """Compact one-line chip/build/mem hint (MicroPython only)."""
        self.device_info = ""
        code = "\n".join(
            [
                "import json as _j",
                "_d = {}",
                "try:\n import sys\n _d['platform']=sys.platform\n _d['impl']=sys.implementation.name\n"
                " _d['ver']='.'.join(str(x) for x in sys.implementation.version[:3])\nexcept Exception: pass",
                "try:\n import os as _o\n _d['machine']=_o.uname().machine\nexcept Exception: pass",
                "try:\n import machine as _m\n _d['freq']=_m.freq()\nexcept Exception: pass",
                "try:\n import gc as _g\n _d['memfree']=_g.mem_free()\nexcept Exception: pass",
                "print('MPINFO:'+_j.dumps(_d))",
            ]
        )
        try:
            res = self.request("exec", {"code": code, "follow": True})
            out = str((res or {}).get("output", "")) if isinstance(res, dict) else ""
            line = next((ln for ln in out.splitlines() if ln.startswith("MPINFO:")), None)
            if not line:
                return
            d = json.loads(line[len("MPINFO:") :])
            parts: list[str] = []
            chip = str(d.get("machine") or d.get("platform") or "")
            if chip:
                parts.append(chip)
            impl = str(d.get("impl") or "")
            ver = str(d.get("ver") or "")
            if impl:
                parts.append(f"{impl} {ver}" if ver else impl)
            freq = d.get("freq")
            if isinstance(freq, (int, float)) and freq > 0:
                parts.append(f"{round(freq / 1e6)} MHz")
            elif isinstance(freq, list) and freq:
                parts.append(f"{round(float(freq[0]) / 1e6)} MHz")
            mem = float(d.get("memfree") or 0)
            if mem > 0:
                human = f"{round(mem / 1024)} KB" if mem < 1048576 else f"{mem / 1048576:.1f} MB"
                parts.append(f"{human} free")
            self.device_info = " · ".join(parts)
        except Exception:
            pass  # board busy / not MicroPython

    # --- talking to the page ----------------------------------------------

    def post_all(self, msg: dict[str, Any]) -> None:
        for tab in self.tabs():
            self.send(tab, {"type": "panel", "msg": msg})

    def host(self, tab: Any, action: str, **fields: Any) -> None:
        """A page-side action (a notification, opening the editor, ...)."""
        msg = {"type": "panel_host", "action": action, **fields}
        if tab is None:
            for t in self.tabs():
                self.send(t, msg)
        else:
            self.send(tab, msg)

    def info(self, tab: Any, text: str) -> None:
        self.host(tab, "info", text=text)

    def error(self, tab: Any, text: str) -> None:
        self.host(tab, "error", text=text)

    def ask(self, tab: Any, kind: str, **fields: Any) -> Any:
        """Ask the tab (input box or confirmation) and wait for its answer;
        None when the user cancels or the tab goes away."""
        event = threading.Event()
        box: list[Any] = [None]
        with self._asks_lock:
            ask_id = self._next_ask
            self._next_ask += 1
            self._asks[ask_id] = (tab, event, box)
        try:
            if not self.send(
                tab,
                {"type": "panel_host", "action": "ask", "askId": ask_id, "kind": kind, **fields},
            ):
                return None
            event.wait()
            return box[0]
        finally:
            with self._asks_lock:
                self._asks.pop(ask_id, None)

    def answer(self, ask_id: Any, value: Any) -> None:
        with self._asks_lock:
            entry = self._asks.get(ask_id)
        if entry:
            entry[2][0] = value
            entry[1].set()

    def forget_tab(self, tab: Any) -> None:
        """A tab closed: its open questions are answered with "cancel"."""
        with self._asks_lock:
            entries = [e for e in self._asks.values() if e[0] is tab]
        for _tab, event, box in entries:
            box[0] = None
            event.set()

    def ask_input(
        self, tab: Any, prompt: str, placeholder: str = "", value: str = "", selection=None
    ):
        fields: dict[str, Any] = {"prompt": prompt, "placeHolder": placeholder, "value": value}
        if selection is not None:
            fields["selection"] = list(selection)
        answer = self.ask(tab, "input", **fields)
        return answer if isinstance(answer, str) else None

    def confirm(self, tab: Any, text: str, ok_label: str) -> bool:
        return self.ask(tab, "confirm", prompt=text, okLabel=ok_label) is True

    def status(self, text: str, phase: str = "idle") -> None:
        with self._status_lock:
            if self._settle:
                self._settle.cancel()
                self._settle = None
            self.post_all({"type": "status", "text": text, "phase": phase})
            # Keep active/stalled visible; ephemeral notes settle back to connection idle.
            if phase in ("active", "stalled"):
                return
            delay = 2.5 if phase == "done" else 2.0
            self._settle = threading.Timer(delay, self.show_idle_status)
            self._settle.daemon = True
            self._settle.start()

    def show_idle_status(self) -> None:
        """Idle footer: connection state (not a generic "Ready")."""
        with self._status_lock:
            if self._settle:
                self._settle.cancel()
                self._settle = None
            if self.transfer_busy:
                return
            text = (
                f"Connected · {short_device_name(self.connected_device)}"
                if self.connected
                else "Disconnected"
            )
            self.post_all({"type": "status", "text": text, "phase": "idle"})

    # --- messages -----------------------------------------------------------

    def handle(self, tab: Any, msg: Any) -> None:
        """One panel message, on its own thread (a transfer can run for minutes
        while the user keeps clicking)."""
        if not isinstance(msg, dict):
            return
        try:
            self._handle(tab, msg)
        except Exception as e:
            self.status(str(e))

    def _handle(self, tab: Any, msg: dict[str, Any]) -> None:
        kind = msg.get("type")
        if kind == "ready":
            self.push_state()
            self.show_idle_status()
        elif kind in ("refreshLocal", "refreshRemote"):
            self.push_state()
        elif kind == "pickLocal":
            # VS Code shows a folder dialog; a page can't hand over a real path.
            chosen = self.ask_input(tab, "Local folder", value=self.local_path)
            if chosen:
                chosen = os.path.expanduser(chosen.strip())
                if os.path.isdir(chosen):
                    self.local_path = os.path.abspath(chosen)
                    self.push_state()
                else:
                    self.status(f"Not a directory: {chosen}")
        elif kind == "localCd":
            p = str(msg.get("path") or "")
            if p and os.path.isdir(os.path.expanduser(p)):
                self.local_path = os.path.abspath(os.path.expanduser(p))
                self.push_state()
            else:
                self.status(f"Not a directory: {p}")
        elif kind == "localUp":
            parent = os.path.dirname(self.local_path.rstrip("/\\") or self.local_path)
            if parent and parent != self.local_path:
                self.local_path = parent
                self.push_state()
        elif kind == "remoteCd":
            self.remote_path = str(msg.get("path") or "/")
            self.push_state()
        elif kind == "upload":
            self.upload_many(tab, list(msg.get("localPaths") or []))
            self.push_state()
        elif kind == "download":
            self.download_many(tab, list(msg.get("remotePaths") or []))
            self.push_state()
        elif kind == "openRemote":
            remote = str(msg.get("path") or "")
            if not remote:
                return
            self._require_connected()
            self.request("fs_touch", {"path": remote})
            res = self.request("edit_pull", {"path": remote})
            self.host(tab, "open", side="remote", path=remote, data_b64=res.get("data_b64", ""))
            self.status(f"Editing {remote}")
        elif kind == "runRemote":
            remote = str(msg.get("path") or "")
            if not remote:
                return
            if not remote.lower().endswith(".py"):
                self.status("Run is only for .py files")
                return
            self.run_remote_path(tab, remote)
        elif kind == "uploadAndRun":
            local = str(msg.get("localPath") or "")
            if not local:
                return
            if not local.lower().endswith(".py"):
                self.status("Upload & Run is only for .py files")
                return
            if not self.connected:
                self.status("Not connected", "stalled")
                return
            base = os.path.basename(local)
            remote = join_remote(self.remote_path, base)
            self.status(f"Uploading {base}…", "active")
            try:
                self.upload_many(tab, [local])
                self.push_state()
            except Exception as e:
                self.status(f"Upload failed: {e}", "stalled")
                self.error(tab, f"mpftp upload: {e}")
                return
            self.run_remote_path(tab, remote)
        elif kind == "hashRemote":
            remote = str(msg.get("path") or "")
            if not remote:
                return
            res = self.request("fs_hash", {"path": remote, "algo": "sha256"})
            self.status(f"{remote}: {res.get('algo')} {res.get('hash')}")
            self.info(tab, f"{remote}\n{res.get('hash')}")
        elif kind == "mkdir":
            name = self.ask_input(tab, "New remote directory name", placeholder="lib")
            if not name:
                return
            dest = join_remote(self.remote_path, name)
            self.request("fs_mkdir", {"path": dest})
            self.status(f"mkdir {dest}")
            self.push_state()
        elif kind == "localMkdir":
            name = self.ask_input(tab, "New local directory name", placeholder="lib")
            if not name:
                return
            if "/" in name or "\\" in name:
                self.status("Name cannot contain path separators")
                return
            dest = os.path.join(self.local_path, name)
            os.mkdir(dest)
            self.status(f"mkdir {dest}")
            self.push_state()
        elif kind == "newFile":
            name = self.ask_input(tab, "New remote file name", placeholder="main.py")
            if not name:
                return
            dest = join_remote(self.remote_path, name)
            self.request("fs_touch", {"path": dest})
            self.status(f"touch {dest}")
            self.push_state()
        elif kind == "localNewFile":
            name = self.ask_input(tab, "New local file name", placeholder="main.py")
            if not name:
                return
            if "/" in name or "\\" in name:
                self.status("Name cannot contain path separators")
                return
            dest = os.path.join(self.local_path, name)
            if os.path.exists(dest):
                self.status(f"Already exists: {name}")
                return
            with open(dest, "xb"):
                pass
            self.status(f"Created {dest}")
            self.push_state()
        elif kind == "openLocal":
            local = str(msg.get("path") or "")
            if not local:
                return
            if not os.path.isfile(local):
                self.status(f"Not a file: {local}")
                return
            with open(local, "rb") as f:
                data = f.read()
            self.host(
                tab,
                "open",
                side="local",
                path=local,
                data_b64=base64.b64encode(data).decode("ascii"),
            )
            self.status(f"Editing {local}")
        elif kind == "saveFile":
            self.save_file(tab, msg)
        elif kind == "saveFileAs":
            self.save_file_as(tab, msg)
        elif kind == "rm":
            self.rm_many(tab, list(msg.get("remotePaths") or []))
            self.push_state()
        elif kind == "localRm":
            self.local_rm_many(tab, list(msg.get("localPaths") or []))
            self.push_state()
        elif kind == "localRename":
            src = str(msg.get("path") or "")
            if not src or not os.path.exists(src):
                self.status(f"Not found: {src}")
                return
            old = os.path.basename(src)
            new = self.ask_input(
                tab, "Rename local item", value=old, selection=_stem_selection(old)
            )
            if not new or new == old:
                return
            if "/" in new or "\\" in new:
                self.status("Name cannot contain path separators")
                return
            os.rename(src, os.path.join(os.path.dirname(src), new))
            self.status(f"Renamed {old} → {new}")
            self.push_state()
        elif kind == "remoteRename":
            src = str(msg.get("path") or "")
            if not src:
                return
            old = ([p for p in src.split("/") if p] or [""])[-1]
            new = self.ask_input(
                tab, "Rename board item", value=old, selection=_stem_selection(old)
            )
            if not new or new == old:
                return
            if "/" in new:
                self.status("Name cannot contain '/'")
                return
            parent = (src[: src.rindex("/")] or "/") if "/" in src else "/"
            dest = join_remote(parent, new)
            self.request("fs_rename", {"src": src, "dest": dest})
            self.status(f"Renamed {old} → {new}")
            self.push_state()

    def _require_connected(self) -> None:
        if not self.connected:
            raise RuntimeError("not connected")

    # --- the editor's save (browser only) -------------------------------------

    def save_file(self, tab: Any, msg: dict[str, Any]) -> None:
        """Write an editor buffer back to where it came from."""
        side = msg.get("side")
        path = str(msg.get("path") or "")
        req_id = msg.get("reqId")
        try:
            data_b64 = str(msg.get("data_b64") or "")
            data = base64.b64decode(data_b64)
            if side == "local":
                with open(path, "wb") as f:
                    f.write(data)
            elif side == "remote":
                self._require_connected()
                self.request("edit_push", {"path": path, "data_b64": data_b64})
            else:
                raise ValueError(f"unknown side: {side}")
        except Exception as e:
            self.host(tab, "reply", reqId=req_id, ok=False, error=str(e))
            self.status(f"Save failed: {e}", "stalled")
            return
        self.host(tab, "reply", reqId=req_id, ok=True)
        self.status(f"Saved {path} ({len(data)} bytes)", "done")
        self.push_state()

    def save_as_target(self, side: str, path: str) -> str:
        """Where a Save As writes: a relative path is taken from that list's folder."""
        path = (path or "").strip()
        if side == "local":
            path = os.path.expanduser(path)
            if not os.path.isabs(path):
                path = os.path.join(self.local_path, path)
            return os.path.abspath(path)
        if side == "remote":
            return normalize_remote(path, self.remote_path)
        raise ValueError(f"unknown side: {side}")

    def _remote_stat(self, path: str) -> Optional[dict[str, Any]]:
        """fs_stat, or None when the board has nothing there."""
        try:
            return self.request("fs_stat", {"path": path})
        except Exception:
            return None

    def save_file_as(self, tab: Any, msg: dict[str, Any]) -> None:
        """Write an editor buffer to a new place, on either side.

        A missing folder is an error (the board's write doesn't create folders,
        and neither does this); an existing file is replaced only after the
        user says so. The reply carries where the file went, so the page can
        move its tab there."""
        side = str(msg.get("side") or "")
        req_id = msg.get("reqId")
        source = (str(msg.get("sourceSide") or ""), str(msg.get("sourcePath") or ""))

        def fail(text: str) -> None:
            self.host(tab, "reply", reqId=req_id, ok=False, error=text)
            self.status(f"Save As failed: {text}", "stalled")

        try:
            dest = self.save_as_target(side, str(msg.get("path") or ""))
            data_b64 = str(msg.get("data_b64") or "")
            data = base64.b64decode(data_b64)
            if side == "local":
                name = os.path.basename(dest)
                folder = os.path.dirname(dest)
                if not name or str(msg.get("path") or "").rstrip().endswith(("/", "\\")):
                    return fail("give the file a name, not just a folder")
                if not os.path.isdir(folder):
                    return fail(f"folder doesn't exist: {folder}")
                exists = os.path.exists(dest)
                if exists and os.path.isdir(dest):
                    return fail(f"{dest} is a folder")
            else:
                self._require_connected()
                if dest == "/" or str(msg.get("path") or "").rstrip().endswith("/"):
                    return fail("give the file a name, not just a folder")
                folder = dest.rsplit("/", 1)[0] or "/"
                st = self._remote_stat(folder)
                if not st or not st.get("isDir"):
                    return fail(f"folder doesn't exist on the board: {folder}")
                st = self._remote_stat(dest)
                exists = st is not None
                if st and st.get("isDir"):
                    return fail(f"{dest} is a folder on the board")
            if exists and (side, dest) != source:
                where = "on the board" if side == "remote" else "on this computer"
                if not self.confirm(tab, f"{dest} already exists {where}. Replace it?", "Replace"):
                    self.host(tab, "reply", reqId=req_id, ok=False, cancelled=True)
                    self.show_idle_status()
                    return
            if side == "local":
                with open(dest, "wb") as f:
                    f.write(data)
            else:
                self.request("edit_push", {"path": dest, "data_b64": data_b64})
        except Exception as e:
            return fail(str(e))
        self.host(tab, "reply", reqId=req_id, ok=True, side=side, path=dest)
        self.status(f"Saved as {dest} ({len(data)} bytes)", "done")
        self.push_state()

    # --- listing -------------------------------------------------------------

    def list_local(self, directory: str) -> list[dict[str, Any]]:
        try:
            entries = []
            with os.scandir(directory) as it:
                for d in it:
                    if should_skip_transfer_entry(d.name):
                        continue
                    try:
                        is_dir = d.is_dir()
                        size = d.stat().st_size if d.is_file() else 0
                    except OSError:
                        is_dir, size = False, 0
                    entries.append({"name": d.name, "isDir": is_dir, "size": size})
            entries.sort(key=lambda e: (not e["isDir"], e["name"].casefold(), e["name"]))
            return entries
        except OSError as e:
            self.status(f"local list failed: {e}")
            return []

    def list_remote(self, directory: str) -> list[dict[str, Any]]:
        if not self.connected:
            return []
        p = directory if directory and directory != "/" else "/"
        entries = self.request("fs_listdir", {"path": p}) or []
        return [e for e in entries if not should_skip_transfer_entry(e.get("name", ""))]

    def push_state(self) -> None:
        if not self.tabs():
            return
        remote_entries: list[dict[str, Any]] = []
        if self.connected:
            try:
                remote_entries = self.list_remote(self.remote_path)
            except Exception as e:
                self.status(f"remote list failed: {e}", "stalled")
        connected = self.connected
        self.post_all(
            {
                "type": "state",
                "connected": connected,
                "device": self.connected_device,
                "deviceInfo": self.device_info if connected else "",
                "interpreter": self.interpreter if connected else "",
                "localPath": self.local_path,
                # Hide the board path while disconnected; keep it for reconnect.
                "remotePath": self.remote_path if connected else "",
                "localEntries": self.list_local(self.local_path),
                "remoteEntries": remote_entries if connected else [],
            }
        )

    # --- transfers -------------------------------------------------------------

    def count_local_files(self, local: str) -> tuple[int, int]:
        if should_skip_transfer_entry(os.path.basename(local.rstrip("/\\"))):
            return 0, 1
        if os.path.isdir(local):
            files = skipped = 0
            for name in os.listdir(local):
                if should_skip_transfer_entry(name):
                    skipped += 1
                    continue
                f, s = self.count_local_files(os.path.join(local, name))
                files += f
                skipped += s
            return files, skipped
        os.stat(local)  # a missing path fails here, as statSync does
        return 1, 0

    def count_remote_files(self, remote: str) -> tuple[int, int]:
        name = ([p for p in remote.split("/") if p] or [""])[-1]
        if name and should_skip_transfer_entry(name):
            return 0, 1
        st = self.request("fs_stat", {"path": remote})
        if not st.get("isDir"):
            return 1, 0
        files = skipped = 0
        for e in self.request("fs_listdir", {"path": remote}) or []:
            if should_skip_transfer_entry(e.get("name", "")):
                skipped += 1
                continue
            f, s = self.count_remote_files(join_remote(remote, e["name"]))
            files += f
            skipped += s
        return files, skipped

    def _batch(
        self, verb: str, total: int, skipped: int, work: Callable[[TransferMonitor], None]
    ) -> None:
        self.transfer_busy = True
        try:
            monitor = TransferMonitor(verb, total, lambda text, phase: self.status(text, phase))
            monitor.start()
            try:
                # Over Wi-Fi the sidecar turns the board's power save off for the
                # whole batch and puts back the exact value afterwards, error or not.
                try:
                    self.request("transfer_begin")
                except Exception:
                    pass
                try:
                    work(monitor)
                finally:
                    try:
                        self.request("transfer_end")
                    except Exception:
                        pass
                skip_note = (
                    f", skipped {skipped} ignored {_plural_entries(skipped)}" if skipped else ""
                )
                past = "Uploaded" if verb == "Uploading" else "Downloaded"
                # transfer_busy still holds, so the idle status can't overwrite this.
                monitor.finish(f"{past} {total} file(s){skip_note}")
            except Exception:
                monitor.dispose()
                raise
        finally:
            self.transfer_busy = False

    def upload_many(self, tab: Any, local_paths: list[str]) -> None:
        if not self.connected:
            raise RuntimeError("not connected")
        if self.transfer_busy:
            self.status("A transfer is already in progress", "stalled")
            return
        total = skipped = 0
        for local in local_paths:
            f, s = self.count_local_files(local)
            total += f
            skipped += s
        if total == 0:
            self.status(
                f"Nothing to upload (skipped {skipped} ignored {_plural_entries(skipped)}: .git, __pycache__, …)"
                if skipped
                else "Nothing to upload"
            )
            return
        remote_dir = self.remote_path

        def work(monitor: TransferMonitor) -> None:
            for local in local_paths:
                self.upload_path(local, remote_dir, monitor)

        self._batch("Uploading", total, skipped, work)

    def upload_path(self, local: str, remote_dir: str, monitor: TransferMonitor) -> None:
        base = os.path.basename(local.rstrip("/\\"))
        if should_skip_transfer_entry(base):
            return
        if os.path.isdir(local):
            dest_dir = join_remote(remote_dir, base)
            try:
                self.request("fs_mkdir", {"path": dest_dir})
            except Exception:
                pass  # may exist
            for name in os.listdir(local):
                if should_skip_transfer_entry(name):
                    continue
                self.upload_path(os.path.join(local, name), dest_dir, monitor)
            return
        with open(local, "rb") as f:
            data = f.read()
        dest = join_remote(remote_dir, base)
        monitor.begin_file(dest)
        verify = bool(self._setting("verifyTransfers", True))
        data_b64 = base64.b64encode(data).decode("ascii")
        if self._setting("compileOnUpload", False):
            # The board may end up with a different path (.py -> .mpy on compile),
            # so verification has to happen sidecar-side against the compiled bytes.
            self.request(
                "fs_write", {"path": dest, "data_b64": data_b64, "mpy": True, "verify": verify}
            )
        else:
            self.request("fs_write", {"path": dest, "data_b64": data_b64})
            if verify:
                self.verify_remote_hash(dest, data)
        monitor.finish_file()

    def verify_remote_hash(self, remote: str, data: bytes) -> None:
        expect = hashlib.sha256(data).hexdigest()
        res = self.request("fs_hash", {"path": remote, "algo": "sha256"})
        if res.get("hash") != expect:
            raise RuntimeError(
                f"hash mismatch for {remote}: expected {expect}, got {res.get('hash')}"
            )

    def download_many(self, tab: Any, remote_paths: list[str]) -> None:
        if not self.connected:
            raise RuntimeError("not connected")
        if self.transfer_busy:
            self.status("A transfer is already in progress", "stalled")
            return
        total = skipped = 0
        for remote in remote_paths:
            f, s = self.count_remote_files(remote)
            total += f
            skipped += s
        if total == 0:
            self.status(
                f"Nothing to download (skipped {skipped} ignored {_plural_entries(skipped)})"
                if skipped
                else "Nothing to download"
            )
            return
        local_dir = self.local_path

        def work(monitor: TransferMonitor) -> None:
            for remote in remote_paths:
                self.download_path(remote, local_dir, monitor)

        self._batch("Downloading", total, skipped, work)

    def download_path(self, remote: str, local_dir: str, monitor: TransferMonitor) -> None:
        name = ([p for p in remote.split("/") if p] or ["file"])[-1]
        if should_skip_transfer_entry(name):
            return
        st = self.request("fs_stat", {"path": remote})
        if st.get("isDir"):
            dest_dir = os.path.join(local_dir, name)
            os.makedirs(dest_dir, exist_ok=True)
            for e in self.request("fs_listdir", {"path": remote}) or []:
                if should_skip_transfer_entry(e.get("name", "")):
                    continue
                self.download_path(join_remote(remote, e["name"]), dest_dir, monitor)
            return
        monitor.begin_file(remote)
        res = self.request("fs_read", {"path": remote})
        data = base64.b64decode(res.get("data_b64", ""))
        with open(os.path.join(local_dir, name), "wb") as f:
            f.write(data)
        if self._setting("verifyTransfers", True):
            self.verify_remote_hash(remote, data)
        monitor.finish_file()

    def run_remote_path(self, tab: Any, remote: str) -> None:
        """Soft-reset and exec a .py already on the board, then show the REPL."""
        self.status(f"Running {remote}…", "active")
        try:
            # Do not follow: UI apps loop forever. Output appears in the REPL.
            self.request("run_path", {"path": remote, "follow": False})
            self.status(f"Running {remote} — see REPL", "done")
            self.host(tab, "openRepl")
        except Exception as e:
            self.status(f"Run failed: {e}", "stalled")
            self.error(tab, f"mpftp run {remote}: {e}")

    def rm_many(self, tab: Any, remote_paths: list[str]) -> None:
        if not self.confirm(tab, f"Delete {len(remote_paths)} item(s) on the board?", "Delete"):
            return
        for p in remote_paths:
            self.status(f"Removing {p}…")
            self.request("fs_rm_rf", {"path": p})
        self.status("Deleted")

    def local_rm_many(self, tab: Any, local_paths: list[str]) -> None:
        if not self.confirm(tab, f"Delete {len(local_paths)} local item(s)?", "Delete"):
            return
        for p in local_paths:
            self.status(f"Removing {p}…")
            if os.path.isdir(p) and not os.path.islink(p):
                shutil.rmtree(p, ignore_errors=True)
            else:
                try:
                    os.remove(p)
                except FileNotFoundError:
                    pass
        self.status("Deleted")


def _stem_selection(name: str) -> tuple[int, int]:
    """Select the name without its extension, as the rename box does in VS Code."""
    dot = name.rfind(".")
    return (0, dot if dot > 0 else len(name))
