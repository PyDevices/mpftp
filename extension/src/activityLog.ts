import * as fs from "fs";
import * as os from "os";
import * as path from "path";

export type ActivityEvent = {
  ts: string;
  source: "extension" | "agent" | "sidecar" | "repl";
  kind: string;
  message?: string;
  data?: Record<string, unknown>;
};

/**
 * NDJSON activity log for Cursor agents (and humans) to watch mpftp.
 * Lives only under ~/.mpftp — never creates files inside the open workspace.
 */
export class ActivityLog {
  readonly dir: string;
  readonly activityPath: string;
  readonly replPath: string;
  readonly rpcPathFile: string;

  constructor() {
    this.dir = path.join(os.homedir(), ".mpftp");
    fs.mkdirSync(this.dir, { recursive: true, mode: 0o700 });
    this.activityPath = path.join(this.dir, "activity.log");
    this.replPath = path.join(this.dir, "repl.log");
    this.rpcPathFile = path.join(this.dir, "rpc.path");
  }

  event(
    kind: string,
    opts: {
      source?: ActivityEvent["source"];
      message?: string;
      data?: Record<string, unknown>;
    } = {}
  ): void {
    const entry: ActivityEvent = {
      ts: new Date().toISOString(),
      source: opts.source || "extension",
      kind,
      message: opts.message,
      data: opts.data,
    };
    const line = JSON.stringify(entry) + "\n";
    try {
      fs.appendFileSync(this.activityPath, line, { encoding: "utf8" });
    } catch {
      /* ignore */
    }
  }

  /** Append raw REPL bytes (as utf-8 with replacement) for agent tailing. */
  appendRepl(text: string): void {
    try {
      fs.appendFileSync(this.replPath, text, { encoding: "utf8" });
    } catch {
      /* ignore */
    }
  }

  writeRpcPath(socketPath: string): void {
    try {
      fs.writeFileSync(this.rpcPathFile, socketPath + "\n", { encoding: "utf8" });
    } catch {
      /* ignore */
    }
  }

  clearRpcPath(): void {
    try {
      fs.unlinkSync(this.rpcPathFile);
    } catch {
      /* ignore */
    }
  }
}

/** Redact bulky fields from RPC params for the activity log. */
export function summarizeParams(method: string, params: Record<string, unknown>): Record<string, unknown> {
  const out: Record<string, unknown> = { ...params };
  if (typeof out.data_b64 === "string") {
    const b64 = out.data_b64 as string;
    out.data_b64 = `<${b64.length} chars b64>`;
    out.bytes_approx = Math.floor((b64.length * 3) / 4);
  }
  if (typeof out.source === "string" && (out.source as string).length > 200) {
    out.source = `<script ${(out.source as string).length} chars>`;
  }
  if (typeof out.code === "string" && (out.code as string).length > 200) {
    out.code = `<code ${(out.code as string).length} chars>`;
  }
  if (method) {
    out._method = method;
  }
  return out;
}
