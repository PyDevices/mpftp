import * as vscode from "vscode";
import { ActivityLog } from "./activityLog";
import { AgentRpcServer } from "./agent/AgentRpcServer";
import { SidecarBridge, PortInfo } from "./bridge/SidecarBridge";
import { openBoardFileInEditor, registerEditSaveHook } from "./editRemote";
import { openRepl } from "./terminal/ReplTerminal";
import { FtpViewProvider } from "./webview/FtpViewProvider";
import { FirmwarePanel } from "./firmware/FirmwarePanel";
import {
  detectHost,
  filterAndSortPorts,
  getConfig,
  resolvePython,
} from "./platform";

let bridge: SidecarBridge;
let log: vscode.OutputChannel;
let activity: ActivityLog;
let agentRpc: AgentRpcServer;
let ftpProvider: FtpViewProvider;
let firmwarePanel: FirmwarePanel;
let statusBar: vscode.StatusBarItem;

export async function activate(context: vscode.ExtensionContext): Promise<void> {
  log = vscode.window.createOutputChannel("mpftp");
  activity = new ActivityLog();
  bridge = new SidecarBridge(
    context.extensionPath,
    log,
    activity,
    context.globalState,
    vscode.env.sessionId
  );
  bridge.seedLastDeviceFromGlobalState();
  agentRpc = new AgentRpcServer(bridge, activity, context.extensionPath);
  agentRpc.start();

  statusBar = vscode.window.createStatusBarItem(vscode.StatusBarAlignment.Left, 80);
  statusBar.command = "mpftp.connect";
  statusBar.text = "$(plug) mpftp";
      statusBar.tooltip = "Connect to MicroPython or CircuitPython board";
  statusBar.show();

  const ensureConnected = async (): Promise<boolean> => {
    if (bridge.connected) {
      return true;
    }
    await connectCommand();
    return bridge.connected;
  };

  const connectCommand = async (): Promise<void> => {
    if (bridge.connected) {
      // Labels match package.json / ⋯ menu (without the "mpftp: " prefix).
      const choice = await vscode.window.showQuickPick(
        [
          { label: "Disconnect", id: "disconnect" },
          { label: "Open REPL", id: "repl" },
          { label: "Open File Transfer in Panel", id: "ftp" },
          { label: "Open File Transfer in Editor", id: "ftpEditor" },
        ],
        { title: `Connected to ${bridge.connectedDevice}` }
      );
      if (choice?.id === "disconnect") {
        await bridge.disconnect();
        updateStatus();
      } else if (choice?.id === "repl") {
        openRepl(bridge, activity);
      } else if (choice?.id === "ftp") {
        await ftpProvider.openInPanel();
      } else if (choice?.id === "ftpEditor") {
        ftpProvider.openInEditor();
      }
      return;
    }

    await vscode.window.withProgress(
      { location: vscode.ProgressLocation.Notification, title: "mpftp: listing serial ports…" },
      async () => {
        await bridge.ensureStarted();
      }
    );

    const ports = filterAndSortPorts(await bridge.listPorts(), {
      lastDevice: bridge.lastDevice,
      lastVidPid: bridge.rememberedVidPid,
    });
    const items = ports.map((p) => portQuickPick(p));
    if (!items.length) {
      const host = detectHost();
      void vscode.window.showWarningMessage(
        host === "wsl"
          ? "No serial ports found. On WSL, mpftp uses Windows python.exe / COM ports. Is the board plugged in?"
          : "No serial ports found."
      );
      return;
    }

    const cfg = getConfig();
    let device = cfg.autoConnectDevice;
    if (!device) {
      // Last-good port is sorted first; preselect it in the quick pick.
      const pick = await showPortQuickPick(items, "Select MicroPython board");
      if (!pick) {
        return;
      }
      device = pick.device;
    }

    try {
      const connected = await vscode.window.withProgress(
        {
          location: vscode.ProgressLocation.Notification,
          title: `mpftp: connecting ${device}…`,
        },
        async () => bridge.connect(device)
      );
      updateStatus();
      const fsWarn =
        connected && typeof connected === "object"
          ? connected.filesystem_warning
          : undefined;
      if (fsWarn) {
        void vscode.window.showWarningMessage(`mpftp connected: ${device} — ${fsWarn}`);
      } else {
        void vscode.window.showInformationMessage(`mpftp connected: ${device}`);
      }
    } catch (e: any) {
      void vscode.window.showErrorMessage(`mpftp connect failed: ${e.message || e}`);
      log.appendLine(String(e));
    }
  };

  ftpProvider = new FtpViewProvider(
    context.extensionUri,
    bridge,
    () => {
      if (!bridge.connected) {
        void vscode.window.showWarningMessage("Connect to a board first");
        return;
      }
      openRepl(bridge, activity);
    },
    connectCommand,
    async () => {
      await bridge.disconnect();
      updateStatus();
    }
  );

  firmwarePanel = new FirmwarePanel(context.extensionUri, context.extensionPath, bridge, activity);

  registerEditSaveHook(bridge, context, log);

  context.subscriptions.push(
    log,
    statusBar,
    bridge,
    vscode.window.registerWebviewViewProvider(FtpViewProvider.viewType, ftpProvider),
    vscode.commands.registerCommand("mpftp.connect", connectCommand),
    vscode.commands.registerCommand("mpftp.disconnect", async () => {
      await bridge.disconnect();
      updateStatus();
    }),
    vscode.commands.registerCommand("mpftp.resume", async () => {
      try {
        await bridge.ensureStarted();
        await bridge.resume();
        updateStatus();
        void vscode.window.showInformationMessage(`mpftp resumed: ${bridge.connectedDevice}`);
      } catch (e: any) {
        void vscode.window.showErrorMessage(`mpftp resume failed: ${e.message || e}`);
      }
    }),
    vscode.commands.registerCommand("mpftp.openFtp", () => ftpProvider.openInPanel()),
    vscode.commands.registerCommand("mpftp.openFtpEditor", () => ftpProvider.openInEditor()),
    vscode.commands.registerCommand("mpftp.openFirmware", () => firmwarePanel.reveal()),
    vscode.commands.registerCommand("mpftp.openRepl", async () => {
      if (!(await ensureConnected())) {
        return;
      }
      openRepl(bridge, activity);
    }),
    vscode.commands.registerCommand("mpftp.editRemote", async () => {
      if (!(await ensureConnected())) {
        return;
      }
      const remote = await vscode.window.showInputBox({
        prompt: "Board file path to edit",
        value: "/main.py",
      });
      if (!remote) {
        return;
      }
      try {
        await openBoardFileInEditor(bridge, remote, log);
      } catch (e: any) {
        void vscode.window.showErrorMessage(`mpftp edit failed: ${e.message || e}`);
      }
    }),
    vscode.commands.registerCommand("mpftp.agentStatus", async () => {
      await bridge.ensureStarted();
      const info = {
        connected: bridge.connected,
        device: bridge.connectedDevice || null,
        rpc: agentRpc.path,
        activityLog: activity.activityPath,
        replLog: activity.replPath,
      };
      log.appendLine(JSON.stringify(info, null, 2));
      log.show(true);
      void vscode.window.showInformationMessage(
        `mpftp agent RPC: ${info.rpc}` + (info.connected ? ` · ${info.device}` : " · not connected")
      );
    }),
    vscode.commands.registerCommand("mpftp.interrupt", async () => {
      if (!(await ensureConnected())) {
        return;
      }
      await bridge.request("interrupt");
      void vscode.window.showInformationMessage("Interrupt (Ctrl+C) sent");
    }),
    vscode.commands.registerCommand("mpftp.softReset", async () => {
      if (!(await ensureConnected())) {
        return;
      }
      const res = await bridge.request<{ interpreter?: string; main_skipped?: boolean }>("soft_reset");
      const interpreter = res.interpreter || bridge.interpreter || "micropython";
      if (interpreter === "circuitpython") {
        void vscode.window.showInformationMessage(
          "Soft reset sent (CircuitPython: friendly↔raw; code.py not auto-run)"
        );
      } else {
        void vscode.window.showInformationMessage("Soft reset sent (main.py not run)");
      }
    }),
    vscode.commands.registerCommand("mpftp.softReboot", async () => {
      if (!(await ensureConnected())) {
        return;
      }
      const res = await bridge.request<{ interpreter?: string; note?: string }>("soft_reboot");
      const interpreter = res.interpreter || bridge.interpreter || "micropython";
      void vscode.window.showInformationMessage(
        interpreter === "circuitpython"
          ? "Soft reboot sent (Ctrl-D; code.py will run)"
          : "Soft reboot sent (Ctrl-D; main.py will run)"
      );
    }),
    vscode.commands.registerCommand("mpftp.hardReset", async () => {
      if (!(await ensureConnected())) {
        return;
      }
      const device = bridge.connectedDevice;
      try {
        await Promise.race([
          bridge.request("hard_reset"),
          new Promise((_, rej) => setTimeout(() => rej(new Error("hard_reset timeout")), 5000)),
        ]);
      } catch (e) {
        log.appendLine(`hard_reset: ${e}`);
      } finally {
        await bridge.disconnect().catch(() => undefined);
        updateStatus();
      }
      if (getConfig().autoReconnectAfterReset && device) {
        void vscode.window.withProgress(
          {
            location: vscode.ProgressLocation.Notification,
            title: `mpftp: waiting to reconnect ${device}…`,
            cancellable: true,
          },
          async (_progress, token) => {
            const ok = await bridge.reconnectAfterReset({ device, token });
            updateStatus();
            if (token.isCancellationRequested) {
              void vscode.window.showInformationMessage(
                "Reconnect cancelled — use Resume or Connect when ready"
              );
            } else if (ok) {
              void vscode.window.showInformationMessage(`mpftp reconnected: ${device}`);
            } else {
              void vscode.window.showWarningMessage(
                "Hard reset sent — board did not come back; use Resume or Connect"
              );
            }
          }
        );
      } else {
        void vscode.window.showInformationMessage("Hard reset sent — reconnect when ready");
      }
    }),
    vscode.commands.registerCommand("mpftp.bootloader", async () => {
      if (!(await ensureConnected())) {
        return;
      }
      try {
        await Promise.race([
          bridge.request("bootloader"),
          new Promise((_, rej) => setTimeout(() => rej(new Error("bootloader timeout")), 5000)),
        ]);
      } catch (e) {
        log.appendLine(`bootloader: ${e}`);
      } finally {
        await bridge.disconnect().catch(() => undefined);
        updateStatus();
      }
      void vscode.window.showInformationMessage(
        "Entered bootloader — flash firmware, then Connect (auto-reconnect skipped)"
      );
    }),
    vscode.commands.registerCommand("mpftp.runFile", async () => {
      if (!(await ensureConnected())) {
        return;
      }
      const ed = vscode.window.activeTextEditor;
      if (!ed) {
        void vscode.window.showWarningMessage("mpftp: open a .py editor tab to run the buffer");
        return;
      }
      const source = ed.document.getText();
      // follow=false: leave UART free for prints and input(); same as File Transfer → Run.
      await bridge.request("run_script", { source, follow: false });
      openRepl(bridge, activity);
    }),
    vscode.commands.registerCommand("mpftp.eval", async () => {
      if (!(await ensureConnected())) {
        return;
      }
      const expr = await vscode.window.showInputBox({ prompt: "Expression to eval on board" });
      if (!expr) {
        return;
      }
      const res = await bridge.request<{ value: string }>("eval", { expr });
      void vscode.window.showInformationMessage(res.value);
    }),
    vscode.commands.registerCommand("mpftp.exec", async () => {
      if (!(await ensureConnected())) {
        return;
      }
      const code = await vscode.window.showInputBox({ prompt: "Code to exec on board" });
      if (!code) {
        return;
      }
      const res = await bridge.request<{ output: string }>("exec", { code, follow: true });
      log.appendLine(res.output || "");
      log.show(true);
    }),
    vscode.commands.registerCommand("mpftp.rtcGet", async () => {
      if (!(await ensureConnected())) {
        return;
      }
      const res = await bridge.request<{ datetime: string }>("rtc_get");
      void vscode.window.showInformationMessage(`RTC: ${res.datetime}`);
    }),
    vscode.commands.registerCommand("mpftp.rtcSet", async () => {
      if (!(await ensureConnected())) {
        return;
      }
      const res = await bridge.request<{ datetime: number[] }>("rtc_set");
      void vscode.window.showInformationMessage(`RTC set: ${JSON.stringify(res.datetime)}`);
    }),
    vscode.commands.registerCommand("mpftp.installPackage", async () => {
      if (!(await ensureConnected())) {
        return;
      }
      const interpreter = bridge.interpreter || "micropython";
      if (interpreter === "circuitpython") {
        await vscode.commands.executeCommand("mpftp.circupInstall");
      } else {
        await vscode.commands.executeCommand("mpftp.mipInstall");
      }
    }),
    vscode.commands.registerCommand("mpftp.mipInstall", async () => {
      if (!(await ensureConnected())) {
        return;
      }
      const pkg = await vscode.window.showInputBox({
        prompt: "Package to install via mip (host downloads, writes to board)",
        placeHolder: "github:org/repo or micropython-lib name",
      });
      if (!pkg) {
        return;
      }
      const res = await bridge.request<{ output: string; target?: string }>("mip_install", {
        packages: [pkg],
        mpy: true,
      });
      log.appendLine(res.output || "");
      if (res.target) {
        log.appendLine(`target: ${res.target}`);
      }
      log.show(true);
      void vscode.window.showInformationMessage(`mip installed ${pkg}`);
    }),
    vscode.commands.registerCommand("mpftp.circupInstall", async () => {
      if (!(await ensureConnected())) {
        return;
      }
      const pkg = await vscode.window.showInputBox({
        prompt: "Package to install via circup (host downloads, serial write to /lib)",
        placeHolder: "adafruit_display_text",
      });
      if (!pkg) {
        return;
      }
      const res = await bridge.request<{ output: string; target?: string }>("circup_install", {
        packages: [pkg],
        py: false,
      });
      log.appendLine(res.output || "");
      if (res.target) {
        log.appendLine(`target: ${res.target}`);
      }
      log.show(true);
      void vscode.window.showInformationMessage(`circup installed ${pkg}`);
    }),
    vscode.commands.registerCommand("mpftp.df", async () => {
      if (!(await ensureConnected())) {
        return;
      }
      const res = await bridge.request<{ mounts: any[] }>("df");
      log.appendLine(JSON.stringify(res.mounts, null, 2));
      log.show(true);
    }),
    vscode.commands.registerCommand("mpftp.mount", async () => {
      if (!(await ensureConnected())) {
        return;
      }
      const uris = await vscode.window.showOpenDialog({
        canSelectFiles: false,
        canSelectFolders: true,
        canSelectMany: false,
        openLabel: "Mount on board as /remote",
      });
      if (!uris?.[0]) {
        return;
      }
      const res = await bridge.request<{ path: string; mount: string }>("mount", {
        path: uris[0].fsPath,
      });
      void vscode.window.showInformationMessage(`Mounted ${res.path} at ${res.mount}`);
    }),
    vscode.commands.registerCommand("mpftp.umount", async () => {
      if (!(await ensureConnected())) {
        return;
      }
      await bridge.request("umount");
      void vscode.window.showInformationMessage("Unmounted /remote");
    }),
    vscode.commands.registerCommand("mpftp.romfsQuery", async () => {
      if (!(await ensureConnected())) {
        return;
      }
      const res = await bridge.request<{ output: string }>("romfs_query");
      log.appendLine(res.output || "(no output)");
      log.show(true);
    }),
    vscode.commands.registerCommand("mpftp.hashRemote", async () => {
      if (!(await ensureConnected())) {
        return;
      }
      const remote = await vscode.window.showInputBox({
        prompt: "Board file to hash",
        value: "/main.py",
      });
      if (!remote) {
        return;
      }
      const res = await bridge.request<{ hash: string; algo: string }>("fs_hash", {
        path: remote,
        algo: "sha256",
      });
      void vscode.window.showInformationMessage(`${res.algo}: ${res.hash}`);
      log.appendLine(`${remote}: ${res.hash}`);
    }),
    vscode.commands.registerCommand("mpftp.refreshPorts", async () => {
      await bridge.ensureStarted();
      const ports = await bridge.listPorts();
      log.appendLine(ports.map((p) => `${p.device}\t${p.product || ""}\t${p.serial_number || ""}`).join("\n"));
      log.show(true);
    })
  );

  bridge.on("connected", (_device?: string, meta?: { silent?: boolean }) => {
    updateStatus();
    if (getConfig().openEditorOnConnect && !meta?.silent) {
      // Open UI only in this extension host / window — the one whose Agent RPC
      // handled the connect. Agents match cwd via ~/.mpftp/workspace-rpc.json
      // so File Transfer + REPL appear beside the chat that issued connect, not
      // in a different Cursor window that merely holds ~/.mpftp/rpc.port.
      ftpProvider.openInEditor();
      openRepl(bridge, activity);
    }
  });
  bridge.on("disconnected", updateStatus);

  const host = detectHost();
  const py = resolvePython(context.extensionPath, getConfig().pythonPath);
  log.appendLine(`[mpftp] host=${host} python=${py}`);
  log.appendLine(`[mpftp] session: ${bridge.sessionId}`);
  log.appendLine(`[mpftp] agent RPC: ${agentRpc.path}`);
  log.appendLine(`[mpftp] activity log: ${activity.activityPath}`);
  log.appendLine(`[mpftp] repl log: ${activity.replPath}`);
  activity.event("activate", {
    message: "extension activated",
    data: {
      host,
      python: py,
      rpc: agentRpc.path,
      sessionId: bridge.sessionId,
    },
  });
  updateStatus();

  context.subscriptions.push({
    dispose: () => {
      // Must dispose the bridge here too — Cursor/WSL reloads sometimes skip
      // deactivate(), which previously left python.exe sidecars holding COM ports.
      agentRpc.dispose();
      bridge.dispose();
      activity.event("deactivate", { message: "extension deactivated" });
    },
  });

  if (getConfig().autoConnectDevice) {
    void connectCommand();
  }
}

type PortPickItem = vscode.QuickPickItem & { device: string };

function portQuickPick(p: PortInfo): PortPickItem {
  const vidpid =
    p.vid != null && p.pid != null
      ? `${p.vid.toString(16).padStart(4, "0")}:${p.pid.toString(16).padStart(4, "0")}`
      : "";
  const detailParts = [p.interface, p.serial_number || p.description].filter(Boolean);
  return {
    label: p.device,
    description: [p.product, p.manufacturer, vidpid].filter(Boolean).join(" · "),
    detail: detailParts.length ? detailParts.join(" · ") : undefined,
    device: p.device,
  };
}

function showPortQuickPick(
  items: PortPickItem[],
  title: string
): Promise<PortPickItem | undefined> {
  return new Promise((resolve) => {
    const qp = vscode.window.createQuickPick<PortPickItem>();
    let settled = false;
    const finish = (value: PortPickItem | undefined) => {
      if (settled) {
        return;
      }
      settled = true;
      qp.dispose();
      resolve(value);
    };
    qp.title = title;
    qp.placeholder = "Serial port";
    qp.items = items;
    if (items.length) {
      qp.activeItems = [items[0]];
    }
    qp.onDidAccept(() => {
      finish(qp.selectedItems[0]);
    });
    qp.onDidHide(() => {
      finish(undefined);
    });
    qp.show();
  });
}

function updateStatus(): void {
  const session = bridge?.sessionId ? `session ${bridge.sessionId.slice(0, 8)}…` : "";
  if (bridge.connected) {
    const rt = bridge.interpreter === "circuitpython" ? "CP" : "MP";
    statusBar.text = `$(check) mpftp: ${bridge.connectedDevice} (${rt})`;
    statusBar.tooltip =
      `mpftp connected in this window (${bridge.interpreter || "micropython"}) — ` +
      `RPC ${agentRpc?.path || "?"} · ${session}. ` +
      `Other windows may own a different board concurrently; the same COM is exclusive.`;
  } else {
    statusBar.text = "$(plug) mpftp";
    statusBar.tooltip =
      `Connect to a board in this window (RPC ${agentRpc?.path || "?"} · ${session}). ` +
      `Not connected here — another window may own a different COM port.`;
  }
}

export function deactivate(): void {
  agentRpc?.dispose();
  bridge?.dispose();
}
