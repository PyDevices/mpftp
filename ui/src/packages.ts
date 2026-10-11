/**
 * Search Packages: find a package without knowing its exact name.
 *
 * The server's package_search looks through micropython-lib, the PyDevices
 * index and the CircuitPython bundles (only the ones the connected board can
 * install from) and ranks the hits. Picking one resolves with that result;
 * the caller runs its install (result.install.method with .params, the same
 * mip_install / circup_install calls the Install Package command makes).
 */
import type { Rpc } from "./rpc";
import { button, buttons, h, openDialog } from "./wifi";

export interface PackageResult {
  name: string;
  source: string;
  source_label: string;
  interpreter: string;
  version: string;
  description: string;
  install: { command: string; method: string; params: Record<string, unknown> };
}

export interface SearchAnswer {
  query: string;
  results: PackageResult[];
  total: number;
  offline: boolean;
  note: string | null;
}

export function searchPackagesDialog(rpc: Rpc, interpreter: string): Promise<PackageResult | null> {
  return openDialog<PackageResult>("Search packages", (body, finish) => {
    const input = h("input", {
      type: "text",
      placeholder: "words from the name or description",
      spellcheck: false,
      "aria-label": "Search packages",
    });
    const note = h("p", { class: "mp-dialog-hint" }, searchHint(interpreter));
    const list = h("div", { class: "mp-device-list mp-package-list" });
    const search = button("Search", true);
    const cancel = button("Cancel");

    const row = (r: PackageResult) => {
      const b = button("");
      b.classList.add("mp-device", "mp-package");
      b.append(
        h("span", { class: "mp-device-name" }, r.name + (r.version ? ` ${r.version}` : "")),
        h("span", { class: "mp-device-detail" }, r.description || r.source_label)
      );
      b.title = `${r.source_label}\n${r.install.command}`;
      b.addEventListener("click", () => finish(r));
      return b;
    };

    const run = async () => {
      const query = input.value.trim();
      if (!query) {
        input.focus();
        return;
      }
      search.disabled = true;
      note.textContent = "Searching…";
      list.replaceChildren();
      try {
        const res: SearchAnswer = await rpc.call("package_search", { query, interpreter, limit: 50 });
        for (const r of res.results) {
          list.append(row(r));
        }
        note.textContent = resultNote(res);
      } catch (e: any) {
        note.textContent = `Search failed: ${e?.message || e}`;
      } finally {
        search.disabled = false;
      }
    };

    input.addEventListener("keydown", (ev) => {
      if (ev.key === "Enter") {
        ev.preventDefault();
        void run();
      }
    });
    search.addEventListener("click", () => void run());
    cancel.addEventListener("click", () => finish(null));
    body.append(h("label", { class: "mp-field" }, input), note, list, buttons(cancel, search));
    setTimeout(() => input.focus(), 0);
  });
}

export function searchHint(interpreter: string): string {
  if (interpreter === "circuitpython") {
    return "Searches the Adafruit and community CircuitPython bundles. Pick a result to install it with circup.";
  }
  return "Searches micropython-lib and the PyDevices index. Pick a result to install it with mip.";
}

/** The line under the search box once an answer is in. */
export function resultNote(res: SearchAnswer): string {
  let text: string;
  if (!res.results.length) {
    text = `Nothing matches "${res.query}".`;
  } else if (res.total > res.results.length) {
    text = `${res.results.length} of ${res.total} matches; add a word to narrow it. Pick one to install it.`;
  } else {
    text = `${res.total} ${res.total === 1 ? "match" : "matches"}. Pick one to install it.`;
  }
  return res.note ? `${text} ${res.note}` : text;
}
