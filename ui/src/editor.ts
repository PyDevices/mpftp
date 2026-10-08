import { EditorView, basicSetup } from "codemirror";
import { python } from "@codemirror/lang-python";
import { keymap } from "@codemirror/view";
import { EditorState, Compartment, Extension } from "@codemirror/state";

const mpTheme = EditorView.theme(
  {
    "&": {
      height: "100%",
      backgroundColor: "#0f172a",
      color: "#f8fafc",
    },
    ".cm-content": { caretColor: "#f54e00" },
    ".cm-cursor": { borderLeftColor: "#f54e00" },
    ".cm-activeLine": { backgroundColor: "rgba(255,255,255,0.04)" },
    ".cm-gutters": {
      backgroundColor: "#0f172a",
      color: "#64748b",
      border: "none",
    },
    ".cm-activeLineGutter": { backgroundColor: "rgba(255,255,255,0.04)" },
    "&.cm-focused .cm-selectionBackground, ::selection": {
      backgroundColor: "rgba(245,78,0,0.25)",
    },
  },
  { dark: true }
);

const mpLightTheme = EditorView.theme(
  {
    "&": {
      height: "100%",
      backgroundColor: "#ffffff",
      color: "#242424",
    },
    ".cm-content": { caretColor: "#0f6cbd" },
    ".cm-cursor": { borderLeftColor: "#0f6cbd" },
    ".cm-activeLine": { backgroundColor: "#f5f5f5" },
    ".cm-gutters": {
      backgroundColor: "#fafafa",
      color: "#616161",
      borderRight: "1px solid #e0e0e0",
    },
    ".cm-activeLineGutter": { backgroundColor: "#ebebeb" },
    "&.cm-focused .cm-selectionBackground, ::selection": {
      backgroundColor: "#cfe4fa",
    },
  },
  { dark: false }
);

/** Where an open file lives: on this computer or on the board. */
export type Side = "local" | "remote";

export interface OpenFile {
  side: Side;
  path: string;
}

interface Doc extends OpenFile {
  key: string;
  state: EditorState;
  clean: string;
  tab: HTMLElement;
}

function baseName(path: string): string {
  const parts = path.split(/[\\/]/).filter(Boolean);
  return parts.length ? parts[parts.length - 1] : path;
}

/**
 * CodeMirror 6 with a tab per open file. A tab remembers whether it came from
 * the local list or the board, so a save goes back to the same place, until
 * a Save As moves it somewhere new.
 */
export class Editor {
  private view: EditorView | null = null;
  private docs = new Map<string, Doc>();
  private active: Doc | null = null;
  private themeCompartment = new Compartment();
  private dark = true;

  constructor(
    private container: HTMLElement,
    private tabs: HTMLElement,
    private opts: {
      onChange: (file: OpenFile | null, dirty: boolean) => void;
      onSave: () => void;
      onSaveAs: () => void;
      confirmClose: (name: string) => Promise<boolean>;
    }
  ) {
    this.showEmpty();
  }

  private showEmpty(): void {
    this.view?.destroy();
    this.view = null;
    this.container.innerHTML =
      '<div class="mp-editor-empty">Double-click a board file, or use Open in Editor on either list, to edit it here.</div>';
  }

  private extensions(): Extension[] {
    return [
      basicSetup,
      python(),
      keymap.of([
        {
          key: "Mod-s",
          run: () => {
            this.opts.onSave();
            return true;
          },
        },
        {
          key: "Shift-Mod-s",
          run: () => {
            this.opts.onSaveAs();
            return true;
          },
        },
      ]),
      this.themeCompartment.of(this.dark ? mpTheme : mpLightTheme),
      EditorView.updateListener.of((update) => {
        if (update.docChanged && this.active) {
          this.active.state = update.state;
          this.refreshTab(this.active);
          this.emit();
        }
      }),
    ];
  }

  /** Open (or switch to) a file; a fresh copy replaces an unchanged tab's text. */
  open(side: Side, path: string, content: string): void {
    const key = `${side}:${path}`;
    let doc = this.docs.get(key);
    if (doc && this.isDocDirty(doc)) {
      this.activate(doc);
      return;
    }
    const state = EditorState.create({ doc: content, extensions: this.extensions() });
    if (!doc) {
      const tab = document.createElement("div");
      tab.className = "mp-editor-tab";
      tab.setAttribute("role", "tab");
      const label = document.createElement("span");
      label.className = "mp-editor-tab-label";
      const close = document.createElement("button");
      close.type = "button";
      close.className = "mp-editor-tab-close";
      close.title = "Close";
      close.setAttribute("aria-label", "Close");
      close.textContent = "×";
      tab.append(label, close);
      doc = { key, side, path, state, clean: content, tab };
      const d = doc;
      tab.addEventListener("click", () => this.activate(d));
      close.addEventListener("click", (ev) => {
        ev.stopPropagation();
        void this.close(d);
      });
      this.docs.set(key, doc);
      this.tabs.append(tab);
    } else {
      doc.state = state;
      doc.clean = content;
    }
    this.activate(doc, true);
  }

  private activate(doc: Doc, reset = false): void {
    if (this.active && this.view && this.active !== doc) {
      this.active.state = this.view.state;
    }
    this.active = doc;
    if (!this.view) {
      this.container.innerHTML = "";
      this.view = new EditorView({ state: doc.state, parent: this.container });
    } else if (reset || this.view.state !== doc.state) {
      this.view.setState(doc.state);
    }
    this.view.dispatch({ effects: this.themeCompartment.reconfigure(this.dark ? mpTheme : mpLightTheme) });
    doc.state = this.view.state;
    for (const d of this.docs.values()) {
      d.tab.classList.toggle("is-active", d === doc);
      this.refreshTab(d);
    }
    this.view.focus();
    this.emit();
  }

  private async close(doc: Doc): Promise<void> {
    if (this.isDocDirty(doc) && !(await this.opts.confirmClose(baseName(doc.path)))) {
      return;
    }
    this.docs.delete(doc.key);
    doc.tab.remove();
    if (this.active === doc) {
      this.active = null;
      const next = [...this.docs.values()].pop();
      if (next) {
        this.activate(next, true);
      } else {
        this.showEmpty();
        this.emit();
      }
    }
  }

  private refreshTab(doc: Doc): void {
    const label = doc.tab.querySelector(".mp-editor-tab-label") as HTMLElement;
    const dirty = this.isDocDirty(doc);
    label.textContent = (dirty ? "● " : "") + baseName(doc.path);
    doc.tab.title = `${doc.side === "remote" ? "Board" : "Local"}: ${doc.path}`;
    doc.tab.dataset.side = doc.side;
    doc.tab.classList.toggle("is-dirty", dirty);
  }

  private emit(): void {
    const doc = this.active;
    this.opts.onChange(doc ? { side: doc.side, path: doc.path } : null, doc ? this.isDocDirty(doc) : false);
  }

  private isDocDirty(doc: Doc): boolean {
    const text = doc === this.active && this.view ? this.view.state.doc.toString() : doc.state.doc.toString();
    return text !== doc.clean;
  }

  current(): OpenFile | null {
    return this.active ? { side: this.active.side, path: this.active.path } : null;
  }

  getContent(): string {
    return this.view ? this.view.state.doc.toString() : "";
  }

  /** The text that was saved becomes the clean copy (the buffer may have moved on). */
  markClean(file: OpenFile, savedText: string): void {
    const doc = this.docs.get(`${file.side}:${file.path}`);
    if (!doc) {
      return;
    }
    doc.clean = savedText;
    this.refreshTab(doc);
    this.emit();
  }

  /**
   * After a Save As: the tab now stands for the new copy, so the next Save
   * goes there. A tab already open on that file gives way to this one.
   */
  rebind(from: OpenFile, to: OpenFile, savedText: string): void {
    const doc = this.docs.get(`${from.side}:${from.path}`);
    if (!doc) {
      return;
    }
    const key = `${to.side}:${to.path}`;
    const other = this.docs.get(key);
    if (other && other !== doc) {
      this.docs.delete(key);
      other.tab.remove();
    }
    this.docs.delete(doc.key);
    doc.key = key;
    doc.side = to.side;
    doc.path = to.path;
    doc.clean = savedText;
    this.docs.set(key, doc);
    this.refreshTab(doc);
    this.emit();
  }

  isDirty(): boolean {
    return [...this.docs.values()].some((d) => this.isDocDirty(d));
  }

  setTheme(dark: boolean): void {
    this.dark = dark;
    this.view?.dispatch({
      effects: this.themeCompartment.reconfigure(dark ? mpTheme : mpLightTheme),
    });
  }
}
