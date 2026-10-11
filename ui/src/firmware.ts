/**
 * The Firmware button: put MicroPython on a board.
 *
 * The image comes from the official catalog (pick a board, a variant and a
 * version; the local server downloads it with firmware_fetch) or from a file
 * on this computer (its bytes go to the server in chunks, firmware_upload).
 * Then firmware_flash writes it: an ESP .bin with esptool over the serial
 * port, a .uf2 by copying it to the board's UF2 bootloader drive. Output
 * streams back (firmware_log, firmware_progress) until firmware_flash answers.
 */
import type { Rpc } from "./rpc";
import { button, buttons, h, openDialog } from "./wifi";

/** Bytes per firmware_upload call, before base64. */
const CHUNK = 512 * 1024;
const MAX_LOG_LINES = 2000;
const LAST_BOARD_KEY = "mpftp.firmware.board";

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

interface CatalogBoard {
  board: string;
  label: string;
  family: string;
}

interface CatalogPort {
  port: string;
  flasher: string;
  boards: CatalogBoard[];
}

interface FirmwareFile {
  name: string;
  version: string;
  date: string;
  url: string;
  kind: string;
  variant: string;
  channel: string;
}

interface BoardFiles {
  board: string;
  title: string;
  port: string;
  info_url: string;
  preferred: string;
  variantNotes: Record<string, string>;
  files: FirmwareFile[];
}

function bytesToBase64(bytes: Uint8Array): string {
  let binary = "";
  for (let i = 0; i < bytes.length; i += 0x8000) {
    binary += String.fromCharCode(...bytes.subarray(i, i + 0x8000));
  }
  return btoa(binary);
}

function remembered(): string {
  try {
    return localStorage.getItem(LAST_BOARD_KEY) || "";
  } catch {
    return "";
  }
}

function remember(board: string): void {
  try {
    localStorage.setItem(LAST_BOARD_KEY, board);
  } catch {
    /* private window: nothing to keep */
  }
}

/** "20260929" → "2026-09-29". */
function day(date: string): string {
  return /^\d{8}$/.test(date) ? `${date.slice(0, 4)}-${date.slice(4, 6)}-${date.slice(6)}` : date;
}

function option(value: string, text: string): HTMLOptionElement {
  return h("option", { value }, text);
}

export function firmwareDialog(
  rpc: Rpc,
  opts: {
    /** The connected board's serial port ("" when none, or over Wi-Fi/Bluetooth). */
    device: string;
    /** Stop the page's REPL before the server lets go of the board. */
    release: () => Promise<void>;
    /** Wait for the board to come back and connect to it again. */
    reconnect: (device: string) => Promise<boolean>;
  }
): Promise<void> {
  let busy = false;
  return openDialog<void>(
    "Flash firmware",
    (body, finish) => {
      // --- where the image comes from ---------------------------------------
      const fromCatalog = h("input", { type: "radio", name: "mp-fw-source", checked: true });
      const fromFile = h("input", { type: "radio", name: "mp-fw-source" });
      const sourceChoice = (input: HTMLInputElement, label: string, note: string) =>
        h(
          "label",
          { class: "mp-choice" },
          input,
          h("span", { class: "mp-choice-label" }, label),
          h("span", { class: "mp-choice-note" }, note)
        );

      // Catalog: board, variant, version, format.
      const boardInput = h("input", {
        type: "text",
        placeholder: "Loading the board list…",
        spellcheck: false,
        "aria-label": "Board",
      });
      boardInput.setAttribute("list", "mp-fw-boards");
      const boardList = h("datalist", { id: "mp-fw-boards" });
      const variant = h("select", { "aria-label": "Variant" });
      const version = h("select", { "aria-label": "Version" });
      const format = h("select", { "aria-label": "Image format" });
      const boardLink = h("a", { target: "_blank", rel: "noopener", class: "mp-fw-link" });
      const formatField = h("label", { class: "mp-field" }, "Format", format);
      const catalogBox = h(
        "fieldset",
        { class: "mp-fieldset mp-fw-catalog" },
        h("legend", {}, "Official firmware"),
        h("label", { class: "mp-field" }, "Board", boardInput, boardList),
        h("div", { class: "mp-fw-row" },
          h("label", { class: "mp-field" }, "Variant", variant),
          h("label", { class: "mp-field" }, "Version", version)
        ),
        formatField,
        boardLink
      );

      // File: any .bin or .uf2 on this computer.
      const file = h("input", { type: "file", accept: ".bin,.uf2", "aria-label": "Firmware file" });
      const fileBox = h(
        "fieldset",
        { class: "mp-fieldset" },
        h("legend", {}, "Firmware file"),
        h("label", { class: "mp-field" }, "An ESP .bin (the combined image) or a .uf2", file)
      );

      // --- where it goes ------------------------------------------------------
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
      const serialBox = h(
        "div",
        {},
        h("label", { class: "mp-field" }, "Serial port", port, portList),
        h("label", { class: "mp-check" }, erase, " Erase all of flash first (also wipes the files on the board)")
      );
      const volume = h("select", { "aria-label": "UF2 drive" });
      const lookAgain = button("Look again");
      const uf2Box = h(
        "div",
        {},
        h("label", { class: "mp-field" }, "UF2 drive", h("div", { class: "mp-fw-row" }, volume, lookAgain)),
        h(
          "p",
          { class: "mp-dialog-hint" },
          "With no drive mounted, the connected board is restarted into its UF2 bootloader. " +
            "Otherwise hold BOOTSEL (or BOOT) while plugging it in, or double-tap reset."
        )
      );

      const statusLine = h("p", { class: "mp-dialog-note mp-fw-status" });
      const bar = h("progress", { class: "mp-fw-progress", max: 100 });
      const log = h("pre", { class: "mp-diff mp-fw-log" });
      bar.hidden = true;
      log.hidden = true;
      const flash = button("Flash", true);
      const close = button("Close");

      // --- state ---------------------------------------------------------------
      const boardIds = new Map<string, string>(); // label or id (upper case) → id
      let current: BoardFiles | null = null;

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

      /** The image kind the Flash button will write: "bin", "uf2" or "". */
      const kind = (): string => {
        if (fromFile.checked) {
          const name = file.files?.[0]?.name || "";
          return /\.uf2$/i.test(name) ? "uf2" : /\.bin$/i.test(name) ? "bin" : "";
        }
        return format.value;
      };
      const chosenFile = (): FirmwareFile | undefined =>
        current?.files.find(
          (f) => f.variant === variant.value && f.version === version.value && f.kind === format.value
        );

      const showTarget = () => {
        catalogBox.hidden = !fromCatalog.checked;
        fileBox.hidden = !fromFile.checked;
        const k = kind();
        serialBox.hidden = k === "uf2";
        uf2Box.hidden = k !== "uf2";
        if (k === "uf2" && !volume.options.length) {
          void refreshVolumes();
        }
      };

      const refreshVolumes = async () => {
        volume.replaceChildren(option("", "Find it when flashing"));
        try {
          const found: Array<{ path: string; board_id?: string }> = await rpc.call("firmware_volumes");
          for (const v of found || []) {
            volume.append(option(v.path, v.board_id ? `${v.path} (${v.board_id})` : v.path));
          }
          if (found?.length === 1) {
            volume.value = found[0].path;
          }
        } catch {
          /* the list is a convenience; Flash finds the drive itself */
        }
      };

      const fillVersions = () => {
        const files = (current?.files || []).filter((f) => f.variant === variant.value);
        const versions: FirmwareFile[] = [];
        for (const f of files) {
          if (!versions.some((v) => v.version === f.version)) {
            versions.push(f);
          }
        }
        version.replaceChildren(
          ...versions.map((f) =>
            option(f.version, `${f.version}${f.channel === "preview" ? " (preview)" : ""}, ${day(f.date)}`)
          )
        );
        fillFormats();
      };

      const fillFormats = () => {
        const kinds = new Set(
          (current?.files || [])
            .filter((f) => f.variant === variant.value && f.version === version.value)
            .map((f) => f.kind)
        );
        const names: Record<string, string> = {
          bin: ".bin, written over the serial port with esptool",
          uf2: ".uf2, copied to the UF2 bootloader drive",
        };
        format.replaceChildren(...[...kinds].map((k) => option(k, names[k] || k)));
        if (current && kinds.has(current.preferred)) {
          format.value = current.preferred;
        }
        formatField.hidden = kinds.size < 2;
        showTarget();
      };

      const loadBoard = async () => {
        const typed = boardInput.value.trim();
        const id = boardIds.get(typed.toUpperCase());
        current = null;
        variant.replaceChildren();
        version.replaceChildren();
        format.replaceChildren();
        boardLink.textContent = "";
        if (!id) {
          if (typed) {
            say(`${typed} isn't in the list. Type part of the board's name and pick it.`, "error");
          }
          return;
        }
        say(`Looking up ${id}'s firmware…`);
        try {
          const info: BoardFiles = await rpc.call("firmware_board", { board: id });
          if (boardIds.get(boardInput.value.trim().toUpperCase()) !== id) {
            return; // the board changed while this one loaded
          }
          current = info;
          remember(id);
          boardLink.href = info.info_url;
          boardLink.textContent = `${info.title}: board page and install notes`;
          if (!info.files.length) {
            say(`micropython.org has no image for ${id} that mpftp can flash; see its board page.`, "error");
            return;
          }
          const variants = [...new Set(info.files.map((f) => f.variant))].sort();
          variant.replaceChildren(
            ...variants.map((v) => {
              const note = v ? info.variantNotes[v] : "";
              return option(v, v ? (note ? `${v}: ${note}` : v) : "Default");
            })
          );
          variant.value = variants.includes("") ? "" : variants[0];
          fillVersions();
          say("");
        } catch (e: any) {
          say(`Couldn't load ${id}'s firmware list: ${e?.message || e}`, "error");
        }
      };

      const loadCatalog = async () => {
        try {
          const tree: { ports: CatalogPort[] } = await rpc.call("firmware_catalog");
          for (const p of tree.ports || []) {
            for (const b of p.boards || []) {
              boardList.append(option(b.label, `${p.port}${b.family ? ", " + b.family : ""}`));
              boardIds.set(b.label.toUpperCase(), b.board);
              boardIds.set(b.board.toUpperCase(), b.board);
            }
          }
          boardInput.placeholder = "Type part of a name: Pico, ESP32-S3, XIAO…";
          const last = remembered();
          if (last && boardIds.has(last.toUpperCase()) && !boardInput.value) {
            boardInput.value = last;
            await loadBoard();
          }
        } catch (e: any) {
          boardInput.placeholder = "";
          say(`Couldn't load the firmware catalog: ${e?.message || e}. A file on this computer still works.`, "error");
        }
      };

      const setBusy = (on: boolean) => {
        busy = on;
        for (const c of [fromCatalog, fromFile, boardInput, variant, version, format, file, port, erase, volume, lookAgain, flash, close]) {
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

      /** Get the image onto the server; returns its name, or "" after saying why not. */
      const stage = async (): Promise<string> => {
        if (fromCatalog.checked) {
          const chosen = chosenFile();
          if (!current || !chosen) {
            say("Choose a board and a version first.", "error");
            return "";
          }
          await rpc.call("firmware_fetch", { board: current.board, url: chosen.url });
          addLog(`[mpftp] downloaded ${chosen.url}`);
          return chosen.name;
        }
        const chosen = file.files?.[0];
        if (!chosen || !chosen.size) {
          say("Choose a firmware .bin or .uf2 first.", "error");
          return "";
        }
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
        return chosen.name;
      };

      const run = async () => {
        const k = kind();
        if (!k) {
          say(fromFile.checked ? "Choose a firmware .bin or .uf2 first." : "Choose a board and a version first.", "error");
          return;
        }
        const device = port.value.trim();
        if (k === "bin" && !device) {
          say("Type the board's serial port, or connect to it first.", "error");
          return;
        }
        setBusy(true);
        log.textContent = "";
        bar.hidden = false;
        bar.value = 0;
        const unlisten = listen();
        try {
          const name = await stage();
          if (!name) {
            return;
          }
          bar.value = 0;
          let params: Record<string, unknown>;
          if (k === "uf2") {
            // With no drive chosen the server may restart the connected board.
            if (opts.device && !volume.value) {
              await opts.release();
            }
            params = { volume: volume.value, port: current?.port || "" };
          } else {
            if (device === opts.device) {
              await opts.release();
            }
            params = { device, erase: erase.checked };
          }
          const res: FlashResult = await rpc.call("firmware_flash", params);
          if (res.ok) {
            bar.value = 100;
            say(
              k === "uf2"
                ? `Flashed ${name} through ${res.port}.`
                : `Flashed ${name} (${res.chip} at ${res.offset}) on ${res.port}.`,
              "ok"
            );
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
          if (k === "uf2") {
            void refreshVolumes();
          }
        }
      };

      // --- wiring --------------------------------------------------------------
      void rpc
        .call("list_ports")
        .then((ports: Array<{ device: string; description?: string }>) => {
          for (const p of ports || []) {
            portList.append(option(p.device, p.description || ""));
          }
          if (!port.value && ports?.length) {
            port.value = ports[0].device;
          }
        })
        .catch(() => undefined);
      fromCatalog.addEventListener("change", showTarget);
      fromFile.addEventListener("change", showTarget);
      file.addEventListener("change", showTarget);
      boardInput.addEventListener("change", () => void loadBoard());
      variant.addEventListener("change", fillVersions);
      version.addEventListener("change", fillFormats);
      format.addEventListener("change", showTarget);
      lookAgain.addEventListener("click", () => void refreshVolumes());
      flash.addEventListener("click", () => void run());
      close.addEventListener("click", () => finish(null));

      body.append(
        h(
          "p",
          { class: "mp-dialog-hint" },
          "Download MicroPython for a board from micropython.org, or use a file you have, " +
            "and write it: an ESP .bin over the serial port, a .uf2 to the board's UF2 drive."
        ),
        h(
          "div",
          { class: "mp-choices" },
          sourceChoice(fromCatalog, "Download", "micropython.org"),
          sourceChoice(fromFile, "File", "on this computer")
        ),
        catalogBox,
        fileBox,
        serialBox,
        uf2Box,
        statusLine,
        bar,
        log,
        buttons(close, flash)
      );
      showTarget();
      void loadCatalog();
    },
    { canCancel: () => !busy }
  ).then(() => undefined);
}
