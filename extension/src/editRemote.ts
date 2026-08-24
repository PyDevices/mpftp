import * as fs from "fs";
import * as os from "os";
import * as path from "path";
import * as vscode from "vscode";
import { SidecarBridge } from "./bridge/SidecarBridge";

const editBindings = new Map<string, string>();

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

  const dir = path.join(os.tmpdir(), "mpftp-edit");
  // Mirror remote path under the temp dir so the editor tab shows the real
  // basename (e.g. /main.py → …/mpftp-edit/main.py), not a flattened _main.py.
  const parts = remotePath
    .replace(/\\/g, "/")
    .split("/")
    .filter((p) => p.length > 0)
    .map((p) => p.replace(/[:<>"|?*]/g, "_"));
  if (!parts.length) {
    throw new Error(`invalid remote path: ${remotePath}`);
  }
  const local = path.join(dir, ...parts);
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
