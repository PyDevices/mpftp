"""A serial port that answers the way a CircuitPython 11 board does.

The detail that matters for connect time is how CircuitPython changes REPL
mode. ``pyexec_friendly_repl`` and ``pyexec_raw_repl`` each *return* on Ctrl-A
or Ctrl-B, and ``run_repl`` in ``main.c`` then tears the VM down and starts it
again before running the other REPL. A MicroPython board flips modes at once.
Here each switch costs ``restart`` seconds, and bytes that arrive during the
restart are lost.

Also modelled, from ``main.c`` and ``pyexec.c``:

* ``code.py`` running: Ctrl-C ends it, flushes the USB receive FIFO, and after
  ``stop_delay`` the board prints "Press any key to enter the REPL".
* "Press any key" eats one byte; Ctrl-D reloads ``code.py`` instead. With
  ``deaf`` set, a byte that arrives within ``deaf`` seconds of the prompt is
  dropped, as a real board sometimes does.
* Raw REPL: Ctrl-A prints the banner again, Ctrl-C clears the line, Ctrl-D
  runs the line (an empty one is a soft reboot, which runs ``code.py``).
* Raw paste (Ctrl-E A Ctrl-A) with a 128-byte window.

``log`` records every byte written and read, with the time, so a test can say
where the seconds went.
"""

from __future__ import annotations

import struct
import threading
import time

BANNER = (
    b"\r\nAdafruit CircuitPython 11.0.0-alpha.2 on 2026-10-01; "
    b"Pimoroni Pico Plus 2 W with rp2350b\r\n"
)
PRESS_ANY_KEY = (
    b"Traceback (most recent call last):\r\n"
    b'  File "code.py", line 3, in <module>\r\n'
    b"KeyboardInterrupt: \r\n\r\n"
    b"Code done running.\r\n\r\n"
    b"Press any key to enter the REPL. Use CTRL-D to reload.\r\n"
)
RAW_BANNER = b"raw REPL; CTRL-B to exit\r\n"
WINDOW = 128


def run_board_code(src: bytes) -> bytes:
    """What the board prints for the few programs the sidecar sends."""
    text = src.decode("utf-8", "replace")
    if "_mpftp_rtc_set" in text:
        return b"False\r\n"  # clock already set
    if "sys.implementation.name" in text:
        return b"'circuitpython'\r\n"
    if text.strip() == "print(1)":
        return b"1\r\n"
    return b""


class FakeCircuitPython:
    def __init__(
        self,
        restart: float = 1.0,
        stop_delay: float = 2.0,
        code_py: bool = True,
        deaf: float = 0.0,
    ):
        self.restart = restart
        self.deaf = deaf
        self.stop_delay = stop_delay
        self.fifo = bytearray()
        self.out = bytearray()
        self.lock = threading.Lock()
        self.log: list[tuple[float, str, bytes]] = []
        self.t0 = time.monotonic()
        self.mode = "code" if code_py else "friendly"
        self.restarts = 0
        self.dtr = True
        self.rts = False
        self.timeout = None
        self._stop = False
        self._line = bytearray()
        threading.Thread(target=self._run, daemon=True).start()

    # -- pyserial surface ---------------------------------------------------
    def inWaiting(self) -> int:
        return len(self.out)

    in_waiting = property(inWaiting)

    def read(self, n: int = 1) -> bytes:
        deadline = time.monotonic() + 15
        while len(self.out) < n and time.monotonic() < deadline and not self._stop:
            time.sleep(0.002)
        with self.lock:
            data, self.out = bytes(self.out[:n]), self.out[n:]
        if data:
            self.log.append((time.monotonic() - self.t0, "rx", data))
        return data

    def write(self, data: bytes) -> int:
        self.log.append((time.monotonic() - self.t0, "tx", bytes(data)))
        with self.lock:
            self.fifo += data
        return len(data)

    def flushInput(self) -> None:
        with self.lock:
            self.out.clear()

    reset_input_buffer = flushInput

    def close(self) -> None:
        self._stop = True

    # -- the board ------------------------------------------------------------
    def _emit(self, data: bytes) -> None:
        with self.lock:
            self.out += data

    def _getc(self):
        while not self._stop:
            with self.lock:
                if self.fifo:
                    return self.fifo.pop(0)
            time.sleep(0.002)
        raise SystemExit

    def _restart(self) -> None:
        """run_repl() returning and starting again: the VM is rebuilt."""
        self.restarts += 1
        end = time.monotonic() + self.restart
        while time.monotonic() < end and not self._stop:
            time.sleep(0.002)
        with self.lock:
            self.fifo.clear()  # input that arrived meanwhile is gone

    def _run(self) -> None:
        try:
            while not self._stop:
                getattr(self, "_mode_" + self.mode)()
        except SystemExit:
            pass

    def _mode_code(self) -> None:
        if self._getc() == 3:
            with self.lock:
                self.fifo.clear()  # tud_cdc_n_read_flush
            time.sleep(self.stop_delay)
            self._emit(PRESS_ANY_KEY)
            self._prompted = time.monotonic()
            self.mode = "wait_key"

    def _mode_wait_key(self) -> None:
        c = self._getc()
        if time.monotonic() - self._prompted < self.deaf:
            return
        if c == 4:
            self.mode = "code"
            return
        self._restart()
        self.mode = "friendly"

    def _mode_friendly(self) -> None:
        self._emit(BANNER + b">>> ")
        while True:
            c = self._getc()
            if c == 1:
                self._emit(b"\r\n")
                self._restart()
                self.mode = "raw"
                return
            if c == 2:
                self._emit(BANNER + b">>> ")
            elif c == 3:
                self._emit(b"\r\n>>> ")
            elif c == 4:
                self._emit(b"\r\nsoft reboot\r\n")
                self.mode = "code"
                return

    def _mode_raw(self) -> None:
        self._emit(RAW_BANNER)
        self._line = bytearray()
        self._emit(b">")
        while True:
            c = self._getc()
            if c == 1:
                if len(self._line) == 2 and self._line[0] == 5:
                    self._raw_paste()
                    continue
                self._emit(RAW_BANNER + b">")
                self._line = bytearray()
            elif c == 2:
                self._emit(b"\r\n")
                self._restart()
                self.mode = "friendly"
                return
            elif c == 3:
                self._line = bytearray()
            elif c == 4:
                if not self._line:
                    self._emit(b"OK\r\nsoft reboot\r\n")
                    self.mode = "code"
                    return
                self._emit(b"OK" + run_board_code(bytes(self._line)) + b"\x04\x04>")
                self._line = bytearray()
            else:
                self._line.append(c)

    def _raw_paste(self) -> None:
        self._emit(b"R\x01" + struct.pack("<H", WINDOW) + b"\x01")
        data = bytearray()
        while True:
            c = self._getc()
            if c == 4:
                break
            data.append(c)
            if len(data) % WINDOW == 0:
                self._emit(b"\x01")
        self._emit(b"\x04" + run_board_code(bytes(data)) + b"\x04\x04>")
        self._line = bytearray()

    # -- reading the log ------------------------------------------------------
    def timeline(self) -> str:
        lines = []
        for at, kind, data in self.log:
            lines.append("%6.2fs %s %r" % (at, kind, data[:60]))
        return "\n".join(lines)
