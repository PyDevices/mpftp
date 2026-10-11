import { strict as assert } from "assert";
import { test } from "node:test";
import { resultNote, searchHint } from "../src/packages";

const answer = (n: number, total: number, note: string | null = null) => ({
  query: "display text",
  results: Array.from({ length: n }, (_, i) => ({
    name: `pkg${i}`,
    source: "adafruit",
    source_label: "Adafruit CircuitPython Bundle",
    interpreter: "circuitpython",
    version: "1.0.0",
    description: "",
    install: { command: `mpftp circup pkg${i}`, method: "circup_install", params: { packages: [`pkg${i}`] } },
  })),
  total,
  offline: !!note,
  note,
});

test("the note under the box says how many matched and whether the cut hid some", () => {
  assert.equal(resultNote(answer(0, 0)), 'Nothing matches "display text".');
  assert.equal(resultNote(answer(1, 1)), "1 match. Pick one to install it.");
  assert.equal(resultNote(answer(3, 3)), "3 matches. Pick one to install it.");
  assert.equal(resultNote(answer(50, 80)), "50 of 80 matches; add a word to narrow it. Pick one to install it.");
});

test("an offline search keeps the server's note", () => {
  const note = "Offline: searched the cached copy of micropython-lib (3 h old).";
  assert.equal(resultNote(answer(2, 2, note)), `2 matches. Pick one to install it. ${note}`);
  assert.equal(resultNote(answer(0, 0, note)), `Nothing matches "display text". ${note}`);
});

test("the hint names the indexes the connected board installs from", () => {
  assert.match(searchHint("circuitpython"), /bundles.*circup/);
  assert.match(searchHint("micropython"), /micropython-lib and the PyDevices index.*mip/);
  assert.match(searchHint(""), /mip/);
});
