/*
 * The emulated board: MicroPython compiled to WebAssembly, driven one byte at
 * a time like a board on a serial line. Runs in the emulator window and,
 * for testing, under Node.
 *
 * Bytes from mpftp go in through write(); everything the interpreter prints
 * comes out through the send callback, batched. A soft reset (Ctrl-D at an
 * empty raw REPL, or machine.soft_reset()) rebuilds the interpreter from a
 * snapshot of its filesystem, which is how a board keeps its flash.
 *
 * Adapted from Workbench's MicroPythonWASM transport.
 */

const PYEXEC_FORCED_EXIT = 0x100;
const CTRL_A = 0x01, CTRL_B = 0x02, CTRL_C = 0x03;

/* Not the board's flash: never saved, rebuilt at every boot. */
export const SYSTEM_DIRS = new Set(["/sys", "/dev", "/proc", "/tmp", "/home", "/.emu"]);

const MACHINE_MODULE = `\
# The emulator has no pins or buses. A reset is a reboot of the interpreter.
def reset():
    raise SystemExit

def soft_reset():
    raise SystemExit
`;

function bootScript(width, height) {
  return `
import os, sys
sys.path[:] = [".", ".frozen", "/lib", "/.emu"]
from boarddev import env_set
env_set("PYDEVICES_WIDTH", ${Number(width)})
env_set("PYDEVICES_HEIGHT", ${Number(height)})
os.chdir("/")
del os, sys, env_set
`;
}

/** Every user file and directory, for the flash folder or a soft reset. */
export function snapshotFS(FS) {
  const dirs = [];
  const files = [];
  const walk = (path) => {
    let entries;
    try {
      entries = FS.readdir(path);
    } catch {
      return;
    }
    for (const name of entries) {
      if (name === "." || name === "..") continue;
      const full = path === "/" ? "/" + name : path + "/" + name;
      if (SYSTEM_DIRS.has(full)) continue;
      let stat;
      try {
        stat = FS.stat(full);
      } catch {
        continue;
      }
      if (FS.isDir(stat.mode)) {
        dirs.push(full);
        walk(full);
      } else if (FS.isFile(stat.mode)) {
        try {
          files.push({ path: full, data: FS.readFile(full) });
        } catch {
          /* unreadable: skip */
        }
      }
    }
  };
  walk("/");
  return { dirs, files };
}

function restoreFS(FS, snapshot) {
  for (const dir of [...snapshot.dirs].sort((a, b) => a.length - b.length)) {
    try {
      FS.mkdir(dir);
    } catch {
      /* exists */
    }
  }
  for (const { path, data } of snapshot.files) {
    try {
      FS.writeFile(path, data);
    } catch {
      /* skip */
    }
  }
}

export class EmuBoard {
  /**
   * @param {object} opts
   * @param {Function} opts.loadMicroPython  the runtime's factory
   * @param {string}   [opts.wasmURL]
   * @param {(bytes: Uint8Array) => void} opts.send  output for mpftp
   * @param {{width: number, height: number}} [opts.size]  display size at boot
   * @param {() => void} [opts.beforeRestart]  let go of the old VM's display
   * @param {() => void} [opts.afterBoot]  a new VM is up (first boot or reset)
   * @param {() => void} [opts.onActivity]  input was processed
   */
  constructor(opts) {
    this.opts = opts;
    this.size = opts.size || { width: 320, height: 240 };
    this.mp = null;
    this.inRaw = false;
    this.busy = false;
    this.queue = [];
    this.draining = false;
    this.suppress = false;
    this.outBuf = [];
    this.outTimer = null;
  }

  async start(snapshot) {
    await this._createVM(snapshot || { dirs: ["/lib"], files: [] });
    this.mp.replInit();
    this._flushSoon();
  }

  async _createVM(snapshot) {
    const mpOpts = {
      linebuffer: false,
      heapsize: 16 * 1024 * 1024,
      stdout: (data) => this._out(data),
    };
    mpOpts.stderr = mpOpts.stdout;
    if (this.opts.wasmURL) mpOpts.url = this.opts.wasmURL;
    this.mp = await this.opts.loadMicroPython(mpOpts);
    const FS = this.mp.FS;
    restoreFS(FS, snapshot);
    for (const dir of ["/lib", "/.emu"]) {
      try {
        FS.mkdir(dir);
      } catch {
        /* exists */
      }
    }
    FS.writeFile("/.emu/machine.py", MACHINE_MODULE);
    this.suppress = true;
    try {
      await this.mp.runPythonAsync(bootScript(this.size.width, this.size.height));
    } finally {
      this.suppress = false;
    }
    this.opts.afterBoot?.();
  }

  snapshot() {
    return snapshotFS(this.mp.FS);
  }

  /*
   * Output arrives one byte per call. It goes out in batches, but a batch
   * can't wait on a timer: time.sleep() in this runtime holds the page, so
   * no timer fires until the program stops. A line end, the raw REPL's "OK",
   * a prompt, or a full batch sends at once, from inside the call.
   */
  _out(data) {
    if (this.suppress) return;
    const bytes = typeof data === "string" ? new TextEncoder().encode(data) : data;
    for (const b of bytes) this.outBuf.push(b);
    const n = this.outBuf.length;
    const last = this.outBuf[n - 1];
    const ok = n >= 2 && this.outBuf[n - 2] === 0x4f && last === 0x4b; // "OK"
    if (last === 0x0a || last === 0x3e || last === 0x04 || ok || n >= 512) {
      this._flush();
    } else {
      this._flushSoon();
    }
  }

  _flush() {
    if (this.outTimer !== null) {
      clearTimeout(this.outTimer);
      this.outTimer = null;
    }
    if (this.outBuf.length) {
      const chunk = Uint8Array.from(this.outBuf);
      this.outBuf = [];
      this.opts.send(chunk);
    }
  }

  _flushSoon() {
    if (this.outTimer !== null) return;
    this.outTimer = setTimeout(() => {
      this.outTimer = null;
      this._flush();
    }, 5);
  }

  _say(text) {
    this._out(new TextEncoder().encode(text));
  }

  /** Bytes from mpftp. */
  write(bytes) {
    for (const b of bytes) {
      // A board's serial driver turns Ctrl-C into KeyboardInterrupt the
      // moment it arrives, even while a program runs. Queued behind the
      // program, it would never be read.
      if (b === CTRL_C && this.busy) {
        this.mp._module._mp_sched_keyboard_interrupt();
        continue;
      }
      this.queue.push(b);
    }
    this._drain();
  }

  /** The reset button: stop what's running, then a fresh interpreter. */
  hardReset() {
    if (this.busy) this.mp._module._mp_sched_keyboard_interrupt();
    this.queue.push("hard-reset");
    this._drain();
  }

  async _drain() {
    if (this.draining) return;
    this.draining = true;
    try {
      while (this.queue.length) {
        const b = this.queue.shift();
        if (b === "hard-reset") {
          await this.restart({ hard: true });
          continue;
        }
        if (b === CTRL_A) this.inRaw = true;
        else if (b === CTRL_B) this.inRaw = false;
        this.busy = true;
        let ret;
        try {
          ret = await this.mp.replProcessCharWithAsyncify(b);
        } finally {
          this.busy = false;
        }
        if (ret === PYEXEC_FORCED_EXIT) {
          await this.restart();
        }
        this.opts.onActivity?.();
      }
    } finally {
      this.draining = false;
    }
  }

  /** Soft reset: a fresh interpreter over the same files. */
  async restart({ hard = false } = {}) {
    const wasRaw = this.inRaw && !hard;
    const snapshot = this.snapshot();
    this.opts.beforeRestart?.();
    await this._createVM(snapshot);
    this.mp.replInit();
    this.suppress = true;
    try {
      if (wasRaw) {
        await this.mp.replProcessCharWithAsyncify(CTRL_C);
        await this.mp.replProcessCharWithAsyncify(CTRL_A);
      }
    } finally {
      this.suppress = false;
    }
    this.inRaw = wasRaw;
    if (wasRaw) {
      this._say("MPY: soft reboot\r\nraw REPL; CTRL-B to exit\r\n>");
    } else {
      this._say("MPY: soft reboot\r\nMicroPython (emulator)\r\n>>> ");
    }
  }
}
