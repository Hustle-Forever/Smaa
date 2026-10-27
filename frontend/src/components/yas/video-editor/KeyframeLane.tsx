/**
 * Video Editor · keyframe lane — a thin strip (one per animated property) with
 * draggable diamonds. Click empty space to add a keyframe (with the interpolated
 * value); drag a diamond to retime it; click it to seek the playhead there;
 * double-click to remove.
 */
import { useRef, useState } from "react";
import { sampleKeyframes } from "video-edit-schema";

import { useVideoEditorStore, type KeyframeProp } from "../../../lib/store";
import type { VideoEditKeyframe } from "../../../lib/types";

export function KeyframeLane({
  clipId,
  prop,
  kfs,
  dur,
  localFrame,
  clipFrom,
}: {
  clipId: string;
  prop: KeyframeProp;
  kfs: VideoEditKeyframe[];
  dur: number;
  localFrame: number | null;
  clipFrom: number;
}) {
  const setKeyframe = useVideoEditorStore((s) => s.setKeyframe);
  const moveKeyframe = useVideoEditorStore((s) => s.moveKeyframe);
  const removeKeyframe = useVideoEditorStore((s) => s.removeKeyframe);
  const setPlayhead = useVideoEditorStore((s) => s.setPlayhead);

  const laneRef = useRef<HTMLDivElement>(null);
  const [drag, setDrag] = useState<{ origF: number; previewF: number; moved: boolean } | null>(null);

  const frameAtClientX = (clientX: number) => {
    const el = laneRef.current;
    if (!el) return 0;
    const rect = el.getBoundingClientRect();
    const t = (clientX - rect.left) / Math.max(1, rect.width);
    return Math.max(0, Math.min(Math.round(t * dur), dur - 1));
  };

  return (
    <div
      ref={laneRef}
      className="relative h-3 rounded bg-bg-muted/50 border border-border/60 cursor-copy"
      onPointerDown={(e) => {
        if (e.target !== e.currentTarget) return; // a diamond handles its own
        const f = frameAtClientX(e.clientX);
        setKeyframe(clipId, prop, f, sampleKeyframes(kfs, f, kfs[0]?.v ?? 0));
      }}
      title="Click to add a mark"
    >
      {localFrame != null && (
        <div
          style={{ left: `${(localFrame / dur) * 100}%` }}
          className="absolute top-0 h-full w-px bg-cyan-500/70 pointer-events-none"
        />
      )}
      {kfs.map((k) => {
        const isDragging = drag?.origF === k.f;
        const shownF = isDragging ? drag!.previewF : k.f;
        return (
          <div
            key={k.f}
            onPointerDown={(e) => {
              e.stopPropagation();
              try {
                (e.currentTarget as HTMLElement).setPointerCapture(e.pointerId);
              } catch {
                /* synthetic — harmless */
              }
              setDrag({ origF: k.f, previewF: k.f, moved: false });
            }}
            onPointerMove={(e) => {
              setDrag((d) => {
                if (!d || d.origF !== k.f) return d;
                const pf = frameAtClientX(e.clientX);
                return { ...d, previewF: pf, moved: d.moved || pf !== d.origF };
              });
            }}
            onPointerUp={(e) => {
              try {
                (e.currentTarget as HTMLElement).releasePointerCapture(e.pointerId);
              } catch {
                /* ignore */
              }
              setDrag((d) => {
                if (!d || d.origF !== k.f) return d;
                if (d.moved) moveKeyframe(clipId, prop, d.origF, d.previewF);
                else setPlayhead(clipFrom + k.f);
                return null;
              });
            }}
            onDoubleClick={(e) => {
              e.stopPropagation();
              removeKeyframe(clipId, prop, k.f);
            }}
            style={{ left: `${(shownF / dur) * 100}%`, top: "50%" }}
            className="absolute w-2 h-2 -translate-x-1/2 -translate-y-1/2 rotate-45 bg-cyan-400 border border-cyan-600 cursor-ew-resize hover:bg-cyan-300"
            title={"Drag to move it · double-click to remove"}
          />
        );
      })}
    </div>
  );
}
