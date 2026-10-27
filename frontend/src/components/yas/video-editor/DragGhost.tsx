/**
 * Video Editor · drag ghost — a fixed, pointer-events-none clone that follows
 * the cursor (x AND y) while a clip is being dragged across tracks. Portaled to
 * document.body so it can span vertically across all lanes. Gives the
 * cross-track drag real-time feedback (the source ClipBlock fades while this
 * shows where the clip will land).
 */
import { createPortal } from "react-dom";

import { useVideoEditorStore } from "../../../lib/store";

const GHOST_H = 46;

export function DragGhost() {
  const ghost = useVideoEditorStore((s) => s.dragGhost);
  if (!ghost) return null;
  return createPortal(
    <div
      style={{
        position: "fixed",
        left: ghost.clientX - ghost.widthPx / 2,
        top: ghost.clientY - GHOST_H / 2,
        width: Math.max(8, ghost.widthPx),
        height: GHOST_H,
        zIndex: 120,
        pointerEvents: "none",
      }}
      className="rounded-md border border-cyan-400 bg-cyan-600/70 shadow-lg flex items-end justify-start overflow-hidden"
    >
      <span className="text-[9.5px] font-mono text-white/95 px-1.5 pb-1">{ghost.label}</span>
    </div>,
    document.body,
  );
}
