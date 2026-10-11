// The plotter's line parser. Run with `npm test` (esbuild bundles, node --test runs).
import assert from "node:assert/strict";
import { test } from "node:test";
import { LineSplitter, parsePlotLine, stripAnsi } from "../src/plotparse";

test("a single number is one series named 1", () => {
  assert.deepEqual(parsePlotLine("21.5"), { names: ["1"], values: [21.5] });
});

test("comma, space, tab and semicolon separate series", () => {
  for (const line of ["1, -2.5, 3e2", "1 -2.5 3e2", "1\t-2.5\t3e2", "1;-2.5;3e2", "(1, -2.5, 3e2)".slice(1, -1)]) {
    assert.deepEqual(parsePlotLine(line), { names: ["1", "2", "3"], values: [1, -2.5, 300] }, line);
  }
});

test("name:value, name: value and name=value name the series", () => {
  const want = { names: ["temp", "hum"], values: [21.5, 40] };
  assert.deepEqual(parsePlotLine("temp:21.5 hum:40"), want);
  assert.deepEqual(parsePlotLine("temp: 21.5, hum: 40"), want);
  assert.deepEqual(parsePlotLine("temp=21.5 hum=40"), want);
});

test("named and unnamed mix by position", () => {
  assert.deepEqual(parsePlotLine("x:1, 2"), { names: ["x", "2"], values: [1, 2] });
});

test("text, prompts and tracebacks are not plotted", () => {
  for (const line of [
    "",
    ">>> print(1, 2)",
    ">>> 1+1",
    "MicroPython v1.29.0 on 2026-08-24; Raspberry Pi Pico with RP2040",
    "Traceback (most recent call last):",
    "temperature is 21.5",
    "1, 2, oops",
    "nan",
    "inf",
    "1 2 3 4 5 6 7 8 9",
    "0x10",
  ]) {
    assert.equal(parsePlotLine(line), null, line);
  }
});

test("colour codes and a trailing CR are ignored", () => {
  assert.equal(stripAnsi("\x1b[32m1.5\x1b[0m"), "1.5");
  assert.deepEqual(parsePlotLine("\x1b[32m1.5\x1b[0m\r"), { names: ["1"], values: [1.5] });
});

test("lines split across chunks are put back together", () => {
  const s = new LineSplitter();
  const enc = new TextEncoder();
  assert.deepEqual(s.push(enc.encode("1, 2")), []);
  assert.deepEqual(s.push(enc.encode("\r\n3, 4\r\n5")), ["1, 2", "3, 4"]);
  assert.deepEqual(s.push(enc.encode("\n")), ["5"]);
});
