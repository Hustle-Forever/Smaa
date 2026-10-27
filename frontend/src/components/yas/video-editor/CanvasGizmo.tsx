/**
 * Video Editor · on-canvas transform gizmo (CapCut-style)
 *
 * Overlays the <Player> preview. For the selected visual clip it draws a
 * selection box + 4 corner resize handles + a rotate knob; dragging them writes
 * x/y/scale/rotation live (the Player follows). Other on-canvas overlay clips
 * (text/shape/image) get a transparent hit-box so you can click to select +
 * move them. Hidden while playing.
 *
 * Coordinate math lives in lib/canvasGizmo.ts; it mirrors the composition's
 * `translate(x,y) scale(s) rotate(r)` on a full-frame layer (origin = center).
 */
import { useCallback, useLayoutEffect, useRef, useState } from "react";

import {
  computeGizmoBox,
  moveDelta,
  resizeDelta,
  rotateDelta,
  type GizmoBox,
  type GizmoTransform,
  type Pt,
} from "../../../lib/canvasGizmo";
import { sampleKeyframes } from "video-edit-schema";
import { useVideoEditorStore } from "../../../lib/store";
import type { VideoEditClip } from "../../../lib/types";

const ACCENT = "#0a84ff";

type DragMode = "move" | "resize" | "rotate";
interface Drag {
  mode: DragMode;
  clipId: string;
  t0: GizmoTransform;
  s: number;
  center: Pt; // container-relative
  ptr0: Pt; // container-relative
  startClientX: number;
  startClientY: number;
  containerLeft: number;
  containerTop: number;
  localFrame: number;
}

function clipKind(c: VideoEditClip): string {
  return c.kind || "video";
}
function isActive(c: VideoEditClip, playhead: number): boolean {
  return playhead >= c.from && playhead < c.from + c.durationInFrames;
}
function clipTransform(c: VideoEditClip, localFrame: number): GizmoTransform {
  const kf = c.keyframes || {};
  return {
    x: sampleKeyframes(kf.x, localFrame, 0),
    y: sampleKeyframes(kf.y, localFrame, 0),
    scale: sampleKeyframes(kf.scale, localFrame, 1),
    rotation: sampleKeyframes(kf.rotation, localFrame, 0),
  };
}
/** Natural (composition-px) box of a clip; null if it can't be measured yet. */
function naturalSize(c: VideoEditClip, compW: number, compH: number): { w: number; h: number } | null {
  const kind = clipKind(c);
  if (kind === "shape" && c.shape) return { w: c.shape.width, h: c.shape.height };
  if (kind === "text") {
    const el = document.querySelector(
      `[data-clip-id="${CSS.escape(c.id)}"] [data-ve-content]`,
    ) as HTMLElement | null;
    if (!el || !el.offsetWidth) return null;
    return { w: el.offsetWidth, h: el.offsetHeight };
  }
  // video / image → the full canvas (objectFit + transform frame it)
  return { w: compW, h: compH };
}

export function CanvasGizmo({
  containerRef,
  isPlaying,
}: {
  containerRef: React.RefObject<HTMLDivElement | null>;
  isPlaying: boolean;
}) {
  const selection = useVideoEditorStore((s) => s.selection);
  const toggleClipInSelection = useVideoEditorStore((s) => s.toggleClipInSelection);
  const clearMultiSelection = useVideoEditorStore((s) => s.clearMultiSelection);
  const selectMode = useVideoEditorStore((s) => s.selectMode);
  const clips = useVideoEditorStore((s) => s.clips);
  const playhead = useVideoEditorStore((s) => s.playheadFrame);
  const project = useVideoEditorStore((s) => s.project);
  const select = useVideoEditorStore((s) => s.select);

  const [, setTick] = useState(0); // force re-measure on resize
  const dragRef = useRef<Drag | null>(null);
  const rafRef = useRef<number | null>(null);
  const lastClient = useRef<Pt>({ x: 0, y: 0 });

  useLayoutEffect(() => {
    const el = containerRef.current;
    if (!el) return;
    const ro = new ResizeObserver(() => setTick((t) => t + 1));
    ro.observe(el);
    const onWin = () => setTick((t) => t + 1);
    window.addEventListener("resize", onWin);
    return () => {
      ro.disconnect();
      window.removeEventListener("resize", onWin);
    };
  }, [containerRef]);

  const onMove = useCallback((e: PointerEvent) => {
    const d = dragRef.current;
    if (!d) return;
    lastClient.current = { x: e.clientX, y: e.clientY };
    const shift = e.shiftKey;
    if (rafRef.current != null) return;
    rafRef.current = requestAnimationFrame(() => {
      rafRef.current = null;
      const d2 = dragRef.current;
      if (!d2) return;
      const ptr: Pt = {
        x: lastClient.current.x - d2.containerLeft,
        y: lastClient.current.y - d2.containerTop,
      };
      let patch: Partial<GizmoTransform>;
      if (d2.mode === "move") {
        patch = moveDelta(d2.t0, lastClient.current.x - d2.startClientX, lastClient.current.y - d2.startClientY, d2.s);
      } else if (d2.mode === "resize") {
        patch = resizeDelta(d2.t0, d2.center, d2.ptr0, ptr);
      } else {
        patch = rotateDelta(d2.t0, d2.center, d2.ptr0, ptr, shift);
      }
      useVideoEditorStore.getState().applyCanvasTransform(d2.clipId, patch, d2.localFrame);
    });
  }, []);

  const onUp = useCallback(() => {
    if (rafRef.current != null) {
      cancelAnimationFrame(rafRef.current);
      rafRef.current = null;
    }
    dragRef.current = null;
    // Close the undo step: the whole drag (per-rAF writes) = one undo entry.
    useVideoEditorStore.getState().endHistoryGesture();
    window.removeEventListener("pointermove", onMove);
    window.removeEventListener("pointerup", onUp);
  }, [onMove]);

  const beginDrag = useCallback(
    (mode: DragMode, e: React.PointerEvent, clip: VideoEditClip, box: GizmoBox, t: GizmoTransform) => {
      if (e.button !== 0) return;
      e.preventDefault();
      e.stopPropagation();
      const cr = containerRef.current?.getBoundingClientRect();
      if (!cr) return;
      dragRef.current = {
        mode,
        clipId: clip.id,
        t0: t,
        s: box.s,
        center: { x: box.cx, y: box.cy },
        ptr0: { x: e.clientX - cr.left, y: e.clientY - cr.top },
        startClientX: e.clientX,
        startClientY: e.clientY,
        containerLeft: cr.left,
        containerTop: cr.top,
        localFrame: Math.max(0, playhead - clip.from),
      };
      // Open one undo step for the whole drag gesture (closed in onUp).
      useVideoEditorStore.getState().beginHistoryGesture();
      window.addEventListener("pointermove", onMove);
      window.addEventListener("pointerup", onUp);
    },
    [containerRef, onMove, onUp, playhead],
  );

  if (!project || isPlaying) return null;
  const container = containerRef.current;
  const rootEl = typeof document !== "undefined" ? (document.querySelector('[data-ve-root="1"]') as HTMLElement | null) : null;
  if (!container || !rootEl) return null;
  const containerRect = container.getBoundingClientRect();
  const rootRect = rootEl.getBoundingClientRect();
  if (rootRect.width < 2) return null;
  const compW = project.width;
  const compH = project.height;

  const selClipId = selection && "clipId" in selection ? selection.clipId : null;
  const selClip = selClipId ? clips.find((c) => c.id === selClipId) : null;

  // Selected clip's gizmo box.
  let selBox: GizmoBox | null = null;
  let selT: GizmoTransform | null = null;
  if (selClip && clipKind(selClip) !== "audio" && isActive(selClip, playhead)) {
    const lf = playhead - selClip.from;
    selT = clipTransform(selClip, lf);
    const nat = naturalSize(selClip, compW, compH);
    if (nat) selBox = computeGizmoBox(selT, nat.w, nat.h, rootRect, containerRect, compW, compH);
  }

  // Hit-boxes for OTHER active overlay clips (text/shape/image) → click-to-select.
  const hitClips = clips.filter(
    (c) => c.id !== selClipId && ["text", "shape", "image"].includes(clipKind(c)) && isActive(c, playhead),
  );

  return (
    <div className="absolute inset-0 z-10" style={{ pointerEvents: "none" }}>
      {/* Hit-boxes for unselected overlay elements */}
      {hitClips.map((c) => {
        const lf = playhead - c.from;
        const t = clipTransform(c, lf);
        const nat = naturalSize(c, compW, compH);
        if (!nat) return null;
        const box = computeGizmoBox(t, nat.w, nat.h, rootRect, containerRect, compW, compH);
        return (
          <div
            key={c.id}
            onPointerDown={(e) => {
              if (e.button !== 0) return;
              // "Select items" mode (or Ctrl/Cmd-click): toggle into the multi-
              // selection instead of single-select + drag.
              if (selectMode || e.ctrlKey || e.metaKey) {
                toggleClipInSelection(c.id);
                select({ kind: "clip", clipId: c.id });
                return;
              }
              clearMultiSelection();
              select({ kind: "clip", clipId: c.id });
              beginDrag("move", e, c, box, t);
            }}
            style={{
              position: "absolute",
              left: box.cx,
              top: box.cy,
              width: box.w,
              height: box.h,
              transform: `translate(-50%, -50%) rotate(${box.rot}deg)`,
              pointerEvents: "auto",
              cursor: "move",
              touchAction: "none",
            }}
          />
        );
      })}

      {/* Selected clip gizmo */}
      {selBox && selT && selClip && (
        <>
          {/* Move body + border (the rotated box) */}
          <div
            onPointerDown={(e) => beginDrag("move", e, selClip, selBox!, selT!)}
            style={{
              position: "absolute",
              left: selBox.cx,
              top: selBox.cy,
              width: selBox.w,
              height: selBox.h,
              transform: `translate(-50%, -50%) rotate(${selBox.rot}deg)`,
              border: `1.5px solid ${ACCENT}`,
              boxShadow: "0 0 0 1px rgba(0,0,0,0.25)",
              pointerEvents: "auto",
              cursor: "move",
              touchAction: "none",
            }}
          >
            {/* Corner resize handles (center-anchored uniform scale) */}
            {([
              { l: "0%", t: "0%", cur: "nwse-resize" },
              { l: "100%", t: "0%", cur: "nesw-resize" },
              { l: "100%", t: "100%", cur: "nwse-resize" },
              { l: "0%", t: "100%", cur: "nesw-resize" },
            ] as const).map((h, i) => (
              <div
                key={i}
                onPointerDown={(e) => {
                  e.stopPropagation();
                  beginDrag("resize", e, selClip, selBox!, selT!);
                }}
                style={{
                  position: "absolute",
                  left: h.l,
                  top: h.t,
                  width: 13,
                  height: 13,
                  transform: "translate(-50%, -50%)",
                  background: "#fff",
                  border: `2px solid ${ACCENT}`,
                  borderRadius: 3,
                  pointerEvents: "auto",
                  cursor: h.cur,
                  touchAction: "none",
                }}
              />
            ))}
            {/* Rotate knob (above the top edge) */}
            <div
              onPointerDown={(e) => {
                e.stopPropagation();
                beginDrag("rotate", e, selClip, selBox!, selT!);
              }}
              style={{
                position: "absolute",
                left: "50%",
                top: -26,
                width: 15,
                height: 15,
                transform: "translate(-50%, -50%)",
                background: "#fff",
                border: `2px solid ${ACCENT}`,
                borderRadius: "50%",
                pointerEvents: "auto",
                cursor: "grab",
                touchAction: "none",
              }}
            />
            <div
              style={{
                position: "absolute",
                left: "50%",
                top: -19,
                width: 1.5,
                height: 19,
                transform: "translateX(-50%)",
                background: ACCENT,
                pointerEvents: "none",
              }}
            />
          </div>
        </>
      )}
    </div>
  );
}
