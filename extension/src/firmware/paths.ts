/**
 * The folder settings the Firmware panel hands the engine, kept free of the
 * vscode API so the ordering can be tested with node:test.
 *
 * The engine (mpftp.firmware) resolves the micropython-pydevices checkout in
 * this order, and these args are how the extension's settings take part:
 *
 *   1. `mpftp.buildSystemPath` -> `--build-system`. Explicit, so a path that
 *      isn't a checkout is an error, never a fallback.
 *   2. `buildSystemPath` in ~/.mpftp/config.json, or MPFTP_BUILD_SYSTEM
 *      (read by the engine itself).
 *   3. `--workspace`: `mpftp.workspacePath` first, then the editor's open
 *      folders, each as the checkout or the folder holding one.
 *   4. Beside the MicroPython tree (`--mp`).
 *   5. ~/micropython-pydevices.
 */

import * as path from "path";

export interface FolderSettings {
  buildSystemPath: string;
  workspacePath: string;
}

export interface FolderArgs {
  /** `--build-system`: only when the setting is set. */
  buildSystem?: string;
  /** `--workspace`: os.pathsep-joined roots, configured workspace first. */
  workspace?: string;
}

/** Roots the engine searches: the configured workspace, then open folders, no repeats. */
export function workspaceRoots(settings: FolderSettings, openFolders: string[]): string[] {
  const roots: string[] = [];
  if (settings.workspacePath) {
    roots.push(settings.workspacePath);
  }
  for (const f of openFolders) {
    if (!roots.includes(f)) {
      roots.push(f);
    }
  }
  return roots;
}

/** The checkout and workspace args for a discover/tree/modules/build call. */
export function folderArgs(settings: FolderSettings, openFolders: string[]): FolderArgs {
  const args: FolderArgs = {};
  const explicit = settings.buildSystemPath.trim();
  if (explicit) {
    args.buildSystem = explicit;
  }
  const roots = workspaceRoots(settings, openFolders);
  if (roots.length) {
    args.workspace = roots.join(path.delimiter);
  }
  return args;
}
