import { test } from "node:test";
import assert from "node:assert/strict";
import { KeyGate } from "../src/keygate";

test("keys typed while the REPL is opening are sent once it's open", () => {
  const sent: string[] = [];
  const gate = new KeyGate((d) => sent.push(d));
  gate.hold();
  gate.key("print(1)\r");
  gate.key("print(2)\r");
  assert.deepEqual(sent, []);
  gate.open();
  assert.deepEqual(sent, ["print(1)\rprint(2)\r"]);
  gate.key("x");
  assert.deepEqual(sent, ["print(1)\rprint(2)\r", "x"]);
});

test("keys are dropped when no REPL is open or opening", () => {
  const sent: string[] = [];
  const gate = new KeyGate((d) => sent.push(d));
  gate.key("a");
  gate.hold();
  gate.key("b");
  gate.close();
  gate.key("c");
  gate.hold();
  gate.open();
  assert.deepEqual(sent, []);
});
