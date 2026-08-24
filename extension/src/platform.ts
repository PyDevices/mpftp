import * as fs from "fs";
import * as os from "os";
import * as path from "path";
import { execFileSync } from "child_process";
import * as vscode from "vscode";

export type HostKind = "wsl" | "linux" | "windows";

export function detectHost(): HostKind {
  if (process.platform === "win32") {
    return "windows";
  }
  if (process.platform === "linux") {
    try {
      const v = fs.readFileSync("/proc/version", "utf8").toLowerCase();
      if (v.includes("microsoft") || v.includes("wsl")) {
        return "wsl";
      }
    } catch {
      /* ignore */
    }
    return "linux";
  }
  return "linux";
}

function existsFile(p: string): boolean {
  try {
    return fs.statSync(p).isFile();
  } catch {
    return false;
  }
}

function which(cmd: string): string | undefined {
  try {
    const out = execFileSync(process.platform === "win32" ? "where" : "which", [cmd], {
      encoding: "utf8",
      timeout: 3000,
    })
      .split(/\r?\n/)
      .map((s) => s.trim())
      .filter(Boolean)[0];
    return out;
  } catch {
    return undefined;
  }
}

/** Resolve a Python that can import mpremote and open the host's serial ports. */
export function resolvePython(extensionPath: string, configured?: string): string {
  if (configured && configured.trim()) {
    return configured.trim();
  }

  const host = detectHost();
  const candidates: string[] = [];

  if (host === "wsl") {
    // Windows Python sees COM ports; WSL Python typically does not.
    const winCandidates = [
      which("python.exe"),
      path.join(os.homedir(), "bin", "python.exe"),
      ...discoverWindowsPythons(),
      "/mnt/c/Windows/py.exe",
    ].filter(Boolean) as string[];
    candidates.push(...winCandidates);
    // Fall back to extension venv (Linux serial / usbipd devices).
    candidates.push(path.join(extensionPath, ".venv", "bin", "python"));
    candidates.push(which("python3") || "", which("python") || "");
  } else if (host === "windows") {
    candidates.push(
      which("python.exe") || "",
      which("python") || "",
      which("py") || "",
      path.join(extensionPath, ".venv", "Scripts", "python.exe")
    );
  } else {
    candidates.push(
      path.join(extensionPath, ".venv", "bin", "python"),
      which("python3") || "",
      which("python") || ""
    );
  }

  for (const c of candidates) {
    if (c && existsFile(c)) {
      if (canImportMpremote(c)) {
        return c;
      }
    }
  }

  // Last resort: return best guess even if import check failed (user may fix env).
  for (const c of candidates) {
    if (c && existsFile(c)) {
      return c;
    }
  }
  return host === "windows" ? "python" : "python3";
}

function discoverWindowsPythons(): string[] {
  const found: string[] = [];
  const usersDir = "/mnt/c/Users";
  try {
    for (const user of fs.readdirSync(usersDir)) {
      if (user === "Public" || user === "Default" || user.startsWith(".")) {
        continue;
      }
      const bases = [
        path.join(usersDir, user, "AppData", "Local", "Programs", "Python"),
        path.join(usersDir, user, "AppData", "Roaming", "Python"),
      ];
      for (const base of bases) {
        if (!fs.existsSync(base)) {
          continue;
        }
        try {
          for (const ent of fs.readdirSync(base)) {
            const py = path.join(base, ent, "python.exe");
            if (existsFile(py)) {
              found.push(py);
            }
            // Roaming layout: Python314/site-packages — interpreter is under Local
          }
        } catch {
          /* ignore */
        }
      }
    }
  } catch {
    /* ignore */
  }
  // Prefer newer version-looking paths last → reverse sort by name
  return found.sort().reverse();
}

function canImportMpremote(python: string): boolean {
  try {
    execFileSync(python, ["-c", "import mpremote, serial; print('ok')"], {
      encoding: "utf8",
      timeout: 15000,
      stdio: ["ignore", "pipe", "pipe"],
    });
    return true;
  } catch {
    return false;
  }
}

/** True when `python` is a Windows interpreter (needs Windows-style paths on WSL). */
export function isWindowsPython(python: string): boolean {
  const p = python.toLowerCase();
  return p.endsWith(".exe") || p.includes("/mnt/c/") || /^[a-z]:\\/.test(python);
}

/**
 * Env vars a Windows child spawned from WSL silently does not receive unless
 * named in WSLENV — MICROPYPATH is the reported case (mpftp#12): a Windows
 * micropython/mpremote falls back to its own default lib path with no error,
 * which reads as a mysteriously stale install rather than a missed env var.
 */
const WSLENV_FORWARD_VARS = ["MICROPYPATH"];

/**
 * On WSL, spawning a Windows binary only forwards env vars listed in
 * WSLENV (each with a translation flag) — plain `env:` on `spawn()` is not
 * enough. Merge in the vars mpftp cares about that are actually set, so a
 * Windows-side interpreter sees them instead of quietly using its own
 * defaults. A no-op off WSL or for a non-Windows interpreter.
 */
export function withWslenvForwarded(
  python: string,
  env: NodeJS.ProcessEnv
): NodeJS.ProcessEnv {
  if (detectHost() !== "wsl" || !isWindowsPython(python)) {
    return env;
  }
  const toForward = WSLENV_FORWARD_VARS.filter((name) => env[name] !== undefined);
  if (toForward.length === 0) {
    return env;
  }
  const existing = (env.WSLENV || "")
    .split(":")
    .map((e) => e.trim())
    .filter(Boolean);
  const already = new Set(existing.map((e) => e.split("/")[0]));
  const additions = toForward.filter((name) => !already.has(name)).map((name) => `${name}/l`);
  if (additions.length === 0) {
    return env;
  }
  return { ...env, WSLENV: [...existing, ...additions].join(":") };
}

/**
 * Convert a WSL/Linux path into a form Windows python.exe can open.
 * Without this, Node spawn often passes `/home/...` and Windows Python
 * mis-resolves it as `C:\home\...` (ENOENT).
 */
export function pathForPythonProcess(python: string, filePath: string): string {
  if (detectHost() !== "wsl" || !isWindowsPython(python)) {
    return filePath;
  }
  if (/^[a-zA-Z]:[\\/]/.test(filePath) || filePath.startsWith("\\\\")) {
    return filePath;
  }
  try {
    const win = execFileSync("wslpath", ["-w", filePath], {
      encoding: "utf8",
      timeout: 3000,
    }).trim();
    if (win) {
      return win;
    }
  } catch {
    /* fall through */
  }
  const distro = process.env.WSL_DISTRO_NAME || "Ubuntu";
  return `\\\\wsl.localhost\\${distro}${filePath.replace(/\//g, "\\")}`;
}

/** Scan WSL-mounted Windows user profiles for mpremote.exe under Scripts. */
function discoverWindowsMpremote(): string[] {
  const found: string[] = [];
  const usersDir = "/mnt/c/Users";
  try {
    for (const user of fs.readdirSync(usersDir)) {
      if (user === "Public" || user === "Default" || user.startsWith(".")) {
        continue;
      }
      const roaming = path.join(usersDir, user, "AppData", "Roaming", "Python");
      if (!fs.existsSync(roaming)) {
        continue;
      }
      try {
        for (const ent of fs.readdirSync(roaming)) {
          const exe = path.join(roaming, ent, "Scripts", "mpremote.exe");
          if (existsFile(exe)) {
            found.push(exe);
          }
        }
      } catch {
        /* ignore */
      }
    }
  } catch {
    /* ignore */
  }
  return found.sort().reverse();
}

export function resolveMpremoteCli(configured?: string): string | undefined {
  if (configured && configured.trim()) {
    return configured.trim();
  }
  const host = detectHost();
  if (host === "wsl") {
    return which("mpremote.exe") || discoverWindowsMpremote()[0] || which("mpremote");
  }
  if (host === "windows") {
    return which("mpremote.exe") || which("mpremote");
  }
  return which("mpremote");
}

/**
 * Resolve a Python that can run the firmware engine and drive `make`.
 * Unlike {@link resolvePython} (which prefers Windows python for COM ports),
 * builds must run under the native Linux/host toolchain, so this prefers a
 * Linux python3 on WSL. The engine itself is stdlib-only.
 */
export function resolveBuildPython(extensionPath: string, configured?: string): string {
  if (configured && configured.trim()) {
    return configured.trim();
  }
  const host = detectHost();
  const candidates: string[] = [];
  if (host === "windows") {
    candidates.push(
      which("python") || "",
      which("py") || "",
      path.join(extensionPath, ".venv", "Scripts", "python.exe")
    );
  } else {
    // wsl + linux: native Linux python3 (make/idf must run in Linux).
    candidates.push(
      which("python3") || "",
      which("python") || "",
      path.join(extensionPath, ".venv", "bin", "python")
    );
  }
  for (const c of candidates) {
    if (c && existsFile(c)) {
      return c;
    }
  }
  return host === "windows" ? "python" : "python3";
}

export function getConfig() {
  const cfg = vscode.workspace.getConfiguration("mpftp");
  return {
    pythonPath: cfg.get<string>("pythonPath") || "",
    mpremotePath: cfg.get<string>("mpremotePath") || "",
    defaultBaud: cfg.get<number>("defaultBaud") || 115200,
    autoConnectDevice: cfg.get<string>("autoConnectDevice") || "",
    verifyTransfers: cfg.get<boolean>("verifyTransfers") !== false,
    compileOnUpload: cfg.get<boolean>("compileOnUpload") === true,
    autoReconnectAfterReset: cfg.get<boolean>("autoReconnectAfterReset") !== false,
    openEditorOnConnect: cfg.get<boolean>("openEditorOnConnect") !== false,
    micropythonPath: cfg.get<string>("micropythonPath") || "",
    workspacePath: cfg.get<string>("workspacePath") || "",
    idfPath: cfg.get<string>("idfPath") || "",
    emsdkPath: cfg.get<string>("emsdkPath") || "",
    toolchainBins: cfg.get<string[]>("toolchainBins") || [],
    buildPythonPath: cfg.get<string>("buildPythonPath") || "",
    esptoolCommand: cfg.get<string>("esptoolCommand") || "",
  };
}

/** globalState keys for last successful connect (not user settings). */
export const GS_LAST_DEVICE = "mpftp.lastDevice";
export const GS_LAST_VIDPID = "mpftp.lastVidPid";

export type PortPickInfo = {
  device: string;
  vid?: number | null;
  pid?: number | null;
  repl?: boolean;
  interface?: string | null;
  description?: string | null;
  product?: string | null;
  manufacturer?: string | null;
  serial_number?: string | null;
  hwid?: string | null;
};

export function portVidPidKey(p: PortPickInfo): string | undefined {
  if (p.vid == null || p.pid == null) {
    return undefined;
  }
  return `${p.vid}:${p.pid}`;
}

/** CircuitPython CDC2 and other non-REPL interfaces are marked repl=false. */
export function isReplPort(p: PortPickInfo): boolean {
  return p.repl !== false;
}

/** Drop non-REPL ports and prefer last-good device / VID:PID. */
export function filterAndSortPorts<T extends PortPickInfo>(
  ports: T[],
  opts: { lastDevice?: string; lastVidPid?: string } = {}
): T[] {
  const repl = ports.filter(isReplPort);
  const score = (p: T): number => {
    if (opts.lastDevice && p.device === opts.lastDevice) {
      return 0;
    }
    const key = portVidPidKey(p);
    if (opts.lastVidPid && key === opts.lastVidPid) {
      return 1;
    }
    return 2;
  };
  return [...repl].sort(
    (a, b) => score(a) - score(b) || (a.device || "").localeCompare(b.device || "")
  );
}
