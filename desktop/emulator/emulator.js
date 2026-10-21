/*
 * The emulator window: an EmuBoard wired to mpftp through the desktop shell.
 *
 * mpftp's sidecar sees this window as a board on a serial line (emu:// in
 * the sidecar). The shell routes the sidecar's emu_* notifications here as
 * "emu-line" events, and this page answers with host_send lines the
 * sidecar's stdin reader hands straight to the link.
 *
 * The board's flash is a folder on disk, read at startup and written back
 * when the board goes quiet, when it resets, and when the window closes.
 */

import { EmuBoard } from "./emu-core.mjs";
import { loadMicroPython } from "./runtime/micropython.mjs";

const tauri = globalThis.__TAURI__;
const invoke = tauri.core.invoke;

const PRESETS = {
  "320x240": { width: 320, height: 240, round: false },
  "240x320": { width: 240, height: 320, round: false },
  "240x240": { width: 240, height: 240, round: false },
  "240x240-round": { width: 240, height: 240, round: true },
  "466x466-round": { width: 466, height: 466, round: true },
  "480x320": { width: 480, height: 320, round: false },
  "320x480": { width: 320, height: 480, round: false },
  "800x480": { width: 800, height: 480, round: false },
};

const BAR = 36;
const PAD = 16;
const BEZEL = 10;
const COMPACT = { width: 480, height: 120 };
const SAVE_AFTER_MS = 400;

const $ = (id) => document.getElementById(id);
const canvas = $("display_canvas");

function stored(key, fallback) {
  try {
    return localStorage.getItem(key) ?? fallback;
  } catch {
    return fallback;
  }
}

function store(key, value) {
  try {
    localStorage.setItem(key, value);
  } catch {
    /* a convenience only */
  }
}

let sizeKey = PRESETS[stored("emu.size", "")] ? stored("emu.size", "") : "320x240";
let zoom = stored("emu.zoom", "1") === "2" ? 2 : 1;
let pinned = stored("emu.pin", "0") === "1";

// Base64, for the shell's JSON lines.

function toB64(bytes) {
  let s = "";
  for (let i = 0; i < bytes.length; i += 0x8000) {
    s += String.fromCharCode.apply(null, bytes.subarray(i, i + 0x8000));
  }
  return btoa(s);
}

function fromB64(text) {
  const s = atob(text || "");
  const out = new Uint8Array(s.length);
  for (let i = 0; i < s.length; i++) out[i] = s.charCodeAt(i);
  return out;
}

// Output to mpftp. One send at a time keeps the bytes in order; what the
// board prints meanwhile goes in the next one.

let pending = [];
let sending = false;

function sendOut(bytes) {
  pending.push(bytes);
  if (!sending) pump();
}

async function pump() {
  sending = true;
  try {
    while (pending.length) {
      const parts = pending;
      pending = [];
      const n = parts.reduce((a, p) => a + p.length, 0);
      const all = new Uint8Array(n);
      let at = 0;
      for (const p of parts) {
        all.set(p, at);
        at += p.length;
      }
      await hostSend({ emu_in: toB64(all) });
    }
  } finally {
    sending = false;
  }
}

function hostSend(msg) {
  return invoke("host_send", { line: JSON.stringify(msg) }).catch((err) => {
    status(`mpftp isn't answering (${err})`);
  });
}

// The flash folder.

let lastSaved = "";
let saveTimer = null;

async function loadFlash() {
  try {
    const snap = await invoke("emu_fs_load");
    if (!snap) return null;
    return {
      dirs: snap.dirs || [],
      files: (snap.files || []).map((f) => ({ path: f.path, data: fromB64(f.b64) })),
    };
  } catch (err) {
    console.warn("flash folder unreadable:", err);
    return null;
  }
}

async function saveFlash() {
  clearTimeout(saveTimer);
  saveTimer = null;
  if (!board.mp) return;
  const snap = board.snapshot();
  const body = {
    dirs: snap.dirs,
    files: snap.files.map((f) => ({ path: f.path, b64: toB64(f.data) })),
  };
  const text = JSON.stringify(body);
  if (text === lastSaved) return;
  try {
    await invoke("emu_fs_save", { snapshot: body });
    lastSaved = text;
  } catch (err) {
    status(`Couldn't save the emulator's files: ${err}`);
  }
}

function saveSoon() {
  clearTimeout(saveTimer);
  saveTimer = setTimeout(saveFlash, SAVE_AFTER_MS);
}

// The display.

let bridge = null;

function applySize() {
  const p = PRESETS[sizeKey];
  canvas.width = p.width;
  canvas.height = p.height;
  canvas.style.width = `${p.width * zoom}px`;
  canvas.style.height = `${p.height * zoom}px`;
  canvas.classList.toggle("round", p.round);
  $("bezel").classList.toggle("round", p.round);
  $("size").value = sizeKey;
  $("zoom").textContent = `${zoom}×`;
  fit();
}

function fit() {
  const p = PRESETS[sizeKey];
  const shown = document.body.classList.contains("has-display");
  const width = shown ? Math.max(COMPACT.width, p.width * zoom + 2 * (PAD + BEZEL) + 4) : COMPACT.width;
  const height = shown ? BAR + p.height * zoom + 2 * (PAD + BEZEL) + 4 : COMPACT.height;
  invoke("emu_fit", { width, height }).catch(() => {});
}

function watchDisplay() {
  const now = globalThis.Module?.pydevicesBridge || null;
  if (now !== bridge) {
    bridge = now;
    document.body.classList.toggle("has-display", !!bridge);
    fit();
    if (bridge) {
      canvas.focus();
      bridge.enableAudio?.(false).catch(() => {});
    }
  }
}

// Status.

function status(text) {
  $("status").textContent = text;
}

let ready = false;

function showState() {
  const dot = $("state");
  dot.className = ready ? (board.busy ? "busy" : "ready") : "";
  dot.title = !ready ? "Starting" : board.busy ? "Running" : "Idle";
  if (ready) status(board.busy ? "Running" : "Idle");
}

// The board.

const board = new EmuBoard({
  loadMicroPython,
  wasmURL: new URL("runtime/micropython.wasm", import.meta.url).href,
  send: sendOut,
  size: { width: PRESETS[sizeKey].width, height: PRESETS[sizeKey].height },
  beforeRestart() {
    try {
      globalThis.Module?.pydevicesBridge?.shutdown?.();
    } catch (err) {
      console.warn("display teardown failed:", err);
    }
    bridge = null;
    document.body.classList.remove("has-display");
    fit();
  },
  afterBoot() {
    saveSoon();
  },
  onActivity() {
    saveSoon();
  },
});

// Bytes that arrive while the board boots (after a reload, say) wait here.
let early = [];

function onLine(line) {
  let msg;
  try {
    msg = JSON.parse(line);
  } catch {
    return;
  }
  const params = msg.params || {};
  if (msg.method === "emu_open") {
    if (ready) hostSend({ emu_ready: true });
  } else if (msg.method === "emu_out") {
    if (ready) board.write(fromB64(params.b64));
    else early.push(fromB64(params.b64));
  }
}

async function main() {
  applySize();
  $("pin").setAttribute("aria-pressed", String(pinned));
  if (pinned) invoke("emu_pin", { on: true }).catch(() => {});

  $("size").addEventListener("change", async (e) => {
    sizeKey = e.target.value;
    store("emu.size", sizeKey);
    board.size = { width: PRESETS[sizeKey].width, height: PRESETS[sizeKey].height };
    applySize();
    // A running interpreter has built its display already.
    await saveFlash();
    board.hardReset();
  });
  $("zoom").addEventListener("click", () => {
    zoom = zoom === 1 ? 2 : 1;
    store("emu.zoom", String(zoom));
    applySize();
  });
  $("pin").addEventListener("click", () => {
    pinned = !pinned;
    store("emu.pin", pinned ? "1" : "0");
    $("pin").setAttribute("aria-pressed", String(pinned));
    invoke("emu_pin", { on: pinned }).catch(() => {});
  });
  $("reset").addEventListener("click", async () => {
    await saveFlash();
    board.hardReset();
  });
  $("bezel").addEventListener("pointerdown", () => canvas.focus());

  await tauri.event.listen("emu-line", (e) => onLine(e.payload));
  tauri.window.getCurrentWindow().onCloseRequested(async () => {
    await saveFlash();
  });

  // The shell reloads a window that stops answering, so a program that
  // never sleeps can still be stopped.
  setInterval(() => invoke("emu_alive", { ready }).catch(() => {}), 500);
  setInterval(() => {
    watchDisplay();
    showState();
  }, 250);

  const snapshot = await loadFlash();
  await board.start(snapshot);
  ready = true;
  showState();
  hostSend({ emu_ready: true });
  for (const bytes of early.splice(0)) board.write(bytes);
}

main().catch((err) => {
  status(`The emulator didn't start: ${err}`);
  console.error(err);
});
