# Official firmware downloads

`firmware_download.py` builds the download catalog the way micropython.org
builds its own download pages, so nothing in between has to be kept up to date.
The browser interface's Firmware dialog and the `firmware download-*` commands
share this code.

## The board list

The board list is `https://micropython.org/resources/boards/index.json`.
MicroPython's `tools/autobuild/build-downloads.py` writes it from every
`ports/<port>/boards/<BOARD>/board.json`, and the site's board pages are rendered
from it. Each entry has `id` (the board, `ESP32_GENERIC_S3`), `port`, `mcu`,
`vendor`, `product`, `variants` (name to description) and, for ESP boards,
`deploy_options.flash_offset`. The catalog keeps the ports mpftp can flash from
a download: esp32 (esptool) and rp2, samd, nrf and mimxrt (UF2).

## The files

The files have no listing of their own: `https://micropython.org/resources/firmware/`
answers 403. A board's page, `https://micropython.org/download/<BOARD>/`, links
every file it has, and the names follow the autobuild's contract
(`tools/autobuild/autobuild.sh`):

```text
<BOARD>[-<VARIANT>]-<YYYYMMDD>-v<version>.<ext>
ESP32_GENERIC_P4-C6_WIFI-20260406-v1.28.0.bin
RPI_PICO-20260929-v1.30.0-preview.95.g5b5f06cb48.uf2
```

A variant name starts with a letter, so it can't be mistaken for the date. A
version with `-preview.` in it is a nightly; the server keeps the last few of
those and every release. ESP boards have `.bin` (the combined image, written at
the board's `flash_offset`), `.uf2`, `.app-bin`, `.elf`, `.map` and `.meta.zip`;
rp2 and samd have `.uf2`; stm32 has `.dfu` and `.hex`.

The index is cached for a day and each page for an hour in
`~/.mpftp/firmware-cache/`, along with the downloaded images. `firmware_fetch`
downloads only a URL the board's page listed.

## Check it still holds

```bash
curl -s https://micropython.org/resources/boards/index.json | python -m json.tool | head
curl -s https://micropython.org/download/RPI_PICO/ | grep -o '/resources/firmware/[^"]*' | head
mpftp firmware download-list --board ESP32_GENERIC_S3 --force
```
