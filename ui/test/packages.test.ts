import { strict as assert } from "assert";
import { test } from "node:test";
import { installersFor, resultNote, rowParts, searchHint } from "../src/packages";

const answer = (n: number, total: number, note: string | null = null) => ({
  query: "display text",
  results: Array.from({ length: n }, (_, i) => ({
    name: `pkg${i}`,
    source: "adafruit",
    source_label: "Adafruit CircuitPython Bundle",
    interpreter: "circuitpython",
    version: "1.0.0",
    description: "",
    install: { command: `mcuctl circup pkg${i}`, method: "circup_install", params: { packages: [`pkg${i}`] } },
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
  assert.match(searchHint("micropython"), /OmniPython index and micropython-lib.*omip or mip/);
  assert.match(searchHint(""), /mip/);
});

test("Install Package offers omip first, then mip, on MicroPython", () => {
  const offered = installersFor("micropython");
  assert.deepEqual(
    offered.map((i) => [i.tool, i.method]),
    [
      ["omip", "omip_install"],
      ["mip", "mip_install"],
    ]
  );
  assert.deepEqual(offered[0].params("palettes"), { packages: ["palettes"], mpy: true });
  assert.deepEqual(offered[1].params("github:org/repo"), { packages: ["github:org/repo"], mpy: true });
  assert.deepEqual(installersFor("").map((i) => i.tool), ["omip", "mip"]);
});

test("Install Package uses circup alone on CircuitPython", () => {
  const offered = installersFor("circuitpython");
  assert.deepEqual(offered.map((i) => i.tool), ["circup"]);
  assert.equal(offered[0].method, "circup_install");
  assert.deepEqual(offered[0].params("adafruit_display_text"), { packages: ["adafruit_display_text"] });
});

test("a result's row names its index and install command, so two of one name differ", () => {
  const [ours, theirs] = ["omip", "mip"].map((tool, i) => ({
    ...answer(1, 1).results[0],
    name: "display",
    source_label: i ? "micropython-lib" : "OmniPython",
    install: { command: `mcuctl ${tool} display`, method: `${tool}_install`, params: { packages: ["display"] } },
  }));
  assert.deepEqual(rowParts(ours), {
    title: "display 1.0.0",
    source: "OmniPython",
    description: "",
    command: "mcuctl omip display",
  });
  assert.equal(rowParts(theirs).source, "micropython-lib");
  assert.equal(rowParts(theirs).command, "mcuctl mip display");
});
