import { Easing } from "remotion";

/**
 * Remotion's exponential easings do not hit their endpoints: Easing.exp(0) is
 * 2^-10, so `interpolate(..., { easing: Easing.out(Easing.exp) })` parks a
 * slab 0.1% short of home FOREVER (a third of a pixel on the top band, but a
 * permanent one), and an in(exp) exit starts with a 0.1% jump. Every curve used
 * here is renormalised so e(0) = 0 and e(1) = 1 exactly.
 */
type Curve = (t: number) => number;

const normalise = (curve: Curve): Curve => {
  const a = curve(0);
  const b = curve(1);
  return (t) => (curve(t) - a) / (b - a);
};

export const EASE = {
  outExp: normalise(Easing.out(Easing.exp)),
  inExp: normalise(Easing.in(Easing.exp)),
  outPoly5: normalise(Easing.out(Easing.poly(5))),
  outSin: normalise(Easing.out(Easing.sin)),
  inOutCubic: normalise(Easing.inOut(Easing.cubic)),
  linear: (t: number) => t,
} as const;

const clamp01 = (t: number) => (t < 0 ? 0 : t > 1 ? 1 : t);

/**
 * Eased 0..1 progress of a motion that starts at frame `start` and takes
 * `frames` frames: 0 at `start`, 1 at `start + frames` and after.
 */
export const ramp = (frame: number, start: number, frames: number, curve: Curve): number =>
  curve(clamp01((frame - start) / Math.max(1, frames)));
