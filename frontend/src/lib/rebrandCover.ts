// Rebrand studio — what the speaker does to a box an icon is dragged to: the
// planner's own rule (backend speaker_track.Track.cover), rule for rule,
// on the same samples (the icon plan's masks: each sample's matte at 1/8
// size, its head box, the bars) — so the outline while dragging says what
// the plan will say once it is dropped:
//
//   clear   off the speaker's (grown) head, and the speaker covers at most
//           3 % of it — green
//   near    the speaker covers more than 3 % of it at some sample: it sits
//           behind the speaker there — amber
//   over    it meets the speaker's head (grown by 0.02 of the frame's
//           width) — red
//
// Judged over the icon's whole window (its time on screen ± 0.25 s), as the
// planner judges it. Python's arithmetic where it differs from JavaScript's
// (floor division, round half to even) is copied, so the two agree exactly
// (backend tests/fixtures/rebrand_icon_cover.json, scripts/check-rebrand-span.mjs).
//
// Plain TypeScript only (no enums, no path aliases, type-only imports).

export interface SpeakerSamples {
  /** Each sample's time (s). */
  times: readonly number[];
  /** Each sample's matte, h × w, row by row, 0..255. */
  masks: readonly ArrayLike<number>[];
  w: number;
  h: number;
  /** The frame (px). */
  frame: readonly [number, number];
  /** Without a cut the samples are evenly spaced: this many frames apart. */
  sampleFrames: number;
  fps: number;
  /** The clip's jump cuts (s): the samples' own times are read then. */
  cuts: readonly number[];
  /** Each sample's head box (frame px; null: none found). */
  heads: readonly (readonly number[] | null)[];
  /** The letterbox bars at the masks' size: [row, x0, x1) runs — never the speaker. */
  bars: readonly (readonly number[])[];
}

export type Cover = "clear" | "near" | "over";

export interface CoverRules {
  overlap_max?: number;
  head_grow?: number;
  person?: number;
  window_pad?: number;
}

const OVERLAP_MAX = 0.03;
const HEAD_GROW = 0.02;
const PERSON = 0.5;
const SAMPLE_FPS = 2.0;

/** Python's float floor division (CPython float_floor_div). */
export function pyFloorDiv(vx: number, wx: number): number {
  let mod = vx % wx;
  let div = (vx - mod) / wx;
  if (mod) {
    if (wx < 0 !== mod < 0) {
      mod += wx;
      div -= 1;
    }
  }
  if (div) {
    let floordiv = Math.floor(div);
    if (div - floordiv > 0.5) floordiv += 1;
    return floordiv;
  }
  return 0;
}

/** Python's round(x) for a float: half to even. */
export function pyRound(x: number): number {
  const f = Math.floor(x);
  const d = x - f;
  if (d > 0.5) return f + 1;
  if (d < 0.5) return f;
  return f % 2 === 0 ? f : f + 1;
}

/** numpy.searchsorted on ascending times. */
function sorted(times: readonly number[], v: number, side: "left" | "right"): number {
  let n = 0;
  for (const t of times) if (side === "right" ? t <= v : t < v) n++;
  return n;
}

/** The samples covering [t0, t1] (Track.samples_in). */
export function samplesIn(s: SpeakerSamples, t0: number, t1: number): number[] {
  const n = s.times.length;
  if (!n) return [];
  let lo: number;
  let hi: number;
  if (!s.cuts.length) {
    const dt = s.fps ? s.sampleFrames / s.fps : 1 / SAMPLE_FPS;
    lo = Math.trunc(Math.max(0, Math.min(n - 1, pyFloorDiv(Math.max(0, t0) + 1e-9, dt))));
    hi = Math.trunc(Math.max(0, Math.min(n - 1, -pyFloorDiv(-(Math.max(t0, t1) - 1e-9), dt))));
  } else {
    lo = Math.trunc(Math.max(0, Math.min(n - 1, sorted(s.times, Math.max(0, t0) + 1e-9, "right") - 1)));
    hi = Math.trunc(Math.max(0, Math.min(n - 1, sorted(s.times, Math.max(t0, t1) - 1e-9, "left"))));
  }
  const out: number[] = [];
  for (let i = lo; i <= Math.max(lo, hi); i++) out.push(i);
  return out;
}

interface Box {
  x: number;
  y: number;
  w: number;
  h: number;
}

/** The mask cells a box covers (Track._box_cells). */
function cells(s: SpeakerSamples, box: Box): [number, number, number, number] {
  const x0 = Math.trunc(Math.max(0, Math.min(s.w, box.x * s.w)));
  const y0 = Math.trunc(Math.max(0, Math.min(s.h, box.y * s.h)));
  const x1 = Math.trunc(Math.max(x0 + 1, Math.min(s.w, -Math.floor(-(box.x + box.w) * s.w))));
  const y1 = Math.trunc(Math.max(y0 + 1, Math.min(s.h, -Math.floor(-(box.y + box.h) * s.h))));
  return [x0, y0, x1, y1];
}

/** The largest share of `box` the speaker covers at any sample in [t0, t1], to the
 *  ten-thousandth (Track.overlap); the bars never count. */
export function overlapOf(s: SpeakerSamples, box: Box, t0: number, t1: number, person = PERSON): number {
  const idx = samplesIn(s, t0, t1);
  if (!idx.length) return 0;
  const [x0, y0, x1c, y1c] = cells(s, box);
  const x1 = Math.min(x1c, s.w);
  const y1 = Math.min(y1c, s.h);
  const size = Math.max(0, x1 - x0) * Math.max(0, y1 - y0);
  if (!size) return 0;
  const bar = new Set<number>();
  for (const [y, a, b] of s.bars) for (let x = a; x < b; x++) bar.add(y * s.w + x);
  const cut = Math.trunc(person * 255);
  let most = 0;
  for (const i of idx) {
    const m = s.masks[i];
    let on = 0;
    for (let y = y0; y < y1; y++) {
      for (let x = x0; x < x1; x++) {
        const k = y * s.w + x;
        if (m[k] > cut && !bar.has(k)) on++;
      }
    }
    most = Math.max(most, on / size);
  }
  return pyRound(most * 1e4) / 1e4;
}

/** The frame's whole pixels under a box (speaker_track._px). */
function px(box: Box, W: number, H: number): [number, number, number, number] {
  const x0 = pyRound(box.x * W);
  const y0 = pyRound(box.y * H);
  const x1 = pyRound((box.x + box.w) * W);
  const y1 = pyRound((box.y + box.h) * H);
  return [Math.max(0, x0), Math.max(0, y0), Math.min(W, x1), Math.min(H, y1)];
}

/** `box` stays off the speaker's head, grown by `grow` of the frame's width, at every
 *  sample in [t0, t1] (Track.head_clear). */
export function headClear(s: SpeakerSamples, box: Box, t0: number, t1: number, grow = HEAD_GROW): boolean {
  const [W, H] = s.frame;
  const [bx0, by0, bx1, by1] = px(box, W, H);
  const g = grow * W;
  for (const i of samplesIn(s, t0, t1)) {
    const hd = s.heads[i];
    if (!hd || hd[0] < 0) continue;
    const [hx0, hy0, hx1, hy1] = hd;
    if (!(bx1 <= hx0 - g || bx0 >= hx1 + g || by1 <= hy0 - g || by0 >= hy1 + g)) return false;
  }
  return true;
}

/** "over" the speaker's head, "near" (behind the speaker, more than 3 % of it), or "clear" —
 *  over [t0, t1] (Track.cover). */
export function coverOf(s: SpeakerSamples, box: Box, t0: number, t1: number, rules: CoverRules = {}): Cover {
  if (!headClear(s, box, t0, t1, rules.head_grow ?? HEAD_GROW)) return "over";
  if (overlapOf(s, box, t0, t1, rules.person ?? PERSON) > (rules.overlap_max ?? OVERLAP_MAX)) return "near";
  return "clear";
}
