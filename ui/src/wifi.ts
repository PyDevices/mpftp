/**
 * Wi-Fi in the PWA: the password prompt, the typed-address box (with an mDNS
 * look-up), and "Enable / Disable Wi-Fi access", which shows the exact boot.py
 * change before anything is written.
 *
 * Passwords and remembered boards are kept by the local server (mpftp.pwa) in
 * ~/.mpftp; this page sends a password in and never gets one back.
 */
import type { Rpc } from "./rpc";

export interface WifiBoard {
  uid: string;
  name: string;
  ip: string;
  hostname?: string;
  device: string;
  seen?: string;
  hasPassword: boolean;
}

/** WebREPL keeps 9 characters; mpftp writes 4 to 9, like upstream's webrepl_setup. */
export const MAX_PASSWORD = 9;
export const MIN_NEW_PASSWORD = 4;

export function isWifiDevice(device: string): boolean {
  return /^wss?:\/\//i.test(device);
}

/** True when a connect failed for want of the right password. */
export function needsPassword(message: string): boolean {
  return /no WebREPL password|rejected the WebREPL password/i.test(message);
}

function h<K extends keyof HTMLElementTagNameMap>(
  tag: K,
  props: Record<string, any> = {},
  ...children: Array<Node | string>
): HTMLElementTagNameMap[K] {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(props)) {
    if (key === "class") {
      node.className = value;
    } else if (key in node) {
      (node as any)[key] = value;
    } else {
      node.setAttribute(key, String(value));
    }
  }
  node.append(...children);
  return node;
}

/** A modal <dialog>; resolves with whatever `finish` is called with. */
function openDialog<T>(
  title: string,
  build: (body: HTMLElement, finish: (value: T | null) => void) => void
): Promise<T | null> {
  return new Promise((resolve) => {
    const body = h("div", { class: "mp-dialog-body" });
    const dialog = h(
      "dialog",
      { class: "mp-dialog" },
      h("h2", { class: "mp-dialog-title" }, title),
      body
    );
    let done = false;
    const finish = (value: T | null) => {
      if (done) {
        return;
      }
      done = true;
      dialog.close();
      dialog.remove();
      resolve(value);
    };
    dialog.addEventListener("cancel", () => finish(null));
    build(body, finish);
    document.body.append(dialog);
    dialog.showModal();
  });
}

function passwordField(label: string, minLength: number): HTMLInputElement {
  return h("input", {
    type: "password",
    maxLength: MAX_PASSWORD,
    minLength,
    required: true,
    autocomplete: "off",
    spellcheck: false,
    "aria-label": label,
  });
}

function buttons(...items: HTMLButtonElement[]): HTMLElement {
  return h("div", { class: "mp-dialog-actions" }, ...items);
}

function button(text: string, primary = false): HTMLButtonElement {
  return h("button", { type: "button", class: primary ? "mp-btn mp-btn-primary" : "mp-btn" }, text);
}

/** Ask for a board's WebREPL password. */
export function askPassword(
  device: string,
  why: string
): Promise<{ password: string; remember: boolean } | null> {
  return openDialog(`Password for ${device}`, (body, finish) => {
    const input = passwordField("WebREPL password", 1);
    const remember = h("input", { type: "checkbox", checked: true });
    const go = button("Connect", true);
    const cancel = button("Cancel");
    const submit = () => {
      if (!input.value) {
        input.focus();
        return;
      }
      finish({ password: input.value, remember: remember.checked });
    };
    input.addEventListener("keydown", (ev) => {
      if (ev.key === "Enter") {
        submit();
      }
    });
    go.addEventListener("click", submit);
    cancel.addEventListener("click", () => finish(null));
    body.append(
      h("p", { class: "mp-dialog-note" }, why),
      h("label", { class: "mp-field" }, "WebREPL password", input),
      h(
        "p",
        { class: "mp-dialog-hint" },
        `WebREPL keeps at most ${MAX_PASSWORD} characters, so mpftp won't take a longer one.`
      ),
      h(
        "label",
        { class: "mp-check" },
        remember,
        " Remember it for this board (saved in ~/.mpftp/webrepl-passwords.json, plaintext, readable only by you)"
      ),
      buttons(cancel, go)
    );
    setTimeout(() => input.focus(), 0);
  });
}

/** Ask for an address, with an mDNS look-up for NAME.local. Resolves to ws://… */
export function askAddress(rpc: Rpc): Promise<string | null> {
  return openDialog("Connect over Wi-Fi", (body, finish) => {
    const input = h("input", {
      type: "text",
      placeholder: "192.168.1.50 or mpy-esp32p4.local",
      spellcheck: false,
      "aria-label": "Board address",
    });
    const result = h("p", { class: "mp-dialog-hint" }, "");
    const lookup = button("Look up .local");
    const go = button("Connect", true);
    const cancel = button("Cancel");
    const submit = () => {
      let host = input.value.trim();
      if (!host) {
        input.focus();
        return;
      }
      if (!isWifiDevice(host)) {
        host = `ws://${host}`;
      }
      finish(host);
    };
    lookup.addEventListener("click", () => {
      const name = input.value.trim().replace(/^wss?:\/\//i, "").split(":")[0];
      if (!name) {
        input.focus();
        return;
      }
      result.textContent = `Asking the network for ${name}…`;
      lookup.disabled = true;
      rpc
        .call("mdns_resolve", { name })
        .then((res: any) => {
          if (res.ip) {
            result.textContent = `${res.name} is at ${res.ip} (found via ${res.via}).`;
            input.value = res.ip;
          } else {
            result.textContent =
              `Nobody answered for ${res.name}. mDNS is best effort; type the address instead ` +
              "(connect once over USB and mpftp remembers it).";
          }
        })
        .catch((e: Error) => {
          result.textContent = `Look-up failed: ${e.message}`;
        })
        .finally(() => {
          lookup.disabled = false;
        });
    });
    input.addEventListener("keydown", (ev) => {
      if (ev.key === "Enter") {
        submit();
      }
    });
    go.addEventListener("click", submit);
    cancel.addEventListener("click", () => finish(null));
    body.append(
      h(
        "p",
        { class: "mp-dialog-note" },
        "The board needs Wi-Fi up and WebREPL started. \"Wi-Fi access\" can set that up over USB."
      ),
      h("label", { class: "mp-field" }, "Address", input),
      result,
      buttons(lookup, cancel, go)
    );
    setTimeout(() => input.focus(), 0);
  });
}

/**
 * Enable or disable Wi-Fi access: plan, show the diff, write only on "yes".
 * Returns a sentence for the status line, or null if the user backed out.
 */
export function wifiAccessDialog(rpc: Rpc, device: string): Promise<string | null> {
  const overWifi = isWifiDevice(device);
  return openDialog<string>("Wi-Fi access", (body, finish) => {
    const status = h("p", { class: "mp-dialog-hint" }, "");
    const diffView = h("pre", { class: "mp-diff", hidden: true });
    const pw1 = passwordField("New WebREPL password", MIN_NEW_PASSWORD);
    const pw2 = passwordField("Password again", MIN_NEW_PASSWORD);
    const enableBtn = button("Show the change to enable", true);
    const disableBtn = button("Show the change to disable");
    const writeBtn = button("Write it to boot.py", true);
    writeBtn.hidden = true;
    const close = button("Close");
    let pending: { action: string; password?: string; sha256: string | null } | null = null;

    const renderDiff = (diff: string) => {
      diffView.replaceChildren();
      for (const line of diff.split("\n")) {
        const cls = line.startsWith("+") && !line.startsWith("+++")
          ? "add"
          : line.startsWith("-") && !line.startsWith("---")
            ? "del"
            : "";
        diffView.append(h("span", { class: cls }, line + "\n"));
      }
      diffView.hidden = false;
    };

    const plan = async (action: "enable" | "disable") => {
      pending = null;
      writeBtn.hidden = true;
      diffView.hidden = true;
      let password: string | undefined;
      if (action === "enable") {
        if (pw1.value.length < MIN_NEW_PASSWORD || pw1.value.length > MAX_PASSWORD) {
          status.textContent = `Pick a password of ${MIN_NEW_PASSWORD} to ${MAX_PASSWORD} characters.`;
          pw1.focus();
          return;
        }
        if (pw1.value !== pw2.value) {
          status.textContent = "The two passwords don't match.";
          pw2.focus();
          return;
        }
        password = pw1.value;
      }
      status.textContent = "Reading boot.py from the board…";
      try {
        const res: any = await rpc.call("wifi_access_plan", { action, password });
        if (res.problems?.length) {
          status.textContent = res.problems.join(" ");
          return;
        }
        renderDiff(res.diff);
        pending = { action, password, sha256: res.sha256 };
        writeBtn.textContent = res.delete ? "Delete boot.py" : "Write it to boot.py";
        writeBtn.hidden = false;
        status.textContent =
          (action === "enable"
            ? "This is the change. The password shows as ***** here; the board gets the real one. "
            : "This is the change. ") + "Nothing is written until you say so.";
      } catch (e: any) {
        status.textContent = e.message;
      }
    };

    enableBtn.addEventListener("click", () => void plan("enable"));
    disableBtn.addEventListener("click", () => void plan("disable"));
    writeBtn.addEventListener("click", () => {
      if (!pending) {
        return;
      }
      const { action, password, sha256 } = pending;
      writeBtn.disabled = true;
      status.textContent = action === "enable" ? "Writing boot.py and joining Wi-Fi…" : "Writing boot.py…";
      rpc
        .call("wifi_access_apply", {
          action,
          password,
          expect_sha256: sha256,
          now: !overWifi,
        })
        .then((res: any) => {
          if (action === "enable") {
            const where = res.ip ? `ws://${res.ip}` : "its address once Wi-Fi is up";
            finish(
              `Wi-Fi access is on. boot.py starts it at every reset; reach the board at ${where}` +
                (res.board?.hostname ? ` (listed as ${res.board.hostname}).` : ".")
            );
          } else {
            finish("Wi-Fi access is off from the next reset; boot.py is back as it was.");
          }
        })
        .catch((e: Error) => {
          status.textContent = e.message;
          writeBtn.disabled = false;
        });
    });
    close.addEventListener("click", () => finish(null));

    const enableSection = h(
      "fieldset",
      { class: "mp-fieldset", disabled: overWifi },
      h("legend", {}, "Enable"),
      h(
        "p",
        { class: "mp-dialog-note" },
        "Adds a marked block to the top of boot.py that joins Wi-Fi with the board's own " +
          "secrets.py (through its wifi helper) and starts WebREPL with this password. " +
          (overWifi ? "Connect over USB to enable it." : "")
      ),
      h("label", { class: "mp-field" }, "New WebREPL password", pw1),
      h("label", { class: "mp-field" }, "Again", pw2),
      h(
        "p",
        { class: "mp-dialog-hint" },
        `${MIN_NEW_PASSWORD} to ${MAX_PASSWORD} characters: the board keeps only ${MAX_PASSWORD}. ` +
          "WebREPL has no encryption, so use it on a network you trust."
      ),
      buttons(enableBtn)
    );
    const disableSection = h(
      "fieldset",
      { class: "mp-fieldset" },
      h("legend", {}, "Disable"),
      h("p", { class: "mp-dialog-note" }, "Removes mpftp's block, leaving the rest of boot.py byte for byte."),
      buttons(disableBtn)
    );
    body.append(enableSection, disableSection, diffView, status, buttons(close, writeBtn));
  });
}
