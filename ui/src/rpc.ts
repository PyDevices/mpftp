/**
 * WebSocket client for the mpftp sidecar protocol, relayed as-is by
 * mpftp.pwa's SidecarRelay: {"id","method","params"} requests get back
 * {"type":"result"|"error", "id", ...}; the sidecar also pushes
 * {"type":"notify","method","params"} on its own (repl_data, ready, ...).
 */

type Pending = { resolve: (v: any) => void; reject: (e: Error) => void };

export class Rpc {
  private ws: WebSocket | null = null;
  private nextId = 1;
  private pending = new Map<number, Pending>();
  private notifyHandlers = new Map<string, Set<(params: any) => void>>();
  private statusHandlers = new Set<(connected: boolean) => void>();
  private reconnectDelay = 500;

  connect(): void {
    const proto = location.protocol === "https:" ? "wss:" : "ws:";
    const ws = new WebSocket(`${proto}//${location.host}/`);
    this.ws = ws;
    ws.onopen = () => {
      this.reconnectDelay = 500;
      this.statusHandlers.forEach((fn) => fn(true));
    };
    ws.onmessage = (ev) => this.handleMessage(String(ev.data));
    ws.onclose = () => {
      this.statusHandlers.forEach((fn) => fn(false));
      for (const [, p] of this.pending) {
        p.reject(new Error("connection closed"));
      }
      this.pending.clear();
      setTimeout(() => this.connect(), this.reconnectDelay);
      this.reconnectDelay = Math.min(this.reconnectDelay * 2, 10000);
    };
    ws.onerror = () => ws.close();
  }

  private handleMessage(text: string): void {
    let msg: any;
    try {
      msg = JSON.parse(text);
    } catch {
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
    if (!this.ws || this.ws.readyState !== WebSocket.OPEN) {
      return Promise.reject(new Error("not connected to mpftp"));
    }
    const id = this.nextId++;
    const promise = new Promise<any>((resolve, reject) => {
      this.pending.set(id, { resolve, reject });
    });
    this.ws.send(JSON.stringify({ id, method, params }));
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

  onStatus(handler: (connected: boolean) => void): void {
    this.statusHandlers.add(handler);
  }
}
