import React from "react";

import type { BarsGeometry } from "./geometry";
import { beyond, FrameSvg, pts } from "./svg";

/**
 * Violet B band across the top bar whose right edge is the reel's exact
 * 45-degree cut: {x + y <= bandCut, y <= hb}. The polygon runs far past the
 * top and left edges so the drop-in and the up-left exit never expose an end.
 */
export const TopBand: React.FC<{ g: BarsGeometry; dx: number; dy: number; color: string }> = ({ g, dx, dy, color }) => {
  const E = beyond(g.W, g.H);
  const poly = [
    [-E, -E],
    [g.bandCut + E, -E],
    [g.bandCut - g.hb, g.hb],
    [-E, g.hb],
  ] as const;
  return (
    <FrameSvg W={g.W} H={g.H}>
      <polygon points={pts(poly, dx, dy)} fill={color} />
    </FrameSvg>
  );
};
