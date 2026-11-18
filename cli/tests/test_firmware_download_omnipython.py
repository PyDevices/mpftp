# SPDX-FileCopyrightText: 2026 Brad Barnett
#
# SPDX-License-Identifier: MIT

"""Firmware from OmniPython's own releases (mcuctl.firmware_download.OmnipythonBuilds).

No network: the manifest in fixtures/ is a trimmed copy of one a real
omnipython-builds release carries (``firmware.json``), and the release list is
built here in the shape GitHub's API returns it.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import unittest
import urllib.request
from pathlib import Path
from unittest import mock

from mcuctl import firmware_download as fd

FIXTURES = Path(__file__).resolve().parent / "fixtures"
API = "https://api.github.com/repos/PyDevices/omnipython-builds/releases?per_page=30"
MANIFEST = json.loads((FIXTURES / "omnipython-firmware.json").read_text("utf-8"))


def release(tag: str, *, draft: bool = False, manifest: bool = True, drop: str = "") -> dict:
    """A release as the API lists it, with the files MANIFEST names."""
    names = ["firmware.json"] if manifest else []
    for b in MANIFEST["builds"]:
        names += [f["name"] for f in b["files"] if drop not in f["name"] or not drop]
    base = f"https://github.com/PyDevices/omnipython-builds/releases/download/{tag}"
    assets = [
        {
            "name": n,
            "url": f"https://api.github.com/repos/PyDevices/omnipython-builds/releases/assets/{i}",
            "browser_download_url": f"{base}/{n}",
        }
        for i, n in enumerate(names, 100)
    ]
    return {"tag_name": tag, "draft": draft, "prerelease": False, "assets": assets}


class Network:
    """A fetcher serving a release list and each release's manifest."""

    def __init__(self, releases: list[dict]):
        self.releases = releases
        self.urls: list[str] = []

    def __call__(self, url: str, timeout: float = 30.0) -> str:
        self.urls.append(url)
        if url == API:
            return json.dumps(self.releases)
        for rel in self.releases:
            for a in rel["assets"]:
                if a["name"] == "firmware.json" and url in (a["url"], a["browser_download_url"]):
                    return json.dumps({**MANIFEST, "tag": rel["tag_name"]})
        raise RuntimeError(f"unexpected fetch in a test: {url}")


class Isolated(unittest.TestCase):
    def setUp(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        patcher = mock.patch.object(fd, "CACHE_ROOT", Path(td.name))
        patcher.start()
        self.addCleanup(patcher.stop)
        env = mock.patch.dict("os.environ", {"GH_TOKEN": "", "GITHUB_TOKEN": ""})
        env.start()
        self.addCleanup(env.stop)

    def catalog(self, net):
        return fd.load_catalog(fetch=net, source="omnipython")


class CatalogTests(Isolated):
    def test_builds_become_boards_by_their_id(self):
        cat = self.catalog(Network([release("v0.8.0")]))
        by_id = {b["board"]: b for b in cat}
        self.assertEqual(set(by_id), {"mp-p4-devkit", "mp-s3-octal", "cp-feather-rp2040"})
        self.assertEqual(by_id["mp-p4-devkit"]["port"], "esp32")
        self.assertEqual(by_id["mp-p4-devkit"]["flash_offset"], "0x2000")
        self.assertEqual(by_id["cp-feather-rp2040"]["port"], "rp2")

    def test_the_default_catalog_never_reads_the_releases(self):
        net = Network([release("v0.8.0")])
        with mock.patch.object(fd, "SOURCES", [fd.MicroPythonOrg()]), self.assertRaises(RuntimeError):
            fd.load_catalog(fetch=net)
        self.assertNotIn(API, net.urls)

    def test_an_unknown_source_names_the_ones_there_are(self):
        with self.assertRaisesRegex(RuntimeError, "micropython, omnipython"):
            fd.sources_for("nowhere")

    def test_releases_without_a_manifest_are_not_firmware(self):
        net = Network([release("v0.9.0", manifest=False), release("v0.8.0")])
        self.assertEqual(len(self.catalog(net)), 3)

    def test_no_release_is_an_error_that_mentions_drafts(self):
        with self.assertRaisesRegex(RuntimeError, "GH_TOKEN"):
            self.catalog(Network([release("v0.9.0", manifest=False)]))

    def test_an_empty_answer_is_not_kept_for_a_later_call_with_a_token(self):
        calls = []

        def net(url, timeout=30.0):
            calls.append(url)
            if url == API:
                return json.dumps([release("staged-x", draft=True)] if os.environ.get("GH_TOKEN") else [])
            return json.dumps({**MANIFEST, "tag": "staged-x"})

        with self.assertRaisesRegex(RuntimeError, "GH_TOKEN"):
            self.catalog(net)
        with mock.patch.dict("os.environ", {"GH_TOKEN": "t"}):
            self.assertEqual(len(self.catalog(net)), 3)

    def test_an_unknown_board_lists_what_the_source_has(self):
        net = Network([release("v0.8.0")])
        cat = self.catalog(net)
        with self.assertRaisesRegex(RuntimeError, r"source omnipython; it has: .*mp-s3-octal"):
            fd.download_board("ESP32_GENERIC_S3", catalog=cat, source="omnipython", fetch=net)


class PickTests(Isolated):
    def pick(self, board, net, **kw):
        cat = self.catalog(net)
        return fd.pick_download(fd.find_variant(board, catalog=cat), fetch=net, **kw)

    def test_esp_boards_get_the_whole_image_at_the_manifests_offset(self):
        net = Network([release("v0.8.0")])
        p4 = self.pick("mp-p4-devkit", net, preview=True)
        self.assertTrue(p4["name"].endswith(".bin") and not p4["name"].endswith(".app.bin"))
        self.assertEqual(p4["offset"], "0x2000")
        self.assertEqual(len(p4["sha256"]), 64)
        self.assertEqual(self.pick("mp-s3-octal", net, preview=True)["offset"], "0x0")

    def test_rp2_gets_the_uf2(self):
        got = self.pick("cp-feather-rp2040", Network([release("v0.8.0")]), preview=True)
        self.assertEqual(got["kind"], "uf2")

    def test_the_newest_release_is_the_default_and_a_tag_selects_one(self):
        net = Network([release("v0.9.0"), release("v0.8.0")])
        self.assertEqual(self.pick("mp-s3-octal", net, preview=True)["version"], "v0.9.0")
        self.assertEqual(self.pick("mp-s3-octal", net, preview=True, version="v0.8.0")["version"], "v0.8.0")
        with self.assertRaisesRegex(RuntimeError, "version v0.7.0 not found"):
            self.pick("mp-s3-octal", net, preview=True, version="v0.7.0")

    def test_the_manifest_marks_previews_so_a_plain_ask_for_a_release_fails(self):
        net = Network([release("v0.8.0")])
        with self.assertRaisesRegex(RuntimeError, "no release build"):
            self.pick("mp-s3-octal", net)

    def test_a_plain_ask_that_only_a_preview_answers_names_the_flag_and_the_set(self):
        net = Network([release("staged-x", draft=True)])
        with self.assertRaisesRegex(RuntimeError, r"--preview.*staged-x|staged-x.*--preview"):
            self.pick("mp-s3-octal", net)

    def test_a_file_the_manifest_lists_but_the_release_lacks_is_an_error(self):
        net = Network([release("v0.8.0", drop="spiram_oct")])
        cat = self.catalog(net)
        entry = fd.find_variant("mp-s3-octal", catalog=cat)
        with self.assertRaisesRegex(RuntimeError, "but has no such file"):
            fd.pick_download(entry, fetch=net, preview=True)

    def test_a_draft_is_reached_through_the_api(self):
        net = Network([release("staged-x", draft=True)])
        got = self.pick("mp-s3-octal", net, preview=True)
        self.assertTrue(got["url"].startswith("https://api.github.com/repos/"), got["url"])
        self.assertTrue(got["name"].endswith(".bin"))


class FakeResponse:
    def __init__(self, body: bytes):
        self.body = body
        self.headers = {"Content-Length": str(len(body))}

    def read(self, n: int) -> bytes:
        chunk, self.body = self.body[:n], self.body[n:]
        return chunk

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class DownloadTests(Isolated):
    def run_download(self, body: bytes, sha: str, **kw):
        manifest = json.loads(json.dumps(MANIFEST))
        for b in manifest["builds"]:
            if b["id"] == "mp-s3-octal":
                for f in b["files"]:
                    if f["role"] == "flash":
                        f["sha256"] = sha
        rels = [release("staged-x", draft=True)]

        def fetch(url, timeout=30.0):
            if url == API:
                return json.dumps(rels)
            return json.dumps(manifest)

        with mock.patch.object(fd, "_open", return_value=FakeResponse(body)) as op:
            out = fd.download_board("mp-s3-octal", preview=True, source="omnipython", fetch=fetch, **kw)
        return out, op

    def test_a_download_is_checked_and_named_after_the_file_not_the_asset_id(self):
        body = b"image"
        out, op = self.run_download(body, hashlib.sha256(body).hexdigest())
        self.assertTrue(Path(out["artifact"]).name.startswith("omnipython-mp-esp32_generic_s3-spiram_oct-"))
        self.assertEqual(out["sha256"], hashlib.sha256(body).hexdigest())
        self.assertEqual(out["flashOffset"], "0x0")
        self.assertEqual(out["catalogSource"], "omnipython-builds")

    def test_a_wrong_checksum_is_an_error_and_leaves_no_file(self):
        with self.assertRaisesRegex(RuntimeError, "release lists"):
            self.run_download(b"image", "0" * 64)
        self.assertEqual(list((fd.cache_root() / "files").iterdir()), [])

    def test_a_cached_copy_with_the_wrong_checksum_says_how_to_fix_it(self):
        body = b"image"
        self.run_download(body, hashlib.sha256(body).hexdigest())
        # The release's manifest is cached for an hour; a changed checksum
        # arrives with a fresh one.
        for f in (fd.cache_root() / "pages").iterdir():
            f.unlink()
        (fd.cache_root() / "index" / "omnipython-releases-anon.json").unlink()
        with self.assertRaisesRegex(RuntimeError, "--force"):
            self.run_download(b"other", "0" * 64)

    def test_the_token_goes_only_to_the_api_and_not_across_a_redirect(self):
        with mock.patch.dict("os.environ", {"GH_TOKEN": "t0ken"}):
            api = fd._request("https://api.github.com/repos/x/y/releases/assets/1")
            plain = fd._request("https://github.com/x/y/releases/download/v1/a.bin")
            self.assertEqual(api.get_header("Authorization"), "Bearer t0ken")
            self.assertEqual(api.get_header("Accept"), "application/octet-stream")
            self.assertIsNone(plain.get_header("Authorization"))
            handler = fd._DropAuthOffHost()
            new = handler.redirect_request(
                api, None, 302, "Found", {}, "https://release-assets.githubusercontent.com/a?sig=1"
            )
            self.assertIsNone(new.get_header("Authorization"))


if __name__ == "__main__":
    unittest.main()
