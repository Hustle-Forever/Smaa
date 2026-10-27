// Rebrand studio — a colour change, painted at once (pure).
//
// Every part of a preview lists its drawn layers in order (chunk.layers),
// each as a colour-free alpha: the part's target IS its frame with each
// alpha laid in, in its element's colour. The server lays them in with
// PIL's alpha_composite; blendLayer repeats that in the same integers, so
// the picture made here is the server's to the pixel (0 measured, at most
// 1 allowed: backend tests/test_rebrand_studio_recolour.py). That is why a
// colour change can be shown the moment it is made: the stage paints it
// from the preview on screen and swaps in the server's frame once it lands.
//
// Only a colour. A new place, pattern or spot changes the alphas themselves,
// so those wait for the server as before.

import type { HarnessCatalogue, HarnessPreview, HarnessRecipe } from "./rebrandHarnessApi";
import { colourOf, isHex } from "./rebrandRecipe";

export type Rgb = readonly [number, number, number];

export function hexRgb(hex: string | null | undefined): Rgb | null {
  if (!isHex(hex)) return null;
  return [parseInt(hex.slice(1, 3), 16), parseInt(hex.slice(3, 5), 16), parseInt(hex.slice(5, 7), 16)];
}

/** round(x / 255) the way PIL's alpha_composite rounds it, for x = C·a +
 *  F·(255 − a) (PRECISION_BITS 7, SHIFTFORDIV255). */
function div255(x: number): number {
  const t = x * 128 + 16384;
  return (((t >> 8) + t) >> 8) >> 7;
}

/** Lays one layer into `px` (RGBA, opaque; changed in place): colour `rgb`
 *  at the opacity `plane` holds per pixel (one byte a pixel, the alpha's A
 *  channel) — PIL's alpha_composite of that layer over an opaque frame. */
export function blendLayer(px: Uint8ClampedArray, plane: ArrayLike<number>, rgb: Rgb): void {
  const [r, g, b] = rgb;
  const n = Math.min(plane.length, px.length >> 2);
  for (let p = 0, i = 0; p < n; p++, i += 4) {
    const a = plane[p];
    if (a === 0) continue;
    const na = 255 - a;
    px[i] = div255(r * a + px[i] * na);
    px[i + 1] = div255(g * a + px[i + 1] * na);
    px[i + 2] = div255(b * a + px[i + 2] * na);
  }
}

/** A frame (RGBA) with every layer laid in, in order: a new array. */
export function recolour(
  frame: Uint8ClampedArray,
  layers: { plane: ArrayLike<number>; rgb: Rgb }[],
): Uint8ClampedArray<ArrayBuffer> {
  const out = new Uint8ClampedArray(frame);
  for (const l of layers) blendLayer(out, l.plane, l.rgb);
  return out;
}

export interface RecolourLayer {
  /** The layer's colour-free alpha. */
  url: string;
  /** The colour the draft paints it (#rrggbb). */
  colour: string;
}

/** What one part looks like in the draft's colours, made from the preview
 *  on screen: its frame (the version's own, added on top of one) and each
 *  layer's alpha with the colour the DRAFT gives its element. null when it
 *  can't be: an older preview with no layers, a layer whose element the
 *  draft no longer shows, or a colour not known yet (Auto before the
 *  intro's colour is). */
export interface RecolourSpec {
  base: string;
  layers: RecolourLayer[];
}

export function recolourOf(
  preview: HarnessPreview | null | undefined,
  draft: HarnessRecipe | null | undefined,
  part: number,
  ctx: { catalogue: HarnessCatalogue | null; introColour: string | null },
): RecolourSpec | null {
  const chunk = preview?.chunks[part];
  if (!chunk || !draft || !Array.isArray(chunk.layers)) return null;
  const layers: RecolourLayer[] = [];
  for (const l of chunk.layers) {
    const el = draft.elements.find((e) => e.id === l.element_id && e.kind === "pattern");
    if (!el || !el.enabled || !l.alpha_url) return null;
    const colour = colourOf(el, draft, ctx);
    if (!hexRgb(colour)) return null;
    layers.push({ url: l.alpha_url, colour: colour!.toLowerCase() });
  }
  return { base: chunk.frame_url, layers };
}
