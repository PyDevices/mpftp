import * as fs from "fs";
import * as net from "net";
import * as os from "os";
import * as path from "path";
import * as vscode from "vscode";
import { ActivityLog } from "../activityLog";
import { SidecarBridge } from "../bridge/SidecarBridge";
import { FirmwareEngine } from "../firmware/engine";
import { getConfig } from "../platform";

const DEFAULT_PORT = 7429;
const HOST = "127.0.0.1";

/**
 * Local JSON-line TCP RPC so agents/CLI share the extension's sidecar session.
 * Uses TCP (not Unix sockets) so WSL UI + Windows python.exe CLI can both talk.
 * Protocol matches sidecar.py: {"id","method","params"} → result|error.
 * Extra methods: agent_status, agent_paths, status.
 */
export class AgentRpcServer {
  private server: net.Server | undefined;
  private port = DEFAULT_PORT;

  private readonly firmware: FirmwareEngine;

  constructor(
    private readonly bridge: SidecarBridge,
    private readonly activity: ActivityLog,
    extensionPath: string
  ) {
    this.firmware = new FirmwareEngine(extensionPath);
  }

  get path(): string {
    return `${HOST}:${this.port}`;
  }

  start(): void {
    this.server = net.createServer((socket) => {
      let buf = "";
      socket.setEncoding("utf8");
      socket.on("data", (chunk: string) => {
        buf += chunk;
        let idx: number;
        while ((idx = buf.indexOf("\n")) >= 0) {
          const line = buf.slice(0, idx).trim();
          buf = buf.slice(idx + 1);
          if (!line) {
            continue;
          }
          void this.handleLine(socket, line);
        }
      });
    });

    const tryListen = (port: number, attemptsLeft: number) => {
      this.server!.once("error", (err: NodeJS.ErrnoException) => {
        if (err.code === "EADDRINUSE" && attemptsLeft > 0) {
          tryListen(port + 1, attemptsLeft - 1);
          return;
        }
        this.activity.event("rpc_error", {
          message: String(err),
          data: { host: HOST, port },
        });
      });
      this.server!.listen(port, HOST, () => {
        this.port = port;
        const addr = `${HOST}:${port}`;
        this.activity.writeRpcPath(addr);
        this.writePortFiles(port);
        this.activity.event("rpc_listen", {
          message: `agent RPC listening on ${addr}`,
          data: { host: HOST, port },
        });
      });
    };

    tryListen(DEFAULT_PORT, 20);
  }

  private writePortFiles(port: number): void {
    const addr = `${HOST}:${port}`;
    const homeDir = path.join(os.homedir(), ".mpftp");
    // Map each open workspace root → this window's RPC under ~/.mpftp so the
    // CLI (cwd inside that tree) can find us without littering the repo with
    // a .mpftp/ directory. Home rpc.port remains the last-writer fallback.
    try {
      fs.mkdirSync(homeDir, { recursive: true, mode: 0o700 });
      const registryPath = path.join(homeDir, "workspace-rpc.json");
      const registry = readWorkspaceRpcRegistry(registryPath);
      for (const folder of vscode.workspace.workspaceFolders || []) {
        registry[path.resolve(folder.uri.fsPath)] = addr;
      }
      fs.writeFileSync(registryPath, JSON.stringify(registry, null, 2) + "\n", "utf8");
      fs.writeFileSync(path.join(homeDir, "rpc.port"), addr + "\n", "utf8");
    } catch {
      /* ignore */
    }
  }

  private async handleLine(socket: net.Socket, line: string): Promise<void> {
    let msg: { id?: number; method?: string; params?: Record<string, unknown> };
    try {
      msg = JSON.parse(line);
    } catch {
      socket.write(JSON.stringify({ type: "error", id: null, error: "invalid json" }) + "\n");
      return;
    }
    const id = msg.id ?? 0;
    const method = msg.method || "";
    const params = msg.params || {};
    try {
      const result = await this.dispatch(method, params);
      socket.write(JSON.stringify({ type: "result", id, result }) + "\n");
    } catch (e: any) {
      socket.write(
        JSON.stringify({ type: "error", id, error: e?.message || String(e) }) + "\n"
      );
    }
  }

  private async dispatch(method: string, params: Record<string, unknown>): Promise<unknown> {
    this.activity.event("agent_rpc", {
      source: "agent",
      message: method,
      data: { method, keys: Object.keys(params) },
    });

    if (method === "agent_status" || method === "status") {
      await this.bridge.ensureStarted();
      return {
        connected: this.bridge.connected,
        device: this.bridge.connectedDevice || null,
        interpreter: this.bridge.interpreter || null,
        rpc: this.path,
        session_id: this.bridge.sessionId,
        activityLog: this.activity.activityPath,
        replLog: this.activity.replPath,
      };
    }
    if (method === "agent_paths") {
      return {
        rpc: this.path,
        session_id: this.bridge.sessionId,
        activityLog: this.activity.activityPath,
        replLog: this.activity.replPath,
        home: this.activity.dir,
      };
    }
    if (method === "connect") {
      const device = String(params.device || "");
      if (!device) {
        throw new Error("device required");
      }
      const res = await this.bridge.connect(device, params.baud as number | undefined);
      return {
        device,
        baud: params.baud ?? 115200,
        interpreter: this.bridge.interpreter || null,
        ...(res && typeof res === "object" ? res : {}),
      };
    }
    if (method === "resume") {
      await this.bridge.resume(params.baud as number | undefined);
      return {
        device: this.bridge.connectedDevice,
        resumed: true,
        interpreter: this.bridge.interpreter || null,
      };
    }
    if (method === "disconnect") {
      await this.bridge.disconnect();
      return { ok: true };
    }
    if (method.startsWith("firmware_")) {
      return this.dispatchFirmware(method.slice("firmware_".length), params);
    }
    return this.bridge.request(method, params);
  }

  /** Host-side firmware engine methods (build/flash never touch the sidecar). */
  private async dispatchFirmware(
    op: string,
    params: Record<string, unknown>
  ): Promise<unknown> {
    const cfg = getConfig();
    const pathArgs: Record<string, string> = {};
    const mp = (params.mp as string) || cfg.micropythonPath;
    if (mp) {
      pathArgs.mp = mp;
    }
    const roots: string[] = [];
    if (cfg.workspacePath) {
      roots.push(cfg.workspacePath);
    }
    for (const f of vscode.workspace.workspaceFolders || []) {
      const p = f.uri.fsPath;
      if (!roots.includes(p)) {
        roots.push(p);
      }
    }
    if (roots.length) {
      pathArgs.workspace = roots.join(path.delimiter);
    }
    if (!pathArgs.mp) {
      for (const root of roots) {
        const nested = path.join(root, "micropython");
        if (
          fs.existsSync(path.join(nested, "ports")) &&
          fs.existsSync(path.join(nested, "py"))
        ) {
          pathArgs.mp = nested;
          break;
        }
        if (
          fs.existsSync(path.join(root, "ports")) &&
          fs.existsSync(path.join(root, "py"))
        ) {
          pathArgs.mp = root;
          break;
        }
      }
    }
    if (cfg.idfPath) {
      pathArgs.idf = cfg.idfPath;
    }
    if (cfg.emsdkPath) {
      pathArgs.emsdk = cfg.emsdkPath;
    }
    const sel = {
      port: (params.port as string) || "",
      board: (params.board as string) || "",
      variant: (params.variant as string) || "",
    };

    switch (op) {
      case "discover":
        return this.firmware.run("discover", pathArgs);
      case "list":
      case "tree":
        return this.firmware.run("tree", pathArgs);
      case "cmods":
        return this.firmware.run("cmods", pathArgs);
      case "flashers":
        return this.firmware.run("flashers");
      case "artifact":
        return this.firmware.run("artifact", { ...pathArgs, ...sel });
      case "build":
      case "clean": {
        const log: string[] = [];
        const handle = this.firmware.stream(
          op === "clean" ? "clean" : "build",
          { ...pathArgs, ...sel, clean: op === "build" ? !!params.clean : undefined },
          (line) => {
            if (log.length < 4000) {
              log.push(line);
            }
          }
        );
        const result = await handle.done;
        return { ...result, log };
      }
      case "flash": {
        const log: string[] = [];
        const handle = this.firmware.stream(
          "flash",
          {
            ...pathArgs,
            ...sel,
            family: (params.family as string) || undefined,
            device: (params.device as string) || "",
            artifact: (params.artifact as string) || undefined,
            erase: !!params.erase,
            esptool: this.firmware.esptoolCommand() || undefined,
          },
          (line) => {
            if (log.length < 4000) {
              log.push(line);
            }
          }
        );
        const result = await handle.done;
        return { ...result, log };
      }
      case "download_tree":
      case "download-tree":
        return this.firmware.run("download-tree", {
          force: params.force ? true : undefined,
        });
      case "download_list":
      case "download-list":
        return this.firmware.run("download-list", {
          board: (params.board as string) || sel.board,
          variant: (params.variant as string) || sel.variant || undefined,
          preview: params.preview ? true : undefined,
          force: params.force ? true : undefined,
        });
      case "download": {
        const log: string[] = [];
        const handle = this.firmware.stream(
          "download",
          {
            board: (params.board as string) || sel.board,
            variant: (params.variant as string) || sel.variant || undefined,
            version: (params.version as string) || undefined,
            preview: params.preview ? true : undefined,
            force: params.force ? true : undefined,
          },
          (line) => {
            if (log.length < 4000) {
              log.push(line);
            }
          }
        );
        const result = await handle.done;
        return { ...result, log };
      }
      case "partitions": {
        const action = (params.action as string) || "get";
        const args: Record<string, string | undefined> = { ...pathArgs, board: sel.board, variant: sel.variant };
        if (params.rows) {
          args.rows = typeof params.rows === "string" ? params.rows : JSON.stringify(params.rows);
        }
        if (params.csvFile) {
          args.csvFile = params.csvFile as string;
        }
        return this.firmware.run("partitions", args, [action]);
      }
      default:
        throw new Error(`unknown firmware op: ${op}`);
    }
  }

  dispose(): void {
    const our = `${HOST}:${this.port}`;
    try {
      this.server?.close();
    } catch {
      /* ignore */
    }
    this.server = undefined;
    this.activity.clearRpcPath();
    const homeDir = path.join(os.homedir(), ".mpftp");
    // Drop our workspace → RPC entries; leave other windows' mappings alone.
    try {
      const registryPath = path.join(homeDir, "workspace-rpc.json");
      const registry = readWorkspaceRpcRegistry(registryPath);
      let changed = false;
      for (const folder of vscode.workspace.workspaceFolders || []) {
        const key = path.resolve(folder.uri.fsPath);
        if (registry[key] === our) {
          delete registry[key];
          changed = true;
        }
      }
      // Also prune any stale entry that still points at this port.
      for (const [key, val] of Object.entries(registry)) {
        if (val === our) {
          delete registry[key];
          changed = true;
        }
      }
      if (changed) {
        fs.writeFileSync(registryPath, JSON.stringify(registry, null, 2) + "\n", "utf8");
      }
    } catch {
      /* ignore */
    }
    // Only remove home rpc.port if it still points at this window — another
    // Cursor window may have become the home fallback.
    for (const f of [path.join(homeDir, "rpc.port"), path.join(homeDir, "rpc.path")]) {
      try {
        const cur = fs.readFileSync(f, "utf8").trim();
        if (cur === our || cur.startsWith(our)) {
          fs.unlinkSync(f);
        }
      } catch {
        /* ignore */
      }
    }
  }
}

function readWorkspaceRpcRegistry(registryPath: string): Record<string, string> {
  try {
    if (!fs.existsSync(registryPath)) {
      return {};
    }
    const raw = JSON.parse(fs.readFileSync(registryPath, "utf8")) as unknown;
    if (!raw || typeof raw !== "object" || Array.isArray(raw)) {
      return {};
    }
    const out: Record<string, string> = {};
    for (const [k, v] of Object.entries(raw as Record<string, unknown>)) {
      if (typeof k === "string" && typeof v === "string" && v.trim()) {
        out[k] = v.trim();
      }
    }
    return out;
  } catch {
    return {};
  }
}
