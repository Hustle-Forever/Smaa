/**
 * Video Editor · clip block
 *
 * A draggable/resizable block on a track lane:
 *   - drag body        → move on the timeline (snaps); cross-track drop onto a
 *                         compatible (same-kind) lane
 *   - drag ≤9px edge   → trim in/out
 *   - double-click      → split at the playhead
 *   - click             → select   ·   (Delete key removes — handled by Timeline)
 *
 * Mid-gesture state is LOCAL (60fps, no store writes); the store commits once on
 * pointerup using the SAME ops the preview uses (WYSIWYG). Audio-track clips
 * render a pseudo-waveform instead of a thumbnail.
 */
import { memo, useMemo, useRef, useState } from "react";

import { api } from "../../../lib/api";
import { useVideoEditorStore } from "../../../lib/store";
import type { VideoEditClip, VideoEditTrack } from "../../../lib/types";
import { applyTrimEdge, snapFrame } from "./ops";

const EDGE_PX = 9;

type DragKind = "move" | "trim-left" | "trim-right";
type DragState = { kind: DragKind; startX: number; dx: number; alt: boolean };

function thumbUrl(clip: VideoEditClip): string | null {
  const s = clip.source;
  if (clip.kind === "image" && s?.asset_id != null)
    return api.producerAssets.previewUrl(s.asset_id);
  if (s?.kind === "animation" && s.animation_id != null)
    return api.yasProducer.videoEditor.sourceThumbUrl("animation", s.animation_id);
  if (s?.kind === "upload" && s.upload_id != null)
    return api.yasProducer.videoEditor.sourceThumbUrl("upload", s.upload_id);
  return null;
}

/** Deterministic pseudo-waveform bar heights from the clip id. */
function waveBars(seed: string, n: number): number[] {
  let h = 2166136261 >>> 0;
  for (let i = 0; i < seed.length; i++) h = (Math.imul(h ^ seed.charCodeAt(i), 16777619)) >>> 0;
  const out: number[] = [];
  for (let i = 0; i < n; i++) {
    h = (Math.imul(h, 1103515245) + 12345) >>> 0;
    out.push(0.25 + ((h >>> 8) % 1000) / 1000 * 0.7);
  }
  return out;
}

// Our words for our plumbing ("upload", "composition"), never the user's.
const CLIP_LABEL: Record<string, string> = {
  upload: "Your upload",
  animation: "Animation",
  composition: "Video",
  poster: "Poster",
};

export const ClipBlock = memo(function ClipBlock({
  clip,
  pxPerFrame,
  height,
  selected,
  snapTargets,
  playhead,
  trackKind,
  locked,
  laneAt,
}: {
  clip: VideoEditClip;
  pxPerFrame: number;
  height: number;
  selected: boolean;
  snapTargets: number[];
  playhead: number;
  trackKind: "video" | "audio";
  locked: boolean;
  laneAt: (clientY: number) => VideoEditTrack | null;
}) {
  const select = useVideoEditorStore((s) => s.select);
  const toggleClipInSelection = useVideoEditorStore((s) => s.toggleClipInSelection);
  const selectMode = useVideoEditorStore((s) => s.selectMode);
  const clearMultiSelection = useVideoEditorStore((s) => s.clearMultiSelection);
  const multiSelected = useVideoEditorStore((s) => s.multiSelection.includes(clip.id));
  const moveClip = useVideoEditorStore((s) => s.moveClip);
  const trimClipEdge = useVideoEditorStore((s) => s.trimClipEdge);
  const splitClip = useVideoEditorStore((s) => s.splitClip);
  const setPlayhead = useVideoEditorStore((s) => s.setPlayhead);
  const beginDragGhost = useVideoEditorStore((s) => s.beginDragGhost);
  const updateDragGhost = useVideoEditorStore((s) => s.updateDragGhost);
  const endDragGhost = useVideoEditorStore((s) => s.endDragGhost);
  const draggingThis = useVideoEditorStore((s) => s.dragGhost?.clipId === clip.id);

  const [drag, setDrag] = useState<DragState | null>(null);
  const rafRef = useRef<number | null>(null);
  const lastPt = useRef({ x: 0, y: 0 });
  const isAudio = trackKind === "audio";
  const isText = clip.kind === "text";
  const isShape = clip.kind === "shape";
  const thumb = useMemo(
    () => (isAudio || isText || isShape ? null : thumbUrl(clip)),
    [clip, isAudio, isText, isShape],
  );
  const bars = useMemo(() => (isAudio ? waveBars(clip.id, 48) : []), [clip.id, isAudio]);
  const [imgErr, setImgErr] = useState(false);

  // Union of keyframe frames across all props → on-clip diamonds.
  const kfFrames = useMemo(() => {
    const set = new Set<number>();
    for (const arr of Object.values(clip.keyframes || {})) for (const k of arr || []) set.add(k.f);
    return Array.from(set).sort((a, b) => a - b);
  }, [clip.keyframes]);

  const preview = computePreview(clip, drag, pxPerFrame, snapTargets);
  const left = preview.from * pxPerFrame;
  const width = Math.max(6, preview.durationInFrames * pxPerFrame);

  function clearRaf() {
    if (rafRef.current != null) {
      cancelAnimationFrame(rafRef.current);
      rafRef.current = null;
    }
  }

  function onPointerDown(e: React.PointerEvent) {
    if (e.button !== 0) return;
    e.stopPropagation();
    // "Select items" mode (or Ctrl/Cmd-click): a click TOGGLES the clip in the
    // AI-chat multi-selection — no drag, no single-select replace.
    if (selectMode || e.ctrlKey || e.metaKey) {
      toggleClipInSelection(clip.id);
      select({ kind: "clip", clipId: clip.id });
      return;
    }
    clearMultiSelection(); // a plain click starts a fresh single selection
    select({ kind: "clip", clipId: clip.id });
    if (locked) return; // locked track → select only
    const rect = (e.currentTarget as HTMLElement).getBoundingClientRect();
    const offX = e.clientX - rect.left;
    const kind: DragKind =
      offX <= EDGE_PX ? "trim-left" : offX >= rect.width - EDGE_PX ? "trim-right" : "move";
    try {
      (e.currentTarget as HTMLElement).setPointerCapture(e.pointerId);
    } catch {
      /* synthetic event — harmless */
    }
    setDrag({ kind, startX: e.clientX, dx: 0, alt: e.altKey });
    if (kind === "move") {
      beginDragGhost(clip.id, e.clientX, e.clientY, clip.durationInFrames * pxPerFrame, `${(clip.durationInFrames / 30).toFixed(1)}s`);
    }
  }
  function onPointerMove(e: React.PointerEvent) {
    setDrag((d) => (d ? { ...d, dx: e.clientX - d.startX, alt: e.altKey } : d));
    if (drag?.kind !== "move") return;
    lastPt.current = { x: e.clientX, y: e.clientY };
    if (rafRef.current != null) return; // rAF-throttle the ghost / lane-highlight
    rafRef.current = requestAnimationFrame(() => {
      rafRef.current = null;
      const { x, y } = lastPt.current;
      const t = laneAt(y);
      updateDragGhost(x, y, t && t.kind === trackKind && !t.locked ? t.id : null);
    });
  }
  function onPointerUp(e: React.PointerEvent) {
    clearRaf();
    if (!drag) {
      endDragGhost();
      return;
    }
    try {
      (e.currentTarget as HTMLElement).releasePointerCapture(e.pointerId);
    } catch {
      /* ignore */
    }
    const deltaFrames = Math.round(drag.dx / pxPerFrame);
    if (drag.kind === "move") {
      const newFrom = previewMoveFrom(clip, drag, pxPerFrame, snapTargets);
      // Cross-track drop onto a compatible (same-kind, unlocked) lane.
      const target = laneAt(e.clientY);
      if (target && target.kind === trackKind && target.id !== clip.trackId && !target.locked) {
        moveClip(clip.id, newFrom, target.id);
      } else {
        moveClip(clip.id, newFrom);
      }
    } else if (Math.abs(deltaFrames) > 0) {
      trimClipEdge(clip.id, drag.kind === "trim-left" ? "left" : "right", deltaFrames);
    }
    endDragGhost();
    setDrag(null);
  }

  const cursor = locked ? "default" : drag?.kind === "move" ? "grabbing" : "grab";

  return (
    <div
      onPointerDown={onPointerDown}
      onPointerMove={onPointerMove}
      onPointerUp={onPointerUp}
      onDoubleClick={(e) => {
        e.stopPropagation();
        if (!locked) splitClip(clip.id, playhead);
      }}
      style={{ left, width, height: height - 6, top: 3, cursor, opacity: draggingThis ? 0.4 : 1 }}
      className={[
        "absolute rounded-md overflow-hidden border select-none group",
        selected
          ? "border-accent ring-1 ring-accent"
          : multiSelected
            ? "border-accent/70 ring-1 ring-accent/40"
            : "border-black/30",
      ].join(" ")}
      title={`${isShape ? "Shape" : isText ? "Text" : CLIP_LABEL[clip.source?.kind || ""] || "Clip"} · ${(clip.durationInFrames / 30).toFixed(2)}s`}
    >
      {isText ? (
        <div className="absolute inset-0 bg-violet-600/35 flex items-center gap-1.5 px-2">
          <span className="text-[12px] font-bold text-white/90 leading-none">T</span>
          <span className="min-w-0 truncate text-3xs text-white/85 leading-none">
            {clip.text?.content || "Text"}
          </span>
        </div>
      ) : isShape ? (
        <div className="absolute inset-0 bg-teal-600/35 flex items-center gap-1.5 px-2">
          <span className="text-[12px] text-white/90 leading-none">◇</span>
          <span className="min-w-0 truncate text-3xs text-white/85 leading-none capitalize">
            {clip.shape?.variant || "shape"}
          </span>
        </div>
      ) : isAudio ? (
        <div className="absolute inset-0 bg-emerald-600/30 flex items-end gap-px px-1 pb-2">
          {bars.map((b, i) => (
            <span key={i} style={{ height: `${Math.round(b * 70)}%` }} className="flex-1 bg-emerald-300/70 rounded-sm" />
          ))}
        </div>
      ) : thumb && !imgErr ? (
        <img
          src={thumb}
          alt=""
          draggable={false}
          onError={() => setImgErr(true)}
          className="absolute inset-0 w-full h-full object-cover opacity-80 pointer-events-none"
        />
      ) : (
        <div className="absolute inset-0 bg-cyan-700/40" />
      )}
      <div className="absolute inset-0 bg-gradient-to-t from-black/55 to-transparent pointer-events-none" />
      <span className="absolute bottom-1 left-1.5 text-[9.5px] font-mono text-white/90 pointer-events-none">
        {isAudio ? "🎵 " : ""}
        {(preview.durationInFrames / 30).toFixed(1)}s
      </span>
      {clip.muted && !isAudio && (
        <span className="absolute top-1 right-1.5 text-[9px] text-white/80 pointer-events-none" title="Sound is off">🔇</span>
      )}
      {/* On-clip keyframe diamonds (CapCut-style) — click to seek */}
      {kfFrames.map((f) => (
        <button
          key={f}
          onPointerDown={(e) => e.stopPropagation()}
          onClick={(e) => {
            e.stopPropagation();
            setPlayhead(clip.from + f);
          }}
          style={{ left: f * pxPerFrame, top: 2 }}
          title={`Mark at ${(f / 30).toFixed(2)}s, click to jump here`}
          className="absolute w-[7px] h-[7px] -translate-x-1/2 rotate-45 bg-yellow-300 border border-yellow-600 hover:bg-yellow-200 pointer-events-auto z-10"
        />
      ))}
      <div className="absolute left-0 top-0 h-full w-[9px] cursor-ew-resize bg-white/0 group-hover:bg-white/15" />
      <div className="absolute right-0 top-0 h-full w-[9px] cursor-ew-resize bg-white/0 group-hover:bg-white/15" />
    </div>
  );
});

function previewMoveFrom(
  clip: VideoEditClip,
  drag: DragState,
  pxPerFrame: number,
  snapTargets: number[],
): number {
  const deltaFrames = Math.round(drag.dx / pxPerFrame);
  let newFrom = Math.max(0, clip.from + deltaFrames);
  if (!drag.alt) {
    const cand = [...snapTargets, ...snapTargets.map((t) => t - clip.durationInFrames)];
    newFrom = Math.max(0, snapFrame(newFrom, cand, pxPerFrame));
  }
  return newFrom;
}

function computePreview(
  clip: VideoEditClip,
  drag: DragState | null,
  pxPerFrame: number,
  snapTargets: number[],
): { from: number; durationInFrames: number } {
  if (!drag) return { from: clip.from, durationInFrames: clip.durationInFrames };
  if (drag.kind === "move") {
    return { from: previewMoveFrom(clip, drag, pxPerFrame, snapTargets), durationInFrames: clip.durationInFrames };
  }
  const deltaFrames = Math.round(drag.dx / pxPerFrame);
  const r = applyTrimEdge(clip, drag.kind === "trim-left" ? "left" : "right", deltaFrames);
  return { from: r.from, durationInFrames: r.durationInFrames };
}
