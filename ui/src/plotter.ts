/**
 * The REPL's plotter: numbers the board prints, drawn live as a line graph.
 *
 * While the pane is open, every line of REPL output that is only numbers
 * (see plotparse.ts) adds one sample per series. The newest WINDOW samples
 * are shown, scaled to fit; hovering reads the values at that sample.
 */
import { LineSplitter, MAX_SERIES, parsePlotLine } from "./plotparse";
import { h } from "./wifi";

/** Samples kept and drawn. */
const WINDOW = 500;
const OPEN_KEY = "mpftp-plotter-open";

interface Series {
  name: string;
  slot: number; // 1-based categorical colour, in the order series appear
}

/** "Nice" tick step for a span, about `count` ticks. */
function niceStep(span: number, count: number): number {
  const raw = span / Math.max(1, count);
  const mag = Math.pow(10, Math.floor(Math.log10(raw)));
  const norm = raw / mag;
  const nice = norm < 1.5 ? 1 : norm < 3 ? 2 : norm < 7 ? 5 : 10;
  return nice * mag;
}

function formatValue(v: number): string {
  if (v === 0) {
    return "0";
  }
  const a = Math.abs(v);
  if (a >= 1e6 || a < 1e-3) {
    return v.toExponential(2);
  }
  return String(Number(v.toPrecision(6)));
}

export class Plotter {
  readonly pane: HTMLElement;
  private canvas: HTMLCanvasElement;
  private legend: HTMLElement;
  private tip: HTMLElement;
  private empty: HTMLElement;
  private pauseBtn: HTMLButtonElement;
  private lines = new LineSplitter();
  private series: Series[] = [];
  /** Each sample: value per series name (absent = that series wasn't on the line). */
  private samples: Array<Map<string, number>> = [];
  private total = 0;
  private paused = false;
  private hoverX: number | null = null;
  private frame = 0;
  private onToggle: (open: boolean) => void;

  constructor(onToggle: (open: boolean) => void = () => undefined) {
    this.onToggle = onToggle;
    this.canvas = h("canvas", { class: "mp-plot-canvas", role: "img", "aria-label": "Plot of numbers printed at the REPL" });
    this.legend = h("div", { class: "mp-plot-legend", "aria-live": "off" });
    this.tip = h("div", { class: "mp-plot-tip", role: "status" });
    this.tip.hidden = true;
    this.empty = h(
      "div",
      { class: "mp-plot-empty" },
      h("p", {}, "Print numbers at the REPL to plot them, one line per sample:"),
      h("pre", {}, "print(t)\nprint(t, h, p)          # three series\nprint(f\"temp:{t} hum:{h}\")")
    );
    this.pauseBtn = h("button", { type: "button", class: "mp-btn", title: "Stop adding samples (Resume carries on)" }, "Pause");
    const clear = h("button", { type: "button", class: "mp-btn", title: "Forget every sample and series" }, "Clear");
    const close = h(
      "button",
      { type: "button", class: "mp-btn mp-icon-btn", title: "Close the plotter", "aria-label": "Close the plotter" },
      h("i", { class: "codicon codicon-close" })
    );
    const body = h("div", { class: "mp-plot-body" }, this.canvas, this.empty, this.tip);
    this.pane = h(
      "section",
      { class: "mp-plot-pane", "aria-label": "Plotter" },
      h("div", { class: "mp-plot-bar" }, this.legend, h("div", { class: "mp-plot-actions" }, this.pauseBtn, clear, close)),
      body
    );
    this.pane.hidden = true;

    this.pauseBtn.addEventListener("click", () => this.setPaused(!this.paused));
    clear.addEventListener("click", () => this.clear());
    close.addEventListener("click", () => this.setOpen(false));
    this.canvas.addEventListener("pointermove", (ev) => {
      this.hoverX = ev.offsetX;
      this.schedule();
    });
    this.canvas.addEventListener("pointerleave", () => {
      this.hoverX = null;
      this.schedule();
    });
    new ResizeObserver(() => this.schedule()).observe(body);
    new MutationObserver(() => this.schedule()).observe(document.documentElement, {
      attributes: true,
      attributeFilter: ["data-theme"],
    });
  }

  get open(): boolean {
    return !this.pane.hidden;
  }

  /** Open as it was last time (false the first time). */
  restore(): void {
    let open = false;
    try {
      open = localStorage.getItem(OPEN_KEY) === "1";
    } catch {
      /* private window */
    }
    this.setOpen(open);
  }

  setOpen(open: boolean): void {
    this.pane.hidden = !open;
    this.lines.reset();
    try {
      localStorage.setItem(OPEN_KEY, open ? "1" : "0");
    } catch {
      /* private window */
    }
    this.onToggle(open);
    this.schedule();
  }

  toggle(): void {
    this.setOpen(!this.open);
  }

  /** REPL output bytes, as the board sent them. */
  feed(bytes: Uint8Array): void {
    if (!this.open) {
      return;
    }
    for (const line of this.lines.push(bytes)) {
      this.addLine(line);
    }
  }

  /** One line of text; returns whether it was plotted. */
  addLine(line: string): boolean {
    if (this.paused) {
      return false;
    }
    const sample = parsePlotLine(line);
    if (!sample) {
      return false;
    }
    const point = new Map<string, number>();
    sample.names.forEach((name, i) => {
      if (!this.series.some((s) => s.name === name)) {
        if (this.series.length >= MAX_SERIES) {
          return; // a 9th series isn't given a made-up colour
        }
        this.series.push({ name, slot: this.series.length + 1 });
      }
      point.set(name, sample.values[i]);
    });
    if (!point.size) {
      return false;
    }
    this.samples.push(point);
    this.total++;
    if (this.samples.length > WINDOW) {
      this.samples.splice(0, this.samples.length - WINDOW);
    }
    this.schedule();
    return true;
  }

  clear(): void {
    this.series = [];
    this.samples = [];
    this.total = 0;
    this.lines.reset();
    this.schedule();
  }

  setPaused(paused: boolean): void {
    this.paused = paused;
    this.pauseBtn.textContent = paused ? "Resume" : "Pause";
    this.pauseBtn.classList.toggle("mp-btn-primary", paused);
  }

  private schedule(): void {
    if (!this.frame) {
      this.frame = requestAnimationFrame(() => {
        this.frame = 0;
        this.draw();
      });
    }
  }

  private color(slot: number): string {
    return getComputedStyle(this.pane).getPropertyValue(`--series-${slot}`).trim() || "#888";
  }

  private renderLegend(): void {
    const last = this.samples[this.samples.length - 1];
    this.legend.replaceChildren(
      ...this.series.map((s) => {
        const value = last?.get(s.name);
        return h(
          "span",
          { class: "mp-plot-key" },
          h("span", { class: "mp-plot-swatch", style: `background:${this.color(s.slot)}` }),
          h("span", { class: "mp-plot-name" }, s.name),
          h("span", { class: "mp-plot-value" }, value === undefined ? "" : formatValue(value))
        );
      })
    );
  }

  private draw(): void {
    if (!this.open) {
      return;
    }
    this.empty.hidden = this.samples.length > 0;
    this.renderLegend();
    const css = getComputedStyle(this.pane);
    const rect = this.canvas.getBoundingClientRect();
    const dpr = window.devicePixelRatio || 1;
    const w = Math.max(1, Math.round(rect.width));
    const hgt = Math.max(1, Math.round(rect.height));
    if (this.canvas.width !== w * dpr || this.canvas.height !== hgt * dpr) {
      this.canvas.width = w * dpr;
      this.canvas.height = hgt * dpr;
    }
    const ctx = this.canvas.getContext("2d");
    if (!ctx) {
      return;
    }
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, w, hgt);
    this.tip.hidden = true;
    if (!this.samples.length) {
      return;
    }

    // Y range over what's on screen, padded, on "nice" ticks.
    let lo = Infinity;
    let hi = -Infinity;
    for (const p of this.samples) {
      for (const v of p.values()) {
        lo = Math.min(lo, v);
        hi = Math.max(hi, v);
      }
    }
    if (lo === hi) {
      const pad = Math.abs(lo) * 0.1 || 1;
      lo -= pad;
      hi += pad;
    }
    const step = niceStep(hi - lo, Math.max(2, Math.floor(hgt / 48)));
    lo = Math.floor(lo / step) * step;
    hi = Math.ceil(hi / step) * step;

    const font = `11px ${css.getPropertyValue("--font-mono") || "monospace"}`;
    ctx.font = font;
    const ticks: number[] = [];
    for (let v = lo; v <= hi + step / 2; v += step) {
      ticks.push(Math.abs(v) < step / 1e6 ? 0 : v);
    }
    const labelW = Math.max(...ticks.map((t) => ctx.measureText(formatValue(t)).width));
    const left = Math.ceil(labelW) + 12;
    const right = 10;
    const top = 8;
    const bottom = 20;
    const pw = Math.max(1, w - left - right);
    const ph = Math.max(1, hgt - top - bottom);
    const n = this.samples.length;
    // The samples fill the width; once there are WINDOW of them it scrolls.
    const xOf = (i: number) => left + (n > 1 ? (pw * i) / (n - 1) : pw / 2);
    const yOf = (v: number) => top + ph - ((v - lo) / (hi - lo)) * ph;

    // Recessive grid and labels.
    const grid = css.getPropertyValue("--plot-grid").trim();
    const muted = css.getPropertyValue("--text-subtle").trim();
    ctx.lineWidth = 1;
    ctx.strokeStyle = grid;
    ctx.fillStyle = muted;
    ctx.textAlign = "right";
    ctx.textBaseline = "middle";
    for (const t of ticks) {
      const y = Math.round(yOf(t)) + 0.5;
      ctx.beginPath();
      ctx.moveTo(left, y);
      ctx.lineTo(left + pw, y);
      ctx.stroke();
      ctx.fillText(formatValue(t), left - 6, y);
    }
    ctx.textAlign = "left";
    ctx.textBaseline = "alphabetic";
    ctx.fillText(n < this.total ? `last ${n} of ${this.total} samples` : `${n} samples`, left, hgt - 5);

    // One 2px line per series; a gap where a sample lacked that series.
    ctx.lineWidth = 2;
    ctx.lineJoin = "round";
    ctx.lineCap = "round";
    for (const s of this.series) {
      ctx.strokeStyle = this.color(s.slot);
      ctx.beginPath();
      let pen = false;
      this.samples.forEach((p, i) => {
        const v = p.get(s.name);
        if (v === undefined) {
          pen = false;
          return;
        }
        if (pen) {
          ctx.lineTo(xOf(i), yOf(v));
        } else {
          ctx.moveTo(xOf(i), yOf(v));
          pen = true;
        }
      });
      ctx.stroke();
      if (n === 1) {
        const v = this.samples[0].get(s.name);
        if (v !== undefined) {
          ctx.fillStyle = this.color(s.slot);
          ctx.beginPath();
          ctx.arc(xOf(0), yOf(v), 4, 0, 2 * Math.PI);
          ctx.fill();
        }
      }
    }

    // Hover: a crosshair at the nearest sample and its values.
    if (this.hoverX !== null && this.hoverX >= left) {
      const i = n > 1 ? Math.round(((this.hoverX - left) * (n - 1)) / pw) : 0;
      if (i >= 0 && i < n) {
        const x = Math.round(xOf(i)) + 0.5;
        ctx.strokeStyle = muted;
        ctx.lineWidth = 1;
        ctx.beginPath();
        ctx.moveTo(x, top);
        ctx.lineTo(x, top + ph);
        ctx.stroke();
        const p = this.samples[i];
        const bg = css.getPropertyValue("--bg-panel").trim();
        for (const s of this.series) {
          const v = p.get(s.name);
          if (v === undefined) {
            continue;
          }
          ctx.fillStyle = this.color(s.slot);
          ctx.strokeStyle = bg;
          ctx.lineWidth = 2;
          ctx.beginPath();
          ctx.arc(x, yOf(v), 4.5, 0, 2 * Math.PI);
          ctx.fill();
          ctx.stroke();
        }
        this.tip.replaceChildren(
          h("div", { class: "mp-plot-tip-title" }, `sample ${this.total - n + i + 1}`),
          ...this.series
            .filter((s) => p.has(s.name))
            .map((s) =>
              h(
                "div",
                { class: "mp-plot-key" },
                h("span", { class: "mp-plot-swatch", style: `background:${this.color(s.slot)}` }),
                h("span", { class: "mp-plot-name" }, s.name),
                h("span", { class: "mp-plot-value" }, formatValue(p.get(s.name)!))
              )
            )
        );
        this.tip.hidden = false;
        const tipW = this.tip.offsetWidth;
        this.tip.style.left = `${x + 12 + tipW > w ? x - 12 - tipW : x + 12}px`;
        this.tip.style.top = `${top + 4}px`;
      }
    }
  }
}
