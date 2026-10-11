/**
 * Which REPL lines the plotter draws, and what is in them.
 *
 * A line plots when every item on it is a number, or a name and a number:
 *
 *   21.5                     one series
 *   21.5, 40.2, 1013         three series, named 1, 2, 3 by position
 *   21.5 40.2 1013           the same, separated by spaces or tabs
 *   temp:21.5 hum:40.2       named series (also "temp: 21.5" and "temp=21.5")
 *
 * Anything else (a prompt, a traceback, text with a number in it) is left
 * alone, so ordinary REPL output never turns into a graph by accident.
 */

/** The most series one line can carry. */
export const MAX_SERIES = 8;

export interface PlotSample {
  /** Series names: the given name, or the 1-based position as a string. */
  names: string[];
  values: number[];
}

const NUMBER = String.raw`[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?`;
const ITEM = new RegExp(String.raw`^(?:([A-Za-z_][\w .()/%-]*?)\s*[:=]\s*)?(${NUMBER})$`);
// Terminal control sequences (colour, cursor moves) a board may print.
const ANSI = /\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b[()][0-9A-Za-z]|\x1b[@-_]/g;

export function stripAnsi(text: string): string {
  return text.replace(ANSI, "");
}

export function parsePlotLine(raw: string): PlotSample | null {
  const line = stripAnsi(raw).trim();
  if (!line || line.length > 400) {
    return null;
  }
  // "name: 1" keeps its space after the colon; join it so spaces can separate items.
  const joined = line.replace(/\s*([:=])\s*/g, "$1");
  const items = /[,;]/.test(joined)
    ? joined.split(/\s*[,;]\s*/)
    : joined.split(/\s+/);
  if (items.length > MAX_SERIES) {
    return null;
  }
  const names: string[] = [];
  const values: number[] = [];
  for (let i = 0; i < items.length; i++) {
    const m = ITEM.exec(items[i]);
    if (!m) {
      return null;
    }
    const value = Number(m[2]);
    if (!Number.isFinite(value)) {
      return null;
    }
    names.push(m[1] ? m[1].trim() : String(i + 1));
    values.push(value);
  }
  return { names, values };
}

/**
 * Turns the REPL's byte stream into whole lines. A board's output arrives in
 * arbitrary chunks, so a line may be split across several.
 */
export class LineSplitter {
  private decoder = new TextDecoder();
  private partial = "";

  /** Feed one chunk; returns the lines it completed. */
  push(bytes: Uint8Array): string[] {
    const text = this.partial + this.decoder.decode(bytes, { stream: true });
    const parts = text.split("\n");
    this.partial = parts.pop() ?? "";
    if (this.partial.length > 4096) {
      this.partial = ""; // no newline in sight: not a line of numbers
    }
    return parts.map((p) => p.replace(/\r$/, ""));
  }

  reset(): void {
    this.partial = "";
  }
}
