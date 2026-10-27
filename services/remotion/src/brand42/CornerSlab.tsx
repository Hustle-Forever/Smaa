import React from "react";

import type { FullGeometry } from "./geometry";
import { beyond, FrameSvg, pts } from "./svg";

/** Corner slab outline {x >= slabLeft, x + y >= cornerC, y <= hs}, shifted right by dx. */
export function cornerSlabPoints(g: FullGeometry, dx: number): Array<readonly [number, number]> {
  const E = beyond(g.W, g.H);
  const d = g.cornerC - g.slabLeft; // where the cut meets the vertical edge
  const poly: Array<readonly [number, number]> = [
    [g.cornerC + E, -E],
    [g.W + E, -E],
    [g.W + E, g.hs],
  ];
  if (d >= g.hs) poly.push([g.cornerC - g.hs, g.hs]);
  else poly.push([g.slabLeft, g.hs], [g.slabLeft, d]);
  return poly.map(([x, y]) => [x + dx, y] as const);
}

/**
 * "full" layout, top-right: Violet B slab carrying the logo. Its 45-degree
 * cut runs down-left from (cornerC, 0); below it a vertical edge on the face
 * zone's right side (the r128 side-slab profile turned to the corner), so
 * the slab can never reach into the face zone. Extends past the top and
 * right edges for the horizontal slide.
 */
export const CornerSlab: React.FC<{ g: FullGeometry; dx: number; color: string }> = ({ g, dx, color }) => (
  <FrameSvg W={g.W} H={g.H}>
    <polygon points={pts(cornerSlabPoints(g, dx))} fill={color} />
  </FrameSvg>
);

/**
 * "full" layout, bottom-left: small Violet A wedge {x - y <= leg - H}, i.e.
 * the corner triangle with legs `leg` and a "\" 45-degree edge.
 */
export const CornerWedge: React.FC<{ g: FullGeometry; dx: number; dy: number; color: string }> = ({
  g,
  dx,
  dy,
  color,
}) => {
  if (g.leg <= 0) return null;
  const E = beyond(g.W, g.H);
  const poly = [
    [-E, g.H - g.leg - E],
    [g.leg + E, g.H + E],
    [-E, g.H + E],
  ] as const;
  return (
    <FrameSvg W={g.W} H={g.H}>
      <polygon points={pts(poly, dx, dy)} fill={color} />
    </FrameSvg>
  );
};
