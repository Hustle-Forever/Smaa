/**
 * The bead string: which pattern-1 glyph sits where, and what each looks like.
 *
 * Measured on reel r128 frame 250 (connected components of the white glyphs,
 * projected onto the 45-degree axis): 21 glyphs in two rows, 246 px long at
 * 720 wide. BOTH of r128's strings (on the top band and on the side slab) use
 * this exact sequence, so it is one layout, not two. Row A is the 12-glyph row.
 *
 *   kind "pill"   lies ALONG the string   (14 x 7 px @720)
 *        "lens"   stands ACROSS it        (7 x 14)
 *        "double" two pills joined        (28 x 7)
 *
 * `u` is the glyph centre along the string in px @720 from the first glyph.
 *
 * The glyphs are drawn as vectors (glyphPath), not as sprites: pattern 1 is
 * pixel art, and a 160x80 sprite of it shrunk ~8x under a 45-degree rotation
 * came out with stair-stepped, speckled outlines. r128's glyphs are smooth
 * almonds: 4-sigma axes 13 x 7.2 px and ~71 px of ink each (fill 0.72-0.79 of
 * the box, between a vesica's 0.61 and an ellipse's 0.79), so the shape is an
 * ellipse whose ends are drawn to a blunt point, fill 0.725.
 */
import { BEADS } from "./tokens";

export type GlyphKind = "pill" | "lens" | "double";
export type BeadGlyph = { u: number; row: 0 | 1; kind: GlyphKind };

export const BEAD_STRING: readonly BeadGlyph[] = [
  // Row A (r128: x + y = 451.4, 6 px inside the band)
  { u: 0, row: 0, kind: "lens" },
  { u: 25.9, row: 0, kind: "lens" },
  { u: 39.2, row: 0, kind: "lens" },
  { u: 52.7, row: 0, kind: "pill" },
  { u: 78.6, row: 0, kind: "lens" },
  { u: 97.4, row: 0, kind: "double" },
  { u: 117.5, row: 0, kind: "lens" },
  { u: 131.2, row: 0, kind: "pill" },
  { u: 144.3, row: 0, kind: "lens" },
  { u: 156.6, row: 0, kind: "pill" },
  { u: 196.4, row: 0, kind: "pill" },
  { u: 209.5, row: 0, kind: "lens" },
  // Row B (r128: x + y = 480.7, 23.5 px outside)
  { u: 51.3, row: 1, kind: "lens" },
  { u: 102.4, row: 1, kind: "lens" },
  { u: 116.3, row: 1, kind: "lens" },
  { u: 129.1, row: 1, kind: "lens" },
  { u: 141.1, row: 1, kind: "lens" },
  { u: 181.6, row: 1, kind: "pill" },
  { u: 213.7, row: 1, kind: "double" },
  { u: 233.3, row: 1, kind: "lens" },
  { u: 246.0, row: 1, kind: "pill" },
];

/**
 * The string repeats every PERIOD px (@720) when it slides: 246 px of glyphs
 * plus one typical glyph gap, so the wrap seam looks like any other gap.
 */
export const BEAD_PERIOD = 260;
/** The slide window along u starts this far before the first glyph (@720). */
export const BEAD_WINDOW_START = -7;

/**
 * Blink set: r128's visible count cycles 22 -> 16 -> 22 every 25 frames, i.e.
 * about a quarter of the glyphs drop out together for frames 8-16 of the
 * cycle. Spread over both rows and the whole length.
 */
export const BLINK_SET: ReadonlySet<number> = new Set([1, 6, 10, 13, 17, 20]);

/** Glyph box in output px: [along the string, across it]. */
export const glyphSize = (kind: GlyphKind, s: number): [number, number] => {
  const [w, h] = BEADS.glyph[kind];
  return [w * s, h * s];
};

const f = (n: number) => Math.round(n * 100) / 100;

/**
 * One almond with its long axis along +u, centred on (cu, cv), `a` / `b` the
 * half-length / half-width. Four cubic quadrants: the control points next to
 * each tip sit at (0.8a, 0.55b), which points the tip, and (0.55a, b) keeps
 * the flanks full — fill 0.725 of the box.
 */
function almond(cu: number, cv: number, a: number, b: number, alongV: boolean): string {
  const P = (du: number, dv: number) => (alongV ? `${f(cu + dv)} ${f(cv + du)}` : `${f(cu + du)} ${f(cv + dv)}`);
  return (
    `M${P(-a, 0)}` +
    `C${P(-0.8 * a, -0.55 * b)} ${P(-0.55 * a, -b)} ${P(0, -b)}` +
    `C${P(0.55 * a, -b)} ${P(0.8 * a, -0.55 * b)} ${P(a, 0)}` +
    `C${P(0.8 * a, 0.55 * b)} ${P(0.55 * a, b)} ${P(0, b)}` +
    `C${P(-0.55 * a, b)} ${P(-0.8 * a, 0.55 * b)} ${P(-a, 0)}Z`
  );
}

/**
 * SVG path of one glyph centred on (u, v) in the string's own frame. A lens
 * stands across the string (long axis along v); a double is two pills tip to
 * tip, overlapping a hair so they read as one joined glyph like pattern 1's.
 */
export function glyphPath(kind: GlyphKind, u: number, v: number, s: number): string {
  const [along, across] = glyphSize(kind, s);
  if (kind === "lens") return almond(u, v, across / 2, along / 2, true);
  if (kind === "pill") return almond(u, v, along / 2, across / 2, false);
  const a = along / 4 + 0.03 * across; // each half, plus the overlap at the join
  return almond(u - along / 4, v, a, across / 2, false) + almond(u + along / 4, v, a, across / 2, false);
}
