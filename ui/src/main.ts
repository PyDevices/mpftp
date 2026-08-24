import "@xterm/xterm/css/xterm.css";
import "./style.css";
import { Rpc } from "./rpc";
import { Repl } from "./repl";
import { Files } from "./files";

interface Port {
  device: string;
  description?: string;
}

const rpc = new Rpc();

function el<T extends HTMLElement>(id: string): T {
  const found = document.getElementById(id);
  if (!found) {
    throw new Error(`missing #${id}`);
  }
  return found as T;
}

async function main(): Promise<void> {
  const portSelect = el<HTMLSelectElement>("port-select");
  const connectBtn = el<HTMLButtonElement>("connect-btn");
  const disconnectBtn = el<HTMLButtonElement>("disconnect-btn");
  const status = el<HTMLElement>("status");
  const replContainer = el<HTMLElement>("repl");
  const filesContainer = el<HTMLElement>("files");

  const repl = new Repl(replContainer, rpc);
  const files = new Files(filesContainer, rpc);

  let wsConnected = false;

  function setBoardStatus(text: string, cls: string): void {
    status.textContent = text;
    status.className = `status ${cls}`;
  }

  rpc.onStatus((connected) => {
    wsConnected = connected;
    if (!connected) {
      setBoardStatus("mpftp server unreachable — retrying…", "down");
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
        setBoardStatus("pick a port first", "down");
        return;
      }
      setBoardStatus(`connecting to ${device}…`, "connecting");
      connectBtn.disabled = true;
      try {
        await rpc.call("connect", { device, baud: 115200 });
        setBoardStatus(`connected — ${device}`, "up");
        disconnectBtn.disabled = false;
        await repl.start();
        await files.refresh();
      } catch (e: any) {
        setBoardStatus(`connect failed: ${e.message}`, "down");
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
      setBoardStatus("disconnected", "down");
    })();
  });

  rpc.connect();
  setInterval(() => void refreshPorts(), 5000);
}

if ("serviceWorker" in navigator) {
  window.addEventListener("load", () => {
    void navigator.serviceWorker.register("/sw.js");
  });
}

void main();
