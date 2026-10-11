"""The browser's Firmware button, server side (mpftp.webflash).

The board is a mocked sidecar and esptool is tests/fixtures/fake-esptool or a
mocked engine: nothing here opens a port.
"""

from __future__ import annotations

import base64
import os
import struct
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from mpftp import firmware, webflash

FAKE_ESPTOOL = Path(__file__).parent / "fixtures" / "fake-esptool"
USER_ACTIVITY_LOG = Path.home() / ".mpftp" / "activity.log"  # before any test moves HOME


def combined(chip_id: int) -> bytes:
    """A combined firmware.bin's start: image header naming the chip, then a
    bootloader's first segment (not an app descriptor)."""
    header = bytearray([0xE9, 1, 2, 0x20]) + bytearray(20)
    header[12:14] = chip_id.to_bytes(2, "little")
    return bytes(header) + struct.pack("<II", 0x40280020, 0x100) + struct.pack("<I", 0x50) + bytes(4096)


APP_S3 = bytearray(combined(0x09))
APP_S3[32:36] = struct.pack("<I", 0xABCD5432)  # esp_app_desc_t: micropython.bin


class FakeSidecar:
    """list_ports answers in turn from `ports_seq` (the last one repeats)."""

    def __init__(self, ports_seq, platform="'esp32'"):
        self.ports_seq = list(ports_seq)
        self.platform = platform
        self.calls: list[str] = []

    def __call__(self, method, params=None, timeout=None):
        self.calls.append(method)
        if method == "list_ports":
            return self.ports_seq.pop(0) if len(self.ports_seq) > 1 else self.ports_seq[0]
        if method == "eval":
            return {"value": self.platform}
        return {"ok": True}


_HARNESSES: list = []


def tearDownModule():
    for h in _HARNESSES:
        h.close()


class Harness:
    def __init__(self, sidecar=None, connected=""):
        _HARNESSES.append(self)
        self.sent: list[dict] = []
        self.sidecar = sidecar or FakeSidecar([[]])
        self.flasher = webflash.WebFlasher(
            request=self.sidecar, send=lambda ws, msg: self.sent.append(msg) or True, connected=lambda: connected
        )
        self.flasher.sleep = lambda s: None
        self.flasher.PORT_WAIT_S = 0.5
        self.ws = object()

    def close(self):
        self.flasher.forget_tab(self.ws)

    def upload(self, data: bytes, name="firmware.bin", chunk=1500):
        for offset in range(0, len(data), chunk):
            self.flasher.upload(
                self.ws,
                {
                    "name": name,
                    "size": len(data),
                    "offset": offset,
                    "data_b64": base64.b64encode(data[offset : offset + chunk]).decode(),
                },
            )

    def notes(self, method):
        return [m["params"] for m in self.sent if m.get("method") == method]


def engine_ok(lines=()):
    def run(argv, on_line):
        for line in lines:
            on_line(line)
        return {"type": "result", "ok": True}

    return run


class CheckImageTests(unittest.TestCase):
    def check(self, data: bytes, name="firmware.bin"):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / name
            p.write_bytes(data)
            return webflash.check_image(p, name)

    def test_the_offset_follows_the_chip_in_the_image(self):
        for chip_id, chip, offset in (
            (0x00, "esp32", "0x1000"),
            (0x02, "esp32s2", "0x1000"),
            (0x09, "esp32s3", "0x0"),
            (0x05, "esp32c3", "0x0"),
            (0x0D, "esp32c6", "0x0"),
            (0x12, "esp32p4", "0x2000"),
            (0x17, "esp32c5", "0x2000"),
        ):
            with self.subTest(chip=chip):
                self.assertEqual(self.check(combined(chip_id)), (chip, offset))

    def test_a_uf2_is_refused_by_name_and_by_content(self):
        for data, name in ((combined(0x09), "firmware.uf2"), (b"UF2\nWQ]\x9e" + bytes(504), "firmware.bin")):
            with self.subTest(name=name), self.assertRaises(webflash.Refused) as cm:
                self.check(data, name)
            self.assertIn("is a UF2 file", str(cm.exception))

    def test_a_file_that_is_not_an_esp_image_is_refused(self):
        with self.assertRaises(webflash.Refused) as cm:
            self.check(b"PK\x03\x04" + bytes(100), "firmware.zip")
        self.assertIn("isn't an ESP firmware image", str(cm.exception))

    def test_an_app_image_at_the_bootloader_offset_is_refused(self):
        with self.assertRaises(webflash.Refused) as cm:
            self.check(bytes(APP_S3), "micropython.bin")
        self.assertIn("application image", str(cm.exception))


class UploadTests(unittest.TestCase):
    def test_chunks_land_in_one_temp_file(self):
        h = Harness()
        data = combined(0x09) + os.urandom(5000)
        h.upload(data)
        up = h.flasher._uploads[h.ws]
        self.assertTrue(up.complete)
        self.assertEqual(up.path.read_bytes(), data)
        h.flasher.forget_tab(h.ws)
        self.assertFalse(up.path.exists())

    def test_a_chunk_out_of_order_is_refused(self):
        h = Harness()
        h.flasher.upload(h.ws, {"name": "a.bin", "size": 10, "offset": 0, "data_b64": base64.b64encode(b"12345").decode()})
        with self.assertRaises(ValueError):
            h.flasher.upload(h.ws, {"name": "a.bin", "size": 10, "offset": 7, "data_b64": base64.b64encode(b"678").decode()})
        h.flasher.forget_tab(h.ws)


class FlashTests(unittest.TestCase):
    def test_no_board_connected_flashes_the_typed_port_and_streams_progress(self):
        h = Harness()
        h.upload(combined(0x09))
        lines = [
            "Connecting....",
            "Writing at 0x00000000 [>                             ]   0.0% 0/4096 bytes...",
            "Writing at 0x00001000 [==============================] 100.0% 4096/4096 bytes...",
            "Hard resetting via RTS pin...",
        ]
        with mock.patch.object(webflash, "run_engine", side_effect=engine_ok(lines)) as run:
            res = h.flasher.flash(h.ws, {"device": "COM9"})
        self.assertTrue(res["ok"], res)
        self.assertFalse(res["reconnect"])
        argv = run.call_args[0][0]
        self.assertEqual(argv[argv.index("--device") + 1], "COM9")
        self.assertEqual(argv[argv.index("--family") + 1], "esp32s3")
        self.assertNotIn("--erase", argv)
        percents = [n["percent"] for n in h.notes("firmware_progress") if n["percent"] is not None]
        self.assertIn(100.0, percents)
        logged = [n["line"] for n in h.notes("firmware_log")]
        self.assertIn("Hard resetting via RTS pin...", logged)
        self.assertFalse(any(line.startswith("Writing at") for line in logged), "progress lines go to the bar, not the log")
        self.assertNotIn("disconnect", h.sidecar.calls)

    def test_a_uart_board_is_released_then_flashed_on_its_own_port(self):
        sidecar = FakeSidecar([[{"device": "COM4", "vid": 0x10C4, "pid": 0xEA60}]])
        h = Harness(sidecar, connected="COM4")
        h.upload(combined(0x00))
        with mock.patch.object(webflash, "run_engine", side_effect=engine_ok()) as run:
            res = h.flasher.flash(h.ws, {"erase": True})
        self.assertTrue(res["ok"], res)
        self.assertTrue(res["reconnect"])
        self.assertEqual((res["device"], res["port"], res["offset"]), ("COM4", "COM4", "0x1000"))
        self.assertIn("disconnect", sidecar.calls)
        self.assertNotIn("bootloader", sidecar.calls)
        self.assertIn("--erase", run.call_args[0][0])

    def test_a_tinyusb_cdc_board_is_flashed_on_the_rom_port_that_appears(self):
        app = {"device": "COM5", "vid": 0x303A, "pid": 0x4001}
        rom = {"device": "COM6", "vid": 0x303A, "pid": 0x1001}
        sidecar = FakeSidecar([[app], [], [rom]])
        h = Harness(sidecar, connected="COM5")
        h.upload(combined(0x09))
        with mock.patch.object(webflash, "run_engine", side_effect=engine_ok()) as run:
            res = h.flasher.flash(h.ws, {})
        self.assertTrue(res["ok"], res)
        self.assertEqual((res["device"], res["port"]), ("COM5", "COM6"))
        self.assertLess(sidecar.calls.index("bootloader"), sidecar.calls.index("disconnect"))
        argv = run.call_args[0][0]
        self.assertEqual(argv[argv.index("--device") + 1], "COM6")

    def test_a_usb_serial_jtag_board_keeps_its_port(self):
        jtag = {"device": "COM7", "vid": 0x303A, "pid": 0x1001}
        sidecar = FakeSidecar([[jtag]])
        h = Harness(sidecar, connected="COM7")
        h.upload(combined(0x0D))
        with mock.patch.object(webflash, "run_engine", side_effect=engine_ok()):
            res = h.flasher.flash(h.ws, {})
        self.assertEqual(res["port"], "COM7")
        self.assertNotIn("bootloader", sidecar.calls)

    def test_no_download_port_stops_before_esptool(self):
        app = {"device": "COM5", "vid": 0x303A, "pid": 0x4001}
        sidecar = FakeSidecar([[app], [app]])
        h = Harness(sidecar, connected="COM5")
        h.upload(combined(0x09))
        with mock.patch.object(webflash, "run_engine") as run:
            res = h.flasher.flash(h.ws, {})
        self.assertFalse(res["ok"])
        self.assertIn("Hold BOOT and tap RESET", res["error"])
        run.assert_not_called()

    def test_a_board_that_is_not_an_esp_is_refused_before_it_is_let_go(self):
        sidecar = FakeSidecar([[{"device": "COM3", "vid": 0x2E8A, "pid": 0x0005}]], platform="'rp2'")
        h = Harness(sidecar, connected="COM3")
        h.upload(combined(0x09))
        with mock.patch.object(webflash, "run_engine") as run:
            res = h.flasher.flash(h.ws, {})
        self.assertFalse(res["ok"])
        self.assertIn("not an ESP chip", res["error"])
        self.assertIn("choose its .uf2", res["error"])
        self.assertNotIn("disconnect", sidecar.calls)
        run.assert_not_called()

    def test_a_network_connection_is_refused(self):
        h = Harness(connected="ws://192.168.1.40:8266")
        h.upload(combined(0x09))
        res = h.flasher.flash(h.ws, {})
        self.assertFalse(res["ok"])
        self.assertIn("USB serial port", res["error"])

    def test_a_uart_write_that_drops_is_retried_once_at_115200(self):
        h = Harness()
        h.upload(combined(0x09))
        outcomes = [
            ("A fatal error occurred: Packet content transfer stopped (received 8 bytes)", False),
            ("Hash of data verified.", True),
        ]

        def run(argv, on_line):
            line, ok = outcomes.pop(0)
            on_line(line)
            return {"ok": ok, "error": "" if ok else "esptool failed (exit 2)"}

        with mock.patch.object(webflash, "run_engine", side_effect=run) as engine:
            res = h.flasher.flash(h.ws, {"device": "COM9"})
        self.assertTrue(res["ok"], res)
        bauds = [c[0][0][c[0][0].index("--baud") + 1] for c in engine.call_args_list]
        self.assertEqual(bauds, ["460800", "115200"])

    def test_a_wrong_chip_is_not_retried_and_says_why(self):
        h = Harness()
        h.upload(combined(0x09))

        def run(argv, on_line):
            on_line("A fatal error occurred: This chip is ESP32, not ESP32-S3. Wrong chip argument?")
            return {"ok": False, "error": "esptool failed (exit 2)"}

        with mock.patch.object(webflash, "run_engine", side_effect=run) as engine:
            res = h.flasher.flash(h.ws, {"device": "COM9"})
        self.assertEqual(engine.call_count, 1)
        self.assertIn("This chip is ESP32, not ESP32-S3", res["error"])

    def test_a_busy_port_gets_a_plain_reason(self):
        h = Harness()
        h.upload(combined(0x09))

        def run(argv, on_line):
            on_line("A fatal error occurred: Could not open COM9, the port is busy or doesn't exist.")
            on_line("(could not open port 'COM9': PermissionError(13, 'Access is denied.', None, 5))")
            return {"ok": False, "error": "esptool failed (exit 2)"}

        with mock.patch.object(webflash, "run_engine", side_effect=run) as engine:
            res = h.flasher.flash(h.ws, {"device": "COM9"})
        self.assertEqual(engine.call_count, 1)
        self.assertIn("another program has it", res["error"])

    def test_a_changed_partition_table_asks_for_erase(self):
        h = Harness()
        h.upload(combined(0x09))
        with mock.patch.object(
            webflash, "run_engine", return_value={"ok": False, "error": "Partition table differs", "needEraseConfirm": {}}
        ):
            res = h.flasher.flash(h.ws, {"device": "COM9"})
        self.assertFalse(res["ok"])
        self.assertTrue(res["needErase"])


def uf2_image(nblocks=2, family=0xE48BFF56) -> bytes:
    """A small valid UF2 (RP2040 family by default)."""
    from mpftp import uf2

    out = []
    for i in range(nblocks):
        header = struct.pack(
            "<8I", uf2.UF2_MAGIC_START0, uf2.UF2_MAGIC_START1, uf2.UF2_FLAG_FAMILY_ID,
            0x10000000 + i * 256, 256, i, nblocks, family,
        )
        out.append(header + b"\xaa" * 256 + b"\x00" * 220 + struct.pack("<I", uf2.UF2_MAGIC_END))
    return b"".join(out)


class FakeCatalog:
    """Stands in for mpftp.firmware_download: one board, two files."""

    FLASHERS = {"rp2": "uf2", "esp32": "esptool"}

    def __init__(self, cache: Path):
        self.cache = cache
        self.entry = {
            "source": "micropython.org", "board": "RPI_PICO", "title": "Raspberry Pi Pico",
            "port": "rp2", "family": "rp2040", "info_url": "https://micropython.org/download/RPI_PICO/",
            "variants": {},
        }
        self.files = [
            {"name": "RPI_PICO-20260824-v1.29.0.uf2", "version": "1.29.0", "date": "20260824",
             "url": "https://micropython.org/resources/firmware/RPI_PICO-20260824-v1.29.0.uf2",
             "kind": "uf2", "variant": "", "channel": "release", "offset": ""},
            {"name": "RPI_PICO-20260824-v1.29.0.elf", "version": "1.29.0", "date": "20260824",
             "url": "https://micropython.org/resources/firmware/RPI_PICO-20260824-v1.29.0.elf",
             "kind": "elf", "variant": "", "channel": "release", "offset": ""},
        ]
        self.downloaded: list[str] = []

    def catalog_tree(self, force=False):
        return {"ports": [{"port": "rp2", "flasher": "uf2", "boards": [{"board": "RPI_PICO"}]}]}

    def find_board(self, board, force=False):
        return self.entry if board.upper() == "RPI_PICO" else None

    def board_files(self, entry, force=False):
        return list(self.files)

    def flashable_files(self, entry, files):
        return [f for f in files if f["kind"] == "uf2"]

    def preferred_artifact_kind(self, entry):
        return "uf2"

    def download_file(self, url, progress=None):
        self.downloaded.append(url)
        path = self.cache / url.rsplit("/", 1)[-1]
        path.write_bytes(uf2_image())
        if progress:
            progress(len(uf2_image()), len(uf2_image()))
        return path


class CatalogMethodTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.cache = Path(tmp.name)
        self.h = Harness()
        self.cat = FakeCatalog(self.cache)
        self.h.flasher.catalog_module = self.cat

    def test_a_board_lists_only_files_it_can_flash(self):
        info = self.h.flasher.board({"board": "rpi_pico"})
        self.assertEqual([f["kind"] for f in info["files"]], ["uf2"])
        self.assertEqual(info["flasher"], "uf2")
        self.assertEqual(info["preferred"], "uf2")

    def test_an_unknown_board_is_refused(self):
        with self.assertRaisesRegex(webflash.Refused, "isn't in the firmware catalog"):
            self.h.flasher.board({"board": "NOPE"})

    def test_fetch_downloads_only_a_url_the_catalog_lists(self):
        with self.assertRaisesRegex(webflash.Refused, "isn't one of RPI_PICO's firmware files"):
            self.h.flasher.fetch(self.h.ws, {"board": "RPI_PICO", "url": "https://example.com/evil.uf2"})
        self.assertEqual(self.cat.downloaded, [])

    def test_a_fetched_file_becomes_the_tabs_image_and_is_never_deleted(self):
        url = self.cat.files[0]["url"]
        res = self.h.flasher.fetch(self.h.ws, {"board": "RPI_PICO", "url": url})
        self.assertEqual(res["kind"], "uf2")
        self.assertEqual(self.cat.downloaded, [url])
        self.assertTrue(self.h.notes("firmware_progress"))
        path = Path(res["path"])
        self.h.flasher.forget_tab(self.h.ws)
        self.assertTrue(path.is_file(), "the cached download must survive the tab")

    def test_handle_answers_catalog_calls_on_a_thread(self):
        done = threading.Event()
        self.h.flasher.send = lambda ws, msg: (self.h.sent.append(msg), done.set())
        self.h.flasher.handle(self.h.ws, 7, "firmware_catalog", {})
        self.assertTrue(done.wait(5))
        self.assertEqual(self.h.sent[-1]["id"], 7)
        self.assertEqual(self.h.sent[-1]["result"]["ports"][0]["port"], "rp2")


class Uf2FlashTests(unittest.TestCase):
    def test_a_uf2_goes_to_the_one_mounted_drive(self):
        h = Harness()
        h.upload(uf2_image(), name="RPI_PICO.uf2")
        with mock.patch.object(h.flasher, "find_volumes", return_value=[{"path": "/media/u/RPI-RP2"}]), \
                mock.patch.object(webflash, "run_engine", side_effect=engine_ok(["[mpftp] wrote 1024 bytes"])) as run:
            res = h.flasher.flash(h.ws, {"port": "rp2"})
        self.assertTrue(res["ok"], res)
        self.assertFalse(res["reconnect"])
        argv = run.call_args[0][0]
        self.assertIn("--uf2", argv)
        self.assertEqual(argv[argv.index("--device") + 1], "/media/u/RPI-RP2")
        self.assertEqual(argv[argv.index("--port") + 1], "rp2")
        self.assertNotIn("bootloader", h.sidecar.calls)

    def test_a_uf2_is_recognised_by_content_too(self):
        h = Harness()
        h.upload(uf2_image(), name="firmware.bin")
        with mock.patch.object(h.flasher, "find_volumes", return_value=[{"path": "E:\\"}]), \
                mock.patch.object(webflash, "run_engine", side_effect=engine_ok()) as run:
            res = h.flasher.flash(h.ws, {"volume": "E:\\"})
        self.assertTrue(res["ok"], res)
        self.assertIn("--uf2", run.call_args[0][0])

    def test_no_drive_and_a_connected_board_restarts_it_into_its_bootloader(self):
        sidecar = FakeSidecar([[{"device": "/dev/ttyACM0", "vid": 0x2E8A, "pid": 0x0005}]], platform="'rp2'")
        h = Harness(sidecar, connected="/dev/ttyACM0")
        h.upload(uf2_image(), name="RPI_PICO.uf2")
        drives = [[], [], [{"path": "/media/u/RPI-RP2"}]]
        with mock.patch.object(h.flasher, "find_volumes", side_effect=lambda: drives.pop(0) if len(drives) > 1 else drives[0]), \
                mock.patch.object(webflash, "run_engine", side_effect=engine_ok()) as run:
            res = h.flasher.flash(h.ws, {})
        self.assertTrue(res["ok"], res)
        self.assertTrue(res["reconnect"])
        self.assertEqual(res["device"], "/dev/ttyACM0")
        self.assertLess(sidecar.calls.index("bootloader"), sidecar.calls.index("disconnect"))
        argv = run.call_args[0][0]
        self.assertEqual(argv[argv.index("--device") + 1], "/media/u/RPI-RP2")

    def test_no_drive_and_no_board_says_how_to_start_the_bootloader(self):
        h = Harness()
        h.upload(uf2_image(), name="RPI_PICO.uf2")
        with mock.patch.object(h.flasher, "find_volumes", return_value=[]), \
                mock.patch.object(webflash, "run_engine") as run:
            res = h.flasher.flash(h.ws, {})
        self.assertFalse(res["ok"])
        self.assertIn("BOOTSEL", res["error"])
        run.assert_not_called()

    def test_two_drives_are_never_guessed_between(self):
        h = Harness()
        h.upload(uf2_image(), name="RPI_PICO.uf2")
        two = [{"path": "/media/u/RPI-RP2", "board_id": "RPI-RP2"}, {"path": "/media/u/FTHRS3BOOT", "board_id": "S3"}]
        with mock.patch.object(h.flasher, "find_volumes", return_value=two), \
                mock.patch.object(webflash, "run_engine") as run:
            res = h.flasher.flash(h.ws, {})
        self.assertFalse(res["ok"])
        self.assertIn("several UF2 drives", res["error"])
        run.assert_not_called()

    def test_a_broken_uf2_is_refused_before_anything_happens(self):
        h = Harness(connected="/dev/ttyACM0")
        h.upload(b"UF2\n" + bytes(100), name="bad.uf2")
        with mock.patch.object(webflash, "run_engine") as run:
            res = h.flasher.flash(h.ws, {})
        self.assertFalse(res["ok"])
        self.assertIn("isn't a usable UF2 file", res["error"])
        self.assertNotIn("bootloader", h.sidecar.calls)
        run.assert_not_called()


class EngineWithFakeEsptoolTests(unittest.TestCase):
    """The real engine (python -m mpftp.firmware flash) driving fake-esptool."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.log = Path(self.tmp.name) / "esptool.log"
        # HOME too: the engine records each flash in ~/.mpftp/activity.log, and
        # a made-up "esp32 -> COM9" in the real log reads as a real flash of
        # whatever board is on COM9.
        self.home = Path(self.tmp.name) / "home"
        env = {
            "HOME": str(self.home),
            "MPFTP_ESPTOOL": str(FAKE_ESPTOOL),
            "FAKE_ESPTOOL_LOG": str(self.log),
            "FAKE_ESPTOOL_DELAY": "0",
            "PYTHONPATH": str(Path(firmware.__file__).parents[1]),
        }
        patcher = mock.patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)

    def flash(self, data: bytes, **params):
        h = Harness()
        h.upload(data)
        res = h.flasher.flash(h.ws, {"device": "COM9", **params})
        return h, res, self.log.read_text().splitlines()

    def test_a_p4_image_is_written_once_at_0x2000_with_its_chip_named(self):
        h, res, calls = self.flash(combined(0x12), erase=True)
        self.assertTrue(res["ok"], res)
        writes = [c for c in calls if "write-flash" in c]
        self.assertEqual(len(writes), 1, calls)
        self.assertIn("--chip esp32p4", writes[0])
        self.assertIn("--erase-all 0x2000", writes[0])
        self.assertFalse(any("erase-flash" in c for c in calls), calls)
        self.assertIn(100.0, [n["percent"] for n in h.notes("firmware_progress")])

    def test_the_flash_is_recorded_in_the_tests_home_not_the_users(self):
        real_log = USER_ACTIVITY_LOG
        before = real_log.stat().st_size if real_log.exists() else None
        _h, res, _calls = self.flash(combined(0x12))
        self.assertTrue(res["ok"], res)
        records = (self.home / ".mpftp" / "activity.log").read_text().splitlines()
        self.assertTrue(any('"firmware_flash"' in r and "COM9" in r for r in records), records)
        after = real_log.stat().st_size if real_log.exists() else None
        self.assertEqual(before, after, f"the test wrote to {real_log}")

    def test_esptool_refuses_a_board_that_is_another_chip(self):
        with mock.patch.dict(os.environ, {"FAKE_ESPTOOL_CHIP": "esp32"}):
            h, res, calls = self.flash(combined(0x09))
        self.assertFalse(res["ok"])
        self.assertIn("This chip is ESP32, not ESP32S3", res["error"])
        self.assertTrue(all("--chip esp32s3" in c for c in calls), calls)


if __name__ == "__main__":
    unittest.main()
