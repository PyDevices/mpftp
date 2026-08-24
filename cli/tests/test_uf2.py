"""UF2 file validation, volume discovery, and the copy/verify flash path."""

from __future__ import annotations

import struct
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from mpftp import uf2


def _block(addr: int, payload: bytes, blk_no: int, num_blocks: int,
           family: int = 0xE48BFF56, flags: int = uf2.UF2_FLAG_FAMILY_ID,
           start0: int = uf2.UF2_MAGIC_START0, end: int = uf2.UF2_MAGIC_END) -> bytes:
    header = struct.pack(
        "<8I", start0, uf2.UF2_MAGIC_START1, flags, addr,
        len(payload), blk_no, num_blocks, family,
    )
    body = payload + b"\x00" * (476 - len(payload))
    return header + body + struct.pack("<I", end)


def _write_uf2(path: Path, blocks: list[bytes]) -> Path:
    path.write_bytes(b"".join(blocks))
    return path


class ParseUf2Tests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)

    def test_parses_a_well_formed_file(self) -> None:
        blocks = [_block(0x10000 + i * 256, b"\xaa" * 256, i, 3) for i in range(3)]
        meta = uf2.parse_uf2(_write_uf2(self.dir / "fw.uf2", blocks))
        self.assertEqual(meta["blocks"], 3)
        self.assertEqual(meta["family_names"], ["RP2040"])
        self.assertEqual(meta["payload_bytes"], 768)
        self.assertEqual(meta["target_start"], 0x10000)
        self.assertEqual(meta["warnings"], [])

    def test_rejects_a_truncated_file(self) -> None:
        path = self.dir / "cut.uf2"
        path.write_bytes(_block(0, b"\x01" * 256, 0, 1)[:400])
        with self.assertRaises(uf2.Uf2Error) as cm:
            uf2.parse_uf2(path)
        self.assertIn("not a multiple", str(cm.exception))

    def test_rejects_bad_magic(self) -> None:
        path = _write_uf2(self.dir / "bad.uf2", [_block(0, b"\x01", 0, 1, start0=0xDEADBEEF)])
        with self.assertRaises(uf2.Uf2Error):
            uf2.parse_uf2(path)

    def test_rejects_bad_end_magic(self) -> None:
        path = _write_uf2(self.dir / "bad2.uf2", [_block(0, b"\x01", 0, 1, end=0)])
        with self.assertRaises(uf2.Uf2Error):
            uf2.parse_uf2(path)

    def test_rejects_block_count_disagreeing_with_header(self) -> None:
        # A bootloader waits for numBlocks blocks, so this hangs rather than flashes.
        blocks = [_block(0, b"\x01" * 16, i, 9) for i in range(2)]
        with self.assertRaises(uf2.Uf2Error) as cm:
            uf2.parse_uf2(_write_uf2(self.dir / "short.uf2", blocks))
        self.assertIn("declares 9 blocks", str(cm.exception))

    def test_not_main_flash_blocks_are_not_counted(self) -> None:
        blocks = [
            _block(0, b"\x01" * 16, 0, 1, flags=uf2.UF2_FLAG_NOT_MAIN_FLASH),
            _block(0, b"\x02" * 32, 0, 1),
        ]
        meta = uf2.parse_uf2(_write_uf2(self.dir / "meta.uf2", blocks))
        self.assertEqual(meta["blocks"], 1)
        self.assertEqual(meta["payload_bytes"], 32)

    def test_missing_family_id_warns_but_parses(self) -> None:
        blocks = [_block(0, b"\x01" * 16, 0, 1, flags=0)]
        meta = uf2.parse_uf2(_write_uf2(self.dir / "nofam.uf2", blocks))
        self.assertEqual(meta["families"], [])
        self.assertTrue(any("no family ID" in w for w in meta["warnings"]))

    def test_empty_file_is_rejected(self) -> None:
        path = self.dir / "empty.uf2"
        path.write_bytes(b"")
        with self.assertRaises(uf2.Uf2Error):
            uf2.parse_uf2(path)


class FamilyNameTests(unittest.TestCase):
    def test_known_families(self) -> None:
        self.assertEqual(uf2.family_name(0xE48BFF56), "RP2040")
        self.assertEqual(uf2.family_name(0xC47E5767), "ESP32S3")
        self.assertEqual(uf2.family_name(0x68ED2B88), "SAMD21")

    def test_unknown_family_degrades_to_hex(self) -> None:
        self.assertEqual(uf2.family_name(0x12345678), "0x12345678")


class InfoUf2Tests(unittest.TestCase):
    def test_parses_board_id(self) -> None:
        info = uf2.parse_info_uf2(
            "UF2 Bootloader v3.0\r\nModel: Raspberry Pi RP2\r\nBoard-ID: RPI-RP2\r\n"
        )
        self.assertEqual(info["banner"], "UF2 Bootloader v3.0")
        self.assertEqual(info["Board-ID"], "RPI-RP2")
        self.assertEqual(info["Model"], "Raspberry Pi RP2")

    def test_ignores_lines_without_a_colon(self) -> None:
        info = uf2.parse_info_uf2("Banner\njunk\nBoard-ID: X\n")
        self.assertEqual(info["Board-ID"], "X")
        self.assertNotIn("junk", info)

    def test_missing_file_gives_empty_info(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(uf2.read_volume_info(Path(d)), {})


class VolumeDiscoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)

    def _volume(self, name: str, board_id: str = "RPI-RP2") -> Path:
        root = self.dir / name
        root.mkdir()
        (root / "INFO_UF2.TXT").write_text(
            f"UF2 Bootloader v3.0\nBoard-ID: {board_id}\n"
        )
        return root

    def test_finds_volumes_by_info_file(self) -> None:
        root = self._volume("RPI-RP2")
        self.dir.joinpath("NotABootloader").mkdir()
        with mock.patch.object(uf2, "_candidate_roots",
                               return_value=[root, self.dir / "NotABootloader"]):
            found = uf2.find_uf2_volumes("linux")
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["board_id"], "RPI-RP2")

    def test_no_volumes_when_no_info_file(self) -> None:
        with mock.patch.object(uf2, "_candidate_roots", return_value=[self.dir]):
            self.assertEqual(uf2.find_uf2_volumes("linux"), [])

    def test_windows_letters_are_probed_on_wsl(self) -> None:
        # WSL does not auto-mount removable drives, so the Windows path is the
        # only way to reach a bootloader volume there.
        with mock.patch.object(uf2, "_windows_volume_letters", return_value=["D:"]):
            roots = [str(r) for r in uf2._candidate_roots("wsl")]
        self.assertTrue(any(r.startswith("D:") or r == "/mnt/d" for r in roots))

    def test_looks_like_volume(self) -> None:
        self.assertTrue(uf2.looks_like_volume("D:"))
        self.assertTrue(uf2.looks_like_volume("/media/brad/RPI-RP2"))
        self.assertFalse(uf2.looks_like_volume("COM7"))


class WaitForVolumeGoneTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)
        (self.dir / "INFO_UF2.TXT").write_text("UF2 Bootloader\nBoard-ID: X\n")

    def test_returns_true_once_the_volume_unmounts(self) -> None:
        clock = {"t": 0.0}
        info = self.dir / "INFO_UF2.TXT"

        def sleep(dt: float) -> None:
            clock["t"] += dt
            if clock["t"] >= 1.0 and info.exists():
                info.unlink()  # the board rebooted

        self.assertTrue(
            uf2.wait_for_volume_gone(self.dir, timeout=10.0, poll=0.25,
                                     sleep=sleep, now=lambda: clock["t"])
        )

    def test_returns_false_when_the_volume_stays(self) -> None:
        clock = {"t": 0.0}

        def sleep(dt: float) -> None:
            clock["t"] += dt

        self.assertFalse(
            uf2.wait_for_volume_gone(self.dir, timeout=2.0, poll=0.25,
                                     sleep=sleep, now=lambda: clock["t"])
        )

    def test_volume_present_survives_an_oserror(self) -> None:
        with mock.patch.object(Path, "is_file", side_effect=OSError("gone")):
            self.assertFalse(uf2.volume_present(self.dir))


class CopyUf2Tests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)

    def test_copies_and_reports_bytes_written(self) -> None:
        src = self.dir / "fw.uf2"
        src.write_bytes(b"\xa5" * (512 * 5))
        dest = self.dir / "out.uf2"
        self.assertEqual(uf2.copy_uf2(src, dest), 512 * 5)
        self.assertEqual(dest.read_bytes(), src.read_bytes())

    def test_fsync_failure_is_not_an_error(self) -> None:
        # The board reboots the moment it has the last block, so fsync on a
        # vanished volume is expected rather than a failure.
        src = self.dir / "fw.uf2"
        src.write_bytes(b"\x01" * 512)
        dest = self.dir / "out.uf2"
        with mock.patch.object(uf2.os, "fsync", side_effect=OSError("device gone")):
            self.assertEqual(uf2.copy_uf2(src, dest), 512)


if __name__ == "__main__":
    unittest.main()
