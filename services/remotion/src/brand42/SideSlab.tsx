import React from "react";

import type { BarsGeometry } from "./geometry";
import { beyond, FrameSvg, pts } from "./svg";

/**
 * Violet A slab anchored bottom-right in the bottom bar: a 45-degree top edge
 * from (W, yTop) down-left to (W - ws, yTop + ws), then a vertical edge down.
 * When the bar is tall enough it also gets r128's LOWER 45-degree cut
 * (x + y = slabLowerC), which turns the wedge into r128's parallelogram band
 * with a charcoal corner under it; otherwise it runs to the frame bottom.
 * Every edge extends past the canvas so the diagonal slide-in/out and the
 * idle sink never open a gap.
 */
export const SideSlab: React.FC<{ g: BarsGeometry; dx: number; dy: number; color: string }> = ({ g, dx, dy, color }) => {
  const E = beyond(g.W, g.H);
  const left = g.W - g.ws;
  const poly: Array<readonly [number, number]> = [
    [g.W + E, g.yTop - E],
    [left, g.yTop + g.ws],
  ];
  if (g.slabLowerC === null) {
    poly.push([left, g.H + E], [g.W + E, g.H + E]);
  } else {
    // Down the vertical edge to the lower cut, then up-right along it.
    poly.push([left, g.slabLowerC - left], [g.W + E, g.slabLowerC - g.W - E]);
  }
  return (
    <FrameSvg W={g.W} H={g.H}>
      <polygon points={pts(poly, dx, dy)} fill={color} />
    </FrameSvg>
  );
};
