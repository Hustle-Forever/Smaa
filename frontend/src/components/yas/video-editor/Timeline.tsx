/**
 * Video Editor · timeline
 *
 * Orchestrates the ruler + track lanes + playhead. Owns zoom, content-width
 * measurement, the clientX→frame and clientY→lane mapping (cross-track drag),
 * snap targets, the toolbar (split / delete / add-track / zoom), and keyboard
 * shortcuts (Del, S=split).
 *
 * Display order: video tracks by index DESC (foreground on top), then audio —
 * matching the composition's z-order (it renders video by index ASC, bottom
 * first). The gutter (TrackHeaders) and lanes share this order.
 */
import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import type { DragEvent } from "react";

import { useVideoEditorStore } from "../../../lib/store";
import type { VideoEditTrack } from "../../../lib/types";
import { DragGhost } from "./DragGhost";
import { playerHandle } from "./playerHandle";
import { Playhead } from "./Playhead";
import { ATTACHMENT_MIME, parseAttachmentDrop } from "./sidebar/attachmentDnd";
import { entryToSource } from "./sidebar/MediaBinPanel";
import { RULER_H, TimeRuler } from "./TimeRuler";
import { TrackHeader } from "./TrackHeader";
import { TrackLane } from "./TrackLane";

const LANE_H = 52;
const GUTTER_W = 116;
const TAIL_FRAMES = 90; // 3s of empty room at the end for dragging

export function Timeline() {
  const project = useVideoEditorStore((s) => s.project);
  const tracks = useVideoEditorStore((s) => s.tracks);
  const clips = useVideoEditorStore((s) => s.clips);
  const pxPerFrame = useVideoEditorStore((s) => s.pxPerFrame);
  const setZoom = useVideoEditorStore((s) => s.setZoom);
  const playhead = useVideoEditorStore((s) => s.playheadFrame);
  const selection = useVideoEditorStore((s) => s.selection);
  const select = useVideoEditorStore((s) => s.select);
  const splitClip = useVideoEditorStore((s) => s.splitClip);
  const removeClip = useVideoEditorStore((s) => s.removeClip);
  const addTrack = useVideoEditorStore((s) => s.addTrack);
  const addSourceAtPosition = useVideoEditorStore((s) => s.addSourceAtPosition);
  const toggleMasterKeyframe = useVideoEditorStore((s) => s.toggleMasterKeyframe);
  const undo = useVideoEditorStore((s) => s.undo);
  const redo = useVideoEditorStore((s) => s.redo);
  const canUndo = useVideoEditorStore((s) => s.canUndo);
  const canRedo = useVideoEditorStore((s) => s.canRedo);
  const ghostTargetTrackId = useVideoEditorStore((s) => s.dragGhost?.targetTrackId ?? null);

  const scrollRef = useRef<HTMLDivElement>(null);
  const contentRef = useRef<HTMLDivElement>(null);
  const lanesRef = useRef<HTMLDivElement>(null);
  const [containerW, setContainerW] = useState(600);
  const [dropActive, setDropActive] = useState(false);

  const fps = project?.fps || 30;
  const selectedClipId = selection && "clipId" in selection ? selection.clipId : null;

  useLayoutEffect(() => {
    const el = scrollRef.current;
    if (!el) return;
    const ro = new ResizeObserver(() => setContainerW(el.clientWidth));
    ro.observe(el);
    setContainerW(el.clientWidth);
    return () => ro.disconnect();
  }, []);

  // Display order: video foreground (high index) on top, then audio.
  const displayTracks = useMemo(() => {
    const vids = tracks.filter((t) => t.kind === "video").sort((a, b) => b.index - a.index);
    const auds = tracks.filter((t) => t.kind === "audio").sort((a, b) => a.index - b.index);
    return [...vids, ...auds];
  }, [tracks]);

  const totalFrames = useMemo(() => {
    let end = 0;
    for (const c of clips) end = Math.max(end, (c.from || 0) + (c.durationInFrames || 0));
    return end;
  }, [clips]);

  // scrollRef now wraps BOTH the gutter and the lanes (single both-axes scroll
  // container), so the lane viewport is its width minus the sticky gutter.
  // Used for "Fit" and for the ruler's tick extent — deliberately NOT for the
  // rendered width; see below.
  const laneViewportW = Math.max(0, containerW - GUTTER_W);
  // The content width is a function of the TIMELINE only, never of the measured
  // container. It used to be `Math.max(laneViewportW, frames * pxPerFrame)`,
  // which closed a feedback loop the moment the first term won (i.e. whenever
  // you zoomed out): rendered width came from clientWidth, clientWidth changed
  // when a scrollbar appeared, and a scrollbar appeared because of the rendered
  // width. The timeline then re-laid-out every frame, shaking by one scrollbar
  // width — and stopped the instant you zoomed back in and the frames term took
  // over (#34). Filling the viewport for a short project is a job for CSS
  // (`min-w-full` on the row below), which has no way to feed back into JS.
  const contentPx = (totalFrames + TAIL_FRAMES) * pxPerFrame;
  const lanesHeight = displayTracks.length * LANE_H;

  const snapTargets = useMemo(() => {
    const t = new Set<number>([0, playhead]);
    for (const c of clips) {
      t.add(c.from);
      t.add(c.from + c.durationInFrames);
    }
    return Array.from(t);
  }, [clips, playhead]);

  const frameAtClientX = useCallback(
    (clientX: number) => {
      const el = contentRef.current;
      if (!el) return 0;
      const rect = el.getBoundingClientRect();
      return Math.max(0, (clientX - rect.left) / pxPerFrame);
    },
    [pxPerFrame],
  );

  const laneAt = useCallback(
    (clientY: number): VideoEditTrack | null => {
      const el = lanesRef.current;
      if (!el) return null;
      const rect = el.getBoundingClientRect();
      const idx = Math.floor((clientY - rect.top) / LANE_H);
      return displayTracks[idx] ?? null;
    },
    [displayTracks],
  );

  // --- Drag-and-drop: a Media-bin item dropped onto the lanes lands as a
  // full-frame clip at the drop position on the dropped-on track. Reuses the
  // SAME drag payload as the AI-chat drop (application/x-ve-chat-attachment) —
  // the two targets live in separate DOM subtrees, so the chat drop is untouched.
  const onLaneDragOver = useCallback((e: DragEvent) => {
    if (!e.dataTransfer.types.includes(ATTACHMENT_MIME)) return;
    e.preventDefault();
    e.dataTransfer.dropEffect = "copy";
    setDropActive(true);
  }, []);

  const onLaneDrop = useCallback(
    (e: DragEvent) => {
      const parsed = parseAttachmentDrop(e.dataTransfer);
      setDropActive(false);
      // Only handle media items; let anything else fall through untouched.
      if (!parsed || parsed.kind !== "media") return;
      e.preventDefault();
      const bin = useVideoEditorStore.getState().project?.media_bin || [];
      const entry = bin.find((b) => b.kind === parsed.sourceKind && b.id === parsed.sourceId);
      if (!entry) return;
      // Drop onto a video track; a drop over audio (or off-lane) falls back to
      // the primary (lowest-index) video track.
      let track = laneAt(e.clientY);
      if (!track || track.kind !== "video") {
        track = tracks.filter((t) => t.kind === "video").sort((a, b) => a.index - b.index)[0] ?? null;
      }
      if (!track) return;
      const from = Math.round(frameAtClientX(e.clientX));
      addSourceAtPosition(entryToSource(entry), track.id, from);
    },
    [laneAt, tracks, frameAtClientX, addSourceAtPosition],
  );

  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      const el = document.activeElement as HTMLElement | null;
      if (el && (el.tagName === "INPUT" || el.tagName === "TEXTAREA" || el.isContentEditable)) return;
      const st = useVideoEditorStore.getState();
      // Undo / redo (Ctrl/Cmd+Z, Ctrl/Cmd+Shift+Z, Ctrl/Cmd+Y)
      if ((e.ctrlKey || e.metaKey) && (e.key === "z" || e.key === "Z")) {
        e.preventDefault();
        if (e.shiftKey) st.redo();
        else st.undo();
        return;
      }
      if ((e.ctrlKey || e.metaKey) && (e.key === "y" || e.key === "Y")) {
        e.preventDefault();
        st.redo();
        return;
      }
      // Transport / playhead (no selection needed)
      if (e.key === " ") {
        e.preventDefault();
        try {
          playerHandle.current?.toggle();
        } catch {
          /* player not mounted */
        }
        return;
      }
      if (e.key === "ArrowLeft" || e.key === "ArrowRight") {
        e.preventDefault();
        const step = (e.shiftKey ? 10 : 1) * (e.key === "ArrowLeft" ? -1 : 1);
        st.setPlayhead(Math.max(0, st.playheadFrame + step));
        return;
      }
      if (e.key === "Home") {
        e.preventDefault();
        st.setPlayhead(0);
        return;
      }
      if (e.key === "End") {
        e.preventDefault();
        const total = st.clips.reduce((m, c) => Math.max(m, (c.from || 0) + (c.durationInFrames || 0)), 0);
        st.setPlayhead(total);
        return;
      }
      // Clip ops (need a selection)
      const cid = st.selection && "clipId" in st.selection ? st.selection.clipId : null;
      if (!cid) return;
      if (e.key === "Delete" || e.key === "Backspace") {
        e.preventDefault();
        st.removeClip(cid);
      } else if (e.key === "s" || e.key === "S") {
        e.preventDefault();
        st.splitClip(cid, st.playheadFrame);
      }
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  const clipsByTrack = useCallback((trackId: string) => clips.filter((c) => c.trackId === trackId), [clips]);

  // For reorder-arrow enabling: position among same-kind tracks (ascending index).
  const sameKindOrder = useMemo(() => {
    const m: Record<string, { pos: number; len: number }> = {};
    for (const kind of ["video", "audio"] as const) {
      const arr = tracks.filter((t) => t.kind === kind).sort((a, b) => a.index - b.index);
      arr.forEach((t, i) => (m[t.id] = { pos: i, len: arr.length }));
    }
    return m;
  }, [tracks]);
  const videoCount = tracks.filter((t) => t.kind === "video").length;

  return (
    <div className="h-full w-full rounded-xl border border-border bg-bg-subtle/40 flex flex-col overflow-hidden">
      {/* Toolbar */}
      <div className="shrink-0 flex items-center gap-2 px-2.5 py-1.5 border-b border-border">
        <span className="eyebrow">Timeline</span>
        <span className="text-3xs font-mono text-muted">
          {fmtTC(playhead, fps)} · {clips.length} clip{clips.length === 1 ? "" : "s"}
        </span>
        <div className="w-px h-4 bg-border mx-0.5" />
        <button onClick={() => undo()} disabled={!canUndo} className="btn btn-ghost text-2xs px-2 py-0.5" title="Undo (Ctrl+Z)">
          ↶ Undo
        </button>
        <button onClick={() => redo()} disabled={!canRedo} className="btn btn-ghost text-2xs px-2 py-0.5" title="Redo (Ctrl+Shift+Z)">
          ↷ Redo
        </button>
        <div className="flex-1" />
        <button onClick={() => addTrack("video")} className="btn btn-ghost text-2xs px-2 py-0.5" title="Add another row for video, to stack a clip on top">
          + Video
        </button>
        <button onClick={() => addTrack("audio")} className="btn btn-ghost text-2xs px-2 py-0.5" title="Add another row for sound">
          + Audio
        </button>
        <div className="w-px h-4 bg-border mx-0.5" />
        <button onClick={() => selectedClipId && splitClip(selectedClipId, playhead)} disabled={!selectedClipId} className="btn btn-ghost text-2xs px-2 py-0.5" title="Cut the clip where the line is (S)">
          ✂ Split
        </button>
        <button
          onClick={() => selectedClipId && toggleMasterKeyframe(selectedClipId, playhead)}
          disabled={!selectedClipId}
          className="btn btn-ghost text-2xs px-2 py-0.5"
          title="Mark this moment, so size and position can change from here (◆)"
        >
          ◆ Mark
        </button>
        <button onClick={() => selectedClipId && removeClip(selectedClipId)} disabled={!selectedClipId} className="btn btn-ghost text-2xs px-2 py-0.5 text-danger" title="Delete the clip you picked (Del)">
          Delete
        </button>
        <div className="w-px h-4 bg-border mx-0.5" />
        <button
          onClick={() => setZoom((laneViewportW - 8) / Math.max(1, totalFrames))}
          disabled={totalFrames === 0}
          className="btn btn-ghost text-2xs px-1.5 py-0.5"
          title="Show the whole video"
        >
          Fit
        </button>
        <button onClick={() => setZoom(pxPerFrame / 1.5)} className="btn btn-ghost text-2xs px-1.5 py-0.5" title="Zoom out">−</button>
        <button onClick={() => setZoom(pxPerFrame * 1.5)} className="btn btn-ghost text-2xs px-1.5 py-0.5" title="Zoom in">＋</button>
      </div>

      {/* Single scroll container — scrolls BOTH axes. The gutter sticks left,
          the ruler sticks top, so many tracks scroll vertically while the
          ruler + track headers stay pinned. */}
      <div ref={scrollRef} className="flex-1 min-h-0 overflow-auto">
        <div className="flex min-w-full" style={{ width: GUTTER_W + contentPx }}>
          {/* Gutter (track headers) — sticky on the horizontal axis */}
          <div style={{ width: GUTTER_W }} className="sticky left-0 z-30 shrink-0 border-r border-border bg-bg-subtle/80 backdrop-blur-sm">
            <div style={{ height: RULER_H }} className="sticky top-0 z-10 border-b border-border bg-bg-subtle/80" />
            {displayTracks.map((t) => {
              const ord = sameKindOrder[t.id];
              return (
                <TrackHeader
                  key={t.id}
                  track={t}
                  height={LANE_H}
                  label={`${t.kind === "video" ? "V" : "A"}${(ord?.pos ?? 0) + 1}`}
                  canUp={!!ord && ord.pos < ord.len - 1}
                  canDown={!!ord && ord.pos > 0}
                  canRemove={t.kind === "audio" || videoCount > 1}
                />
              );
            })}
          </div>

          {/* Content (ruler + lanes + playhead) — scrolls horizontally; ruler pins to top */}
          {/* `grow` lets the row's `min-w-full` stretch this column to fill the
              viewport on a short project — the job `Math.max(laneViewportW, …)`
              used to do in JS, now done where it cannot feed back (#34). */}
          <div ref={contentRef} style={{ width: contentPx }} className="relative grow shrink-0">
            <div className="sticky top-0 z-20">
              <TimeRuler
                pxPerFrame={pxPerFrame}
                contentPx={contentPx}
                minPx={laneViewportW}
                fps={fps}
                frameAtClientX={frameAtClientX}
              />
            </div>
            <div
              ref={lanesRef}
              className={["relative", dropActive ? "ring-2 ring-inset ring-accent/60 rounded" : ""].join(" ")}
              onDragOver={onLaneDragOver}
              onDrop={onLaneDrop}
              onDragLeave={() => setDropActive(false)}
            >
              {displayTracks.map((t) => (
                <TrackLane
                  key={t.id}
                  track={t}
                  clips={clipsByTrack(t.id)}
                  pxPerFrame={pxPerFrame}
                  height={LANE_H}
                  // 100%, not contentPx: the column is stretched by the row's
                  // `min-w-full` when the project is shorter than the viewport,
                  // and a fixed px width would leave the lane backgrounds short
                  // of the right edge.
                  width="100%"
                  selectedClipId={selectedClipId}
                  snapTargets={snapTargets}
                  playhead={playhead}
                  laneAt={laneAt}
                  highlighted={ghostTargetTrackId === t.id}
                  onDeselect={() => select(null)}
                />
              ))}
            </div>
            <Playhead pxPerFrame={pxPerFrame} height={RULER_H + lanesHeight} frameAtClientX={frameAtClientX} />
          </div>
        </div>
      </div>
      <DragGhost />
    </div>
  );
}

function fmtTC(frames: number, fps: number): string {
  const totalSec = frames / fps;
  const m = Math.floor(totalSec / 60);
  const s = Math.floor(totalSec % 60);
  const f = Math.floor(frames % fps);
  return `${m}:${String(s).padStart(2, "0")}.${String(f).padStart(2, "0")}`;
}
