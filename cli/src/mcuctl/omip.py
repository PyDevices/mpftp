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
OMIP_FILE = "github:PyDevices/omnipython/lib/omip.py"

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
class Root:
    """A package asked for, or one that names its own folder; its files land in ``folder``."""

    folder: Optional[str] = None  # None: the plan's library folder


@dataclass
class PlannedFile:
    path: str  # relative to the folder of its root
    url: str
    short_hash: Optional[str] = None  # set when the file comes from an index
    root: Root = field(default_factory=Root)


@dataclass
class Plan:
    """What `walk` found for one package, laid out as ``omip.install`` on a board would.

    ``target`` is the folder given to install into. A package that names its
    own folder under ``target`` in its JSON, as an app does, lands beside it
    when it is a ``lib`` folder and inside it otherwise, and what it needs goes
    in a ``lib`` folder: `dest` says where each file goes. The rule is the
    board's, in omnipython's ``lib/omip.py``.
    """

    channel: str  # "py", or the board's .mpy version
    target: str = "/lib"
    files: list[PlannedFile] = field(default_factory=list)
    seen: set[str] = field(default_factory=set)
    libs: str = ""
    roots: list[Root] = field(default_factory=list)

    def __post_init__(self):
        self.target = self.target.rstrip("/") or "/"
        self.libs = self.libs or self.target

    def dest(self, file: PlannedFile) -> str:
        """Where ``file`` goes on the board."""
        return join(file.root.folder or self.libs, file.path)


def join(folder: str, path: str) -> str:
    return folder.rstrip("/") + "/" + path


def beside(target: str, folder: str) -> str:
    """Where a package's own folder is: beside a target named ``lib``, inside any other."""
    if folder.startswith("/"):
        return folder.rstrip("/")
    folder = folder.strip("/")
    head, slash, last = target.rpartition("/")
    if last == "lib":
        return head + slash + folder
    return target.rstrip("/") + "/" + folder


def libs_for_app(target: str) -> str:
    """The library folder for what an app needs: ``target`` when it is a ``lib`` folder, else inside it."""
    if target.rpartition("/")[2] == "lib":
        return target
    return target.rstrip("/") + "/lib"


def fetch(url: str) -> Optional[bytes]:
    """The bytes at ``url``, or None when the server says it isn't there."""
    # Some hosts refuse urllib's own user agent.
    request = urllib.request.Request(url, headers={"User-Agent": "mcuctl"})
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


def walk(plan: Plan, spec: str, version: Optional[str], indexes: list[str], owner: Optional[Root] = None) -> None:
    """Add the files of ``spec``, and of everything it needs, to ``plan``.

    ``owner`` is the root whose folder ``spec``'s files land in; with none,
    ``spec`` is a root of its own.
    """
    if spec in plan.seen:
        return
    plan.seen.add(spec)
    entry = Root()
    if owner is None:
        owner = entry
        plan.roots.append(entry)
    source = indexes[0]
    base = None
    if _is_url(spec):
        url = _rewrite_url(spec, version)
        if spec.endswith((".py", ".mpy")):
            plan.files.append(PlannedFile(spec.rsplit("/", 1)[-1], url, root=owner))
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
    folder = body.get("target")
    if folder:
        # Its own folder: what it needs is a root of its own, bound for the library folder.
        if entry is not owner:
            plan.roots.append(entry)
            owner = entry
        entry.folder = beside(plan.target, folder)
        if entry is plan.roots[0]:
            plan.libs = libs_for_app(plan.target)
    for path, short_hash in body.get("hashes", ()):
        plan.files.append(PlannedFile(path, f"{source}/file/{short_hash[:2]}/{short_hash}", short_hash, owner))
    for path, url in body.get("urls", ()):
        if base and not _is_url(url):
            url = f"{base}/{url[2:] if url.startswith('./') else url}"
        plan.files.append(PlannedFile(path, _rewrite_url(url, version), root=owner))
    for dep, dep_version in body.get("deps", ()):
        walk(plan, dep, dep_version, indexes, None if folder else owner)
    for dep, dep_version in body.get("upstream", ()):
        walk(plan, dep, dep_version, [UPSTREAM])
