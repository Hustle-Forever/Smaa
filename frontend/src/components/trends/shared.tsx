/**
 * Trends-specific atoms, plus a re-export shim for the generic ones.
 *
 * The generic half of this file (formatters, Field, EmptyState, ErrorCard,
 * Avatar, the input class constants) now lives in components/ui/primitives.tsx
 * so the rest of the app can reach it. What stays here is the half that is
 * genuinely about trends and would not mean anything elsewhere:
 * outperformance banding, freshness, region labels, the 0-100 score badge.
 *
 * The re-export at the bottom is deliberate and must not be "tidied" into
 * forcing callers to import from ui/ directly. Nine files import from here,
 * three of them behind the VIRAL_SOCIAL_TRENDS_ENABLED flag, and
 * featureFlags.ts is explicit that the guarded code must stay byte-stable so
 * flipping one constant is the whole revert. A shim costs one line and keeps
 * that promise; a codemod would break it.
 *
 * Every colour band is a COMPLETE class string in a map -- Tailwind's content
 * scanner only sees literals, so `"bg-" + x` would compile to nothing.
 */
import type { ReactNode } from "react";
import { Flame } from "lucide-react";

import type { TrendRegion } from "./types";
import { fmtCompact, fmtMultiple, fmtScore } from "../ui/primitives";

/** Not exported — `regionLabel` is the only way anything should read this, so
 *  an unknown value always falls back rather than crashing on undefined. */
const REGION_LABELS: Record<TrendRegion, string> = {
  uae: "UAE",
  gcc: "GCC",
  international: "International",
};

export function regionLabel(r: TrendRegion | null | undefined): string {
  return r ? REGION_LABELS[r] ?? r : "—";
}

// ---------------------------------------------------------------------------
// Outperformance — the hero stat
// ---------------------------------------------------------------------------

type Band = "strong" | "medium" | "muted";

export function outperfBand(v: number | null | undefined): Band {
  if (v == null || !Number.isFinite(v)) return "muted";
  if (v >= 3) return "strong";
  if (v >= 1.5) return "medium";
  return "muted";
}

const OUTPERF_PILL: Record<Band, string> = {
  strong: "bg-accent text-white border border-accent shadow-lift",
  medium: "bg-white text-accent border border-accent/40 shadow-soft",
  muted: "bg-white/95 text-muted border border-border shadow-soft",
};

const OUTPERF_SUB: Record<Band, string> = {
  strong: "text-white/80",
  medium: "text-accent/70",
  muted: "text-muted",
};

/**
 * "4.7x / their average". Raw likes are meaningless across accounts of wildly
 * different sizes, so this — not the like count — is what the eye should hit
 * first on a card.
 */
export function OutperformancePill({
  value,
  className = "",
}: {
  value: number | null | undefined;
  className?: string;
}) {
  const band = outperfBand(value);
  const known = value != null && Number.isFinite(value);
  return (
    <span
      className={`inline-flex items-baseline gap-1.5 rounded px-2.5 py-1.5 ${OUTPERF_PILL[band]} ${className}`}
      title={
        known
          ? `${fmtMultiple(value)} this account's own typical engagement`
          : "Not enough history to compare this post to the account's average"
      }
    >
      <span className="font-mono text-lg font-bold leading-none tabular-nums">
        {known ? fmtMultiple(value) : "—"}
      </span>
      <span className={`text-3xs font-medium leading-none ${OUTPERF_SUB[band]}`}>
        {known ? "their average" : "no baseline"}
      </span>
    </span>
  );
}

// ---------------------------------------------------------------------------
// Freshness — the hero stat for a news story
// ---------------------------------------------------------------------------

/** "3h", "2d", "3w". Compact enough for the pill's big numeral. */
function fmtAge(iso: string | null | undefined): string | null {
  if (!iso) return null;
  const then = new Date(iso).getTime();
  if (Number.isNaN(then)) return null;
  const mins = Math.max(0, Math.round((Date.now() - then) / 60000));
  if (mins < 60) return `${mins}m`;
  const hours = Math.round(mins / 60);
  if (hours < 24) return `${hours}h`;
  const days = Math.round(hours / 24);
  if (days < 14) return `${days}d`;
  return `${Math.round(days / 7)}w`;
}

function freshBand(iso: string | null | undefined): Band {
  if (!iso) return "muted";
  const then = new Date(iso).getTime();
  if (Number.isNaN(then)) return "muted";
  const hours = (Date.now() - then) / 3_600_000;
  if (hours <= 12) return "strong";
  if (hours <= 48) return "medium";
  return "muted";
}

/**
 * A news story's age, in the slot where a competitor post shows outperformance.
 *
 * Recency is the honest hero metric here: a story has no engagement of its own
 * to beat, and "how widely was it covered" belongs in the metrics row where the
 * reader can weigh it. Same geometry and the same three colour bands as
 * `OutperformancePill`, so a mixed "All" grid keeps one visual rhythm.
 */
export function FreshnessPill({
  postedAt,
  approximate = false,
  className = "",
}: {
  postedAt: string | null | undefined;
  /** the date is an EDIT time, not a publish time (sitemap-derived sources) */
  approximate?: boolean;
  className?: string;
}) {
  const band = freshBand(postedAt);
  const age = fmtAge(postedAt);
  return (
    <span
      className={`inline-flex items-baseline gap-1.5 rounded px-2.5 py-1.5 ${OUTPERF_PILL[band]} ${className}`}
      title={
        age
          ? approximate
            ? `Last updated about ${age} ago — this source reports edit time, not publication time`
            : `Published ${age} ago`
          : "No publication date on this story"
      }
    >
      <span className="font-mono text-lg font-bold leading-none tabular-nums">
        {age ?? "—"}
      </span>
      <span className={`text-3xs font-medium leading-none ${OUTPERF_SUB[band]}`}>
        {age ? (approximate ? "ago approx." : "ago") : "undated"}
      </span>
    </span>
  );
}

// ---------------------------------------------------------------------------
// Volume — the hero stat for a tracked account
// ---------------------------------------------------------------------------

/**
 * Below this many posts the scanner does not trust an account's own baseline
 * (MIN_POSTS_FOR_BASELINE in trend_finder.py), so the pill should not call the
 * account strong on the strength of them either.
 */
const VOLUME_STRONG_FLOOR = 6;

/**
 * Banded against the list the count sits in, not against a fixed number.
 *
 * A fixed threshold saturates: "10 or more is strong" paints a solid teal
 * block on nearly every account that has ever been scanned, and a column of
 * identical strong pills ranks nothing. Strong here means at least TWICE the
 * typical account on the same list (its median), which on any real spread is
 * a minority and stays a minority as the list matures. Zero is muted because
 * it is the one count with no story in it; everything else is medium, so the
 * eye lands on the outliers and nowhere else.
 *
 * A `benchmark` of 0 (nothing scanned yet, or a list of one) cannot produce a
 * strong pill: with nothing to compare against there is nothing to claim.
 */
function volumeBand(value: number | null | undefined, benchmark: number): Band {
  if (value == null || !Number.isFinite(value) || value <= 0) return "muted";
  if (benchmark > 0 && value >= Math.max(VOLUME_STRONG_FLOOR, benchmark * 2)) {
    return "strong";
  }
  return "medium";
}

/**
 * A count in the slot where a trend card shows outperformance: same geometry,
 * same three bands, so a competitor row keeps the visual rhythm of the feed.
 * Takes the raw count and bands it itself; callers never name a band.
 */
export function MetricPill({
  value,
  benchmark,
  caption = "posts found",
  emptyCaption = "not scanned",
  subdued = false,
  title,
  className = "",
}: {
  /** null when the number does not exist yet. That draws "—" rather than a 0,
   *  which would read as "looked and found nothing". */
  value: number | null | undefined;
  /** The list's typical value (its median): what "strong" is measured against. */
  benchmark: number;
  caption?: string;
  emptyCaption?: string;
  /** Always muted, for rows the reader has parked. A teal block on an excluded
   *  account would pull the eye to the wrong list. */
  subdued?: boolean;
  title?: string;
  className?: string;
}) {
  const known = value != null && Number.isFinite(value);
  const band: Band = subdued ? "muted" : volumeBand(value, benchmark);
  return (
    <span
      title={title}
      className={`inline-flex items-baseline gap-1.5 rounded px-2.5 py-1.5 ${OUTPERF_PILL[band]} ${className}`}
    >
      <span className="font-mono text-lg font-bold leading-none tabular-nums">
        {known ? fmtCompact(value) : "—"}
      </span>
      <span className={`text-3xs font-medium leading-none ${OUTPERF_SUB[band]}`}>
        {known ? caption : emptyCaption}
      </span>
    </span>
  );
}

// ---------------------------------------------------------------------------
// Fit / UAE relevance
// ---------------------------------------------------------------------------

const FIT_BADGE: Record<"high" | "mid" | "low" | "none", string> = {
  high: "bg-success/10 text-success border-success/25",
  mid: "bg-warning/10 text-warning border-warning/25",
  low: "bg-bg-muted text-muted border-border",
  none: "bg-bg-muted text-muted border-border",
};

function fitBand(v: number | null | undefined): "high" | "mid" | "low" | "none" {
  if (v == null || !Number.isFinite(v)) return "none";
  if (v >= 70) return "high";
  if (v >= 45) return "mid";
  return "low";
}

/** A 0-100 score chip. `label` names what is being scored. */
export function ScoreBadge({
  label,
  value,
  title,
}: {
  label: string;
  value: number | null | undefined;
  title?: string;
}) {
  const band = fitBand(value);
  return (
    <span
      title={title}
      className={`inline-flex items-center gap-1 rounded border px-1.5 py-0.5 text-3xs font-medium ${FIT_BADGE[band]}`}
    >
      <span className="uppercase tracking-wider2 font-mono text-[9px] opacity-80">
        {label}
      </span>
      <span className="font-mono tabular-nums">{fmtScore(value)}</span>
    </span>
  );
}

// ---------------------------------------------------------------------------
// Re-export shim -- see the note at the top of this file before changing it.
// `export type` is required: tsconfig sets isolatedModules, under which a
// re-exported type without it is a build error.
// ---------------------------------------------------------------------------
export {
  fmtCompact,
  fmtMultiple,
  fmtScore,
  SELECT_CLS,
  INPUT_CLS,
  Field,
  EmptyState,
  ErrorCard,
  Avatar,
} from "../ui/primitives";

// ---------------------------------------------------------------------------
// Which kind of picture a card has
// ---------------------------------------------------------------------------

export type CoverKind = "logo" | "house" | "photo" | "none";

/**
 * What kind of picture a post's cached cover is, read off its path.
 *
 * The directory an asset is filed under is the one durable record of where it
 * came from (`news_thumbs`, `news_logos` on the backend): a brand mark lives
 * under `news_brand_logos`, a drawn house card under `news_cover_art`, and
 * anything else is the publisher's own picture. A mark is drawn differently
 * from a photograph, small and centred rather than filling the tile, so the
 * card has to know which one it holds.
 */
export function coverKindOf(post: {
  local_thumb_path?: string | null;
  thumbnail_url?: string | null;
}): CoverKind {
  const path = post.local_thumb_path || "";
  if (path.includes("news_brand_logos")) return "logo";
  if (path.includes("news_cover_art")) return "house";
  if (path || post.thumbnail_url) return "photo";
  return "none";
}

// ---------------------------------------------------------------------------
// Viral potential
// ---------------------------------------------------------------------------

export interface ViralVerdict {
  /** 0-100: how likely a post about this would get views and shares. */
  score: number;
  /** One plain sentence from the judge on why, or "" when it gave none. */
  reason: string;
}

/**
 * The judge's viral potential for a news story, off its metadata.
 *
 * Null when the story was judged before the score existed. The housekeeping
 * tick scores those a batch at a time, so null is a state that clears itself;
 * until it does the card says "not scored yet" rather than inventing a zero.
 */
export function viralOf(post: {
  metadata?: Record<string, unknown> | null;
}): ViralVerdict | null {
  const meta = post.metadata ?? {};
  const raw = meta.viral_potential;
  const n = typeof raw === "number" ? raw : typeof raw === "string" ? Number(raw) : NaN;
  if (!Number.isFinite(n)) return null;
  return {
    score: Math.max(0, Math.min(100, Math.round(n))),
    reason: typeof meta.viral_reason === "string" ? meta.viral_reason : "",
  };
}

// ---------------------------------------------------------------------------
// UAE score
// ---------------------------------------------------------------------------

// Places, people and days that put a story inside the UAE (English + Arabic).
const UAE_RE =
  /(u\.?a\.?e|emirat(?:es|i|is)|abu\s?dhabi|dubai|sharjah|ajman|fujairah|ras\s?al[\s-]?khaimah|umm\s?al[\s-]?quwain|al\s?ain|mbzuai|khalifa\s+university|zayed)|الإمارات|الامارات|إماراتي|اماراتي|أبوظبي|ابوظبي|أبو ظبي|دبي|الشارقة|عجمان|الفجيرة|رأس الخيمة|العين/i;

/**
 * How much this lands with a UAE audience, 0-100.
 *
 * Happening inside the UAE (a UAE place, Emirati people or day named in it, or
 * posted by a UAE account) is 100. A story from outside it keeps the scorer's
 * own number; a news story the scorer never rated counts as international, 90.
 */
export function uaeScoreOf(post: {
  source?: string | null;
  topic?: string | null;
  caption?: string | null;
  tags?: unknown;
  region?: string | null;
  competitor_region?: string | null;
  uae_relevance?: number | null;
  metadata?: Record<string, unknown> | null;
}): { score: number | null; reason: string } {
  const meta = post.metadata ?? {};
  const text = [
    post.topic,
    post.caption,
    typeof meta.original_headline === "string" ? meta.original_headline : "",
    typeof meta.why_it_matters === "string" ? meta.why_it_matters : "",
    typeof meta.company === "string" ? meta.company : "",
    Array.isArray(post.tags) ? post.tags.join(" ") : "",
  ].join(" ");
  const uaeRegion = post.region === "uae" || post.competitor_region === "uae";
  if (UAE_RE.test(text) || uaeRegion) {
    return { score: 100, reason: "Happening inside the UAE." };
  }
  if (post.uae_relevance != null && Number.isFinite(post.uae_relevance)) {
    return { score: Math.round(post.uae_relevance), reason: "Outside the UAE. The AI's number for how well it lands here." };
  }
  if (post.source === "news") return { score: 90, reason: "International story, still relevant to a UAE audience." };
  return { score: null, reason: "Not scored yet." };
}

/**
 * Viral potential for a COMPETITOR post, from what performed in the past.
 *
 * `peers` is every competitor post we hold a measured result for (likes and
 * comments against that account's usual post). The score is where this post
 * lands among them: 90 means it did better than 90% of the competitor posts we
 * have tracked. Measured, not guessed, and it sharpens by itself as every scan
 * adds more results to compare against. Null below 5 results.
 */
export function pastViralOf(
  outperf: number | null | undefined,
  peers: readonly number[],
): ViralVerdict | null {
  if (outperf == null || !Number.isFinite(outperf) || peers.length < 5) return null;
  let below = 0;
  let same = 0;
  for (const v of peers) {
    if (v < outperf) below += 1;
    else if (v === outperf) same += 1;
  }
  const score = Math.max(0, Math.min(100, Math.round((100 * (below + same / 2)) / peers.length)));
  return {
    score,
    reason: `Did better than ${score}% of the ${peers.length} competitor posts we have tracked, measured against each account's usual post.`,
  };
}

/**
 * The plain word for a score, and the colour band that goes with it. Every
 * class string is a complete literal, because Tailwind's scanner only sees
 * literals. The word is what carries the meaning; the colour restates it.
 */
export function viralBand(score: number): { label: string; cls: string } {
  if (score >= 80) {
    return { label: "Very likely to travel", cls: "border-rose-300 bg-rose-50 text-rose-700" };
  }
  if (score >= 60) {
    return { label: "Good chance", cls: "border-amber-300 bg-amber-50 text-amber-700" };
  }
  if (score >= 40) {
    return { label: "Some chance", cls: "border-border bg-bg-muted text-fg" };
  }
  return { label: "Unlikely to travel", cls: "border-border bg-bg-muted text-muted" };
}

/**
 * The one score a news card shows: could this go viral?
 *
 * ONE PILL, IN WORDS. The card used to carry a row of the scorer's numbers
 * ("fit 100", "uae 100") and the owner had them removed as the machine's own
 * working. This is different: it is the answer to the one question the owner
 * asked the feed to answer ("what has the potential to go viral"), it is the
 * order the news channel is in, and it is said in words with the number
 * beside them rather than as a bare number. The judge's reason is the
 * tooltip and is spelled out in Details.
 */
export function ViralPill({ verdict }: { verdict: ViralVerdict }) {
  const band = viralBand(verdict.score);
  return (
    <span
      title={verdict.reason || undefined}
      className={`inline-flex max-w-full items-center gap-1.5 rounded-md border px-2 py-1 text-xs ${band.cls}`}
    >
      <Flame className="h-3.5 w-3.5 shrink-0" strokeWidth={2} aria-hidden />
      <span className="font-medium">Viral potential</span>
      <span className="font-mono text-sm font-semibold tabular-nums">{verdict.score}</span>
      <span className="truncate text-2xs opacity-80">· {band.label}</span>
    </span>
  );
}
