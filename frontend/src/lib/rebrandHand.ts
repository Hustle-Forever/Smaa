// Rebrand studio — a pattern moved, sized or turned by hand (the in-house
// engine): the numbers, and the element edits.
//
// The recipe keeps a move by hand on the pattern's placement (the server's
// recipe.hand_of), always made from a pinned place (props.layout):
//
//   offset   [dx, dy] — the move, in frame fractions (±1 each way)
//   size     × the size it was placed at (0.5–3; absent: as placed)
//   rotate   degrees, clockwise (±180)
//   box      its visible box when it was first grabbed: the pivot is its
//            centre, kept so a later redraw can never make it drift
//
// The renderer draws p' = c + d + s·R(θ)·(p − c) (rebrand_pattern_ref.
// hand_matrix: turned and sized about the pivot c, then moved by d) — and a
// full-screen composition (Blueprint: ONE copy of its art) has its size
// raised until the frame stays inside it (clamp_cover): it always fills the
// frame. Both are mirrored here, number for number; the shared fixture
// (backend/tests/fixtures/rebrand_hand.json) holds the two together
// (scripts/check-rebrand-hand.mjs).
//
// A gesture on the stage (TransformBox) is a Transform against the box it
// started from: its move adds to the hand's, its size multiplies it, its
// turn adds to it (composeHand) — the pivot never moves.
//
// Plain TypeScript only (no enums, no path aliases, type-only imports): the
// check script runs it straight in node.

import type { HarnessBox, HarnessElement, HarnessPatternLayout } from "./rebrandHarnessApi";
import type { Transform } from "./rebrandTransform";

export interface Hand {
  dx: number;
  dy: number;
  scale: number;
  rotate: number;
  /** Frame fractions: the centre of the box it was first grabbed at. */
  pivot: [number, number];
}

export const NO_HAND: Hand = { dx: 0, dy: 0, scale: 1, rotate: 0, pivot: [0.5, 0.5] };
/** × its placed size (recipe.HAND_SCALE). */
export const HAND_SCALE: readonly [number, number] = [0.5, 3];
/** Frame widths / heights each way (recipe.HAND_OFFSET_MAX). */
export const HAND_OFFSET_MAX = 1;
/** Degrees each way (recipe.HAND_ROTATE_MAX). */
export const HAND_ROTATE_MAX = 180;
/** The full-screen clamp (rebrand_pattern_ref.COVER_*). */
export const COVER_SCALE_MAX = 8;
export const COVER_STEPS = 12;
export const COVER_EDGE = 1;

/** A 2x3 matrix on frame pixels. */
export type Matrix = [[number, number, number], [number, number, number]];
/** x0, y0, x1, y1 — frame pixels. */
export type Rect = readonly [number, number, number, number];

export function isIdentity(h: Pick<Hand, "dx" | "dy" | "scale" | "rotate">): boolean {
  return h.dx === 0 && h.dy === 0 && h.scale === 1 && h.rotate === 0;
}

/** Whether a pattern was moved, sized or turned by hand (recipe.is_hand). */
export function isHandPlaced(el: HarnessElement): boolean {
  if (el.kind !== "pattern") return false;
  const p = el.placement;
  const [ox, oy] = p.offset ?? [0, 0];
  return ox !== 0 || oy !== 0 || (p.rotate ?? 0) !== 0 || (p.size !== undefined && p.size !== null);
}

/** A pattern's hand move as the recipe says it (recipe.hand_of). */
export function handOf(el: HarnessElement): Hand {
  if (!isHandPlaced(el)) return NO_HAND;
  const p = el.placement;
  const b = p.box;
  const [ox, oy] = p.offset ?? [0, 0];
  return {
    dx: ox,
    dy: oy,
    scale: p.size ?? 1,
    rotate: p.rotate ?? 0,
    pivot: b ? [b.x + b.w / 2, b.y + b.h / 2] : [0.5, 0.5],
  };
}

/** The move as a 2x3 matrix on frame pixels, null for none
 *  (rebrand_pattern_ref.hand_matrix). */
export function handMatrix(h: Hand, width: number, height: number): Matrix | null {
  if (isIdentity(h)) return null;
  const px = h.pivot[0] * width;
  const py = h.pivot[1] * height;
  const th = (h.rotate * Math.PI) / 180;
  const c = Math.cos(th) * h.scale;
  const s = Math.sin(th) * h.scale;
  return [
    [c, -s, px + h.dx * width - (c * px - s * py)],
    [s, c, py + h.dy * height - (s * px + c * py)],
  ];
}

/** Frame points taken back through the move: where on the unmoved art each
 *  one comes from. */
export function unmapped(h: Hand, width: number, height: number, pts: readonly (readonly [number, number])[]): [number, number][] {
  const m = handMatrix(h, width, height);
  if (!m) return pts.map(([x, y]) => [x, y]);
  const [[a, b, tx], [c, d, ty]] = m;
  const det = a * d - b * c;
  return pts.map(([x, y]) => {
    const u = x - tx;
    const v = y - ty;
    return [(d * u - b * v) / det, (-c * u + a * v) / det];
  });
}

/** Whether a one-copy art whose outline is `rect` (unmoved) still covers
 *  the whole frame once moved by `h` (rebrand_pattern_ref.covers_frame). */
export function coversFrame(h: Hand, rect: Rect, width: number, height: number): boolean {
  const [x0, y0, x1, y1] = rect;
  const e = COVER_EDGE;
  return unmapped(h, width, height, [
    [0, 0],
    [width, 0],
    [width, height],
    [0, height],
  ]).every(([x, y]) => x >= x0 + e && x <= x1 - e && y >= y0 + e && y <= y1 - e);
}

/** `h` made to keep the frame inside a one-copy art (rebrand_pattern_ref.
 *  clamp_cover): as it is when it already does; else the least size that
 *  does (rounded up to 1/10000); else the largest size and as much of its
 *  move as still does. */
export function clampCover(h: Hand, rect: Rect, width: number, height: number): Hand {
  if (isIdentity(h) || coversFrame(h, rect, width, height)) return h;
  let lo = h.scale;
  let hi = Math.max(h.scale, COVER_SCALE_MAX);
  if (coversFrame({ ...h, scale: hi }, rect, width, height)) {
    for (let i = 0; i < COVER_STEPS; i++) {
      const mid = (lo + hi) / 2;
      if (coversFrame({ ...h, scale: mid }, rect, width, height)) hi = mid;
      else lo = mid;
    }
    const up = Math.ceil(hi * 1e4) / 1e4;
    return { ...h, scale: Math.min(up, Math.max(h.scale, COVER_SCALE_MAX)) };
  }
  let keepLo = 0;
  let keepHi = 1;
  for (let i = 0; i < COVER_STEPS; i++) {
    const mid = (keepLo + keepHi) / 2;
    if (coversFrame({ ...h, scale: hi, dx: h.dx * mid, dy: h.dy * mid }, rect, width, height)) keepLo = mid;
    else keepHi = mid;
  }
  return { ...h, scale: hi, dx: round(h.dx * keepLo, 4), dy: round(h.dy * keepLo, 4) };
}

function round(v: number, digits: number): number {
  const k = 10 ** digits;
  return Math.round(v * k) / k;
}

/** Degrees into (−180, 180]. */
export function wrapTurn(deg: number): number {
  let d = deg % 360;
  if (d > 180) d -= 360;
  if (d <= -180) d += 360;
  return d;
}

/** Within the recipe's limits: a move of at most a frame each way, a size
 *  of 0.5–3, a turn of at most half a turn each way. */
export function clampHand(h: Hand, scaleRange: readonly [number, number] = HAND_SCALE): Hand {
  const lim = (v: number, m: number) => Math.min(m, Math.max(-m, v));
  return {
    ...h,
    dx: lim(h.dx, HAND_OFFSET_MAX),
    dy: lim(h.dy, HAND_OFFSET_MAX),
    scale: Math.min(scaleRange[1], Math.max(scaleRange[0], h.scale)),
    rotate: wrapTurn(h.rotate),
  };
}

/** The hand after a gesture (a Transform against the box it started from,
 *  whose centre is the pivot plus the hand's move): the moves add, the
 *  sizes multiply, the turns add; the pivot stays. */
export function composeHand(h: Hand, t: Transform): Hand {
  return { dx: h.dx + t.dx, dy: h.dy + t.dy, scale: h.scale * t.scale, rotate: h.rotate + t.rotate, pivot: h.pivot };
}

/** Rounded as the recipe keeps it (a move to 1/10000, a size to 1/1000, a
 *  turn to a tenth of a degree): float noise never makes a new preview. */
export function roundHand(h: Hand): Hand {
  const z = (v: number) => (Object.is(v, -0) ? 0 : v);
  return {
    ...h,
    dx: z(round(h.dx, 4)),
    dy: z(round(h.dy, 4)),
    scale: z(round(h.scale, 3)),
    rotate: z(round(wrapTurn(h.rotate), 1)),
  };
}

/** The box a pattern is at on the picture (unturned; turned by h.rotate
 *  about its centre): its box before any move, moved and sized. */
export function handBox(base: HarnessBox, h: Pick<Hand, "dx" | "dy" | "scale">): HarnessBox {
  const cx = base.x + base.w / 2 + h.dx;
  const cy = base.y + base.h / 2 + h.dy;
  return { x: cx - (base.w * h.scale) / 2, y: cy - (base.h * h.scale) / 2, w: base.w * h.scale, h: base.h * h.scale };
}

/** Where the preview drew a pattern, as a first move by hand needs it: its
 *  place (pinned by the move) and its visible box before the move (the
 *  pivot). */
export interface HandDrawn {
  layout: HarnessPatternLayout;
  box: HarnessBox;
}

function roundBox(b: HarnessBox): HarnessBox {
  return { x: round(b.x, 4), y: round(b.y, 4), w: round(b.w, 4), h: round(b.h, 4) };
}

/** A placement with its hand fields at their defaults, in the shape it
 *  came in: one the server dumped (it holds `region`: every field, offset
 *  [0, 0], rotate 0, size and box null) keeps them at those; one the studio
 *  made ({anchor} alone: addPatterns) leaves them out. Set back, an element
 *  is the very element it was. */
function unhanded(p: HarnessElement["placement"]): HarnessElement["placement"] {
  const { offset: _o, rotate: _r, size: _s, box: _b, ...rest } = p;
  return "region" in p ? { ...rest, size: null, box: null, rotate: 0, offset: [0, 0] } : rest;
}

/** A pattern element moved by hand to `hand` (its move, size and turn;
 *  its pivot is its placement.box): pinned where the preview drew it and
 *  given that box, the first time; a field at its default is left as the
 *  element had it (so a hand back at none leaves no trace but the pin). */
export function withHand(el: HarnessElement, hand: Pick<Hand, "dx" | "dy" | "scale" | "rotate">, drawn: HandDrawn | null): HarnessElement {
  const h = roundHand({ ...clampHand({ ...NO_HAND, ...hand }), pivot: [0.5, 0.5] });
  const placement: HarnessElement["placement"] = unhanded(el.placement);
  const box = el.placement.box ?? (drawn ? roundBox(drawn.box) : null);
  if (box) placement.box = box;
  if (h.dx !== 0 || h.dy !== 0) placement.offset = [h.dx, h.dy];
  if (h.rotate !== 0) placement.rotate = h.rotate;
  if (h.scale !== 1) placement.size = h.scale;
  const props = el.props.layout || !drawn ? el.props : { ...el.props, layout: { ...drawn.layout } };
  return { ...el, placement, props };
}

/** "Back to automatic": no move, no box, no pin — the planner places it
 *  again (its shuffle is kept). */
export function withoutHand(el: HarnessElement): HarnessElement {
  return { ...el, placement: unhanded(el.placement), props: { ...el.props, layout: null } };
}

/** The hand fields only, cleared (a new anchor, pattern or spot is placed
 *  afresh: a move made from the old place means nothing there). */
export function clearHand(el: HarnessElement): HarnessElement {
  if (el.kind !== "pattern" || !(isHandPlaced(el) || el.placement.box)) return el;
  return { ...el, placement: unhanded(el.placement) };
}

/** What a move by hand adds to a layer's signature ("same as v2"): null
 *  when there is none. */
export function handSig(el: HarnessElement): unknown {
  if (!isHandPlaced(el)) return null;
  const p = el.placement;
  return [p.offset ?? [0, 0], p.size ?? null, p.rotate ?? 0, p.box ?? null];
}
