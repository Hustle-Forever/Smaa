import React from "react";

import { BEAD_PERIOD, BEAD_STRING, BEAD_WINDOW_START, BLINK_SET, glyphPath, glyphSize } from "./beads";
import type { BeadLine } from "./geometry";
import { FrameSvg } from "./svg";
import { MOTION } from "./tokens";

const mod = (a: number, n: number) => ((a % n) + n) % n;

/**
 * One two-row bead string on a slab edge, drawn as SVG vectors in the
 * string's own rotated frame (see BeadLine), so every glyph is a smooth,
 * anti-aliased almond at any size and angle.
 *
 * Motion:
 *  - slides +u at 0.30 px/frame (@720) inside a window one period long and
 *    wraps. A glyph shows only while it lies WHOLLY inside the window (and
 *    inside `line.span`, when given): it toggles off whole as it crosses the
 *    far end and back on whole at the near end, never clipped into a
 *    half-glyph fragment;
 *  - pops on glyph by glyph from the far (+u) end at 17 px/frame, whole glyphs
 *    only, no fade (`popStart`; null = already on);
 *  - blinks: BLINK_SET is off on frames 8-16 of every 25-frame cycle;
 *  - hard cut: nothing from `cutAt` on.
 * `dx, dy` carry the string along with the slab it rides.
 */
export const BeadTrail: React.FC<{
  W: number;
  H: number;
  line: BeadLine;
  s: number;
  frame: number;
  color: string;
  dx?: number;
  dy?: number;
  popStart?: number | null;
  cutAt?: number | null;
  blink?: boolean;
}> = ({ W, H, line, s, frame, color, dx = 0, dy = 0, popStart = null, cutAt = null, blink = true }) => {
  if (cutAt !== null && frame >= cutAt) return null;
  if (popStart !== null && frame < popStart) return null;

  const P = BEAD_PERIOD * s;
  const w0 = BEAD_WINDOW_START * s;
  const lo = Math.max(w0, line.span ? line.span[0] : -Infinity);
  const hi = Math.min(w0 + P, line.span ? line.span[1] : Infinity);
  const slide = MOTION.beadSlide * s * frame;
  const revealed = popStart === null ? Infinity : MOTION.beadPopSpeed * s * (frame - popStart + 1);
  const cycle = mod(frame, MOTION.blinkCycle);
  const blinking = blink && cycle >= MOTION.blinkOff[0] && cycle <= MOTION.blinkOff[1];

  let d = "";
  BEAD_STRING.forEach((g, idx) => {
    const half = glyphSize(g.kind, s)[0] / 2;
    const u = w0 + mod(g.u * s + slide - w0, P);
    if (u - half < lo || u + half > hi) return;
    if (w0 + P - u > revealed) return;
    if (blinking && BLINK_SET.has(idx)) return;
    d += glyphPath(g.kind, u, line.rows[g.row], s);
  });
  if (!d) return null;

  const { x, y } = line.origin;
  return (
    <FrameSvg W={W} H={H}>
      <path transform={`translate(${x + dx} ${y + dy}) rotate(${line.deg})`} d={d} fill={color} />
    </FrameSvg>
  );
};
