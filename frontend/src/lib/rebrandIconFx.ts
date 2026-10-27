// Rebrand studio — an icon's entrance and exit as numbers, ported frame for
// frame from the server (backend/app/services/rebrand_harness/icon_render.py
// `fx_progress`, which the run stamps every frame with). The moving preview
// uses it so fade, pop and slide move exactly as the video will; the glitch's
// torn slices and a chip's grow are drawn by the canvas's own hand (their
// numbers are exact).
// A shared fixture (backend/tests/fixtures/rebrand_icon_fx.json, checked by
// the backend test and by frontend/scripts/check-rebrand-icon-fx.mjs) keeps
// the two from drifting.
//
// Frames, not seconds: the server quantises every time to a frame with
// Python's round() (halves to even), so this does too (pyRound).
//
// Plain TypeScript only (no enums, no path aliases): the check script runs
// this file straight in node.

/** The motions the renderer draws (icon_render.ENTRANCES). */
export const MOTIONS = ["none", "fade", "pop", "slide", "glitch", "grow"] as const;
export type Motion = (typeof MOTIONS)[number];

/** "grow" (a chip's own): its circle pops over the first GROW_POP of the way
 *  in, then the pill unfolds, its words showing from GROW_WORDS of the
 *  unfold on; going, the mirror (icon_render.GROW_POP / GROW_WORDS). */
export const GROW_POP = 0.4;
export const GROW_WORDS = 0.4;

/** The motion an icon of `style` is drawn with: any style but the chip grows
 *  as a pop (icon_render.motion_for). */
export function motionFor(style: string | null | undefined, kind: string | null | undefined): string | null {
  return kind === "grow" && style !== "chip" ? "pop" : kind ?? null;
}

/** Where one frame is in an icon's life (icon_render.FxProgress). */
export interface FxProgress {
  /** The motion this frame is drawn with: the entrance while it comes in
   *  (and holds), the exit while it goes. */
  kind: string;
  phase: "in" | "hold" | "out";
  /** fade / pop: its opacity. */
  alpha: number;
  /** pop: its size, about its centre. */
  scale: number;
  /** slide: the share of the travel still to go (0 = at rest). */
  slide: number;
  /** glitch: how torn it is, and the share of its slices in. */
  amount: number;
  reveal: number;
  /** grow: how far the pill is open from its circle (0: the circle alone),
   *  and its words' strength. */
  unfold: number;
  words: number;
}

/** Python's round(): halves go to the even neighbour. */
export function pyRound(x: number): number {
  if (Math.abs(x % 1) === 0.5) return 2 * Math.round(x / 2);
  return Math.round(x);
}

const clamp01 = (t: number) => (t < 0 ? 0 : t > 1 ? 1 : t);
const inOutCubic = (t: number) => (t < 0.5 ? 4 * t ** 3 : 1 - (-2 * t + 2) ** 3 / 2);
const outBack = (t: number) => {
  const c1 = 1.70158;
  const c3 = c1 + 1;
  return 1 + c3 * (t - 1) ** 3 + c1 * (t - 1) ** 2;
};
/** easeOutExpo renormalised so e(0) = 0 and e(1) = 1 exactly. */
const outExpo = (t: number) => (1 - 2 ** (-10 * t)) / (1 - 2 ** -10);

/** A motion as the renderer names it ("auto" and none given: the glitch);
 *  one it does not draw is null. */
export function motionOf(kind: string | null | undefined): Motion | null {
  const k = !kind || kind === "auto" ? "glitch" : kind;
  return (MOTIONS as readonly string[]).includes(k) ? (k as Motion) : null;
}

function progress(kind: string, phase: FxProgress["phase"], p: Partial<FxProgress> = {}): FxProgress {
  return { kind, phase, alpha: 1, scale: 1, slide: 0, amount: 0, reveal: 1, unfold: 1, words: 1, ...p };
}

/** Frame `f` (since the icon's first frame) of an icon on screen for `end`
 *  frames, coming in over `fin` and going over `fout`: null when it is not
 *  on screen. `exit` null: the entrance mirrored. */
export function fxProgress(
  entrance: string | null | undefined,
  f: number,
  end: number,
  fin: number,
  fout: number,
  exit: string | null = null,
): FxProgress | null {
  const enter = motionOf(entrance) ?? "glitch";
  const leave = exit == null ? enter : motionOf(exit) ?? enter;
  if (f < 0 || f >= end) return null;
  const phase: FxProgress["phase"] = f < fin ? "in" : f >= end - fout ? "out" : "hold";
  const kind = phase === "out" ? leave : enter;
  if (kind === "none" || phase === "hold") return progress(kind, phase);
  if (kind === "glitch") {
    const t = phase === "in" ? (f + 1) / fin : (end - f) / fout;
    if (t >= 1) return progress(kind, phase, { amount: 0, reveal: 1 });
    return progress(kind, phase, { amount: 1 - inOutCubic(clamp01(t)), reveal: clamp01(t / 0.6) });
  }
  const leaving = phase === "out";
  const p = clamp01(leaving ? (end - f - 1) / fout : (f + 1) / fin);
  if (kind === "grow") {
    // The circle pops, then the pill unfolds and its words come in.
    if (p <= GROW_POP) {
      const q = p / GROW_POP;
      return progress(kind, phase, { alpha: clamp01(q * 2), scale: 0.7 + 0.3 * (leaving ? q : outBack(q)), unfold: 0, words: 0 });
    }
    const u = (p - GROW_POP) / (1 - GROW_POP);
    return progress(kind, phase, { unfold: leaving ? u : outExpo(u), words: clamp01((u - GROW_WORDS) / (1 - GROW_WORDS)) });
  }
  if (kind === "fade") return progress(kind, phase, { alpha: p });
  if (kind === "pop") return progress(kind, phase, { alpha: clamp01(p * 2), scale: 0.7 + 0.3 * (leaving ? p : outBack(p)) });
  const e = leaving ? p ** 3 : outExpo(p);
  return progress(kind, phase, { slide: 1 - e });
}

/** An icon's frames, as the run counts them (icon_render.fx): `tRel` s
 *  since it starts coming in, on screen `span` s, in over `inSec`, out over
 *  `outSec`. */
export function framesOf(tRel: number, span: number, inSec: number, outSec: number, fps: number) {
  return {
    f: pyRound(tRel * fps),
    end: pyRound(span * fps),
    fin: Math.max(1, pyRound(inSec * fps)),
    fout: Math.max(1, pyRound(outSec * fps)),
  };
}

export type Edge = "left" | "right" | "top" | "bottom";

/** The frame edge a slide comes from (the one nearest the body box, px) and
 *  how far the body must move to be fully outside it (icon_render.nearest_edge). */
export function nearestEdge(x: number, y: number, w: number, h: number, W: number, H: number): { edge: Edge; travel: number } {
  const near: [Edge, number][] = [
    ["left", x],
    ["right", W - (x + w)],
    ["top", y],
    ["bottom", H - (y + h)],
  ];
  // Ties by name, as the server's min over (distance, edge).
  near.sort((a, b) => a[1] - b[1] || (a[0] < b[0] ? -1 : a[0] > b[0] ? 1 : 0));
  const edge = near[0][0];
  const gaps: Record<Edge, number> = { left: x + w, right: W - x, top: y + h, bottom: H - y };
  return { edge, travel: Math.trunc(gaps[edge]) };
}

/** A slide's offset (px) at `slide` share of `travel` to go, from `edge`. */
export function slideOffset(slide: number, travel: number, edge: Edge): { dx: number; dy: number } {
  const off = pyRound(slide * travel);
  return edge === "left" ? { dx: -off, dy: 0 } : edge === "right" ? { dx: off, dy: 0 } : edge === "top" ? { dx: 0, dy: -off } : { dx: 0, dy: off };
}
