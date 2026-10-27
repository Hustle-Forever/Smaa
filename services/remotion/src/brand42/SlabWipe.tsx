import React from "react";

import { BeadTrail } from "./BeadTrail";
import { STRING_SPAN, onSlash, uOnSlash, type BeadLine, type Geometry } from "./geometry";
import { FrameSvg, pts } from "./svg";
import type { WipeState } from "./timeline";
import { BEADS, FULL, type Palette } from "./tokens";

const SQRT2 = Math.SQRT2;

/**
 * The cover / short slab wipe that hides a hard cut (precedent: 42 reel 436).
 *
 * In:   a Violet A region {x + y <= c} grows out of the outgoing layout's top
 *       band until its leading 45-degree edge is past the far corner. It
 *       starts at the band's bottom-left corner and grows UNDER the band (the
 *       caller redraws the band and its beads on top during this phase), so
 *       the band keeps its own Violet B and nothing pops: the first in-frame
 *       shows a sliver emerging from under the band, not a recoloured band
 *       plus a 336 px triangle below it at once. (On a "full" layout it grows
 *       from the top-left corner.)
 * Hold: the whole canvas, painted as one rect overshooting every edge, so
 *       100% of pixels are violet and nothing anti-aliases at the border.
 * Out:  the trailing edge {x + y >= c} sweeps from the top-left past the
 *       bottom-right, uncovering the incoming layout; its last piece leaves at
 *       the bottom-right, where the incoming side slab sits.
 * A 0.05 W Violet B sliver rides on the footage side of the moving edge
 * (ahead of it going in, behind it going out). A white bead string rides
 * the sliver's outer edge — but only on the part of that edge over the bars:
 * never across the picture (the face), never under the band, and never
 * within 60 px (@720, in x + y) of the band's own string, where the two
 * strings read as one cluttered double cluster (see wipeString). The wipe is
 * drawn over everything and is NOT clipped to the bars.
 *
 * EASING IS APPLIED TO THE SWEPT AREA, NOT TO THE EDGE POSITION. The spec's
 * in-out-cubic was first applied to c directly and measured on a 1080x1920
 * render: the cubic's flat ends plus the quadratic area of a diagonal near a
 * corner left only 3 visibly moving frames on each side and >= 97% cover on 11
 * frames (F-5..F+5), a long flat violet hold. Easing the covered share of the
 * frame instead keeps the same curve but spends it where the eye sees it:
 * 9 (the band) / 13 / 51 / 90 / 99.6% going in and 96 / 82 / 50 / 18 / 4%
 * going out, which is reel 436's rhythm (16 -> 88% over 5 frames, 7-frame
 * hold, 6-frame exit).
 */
/** Area of {x + y <= e} inside the W x H canvas. */
export function sweptArea(e: number, W: number, H: number): number {
  const a = Math.min(W, H);
  const b = Math.max(W, H);
  if (e <= 0) return 0;
  if (e <= a) return (e * e) / 2;
  if (e <= b) return (a * a) / 2 + a * (e - a);
  if (e <= a + b) return a * b - ((a + b - e) * (a + b - e)) / 2;
  return a * b;
}

/** Inverse of sweptArea: the x + y edge that sweeps `area` of the canvas. */
export function edgeForArea(area: number, W: number, H: number): number {
  const a = Math.min(W, H);
  const b = Math.max(W, H);
  const X = Math.max(0, Math.min(a * b, area));
  if (X <= (a * a) / 2) return Math.sqrt(2 * X);
  if (X <= a * b - (a * a) / 2) return a + (X - (a * a) / 2) / a;
  return a + b - Math.sqrt(2 * (a * b - X));
}

type Shape =
  | { hold: true }
  | { hold: false; violetA: string; violetB: string; edge: number; rows: readonly [number, number] };

function wipeShape(wipe: WipeState, W: number, H: number, s: number): Shape {
  if (wipe.phase === "hold") return { hold: true };
  const sw = 0.05 * W;
  const margin = 40 * s; // the bead string's reach beyond the edge, plus AA
  const M = 4 * (W + H);
  const below = (c: number) => pts([[-M, -M], [c + M, -M], [-M, c + M]]); // {x + y <= c}
  const above = (c: number) => pts([[c + M, -M], [c + 3 * M, c + 3 * M], [-M, c + M]]); // {x + y >= c}

  if (wipe.phase === "in") {
    // Leading edge (the sliver's front) from the band's bottom-left corner
    // (the top-left corner on a "full" layout) to the far corner, eased by
    // covered area.
    const og = wipe.outgoing.geom;
    const e0 = og.layout === "bars" ? og.hb : 0;
    const a0 = sweptArea(e0, W, H);
    const lead = edgeForArea(a0 + (W * H - a0) * wipe.e, W, H);
    const c = wipe.e >= 1 ? W + H + margin : lead - sw;
    return {
      hold: false,
      violetB: below(c + sw),
      violetA: below(c),
      edge: c + sw,
      rows: [(-BEADS.insideXY * s) / SQRT2, (BEADS.outsideXY * s) / SQRT2],
    };
  }
  // Out: the revealed share grows 0 -> 1: the sliver's back edge starts on
  // the top-left corner (sliver and string still off-canvas) and ends on the
  // bottom-right one.
  const trail = edgeForArea(W * H * wipe.e, W, H);
  const c = wipe.e >= 1 ? W + H + sw + margin : trail + sw;
  return {
    hold: false,
    violetB: above(c - sw),
    violetA: above(c),
    edge: c - sw,
    rows: [(-BEADS.outsideXY * s) / SQRT2, (BEADS.insideXY * s) / SQRT2],
  };
}

/** The wipe's violet: Violet B sliver + Violet A body (a full-frame rect on hold). */
export const SlabWipeBody: React.FC<{ wipe: WipeState; W: number; H: number; s: number; palette: Palette }> = ({
  wipe,
  W,
  H,
  s,
  palette,
}) => {
  const sh = wipeShape(wipe, W, H, s);
  return (
    <FrameSvg W={W} H={H}>
      {sh.hold ? (
        <rect x={-8} y={-8} width={W + 16} height={H + 16} fill={palette.violet} />
      ) : (
        <>
          <polygon points={sh.violetB} fill={palette.violetB} />
          <polygon points={sh.violetA} fill={palette.violet} />
        </>
      )}
    </FrameSvg>
  );
};

type Interval = [number, number];

/** [lo, hi] minus every interval in `cut`; the pieces left, unsorted. */
function subtract(base: Interval, cut: Interval[]): Interval[] {
  let pieces: Interval[] = [base];
  for (const [a, b] of cut) {
    const next: Interval[] = [];
    for (const [lo, hi] of pieces) {
      if (b <= lo || a >= hi) {
        next.push([lo, hi]);
        continue;
      }
      if (a > lo) next.push([lo, a]);
      if (b < hi) next.push([b, hi]);
    }
    pieces = next;
  }
  return pieces;
}

/**
 * Where the wipe's string may sit on the edge x + y = e, as an x interval:
 * the on-canvas part of the edge minus (1) the picture of the layout the
 * edge is uncovering / covering (the content box on "bars", the face zone on
 * "full"), (2) its logo, and (3) on "bars" the band's corner of the top bar,
 * wherever the band covers the edge (in-phase, the band is drawn over the
 * wipe) or the band's own string runs next to it. The longest piece wins.
 */
function stringInterval(e: number, g: Geometry, phase: "in" | "out", W: number, H: number, s: number): Interval | null {
  const m = 28 * s; // the rows' reach across the edge, as x along it
  const cut: Interval[] = [];
  const rect = (x0: number, y0: number, x1: number, y1: number) => {
    const lo = Math.max(x0 - m, e - y1 - m);
    const hi = Math.min(x1 + m, e - y0 + m);
    if (hi > lo) cut.push([lo, hi]);
  };
  if (g.layout === "bars") {
    const c = g.content;
    rect(c.x, c.y, c.x + c.w, c.y + c.h);
    const bandNear = Math.abs(e - g.bandCut) < BEADS.wipeBandClear * s;
    if (bandNear || (phase === "in" && e <= g.bandCut)) rect(-W, -H, 2 * W, g.hb + 20 * s);
  } else {
    const z = FULL.faceZone;
    rect(z.x0 * W, z.y0 * H, z.x1 * W, z.y1 * H);
  }
  const L = g.logo;
  if (L.w > 0) rect(L.x, L.y, L.x + L.w, L.y + L.h);

  const pieces = subtract([Math.max(0, e - H), Math.min(W, e)], cut);
  let best: Interval | null = null;
  for (const p of pieces) if (!best || p[1] - p[0] > best[1] - best[0]) best = p;
  return best;
}

/**
 * The white bead string riding the wipe's moving edge (none on hold). Only
 * whole glyphs inside the chosen part of the edge are drawn; a part shorter
 * than ~60 px (@720) along the edge gets no string at all.
 */
export const SlabWipeString: React.FC<{
  wipe: WipeState;
  W: number;
  H: number;
  s: number;
  palette: Palette;
  frame: number;
}> = ({ wipe, W, H, s, palette, frame }) => {
  const sh = wipeShape(wipe, W, H, s);
  if (sh.hold) return null;
  const g = wipe.phase === "in" ? wipe.outgoing.geom : wipe.incoming.geom;
  const iv = stringInterval(sh.edge, g, wipe.phase === "in" ? "in" : "out", W, H, s);
  if (!iv) return null;
  const uLo = uOnSlash(sh.edge, iv[0]);
  const uHi = uOnSlash(sh.edge, iv[1]);
  if (uHi - uLo < 60 * s) return null;
  const start = (uLo + uHi) / 2 - (STRING_SPAN * s) / 2;
  const line: BeadLine = {
    origin: onSlash(sh.edge, start),
    deg: -45,
    rows: sh.rows,
    span: [uLo - start, uHi - start],
  };
  return <BeadTrail W={W} H={H} line={line} s={s} frame={frame} color={palette.ink} />;
};
