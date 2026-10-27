/**
 * Digital glitch, as a wrapper around any graphic layer of the frame.
 *
 * The grammar is the one reels (and AE / Premiere "glitch transition" packs)
 * use, measured against remocn's Glitch Cut and the MIT remotion-glitch-effect:
 *  - the layer breaks into horizontal SLICES that jump sideways by stepped
 *    pseudo-random amounts (new values every STEP frames, never smoothly);
 *    a few slices tear much further than the rest;
 *  - the colour channels SPLIT: a red copy goes one way and a cyan copy the
 *    other, under the layer itself, so its edges fringe red / cyan;
 *  - small CORRUPT BLOCKS flash for a frame or two;
 *  - on the way IN each slice appears at its own moment (a staggered
 *    hand-over, not a fade), on the way OUT each one drops at its own moment;
 *  - it is short: an 8-10 frame transition, or a 2-3 frame idle "burst"
 *    every few seconds so the frame feels alive.
 * Every value is a pure function of (seed, slice, step): frames render the
 * same in any order, on any machine (Remotion's random(seed)).
 *
 * The composition paints graphics only (the footage is composited later), so
 * the channel copies are coloured copies of the graphic drawn with normal
 * alpha, not a blend with the picture underneath.
 */
import React from "react";
import { AbsoluteFill, random } from "remotion";

import { EASE } from "./easing";

export const GLITCH_STEP = 2;

/** How glitched a layer is on this frame. */
export type GlitchState = {
  /** Displacement / channel split / block strength, 0..1. */
  amount: number;
  /** Share of slices showing (1 = all; the staggered hand-over runs it 0 -> 1 or 1 -> 0). */
  reveal: number;
};

export const CLEAN: GlitchState = { amount: 0, reveal: 1 };

const clamp01 = (t: number) => (t < 0 ? 0 : t > 1 ? 1 : t);

/**
 * Glitch IN over `frames` from `start`: the slices come on one by one over the
 * first ~60% (stepped), the tearing is strongest on the first frame and has
 * settled by the last. Before `start` the layer is hidden.
 */
export function glitchIn(frame: number, start: number, frames: number): GlitchState {
  if (frame < start) return { amount: 0, reveal: 0 };
  const t = (frame - start + 1) / Math.max(1, frames);
  if (t >= 1) return CLEAN;
  return { amount: 1 - EASE.inOutCubic(clamp01(t)), reveal: clamp01(t / 0.6) };
}

/** Glitch OUT over the `frames` that end at `end` (exclusive): the mirror image. */
export function glitchOut(frame: number, end: number, frames: number): GlitchState {
  if (frame >= end) return { amount: 0, reveal: 0 };
  const t = (end - frame) / Math.max(1, frames);
  if (t >= 1) return CLEAN;
  return { amount: 1 - EASE.inOutCubic(clamp01(t)), reveal: clamp01(t / 0.6) };
}

/**
 * Idle bursts: about every `period` frames (jittered per burst) a `length`
 * frame twitch at `strength`, the rest of the time nothing. `key` separates
 * layers so they never twitch in unison.
 */
export function burst(frame: number, key: string, period = 84, length = 3, strength = 0.38): GlitchState {
  const n = Math.floor(frame / period);
  const at = n * period + Math.floor(random(`${key}-burst-${n}`) * (period - length - 6)) + 6;
  if (frame >= at && frame < at + length) return { amount: strength, reveal: 1 };
  return CLEAN;
}

/** The stronger of two states (a burst never weakens a transition). */
export const worst = (a: GlitchState, b: GlitchState): GlitchState => ({
  amount: Math.max(a.amount, b.amount),
  reveal: Math.min(a.reveal, b.reveal),
});

/** One-time SVG defs: the red / cyan channel filters the split copies use. */
export const GlitchDefs: React.FC = () => (
  <svg width={0} height={0} style={{ position: "absolute" }} aria-hidden>
    <defs>
      {/* Keep the alpha, replace the colour: a pure red / cyan silhouette. */}
      <filter id="b42-glitch-red" colorInterpolationFilters="sRGB">
        <feColorMatrix type="matrix" values="0 0 0 0 1  0 0 0 0 0.12  0 0 0 0 0.3  0 0 0 1 0" />
      </filter>
      <filter id="b42-glitch-cyan" colorInterpolationFilters="sRGB">
        <feColorMatrix type="matrix" values="0 0 0 0 0.1  0 0 0 0 0.9  0 0 0 0 1  0 0 0 1 0" />
      </filter>
    </defs>
  </svg>
);

export type GlitchRegion = { x0: number; y0: number; x1: number; y1: number };

/**
 * Renders `children` (full-canvas layers) glitched by `state`. With a clean
 * state it is exactly `children`, so wrapping a layer costs nothing between
 * glitches. `region` is where the slicing happens (the layer's own box: an
 * icon is cut into bands of ITS height, not of the frame's); outside it the
 * layer is left alone. `shift` is the largest ordinary sideways jump in px,
 * `split` the channel offset at full strength.
 */
export const GlitchWrap: React.FC<{
  frame: number;
  state: GlitchState;
  seed: string;
  region: GlitchRegion;
  shift: number;
  split: number;
  slices?: number;
  blocks?: string[];
  children: React.ReactNode;
}> = ({ frame, state, seed, region, shift, split, slices = 10, blocks = [], children }) => {
  const { amount, reveal } = state;
  if (reveal <= 0) return null;
  if (amount <= 0.001 && reveal >= 1) return <>{children}</>;

  const step = Math.floor(frame / GLITCH_STEP);
  const r = (k: string) => random(`${seed}-${k}-${step}`);
  const rs = (k: string) => random(`${seed}-${k}`); // the same for the whole glitch
  const top = region.y0;
  const height = Math.max(1, region.y1 - region.y0);

  // Slice bounds: equal bands with their inner edges jittered per step, so
  // the tear lines move between steps like a real signal fault.
  const bounds: number[] = [top];
  for (let i = 1; i < slices; i++) {
    bounds.push(top + ((i + (r(`b${i}`) - 0.5) * 0.7) / slices) * height);
  }
  bounds.push(region.y1);

  // A whole-layer flicker now and then while it is strongly glitched.
  if (amount > 0.5 && r("flicker") < 0.16 * amount) return null;

  const split1 = split * amount * (0.6 + 0.4 * r("split"));
  const pieces: React.ReactNode[] = [];
  for (let i = 0; i < slices; i++) {
    // Staggered hand-over: slice i shows once `reveal` passes its own rank.
    if (rs(`rank${i}`) >= reveal) continue;
    const tear = r(`tear${i}`) < 0.18 ? 2.6 : 1;
    const dx = (r(`dx${i}`) - 0.5) * 2 * shift * amount * tear;
    const y0 = bounds[i];
    const y1 = bounds[i + 1];
    // Slices touching the region's edges extend to the canvas edge, so the
    // parts of the layer outside the region are neither lost nor sliced.
    const clipTop = i === 0 ? -100000 : y0;
    const clipBottom = i === slices - 1 ? 100000 : y1;
    const clip = `polygon(-100000px ${clipTop}px, 100000px ${clipTop}px, 100000px ${clipBottom}px, -100000px ${clipBottom}px)`;
    pieces.push(
      <AbsoluteFill key={i} style={{ clipPath: clip }}>
        {split1 > 0.5 ? (
          <>
            <AbsoluteFill style={{ transform: `translateX(${dx - split1}px)`, filter: "url(#b42-glitch-cyan)", opacity: 0.85 }}>
              {children}
            </AbsoluteFill>
            <AbsoluteFill style={{ transform: `translateX(${dx + split1}px)`, filter: "url(#b42-glitch-red)", opacity: 0.85 }}>
              {children}
            </AbsoluteFill>
          </>
        ) : null}
        <AbsoluteFill style={{ transform: `translateX(${dx}px)` }}>{children}</AbsoluteFill>
      </AbsoluteFill>,
    );
  }

  // Corrupt blocks: thin bars of brand colour across the region (a few px
  // tall whatever the region: a tall block reads as a UI panel, not a fault).
  const blockEls: React.ReactNode[] = [];
  if (blocks.length && amount > 0.35) {
    const n = 1 + Math.floor(r("nblocks") * 3 * amount);
    const w = Math.max(1, region.x1 - region.x0);
    for (let k = 0; k < n; k++) {
      const bh = Math.max(2, Math.min(0.12 * height, split * (0.6 + 2.2 * r(`bh${k}`))));
      const bw = (0.1 + 0.35 * r(`bw${k}`)) * w;
      const bx = region.x0 + r(`bx${k}`) * (w - bw) + (r(`bj${k}`) - 0.5) * shift;
      const by = top + r(`by${k}`) * (height - bh);
      blockEls.push(
        <div
          key={`blk${k}`}
          style={{
            position: "absolute",
            left: bx,
            top: by,
            width: bw,
            height: bh,
            background: blocks[Math.floor(r(`bc${k}`) * blocks.length) % blocks.length],
            opacity: 0.9,
          }}
        />,
      );
    }
  }

  return (
    <AbsoluteFill>
      {pieces}
      {blockEls}
    </AbsoluteFill>
  );
};
