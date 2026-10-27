import { useMemo, useState } from "react";
import { Shuffle } from "lucide-react";

import { luminance, vividness } from "./colour";
import { pairFor, type BrandKit } from "./kit";
import { fontStack } from "./useBrandFonts";

/**
 * The whole profile in one picture: an Instagram-sized post painted with the
 * brand's own colours, set in its own font, saying one of its own lines.
 * "Try another" walks through the colours and phrases. Built only from the
 * profile; it is a preview, nothing is made or saved.
 */
export function BrandPreview({
  kit,
  font,
  lines,
}: {
  kit: BrandKit;
  font: string | null;
  /** Taglines first; the voice words if there are none. */
  lines: string[];
}) {
  // The most colourful ground first (purple before charcoal for 42): it is
  // the one that says "this is our brand" at a glance.
  const looks = useMemo(
    () => kit.paintable.map((bg) => pairFor(bg, kit, true)).sort((a, b) => vividness(b.bg.rgb) - vividness(a.bg.rgb)),
    [kit],
  );
  const [n, setN] = useState(0);

  if (looks.length === 0 && lines.length === 0) return null;

  const look = looks.length ? looks[n % looks.length] : null;
  const line = lines.length ? lines[n % lines.length] : "42 Abu Dhabi";
  const bg = look?.bg.hex ?? "#313131";
  const fg = look?.fg ?? "#FFFFFF";
  const accent = look?.accent ?? "#00E5FC";
  const lightGround = look ? luminance(look.bg.rgb) > 0.45 : false;
  const size = line.length <= 26 ? 34 : line.length <= 52 ? 27 : 21;
  const canShuffle = looks.length > 1 || lines.length > 1;

  return (
    <figure className="w-full max-w-[340px] mx-auto lg:mx-0 space-y-3">
      <div
        className="relative aspect-square w-full overflow-hidden border border-k-border shadow-[-6px_6px_0_0_rgb(var(--k-btn)/0.35)] transition-[background-color,color] duration-700 ease-[cubic-bezier(0.16,1,0.3,1)]"
        style={{ backgroundColor: bg, color: fg }}
        role="img"
        aria-label={`A sample post: "${line}", ${look ? `${look.bg.hex} background with ${fg} text` : "in the 42 colours"}${font ? `, in ${font}` : ""}.`}
      >
        <div className="absolute inset-x-5 top-5 flex items-center justify-between gap-2" aria-hidden>
          <span className="flex items-center gap-2">
            <img
              src="/42-logo.png"
              alt=""
              className="h-6 w-6 object-contain transition-[filter] duration-700"
              style={{ filter: lightGround ? "invert(1)" : "none" }}
            />
            <span className="text-[10.5px] font-bold uppercase tracking-[0.14em]" style={{ fontFamily: fontStack(font) }}>
              Abu Dhabi
            </span>
          </span>
          <span className="k-pixel text-[13px]" style={{ color: accent }}>
            &lt;42/&gt;
          </span>
        </div>

        <div className="absolute inset-x-5 bottom-14 space-y-3" aria-hidden>
          <span className="block h-1 w-12 transition-colors duration-700" style={{ backgroundColor: accent }} />
          <p
            key={line + n}
            className="k-rise font-bold leading-[1.08] text-balance break-words"
            style={{ fontFamily: fontStack(font), fontSize: size }}
          >
            {line}
          </p>
        </div>

        <div className="absolute inset-x-5 bottom-5 flex items-center justify-between text-[11px] font-semibold opacity-80" aria-hidden>
          <span style={{ fontFamily: fontStack(font) }}>@42abudhabi</span>
          {looks.length > 1 && (
            <span className="flex gap-1">
              {looks.slice(0, 8).map((l, i) => (
                <span
                  key={l.bg.hex}
                  className="h-1.5 w-1.5 transition-opacity"
                  style={{ backgroundColor: fg, opacity: i === n % looks.length ? 1 : 0.3 }}
                />
              ))}
            </span>
          )}
        </div>
      </div>

      <figcaption className="space-y-2.5">
        <p className="text-[12.5px] leading-snug text-fg/60">A post in our colours, font and words</p>
        <div className="flex items-center justify-between gap-3">
          <span className="text-[12px] text-fg/45 tabular-nums min-w-0 truncate">
            {look ? `${look.bg.hex} with ${fg}` : "42 colours"}
            {font ? ` · ${font}` : ""}
          </span>
          {canShuffle && (
            <button type="button" onClick={() => setN((x) => x + 1)} className="k-btn k-btn-sm shrink-0">
              <Shuffle className="w-3.5 h-3.5" strokeWidth={2} aria-hidden />
              Try another
            </button>
          )}
        </div>
      </figcaption>
    </figure>
  );
}
