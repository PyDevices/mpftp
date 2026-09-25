import "@xterm/xterm/css/xterm.css";
import "./style.css";
import { Rpc } from "./rpc";
import { Repl } from "./repl";
import { Files } from "./files";
import { Editor } from "./editor";
import { initSplitters } from "./splitters";
import {
  WifiBoard,
  askAddress,
  askPassword,
  isBleDevice,
  isWifiDevice,
  needsPassword,
  pickBleBoard,
  wifiAccessDialog,
} from "./wifi";

/** The port list's "type an address" entry. */
const WIFI_ADDRESS = "wifi:address";
const BLE_SCAN = "ble:scan";

interface Port {
  device: string;
  description?: string;
}

const rpc = new Rpc();
const THEME_KEY = "mpftp-theme";

function el<T extends HTMLElement>(id: string): T {
  const found = document.getElementById(id);
  if (!found) {
    throw new Error(`missing #${id}`);
  }
  return found as T;
}

function bufferToBase64(str: string): string {
  const bytes = new TextEncoder().encode(str);
  let binary = "";
  for (const b of bytes) {
    binary += String.fromCharCode(b);
  }
  return btoa(binary);
}

async function main(): Promise<void> {
  const portSelect = el<HTMLSelectElement>("port-select");
  const connectBtn = el<HTMLButtonElement>("connect-btn");
  const disconnectBtn = el<HTMLButtonElement>("disconnect-btn");
  const status = el<HTMLElement>("status-text");
  const statusDot = el<HTMLElement>("status-dot");
  const replContainer = el<HTMLElement>("repl");
  const editorContainer = el<HTMLElement>("editor-container");
  const saveBtn = el<HTMLButtonElement>("save-btn");
  const programName = el<HTMLElement>("program-name");
  const programDirty = el<HTMLElement>("program-dirty");
  const themeToggle = el<HTMLButtonElement>("theme-toggle");
  const wifiBtn = el<HTMLButtonElement>("wifi-btn");

  const repl = new Repl(replContainer, rpc);

  const editor = new Editor(editorContainer, {
    onDirty: (dirty) => {
      programDirty.hidden = !dirty;
      saveBtn.disabled = !dirty;
    },
    onSave: () => void saveCurrentFile(),
  });

  const files = new Files(rpc, {
    onOpenFile: (path, content) => {
      editor.open(path, content);
      programName.textContent = path;
      programDirty.hidden = true;
      saveBtn.disabled = true;
    },
  });

  async function saveCurrentFile(): Promise<void> {
    const path = editor.getPath();
    if (!path) {
      return;
    }
    saveBtn.disabled = true;
    try {
      await rpc.call("fs_write", { path, data_b64: bufferToBase64(editor.getContent()) });
      editor.markClean();
    } catch (e: any) {
      alert(`Save failed: ${e.message}`);
      saveBtn.disabled = false;
    }
  }
  saveBtn.addEventListener("click", () => void saveCurrentFile());

  let wsConnected = false;

  function setBoardStatus(text: string, cls: "is-up" | "is-connecting" | "is-down"): void {
    status.textContent = text;
    status.title = text;
    statusDot.className = `mp-status-dot ${cls}`;
  }

  rpc.onStatus((connected) => {
    wsConnected = connected;
    if (!connected) {
      setBoardStatus("mpftp server unreachable — retrying…", "is-down");
      connectBtn.disabled = true;
      return;
    }
    connectBtn.disabled = false;
    void refreshPorts();
  });

  async function refreshPorts(): Promise<void> {
    if (!wsConnected) {
      return;
    }
    try {
      const ports: Port[] = await rpc.call("list_ports");
      let wifiBoards: WifiBoard[] = [];
      try {
        wifiBoards = await rpc.call("wifi_boards");
      } catch {
        /* an older server; serial still works */
      }
      const current = portSelect.value;
      portSelect.innerHTML = "";
      const serial = document.createElement("optgroup");
      serial.label = "USB serial";
      for (const p of ports) {
        const opt = document.createElement("option");
        opt.value = p.device;
        opt.textContent = p.description ? `${p.device} — ${p.description}` : p.device;
        serial.appendChild(opt);
      }
      const wifi = document.createElement("optgroup");
      wifi.label = "Wi-Fi";
      for (const b of wifiBoards) {
        const opt = document.createElement("option");
        opt.value = b.device;
        opt.textContent = `${b.name} — ${b.ip}`;
        opt.title = `WebREPL at ${b.device}, board ${b.uid}` + (b.hasPassword ? "" : " (no password saved)");
        wifi.appendChild(opt);
      }
      const typed = document.createElement("option");
      typed.value = WIFI_ADDRESS;
      typed.textContent = "Type an address…";
      wifi.appendChild(typed);
      const bt = document.createElement("optgroup");
      bt.label = "Bluetooth (bledev)";
      const scan = document.createElement("option");
      scan.value = BLE_SCAN;
      scan.textContent = "Look for Bluetooth boards…";
      bt.appendChild(scan);
      portSelect.append(serial, wifi, bt);
      if (current && current !== WIFI_ADDRESS && current !== BLE_SCAN) {
        const known = Array.from(portSelect.options).some((o) => o.value === current);
        if (!known) {
          const opt = document.createElement("option");
          opt.value = current;
          opt.textContent = isBleDevice(current) ? current.replace(/^ble:\/\//i, "") : current;
          if (isBleDevice(current)) {
            bt.insertBefore(opt, scan);
          } else {
            wifi.insertBefore(opt, typed);
          }
        }
        portSelect.value = current;
      }
    } catch {
      /* transient; next status/interval refresh will retry */
    }
  }

  let connectedDevice = "";

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
        setBoardStatus(`connecting to ${device}…`, "is-connecting");
      }
    }
  }

  connectBtn.addEventListener("click", () => {
    void (async () => {
      let device = portSelect.value;
      if (!device) {
        setBoardStatus("pick a port first", "is-down");
        return;
      }
      if (device === WIFI_ADDRESS) {
        const typed = await askAddress(rpc);
        if (!typed) {
          return;
        }
        device = typed;
      } else if (device === BLE_SCAN) {
        const picked = await pickBleBoard(rpc);
        if (!picked) {
          return;
        }
        device = picked;
      }
      setBoardStatus(`connecting to ${device}…`, "is-connecting");
      connectBtn.disabled = true;
      try {
        await connectTo(device);
        connectedDevice = device;
        const via = isWifiDevice(device) ? " (Wi-Fi)" : isBleDevice(device) ? " (Bluetooth)" : "";
        setBoardStatus(`connected — ${device}${via}`, "is-up");
        disconnectBtn.disabled = false;
        wifiBtn.disabled = false;
        await repl.start();
        await files.refresh();
        void refreshPorts(); // a serial connect with Wi-Fi up adds the board to the Wi-Fi list
      } catch (e: any) {
        setBoardStatus(`connect failed: ${e.message}`, "is-down");
        // The status line truncates; the terminal shows the whole reason.
        repl.note(`connect failed: ${e.message}`);
      } finally {
        connectBtn.disabled = false;
      }
    })();
  });

  wifiBtn.addEventListener("click", () => {
    void (async () => {
      if (!connectedDevice) {
        return;
      }
      await repl.stop();
      const said = await wifiAccessDialog(rpc, connectedDevice);
      await repl.start().catch(() => undefined);
      if (said) {
        setBoardStatus(said, "is-up");
        void refreshPorts();
        void files.refresh();
      }
    })();
  });

  disconnectBtn.addEventListener("click", () => {
    void (async () => {
      disconnectBtn.disabled = true;
      wifiBtn.disabled = true;
      connectedDevice = "";
      await repl.stop();
      try {
        await rpc.call("disconnect");
      } catch {
        /* already gone */
      }
      setBoardStatus("disconnected", "is-down");
      editor.close();
      programName.textContent = "No file open";
    })();
  });

  document.addEventListener("keydown", (event) => {
    if ((event.ctrlKey || event.metaKey) && event.key === "s") {
      event.preventDefault();
      void saveCurrentFile();
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

  themeToggle.addEventListener("click", () => {
    applyTheme(currentTheme() === "light" ? "dark" : "light");
  });
  applyTheme(currentTheme());

  initSplitters();
  rpc.connect();
  setInterval(() => void refreshPorts(), 5000);
}

if ("serviceWorker" in navigator) {
  window.addEventListener("load", () => {
    void navigator.serviceWorker.register("/sw.js");
  });
}

void main();
