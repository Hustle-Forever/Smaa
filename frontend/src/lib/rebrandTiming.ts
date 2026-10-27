// Rebrand studio — a pattern's time, mirrored from the server
// (backend/app/services/rebrand_harness/elements/pattern.py `timing`), rule
// for rule. The studio uses it only to DRAW: the bars on the timeline, the
// moment a pattern is fully in. The server's own timing() makes every frame
// of the video; a shared fixture (backend/tests/fixtures/rebrand_timing.json,
// checked by the backend test and by frontend/scripts/check-rebrand-timing.mjs)
// keeps the two from drifting.
//
// A pattern shows over a SPAN in scene seconds: its entrance runs at the
// start of the span, it holds, and its exit runs at the end.
//
// And the edits of that time (the timeline's bars, the row's Time section):
//
//   withSpan / withRamps   the element with its span or its ramps set — the
//                          recipe's own fields (animation.start / end,
//                          duration_sec, exit / exit_sec), each left out
//                          (deleted) at its default, so an untouched draft
//                          stays byte for byte what it was
//   spansOverlap           two spans show together (recipe.spans_overlap:
//                          spans that only touch do not)
//   fullClash              "a full-screen pattern goes alone WHILE it shows"
//                          (pattern_el.check): the pattern a span would show
//                          together with when one of the two fills the frame
//   asTransition / makeRoom  a full-screen transition as it is added
//                          (Blueprint: 1.5 s at the scene's start, 0.3 s in,
//                          0.5 s out) and the others starting once it is over
//   freeSpan               where a pattern can show beside full-screen ones
//
// backend/tests/fixtures/rebrand_pattern_time.json holds the overlap and the
// clash to the server's (scripts/check-rebrand-pattern-time.mjs).
//
// Plain TypeScript only (no enums, no path aliases, type-only imports): the
// check script runs this file straight in node.

import type { HarnessElement } from "./rebrandHarnessApi";

/** The least a pattern may show (s). */
export const SPAN_MIN_SEC = 0.5;
/** An entrance's or an exit's length (s). */
export const RAMP_SEC: readonly [number, number] = [0.2, 3.0];
/** An exit's length when none is set (s). */
export const EXIT_SEC = 0.6;
/** Each ramp keeps at least this much when a short span squeezes both. */
export const RAMP_SQUEEZE_MIN = 0.1;

/** How a pattern goes out (the server's recipe.PATTERN_EXITS, in order). */
export const PATTERN_EXITS = [
  "auto",
  "none",
  "fade",
  "glitch",
  "to_left",
  "to_right",
  "to_top",
  "to_bottom",
  "slide_out",
] as const;
export type PatternExit = (typeof PATTERN_EXITS)[number];

/** The exit that plays an entrance backwards (pattern_library.EXIT_OF_ENTRANCE). */
export const EXIT_OF_ENTRANCE: Readonly<Record<string, string>> = {
  left_to_right: "to_left",
  right_to_left: "to_right",
  top_to_bottom: "to_top",
  bottom_to_top: "to_bottom",
  fade: "fade",
  glitch: "glitch",
  none: "none",
  slide: "slide_out",
};

/** When a pattern shows, in scene seconds: in from `t_in` to `t_full`, held
 *  to `t_hold_end`, out by `t_out`. `entrance` / `exit` are resolved. */
export interface Timing {
  t_in: number;
  t_full: number;
  t_hold_end: number;
  t_out: number;
  entrance: string;
  exit: string;
  runs_to_end: boolean;
}

export type TimingPhase = "before" | "in" | "hold" | "out" | "after";

type Animated = { animation: HarnessElement["animation"] };

/** [start, end] of a pattern's span as the recipe says it (end null: to the
 *  scene's end) — the server's recipe.pattern_span. */
export function patternSpan(el: Animated): [number, number | null] {
  const a = el.animation;
  const start = a.start;
  const s = start && start.mode === "at_sec" && start.sec != null ? Number(start.sec) : 0;
  const end = a.end;
  const e = end && end.mode === "at_sec" && end.sec != null ? Number(end.sec) : null;
  return [s, e];
}

function ramp(sec: number | null | undefined, fallback: number): number {
  const v = sec == null ? fallback : Number(sec);
  return Math.min(RAMP_SEC[1], Math.max(RAMP_SEC[0], v));
}

/** A pattern's time on a scene `duration` seconds long; `entrance` is the
 *  one the server resolved (the preview's `animation`). null: it starts at
 *  or after the scene's end. */
export function timing(el: Animated, opts: { duration: number; entrance: string }): Timing | null {
  const d = Math.max(0, Number(opts.duration));
  const entrance = opts.entrance;
  const [start, end] = patternSpan(el);
  if (start >= d) return null;
  let s = Math.max(0, start);
  const e = end == null ? d : Math.min(Math.max(end, 0), d);
  if (e <= s) return null;
  if (e - s < SPAN_MIN_SEC) s = Math.max(0, e - SPAN_MIN_SEC);
  const runsToEnd = e >= d - 1e-6;
  let ex: string = el.animation.exit ?? "auto";
  if (ex === "auto") {
    const back = Object.prototype.hasOwnProperty.call(EXIT_OF_ENTRANCE, entrance) ? EXIT_OF_ENTRANCE[entrance] : "fade";
    ex = runsToEnd ? "none" : back;
  }
  let a = entrance === "none" ? 0 : ramp(el.animation.duration_sec, 1.0);
  let b = ex === "none" ? 0 : ramp(el.animation.exit_sec, EXIT_SEC);
  const span = e - s;
  if (a + b > span) {
    if (a > 0 && b > 0) {
      const least = Math.min(RAMP_SQUEEZE_MIN, span / 2);
      let a2 = (span * a) / (a + b);
      let b2 = span - a2;
      if (a2 < least) {
        a2 = least;
        b2 = span - least;
      } else if (b2 < least) {
        a2 = span - least;
        b2 = least;
      }
      a = a2;
      b = b2;
    } else {
      a = Math.min(a, span);
      b = Math.min(b, span);
    }
  }
  return { t_in: s, t_full: s + a, t_hold_end: e - b, t_out: e, entrance, exit: ex, runs_to_end: runsToEnd };
}

/** The phase at scene time t and its linear progress 0..1 (the entrance's,
 *  the exit's; 1 while it holds, 0 before, 1 after). */
export function phase(tm: Timing, t: number): [TimingPhase, number] {
  if (t < tm.t_in) return ["before", 0];
  if (t >= tm.t_out) return ["after", 1];
  if (t < tm.t_full) return ["in", (t - tm.t_in) / (tm.t_full - tm.t_in)];
  if (t < tm.t_hold_end) return ["hold", 1];
  return ["out", (t - tm.t_hold_end) / (tm.t_out - tm.t_hold_end)];
}

// --- editing a pattern's time --------------------------------------------------------

/** Seconds as the recipe keeps a pattern's time: to the hundredth (float
 *  noise never keys a new preview). */
export function sec2(x: number): number {
  return Math.round(x * 100) / 100;
}

/** Within this of the scene's start or end, a span's edge IS the start or
 *  the end (the recipe then keeps none). */
export const EDGE_SEC = 0.02;

/** A span in scene seconds. */
export interface SecSpan {
  from: number;
  to: number;
}

/** A pattern's span on a scene `duration` seconds long, as the timeline
 *  draws it — timing()'s first rule: clamped to the scene, and one a shorter
 *  scene cuts below SPAN_MIN_SEC keeps its end and starts earlier. null when
 *  it starts at or after the scene's end (it does not show). */
export function spanOn(el: Animated, duration: number): SecSpan | null {
  const d = Math.max(0, Number(duration));
  const [start, end] = patternSpan(el);
  if (start >= d) return null;
  let s = Math.max(0, start);
  const e = end == null ? d : Math.min(Math.max(end, 0), d);
  if (e <= s) return null;
  if (e - s < SPAN_MIN_SEC) s = Math.max(0, e - SPAN_MIN_SEC);
  return { from: s, to: e };
}

/** The span is the whole scene: the recipe keeps no start and no end. */
export function isWholeScene(span: SecSpan, duration: number): boolean {
  return span.from <= EDGE_SEC && span.to >= duration - EDGE_SEC;
}

type Anim = HarnessElement["animation"];

/** The element showing over `span` (scene seconds, inside the scene, at
 *  least SPAN_MIN_SEC, to the hundredth): a start only when it isn't the
 *  scene's start, an end only when it isn't its end. A start the server
 *  wrote out (it carries `word`) goes back to clip_start rather than away,
 *  so a round trip gives the very element it began as either way. */
export function withSpan<E extends Animated>(el: E, span: SecSpan, duration: number): E {
  const d = Math.max(0, Number(duration));
  const from = sec2(Math.min(Math.max(0, span.from), Math.max(0, d - SPAN_MIN_SEC)));
  const to = sec2(Math.min(d, Math.max(span.to, from + SPAN_MIN_SEC)));
  const a: Anim = { ...el.animation };
  const had = el.animation.start;
  if (from <= EDGE_SEC) {
    if (had && "word" in had) a.start = { ...had, mode: "clip_start", sec: null };
    else delete a.start;
  } else {
    a.start = had && "word" in had ? { ...had, mode: "at_sec", sec: from } : { mode: "at_sec", sec: from };
  }
  if (to >= d - EDGE_SEC) delete a.end;
  else a.end = { mode: "at_sec", sec: to };
  return { ...el, animation: a };
}

/** An entrance's or an exit's length, within RAMP_SEC, to the hundredth. */
export function rampSec(v: number): number {
  return sec2(Math.min(RAMP_SEC[1], Math.max(RAMP_SEC[0], Number(v))));
}

export interface Ramps {
  /** Its entrance ("auto": its own). */
  entrance?: string;
  /** The entrance's length (s; null: the default, 1 s). */
  entranceSec?: number | null;
  /** How it goes out ("auto" / null: its own — none when it lasts to the
   *  end, else its entrance played backwards). */
  exit?: string | null;
  /** The exit's length (s; null: EXIT_SEC). */
  exitSec?: number | null;
}

/** The element with its entrance and exit (and their lengths) set; each
 *  length at its default is left out, as the server leaves it out (an
 *  entrance length the server wrote out stays written). */
export function withRamps<E extends Animated>(el: E, r: Ramps): E {
  const a: Anim = { ...el.animation };
  if (r.entrance !== undefined) a.entrance = r.entrance;
  if (r.entranceSec !== undefined) {
    const v = r.entranceSec == null ? 1.0 : rampSec(r.entranceSec);
    if (v === 1.0 && !("duration_sec" in el.animation)) delete a.duration_sec;
    else a.duration_sec = v;
  }
  if (r.exit !== undefined) {
    if (!r.exit || r.exit === "auto") delete a.exit;
    else a.exit = r.exit as Anim["exit"];
  }
  if (r.exitSec !== undefined) {
    const v = r.exitSec == null ? null : rampSec(r.exitSec);
    if (v == null || Math.abs(v - EXIT_SEC) < 1e-9) delete a.exit_sec;
    else a.exit_sec = v;
  }
  return { ...el, animation: a };
}

/** A pattern's time as it draws — its span and ramps as the recipe has
 *  them — or null while it has none of its own (the whole scene, in over
 *  1 s, out as its own): what "same as v2" compares. */
export function timeSig(el: Animated): (number | string | null)[] | null {
  const [s, e] = patternSpan(el);
  const a = el.animation;
  const dur = a.duration_sec ?? 1.0;
  const exit = a.exit && a.exit !== "auto" ? a.exit : null;
  const exitSec = a.exit_sec ?? null;
  if (s <= 0 && e == null && dur === 1.0 && exit == null && (exitSec == null || exitSec === EXIT_SEC)) return null;
  return [s, e, dur, exit, exitSec];
}

/** Two spans show at the same time (the server's recipe.spans_overlap):
 *  spans that only touch — one ends at 1.5 s, the next starts at 1.5 s —
 *  do not. An end of null is the scene's end. */
export function spansOverlap(a: readonly [number, number | null], b: readonly [number, number | null]): boolean {
  const ae = a[1] == null ? Infinity : a[1];
  const be = b[1] == null ? Infinity : b[1];
  return a[0] < be - 1e-9 && b[0] < ae - 1e-9;
}

/** A pattern as the "goes alone" rule sees it. */
export interface TimedPattern {
  id: string;
  name: string;
  /** It fills the whole frame. */
  full: boolean;
  /** Its span as the recipe says it (patternSpan). */
  span: readonly [number, number | null];
}

/** "A full-screen pattern goes alone WHILE it shows" (the server's
 *  pattern_el.check): the first of `others` that `who` over `span` would
 *  show together with when either fills the frame — or null. */
export function fullClash(
  others: readonly TimedPattern[],
  who: { full: boolean },
  span: readonly [number, number | null],
): TimedPattern | null {
  return others.find((o) => (o.full || who.full) && spansOverlap(o.span, span)) ?? null;
}

/** A time as the studio writes it, as the server's own words do
 *  (elements/pattern._clock): "0:01.5", "0:06" on a whole second. */
export function clockWords(sec: number): string {
  // Python's round(): a half goes to the even tenth.
  const x = Math.max(0, Number(sec)) * 10;
  const up = Math.round(x);
  const tenths = Math.abs(x - Math.trunc(x)) === 0.5 && up % 2 !== 0 ? up - 1 : up;
  const m = Math.floor(tenths / 600);
  const rest = tenths % 600;
  const s = Math.floor(rest / 10);
  const d = rest % 10;
  return `${m}:${String(s).padStart(2, "0")}${d ? `.${d}` : ""}`;
}

/** A full-screen transition's time (the catalogue's `transition`). */
export interface TransitionTime {
  sec: number;
  in_sec: number;
  out_sec: number;
}

/** A full-screen pattern as a transition is added: shown from the scene's
 *  start for `t.sec`, in over `t.in_sec`, out over `t.out_sec`. */
export function asTransition<E extends Animated>(el: E, t: TransitionTime, duration: number): E {
  return withRamps(withSpan(el, { from: 0, to: t.sec }, duration), { entranceSec: t.in_sec, exitSec: t.out_sec });
}

/** Every pattern of `els` that would show before `from` s starts at `from`
 *  instead (it keeps its end), so a transition over the scene's start goes
 *  alone — or null when one would then show less than SPAN_MIN_SEC. The
 *  rest are returned as they are. */
export function startingAfter<E extends Animated>(els: readonly E[], from: number, duration: number): E[] | null {
  const out: E[] = [];
  for (const el of els) {
    const sp = spanOn(el, duration);
    if (!sp || sp.from >= from - 1e-9) {
      out.push(el);
      continue;
    }
    if (sp.to - from < SPAN_MIN_SEC - 1e-9) return null;
    out.push(withSpan(el, { from, to: sp.to }, duration));
  }
  return out;
}

/** Where a pattern can show beside full-screen ones (their spans on the
 *  scene): the longest stretch none of them covers, or null when none is
 *  as long as SPAN_MIN_SEC. */
export function freeSpan(full: readonly SecSpan[], duration: number): SecSpan | null {
  const d = Math.max(0, Number(duration));
  const taken = [...full].sort((a, b) => a.from - b.from);
  let best: SecSpan | null = null;
  let at = 0;
  const consider = (from: number, to: number) => {
    if (to - from >= SPAN_MIN_SEC - 1e-9 && (!best || to - from > best.to - best.from + 1e-9)) best = { from, to };
  };
  for (const f of taken) {
    consider(at, Math.min(d, f.from));
    at = Math.max(at, f.to);
  }
  consider(at, d);
  return best;
}

/** The moment a pattern is shown at when its row opens or its bar is let
 *  go: just after it is fully in (never past its hold). */
export function focusTime(tm: Timing): number {
  const hi = Math.max(tm.t_full, tm.t_hold_end - 0.05);
  return Math.min(Math.max(tm.t_full + 0.3, tm.t_in), hi);
}
