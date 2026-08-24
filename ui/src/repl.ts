import { Terminal } from "@xterm/xterm";
import type { Rpc } from "./rpc";

function bytesToBase64(bytes: Uint8Array): string {
  let binary = "";
  for (const b of bytes) {
    binary += String.fromCharCode(b);
  }
  return btoa(binary);
}

function base64ToBytes(b64: string): Uint8Array {
  const binary = atob(b64);
  const bytes = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i++) {
    bytes[i] = binary.charCodeAt(i);
  }
  return bytes;
}

export class Repl {
  private term: Terminal;
  private rpc: Rpc;
  private started = false;

  constructor(container: HTMLElement, rpc: Rpc) {
    this.rpc = rpc;
    this.term = new Terminal({
      convertEol: true,
      cursorBlink: true,
      fontFamily: "Menlo, Consolas, monospace",
      fontSize: 13,
      theme: { background: "#1e1e1e" },
    });
    this.term.open(container);
    this.term.writeln("mpftp REPL — connect to a board to begin.");

    this.term.onData((data: string) => {
      if (!this.started) {
        return;
      }
      const bytes = new TextEncoder().encode(data);
      this.rpc.call("repl_write", { data_b64: bytesToBase64(bytes) }).catch((e) => {
        this.term.writeln(`\r\n[repl_write failed: ${e.message}]`);
      });
    });

    rpc.onNotify("repl_data", (params) => {
      const bytes = base64ToBytes(params.data_b64 || "");
      this.term.write(bytes);
    });
    rpc.onNotify("repl_error", (params) => {
      this.term.writeln(`\r\n[repl error: ${params.message || "unknown"}]`);
    });
  }

  async start(): Promise<void> {
    this.term.clear();
    await this.rpc.call("repl_start");
    this.started = true;
    this.term.writeln("[connected — press Enter for a prompt]");
  }

  async stop(): Promise<void> {
    this.started = false;
    try {
      await this.rpc.call("repl_stop");
    } catch {
      /* board may already be gone */
    }
  }
}
