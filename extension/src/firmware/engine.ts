import { ChildProcess, spawn } from "child_process";
import * as path from "path";
import {
  detectHost,
  getConfig,
  resolveBuildPython,
  resolvePython,
} from "../platform";

export type EngineArgs = Record<string, string | boolean | number | undefined>;

export interface StreamHandle {
  proc: ChildProcess;
  /** Resolves with the final {"type":"result",...} record (ok true/false). */
  done: Promise<Record<string, unknown>>;
  cancel(): void;
}

/**
 * Host-side driver for python/firmware_engine.py. Builds run under a native
 * (Linux on WSL) python; flashing esp32 COM ports on WSL reuses the sidecar's
 * Windows python resolution so esptool can see the port.
 */
export class FirmwareEngine {
  constructor(private readonly extensionPath: string) {}

  private script(): string {
    return path.join(this.extensionPath, "python", "firmware_engine.py");
  }

  private buildPython(): string {
    return resolveBuildPython(this.extensionPath, getConfig().buildPythonPath);
  }

  /** esptool interpreter for the current host (Windows python on WSL for COM). */
  esptoolCommand(): string {
    const cfg = getConfig();
    if (cfg.esptoolCommand) {
      return cfg.esptoolCommand;
    }
    if (detectHost() === "wsl") {
      // Same resolution the sidecar uses to reach COM ports.
      return resolvePython(this.extensionPath, cfg.pythonPath);
    }
    return "";
  }

  private toArgv(cmd: string, args: EngineArgs): string[] {
    const argv = [cmd];
    for (const [k, v] of Object.entries(args)) {
      if (v === undefined || v === "" || v === false) {
        continue;
      }
      const flag = "--" + k.replace(/[A-Z]/g, (m) => "-" + m.toLowerCase());
      if (v === true) {
        argv.push(flag);
      } else {
        argv.push(flag, String(v));
      }
    }
    return argv;
  }

  /** Positional subcommand form (e.g. partitions get/set/reset). */
  private toArgvPositional(cmd: string, positional: string[], args: EngineArgs): string[] {
    const argv = [cmd, ...positional];
    for (const [k, v] of Object.entries(args)) {
      if (v === undefined || v === "" || v === false) {
        continue;
      }
      const flag = "--" + k.replace(/[A-Z]/g, (m) => "-" + m.toLowerCase());
      if (v === true) {
        argv.push(flag);
      } else {
        argv.push(flag, String(v));
      }
    }
    return argv;
  }

  /** Single-shot JSON command (discover/tree/cmods/artifact/partitions/...). */
  async run<T = Record<string, unknown>>(
    cmd: string,
    args: EngineArgs = {},
    positional: string[] = []
  ): Promise<T> {
    const argv = positional.length
      ? this.toArgvPositional(cmd, positional, args)
      : this.toArgv(cmd, args);
    return new Promise<T>((resolve, reject) => {
      const proc = spawn(this.buildPython(), [this.script(), ...argv], {
        env: { ...process.env, PYTHONUNBUFFERED: "1" },
        windowsHide: true,
      });
      let out = "";
      let err = "";
      proc.stdout.setEncoding("utf8");
      proc.stderr.setEncoding("utf8");
      proc.stdout.on("data", (d: string) => (out += d));
      proc.stderr.on("data", (d: string) => (err += d));
      proc.on("error", reject);
      proc.on("close", (code) => {
        try {
          resolve(JSON.parse(out) as T);
        } catch {
          reject(new Error(err.trim() || out.trim() || `engine exited ${code}`));
        }
      });
    });
  }

  /** Streaming command (build/clean/flash): forwards log lines, resolves result. */
  stream(
    cmd: string,
    args: EngineArgs,
    onLog: (line: string) => void,
    onPhase?: (state: string, text: string) => void
  ): StreamHandle {
    const argv = this.toArgv(cmd, args);
    const proc = spawn(this.buildPython(), [this.script(), ...argv], {
      env: { ...process.env, PYTHONUNBUFFERED: "1" },
      detached: true, // own process group so cancel kills make/esptool too
      windowsHide: true, // Windows: detached otherwise opens an empty console
    });
    proc.stdout.setEncoding("utf8");
    proc.stderr.setEncoding("utf8");

    let buf = "";
    let result: Record<string, unknown> | undefined;
    const handleLine = (line: string) => {
      const t = line.trim();
      if (!t) {
        return;
      }
      try {
        const msg = JSON.parse(t) as Record<string, unknown>;
        if (msg.type === "log") {
          onLog(String(msg.line ?? ""));
        } else if (msg.type === "phase") {
          onPhase?.(String(msg.state ?? ""), String(msg.text ?? ""));
        } else if (msg.type === "result") {
          result = msg;
        }
      } catch {
        onLog(line);
      }
    };
    proc.stdout.on("data", (chunk: string) => {
      buf += chunk;
      let idx: number;
      while ((idx = buf.indexOf("\n")) >= 0) {
        handleLine(buf.slice(0, idx));
        buf = buf.slice(idx + 1);
      }
    });
    proc.stderr.on("data", (chunk: string) => onLog(chunk.replace(/\n$/, "")));

    const done = new Promise<Record<string, unknown>>((resolve) => {
      proc.on("close", (code) => {
        if (buf.trim()) {
          handleLine(buf);
        }
        resolve(result ?? { ok: code === 0, returncode: code });
      });
      proc.on("error", (e) => resolve({ ok: false, error: String(e) }));
    });

    const cancel = () => {
      try {
        if (proc.pid) {
          process.kill(-proc.pid, "SIGTERM");
        }
      } catch {
        try {
          proc.kill("SIGTERM");
        } catch {
          /* ignore */
        }
      }
    };

    return { proc, done, cancel };
  }
}
