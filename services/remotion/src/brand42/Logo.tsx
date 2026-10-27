import React from "react";
import { Img, staticFile } from "remotion";

import type { Rect } from "./geometry";
import { ASSETS } from "./tokens";

/**
 * The stacked white 42 lockup. `reveal` (0..1) drives the reel's entrance:
 * fade in while rising `rise` px into place. `dx` carries it with the slab it
 * sits on (full layout), `alpha` fades it out when a hand-off takes it away.
 */
export const Logo: React.FC<{ rect: Rect; reveal: number; rise: number; dx?: number; alpha?: number }> = ({
  rect,
  reveal,
  rise,
  dx = 0,
  alpha = 1,
}) => {
  const opacity = reveal * alpha;
  if (opacity <= 0 || rect.w <= 0) return null;
  return (
    <Img
      src={staticFile(ASSETS.logo)}
      style={{
        position: "absolute",
        left: rect.x + dx,
        top: rect.y + rise * (1 - reveal),
        width: rect.w,
        height: rect.h,
        opacity,
      }}
    />
  );
};
