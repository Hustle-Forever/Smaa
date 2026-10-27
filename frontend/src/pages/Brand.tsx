import { useEffect, useMemo, useState } from "react";
import type { ComponentType, ReactNode } from "react";
import {
  AlertTriangle,
  ArrowRight,
  Bot,
  Check,
  Copy,
  FileText,
  Film,
  Globe2,
  Image as ImageIcon,
  ListChecks,
  MessagesSquare,
  Mic2,
  Palette as PaletteIcon,
  Quote,
  RotateCcw,
  Sparkles,
  Type,
  Users,
  X,
} from "lucide-react";

import { api } from "../lib/api";
import { fmtNumber, fmtRel } from "../lib/format";
import type { BrandResponse } from "../lib/types";
import { Skeleton } from "../components/Skeleton";
import { useToast } from "../components/ToastProvider";
import { ChartGate } from "../components/ChartGate";
import { BrandPreview } from "../components/brand/BrandPreview";
import { Palette } from "../components/brand/Palette";
import { VisualBoard } from "../components/brand/VisualBoard";
import { vividness } from "../components/brand/colour";
import { buildKit, pairFor, type BrandKit } from "../components/brand/kit";
import { topicIcon } from "../components/brand/topicIcon";
import { fontStack, standInFor, useBrandFonts, type FontState } from "../components/brand/useBrandFonts";

/**
 * The synthesized brand profile, laid out as a visual brand book: a sample
 * post painted with the brand, a map of the sections, then each part of the
 * profile shown rather than only written (colours painted, fonts set,
 * audiences and themes with pictures, phrases as posters, rules as a
 * checklist). Every field the backend sends is shown. The only data is
 * GET /api/brand, plus two GET lists for the pictures of the visual themes.
 */
export function BrandPage({
  embedded = false,
  onOpenSynthesizer,
}: {
  embedded?: boolean;
  /** Where "Build the profile" goes from the empty state (Brand workspace). */
  onOpenSynthesizer?: () => void;
} = {}) {
  const [data, setData] = useState<BrandResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    api
      .brand()
      .then((d) => {
        if (cancelled) return;
        setData(d);
      })
      .catch((e) => setError(String(e?.message || e)))
      .finally(() => setLoading(false));
    return () => {
      cancelled = true;
    };
  }, []);

  const vi = data?.payload?.visual_identity;
  const kit = useMemo(() => buildKit(vi?.primary_colors, vi?.secondary_colors), [vi?.primary_colors, vi?.secondary_colors]);
  const fontStates = useBrandFonts(vi?.fonts);

  if (loading) {
    return (
      <div className="space-y-4" aria-busy="true" aria-label="Loading brand profile">
        <Skeleton h="h-80" rounded="rounded-2xl" />
        <Skeleton h="h-32" rounded="rounded-2xl" />
        <div className="grid md:grid-cols-2 gap-4">
          <Skeleton h="h-40" rounded="rounded-2xl" />
          <Skeleton h="h-40" rounded="rounded-2xl" />
        </div>
      </div>
    );
  }

  if (error) {
    return (
      <div className="glass px-5 py-4 flex items-start gap-3" role="alert">
        <AlertTriangle className="w-5 h-5 text-k-danger shrink-0 mt-0.5" strokeWidth={1.8} aria-hidden />
        <div className="space-y-0.5 min-w-0">
          <p className="text-[14px] font-semibold text-fg">Couldn't load the brand profile</p>
          <p className="text-[13px] text-fg/60 break-words">{error}</p>
        </div>
      </div>
    );
  }

  if (!data?.payload) {
    return (
      <div className="glass px-6 py-12 flex flex-col items-center text-center gap-3">
        <span className="icon-well w-12 h-12">
          <Sparkles className="w-5 h-5" strokeWidth={1.5} aria-hidden />
        </span>
        <div className="space-y-1 max-w-md">
          <p className="text-[15px] font-semibold text-fg">No brand profile yet</p>
          <p className="text-[13.5px] text-fg/60">
            The AI builds it by reading the website and your documents. Open the Synthesizer and press
            Re-synthesize to create it.
          </p>
        </div>
        {onOpenSynthesizer && (
          <button type="button" onClick={onOpenSynthesizer} className="k-btn">
            Open the Synthesizer
            <ArrowRight className="w-4 h-4" strokeWidth={2} aria-hidden />
          </button>
        )}
      </div>
    );
  }

  const p = data.payload;
  const r = data.run;
  const s = data.sources;
  const fonts = p.visual_identity?.fonts ?? [];
  const mainFont = fonts[0] ?? null;
  const pillars = p.voice?.pillars ?? [];
  const taglines = p.taglines_in_use ?? [];
  const styles = p.visual_identity?.style_pillars ?? [];

  // What the AI read, in words.
  const readFrom = [
    { n: s.website_pages ?? 0, one: "web page", many: "web pages", Icon: Globe2 },
    { n: s.image_descriptions ?? 0, one: "image", many: "images", Icon: ImageIcon },
    { n: s.video_summaries ?? 0, one: "video", many: "videos", Icon: Film },
    { n: s.owner_documents ?? 0, one: "document", many: "documents", Icon: FileText },
  ];

  const hasVoice = !!p.voice && pillars.length + (p.voice.do?.length ?? 0) + (p.voice.dont?.length ?? 0) > 0;
  const colourCount = kit.main.length + kit.support.length;
  const hasColours = colourCount + styles.length > 0;
  const hasRules = (p.brand_dos?.length ?? 0) + (p.brand_donts?.length ?? 0) > 0;
  const notes = splitSentences(p.notes_for_producer_agent ?? "");

  // The map of the book: one tile per section that exists, each with a
  // small picture of what's inside.
  const tiles: Tile[] = [
    hasVoice && {
      id: "brand-voice",
      label: "Voice",
      Icon: Mic2,
      count: pillars.length || undefined,
      preview: <span className="line-clamp-2">{pillars.length ? pillars.join(" · ") : "Do and don't"}</span>,
    },
    hasColours && {
      id: "brand-colours",
      label: "Colours",
      Icon: PaletteIcon,
      count: colourCount || undefined,
      preview: kit.paintable.length ? (
        <span className="flex h-5 w-full overflow-hidden border border-k-border">
          {kit.paintable.slice(0, 8).map((c) => (
            <span key={c.hex} className="flex-1" style={{ backgroundColor: c.hex }} />
          ))}
        </span>
      ) : (
        <span className="truncate">{styles.join(" · ")}</span>
      ),
    },
    fonts.length > 0 && {
      id: "brand-type",
      label: "Typefaces",
      Icon: Type,
      count: fonts.length,
      preview: (
        <span className="flex items-baseline gap-3 min-w-0">
          {fonts.slice(0, 3).map((f) => (
            <span key={f} className="text-[22px] leading-none text-fg" style={{ fontFamily: fontStack(f) }} title={f}>
              Aa
            </span>
          ))}
          <span className="truncate">{fonts[0]}</span>
        </span>
      ),
    },
    (p.audiences?.length ?? 0) > 0 && {
      id: "brand-audiences",
      label: "Audiences",
      Icon: Users,
      count: p.audiences!.length,
      preview: <IconStack items={p.audiences!} />,
    },
    (p.message_themes?.length ?? 0) > 0 && {
      id: "brand-messages",
      label: "Messages",
      Icon: MessagesSquare,
      count: p.message_themes!.length,
      preview: <IconStack items={p.message_themes!} />,
    },
    (p.visual_themes?.length ?? 0) > 0 && {
      id: "brand-visuals",
      label: "Visuals",
      Icon: ImageIcon,
      count: p.visual_themes!.length,
      preview: <IconStack items={p.visual_themes!} />,
    },
    taglines.length > 0 && {
      id: "brand-phrases",
      label: "Phrases",
      Icon: Quote,
      count: taglines.length,
      preview: <span className="line-clamp-2 italic">&ldquo;{taglines[0]}&rdquo;</span>,
    },
    hasRules && {
      id: "brand-rules",
      label: "Rules",
      Icon: ListChecks,
      count: (p.brand_dos?.length ?? 0) + (p.brand_donts?.length ?? 0),
      preview: <RuleBar always={p.brand_dos?.length ?? 0} never={p.brand_donts?.length ?? 0} />,
    },
    notes.length > 0 && {
      id: "brand-notes",
      label: "Notes for the AI",
      Icon: Bot,
      count: notes.length,
      preview: <span className="line-clamp-2">{notes[0]}</span>,
    },
  ].filter(Boolean) as Tile[];

  return (
    <article className="space-y-5">
      {/* Summary + the brand in one picture */}
      <section className="glass p-6 md:p-8 animate-reveal grid grid-cols-1 gap-8 lg:grid-cols-[minmax(0,1fr)_340px] lg:items-start">
        <div className="space-y-5 min-w-0">
          <div className="flex items-center gap-3">
            <span className="w-10 h-10 rounded-none bg-[#313131] p-2 shrink-0">
              <img src="/42-logo.png" alt="" className="h-full w-full object-contain" />
            </span>
            <div>
              <h2 className="text-[20px] font-semibold tracking-[-0.02em] text-fg leading-tight">42 Abu Dhabi</h2>
              <p className="text-[13px] text-fg/55" title={r ? `Run #${r.id}${r.notes ? ` · ${r.notes}` : ""}` : undefined}>
                {r?.finished_at ? `Profile updated ${fmtRel(r.finished_at)}` : "Brand profile"}
              </p>
            </div>
          </div>

          {p.brand_summary && (
            <p className="text-[16.5px] md:text-[17.5px] leading-[1.65] text-fg/85 max-w-[68ch] text-pretty whitespace-pre-line">
              {p.brand_summary}
            </p>
          )}

          <div className="pt-4 border-t border-fg/[0.07] space-y-2.5">
            <p className="text-[12.5px] text-fg/50">Built from</p>
            <ul className="grid grid-cols-2 sm:grid-cols-4 gap-2">
              {readFrom.map((x) => (
                <li key={x.one} className="flex items-center gap-2.5 border border-k-border bg-k-inset px-3 py-2">
                  <x.Icon className="w-4 h-4 text-k-accent shrink-0" strokeWidth={1.8} aria-hidden />
                  <span className="min-w-0 leading-tight">
                    <b className="block k-num text-[17px] text-fg">{fmtNumber(x.n)}</b>
                    <span className="text-[11.5px] text-fg/55">{x.n === 1 ? x.one : x.many}</span>
                  </span>
                </li>
              ))}
            </ul>
          </div>
        </div>

        <BrandPreview kit={kit} font={mainFont} lines={taglines.length ? taglines : pillars} />
      </section>

      {/* The map of the book */}
      {tiles.length > 1 && (
        <nav aria-label="Brand book sections" className="animate-reveal" style={{ animationDelay: "60ms" }}>
          <ul className="flex gap-3 overflow-x-auto snap-x snap-mandatory -mx-1 px-1 pt-1 pb-2 md:grid md:grid-cols-3 md:overflow-visible md:pb-0">
            {tiles.map((t, i) => (
              <li key={t.id} className="snap-start shrink-0 w-[168px] md:w-auto k-rise" style={{ animationDelay: `${80 + i * 40}ms` }}>
                <button
                  type="button"
                  onClick={() => document.getElementById(t.id)?.scrollIntoView({ behavior: "smooth", block: "start" })}
                  className="k-panel k-hover w-full h-full min-h-[118px] p-4 text-left flex flex-col gap-3 focus:outline-none focus-visible:ring-2 focus-visible:ring-k-btn"
                >
                  <span className="flex items-center justify-between gap-2 w-full">
                    <span className="flex items-center gap-2 min-w-0">
                      <t.Icon className="w-4 h-4 text-k-accent shrink-0" strokeWidth={1.8} aria-hidden />
                      <span className="k-title text-[12.5px] tracking-[0.06em] before:content-none truncate">{t.label}</span>
                    </span>
                    {t.count != null && <span className="k-num text-[15px] text-fg/70 tabular-nums">{t.count}</span>}
                  </span>
                  <span className="mt-auto w-full min-w-0 text-[12.5px] leading-snug text-fg/60">{t.preview}</span>
                </button>
              </li>
            ))}
          </ul>
        </nav>
      )}

      {/* Voice */}
      {hasVoice && p.voice && (
        <BookSection id="brand-voice" Icon={Mic2} title="How we sound" sub="The tone every caption and script should follow">
          {pillars.length > 0 && (
            <ChartGate>
              <ul className="flex flex-wrap items-baseline gap-x-3 gap-y-1" aria-label="Voice words">
                {pillars.map((w, i) => (
                  <li key={i} className="k-rise flex items-baseline gap-3" style={{ animationDelay: `${i * 90}ms` }}>
                    {i > 0 && (
                      <span className="k-pixel k-code text-[22px] md:text-[28px]" aria-hidden>
                        /
                      </span>
                    )}
                    <span
                      className={[
                        "k-display text-fg",
                        pillars.some((x) => x.length > 26) ? "text-[19px] md:text-[23px]" : "text-[26px] md:text-[36px]",
                      ].join(" ")}
                    >
                      {w}
                    </span>
                  </li>
                ))}
              </ul>
            </ChartGate>
          )}
          {((p.voice.do?.length ?? 0) > 0 || (p.voice.dont?.length ?? 0) > 0) && (
            <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
              <RuleList kind="do" title="Say it like this" items={p.voice.do ?? []} />
              <RuleList kind="dont" title="Not like this" items={p.voice.dont ?? []} />
            </div>
          )}
        </BookSection>
      )}

      {/* Colours */}
      {hasColours && (
        <BookSection
          id="brand-colours"
          Icon={PaletteIcon}
          title="Brand colours"
          sub="The real colours. Click one to copy its code"
          badge={<SourceBadge source={p._sources?.primary_colors} />}
        >
          {colourCount > 0 && <Palette kit={kit} font={mainFont} />}
          {styles.length > 0 && (
            <div className="space-y-2.5">
              <p className="text-[13px] font-medium text-fg/60">Visual style</p>
              <ul className="grid grid-cols-1 sm:grid-cols-2 xl:grid-cols-4 gap-3">
                {styles.map((st, i) => {
                  const Icon = topicIcon(st);
                  return (
                    <li key={i} className="flex items-center gap-3 border border-k-border bg-k-inset px-3.5 py-3">
                      <span className="k-well w-9 h-9 text-k-accent">
                        <Icon className="w-[18px] h-[18px]" strokeWidth={1.7} aria-hidden />
                      </span>
                      <span className="text-[13.5px] font-medium text-fg leading-snug">{st}</span>
                    </li>
                  );
                })}
              </ul>
            </div>
          )}
        </BookSection>
      )}

      {/* Typefaces */}
      {fonts.length > 0 && (
        <BookSection
          id="brand-type"
          Icon={Type}
          title="Typefaces"
          sub="The fonts used on posters and in videos"
          badge={<SourceBadge source={p._sources?.fonts} />}
        >
          <div className={["grid grid-cols-1 gap-3", fonts.length > 1 ? "md:grid-cols-2" : ""].join(" ")}>
            {fonts.map((f) => (
              <FontCard key={f} family={f} state={fontStates[f] ?? "loading"} sample={taglines[0]} />
            ))}
          </div>
        </BookSection>
      )}

      {/* Audiences */}
      {(p.audiences?.length ?? 0) > 0 && (
        <BookSection id="brand-audiences" Icon={Users} title="Who we talk to" sub="The people our posts are for">
          <ul className={`grid grid-cols-1 gap-3 ${colsFor(p.audiences!.length)}`}>
            {p.audiences!.map((a, i) => {
              const Icon = topicIcon(a.label, a.description);
              return (
                <li
                  key={i}
                  className="k-rise border border-k-border bg-k-inset p-4 md:p-5 flex flex-col gap-3"
                  style={{ animationDelay: `${i * 60}ms` }}
                >
                  <span className="w-12 h-12 flex items-center justify-center bg-k-accent/10 text-k-accent border border-k-accent/30">
                    <Icon className="w-6 h-6" strokeWidth={1.6} aria-hidden />
                  </span>
                  <div className="space-y-1.5">
                    {a.label && <p className="text-[15px] font-semibold text-fg leading-snug">{a.label}</p>}
                    {a.description && <p className="text-[13.5px] leading-[1.55] text-fg/65 text-pretty">{a.description}</p>}
                  </div>
                </li>
              );
            })}
          </ul>
        </BookSection>
      )}

      {/* Messages */}
      {(p.message_themes?.length ?? 0) > 0 && (
        <BookSection id="brand-messages" Icon={MessagesSquare} title="What we talk about" sub="The themes we come back to">
          <ul className="grid grid-cols-1 lg:grid-cols-2 gap-3">
            {p.message_themes!.map((m, i) => {
              const Icon = topicIcon(m.label, m.description);
              return (
                <li
                  key={i}
                  className="k-rise flex gap-4 border border-k-border border-l-[3px] border-l-k-accent bg-k-inset p-4"
                  style={{ animationDelay: `${i * 60}ms` }}
                >
                  <Icon className="w-7 h-7 text-k-accent shrink-0 mt-0.5" strokeWidth={1.5} aria-hidden />
                  <div className="space-y-1 min-w-0">
                    {m.label && <p className="k-title text-[14px] before:content-none leading-snug">{m.label}</p>}
                    {m.description && <p className="text-[13.5px] leading-[1.55] text-fg/65 text-pretty">{m.description}</p>}
                  </div>
                </li>
              );
            })}
          </ul>
        </BookSection>
      )}

      {/* Visuals: a mood board with our own pictures */}
      {(p.visual_themes?.length ?? 0) > 0 && (
        <BookSection id="brand-visuals" Icon={ImageIcon} title="What our visuals show" sub="The scenes and subjects in our images, with real examples">
          <VisualBoard themes={p.visual_themes!} />
        </BookSection>
      )}

      {/* Phrases as posters */}
      {taglines.length > 0 && (
        <BookSection id="brand-phrases" Icon={Quote} title="Phrases we use" sub="Lines that already appear in our content, shown in our colours. Click to copy">
          <PhrasePosters lines={taglines} kit={kit} font={mainFont} />
        </BookSection>
      )}

      {/* Brand rules as a checklist */}
      {hasRules && (
        <BookSection id="brand-rules" Icon={ListChecks} title="Brand rules" sub="Tick through these before a post goes out">
          <Checklist always={p.brand_dos ?? []} never={p.brand_donts ?? []} />
        </BookSection>
      )}

      {/* Notes for the AI */}
      {notes.length > 0 && (
        <BookSection id="brand-notes" Icon={Bot} title="Notes for the AI" sub="Extra guidance the AI reads before it writes or designs anything">
          <AiNotes lines={notes} />
        </BookSection>
      )}
    </article>
  );
}

/* ---------- building blocks ---------- */

interface Tile {
  id: string;
  label: string;
  Icon: ComponentType<any>;
  count?: number;
  preview: ReactNode;
}

function colsFor(n: number): string {
  if (n === 2 || n === 4) return "md:grid-cols-2 xl:grid-cols-4";
  if (n === 1) return "";
  return "md:grid-cols-2 xl:grid-cols-3";
}

/** "Posters work best... Always include..." -> one line per sentence. */
function splitSentences(text: string): string[] {
  return text
    .split(/\n+/)
    .flatMap((para) => para.split(/(?<=[.!?])\s+(?=[A-Z0-9"“'(])/))
    .map((x) => x.trim())
    .filter(Boolean);
}

function BookSection({
  id,
  Icon,
  title,
  sub,
  badge,
  children,
}: {
  id: string;
  Icon: ComponentType<any>;
  title: string;
  sub: string;
  badge?: ReactNode;
  children: ReactNode;
}) {
  return (
    <section id={id} className="glass p-5 md:p-6 space-y-5 scroll-mt-24" aria-labelledby={`${id}-title`}>
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="flex items-start gap-3 min-w-0">
          <span className="k-well w-10 h-10 text-k-accent">
            <Icon className="w-5 h-5" strokeWidth={1.6} aria-hidden />
          </span>
          <div className="min-w-0">
            <h3 id={`${id}-title`} className="k-title text-[16px]">
              {title}
            </h3>
            <p className="text-[13px] text-fg/55">{sub}</p>
          </div>
        </div>
        {badge}
      </div>
      {children}
    </section>
  );
}

function IconStack({ items }: { items: Array<{ label?: string; description?: string }> }) {
  return (
    <span className="flex items-center gap-2 min-w-0">
      <span className="flex -space-x-1.5 shrink-0" aria-hidden>
        {items.slice(0, 4).map((it, i) => {
          const Icon = topicIcon(it.label, it.description);
          return (
            <span key={i} className="w-7 h-7 flex items-center justify-center bg-k-inset border border-k-border text-k-accent">
              <Icon className="w-3.5 h-3.5" strokeWidth={1.8} />
            </span>
          );
        })}
      </span>
      <span className="truncate">{items[0]?.label}</span>
    </span>
  );
}

function RuleBar({ always, never }: { always: number; never: number }) {
  const total = Math.max(1, always + never);
  return (
    <span className="block space-y-1.5">
      <span className="flex h-1.5 w-full gap-0.5" aria-hidden>
        <span className="bg-k-accent" style={{ width: `${(always / total) * 100}%` }} />
        <span className="bg-k-coral" style={{ width: `${(never / total) * 100}%` }} />
      </span>
      <span className="block tabular-nums">
        {always} always · {never} never
      </span>
    </span>
  );
}

function RuleList({ kind, title, items }: { kind: "do" | "dont"; title: string; items: string[] }) {
  const isDo = kind === "do";
  return (
    <div className={["bg-k-inset border border-k-border border-t-[3px] p-4 md:p-5 space-y-3", isDo ? "border-t-k-accent" : "border-t-k-coral"].join(" ")}>
      <p className="flex items-center gap-2 text-[14px] font-semibold text-fg">
        <span
          className={["w-6 h-6 flex items-center justify-center", isDo ? "bg-k-accent text-k-canvas" : "bg-k-coral text-white"].join(" ")}
          aria-hidden
        >
          {isDo ? <Check className="w-3.5 h-3.5" strokeWidth={2.6} /> : <X className="w-3.5 h-3.5" strokeWidth={2.6} />}
        </span>
        {title}
      </p>
      {items.length === 0 ? (
        <p className="text-[13px] text-fg/45">Nothing listed yet.</p>
      ) : (
        <ul className="space-y-2">
          {items.map((d, i) => (
            <li key={i} className="flex gap-2.5 text-[14px] leading-snug text-fg/80">
              {isDo ? (
                <Check className="w-4 h-4 mt-[1px] text-k-accent shrink-0" strokeWidth={2.2} aria-hidden />
              ) : (
                <X className="w-4 h-4 mt-[1px] text-k-coral shrink-0" strokeWidth={2.2} aria-hidden />
              )}
              <span className="text-pretty">{d}</span>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

function FontCard({ family, state, sample }: { family: string; state: FontState; sample?: string }) {
  const style = { fontFamily: fontStack(family) };
  const note =
    state === "loading"
      ? "Loading the font…"
      : state === "real"
        ? "Shown in the real font"
        : `Not on Google Fonts, so ${standInFor(family)} stands in`;
  return (
    <div className="border border-k-border bg-k-inset p-5 space-y-4 min-w-0">
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <p className="text-[15px] font-semibold text-fg truncate">{family}</p>
          <p className="flex items-center gap-1.5 text-[12px] text-fg/55">
            <span
              className={["inline-block w-1.5 h-1.5", state === "real" ? "bg-k-accent" : state === "standin" ? "bg-k-coral" : "bg-fg/40 animate-pulse"].join(" ")}
              aria-hidden
            />
            {note}
          </p>
        </div>
      </div>
      <p className="text-[84px] md:text-[96px] leading-[0.9] text-fg tracking-[-0.02em]" style={style} aria-hidden>
        Aa<span className="text-k-accent">g</span>
      </p>
      <p className="text-[15px] leading-[1.5] text-fg/75 break-words" style={style} aria-hidden>
        ABCDEFGHIJKLMNOPQRSTUVWXYZ
        <br />
        abcdefghijklmnopqrstuvwxyz
        <br />
        0123456789 !?&amp;@
      </p>
      <div className="flex flex-wrap items-baseline gap-x-6 gap-y-1 pt-3 border-t border-k-border" aria-hidden>
        <span className="text-[20px] text-fg" style={{ ...style, fontWeight: 400 }}>
          Regular
        </span>
        <span className="text-[20px] text-fg" style={{ ...style, fontWeight: 700 }}>
          Bold
        </span>
      </div>
      {sample && (
        <p className="text-[21px] font-bold leading-snug text-fg text-balance" style={style}>
          {sample}
        </p>
      )}
    </div>
  );
}

function PhrasePosters({ lines, kit, font }: { lines: string[]; kit: BrandKit; font: string | null }) {
  // Colourful grounds first, so the posters read as the brand, not as panels.
  const looks = useMemo(
    () => kit.paintable.map((bg) => pairFor(bg, kit, true)).sort((a, b) => vividness(b.bg.rgb) - vividness(a.bg.rgb)),
    [kit],
  );
  return (
    <ChartGate>
      <ul className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-3">
        {lines.map((t, i) => {
          const look = looks.length ? looks[i % looks.length] : null;
          return (
            <li key={i} className="k-rise" style={{ animationDelay: `${i * 80}ms` }}>
              <Poster text={t} bg={look?.bg.hex} fg={look?.fg} accent={look?.accent ?? null} font={font} />
            </li>
          );
        })}
      </ul>
    </ChartGate>
  );
}

function Poster({ text, bg, fg, accent, font }: { text: string; bg?: string; fg?: string; accent: string | null; font: string | null }) {
  const toast = useToast();
  const [copied, setCopied] = useState(false);
  const size = text.length <= 30 ? 25 : text.length <= 70 ? 20 : 17;

  async function copy() {
    try {
      await navigator.clipboard.writeText(text);
      setCopied(true);
      toast.info("Phrase copied", text);
      window.setTimeout(() => setCopied(false), 1400);
    } catch {
      toast.error("Couldn't copy", text);
    }
  }

  return (
    <button
      type="button"
      onClick={copy}
      aria-label={`Copy the phrase: ${text}`}
      className={[
        "k-hover group w-full min-h-[184px] h-full p-5 text-left flex flex-col justify-between gap-4 border border-k-border",
        "focus:outline-none focus-visible:ring-2 focus-visible:ring-k-btn",
        bg ? "" : "bg-k-inset text-fg",
      ].join(" ")}
      style={bg ? { backgroundColor: bg, color: fg } : undefined}
    >
      <span className="flex items-center justify-between w-full" aria-hidden>
        <span className="text-[44px] leading-[0.6] font-bold" style={{ color: accent ?? undefined, fontFamily: fontStack(font) }}>
          &ldquo;
        </span>
        <span className="opacity-0 group-hover:opacity-80 group-focus-visible:opacity-80 transition-opacity">
          {copied ? <Check className="w-4 h-4" strokeWidth={2.4} /> : <Copy className="w-4 h-4" strokeWidth={1.8} />}
        </span>
      </span>
      <span className="block font-bold leading-[1.15] text-balance" style={{ fontFamily: fontStack(font), fontSize: size }}>
        {text}
      </span>
    </button>
  );
}

function Checklist({ always, never }: { always: string[]; never: string[] }) {
  const [ticked, setTicked] = useState<Set<string>>(() => new Set());
  const total = always.length + never.length;
  const done = ticked.size;
  const allDone = total > 0 && done === total;

  function toggle(key: string) {
    setTicked((s) => {
      const n = new Set(s);
      if (n.has(key)) n.delete(key);
      else n.add(key);
      return n;
    });
  }

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-x-4 gap-y-2">
        <p className="text-[13px] text-fg/70 tabular-nums" aria-live="polite">
          {allDone ? (
            <span className="inline-flex items-center gap-1.5 font-semibold text-k-accent">
              <Check className="w-4 h-4" strokeWidth={2.4} aria-hidden />
              All {total} checked. Good to go
            </span>
          ) : (
            <>
              Checked <b className="font-semibold text-fg">{done}</b> of {total}
            </>
          )}
        </p>
        <span className="flex-1 min-w-[120px] h-1.5 bg-fg/10 overflow-hidden" aria-hidden>
          <span
            className="block h-full bg-k-accent transition-[width] duration-500 ease-[cubic-bezier(0.16,1,0.3,1)]"
            style={{ width: `${total ? (done / total) * 100 : 0}%` }}
          />
        </span>
        {done > 0 && (
          <button
            type="button"
            onClick={() => setTicked(new Set())}
            className="inline-flex items-center gap-1.5 text-[12.5px] font-medium text-fg/60 hover:text-fg focus:outline-none focus-visible:ring-2 focus-visible:ring-k-btn"
          >
            <RotateCcw className="w-3.5 h-3.5" strokeWidth={2} aria-hidden />
            Start again
          </button>
        )}
      </div>
      <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
        <CheckColumn kind="do" title="Always" hint="Tick when the post does it" items={always} ticked={ticked} onToggle={toggle} />
        <CheckColumn kind="dont" title="Never" hint="Tick when the post avoids it" items={never} ticked={ticked} onToggle={toggle} />
      </div>
      <p className="text-[12px] text-fg/45">Ticks are only for you while you check a post. They are not saved.</p>
    </div>
  );
}

function CheckColumn({
  kind,
  title,
  hint,
  items,
  ticked,
  onToggle,
}: {
  kind: "do" | "dont";
  title: string;
  hint: string;
  items: string[];
  ticked: Set<string>;
  onToggle: (key: string) => void;
}) {
  const isDo = kind === "do";
  return (
    <div className={["bg-k-inset border border-k-border border-t-[3px] p-4 md:p-5 space-y-3", isDo ? "border-t-k-accent" : "border-t-k-coral"].join(" ")}>
      <div className="flex items-center gap-2">
        <span
          className={["w-6 h-6 flex items-center justify-center", isDo ? "bg-k-accent text-k-canvas" : "bg-k-coral text-white"].join(" ")}
          aria-hidden
        >
          {isDo ? <Check className="w-3.5 h-3.5" strokeWidth={2.6} /> : <X className="w-3.5 h-3.5" strokeWidth={2.6} />}
        </span>
        <p className="text-[14px] font-semibold text-fg">{title}</p>
        <p className="text-[12px] text-fg/50 ml-auto">{hint}</p>
      </div>
      {items.length === 0 ? (
        <p className="text-[13px] text-fg/45">Nothing listed yet.</p>
      ) : (
        <ul className="space-y-1">
          {items.map((d, i) => {
            const key = `${kind}${i}`;
            const on = ticked.has(key);
            return (
              <li key={key}>
                <button
                  type="button"
                  role="checkbox"
                  aria-checked={on}
                  onClick={() => onToggle(key)}
                  className="w-full flex items-start gap-3 px-2 py-2 -mx-2 text-left hover:bg-fg/[0.04] transition-colors focus:outline-none focus-visible:ring-2 focus-visible:ring-k-btn"
                >
                  <span
                    className={[
                      "mt-[1px] w-[18px] h-[18px] shrink-0 flex items-center justify-center border-2 transition-colors duration-200",
                      on ? "bg-k-accent border-k-accent text-k-canvas" : "border-fg/35",
                    ].join(" ")}
                    aria-hidden
                  >
                    {on && <Check className="w-3 h-3" strokeWidth={3} />}
                  </span>
                  <span className={["text-[14px] leading-snug text-pretty transition-colors", on ? "text-fg/45" : "text-fg/85"].join(" ")}>{d}</span>
                </button>
              </li>
            );
          })}
        </ul>
      )}
    </div>
  );
}

function AiNotes({ lines }: { lines: string[] }) {
  return (
    <ChartGate>
      <div className="bg-k-rail text-white border border-k-border">
        <div className="flex items-center gap-2 px-4 py-2.5 border-b border-white/10">
          <Bot className="w-4 h-4 text-[#00E5FC]" strokeWidth={1.8} aria-hidden />
          <span className="text-[11.5px] font-bold uppercase tracking-[0.1em] text-white/80">What the AI reads first</span>
          <span className="ml-auto text-[11.5px] text-white/50 tabular-nums">
            {lines.length} {lines.length === 1 ? "line" : "lines"}
          </span>
        </div>
        <ol className="p-5 md:p-6 space-y-2.5">
          {lines.map((l, i) => (
            <li key={i} className="k-rise flex gap-3" style={{ animationDelay: `${150 + i * 110}ms` }}>
              <span className="k-pixel text-[#00E5FC] text-[15px] leading-[1.6] shrink-0" aria-hidden>
                &gt;
              </span>
              <span className="text-[14.5px] leading-[1.6] text-white/85 text-pretty max-w-[80ch]">
                {l}
                {i === lines.length - 1 && (
                  <span className="k-cursor ml-1" aria-hidden>
                    _
                  </span>
                )}
              </span>
            </li>
          ))}
        </ol>
      </div>
    </ChartGate>
  );
}

/**
 * Where a brand-profile field came from, e.g.
 *   _sources.primary_colors = "priority_1_canonical_brand_guides" ->
 *   "From the brand guide".
 *
 * Returns null when source is undefined.
 */
function SourceBadge({ source }: { source?: string }) {
  if (!source) return null;
  // The profile may say "priority_1_canonical_brand_guides" or just "priority_1".
  const words: Record<string, string> = {
    "1": "From the brand guide",
    "2": "From an uploaded image",
    "3": "From your written notes",
    "3b": "From an uploaded brand video",
    "4": "Guessed from the website's tone",
    "5": "Guessed from the website",
    "6": "Guessed from website pictures",
    "7": "From our Instagram posts",
  };
  const level = source.match(/priority_(\d+b?)/)?.[1];
  const text = (level && words[level]) || source.replace(/_/g, " ");
  const isCanonical = level === "1";
  return (
    <span
      className={[
        "inline-flex items-center gap-1.5 text-[12px] font-medium px-2.5 py-1 border",
        isCanonical ? "bg-k-accent/10 text-k-accent border-k-accent/40" : "bg-fg/[0.04] text-fg/60 border-k-border",
      ].join(" ")}
      title={source}
    >
      {isCanonical && <Check className="w-3 h-3" strokeWidth={2.6} aria-hidden />}
      {text}
    </span>
  );
}
