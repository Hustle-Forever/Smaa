import {
  INK,
  PAPER,
  contrast,
  parseColour,
  rgbToHex,
  vividness,
  type BrandColour,
  type RGB,
} from "./colour";

/**
 * The brand's colours, read once and shared by every part of the page that
 * paints with them (the post preview, the palette, the phrase cards).
 */
export interface BrandKit {
  main: BrandColour[];
  support: BrandColour[];
  /** Every colour that could be read, main first, without repeats. */
  paintable: Array<BrandColour & { rgb: RGB; hex: string }>;
}

export function buildKit(main: string[] = [], support: string[] = []): BrandKit {
  const m = main.map(parseColour);
  const s = support.map(parseColour);
  const seen = new Set<string>();
  const paintable = [...m, ...s].filter((c): c is BrandColour & { rgb: RGB; hex: string } => {
    if (!c.rgb || !c.hex || seen.has(c.hex)) return false;
    seen.add(c.hex);
    return true;
  });
  return { main: m, support: s, paintable };
}

export interface Pairing {
  bg: BrandColour & { rgb: RGB; hex: string };
  /** The text colour, as #RRGGBB. */
  fg: string;
  /** The brand colour the text uses, or null for plain black/white. */
  fgColour: (BrandColour & { rgb: RGB; hex: string }) | null;
  ratio: number;
  /** A third brand colour for a small mark, or null. */
  accent: string | null;
}

/**
 * What to write on a brand colour: a brand colour that reads well on it
 * (the most colourful of those that pass), else black or white.
 */
export function pairFor(bg: BrandKit["paintable"][number], kit: BrandKit, preferVivid = true): Pairing {
  const readable = kit.paintable
    .filter((c) => c.hex !== bg.hex)
    .map((c) => ({ c, ratio: contrast(bg.rgb, c.rgb) }))
    .filter((x) => x.ratio >= 4.5)
    .sort((a, b) => (preferVivid ? vividness(b.c.rgb) - vividness(a.c.rgb) || b.ratio - a.ratio : b.ratio - a.ratio));
  const plain = [PAPER, INK]
    .map((rgb) => ({ rgb, ratio: contrast(bg.rgb, rgb) }))
    .sort((a, b) => b.ratio - a.ratio)[0];

  const pick = readable[0];
  const fg = pick && (!preferVivid ? pick.ratio >= plain.ratio : true) ? pick : null;
  const fgHex = fg ? fg.c.hex : rgbToHex(plain.rgb);
  const ratio = fg ? fg.ratio : plain.ratio;

  const accent =
    kit.paintable
      .filter((c) => c.hex !== bg.hex && c.hex !== fgHex && contrast(bg.rgb, c.rgb) >= 3)
      .sort((a, b) => vividness(b.rgb) - vividness(a.rgb))[0]?.hex ?? null;

  return { bg, fg: fgHex, fgColour: fg ? fg.c : null, ratio, accent };
}

/** Pairs to avoid: brand colours that look fine together but can't be read. */
export function clashes(kit: BrandKit, max = 3): Array<{ bg: BrandKit["paintable"][number]; fg: BrandKit["paintable"][number]; ratio: number }> {
  const out: Array<{ bg: BrandKit["paintable"][number]; fg: BrandKit["paintable"][number]; ratio: number }> = [];
  const usedBg = new Set<string>();
  const all = kit.paintable;
  const pairs = all.flatMap((bg) => all.filter((fg) => fg.hex !== bg.hex).map((fg) => ({ bg, fg, ratio: contrast(bg.rgb, fg.rgb) })));
  pairs
    // Only tempting pairs: at least one of the two is a real colour, not grey.
    .filter((p) => p.ratio < 3 && Math.max(vividness(p.bg.rgb), vividness(p.fg.rgb)) > 0.25)
    .sort((a, b) => a.ratio - b.ratio)
    .forEach((p) => {
      const pair = [p.bg.hex, p.fg.hex].sort().join("");
      if (out.length >= max || usedBg.has(p.bg.hex) || usedBg.has(pair)) return;
      usedBg.add(p.bg.hex);
      usedBg.add(pair);
      out.push(p);
    });
  return out;
}
