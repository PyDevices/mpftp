import * as crypto from "crypto";
import * as fs from "fs";
import * as os from "os";
import * as path from "path";
import * as vscode from "vscode";
import { DirEntry, SidecarBridge } from "../bridge/SidecarBridge";
import { openBoardFileInEditor } from "../editRemote";
import { getConfig } from "../platform";

function joinRemote(base: string, name: string): string {
  if (!base || base === "/") {
    return "/" + name.replace(/^\/+/, "");
  }
  return base.replace(/\/+$/, "") + "/" + name;
}

/** Short label for tab titles: COM4, ttyACM0 (not the full /dev path). */
function shortDeviceName(device: string): string {
  const s = device.trim();
  if (!s) {
    return s;
  }
  // Windows COM ports stay as-is; Unix device paths → basename.
  if (s.includes("/") || s.includes("\\")) {
    return path.basename(s);
  }
  return s;
}

function isWebviewDisposedError(e: unknown): boolean {
  return /disposed/i.test(String((e as Error)?.message || e));
}

/** Skip VCS/env/bytecode noise: .git, .venv, __pycache__, *.pyc, etc. */
function shouldSkipTransferEntry(name: string): boolean {
  if (!name || name === "." || name === "..") {
    return true;
  }
  if (name.startsWith(".")) {
    return true;
  }
  if (name === "__pycache__") {
    return true;
  }
  const lower = name.toLowerCase();
  return lower.endsWith(".pyc") || lower.endsWith(".pyo");
}

type TransferPhase = "active" | "stalled" | "done" | "idle";

/**
 * Tracks per-file transfer progress and periodically reports whether work is
 * still advancing or appears hung (no completion for stallMs).
 */
class TransferMonitor {
  private done = 0;
  private current = "";
  private startedAt = Date.now();
  private lastActivityAt = Date.now();
  private timer: ReturnType<typeof setInterval> | undefined;
  private disposed = false;

  constructor(
    private readonly verb: "Uploading" | "Downloading",
    private readonly total: number,
    private readonly onUpdate: (text: string, phase: TransferPhase) => void,
    private readonly progress: vscode.Progress<{ message?: string; increment?: number }>,
    private readonly stallMs = 15000,
    private readonly tickMs = 2000
  ) {}

  start(): void {
    this.startedAt = Date.now();
    this.lastActivityAt = Date.now();
    this.timer = setInterval(() => this.emit(), this.tickMs);
    this.emit();
  }

  beginFile(label: string): void {
    this.current = label;
    this.lastActivityAt = Date.now();
    this.emit();
  }

  finishFile(): void {
    this.done += 1;
    this.lastActivityAt = Date.now();
    const increment = this.total > 0 ? 100 / this.total : 0;
    this.progress.report({
      increment,
      message: this.current ? `${this.done}/${this.total} ${this.current}` : `${this.done}/${this.total}`,
    });
    this.emit();
  }

  finish(summary: string): void {
    this.dispose();
    this.onUpdate(summary, "done");
  }

  dispose(): void {
    if (this.disposed) {
      return;
    }
    this.disposed = true;
    if (this.timer) {
      clearInterval(this.timer);
      this.timer = undefined;
    }
  }

  private emit(): void {
    if (this.disposed) {
      return;
    }
    const idleMs = Date.now() - this.lastActivityAt;
    const elapsedSec = Math.max(0, Math.round((Date.now() - this.startedAt) / 1000));
    const idleSec = Math.max(0, Math.round(idleMs / 1000));
    const filePart = this.current || "…";
    const counts = this.total > 0 ? `${this.done}/${this.total}` : `${this.done}`;
    if (idleMs >= this.stallMs && this.current) {
      this.onUpdate(
        `${this.verb} ${filePart} (${counts}) — no activity ${idleSec}s (may be hung) · ${elapsedSec}s total`,
        "stalled"
      );
      this.progress.report({
        message: `${filePart} — stalled ${idleSec}s?`,
      });
      return;
    }
    this.onUpdate(
      `${this.verb} ${filePart} (${counts}) — in progress · ${elapsedSec}s`,
      "active"
    );
  }
}

export class FtpViewProvider implements vscode.WebviewViewProvider {
  /** Fresh ids — old mpftp.ftpView / mpftp-panel locations were stuck in the sidebar. */
  public static readonly viewType = "mpftp.fileTransferView";
  public static readonly panelContainerId = "mpftp-file-transfer";

  private panelView?: vscode.WebviewView;
  private editorPanel: vscode.WebviewPanel | undefined;
  private readonly webviews = new Set<vscode.Webview>();
  private localPath: string;
  private remotePath = "/";
  private transferBusy = false;
  private bridgeEventsBound = false;
  private deviceInfo = "";
  private statusSettleTimer: ReturnType<typeof setTimeout> | undefined;

  constructor(
    private readonly extensionUri: vscode.Uri,
    private readonly bridge: SidecarBridge,
    private readonly onOpenRepl: () => void,
    private readonly onConnect: () => Promise<void>,
    private readonly onDisconnect: () => Promise<void>
  ) {
    const folder = vscode.workspace.workspaceFolders?.[0]?.uri.fsPath;
    this.localPath = folder || os.homedir();
  }

  resolveWebviewView(
    webviewView: vscode.WebviewView,
    _context: vscode.WebviewViewResolveContext,
    _token: vscode.CancellationToken
  ): void {
    this.panelView = webviewView;
    // Capture before dispose — reading .webview after dispose throws.
    const webview = webviewView.webview;
    this.attachWebview(webview, "panel");
    this.updateTitles();
    webviewView.onDidDispose(() => {
      this.webviews.delete(webview);
      if (this.panelView === webviewView) {
        this.panelView = undefined;
      }
    });
    this.bindBridgeEvents();
  }

  /** Focus File Transfer in the bottom panel (Terminal / Output area). */
  async openInPanel(): Promise<void> {
    // Relocate into our panel container (no-op if already there / unsupported).
    // moveViews also opens the destination container.
    try {
      await vscode.commands.executeCommand("vscode.moveViews", {
        viewIds: [FtpViewProvider.viewType],
        destinationId: FtpViewProvider.panelContainerId,
      });
    } catch {
      /* ignore */
    }
    try {
      await vscode.commands.executeCommand("workbench.action.focusPanel");
    } catch {
      /* ignore */
    }
    await vscode.commands.executeCommand(`${FtpViewProvider.viewType}.focus`);
    await this.pushState();
  }

  /** @deprecated Alias for openInPanel. */
  async reveal(): Promise<void> {
    await this.openInPanel();
  }

  /**
   * Open (or focus) the FTP UI as an editor tab — movable, splittable,
   * and can be dragged to another editor group or window.
   */
  openInEditor(): void {
    if (this.editorPanel) {
      try {
        this.editorPanel.reveal(vscode.ViewColumn.Beside);
        this.updateTitles();
        void this.pushState();
        return;
      } catch (e: unknown) {
        // Stale panel: dispose handler used to throw on .webview and never clear.
        if (!isWebviewDisposedError(e)) {
          throw e;
        }
        this.forgetEditorPanel();
      }
    }
    const panel = vscode.window.createWebviewPanel(
      "mpftp.ftpEditor",
      this.panelTitle(),
      vscode.ViewColumn.Beside,
      {
        enableScripts: true,
        retainContextWhenHidden: true,
        localResourceRoots: [vscode.Uri.joinPath(this.extensionUri, "media")],
      }
    );
    panel.iconPath = vscode.Uri.joinPath(this.extensionUri, "resources", "mpftp.svg");
    const webview = panel.webview;
    this.editorPanel = panel;
    this.attachWebview(webview, "editor");
    panel.onDidDispose(() => {
      this.webviews.delete(webview);
      if (this.editorPanel === panel) {
        this.editorPanel = undefined;
      }
    });
    this.bindBridgeEvents();
    void this.pushState();
  }

  /** Drop a stale editor panel without touching its disposed .webview getter. */
  private forgetEditorPanel(): void {
    const panel = this.editorPanel;
    this.editorPanel = undefined;
    if (!panel) {
      return;
    }
    // Best-effort prune: may already be gone from the set via dispose handler.
    try {
      this.webviews.delete(panel.webview);
    } catch {
      /* disposed — leave set; postAll will prune on next post */
    }
  }

  /** Editor tab / view title: `mpftp COM4` or `mpftp disconnected`. */
  private panelTitle(): string {
    const device = this.bridge.connectedDevice;
    return device ? `mpftp ${shortDeviceName(device)}` : "mpftp disconnected";
  }

  private updateTitles(): void {
    const title = this.panelTitle();
    if (this.editorPanel) {
      try {
        this.editorPanel.title = title;
      } catch (e: unknown) {
        if (isWebviewDisposedError(e)) {
          this.forgetEditorPanel();
        } else {
          throw e;
        }
      }
    }
    if (this.panelView) {
      try {
        this.panelView.title = title;
      } catch (e: unknown) {
        if (isWebviewDisposedError(e)) {
          this.panelView = undefined;
        } else {
          throw e;
        }
      }
    }
  }

  private attachWebview(webview: vscode.Webview, surface: "panel" | "editor"): void {
    webview.options = {
      enableScripts: true,
      localResourceRoots: [vscode.Uri.joinPath(this.extensionUri, "media")],
    };
    webview.html = this.getHtml(webview, surface);
    this.webviews.add(webview);
    webview.onDidReceiveMessage(async (msg) => {
      try {
        await this.handleMessage(msg);
      } catch (e: any) {
        this.status(String(e?.message || e));
      }
    });
  }

  private bindBridgeEvents(): void {
    if (this.bridgeEventsBound) {
      return;
    }
    this.bridgeEventsBound = true;
    this.bridge.on("connected", () => {
      this.updateTitles();
      void this.refreshDeviceInfo().then(() => {
        void this.pushState();
        this.showIdleStatus();
      });
    });
    this.bridge.on("disconnected", () => {
      this.deviceInfo = "";
      this.updateTitles();
      void this.pushState();
      this.showIdleStatus();
    });
  }

  /** Compact one-line chip/build/mem hint for the header (MicroPython only). */
  private async refreshDeviceInfo(): Promise<void> {
    this.deviceInfo = "";
    const code = [
      "import json as _j",
      "_d = {}",
      "try:\n import sys\n _d['platform']=sys.platform\n _d['impl']=sys.implementation.name\n _d['ver']='.'.join(str(x) for x in sys.implementation.version[:3])\nexcept Exception: pass",
      "try:\n import os as _o\n _d['machine']=_o.uname().machine\nexcept Exception: pass",
      "try:\n import machine as _m\n _d['freq']=_m.freq()\nexcept Exception: pass",
      "try:\n import gc as _g\n _d['memfree']=_g.mem_free()\nexcept Exception: pass",
      "print('MPINFO:'+_j.dumps(_d))",
    ].join("\n");
    try {
      const res = await this.bridge.request<{ output?: string }>("exec", {
        code,
        follow: true,
      });
      const out = String((res as any)?.output ?? "");
      const line = out.split(/\r?\n/).find((l) => l.startsWith("MPINFO:"));
      if (!line) {
        return;
      }
      const d = JSON.parse(line.slice("MPINFO:".length)) as Record<string, unknown>;
      const parts: string[] = [];
      const chip = String(d.machine || d.platform || "");
      if (chip) {
        parts.push(chip);
      }
      const impl = String(d.impl || "");
      const ver = String(d.ver || "");
      if (impl) {
        parts.push(ver ? `${impl} ${ver}` : impl);
      }
      const freq = d.freq;
      if (typeof freq === "number" && freq > 0) {
        parts.push(`${Math.round(freq / 1e6)} MHz`);
      } else if (Array.isArray(freq) && freq.length) {
        parts.push(`${Math.round(Number(freq[0]) / 1e6)} MHz`);
      }
      const mem = Number(d.memfree || 0);
      if (mem > 0) {
        const human = mem < 1048576 ? `${Math.round(mem / 1024)} KB` : `${(mem / 1048576).toFixed(1)} MB`;
        parts.push(`${human} free`);
      }
      this.deviceInfo = parts.join(" · ");
    } catch {
      /* board busy / not MicroPython */
    }
  }

  private postAll(msg: Record<string, unknown>): void {
    // Panel and editor tabs share this set; a disposed view must be pruned or
    // postMessage surfaces "Webview is disposed".
    for (const webview of [...this.webviews]) {
      try {
        const result = webview.postMessage(msg);
        void Promise.resolve(result).then(
          () => undefined,
          (e: unknown) => {
            if (isWebviewDisposedError(e)) {
              this.webviews.delete(webview);
            }
          }
        );
      } catch (e: unknown) {
        if (isWebviewDisposedError(e)) {
          this.webviews.delete(webview);
          continue;
        }
        throw e;
      }
    }
  }

  private status(text: string, phase: TransferPhase = "idle"): void {
    if (this.statusSettleTimer) {
      clearTimeout(this.statusSettleTimer);
      this.statusSettleTimer = undefined;
    }
    this.postAll({ type: "status", text, phase });
    // Keep active/stalled visible; ephemeral notes settle back to connection idle.
    if (phase === "active" || phase === "stalled") {
      return;
    }
    const delay = phase === "done" ? 2500 : 2000;
    this.statusSettleTimer = setTimeout(() => this.showIdleStatus(), delay);
  }

  /** Idle footer: connection state (not a generic "Ready"). */
  private showIdleStatus(): void {
    if (this.statusSettleTimer) {
      clearTimeout(this.statusSettleTimer);
      this.statusSettleTimer = undefined;
    }
    if (this.transferBusy) {
      return;
    }
    const device = this.bridge.connectedDevice || "";
    const text = this.bridge.connected
      ? `Connected · ${shortDeviceName(device)}`
      : "Disconnected";
    this.postAll({ type: "status", text, phase: "idle" });
  }

  private async handleMessage(msg: any): Promise<void> {
    switch (msg.type) {
      case "ready":
        await this.pushState();
        this.showIdleStatus();
        break;
      case "connect":
        await this.onConnect();
        await this.pushState();
        break;
      case "disconnect":
        await this.onDisconnect();
        await this.pushState();
        break;
      case "openRepl":
        this.onOpenRepl();
        break;
      case "command": {
        const cmd = String(msg.command || "");
        if (cmd.startsWith("mpftp.")) {
          await vscode.commands.executeCommand(cmd);
          await this.pushState();
        }
        break;
      }
      case "refreshLocal":
        await this.pushState();
        break;
      case "refreshRemote":
        await this.pushState();
        break;
      case "pickLocal": {
        const uris = await vscode.window.showOpenDialog({
          canSelectFiles: false,
          canSelectFolders: true,
          canSelectMany: false,
          openLabel: "Select local folder",
          defaultUri: vscode.Uri.file(this.localPath),
        });
        if (uris?.[0]) {
          this.localPath = uris[0].fsPath;
          await this.pushState();
        }
        break;
      }
      case "localCd":
        if (msg.path && fs.existsSync(msg.path) && fs.statSync(msg.path).isDirectory()) {
          this.localPath = msg.path;
          await this.pushState();
        } else {
          this.status(`Not a directory: ${msg.path}`);
        }
        break;
      case "localUp": {
        const parent = path.dirname(this.localPath);
        if (parent && parent !== this.localPath) {
          this.localPath = parent;
          await this.pushState();
        }
        break;
      }
      case "remoteCd":
        this.remotePath = msg.path || "/";
        await this.pushState();
        break;
      case "upload":
        await this.uploadMany(msg.localPaths || []);
        await this.pushState();
        break;
      case "download":
        await this.downloadMany(msg.remotePaths || []);
        await this.pushState();
        break;
      case "openRemote": {
        const remote = String(msg.path || "");
        if (!remote) {
          return;
        }
        await openBoardFileInEditor(this.bridge, remote);
        this.status(`Editing ${remote}`);
        break;
      }
      case "runRemote": {
        const remote = String(msg.path || "");
        if (!remote) {
          return;
        }
        if (!/\.py$/i.test(remote)) {
          this.status("Run is only for .py files");
          return;
        }
        await this.runRemotePath(remote);
        break;
      }
      case "uploadAndRun": {
        const local = String(msg.localPath || "");
        if (!local) {
          return;
        }
        if (!/\.py$/i.test(local)) {
          this.status("Upload & Run is only for .py files");
          return;
        }
        if (!this.bridge.connected) {
          this.status("Not connected", "stalled");
          return;
        }
        const remote = joinRemote(this.remotePath, path.basename(local));
        this.status(`Uploading ${path.basename(local)}…`, "active");
        try {
          await this.uploadMany([local]);
          await this.pushState();
        } catch (e: any) {
          const err = String(e?.message || e);
          this.status(`Upload failed: ${err}`, "stalled");
          void vscode.window.showErrorMessage(`mpftp upload: ${err}`);
          return;
        }
        await this.runRemotePath(remote);
        break;
      }
      case "hashRemote": {
        const remote = String(msg.path || "");
        if (!remote) {
          return;
        }
        const res = await this.bridge.request<{ hash: string; algo: string }>("fs_hash", {
          path: remote,
          algo: "sha256",
        });
        this.status(`${remote}: ${res.algo} ${res.hash}`);
        void vscode.window.showInformationMessage(`${remote}\n${res.hash}`);
        break;
      }
      case "mkdir": {
        const name = await vscode.window.showInputBox({
          prompt: "New remote directory name",
          placeHolder: "lib",
        });
        if (!name) {
          return;
        }
        const dest = joinRemote(this.remotePath, name);
        await this.bridge.request("fs_mkdir", { path: dest });
        this.status(`mkdir ${dest}`);
        await this.pushState();
        break;
      }
      case "localMkdir": {
        const name = await vscode.window.showInputBox({
          prompt: "New local directory name",
          placeHolder: "lib",
        });
        if (!name) {
          return;
        }
        if (name.includes("/") || name.includes("\\")) {
          this.status("Name cannot contain path separators");
          return;
        }
        const dest = path.join(this.localPath, name);
        fs.mkdirSync(dest, { recursive: false });
        this.status(`mkdir ${dest}`);
        await this.pushState();
        break;
      }
      case "newFile": {
        const name = await vscode.window.showInputBox({
          prompt: "New remote file name",
          placeHolder: "main.py",
        });
        if (!name) {
          return;
        }
        const dest = joinRemote(this.remotePath, name);
        await this.bridge.request("fs_touch", { path: dest });
        this.status(`touch ${dest}`);
        await this.pushState();
        break;
      }
      case "localNewFile": {
        const name = await vscode.window.showInputBox({
          prompt: "New local file name",
          placeHolder: "main.py",
        });
        if (!name) {
          return;
        }
        if (name.includes("/") || name.includes("\\")) {
          this.status("Name cannot contain path separators");
          return;
        }
        const dest = path.join(this.localPath, name);
        if (fs.existsSync(dest)) {
          this.status(`Already exists: ${name}`);
          return;
        }
        fs.writeFileSync(dest, "", { flag: "wx" });
        this.status(`Created ${dest}`);
        await this.pushState();
        break;
      }
      case "openLocal": {
        const local = String(msg.path || "");
        if (!local) {
          return;
        }
        if (!fs.existsSync(local) || fs.statSync(local).isDirectory()) {
          this.status(`Not a file: ${local}`);
          return;
        }
        const doc = await vscode.workspace.openTextDocument(vscode.Uri.file(local));
        await vscode.window.showTextDocument(doc, { preview: false });
        this.status(`Editing ${local}`);
        break;
      }
      case "rm":
        await this.rmMany(msg.remotePaths || []);
        await this.pushState();
        break;
      case "localRm":
        await this.localRmMany(msg.localPaths || []);
        await this.pushState();
        break;
      case "localRename": {
        const src = String(msg.path || "");
        if (!src || !fs.existsSync(src)) {
          this.status(`Not found: ${src}`);
          return;
        }
        const oldName = path.basename(src);
        const newName = await vscode.window.showInputBox({
          prompt: "Rename local item",
          value: oldName,
          valueSelection: [0, oldName.lastIndexOf(".") > 0 ? oldName.lastIndexOf(".") : oldName.length],
        });
        if (!newName || newName === oldName) {
          return;
        }
        if (newName.includes("/") || newName.includes("\\")) {
          this.status("Name cannot contain path separators");
          return;
        }
        const dest = path.join(path.dirname(src), newName);
        fs.renameSync(src, dest);
        this.status(`Renamed ${oldName} → ${newName}`);
        await this.pushState();
        break;
      }
      case "remoteRename": {
        const src = String(msg.path || "");
        if (!src) {
          return;
        }
        const oldName = src.split("/").filter(Boolean).pop() || "";
        const newName = await vscode.window.showInputBox({
          prompt: "Rename board item",
          value: oldName,
          valueSelection: [0, oldName.lastIndexOf(".") > 0 ? oldName.lastIndexOf(".") : oldName.length],
        });
        if (!newName || newName === oldName) {
          return;
        }
        if (newName.includes("/")) {
          this.status("Name cannot contain '/'");
          return;
        }
        const parent = src.includes("/") ? src.slice(0, src.lastIndexOf("/")) || "/" : "/";
        const dest = joinRemote(parent === "" ? "/" : parent, newName);
        await this.bridge.request("fs_rename", { src, dest });
        this.status(`Renamed ${oldName} → ${newName}`);
        await this.pushState();
        break;
      }
      default:
        break;
    }
  }

  private listLocal(dir: string): DirEntry[] {
    try {
      return fs
        .readdirSync(dir, { withFileTypes: true })
        .filter((d) => !shouldSkipTransferEntry(d.name))
        .map((d) => {
          let size = 0;
          try {
            if (d.isFile()) {
              size = fs.statSync(path.join(dir, d.name)).size;
            }
          } catch {
            /* ignore */
          }
          return { name: d.name, isDir: d.isDirectory(), size };
        })
        .sort((a, b) => Number(b.isDir) - Number(a.isDir) || a.name.localeCompare(b.name));
    } catch (e: any) {
      this.status(`local list failed: ${e.message}`);
      return [];
    }
  }

  private async listRemote(dir: string): Promise<DirEntry[]> {
    if (!this.bridge.connected) {
      return [];
    }
    const p = !dir || dir === "/" ? "/" : dir;
    // sidecar treats "" as cwd/root listing for "/"
    const entries = await this.bridge.request<DirEntry[]>("fs_listdir", {
      path: p === "/" ? "/" : p,
    });
    return entries.filter((e) => !shouldSkipTransferEntry(e.name));
  }

  private async pushState(): Promise<void> {
    if (!this.webviews.size) {
      return;
    }
    let remoteEntries: DirEntry[] = [];
    if (this.bridge.connected) {
      try {
        remoteEntries = await this.listRemote(this.remotePath);
      } catch (e: any) {
        this.status(`remote list failed: ${e.message}`, "stalled");
      }
    }
    const connected = this.bridge.connected;
    this.postAll({
      type: "state",
      connected,
      device: this.bridge.connectedDevice || "",
      deviceInfo: connected ? this.deviceInfo : "",
      interpreter: connected ? this.bridge.interpreter || "" : "",
      localPath: this.localPath,
      // Hide board path while disconnected; keep this.remotePath for reconnect.
      remotePath: connected ? this.remotePath : "",
      localEntries: this.listLocal(this.localPath),
      remoteEntries: connected ? remoteEntries : [],
    });
  }

  private countLocalFiles(local: string): { files: number; skipped: number } {
    const base = path.basename(local);
    if (shouldSkipTransferEntry(base)) {
      return { files: 0, skipped: 1 };
    }
    let files = 0;
    let skipped = 0;
    const st = fs.statSync(local);
    if (st.isDirectory()) {
      for (const name of fs.readdirSync(local)) {
        if (shouldSkipTransferEntry(name)) {
          skipped += 1;
          continue;
        }
        const sub = this.countLocalFiles(path.join(local, name));
        files += sub.files;
        skipped += sub.skipped;
      }
      return { files, skipped };
    }
    return { files: 1, skipped: 0 };
  }

  private async countRemoteFiles(remote: string): Promise<{ files: number; skipped: number }> {
    const name = remote.split("/").filter(Boolean).pop() || "";
    if (name && shouldSkipTransferEntry(name)) {
      return { files: 0, skipped: 1 };
    }
    const st = await this.bridge.request<{ isDir: boolean; size: number }>("fs_stat", {
      path: remote,
    });
    if (!st.isDir) {
      return { files: 1, skipped: 0 };
    }
    let files = 0;
    let skipped = 0;
    const entries = await this.bridge.request<DirEntry[]>("fs_listdir", { path: remote });
    for (const e of entries) {
      if (shouldSkipTransferEntry(e.name)) {
        skipped += 1;
        continue;
      }
      const sub = await this.countRemoteFiles(joinRemote(remote, e.name));
      files += sub.files;
      skipped += sub.skipped;
    }
    return { files, skipped };
  }

  private async uploadMany(localPaths: string[]): Promise<void> {
    if (!this.bridge.connected) {
      throw new Error("not connected");
    }
    if (this.transferBusy) {
      this.status("A transfer is already in progress", "stalled");
      return;
    }
    let totalFiles = 0;
    let skippedDots = 0;
    for (const local of localPaths) {
      const c = this.countLocalFiles(local);
      totalFiles += c.files;
      skippedDots += c.skipped;
    }
    if (totalFiles === 0) {
      this.status(
        skippedDots
          ? `Nothing to upload (skipped ${skippedDots} ignored entr${skippedDots === 1 ? "y" : "ies"}: .git, __pycache__, …)`
          : "Nothing to upload"
      );
      return;
    }

    this.transferBusy = true;
    try {
      await vscode.window.withProgress(
        {
          location: vscode.ProgressLocation.Notification,
          title: "mpftp upload",
          cancellable: false,
        },
        async (progress) => {
          const monitor = new TransferMonitor(
            "Uploading",
            totalFiles,
            (text, phase) => this.status(text, phase),
            progress
          );
          monitor.start();
          try {
            for (const local of localPaths) {
              await this.uploadPath(local, this.remotePath, monitor);
            }
            const skipNote = skippedDots
              ? `, skipped ${skippedDots} ignored entr${skippedDots === 1 ? "y" : "ies"}`
              : "";
            monitor.finish(`Uploaded ${totalFiles} file(s)${skipNote}`);
          } catch (e) {
            monitor.dispose();
            throw e;
          }
        }
      );
    } finally {
      this.transferBusy = false;
    }
  }

  /** Interrupt + raw soft-reset, then exec a .py already on the board; open REPL. */
  private async runRemotePath(remote: string): Promise<void> {
    this.status(`Running ${remote}…`, "active");
    try {
      // Soft-reset + exec board file; do not follow (UI apps loop forever).
      // Output appears on the REPL UART — open it so the user can see prints/tracebacks.
      await this.bridge.request("run_path", { path: remote, follow: false });
      this.status(`Running ${remote} — see REPL`, "done");
      this.onOpenRepl();
    } catch (e: any) {
      const err = String(e?.message || e);
      this.status(`Run failed: ${err}`, "stalled");
      void vscode.window.showErrorMessage(`mpftp run ${remote}: ${err}`);
    }
  }

  private async uploadPath(local: string, remoteDir: string, monitor: TransferMonitor): Promise<void> {
    const base = path.basename(local);
    if (shouldSkipTransferEntry(base)) {
      return;
    }
    const st = fs.statSync(local);
    if (st.isDirectory()) {
      const destDir = joinRemote(remoteDir, base);
      try {
        await this.bridge.request("fs_mkdir", { path: destDir });
      } catch {
        /* may exist */
      }
      for (const name of fs.readdirSync(local)) {
        if (shouldSkipTransferEntry(name)) {
          continue;
        }
        await this.uploadPath(path.join(local, name), destDir, monitor);
      }
      return;
    }
    const data = fs.readFileSync(local);
    const dest = joinRemote(remoteDir, base);
    monitor.beginFile(dest);
    await this.bridge.request("fs_write", {
      path: dest,
      data_b64: data.toString("base64"),
    });
    if (getConfig().verifyTransfers) {
      await this.verifyRemoteHash(dest, data);
    }
    monitor.finishFile();
  }

  private async verifyRemoteHash(remote: string, data: Buffer): Promise<void> {
    const expect = crypto.createHash("sha256").update(data).digest("hex");
    const res = await this.bridge.request<{ hash: string }>("fs_hash", {
      path: remote,
      algo: "sha256",
    });
    if (res.hash !== expect) {
      throw new Error(`hash mismatch for ${remote}: expected ${expect}, got ${res.hash}`);
    }
  }

  private async downloadMany(remotePaths: string[]): Promise<void> {
    if (!this.bridge.connected) {
      throw new Error("not connected");
    }
    if (this.transferBusy) {
      this.status("A transfer is already in progress", "stalled");
      return;
    }
    let totalFiles = 0;
    let skippedDots = 0;
    for (const remote of remotePaths) {
      const c = await this.countRemoteFiles(remote);
      totalFiles += c.files;
      skippedDots += c.skipped;
    }
    if (totalFiles === 0) {
      this.status(
        skippedDots
          ? `Nothing to download (skipped ${skippedDots} ignored entr${skippedDots === 1 ? "y" : "ies"})`
          : "Nothing to download"
      );
      return;
    }

    this.transferBusy = true;
    try {
      await vscode.window.withProgress(
        {
          location: vscode.ProgressLocation.Notification,
          title: "mpftp download",
          cancellable: false,
        },
        async (progress) => {
          const monitor = new TransferMonitor(
            "Downloading",
            totalFiles,
            (text, phase) => this.status(text, phase),
            progress
          );
          monitor.start();
          try {
            for (const remote of remotePaths) {
              await this.downloadPath(remote, this.localPath, monitor);
            }
            const skipNote = skippedDots
              ? `, skipped ${skippedDots} ignored entr${skippedDots === 1 ? "y" : "ies"}`
              : "";
            monitor.finish(`Downloaded ${totalFiles} file(s)${skipNote}`);
          } catch (e) {
            monitor.dispose();
            throw e;
          }
        }
      );
    } finally {
      this.transferBusy = false;
    }
  }

  private async downloadPath(
    remote: string,
    localDir: string,
    monitor: TransferMonitor
  ): Promise<void> {
    const name = remote.split("/").filter(Boolean).pop() || "file";
    if (shouldSkipTransferEntry(name)) {
      return;
    }
    const st = await this.bridge.request<{ isDir: boolean; size: number }>("fs_stat", {
      path: remote,
    });
    if (st.isDir) {
      const destDir = path.join(localDir, name);
      fs.mkdirSync(destDir, { recursive: true });
      const entries = await this.bridge.request<DirEntry[]>("fs_listdir", { path: remote });
      for (const e of entries) {
        if (shouldSkipTransferEntry(e.name)) {
          continue;
        }
        await this.downloadPath(joinRemote(remote, e.name), destDir, monitor);
      }
      return;
    }
    monitor.beginFile(remote);
    const res = await this.bridge.request<{ data_b64: string }>("fs_read", { path: remote });
    const buf = Buffer.from(res.data_b64, "base64");
    fs.writeFileSync(path.join(localDir, name), buf);
    if (getConfig().verifyTransfers) {
      await this.verifyRemoteHash(remote, buf);
    }
    monitor.finishFile();
  }

  private async rmMany(remotePaths: string[]): Promise<void> {
    const ok = await vscode.window.showWarningMessage(
      `Delete ${remotePaths.length} item(s) on the board?`,
      { modal: true },
      "Delete"
    );
    if (ok !== "Delete") {
      return;
    }
    for (const p of remotePaths) {
      this.status(`Removing ${p}…`);
      await this.bridge.request("fs_rm_rf", { path: p });
    }
    this.status("Deleted");
  }

  private async localRmMany(localPaths: string[]): Promise<void> {
    const ok = await vscode.window.showWarningMessage(
      `Delete ${localPaths.length} local item(s)?`,
      { modal: true },
      "Delete"
    );
    if (ok !== "Delete") {
      return;
    }
    for (const p of localPaths) {
      this.status(`Removing ${p}…`);
      fs.rmSync(p, { recursive: true, force: true });
    }
    this.status("Deleted");
  }

  private getHtml(webview: vscode.Webview, surface: "panel" | "editor"): string {
    const css = webview.asWebviewUri(vscode.Uri.joinPath(this.extensionUri, "media", "ftp.css"));
    const js = webview.asWebviewUri(vscode.Uri.joinPath(this.extensionUri, "media", "ftp.js"));
    const codicons = webview.asWebviewUri(
      vscode.Uri.joinPath(this.extensionUri, "media", "codicons", "codicon.css")
    );
    const csp = [
      `default-src 'none'`,
      `style-src ${webview.cspSource}`,
      `font-src ${webview.cspSource}`,
      `script-src ${webview.cspSource}`,
    ].join("; ");

    return `<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta http-equiv="Content-Security-Policy" content="${csp}" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <link rel="stylesheet" href="${codicons}" />
  <link rel="stylesheet" href="${css}" />
  <title>mpftp</title>
</head>
<body data-surface="${surface}">
  <div id="app">
    <div class="toolbar">
      <button id="btnMore" class="secondary icon-btn more-btn" title="mpftp commands" aria-label="mpftp commands">
        <i class="codicon codicon-ellipsis"></i>
      </button>
      <button id="btnConnect" class="secondary icon-btn tool-btn" title="Connect" aria-label="Connect">
        <i id="btnConnectIcon" class="codicon codicon-plug"></i>
      </button>
      <button id="btnInterrupt" class="secondary icon-btn tool-btn" disabled title="Interrupt (Ctrl+C)" aria-label="Interrupt">
        <i class="codicon codicon-debug-stop"></i>
      </button>
      <button id="btnSoftReset" class="secondary icon-btn tool-btn" disabled title="Soft Reset" aria-label="Soft Reset">
        <i class="codicon codicon-debug-rerun"></i>
      </button>
      <button id="btnHardReset" class="secondary icon-btn tool-btn" disabled title="Hard Reset" aria-label="Hard Reset">
        <i class="codicon codicon-debug-restart"></i>
      </button>
      <button id="btnRepl" class="secondary tool-label-btn" disabled title="Open REPL" aria-label="REPL">
        <i class="codicon codicon-terminal"></i><span>REPL</span>
      </button>
      <button id="btnFirmware" class="secondary tool-label-btn" title="Build & flash firmware" aria-label="Firmware">
        <i class="codicon codicon-chip"></i><span>Firmware</span>
      </button>
      <span id="interpreterPill" class="interpreter-pill" hidden aria-live="polite"></span>
    </div>
    <div class="panes">
      <section class="pane" id="localPane">
        <div class="pane-header">
          <span>Local</span>
          <div class="pane-actions">
            <button id="btnLocalMkdir" class="secondary icon-btn" title="New folder" aria-label="New local folder">
              <i class="codicon codicon-new-folder"></i>
            </button>
            <button id="btnLocalNewFile" class="secondary icon-btn" title="New file" aria-label="New local file">
              <i class="codicon codicon-new-file"></i>
            </button>
            <button id="btnLocalRun" class="secondary icon-btn" disabled title="Upload & Run" aria-label="Upload and run local Python file">
              <i class="codicon codicon-play"></i>
            </button>
            <button id="btnLocalOpen" class="secondary icon-btn" disabled title="Open in Editor" aria-label="Open local file in editor">
              <i class="codicon codicon-go-to-file"></i>
            </button>
            <button id="btnLocalRename" class="secondary icon-btn" disabled title="Rename" aria-label="Rename local item">
              <i class="codicon codicon-edit"></i>
            </button>
            <button id="btnLocalDelete" class="secondary icon-btn" disabled title="Delete" aria-label="Delete local selection">
              <i class="codicon codicon-trash"></i>
            </button>
          </div>
        </div>
        <div class="pathbar">
          <button id="btnRefreshLocal" class="secondary icon-btn" title="Refresh"><i class="codicon codicon-refresh"></i></button>
          <input id="localPath" spellcheck="false" />
          <button id="btnLocalBrowse" class="secondary icon-btn" title="Browse…" aria-label="Browse local folder">
            <i class="codicon codicon-folder-opened"></i>
          </button>
        </div>
        <div class="listing" id="localListing"></div>
      </section>
      <div class="xfer">
        <button id="btnXferUp" disabled title="Upload"><i class="codicon codicon-arrow-right"></i></button>
        <button id="btnXferDown" disabled title="Download"><i class="codicon codicon-arrow-left"></i></button>
      </div>
      <section class="pane" id="remotePane">
        <div class="pane-header">
          <span>Board</span>
          <div class="pane-actions">
            <button id="btnRemoteMkdir" class="secondary icon-btn" disabled title="New folder" aria-label="New board folder">
              <i class="codicon codicon-new-folder"></i>
            </button>
            <button id="btnRemoteNewFile" class="secondary icon-btn" disabled title="New file" aria-label="New board file">
              <i class="codicon codicon-new-file"></i>
            </button>
            <button id="btnRemoteRun" class="secondary icon-btn" disabled title="Run board file" aria-label="Run board Python file">
              <i class="codicon codicon-play"></i>
            </button>
            <button id="btnRemoteOpen" class="secondary icon-btn" disabled title="Open in Editor" aria-label="Open board file in editor">
              <i class="codicon codicon-go-to-file"></i>
            </button>
            <button id="btnRemoteRename" class="secondary icon-btn" disabled title="Rename" aria-label="Rename board item">
              <i class="codicon codicon-edit"></i>
            </button>
            <button id="btnRemoteDelete" class="secondary icon-btn" disabled title="Delete" aria-label="Delete board selection">
              <i class="codicon codicon-trash"></i>
            </button>
          </div>
        </div>
        <div class="pathbar">
          <button id="btnRefreshRemote" class="secondary icon-btn" disabled title="Refresh"><i class="codicon codicon-refresh"></i></button>
          <input id="remotePath" spellcheck="false" />
        </div>
        <div class="listing" id="remoteListing"></div>
      </section>
    </div>
    <div class="footer" id="footer">Disconnected</div>
  </div>
  <div id="ctxMenu" class="ctx-menu" hidden></div>
  <script src="${js}"></script>
</body>
</html>`;
  }
}
