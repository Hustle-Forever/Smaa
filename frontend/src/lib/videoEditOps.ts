/**
 * Video Editor · pure timeline edit ops.
 *
 * Shared by the editor store (commit) AND the timeline drag handlers (live
 * preview) so what you see while dragging is exactly what gets saved. Pure
 * functions, no React — lives in lib/ so the store can import it cleanly.
 */
import type { VideoEditClip } from "./types";

export function clipNatural(c: VideoEditClip): number {
  return c.source?.naturalDurationInFrames || c.trimBefore + c.durationInFrames || 1;
}

/**
 * Trim one edge by `deltaFrames`. Left edge moves `from` + `trimBefore` (right
 * edge stays anchored); right edge moves `trimAfter`. Clamped to the source's
 * natural length and to timeline frame 0.
 */
export function applyTrimEdge(
  c: VideoEditClip,
  edge: "left" | "right",
  deltaFrames: number,
): Pick<VideoEditClip, "from" | "trimBefore" | "trimAfter" | "durationInFrames"> {
  const natural = clipNatural(c);
  if (edge === "left") {
    let nb = Math.max(0, Math.min(c.trimBefore + deltaFrames, c.trimAfter - 1));
    let actual = nb - c.trimBefore;
    actual = Math.max(actual, -c.from); // left edge can't pass timeline 0
    nb = c.trimBefore + actual;
    return {
      from: c.from + actual,
      trimBefore: nb,
      trimAfter: c.trimAfter,
      durationInFrames: c.durationInFrames - actual,
    };
  }
  const na = Math.max(c.trimBefore + 1, Math.min(c.trimAfter + deltaFrames, natural));
  return {
    from: c.from,
    trimBefore: c.trimBefore,
    trimAfter: na,
    durationInFrames: na - c.trimBefore,
  };
}

/** Snap `frame` to the nearest target within `thresholdPx` (screen pixels). */
export function snapFrame(
  frame: number,
  targets: number[],
  pxPerFrame: number,
  thresholdPx = 6,
): number {
  let best = frame;
  let bestDist = thresholdPx;
  for (const t of targets) {
    const d = Math.abs((frame - t) * pxPerFrame);
    if (d < bestDist) {
      best = t;
      bestDist = d;
    }
  }
  return Math.round(best);
}
