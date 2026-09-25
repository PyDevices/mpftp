import * as fs from "fs";
import * as os from "os";
import * as path from "path";
import * as vscode from "vscode";
import { BoardIdentity, SidecarBridge, isBleDevice, isWifiDevice } from "../bridge/SidecarBridge";

export { isBleDevice };

/**
 * Wi-Fi as a first-class connection in the extension.
 *
 * - Remembered boards live in ~/.mpftp/config.json under "wifiBoards", keyed
 *   by machine.unique_id() hex, the same shape mpftp.boards (CLI, PWA) uses.
 * - Passwords live in VS Code's SecretStorage, one per board, and never in a
 *   log. The CLI, the PWA and agents keep theirs in
 *   ~/.mpftp/webrepl-passwords.json (plaintext, 0600). The extension reads
 *   that file too, and writes a password there only when you say it may
 *   (mpftp.sharePasswords), so a board set up here is reachable from
 *   everything else (mpftp#43). Same keys as mpftp.boards: the board's uid,
 *   "host:HOST:PORT", or "ble:NAME".
 * - "Enable / Disable Wi-Fi access" shows the exact boot.py change and writes
 *   nothing without a yes.
 *
 * The design note is docs/plans/wifi-webrepl.md.
 */

const CONFIG_FILE = path.join(os.homedir(), ".mpftp", "config.json");
const PASSWORDS_FILE = path.join(os.homedir(), ".mpftp", "webrepl-passwords.json");
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

/**
 * A board's keys in the password store, preferred first: its uid, else
 * "host:HOST:PORT", or "ble:NAME". The same keys mpftp.boards.password_key
 * uses for ~/.mpftp/webrepl-passwords.json.
 */
export function passwordKeys(deviceOrUid: string): string[] {
  const value = deviceOrUid.trim();
  if (isBleDevice(value)) {
    return [`ble:${value.slice("ble://".length).toLowerCase()}`];
  }
  if (!isWifiDevice(value)) {
    return [value.toLowerCase()];
  }
  const keys: string[] = [];
  const uid = uidForDevice(value);
  if (uid) {
    keys.push(uid);
  }
  const parsed = parseHost(value);
  if (parsed) {
    keys.push(`host:${parsed.host}:${parsed.port}`);
  }
  return keys;
}

/** ~/.mpftp/webrepl-passwords.json: what the CLI, the PWA and agents read. */
export function readSharedPasswords(file: string = PASSWORDS_FILE): Record<string, string> {
  try {
    const parsed = JSON.parse(fs.readFileSync(file, "utf8"));
    if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
      return {};
    }
    const out: Record<string, string> = {};
    for (const [k, v] of Object.entries(parsed)) {
      out[String(k)] = String(v);
    }
    return out;
  } catch {
    return {};
  }
}

/**
 * Rewrite the shared file the way mpftp.boards does: sorted keys, a temp file
 * created 0600 in the same folder, then a rename over the old one.
 */
export function writeSharedPasswords(data: Record<string, string>, file: string = PASSWORDS_FILE): void {
  const dir = path.dirname(file);
  fs.mkdirSync(dir, { recursive: true });
  const tmp = path.join(dir, `.webrepl-${process.pid}-${Date.now()}.json`);
  const sorted: Record<string, string> = {};
  for (const k of Object.keys(data).sort()) {
    sorted[k] = data[k];
  }
  try {
    fs.writeFileSync(tmp, JSON.stringify(sorted, null, 2) + "\n", { encoding: "utf8", mode: 0o600 });
    try {
      fs.chmodSync(tmp, 0o600); // mode is masked by the umask on create
    } catch {
      /* Windows: no POSIX modes; the file inherits the profile's ACL */
    }
    fs.renameSync(tmp, file);
  } catch (e) {
    try {
      fs.unlinkSync(tmp);
    } catch {
      /* already gone */
    }
    throw e;
  }
}

export type ShareMode = "ask" | "always" | "never";

function shareMode(): ShareMode {
  const v = vscode.workspace.getConfiguration("mpftp").get<string>("sharePasswords", "ask");
  return v === "always" || v === "never" ? v : "ask";
}

const DECLINED_KEY = "mpftp.passwordsKeptInVsCode";

/**
 * One password per board (WebREPL or bledev), in SecretStorage, and in
 * ~/.mpftp/webrepl-passwords.json when you let it be shared. Lookups try
 * SecretStorage first, then the shared file, so a password saved by the CLI
 * or the PWA works here too.
 */
export class WifiPasswords {
  constructor(
    private readonly secrets: vscode.SecretStorage,
    private readonly state?: vscode.Memento,
    private readonly file: string = PASSWORDS_FILE
  ) {}

  async get(deviceOrUid: string): Promise<string | undefined> {
    const keys = passwordKeys(deviceOrUid);
    for (const key of keys) {
      const value = await this.secrets.get(SECRET_PREFIX + key);
      if (value) {
        return value;
      }
    }
    const shared = readSharedPasswords(this.file);
    for (const key of keys) {
      if (shared[key]) {
        return shared[key];
      }
    }
    return undefined;
  }

  /**
   * Keep a password that works. It always goes in SecretStorage; it goes in
   * the shared file too when mpftp.sharePasswords says so, or, set to "ask",
   * when you say yes. A board you kept to VS Code isn't asked about again.
   */
  async set(deviceOrUid: string, password: string): Promise<void> {
    const key = passwordKeys(deviceOrUid)[0];
    if (!key) {
      return;
    }
    await this.secrets.store(SECRET_PREFIX + key, password);
    const shared = readSharedPasswords(this.file);
    if (shared[key] === password) {
      return;
    }
    const mode = shareMode();
    if (mode === "never") {
      return;
    }
    if (mode === "ask") {
      const declined = this.state?.get<string[]>(DECLINED_KEY, []) ?? [];
      if (declined.includes(key)) {
        return;
      }
      const share = "Save it for all of mpftp";
      const keep = "Keep it in VS Code only";
      const choice = await vscode.window.showInformationMessage(
        "Let the mpftp command line, the PWA and agents use this board's password too?",
        {
          modal: true,
          detail:
            `They read ${this.file}. It is plaintext on disk, readable only by you (mode 0600). ` +
            "VS Code keeps its own copy in its secret storage either way. " +
            'The setting "mpftp.sharePasswords" answers this for every board.',
        },
        share,
        keep
      );
      if (choice !== share) {
        if (choice === keep && this.state) {
          await this.state.update(DECLINED_KEY, [...declined, key]);
        }
        return;
      }
    }
    shared[key] = password;
    writeSharedPasswords(shared, this.file);
  }

  /** Forget a board's password everywhere mpftp keeps one. */
  async delete(deviceOrUid: string): Promise<void> {
    const keys = passwordKeys(deviceOrUid);
    for (const key of keys) {
      await this.secrets.delete(SECRET_PREFIX + key);
    }
    const shared = readSharedPasswords(this.file);
    if (keys.some((k) => k in shared)) {
      for (const key of keys) {
        delete shared[key];
      }
      writeSharedPasswords(shared, this.file);
    }
    if (this.state) {
      const declined = this.state.get<string[]>(DECLINED_KEY, []);
      await this.state.update(
        DECLINED_KEY,
        declined.filter((k) => !keys.includes(k))
      );
    }
  }

  /** Where a password saved now would end up, for the Enable dialog. */
  whereSaved(): string {
    const mode = shareMode();
    if (mode === "never") {
      return "mpftp keeps the password in VS Code's secret storage only (mpftp.sharePasswords is \"never\").";
    }
    if (mode === "always") {
      return (
        `mpftp keeps the password in VS Code's secret storage and in ${this.file} ` +
        "(plaintext, readable only by you), where the command line, the PWA and agents find it."
      );
    }
    return (
      "mpftp keeps the password in VS Code's secret storage, then asks whether the command line, " +
      `the PWA and agents may have it too (in ${this.file}, plaintext, readable only by you).`
    );
  }
}

export function needsPassword(message: string): boolean {
  return /no (WebREPL|BLE REPL) password|rejected the (WebREPL|BLE REPL) password/i.test(message);
}

/** bledev.repl takes 4 to 64 characters, with no line breaks. */
export const MIN_BLE_PASSWORD = 4;
export const MAX_BLE_PASSWORD = 64;

function validateBlePassword(value: string): string | undefined {
  if (value.length < MIN_BLE_PASSWORD || value.length > MAX_BLE_PASSWORD || /[\r\n]/.test(value)) {
    return `A bledev password is ${MIN_BLE_PASSWORD} to ${MAX_BLE_PASSWORD} characters, with no line breaks`;
  }
  return undefined;
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
 * Connect over Wi-Fi or Bluetooth: the saved password first, then ask (up to
 * three times) when the board has none saved or turns it down. A password
 * that works is saved under the board's uid (Wi-Fi) or its name (Bluetooth).
 */
export async function connectWifi(
  bridge: SidecarBridge,
  passwords: WifiPasswords,
  device: string
): Promise<{ filesystem_warning?: string } | void> {
  let typed: string | undefined;
  const ble = isBleDevice(device);
  for (let attempt = 0; ; attempt++) {
    try {
      const res = await vscode.window.withProgress(
        { location: vscode.ProgressLocation.Notification, title: `mpftp: connecting ${device}…` },
        () => bridge.connect(device, undefined, { password: typed })
      );
      if (typed) {
        const uid = (res as any)?.board?.uid;
        await passwords.set(ble ? device : uid || device, typed);
      }
      return res;
    } catch (e: any) {
      const message = String(e?.message || e);
      if (!needsPassword(message) || attempt >= 3) {
        throw e;
      }
      typed = await vscode.window.showInputBox({
        title: `${ble ? "bledev" : "WebREPL"} password for ${device}`,
        prompt: /rejected/i.test(message)
          ? "The board said no to that password. Try again."
          : "mpftp has no password saved for this board. VS Code keeps it in its secret storage.",
        password: true,
        ignoreFocusOut: true,
        validateInput: ble ? validateBlePassword : validateConnectPassword,
      });
      if (!typed) {
        throw new Error("connect cancelled");
      }
    }
  }
}

/**
 * Scan for bledev boards and pick one (or type its advertised name). Returns
 * the ble:// device, or undefined when cancelled.
 */
export async function pickBleBoard(bridge: SidecarBridge): Promise<string | undefined> {
  type Item = vscode.QuickPickItem & { device?: string; typeName?: boolean };
  let items: Item[] = [];
  try {
    const found = await vscode.window.withProgress(
      { location: vscode.ProgressLocation.Notification, title: "mpftp: looking for Bluetooth boards (5 s)…" },
      () => bridge.scanBle(5)
    );
    items = found.map((b) => ({
      label: `$(broadcast) ${b.name || b.address}`,
      description: [b.rssi != null ? `${b.rssi} dBm` : "", b.files ? "REPL + files" : "REPL"]
        .filter(Boolean)
        .join(" · "),
      detail: b.address,
      device: b.device,
    }));
  } catch (e: any) {
    void vscode.window.showWarningMessage(`mpftp: Bluetooth scan failed: ${e?.message || e}`);
  }
  items.push({
    label: "$(edit) Type the board's name…",
    description: "the name it advertises",
    detail: items.length
      ? undefined
      : "Nothing advertising bledev's REPL answered. The board runs bledev.repl or bledev.filetransfer from main.py, and takes one client at a time.",
    typeName: true,
  });
  const pick = await vscode.window.showQuickPick(items, {
    title: "Connect over Bluetooth (bledev)",
    placeHolder: "Board",
  });
  if (!pick) {
    return undefined;
  }
  if (!pick.typeName) {
    return pick.device;
  }
  const name = await vscode.window.showInputBox({
    title: "Connect over Bluetooth",
    prompt: "The name the board advertises, as given to bledev's start(name=...)",
    ignoreFocusOut: true,
  });
  const trimmed = name?.trim().replace(/^ble:\/\//i, "");
  return trimmed ? `ble://${trimmed}` : undefined;
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
    (action === "enable"
      ? "\nThe password shows as ***** here; the board gets the real one.\n" + passwords.whereSaved()
      : "");
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
