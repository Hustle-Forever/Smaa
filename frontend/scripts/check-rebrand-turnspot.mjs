// Checks src/lib/rebrandTransform.ts — where a pattern's turn button goes
// (turnButtonSpot) and how a turn lands (snapTurn) — over an exhaustive grid:
// box positions −0.85…1.85, scales 0.5–3, turns in 15° steps, both canvas
// shapes, a phone's and a tiny canvas, full-screen (pan) patterns, with and
// without each of the stage's avoid zones. For every case the button (its
// disc 8 px in, its 44 px hit area off every zone) and its angle pill lie
// wholly on the canvas, clear of every zone, and the button is 44 px or more
// from the pivot. Where the function says that can't be done (`level` > 0),
// a brute-force search over the canvas (2 px) must agree that no point keeps
// the rules it dropped.
//
//   node --experimental-strip-types scripts/check-rebrand-turnspot.mjs
//
// (node 22.6+.) Prints a line per group and exits 1 on any failure.

import { snapTurn, onTurnStop, turnButtonSpot, TURN_BUTTON, TURN_PILL } from "../src/lib/rebrandTransform.ts";

let failed = 0;
let total = 0;
const fail = (msg) => {
  failed += 1;
  if (failed <= 25) console.log(`FAIL ${msg}`);
};

// --- how a turn lands ----------------------------------------------------------------
{
  const cases = [
    // a drag: pulled onto a multiple of 45° within 4°
    [44, { magnet: true }, 45],
    [41, { magnet: true }, 45],
    [49, { magnet: true }, 45],
    [40.9, { magnet: true }, 40.9],
    [49.1, { magnet: true }, 49.1],
    [3, { magnet: true }, 0],
    [-4, { magnet: true }, 0],
    [87, { magnet: true }, 90],
    [133, { magnet: true }, 135],
    [178, { magnet: true }, 180],
    [-178, { magnet: true }, 180],
    [225, { magnet: true }, -135],
    [20, { magnet: true }, 20],
    // Shift: 15° steps
    [22, { step: true }, 15],
    [23, { step: true }, 30],
    [-8, { step: true }, -15],
    [44, { step: true, magnet: true }, 45],
    // [ / ] and the arrows: exactly as asked, no pull (1° at 0° stays 1°)
    [1, { magnet: false }, 1],
    [-1, {}, -1],
    [44, {}, 44],
    [91, { magnet: false }, 91],
    // always in (−180, 180]
    [181, {}, -179],
    [-180, {}, 180],
    [360, {}, 0],
    [540, {}, 180],
  ];
  let bad = 0;
  for (const [deg, opts, want] of cases) {
    total += 1;
    const got = snapTurn(deg, opts);
    if (Math.abs(got - want) > 1e-9) {
      bad += 1;
      fail(`snapTurn(${deg}, ${JSON.stringify(opts)}) = ${got}, want ${want}`);
    }
  }
  for (const [deg, want] of [[45, true], [0, true], [-135, true], [180, true], [44, false], [15, false]]) {
    total += 1;
    if (onTurnStop(deg) !== want) {
      bad += 1;
      fail(`onTurnStop(${deg}) = ${!want}`);
    }
  }
  console.log(`${bad ? "BAD " : "ok  "} how a turn lands (${cases.length + 6})`);
}

// --- where the button goes -------------------------------------------------------------
const R = TURN_BUTTON.disc / 2;
const HALF = TURN_BUTTON.hit / 2;
const IN = TURN_BUTTON.inset;
const EPS = 1e-6;
const overlaps = (a, b) =>
  a.x < b.x + b.w - EPS && b.x < a.x + a.w - EPS && a.y < b.y + b.h - EPS && b.y < a.y + a.h - EPS;

/** The stage's avoid zones on a canvas W×H, as StudioStage measures them
 *  (chips 24 px tall, the Play pills 28 px, the pan pill 28×76). */
function zonesFor(W, H) {
  const chip = Math.min(150, W - 16);
  const band = Math.min(180, W - 16);
  return {
    "top-left chip": { x: 8, y: 8, w: chip, h: 24 },
    "bottom-left chip": { x: 8, y: H - 8 - 24, w: chip, h: 24 },
    "top-centre chip": { x: W / 2 - band / 2, y: 8, w: band, h: 24 },
    "Next's Play": { x: W / 2 - 34, y: H - 12 - 28, w: 68, h: 28 },
    "Play / Still": { x: W / 2 - 38, y: H - 12 - 28, w: 76, h: 28 },
    "pan zoom pill": { x: W - 16 - 28, y: H / 2 - 38, w: 28, h: 76 },
  };
}

/** Which rules a spot keeps: the most `level` it satisfies (0 = all). */
function levelOf(W, H, zones, pivot, x, y, pill) {
  const inside = x - R >= IN - EPS && x + R <= W - IN + EPS && y - R >= IN - EPS && y + R <= H - IN + EPS;
  if (!inside) return 4;
  const hit = { x: x - HALF, y: y - HALF, w: 2 * HALF, h: 2 * HALF };
  const clear = !zones.some((z) => overlaps(hit, z));
  const far = Math.hypot(x - pivot.x, y - pivot.y) >= TURN_BUTTON.pivot - EPS;
  const p = { x: pill.x - TURN_PILL.w / 2, y: pill.y - TURN_PILL.h / 2, w: TURN_PILL.w, h: TURN_PILL.h };
  const pillIn = p.x >= -EPS && p.y >= -EPS && p.x + p.w <= W + EPS && p.y + p.h <= H + EPS && !overlaps(p, hit);
  const pillClear = pillIn && !zones.some((z) => overlaps(p, z));
  if (clear && far && pillClear) return 0;
  if (clear && far && pillIn) return 1;
  if (clear && pillIn) return 2;
  return 3;
}

/** Brute force: does any point on the canvas (every 2 px, with each of the
 *  pill's four places) keep the rules of `level`? */
function feasible(W, H, zones, pivot, level) {
  const off = R + TURN_PILL.gap;
  const pills = [[0, off + TURN_PILL.h / 2], [0, -(off + TURN_PILL.h / 2)], [off + TURN_PILL.w / 2, 0], [-(off + TURN_PILL.w / 2), 0]];
  for (let x = IN + R; x <= W - IN - R + EPS; x += 2)
    for (let y = IN + R; y <= H - IN - R + EPS; y += 2)
      for (const [ox, oy] of pills) if (levelOf(W, H, zones, pivot, x, y, { x: x + ox, y: y + oy }) <= level) return true;
  return false;
}

const CANVASES = [
  { name: "9:16 stage", w: 405, h: 720, frame: { w: 720, h: 1280 } },
  { name: "16:9 stage", w: 960, h: 540, frame: { w: 1280, h: 720 } },
  { name: "9:16 phone", w: 225, h: 400, frame: { w: 720, h: 1280 } },
  { name: "tiny (phone, a view pushed over the rail)", w: 95, h: 169, frame: { w: 720, h: 1280 } },
];
// A strip down a side, a band of beads, a squarish mark (unscaled, fractions).
const BOXES = [
  { name: "strip", w: 0.25, h: 0.6 },
  { name: "band", w: 0.6, h: 0.1 },
  { name: "mark", w: 0.2, h: 0.12 },
];
const SCALES = [0.5, 1, 1.5, 2, 3];
const POS = [];
for (let v = -0.85; v <= 1.85 + 1e-9; v += 0.15) POS.push(Math.round(v * 100) / 100);
const TURNS = [];
for (let t = -165; t <= 180; t += 15) TURNS.push(t);

const byGroup = new Map();
const note = (group, ok) => {
  const g = byGroup.get(group) ?? { n: 0, bad: 0, levels: [0, 0, 0, 0] };
  g.n += 1;
  if (!ok) g.bad += 1;
  byGroup.set(group, g);
  return g;
};
const oracleMemo = new Map();

function check(group, c, zoneNames, box, rot, pan) {
  total += 1;
  const all = zonesFor(c.w, c.h);
  const zones = zoneNames.map((n) => all[n]);
  const spot = turnButtonSpot(box, rot, c.frame, { w: c.w, h: c.h }, zones, { pan });
  const pivot = { x: (box.x + box.w / 2) * c.w, y: (box.y + box.h / 2) * c.h };
  const got = levelOf(c.w, c.h, zones, pivot, spot.x, spot.y, spot.pill);
  const g = note(group, true);
  g.levels[Math.min(3, spot.level)] += 1;
  const where = `${c.name} ${pan ? "pan" : ""} box=${JSON.stringify(box)} rot=${rot} zones=[${zoneNames.join(", ")}]`;
  if (!Number.isFinite(spot.x) || !Number.isFinite(spot.y) || got > 3) {
    g.bad += 1;
    return fail(`${group}: off the canvas (${spot.x}, ${spot.y}) — ${where}`);
  }
  if (got > spot.level) {
    g.bad += 1;
    return fail(`${group}: says level ${spot.level} but keeps only ${got} at (${spot.x.toFixed(1)}, ${spot.y.toFixed(1)}) — ${where}`);
  }
  if (spot.level > 0) {
    // It dropped a rule: no point on the canvas may keep one more.
    const key = `${c.name}|${zoneNames.join(",")}|${Math.round(pivot.x * 2)}|${Math.round(pivot.y * 2)}|${spot.level}`;
    let could = oracleMemo.get(key);
    if (could === undefined) {
      could = feasible(c.w, c.h, zones, pivot, spot.level - 1);
      oracleMemo.set(key, could);
    }
    if (could) {
      g.bad += 1;
      fail(`${group}: fell back to level ${spot.level}, but a point keeps level ${spot.level - 1} — ${where}`);
    }
  }
}

const ZONE_NAMES = Object.keys(zonesFor(100, 100));
const ZONE_SETS = [[], ...ZONE_NAMES.map((n) => [n]), ZONE_NAMES];

for (const c of CANVASES) {
  for (const zs of ZONE_SETS) {
    const group = `${c.name} · ${zs.length === 0 ? "no zones" : zs.length === ZONE_NAMES.length ? "every zone" : zs[0]}`;
    for (const b of BOXES)
      for (const s of SCALES)
        for (const x of POS)
          for (const y of POS)
            for (const t of TURNS) check(group, c, zs, { x, y, w: b.w * s, h: b.h * s }, t, false);
    // A full-screen pattern: its composition (the frame, or larger), zoomed
    // and moved, turned.
    for (const s of [1, 1.5, 2, 3])
      for (const dx of [-0.4, -0.2, 0, 0.2, 0.4])
        for (const dy of [-0.4, 0, 0.4])
          for (const t of TURNS) check(`${group} · pan`, c, zs, { x: 0.5 - s / 2 + dx, y: 0.5 - s / 2 + dy, w: s, h: s }, t, true);
  }
}

// --- the button never sits on the pattern's own side handles when a side fits -------
// (and below the middle is the first choice, as Canva has it)
{
  const c = CANVASES[0];
  const spot = turnButtonSpot({ x: 0.35, y: 0.3, w: 0.3, h: 0.17 }, 0, c.frame, { w: c.w, h: c.h }, []);
  total += 1;
  const box = { x: 0.35 * c.w, y: 0.3 * c.h, w: 0.3 * c.w, h: 0.17 * c.h };
  const want = { x: box.x + box.w / 2, y: box.y + box.h + TURN_BUTTON.gap + R };
  if (spot.side !== "below" || Math.abs(spot.x - want.x) > 1e-6 || Math.abs(spot.y - want.y) > 1e-6)
    fail(`a box in the open: ${JSON.stringify(spot)}, want below its middle at ${JSON.stringify(want)}`);
  // Near the bottom: above instead; its pill above it too.
  const up = turnButtonSpot({ x: 0.35, y: 0.8, w: 0.3, h: 0.17 }, 0, c.frame, { w: c.w, h: c.h }, []);
  total += 1;
  if (up.side !== "above" || !(up.pill.y < up.y)) fail(`a box at the foot: ${JSON.stringify(up)}, want above with the pill above`);
  // A full-screen pattern: bottom-right, 12 px in.
  const pan = turnButtonSpot({ x: 0, y: 0, w: 1, h: 1 }, 0, c.frame, { w: c.w, h: c.h }, [], { pan: true });
  total += 1;
  if (pan.side !== "corner" || Math.abs(pan.x - (c.w - 12 - R)) > 1e-6 || Math.abs(pan.y - (c.h - 12 - R)) > 1e-6)
    fail(`a full-screen pattern: ${JSON.stringify(pan)}, want bottom-right 12 px in`);
  // At the top, the pill goes below the button.
  const top = turnButtonSpot({ x: 0.35, y: -0.1, w: 0.3, h: 0.08 }, 0, c.frame, { w: c.w, h: c.h }, []);
  total += 1;
  if (!(top.y - R < TURN_PILL.top) || !(top.pill.y > top.y)) fail(`a button in the top band: ${JSON.stringify(top)}, want its pill below`);
  console.log(`ok   the usual places (4)`);
}

for (const [group, g] of byGroup) {
  console.log(
    `${g.bad ? "BAD " : "ok  "} ${group} (${g.n}; level 0: ${g.levels[0]}, 1: ${g.levels[1]}, 2: ${g.levels[2]}, 3: ${g.levels[3]})`,
  );
}
console.log(failed ? `${failed} of ${total} cases FAILED` : `all ${total} cases agree`);
process.exit(failed ? 1 : 0);
