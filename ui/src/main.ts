import "@xterm/xterm/css/xterm.css";
import "./style.css";
import { Rpc } from "./rpc";
import { Repl } from "./repl";
import { Editor } from "./editor";
import { initSplitters } from "./splitters";
import { BROWSER_COMMANDS, PanelState, installVsCodeShim, panelStatus } from "./host";
import { pickDevice, rememberDevice } from "./connect";
import { confirmDialog, promptText, saveAsDialog, toast } from "./dialogs";
import { firmwareDialog } from "./firmware";
import { askPassword, isBleDevice, isWifiDevice, needsPassword, wifiAccessDialog } from "./wifi";

const rpc = new Rpc();
const THEME_KEY = "mpftp-theme";

function el<T extends HTMLElement>(id: string): T {
  const found = document.getElementById(id);
  if (!found) {
    throw new Error(`missing #${id}`);
  }
  return found as T;
}

function textToBase64(str: string): string {
  const bytes = new TextEncoder().encode(str);
  let binary = "";
  for (const b of bytes) {
    binary += String.fromCharCode(b);
  }
  return btoa(binary);
}

function base64ToText(b64: string): string | null {
  const binary = atob(b64 || "");
  const bytes = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i++) {
    bytes[i] = binary.charCodeAt(i);
  }
  try {
    return new TextDecoder("utf-8", { fatal: true }).decode(bytes);
  } catch {
    return null;
  }
}

/** The board as the panel last described it. */
let board: PanelState = { connected: false, device: "", interpreter: "", localPath: "", remotePath: "" };

const repl = new Repl(el("repl"), rpc);

const editorTitle = el<HTMLElement>("editor-where");
const saveBtn = el<HTMLButtonElement>("save-btn");
const saveAsBtn = el<HTMLButtonElement>("save-as-btn");
const editor = new Editor(el("editor-container"), el("editor-tabs"), {
  onChange: (file, dirty) => {
    saveBtn.disabled = !file || !dirty;
    saveAsBtn.disabled = !file;
    editorTitle.textContent = file ? `${file.side === "remote" ? "Board" : "Local"} · ${file.path}` : "";
    editorTitle.title = editorTitle.textContent;
  },
  onSave: () => void saveCurrentFile(),
  onSaveAs: () => void saveCurrentFileAs(),
  confirmClose: (name) => confirmDialog(`${name} has unsaved changes. Close it anyway?`, "Close without saving"),
});

// --- saving: the panel's host writes the buffer back where it came from ----

interface SaveReply {
  ok: boolean;
  error?: string;
  cancelled?: boolean;
  side?: string;
  path?: string;
}

let nextSave = 1;
const pendingSaves = new Map<number, (r: SaveReply) => void>();

function askPanel(msg: Record<string, unknown>): Promise<SaveReply> {
  const reqId = nextSave++;
  return new Promise<SaveReply>((resolve) => {
    pendingSaves.set(reqId, resolve);
    rpc.sendPanel({ ...msg, reqId });
  });
}

async function saveCurrentFile(): Promise<void> {
  const file = editor.current();
  if (!file) {
    return;
  }
  const text = editor.getContent();
  saveBtn.disabled = true;
  const reply = await askPanel({ type: "saveFile", side: file.side, path: file.path, data_b64: textToBase64(text) });
  if (reply.ok) {
    editor.markClean(file, text);
  } else {
    toast(`Save failed: ${reply.error || "unknown error"}`, "error");
    saveBtn.disabled = false;
  }
}
saveBtn.addEventListener("click", () => void saveCurrentFile());

/** Save As: either side, any folder; the tab then belongs to the new copy. */
async function saveCurrentFileAs(): Promise<void> {
  const file = editor.current();
  if (!file) {
    return;
  }
  const name = file.path.split(/[\\/]/).filter(Boolean).pop() || "untitled.py";
  const target = await saveAsDialog({
    name,
    side: file.side,
    folders: { local: board.localPath, remote: board.remotePath || "/" },
    boardAvailable: board.connected,
  });
  if (!target) {
    return;
  }
  const text = editor.getContent();
  const reply = await askPanel({
    type: "saveFileAs",
    side: target.side,
    path: target.path,
    data_b64: textToBase64(text),
    sourceSide: file.side,
    sourcePath: file.path,
  });
  if (reply.ok) {
    const side = reply.side === "remote" ? "remote" : "local";
    editor.rebind(file, { side, path: String(reply.path || target.path) }, text);
  } else if (!reply.cancelled) {
    toast(`Save As failed: ${reply.error || "unknown error"}`, "error");
  }
}
saveAsBtn.addEventListener("click", () => void saveCurrentFileAs());

// --- connecting --------------------------------------------------------------

/** Connect; over Wi-Fi, ask for the password when the server has none (or a wrong one). */
async function connectTo(device: string): Promise<void> {
  const params: Record<string, unknown> = { device, baud: 115200 };
  for (let attempt = 0; ; attempt++) {
    try {
      await rpc.call("connect", params);
      return;
    } catch (e: any) {
      const remote = isWifiDevice(device) || isBleDevice(device);
      if (!remote || !needsPassword(e.message) || attempt >= 3) {
        throw e;
      }
      const why = /rejected/i.test(e.message)
        ? "The board said no to that password."
        : `mpftp has no ${isBleDevice(device) ? "bledev" : "WebREPL"} password saved for this board.`;
      const answer = await askPassword(device, why);
      if (!answer) {
        throw new Error("cancelled");
      }
      params.password = answer.password;
      params.remember = answer.remember;
    }
  }
}

async function connectFlow(): Promise<void> {
  const device = await pickDevice(rpc);
  if (!device) {
    return;
  }
  panelStatus(`Connecting to ${device}…`, "active");
  try {
    await connectTo(device);
    rememberDevice(device);
  } catch (e: any) {
    if (e.message !== "cancelled") {
      toast(`mpftp connect failed: ${e.message}`, "error");
      // The toast fades; the terminal keeps the whole reason.
      repl.note(`connect failed: ${e.message}`);
    }
    panelStatus("Disconnected");
  }
}

async function disconnectFlow(): Promise<void> {
  repl.detach();
  try {
    await rpc.call("repl_stop");
  } catch {
    /* board may already be gone */
  }
  try {
    await rpc.call("disconnect");
  } catch {
    /* already gone */
  }
}

/** A call that may never answer (the board resets under it). */
function bounded(method: string, ms: number): Promise<unknown> {
  return Promise.race([
    rpc.call(method),
    new Promise((_, reject) => setTimeout(() => reject(new Error(`${method} timeout`)), ms)),
  ]);
}

async function reconnectAfterReset(device: string, tries = 20): Promise<boolean> {
  panelStatus(`Waiting to reconnect ${device}…`, "active");
  for (let i = 0; i < tries; i++) {
    await new Promise((r) => setTimeout(r, 1000));
    try {
      if (!isWifiDevice(device) && !isBleDevice(device)) {
        const ports: Array<{ device: string }> = await rpc.call("list_ports");
        if (!ports.some((p) => p.device === device)) {
          continue;
        }
      }
      await rpc.call("connect", { device, baud: 115200 });
      return true;
    } catch {
      /* not back yet */
    }
  }
  return false;
}

// --- the mpftp.* commands (the panel's ⋯ menu and toolbar) ----------------------

function needBoard(): boolean {
  if (!board.connected) {
    toast("mpftp: connect to a board first", "warning");
    return false;
  }
  return true;
}

async function runCommand(command: string): Promise<void> {
  switch (command) {
    case "mpftp.connect":
      await connectFlow();
      return;
    case "mpftp.disconnect":
      await disconnectFlow();
      return;
    case "mpftp.resume": {
      const res: { device?: string } = await rpc.call("resume");
      toast(`mpftp resumed: ${res?.device || board.device}`);
      return;
    }
    case "mpftp.openRepl":
      repl.focus();
      return;
    case "mpftp.openFirmware":
      await firmwareDialog(rpc, {
        device: board.connected && !isWifiDevice(board.device) && !isBleDevice(board.device) ? board.device : "",
        release: async () => {
          repl.detach();
          await rpc.call("repl_stop").catch(() => undefined);
        },
        reconnect: (device) => reconnectAfterReset(device, 30),
      });
      return;
  }
  if (!needBoard()) {
    return;
  }
  switch (command) {
    case "mpftp.interrupt":
      await rpc.call("interrupt");
      toast("Interrupt (Ctrl+C) sent");
      break;
    case "mpftp.softReset": {
      const res: { interpreter?: string } = await rpc.call("soft_reset");
      toast(
        (res?.interpreter || board.interpreter) === "circuitpython"
          ? "Soft reset sent (CircuitPython: friendly↔raw; code.py not auto-run)"
          : "Soft reset sent (main.py not run)"
      );
      break;
    }
    case "mpftp.hardReset": {
      const device = board.device;
      await bounded("hard_reset", 5000).catch(() => undefined);
      await disconnectFlow();
      if (device && (await reconnectAfterReset(device))) {
        toast(`mpftp reconnected: ${device}`);
      } else if (device) {
        panelStatus("Disconnected");
        toast(`mpftp: ${device} did not come back; press Connect when it's ready`, "warning");
      }
      break;
    }
    case "mpftp.bootloader":
      await bounded("bootloader", 5000).catch(() => undefined);
      await disconnectFlow();
      toast("Entered bootloader — flash firmware, then Connect (auto-reconnect skipped)");
      break;
    case "mpftp.runFile": {
      const file = editor.current();
      if (!file) {
        toast("mpftp: open a .py file in the editor to run it", "warning");
        return;
      }
      // follow=false: leave the REPL free for prints and input(), like Run on the panel.
      await rpc.call("run_script", { source: editor.getContent(), follow: false });
      repl.focus();
      break;
    }
    case "mpftp.enableWifiAccess":
    case "mpftp.disableWifiAccess": {
      repl.detach();
      await rpc.call("repl_stop").catch(() => undefined);
      const said = await wifiAccessDialog(rpc, board.device);
      await repl.start().catch(() => undefined);
      if (said) {
        toast(said);
      }
      break;
    }
    case "mpftp.editRemote": {
      const remote = await promptText({ prompt: "Board file path to edit", value: "/main.py" });
      if (remote) {
        rpc.sendPanel({ type: "openRemote", path: remote });
      }
      return;
    }
    case "mpftp.eval": {
      const expr = await promptText({ prompt: "Expression to eval on board" });
      if (expr) {
        const res: { value?: string } = await rpc.call("eval", { expr });
        toast(String(res?.value ?? ""));
      }
      return;
    }
    case "mpftp.exec": {
      const code = await promptText({ prompt: "Code to exec on board" });
      if (code) {
        const res: { output?: string } = await rpc.call("exec", { code, follow: true });
        repl.block(res?.output || "");
      }
      break;
    }
    case "mpftp.rtcGet": {
      const res: { datetime?: string } = await rpc.call("rtc_get");
      toast(`RTC: ${res?.datetime}`);
      return;
    }
    case "mpftp.rtcSet": {
      const res: { datetime?: number[] } = await rpc.call("rtc_set");
      toast(`RTC set: ${JSON.stringify(res?.datetime)}`);
      return;
    }
    case "mpftp.installPackage": {
      const circuit = board.interpreter === "circuitpython";
      const pkg = await promptText(
        circuit
          ? { prompt: "Library to install with circup", placeHolder: "adafruit_display_text" }
          : {
              prompt: "Package to install via mip (host downloads, writes to board)",
              placeHolder: "github:org/repo or micropython-lib name",
            }
      );
      if (!pkg) {
        return;
      }
      panelStatus(`Installing ${pkg}…`, "active");
      const res: { output?: string; target?: string } = await (circuit
        ? rpc.call("circup_install", { packages: [pkg] })
        : rpc.call("mip_install", { packages: [pkg], mpy: true })
      ).catch((e: any) => {
        panelStatus(`Install of ${pkg} failed`, "stalled");
        throw e;
      });
      repl.block((res?.output || "") + (res?.target ? `\ntarget: ${res.target}` : ""));
      panelStatus(`Installed ${pkg}`, "done");
      toast(`Installed ${pkg}`);
      break;
    }
    case "mpftp.df": {
      const res: { mounts?: unknown[] } = await rpc.call("df");
      repl.block(JSON.stringify(res?.mounts, null, 2));
      return;
    }
    case "mpftp.romfsQuery": {
      const res: { output?: string } = await rpc.call("romfs_query");
      repl.block(res?.output || "(no output)");
      return;
    }
    case "mpftp.hashRemote": {
      const remote = await promptText({ prompt: "Board file to hash", value: "/main.py" });
      if (remote) {
        const res: { hash?: string; algo?: string } = await rpc.call("fs_hash", { path: remote, algo: "sha256" });
        toast(`${res?.algo}: ${res?.hash}`);
      }
      return;
    }
    default:
      return;
  }
  // As FtpViewProvider does after a command: redraw the board list.
  rpc.sendPanel({ type: "refreshRemote" });
}

function runSafely(command: string): void {
  runCommand(command).catch((e: any) => {
    toast(`mpftp: ${e?.message || e}`, "error");
  });
}

// --- the panel's host, page side ------------------------------------------------

installVsCodeShim(rpc, {
  local: (msg) => {
    switch (msg.type) {
      case "connect":
        runSafely("mpftp.connect");
        return true;
      case "disconnect":
        runSafely("mpftp.disconnect");
        return true;
      case "openRepl":
        repl.focus();
        return true;
      case "command":
        if (BROWSER_COMMANDS.includes(String(msg.command))) {
          runSafely(String(msg.command));
        } else {
          toast(`${msg.command} isn't available in the browser`, "warning");
        }
        return true;
      default:
        return false;
    }
  },
  onHost: (msg) => {
    switch (msg.action) {
      case "ask": {
        const answer =
          msg.kind === "confirm"
            ? confirmDialog(String(msg.prompt || ""), String(msg.okLabel || "OK"))
            : promptText({
                prompt: String(msg.prompt || ""),
                value: msg.value || "",
                placeHolder: msg.placeHolder || "",
                selection: Array.isArray(msg.selection) ? (msg.selection as [number, number]) : undefined,
              });
        void answer.then((value) => rpc.answerPanel(msg.askId, value));
        break;
      }
      case "info":
        toast(String(msg.text || ""));
        break;
      case "error":
        toast(String(msg.text || ""), "error");
        break;
      case "openRepl":
        repl.focus();
        break;
      case "open": {
        const text = base64ToText(msg.data_b64 || "");
        if (text === null) {
          toast(`${msg.path} isn't a text file, so it can't be edited here`, "warning");
          break;
        }
        editor.open(msg.side === "remote" ? "remote" : "local", String(msg.path), text);
        break;
      }
      case "reply": {
        const done = pendingSaves.get(msg.reqId);
        if (done) {
          pendingSaves.delete(msg.reqId);
          done({ ok: !!msg.ok, error: msg.error, cancelled: !!msg.cancelled, side: msg.side, path: msg.path });
        }
        break;
      }
    }
  },
  onState: (state) => {
    const was = board.connected;
    board = state;
    if (state.connected && !repl.running) {
      // A connect from this tab, another tab, or before a reload: show the REPL.
      repl.start().catch((e: any) => repl.note(`REPL failed to start: ${e.message}`));
    } else if (!state.connected && was) {
      repl.detach();
      repl.note("disconnected");
    }
  },
});

// --- page chrome ------------------------------------------------------------------

document.addEventListener("keydown", (event) => {
  // The editor handles its own Ctrl+S and Ctrl+Shift+S (and prevents the default).
  if (event.defaultPrevented || !(event.ctrlKey || event.metaKey) || event.key.toLowerCase() !== "s") {
    return;
  }
  event.preventDefault();
  void (event.shiftKey ? saveCurrentFileAs() : saveCurrentFile());
});

window.addEventListener("beforeunload", (event) => {
  if (editor.isDirty()) {
    event.preventDefault();
  }
});

function currentTheme(): "dark" | "light" {
  return document.documentElement.getAttribute("data-theme") === "light" ? "light" : "dark";
}

function applyTheme(theme: "dark" | "light"): void {
  if (theme === "light") {
    document.documentElement.setAttribute("data-theme", "light");
  } else {
    document.documentElement.removeAttribute("data-theme");
  }
  try {
    localStorage.setItem(THEME_KEY, theme);
  } catch {
    /* private browsing, etc. */
  }
  editor.setTheme(theme === "dark");
  repl.setTheme(theme === "dark");
}

el<HTMLButtonElement>("theme-toggle").addEventListener("click", () => {
  applyTheme(currentTheme() === "light" ? "dark" : "light");
});
applyTheme(currentTheme());

// The toolbar's Firmware button flashes here; building is the extension's.
el<HTMLButtonElement>("btnFirmware").title = "Flash firmware onto an ESP board (esptool)";

initSplitters(() => repl.fit());
rpc.connect();

if ("serviceWorker" in navigator) {
  window.addEventListener("load", () => {
    void navigator.serviceWorker.register("/sw.js");
  });
}

