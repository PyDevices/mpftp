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
