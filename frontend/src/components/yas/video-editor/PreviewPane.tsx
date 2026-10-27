/**
 * Video Editor · preview pane
 *
 * Wraps @remotion/player's <Player> rendering the SAME `video-edit` composition
 * the backend exports (WYSIWYG). inputProps are memoized so the Player doesn't
 * remount/reload on every keystroke; durationInFrames is derived with the SAME
 * shared helper the server uses, so the preview length matches the export.
 *
 * The Player lives in a child (PlayerSurface) so its event-binding lifecycle
 * matches the Player's mount. It exposes a module-level handle (playerHandle)
 * and keeps the store playhead in sync both ways:
 *   - playback → store (frameupdate)
 *   - timeline scrub → player (seekTo), guarded against the echo loop.
 */
import { useEffect, useMemo, useRef, useState } from "react";
import { prefetch } from "remotion";
import { Player, type PlayerRef } from "@remotion/player";
import { VideoEdit } from "video-edit-composition";
import { deriveDurationInFrames, FPS, type VideoEditProps } from "video-edit-schema";

import { useVideoEditorStore } from "../../../lib/store";
import { CanvasGizmo } from "./CanvasGizmo";
import { playerHandle } from "./playerHandle";

export function PreviewPane() {
  const project = useVideoEditorStore((s) => s.project);
  const clips = useVideoEditorStore((s) => s.clips);

  if (!project) {
    return <Shell><p className="text-[12px] text-white/50">Loading…</p></Shell>;
  }
  if (clips.length === 0) {
    return (
      <Shell>
        <div className="text-center px-6 space-y-1.5">
          <div className="text-2xl opacity-70">🎬</div>
          <p className="text-[13px] text-white/80 font-medium">Nothing here yet</p>
          <p className="text-2xs text-white/45 max-w-[26ch]">
            Add a clip from the Media panel on the left to see it here.
          </p>
        </div>
      </Shell>
    );
  }
  return <PlayerSurface />;
}

function PlayerSurface() {
  const project = useVideoEditorStore((s) => s.project)!;
  const tracks = useVideoEditorStore((s) => s.tracks);
  const clips = useVideoEditorStore((s) => s.clips);
  const playhead = useVideoEditorStore((s) => s.playheadFrame);
  const setPlayhead = useVideoEditorStore((s) => s.setPlayhead);

  const playerRef = useRef<PlayerRef>(null);
  const containerRef = useRef<HTMLDivElement>(null);
  const lastPlayerFrame = useRef(0);
  const [isPlaying, setIsPlaying] = useState(false);

  const inputProps = useMemo<VideoEditProps>(
    () => ({ project, tracks, clips }),
    [project, tracks, clips],
  );
  const duration = Math.max(1, deriveDurationInFrames(inputProps));

  // Prefetch every distinct clip source so the <Player> plays across cuts /
  // transitions WITHOUT pausing to buffer (the "stops for a sec between videos"
  // stutter). prefetch() downloads each file to an in-memory blob; OffthreadVideo
  // then loads it instantly. Re-runs only when the SET of source URLs changes
  // (not on every trim/move), and frees the blobs on change/unmount.
  const sourceUrls = useMemo(() => {
    const set = new Set<string>();
    for (const c of clips) if (c.sourceUrl) set.add(c.sourceUrl);
    return Array.from(set).sort();
  }, [clips]);
  const urlKey = sourceUrls.join("|");
  useEffect(() => {
    const handles = sourceUrls.map((u) => prefetch(u, { method: "blob-url" }));
    return () => {
      for (const h of handles) {
        try {
          h.free();
        } catch {
          /* already freed */
        }
      }
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [urlKey]);

  // Bind playback → store, and publish the handle.
  useEffect(() => {
    const pr = playerRef.current;
    if (!pr) return;
    playerHandle.current = pr;
    const onFrame = (e: { detail: { frame: number } }) => {
      lastPlayerFrame.current = e.detail.frame;
      setPlayhead(e.detail.frame);
    };
    const onPlay = () => setIsPlaying(true);
    const onPause = () => setIsPlaying(false);
    pr.addEventListener("frameupdate", onFrame);
    pr.addEventListener("play", onPlay);
    pr.addEventListener("pause", onPause);
    return () => {
      pr.removeEventListener("frameupdate", onFrame);
      pr.removeEventListener("play", onPlay);
      pr.removeEventListener("pause", onPause);
      if (playerHandle.current === pr) playerHandle.current = null;
    };
  }, [setPlayhead]);

  // Store playhead → player (scrub). Guard: skip when the change originated from
  // the player's own frameupdate (otherwise playback would fight the seek).
  useEffect(() => {
    const pr = playerRef.current;
    if (!pr) return;
    if (playhead !== lastPlayerFrame.current) {
      lastPlayerFrame.current = playhead;
      pr.seekTo(playhead);
    }
  }, [playhead]);

  const fps = project.fps || FPS;
  return (
    <div className="h-full w-full flex flex-col bg-black/90 rounded-xl overflow-hidden">
      {/* Canvas — Player + on-canvas gizmo overlay */}
      <div ref={containerRef} className="relative flex-1 min-h-0">
        <Player
          ref={playerRef}
          component={VideoEdit}
          inputProps={inputProps}
          durationInFrames={duration}
          compositionWidth={project.width}
          compositionHeight={project.height}
          fps={fps}
          acknowledgeRemotionLicense
          numberOfSharedAudioTags={16}
          style={{ width: "100%", height: "100%" }}
        />
        <CanvasGizmo containerRef={containerRef} isPlaying={isPlaying} />
      </div>
      {/* Transport */}
      <div className="shrink-0 h-9 flex items-center gap-3 px-3 border-t border-white/10 bg-black/60">
        <button
          onClick={() => playerRef.current?.toggle()}
          className="w-6 h-6 rounded-full bg-white/15 hover:bg-white/25 text-white flex items-center justify-center text-2xs"
          title={isPlaying ? "Pause (space)" : "Play (space)"}
        >
          {isPlaying ? "❚❚" : "►"}
        </button>
        <span className="text-2xs font-mono text-white/70 tabular-nums">
          {fmtTC(playhead, fps)} / {fmtTC(duration, fps)}
        </span>
        <span className="text-3xs text-white/35 ml-auto pr-1">drag to move · corners resize · the top handle turns it</span>
      </div>
    </div>
  );
}

function fmtTC(frames: number, fps: number): string {
  const totalSec = Math.max(0, frames) / fps;
  const m = Math.floor(totalSec / 60);
  const s = Math.floor(totalSec % 60);
  const f = Math.floor(Math.max(0, frames) % fps);
  return `${m}:${String(s).padStart(2, "0")}.${String(f).padStart(2, "0")}`;
}

function Shell({ children }: { children: React.ReactNode }) {
  return (
    <div className="h-full w-full flex items-center justify-center bg-black/90 rounded-xl overflow-hidden">
      {children}
    </div>
  );
}
