/**
 * Keys for the board's REPL. Closed, they're dropped; holding, they wait
 * (the board is still switching to its REPL, seconds on CircuitPython);
 * open, they go straight through, and opening sends what waited.
 */
export class KeyGate {
  private state: "closed" | "holding" | "open" = "closed";
  private held: string[] = [];

  constructor(private send: (data: string) => void) {}

  hold(): void {
    this.state = "holding";
    this.held = [];
  }

  open(): void {
    const typed = this.held.join("");
    this.state = "open";
    this.held = [];
    if (typed) {
      this.send(typed);
    }
  }

  close(): void {
    this.state = "closed";
    this.held = [];
  }

  key(data: string): void {
    if (this.state === "open") {
      this.send(data);
    } else if (this.state === "holding") {
      this.held.push(data);
    }
  }

  get isOpen(): boolean {
    return this.state === "open";
  }
}
