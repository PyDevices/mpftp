/*
 * The emulator window's part, under Node: for testing the sidecar's emu://
 * link without the desktop app. Reads the sidecar's emu_* notify lines on
 * fd 3 and answers with {"emu_ready"} and {"emu_in"} lines on fd 4.
 *
 * Not stdin and stdout: under Node the runtime's VM hook reads stdin itself,
 * looking for Ctrl-C, and would eat the messages.
 *
 *   node node-emu.mjs <path/to/micropython.mjs>  3<requests 4>replies
 */
import { createReadStream, writeSync } from "node:fs";
import { createInterface } from "node:readline";
import { pathToFileURL } from "node:url";
import { EmuBoard } from "./emu-core.mjs";

const { loadMicroPython } = await import(pathToFileURL(process.argv[2]).href);
const say = (obj) => writeSync(4, JSON.stringify(obj) + "\n");

let board = null;
let starting = null;

async function ensureBoard() {
  if (board) return board;
  if (!starting) {
    const b = new EmuBoard({
      loadMicroPython,
      send: (bytes) => say({ emu_in: Buffer.from(bytes).toString("base64") }),
    });
    starting = b.start().then(() => (board = b));
  }
  return starting;
}

for await (const line of createInterface({ input: createReadStream(null, { fd: 3 }) })) {
  let msg;
  try {
    msg = JSON.parse(line);
  } catch {
    continue;
  }
  if (msg.method === "emu_open") {
    await ensureBoard();
    say({ emu_ready: true });
  } else if (msg.method === "emu_out") {
    (await ensureBoard()).write(Buffer.from(msg.params.b64, "base64"));
  }
}
