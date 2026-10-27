/**
 * Video Editor · timeline ruler — second ticks + click/drag scrubbing.
 */
import { useRef } from "react";

import { useVideoEditorStore } from "../../../lib/store";

export const RULER_H = 22;

function fmt(frames: number, fps: number): string {
  const total = Math.max(0, Math.round(frames / fps));
  return `${Math.floor(total / 60)}:${String(total % 60).padStart(2, "0")}`;
}

export function TimeRuler({
  pxPerFrame,
  contentPx,
  minPx = 0,
  fps,
  frameAtClientX,
}: {
  pxPerFrame: number;
  contentPx: number;
  /** Keep laying ticks out to at least this width. When the project is shorter
   *  than the viewport the ruler is STRETCHED past `contentPx` by the flex row,
   *  and without this the stretched remainder would be blank. */
  minPx?: number;
  fps: number;
  frameAtClientX: (clientX: number) => number;
}) {
  const setPlayhead = useVideoEditorStore((s) => s.setPlayhead);
  const dragging = useRef(false);

  const secondsPx = fps * pxPerFrame;
  const stepSec = secondsPx >= 64 ? 1 : secondsPx >= 32 ? 2 : secondsPx >= 16 ? 5 : 10;
  const stepFrames = stepSec * fps;
  const ticks: number[] = [];
  const tickLimit = Math.max(contentPx, minPx);
  for (let f = 0; f * pxPerFrame <= tickLimit; f += stepFrames) ticks.push(f);

  return (
    <div
      // `overflow-hidden` is load-bearing, not cosmetic. Each tick's LABEL is
      // absolutely positioned ~26px to the right of its tick, so the last one
      // spilled past the ruler's own box and made scrollWidth exceed clientWidth
      // — which flipped on a horizontal scrollbar, which stole 15px of height,
      // which summoned a vertical scrollbar, which changed the measured width.
      // That was the timeline's zoom shake (#34).
      style={{ height: RULER_H, width: "100%" }}
      className="relative overflow-hidden border-b border-border bg-bg-subtle/60 select-none cursor-text"
      onPointerDown={(e) => {
        dragging.current = true;
        setPlayhead(frameAtClientX(e.clientX));
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
    >
      {ticks.map((f) => (
        <div key={f} style={{ left: f * pxPerFrame }} className="absolute top-0 h-full">
          <div className="w-px h-2 bg-border absolute top-0" />
          <span className="absolute top-1.5 left-1 text-[9px] font-mono text-muted whitespace-nowrap">
            {fmt(f, fps)}
          </span>
        </div>
      ))}
    </div>
  );
}
