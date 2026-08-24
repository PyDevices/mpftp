"""
Official MicroPython firmware catalog + download (Thonny-compatible).

Catalog source (same as Thonny):
  https://raw.githubusercontent.com/thonny/thonny/master/data/
    micropython-variants-esptool.json
    micropython-variants-uf2.json

Binary URLs inside those JSON files point at micropython.org/resources/firmware/.
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

DEFAULT_DATA_PREFIX = "https://raw.githubusercontent.com/thonny/thonny/master/data"
ESPTOOL_JSON = "micropython-variants-esptool.json"
UF2_JSON = "micropython-variants-uf2.json"
# Official board.json (deploy_options.flash_offset, mcu, …) — same source as a
# MicroPython checkout's ports/<port>/boards/<board>/board.json.
MP_BOARD_JSON_PREFIX = (
    "https://raw.githubusercontent.com/micropython/micropython/master/ports"
)

CACHE_ROOT = Path.home() / ".mpftp" / "firmware-cache"
INDEX_TTL_SEC = 24 * 3600

# Map Thonny ``family`` → mpftp flasher port.
_FAMILY_TO_PORT = {
    "esp32": "esp32",
    "esp32s2": "esp32",
    "esp32s3": "esp32",
    "esp32c2": "esp32",
    "esp32c3": "esp32",
    "esp32c5": "esp32",
    "esp32c6": "esp32",
    "esp32h2": "esp32",
    "esp32p4": "esp32",
    "rp2": "rp2",
    "samd21": "samd",
    "samd51": "samd",
}

Fetcher = Callable[[str, float], str]


def cache_root() -> Path:
    CACHE_ROOT.mkdir(parents=True, exist_ok=True)
    (CACHE_ROOT / "index").mkdir(exist_ok=True)
    (CACHE_ROOT / "files").mkdir(exist_ok=True)
    return CACHE_ROOT


def default_fetch(url: str, timeout: float = 30.0) -> str:
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "mpftp/0.1 (MicroPython firmware download)"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="replace")


def board_id_from_info_url(info_url: str) -> str:
    path = urlparse(info_url or "").path.rstrip("/")
    return unquote(path.split("/")[-1]) if path else ""


def port_for_family(family: str) -> str:
    fam = (family or "").lower()
    return _FAMILY_TO_PORT.get(fam, fam or "unknown")


def normalize_variant(raw: dict[str, Any]) -> dict[str, Any]:
    """Normalize a Thonny variant entry for mpftp Target / download UI."""
    info_url = raw.get("info_url") or ""
    board = board_id_from_info_url(info_url)
    family = str(raw.get("family") or "")
    downloads = list(raw.get("downloads") or [])
    return {
        "board": board,
        "vendor": raw.get("vendor") or "",
        "model": raw.get("model") or "",
        "family": family,
        "port": port_for_family(family),
        "info_url": info_url,
        "downloads": downloads,
        "latest_prerelease_regex": raw.get("latest_prerelease_regex"),
        "popular": bool(raw.get("popular", False)),
        "title": raw.get("title") or raw.get("model") or board,
    }


def _index_path(name: str) -> Path:
    return cache_root() / "index" / name


def _board_json_cache_path(port: str, board: str) -> Path:
    safe_port = re.sub(r"[^\w.-]+", "_", port or "esp32")
    safe_board = re.sub(r"[^\w.-]+", "_", board or "unknown")
    return cache_root() / "board-json" / safe_port / f"{safe_board}.json"


def normalize_flash_offset(raw: Any) -> str:
    """Normalize board.json flash_offset values (``0``, ``0x0``, ``0x2000``)."""
    s = str(raw).strip()
    if not s:
        return "0x0"
    try:
        return hex(int(s, 0))
    except ValueError:
        return s if s.lower().startswith("0x") else f"0x{s}"


def board_json_url(board: str, port: str = "esp32") -> str:
    return f"{MP_BOARD_JSON_PREFIX.rstrip('/')}/{port}/boards/{board}/board.json"


def fetch_board_json(
    board: str,
    *,
    port: str = "esp32",
    fetch: Optional[Fetcher] = None,
    force: bool = False,
) -> Optional[dict[str, Any]]:
    """Fetch and cache ``board.json`` from the MicroPython GitHub tree."""
    if not board:
        return None
    fetch = fetch or default_fetch
    path = _board_json_cache_path(port, board)
    if not force:
        cached = _load_cached_json(path)
        if isinstance(cached, dict):
            return cached
    url = board_json_url(board, port)
    try:
        text = fetch(url)
        data = json.loads(text)
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    _save_cached_json(path, data)
    return data


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


def _load_cached_json(path: Path, ttl: float = INDEX_TTL_SEC) -> Optional[Any]:
    try:
        if not path.is_file():
            return None
        age = time.time() - path.stat().st_mtime
        if age > ttl:
            return None
        return json.loads(path.read_text("utf-8"))
    except Exception:
        return None


def _save_cached_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def fetch_variants_json(
    filename: str,
    *,
    data_prefix: str = DEFAULT_DATA_PREFIX,
    fetch: Optional[Fetcher] = None,
    force: bool = False,
) -> list[dict[str, Any]]:
    fetch = fetch or default_fetch
    path = _index_path(filename)
    if not force:
        cached = _load_cached_json(path)
        if isinstance(cached, list):
            return cached
    url = data_prefix.rstrip("/") + "/" + filename
    text = fetch(url)
    data = json.loads(text)
    if not isinstance(data, list):
        raise ValueError(f"unexpected variants JSON shape from {url}")
    _save_cached_json(path, data)
    return data


def load_catalog(
    *,
    data_prefix: str = DEFAULT_DATA_PREFIX,
    fetch: Optional[Fetcher] = None,
    force: bool = False,
) -> list[dict[str, Any]]:
    """Load and merge esptool + uf2 variant catalogs."""
    merged: list[dict[str, Any]] = []
    seen: set[str] = set()
    for filename in (ESPTOOL_JSON, UF2_JSON):
        for raw in fetch_variants_json(
            filename, data_prefix=data_prefix, fetch=fetch, force=force
        ):
            v = normalize_variant(raw)
            key = v["board"] or f"{v['vendor']}:{v['model']}:{v['family']}"
            if key in seen:
                continue
            seen.add(key)
            merged.append(v)
    merged.sort(
        key=lambda v: (
            v["port"],
            (v["vendor"] or "").lower(),
            (v["model"] or "").lower(),
            v["board"],
        )
    )
    return merged


def catalog_tree(
    catalog: Optional[list[dict[str, Any]]] = None,
    **load_kw: Any,
) -> dict[str, Any]:
    """Group catalog for UI: ports -> boards (flashable families only)."""
    if catalog is None:
        catalog = load_catalog(**load_kw)
    flashers = {"esp32": "esptool", "rp2": "uf2", "samd": "uf2"}
    ports: dict[str, dict[str, Any]] = {}
    for v in catalog:
        port = v["port"]
        if port not in ("esp32", "rp2", "samd"):
            continue
        bucket = ports.setdefault(
            port,
            {
                "port": port,
                "kind": "boards",
                "flashable": True,
                "flasher": flashers.get(port, ""),
                "boards": [],
            },
        )
        label = v["board"]
        if v.get("vendor") or v.get("model"):
            label = f"{v.get('vendor', '')} {v.get('model', '')}".strip() + f" ({v['board']})"
        bucket["boards"].append(
            {
                "board": v["board"],
                "vendor": v["vendor"],
                "model": v["model"],
                "family": v["family"],
                "title": v["title"],
                "label": label,
                "info_url": v["info_url"],
                "popular": v["popular"],
                "versions": [d.get("version") for d in v["downloads"] if d.get("version")],
            }
        )
    # Stable port order
    ordered = [ports[p] for p in ("esp32", "rp2", "samd") if p in ports]
    return {"ports": ordered, "source": "thonny-variants"}


def find_variant(
    board: str, catalog: Optional[list[dict[str, Any]]] = None, **load_kw: Any
) -> Optional[dict[str, Any]]:
    if catalog is None:
        catalog = load_catalog(**load_kw)
    board_u = (board or "").upper()
    for v in catalog:
        if (v.get("board") or "").upper() == board_u:
            return v
    return None


def _is_preview_version(version: str) -> bool:
    v = (version or "").lower()
    return "preview" in v or "pre" in v.split(".")[0:1]


def list_downloads(variant: dict[str, Any]) -> list[dict[str, str]]:
    """Downloads from the Thonny JSON entry (base board image only)."""
    out: list[dict[str, str]] = []
    for d in variant.get("downloads") or []:
        ver = str(d.get("version") or "")
        url = str(d.get("url") or "")
        if not ver or not url:
            continue
        out.append(
            {
                "version": ver.lstrip("v"),
                "url": url,
                "channel": "preview" if _is_preview_version(ver) else "release",
                "mp_variant": "",
            }
        )
    return out


def _fetch_board_html(info_url: str, fetch: Fetcher) -> str:
    try:
        return fetch(info_url if info_url.endswith("/") else info_url + "/")
    except Exception:
        return fetch(info_url)


def parse_board_page_firmware(board: str, html: str) -> dict[str, list[dict[str, str]]]:
    """Group firmware links from a micropython.org board page by MP variant.

    Filenames look like:
      ESP32_GENERIC_P4-20260406-v1.28.0.bin          → mp_variant ""
      ESP32_GENERIC_P4-C6_WIFI-20260406-v1.28.0.bin → mp_variant "C6_WIFI"
    """
    # Variant must start with a letter so the date (8 digits) is not captured as one.
    file_re = re.compile(
        rf"{re.escape(board)}"
        rf"(?:-([A-Za-z_][A-Za-z0-9_]*))?"
        rf"-(\d{{8}})-(v[\w.+-]+)\.(bin|uf2)",
        re.I,
    )
    by_variant: dict[str, list[dict[str, str]]] = {}
    seen: set[str] = set()
    for rel in re.findall(
        r'href="(/resources/firmware/[^"]+\.(?:bin|uf2))"', html, flags=re.I
    ):
        name = rel.rsplit("/", 1)[-1]
        m = file_re.fullmatch(name) or file_re.search(name)
        if not m:
            continue
        mp_var = m.group(1) or ""
        ver = m.group(3).lstrip("v")
        url = "https://micropython.org" + rel
        if url in seen:
            continue
        seen.add(url)
        by_variant.setdefault(mp_var, []).append(
            {
                "version": ver,
                "url": url,
                "channel": "preview" if _is_preview_version(ver) else "release",
                "mp_variant": mp_var,
            }
        )
    for key in by_variant:
        by_variant[key] = _sort_download_list(by_variant[key])
    return by_variant


def _sort_download_list(items: list[dict[str, str]]) -> list[dict[str, str]]:
    releases = [d for d in items if d["channel"] == "release"]
    previews = [d for d in items if d["channel"] == "preview"]
    releases.sort(key=lambda d: d["version"], reverse=True)
    previews.sort(key=lambda d: d["version"], reverse=True)
    return releases + previews


def enrich_downloads_from_page(
    variant: dict[str, Any],
    *,
    fetch: Optional[Fetcher] = None,
) -> dict[str, list[dict[str, str]]]:
    """Scrape the board page for all MP-variant firmware; fall back to JSON."""
    fetch = fetch or default_fetch
    json_default = list_downloads(variant)
    info_url = variant.get("info_url") or ""
    board = variant.get("board") or ""
    if info_url and board:
        try:
            html = _fetch_board_html(info_url, fetch)
            parsed = parse_board_page_firmware(board, html)
            if parsed:
                # Merge Thonny JSON entries for the default image when the page
                # scrape is incomplete (tests / transient HTML).
                seen = {d["url"] for d in parsed.get("", [])}
                for d in json_default:
                    if d["url"] not in seen:
                        parsed.setdefault("", []).append(d)
                for key in list(parsed.keys()):
                    parsed[key] = _sort_download_list(parsed[key])
                return parsed
        except Exception:
            pass
    return {"": json_default}


def pick_download(
    variant: dict[str, Any],
    *,
    version: Optional[str] = None,
    preview: bool = False,
    mp_variant: str = "",
    fetch: Optional[Fetcher] = None,
) -> dict[str, str]:
    """Choose a download entry for board + MP variant (e.g. C6_WIFI)."""
    by_var = enrich_downloads_from_page(variant, fetch=fetch)
    mp_variant = mp_variant or ""
    if mp_variant not in by_var:
        known = ", ".join(sorted(by_var.keys()) or ["(none)"])
        raise RuntimeError(
            f"variant {mp_variant or '(default)'} not available for "
            f"{variant.get('board')}; have: {known}"
        )
    downloads = by_var[mp_variant]
    if preview:
        for d in downloads:
            if d["channel"] == "preview":
                return d
        # Fall back to Thonny regex tweak filtered by variant name in URL.
        patched = maybe_latest_preview(
            variant, fetch=fetch, mp_variant=mp_variant, by_variant=by_var
        )
        if patched:
            return patched
        raise RuntimeError(
            f"no preview build for {variant.get('board')}"
            + (f" / {mp_variant}" if mp_variant else "")
        )
    if version:
        ver = version.lstrip("v")
        for d in downloads:
            if d["version"].lstrip("v") == ver:
                return d
        raise RuntimeError(
            f"version {version} not found for {variant.get('board')}"
            + (f" / {mp_variant}" if mp_variant else "")
        )
    for d in downloads:
        if d["channel"] == "release":
            return d
    if downloads:
        return downloads[0]
    raise RuntimeError(f"no downloads for {variant.get('board')}")


def maybe_latest_preview(
    variant: dict[str, Any],
    *,
    fetch: Optional[Fetcher] = None,
    mp_variant: str = "",
    by_variant: Optional[dict[str, list[dict[str, str]]]] = None,
) -> Optional[dict[str, str]]:
    """Pick latest preview for an MP variant from scraped page data."""
    if by_variant is None:
        by_variant = enrich_downloads_from_page(variant, fetch=fetch)
    for d in by_variant.get(mp_variant or "", []):
        if d["channel"] == "preview":
            return d
    # Thonny regex fallback (base image only).
    regex_s = variant.get("latest_prerelease_regex")
    info_url = variant.get("info_url") or ""
    if not regex_s or not info_url:
        return None
    fetch = fetch or default_fetch
    try:
        html = _fetch_board_html(info_url, fetch)
    except Exception:
        return None
    try:
        rx = re.compile(regex_s)
    except re.error:
        return None
    urls = re.findall(
        r'href="(/resources/firmware/[^"]+\.(?:bin|uf2))"', html, flags=re.I
    )
    for rel in urls:
        name = rel.rsplit("/", 1)[-1]
        if mp_variant and mp_variant not in name:
            continue
        if mp_variant == "" and re.search(
            rf"{re.escape(variant.get('board') or '')}-[A-Za-z_]", name
        ):
            # Skip variant-tagged files when asking for the default image.
            continue
        m = rx.search(name)
        if not m:
            continue
        ver_m = re.search(r"(v?\d+\.\d+\.\d+-preview\.\d+\.[a-z0-9]+)", name, re.I)
        version = ver_m.group(1) if ver_m else m.group(0)
        return {
            "version": version.lstrip("v"),
            "url": "https://micropython.org" + rel,
            "channel": "preview",
            "mp_variant": mp_variant,
        }
    return None


def download_file(
    url: str,
    *,
    dest_dir: Optional[Path] = None,
    progress: Optional[Callable[[int, int], None]] = None,
) -> Path:
    """Download URL into firmware-cache/files; skip if same-sized file exists."""
    dest_dir = dest_dir or (cache_root() / "files")
    dest_dir.mkdir(parents=True, exist_ok=True)
    name = unquote(urlparse(url).path.rsplit("/", 1)[-1]) or "firmware.bin"
    dest = dest_dir / name

    req = urllib.request.Request(
        url, headers={"User-Agent": "mpftp/0.1 (MicroPython firmware download)"}
    )
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
        tmp.replace(dest)
    return dest


def download_board(
    board: str,
    *,
    version: Optional[str] = None,
    preview: bool = False,
    mp_variant: str = "",
    data_prefix: str = DEFAULT_DATA_PREFIX,
    fetch: Optional[Fetcher] = None,
    catalog: Optional[list[dict[str, Any]]] = None,
) -> dict[str, Any]:
    """Resolve board + MP variant + version, download, return artifact-shaped dict."""
    variant = find_variant(board, catalog=catalog, data_prefix=data_prefix, fetch=fetch)
    if not variant:
        raise RuntimeError(f"board not in download catalog: {board}")
    chosen = pick_download(
        variant,
        version=version,
        preview=preview,
        mp_variant=mp_variant or "",
        fetch=fetch,
    )
    path = download_file(chosen["url"])
    st = path.stat()
    flash_offset = resolve_remote_flash_offset(
        variant["board"], port=variant["port"] or "esp32", fetch=fetch
    )
    out: dict[str, Any] = {
        "ready": True,
        "artifact": str(path),
        "size": st.st_size,
        "mtime": st.st_mtime,
        "source": "download",
        "board": variant["board"],
        "variant": mp_variant or "",
        "version": chosen["version"],
        "family": variant["family"],
        "port": variant["port"],
        "url": chosen["url"],
        "info_url": variant["info_url"],
        "vendor": variant["vendor"],
        "model": variant["model"],
    }
    if flash_offset:
        out["flashOffset"] = flash_offset
    return out


def list_board(
    board: str,
    *,
    mp_variant: str = "",
    data_prefix: str = DEFAULT_DATA_PREFIX,
    fetch: Optional[Fetcher] = None,
    catalog: Optional[list[dict[str, Any]]] = None,
    include_preview_probe: bool = False,
    force: bool = False,
) -> dict[str, Any]:
    variant = find_variant(
        board, catalog=catalog, data_prefix=data_prefix, fetch=fetch, force=force
    )
    if not variant:
        raise RuntimeError(f"board not in download catalog: {board}")
    by_var = enrich_downloads_from_page(variant, fetch=fetch)
    # UI "variants" are named MP builds (C6_WIFI, …); default "" is separate.
    variants = sorted(k for k in by_var if k)
    mp_variant = mp_variant or ""
    if mp_variant and mp_variant not in by_var:
        downloads: list[dict[str, str]] = []
    else:
        downloads = list(by_var.get(mp_variant, by_var.get("", [])))
    preview = None
    if include_preview_probe:
        preview = maybe_latest_preview(
            variant, fetch=fetch, mp_variant=mp_variant, by_variant=by_var
        )
    flash_offset = resolve_remote_flash_offset(
        variant["board"],
        port=variant["port"] or "esp32",
        fetch=fetch,
        force=force,
    )
    out: dict[str, Any] = {
        "board": variant["board"],
        "vendor": variant["vendor"],
        "model": variant["model"],
        "family": variant["family"],
        "port": variant["port"],
        "info_url": variant["info_url"],
        "variants": variants,
        "variant": mp_variant,
        "downloads": downloads,
        "preview": preview,
    }
    if flash_offset:
        out["flashOffset"] = flash_offset
    return out
