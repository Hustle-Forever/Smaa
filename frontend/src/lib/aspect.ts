/**
 * One place that answers "what shape is this clip?".
 *
 * Three incompatible answers existed. HumanCharacterCreator kept a proper
 * lookup map and threaded the result down as a prop. GeneralVideoCreator and
 * TalkingHeadV2Creator each wrote an inline ternary -- and picked DIFFERENT
 * fallbacks for the same unknown input, 16:9 in one and 9:16 in the other. A
 * third group just hardcoded aspect-square / aspect-[3/4] / aspect-video at the
 * call site.
 *
 * That is not a tidiness problem. GeneralVideoCreator's own comment records the
 * bug it already caused: "a hardcoded aspect-video cropped vertical stills into
 * a wide box."
 *
 * Complete class literals in a map, never `aspect-[${w}/${h}]` -- Tailwind's
 * scanner only sees literals and a built string compiles to nothing.
 */
import type { SeedanceAspect, YasHumanCharacterAspect, YasPosterAspect } from "./types";

export type AspectRatio = "1:1" | "3:4" | "4:3" | "9:16" | "16:9" | "21:9";

const ASPECT_CLASS: Record<AspectRatio, string> = {
  "1:1": "aspect-square",
  "3:4": "aspect-[3/4]",
  "4:3": "aspect-[4/3]",
  "9:16": "aspect-[9/16]",
  "16:9": "aspect-[16/9]",
  "21:9": "aspect-[21/9]",
};

/**
 * The Tailwind aspect class for a ratio.
 *
 * `fallback` is explicit and has no default on purpose: the two existing
 * ternaries disagreed about it, so picking a global winner here would silently
 * change one of them. Each caller states the shape its surface actually
 * produces.
 */
export function aspectClass(
  ratio: string | null | undefined,
  fallback: AspectRatio,
): string {
  if (ratio && ratio in ASPECT_CLASS) {
    return ASPECT_CLASS[ratio as AspectRatio];
  }
  return ASPECT_CLASS[fallback];
}

// ---------------------------------------------------------------------------
// The formats each surface offers
//
// ONE NAMING CONVENTION, EVERYWHERE: the button says the RATIO and the line
// under it says what that shape is for. "Square", "Story" and "Wide" were three
// invented words for three numbers, and they made the same choice look like a
// different choice on each screen. The ratio is the thing the model is actually
// given, so it is the thing the button says.
//
// These lists live here, next to `aspectClass`, so a surface cannot offer a
// shape that has no class to render it in. `FormatPicker` draws them.
// ---------------------------------------------------------------------------

export type FormatOption<K extends string = AspectRatio> = {
  key: K;
  /** The ratio itself. Never a nickname. */
  label: string;
  /** What that shape is for, in the reader's words. */
  hint: string;
};

/** Create Poster and Create Poster + Human. Kept in sync with POSTER_ASPECTS in
 *  backend/app/services/poster_pipeline.py, which is what actually forces the
 *  shape (a real Gemini ImageConfig parameter, not a wish in the prompt). */
export const POSTER_FORMATS: FormatOption<YasPosterAspect>[] = [
  { key: "1:1", label: "1:1", hint: "The feed post." },
  { key: "9:16", label: "9:16", hint: "Tall, for Stories and Reels covers." },
  { key: "16:9", label: "16:9", hint: "Landscape, for screens and slides." },
];

/** Human Character. A portrait surface, so it carries 3:4 as well. */
export const HUMAN_CHARACTER_FORMATS: FormatOption<YasHumanCharacterAspect>[] = [
  { key: "1:1", label: "1:1", hint: "Square." },
  { key: "3:4", label: "3:4", hint: "Portrait, the classic headshot." },
  { key: "9:16", label: "9:16", hint: "Vertical, full-height for Reels." },
  { key: "16:9", label: "16:9", hint: "Wide, landscape." },
];

/** General AI Video on the Seedance engine. The six shapes Seedance accepts,
 *  in the order they are offered; 21:9 exists only here. */
export const SEEDANCE_FORMATS: FormatOption<SeedanceAspect>[] = [
  { key: "16:9", label: "16:9", hint: "Landscape, for screens and YouTube." },
  { key: "4:3", label: "4:3", hint: "Classic, a little taller than landscape." },
  { key: "1:1", label: "1:1", hint: "Square, for the feed." },
  { key: "3:4", label: "3:4", hint: "Portrait, for the feed." },
  { key: "9:16", label: "9:16", hint: "Tall, for Reels and Stories." },
  { key: "21:9", label: "21:9", hint: "Extra wide, cinematic." },
];

/**
 * The shape an image actually came out as, read off its own pixels.
 *
 * A finished row carries no aspect field, and the pixels cannot disagree with
 * the image the way a stored label could. Mirrors `aspect_from_dims` on the
 * backend, snap-to-nearest included, so Explore and Remove-logo inherit the
 * shape their parent really has rather than the one someone asked for.
 */
export function aspectFromDims<K extends string>(
  dims: { width?: number | null; height?: number | null },
  options: FormatOption<K>[],
  fallback: K,
): K {
  const w = dims.width || 0;
  const h = dims.height || 0;
  if (w <= 0 || h <= 0) return fallback;
  const ratio = w / h;
  let best = fallback;
  let bestGap = Infinity;
  for (const { key } of options) {
    const [aw, ah] = key.split(":").map(Number);
    const gap = Math.abs(ratio - aw / ah);
    if (gap < bestGap) {
      bestGap = gap;
      best = key;
    }
  }
  return best;
}
