/**
 * Everything we know about ONE trend, in a dialog.
 *
 * WHY this exists. The cards are deliberately lossy — line-clamped reasons, a
 * six-row evidence preview, no research block, no timeline — because a 36-card
 * grid has to stay scannable. That was fine while the Trends page was a reading
 * surface. It stopped being fine the moment the same rows started feeding a
 * downloadable report: a reader who is about to act on "108,398 likes" needs to
 * be able to click through to where that number came from.
 *
 * WHY the provenance labelling is not decoration. Half of what a card shows is
 * COUNTED (likes, comments, views, sightings, dates) and half is a model's
 * OPINION (fit, angle, theme, sensitivity, tone). They look identical on a card
 * — both are just text in a chip — and the report is about to hand people both
 * kinds side by side. So every block here carries a chip saying which it is,
 * and derived arithmetic (engagement rate, outperformance, heat, opportunity)
 * says so in its own note rather than passing as a raw measurement.
 *
 * WHY the order is what it is. This panel used to open on a legend: three lines
 * teaching the reader to read chips they had not seen yet, above a full-width
 * image, above eight blocks of equal weight. Nobody opens a detail view to be
 * taught a notation. They open it to find out whether the thing is worth acting
 * on, so the order is now:
 *
 *   1. the answer      the one number the card led with, at the size that says
 *                      so, the fit scores beside it, and one plain sentence
 *   2. the workings    the counted numbers, grouped by the question they
 *                      answer instead of listed flat
 *   3. where it came   the account, the outlet, the sightings, the caption
 *   4. what the AI says clearly fenced off, and last of the reading
 *   5. how to read it  the legend, as a footnote, where a reference belongs
 *   6. actions         the report links, the source, the way out
 *
 * Two things this file will NEVER print:
 *   • a share count — the Instagram Graph API does not expose one, so the row
 *     reads "not available" rather than a zero that would be read as "nobody
 *     shared it";
 *   • an audience sentiment — we store no comment text, so nothing here
 *     measures how anyone felt.
 *
 * Note the deliberate import cycle with `GeneralTrendCard`: that module owns the
 * source/region label maps and the heat pill, and imports this one for its
 * Details button. Both sides only touch each other inside component bodies,
 * never at module scope, so the cycle resolves before anything renders.
 */
import {
  useEffect,
  useId,
  useRef,
  useState,
  type ComponentType,
  type KeyboardEvent as ReactKeyboardEvent,
  type ReactNode,
} from "react";
import { createPortal } from "react-dom";
import {
  Bot,
  ExternalLink,
  Globe,
  Layers,
  Ruler,
  ShieldAlert,
  X,
} from "lucide-react";

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
import {
  generalRegionLabel,
  HeatPill,
  safetyReasonWords,
  sourceLabel,
} from "./GeneralTrendCard";
import {
  Avatar,
  coverKindOf,
  ErrorCard,
  fmtCompact,
  fmtMultiple,
  fmtScore,
  FreshnessPill,
  OutperformancePill,
  regionLabel,
  ScoreBadge,
  viralBand,
  viralOf,
} from "./shared";
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
// Provenance — the point of the whole dialog
// ---------------------------------------------------------------------------

type Provenance = "measured" | "ai";

const PROVENANCE_CHIP: Record<Provenance, string> = {
  measured: "text-accent border-accent/30 bg-accent-fade",
  ai: "text-warning border-warning/25 bg-warning/10",
};

const PROVENANCE_WORD: Record<Provenance, string> = {
  measured: "Measured",
  // "AI opinion", not "AI judgement": the point is that it can be wrong, and
  // "judgement" sounds like a ruling. Same word the reports use.
  ai: "AI opinion",
};

const PROVENANCE_TITLE: Record<Provenance, string> = {
  measured:
    "Counted where we found it, or worked out from numbers that were counted. Anything worked out says so underneath it.",
  ai: "A language model wrote this from the text we hold. It is an opinion, not a measurement, and it can be wrong.",
};

function ProvenanceChip({ kind }: { kind: Provenance }) {
  const Icon = kind === "measured" ? Ruler : Bot;
  return (
    <span
      title={PROVENANCE_TITLE[kind]}
      className={`inline-flex shrink-0 items-center gap-1 rounded border px-1.5 py-0.5 font-mono text-3xs font-medium uppercase tracking-wider2 ${PROVENANCE_CHIP[kind]}`}
    >
      <Icon className="h-3 w-3" aria-hidden />
      {PROVENANCE_WORD[kind]}
    </span>
  );
}

/**
 * The legend.
 *
 * It sits at the FOOT of the panel now. It used to be the first thing in the
 * body, which taught a notation to a reader who had not yet seen a single chip
 * and pushed the number they came for below the fold. The chips say "Measured"
 * and "AI opinion" in words and carry the same sentences as tooltips, so
 * nothing depends on having read this first: it is a reference, and a reference
 * belongs at the end.
 */
function ProvenanceLegend() {
  return (
    <div className="rounded-lg border border-border bg-bg-muted p-3">
      <p className="eyebrow mb-2">How to read this panel</p>
      <ul className="space-y-1.5 text-2xs leading-relaxed text-muted">
        <li className="flex items-start gap-2">
          <ProvenanceChip kind="measured" />
          <span>
            Counted by the platform we read it from, or worked out from counted
            numbers. Rows that are worked out name what from.
          </span>
        </li>
        <li className="flex items-start gap-2">
          <ProvenanceChip kind="ai" />
          <span>
            Written by a language model from the text we hold. Useful for
            deciding what to make; never quote it as a finding.
          </span>
        </li>
      </ul>
    </div>
  );
}

/**
 * One titled block with its provenance stated in the header.
 *
 * `Icon` and `aside` exist so the evidence list and the research block can use
 * this too: both used to hand-roll the same header, and the three had already
 * drifted apart by a pixel or two.
 */
function Block({
  title,
  provenance,
  note,
  Icon,
  aside,
  children,
}: {
  title: string;
  provenance: Provenance;
  note?: string;
  /** `ComponentType<any>` for the same reason `CoverTile` uses it: a lucide
   *  icon is a forwardRef whose props do not satisfy a hand-written signature. */
  Icon?: ComponentType<any>;
  /** Sits after the title, inside the heading: a count, a state. */
  aside?: ReactNode;
  children: ReactNode;
}) {
  return (
    <section className="card p-4">
      <div className="mb-3 flex flex-wrap items-center justify-between gap-2 border-b border-border pb-2">
        <h3 className="flex min-w-0 items-center gap-2 text-sm font-semibold text-fg">
          {Icon && (
            <Icon
              className="h-4 w-4 shrink-0 text-muted"
              strokeWidth={1.8}
              aria-hidden
            />
          )}
          <span className="min-w-0 break-words">{title}</span>
          {aside}
        </h3>
        <ProvenanceChip kind={provenance} />
      </div>
      {note && (
        <p className="-mt-1 mb-3 text-2xs leading-relaxed text-muted">{note}</p>
      )}
      {children}
    </section>
  );
}

/**
 * A named set of rows inside a block.
 *
 * The blocks used to be one flat `dl` each: eleven label/value pairs in one
 * grid, where "Likes" and "Engagement rate" and "Shares: not available" all
 * looked equally important. Grouping them by the question they answer is the
 * whole hierarchy fix: a reader scanning for "did this do well" reads four
 * rows, not eleven.
 */
function Group({
  title,
  note,
  children,
}: {
  title: string;
  note?: string;
  children: ReactNode;
}) {
  return (
    <div className="min-w-0">
      {/* A shade darker than the row labels below it, so the group reads as
          the parent of its rows rather than as another one of them. */}
      <p className="eyebrow mb-2 text-fg/70">{title}</p>
      <dl className={GRID}>{children}</dl>
      {note && (
        <p className="mt-2 text-3xs leading-relaxed text-muted">{note}</p>
      )}
    </div>
  );
}

/**
 * A label/value pair. `note` is where a worked-out number admits what it is.
 *
 * The note lives INSIDE the `dd` rather than beside it: a caveat that only
 * applies to one value should be read out with that value, and a `<p>` between
 * a `dd` and the next `dt` is not what a definition list means.
 */
function Row({
  label,
  value,
  note,
  mono = true,
}: {
  label: string;
  value: ReactNode;
  note?: string;
  mono?: boolean;
}) {
  return (
    <div className="min-w-0">
      <dt className="eyebrow mb-0.5">{label}</dt>
      <dd
        className={`break-words text-[13px] leading-snug text-fg ${
          mono ? "font-mono tabular-nums" : ""
        }`}
      >
        {value}
        {note && (
          <span className="mt-0.5 block font-sans text-3xs font-normal leading-relaxed tracking-normal text-muted">
            {note}
          </span>
        )}
      </dd>
    </div>
  );
}

/** A wrapped paragraph field — angles and reasons, not numbers. */
function Para({ label, text }: { label: string; text: string }) {
  return (
    <div className="min-w-0">
      <p className="eyebrow mb-1">{label}</p>
      <p className="whitespace-pre-wrap break-words text-xs leading-relaxed text-fg/85">
        {text}
      </p>
    </div>
  );
}

const GRID = "grid gap-x-5 gap-y-3 sm:grid-cols-2";

/**
 * Mirrors `general_trends.DETAIL_EVIDENCE_MAX`. Only used to decide whether to
 * warn that the list may be truncated — if the backend lowers its cap the worst
 * that happens is the warning stops appearing, never a wrong number on screen.
 */
const EVIDENCE_CAP = 200;

// ---------------------------------------------------------------------------
// Words for the values the database stores as codes
// ---------------------------------------------------------------------------

/**
 * These maps are the de-jargoning. The database says `competitor`, `new`,
 * `CAROUSEL_ALBUM`; a reader should not have to know that. Unknown values fall
 * through to the raw string rather than to a blank, because a code the reader
 * can quote at us beats a silence.
 */
const CHANNEL_WORDS: Record<string, string> = {
  competitor: "We watch this account",
  news: "The industry news collector found it",
};

const STATUS_WORDS: Record<string, string> = {
  new: "New, nobody has triaged it",
  saved: "Saved",
  dismissed: "Dismissed",
  used: "Marked as used",
};

const MEDIA_WORDS: Record<string, string> = {
  IMAGE: "Photo",
  VIDEO: "Video",
  CAROUSEL_ALBUM: "Carousel",
};

// ---------------------------------------------------------------------------
// Formatting
// ---------------------------------------------------------------------------

/** Full date AND time — a detail view is where someone checks the exact moment. */
function fmtStamp(iso: string | null | undefined): string {
  if (!iso) return "not recorded";
  const t = new Date(iso);
  if (Number.isNaN(t.getTime())) return iso;
  return t.toLocaleString(undefined, {
    year: "numeric",
    month: "short",
    day: "numeric",
    ...CLOCK_12H,
  });
}

/** `engagement_rate` is stored as a FRACTION of followers (eng / followers). */
function fmtRate(v: number | null | undefined): string {
  if (v == null || !Number.isFinite(v)) return "not known";
  return `${(v * 100).toFixed(2)}%`;
}

function fmtVelocity(v: number | null | undefined): string {
  if (v == null || !Number.isFinite(v)) return "not worked out yet";
  return `${Math.round(v * 100)} / 100`;
}

/** Never renders an empty string as if it were a value. Named away from
 *  `text` so it cannot be shadowed by the `text` prop `Para` takes. */
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

/**
 * Everything Tab can land on inside the panel. Used by the focus trap, which
 * has to know where the ring is allowed to go before it can stop it leaving.
 */
const FOCUSABLE =
  'a[href],button:not([disabled]),input:not([disabled]),select:not([disabled]),' +
  'textarea:not([disabled]),[tabindex]:not([tabindex="-1"])';

export function TrendDetailDialog({
  target,
  onClose,
}: {
  target: TrendDetailTarget;
  onClose: () => void;
}) {
  const id = targetId(target);

  // The server's copy. Null until it lands — the seed row from the card is what
  // renders in the meantime, so the dialog is never a spinner in a void.
  const [trend, setTrend] = useState<GeneralTrend | null>(
    target.kind === "general" ? target.trend : null,
  );
  const [post, setPost] = useState<TrendPost | null>(
    target.kind === "post" ? target.post : null,
  );
  // Seeded with the card's preview rows (a `GeneralTrendEvidence` is a
  // `GeneralTrendEvidenceDetail` with the six extra fields absent), replaced by
  // the server's full list the moment it lands.
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
  /** Whatever had focus when this opened, so closing can hand it straight back
   *  instead of dropping the keyboard at the top of the document. */
  const openerRef = useRef<Element | null>(null);
  /** True only when the press that started a click landed on the backdrop
   *  itself. Selecting a caption and releasing the mouse outside the panel used
   *  to close it, throwing away the scroll position and the fetched detail. */
  const pressedBackdrop = useRef(false);

  // The dialog's own ids. `useId` rather than a literal, because two panels can
  // in principle be mounted at once and duplicate ids would point every
  // `aria-labelledby` at whichever title happens to be first in the document.
  const titleId = useId();
  const sublineId = useId();

  // `onClose` without the effect below depending on its identity. Both callers
  // pass an inline arrow, so its identity changes on every render of the card
  // that owns it. See the mount effect for why that matters.
  const onCloseRef = useRef(onClose);
  useEffect(() => {
    onCloseRef.current = onClose;
  });

  /**
   * Mount-only, and the empty dependency list is load-bearing.
   *
   * Everything here is about the dialog EXISTING: the page behind it must not
   * scroll, focus must come inside, Escape must close, and the opener must get
   * focus back. With `[onClose]` this re-ran on every render of the card that
   * owns it (and `TrendsFeed` re-renders the whole grid every few seconds
   * while a scan is polling), which yanked focus back to the Close button while
   * someone was tabbing through the sightings.
   */
  useEffect(() => {
    openerRef.current = document.activeElement;

    const prevOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";

    function onKey(e: KeyboardEvent) {
      if (e.key === "Escape") onCloseRef.current();
    }
    window.addEventListener("keydown", onKey);

    // Into the panel, on the one control that is always there. The dialog role
    // and its label are announced with it, so the reader hears what opened.
    closeRef.current?.focus();

    return () => {
      document.body.style.overflow = prevOverflow;
      window.removeEventListener("keydown", onKey);
      const opener = openerRef.current;
      // Only if it is still in the document: the card behind can be gone by
      // now (a filter changed, a refresh landed), and focusing a detached node
      // silently does nothing while looking like it worked.
      if (opener instanceof HTMLElement && document.contains(opener)) {
        opener.focus();
      }
    };
  }, []);

  /**
   * The focus trap.
   *
   * A modal that Tab can walk out of is a modal only to the mouse: the reader
   * ends up filling in the filter bar they cannot see, behind an overlay. This
   * wraps the ring from the last control back to the first and from the first
   * back to the last. Elements that are not laid out (`offsetParent === null`,
   * which covers `[hidden]` and `display:none`) are skipped, so a collapsed
   * region never becomes an invisible stop.
   */
  function trapTab(e: ReactKeyboardEvent<HTMLDivElement>) {
    if (e.key !== "Tab") return;
    const root = panelRef.current;
    if (!root) return;
    const items = Array.from(
      root.querySelectorAll<HTMLElement>(FOCUSABLE),
    ).filter((el) => el.offsetParent !== null || el === document.activeElement);
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
          // Every sighting, not the preview the list embeds. An empty array is
          // a real answer (a row can genuinely have none), so it replaces the
          // preview either way — but the flag stops us claiming "complete"
          // while the preview is still what is on screen.
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
        // `http()` prefixes the status; 404 is a different state from a
        // failure, because the row is gone rather than unreachable.
        if (/^404\b/.test(raw)) {
          setNotFound(true);
        } else {
          setError(humanError(e, "Couldn't load the details"));
        }
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

  const heading = shownTrend
    ? generalHeadline(shownTrend)
    : shownPost
      ? postHeadline(shownPost)
      : "Untitled";

  const eyebrowText = shownTrend
    ? "Viral social trend"
    : shownPost?.source === "news"
      ? "Industry signal"
      : "Competitor post";

  const subline = shownTrend
    ? generalSubline(shownTrend)
    : shownPost
      ? postSubline(shownPost)
      : "";

  // Only a competitor post has an account behind it. A news row's `username`
  // holds the OUTLET's name, so an avatar and an "@" there would invent an
  // Instagram account that does not exist.
  const avatarFor =
    shownPost && shownPost.source !== "news" ? shownPost : null;

  const sourceHref = shownTrend ? shownTrend.url : (shownPost?.permalink ?? null);

  return createPortal(
    <div
      // The backdrop, and nothing else. The dialog role used to live here,
      // which made the whole overlay the dialog and put its accessible name on
      // a click target rather than on the panel. Clicking it is a convenience
      // that Escape and the two Close buttons already provide, so it stays
      // presentational rather than pretending to be a control.
      role="presentation"
      style={{
        position: "fixed",
        inset: 0,
        zIndex: 100,
        background: "rgba(0,0,0,0.7)",
        backdropFilter: "blur(2px)",
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        padding: 16,
      }}
      // Portalled to `document.body` on purpose. `animate-pageEnter` leaves a
      // transform on the page wrapper (`fill-mode: both`), and a transformed
      // ancestor becomes the containing block for `position: fixed`, so an
      // overlay rendered inside the page would measure the PAGE instead of the
      // viewport. `document.body` has no transform, so `inset: 0` here is the
      // viewport. Same reason `MakeThisFlow` portals.
      onMouseDown={(e) => {
        pressedBackdrop.current = e.target === e.currentTarget;
      }}
      onClick={(e) => {
        if (e.target === e.currentTarget && pressedBackdrop.current) onClose();
      }}
    >
      <div
        ref={panelRef}
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        aria-describedby={sublineId}
        onKeyDown={trapTab}
        className="card flex max-h-[85vh] w-full max-w-3xl flex-col overflow-hidden"
      >
        {/* ------------------------------ header ------------------------- */}
        {/* Not sticky, and deliberately outside the scrolling body: a header
            that floated over the scroll area would cover a control the moment
            Tab scrolled it under, which is the `focus-not-obscured` failure. */}
        <div className="flex shrink-0 items-start justify-between gap-3 border-b border-border p-5">
          <div className="min-w-0">
            <p className="eyebrow mb-1">{eyebrowText}</p>
            <h2
              id={titleId}
              className="break-words text-lg font-semibold text-fg"
            >
              {heading}
            </h2>
            <p className="mt-1.5 flex flex-wrap items-center gap-x-2 gap-y-1 font-mono text-2xs text-muted">
              {avatarFor && (
                <Avatar
                  src={avatarFor.competitor_avatar}
                  name={avatarFor.competitor_name || avatarFor.username}
                  size={18}
                />
              )}
              {/* The id is on the TEXT, not on the row: the avatar's letter
                  fallback is inside the row too, and a description that opens
                  with a stray "Y" helps nobody. */}
              <span id={sublineId} className="min-w-0 break-words">
                {subline}
              </span>
            </p>
          </div>
          <button
            ref={closeRef}
            onClick={onClose}
            className="btn btn-ghost px-2"
            aria-label="Close"
          >
            <X className="h-4 w-4" strokeWidth={1.8} />
          </button>
        </div>

        {/* ------------------------------- body -------------------------- */}
        <div className="min-h-0 flex-1 space-y-4 overflow-y-auto p-5">
          {notFound ? (
            <div className="card p-6 text-center">
              <p className="text-sm font-semibold text-fg">
                This one is no longer in the database
              </p>
              <p className="lead mx-auto mt-2 max-w-md text-sm">
                It was removed after the page loaded. A refresh drops rows that
                fell out of the window. Close this and reload the tab.
              </p>
            </div>
          ) : (
            <>
              {error && <ErrorCard message={error} />}
              {error && (
                <p className="text-2xs leading-relaxed text-muted">
                  Showing what the card already had. Anything only the full
                  record holds is missing until this loads.
                </p>
              )}

              {shownTrend ? (
                <GeneralBody
                  trend={shownTrend}
                  evidence={evidence}
                  evidencePreview={evidencePreview}
                  research={research}
                  loading={loading}
                />
              ) : shownPost ? (
                <PostBody post={shownPost} loading={loading} />
              ) : null}

              <ProvenanceLegend />
            </>
          )}
        </div>

        {/* ------------------------------ footer ------------------------- */}
        {/* Where a reader looks once they have finished reading: the source and
            the way out. A per-row PDF used to sit here too. It is gone on
            purpose: reports are now reached from one place, the Reports tab,
            so there is exactly one screen to learn and one document to trust. */}
        <div className="shrink-0 border-t border-border p-4">
          <div className="flex flex-wrap items-center gap-x-3 gap-y-2">
            {sourceHref && (
              <a
                href={sourceHref}
                target="_blank"
                rel="noopener noreferrer"
                className="btn btn-ghost text-xs"
              >
                <ExternalLink className="h-3.5 w-3.5" aria-hidden />
                Open the source
              </a>
            )}

            <button
              type="button"
              onClick={onClose}
              className="btn btn-primary ml-auto text-xs"
            >
              Close
            </button>
          </div>
        </div>
      </div>
    </div>,
    document.body,
  );
}

// ---------------------------------------------------------------------------
// The answer strip: the first thing in the body, for both kinds
// ---------------------------------------------------------------------------

/**
 * The one thing someone opens this panel for, at the size that says so.
 *
 * Reading order inside it is measured first, then the model: the hero pill and
 * its one sentence of plain English on top, a hairline, then the fit numbers
 * and the model's summary underneath. That hairline is the whole provenance
 * argument in miniature, which is why the two chips sit on either side of it.
 *
 * The picture is a 160px tile rather than the full-width band this used to
 * open with. A 288px-tall image is not the answer to any question a reader
 * came here with, and it pushed every number below the fold; "Open the source"
 * in the footer is one click away for anyone who wants to see the real thing.
 */
function Summary({
  media,
  hero,
  heroLine,
  scored,
  plain,
  scores,
}: {
  media: ReactNode;
  /** The pill the card led with: outperformance, freshness or heat. */
  hero: ReactNode;
  /** One sentence saying what the pill's number actually means. */
  heroLine: string;
  scored: boolean;
  /** The model's plain-words summary, when it has written one. */
  plain: string | null;
  scores: ReactNode;
}) {
  return (
    <section className="card overflow-hidden">
      <div className="flex flex-col gap-4 p-4 sm:flex-row sm:items-start">
        {media}
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-x-3 gap-y-2">
            {hero}
            <p className="min-w-0 flex-1 text-[13px] leading-snug text-fg/80">
              {heroLine}
            </p>
            <ProvenanceChip kind="measured" />
          </div>

          <div className="mt-3 border-t border-border pt-3">
            {scored ? (
              <>
                <div className="flex flex-wrap items-center gap-1.5">
                  <ProvenanceChip kind="ai" />
                  {scores}
                </div>
                {plain && (
                  <p className="mt-2 text-sm leading-relaxed text-fg">{plain}</p>
                )}
              </>
            ) : (
              <p className="text-xs leading-relaxed text-muted">
                Not scored yet. The next scoring run fills in the fit numbers
                and the model's read. Nothing is being held back.
              </p>
            )}
          </div>
        </div>
      </div>
    </section>
  );
}

/** The three numbers the feed ranks on, in the order the feed uses them. */
function ScoreRow({
  fit,
  fitReason,
  uae,
  opportunity,
  opportunityTitle,
}: {
  fit: number | null;
  fitReason: string | null;
  uae: number | null;
  opportunity: number | null;
  opportunityTitle: string;
}) {
  return (
    <>
      <ScoreBadge
        label="fit"
        value={fit}
        title={fitReason ?? "How practical this is for 42 to make"}
      />
      <ScoreBadge
        label="uae"
        value={uae}
        title="How well the idea lands with a UAE audience"
      />
      <ScoreBadge
        label="opportunity"
        value={opportunity}
        title={opportunityTitle}
      />
    </>
  );
}

/** The 160px picture tile the summary opens with, or the tinted stand-in. */
function MediaTile({ children }: { children: ReactNode }) {
  return (
    <div className="h-40 w-full shrink-0 overflow-hidden rounded-lg border border-border bg-bg-muted sm:w-40">
      {children}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Competitor post / news story
// ---------------------------------------------------------------------------

function PostBody({ post, loading }: { post: TrendPost; loading: boolean }) {
  const isNews = post.source === "news";
  const meta: Record<string, unknown> = post.metadata ?? {};
  // The publisher's own headline, kept because the card shows OUR plain
  // rewrite. This is where a reader checks what that rewrite was made from,
  // one line above the link to the story itself. Nothing is hidden; it is just
  // not what a feed card leads with.
  const originalHeadline =
    typeof meta.original_headline === "string" ? meta.original_headline : "";
  const whyItMatters =
    typeof meta.why_it_matters === "string" ? meta.why_it_matters : "";
  const viral = viralOf(post);
  const dateIsEditTime = meta.date_is_estimated === true;
  const hasImage = Boolean(post.local_thumb_path || post.thumbnail_url);
  // Who the story is about, when the judge named a company: the logo tile's
  // caption, because the mark on it is theirs and not the publisher's.
  const company = typeof meta.company === "string" ? meta.company : "";
  const tags = tagsOf(post.tags);
  const handle = post.username?.replace(/^@/, "") ?? "";

  const opportunityTitle = isNews
    ? "How the feed ranks this story: freshness blended with the two AI scores"
    : "How the feed ranks this post: outperformance blended with the two AI scores";

  return (
    <>
      <Summary
        media={
          <MediaTile>
            {coverKindOf(post) === "logo" ? (
              // A brand mark, drawn the way the card draws it: small and
              // centred on the tinted tile, captioned with whose it is. Through
              // <Thumb> it would be stretched into a blurry square.
              <CoverTile
                seed={company || post.platform || post.username || "news"}
                label={company || post.platform || post.username || null}
                logoSrc={api.trends.thumbUrl(post.id)}
                compact
              />
            ) : hasImage ? (
              <Thumb
                src={api.trends.thumbUrl(post.id)}
                fallbackSrc={post.thumbnail_url ?? post.media_url ?? null}
                alt={postHeadline(post)}
                eager
                objectFit="contain"
                className="h-full w-full"
                // The same tinted tile the card shows, seeded the same way, so
                // an item whose image has expired looks like ITSELF in both
                // places rather than like a card in the grid and a failure
                // here.
                empty={
                  <CoverTile
                    seed={post.platform || post.username || "news"}
                    label={post.platform || post.username || null}
                    compact
                  />
                }
              />
            ) : (
              <CoverTile
                seed={post.platform || post.username || "news"}
                label={post.platform || post.username || null}
                compact
              />
            )}
          </MediaTile>
        }
        hero={
          isNews ? (
            <FreshnessPill
              postedAt={post.posted_at}
              approximate={dateIsEditTime}
            />
          ) : (
            <OutperformancePill value={post.outperformance} />
          )
        }
        heroLine={
          isNews
            ? dateIsEditTime
              ? "How long ago this was last touched. The source reports edit time, not publication time, so read it as approximate."
              : "How long ago the story went out. A story has no engagement of its own on our side, so recency is the honest headline number."
            : post.outperformance != null && Number.isFinite(post.outperformance)
              ? "Likes and comments against this account's own typical post, so a big account does not look impressive just for being big."
              : "We do not hold enough history for this account to say how this post compares to its own average."
        }
        scored={Boolean(post.scored_at)}
        plain={nonEmpty(post.topic_summary)}
        scores={
          <ScoreRow
            fit={post.fit_score}
            fitReason={post.fit_reason}
            uae={post.uae_relevance}
            opportunity={post.opportunity_score}
            opportunityTitle={opportunityTitle}
          />
        }
      />

      {/* ------------------------------ measured ------------------------- */}
      <Block
        title={isNews ? "Where this came from" : "What Instagram reported"}
        provenance="measured"
        note={
          isNews
            ? "A story has no engagement of its own on our side. The only thing we measure about one is when it went out."
            : "Read through the Instagram Graph API's Business Discovery, which returns likes, comments and (for video only) views. Nothing else about somebody else's post is measurable from outside."
        }
      >
        {isNews ? (
          <div className="space-y-4">
            <Group title="The story itself">
              <Row
                label="Headline as published"
                value={nonEmpty(originalHeadline) ?? "not recorded"}
                mono={false}
                note="The outlet's own words. The headline on the card is our plain-English version of this."
              />
              <Row
                label="Published"
                value={fmtStamp(post.posted_at)}
                note={
                  dateIsEditTime
                    ? "The story arrived without a date, so this is the day we found it. Read it as approximate."
                    : undefined
                }
              />
            </Group>

            <Group title="What we cannot see">
              <Row
                label="How people reacted"
                value="not measured"
                note="We store no comment text, so nothing here measures how anyone felt about it."
              />
            </Group>
          </div>
        ) : (
          <div className="space-y-4">
            <Group title="What it got">
              <Row label="Likes" value={fmtCompact(post.like_count)} />
              <Row label="Comments" value={fmtCompact(post.comments_count)} />
              <Row
                label="Views"
                value={
                  post.view_count != null ? fmtCompact(post.view_count) : "not reported"
                }
                note={
                  post.view_count == null
                    ? "Instagram reports views for VIDEO only. This is not a zero."
                    : undefined
                }
              />
              <Row label="Posted" value={fmtStamp(post.posted_at)} />
            </Group>

            <Group
              title="How that compares"
              note="Worked out here from the counts above. Instagram publishes none of these three."
            >
              <Row
                label="Likes plus comments"
                value={fmtCompact(post.engagement)}
                note="The two counted numbers added together."
              />
              <Row
                label="Share of the audience"
                value={fmtRate(post.engagement_rate)}
                note="Likes plus comments, divided by the account's follower count."
              />
              <Row
                label="Against its own average"
                value={fmtMultiple(post.outperformance)}
                note="Multiples of THIS account's own median post, so it is comparable across accounts of different sizes."
              />
            </Group>

            <Group
              title="What we cannot see"
              note="Two things people ask for that we will not invent."
            >
              {/* The owner's sketch asked for a share count. There isn't one,
                  and printing 0 would be read as "nobody shared it". */}
              <Row
                label="Shares"
                value="not available"
                note="The Instagram Graph API does not report shares on another account's post."
              />
              <Row
                label="How people reacted"
                value="not measured"
                note="We store no comment text, so nothing here measures how anyone felt about it."
              />
            </Group>
          </div>
        )}
      </Block>

      {/* ------------------------------ where from ----------------------- */}
      <Block
        title="Where this came from"
        provenance="measured"
        note="Recorded when we collected it. Facts about the row, not judgements about it."
      >
        <div className="space-y-4">
          <Group title="Who published it">
            {isNews ? (
              <>
                <Row
                  label="Publisher"
                  value={nonEmpty(post.platform) ?? "not recorded"}
                  mono={false}
                />
                {/* Never an "@" on a news row: `username` holds the outlet's
                    NAME there, so an @ invents an account that does not exist. */}
                <Row
                  label="Name in the feed"
                  value={nonEmpty(post.username) ?? "not recorded"}
                  mono={false}
                />
                <Row
                  label="Why it matters"
                  value={nonEmpty(whyItMatters) ?? "not recorded"}
                  mono={false}
                  note="The AI's one-line reason this is worth 42's attention."
                />
                <Row
                  label="Viral potential"
                  value={
                    viral
                      ? `${viral.score} out of 100. ${viralBand(viral.score).label}.`
                      : "not scored yet"
                  }
                  mono={false}
                  note={
                    viral?.reason ||
                    "How likely a post about this would get views and shares. The news feed is ordered by it."
                  }
                />
              </>
            ) : (
              <>
                <Row label="Account" value={handle ? `@${handle}` : "not recorded"} />
                <Row
                  label="Followers"
                  value={fmtCompact(post.competitor_followers)}
                  note="At the time of the scan."
                />
                <Row
                  label="Platform"
                  value={nonEmpty(post.platform) ?? "not recorded"}
                />
              </>
            )}
          </Group>

          <Group title="What kind of row this is">
            <Row
              label="How we found it"
              value={CHANNEL_WORDS[post.source] ?? post.source}
              mono={false}
            />
            {/* News rows carry no region: Industry Signal is not scoped to one
                any more, so the row would say "not recorded" forever. */}
            {!isNews && (
              <Row
                label="Region"
                value={regionLabel(post.region ?? post.competitor_region)}
              />
            )}
            <Row label="Language" value={nonEmpty(post.language) ?? "not recorded"} />
            {post.media_type && (
              <Row
                label="Format"
                value={MEDIA_WORDS[post.media_type] ?? post.media_type}
                mono={false}
              />
            )}
            <Row
              label="Where it sits in the feed"
              value={STATUS_WORDS[post.status] ?? post.status}
              mono={false}
            />
            <Row
              label="Id at the source"
              value={post.ig_media_id}
              note="The id the source gave it. Quote this if a number here ever needs checking."
            />
          </Group>
        </div>

        {post.caption && (
          <div className="mt-4 border-t border-border pt-3">
            <Para
              label={isNews ? "The story, as the feed gave it" : "Caption, word for word"}
              text={post.caption}
            />
          </div>
        )}
      </Block>

      {/* -------------------------------- AI ----------------------------- */}
      <AiReadBlock
        loading={loading}
        topic={post.topic}
        theme={post.theme}
        fitScore={post.fit_score}
        fitReason={post.fit_reason}
        uae={post.uae_relevance}
        angle={post.suggested_angle}
        format={post.suggested_format}
        effort={post.effort}
        tags={tags}
        opportunity={post.opportunity_score}
        opportunityNote={
          isNews
            ? "Worked out here: freshness blended with the two scores above."
            : "Worked out here: the measured outperformance blended with the two scores above."
        }
        scoredAt={post.scored_at}
        extra={null}
      />
    </>
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
  const hasImage = Boolean(trend.local_thumb_path || trend.image_url);
  const tags = tagsOf(trend.tags);
  const coverSeed = (trend.sources && trend.sources[0]) || trend.title || "trend";
  const coverLabel = (trend.sources && trend.sources[0]) || null;

  return (
    <>
      <Summary
        media={
          <MediaTile>
            {hasImage ? (
              <Thumb
                src={
                  trend.local_thumb_path
                    ? api.generalTrends.thumbUrl(trend.id)
                    : (trend.image_url ?? null)
                }
                fallbackSrc={
                  trend.local_thumb_path ? (trend.image_url ?? null) : null
                }
                alt={generalHeadline(trend)}
                eager
                objectFit="contain"
                className="h-full w-full"
                empty={<CoverTile seed={coverSeed} label={coverLabel} compact />}
              />
            ) : (
              <CoverTile seed={coverSeed} label={coverLabel} compact />
            )}
          </MediaTile>
        }
        hero={
          <HeatPill
            heat={trend.heat}
            velocity={trend.velocity}
            sourceCount={trend.source_count}
          />
        }
        heroLine="How fast and how widely this is spreading across the public sources we poll. Worked out from the counts below, not a number any platform publishes."
        scored={Boolean(trend.scored_at)}
        plain={nonEmpty(trend.topic_summary ?? trend.summary)}
        scores={
          <ScoreRow
            fit={trend.fit_score}
            fitReason={trend.fit_reason}
            uae={trend.uae_relevance}
            opportunity={trend.opportunity_score}
            opportunityTitle="How the feed ranks this trend: heat blended with the two AI scores"
          />
        }
      />

      {/* A held-back row is the one fact about it that changes what a reader
          should do next, so it goes near the top rather than as row eight of a
          grid. The quiet "passed" case stays a Row below: a filter that says
          nothing when it passed something is a filter nobody can audit. */}
      {trend.safety === "flagged" && (
        <Notice tone="warning" Icon={ShieldAlert}>
          Our content filter held this back before scoring:{" "}
          {safetyReasonWords(trend.safety_reason)}. It was never sent to the AI,
          and it only appears in the feed when "Hidden trends" is on.
        </Notice>
      )}

      {/* ------------------------------ measured ------------------------- */}
      <Block
        title="How far it has spread"
        provenance="measured"
        note="Counted across the public sources we poll."
      >
        <div className="space-y-4">
          <Group title="How many places carried it">
            <Row
              label="Sources carrying it"
              value={String(trend.source_count)}
              note={trend.sources.map((s) => sourceLabel(s)).join(", ") || undefined}
            />
            <Row
              label="Separate places we saw it"
              value={String(trend.evidence_count)}
              note="One count per source and item. This is the number that says how widely it was picked up."
            />
            <Row
              label="Times we re-checked it"
              value={String(trend.sighting_count)}
              note="How often a source still listed this when we polled. It says the trend is still alive, NOT that more places carried it."
            />
            <Row
              label="Biggest number reported"
              value={fmtCompact(trend.peak_metric)}
              note="The largest figure any single source gave. Each source counts something different, so read it next to the sightings below."
            />
            <Row
              label="Regions"
              value={
                trend.regions.length
                  ? trend.regions.map((r) => generalRegionLabel(r)).join(", ")
                  : "none recorded"
              }
            />
          </Group>

          <Group title="What we worked out from that">
            <Row
              label="Heat"
              value={`${trend.heat.toUpperCase()} · ${fmtVelocity(trend.velocity)}`}
              note="Speed and spread, scored out of 100 by us. No platform publishes this."
            />
          </Group>

          <Group title="Checks and gaps">
            <Row
              label="Content filter"
              value={trend.safety === "flagged" ? "held back" : "passed"}
              note={
                trend.safety === "flagged"
                  ? `Held back before scoring: ${safetyReasonWords(trend.safety_reason)}`
                  : "Screened before scoring, and nothing was caught."
              }
            />
            <Row
              label="How people reacted"
              value="not measured"
              note="We store no comment text from any of these sources."
            />
          </Group>
        </div>
      </Block>

      {/* ------------------------------ timeline ------------------------- */}
      <Block
        title="When we saw it"
        provenance="measured"
        // Quoted, not back-ticked: the reader is not looking at code, and the
        // two labels are the point of the sentence.
        note={'"Last seen" only says a source still lists it. "Newest content" is the one that answers whether anything NEW is happening.'}
      >
        <dl className={GRID}>
          <Row label="First seen" value={fmtStamp(trend.first_seen_at)} />
          <Row label="Last seen" value={fmtStamp(trend.last_seen_at)} />
          <Row label="Newest content" value={fmtStamp(trend.latest_content_at)} />
          <Row label="Row added" value={fmtStamp(trend.created_at)} />
        </dl>
      </Block>

      {/* ------------------------------ evidence ------------------------- */}
      <Block
        title="Where we saw it"
        provenance="measured"
        Icon={Layers}
        aside={
          <span className="font-mono text-2xs font-normal text-muted tabular-nums">
            ({evidence.length})
          </span>
        }
      >
        {loading && evidencePreview && (
          <p className="mb-3 text-2xs leading-relaxed text-warning">
            Showing the card's preview rows while the full list loads.
          </p>
        )}

        {/* The server caps the list. Nothing real has come close, but a
            truncated list must never be presented as a complete one. */}
        {!evidencePreview && evidence.length >= EVIDENCE_CAP && (
          <p className="mb-3 text-2xs leading-relaxed text-warning">
            The server returns at most {EVIDENCE_CAP} sightings per trend, and
            this one is at the limit, so there may be more than are listed here.
          </p>
        )}

        {evidence.length === 0 ? (
          loading ? (
            <div className="space-y-2">
              <Skeleton h="h-9" className="w-full" rounded="rounded-lg" />
              <Skeleton h="h-9" className="w-full" rounded="rounded-lg" />
              <Skeleton h="h-9" className="w-4/5" rounded="rounded-lg" />
            </div>
          ) : (
            <p className="text-xs leading-relaxed text-muted">
              No sightings are stored for this row. That is the honest state, not
              an error: the cluster carries a title and nothing that links back
              out.
            </p>
          )
        ) : (
          <ul className="divide-y divide-border">
            {evidence.map((ev, i) => (
              <EvidenceRow
                // The same item can be seen in two regions under one url, so
                // source + url alone is not unique.
                key={`${ev.source}-${ev.region ?? ""}-${ev.url ?? ""}-${i}`}
                ev={ev}
              />
            ))}
          </ul>
        )}
      </Block>

      {/* ------------------------------ research ------------------------- */}
      {loading && !research ? (
        <div className="card space-y-2 p-4">
          <Skeleton h="h-4" className="w-2/5" />
          <Skeleton h="h-3" className="w-full" />
          <Skeleton h="h-3" className="w-4/5" />
        </div>
      ) : (
        research &&
        nonEmpty(research.what_it_is) && <ResearchBlock research={research} />
      )}

      {/* -------------------------------- AI ----------------------------- */}
      <AiReadBlock
        loading={loading}
        topic={trend.topic}
        theme={trend.theme}
        fitScore={trend.fit_score}
        fitReason={trend.fit_reason}
        uae={trend.uae_relevance}
        angle={trend.suggested_angle}
        format={trend.suggested_format}
        effort={trend.effort}
        tags={tags}
        opportunity={trend.opportunity_score}
        opportunityNote="Worked out here: the measured heat blended with the two scores above."
        scoredAt={trend.scored_at}
        extra={
          <>
            <Row
              label="Type of trend"
              value={nonEmpty(trend.trend_kind) ?? "not set"}
              mono={false}
            />
            <Row
              label="How sensitive"
              value={nonEmpty(trend.sensitivity) ?? "not set"}
              note="The model's read of whether a school should touch it."
              mono={false}
            />
          </>
        }
      />
    </>
  );
}

function EvidenceRow({ ev }: { ev: GeneralTrendEvidenceDetail }) {
  const label = sourceLabel(ev.source);
  const region =
    ev.region && ev.region !== "global" ? generalRegionLabel(ev.region) : null;
  const metric =
    ev.metric_value != null && Number.isFinite(ev.metric_value)
      ? `${fmtCompact(ev.metric_value)}${ev.metric_label ? ` ${ev.metric_label}` : ""}`
      : null;
  const title = nonEmpty(ev.title);
  // Detail-only fields — absent while the card's preview rows are what is on
  // screen, so both are rendered conditionally rather than as empty slots.
  const summary = nonEmpty(ev.summary);
  const published = nonEmpty(ev.published_at);

  return (
    <li className="flex flex-wrap items-baseline gap-x-3 gap-y-1 py-2">
      <span className="w-24 shrink-0 font-mono text-2xs font-medium text-fg">
        {label}
      </span>
      <span className="min-w-0 flex-1 text-xs leading-snug text-muted">
        {ev.url ? (
          <a
            href={ev.url}
            target="_blank"
            rel="noopener noreferrer"
            className="break-words underline decoration-border underline-offset-2 transition-colors hover:text-fg hover:decoration-accent"
          >
            {title ?? ev.url}
          </a>
        ) : (
          <span className="break-words">{title ?? "no link recorded"}</span>
        )}
        {summary && (
          <span className="mt-0.5 block break-words text-2xs leading-relaxed text-muted/85">
            {summary}
          </span>
        )}
      </span>
      {region && (
        <span className="shrink-0 rounded border border-border bg-bg-muted px-1.5 py-0.5 font-mono text-3xs text-muted">
          {region}
        </span>
      )}
      {metric && (
        <span className="shrink-0 font-mono text-2xs tabular-nums text-fg">
          {metric}
        </span>
      )}
      {/* The two stamps take a line of their own (`basis-full` forces the
          wrap), so the first line of every sighting is the part a reader
          scans: who carried it, what it said, how big it was. `published` is
          when the CONTENT went out and `seen` is when WE read it, which answer
          different questions, so both are named in full rather than abbreviated
          into a puzzle. */}
      <span className="basis-full font-mono text-3xs text-muted">
        {published && <>published {fmtStamp(published)} · </>}
        seen {fmtStamp(ev.seen_at)}
      </span>
    </li>
  );
}

function ResearchBlock({ research }: { research: GeneralTrendResearch }) {
  const sources = (research.sources ?? []).filter((s) => s.uri || s.title);
  return (
    <Block
      title="What this actually is"
      provenance="ai"
      Icon={Globe}
      note="A model read the pages listed below and summarised them. The pages are real; the summary is the model's."
    >
      <div className="space-y-3">
        <Para label="In plain words" text={research.what_it_is} />
        {nonEmpty(research.why_people_post) && (
          <Para label="Why people post it" text={research.why_people_post} />
        )}
        {sources.length > 0 && (
          <div>
            <p className="eyebrow mb-1">Pages it read</p>
            <ul className="space-y-1">
              {sources.map((s, i) => (
                <li key={`${s.uri || s.title}-${i}`}>
                  <a
                    href={s.uri || undefined}
                    target="_blank"
                    rel="noopener noreferrer"
                    className="break-all text-2xs text-muted underline decoration-border underline-offset-2 transition-colors hover:text-fg hover:decoration-accent"
                  >
                    {s.title || s.uri}
                  </a>
                </li>
              ))}
            </ul>
          </div>
        )}
        {nonEmpty(research.researched_at) && (
          <p className="font-mono text-3xs text-muted">
            Looked up {fmtStamp(research.researched_at)}
            {research.cached ? " · served from cache" : ""}
          </p>
        )}
      </div>
    </Block>
  );
}

// ---------------------------------------------------------------------------
// What the AI says: identical for both kinds, because the scorer writes the
// same fields either way.
// ---------------------------------------------------------------------------

/**
 * The model's read, last in the reading order.
 *
 * It takes no `topicSummary`: the plain-words line is the one AI field a reader
 * needs before anything else, so `Summary` at the top of the panel owns it, and
 * printing it twice would only make the panel longer.
 *
 * "How 42 could use this" leads the prose. It is the only line here anyone
 * actually acts on; the scores and the reasoning are how you decide whether to
 * trust it.
 */
function AiReadBlock({
  loading,
  topic,
  theme,
  fitScore,
  fitReason,
  uae,
  angle,
  format,
  effort,
  tags,
  opportunity,
  opportunityNote,
  scoredAt,
  extra,
}: {
  loading: boolean;
  topic: string | null;
  theme: string | null;
  fitScore: number | null;
  fitReason: string | null;
  uae: number | null;
  angle: string | null;
  format: string | null;
  effort: string | null;
  tags: string[];
  opportunity: number | null;
  opportunityNote: string;
  scoredAt: string | null;
  extra: ReactNode;
}) {
  const scored = Boolean(scoredAt);

  return (
    <Block
      title="What the AI says"
      provenance="ai"
      Icon={Bot}
      note="Every line below was written by a language model from the text above. None of it is a measurement, and none of it should be quoted as a finding."
    >
      {!scored ? (
        loading ? (
          <div className="space-y-2">
            <Skeleton h="h-3" className="w-full" />
            <Skeleton h="h-3" className="w-4/5" />
          </div>
        ) : (
          <p className="text-xs leading-relaxed text-muted">
            Not scored yet. The next scoring run fills this in. Nothing is being
            held back: the model simply has not run on this row.
          </p>
        )
      ) : (
        <div className="space-y-4">
          {nonEmpty(angle) && (
            <div className="rounded-lg border border-accent/20 bg-accent-fade p-3">
              <Para label="How 42 could use this" text={angle ?? ""} />
            </div>
          )}

          <div className="space-y-3">
            {nonEmpty(topic) && (
              <Para label="The idea worth borrowing" text={topic ?? ""} />
            )}
            {nonEmpty(fitReason) && (
              <Para label="Why it scored what it did" text={fitReason ?? ""} />
            )}
          </div>

          <div className="border-t border-border pt-3">
            <Group title="Its numbers and labels">
              <Row
                label="Fit for 42"
                value={fmtScore(fitScore)}
                note="0-100. The model's own number, not a measurement."
              />
              <Row
                label="UAE relevance"
                value={fmtScore(uae)}
                note="0-100. The model's own number, not a measurement."
              />
              <Row
                label="Opportunity"
                value={fmtScore(opportunity)}
                note={opportunityNote}
              />
              <Row label="Theme" value={nonEmpty(theme) ?? "not set"} mono={false} />
              <Row
                label="Effort to make"
                value={nonEmpty(effort) ?? "not set"}
                mono={false}
              />
              <Row
                label="Format it suggests"
                value={nonEmpty(format) ?? "not set"}
                mono={false}
              />
              {extra}
              <Row label="Scored" value={fmtStamp(scoredAt)} />
            </Group>
          </div>

          {tags.length > 0 && (
            <div className="border-t border-border pt-3">
              <p className="eyebrow mb-1">Tags</p>
              <div className="flex flex-wrap gap-1.5">
                {tags.map((t) => (
                  <span
                    key={t}
                    className="rounded border border-border bg-bg-muted px-1.5 py-0.5 font-mono text-3xs text-muted"
                  >
                    {t}
                  </span>
                ))}
              </div>
            </div>
          )}
        </div>
      )}
    </Block>
  );
}

// ---------------------------------------------------------------------------
// Headlines
// ---------------------------------------------------------------------------

function generalHeadline(trend: GeneralTrend): string {
  return (
    nonEmpty(trend.title?.replace(/\s+/g, " ")) ??
    nonEmpty(trend.topic) ??
    "Untitled trend"
  );
}

/** The sources it was seen on. Heat used to lead this line and now leads the
 *  panel, in the pill, so repeating the word here said nothing twice. */
function generalSubline(trend: GeneralTrend): string {
  return trend.sources.length
    ? trend.sources.map((s) => sourceLabel(s)).join(" · ")
    : "no sources recorded";
}

/**
 * News inverts the order, the same way the card and the Make-this header do:
 * the real headline IS the story, while `topic` is 42's idea ABOUT the story,
 * and putting our interpretation in the title slot presents it as the news.
 */
function postHeadline(post: TrendPost): string {
  const caption = nonEmpty(post.caption?.split("\n")[0]);
  const best =
    post.source === "news"
      ? (caption ?? nonEmpty(post.topic))
      : (nonEmpty(post.topic) ?? caption);
  return (
    best ??
    (post.username ? `@${post.username.replace(/^@/, "")}` : "Untitled post")
  );
}

/**
 * Who, and how big or how recent. A news row has no account behind it:
 * `username` there holds the OUTLET's name, so rendering it as "@handle" the
 * way the competitor feed does would invent an Instagram account.
 */
function postSubline(post: TrendPost): string {
  if (post.source === "news") {
    const outlet =
      nonEmpty(post.platform) ?? nonEmpty(post.username) ?? "unnamed outlet";
    return `${outlet} · ${fmtStamp(post.posted_at)}`;
  }
  const handle = post.username
    ? `@${post.username.replace(/^@/, "")}`
    : "no account recorded";
  return post.competitor_followers != null
    ? `${handle} · ${fmtCompact(post.competitor_followers)} followers`
    : handle;
}
