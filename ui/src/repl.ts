import { Terminal } from "@xterm/xterm";
import { FitAddon } from "@xterm/addon-fit";
import type { Rpc } from "./rpc";
import { KeyGate } from "./keygate";

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
  private keys: KeyGate;
  private fitAddon = new FitAddon();
  private outputListeners: Array<(bytes: Uint8Array) => void> = [];

  constructor(container: HTMLElement, rpc: Rpc) {
    this.rpc = rpc;
    this.keys = new KeyGate((data) => {
      const bytes = new TextEncoder().encode(data);
      this.rpc.call("repl_write", { data_b64: bytesToBase64(bytes) }).catch((e) => {
        this.term.writeln(`\r\n[repl_write failed: ${e.message}]`);
      });
    });
    this.term = new Terminal({
      convertEol: true,
      cursorBlink: true,
      fontFamily: '"JetBrains Mono", Menlo, Consolas, monospace',
      fontSize: 13,
      theme: { background: "#080c14", foreground: "#f8fafc", cursor: "#f54e00" },
    });
    this.term.loadAddon(this.fitAddon);
    this.term.open(container);
    // The pane is resizable (splitters, window): keep the terminal filling it.
    new ResizeObserver(() => this.fit()).observe(container);
    this.term.writeln("mpftp REPL — connect to a board to begin.");

    this.term.onData((data: string) => this.keys.key(data));

    rpc.onNotify("repl_data", (params) => {
      const bytes = base64ToBytes(params.data_b64 || "");
      this.term.write(bytes);
      for (const fn of this.outputListeners) {
        fn(bytes);
      }
    });
    rpc.onNotify("repl_error", (params) => {
      this.term.writeln(`\r\n[repl error: ${params.message || "unknown"}]`);
    });
  }

  async start(): Promise<void> {
    this.term.clear();
    this.keys.hold();
    try {
      await this.rpc.call("repl_start");
    } catch (e) {
      this.keys.close();
      throw e;
    }
    this.term.writeln("[connected — press Enter for a prompt]");
    this.keys.open();
  }

  async stop(): Promise<void> {
    this.keys.close();
    try {
      await this.rpc.call("repl_stop");
    } catch {
      /* board may already be gone */
    }
  }

  /** Also hand the board's output to `fn` (the plotter). */
  onOutput(fn: (bytes: Uint8Array) => void): void {
    this.outputListeners.push(fn);
  }

  fit(): void {
    try {
      this.fitAddon.fit();
    } catch {
      /* not laid out yet */
    }
  }

  get running(): boolean {
    return this.keys.isOpen;
  }

  /** The board went away: stop sending keys (nothing to tell the sidecar). */
  detach(): void {
    this.keys.close();
  }

  focus(): void {
    this.term.focus();
  }

  /** Several lines from mpftp itself (exec output, df, ...). */
  block(text: string): void {
    this.term.write("\r\n" + text.replace(/\r?\n/g, "\r\n") + "\r\n");
  }

  /** A line from mpftp itself (not the board), e.g. why a connect failed. */
  note(text: string): void {
    this.term.writeln(`\r\n[${text}]`);
  }

  setTheme(dark: boolean): void {
    this.term.options.theme = dark
      ? { background: "#080c14", foreground: "#f8fafc", cursor: "#f54e00" }
      : { background: "#ffffff", foreground: "#242424", cursor: "#0f6cbd", selectionBackground: "#cfe4fa" };
  }
}
