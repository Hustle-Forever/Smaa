// Checks the studio's edits of a pattern's time (src/lib/rebrandTiming.ts,
// src/lib/rebrandSpan.ts dragPatternSpan) — against the server through a
// shared fixture (backend/tests/fixtures/rebrand_pattern_time.json: the
// server's spans_overlap, its words for a time, and whether pattern_el.check
// refuses a full-screen pattern showing with another), and on their own
// terms: a span or a ramp set back gives the very element it began as, a
// transition's own time, the room it makes, the bars' drags.
//
//   node --experimental-strip-types scripts/check-rebrand-pattern-time.mjs
//
// (node 22.6+; the backend test runs it too when node is there). Prints one
// line per group and exits 1 on any difference.

import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import { dragPatternSpan } from "../src/lib/rebrandSpan.ts";
import {
  asTransition,
  clockWords,
  focusTime,
  freeSpan,
  fullClash,
  spanOn,
  spansOverlap,
  startingAfter,
  timeSig,
  timing,
  withRamps,
  withSpan,
} from "../src/lib/rebrandTiming.ts";

const here = dirname(fileURLToPath(import.meta.url));
const fx = JSON.parse(readFileSync(resolve(here, "../../backend/tests/fixtures/rebrand_pattern_time.json"), "utf-8"));
const same = (a, b) => JSON.stringify(a) === JSON.stringify(b);

let failed = 0;
let total = 0;
function group(name, cases, check) {
  let bad = 0;
  for (const c of cases) {
    total += 1;
    const why = check(c);
    if (why) {
      bad += 1;
      if (bad <= 5) console.log(`  ${name}: ${why}`);
    }
  }
  failed += bad;
  console.log(`${bad ? "FAIL" : "ok  "} ${name} (${cases.length})`);
}

// --- against the server --------------------------------------------------------------
group("two spans show together (spans_overlap)", fx.overlap, (c) =>
  spansOverlap(c.a, c.b) === c.expect ? null : `${JSON.stringify(c)} got ${!c.expect}`,
);
group("a time in the server's words (_clock)", fx.clock, (c) => {
  const got = clockWords(c.sec);
  return got === c.expect ? null : `${c.sec}: ${got} != ${c.expect}`;
});
group("a full-screen pattern goes alone while it shows (check)", fx.clash, (c) => {
  const before = c.before.map((p, i) => ({ id: `p${i}`, name: `P${i}`, full: p.full, span: p.span }));
  const got = fullClash(before, { full: c.who.full }, c.who.span);
  const want = c.with === null ? null : `p${c.with}`;
  if (!!got !== c.refused) return `${JSON.stringify(c)}: refused ${!!got}`;
  return (got ? got.id : null) === want ? null : `${JSON.stringify(c)}: with ${got?.id}`;
});

// --- the edits ---------------------------------------------------------------------------
const D = 8;
const studio = { id: "pattern", animation: { entrance: "auto" } };
const server = {
  id: "pattern",
  animation: { entrance: "auto", start: { mode: "clip_start", sec: null, word: null }, duration_sec: 1.0 },
};
const edits = [
  ["the whole scene leaves a studio element as it was", () => same(withSpan(studio, { from: 0, to: D }, D), studio)],
  ["the whole scene leaves a server element as it was", () => same(withSpan(server, { from: 0, to: D }, D), server)],
  [
    "a span on a studio element: its start and end, nothing else",
    () =>
      same(withSpan(studio, { from: 1.5, to: 4 }, D).animation, {
        entrance: "auto",
        start: { mode: "at_sec", sec: 1.5 },
        end: { mode: "at_sec", sec: 4 },
      }),
  ],
  [
    "a span on a server element keeps its shape",
    () =>
      same(withSpan(server, { from: 1.5, to: D }, D).animation, {
        entrance: "auto",
        start: { mode: "at_sec", sec: 1.5, word: null },
        duration_sec: 1.0,
      }),
  ],
  [
    "a span set back is the element it began as (both shapes)",
    () =>
      same(withSpan(withSpan(studio, { from: 2, to: 5 }, D), { from: 0, to: D }, D), studio) &&
      same(withSpan(withSpan(server, { from: 2, to: 5 }, D), { from: 0, to: D }, D), server),
  ],
  [
    "a span is kept inside the scene and at least half a second, to the hundredth",
    () =>
      same(spanOn(withSpan(studio, { from: -1, to: 0.2 }, D), D), { from: 0, to: 0.5 }) &&
      same(spanOn(withSpan(studio, { from: 7.9, to: 9 }, D), D), { from: 7.5, to: 8 }) &&
      same(withSpan(studio, { from: 1.23456, to: 3.98765 }, D).animation.start, { mode: "at_sec", sec: 1.23 }),
  ],
  [
    "ramps at their defaults are left out (a server's length stays written)",
    () =>
      same(withRamps(studio, { entranceSec: 1.0, exit: "auto", exitSec: 0.6 }), studio) &&
      same(withRamps(server, { entranceSec: 1.0, exit: "auto", exitSec: null }), server),
  ],
  [
    "ramps are clamped to 0.2-3 s",
    () =>
      withRamps(studio, { entranceSec: 0.05 }).animation.duration_sec === 0.2 &&
      withRamps(studio, { exitSec: 9 }).animation.exit_sec === 3 &&
      withRamps(studio, { exit: "to_left" }).animation.exit === "to_left",
  ],
  [
    "a transition: 1.5 s at the start, 0.3 s in, 0.5 s out",
    () => {
      const t = asTransition(studio, { sec: 1.5, in_sec: 0.3, out_sec: 0.5 }, D);
      const tm = timing(t, { duration: D, entrance: "fade" });
      return (
        same(t.animation, { entrance: "auto", end: { mode: "at_sec", sec: 1.5 }, duration_sec: 0.3, exit_sec: 0.5 }) &&
        same([tm.t_in, tm.t_full, tm.t_hold_end, tm.t_out, tm.exit], [0, 0.3, 1, 1.5, "fade"])
      );
    },
  ],
  [
    "the others start once it is over — unless one would be too short",
    () => {
      const moved = startingAfter([studio, withSpan(studio, { from: 3, to: 6 }, D)], 1.5, D);
      return (
        same(moved[0].animation.start, { mode: "at_sec", sec: 1.5 }) &&
        same(moved[1], withSpan(studio, { from: 3, to: 6 }, D)) &&
        startingAfter([withSpan(studio, { from: 0, to: 1.8 }, D)], 1.5, D) === null
      );
    },
  ],
  [
    "where a pattern can show beside full-screen ones",
    () =>
      same(freeSpan([{ from: 0, to: 1.5 }], D), { from: 1.5, to: 8 }) &&
      freeSpan([{ from: 0, to: 8 }], D) === null &&
      same(freeSpan([{ from: 2, to: 3 }, { from: 6, to: 7.8 }], D), { from: 3, to: 6 }),
  ],
  [
    "a time of its own shows in the version sign; none, none",
    () =>
      timeSig(studio) === null &&
      timeSig(server) === null &&
      same(timeSig(withSpan(studio, { from: 1.5, to: D }, D)), [1.5, null, 1, null, null]),
  ],
  [
    "the moment a pattern is shown at: just after it is fully in",
    () =>
      Math.abs(focusTime({ t_in: 1.5, t_full: 2.5, t_hold_end: 8, t_out: 8 }) - 2.8) < 1e-9 &&
      Math.abs(focusTime({ t_in: 0, t_full: 0.3, t_hold_end: 0.4, t_out: 1.5 }) - 0.35) < 1e-9,
  ],
];
group("the element edits", edits, ([name, ok]) => (ok() ? null : name));

// --- a bar dragged -------------------------------------------------------------------------
const lim = { min: 0.5, max: Infinity, lo: 0, hi: 8 };
const snap = { points: [0, 8, 1.5, 4.27], within: 0.1, grid: 0.1 };
const drags = [
  // [start, kind, dt, snap, want span, want snapped]
  [{ from: 2, to: 5 }, "move", 0.43, null, { from: 2.43, to: 5.43 }, null],
  [{ from: 2, to: 5 }, "move", 0.44, snap, { from: 2.4, to: 5.4 }, null],
  [{ from: 2, to: 5 }, "move", -0.46, snap, { from: 1.5, to: 4.5 }, 1.5],
  [{ from: 2, to: 5 }, "move", -0.76, snap, { from: 1.27, to: 4.27 }, 4.27],
  [{ from: 2, to: 5 }, "move", 4, snap, { from: 5, to: 8 }, null],
  [{ from: 2, to: 5 }, "start", -0.55, snap, { from: 1.5, to: 5 }, 1.5],
  [{ from: 2, to: 5 }, "start", 2.9, snap, { from: 4.5, to: 5 }, null],
  [{ from: 2, to: 5 }, "end", -0.77, snap, { from: 2, to: 4.27 }, 4.27],
  [{ from: 2, to: 5 }, "end", 9, snap, { from: 2, to: 8 }, null],
  [{ from: 2, to: 5 }, "end", -3, snap, { from: 2, to: 2.5 }, null],
];
group("a pattern's bar dragged (dragPatternSpan)", drags, ([start, kind, dt, s, want, snapped]) => {
  const got = dragPatternSpan(start, kind, dt, lim, s);
  return same(got.span, want) && got.snapped === snapped ? null : `${kind} ${dt}: ${JSON.stringify(got)}`;
});

console.log(failed ? `${failed} of ${total} differ` : `all ${total} cases agree`);
process.exit(failed ? 1 : 0);
