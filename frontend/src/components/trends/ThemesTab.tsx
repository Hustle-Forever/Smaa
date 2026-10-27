/**
 * Themes — the topic-finder view.
 *
 * The feed answers "which post should we copy?"; this answers the broader
 * question "what kind of thing keeps working?". Each row is a theme the scorer
 * clustered posts into, with the two numbers that decide whether it is worth
 * chasing: how far those posts beat their own accounts, and how realistically
 * 42 can make something in that lane.
 *
 * Sorted by average fit descending by default, because a theme 42 cannot
 * execute is not an opportunity no matter how well it performed for someone else.
 *
 * This tab is behind `THEMES_TAB_ENABLED` and is not reachable today. It is
 * guarded, not dead, so it uses the same control system as the feed: `Toolbar`
 * for the row that never collapses, `ErrorCard` for a failed fetch, `EmptyState`
 * for a list with nothing in it. Flip the flag and it already matches the
 * screens either side of it in the tablist.
 */
import { useEffect, useMemo, useState } from "react";
import { ArrowRight, RefreshCw } from "lucide-react";

import { Skeleton } from "../Skeleton";
import { goToTrendsTab, showTheme } from "./bus";
import { Toolbar, ToolbarSpacer } from "./controls";
import {
  EmptyState,
  ErrorCard,
  fmtMultiple,
  fmtScore,
  outperfBand,
  SELECT_CLS,
} from "./shared";
import { humanError, trendsApi } from "./trendsApi";
import type { TrendTheme } from "./types";

type ThemeSort = "fit" | "outperformance" | "volume" | "best";

/**
 * Named after what they DO, not after the column they order by. Same wording as
 * the feed's own sort list, so the two screens do not have two vocabularies for
 * one idea.
 */
const SORTS: Array<{ value: ThemeSort; label: string }> = [
  { value: "fit", label: "Best fit for 42" },
  { value: "outperformance", label: "Beat their average by most" },
  { value: "volume", label: "Most posts" },
  { value: "best", label: "Best single post" },
];

/** Complete class strings — Tailwind never sees a concatenated name. */
const BAR_FILL: Record<"strong" | "medium" | "muted", string> = {
  strong: "bg-accent",
  medium: "bg-accent/50",
  muted: "bg-border-strong",
};

export function ThemesTab() {
  const [items, setItems] = useState<TrendTheme[] | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [sort, setSort] = useState<ThemeSort>("fit");
  const [reloadKey, setReloadKey] = useState(0);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    void (async () => {
      try {
        const res = await trendsApi.themes();
        if (cancelled) return;
        setItems(res.items ?? []);
        setError(null);
      } catch (e) {
        if (cancelled) return;
        setError(humanError(e, "Couldn't load themes"));
        setItems([]);
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [reloadKey]);

  const sorted = useMemo(() => {
    const list = [...(items ?? [])];
    const num = (v: number | null | undefined) => (v == null ? -1 : v);
    list.sort((a, b) => {
      switch (sort) {
        case "outperformance":
          return num(b.avg_outperformance) - num(a.avg_outperformance);
        case "volume":
          return b.post_count - a.post_count;
        case "best":
          return num(b.best_score) - num(a.best_score);
        case "fit":
        default:
          return num(b.avg_fit) - num(a.avg_fit);
      }
    });
    return list;
  }, [items, sort]);

  // Scale the outperformance bars against the strongest theme on screen, so the
  // comparison is between themes rather than against an arbitrary ceiling.
  const maxOutperf = useMemo(
    () =>
      Math.max(
        1.5,
        ...sorted.map((t) => (t.avg_outperformance == null ? 0 : t.avg_outperformance)),
      ),
    [sorted],
  );

  return (
    <div className="space-y-5">
      {/* One line of orientation, and only the half the tab bar cannot say:
          where these groups come from. The sentence that used to follow it
          ("Pick a lane, then open the posts behind it") described the button on
          every card below, which is the kind of paragraph a reader learns to
          skip. */}
      <p className="lead max-w-2xl text-sm">
        Every scored post, grouped by what it is about.
      </p>

      {/* The row that never collapses. There is nothing to fold away here:
          one sort and one refresh IS the whole control set, so this is a
          Toolbar with no filter disclosure rather than a filter bar with an
          empty panel behind it. */}
      <Toolbar>
        <label className="inline-flex items-center gap-1.5">
          <span className="eyebrow">Sort</span>
          <select
            value={sort}
            onChange={(e) => setSort(e.target.value as ThemeSort)}
            className={SELECT_CLS}
          >
            {SORTS.map((s) => (
              <option key={s.value} value={s.value}>
                {s.label}
              </option>
            ))}
          </select>
        </label>

        <ToolbarSpacer />

        <button
          type="button"
          onClick={() => setReloadKey((k) => k + 1)}
          disabled={loading}
          title="Regroup the posts that have been scored since the last look"
          className="btn btn-ghost text-xs"
        >
          {/* The spin is the only thing on screen that says a reload is in
              flight: the grid keeps the themes it already has, which is the
              right behaviour and also means nothing else moves. */}
          <RefreshCw
            className={`h-3.5 w-3.5 ${loading ? "animate-spin" : ""}`}
            aria-hidden
          />
          {loading ? "Refreshing…" : "Refresh"}
        </button>
      </Toolbar>

      {error && <ErrorCard message={error} />}

      {loading && items === null ? (
        <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-3">
          {Array.from({ length: 6 }).map((_, i) => (
            <div key={i} className="card space-y-3 p-5">
              <Skeleton h="h-4" className="w-1/2" />
              <Skeleton h="h-8" className="w-2/3" />
              <Skeleton h="h-3" className="w-full" />
            </div>
          ))}
        </div>
      ) : sorted.length === 0 ? (
        <EmptyState
          title="No themes yet"
          body="Themes appear once scanned posts have been scored: the AI groups them into recurring angles. Add the accounts you want watched and run a scan first."
          action={
            <button
              type="button"
              className="btn btn-primary text-sm"
              onClick={() => goToTrendsTab("accounts")}
            >
              Add accounts and scan
            </button>
          }
        />
      ) : (
        <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-3">
          {sorted.map((t) => (
            <ThemeCard key={t.theme} theme={t} maxOutperf={maxOutperf} />
          ))}
        </div>
      )}
    </div>
  );
}

function ThemeCard({
  theme,
  maxOutperf,
}: {
  theme: TrendTheme;
  maxOutperf: number;
}) {
  const band = outperfBand(theme.avg_outperformance);
  const pct = Math.max(
    4,
    Math.min(100, ((theme.avg_outperformance ?? 0) / maxOutperf) * 100),
  );

  return (
    <article className="card card-hover flex flex-col gap-4 p-5">
      <div className="flex items-start justify-between gap-3">
        <h3 className="text-sm font-semibold leading-snug text-fg">
          {theme.theme || "Uncategorised"}
        </h3>
        <span className="shrink-0 rounded border border-border bg-bg-muted px-1.5 py-0.5 font-mono text-[10px] text-muted">
          {theme.post_count} post{theme.post_count === 1 ? "" : "s"}
        </span>
      </div>

      <div>
        <div className="flex items-baseline gap-2">
          <span className="font-mono text-2xl font-bold tabular-nums text-fg">
            {fmtMultiple(theme.avg_outperformance)}
          </span>
          {/* "avg vs. their own average" said average twice and left the reader
              to work out which average. The bar underneath compares this theme
              to the strongest one on screen, and the multiple is written out
              here in words, so the bar's colour never carries meaning alone. */}
          <span className="text-[11px] text-muted">
            their accounts' usual engagement
          </span>
        </div>
        <div
          aria-hidden
          className="mt-2 h-1.5 overflow-hidden rounded-full bg-bg-muted"
        >
          <div
            className={`h-full rounded-full ${BAR_FILL[band]}`}
            style={{ width: `${pct}%` }}
          />
        </div>
      </div>

      <dl className="grid grid-cols-3 gap-2 border-t border-border pt-3">
        <Stat
          label="Fit for 42"
          value={fmtScore(theme.avg_fit)}
          title="Average of how practical the posts in this theme are for 42 to make, out of 100"
        />
        <Stat
          label="Lands in UAE"
          value={fmtScore(theme.avg_uae)}
          title="Average of how well the posts in this theme land with a UAE audience, out of 100"
        />
        <Stat
          label="Best post"
          value={fmtScore(theme.best_score)}
          title="The highest-scoring single post in this theme, out of 100"
        />
      </dl>

      <button
        type="button"
        onClick={() => showTheme(theme.theme)}
        title="Open the feed filtered to this theme"
        className="btn btn-ghost mt-auto w-full text-xs"
      >
        See posts
        <ArrowRight className="h-3.5 w-3.5" aria-hidden />
      </button>
    </article>
  );
}

function Stat({
  label,
  value,
  title,
}: {
  label: string;
  value: string;
  title?: string;
}) {
  return (
    <div title={title}>
      <dt className="eyebrow">{label}</dt>
      <dd className="mt-0.5 font-mono text-sm font-semibold tabular-nums text-fg">
        {value}
      </dd>
    </div>
  );
}
