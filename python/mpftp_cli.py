#!/usr/bin/env python3
"""
mpftp CLI — agent-friendly front-end to the mpftp sidecar / extension RPC.

Prefer the Cursor extension's Unix socket (~/.mpftp/rpc.sock) so CLI and UI
share one serial session. If the socket is missing, spawn sidecar.py directly
(standalone; requires --device for board ops).

Examples:
  mpftp status
  mpftp ports
  mpftp connect COM4
  mpftp ls /
  mpftp put ./main.py /main.py
  mpftp get /main.py ./main.py
  mpftp eval '1+1'
  mpftp exec 'print(42)'
  mpftp interrupt
  mpftp soft-reset     # MP: skip main.py
  mpftp soft-reboot    # Ctrl-D; runs main.py / code.py
  mpftp run script.py  # default --no-follow (UI-safe)
  mpftp debug-tee COM50
  mpftp watch          # tail activity log
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Optional

def _linux_home() -> Path:
    """Prefer the WSL/Linux home even if this script is run under Windows Python."""
    for key in ("HOME", "USERPROFILE"):
        pass
    # If we're Windows Python launched from WSL, USERPROFILE is Windows; agents use Linux paths.
    wsl = os.environ.get("WSL_DISTRO_NAME") or os.environ.get("WSL_INTEROP")
    linux_home = os.environ.get("HOME")
    if linux_home and (wsl or sys.platform.startswith("linux")):
        return Path(linux_home)
    # When python.exe runs with HOME unset to Linux, try /home/<user>
    if sys.platform == "win32":
        for cand in (
            os.environ.get("HOME"),
            "/home/" + os.environ.get("USER", ""),
            "/home/" + os.environ.get("USERNAME", "").lower(),
        ):
            if cand and cand.startswith("/home/") and Path(cand).is_dir():
                return Path(cand)
    return Path.home()


HOME_MPFTP = _linux_home() / ".mpftp"
# Also check Windows-side mirror when needed
WIN_MPFTP = Path.home() / ".mpftp"
ACTIVITY_LOG = HOME_MPFTP / "activity.log"
REPL_LOG = HOME_MPFTP / "repl.log"


def _parse_rpc_addr(text: str) -> Optional[tuple[str, int]]:
    text = (text or "").strip()
    if not text:
        return None
    # "127.0.0.1:7429" or legacy socket path
    if ":" in text and not text.startswith("/"):
        host, _, port_s = text.rpartition(":")
        try:
            return host.strip() or "127.0.0.1", int(port_s)
        except ValueError:
            return None
    return None


def _read_rpc_port_file(path: Path) -> Optional[tuple[str, int]]:
    try:
        if not path.is_file():
            return None
        return _parse_rpc_addr(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _read_workspace_rpc_registry(path: Path) -> dict[str, str]:
    try:
        if not path.is_file():
            return {}
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            return {}
        out: dict[str, str] = {}
        for k, v in raw.items():
            if isinstance(k, str) and isinstance(v, str) and v.strip():
                out[k] = v.strip()
        return out
    except Exception:
        return {}


def _workspace_rpc_from_registry(start: Optional[Path] = None) -> Optional[tuple[str, int]]:
    """Match cwd/parents against ``~/.mpftp/workspace-rpc.json`` (no repo litter)."""
    registries = [
        HOME_MPFTP / "workspace-rpc.json",
        WIN_MPFTP / "workspace-rpc.json",
    ]
    merged: dict[str, str] = {}
    for reg_path in registries:
        merged.update(_read_workspace_rpc_registry(reg_path))
    if not merged:
        return None
    # Normalize keys once for prefix matching.
    norm: dict[str, str] = {}
    for k, v in merged.items():
        try:
            norm[str(Path(k).resolve())] = v
        except Exception:
            norm[k] = v
    cur = (start or Path.cwd()).resolve()
    seen: set[Path] = set()
    for _ in range(48):
        if cur in seen:
            break
        seen.add(cur)
        addr = norm.get(str(cur))
        if addr:
            parsed = _parse_rpc_addr(addr)
            if parsed:
                return parsed
        if cur.parent == cur:
            break
        cur = cur.parent
    return None


HERE = Path(__file__).resolve().parent
SIDECAR = HERE / "sidecar.py"
FIRMWARE_ENGINE = HERE / "firmware_engine.py"


def _die(msg: str, code: int = 1) -> None:
    print(msg, file=sys.stderr)
    raise SystemExit(code)


def find_rpc_addr() -> Optional[tuple[str, int]]:
    """Return (host, port) for the extension AgentRpcServer, if running.

    Preference order:

    1. ``MPFTP_RPC`` env (``127.0.0.1:7429``)
    2. ``~/.mpftp/workspace-rpc.json`` match for cwd/parents (per-window, no repo litter)
    3. ``~/.mpftp/rpc.port`` home fallback (last writer among empty/global windows)
    4. Probe default port 7429
    """
    env = (os.environ.get("MPFTP_RPC") or "").strip()
    if env:
        parsed = _parse_rpc_addr(env)
        if parsed:
            return parsed

    from_reg = _workspace_rpc_from_registry()
    if from_reg:
        return from_reg

    for f in (
        HOME_MPFTP / "rpc.port",
        HOME_MPFTP / "rpc.path",
        WIN_MPFTP / "rpc.port",
        WIN_MPFTP / "rpc.path",
    ):
        parsed = _read_rpc_port_file(f)
        if parsed:
            return parsed

    # Probe default port
    try:
        with socket.create_connection(("127.0.0.1", 7429), timeout=0.3):
            return "127.0.0.1", 7429
    except Exception:
        return None

class RpcClient:
    def call(self, method: str, params: Optional[dict] = None) -> Any:
        raise NotImplementedError

    def close(self) -> None:
        pass


class TcpClient(RpcClient):
    def __init__(self, host: str, port: int) -> None:
        self.host = host
        self.port = port
        self._id = 0

    def call(self, method: str, params: Optional[dict] = None) -> Any:
        self._id += 1
        req = {"id": self._id, "method": method, "params": params or {}}
        with socket.create_connection((self.host, self.port), timeout=120) as s:
            s.sendall((json.dumps(req) + "\n").encode("utf-8"))
            buf = b""
            while True:
                chunk = s.recv(65536)
                if not chunk:
                    break
                buf += chunk
                if b"\n" in buf:
                    break
        line = buf.split(b"\n", 1)[0].decode("utf-8", "replace")
        msg = json.loads(line)
        if msg.get("type") == "error":
            raise RuntimeError(msg.get("error") or "rpc error")
        return msg.get("result")


class SidecarClient(RpcClient):
    """One-shot sidecar process; connect yourself before board ops."""

    def __init__(self, python: str) -> None:
        self.python = python
        self.proc = subprocess.Popen(
            [python, str(SIDECAR)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        self._id = 0
        assert self.proc.stdout
        # wait for ready
        deadline = time.time() + 20
        while time.time() < deadline:
            line = self.proc.stdout.readline()
            if not line:
                err = self.proc.stderr.read() if self.proc.stderr else ""
                _die(f"sidecar exited early: {err}")
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            if msg.get("type") == "notify" and msg.get("method") == "ready":
                break
        else:
            _die("sidecar ready timeout")

    def call(self, method: str, params: Optional[dict] = None) -> Any:
        assert self.proc.stdin and self.proc.stdout
        self._id += 1
        self.proc.stdin.write(json.dumps({"id": self._id, "method": method, "params": params or {}}) + "\n")
        self.proc.stdin.flush()
        while True:
            line = self.proc.stdout.readline()
            if not line:
                err = self.proc.stderr.read() if self.proc.stderr else ""
                raise RuntimeError(f"sidecar closed: {err}")
            msg = json.loads(line)
            if msg.get("type") == "notify":
                continue
            if msg.get("id") != self._id:
                continue
            if msg.get("type") == "error":
                raise RuntimeError(msg.get("error") or "sidecar error")
            return msg.get("result")

    def close(self) -> None:
        try:
            self.call("disconnect")
        except Exception:
            pass
        if self.proc:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=2)
            except Exception:
                self.proc.kill()


def resolve_python() -> str:
    env = os.environ.get("MPFTP_PYTHON")
    if env:
        return env
    # Prefer Windows python on WSL for COM ports
    for cand in (
        str(Path.home() / "bin" / "python.exe"),
        "python.exe",
        str(HERE.parent / ".venv" / "bin" / "python"),
        "python3",
        "python",
    ):
        try:
            r = subprocess.run(
                [cand, "-c", "import mpremote, serial; print('ok')"],
                capture_output=True,
                timeout=15,
            )
            if r.returncode == 0:
                return cand
        except Exception:
            continue
    return "python3"


def get_client(prefer_rpc: bool = True) -> tuple[RpcClient, str]:
    if prefer_rpc:
        addr = find_rpc_addr()
        if addr:
            host, port = addr
            client = TcpClient(host, port)
            try:
                # A surviving extension RPC listener can outlive its Python
                # sidecar after an editor/WSL restart.  Probe the sidecar, not
                # merely the TCP socket, before committing this command to RPC.
                client.call("ping")
                return client, f"tcp:{host}:{port}"
            except Exception:
                # Standalone mode is the documented recovery path when the
                # extension session is unavailable.  Board operations still
                # require --device so this does not guess a serial target.
                pass
    return SidecarClient(resolve_python()), "sidecar"


def out(obj: Any) -> None:
    if isinstance(obj, (dict, list)):
        print(json.dumps(obj, indent=2, ensure_ascii=False))
    else:
        print(obj)


def cmd_status(_: argparse.Namespace) -> None:
    addr = find_rpc_addr()
    info = {
        "rpc": f"{addr[0]}:{addr[1]}" if addr else None,
        "rpc_preference": "MPFTP_RPC > ~/.mpftp/workspace-rpc.json (cwd match) > ~/.mpftp/rpc.port",
        "workspace_rpc_registry": str(HOME_MPFTP / "workspace-rpc.json"),
        "activity_log": str(ACTIVITY_LOG),
        "repl_log": str(REPL_LOG),
        "extension_running": bool(addr),
    }
    if addr:
        try:
            client: RpcClient = TcpClient(*addr)
            info["session"] = client.call("agent_status")
        except Exception as e:
            info["session_error"] = str(e)
    out(info)


def cmd_ports(_: argparse.Namespace) -> None:
    client, _ = get_client()
    try:
        ports = client.call("list_ports")
        out(ports)
    finally:
        client.close()


def cmd_connect(ns: argparse.Namespace) -> None:
    client, mode = get_client()
    try:
        res = client.call("connect", {"device": ns.device, "baud": ns.baud})
        print(f"connected via {mode}: {res}", file=sys.stderr)
        out(res)
    finally:
        if mode.startswith("sidecar"):
            # keep process? one-shot connect is useless in sidecar mode without linger
            client.close()


def cmd_disconnect(_: argparse.Namespace) -> None:
    client, _ = get_client()
    try:
        out(client.call("disconnect"))
    finally:
        client.close()


def cmd_resume(ns: argparse.Namespace) -> None:
    client, mode = get_client()
    try:
        params: dict[str, Any] = {}
        if ns.baud:
            params["baud"] = ns.baud
        out(client.call("resume", params))
    finally:
        if mode.startswith("sidecar"):
            client.close()


def ensure_device(client: RpcClient, device: Optional[str], baud: int) -> None:
    if not device:
        return
    client.call("connect", {"device": device, "baud": baud})


def cmd_ls(ns: argparse.Namespace) -> None:
    client, mode = get_client()
    try:
        ensure_device(client, ns.device, ns.baud)
        entries = client.call("fs_listdir", {"path": ns.path})
        if ns.json:
            out(entries)
            return
        for e in entries or []:
            kind = "d" if e.get("isDir") else "-"
            print(f"{kind} {e.get('size', 0):8}  {e.get('name')}")
    finally:
        if mode.startswith("sidecar"):
            client.close()


def cmd_tree(ns: argparse.Namespace) -> None:
    client, mode = get_client()
    try:
        ensure_device(client, ns.device, ns.baud)
        out(client.call("fs_tree", {"path": ns.path}))
    finally:
        if mode.startswith("sidecar"):
            client.close()


def cmd_put(ns: argparse.Namespace) -> None:
    data = Path(ns.local).read_bytes()
    client, mode = get_client()
    try:
        ensure_device(client, ns.device, ns.baud)
        dest = ns.remote
        if getattr(ns, "recursive", False) or Path(ns.local).is_dir():
            out(
                client.call(
                    "fs_cp",
                    {
                        "src": str(Path(ns.local).resolve()),
                        "dest": ":" + dest if not dest.startswith(":") else dest,
                        "verify": bool(getattr(ns, "verify", False)),
                    },
                )
            )
            return
        res = client.call(
            "fs_write",
            {"path": dest, "data_b64": base64.b64encode(data).decode("ascii")},
        )
        if getattr(ns, "verify", False):
            import hashlib

            expect = hashlib.sha256(data).hexdigest()
            got = client.call("fs_hash", {"path": dest, "algo": "sha256"})["hash"]
            if got != expect:
                raise SystemExit(f"hash mismatch: expected {expect}, got {got}")
            res = {**res, "verified": got}
        out(res)
    finally:
        if mode.startswith("sidecar"):
            client.close()


def cmd_get(ns: argparse.Namespace) -> None:
    client, mode = get_client()
    try:
        ensure_device(client, ns.device, ns.baud)
        remote = ns.remote
        if getattr(ns, "recursive", False):
            out(
                client.call(
                    "fs_cp",
                    {
                        "src": ":" + remote if not remote.startswith(":") else remote,
                        "dest": str(Path(ns.local).resolve()),
                        "verify": bool(getattr(ns, "verify", False)),
                    },
                )
            )
            return
        res = client.call("fs_read", {"path": remote})
        raw = base64.b64decode(res["data_b64"])
        Path(ns.local).write_bytes(raw)
        if getattr(ns, "verify", False):
            import hashlib

            expect = client.call("fs_hash", {"path": remote, "algo": "sha256"})["hash"]
            got = hashlib.sha256(raw).hexdigest()
            if got != expect:
                raise SystemExit(f"hash mismatch: expected {expect}, got {got}")
        print(f"wrote {len(raw)} bytes → {ns.local}", file=sys.stderr)
    finally:
        if mode.startswith("sidecar"):
            client.close()


def cmd_cp(ns: argparse.Namespace) -> None:
    client, mode = get_client()
    try:
        ensure_device(client, ns.device, ns.baud)
        out(
            client.call(
                "fs_cp",
                {"src": ns.src, "dest": ns.dest, "verify": bool(ns.verify)},
            )
        )
    finally:
        if mode.startswith("sidecar"):
            client.close()


def cmd_hash(ns: argparse.Namespace) -> None:
    client, mode = get_client()
    try:
        ensure_device(client, ns.device, ns.baud)
        out(client.call("fs_hash", {"path": ns.path, "algo": ns.algo}))
    finally:
        if mode.startswith("sidecar"):
            client.close()


def cmd_edit(ns: argparse.Namespace) -> None:
    import os
    import tempfile

    editor = os.environ.get("EDITOR")
    if not editor:
        raise SystemExit("edit: $EDITOR not set")
    client, mode = get_client()
    try:
        ensure_device(client, ns.device, ns.baud)
        path = ns.path
        client.call("fs_touch", {"path": path})
        res = client.call("edit_pull", {"path": path})
        raw = base64.b64decode(res["data_b64"])
        fd, tmp = tempfile.mkstemp(suffix="-" + Path(path).name)
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(raw)
            rc = os.system(f'{editor} "{tmp}"')
            if rc != 0:
                raise SystemExit(f"editor exited {rc}")
            data = Path(tmp).read_bytes()
            out(
                client.call(
                    "edit_push",
                    {"path": path, "data_b64": base64.b64encode(data).decode("ascii")},
                )
            )
        finally:
            try:
                os.unlink(tmp)
            except OSError:
                pass
    finally:
        if mode.startswith("sidecar"):
            client.close()


def cmd_romfs(ns: argparse.Namespace) -> None:
    client, mode = get_client()
    try:
        if ns.romfs_cmd == "build":
            # build is host-only; still needs a client for method dispatch
            out(
                client.call(
                    "romfs_build",
                    {"path": ns.path, "output": ns.output, "mpy": not ns.no_mpy},
                )
            )
            return
        ensure_device(client, ns.device, ns.baud)
        if ns.romfs_cmd == "query":
            out(client.call("romfs_query"))
        elif ns.romfs_cmd == "deploy":
            out(
                client.call(
                    "romfs_deploy",
                    {
                        "path": ns.path,
                        "partition": ns.partition,
                        "mpy": not ns.no_mpy,
                    },
                )
            )
        else:
            raise SystemExit(f"unknown romfs command: {ns.romfs_cmd}")
    finally:
        if mode.startswith("sidecar"):
            client.close()


def cmd_mkdir(ns: argparse.Namespace) -> None:
    client, mode = get_client()
    try:
        ensure_device(client, ns.device, ns.baud)
        out(client.call("fs_mkdir", {"path": ns.path}))
    finally:
        if mode.startswith("sidecar"):
            client.close()


def cmd_rm(ns: argparse.Namespace) -> None:
    client, mode = get_client()
    try:
        ensure_device(client, ns.device, ns.baud)
        method = "fs_rm_rf" if ns.recursive else "fs_rm"
        out(client.call(method, {"path": ns.path}))
    finally:
        if mode.startswith("sidecar"):
            client.close()


def cmd_touch(ns: argparse.Namespace) -> None:
    client, mode = get_client()
    try:
        ensure_device(client, ns.device, ns.baud)
        out(client.call("fs_touch", {"path": ns.path}))
    finally:
        if mode.startswith("sidecar"):
            client.close()


def cmd_rename(ns: argparse.Namespace) -> None:
    client, mode = get_client()
    try:
        ensure_device(client, ns.device, ns.baud)
        out(client.call("fs_rename", {"src": ns.src, "dest": ns.dest}))
    finally:
        if mode.startswith("sidecar"):
            client.close()


def cmd_eval(ns: argparse.Namespace) -> None:
    client, mode = get_client()
    try:
        ensure_device(client, ns.device, ns.baud)
        out(client.call("eval", {"expr": ns.expr}))
    finally:
        if mode.startswith("sidecar"):
            client.close()


def cmd_exec(ns: argparse.Namespace) -> None:
    client, mode = get_client()
    try:
        ensure_device(client, ns.device, ns.baud)
        follow = not bool(getattr(ns, "no_follow", False))
        out(client.call("exec", {"code": ns.code, "follow": follow}))
    finally:
        if mode.startswith("sidecar"):
            client.close()


def cmd_run(ns: argparse.Namespace) -> None:
    source = Path(ns.file).read_text(encoding="utf-8")
    client, mode = get_client()
    try:
        ensure_device(client, ns.device, ns.baud)
        # Default no-follow so UI apps do not wedge the COM handle (mpftp#3).
        follow = bool(getattr(ns, "follow", False))
        out(client.call("run_script", {"source": source, "follow": follow}))
    finally:
        if mode.startswith("sidecar"):
            client.close()


def cmd_interrupt(ns: argparse.Namespace) -> None:
    client, mode = get_client()
    try:
        ensure_device(client, ns.device, ns.baud)
        out(client.call("interrupt"))
    finally:
        if mode.startswith("sidecar"):
            client.close()


def cmd_soft_reset(ns: argparse.Namespace) -> None:
    client, mode = get_client()
    try:
        ensure_device(client, ns.device, ns.baud)
        out(client.call("soft_reset"))
    finally:
        if mode.startswith("sidecar"):
            client.close()


def cmd_soft_reboot(ns: argparse.Namespace) -> None:
    client, mode = get_client()
    try:
        ensure_device(client, ns.device, ns.baud)
        out(client.call("soft_reboot"))
    finally:
        if mode.startswith("sidecar"):
            client.close()


def cmd_hard_reset(ns: argparse.Namespace) -> None:
    client, mode = get_client()
    try:
        ensure_device(client, ns.device, ns.baud)
        out(client.call("hard_reset"))
    finally:
        if mode.startswith("sidecar"):
            client.close()


def cmd_debug_tee(ns: argparse.Namespace) -> None:
    client, mode = get_client()
    try:
        if ns.stop:
            out(client.call("debug_tee_stop"))
            return
        if not ns.device_tee:
            raise SystemExit("debug-tee requires a device (e.g. COM50) or --stop")
        out(
            client.call(
                "debug_tee_start",
                {
                    "device": ns.device_tee,
                    "baud": ns.baud,
                    "log_path": ns.log_path,
                },
            )
        )
    finally:
        client.close()


def cmd_bootloader(ns: argparse.Namespace) -> None:
    client, mode = get_client()
    try:
        ensure_device(client, ns.device, ns.baud)
        out(client.call("bootloader"))
    finally:
        if mode.startswith("sidecar"):
            client.close()


def cmd_rtc(ns: argparse.Namespace) -> None:
    client, mode = get_client()
    try:
        ensure_device(client, ns.device, ns.baud)
        if ns.set:
            out(client.call("rtc_set"))
        else:
            out(client.call("rtc_get"))
    finally:
        if mode.startswith("sidecar"):
            client.close()


def cmd_df(ns: argparse.Namespace) -> None:
    client, mode = get_client()
    try:
        ensure_device(client, ns.device, ns.baud)
        out(client.call("df"))
    finally:
        if mode.startswith("sidecar"):
            client.close()


def cmd_mip(ns: argparse.Namespace) -> None:
    client, mode = get_client()
    try:
        ensure_device(client, ns.device, ns.baud)
        out(
            client.call(
                "mip_install",
                {"packages": ns.packages, "target": ns.target, "mpy": not ns.no_mpy},
            )
        )
    finally:
        if mode.startswith("sidecar"):
            client.close()


def cmd_circup(ns: argparse.Namespace) -> None:
    client, mode = get_client()
    try:
        ensure_device(client, ns.device, ns.baud)
        params: dict[str, Any] = {
            "packages": ns.packages,
            "target": ns.target or "/lib",
            "py": bool(ns.py),
            "prefer_web": not bool(ns.no_web),
        }
        if ns.host:
            params["host"] = ns.host
        if ns.password:
            params["password"] = ns.password
        out(client.call("circup_install", params))
    finally:
        if mode.startswith("sidecar"):
            client.close()


def cmd_mount(ns: argparse.Namespace) -> None:
    client, mode = get_client()
    try:
        ensure_device(client, ns.device, ns.baud)
        out(client.call("mount", {"path": ns.path, "unsafe_links": ns.unsafe_links}))
    finally:
        if mode.startswith("sidecar"):
            client.close()


def cmd_umount(ns: argparse.Namespace) -> None:
    client, mode = get_client()
    try:
        ensure_device(client, ns.device, ns.baud)
        out(client.call("umount"))
    finally:
        if mode.startswith("sidecar"):
            client.close()


def cmd_rpc(ns: argparse.Namespace) -> None:
    """Raw JSON-RPC: mpftp rpc METHOD [JSON_PARAMS]"""
    params = json.loads(ns.params) if ns.params else {}
    client, mode = get_client()
    try:
        ensure_device(client, ns.device, ns.baud)
        out(client.call(ns.method, params))
    finally:
        if mode.startswith("sidecar"):
            client.close()


def resolve_build_python() -> str:
    """A native (Linux on WSL) python3 to run the firmware engine + make."""
    env = os.environ.get("MPFTP_BUILD_PYTHON")
    if env:
        return env
    import shutil

    if sys.platform != "win32":
        for cand in ("python3", "python"):
            p = shutil.which(cand)
            if p:
                return p
    return sys.executable or "python3"


def _engine_argv(cmd: str, extra: list[str]) -> list[str]:
    return [resolve_build_python(), str(FIRMWARE_ENGINE), cmd, *extra]


def _engine_json(cmd: str, extra: list[str]) -> Any:
    r = subprocess.run(_engine_argv(cmd, extra), capture_output=True, text=True)
    if r.returncode != 0 and not r.stdout.strip():
        _die(r.stderr.strip() or f"engine {cmd} failed")
    try:
        return json.loads(r.stdout)
    except Exception:
        _die(r.stderr.strip() or r.stdout.strip() or f"engine {cmd}: bad output")


def _engine_stream(cmd: str, extra: list[str]) -> dict:
    """Run a streaming engine command; log lines -> stderr, return final result."""
    proc = subprocess.Popen(
        _engine_argv(cmd, extra),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    result: dict = {}
    assert proc.stdout
    for line in proc.stdout:
        line = line.rstrip("\n")
        if not line:
            continue
        try:
            msg = json.loads(line)
        except Exception:
            print(line, file=sys.stderr)
            continue
        if msg.get("type") == "log":
            print(msg.get("line", ""), file=sys.stderr)
        elif msg.get("type") == "result":
            result = msg
    proc.wait()
    return result or {"ok": proc.returncode == 0, "returncode": proc.returncode}


def _resolve_mp(ns: argparse.Namespace) -> Optional[str]:
    if getattr(ns, "mp", None):
        return ns.mp
    cwd = Path.cwd().resolve()
    workspace_hints = os.pathsep.join(str(path) for path in (cwd, *cwd.parents))
    info = _engine_json("discover", ["--workspace", workspace_hints])
    return info.get("micropython")


def _sel_args(ns: argparse.Namespace) -> list[str]:
    args: list[str] = []
    mp = _resolve_mp(ns)
    if mp:
        args += ["--mp", mp]
    if getattr(ns, "port", None):
        args += ["--port", ns.port]
    if getattr(ns, "board", None):
        args += ["--board", ns.board]
    if getattr(ns, "variant", None):
        args += ["--variant", ns.variant]
    return args


def cmd_firmware(ns: argparse.Namespace) -> None:
    sub = ns.fw_cmd
    if sub == "list":
        extra = ["--mp", ns.mp] if getattr(ns, "mp", None) else []
        out(_engine_json("tree", extra))
        return
    if sub == "discover":
        extra = ["--mp", ns.mp] if getattr(ns, "mp", None) else []
        out(_engine_json("discover", extra))
        return
    if sub == "cmods":
        extra = ["--mp", ns.mp] if getattr(ns, "mp", None) else []
        out(_engine_json("cmods", extra))
        return
    if sub == "artifact":
        out(_engine_json("artifact", _sel_args(ns)))
        return
    if sub == "build":
        extra = _sel_args(ns)
        if ns.clean:
            extra.append("--clean")
        res = _engine_stream("build", extra)
        out(res)
        if not res.get("ok"):
            raise SystemExit(1)
        return
    if sub == "clean":
        res = _engine_stream("clean", _sel_args(ns))
        out(res)
        return
    if sub == "flash":
        extra = _sel_args(ns)
        if ns.device:
            extra += ["--device", ns.device]
        if getattr(ns, "artifact", None):
            extra += ["--artifact", ns.artifact]
        if getattr(ns, "family", None):
            extra += ["--family", ns.family]
        if getattr(ns, "erase", False):
            extra.append("--erase")
        if getattr(ns, "uf2", False):
            extra.append("--uf2")
        if getattr(ns, "uf2_timeout", 0):
            extra += ["--uf2-timeout", str(ns.uf2_timeout)]
        res = _engine_stream("flash", extra)
        out(res)
        if not res.get("ok"):
            raise SystemExit(1)
        return
    if sub == "download-tree":
        extra = []
        if getattr(ns, "force", False):
            extra.append("--force")
        out(_engine_json("download-tree", extra))
        return
    if sub == "download-list":
        extra = ["--board", ns.board]
        if getattr(ns, "variant", None):
            extra += ["--variant", ns.variant]
        if getattr(ns, "preview", False):
            extra.append("--preview")
        if getattr(ns, "force", False):
            extra.append("--force")
        out(_engine_json("download-list", extra))
        return
    if sub == "download":
        extra = ["--board", ns.board]
        if getattr(ns, "variant", None):
            extra += ["--variant", ns.variant]
        if getattr(ns, "version", None):
            extra += ["--version", ns.version]
        if getattr(ns, "preview", False):
            extra.append("--preview")
        if getattr(ns, "force", False):
            extra.append("--force")
        res = _engine_stream("download", extra)
        out(res)
        if not res.get("ok"):
            raise SystemExit(1)
        return
    if sub == "detect":
        extra = []
        mp = _resolve_mp(ns)
        if mp:
            extra += ["--mp", mp]
        extra += ["--device", ns.device]
        if getattr(ns, "baud", None):
            extra += ["--baud", str(ns.baud)]
        if getattr(ns, "mp_hints", None):
            extra += ["--mp-hints", ns.mp_hints]
        out(_engine_json("detect", extra))
        return
    if sub == "partitions":
        extra = [ns.part_action]
        mp = _resolve_mp(ns)
        if mp:
            extra += ["--mp", mp]
        if getattr(ns, "board", None):
            extra += ["--board", ns.board]
        if getattr(ns, "variant", None):
            extra += ["--variant", ns.variant]
        if ns.part_action == "set":
            if getattr(ns, "csv_file", None):
                extra += ["--csv-file", ns.csv_file]
            elif getattr(ns, "rows", None):
                extra += ["--rows", ns.rows]
            else:
                _die("partitions set requires --csv-file or --rows")
        elif ns.part_action == "split":
            if getattr(ns, "storage_bytes", None):
                extra += ["--storage-bytes", str(ns.storage_bytes)]
            if getattr(ns, "flash_bytes", None):
                extra += ["--flash-bytes", str(ns.flash_bytes)]
            if getattr(ns, "flash_mb", None):
                extra += ["--flash-mb", str(ns.flash_mb)]
        out(_engine_json("partitions", extra))
        return
    _die(f"unknown firmware command: {sub}")


def cmd_watch(ns: argparse.Namespace) -> None:
    path = Path(ns.file) if ns.file else (REPL_LOG if ns.repl else ACTIVITY_LOG)
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
    print(f"watching {path}", file=sys.stderr)
    with path.open("r", encoding="utf-8", errors="replace") as f:
        if not ns.from_start:
            f.seek(0, os.SEEK_END)
        while True:
            line = f.readline()
            if line:
                sys.stdout.write(line)
                sys.stdout.flush()
            else:
                time.sleep(0.25)


def build_parser() -> argparse.ArgumentParser:
    device_opts = argparse.ArgumentParser(add_help=False)
    device_opts.add_argument(
        "--device",
        "-d",
        dest="device",
        default=None,
        help="Serial device (standalone / force connect)",
    )
    device_opts.add_argument("--baud", type=int, default=115200)

    p = argparse.ArgumentParser(prog="mpftp", description="mpftp agent CLI (mpremote via sidecar)")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("status", help="RPC socket + session status").set_defaults(func=cmd_status)
    sub.add_parser("ports", parents=[device_opts], help="List serial ports").set_defaults(func=cmd_ports)

    c = sub.add_parser("connect", parents=[device_opts], help="Connect to device")
    c.add_argument("device_pos", metavar="DEVICE", help="e.g. COM4 or /dev/ttyACM0")
    c.set_defaults(func=cmd_connect)

    sub.add_parser("disconnect", parents=[device_opts], help="Disconnect").set_defaults(func=cmd_disconnect)
    sub.add_parser("resume", parents=[device_opts], help="Reconnect to last device").set_defaults(
        func=cmd_resume
    )

    ls = sub.add_parser("ls", parents=[device_opts], help="List board directory")
    ls.add_argument("path", nargs="?", default="/")
    ls.add_argument("--json", action="store_true")
    ls.set_defaults(func=cmd_ls)

    tr = sub.add_parser("tree", parents=[device_opts], help="Tree board directory")
    tr.add_argument("path", nargs="?", default="/")
    tr.set_defaults(func=cmd_tree)

    put = sub.add_parser("put", parents=[device_opts], help="Upload local file to board")
    put.add_argument("local")
    put.add_argument("remote")
    put.add_argument("-r", "--recursive", action="store_true", help="Copy directories via fs_cp")
    put.add_argument("--verify", action="store_true", help="SHA-256 verify after transfer")
    put.set_defaults(func=cmd_put)

    get = sub.add_parser("get", parents=[device_opts], help="Download board file to local")
    get.add_argument("remote")
    get.add_argument("local")
    get.add_argument("-r", "--recursive", action="store_true", help="Copy directories via fs_cp")
    get.add_argument("--verify", action="store_true", help="SHA-256 verify after transfer")
    get.set_defaults(func=cmd_get)

    cp = sub.add_parser(
        "cp",
        parents=[device_opts],
        help="Copy (use : prefix for board paths, e.g. ./a.py :/a.py)",
    )
    cp.add_argument("src")
    cp.add_argument("dest")
    cp.add_argument("--verify", action="store_true")
    cp.set_defaults(func=cmd_cp)

    hx = sub.add_parser("hash", parents=[device_opts], help="SHA-256 (or algo) of board file")
    hx.add_argument("path")
    hx.add_argument("--algo", default="sha256")
    hx.set_defaults(func=cmd_hash)

    ed = sub.add_parser("edit", parents=[device_opts], help="Edit board file with $EDITOR")
    ed.add_argument("path")
    ed.set_defaults(func=cmd_edit)

    mk = sub.add_parser("mkdir", parents=[device_opts], help="Create board directory")
    mk.add_argument("path")
    mk.set_defaults(func=cmd_mkdir)

    rm = sub.add_parser("rm", parents=[device_opts], help="Remove board file (or -r tree)")
    rm.add_argument("path")
    rm.add_argument("-r", "--recursive", action="store_true")
    rm.set_defaults(func=cmd_rm)

    touch = sub.add_parser("touch", parents=[device_opts], help="Create empty board file")
    touch.add_argument("path")
    touch.set_defaults(func=cmd_touch)

    ren = sub.add_parser("rename", parents=[device_opts], help="Rename board path")
    ren.add_argument("src")
    ren.add_argument("dest")
    ren.set_defaults(func=cmd_rename)

    ev = sub.add_parser("eval", parents=[device_opts], help="Eval expression on board")
    ev.add_argument("expr")
    ev.set_defaults(func=cmd_eval)

    ex = sub.add_parser("exec", parents=[device_opts], help="Exec code on board")
    ex.add_argument("code")
    ex.add_argument(
        "--no-follow",
        action="store_true",
        help="Do not wait for raw-REPL EOF (use for long-running / UI code)",
    )
    ex.set_defaults(func=cmd_exec)

    run = sub.add_parser(
        "run",
        parents=[device_opts],
        help="Run local script on board (default: --no-follow)",
    )
    run.add_argument("file")
    run.add_argument(
        "--follow",
        action="store_true",
        help="Wait for the script to finish (default is no-follow for UI apps)",
    )
    run.set_defaults(func=cmd_run)

    sub.add_parser(
        "interrupt",
        parents=[device_opts],
        help="Send Ctrl-C (interrupt running program; no reset)",
    ).set_defaults(func=cmd_interrupt)
    sub.add_parser(
        "soft-reset",
        parents=[device_opts],
        help="Soft reset (MP: skip main.py; CP: friendly↔raw, does not run code.py)",
    ).set_defaults(func=cmd_soft_reset)
    sub.add_parser(
        "soft-reboot",
        parents=[device_opts],
        help="Friendly Ctrl-D soft-reboot (runs main.py / code.py)",
    ).set_defaults(func=cmd_soft_reboot)
    sub.add_parser("hard-reset", parents=[device_opts], help="Hard reset").set_defaults(func=cmd_hard_reset)
    sub.add_parser("bootloader", parents=[device_opts], help="Enter bootloader").set_defaults(
        func=cmd_bootloader
    )

    dtee = sub.add_parser(
        "debug-tee",
        help="Read-only monitor on a second COM (e.g. ESP native USB CDC)",
    )
    dtee.add_argument(
        "device_tee",
        nargs="?",
        help="Second serial device (required unless --stop)",
    )
    dtee.add_argument("--baud", type=int, default=115200)
    dtee.add_argument("--log-path", help="Append raw bytes (default ~/.mpftp/debug-tee.log)")
    dtee.add_argument("--stop", action="store_true", help="Stop an active debug tee")
    dtee.set_defaults(func=cmd_debug_tee)

    rtc = sub.add_parser("rtc", parents=[device_opts], help="Get or set RTC")
    rtc.add_argument("--set", action="store_true", help="Set RTC from host")
    rtc.set_defaults(func=cmd_rtc)

    sub.add_parser("df", parents=[device_opts], help="Disk free").set_defaults(func=cmd_df)

    mip = sub.add_parser("mip", parents=[device_opts], help="mip install package(s) (MicroPython)")
    mip.add_argument("packages", nargs="+")
    mip.add_argument(
        "--target",
        default="/lib",
        help="Board install directory (default: /lib)",
    )
    mip.add_argument("--no-mpy", action="store_true")
    mip.set_defaults(func=cmd_mip)

    circ = sub.add_parser(
        "circup",
        parents=[device_opts],
        help="circup install package(s) (prefers Web Workflow when Wi-Fi is up)",
    )
    circ.add_argument("packages", nargs="+")
    circ.add_argument("--target", default="/lib")
    circ.add_argument(
        "--py",
        action="store_true",
        help="Install .py sources instead of .mpy",
    )
    circ.add_argument("--host", help="Web Workflow host/IP (default: probe board Wi-Fi)")
    circ.add_argument(
        "--password",
        help="Web Workflow password (default: CIRCUITPY_WEB_API_PASSWORD / env)",
    )
    circ.add_argument(
        "--no-web",
        action="store_true",
        help="Skip Web Workflow; use USB staging/serial/MSC only",
    )
    circ.set_defaults(func=cmd_circup)

    mnt = sub.add_parser("mount", parents=[device_opts], help="Mount local path on board (MicroPython)")
    mnt.add_argument("path")
    mnt.add_argument("--unsafe-links", action="store_true")
    mnt.set_defaults(func=cmd_mount)
    sub.add_parser("umount", parents=[device_opts], help="Umount local mount (MicroPython)").set_defaults(func=cmd_umount)

    rom = sub.add_parser("romfs", parents=[device_opts], help="ROMFS query/build/deploy (MicroPython)")
    rom.add_argument("romfs_cmd", choices=["query", "build", "deploy"])
    rom.add_argument("path", nargs="?", help="Source dir or .romfs image (build/deploy)")
    rom.add_argument("-o", "--output", help="Output file for build")
    rom.add_argument("--partition", type=int, default=0)
    rom.add_argument("--no-mpy", action="store_true")
    rom.set_defaults(func=cmd_romfs)

    rpc = sub.add_parser("rpc", parents=[device_opts], help="Raw RPC method")
    rpc.add_argument("method")
    rpc.add_argument("params", nargs="?", help='JSON object, e.g. {"path":"/"}')
    rpc.set_defaults(func=cmd_rpc)

    fw = sub.add_parser("firmware", help="Build & flash MicroPython firmware (host-side)")
    fwsub = fw.add_subparsers(dest="fw_cmd", required=True)

    fw_sel = argparse.ArgumentParser(add_help=False)
    fw_sel.add_argument("--mp", help="MicroPython tree path (auto-discovered if omitted)")
    fw_sel.add_argument("--port", help="MicroPython port, e.g. esp32")
    fw_sel.add_argument("--board", default="", help="Board name")
    fw_sel.add_argument("--variant", default="", help="Board/port variant")

    fwsub.add_parser("list", parents=[fw_sel], help="List ports/boards/variants").set_defaults(
        func=cmd_firmware
    )
    fwsub.add_parser("discover", parents=[fw_sel], help="Show resolved MP/IDF/emsdk paths").set_defaults(
        func=cmd_firmware
    )
    fwsub.add_parser("cmods", parents=[fw_sel], help="List discovered user C modules").set_defaults(
        func=cmd_firmware
    )
    fwsub.add_parser("artifact", parents=[fw_sel], help="Report built firmware for a selection").set_defaults(
        func=cmd_firmware
    )

    fwb = fwsub.add_parser("build", parents=[fw_sel], help="Build firmware (streams log)")
    fwb.add_argument("--clean", action="store_true", help="Clean before building")
    fwb.set_defaults(func=cmd_firmware)

    fwsub.add_parser("clean", parents=[fw_sel], help="Clean a selection").set_defaults(
        func=cmd_firmware
    )

    fwf = fwsub.add_parser("flash", parents=[fw_sel, device_opts], help="Flash a built or downloaded artifact")
    fwf.add_argument("--artifact", help="Explicit firmware file (else last build)")
    fwf.add_argument("--family", default="", help="MCU family for flash offset (download mode)")
    fwf.add_argument("--erase", action="store_true", help="esp32: erase flash first")
    fwf.add_argument("--uf2", action="store_true",
                     help="Copy a .uf2 to a bootloader volume instead of flashing over serial")
    fwf.add_argument("--uf2-timeout", dest="uf2_timeout", type=float, default=0.0,
                     help="Seconds to wait for the volume to unmount (default 30)")
    fwf.set_defaults(func=cmd_firmware)

    fwdt = fwsub.add_parser(
        "download-tree", help="Official firmware catalog (Thonny JSON → micropython.org)"
    )
    fwdt.add_argument("--force", action="store_true", help="Refresh catalog cache")
    fwdt.set_defaults(func=cmd_firmware)

    fwdlist = fwsub.add_parser("download-list", help="List downloadable versions for a board")
    fwdlist.add_argument("--board", required=True)
    fwdlist.add_argument(
        "--variant",
        default="",
        help="MP board variant (e.g. C6_WIFI)",
    )
    fwdlist.add_argument("--preview", action="store_true", help="Probe board page for latest preview")
    fwdlist.add_argument("--force", action="store_true")
    fwdlist.set_defaults(func=cmd_firmware)

    fwdd = fwsub.add_parser("download", help="Download official firmware for a board")
    fwdd.add_argument("--board", required=True)
    fwdd.add_argument(
        "--variant",
        default="",
        help="MP board variant (e.g. C6_WIFI)",
    )
    fwdd.add_argument("--version", default="", help="Release version (e.g. 1.28.0)")
    fwdd.add_argument("--preview", action="store_true", help="Latest preview build")
    fwdd.add_argument("--force", action="store_true", help="Refresh catalog cache")
    fwdd.set_defaults(func=cmd_firmware)

    fwd = fwsub.add_parser("detect", parents=[fw_sel, device_opts],
                           help="esptool-first chip/flash/security probe")
    fwd.add_argument("--mp-hints", dest="mp_hints",
                     help="JSON of MicroPython interpreter hints (optional)")
    fwd.set_defaults(func=cmd_firmware)

    fwp = fwsub.add_parser("partitions", parents=[fw_sel], help="esp32 partition override")
    fwp.add_argument("part_action", choices=["get", "set", "reset", "candidates", "split"])
    fwp.add_argument("--rows", help="JSON array of partition rows (set)")
    fwp.add_argument("--csv-file", dest="csv_file", help="CSV file to import (set)")
    fwp.add_argument("--storage-bytes", dest="storage_bytes", type=int,
                     help="storage partition size in bytes (split)")
    fwp.add_argument("--flash-bytes", dest="flash_bytes", type=int,
                     help="total flash in bytes (split)")
    fwp.add_argument("--flash-mb", dest="flash_mb", type=int,
                     help="flash size in MB for the sdkconfig fragment (split)")
    fwp.set_defaults(func=cmd_firmware)

    w = sub.add_parser("watch", help="Tail activity or REPL log")
    w.add_argument("--repl", action="store_true", help="Watch REPL log instead of activity")
    w.add_argument("--file", help="Custom log path")
    w.add_argument("--from-start", action="store_true")
    w.set_defaults(func=cmd_watch)

    return p


def main(argv: Optional[list[str]] = None) -> None:
    parser = build_parser()
    ns = parser.parse_args(argv)
    if getattr(ns, "cmd", None) == "connect":
        ns.device = ns.device_pos
    elif not hasattr(ns, "device"):
        ns.device = None
    if not hasattr(ns, "baud"):
        ns.baud = 115200
    try:
        ns.func(ns)
    except BrokenPipeError:
        pass
    except Exception as e:
        _die(str(e))


if __name__ == "__main__":
    main()
