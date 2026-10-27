/**
 * Video Editor · track lane — one horizontal row holding a track's clip blocks.
 * On video lanes it also renders transition pills at clip boundaries (click to
 * select that clip → edit its transition in the Inspector).
 */
import { memo, useMemo } from "react";

import { useVideoEditorStore } from "../../../lib/store";
import type { VideoEditClip, VideoEditTrack } from "../../../lib/types";
import { ClipBlock } from "./ClipBlock";

const PRESET_ABBR: Record<string, string> = {
  fade: "Fade",
  slide: "Slide",
  wipe: "Wipe",
  flip: "Flip",
  clockWipe: "Clock",
  iris: "Iris",
};

export const TrackLane = memo(function TrackLane({
  track,
  clips,
  pxPerFrame,
  height,
  width,
  selectedClipId,
  snapTargets,
  playhead,
  laneAt,
  highlighted,
  onDeselect,
}: {
  track: VideoEditTrack;
  clips: VideoEditClip[];
  pxPerFrame: number;
  height: number;
  /** A CSS width — the timeline passes "100%" so the lane fills a column that
   *  CSS may have stretched past the clips' own pixel extent. */
  width: number | string;
  selectedClipId: string | null;
  snapTargets: number[];
  playhead: number;
  laneAt: (clientY: number) => VideoEditTrack | null;
  highlighted: boolean;
  onDeselect: () => void;
}) {
  const select = useVideoEditorStore((s) => s.select);

  // Transition boundaries: clips with a preceding adjacent/overlapping neighbour.
  const pills = useMemo(() => {
    if (track.kind !== "video") return [];
    const sorted = [...clips].sort((a, b) => a.from - b.from);
    const out: { clipId: string; from: number; preset?: string }[] = [];
    for (let i = 1; i < sorted.length; i++) {
      const c = sorted[i];
      const prev = sorted[i - 1];
      if (c.from <= prev.from + prev.durationInFrames) {
        out.push({ clipId: c.id, from: c.from, preset: c.transitionIn?.preset });
      }
    }
    return out;
  }, [clips, track.kind]);

  return (
    <div
      style={{ height, width }}
      className={[
        "relative border-b border-border/70",
        track.kind === "audio" ? "bg-success/[0.04]" : "bg-bg-subtle/30",
        track.locked ? "opacity-70" : "",
        highlighted ? "ring-2 ring-inset ring-accent bg-accent/5" : "",
      ].join(" ")}
      onPointerDown={(e) => {
        if (e.target === e.currentTarget) onDeselect();
      }}
    >
      {clips.map((c) => (
        <ClipBlock
          key={c.id}
          clip={c}
          pxPerFrame={pxPerFrame}
          height={height}
          selected={c.id === selectedClipId}
          snapTargets={snapTargets}
          playhead={playhead}
          trackKind={track.kind}
          locked={!!track.locked}
          laneAt={laneAt}
        />
      ))}

      {pills.map((p) => {
        const has = p.preset && p.preset !== "none";
        return (
          <button
            key={p.clipId}
            onPointerDown={(e) => e.stopPropagation()}
            onClick={(e) => {
              e.stopPropagation();
              select({ kind: "clip", clipId: p.clipId });
            }}
            style={{ left: p.from * pxPerFrame, top: 1 }}
            title={has ? `Transition: ${PRESET_ABBR[p.preset!] || p.preset}` : "Add a transition"}
            className={[
              "absolute z-10 -translate-x-1/2 h-[14px] px-1 rounded-b text-[8.5px] leading-[14px] font-medium transition-colors",
              has
                ? "bg-cyan-500 text-white"
                : "bg-bg-muted/80 text-muted border border-border opacity-60 hover:opacity-100",
            ].join(" ")}
          >
            {has ? `⇄ ${PRESET_ABBR[p.preset!] || ""}` : "⇄"}
          </button>
        );
      })}
    </div>
  );
});
