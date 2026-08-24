"""mip install --index (mpftp#18): the RPC already took an index, the CLI just never asked for one."""

from __future__ import annotations

import argparse
import io
import unittest
from contextlib import redirect_stdout
from unittest import mock

from mpftp.cli import RpcClient, cmd_mip


class FakeClient(RpcClient):
    def __init__(self):
        self.calls: list[tuple[str, dict]] = []

    def call(self, method, params=None):
        self.calls.append((method, params or {}))
        return {"output": "", "target": "/lib"}


class CmdMipTests(unittest.TestCase):
    def _ns(self, **overrides):
        base = dict(device=None, baud=115200, packages=["adafruit_bus_device"], target="/lib", no_mpy=False, index=None)
        base.update(overrides)
        return argparse.Namespace(**base)

    def _run(self, ns):
        client = FakeClient()
        with mock.patch("mpftp.cli.get_client", return_value=(client, "tcp")):
            buf = io.StringIO()
            with redirect_stdout(buf):
                cmd_mip(ns)
        return client

    def test_no_index_omits_the_param_entirely(self):
        client = self._run(self._ns())
        _, params = client.calls[-1]
        self.assertNotIn("index", params)

    def test_index_flag_is_forwarded_to_mip_install(self):
        client = self._run(self._ns(index="https://micropython.org/pi/v2"))
        method, params = client.calls[-1]
        self.assertEqual(method, "mip_install")
        self.assertEqual(params["index"], "https://micropython.org/pi/v2")

    def test_packages_target_and_mpy_still_forwarded(self):
        client = self._run(self._ns(no_mpy=True))
        _, params = client.calls[-1]
        self.assertEqual(params["packages"], ["adafruit_bus_device"])
        self.assertEqual(params["target"], "/lib")
        self.assertFalse(params["mpy"])


if __name__ == "__main__":
    unittest.main()
