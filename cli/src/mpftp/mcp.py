#!/usr/bin/env python3
"""
mpftp MCP server — stdio JSON-RPC 2.0 (Model Context Protocol).

Exposes the same device / filesystem / REPL / firmware / probe operations as
`mpftp.cli` as typed MCP tools, reusing the same dual-transport RpcClient
(a running extension's agent RPC, or a private standalone sidecar) so an
agent talking MCP gets the exact same session semantics as the CLI.

Run: python -m mpftp.mcp
"""

from __future__ import annotations

import base64
import json
import sys
import types
from typing import Any, Callable, Optional

from . import __version__
from .cli import (
    RpcClient,
    _engine_json,
    _engine_stream,
    _sel_args,
    ensure_device,
    get_client,
    run_probe,
)

PROTOCOL_VERSION = "2024-11-05"
SERVER_NAME = "mpftp"

DEVICE_PROPS: dict[str, Any] = {
    "device": {
        "type": "string",
        "description": "Serial device to connect first (e.g. COM4, /dev/ttyACM0). "
        "Omit to use the board a session is already connected to.",
    },
    "baud": {"type": "integer", "description": "Baud rate.", "default": 115200},
}


def _with_client(device: Optional[str], baud: int, fn: Callable[[RpcClient], Any]) -> Any:
    client, mode = get_client()
    try:
        ensure_device(client, device, baud or 115200)
        return fn(client)
    finally:
        if mode.startswith("sidecar"):
            client.close()


def _text_result(payload: Any, *, is_error: bool = False) -> dict[str, Any]:
    text = payload if isinstance(payload, str) else json.dumps(payload, indent=2)
    result: dict[str, Any] = {"content": [{"type": "text", "text": text}]}
    if is_error:
        result["isError"] = True
    return result


# --- tool handlers ---------------------------------------------------------


def _tool_list_ports(args: dict) -> Any:
    return _with_client(None, 0, lambda c: c.call("list_ports"))


def _tool_connect(args: dict) -> Any:
    device = args["device"]
    baud = int(args.get("baud", 115200))
    return _with_client(device, baud, lambda c: c.call("connect", {"device": device, "baud": baud}))


def _tool_disconnect(args: dict) -> Any:
    return _with_client(None, 0, lambda c: c.call("disconnect"))


def _tool_fs_ls(args: dict) -> Any:
    path = args.get("path", "/")
    return _with_client(
        args.get("device"), args.get("baud", 115200), lambda c: c.call("fs_listdir", {"path": path})
    )


def _tool_fs_tree(args: dict) -> Any:
    path = args.get("path", "/")
    return _with_client(
        args.get("device"), args.get("baud", 115200), lambda c: c.call("fs_tree", {"path": path})
    )


def _tool_fs_read(args: dict) -> Any:
    path = args["path"]

    def op(c: RpcClient) -> Any:
        res = c.call("fs_read", {"path": path})
        raw = base64.b64decode(res["data_b64"])
        return {
            "path": path,
            "size": len(raw),
            "text": raw.decode("utf-8", "replace"),
            "data_b64": res["data_b64"],
        }

    return _with_client(args.get("device"), args.get("baud", 115200), op)


def _tool_fs_write(args: dict) -> Any:
    path = args["path"]
    if "data_b64" in args:
        data_b64 = args["data_b64"]
    else:
        data_b64 = base64.b64encode(args.get("content", "").encode("utf-8")).decode("ascii")
    params = {
        "path": path,
        "data_b64": data_b64,
        "mpy": bool(args.get("mpy", False)),
        "verify": bool(args.get("verify", False)),
    }
    return _with_client(
        args.get("device"), args.get("baud", 115200), lambda c: c.call("fs_write", params)
    )


def _tool_fs_cp(args: dict) -> Any:
    params = {
        "src": args["src"],
        "dest": args["dest"],
        "verify": bool(args.get("verify", False)),
        "mpy": bool(args.get("mpy", False)),
    }
    return _with_client(
        args.get("device"), args.get("baud", 115200), lambda c: c.call("fs_cp", params)
    )


def _tool_fs_rm(args: dict) -> Any:
    path = args["path"]
    method = "fs_rm_rf" if args.get("recursive") else "fs_rm"
    return _with_client(
        args.get("device"), args.get("baud", 115200), lambda c: c.call(method, {"path": path})
    )


def _tool_fs_mkdir(args: dict) -> Any:
    path = args["path"]
    return _with_client(
        args.get("device"), args.get("baud", 115200), lambda c: c.call("fs_mkdir", {"path": path})
    )


def _tool_fs_hash(args: dict) -> Any:
    path = args["path"]
    algo = args.get("algo", "sha256")
    return _with_client(
        args.get("device"),
        args.get("baud", 115200),
        lambda c: c.call("fs_hash", {"path": path, "algo": algo}),
    )


def _tool_exec_code(args: dict) -> Any:
    code = args["code"]
    follow = bool(args.get("follow", True))
    return _with_client(
        args.get("device"),
        args.get("baud", 115200),
        lambda c: c.call("exec", {"code": code, "follow": follow}),
    )


def _tool_eval_expr(args: dict) -> Any:
    expr = args["expr"]
    return _with_client(
        args.get("device"), args.get("baud", 115200), lambda c: c.call("eval", {"expr": expr})
    )


def _tool_run_script(args: dict) -> Any:
    source = args["source"]
    follow = bool(args.get("follow", False))
    return _with_client(
        args.get("device"),
        args.get("baud", 115200),
        lambda c: c.call("run_script", {"source": source, "follow": follow}),
    )


def _tool_run_path(args: dict) -> Any:
    path = args["path"]
    follow = bool(args.get("follow", False))
    return _with_client(
        args.get("device"),
        args.get("baud", 115200),
        lambda c: c.call("run_path", {"path": path, "follow": follow}),
    )


def _tool_watch_repl(args: dict) -> Any:
    duration = min(float(args.get("duration", 5.0)), 120.0)

    def op(c: RpcClient) -> Any:
        chunks: list[bytes] = []
        errors: list[str] = []

        def on_notify(method: str, params: dict) -> None:
            if method == "repl_data":
                chunks.append(base64.b64decode(params.get("data_b64", "")))
            elif method == "repl_error":
                errors.append(str(params.get("message", "")))

        c.stream_repl(on_notify, duration=duration)
        return {
            "text": b"".join(chunks).decode("utf-8", "replace"),
            "errors": errors,
            "duration": duration,
        }

    return _with_client(args.get("device"), args.get("baud", 115200), op)


def _tool_interrupt(args: dict) -> Any:
    return _with_client(
        args.get("device"), args.get("baud", 115200), lambda c: c.call("interrupt")
    )


def _tool_soft_reset(args: dict) -> Any:
    return _with_client(
        args.get("device"), args.get("baud", 115200), lambda c: c.call("soft_reset")
    )


def _tool_soft_reboot(args: dict) -> Any:
    return _with_client(
        args.get("device"), args.get("baud", 115200), lambda c: c.call("soft_reboot")
    )


def _tool_hard_reset(args: dict) -> Any:
    return _with_client(
        args.get("device"), args.get("baud", 115200), lambda c: c.call("hard_reset")
    )


def _tool_probe(args: dict) -> Any:
    def op(c: RpcClient) -> Any:
        return run_probe(
            c,
            device=args.get("device"),
            baud=int(args.get("baud", 115200)),
            file=args["file"],
            reboot_first=bool(args.get("reboot_first", False)),
            capture=args.get("capture"),
            wait=float(args.get("wait", 0.0)),
        )

    # run_probe calls ensure_device itself; go through get_client directly
    # rather than _with_client (which would call ensure_device twice).
    client, mode = get_client()
    try:
        return op(client)
    finally:
        if mode.startswith("sidecar"):
            client.close()


def _tool_firmware_discover(args: dict) -> Any:
    extra = ["--mp", args["mp"]] if args.get("mp") else []
    return _engine_json("discover", extra)


def _tool_firmware_tree(args: dict) -> Any:
    extra = ["--mp", args["mp"]] if args.get("mp") else []
    return _engine_json("tree", extra)


def _tool_firmware_build(args: dict) -> Any:
    ns = types.SimpleNamespace(
        mp=args.get("mp"), port=args.get("port"), board=args.get("board"), variant=args.get("variant")
    )
    extra = _sel_args(ns)
    if args.get("clean"):
        extra.append("--clean")
    return _engine_stream("build", extra)


def _tool_firmware_flash(args: dict) -> Any:
    ns = types.SimpleNamespace(
        mp=args.get("mp"), port=args.get("port"), board=args.get("board"), variant=args.get("variant")
    )
    extra = _sel_args(ns)
    if args.get("device"):
        extra += ["--device", args["device"]]
    if args.get("artifact"):
        extra += ["--artifact", args["artifact"]]
    if args.get("family"):
        extra += ["--family", args["family"]]
    if args.get("erase"):
        extra.append("--erase")
    if args.get("uf2"):
        extra.append("--uf2")
    if args.get("uf2_timeout"):
        extra += ["--uf2-timeout", str(args["uf2_timeout"])]
    return _engine_stream("flash", extra)


TOOLS: list[dict[str, Any]] = [
    {
        "name": "list_ports",
        "description": "List serial ports the board might be on, with vid/pid/interface role hints.",
        "inputSchema": {"type": "object", "properties": {}},
        "handler": _tool_list_ports,
    },
    {
        "name": "connect",
        "description": "Connect to a board over serial.",
        "inputSchema": {
            "type": "object",
            "properties": DEVICE_PROPS,
            "required": ["device"],
        },
        "handler": _tool_connect,
    },
    {
        "name": "disconnect",
        "description": "Disconnect the current board session.",
        "inputSchema": {"type": "object", "properties": {}},
        "handler": _tool_disconnect,
    },
    {
        "name": "fs_ls",
        "description": "List a board directory.",
        "inputSchema": {
            "type": "object",
            "properties": {**DEVICE_PROPS, "path": {"type": "string", "default": "/"}},
        },
        "handler": _tool_fs_ls,
    },
    {
        "name": "fs_tree",
        "description": "Recursively list a board directory tree.",
        "inputSchema": {
            "type": "object",
            "properties": {**DEVICE_PROPS, "path": {"type": "string", "default": "/"}},
        },
        "handler": _tool_fs_tree,
    },
    {
        "name": "fs_read",
        "description": "Read a board file. Returns both best-effort UTF-8 text and base64 bytes.",
        "inputSchema": {
            "type": "object",
            "properties": {**DEVICE_PROPS, "path": {"type": "string"}},
            "required": ["path"],
        },
        "handler": _tool_fs_read,
    },
    {
        "name": "fs_write",
        "description": "Write a board file from text content (or raw base64 bytes via data_b64). "
        "Set mpy to compile .py to .mpy via mpy-cross first (MicroPython only).",
        "inputSchema": {
            "type": "object",
            "properties": {
                **DEVICE_PROPS,
                "path": {"type": "string"},
                "content": {"type": "string", "description": "UTF-8 text to write."},
                "data_b64": {"type": "string", "description": "Raw bytes, base64-encoded."},
                "mpy": {"type": "boolean", "default": False},
                "verify": {"type": "boolean", "default": False},
            },
            "required": ["path"],
        },
        "handler": _tool_fs_write,
    },
    {
        "name": "fs_cp",
        "description": "Copy local<->board or board<->board (prefix a board path with ':').",
        "inputSchema": {
            "type": "object",
            "properties": {
                **DEVICE_PROPS,
                "src": {"type": "string"},
                "dest": {"type": "string"},
                "verify": {"type": "boolean", "default": False},
                "mpy": {"type": "boolean", "default": False},
            },
            "required": ["src", "dest"],
        },
        "handler": _tool_fs_cp,
    },
    {
        "name": "fs_rm",
        "description": "Remove a board file, or a whole tree with recursive.",
        "inputSchema": {
            "type": "object",
            "properties": {
                **DEVICE_PROPS,
                "path": {"type": "string"},
                "recursive": {"type": "boolean", "default": False},
            },
            "required": ["path"],
        },
        "handler": _tool_fs_rm,
    },
    {
        "name": "fs_mkdir",
        "description": "Create a board directory.",
        "inputSchema": {
            "type": "object",
            "properties": {**DEVICE_PROPS, "path": {"type": "string"}},
            "required": ["path"],
        },
        "handler": _tool_fs_mkdir,
    },
    {
        "name": "fs_hash",
        "description": "SHA-256 (or other algo) of a board file.",
        "inputSchema": {
            "type": "object",
            "properties": {
                **DEVICE_PROPS,
                "path": {"type": "string"},
                "algo": {"type": "string", "default": "sha256"},
            },
            "required": ["path"],
        },
        "handler": _tool_fs_hash,
    },
    {
        "name": "exec_code",
        "description": "Execute code on the board in raw REPL and return its output.",
        "inputSchema": {
            "type": "object",
            "properties": {
                **DEVICE_PROPS,
                "code": {"type": "string"},
                "follow": {"type": "boolean", "default": True},
            },
            "required": ["code"],
        },
        "handler": _tool_exec_code,
    },
    {
        "name": "eval_expr",
        "description": "Evaluate an expression on the board and return its repr.",
        "inputSchema": {
            "type": "object",
            "properties": {**DEVICE_PROPS, "expr": {"type": "string"}},
            "required": ["expr"],
        },
        "handler": _tool_eval_expr,
    },
    {
        "name": "run_script",
        "description": "Run local script source on the board (soft-reset + exec). "
        "follow=false (default) returns immediately without waiting for the script to finish — "
        "use for anything that loops; pair with watch_repl or probe to see its output.",
        "inputSchema": {
            "type": "object",
            "properties": {
                **DEVICE_PROPS,
                "source": {"type": "string"},
                "follow": {"type": "boolean", "default": False},
            },
            "required": ["source"],
        },
        "handler": _tool_run_script,
    },
    {
        "name": "run_path",
        "description": "Run a file already on the board (soft-reset + exec).",
        "inputSchema": {
            "type": "object",
            "properties": {
                **DEVICE_PROPS,
                "path": {"type": "string"},
                "follow": {"type": "boolean", "default": False},
            },
            "required": ["path"],
        },
        "handler": _tool_run_path,
    },
    {
        "name": "watch_repl",
        "description": "Tail the board's own stdout for `duration` seconds without ever entering "
        "raw REPL — the running script keeps running. The only way to see progress from a script "
        "started with run_script/run_path follow=false without interrupting it. Returns the "
        "captured text; call again to keep watching.",
        "inputSchema": {
            "type": "object",
            "properties": {
                **DEVICE_PROPS,
                "duration": {
                    "type": "number",
                    "default": 5.0,
                    "description": "Seconds to capture, capped at 120.",
                },
            },
        },
        "handler": _tool_watch_repl,
    },
    {
        "name": "interrupt",
        "description": "Send Ctrl-C. Does not reset the board.",
        "inputSchema": {"type": "object", "properties": DEVICE_PROPS},
        "handler": _tool_interrupt,
    },
    {
        "name": "soft_reset",
        "description": "MicroPython raw soft-reset (skips main.py). CircuitPython: "
        "friendly<->raw toggle (does not run code.py).",
        "inputSchema": {"type": "object", "properties": DEVICE_PROPS},
        "handler": _tool_soft_reset,
    },
    {
        "name": "soft_reboot",
        "description": "Ctrl-D soft reboot; runs main.py / code.py.",
        "inputSchema": {"type": "object", "properties": DEVICE_PROPS},
        "handler": _tool_soft_reboot,
    },
    {
        "name": "hard_reset",
        "description": "Hard reset the board (DTR/RTS or 1200bps touch as applicable).",
        "inputSchema": {"type": "object", "properties": DEVICE_PROPS},
        "handler": _tool_hard_reset,
    },
    {
        "name": "probe",
        "description": "Run a local script, optionally wait, optionally capture a result file, "
        "in one call — the pattern for anything that outlives a single raw-REPL session. "
        "reboot_first hard-resets and reconnects first (clears stale module state); requires device.",
        "inputSchema": {
            "type": "object",
            "properties": {
                **DEVICE_PROPS,
                "file": {"type": "string", "description": "Local script path to run."},
                "reboot_first": {"type": "boolean", "default": False},
                "capture": {"type": "string", "description": "Board path to read back after wait."},
                "wait": {"type": "number", "default": 0.0, "description": "Seconds to sleep before capture."},
            },
            "required": ["file"],
        },
        "handler": _tool_probe,
    },
    {
        "name": "firmware_discover",
        "description": "Resolve the MicroPython tree, SDK paths, and firmware workspace.",
        "inputSchema": {
            "type": "object",
            "properties": {"mp": {"type": "string", "description": "Explicit MicroPython tree path."}},
        },
        "handler": _tool_firmware_discover,
    },
    {
        "name": "firmware_tree",
        "description": "List ports -> boards -> variants available to build.",
        "inputSchema": {
            "type": "object",
            "properties": {"mp": {"type": "string", "description": "Explicit MicroPython tree path."}},
        },
        "handler": _tool_firmware_tree,
    },
    {
        "name": "firmware_build",
        "description": "Build MicroPython firmware for a port/board/variant (make submodules + all). "
        "Can take minutes; the log is captured and returned on completion.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "mp": {"type": "string"},
                "port": {"type": "string"},
                "board": {"type": "string"},
                "variant": {"type": "string"},
                "clean": {"type": "boolean", "default": False},
            },
            "required": ["port"],
        },
        "handler": _tool_firmware_build,
    },
    {
        "name": "firmware_flash",
        "description": "Flash a built (or explicit) firmware artifact to a device.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "mp": {"type": "string"},
                "port": {"type": "string"},
                "board": {"type": "string"},
                "variant": {"type": "string"},
                "device": {"type": "string"},
                "artifact": {"type": "string", "description": "Explicit firmware file (else last build)."},
                "family": {"type": "string"},
                "erase": {"type": "boolean", "default": False},
                "uf2": {"type": "boolean", "default": False},
                "uf2_timeout": {"type": "number"},
            },
            "required": ["port"],
        },
        "handler": _tool_firmware_flash,
    },
]

TOOLS_BY_NAME = {t["name"]: t for t in TOOLS}


# --- JSON-RPC / MCP framing --------------------------------------------------


def _send(msg: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(msg) + "\n")
    sys.stdout.flush()


def _reply(msg_id: Any, result: Any) -> None:
    _send({"jsonrpc": "2.0", "id": msg_id, "result": result})


def _reply_error(msg_id: Any, code: int, message: str) -> None:
    _send({"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}})


def _handle_initialize(params: dict) -> dict:
    return {
        "protocolVersion": PROTOCOL_VERSION,
        "capabilities": {"tools": {}},
        "serverInfo": {"name": SERVER_NAME, "version": __version__},
    }


def _handle_tools_list(params: dict) -> dict:
    return {
        "tools": [
            {"name": t["name"], "description": t["description"], "inputSchema": t["inputSchema"]}
            for t in TOOLS
        ]
    }


def _handle_tools_call(params: dict) -> dict:
    name = params.get("name")
    tool = TOOLS_BY_NAME.get(name)
    if tool is None:
        return _text_result(f"unknown tool: {name}", is_error=True)
    args = params.get("arguments") or {}
    try:
        result = tool["handler"](args)
    except Exception as e:  # a tool failure is a result, not a transport error
        return _text_result(str(e), is_error=True)
    return _text_result(result)


_METHODS: dict[str, Callable[[dict], Any]] = {
    "initialize": _handle_initialize,
    "tools/list": _handle_tools_list,
    "tools/call": _handle_tools_call,
    "ping": lambda params: {},
}


def _dispatch_line(line: str) -> None:
    try:
        msg = json.loads(line)
    except json.JSONDecodeError as e:
        _reply_error(None, -32700, f"parse error: {e}")
        return

    method = msg.get("method")
    msg_id = msg.get("id")
    is_notification = "id" not in msg

    if method == "notifications/initialized" or (
        isinstance(method, str) and method.startswith("notifications/")
    ):
        return  # notifications never get a response

    handler = _METHODS.get(method)
    if handler is None:
        if not is_notification:
            _reply_error(msg_id, -32601, f"method not found: {method}")
        return

    try:
        result = handler(msg.get("params") or {})
    except Exception as e:
        if not is_notification:
            _reply_error(msg_id, -32603, str(e))
        return

    if not is_notification:
        _reply(msg_id, result)


def main() -> None:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        _dispatch_line(line)


if __name__ == "__main__":
    main()
