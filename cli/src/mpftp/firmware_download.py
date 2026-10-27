"""
Official firmware: a catalog of boards, the images each one has, and the
download.

Boards and their images come from *sources*. A source answers two questions:
which boards it has (``boards``), and which firmware files one board has
(``firmware``). Everything else here, the CLI's ``download-tree``,
``download-list`` and ``download`` and the browser's Firmware dialog, shows
whatever the configured sources return.

The source is MicroPython's own download site, read the way the site
itself is built:

* ``https://micropython.org/resources/boards/index.json`` lists every board.
  MicroPython's ``tools/autobuild/build-downloads.py`` writes it from each
  ``ports/<port>/boards/<BOARD>/board.json``, so it carries the board's port,
  MCU, vendor, product name, variants and ``deploy_options.flash_offset``.
* ``https://micropython.org/download/<BOARD>/`` is the board's page. Its links
  to ``/resources/firmware/`` are the files, named by the autobuild's contract
  ``<BOARD>[-<VARIANT>]-<YYYYMMDD>-v<version>.<ext>``; a version with
  ``-preview.`` in it is a nightly. The firmware directory itself has no
  listing, so the page is the only list of files.

How this was worked out, and how to check it still holds, is in
``docs/how-it-works/firmware-downloads.md``.
"""

from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.parse import unquote, urlparse

MICROPYTHON_ORG = "https://micropython.org"
MICROPYTHON_BOARD_INDEX = MICROPYTHON_ORG + "/resources/boards/index.json"
# A board's board.json, for a board the catalog doesn't carry (the flash
# offset of a board built from a local tree): same file the index is made of.
MP_BOARD_JSON_PREFIX = (
    "https://raw.githubusercontent.com/micropython/micropython/master/ports"
)

CACHE_ROOT = Path.home() / ".mpftp" / "firmware-cache"
INDEX_TTL_SEC = 24 * 3600
#: A board's page changes with every nightly; an hour keeps a session snappy.
PAGE_TTL_SEC = 3600

USER_AGENT = "mpftp (MicroPython firmware download)"

#: How each port's boards are flashed from a downloaded image: ``esptool``
#: writes an ESP ``.bin``, ``uf2`` copies a ``.uf2`` to the bootloader drive.
#: Ports missing here have images in other formats (``.dfu``, ``.hex``) that
#: mpftp doesn't write; their boards aren't in the catalog.
FLASHERS = {
    "esp32": "esptool",
    "rp2": "uf2",
    "samd": "uf2",
    "nrf": "uf2",
    "mimxrt": "uf2",
}
PORT_ORDER = ("esp32", "rp2", "samd", "nrf", "mimxrt")

Fetcher = Callable[[str, float], str]


# --------------------------------------------------------------------------- #
# Fetching and caching
# --------------------------------------------------------------------------- #

def cache_root() -> Path:
    CACHE_ROOT.mkdir(parents=True, exist_ok=True)
    (CACHE_ROOT / "index").mkdir(exist_ok=True)
    (CACHE_ROOT / "files").mkdir(exist_ok=True)
    return CACHE_ROOT


def default_fetch(url: str, timeout: float = 30.0) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="replace")


def _safe(name: str) -> str:
    return re.sub(r"[^\w.-]+", "_", name or "unknown")


def _load_cached(path: Path, ttl: float) -> Optional[str]:
    try:
        if not path.is_file() or time.time() - path.stat().st_mtime > ttl:
            return None
        return path.read_text("utf-8")
    except OSError:
        return None


def _save_cached(path: Path, text: str) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    except OSError:
        pass


def _fetch_cached(
    url: str, cache: Path, ttl: float, fetch: Optional[Fetcher], force: bool
) -> str:
    if not force:
        text = _load_cached(cache, ttl)
        if text is not None:
            return text
    text = (fetch or default_fetch)(url, 30.0)
    _save_cached(cache, text)
    return text


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #

def normalize_flash_offset(raw: Any) -> str:
    """Normalize board.json flash_offset values (``0``, ``0x0``, ``0x2000``)."""
    s = str(raw).strip()
    if not s:
        return "0x0"
    try:
        return hex(int(s, 0))
    except ValueError:
        return s if s.lower().startswith("0x") else f"0x{s}"


def artifact_kind(url: str) -> str:
    """A firmware file's kind from its name: ``bin``, ``uf2``, ``app-bin``, ``hex``…"""
    name = unquote(urlparse(url or "").path.rsplit("/", 1)[-1]).lower()
    for kind in ("app-bin", "meta.zip"):
        if name.endswith("." + kind):
            return kind
    return name.rsplit(".", 1)[-1] if "." in name else ""


def _is_preview_version(version: str) -> bool:
    return "preview" in (version or "").lower()


def _version_key(version: str) -> tuple:
    """Sort key for ``1.28.0`` / ``1.30.0-preview.95.g5b5f06cb48``."""
    base, _, rest = (version or "").lstrip("v").partition("-")
    nums = tuple(int(n) if n.isdigit() else 0 for n in base.split("."))
    m = re.search(r"preview\.(\d+)", rest)
    # A release sorts after its own previews.
    return nums + ((int(m.group(1)),) if m else (1 << 30,))


def _sort_files(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Releases newest first, then previews newest first."""
    releases = [d for d in items if d["channel"] == "release"]
    previews = [d for d in items if d["channel"] == "preview"]
    for group in (releases, previews):
        group.sort(key=lambda d: (_version_key(d["version"]), d.get("date", "")), reverse=True)
    return releases + previews


# --------------------------------------------------------------------------- #
# Sources
# --------------------------------------------------------------------------- #

class FirmwareSource:
    """Where a catalog's boards and their firmware files come from.

    ``boards`` returns board entries::

        {"source", "board", "vendor", "model", "title", "family", "port",
         "info_url", "variants": {name: description}, "flash_offset",
         "features"}

    ``firmware`` returns one board's files, any order::

        {"name", "version", "date", "url", "kind", "variant", "channel",
         "offset"}

    ``kind`` is the file's format (``bin``, ``uf2``, ``hex``…), ``variant``
    is ``""`` for the board's default build, ``channel`` is ``release`` or
    ``preview``, and ``offset`` is where an ESP ``.bin`` is written (``""``
    when it isn't one).
    """

    name = ""
    label = ""

    def boards(self, *, fetch: Optional[Fetcher] = None, force: bool = False) -> list[dict[str, Any]]:
        raise NotImplementedError

    def firmware(
        self, board: dict[str, Any], *, fetch: Optional[Fetcher] = None, force: bool = False
    ) -> list[dict[str, Any]]:
        raise NotImplementedError


class MicroPythonOrg(FirmwareSource):
    """micropython.org: its board index and each board's download page."""

    name = "micropython.org"
    label = "MicroPython (micropython.org)"

    def __init__(self, base: str = MICROPYTHON_ORG) -> None:
        self.base = base.rstrip("/")

    def boards(self, *, fetch: Optional[Fetcher] = None, force: bool = False) -> list[dict[str, Any]]:
        text = _fetch_cached(
            self.base + "/resources/boards/index.json",
            cache_root() / "index" / "micropython-boards.json",
            INDEX_TTL_SEC,
            fetch,
            force,
        )
        data = json.loads(text)
        if not isinstance(data, list):
            raise ValueError("micropython.org's board index isn't a list")
        return [b for b in (self._board(raw) for raw in data if isinstance(raw, dict)) if b]

    def _board(self, raw: dict[str, Any]) -> Optional[dict[str, Any]]:
        board = str(raw.get("id") or raw.get("build") or "")
        if not board:
            return None
        deploy = raw.get("deploy_options") if isinstance(raw.get("deploy_options"), dict) else {}
        offset = deploy.get("flash_offset")
        variants = raw.get("variants") if isinstance(raw.get("variants"), dict) else {}
        vendor = str(raw.get("vendor") or "")
        model = str(raw.get("product") or "")
        return {
            "source": self.name,
            "board": board,
            "vendor": vendor,
            "model": model,
            "title": f"{vendor} {model}".strip() or board,
            "family": str(raw.get("mcu") or ""),
            "port": str(raw.get("port") or ""),
            "info_url": f"{self.base}/download/{board}/",
            "variants": {str(k): str(v) for k, v in variants.items()},
            "flash_offset": normalize_flash_offset(offset) if offset not in (None, "") else "",
            "features": [str(f) for f in raw.get("features") or []],
        }

    def firmware(
        self, board: dict[str, Any], *, fetch: Optional[Fetcher] = None, force: bool = False
    ) -> list[dict[str, Any]]:
        name = board.get("board") or ""
        html = _fetch_cached(
            f"{self.base}/download/{name}/",
            cache_root() / "pages" / f"{_safe(name)}.html",
            PAGE_TTL_SEC,
            fetch,
            force,
        )
        files = parse_board_page(name, html, base=self.base)
        offset = board.get("flash_offset") or ""
        if not offset and board.get("port") == "esp32":
            from .firmware import esp32_flash_offset_for_family

            offset = esp32_flash_offset_for_family(board.get("family") or "")
        for f in files:
            f["offset"] = offset if f["kind"] == "bin" and board.get("port") == "esp32" else ""
        return files


def parse_board_page(board: str, html: str, base: str = MICROPYTHON_ORG) -> list[dict[str, Any]]:
    """The firmware files a micropython.org board page links to.

    Filenames follow the autobuild's contract::

        ESP32_GENERIC_P4-20260406-v1.28.0.bin           variant ""
        ESP32_GENERIC_P4-C6_WIFI-20260406-v1.28.0.bin   variant "C6_WIFI"
        RPI_PICO-20260929-v1.30.0-preview.95.g5b5f06cb48.uf2
    """
    # A variant starts with a letter, so the 8-digit date is never one.
    file_re = re.compile(
        rf"{re.escape(board)}(?:-([A-Za-z_][A-Za-z0-9_]*))?-(\d{{8}})-v(.+?)"
        r"\.(app-bin|meta\.zip|[a-z0-9]+)$",
        re.I,
    )
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for rel in re.findall(r'href="(/resources/firmware/[^"]+)"', html, flags=re.I):
        filename = unquote(rel.rsplit("/", 1)[-1])
        m = file_re.fullmatch(filename)
        if not m:
            continue
        url = base + rel
        if url in seen:
            continue
        seen.add(url)
        version = m.group(3)
        out.append(
            {
                "name": filename,
                "version": version,
                "date": m.group(2),
                "url": url,
                "kind": m.group(4).lower(),
                "variant": m.group(1) or "",
                "channel": "preview" if _is_preview_version(version) else "release",
                "offset": "",
            }
        )
    return out


#: The sources the catalog reads, in order. A board found in two keeps the
#: first one's entry.
SOURCES: list[FirmwareSource] = [MicroPythonOrg()]


def configured_sources() -> list[FirmwareSource]:
    return list(SOURCES)


def _source_named(name: str) -> FirmwareSource:
    for s in configured_sources():
        if s.name == name:
            return s
    raise RuntimeError(f"unknown firmware source {name!r}")


# --------------------------------------------------------------------------- #
# The catalog
# --------------------------------------------------------------------------- #

def load_catalog(
    *,
    fetch: Optional[Fetcher] = None,
    force: bool = False,
    sources: Optional[list[FirmwareSource]] = None,
) -> list[dict[str, Any]]:
    """Every board the sources list whose port mpftp can flash, sorted."""
    merged: list[dict[str, Any]] = []
    seen: set[str] = set()
    for source in sources if sources is not None else configured_sources():
        for b in source.boards(fetch=fetch, force=force):
            if b["port"] not in FLASHERS or b["board"].upper() in seen:
                continue
            seen.add(b["board"].upper())
            merged.append(b)
    order = {p: i for i, p in enumerate(PORT_ORDER)}
    merged.sort(
        key=lambda b: (order.get(b["port"], 99), b["vendor"].lower(), b["model"].lower(), b["board"])
    )
    return merged


def board_label(b: dict[str, Any]) -> str:
    named = f"{b.get('vendor', '')} {b.get('model', '')}".strip()
    return f"{named} ({b['board']})" if named else b["board"]


def catalog_tree(
    catalog: Optional[list[dict[str, Any]]] = None, **load_kw: Any
) -> dict[str, Any]:
    """The catalog grouped by port, each with the flasher its boards use."""
    if catalog is None:
        catalog = load_catalog(**load_kw)
    ports: dict[str, dict[str, Any]] = {}
    for b in catalog:
        bucket = ports.setdefault(
            b["port"],
            {
                "port": b["port"],
                "kind": "boards",
                "flashable": True,
                "flasher": FLASHERS.get(b["port"], ""),
                "boards": [],
            },
        )
        bucket["boards"].append(
            {
                "board": b["board"],
                "vendor": b["vendor"],
                "model": b["model"],
                "family": b["family"],
                "title": b["title"],
                "label": board_label(b),
                "info_url": b["info_url"],
                "catalogSource": b["source"],
                "popular": False,
                "variants": sorted(b["variants"]),
            }
        )
    ordered = [ports[p] for p in PORT_ORDER if p in ports]
    return {"ports": ordered, "sources": [s.name for s in configured_sources()]}


def find_variant(
    board: str, catalog: Optional[list[dict[str, Any]]] = None, **load_kw: Any
) -> Optional[dict[str, Any]]:
    """The catalog entry for ``board`` (case-insensitive), or None."""
    if catalog is None:
        catalog = load_catalog(**load_kw)
    want = (board or "").upper()
    for b in catalog:
        if b["board"].upper() == want:
            return b
    return None


find_board = find_variant


def board_files(
    entry: dict[str, Any], *, fetch: Optional[Fetcher] = None, force: bool = False
) -> list[dict[str, Any]]:
    """Every firmware file a board's source has for it, newest first."""
    return _sort_files(_source_named(entry["source"]).firmware(entry, fetch=fetch, force=force))


def preferred_artifact_kind(entry: dict[str, Any], *, uf2: bool = False) -> str:
    """The image a board's flasher writes: ``bin`` for esptool, ``uf2`` for UF2.

    ``uf2=True`` asks for the UF2 even on an ESP board (an S2 or S3 running a
    UF2 bootloader).
    """
    if uf2:
        return "uf2"
    return "bin" if FLASHERS.get(entry.get("port") or "") == "esptool" else "uf2"


def flashable_files(entry: dict[str, Any], files: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The files mpftp can write to this board: ESP ``.bin`` or any ``.uf2``."""
    esp = entry.get("port") == "esp32"
    return [f for f in files if f["kind"] == "uf2" or (esp and f["kind"] == "bin")]


def downloads_by_variant(
    entry: dict[str, Any], *, fetch: Optional[Fetcher] = None, force: bool = False
) -> dict[str, list[dict[str, Any]]]:
    """Flashable files grouped by variant (``""`` is the default build)."""
    out: dict[str, list[dict[str, Any]]] = {}
    for f in flashable_files(entry, board_files(entry, fetch=fetch, force=force)):
        out.setdefault(f["variant"], []).append(
            {
                "version": f["version"],
                "date": f["date"],
                "url": f["url"],
                "name": f["name"],
                "kind": f["kind"],
                "channel": f["channel"],
                "mp_variant": f["variant"],
                "offset": f["offset"],
            }
        )
    return out


def _select(items: list[dict[str, Any]], kind: str) -> Optional[dict[str, Any]]:
    for item in items:
        if item.get("kind") == kind:
            return item
    return None


def pick_download(
    entry: dict[str, Any],
    *,
    version: Optional[str] = None,
    preview: bool = False,
    mp_variant: str = "",
    fetch: Optional[Fetcher] = None,
    uf2: bool = False,
    by_variant: Optional[dict[str, list[dict[str, Any]]]] = None,
) -> dict[str, Any]:
    """The file to download for a board, variant and version.

    The newest release by default, the newest preview with ``preview``, or an
    exact ``version``; always in the format the board's flasher writes (see
    ``preferred_artifact_kind``).
    """
    if by_variant is None:
        by_variant = downloads_by_variant(entry, fetch=fetch)
    board = entry.get("board")
    mp_variant = mp_variant or ""
    extra = f" / {mp_variant}" if mp_variant else ""
    if mp_variant not in by_variant:
        known = ", ".join(sorted(k or "(default)" for k in by_variant)) or "(none)"
        raise RuntimeError(f"no {mp_variant or 'default'} build for {board}; it has: {known}")
    kind = preferred_artifact_kind(entry, uf2=uf2)
    items = [d for d in by_variant[mp_variant] if d["kind"] == kind]
    if not items:
        raise RuntimeError(
            f"{board}{extra} has no .{kind} image to flash; see {entry.get('info_url')}"
        )
    if version:
        want = version.lstrip("v")
        chosen = _select([d for d in items if d["version"] == want], kind)
        if chosen:
            return chosen
        raise RuntimeError(f"version {version} not found for {board}{extra}")
    channel = "preview" if preview else "release"
    chosen = _select([d for d in items if d["channel"] == channel], kind)
    if chosen:
        return chosen
    if preview:
        raise RuntimeError(f"no preview build for {board}{extra}")
    raise RuntimeError(f"no release build for {board}{extra}")


def list_board(
    board: str,
    *,
    mp_variant: str = "",
    fetch: Optional[Fetcher] = None,
    catalog: Optional[list[dict[str, Any]]] = None,
    include_preview_probe: bool = False,
    force: bool = False,
) -> dict[str, Any]:
    """A board's variants and the downloads for one of them."""
    entry = find_variant(board, catalog=catalog, fetch=fetch, force=force)
    if not entry:
        raise RuntimeError(f"board not in the download catalog: {board}")
    by_var = downloads_by_variant(entry, fetch=fetch, force=force)
    mp_variant = mp_variant or ""
    downloads = list(by_var.get(mp_variant, [])) if mp_variant else list(by_var.get("", []))
    preview = None
    if include_preview_probe:
        preview = next((d for d in downloads if d["channel"] == "preview"), None)
    out: dict[str, Any] = {
        "board": entry["board"],
        "catalogSource": entry["source"],
        "vendor": entry["vendor"],
        "model": entry["model"],
        "family": entry["family"],
        "port": entry["port"],
        "flasher": FLASHERS.get(entry["port"], ""),
        "info_url": entry["info_url"],
        "variants": sorted(k for k in by_var if k),
        "variantNotes": dict(entry.get("variants") or {}),
        "variant": mp_variant,
        "downloads": downloads,
        "preview": preview,
    }
    if entry.get("flash_offset"):
        out["flashOffset"] = entry["flash_offset"]
    return out


# --------------------------------------------------------------------------- #
# board.json for a board built locally (flash offset when no tree has it)
# --------------------------------------------------------------------------- #

def board_json_url(board: str, port: str = "esp32") -> str:
    return f"{MP_BOARD_JSON_PREFIX.rstrip('/')}/{port}/boards/{board}/board.json"


def fetch_board_json(
    board: str,
    *,
    port: str = "esp32",
    fetch: Optional[Fetcher] = None,
    force: bool = False,
) -> Optional[dict[str, Any]]:
    """A board's ``board.json`` from MicroPython's GitHub tree, cached."""
    if not board:
        return None
    try:
        text = _fetch_cached(
            board_json_url(board, port),
            cache_root() / "board-json" / _safe(port) / f"{_safe(board)}.json",
            INDEX_TTL_SEC,
            fetch,
            force,
        )
        data = json.loads(text)
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def flash_offset_from_board_json(data: dict[str, Any]) -> Optional[str]:
    """Return deploy_options.flash_offset when present (normalized)."""
    deploy = data.get("deploy_options") or {}
    if not isinstance(deploy, dict):
        return None
    raw = deploy.get("flash_offset")
    if raw is None or str(raw).strip() == "":
        return None
    return normalize_flash_offset(raw)


def resolve_remote_flash_offset(
    board: str,
    *,
    port: str = "esp32",
    fetch: Optional[Fetcher] = None,
    force: bool = False,
) -> Optional[str]:
    """Flash offset from upstream board.json, or None if unavailable."""
    data = fetch_board_json(board, port=port, fetch=fetch, force=force)
    if not data:
        return None
    return flash_offset_from_board_json(data)


# --------------------------------------------------------------------------- #
# Download
# --------------------------------------------------------------------------- #

def download_file(
    url: str,
    *,
    dest_dir: Optional[Path] = None,
    progress: Optional[Callable[[int, int], None]] = None,
) -> Path:
    """Download ``url`` into the firmware cache; a same-sized copy is reused."""
    dest_dir = dest_dir or (cache_root() / "files")
    dest_dir.mkdir(parents=True, exist_ok=True)
    name = unquote(urlparse(url).path.rsplit("/", 1)[-1]) or "firmware.bin"
    dest = dest_dir / name

    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=120) as resp:
        total = int(resp.headers.get("Content-Length") or 0)
        if dest.is_file() and total and dest.stat().st_size == total:
            if progress:
                progress(total, total)
            return dest
        tmp = dest.with_suffix(dest.suffix + ".partial")
        written = 0
        with open(tmp, "wb") as f:
            while True:
                chunk = resp.read(256 * 1024)
                if not chunk:
                    break
                f.write(chunk)
                written += len(chunk)
                if progress and total:
                    progress(written, total)
        if total and written != total:
            tmp.unlink()
            raise RuntimeError(f"download of {name} stopped at {written} of {total} bytes")
        tmp.replace(dest)
    return dest


def download_board(
    board: str,
    *,
    version: Optional[str] = None,
    preview: bool = False,
    mp_variant: str = "",
    uf2: bool = False,
    fetch: Optional[Fetcher] = None,
    catalog: Optional[list[dict[str, Any]]] = None,
    progress: Optional[Callable[[int, int], None]] = None,
    on_url: Optional[Callable[[str], None]] = None,
    force: bool = False,
) -> dict[str, Any]:
    """Choose, download and describe one board's firmware (the CLI's result)."""
    entry = find_variant(board, catalog=catalog, fetch=fetch, force=force)
    if not entry:
        raise RuntimeError(f"board not in the download catalog: {board}")
    chosen = pick_download(
        entry, version=version, preview=preview, mp_variant=mp_variant, fetch=fetch, uf2=uf2
    )
    if on_url:
        on_url(chosen["url"])
    path = download_file(chosen["url"], progress=progress)
    st = path.stat()
    out: dict[str, Any] = {
        "ready": True,
        "artifact": str(path),
        "size": st.st_size,
        "mtime": st.st_mtime,
        "source": "download",
        "catalogSource": entry["source"],
        "board": entry["board"],
        "variant": mp_variant or "",
        "version": chosen["version"],
        "kind": chosen["kind"],
        "family": entry["family"],
        "port": entry["port"],
        "url": chosen["url"],
        "info_url": entry["info_url"],
        "vendor": entry["vendor"],
        "model": entry["model"],
    }
    if chosen.get("offset"):
        out["flashOffset"] = chosen["offset"]
    return out
