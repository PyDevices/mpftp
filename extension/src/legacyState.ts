import * as fs from "fs";
import * as os from "os";
import * as path from "path";

/**
 * The tool used to keep its saved devices and settings in ~/.mpftp. On first
 * run, when ~/.mcuctl doesn't exist and ~/.mpftp does, the old folder becomes
 * the new one, so saved devices carry over. Returns a message when the move
 * failed, otherwise undefined.
 */
export function adoptLegacyStateDir(home: string = os.homedir()): string | undefined {
  const oldDir = path.join(home, ".mpftp");
  const newDir = path.join(home, ".mcuctl");
  try {
    if (fs.existsSync(newDir) || !fs.statSync(oldDir).isDirectory()) {
      return undefined;
    }
    fs.renameSync(oldDir, newDir);
  } catch (err) {
    if ((err as NodeJS.ErrnoException).code === "ENOENT") {
      return undefined;
    }
    return `could not move ${oldDir} to ${newDir}: ${String(err)}`;
  }
  return undefined;
}
