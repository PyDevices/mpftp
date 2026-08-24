"""mpftp MCP server: JSON-RPC framing and tool dispatch (mpftp#19). No board required."""

from __future__ import annotations

import base64
import io
import json
import unittest
from contextlib import redirect_stdout
from unittest import mock


def _load_mcp():
    from mpftp import mcp

    return mcp


class FakeClient:
    def __init__(self, responses=None):
        self.calls: list[tuple[str, dict]] = []
        self.responses = responses or {}
        self.closed = False

    def call(self, method, params=None):
        self.calls.append((method, params or {}))
        resp = self.responses.get(method)
        if isinstance(resp, Exception):
            raise resp
        return resp if resp is not None else {}

    def stream_repl(self, on_notify, duration=None):
        for method, params in self.responses.get("_stream", []):
            on_notify(method, params)

    def close(self):
        self.closed = True


class ProtocolFramingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = _load_mcp()

    def _dispatch(self, msg: dict) -> list[dict]:
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.mod._dispatch_line(json.dumps(msg))
        lines = [line for line in buf.getvalue().splitlines() if line.strip()]
        return [json.loads(line) for line in lines]

    def test_initialize_reports_protocol_version_and_tools_capability(self):
        replies = self._dispatch({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
        self.assertEqual(len(replies), 1)
        result = replies[0]["result"]
        self.assertEqual(result["protocolVersion"], self.mod.PROTOCOL_VERSION)
        self.assertIn("tools", result["capabilities"])
        self.assertEqual(result["serverInfo"]["name"], "mpftp")

    def test_tools_list_matches_the_registry(self):
        replies = self._dispatch({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
        names = {t["name"] for t in replies[0]["result"]["tools"]}
        self.assertEqual(names, set(self.mod.TOOLS_BY_NAME))
        for tool in replies[0]["result"]["tools"]:
            self.assertIn("inputSchema", tool)
            self.assertIn("description", tool)

    def test_a_notification_gets_no_reply(self):
        replies = self._dispatch({"jsonrpc": "2.0", "method": "notifications/initialized"})
        self.assertEqual(replies, [])

    def test_an_unknown_method_is_a_jsonrpc_error(self):
        replies = self._dispatch({"jsonrpc": "2.0", "id": 3, "method": "nonexistent"})
        self.assertEqual(replies[0]["error"]["code"], -32601)

    def test_a_parse_error_replies_with_a_null_id(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.mod._dispatch_line("{not json")
        reply = json.loads(buf.getvalue().strip())
        self.assertEqual(reply["error"]["code"], -32700)


class ToolsCallDispatchTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = _load_mcp()

    def test_an_unknown_tool_name_is_an_error_result_not_a_protocol_error(self):
        result = self.mod._handle_tools_call({"name": "nope", "arguments": {}})
        self.assertTrue(result.get("isError"))
        self.assertIn("unknown tool", result["content"][0]["text"])

    def test_a_tool_exception_is_reported_as_an_error_result(self):
        client = FakeClient(responses={"list_ports": RuntimeError("port busy")})
        with mock.patch.object(self.mod, "get_client", return_value=(client, "tcp:x")):
            result = self.mod._handle_tools_call({"name": "list_ports", "arguments": {}})
        self.assertTrue(result.get("isError"))
        self.assertIn("port busy", result["content"][0]["text"])

    def test_a_successful_call_returns_json_text_content(self):
        client = FakeClient(responses={"list_ports": [{"device": "COM4"}]})
        with mock.patch.object(self.mod, "get_client", return_value=(client, "tcp:x")):
            result = self.mod._handle_tools_call({"name": "list_ports", "arguments": {}})
        self.assertNotIn("isError", result)
        self.assertEqual(json.loads(result["content"][0]["text"]), [{"device": "COM4"}])


class ClientLifecycleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = _load_mcp()

    def test_a_sidecar_mode_client_is_closed_after_the_call(self):
        client = FakeClient(responses={"list_ports": []})
        with mock.patch.object(self.mod, "get_client", return_value=(client, "sidecar")):
            self.mod._tool_list_ports({})
        self.assertTrue(client.closed)

    def test_a_tcp_mode_client_is_left_open_after_the_call(self):
        client = FakeClient(responses={"list_ports": []})
        with mock.patch.object(self.mod, "get_client", return_value=(client, "tcp:127.0.0.1:1")):
            self.mod._tool_list_ports({})
        self.assertFalse(client.closed)

    def test_connect_passes_device_and_baud_through(self):
        client = FakeClient(responses={"connect": {"ok": True}})
        with mock.patch.object(self.mod, "get_client", return_value=(client, "sidecar")):
            self.mod._tool_connect({"device": "COM4", "baud": 9600})
        self.assertIn(("connect", {"device": "COM4", "baud": 9600}), client.calls)


class FsToolTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = _load_mcp()

    def test_fs_read_returns_both_text_and_base64(self):
        data_b64 = base64.b64encode(b"hello board").decode()
        client = FakeClient(responses={"fs_read": {"data_b64": data_b64}})
        with mock.patch.object(self.mod, "get_client", return_value=(client, "sidecar")):
            result = self.mod._tool_fs_read({"path": "/a.py"})
        self.assertEqual(result["text"], "hello board")
        self.assertEqual(result["data_b64"], data_b64)

    def test_fs_write_encodes_plain_text_content_to_base64(self):
        client = FakeClient(responses={"fs_write": {"ok": True}})
        with mock.patch.object(self.mod, "get_client", return_value=(client, "sidecar")):
            self.mod._tool_fs_write({"path": "/a.py", "content": "print(1)"})
        method, params = client.calls[-1]
        self.assertEqual(method, "fs_write")
        self.assertEqual(base64.b64decode(params["data_b64"]), b"print(1)")
        self.assertFalse(params["mpy"])

    def test_fs_rm_recursive_uses_fs_rm_rf(self):
        client = FakeClient(responses={"fs_rm_rf": {"ok": True}})
        with mock.patch.object(self.mod, "get_client", return_value=(client, "sidecar")):
            self.mod._tool_fs_rm({"path": "/lib", "recursive": True})
        self.assertEqual(client.calls[-1][0], "fs_rm_rf")


class WatchReplToolTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = _load_mcp()

    def test_collects_repl_data_and_reports_errors_separately(self):
        client = FakeClient(
            responses={
                "_stream": [
                    ("repl_data", {"data_b64": base64.b64encode(b"stage 1\n").decode()}),
                    ("repl_error", {"message": "boom"}),
                ]
            }
        )
        with mock.patch.object(self.mod, "get_client", return_value=(client, "sidecar")):
            result = self.mod._tool_watch_repl({"duration": 1.0})
        self.assertEqual(result["text"], "stage 1\n")
        self.assertEqual(result["errors"], ["boom"])

    def test_duration_is_capped(self):
        client = FakeClient(responses={"_stream": []})
        captured = {}

        def fake_stream(on_notify, duration=None):
            captured["duration"] = duration

        client.stream_repl = fake_stream
        with mock.patch.object(self.mod, "get_client", return_value=(client, "sidecar")):
            self.mod._tool_watch_repl({"duration": 9999})
        self.assertEqual(captured["duration"], 120.0)


class ProbeToolTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = _load_mcp()

    def test_probe_tool_delegates_to_run_probe(self):
        client = FakeClient(responses={"run_script": {"ok": True}})
        with mock.patch.object(
            self.mod, "get_client", return_value=(client, "sidecar")
        ), mock.patch("mpftp.cli.Path") as fake_path:
            fake_path.return_value.read_text.return_value = "print(1)"
            result = self.mod._tool_probe({"file": "probe.py"})
        self.assertEqual(result["ok"], True)
        self.assertEqual(result["script"], "probe.py")


class FirmwareToolTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = _load_mcp()

    def test_firmware_build_forwards_selection_and_clean(self):
        with mock.patch.object(self.mod, "_engine_stream", return_value={"ok": True}) as stream:
            result = self.mod._tool_firmware_build(
                {"port": "esp32", "board": "ESP32_GENERIC", "clean": True}
            )
        self.assertEqual(result, {"ok": True})
        cmd, extra = stream.call_args.args
        self.assertEqual(cmd, "build")
        self.assertIn("--port", extra)
        self.assertIn("esp32", extra)
        self.assertIn("--clean", extra)

    def test_firmware_flash_forwards_device_and_uf2_flags(self):
        with mock.patch.object(self.mod, "_engine_stream", return_value={"ok": True}) as stream:
            self.mod._tool_firmware_flash({"port": "rp2", "device": "COM9", "uf2": True})
        _cmd, extra = stream.call_args.args
        self.assertIn("--device", extra)
        self.assertIn("COM9", extra)
        self.assertIn("--uf2", extra)


if __name__ == "__main__":
    unittest.main()
