/**
 * Competitors: the Instagram accounts the trend scanner watches, and the two
 * ways one gets onto that list.
 *
 * This screen is a FORM first and a list second, so it is laid out as one
 * decision per group, in the order the work happens:
 *
 *   toolbar        find a row (search, sort, filters) on the left; change the
 *                  list (Add account, Find accounts with AI, Scan all) on the
 *                  right; chips for every filter hiding rows; the panel
 *   status         a scan running right now, or how the last one failed
 *   open panel     the add form, or the research results, never both
 *   Tracked        the accounts every scan reads
 *   Excluded       parked accounts, collapsed, one click from coming back
 *
 * Suggestions, tracked accounts and excluded accounts are three different
 * kinds of row, so each one is its own labelled section with its own count and
 * its own heading. They used to run together as one column of near-identical
 * rows, which is how a suggestion waiting for a decision ended up looking like
 * an account that was already being scanned.
 *
 * The toolbar is the feed's control system (ui/controls.tsx), ranked the same
 * way: search and sort always on screen, region / provenance / scan health one
 * click away, a chip per filter that is actually hiding rows. The chips are
 * what makes folding the controls away honest: a list that quietly drops rows
 * is a list nobody trusts. Search, sort and filters are all client-side over
 * the rows already fetched, and must stay so: the Excluded section and the
 * research panel's "Already tracked" check both need the full payload.
 *
 * Every row leads with ONE number, posts found, in the same pill geometry the
 * feed gives outperformance. Followers would be the obvious hero and the wrong
 * one: the feed's whole thesis is that raw size says nothing, and what a
 * reader of this screen actually wants to know is whether watching an account
 * is producing anything.
 *
 * Two things make this fiddly, and both are designed for here:
 *
 *  1. Instagram's Business Discovery API can only read Business and Creator
 *     accounts. Adding a perfectly real handle and being refused is the SINGLE
 *     most common outcome, not an error, so the server's plain-language reason
 *     is shown as guidance beside the field that caused it, never as a red
 *     crash banner. That limitation is spelled out in exactly ONE place, the
 *     hint under the handle field, because it only matters while somebody is
 *     typing a handle. Saying it again in the empty state and again in the
 *     research panel is how a warning gets trained out.
 *  2. A scan is a long background job. The tab re-syncs on mount and resumes
 *     polling if one is already running, because `PageTransition` remounts tab
 *     bodies and would otherwise strand a running scan with no progress UI.
 *
 * Every request body here is the one the backend already accepts. The layout
 * moved and the words changed; nothing about what is sent did.
 */
import { useCallback, useEffect, useId, useMemo, useRef, useState } from "react";
import type { ComponentType, RefObject } from "react";
import {
  AlertTriangle,
  ChevronDown,
  ChevronRight,
  EyeOff,
  Loader2,
  Pencil,
  Plus,
  RadioTower,
  RefreshCw,
  RotateCcw,
  Search,
  Trash2,
  Undo2,
  X,
} from "lucide-react";

import { useConfirm } from "../ConfirmProvider";
import { Skeleton } from "../Skeleton";
import { useToast } from "../ToastProvider";
import { fmtRel } from "../../lib/format";
import { usePoll } from "../../lib/usePoll";
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
import {
  Avatar,
  EmptyState,
  ErrorCard,
  Field,
  fmtCompact,
  INPUT_CLS,
  MetricPill,
  regionLabel,
  SELECT_CLS,
} from "./shared";
import { SectionHeader } from "../ui/primitives";
import { humanError, trendsApi } from "./trendsApi";
import type {
  ResearchCandidate,
  ResearchResult,
  TrendCompetitor,
  TrendRegion,
  TrendResearchEngagement,
  TrendResearchGeo,
  TrendResearchRelevance,
  TrendResearchSize,
  TrendScan,
} from "./types";

/**
 * How many posts one read may take from one account.
 *
 * The backend accepts 5 to 200 and defaults to 200, which is what the old
 * four-page ceiling already allowed, so 200 means "same as it always was". The
 * smaller numbers are the useful ones day to day: nobody needs two hundred
 * posts to see what an account has been doing this week.
 */
const POST_CAPS = [10, 25, 50, 100, 200];
const DEFAULT_POST_CAP = 50;

/** "day", "2 days", "12 hours": the server's automatic-read interval, said the
 *  way a person would. */
function everyPhrase(hours: number): string {
  if (hours % 24 === 0) return hours === 24 ? "day" : `${hours / 24} days`;
  return hours === 1 ? "hour" : `${hours} hours`;
}

const SCAN_POLL_MS = 2000;

/** How long a freshly added row wears its "Just added" pill. */
const JUST_ADDED_MS = 8000;

/**
 * Which request is in flight for one row.
 *
 * It used to be a plain list of ids, which was enough to disable the row but
 * not enough to say WHICH button the reader had pressed. Recording the action
 * lets the spinner land on the button that was clicked, so "Exclude" and
 * "Delete" no longer both look like they might be the one running.
 */
type RowBusy = "status" | "delete" | "edit";

// ---------------------------------------------------------------------------
// Sorting and narrowing
// ---------------------------------------------------------------------------

type SortKey = "audience" | "posts" | "stale" | "newest" | "name";

/**
 * The default reproduces the server's own order (followers descending, nulls
 * last, then handle), so the first paint is the list exactly as it was before
 * there was a sort control.
 */
const SORTS: ReadonlyArray<{ value: SortKey; label: string }> = [
  { value: "audience", label: "Biggest audience" },
  { value: "posts", label: "Most posts found" },
  { value: "stale", label: "Longest since a scan" },
  { value: "newest", label: "Newest added" },
  { value: "name", label: "A to Z" },
];

/** "" is "any". "none" is a region the account was never given. */
type RegionFilter = "" | TrendRegion | "none";
type SourceFilter = "" | TrendCompetitor["source"];

/**
 * How the last scan of an account went, folded from three columns the backend
 * writes (last_scanned_at, last_scan_status, last_scan_error) into one word.
 * `failed` is checked before `empty` because a failed scan leaves the previous
 * posts_found in place on purpose (record_scan_result in trend_store.py), so a
 * zero there says nothing about this attempt.
 */
type ScanHealth = "never" | "failed" | "empty" | "ok";
type HealthFilter = "" | ScanHealth;

/** Short, because they always sit under a "Last scan" label, in the panel's
 *  field and on the chip alike. */
const HEALTH_LABEL: Record<ScanHealth, string> = {
  never: "Never",
  failed: "Failed",
  empty: "Found nothing",
  ok: "Found posts",
};

const SOURCE_LABEL: Record<TrendCompetitor["source"], string> = {
  manual: "By hand",
  ai_research: "Found by AI",
};

function scanHealth(c: TrendCompetitor): ScanHealth {
  if (!c.last_scanned_at) return "never";
  if (c.last_scan_status === "failed" || c.last_scan_error) return "failed";
  if (c.last_scan_status === "empty" || (c.posts_found ?? 0) === 0) return "empty";
  return "ok";
}

/** Epoch ms, or 0 for a missing or unparseable date, so sorts never see NaN. */
function ts(iso: string | null | undefined): number {
  const t = iso ? Date.parse(iso) : NaN;
  return Number.isNaN(t) ? 0 : t;
}

function byName(a: TrendCompetitor, b: TrendCompetitor): number {
  return a.username.localeCompare(b.username);
}

function compare(a: TrendCompetitor, b: TrendCompetitor, sort: SortKey): number {
  switch (sort) {
    case "posts":
      return (b.posts_found ?? 0) - (a.posts_found ?? 0) || byName(a, b);
    // Never scanned sorts first: 0 is older than any real date, which is the
    // point of this order. It exists to surface the accounts nobody has read.
    case "stale":
      return ts(a.last_scanned_at) - ts(b.last_scanned_at) || byName(a, b);
    case "newest":
      return ts(b.created_at) - ts(a.created_at) || byName(a, b);
    case "name":
      return byName(a, b);
    case "audience":
    default:
      return (b.followers_count ?? -1) - (a.followers_count ?? -1) || byName(a, b);
  }
}

interface Narrowing {
  /** already trimmed and lower-cased */
  needle: string;
  region: RegionFilter;
  source: SourceFilter;
  health: HealthFilter;
  sort: SortKey;
}

/** Pure, so both lists (tracked and excluded) go through the same rules. */
function narrow(list: TrendCompetitor[], n: Narrowing): TrendCompetitor[] {
  const kept = list.filter((c) => {
    if (
      n.needle &&
      !`${c.username} ${c.display_name ?? ""} ${c.category ?? ""}`
        .toLowerCase()
        .includes(n.needle)
    ) {
      return false;
    }
    if (n.region) {
      const want = n.region === "none" ? null : n.region;
      if ((c.region ?? null) !== want) return false;
    }
    if (n.source && c.source !== n.source) return false;
    if (n.health && scanHealth(c) !== n.health) return false;
    return true;
  });
  return kept.sort((a, b) => compare(a, b, n.sort));
}

function median(xs: number[]): number {
  if (xs.length === 0) return 0;
  const s = [...xs].sort((a, b) => a - b);
  const mid = s.length >> 1;
  return s.length % 2 ? s[mid] : (s[mid - 1] + s[mid]) / 2;
}

// ---------------------------------------------------------------------------
// Small atoms
// ---------------------------------------------------------------------------

/**
 * A button's icon, swapped for a spinner while that button's own request is in
 * flight. The `Loading Buttons` rule: an async action disables its control and
 * shows progress on it, rather than leaving the page looking untouched.
 */
function BtnIcon({
  busy,
  Icon,
  className = "h-3.5 w-3.5",
}: {
  busy: boolean;
  Icon: ComponentType<any>;
  className?: string;
}) {
  return busy ? (
    <Loader2 className={`${className} animate-spin`} aria-hidden />
  ) : (
    <Icon className={className} aria-hidden />
  );
}

/**
 * `Notice` sets its icon's className itself, so a spinning one has to arrive as
 * a component that appends the animation rather than as a styled element.
 */
function SpinnerIcon({ className = "" }: { className?: string }) {
  return <RefreshCw className={`${className} animate-spin`} aria-hidden />;
}

/**
 * One label/value pair on a row's metadata line.
 *
 * These were five fixed-width columns, which on any width below a desktop
 * wrapped into a ragged block that pushed the buttons onto their own line.
 * Inline pairs inside a `<dl>` wrap as a sentence does, and the label stays
 * attached to its number, which is the part a column layout kept losing.
 */
function RowStat({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex items-baseline gap-1.5">
      <dt className="eyebrow">{label}</dt>
      <dd
        className="max-w-[12rem] truncate font-mono text-xs tabular-nums text-fg"
        title={value}
      >
        {value}
      </dd>
    </div>
  );
}

/**
 * Bring a freshly mounted panel on screen and put the caret in its first field.
 *
 * Both panels can be opened from the empty state, which sits BELOW the slot
 * they mount into. The add form got away with it because its handle input had
 * `autoFocus`, and focusing an off-screen element drags the viewport to it;
 * the research panel had nothing, so a keyboard user pressing its button got
 * no focus change, no announcement, and a panel somewhere above them.
 *
 * `nearest` rather than `center`, and no pulse: from the toolbar the panel
 * mounts directly under the button that opened it and is already in view, in
 * which case this must move nothing at all. lib/useRevealPanel is the tool for
 * a panel that opens far from the click; it centres and flashes, which here
 * would be motion on every open for the benefit of one path. Focus is taken
 * with `preventScroll` because focusing jumps instantly, which would cancel
 * the smooth scroll started on the line above it.
 */
function useRevealOnMount(
  panelRef: RefObject<HTMLElement>,
  focusRef: RefObject<HTMLElement>,
) {
  useEffect(() => {
    const still =
      typeof window.matchMedia === "function" &&
      window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    panelRef.current?.scrollIntoView({
      block: "nearest",
      behavior: still ? "auto" : "smooth",
    });
    focusRef.current?.focus({ preventScroll: true });
  }, [panelRef, focusRef]);
}

// ---------------------------------------------------------------------------
// The tab
// ---------------------------------------------------------------------------

export function CompetitorsTab() {
  const toast = useToast();
  const confirm = useConfirm();

  const [items, setItems] = useState<TrendCompetitor[] | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [retrying, setRetrying] = useState(false);
  // Scan state is kept here for three jobs: the in-tab progress line (the page
  // shell draws its own on every OTHER route and stays out of this one), the
  // scan buttons, which must be disabled while one runs, and the rows, which
  // are refreshed the moment it finishes.
  const [scan, setScan] = useState<TrendScan | null>(null);
  // The ceiling on the NEXT read, per account. Picked every time rather than
  // stored, because "check what is new" and "pull in everything you can" are
  // different asks on different days. The server's own daily read uses the
  // default, which is the "check what is new" answer.
  const [postCap, setPostCap] = useState(DEFAULT_POST_CAP);
  // How often the server reads every account by itself (0 = never). Said under
  // the Tracked heading, because a feed that refreshes on its own and a feed
  // that waits for this button call for different habits.
  const [autoHours, setAutoHours] = useState(0);
  const [busyRows, setBusyRows] = useState<Record<number, RowBusy>>({});
  const [addOpen, setAddOpen] = useState(false);
  const [researchOpen, setResearchOpen] = useState(false);
  // Excluded accounts start folded away: they are the group nobody came here
  // to read. The heading still carries the count, so the fold hides the rows,
  // never the fact that there are rows.
  const [excludedOpen, setExcludedOpen] = useState(false);
  // The row just added by hand or from the research panel. The list is sorted,
  // so a new account lands wherever the sort puts it rather than on top, and a
  // pill for a few seconds is how the reader finds it there.
  const [justAdded, setJustAdded] = useState<number | null>(null);

  // Find-a-row controls. All client-side; see the header.
  const [q, setQ] = useState("");
  const [sort, setSort] = useState<SortKey>("audience");
  const [region, setRegion] = useState<RegionFilter>("");
  const [source, setSource] = useState<SourceFilter>("");
  const [health, setHealth] = useState<HealthFilter>("");
  const [filtersOpen, setFiltersOpen] = useState(false);

  const addPanelId = useId();
  const researchPanelId = useId();
  const filterPanelId = useId();
  const trackedTitleId = useId();
  const excludedTitleId = useId();
  const excludedListId = useId();

  // Closing a panel puts focus back on the button that opened it. Without this
  // the panel vanishes and a keyboard user is returned to the top of the
  // document with no idea where they were.
  const addBtnRef = useRef<HTMLButtonElement>(null);
  const researchBtnRef = useRef<HTMLButtonElement>(null);
  const searchRef = useRef<HTMLInputElement>(null);

  const markBusy = useCallback((id: number, kind: RowBusy | null) => {
    setBusyRows((prev) => {
      if (kind === null) {
        if (!(id in prev)) return prev;
        const next = { ...prev };
        delete next[id];
        return next;
      }
      return { ...prev, [id]: kind };
    });
  }, []);

  const reload = useCallback(async () => {
    try {
      const res = await trendsApi.competitors();
      setItems(res.items ?? []);
      setError(null);
    } catch (e) {
      setError(humanError(e, "Couldn't load competitors"));
      setItems((prev) => prev ?? []);
    }
  }, []);

  // Mount: everything this screen needs, in one pass. The `cancelled` guard is
  // what keeps StrictMode's double-invoke from writing state twice.
  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    void (async () => {
      const [list, latest] = await Promise.allSettled([
        trendsApi.competitors(),
        trendsApi.latestScan(),
      ]);
      if (cancelled) return;
      if (list.status === "fulfilled") {
        setItems(list.value.items ?? []);
        setError(null);
      } else {
        setItems([]);
        setError(humanError(list.reason, "Couldn't load competitors"));
      }
      if (latest.status === "fulfilled") {
        setScan(latest.value.scan);
        setAutoHours(latest.value.auto_scan_hours ?? 0);
      }
      setLoading(false);
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  // Resume polling whenever a scan is live: on mount, or right after starting
  // one. `enabled` is a boolean, so the loop is not torn down and rebuilt on
  // every tick, and it stops itself the moment the scan settles. A transient
  // failure backs off rather than killing the loop; usePoll bounds that.
  usePoll({
    enabled: !!scan?.active,
    intervalMs: SCAN_POLL_MS,
    onTick: async () => {
      const res = await trendsApi.latestScan();
      setScan(res.scan);
      if (res.scan?.active) return;
      // Pull the fresh posts_found / last_scanned_at into the rows. The "scan
      // finished" toast is the page shell's job; announcing it here too would
      // fire the same news twice.
      await reload();
      return "stop";
    },
  });

  /**
   * "/" puts the cursor in the search box, Escape gives it back.
   *
   * Guarded so it cannot steal a character the reader meant to type: not while
   * they are already in a field, and not while a dialog is open, where "/"
   * belongs to whatever has focus in there. Same keystroke, same guard, as the
   * feed, so the two screens under one tab bar answer to one key.
   */
  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      if (e.key !== "/" || e.metaKey || e.ctrlKey || e.altKey) return;
      const el = document.activeElement as HTMLElement | null;
      const tag = el?.tagName;
      if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT") return;
      if (el?.isContentEditable) return;
      if (document.querySelector('[role="dialog"]')) return;
      e.preventDefault();
      searchRef.current?.focus();
      searchRef.current?.select();
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  useEffect(() => {
    if (justAdded == null) return;
    const timer = window.setTimeout(() => setJustAdded(null), JUST_ADDED_MS);
    return () => window.clearTimeout(timer);
  }, [justAdded]);

  // --- actions -------------------------------------------------------------

  // The two panels are mutually exclusive, and every way of opening one says so
  // in the same place. The empty state used to open its panel directly, which
  // is how both could end up on screen at once.
  const openAdd = useCallback(() => {
    setResearchOpen(false);
    setAddOpen(true);
  }, []);
  const openResearch = useCallback(() => {
    setAddOpen(false);
    setResearchOpen(true);
  }, []);
  const closeAdd = useCallback(() => {
    setAddOpen(false);
    addBtnRef.current?.focus();
  }, []);
  const closeResearch = useCallback(() => {
    setResearchOpen(false);
    researchBtnRef.current?.focus();
  }, []);

  const retry = useCallback(async () => {
    setRetrying(true);
    await reload();
    setRetrying(false);
  }, [reload]);

  const startScan = useCallback(
    async (competitorId?: number) => {
      try {
        const res = competitorId
          ? await trendsApi.scanCompetitor(competitorId, { max_posts: postCap })
          : await trendsApi.scan({ max_posts: postCap });
        setScan(res.scan);
        toast.info(
          competitorId ? "Reading that account…" : "Reading new posts",
          `Up to ${postCap} per account. It runs in the background, and progress shows on this page.`,
        );
      } catch (e) {
        const msg = humanError(e);
        // 409 = one is already running. That's a state, not a failure.
        if (/already|running|409/i.test(msg)) {
          toast.info("A scan is already running", "Wait for it to finish, then try again.");
          try {
            const latest = await trendsApi.latestScan();
            setScan(latest.scan);
          } catch {
            /* leave the existing banner alone */
          }
        } else {
          toast.error("Couldn't start reading", msg);
        }
      }
    },
    [toast, postCap],
  );

  const setStatus = useCallback(
    async (c: TrendCompetitor, status: "active" | "excluded") => {
      markBusy(c.id, "status");
      try {
        const res = await trendsApi.updateCompetitor(c.id, { status });
        setItems((prev) =>
          prev
            ? prev.map((x) => (x.id === c.id ? res.competitor ?? { ...x, status } : x))
            : prev,
        );
        // Excluding moves the row into a group that is folded by default, so
        // open that group: a row that disappears with no visible destination
        // reads as a delete, which is the one thing this action is not.
        if (status === "excluded") setExcludedOpen(true);
        toast.info(
          status === "excluded"
            ? `@${c.username} won't be scanned again`
            : `@${c.username} is back in the rotation`,
        );
      } catch (e) {
        toast.error("Couldn't update that account", humanError(e));
      } finally {
        markBusy(c.id, null);
      }
    },
    [markBusy, toast],
  );

  const saveDetails = useCallback(
    async (
      c: TrendCompetitor,
      patch: {
        region: TrendRegion | null;
        category: string | null;
        why: string | null;
      },
    ) => {
      markBusy(c.id, "edit");
      try {
        const res = await trendsApi.updateCompetitor(c.id, patch);
        setItems((prev) =>
          prev
            ? prev.map((x) => (x.id === c.id ? res.competitor ?? { ...x, ...patch } : x))
            : prev,
        );
        toast.info(`Updated @${c.username}`);
      } catch (e) {
        toast.error("Couldn't save those details", humanError(e));
        // Rethrown so the row keeps the form open with what was typed still in
        // it. Closing on a failure loses the edit and says nothing went wrong.
        throw e;
      } finally {
        markBusy(c.id, null);
      }
    },
    [markBusy, toast],
  );

  const remove = useCallback(
    async (c: TrendCompetitor) => {
      const ok = await confirm({
        title: `Delete @${c.username}?`,
        message: (
          <>
            This removes the account <strong>and every post of theirs</strong>{" "}
            already in the feed
            {c.posts_found ? ` (${c.posts_found} posts)` : ""}, including
            anything you saved from them.
            <br />
            <br />
            To simply stop scanning them without losing the posts, use{" "}
            <strong>Exclude</strong> instead.
          </>
        ),
        confirmLabel: "Delete",
        danger: true,
      });
      if (!ok) return;
      markBusy(c.id, "delete");
      try {
        await trendsApi.deleteCompetitor(c.id);
        setItems((prev) => (prev ? prev.filter((x) => x.id !== c.id) : prev));
        toast.info(`Deleted @${c.username}`);
      } catch (e) {
        toast.error("Couldn't delete that account", humanError(e));
      } finally {
        markBusy(c.id, null);
      }
    },
    [confirm, markBusy, toast],
  );

  const onAdded = useCallback((competitor: TrendCompetitor) => {
    setItems((prev) => {
      if (!prev) return [competitor];
      const without = prev.filter((x) => x.id !== competitor.id);
      return [competitor, ...without];
    });
    setJustAdded(competitor.id);
  }, []);

  const clearFilters = useCallback(() => {
    setRegion("");
    setSource("");
    setHealth("");
  }, []);
  const clearAll = useCallback(() => {
    setQ("");
    clearFilters();
  }, [clearFilters]);

  // --- derived -------------------------------------------------------------

  const active = useMemo(
    () => (items ?? []).filter((c) => c.status !== "excluded"),
    [items],
  );
  const excluded = useMemo(
    () => (items ?? []).filter((c) => c.status === "excluded"),
    [items],
  );
  const narrowing = useMemo<Narrowing>(
    () => ({ needle: q.trim().toLowerCase(), region, source, health, sort }),
    [q, region, source, health, sort],
  );
  const visibleActive = useMemo(() => narrow(active, narrowing), [active, narrowing]);
  const visibleExcluded = useMemo(
    () => narrow(excluded, narrowing),
    [excluded, narrowing],
  );
  // What the hero pill calls "typical": the median over accounts that have
  // actually been read. Counting the unscanned zeros would drag it to nothing
  // on a young list and let every scanned account through as strong.
  const benchmark = useMemo(
    () =>
      median(
        active.filter((c) => c.last_scanned_at).map((c) => c.posts_found ?? 0),
      ),
    [active],
  );
  const filterCount = (region ? 1 : 0) + (source ? 1 : 0) + (health ? 1 : 0);
  const scanning = !!scan?.active;
  const firstLoad = loading && items === null;
  // How the last scan ended, for the Tracked section's one line of context. A
  // running or failed scan is a Notice above instead; this is neither.
  const settledScan = scan && !scan.active && scan.status !== "failed" ? scan : null;
  const settledWhen = settledScan ? settledScan.finished_at ?? settledScan.started_at : null;
  const excludedCount =
    visibleExcluded.length === excluded.length
      ? String(excluded.length)
      : `${visibleExcluded.length} of ${excluded.length}`;

  return (
    <div className="space-y-5">
      {/* This screen's own name and sentence. It used to have neither: the
          Trends shell printed both above it, back when it was a tab there and
          the shell knew which one was open. It grew them as a section of the
          Settings page, stacked between three others, and it kept them coming
          back: the Configure bar above it names two screens, and a screen that
          says what it is beats a shell guessing on its behalf. */}
      <SectionHeader id="set-accounts" title="Accounts we watch">
        The Instagram accounts we read to see what is working for other people.
        Add them by name, or let the AI find them. Instagram only lets us read
        Business and Creator accounts.
      </SectionHeader>

      {/* Controls ----------------------------------------------------------
          Two groups on one row. Left: find a row. Right: change the list. The
          spacer between them is also what keeps the heights honest: Filters
          is the height of the search box and the select it sits beside, the
          three actions are a size up, and the gap means the two sizes are
          never adjacent. */}
      <div className="card space-y-3 p-4">
        <Toolbar>
          <SearchField
            value={q}
            onChange={setQ}
            onClear={() => setQ("")}
            inputRef={searchRef}
            placeholder="Handle, name or category…"
            className="min-w-[13rem] flex-1"
          />

          {/* Sort stays in the open row: its current value is the answer to
              the first question anyone asks of a list, which is why it is in
              this order. The label is visible rather than an aria-label; a
              bare select in a toolbar is a guess. */}
          <label className="inline-flex items-center gap-1.5">
            <span className="eyebrow">Sort</span>
            <select
              value={sort}
              onChange={(e) => setSort(e.target.value as SortKey)}
              className={SELECT_CLS}
            >
              {SORTS.map((s) => (
                <option key={s.value} value={s.value}>
                  {s.label}
                </option>
              ))}
            </select>
          </label>

          <FilterToggle
            open={filtersOpen}
            count={filterCount}
            onClick={() => setFiltersOpen((v) => !v)}
            panelId={filterPanelId}
            title="Region, who added the account, and how its last scan went"
          />

          <ToolbarSpacer />

          <button
            ref={addBtnRef}
            type="button"
            onClick={() => (addOpen ? closeAdd() : openAdd())}
            aria-expanded={addOpen}
            // Only while the panel exists: aria-controls pointing at nothing is
            // a broken promise to a screen reader.
            aria-controls={addOpen ? addPanelId : undefined}
            className={`btn ${addOpen ? "btn-accent" : "btn-ghost"}`}
          >
            <Plus className="h-4 w-4" aria-hidden />
            Add account
          </button>
          <button
            ref={researchBtnRef}
            type="button"
            onClick={() => (researchOpen ? closeResearch() : openResearch())}
            aria-expanded={researchOpen}
            aria-controls={researchOpen ? researchPanelId : undefined}
            className={`btn ${researchOpen ? "btn-accent" : "btn-ghost"}`}
          >
            <Search className="h-4 w-4" aria-hidden />
            Find accounts with AI
          </button>
          {/* The ceiling, then the button, in that reading order: the number
              is part of this decision, so it is settled before the thing that
              acts on it. "Up to" is doing real work in that label. A bare
              number beside a scan button reads as a promise, and then an
              account with nine recent posts looks like a failure. It is a
              ceiling: we stop there, and fewer is the normal answer. */}
          <label
            htmlFor="scan-cap"
            className="text-xs text-muted"
            title="We stop at this many per account. Accounts that have posted less come back with less, which is normal."
          >
            Up to
          </label>
          <select
            id="scan-cap"
            value={String(postCap)}
            onChange={(e) => setPostCap(Number(e.target.value))}
            disabled={scanning}
            className={SELECT_CLS}
          >
            {POST_CAPS.map((n) => (
              <option key={n} value={String(n)}>
                {n} posts each
              </option>
            ))}
          </select>
          <button
            type="button"
            onClick={() => void startScan()}
            disabled={scanning || active.length === 0}
            title={
              active.length === 0
                ? "Add at least one account first"
                : `Read the newest ${postCap} posts from every account we watch`
            }
            className="btn btn-primary"
          >
            <BtnIcon busy={scanning} Icon={RadioTower} className="h-4 w-4" />
            {scanning ? "Reading…" : "Read new posts"}
          </button>
        </Toolbar>

        {/* Chips: every filter the panel is hiding, and its undo. The search
            term has no chip because the box it is written in never leaves the
            screen, and it already carries its own clear button. */}
        {filterCount > 0 && (
          <ChipRow>
            {region && (
              <FilterChip
                label="Region"
                value={region === "none" ? "Not set" : regionLabel(region)}
                onRemove={() => setRegion("")}
              />
            )}
            {source && (
              <FilterChip
                label="Added"
                value={SOURCE_LABEL[source]}
                onRemove={() => setSource("")}
              />
            )}
            {health && (
              <FilterChip
                label="Last scan"
                value={HEALTH_LABEL[health]}
                onRemove={() => setHealth("")}
              />
            )}
          </ChipRow>
        )}

        {/* The panel. Shut on arrival, and out of the tab order while shut. */}
        <FilterPanel
          id={filterPanelId}
          open={filtersOpen}
          footer={
            filterCount > 0 ? (
              <button type="button" onClick={clearFilters} className="btn btn-ghost btn-sm">
                <RotateCcw className="h-3.5 w-3.5" aria-hidden />
                Clear filters
              </button>
            ) : undefined
          }
        >
          <Field label="Region">
            <select
              value={region}
              onChange={(e) => setRegion(e.target.value as RegionFilter)}
              className={`${SELECT_CLS} w-full`}
            >
              <option value="">Any region</option>
              <option value="uae">UAE</option>
              <option value="gcc">GCC</option>
              <option value="international">International</option>
              <option value="none">Not set</option>
            </select>
          </Field>
          <Field label="Added by">
            <select
              value={source}
              onChange={(e) => setSource(e.target.value as SourceFilter)}
              className={`${SELECT_CLS} w-full`}
            >
              <option value="">Anyone</option>
              <option value="manual">{SOURCE_LABEL.manual}</option>
              <option value="ai_research">{SOURCE_LABEL.ai_research}</option>
            </select>
          </Field>
          <Field label="Last scan">
            <select
              value={health}
              onChange={(e) => setHealth(e.target.value as HealthFilter)}
              className={`${SELECT_CLS} w-full`}
            >
              <option value="">Any outcome</option>
              <option value="never">{HEALTH_LABEL.never}</option>
              <option value="failed">{HEALTH_LABEL.failed}</option>
              <option value="empty">{HEALTH_LABEL.empty}</option>
              <option value="ok">{HEALTH_LABEL.ok}</option>
            </select>
          </Field>
        </FilterPanel>
      </div>

      {/* A list that failed to load is a dead end unless it offers a way back,
          so the retry sits with the message rather than in a menu. */}
      {error && (
        <div className="space-y-2">
          <ErrorCard message={error} />
          <button
            type="button"
            onClick={() => void retry()}
            disabled={retrying}
            className="btn btn-ghost btn-sm"
          >
            <BtnIcon busy={retrying} Icon={RefreshCw} />
            {retrying ? "Loading…" : "Try again"}
          </button>
        </div>
      )}

      {/* The scanner, while it is running or after it has failed. The page
          shell draws the same thing on every other route and leaves this one
          alone, so there is exactly one of these on screen wherever the reader
          is. How a finished scan went is a line under the Tracked heading, not
          a banner: a permanent Notice above the list is read once and then
          trained out, and it takes the running and failed ones with it. */}
      {scan && <ScanNotice scan={scan} />}

      {addOpen && (
        <AddCompetitorForm
          id={addPanelId}
          onClose={closeAdd}
          onAdded={(c) => {
            onAdded(c);
            closeAdd();
          }}
        />
      )}

      {researchOpen && (
        <ResearchPanel
          id={researchPanelId}
          onClose={closeResearch}
          onAdded={onAdded}
          knownUsernames={(items ?? []).map((c) => c.username.toLowerCase())}
        />
      )}

      {/* Tracked ---------------------------------------------------------- */}
      <section aria-labelledby={trackedTitleId} className="space-y-2">
        <div className="flex flex-wrap items-baseline gap-x-2">
          <h2 id={trackedTitleId} className="text-sm font-semibold text-fg">
            Tracked
          </h2>
          {/* The one announced number on the page, and the one that says how
              much of the list the controls above are hiding. It renders a
              line before the first fetch lands, so the heading row does not
              re-flow when the count appears. */}
          <ResultCount
            loading={firstLoad}
            shown={visibleActive.length}
            total={active.length}
            noun="account"
          />
        </div>
        <p className="text-xs text-muted">
          Every scan reads these
          {autoHours > 0 ? `, and one runs on its own every ${everyPhrase(autoHours)}.` : "."}
          {settledScan &&
            ` Last scan ${settledWhen ? fmtRel(settledWhen) : "finished"}: ${
              settledScan.posts_found ?? 0
            } posts found, ${settledScan.posts_scored ?? 0} scored.`}
        </p>

        {firstLoad ? (
          <div className="card divide-y divide-border">
            {Array.from({ length: 4 }).map((_, i) => (
              <div key={i} className="flex items-center gap-3 p-4">
                <Skeleton h="h-10" w="w-10" rounded="rounded-full" />
                <div className="flex-1 space-y-2">
                  <Skeleton h="h-3.5" className="w-40" />
                  <Skeleton h="h-3" className="w-56 max-w-full" />
                </div>
                <Skeleton h="h-9" className="w-24" />
                <Skeleton h="h-7" className="w-32" />
              </div>
            ))}
          </div>
        ) : active.length === 0 ? (
          // A list that failed to load is not an empty list, so the teaching
          // empty state is withheld while the error card is up. "No accounts
          // tracked yet" under "Couldn't load competitors" is a claim this
          // screen cannot make.
          error ? null : (
            <EmptyState
              title="No accounts tracked yet"
              body={
                <>
                  Add accounts by name, or let the AI find them for you. The ones
                  worth watching are those whose audience overlaps with 42's:
                  universities, coding bootcamps, tech communities, local youth
                  brands.
                  {excluded.length > 0 && (
                    <>
                      {" "}
                      {excluded.length} excluded account
                      {excluded.length === 1 ? " is" : "s are"} listed below, and
                      Include puts one back.
                    </>
                  )}
                </>
              }
              action={
                <>
                  <button type="button" className="btn btn-primary" onClick={openAdd}>
                    <Plus className="h-4 w-4" aria-hidden />
                    Add account
                  </button>
                  <button type="button" className="btn btn-ghost" onClick={openResearch}>
                    <Search className="h-4 w-4" aria-hidden />
                    Find accounts with AI
                  </button>
                </>
              }
            />
          )
        ) : visibleActive.length === 0 ? (
          // Every account is hidden by the controls above, which is a different
          // sentence from "there are none": the way out is a chip, not a form.
          <EmptyState
            title="No tracked accounts match"
            body="Every filter that is hiding accounts is a chip above, and each chip removes itself."
            action={
              <button type="button" className="btn btn-ghost" onClick={clearAll}>
                <RotateCcw className="h-4 w-4" aria-hidden />
                Clear search and filters
              </button>
            }
          />
        ) : (
          // No `overflow-hidden` on a list whose rows hold a kebab. The menu is
          // absolutely positioned inside the row, so a clipping ancestor cuts
          // it off — on the last row that means the note under Delete is half
          // there. The rows have no background of their own, so the card's
          // rounded corners survive without it, and every text node in a row is
          // already truncated or clamped, which is what the clip was insuring
          // against.
          <div className="card divide-y divide-border">
            {visibleActive.map((c) => (
              <CompetitorRow
                key={c.id}
                competitor={c}
                benchmark={benchmark}
                busy={busyRows[c.id]}
                scanning={scanning}
                highlight={c.id === justAdded}
                onScan={() => void startScan(c.id)}
                onToggle={() => void setStatus(c, "excluded")}
                onSave={(patch) => saveDetails(c, patch)}
                onDelete={() => void remove(c)}
              />
            ))}
          </div>
        )}
      </section>

      {/* Excluded: de-emphasised, folded away, but always reversible ------- */}
      {excluded.length > 0 && (
        <section aria-labelledby={excludedTitleId} className="space-y-2 pt-1">
          <h2 id={excludedTitleId}>
            <button
              type="button"
              onClick={() => setExcludedOpen((v) => !v)}
              aria-expanded={excludedOpen}
              aria-controls={excludedListId}
              className="-mx-1 flex items-center gap-2 rounded px-1 py-1 text-sm font-semibold text-fg transition-colors hover:text-accent focus:outline-none focus-visible:ring-2 focus-visible:ring-accent/40"
            >
              {excludedOpen ? (
                <ChevronDown className="h-4 w-4 text-muted" aria-hidden />
              ) : (
                <ChevronRight className="h-4 w-4 text-muted" aria-hidden />
              )}
              Excluded
              {/* "2 of 5" once the controls above are hiding some: the fold may
                  hide the rows, but it must not hide that a filter is. */}
              <span className="font-mono text-2xs font-normal tabular-nums text-muted">
                {excludedCount}
              </span>
            </button>
          </h2>
          {/* `hidden` rather than unmounted, so `aria-controls` above always
              resolves to a real element. */}
          <div id={excludedListId} hidden={!excludedOpen} className="space-y-2">
            <p className="text-xs text-muted">
              Skipped by every scan. Posts already collected stay in the feed,
              and Include puts an account back.
            </p>
            {visibleExcluded.length === 0 ? (
              <p className="text-xs text-muted">
                None of them match the search or filters above.
              </p>
            ) : (
              // A real step down from the tracked list's white. The previous
              // surface was 1.5% darker, which is a difference a screen cannot
              // show and a reader cannot see. Not clipped, for the same reason
              // as the tracked list above: these rows carry a kebab.
              <div className="card divide-y divide-border bg-bg-muted">
                {visibleExcluded.map((c) => (
                  <CompetitorRow
                    key={c.id}
                    competitor={c}
                    benchmark={benchmark}
                    busy={busyRows[c.id]}
                    scanning={scanning}
                    excludedStyle
                    onToggle={() => void setStatus(c, "active")}
                    onSave={(patch) => saveDetails(c, patch)}
                    onDelete={() => void remove(c)}
                  />
                ))}
              </div>
            )}
          </div>
        </section>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Row
// ---------------------------------------------------------------------------

/**
 * One tracked or excluded account.
 *
 * Three type weights, on purpose, so a list of these can be triaged by eye:
 * the handle at text-sm semibold, the posts-found numeral at text-lg bold in
 * its pill, and everything else at text-xs muted. Before this every number on
 * the row was the same weight as its label, so "Followers 12.4K" and "Region
 * UAE" were typographically identical and nothing ranked.
 *
 * Two layouts. From `sm` up, avatar / identity / hero-and-actions sit on one
 * line. Below it the hero-and-actions cluster drops to a line of its own and
 * the identity column gets the whole width; the avatar stays beside the name
 * at every width. It used to be one wrapping row whose identity column was
 * `flex-1 min-w-0`, which has a flex-basis of zero, so at phone widths the
 * three fixed-width buttons kept the width and the handle was squeezed into
 * ~20px and clipped by the card.
 */
function CompetitorRow({
  competitor: c,
  benchmark,
  busy,
  scanning,
  excludedStyle = false,
  highlight = false,
  onScan,
  onToggle,
  onDelete,
  onSave,
}: {
  competitor: TrendCompetitor;
  /** The tracked list's median posts_found; what the hero pill bands against. */
  benchmark: number;
  /** Which of this row's requests is in flight, if any. */
  busy?: RowBusy;
  scanning: boolean;
  excludedStyle?: boolean;
  /** Just added: a one-shot entrance and a pill, so it can be found in a
   *  sorted list that did not put it on top. */
  highlight?: boolean;
  onScan?: () => void;
  onToggle: () => void;
  /** Save the three fields a person can change after adding an account. */
  onSave: (patch: {
    region: TrendRegion | null;
    category: string | null;
    why: string | null;
  }) => Promise<void>;
  onDelete: () => void;
}) {
  const working = busy != null;
  const health = scanHealth(c);
  const posts = c.posts_found ?? 0;
  const [editing, setEditing] = useState(false);
  return (
    // No row-wide dimming while a request runs: text-muted on white is 4.87:1
    // and at 60% opacity falls under AA for the whole of every exclude and
    // delete. `aria-busy` on the row and the spinner on the pressed button
    // already say what is happening.
    <div
      className={`p-4 ${highlight ? "animate-riseFade" : ""}`}
      aria-busy={working || undefined}
    >
      <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:gap-x-4">
        <div className="flex min-w-0 flex-1 gap-3">
          <span className={excludedStyle ? "opacity-70" : undefined}>
            <Avatar src={c.profile_picture_url} name={c.username} size={40} />
          </span>

          <div className="min-w-0 flex-1">
            <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
              <a
                href={`https://instagram.com/${c.username}`}
                target="_blank"
                rel="noopener noreferrer"
                className="min-w-0 max-w-full truncate rounded text-sm font-semibold text-fg hover:text-accent focus:outline-none focus-visible:ring-2 focus-visible:ring-accent/40"
              >
                @{c.username}
              </a>
              {c.display_name && (
                <span className="min-w-0 truncate text-xs text-muted">
                  {c.display_name}
                </span>
              )}
              {/* Spelled out rather than "ai": the badge has room, and a reader
                  meeting it for the first time should not have to guess. */}
              {c.source === "ai_research" && (
                <span className="pill pill-uploaded">Found by AI</span>
              )}
              {highlight && <span className="pill pill-started">Just added</span>}
              {excludedStyle && <span className="pill pill-skipped">Excluded</span>}
              {/* The one scan outcome that needs the reader: never-scanned and
                  found-nothing are already said by the hero pill ("—" against
                  "0") and by Last scan on the line below, and a chip on every
                  row is a row of noise. The detail strip under the row carries
                  the reason. */}
              {health === "failed" && (
                <span className="pill pill-failed">Scan failed</span>
              )}
            </div>
            {c.why && (
              <p className="mt-1 line-clamp-2 text-xs text-muted" title={c.why}>
                {c.why}
              </p>
            )}
            <dl className="mt-2 flex flex-wrap items-baseline gap-x-4 gap-y-1">
              <RowStat label="Followers" value={fmtCompact(c.followers_count)} />
              {/* The account's own baseline, which is what every score in the
                  feed is measured against. Fetched all along; never shown. */}
              {c.median_engagement != null && (
                <RowStat
                  label="Median engagement"
                  value={fmtCompact(c.median_engagement)}
                />
              )}
              <RowStat label="Region" value={regionLabel(c.region)} />
              {/* Optional fields are omitted rather than shown as a blank: a
                  metadata line reading "Category -" is a row of noise. */}
              {c.category && <RowStat label="Category" value={c.category} />}
              <RowStat
                label="Last scan"
                value={c.last_scanned_at ? fmtRel(c.last_scanned_at) : "Never"}
              />
            </dl>
          </div>
        </div>

        <div className="flex flex-wrap items-center justify-between gap-3 sm:shrink-0 sm:justify-start">
          <MetricPill
            value={c.last_scanned_at ? posts : null}
            benchmark={benchmark}
            subdued={excludedStyle}
            title={
              c.last_scanned_at
                ? `${posts} post${posts === 1 ? "" : "s"} collected by the last scan. Strong means at least twice the typical tracked account.`
                : "Not scanned yet, so there is nothing to count"
            }
          />

          <div className="flex items-center gap-1.5">
            {onScan && (
              <button
                type="button"
                onClick={onScan}
                disabled={working || scanning}
                title={scanning ? "A scan is already running" : "Scan just this account"}
                className="btn btn-ghost btn-xs"
              >
                <RefreshCw className="h-3.5 w-3.5" aria-hidden />
                Scan
              </button>
            )}
            <button
              type="button"
              onClick={onToggle}
              disabled={working}
              aria-busy={busy === "status" || undefined}
              title={
                excludedStyle
                  ? "Put this account back into every scan"
                  : "Stop scanning this account. Posts already collected stay in the feed."
              }
              className={`btn btn-xs ${excludedStyle ? "btn-accent" : "btn-ghost"}`}
            >
              <BtnIcon busy={busy === "status"} Icon={excludedStyle ? Undo2 : EyeOff} />
              {excludedStyle ? "Include" : "Exclude"}
            </button>
            {/* Delete's own progress. Its button lives in the menu, which is
                shut by the time the confirm dialog has been answered, so the
                spinner has to be drawn out here where the reader is looking. */}
            {busy === "delete" && (
              <span
                role="status"
                className="inline-flex items-center gap-1.5 font-mono text-2xs text-muted"
              >
                <Loader2 className="h-3.5 w-3.5 animate-spin" aria-hidden />
                Deleting…
              </span>
            )}
            {/* Real but rare, and the one irreversible thing on the row. In the
                menu it stops being an unlabelled trash icon a finger's width
                from Exclude, and the note is the Exclude-versus-Delete
                distinction that until now lived only inside the confirm. */}
            <OverflowMenu label={`More actions for @${c.username}`} size="sm">
              {/* Region, category and why could only ever be set while ADDING
                  an account, even though the endpoint has always accepted a
                  change to all three. So a typo, or a second thought about why
                  an account is worth watching, meant deleting it and adding it
                  again, which throws away every post already collected. */}
              <MenuItem Icon={Pencil} onClick={() => setEditing(true)} disabled={working}>
                Edit these details
              </MenuItem>
              <MenuItem Icon={Trash2} onClick={onDelete} disabled={working}>
                Delete account and its posts
              </MenuItem>
              <MenuNote>
                Exclude keeps the posts already collected and only stops future
                scans. Delete removes the account and every post of theirs.
              </MenuNote>
            </OverflowMenu>
          </div>
        </div>
      </div>

      {editing && (
        <EditCompetitorForm
          competitor={c}
          saving={busy === "edit"}
          onCancel={() => setEditing(false)}
          onSave={async (patch) => {
            await onSave(patch);
            setEditing(false);
          }}
        />
      )}

      {/* The icon and the word "failed" both carry the meaning, so the red is
          reinforcement and never the only signal (`color-not-only`). */}
      {c.last_scan_error && (
        <p className="mt-2 flex items-start gap-1.5 rounded bg-danger/5 px-2 py-1.5 text-xs text-danger">
          <AlertTriangle className="mt-px h-3.5 w-3.5 shrink-0" aria-hidden />
          <span>Last scan failed: {c.last_scan_error}</span>
        </p>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Scan status
// ---------------------------------------------------------------------------

/**
 * One line for a scanner that is running, or one that failed.
 *
 * A finished scan gets nothing here: it is a sentence under the Tracked
 * heading instead, because a permanent Notice above the list is read once and
 * then trained out, and it trained out the two states that share its slot and
 * actually need reading. A failed scan is the only one that interrupts;
 * `Notice` picks that role from its tone.
 */
function ScanNotice({ scan }: { scan: TrendScan }) {
  if (scan.active) {
    const total = scan.competitors_total ?? 0;
    const done = scan.competitors_done ?? 0;
    const pct = total > 0 ? Math.min(100, Math.round((done / total) * 100)) : 0;
    return (
      <Notice tone="info" Icon={SpinnerIcon}>
        <div className="flex flex-wrap items-baseline gap-x-2">
          {/* The backend writes this sentence; show it verbatim. */}
          <span className="font-medium">{scan.progress || `Scan ${scan.status}…`}</span>
          {total > 0 && (
            <span className="font-mono tabular-nums text-muted">
              {done}/{total} accounts
            </span>
          )}
        </div>
        {/* The bar the page shell draws on every other route, so this one
            carries everything that one does and can stand in for it. */}
        {total > 0 && (
          <div
            className="bar mt-2"
            role="progressbar"
            aria-valuemin={0}
            aria-valuemax={100}
            aria-valuenow={pct}
            aria-label="Accounts scanned"
          >
            <div style={{ width: `${pct}%` }} />
          </div>
        )}
      </Notice>
    );
  }

  if (scan.status === "failed") {
    return (
      <Notice tone="danger" Icon={AlertTriangle}>
        <span className="font-medium">Last scan failed.</span>{" "}
        {scan.error || "No reason was reported."}
      </Notice>
    );
  }

  return null;
}

// ---------------------------------------------------------------------------
// Add an account
// ---------------------------------------------------------------------------

const REGION_OPTIONS: Array<{ value: "" | TrendRegion; label: string }> = [
  // "" is sent as null. The visible label now sits above the select, so the
  // first option names the empty value instead of repeating the label.
  { value: "", label: "Not set" },
  { value: "uae", label: "UAE" },
  { value: "gcc", label: "GCC" },
  { value: "international", label: "International" },
];

/**
 * The three fields that were add-only until now.
 *
 * NOT THE HANDLE. An account's handle is its identity: `trend_competitors`
 * keys on it, every stored post carries it, and the add form checks it against
 * Instagram before saving. Letting it be edited in place would quietly
 * re-point a row at a different account while keeping the other one's posts.
 * Changing who you watch is still add plus delete, deliberately.
 *
 * Opens inside the row rather than in a dialog: the reader pressed Edit on a
 * specific line and the fields belong under that line, next to the handle they
 * describe.
 */
function EditCompetitorForm({
  competitor: c,
  saving,
  onSave,
  onCancel,
}: {
  competitor: TrendCompetitor;
  saving: boolean;
  onSave: (patch: {
    region: TrendRegion | null;
    category: string | null;
    why: string | null;
  }) => Promise<void>;
  onCancel: () => void;
}) {
  const [region, setRegion] = useState<"" | TrendRegion>(
    (c.region as TrendRegion | null) ?? "",
  );
  const [category, setCategory] = useState(c.category ?? "");
  const [why, setWhy] = useState(c.why ?? "");

  return (
    <form
      className="mt-3 grid gap-3 rounded-lg border border-border bg-bg-subtle p-3 sm:grid-cols-3"
      onSubmit={(e) => {
        e.preventDefault();
        // Null, not "", for an emptied box. The route sends only the fields it
        // was given and the store now writes a null through, so clearing
        // Category here actually clears the column. It did not used to: the
        // store dropped every null, so Save reported "Updated @handle" and
        // changed nothing, which is the worst shape a bug can take.
        void onSave({
          region: region || null,
          category: category.trim() || null,
          why: why.trim() || null,
        });
      }}
    >
      <Field label="Region">
        <select
          value={region}
          onChange={(e) => setRegion(e.target.value as "" | TrendRegion)}
          className={`${SELECT_CLS} w-full`}
        >
          {REGION_OPTIONS.map((o) => (
            <option key={o.value} value={o.value}>
              {o.label}
            </option>
          ))}
        </select>
      </Field>

      <Field label="Category">
        <input
          value={category}
          onChange={(e) => setCategory(e.target.value)}
          placeholder="university, bootcamp"
          className={`${INPUT_CLS} w-full`}
        />
      </Field>

      <Field label="Why track them">
        <input
          value={why}
          onChange={(e) => setWhy(e.target.value)}
          placeholder="Same student-age audience, strong reels"
          className={`${INPUT_CLS} w-full`}
        />
      </Field>

      <div className="flex items-center gap-2 sm:col-span-3">
        <button type="submit" disabled={saving} className="btn btn-primary btn-xs">
          {saving ? "Saving…" : "Save"}
        </button>
        <button
          type="button"
          onClick={onCancel}
          disabled={saving}
          className="btn btn-ghost btn-xs"
        >
          Cancel
        </button>
      </div>
    </form>
  );
}

function AddCompetitorForm({
  id,
  onClose,
  onAdded,
}: {
  /** Owned by the toolbar button's `aria-controls`. */
  id: string;
  onClose: () => void;
  onAdded: (c: TrendCompetitor) => void;
}) {
  const toast = useToast();
  const [username, setUsername] = useState("");
  const [region, setRegion] = useState<"" | TrendRegion>("");
  const [category, setCategory] = useState("");
  const [why, setWhy] = useState("");
  const [busy, setBusy] = useState(false);
  /** Which of the two submits is running, so only that button shows progress. */
  const [excluding, setExcluding] = useState(false);
  /** The server's plain-language 422 reason. Guidance, not a crash. */
  const [refusal, setRefusal] = useState<string | null>(null);
  /** Bumped on every refusal, so a second attempt that fails the same way still
   *  moves focus back to the message. */
  const [refusalSeq, setRefusalSeq] = useState(0);
  const inFlight = useRef(false);

  const titleId = useId();
  const hintId = useId();
  const errorId = useId();
  const errorRef = useRef<HTMLDivElement>(null);
  const panelRef = useRef<HTMLFormElement>(null);
  const handleRef = useRef<HTMLInputElement>(null);
  useRevealOnMount(panelRef, handleRef);

  const clean = username.trim().replace(/^@/, "").replace(/\/+$/, "");

  /**
   * A failed submit has to be findable, not just visible. The message sits
   * beside the field that caused it, is wired to that field with
   * aria-describedby, and takes focus once so a keyboard or screen reader user
   * lands ON the reason. That is `focusable-error-summary` minus the summary:
   * with one field there is nothing to summarise, and a list of one item above
   * the form is a second thing to read on the way back to the same input.
   */
  useEffect(() => {
    if (refusalSeq > 0) errorRef.current?.focus();
  }, [refusalSeq]);

  async function submit(exclude = false) {
    if (!clean || inFlight.current) return;
    inFlight.current = true;
    setExcluding(exclude);
    setBusy(true);
    // Cleared only for a fresh attempt. "Never suggest" is fired FROM inside
    // the refusal box, and wiping the box on click would take that button, and
    // the progress it is meant to show, off screen mid-request.
    if (!exclude) setRefusal(null);
    try {
      const res = await trendsApi.addCompetitor({
        username: clean,
        region: region || null,
        category: category.trim() || null,
        why: why.trim() || null,
        ...(exclude ? { exclude: true } : {}),
      });
      onAdded(res.competitor);
      toast.success(
        exclude ? `@${clean} will never be suggested again` : `Tracking @${clean}`,
        exclude ? undefined : "It'll be included in the next scan.",
      );
      setUsername("");
      setWhy("");
      setCategory("");
      setRefusal(null);
    } catch (e) {
      const msg = humanError(e);
      if (/already/i.test(msg)) {
        // A no-op on the normal path, where there is no refusal on screen.
        setRefusal(null);
        toast.info(`@${clean} is already tracked`);
      } else {
        setRefusal(msg);
        setRefusalSeq((n) => n + 1);
      }
    } finally {
      inFlight.current = false;
      setBusy(false);
      setExcluding(false);
    }
  }

  return (
    <form
      ref={panelRef}
      id={id}
      aria-labelledby={titleId}
      onSubmit={(e) => {
        // A real form, so Enter submits from any field rather than only from
        // the handle box. `inFlight` is still the double-submit guard.
        e.preventDefault();
        void submit();
      }}
      className="card space-y-4 p-4"
    >
      <div className="flex items-start justify-between gap-3">
        <h2 id={titleId} className="text-sm font-semibold text-fg">
          Add an account
        </h2>
        <button
          type="button"
          onClick={onClose}
          aria-label="Close the add form"
          className="flex h-6 w-6 shrink-0 items-center justify-center rounded text-muted transition-colors hover:bg-bg-muted hover:text-fg focus:outline-none focus-visible:ring-2 focus-visible:ring-accent/40"
        >
          <X className="h-3.5 w-3.5" aria-hidden />
        </button>
      </div>

      <div className="grid gap-x-4 gap-y-3 sm:grid-cols-2">
        <div className="sm:col-span-2">
          <Field label="Instagram handle">
            <div className="relative">
              <span
                aria-hidden
                className="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-sm text-muted"
              >
                @
              </span>
              <input
                ref={handleRef}
                value={username}
                onChange={(e) => {
                  setUsername(e.target.value);
                  // The refusal names a specific handle. Editing the field
                  // makes it stale, and a stale error beside a changed value is
                  // worse than none.
                  if (refusal) setRefusal(null);
                }}
                onKeyDown={(e) => {
                  if (e.key === "Escape") onClose();
                }}
                placeholder="visitabudhabi"
                autoComplete="off"
                spellCheck={false}
                aria-describedby={refusal ? `${errorId} ${hintId}` : hintId}
                aria-invalid={refusal ? true : undefined}
                className={`${INPUT_CLS} w-full pl-7`}
              />
            </div>
          </Field>

          {/* The ONE place this screen explains the Business/Creator limit: it
              only matters while somebody is typing a handle, and it is wired to
              that field so it is read with it. */}
          <p id={hintId} className="mt-1.5 text-xs leading-relaxed text-muted">
            The handle is checked against Instagram before it is saved.
            Instagram only lets us read <strong>Business</strong> and{" "}
            <strong>Creator</strong> accounts, so a personal account will be
            refused.
          </p>

          {refusal && (
            <div
              ref={errorRef}
              id={errorId}
              tabIndex={-1}
              // A real alert, not a status: this is the answer to a button the
              // reader just pressed. `role="alert"` announces it, the heading
              // line names what failed, and the two buttons are the ways out.
              role="alert"
              className="mt-2 rounded-lg border border-warning/30 bg-warning/10 p-3 focus:outline-none focus:ring-2 focus:ring-accent/40"
            >
              <p className="flex items-center gap-2 text-xs font-medium text-fg">
                <AlertTriangle className="h-3.5 w-3.5 shrink-0 text-warning" aria-hidden />
                Instagram couldn't read @{clean}
              </p>
              <p className="mt-1.5 text-xs leading-relaxed text-muted">{refusal}</p>
              <div className="mt-2.5 flex flex-wrap gap-2">
                <button
                  type="button"
                  onClick={() => void submit(true)}
                  disabled={busy}
                  aria-busy={busy && excluding}
                  title="Files it under Excluded so the AI stops proposing it. You can undo that below."
                  className="btn btn-ghost btn-sm"
                >
                  <BtnIcon busy={busy && excluding} Icon={EyeOff} />
                  {busy && excluding ? "Saving…" : `Never suggest @${clean}`}
                </button>
                <button
                  type="button"
                  onClick={() => setRefusal(null)}
                  className="btn btn-ghost btn-sm"
                >
                  Try another handle
                </button>
              </div>
            </div>
          )}
        </div>

        <Field label="Region (optional)">
          <select
            value={region}
            onChange={(e) => setRegion(e.target.value as "" | TrendRegion)}
            className={`${SELECT_CLS} w-full`}
          >
            {REGION_OPTIONS.map((o) => (
              <option key={o.value} value={o.value}>
                {o.label}
              </option>
            ))}
          </select>
        </Field>

        <Field label="Category (optional)">
          <input
            value={category}
            onChange={(e) => setCategory(e.target.value)}
            placeholder="university, bootcamp"
            className={`${INPUT_CLS} w-full`}
          />
        </Field>

        <Field label="Why track them (optional)" className="sm:col-span-2">
          <input
            value={why}
            onChange={(e) => setWhy(e.target.value)}
            placeholder="Same student-age audience, strong reels"
            className={`${INPUT_CLS} w-full`}
          />
        </Field>
      </div>

      <div className="flex flex-wrap items-center gap-2">
        <button
          type="submit"
          disabled={!clean || busy}
          aria-busy={busy && !excluding}
          className="btn btn-primary"
        >
          <BtnIcon busy={busy && !excluding} Icon={Plus} className="h-4 w-4" />
          {busy && !excluding ? "Checking…" : "Add account"}
        </button>
        <button type="button" onClick={onClose} className="btn btn-ghost">
          Cancel
        </button>
      </div>
    </form>
  );
}

// ---------------------------------------------------------------------------
// Find accounts with AI
// ---------------------------------------------------------------------------

function ResearchPanel({
  id,
  onClose,
  onAdded,
  knownUsernames,
}: {
  /** Owned by the toolbar button's `aria-controls`. */
  id: string;
  onClose: () => void;
  onAdded: (c: TrendCompetitor) => void;
  knownUsernames: string[];
}) {
  const toast = useToast();
  const [hint, setHint] = useState("");
  const [limit, setLimit] = useState(8);
  // Focus dials. Defaults reproduce the endpoint's previous behaviour exactly,
  // so an untouched panel behaves as it always did.
  const [geo, setGeo] = useState<TrendResearchGeo>("uae");
  const [relevance, setRelevance] = useState<TrendResearchRelevance>("normal");
  const [engagement, setEngagement] = useState<TrendResearchEngagement>("any");
  const [size, setSize] = useState<TrendResearchSize>("any");
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<ResearchResult | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [rejectedOpen, setRejectedOpen] = useState(false);
  /** username -> which of its two buttons is running. */
  const [rowBusy, setRowBusy] = useState<Record<string, "add" | "ignore">>({});
  const inFlight = useRef(false);

  const titleId = useId();
  const suggestionsId = useId();
  const rejectedTitleId = useId();
  const rejectedListId = useId();
  const panelRef = useRef<HTMLElement>(null);
  const hintRef = useRef<HTMLInputElement>(null);
  useRevealOnMount(panelRef, hintRef);

  async function run() {
    if (inFlight.current) return; // StrictMode / double-click guard
    inFlight.current = true;
    setBusy(true);
    setError(null);
    try {
      const res = await trendsApi.research({
        ...(hint.trim() ? { hint: hint.trim() } : {}),
        limit,
        geo,
        relevance,
        engagement,
        size,
      });
      setResult(res);
      setRejectedOpen(false);
    } catch (e) {
      setError(humanError(e, "Research failed"));
    } finally {
      inFlight.current = false;
      setBusy(false);
    }
  }

  function dropCandidate(username: string) {
    setResult((prev) =>
      prev
        ? {
            ...prev,
            candidates: prev.candidates.filter((c) => c.username !== username),
          }
        : prev,
    );
  }

  async function act(candidate: ResearchCandidate, exclude: boolean) {
    const u = candidate.username;
    setRowBusy((prev) => ({ ...prev, [u]: exclude ? "ignore" : "add" }));
    try {
      const res = await trendsApi.addCompetitor({
        username: u,
        region: candidate.region ?? null,
        category: candidate.category ?? null,
        why: candidate.why ?? null,
        ...(exclude ? { exclude: true } : {}),
      });
      if (!exclude) onAdded(res.competitor);
      dropCandidate(u);
      toast.info(exclude ? `@${u} won't be suggested again` : `Tracking @${u}`);
    } catch (e) {
      const msg = humanError(e);
      if (/already/i.test(msg)) {
        dropCandidate(u);
        toast.info(`@${u} is already on your list`);
      } else {
        toast.error(`Couldn't add @${u}`, msg);
      }
    } finally {
      setRowBusy((prev) => {
        const next = { ...prev };
        delete next[u];
        return next;
      });
    }
  }

  const candidates = result?.candidates ?? [];
  const rejected = result?.rejected ?? [];

  return (
    <section ref={panelRef} id={id} aria-labelledby={titleId} className="card space-y-4 p-4">
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <h2 id={titleId} className="text-sm font-semibold text-fg">
            Find accounts with AI
          </h2>
          <p className="mt-1 text-xs leading-relaxed text-muted">
            Describe the kind of account you want. Every suggestion is checked
            against Instagram first, so what comes back can actually be scanned.
          </p>
        </div>
        <button
          type="button"
          onClick={onClose}
          aria-label="Close the research panel"
          className="flex h-6 w-6 shrink-0 items-center justify-center rounded text-muted transition-colors hover:bg-bg-muted hover:text-fg focus:outline-none focus-visible:ring-2 focus-visible:ring-accent/40"
        >
          <X className="h-3.5 w-3.5" aria-hidden />
        </button>
      </div>

      {/* The search itself is a form, so Enter runs it from any field. */}
      <form
        onSubmit={(e) => {
          e.preventDefault();
          void run();
        }}
        className="space-y-4"
      >
        <div className="grid gap-x-4 gap-y-3 sm:grid-cols-4">
          <Field label="What kind of accounts?" className="sm:col-span-3">
            <input
              ref={hintRef}
              value={hint}
              onChange={(e) => setHint(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Escape") onClose();
              }}
              placeholder="Coding schools and tech communities in the Gulf"
              className={`${INPUT_CLS} w-full`}
            />
          </Field>
          <Field label="How many">
            <select
              value={String(limit)}
              onChange={(e) => setLimit(Number(e.target.value))}
              className={`${SELECT_CLS} w-full`}
            >
              {/* The endpoint caps `limit` at 12, so offering more would 422. */}
              {[5, 8, 12].map((n) => (
                <option key={n} value={n}>
                  {n}
                </option>
              ))}
            </select>
          </Field>
        </div>

        {/* These four steer WHICH accounts the model proposes. They are not the
            feed's opportunity weights, which re-rank posts that already exist.
            Grouped in a fieldset so they read as one decision, not four, on
            the same surface as the filter panel above, since it is the same
            kind of thing: secondary controls, one step in from the row. */}
        <fieldset className="rounded-xl border border-border bg-bg-muted p-4">
          <legend className="eyebrow px-1">Narrow the search</legend>
          <div className="grid gap-x-4 gap-y-3 sm:grid-cols-2 lg:grid-cols-4">
            <Field label="Where">
              <select
                value={geo}
                onChange={(e) => setGeo(e.target.value as TrendResearchGeo)}
                className={`${SELECT_CLS} w-full`}
              >
                <option value="uae">UAE &amp; Gulf</option>
                <option value="global">Worldwide</option>
              </select>
            </Field>
            <Field label="Relevance to 42">
              <select
                value={relevance}
                onChange={(e) => setRelevance(e.target.value as TrendResearchRelevance)}
                className={`${SELECT_CLS} w-full`}
              >
                <option value="normal">Broad</option>
                <option value="high">Only 42's world</option>
              </select>
            </Field>
            <Field label="Engagement">
              <select
                value={engagement}
                onChange={(e) => setEngagement(e.target.value as TrendResearchEngagement)}
                className={`${SELECT_CLS} w-full`}
              >
                <option value="any">Any</option>
                <option value="high">Lots of likes &amp; comments</option>
              </select>
            </Field>
            <Field label="Account size">
              <select
                value={size}
                onChange={(e) => setSize(e.target.value as TrendResearchSize)}
                className={`${SELECT_CLS} w-full`}
              >
                <option value="any">Any size</option>
                <option value="small">Small (under 150k)</option>
                <option value="large">Large (150k+)</option>
              </select>
            </Field>
          </div>
          <p className="mt-3 text-xs leading-relaxed text-muted">
            {geo === "global"
              ? "Searching worldwide, with no UAE quota."
              : "Weighted toward UAE and Gulf accounts."}
          </p>
        </fieldset>

        <button
          type="submit"
          disabled={busy}
          aria-busy={busy}
          className="btn btn-primary"
        >
          <BtnIcon busy={busy} Icon={Search} className="h-4 w-4" />
          {busy ? "Researching…" : "Find accounts"}
        </button>
      </form>

      {error && <ErrorCard message={error} />}

      {busy && (
        <div className="space-y-2">
          {Array.from({ length: 3 }).map((_, i) => (
            <Skeleton key={i} h="h-14" className="w-full" rounded="rounded-lg" />
          ))}
        </div>
      )}

      {result && !busy && (
        <div className="space-y-4">
          {/* A Notice, so the result of an async action is announced rather than
              appearing silently under the button that caused it. The wording
              stays true as rows are decided: "still to decide" counts down. */}
          <Notice tone="info">
            {result.tried} suggested, {candidates.length} still to decide
            {rejected.length > 0
              ? `, ${rejected.length} couldn't be read`
              : ""}
            .{result.note ? ` ${result.note}` : ""}
          </Notice>

          {candidates.length === 0 && rejected.length === 0 && (
            <p className="text-sm text-muted">
              Nothing came back. Try a broader description, or set Where to
              Worldwide.
            </p>
          )}

          {candidates.length > 0 && (
            <section aria-labelledby={suggestionsId} className="space-y-2">
              <div className="flex flex-wrap items-baseline gap-x-2">
                <h3 id={suggestionsId} className="text-sm font-semibold text-fg">
                  Suggestions
                </h3>
                <span className="font-mono text-2xs tabular-nums text-muted">
                  {candidates.length}
                </span>
              </div>
              <p className="text-xs text-muted">
                Add puts an account into the next scan. Never suggest files it
                under Excluded instead, so it stops coming back.
              </p>
              {/* The same surface as the tracked list: it is the same idea, a
                  list of account rows, and it sits an inch above that list. */}
              <div className="card divide-y divide-border overflow-hidden">
                {candidates.map((c) => (
                  <CandidateRow
                    key={c.username}
                    candidate={c}
                    already={knownUsernames.includes(c.username.toLowerCase())}
                    busy={rowBusy[c.username]}
                    onAdd={() => void act(c, false)}
                    onIgnore={() => void act(c, true)}
                  />
                ))}
              </div>
            </section>
          )}

          {rejected.length > 0 && (
            <section aria-labelledby={rejectedTitleId} className="space-y-2">
              <h3 id={rejectedTitleId}>
                <button
                  type="button"
                  onClick={() => setRejectedOpen((v) => !v)}
                  aria-expanded={rejectedOpen}
                  aria-controls={rejectedListId}
                  className="-mx-1 flex items-center gap-2 rounded px-1 py-1 text-sm font-semibold text-fg transition-colors hover:text-accent focus:outline-none focus-visible:ring-2 focus-visible:ring-accent/40"
                >
                  {rejectedOpen ? (
                    <ChevronDown className="h-4 w-4 text-muted" aria-hidden />
                  ) : (
                    <ChevronRight className="h-4 w-4 text-muted" aria-hidden />
                  )}
                  Couldn't be read
                  <span className="font-mono text-2xs font-normal tabular-nums text-muted">
                    {rejected.length}
                  </span>
                </button>
              </h3>
              <ul
                id={rejectedListId}
                hidden={!rejectedOpen}
                className="space-y-1 rounded-lg border border-border bg-bg-muted px-3 py-2"
              >
                {rejected.map((r) => (
                  <li key={r.username} className="text-xs leading-relaxed text-muted">
                    <span className="font-mono text-fg/80">@{r.username}</span>:{" "}
                    {r.error}
                  </li>
                ))}
              </ul>
            </section>
          )}
        </div>
      )}
    </section>
  );
}

/** One AI suggestion, and the two decisions available on it. */
function CandidateRow({
  candidate: c,
  already,
  busy,
  onAdd,
  onIgnore,
}: {
  candidate: ResearchCandidate;
  already: boolean;
  busy?: "add" | "ignore";
  onAdd: () => void;
  onIgnore: () => void;
}) {
  const working = busy != null;
  const description = c.why || c.biography || "No description.";
  return (
    // Same two-breakpoint layout as a tracked row, for the same reason; and no
    // row-wide dimming, for the same contrast reason.
    <div
      className="flex flex-col gap-3 p-4 sm:flex-row sm:items-start sm:gap-x-4"
      aria-busy={working || undefined}
    >
      <div className="flex min-w-0 flex-1 gap-3">
        <Avatar src={c.profile_picture_url} name={c.username} size={40} />

        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
            <span className="min-w-0 max-w-full truncate text-sm font-semibold text-fg">
              @{c.username}
            </span>
            {c.display_name && (
              <span className="min-w-0 truncate text-xs text-muted">{c.display_name}</span>
            )}
            {already && <span className="pill pill-uploaded">Already tracked</span>}
          </div>
          {/* Clamped to two lines with the full text in `title`, rather than cut
              with no way to read the rest (`truncation-strategy`). */}
          <p className="mt-1 line-clamp-2 text-xs text-muted" title={description}>
            {description}
          </p>
          <dl className="mt-2 flex flex-wrap items-baseline gap-x-4 gap-y-1">
            <RowStat label="Followers" value={fmtCompact(c.followers_count)} />
            {c.category && <RowStat label="Category" value={c.category} />}
          </dl>
        </div>
      </div>

      <div className="flex items-center gap-1.5 sm:shrink-0">
        <button
          type="button"
          onClick={onAdd}
          disabled={working || already}
          aria-busy={busy === "add" || undefined}
          title={already ? "Already on your list" : "Track this account from the next scan"}
          className="btn btn-primary btn-xs"
        >
          <BtnIcon busy={busy === "add"} Icon={Plus} />
          Add
        </button>
        <button
          type="button"
          onClick={onIgnore}
          disabled={working}
          aria-busy={busy === "ignore" || undefined}
          title="Files it under Excluded so the AI stops proposing it. You can undo that below."
          className="btn btn-ghost btn-xs"
        >
          <BtnIcon busy={busy === "ignore"} Icon={EyeOff} />
          Never suggest
        </button>
      </div>
    </div>
  );
}
