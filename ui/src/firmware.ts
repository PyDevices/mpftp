/**
 * The Firmware button: flash a .bin from this computer onto an ESP board with
 * esptool. The file's bytes go to the local server in chunks (firmware_upload),
 * which checks the image, lets go of the board, runs esptool and streams its
 * output back (firmware_log, firmware_progress) until firmware_flash answers.
 *
 * UF2 boards need none of this: their .uf2 is copied onto the board's drive.
 */
import type { Rpc } from "./rpc";
import { button, buttons, h, openDialog } from "./wifi";

/** Bytes per firmware_upload call, before base64. */
const CHUNK = 512 * 1024;
const MAX_LOG_LINES = 2000;

interface FlashResult {
  ok: boolean;
  error?: string;
  needErase?: boolean;
  reconnect?: boolean;
  device?: string;
  port?: string;
  chip?: string;
  offset?: string;
}

function bytesToBase64(bytes: Uint8Array): string {
  let binary = "";
  for (let i = 0; i < bytes.length; i += 0x8000) {
    binary += String.fromCharCode(...bytes.subarray(i, i + 0x8000));
  }
  return btoa(binary);
}

export function firmwareDialog(
  rpc: Rpc,
  opts: {
    /** The connected board's port ("" when none). */
    device: string;
    /** Stop the page's REPL before the server lets go of the board. */
    release: () => Promise<void>;
    /** Wait for the board to come back and connect to it again. */
    reconnect: (device: string) => Promise<boolean>;
  }
): Promise<void> {
  let busy = false;
  return openDialog<void>(
    "Flash firmware (esptool)",
    (body, finish) => {
      const file = h("input", { type: "file", accept: ".bin", "aria-label": "Firmware file" });
      const port = h("input", {
        type: "text",
        value: opts.device,
        placeholder: "COM4, /dev/ttyACM0",
        spellcheck: false,
        "aria-label": "Serial port",
      });
      port.setAttribute("list", "mp-fw-ports");
      const portList = h("datalist", { id: "mp-fw-ports" });
      const erase = h("input", { type: "checkbox" });
      const statusLine = h("p", { class: "mp-dialog-note mp-fw-status" });
      const bar = h("progress", { class: "mp-fw-progress", max: 100 });
      const log = h("pre", { class: "mp-diff mp-fw-log" });
      bar.hidden = true;
      log.hidden = true;
      const flash = button("Flash", true);
      const close = button("Close");

      void rpc
        .call("list_ports")
        .then((ports: Array<{ device: string; description?: string }>) => {
          for (const p of ports || []) {
            portList.append(h("option", { value: p.device }, p.description || ""));
          }
          if (!port.value && ports?.length) {
            port.value = ports[0].device;
          }
        })
        .catch(() => undefined);

      const say = (text: string, kind: "" | "ok" | "error" = "") => {
        statusLine.textContent = text;
        statusLine.className = `mp-dialog-note mp-fw-status${kind ? " mp-fw-" + kind : ""}`;
      };
      const addLog = (line: string) => {
        log.hidden = false;
        const atEnd = log.scrollTop + log.clientHeight >= log.scrollHeight - 4;
        log.append(line + "\n");
        while (log.childNodes.length > MAX_LOG_LINES) {
          log.firstChild?.remove();
        }
        if (atEnd) {
          log.scrollTop = log.scrollHeight;
        }
      };
      const setBusy = (on: boolean) => {
        busy = on;
        for (const c of [file, port, erase, flash, close]) {
          c.disabled = on;
        }
      };

      const listen = () => {
        const offLog = rpc.onNotify("firmware_log", (p) => addLog(String(p.line ?? "")));
        const offProgress = rpc.onNotify("firmware_progress", (p) => {
          say(String(p.text || ""));
          if (typeof p.percent === "number") {
            bar.hidden = false;
            bar.value = p.percent;
          }
        });
        return () => {
          offLog();
          offProgress();
        };
      };

      const run = async () => {
        const chosen = file.files?.[0];
        if (!chosen || !chosen.size) {
          say("Choose a firmware .bin first.", "error");
          return;
        }
        if (/\.uf2$/i.test(chosen.name)) {
          say("That's a UF2 file. For UF2 boards, drag the .uf2 onto the board's drive.", "error");
          return;
        }
        const device = port.value.trim();
        if (!device) {
          say("Type the board's serial port, or connect to it first.", "error");
          return;
        }
        setBusy(true);
        log.textContent = "";
        bar.hidden = false;
        bar.value = 0;
        const unlisten = listen();
        try {
          const bytes = new Uint8Array(await chosen.arrayBuffer());
          for (let offset = 0; offset < bytes.length; offset += CHUNK) {
            say(`Sending ${chosen.name} to mpftp… ${Math.round((100 * offset) / bytes.length)}%`);
            await rpc.call("firmware_upload", {
              name: chosen.name,
              size: bytes.length,
              offset,
              data_b64: bytesToBase64(bytes.subarray(offset, offset + CHUNK)),
            });
          }
          bar.value = 0;
          if (device === opts.device) {
            await opts.release();
          }
          const res: FlashResult = await rpc.call("firmware_flash", { device, erase: erase.checked });
          if (res.ok) {
            bar.value = 100;
            say(`Flashed ${chosen.name} (${res.chip} at ${res.offset}) on ${res.port}.`, "ok");
          } else {
            say(res.error || "The flash failed.", "error");
            if (res.needErase) {
              erase.checked = true;
              addLog("[mpftp] Erase is now ticked: Flash again to apply the new partition table (the board's files are wiped).");
            }
          }
          if (res.reconnect && res.device) {
            const was = statusLine.textContent || "";
            say(`${was} Reconnecting to ${res.device}…`, res.ok ? "ok" : "error");
            if (await opts.reconnect(res.device)) {
              say(`${was} Reconnected to ${res.device}.`, res.ok ? "ok" : "error");
            } else {
              say(`${was} ${res.device} didn't come back; press Connect when it's ready.`, res.ok ? "ok" : "error");
            }
          }
        } catch (e: any) {
          say(`Flash failed: ${e?.message || e}`, "error");
        } finally {
          unlisten();
          setBusy(false);
        }
      };

      flash.addEventListener("click", () => void run());
      close.addEventListener("click", () => finish(null));
      body.append(
        h(
          "p",
          { class: "mp-dialog-hint" },
          "Writes a MicroPython or CircuitPython .bin to an ESP32-family board with esptool. " +
            "For UF2 boards, drag the .uf2 onto the board's drive."
        ),
        h("label", { class: "mp-field" }, "Firmware file (.bin)", file),
        h("label", { class: "mp-field" }, "Serial port", port, portList),
        h("label", { class: "mp-check" }, erase, " Erase all of flash first (also wipes the files on the board)"),
        statusLine,
        bar,
        log,
        buttons(close, flash)
      );
    },
    { canCancel: () => !busy }
  ).then(() => undefined);
}
