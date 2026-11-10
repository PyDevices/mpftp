import { strict as assert } from "assert";
import { test } from "node:test";
import { bytesPart } from "../transferProgress";

test("bytes inside one file show as a size and a percentage", () => {
  assert.equal(bytesPart(1.2 * 1024 * 1024, 3.4 * 1024 * 1024), "1.2 MB of 3.4 MB, 35%");
  assert.equal(bytesPart(512, 2048), "512 B of 2.0 KB, 25%");
  assert.equal(bytesPart(300, 0), "300 B");
});
