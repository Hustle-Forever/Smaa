/**
 * Layout geometry for one segment, in output px.
 *
 * Every 45-degree edge is stored as a constant of its line (x + y = C for a
 * "/" edge, x - y = D for a "\" edge), and every polygon vertex is computed
 * ON that line — never approximated — so the edges are exactly 45 degrees at
 * any canvas size.
 */
import type { BrandSegment } from "./types";
import { BARS, BEADS, FULL, ICON, LOGO, LOGO_ASPECT, PATTERN, REF, TYPE } from "./tokens";

export type Pt = { x: number; y: number };
export type Rect = { x: number; y: number; w: number; h: number };
/** Topic icon badge: top-left corner and side, output px. */
export type IconBox = { x: number; y: number; size: number };

const SQRT2 = Math.SQRT2;

/**
 * Where a bead string sits. The string's own frame: `origin` is the first
 * glyph's centre projected onto the slab edge, +u runs along the string, +v
 * across it. `deg` is the rotation that maps that frame onto the canvas:
 * -45 lays the string on a "/" edge (+u up-right, +v down-right), +45 on a "\"
 * edge (+u down-right, +v down-left). `rows` are the two rows' v offsets.
 * `span` (optional) is the part of the string, in u relative to `origin`,
 * where glyphs may show: a glyph is drawn only when it lies WHOLLY inside it.
 */
export type BeadLine = {
  origin: Pt;
  deg: -45 | 45;
  rows: readonly [number, number];
  span?: readonly [number, number];
};

/**
 * Where the optional heading + sub-line may go, in preference order. `top`
 * is the top bar under the band (r128's spot for "Myth"), full width, block
 * centred vertically; `bottom` is the bottom bar left of the side slab, block
 * top-aligned. `minScale` is the heading's shrink floor in that region.
 */
export type TextRegion = {
  placement: "top" | "bottom";
  left: number;
  top: number;
  right: number;
  bottom: number;
  minScale: number;
  valign: "top" | "centre";
};

export type BarsGeometry = {
  layout: "bars";
  W: number;
  H: number;
  s: number;
  content: Rect;
  contentBottom: number;
  /** TopBand: {x + y <= bandCut, y <= hb}. */
  hb: number;
  bandCut: number;
  /** SideSlab: {x + y >= slabC, x >= W - ws, x + y <= slabLowerC}; its top corner is (W, yTop). */
  ws: number;
  yTop: number;
  slabC: number;
  /** r128's lower 45-degree cut of the side slab; null = the slab runs to the frame bottom. */
  slabLowerC: number | null;
  /** The letterbox bars, where the pattern bed slides. */
  topBar: Rect;
  bottomBar: Rect;
  /** The picture's left edge, where the pattern slides over the footage (see PATTERN). */
  edge: Rect;
  icon: IconBox;
  logo: Rect;
  /** Candidate text blocks, best first (see TextRegion). */
  textRegions: TextRegion[];
  topBeads: BeadLine;
  slabBeads: BeadLine;
};

export type FullGeometry = {
  layout: "full";
  W: number;
  H: number;
  s: number;
  content: Rect;
  /**
   * CornerSlab: {x >= slabLeft, x + y >= cornerC, y <= hs}. slabLeft is the
   * face zone's right edge, so the slab can never reach into the face zone;
   * the 45-degree cut runs from (cornerC, 0) down-left to the vertical edge
   * (or to the slab bottom when the cut is that long).
   */
  hs: number;
  cornerC: number;
  slabLeft: number;
  /** Bottom-left wedge: {x - y <= leg - H}; 0 = no wedge (it would reach the caption band). */
  leg: number;
  logo: Rect;
  wedgeBeads: BeadLine | null;
  edge: Rect;
  icon: IconBox;
};

export type Geometry = BarsGeometry | FullGeometry;

/** Content box in whole output pixels (the backend snaps it; re-snap defensively). */
export const contentRect = (seg: BrandSegment, W: number, H: number): Rect => {
  const x0 = Math.round(seg.content.x * W);
  const y0 = Math.round(seg.content.y * H);
  const x1 = Math.round((seg.content.x + seg.content.w) * W);
  const y1 = Math.round((seg.content.y + seg.content.h) * H);
  return { x: x0, y: y0, w: Math.max(0, x1 - x0), h: Math.max(0, y1 - y0) };
};

/** Point on the "/" line x + y = C at along-coordinate u = (x - y) / sqrt2. */
export const onSlash = (C: number, u: number): Pt => ({ x: (C + SQRT2 * u) / 2, y: (C - SQRT2 * u) / 2 });
/** Point on the "\" line x - y = D at along-coordinate u = (x + y) / sqrt2. */
export const onBackslash = (D: number, u: number): Pt => ({ x: (SQRT2 * u + D) / 2, y: (SQRT2 * u - D) / 2 });
/** Along-coordinate u of the point with abscissa x on the "/" line x + y = C. */
export const uOnSlash = (C: number, x: number): number => (2 * x - C) / SQRT2;

/** Local v of a row that sits `xy` x+y-units from a "/" edge (positive = larger x+y). */
const vOf = (xy: number) => xy / SQRT2;

/** Span of a string along u: first glyph at 0, last at STRING_SPAN (see beads.ts). */
export const STRING_SPAN = 246;
/** Half the longest glyph along u (the double pill). */
export const STRING_PAD = 14;

/**
 * The stacked lockup's ink comes no nearer a "/" line approaching from its
 * top-left than this share of the lockup's width, in x + y from the box's
 * top-left corner (the "4"'s diagonal; measured on the PNG).
 */
const LOGO_INK_XY = 0.242;

export function geometryFor(seg: BrandSegment, W: number, H: number): Geometry {
  const s = Math.min(W, H) / REF;
  const content = contentRect(seg, W, H);
  if (seg.layout === "bars") return barsGeometry(W, H, s, content);
  return fullGeometry(W, H, s, content);
}

function barsGeometry(W: number, H: number, s: number, content: Rect): BarsGeometry {
  const topBar = content.y;
  const contentBottom = content.y + content.h;

  // TopBand: a band whose right edge is the reel's 45-degree cut, shortened when
  // the top bar is too short to keep 8 px of plate under it.
  const bandCut = BARS.bandCut * s;
  const hb = Math.max(0, Math.min(BARS.bandHeight * s, topBar - BARS.gap * s));

  // SideSlab: right-anchored in the bottom bar.
  const yTop = contentBottom + BARS.gap * s;
  let ws = BARS.slabLeg * s;
  if (H - BARS.slabBottomMargin * s - yTop < ws) {
    ws = Math.max(BARS.slabLegMin * s, H - BARS.slabBottomMargin * s - yTop);
  }
  const slabC = W + yTop;
  // r128's lower cut, when the bar is tall enough to show the charcoal corner
  // under it (otherwise the slab is the plain wedge down to the frame bottom).
  const barH = H - yTop;
  const slabLowerC =
    barH >= ws + BARS.slabCutMinExtra * s
      ? slabC + Math.min(BARS.slabSide * s, (barH * BARS.slabSide) / BARS.slabSideBar)
      : null;

  // The bars themselves: the pattern bed fills them edge to edge, UNDER the
  // band, the slab, the logo and the text.
  const topBarRect = { x: 0, y: 0, w: W, h: Math.max(0, topBar) };
  const bottomBarRect = { x: 0, y: contentBottom, w: W, h: Math.max(0, H - contentBottom) };

  // Logo: the reels' fixed spot; scaled down if it would reach the picture.
  const nominalW = LOGO.widthW * W;
  let lw = nominalW;
  let lh = lw / LOGO_ASPECT;
  let ly = LOGO.topMarginH * H;
  const limit = topBar - BARS.gap * s;
  if (ly + lh > limit) {
    lh = Math.max(0, limit - ly);
    lw = lh * LOGO_ASPECT;
  }
  if (lw < 0.6 * nominalW) {
    // Bar too short for the reel's top margin as well: centre the lockup in
    // what there is rather than shrinking it to a speck.
    lh = Math.min(nominalW / LOGO_ASPECT, Math.max(0, limit) * 0.8);
    lw = lh * LOGO_ASPECT;
    ly = (limit - lh) / 2;
  }
  const logo = { x: W - LOGO.rightMarginW * W - lw, y: ly, w: lw, h: lh };

  // Text: under the band in the top bar when there is room (r128's layout —
  // it leaves the bottom bar to the grid and the slab), else in the bottom
  // bar left of the slab.
  const textRegions: TextRegion[] = [];
  const underTop = Math.max(hb, logo.h > 0 ? logo.y + logo.h : 0);
  if (topBar - underTop >= BARS.textTopMinSpace * s) {
    textRegions.push({
      placement: "top",
      left: BARS.textLeft * s,
      top: underTop + BARS.textTopGapBand * s,
      right: W - BARS.textLeft * s,
      bottom: topBar - BARS.textTopGapPicture * s,
      minScale: TYPE.headingMinScaleTop,
      valign: "centre",
    });
  }
  textRegions.push({
    placement: "bottom",
    left: BARS.textLeft * s,
    top: contentBottom + BARS.textTopGap * s,
    right: W - ws - BARS.textRightGap * s,
    bottom: H - BARS.textBottomGap * s,
    minScale: TYPE.headingMinScale,
    valign: "top",
  });

  // Top string: along the band's cut, one row 6 px inside the violet, one
  // 23.5 px outside, starting 52 px up from the cut's lower-left end (r128).
  const bandLL = (bandCut - 2 * hb) / SQRT2;
  const topBeads: BeadLine = {
    origin: onSlash(bandCut, bandLL + BEADS.startAlongEdge * s),
    deg: -45,
    rows: [vOf(-BEADS.insideXY * s), vOf(BEADS.outsideXY * s)],
  };

  // Slab string: straddles the slab's 45-degree top edge the same way (outside
  // row above the edge). Its outside row must stay in the bar: every glyph of
  // it keeps >= 10 px (@720) of plate between it and the picture.
  const slabLL = (W - ws - (yTop + ws)) / SQRT2;
  const rowA = -BEADS.outsideXY * s;
  const uMaxRowA = (slabC + rowA - 2 * (contentBottom + 10 * s)) / SQRT2;
  const slabStart = Math.min(slabLL + BEADS.startAlongEdge * s, uMaxRowA - (STRING_SPAN + STRING_PAD / 2) * s);
  const slabBeads: BeadLine = {
    origin: onSlash(slabC, slabStart),
    deg: -45,
    rows: [vOf(rowA), vOf(BEADS.insideXY * s)],
  };

  return {
    layout: "bars",
    W,
    H,
    s,
    content,
    contentBottom,
    hb,
    bandCut,
    ws,
    yTop,
    slabC,
    slabLowerC,
    topBar: topBarRect,
    bottomBar: bottomBarRect,
    edge: edgeRect(W, content),
    icon: iconBox(W, content),
    logo,
    textRegions,
    topBeads,
    slabBeads,
  };
}

function fullGeometry(W: number, H: number, s: number, content: Rect): FullGeometry {
  // Corner slab. Its left edge is the face zone's right edge (plus a pixel),
  // so no part of it — at any height — can reach into the face zone. (The
  // spec's triangle-ish slab with its foot at 0.72 W covered the zone's
  // top-right corner on 9:16 and a whole corner of it on 16:9.)
  const slabLeft = Math.ceil(FULL.faceZone.x1 * W) + 1;

  // Logo: the spec's 0.17 W, never wider than the reels' own lockup (150 px
  // @720, which matters on 16:9) nor than the slab minus its margins.
  const mR = FULL.logoRight * s;
  const lw = Math.max(
    0,
    Math.min(LOGO.fullWidthW * W, LOGO.widthW * REF * s, W - mR - slabLeft - FULL.logoLeftClear * s),
  );
  const lh = lw / LOGO_ASPECT;
  // The slab is at least 0.11 H tall and always tall enough for the lockup
  // plus its margins; it stops above the caption band whatever the aspect.
  let hs = Math.min(
    Math.max(FULL.slabHeightH * H, (FULL.logoTop + FULL.logoBottom) * s + lh),
    FULL.captionBand.y0 * H - 8 * s,
  );
  const logo = { x: W - mR - lw, y: Math.max(0, hs - FULL.logoBottom * s - lh), w: lw, h: lh };

  // The 45-degree cut: as long as it can be while the lockup's ink keeps
  // logoInkClear of violet in front of it; it never runs past the slab's
  // bottom-left corner (the cut then IS the slab's left side).
  const inkC = logo.x + logo.y + LOGO_INK_XY * lw - FULL.logoInkClear * s;
  const cornerC = Math.max(slabLeft, Math.min(slabLeft + hs, inkC));
  // A cut that stops just short of the bottom leaves a stub of vertical edge
  // that reads as a mistake: lift the slab bottom to the cut instead, as
  // long as the lockup keeps half its bottom margin.
  const stub = hs - (cornerC - slabLeft);
  if (stub > 0 && stub < 24 * s && hs - stub - (logo.y + lh) >= (FULL.logoBottom / 2) * s) hs -= stub;

  // Wedge: legs 0.20 of the short side, but its top (y = H - leg) stays below
  // the caption band — on 16:9 that is 0.12 H, not 0.20 min(W, H). Too small
  // to read as a slab: none at all.
  let leg = Math.min(FULL.wedgeLeg * Math.min(W, H), (1 - FULL.captionBand.y1) * H - 4 * s);
  if (leg < FULL.wedgeLegMin * s) leg = 0;

  // Wedge string: both rows just INSIDE the violet (+v is the violet side on
  // this rotation), centred on the "\" edge, and only glyphs that fit wholly
  // on the on-canvas part of the edge (no half-glyphs at the frame edges).
  let wedgeBeads: BeadLine | null = null;
  if (leg > 0) {
    const pitchXY = BEADS.outsideXY + BEADS.insideXY; // the reel's 29.5 x+y row pitch
    const D = leg - H;
    const wedgeUL = (H - leg) / SQRT2;
    const edgeLen = leg * SQRT2;
    const wedgeStart = wedgeUL + (edgeLen - STRING_SPAN * s) / 2;
    wedgeBeads = {
      origin: onBackslash(D, wedgeStart),
      deg: 45,
      rows: [vOf(BEADS.insideXY * s), vOf((BEADS.insideXY + pitchXY) * s)],
      span: [wedgeUL - wedgeStart, wedgeUL + edgeLen - wedgeStart],
    };
  }

  // The full layout has no corner string: the reel's straddle would put it
  // across the lockup or over the picture (clipped at the top edge it read as
  // debris), and inside the violet it would run through the logo.
  // Left-edge field + icon: the picture is the whole frame here; keep them in
  // the band between the top corner slab's height and the caption band.
  const [ey0, ey1] = PATTERN.fullEdgeY;
  const band = { x: 0, y: ey0 * H, w: W, h: (ey1 - ey0) * H };
  return {
    layout: "full", W, H, s, content, hs, cornerC, slabLeft, leg, logo, wedgeBeads,
    edge: edgeRect(W, band),
    icon: iconBox(W, band),
  };
}


/** The picture's left-edge strip (PATTERN.edgeW of its width, never past the face zone). */
function edgeRect(W: number, picture: Rect): Rect {
  const w = Math.min(PATTERN.edgeW * picture.w, FULL.faceZone.x0 * W - picture.x);
  return { x: picture.x, y: picture.y, w: Math.max(0, w), h: picture.h };
}

/** Topic icon badge: on the left edge, vertically at ICON.centreY of the picture. */
function iconBox(W: number, picture: Rect): IconBox {
  const size = Math.min(ICON.sizeW * W, ICON.maxPictureH * picture.h);
  return { x: picture.x + ICON.leftW * W, y: picture.y + ICON.centreY * picture.h - size / 2, size };
}

/** Two layouts put the logo in the same place (so a hand-off need not re-reveal it). */
export const sameRect = (a: Rect, b: Rect): boolean =>
  Math.abs(a.x - b.x) < 0.5 && Math.abs(a.y - b.y) < 0.5 && Math.abs(a.w - b.w) < 0.5 && Math.abs(a.h - b.h) < 0.5;

/** Two "full" layouts are drawn identically (full geometry ignores the content box). */
export const sameFull = (a: Geometry, b: Geometry): boolean =>
  a.layout === "full" && b.layout === "full" && a.W === b.W && a.H === b.H;
