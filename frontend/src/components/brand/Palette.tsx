import { useState } from "react";
import { AlertTriangle, Check, Copy } from "lucide-react";

import { useToast } from "../ToastProvider";
import { ChartGate } from "../ChartGate";
import { READABILITY_WORDS, inkOn, plainName, readability, type BrandColour } from "./colour";
import { clashes, pairFor, type BrandKit } from "./kit";
import { fontStack } from "./useBrandFonts";

/**
 * Brand colours painted, the way a brand guide shows them: the main colours
 * as one wide band, the supporting ones as a thinner band under it, then
 * what to write on each colour and which pairs can't be read.
 * Click any colour to copy its code.
 */
export function Palette({ kit, font }: { kit: BrandKit; font: string | null }) {
  const pairs = kit.paintable.map((bg) => pairFor(bg, kit, false));
  const avoid = clashes(kit);

  return (
    <div className="space-y-6">
      {kit.main.length > 0 && <Band label="Main colours" colours={kit.main} big />}
      {kit.support.length > 0 && <Band label="Supporting colours" colours={kit.support} />}

      {pairs.length > 0 && (
        <div className="space-y-2.5">
          <p className="text-[13px] font-medium text-fg/60">What to write on each colour</p>
          <ChartGate>
            <ul className="grid grid-cols-2 sm:grid-cols-3 xl:grid-cols-4 gap-3">
              {pairs.map((p, i) => {
                const level = readability(p.ratio);
                return (
                  <li
                    key={p.bg.hex}
                    className="k-rise border border-k-border overflow-hidden"
                    style={{ animationDelay: `${i * 60}ms` }}
                  >
                    <div className="h-[92px] px-4 py-3 flex flex-col justify-between" style={{ backgroundColor: p.bg.hex, color: p.fg }}>
                      <span className="text-[30px] font-bold leading-none" style={{ fontFamily: fontStack(font) }} aria-hidden>
                        Aa
                      </span>
                      <span className="text-[12px] font-semibold truncate" style={{ fontFamily: fontStack(font) }} aria-hidden>
                        Hello, World.
                      </span>
                    </div>
                    <div className="px-3 py-2.5 bg-k-inset space-y-0.5">
                      <p className="text-[12.5px] text-fg leading-snug">
                        On <b className="font-semibold">{p.bg.name || plainName(p.bg.rgb)}</b>, write in{" "}
                        <b className="font-semibold">
                          {p.fgColour ? p.fgColour.name || plainName(p.fgColour.rgb) : p.fg === "#FFFFFF" ? "white" : "black"}
                        </b>
                      </p>
                      <p className="flex items-center gap-1.5 text-[11.5px] text-fg/55 tabular-nums">
                        <span
                          className={[
                            "inline-block w-1.5 h-1.5",
                            level === "easy" || level === "good" ? "bg-k-accent" : level === "large" ? "bg-fg/50" : "bg-k-coral",
                          ].join(" ")}
                          aria-hidden
                        />
                        {READABILITY_WORDS[level]} · {p.ratio.toFixed(1)}:1
                      </p>
                    </div>
                  </li>
                );
              })}
            </ul>
          </ChartGate>
        </div>
      )}

      {avoid.length > 0 && (
        <div className="space-y-2.5">
          <p className="flex items-center gap-1.5 text-[13px] font-medium text-fg/60">
            <AlertTriangle className="w-3.5 h-3.5 text-k-coral" strokeWidth={2} aria-hidden />
            Hard to read together, keep them apart for text
          </p>
          <ul className="flex flex-wrap gap-3">
            {avoid.map((p) => (
              <li key={p.bg.hex + p.fg.hex} className="flex items-center gap-3 border border-k-border bg-k-inset pr-3">
                <span
                  className="h-12 w-20 flex items-center justify-center text-[17px] font-bold"
                  style={{ backgroundColor: p.bg.hex, color: p.fg.hex, fontFamily: fontStack(font) }}
                  aria-hidden
                >
                  Aa
                </span>
                <span className="text-[12.5px] leading-snug text-fg/75">
                  {p.fg.name || plainName(p.fg.rgb)} on {p.bg.name || plainName(p.bg.rgb)}
                  <span className="block text-[11.5px] text-fg/50 tabular-nums">{p.ratio.toFixed(1)}:1</span>
                </span>
              </li>
            ))}
          </ul>
        </div>
      )}
    </div>
  );
}

function Band({ label, colours, big = false }: { label: string; colours: BrandColour[]; big?: boolean }) {
  return (
    <div className="space-y-2.5">
      <p className="text-[13px] font-medium text-fg/60">
        {label} <span className="text-fg/40 tabular-nums">· {colours.length}</span>
      </p>
      <ChartGate>
        <ul className="flex flex-col sm:flex-row border border-k-border overflow-hidden">
          {colours.map((c, i) => (
            <li
              key={c.raw + i}
              className={[
                "flex min-w-0 sm:basis-0 sm:grow transition-[flex-grow] duration-500 ease-[cubic-bezier(0.16,1,0.3,1)] sm:hover:grow-[1.7] sm:focus-within:grow-[1.7]",
                big ? "h-[84px] sm:h-[200px]" : "h-[64px] sm:h-[104px]",
              ].join(" ")}
            >
              <Swatch colour={c} big={big} delay={i * 70} />
            </li>
          ))}
        </ul>
      </ChartGate>
    </div>
  );
}

function Swatch({ colour, big, delay }: { colour: BrandColour; big: boolean; delay: number }) {
  const toast = useToast();
  const [copied, setCopied] = useState(false);
  const code = colour.hex ?? colour.raw;
  const ink = colour.rgb ? inkOn(colour.rgb) : undefined;
  const name = colour.name || (colour.rgb ? plainName(colour.rgb) : null);

  async function copy() {
    try {
      await navigator.clipboard.writeText(code);
      setCopied(true);
      toast.info("Colour code copied", code);
      window.setTimeout(() => setCopied(false), 1400);
    } catch {
      toast.error("Couldn't copy", `The colour code is ${code}`);
    }
  }

  if (!colour.rgb || !colour.hex) {
    return (
      <div
        className="k-hatch w-full flex flex-col justify-end gap-0.5 px-3 py-2.5 min-w-0"
        title="This line has no colour code the page can show"
      >
        <span className="text-[12.5px] font-semibold text-fg truncate">{colour.raw || "Empty"}</span>
        <span className="text-[11px] text-fg/55">No colour code</span>
      </div>
    );
  }

  return (
    <button
      type="button"
      onClick={copy}
      title={`Copy ${colour.hex}`}
      aria-label={`${name ?? colour.hex}, ${colour.hex}. Copy the code`}
      className="k-wipe group relative w-full text-left flex sm:flex-col sm:justify-end items-center sm:items-stretch justify-between gap-2 px-3.5 py-3 min-w-0 focus:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-k-btn"
      style={{ backgroundColor: colour.hex, color: ink, animationDelay: `${delay}ms` }}
    >
      <span className="absolute right-3 top-3 hidden sm:block opacity-0 group-hover:opacity-80 group-focus-visible:opacity-80 transition-opacity" aria-hidden>
        {copied ? <Check className="w-4 h-4" strokeWidth={2.4} /> : <Copy className="w-4 h-4" strokeWidth={1.8} />}
      </span>
      <span className="min-w-0 space-y-0.5">
        {colour.approx && (
          <span
            className="block text-[10.5px] font-semibold uppercase tracking-[0.06em] opacity-75"
            title={`The profile says "${colour.raw}" without a code, so this is the closest standard colour`}
          >
            Close match
          </span>
        )}
        {name && (
          <span className={["block font-semibold leading-tight truncate", big ? "text-[15px]" : "text-[12.5px]"].join(" ")}>
            {name}
          </span>
        )}
        <span className={["block k-num uppercase", big ? "text-[13.5px]" : "text-[11.5px]", name ? "opacity-80" : "font-semibold"].join(" ")}>
          {colour.hex}
        </span>
        {big && (
          <span className="hidden sm:block text-[11.5px] opacity-65 tabular-nums">
            RGB {colour.rgb.join(" ")}
          </span>
        )}
        {colour.note && <span className={["hidden sm:block opacity-70 truncate", big ? "text-[11.5px]" : "text-[11px]"].join(" ")}>{colour.note}</span>}
      </span>
      <span className="sm:hidden shrink-0 opacity-70" aria-hidden>
        {copied ? <Check className="w-4 h-4" strokeWidth={2.4} /> : <Copy className="w-4 h-4" strokeWidth={1.8} />}
      </span>
    </button>
  );
}
