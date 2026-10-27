import React from "react";
import { Img, staticFile } from "remotion";

import { PATTERN_TILES, type PatternSlug } from "./patternTiles";

export type PatternColour = "white" | "violet";

/**
 * One of the owner's patterns (patterns/1-5.png) filling a rectangle and
 * sliding LEFT TO RIGHT: the seamless tile (scripts/make_brand42_assets.py)
 * is laid edge to edge, and the whole lattice moves `drift` px to the right,
 * wrapping by one tile width, so it can run for any length of reel with no
 * seam and no end.
 *
 * `reveal` (0..1) is the entrance: the field is uncovered from the rectangle's
 * left edge to its right edge, so the pattern visibly slides IN from the left
 * as well as moving that way. `mask` is an optional CSS mask (gradients only:
 * a mask image fetched from a URL is dropped when it is cross-origin, and the
 * field then renders UNMASKED — never pass one).
 */
export const PatternField: React.FC<{
  slug: PatternSlug;
  colour: PatternColour;
  rect: { x: number; y: number; w: number; h: number };
  /** Tile scale: output px per tile px (1 at 1080 wide). */
  scale: number;
  /** Rightward travel so far, in output px. */
  drift: number;
  /** Where the lattice's rows start, in output px from the canvas top (keeps rows steady). */
  anchorY: number;
  opacity: number;
  reveal?: number;
  mask?: string;
}> = ({ slug, colour, rect, scale, drift, anchorY, opacity, reveal = 1, mask }) => {
  const tile = PATTERN_TILES[slug];
  const file = tile.files[colour] ?? tile.files.white ?? tile.files.violet;
  if (!file || rect.w <= 0 || rect.h <= 0 || opacity <= 0 || reveal <= 0) return null;
  const tw = tile.w * scale;
  const th = tile.h * scale;
  const src = staticFile(file);

  // First column/row at or left of / above the rectangle's edge.
  const mod = (a: number, n: number) => ((a % n) + n) % n;
  const x0 = rect.x - mod(rect.x - drift, tw);
  const y0 = rect.y - mod(rect.y - anchorY, th);
  const tiles: React.ReactNode[] = [];
  for (let y = y0; y < rect.y + rect.h; y += th) {
    for (let x = x0; x < rect.x + rect.w; x += tw) {
      tiles.push(
        <Img
          key={`${Math.round(x)}:${Math.round(y)}`}
          src={src}
          style={{ position: "absolute", left: x - rect.x, top: y - rect.y, width: tw, height: th, maxWidth: "none" }}
        />,
      );
    }
  }
  const shown = reveal >= 1 ? rect.w : rect.w * reveal;
  return (
    <div
      style={{
        position: "absolute",
        left: rect.x,
        top: rect.y,
        width: rect.w,
        height: rect.h,
        overflow: "hidden",
        opacity,
        clipPath: reveal >= 1 ? undefined : `inset(0px ${rect.w - shown}px 0px 0px)`,
        WebkitMaskImage: mask,
        maskImage: mask,
        // Several gradients = the intersection of them (a fade on two sides).
        WebkitMaskComposite: mask ? "source-in" : undefined,
        maskComposite: mask ? "intersect" : undefined,
      }}
    >
      {tiles}
    </div>
  );
};
