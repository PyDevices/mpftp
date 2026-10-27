"""
mpftp desktop host: the PWA's relay without the web server.

The desktop app shows mpftp's web UI in its own window and starts this
process beside it. Messages the page would have sent over its WebSocket
arrive here as JSON lines on stdin; everything the page would have
received goes out as JSON lines on stdout. In between is the same
``SidecarRelay`` the PWA uses, so the sidecar, the File Transfer panel and
the Firmware button behave exactly as they do in the browser.

There is one page, so there is one "tab", and no port for anything else on
the machine to reach.

Run: python -m mpftp.desktop_host
"""

from __future__ import annotations

import io
import os
import sys
import threading
from pathlib import Path


class _StdoutTab:
    """Stands in for a browser tab's WebSocket: one line out per message."""

    def __init__(self, out: io.TextIOBase) -> None:
        self._out = out
        self._lock = threading.Lock()

    def send_text(self, text: str) -> None:
        with self._lock:
            self._out.write(text + "\n")
            self._out.flush()


def main() -> None:
    # The channel to the page is this process's real stdout. Anything else
    # that prints (a library, a stray debug line) goes to stderr instead, so
    # it lands in the log and can't corrupt a message.
    channel = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", newline="\n")
    sys.stdout = sys.stderr
    incoming = io.TextIOWrapper(sys.stdin.buffer, encoding="utf-8", newline="\n")

    from .cli import resolve_python
    from .pwa import SidecarRelay

    relay = SidecarRelay(resolve_python(), local_path=str(Path.home()))
    tab = _StdoutTab(channel)
    relay.subscribe(tab)  # type: ignore[arg-type]
    print(f"mpftp desktop host: Local side {relay.panel.local_path}", file=sys.stderr)

    try:
        for line in incoming:
            line = line.rstrip("\n")
            if line:
                relay.send(line, tab)  # type: ignore[arg-type]
    except KeyboardInterrupt:
        pass
    finally:
        # stdin closed: the window is gone.
        relay.close()
        os._exit(0)


if __name__ == "__main__":
    main()
