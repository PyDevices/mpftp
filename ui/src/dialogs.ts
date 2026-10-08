/**
 * The page's stand-ins for VS Code's input box, modal warning and
 * notifications, which the panel's host asks for.
 */
import { button, buttons, h, openDialog } from "./wifi";

/** An input box; resolves to the text, or null on cancel. */
export function promptText(opts: {
  prompt: string;
  value?: string;
  placeHolder?: string;
  selection?: [number, number];
}): Promise<string | null> {
  return openDialog<string>(opts.prompt, (body, finish) => {
    const input = h("input", {
      type: "text",
      value: opts.value || "",
      placeholder: opts.placeHolder || "",
      spellcheck: false,
      "aria-label": opts.prompt,
    });
    const ok = button("OK", true);
    const cancel = button("Cancel");
    const submit = () => finish(input.value);
    input.addEventListener("keydown", (ev) => {
      if (ev.key === "Enter") {
        ev.preventDefault();
        submit();
      }
    });
    ok.addEventListener("click", submit);
    cancel.addEventListener("click", () => finish(null));
    body.append(h("label", { class: "mp-field" }, input), buttons(cancel, ok));
    setTimeout(() => {
      input.focus();
      const sel = opts.selection;
      if (sel) {
        input.setSelectionRange(sel[0], sel[1]);
      } else {
        input.select();
      }
    }, 0);
  });
}

/** A modal question with one action; resolves true when the user takes it. */
export async function confirmDialog(text: string, okLabel: string, danger = true): Promise<boolean> {
  const answer = await openDialog<boolean>(text, (body, finish) => {
    const ok = button(okLabel, true);
    if (danger) {
      ok.classList.add("mp-btn-danger");
    }
    const cancel = button("Cancel");
    ok.addEventListener("click", () => finish(true));
    cancel.addEventListener("click", () => finish(false));
    body.append(buttons(cancel, ok));
    setTimeout(() => cancel.focus(), 0);
  });
  return answer === true;
}

/** Where a Save As goes: which side, and the path there. */
export interface SaveAsTarget {
  side: "local" | "remote";
  path: string;
}

/**
 * Save As: this computer or the board (the board only while connected), and
 * a path that starts as that list's current folder plus the file's name.
 * Switching sides swaps in the other list's folder.
 */
export function saveAsDialog(opts: {
  name: string;
  side: "local" | "remote";
  folders: { local: string; remote: string };
  boardAvailable: boolean;
}): Promise<SaveAsTarget | null> {
  const defaultPath = (side: "local" | "remote"): string => {
    const folder = side === "local" ? opts.folders.local : opts.folders.remote || "/";
    const sep = side === "local" && /\\/.test(folder) && !folder.includes("/") ? "\\" : "/";
    return folder.endsWith(sep) ? folder + opts.name : folder + sep + opts.name;
  };
  return openDialog<SaveAsTarget>(`Save ${opts.name} as`, (body, finish) => {
    let side: "local" | "remote" = opts.side === "remote" && opts.boardAvailable ? "remote" : "local";
    const input = h("input", {
      type: "text",
      value: defaultPath(side),
      spellcheck: false,
      "aria-label": "Path",
    });
    const selectName = () => {
      const value = input.value;
      const start = value.length - opts.name.length;
      const dot = opts.name.lastIndexOf(".");
      input.focus();
      input.setSelectionRange(start, dot > 0 ? start + dot : value.length);
    };
    const choice = (value: "local" | "remote", label: string, enabled: boolean, note: string) => {
      const radio = h("input", {
        type: "radio",
        name: "mp-save-as-side",
        value,
        checked: value === side,
        disabled: !enabled,
      });
      radio.addEventListener("change", () => {
        if (radio.checked) {
          side = value;
          input.value = defaultPath(side);
          selectName();
        }
      });
      return h(
        "label",
        { class: "mp-choice" + (enabled ? "" : " is-disabled") },
        radio,
        h("span", { class: "mp-choice-label" }, label),
        h("span", { class: "mp-choice-note" }, note)
      );
    };
    const sides = h(
      "div",
      { class: "mp-choices", role: "radiogroup", "aria-label": "Where to save" },
      choice("local", "This computer", true, opts.folders.local),
      choice("remote", "Board", opts.boardAvailable, opts.boardAvailable ? opts.folders.remote || "/" : "not connected")
    );
    const ok = button("Save", true);
    const cancel = button("Cancel");
    const submit = () => {
      const path = input.value.trim();
      if (path) {
        finish({ side, path });
      }
    };
    input.addEventListener("keydown", (ev) => {
      if (ev.key === "Enter") {
        ev.preventDefault();
        submit();
      }
    });
    ok.addEventListener("click", submit);
    cancel.addEventListener("click", () => finish(null));
    body.append(
      sides,
      h("label", { class: "mp-field" }, "Path", input),
      h("p", { class: "mp-dialog-hint" }, "Browse to a folder in either list first to start there."),
      buttons(cancel, ok)
    );
    setTimeout(selectName, 0);
  });
}

let toastHost: HTMLElement | null = null;

/** A notification in the corner, like VS Code's; click to dismiss. */
export function toast(text: string, kind: "info" | "warning" | "error" = "info"): void {
  if (!toastHost) {
    toastHost = h("div", { class: "mp-toasts", role: "status", "aria-live": "polite" });
    document.body.append(toastHost);
  }
  const item = h("div", { class: `mp-toast mp-toast-${kind}` });
  item.textContent = text;
  const remove = () => item.remove();
  item.addEventListener("click", remove);
  toastHost.append(item);
  setTimeout(remove, kind === "error" ? 12000 : 6000);
}
