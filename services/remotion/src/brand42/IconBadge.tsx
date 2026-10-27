import React from "react";

import { ICON_VIEWBOX, ICONS } from "./icons";
import { FrameSvg } from "./svg";

/**
 * A topic icon (what the speaker is talking about: a piggy bank for "free",
 * </> for "code") on a Violet A badge whose top-right corner is cut at 45
 * degrees — the 42 slab grammar in miniature. The badge, not the icon, is
 * what makes it read over ANY footage (a white icon alone vanishes on a light
 * wall, a violet one on a dark one). Flat, no shadow, like the reels.
 */
export const IconBadge: React.FC<{
  W: number;
  H: number;
  box: { x: number; y: number; size: number };
  icon: string;
  violet: string;
  ink: string;
}> = ({ W, H, box, icon, violet, ink }) => {
  const d = ICONS[icon];
  if (!d) return null;
  const { x, y, size: L } = box;
  const cut = 0.24 * L;
  const inner = 0.6 * L;
  const k = inner / ICON_VIEWBOX;
  return (
    <FrameSvg W={W} H={H}>
      <polygon
        points={`${x},${y} ${x + L - cut},${y} ${x + L},${y + cut} ${x + L},${y + L} ${x},${y + L}`}
        fill={violet}
      />
      <path d={d} fill={ink} transform={`translate(${x + (L - inner) / 2} ${y + (L - inner) / 2}) scale(${k})`} />
    </FrameSvg>
  );
};
