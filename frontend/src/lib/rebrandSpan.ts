// Rebrand studio — a bar on the scene's timeline, as numbers: what one drag
// of a SpanBlock does to its span. The icon lane's blocks (the shortest an
// icon may be fully on: 0.2 s) and the patterns' bars (at least 0.5 s).
//
//   dragSpan     moved whole, or one edge trimmed (the other kept), inside
//                its limits; the anchor (an icon's settle) lands on the
//                nearest snap point within reach — "Snap to words"
//   dragPatternSpan  a pattern's bar: the edge a drag moves (either, for a
//                move) lands on the nearest snap point within reach — the
//                scene's ends, the playhead, the other bars' edges, the
//                parts, the words — else on the 0.1 s grid
//   stackRows    blocks that overlap in time go on rows of their own (as
//                many as it takes: the first row each one fits on)
//   laneRows     the icons' rows: each block's own track from the plan (the
//                planner's), else stacked
//   rowAt        the row a block dragged up or down lands on (one past the
//                last: a new track)
//   iconSpan     an icon's block: from when it starts coming in (settle −
//                its entrance) to when it is gone (settle + hold + its exit)
//   iconSpanTweak  a block set by hand as the pin the planner reads: the
//                settle (sec) and how long it is fully on (hold_sec), to the
//                hundredth — the planner times it back to within a frame
//                (backend tests/fixtures/rebrand_icon_span.json)
//
// Plain TypeScript only (no enums, no path aliases, type-only imports): node
// runs it (scripts/check-rebrand-span.mjs).

export interface Span {
  /** Seconds. */
  from: number;
  to: number;
}

export type SpanDrag = "move" | "start" | "end";

export interface SpanLimits {
  /** The shortest the span may be (s). */
  min: number;
  /** The longest (s; Infinity: no limit). */
  max: number;
  /** Where it must stay (s). */
  lo: number;
  hi: number;
}

export interface SpanSnap {
  /** The times the anchor may land on (s). */
  points: readonly number[];
  /** The anchor: this far into the span from its start (s) — an icon's
   *  entrance, so its settle lands on the word. */
  anchor: number;
  /** How near a point pulls it in (s). */
  within: number;
}

const EPS = 1e-9;

function nearest(points: readonly number[], at: number, within: number): number | null {
  let best: number | null = null;
  for (const p of points) {
    if (Math.abs(p - at) <= within + EPS && (best === null || Math.abs(p - at) < Math.abs(best - at) - EPS)) best = p;
  }
  return best;
}

/** `start` dragged by `dt` seconds: moved whole (`move`), its start trimmed
 *  (the end kept) or its end trimmed (the start kept), inside `limits`.
 *  With `snap`, a move or a start trim lands its anchor on the nearest
 *  point within reach (`snapped`: that point; null when none, or when the
 *  limits pulled it off). */
export function dragSpan(
  start: Span,
  kind: SpanDrag,
  dt: number,
  limits: SpanLimits,
  snap: SpanSnap | null = null,
): { span: Span; snapped: number | null } {
  const { min, max, lo, hi } = limits;
  if (kind === "end") {
    let to = start.to + dt;
    to = Math.min(to, hi, start.from + max);
    to = Math.max(to, start.from + min);
    return { span: { from: start.from, to }, snapped: null };
  }
  let from = start.from + dt;
  let snapped: number | null = null;
  if (snap) {
    const p = nearest(snap.points, from + snap.anchor, snap.within);
    if (p !== null) {
      from = p - snap.anchor;
      snapped = p;
    }
  }
  if (kind === "move") {
    const len = start.to - start.from;
    const clamped = Math.min(Math.max(from, lo), hi - len);
    if (Math.abs(clamped - from) > EPS) snapped = null;
    return { span: { from: clamped, to: clamped + len }, snapped };
  }
  // The start: the end stays where it is.
  const clamped = Math.max(Math.min(from, start.to - min), lo, start.to - max);
  if (Math.abs(clamped - from) > EPS) snapped = null;
  return { span: { from: clamped, to: start.to }, snapped };
}

/** What a pattern's bar snaps to: the points (s) an edge lands on within
 *  `within` (s), else the `grid` (s; 0: none). */
export interface EdgeSnap {
  points: readonly number[];
  within: number;
  grid: number;
}

function onGrid(x: number, grid: number): number {
  return grid > 0 ? Math.round(x / grid) * grid : x;
}

function to2(x: number): number {
  return Math.round(x * 100) / 100;
}

/** A pattern's bar dragged by `dt` s: moved whole (whichever edge is nearer
 *  a snap point lands on it), its start trimmed or its end trimmed (the
 *  other edge kept), inside `limits`; without a point in reach the moved
 *  edge lands on the grid. `snapped`: the point it took (null: none, or the
 *  limits pulled it off). No `snap` (Alt): exactly where it was let go, to
 *  the hundredth. */
export function dragPatternSpan(
  start: Span,
  kind: SpanDrag,
  dt: number,
  limits: SpanLimits,
  snap: EdgeSnap | null = null,
): { span: Span; snapped: number | null } {
  const { min, max, lo, hi } = limits;
  const land = (x: number): { at: number; point: number | null } => {
    if (!snap) return { at: x, point: null };
    const p = nearest(snap.points, x, snap.within);
    return p !== null ? { at: p, point: p } : { at: onGrid(x, snap.grid), point: null };
  };
  if (kind === "move") {
    const len = start.to - start.from;
    let from = start.from + dt;
    let snapped: number | null = null;
    if (snap) {
      const a = nearest(snap.points, from, snap.within);
      const b = nearest(snap.points, from + len, snap.within);
      if (a !== null && (b === null || Math.abs(a - from) <= Math.abs(b - (from + len)))) {
        from = a;
        snapped = a;
      } else if (b !== null) {
        from = b - len;
        snapped = b;
      } else {
        from = onGrid(from, snap.grid);
      }
    }
    const clamped = Math.min(Math.max(from, lo), hi - len);
    if (Math.abs(clamped - from) > EPS) snapped = null;
    return { span: { from: to2(clamped), to: to2(clamped + len) }, snapped };
  }
  if (kind === "start") {
    const got = land(start.from + dt);
    const clamped = Math.max(Math.min(got.at, start.to - min), lo, start.to - max);
    return {
      span: { from: to2(clamped), to: start.to },
      snapped: Math.abs(clamped - got.at) > EPS ? null : got.point,
    };
  }
  const got = land(start.to + dt);
  const clamped = Math.max(Math.min(got.at, hi, start.from + max), start.from + min);
  return { span: { from: start.from, to: to2(clamped) }, snapped: Math.abs(clamped - got.at) > EPS ? null : got.point };
}

/** The row each span sits on: the first row it fits on after the ones
 *  already there (by start), a new row when it fits on none, so no two on
 *  a row overlap (icons may be on screen together). In the order given (the
 *  lane's: speech order). */
export function stackRows(spans: readonly Span[]): number[] {
  const order = spans.map((s, i) => ({ s, i })).sort((a, b) => a.s.from - b.s.from || a.i - b.i);
  const ends: number[] = [];
  const rows = new Array<number>(spans.length).fill(0);
  for (const { s, i } of order) {
    let row = ends.findIndex((end) => s.from >= end - EPS);
    if (row < 0) {
      row = ends.length;
      ends.push(-Infinity);
    }
    rows[i] = row;
    ends[row] = Math.max(ends[row], s.to);
  }
  return rows;
}

/** A block with the track the planner put it on (`lane`: 0 the AI's, one
 *  at a time; 1+ one of the user's that shows with others). */
export interface LaneSpan extends Span {
  lane?: number | null;
}

/** The icons' rows and how many (at least one): each block on its own
 *  track when every one has a track (the planner's: numbered with none
 *  empty above the last), else stacked as they overlap (an older server's
 *  plan; a version's frozen icons whose tracks aren't known). */
export function laneRows(spans: readonly LaneSpan[]): { rows: number[]; count: number } {
  const tracked = spans.length > 0 && spans.every((s) => typeof s.lane === "number" && s.lane >= 0);
  const rows = tracked ? spans.map((s) => Math.floor(s.lane as number)) : stackRows(spans);
  return { rows, count: Math.max(1, ...rows.map((r) => r + 1)) };
}

/** The row a block on row `row` lands on, dragged `dy` px up or down on
 *  rows `pitch` px apart, `count` rows drawn: from 0 to `count` (one past
 *  the last: a new track). */
export function rowAt(row: number, dy: number, pitch: number, count: number): number {
  if (!(pitch > 0)) return row;
  return Math.min(Math.max(0, row + Math.round(dy / pitch)), Math.max(0, count));
}

/** Where a block dragged between rows is drawn (its top, px): on the row it
 *  would land on (`liveRow`; null: not dragged, its own `top`); one past the
 *  last of `count` (a new track) at `newTop` when given, inside the lane,
 *  so the lane need not grow under the pointer while it is dragged. */
export function shownRowTop(
  top: number,
  row: number,
  liveRow: number | null,
  pitch: number,
  count: number,
  newTop?: number | null,
): number {
  if (liveRow == null) return top;
  if (liveRow >= count && newTop != null) return newTop;
  return top + (liveRow - row) * pitch;
}

/** The planner's frame numbers, in seconds. */
export interface IconClock {
  fps: number;
  in_f: number;
  out_f: number;
}

/** An icon's block for a settle and a hold (s). */
export function iconSpan(settle: number, hold: number, c: IconClock): Span {
  return { from: settle - c.in_f / c.fps, to: settle + hold + c.out_f / c.fps };
}

/** Seconds to the hundredth, as the recipe keeps a pin's (rebrandIcons). */
export function round2(x: number): number {
  return Math.round(x * 100) / 100;
}

/** The pin a block set by hand is: its settle (`sec`, the block's start +
 *  the entrance) and how long it is fully on (`hold_sec`). */
export function iconSpanTweak(span: Span, c: IconClock): { sec: number; hold_sec: number } {
  const inS = c.in_f / c.fps;
  const outS = c.out_f / c.fps;
  return { sec: round2(span.from + inS), hold_sec: round2(span.to - span.from - inS - outS) };
}
