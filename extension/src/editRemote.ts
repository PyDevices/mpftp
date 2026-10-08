import * as fs from "fs";
import * as os from "os";
import * as path from "path";
import * as vscode from "vscode";
import { SidecarBridge } from "./bridge/SidecarBridge";
import {
  Side,
  defaultSaveAsPath,
  nameSelection,
  parentOf,
  planSaveAs,
  rebindAfterSaveAs,
  remoteBaseName,
  resolveSaveAsTarget,
  tempPathFor,
} from "./saveAs";

/** A board file's temp copy (its fsPath) → the board path its saves push to. */
const editBindings = new Map<string, string>();

function editRoot(): string {
  return path.join(os.tmpdir(), "mpftp-edit");
}

/**
 * Pull a board file into a temp buffer, open it in the editor, and push on save.
 */
export async function openBoardFileInEditor(
  bridge: SidecarBridge,
  remotePath: string,
  log?: vscode.OutputChannel
): Promise<void> {
  if (!bridge.connected) {
    throw new Error("not connected");
  }
  await bridge.request("fs_touch", { path: remotePath });
  const res = await bridge.request<{ data_b64: string }>("edit_pull", { path: remotePath });
  const data = Buffer.from(res.data_b64, "base64");

  const local = tempPathFor(remotePath, editRoot());
  fs.mkdirSync(path.dirname(local), { recursive: true });
  fs.writeFileSync(local, data);

  const doc = await vscode.workspace.openTextDocument(vscode.Uri.file(local));
  await vscode.window.showTextDocument(doc, { preview: false });
  editBindings.set(doc.uri.fsPath, remotePath);
  log?.appendLine(`[edit] opened ${remotePath} → ${local}`);
}

export function registerEditSaveHook(
  bridge: SidecarBridge,
  context: vscode.ExtensionContext,
  log?: vscode.OutputChannel
): void {
  context.subscriptions.push(
    vscode.workspace.onDidSaveTextDocument(async (doc) => {
      const remote = editBindings.get(doc.uri.fsPath);
      if (!remote) {
        return;
      }
      if (!bridge.connected) {
        void vscode.window.showErrorMessage(`mpftp: not connected — cannot save ${remote}`);
        return;
      }
      try {
        const data = fs.readFileSync(doc.uri.fsPath);
        await bridge.request("edit_push", {
          path: remote,
          data_b64: data.toString("base64"),
        });
        log?.appendLine(`[edit] saved ${remote} (${data.length} bytes)`);
        void vscode.window.setStatusBarMessage(`mpftp: saved ${remote}`, 2500);
      } catch (e: any) {
        void vscode.window.showErrorMessage(`mpftp save failed: ${e?.message || e}`);
      }
    })
  );
}

async function remoteStat(bridge: SidecarBridge, p: string): Promise<{ isDir?: boolean } | undefined> {
  try {
    return await bridge.request<{ isDir?: boolean }>("fs_stat", { path: p });
  } catch {
    return undefined;
  }
}

/**
 * mpftp: Save As… — the editor's text to this computer or the board, under a
 * new name or folder. The editor then shows the new copy, so the next Save
 * goes there; the original is left as it was. VS Code's own Save As is
 * untouched (it only reaches this computer).
 */
export async function saveAs(
  bridge: SidecarBridge,
  panel: { folders(): { local: string; remote: string }; refresh(): Promise<void> },
  log?: vscode.OutputChannel
): Promise<void> {
  const editor = vscode.window.activeTextEditor;
  if (!editor) {
    void vscode.window.showWarningMessage("mpftp: open a file in the editor first");
    return;
  }
  const doc = editor.document;
  const sourceKey = doc.uri.scheme === "file" ? doc.uri.fsPath : undefined;
  const sourceRemote = sourceKey !== undefined ? editBindings.get(sourceKey) : undefined;
  const sourceSide: Side = sourceRemote ? "remote" : "local";
  const name = sourceRemote ? remoteBaseName(sourceRemote) : path.basename(doc.fileName);
  const folders = panel.folders();

  let side: Side = "local";
  if (bridge.connected) {
    const items: Array<vscode.QuickPickItem & { side: Side }> = [
      { label: "$(device-desktop) This computer", description: folders.local, side: "local" },
      { label: "$(circuit-board) Board", description: folders.remote || "/", side: "remote" },
    ];
    if (sourceSide === "remote") {
      items.reverse();
    }
    const picked = await vscode.window.showQuickPick(items, {
      title: `Save ${name} as`,
      placeHolder: "Where to save",
    });
    if (!picked) {
      return;
    }
    side = picked.side;
  }

  const folder = side === "local" ? folders.local : folders.remote || "/";
  const start = defaultSaveAsPath(side, folder, name);
  const typed = await vscode.window.showInputBox({
    title: `Save ${name} as`,
    prompt: side === "remote" ? "Path on the board" : "Path on this computer",
    value: start,
    valueSelection: nameSelection(start, name),
    validateInput: (value) => {
      try {
        resolveSaveAsTarget(side, value, folder, os.homedir());
        return undefined;
      } catch (e: any) {
        return String(e?.message || e);
      }
    },
  });
  if (typed === undefined) {
    return;
  }

  try {
    const dest = resolveSaveAsTarget(side, typed, folder, os.homedir());
    const isSource = side === "remote" ? dest === sourceRemote : !sourceRemote && dest === sourceKey;
    if (isSource) {
      // Save As onto itself is a Save (the save hook pushes a board file).
      await doc.save();
      return;
    }

    const parent = parentOf(side, dest);
    let exists: boolean;
    if (side === "remote") {
      if (!bridge.connected) {
        throw new Error("not connected");
      }
      const dir = await remoteStat(bridge, parent);
      if (!dir?.isDir) {
        throw new Error(`folder doesn't exist on the board: ${parent}`);
      }
      const st = await remoteStat(bridge, dest);
      if (st?.isDir) {
        throw new Error(`${dest} is a folder on the board`);
      }
      exists = st !== undefined;
    } else {
      if (!fs.existsSync(parent) || !fs.statSync(parent).isDirectory()) {
        throw new Error(`folder doesn't exist: ${parent}`);
      }
      exists = fs.existsSync(dest);
      if (exists && fs.statSync(dest).isDirectory()) {
        throw new Error(`${dest} is a folder`);
      }
    }
    if (exists) {
      const where = side === "remote" ? "on the board" : "on this computer";
      const answer = await vscode.window.showWarningMessage(
        `${dest} already exists ${where}. Replace it?`,
        { modal: true },
        "Replace"
      );
      if (answer !== "Replace") {
        return;
      }
    }

    const data = Buffer.from(doc.getText(), "utf8");
    if (side === "remote") {
      await bridge.request("edit_push", { path: dest, data_b64: data.toString("base64") });
    }
    const plan = planSaveAs(side, dest, editRoot());
    fs.mkdirSync(path.dirname(plan.openLocal), { recursive: true });
    fs.writeFileSync(plan.openLocal, data);

    // The tab moves to the new copy: close the old one without touching its
    // file (revert, then close), and open the new one in the same place.
    const column = editor.viewColumn;
    await vscode.window.showTextDocument(doc, { viewColumn: column, preview: false });
    await vscode.commands.executeCommand("workbench.action.revertAndCloseActiveEditor");
    const newDoc = await vscode.workspace.openTextDocument(vscode.Uri.file(plan.openLocal));
    await vscode.window.showTextDocument(newDoc, { viewColumn: column, preview: false });
    rebindAfterSaveAs(editBindings, sourceKey, newDoc.uri.fsPath, plan.pushRemote);

    const label = side === "remote" ? `board ${dest}` : dest;
    log?.appendLine(`[edit] saved as ${label} (${data.length} bytes)`);
    void vscode.window.setStatusBarMessage(`mpftp: saved as ${label}`, 2500);
    await panel.refresh();
  } catch (e: any) {
    void vscode.window.showErrorMessage(`mpftp Save As failed: ${e?.message || e}`);
  }
}
