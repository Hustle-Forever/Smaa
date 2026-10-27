/**
 * The tile a card shows when there is no picture.
 *
 * WHY IT EXISTS. Roughly half the feed has no image and never will: OpenAI,
 * Mistral and every Google News lane ship no media at all, and a competitor
 * post's Instagram thumbnail 410s within days of being collected. Those cards
 * were flat grey rectangles with "no preview" in 10px mono — next to a card
 * carrying a real photograph, they read as broken rather than as a different
 * KIND of thing, and the eye skips them.
 *
 * WHY THE COLOUR IS DERIVED, NOT RANDOM. The tint comes from a hash of the
 * source's name, so OpenAI is always the same colour and Gulf News is always a
 * different one, in this session and the next. That is the whole reason it is
 * worth doing: a stable colour per publisher is something you learn without
 * being taught, and it turns a wall of grey into a scannable list. Random tints
 * would look identical on any one screenshot and be worthless in use — worse
 * than worthless, since a card would change colour on every reload.
 *
 * WHY INLINE STYLES. This file is the one deliberate exception to the repo's
 * complete-literal Tailwind rule. The tint is computed at runtime, and
 * `bg-[${hue}]` is exactly the concatenation the scanner cannot see, so it
 * would ship a class that does not exist in the stylesheet. Inline `style` is
 * the correct tool for a value the design system does not know in advance —
 * every FIXED colour here is still a token.
 *
 * The palette is deliberately quiet: these tiles sit beside photographs and
 * must not out-shout them. Everything is a wash at 8-14% alpha over the card's
 * own background, which is why it reads as tinted paper rather than as a block
 * of colour.
 *
 * WHAT MAKES IT READ AS DESIGNED. Three things, and each one is doing a job:
 *
 *   the rule along the top   the one place the hue is stated at full strength,
 *                            so the identity survives at any size, including
 *                            the compact tile that has no caption at all
 *   the giant monogram       fills the space a photograph would have filled,
 *                            at a weight that never competes with the headline
 *   the ruled caption        the source, tied to the tile by a hairline instead
 *                            of floating loose at the bottom edge
 *
 * A tile that is only a wash and some letters looks like the thing that
 * happens when an image fails. A tile with a structure looks like the thing
 * this kind of item is supposed to look like, which is the whole point.
 */
import { useEffect, useLayoutEffect, useRef, useState } from "react";
import type { ComponentType, RefObject } from "react";

/**
 * Hues that sit well beside the teal accent on white. Not a rainbow: six
 * neighbours plus two warm outliers, so two adjacent cards are always
 * distinguishable but the grid never looks like a toy.
 */
const HUES = [
  "6, 182, 162", // the brand teal itself
  "99, 102, 241", // indigo
  "14, 165, 233", // sky
  "139, 92, 246", // violet
  "16, 185, 129", // emerald
  "244, 63, 94", // rose
  "245, 158, 11", // amber
  "100, 116, 139", // slate — the quiet one, for anything unnamed
] as const;

/**
 * FNV-1a, which is not chosen for cryptography but for STABILITY: it is a few
 * lines, it has no dependencies, and the same string gives the same number in
 * every browser and every build. A hash that varied per engine would undo the
 * one property that makes this useful.
 */
function hueOf(seed: string): string {
  const s = (seed || "").trim().toLowerCase();
  if (!s) return HUES[HUES.length - 1];
  let h = 0x811c9dc5;
  for (let i = 0; i < s.length; i += 1) {
    h ^= s.charCodeAt(i);
    h = Math.imul(h, 0x01000193) >>> 0;
  }
  return HUES[h % HUES.length];
}

/**
 * Up to two letters, from the initials of the first two words.
 *
 * "@hub71ad" -> H, "Gulf News" -> GN, "AI in everyday life" -> AI. The leading
 * @ is stripped because a monogram of punctuation identifies nothing.
 */
export function monogramOf(label: string): string {
  const words = (label || "")
    .replace(/^@/, "")
    .split(/[\s._/-]+/)
    .filter(Boolean);
  if (words.length === 0) return "?";
  if (words.length === 1) return words[0].slice(0, 2).toUpperCase();
  return (words[0][0] + words[1][0]).toUpperCase();
}

/**
 * How many whole lines of the headline fit the room the tile leaves it.
 *
 * A fixed clamp cannot be right: the room is whatever the tile's height leaves
 * after the overlay row and the caption, and the tile is not one height. With
 * `line-clamp-5` in a 160px feed tile, which has room for three, a long
 * headline ran past the bottom and the tile's own overflow cut its last
 * visible line through the middle of the letters. So the room is measured,
 * the clamp is however many whole lines fit it (the last one ends in an
 * ellipsis), and it is measured again whenever the tile changes size.
 *
 * Starts at `fallback` so the first paint, and any browser without
 * ResizeObserver, behaves as the fixed clamp did.
 */
function useLinesThatFit(
  room: RefObject<HTMLElement | null>,
  line: RefObject<HTMLElement | null>,
  fallback: number,
  // Re-attach when the headline comes or goes: with no headline there is
  // nothing to measure, and the refs only fill once it renders.
  hasLine: boolean,
): number {
  const [lines, setLines] = useState(fallback);
  useLayoutEffect(() => {
    const box = room.current;
    const text = line.current;
    if (!box || !text) return;
    const measure = () => {
      const lh = parseFloat(getComputedStyle(text).lineHeight);
      if (!Number.isFinite(lh) || lh <= 0) return;
      // Half a pixel of slack: the room is a whole number of pixels and the
      // line height is not, so an exact fit must not round down a line.
      setLines(Math.max(1, Math.floor((box.clientHeight + 0.5) / lh)));
    };
    measure();
    if (typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(measure);
    observer.observe(box);
    return () => observer.disconnect();
  }, [room, line, hasLine]);
  return lines;
}

interface Props {
  /** Decides the colour. The publisher or handle — NOT the headline, or every
   *  card from one outlet would be a different colour. */
  seed: string;
  /** Shown in mono along the bottom, and the monogram is taken from it. */
  label?: string | null;
  /** The headline. On a full-size tile the text IS the preview. */
  title?: string | null;
  /** Sits beside the label: which kind of thing this is. `any` because a lucide
   *  icon is a forwardRef whose props do not satisfy a hand-written signature —
   *  the same spelling `TabDef.Icon` and every other icon prop here uses. */
  Icon?: ComponentType<any>;
  /** Small tiles (list rows, detail sidebars) drop the headline and keep the
   *  monogram, because four lines of clamped text in a 64px box is a smudge. */
  compact?: boolean;
  /** A brand mark to show IN PLACE OF the monogram: the company the story is
   *  about, or its publisher. Drawn small and centred on the same tinted card,
   *  never stretched to fill it, so a logo tile and a plain tile read as the
   *  same kind of thing and a 180px mark never becomes a blurry square. If the
   *  picture will not load the monogram comes back, so the tile is never
   *  empty. */
  logoSrc?: string | null;
  className?: string;
}

export function CoverTile({
  seed,
  label,
  title,
  Icon,
  compact = false,
  logoSrc = null,
  className = "",
}: Props) {
  const rgb = hueOf(seed || label || "");
  const mono = monogramOf(label || seed || "");
  // A mark that fails to load falls back to the monogram, and a NEW mark gets
  // a fresh chance: the same tile is reused across cards by React.
  const [logoBroken, setLogoBroken] = useState(false);
  useEffect(() => {
    setLogoBroken(false);
  }, [logoSrc]);
  const logo = logoSrc && !logoBroken ? logoSrc : null;
  const headlineRoom = useRef<HTMLDivElement>(null);
  const headline = useRef<HTMLHeadingElement>(null);
  const headlineLines = useLinesThatFit(headlineRoom, headline, 5, !compact && !!title);

  return (
    <div
      className={`relative flex h-full w-full flex-col overflow-hidden ${className}`}
      // The compact tile has no room for the caption, so the tooltip is the
      // only place the source name survives at that size. On a full tile the
      // caption is written on the tile and its own title carries the untruncated
      // version, so a second tooltip over the whole box would only get in the way.
      title={compact ? (label ?? undefined) : undefined}
      style={{
        // Two stops of the SAME hue rather than two different colours: a
        // two-hue gradient reads as a graphic in its own right and competes
        // with the photographs on neighbouring cards.
        backgroundImage:
          `linear-gradient(145deg, rgba(${rgb},0.14) 0%, rgba(${rgb},0.05) 55%, rgba(${rgb},0.10) 100%)`,
      }}
    >
      {/* The only full-strength use of the hue on the tile, and the only part
          of the identity that survives at 64px. It sits above the card's pill
          overlay row rather than under it: the pills are inset by p-2.5, so a
          2px rule along the very top edge is never covered. */}
      <span
        aria-hidden
        className="pointer-events-none absolute inset-x-0 top-0 h-0.5"
        style={{ background: `rgba(${rgb},0.45)` }}
      />

      {/* The monogram, oversized and nearly transparent. This is the part that
          makes the tile look designed rather than empty: it fills the space a
          photograph would have filled, at a weight that never competes with the
          headline in front of it. aria-hidden — it is the label below, restated
          as texture, and a screen reader announcing "OA" helps nobody.

          `select-none` because dragging across a card should select the
          headline, not two letters of decoration that would paste as garbage.

          Compact is the size the detail dialog uses, and 1.1rem there was two
          small letters in a tinted box: the exact "an image failed" look this
          file exists to avoid. Big enough to read as a mark, small enough that
          a 64px list row still contains it. */}
      {logo ? (
        /* The mark, centred, at app-icon size. `aspect-square` and rounded
           corners because most marks arrive as a phone home-screen icon, a
           square with its own background, and drawn any other way it looks
           like a sticker dropped on the tile. The caption underneath names
           whose it is; the picture itself says nothing a screen reader needs. */
        <span
          aria-hidden
          className="pointer-events-none absolute inset-0 flex select-none items-center justify-center"
        >
          <img
            src={logo}
            alt=""
            draggable={false}
            onError={() => setLogoBroken(true)}
            // Sized by the tile's HEIGHT on the card: the card's tile is a short
            // fixed band (h-40), and a width-based square there ran up under the
            // freshness pill and down over the caption.
            className={
              compact
                ? "aspect-square w-[58%] rounded-lg object-contain"
                : "aspect-square h-[45%] w-auto rounded-2xl object-contain"
            }
          />
        </span>
      ) : (
        <span
          aria-hidden
          className={
            compact
              ? "pointer-events-none absolute inset-0 flex select-none items-center justify-center font-semibold leading-none"
              : "pointer-events-none absolute -bottom-[0.18em] -right-[0.06em] select-none font-semibold leading-none"
          }
          style={{
            // Lighter on the full tile than it used to be: the caption below
            // crosses the letterforms, and muted text over a 22% wash of the same
            // hue was the weakest text on the card.
            color: `rgba(${rgb},${compact ? 0.45 : 0.18})`,
            fontSize: compact ? "2.25rem" : "7.5rem",
            letterSpacing: "-0.04em",
          }}
        >
          {mono}
        </span>
      )}

      {!compact && (
        /* `pt-11` clears the card's overlay row. Every tile in the app sits
           under one — the freshness pill on a news card, the outperformance
           pill on a competitor one — and they are absolutely positioned at
           top-0, so a headline starting at p-4 renders UNDER the pill. That is
           exactly what happened the first time: "7d ago" sat on top of the
           first two words of the story. */
        <div className="relative flex h-full flex-col gap-3 p-4 pt-11">
          {/* The headline's room: everything the caption leaves. `min-h-0` is
              what lets it be smaller than the headline, so the caption below
              keeps its place and the clamp, not the tile's edge, decides where
              the text stops. */}
          <div ref={headlineRoom} className="min-h-0 flex-1 overflow-hidden">
            {title && (
              /* On this tile the headline is not a caption for a picture, it
                 IS the card's content, so it is set at the size the card's own
                 heading would be. `title` carries the untruncated text: a
                 clamped heading with no way to read the rest is the one
                 truncation the rules call critical. */
              <h3
                ref={headline}
                title={title}
                className="line-clamp-5 text-sm font-semibold leading-snug tracking-[-0.01em] text-fg"
                style={{ WebkitLineClamp: headlineLines }}
              >
                {title}
              </h3>
            )}
          </div>
          {label && (
            /* The hairline is what stops this reading as a word dropped at the
               bottom of an empty box. It is the same hue as everything else on
               the tile, at the lowest strength of the three. */
            <p
              title={label}
              className="flex shrink-0 items-center gap-1.5 border-t pt-2 font-mono text-3xs uppercase tracking-wider2 text-muted"
              style={{ borderColor: `rgba(${rgb},0.22)` }}
            >
              {/* Decorative: it repeats what the label already says in words,
                  and it must not shrink when a long publisher name truncates. */}
              {Icon && (
                <Icon className="h-3 w-3 shrink-0" strokeWidth={1.8} aria-hidden />
              )}
              <span className="truncate">{label}</span>
            </p>
          )}
        </div>
      )}
    </div>
  );
}
