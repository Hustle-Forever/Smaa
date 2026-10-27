/**
 * Viral Social Trends — what the whole internet is doing right now.
 *
 * Not "what worked for a competitor on Instagram" (that is TrendsFeed) but the
 * memes, formats, sounds and moments rising across Google Trends, Bluesky,
 * Reddit, Hacker News, Lemmy, GitHub, Google News, Know Your Meme, Wikipedia,
 * YouTube, TikTok and Instagram — clustered into ONE card
 * per trend, and ranked by how well 42 Abu Dhabi could ride each one. HEAT
 * (cross-source spread + recency) is the hero stat; fit / UAE relevance come
 * from the AI pass and blend into the Opportunity ranking.
 *
 * Re-syncs from the server on mount — `PageTransition` remounts tab bodies, so
 * there is no page state to preserve. Mirrors `TrendsFeed` line for line where
 * the two screens do the same job.
 *
 * The controls are the feed's, deliberately: `Toolbar` for the row that never
 * collapses, `FilterPanel` for everything one click behind it, one chip per
 * setting that is changing what the grid holds, `Notice` for status. The two
 * screens sit in the same tablist, so a reader switching between them meets
 * one control system rather than two.
 *
 * `embedded` is the ONE difference between the two places this renders. As its
 * own page (#trends/general) it owns the page-level status row: when the last
 * refresh ran, how many sources answered, and Refresh now, with Re-score all
 * in the toolbar's overflow menu. Inside the Feed tab's "General" category
 * that row would be a second page header under the first, so `embedded` drops
 * it and the toolbar becomes the top of the panel. Nothing else changes: same
 * fetching, same filters, same cards, which is why the Feed embeds this
 * component rather than rebuilding the grid.
 *
 * This whole tab is behind `VIRAL_SOCIAL_TRENDS_ENABLED` and is not reachable
 * today. Guarded, not dead: flipping that constant brings it back, and it
 * comes back matching the screens either side of it.
 */
import { useCallback, useEffect, useId, useMemo, useRef, useState } from "react";
import {
  Newspaper,
  RefreshCw,
  RotateCcw,
  ShieldAlert,
  Sparkles,
} from "lucide-react";

import { useConfirm } from "../ConfirmProvider";
import { useToast } from "../ToastProvider";
import { fmtRel } from "../../lib/format";
import type {
  GeneralTrend,
  GeneralTrendRun,
  GeneralTrendSort,
  GeneralTrendSource,
  GeneralTrendStats,
  GeneralTrendTheme,
  TrendPostStatus,
} from "../../lib/types";
import {
  GeneralTrendCard,
  GeneralTrendCardSkeleton,
  generalRegionLabel,
  sourceLabel,
} from "./GeneralTrendCard";
import { GeneralMakeThisDialog } from "./GeneralMakeThisDialog";
import { generalTrendsApi, humanError } from "./generalTrendsApi";
import {
  ChipRow,
  FilterChip,
  FilterPanel,
  FilterToggle,
  MenuItem,
  MenuNote,
  Notice,
  OverflowMenu,
  ResultCount,
  SearchField,
  Toolbar,
  ToolbarSpacer,
} from "./controls";
import { EmptyState, ErrorCard, Field, SELECT_CLS } from "./shared";

const PAGE_SIZE = 36;

/** How often to re-check a running refresh. */
const RUN_POLL_MS = 3000;
/** Give up polling after this many consecutive failures (backend restarting). */
const MAX_POLL_FAILURES = 3;

/**
 * Named after what they DO rather than after the column they order by, and
 * worded exactly as the feed words the same idea: these two screens sit in one
 * tablist, and "Opportunity (blend)" here against "Best opportunity" there was
 * two names for one sort.
 */
const SORTS: Array<{ value: GeneralTrendSort; label: string }> = [
  { value: "opportunity", label: "Best opportunity" },
  { value: "velocity", label: "Hottest right now" },
  { value: "fit", label: "Best fit for 42" },
  { value: "recent", label: "Newest first" },
  { value: "sources", label: "Seen in the most places" },
];

const MIN_FIT_OPTIONS = [
  { value: 0, label: "Any fit" },
  { value: 40, label: "Fit 40+" },
  { value: 60, label: "Fit 60+" },
  { value: 75, label: "Fit 75+" },
];

/** Region codes when /sources hasn't answered (or lists nothing). */
const FALLBACK_REGIONS = ["US", "AE", "global"];

/**
 * How far back the feed reaches, over `last_seen_at`.
 *
 * 0 means "don't send the parameter at all" so the server's own default (7
 * days) stays the single definition of current — the tab must not pin it to
 * whatever number happened to be hard-coded here on the day it shipped.
 *
 * Widening past 30 days also has to widen `content_days`: the server clips on
 * CONTENT age at 30 by default, so asking for 60 days of feed without it would
 * silently return the same rows and read as a broken control.
 */
const FRESH_OPTIONS = [
  { value: 0, label: "Last 7 days (default)" },
  { value: 30, label: "Last 30 days" },
  { value: 60, label: "Last 60 days" },
];

/** The chip and the picker say the same words for the same setting. */
function freshLabel(v: number): string {
  return FRESH_OPTIONS.find((o) => o.value === v)?.label ?? `Last ${v} days`;
}

/**
 * "active" hides dismissed trends client-side — the API's `status` filter takes
 * a single value, so there is no server-side "everything except dismissed".
 */
type ShowMode = "active" | "all" | "new" | "saved" | "used" | "dismissed";

const SHOW_OPTIONS: Array<{ value: ShowMode; label: string }> = [
  { value: "active", label: "Not dismissed" },
  { value: "all", label: "Everything" },
  { value: "new", label: "New only" },
  { value: "saved", label: "Saved" },
  { value: "used", label: "Used" },
  { value: "dismissed", label: "Dismissed" },
];

/** The chip says the same word the picker says. */
function showLabel(v: ShowMode): string {
  return SHOW_OPTIONS.find((o) => o.value === v)?.label ?? v;
}

interface GeneralTrendsTabProps {
  /** Drop the page-level header/refresh card — the host already has page
   *  chrome above this. Default false: the standalone tab is unchanged. */
  embedded?: boolean;
}

export function GeneralTrendsTab({ embedded = false }: GeneralTrendsTabProps = {}) {
  const toast = useToast();
  const confirm = useConfirm();

  // --- filters -------------------------------------------------------------
  const [sort, setSort] = useState<GeneralTrendSort>("opportunity");
  const [region, setRegion] = useState("");
  const [source, setSource] = useState("");
  const [theme, setTheme] = useState("");
  const [minFit, setMinFit] = useState(0);
  const [show, setShow] = useState<ShowMode>("active");
  // ONE switch for both hiding gates. The AI's `sensitivity=avoid` and the
  // ingest filter's `safety=flagged` are our internal distinction; from the
  // outside there is only "stuff you're not showing me".
  const [showHidden, setShowHidden] = useState(false);
  // The feed carries only what people are RECREATING. This reveals the
  // news / event / product rows the participation gate holds back.
  const [showAllKinds, setShowAllKinds] = useState(false);
  const [days, setDays] = useState(0);
  const [qInput, setQInput] = useState("");
  const [q, setQ] = useState("");
  // Shut on arrival. The panel holds the CONTROLS; the chip row below the
  // toolbar holds the CONSEQUENCES, so nothing a filter is doing depends on
  // this being open.
  const [filtersOpen, setFiltersOpen] = useState(false);
  const filterPanelId = useId();

  // --- data ----------------------------------------------------------------
  const [items, setItems] = useState<GeneralTrend[] | null>(null);
  const [total, setTotal] = useState(0);
  const [offset, setOffset] = useState(0);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [themes, setThemes] = useState<GeneralTrendTheme[]>([]);
  const [stats, setStats] = useState<GeneralTrendStats | null>(null);
  const [sources, setSources] = useState<GeneralTrendSource[]>([]);
  const [run, setRun] = useState<GeneralTrendRun | null>(null);
  const [runLoaded, setRunLoaded] = useState(false);
  const [refreshing, setRefreshing] = useState(false);
  const [rescoring, setRescoring] = useState(false);
  const [reloadKey, setReloadKey] = useState(0);
  // Bumped after "Refresh now" so the poll loop restarts on the new run.
  const [pollKey, setPollKey] = useState(0);
  // The trend whose "Make this" picker is open — null when the dialog is closed.
  const [makeTarget, setMakeTarget] = useState<GeneralTrend | null>(null);

  // Debounce the search box — one request per pause, not per keystroke.
  useEffect(() => {
    const t = setTimeout(() => {
      setQ(qInput.trim());
      setOffset(0);
    }, 350);
    return () => clearTimeout(t);
  }, [qInput]);

  // --- side data: themes + sources for the pickers, stats for the header ----
  useEffect(() => {
    let cancelled = false;
    void (async () => {
      const [t, s, src] = await Promise.allSettled([
        generalTrendsApi.themes(),
        generalTrendsApi.stats(),
        generalTrendsApi.sources(),
      ]);
      if (cancelled) return;
      // Each one is non-fatal on its own: the list still renders, the pickers
      // just fall back to their built-in options and the header to generic copy.
      if (t.status === "fulfilled") setThemes(t.value.items ?? []);
      if (s.status === "fulfilled") setStats(s.value);
      if (src.status === "fulfilled") setSources(src.value.items ?? []);
    })();
    return () => {
      cancelled = true;
    };
  }, [reloadKey]);

  // Refresh progress. A refresh runs in a background worker and survives a
  // backend restart, so the tab can't hold the state — it has to ask. Poll only
  // while one is genuinely active, and announce the finish once (then reload
  // the list, because the run is what changes it).
  useEffect(() => {
    let cancelled = false;
    let timer: number | undefined;
    let sawActive = false;
    let failures = 0;

    const tick = async () => {
      try {
        const { run: latest } = await generalTrendsApi.latestRun();
        if (cancelled) return;
        failures = 0;
        setRun(latest);
        setRunLoaded(true);
        if (latest?.active) {
          sawActive = true;
          timer = window.setTimeout(tick, RUN_POLL_MS);
        } else if (sawActive) {
          // Only announce a run we actually watched running, so landing on the
          // tab after an old finished run stays silent.
          sawActive = false;
          if (latest?.status === "failed") {
            toast.error("Refresh failed", latest.error ?? undefined);
          } else {
            toast.success("Refresh finished", latest?.progress ?? undefined);
          }
          setReloadKey((k) => k + 1);
        }
      } catch {
        if (cancelled) return;
        failures += 1;
        // Keep trying through a blip, but don't hammer a backend that's down.
        if (sawActive && failures < MAX_POLL_FAILURES) {
          timer = window.setTimeout(tick, RUN_POLL_MS * 2);
        }
      }
    };

    void tick();
    return () => {
      cancelled = true;
      if (timer) window.clearTimeout(timer);
    };
  }, [toast, pollKey]);

  const filterKey = useMemo(
    () =>
      JSON.stringify({
        sort,
        region,
        source,
        theme,
        minFit,
        show,
        showHidden,
        showAllKinds,
        days,
        q,
      }),
    [sort, region, source, theme, minFit, show, showHidden, showAllKinds, days, q],
  );
  const filterKeyRef = useRef(filterKey);
  const reloadKeyRef = useRef(reloadKey);

  useEffect(() => {
    // Any filter change — or a forced reload (a refresh just finished, a
    // re-score landed) — restarts paging. Bounce through offset=0 rather than
    // fetching page N and appending it onto the list it already came from.
    if (filterKeyRef.current !== filterKey || reloadKeyRef.current !== reloadKey) {
      filterKeyRef.current = filterKey;
      reloadKeyRef.current = reloadKey;
      if (offset !== 0) {
        setOffset(0);
        return;
      }
    }

    let cancelled = false;
    setLoading(true);
    void (async () => {
      try {
        const res = await generalTrendsApi.list({
          sort,
          ...(region ? { region } : {}),
          ...(source ? { source } : {}),
          ...(theme ? { theme } : {}),
          ...(minFit > 0 ? { min_fit: minFit } : {}),
          ...(show !== "active" && show !== "all"
            ? { status: show as TrendPostStatus }
            : {}),
          ...(q ? { q } : {}),
          ...(showHidden
            ? { include_sensitive: true, include_filtered: true }
            : {}),
          // The feed only carries things people are RECREATING (meme, format,
          // sound, challenge). This reveals the news, events and product rows
          // the participation gate holds back.
          ...(showAllKinds ? { all_kinds: true } : {}),
          // Omitted at the default so the server decides what "current" means;
          // past 30 days the content-age gate has to move with it or the wider
          // window returns exactly the same rows.
          ...(days > 0 ? { days } : {}),
          ...(days > 30 ? { content_days: days } : {}),
          // No w_* here any more: the three sliders that sent them were removed
          // by request, so "Best opportunity" is the server's blend and only
          // the server's.
          limit: PAGE_SIZE,
          offset,
        });
        if (cancelled) return;
        setError(null);
        setTotal(res.total ?? res.items.length);
        setItems((prev) =>
          offset > 0 && prev ? [...prev, ...res.items] : res.items,
        );
      } catch (e) {
        if (cancelled) return;
        setError(humanError(e, "Couldn't load Viral Social Trends"));
        if (offset === 0) setItems([]);
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [filterKey, offset, reloadKey]);

  // --- actions -------------------------------------------------------------

  const setStatus = useCallback(
    async (trend: GeneralTrend, next: TrendPostStatus) => {
      const prev = trend.status;
      // Optimistic — a Save/Dismiss should feel instant; revert if it 4xxs.
      setItems((cur) =>
        cur ? cur.map((t) => (t.id === trend.id ? { ...t, status: next } : t)) : cur,
      );
      try {
        await generalTrendsApi.updateTrend(trend.id, { status: next });
      } catch (e) {
        setItems((cur) =>
          cur
            ? cur.map((t) => (t.id === trend.id ? { ...t, status: prev } : t))
            : cur,
        );
        toast.error("Couldn't update that trend", humanError(e));
      }
    },
    [toast],
  );

  // "Make this" opens the tool picker; the model writes that tool's brief and
  // the creator opens pre-filled (lib/producerBrief).
  const makeThis = useCallback(
    (trend: GeneralTrend) => {
      const seed = (
        trend.topic ||
        trend.topic_summary ||
        trend.summary ||
        trend.title ||
        ""
      ).trim();
      if (!seed) {
        // Unscored AND no evidence text — there is genuinely nothing to work from.
        toast.info(
          "Nothing to build from this one",
          "It hasn't been scored and has no description — open the source instead.",
        );
        return;
      }
      setMakeTarget(trend);
    },
    [toast],
  );

  // POST /refresh queues a background run; the poll loop above follows it.
  const startRefresh = useCallback(async () => {
    setRefreshing(true);
    try {
      const res = await generalTrendsApi.refresh();
      setRun(res.run);
      setRunLoaded(true);
      setPollKey((k) => k + 1);
      toast.info("Refreshing", "Pulling the latest from every source…");
    } catch (e) {
      const msg = humanError(e);
      if (/already|busy|409/i.test(msg)) {
        toast.info("Already refreshing", "A refresh is running — hang on.");
        setPollKey((k) => k + 1);
      } else {
        toast.error("Couldn't start a refresh", msg);
      }
    } finally {
      setRefreshing(false);
    }
  }, [toast]);

  // POST /items/rescore with no ids CLEARS every existing score and re-runs the
  // AI pass over the whole table — it is not a "score the new ones" shortcut.
  // That's expensive and irreversible-ish, so it asks first.
  const runRescore = useCallback(async () => {
    const ok = await confirm({
      title: "Re-score every trend?",
      message: (
        <>
          This clears the existing fit / relevance scores and runs the AI pass
          again across all {stats?.trends_total ?? 0} trends. Worth doing after
          the brand profile changes — otherwise the ranking you have is already
          current.
        </>
      ),
      confirmLabel: "Re-score all",
    });
    if (!ok) return;
    setRescoring(true);
    try {
      const res = await generalTrendsApi.rescore();
      toast.success(
        `Scored ${res.scored} trend${res.scored === 1 ? "" : "s"}`,
        "The ranking has been refreshed.",
      );
      setReloadKey((k) => k + 1);
    } catch (e) {
      toast.error("Scoring failed", humanError(e));
    } finally {
      setRescoring(false);
    }
  }, [confirm, stats, toast]);

  const clearFilters = useCallback(() => {
    setSort("opportunity");
    setRegion("");
    setSource("");
    setTheme("");
    setMinFit(0);
    setShow("active");
    setShowHidden(false);
    // Was missing, which made "Clear filters" a button that cleared everything
    // except the one filter still counted in `filtersActive`: press it with
    // "News and events" on and the button stayed on screen, having done
    // nothing to the setting that kept it there.
    setShowAllKinds(false);
    setDays(0);
    setQInput("");
    setQ("");
    setOffset(0);
  }, []);

  // --- derived -------------------------------------------------------------

  const visible = useMemo(
    () =>
      show === "active" && items
        ? items.filter((t) => t.status !== "dismissed")
        : items ?? [],
    [items, show],
  );
  const hiddenDismissed = (items?.length ?? 0) - visible.length;
  const filtersActive =
    !!region ||
    !!source ||
    !!theme ||
    minFit > 0 ||
    !!q ||
    show !== "active" ||
    showHidden ||
    showAllKinds ||
    days > 0;
  const hasMore = (items?.length ?? 0) < total;
  const firstLoad = loading && items === null;
  const runActive = !!run?.active;

  /**
   * How many of the panel's controls are set, for the badge on the Filters
   * button.
   *
   * Everything the panel holds and nothing the toolbar holds: search and sort
   * are on screen already, so counting them would be the badge reporting a
   * state the reader can see. Collapsing controls is only honest if the
   * collapsed state says how much is going on behind it.
   */
  const filterCount = useMemo(() => {
    let n = 0;
    if (region) n += 1;
    if (source) n += 1;
    if (theme) n += 1;
    if (minFit > 0) n += 1;
    if (days > 0) n += 1;
    if (show !== "active") n += 1;
    if (showHidden) n += 1;
    if (showAllKinds) n += 1;
    return n;
  }, [region, source, theme, minFit, days, show, showHidden, showAllKinds]);

  // What the ingest screen threw away on the last run, so a suddenly small
  // feed reads as "the filter worked" rather than "a source broke". `dropped`
  // is null on runs that predate the screen — never render that as zero.
  const dropped = useMemo(() => {
    let total = 0;
    let reported = false;
    const parts: string[] = [];
    for (const s of sources) {
      if (s.dropped == null) continue;
      reported = true;
      total += s.dropped;
      if (s.dropped > 0) {
        const why = Object.entries(s.dropped_by ?? {})
          .map(([reason, n]) => `${reason} ${n}`)
          .join(", ");
        parts.push(
          `${s.label || sourceLabel(s.id)}: ${s.dropped} dropped${why ? ` (${why})` : ""}`,
        );
      }
    }
    return { total, reported, detail: parts.join(" \u00b7 ") };
  }, [sources]);

  // Region picker: every region code the configured sources cover, geos first
  // and "global" last. Falls back to the defaults if /sources didn't answer.
  const regionOptions = useMemo(() => {
    const seen = new Set<string>();
    for (const s of sources) for (const r of s.regions ?? []) seen.add(r);
    const codes = seen.size ? Array.from(seen) : FALLBACK_REGIONS;
    const geos = codes.filter((c) => c !== "global");
    return [...geos, ...(codes.includes("global") ? ["global"] : [])];
  }, [sources]);

  // Menu contents, worked out once so the kebab can decide whether it has
  // anything to open onto.
  const unscoredCount = stats?.trends_unscored ?? 0;
  const droppedCount = dropped.reported ? dropped.total : 0;
  // Re-score is page-level, like Refresh now, so it is offered only on the full
  // tab. That is the promise the Feed's "Open the full tab" button makes about
  // what the embedded copy leaves out, and it is also the truth: re-scoring
  // every trend is not something to start from inside another screen's tab.
  const rescoreOffered = !embedded && (stats?.trends_total ?? 0) > 0;
  const selectedSourceLabel = source
    ? sources.find((s) => s.id === source)?.label || sourceLabel(source)
    : "";

  return (
    /*
      `scroll-mt-24` on every descendant, for the embedded case.

      The Feed pins its category tablist to the top of the viewport once the
      page scrolls, and a browser scrolling a newly focused control into view
      aims for the top of the viewport, which is the space the tablist is now
      occupying. Tab into this grid from off-screen and the focused button
      lands underneath it: `focus-not-obscured`, and a WCAG 2.2 AA failure. The
      Feed applies the same rule to its own body, but this component renders
      outside that wrapper, so it has to carry it. 6rem clears the tablist even
      when its labels wrap to a second line.

      Harmless standalone, where there is nothing sticky: a focused control is
      simply scrolled a little further down the viewport than it would be.
    */
    <div className="space-y-5 [&_*]:scroll-mt-24">
      {/* ---------------------------------------------------------------- */}
      {/* Refresh state, and the one action that changes it.                */}
      {/*                                                                   */}
      {/* This was a card carrying a paragraph about what a refresh does,   */}
      {/* which is what the Refresh button's own tooltip says, plus a       */}
      {/* separate progress span that existed only while a run was active.  */}
      {/* It is one row of status now, in the same Notice the rest of the   */}
      {/* page uses, and one sentence covers every state a run can be in.   */}
      {/*                                                                   */}
      {/* Page-level chrome, so an embedded copy would sit under the host's */}
      {/* own header and read as two page headers in a row. Embedded, the   */}
      {/* refresh controls stay on the full tab, one click away.            */}
      {/* ---------------------------------------------------------------- */}
      {!embedded && (
        <Notice
          // A run that ended in failure is the one state here worth a colour.
          // The sentence names the error either way; amber is what stops it
          // reading as ordinary status.
          tone={!runActive && run?.status === "failed" ? "warning" : "info"}
          Icon={RefreshCw}
          action={
            <button
              type="button"
              onClick={() => void startRefresh()}
              disabled={runActive || refreshing}
              title="Pull the latest from every source now"
              className="btn btn-primary text-xs"
            >
              <RefreshCw
                className={`h-3.5 w-3.5 ${runActive || refreshing ? "animate-spin" : ""}`}
                aria-hidden
              />
              {runActive ? "Refreshing…" : "Refresh now"}
            </button>
          }
        >
          <RefreshSentence stats={stats} run={run} />
        </Notice>
      )}

      {/* ---------------------------------------------------------------- */}
      {/* Controls                                                          */}
      {/*                                                                   */}
      {/* Ranked rather than hidden, and ranked exactly the way the feed     */}
      {/* ranks its own: these two screens sit in one tablist, and a reader  */}
      {/* switching between them should not meet two different control       */}
      {/* systems. Search and sort never collapse. Region, source, theme,    */}
      {/* the fit bar, how far back, what to show, the two "also show"       */}
      {/* switches are one click away behind                                 */}
      {/* Filters, with a count on the button saying how many are set.       */}
      {/*                                                                   */}
      {/* That is only safe because of the chip row: every setting that is   */}
      {/* changing what the grid contains names itself and carries its own   */}
      {/* undo, whether the panel is open or shut. The panel hides the       */}
      {/* CONTROL, never the CONSEQUENCE.                                    */}
      {/* ---------------------------------------------------------------- */}
      <div className="card space-y-3 p-4">
        <Toolbar>
          <SearchField
            value={qInput}
            onChange={setQInput}
            // One handler for the clear button AND for Escape, so both empty
            // the box, the query it applied and the page offset. Clearing only
            // the input would leave the feed filtered by a word that is no
            // longer written anywhere on screen.
            onClear={() => {
              setQInput("");
              setQ("");
              setOffset(0);
            }}
            placeholder="Trend, topic, meme…"
            // No "/" hint. This tab never bound that key, and a keyboard hint
            // for a shortcut that does not exist is worse than none.
            hint=""
            className="min-w-[13rem] flex-1"
          />

          {/* Sort stays in the open row. Its current value is the biggest
              single clue to why the list is in the order it is in, so folding
              it away would hide the answer to the first question anyone asks
              about a ranked feed. The label is visible rather than an
              aria-label: a bare select in a toolbar is a guess. */}
          <label className="inline-flex items-center gap-1.5">
            <span className="eyebrow">Sort</span>
            <select
              value={sort}
              onChange={(e) => {
                setSort(e.target.value as GeneralTrendSort);
                setOffset(0);
              }}
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

          <FilterToggle
            open={filtersOpen}
            count={filterCount}
            onClick={() => setFiltersOpen((v) => !v)}
            panelId={filterPanelId}
          />

          {/* Real but rare, or not an action at all. Re-score is run once
              after the brand profile changes, never during a normal session,
              and the two notes are things worth knowing that nobody can act
              on. Not rendered when it would be empty: a kebab that opens onto
              nothing is worse than no kebab. */}
          {(rescoreOffered || unscoredCount > 0 || droppedCount > 0) && (
            <OverflowMenu label="Scoring and sources">
              {rescoreOffered && (
                <MenuItem
                  onClick={() => void runRescore()}
                  disabled={rescoring || runActive}
                  Icon={Sparkles}
                  title="Clears every score and asks the AI to read every trend again. For after the brand profile changes."
                >
                  {rescoring ? "Re-scoring…" : "Re-score all"}
                </MenuItem>
              )}

              {/* Was a standing amber line in the filter bar. It is a note
                  about work already scheduled, not something to act on, so it
                  sits beside the one button that would change it. */}
              {unscoredCount > 0 && (
                <MenuNote>
                  {unscoredCount} trend{unscoredCount === 1 ? " has" : "s have"}{" "}
                  no fit score or angle yet. The next refresh finishes them off.
                </MenuNote>
              )}

              {droppedCount > 0 && (
                <MenuNote>
                  {droppedCount} row{droppedCount === 1 ? " was" : "s were"}{" "}
                  dropped by the content filter on the way in, before anything
                  was scored.
                  {dropped.detail ? ` ${dropped.detail}.` : ""}
                </MenuNote>
              )}
            </OverflowMenu>
          )}
        </Toolbar>

        {/* ---------------------------------------------------------------- */}
        {/* Chips: every reason the grid holds something other than the       */}
        {/* default. Neutral ones are settings the reader chose and can see    */}
        {/* the effect of; the amber one is rows a default is withholding,     */}
        {/* which is the case nobody asked for and so the only one that needs  */}
        {/* a colour. Both kinds say the number and the reason in words, so    */}
        {/* the tone only ever reinforces what is already written. Every chip  */}
        {/* is its own one-click undo.                                          */}
        {/* ---------------------------------------------------------------- */}
        {(filterCount > 0 || (!firstLoad && hiddenDismissed > 0)) && (
          <ChipRow>
            {region && (
              <FilterChip
                label="Region"
                value={generalRegionLabel(region)}
                onRemove={() => {
                  setRegion("");
                  setOffset(0);
                }}
              />
            )}

            {source && (
              <FilterChip
                label="Source"
                value={selectedSourceLabel}
                onRemove={() => {
                  setSource("");
                  setOffset(0);
                }}
              />
            )}

            {theme && (
              <FilterChip
                label="Theme"
                value={theme}
                onRemove={() => {
                  setTheme("");
                  setOffset(0);
                }}
              />
            )}

            {minFit > 0 && (
              <FilterChip
                label="Fit"
                value={`${minFit} and up`}
                title={`Hiding anything the AI scored below ${minFit} for how practical it is for 42`}
                onRemove={() => {
                  setMinFit(0);
                  setOffset(0);
                }}
              />
            )}

            {days > 0 && (
              <FilterChip
                label="How far back"
                value={freshLabel(days)}
                onRemove={() => {
                  setDays(0);
                  setOffset(0);
                }}
              />
            )}

            {show !== "active" && (
              <FilterChip
                label="Show"
                value={showLabel(show)}
                onRemove={() => {
                  setShow("active");
                  setOffset(0);
                }}
              />
            )}

            {/* The two switches that ADD rows rather than remove them. They
                are still chips: the reader turned a default off, and the row
                of chips is where this screen keeps the answer to "why is this
                list not what it usually is". */}
            {showHidden && (
              <FilterChip
                value="Hidden trends shown"
                title="What the content filter caught on the way in, and what the AI marked as one to leave, are both on screen"
                removeLabel="Hide the flagged trends again"
                onRemove={() => {
                  setShowHidden(false);
                  setOffset(0);
                }}
              />
            )}

            {showAllKinds && (
              <FilterChip
                value="News and events shown"
                title="Things that happened are on screen as well as formats people are copying"
                removeLabel="Go back to formats people are copying"
                onRemove={() => {
                  setShowAllKinds(false);
                  setOffset(0);
                }}
              />
            )}

            {/* Amber: not a filter anyone chose. A feed that quietly drops a
                quarter of its rows is a feed nobody can trust. The undo
                switches to the dismissed view rather than un-hiding in place,
                because the API returns dismissed rows only under an explicit
                status filter. */}
            {!firstLoad && hiddenDismissed > 0 && (
              <FilterChip
                tone="hidden"
                value={`${hiddenDismissed} dismissed`}
                title="Show the dismissed trends on their own"
                removeLabel="Show the dismissed trends on their own"
                onRemove={() => {
                  setShow("dismissed");
                  setOffset(0);
                }}
              />
            )}
          </ChipRow>
        )}

        {/* How much of the list is on screen. Its own component so the digit
            width is fixed and the line is reserved before the first fetch
            lands, which is what stops the grid stepping up by a row the moment
            a number appears. */}
        <ResultCount
          loading={firstLoad}
          shown={visible.length}
          total={total}
          noun="trend"
        />

        {/* ---------------------------------------------------------------- */}
        {/* The panel. Shut on arrival, and out of the tab order while shut.  */}
        {/* ---------------------------------------------------------------- */}
        <FilterPanel
          id={filterPanelId}
          open={filtersOpen}
          footer={
            filtersActive ? (
              <button
                type="button"
                onClick={clearFilters}
                className="btn btn-ghost text-xs"
              >
                <RotateCcw className="h-3.5 w-3.5" aria-hidden />
                Clear filters
              </button>
            ) : undefined
          }
        >
          <Field label="Region">
            <select
              value={region}
              onChange={(e) => {
                setRegion(e.target.value);
                setOffset(0);
              }}
              className={`${SELECT_CLS} w-full`}
            >
              <option value="">All regions</option>
              {regionOptions.map((r) => (
                <option key={r} value={r}>
                  {generalRegionLabel(r)}
                </option>
              ))}
            </select>
          </Field>

          <Field label="Source">
            <select
              value={source}
              onChange={(e) => {
                setSource(e.target.value);
                setOffset(0);
              }}
              className={`${SELECT_CLS} w-full`}
            >
              <option value="">All sources</option>
              {sources.map((s) => (
                <option key={s.id} value={s.id}>
                  {s.label || sourceLabel(s.id)}
                  {s.enabled ? "" : " (off)"}
                </option>
              ))}
            </select>
          </Field>

          <Field label="Theme">
            <select
              value={theme}
              onChange={(e) => {
                setTheme(e.target.value);
                setOffset(0);
              }}
              className={`${SELECT_CLS} w-full`}
            >
              <option value="">All themes</option>
              {themes.map((t) => (
                <option key={t.theme} value={t.theme}>
                  {t.theme} ({t.trend_count})
                </option>
              ))}
            </select>
          </Field>

          <Field label="Min fit">
            <select
              value={String(minFit)}
              onChange={(e) => {
                setMinFit(Number(e.target.value));
                setOffset(0);
              }}
              className={`${SELECT_CLS} w-full`}
            >
              {MIN_FIT_OPTIONS.map((o) => (
                <option key={o.value} value={o.value}>
                  {o.label}
                </option>
              ))}
            </select>
          </Field>

          {/* "Fresh" was a label for the column, not for the choice. */}
          <Field label="How far back">
            <select
              value={String(days)}
              onChange={(e) => {
                setDays(Number(e.target.value));
                setOffset(0);
              }}
              className={`${SELECT_CLS} w-full`}
            >
              {FRESH_OPTIONS.map((o) => (
                <option key={o.value} value={o.value}>
                  {o.label}
                </option>
              ))}
            </select>
          </Field>

          <Field label="Show">
            <select
              value={show}
              onChange={(e) => {
                setShow(e.target.value as ShowMode);
                setOffset(0);
              }}
              className={`${SELECT_CLS} w-full`}
            >
              {SHOW_OPTIONS.map((o) => (
                <option key={o.value} value={o.value}>
                  {o.label}
                </option>
              ))}
            </select>
          </Field>

          {/* The two switches that widen what the feed carries.

              Their labels are now NOUNS and they stay put: the buttons used to
              read "Show hidden" when off and "Hidden shown" when on, which
              contradicted their own aria-pressed state on every second press.
              A fixed label plus the pressed fill says the same thing once, and
              says it to a screen reader too. Same pattern as the card's Save
              button and the feed's UAE toggle, rather than a third kind of
              switch on a third screen.

              `role="group"` so the pair is announced under the heading a
              sighted reader already sees above them. Without it they are two
              unrelated buttons that happen to sit next to each other. */}
          <div className="col-span-full" role="group" aria-label="Also show">
            <p className="eyebrow mb-1.5">Also show</p>
            <div className="flex flex-wrap items-center gap-2">
              <button
                type="button"
                onClick={() => {
                  setShowHidden((v) => !v);
                  setOffset(0);
                }}
                aria-pressed={showHidden}
                title="Two things are hidden by default: what the content filter caught on the way in (adult content, dating bait, war and politics) and what the AI marked as one to leave. This shows both."
                className={`btn text-xs ${showHidden ? "btn-accent" : "btn-ghost"}`}
              >
                <ShieldAlert className="h-3.5 w-3.5" aria-hidden />
                Hidden trends
              </button>

              <button
                type="button"
                onClick={() => {
                  setShowAllKinds((v) => !v);
                  setOffset(0);
                }}
                aria-pressed={showAllKinds}
                title="A trend here is a format people are COPYING: a meme, a sound, a challenge, a template. By default the feed carries only those. This also shows news, events and product launches, which are things that happened rather than things anyone can make their own version of."
                className={`btn text-xs ${showAllKinds ? "btn-accent" : "btn-ghost"}`}
              >
                <Newspaper className="h-3.5 w-3.5" aria-hidden />
                News and events
              </button>
            </div>
          </div>

        </FilterPanel>
      </div>

      {error && <ErrorCard message={error} />}

      {/* ---------------------------------------------------------------- */}
      {/* Grid                                                              */}
      {/* ---------------------------------------------------------------- */}
      {firstLoad ? (
        <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-3">
          {Array.from({ length: 6 }).map((_, i) => (
            <GeneralTrendCardSkeleton key={i} />
          ))}
        </div>
      ) : visible.length === 0 ? (
        <GeneralEmptyState
          stats={stats}
          run={run}
          runLoaded={runLoaded}
          refreshing={refreshing}
          filtersActive={filtersActive}
          hiddenDismissed={hiddenDismissed}
          onRefresh={() => void startRefresh()}
          onClearFilters={clearFilters}
          onShowDismissed={() => {
            // The API only returns dismissed trends under an explicit
            // status=dismissed filter (the default and "Everything" both hide
            // them server-side), so this is the one mode that can show them.
            setShow("dismissed");
            setOffset(0);
          }}
        />
      ) : (
        <>
          <div
            className={`grid gap-4 sm:grid-cols-2 xl:grid-cols-3 transition-opacity ${
              loading ? "opacity-60" : "opacity-100"
            }`}
          >
            {visible.map((trend) => (
              <GeneralTrendCard
                key={trend.id}
                trend={trend}
                onStatus={setStatus}
                onMakeThis={makeThis}
              />
            ))}
          </div>

          {hasMore && (
            <div className="flex justify-center pt-2">
              <button
                type="button"
                disabled={loading}
                onClick={() => setOffset(items?.length ?? 0)}
                className="btn btn-ghost text-sm"
              >
                {loading
                  ? "Loading…"
                  : `Load more · ${total - (items?.length ?? 0)} left`}
              </button>
            </div>
          )}
        </>
      )}

      {makeTarget && (
        <GeneralMakeThisDialog
          trend={makeTarget}
          onClose={() => setMakeTarget(null)}
        />
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Header sentence
// ---------------------------------------------------------------------------

/** "Refreshed 2h ago · 6/7 sources · auto every 6h" — or what stands in for it. */
function RefreshSentence({
  stats,
  run,
}: {
  stats: GeneralTrendStats | null;
  run: GeneralTrendRun | null;
}) {
  if (run?.active) {
    // The progress and the source count used to be a separate span beside the
    // sentence, which meant two status lines in one row, one of them empty
    // most of the time. Same sentence, running or not.
    const live: string[] = [run.progress || "Pulling the latest from every source"];
    if (run.sources_total != null && run.sources_total > 0) {
      live.push(`${run.sources_done ?? 0}/${run.sources_total} sources`);
    }
    return <>{live.join(" · ")}</>;
  }
  const last = stats?.last_run_at ?? run?.finished_at ?? run?.started_at ?? null;
  const parts: string[] = [];
  parts.push(last ? `Refreshed ${fmtRel(last)}` : "Never refreshed");
  if (stats && stats.sources_total > 0) {
    parts.push(`${stats.sources_ok}/${stats.sources_total} sources`);
  }
  if (stats && stats.refresh_every_hours > 0) {
    parts.push(`auto every ${stats.refresh_every_hours}h`);
  } else if (stats) {
    parts.push("manual refresh only");
  }
  if (run?.status === "failed" && run.error) {
    parts.push(`last run failed: ${run.error}`);
  }
  return <>{parts.join(" · ")}</>;
}

// ---------------------------------------------------------------------------
// Empty states — each one names the next action
// ---------------------------------------------------------------------------

function GeneralEmptyState({
  stats,
  run,
  runLoaded,
  refreshing,
  filtersActive,
  hiddenDismissed,
  onRefresh,
  onClearFilters,
  onShowDismissed,
}: {
  stats: GeneralTrendStats | null;
  run: GeneralTrendRun | null;
  runLoaded: boolean;
  refreshing: boolean;
  filtersActive: boolean;
  hiddenDismissed: number;
  onRefresh: () => void;
  onClearFilters: () => void;
  onShowDismissed: () => void;
}) {
  if (run?.active) {
    return (
      <EmptyState
        title="Refreshing now"
        body={
          run.progress ||
          "Pulling the latest from every source, merging the same story into one card, then scoring what's new. This takes a minute or two."
        }
      />
    );
  }

  const neverRun =
    (runLoaded && run == null) || (stats != null && stats.last_run_at == null);
  if (neverRun && !filtersActive) {
    return (
      <EmptyState
        title="Nothing pulled in yet"
        body={
          <>
            Nothing has been pulled from the sources yet. Run a refresh: it
            fetches what's spiking on Google Trends, Bluesky, Hacker News,
            Lemmy, GitHub, Google News, Reddit, Know Your Meme, Wikipedia,
            YouTube, TikTok and Instagram, merges the same story into one card,
            and scores how well 42 could ride each one.
          </>
        }
        action={
          <button
            type="button"
            className="btn btn-primary text-sm"
            onClick={onRefresh}
            disabled={refreshing}
          >
            <RefreshCw className="h-4 w-4" aria-hidden />
            Refresh now
          </button>
        }
      />
    );
  }

  if (filtersActive) {
    return (
      <EmptyState
        title="Nothing matches these filters"
        body="Loosen the minimum fit, widen the region or source, or clear the search to see more trends."
        action={
          <button type="button" className="btn btn-ghost text-sm" onClick={onClearFilters}>
            Clear filters
          </button>
        }
      />
    );
  }

  if (hiddenDismissed > 0) {
    return (
      <EmptyState
        title="You've dismissed everything here"
        body={`All ${hiddenDismissed} trend${hiddenDismissed === 1 ? " has" : "s have"} been marked as "not for us". Refresh for new ones, or bring the dismissed ones back to reconsider them.`}
        action={
          <button type="button" className="btn btn-ghost text-sm" onClick={onShowDismissed}>
            Show dismissed
          </button>
        }
      />
    );
  }

  return (
    <EmptyState
      title="No trends yet"
      body="The last refresh didn't leave anything people are actually copying. This feed carries memes, formats, sounds and challenges, so a run that found only news and search spikes lands here. Sources may also have been unreachable, or everything was too old or not safe for a school's feed. Open Filters and turn on 'News and events' or 'Hidden trends', reach further back than 7 days, or refresh again."
      action={
        <button
          type="button"
          className="btn btn-ghost text-sm"
          onClick={onRefresh}
          disabled={refreshing}
        >
          <RefreshCw className="h-4 w-4" aria-hidden />
          Refresh now
        </button>
      }
    />
  );
}
