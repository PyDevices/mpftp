"""The browser's File Transfer panel host (mpftp.panel): the port of the VS Code
extension's FtpViewProvider. A fake sidecar stands in for the board; the local
side is a real temporary directory. No board, browser or built UI required.
"""

from __future__ import annotations

import base64
import hashlib
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock


class FakeBoard:
    """Just enough of the sidecar's fs_* surface for the panel."""

    def __init__(self) -> None:
        self.files: dict[str, bytes] = {}
        self.dirs: set[str] = {"/"}
        self.calls: list[str] = []

    def __call__(self, method: str, params=None):
        p = params or {}
        self.calls.append(method)
        path = p.get("path")
        if method == "fs_listdir":
            base = path.rstrip("/") or ""
            out = []
            for d in sorted(self.dirs):
                if d != "/" and d.rsplit("/", 1)[0] == base:
                    out.append({"name": d.rsplit("/", 1)[1], "isDir": True, "size": 0})
            for f, data in sorted(self.files.items()):
                if f.rsplit("/", 1)[0] == base:
                    out.append({"name": f.rsplit("/", 1)[1], "isDir": False, "size": len(data)})
            return out
        if method == "fs_stat":
            if path in self.dirs:
                return {"isDir": True, "size": 0}
            return {"isDir": False, "size": len(self.files[path])}
        if method == "fs_mkdir":
            self.dirs.add(path)
            return {"ok": True}
        if method == "fs_write" or method == "edit_push":
            self.files[path] = base64.b64decode(p["data_b64"])
            return {"ok": True}
        if method in ("fs_read", "edit_pull"):
            return {"data_b64": base64.b64encode(self.files[path]).decode()}
        if method == "fs_hash":
            return {"algo": "sha256", "hash": hashlib.sha256(self.files[path]).hexdigest()}
        if method == "fs_touch":
            self.files.setdefault(path, b"")
            return {"ok": True}
        if method == "fs_rm_rf":
            self.files = {
                k: v for k, v in self.files.items() if not (k == path or k.startswith(path + "/"))
            }
            self.dirs = {d for d in self.dirs if not (d == path or d.startswith(path + "/"))}
            return {"ok": True}
        if method == "fs_rename":
            self.files[p["dest"]] = self.files.pop(p["src"])
            return {"ok": True}
        if method in ("transfer_begin", "transfer_end", "exec", "run_path"):
            return {}
        raise AssertionError(f"unexpected sidecar call {method}")


class PanelHostTests(unittest.TestCase):
    def setUp(self):
        from mpftp import panel

        self.mod = panel
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        proj = self.root / "proj"
        (proj / "lib" / "sub").mkdir(parents=True)
        (proj / "main.py").write_text("print(1)\n")
        (proj / "lib" / "util.py").write_text("X = 1\n")
        (proj / "lib" / "sub" / "blob.bin").write_bytes(bytes(range(256)) * 4)
        (proj / "__pycache__").mkdir()
        (proj / "__pycache__" / "x.pyc").write_bytes(b"junk")
        (self.root / ".hidden").write_text("no")
        self.board = FakeBoard()
        self.sent: list[dict] = []
        self.answers: list = []
        self.tab = object()

        def send(tab, msg):
            self.sent.append(msg)
            if msg.get("action") == "ask":
                # Answer on another thread, as the page would over the socket.
                value = self.answers.pop(0)
                threading.Thread(target=self.host.answer, args=(msg["askId"], value)).start()
            return True

        self.host = panel.PanelHost(self.board, send, lambda: [self.tab], str(self.root))
        self.host.connected_device = "COM42"
        self.host.interpreter = "micropython"
        # verifyTransfers on, compileOnUpload off: the extension's defaults.
        patcher = mock.patch.object(
            panel.PanelHost, "_setting", staticmethod(lambda name, default: default)
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def states(self):
        return [
            m["msg"] for m in self.sent if m.get("type") == "panel" and m["msg"]["type"] == "state"
        ]

    def statuses(self):
        return [
            m["msg"]["text"]
            for m in self.sent
            if m.get("type") == "panel" and m["msg"]["type"] == "status"
        ]

    def test_ready_pushes_both_lists_and_skips_noise(self):
        self.board.files["/boot.py"] = b"x"
        self.host.handle(self.tab, {"type": "ready"})
        state = self.states()[-1]
        self.assertTrue(state["connected"])
        self.assertEqual(state["localPath"], str(self.root))
        self.assertEqual([e["name"] for e in state["localEntries"]], ["proj"])
        self.assertEqual([e["name"] for e in state["remoteEntries"]], ["boot.py"])
        self.assertEqual(self.statuses()[-1], "Connected · COM42")

    def test_upload_and_download_round_trip_a_folder_tree(self):
        proj = self.root / "proj"
        self.host.handle(self.tab, {"type": "upload", "localPaths": [str(proj)]})
        self.assertEqual(
            sorted(self.board.files),
            ["/proj/lib/sub/blob.bin", "/proj/lib/util.py", "/proj/main.py"],
        )
        self.assertIn("Uploaded 3 file(s), skipped 1 ignored entry", self.statuses())
        self.assertIn("fs_hash", self.board.calls)  # verified, as verifyTransfers asks

        back = self.root / "back"
        back.mkdir()
        self.host.handle(self.tab, {"type": "localCd", "path": str(back)})
        self.host.handle(self.tab, {"type": "download", "remotePaths": ["/proj"]})
        self.assertIn("Downloaded 3 file(s)", self.statuses())
        for rel in ("main.py", "lib/util.py", "lib/sub/blob.bin"):
            self.assertEqual((back / "proj" / rel).read_bytes(), (proj / rel).read_bytes())
        self.assertFalse((back / "proj" / "__pycache__").exists())

    def test_local_delete_asks_first_and_cancel_keeps_the_files(self):
        target = self.root / "proj"
        self.answers = [None]
        self.host.handle(self.tab, {"type": "localRm", "localPaths": [str(target)]})
        self.assertTrue(target.exists())
        ask = [m for m in self.sent if m.get("action") == "ask"][-1]
        self.assertEqual(
            (ask["kind"], ask["prompt"], ask["okLabel"]),
            ("confirm", "Delete 1 local item(s)?", "Delete"),
        )
        self.answers = [True]
        self.host.handle(self.tab, {"type": "localRm", "localPaths": [str(target)]})
        self.assertFalse(target.exists())

    def test_board_mkdir_rename_and_delete(self):
        self.answers = ["lib2"]
        self.host.handle(self.tab, {"type": "mkdir"})
        self.assertIn("/lib2", self.board.dirs)
        self.board.files["/a.py"] = b"1"
        self.answers = ["b.py"]
        self.host.handle(self.tab, {"type": "remoteRename", "path": "/a.py"})
        self.assertEqual(sorted(self.board.files), ["/b.py"])
        ask = [m for m in self.sent if m.get("action") == "ask"][-1]
        self.assertEqual(ask["selection"], [0, 1])  # the stem, as VS Code selects it
        self.answers = [True]
        self.host.handle(self.tab, {"type": "rm", "remotePaths": ["/b.py", "/lib2"]})
        self.assertEqual(self.board.files, {})
        self.assertNotIn("/lib2", self.board.dirs)

    def test_local_new_file_rename_and_separator_refusal(self):
        self.answers = ["new.py"]
        self.host.handle(self.tab, {"type": "localNewFile"})
        self.assertTrue((self.root / "new.py").is_file())
        self.answers = ["a/b.py"]
        self.host.handle(self.tab, {"type": "localRename", "path": str(self.root / "new.py")})
        self.assertIn("Name cannot contain path separators", self.statuses())
        self.answers = ["renamed.py"]
        self.host.handle(self.tab, {"type": "localRename", "path": str(self.root / "new.py")})
        self.assertTrue((self.root / "renamed.py").is_file())

    def test_open_and_save_go_back_where_the_file_came_from(self):
        self.board.files["/main.py"] = b"print(1)\n"
        self.host.handle(self.tab, {"type": "openRemote", "path": "/main.py"})
        opened = [m for m in self.sent if m.get("action") == "open"][-1]
        self.assertEqual((opened["side"], opened["path"]), ("remote", "/main.py"))
        self.assertEqual(base64.b64decode(opened["data_b64"]), b"print(1)\n")

        data = base64.b64encode(b"print(2)\n").decode()
        self.host.handle(
            self.tab,
            {
                "type": "saveFile",
                "side": "remote",
                "path": "/main.py",
                "data_b64": data,
                "reqId": 7,
            },
        )
        self.assertEqual(self.board.files["/main.py"], b"print(2)\n")
        reply = [m for m in self.sent if m.get("action") == "reply"][-1]
        self.assertEqual((reply["reqId"], reply["ok"]), (7, True))

        local = self.root / "proj" / "main.py"
        self.host.handle(
            self.tab,
            {"type": "saveFile", "side": "local", "path": str(local), "data_b64": data, "reqId": 8},
        )
        self.assertEqual(local.read_bytes(), b"print(2)\n")

    def save_as(self, side, path, text=b"new\n", source=("", ""), req_id=20):
        self.host.handle(
            self.tab,
            {
                "type": "saveFileAs",
                "side": side,
                "path": path,
                "data_b64": base64.b64encode(text).decode(),
                "sourceSide": source[0],
                "sourcePath": source[1],
                "reqId": req_id,
            },
        )
        reply = [m for m in self.sent if m.get("action") == "reply"][-1]
        self.assertEqual(reply["reqId"], req_id)
        return reply

    def asks(self):
        return [m for m in self.sent if m.get("action") == "ask"]

    def test_save_as_to_another_board_folder_leaves_the_original(self):
        self.board.files["/main.py"] = b"print(1)\n"
        self.board.dirs.add("/lib")
        reply = self.save_as("remote", "/lib/copy.py", b"print(2)\n", ("remote", "/main.py"))
        self.assertEqual((reply["ok"], reply["side"], reply["path"]), (True, "remote", "/lib/copy.py"))
        self.assertEqual(self.board.files["/lib/copy.py"], b"print(2)\n")
        self.assertEqual(self.board.files["/main.py"], b"print(1)\n")
        self.assertEqual(self.asks(), [])  # nothing was there, so nothing to ask
        self.assertTrue(self.states())  # the lists redraw

    def test_save_as_takes_a_relative_path_from_that_lists_folder(self):
        self.board.dirs.add("/lib")
        self.host.remote_path = "/lib"
        reply = self.save_as("remote", "./a/../b.py")
        self.assertEqual(reply["path"], "/lib/b.py")
        self.host.local_path = str(self.root / "proj")
        reply = self.save_as("local", "c.py")
        self.assertEqual(reply["path"], str(self.root / "proj" / "c.py"))
        self.assertEqual((self.root / "proj" / "c.py").read_bytes(), b"new\n")

    def test_save_as_asks_before_replacing_and_cancel_keeps_the_file(self):
        self.board.files["/boot.py"] = b"old\n"
        self.answers = [None]
        reply = self.save_as("remote", "/boot.py")
        self.assertEqual((reply["ok"], reply.get("cancelled")), (False, True))
        self.assertEqual(self.board.files["/boot.py"], b"old\n")
        ask = self.asks()[-1]
        self.assertEqual(
            (ask["kind"], ask["prompt"], ask["okLabel"]),
            ("confirm", "/boot.py already exists on the board. Replace it?", "Replace"),
        )
        self.answers = [True]
        self.assertTrue(self.save_as("remote", "/boot.py")["ok"])
        self.assertEqual(self.board.files["/boot.py"], b"new\n")

        local = self.root / "proj" / "main.py"
        self.answers = [None]
        self.assertFalse(self.save_as("local", str(local))["ok"])
        self.assertEqual(local.read_bytes(), b"print(1)\n")
        self.assertIn("already exists on this computer", self.asks()[-1]["prompt"])

    def test_save_as_onto_its_own_file_does_not_ask(self):
        self.board.files["/main.py"] = b"old\n"
        reply = self.save_as("remote", "/main.py", source=("remote", "/main.py"))
        self.assertTrue(reply["ok"])
        self.assertEqual(self.asks(), [])

    def test_save_as_into_a_missing_folder_says_so(self):
        reply = self.save_as("remote", "/nowhere/x.py")
        self.assertFalse(reply["ok"])
        self.assertEqual(reply["error"], "folder doesn't exist on the board: /nowhere")
        self.assertNotIn("/nowhere/x.py", self.board.files)
        reply = self.save_as("local", str(self.root / "nowhere" / "x.py"))
        self.assertIn("folder doesn't exist", reply["error"])
        self.assertFalse((self.root / "nowhere").exists())

    def test_save_as_refuses_a_folder_as_the_target(self):
        self.board.dirs.add("/lib")
        self.assertIn("is a folder", self.save_as("remote", "/lib")["error"])
        self.assertIn("not just a folder", self.save_as("remote", "/lib/")["error"])
        self.assertIn("is a folder", self.save_as("local", str(self.root / "proj"))["error"])

    def test_save_as_between_sides(self):
        local = self.root / "proj" / "main.py"
        reply = self.save_as("remote", "/main.py", b"print(1)\n", ("local", str(local)))
        self.assertEqual((reply["side"], reply["path"]), ("remote", "/main.py"))
        self.assertEqual(self.board.files["/main.py"], b"print(1)\n")
        dest = self.root / "from_board.py"
        reply = self.save_as("local", str(dest), b"x = 2\n", ("remote", "/main.py"))
        self.assertEqual((reply["side"], reply["path"]), ("local", str(dest)))
        self.assertEqual(dest.read_bytes(), b"x = 2\n")

    def test_save_as_to_the_board_while_disconnected_fails(self):
        self.host.connected_device = ""
        reply = self.save_as("remote", "/x.py")
        self.assertEqual((reply["ok"], reply["error"]), (False, "not connected"))
        self.assertEqual(self.board.calls, [])

    def test_board_operations_while_disconnected_report_it(self):
        self.host.connected_device = ""
        self.host.handle(self.tab, {"type": "upload", "localPaths": [str(self.root / "proj")]})
        self.assertIn("not connected", self.statuses())
        state = self.states()
        self.assertEqual(self.board.calls, [])
        self.assertEqual(state, [])  # the error stops the message, as in VS Code

    def test_a_closed_tab_cancels_its_open_question(self):
        sent_ask = threading.Event()
        host = self.mod.PanelHost(
            self.board, lambda tab, msg: sent_ask.set() or True, lambda: [self.tab], str(self.root)
        )
        result = []
        t = threading.Thread(target=lambda: result.append(host.ask_input(self.tab, "Name?")))
        t.start()
        sent_ask.wait(2)
        host.forget_tab(self.tab)
        t.join(2)
        self.assertEqual(result, [None])


class RelayPanelRoutingTests(unittest.TestCase):
    def _relay(self):
        from mpftp import pwa

        proc = mock.Mock()
        proc.stdin = mock.Mock()
        proc.stdout = [""]
        with mock.patch.object(pwa.subprocess, "Popen", return_value=proc):
            relay = pwa.SidecarRelay("python3", local_path="/tmp")
        relay._reader.join(timeout=2)
        return relay, proc

    def test_a_server_request_is_answered_by_the_matching_reply(self):
        relay, proc = self._relay()
        out = []
        t = threading.Thread(target=lambda: out.append(relay.request("fs_stat", {"path": "/"})))
        t.start()
        for _ in range(100):
            if proc.stdin.write.call_args:
                break
            threading.Event().wait(0.01)
        sent = json.loads(proc.stdin.write.call_args[0][0])
        relay._route(json.dumps({"type": "result", "id": sent["id"], "result": {"isDir": True}}))
        t.join(2)
        self.assertEqual(out, [{"isDir": True}])

    def test_a_connect_result_tells_the_panel(self):
        relay, proc = self._relay()
        ws = mock.Mock()
        relay.subscribe(ws)
        with mock.patch.object(relay.panel, "set_connection") as set_connection:
            relay.send(
                json.dumps({"id": 1, "method": "connect", "params": {"device": "COM42"}}), ws
            )
            sent = json.loads(proc.stdin.write.call_args[0][0])
            relay._route(
                json.dumps(
                    {
                        "type": "result",
                        "id": sent["id"],
                        "result": {"device": "COM42", "interpreter": "micropython"},
                    }
                )
            )
            set_connection.assert_called_with("COM42", "micropython")
            relay._route(json.dumps({"type": "notify", "method": "transport_dead", "params": {}}))
            set_connection.assert_called_with("")

    def test_panel_messages_do_not_reach_the_sidecar(self):
        relay, proc = self._relay()
        ws = mock.Mock()
        with mock.patch.object(relay.panel, "handle") as handle:
            relay.send(json.dumps({"panel": {"type": "refreshLocal"}}), ws)
            for _ in range(100):
                if handle.called:
                    break
                threading.Event().wait(0.01)
        handle.assert_called_with(ws, {"type": "refreshLocal"})
        proc.stdin.write.assert_not_called()


if __name__ == "__main__":
    unittest.main()
