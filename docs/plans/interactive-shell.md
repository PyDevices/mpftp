# Plan: `mpftp shell` — an interactive FTP-style REPL

Status: not started. Picked up later, independent of any other in-flight work.

## Context

The mpftp split/restructure plan is complete; this is unrelated follow-on work. The
question that prompted it: could mpftp speak real FTP-protocol commands instead of
mpremote-based ones? Real FTP (RFC 959) is infeasible as a wire-protocol swap — most
boards are reached over USB serial via raw-REPL, not TCP, and adopting it would drop
every non-networked board. What's actually wanted is the *ergonomics*: an interactive
session with `open`/`cd`/`pwd`/`get`/`put`/`mget`/`mput`/`bye` instead of one-shot
`mpftp <verb> <path>` invocations. This is purely additive — none of the existing flat
subcommands (`ls`, `get`, `put`, `rm`, `mkdir`, …) are renamed or touched, since
scripts, the VS Code extension, and the MCP server all depend on them as-is.

## Design

- New `cli/src/mpftp/shell.py`, a `FtpShell(cmd.Cmd)` subclass (stdlib `cmd` — no new
  dependency, matches the project's stdlib-only bias seen in `pwa.py`'s hand-rolled
  WebSocket). `cmd.Cmd`'s `do_*` methods are individually callable via
  `shell.onecmd("ls")` without driving the real `input()` loop, which is exactly the
  test pattern `test_mip.py` already uses (`FakeClient` + direct calls +
  `redirect_stdout`).
- Board paths have no server-side cwd concept (confirmed: zero `cwd`/`chdir`/`getcwd`
  hits in `sidecar.py`; every `fs_*` RPC takes an absolute path string). `FtpShell`
  tracks `self.board_cwd: str = "/"` client-side and resolves every path through:
  ```python
  def _resolve(self, path: str) -> str:
      if not path:
          return self.board_cwd
      if path.startswith("/"):
          return posixpath.normpath(path)
      joined = posixpath.normpath(posixpath.join(self.board_cwd, path))
      return joined if joined != "." else "/"
  ```
  (handles `cd ..`, `cd /`, and absolute get/put paths bypassing `board_cwd` for free).
  `cd` validates the target by calling `fs_listdir(new_path)` and catching the error —
  cheaper and more correct than stat + bit-masking `st_mode`, and reuses `ls`'s error
  path.
- Local-side `lcd` just calls `os.chdir`; `get`/`put` resolve local paths via
  `Path(local).expanduser()` against the real `os.getcwd()` — no separate local-cwd
  field needed.
- `RpcClient.call()` raises `RuntimeError` on RPC errors (never returns an
  error-shaped dict — confirmed in both `TcpClient.call` and `SidecarClient.call`).
  Every `do_*` body runs through a `_safe(self, fn, *a, **kw)` wrapper that catches
  `Exception`, prints `?{e}`, and returns — a failed command must never crash the
  REPL loop, unlike every existing one-shot `cmd_*` in `cli.py` where an uncaught
  exception propagating to `SystemExit(1)` is correct.
- Command surface: `open [device] [baud]`, `close`, `bye`/`quit`/`EOF` (aliased to one
  impl, does **not** call `client.close()` itself — that stays the caller's job,
  matching every existing `cmd_*`'s separation of concerns), `ls`/`dir` (aliased),
  `cd`, `pwd`, `lcd`, `get`, `put`, `mget` (fnmatch against one
  `fs_listdir(board_cwd)` call), `mput` (stdlib `glob.glob` in the local cwd),
  `delete`/`rm` (alias), `mkdir`, `prompt` (toggles y/n confirm for mget/mput,
  default on), `ascii`/`binary` (one-line no-op informational prints — board fs is
  always byte-exact), `status`, `help` (free from `cmd.Cmd`).
- `get`/`put` are a deliberately trimmed reimplementation (no `--mpy` compile, no
  `--verify`) — extracting shared helpers from `cmd_get`/`cmd_put` in `cli.py` would
  mean threading `board_cwd` resolution and the REPL's non-fatal error handling back
  through one-shot functions, for ~15 lines of savings each. Not worth it now;
  revisit only if verify-on-shell turns out to matter in practice (still just an
  inline `fs_hash` call, not an import from `cli.py`).
- `shell.py` must **not** import from `cli.py` (that would be circular, since
  `cli.py` imports `shell.py`) — accept the client as a duck-typed constructor arg.

## Files

- **New `cli/src/mpftp/shell.py`** — `FtpShell(cmd.Cmd)`, `_resolve`, `_safe`, the
  `do_*` methods above, dynamic `self.prompt = f"mpftp:{self.board_cwd}> "` updated
  on every state-changing command.
- **Edited `cli/src/mpftp/cli.py`** — `from . import shell` near the existing
  `from . import config`; new `cmd_shell(ns)` near `cmd_watch_repl`/`cmd_watch`:
  ```python
  def cmd_shell(ns: argparse.Namespace) -> None:
      client, mode = get_client()
      try:
          if ns.device:
              ensure_device(client, ns.device, ns.baud)
          shell.FtpShell(client, mode, ns.device, ns.baud).cmdloop()
      finally:
          if mode.startswith("sidecar"):
              client.close()
  ```
  and one new subparser registration next to `watch-repl`:
  `sub.add_parser("shell", parents=[device_opts], help="Interactive FTP-style session").set_defaults(func=cmd_shell)`.
  No other existing function changes.
- **New `cli/tests/test_shell.py`** — mirrors `test_mip.py`'s `FakeClient(RpcClient)`
  + `redirect_stdout` pattern, driving via `shell.onecmd(...)`. Cases: `cd` into a
  subdir then `cd ..` returns to `/`; `cd` to a nonexistent dir leaves `board_cwd`
  unchanged and prints an error instead of raising; `get`/`put` with the second arg
  omitted; a `RuntimeError` from a fake client's `call()` is caught and printed, not
  raised, and `onecmd` returns cleanly; `bye` returns a truthy stop value and does
  **not** call `client.close()`.
- **Optional doc note in `docs/agent-guide.md`** (near the existing `watch-repl`
  mention) — state explicitly that `mpftp shell` is for humans and agents should
  keep scripting the flat one-shot subcommands, so a future agent reading the docs
  doesn't try to drive the REPL.
- **Optional short subsection in `docs/user-guide.md`** near the existing
  connect/transfer docs, for discoverability.

## Verification

```bash
cd cli && python -m pytest tests/test_shell.py -v
python -m pytest tests/   # full suite — confirm the new cli.py import causes no regressions
```

Manual smoke test (non-device parts need no hardware):

```
mpftp shell
mpftp:/> help
mpftp:/> status          # shows disconnected
mpftp:/> open COM4       # or /dev/ttyACM0 — needs a real board
mpftp:/> ls
mpftp:/> cd lib
mpftp:/> pwd
mpftp:/> cd ..
mpftp:/> lcd /tmp
mpftp:/> put ./test.py
mpftp:/> get test.py
mpftp:/> mkdir scratch
mpftp:/> delete /scratch
mpftp:/> ascii
mpftp:/> bye
```

Then confirm the flat commands are unaffected: run the full existing test suite, plus
one manual `mpftp ls /` outside the shell, to confirm no state leaked between the two
code paths.
