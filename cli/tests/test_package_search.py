"""`mpftp search`: one search across micropython-lib, the PyDevices index and the CircuitPython bundles.

The index samples under fixtures/package-index are cut from the real ones
(micropython.org/pi/v2, PyDevices.github.io/mip and the two bundles' release
manifests), so nothing here touches the network.
"""

from __future__ import annotations

import argparse
import io
import json
import tempfile
import unittest
import urllib.error
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from mpftp import package_search as ps
from mpftp.cli import cmd_search

FIXTURES = Path(__file__).parent / "fixtures" / "package-index"
SOURCE = {s["key"]: s for s in ps.SOURCES}

TEXT = {
    SOURCE["micropython-lib"]["url"]: (FIXTURES / "micropython-lib.json").read_text(),
    SOURCE["pydevices"]["url"]: (FIXTURES / "pydevices.json").read_text(),
    ps.bundle_manifest_url(SOURCE["adafruit"], "20261010"): (
        FIXTURES / "adafruit-bundle.json"
    ).read_text(),
    ps.bundle_manifest_url(SOURCE["community"], "20261008"): (
        FIXTURES / "community-bundle.json"
    ).read_text(),
}
TAGS = {
    "https://github.com/adafruit/Adafruit_CircuitPython_Bundle/releases/latest": "20261010",
    "https://github.com/adafruit/CircuitPython_Community_Bundle/releases/latest": "20261008",
}


class Net:
    """A recorded network: every fetch is counted, and it can be switched off."""

    def __init__(self, down: bool = False):
        self.down = down
        self.fetched: list[str] = []

    def fetch(self, url: str) -> str:
        self.fetched.append(url)
        if self.down:
            raise urllib.error.URLError("Name or service not known")
        return TEXT[url]

    def latest_tag(self, url: str) -> str:
        self.fetched.append(url)
        if self.down:
            raise urllib.error.URLError("Name or service not known")
        return TAGS[url]


def run_search(query, net: Net, root: Path, **kw):
    return ps.search(
        query, fetch=net.fetch, latest_tag=net.latest_tag, cache_root=root, **kw
    )


class ParseTests(unittest.TestCase):
    def test_mip_index_entries_carry_version_description_and_a_plain_mip_install(self):
        entries = ps.parse_mip_index(TEXT[SOURCE["micropython-lib"]["url"]], SOURCE["micropython-lib"])
        by_name = {e["name"]: e for e in entries}
        self.assertEqual(len(entries), 7)
        neo = by_name["neopixel"]
        self.assertEqual(neo["version"], "0.1.0")
        self.assertEqual(neo["description"], "WS2812/NeoPixel driver.")
        self.assertEqual(neo["interpreter"], "micropython")
        self.assertEqual(neo["source_label"], "micropython-lib")
        self.assertEqual(neo["install"]["command"], "mpftp mip neopixel")
        self.assertEqual(neo["install"]["method"], "mip_install")
        self.assertEqual(neo["install"]["params"], {"packages": ["neopixel"]})
        # An empty description stays empty rather than becoming "None".
        self.assertEqual(by_name["__future__"]["description"], "")

    def test_pydevices_entries_install_with_the_pydevices_index(self):
        entries = ps.parse_mip_index(TEXT[SOURCE["pydevices"]["url"]], SOURCE["pydevices"])
        pd = next(e for e in entries if e["name"] == "pdwidgets")
        self.assertEqual(
            pd["install"]["command"], "mpftp mip pdwidgets --index https://PyDevices.github.io/mip"
        )
        self.assertEqual(
            pd["install"]["params"],
            {"packages": ["pdwidgets"], "index": "https://PyDevices.github.io/mip"},
        )
        self.assertEqual(pd["version"], "0.0.24")

    def test_bundle_manifest_entries_use_the_pypi_description_and_circup(self):
        url = ps.bundle_manifest_url(SOURCE["adafruit"], "20261010")
        self.assertEqual(
            url,
            "https://github.com/adafruit/Adafruit_CircuitPython_Bundle/releases/download/"
            "20261010/adafruit-circuitpython-bundle-20261010.json",
        )
        entries = ps.parse_bundle_index(TEXT[url], SOURCE["adafruit"], "20261010")
        by_name = {e["name"]: e for e in entries}
        self.assertEqual(len(entries), 6)
        dt = by_name["adafruit_display_text"]
        self.assertEqual(dt["version"], "5.0.6")
        self.assertEqual(dt["description"], "Displays text using CircuitPython's displayio.")
        self.assertEqual(dt["pypi_name"], "adafruit-circuitpython-display-text")
        self.assertEqual(dt["interpreter"], "circuitpython")
        self.assertEqual(dt["bundle_tag"], "20261010")
        self.assertEqual(dt["install"]["command"], "mpftp circup adafruit_display_text")
        self.assertEqual(dt["install"]["method"], "circup_install")
        self.assertEqual(dt["install"]["params"], {"packages": ["adafruit_display_text"]})
        self.assertEqual(dt["dependencies"], ["adafruit_bitmap_font", "adafruit_ticks"])

    def test_a_malformed_index_is_an_error_not_an_empty_result(self):
        with self.assertRaises(ValueError):
            ps.parse_mip_index('{"v": 2}', SOURCE["micropython-lib"])
        with self.assertRaises(ValueError):
            ps.parse_bundle_index("[]", SOURCE["adafruit"])


class RankTests(unittest.TestCase):
    def entries(self):
        out = []
        out += ps.parse_mip_index(TEXT[SOURCE["micropython-lib"]["url"]], SOURCE["micropython-lib"])
        out += ps.parse_bundle_index(
            TEXT[ps.bundle_manifest_url(SOURCE["adafruit"], "20261010")], SOURCE["adafruit"]
        )
        return out

    def test_exact_name_then_prefix_then_substring_then_description(self):
        hits = ps.rank(self.entries(), "neopixel")
        self.assertEqual(
            [(h["name"], h["source"], h["match"]) for h in hits],
            [("neopixel", "micropython-lib", 0), ("neopixel", "adafruit", 0)],
        )
        hits = ps.rank(self.entries(), "ssd1306")
        self.assertEqual(
            [(h["name"], h["match"]) for h in hits],
            [("ssd1306", 0), ("adafruit_ssd1306", 2)],
        )
        # A name that starts with the query outranks one that merely contains it,
        # whatever the alphabet says.
        hits = ps.rank(self.entries(), "ssd")
        self.assertEqual(
            [(h["name"], h["match"]) for h in hits],
            [("ssd1306", 1), ("adafruit_ssd1306", 2)],
        )
        hits = ps.rank(self.entries(), "display")
        names = [h["name"] for h in hits]
        # Name matches come before a description-only match (imageload's
        # description says "Displays text").
        self.assertEqual(names[:2], ["adafruit_display_emoji_text", "adafruit_display_text"])
        self.assertEqual(hits[0]["match"], 2)
        self.assertEqual([h["name"] for h in hits if h["match"] == 4],
                         ["adafruit_imageload", "adafruit_ssd1306"])
        self.assertEqual([h["match"] for h in hits], sorted(h["match"] for h in hits))

    def test_every_word_must_match_and_case_underscore_and_dash_do_not_matter(self):
        hits = ps.rank(self.entries(), ["Display", "TEXT"])
        self.assertEqual(hits[0]["name"], "adafruit_display_text")
        self.assertEqual(hits[0]["match"], 2)  # "display-text" is inside the name
        self.assertEqual(
            [h["name"] for h in hits],
            ["adafruit_display_text", "adafruit_display_emoji_text", "adafruit_imageload"],
        )
        # Joined with a dash or an underscore it is one phrase: only the name
        # that contains "display-text" in that order matches.
        self.assertEqual([h["name"] for h in ps.rank(self.entries(), "display-text")],
                         ["adafruit_display_text"])
        self.assertEqual([h["name"] for h in ps.rank(self.entries(), "DISPLAY_TEXT")],
                         ["adafruit_display_text"])
        self.assertEqual(ps.rank(self.entries(), "display zebra"), [])

    def test_words_from_the_pypi_name_count(self):
        hits = ps.rank(self.entries(), "circuitpython-bus-device")
        self.assertEqual([h["name"] for h in hits], ["adafruit_bus_device"])

    def test_mirrored_micropython_lib_packages_list_once(self):
        entries = ps.parse_mip_index(
            TEXT[SOURCE["micropython-lib"]["url"]], SOURCE["micropython-lib"]
        ) + ps.parse_mip_index(TEXT[SOURCE["pydevices"]["url"]], SOURCE["pydevices"])
        kept = ps.drop_mirrors(entries)
        pairs = {(e["name"], e["source"]) for e in kept}
        # Same name and version in both: micropython-lib's own copy is listed.
        self.assertIn(("neopixel", "micropython-lib"), pairs)
        self.assertNotIn(("neopixel", "pydevices"), pairs)
        # A different version is a different package and stays.
        self.assertIn(("requests", "micropython-lib"), pairs)
        self.assertIn(("requests", "pydevices"), pairs)
        # PyDevices' own packages stay, of course.
        self.assertIn(("pdwidgets", "pydevices"), pairs)


class CacheTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)

    def test_first_search_fetches_every_index_and_writes_the_cache(self):
        net = Net()
        res = run_search("neopixel", net, self.root)
        self.assertEqual([s["from"] for s in res["sources"]], ["network"] * 4)
        self.assertFalse(res["offline"])
        self.assertIsNone(res["note"])
        self.assertEqual(sorted(p.name for p in self.root.iterdir()),
                         ["adafruit.json", "community.json", "micropython-lib.json", "pydevices.json"])
        self.assertEqual(len(net.fetched), 6)  # four indexes, two latest-tag lookups
        self.assertEqual(res["sources"][2]["tag"], "20261010")

    def test_second_search_reads_the_cache_and_refresh_fetches_again(self):
        net = Net()
        run_search("neopixel", net, self.root)
        net.fetched.clear()
        res = run_search("neopixel", net, self.root)
        self.assertEqual(net.fetched, [])
        self.assertEqual([s["from"] for s in res["sources"]], ["cache"] * 4)
        self.assertEqual([h["name"] for h in res["results"]], ["neopixel", "neopixel"])
        res = run_search("neopixel", net, self.root, refresh=True)
        self.assertEqual(len(net.fetched), 6)
        self.assertEqual([s["from"] for s in res["sources"]], ["network"] * 4)

    def test_an_old_cache_is_fetched_again(self):
        net = Net()
        run_search("neopixel", net, self.root)
        net.fetched.clear()
        old = ps.INDEX_TTL_SEC + 60
        with mock.patch("mpftp.package_search.time.time", return_value=ps.time.time() + old):
            res = run_search("neopixel", net, self.root)
        self.assertEqual(len(net.fetched), 6)
        self.assertEqual([s["from"] for s in res["sources"]], ["network"] * 4)

    def test_offline_with_an_old_cache_searches_it_and_says_so(self):
        run_search("neopixel", Net(), self.root)
        down = Net(down=True)
        far = ps.time.time() + 3 * 86400
        with mock.patch("mpftp.package_search.time.time", return_value=far):
            res = run_search("neopixel", down, self.root)
        self.assertTrue(res["offline"])
        self.assertEqual([h["name"] for h in res["results"]], ["neopixel", "neopixel"])
        self.assertIn("Offline: searched the cached copy of", res["note"])
        self.assertIn("micropython-lib (3 d old)", res["note"])
        self.assertIn("Name or service not known", res["note"])
        for s in res["sources"]:
            self.assertTrue(s["offline"])
            self.assertEqual(s["from"], "cache")
            self.assertGreater(s["count"], 0)

    def test_offline_with_no_cache_is_a_clear_message_and_no_results(self):
        res = run_search("neopixel", Net(down=True), self.root)
        self.assertTrue(res["offline"])
        self.assertEqual(res["results"], [])
        self.assertIn("Could not fetch micropython-lib, PyDevices, Adafruit CircuitPython Bundle, "
                      "CircuitPython Community Bundle and there is no cached copy yet", res["note"])
        self.assertEqual(list(self.root.iterdir()), [])

    def test_one_index_down_leaves_the_others_searchable(self):
        net = Net()
        good = net.fetch

        def flaky(url):
            if "micropython.org" in url:
                raise urllib.error.URLError("timed out")
            return good(url)

        res = ps.search("neopixel", fetch=flaky, latest_tag=net.latest_tag, cache_root=self.root)
        self.assertTrue(res["offline"])
        self.assertEqual([(h["name"], h["source"]) for h in res["results"]],
                         [("neopixel", "pydevices"), ("neopixel", "adafruit")])
        self.assertEqual(res["sources"][0]["error"], "URLError: <urlopen error timed out>")
        self.assertIn("Could not fetch micropython-lib and there is no cached copy", res["note"])

    def test_a_damaged_cache_file_is_fetched_over(self):
        net = Net()
        run_search("neopixel", net, self.root)
        (self.root / "adafruit.json").write_text("not json")
        net.fetched.clear()
        res = run_search("neopixel", net, self.root)
        self.assertEqual(res["sources"][2]["from"], "network")
        self.assertEqual(len(net.fetched), 2)


class SearchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)
        self.net = Net()

    def test_results_are_ranked_cut_to_limit_and_counted(self):
        res = run_search("display text", self.net, self.root, limit=2)
        self.assertEqual(res["query"], "display text")
        self.assertEqual([h["name"] for h in res["results"]],
                         ["adafruit_display_text", "adafruit_display_emoji_text"])
        self.assertEqual(res["total"], 3)
        res = run_search("display text", self.net, self.root, limit=None)
        self.assertEqual(len(res["results"]), 3)

    def test_interpreter_and_source_narrow_what_is_searched(self):
        res = run_search("neopixel", self.net, self.root, interpreter="circuitpython")
        self.assertEqual([s["source"] for s in res["sources"]], ["adafruit", "community"])
        self.assertEqual([h["source"] for h in res["results"]], ["adafruit"])
        res = run_search("neopixel", self.net, self.root, sources=["pydevices"])
        self.assertEqual([s["source"] for s in res["sources"]], ["pydevices"])
        # Alone, the PyDevices index is not a mirror of anything: its copy is listed.
        self.assertEqual([h["source"] for h in res["results"]], ["pydevices"])
        with self.assertRaises(ValueError):
            run_search("neopixel", self.net, self.root, sources=["pypi"])
        with self.assertRaises(ValueError):
            run_search("neopixel", self.net, self.root, interpreter="python")

    def test_an_empty_query_is_refused(self):
        with self.assertRaises(ValueError):
            run_search("   ", self.net, self.root)
        with self.assertRaises(ValueError):
            run_search([], self.net, self.root)

    def test_text_output_shows_name_version_source_and_the_install_line(self):
        text = ps.format_text(run_search("pdwidgets", self.net, self.root))
        self.assertEqual(
            text.splitlines(),
            [
                "pdwidgets 0.0.24  [PyDevices] — Cross-platform widget toolkit for PyDevices",
                "    mpftp mip pdwidgets --index https://PyDevices.github.io/mip",
            ],
        )
        self.assertEqual(ps.format_text(run_search("zzz", self.net, self.root)),
                         "Nothing matches 'zzz'.")
        text = ps.format_text(run_search("display text", self.net, self.root, limit=1))
        self.assertTrue(text.endswith("1 of 3 matches; --limit shows more."))


class LatestTagTests(unittest.TestCase):
    def test_the_tag_is_read_from_the_redirect(self):
        class Resp:
            headers = {"Location": "https://github.com/adafruit/Adafruit_CircuitPython_Bundle/releases/tag/20261010"}

            def geturl(self):
                return "x"

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        opener = mock.Mock()
        opener.open.return_value = Resp()
        with mock.patch("mpftp.package_search.urllib.request.build_opener", return_value=opener):
            tag = ps.default_latest_tag("https://github.com/adafruit/Adafruit_CircuitPython_Bundle/releases/latest")
        self.assertEqual(tag, "20261010")
        req = opener.open.call_args[0][0]
        self.assertEqual(req.get_method(), "HEAD")

    def test_a_redirect_raised_as_an_http_error_still_gives_the_tag(self):
        err = urllib.error.HTTPError(
            "u", 302, "Found",
            {"Location": "https://github.com/adafruit/CircuitPython_Community_Bundle/releases/tag/20261008"},
            None,
        )
        opener = mock.Mock()
        opener.open.side_effect = err
        with mock.patch("mpftp.package_search.urllib.request.build_opener", return_value=opener):
            tag = ps.default_latest_tag("https://github.com/adafruit/CircuitPython_Community_Bundle/releases/latest")
        self.assertEqual(tag, "20261008")

    def test_no_redirect_is_an_error(self):
        class Resp:
            headers = {}

            def geturl(self):
                return "https://github.com/x/y/releases/latest"

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        opener = mock.Mock()
        opener.open.return_value = Resp()
        with mock.patch(
            "mpftp.package_search.urllib.request.build_opener", return_value=opener
        ), self.assertRaises(RuntimeError):
            ps.default_latest_tag("https://github.com/x/y/releases/latest")


class CmdSearchTests(unittest.TestCase):
    """The CLI passes its flags through and never asks for a board."""

    def _ns(self, **overrides):
        base = dict(words=["display", "text"], source=None, interpreter=None, limit=25,
                    refresh=False, text=False)
        base.update(overrides)
        return argparse.Namespace(**base)

    def test_flags_reach_the_engine_and_the_answer_is_json(self):
        answer = {"query": "display text", "results": [], "total": 0, "sources": [],
                  "offline": False, "note": None}
        with mock.patch("mpftp.package_search.search", return_value=answer) as search, \
                mock.patch("mpftp.cli.get_client") as get_client:
            buf = io.StringIO()
            with redirect_stdout(buf):
                cmd_search(self._ns(source=["adafruit"], interpreter="circuitpython", limit=5, refresh=True))
        get_client.assert_not_called()
        search.assert_called_once_with(["display", "text"], sources=["adafruit"],
                                       interpreter="circuitpython", limit=5, refresh=True)
        self.assertEqual(json.loads(buf.getvalue()), answer)

    def test_text_flag_prints_lines(self):
        answer = {"query": "x", "results": [], "total": 0, "sources": [], "offline": False, "note": None}
        with mock.patch("mpftp.package_search.search", return_value=answer):
            buf = io.StringIO()
            with redirect_stdout(buf):
                cmd_search(self._ns(words=["x"], text=True))
        self.assertEqual(buf.getvalue().strip(), "Nothing matches 'x'.")


class SidecarMethodTests(unittest.TestCase):
    def test_package_search_rpc_takes_query_or_words(self):
        from mpftp.sidecar import METHODS

        with mock.patch("mpftp.package_search.search", return_value={"results": []}) as search:
            METHODS["package_search"]({"query": "neo", "interpreter": "micropython", "limit": 3})
            search.assert_called_with("neo", sources=None, interpreter="micropython", limit=3, refresh=False)
            METHODS["package_search"]({"words": ["a", "b"], "sources": ["pydevices"], "refresh": True})
            search.assert_called_with(["a", "b"], sources=["pydevices"], interpreter=None, limit=25, refresh=True)


if __name__ == "__main__":
    unittest.main()
