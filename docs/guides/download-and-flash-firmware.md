# Download and flash firmware

Official MicroPython images need no checkout and no toolchain. To build your
own, see [Build firmware](build-firmware.md); flashing works the same for both.

## Find out what you have

```bash
mpftp firmware detect -d PORT
```

Detect uses esptool first, so it works on a bare board with no interpreter,
and reports chip, flash size, secure boot and flash encryption. If secure boot
or flash encryption is on, stop and be sure before you erase anything. In the
editor's Firmware panel and the browser interface, **Detect** does the same
and can then add what a live MicroPython session reports.

## Download

```bash
mpftp firmware download-tree                        # boards, by port
mpftp firmware download-list --board RPI_PICO       # versions, newest release first
mpftp firmware download --board ESP32_GENERIC_S3 [--variant SPIRAM_OCT] [--version 1.29.0 | --preview] [--uf2]
```

`download` prints the cached file's path: a `.bin` for ESP boards and a `.uf2`
elsewhere. Releases come first, then the nightly previews. The catalog covers
esp32 (written with esptool) and rp2, samd, nrf and mimxrt (UF2). A board
whose images are only `.dfu` or `.hex` (stm32, most nrf) isn't listed; use its
page on micropython.org. In the Firmware panel, choose **Download**, type part
of the board's name (Pico, ESP32-S3, XIAO), then its variant and version. Choose
**File** to flash a `.bin` or `.uf2` you already have. Images are kept in
`~/.mpftp/firmware-cache/`.

## Flash an ESP board

```bash
mpftp firmware flash --port esp32 --artifact PATH.bin -d PORT
```

An ESP `.bin` (the combined image, such as `ESP32_GENERIC_S3-...bin`) is
written over serial with esptool at the offset the chip needs: 0x1000 on the
ESP32 and S2, 0x2000 on the P4 and C5, 0x0 on the rest. mpftp lets go of the
board first, shows progress, and reconnects when the board restarts. Tick
**Erase all of flash first** (`--erase`) for a clean board; it deletes the
files on it. A board whose REPL is its own USB (an S2 or S3 on native USB) is
put into its bootloader first. If no download port appears, hold BOOT, tap
RESET, choose the new port and flash again; [Recover a board](recover-a-board.md)
covers the stubborn cases.

Flashing without a rebuild to another board is `mpftp firmware flash -d OTHER_PORT`
after a build.

## Flash a UF2 board

A `.uf2` (rp2, samd, and the nrf and mimxrt boards that have one) is copied to
the board's UF2 bootloader drive. Pick the drive, or leave it to mpftp: with
none mounted, it restarts the connected board into its bootloader with
`machine.bootloader()`. Otherwise hold BOOTSEL (or BOOT) while you plug the
board in, or double-tap reset.

`--uf2` forces this path for any port, which reaches a board whose UF2
bootloader its MicroPython port doesn't imply, such as an ESP32-S3 carrying
tinyuf2:

```bash
mpftp bootloader -d PORT                  # or double-tap reset / hold BOOTSEL
mpftp firmware flash --port esp32 --uf2 --artifact build/firmware.uf2
```

The artifact must be a `.uf2`; mpftp refuses a `.bin` rather than copy
something the bootloader will ignore. A UF2 copy can look like success and do
nothing, so mpftp validates the file first (magic, block count against the
header, family IDs), checks the byte count it wrote, and then waits for the
bootloader volume to unmount, which only happens once the board has accepted a
complete image and rebooted. A volume still mounted after `--uf2-timeout`
seconds (default 30) is reported as a failure naming the image's family,
because a family mismatch is the usual cause.

mpftp finds volumes by `INFO_UF2.TXT`, not by label. If more than one is
mounted it stops and asks for `--device`. On WSL a removable drive is usually
not mounted under `/mnt`, so discovery also asks Windows for drive letters, and
`--device 'D:'` works there too.

## Partition layout

If the layout on the board differs from the image's, mpftp stops and warns
instead of erasing. Tick **Erase flash before writing** and flash again, knowing
a full erase wipes the filesystem partition and every file on the board.
[Build firmware](build-firmware.md#when-the-image-doesnt-fit) covers the build
side.
