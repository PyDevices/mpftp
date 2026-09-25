"""ble:// devices: addresses, passwords, the bledev.repl login and file transfer.

No radio and no bleak: the "board" answers what BleSerial writes, the way
bledev.repl and bledev.filetransfer do on a real one. The protocol side here
is written from Adafruit's file-transfer spec, not from ble.py.
"""

from __future__ import annotations

import os
import struct
import unittest
from unittest import mock

from mpftp import ble, boards, cli, config

PASSWORD = "correct-horse"


class FakeBoard:
    """bledev.repl's login and a minimal file-transfer server, in memory."""

    def __init__(self, link: ble.BleSerial, *, password: str = PASSWORD, window: int = 512) -> None:
        self.link = link
        self.password = password
        self.window = window
        self.files: dict[str, bytes] = {}
        self.cmd = bytearray()
        self.writing: dict | None = None
        self.link._write_char = self.receive  # type: ignore[method-assign]
        self.link._client = object()

    def receive(self, uuid: str, data: bytes, response: bool = False) -> None:
        if uuid == ble.NUS_RX:
            self.repl(data)
        elif uuid == ble.FT_TRANSFER:
            self.cmd += data
            self.serve()

    def repl(self, data: bytes) -> None:
        line = data.rstrip(b"\r")
        if not line:
            self.link._on_repl(None, bytearray(b"Password: "))
        elif line == self.password.encode():
            self.link._on_repl(None, bytearray(b"\r\nbledev REPL connected\r\n>>> "))
        else:
            self.link._on_repl(None, bytearray(b"\r\nAccess denied\r\n"))

    def answer(self, data: bytes) -> None:
        # Notifications of MTU - 3 bytes, as the board sends them.
        for i in range(0, len(data), 244):
            self.link._on_files(None, bytearray(data[i : i + 244]))

    def serve(self) -> None:
        b = self.cmd
        while b:
            if b[0] == 0x10 and len(b) >= 12:
                _, _, n, offset, chunk = struct.unpack_from("<BBHII", b)
                if len(b) < 12 + n:
                    return
                path = bytes(b[12 : 12 + n]).decode()
                del b[: 12 + n]
                if path not in self.files:
                    self.answer(struct.pack("<BBHIII", 0x11, 0x02, 0, 0, 0, 0))
                    continue
                body = self.files[path]
                self.answer(struct.pack("<BBHIII", 0x11, 0x01, 0, offset, len(body), len(body) - offset))
                self.answer(body[offset:])
            elif b[0] == 0x20 and len(b) >= 20:
                _, _, n, offset, _t, total = struct.unpack_from("<BBHIQI", b)
                if len(b) < 20 + n:
                    return
                path = bytes(b[20 : 20 + n]).decode()
                del b[: 20 + n]
                self.writing = {"path": path, "total": total, "data": bytearray()}
                if not total:
                    self.files[path] = b""
                self.answer(struct.pack("<BBHIQI", 0x21, 0x01, 0, 0, 1, min(total, self.window)))
            elif b[0] == 0x22 and len(b) >= 12:
                _, _, _, offset, size = struct.unpack_from("<BBHII", b)
                if len(b) < 12 + size:
                    return
                w = self.writing
                assert w is not None and offset == len(w["data"])
                w["data"] += b[12 : 12 + size]
                del b[: 12 + size]
                at = len(w["data"])
                if at == w["total"]:
                    self.files[w["path"]] = bytes(w["data"])
                self.answer(struct.pack("<BBHIQI", 0x21, 0x01, 0, at, 1, min(w["total"] - at, self.window)))
            else:
                return


def link(password: str | None = PASSWORD) -> ble.BleSerial:
    serial = ble.BleSerial("ble://rack", password)
    serial.mtu = 247
    serial.has_files = True
    return serial


class Addresses(unittest.TestCase):
    def test_scheme(self) -> None:
        self.assertTrue(ble.is_ble_device("ble://rack"))
        self.assertTrue(ble.is_ble_device(" BLE://rack"))
        self.assertFalse(ble.is_ble_device("ws://rack"))
        self.assertFalse(ble.is_ble_device("COM5"))
        self.assertEqual(ble.parse_device("ble://bledev-files"), "bledev-files")
        self.assertEqual(ble.parse_device("ble://AA:BB:CC:DD:EE:FF/"), "AA:BB:CC:DD:EE:FF")

    def test_bad_addresses(self) -> None:
        with self.assertRaises(ValueError):
            ble.parse_device("ble://")
        with self.assertRaisesRegex(ValueError, "password"):
            ble.parse_device("ble://pw@rack")

    def test_password_rules(self) -> None:
        # No password is fine: a board that pairing unlocks never asks.
        self.assertIsNone(ble.check_password(None, "ble://rack"))
        self.assertIsNone(ble.check_password("", "ble://rack"))
        with self.assertRaises(ble.BleAuthError):
            ble.check_password("abc", "ble://rack")
        with self.assertRaises(ble.BleAuthError):
            ble.check_password("x" * 65, "ble://rack")
        self.assertEqual(ble.check_password("a-long-password-past-webrepls-nine", "ble://rack")[:6], "a-long")


class Passwords(unittest.TestCase):
    def setUp(self) -> None:
        import tempfile
        from pathlib import Path

        self.home = Path(tempfile.mkdtemp())
        patches = [
            mock.patch.object(config, "CONFIG_DIR", self.home),
            mock.patch.object(config, "CONFIG_PATH", self.home / "config.json"),
            mock.patch.object(boards, "_passwords_path", lambda: self.home / "webrepl-passwords.json"),
            mock.patch.dict(os.environ, {"MPFTP_BLE_PASSWORD": "", "MPFTP_WEBREPL_PASSWORD": ""}),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def test_key(self) -> None:
        self.assertEqual(boards.password_key("ble://Rack"), "ble:rack")

    def test_falls_back_to_the_webrepl_password(self) -> None:
        # bledev.repl defaults to webrepl_cfg.PASS.
        self.assertIsNone(boards.get_password("ble://rack"))
        with mock.patch.dict(os.environ, {"MPFTP_WEBREPL_PASSWORD": "webpw1"}):
            self.assertEqual(boards.get_password("ble://rack"), "webpw1")
            with mock.patch.dict(os.environ, {"MPFTP_BLE_PASSWORD": "blepw12"}):
                self.assertEqual(boards.get_password("ble://rack"), "blepw12")
                self.assertEqual(cli.connect_params("ble://rack", 115200)["password"], "blepw12")

    def test_saved_password_wins(self) -> None:
        key = boards.set_password("ble://rack", "a-long-ble-password")
        self.assertEqual(key, "ble:rack")
        with mock.patch.dict(os.environ, {"MPFTP_BLE_PASSWORD": "blepw12"}):
            self.assertEqual(boards.get_password("ble://rack"), "a-long-ble-password")
        with self.assertRaises(ble.BleAuthError):
            boards.set_password("ble://rack", "abc")


class Login(unittest.TestCase):
    def test_right_password(self) -> None:
        serial = link()
        FakeBoard(serial)
        serial._login()
        self.assertTrue(serial.is_open)

    def test_wrong_password(self) -> None:
        serial = link("not-the-one")
        FakeBoard(serial)
        with self.assertRaisesRegex(ble.BleAuthError, "rejected"):
            serial._login()


class PairedBoard(FakeBoard):
    """bledev.repl started with pairing: RX refuses an unpaired host (ATT
    0x05); with ``password=None`` the first line opens the REPL."""

    def __init__(self, link: ble.BleSerial, *, password=None, justworks_enough: bool = True) -> None:
        FakeBoard.__init__(self, link, password=password or "unused")
        self.board_password = password
        self.paired = False
        self.pairings = 0
        self.justworks_enough = justworks_enough
        link._pair = self.pair  # type: ignore[method-assign]
        link._unpair = self.unpair  # type: ignore[method-assign]

    def unpair(self) -> None:
        self.paired = False

    def pair(self) -> None:
        self.pairings += 1
        self.paired = True

    def receive(self, uuid: str, data: bytes, response: bool = False) -> None:
        if not (self.paired and self.justworks_enough):
            try:
                raise RuntimeError("Protocol Error 0x05: Insufficient Authentication")
            except RuntimeError as e:
                raise ble.BleError("ble://rack: BLE write failed") from e
        if uuid == ble.NUS_RX and self.board_password is None:
            self.link._on_repl(None, bytearray(b"\r\nbledev REPL connected\r\n>>> "))
            return
        FakeBoard.receive(self, uuid, data, response)


class Pairing(unittest.TestCase):
    def test_pairs_just_works_when_refused_then_logs_in_without_a_password(self) -> None:
        serial = link(password=None)
        board = PairedBoard(serial)
        serial._login()
        self.assertEqual(board.pairings, 1)
        self.assertTrue(serial.is_open)

    def test_already_paired_needs_no_pairing(self) -> None:
        serial = link(password=None)
        board = PairedBoard(serial)
        board.paired = True
        serial._login()
        self.assertEqual(board.pairings, 0)

    def test_pairing_plus_password(self) -> None:
        serial = link()
        board = PairedBoard(serial, password=PASSWORD)
        serial._login()
        self.assertEqual(board.pairings, 1)

    def test_a_board_that_asks_for_a_password_needs_one(self) -> None:
        serial = link(password=None)
        PairedBoard(serial, password=PASSWORD)
        with self.assertRaisesRegex(ble.BleAuthError, "asks for a password"):
            serial._login()

    def test_a_passkey_board_says_how_to_pair(self) -> None:
        serial = link(password=None)
        board = PairedBoard(serial, justworks_enough=False)
        with self.assertRaisesRegex(ble.BleAuthError, "bledev.bleak pair"):
            serial._login()
        self.assertFalse(board.paired, "the useless just-works pairing was left behind")

    def test_other_write_failures_are_not_pairing(self) -> None:
        serial = link(password=None)
        board = PairedBoard(serial)

        def broken(uuid: str, data: bytes, response: bool = False) -> None:
            raise ble.BleError("ble://rack: BLE write failed: the radio is off")

        serial._write_char = broken  # type: ignore[method-assign]
        with self.assertRaisesRegex(ble.BleError, "radio is off"):
            serial._login()
        self.assertEqual(board.pairings, 0)


class Files(unittest.TestCase):
    def test_round_trip_in_windows(self) -> None:
        serial = link()
        board = FakeBoard(serial, window=512)
        data = os.urandom(20 * 1024 + 7)
        serial.files_write("/big.bin", data)
        self.assertEqual(board.files["/big.bin"], data)
        self.assertEqual(serial.files_read("/big.bin"), data)

    def test_empty_file(self) -> None:
        serial = link()
        board = FakeBoard(serial)
        serial.files_write("/empty", b"")
        self.assertEqual(board.files["/empty"], b"")
        self.assertEqual(serial.files_read("/empty"), b"")

    def test_missing_file_is_oserror(self) -> None:
        serial = link()
        FakeBoard(serial)
        with self.assertRaises(OSError) as caught:
            serial.files_read("/nope")
        self.assertNotIsInstance(caught.exception, ble.BleError)  # a board answer, not a dead link

    def test_planted_faults_change_the_bytes(self) -> None:
        # What mpftp's SHA-256 check after put/get has to catch.
        data = os.urandom(4096)
        with mock.patch.dict(os.environ, {ble.PLANT_ENV: "put"}):
            serial = link()
            board = FakeBoard(serial)
            serial.files_write("/p.bin", data)
            self.assertNotEqual(board.files["/p.bin"], data)
            self.assertEqual(len(board.files["/p.bin"]), len(data))
        with mock.patch.dict(os.environ, {ble.PLANT_ENV: "get"}):
            serial = link()
            board = FakeBoard(serial)
            board.files["/g.bin"] = data
            self.assertNotEqual(serial.files_read("/g.bin"), data)


class Wslenv(unittest.TestCase):
    def test_plain_values_forwarded_without_a_flag(self) -> None:
        env = {"WSL_DISTRO_NAME": "Ubuntu", "WSLENV": "", ble.PLANT_ENV: "put"}
        with mock.patch.dict(os.environ, env, clear=False):
            os.environ.pop("MICROPYPATH", None)
            os.environ.pop("PYTHONPATH", None)
            os.environ.pop(ble.FILES_ENV, None)
            out = cli._wslenv_forwarded_env("python.exe")
        self.assertIsNotNone(out)
        self.assertIn(ble.PLANT_ENV, out["WSLENV"].split(":"))
        self.assertNotIn(ble.PLANT_ENV + "/", out["WSLENV"])


if __name__ == "__main__":
    unittest.main()


class Scan(unittest.TestCase):
    """ble.scan: what the Connect lists (VS Code, the PWA) offer (mpftp#49)."""

    def run_scan(self, found: dict) -> list:
        import sys
        import types

        async def discover(timeout: float, return_adv: bool):  # noqa: ARG001
            self.assertTrue(return_adv)
            return found

        fake = types.ModuleType("bleak")
        fake.BleakScanner = types.SimpleNamespace(discover=discover)  # type: ignore[attr-defined]
        with mock.patch.dict(sys.modules, {"bleak": fake}):
            return ble.scan(0.1)

    @staticmethod
    def adv(name: str, rssi: int, *uuids: str):
        import types

        dev = types.SimpleNamespace(name=None)
        return dev, types.SimpleNamespace(local_name=name, rssi=rssi, service_uuids=list(uuids))

    def test_lists_repl_boards_strongest_first(self) -> None:
        rows = self.run_scan(
            {
                "AA:01": self.adv("far", -80, ble.NUS_SERVICE),
                "AA:02": self.adv("near", -40, ble.NUS_SERVICE.upper(), ble.FT_SERVICE),
                "AA:03": self.adv("headphones", -30, "0000110b-0000-1000-8000-00805f9b34fb"),
                "AA:04": self.adv("", -60, ble.NUS_SERVICE),
            }
        )
        self.assertEqual([r["device"] for r in rows], ["ble://near", "ble://AA:04", "ble://far"])
        self.assertEqual([r["files"] for r in rows], [True, False, False])

    def test_nothing_found(self) -> None:
        self.assertEqual(self.run_scan({}), [])


class PwaConnect(Passwords):
    """The PWA server fills a ble:// connect from the store and keeps a typed
    password when asked to (mpftp#49)."""

    def test_saved_password_goes_in(self) -> None:
        from mpftp import pwa

        boards.set_password("ble://rack", "a-long-ble-password")
        params = {"device": "ble://rack"}
        keep = pwa._prepare_connect(params)
        self.assertEqual(params["password"], "a-long-ble-password")
        self.assertNotIn("known", params)
        self.assertFalse(keep.get("typed"))

    def test_typed_password_is_kept_when_remembered(self) -> None:
        from mpftp import pwa

        for remember, expect in ((False, None), (True, "typed-pw-1")):
            params = {"device": "ble://Rack", "password": "typed-pw-1", "remember": remember}
            keep = pwa._prepare_connect(params)
            pwa._after_reply("connect", keep, {"board": {"uid": "abc"}})
            self.assertEqual(boards.passwords().get("ble:rack"), expect)
