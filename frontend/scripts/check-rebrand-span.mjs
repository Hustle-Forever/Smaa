// Checks the numbers behind icons by hand against the server's, through two
// shared fixtures (backend/tests/fixtures):
//
//   rebrand_icon_span.json   a lane block set by hand -> the pin the studio
//                            writes (src/lib/rebrandSpan.ts iconSpanTweak;
//                            the planner times it back within a frame), the
//                            SpanBlock drags (dragSpan), the rows blocks
//                            stack on (stackRows: as many as it takes), the
//                            icons' tracks (laneRows: the planner's `lane`)
//                            and a block dragged up or down (rowAt)
//   rebrand_icon_cover.json  what the speaker does to a box over a window —
//                            src/lib/rebrandCover.ts against speaker_track's
//                            samples_in / overlap / head_clear / cover
//
// and the stage handles' own maths (src/lib/rebrandTransform.ts).
//
//   node --experimental-strip-types scripts/check-rebrand-span.mjs
//
// (node 22.6+; the backend test runs it too when node is there). Prints one
// line per group and exits 1 on any difference.

import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import { coverOf, headClear, overlapOf, samplesIn } from "../src/lib/rebrandCover.ts";
import { dragSpan, iconSpan, iconSpanTweak, laneRows, rowAt, shownRowTop, stackRows } from "../src/lib/rebrandSpan.ts";
import { applied, between, iconBox, insideFrame, maxScaleInside, scaledAbout, snapAxis } from "../src/lib/rebrandTransform.ts";

const here = dirname(fileURLToPath(import.meta.url));
const read = (name) => JSON.parse(readFileSync(resolve(here, "../../backend/tests/fixtures", name), "utf-8"));
const span = read("rebrand_icon_span.json");
const cover = read("rebrand_icon_cover.json");
const EPS = 1e-9;
const near = (a, b, tol = EPS) => (a === null || b === null ? a === b : Math.abs(a - b) <= tol);

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

// --- spans ------------------------------------------------------------------------
group("a block set by hand -> its pin", span.tweaks, (c) => {
  const got = iconSpanTweak({ from: c.from, to: c.to }, span.clock);
  if (!near(got.sec, c.sec) || !near(got.hold_sec, c.hold_sec)) return `${JSON.stringify(c)} got ${JSON.stringify(got)}`;
  const back = iconSpan(got.sec, got.hold_sec, span.clock);
  if (Math.abs(back.from - c.from) > 0.0051 || Math.abs(back.to - c.to) > 0.0101) return `round trip ${JSON.stringify(back)}`;
  return null;
});
group("SpanBlock drags", span.drags, (c) => {
  const got = dragSpan(c.start, c.kind, c.dt, c.limits, c.snap);
  const ok = near(got.span.from, c.want.span.from) && near(got.span.to, c.want.span.to) && near(got.snapped, c.want.snapped);
  return ok ? null : `${c.kind} ${c.dt} ${c.snap ? "snap" : ""} got ${JSON.stringify(got)} want ${JSON.stringify(c.want)}`;
});
group("rows", span.stacks, (c) => {
  const got = stackRows(c.spans);
  if (JSON.stringify(got) !== JSON.stringify(c.rows)) return `got ${JSON.stringify(got)} want ${JSON.stringify(c.rows)}`;
  // No two blocks on one row overlap.
  for (let i = 0; i < c.spans.length; i++) {
    for (let j = i + 1; j < c.spans.length; j++) {
      const a = c.spans[i];
      const b = c.spans[j];
      if (got[i] === got[j] && a.from < b.to - EPS && b.from < a.to - EPS) return `${i} and ${j} overlap on row ${got[i]}`;
    }
  }
  return null;
});
group("the icons' tracks", span.lanes, (c) => {
  const got = laneRows(c.spans);
  return JSON.stringify(got) === JSON.stringify({ rows: c.rows, count: c.count })
    ? null
    : `got ${JSON.stringify(got)} want ${JSON.stringify({ rows: c.rows, count: c.count })}`;
});
group("a block dragged to another track", span.row_drags, (c) => {
  const got = rowAt(c.row, c.dy, c.pitch, c.count);
  return got === c.want ? null : `${JSON.stringify(c)} got ${got}`;
});

// --- the speaker ----------------------------------------------------------------------
for (const tr of cover.tracks) {
  const s = {
    times: tr.times,
    masks: tr.masks,
    w: tr.w,
    h: tr.h,
    frame: tr.frame,
    sampleFrames: tr.sample_frames,
    fps: tr.fps,
    cuts: tr.cuts,
    heads: tr.heads,
    bars: tr.bars,
  };
  group(`the speaker over a box, ${tr.name}`, tr.cases, (c) => {
    const idx = samplesIn(s, c.t0, c.t1);
    if (JSON.stringify(idx) !== JSON.stringify(c.samples)) return `samples ${JSON.stringify(idx)} want ${JSON.stringify(c.samples)}`;
    const ov = overlapOf(s, c.box, c.t0, c.t1, cover.rules.person);
    if (Math.abs(ov - c.overlap) > 1e-4 + EPS) return `overlap ${ov} want ${c.overlap} (${JSON.stringify(c.box)})`;
    const hc = headClear(s, c.box, c.t0, c.t1, cover.rules.head_grow);
    if (hc !== c.head_clear) return `head_clear ${hc} want ${c.head_clear} (${JSON.stringify(c.box)} ${c.t0}-${c.t1})`;
    const cv = coverOf(s, c.box, c.t0, c.t1, cover.rules);
    return cv === c.cover ? null : `cover ${cv} want ${c.cover}`;
  });
}

// --- the stage handles ------------------------------------------------------------------
const box = { x: 0.1, y: 0.2, w: 0.2, h: 0.1125 };
const handles = [
  () => {
    const t = { dx: 0.05, dy: -0.02, scale: 1.5, rotate: 0 };
    const b = applied(box, t);
    const back = between(box, b);
    return near(back.dx, t.dx, 1e-12) && near(back.dy, t.dy, 1e-12) && near(back.scale, t.scale, 1e-12) ? null : "between(applied) is not itself";
  },
  () => {
    const b = applied(box, { dx: 0, dy: 0, scale: 2, rotate: 0 });
    return near(b.x + b.w / 2, box.x + box.w / 2, 1e-12) && near(b.w / b.h, box.w / box.h, 1e-12) ? null : "a scale is not about the centre";
  },
  () => {
    const b = insideFrame({ x: -0.1, y: 0.95, w: 0.2, h: 0.1 });
    return near(b.x, 0) && near(b.y, 0.9) && near(b.w, 0.2) ? null : `insideFrame ${JSON.stringify(b)}`;
  },
  () => {
    // About its top-left corner, it may grow until its right edge meets the frame's.
    const s = maxScaleInside(box, box.x, box.y);
    const b = scaledAbout(box, s, box.x, box.y);
    return near(b.x + b.w, 1, 1e-12) && near(b.x, box.x, 1e-12) ? null : `maxScaleInside ${s}`;
  },
  () => {
    const got = snapAxis([0.1, 0.2, 0.3], [0.0, 0.305, 0.5], 0.01);
    return got && near(got.target, 0.305) && near(got.shift, 0.005, 1e-12) ? null : `snapAxis ${JSON.stringify(got)}`;
  },
  () => (snapAxis([0.1], [0.2], 0.01) === null ? null : "a far target pulled"),
  () => {
    const b = iconBox(box, { dx: 0.9, dy: 0, scale: 1, rotate: 30 });
    return near(b.x, 0.8) && near(b.w, 0.2) ? null : `iconBox kept inside ${JSON.stringify(b)}`;
  },
];
group("stage handles", handles, (f) => f());

// A block dragged between rows is drawn on the row it would land on; past
// the last (a new track) inside the lane, which keeps its height mid-drag.
group("where a dragged block is drawn", [
  { name: "not dragged", got: shownRowTop(3, 1, null, 24, 2, 30), want: 3 },
  { name: "its own row", got: shownRowTop(27, 1, 1, 24, 2, 30), want: 27 },
  { name: "the row above", got: shownRowTop(27, 1, 0, 24, 2, 30), want: 3 },
  { name: "a new track: inside the lane", got: shownRowTop(27, 1, 2, 24, 2, 30), want: 30 },
  { name: "a new track with no room inside: the row below", got: shownRowTop(27, 1, 2, 24, 2, null), want: 51 },
  { name: "the last row is a row, not a new track", got: shownRowTop(3, 0, 1, 24, 2, 30), want: 27 },
], (c) => (c.got === c.want ? null : `${c.name}: ${c.got}`));

console.log(failed ? `${failed} of ${total} differ` : `all ${total} cases agree`);
process.exit(failed ? 1 : 0);
