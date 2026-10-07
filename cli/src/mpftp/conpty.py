"""A Windows program inside a pseudo console (ConPTY), for ``mpftp hold``.

Standard library only (ctypes). Windows-only: imported by the hold pump when
it runs under Windows Python.
"""

from __future__ import annotations

import os
import subprocess
from typing import Any, Optional

from .hold import vt_to_text


class ConPtyEnd:
    """A Windows program inside a pseudo console (ConPTY), with stdlib ctypes.

    ``micropython.exe`` reads keys with the console API, so a pipe is no use
    to it; a pseudo console is a real console that we type into and read
    back as a VT stream, whose escapes are stripped here. The program is put
    in a job object that kills it if this pump dies. The ctypes calls follow
    pydevices-examples' ``tools/prove_repl/prove_windows.py``.
    """

    def __init__(self, spec: dict[str, Any]) -> None:
        import ctypes
        import ctypes.wintypes as wt

        self.ct, self.wt = ctypes, wt
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self.k32 = k32

        class COORD(ctypes.Structure):
            _fields_ = [("X", ctypes.c_short), ("Y", ctypes.c_short)]

        class STARTUPINFOW(ctypes.Structure):
            _fields_ = [
                ("cb", wt.DWORD),
                ("lpReserved", wt.LPWSTR),
                ("lpDesktop", wt.LPWSTR),
                ("lpTitle", wt.LPWSTR),
                ("dwX", wt.DWORD),
                ("dwY", wt.DWORD),
                ("dwXSize", wt.DWORD),
                ("dwYSize", wt.DWORD),
                ("dwXCountChars", wt.DWORD),
                ("dwYCountChars", wt.DWORD),
                ("dwFillAttribute", wt.DWORD),
                ("dwFlags", wt.DWORD),
                ("wShowWindow", wt.WORD),
                ("cbReserved2", wt.WORD),
                ("lpReserved2", ctypes.c_void_p),
                ("hStdInput", wt.HANDLE),
                ("hStdOutput", wt.HANDLE),
                ("hStdError", wt.HANDLE),
            ]

        class STARTUPINFOEXW(ctypes.Structure):
            _fields_ = [("StartupInfo", STARTUPINFOW), ("lpAttributeList", ctypes.c_void_p)]

        class PROCESS_INFORMATION(ctypes.Structure):
            _fields_ = [
                ("hProcess", wt.HANDLE),
                ("hThread", wt.HANDLE),
                ("dwProcessId", wt.DWORD),
                ("dwThreadId", wt.DWORD),
            ]

        class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_int64),
                ("PerJobUserTimeLimit", ctypes.c_int64),
                ("LimitFlags", wt.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wt.DWORD),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", wt.DWORD),
                ("SchedulingClass", wt.DWORD),
            ]

        class IO_COUNTERS(ctypes.Structure):
            _fields_ = [(n, ctypes.c_uint64) for n in ("r", "w", "o", "rb", "wb", "ob")]

        class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
                ("IoInfo", IO_COUNTERS),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        H = wt.HANDLE
        k32.CreatePipe.argtypes = [ctypes.POINTER(H), ctypes.POINTER(H), ctypes.c_void_p, wt.DWORD]
        k32.CreatePseudoConsole.argtypes = [COORD, H, H, wt.DWORD, ctypes.POINTER(H)]
        k32.CreatePseudoConsole.restype = ctypes.c_long
        k32.InitializeProcThreadAttributeList.argtypes = [
            ctypes.c_void_p,
            wt.DWORD,
            wt.DWORD,
            ctypes.POINTER(ctypes.c_size_t),
        ]
        k32.UpdateProcThreadAttribute.argtypes = [
            ctypes.c_void_p,
            wt.DWORD,
            ctypes.c_size_t,
            ctypes.c_void_p,
            ctypes.c_size_t,
            ctypes.c_void_p,
            ctypes.c_void_p,
        ]
        k32.CreateProcessW.argtypes = [
            wt.LPCWSTR,
            wt.LPWSTR,
            ctypes.c_void_p,
            ctypes.c_void_p,
            wt.BOOL,
            wt.DWORD,
            ctypes.c_void_p,
            wt.LPCWSTR,
            ctypes.c_void_p,
            ctypes.POINTER(PROCESS_INFORMATION),
        ]
        k32.ReadFile.argtypes = [
            H,
            ctypes.c_void_p,
            wt.DWORD,
            ctypes.POINTER(wt.DWORD),
            ctypes.c_void_p,
        ]
        k32.WriteFile.argtypes = [
            H,
            ctypes.c_void_p,
            wt.DWORD,
            ctypes.POINTER(wt.DWORD),
            ctypes.c_void_p,
        ]
        k32.WaitForSingleObject.argtypes = [H, wt.DWORD]
        k32.GetExitCodeProcess.argtypes = [H, ctypes.POINTER(wt.DWORD)]
        k32.ClosePseudoConsole.argtypes = [H]
        k32.CloseHandle.argtypes = [H]
        k32.TerminateProcess.argtypes = [H, wt.UINT]
        k32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wt.LPCWSTR]
        k32.CreateJobObjectW.restype = H
        k32.SetInformationJobObject.argtypes = [H, ctypes.c_int, ctypes.c_void_p, wt.DWORD]
        k32.AssignProcessToJobObject.argtypes = [H, H]
        k32.ResumeThread.argtypes = [H]

        def check(ok: Any, what: str) -> None:
            if not ok:
                raise OSError(f"{what} failed: {ctypes.FormatError(ctypes.get_last_error())}")

        in_r, in_w, out_r, out_w = H(), H(), H(), H()
        check(k32.CreatePipe(ctypes.byref(in_r), ctypes.byref(in_w), None, 0), "CreatePipe")
        check(k32.CreatePipe(ctypes.byref(out_r), ctypes.byref(out_w), None, 0), "CreatePipe")
        self._hpc = H()
        # Wide, so a long line isn't wrapped into two by the console.
        rc = k32.CreatePseudoConsole(COORD(2000, 50), in_r, out_w, 0, ctypes.byref(self._hpc))
        if rc != 0:
            raise OSError("CreatePseudoConsole failed: 0x%08x" % (rc & 0xFFFFFFFF))
        k32.CloseHandle(in_r)
        k32.CloseHandle(out_w)
        self._in_w, self._out_r = in_w, out_r

        size = ctypes.c_size_t(0)
        k32.InitializeProcThreadAttributeList(None, 1, 0, ctypes.byref(size))
        attrs = ctypes.create_string_buffer(size.value)
        check(
            k32.InitializeProcThreadAttributeList(attrs, 1, 0, ctypes.byref(size)),
            "InitializeProcThreadAttributeList",
        )
        check(
            k32.UpdateProcThreadAttribute(
                attrs, 0, 0x00020016, self._hpc.value, ctypes.sizeof(H), None, None
            ),
            "UpdateProcThreadAttribute",
        )
        six = STARTUPINFOEXW()
        six.StartupInfo.cb = ctypes.sizeof(STARTUPINFOEXW)
        six.lpAttributeList = ctypes.addressof(attrs)
        # Our own std handles are pipes; with this flag and null handles the
        # child takes the pseudo console's instead of inheriting ours.
        six.StartupInfo.dwFlags = 0x00000100  # STARTF_USESTDHANDLES
        pi = PROCESS_INFORMATION()
        cmdline = ctypes.create_unicode_buffer(subprocess.list2cmdline(spec["argv"]))
        env = dict(os.environ)
        env.update(spec.get("env") or {})
        block = "".join("%s=%s\0" % kv for kv in sorted(env.items())) + "\0"
        envbuf = ctypes.create_unicode_buffer(block, len(block))
        flags = (
            0x00080000 | 0x00000400 | 0x00000004
        )  # EXTENDED_STARTUPINFO | UNICODE_ENV | SUSPENDED
        check(
            k32.CreateProcessW(
                None,
                cmdline,
                None,
                None,
                False,
                flags,
                envbuf,
                spec.get("cwd") or None,
                ctypes.byref(six),
                ctypes.byref(pi),
            ),
            "CreateProcessW",
        )
        self._hproc = pi.hProcess
        # Kill the program with this pump, however the pump ends.
        self._job = k32.CreateJobObjectW(None, None)
        if self._job:
            info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
            info.BasicLimitInformation.LimitFlags = 0x2000  # KILL_ON_JOB_CLOSE
            k32.SetInformationJobObject(self._job, 9, ctypes.byref(info), ctypes.sizeof(info))
            k32.AssignProcessToJobObject(self._job, self._hproc)
        k32.ResumeThread(pi.hThread)
        k32.CloseHandle(pi.hThread)
        self.info = {"pid": pi.dwProcessId, "argv": spec["argv"], "console": "conpty"}
        self._carry = b""
        self._buf = ctypes.create_string_buffer(65536)

    def read(self) -> Optional[bytes]:
        n = self.wt.DWORD()
        if not self.k32.ReadFile(self._out_r, self._buf, 65536, self.ct.byref(n), None):
            return None
        text, self._carry = vt_to_text(self._carry + self._buf.raw[: n.value])
        return text

    def write(self, data: bytes) -> None:
        n = self.wt.DWORD()
        if not self.k32.WriteFile(self._in_w, data, len(data), self.ct.byref(n), None):
            raise OSError("WriteFile to the console failed")

    def exit_note(self) -> str:
        code = self.wt.DWORD()
        self.k32.GetExitCodeProcess(self._hproc, self.ct.byref(code))
        return f"the program exited with code {code.value}"

    def close(self) -> None:
        if self._hpc:
            self.k32.ClosePseudoConsole(self._hpc)
            self._hpc = None
        if self.k32.WaitForSingleObject(self._hproc, 1000) != 0:
            self.k32.TerminateProcess(self._hproc, 9)
        for h in (self._hproc, self._in_w, self._job):
            if h:
                self.k32.CloseHandle(h)
