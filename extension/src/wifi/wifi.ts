import * as fs from "fs";
import * as os from "os";
import * as path from "path";
import * as vscode from "vscode";
import { BoardIdentity, SidecarBridge, isWifiDevice } from "../bridge/SidecarBridge";

/**
 * Wi-Fi as a first-class connection in the extension.
 *
 * - Remembered boards live in ~/.mpftp/config.json under "wifiBoards", keyed
 *   by machine.unique_id() hex, the same shape mpftp.boards (CLI, PWA) uses.
 * - Passwords live in VS Code's SecretStorage, one per board, never on disk
 *   in the clear and never in a log.
 * - "Enable / Disable Wi-Fi access" shows the exact boot.py change and writes
 *   nothing without a yes.
 *
 * The design note is docs/plans/wifi-webrepl.md.
 */

const CONFIG_FILE = path.join(os.homedir(), ".mpftp", "config.json");
const WIFI_KEY = "wifiBoards";
const DEFAULT_PORT = 8266;
const SECRET_PREFIX = "mpftp.webrepl.";

/** WebREPL keeps 9 characters; mpftp writes 4 to 9 (upstream webrepl_setup's rule). */
export const MAX_PASSWORD = 9;
export const MIN_NEW_PASSWORD = 4;

export type WifiBoard = {
  uid: string;
  name: string;
  ip: string;
  port?: number;
  hostname?: string;
  machine?: string;
  seen?: string;
};

function readConfig(): Record<string, any> {
  try {
    const parsed = JSON.parse(fs.readFileSync(CONFIG_FILE, "utf8"));
    return parsed && typeof parsed === "object" ? parsed : {};
  } catch {
    return {};
  }
}

function writeConfig(cfg: Record<string, unknown>): void {
  const dir = path.dirname(CONFIG_FILE);
  fs.mkdirSync(dir, { recursive: true });
  const tmp = path.join(dir, `.config-${process.pid}-${Date.now()}.json.tmp`);
  fs.writeFileSync(tmp, JSON.stringify(cfg, null, 2) + "\n", "utf8");
  fs.renameSync(tmp, CONFIG_FILE);
}

function parseHost(device: string): { host: string; port: number } | undefined {
  const m = /^wss?:\/\/([^/:]+)(?::(\d+))?/i.exec(device.trim());
  if (!m) {
    return undefined;
  }
  return { host: m[1].toLowerCase(), port: m[2] ? Number(m[2]) : DEFAULT_PORT };
}

export function deviceFor(board: WifiBoard): string {
  const port = board.port || DEFAULT_PORT;
  return `ws://${board.ip}${port === DEFAULT_PORT ? "" : `:${port}`}`;
}

export function loadBoards(): WifiBoard[] {
  const raw = readConfig()[WIFI_KEY];
  if (!raw || typeof raw !== "object") {
    return [];
  }
  const rows: WifiBoard[] = [];
  for (const [uid, entry] of Object.entries(raw as Record<string, any>)) {
    if (entry && typeof entry === "object" && entry.ip) {
      rows.push({ uid, ...entry, name: entry.name || uid });
    }
  }
  rows.sort((a, b) => String(b.seen || "").localeCompare(String(a.seen || "")));
  return rows;
}

export function uidForDevice(device: string): string | undefined {
  const parsed = parseHost(device);
  if (!parsed) {
    return undefined;
  }
  const short = parsed.host.endsWith(".local") ? parsed.host.slice(0, -6) : parsed.host;
  for (const b of loadBoards()) {
    if (parsed.host === String(b.ip).toLowerCase()) {
      return b.uid;
    }
    if (short && (short === String(b.hostname || "").toLowerCase() || short === b.name.toLowerCase())) {
      return b.uid;
    }
  }
  return undefined;
}

function timestamp(): string {
  const d = new Date();
  const pad = (n: number) => String(n).padStart(2, "0");
  return (
    `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}` +
    `T${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`
  );
}

/** Remember a board a connect found with Wi-Fi up (or reached over Wi-Fi). */
export function rememberBoard(identity: BoardIdentity | undefined, device?: string): WifiBoard | undefined {
  const uid = String(identity?.uid || "").toLowerCase();
  if (!uid) {
    return undefined;
  }
  const typed = device && isWifiDevice(device) ? parseHost(device) : undefined;
  const ip = identity?.ip || typed?.host;
  if (!ip) {
    return undefined;
  }
  const cfg = readConfig();
  const all = (cfg[WIFI_KEY] && typeof cfg[WIFI_KEY] === "object" ? cfg[WIFI_KEY] : {}) as Record<
    string,
    any
  >;
  const entry = { ...(all[uid] || {}) };
  entry.name = entry.name || identity?.hostname || uid;
  entry.ip = ip;
  entry.port = typed?.port || DEFAULT_PORT;
  entry.seen = timestamp();
  if (identity?.hostname) {
    entry.hostname = identity.hostname;
  }
  if (identity?.machine) {
    entry.machine = identity.machine;
  }
  all[uid] = entry;
  cfg[WIFI_KEY] = all;
  try {
    writeConfig(cfg);
  } catch {
    return undefined;
  }
  return { uid, ...entry };
}

export function forgetBoard(uid: string): void {
  const cfg = readConfig();
  if (cfg[WIFI_KEY] && typeof cfg[WIFI_KEY] === "object" && uid in cfg[WIFI_KEY]) {
    delete cfg[WIFI_KEY][uid];
    writeConfig(cfg);
  }
}

/** One WebREPL password per board in SecretStorage: by uid, else by address. */
export class WifiPasswords {
  constructor(private readonly secrets: vscode.SecretStorage) {}

  private keys(deviceOrUid: string): string[] {
    if (!isWifiDevice(deviceOrUid)) {
      return [SECRET_PREFIX + deviceOrUid.toLowerCase()];
    }
    const keys: string[] = [];
    const uid = uidForDevice(deviceOrUid);
    if (uid) {
      keys.push(SECRET_PREFIX + uid);
    }
    const parsed = parseHost(deviceOrUid);
    if (parsed) {
      keys.push(`${SECRET_PREFIX}host:${parsed.host}:${parsed.port}`);
    }
    return keys;
  }

  async get(deviceOrUid: string): Promise<string | undefined> {
    for (const key of this.keys(deviceOrUid)) {
      const value = await this.secrets.get(key);
      if (value) {
        return value;
      }
    }
    return undefined;
  }

  async set(deviceOrUid: string, password: string): Promise<void> {
    const key = this.keys(deviceOrUid)[0];
    if (key) {
      await this.secrets.store(key, password);
    }
  }

  async delete(deviceOrUid: string): Promise<void> {
    for (const key of this.keys(deviceOrUid)) {
      await this.secrets.delete(key);
    }
  }
}

export function needsPassword(message: string): boolean {
  return /no WebREPL password|rejected the WebREPL password/i.test(message);
}

function validateConnectPassword(value: string): string | undefined {
  if (!value) {
    return "Type the board's WebREPL password";
  }
  if (value.length > MAX_PASSWORD) {
    return `WebREPL keeps at most ${MAX_PASSWORD} characters, so this can't be the board's password`;
  }
  return undefined;
}

function validateNewPassword(value: string): string | undefined {
  if (value.length < MIN_NEW_PASSWORD || value.length > MAX_PASSWORD) {
    return `${MIN_NEW_PASSWORD} to ${MAX_PASSWORD} characters: WebREPL keeps only ${MAX_PASSWORD}`;
  }
  if (!/^[\x20-\x7e]+$/.test(value)) {
    return "Plain printable ASCII only";
  }
  return undefined;
}

/**
 * Connect over Wi-Fi: the saved password first, then ask (up to three
 * times) when the board has none saved or turns it down. A password that
 * works is saved under the board's uid.
 */
export async function connectWifi(
  bridge: SidecarBridge,
  passwords: WifiPasswords,
  device: string
): Promise<{ filesystem_warning?: string } | void> {
  let typed: string | undefined;
  for (let attempt = 0; ; attempt++) {
    try {
      const res = await vscode.window.withProgress(
        { location: vscode.ProgressLocation.Notification, title: `mpftp: connecting ${device}…` },
        () => bridge.connect(device, undefined, { password: typed })
      );
      if (typed) {
        const uid = (res as any)?.board?.uid;
        await passwords.set(uid || device, typed);
      }
      return res;
    } catch (e: any) {
      const message = String(e?.message || e);
      if (!needsPassword(message) || attempt >= 3) {
        throw e;
      }
      typed = await vscode.window.showInputBox({
        title: `WebREPL password for ${device}`,
        prompt: /rejected/i.test(message)
          ? "The board said no to that password. Try again."
          : "mpftp has no password saved for this board. It's kept in VS Code's secret storage.",
        password: true,
        ignoreFocusOut: true,
        validateInput: validateConnectPassword,
      });
      if (!typed) {
        throw new Error("connect cancelled");
      }
    }
  }
}

/** The typed-address box, with an mDNS look-up for NAME.local names. */
export async function askWifiAddress(bridge: SidecarBridge): Promise<string | undefined> {
  const value = await vscode.window.showInputBox({
    title: "Connect over Wi-Fi",
    prompt:
      "The board's IP address or its NAME.local (e.g. mpy-esp32p4.local). It needs Wi-Fi up and " +
      "WebREPL started; \"mpftp: Enable Wi-Fi Access\" sets that up over USB.",
    placeHolder: "192.168.1.50 or mpy-esp32p4.local",
    ignoreFocusOut: true,
  });
  const host = value?.trim().replace(/^wss?:\/\//i, "");
  if (!host) {
    return undefined;
  }
  const name = host.split(":")[0];
  if (name.toLowerCase().endsWith(".local")) {
    // Resolve where the sidecar runs: on WSL that's Windows Python, whose
    // resolver speaks mDNS. Best effort; a miss falls through to the name.
    try {
      const res = await vscode.window.withProgress(
        { location: vscode.ProgressLocation.Notification, title: `mpftp: looking up ${name}…` },
        () => bridge.request<{ ip?: string; via?: string }>("mdns_resolve", { name })
      );
      if (res.ip) {
        return `ws://${res.ip}${host.slice(name.length)}`;
      }
      void vscode.window.showWarningMessage(
        `Nobody answered for ${name} (mDNS is best effort). Trying the name anyway; ` +
          "connect once over USB and mpftp remembers the board's address."
      );
    } catch {
      /* fall through to the name */
    }
  }
  return `ws://${host}`;
}

/** Virtual documents for the boot.py before/after diff editor. */
class BootDiffDocs implements vscode.TextDocumentContentProvider {
  static readonly scheme = "mpftp-boot";
  private readonly docs = new Map<string, string>();
  set(name: string, text: string): vscode.Uri {
    this.docs.set(name, text);
    return vscode.Uri.from({ scheme: BootDiffDocs.scheme, path: `/${name}/boot.py` });
  }
  provideTextDocumentContent(uri: vscode.Uri): string {
    return this.docs.get(uri.path.split("/")[1]) ?? "";
  }
}

let bootDocs: BootDiffDocs | undefined;

export function registerWifi(context: vscode.ExtensionContext): void {
  bootDocs = new BootDiffDocs();
  context.subscriptions.push(
    vscode.workspace.registerTextDocumentContentProvider(BootDiffDocs.scheme, bootDocs)
  );
}

type Plan = {
  action: string;
  exists: boolean;
  delete: boolean;
  problems: string[];
  sha256: string | null;
  current_masked: string | null;
  proposed_masked: string | null;
  diff: string;
};

/**
 * Enable or disable Wi-Fi access in the board's boot.py. Shows the diff (an
 * editor diff plus the same text in the confirmation) and writes only on yes.
 */
export async function wifiAccessCommand(
  bridge: SidecarBridge,
  passwords: WifiPasswords,
  action: "enable" | "disable",
  log: vscode.OutputChannel
): Promise<void> {
  const device = bridge.connectedDevice || "";
  const overWifi = isWifiDevice(device);
  if (action === "enable" && overWifi) {
    void vscode.window.showWarningMessage(
      "Enable Wi-Fi access over a USB connection: it writes boot.py, and you need a way back if Wi-Fi doesn't come up."
    );
    return;
  }
  let password: string | undefined;
  if (action === "enable") {
    password = await vscode.window.showInputBox({
      title: "Enable Wi-Fi access: WebREPL password (1 of 2)",
      prompt:
        "The board will start WebREPL with this password at every reset. WebREPL has no encryption; use it on a network you trust.",
      password: true,
      ignoreFocusOut: true,
      validateInput: validateNewPassword,
    });
    if (!password) {
      return;
    }
    const again = await vscode.window.showInputBox({
      title: "Enable Wi-Fi access: the same password again (2 of 2)",
      password: true,
      ignoreFocusOut: true,
      validateInput: (v) => (v === password ? undefined : "Doesn't match the first one"),
    });
    if (again !== password) {
      return;
    }
  }

  const plan = await vscode.window.withProgress(
    { location: vscode.ProgressLocation.Notification, title: "mpftp: reading boot.py…" },
    () => bridge.request<Plan>("wifi_access_plan", { action, password })
  );
  if (plan.problems?.length) {
    void vscode.window.showErrorMessage(plan.problems.join(" "));
    return;
  }

  if (bootDocs) {
    const stamp = String(Date.now());
    const left = bootDocs.set(`before-${stamp}`, plan.current_masked ?? "");
    const right = bootDocs.set(`after-${stamp}`, plan.proposed_masked ?? "");
    await vscode.commands.executeCommand(
      "vscode.diff",
      left,
      right,
      `boot.py on the board: ${action === "enable" ? "enable" : "disable"} Wi-Fi access (proposed)`,
      { preview: true }
    );
  }
  const verb = plan.delete ? "Delete boot.py" : "Write to boot.py";
  const detail =
    plan.diff +
    (action === "enable" ? "\nThe password shows as ***** here; the board gets the real one." : "");
  const choice = await vscode.window.showWarningMessage(
    action === "enable"
      ? "Add this Wi-Fi block to the top of the board's boot.py?"
      : "Remove mpftp's Wi-Fi block from the board's boot.py?",
    { modal: true, detail },
    verb
  );
  if (choice !== verb) {
    return;
  }

  const res = await vscode.window.withProgress(
    {
      location: vscode.ProgressLocation.Notification,
      title: action === "enable" ? "mpftp: writing boot.py and joining Wi-Fi…" : "mpftp: writing boot.py…",
    },
    () =>
      bridge.request<{ ip?: string; board?: BoardIdentity; started?: boolean }>("wifi_access_apply", {
        action,
        password,
        expect_sha256: plan.sha256,
        now: !overWifi,
      })
  );
  log.appendLine(`[mpftp] Wi-Fi access ${action}d in boot.py`);
  if (action === "enable") {
    const board = res.board;
    if (password && board?.uid) {
      await passwords.set(board.uid, password);
    }
    const remembered = rememberBoard(board);
    const where = res.ip ? `ws://${res.ip}` : "its address once Wi-Fi is up";
    void vscode.window.showInformationMessage(
      `Wi-Fi access is on. boot.py starts it at every reset; reach the board at ${where}` +
        (remembered ? ` ("${remembered.name}" in the Connect list).` : ".")
    );
  } else {
    void vscode.window.showInformationMessage(
      "Wi-Fi access is off from the next reset; boot.py is back as it was."
    );
  }
}
