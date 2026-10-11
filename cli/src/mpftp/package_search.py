"""Find a package without knowing its exact name.

One search across the three places a board's packages come from:

* micropython-lib, through the index ``mip`` itself reads
  (``https://micropython.org/pi/v2/index.json``);
* the PyDevices index (``https://PyDevices.github.io/mip/index.json``),
  which carries the PyDevices packages and a mirror of micropython-lib so
  their named dependencies resolve from one place;
* the two CircuitPython bundles ``circup`` installs from (Adafruit's and the
  community bundle), through the JSON manifest each release attaches.

Each result says where it comes from, its version, a one-line description
where the index has one, and what to run (``install.command``) or call
(``install.method`` with ``install.params``) to install it. A PyDevices
package needs ``index=https://PyDevices.github.io/mip``, and its install
line carries that.

Fetched indexes are cached under ``~/.mpftp/package-cache`` for
``INDEX_TTL_SEC``; when the network is away, a stale copy is used and the
result says so (``offline`` and ``note``).
"""

from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable, Optional

MIP_INDEX = "https://micropython.org/pi/v2"
PYDEVICES_INDEX = "https://PyDevices.github.io/mip"

CACHE_ROOT = Path.home() / ".mpftp" / "package-cache"
INDEX_TTL_SEC = 6 * 3600
USER_AGENT = "mpftp"

# key, label, interpreter, how to fetch. Order here is the tie-break order in
# the results: a name that exists in two places lists micropython-lib first.
SOURCES: list[dict[str, str]] = [
    {
        "key": "micropython-lib",
        "label": "micropython-lib",
        "interpreter": "micropython",
        "kind": "mip",
        "url": MIP_INDEX + "/index.json",
    },
    {
        "key": "pydevices",
        "label": "PyDevices",
        "interpreter": "micropython",
        "kind": "mip",
        "url": PYDEVICES_INDEX + "/index.json",
    },
    {
        "key": "adafruit",
        "label": "Adafruit CircuitPython Bundle",
        "interpreter": "circuitpython",
        "kind": "bundle",
        "repo": "adafruit/Adafruit_CircuitPython_Bundle",
        "asset": "adafruit-circuitpython-bundle",
    },
    {
        "key": "community",
        "label": "CircuitPython Community Bundle",
        "interpreter": "circuitpython",
        "kind": "bundle",
        "repo": "adafruit/CircuitPython_Community_Bundle",
        "asset": "circuitpython-community-bundle",
    },
]
SOURCE_KEYS = [s["key"] for s in SOURCES]

Fetcher = Callable[[str], str]


# --------------------------------------------------------------------------- #
# Network
# --------------------------------------------------------------------------- #


def default_fetch(url: str, timeout: float = 30.0) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="replace")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D401
        return None


def default_latest_tag(releases_latest_url: str, timeout: float = 30.0) -> str:
    """The tag ``<repo>/releases/latest`` redirects to, read from the redirect.

    This is how circup finds a bundle's newest release: one HEAD, no API
    token, no rate limit to speak of.
    """
    opener = urllib.request.build_opener(_NoRedirect)
    req = urllib.request.Request(
        releases_latest_url, method="HEAD", headers={"User-Agent": USER_AGENT}
    )
    try:
        with opener.open(req, timeout=timeout) as resp:
            location = resp.headers.get("Location") or resp.geturl()
    except urllib.error.HTTPError as e:
        if e.code not in (301, 302, 303, 307, 308):
            raise
        location = e.headers.get("Location") or ""
    tag = location.rstrip("/").rsplit("/", 1)[-1]
    if not tag or tag == "latest":
        raise RuntimeError(f"{releases_latest_url} did not say which release is latest")
    return tag


def bundle_manifest_url(source: dict[str, str], tag: str) -> str:
    return (
        f"https://github.com/{source['repo']}/releases/download/{tag}/"
        f"{source['asset']}-{tag}.json"
    )


# --------------------------------------------------------------------------- #
# Parsing: one index text -> entries in the result shape
# --------------------------------------------------------------------------- #


def _install_for(source: dict[str, str], name: str) -> dict[str, Any]:
    if source["kind"] == "bundle":
        return {
            "command": f"mpftp circup {name}",
            "method": "circup_install",
            "params": {"packages": [name]},
        }
    if source["key"] == "pydevices":
        return {
            "command": f"mpftp mip {name} --index {PYDEVICES_INDEX}",
            "method": "mip_install",
            "params": {"packages": [name], "index": PYDEVICES_INDEX},
        }
    return {
        "command": f"mpftp mip {name}",
        "method": "mip_install",
        "params": {"packages": [name]},
    }


def _entry(source: dict[str, str], name: str, version: str, description: str, **extra) -> dict:
    e: dict[str, Any] = {
        "name": name,
        "source": source["key"],
        "source_label": source["label"],
        "interpreter": source["interpreter"],
        "version": version or "",
        "description": (description or "").strip(),
    }
    e.update({k: v for k, v in extra.items() if v})
    e["install"] = _install_for(source, name)
    return e


def parse_mip_index(text: str, source: dict[str, str]) -> list[dict]:
    """``index.json`` as mip reads it: ``{"v": 2, "packages": [{name, version, description, ...}]}``."""
    data = json.loads(text)
    packages = data.get("packages") if isinstance(data, dict) else None
    if not isinstance(packages, list):
        raise ValueError(f"{source['label']}: index has no package list")
    entries = []
    for raw in packages:
        if not isinstance(raw, dict) or not raw.get("name"):
            continue
        entries.append(
            _entry(
                source,
                str(raw["name"]),
                str(raw.get("version") or ""),
                str(raw.get("description") or ""),
                author=str(raw.get("author") or ""),
                license=str(raw.get("license") or ""),
                path=str(raw.get("path") or ""),
            )
        )
    return entries


def parse_bundle_index(text: str, source: dict[str, str], tag: str = "") -> list[dict]:
    """A bundle release's manifest: ``{module_name: {version, pypi_description, repo, ...}}``."""
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError(f"{source['label']}: manifest is not a module table")
    entries = []
    for name, raw in data.items():
        if not isinstance(raw, dict):
            continue
        entries.append(
            _entry(
                source,
                str(name),
                str(raw.get("version") or ""),
                str(raw.get("pypi_description") or ""),
                pypi_name=str(raw.get("pypi_name") or ""),
                repo=str(raw.get("repo") or ""),
                bundle_tag=tag,
                dependencies=list(raw.get("dependencies") or []),
            )
        )
    return entries


# --------------------------------------------------------------------------- #
# Cache
# --------------------------------------------------------------------------- #


def cache_path(key: str, root: Optional[Path] = None) -> Path:
    return (root or CACHE_ROOT) / f"{key}.json"


def _read_cache(path: Path) -> Optional[dict]:
    try:
        data = json.loads(path.read_text("utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("packages"), list):
        return None
    return data


def _write_cache(path: Path, data: dict) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data), encoding="utf-8")
    except OSError:
        pass


def _age_text(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds < 90:
        return f"{seconds} s"
    if seconds < 90 * 60:
        return f"{seconds // 60} min"
    if seconds < 36 * 3600:
        return f"{seconds // 3600} h"
    return f"{seconds // 86400} d"


def _fetch_source(
    source: dict[str, str], fetch: Fetcher, latest_tag: Callable[[str], str]
) -> dict:
    if source["kind"] == "mip":
        text = fetch(source["url"])
        return {"packages": parse_mip_index(text, source), "url": source["url"]}
    tag = latest_tag(f"https://github.com/{source['repo']}/releases/latest")
    url = bundle_manifest_url(source, tag)
    text = fetch(url)
    return {"packages": parse_bundle_index(text, source, tag), "url": url, "tag": tag}


def load_source(
    source: dict[str, str],
    *,
    refresh: bool = False,
    ttl: float = INDEX_TTL_SEC,
    fetch: Optional[Fetcher] = None,
    latest_tag: Optional[Callable[[str], str]] = None,
    cache_root: Optional[Path] = None,
    now: Optional[float] = None,
) -> tuple[list[dict], dict[str, Any]]:
    """One source's entries and a status line: where they came from and how old they are.

    A fresh cache is used as is. Otherwise the index is fetched; when that
    fails, a stale cache stands in and the status says ``offline``. With no
    cache at all the status carries the error and the entries are empty.
    """
    now = time.time() if now is None else now
    path = cache_path(source["key"], cache_root)
    cached = _read_cache(path)
    age = (now - float(cached.get("fetched", 0))) if cached else None
    status: dict[str, Any] = {
        "source": source["key"],
        "label": source["label"],
        "from": "cache",
        "age_sec": age,
        "offline": False,
        "error": None,
        "count": 0,
    }
    if cached and not refresh and age is not None and age <= ttl:
        status["count"] = len(cached["packages"])
        status["tag"] = cached.get("tag")
        return list(cached["packages"]), status
    try:
        data = _fetch_source(source, fetch or default_fetch, latest_tag or default_latest_tag)
    except Exception as e:
        err = f"{type(e).__name__}: {e}" if not str(e).startswith(type(e).__name__) else str(e)
        if cached:
            status.update(offline=True, error=err, count=len(cached["packages"]))
            status["tag"] = cached.get("tag")
            return list(cached["packages"]), status
        status.update(offline=True, error=err)
        return [], status
    data["fetched"] = now
    _write_cache(path, data)
    status.update({"from": "network", "age_sec": 0.0, "count": len(data["packages"])})
    if data.get("tag"):
        status["tag"] = data["tag"]
    return list(data["packages"]), status


# --------------------------------------------------------------------------- #
# Ranking
# --------------------------------------------------------------------------- #


def _norm(text: str) -> str:
    """Case, ``_``/``-`` and spacing don't matter when a name is typed from memory."""
    return re.sub(r"[\s_]+", "-", (text or "").strip().lower())


def _words(query: Any) -> list[str]:
    if isinstance(query, str):
        raw = query.split()
    else:
        raw = [w for part in (query or []) for w in str(part).split()]
    return [_norm(w) for w in raw if _norm(w)]


def match_tier(entry: dict, words: list[str]) -> Optional[int]:
    """How well an entry matches, or None when it doesn't.

    0 the name is the query; 1 the name starts with it; 2 the name contains
    it; 3 every word is in the name; 4 every word is somewhere in the name,
    PyPI name or description.
    """
    if not words:
        return None
    name = _norm(entry.get("name", ""))
    query = "-".join(words)
    if name == query:
        return 0
    if name.startswith(query):
        return 1
    if query in name:
        return 2
    if all(w in name for w in words):
        return 3
    hay = " ".join(
        (name, _norm(entry.get("pypi_name", "")), _norm(entry.get("description", "")))
    )
    if all(w in hay for w in words):
        return 4
    return None


def rank(entries: list[dict], query: Any) -> list[dict]:
    """The entries that match every word, best first, each with its ``match`` tier."""
    words = _words(query)
    order = {k: i for i, k in enumerate(SOURCE_KEYS)}
    hits = []
    for e in entries:
        tier = match_tier(e, words)
        if tier is None:
            continue
        hit = dict(e)
        hit["match"] = tier
        hits.append(hit)
    hits.sort(key=lambda e: (e["match"], _norm(e["name"]), order.get(e["source"], 99)))
    return hits


def drop_mirrors(entries: list[dict]) -> list[dict]:
    """The PyDevices index mirrors micropython-lib; list a mirrored package once.

    A PyDevices entry whose name and version match a micropython-lib entry is
    the same package and installs from micropython-lib's own index.
    """
    upstream = {
        (e["name"], e["version"]) for e in entries if e.get("source") == "micropython-lib"
    }
    return [
        e
        for e in entries
        if not (e.get("source") == "pydevices" and (e["name"], e["version"]) in upstream)
    ]


# --------------------------------------------------------------------------- #
# The search
# --------------------------------------------------------------------------- #


def sources_for(
    keys: Optional[list[str]] = None, interpreter: Optional[str] = None
) -> list[dict[str, str]]:
    chosen = list(SOURCES)
    if keys:
        unknown = sorted(set(keys) - set(SOURCE_KEYS))
        if unknown:
            raise ValueError(
                f"unknown source {', '.join(unknown)}; one of {', '.join(SOURCE_KEYS)}"
            )
        chosen = [s for s in chosen if s["key"] in keys]
    if interpreter:
        if interpreter not in ("micropython", "circuitpython"):
            raise ValueError(f"interpreter must be micropython or circuitpython, not {interpreter!r}")
        chosen = [s for s in chosen if s["interpreter"] == interpreter]
    return chosen


def search(
    query: Any,
    *,
    sources: Optional[list[str]] = None,
    interpreter: Optional[str] = None,
    limit: Optional[int] = 25,
    refresh: bool = False,
    fetch: Optional[Fetcher] = None,
    latest_tag: Optional[Callable[[str], str]] = None,
    cache_root: Optional[Path] = None,
    ttl: float = INDEX_TTL_SEC,
) -> dict[str, Any]:
    """Search the indexes for ``query`` (a string or a list of words).

    Returns ``{"query", "results", "total", "sources", "offline", "note"}``.
    ``results`` is ranked best first and cut to ``limit``; ``total`` is the
    count before the cut. ``sources`` has one status per index asked.
    """
    words = _words(query)
    if not words:
        raise ValueError("say what to search for, e.g. `mpftp search display text`")
    entries: list[dict] = []
    statuses: list[dict] = []
    for src in sources_for(sources, interpreter):
        got, status = load_source(
            src,
            refresh=refresh,
            ttl=ttl,
            fetch=fetch,
            latest_tag=latest_tag,
            cache_root=cache_root,
        )
        entries.extend(got)
        statuses.append(status)
    hits = rank(drop_mirrors(entries), words)
    total = len(hits)
    if limit is not None and limit >= 0:
        hits = hits[:limit]
    note = _offline_note(statuses)
    return {
        "query": " ".join(words),
        "results": hits,
        "total": total,
        "sources": statuses,
        "offline": any(s["offline"] for s in statuses),
        "note": note,
    }


def _offline_note(statuses: list[dict]) -> Optional[str]:
    stale = [s for s in statuses if s["offline"] and s["count"]]
    missing = [s for s in statuses if s["offline"] and not s["count"]]
    parts = []
    if stale:
        ages = ", ".join(f"{s['label']} ({_age_text(s['age_sec'] or 0)} old)" for s in stale)
        parts.append(f"Offline: searched the cached copy of {ages}.")
    if missing:
        names = ", ".join(s["label"] for s in missing)
        parts.append(f"Could not fetch {names} and there is no cached copy yet, so it was not searched.")
    if not parts:
        return None
    first_error = next((s["error"] for s in statuses if s["error"]), None)
    if first_error:
        parts.append(f"Last error: {first_error}")
    return " ".join(parts)


def format_text(result: dict[str, Any]) -> str:
    """One line per result, for a person at a terminal."""
    lines = []
    for r in result["results"]:
        version = f" {r['version']}" if r.get("version") else ""
        desc = f" — {r['description']}" if r.get("description") else ""
        lines.append(f"{r['name']}{version}  [{r['source_label']}]{desc}")
        lines.append(f"    {r['install']['command']}")
    shown = len(result["results"])
    if not shown:
        lines.append(f"Nothing matches {result['query']!r}.")
    elif result["total"] > shown:
        lines.append(f"{shown} of {result['total']} matches; --limit shows more.")
    if result.get("note"):
        lines.append(result["note"])
    return "\n".join(lines)
