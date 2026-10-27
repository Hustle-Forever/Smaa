/**
 * Resolves the reel timeline into per-segment facts and per-frame states.
 *
 * A frame f belongs to exactly one segment S. The seam that STARTED S (or the
 * reel start) decides whether S's layout makes an entrance; the seam that ENDS
 * S decides whether it makes an exit. Cover/short seams never animate the
 * layouts: the wipe hides the swap, so the incoming layout is simply there
 * when revealed. Glitch seams don't either: the whole overlay glitches over
 * the cut and the new layout is there when it settles. Hand-off seams exit
 * the outgoing layout before the cut and run the full entrance after it.
 */
import { EASE } from "./easing";
import { geometryFor, sameFull, sameRect, type Geometry } from "./geometry";
import type { GlitchState } from "./Glitch";
import type { PatternSlug } from "./patternTiles";
import { COLOR, GLITCH, MOTION, PATTERN, type Palette } from "./tokens";
import type { BrandIconCue, BrandOverlayProps, BrandSeam, BrandSegment } from "./types";

export type SegmentModel = {
  seg: BrandSegment;
  i: number;
  geom: Geometry;
  start: BrandSeam | null;
  end: BrandSeam | null;
  /** Frame the entrance animations are timed from; null = present from the first frame. */
  entrance: number | null;
  /** Hand-off cut at the end of this segment (exit runs over the frames before it); null = no exit. */
  exitAt: number | null;
  logoVisible: boolean;
  /** Frame the logo's fade + rise starts; null = the logo is simply there. */
  logoEntrance: number | null;
  /** The next segment keeps the logo exactly here, so it must not leave. */
  logoStays: boolean;
  /**
   * "full" layout across a hand-off to / from an identical full layout: the
   * corner slab (and the logo on it) simply stays — only the wedge exits and
   * re-enters. Without this the slab slid out from under a logo that stayed,
   * leaving the white lockup on bare footage for ~7 frames.
   */
  cornerStaysIn: boolean;
  cornerStaysOut: boolean;
  /** The one pattern this segment slides (PATTERN.cycle by position: neighbours always differ). */
  pattern: PatternSlug;
};

export type ReelModel = {
  W: number;
  H: number;
  segments: SegmentModel[];
  seams: BrandSeam[];
  /** First frame of the heading typewriter (null = no scene segment). */
  typeStart: number | null;
  palette: Palette;
  /** Topic icons, in reel order, each clipped to its scene segment. */
  icons: BrandIconCue[];
  edgePatterns: boolean;
};

/**
 * Wipe windows (frames relative to the cut F, inclusive):
 *   cover: in F-8..F-4, full cover F-3..F+3, out F+4..F+10   (19 f)
 *   short: in F-5..F-2, full cover F-1..F+1, out F+2..F+4    (10 f)
 * Within the n frames of an in/out range, frame k is sampled at the MIDDLE of
 * its frame interval, t = (k + 0.5) / n, so no frame of either range is a
 * duplicate of the hold or of the clean layout. `e` is that t through
 * in-out-cubic; SlabWipe turns it into an edge position (see there).
 */
export const WIPE_WINDOWS = {
  cover: { inFrom: -8, inTo: -4, holdTo: 3, outTo: 10 },
  short: { inFrom: -5, inTo: -2, holdTo: 1, outTo: 4 },
} as const;

export type WipeState = {
  seam: BrandSeam;
  phase: "in" | "hold" | "out";
  /** Eased progress of the wipe (0..1); 1 during the hold. */
  e: number;
  /** The segment the wipe grows out of (ends at the cut). */
  outgoing: SegmentModel;
  /** The segment it uncovers (starts at the cut). */
  incoming: SegmentModel;
};

export function validateProps(props: BrandOverlayProps): void {
  const segs = props.segments ?? [];
  if (segs.length === 0) throw new Error("brand-overlay-42: props.segments is empty");
  let at = 0;
  for (const [i, seg] of segs.entries()) {
    if (seg.from !== at) {
      throw new Error(`brand-overlay-42: segment ${i} starts at ${seg.from}, expected ${at} (segments must be contiguous)`);
    }
    if (!(seg.frames > 0)) throw new Error(`brand-overlay-42: segment ${i} has ${seg.frames} frames`);
    at += seg.frames;
  }
  if (props.durationInFrames !== at) {
    throw new Error(`brand-overlay-42: durationInFrames ${props.durationInFrames} != sum(segment frames) ${at}`);
  }
  const seams = props.seams ?? [];
  if (seams.length !== segs.length - 1) {
    throw new Error(`brand-overlay-42: ${seams.length} seams for ${segs.length} segments (need one per join)`);
  }
  for (const [i, seam] of seams.entries()) {
    if (seam.frame !== segs[i + 1].from) {
      throw new Error(`brand-overlay-42: seam ${i} is at frame ${seam.frame}, but segment ${i + 1} starts at ${segs[i + 1].from}`);
    }
  }
}

export function buildModel(props: BrandOverlayProps, W: number, H: number): ReelModel {
  const palette: Palette = {
    violet: props.palette?.violet || COLOR.violet,
    violetB: props.palette?.violet_b || COLOR.violetB,
    charcoal: props.palette?.charcoal || COLOR.charcoal,
    ink: props.palette?.ink || COLOR.ink,
  };
  const onCards = Boolean(props.logo?.on_cards);

  const segments: SegmentModel[] = props.segments.map((seg, i) => {
    const start = i > 0 ? props.seams[i - 1] : null;
    const end = i < props.seams.length ? props.seams[i] : null;
    const entrance = start === null || start.style === "handoff" ? seg.from : null;
    return {
      seg,
      i,
      geom: geometryFor(seg, W, H),
      start,
      end,
      entrance,
      exitAt: end && end.style === "handoff" ? end.frame : null,
      logoVisible: seg.kind === "scene" || onCards,
      logoEntrance: null,
      logoStays: false,
      cornerStaysIn: false,
      cornerStaysOut: false,
      pattern: PATTERN.cycle[i % PATTERN.cycle.length],
    };
  });

  // Logo: reveal it only where it was not already on screen in the same spot.
  for (const m of segments) {
    if (!m.logoVisible || m.entrance === null) continue;
    const prev = m.i > 0 ? segments[m.i - 1] : null;
    const kept = prev !== null && prev.logoVisible && sameRect(prev.geom.logo, m.geom.logo);
    m.logoEntrance = kept ? null : m.entrance + MOTION.logoInDelay;
  }
  for (const m of segments) {
    const next = m.i + 1 < segments.length ? segments[m.i + 1] : null;
    m.logoStays = next !== null && m.logoVisible && next.logoVisible && sameRect(next.geom.logo, m.geom.logo);
    if (next !== null && m.exitAt !== null && sameFull(m.geom, next.geom) && m.logoVisible === next.logoVisible) {
      m.cornerStaysOut = true;
      next.cornerStaysIn = true;
    }
  }

  // Icons: only on scene segments, and never across a cut (each is clipped
  // to the segment its first frame falls in; a sliver under 6 frames is dropped).
  const icons: BrandIconCue[] = [];
  for (const cue of [...(props.icons ?? [])].sort((a, b) => a.from - b.from)) {
    const from = Math.round(cue.from);
    const seg = segments.find((m) => from >= m.seg.from && from < m.seg.from + m.seg.frames);
    if (!seg || seg.seg.kind !== "scene") continue;
    const end = Math.min(from + Math.round(cue.frames), seg.seg.from + seg.seg.frames);
    const prev = icons[icons.length - 1];
    const start = prev ? Math.max(from, prev.from + prev.frames) : from;
    if (end - start >= 6) icons.push({ icon: cue.icon, from: start, frames: end - start });
  }

  const firstScene = props.segments.find((s) => s.kind === "scene");
  return {
    W,
    H,
    segments,
    seams: props.seams,
    typeStart: firstScene ? firstScene.from + MOTION.textDelay : null,
    palette,
    icons,
    edgePatterns: props.edge_patterns !== false,
  };
}

/** The icon on screen at `frame`, if any. */
export function iconAt(model: ReelModel, frame: number): BrandIconCue | null {
  for (const cue of model.icons) if (frame >= cue.from && frame < cue.from + cue.frames) return cue;
  return null;
}

/**
 * The whole-overlay glitch of a "glitch" seam on this frame (null outside
 * every glitch window). It rises to the cut and settles after it.
 */
export function seamGlitchAt(model: ReelModel, frame: number): { seam: BrandSeam; state: GlitchState } | null {
  for (const seam of model.seams) {
    if (seam.style !== "glitch") continue;
    const k = frame - seam.frame;
    const before = GLITCH.seamBefore.length;
    if (k >= -before && k < 0) return { seam, state: { amount: GLITCH.seamBefore[k + before], reveal: 1 } };
    if (k >= 0 && k < GLITCH.seamAfter.length) return { seam, state: { amount: GLITCH.seamAfter[k], reveal: 1 } };
  }
  return null;
}

export function segmentAt(model: ReelModel, frame: number): SegmentModel {
  const segs = model.segments;
  for (let i = segs.length - 1; i >= 0; i--) {
    if (frame >= segs[i].seg.from) return segs[i];
  }
  return segs[0];
}

export function wipesAt(model: ReelModel, frame: number): WipeState[] {
  const out: WipeState[] = [];
  model.seams.forEach((seam, i) => {
    if (seam.style !== "cover" && seam.style !== "short") return;
    const w = WIPE_WINDOWS[seam.style];
    const F = seam.frame;
    const k = frame - F;
    if (k < w.inFrom || k > w.outTo) return;
    const outgoing = model.segments[i];
    const incoming = model.segments[i + 1];
    if (k <= w.inTo) {
      const n = w.inTo - w.inFrom + 1;
      out.push({ seam, phase: "in", e: EASE.inOutCubic((k - w.inFrom + 0.5) / n), outgoing, incoming });
    } else if (k <= w.holdTo) {
      out.push({ seam, phase: "hold", e: 1, outgoing, incoming });
    } else {
      const n = w.outTo - w.holdTo;
      out.push({ seam, phase: "out", e: EASE.inOutCubic((k - w.holdTo - 0.5) / n), outgoing, incoming });
    }
  });
  return out;
}
