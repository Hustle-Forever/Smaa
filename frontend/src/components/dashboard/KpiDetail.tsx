/**
 * The detail window behind each Dashboard total (Videos, Images, Edited videos,
 * Assets, Spent on AI).
 *
 * Motion: the window grows OUT of the tile that was clicked and shrinks back
 * INTO it on close (same path both ways), with a soft spring-like curve. CSS
 * transitions start from the current on-screen value, so closing mid-opening
 * simply reverses. Reduced motion: the global rule in styles.css makes it a
 * plain instant swap.
 *
 * Data: only what the app already serves. The stats and asset totals come from
 * the Dashboard; the daily charts and "latest" pictures come from the same
 * library list the Library page reads (GET /api/yas-producer/library), fetched
 * once, the first time a window opens. Nothing here writes.
 */
import { useEffect, useLayoutEffect, useMemo, useRef, useState, type ComponentType, type ReactNode } from "react";
import { createPortal } from "react-dom";
import { ArrowRight, X } from "lucide-react";

import type { Tab } from "../../App";
import { api } from "../../lib/api";
import { fmtNumber } from "../../lib/format";
import type { ProducerAssetRow, ProducerAssetsList, YasLibraryItem, YasLibraryType, YasStats } from "../../lib/types";

export type KpiKind = "videos" | "images" | "edited" | "assets" | "spend";

const TYPE_LABEL: Record<YasLibraryType, string> = {
  talking_head: "Presenter video (old)",
  talking_head_v2: "Human Element Video",
  poster: "Poster",
  poster_human: "Poster + Human",
  animation: "Animation",
  video_editor: "Video Editor",
  general_video: "General AI Video",
  broll: "B-roll",
};

/** Which library rows belong to which tile (matches how /stats counts them). */
const KIND_TYPES: Record<Exclude<KpiKind, "assets" | "spend">, YasLibraryType[]> = {
  videos: ["talking_head_v2", "animation"],
  images: ["poster", "poster_human"],
  edited: ["video_editor"],
};
/** Which /stats categories belong to which tile (backend _REEL_KEYS etc.). */
const KIND_CATS: Record<Exclude<KpiKind, "assets" | "spend">, string[]> = {
  videos: ["talking_head_v2"],
  images: ["style_inspired", "template_fill", "poster_human"],
  edited: ["video_edit"],
};

const money = (n: number) =>
  n >= 100 ? `$${Math.round(n).toLocaleString()}` : `$${n.toFixed(2)}`;
const cost = (it: YasLibraryItem) => {
  const v = typeof it.cost_usd === "string" ? parseFloat(it.cost_usd) : it.cost_usd ?? 0;
  return Number.isFinite(v) ? (v as number) : 0;
};
const dayKey = (d: Date) =>
  `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;

/* ------------------------------------------------------------------ */
/* shared, fetched once                                                */
/* ------------------------------------------------------------------ */

let libraryCache: Promise<YasLibraryItem[]> | null = null;
function loadLibrary(demo: boolean): Promise<YasLibraryItem[]> {
  if (demo) return Promise.resolve(sampleLibrary());
  if (!libraryCache) {
    libraryCache = api.yasProducer.library
      .list({ limit: 300 })
      .then((r) => r.items ?? [])
      .catch((e) => {
        libraryCache = null; // let the next open try again
        throw e;
      });
  }
  return libraryCache;
}

/* ------------------------------------------------------------------ */
/* the window                                                          */
/* ------------------------------------------------------------------ */

export interface KpiDetailProps {
  kind: KpiKind | null;
  /** The tile that was clicked: the window grows from here and returns here. */
  origin: HTMLElement | null;
  stats: YasStats | null;
  assets: ProducerAssetsList["totals"] | null;
  demo: boolean;
  navigate: (t: Tab, param?: string) => void;
  onClose: () => void;
  Icon?: ComponentType<any>;
}

export function KpiDetail({ kind, origin, stats, assets, demo, navigate, onClose, Icon }: KpiDetailProps) {
  // `shown` = the kind being displayed. It outlives `kind` by the closing
  // animation, so the window can shrink back with its content still there.
  const [shown, setShown] = useState<KpiKind | null>(kind);
  const [phase, setPhase] = useState<"from" | "open" | "closing">("from");
  const panelRef = useRef<HTMLDivElement | null>(null);
  const closeBtn = useRef<HTMLButtonElement | null>(null);

  useEffect(() => {
    if (kind) {
      setShown(kind);
      setPhase("from");
    } else if (shown) {
      setPhase("closing");
      const t = window.setTimeout(() => setShown(null), 380);
      return () => window.clearTimeout(t);
    }
  }, [kind]); // eslint-disable-line react-hooks/exhaustive-deps

  // Where the tile is, relative to the window's resting place.
  const fromTransform = () => {
    const p = panelRef.current;
    if (!p || !origin) return "translateY(24px) scale(0.96)";
    const a = origin.getBoundingClientRect();
    const b = p.getBoundingClientRect();
    if (!b.width || !b.height) return "translateY(24px) scale(0.96)";
    const dx = a.left + a.width / 2 - (b.left + b.width / 2);
    const dy = a.top + a.height / 2 - (b.top + b.height / 2);
    return `translate(${dx}px, ${dy}px) scale(${a.width / b.width}, ${a.height / b.height})`;
  };

  // Open: paint at the tile, then release to the resting place next frame.
  useLayoutEffect(() => {
    if (!shown || phase !== "from") return;
    const p = panelRef.current;
    if (!p) return;
    p.style.transition = "none";
    p.style.transform = fromTransform();
    p.getBoundingClientRect(); // commit the start frame
    const id = requestAnimationFrame(() => {
      p.style.transition = "";
      p.style.transform = "";
      setPhase("open");
    });
    return () => cancelAnimationFrame(id);
  }, [shown, phase]); // eslint-disable-line react-hooks/exhaustive-deps

  // Close: back into the tile, along the same path.
  useLayoutEffect(() => {
    if (phase !== "closing") return;
    const p = panelRef.current;
    if (!p) return;
    p.style.transition = "transform 340ms cubic-bezier(0.4, 0, 0.9, 0.6)";
    p.style.transform = fromTransform();
  }, [phase]); // eslint-disable-line react-hooks/exhaustive-deps

  // Keyboard, scroll lock, focus in and back out.
  useEffect(() => {
    if (!shown) return;
    const prev = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
      if (e.key === "Tab" && panelRef.current) {
        const f = panelRef.current.querySelectorAll<HTMLElement>("button, a[href], [tabindex]:not([tabindex='-1'])");
        if (!f.length) return;
        const first = f[0], last = f[f.length - 1];
        if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
        else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
      }
    };
    window.addEventListener("keydown", onKey);
    const t = window.setTimeout(() => closeBtn.current?.focus({ preventScroll: true }), 60);
    return () => {
      document.body.style.overflow = prev;
      window.removeEventListener("keydown", onKey);
      window.clearTimeout(t);
      origin?.focus({ preventScroll: true });
    };
  }, [shown]); // eslint-disable-line react-hooks/exhaustive-deps

  if (!shown) return null;
  const open = phase === "open";

  // Portalled to <body>: the page's entrance animation leaves a transform on
  // its wrapper, which would otherwise trap a fixed window inside the page.
  return createPortal(
    <div className="fixed inset-0 z-[90] flex items-center justify-center p-3 sm:p-6" role="presentation">
      {/* Scrim: dims and softly blurs the page behind. */}
      <div
        className="absolute inset-0 bg-black/55 backdrop-blur-[3px] transition-opacity duration-300"
        style={{ opacity: open ? 1 : 0 }}
        onClick={onClose}
        aria-hidden
      />
      <div
        ref={panelRef}
        role="dialog"
        aria-modal="true"
        aria-labelledby="kpi-detail-title"
        className="k-kpi-sheet relative w-full max-w-[820px] max-h-[88vh] flex flex-col bg-k-surface border border-k-border-strong shadow-[0_30px_80px_-20px_rgb(0_0_0/0.55)] will-change-transform"
      >
        <button
          ref={closeBtn}
          type="button"
          onClick={onClose}
          aria-label="Close"
          className="absolute top-3 right-3 z-10 w-10 h-10 flex items-center justify-center text-k-text/60 hover:text-k-text hover:bg-k-text/[0.06] focus:outline-none focus-visible:ring-2 focus-visible:ring-k-btn transition-colors"
        >
          <X className="w-5 h-5" strokeWidth={1.8} aria-hidden />
        </button>
        {/* The content fades in once the window has nearly landed, and out
            first when it leaves, so nothing is seen squashed. */}
        <div
          className="overflow-y-auto overscroll-contain transition-opacity"
          style={{ opacity: open ? 1 : 0, transitionDuration: open ? "260ms" : "120ms", transitionDelay: open ? "140ms" : "0ms" }}
        >
          <Body kind={shown} stats={stats} assets={assets} demo={demo} navigate={navigate} onClose={onClose} Icon={Icon} />
        </div>
      </div>
    </div>,
    document.body,
  );
}

/* ------------------------------------------------------------------ */
/* contents                                                            */
/* ------------------------------------------------------------------ */

function Body({
  kind,
  stats,
  assets,
  demo,
  navigate,
  onClose,
  Icon,
}: {
  kind: KpiKind;
  stats: YasStats | null;
  assets: ProducerAssetsList["totals"] | null;
  demo: boolean;
  navigate: (t: Tab, param?: string) => void;
  onClose: () => void;
  Icon?: ComponentType<any>;
}) {
  const [items, setItems] = useState<YasLibraryItem[] | null>(null);
  const [failed, setFailed] = useState(false);
  useEffect(() => {
    if (kind === "assets") return;
    let live = true;
    loadLibrary(demo)
      .then((r) => live && setItems(r))
      .catch(() => live && setFailed(true));
    return () => {
      live = false;
    };
  }, [kind, demo]);

  const go = (t: Tab, p?: string) => {
    onClose();
    window.setTimeout(() => navigate(t, p), 200);
  };

  if (kind === "assets") return <AssetsBody assets={assets} demo={demo} go={go} Icon={Icon} />;
  if (kind === "spend") return <SpendBody stats={stats} items={items} failed={failed} go={go} Icon={Icon} />;
  return <MadeBody kind={kind} stats={stats} items={items} failed={failed} go={go} Icon={Icon} />;
}

const COPY: Record<KpiKind, { title: string; sub: string }> = {
  videos: { title: "Videos", sub: "Presenter reels and animations, from idea to finished video." },
  images: { title: "Images", sub: "Posters in every style, with and without people." },
  edited: { title: "Edited videos", sub: "Projects cut and joined in the Video Editor." },
  assets: { title: "Assets", sub: "Photos, clips, people, fonts and music you can reuse." },
  spend: { title: "Spent on AI", sub: "What the images and videos have cost so far." },
};

function Head({ kind, value, Icon, extra }: { kind: KpiKind; value: string; Icon?: ComponentType<any>; extra?: ReactNode }) {
  return (
    <header className="px-6 md:px-8 pt-7 pb-6 border-b border-k-border pr-16">
      <p className="flex items-center gap-2 text-[12px] font-bold uppercase tracking-[0.1em] text-k-text/60">
        {Icon ? <Icon className="w-4 h-4" strokeWidth={1.7} aria-hidden /> : null}
        Details
      </p>
      <h2 id="kpi-detail-title" className="k-display text-[26px] md:text-[30px] text-k-text mt-1.5">
        <span className="k-code" aria-hidden>&gt; </span>
        {COPY[kind].title}
      </h2>
      <div className="mt-3 flex flex-wrap items-end gap-x-6 gap-y-2">
        <p className="k-num text-[48px] md:text-[56px] leading-none text-k-accent">{value}</p>
        <p className="text-[14px] text-k-text/65 max-w-sm pb-1">{COPY[kind].sub}</p>
      </div>
      {extra}
    </header>
  );
}

function Section({ title, sub, children, className = "" }: { title: string; sub?: string; children: ReactNode; className?: string }) {
  return (
    <section className={`space-y-3 ${className}`}>
      <div>
        <h3 className="k-title text-[13px]">{title}</h3>
        {sub ? <p className="text-[12.5px] text-k-text/55">{sub}</p> : null}
      </div>
      {children}
    </section>
  );
}

/* ---------- Videos / Images / Edited videos ---------- */

function MadeBody({
  kind,
  stats,
  items,
  failed,
  go,
  Icon,
}: {
  kind: "videos" | "images" | "edited";
  stats: YasStats | null;
  items: YasLibraryItem[] | null;
  failed: boolean;
  go: (t: Tab, p?: string) => void;
  Icon?: ComponentType<any>;
}) {
  const cats = (stats?.generations.categories ?? []).filter((c) => KIND_CATS[kind].includes(c.key));
  const anim = kind === "videos" ? stats?.animations : undefined;
  const rows = [
    ...cats.map((c) => ({ label: c.label, ready: c.ready + c.saved, making: c.generating, failed: c.failed, total: c.total })),
    ...(anim && anim.total > 0 ? [{ label: "Animations", ready: anim.ready, making: anim.generating, failed: anim.failed, total: anim.total }] : []),
  ].sort((a, b) => b.total - a.total);
  const total = rows.reduce((s, r) => s + r.total, 0);
  const ready = rows.reduce((s, r) => s + r.ready, 0);
  const making = rows.reduce((s, r) => s + r.making, 0);
  const bad = rows.reduce((s, r) => s + r.failed, 0);

  const mine = useMemo(() => (items ?? []).filter((it) => KIND_TYPES[kind].includes(it.type)), [items, kind]);
  const daily = useMemo(() => perDay(mine, () => 1), [mine]);
  const week = daily.slice(-7).reduce((s, d) => s + d.value, 0);
  const lastWeek = daily.slice(-14, -7).reduce((s, d) => s + d.value, 0);
  const latest = mine.slice().sort((a, b) => b.created_at.localeCompare(a.created_at)).slice(0, 4);
  const KIND_COLORS = ["rgb(var(--k-cyan))", "rgb(var(--k-purple))", "rgb(var(--k-coral))", "rgb(var(--k-text) / 0.6)"];

  return (
    <>
      <Head kind={kind} value={fmtNumber(total)} Icon={Icon} />
      {/* Same layout as the Assets window: ring left, bars right, then the
          daily chart, then big pictures. */}
      <div className="px-6 md:px-8 py-6 grid md:grid-cols-[auto_1fr] gap-x-8 gap-y-7">
        <Section title="By status">
          <Ring
            segments={[
              { label: "Ready", value: ready, color: "rgb(var(--k-cyan))" },
              { label: "Being made", value: making, color: "rgb(var(--k-purple))" },
              { label: "Didn't work", value: bad, color: "rgb(var(--k-coral))" },
            ]}
            centre={fmtNumber(total)}
            centreSub={COPY[kind].title.toLowerCase()}
          />
        </Section>
        <Section title="How many of each">
          <ValueBars rows={rows.map((r, i) => ({ label: r.label, value: r.total, color: KIND_COLORS[i % KIND_COLORS.length] }))} format={fmtNumber} />
          <Meter label="Ready to caption and post" value={ready} total={total} />
        </Section>
        <Section title="Made per day" sub="The last 30 days" className="md:col-span-2">
          <DailyBars data={daily} items={items} failed={failed} format={(n) => `${n} made`} />
          {items && !failed ? <WeekCompare week={week} lastWeek={lastWeek} unit={(n) => fmtNumber(n)} /> : null}
        </Section>
        <Section title="Latest" className="md:col-span-2">
          <SquareTiles
            items={latest}
            loading={!items && !failed}
            badge={(it) => TYPE_LABEL[it.type]}
            onOpen={(it) => go("library", `${it.type}:${it.id}`)}
          />
        </Section>
      </div>
      <Foot>
        <button type="button" className="k-btn" onClick={() => go("library")}>
          See all in Library <ArrowRight className="w-4 h-4" strokeWidth={2} aria-hidden />
        </button>
      </Foot>
    </>
  );
}

/* ---------- Spent on AI ---------- */

function SpendBody({
  stats,
  items,
  failed,
  go,
  Icon,
}: {
  stats: YasStats | null;
  items: YasLibraryItem[] | null;
  failed: boolean;
  go: (t: Tab, p?: string) => void;
  Icon?: ComponentType<any>;
}) {
  const s = stats?.spend;
  const total = s?.total_usd ?? 0;
  const pieces = (stats?.generations.total ?? 0) + (stats?.animations.total ?? 0);
  const avg = pieces > 0 ? total / pieces : 0;
  const priced = useMemo(() => (items ?? []).filter((it) => cost(it) > 0), [items]);
  const daily = useMemo(() => perDay(priced, cost), [priced]);
  const week = daily.slice(-7).reduce((a, d) => a + d.value, 0);
  const lastWeek = daily.slice(-14, -7).reduce((a, d) => a + d.value, 0);
  const byTool = useMemo(() => {
    const m = new Map<YasLibraryType, number>();
    for (const it of priced) m.set(it.type, (m.get(it.type) ?? 0) + cost(it));
    return [...m.entries()].map(([t, v]) => ({ label: TYPE_LABEL[t] ?? t, value: v })).sort((a, b) => b.value - a.value);
  }, [priced]);
  const top = priced.slice().sort((a, b) => cost(b) - cost(a)).slice(0, 4);
  const TOOL_COLORS = ["rgb(var(--k-cyan))", "rgb(var(--k-purple))", "rgb(var(--k-coral))", "rgb(var(--k-text) / 0.75)", "rgb(var(--k-text) / 0.45)", "rgb(var(--k-text) / 0.25)"];

  return (
    <>
      <Head kind="spend" value={money(total)} Icon={Icon} />
      {/* Same layout as the Assets window. */}
      <div className="px-6 md:px-8 py-6 grid md:grid-cols-[auto_1fr] gap-x-8 gap-y-7">
        <Section title="Split">
          <Ring
            segments={[
              { label: "Images and videos", value: s?.generations_usd ?? 0, color: "rgb(var(--k-cyan))" },
              { label: "Animations", value: s?.animations_usd ?? 0, color: "rgb(var(--k-purple))" },
            ]}
            centre={money(total)}
            centreSub="in total"
            format={money}
          />
        </Section>
        <Section title="By tool" sub={byTool.length ? `From the latest ${fmtNumber(priced.length)} priced pieces` : undefined}>
          {byTool.length ? (
            <ValueBars rows={byTool.map((r, i) => ({ ...r, color: TOOL_COLORS[i % TOOL_COLORS.length] }))} format={money} />
          ) : items || failed ? (
            <p className="text-[13px] text-k-text/55">No priced pieces yet.</p>
          ) : (
            <div className="h-24 bg-k-text/[0.04] animate-pulse" aria-busy="true" />
          )}
          <div className="mt-2 pt-3 flex items-center justify-between text-[12.5px] text-k-text/65 border-t border-k-border">
            <span>Average per piece, across {fmtNumber(pieces)} pieces</span>
            <b className="k-num text-[16px] text-k-text tabular-nums">{money(avg)}</b>
          </div>
        </Section>
        <Section title="Spent per day" sub="The last 30 days" className="md:col-span-2">
          <DailyBars data={daily} items={items} failed={failed} format={(n) => money(n)} />
          {items && !failed ? <WeekCompare week={week} lastWeek={lastWeek} unit={money} /> : null}
        </Section>
        <Section title="Most expensive pieces" className="md:col-span-2">
          <SquareTiles
            items={top}
            loading={!items && !failed}
            badge={(it) => money(cost(it))}
            accentBadge
            onOpen={(it) => go("library", `${it.type}:${it.id}`)}
          />
        </Section>
      </div>
      <Foot>
        <button type="button" className="k-btn" onClick={() => go("library")}>
          Open Library <ArrowRight className="w-4 h-4" strokeWidth={2} aria-hidden />
        </button>
      </Foot>
    </>
  );
}

/* ---------- Assets ---------- */

function AssetsBody({
  assets,
  demo,
  go,
  Icon,
}: {
  assets: ProducerAssetsList["totals"] | null;
  demo: boolean;
  go: (t: Tab, p?: string) => void;
  Icon?: ComponentType<any>;
}) {
  const [latest, setLatest] = useState<ProducerAssetRow[] | null>(demo ? [] : null);
  useEffect(() => {
    if (demo) return;
    let live = true;
    api.producerAssets
      .list({ limit: 8 })
      .then((r) => live && setLatest(r.items ?? []))
      .catch(() => live && setLatest([]));
    return () => {
      live = false;
    };
  }, [demo]);

  const a = assets ?? { total: 0, images: 0, videos: 0, fonts: 0, audio: 0, characters: 0, stickers: 0, enabled_count: 0 };
  const kinds = [
    { label: "Images", value: a.images, color: "rgb(var(--k-cyan))" },
    { label: "Videos", value: a.videos, color: "rgb(var(--k-purple))" },
    { label: "People", value: a.characters, color: "rgb(var(--k-coral))" },
    { label: "Stickers", value: a.stickers, color: "rgb(var(--k-text) / 0.75)" },
    { label: "Fonts", value: a.fonts, color: "rgb(var(--k-text) / 0.45)" },
    { label: "Music", value: a.audio, color: "rgb(var(--k-text) / 0.25)" },
  ];
  const paused = Math.max(0, a.total - a.enabled_count);
  const thumbs = (latest ?? []).filter((r) => r.thumb_url).slice(0, 4);

  return (
    <>
      <Head kind="assets" value={fmtNumber(a.total)} Icon={Icon} />
      <div className="px-6 md:px-8 py-6 grid md:grid-cols-[auto_1fr] gap-x-8 gap-y-7">
        <Section title="By kind">
          <Ring segments={kinds} centre={fmtNumber(a.total)} centreSub="assets" />
        </Section>
        <Section title="How many of each">
          <ValueBars rows={kinds.filter((k) => k.value > 0)} format={fmtNumber} />
          <Meter label="Active, the AI can use them" value={a.enabled_count} total={a.total} note={paused > 0 ? `${fmtNumber(paused)} paused` : undefined} />
        </Section>
        {thumbs.length > 0 ? (
          <Section title="Latest" className="md:col-span-2">
            <div className="grid grid-cols-4 gap-2">
              {thumbs.map((r, i) => (
                <button
                  key={r.id}
                  type="button"
                  onClick={() => go("assets")}
                  className="k-hover aspect-square overflow-hidden border border-k-border bg-k-inset k-rise focus:outline-none focus-visible:ring-2 focus-visible:ring-k-btn"
                  style={{ animationDelay: `${200 + i * 70}ms` }}
                  title={r.display_name || undefined}
                >
                  <img src={r.thumb_url!} alt={r.display_name || "Asset"} className="w-full h-full object-cover" loading="lazy" />
                </button>
              ))}
            </div>
          </Section>
        ) : null}
      </div>
      <Foot>
        <button type="button" className="k-btn" onClick={() => go("assets")}>
          Open Assets <ArrowRight className="w-4 h-4" strokeWidth={2} aria-hidden />
        </button>
      </Foot>
    </>
  );
}

/** One labelled share of a whole, as a thin bar (as in the Assets window). */
function Meter({ label, value, total, note }: { label: string; value: number; total: number; note?: string }) {
  return (
    <div className="pt-2 space-y-1.5">
      <div className="flex items-center justify-between text-[12.5px] text-k-text/65">
        <span>{label}</span>
        <b className="text-k-text tabular-nums">
          {fmtNumber(value)}
          <span className="text-k-text/50 font-normal"> of {fmtNumber(total)}</span>
        </b>
      </div>
      <div className="h-2 bg-k-text/[0.08] overflow-hidden">
        <div className="h-full bg-k-cyan k-grow-x" style={{ width: `${total ? (value / total) * 100 : 0}%`, animationDelay: "350ms" }} />
      </div>
      {note ? <p className="text-[12px] text-k-text/55">{note}</p> : null}
    </div>
  );
}

/** Big square pictures (as in the Assets window), each with a small label. */
function SquareTiles({
  items,
  loading,
  badge,
  accentBadge = false,
  onOpen,
}: {
  items: YasLibraryItem[];
  loading: boolean;
  badge: (it: YasLibraryItem) => string;
  accentBadge?: boolean;
  onOpen: (it: YasLibraryItem) => void;
}) {
  if (loading)
    return (
      <div className="grid grid-cols-2 sm:grid-cols-4 gap-2">
        {Array.from({ length: 4 }).map((_, i) => (
          <div key={i} className="aspect-square bg-k-text/[0.05] animate-pulse" />
        ))}
      </div>
    );
  if (!items.length) return <p className="text-[13px] text-k-text/55">Nothing here yet.</p>;
  return (
    <div className="grid grid-cols-2 sm:grid-cols-4 gap-2">
      {items.map((it, i) => (
        <button
          key={`${it.type}-${it.id}`}
          type="button"
          onClick={() => onOpen(it)}
          title={it.topic || "Untitled"}
          aria-label={`${it.topic || "Untitled"}, ${badge(it)}. Open in Library`}
          className="group relative k-hover aspect-square overflow-hidden border border-k-border bg-k-inset k-rise focus:outline-none focus-visible:ring-2 focus-visible:ring-k-btn"
          style={{ animationDelay: `${200 + i * 70}ms` }}
        >
          {/* A finished editor project's thumbnail_url IS its MP4: as an <img>
              it would download the whole video and still show nothing. */}
          {it.thumbnail_url && it.thumbnail_url !== it.video_url ? (
            <img src={it.thumbnail_url} alt="" className="w-full h-full object-cover transition-transform duration-500 group-hover:scale-[1.04]" loading="lazy" />
          ) : (
            <span className="w-full h-full flex items-center justify-center text-[11.5px] text-k-text/45">No picture yet</span>
          )}
          <span
            className={[
              "absolute left-2 bottom-2 max-w-[calc(100%-1rem)] truncate px-1.5 py-0.5 text-[10.5px] font-bold uppercase tracking-[0.06em]",
              accentBadge ? "bg-k-btn text-k-onbtn" : "bg-black/70 text-white",
            ].join(" ")}
          >
            {badge(it)}
          </span>
        </button>
      ))}
    </div>
  );
}

function Foot({ children }: { children: ReactNode }) {
  return <footer className="px-6 md:px-8 py-4 border-t border-k-border flex justify-end">{children}</footer>;
}

/* ------------------------------------------------------------------ */
/* small charts                                                        */
/* ------------------------------------------------------------------ */

function perDay(list: YasLibraryItem[], val: (it: YasLibraryItem) => number) {
  const out: { day: string; label: string; value: number }[] = [];
  const idx = new Map<string, number>();
  const today = new Date();
  for (let i = 29; i >= 0; i--) {
    const d = new Date(today);
    d.setDate(today.getDate() - i);
    idx.set(dayKey(d), out.length);
    out.push({ day: dayKey(d), label: d.toLocaleDateString("en-GB", { day: "numeric", month: "short" }), value: 0 });
  }
  for (const it of list) {
    const k = dayKey(new Date(it.created_at));
    const j = idx.get(k);
    if (j !== undefined) out[j].value += val(it);
  }
  return out;
}

/** 30 thin bars growing up one after another; today is the strong one. */
function DailyBars({
  data,
  items,
  failed,
  format,
}: {
  data: { day: string; label: string; value: number }[];
  items: YasLibraryItem[] | null;
  failed: boolean;
  format: (n: number) => string;
}) {
  const [hover, setHover] = useState<number | null>(null);
  if (failed) return <p className="text-[13px] text-k-text/55 py-6">Couldn't load the daily numbers. Close and open again to retry.</p>;
  if (!items) return <div className="h-[120px] bg-k-text/[0.04] animate-pulse" aria-busy="true" />;
  const max = Math.max(1, ...data.map((d) => d.value));
  const sum = data.reduce((s, d) => s + d.value, 0);
  const at = hover ?? data.length - 1;
  return (
    <div className="space-y-2 min-w-0">
      <p className="text-[12.5px] text-k-text/60 h-5" aria-live="polite">
        <b className="text-k-text font-semibold">{data[at].label}</b>: {format(data[at].value)}
      </p>
      <div
        className="h-[110px] flex items-end gap-[3px]"
        role="img"
        aria-label={`Last 30 days: ${format(sum)} in total`}
        onPointerLeave={() => setHover(null)}
      >
        {data.map((d, i) => (
          <div
            key={d.day}
            className="flex-1 h-full flex items-end cursor-default"
            onPointerEnter={() => setHover(i)}
          >
            <div
              className="w-full k-grow-y"
              style={{
                height: `${Math.max(d.value > 0 ? 6 : 2, (d.value / max) * 100)}%`,
                background: d.value === 0 ? "rgb(var(--k-text) / 0.1)" : i === at ? "rgb(var(--k-accent))" : "rgb(var(--k-accent) / 0.45)",
                animationDelay: `${120 + i * 18}ms`,
                transition: "background-color 120ms",
              }}
            />
          </div>
        ))}
      </div>
      <div className="flex justify-between text-[11px] text-k-text/45">
        <span>{data[0].label}</span>
        <span>Today</span>
      </div>
    </div>
  );
}

function WeekCompare({ week, lastWeek, unit }: { week: number; lastWeek: number; unit: (n: number) => string }) {
  const diff = week - lastWeek;
  const word = diff === 0 ? "the same as" : diff > 0 ? "more than" : "less than";
  return (
    <p className="text-[13px] text-k-text/65">
      This week <b className="text-k-text tabular-nums">{unit(week)}</b>, {word} last week ({unit(lastWeek)}).
    </p>
  );
}

/** A ring split into its parts, drawing itself in. Legend beside it. */
function Ring({
  segments,
  centre,
  centreSub,
  format = fmtNumber,
}: {
  segments: { label: string; value: number; color: string }[];
  centre: string;
  centreSub: string;
  format?: (n: number) => string;
}) {
  const size = 132, stroke = 14, r = (size - stroke) / 2, c = 2 * Math.PI * r;
  const total = segments.reduce((s, x) => s + x.value, 0);
  const [drawn, setDrawn] = useState(false);
  useEffect(() => {
    const id = requestAnimationFrame(() => requestAnimationFrame(() => setDrawn(true)));
    return () => cancelAnimationFrame(id);
  }, []);
  let acc = 0;
  return (
    <div className="flex items-center gap-5">
      <span className="relative shrink-0" style={{ width: size, height: size }} role="img" aria-label={segments.map((s) => `${s.label} ${format(s.value)}`).join(", ")}>
        <svg width={size} height={size} viewBox={`0 0 ${size} ${size}`} className="-rotate-90" aria-hidden>
          <circle cx={size / 2} cy={size / 2} r={r} fill="none" stroke="rgb(var(--k-line) / 0.08)" strokeWidth={stroke} />
          {total > 0 &&
            segments.map((s, i) => {
              const len = (s.value / total) * c;
              const off = acc;
              acc += len;
              if (s.value <= 0) return null;
              const gap = segments.filter((x) => x.value > 0).length > 1 ? 2 : 0;
              return (
                <circle
                  key={s.label}
                  cx={size / 2}
                  cy={size / 2}
                  r={r}
                  fill="none"
                  stroke={s.color}
                  strokeWidth={stroke}
                  strokeDasharray={`${drawn ? Math.max(0, len - gap) : 0} ${c}`}
                  strokeDashoffset={-off}
                  style={{ transition: `stroke-dasharray 900ms cubic-bezier(0.16, 1, 0.3, 1) ${150 + i * 120}ms` }}
                />
              );
            })}
        </svg>
        <span className="absolute inset-0 flex flex-col items-center justify-center leading-none">
          <span className="k-num text-[20px] text-k-text">{centre}</span>
          <span className="mt-1 text-[11px] text-k-text/55">{centreSub}</span>
        </span>
      </span>
      <ul className="space-y-1.5 min-w-[150px]">
        {segments.map((s) => (
          <li key={s.label} className="flex items-center gap-2 text-[12.5px] text-k-text/70">
            <span className="w-2.5 h-2.5 shrink-0" style={{ background: s.color }} aria-hidden />
            <span className="flex-1">{s.label}</span>
            <b className="text-k-text font-semibold tabular-nums">{format(s.value)}</b>
          </li>
        ))}
      </ul>
    </div>
  );
}

function ValueBars({ rows, format }: { rows: { label: string; value: number; color?: string }[]; format: (n: number) => string }) {
  const max = Math.max(1e-9, ...rows.map((r) => r.value));
  return (
    <ul className="space-y-2.5">
      {rows.map((r, i) => (
        <li key={r.label} className="grid grid-cols-[minmax(90px,150px)_1fr_auto] items-center gap-3 text-[13px]">
          <span className="text-k-text/80 truncate">{r.label}</span>
          <span className="h-2.5 bg-k-text/[0.07]">
            <span
              className="block h-full k-grow-x"
              style={{ width: `${(r.value / max) * 100}%`, background: r.color ?? "rgb(var(--k-accent))", animationDelay: `${250 + i * 90}ms` }}
            />
          </span>
          <b className="text-k-text font-semibold tabular-nums text-right min-w-[52px]">{format(r.value)}</b>
        </li>
      ))}
    </ul>
  );
}

/* ------------------------------------------------------------------ */
/* sample data for ?demo (local design preview only)                   */
/* ------------------------------------------------------------------ */

function sampleLibrary(): YasLibraryItem[] {
  const types: YasLibraryType[] = ["talking_head_v2", "animation", "poster", "poster_human", "video_editor"];
  const now = Date.now();
  return Array.from({ length: 48 }, (_, i) => {
    const t = types[i % types.length];
    const ageDays = (i * 7) % 29;
    return {
      id: 900 + i,
      type: t,
      topic: ["Hello, World", "Piscine countdown", "Why peer learning works", "Meet the July cohort", "Campus tour cut"][i % 5],
      status: "ready",
      image_model: null,
      cost_usd: t === "animation" || t === "talking_head_v2" ? 0.9 + ((i * 13) % 40) / 100 : 0.13 * (1 + (i % 4)),
      created_at: new Date(now - ageDays * 864e5 - (i % 5) * 36e5).toISOString(),
      thumbnail_url: null,
      video_url: null,
      has_video: false,
    } as YasLibraryItem;
  });
}
