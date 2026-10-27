/**
 * Video Editor — the `video-edit` Remotion composition.
 *
 * Renders a multi-track timeline: video tracks stack as z-ordered
 * <AbsoluteFill> layers (array order by `index`, earlier index = bottom),
 * audio tracks emit a hidden <OffthreadVideo>. Per-clip opacity/scale/x/y/
 * rotation/volume are interpolated from keyframes.
 *
 * Transitions: within a video track, consecutive clips that are adjacent (gap
 * 0) or overlapping-with-a-transition form a "run" rendered via
 * <TransitionSeries>. The transition duration is derived from the ACTUAL
 * overlap between clips (the editor bakes the overlap into clip `from` via
 * ripple), so the timeline, the <Player>, and the server export agree exactly.
 *
 * This SAME component drives the in-browser <Player> preview (frontend, via a
 * Vite alias) and the server `renderMedia` export → WYSIWYG.
 */
import {
  AbsoluteFill,
  Audio,
  CalculateMetadataFunction,
  Img,
  interpolate,
  OffthreadVideo,
  Sequence,
  useCurrentFrame,
} from "remotion";
import {
  TransitionSeries,
  linearTiming,
  springTiming,
  type TransitionPresentation,
  type TransitionPresentationComponentProps,
} from "@remotion/transitions";
import { fade } from "@remotion/transitions/fade";
import { slide } from "@remotion/transitions/slide";
import { wipe } from "@remotion/transitions/wipe";
import { flip } from "@remotion/transitions/flip";
import { clockWipe } from "@remotion/transitions/clock-wipe";
import { iris } from "@remotion/transitions/iris";

import {
  Clip,
  FPS,
  Track,
  TransitionPreset,
  TransitionTiming,
  VideoEditProps,
  deriveDurationInFrames,
  sampleKeyframes,
} from "./schema";
// Side-effect: loads the curated Google fonts (and exposes ensureBrandFont for
// brand fonts) for BOTH the <Player> preview and the headless export.
import { ensureBrandFont } from "./fonts";

export const videoEditCalculateMetadata: CalculateMetadataFunction<VideoEditProps> = ({
  props,
}) => {
  const proj = props.project || ({} as VideoEditProps["project"]);
  return {
    durationInFrames: Math.max(1, deriveDurationInFrames(props)),
    fps: proj.fps || FPS,
    width: proj.width || 1080,
    height: proj.height || 1920,
  };
};

const clipUrl = (clip: Clip): string => clip.sourceUrl || clip.source?.url || "";

// Mount each clip's video/audio this many frames BEFORE it's shown so the media
// element loads + seeks + buffers ahead of the cut/transition. Without this the
// <Player> globally pauses to buffer when a new clip mounts at the boundary —
// the "stops for a sec between videos" stutter. Combined with prefetch() on the
// frontend (which caches the files), playback crosses boundaries seamlessly.
// premountFor renders the content invisibly + silently, so the export is
// unchanged (frames stay deterministic per-frame).
const PREMOUNT_FRAMES = 30; // ~1s at 30fps

/** CSS filter string from a clip's static filters (Player AND export). */
function buildFilter(clip: Clip): string | undefined {
  const f = clip.filters;
  if (!f) return undefined;
  const parts: string[] = [];
  if (f.brightness != null && f.brightness !== 1) parts.push(`brightness(${f.brightness})`);
  if (f.contrast != null && f.contrast !== 1) parts.push(`contrast(${f.contrast})`);
  if (f.saturate != null && f.saturate !== 1) parts.push(`saturate(${f.saturate})`);
  if (f.blur != null && f.blur > 0) parts.push(`blur(${f.blur}px)`);
  return parts.length ? parts.join(" ") : undefined;
}

// --- Custom presentations (no built-in equivalent) --------------------------
// Both the exiting and entering scenes render during the overlap; the component
// is applied to each with `presentationDirection` telling which is which.

/** Zoom / punch-in: entering scene eases in from 112% while fading up; the
 * exiting scene shrinks to 92% while fading out — a snappy cross-zoom. */
const ZoomPresentation: React.FC<TransitionPresentationComponentProps<Record<string, never>>> = ({
  children,
  presentationDirection,
  presentationProgress,
}) => {
  const entering = presentationDirection === "entering";
  const scale = entering
    ? interpolate(presentationProgress, [0, 1], [1.12, 1])
    : interpolate(presentationProgress, [0, 1], [1, 0.92]);
  const opacity = entering ? presentationProgress : 1 - presentationProgress;
  return <AbsoluteFill style={{ opacity, transform: `scale(${scale})` }}>{children}</AbsoluteFill>;
};
const zoom = (): TransitionPresentation<Record<string, never>> => ({
  component: ZoomPresentation,
  props: {},
});

/** Blur dissolve: entering scene unblurs from 14px while fading up; the exiting
 * scene blurs out — a soft, modern crossfade. */
const BlurPresentation: React.FC<TransitionPresentationComponentProps<Record<string, never>>> = ({
  children,
  presentationDirection,
  presentationProgress,
}) => {
  const entering = presentationDirection === "entering";
  const blurPx = entering
    ? interpolate(presentationProgress, [0, 1], [14, 0])
    : interpolate(presentationProgress, [0, 1], [0, 14]);
  const opacity = entering ? presentationProgress : 1 - presentationProgress;
  return <AbsoluteFill style={{ opacity, filter: `blur(${blurPx}px)` }}>{children}</AbsoluteFill>;
};
const blurDissolve = (): TransitionPresentation<Record<string, never>> => ({
  component: BlurPresentation,
  props: {},
});

// eslint-disable-next-line @typescript-eslint/no-explicit-any
function presentationFor(preset: TransitionPreset, width: number, height: number): any {
  switch (preset) {
    case "slide":
      return slide();
    case "slideLeft":
      return slide({ direction: "from-left" });
    case "slideRight":
      return slide({ direction: "from-right" });
    case "slideUp":
      return slide({ direction: "from-bottom" });
    case "slideDown":
      return slide({ direction: "from-top" });
    case "wipe":
      return wipe();
    case "flip":
      return flip();
    case "clockWipe":
      return clockWipe({ width, height });
    case "iris":
      return iris({ width, height });
    case "zoom":
      return zoom();
    case "blur":
      return blurDissolve();
    case "fade":
    default:
      return fade();
  }
}

// eslint-disable-next-line @typescript-eslint/no-explicit-any
function timingFor(timing: TransitionTiming | undefined, durationInFrames: number): any {
  return timing === "spring"
    ? springTiming({ durationInFrames, config: { damping: 200 } })
    : linearTiming({ durationInFrames });
}

/** Group a track's (from-sorted) clips into runs: a run is a maximal sequence
 * where each clip is adjacent (gap 0) to, or overlapping-with-a-transition,
 * the previous one. */
function buildRuns(sorted: Clip[]): Clip[][] {
  if (!sorted.length) return [];
  const runs: Clip[][] = [[sorted[0]]];
  for (let i = 1; i < sorted.length; i++) {
    const run = runs[runs.length - 1];
    const last = run[run.length - 1];
    const overlap = last.from + last.durationInFrames - sorted[i].from;
    const preset = sorted[i].transitionIn?.preset;
    const hasTransition = !!preset && preset !== "none";
    const connected = overlap === 0 || (overlap > 0 && hasTransition);
    if (connected) run.push(sorted[i]);
    else runs.push([sorted[i]]);
  }
  return runs;
}

/** Inner content of a video clip — the trimmed <OffthreadVideo> (keyframed
 * volume; the transform/opacity/filter wrapper lives in <ClipLayer>). */
const VideoClipBody: React.FC<{ clip: Clip; trackMuted?: boolean }> = ({ clip, trackMuted }) => {
  const frame = useCurrentFrame(); // clip-local (reset inside the Sequence)
  const kf = clip.keyframes || {};
  const muted = !!trackMuted || !!clip.muted;
  const volume = muted ? 0 : Math.max(0, sampleKeyframes(kf.volume, frame, 1));
  const url = clipUrl(clip);
  if (!url) return null;
  const trimBefore = Math.max(0, clip.trimBefore || 0);
  const trimAfter = clip.trimAfter || undefined;
  const playbackRate = clip.playbackRate || 1;
  const bg = clip.background;
  return (
    <AbsoluteFill>
      {/* Letterbox fill (when fit=contain / scaled-down leaves bars). */}
      {bg === "blur" ? (
        <OffthreadVideo
          src={url}
          trimBefore={trimBefore}
          trimAfter={trimAfter}
          playbackRate={playbackRate}
          muted
          style={{ width: "100%", height: "100%", objectFit: "cover", filter: "blur(40px) brightness(0.6)", transform: "scale(1.12)" }}
        />
      ) : bg ? (
        <AbsoluteFill style={{ backgroundColor: bg }} />
      ) : null}
      <OffthreadVideo
        src={url}
        trimBefore={trimBefore}
        trimAfter={trimAfter}
        playbackRate={playbackRate}
        volume={volume}
        muted={muted}
        style={{ width: "100%", height: "100%", objectFit: clip.fit || "cover" }}
      />
    </AbsoluteFill>
  );
};

/** Inner content of a text clip — centered, brand/Google font, optional
 * background pill + stroke + shadow. Layout matches the editor's TextInspector. */
const TextClipBody: React.FC<{ clip: Clip }> = ({ clip }) => {
  const t = clip.text;
  if (!t) return null;
  // Brand fonts load on demand (Google fonts are loaded at module top in fonts.ts).
  ensureBrandFont(t.fontFamily, t.fontUrl);
  const stroke =
    t.stroke && (t.strokeWidth ?? 0) > 0
      ? { WebkitTextStroke: `${t.strokeWidth}px ${t.stroke}` as string }
      : {};
  return (
    <AbsoluteFill
      style={{ display: "flex", alignItems: "center", justifyContent: "center", padding: "6%" }}
    >
      <div
        data-ve-content="1"
        style={{
          fontFamily: `"${t.fontFamily}", sans-serif`,
          fontSize: t.fontSize,
          color: t.color,
          fontWeight: t.fontWeight || 700,
          textAlign: t.textAlign || "center",
          lineHeight: t.lineHeight || 1.15,
          background: t.background || undefined,
          padding: t.background ? (t.padding ?? 16) : undefined,
          borderRadius: t.background ? 12 : undefined,
          textShadow: t.shadow || undefined,
          whiteSpace: "pre-wrap",
          wordBreak: "break-word",
          maxWidth: "100%",
          ...stroke,
        }}
      >
        {t.content}
      </div>
    </AbsoluteFill>
  );
};

function _trianglePoints(w: number, h: number): string {
  return `${w / 2},0 ${w},${h} 0,${h}`;
}
function _starPoints(w: number, h: number): string {
  const cx = w / 2;
  const cy = h / 2;
  const R = Math.min(w, h) / 2;
  const r = R * 0.5;
  const pts: string[] = [];
  for (let i = 0; i < 10; i++) {
    const ang = ((-90 + i * 36) * Math.PI) / 180;
    const rad = i % 2 === 0 ? R : r;
    pts.push(`${(cx + rad * Math.cos(ang)).toFixed(2)},${(cy + rad * Math.sin(ang)).toFixed(2)}`);
  }
  return pts.join(" ");
}

/** Inner content of a shape clip — rect / ellipse / line / triangle / star,
 * centered, fill + optional stroke. CSS for rect/ellipse/line, SVG polygon for
 * triangle/star (renders identically headless + in the Player). */
const ShapeClipBody: React.FC<{ clip: Clip }> = ({ clip }) => {
  const sh = clip.shape;
  if (!sh) return null;
  const { variant, fill, stroke, strokeWidth = 0, cornerRadius = 0, width, height } = sh;
  const center = { display: "flex", alignItems: "center", justifyContent: "center" } as const;
  if (variant === "rect" || variant === "ellipse") {
    return (
      <AbsoluteFill style={center}>
        <div
          style={{
            width,
            height,
            background: fill,
            border: stroke && strokeWidth > 0 ? `${strokeWidth}px solid ${stroke}` : undefined,
            borderRadius: variant === "ellipse" ? "50%" : cornerRadius,
          }}
        />
      </AbsoluteFill>
    );
  }
  if (variant === "line") {
    return (
      <AbsoluteFill style={center}>
        <div style={{ width, height: Math.max(2, strokeWidth || height || 8), background: stroke || fill }} />
      </AbsoluteFill>
    );
  }
  const points = variant === "triangle" ? _trianglePoints(width, height) : _starPoints(width, height);
  return (
    <AbsoluteFill style={center}>
      <svg width={width} height={height} viewBox={`0 0 ${width} ${height}`}>
        <polygon
          points={points}
          fill={fill}
          stroke={stroke && strokeWidth > 0 ? stroke : undefined}
          strokeWidth={strokeWidth > 0 ? strokeWidth : undefined}
          strokeLinejoin="round"
        />
      </svg>
    </AbsoluteFill>
  );
};

/** Inner content of an image/sticker clip — a render-safe <Img> (Remotion waits
 * for it to decode before painting). Filling the frame with object-fit; the
 * sticker is sized/placed via the clip's scale/x/y keyframes. */
const ImageClipBody: React.FC<{ clip: Clip }> = ({ clip }) => {
  const url = clipUrl(clip);
  if (!url) return null;
  const bg = clip.background;
  // Passing onError makes Remotion's <Img> NOT throw when an image fails to
  // decode (corrupt/unreachable source → "source image cannot be decoded") —
  // it just renders nothing instead of crashing the Player/export.
  const onError = (e: unknown) => {
    // eslint-disable-next-line no-console
    console.warn(`[video-edit] image clip failed to decode: ${url}`, e);
  };
  return (
    <AbsoluteFill style={{ display: "flex", alignItems: "center", justifyContent: "center" }}>
      {bg === "blur" ? (
        <Img src={url} onError={onError} style={{ position: "absolute", inset: 0, width: "100%", height: "100%", objectFit: "cover", filter: "blur(40px) brightness(0.6)", transform: "scale(1.12)" }} />
      ) : bg ? (
        <AbsoluteFill style={{ backgroundColor: bg }} />
      ) : null}
      <Img src={url} onError={onError} style={{ width: "100%", height: "100%", objectFit: clip.fit || "contain" }} />
    </AbsoluteFill>
  );
};

/** A single clip layer — keyframed opacity/transform + CSS filter wrapper,
 * dispatched by `clip.kind` (absent/null = video, back-compat). Used both by a
 * plain <Sequence> and inside a <TransitionSeries.Sequence>. */
const ClipLayer: React.FC<{ clip: Clip; trackMuted?: boolean }> = ({ clip, trackMuted }) => {
  const frame = useCurrentFrame(); // clip-local (reset inside the Sequence)
  const kf = clip.keyframes || {};
  const opacity = sampleKeyframes(kf.opacity, frame, 1);
  const scale = sampleKeyframes(kf.scale, frame, 1);
  const x = sampleKeyframes(kf.x, frame, 0);
  const y = sampleKeyframes(kf.y, frame, 0);
  const rotation = sampleKeyframes(kf.rotation, frame, 0);
  const kind = clip.kind || "video";
  const body =
    kind === "text" ? (
      <TextClipBody clip={clip} />
    ) : kind === "shape" ? (
      <ShapeClipBody clip={clip} />
    ) : kind === "image" ? (
      <ImageClipBody clip={clip} />
    ) : (
      <VideoClipBody clip={clip} trackMuted={trackMuted} />
    );
  return (
    <AbsoluteFill
      data-clip-id={clip.id}
      style={{
        opacity,
        transform: `translate(${x}px, ${y}px) scale(${scale}) rotate(${rotation}deg)`,
        filter: buildFilter(clip),
      }}
    >
      {body}
    </AbsoluteFill>
  );
};

/** A single audio clip — keyframed volume, no visual. Sources are videos, so we
 * use a hidden <OffthreadVideo> (Remotion's recommended way to play a video's
 * audio) rather than <Audio> (which is for audio files). */
const AudioClipLayer: React.FC<{ clip: Clip; trackMuted?: boolean }> = ({ clip, trackMuted }) => {
  const frame = useCurrentFrame();
  const kf = clip.keyframes || {};
  const muted = !!trackMuted || !!clip.muted;
  const volume = muted ? 0 : Math.max(0, sampleKeyframes(kf.volume, frame, 1));
  const url = clipUrl(clip);
  if (!url) return null;
  // A true audio/music file → <Audio>; a detached video's audio → hidden
  // <OffthreadVideo> (Remotion's way to play a video's audio track).
  const playbackRate = clip.playbackRate || 1;
  if (clip.source?.audioFile) {
    return (
      <Audio
        src={url}
        trimBefore={Math.max(0, clip.trimBefore || 0)}
        trimAfter={clip.trimAfter || undefined}
        playbackRate={playbackRate}
        muted={muted}
        volume={volume}
      />
    );
  }
  return (
    <OffthreadVideo
      src={url}
      trimBefore={Math.max(0, clip.trimBefore || 0)}
      trimAfter={clip.trimAfter || undefined}
      playbackRate={playbackRate}
      muted={muted}
      volume={volume}
      style={{ position: "absolute", width: 1, height: 1, opacity: 0, pointerEvents: "none" }}
    />
  );
};

/** One video track → runs, each rendered as a Sequence (single clip) or a
 * TransitionSeries (multiple connected clips). */
const VideoTrackContent: React.FC<{
  track: Track;
  clips: Clip[];
  width: number;
  height: number;
}> = ({ track, clips, width, height }) => {
  const sorted = [...clips].sort((a, b) => a.from - b.from);
  const runs = buildRuns(sorted);
  return (
    <AbsoluteFill>
      {runs.map((run) => {
        if (run.length === 1) {
          const c = run[0];
          return (
            <Sequence
              key={c.id}
              from={Math.max(0, c.from)}
              durationInFrames={Math.max(1, c.durationInFrames)}
              premountFor={PREMOUNT_FRAMES}
            >
              <ClipLayer clip={c} trackMuted={track.muted} />
            </Sequence>
          );
        }
        const runFrom = run[0].from;
        const last = run[run.length - 1];
        const runLen = Math.max(1, last.from + last.durationInFrames - runFrom);
        return (
          <Sequence
            key={run[0].id}
            from={Math.max(0, runFrom)}
            durationInFrames={runLen}
            premountFor={PREMOUNT_FRAMES}
          >
            <TransitionSeries>
              {run.flatMap((c, i) => {
                const items: React.ReactNode[] = [];
                if (i > 0) {
                  const overlap = Math.round(run[i - 1].from + run[i - 1].durationInFrames - c.from);
                  const preset = c.transitionIn?.preset;
                  if (overlap > 0 && preset && preset !== "none") {
                    items.push(
                      <TransitionSeries.Transition
                        key={`${c.id}-t`}
                        timing={timingFor(c.transitionIn?.timing, overlap)}
                        presentation={presentationFor(preset, width, height)}
                      />,
                    );
                  }
                }
                items.push(
                  <TransitionSeries.Sequence key={c.id} durationInFrames={Math.max(1, c.durationInFrames)}>
                    <ClipLayer clip={c} trackMuted={track.muted} />
                  </TransitionSeries.Sequence>,
                );
                return items;
              })}
            </TransitionSeries>
          </Sequence>
        );
      })}
    </AbsoluteFill>
  );
};

export const VideoEdit: React.FC<VideoEditProps> = ({ project, tracks, clips }) => {
  const safeTracks = tracks || [];
  const safeClips = clips || [];
  const clipsOf = (trackId: string) => safeClips.filter((c) => c.trackId === trackId);

  const videoTracks = safeTracks
    .filter((t) => t.kind === "video")
    .sort((a, b) => (a.index ?? 0) - (b.index ?? 0));
  const audioTracks = safeTracks.filter((t) => t.kind === "audio");
  const width = project?.width || 1080;
  const height = project?.height || 1920;

  return (
    <AbsoluteFill data-ve-root="1" style={{ backgroundColor: project?.background || "#000000" }}>
      {/* Video tracks — earlier index renders first → later composites on top. */}
      {videoTracks.map((t) => (
        <VideoTrackContent key={t.id} track={t} clips={clipsOf(t.id)} width={width} height={height} />
      ))}

      {/* Audio tracks — no visual output. */}
      {audioTracks.map((t) =>
        clipsOf(t.id).map((c) => (
          <Sequence
            key={c.id}
            from={Math.max(0, c.from || 0)}
            durationInFrames={Math.max(1, c.durationInFrames || 1)}
            premountFor={PREMOUNT_FRAMES}
          >
            <AudioClipLayer clip={c} trackMuted={t.muted} />
          </Sequence>
        )),
      )}
    </AbsoluteFill>
  );
};
