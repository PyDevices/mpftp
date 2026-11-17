# SPDX-FileCopyrightText: 2026 Brad Barnett
#
# SPDX-License-Identifier: MIT

"""``mcuctl omip``: the host walks a package the way omip does on a board.

The two indexes are real HTTP servers and the install runs through the real
session and mpremote's own helpers. Only the board is a stand-in, a folder.
"""

from __future__ import annotations

import argparse
import hashlib
import http.server
import io
import json
import tempfile
import threading
import unittest
from contextlib import redirect_stdout
from functools import partial
from pathlib import Path
from unittest import mock

from mcuctl import omip
from mcuctl.cli import build_parser, cmd_omip
from test_helpers import _load_sidecar


class Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


def serve(root: Path):
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), partial(Quiet, directory=str(root)))
    threading.Thread(target=server.serve_forever, args=(0.02,), daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}"


def add_package(root: Path, channel: str, name: str, files: dict[str, bytes], **keys) -> None:
    """Write a package the way an index lays it out: JSON by name, files by hash."""
    hashes = []
    for path, data in files.items():
        short = hashlib.sha256(data).hexdigest()[:8]
        blob = root / "file" / short[:2] / short
        blob.parent.mkdir(parents=True, exist_ok=True)
        blob.write_bytes(data)
        hashes.append([path, short])
    pkg = root / "package" / channel / name / "latest.json"
    pkg.parent.mkdir(parents=True, exist_ok=True)
    pkg.write_text(json.dumps({"v": 1, "hashes": hashes, "version": "1.0", **keys}))


class FolderBoard:
    """The parts of an mpremote transport an install uses, on a folder."""

    in_raw_repl = True

    def __init__(self, root: Path, mpy=6):
        self.root = root
        self.mpy = mpy
        self.writes: list[str] = []

    def _at(self, path: str) -> Path:
        return self.root / path.lstrip("/")

    def exec(self, code, **kwargs):
        return b""

    def eval(self, expr, **kwargs):
        return self.mpy

    def fs_exists(self, path):
        return self._at(path).exists()

    def fs_mkdir(self, path):
        self._at(path).mkdir()

    def fs_hashfile(self, path, algo):
        if not self._at(path).exists():
            raise FileNotFoundError(path)
        return hashlib.sha256(self._at(path).read_bytes()).digest()

    def fs_writefile(self, path, data, chunk_size=256, progress_callback=None):
        self.writes.append(path)
        self._at(path).write_bytes(data)


class Base(unittest.TestCase):
    def setUp(self):
        self.mod = _load_sidecar()
        quiet = mock.patch.object(self.mod, "_notify")
        quiet.start()
        self.addCleanup(quiet.stop)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        self.ours_root = root / "ours"
        self.up_root = root / "upstream"
        self.other_root = root / "other"
        self.web_root = root / "web"
        for folder in (self.ours_root, self.up_root, self.other_root, self.web_root):
            folder.mkdir()
        self.board_root = root / "board"
        self.board_root.mkdir()

        add_package(
            self.ours_root, "6", "ourble", {"ourble/__init__.mpy": b"ours"},
            deps=[["ourhelper", "latest"]], upstream=[["aioble", "latest"]],
        )
        add_package(self.ours_root, "6", "ourhelper", {"ourhelper.mpy": b"helper"})
        add_package(self.ours_root, "py", "ourble", {"ourble/__init__.py": b"ours source"})
        add_package(self.up_root, "6", "aioble", {"aioble/core.mpy": b"aioble"}, deps=[["aioble-core", "latest"]])
        add_package(self.up_root, "6", "aioble-core", {"aioble/device.mpy": b"device"})
        add_package(self.up_root, "6", "fnmatch", {"fnmatch.mpy": b"from upstream"})
        add_package(self.up_root, "6", "both", {"both.mpy": b"from upstream"})
        add_package(self.ours_root, "6", "both", {"both.mpy": b"from ours"})
        add_package(self.other_root, "6", "ourble", {"other.mpy": b"other"}, upstream=[["aioble", "latest"]])
        (self.web_root / "omip.py").write_bytes(b"# omip\n")

        self.urls = {}
        for key, folder in (
            ("ours", self.ours_root),
            ("up", self.up_root),
            ("other", self.other_root),
            ("web", self.web_root),
        ):
            server, self.urls[key] = serve(folder)
            self.addCleanup(server.server_close)
            self.addCleanup(server.shutdown)
        for name, value in (
            ("INDEX", self.urls["ours"]),
            ("UPSTREAM", self.urls["up"]),
            ("OMIP_FILE", self.urls["web"] + "/omip.py"),
        ):
            patch = mock.patch.object(omip, name, value)
            patch.start()
            self.addCleanup(patch.stop)

        self.board = FolderBoard(self.board_root)
        self.session = self.mod.Session()
        self.session.interpreter = "micropython"
        self.session.transport = self.board

    def on_board(self) -> dict[str, bytes]:
        return {
            "/" + p.relative_to(self.board_root).as_posix(): p.read_bytes()
            for p in self.board_root.rglob("*")
            if p.is_file()
        }


class HostWalk(Base):
    def test_a_package_brings_its_deps_and_what_it_names_upstream(self):
        result = self.session.omip_install(["ourble"])
        self.assertEqual(
            self.on_board(),
            {
                "/lib/ourble/__init__.mpy": b"ours",
                "/lib/ourhelper.mpy": b"helper",
                "/lib/aioble/core.mpy": b"aioble",
                "/lib/aioble/device.mpy": b"device",
            },
        )
        self.assertEqual(result["target"], "/lib")

    def test_a_name_only_upstream_has_is_found_there(self):
        self.session.omip_install(["fnmatch"])
        self.assertEqual(self.on_board(), {"/lib/fnmatch.mpy": b"from upstream"})

    def test_ours_comes_before_upstream(self):
        self.session.omip_install(["both"])
        self.assertEqual(self.on_board(), {"/lib/both.mpy": b"from ours"})

    def test_index_replaces_the_lookup_but_upstream_still_comes_from_micropython_lib(self):
        self.session.omip_install(["ourble"], index=self.urls["other"] + "/")
        self.assertEqual(
            self.on_board(),
            {
                "/lib/other.mpy": b"other",
                "/lib/aioble/core.mpy": b"aioble",
                "/lib/aioble/device.mpy": b"device",
            },
        )

    def test_no_mpy_takes_the_py_channel(self):
        self.session.omip_install(["ourble"], mpy=False, index=self.urls["ours"])
        self.assertEqual(self.on_board(), {"/lib/ourble/__init__.py": b"ours source"})

    def test_target_is_honoured(self):
        self.session.omip_install(["fnmatch"], target="/flash/lib/")
        self.assertEqual(self.on_board(), {"/flash/lib/fnmatch.mpy": b"from upstream"})

    def test_a_relative_target_is_refused(self):
        with self.assertRaisesRegex(RuntimeError, "absolute"):
            self.session.omip_install(["fnmatch"], target="lib")

    def test_a_missing_package_says_where_it_looked_and_writes_nothing(self):
        with self.assertRaises(omip.OmipError) as ctx:
            self.session.omip_install(["fnmatch", "nothere"])
        self.assertIn(self.urls["ours"], str(ctx.exception))
        self.assertIn(self.urls["up"], str(ctx.exception))
        self.assertEqual(self.on_board(), {})

    def test_a_missing_upstream_package_fails_rather_than_half_installs(self):
        (self.up_root / "package" / "6" / "aioble" / "latest.json").unlink()
        with self.assertRaisesRegex(omip.OmipError, "aioble"):
            self.session.omip_install(["ourble"])
        self.assertEqual(self.on_board(), {})

    def test_a_missing_file_writes_nothing(self):
        for blob in (self.up_root / "file").rglob("*"):
            if blob.is_file():
                blob.unlink()
        with self.assertRaisesRegex(omip.OmipError, "File not found"):
            self.session.omip_install(["ourble"])
        self.assertEqual(self.on_board(), {})

    def test_a_file_already_there_is_left_alone(self):
        self.session.omip_install(["ourble"])
        self.board.writes.clear()
        result = self.session.omip_install(["ourble"])
        self.assertEqual(self.board.writes, [])
        self.assertIn("Exists: /lib/aioble/core.mpy", result["output"])


class AppPackage(Base):
    """A package that names its own folder under ``target`` lands where ``omip.install`` puts it.

    The package is Home's real ``package.json`` from PyDevices/apps (copied to
    ``fixtures/apps_home_package.json``), with "target": "apps/home". The
    on-board rule is in omnipython's ``lib/omip.py``, ``_beside`` and
    ``_libs_for_app``.
    """

    def setUp(self):
        super().setUp()
        home = self.web_root / "apps" / "home"
        home.mkdir(parents=True)
        fixture = Path(__file__).parent / "fixtures" / "apps_home_package.json"
        (home / "package.json").write_bytes(fixture.read_bytes())
        (home / "__init__.py").write_bytes(b"home init")
        (home / "app.json").write_bytes(b"home json")
        self.spec = self.urls["web"] + "/apps/home/package.json"
        for name in ("appkit", "deskkit", "iconkit", "touchkit"):
            add_package(self.ours_root, "6", name, {f"{name}/__init__.mpy": name.encode()})
        add_package(self.up_root, "6", "requests", {"requests/__init__.mpy": b"requests"})
        self.libs = {
            "appkit/__init__.mpy": b"appkit",
            "deskkit/__init__.mpy": b"deskkit",
            "iconkit/__init__.mpy": b"iconkit",
            "touchkit/__init__.mpy": b"touchkit",
            "requests/__init__.mpy": b"requests",
        }

    def installed(self, app_folder: str, lib_folder: str) -> dict[str, bytes]:
        tree = {f"{app_folder}/__init__.py": b"home init", f"{app_folder}/app.json": b"home json"}
        tree.update({f"{lib_folder}/{path}": data for path, data in self.libs.items()})
        return tree

    def test_beside_the_default_lib_folder(self):
        self.session.omip_install([self.spec])
        self.assertEqual(self.on_board(), self.installed("/apps/home", "/lib"))

    def test_inside_a_target_that_is_not_a_lib_folder(self):
        self.session.omip_install([self.spec], target="/flash")
        self.assertEqual(self.on_board(), self.installed("/flash/apps/home", "/flash/lib"))

    def test_beside_a_target_that_is_a_lib_folder(self):
        self.session.omip_install([self.spec], target="/flash/lib")
        self.assertEqual(self.on_board(), self.installed("/flash/apps/home", "/flash/lib"))

    def test_on_the_root_drive(self):
        self.session.omip_install([self.spec], target="/")
        self.assertEqual(self.on_board(), self.installed("/apps/home", "/lib"))

    def test_an_absolute_folder_is_taken_as_written(self):
        path = self.web_root / "apps" / "home" / "package.json"
        path.write_text(path.read_text().replace('"apps/home"', '"/data/home"'))
        self.session.omip_install([self.spec], target="/flash")
        self.assertEqual(self.on_board(), self.installed("/data/home", "/flash/lib"))

    def test_a_library_installed_after_an_app_still_goes_to_the_target(self):
        self.session.omip_install([self.spec, "fnmatch"], target="/flash")
        self.assertEqual(
            self.on_board(),
            {**self.installed("/flash/apps/home", "/flash/lib"), "/flash/fnmatch.mpy": b"from upstream"},
        )

    def test_a_dependency_with_its_own_folder_gets_it(self):
        add_package(self.ours_root, "6", "extra", {"main.mpy": b"extra"}, target="apps/extra")
        add_package(self.ours_root, "6", "outer", {"outer.mpy": b"outer"}, deps=[["extra", "latest"]])
        self.session.omip_install(["outer"], target="/flash")
        self.assertEqual(
            self.on_board(), {"/flash/outer.mpy": b"outer", "/flash/apps/extra/main.mpy": b"extra"}
        )


class OmipItself(Base):
    def test_self_copies_the_file_as_source_on_micropython(self):
        self.session.omip_install([], omip_file=True)
        self.assertEqual(self.on_board(), {"/lib/omip.py": b"# omip\n"})

    def test_self_goes_by_the_github_address_mip_teaches(self):
        with mock.patch.object(omip, "OMIP_FILE", "github:PyDevices/omnipython/lib/omip.py"):
            plan = omip.Plan("6")
            omip.walk(plan, omip.OMIP_FILE, None, [omip.INDEX])
        self.assertEqual(
            plan.files[0].url,
            "https://raw.githubusercontent.com/PyDevices/omnipython/HEAD/lib/omip.py",
        )

    def test_self_copies_through_the_drive_on_circuitpython(self):
        self.session.interpreter = "circuitpython"
        with tempfile.TemporaryDirectory() as drive:
            self.session._circuitpy_msc_root = lambda: Path(drive)
            result = self.session.omip_install([], omip_file=True)
            self.assertEqual((Path(drive) / "lib" / "omip.py").read_bytes(), b"# omip\n")
        self.assertEqual(result["via"], "circuitpy_msc")

    def test_self_copies_over_serial_on_circuitpython_with_no_drive(self):
        self.session.interpreter = "circuitpython"
        self.session._circuitpy_msc_root = lambda: None
        self.session.omip_install([], omip_file=True)
        self.assertEqual(self.on_board(), {"/lib/omip.py": b"# omip\n"})

    def test_named_packages_on_circuitpython_point_at_circup(self):
        self.session.interpreter = "circuitpython"
        with self.assertRaisesRegex(RuntimeError, "circup"):
            self.session.omip_install(["bledev"])
        self.assertEqual(self.on_board(), {})


class PlainMip(Base):
    """``mcuctl mip --index`` is mpremote's mip: one index, and upstream ignored."""

    def test_mip_index_ignores_upstream_and_stays_on_its_one_index(self):
        with mock.patch.object(self.session, "_take_control_resilient", side_effect=lambda t, **k: t):
            self.session.mip_install(["ourble"], index=self.urls["ours"])
        self.assertEqual(
            self.on_board(),
            {"/lib/ourble/__init__.mpy": b"ours", "/lib/ourhelper.mpy": b"helper"},
        )


class Cli(unittest.TestCase):
    class Client:
        def __init__(self):
            self.calls = []

        def call(self, method, params=None):
            self.calls.append((method, params))
            return {}

    def _run(self, *argv):
        ns = build_parser().parse_args(["omip", *argv])
        client = self.Client()
        with (
            mock.patch("mcuctl.cli.get_client", return_value=(client, "tcp")),
            mock.patch("mcuctl.cli.ensure_device"),
            redirect_stdout(io.StringIO()),
        ):
            ns.func(ns)
        return client.calls[-1]

    def test_names_and_flags_reach_the_sidecar(self):
        method, params = self._run("bledev", "--index", "http://x", "--target", "/flash/lib", "--no-mpy")
        self.assertEqual(method, "omip_install")
        self.assertEqual(
            params,
            {"packages": ["bledev"], "target": "/flash/lib", "mpy": False, "omip_file": False, "index": "http://x"},
        )

    def test_self_needs_no_name(self):
        _, params = self._run("--self")
        self.assertEqual(params["packages"], [])
        self.assertTrue(params["omip_file"])

    def test_nothing_to_do_is_an_error_not_a_quiet_success(self):
        ns = argparse.Namespace(packages=[], self_file=False)
        with self.assertRaisesRegex(SystemExit, "--self"):
            cmd_omip(ns)

    def test_mip_keeps_its_own_method(self):
        ns = build_parser().parse_args(["mip", "bledev", "--index", "http://x"])
        client = self.Client()
        with (
            mock.patch("mcuctl.cli.get_client", return_value=(client, "tcp")),
            mock.patch("mcuctl.cli.ensure_device"),
            redirect_stdout(io.StringIO()),
        ):
            ns.func(ns)
        self.assertEqual(client.calls[-1][0], "mip_install")


if __name__ == "__main__":
    unittest.main()
