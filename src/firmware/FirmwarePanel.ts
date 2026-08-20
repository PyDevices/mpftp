import * as fs from "fs";
import * as os from "os";
import * as path from "path";
import * as vscode from "vscode";
import { ActivityLog } from "../activityLog";
import { SidecarBridge } from "../bridge/SidecarBridge";
import { detectHost, filterAndSortPorts, getConfig } from "../platform";
import { FirmwareEngine, StreamHandle } from "./engine";

interface Selection {
  port: string;
  board: string;
  variant: string;
}

interface Prefs {
  reconnectAfterFlash: boolean;
  alsoFlashAfterBuild: boolean;
  device: string;
  /** Explicit source — never inferred from MicroPython checkout presence. */
  firmwareSource: "build" | "download";
  /** Empty = latest stable release from catalog. */
  downloadVersion: string;
  downloadPreview: boolean;
}

/** Structured "toolchain missing" payload emitted by the build engine. */
interface NeedToolchain {
  id: string;
  label: string;
  kind: "dir" | "command";
  configKey?: string | null;
  bin?: string | null;
  hint?: string;
  url?: string;
}

const STATE_FILE = path.join(os.homedir(), ".mpftp", "firmware.json");

/**
 * The Firmware build/flash webview (editor tab). Guides the user through
 * selecting a target, building once, then flashing one or many boards.
 */
export class FirmwarePanel {
  private panel: vscode.WebviewPanel | undefined;
  private readonly engine: FirmwareEngine;
  private discovery: Record<string, unknown> = {};
  private tree: any[] = [];
  private downloadTree: any[] = [];
  private cmods: Record<string, unknown> = {};
  private flashers: Record<string, string> = {};
  private selection: Selection = { port: "", board: "", variant: "" };
  /** MCU family from download catalog (for flash offset). */
  private downloadFamily = "";
  private downloadVersions: Array<{ version: string; channel: string; url: string }> = [];
  private downloadedArtifact: Record<string, unknown> | undefined;
  private prefs: Prefs = {
    reconnectAfterFlash: false,
    alsoFlashAfterBuild: false,
    device: "",
    firmwareSource: "build",
    downloadVersion: "",
    downloadPreview: false,
  };
  private activeStream: StreamHandle | undefined;
  private busy = false;
  private lastDetect: Record<string, unknown> | undefined;

  constructor(
    private readonly extensionUri: vscode.Uri,
    private readonly extensionPath: string,
    private readonly bridge: SidecarBridge,
    private readonly activity: ActivityLog
  ) {
    this.engine = new FirmwareEngine(extensionPath);
    this.loadPrefs();
  }

  reveal(): void {
    if (this.panel) {
      this.panel.reveal(vscode.ViewColumn.Active);
      return;
    }
    this.panel = vscode.window.createWebviewPanel(
      "mpftp.firmware",
      "mpftp Firmware",
      vscode.ViewColumn.Active,
      {
        enableScripts: true,
        retainContextWhenHidden: true,
        localResourceRoots: [vscode.Uri.joinPath(this.extensionUri, "media")],
      }
    );
    this.panel.iconPath = vscode.Uri.joinPath(this.extensionUri, "resources", "mpftp.svg");
    this.panel.webview.html = this.getHtml(this.panel.webview);
    this.panel.webview.onDidReceiveMessage((m) => void this.onMessage(m));
    this.panel.onDidDispose(() => {
      this.activeStream?.cancel();
      this.activeStream = undefined;
      this.panel = undefined;
    });
  }

  // ---------------------------------------------------------------------- //
  // Preferences (mirror of ~/.mpftp/firmware.json written by the engine)
  // ---------------------------------------------------------------------- //

  private loadPrefs(): void {
    try {
      const s = JSON.parse(fs.readFileSync(STATE_FILE, "utf8"));
      if (s.lastSelection) {
        this.selection = {
          port: s.lastSelection.port || "",
          board: s.lastSelection.board || "",
          variant: s.lastSelection.variant || "",
        };
      }
      this.prefs.device = s.lastDevice || "";
      if (typeof s.reconnectAfterFlash === "boolean") {
        this.prefs.reconnectAfterFlash = s.reconnectAfterFlash;
      }
      if (typeof s.alsoFlashAfterBuild === "boolean") {
        this.prefs.alsoFlashAfterBuild = s.alsoFlashAfterBuild;
      }
      if (s.firmwareSource === "build" || s.firmwareSource === "download") {
        this.prefs.firmwareSource = s.firmwareSource;
      }
      if (typeof s.downloadVersion === "string") {
        this.prefs.downloadVersion = s.downloadVersion;
      }
      if (typeof s.downloadPreview === "boolean") {
        this.prefs.downloadPreview = s.downloadPreview;
      }
    } catch {
      /* first run */
    }
  }

  private savePrefs(): void {
    let s: Record<string, unknown> = {};
    try {
      s = JSON.parse(fs.readFileSync(STATE_FILE, "utf8"));
    } catch {
      /* ignore */
    }
    s.lastSelection = this.selection;
    s.lastDevice = this.prefs.device;
    s.reconnectAfterFlash = this.prefs.reconnectAfterFlash;
    s.alsoFlashAfterBuild = this.prefs.alsoFlashAfterBuild;
    s.firmwareSource = this.prefs.firmwareSource;
    s.downloadVersion = this.prefs.downloadVersion;
    s.downloadPreview = this.prefs.downloadPreview;
    try {
      fs.mkdirSync(path.dirname(STATE_FILE), { recursive: true });
      fs.writeFileSync(STATE_FILE, JSON.stringify(s, null, 2), "utf8");
    } catch {
      /* ignore */
    }
  }

  // ---------------------------------------------------------------------- //
  // Engine path args
  // ---------------------------------------------------------------------- //

  /** VS Code / Cursor workspace folders (editor open roots). */
  private editorWorkspaces(): string[] {
    return (vscode.workspace.workspaceFolders || []).map((f) => f.uri.fsPath);
  }

  /** True if path is a MicroPython tree (ports/ + py/). */
  private isMpTree(dir: string): boolean {
    return (
      !!dir &&
      fs.existsSync(path.join(dir, "ports")) &&
      fs.existsSync(path.join(dir, "py"))
    );
  }

  /** Resolve MicroPython under a firmware workspace folder. */
  private mpUnderWorkspace(root: string): string {
    if (this.isMpTree(root)) {
      return root;
    }
    const nested = path.join(root, "micropython");
    return this.isMpTree(nested) ? nested : "";
  }

  /** Firmware workspace used for modules/stubs (setting, discovery, or MP parent). */
  private firmwareWorkspace(): string {
    const cfg = getConfig();
    if (cfg.workspacePath && fs.existsSync(cfg.workspacePath)) {
      return cfg.workspacePath;
    }
    const discovered =
      (this.discovery.workspace as string) ||
      (this.cmods.workspaceDir as string) ||
      "";
    if (discovered && fs.existsSync(discovered)) {
      return discovered;
    }
    const mp = this.mpDir();
    return mp ? path.dirname(mp) : "";
  }

  private mpDir(): string {
    const cfg = getConfig();
    if (cfg.micropythonPath && this.isMpTree(cfg.micropythonPath)) {
      return cfg.micropythonPath;
    }
    if (cfg.workspacePath) {
      const under = this.mpUnderWorkspace(cfg.workspacePath);
      if (under) {
        return under;
      }
    }
    if (this.discovery.micropython) {
      return this.discovery.micropython as string;
    }
    for (const root of this.editorWorkspaces()) {
      const under = this.mpUnderWorkspace(root);
      if (under) {
        return under;
      }
    }
    return "";
  }

  /** Roots passed to the engine: configured workspace first, then open folders. */
  private workspaceArg(): string {
    const cfg = getConfig();
    const roots: string[] = [];
    if (cfg.workspacePath) {
      roots.push(cfg.workspacePath);
    }
    for (const f of this.editorWorkspaces()) {
      if (!roots.includes(f)) {
        roots.push(f);
      }
    }
    return roots.join(path.delimiter);
  }

  private pathArgs(): Record<string, string> {
    const cfg = getConfig();
    const args: Record<string, string> = {};
    const mp = this.mpDir();
    if (mp) {
      args.mp = mp;
    }
    const ws = this.workspaceArg();
    if (ws) {
      args.workspace = ws;
    }
    if (cfg.idfPath) {
      args.idf = cfg.idfPath;
    }
    if (cfg.emsdkPath) {
      args.emsdk = cfg.emsdkPath;
    }
    if (cfg.toolchainBins.length) {
      args.toolchainBins = cfg.toolchainBins.join(path.delimiter);
    }
    return args;
  }

  // ---------------------------------------------------------------------- //
  // Messages
  // ---------------------------------------------------------------------- //

  private post(msg: Record<string, unknown>): void {
    void this.panel?.webview.postMessage(msg);
  }

  private log(line: string): void {
    this.post({ type: "log", line });
  }

  private phase(phase: string, text = ""): void {
    this.post({ type: "phase", phase, text });
  }

  /** Detect's own status channel — kept separate from the build/flash phase so
   *  its result renders with Device Info, not in the Build step. */
  private detectStatus(state: string, text = ""): void {
    this.post({ type: "detectStatus", state, text });
  }

  /** Flash's own status channel — renders under the Flash step, not Build. */
  private flashStatus(state: string, text = ""): void {
    this.post({ type: "flashStatus", state, text });
  }

  private async onMessage(msg: any): Promise<void> {
    switch (msg?.type) {
      case "ready":
        await this.refreshAll();
        break;
      case "refresh":
        await this.refreshAll();
        break;
      case "changePath":
        await this.changePath();
        break;
      case "chooseWorkspace":
        await this.chooseWorkspace();
        break;
      case "createWorkspaceStubs":
        await this.createWorkspaceStubs();
        break;
      case "select":
        this.selection = {
          port: msg.port || "",
          board: msg.board || "",
          variant: msg.variant || "",
        };
        this.downloadedArtifact = undefined;
        this.savePrefs();
        if (this.prefs.firmwareSource === "download") {
          await this.refreshDownloadList();
          this.pushState();
        }
        await this.refreshArtifact();
        break;
      case "build":
        await this.doBuild(!!msg.clean);
        break;
      case "clean":
        await this.doClean();
        break;
      case "flash":
        await this.doFlash(
          typeof msg.offset === "string" ? msg.offset : "",
          typeof msg.baud === "string" ? msg.baud : "",
          typeof msg.before === "string" ? msg.before : "",
          typeof msg.after === "string" ? msg.after : "",
          msg.erase === true
        );
        break;
      case "detect":
        await this.detectDevice();
        break;
      case "cancel":
        this.activeStream?.cancel();
        this.log("[mpftp] cancel requested");
        break;
      case "refreshDevices":
        await this.pushDevices();
        break;
      case "changeDevice":
        await this.changeDevice();
        break;
      case "setPref":
        if (msg.key === "reconnectAfterFlash") {
          this.prefs.reconnectAfterFlash = !!msg.value;
        } else if (msg.key === "alsoFlashAfterBuild") {
          this.prefs.alsoFlashAfterBuild = !!msg.value;
        } else if (msg.key === "downloadVersion") {
          this.prefs.downloadVersion = String(msg.value || "");
          this.prefs.downloadPreview = false;
        } else if (msg.key === "downloadPreview") {
          this.prefs.downloadPreview = !!msg.value;
          if (this.prefs.downloadPreview) {
            this.prefs.downloadVersion = "";
          }
        }
        this.savePrefs();
        break;
      case "setSource":
        await this.setSource(msg.source === "download" ? "download" : "build");
        break;
      case "download":
        await this.doDownload();
        break;
      case "browseArtifact":
        await this.browseArtifact();
        break;
      default:
        break;
    }
  }

  private async setSource(source: "build" | "download"): Promise<void> {
    this.prefs.firmwareSource = source;
    this.downloadedArtifact = undefined;
    this.savePrefs();
    await this.refreshAll();
  }

  // ---------------------------------------------------------------------- //
  // Discovery / state push
  // ---------------------------------------------------------------------- //

  private async refreshAll(): Promise<void> {
    try {
      this.discovery = await this.engine.run("discover", this.pathArgs());
    } catch (e: any) {
      this.log(`[mpftp] discover failed: ${e?.message || e}`);
    }

    // Persist a firmware workspace when discover found MicroPython under an
    // open folder / setting and the user has not pinned one yet.
    const cfg = getConfig();
    if (!cfg.workspacePath && this.discovery.workspace) {
      const ws = String(this.discovery.workspace);
      if (this.mpUnderWorkspace(ws) || this.isMpTree(ws)) {
        await vscode.workspace
          .getConfiguration("mpftp")
          .update("workspacePath", ws, vscode.ConfigurationTarget.Global);
      } else {
        const mp = this.discovery.micropython as string | undefined;
        if (mp && this.isMpTree(mp)) {
          await vscode.workspace
            .getConfiguration("mpftp")
            .update(
              "workspacePath",
              path.dirname(mp),
              vscode.ConfigurationTarget.Global
            );
        }
      }
    }

    try {
      const f = await this.engine.run<{ flashers: Record<string, string> }>("flashers");
      this.flashers = f.flashers || {};
    } catch {
      /* ignore */
    }

    if (this.prefs.firmwareSource === "download") {
      try {
        const t = await this.engine.run<{ ports: any[] }>("download-tree");
        this.downloadTree = t.ports || [];
        this.tree = this.downloadTree;
      } catch (e: any) {
        this.log(`[mpftp] download-tree failed: ${e?.message || e}`);
        this.downloadTree = [];
        this.tree = [];
      }
      this.cmods = {};
      await this.refreshDownloadList();
    } else {
      if (!this.mpDir()) {
        this.tree = [];
        this.pushState();
        await this.pushDevices();
        return;
      }
      try {
        const t = await this.engine.run<{ ports: any[] }>("tree", this.pathArgs());
        this.tree = t.ports || [];
      } catch (e: any) {
        this.log(`[mpftp] tree failed: ${e?.message || e}`);
      }
      try {
        this.cmods = await this.engine.run("cmods", this.pathArgs());
      } catch {
        /* ignore */
      }
      await this.autoSelectFromDevice();
    }

    this.pushState();
    await this.refreshArtifact();
    await this.pushDevices();
  }

  private async refreshDownloadList(): Promise<void> {
    this.downloadVersions = [];
    this.downloadFamily = "";
    if (!this.selection.board) {
      return;
    }
    // Resolve family from tree entry.
    for (const p of this.downloadTree) {
      const b = (p.boards || []).find((x: any) => x.board === this.selection.board);
      if (b) {
        this.downloadFamily = b.family || "";
        this.selection.port = p.port || this.selection.port;
        break;
      }
    }
    try {
      const info = await this.engine.run<{
        downloads?: Array<{ version: string; channel: string; url: string }>;
        variants?: string[];
        family?: string;
        port?: string;
        flashOffset?: string;
      }>("download-list", {
        board: this.selection.board,
        variant: this.selection.variant || undefined,
        preview: this.prefs.downloadPreview ? true : undefined,
      });
      this.downloadVersions = info.downloads || [];
      if (info.family) {
        this.downloadFamily = info.family;
      }
      if (info.port) {
        this.selection.port = info.port;
      }
      // Attach scraped MP variants (C6_WIFI, …) so Target can expand them.
      if (Array.isArray(info.variants)) {
        for (const p of this.downloadTree) {
          const b = (p.boards || []).find(
            (x: any) => x.board === this.selection.board
          );
          if (b) {
            b.variants = info.variants;
            break;
          }
        }
        // Detect ran before scrape; re-apply P4 Wi-Fi variant from MP hints.
        this.reapplyWifiVariantFromHints(info.variants);
      }
      // Prefill Flash offset from upstream board.json (e.g. P4 → 0x2000).
      if (typeof info.flashOffset === "string" && info.flashOffset) {
        this.post({
          type: "flashOffsetDefault",
          flashOffset: info.flashOffset,
        });
      }
    } catch (e: any) {
      this.log(`[mpftp] download-list failed: ${e?.message || e}`);
    }
  }

  // ---------------------------------------------------------------------- //
  // Detect (esptool-first chip / flash / security probe)
  // ---------------------------------------------------------------------- //

  /**
   * One-click device probe. esptool-first (works on a bare board), with
   * optional MicroPython enrichment only if a session was already active.
   */
  private async detectDevice(): Promise<void> {
    if (this.busy) {
      return;
    }
    // The connected board is the source of truth; only fall back to the saved
    // device (e.g. a bare board that was never connected).
    const connected = this.bridge.connectedDevice || "";
    const device = connected || this.prefs.device || "";
    if (!device) {
      void vscode.window.showWarningMessage(
        "Select a device (serial port) to detect."
      );
      return;
    }
    if (device !== this.prefs.device) {
      this.prefs.device = device;
      this.savePrefs();
    }

    this.busy = true;
    this.detectStatus("detecting", "Detecting…");
    this.post({ type: "clearLog" });
    this.log(`[mpftp] detecting ${device}…`);

    // Enrichment only: gather MicroPython hints while still connected, then
    // release the port so esptool can open it.
    let mpHints: Record<string, unknown> = {};
    const wasConnected = !!connected;
    if (wasConnected) {
      mpHints = await this.gatherMpHints();
      try {
        await this.bridge.disconnect();
        this.log(`[mpftp] released ${device} for esptool`);
      } catch {
        /* ignore */
      }
      // Let the OS release the serial handle before esptool grabs it.
      await new Promise((r) => setTimeout(r, 800));
    }

    let res: Record<string, unknown> | undefined;
    try {
      res = await this.engine.run("detect", {
        ...this.pathArgs(),
        device,
        esptool: this.engine.esptoolCommand() || undefined,
        mpHints: Object.keys(mpHints).length ? JSON.stringify(mpHints) : undefined,
      });
    } catch (e: any) {
      this.log(`[mpftp] detect failed: ${e?.message || e}`);
    }

    this.busy = false;

    if (res && res.ok !== false) {
      this.lastDetect = res;
      this.applyDetect(res);
      const chip = String((res as any).chip || "");
      const board = String(((res as any).match || {}).board || "");
      this.detectStatus(
        "ok",
        res.espressif === false
          ? "Detected — not an Espressif chip"
          : `Detected ${chip}${board ? " · " + board : ""}`
      );
      this.activity.event("firmware_detect", {
        message: chip || "detect",
        data: { device, board },
      });
    } else {
      this.detectStatus("failed", "Detect failed");
    }

    // esptool hard-resets the chip; give USB-UART boards time to finish boot
    // before Connect's raw-REPL handshake (P4 + CH343 needs ~1.5–2s).
    await new Promise((r) => setTimeout(r, 1800));

    if (wasConnected) {
      try {
        await this.bridge.connect(device, undefined, { silent: true });
        this.log(`[mpftp] reconnected ${device}`);
        if (res && res.ok !== false && !Object.keys(mpHints).length) {
          const fresh = await this.gatherMpHints();
          if (Object.keys(fresh).length && this.lastDetect) {
            (this.lastDetect as any).mp = fresh;
          }
        }
      } catch (e: any) {
        this.log(`[mpftp] reconnect failed (no MicroPython?): ${e?.message || e}`);
      }
    }

    this.post({ type: "detect", detect: this.lastDetect || null });
    if (this.prefs.firmwareSource === "download") {
      await this.refreshDownloadList();
    }
    this.pushState();
    await this.refreshArtifact();
    await this.pushDevices();
  }

  /**
   * Choose the port esptool should flash and put the board where it can talk.
   *
   * Native-USB ESP32 parts (S2/S3/C3/C6/H2/P4) running MicroPython expose the
   * firmware's *own* USB CDC (Espressif VID 0x303A, app PID e.g. 0x4001).
   * esptool cannot reset that CDC into the ROM loader — toggling reset just
   * keeps the app running, yielding "Invalid head of packet". We reset into the
   * ROM USB-Serial/JTAG downloader (PID 0x1001), which enumerates as a *new*
   * port, and flash that instead. Boards on a UART bridge (CP210x/CH340/FTDI)
   * keep the same port; esptool's DTR/RTS auto-reset works there.
   */
  private async prepareEspFlashPort(device: string): Promise<string | undefined> {
    const ESP_VID = 0x303a;
    const ports = await this.bridge.listPorts().catch(() => []);
    const cur = ports.find((p) => p.device === device);
    const nativeUsb = !!cur && cur.vid === ESP_VID;
    if (!nativeUsb) {
      await this.bridge.disconnect().catch(() => undefined);
      this.log(`[mpftp] disconnected ${device} for flashing`);
      return device;
    }

    this.log("[mpftp] native-USB board — entering ROM download mode…");
    const before = new Set(ports.map((p) => p.device));
    try {
      await this.bridge.request("bootloader", {});
    } catch {
      /* expected: the app CDC drops as the chip resets */
    }
    await this.bridge.disconnect().catch(() => undefined);

    const rom = await this.waitForEspDownloadPort(before, device);
    if (rom) {
      this.log(`[mpftp] ROM download port: ${rom}`);
      return rom;
    }
    // Software reset alone did not surface a usable download port (common on
    // boards whose ROM USB-Serial/JTAG is a separate peripheral/connector).
    // Abort rather than flashing the now-vanished app-CDC port.
    return undefined;
  }

  /** Poll for the ESP32 ROM USB-Serial/JTAG port after a reset to bootloader. */
  private async waitForEspDownloadPort(
    before: Set<string>,
    original: string,
    timeoutMs = 15000
  ): Promise<string | undefined> {
    const ESP_VID = 0x303a;
    const ROM_PID = 0x1001; // USB-Serial/JTAG (ROM download)
    const deadline = Date.now() + timeoutMs;
    while (Date.now() < deadline) {
      await new Promise((r) => setTimeout(r, 700));
      const ports = await this.bridge.listPorts().catch(() => []);
      const rom = ports.find((p) => p.vid === ESP_VID && p.pid === ROM_PID);
      if (rom) {
        return rom.device;
      }
      const fresh = ports.find(
        (p) => p.vid === ESP_VID && p.device !== original && !before.has(p.device)
      );
      if (fresh) {
        return fresh.device;
      }
    }
    return undefined;
  }

  /** Best-effort MicroPython/CircuitPython interpreter hints (enrichment only). */
  private async gatherMpHints(): Promise<Record<string, unknown>> {
    const code = [
      "import json as _j",
      "_d = {}",
      "try:\n import sys\n _d['platform']=sys.platform\n _d['impl']=sys.implementation.name\n _d['build']=getattr(sys.implementation,'_build','')\nexcept Exception: pass",
      // Rebuild the boot banner: prefix from implementation name (MP or CP).
      "try:\n import os as _os\n _u=_os.uname()\n _d['machine']=_u.machine\n _impl=_d.get('impl') or 'MicroPython'\n _name='CircuitPython' if str(_impl).lower().startswith('circuit') else 'MicroPython'\n _d['version']=_name+' '+_u.version+' on '+_u.machine\nexcept Exception: pass",
      "try:\n import machine as _m\n _d['freq']=_m.freq()\nexcept Exception: pass",
      "try:\n import gc as _g\n _d['memfree']=_g.mem_free()\nexcept Exception: pass",
      "try:\n import esp as _e\n _d['flash']=_e.flash_size()\nexcept Exception: pass",
      // Largest HEAP_DATA region on a PSRAM board is the PSRAM bank (MBs), far
      // larger than internal DRAM regions (<~1MB); use it as the PSRAM size.
      "try:\n import esp32 as _e2\n _mx=0\n for _r in _e2.idf_heap_info(_e2.HEAP_DATA):\n  _mx=max(_mx,_r[0])\n if _mx>1048576: _d['psramBytes']=_mx\nexcept Exception: pass",
      "print('MPHINTS:'+_j.dumps(_d))",
    ].join("\n");
    try {
      const res = await this.bridge.request<{ output?: string }>("exec", {
        code,
        follow: true,
      });
      const out = String((res as any)?.output ?? "");
      const line = out.split(/\r?\n/).find((l) => l.startsWith("MPHINTS:"));
      if (line) {
        return JSON.parse(line.slice("MPHINTS:".length)) as Record<string, unknown>;
      }
    } catch {
      /* board busy / not MicroPython */
    }
    return {};
  }

  /** Apply Detect's suggested port/board/variant when confidence allows. */
  private applyDetect(res: Record<string, unknown>): void {
    const m = (res.match || {}) as Record<string, unknown>;
    const conf = String(m.confidence || "");
    const port = String(m.port || "");
    if (!port) {
      return;
    }
    if (res.espressif === false) {
      // Non-Espressif: only select the firmware port, never force an ESP board.
      if (conf === "family-only") {
        this.selectPort(port);
      }
      return;
    }
    if (conf === "matched" || conf === "family-only") {
      let board = String(m.board || "");
      const variant = String(m.variant || "");
      if (this.prefs.firmwareSource === "download" && board) {
        const inCatalog = this.downloadTree.some((p: any) =>
          (p.boards || []).some((b: any) => b.board === board)
        );
        if (!inCatalog) {
          this.log(
            `[mpftp] detected board ${board} not in download catalog — pick Target or Browse local`
          );
          // Keep port; prefer a GENERIC board on that port if available.
          const node = this.downloadTree.find((p: any) => p.port === port);
          const generic = node?.boards?.find((x: any) => /GENERIC/.test(x.board));
          board = generic?.board || "";
        }
      }
      this.selection = { port, board, variant };
      this.savePrefs();
    }
  }

  /**
   * After download-list scrapes MP variants (C6_WIFI, …), pick Wi-Fi variant
   * from Detect's MicroPython hints if Target still has the default image.
   */
  private reapplyWifiVariantFromHints(variants: string[]): void {
    if (this.selection.variant) {
      return;
    }
    const mp = ((this.lastDetect as any)?.mp || {}) as Record<string, unknown>;
    const blob = ["build", "machine", "version", "platform"]
      .map((k) => String(mp[k] || ""))
      .join(" ")
      .toUpperCase();
    for (const w of ["C6_WIFI", "C5_WIFI"]) {
      if (blob.includes(w) && (variants.length === 0 || variants.includes(w))) {
        this.selection = { ...this.selection, variant: w };
        this.savePrefs();
        this.log(`[mpftp] Target variant → ${w} (from running firmware)`);
        return;
      }
    }
  }

  /** Select a port node, picking a GENERIC board when the board is unknown. */
  private selectPort(port: string, board = "", variant = ""): void {
    let b = board;
    const node = this.tree.find((p: any) => p.port === port);
    if (!b && node?.boards?.length) {
      const generic = node.boards.find((x: any) => /GENERIC/.test(x.board));
      b = (generic || node.boards[0]).board;
    }
    this.selection = { port, board: b, variant };
    this.savePrefs();
  }

  private async autoSelectFromDevice(): Promise<void> {
    if (this.selection.port) {
      return;
    }
    // Best-effort chip hint: ask the connected board for its platform.
    let hintPort = "";
    if (this.bridge.connected) {
      try {
        const res = await this.bridge.request<{ value?: string }>("eval", {
          expr: "__import__('sys').platform",
        });
        const plat = String((res as any)?.value ?? res ?? "").replace(/['"]/g, "").trim();
        const map: Record<string, string> = {
          esp32: "esp32",
          rp2: "rp2",
          samd: "samd",
          pyboard: "stm32",
          mimxrt: "mimxrt",
          nrf52: "nrf",
        };
        hintPort = map[plat] || "";
      } catch {
        /* board may be busy */
      }
    }
    const flashable = this.tree.filter((p) => p.flashable);
    const hinted = hintPort ? this.tree.find((p) => p.port === hintPort) : undefined;
    const pick = hinted || flashable.find((p) => p.port === "esp32") || flashable[0];
    if (pick) {
      this.selection.port = pick.port;
      if (pick.boards?.length) {
        const generic = pick.boards.find((b: any) => /GENERIC/.test(b.board));
        this.selection.board = (generic || pick.boards[0]).board;
      } else if (pick.variants?.length) {
        this.selection.variant = "";
      }
    }
  }

  private async refreshArtifact(): Promise<void> {
    if (this.prefs.firmwareSource === "download") {
      const artVar = String(this.downloadedArtifact?.variant || "");
      if (
        this.downloadedArtifact?.ready &&
        this.downloadedArtifact.board === this.selection.board &&
        artVar === (this.selection.variant || "")
      ) {
        this.post({ type: "artifact", artifact: this.downloadedArtifact });
      } else {
        this.post({ type: "artifact", artifact: { ready: false } });
      }
      return;
    }
    if (!this.selection.port || !this.mpDir()) {
      this.post({ type: "artifact", artifact: { ready: false } });
      return;
    }
    try {
      const info = await this.engine.run("artifact", {
        ...this.pathArgs(),
        port: this.selection.port,
        board: this.selection.board,
        variant: this.selection.variant,
      });
      this.post({ type: "artifact", artifact: info });
    } catch {
      this.post({ type: "artifact", artifact: { ready: false } });
    }
  }

  private async pushDevices(): Promise<void> {
    let devices: Array<{ device: string; description?: string }> = [];
    try {
      await this.bridge.ensureStarted();
      const ports = await this.bridge.listPorts();
      devices = ports.map((p) => ({
        device: p.device,
        description: p.product || p.description || p.manufacturer || "",
      }));
    } catch {
      /* ignore */
    }
    if (!this.prefs.device && this.bridge.connectedDevice) {
      this.prefs.device = this.bridge.connectedDevice;
    }
    this.post({
      type: "devices",
      devices,
      device: this.prefs.device,
      connectedDevice: this.bridge.connectedDevice || "",
    });
  }

  private pushState(): void {
    this.post({
      type: "state",
      host: detectHost(),
      micropython: this.mpDir(),
      workspace: this.firmwareWorkspace() || this.discovery.workspace || null,
      tree: this.tree,
      cmods: this.cmods,
      flashers: this.flashers,
      selection: this.selection,
      prefs: this.prefs,
      downloadVersions: this.downloadVersions,
      downloadFamily: this.downloadFamily,
      connectedDevice: this.bridge.connectedDevice || "",
      detect: this.lastDetect || null,
      busy: this.busy,
    });
  }

  // ---------------------------------------------------------------------- //
  // Firmware workspace / MicroPython path
  // ---------------------------------------------------------------------- //

  /** Quick-pick open folders + Browse… when MicroPython is not resolved. */
  private async chooseWorkspace(): Promise<void> {
    type Item = vscode.QuickPickItem & { id: string; folder?: string };
    const items: Item[] = this.editorWorkspaces().map((f) => ({
      id: "folder",
      folder: f,
      label: path.basename(f),
      description: this.mpUnderWorkspace(f) ? "has MicroPython" : "no MicroPython yet",
      detail: f,
    }));
    items.push({
      id: "browse",
      label: "Browse…",
      description: "Pick a firmware workspace folder",
    });
    const pick = await vscode.window.showQuickPick(items, {
      title: "Select firmware workspace",
      placeHolder: "Folder containing micropython/ (or the MicroPython tree itself)",
    });
    if (!pick) {
      return;
    }
    let root = pick.folder || "";
    if (pick.id === "browse") {
      const uris = await vscode.window.showOpenDialog({
        canSelectFiles: false,
        canSelectFolders: true,
        canSelectMany: false,
        openLabel: "Select firmware workspace",
        title: "Select the firmware workspace (contains micropython/)",
      });
      if (!uris?.[0]) {
        return;
      }
      root = uris[0].fsPath;
    }
    const mp = this.mpUnderWorkspace(root);
    if (!mp) {
      void vscode.window.showErrorMessage(
        "That folder has no MicroPython tree (need micropython/ with ports/ and py/, or the tree itself)."
      );
      return;
    }
    const cfg = vscode.workspace.getConfiguration("mpftp");
    await cfg.update("workspacePath", root, vscode.ConfigurationTarget.Global);
    // Clear a stale MP override so the workspace wins.
    if (cfg.get<string>("micropythonPath")) {
      await cfg.update("micropythonPath", "", vscode.ConfigurationTarget.Global);
    }
    await this.refreshAll();
  }

  /** Advanced override: pin an explicit MicroPython checkout. */
  private async changePath(): Promise<void> {
    const uris = await vscode.window.showOpenDialog({
      canSelectFiles: false,
      canSelectFolders: true,
      canSelectMany: false,
      openLabel: "Select MicroPython folder",
      title: "Select the MicroPython checkout (contains ports/ and py/)",
    });
    if (!uris?.[0]) {
      return;
    }
    const p = uris[0].fsPath;
    if (!this.isMpTree(p)) {
      void vscode.window.showErrorMessage(
        "That folder is not a MicroPython tree (missing ports/ or py/)."
      );
      return;
    }
    const cfg = vscode.workspace.getConfiguration("mpftp");
    await cfg.update("micropythonPath", p, vscode.ConfigurationTarget.Global);
    const parent = path.dirname(p);
    if (!cfg.get<string>("workspacePath")) {
      await cfg.update("workspacePath", parent, vscode.ConfigurationTarget.Global);
    }
    await this.refreshAll();
  }

  /** Write bundled micropython.cmake + manifest-micropython.py into the firmware workspace. */
  private async createWorkspaceStubs(): Promise<void> {
    const workspace = this.firmwareWorkspace();
    if (!workspace) {
      void vscode.window.showErrorMessage(
        "Firmware workspace not set. Choose a workspace that contains MicroPython first."
      );
      return;
    }
    if (!fs.existsSync(workspace)) {
      void vscode.window.showErrorMessage(`Workspace folder not found: ${workspace}`);
      return;
    }

    const templatesDir = path.join(this.extensionPath, "resources", "templates");
    const stubs: Array<{ name: string; dest: string }> = [
      {
        name: "micropython.cmake",
        dest: path.join(workspace, "micropython.cmake"),
      },
      {
        name: "manifest-micropython.py",
        dest: path.join(workspace, "manifest-micropython.py"),
      },
    ];

    const toWrite: Array<{ name: string; dest: string; src: string }> = [];
    const skipped: string[] = [];
    for (const s of stubs) {
      const src = path.join(templatesDir, s.name);
      if (!fs.existsSync(src)) {
        void vscode.window.showErrorMessage(
          `mpftp template missing: resources/templates/${s.name}`
        );
        return;
      }
      if (fs.existsSync(s.dest)) {
        skipped.push(s.name);
        continue;
      }
      toWrite.push({ ...s, src });
    }

    if (!toWrite.length) {
      void vscode.window.showInformationMessage(
        `Workspace already has ${skipped.join(" and ")}.`
      );
      return;
    }

    const names = toWrite.map((t) => t.name).join(" + ");
    const ok = await vscode.window.showInformationMessage(
      `Create ${names} in ${workspace}?` +
        (skipped.length ? ` (keep existing ${skipped.join(", ")})` : ""),
      { modal: true },
      "Create"
    );
    if (ok !== "Create") {
      return;
    }

    try {
      for (const t of toWrite) {
        fs.copyFileSync(t.src, t.dest);
        this.log(`[mpftp] created ${t.dest}`);
      }
    } catch (e: any) {
      void vscode.window.showErrorMessage(
        `Failed to create workspace stubs: ${e?.message || e}`
      );
      return;
    }

    void vscode.window.showInformationMessage(
      `Created ${names}. Add sibling modules with their own micropython.cmake to include them in builds.`
    );
    await this.refreshAll();
  }

  // ---------------------------------------------------------------------- //
  // Build / Clean
  // ---------------------------------------------------------------------- //

  private guardMp(): boolean {
    if (!this.mpDir()) {
      void vscode.window.showErrorMessage(
        "MicroPython tree not found. Use Change… to select it."
      );
      return false;
    }
    if (!this.selection.port) {
      void vscode.window.showWarningMessage("Select a target port/board first.");
      return false;
    }
    return true;
  }

  private async doBuild(clean: boolean): Promise<void> {
    if (this.busy || !this.guardMp()) {
      return;
    }
    this.busy = true;
    this.phase("building", clean ? "Clean build…" : "Building…");
    this.post({ type: "clearLog" });
    this.activity.event("firmware_build_start", {
      data: { ...this.selection, clean },
    });
    const handle = this.engine.stream(
      "build",
      {
        ...this.pathArgs(),
        port: this.selection.port,
        board: this.selection.board,
        variant: this.selection.variant,
        clean,
      },
      (line) => this.log(line),
      (state, text) => this.phase(state, text)
    );
    this.activeStream = handle;
    const result = await handle.done;
    this.activeStream = undefined;
    this.busy = false;
    if (result.ok) {
      this.phase("ready", "Firmware ready");
      this.post({ type: "artifact", artifact: result });
      this.activity.event("firmware_build_ok", { data: { ...this.selection } });
      if (this.prefs.alsoFlashAfterBuild) {
        await this.doFlash();
      }
    } else if (result.needToolchain) {
      this.phase("failed", String(result.error || "Toolchain not found"));
      this.activity.event("firmware_build_need_toolchain", {
        data: { ...this.selection, toolchain: result.needToolchain },
      });
      const located = await this.promptToolchain(
        result.needToolchain as NeedToolchain
      );
      if (located) {
        await this.doBuild(clean);
      }
    } else {
      this.phase("failed", String(result.error || "Build failed"));
      this.activity.event("firmware_build_fail", {
        data: { ...this.selection, error: result.error },
      });
    }
  }

  /**
   * Prompt the user to resolve a missing build toolchain. Returns true if the
   * user located it (config was updated) so the caller can retry the build.
   */
  private async promptToolchain(need: NeedToolchain): Promise<boolean> {
    const detail = need.hint ? `\n${need.hint}` : "";
    const choice = await vscode.window.showWarningMessage(
      `${need.label} not found for the ${this.selection.port} build.${detail}`,
      "Locate…",
      "Install instructions",
      "Cancel"
    );
    if (choice === "Install instructions") {
      if (need.url) {
        void vscode.env.openExternal(vscode.Uri.parse(need.url));
      }
      return false;
    }
    if (choice !== "Locate…") {
      return false;
    }
    const conf = vscode.workspace.getConfiguration("mpftp");
    if (need.kind === "dir") {
      if (!need.configKey) {
        return false;
      }
      const uris = await vscode.window.showOpenDialog({
        canSelectFiles: false,
        canSelectFolders: true,
        canSelectMany: false,
        openLabel: `Select ${need.label} folder`,
        title: `Locate ${need.label}`,
      });
      if (!uris?.[0]) {
        return false;
      }
      await conf.update(
        need.configKey,
        uris[0].fsPath,
        vscode.ConfigurationTarget.Global
      );
      return true;
    }
    // command kind: pick the executable, persist its bin/ dir to toolchainBins.
    const uris = await vscode.window.showOpenDialog({
      canSelectFiles: true,
      canSelectFolders: false,
      canSelectMany: false,
      openLabel: `Select ${need.bin || need.label}`,
      title: `Locate ${need.bin || need.label}`,
    });
    if (!uris?.[0]) {
      return false;
    }
    const binDir = path.dirname(uris[0].fsPath);
    const cur = conf.get<string[]>("toolchainBins") || [];
    if (!cur.includes(binDir)) {
      await conf.update(
        "toolchainBins",
        [...cur, binDir],
        vscode.ConfigurationTarget.Global
      );
    }
    return true;
  }

  private async doClean(): Promise<void> {
    if (this.busy || !this.guardMp()) {
      return;
    }
    this.busy = true;
    this.phase("building", "Cleaning…");
    this.post({ type: "clearLog" });
    const handle = this.engine.stream(
      "clean",
      {
        ...this.pathArgs(),
        port: this.selection.port,
        board: this.selection.board,
        variant: this.selection.variant,
      },
      (line) => this.log(line)
    );
    this.activeStream = handle;
    const result = await handle.done;
    this.activeStream = undefined;
    this.busy = false;
    this.phase(result.ok ? "idle" : "failed", result.ok ? "Cleaned" : "Clean failed");
    await this.refreshArtifact();
  }

  // ---------------------------------------------------------------------- //
  // Flash
  // ---------------------------------------------------------------------- //

  private async doDownload(): Promise<void> {
    if (this.busy) {
      return;
    }
    if (!this.selection.board) {
      void vscode.window.showWarningMessage("Select a board in Target first.");
      return;
    }
    this.busy = true;
    this.phase("building", "Downloading…");
    this.post({ type: "clearLog" });
    const handle = this.engine.stream(
      "download",
      {
        board: this.selection.board,
        variant: this.selection.variant || undefined,
        version: this.prefs.downloadPreview
          ? undefined
          : this.prefs.downloadVersion || undefined,
        preview: this.prefs.downloadPreview ? true : undefined,
      },
      (line) => this.log(line)
    );
    this.activeStream = handle;
    const result = await handle.done;
    this.activeStream = undefined;
    this.busy = false;
    if (result.ok && result.artifact) {
      this.downloadedArtifact = { ...result, ready: true };
      if (typeof result.family === "string") {
        this.downloadFamily = result.family;
      }
      if (typeof result.port === "string") {
        this.selection.port = result.port;
      }
      this.phase("ready", "Downloaded");
      const path = String(result.artifact || "");
      this.log(`[mpftp] downloaded → ${path}`);
      this.post({ type: "artifact", artifact: this.downloadedArtifact });
      if (typeof result.flashOffset === "string" && result.flashOffset) {
        this.post({
          type: "flashOffsetDefault",
          flashOffset: result.flashOffset,
        });
      }
      this.pushState();
    } else {
      this.downloadedArtifact = undefined;
      this.phase("failed", String(result.error || "Download failed"));
      this.post({ type: "artifact", artifact: { ready: false } });
    }
  }

  private async browseArtifact(): Promise<void> {
    const uris = await vscode.window.showOpenDialog({
      canSelectFiles: true,
      canSelectFolders: false,
      canSelectMany: false,
      openLabel: "Use firmware file",
      title: "Select a .bin or .uf2 firmware file",
      filters: { Firmware: ["bin", "uf2", "hex"] },
    });
    if (!uris?.[0]) {
      return;
    }
    const p = uris[0].fsPath;
    let size = 0;
    try {
      size = fs.statSync(p).size;
    } catch {
      /* ignore */
    }
    this.downloadedArtifact = {
      ready: true,
      artifact: p,
      size,
      mtime: Date.now() / 1000,
      source: "local",
      board: this.selection.board,
      family: this.downloadFamily,
      port: this.selection.port,
    };
    this.post({ type: "artifact", artifact: this.downloadedArtifact });
    this.log(`[mpftp] using local firmware ${p}`);
  }

  private async doFlash(
    offset = "",
    baud = "",
    before = "",
    after = "",
    erase = false
  ): Promise<void> {
    const downloadMode = this.prefs.firmwareSource === "download";
    if (this.busy || (!downloadMode && !this.guardMp())) {
      return;
    }
    if (downloadMode && !this.downloadedArtifact?.ready) {
      void vscode.window.showWarningMessage("Download firmware first (or Browse local…).");
      return;
    }
    const port = this.selection.port;
    if (!this.flashers[port] && !downloadMode) {
      void vscode.window.showWarningMessage(`Flashing is not supported for '${port}'.`);
      return;
    }
    if (downloadMode && !["esp32", "rp2", "samd"].includes(port)) {
      void vscode.window.showWarningMessage(`Flashing is not supported for '${port}'.`);
      return;
    }
    const device = this.prefs.device;
    const isSerial = port === "esp32";
    if (isSerial && !device) {
      void vscode.window.showWarningMessage("Select a device (serial port) to flash.");
      return;
    }

    if (!(await this.securityGuard(port, device))) {
      return;
    }

    this.busy = true;
    this.flashStatus("flashing", "Flashing…");
    this.post({ type: "clearLog" });

    // Release the port if we are connected to the flash target. For native-USB
    // ESP32 parts running MicroPython, esptool must talk to the ROM
    // USB-Serial/JTAG port, which differs from the app CDC we are attached to.
    let flashDevice = device;
    let wasConnected = false;
    const connected = this.bridge.connectedDevice;
    if (connected && (connected === device || !isSerial)) {
      wasConnected = true;
      try {
        if (port === "rp2" || port === "samd") {
          this.log("[mpftp] rebooting board into bootloader…");
          try {
            await this.bridge.request("bootloader", {});
          } catch {
            /* board may already be in bootloader */
          }
          await this.bridge.disconnect();
          this.log(`[mpftp] disconnected ${connected} for flashing`);
        } else if (isSerial) {
          const rom = await this.prepareEspFlashPort(device);
          if (!rom) {
            this.busy = false;
            this.flashStatus(
              "failed",
              "No download port — hold BOOT + tap RESET, then retry"
            );
            this.log(
              "[mpftp] no ROM download port appeared. Put the board in download mode " +
                "(hold BOOT, tap RESET) or use its USB-Serial/JTAG connector, then flash again."
            );
            await this.pushDevices();
            return;
          }
          flashDevice = rom;
        } else {
          await this.bridge.disconnect();
          this.log(`[mpftp] disconnected ${connected} for flashing`);
        }
      } catch {
        /* ignore */
      }
    }

    const esptool = this.engine.esptoolCommand();
    const artifactPath =
      downloadMode && typeof this.downloadedArtifact?.artifact === "string"
        ? String(this.downloadedArtifact.artifact)
        : undefined;
    const handle = this.engine.stream(
      "flash",
      {
        ...(downloadMode ? {} : this.pathArgs()),
        // Download mode still passes mp if known (better offset from board.json).
        ...(downloadMode && this.mpDir() ? { mp: this.mpDir() } : {}),
        port,
        board: this.selection.board,
        variant: this.selection.variant,
        family: downloadMode ? this.downloadFamily || undefined : undefined,
        artifact: artifactPath,
        device: flashDevice,
        esptool: esptool || undefined,
        offset: port === "esp32" ? offset || undefined : undefined,
        baud: port === "esp32" ? baud || undefined : undefined,
        before: port === "esp32" ? before || undefined : undefined,
        after: port === "esp32" ? after || undefined : undefined,
        erase: port === "esp32" && erase ? true : undefined,
      },
      (line) => this.log(line)
    );
    this.activeStream = handle;
    const result = await handle.done;
    this.activeStream = undefined;
    this.busy = false;

    if (result.ok) {
      this.flashStatus("ok", "Flashed — ready for the next board");
      this.activity.event("firmware_flash_ok", {
        data: { ...this.selection, device },
      });
      this.post({ type: "flashed", device });
    } else if (result.needEraseConfirm) {
      const detail = String(
        (result.needEraseConfirm as { message?: string }).message ||
          result.error ||
          "Partition layout changed — erase required."
      );
      this.flashStatus("failed", "Erase required — enable Erase, then Flash again");
      this.log(`[mpftp] ${detail}`);
      this.activity.event("firmware_flash_need_erase", {
        data: { ...this.selection, device },
      });
      this.post({ type: "needEraseConfirm", message: detail });
      void vscode.window.showWarningMessage(
        "Partition layout changed. Enable “Erase flash before writing” and click Flash again. " +
          "Erase wipes the filesystem (vfs/storage) partition — all board files will be lost.",
        "OK"
      );
    } else {
      this.flashStatus("failed", String(result.error || "Flash failed"));
      this.activity.event("firmware_flash_fail", {
        data: { ...this.selection, device, error: result.error },
      });
    }

    if (wasConnected && this.prefs.reconnectAfterFlash && device) {
      try {
        await this.bridge.connect(device, undefined, { silent: true });
        this.log(`[mpftp] reconnected ${device}`);
      } catch (e: any) {
        this.log(`[mpftp] reconnect failed: ${e?.message || e}`);
      }
    }
    await this.pushDevices();
    await this.refreshArtifact();
  }

  /**
   * Block esp32 flashing when the last Detect reported flash encryption or
   * secure boot enabled — writing a fresh image can brick or fail to boot.
   * Returns false to abort the flash.
   */
  private async securityGuard(port: string, device: string): Promise<boolean> {
    if (port !== "esp32" || !this.lastDetect) {
      return true;
    }
    const det = this.lastDetect as any;
    if (det.device !== device || !det.security) {
      return true;
    }
    const enc = /enable/i.test(String(det.security.flashEncryption || ""));
    const sb = /enable/i.test(String(det.security.secureBoot || ""));
    if (!enc && !sb) {
      return true;
    }
    const what = [enc ? "flash encryption" : "", sb ? "secure boot" : ""]
      .filter(Boolean)
      .join(" and ");
    const pick = await vscode.window.showWarningMessage(
      `This device reports ${what} enabled. Flashing a new image may brick it ` +
        `or fail to boot. Continue anyway?`,
      { modal: true },
      "Flash anyway"
    );
    return pick === "Flash anyway";
  }

  private async changeDevice(): Promise<void> {
    const port = this.selection.port;
    if (port === "rp2" || port === "samd") {
      const uris = await vscode.window.showOpenDialog({
        canSelectFiles: false,
        canSelectFolders: true,
        canSelectMany: false,
        openLabel: "Select UF2 drive",
        title: "Select the board's UF2 bootloader drive (RPI-RP2, etc.)",
      });
      if (uris?.[0]) {
        this.prefs.device = uris[0].fsPath;
        this.savePrefs();
        await this.pushDevices();
      }
      return;
    }
    try {
      await this.bridge.ensureStarted();
      const ports = filterAndSortPorts(await this.bridge.listPorts(), {
        lastDevice: this.bridge.lastDevice || this.prefs.device || undefined,
        lastVidPid: this.bridge.rememberedVidPid,
      });
      const items = ports.map((p) => ({
        label: p.device,
        description: [p.product || p.description || "", p.interface || ""]
          .filter(Boolean)
          .join(" · "),
      }));
      const manual = { label: "$(edit) Enter manually…", description: "" };
      const pick = await vscode.window.showQuickPick([...items, manual], {
        title: "Select device to flash",
      });
      if (!pick) {
        return;
      }
      if (pick === manual) {
        const v = await vscode.window.showInputBox({ prompt: "Device (e.g. COM5)" });
        if (v) {
          this.prefs.device = v;
        }
      } else {
        this.prefs.device = pick.label;
      }
      this.savePrefs();
      await this.pushDevices();
    } catch (e: any) {
      void vscode.window.showErrorMessage(`Could not list ports: ${e?.message || e}`);
    }
  }

  // ---------------------------------------------------------------------- //
  // HTML
  // ---------------------------------------------------------------------- //

  private getHtml(webview: vscode.Webview): string {
    const css = webview.asWebviewUri(
      vscode.Uri.joinPath(this.extensionUri, "media", "firmware.css")
    );
    const js = webview.asWebviewUri(
      vscode.Uri.joinPath(this.extensionUri, "media", "firmware.js")
    );
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
  <title>mpftp Firmware</title>
</head>
<body>
  <div id="app"></div>
  <script src="${js}"></script>
</body>
</html>`;
  }
}
