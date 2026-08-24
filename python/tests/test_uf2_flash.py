"""The flash_uf2 path in the firmware engine.

These cover the failure modes that motivated the feature: a copy that reports
success while writing nothing, and a bootloader that ignores an image whose
family it does not own. Both look identical to a naive implementation, so each
one has to produce a distinguishable, non-ok result.
"""

from __future__ import annotations

import argparse
import struct
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import firmware_engine as fe
import uf2


def _uf2_bytes(nblocks: int = 2, family: int = 0xE48BFF56) -> bytes:
    out = []
    for i in range(nblocks):
        header = struct.pack(
            "<8I", uf2.UF2_MAGIC_START0, uf2.UF2_MAGIC_START1,
            uf2.UF2_FLAG_FAMILY_ID, 0x10000 + i * 256, 256, i, nblocks, family,
        )
        out.append(header + b"\xaa" * 256 + b"\x00" * 220
                   + struct.pack("<I", uf2.UF2_MAGIC_END))
    return b"".join(out)


class FlashUf2Tests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)

        self.artifact = self.dir / "firmware.uf2"
        self.artifact.write_bytes(_uf2_bytes())

        self.volume = self.dir / "RPI-RP2"
        self.volume.mkdir()
        (self.volume / "INFO_UF2.TXT").write_text(
            "UF2 Bootloader v3.0\nBoard-ID: RPI-RP2\n"
        )

        self.results: list[dict] = []
        self.logs: list[str] = []
        patches = [
            mock.patch.object(fe, "emit_result",
                              side_effect=lambda ok, **kw: self.results.append(dict(ok=ok, **kw))),
            mock.patch.object(fe, "emit_log", side_effect=self.logs.append),
            mock.patch.object(fe, "emit_phase", lambda *a, **k: None),
            mock.patch.object(fe, "log_activity", lambda *a, **k: None),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    def _ns(self, **kw) -> argparse.Namespace:
        base = dict(device=str(self.volume), port="rp2", uf2_timeout=1.0)
        base.update(kw)
        return argparse.Namespace(**base)

    @property
    def result(self) -> dict:
        self.assertEqual(len(self.results), 1, f"expected one result, got {self.results}")
        return self.results[0]

    def test_success_when_the_volume_unmounts(self) -> None:
        with mock.patch.object(uf2, "wait_for_volume_gone", return_value=True):
            fe.flash_uf2(self._ns(), self.artifact)
        self.assertTrue(self.result["ok"])
        self.assertEqual(self.result["method"], "uf2")
        self.assertEqual(self.result["board_id"], "RPI-RP2")
        self.assertEqual(self.result["family"], ["RP2040"])
        self.assertEqual(self.result["bytes_written"], self.artifact.stat().st_size)
        self.assertEqual((self.volume / "firmware.uf2").read_bytes(),
                         self.artifact.read_bytes())

    def test_volume_still_mounted_is_a_failure_naming_the_family(self) -> None:
        # The copy succeeded; the bootloader ignored the image. Reporting ok
        # here is the exact bug this path exists to prevent.
        with mock.patch.object(uf2, "wait_for_volume_gone", return_value=False):
            fe.flash_uf2(self._ns(), self.artifact)
        self.assertFalse(self.result["ok"])
        self.assertIn("still mounted", self.result["error"])
        self.assertIn("RP2040", self.result["error"])

    def test_short_write_is_reported_rather_than_claimed_as_success(self) -> None:
        with mock.patch.object(uf2, "copy_uf2", return_value=16):
            fe.flash_uf2(self._ns(), self.artifact)
        self.assertFalse(self.result["ok"])
        self.assertIn("Short write", self.result["error"])

    def test_invalid_uf2_is_rejected_before_any_copy(self) -> None:
        bad = self.dir / "bad.uf2"
        bad.write_bytes(b"not a uf2 at all")
        fe.flash_uf2(self._ns(), bad)
        self.assertFalse(self.result["ok"])
        self.assertFalse((self.volume / "bad.uf2").exists())

    def test_copy_error_after_an_early_reboot_is_a_success(self) -> None:
        with mock.patch.object(uf2, "copy_uf2", side_effect=OSError("device gone")), \
             mock.patch.object(uf2, "wait_for_volume_gone", return_value=True):
            fe.flash_uf2(self._ns(), self.artifact)
        self.assertTrue(self.result["ok"])

    def test_copy_error_with_the_volume_still_there_is_a_failure(self) -> None:
        with mock.patch.object(uf2, "copy_uf2", side_effect=OSError("no space")), \
             mock.patch.object(uf2, "wait_for_volume_gone", return_value=False):
            fe.flash_uf2(self._ns(), self.artifact)
        self.assertFalse(self.result["ok"])
        self.assertIn("no space", self.result["error"])

    def test_no_volume_explains_how_to_get_one(self) -> None:
        with mock.patch.object(uf2, "find_uf2_volumes", return_value=[]), \
             mock.patch.object(fe.shutil, "which", return_value=None):
            fe.flash_uf2(self._ns(device=""), self.artifact)
        self.assertFalse(self.result["ok"])
        self.assertIn("bootloader mode", self.result["error"])

    def test_multiple_volumes_refuses_to_guess(self) -> None:
        volumes = [
            {"path": "/media/a", "board_id": "RPI-RP2", "info": {}},
            {"path": "/media/b", "board_id": "FTHRS3BOOT", "info": {}},
        ]
        with mock.patch.object(uf2, "find_uf2_volumes", return_value=volumes):
            fe.flash_uf2(self._ns(device=""), self.artifact)
        self.assertFalse(self.result["ok"])
        self.assertIn("Multiple UF2 bootloader volumes", self.result["error"])
        self.assertIn("RPI-RP2", self.result["error"])
        self.assertFalse((self.volume / "firmware.uf2").exists())

    def test_single_discovered_volume_is_used(self) -> None:
        found = [{"path": str(self.volume), "board_id": "RPI-RP2", "info": {}}]
        with mock.patch.object(uf2, "find_uf2_volumes", return_value=found), \
             mock.patch.object(uf2, "wait_for_volume_gone", return_value=True):
            fe.flash_uf2(self._ns(device=""), self.artifact)
        self.assertTrue(self.result["ok"])


class DoFlashUf2RoutingTests(unittest.TestCase):
    """--uf2 must reach ports that have no serial flasher (esp32 + tinyuf2)."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)
        self.results: list[dict] = []
        patch = mock.patch.object(
            fe, "emit_result",
            side_effect=lambda ok, **kw: self.results.append(dict(ok=ok, **kw)),
        )
        patch.start()
        self.addCleanup(patch.stop)
        patch2 = mock.patch.object(fe, "save_state", lambda *a, **k: None)
        patch2.start()
        self.addCleanup(patch2.stop)

    def _ns(self, artifact: Path, **kw) -> argparse.Namespace:
        base = dict(port="esp32", board="", variant="", mp=None, device="",
                    artifact=str(artifact), uf2=True, uf2_timeout=1.0)
        base.update(kw)
        return argparse.Namespace(**base)

    def test_uf2_flag_routes_esp32_to_the_uf2_path(self) -> None:
        art = self.dir / "firmware.uf2"
        art.write_bytes(_uf2_bytes(family=0xC47E5767))
        with mock.patch.object(fe, "flash_uf2") as flash:
            fe.do_flash(self._ns(art))
        flash.assert_called_once()

    def test_uf2_flag_rejects_a_non_uf2_artifact(self) -> None:
        art = self.dir / "firmware.bin"
        art.write_bytes(b"\x00" * 64)
        with mock.patch.object(fe, "flash_uf2") as flash:
            fe.do_flash(self._ns(art))
        flash.assert_not_called()
        self.assertFalse(self.results[0]["ok"])
        self.assertIn("needs a .uf2 artifact", self.results[0]["error"])

    def test_unflashable_port_still_rejected_without_the_flag(self) -> None:
        art = self.dir / "firmware.uf2"
        art.write_bytes(_uf2_bytes())
        fe.do_flash(self._ns(art, port="stm32", uf2=False))
        self.assertFalse(self.results[0]["ok"])
        self.assertIn("not supported", self.results[0]["error"])

    def test_unflashable_port_is_allowed_with_the_flag(self) -> None:
        art = self.dir / "firmware.uf2"
        art.write_bytes(_uf2_bytes())
        with mock.patch.object(fe, "flash_uf2") as flash:
            fe.do_flash(self._ns(art, port="stm32"))
        flash.assert_called_once()


if __name__ == "__main__":
    unittest.main()
