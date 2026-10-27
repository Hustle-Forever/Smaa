// Rebrand studio — the moving preview's QUICK STAND-IN for a draft made in
// house: each pattern at a moment, drawn roughly as the in-house engine
// draws it (backend/app/services/rebrand_harness/plate.py), until the exact
// video (POST /house/render — the very file Generate copies) is ready.
//
// The same time as the video (lib/rebrandTiming.ts is the server's timing()
// rule for rule) and the same camera (the scene's track, [a, b, tx, ty] per
// frame: frame 0's pixels to frame i's); the motions are approximations,
// and the stage says so ("Quick preview"):
//
//   fade           every line together (the same easing)
//   wipes, to_*    a hard edge where the engine's soft one is half-way
//   slide(_out)    the same slide, from the same edge
//   glitch         its fade and a sideways jitter (not the engine's slices)
//
// Plain TypeScript only (no enums, no path aliases, type-only imports).

import type { HarnessHouseLayer } from "./rebrandHarnessApi";
import type { Timing, TimingPhase } from "./rebrandTiming";

/** A wipe's soft edge, as a share of the ink box along its sweep (plate.WIPE_SOFT). */
export const WIPE_SOFT = 0.12;
/** The glitch's jump, of the ink box's shorter side (plate.GLITCH_SHIFT). */
export const GLITCH_SHIFT = 0.08;
const GLITCH_STEP = 2;
const GLITCH_FLICKER = 0.16;

/** kind -> [axis, sweeps from the far end] (plate.WIPES). */
export const WIPES: Readonly<Record<string, readonly ["x" | "y", boolean]>> = {
  left_to_right: ["x", false],
  right_to_left: ["x", true],
  top_to_bottom: ["y", false],
  bottom_to_top: ["y", true],
  to_left: ["x", false],
  to_right: ["x", true],
  to_top: ["y", false],
  to_bottom: ["y", true],
};

const clamp01 = (q: number) => (q < 0 ? 0 : q > 1 ? 1 : q);

/** The phase at scene time t and its progress (rebrandTiming.phase, the
 *  server's Timing.phase — restated here so this file needs no other at
 *  run time: the fixture check runs it straight in node). */
function phaseOf(tm: Timing, t: number): [TimingPhase, number] {
  if (t < tm.t_in) return ["before", 0];
  if (t >= tm.t_out) return ["after", 1];
  if (t < tm.t_full) return ["in", (t - tm.t_in) / (tm.t_full - tm.t_in)];
  if (t < tm.t_hold_end) return ["hold", 1];
  return ["out", (t - tm.t_hold_end) / (tm.t_out - tm.t_hold_end)];
}
/** easeOutExpo renormalised so e(0) = 0 and e(1) = 1 (icon_render._out_expo). */
export const outExpo = (q: number) => (1 - 2 ** (-10 * q)) / (1 - 2 ** -10);
export const inOutCubic = (q: number) => (q < 0.5 ? 4 * q ** 3 : 1 - (-2 * q + 2) ** 3 / 2);

/** How much of a layer shows at progress q of its entrance ("in") or its
 *  exit ("out"): 0 gone, 1 fully in (plate.shown_share). */
export function shownShare(direction: "in" | "out", q: number): number {
  const p = clamp01(q);
  return direction === "in" ? outExpo(p) : 1 - inOutCubic(p);
}

/** A small deterministic 0..1 from a key (not the engine's hash: the
 *  stand-in's glitch only looks like it). */
function hash01(...key: (string | number)[]): number {
  let h = 2166136261;
  const s = key.join("|");
  for (let i = 0; i < s.length; i++) {
    h ^= s.charCodeAt(i);
    h = Math.imul(h, 16777619);
  }
  return (h >>> 0) / 4294967296;
}

/** How a layer is drawn at one moment: nothing (null), or with `alpha`,
 *  moved (dx, dy) art px, and clipped to `clip` (art px) when a wipe is on
 *  its way. */
export interface LayerFrame {
  alpha: number;
  dx: number;
  dy: number;
  clip: { x0: number; y0: number; x1: number; y1: number } | null;
}

/** How far a sliding layer is moved (art px) with `hidden` (0..1) of its
 *  slide still to go (plate.slide_offset). */
export function slideOffset(
  edge: string,
  box: readonly [number, number, number, number],
  margin: readonly [number, number],
  size: readonly [number, number],
  hidden: number,
): [number, number] {
  const [x0, y0, x1, y1] = box;
  const [mW, mH] = margin;
  const [W, H] = size;
  if (edge === "right") return [hidden * (W - (x0 - mW)), 0];
  if (edge === "top") return [0, -hidden * (y1 - mH)];
  if (edge === "bottom") return [0, hidden * (H - (y0 - mH))];
  return [-hidden * (x1 - mW), 0];
}

/** The layer at scene time t (frame i of a `size` frame), or null when it
 *  does not show. */
export function layerAt(
  layer: Pick<HarnessHouseLayer, "timing" | "ink_box" | "margin" | "edge" | "seed">,
  t: number,
  i: number,
  size: readonly [number, number],
): LayerFrame | null {
  const tm: Timing | null = layer.timing;
  if (!tm) return null;
  const [ph, q] = phaseOf(tm, t);
  if (ph === "before" || ph === "after") return null;
  if (ph === "hold") return { alpha: 1, dx: 0, dy: 0, clip: null };
  const kind = ph === "in" ? tm.entrance : tm.exit;
  const direction = ph === "in" ? "in" : "out";
  const e = shownShare(direction, q);
  const box = layer.ink_box;
  if (kind === "none") return { alpha: 1, dx: 0, dy: 0, clip: null };
  if (kind === "slide" || kind === "slide_out") {
    const [dx, dy] = slideOffset(layer.edge, box, layer.margin, size, 1 - e);
    return { alpha: 1, dx, dy, clip: null };
  }
  if (Object.prototype.hasOwnProperty.call(WIPES, kind)) {
    if (e <= 0) return null;
    if (e >= 1) return { alpha: 1, dx: 0, dy: 0, clip: null };
    const [axis, high] = WIPES[kind];
    const [x0, y0, x1, y1] = box;
    const lo = axis === "x" ? x0 : y0;
    const hi = axis === "x" ? x1 : y1;
    const span = Math.max(1, hi - lo);
    const soft = Math.max(1, WIPE_SOFT * span);
    // Where the engine's soft edge is half-way.
    const edge = lo - soft + e * (span + 2 * soft) - soft / 2;
    const cut = high ? lo + hi - edge : edge;
    const clip =
      axis === "x"
        ? high
          ? { x0: cut, y0, x1, y1 }
          : { x0, y0, x1: cut, y1 }
        : high
          ? { x0, y0: cut, x1, y1 }
          : { x0, y0, x1, y1: cut };
    return { alpha: 1, dx: 0, dy: 0, clip };
  }
  if (kind === "glitch") {
    const p = direction === "in" ? clamp01(q) : 1 - clamp01(q);
    const amount = 1 - p;
    const step = Math.floor(i / GLITCH_STEP);
    if (p <= 0 || (amount > 0.5 && hash01(layer.seed, "flicker", step) < GLITCH_FLICKER * amount)) return null;
    const L = Math.max(1, Math.min(box[2] - box[0], box[3] - box[1]));
    const dx = (hash01(layer.seed, "dx", step) - 0.5) * 2 * GLITCH_SHIFT * L * amount;
    return { alpha: p, dx, dy: 0, clip: null };
  }
  // fade — and anything the engine draws as a fade
  if (e <= 0) return null;
  return { alpha: e, dx: 0, dy: 0, clip: null };
}

/** The camera at scene time t ([a, b, tx, ty]: frame 0's pixels to that
 *  frame's), or the identity without one. */
export function cameraAt(
  track: { fps: number; m: [number, number, number, number][] } | null | undefined,
  t: number,
): [number, number, number, number] {
  if (!track || !track.m.length) return [1, 0, 0, 0];
  const i = Math.min(track.m.length - 1, Math.max(0, Math.round(t * track.fps)));
  return track.m[i];
}
