/**
 * 42 Abu Dhabi reel tokens for the `brand-overlay-42` composition.
 *
 * Adapted from the 2026-09-23 brand research (brand42.tokens.ts): every value
 * was MEASURED on 42's own published reels — r128 "Myth vs Fact / age barrier"
 * is the primary reference — not taken from the brand book, because the goal
 * is to look like the reels 42 actually posts. Where the book differs (violet
 * #730FFF, charcoal #414141) the reels win.
 *
 * Geometry is in px at the 720x1280 reference the reels were measured at. A
 * layout multiplies by `s = min(W, H) / 720` (1.5 at 1080x1920). For portrait
 * canvases that is exactly W/720; using the SHORT side keeps bead strings, type
 * and pattern tiles at the same physical size on a 16:9 canvas instead of
 * blowing them up 2.67x.
 */

// ---------------------------------------------------------------- colour ----
export const COLOR = {
  /** Side slab, headings, the cover wipe. 43-72% of violet px in all 4 reels. */
  violet: "#7F2BF2",
  /** Top band / corner slab that carries the logo; the wipe's leading sliver. */
  violetB: "#8D01FA",
  /** Bars plate (the reel background). */
  charcoal: "#303030",
  /** Beads, logo, body text. */
  ink: "#FFFFFF",
} as const;

export type Palette = {
  violet: string;
  violetB: string;
  charcoal: string;
  ink: string;
};

// ----------------------------------------------------------------- assets ----
/**
 * Paths under services/remotion/public/, for staticFile(). Everything the
 * composition draws ships with it — no Google Fonts, no CDN — so the brand
 * arm renders with no network at all. (The bead glyphs are SVG, see beads.ts.)
 */
export const ASSETS = {
  vg5000: "brand42/fonts/VG5000-Regular.woff2",
  jost: "brand42/fonts/Jost-Regular.woff2",
  logo: "brand42/logo_stacked_white.png",
} as const;

/** Stacked lockup PNG is 388x270. */
export const LOGO_ASPECT = 388 / 270;

// ----------------------------------------------------------------- fonts ----
export const VG5000_FAMILY = "VG5000";
/**
 * Jost 400 (stand-in for Futura PT Book, which is commercial), loaded from
 * public/ under a family name of our own so it can never resolve to a Jost
 * some other composition in the bundle registered from Google.
 */
export const JOST_FAMILY = "Jost42";

/** VG5000 Regular vertical metrics (em fractions). */
export const VG5000_METRICS = { capHeight: 0.7, descent: 0.2, leftBearing: 0.05 } as const;
/** Jost 400. */
export const JOST_METRICS = { capHeight: 0.7, descent: 0.25 } as const;

// ------------------------------------------------- geometry @ 720x1280 ------
export const REF = 720;

export const BARS = {
  /** TopBand: {x + y <= bandCut, y <= hb}; hb = min(bandHeight, topBar - gap). */
  bandCut: 457,
  bandHeight: 233,
  /** Clearance kept between any bar graphic and the picture. */
  gap: 8,
  /** SideSlab leg (the 45-degree top edge spans ws x ws). */
  slabLeg: 240,
  slabLegMin: 80,
  slabBottomMargin: 20,
  /**
   * r128's side slab is a parallelogram band, not a wedge: its lower 45-degree
   * edge x + y = 1784 runs 442 (in x + y) below its top edge x + y = 1342 and
   * leaves a charcoal corner under it, in a bottom bar 658 px tall at the
   * right edge. A bottom bar at least slabLeg + slabCutMinExtra tall gets
   * that lower cut, at the same share of its height (never more than 442).
   */
  slabSide: 442,
  slabSideBar: 658,
  slabCutMinExtra: 150,
  /** Text block in the bottom bar. */
  textLeft: 60,
  textTopGap: 20,
  textRightGap: 24,
  textBottomGap: 24,
  /**
   * Text block in the TOP bar, under the band, where r128 puts "Myth" — used
   * first whenever the bar leaves at least textTopMinSpace under the band.
   * Full width (textLeft on both sides), textTopGapBand under the band (or
   * the logo, whichever is lower) and textTopGapPicture above the picture;
   * the block is centred in that space.
   */
  textTopMinSpace: 150,
  textTopGapBand: 16,
  textTopGapPicture: 16,
} as const;

export const TYPE = {
  headingSize: 158, // VG5000, cap height ~111 px
  subSize: 47, // Jost 400 (Futura PT Book would be 52)
  subPitch: 55.7,
  /** Heading line pitch (em) when a long heading wraps to a second line. */
  headingPitch: 1.0,
  /** Heading baseline -> first sub-line cap top (scaled with the sub). */
  headingToSubGap: 48,
  headingMaxLines: 2,
  subMaxLines: 3,
  /**
   * Shrink floors. The heading may go down to 40% (a 22-character headline
   * in a 1:1 reel's bottom bar needs it), but only to 50% in the short top
   * bar — below that the bottom bar gives the heading more room. The sub-line
   * shrinks with the heading but never below 60% (28 px @720): it is the
   * sentence people read, so it stays legible on a phone.
   */
  headingMinScale: 0.4,
  headingMinScaleTop: 0.5,
  subMinScale: 0.6,
} as const;

export const LOGO = {
  // Bars layout: identical in all 4 reels, and it never moves.
  widthW: 0.208,
  rightMarginW: 0.0764,
  topMarginH: 0.067,
  // Full layout: sits on the corner slab (see FULL for its margins).
  fullWidthW: 0.17,
} as const;

export const FULL = {
  /** Minimum corner slab height (fraction of H) and the spec's cut foot (fraction of W). */
  slabHeightH: 0.11,
  slabFootW: 0.72,
  /** Bottom-left wedge legs, fraction of the SHORT side (0.20 W on a portrait canvas). */
  wedgeLeg: 0.2,
  /** Below this leg (px @720) the wedge is dropped rather than drawn as a sliver. */
  wedgeLegMin: 60,
  /** Face zone and caption band stay clear. */
  faceZone: { x0: 0.22, x1: 0.78, y0: 0.06, y1: 0.5 },
  captionBand: { y0: 0.7, y1: 0.88 },
  /**
   * Corner-slab logo, px @720: right margin, top and bottom margins inside
   * the slab, violet kept left of the lockup, and violet kept between the
   * lockup's ink and the 45-degree cut.
   */
  logoRight: 36,
  logoTop: 32,
  logoBottom: 24,
  logoLeftClear: 14,
  logoInkClear: 12,
} as const;

// ----------------------------------------------------------------- beads ----
export const BEADS = {
  /** Rows sit this far from the slab edge, in x+y units (r128: 451 / 480.5 vs cut 457). */
  insideXY: 6,
  outsideXY: 23.5,
  /** First glyph sits this far up the edge from its lower-left end (r128: 52 px). */
  startAlongEdge: 52,
  /** Glyph boxes: along x across. */
  glyph: {
    pill: [14, 7],
    lens: [7, 14],
    double: [28, 7],
  },
  /** A wipe's string keeps this far (x + y) from the band's own string. */
  wipeBandClear: 60,
} as const;

// -------------------------------------------------------------- patterns ----
/**
 * The owner's patterns (patterns/1-5.png, tiles in public/brand42/patterns)
 * replace r128's plain grid: ONE pattern at a time (the brand guide: never
 * combine patterns), a different one on every segment, sliding left to right
 * through the letterbox bars and over the picture's left edge.
 */
export const PATTERN = {
  /** Segment k shows cycle[k % 4]: consecutive segments always differ. */
  cycle: ["blueprint", "beads_field", "structure", "beads_row"] as const,
  /** In the bars, over charcoal: colour and opacity per pattern (line art strong, dense art quiet). */
  bars: {
    blueprint: { colour: "violet", opacity: 0.9 },
    beads_field: { colour: "white", opacity: 0.3 },
    structure: { colour: "white", opacity: 0.32 },
    beads_row: { colour: "white", opacity: 0.5 },
  },
  /** Over the footage: always violet (white vanishes on a light wall). */
  edge: { colour: "violet", opacity: 0.85 },
  /** Rightward drift, px @720 per frame (~54 px/s at 1080). */
  drift: 1.2,
  /** Entrance: the field is uncovered left to right over this many frames. */
  reveal: 16,
  /**
   * Left-edge field over the picture: this share of the picture's width,
   * solid for the first `solid` of it and fading to nothing at its right
   * edge (it stops short of the face zone, x >= 0.22 W), faded in over the
   * top `fadeTop` and out below `fadeFrom`..`fadeTo` of the picture's height
   * (a talking head's shoulders reach the frame edges there).
   */
  edgeW: 0.2,
  edgeSolid: 0.55,
  edgeFadeTop: 0.08,
  edgeFadeFrom: 0.55,
  edgeFadeTo: 0.8,
  /** "full" layout (no bars): the field's vertical extent, fractions of H. */
  fullEdgeY: [0.06, 0.66] as const,
} as const;

// ----------------------------------------------------------------- icons ----
/** Topic icon badge on the picture's left edge (inside the edge field). */
export const ICON = {
  /** Badge side as a share of W, and never more than this share of the picture height. */
  sizeW: 0.19,
  maxPictureH: 0.3,
  /** Badge left edge from the picture's left edge (share of W); centre at this share of the picture height. */
  leftW: 0.03,
  centreY: 0.42,
  /** Glitch in / out frames. */
  in: 8,
  out: 6,
} as const;

// ---------------------------------------------------------------- glitch ----
export const GLITCH = {
  /** Largest ordinary sideways slice jump and the channel split, px @720. */
  shift: 26,
  split: 5,
  /** Entrance / exit glitch lengths for layers (patterns, logo), frames. */
  in: 10,
  out: 8,
  /** Slabs glitch only over the first frames of their slide. */
  slabIn: 8,
  /**
   * "glitch" seam at the cut F: the whole overlay glitches over F-6..F+5 and
   * the footage under it is glitched by the backend (brand_overlay glitch
   * plates) over the same frames. Strength per frame, rising to the cut.
   */
  seamBefore: [0.35, 0.5, 0.65, 0.8, 0.95, 1] as const,
  seamAfter: [1, 0.85, 0.65, 0.45, 0.28, 0.12] as const,
  /** Idle twitch of the patterns and the icon: about every this many frames. */
  burstPeriod: 84,
} as const;

// ---------------------------------------------------------------- motion ----
/** Frames at 30 fps, measured on r128 (see research motion_spec.json). */
export const MOTION = {
  bandIn: 34, // TopBand / CornerSlab drop-in, out(exp)
  slabInDelay: 6,
  slabIn: 35, // SideSlab / wedge slide-in, out(exp)
  topBeadsDelay: 4,
  slabBeadsDelay: 10,
  beadPopSpeed: 17, // px @720 per frame along the string
  logoInDelay: 11,
  logoIn: 29, // out(poly(5))
  logoRise: 40, // px @720
  exit: 18, // handoff exit, in(exp), ends at the cut
  /**
   * The outgoing beads cut on the first frame the exit has covered this share
   * of its travel (F-7 on an 18-frame exit). r128 cuts its beads at f146,
   * when its in(exp) exit (t0 134.3, 18.5 f) is 7.8% along. Cutting them on
   * the exit's first frame (F-18) left ~12 frames with the beads gone and
   * nothing visibly moving yet, which read as a glitch.
   */
  beadCutProgress: 0.07,
  // Idle
  beadSlide: 0.3, // px @720 per frame along the string
  blinkCycle: 25,
  blinkOff: [8, 16] as const, // cycle frames (inclusive) where the blink set is off
  slabSinkH: 0.015, // SideSlab sinks this share of H over a segment, out(sin)
  // Text
  headingFramesPerChar: 3.33,
  subCharsPerFrame: 1.14,
  textDelay: 11, // after the first scene starts
} as const;
