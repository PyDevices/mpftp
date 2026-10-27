"""End this process when the one that started it ends, however it ends.

The desktop app starts ``mpftp.desktop_host``, which starts
``mpftp.sidecar``. A normal close shuts both down in order. A force-kill of
the app (End task, ``Stop-Process -Force``) gives nobody the chance, and a
helper that is busy with the board, or that has lost its pipe but not its
serial port, can outlive it. Watching the parent directly doesn't depend on
the pipe or on what the helper is doing.

Opt in with ``MPFTP_EXIT_WITH_PARENT=1``: the desktop shell and host set it,
and everything else (the CLI, an editor extension) keeps running on its own.
"""

from __future__ import annotations

import os
import sys
import threading
import time

ENV = "MPFTP_EXIT_WITH_PARENT"


def _wait_windows(ppid: int) -> None:
    import ctypes
    from ctypes import wintypes

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.OpenProcess.restype = wintypes.HANDLE
    k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    handle = k32.OpenProcess(0x00100000, False, ppid)  # SYNCHRONIZE
    if not handle:
        # No such process: it is already gone.
        return
    k32.WaitForSingleObject(handle, 0xFFFFFFFF)


def _wait_posix(ppid: int) -> None:
    # A dead parent re-parents this process, so getppid() changes.
    while os.getppid() == ppid:
        time.sleep(0.5)


def _watch(ppid: int) -> None:
    try:
        if sys.platform == "win32":
            _wait_windows(ppid)
        else:
            _wait_posix(ppid)
    except Exception:
        return
    os._exit(0)


def exit_with_parent(force: bool = False) -> bool:
    """Start the watch when ``MPFTP_EXIT_WITH_PARENT`` is set (or ``force``)."""
    if not force and os.environ.get(ENV) != "1":
        return False
    ppid = os.getppid()
    if ppid <= 1:
        return False
    threading.Thread(target=_watch, args=(ppid,), daemon=True, name="parent-watch").start()
    return True
