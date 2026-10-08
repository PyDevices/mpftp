/**
 * Save As for the editor, without the VS Code API, so it can be tested
 * on its own. The browser interface's host (cli/src/mpftp/panel.py,
 * save_file_as) follows the same rules.
 */
import * as path from "path";

/** Where a file lives: this computer, or the board. */
export type Side = "local" | "remote";

/** The temp file a board file is edited through: the board path mirrored under `root`. */
export function tempPathFor(remotePath: string, root: string): string {
  // Mirror the remote path so the editor tab shows the real basename
  // (/main.py → …/mpftp-edit/main.py), not a flattened _main.py.
  const parts = remotePath
    .replace(/\\/g, "/")
    .split("/")
    .filter((p) => p.length > 0)
    .map((p) => p.replace(/[:<>"|?*]/g, "_"));
  if (!parts.length) {
    throw new Error(`invalid remote path: ${remotePath}`);
  }
  return path.join(root, ...parts);
}

export function joinRemote(base: string, name: string): string {
  if (!base || base === "/") {
    return "/" + name.replace(/^\/+/, "");
  }
  return base.replace(/\/+$/, "") + "/" + name;
}

/** An absolute board path: relative ones start at `base`; `.`, `..` and extra slashes fold away. */
export function normalizeRemote(p: string, base = "/"): string {
  let full = p.replace(/\\/g, "/");
  if (!full.startsWith("/")) {
    full = joinRemote(base || "/", full);
  }
  const parts: string[] = [];
  for (const part of full.split("/")) {
    if (part === "" || part === ".") {
      continue;
    }
    if (part === "..") {
      parts.pop();
      continue;
    }
    parts.push(part);
  }
  return "/" + parts.join("/");
}

export function remoteBaseName(remotePath: string): string {
  const parts = remotePath.split("/").filter(Boolean);
  return parts.length ? parts[parts.length - 1] : remotePath;
}

/** The path a Save As box starts with: that list's current folder plus the file's name. */
export function defaultSaveAsPath(side: Side, folder: string, name: string): string {
  return side === "local" ? path.join(folder, name) : joinRemote(folder || "/", name);
}

/** The selection a Save As box starts with: the name, without its extension. */
export function nameSelection(value: string, name: string): [number, number] {
  const start = Math.max(0, value.length - name.length);
  const dot = name.lastIndexOf(".");
  return [start, dot > 0 ? start + dot : value.length];
}

/**
 * Where a typed Save As path writes. A relative path starts from that list's
 * folder; a path ending in a separator names a folder, not a file, and is
 * refused.
 */
export function resolveSaveAsTarget(side: Side, input: string, folder: string, home = ""): string {
  const typed = input.trim();
  if (!typed || /[\\/]$/.test(typed)) {
    throw new Error("give the file a name, not just a folder");
  }
  if (side === "remote") {
    const dest = normalizeRemote(typed, folder);
    if (dest === "/") {
      throw new Error("give the file a name, not just a folder");
    }
    return dest;
  }
  let local = typed;
  if (home && (local === "~" || local.startsWith("~/") || local.startsWith("~\\"))) {
    local = path.join(home, local.slice(1));
  }
  return path.resolve(folder, local);
}

/** The folder a Save As target goes in, on its side. */
export function parentOf(side: Side, dest: string): string {
  if (side === "remote") {
    const cut = dest.lastIndexOf("/");
    return cut > 0 ? dest.slice(0, cut) : "/";
  }
  return path.dirname(dest);
}

/** What the editor does after a Save As: which file to open, and the board path it pushes to. */
export interface SaveAsPlan {
  /** The local file the editor shows from now on. */
  openLocal: string;
  /** Set when that file is a board file's temp copy: where its saves go. */
  pushRemote?: string;
}

export function planSaveAs(side: Side, dest: string, tempRoot: string): SaveAsPlan {
  return side === "remote" ? { openLocal: tempPathFor(dest, tempRoot), pushRemote: dest } : { openLocal: dest };
}

/**
 * Move a temp-file binding after a Save As. The old document stops pushing
 * (it is closed, and the board original stays as it was). The new one pushes
 * to its board path, or, saved to this computer, is a plain local file
 * that pushes nowhere, even if it lands on a path that used to be bound.
 */
export function rebindAfterSaveAs(
  bindings: Map<string, string>,
  oldKey: string | undefined,
  newKey: string,
  pushRemote: string | undefined
): void {
  if (oldKey !== undefined) {
    bindings.delete(oldKey);
  }
  if (pushRemote) {
    bindings.set(newKey, pushRemote);
  } else {
    bindings.delete(newKey);
  }
}
