/**
 * Video Editor · playhead — a draggable vertical line across the lanes.
 */
import { useRef } from "react";

import { useVideoEditorStore } from "../../../lib/store";

export function Playhead({
  pxPerFrame,
  height,
  frameAtClientX,
}: {
  pxPerFrame: number;
  height: number;
  frameAtClientX: (clientX: number) => number;
}) {
  const playhead = useVideoEditorStore((s) => s.playheadFrame);
  const setPlayhead = useVideoEditorStore((s) => s.setPlayhead);
  const dragging = useRef(false);

  const x = playhead * pxPerFrame;

  return (
    <div
      style={{ left: x, height }}
      className="absolute top-0 z-20 pointer-events-none"
    >
      {/* Line */}
      <div className="absolute top-0 w-px h-full bg-cyan-500/90" />
      {/* Handle (draggable) */}
      <div
        className="absolute -top-0.5 -left-[5px] w-[11px] h-[11px] rounded-sm bg-cyan-500 cursor-ew-resize pointer-events-auto"
        onPointerDown={(e) => {
          e.stopPropagation();
          dragging.current = true;
          try {
            (e.currentTarget as HTMLElement).setPointerCapture(e.pointerId);
          } catch {
            /* synthetic event — harmless */
          }
        }}
        onPointerMove={(e) => {
          if (dragging.current) setPlayhead(frameAtClientX(e.clientX));
        }}
        onPointerUp={(e) => {
          dragging.current = false;
          try {
            (e.currentTarget as HTMLElement).releasePointerCapture(e.pointerId);
          } catch {
            /* ignore */
          }
        }}
      />
    </div>
  );
}
