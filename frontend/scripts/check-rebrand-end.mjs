// Checks src/lib/rebrandEnd.ts — a scene's end (the end grip):
//   * frames ↔ seconds: the bubble ("7.50 s") and the server's seconds
//     (duration_sec: round(n / 30, 2) — never a tie, so JS and Python agree
//     for every end the grip can have);
//   * the range: 1.0 s (30 frames) to the clip's real end, clamped;
//   * a drop: within 2 frames of the automatic end IS automatic (null) — the
//     tail-flash cut, else the whole clip;
//   * snapping: to the nearest point within 8 px, else where it was put;
//   * the keys: ←/→ one frame, Shift 10, Home / End the limits, 0 and
//     Backspace automatic, a step onto the automatic end is automatic;
//   * the cut (the server's _cut_n): only an end before the clip's own last
//     frame cuts, and with no cut the scene's seconds are the server's own;
//   * the snap points: the automatic cut, part starts, words, the end of
//     speech — in range, once each, in order;
//   * Generate waits for the whole clip: an end past the automatic cut on
//     the cut clip (a reload, Original then Next, the whole clip still on
//     its way), never at or before the cut, on the whole clip, on a server
//     without it, or with nothing to ask;
//   * the start (a main scene's head trim): its range (the clip's first
//     frame to 1.0 s before the end), a drop within 2 frames of the first
//     frame is no trim, its snaps and keys (→ from no trim goes to the
//     first start kept), and the end grip kept 1.0 s after it — with no
//     start every end helper answers exactly as before.
//
//   node --experimental-strip-types scripts/check-rebrand-end.mjs
//
// (node 22.6+.) Prints a line per group and exits 1 on any failure.

import {
  AUTO_NEAR,
  autoEnd,
  autoFits,
  clampEnd,
  clampStart,
  cutOf,
  dropEnd,
  dropStart,
  endBubble,
  endNow,
  endRange,
  endReset,
  endSnaps,
  FPS,
  framesSec,
  keyEnd,
  keyStart,
  MIN_END,
  sceneSeconds,
  snapEnd,
  SNAP_PX,
  START_MIN,
  startRange,
  startSnaps,
  wantsWhole,
} from "../src/lib/rebrandEnd.ts";

let failed = 0;
let total = 0;
const groups = [];
let group = null;
const start = (name) => {
  group = { name, n: 0, bad: 0 };
  groups.push(group);
};
const check = (cond, msg) => {
  total += 1;
  group.n += 1;
  if (!cond) {
    failed += 1;
    group.bad += 1;
    if (failed <= 30) console.log(`FAIL ${group.name}: ${msg}`);
  }
};
const eq = (a, b) => JSON.stringify(a) === JSON.stringify(b);

// 815's main scene (a Veo tail flash: cut at 235 of 240), its intro (a 4 s
// card animation: no cut), a clean clip.
const MAIN = { auto: 235, extent: 240 };
const INTRO = { auto: null, extent: 120 };
const CLEAN = { auto: null, extent: 239 };

start("frames ↔ seconds");
check(FPS === 30 && MIN_END === 30 && AUTO_NEAR === 2 && SNAP_PX === 8, "the plan's numbers");
check(endBubble(225) === "7.50 s", endBubble(225));
check(endBubble(235) === "7.83 s", endBubble(235));
check(endBubble(30) === "1.00 s", endBubble(30));
check(framesSec(235) === 7.83 && framesSec(200) === 6.67 && framesSec(234) === 7.8 && framesSec(120) === 4, "framesSec");
// Python's round() only differs from Math.round on an exact tie; 100·n/30 =
// 10n/3 is never k + 0.5, so every end the grip can have agrees.
for (let n = 1; n <= 900; n++) {
  const exact = (10 * n) / 3;
  check(Math.abs(exact - Math.floor(exact) - 0.5) > 1e-9, `n=${n}: no tie`);
  check(framesSec(n) === Math.round(exact) / 100, `n=${n}: ${framesSec(n)}`);
}

start("the range: 1.0 s to the clip's real end");
check(eq(endRange(MAIN), { min: 30, max: 240 }), JSON.stringify(endRange(MAIN)));
check(eq(endRange(INTRO), { min: 30, max: 120 }), JSON.stringify(endRange(INTRO)));
check(clampEnd(10, MAIN) === 30 && clampEnd(-5, MAIN) === 30 && clampEnd(999, MAIN) === 240, "clamped");
check(clampEnd(200.4, MAIN) === 200 && clampEnd(200.6, MAIN) === 201, "whole frames");
check(eq(endRange({ auto: null, extent: 20 }), { min: 20, max: 20 }), "a clip shorter than the floor: its own length");

start("where it ends now");
check(endNow(null, MAIN) === 235 && endNow(undefined, MAIN) === 235, "automatic: the cut");
check(endNow(null, INTRO) === 120 && endNow(null, CLEAN) === 239, "no cut: the whole clip");
check(endNow(200, MAIN) === 200 && endNow(238, MAIN) === 238, "set by hand");
check(autoEnd(MAIN) === 235 && autoEnd(INTRO) === 120, "autoEnd");

start("a drop: near the automatic end is automatic");
for (let f = 0; f <= 300; f++) {
  const got = dropEnd(f, MAIN);
  const at = Math.min(240, Math.max(30, f));
  check(got === (Math.abs(at - 235) <= 2 ? null : at), `main f=${f} → ${got}`);
  const gi = dropEnd(f, INTRO);
  const ai = Math.min(120, Math.max(30, f));
  check(gi === (Math.abs(ai - 120) <= 2 ? null : ai), `intro f=${f} → ${gi}`);
}
check(dropEnd(236, MAIN, 0) === 236 && dropEnd(235, MAIN, 0) === null, "a key: only exactly on it");

start("snapping within 8 px");
{
  const pxPerFrame = 3; // 8 px = 2.67 frames
  const pts = [100, 150, 235];
  check(snapEnd(102, pts, pxPerFrame) === 100, "2 frames = 6 px: snaps");
  check(snapEnd(103, pts, pxPerFrame) === 103, "3 frames = 9 px: stays");
  check(snapEnd(233, pts, pxPerFrame) === 235, "to the automatic cut");
  check(snapEnd(125, pts, pxPerFrame) === 125, "between points: stays");
  check(snapEnd(149, [148, 151], pxPerFrame) === 148, "the nearest of two");
  check(snapEnd(149, [], pxPerFrame) === 149, "no points");
  check(snapEnd(120, [100], 0.5) === 120 && snapEnd(116, [100], 0.5) === 100, "a narrow ruler: 8 px is more frames");
}

start("the keys");
{
  const k = (key, shift, user, f = MAIN) => keyEnd(key, shift, user, f);
  check(eq(k("ArrowLeft", false, null), { end: 234, nudge: true }), JSON.stringify(k("ArrowLeft", false, null)));
  check(eq(k("ArrowRight", false, null), { end: 236, nudge: true }), "→ from automatic: past the cut");
  check(eq(k("ArrowLeft", true, null), { end: 225, nudge: true }), "Shift ← 10");
  check(eq(k("ArrowRight", true, 230), { end: 240, nudge: true }), "Shift → to the end");
  check(eq(k("ArrowRight", false, 234), { end: null, nudge: true }), "a step onto the cut is automatic");
  check(eq(k("ArrowLeft", false, 30), { end: 30, nudge: true }), "the floor holds");
  check(eq(k("Home", false, 200), { end: 30, nudge: false }), "Home: 1.0 s");
  check(eq(k("End", false, 200), { end: 240, nudge: false }), "End: the clip's real end");
  check(eq(k("End", false, 60, INTRO), { end: null, nudge: false }), "End with no cut: the whole clip = automatic");
  check(eq(k("0", false, 200), { end: null, nudge: false }) && eq(k("Backspace", false, 200), { end: null, nudge: false }), "0 / Backspace");
  check(k("a", false, 200) === undefined && k("Enter", false, 200) === undefined && k("ArrowUp", false, 200) === undefined, "not its keys");
}

start("the cut (the server's _cut_n) and the scene's seconds");
{
  check(cutOf(null, 234) === null && cutOf(undefined, 234) === null, "no end: no cut");
  check(cutOf(200, 234) === 200, "before the last frame: cut");
  check(cutOf(234, 234) === null && cutOf(235, 234) === null && cutOf(238, 234) === null, "at or past the clip's end: none");
  check(cutOf(200, 0) === null && cutOf(200, null) === null, "frames not counted: no cut");
  // With no cut the seconds are exactly the server's, whatever they are.
  for (const whole of [7.83, 7.8, 3.97, 4, 7.97, 0.1, 12.345]) check(sceneSeconds(whole, null) === whole, `whole ${whole}`);
  check(sceneSeconds(7.8, 200) === 6.67 && sceneSeconds(7.8, 225) === 7.5, "cut: the end's seconds");
}

start("the snap points");
{
  const pts = endSnaps(MAIN, { parts: [4], words: [0.2, 1.5, 3.25, 7.95, 9], voicedEnd: 7.1 });
  check(eq(pts, [45, 98, 120, 213, 235, 239]), JSON.stringify(pts));
  check(eq(endSnaps(INTRO, {}), []), "an intro with nothing said: none");
  check(eq(endSnaps(MAIN, { words: [7.83, 7.84] }), [235]), "once each");
  const every = endSnaps(MAIN, { words: Array.from({ length: 200 }, (_, i) => i / 20) });
  check(every.every((f, i) => f >= 30 && f <= 240 && (i === 0 || every[i - 1] < f)), "in range, in order");
}

start("Generate waits for the whole clip past the automatic cut");
{
  const w = (o) => wantsWhole({ end: 236, auto: 235, onWhole: false, canAsk: true, whole: null, ...o });
  check(w({}) === true, "past the cut, on the cut clip (a reload): waits");
  check(w({ whole: "preparing" }) === true, "…while the whole clip is got ready");
  check(w({ whole: "ready" }) === true, "…ready but not mounted yet (the grip still held)");
  check(w({ whole: "failed" }) === true, "…not got ready: still waits (Try again)");
  check(w({ onWhole: true }) === false, "on the whole clip: goes");
  check(w({ end: 235 }) === false && w({ end: 200 }) === false, "at or before the cut: goes");
  check(w({ end: null }) === false && w({ end: undefined }) === false, "automatic: goes");
  check(w({ auto: null }) === false, "no cut (an intro, a clean clip): goes");
  check(w({ whole: "none" }) === false, "a server without the whole clip: as before");
  check(w({ canAsk: false }) === false, "nothing to ask it of: as before");
}

start("the start: its range, 0 to 1.0 s before the end");
{
  check(START_MIN === 3, "the first start kept");
  check(eq(startRange(235), { min: 0, max: 205 }) && eq(startRange(200), { min: 0, max: 170 }), "1.0 s before the end");
  check(eq(startRange(20), { min: 0, max: 0 }), "a scene a second long or less: no room");
  check(clampStart(-5, 235) === 0 && clampStart(999, 235) === 205 && clampStart(100.4, 235) === 100, "clamped, whole frames");
}

start("the start: a drop on the first frames is no trim");
for (let f = -10; f <= 300; f++) {
  const got = dropStart(f, 235);
  const at = Math.min(205, Math.max(0, Math.round(f)));
  check(got === (at <= AUTO_NEAR ? null : at), `f=${f} → ${got}`);
}

start("the start: snap points");
{
  // A part at 4 s, words at 0.2 / 1.5 / 3.25 / 7.95 s (the last past the
  // room before the end), speech from 0.18 s.
  const pts = startSnaps(235, { parts: [4], words: [0.2, 1.5, 3.25, 7.95], voicedStart: 0.18 });
  check(eq(pts, [0, 5, 6, 45, 98, 120]), JSON.stringify(pts));
  check(eq(startSnaps(235), [0]), "nothing said: the first frame");
  check(eq(startSnaps(200, { words: [6.5, 6.6] }), [0]), "past 1.0 s before the end: none");
  check(eq(startSnaps(235, { words: [1.5, 1.51] }), [0, 45]), "once each");
}

start("the start: keys");
{
  const k = (key, shift, user, end = 235) => keyStart(key, shift, user, end);
  check(eq(k("ArrowRight", false, null), { start: START_MIN, nudge: true }), "→ from no trim: the first start kept");
  check(eq(k("ArrowRight", true, null), { start: 10, nudge: true }), "Shift → 10");
  check(eq(k("ArrowRight", false, 60), { start: 61, nudge: true }) && eq(k("ArrowLeft", false, 60), { start: 59, nudge: true }), "one frame");
  check(eq(k("ArrowLeft", false, 3), { start: null, nudge: true }), "← into the first frames: no trim");
  check(eq(k("ArrowLeft", true, 8), { start: null, nudge: true }), "Shift ← past the first frame: no trim");
  check(eq(k("ArrowRight", true, 200), { start: 205, nudge: true }), "→ holds 1.0 s before the end");
  check(eq(k("ArrowRight", false, null, 31), { start: null, nudge: true }), "no room for a start: none");
  check(eq(k("End", false, null), { start: 205, nudge: false }) && eq(k("End", false, 60, 200), { start: 170, nudge: false }), "End: the latest");
  for (const key of ["Home", "0", "Backspace"]) check(eq(k(key, false, 90), { start: null, nudge: false }), `${key}: no trim`);
  check(k("a", false, 90) === undefined && k("ArrowUp", false, 90) === undefined, "not its keys");
}

start("the end with a start: kept 1.0 s after it");
{
  const S = { ...MAIN, start: 100 };
  check(eq(endRange(S), { min: 130, max: 240 }), JSON.stringify(endRange(S)));
  check(clampEnd(100, S) === 130 && clampEnd(999, S) === 240, "clamped after the start");
  check(dropEnd(234, S) === null && autoFits(S), "the automatic end still fits: still automatic");
  check(eq(keyEnd("Home", false, 200, S), { end: 130, nudge: false }), "Home: 1.0 s after the start");
  check(eq(endSnaps(S, { words: [1, 4, 6] }), [180, 235]), "snaps only after the start's second");
  // A start 25 frames before the automatic cut: automatic would leave less
  // than a second, so it is never stored — the nearest end that fits is.
  const late = { ...MAIN, start: 210 };
  check(!autoFits(late) && endReset(late) === 240, `automatic is too close: ${endReset(late)}`);
  check(dropEnd(235, late) === 240 && dropEnd(238, late) === 240, "a drop near it: in range, never null");
  check(eq(keyEnd("0", false, 240, late), { end: 240, nudge: false }), "0: the nearest end that fits");
  // No start (absent or 0): every end helper as before.
  for (const f of [MAIN, INTRO, CLEAN]) {
    for (const z of [{ ...f, start: 0 }, { ...f }]) {
      check(eq(endRange(z), endRange(f)) && endReset(z) === null && autoFits(z), "range and automatic");
      for (let x = 0; x <= 300; x += 7) check(dropEnd(x, z) === dropEnd(x, f) && clampEnd(x, z) === clampEnd(x, f), `x=${x}`);
      check(eq(keyEnd("0", false, 200, z), { end: null, nudge: false }), "0 / Backspace: automatic");
    }
  }
}

for (const g of groups) console.log(`${g.bad ? "FAIL" : "ok  "} ${g.name} (${g.n})`);
console.log(failed ? `${failed} of ${total} FAILED` : `all ${total} cases agree`);
process.exit(failed ? 1 : 0);
