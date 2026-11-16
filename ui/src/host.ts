/**
 * The page side of the File Transfer panel's host.
 *
 * The panel is the VS Code extension's own script (ftp.js), unchanged. It
 * calls acquireVsCodeApi().postMessage({type: ...}) and listens for window
 * "message" events. This file supplies that API: messages go up the
 * WebSocket to mcuctl.panel (the Python port of FtpViewProvider), and the
 * replies are dispatched as "message" events, so ftp.js can't tell it isn't
 * in a VS Code webview.
 *
 * A few messages are the page's own business, as they are the extension's
 * in VS Code: connecting, the REPL, and the mcuctl.* commands. `local`
 * answers those and returns true.
 */
import type { Rpc } from "./rpc";

/** The menu's mcuctl.* commands this page can run (ftp.js hides the rest). */
export const BROWSER_COMMANDS = [
  "mcuctl.connect",
  "mcuctl.disconnect",
  "mcuctl.resume",
  "mcuctl.interrupt",
  "mcuctl.softReset",
  "mcuctl.hardReset",
  "mcuctl.openRepl",
  "mcuctl.runFile",
  "mcuctl.openFirmware",
  "mcuctl.enableWifiAccess",
  "mcuctl.disableWifiAccess",
  "mcuctl.editRemote",
  "mcuctl.eval",
  "mcuctl.exec",
  "mcuctl.bootloader",
  "mcuctl.rtcGet",
  "mcuctl.rtcSet",
  "mcuctl.installPackage",
  "mcuctl.searchPackages",
  "mcuctl.df",
  "mcuctl.romfsQuery",
  "mcuctl.hashRemote",
];

/** The menu's names for what a command does here, where that differs from VS Code. */
export const BROWSER_COMMAND_TITLES: Record<string, string> = {
  "mcuctl.openFirmware": "Flash Firmware (esptool)…",
};

/** What the panel last said about the board (its "state" message). */
export interface PanelState {
  connected: boolean;
  device: string;
  interpreter: string;
  /** The folders the Local and Board lists are showing. */
  localPath: string;
  remotePath: string;
}

/** Hand ftp.js a message as if its VS Code host had posted it. */
export function toPanel(msg: unknown): void {
  window.dispatchEvent(new MessageEvent("message", { data: msg }));
}

/** Show a line in the panel's status footer. */
export function panelStatus(text: string, phase: "idle" | "active" | "stalled" | "done" = "idle"): void {
  toPanel({ type: "status", text, phase });
}

export function installVsCodeShim(
  rpc: Rpc,
  opts: {
    /** A message the page answers itself; true when handled. */
    local: (msg: any) => boolean;
    /** A panel_host message from the server (ask, info, error, open, ...). */
    onHost: (msg: any) => void;
    /** Every "state" message, after ftp.js has seen it. */
    onState: (state: PanelState) => void;
  }
): void {
  let saved: unknown = undefined;
  const api = {
    postMessage(msg: any): void {
      if (msg && typeof msg === "object" && opts.local(msg)) {
        return;
      }
      rpc.sendPanel(msg);
    },
    getState(): unknown {
      return saved;
    },
    setState(state: unknown): void {
      saved = state;
    },
  };
  (window as any).acquireVsCodeApi = () => api;

  rpc.onPanel((m) => {
    if (m.type !== "panel") {
      opts.onHost(m);
      return;
    }
    const msg = m.msg || {};
    if (msg.type === "state") {
      msg.commands = BROWSER_COMMANDS;
      msg.commandTitles = BROWSER_COMMAND_TITLES;
    }
    toPanel(msg);
    if (msg.type === "state") {
      opts.onState({
        connected: !!msg.connected,
        device: String(msg.device || ""),
        interpreter: String(msg.interpreter || ""),
        localPath: String(msg.localPath || ""),
        remotePath: String(msg.remotePath || ""),
      });
    }
  });

  // ftp.js says "ready" once, when it loads (queued until the socket opens).
  // After the server comes back, say it again so the lists repopulate.
  let opened = false;
  rpc.onStatus((up) => {
    if (!up) {
      panelStatus("mcuctl server unreachable — retrying…", "stalled");
      return;
    }
    if (opened) {
      rpc.sendPanel({ type: "ready" });
    }
    opened = true;
  });
}
