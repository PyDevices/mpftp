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
`deploy_options.flash_offset`. The catalog keeps the ports mcuctl can flash from
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
`~/.mcuctl/firmware-cache/`, along with the downloaded images. `firmware_fetch`
downloads only a URL the board's page listed.

## OmniPython's releases

The second source, `--source omnipython`, reads `PyDevices/omnipython-builds` the
way its releases are made. `GET /repos/PyDevices/omnipython-builds/releases`
lists them; each firmware release has a `firmware.json` asset with a `builds`
list. A build has an `id` (`mp-s3-octal`, which is the board name here), a
`chip`, a `flash_offset` and `app_offset`, and `files`: a `role` (`flash` is the
whole-flash image, `app` the app-only image, `uf2` for RP2), a name, a size and
a sha256. The manifest's `preview` flag decides whether a build is a release or
a preview, and the release's tag is its version.

A draft release's public URL answers 404, so its files are fetched through the
API's asset URL with a token from `GH_TOKEN` or `GITHUB_TOKEN`. The token goes to
api.github.com only, and is dropped when GitHub redirects the download to its
storage. The release list and each manifest are cached for an hour, the same as a
micropython.org page. A release with no manifest is skipped; if none has one, or a
file the manifest lists isn't on the release, or a download doesn't match its
sha256, the command stops and says which.

## Check it still holds

```bash
curl -s https://micropython.org/resources/boards/index.json | python -m json.tool | head
curl -s https://micropython.org/download/RPI_PICO/ | grep -o '/resources/firmware/[^"]*' | head
mcuctl firmware download-list --board ESP32_GENERIC_S3 --force
gh release list -R PyDevices/omnipython-builds
mcuctl firmware download-list --board mp-s3-octal --source omnipython --force
```
