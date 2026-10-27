/**
 * Video Editor — shared timeline contract + pure math.
 *
 * This file is intentionally **framework-free** (no remotion / react runtime
 * imports) so it can be imported by BOTH:
 *   - the Remotion composition (VideoEdit.tsx) for server-side export, AND
 *   - the frontend editor (via a Vite alias) for the live <Player> preview and
 *     the timeline UI's duration/keyframe math.
 *
 * Keep it structurally identical to frontend/src/lib/types.ts + backend
 * services/yas_video_editor.py. Drift here means Player preview != export.
 * All time fields are INTEGER FRAMES.
 */

export const FPS = 30;

export type VideoEditAspect = "9:16" | "16:9" | "1:1";

export const ASPECT_DIMS: Record<VideoEditAspect, { width: number; height: number }> = {
  "9:16": { width: 1080, height: 1920 },
  "16:9": { width: 1920, height: 1080 },
  "1:1": { width: 1080, height: 1080 },
};

export type TransitionPreset =
  | "none"
  | "fade"
  | "slide"
  | "slideLeft"
  | "slideRight"
  | "slideUp"
  | "slideDown"
  | "wipe"
  | "flip"
  | "clockWipe"
  | "iris"
  | "zoom"
  | "blur";
export type TransitionTiming = "linear" | "spring";

export interface Keyframe {
  f: number; // frame relative to clip start
  v: number;
}

export interface ClipKeyframes {
  opacity?: Keyframe[];
  scale?: Keyframe[];
  x?: Keyframe[];
  y?: Keyframe[];
  rotation?: Keyframe[];
  volume?: Keyframe[];
}

export interface ClipTransition {
  preset: TransitionPreset;
  durationInFrames: number;
  timing?: TransitionTiming;
}

export interface ClipSource {
  kind: "animation" | "upload" | "composition" | "asset" | "still";
  animation_id?: number | null;
  upload_id?: number | null;
  composition_id?: number | null;
  still_id?: number | null;
  asset_id?: number | null;
  url?: string | null;
  naturalDurationInFrames?: number | null;
  audioFile?: boolean;
}

/** Visual layer kind. Absent/null = "video" (back-compat). */
export type ClipKind = "video" | "audio" | "text" | "shape" | "image";
export type ShapeVariant = "rect" | "ellipse" | "line" | "triangle" | "star";

export interface ClipFilters {
  brightness?: number;
  contrast?: number;
  saturate?: number;
  blur?: number;
}

export interface ClipText {
  content: string;
  fontFamily: string;
  fontUrl?: string | null;
  fontSize: number;
  color: string;
  fontWeight?: number;
  textAlign?: "left" | "center" | "right";
  lineHeight?: number;
  background?: string | null;
  padding?: number;
  stroke?: string | null;
  strokeWidth?: number;
  shadow?: string | null;
}

export interface ClipShape {
  variant: ShapeVariant;
  fill: string;
  stroke?: string | null;
  strokeWidth?: number;
  cornerRadius?: number;
  width: number;
  height: number;
}

export interface Clip {
  id: string;
  trackId: string;
  /** Absent for sourceless layers (text / shape). */
  source?: ClipSource;
  /** Resolved, browser/headless-fetchable URL. Set by the backend at fetch/export. */
  sourceUrl?: string | null;
  trimBefore: number;
  trimAfter: number;
  from: number;
  durationInFrames: number;
  keyframes?: ClipKeyframes;
  transitionIn?: ClipTransition | null;
  transitionOut?: ClipTransition | null;
  muted?: boolean;
  /** Playback rate for video/audio clips (1 = normal, 2 = 2x, 0.5 = slow-mo).
   * The timeline `durationInFrames` already reflects the re-timed length. */
  playbackRate?: number | null;
  fit?: "cover" | "contain";
  kind?: ClipKind | null;
  text?: ClipText | null;
  shape?: ClipShape | null;
  filters?: ClipFilters | null;
  /** Letterbox fill for video/image when it doesn't cover the frame:
   * "blur" = blurred cover-copy of the same media, a hex = solid colour, null = none. */
  background?: string | null;
}

export interface Track {
  id: string;
  kind: "video" | "audio";
  index: number;
  muted?: boolean;
  locked?: boolean;
  name?: string | null;
}

export interface ProjectMeta {
  title: string;
  aspect: VideoEditAspect;
  width: number;
  height: number;
  fps: number;
  background: string;
  durationInFrames: number;
}

/** The props passed to the Remotion composition (and the live <Player>).
 * Must be a `type` alias (not an interface) so it satisfies Remotion's
 * `Composition` generic constraint `Props extends Record<string, unknown>`. */
export type VideoEditProps = {
  project: ProjectMeta;
  tracks: Track[];
  clips: Clip[];
};

export function resolveDimensions(aspect: VideoEditAspect): { width: number; height: number } {
  return ASPECT_DIMS[aspect] ?? ASPECT_DIMS["9:16"];
}

/**
 * Linear keyframe sampling with clamped extrapolation — the exact behaviour of
 * remotion's `interpolate(frame, fs, vs, {extrapolate:"clamp"})`, implemented
 * in pure TS so the timeline UI can share it without a remotion import.
 * Assumes keyframes are sorted ascending by `f` (the editor maintains this).
 */
export function sampleKeyframes(
  kfs: Keyframe[] | undefined | null,
  frame: number,
  fallback: number,
): number {
  if (!kfs || kfs.length === 0) return fallback;
  if (kfs.length === 1) return kfs[0].v;
  if (frame <= kfs[0].f) return kfs[0].v;
  const last = kfs[kfs.length - 1];
  if (frame >= last.f) return last.v;
  for (let i = 0; i < kfs.length - 1; i++) {
    const a = kfs[i];
    const b = kfs[i + 1];
    if (frame >= a.f && frame <= b.f) {
      const span = b.f - a.f;
      if (span <= 0) return b.v;
      const t = (frame - a.f) / span;
      return a.v + (b.v - a.v) * t;
    }
  }
  return fallback;
}

/**
 * Total composition length in frames. The flat end is `max(from + duration)`
 * across all clips. Transition overlaps shorten the timeline; that subtraction
 * is layered in at Phase E (per contiguous video-track run). For now (no
 * transitions) the flat end is exact.
 */
export function deriveDurationInFrames(props: VideoEditProps): number {
  let end = 1;
  for (const c of props.clips || []) {
    const e = (c.from || 0) + (c.durationInFrames || 0);
    if (e > end) end = e;
  }
  return Math.max(1, Math.round(end));
}
