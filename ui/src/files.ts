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

export class Files {
  private rpc: Rpc;
  private root: HTMLElement;
  private path = "/";

  constructor(container: HTMLElement, rpc: Rpc) {
    this.rpc = rpc;
    this.root = container;
    this.root.innerHTML = `
      <div class="files-toolbar">
        <button data-action="up" title="Up one level">⬆</button>
        <span class="files-path"></span>
        <span class="files-spacer"></span>
        <button data-action="mkdir">New folder</button>
        <label class="files-upload">
          Upload
          <input type="file" multiple style="display:none" />
        </label>
      </div>
      <ul class="files-list"></ul>
    `;
    this.root.querySelector('[data-action="up"]')!.addEventListener("click", () => {
      this.navigate(parentPath(this.path));
    });
    this.root.querySelector('[data-action="mkdir"]')!.addEventListener("click", () => {
      void this.mkdir();
    });
    const input = this.root.querySelector("input[type=file]") as HTMLInputElement;
    input.addEventListener("change", () => {
      void this.uploadFiles(input.files);
      input.value = "";
    });
  }

  async refresh(): Promise<void> {
    this.root.querySelector(".files-path")!.textContent = this.path;
    const list = this.root.querySelector(".files-list") as HTMLElement;
    list.innerHTML = "<li class=\"files-loading\">Loading…</li>";
    try {
      const entries: Entry[] = await this.rpc.call("fs_listdir", { path: this.path });
      list.innerHTML = "";
      for (const entry of entries) {
        list.appendChild(this.renderEntry(entry));
      }
      if (entries.length === 0) {
        list.innerHTML = "<li class=\"files-empty\">Empty</li>";
      }
    } catch (e: any) {
      list.innerHTML = `<li class="files-error">${e.message}</li>`;
    }
  }

  private renderEntry(entry: Entry): HTMLElement {
    const li = document.createElement("li");
    li.className = "files-entry" + (entry.isDir ? " files-dir" : "");
    const name = document.createElement("span");
    name.className = "files-name";
    name.textContent = (entry.isDir ? "📁 " : "📄 ") + entry.name;
    name.addEventListener("click", () => {
      if (entry.isDir) {
        this.navigate(joinPath(this.path, entry.name));
      } else {
        void this.download(entry);
      }
    });
    const size = document.createElement("span");
    size.className = "files-size";
    size.textContent = entry.isDir ? "" : String(entry.size);
    const del = document.createElement("button");
    del.className = "files-delete";
    del.textContent = "✕";
    del.title = "Delete";
    del.addEventListener("click", (ev) => {
      ev.stopPropagation();
      void this.delete(entry);
    });
    li.append(name, size, del);
    return li;
  }

  private navigate(path: string): void {
    this.path = path;
    void this.refresh();
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
