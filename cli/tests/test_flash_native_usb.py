# SPDX-FileCopyrightText: 2026 Brad Barnett
#
# SPDX-License-Identifier: MIT

"""`firmware flash` reaches a native-USB ESP32 running MicroPython (mpftp#208),
and passes --before and --after through to the engine (mpftp#209).

MicroPython's TinyUSB CDC (303A:4001) ignores DTR and RTS, so esptool's reset
keeps talking to the running interpreter. The flash has to send the board to
its bootloader and flash the ROM's port (303A:1001) it comes back on.
"""

from __future__ import annotations

import io
import unittest
from contextlib import redirect_stderr
from unittest import mock

from mpftp import cli

CDC = {"device": "COM104", "vid": 0x303A, "pid": 0x4001, "serial_number": "C04E3003F6300000"}
OTHER_CDC = {"device": "COM105", "vid": 0x303A, "pid": 0x4001, "serial_number": "70041DAD86C40000"}
ROM = {"device": "COM103", "vid": 0x303A, "pid": 0x1001, "serial_number": "C0:4E:30:03:F6:30"}
OTHER_ROM = {"device": "COM102", "vid": 0x303A, "pid": 0x1001, "serial_number": "70:04:1D:AD:86:C4"}
UART = {"device": "COM7", "vid": 0x10C4, "pid": 0xEA60, "serial_number": "0001"}


class FakeClient:
    """Answers list_ports from a script of port lists, and records calls."""

    def __init__(self, port_lists, bootloader_ok=True):
        self.port_lists = list(port_lists)
        self.calls = []
        self.bootloader_ok = bootloader_ok

    def call(self, method, params=None):
        self.calls.append(method)
        if method == "list_ports":
            return self.port_lists.pop(0) if len(self.port_lists) > 1 else self.port_lists[0]
        if method == "connect":
            return {"ok": True}
        if method == "bootloader":
            return {"ok": self.bootloader_ok, "error": None if self.bootloader_ok else "no REPL"}
        raise AssertionError(method)

    def close(self):
        pass


def run_flash(argv, client, engine_result=None):
    """Parse ``argv`` with the real parser, run it, return the engine's args."""
    ns = cli.build_parser().parse_args(argv)
    seen = {}

    def engine(cmd, extra):
        seen["cmd"], seen["extra"] = cmd, extra
        return engine_result or {"ok": True}

    with mock.patch.object(cli, "get_client", return_value=(client, "sidecar")), \
            mock.patch.object(cli, "_engine_stream", side_effect=engine), \
            mock.patch.object(cli, "_sel_args", return_value=["--port", ns.port]), \
            mock.patch.object(cli, "boards"), \
            mock.patch.object(cli.time, "sleep"), \
            mock.patch.object(cli, "out"), \
            redirect_stderr(io.StringIO()):
        cli.cmd_firmware(ns)
    return seen["extra"]


BASE = ["firmware", "flash", "--port", "esp32", "--artifact", "fw.bin", "--offset", "0x10000"]


class NativeUsbFlash(unittest.TestCase):
    def test_native_usb_board_is_flashed_on_its_rom_port(self):
        client = FakeClient([[CDC, OTHER_CDC, OTHER_ROM], [OTHER_CDC, OTHER_ROM], [OTHER_CDC, OTHER_ROM, ROM]])
        extra = run_flash(BASE + ["-d", "COM104"], client)
        self.assertIn("bootloader", client.calls)
        self.assertEqual(extra[extra.index("--device") + 1], "COM103")

    def test_rom_port_is_matched_by_mac_not_by_being_new(self):
        # Another board's ROM port appears first; it is not this board.
        client = FakeClient([[CDC], [OTHER_ROM], [OTHER_ROM, ROM]])
        extra = run_flash(BASE + ["-d", "COM104"], client)
        self.assertEqual(extra[extra.index("--device") + 1], "COM103")

    def test_uart_bridge_board_is_flashed_where_it_is(self):
        client = FakeClient([[UART]])
        extra = run_flash(BASE + ["-d", "COM7"], client)
        self.assertNotIn("bootloader", client.calls)
        self.assertEqual(extra[extra.index("--device") + 1], "COM7")

    def test_board_already_in_rom_is_flashed_where_it_is(self):
        client = FakeClient([[ROM]])
        extra = run_flash(BASE + ["-d", "COM103"], client)
        self.assertNotIn("bootloader", client.calls)
        self.assertEqual(extra[extra.index("--device") + 1], "COM103")

    def test_no_reset_skips_the_bootloader_step(self):
        client = FakeClient([[CDC]])
        extra = run_flash(BASE + ["-d", "COM104", "--before", "no-reset"], client)
        self.assertEqual(client.calls, [])
        self.assertEqual(extra[extra.index("--device") + 1], "COM104")

    def test_no_rom_port_fails_without_flashing_the_dead_port(self):
        client = FakeClient([[CDC], [OTHER_ROM]])
        with mock.patch.object(cli, "_ROM_PORT_WAIT_S", 0.0), \
                mock.patch.object(cli.time, "monotonic", side_effect=[0.0, 1.0, 2.0]):
            with self.assertRaises(SystemExit):
                run_flash(BASE + ["-d", "COM104"], client)

    def test_refused_bootloader_fails(self):
        client = FakeClient([[CDC]], bootloader_ok=False)
        with self.assertRaises(SystemExit):
            run_flash(BASE + ["-d", "COM104"], client)


class ResetModes(unittest.TestCase):
    def test_before_and_after_reach_the_engine(self):
        client = FakeClient([[ROM]])
        extra = run_flash(BASE + ["-d", "COM103", "--before", "no_reset", "--after", "watchdog-reset"], client)
        self.assertEqual(extra[extra.index("--before") + 1], "no-reset")
        self.assertEqual(extra[extra.index("--after") + 1], "watchdog-reset")

    def test_engine_accepts_what_the_cli_passes(self):
        from mpftp import firmware

        ns = firmware.build_parser().parse_args(
            ["flash", "--port", "esp32", "--before", "no-reset", "--after", "watchdog-reset"]
        )
        self.assertEqual((ns.before, ns.after), ("no-reset", "watchdog-reset"))


class ChipMac(unittest.TestCase):
    def test_both_serial_forms_give_the_same_mac(self):
        self.assertEqual(cli._chip_mac(CDC["serial_number"]), cli._chip_mac(ROM["serial_number"]))
        self.assertEqual(cli._chip_mac(None), "")


if __name__ == "__main__":
    unittest.main()
