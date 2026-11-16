# SPDX-FileCopyrightText: 2026 Brad Barnett
#
# SPDX-License-Identifier: MIT

"""Wi-Fi as a first-class connection: boot.py block, power save, busy boards,
remembered boards and their passwords, mDNS. No board required."""

from __future__ import annotations

import contextlib
import io
import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from mpftp import boards, config, mdns, sidecar, webrepl, wifiboard

ORIGINAL_BOOT = (
    "# This file is executed on every boot (including wake-boot from deepsleep)\n"
    "#import esp\n#esp.osdebug(None)\n#import webrepl\n#webrepl.start()\n\n\n"
)


class BootBlockTests(unittest.TestCase):
    def test_disable_gives_back_the_original_bytes(self):
        enabled = wifiboard.add_block(ORIGINAL_BOOT)
        self.assertTrue(enabled.endswith(ORIGINAL_BOOT))
        self.assertEqual(wifiboard.remove_block(enabled), ORIGINAL_BOOT)

    def test_the_block_needs_only_what_stock_micropython_has(self):
        block = wifiboard.render_block()
        self.assertIn("import network, time, webrepl", block)
        self.assertIn("from secrets import WIFI_SSID, WIFI_PASSWORD", block)
        self.assertIn("webrepl.start()", block)
        self.assertNotIn("password=", block)
        # The PyDevices helper is an optional first try, never an import that can fail the block.
        self.assertIn("wifi_manager.connect(setup=False)", block)
        self.assertEqual(block.count("import wifi"), 1)
        compile(block, "boot.py", "exec")

    def test_a_block_that_cannot_join_says_so_and_finishes(self):
        """Run the block on this interpreter with stand-ins for the board's modules."""
        import types

        class Wlan:
            def __init__(self, up=False):
                self.up = up

            def active(self, on):
                pass

            def isconnected(self):
                return self.up

            def connect(self, ssid, password):
                self.joined = (ssid, password)

        net = types.ModuleType("network")
        net.STA_IF = 0
        wlan = Wlan()
        net.WLAN = lambda _i: wlan
        started = []
        web = types.ModuleType("webrepl")
        web.start = lambda *a, **k: started.append((a, k))
        secrets = types.ModuleType("secrets")
        secrets.WIFI_SSID, secrets.WIFI_PASSWORD = "net", "pw"
        clock = {"t": 0}
        limit = {"up_at": 0}
        tm = types.ModuleType("time")
        tm.ticks_ms = lambda: clock["t"]
        tm.ticks_diff = lambda a, b: a - b

        def sleep_ms(ms):
            clock["t"] += ms
            if clock["t"] >= limit["up_at"]:
                wlan.up = True

        tm.sleep_ms = sleep_ms
        mods = {"network": net, "webrepl": web, "secrets": secrets, "time": tm}
        block = wifiboard.render_block()
        for up_at, joined in ((500, True), (10**9, False)):
            limit["up_at"] = up_at
            started.clear()
            wlan.up = False
            clock["t"] = 0
            out = io.StringIO()
            with mock.patch.dict(sys.modules, mods), contextlib.redirect_stdout(out):
                exec(compile(block, "boot.py", "exec"), {})
            self.assertEqual(wlan.joined, ("net", "pw"))
            self.assertEqual(started, [((), {})] if joined else [])
            self.assertEqual("no Wi-Fi after 20 s" in out.getvalue(), not joined)

    def test_crlf_and_no_final_newline_survive_the_round_trip(self):
        original = "import gc\r\ngc.collect()"
        self.assertEqual(wifiboard.remove_block(wifiboard.add_block(original)), original)

    def test_a_boot_py_mpftp_created_is_deleted_on_disable(self):
        created = wifiboard.add_block(None)
        self.assertIsNone(wifiboard.remove_block(created))

    def test_an_empty_boot_py_stays_an_empty_file(self):
        self.assertEqual(wifiboard.remove_block(wifiboard.add_block("")), "")

    def test_user_lines_added_after_enable_are_kept(self):
        enabled = wifiboard.add_block(ORIGINAL_BOOT) + "print('mine')\n"
        self.assertEqual(wifiboard.remove_block(enabled), ORIGINAL_BOOT + "print('mine')\n")

    def test_enable_twice_is_refused(self):
        with self.assertRaises(ValueError):
            wifiboard.add_block(wifiboard.add_block(ORIGINAL_BOOT))

    def test_disable_without_a_block_is_refused(self):
        with self.assertRaises(ValueError):
            wifiboard.remove_block(ORIGINAL_BOOT)

    def test_password_rules(self):
        for bad in ("", "abc", "abcdefghij", "pass\nword", "pässwort"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                wifiboard.check_new_password(bad)
        self.assertEqual(wifiboard.check_new_password("123456789"), "123456789")

    def test_the_shown_diff_never_holds_the_password(self):
        cfg = wifiboard.webrepl_cfg_text("p4wifi")
        self.assertIn('PASS = "p4wifi"', cfg)
        diff = wifiboard.unified_diff(None, wifiboard.mask_secrets(cfg, "p4wifi"), "/webrepl_cfg.py")
        self.assertNotIn("p4wifi", diff)
        self.assertIn('+PASS = "(new)"', diff)
        self.assertTrue(diff.startswith("--- /dev/null\n+++ /webrepl_cfg.py\n"))

    def test_an_old_block_with_its_password_inline_is_masked_too(self):
        old = "try:\n    webrepl.start(password='sekrit')\nexcept Exception:\n    pass\n"
        self.assertNotIn("sekrit", wifiboard.mask_secrets(old))

    def test_the_cfg_file_is_quoted_like_upstreams(self):
        self.assertEqual(wifiboard.webrepl_cfg_text('a"b\\c').splitlines()[1], 'PASS = "a\\"b\\\\c"')
        self.assertTrue(wifiboard.cfg_is_ours(wifiboard.webrepl_cfg_text("abcd")))
        self.assertFalse(wifiboard.cfg_is_ours('PASS = "abcd"\n'))


class SettingsTomlTests(unittest.TestCase):
    def test_keys_the_file_defines_itself_are_left_alone(self):
        text = 'CIRCUITPY_WIFI_SSID = "mine"\nOTHER = 1\n'
        self.assertEqual(
            wifiboard.settings_block_keys(text), ["CIRCUITPY_WIFI_PASSWORD", "CIRCUITPY_WEB_API_PASSWORD"]
        )
        self.assertEqual(len(wifiboard.settings_block_keys(None)), 3)

    def test_the_block_round_trips_at_the_top(self):
        original = 'SOMETHING = "x"\n[table]\nk = 1'
        enabled = wifiboard.render_settings_block(list(wifiboard.SETTINGS_KEYS)) + original
        self.assertEqual(wifiboard.remove_block(enabled), original)
        self.assertEqual(wifiboard.settings_keys_outside_block(enabled), {"SOMETHING"} & set(wifiboard.SETTINGS_KEYS))

    def run_on_board(self, keys, webpw, files, secrets_attrs):
        """Run the code the board runs, on this interpreter, over a dict of files."""
        import types

        class File:
            def __init__(self, name, mode):
                self.name, self.mode, self.buf = name, mode, ""
                if "w" not in mode and name not in files:
                    raise OSError(2)

            def read(self):
                return files[self.name]

            def write(self, text):
                self.buf += text

            def close(self):
                if "w" in self.mode:
                    files[self.name] = self.buf

        mod = types.ModuleType("secrets")
        for k, v in secrets_attrs.items():
            setattr(mod, k, v)
        out = io.StringIO()
        env = {"open": lambda n, m="r": File(n, m), "getattr": getattr}
        with mock.patch.dict(sys.modules, {"secrets": mod}), contextlib.redirect_stdout(out):
            exec(wifiboard.cp_enable_code(keys, webpw), env)
        return out.getvalue()

    def test_the_board_builds_the_block_from_its_own_secrets_and_the_preview_matches(self):
        files = {"/settings.toml": 'KEEP = "me"\n'}
        keys = list(wifiboard.SETTINGS_KEYS)
        out = self.run_on_board(keys, 'pw"1', files, {"WIFI_SSID": "home net", "WIFI_PASSWORD": "p\\w"})
        self.assertIsNone(wifiboard.parse_cp_enable(out))
        text = files["/settings.toml"]
        self.assertIn('CIRCUITPY_WIFI_SSID = "home net"', text)
        self.assertIn('CIRCUITPY_WIFI_PASSWORD = "p\\\\w"', text)
        self.assertIn('CIRCUITPY_WEB_API_PASSWORD = "pw\\"1"', text)
        try:
            import tomllib
        except ImportError:  # Python before 3.11
            tomllib = None
        if tomllib:
            self.assertEqual(tomllib.loads(text)["CIRCUITPY_WIFI_PASSWORD"], "p\\w")
        self.assertEqual(
            wifiboard.mask_secrets(text),
            wifiboard.render_settings_block(keys) + 'KEEP = "me"\n',
        )
        self.assertEqual(wifiboard.remove_block(text), 'KEEP = "me"\n')
        self.assertNotIn("home net", out)

    def test_the_old_adafruit_secrets_dict_works_too(self):
        files = {}
        out = self.run_on_board(
            list(wifiboard.SETTINGS_KEYS), "abcd", files,
            {"secrets": {"ssid": "net", "password": "pw"}},
        )
        self.assertIsNone(wifiboard.parse_cp_enable(out))
        self.assertTrue(files["/settings.toml"].startswith("# >>> mcuctl wifi-access >>> (mcuctl created this file)\n"))
        self.assertIsNone(wifiboard.remove_block(files["/settings.toml"]))

    def test_secrets_without_a_network_name_writes_nothing(self):
        files = {"/settings.toml": "X = 1\n"}
        out = self.run_on_board(list(wifiboard.SETTINGS_KEYS), "abcd", files, {"WIFI_PASSWORD": "pw"})
        self.assertIn("no Wi-Fi name", wifiboard.parse_cp_enable(out))
        self.assertEqual(files["/settings.toml"], "X = 1\n")


class ParseTests(unittest.TestCase):
    def test_identity_takes_the_last_dict_line_and_drops_a_zero_address(self):
        out = b"noise\r\n{'uid': 'e8f60ae0f070', 'ip': '0.0.0.0', 'hostname': 'mpy'}\r\n"
        self.assertEqual(wifiboard.parse_identity(out), {"uid": "e8f60ae0f070", "hostname": "mpy"})
        self.assertIsNone(wifiboard.parse_identity(b"Traceback ...\r\n"))

    def test_power_save_tuple(self):
        self.assertEqual(wifiboard.parse_pm(b"(1, 0, False)\r\n"), (1, 0, False))
        self.assertIsNone(wifiboard.parse_pm(b"oops"))

    def test_start_now_keeps_only_the_address(self):
        out = b"Connecting to: HomeNet\r\nwifi: ntp ok\r\nMPFTP-IP 192.168.1.147\r\n"
        self.assertEqual(wifiboard.parse_start_now(out), "192.168.1.147")
        self.assertIsNone(wifiboard.parse_start_now(b"MPFTP-IP -\r\n"))


class MdnsTests(unittest.TestCase):
    #: The P4's real answer to build_query("mpy-esp32p4.local", 0x1235).
    REPLY = bytes.fromhex(
        "1235840000010001000000000b6d70792d65737033327034056c6f63616c0000010001"
        "c00c00010001000000780004c0a80193"
    )

    def test_normalize(self):
        self.assertEqual(mdns.normalize("Board"), "board.local")
        self.assertEqual(mdns.normalize("board.local."), "board.local")

    def test_query_packet(self):
        q = mdns.build_query("mpy-esp32p4.local", 0x1235)
        self.assertEqual(q[:12].hex(), "123500000001000000000000")
        self.assertTrue(self.REPLY[12:12 + len(q) - 12] == q[12:])

    def test_parses_a_real_board_reply_with_a_compressed_name(self):
        self.assertEqual(mdns.parse_a_records(self.REPLY, "mpy-esp32p4.local"), ["192.168.1.147"])
        self.assertEqual(mdns.parse_a_records(self.REPLY, "other.local"), [])

    def test_a_query_is_not_an_answer(self):
        self.assertEqual(mdns.parse_a_records(mdns.build_query("x.local", 1), "x.local"), [])

    def test_resolve_prefers_the_system_resolver(self):
        with mock.patch.object(
            mdns.socket, "getaddrinfo", return_value=[(2, 1, 6, "", ("10.0.0.9", 0))]
        ), mock.patch.object(mdns, "query") as q:
            self.assertEqual(mdns.resolve("b")["via"], "system")
        q.assert_not_called()

    def test_resolve_falls_back_to_our_own_query(self):
        with mock.patch.object(
            mdns.socket, "getaddrinfo", side_effect=OSError("unknown")
        ), mock.patch.object(mdns, "query", return_value="10.0.0.7"):
            self.assertEqual(mdns.resolve("b"), {"name": "b.local", "ip": "10.0.0.7", "via": "mdns"})


class StoreCase(unittest.TestCase):
    """A private ~/.mpftp for each test."""

    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        home = Path(self.td.name)
        self.patches = [
            mock.patch.object(config, "CONFIG_DIR", home),
            mock.patch.object(config, "CONFIG_PATH", home / "config.json"),
            mock.patch.dict(os.environ, {"MPFTP_WEBREPL_PASSWORD": ""}),
        ]
        for p in self.patches:
            p.start()
        self.home = home

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        self.td.cleanup()


IDENTITY = {"uid": "e8f60ae0f070", "hostname": "mpy-esp32p4", "ip": "192.168.1.147"}


class BoardsTests(StoreCase):
    def test_a_serial_connect_with_wifi_up_is_remembered(self):
        boards.record_connect("COM4", {"board": IDENTITY})
        rows = boards.listing()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["name"], "mpy-esp32p4")
        self.assertEqual(rows[0]["device"], "ws://192.168.1.147")
        self.assertFalse(rows[0]["hasPassword"])
        stored = json.loads((self.home / "config.json").read_text())
        self.assertIn("e8f60ae0f070", stored["wifiBoards"])

    def test_wifi_down_is_not_remembered(self):
        boards.record_connect("COM4", {"board": {"uid": "abc", "hostname": "x"}})
        self.assertEqual(boards.listing(), [])

    def test_lookup_by_ip_or_hostname(self):
        boards.remember(IDENTITY)
        self.assertEqual(boards.uid_for_device("ws://192.168.1.147"), "e8f60ae0f070")
        self.assertEqual(boards.uid_for_device("ws://mpy-esp32p4.local:8266"), "e8f60ae0f070")
        self.assertIsNone(boards.uid_for_device("ws://10.9.9.9"))

    def test_passwords_are_per_board_in_a_0600_file(self):
        boards.remember(IDENTITY)
        boards.set_password("ws://192.168.1.147", "p4wifi")
        boards.remember({"uid": "0badc0de", "ip": "192.168.1.150"})
        boards.set_password("0badc0de", "other1")
        self.assertEqual(boards.get_password("ws://192.168.1.147"), "p4wifi")
        self.assertEqual(boards.get_password("ws://192.168.1.150"), "other1")
        path = self.home / boards.PASSWORDS_NAME
        if os.name == "posix":
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertNotIn("p4wifi", (self.home / "config.json").read_text())

    def test_a_typed_address_password_moves_to_the_uid_after_connect(self):
        boards.set_password("ws://10.0.0.5", "typed1")
        self.assertIn("host:10.0.0.5:8266", boards.passwords())
        boards.record_connect(
            "ws://10.0.0.5", {"board": {"uid": "cafe01", "hostname": "b"}}, "typed1"
        )
        self.assertEqual(boards.passwords(), {"cafe01": "typed1"})
        self.assertEqual(boards.get_password("ws://10.0.0.5"), "typed1")

    def test_falls_back_to_the_phase_one_single_password(self):
        with mock.patch.dict(os.environ, {"MPFTP_WEBREPL_PASSWORD": "global1"}):
            self.assertEqual(boards.get_password("ws://10.1.1.1"), "global1")

    def test_cli_connect_params_carry_the_boards_password_and_known(self):
        from mpftp import cli

        boards.remember(IDENTITY)
        boards.set_password("e8f60ae0f070", "p4wifi")
        params = cli.connect_params("ws://192.168.1.147", 115200)
        self.assertEqual(params["password"], "p4wifi")
        self.assertTrue(params["known"])
        self.assertEqual(cli.connect_params("COM4", 115200), {"device": "COM4", "baud": 115200})


class FakeBoard:
    """Just enough of an mpremote transport for the Wi-Fi paths."""

    def __init__(self, *, pm=1, ble=False, boot=ORIGINAL_BOOT, prereqs=None):
        self.in_raw_repl = True
        self.pm = pm
        self.ble = ble
        self.files: dict[str, bytes] = {} if boot is None else {"/boot.py": boot.encode()}
        self.prereqs = prereqs or {"webrepl": True, "secrets": True, "names": True}
        self.cp_secrets = {"WIFI_SSID": "home net", "WIFI_PASSWORD": "joinpass"}
        self.cp_writable = True
        self.cp_readonly = False
        self.pm_log: list[int] = []
        self.fail_on: str | None = None

    def exec(self, code):
        if self.fail_on and self.fail_on in code:
            raise OSError("connection reset")
        if code == wifiboard.PM_READ_CODE:
            return repr((self.pm, 0, self.ble)).encode()
        if "config(pm=" in code:
            self.pm = int(code.split("pm=")[1].split(")")[0])
            self.pm_log.append(self.pm)
            return b""
        if code == wifiboard.PREREQ_CODE:
            return repr(self.prereqs).encode()
        if code == wifiboard.CP_READONLY_CODE:
            return repr(self.cp_readonly).encode()
        if code == wifiboard.CP_PREREQ_CODE:
            return repr({"secrets": bool(self.cp_secrets)}).encode()
        if code.startswith("def _mpftp_cp("):
            return self._run_cp_enable(code)
        return b""

    def _run_cp_enable(self, code):
        """Run the code the CircuitPython board would, over this fake's files."""
        import types

        board = self

        class File:
            def __init__(self, name, mode):
                if "w" in mode and not board.cp_writable:
                    raise OSError(30)
                if "w" not in mode and name not in board.files:
                    raise OSError(2)
                self.name, self.mode, self.buf = name, mode, ""

            def read(self):
                return board.files[self.name].decode()

            def write(self, text):
                self.buf += text

            def close(self):
                if "w" in self.mode:
                    board.files[self.name] = self.buf.encode()

        mod = types.ModuleType("secrets")
        for k, v in self.cp_secrets.items():
            setattr(mod, k, v)
        out = io.StringIO()
        with mock.patch.dict(sys.modules, {"secrets": mod}), contextlib.redirect_stdout(out):
            exec(code, {"open": lambda n, m="r": File(n, m)})
        return out.getvalue().encode()

    def fs_exists(self, path):
        return path in self.files

    def fs_readfile(self, path):
        return self.files[path]

    def fs_writefile(self, path, data):
        self.files[path] = bytes(data)

    def fs_rmfile(self, path):
        del self.files[path]


def network_session(board: FakeBoard) -> sidecar.Session:
    s = sidecar.Session()
    s.transport = board
    s.device = "ws://10.0.0.5"
    s.interpreter = "micropython"
    return s


class PowerSaveTests(unittest.TestCase):
    def test_off_during_the_transfer_then_exactly_the_value_read(self):
        board = FakeBoard(pm=2)
        s = network_session(board)
        seen = []
        res = s.with_raw(s._transfer(lambda t: (seen.append(t.pm), {"ok": True})[1]))
        self.assertEqual(seen, [0])
        self.assertEqual(board.pm, 2)
        self.assertEqual(res["power_save"], {"changed": True, "was": 2, "during": 0, "restored": 2})

    def test_restored_when_the_transfer_fails(self):
        board = FakeBoard(pm=1)
        s = network_session(board)

        def boom(t):
            raise RuntimeError("ENOSPC")

        with self.assertRaisesRegex(RuntimeError, "ENOSPC"):
            s.with_raw(s._transfer(boom))
        self.assertEqual(board.pm, 1)

    def test_a_dropped_connection_says_it_stays_off_until_reset(self):
        board = FakeBoard(pm=1)
        s = network_session(board)

        def drop(t):
            board.fail_on = "config(pm=1)"
            raise RuntimeError("upload failed")

        with self.assertRaisesRegex(RuntimeError, "stays off until the board resets"):
            s.with_raw(s._transfer(drop))
        # The next transfer (after a reconnect) puts back 1, not the 0 it reads.
        board.fail_on = None
        res = s.with_raw(s._transfer(lambda t: {"ok": True}))
        self.assertEqual(board.pm, 1)
        self.assertEqual(res["power_save"]["restored"], 1)

    def test_left_alone_while_bluetooth_is_active(self):
        board = FakeBoard(pm=1, ble=True)
        s = network_session(board)
        res = s.with_raw(s._transfer(lambda t: {"ok": True}))
        self.assertEqual(board.pm_log, [])
        self.assertEqual(res["power_save"]["reason"], "Bluetooth is active")

    def test_serial_never_touches_it(self):
        board = FakeBoard(pm=1)
        s = network_session(board)
        s.device = "COM4"
        res = s.with_raw(s._transfer(lambda t: {"ok": True}))
        self.assertEqual(board.pm_log, [])
        self.assertNotIn("power_save", res)

    def test_a_batch_changes_it_once(self):
        board = FakeBoard(pm=1)
        s = network_session(board)
        s.transfer_begin()
        for _ in range(3):
            s.with_raw(s._transfer(lambda t: {"ok": True}))
        self.assertEqual(board.pm, 0)
        res = s.transfer_end()
        self.assertEqual(board.pm_log, [0, 1])
        self.assertEqual(res["power_save"]["restored"], 1)


class WifiAccessTests(unittest.TestCase):
    def serial_session(self, board, interpreter="micropython"):
        s = network_session(board)
        s.device = "COM4"
        s.interpreter = interpreter
        return s

    def test_plan_then_apply_then_disable_restores_boot_py(self):
        board = FakeBoard()
        s = self.serial_session(board)
        plan = s.wifi_access_plan("enable", "p4wifi")
        self.assertEqual(plan["problems"], [])
        self.assertNotIn("p4wifi", json.dumps(plan))
        self.assertEqual(plan["paths"], ["/webrepl_cfg.py", "/boot.py"])
        self.assertIn('+PASS = "(new)"', plan["diff"])
        s.wifi_access_apply("enable", "p4wifi", plan["sha256"])
        self.assertNotIn(b"p4wifi", board.files["/boot.py"])
        self.assertTrue(board.files["/boot.py"].endswith(ORIGINAL_BOOT.encode()))
        self.assertIn(b'PASS = "p4wifi"', board.files["/webrepl_cfg.py"])
        plan = s.wifi_access_plan("disable")
        self.assertNotIn("p4wifi", plan["diff"])
        s.wifi_access_apply("disable", None, plan["sha256"])
        self.assertEqual(board.files, {"/boot.py": ORIGINAL_BOOT.encode()})

    def test_a_webrepl_cfg_that_was_already_there_is_shown_kept_and_put_back(self):
        board = FakeBoard()
        theirs = b'PASS = "theirs1"\n'
        board.files["/webrepl_cfg.py"] = theirs
        s = self.serial_session(board)
        plan = s.wifi_access_plan("enable", "p4wifi")
        self.assertIn('-PASS = "*****"', plan["diff"])
        self.assertIn('+PASS = "(new)"', plan["diff"])
        self.assertNotIn("theirs1", json.dumps(plan))
        self.assertIn("/webrepl_cfg.py.mpftp-orig", plan["paths"])
        s.wifi_access_apply("enable", "p4wifi", plan["sha256"])
        self.assertEqual(board.files["/webrepl_cfg.py.mpftp-orig"], theirs)
        plan = s.wifi_access_plan("disable")
        s.wifi_access_apply("disable", None, plan["sha256"])
        self.assertEqual(board.files["/webrepl_cfg.py"], theirs)
        self.assertNotIn("/webrepl_cfg.py.mpftp-orig", board.files)
        self.assertEqual(board.files["/boot.py"], ORIGINAL_BOOT.encode())

    def test_a_cfg_that_mpftp_did_not_write_survives_disable_of_an_old_block(self):
        board = FakeBoard(boot=wifiboard.render_block() + ORIGINAL_BOOT)
        board.files["/webrepl_cfg.py"] = b'PASS = "theirs1"\n'
        s = self.serial_session(board)
        plan = s.wifi_access_plan("disable")
        self.assertEqual(plan["paths"], ["/boot.py"])
        s.wifi_access_apply("disable", None, plan["sha256"])
        self.assertEqual(board.files["/webrepl_cfg.py"], b'PASS = "theirs1"\n')

    def test_nothing_is_written_if_a_file_changed_since_the_plan(self):
        board = FakeBoard()
        s = self.serial_session(board)
        plan = s.wifi_access_plan("enable", "p4wifi")
        board.files["/boot.py"] += b"x = 1\n"
        with self.assertRaisesRegex(RuntimeError, "changed since you reviewed"):
            s.wifi_access_apply("enable", "p4wifi", plan["sha256"])
        self.assertNotIn(b"mpftp", board.files["/boot.py"])
        self.assertNotIn("/webrepl_cfg.py", board.files)

    def test_a_missing_secrets_file_is_explained_and_no_pydevices_helper_is_asked_for(self):
        board = FakeBoard(prereqs={"webrepl": True, "secrets": False})
        plan = self.serial_session(board).wifi_access_plan("enable", "p4wifi")
        self.assertEqual(len(plan["problems"]), 1)
        self.assertIn("secrets.py", plan["problems"][0])
        self.assertNotIn("pydevices", plan["problems"][0].lower())
        self.assertEqual(plan["diff"], "")
        board = FakeBoard(prereqs={"webrepl": False, "secrets": True, "names": False})
        plan = self.serial_session(board).wifi_access_plan("enable", "p4wifi")
        self.assertEqual(len(plan["problems"]), 2)

    def test_enable_needs_serial(self):
        s = network_session(FakeBoard())
        with self.assertRaisesRegex(RuntimeError, "serial"):
            s.wifi_access_plan("enable", "p4wifi")

    def test_a_boot_py_mpftp_created_is_removed_again(self):
        board = FakeBoard(boot=None)
        s = self.serial_session(board)
        plan = s.wifi_access_plan("enable", "abcd")
        self.assertFalse(plan["exists"])
        s.wifi_access_apply("enable", "abcd", plan["sha256"])
        plan = s.wifi_access_plan("disable")
        self.assertTrue(plan["delete"])
        s.wifi_access_apply("disable", None, plan["sha256"])
        self.assertEqual(board.files, {})

    # CircuitPython

    def test_circuitpython_enable_writes_settings_toml_from_the_boards_own_secrets(self):
        board = FakeBoard(boot=None)
        board.files["/settings.toml"] = b'KEEP = "me"\n'
        s = self.serial_session(board, "circuitpython")
        plan = s.wifi_access_plan("enable", "web1")
        self.assertEqual(plan["problems"], [])
        self.assertEqual(plan["paths"], ["/settings.toml"])
        shown = json.dumps(plan)
        for secret in ("home net", "joinpass", "web1"):
            self.assertNotIn(secret, shown)
        self.assertIn('+CIRCUITPY_WIFI_SSID = "*****"', plan["diff"])
        self.assertNotIn("KEEP", plan["diff"])
        done = s.wifi_access_apply("enable", "web1", plan["sha256"])
        self.assertTrue(done["reset_needed"])
        text = board.files["/settings.toml"].decode()
        self.assertIn('CIRCUITPY_WIFI_SSID = "home net"', text)
        self.assertIn('CIRCUITPY_WEB_API_PASSWORD = "web1"', text)
        self.assertNotIn("joinpass", json.dumps(done))
        plan = s.wifi_access_plan("disable")
        self.assertNotIn("joinpass", plan["diff"])
        s.wifi_access_apply("disable", None, plan["sha256"])
        self.assertEqual(board.files, {"/settings.toml": b'KEEP = "me"\n'})

    def test_circuitpython_keeps_wifi_settings_the_file_already_has(self):
        board = FakeBoard(boot=None)
        board.cp_secrets = {}
        board.files["/settings.toml"] = b'CIRCUITPY_WIFI_SSID = "a"\nCIRCUITPY_WIFI_PASSWORD = "b"\n'
        s = self.serial_session(board, "circuitpython")
        plan = s.wifi_access_plan("enable", "web1")
        self.assertEqual(plan["problems"], [])
        self.assertEqual(plan["diff"].count("+CIRCUITPY"), 1)
        s.wifi_access_apply("enable", "web1", plan["sha256"])
        self.assertEqual(
            board.files["/settings.toml"].decode().count("CIRCUITPY_WIFI_SSID"), 1
        )

    def test_circuitpython_without_any_wifi_credentials_is_explained(self):
        board = FakeBoard(boot=None)
        board.cp_secrets = {}
        plan = self.serial_session(board, "circuitpython").wifi_access_plan("enable", "web1")
        self.assertIn("secrets.py", plan["problems"][0])
        self.assertEqual(plan["diff"], "")

    def test_a_drive_the_computer_has_mounted_is_written_through_the_drive(self):
        with tempfile.TemporaryDirectory() as tmp:
            drive = Path(tmp)
            (drive / "settings.toml").write_text('KEEP = "me"\n')
            board = FakeBoard(boot=None)
            board.cp_readonly = True
            board.cp_secrets = {}
            board.files["/settings.toml"] = b'KEEP = "me"\n'
            s = self.serial_session(board, "circuitpython")
            s._circuitpy_msc_root = lambda: drive
            s._circuitpy_host_path = lambda p: drive / p.lstrip("/")
            plan = s.wifi_access_plan("enable", "web1")
            self.assertTrue(plan["needs_credentials"])
            self.assertEqual(plan["problems"], [])
            with self.assertRaisesRegex(RuntimeError, "Wi-Fi name and password"):
                s.wifi_access_apply("enable", "web1", plan["sha256"])
            creds = {"ssid": "home net", "password": "joinpass"}
            board.files["/settings.toml"] = b'KEEP = "me"\n'
            real_read = s._read_text
            s._read_text = lambda t, path: (drive / path.lstrip("/")).read_text()
            s.wifi_access_apply("enable", "web1", plan["sha256"], credentials=creds)
            text = (drive / "settings.toml").read_text()
            self.assertIn('CIRCUITPY_WIFI_SSID = "home net"', text)
            self.assertIn('CIRCUITPY_WEB_API_PASSWORD = "web1"', text)
            plan = s.wifi_access_plan("disable")
            self.assertNotIn("joinpass", plan["diff"])
            s.wifi_access_apply("disable", None, plan["sha256"])
            self.assertEqual((drive / "settings.toml").read_text(), 'KEEP = "me"\n')
            s._read_text = real_read

    def test_circuitpython_says_so_when_the_drive_is_read_only(self):
        board = FakeBoard(boot=None)
        board.cp_writable = False
        s = self.serial_session(board, "circuitpython")
        plan = s.wifi_access_plan("enable", "web1")
        with self.assertRaisesRegex(RuntimeError, "can not write"):
            s.wifi_access_apply("enable", "web1", plan["sha256"])
        self.assertEqual(board.files, {})


class BusyBoardTests(unittest.TestCase):
    def test_silent_webrepl_on_a_host_that_answers_ping_is_a_busy_board(self):
        s = sidecar.Session()
        err = webrepl.WebReplError("ws://10.0.0.5: no answer from 10.0.0.5:8266 in 5 s. ...")
        with mock.patch.object(webrepl, "host_answers_ping", return_value=True):
            msg = s._network_open_error("ws://10.0.0.5", err)
        self.assertEqual(msg, f"ws://10.0.0.5: {wifiboard.STUCK_MESSAGE}")

    def test_a_host_that_is_gone_keeps_the_plain_message(self):
        s = sidecar.Session()
        err = webrepl.WebReplError("ws://10.0.0.5: no answer from 10.0.0.5:8266 in 5 s.")
        with mock.patch.object(webrepl, "host_answers_ping", return_value=False):
            self.assertEqual(s._network_open_error("ws://10.0.0.5", err), str(err))

    def test_a_board_that_answered_before_and_goes_quiet_after_connecting_is_busy(self):
        s = sidecar.Session()
        s._answered.add("ws://10.0.0.5")
        err = webrepl.WebReplError("ws://10.0.0.5: no the password prompt within 5 s (got b'')")
        with mock.patch.object(webrepl, "host_answers_ping") as ping:
            self.assertIn(wifiboard.STUCK_MESSAGE, s._network_open_error("ws://10.0.0.5", err))
        ping.assert_not_called()

    def test_a_board_that_answered_before_and_is_now_off_is_not_called_busy(self):
        # A saved board that's switched off: the TCP connect times out and
        # ping gets no answer, so the plain "is the board on this network"
        # message stands.
        s = sidecar.Session()
        s._answered.add("ws://10.0.0.5")
        err = webrepl.WebReplError("ws://10.0.0.5: no answer from 10.0.0.5:8266 in 5 s.")
        with mock.patch.object(webrepl, "host_answers_ping", return_value=False):
            self.assertEqual(s._network_open_error("ws://10.0.0.5", err), str(err))

    def test_a_board_that_answered_before_and_still_answers_ping_is_busy(self):
        s = sidecar.Session()
        s._answered.add("ws://10.0.0.5")
        err = webrepl.WebReplError("ws://10.0.0.5: no answer from 10.0.0.5:8266 in 5 s.")
        with mock.patch.object(webrepl, "host_answers_ping", return_value=True):
            self.assertIn(wifiboard.STUCK_MESSAGE, s._network_open_error("ws://10.0.0.5", err))

    def test_a_refused_connection_is_not_called_busy(self):
        s = sidecar.Session()
        s._answered.add("ws://10.0.0.5")
        err = webrepl.WebReplError("ws://10.0.0.5: 10.0.0.5:8266 refused the connection.")
        self.assertEqual(s._network_open_error("ws://10.0.0.5", err), str(err))

    def _silent_board(self, answers=()):
        """A WebREPL link that answers only the bytes in ``answers``."""
        ws = mock.Mock(spec=webrepl.WebSocketSerial)
        ws.rx_total = 0
        ws.port = "ws://10.0.0.5"
        ws.is_open = True
        ws.inWaiting.return_value = 0

        def write(data):
            if any(key in data for key in answers):
                ws.rx_total += 6

        ws.write.side_effect = write
        return ws

    def _session(self, ws, *, raw=False):
        s = sidecar.Session()
        s.transport = mock.Mock(serial=ws, in_raw_repl=raw)
        s.NETWORK_INTERRUPT_WAIT = 0.4
        s.NETWORK_POKE_AFTER = 0.05
        s.NETWORK_POKE_EVERY = 0.1
        return s

    def test_a_board_that_answers_nothing_is_busy(self):
        # Firmware without the WebREPL Ctrl-C fix, in `while True: pass`:
        # the socket is never read, so neither Ctrl-C nor a poke is answered.
        ws = self._silent_board()
        with self.assertRaisesRegex(RuntimeError, "never yields"):
            self._session(ws).interrupt()
        writes = [c.args[0] for c in ws.write.call_args_list]
        self.assertEqual(writes[0], b"\r\x03")
        self.assertGreaterEqual(writes.count(b"\x02"), 2)

    def test_ctrl_c_that_gets_a_prompt_is_fine_and_not_poked(self):
        ws = self._silent_board(answers=(b"\x03",))
        result = self._session(ws).interrupt()
        self.assertTrue(result["ok"])
        self.assertIn("latency_ms", result)
        self.assertNotIn("poked", result)
        ws.write.assert_called_once_with(b"\r\x03")

    def test_a_silent_interrupt_is_found_out_by_the_poke(self):
        # A program that catches KeyboardInterrupt and ends quietly, or raw
        # REPL clearing its line: nothing comes back for Ctrl-C, but Ctrl-B
        # gets the banner, so the board is alive.
        ws = self._silent_board(answers=(b"\x02",))
        result = self._session(ws).interrupt()
        self.assertEqual(result["ok"], True)
        self.assertTrue(result["poked"])

    def test_a_session_holding_raw_repl_pokes_with_ctrl_a(self):
        ws = self._silent_board(answers=(b"\x01",))
        self.assertTrue(self._session(ws, raw=True).interrupt()["poked"])
        self.assertNotIn(b"\x02", [c.args[0] for c in ws.write.call_args_list])

    def _type_ctrl_c(self, ws):
        s = self._session(ws)
        s._repl_mode = True
        with mock.patch.object(s, "_start_repl_reader"), mock.patch.object(
            sidecar, "_notify"
        ) as notify:
            s.repl_write("Aw==")  # base64 of b"\x03"
            import time

            time.sleep(0.8)
        return notify

    def test_ctrl_c_typed_in_the_repl_reports_a_busy_board(self):
        notify = self._type_ctrl_c(self._silent_board())
        notify.assert_called_once_with("repl_error", {"message": wifiboard.STUCK_MESSAGE})

    def test_ctrl_c_typed_in_the_repl_is_fine_when_the_poke_answers(self):
        notify = self._type_ctrl_c(self._silent_board(answers=(b"\x02",)))
        notify.assert_not_called()

    def test_a_login_hung_up_by_a_closing_client_is_retried(self):
        busy = webrepl.WebReplError(
            "ws://10.0.0.5: the board closed the connection before the password prompt."
        )
        s = sidecar.Session()
        s.WEBREPL_BUSY_RETRIES = (0, 0)
        with mock.patch.object(webrepl, "open_transport", side_effect=[busy, "t"]) as op:
            self.assertEqual(s._open_transport("ws://10.0.0.5", 115200), "t")
        self.assertEqual(op.call_count, 2)
        with mock.patch.object(
            webrepl, "open_transport", side_effect=busy
        ) as op, self.assertRaisesRegex(webrepl.WebReplError, "password prompt"):
            s._open_transport("ws://10.0.0.5", 115200)
        self.assertEqual(op.call_count, 3)
        other = webrepl.WebReplError("ws://10.0.0.5: 10.0.0.5:8266 refused the connection.")
        with mock.patch.object(
            webrepl, "open_transport", side_effect=other
        ) as op, self.assertRaises(webrepl.WebReplError):
            s._open_transport("ws://10.0.0.5", 115200)
        self.assertEqual(op.call_count, 1)

    def test_mount_over_wifi_is_refused_with_a_reason(self):
        s = network_session(FakeBoard())
        with self.assertRaisesRegex(RuntimeError, "serial only"):
            s.mount("/tmp")


@unittest.skipIf(sys.platform == "win32", "ping flags differ; covered by the POSIX branch")
class PingTests(unittest.TestCase):
    def test_a_reply_needs_a_ttl(self):
        done = mock.Mock(stdout="Reply from 10.0.0.1: Destination host unreachable.")
        with mock.patch("subprocess.run", return_value=done):
            self.assertFalse(webrepl.host_answers_ping("10.0.0.5"))
        done = mock.Mock(stdout="64 bytes from 10.0.0.5: icmp_seq=1 ttl=255 time=4 ms")
        with mock.patch("subprocess.run", return_value=done):
            self.assertTrue(webrepl.host_answers_ping("10.0.0.5"))
        with mock.patch("subprocess.run", side_effect=OSError("no ping")):
            self.assertIsNone(webrepl.host_answers_ping("10.0.0.5"))


class PwaRelayTests(StoreCase):
    def test_connect_gets_the_stored_password_and_the_board_is_remembered(self):
        from mpftp import pwa

        boards.remember(IDENTITY)
        boards.set_password("e8f60ae0f070", "p4wifi")
        params = {"device": "ws://192.168.1.147"}
        keep = pwa._prepare_connect(params)
        self.assertEqual(params["password"], "p4wifi")
        self.assertTrue(params["known"])
        pwa._after_reply("connect", keep, {"board": dict(IDENTITY, ip="192.168.1.160")})
        self.assertEqual(boards.listing()[0]["ip"], "192.168.1.160")

    def test_a_typed_password_is_saved_only_when_asked(self):
        from mpftp import pwa

        params = {"device": "ws://10.0.0.5", "password": "typed1"}
        keep = pwa._prepare_connect(params)
        pwa._after_reply("connect", keep, {"board": {"uid": "cafe01", "hostname": "b"}})
        self.assertEqual(boards.passwords(), {})
        params = {"device": "ws://10.0.0.5", "password": "typed1", "remember": True}
        keep = pwa._prepare_connect(params)
        self.assertNotIn("remember", params)
        pwa._after_reply("connect", keep, {"board": {"uid": "cafe01", "hostname": "b"}})
        self.assertEqual(boards.passwords(), {"cafe01": "typed1"})

    def test_local_methods_never_return_a_password(self):
        from mpftp import pwa

        boards.remember(IDENTITY)
        pwa._local_method("wifi_password_set", {"board": "e8f60ae0f070", "password": "p4wifi"})
        rows = pwa._local_method("wifi_boards", {})
        self.assertTrue(rows[0]["hasPassword"])
        self.assertNotIn("p4wifi", json.dumps(rows))


if __name__ == "__main__":
    unittest.main()
