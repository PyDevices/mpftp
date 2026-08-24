import * as vscode from "vscode";
import { ActivityLog } from "../activityLog";
import { SidecarBridge } from "../bridge/SidecarBridge";

/**
 * VS Code Pseudoterminal backed by the mpremote sidecar.
 * Bytes from the board are written as-is so ANSI/VT color sequences render
 * in the integrated terminal (xterm.js). Also mirrored to ~/.mpftp/repl.log.
 */
export class ReplTerminal implements vscode.Pseudoterminal {
  private readonly writeEmitter = new vscode.EventEmitter<string>();
  private readonly closeEmitter = new vscode.EventEmitter<number | void>();
  onDidWrite = this.writeEmitter.event;
  onDidClose = this.closeEmitter.event;

  private onData?: (params: { data_b64: string }) => void;
  private onErr?: (params: { message: string }) => void;
  private onExit?: () => void;

  constructor(
    private readonly bridge: SidecarBridge,
    private readonly activity?: ActivityLog
  ) {}

  open(): void {
    const device = this.bridge.connectedDevice || "board";
    const banner = `mpftp REPL — ${device} (Ctrl+] to close)\r\n`;
    this.writeEmitter.fire(`\x1b[90m${banner}\x1b[0m`);
    this.activity?.event("repl_open", { source: "repl", message: device });
    this.activity?.appendRepl(`\n----- REPL open ${device} ${new Date().toISOString()} -----\n`);

    this.onData = (params) => {
      const buf = Buffer.from(params.data_b64, "base64");
      // Pseudoterminal expects string; pass through Latin1 to preserve all bytes including ESC.
      const text = buf.toString("latin1");
      this.writeEmitter.fire(text);
      this.activity?.appendRepl(buf.toString("utf8"));
    };
    this.onErr = (params) => {
      this.writeEmitter.fire(`\r\n\x1b[31m[repl error] ${params.message}\x1b[0m\r\n`);
      this.activity?.event("repl_error", { source: "repl", message: params.message });
    };
    this.onExit = () => {
      this.writeEmitter.fire("\r\n\x1b[90m[disconnected]\x1b[0m\r\n");
      this.activity?.event("repl_close", { source: "repl", message: "closed" });
      this.activity?.appendRepl(`\n----- REPL close ${new Date().toISOString()} -----\n`);
      this.closeEmitter.fire(0);
    };

    this.bridge.on("repl_data", this.onData);
    this.bridge.on("repl_error", this.onErr);
    this.bridge.on("exit", this.onExit);
    this.bridge.on("disconnected", this.onExit);

    void this.bridge
      .request("repl_start")
      .then(() => {
        // MicroPython / CircuitPython treat CR as Enter; nudge so ">>> " appears.
        const b64 = Buffer.from("\r", "latin1").toString("base64");
        return this.bridge.request("repl_write", { data_b64: b64 });
      })
      .catch((e) => {
        this.writeEmitter.fire(`\x1b[31mFailed to start REPL: ${e}\x1b[0m\r\n`);
      });
  }

  close(): void {
    if (this.onData) {
      this.bridge.off("repl_data", this.onData);
    }
    if (this.onErr) {
      this.bridge.off("repl_error", this.onErr);
    }
    if (this.onExit) {
      this.bridge.off("exit", this.onExit);
      this.bridge.off("disconnected", this.onExit);
    }
    void this.bridge.request("repl_stop").catch(() => undefined);
  }

  handleInput(data: string): void {
    // Ctrl+] closes
    if (data === "\x1d") {
      this.closeEmitter.fire(0);
      return;
    }
    // Cursor/Python extension auto-runs `source …/.venv/bin/activate` on new
    // terminals via sendText — that must not reach MicroPython.
    if (isIdeShellInjection(data)) {
      this.activity?.event("repl_ignore_injection", {
        source: "repl",
        message: data.trim().slice(0, 120),
      });
      return;
    }
    this.activity?.appendRepl(data);
    const b64 = Buffer.from(data, "latin1").toString("base64");
    void this.bridge.request("repl_write", { data_b64: b64 }).catch((e) => {
      this.writeEmitter.fire(`\r\n\x1b[31mwrite failed: ${e}\x1b[0m\r\n`);
    });
  }
}

/** Detect shell env-activation text injected by the IDE into new terminals. */
function isIdeShellInjection(data: string): boolean {
  const cleaned = data.replace(/\r\n/g, "\n").replace(/\r/g, "\n").trim();
  if (!cleaned) {
    return false;
  }
  if (/^source\s+\S*activate\S*/m.test(cleaned)) {
    return true;
  }
  if (/^\.\s+\S*activate\S*/m.test(cleaned)) {
    return true;
  }
  if (/^conda\s+activate\b/m.test(cleaned)) {
    return true;
  }
  if (/activate\.(ps1|bat|fish)\b/i.test(cleaned) && /(venv|virtualenv|\.venv)/i.test(cleaned)) {
    return true;
  }
  if (/\.venv[/\\](?:bin|Scripts)[/\\]activate/.test(cleaned)) {
    return true;
  }
  return false;
}

let activeRepl: vscode.Terminal | undefined;
let closeWatch: vscode.Disposable | undefined;

function isMpftpRepl(term: vscode.Terminal): boolean {
  return term.name.startsWith("mpftp:");
}

function ensureCloseWatch(): void {
  if (closeWatch) {
    return;
  }
  closeWatch = vscode.window.onDidCloseTerminal((term) => {
    if (term === activeRepl) {
      activeRepl = undefined;
    }
  });
}

/** Dispose every mpftp REPL terminal (name prefix ``mpftp:``). */
function disposeMpftpRepls(): void {
  activeRepl = undefined;
  for (const term of [...vscode.window.terminals]) {
    if (isMpftpRepl(term)) {
      term.dispose();
    }
  }
}

export function openRepl(bridge: SidecarBridge, activity?: ActivityLog): vscode.Terminal {
  ensureCloseWatch();

  if (activeRepl && vscode.window.terminals.includes(activeRepl)) {
    // Keep the same PTY session; just focus it.
    const want = `mpftp: ${bridge.connectedDevice || "REPL"}`;
    if (activeRepl.name !== want) {
      // Device changed — cannot rename an existing terminal; replace it.
      disposeMpftpRepls();
    } else {
      activeRepl.show();
      return activeRepl;
    }
  } else {
    // Stale ref or duplicates from earlier opens — clear before creating.
    disposeMpftpRepls();
  }

  const pty = new ReplTerminal(bridge, activity);
  const term = vscode.window.createTerminal({
    name: `mpftp: ${bridge.connectedDevice || "REPL"}`,
    pty,
    // Avoid treating this PTY like a host shell (reduces shell-integration hooks).
    isTransient: true,
  });
  activeRepl = term;
  term.show();
  return term;
}
