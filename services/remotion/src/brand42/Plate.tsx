import React from "react";

import type { Rect } from "./geometry";
import { FrameSvg } from "./svg";

/**
 * Charcoal fill of the letterbox bars: the whole frame minus the content box
 * (even-odd), so it is exact even outside the bars clip. The source's bars
 * are black; this replaces them with the reel background.
 */
export const Plate: React.FC<{ W: number; H: number; content: Rect; color: string }> = ({ W, H, content, color }) => {
  const { x, y, w, h } = content;
  const d = `M0 0H${W}V${H}H0Z M${x} ${y}H${x + w}V${y + h}H${x}Z`;
  return (
    <FrameSvg W={W} H={H}>
      <path d={d} fill={color} fillRule="evenodd" />
    </FrameSvg>
  );
};
