/**
 * Props contract of the `brand-overlay-42` composition. The backend
 * (brand_overlay.build_props) writes exactly this shape; keep the two in step.
 */
import type { TextFitReport, TextLayout } from "./textFit";

/** A rectangle in FRACTIONS of the frame (0..1). */
export type Box = { x: number; y: number; w: number; h: number };

export type SegmentKind = "intro" | "scene" | "outro";

export type BrandSegment = {
  kind: SegmentKind;
  /** Scene index (0 for bookends). */
  index: number;
  /** First frame of this segment in the reel timeline (inclusive). */
  from: number;
  /** Length in frames (30 fps). */
  frames: number;
  /** The picture area; outside it the source is black letterbox bars. */
  content: Box;
  /** "bars" when top AND bottom bar are each >= 0.10H on a portrait canvas. */
  layout: "bars" | "full";
};

/**
 * cover / short: a slab wipe hides the cut; handoff: r128's slab exit + entrance;
 * glitch: the whole frame glitches over F-6..F+5 (the backend glitches the
 * footage under it for those frames).
 */
export type SeamStyle = "cover" | "handoff" | "short" | "glitch";

/** One topic icon on screen: reel frames [from, from + frames), glitching in and out. */
export type BrandIconCue = { icon: string; from: number; frames: number };

export type BrandSeam = {
  /** == the incoming segment's `from` (the hard cut happens here). */
  frame: number;
  style: SeamStyle;
  from_kind: SegmentKind;
  to_kind: SegmentKind;
};

export type BrandOverlayProps = {
  width: number;
  height: number;
  fps: number;
  /** == sum(segments[].frames) */
  durationInFrames: number;
  /** Contiguous, ordered. */
  segments: BrandSegment[];
  /** One per join, ordered. */
  seams: BrandSeam[];
  /** null / empty strings = no text. */
  text: { heading: string; sub: string } | null;
  /** false: hide the logo while a card (intro/outro) is on screen. */
  logo: { on_cards: boolean };
  palette?: { violet?: string; violet_b?: string; charcoal?: string; ink?: string };
  /** Topic icons (ids from brand42/icons.ts), in reel order; absent/empty = none. */
  icons?: BrandIconCue[];
  /** Slide the segment's pattern over the picture's left edge too (default true). */
  edge_patterns?: boolean;
  /** Draws content boxes and safe zones; never in production. */
  debug?: boolean;
  /**
   * OUTPUTS of calculateMetadata, never inputs (whatever the caller sends is
   * overwritten): the text measured once, before any frame renders, with the
   * real fonts. `text_fit` is echoed to the backend by the render server
   * (job JSON / still response) so the arm can say "sub-line dropped: too
   * long"; `text_layouts` (keyed by segment position) is what the frames draw.
   */
  text_fit?: TextFitReport;
  text_layouts?: Record<string, TextLayout | null>;
};
