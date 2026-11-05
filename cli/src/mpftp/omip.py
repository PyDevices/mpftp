# SPDX-FileCopyrightText: 2026 Brad Barnett
#
# SPDX-License-Identifier: MIT

"""Work out what an OmniPython package needs, from the computer.

A board with no network can't run ``omip`` on itself, so the host does the same
walk and copies the files. A package is looked for on the OmniPython index and
then on micropython-lib, and whatever it names under ``upstream`` in its JSON
comes from micropython-lib. ``mip`` and mpremote ignore that key.

Nothing here touches a board: `walk` returns the files, and the sidecar copies
them.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Optional

INDEX = "https://lib.omnipython.org"
UPSTREAM = "https://micropython.org/pi/v2"
#: The one file that gives a board ``omip.install``, the bootstrap ``mip`` uses too.
OMIP_FILE = "github:PyDevices/pydevices/lib/omip.py"

_URL_PREFIXES = ("http://", "https://", "github:", "gitlab:", "codeberg:")
_HOSTS = {
    "codeberg:": "codeberg.org/api/v1/repos/{}/{}/raw/{p}?ref={}",
    "github:": "raw.githubusercontent.com/{}/{}/{}/{p}",
    "gitlab:": "gitlab.com/{}/{}/-/raw/{}/{p}",
}
_TIMEOUT = 30


class OmipError(RuntimeError):
    """A package or a file it needs can't be had."""


@dataclass
class PlannedFile:
    path: str  # relative to the install target
    url: str
    short_hash: Optional[str] = None  # set when the file comes from an index


@dataclass
class Plan:
    channel: str  # "py", or the board's .mpy version
    files: list[PlannedFile] = field(default_factory=list)
    seen: set[str] = field(default_factory=set)


def fetch(url: str) -> Optional[bytes]:
    """The bytes at ``url``, or None when the server says it isn't there."""
    # Some hosts refuse urllib's own user agent.
    request = urllib.request.Request(url, headers={"User-Agent": "mpftp"})
    try:
        with urllib.request.urlopen(request, timeout=_TIMEOUT) as response:
            return response.read()
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise OmipError(f"HTTP {e.code} from {url}") from e
    except (urllib.error.URLError, OSError) as e:
        raise OmipError(f"Can't reach {url}: {getattr(e, 'reason', e)}") from e


def _is_url(spec: str) -> bool:
    return spec.startswith(_URL_PREFIXES)


def _rewrite_url(url: str, version: Optional[str]) -> str:
    for host, form in _HOSTS.items():
        if url.startswith(host):
            parts = url[len(host) :].split("/")
            return "https://" + form.format(parts[0], parts[1], version or "HEAD", p="/".join(parts[2:]))
    return url


def _read_json(url: str) -> Optional[dict]:
    body = fetch(url)
    if body is None:
        return None
    try:
        return json.loads(body)
    except ValueError as e:
        raise OmipError(f"Not JSON at {url}: {e}") from e


def walk(plan: Plan, spec: str, version: Optional[str], indexes: list[str]) -> None:
    """Add the files of ``spec``, and of everything it needs, to ``plan``."""
    if spec in plan.seen:
        return
    plan.seen.add(spec)
    source = indexes[0]
    base = None
    if _is_url(spec):
        url = _rewrite_url(spec, version)
        if spec.endswith((".py", ".mpy")):
            plan.files.append(PlannedFile(spec.rsplit("/", 1)[-1], url))
            return
        if not spec.endswith(".json"):
            url = url.rstrip("/") + "/package.json"
        body = _read_json(url)
        if body is None:
            raise OmipError(f"Package not found: {url}")
        base = url.rpartition("/")[0]
    else:
        for index in indexes:
            body = _read_json(f"{index}/package/{plan.channel}/{spec}/{version or 'latest'}.json")
            if body is not None:
                source = index
                break
        else:
            raise OmipError(f"Package not found: {spec} (looked on {', '.join(indexes)})")
        indexes = [source]
    for path, short_hash in body.get("hashes", ()):
        plan.files.append(PlannedFile(path, f"{source}/file/{short_hash[:2]}/{short_hash}", short_hash))
    for path, url in body.get("urls", ()):
        if base and not _is_url(url):
            url = f"{base}/{url}"
        plan.files.append(PlannedFile(path, _rewrite_url(url, version)))
    for dep, dep_version in body.get("deps", ()):
        walk(plan, dep, dep_version, indexes)
    for dep, dep_version in body.get("upstream", ()):
        walk(plan, dep, dep_version, [UPSTREAM])
