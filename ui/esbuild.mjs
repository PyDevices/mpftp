// Bundle ui/src -> cli/src/mpftp/webui/, staged as package-data (mirrors how
// extension/python/ is staged from cli/src/mpftp for the VSIX — see
// scripts/stage-vendored-python.sh, the extension-side equivalent of this file).
import { build, context } from "esbuild";
import { cpSync, mkdirSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import path from "node:path";

const here = path.dirname(fileURLToPath(import.meta.url));
const outdir = path.resolve(here, "../cli/src/mpftp/webui");
const watch = process.argv.includes("--watch");

rmSync(outdir, { recursive: true, force: true });
mkdirSync(outdir, { recursive: true });

for (const [src, dest] of [
  ["manifest.webmanifest", "manifest.webmanifest"],
  ["sw.js", "sw.js"],
  ["icons", "icons"],
]) {
  cpSync(path.join(here, src), path.join(outdir, dest), { recursive: true });
}

// The File Transfer panel is the VS Code extension's own: its markup, script,
// stylesheet and icon font are copied from extension/media, so there is one
// copy of each. The page supplies acquireVsCodeApi() (src/host.ts).
const media = path.resolve(here, "../extension/media");
for (const name of ["ftp.js", "ftp.css", "codicons"]) {
  cpSync(path.join(media, name), path.join(outdir, name), { recursive: true });
}
const marker = "<!-- mpftp:ftp-panel -->";
const page = readFileSync(path.join(here, "index.html"), "utf8");
if (!page.includes(marker)) {
  throw new Error(`index.html has no ${marker}`);
}
const panel = readFileSync(path.join(media, "ftp.html"), "utf8").replace(/^<!--[\s\S]*?-->\s*/, "");
writeFileSync(path.join(outdir, "index.html"), page.replace(marker, panel));

// cli/src/mpftp/webui/ is a committed build artifact (the actual TestPyPI
// release build calls the org's shared reusable workflow, which only knows
// `python -m build .` — there's no room in that pipeline to run npm first),
// so the checked-in output is minified and sourcemap-free to keep it lean.
// `npm run watch` keeps sourcemaps for local iteration.
const options = {
  entryPoints: [path.join(here, "src/main.ts")],
  bundle: true,
  outfile: path.join(outdir, "app.js"),
  format: "esm",
  target: "es2020",
  minify: !watch,
  sourcemap: watch,
  logLevel: "info",
};

if (watch) {
  const ctx = await context(options);
  await ctx.watch();
  console.log("watching ui/src ...");
} else {
  await build(options);
}
