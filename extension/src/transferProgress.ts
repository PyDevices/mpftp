// SPDX-FileCopyrightText: 2026 Brad Barnett
//
// SPDX-License-Identifier: MIT

/** `n` bytes as B, KB or MB, one decimal from KB up. */
export function formatBytes(n: number): string {
  if (n < 1024) {
    return `${n} B`;
  }
  if (n < 1024 * 1024) {
    return `${(n / 1024).toFixed(1)} KB`;
  }
  return `${(n / (1024 * 1024)).toFixed(1)} MB`;
}

/** How far into one file a transfer is: `1.2 of 3.4 MB, 35%`. */
export function bytesPart(done: number, total: number): string {
  if (total <= 0) {
    return formatBytes(done);
  }
  const pct = Math.min(100, Math.floor((done * 100) / total));
  return `${formatBytes(done)} of ${formatBytes(total)}, ${pct}%`;
}
