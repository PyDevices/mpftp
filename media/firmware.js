/* global acquireVsCodeApi */
(function () {
  const vscode = acquireVsCodeApi();

  const model = {
    host: "",
    micropython: "",
    workspace: "",
    tree: [],
    cmods: { modules: [], hasAggregator: false, hasManifest: false },
    flashers: {},
    selection: { port: "", board: "", variant: "" },
    prefs: {
      reconnectAfterFlash: false,
      alsoFlashAfterBuild: false,
      device: "",
      firmwareSource: "build",
      downloadVersion: "",
      downloadPreview: false,
    },
    downloadVersions: [],
    downloadFamily: "",
    connectedDevice: "",
    devices: [],
    detect: null,
    detecting: false,
    detectStatus: null,
    artifact: { ready: false },
    flashOffset: "",
    flashBaud: "460800",
    flashBefore: "default-reset",
    flashAfter: "hard-reset",
    flashErase: false,
    flashEraseHint: false,
    flashStatus: null,
    flashing: false,
    phase: "idle",
    phaseText: "Idle",
    filter: "",
    busy: false,
    cleanBuild: false,
  };

  function isDownloadMode() {
    return model.prefs.firmwareSource === "download";
  }

  const $ = (sel, root) => (root || document).querySelector(sel);
  const el = (tag, cls, txt) => {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    if (txt != null) e.textContent = txt;
    return e;
  };

  function humanSize(n) {
    if (!n && n !== 0) return "";
    if (n < 1024) return n + " B";
    if (n < 1024 * 1024) return (n / 1024).toFixed(1) + " KB";
    return (n / 1024 / 1024).toFixed(2) + " MB";
  }

  function relTime(mtime) {
    if (!mtime) return "";
    const secs = Math.max(0, Math.floor(Date.now() / 1000 - mtime));
    if (secs < 60) return "just now";
    if (secs < 3600) return Math.floor(secs / 60) + " min ago";
    if (secs < 86400) return Math.floor(secs / 3600) + " h ago";
    return Math.floor(secs / 86400) + " d ago";
  }

  // ---------------------------------------------------------------- render

  function render() {
    const app = $("#app");
    app.innerHTML = "";

    app.appendChild(renderHeader());

    const grid = el("div", "grid");
    const detect = renderDetect();
    detect.classList.add("step-span");
    grid.appendChild(detect);
    grid.appendChild(renderSource());
    grid.appendChild(renderTarget());
    if (isDownloadMode()) {
      grid.appendChild(renderDownload());
    } else if (model.micropython) {
      grid.appendChild(renderBuild());
    } else {
      grid.appendChild(renderBuildNeedsMp());
    }
    grid.appendChild(renderFlash());
    app.appendChild(grid);
    app.appendChild(renderLog());
  }

  function renderHeader() {
    const h = el("header", "hdr");

    const top = el("div", "hdr-top");
    const brand = el("div", "brand");
    brand.appendChild(el("span", "brand-mark", "mpftp"));
    brand.appendChild(el("span", "brand-title", "Firmware"));
    top.appendChild(brand);
    const meta = el("div", "meta-row");
    meta.appendChild(el("span", "hdr-label", "Environment"));
    meta.appendChild(
      pill(model.host, "ok", "Host platform running the build (" + model.host + ")")
    );
    top.appendChild(meta);
    h.appendChild(top);

    const below = el("div", "hdr-below");
    below.appendChild(
      el(
        "p",
        "subtitle",
        "Detect the board, choose Build or Download, pick a target, then build or download firmware and flash."
      )
    );
    h.appendChild(below);
    return h;
  }

  function pill(text, kind, title) {
    const p = el("span", "pill " + (kind || ""), text);
    if (title) p.title = title;
    return p;
  }

  function renderNoMp() {
    const c = el("div", "card empty");
    c.appendChild(el("h2", null, "Firmware workspace not set"));
    c.appendChild(
      el(
        "p",
        "muted",
        "Choose a folder that contains micropython/ (or is the MicroPython tree). Port SDKs can live there as directories or symlinks, or via environment variables."
      )
    );
    const b = el("button", "btn primary", "Choose workspace…");
    b.onclick = () => vscode.postMessage({ type: "chooseWorkspace" });
    c.appendChild(b);
    return c;
  }

  function renderBuildNeedsMp() {
    const card = el("section", "card step");
    card.appendChild(stepHead("4", "Build", "no checkout"));
    card.appendChild(
      el(
        "p",
        "muted",
        "Choose a firmware workspace with MicroPython, or switch Select to Download for official firmware."
      )
    );
    const b = el("button", "btn primary", "Choose workspace…");
    b.onclick = () => vscode.postMessage({ type: "chooseWorkspace" });
    card.appendChild(b);
    return card;
  }

  // Step 1 — Detect (esptool-first)
  function mpMachine(d) {
    const mp = (d && d.mp) || {};
    return mp.machine || mp.platform || "";
  }

  function chipTitle(d) {
    if (d.espressif === false) {
      const port = (d.match && d.match.port) || d.suggestedPort || "";
      const name = mpMachine(d) || "Non-Espressif device";
      return name + (port ? " · firmware port: " + port : "");
    }
    const parts = [];
    let head = d.chip || "ESP32";
    if (d.revision) head += " (rev " + d.revision + ")";
    parts.push(head);
    parts.push((d.cores === 1 ? "single" : "dual") + (d.lpCore ? " + LP" : ""));
    if (d.maxMhz) parts.push(d.maxMhz + " MHz");
    if (d.flashMb) parts.push(d.flashMb + " MB flash");
    if (d.psram && d.psram.present) parts.push(d.psram.octal ? "Octal PSRAM" : "PSRAM");
    const m = d.match || {};
    if (m.board) parts.push(m.board + (m.variant ? " / " + m.variant : ""));
    return parts.join(" · ");
  }

  function infoRow(label, value) {
    if (value == null || value === "") return null;
    const row = el("div", "info-row");
    row.appendChild(el("span", "info-label", label));
    row.appendChild(el("span", "info-value", String(value)));
    return row;
  }

  function infoGroup(title, rows) {
    const kept = rows.filter(Boolean);
    if (!kept.length) return null;
    const g = el("div", "info-group");
    g.appendChild(el("div", "info-group-title", title));
    for (const r of kept) g.appendChild(r);
    return g;
  }

  // PSRAM display: prefer esptool's embedded size, else the MicroPython
  // interpreter probe (largest PSRAM heap bank), else just presence/type.
  function psramValue(d, mp) {
    const p = (d && d.psram) || {};
    const bytes = mp && mp.psramBytes;
    if (!p.present && !bytes) return null;
    const parts = [];
    if (p.sizeMb) parts.push(p.sizeMb + " MB");
    else if (bytes) parts.push("~" + humanSize(bytes));
    if (p.octal) parts.push("Octal");
    if (!parts.length) parts.push(p.label || "yes");
    return parts.join(" · ");
  }

  function renderDetect() {
    const card = el("section", "card step");
    const btn = el("button", "btn accent sm", model.detecting ? "Detecting…" : "Detect");
    btn.disabled = model.busy || model.detecting || model.flashing;
    btn.title = "Probe the selected device with esptool (bare board OK)";
    btn.onclick = () => vscode.postMessage({ type: "detect" });
    const chip = model.prefs.device
      ? model.prefs.device +
        (model.detect && model.detect.chip ? " · " + model.detect.chip : "")
      : "no device";
    card.appendChild(stepHead("1", "Detect", chip, btn));

    const ds = model.detectStatus;
    if (ds && ds.state === "failed") {
      card.appendChild(el("p", "info-status err", ds.text || "Detect failed"));
    }

    const d = model.detect;
    if (!d) {
      if (model.detecting) {
        card.appendChild(el("p", "muted", "Detecting…"));
      } else if (!(ds && ds.state === "failed")) {
        card.appendChild(
          el("p", "muted", "No device selected — click Detect to inspect.")
        );
      }
      return card;
    }

    card.appendChild(el("div", "chip-title", chipTitle(d)));

    const mp = d.mp || {};
    const hasMp = !!(mp.platform || mp.machine);
    const grid = el("div", "info-grid");
    // infoGroup returns null when every row is empty; never appendChild(null).
    const addGroup = (g) => {
      if (g) grid.appendChild(g);
    };

    if (d.espressif === false) {
      addGroup(
        infoGroup("Identity", [
          infoRow("Board", mpMachine(d)),
          infoRow("Platform", mp.platform),
          infoRow("Firmware port", (d.match && d.match.port) || d.suggestedPort),
        ])
      );
      addGroup(
        infoGroup("Interpreter", [
          infoRow("MicroPython", hasMp ? mp.impl || "yes" : null),
          infoRow("Frequency", mp.freq ? fmtFreq(mp.freq) : null),
          infoRow("Free heap", mp.memfree ? humanSize(mp.memfree) : null),
        ])
      );
      card.appendChild(grid);
      card.appendChild(
        el(
          "p",
          "muted sm",
          "Not an Espressif chip — " +
            (d.reason || "esptool did not detect an ESP") +
            ". Build/flash uses the suggested port."
        )
      );
      return card;
    }

    addGroup(
      infoGroup("Identity", [
        infoRow("Chip", d.chip),
        infoRow("Revision", d.revision),
        infoRow("MAC", d.mac),
      ])
    );
    addGroup(
      infoGroup("Performance", [
        infoRow("Cores", (d.cores || "") + (d.lpCore ? " + LP" : "")),
        infoRow("Max clock", d.maxMhz ? d.maxMhz + " MHz" : null),
        infoRow("Current", hasMp && mp.freq ? fmtFreq(mp.freq) : null),
      ])
    );
    addGroup(
      infoGroup("Memory", [
        infoRow("SRAM", d.sramKb ? d.sramKb + " KB" : null),
        infoRow("PSRAM", psramValue(d, mp)),
        infoRow("Flash storage", d.flashMb ? d.flashMb + " MB" : null),
        infoRow("Free heap", hasMp && mp.memfree ? humanSize(mp.memfree) : null),
      ])
    );
    const m = d.match || {};
    addGroup(
      infoGroup("Build target", [
        infoRow("Board", m.board),
        infoRow("Variant", m.variant || "default"),
        infoRow("Flash size", m.flashSize),
      ])
    );
    addGroup(
      infoGroup("Security", [
        infoRow(
          "Flash encryption",
          d.security && d.security.available ? d.security.flashEncryption : "unavailable"
        ),
        infoRow(
          "Secure boot",
          d.security && d.security.available ? d.security.secureBoot : "unavailable"
        ),
      ])
    );
    card.appendChild(grid);

    // Prefer the running firmware's full boot banner (reconstructed from
    // os.uname); fall back to impl · build name when the version isn't known.
    const buildName = mp.build ? " · " + mp.build : "";
    const running = mp.version || (mp.impl || "mpy") + buildName;
    const status = hasMp
      ? "MicroPython running (" + running + ")"
      : "No MicroPython (bootloader / other firmware)";
    card.appendChild(el("div", "info-status", status));

    if (m.confidence && m.confidence !== "matched") {
      card.appendChild(
        pill(
          m.confidence === "family-only" ? "family match" : "unconfirmed",
          "muted"
        )
      );
    }
    if (m.variantOptions && m.variantOptions.length) {
      card.appendChild(
        el("p", "hint sm", "Variant options: " + m.variantOptions.join(", "))
      );
    }
    for (const n of m.notes || []) {
      card.appendChild(el("p", "hint sm", n));
    }
    return card;
  }

  function fmtFreq(freq) {
    if (Array.isArray(freq)) return freq[0] / 1e6 + " MHz";
    if (typeof freq === "number") return Math.round(freq / 1e6) + " MHz";
    return String(freq);
  }

  // Step 3 — Target
  function renderTarget() {
    const card = el("section", "card step");
    card.appendChild(stepHead("3", "Target", chipText()));

    const search = el("input", "search");
    search.placeholder = "Filter ports and boards…";
    search.value = model.filter;
    search.oninput = (e) => {
      model.filter = e.target.value.toLowerCase();
      renderTree(treeWrap);
    };
    card.appendChild(search);

    const treeWrap = el("div", "tree");
    renderTree(treeWrap);
    card.appendChild(treeWrap);
    return card;
  }

  function chipText() {
    const s = model.selection;
    if (!s.port) return "none selected";
    // Download mode also has MP variants (e.g. ESP32_GENERIC_P4 / C6_WIFI).
    return [s.port, s.board || null, s.variant || "default"].filter(Boolean).join(" / ");
  }

  // Step 2 — Select Build or Download
  function renderSource() {
    const card = el("section", "card step");
    const mode = isDownloadMode() ? "Download" : "Build";
    card.appendChild(stepHead("2", "Select", mode));

    const sourceRow = el("div", "source-row");
    const seg = el("div", "seg");
    for (const [id, label] of [
      ["download", "Download"],
      ["build", "Build"],
    ]) {
      const btn = el(
        "button",
        "seg-btn" + (model.prefs.firmwareSource === id ? " active" : ""),
        label
      );
      btn.onclick = () => {
        if (model.prefs.firmwareSource === id) return;
        model.prefs.firmwareSource = id;
        vscode.postMessage({ type: "setSource", source: id });
        render();
      };
      seg.appendChild(btn);
    }
    sourceRow.appendChild(seg);
    card.appendChild(sourceRow);

    if (isDownloadMode()) {
      card.appendChild(
        el(
          "p",
          "muted sm",
          "Official builds via Thonny’s catalog → micropython.org (no local checkout required)."
        )
      );
    } else {
      const pathRow = el("div", "path-row");
      pathRow.appendChild(el("span", "hdr-label", "Workspace"));
      pathRow.appendChild(el("i", "codicon codicon-root-folder"));
      const pathText = el(
        "span",
        "path-text",
        model.workspace || model.micropython || "No workspace set"
      );
      pathText.title =
        (model.workspace || "") +
        (model.micropython ? "\nMicroPython: " + model.micropython : "");
      pathRow.appendChild(pathText);
      const change = el("button", "btn ghost sm", "Change…");
      change.title = "Choose firmware workspace (open folder or Browse…)";
      change.onclick = () => vscode.postMessage({ type: "chooseWorkspace" });
      pathRow.appendChild(change);
      card.appendChild(pathRow);
      card.appendChild(renderModules());
    }
    return card;
  }

  function matches(text) {
    return !model.filter || text.toLowerCase().includes(model.filter);
  }

  function renderTree(wrap) {
    wrap.innerHTML = "";
    for (const port of model.tree) {
      const portMatch = matches(port.port);
      const boards = (port.boards || []).filter(
        (b) => portMatch || matches(b.board)
      );
      const variants = (port.variants || []).filter((v) => portMatch || matches(v));
      if (!portMatch && !boards.length && !variants.length) continue;

      const node = el("div", "tnode");
      const head = el("div", "trow port");
      const caret = el("i", "codicon codicon-chevron-right caret");
      head.appendChild(caret);
      head.appendChild(el("span", "tname", port.port));
      if (port.flashable) head.appendChild(pill(port.flasher, "flash"));
      else head.appendChild(pill("build-only", "muted"));

      const kids = el("div", "tkids");
      const expanded =
        model.selection.port === port.port || !!model.filter;
      if (expanded) {
        node.classList.add("open");
      }
      head.onclick = () => node.classList.toggle("open");

      if (port.kind === "boards") {
        for (const b of boards) {
          kids.appendChild(renderBoard(port, b));
        }
      } else if (port.kind === "variants") {
        for (const v of variants) {
          kids.appendChild(renderLeaf(port.port, "", v, v));
        }
        if (portMatch) {
          kids.appendChild(renderLeaf(port.port, "", "", "default"));
        }
      } else {
        // plain port
        head.classList.add("selectable");
        head.onclick = () => selectTriple(port.port, "", "");
      }
      node.appendChild(head);
      node.appendChild(kids);
      wrap.appendChild(node);
    }
    if (!wrap.children.length) {
      wrap.appendChild(el("p", "muted pad", "No matches."));
    }
  }

  function renderBoard(port, b) {
    if (b.variants && b.variants.length) {
      const node = el("div", "tnode");
      const head = el("div", "trow board");
      const caret = el("i", "codicon codicon-chevron-right caret");
      head.appendChild(caret);
      head.appendChild(el("span", "tname", b.board));
      head.appendChild(pill(b.variants.length + " variants", "muted"));
      const open =
        model.selection.port === port.port && model.selection.board === b.board;
      if (open) node.classList.add("open");
      head.onclick = () => node.classList.toggle("open");
      const kids = el("div", "tkids");
      kids.appendChild(renderLeaf(port.port, b.board, "", "default"));
      for (const v of b.variants) {
        kids.appendChild(renderLeaf(port.port, b.board, v, v));
      }
      node.appendChild(head);
      node.appendChild(kids);
      return node;
    }
    return renderLeaf(port.port, b.board, "", b.label || b.board);
  }

  function renderLeaf(port, board, variant, label) {
    const s = model.selection;
    const selected =
      s.port === port && s.board === board && s.variant === variant;
    const leaf = el("div", "trow leaf selectable" + (selected ? " selected" : ""));
    leaf.appendChild(el("span", "tdot"));
    leaf.appendChild(el("span", "tname", label));
    if (selected) leaf.appendChild(el("i", "codicon codicon-check"));
    leaf.onclick = () => selectTriple(port, board, variant);
    return leaf;
  }

  function selectTriple(port, board, variant) {
    model.selection = { port, board, variant };
    // Let the new board's default offset re-populate the field, and drop any
    // stale flash result from the previous board.
    model.flashOffset = "";
    model.flashStatus = null;
    model.flashing = false;
    vscode.postMessage({ type: "select", port, board, variant });
    render();
  }

  // Modules — discovery sub-card inside Select when Build is chosen.
  // Scanned from the workspace (parent of the MicroPython checkout); repoint
  // via Select's Change… control.
  function renderModules() {
    const card = el("div", "sub-card");
    card.appendChild(el("div", "sub-card-title", "Modules"));
    const from = model.workspace || model.micropython;
    if (from) {
      const src = el("p", "discovered-from");
      src.appendChild(el("span", "discovered-label", "Discovered from"));
      const path = el("span", "discovered-path", from);
      path.title = from;
      src.appendChild(path);
      card.appendChild(src);
    }
    const cm = model.cmods || {};
    const list = cm.modules || [];
    const needAggregator = cm.hasAggregator !== true;
    const needManifest = cm.hasManifest !== true;
    const needStubs = needAggregator || needManifest;

    if (!list.length) {
      let msg;
      if (needAggregator) {
        msg =
          "No micropython.cmake aggregator in the workspace. Create stubs to enable USER_C_MODULES, then add sibling modules with micropython.cmake / micropython.mk and/or manifest.py.";
      } else {
        msg =
          "Aggregator present — no sibling modules yet. Add folders with micropython.cmake / micropython.mk and/or manifest.py beside the MicroPython checkout.";
      }
      card.appendChild(el("p", "muted", msg));
    } else {
      const chips = el("div", "chips");
      for (const c of list) {
        const chip = el("span", "chip mod");
        chip.appendChild(el("i", "codicon codicon-package"));
        chip.appendChild(el("span", null, c.name));
        const bits = [];
        if (c.kind) bits.push(c.kind);
        if (c.hasManifest && c.kind !== "manifest") bits.push("manifest");
        chip.title = c.path + (bits.length ? " (" + bits.join(", ") + ")" : "");
        chips.appendChild(chip);
      }
      card.appendChild(chips);
    }

    if (needStubs && (model.workspace || model.micropython)) {
      const row = el("div", "actions");
      const create = el(
        "button",
        "btn ghost sm",
        needAggregator && needManifest
          ? "Create stubs…"
          : needAggregator
            ? "Create aggregator…"
            : "Create manifest…"
      );
      create.title =
        "Write micropython.cmake and/or manifest-micropython.py into the workspace from mpftp templates";
      create.onclick = () => vscode.postMessage({ type: "createWorkspaceStubs" });
      row.appendChild(create);
      card.appendChild(row);
    }

    const flags = el("div", "flags");
    if (cm.hasAggregator) {
      flags.appendChild(
        pill(
          "USER_C_MODULES",
          "ok",
          "A micropython.cmake aggregator was found in the workspace — sibling C modules are compiled into the firmware via USER_C_MODULES."
        )
      );
    }
    if (cm.hasManifest) {
      flags.appendChild(
        pill(
          "FROZEN_MANIFEST",
          "ok",
          "A manifest-micropython.py was found in the workspace — its Python modules are frozen into the firmware via FROZEN_MANIFEST."
        )
      );
    }
    if (flags.children.length) card.appendChild(flags);
    return card;
  }

  // Step 2 — Download (official firmware)
  function renderDownload() {
    const card = el("section", "card step");
    card.appendChild(stepHead("4", "Download", chipText()));

    const statusRow = el("div", "status-row");
    statusRow.appendChild(phasePill());
    card.appendChild(statusRow);

    if (!model.selection.board) {
      card.appendChild(
        el("p", "muted", "Select a board in Target. Detect can suggest one from the chip.")
      );
      return card;
    }

    const verRow = el("div", "field-row");
    verRow.appendChild(el("label", null, "Version"));
    const sel = document.createElement("select");
    const latest = document.createElement("option");
    latest.value = "";
    latest.textContent = "Latest release";
    sel.appendChild(latest);
    const prev = document.createElement("option");
    prev.value = "__preview__";
    prev.textContent = "Latest preview";
    sel.appendChild(prev);
    for (const d of model.downloadVersions || []) {
      if (d.channel === "preview") continue;
      const o = document.createElement("option");
      o.value = d.version;
      o.textContent = d.version;
      sel.appendChild(o);
    }
    if (model.prefs.downloadPreview) {
      sel.value = "__preview__";
    } else {
      sel.value = model.prefs.downloadVersion || "";
    }
    sel.onchange = () => {
      if (sel.value === "__preview__") {
        model.prefs.downloadPreview = true;
        model.prefs.downloadVersion = "";
        vscode.postMessage({ type: "setPref", key: "downloadPreview", value: true });
      } else {
        model.prefs.downloadPreview = false;
        model.prefs.downloadVersion = sel.value;
        vscode.postMessage({ type: "setPref", key: "downloadVersion", value: sel.value });
      }
    };
    verRow.appendChild(sel);
    card.appendChild(verRow);

    if (model.artifact && model.artifact.ready) {
      const info = el("div", "artifact");
      info.appendChild(el("i", "codicon codicon-cloud-download"));
      const details = el("div", "artifact-details");
      const full = model.artifact.artifact || "";
      const parts = full.split(/[/\\]/);
      const name = parts.pop() || full;
      details.appendChild(el("div", "artifact-name", name));
      const dir = parts.join(full.includes("\\") ? "\\" : "/");
      if (dir) {
        const dirEl = el("div", "artifact-dir muted sm", dir);
        dirEl.title = full;
        details.appendChild(dirEl);
      }
      const meta = [
        model.artifact.variant ? String(model.artifact.variant) : "default",
        model.artifact.version ? "v" + String(model.artifact.version).replace(/^v/, "") : "",
        humanSize(model.artifact.size),
        model.artifact.source === "local" ? "local file" : "cached",
      ]
        .filter(Boolean)
        .join(" · ");
      details.appendChild(el("div", "muted sm", meta));
      info.appendChild(details);
      card.appendChild(info);
    } else {
      card.appendChild(el("p", "muted", "Not downloaded yet for this selection."));
    }

    const actions = el("div", "actions");
    const dl = el("button", "btn primary", model.busy ? "Working…" : "Download");
    dl.disabled =
      model.busy || model.detecting || model.flashing || !model.selection.board;
    dl.onclick = () => vscode.postMessage({ type: "download" });
    actions.appendChild(dl);
    const browse = el("button", "btn ghost", "Browse local…");
    browse.disabled = model.busy || model.flashing;
    browse.onclick = () => vscode.postMessage({ type: "browseArtifact" });
    actions.appendChild(browse);
    card.appendChild(actions);
    return card;
  }

  // Step 2 — Build
  function renderBuild() {
    const card = el("section", "card step");
    card.appendChild(stepHead("4", "Build", chipText()));

    const statusRow = el("div", "status-row");
    statusRow.appendChild(phasePill());
    card.appendChild(statusRow);

    if (model.artifact && model.artifact.ready) {
      const info = el("div", "artifact");
      info.appendChild(el("i", "codicon codicon-file-binary"));
      const details = el("div", "artifact-details");
      const full = model.artifact.artifact || "";
      const name = full.split("/").pop();
      details.appendChild(el("div", "artifact-name", name));
      const dir = full.slice(0, full.length - name.length).replace(/\/$/, "");
      if (dir) {
        const dirEl = el("div", "artifact-dir muted sm", dir);
        dirEl.title = dir;
        details.appendChild(dirEl);
      }
      details.appendChild(
        el(
          "div",
          "muted sm",
          humanSize(model.artifact.size) +
            " · built " +
            relTime(model.artifact.mtime)
        )
      );
      info.appendChild(details);
      card.appendChild(info);
    } else {
      card.appendChild(el("p", "muted", "Not built yet for this selection."));
    }

    card.appendChild(
      checkbox(
        "Clean build",
        model.cleanBuild,
        (v) => {
          model.cleanBuild = v;
        },
        { title: "Run make clean before building (from-scratch rebuild)." }
      )
    );

    const flashable = selectedPortFlashable();
    card.appendChild(
      prefCheckbox("Also flash after build", "alsoFlashAfterBuild", {
        disabled: !flashable,
        title: flashable
          ? ""
          : "This target builds a binary that isn't flashed to a board.",
      })
    );

    const actions = el("div", "actions");
    const build = el("button", "btn primary", model.busy ? "Working…" : "Build");
    build.disabled =
      model.busy || model.detecting || model.flashing || !model.selection.port;
    build.onclick = () =>
      vscode.postMessage({ type: "build", clean: !!model.cleanBuild });
    actions.appendChild(build);
    card.appendChild(actions);
    return card;
  }

  // Step 5 — Flash
  function renderFlash() {
    const card = el("section", "card step");
    card.appendChild(stepHead("5", "Flash", ""));

    const fp = flashPill();
    if (fp) {
      const statusRow = el("div", "status-row");
      statusRow.appendChild(fp);
      card.appendChild(statusRow);
    }

    const port = model.selection.port;
    const flasher = model.flashers[port];
    if (!flasher) {
      card.appendChild(
        el(
          "p",
          "muted",
          port
            ? `Flashing is not supported for '${port}' — build only.`
            : "Select a target to flash."
        )
      );
      return card;
    }

    const devRow = el("div", "device-row");
    const devInfo = el("div", "device-info");
    devInfo.appendChild(el("i", "codicon codicon-plug"));
    const dev = model.prefs.device || "No device selected";
    const devSpan = el("span", "device-name", dev);
    if (model.prefs.device && model.prefs.device === model.connectedDevice) {
      devSpan.appendChild(el("span", "conn-badge", " connected"));
    }
    devInfo.appendChild(devSpan);
    devRow.appendChild(devInfo);

    const refresh = el("button", "btn ghost icon", "");
    refresh.appendChild(el("i", "codicon codicon-refresh"));
    refresh.title = "Refresh devices";
    refresh.onclick = () => vscode.postMessage({ type: "refreshDevices" });
    devRow.appendChild(refresh);

    const change = el("button", "btn ghost sm", "Change…");
    change.onclick = () => vscode.postMessage({ type: "changeDevice" });
    devRow.appendChild(change);
    card.appendChild(devRow);

    const ready = model.artifact && model.artifact.ready;
    const needsDevice = port === "esp32" && !model.prefs.device;
    // flash tip uses ready below

    // esp32 only: editable flash offset + baud, between the device row and Flash.
    if (port === "esp32") {
      // Two-column grid: offset | baud on the first row, reset-before | reset-after
      // on the second. The erase checkbox sits full-width below.
      const opts = el("div", "flash-opts");

      const offRow = el("div", "offset-row");
      offRow.appendChild(el("span", "offset-label", "Flash offset"));
      const off = document.createElement("input");
      off.type = "text";
      off.className = "offset-input";
      off.value = model.flashOffset || "";
      off.placeholder = (model.artifact && model.artifact.flashOffset) || "0x0";
      off.title = "Where the merged firmware.bin is written (board default shown)";
      off.oninput = () => {
        model.flashOffset = off.value.trim();
      };
      offRow.appendChild(off);
      opts.appendChild(offRow);

      const baudRow = el("div", "offset-row");
      baudRow.appendChild(el("span", "offset-label", "Baud rate"));
      const baud = document.createElement("input");
      baud.type = "text";
      baud.inputMode = "numeric";
      baud.className = "offset-input";
      baud.setAttribute("list", "baud-rates");
      baud.value = model.flashBaud || "";
      baud.placeholder = "460800";
      baud.title = "esptool baud rate — pick a common rate or type your own";
      baud.oninput = () => {
        model.flashBaud = baud.value.trim();
      };
      baudRow.appendChild(baud);
      const rates = document.createElement("datalist");
      rates.id = "baud-rates";
      for (const r of ["115200", "230400", "460800", "921600", "1500000"]) {
        const opt = document.createElement("option");
        opt.value = r;
        rates.appendChild(opt);
      }
      baudRow.appendChild(rates);
      opts.appendChild(baudRow);

      opts.appendChild(
        selectRow(
          "Reset before",
          [
            ["default-reset", "default-reset"],
            ["no-reset", "no-reset"],
            ["usb-reset", "usb-reset"],
          ],
          model.flashBefore,
          (v) => {
            model.flashBefore = v;
          },
          "esptool --before: how the chip enters the bootloader. Use no-reset when you've manually put the board in download mode."
        )
      );
      opts.appendChild(
        selectRow(
          "Reset after",
          [
            ["hard-reset", "hard-reset"],
            ["soft-reset", "soft-reset"],
            ["no-reset", "no-reset"],
          ],
          model.flashAfter,
          (v) => {
            model.flashAfter = v;
          },
          "esptool --after: what the chip does when flashing finishes."
        )
      );

      card.appendChild(opts);

      card.appendChild(
        checkbox(
          "Erase flash before writing",
          model.flashErase,
          (v) => {
            model.flashErase = v;
            if (v) {
              model.flashEraseHint = false;
            }
          },
          {
            title:
              "Full chip erase before flashing. Required when the partition table changes — wipes the filesystem (vfs/storage) partition and all board files.",
          }
        )
      );
      if (model.flashEraseHint) {
        const warn = el(
          "p",
          "hint warn",
          "Partition table differs from this firmware. Enable Erase and click Flash again — " +
            "this wipes the filesystem (vfs/storage) partition; all board files will be lost."
        );
        card.appendChild(warn);
      }
    }

    const actions = el("div", "actions");
    const flash = el("button", "btn accent", model.flashing ? "Flashing…" : "Flash");
    flash.disabled =
      model.busy || model.detecting || model.flashing || !ready || needsDevice;
    if (!ready) {
      flash.title = isDownloadMode()
        ? "Download firmware first"
        : "Build this board first";
    } else if (needsDevice) flash.title = "Select a device";
    flash.onclick = () =>
      vscode.postMessage({
        type: "flash",
        offset: model.flashOffset || "",
        baud: model.flashBaud || "",
        before: model.flashBefore || "",
        after: model.flashAfter || "",
        erase: !!model.flashErase,
      });
    actions.appendChild(flash);

    card.appendChild(prefCheckbox("Reconnect after flash", "reconnectAfterFlash"));

    card.appendChild(actions);

    if (ready) {
      card.appendChild(
        el(
          "p",
          "hint sm",
          "Tip: swap boards and click Flash again — no rebuild needed."
        )
      );
    }
    return card;
  }

  // Log
  function renderLog() {
    const card = el("section", "card log-card");
    const head = el("div", "log-head");
    head.appendChild(el("span", "log-title", "Build & flash log"));
    const cancel = el("button", "btn ghost sm", "Cancel");
    cancel.disabled = !model.busy;
    cancel.onclick = () => vscode.postMessage({ type: "cancel" });
    head.appendChild(cancel);
    card.appendChild(head);
    const pre = el("pre", "log");
    pre.id = "logPre";
    if (logBuffer.length) pre.textContent = logBuffer.join("\n");
    card.appendChild(pre);
    return card;
  }

  function stepHead(n, title, chip, trailing) {
    const h = el("div", "step-head");
    if (n) h.appendChild(el("span", "step-num", n));
    h.appendChild(el("span", "step-title", title));
    if (chip) h.appendChild(el("span", "sel-chip", chip));
    if (trailing) {
      trailing.classList.add("step-trailing");
      h.appendChild(trailing);
    }
    return h;
  }

  function phasePill() {
    const map = {
      idle: ["Idle", "muted"],
      building: ["Building", "run"],
      ready: ["Ready", "ok"],
      failed: ["Failed", "err"],
    };
    const [label, kind] = map[model.phase] || ["Idle", "muted"];
    const p = el("span", "phase-pill " + kind);
    if (model.phase === "building") {
      p.appendChild(el("i", "codicon codicon-loading spin"));
    }
    p.appendChild(el("span", null, model.phaseText || label));
    return p;
  }

  // Flash-step status pill (separate from the Build phase pill).
  function flashPill() {
    const fs = model.flashStatus;
    if (!fs || !fs.state) return null;
    const map = {
      flashing: ["Flashing", "run"],
      ok: ["Flashed", "ok"],
      failed: ["Failed", "err"],
    };
    const [label, kind] = map[fs.state] || ["", "muted"];
    const p = el("span", "phase-pill " + kind);
    if (fs.state === "flashing") {
      p.appendChild(el("i", "codicon codicon-loading spin"));
    }
    p.appendChild(el("span", null, fs.text || label));
    return p;
  }

  // Labeled <select> laid out like the offset/baud rows. options is a list of
  // [value, label] pairs; current is the selected value.
  function selectRow(label, options, current, onChange, title) {
    const row = el("div", "offset-row");
    const lab = el("span", "offset-label", label);
    if (title) lab.title = title;
    row.appendChild(lab);
    const sel = document.createElement("select");
    sel.className = "offset-select";
    if (title) sel.title = title;
    for (const [value, text] of options) {
      const opt = document.createElement("option");
      opt.value = value;
      opt.textContent = text;
      if (value === current) opt.selected = true;
      sel.appendChild(opt);
    }
    sel.onchange = () => onChange(sel.value);
    row.appendChild(sel);
    return row;
  }

  function checkbox(label, checked, onChange, opts) {
    const wrap = el("label", "check");
    const input = document.createElement("input");
    input.type = "checkbox";
    input.checked = !!checked;
    input.onchange = (e) => onChange(e.target.checked);
    if (opts && opts.disabled) {
      input.disabled = true;
      wrap.classList.add("disabled");
      if (opts.title) wrap.title = opts.title;
    }
    wrap.appendChild(input);
    wrap.appendChild(el("span", null, label));
    return wrap;
  }

  // Checkbox bound to a persisted pref. Updates the local model on toggle so a
  // later render() (e.g. from a build/flash phase change) reflects the user's
  // actual choice instead of reverting to the last pushed state.
  //
  // When disabled (opts.disabled), the box renders unchecked and read-only
  // without touching the persisted pref, so the user's real choice is restored
  // once a flashable target is selected again.
  function prefCheckbox(label, key, opts) {
    const disabled = !!(opts && opts.disabled);
    return checkbox(
      label,
      disabled ? false : model.prefs[key],
      (v) => {
        model.prefs[key] = v;
        vscode.postMessage({ type: "setPref", key, value: v });
      },
      opts
    );
  }

  // The tree node for the currently selected port carries its .flashable flag;
  // build-only ports (unix/windows/webassembly) produce non-flashable binaries.
  function selectedPortFlashable() {
    const p = model.selection && model.selection.port;
    if (!p) return true;
    const node = (model.tree || []).find((t) => t.port === p);
    return node ? !!node.flashable : true;
  }

  // ---------------------------------------------------------------- log buffer
  let logBuffer = [];

  // esptool emits one "Writing at 0x… NN.N% …/… bytes…" line per progress tick
  // (text-mode reads split on \r), which would flood the log. Collapse a run of
  // these into a single entry that updates in place, with a blank line above the
  // bar and a blank line below once it finishes.
  const WRITE_PROGRESS_RE = /Writing at 0x[0-9a-fA-F]+.*%.*bytes\.\.\./;
  let inWriteProgress = false;

  function appendLog(line) {
    const isProgress = WRITE_PROGRESS_RE.test(line);
    if (isProgress) {
      const last = logBuffer.length - 1;
      if (inWriteProgress && last >= 0 && WRITE_PROGRESS_RE.test(logBuffer[last])) {
        logBuffer[last] = line; // overwrite the previous progress tick in place
      } else {
        if (logBuffer.length && logBuffer[logBuffer.length - 1] !== "") {
          logBuffer.push(""); // blank line above a fresh progress bar
        }
        logBuffer.push(line);
        inWriteProgress = true;
      }
    } else if (inWriteProgress && line === "") {
      // Swallow esptool's own blank lines while a bar is live so it keeps
      // updating in place instead of scrolling.
      return;
    } else {
      if (inWriteProgress) {
        inWriteProgress = false;
        logBuffer.push(""); // blank line below the completed bar
      }
      logBuffer.push(line);
    }
    if (logBuffer.length > 5000) logBuffer = logBuffer.slice(-4000);
    const pre = $("#logPre");
    if (pre) {
      pre.textContent = logBuffer.join("\n");
      pre.scrollTop = pre.scrollHeight;
    }
  }

  // ---------------------------------------------------------------- messages
  window.addEventListener("message", (ev) => {
    const msg = ev.data;
    switch (msg.type) {
      case "state":
        Object.assign(model, {
          host: msg.host,
          micropython: msg.micropython,
          workspace: msg.workspace,
          tree: msg.tree || [],
          cmods: msg.cmods || {},
          flashers: msg.flashers || {},
          selection: msg.selection || model.selection,
          prefs: Object.assign({}, model.prefs, msg.prefs || {}),
          downloadVersions: msg.downloadVersions || [],
          downloadFamily: msg.downloadFamily || "",
          connectedDevice: msg.connectedDevice || "",
          detect: msg.detect !== undefined ? msg.detect : model.detect,
          busy: msg.busy,
        });
        render();
        break;
      case "detectStatus":
        model.detectStatus = { state: msg.state, text: msg.text || "" };
        // Latch "detecting" on start; cleared by the later "detect" message so
        // the Detect button stays disabled until Detect results are populated.
        if (msg.state === "detecting") {
          model.detecting = true;
        }
        render();
        break;
      case "detect":
        model.detect = msg.detect || null;
        // Detect is the last message of the probe (posted after enrichment), so
        // clear the in-progress flag here — not on the earlier status change —
        // to keep the button disabled until Device Info is populated.
        model.detecting = false;
        render();
        break;
      case "artifact":
        model.artifact = msg.artifact || { ready: false };
        // Pre-fill the offset field with the resolved board default until the
        // user edits it (empty === "use default").
        if (!model.flashOffset && model.artifact.flashOffset) {
          model.flashOffset = model.artifact.flashOffset;
        }
        render();
        break;
      case "flashOffsetDefault":
        if (msg.flashOffset) {
          // Always refresh from board.json when Target changes; user edits to
          // the field are wiped on select (model.flashOffset = "").
          model.flashOffset = String(msg.flashOffset);
          if (model.artifact) {
            model.artifact.flashOffset = model.flashOffset;
          }
        }
        render();
        break;
      case "devices":
        model.devices = msg.devices || [];
        model.prefs.device = msg.device || model.prefs.device;
        model.connectedDevice = msg.connectedDevice || "";
        render();
        break;
      case "phase":
        model.phase = msg.phase;
        model.phaseText = msg.text || "";
        model.busy = msg.phase === "building";
        render();
        break;
      case "flashStatus":
        model.flashStatus = { state: msg.state, text: msg.text || "" };
        model.flashing = msg.state === "flashing";
        render();
        break;
      case "clearLog":
        logBuffer = [];
        inWriteProgress = false;
        appendLog("");
        break;
      case "log":
        appendLog(msg.line);
        break;
      case "flashed":
        appendLog("[mpftp] ✓ flashed " + (msg.device || ""));
        model.flashEraseHint = false;
        break;
      case "needEraseConfirm":
        appendLog(
          "[mpftp] ⚠ " +
            (msg.message ||
              "Partition layout changed — enable Erase, then Flash again.")
        );
        model.flashEraseHint = true;
        render();
        break;
      default:
        break;
    }
  });

  vscode.postMessage({ type: "ready" });
})();
