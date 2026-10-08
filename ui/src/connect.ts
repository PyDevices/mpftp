/**
 * The Connect picker (the panel's plug button): USB serial ports, remembered
 * Wi-Fi boards, a typed address, or a Bluetooth scan. Resolves to the device
 * to connect to, or null.
 */
import type { Rpc } from "./rpc";
import { WifiBoard, askAddress, button, buttons, h, openDialog, pickBleBoard } from "./wifi";

interface Port {
  device: string;
  description?: string | null;
  product?: string | null;
  manufacturer?: string | null;
  /** false for CircuitPython's second (data) CDC interface. */
  repl?: boolean;
}

const LAST_DEVICE_KEY = "mpftp-last-device";

export function lastDevice(): string {
  try {
    return localStorage.getItem(LAST_DEVICE_KEY) || "";
  } catch {
    return "";
  }
}

export function rememberDevice(device: string): void {
  try {
    localStorage.setItem(LAST_DEVICE_KEY, device);
  } catch {
    /* private browsing */
  }
}

type Choice = { device: string } | { typeAddress: true } | { scanBle: true };

export async function pickDevice(rpc: Rpc): Promise<string | null> {
  const choice = await openDialog<Choice>("Connect to a board", (body, finish) => {
    const list = h("div", { class: "mp-device-list" });
    const note = h("p", { class: "mp-dialog-hint" }, "Looking for boards…");
    const refresh = button("Refresh");
    const cancel = button("Cancel");
    const last = lastDevice();

    const row = (label: string, detail: string, onPick: () => void, primary = false) => {
      const b = button("", primary);
      b.classList.add("mp-device");
      b.append(h("span", { class: "mp-device-name" }, label), h("span", { class: "mp-device-detail" }, detail));
      b.addEventListener("click", onPick);
      return b;
    };

    const load = async () => {
      refresh.disabled = true;
      list.replaceChildren();
      let ports: Port[] = [];
      let boards: WifiBoard[] = [];
      try {
        ports = await rpc.call("list_ports");
      } catch (e: any) {
        note.textContent = `Could not list serial ports: ${e.message}`;
      }
      try {
        boards = await rpc.call("wifi_boards");
      } catch {
        /* an older server; serial still works */
      }
      const usable = ports.filter((p) => p.repl !== false);
      note.textContent = usable.length || boards.length ? "" : "No USB serial ports found. Plug a board in and press Refresh.";
      if (usable.length) {
        list.append(h("div", { class: "mp-device-group" }, "USB serial"));
        for (const p of usable) {
          const detail = p.description || p.product || "";
          list.append(row(p.device, detail, () => finish({ device: p.device }), p.device === last));
        }
      }
      list.append(h("div", { class: "mp-device-group" }, "Wi-Fi (WebREPL)"));
      for (const b of boards) {
        list.append(
          row(b.name, `${b.ip}${b.hasPassword ? "" : " · no password saved"}`, () => finish({ device: b.device }), b.device === last)
        );
      }
      list.append(row("Type an address…", "IP address or NAME.local", () => finish({ typeAddress: true })));
      list.append(h("div", { class: "mp-device-group" }, "Bluetooth (bledev)"));
      list.append(row("Look for Bluetooth boards…", "", () => finish({ scanBle: true })));
      refresh.disabled = false;
    };

    refresh.addEventListener("click", () => void load());
    cancel.addEventListener("click", () => finish(null));
    body.append(note, list, buttons(refresh, cancel));
    void load();
  });
  if (!choice) {
    return null;
  }
  if ("typeAddress" in choice) {
    return askAddress(rpc);
  }
  if ("scanBle" in choice) {
    return pickBleBoard(rpc);
  }
  return choice.device;
}
