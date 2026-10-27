/**
 * One general-trend card — the same skeleton as the competitor feed's PostCard
 * (`TrendsFeed.tsx`), with the hero stat swapped: HEAT (how fast and how widely
 * the trend is spreading across sources) instead of outperformance, and an
 * evidence row linking out to every place we saw it.
 *
 * Every colour band is a COMPLETE class string in a map — Tailwind's content
 * scanner only sees literals, so `"bg-" + x` would compile to nothing.
 *
 * The card OWNS its detail dialog rather than taking an `onDetails` prop: the
 * only thing that renders it is `GeneralTrendsTab`, and keeping the state here
 * means opening a detail view needs no change on that side at all.
 */
import { useState } from "react";
import {
  Bookmark,
  BookmarkCheck,
  ExternalLink,
  Flame,
  Info,
  Layers,
  Sparkles,
  Undo2,
  X,
} from "lucide-react";

import { Skeleton } from "../Skeleton";
import { Thumb } from "../Thumb";
import { fmtRel } from "../../lib/format";
import type {
  GeneralTrend,
  GeneralTrendEvidence,
  GeneralTrendHeat,
  GeneralTrendSensitivity,
  TrendPostStatus,
} from "../../lib/types";
import { CoverTile } from "./CoverTile";
import { generalTrendsApi } from "./generalTrendsApi";
import { fmtCompact } from "./shared";
import { TrendDetailDialog } from "./TrendDetailDialog";

// ---------------------------------------------------------------------------
// Labels
// ---------------------------------------------------------------------------

/** Short human names for the source ids the backend records. */
export const SOURCE_LABELS: Record<string, string> = {
  google_trends: "Google",
  bluesky: "Bluesky",
  reddit: "Reddit",
  kym: "KYM",
  wikipedia: "Wikipedia",
  youtube: "YouTube",
  x_trends: "X",
  // Added with the keyless + gated sources. Deliberately short: these render
  // inside evidence chips next to a region and a metric, on a 3-up grid.
  hacker_news: "HN",
  lemmy: "Lemmy",
  github: "GitHub",
  google_news: "News",
  tiktok: "TikTok",
  instagram: "Instagram",
  instagram_watch: "IG posts",
  mastodon: "Mastodon",
  huggingface: "HF",
  // The format registries. These are the only sources allowed to CREATE a
  // card, so they are the labels the reader sees most.
  kym_entries: "KYM",
  kym_editorials: "KYM read",
  youtube_shorts: "Shorts audio",
  imgflip: "Imgflip",
};

export function sourceLabel(id: string | null | undefined): string {
  if (!id) return "—";
  return SOURCE_LABELS[id] ?? id;
}

/** Region codes → labels. "AE" is the one people read as UAE, not as a code. */
export function generalRegionLabel(r: string | null | undefined): string {
  if (!r) return "—";
  if (r === "US") return "US";
  if (r === "AE") return "UAE";
  if (r === "global") return "Global";
  return r;
}

// ---------------------------------------------------------------------------
// Heat — the hero stat (same visual grammar as OutperformancePill)
// ---------------------------------------------------------------------------

const HEAT_PILL: Record<GeneralTrendHeat, string> = {
  hot: "bg-accent text-white border border-accent shadow-lift",
  rising: "bg-white text-accent border border-accent/40 shadow-soft",
  warm: "bg-white/95 text-muted border border-border shadow-soft",
};

const HEAT_SUB: Record<GeneralTrendHeat, string> = {
  hot: "text-white/80",
  rising: "text-accent/70",
  warm: "text-muted",
};

const HEAT_WORD: Record<GeneralTrendHeat, string> = {
  hot: "HOT",
  rising: "RISING",
  warm: "WARM",
};

/**
 * "HOT / 4 sources". Heat is what the eye should hit first on a card: a trend
 * seen on four platforms in the last day matters more than one Reddit thread,
 * whatever the raw numbers say.
 */
export function HeatPill({
  heat,
  velocity,
  sourceCount,
  className = "",
}: {
  heat: GeneralTrendHeat;
  velocity: number | null | undefined;
  sourceCount: number;
  className?: string;
}) {
  const known = velocity != null && Number.isFinite(velocity);
  return (
    <span
      className={`inline-flex items-baseline gap-1.5 rounded px-2.5 py-1.5 ${HEAT_PILL[heat]} ${className}`}
      title={
        known
          ? `Heat ${Math.round(velocity * 100)} / 100 — how fast and how widely this is spreading`
          : "Heat not computed yet"
      }
    >
      <span className="font-mono text-lg font-bold leading-none tabular-nums">
        {HEAT_WORD[heat]}
      </span>
      <span className={`text-[10px] font-medium leading-none ${HEAT_SUB[heat]}`}>
        {sourceCount} source{sourceCount === 1 ? "" : "s"}
      </span>
    </span>
  );
}

// ---------------------------------------------------------------------------
// Sensitivity chip
// ---------------------------------------------------------------------------

const SENSITIVITY_CHIP: Record<Exclude<GeneralTrendSensitivity, "ok">, string> = {
  caution: "text-warning border-warning/25 bg-warning/10",
  avoid: "text-danger border-danger/25 bg-danger/10",
};

const SENSITIVITY_TITLE: Record<Exclude<GeneralTrendSensitivity, "ok">, string> = {
  caution: "Grief, health or controversy in the story — handle with care",
  avoid: "Deaths, tragedy, politics, religion, sexual or drug content — not for a school's feed",
};

/**
 * The chip said the slug the model returned: "caution", "avoid". Those are the
 * scorer's own two words for its own scale, and on a card they read as a
 * grading rather than as advice. These say what to DO about it, which is the
 * only reason the chip is on the card at all.
 */
const SENSITIVITY_WORD: Record<Exclude<GeneralTrendSensitivity, "ok">, string> = {
  caution: "handle with care",
  avoid: "leave this one",
};

function SensitivityChip({ value }: { value: GeneralTrendSensitivity | null }) {
  if (value !== "caution" && value !== "avoid") return null;
  return (
    <span
      title={SENSITIVITY_TITLE[value]}
      className={`rounded border px-1.5 py-0.5 text-[10px] font-medium ${SENSITIVITY_CHIP[value]}`}
    >
      {SENSITIVITY_WORD[value]}
    </span>
  );
}

// ---------------------------------------------------------------------------
// Safety — the INGEST screen's verdict. A different gate from `sensitivity`,
// which is the AI's: this one runs on raw text before a token is spent, so it
// is the only reason a card can be hidden having never been scored at all.
// ---------------------------------------------------------------------------

/**
 * Plain words for the reason slugs `general_trends_safety.screen` returns. The
 * slug ("bait", "avoid_topic") means nothing to the person reading the card,
 * and a flagged card is exactly the case where they are judging OUR filter
 * rather than the trend — so it has to say what the filter thought it saw.
 */
const SAFETY_REASON_WORDS: Record<string, string> = {
  explicit: "adult content",
  harm: "harmful content",
  bait: "dating bait",
  avoid_topic: "sensitive topic",
};

export function safetyReasonWords(reason: string | null | undefined): string {
  if (!reason) return "content filter";
  return SAFETY_REASON_WORDS[reason] ?? reason.replace(/_/g, " ");
}

function SafetyChip({ trend }: { trend: GeneralTrend }) {
  if (trend.safety !== "flagged") return null;
  return (
    <span
      title={`Our ingest filter flagged this before scoring (${trend.safety_reason || "no reason recorded"}). Flagged trends are never sent to the AI, and this one is only on screen because "Hidden trends" is on.`}
      className="rounded border px-1.5 py-0.5 text-[10px] font-medium text-warning border-warning/25 bg-warning/10"
    >
      Filtered · {safetyReasonWords(trend.safety_reason)}
    </span>
  );
}

// ---------------------------------------------------------------------------
// Evidence quality — so a thin card LOOKS thin
// ---------------------------------------------------------------------------

type EvidenceBand = "strong" | "some" | "thin";

const EVIDENCE_CHIP: Record<EvidenceBand, string> = {
  strong: "text-success border-success/25 bg-success/10",
  some: "text-muted border-border bg-bg-muted",
  thin: "text-warning border-warning/25 bg-warning/10",
};

const EVIDENCE_TITLE: Record<EvidenceBand, string> = {
  strong:
    "Three or more independent sources carried this — the strongest corroboration the feed can give you",
  some: "Two sources, or one source that kept producing — worth a look",
  thin: "One source, one sighting. That is a single post, not evidence that anything is spreading",
};

/**
 * `sighting_count` is a POLL counter — it rises every refresh even for one dead
 * item — so the honest number is `evidence_count` (distinct source + key). Rows
 * written before that column existed carry 0, and there the evidence rows we
 * already render are the better estimate; `sighting_count` is the last resort.
 */
function evidenceStrength(trend: GeneralTrend): {
  band: EvidenceBand;
  text: string;
} {
  const sightings =
    trend.evidence_count > 0
      ? trend.evidence_count
      : trend.evidence.length || trend.sighting_count;
  const sources = trend.source_count;
  const band: EvidenceBand =
    sources >= 3 ? "strong" : sources >= 2 || sightings >= 3 ? "some" : "thin";
  return {
    band,
    text: `${sources} source${sources === 1 ? "" : "s"}, ${sightings} sighting${
      sightings === 1 ? "" : "s"
    }`,
  };
}

function EvidenceStrengthChip({ trend }: { trend: GeneralTrend }) {
  const { band, text } = evidenceStrength(trend);
  return (
    <span
      title={EVIDENCE_TITLE[band]}
      className={`rounded border px-1.5 py-0.5 text-[10px] font-medium ${EVIDENCE_CHIP[band]}`}
    >
      {text}
    </span>
  );
}

// ---------------------------------------------------------------------------
// Fit cap
// ---------------------------------------------------------------------------

/**
 * The scorer appends "[capped at 25: old]" to `fit_reason` when Python
 * overrode the model's number (bare evidence, a single post, something that
 * peaked months ago). That suffix IS the explanation for a low score, so it is
 * lifted into its own chip — and left in the sentence below it, never stripped.
 */
const FIT_CAP_RE = /\[capped at (\d+):\s*([^\]]+)\]/i;

function FitCapChip({ reason }: { reason: string | null }) {
  const m = reason ? FIT_CAP_RE.exec(reason) : null;
  if (!m) return null;
  const why = m[2].trim();
  return (
    <span
      title={`The AI scored this higher; we held it at ${m[1]} because the evidence is "${why}".`}
      className="rounded border px-1.5 py-0.5 text-[10px] font-medium text-warning border-warning/25 bg-warning/10"
    >
      {/* "capped 25" was the scorer's word for it. "held at" says the same
          thing to someone who has never read the scoring code. */}
      fit held at {m[1]} · {why}
    </span>
  );
}

// ---------------------------------------------------------------------------
// Card
// ---------------------------------------------------------------------------

export function GeneralTrendCard({
  trend,
  onStatus,
  onMakeThis,
}: {
  trend: GeneralTrend;
  onStatus: (trend: GeneralTrend, next: TrendPostStatus) => void | Promise<void>;
  onMakeThis: (trend: GeneralTrend) => void | Promise<void>;
}) {
  const dismissed = trend.status === "dismissed";
  const saved = trend.status === "saved";
  const headline = headlineFor(trend);
  const whatItIs = (trend.topic_summary || trend.summary || "").trim();
  const primarySource = trend.sources[0] ?? null;
  // The detail dialog. A dedicated BUTTON, not a clickable card body: the card
  // already contains a "Make this", a save, a dismiss, an outbound link and one
  // link per evidence chip, and wrapping all of that in a click target makes
  // every one of them ambiguous to both a mouse and a screen reader.
  const [detailOpen, setDetailOpen] = useState(false);

  return (
    <article
      className={`card card-hover flex flex-col overflow-hidden ${
        dismissed ? "opacity-55" : ""
      }`}
    >
      {/* Thumbnail. The locally cached thumb is the primary source because the
          source image URLs go stale; the live URL is the fallback. */}
      <div className="relative aspect-[4/3] bg-bg-muted">
        <Thumb
          src={trend.local_thumb_path ? generalTrendsApi.thumbUrl(trend.id) : (trend.image_url ?? null)}
          fallbackSrc={trend.local_thumb_path ? (trend.image_url ?? null) : null}
          alt={headline}
          className="h-full w-full"
          empty={
            // A hashtag has no picture, and TikTok's only image (the top
            // creator's avatar) is signed with a ~48h expiry, so it would 403
            // on the card a day later. Having no picture is the NORMAL case
            // here, not a degraded one, so it gets the same tinted cover tile
            // an imageless news story gets in the feed: one look for "there
            // was never a picture", everywhere in the app. This used to be a
            // hand-rolled block of centred mono text, which was the same idea
            // said in a second visual language on a screen that sits in the
            // same tablist as the feed.
            //
            // Seeded on the SOURCE, never the headline, so every Google Trends
            // card arrives the same colour and a screen of them is scannable.
            // No `title` passed: the headline is the <h3> immediately below,
            // and putting it in the tile as well would print the same sentence
            // twice, adjacently.
            <CoverTile
              seed={primarySource || "trend"}
              label={primarySource ? sourceLabel(primarySource) : null}
              Icon={Flame}
            />
          }
        />

        <div className="pointer-events-none absolute inset-x-0 top-0 flex items-start justify-between gap-2 p-2.5">
          <HeatPill
            heat={trend.heat}
            velocity={trend.velocity}
            sourceCount={trend.source_count}
          />
          {trend.trend_kind && (
            <span className="rounded bg-black/60 px-1.5 py-0.5 font-mono text-[9px] uppercase tracking-wider2 text-white/90">
              {trend.trend_kind}
            </span>
          )}
        </div>

        {dismissed && (
          <span className="absolute bottom-2 left-2.5 rounded bg-black/65 px-2 py-0.5 font-mono text-[9px] uppercase tracking-wider2 text-white/90">
            dismissed
          </span>
        )}
      </div>

      <div className="flex flex-1 flex-col gap-3 p-4">
        {/* Headline — the AI's clean name once scored, the raw title before. */}
        <h3 className="text-sm font-semibold leading-snug text-fg line-clamp-2">
          {headline}
        </h3>

        {/* "fit", "uae", the theme and the effort sat here as four chips.
            Removed by request, for the same reason as on the competitor card:
            a bare 0-100 with no scale beside it is the machine's working, not
            information, and it was the first thing under the headline. Every
            score is still computed, still orders the feed, and is still shown
            with its provenance in Details. */}

        {/* The fit score's own reasoning stood here, clamped to two lines. It
            went with the chips above it: it existed to explain a number that is
            no longer on the card, and a clamped paragraph is the thing a card
            gets scanned past. Details still prints it whole. */}

        {whatItIs && (
          <div>
            <p className="eyebrow mb-1">What it is</p>
            <p className="text-xs text-muted leading-relaxed line-clamp-3">
              {whatItIs}
            </p>
          </div>
        )}

        {trend.suggested_angle && (
          <div className="rounded-lg border border-accent/20 bg-accent-fade p-2.5">
            <p className="eyebrow mb-1">How 42 could do this</p>
            <p className="text-xs leading-relaxed text-fg/85 line-clamp-3">
              {trend.suggested_angle}
            </p>
            {trend.suggested_format && (
              <p className="mt-1.5 font-mono text-[10px] text-muted">
                as a {trend.suggested_format}
              </p>
            )}
          </div>
        )}

        {/* Where we saw it — one chip per sighting, each linking out. */}
        {trend.evidence.length > 0 && (
          <div className="flex flex-wrap gap-1.5">
            {trend.evidence.map((ev, i) => (
              // The same item can be seen in two regions with one url (an X
              // trend in US and AE), so source+url alone is not unique.
              <EvidenceChip
                key={`${ev.source}-${ev.region ?? ""}-${ev.url ?? ""}-${i}`}
                ev={ev}
              />
            ))}
          </div>
        )}

        {/* Supporting metrics. */}
        <div className="flex flex-wrap items-center gap-3 font-mono text-[11px] text-muted">
          <span
            className="inline-flex items-center gap-1"
            title="Heat — how fast and how widely it's spreading"
          >
            <Flame className="h-3 w-3" aria-hidden />
            {trend.velocity != null && Number.isFinite(trend.velocity)
              ? `${Math.round(trend.velocity * 100)}%`
              : "—"}
          </span>
          <span
            className="inline-flex items-center gap-1"
            title="Times a source still listed this when we polled — a liveness count, not corroboration. The chip above is the evidence."
          >
            <Layers className="h-3 w-3" aria-hidden />
            {trend.sighting_count} sighting{trend.sighting_count === 1 ? "" : "s"}
          </span>
          {/* Content age, not fetch age. `last_seen_at` only says a source
              still lists it; this says something NEW happened — which is the
              whole question behind "does the feed feel current". */}
          {trend.latest_content_at ? (
            <span
              className="ml-auto"
              title={`Newest content behind this trend: ${trend.latest_content_at}`}
            >
              new {fmtRel(trend.latest_content_at)}
            </span>
          ) : (
            trend.first_seen_at && (
              <span className="ml-auto" title={trend.first_seen_at}>
                first seen {fmtRel(trend.first_seen_at)}
              </span>
            )
          )}
        </div>

        {/* Actions */}
        <div className="mt-auto flex flex-wrap items-center gap-1.5 pt-1">
          <button
            type="button"
            onClick={() => void onMakeThis(trend)}
            className="btn btn-primary flex-1 text-xs"
          >
            <Sparkles className="h-3.5 w-3.5" aria-hidden />
            Make this
          </button>
          <button
            type="button"
            onClick={() => setDetailOpen(true)}
            title="Everything we know about this trend — every sighting, the AI read, the timeline"
            className="btn btn-ghost text-xs"
          >
            <Info className="h-3.5 w-3.5" aria-hidden />
            Details
          </button>
          <button
            type="button"
            onClick={() => void onStatus(trend, saved ? "new" : "saved")}
            aria-pressed={saved}
            title={saved ? "Remove from saved" : "Save for later"}
            className={`btn text-xs ${saved ? "btn-accent" : "btn-ghost"}`}
          >
            {saved ? (
              <BookmarkCheck className="h-3.5 w-3.5" aria-hidden />
            ) : (
              <Bookmark className="h-3.5 w-3.5" aria-hidden />
            )}
            {saved ? "Saved" : "Save"}
          </button>
          <button
            type="button"
            onClick={() => void onStatus(trend, dismissed ? "new" : "dismissed")}
            title={dismissed ? "Put this back in the list" : "Not for us"}
            className="btn btn-ghost text-xs"
          >
            {dismissed ? (
              <Undo2 className="h-3.5 w-3.5" aria-hidden />
            ) : (
              <X className="h-3.5 w-3.5" aria-hidden />
            )}
            {dismissed ? "Restore" : "Dismiss"}
          </button>
          {trend.url && (
            <a
              href={trend.url}
              target="_blank"
              rel="noopener noreferrer"
              title="Open the source"
              aria-label="Open the source"
              className="btn btn-ghost text-xs"
            >
              <ExternalLink className="h-3.5 w-3.5" aria-hidden />
            </a>
          )}
        </div>
      </div>

      {detailOpen && (
        <TrendDetailDialog
          target={{ kind: "general", trend }}
          onClose={() => setDetailOpen(false)}
        />
      )}
    </article>
  );
}

function EvidenceChip({ ev }: { ev: GeneralTrendEvidence }) {
  const label = sourceLabel(ev.source);
  const region = ev.region && ev.region !== "global" ? generalRegionLabel(ev.region) : null;
  const metric =
    ev.metric_value != null && Number.isFinite(ev.metric_value)
      ? `${fmtCompact(ev.metric_value)}${ev.metric_label ? ` ${ev.metric_label}` : ""}`
      : null;
  const text = [label, region, metric].filter(Boolean).join(" · ");
  const title = [ev.title, ev.seen_at ? `seen ${fmtRel(ev.seen_at)}` : null]
    .filter(Boolean)
    .join(" — ");
  // 24px minimum height on both kinds, not just the link. Half these chips are
  // links and half are plain text depending on whether the source gave us a
  // url, so sizing only the links would leave a ragged row. The link is the
  // one that has to clear the 24x24 pointer-target minimum: a 10px chip at
  // py-0.5 is about 18px of hit area.
  const cls =
    "inline-flex min-h-[24px] items-center rounded border border-border bg-bg-muted px-1.5 text-[10px] text-muted";
  if (!ev.url) {
    return (
      <span className={cls} title={title || undefined}>
        {text}
      </span>
    );
  }
  return (
    <a
      href={ev.url}
      target="_blank"
      rel="noopener noreferrer"
      title={title || undefined}
      className={`${cls} transition-colors hover:border-accent hover:text-fg focus:outline-none focus-visible:ring-2 focus-visible:ring-accent/40`}
    >
      {text}
    </a>
  );
}

export function GeneralTrendCardSkeleton() {
  return (
    <div className="card overflow-hidden">
      <Skeleton className="aspect-[4/3] w-full" rounded="rounded-none" />
      <div className="space-y-2 p-4">
        <Skeleton h="h-4" className="w-4/5" />
        <Skeleton h="h-3" className="w-2/5" />
        <Skeleton h="h-14" className="w-full" rounded="rounded-lg" />
        <Skeleton h="h-3" className="w-3/5" />
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// helpers
// ---------------------------------------------------------------------------

/** `title` is the AI's clean name once scored, else the first sighting's title. */
function headlineFor(trend: GeneralTrend): string {
  const t = trend.title?.replace(/\s+/g, " ").trim();
  if (t) return t;
  const topic = trend.topic?.trim();
  if (topic) return topic;
  return "Untitled trend";
}
