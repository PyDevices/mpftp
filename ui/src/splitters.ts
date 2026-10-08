/** Draggable pane splitters — same pointer-capture pattern as the PyDevices simulator. */

function makeDraggable(
  splitter: HTMLElement | null,
  cursor: string,
  onMove: (event: PointerEvent) => void
): void {
  if (!splitter) {
    return;
  }
  splitter.addEventListener("pointerdown", (event) => {
    if (event.button !== 0 && event.pointerType === "mouse") {
      return;
    }
    event.preventDefault();
    splitter.setPointerCapture(event.pointerId);
    splitter.classList.add("is-dragging");
    document.body.style.cursor = cursor;
    document.body.style.userSelect = "none";
  });

  splitter.addEventListener("pointermove", (event) => {
    if (!splitter.hasPointerCapture(event.pointerId)) {
      return;
    }
    onMove(event);
  });

  const end = (event: PointerEvent) => {
    if (!splitter.hasPointerCapture(event.pointerId)) {
      return;
    }
    splitter.releasePointerCapture(event.pointerId);
    splitter.classList.remove("is-dragging");
    document.body.style.cursor = "";
    document.body.style.userSelect = "";
  };
  splitter.addEventListener("pointerup", end);
  splitter.addEventListener("pointercancel", end);
}

const LAYOUT_KEY = "mpftp-layout";

function loadLayout(): { main?: number; ftp?: number } {
  try {
    return JSON.parse(localStorage.getItem(LAYOUT_KEY) || "{}");
  } catch {
    return {};
  }
}

function saveLayout(layout: { main?: number; ftp?: number }): void {
  try {
    localStorage.setItem(LAYOUT_KEY, JSON.stringify(layout));
  } catch {
    /* private browsing */
  }
}

/**
 * Two splitters: between the main column (File Transfer over the REPL) and the
 * editor, and between File Transfer and the REPL. Positions are remembered.
 */
export function initSplitters(onResize: () => void): void {
  const mainPane = document.getElementById("main-pane");
  const editorPane = document.getElementById("editor-pane");
  const ftpPane = document.getElementById("ftp-pane");
  const consolePane = document.getElementById("console-pane");
  const layout = loadLayout();

  const setMain = (pct: number) => {
    if (mainPane && editorPane) {
      mainPane.style.flex = `0 0 ${pct}%`;
      editorPane.style.flex = `1 1 ${100 - pct}%`;
    }
  };
  const setFtp = (pct: number) => {
    if (ftpPane && consolePane) {
      ftpPane.style.flex = `0 0 ${pct}%`;
      consolePane.style.flex = `1 1 ${100 - pct}%`;
    }
  };
  if (layout.main) {
    setMain(layout.main);
  }
  if (layout.ftp) {
    setFtp(layout.ftp);
  }

  if (mainPane && editorPane) {
    makeDraggable(document.getElementById("splitter-v"), "col-resize", (event) => {
      const totalW = window.innerWidth;
      const newLeftW = Math.max(420, Math.min(event.clientX, totalW - 280));
      layout.main = (newLeftW / totalW) * 100;
      setMain(layout.main);
      saveLayout(layout);
      onResize();
    });
  }

  if (mainPane && ftpPane && consolePane) {
    makeDraggable(document.getElementById("splitter-h"), "row-resize", (event) => {
      const bounds = mainPane.getBoundingClientRect();
      const stageH = Math.max(180, Math.min(event.clientY - bounds.top, bounds.height - 100));
      layout.ftp = (stageH / bounds.height) * 100;
      setFtp(layout.ftp);
      saveLayout(layout);
      onResize();
    });
  }
}
