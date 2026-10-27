// Rebrand studio — the stage handles' numbers (TransformBox): a box on the
// picture, in frame fractions, and what a gesture does to it.
//
// A gesture always gives a Transform against the box it started from:
//
//   dx, dy   the move of its centre (frame fractions)
//   scale    × its size, about its centre (a corner drag is a scale plus
//            the move that keeps the opposite corner still) — uniform, so
//            nothing is ever stretched
//   rotate   degrees, clockwise (a pattern's turn; an icon never turns)
//
// An icon turns the result into the box it is kept in (iconBox: inside the
// frame, to the ten-thousandth) — the recipe's props.tweaks[glyph].box; a
// chip's height is what counts, the planner works its width out from its
// words. A pattern (by hand) reads the Transform itself.
//
// Plain TypeScript only (no enums, no path aliases, type-only imports).

export interface Box {
  x: number;
  y: number;
  w: number;
  h: number;
}

export interface Transform {
  dx: number;
  dy: number;
  scale: number;
  rotate: number;
}

export const IDENTITY: Transform = { dx: 0, dy: 0, scale: 1, rotate: 0 };

const EPS = 1e-9;

/** `box` under `t` (the turn is not a box's: the caller draws it). */
export function applied(box: Box, t: Transform): Box {
  const cx = box.x + box.w / 2 + t.dx;
  const cy = box.y + box.h / 2 + t.dy;
  const w = box.w * t.scale;
  const h = box.h * t.scale;
  return { x: cx - w / 2, y: cy - h / 2, w, h };
}

/** The Transform that takes `start` to `box` (same aspect; the turn kept). */
export function between(start: Box, box: Box, rotate = 0): Transform {
  return {
    dx: box.x + box.w / 2 - (start.x + start.w / 2),
    dy: box.y + box.h / 2 - (start.y + start.h / 2),
    scale: start.w > 0 ? box.w / start.w : 1,
    rotate,
  };
}

/** `box` moved inside the frame (shrunk first if it is larger than it). */
export function insideFrame(box: Box): Box {
  const w = Math.min(box.w, 1);
  const h = Math.min(box.h, 1);
  return { x: Math.min(Math.max(0, box.x), 1 - w), y: Math.min(Math.max(0, box.y), 1 - h), w, h };
}

/** The largest scale about the point (ax, ay) that keeps `box` inside the
 *  frame. */
export function maxScaleInside(box: Box, ax: number, ay: number): number {
  let s = Infinity;
  const lim = (edge: number, anchor: number, bound: number) => {
    const d = edge - anchor;
    if (Math.abs(d) > EPS) {
      const k = (bound - anchor) / d;
      if (k > 0) s = Math.min(s, k);
    }
  };
  lim(box.x, ax, 0);
  lim(box.x + box.w, ax, 1);
  lim(box.y, ay, 0);
  lim(box.y + box.h, ay, 1);
  return s;
}

/** `box` scaled by `s` about the point (ax, ay). */
export function scaledAbout(box: Box, s: number, ax: number, ay: number): Box {
  return { x: ax + (box.x - ax) * s, y: ay + (box.y - ay) * s, w: box.w * s, h: box.h * s };
}

/** The pull of the nearest target on any of `edges` within `radius` (all in
 *  one axis' fractions): the shift that lands it, and the target — or null. */
export function snapAxis(
  edges: readonly number[],
  targets: readonly number[],
  radius: number,
): { shift: number; target: number } | null {
  let best: { shift: number; target: number } | null = null;
  for (const e of edges) {
    for (const t of targets) {
      const d = t - e;
      if (Math.abs(d) <= radius + EPS && (!best || Math.abs(d) < Math.abs(best.shift) - EPS)) best = { shift: d, target: t };
    }
  }
  return best;
}

export function roundBox(box: Box, digits = 4): Box {
  const k = 10 ** digits;
  const r = (v: number) => Math.round(v * k) / k;
  return { x: r(box.x), y: r(box.y), w: r(box.w), h: r(box.h) };
}

/** Where an icon is kept after a gesture: `start` under `t`, inside the
 *  frame, to the ten-thousandth. */
export function iconBox(start: Box, t: Transform): Box {
  return roundBox(insideFrame(applied(start, { ...t, rotate: 0 })));
}

// --- a box that turns (a pattern moved by hand) ------------------------------

const DEG = Math.PI / 180;

/** The corners of `box` turned `rotate` degrees (clockwise) about its
 *  centre, in frame fractions — top-left, top-right, bottom-right,
 *  bottom-left. The turn is made in the frame's pixels (`frame`), as the
 *  picture turns it. */
export function turnedCorners(box: Box, rotate: number, frame: { w: number; h: number }): [number, number][] {
  const cx = (box.x + box.w / 2) * frame.w;
  const cy = (box.y + box.h / 2) * frame.h;
  const hw = (box.w * frame.w) / 2;
  const hh = (box.h * frame.h) / 2;
  const c = Math.cos(rotate * DEG);
  const s = Math.sin(rotate * DEG);
  return ([[-hw, -hh], [hw, -hh], [hw, hh], [-hw, hh]] as const).map(([x, y]) => [
    (cx + x * c - y * s) / frame.w,
    (cy + x * s + y * c) / frame.h,
  ]);
}

// --- the turn button (a pattern's) ------------------------------------------------
//
// A round button just outside the turned box, in the canvas' own pixels. It
// is tried below the box's middle (as Canva has it), then above, right and
// left — each slid along the box's side to clear what is in the way — and
// must be wholly on the canvas (8 px in), clear of every avoid zone (the
// stage's chips and buttons: its 44 px hit area must not touch one) and
// 44 px or more from the pivot (so a turn has something to hold). Its angle
// pill (while it turns) sits on its free side: below it when the button is
// in the top 40 px, else above, else beside it. When no side works (a box
// larger than the canvas, 3×, a tiny canvas) it takes the nearest point on
// the canvas that keeps the rules; when none keeps them all, the one that
// keeps the most (`level`). A full-screen pattern keeps it bottom-right,
// 12 px in (bottom-left holds the "enters" chip).

/** A rectangle in the canvas' own pixels (CSS px from its top-left). */
export interface Rect {
  x: number;
  y: number;
  w: number;
  h: number;
}

/** The button: a disc in a larger hit area, `gap` outside the turned box,
 *  `inset` inside the canvas (`panInset` for a full-screen pattern), and at
 *  least `pivot` from the box's centre (px). */
export const TURN_BUTTON = { disc: 32, hit: 44, gap: 12, inset: 8, pivot: 44, panInset: 12 } as const;
/** The angle pill ("45°"): its size, its gap from the button, and the band
 *  at the top where the button puts it below itself (px). */
export const TURN_PILL = { w: 48, h: 22, gap: 8, top: 40 } as const;

export type TurnSide = "below" | "above" | "right" | "left" | "corner" | "nearest";

export interface TurnSpot {
  /** The button's centre (canvas px). */
  x: number;
  y: number;
  side: TurnSide;
  /** The angle pill's centre (canvas px). */
  pill: { x: number; y: number };
  /** Which rules it keeps: 0 all of them; 1 all but the pill clear of the
   *  zones; 2 also nearer the pivot than `pivot`; 3 only on the canvas. */
  level: number;
}

const SPOT_EPS = 1e-6;

function overlaps(a: Rect, b: Rect): boolean {
  return (
    a.x < b.x + b.w - SPOT_EPS && b.x < a.x + a.w - SPOT_EPS && a.y < b.y + b.h - SPOT_EPS && b.y < a.y + a.h - SPOT_EPS
  );
}

/** Where the turn button of `box` (frame fractions, turned `rot` degrees in
 *  the frame's pixels `frame`) goes on a canvas `canvas` px big, clear of
 *  `avoid` (canvas px). `pan`: a full-screen pattern. */
export function turnButtonSpot(
  box: Box,
  rot: number,
  frame: { w: number; h: number },
  canvas: { w: number; h: number },
  avoid: readonly Rect[] = [],
  { pan = false }: { pan?: boolean } = {},
): TurnSpot {
  const W = Math.max(0, canvas.w);
  const H = Math.max(0, canvas.h);
  const r = TURN_BUTTON.disc / 2;
  const half = TURN_BUTTON.hit / 2;
  const lo = TURN_BUTTON.inset + r;
  // Where its centre may go (a canvas too small for it: the middle).
  const x0 = Math.min(lo, W / 2);
  const x1 = Math.max(W - lo, W / 2);
  const y0 = Math.min(lo, H / 2);
  const y1 = Math.max(H - lo, H / 2);
  const cx = (x: number) => Math.min(Math.max(x, x0), x1);
  const cy = (y: number) => Math.min(Math.max(y, y0), y1);
  const px = (box.x + box.w / 2) * W;
  const py = (box.y + box.h / 2) * H;
  const zones = avoid.filter((z) => z.w > 0 && z.h > 0);
  const P = TURN_PILL;
  const pillOff = r + P.gap;

  /** The pill's offsets from the button, in the order they are tried. */
  const pillOrder = (y: number): [number, number][] => {
    const below: [number, number] = [0, pillOff + P.h / 2];
    const above: [number, number] = [0, -(pillOff + P.h / 2)];
    const side = pillOff + P.w / 2;
    return [...(y - r < P.top ? [below, above] : [above, below]), [side, 0], [-side, 0]];
  };
  const pillRect = (x: number, y: number): Rect => ({ x: x - P.w / 2, y: y - P.h / 2, w: P.w, h: P.h });
  const pillFor = (x: number, y: number, strict: boolean): { x: number; y: number } | null => {
    for (const [ox, oy] of pillOrder(y)) {
      const p = pillRect(x + ox, y + oy);
      if (p.x < -SPOT_EPS || p.y < -SPOT_EPS || p.x + p.w > W + SPOT_EPS || p.y + p.h > H + SPOT_EPS) continue;
      if (strict && zones.some((z) => overlaps(p, z))) continue;
      return { x: x + ox, y: y + oy };
    }
    return null;
  };
  const ok = (x: number, y: number, level: number): boolean => {
    if (x < x0 - SPOT_EPS || x > x1 + SPOT_EPS || y < y0 - SPOT_EPS || y > y1 + SPOT_EPS) return false;
    if (level >= 3) return true;
    const hit = { x: x - half, y: y - half, w: 2 * half, h: 2 * half };
    if (zones.some((z) => overlaps(hit, z))) return false;
    if (level < 2 && Math.hypot(x - px, y - py) < TURN_BUTTON.pivot - SPOT_EPS) return false;
    return pillFor(x, y, level === 0) !== null;
  };
  const done = (x: number, y: number, side: TurnSide, level: number): TurnSpot => {
    const [ox, oy] = pillOrder(y)[0];
    const pill = pillFor(x, y, true) ??
      pillFor(x, y, false) ?? {
        x: Math.min(Math.max(x + ox, P.w / 2), Math.max(P.w / 2, W - P.w / 2)),
        y: Math.min(Math.max(y + oy, P.h / 2), Math.max(P.h / 2, H - P.h / 2)),
      };
    return { x, y, side, pill, level };
  };

  let target: [number, number];
  if (pan) {
    const inset = TURN_BUTTON.panInset + r;
    target = [cx(W - inset), cy(H - inset)];
    if (ok(target[0], target[1], 0)) return done(target[0], target[1], "corner", 0);
  } else {
    const pts = turnedCorners(box, rot, frame).map(([fx, fy]) => [fx * W, fy * H]);
    const L = Math.min(...pts.map((p) => p[0]));
    const R = Math.max(...pts.map((p) => p[0]));
    const T = Math.min(...pts.map((p) => p[1]));
    const B = Math.max(...pts.map((p) => p[1]));
    const off = TURN_BUTTON.gap + r;
    const piv = TURN_BUTTON.pivot;
    // Each side: the button's distance out (never nearer the pivot than
    // `pivot`), and the stretch of the box's side it may slide along.
    const sides: { side: TurnSide; fixed: number; vertical: boolean; from: number; to: number; pref: number }[] = [
      { side: "below", fixed: Math.max(B + off, py + piv), vertical: false, from: L, to: R, pref: px },
      { side: "above", fixed: Math.min(T - off, py - piv), vertical: false, from: L, to: R, pref: px },
      { side: "right", fixed: Math.max(R + off, px + piv), vertical: true, from: T, to: B, pref: py },
      { side: "left", fixed: Math.min(L - off, px - piv), vertical: true, from: T, to: B, pref: py },
    ];
    for (const s of sides) {
      const [f0, f1, a0, a1] = s.vertical ? [x0, x1, y0, y1] : [y0, y1, x0, x1];
      if (s.fixed < f0 - SPOT_EPS || s.fixed > f1 + SPOT_EPS) continue;
      const from = Math.min(Math.max(s.from, a0), a1);
      const to = Math.max(Math.min(s.to, a1), a0);
      const pref = Math.min(Math.max(s.pref, from), to);
      const at = (a: number): [number, number] => (s.vertical ? [s.fixed, a] : [a, s.fixed]);
      // Slid 1 px at a time, nearest first, along the side.
      for (let d = 0; pref - d >= from - SPOT_EPS || pref + d <= to + SPOT_EPS; d += 1) {
        for (const a of d === 0 ? [pref] : [pref - d, pref + d]) {
          if (a < from - SPOT_EPS || a > to + SPOT_EPS) continue;
          const [x, y] = at(a);
          if (ok(x, y, 0)) return done(x, y, s.side, 0);
        }
      }
    }
    target = [cx(px), cy(B + off)];
  }

  // Nowhere beside it: the nearest point that keeps the most rules.
  for (let level = 0; level <= 3; level++) {
    const best = nearestSpot(target, level, ok, {
      x0,
      x1,
      y0,
      y1,
      lines: spotLines(zones, half, level, pillOrder, W, H),
      pivot: level < 2 ? { x: px, y: py, r: TURN_BUTTON.pivot } : null,
    });
    if (best) return done(best[0], best[1], "nearest", level);
  }
  return done(target[0], target[1], "nearest", 3);
}

/** The lines where a rule starts to hold (a zone's edge moved out by the
 *  hit area; a pill's edge by its offset): where the nearest point that
 *  keeps the rules is found, with the canvas' own edges. */
function spotLines(
  zones: readonly Rect[],
  half: number,
  level: number,
  pillOrder: (y: number) => [number, number][],
  W: number,
  H: number,
): { xs: number[]; ys: number[] } {
  const xs: number[] = [];
  const ys: number[] = [];
  if (level >= 3) return { xs, ys };
  const P = TURN_PILL;
  // Every offset the pill can take (the order doesn't matter here).
  const offs = pillOrder(0);
  for (const z of zones) {
    xs.push(z.x - half, z.x + z.w + half);
    ys.push(z.y - half, z.y + z.h + half);
    if (level === 0) {
      for (const [ox, oy] of offs) {
        xs.push(z.x - P.w / 2 - ox, z.x + z.w + P.w / 2 - ox);
        ys.push(z.y - P.h / 2 - oy, z.y + z.h + P.h / 2 - oy);
      }
    }
  }
  for (const [ox, oy] of offs) {
    xs.push(P.w / 2 - ox, W - P.w / 2 - ox);
    ys.push(P.h / 2 - oy, H - P.h / 2 - oy);
  }
  return { xs, ys };
}

/** The point nearest `target` for which `ok(x, y, level)` holds, exactly:
 *  every rule's edge is a straight line (`lines`, the canvas' own) or the
 *  pivot's circle, so the nearest point is the target itself, its foot on a
 *  line or on the circle, or a corner where two of them cross. Tried
 *  nearest first, so the first that keeps the rules is the one. */
function nearestSpot(
  target: [number, number],
  level: number,
  ok: (x: number, y: number, level: number) => boolean,
  area: {
    x0: number;
    x1: number;
    y0: number;
    y1: number;
    lines: { xs: number[]; ys: number[] };
    pivot: { x: number; y: number; r: number } | null;
  },
): [number, number] | null {
  const { x0, x1, y0, y1, pivot } = area;
  const [tx, ty] = target;
  const uniq = (vs: number[], a: number, b: number) =>
    [...new Set(vs.filter((v) => v >= a - SPOT_EPS && v <= b + SPOT_EPS).map((v) => Math.min(Math.max(v, a), b)))];
  // The lines, nearest the target first (its own x and y among them: the
  // feet of the perpendiculars).
  const xs = uniq([x0, x1, tx, ...area.lines.xs], x0, x1).sort((a, b) => Math.abs(a - tx) - Math.abs(b - tx));
  const ys = uniq([y0, y1, ty, ...area.lines.ys], y0, y1).sort((a, b) => Math.abs(a - ty) - Math.abs(b - ty));
  let best: [number, number] | null = null;
  let bestD = Infinity;
  const tryAt = (x: number, y: number) => {
    const d = (x - tx) ** 2 + (y - ty) ** 2;
    if (d < bestD - SPOT_EPS && ok(x, y, level)) {
      best = [x, y];
      bestD = d;
    }
  };
  // Where two lines cross: pruned by distance (both lists are sorted).
  for (const x of xs) {
    const dx = (x - tx) ** 2;
    if (dx >= bestD) break;
    for (const y of ys) {
      if (dx + (y - ty) ** 2 >= bestD) break;
      tryAt(x, y);
    }
  }
  // On the pivot's circle: the target's foot, and where a line crosses it.
  if (pivot) {
    const { x: cx, y: cy, r } = pivot;
    const len = Math.hypot(tx - cx, ty - cy);
    if (len > SPOT_EPS) tryAt(cx + ((tx - cx) / len) * r, cy + ((ty - cy) / len) * r);
    for (const x of xs) {
      const d = r * r - (x - cx) ** 2;
      if (d >= 0) {
        tryAt(x, cy - Math.sqrt(d));
        tryAt(x, cy + Math.sqrt(d));
      }
    }
    for (const y of ys) {
      const d = r * r - (y - cy) ** 2;
      if (d >= 0) {
        tryAt(cx - Math.sqrt(d), y);
        tryAt(cx + Math.sqrt(d), y);
      }
    }
  }
  return best;
}

/** `box` (turned `rotate`) sized uniformly by dragging its corner `corner`
 *  (0 top-left … 3 bottom-left) to (px, py) — frame fractions — about the
 *  opposite corner, or about its centre: its scale (× the box, within
 *  [lo, hi]) and the new box (unturned; it keeps the same turn). With no
 *  turn it is scaledAbout the opposite corner. */
export function sizedByCorner(
  box: Box,
  rotate: number,
  corner: number,
  px: number,
  py: number,
  frame: { w: number; h: number },
  aboutCentre: boolean,
  lo: number,
  hi: number,
): { box: Box; scale: number } {
  const pts = turnedCorners(box, rotate, frame);
  const cx = box.x + box.w / 2;
  const cy = box.y + box.h / 2;
  const [ax, ay] = aboutCentre ? [cx, cy] : pts[(corner + 2) % 4];
  const [kx, ky] = pts[corner];
  const vx = (kx - ax) * frame.w;
  const vy = (ky - ay) * frame.h;
  const wx = (px - ax) * frame.w;
  const wy = (py - ay) * frame.h;
  const len = vx * vx + vy * vy;
  let s = len > 0 ? (wx * vx + wy * vy) / len : 1;
  s = Math.max(1e-3, Math.min(Math.max(s, lo), hi));
  const ncx = ax + (cx - ax) * s;
  const ncy = ay + (cy - ay) * s;
  const w = box.w * s;
  const h = box.h * s;
  return { box: { x: ncx - w / 2, y: ncy - h / 2, w, h }, scale: s };
}

/** The angle (degrees, clockwise from straight up) of the point (px, py)
 *  about the centre of `box` — frame fractions, measured in the frame's
 *  pixels: where the turn button dragged there points. */
export function angleAbout(box: Box, px: number, py: number, frame: { w: number; h: number }): number {
  const dx = (px - (box.x + box.w / 2)) * frame.w;
  const dy = (py - (box.y + box.h / 2)) * frame.h;
  return Math.atan2(dx, -dy) / DEG;
}

/** Within this of a multiple of 45°, a dragged turn clicks onto it. */
export const TURN_MAGNET = 4;

/** A turn as it lands: with `step` (Shift) on a multiple of 15°; else, with
 *  `magnet` (a drag), pulled onto a multiple of 45° within TURN_MAGNET;
 *  else as it is ([ / ] and the arrows turn exactly 1°). Always in
 *  (−180, 180]. */
export function snapTurn(deg: number, { step = false, magnet = false }: { step?: boolean; magnet?: boolean } = {}): number {
  const wrap = (d: number) => {
    let v = d % 360;
    if (v > 180) v -= 360;
    if (v <= -180) v += 360;
    return v;
  };
  let d = wrap(deg);
  if (step) d = Math.round(d / 15) * 15;
  else if (magnet) {
    const q = Math.round(d / 45) * 45;
    if (Math.abs(d - q) <= TURN_MAGNET) d = q;
  }
  return wrap(d);
}

/** A turn that sits on a multiple of 45° (the angle pill goes purple). */
export function onTurnStop(deg: number): boolean {
  return Math.abs(deg / 45 - Math.round(deg / 45)) < 1e-9;
}

/** The least of a turned pattern that must really show (see partlyInside):
 *  under the 2.25% an unturned one keeps in a corner (15% × 15%), so only an
 *  empty-cornered shape is ever walked back. */
const TURNED_SHOWN = 0.02;

/** `box` moved (never sized) so at least `keep` of its width and of its
 *  height stay on the frame: a pattern may go almost anywhere, never all
 *  the way off. A turned pattern (`turn`) is held by what is seen: its
 *  turned outline, so a pattern turned 90° can't slip off by the width it
 *  no longer has; and because a turned shape leaves the outline's corners
 *  empty (a diamond at a corner shows nothing), it is then walked back
 *  toward the frame until a real piece of it — TURNED_SHOWN of it, or of
 *  the frame when it is bigger — is on the frame. Unturned, it is exactly
 *  the rule it always was. */
export function partlyInside(
  box: Box,
  keep = 0.15,
  turn?: { rotate: number; frame: { w: number; h: number } },
): Box {
  if (!turn || turn.rotate % 360 === 0 || turn.frame.w <= 0 || turn.frame.h <= 0) {
    const kw = box.w * keep;
    const kh = box.h * keep;
    return {
      ...box,
      x: Math.min(Math.max(box.x, kw - box.w), 1 - kw),
      y: Math.min(Math.max(box.y, kh - box.h), 1 - kh),
    };
  }
  const { rotate, frame } = turn;
  const cs = turnedCorners(box, rotate, frame);
  const xs = cs.map((c) => c[0]);
  const ys = cs.map((c) => c[1]);
  const bx = Math.min(...xs);
  const by = Math.min(...ys);
  const bw = Math.max(...xs) - bx;
  const bh = Math.max(...ys) - by;
  const kw = bw * keep;
  const kh = bh * keep;
  // The shift, not the new spot, so a box that needs none keeps its bits.
  const dx = Math.min(Math.max(bx, kw - bw), 1 - kw) - bx;
  const dy = Math.min(Math.max(by, kh - bh), 1 - kh) - by;
  const moved: Box = { ...box, x: dx ? box.x + dx : box.x, y: dy ? box.y + dy : box.y };
  // At a quarter turn the shape fills its outline: the outline rule is exact.
  if (rotate % 90 === 0 || shownShare(moved, rotate, frame) >= TURNED_SHOWN) return moved;
  // Home: the same box with its centre pulled 10% inside the frame, where a
  // piece of it always shows; keep the farthest point on the way that shows
  // enough.
  const cx = moved.x + moved.w / 2;
  const cy = moved.y + moved.h / 2;
  const hx = moved.x + Math.min(Math.max(cx, 0.1), 0.9) - cx;
  const hy = moved.y + Math.min(Math.max(cy, 0.1), 0.9) - cy;
  const at = (t: number): Box => ({ ...moved, x: hx + (moved.x - hx) * t, y: hy + (moved.y - hy) * t });
  let lo = 0;
  let hi = 1;
  for (let i = 0; i < 20; i++) {
    const mid = (lo + hi) / 2;
    if (shownShare(at(mid), rotate, frame) >= TURNED_SHOWN) lo = mid;
    else hi = mid;
  }
  return at(lo);
}

/** How much of the turned `box` is on the frame: its area there over the
 *  smaller of its own area and the frame's (a pattern bigger than the frame
 *  that covers it counts as wholly shown). Fractions keep area ratios. */
export function shownShare(box: Box, rotate: number, frame: { w: number; h: number }): number {
  let poly: [number, number][] = turnedCorners(box, rotate, frame);
  const own = Math.abs(polyArea(poly));
  if (own <= 0) return 0;
  for (const [axis, bound, below] of [
    [0, 0, false],
    [0, 1, true],
    [1, 0, false],
    [1, 1, true],
  ] as const) {
    poly = clipHalf(poly, axis, bound, below);
    if (poly.length < 3) return 0;
  }
  return Math.abs(polyArea(poly)) / Math.min(own, 1);
}

/** `poly` cut to one side of a frame edge (Sutherland–Hodgman). */
function clipHalf(poly: [number, number][], axis: 0 | 1, bound: number, below: boolean): [number, number][] {
  const inside = (p: [number, number]) => (below ? p[axis] <= bound : p[axis] >= bound);
  const out: [number, number][] = [];
  for (let i = 0; i < poly.length; i++) {
    const a = poly[i];
    const b = poly[(i + 1) % poly.length];
    if (inside(a)) out.push(a);
    if (inside(a) !== inside(b)) {
      const t = (bound - a[axis]) / (b[axis] - a[axis]);
      out.push([a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t]);
    }
  }
  return out;
}

function polyArea(p: [number, number][]): number {
  let s = 0;
  for (let i = 0; i < p.length; i++) {
    const [x1, y1] = p[i];
    const [x2, y2] = p[(i + 1) % p.length];
    s += x1 * y2 - x2 * y1;
  }
  return s / 2;
}

/** Two boxes the same to within `tol` (fractions). */
export function sameBox(a: Box | null | undefined, b: Box | null | undefined, tol = 1e-4): boolean {
  if (!a || !b) return !a && !b;
  return Math.abs(a.x - b.x) <= tol && Math.abs(a.y - b.y) <= tol && Math.abs(a.w - b.w) <= tol && Math.abs(a.h - b.h) <= tol;
}
