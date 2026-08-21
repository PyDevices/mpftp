#!/usr/bin/env python3
"""
UF2 support for the mpftp firmware engine: file validation, bootloader-volume
discovery, and the copy-and-verify flash itself.

Flashing over UF2 is a file copy, which makes the *failure* modes the hard part
rather than the success path. Two in particular drove this module:

* A copy can report success and write nothing. PowerShell's ``Copy-Item`` fails
  non-terminatingly (exit 0, no file), and for a flash operation "succeeded" and
  "wrote nothing" being indistinguishable is the worst possible ambiguity.
* A bootloader silently ignores UF2 blocks whose family ID it does not own. The
  copy then genuinely succeeds, the drive stays mounted, and nothing is flashed.

So the copy is never the proof. The proof is that the volume *disappears*: a UF2
bootloader reboots into the new firmware once it has accepted a full image, and
the mount goes with it. ``flash_uf2_file`` therefore waits for the volume to go
away and treats a volume that is still there as a failure -- see the module's
one real assumption, documented on ``wait_for_volume_gone``.

Stdlib only, like the rest of the engine.
"""

from __future__ import annotations

import os
import shutil
import struct
import subprocess
import time
from pathlib import Path
from typing import Any, Callable, Optional

# UF2 block format: 512 bytes, 32-byte header, 476-byte payload area, 4-byte end
# magic. https://github.com/microsoft/uf2
UF2_MAGIC_START0 = 0x0A324655  # "UF2\n"
UF2_MAGIC_START1 = 0x9E5D5157
UF2_MAGIC_END = 0x0AB16F30
UF2_BLOCK_SIZE = 512

# Header flags we care about.
UF2_FLAG_NOT_MAIN_FLASH = 0x00000001
UF2_FLAG_FILE_CONTAINER = 0x00001000
UF2_FLAG_FAMILY_ID = 0x00002000
UF2_FLAG_MD5_CHECKSUM = 0x00004000

# Family IDs from the upstream registry (microsoft/uf2 utils/uf2families.json).
# Vendored rather than read from a sibling checkout so the engine stays
# self-contained; unknown IDs degrade to hex, never to an error.
#
# Note that this registry is not universally honoured: CircuitPython's espressif
# Makefile builds esp32p4 images with 0x540ddf62, which the registry assigns to
# ESP32-C6. That disagreement is exactly why a family mismatch is reported as
# information rather than enforced as a hard failure.
UF2_FAMILIES: dict[int, str] = {
    0x00ff6919: "STM32L4",
    0x04240bdf: "STM32L5",
    0x06d1097b: "STM32F411xC",
    0x11de784a: "M0SENSE",
    0x16573617: "ATMEGA32",
    0x1851780a: "SAML21",
    0x1b57745f: "NRF52",
    0x1c5f21b0: "ESP32",
    0x1e1f432d: "STM32L1",
    0x202e3a91: "STM32L0",
    0x21460ff0: "STM32WL",
    0x22e0d6fc: "RTL8710B",
    0x2abc77ec: "LPC55",
    0x2b88d29c: "ESP32C2",
    0x2dc309c5: "STM32F411xE",
    0x300f5633: "STM32G0",
    0x3101f7c1: "ESP32S31",
    0x31d228c6: "GD32F350",
    0x332726f6: "ESP32H2",
    0x3379CFE2: "RTL8720D",
    0x3d308e94: "ESP32P4",
    0x4b684d71: "MaixPlay-U4",
    0x4c71240a: "STM32G4",
    0x4e8f1c5d: "STM32H5",
    0x4f6ace52: "CSK4",
    0x4fb2d5bd: "MIMXRT10XX",
    0x51e903a8: "XR809",
    0x53b80f00: "STM32F7",
    0x540ddf62: "ESP32C6",
    0x55114460: "SAMD51",
    0x57755a57: "STM32F4",
    0x5a18069b: "FX2",
    0x5d1a0a2e: "STM32F2",
    0x5ee21072: "STM32F1",
    0x621e937a: "NRF52833",
    0x647824b6: "STM32F0",
    0x675a40b0: "BK7231U",
    0x68ed2b88: "SAMD21",
    0x699b62ec: "CH32V",
    0x6a82cc42: "BK7251",
    0x6b846188: "STM32F3",
    0x6d0922fa: "STM32F407",
    0x6db66082: "STM32H7",
    0x6e7348a8: "CSK6",
    0x6f752678: "NRF52832xxAB",
    0x70d16653: "STM32WB",
    0x72721d4e: "NRF52832xxAA",
    0x7410520a: "MAX32690",
    0x77d850c4: "ESP32C61",
    0x7b3ef230: "BK7231N",
    0x7be8976d: "RA4M1",
    0x7d7a66ef: "PY32F071-UVK5-V3",
    0x7eab61ed: "ESP8266",
    0x7f83e793: "KL32L2",
    0x820d9a5f: "NRF52820",
    0x8fb060fe: "STM32F407VG",
    0x91d3fd18: "MAX78002",
    0x9517422f: "RZA1LU",
    0x9af03e33: "GD32VF103",
    0x9e0baa8a: "ESP32H4",
    0x9fffd543: "RTL8710A",
    0xa0c97b8e: "AT32F415",
    0xada52840: "NRF52840",
    0xb6dd00af: "ESP32H21",
    0xbfdd4eee: "ESP32S2",
    0xc47e5767: "ESP32S3",
    0xd42ba06c: "ESP32C3",
    0xd63f8632: "MAX32650",
    0xde1270b7: "BL602",
    0xe08f7564: "RTL8720C",
    0xe48bff56: "RP2040",
    0xe48bff57: "RP2XXX_ABSOLUTE",
    0xe48bff58: "RP2XXX_DATA",
    0xe48bff59: "RP2350_ARM_S",
    0xe48bff5a: "RP2350_RISCV",
    0xe48bff5b: "RP2350_ARM_NS",
    0xf0c30d71: "MAX32666",
    0xf71c0343: "ESP32C5",
}


def family_name(fid: int) -> str:
    """Human name for a family ID, falling back to hex for unknown ones."""
    return UF2_FAMILIES.get(fid, "0x%08x" % fid)


# --------------------------------------------------------------------------- #
# UF2 file validation
# --------------------------------------------------------------------------- #

class Uf2Error(Exception):
    """A .uf2 file that no bootloader would accept."""


def parse_uf2(path: Path, max_blocks: int = 0) -> dict:
    """Validate a .uf2 and summarise what a bootloader would make of it.

    Returns ``{"blocks", "families", "family_names", "payload_bytes",
    "target_start", "target_end", "warnings"}``.

    Raises :class:`Uf2Error` for anything structurally wrong -- a truncated
    file, bad magic, or a block count that disagrees with the header. Checking
    this up front matters because the bootloader's response to a bad image is
    to ignore it in silence, which is indistinguishable from a failed copy.
    """
    size = path.stat().st_size
    if size == 0:
        raise Uf2Error(f"{path.name} is empty")
    if size % UF2_BLOCK_SIZE:
        raise Uf2Error(
            f"{path.name} is {size} bytes, not a multiple of {UF2_BLOCK_SIZE} "
            "-- truncated or not a UF2 file"
        )

    total = size // UF2_BLOCK_SIZE
    families: list[int] = []
    warnings: list[str] = []
    payload_bytes = 0
    addr_lo: Optional[int] = None
    addr_hi: Optional[int] = None
    declared_total: Optional[int] = None
    counted = 0

    with path.open("rb") as fh:
        for index in range(total):
            block = fh.read(UF2_BLOCK_SIZE)
            if len(block) != UF2_BLOCK_SIZE:
                raise Uf2Error(f"{path.name}: short read at block {index}")
            (start0, start1, flags, addr, payload_len,
             blk_no, num_blocks, family) = struct.unpack("<8I", block[:32])
            if start0 != UF2_MAGIC_START0 or start1 != UF2_MAGIC_START1:
                raise Uf2Error(f"{path.name}: bad start magic at block {index}")
            if struct.unpack("<I", block[-4:])[0] != UF2_MAGIC_END:
                raise Uf2Error(f"{path.name}: bad end magic at block {index}")
            if payload_len > 476:
                raise Uf2Error(
                    f"{path.name}: block {index} claims {payload_len} payload bytes (max 476)"
                )

            # Blocks flagged NOT_MAIN_FLASH are metadata the bootloader skips;
            # they must not count toward what actually gets written.
            if flags & UF2_FLAG_NOT_MAIN_FLASH:
                continue
            counted += 1
            payload_bytes += payload_len
            if declared_total is None:
                declared_total = num_blocks
            if flags & UF2_FLAG_FAMILY_ID:
                if family not in families:
                    families.append(family)
            addr_lo = addr if addr_lo is None else min(addr_lo, addr)
            addr_hi = addr + payload_len if addr_hi is None else max(addr_hi, addr + payload_len)

            if max_blocks and index + 1 >= max_blocks:
                break

    if counted == 0:
        raise Uf2Error(f"{path.name}: no flashable blocks")
    if not max_blocks and declared_total is not None and declared_total != counted:
        # A bootloader waits for numBlocks blocks before committing, so a file
        # that disagrees with its own header will hang rather than flash.
        raise Uf2Error(
            f"{path.name}: header declares {declared_total} blocks but the file has {counted}"
        )
    if not families:
        # Legal, but it means the file targets whatever it is copied onto.
        warnings.append(
            "no family ID in this UF2 -- it cannot be checked against the board"
        )
    elif len(families) > 1:
        warnings.append(
            "multiple family IDs: " + ", ".join(family_name(f) for f in families)
        )

    return {
        "blocks": counted,
        "families": families,
        "family_names": [family_name(f) for f in families],
        "payload_bytes": payload_bytes,
        "target_start": addr_lo,
        "target_end": addr_hi,
        "warnings": warnings,
    }


# --------------------------------------------------------------------------- #
# Bootloader volume discovery
# --------------------------------------------------------------------------- #

def parse_info_uf2(text: str) -> dict:
    """Parse INFO_UF2.TXT into a dict.

    The first line is a free-form banner ("UF2 Bootloader v3.0"); the rest are
    ``Key: Value``. Only ``Board-ID`` is reliably present across bootloaders.
    """
    info: dict[str, str] = {}
    lines = text.splitlines()
    if lines:
        info["banner"] = lines[0].strip()
    for line in lines[1:]:
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        key = key.strip()
        if key:
            info[key] = value.strip()
    return info


def read_volume_info(root: Path) -> dict:
    """Read INFO_UF2.TXT from a mounted bootloader volume, or {} if unreadable."""
    try:
        return parse_info_uf2((root / "INFO_UF2.TXT").read_text(errors="replace"))
    except Exception:
        return {}


def _windows_volume_letters(timeout: int = 15) -> list[str]:
    """Drive letters of removable/fixed volumes, via PowerShell.

    Used from WSL as well as Windows: WSL does not auto-mount removable drives,
    so ``/mnt/d`` is typically absent for exactly the volume we are looking for
    and a filesystem scan alone would miss every bootloader drive.
    """
    ps = (
        "Get-CimInstance Win32_LogicalDisk | "
        "Where-Object { $_.DriveType -eq 2 -or $_.DriveType -eq 3 } | "
        "ForEach-Object { $_.DeviceID }"
    )
    try:
        out = subprocess.check_output(
            ["powershell.exe", "-NoProfile", "-Command", ps],
            text=True,
            timeout=timeout,
            stderr=subprocess.DEVNULL,
        )
    except Exception:
        return []
    letters = []
    for line in out.splitlines():
        letter = line.strip().rstrip("\\/")
        if len(letter) >= 2 and letter[1] == ":":
            letters.append(letter)
    return letters


def _candidate_roots(host: str) -> list[Path]:
    """Every path that might be a mounted UF2 volume, most specific first."""
    roots: list[Path] = []
    if host in ("wsl", "windows"):
        for letter in _windows_volume_letters():
            # Prefer a WSL mount when one exists; a Windows-side path still
            # works for a Windows python and is the only option under WSL when
            # the removable drive was never mounted.
            wsl = Path(f"/mnt/{letter[0].lower()}")
            roots.append(wsl if wsl.is_dir() else Path(letter + "\\"))
    if host in ("wsl", "linux", "macos"):
        for base in ("/media", "/run/media", "/Volumes", str(Path.home() / "media")):
            base_path = Path(base)
            try:
                if not base_path.is_dir():
                    continue
                for child in base_path.iterdir():
                    if not child.is_dir():
                        continue
                    roots.append(child)
                    # /media/<user>/<LABEL>
                    try:
                        roots.extend(g for g in child.iterdir() if g.is_dir())
                    except OSError:
                        pass
            except OSError:
                continue
    seen: set[str] = set()
    uniq: list[Path] = []
    for r in roots:
        key = str(r)
        if key not in seen:
            seen.add(key)
            uniq.append(r)
    return uniq


def find_uf2_volumes(host: str) -> list[dict]:
    """Mounted UF2 bootloader volumes: ``[{"path", "info", "board_id"}]``.

    Identified by INFO_UF2.TXT, which every UF2 bootloader exposes and nothing
    else does -- more reliable than matching volume labels, which differ per
    board family (RPI-RP2, FTHRS3BOOT, ...).
    """
    found: list[dict] = []
    for root in _candidate_roots(host):
        try:
            if not (root / "INFO_UF2.TXT").is_file():
                continue
        except OSError:
            continue
        info = read_volume_info(root)
        found.append({
            "path": str(root),
            "info": info,
            "board_id": info.get("Board-ID", ""),
        })
    return found


def looks_like_volume(dev: str) -> bool:
    """True when a --device value names a mount point rather than a serial port."""
    return "/" in dev or dev.endswith(":") or dev.endswith(":\\")


# --------------------------------------------------------------------------- #
# Copy + verify
# --------------------------------------------------------------------------- #

def volume_present(root: Path) -> bool:
    """True while the bootloader volume is still mounted."""
    try:
        return (root / "INFO_UF2.TXT").is_file()
    except OSError:
        # A vanishing removable volume can raise rather than return False.
        return False


def wait_for_volume_gone(root: Path, timeout: float, poll: float = 0.25,
                         sleep: Callable[[float], None] = time.sleep,
                         now: Callable[[], float] = time.monotonic) -> bool:
    """Wait for the bootloader volume to unmount, which is what proves the flash.

    A UF2 bootloader reboots into the new firmware once it has received a
    complete, acceptable image, and the mount goes away with it. That makes
    disappearance a far stronger signal than a successful copy: it is reported
    by the board, not by the host filesystem.

    The assumption -- and the one thing that would make this wrong -- is a
    bootloader that accepts an image without rebooting. None of the bootloaders
    in scope (RP2040/RP2350, SAMD, nRF, tinyuf2) behave that way, but a false
    negative here reports failure after a flash that actually worked, so the
    caller must say so rather than claim the image was rejected.
    """
    deadline = now() + timeout
    while now() < deadline:
        if not volume_present(root):
            return True
        sleep(poll)
    return not volume_present(root)


def copy_uf2(src: Path, dest: Path) -> int:
    """Copy a UF2 onto a bootloader volume, returning the bytes written.

    Written with an explicit loop and flushed to the device rather than via
    ``shutil.copyfile`` so that a short write is an error here instead of a
    mystery later. The board can reboot mid-copy once it has the last block, so
    errors *after* the final write are expected and are left for the caller's
    disappearance check to adjudicate.
    """
    written = 0
    with src.open("rb") as fin, dest.open("wb") as fout:
        while True:
            chunk = fin.read(UF2_BLOCK_SIZE * 64)
            if not chunk:
                break
            fout.write(chunk)
            written += len(chunk)
        try:
            fout.flush()
            os.fsync(fout.fileno())
        except OSError:
            # The board rebooted as soon as it had the last block. Not an
            # error by itself -- wait_for_volume_gone decides.
            pass
    return written
