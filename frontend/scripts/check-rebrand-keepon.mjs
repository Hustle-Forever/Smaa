// Checks src/lib/rebrandTransform.ts — partlyInside, how far a pattern may be
// dragged off the frame — over a grid: box positions −3…3, sizes, turns in
// 7.5° steps plus odd ones, both frame shapes.
//   * unturned, it is exactly the rule it always was (15% of the width and
//     of the height stay on);
//   * turned, a real piece of the pattern is always on the frame (a pattern
//     turned 90° or 45° can no longer slip all the way off);
//   * a pattern already on the frame never moves, and holding it again
//     changes nothing (no jitter while dragging along an edge).
//
//   node --experimental-strip-types scripts/check-rebrand-keepon.mjs
//
// (node 22.6+.) Prints a line per group and exits 1 on any failure.

import { partlyInside, shownShare, turnedCorners } from "../src/lib/rebrandTransform.ts";

let failed = 0;
let total = 0;
const fail = (msg) => {
  failed += 1;
  if (failed <= 25) console.log(`FAIL ${msg}`);
};
const near = (a, b, tol = 1e-9) => Math.abs(a - b) <= tol;
const sameBox = (a, b, tol = 1e-9) => near(a.x, b.x, tol) && near(a.y, b.y, tol) && near(a.w, b.w, tol) && near(a.h, b.h, tol);
const old = (box, keep = 0.15) => {
  const kw = box.w * keep;
  const kh = box.h * keep;
  return {
    ...box,
    x: Math.min(Math.max(box.x, kw - box.w), 1 - kw),
    y: Math.min(Math.max(box.y, kh - box.h), 1 - kh),
  };
};

const FRAMES = [
  { w: 1080, h: 1920 },
  { w: 1920, h: 1080 },
];
const SIZES = [
  [0.1, 0.05],
  [0.3, 0.1],
  [0.05, 0.6],
  [0.5, 0.5],
  [1, 0.2],
  [2.5, 0.4],
  [3, 3],
];
const POS = [];
for (let v = -3; v <= 3.0001; v += 0.125) POS.push(Math.round(v * 1000) / 1000);
const TURNS = [];
for (let r = -180; r < 180; r += 7.5) TURNS.push(r);
TURNS.push(1, -2, 33, 91, 179.5);

// --- unturned: the old rule, to the bit -------------------------------------------------
{
  let n = 0;
  for (const [w, h] of SIZES)
    for (const x of POS)
      for (const y of POS) {
        const box = { x, y, w, h };
        const want = old(box);
        for (const frame of FRAMES)
          for (const rotate of [0, 360, -360]) {
            n += 1;
            const got = partlyInside(box, 0.15, { rotate, frame });
            if (!sameBox(got, want, 0)) fail(`unturned ${JSON.stringify(box)} r=${rotate}: ${JSON.stringify(got)} != ${JSON.stringify(want)}`);
          }
        n += 1;
        if (!sameBox(partlyInside(box), want, 0)) fail(`no turn given ${JSON.stringify(box)}`);
      }
  total += n;
  console.log(`unturned is the old rule: ${n} cases`);
}

// --- turned: a real piece always shows; stable; on-frame boxes stay put ---------------------
{
  let n = 0;
  let walked = 0;
  for (const frame of FRAMES)
    for (const [w, h] of SIZES)
      for (const x of POS)
        for (const y of POS)
          for (const rotate of TURNS) {
            if (rotate % 360 === 0) continue;
            n += 1;
            const box = { x, y, w, h };
            const got = partlyInside(box, 0.15, { rotate, frame });
            if (!near(got.w, w, 0) || !near(got.h, h, 0)) fail(`resized ${JSON.stringify(box)} r=${rotate}`);
            const share = shownShare(got, rotate, frame);
            // A quarter turn fills its outline: 15% × 15% of it at the least.
            const least = rotate % 90 === 0 ? 0.0225 - 1e-9 : 0.02 - 1e-6;
            if (!(share >= least)) fail(`hidden ${JSON.stringify(box)} r=${rotate} f=${frame.w}x${frame.h}: shows ${share.toFixed(4)}`);
            const again = partlyInside(got, 0.15, { rotate, frame });
            if (!sameBox(again, got, 1e-6)) fail(`not stable ${JSON.stringify(box)} r=${rotate}: ${JSON.stringify(got)} -> ${JSON.stringify(again)}`);
            if (!sameBox(got, box, 0)) walked += 1;
            // Wholly on the frame: never moved.
            const cs = turnedCorners(box, rotate, frame);
            const inside = cs.every(([cx, cy]) => cx >= 0 && cx <= 1 && cy >= 0 && cy <= 1);
            if (inside && !sameBox(got, box, 0)) fail(`moved an on-frame box ${JSON.stringify(box)} r=${rotate}`);
          }
  total += n;
  console.log(`turned always shows a piece, stable, on-frame untouched: ${n} cases (${walked} held back)`);
}

// --- the reported case: 1×, 90°, dragged far up -----------------------------------------------
{
  const frame = { w: 1080, h: 1920 };
  const box = { x: 0.2, y: -2, w: 0.6, h: 0.08 };
  const got = partlyInside(box, 0.15, { rotate: 90, frame });
  total += 1;
  const share = shownShare(got, 90, frame);
  if (!(share >= 0.0225 - 1e-9)) fail(`90° dragged up still hides: ${share}`);
  console.log(`90° dragged up keeps ${(share * 100).toFixed(1)}% on the frame`);
}

console.log(failed ? `\n${failed} FAILED of ${total}` : `\nall ${total} agree`);
process.exit(failed ? 1 : 0);
