// Rebrand studio — one icon, small: in the list, the picker and the lane.
//
// A library icon is drawn inline from its Phosphor path (the same shape the
// server stamps, in the weight its look draws: bold for the circle and the
// chip); one the AI drew is its PNG, as a mask in the ink colour. With a
// `look` it sits on its body in the colours the plan chose — a circle, a
// small pill for the chip (the row already prints its words), the 42 badge
// (the 45° cut corner) of older versions, or the bare glyph — so the list
// already shows what the frame will. `badge` is the old shorthand for the
// badge look.

import { useEffect, useState } from "react";

import { iconGlyphUrl, type HarnessIconGlyph, type HarnessIconStyle } from "../../../lib/rebrandHarnessApi";
import { loadArt, onArt, pathOf, type GlyphWeight } from "./iconArt";

/** How a small icon is drawn. */
export interface IconGlyphLook {
  style: HarnessIconStyle;
  fill: string;
  ink: string;
  weight?: GlyphWeight;
}

function usePath(glyph: HarnessIconGlyph, weight: GlyphWeight, d?: string | null): string | null {
  const custom = glyph.source === "custom" || /^c-[0-9a-f]{12}$/.test(glyph.id);
  const [, bump] = useState(0);
  const known = d ?? (custom ? null : pathOf(glyph.id, weight));
  const { id, source } = glyph;
  useEffect(() => {
    if (custom || known) return;
    const off = onArt(() => bump((n) => n + 1));
    void loadArt({ id, source }, weight);
    return () => {
      off();
    };
  }, [custom, known, id, source, weight]);
  return known;
}

export function IconGlyph({
  glyph,
  d = null,
  size = 20,
  badge = false,
  look = null,
  fill = "#7f2bf2",
  ink = "#ffffff",
  className = "",
  title,
}: {
  glyph: HarnessIconGlyph;
  /** Its path in the look's weight, when the caller has it (else the
   *  cache, else the server). */
  d?: string | null;
  size?: number;
  /** On the 42 badge, in `fill` / `ink` (the old shorthand for `look`). */
  badge?: boolean;
  /** On its body, in its own colours. */
  look?: IconGlyphLook | null;
  fill?: string;
  ink?: string;
  className?: string;
  title?: string;
}) {
  const shown: IconGlyphLook | null = look ?? (badge ? { style: "badge", fill, ink } : null);
  const weight: GlyphWeight = shown?.weight ?? (shown && (shown.style === "circle" || shown.style === "chip") ? "bold" : "fill");
  const path = usePath(glyph, weight, d);
  const custom = glyph.source === "custom" || /^c-[0-9a-f]{12}$/.test(glyph.id);
  // The glyph's own colour: the ink on a body, the fill for the bare glyph.
  const colour = !shown ? "currentColor" : shown.style === "glyph" ? shown.fill : shown.ink;
  const share = !shown ? 1 : shown.style === "circle" ? 0.53 : shown.style === "chip" ? 0.6 : shown.style === "glyph" ? 0.8 : 0.6;
  const inner = size * share;
  const art = custom ? (
    <span
      aria-hidden
      className="block"
      style={{
        width: inner,
        height: inner,
        background: colour,
        WebkitMaskImage: `url(${iconGlyphUrl(glyph.id)})`,
        maskImage: `url(${iconGlyphUrl(glyph.id)})`,
        WebkitMaskSize: "contain",
        maskSize: "contain",
        WebkitMaskRepeat: "no-repeat",
        maskRepeat: "no-repeat",
        WebkitMaskPosition: "center",
        maskPosition: "center",
      }}
    />
  ) : (
    <svg aria-hidden viewBox="0 0 256 256" width={inner} height={inner} className="block">
      {path && <path d={path} fill={colour} />}
    </svg>
  );
  if (!shown || shown.style === "glyph") {
    return (
      <span
        className={`inline-flex shrink-0 items-center justify-center ${className}`}
        style={shown ? { width: size, height: size } : undefined}
        title={title}
      >
        {art}
      </span>
    );
  }
  if (shown.style === "circle" || shown.style === "chip") {
    // A disc; the chip a short pill with its glyph on the left.
    const chip = shown.style === "chip";
    return (
      <span
        className={`relative inline-flex shrink-0 items-center rounded-full shadow-[0_1px_2px_rgba(0,0,0,0.3)] ${
          chip ? "justify-start" : "justify-center"
        } ${className}`}
        style={{
          width: chip ? size * 1.45 : size,
          height: chip ? size * 0.82 : size,
          paddingLeft: chip ? size * 0.18 : undefined,
          background: shown.fill,
        }}
        title={title}
      >
        {art}
      </span>
    );
  }
  // The badge: a square with its top-right corner cut at 45° (legs 0.24 L).
  return (
    <span
      className={`relative inline-flex shrink-0 items-center justify-center ${className}`}
      style={{ width: size, height: size }}
      title={title}
    >
      <svg aria-hidden viewBox="0 0 100 100" width={size} height={size} className="absolute inset-0">
        <polygon points="0,0 76,0 100,24 100,100 0,100" fill={shown.fill} />
      </svg>
      <span className="relative">{art}</span>
    </span>
  );
}
