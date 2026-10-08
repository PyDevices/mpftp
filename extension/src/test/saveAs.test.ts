import { strict as assert } from "assert";
import * as path from "path";
import { test } from "node:test";
import {
  defaultSaveAsPath,
  nameSelection,
  normalizeRemote,
  parentOf,
  planSaveAs,
  rebindAfterSaveAs,
  resolveSaveAsTarget,
  tempPathFor,
} from "../saveAs";

const root = path.join(path.sep, "tmp", "mpftp-edit");

test("the box starts in that list's folder with the file's name, name selected", () => {
  assert.equal(defaultSaveAsPath("remote", "/lib", "main.py"), "/lib/main.py");
  assert.equal(defaultSaveAsPath("remote", "/", "main.py"), "/main.py");
  assert.equal(defaultSaveAsPath("remote", "", "main.py"), "/main.py");
  assert.equal(defaultSaveAsPath("local", path.join(path.sep, "proj"), "main.py"), path.join(path.sep, "proj", "main.py"));
  assert.deepEqual(nameSelection("/lib/main.py", "main.py"), [5, 9]);
  assert.deepEqual(nameSelection("/lib/Makefile", "Makefile"), [5, 13]);
});

test("a typed path resolves against that list's folder", () => {
  assert.equal(resolveSaveAsTarget("remote", "b.py", "/lib"), "/lib/b.py");
  assert.equal(resolveSaveAsTarget("remote", "./a/../b.py", "/lib"), "/lib/b.py");
  assert.equal(resolveSaveAsTarget("remote", "//x//y.py", "/lib"), "/x/y.py");
  assert.equal(resolveSaveAsTarget("remote", "\\lib\\c.py", "/"), "/lib/c.py");
  const proj = path.join(path.sep, "proj");
  assert.equal(resolveSaveAsTarget("local", "c.py", proj), path.join(proj, "c.py"));
  assert.equal(resolveSaveAsTarget("local", "~/c.py", proj, path.join(path.sep, "home", "u")), path.join(path.sep, "home", "u", "c.py"));
  assert.throws(() => resolveSaveAsTarget("remote", "/lib/", "/"), /not just a folder/);
  assert.throws(() => resolveSaveAsTarget("remote", "/", "/"), /not just a folder/);
  assert.throws(() => resolveSaveAsTarget("local", "  ", proj), /not just a folder/);
  assert.equal(normalizeRemote("../../x", "/a"), "/x");
});

test("parent folders, on each side", () => {
  assert.equal(parentOf("remote", "/lib/x.py"), "/lib");
  assert.equal(parentOf("remote", "/x.py"), "/");
  assert.equal(parentOf("local", path.join(path.sep, "p", "x.py")), path.join(path.sep, "p"));
});

test("Save As to the board: the temp mapping moves to the new board path", () => {
  const oldTemp = tempPathFor("/main.py", root);
  const bindings = new Map([[oldTemp, "/main.py"]]);
  const plan = planSaveAs("remote", "/lib/copy.py", root);
  assert.equal(plan.openLocal, path.join(root, "lib", "copy.py"));
  assert.equal(plan.pushRemote, "/lib/copy.py");
  rebindAfterSaveAs(bindings, oldTemp, plan.openLocal, plan.pushRemote);
  assert.deepEqual([...bindings], [[plan.openLocal, "/lib/copy.py"]]);
});

test("Save As to this computer: the file stops pushing to the board", () => {
  const oldTemp = tempPathFor("/main.py", root);
  const bindings = new Map([[oldTemp, "/main.py"]]);
  const dest = path.join(path.sep, "proj", "main.py");
  const plan = planSaveAs("local", dest, root);
  assert.deepEqual(plan, { openLocal: dest });
  rebindAfterSaveAs(bindings, oldTemp, dest, plan.pushRemote);
  assert.equal(bindings.size, 0);
});

test("a local file saved to the board starts pushing; one saved over a bound temp path stops it", () => {
  const bindings = new Map<string, string>();
  const plan = planSaveAs("remote", "/main.py", root);
  rebindAfterSaveAs(bindings, path.join(path.sep, "proj", "main.py"), plan.openLocal, plan.pushRemote);
  assert.deepEqual([...bindings], [[path.join(root, "main.py"), "/main.py"]]);
  // Saving some other file to this computer at that same temp path leaves it unbound.
  rebindAfterSaveAs(bindings, path.join(path.sep, "proj", "x.py"), path.join(root, "main.py"), undefined);
  assert.equal(bindings.size, 0);
});

test("temp paths mirror the board path and keep the real name", () => {
  assert.equal(tempPathFor("/lib/a:b.py", root), path.join(root, "lib", "a_b.py"));
  assert.throws(() => tempPathFor("/", root), /invalid remote path/);
});
