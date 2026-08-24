#!/usr/bin/env python3
"""
mpftp local PWA — a loopback-only web UI (file transfer + REPL) for a
MicroPython/CircuitPython board, served entirely with the standard library.

Architecture: this process owns one long-lived ``mpftp.sidecar`` subprocess
(the same JSON-lines protocol the CLI and the VS Code extension speak) and
relays it to the browser over a hand-rolled WebSocket. The sidecar's
existing method surface (``connect``, ``fs_*``, ``repl_start``/``repl_data``,
...) is unchanged and reused as-is — the relay only touches request/response
``id`` fields, rewriting each to a server-assigned id so two browser tabs
independently starting their own id counter at 1 can never collide on the
one shared sidecar process (mpftp#20); a ``result``/``error`` is routed back
only to the tab that made the matching request, while unsolicited
``notify`` events (``repl_data`` and friends) still broadcast to every
connected tab, since those are board-initiated, not a reply to anyone.

This HTTP/WS port is a separate, explicitly launched service — distinct from
the VS Code extension's agent RPC port (ephemeral, closed until a board
connects; see AgentRpcServer). Running both at once is fine; they don't
share a board session.

Run: python -m mpftp   (no arguments)   or   python -m mpftp.pwa
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import http.server
import json
import os
import socket
import socketserver
import subprocess
import sys
import threading
import webbrowser
from pathlib import Path
from typing import Any, Optional

DEFAULT_PORT = 8317

_WS_MAGIC = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

_CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".mjs": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json",
    ".webmanifest": "application/manifest+json",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/x-icon",
    ".woff2": "font/woff2",
}


def _content_type(suffix: str) -> str:
    return _CONTENT_TYPES.get(suffix.lower(), "application/octet-stream")


def _ws_accept_key(client_key: str) -> str:
    """RFC 6455 handshake: base64(SHA-1(client key + the spec's magic GUID))."""
    digest = hashlib.sha1((client_key + _WS_MAGIC).encode("ascii")).digest()
    return base64.b64encode(digest).decode("ascii")


def _resolve_static_path(root: Path, url_path: str) -> Optional[Path]:
    """Map a request path onto a file under ``root``, or None (missing / escapes root)."""
    clean = url_path.split("?", 1)[0].split("#", 1)[0]
    if clean in ("", "/"):
        clean = "/index.html"
    rel = clean.lstrip("/")
    try:
        target = (root / rel).resolve()
    except (OSError, ValueError):
        return None
    root_resolved = root.resolve()
    if target != root_resolved and root_resolved not in target.parents:
        return None
    if not target.is_file():
        return None
    return target


class WebSocket:
    """Minimal RFC 6455 server-side frame codec — text frames only, no extensions."""

    def __init__(self, sock: socket.socket) -> None:
        self.sock = sock

    def send_text(self, text: str) -> None:
        self._send_frame(0x1, text.encode("utf-8"))

    def _send_frame(self, opcode: int, payload: bytes) -> None:
        header = bytes([0x80 | opcode])
        length = len(payload)
        if length < 126:
            header += bytes([length])
        elif length < 65536:
            header += bytes([126]) + length.to_bytes(2, "big")
        else:
            header += bytes([127]) + length.to_bytes(8, "big")
        self.sock.sendall(header + payload)

    def _recv_exact(self, n: int) -> bytes:
        buf = b""
        while len(buf) < n:
            chunk = self.sock.recv(n - len(buf))
            if not chunk:
                return b""
            buf += chunk
        return buf

    def _recv_frame(self) -> tuple[Optional[int], bytes]:
        b0 = self._recv_exact(1)
        if not b0:
            return None, b""
        opcode = b0[0] & 0x0F
        b1 = self._recv_exact(1)
        if not b1:
            return None, b""
        masked = bool(b1[0] & 0x80)
        length = b1[0] & 0x7F
        if length == 126:
            length = int.from_bytes(self._recv_exact(2), "big")
        elif length == 127:
            length = int.from_bytes(self._recv_exact(8), "big")
        mask_key = self._recv_exact(4) if masked else b""
        payload = self._recv_exact(length)
        if masked and mask_key:
            payload = bytes(b ^ mask_key[i % 4] for i, b in enumerate(payload))
        return opcode, payload

    def recv_text(self) -> Optional[str]:
        """Blocks for the next text frame; auto-replies to pings; None on close/EOF."""
        while True:
            opcode, payload = self._recv_frame()
            if opcode is None or opcode == 0x8:  # EOF or close
                return None
            if opcode == 0x9:  # ping -> pong
                self._send_frame(0xA, payload)
                continue
            if opcode == 0xA:  # pong, ignore
                continue
            return payload.decode("utf-8", "replace")

    def close(self) -> None:
        try:
            self._send_frame(0x8, b"")
        except OSError:
            pass
        try:
            self.sock.close()
        except OSError:
            pass


class SidecarRelay:
    """One long-lived ``mpftp.sidecar`` subprocess, shared by every connected tab.

    Each tab's own request ``id`` is rewritten to a server-assigned one on
    the way to the sidecar (and rewritten back on the way out), so two tabs
    independently starting their own counter at 1 can never collide on the
    single shared sidecar (mpftp#20) — a ``result``/``error`` only ever goes
    back to the tab that made the matching request. ``notify`` events (and
    anything without a recognized pending id) still broadcast to everyone.
    """

    def __init__(self, python: str) -> None:
        from .cli import _wslenv_forwarded_env

        self.python = python
        self.proc = subprocess.Popen(
            [python, "-m", "mpftp.sidecar"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            env=_wslenv_forwarded_env(python),
        )
        self._lock = threading.Lock()
        self._subscribers: list[WebSocket] = []
        self._next_id = 1
        self._pending: dict[int, tuple[WebSocket, Any]] = {}
        self._reader = threading.Thread(target=self._pump, daemon=True)
        self._reader.start()

    def _pump(self) -> None:
        assert self.proc.stdout
        for line in self.proc.stdout:
            line = line.rstrip("\n")
            if line:
                self._route(line)

    def _route(self, line: str) -> None:
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            msg = None
        if isinstance(msg, dict) and msg.get("type") in ("result", "error") and "id" in msg:
            with self._lock:
                entry = self._pending.pop(msg["id"], None)
            if entry is not None:
                ws, client_id = entry
                msg["id"] = client_id
                try:
                    ws.send_text(json.dumps(msg))
                except OSError:
                    self.unsubscribe(ws)
                return
        self._broadcast(line)

    def _broadcast(self, line: str) -> None:
        with self._lock:
            subscribers = list(self._subscribers)
        for ws in subscribers:
            try:
                ws.send_text(line)
            except OSError:
                self.unsubscribe(ws)

    def subscribe(self, ws: WebSocket) -> None:
        with self._lock:
            self._subscribers.append(ws)

    def unsubscribe(self, ws: WebSocket) -> None:
        with self._lock:
            if ws in self._subscribers:
                self._subscribers.remove(ws)
            stale = [rid for rid, (pending_ws, _) in self._pending.items() if pending_ws is ws]
            for rid in stale:
                del self._pending[rid]

    def send(self, line: str, ws: WebSocket) -> None:
        assert self.proc.stdin
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            msg = None
        if isinstance(msg, dict) and "id" in msg:
            with self._lock:
                server_id = self._next_id
                self._next_id += 1
                self._pending[server_id] = (ws, msg["id"])
            msg["id"] = server_id
            line = json.dumps(msg)
        self.proc.stdin.write(line + "\n")
        self.proc.stdin.flush()

    def close(self) -> None:
        try:
            self.proc.terminate()
            self.proc.wait(timeout=2)
        except Exception:
            try:
                self.proc.kill()
            except Exception:
                pass


class _ThreadingHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "mpftp-pwa/1"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args: object) -> None:
        if os.environ.get("MPFTP_PWA_VERBOSE"):
            super().log_message(fmt, *args)

    def do_GET(self) -> None:  # noqa: N802 (stdlib method name)
        if self.headers.get("Upgrade", "").lower() == "websocket":
            self._handle_ws_upgrade()
            return
        self._serve_static()

    def _serve_static(self) -> None:
        root: Path = self.server.webui_root  # type: ignore[attr-defined]
        target = _resolve_static_path(root, self.path)
        if target is None:
            self.send_error(404)
            return
        data = target.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", _content_type(target.suffix))
        self.send_header("Content-Length", str(len(data)))
        if target.suffix in (".html", ""):
            self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(data)

    def _handle_ws_upgrade(self) -> None:
        key = self.headers.get("Sec-WebSocket-Key", "")
        if not key:
            self.send_error(400, "missing Sec-WebSocket-Key")
            return
        accept = _ws_accept_key(key)
        self.send_response(101, "Switching Protocols")
        self.send_header("Upgrade", "websocket")
        self.send_header("Connection", "Upgrade")
        self.send_header("Sec-WebSocket-Accept", accept)
        self.end_headers()

        relay: SidecarRelay = self.server.relay  # type: ignore[attr-defined]
        ws = WebSocket(self.connection)
        relay.subscribe(ws)
        try:
            while True:
                text = ws.recv_text()
                if text is None:
                    break
                relay.send(text, ws)
        except OSError:
            pass
        finally:
            relay.unsubscribe(ws)
            ws.close()


def main(argv: Optional[list[str]] = None) -> None:
    parser = argparse.ArgumentParser(
        prog="mpftp.pwa",
        description="Local PWA: file transfer + REPL for a MicroPython/CircuitPython board.",
    )
    parser.add_argument(
        "--port", type=int, default=int(os.environ.get("MPFTP_PWA_PORT", DEFAULT_PORT))
    )
    parser.add_argument(
        "--no-open", action="store_true", help="Do not open a browser tab automatically."
    )
    ns = parser.parse_args(argv)

    root = Path(__file__).with_name("webui")
    if not root.is_dir():
        print(
            f"mpftp: no built UI at {root} — run `npm run build` in ui/ first",
            file=sys.stderr,
        )
        raise SystemExit(1)

    url = f"http://127.0.0.1:{ns.port}/"
    try:
        server = _ThreadingHTTPServer(("127.0.0.1", ns.port), Handler)
    except OSError as e:
        print(
            f"mpftp: could not bind 127.0.0.1:{ns.port} ({e}). Already running? Open {url}",
            file=sys.stderr,
        )
        if not ns.no_open:
            webbrowser.open(url)
        raise SystemExit(1) from None

    from .cli import resolve_python

    relay = SidecarRelay(resolve_python())
    server.webui_root = root  # type: ignore[attr-defined]
    server.relay = relay  # type: ignore[attr-defined]

    print(f"mpftp: serving {url}", file=sys.stderr)
    if not ns.no_open:
        webbrowser.open(url)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        relay.close()


if __name__ == "__main__":
    main()
