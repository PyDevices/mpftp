import { EditorView, basicSetup } from "codemirror";
import { python } from "@codemirror/lang-python";
import { keymap } from "@codemirror/view";
import { EditorState, Compartment } from "@codemirror/state";

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
      color: "#0f172a",
    },
    ".cm-content": { caretColor: "#ea580c" },
    ".cm-cursor": { borderLeftColor: "#ea580c" },
    ".cm-activeLine": { backgroundColor: "rgba(0,0,0,0.03)" },
    ".cm-gutters": {
      backgroundColor: "#ffffff",
      color: "#94a3b8",
      border: "none",
    },
    ".cm-activeLineGutter": { backgroundColor: "rgba(0,0,0,0.03)" },
    "&.cm-focused .cm-selectionBackground, ::selection": {
      backgroundColor: "rgba(234,88,12,0.18)",
    },
  },
  { dark: false }
);

export class Editor {
  private view: EditorView | null = null;
  private container: HTMLElement;
  private themeCompartment = new Compartment();
  private currentPath: string | null = null;
  private cleanContent = "";
  private onDirty: (dirty: boolean) => void;
  private onSave: () => void;
  private dark = true;

  constructor(container: HTMLElement, opts: { onDirty: (dirty: boolean) => void; onSave: () => void }) {
    this.container = container;
    this.onDirty = opts.onDirty;
    this.onSave = opts.onSave;
    this.showEmpty();
  }

  private showEmpty(): void {
    this.view?.destroy();
    this.view = null;
    this.container.innerHTML = '<div class="mp-editor-empty">Open a file from the panel on the right to edit it.</div>';
  }

  open(path: string, content: string): void {
    this.currentPath = path;
    this.cleanContent = content;
    this.container.innerHTML = "";
    this.view = new EditorView({
      state: EditorState.create({
        doc: content,
        extensions: [
          basicSetup,
          python(),
          keymap.of([
            {
              key: "Mod-s",
              run: () => {
                this.onSave();
                return true;
              },
            },
          ]),
          this.themeCompartment.of(this.dark ? mpTheme : mpLightTheme),
          EditorView.updateListener.of((update) => {
            if (update.docChanged) {
              this.onDirty(this.getContent() !== this.cleanContent);
            }
          }),
        ],
      }),
      parent: this.container,
    });
  }

  close(): void {
    this.currentPath = null;
    this.cleanContent = "";
    this.showEmpty();
  }

  getPath(): string | null {
    return this.currentPath;
  }

  getContent(): string {
    return this.view ? this.view.state.doc.toString() : "";
  }

  markClean(): void {
    this.cleanContent = this.getContent();
    this.onDirty(false);
  }

  isDirty(): boolean {
    return this.view !== null && this.getContent() !== this.cleanContent;
  }

  setTheme(dark: boolean): void {
    this.dark = dark;
    this.view?.dispatch({
      effects: this.themeCompartment.reconfigure(dark ? mpTheme : mpLightTheme),
    });
  }
}
