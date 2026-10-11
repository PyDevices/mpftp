import { strict as assert } from "assert";
import * as path from "path";
import { test } from "node:test";
import { folderArgs, workspaceRoots } from "../firmware/paths";

const d = path.delimiter;

test("the buildSystemPath setting is passed as --build-system, and wins by being explicit", () => {
  const args = folderArgs(
    { buildSystemPath: "/src/micropython-pydevices", workspacePath: "/ws" },
    ["/open/a"]
  );
  // The engine treats --build-system as an explicit choice ahead of
  // config.json and the workspace roots (see cli/tests/test_firmware_build.py).
  assert.equal(args.buildSystem, "/src/micropython-pydevices");
  assert.equal(args.workspace, ["/ws", "/open/a"].join(d));
});

test("without the setting the engine gets only the roots: workspace first, then open folders", () => {
  const args = folderArgs({ buildSystemPath: "", workspacePath: "/ws" }, ["/open/a", "/ws", "/open/b"]);
  assert.equal(args.buildSystem, undefined);
  assert.equal(args.workspace, ["/ws", "/open/a", "/open/b"].join(d));
  assert.deepEqual(workspaceRoots({ buildSystemPath: "", workspacePath: "" }, ["/open/a"]), ["/open/a"]);
});

test("nothing set means no folder args, so the engine falls back to config.json and its own search", () => {
  assert.deepEqual(folderArgs({ buildSystemPath: "  ", workspacePath: "" }, []), {});
});
