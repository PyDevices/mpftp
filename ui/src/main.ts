import "@xterm/xterm/css/xterm.css";
import "./style.css";
import { Rpc } from "./rpc";
import { Repl } from "./repl";
import { Files } from "./files";
import { Editor } from "./editor";
import { initSplitters } from "./splitters";

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
      const current = portSelect.value;
      portSelect.innerHTML = "";
      for (const p of ports) {
        const opt = document.createElement("option");
        opt.value = p.device;
        opt.textContent = p.description ? `${p.device} — ${p.description}` : p.device;
        portSelect.appendChild(opt);
      }
      if (current) {
        portSelect.value = current;
      }
    } catch {
      /* transient; next status/interval refresh will retry */
    }
  }

  connectBtn.addEventListener("click", () => {
    void (async () => {
      const device = portSelect.value;
      if (!device) {
        setBoardStatus("pick a port first", "is-down");
        return;
      }
      setBoardStatus(`connecting to ${device}…`, "is-connecting");
      connectBtn.disabled = true;
      try {
        await rpc.call("connect", { device, baud: 115200 });
        setBoardStatus(`connected — ${device}`, "is-up");
        disconnectBtn.disabled = false;
        await repl.start();
        await files.refresh();
      } catch (e: any) {
        setBoardStatus(`connect failed: ${e.message}`, "is-down");
      } finally {
        connectBtn.disabled = false;
      }
    })();
  });

  disconnectBtn.addEventListener("click", () => {
    void (async () => {
      disconnectBtn.disabled = true;
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
