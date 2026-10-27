/**
 * Everything we know about ONE trend, in a dialog.
 *
 * WHY this exists. The cards are deliberately lossy — line-clamped reasons, a
 * six-row evidence preview, no research block, no timeline — because a 36-card
 * grid has to stay scannable. A reader who is about to act on "108,398 likes"
 * needs to be able to click through to where that number came from.
 *
 * 2026-10 redesign (42 branding, for a non-technical team). The panel used to
 * read like an audit sheet: a "Measured / AI opinion" chip on every block,
 * "Where this came from" twice, and "Id at the source" at the same weight as
 * the story. The reading order is now the order of the decision:
 *
 *   1. the thing     picture, headline, outlet, when, the plain summary
 *   2. worth it?     the one counted number, big, beside the three AI scores
 *                    drawn as rings (0-100)
 *   3. the idea      "How 42 could use this" in the purple 42 block
 *   4. the evidence  the story text / the counts / the spread, as tiles,
 *                    a timeline and the sightings
 *   5. the reasoning why the AI scored it what it did
 *   6. the rest      every technical row, folded into "All the details"
 *
 * Provenance is kept, just quieter: each section names whether it is COUNTED
 * or the AI's OPINION in a small tag with the same explanation as a tooltip,
 * and the honest gaps (no share count, no audience sentiment) are still
 * stated, in "All the details". Nothing that was shown before is dropped.
 *
 * Motion: the panel grows out of the card that opened it and shrinks back
 * into it (the same as the Dashboard detail windows); the rings sweep in.
 * Reduced motion: instant, via the global rule in styles.css.
 *
 * Two things this file will NEVER print:
 *   • a share count — the Instagram Graph API does not expose one;
 *   • an audience sentiment — we store no comment text.
 *
 * Note the deliberate import cycle with `GeneralTrendCard`: both sides only
 * touch each other inside component bodies, never at module scope.
 */
import {
  useEffect,
  useId,
  useLayoutEffect,
  useRef,
  useState,
  type KeyboardEvent as ReactKeyboardEvent,
  type ReactNode,
} from "react";
import { createPortal } from "react-dom";
import { Bot, ChevronDown, ExternalLink, Ruler, ShieldAlert, X } from "lucide-react";

import { Skeleton } from "../Skeleton";
import { Thumb } from "../Thumb";
import { api } from "../../lib/api";
import { CLOCK_12H } from "../../lib/format";
import type {
  GeneralTrend,
  GeneralTrendEvidenceDetail,
  GeneralTrendResearch,
  TrendPost,
} from "../../lib/types";
import { Notice } from "./controls";
import { CoverTile } from "./CoverTile";
import { generalRegionLabel, safetyReasonWords, sourceLabel } from "./GeneralTrendCard";
import { Avatar, coverKindOf, ErrorCard, fmtCompact, fmtMultiple, fmtScore, regionLabel, uaeScoreOf, viralBand, viralOf, type ViralVerdict } from "./shared";
import { humanError } from "./trendsApi";

// ---------------------------------------------------------------------------
// What the dialog is showing
// ---------------------------------------------------------------------------

/**
 * The row the caller already has. It is rendered IMMEDIATELY so the dialog is
 * never an empty box, and the fetch below replaces it with the full record
 * (every sighting, the research block) as soon as it lands.
 */
export type TrendDetailTarget =
  | { kind: "general"; trend: GeneralTrend }
  | { kind: "post"; post: TrendPost };

/** The one place a caller names the id, so the fetch and the key agree. */
function targetId(target: TrendDetailTarget): number {
  return target.kind === "general" ? target.trend.id : target.post.id;
}

// ---------------------------------------------------------------------------
// Provenance: still stated, now as a quiet tag on each section
// ---------------------------------------------------------------------------

type Provenance = "measured" | "ai";

const PROVENANCE_WORD: Record<Provenance, string> = {
  measured: "Counted",
  // "AI opinion", not "AI judgement": the point is that it can be wrong.
  ai: "AI opinion",
};

const PROVENANCE_TITLE: Record<Provenance, string> = {
  measured:
    "Counted where we found it, or worked out from numbers that were counted. Anything worked out says so underneath it.",
  ai: "A language model wrote this from the text we hold. It is an opinion, not a measurement, and it can be wrong.",
};

function ProvenanceTag({ kind }: { kind: Provenance }) {
  const Icon = kind === "measured" ? Ruler : Bot;
  return (
    <span
      title={PROVENANCE_TITLE[kind]}
      className="inline-flex shrink-0 items-center gap-1 text-[10.5px] font-bold uppercase tracking-[0.08em] text-k-text/45"
    >
      <Icon className="h-3 w-3" strokeWidth={2} aria-hidden />
      {PROVENANCE_WORD[kind]}
    </span>
  );
}

/** A titled section of the panel, with its provenance named on the right. */
function Section({
  title,
  provenance,
  aside,
  delay = 0,
  children,
}: {
  title: string;
  provenance?: Provenance;
  aside?: ReactNode;
  delay?: number;
  children: ReactNode;
}) {
  return (
    <section className="space-y-3 k-rise" style={{ animationDelay: `${delay}ms` }}>
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h3 className="k-title text-[13px] flex items-center gap-2">
          {title}
          {aside}
        </h3>
        {provenance && <ProvenanceTag kind={provenance} />}
      </div>
      {children}
    </section>
  );
}

/** A label/value pair for "All the details". */
function Row({
  label,
  value,
  note,
}: {
  label: string;
  value: ReactNode;
  note?: string;
}) {
  return (
    <div className="min-w-0">
      <dt className="text-[11px] font-bold uppercase tracking-[0.08em] text-k-text/50">{label}</dt>
      <dd className="mt-0.5 break-words text-[13.5px] leading-snug text-k-text">
        {value}
        {note && <span className="mt-0.5 block text-[12px] leading-relaxed text-k-text/55">{note}</span>}
      </dd>
    </div>
  );
}

/** A big counted number with its label, in a square tile. */
function StatTile({ label, value, note }: { label: string; value: string; note?: string }) {
  return (
    <div className="k-inset px-4 py-3.5 min-w-0">
      <p className="text-[11px] font-bold uppercase tracking-[0.08em] text-k-text/55">{label}</p>
      <p className="k-num mt-1.5 text-[28px] leading-none text-k-text tabular-nums">{value}</p>
      {note && <p className="mt-1.5 text-[11.5px] leading-snug text-k-text/50">{note}</p>}
    </div>
  );
}

/** A 0-100 AI score as a ring that sweeps in. */
function ScoreRing({ label, value, title, delay = 0 }: { label: string; value: number | null; title: string; delay?: number }) {
  const size = 84, stroke = 7, r = (size - stroke) / 2, c = 2 * Math.PI * r;
  const v = value != null && Number.isFinite(value) ? Math.max(0, Math.min(100, value)) : null;
  const [drawn, setDrawn] = useState(false);
  useEffect(() => {
    const id = requestAnimationFrame(() => requestAnimationFrame(() => setDrawn(true)));
    return () => cancelAnimationFrame(id);
  }, []);
  return (
    <div className="flex flex-col items-center gap-2 min-w-0" title={title}>
      <span className="relative" style={{ width: size, height: size }} role="img" aria-label={`${label}: ${v == null ? "not scored" : `${Math.round(v)} out of 100`}`}>
        <svg width={size} height={size} viewBox={`0 0 ${size} ${size}`} className="-rotate-90" aria-hidden>
          <circle cx={size / 2} cy={size / 2} r={r} fill="none" stroke="rgb(var(--k-line) / 0.09)" strokeWidth={stroke} />
          {v != null && v > 0 && (
            <circle
              cx={size / 2}
              cy={size / 2}
              r={r}
              fill="none"
              stroke="rgb(var(--k-accent))"
              strokeWidth={stroke}
              strokeDasharray={`${drawn ? (v / 100) * c : 0} ${c}`}
              style={{ transition: `stroke-dasharray 1000ms cubic-bezier(0.16, 1, 0.3, 1) ${200 + delay}ms` }}
            />
          )}
        </svg>
        <span className="absolute inset-0 flex items-center justify-center k-num text-[22px] text-k-text tabular-nums">
          {v == null ? "–" : Math.round(v)}
        </span>
      </span>
      <span className="text-[11.5px] font-bold uppercase tracking-[0.06em] text-k-text/70 text-center leading-tight">{label}</span>
    </div>
  );
}

/** Long text from the source, in a quiet box. */
function Quote({ label, text }: { label?: string; text: string }) {
  return (
    <div className="k-inset px-4 py-3.5">
      {label && <p className="mb-1.5 text-[11px] font-bold uppercase tracking-[0.08em] text-k-text/55">{label}</p>}
      <p className="whitespace-pre-wrap break-words text-[14px] leading-relaxed text-k-text/85">{text}</p>
    </div>
  );
}

/**
 * Mirrors `general_trends.DETAIL_EVIDENCE_MAX`. Only used to decide whether to
 * warn that the list may be truncated.
 */
const EVIDENCE_CAP = 200;

// ---------------------------------------------------------------------------
// Words for the values the database stores as codes
// ---------------------------------------------------------------------------

const CHANNEL_WORDS: Record<string, string> = {
  competitor: "We watch this account",
  news: "The industry news collector found it",
};

const STATUS_WORDS: Record<string, string> = {
  new: "New, nobody has looked at it yet",
  saved: "Saved",
  dismissed: "Dismissed",
  used: "Marked as used",
};

const MEDIA_WORDS: Record<string, string> = {
  IMAGE: "Photo",
  VIDEO: "Video",
  CAROUSEL_ALBUM: "Carousel",
};

const HEAT_WORDS: Record<string, string> = { hot: "Hot", rising: "Rising", warm: "Warm" };

// ---------------------------------------------------------------------------
// Formatting
// ---------------------------------------------------------------------------

/** Full date AND time — a detail view is where someone checks the exact moment. */
function fmtStamp(iso: string | null | undefined): string {
  if (!iso) return "not recorded";
  const t = new Date(iso);
  if (Number.isNaN(t.getTime())) return iso;
  return t.toLocaleString(undefined, { year: "numeric", month: "short", day: "numeric", ...CLOCK_12H });
}

function fmtDay(iso: string | null | undefined): string {
  if (!iso) return "not recorded";
  const t = new Date(iso);
  if (Number.isNaN(t.getTime())) return iso;
  return t.toLocaleDateString("en-GB", { day: "numeric", month: "short" });
}

/** "10 days ago", "3 hours ago". */
function fmtAgo(iso: string | null | undefined): { n: string; unit: string } | null {
  if (!iso) return null;
  const then = new Date(iso).getTime();
  if (Number.isNaN(then)) return null;
  const mins = Math.max(0, Math.round((Date.now() - then) / 60000));
  if (mins < 60) return { n: String(mins), unit: mins === 1 ? "minute ago" : "minutes ago" };
  const hours = Math.round(mins / 60);
  if (hours < 24) return { n: String(hours), unit: hours === 1 ? "hour ago" : "hours ago" };
  const days = Math.round(hours / 24);
  if (days < 14) return { n: String(days), unit: days === 1 ? "day ago" : "days ago" };
  const weeks = Math.round(days / 7);
  return { n: String(weeks), unit: weeks === 1 ? "week ago" : "weeks ago" };
}

/** `engagement_rate` is stored as a FRACTION of followers (eng / followers). */
function fmtRate(v: number | null | undefined): string {
  if (v == null || !Number.isFinite(v)) return "not known";
  return `${(v * 100).toFixed(2)}%`;
}

/** Never renders an empty string as if it were a value. */
function nonEmpty(v: string | null | undefined): string | null {
  const s = (v ?? "").trim();
  return s ? s : null;
}

function tagsOf(v: string[] | null | undefined): string[] {
  return Array.isArray(v) ? v.filter((t) => nonEmpty(t)) : [];
}

// ---------------------------------------------------------------------------
// Dialog
// ---------------------------------------------------------------------------

const FOCUSABLE =
  'a[href],button:not([disabled]),input:not([disabled]),select:not([disabled]),' +
  'textarea:not([disabled]),summary,[tabindex]:not([tabindex="-1"])';

export function TrendDetailDialog({
  target,
  onClose,
  pastViral = null,
}: {
  target: TrendDetailTarget;
  onClose: () => void;
  /** A competitor post's Viral potential, as its card worked it out (same number). */
  pastViral?: ViralVerdict | null;
}) {
  const id = targetId(target);

  // The server's copy. Null until it lands — the seed row from the card is what
  // renders in the meantime, so the dialog is never a spinner in a void.
  const [trend, setTrend] = useState<GeneralTrend | null>(target.kind === "general" ? target.trend : null);
  const [post, setPost] = useState<TrendPost | null>(target.kind === "post" ? target.post : null);
  const [evidence, setEvidence] = useState<GeneralTrendEvidenceDetail[]>(
    target.kind === "general" ? target.trend.evidence : [],
  );
  const [research, setResearch] = useState<GeneralTrendResearch | null>(null);

  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [notFound, setNotFound] = useState(false);
  /** True until the server's evidence list replaces the card's preview. */
  const [evidencePreview, setEvidencePreview] = useState(target.kind === "general");

  const closeRef = useRef<HTMLButtonElement | null>(null);
  const panelRef = useRef<HTMLDivElement | null>(null);
  /** Whatever had focus when this opened (the card's button): the panel grows
   *  out of it, shrinks back into it, and hands focus back to it. */
  const openerRef = useRef<Element | null>(typeof document !== "undefined" ? document.activeElement : null);
  /** True only when the press that started a click landed on the backdrop. */
  const pressedBackdrop = useRef(false);
  const [open, setOpen] = useState(false);
  const closing = useRef(false);

  const titleId = useId();
  const sublineId = useId();

  const onCloseRef = useRef(onClose);
  useEffect(() => {
    onCloseRef.current = onClose;
  });

  /** Where the opener is, relative to the panel's resting place. */
  const fromTransform = () => {
    const p = panelRef.current;
    const o = openerRef.current;
    if (!p || !(o instanceof HTMLElement) || !document.contains(o)) return "translateY(24px) scale(0.96)";
    const a = o.getBoundingClientRect();
    const b = p.getBoundingClientRect();
    if (!a.width || !b.width || !b.height) return "translateY(24px) scale(0.96)";
    const dx = a.left + a.width / 2 - (b.left + b.width / 2);
    const dy = a.top + a.height / 2 - (b.top + b.height / 2);
    return `translate(${dx}px, ${dy}px) scale(${Math.max(0.15, a.width / b.width)}, ${Math.max(0.1, a.height / b.height)})`;
  };

  // Grow out of the card.
  useLayoutEffect(() => {
    const p = panelRef.current;
    if (!p) return;
    p.style.transition = "none";
    p.style.transform = fromTransform();
    p.getBoundingClientRect();
    const raf = requestAnimationFrame(() => {
      p.style.transition = "";
      p.style.transform = "";
      setOpen(true);
    });
    return () => cancelAnimationFrame(raf);
  }, []); // eslint-disable-line react-hooks/exhaustive-deps

  /** Shrink back into the card, then tell the owner. */
  function requestClose() {
    if (closing.current) return;
    closing.current = true;
    setOpen(false);
    const p = panelRef.current;
    if (p) {
      p.style.transition = "transform 320ms cubic-bezier(0.4, 0, 0.9, 0.6)";
      p.style.transform = fromTransform();
    }
    window.setTimeout(() => onCloseRef.current(), 300);
  }
  const requestCloseRef = useRef(requestClose);
  requestCloseRef.current = requestClose;

  /** Mount-only: scroll lock, focus in, Escape, focus back out. */
  useEffect(() => {
    const prevOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    function onKey(e: KeyboardEvent) {
      if (e.key === "Escape") requestCloseRef.current();
    }
    window.addEventListener("keydown", onKey);
    const t = window.setTimeout(() => closeRef.current?.focus({ preventScroll: true }), 60);
    return () => {
      document.body.style.overflow = prevOverflow;
      window.removeEventListener("keydown", onKey);
      window.clearTimeout(t);
      const opener = openerRef.current;
      if (opener instanceof HTMLElement && document.contains(opener)) opener.focus({ preventScroll: true });
    };
  }, []);

  /** The focus trap: Tab wraps inside the panel. */
  function trapTab(e: ReactKeyboardEvent<HTMLDivElement>) {
    if (e.key !== "Tab") return;
    const root = panelRef.current;
    if (!root) return;
    const items = Array.from(root.querySelectorAll<HTMLElement>(FOCUSABLE)).filter(
      (el) => el.offsetParent !== null || el === document.activeElement,
    );
    if (items.length === 0) return;
    const first = items[0];
    const last = items[items.length - 1];
    const active = document.activeElement;
    if (e.shiftKey && (active === first || active === root)) {
      e.preventDefault();
      last.focus();
    } else if (!e.shiftKey && active === last) {
      e.preventDefault();
      first.focus();
    }
  }

  const kind = target.kind;

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);
    setNotFound(false);
    const run = async () => {
      try {
        if (kind === "general") {
          const detail = await api.generalTrends.detail(id);
          if (cancelled) return;
          setTrend(detail.trend);
          setEvidence(detail.evidence ?? []);
          setEvidencePreview(false);
          setResearch(detail.research ?? null);
        } else {
          const detail = await api.trends.postDetail(id);
          if (cancelled) return;
          setPost(detail.post);
        }
      } catch (e) {
        if (cancelled) return;
        const raw = e instanceof Error ? e.message : String(e ?? "");
        if (/^404\b/.test(raw)) setNotFound(true);
        else setError(humanError(e, "Couldn't load the details"));
      } finally {
        if (!cancelled) setLoading(false);
      }
    };
    void run();
    return () => {
      cancelled = true;
    };
  }, [id, kind]);

  const shownPost = post ?? (target.kind === "post" ? target.post : null);
  const shownTrend = trend ?? (target.kind === "general" ? target.trend : null);

  const heading = shownTrend ? generalHeadline(shownTrend) : shownPost ? postHeadline(shownPost) : "Untitled";
  const eyebrowText = shownTrend ? "Viral social trend" : shownPost?.source === "news" ? "Industry signal" : "Competitor post";
  const subline = shownTrend ? generalSubline(shownTrend) : shownPost ? postSubline(shownPost) : "";
  // Only a competitor post has an account behind it; a news row's `username`
  // holds the OUTLET's name, so an avatar there would invent an account.
  const avatarFor = shownPost && shownPost.source !== "news" ? shownPost : null;
  const sourceHref = shownTrend ? shownTrend.url : (shownPost?.permalink ?? null);

  return createPortal(
    <div
      role="presentation"
      className="fixed inset-0 z-[100] flex items-center justify-center p-3 sm:p-6"
      onMouseDown={(e) => {
        pressedBackdrop.current = e.target === e.currentTarget;
      }}
      onClick={(e) => {
        if (e.target === e.currentTarget && pressedBackdrop.current) requestClose();
      }}
    >
      {/* Scrim (portalled to <body>: the page's entrance transform would
          otherwise trap a fixed overlay inside the page). */}
      <div
        aria-hidden
        className="pointer-events-none absolute inset-0 bg-black/60 backdrop-blur-[3px] transition-opacity duration-300"
        style={{ opacity: open ? 1 : 0 }}
      />
      <div
        ref={panelRef}
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        aria-describedby={sublineId}
        onKeyDown={trapTab}
        className="k-kpi-sheet relative flex max-h-[90vh] w-full max-w-[900px] flex-col overflow-hidden bg-k-surface border border-k-border-strong shadow-[0_30px_80px_-20px_rgb(0_0_0/0.55)] will-change-transform"
      >
        {/* A slim bar that never scrolls: what this is, and the way out. Kept
            outside the scroll area so nothing ever slides under the X. */}
        <div className="flex shrink-0 items-center justify-between gap-3 border-b border-k-border pl-6 pr-2 md:pl-8 h-14">
          <p className="min-w-0 truncate text-[11.5px] font-bold uppercase tracking-[0.14em] text-k-accent">
            <span aria-hidden>&gt; </span>
            {eyebrowText}
          </p>
          <button
            ref={closeRef}
            type="button"
            onClick={requestClose}
            aria-label="Close"
            className="flex h-10 w-10 shrink-0 items-center justify-center text-k-text/60 transition-colors hover:bg-k-text/[0.06] hover:text-k-text focus:outline-none focus-visible:ring-2 focus-visible:ring-k-btn"
          >
            <X className="h-5 w-5" strokeWidth={1.8} aria-hidden />
          </button>
        </div>

        {/* The content fades in as the panel lands, and out first as it leaves. */}
        <div
          className="min-h-0 flex-1 overflow-y-auto overscroll-contain transition-opacity"
          style={{ opacity: open ? 1 : 0, transitionDuration: open ? "260ms" : "120ms", transitionDelay: open ? "140ms" : "0ms" }}
        >
          {notFound ? (
            <div className="px-8 py-16 text-center">
              <h2 id={titleId} className="k-display text-[22px] text-k-text">No longer here</h2>
              <p id={sublineId} className="mx-auto mt-2 max-w-md text-[14px] text-k-text/65">
                It was removed after the page loaded. Close this and reload the tab.
              </p>
            </div>
          ) : (
            <>
              <Hero
                titleId={titleId}
                sublineId={sublineId}
                eyebrow={eyebrowText}
                heading={heading}
                subline={subline}
                avatar={
                  avatarFor ? (
                    <Avatar src={avatarFor.competitor_avatar} name={avatarFor.competitor_name || avatarFor.username} size={20} />
                  ) : null
                }
                media={shownTrend ? <TrendMedia trend={shownTrend} /> : shownPost ? <PostMedia post={shownPost} /> : null}
                summary={
                  shownTrend
                    ? nonEmpty(shownTrend.topic_summary ?? shownTrend.summary)
                    : shownPost
                      ? nonEmpty(shownPost.topic_summary)
                      : null
                }
              />
              <div className="space-y-8 px-6 py-7 md:px-8">
                {error && (
                  <div className="space-y-1.5">
                    <ErrorCard message={error} />
                    <p className="text-[12px] text-k-text/55">
                      Showing what the card already had. Anything only the full record holds is missing until this loads.
                    </p>
                  </div>
                )}
                {shownTrend ? (
                  <GeneralBody trend={shownTrend} evidence={evidence} evidencePreview={evidencePreview} research={research} loading={loading} />
                ) : shownPost ? (
                  <PostBody post={shownPost} loading={loading} pastViral={pastViral} />
                ) : null}
              </div>
            </>
          )}
        </div>

        {/* Footer: the source and the way out. */}
        <div className="flex shrink-0 flex-wrap items-center gap-3 border-t border-k-border px-6 py-4 md:px-8">
          {sourceHref && (
            <a href={sourceHref} target="_blank" rel="noopener noreferrer" className="k-btn-ghost">
              <ExternalLink className="h-4 w-4" strokeWidth={1.8} aria-hidden />
              Open the source
            </a>
          )}
          <button type="button" onClick={requestClose} className="k-btn ml-auto">
            Close
          </button>
        </div>
      </div>
    </div>,
    document.body,
  );
}

// ---------------------------------------------------------------------------
// The top of the panel: the thing itself
// ---------------------------------------------------------------------------

function Hero({
  titleId,
  sublineId,
  eyebrow,
  heading,
  subline,
  avatar,
  media,
  summary,
}: {
  titleId: string;
  sublineId: string;
  eyebrow: string;
  heading: string;
  subline: string;
  avatar: ReactNode;
  media: ReactNode;
  summary: string | null;
}) {
  return (
    <header className="grid gap-5 border-b border-k-border px-6 pb-7 pt-6 md:grid-cols-[200px_1fr] md:px-8" aria-label={eyebrow}>
      <div className="aspect-square w-40 md:w-full overflow-hidden border border-k-border bg-k-inset k-rise">{media}</div>
      <div className="min-w-0 space-y-3 k-rise" style={{ animationDelay: "60ms" }}>
        <h2 id={titleId} className="break-words text-[22px] md:text-[26px] font-semibold leading-tight tracking-[-0.01em] text-k-text">
          {heading}
        </h2>
        <p className="flex flex-wrap items-center gap-x-2 gap-y-1 text-[13px] text-k-text/60">
          {avatar}
          <span id={sublineId} className="min-w-0 break-words">{subline}</span>
        </p>
        {summary && <p className="text-[15px] leading-relaxed text-k-text/85 max-w-[60ch]">{summary}</p>}
      </div>
    </header>
  );
}

function PostMedia({ post }: { post: TrendPost }) {
  const hasImage = Boolean(post.local_thumb_path || post.thumbnail_url);
  const cover = <CoverTile seed={post.platform || post.username || "news"} label={post.platform || post.username || null} compact />;
  // A brand mark (the judge named the company): small and centred on the
  // tinted tile, captioned with whose it is. Through <Thumb> it would be
  // stretched into a blurry square.
  const meta: Record<string, unknown> = post.metadata ?? {};
  const company = typeof meta.company === "string" ? meta.company : "";
  if (coverKindOf(post) === "logo") {
    return (
      <CoverTile
        seed={company || post.platform || post.username || "news"}
        label={company || post.platform || post.username || null}
        logoSrc={api.trends.thumbUrl(post.id)}
        compact
      />
    );
  }
  return hasImage ? (
    <Thumb
      src={api.trends.thumbUrl(post.id)}
      fallbackSrc={post.thumbnail_url ?? post.media_url ?? null}
      alt={postHeadline(post)}
      eager
      objectFit="cover"
      className="h-full w-full"
      empty={cover}
    />
  ) : (
    cover
  );
}

function TrendMedia({ trend }: { trend: GeneralTrend }) {
  const hasImage = Boolean(trend.local_thumb_path || trend.image_url);
  const seed = (trend.sources && trend.sources[0]) || trend.title || "trend";
  const label = (trend.sources && trend.sources[0]) || null;
  const cover = <CoverTile seed={seed} label={label} compact />;
  return hasImage ? (
    <Thumb
      src={trend.local_thumb_path ? api.generalTrends.thumbUrl(trend.id) : (trend.image_url ?? null)}
      fallbackSrc={trend.local_thumb_path ? (trend.image_url ?? null) : null}
      alt={generalHeadline(trend)}
      eager
      objectFit="cover"
      className="h-full w-full"
      empty={cover}
    />
  ) : (
    cover
  );
}

// ---------------------------------------------------------------------------
// "Worth making?": the counted number beside the three AI scores
// ---------------------------------------------------------------------------

function WorthIt({
  heroLabel,
  heroValue,
  heroUnit,
  heroNote,
  scored,
  fit,
  fitReason,
  uae,
  opportunity,
  opportunityTitle,
  opportunityLabel = "Opportunity",
}: {
  heroLabel: string;
  heroValue: string;
  heroUnit?: string;
  heroNote: string;
  scored: boolean;
  fit: number | null;
  fitReason: string | null;
  uae: number | null;
  opportunity: number | null;
  opportunityTitle: string;
  opportunityLabel?: string;
}) {
  return (
    <Section title="Worth making?" delay={120}>
      <div className="grid gap-4 md:grid-cols-[minmax(0,240px)_1fr]">
        <div className="k-inset px-5 py-4 flex flex-col justify-between gap-3">
          <div className="flex items-center justify-between gap-2">
            <span className="text-[11px] font-bold uppercase tracking-[0.08em] text-k-text/55">{heroLabel}</span>
            <ProvenanceTag kind="measured" />
          </div>
          <p className="flex items-baseline gap-2">
            <span className="k-num text-[48px] leading-none text-k-accent tabular-nums">{heroValue}</span>
            {heroUnit && <span className="text-[14px] font-semibold text-k-text/70">{heroUnit}</span>}
          </p>
          <p className="text-[12px] leading-snug text-k-text/55">{heroNote}</p>
        </div>
        <div className="k-inset px-5 py-4">
          <div className="flex items-center justify-between gap-2">
            <span className="text-[11px] font-bold uppercase tracking-[0.08em] text-k-text/55">Scores out of 100</span>
            <ProvenanceTag kind="ai" />
          </div>
          {scored ? (
            <div className="mt-3 grid grid-cols-3 gap-2">
              <ScoreRing label="Fit for 42" value={fit} title={fitReason ?? "How practical this is for 42 to make"} />
              <ScoreRing label="UAE" value={uae} title="How well the idea lands with a UAE audience" delay={120} />
              <ScoreRing label={opportunityLabel} value={opportunity} title={opportunityTitle} delay={240} />
            </div>
          ) : (
            <p className="mt-3 text-[13px] leading-relaxed text-k-text/60">
              Not scored yet. The next scoring run fills in these three numbers. Nothing is being held back.
            </p>
          )}
        </div>
      </div>
    </Section>
  );
}

/** "How 42 could use this", in the purple 42 block. */
function UseIt({
  angle,
  format,
  effort,
  theme,
}: {
  angle: string | null;
  format: string | null;
  effort: string | null;
  theme: string | null;
}) {
  if (!nonEmpty(angle)) return null;
  const chips = [
    nonEmpty(format) && { k: "Format", v: format },
    nonEmpty(effort) && { k: "Effort", v: effort },
    nonEmpty(theme) && { k: "Theme", v: theme },
  ].filter(Boolean) as { k: string; v: string }[];
  return (
    <section className="k-feature px-6 py-5 md:px-7 md:py-6 space-y-3 k-rise" style={{ animationDelay: "180ms" }}>
      <div className="flex flex-wrap items-center justify-between gap-2">
        <p className="text-[12px] font-bold uppercase tracking-[0.12em] text-k-cyan">
          <span className="k-pixel" aria-hidden>&lt; </span>How 42 could use this<span className="k-pixel" aria-hidden> /&gt;</span>
        </p>
        <span title={PROVENANCE_TITLE.ai} className="inline-flex items-center gap-1 text-[10.5px] font-bold uppercase tracking-[0.08em] text-white/60">
          <Bot className="h-3 w-3" strokeWidth={2} aria-hidden />
          AI idea
        </span>
      </div>
      <p className="text-[17px] md:text-[18px] leading-relaxed text-white">{angle}</p>
      {chips.length > 0 && (
        <div className="flex flex-wrap gap-2 pt-1">
          {chips.map((c) => (
            <span key={c.k} className="inline-flex items-center gap-1.5 px-2.5 py-1 bg-white/10 ring-1 ring-white/20 text-[12.5px] text-white">
              <span className="text-white/60">{c.k}</span>
              <b className="font-semibold">{c.v}</b>
            </span>
          ))}
        </div>
      )}
    </section>
  );
}

/** Why the AI scored it what it did. */
function Reasoning({ topic, fitReason, loading, scored, delay }: { topic: string | null; fitReason: string | null; loading: boolean; scored: boolean; delay: number }) {
  if (!scored) {
    return loading ? (
      <div className="space-y-2">
        <Skeleton h="h-3" className="w-full" />
        <Skeleton h="h-3" className="w-4/5" />
      </div>
    ) : null;
  }
  if (!nonEmpty(topic) && !nonEmpty(fitReason)) return null;
  return (
    <Section title="Why it scored this" provenance="ai" delay={delay}>
      <div className="grid gap-3 md:grid-cols-2">
        {nonEmpty(topic) && <Quote label="The idea worth borrowing" text={topic ?? ""} />}
        {nonEmpty(fitReason) && <Quote label="Why it fits 42 (or not)" text={fitReason ?? ""} />}
      </div>
    </Section>
  );
}

/** Every technical row, folded away. Nothing is dropped, only quieter. */
function AllDetails({ children, tags }: { children: ReactNode; tags: string[] }) {
  return (
    <details className="group k-inset k-rise" style={{ animationDelay: "320ms" }}>
      <summary className="flex cursor-pointer list-none items-center justify-between gap-3 px-4 py-3.5 text-[12.5px] font-bold uppercase tracking-[0.08em] text-k-text/75 hover:text-k-text focus:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-k-btn [&::-webkit-details-marker]:hidden">
        All the details
        <ChevronDown className="h-4 w-4 transition-transform duration-200 group-open:rotate-180" strokeWidth={2} aria-hidden />
      </summary>
      <div className="space-y-4 border-t border-k-border px-4 py-4">
        <dl className="grid gap-x-6 gap-y-4 sm:grid-cols-2">{children}</dl>
        {tags.length > 0 && (
          <div>
            <p className="mb-1.5 text-[11px] font-bold uppercase tracking-[0.08em] text-k-text/50">Tags</p>
            <div className="flex flex-wrap gap-1.5">
              {tags.map((t) => (
                <span key={t} className="px-2 py-0.5 text-[12px] text-k-text/70 ring-1 ring-k-border">
                  {t}
                </span>
              ))}
            </div>
          </div>
        )}
      </div>
    </details>
  );
}

// ---------------------------------------------------------------------------
// Competitor post / news story
// ---------------------------------------------------------------------------

function PostBody({ post, loading, pastViral: cardViral }: { post: TrendPost; loading: boolean; pastViral: ViralVerdict | null }) {
  const isNews = post.source === "news";
  const meta: Record<string, unknown> = post.metadata ?? {};
  // The publisher's own headline: the card shows OUR plain rewrite.
  const originalHeadline = typeof meta.original_headline === "string" ? meta.original_headline : "";
  const whyItMatters = typeof meta.why_it_matters === "string" ? meta.why_it_matters : "";
  const dateIsEditTime = meta.date_is_estimated === true;
  const viral = viralOf(post);
  const tags = tagsOf(post.tags);
  const handle = post.username?.replace(/^@/, "") ?? "";
  const ago = fmtAgo(post.posted_at);
  const scored = Boolean(post.scored_at);
  const outperf = post.outperformance != null && Number.isFinite(post.outperformance) ? post.outperformance : null;
  // Competitor posts: Viral potential from how past competitor posts performed.
  const pastViral = isNews ? null : cardViral;
  const uaeScore = uaeScoreOf(post);

  return (
    <>
      <WorthIt
        heroLabel={isNews ? "How fresh" : "Against its usual post"}
        heroValue={isNews ? (ago?.n ?? "–") : fmtMultiple(outperf)}
        heroUnit={isNews ? (ago ? (dateIsEditTime ? `${ago.unit}, approx.` : ago.unit) : "undated") : outperf != null ? "its usual" : undefined}
        heroNote={
          isNews
            ? dateIsEditTime
              ? "The source reports edit time, not publication time, so read it as approximate."
              : "A news story has no likes on our side, so how recent it is, is the honest number."
            : outperf != null
              ? "Likes and comments against this account's own typical post, so a big account does not look good just for being big."
              : "We do not hold enough history for this account to compare yet."
        }
        scored={scored}
        fit={post.fit_score}
        fitReason={post.fit_reason}
        uae={uaeScore.score}
        opportunityLabel="Viral potential"
        opportunity={isNews ? (viral?.score ?? null) : (pastViral?.score ?? null)}
        opportunityTitle={
          isNews
            ? viral?.reason || "How likely a post about this would get views and shares"
            : pastViral?.reason || "Not enough tracked competitor posts to compare yet"
        }
      />

      {scored && <UseIt angle={post.suggested_angle} format={post.suggested_format} effort={post.effort} theme={post.theme} />}

      {isNews ? (
        <Section title="The story" provenance="measured" delay={240}>
          <div className="space-y-3">
            {nonEmpty(whyItMatters) && (
              <div className="border-l-[3px] border-k-btn pl-4">
                <p className="text-[11px] font-bold uppercase tracking-[0.08em] text-k-text/55">Why it matters for 42</p>
                <p className="mt-1 text-[15px] leading-relaxed text-k-text">{whyItMatters}</p>
              </div>
            )}
            <div className="grid gap-3 sm:grid-cols-3">
              <StatTile label="Published" value={fmtDay(post.posted_at)} note={dateIsEditTime ? "Approximate: the day we found it" : fmtStamp(post.posted_at)} />
              <StatTile label="Publisher" value={nonEmpty(post.platform) ?? nonEmpty(post.username) ?? "–"} />
              <StatTile
                label="Viral potential"
                value={viral ? String(viral.score) : "–"}
                note={viral ? `out of 100. ${viralBand(viral.score).label}.` : "Not scored yet"}
              />
            </div>
            {nonEmpty(originalHeadline) && <Quote label="Headline, as the outlet wrote it" text={originalHeadline} />}
            {post.caption && <Quote label="The story, as the feed gave it" text={post.caption} />}
          </div>
        </Section>
      ) : (
        <Section title="What it got on Instagram" provenance="measured" delay={240}>
          <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
            <StatTile label="Likes" value={fmtCompact(post.like_count)} />
            <StatTile label="Comments" value={fmtCompact(post.comments_count)} />
            <StatTile label="Views" value={post.view_count != null ? fmtCompact(post.view_count) : "–"} note={post.view_count == null ? "Instagram reports views for video only" : undefined} />
            <StatTile label="Share of followers" value={fmtRate(post.engagement_rate)} note="Likes + comments ÷ followers" />
          </div>
          {outperf != null && <VsUsual multiple={outperf} />}
          {pastViral && (
            <StatTile
              label="Viral potential"
              value={String(pastViral.score)}
              note={`out of 100. ${viralBand(pastViral.score).label}. ${pastViral.reason}`}
            />
          )}
          {post.caption && <Quote label="Caption, word for word" text={post.caption} />}
        </Section>
      )}

      <Reasoning topic={post.topic} fitReason={post.fit_reason} loading={loading} scored={scored} delay={300} />

      <AllDetails tags={tags}>
        {isNews ? (
          <>
            <Row label="Name in the feed" value={nonEmpty(post.username) ?? "not recorded"} />
            <Row
              label="Viral potential"
              value={viral ? `${viral.score} out of 100. ${viralBand(viral.score).label}.` : "not scored yet"}
              note={viral?.reason || "How likely a post about this would get views and shares. The news feed is ordered by it."}
            />
            <Row label="How people reacted" value="not measured" note="We store no comment text, so nothing here measures how anyone felt." />
          </>
        ) : (
          <>
            <Row label="Account" value={handle ? `@${handle}` : "not recorded"} />
            <Row label="Followers" value={fmtCompact(post.competitor_followers)} note="At the time of the scan." />
            <Row label="Posted" value={fmtStamp(post.posted_at)} />
            <Row label="Likes plus comments" value={fmtCompact(post.engagement)} note="The two counted numbers added together." />
            <Row label="Shares" value="not available" note="The Instagram Graph API does not report shares on another account's post." />
            <Row label="How people reacted" value="not measured" note="We store no comment text, so nothing here measures how anyone felt." />
            <Row label="Region" value={regionLabel(post.region ?? post.competitor_region)} />
          </>
        )}
        <Row label="Platform" value={nonEmpty(post.platform) ?? "not recorded"} />
        <Row label="How we found it" value={CHANNEL_WORDS[post.source] ?? post.source} />
        <Row label="Language" value={nonEmpty(post.language) ?? "not recorded"} />
        {post.media_type && <Row label="Format" value={MEDIA_WORDS[post.media_type] ?? post.media_type} />}
        <Row label="Where it sits in the feed" value={STATUS_WORDS[post.status] ?? post.status} />
        <Row label="Fit for 42" value={fmtScore(post.fit_score)} note="0-100. The AI's own number." />
        <Row label="UAE relevance" value={fmtScore(uaeScore.score)} note={`0-100. ${uaeScore.reason}`} />
        <Row
          label="Opportunity"
          value={fmtScore(post.opportunity_score)}
          note={isNews ? "Worked out here: freshness blended with the two scores." : "Worked out here: outperformance blended with the two scores."}
        />
        <Row label="Scored" value={fmtStamp(post.scored_at)} />
        <Row label="Id at the source" value={post.ig_media_id} note="Quote this if a number here ever needs checking." />
      </AllDetails>
    </>
  );
}

/** This post against the account's typical post (1×), as two bars. */
function VsUsual({ multiple }: { multiple: number }) {
  const max = Math.max(2, multiple);
  return (
    <div className="k-inset px-4 py-3.5 space-y-2.5">
      <p className="text-[11px] font-bold uppercase tracking-[0.08em] text-k-text/55">This post vs the account's usual post</p>
      {[
        { label: "Usual post", v: 1, cls: "bg-k-text/30" },
        { label: "This post", v: multiple, cls: "bg-k-accent" },
      ].map((r, i) => (
        <div key={r.label} className="grid grid-cols-[90px_1fr_56px] items-center gap-3 text-[13px]">
          <span className="text-k-text/70">{r.label}</span>
          <span className="h-2.5 bg-k-text/[0.07]">
            <span className={`block h-full k-grow-x ${r.cls}`} style={{ width: `${(r.v / max) * 100}%`, animationDelay: `${300 + i * 140}ms` }} />
          </span>
          <b className="text-right tabular-nums text-k-text">{fmtMultiple(r.v)}</b>
        </div>
      ))}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Viral social trend
// ---------------------------------------------------------------------------

function GeneralBody({
  trend,
  evidence,
  evidencePreview,
  research,
  loading,
}: {
  trend: GeneralTrend;
  evidence: GeneralTrendEvidenceDetail[];
  evidencePreview: boolean;
  research: GeneralTrendResearch | null;
  loading: boolean;
}) {
  const tags = tagsOf(trend.tags);
  const scored = Boolean(trend.scored_at);
  const velocity = trend.velocity != null && Number.isFinite(trend.velocity) ? Math.round(trend.velocity * 100) : null;

  return (
    <>
      {/* A held-back row changes what a reader should do next, so it leads. */}
      {trend.safety === "flagged" && (
        <Notice tone="warning" Icon={ShieldAlert}>
          Our content filter held this back before scoring: {safetyReasonWords(trend.safety_reason)}. It was never sent to the AI,
          and it only appears in the feed when "Hidden trends" is on.
        </Notice>
      )}

      <WorthIt
        heroLabel="How hot"
        heroValue={HEAT_WORDS[trend.heat] ?? trend.heat}
        heroUnit={velocity != null ? `${velocity}/100` : undefined}
        heroNote="How fast and how widely this is spreading across the sources we poll. Worked out by us; no platform publishes it."
        scored={scored}
        fit={trend.fit_score}
        fitReason={trend.fit_reason}
        uae={trend.uae_relevance}
        opportunity={trend.opportunity_score}
        opportunityTitle="How the feed ranks this trend: heat blended with the two AI scores"
      />

      {scored && <UseIt angle={trend.suggested_angle} format={trend.suggested_format} effort={trend.effort} theme={trend.theme} />}

      <Section title="How far it has spread" provenance="measured" delay={240}>
        <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
          <StatTile label="Sources" value={String(trend.source_count)} note={trend.sources.map((s) => sourceLabel(s)).join(", ") || undefined} />
          <StatTile label="Places seen" value={String(trend.evidence_count)} note="One per source and item" />
          <StatTile label="Re-checks" value={String(trend.sighting_count)} note="Still listed when we looked" />
          <StatTile label="Biggest number" value={fmtCompact(trend.peak_metric)} note="Largest figure one source gave" />
        </div>
        <Timeline first={trend.first_seen_at} newest={trend.latest_content_at} last={trend.last_seen_at} />
      </Section>

      <Section
        title="Where we saw it"
        provenance="measured"
        delay={280}
        aside={<span className="text-[12px] font-normal normal-case tracking-normal text-k-text/50 tabular-nums">({evidence.length})</span>}
      >
        {loading && evidencePreview && <p className="text-[12px] text-k-text/55">Showing the card's preview while the full list loads.</p>}
        {!evidencePreview && evidence.length >= EVIDENCE_CAP && (
          <p className="text-[12px] text-k-text/55">
            The server returns at most {EVIDENCE_CAP} sightings, and this one is at the limit, so there may be more.
          </p>
        )}
        {evidence.length === 0 ? (
          loading ? (
            <div className="space-y-2">
              <Skeleton h="h-12" className="w-full" />
              <Skeleton h="h-12" className="w-full" />
            </div>
          ) : (
            <p className="text-[13px] text-k-text/60">No sightings are stored for this one: it carries a title and nothing that links back out.</p>
          )
        ) : (
          <ul className="divide-y divide-k-border border border-k-border">
            {evidence.map((ev, i) => (
              <EvidenceRow key={`${ev.source}-${ev.region ?? ""}-${ev.url ?? ""}-${i}`} ev={ev} />
            ))}
          </ul>
        )}
      </Section>

      {loading && !research ? (
        <div className="space-y-2">
          <Skeleton h="h-4" className="w-2/5" />
          <Skeleton h="h-3" className="w-full" />
        </div>
      ) : (
        research && nonEmpty(research.what_it_is) && <ResearchBlock research={research} />
      )}

      <Reasoning topic={trend.topic} fitReason={trend.fit_reason} loading={loading} scored={scored} delay={320} />

      <AllDetails tags={tags}>
        <Row label="Regions" value={trend.regions.length ? trend.regions.map((r) => generalRegionLabel(r)).join(", ") : "none recorded"} />
        <Row
          label="Content filter"
          value={trend.safety === "flagged" ? "held back" : "passed"}
          note={trend.safety === "flagged" ? `Held back before scoring: ${safetyReasonWords(trend.safety_reason)}` : "Screened before scoring, and nothing was caught."}
        />
        <Row label="How people reacted" value="not measured" note="We store no comment text from any of these sources." />
        <Row label="First seen" value={fmtStamp(trend.first_seen_at)} />
        <Row label="Last seen" value={fmtStamp(trend.last_seen_at)} note='"Last seen" only says a source still lists it.' />
        <Row label="Newest content" value={fmtStamp(trend.latest_content_at)} note="This one says whether anything NEW is happening." />
        <Row label="Row added" value={fmtStamp(trend.created_at)} />
        <Row label="Type of trend" value={nonEmpty(trend.trend_kind) ?? "not set"} />
        <Row label="How sensitive" value={nonEmpty(trend.sensitivity) ?? "not set"} note="The AI's read of whether a school should touch it." />
        <Row label="Fit for 42" value={fmtScore(trend.fit_score)} note="0-100. The AI's own number." />
        <Row label="UAE relevance" value={fmtScore(trend.uae_relevance)} note="0-100. The AI's own number." />
        <Row label="Opportunity" value={fmtScore(trend.opportunity_score)} note="Worked out here: heat blended with the two scores." />
        <Row label="Scored" value={fmtStamp(trend.scored_at)} />
      </AllDetails>
    </>
  );
}

/** First seen → newest content → last seen, placed on one line by date. */
function Timeline({ first, newest, last }: { first: string | null; newest: string | null; last: string | null }) {
  const pts = [
    { key: "first", label: "First seen", iso: first },
    { key: "newest", label: "Newest content", iso: newest },
    { key: "last", label: "Last seen", iso: last },
  ]
    .map((p) => ({ ...p, t: p.iso ? new Date(p.iso).getTime() : NaN }))
    .filter((p) => Number.isFinite(p.t));
  if (pts.length < 2) return null;
  const lo = Math.min(...pts.map((p) => p.t));
  const hi = Math.max(...pts.map((p) => p.t));
  const span = hi - lo || 1;
  return (
    <div className="k-inset px-5 pb-4 pt-5">
      <div className="relative h-16">
        <span className="absolute left-0 right-0 top-[7px] h-[2px] bg-k-text/15" aria-hidden />
        <span className="absolute left-0 top-[7px] h-[2px] bg-k-accent k-grow-x" style={{ width: "100%", animationDelay: "350ms" }} aria-hidden />
        {pts.map((p, i) => {
          const x = ((p.t - lo) / span) * 100;
          const align = x < 12 ? "left-[-7px]" : x > 88 ? "right-[-7px] text-right" : "-translate-x-1/2 text-center";
          return (
            <div key={p.key} className="absolute top-0" style={{ left: `${x}%` }}>
              <span
                className="absolute -left-[7px] top-0 block h-4 w-4 rotate-45 border-2 border-k-accent bg-k-surface k-rise"
                style={{ animationDelay: `${450 + i * 120}ms` }}
                aria-hidden
              />
              <span className={`absolute top-6 whitespace-nowrap ${align}`}>
                <span className="block text-[11px] font-bold uppercase tracking-[0.06em] text-k-text/55">{p.label}</span>
                <span className="block text-[13px] font-semibold text-k-text">{fmtDay(p.iso)}</span>
              </span>
            </div>
          );
        })}
      </div>
    </div>
  );
}

function EvidenceRow({ ev }: { ev: GeneralTrendEvidenceDetail }) {
  const label = sourceLabel(ev.source);
  const region = ev.region && ev.region !== "global" ? generalRegionLabel(ev.region) : null;
  const metric =
    ev.metric_value != null && Number.isFinite(ev.metric_value)
      ? `${fmtCompact(ev.metric_value)}${ev.metric_label ? ` ${ev.metric_label}` : ""}`
      : null;
  const title = nonEmpty(ev.title);
  const summary = nonEmpty(ev.summary);
  const published = nonEmpty(ev.published_at);
  return (
    <li className="flex flex-wrap items-start gap-x-3 gap-y-1 px-4 py-3">
      <span className="shrink-0 px-1.5 py-0.5 text-[10.5px] font-bold uppercase tracking-[0.06em] bg-k-btn text-k-onbtn">{label}</span>
      <span className="min-w-0 flex-1 text-[13.5px] leading-snug text-k-text">
        {ev.url ? (
          <a href={ev.url} target="_blank" rel="noopener noreferrer" className="break-words underline decoration-k-border underline-offset-2 hover:decoration-k-btn">
            {title ?? ev.url}
          </a>
        ) : (
          <span className="break-words">{title ?? "no link recorded"}</span>
        )}
        {summary && <span className="mt-0.5 block break-words text-[12.5px] text-k-text/60">{summary}</span>}
        <span className="mt-1 block text-[11.5px] text-k-text/45">
          {published && <>published {fmtStamp(published)} · </>}seen {fmtStamp(ev.seen_at)}
        </span>
      </span>
      {region && <span className="shrink-0 px-1.5 py-0.5 text-[11px] text-k-text/60 ring-1 ring-k-border">{region}</span>}
      {metric && <span className="shrink-0 k-num text-[15px] tabular-nums text-k-accent">{metric}</span>}
    </li>
  );
}

function ResearchBlock({ research }: { research: GeneralTrendResearch }) {
  const sources = (research.sources ?? []).filter((s) => s.uri || s.title);
  return (
    <Section title="What this actually is" provenance="ai" delay={300}>
      <div className="space-y-3">
        <Quote label="In plain words" text={research.what_it_is} />
        {nonEmpty(research.why_people_post) && <Quote label="Why people post it" text={research.why_people_post} />}
        {sources.length > 0 && (
          <div>
            <p className="mb-1 text-[11px] font-bold uppercase tracking-[0.08em] text-k-text/55">Pages the AI read (real pages, its summary)</p>
            <ul className="space-y-1">
              {sources.map((s, i) => (
                <li key={`${s.uri || s.title}-${i}`}>
                  <a
                    href={s.uri || undefined}
                    target="_blank"
                    rel="noopener noreferrer"
                    className="break-all text-[12.5px] text-k-text/65 underline decoration-k-border underline-offset-2 hover:text-k-text hover:decoration-k-btn"
                  >
                    {s.title || s.uri}
                  </a>
                </li>
              ))}
            </ul>
          </div>
        )}
        {nonEmpty(research.researched_at) && (
          <p className="text-[11.5px] text-k-text/45">
            Looked up {fmtStamp(research.researched_at)}
            {research.cached ? " · served from cache" : ""}
          </p>
        )}
      </div>
    </Section>
  );
}

// ---------------------------------------------------------------------------
// Headlines
// ---------------------------------------------------------------------------

function generalHeadline(trend: GeneralTrend): string {
  return nonEmpty(trend.title?.replace(/\s+/g, " ")) ?? nonEmpty(trend.topic) ?? "Untitled trend";
}

/** The sources it was seen on. */
function generalSubline(trend: GeneralTrend): string {
  return trend.sources.length ? trend.sources.map((s) => sourceLabel(s)).join(" · ") : "no sources recorded";
}

/**
 * News inverts the order, the same way the card and the Make-this header do:
 * the real headline IS the story, while `topic` is 42's idea ABOUT the story.
 */
function postHeadline(post: TrendPost): string {
  const caption = nonEmpty(post.caption?.split("\n")[0]);
  const best = post.source === "news" ? (caption ?? nonEmpty(post.topic)) : (nonEmpty(post.topic) ?? caption);
  return best ?? (post.username ? `@${post.username.replace(/^@/, "")}` : "Untitled post");
}

/** Who, and how big or how recent. A news row has no account behind it. */
function postSubline(post: TrendPost): string {
  if (post.source === "news") {
    const outlet = nonEmpty(post.platform) ?? nonEmpty(post.username) ?? "unnamed outlet";
    return `${outlet} · ${fmtStamp(post.posted_at)}`;
  }
  const handle = post.username ? `@${post.username.replace(/^@/, "")}` : "no account recorded";
  return post.competitor_followers != null ? `${handle} · ${fmtCompact(post.competitor_followers)} followers` : handle;
}
