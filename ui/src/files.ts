import type { Rpc } from "./rpc";

interface Entry {
  name: string;
  isDir: boolean;
  size: number;
}

function bufferToBase64(buf: ArrayBuffer): string {
  const bytes = new Uint8Array(buf);
  let binary = "";
  for (const b of bytes) {
    binary += String.fromCharCode(b);
  }
  return btoa(binary);
}

function base64ToBytes(b64: string): Uint8Array {
  const binary = atob(b64);
  const bytes = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i++) {
    bytes[i] = binary.charCodeAt(i);
  }
  return bytes;
}

function joinPath(dir: string, name: string): string {
  return dir === "/" ? `/${name}` : `${dir}/${name}`;
}

function parentPath(dir: string): string {
  if (dir === "/" || dir === "") {
    return "/";
  }
  const trimmed = dir.replace(/\/$/, "");
  const idx = trimmed.lastIndexOf("/");
  return idx <= 0 ? "/" : trimmed.slice(0, idx);
}

function el<T extends HTMLElement>(id: string): T {
  const found = document.getElementById(id);
  if (!found) {
    throw new Error(`missing #${id}`);
  }
  return found as T;
}

export class Files {
  private rpc: Rpc;
  private path = "/";
  private pathEl = el<HTMLElement>("files-path");
  private listEl = el<HTMLUListElement>("files-list");
  private onOpenFile: (path: string, content: string) => void;

  constructor(rpc: Rpc, opts: { onOpenFile: (path: string, content: string) => void }) {
    this.rpc = rpc;
    this.onOpenFile = opts.onOpenFile;

    document.querySelector('[data-action="up"]')!.addEventListener("click", () => {
      this.navigate(parentPath(this.path));
    });
    document.querySelector('[data-action="mkdir"]')!.addEventListener("click", () => {
      void this.mkdir();
    });
    const input = el<HTMLInputElement>("upload-input");
    input.addEventListener("change", () => {
      void this.uploadFiles(input.files);
      input.value = "";
    });
  }

  async refresh(): Promise<void> {
    this.pathEl.textContent = this.path;
    this.listEl.innerHTML = '<li class="mp-files-loading">Loading…</li>';
    try {
      const entries: Entry[] = await this.rpc.call("fs_listdir", { path: this.path });
      this.listEl.innerHTML = "";
      for (const entry of entries) {
        this.listEl.appendChild(this.renderEntry(entry));
      }
      if (entries.length === 0) {
        this.listEl.innerHTML = '<li class="mp-files-empty">Empty</li>';
      }
    } catch (e: any) {
      this.listEl.innerHTML = `<li class="mp-files-error">${e.message}</li>`;
    }
  }

  private renderEntry(entry: Entry): HTMLElement {
    const li = document.createElement("li");
    li.className = "mp-files-entry";
    const name = document.createElement("span");
    name.className = "mp-files-name";
    name.textContent = (entry.isDir ? "📁 " : "📄 ") + entry.name;
    name.addEventListener("click", () => {
      if (entry.isDir) {
        this.navigate(joinPath(this.path, entry.name));
      } else {
        void this.openInEditor(entry);
      }
    });
    const size = document.createElement("span");
    size.className = "mp-files-size";
    size.textContent = entry.isDir ? "" : String(entry.size);
    li.append(name, size);
    if (!entry.isDir) {
      const download = document.createElement("button");
      download.className = "mp-btn mp-files-action";
      download.textContent = "⬇";
      download.title = "Download";
      download.addEventListener("click", (ev) => {
        ev.stopPropagation();
        void this.download(entry);
      });
      li.append(download);
    }
    const del = document.createElement("button");
    del.className = "mp-btn mp-files-action";
    del.textContent = "✕";
    del.title = "Delete";
    del.addEventListener("click", (ev) => {
      ev.stopPropagation();
      void this.delete(entry);
    });
    li.append(del);
    return li;
  }

  private navigate(path: string): void {
    this.path = path;
    void this.refresh();
  }

  private async openInEditor(entry: Entry): Promise<void> {
    const remote = joinPath(this.path, entry.name);
    const res = await this.rpc.call("fs_read", { path: remote });
    const bytes = base64ToBytes(res.data_b64);
    const text = new TextDecoder("utf-8", { fatal: false }).decode(bytes);
    this.onOpenFile(remote, text);
  }

  private async download(entry: Entry): Promise<void> {
    const remote = joinPath(this.path, entry.name);
    const res = await this.rpc.call("fs_read", { path: remote });
    const bytes = base64ToBytes(res.data_b64);
    const blob = new Blob([bytes.buffer as ArrayBuffer]);
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = entry.name;
    a.click();
    URL.revokeObjectURL(url);
  }

  private async uploadFiles(files: FileList | null): Promise<void> {
    if (!files || files.length === 0) {
      return;
    }
    for (const file of Array.from(files)) {
      const buf = await file.arrayBuffer();
      const remote = joinPath(this.path, file.name);
      await this.rpc.call("fs_write", { path: remote, data_b64: bufferToBase64(buf) });
    }
    await this.refresh();
  }

  private async mkdir(): Promise<void> {
    const name = prompt("New folder name:");
    if (!name) {
      return;
    }
    await this.rpc.call("fs_mkdir", { path: joinPath(this.path, name) });
    await this.refresh();
  }

  private async delete(entry: Entry): Promise<void> {
    const remote = joinPath(this.path, entry.name);
    if (!confirm(`Delete ${remote}?`)) {
      return;
    }
    await this.rpc.call(entry.isDir ? "fs_rm_rf" : "fs_rm", { path: remote });
    await this.refresh();
  }
}
