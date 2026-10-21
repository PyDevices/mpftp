/**
 * WebSocket client for the mpftp sidecar protocol, relayed as-is by
 * mpftp.pwa's SidecarRelay: {"id","method","params"} requests get back
 * {"type":"result"|"error", "id", ...}; the sidecar also pushes
 * {"type":"notify","method","params"} on its own (repl_data, ready, ...).
 *
 * The same socket carries the File Transfer panel (mpftp.panel): {"panel": msg}
 * goes up, {"type":"panel","msg"} and {"type":"panel_host",...} come down.
 *
 * In the desktop app there is no socket: the same lines go through the
 * app's own messaging to mpftp.desktop_host, which runs the same relay.
 */

/** The desktop app's bridge, present only inside its window. */
const tauri = (globalThis as any).__TAURI__ as
  | {
      core: { invoke: (cmd: string, args?: Record<string, unknown>) => Promise<any> };
      event: { listen: (name: string, fn: (ev: { payload: any }) => void) => Promise<unknown> };
    }
  | undefined;

export const inDesktopApp = tauri !== undefined;

/** Where lines go: a WebSocket in the browser, the app's host in the desktop app. */
interface Link {
  open: boolean;
  send(line: string): void;
}

type Pending = { resolve: (v: any) => void; reject: (e: Error) => void };

export class Rpc {
  private link: Link | null = null;
  private nextId = 1;
  private pending = new Map<number, Pending>();
  private notifyHandlers = new Map<string, Set<(params: any) => void>>();
  private statusHandlers = new Set<(connected: boolean) => void>();
  private panelHandlers = new Set<(msg: any) => void>();
  /** Panel messages sent before the socket opened (ftp.js says "ready" at once). */
  private panelQueue: string[] = [];
  private reconnectDelay = 500;

  connect(): void {
    if (tauri) {
      void this.connectDesktop(tauri);
    } else {
      this.connectSocket();
    }
  }

  private connectSocket(): void {
    const proto = location.protocol === "https:" ? "wss:" : "ws:";
    const ws = new WebSocket(`${proto}//${location.host}/`);
    const link: Link = { open: false, send: (line) => ws.send(line) };
    this.link = link;
    ws.onopen = () => {
      this.reconnectDelay = 500;
      this.opened(link);
    };
    ws.onmessage = (ev) => this.handleMessage(String(ev.data));
    ws.onclose = () => {
      this.closed(link);
      setTimeout(() => this.connectSocket(), this.reconnectDelay);
      this.reconnectDelay = Math.min(this.reconnectDelay * 2, 10000);
    };
    ws.onerror = () => ws.close();
  }

  /** The app restarts its host on its own and says so with host-status. */
  private async connectDesktop(bridge: NonNullable<typeof tauri>): Promise<void> {
    const link: Link = {
      open: false,
      send: (line) => void bridge.core.invoke("host_send", { line }),
    };
    this.link = link;
    await bridge.event.listen("host-line", (ev) => this.handleMessage(String(ev.payload)));
    await bridge.event.listen("host-status", (ev) => {
      if (ev.payload) {
        this.opened(link);
      } else {
        this.closed(link);
      }
    });
    if (await bridge.core.invoke("host_running")) {
      this.opened(link);
    }
  }

  private opened(link: Link): void {
    if (link.open) {
      return;
    }
    link.open = true;
    for (const line of this.panelQueue.splice(0)) {
      link.send(line);
    }
    this.statusHandlers.forEach((fn) => fn(true));
  }

  private closed(link: Link): void {
    if (!link.open) {
      return;
    }
    link.open = false;
    this.statusHandlers.forEach((fn) => fn(false));
    for (const [, p] of this.pending) {
      p.reject(new Error("connection closed"));
    }
    this.pending.clear();
  }

  private handleMessage(text: string): void {
    let msg: any;
    try {
      msg = JSON.parse(text);
    } catch {
      return;
    }
    if (msg.type === "panel" || msg.type === "panel_host") {
      this.panelHandlers.forEach((fn) => fn(msg));
      return;
    }
    if (msg.type === "notify") {
      const handlers = this.notifyHandlers.get(msg.method);
      if (handlers) {
        handlers.forEach((fn) => fn(msg.params || {}));
      }
      return;
    }
    const pending = this.pending.get(msg.id);
    if (!pending) {
      return;
    }
    this.pending.delete(msg.id);
    if (msg.type === "error") {
      pending.reject(new Error(msg.error || "rpc error"));
    } else {
      pending.resolve(msg.result);
    }
  }

  call(method: string, params: Record<string, unknown> = {}): Promise<any> {
    const link = this.link;
    if (!link || !link.open) {
      return Promise.reject(new Error("not connected to mpftp"));
    }
    const id = this.nextId++;
    const promise = new Promise<any>((resolve, reject) => {
      this.pending.set(id, { resolve, reject });
    });
    link.send(JSON.stringify({ id, method, params }));
    return promise;
  }

  onNotify(method: string, handler: (params: any) => void): () => void {
    let set = this.notifyHandlers.get(method);
    if (!set) {
      set = new Set();
      this.notifyHandlers.set(method, set);
    }
    set.add(handler);
    return () => set!.delete(handler);
  }

  /** A message for the panel's host (mpftp.panel): queued until the socket opens. */
  sendPanel(panel: unknown): void {
    this.sendRaw(JSON.stringify({ panel }));
  }

  /** The answer to a panel question (an input box or a confirmation). */
  answerPanel(askId: number, value: unknown): void {
    this.sendRaw(JSON.stringify({ panelAnswer: askId, value }));
  }

  private sendRaw(line: string): void {
    if (this.link && this.link.open) {
      this.link.send(line);
    } else {
      this.panelQueue.push(line);
    }
  }

  onPanel(handler: (msg: any) => void): void {
    this.panelHandlers.add(handler);
  }

  onStatus(handler: (connected: boolean) => void): void {
    this.statusHandlers.add(handler);
  }
}
