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

export function initSplitters(): void {
  const editorPane = document.getElementById("editor-pane");
  const sidePane = document.getElementById("side-pane");
  const filesStage = document.getElementById("files-stage");
  const consolePane = document.getElementById("console-pane");

  if (editorPane && sidePane) {
    makeDraggable(document.getElementById("splitter-v"), "col-resize", (event) => {
      const totalW = window.innerWidth;
      const newLeftW = Math.max(260, Math.min(event.clientX, totalW - 320));
      const pct = (newLeftW / totalW) * 100;
      editorPane.style.flex = `0 0 ${pct}%`;
      sidePane.style.flex = `0 0 ${100 - pct}%`;
    });
  }

  if (sidePane && filesStage && consolePane) {
    makeDraggable(document.getElementById("splitter-h"), "row-resize", (event) => {
      const bounds = sidePane.getBoundingClientRect();
      const stageH = Math.max(120, Math.min(event.clientY - bounds.top, bounds.height - 120));
      const pct = (stageH / bounds.height) * 100;
      filesStage.style.flex = `0 0 ${pct}%`;
      consolePane.style.flex = `1 1 ${100 - pct}%`;
    });
  }
}
