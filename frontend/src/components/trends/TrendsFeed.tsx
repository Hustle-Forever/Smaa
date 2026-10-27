/**
 * The ranked trend feed — the heart of the trend finder.
 *
 * What this screen is for: competitor posts that OVERPERFORMED for their own
 * account, ranked by how practical the idea is for 42 Abu Dhabi to make. The
 * outperformance multiple is the hero stat on every card; likes are supporting
 * detail, because a like count means nothing across accounts of different sizes.
 *
 * The tablist also carries channels that are not filters of the ranked list at
 * all, but whole other bodies one click away instead of one tab away:
 *
 *   • Report                → `ReportsPanel` embedded. It used to be the second
 *                             half of a page-level "Feed | Reports" bar above
 *                             this row, which made two rows of tabs stacked on
 *                             each other, and the top one only ever had two
 *                             buttons on it. It is a chip here instead, so the
 *                             section has ONE row of controls.
 *   • Viral Social Trends   → `GeneralTrendsTab` embedded whole. Those rows live
 *                             in a different table with their own scoring, so
 *                             they cannot join the ranked list here.
 *
 * That channel is HIDDEN for now behind `VIRAL_SOCIAL_TRENDS_ENABLED`
 * (featureFlags.ts) while the owner works out what belongs in it. While the
 * flag is off the tab is not rendered, the count it would carry is not
 * fetched, and `channel` can never reach "general". Nothing was removed —
 * every branch below is still compiled and type-checked, and flipping the flag
 * brings the fourth channel back exactly as written.
 *
 * Re-syncs from the server on mount — `PageTransition` remounts tab bodies, so
 * there is no page state to preserve. The one prop, `initialChannel`, is for
 * the legacy `#trends/reports` hash: that URL is in bookmarks from when Reports
 * was a page tab, and it still has to open the report rather than drop the
 * reader on the post list. After mount the channel is local state, exactly like
 * Viral Social Trends: pressing a chip does not touch the hash.
 */
import { RadarPicks } from "./RadarPicks";
import {
  useCallback,
  useEffect,
  useId,
  useMemo,
  useRef,
  useState,
} from "react";
import {
  ArrowUp,
  Bookmark,
  BookmarkCheck,
  ExternalLink,
  FileText,
  Globe,
  Heart,
  Info,
  MessageCircle,
  Newspaper,
  Play,
  RefreshCw,
  RotateCcw,
  Search,
  Sparkles,
  Undo2,
  Users,
  X,
} from "lucide-react";

import { useConfirm } from "../ConfirmProvider";
import { Skeleton } from "../Skeleton";
import { Thumb } from "../Thumb";
import { useToast } from "../ToastProvider";
import { CLOCK_12H } from "../../lib/format";
import { goToTrendsTab, consumePendingTheme, TRENDS_THEME_EVENT } from "./bus";
import { ReportsPanel } from "./ReportsPanel";
import {
  ChipRow,
  FilterChip,
  FilterPanel,
  FilterToggle,
  MenuItem,
  MenuNote,
  OverflowMenu,
  ResultCount,
  SearchField,
  Toolbar,
  ToolbarSpacer,
} from "./controls";
import { CoverTile } from "./CoverTile";
import { VIRAL_SOCIAL_TRENDS_ENABLED } from "./featureFlags";
import { generalTrendsApi } from "./generalTrendsApi";
import { GeneralTrendsTab } from "./GeneralTrendsTab";
import { MakeThisDialog } from "./MakeThisDialog";
import {
  Avatar,
  coverKindOf,
  EmptyState,
  ErrorCard,
  Field,
  fmtCompact,
  FreshnessPill,
  regionLabel,
  SELECT_CLS,
  pastViralOf,
  ViralPill,
  viralOf,
} from "./shared";
import { TrendDetailDialog } from "./TrendDetailDialog";
import { humanError, trendsApi } from "./trendsApi";
import type {
  TrendCompetitor,
  TrendMediaType,
  TrendNewsHealth,
  TrendPost,
  TrendPostStatus,
  TrendRegion,
  TrendSort,
  TrendSource,
  TrendStats,
  TrendTheme,
} from "./types";

const PAGE_SIZE = 36;

/** How a watched account reads in the competitor picker. */
function competitorName(c: TrendCompetitor): string {
  const handle = `@${c.username.replace(/^@/, "")}`;
  const name = (c.display_name || "").trim();
  return name && name.toLowerCase() !== c.username.toLowerCase() ? `${name} (${handle})` : handle;
}

/**
 * The feed keeps each tab's own order and offers no sort control: All and
 * Competitors read best opportunity first, Industry Signal most likely to
 * go viral first (`selectCategory`). In the sort's place the toolbar filters
 * by competitor, which is what people asked for ("only MBZUAI").
 */

/**
 * The two discovery channels, plus a combined view.
 *
 * They share one ranked list because a news story is normalised into the same
 * row shape and scored by the same pass as a competitor post — the category
 * picks which rows to show, not which pipeline produced them.
 *
 * Viral Social Trends are NOT in here: different table, different row shape,
 * different score. Merging them into "All" would mean inventing a cross-table
 * ranking, so they get their own channel below instead.
 */
const CATEGORIES: Array<{
  value: "" | TrendSource;
  label: string;
  hint: string;
  Icon: typeof Newspaper;
}> = [
  {
    value: "",
    label: "All",
    hint: "Everything we found, best first",
    Icon: Sparkles,
  },
  {
    value: "competitor",
    label: "Competitors / Sentiment",
    hint: "Posts that did better than that account usually does",
    Icon: Users,
  },
  {
    value: "news",
    label: "Industry Signal",
    hint: "AI news from around the world and the UAE",
    Icon: Newspaper,
  },
];

/**
 * Which body the tab is showing: this feed's ranked posts, or Viral Social Trends.
 *
 * Deliberately separate from `category`. That state is `"" | TrendSource` and
 * is sent to the API as `source=`, so "general" cannot live in it — and keeping
 * it out means the competitor filters, sort and paging survive a trip to the
 * Viral Social Trends tab exactly as they were left.
 */
type FeedChannel = "posts" | "general" | "report";

/**
 * The report chip. Not a `TrendSource` either: it swaps the body for the
 * written-up document instead of re-filtering the ranked list, so it is
 * described here beside the other body-swapping channel rather than in
 * CATEGORIES.
 *
 * No count. The other chips count rows they are about to list; this one opens a
 * document assembled from all of them, and a number beside it could only repeat
 * whichever total the reader had just read two chips to the left.
 */
const REPORT_TAB = {
  label: "Report",
  hint:
    "Everything we found, written up as one document: read it here, or download it as a PDF",
  Icon: FileText,
};

/**
 * The Viral Social Trends tab. Not a `TrendSource`, so it is described
 * separately and rendered alongside the three above.
 */
const GENERAL_TAB = {
  label: "Viral Social Trends",
  hint:
    "Formats people are copying right now: memes, sounds and challenges from across the internet, not what one competitor posted",
  Icon: Globe,
};

/** The short word stamped on a card's thumbnail. */
const MEDIA_LABELS: Record<TrendMediaType, string> = {
  IMAGE: "Image",
  VIDEO: "Video",
  CAROUSEL_ALBUM: "Carousel",
};

/**
 * The Format filter's options.
 *
 * One list rather than three hand-written `<option>`s, because the chip that
 * appears when a format is chosen has to be labelled with the SAME words the
 * select used. Two copies of that wording is how a chip ends up saying
 * "Video" while the control that set it says "Video / Reel", and a chip that
 * disagrees with its own control is worse than no chip. Deliberately not
 * MEDIA_LABELS: that map is the badge on the thumbnail, where there is room
 * for one word and no room for "/ Reel".
 */
const FORMAT_OPTIONS: Array<{ value: TrendMediaType; label: string }> = [
  { value: "VIDEO", label: "Video / Reel" },
  { value: "IMAGE", label: "Image" },
  { value: "CAROUSEL_ALBUM", label: "Carousel" },
];

function formatLabel(v: TrendMediaType | ""): string {
  return FORMAT_OPTIONS.find((o) => o.value === v)?.label ?? v;
}

const MIN_FIT_OPTIONS = [
  { value: 0, label: "Any fit" },
  { value: 40, label: "Fit 40+" },
  { value: 60, label: "Fit 60+" },
  { value: 75, label: "Fit 75+" },
];

/**
 * The fixed options, plus the current value if it isn't one of them.
 *
 * The news default comes from the server (`min_fit_default`), so re-measuring
 * the threshold could set it to a number this list doesn't contain — and a
 * `<select>` whose value matches no option silently displays the FIRST one.
 * The control would read "Any fit" while the feed was gating at 45.
 */
function minFitOptions(current: number) {
  if (MIN_FIT_OPTIONS.some((o) => o.value === current)) return MIN_FIT_OPTIONS;
  return [...MIN_FIT_OPTIONS, { value: current, label: `Fit ${current}+` }].sort(
    (a, b) => a.value - b.value,
  );
}

/**
 * The fit bar the news category starts at, if the server doesn't say otherwise.
 *
 * The real value is served by `/news/health` so the measured threshold has one
 * definition, in the backend. This is only the fallback for a first paint
 * before that request lands.
 */

/**
 * "active" hides dismissed posts client-side — the API's `status` filter takes
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

/** Same reasoning as `formatLabel`: the chip must quote the control. */
function showLabel(v: ShowMode): string {
  return SHOW_OPTIONS.find((o) => o.value === v)?.label ?? v;
}

export function TrendsFeed({ initialChannel }: { initialChannel?: FeedChannel }) {
  const toast = useToast();
  const confirm = useConfirm();

  // --- filters -------------------------------------------------------------
  // Which discovery channel to show. "" is All — both categories ranked
  // together, which is possible precisely because they share one score.
  const [category, setCategory] = useState<"" | TrendSource>("");
  // Which body is on screen. Separate from `category` on purpose — see
  // `FeedChannel`. "posts" is every category above; "general" swaps the body
  // for the Viral Social Trends tab and touches none of the filters below.
  const [channel, setChannel] = useState<FeedChannel>(initialChannel ?? "posts");
  const [sort, setSort] = useState<TrendSort>("opportunity");
  // One watched account, or "" for all of them. Sent as competitor_id, so the
  // count and paging are the server's, not a filter over one loaded page.
  const [competitorId, setCompetitorId] = useState<number | "">("");
  const [competitors, setCompetitors] = useState<TrendCompetitor[]>([]);
  // From / to (YYYY-MM-DD, inclusive), on the date a post or story went out.
  const [fromDate, setFromDate] = useState("");
  const [toDate, setToDate] = useState("");
  const [region, setRegion] = useState<"" | TrendRegion>("");
  const [mediaType, setMediaType] = useState<"" | TrendMediaType>("");
  const [theme, setTheme] = useState("");
  const [minFit, setMinFit] = useState(0);
  const [show, setShow] = useState<ShowMode>("active");
  const [qInput, setQInput] = useState("");
  const [q, setQ] = useState("");
  // News-only controls.
  const isNews = category === "news";
  // ANDed with the flag deliberately: this is the ONE place that decides
  // whether the body is swapped, so gating it here is what makes "general"
  // unreachable while the channel is hidden. The tab that sets it is not
  // rendered either, but a single choke point beats trusting that every future
  // caller of setChannel remembers the flag.
  const isGeneral = VIRAL_SOCIAL_TRENDS_ENABLED && channel === "general";
  // The report body. No flag: unlike Viral Social Trends this is not a feature
  // being researched, it is where the Reports tab went.
  const isReport = channel === "report";

  // --- the filter disclosure ------------------------------------------------
  // Shut on arrival. Search and the competitor picker stay on screen because
  // they are what people reach for; region, outlet, format, theme, the fit bar,
  // and "Show" live behind this one button.
  //
  // This is only honest because the chip row below the toolbar renders whether
  // the panel is open or shut: the panel hides the CONTROL, never the
  // CONSEQUENCE. Nothing here is unreachable, and nothing here is silent.
  const [filtersOpen, setFiltersOpen] = useState(false);
  // `aria-controls` on the toggle has to point at the panel, and the panel is
  // rendered by a component that does not know what else is on the page, so
  // the id is generated here and handed to both.
  const filterPanelId = useId();

  // --- data ----------------------------------------------------------------
  const [items, setItems] = useState<TrendPost[] | null>(null);
  const [total, setTotal] = useState(0);
  const [hiddenByFit, setHiddenByFit] = useState(0);
  const [offset, setOffset] = useState(0);
  // --- navigation aids ------------------------------------------------------
  // The feed is long: 891 ideas at 36 a page is a ~12,000px scroll to reach the
  // "Load more" button, which is a strange thing to ask of someone who has
  // already told us they want more by scrolling this far. These three refs and
  // the one state below are what make the page navigable; none of them touch
  // what the feed SHOWS.
  const loadMoreRef = useRef<HTMLDivElement | null>(null);
  // The page we have already asked for, so one sentinel can't fire twice for it.
  const autoLoadedAt = useRef(-1);
  const searchRef = useRef<HTMLInputElement | null>(null);
  const topRef = useRef<HTMLDivElement | null>(null);
  // True once the top of the feed has scrolled away — drives the compact bar.
  const [scrolled, setScrolled] = useState(false);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [themes, setThemes] = useState<TrendTheme[]>([]);
  const [stats, setStats] = useState<TrendStats | null>(null);
  const [rescoring, setRescoring] = useState(false);
  const [reloadKey, setReloadKey] = useState(0);
  const [newsHealth, setNewsHealth] = useState<TrendNewsHealth | null>(null);
  // How many stories the next search may keep. Seeded from the last value the
  // backend remembers, so the control opens where it was left; after that it is
  // whatever is on screen. It is a cap on ONE search, never a schedule.
  const [storyCap, setStoryCap] = useState(10);
  const [cancelling, setCancelling] = useState(false);
  // How many Viral Social Trends that tab would show. null = don't render a
  // number at all; a wrong count is worse than none.
  const [generalCount, setGeneralCount] = useState<number | null>(null);
  const [fetching, setFetching] = useState(false);

  // The post whose "Make this" picker is open — null when the dialog is closed.
  const [makeTarget, setMakeTarget] = useState<TrendPost | null>(null);

  // A theme handed over from the Themes tab. The module slot covers the case
  // where this component mounts *after* the click; the event covers the case
  // where it is already mounted.
  useEffect(() => {
    const pending = consumePendingTheme();
    if (pending) {
      setTheme(pending);
      setOffset(0);
    }
    const onTheme = (e: Event) => {
      const next = (e as CustomEvent<string>).detail;
      if (typeof next === "string") {
        setTheme(next);
        setOffset(0);
        // Already mounted, so no remount will drain the slot — do it here, or
        // this theme would silently re-apply the next time the feed mounts.
        consumePendingTheme();
      }
    };
    window.addEventListener(TRENDS_THEME_EVENT, onTheme);
    return () => window.removeEventListener(TRENDS_THEME_EVENT, onTheme);
  }, []);

  // Debounce the search box — one request per pause, not per keystroke.
  useEffect(() => {
    const t = setTimeout(() => {
      setQ(qInput.trim());
      setOffset(0);
    }, 350);
    return () => clearTimeout(t);
  }, [qInput]);

  // The competitor picker: every watched account, by name. Excluded ones stay
  // in the list because their earlier posts are still in the feed.
  useEffect(() => {
    let cancelled = false;
    trendsApi
      .competitors()
      .then((r) => {
        if (cancelled) return;
        setCompetitors(
          [...(r.items || [])].sort((a, b) =>
            competitorName(a).localeCompare(competitorName(b), undefined, { sensitivity: "base" }),
          ),
        );
      })
      .catch(() => {
        /* the picker just offers "All competitors" */
      });
    return () => {
      cancelled = true;
    };
  }, []);

  // --- side data: themes for the picker + stats for the empty states --------
  useEffect(() => {
    let cancelled = false;
    void (async () => {
      try {
        const [t, s] = await Promise.all([
          trendsApi.themes(),
          trendsApi.stats(),
        ]);
        if (cancelled) return;
        setThemes(t.items ?? []);
        setStats(s);
      } catch {
        // Non-fatal: the feed itself still renders, the theme picker is just
        // empty and the empty states fall back to their generic copy.
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [reloadKey]);

  // --- the count on the Viral Social Trends tab -----------------------------
  // `generalTrends.stats().trends_total` is one COUNT(*) and would be cheaper,
  // but it counts EVERY row in general_trends — including the news, event and
  // product rows the participation gate holds back, and everything past the
  // freshness window. It reads far higher than what a click actually reveals.
  // The list endpoint returns `total` as the filtered count for the query, so
  // asking for one item gives the honest number: what the tab will show at its
  // own defaults.
  useEffect(() => {
    // No tab to hang the number on while the channel is hidden, so this is a
    // request whose answer nobody can see. See featureFlags.ts.
    if (!VIRAL_SOCIAL_TRENDS_ENABLED) return;
    let cancelled = false;
    void (async () => {
      try {
        const res = await generalTrendsApi.list({ limit: 1 });
        if (!cancelled) setGeneralCount(res.total ?? null);
      } catch {
        // Non-fatal: the tab renders without a count. The Viral Social Trends
        // endpoints are a separate router, so this failing says nothing about
        // the feed itself.
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [reloadKey]);

  // --- news collector status -------------------------------------------------
  // Only when the category is showing, so the competitor tabs cost nothing.
  useEffect(() => {
    if (!isNews) return;
    let cancelled = false;
    void (async () => {
      try {
        const h = await trendsApi.newsHealth();
        // Snapped to an option we actually offer. The backend accepts 3 to 20
        // and the old Configure screen let people store any of them, so a row
        // left at 12 opened this select on a value with no <option>, which
        // renders BLANK: the label reads "Up to" followed by nothing.
        if (h?.stories_per_day) setStoryCap(nearestStoryCap(h.stories_per_day));
        if (cancelled) return;
        setNewsHealth(h);
      } catch {
        // Non-fatal: the stories still render, the status line just goes quiet.
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [isNews, reloadKey]);

  const filterKey = useMemo(
    () =>
      JSON.stringify({
        category,
        sort,
        competitorId,
        region,
        mediaType,
        theme,
        minFit,
        show,
        q,
      }),
    [category, sort, competitorId, region, mediaType, theme, minFit, show, q],
  );
  const filterKeyRef = useRef(filterKey);

  useEffect(() => {
    // Any filter change restarts paging. Bounce through offset=0 rather than
    // fetching page N of a different query and appending it to the old list.
    if (filterKeyRef.current !== filterKey) {
      filterKeyRef.current = filterKey;
      if (offset !== 0) {
        setOffset(0);
        return;
      }
    }

    let cancelled = false;
    setLoading(true);
    void (async () => {
      try {
        const res = await trendsApi.posts({
          sort,
          ...(category ? { source: category } : {}),
          ...(competitorId !== "" ? { competitor_id: competitorId } : {}),
          ...(region ? { region } : {}),
          ...(mediaType ? { media_type: mediaType } : {}),
          ...(theme ? { theme } : {}),
          ...(minFit > 0 ? { min_fit: minFit } : {}),
          ...(show !== "active" && show !== "all"
            ? { status: show as TrendPostStatus }
            : {}),
          ...(q ? { q } : {}),
          // No w_* here any more. The blend that orders "Best opportunity" is
          // the server's, and it is the only one: three sliders that reordered
          // the feed were removed by request, so there is nothing left to send
          // and nothing for the two ends to disagree about.
          limit: PAGE_SIZE,
          offset,
        });
        if (cancelled) return;
        setError(null);
        setTotal(res.total ?? res.items.length);
        setHiddenByFit(res.hidden_by_min_fit ?? 0);
        setItems((prev) =>
          offset > 0 && prev ? [...prev, ...res.items] : res.items,
        );
      } catch (e) {
        if (cancelled) return;
        setError(humanError(e, "Couldn't load the trend feed"));
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
    async (post: TrendPost, next: TrendPostStatus) => {
      const prev = post.status;
      // Optimistic — a Save/Dismiss should feel instant; revert if it 4xxs.
      setItems((cur) =>
        cur ? cur.map((p) => (p.id === post.id ? { ...p, status: next } : p)) : cur,
      );
      try {
        await trendsApi.updatePost(post.id, { status: next });
      } catch (e) {
        setItems((cur) =>
          cur
            ? cur.map((p) => (p.id === post.id ? { ...p, status: prev } : p))
            : cur,
        );
        toast.error("Couldn't update that post", humanError(e));
      }
    },
    [toast],
  );

  // "Make this" now opens the tool picker; the model writes that tool's brief
  // and the creator opens pre-filled (lib/producerBrief). The old behaviour was
  // a clipboard copy plus a bare jump to #producer.
  const makeThis = useCallback(
    (post: TrendPost) => {
      const seed = (post.topic || post.topic_summary || post.caption || "").trim();
      if (!seed) {
        // Unscored AND captionless — there is genuinely nothing to work from.
        toast.info(
          "Nothing to build from this one",
          "It hasn't been scored and has no caption — open it on Instagram instead.",
        );
        return;
      }
      setMakeTarget(post);
    },
    [toast],
  );

  // POST /posts/rescore with no ids CLEARS every existing score and re-runs the
  // AI pass over the whole table — it is not an "score the new ones" shortcut.
  // That's expensive and irreversible-ish, so it asks first.
  const runRescore = useCallback(async () => {
    const ok = await confirm({
      // Title and button say the same words as the menu item that opened this,
      // so the confirmation reads as the same action rather than a new one.
      title: "Re-score every post?",
      message: (
        <>
          This wipes the fit and UAE scores on all {stats?.posts_total ?? 0}{" "}
          posts and asks the AI to read them again. Worth doing after the brand
          profile changes. Otherwise the ranking you already have is up to date.
        </>
      ),
      confirmLabel: "Re-score all",
    });
    if (!ok) return;
    setRescoring(true);
    try {
      const res = await trendsApi.rescore();
      toast.success(
        `Scored ${res.scored} post${res.scored === 1 ? "" : "s"}`,
        "The ranking has been refreshed.",
      );
      setReloadKey((k) => k + 1);
    } catch (e) {
      toast.error("Scoring failed", humanError(e));
    } finally {
      setRescoring(false);
    }
  }, [confirm, stats, toast]);

  /**
   * Switching category also switches the defaults that category is built on.
   *
   * News opens on Newest with the technical bar already raised; competitors
   * open on the opportunity blend with no bar, which is what they have always
   * done. Neither set of defaults would make sense on the other tab.
   */
  const selectCategory = useCallback(
    (next: "" | TrendSource) => {
      setChannel("posts");
      // Coming back from Viral Social Trends to the category you were already on only
      // swaps the body back. You never left that category — the filters, the
      // sort and the page you had are still the ones you chose, and re-applying
      // the defaults over them would look like the tab lost your work.
      if ((isGeneral || isReport) && next === category) return;
      setCategory(next);
      setOffset(0);
      if (next === "news") {
        // What could travel, on top: the order the owner asked for.
        setSort("viral");
        // A story belongs to no competitor, so this filter would empty the tab.
        setCompetitorId("");
        // No fit bar: the judge already refused to write anything below it, so
        // a bar here could only hide stories that passed the real test.
        setMinFit(0);
        setMediaType("");
        setRegion("");
        setTheme("");
        setShow("active");
        setQInput("");
        setQ("");
      } else {
        setSort("opportunity");
        setMinFit(0);
      }
    },
    [category, isGeneral, isReport],
  );

  // --- navigation aids ------------------------------------------------------

  /**
   * Fetch the next page when the "Load more" row comes into view.
   *
   * Scrolling to the bottom of a page IS the request for more, so making
   * someone find and click a button once they are already there is a step that
   * only ever costs them something. The button stays: it is the keyboard route,
   * it carries the remaining count, and it is what shows when a page fails.
   *
   * `rootMargin` fires it a screenful early so the next rows are usually in
   * place before the reader arrives, and `autoLoadedAt` makes the whole thing
   * idempotent — an observer can fire repeatedly for one intersection, and
   * without the guard that is a burst of duplicate requests for the same page.
   */
  useEffect(() => {
    const loaded = items?.length ?? 0;
    // Spelled out rather than reusing `hasMore`, which is derived further down
    // the component — this effect has to live up here beside the other refs.
    const more = loaded < total;
    const el = loadMoreRef.current;
    if (!el || !more || loading) return;
    const io = new IntersectionObserver(
      (entries) => {
        if (!entries[0]?.isIntersecting) return;
        if (loaded === 0 || autoLoadedAt.current === loaded) return;
        autoLoadedAt.current = loaded;
        setOffset(loaded);
      },
      { rootMargin: "600px 0px" },
    );
    io.observe(el);
    return () => io.disconnect();
  }, [items?.length, total, loading]);

  // Every filter change resets `offset` to 0, and that is also the moment the
  // guard above has to forget what it fetched: without this, changing a filter
  // and scrolling down again lands on a page number it thinks it already asked
  // for, and auto-load silently stops working for the rest of the session.
  useEffect(() => {
    if (offset === 0) autoLoadedAt.current = -1;
  }, [offset]);

  /**
   * Whether the top of the feed is still on screen.
   *
   * A sentinel rather than a scroll listener: this fires twice per session
   * instead of on every frame, and it stays correct when the page grows under
   * the reader as pages load in.
   */
  useEffect(() => {
    const el = topRef.current;
    if (!el) return;
    const io = new IntersectionObserver(
      (entries) => setScrolled(!entries[0]?.isIntersecting),
      { threshold: 0 },
    );
    io.observe(el);
    return () => io.disconnect();
  }, []);

  /**
   * "/" puts the cursor in the search box, Escape gives it back.
   *
   * The one keystroke worth having on a screen whose whole job is finding
   * something. Guarded so it cannot steal a character the reader meant to type:
   * not while they are already in a field, and not while a dialog is open,
   * where "/" belongs to whatever has focus in there.
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

  const scrollToTop = useCallback(() => {
    // `topRef` rather than window.scrollTo(0,0): the feed does not start at the
    // top of the document — the page heading and the tab bar sit above it — and
    // scrolling past those just makes the reader scroll back down again.
    topRef.current?.scrollIntoView({ behavior: "smooth", block: "start" });
    setTimeout(() => searchRef.current?.focus(), 350);
  }, []);

  // "Fetch now". The collector runs once a day on its own; this is the whole
  // of the manual half. `POST /news/fetch` marks the sources due AND starts the
  // round, so the work is genuinely under way when this resolves rather than
  // waiting on the next tick to notice.
  //
  // It still returns before the round finishes: a dozen feeds plus scoring is
  // minutes of work. So the feed is reloaded twice, at 15s and at 60s. The
  // first catches the fast sources, the second catches the rest, and neither
  // asks the reader to work out when to press refresh.
  const fetchNow = useCallback(async () => {
    setFetching(true);
    try {
      await trendsApi.newsFetch(storyCap);
      toast.success(
        "Searching",
        `Looking for up to ${storyCap} stories. They land over the next minute or two, and this page refreshes itself.`,
      );
      const timers = [15_000, 60_000].map((delay) =>
        setTimeout(() => setReloadKey((k) => k + 1), delay),
      );
      // Nothing to clean up on unmount: setReloadKey on an unmounted component
      // is a no-op in React 18, and the timers are gone within the minute.
      void timers;
    } catch (e) {
      toast.error("Couldn't start the search", humanError(e));
    } finally {
      setFetching(false);
    }
  }, [toast, storyCap]);

  const cancelSearch = useCallback(async () => {
    setCancelling(true);
    try {
      const r = await trendsApi.newsCancel();
      toast.info(r.stopped ? "Stopped" : "Nothing was running", r.note);
      // Re-read straight away: the whole point is that the button unsticks now,
      // not on whatever the next poll happens to be.
      setReloadKey((k) => k + 1);
    } catch (e) {
      toast.error("Couldn't stop it", humanError(e));
    } finally {
      setCancelling(false);
    }
  }, [toast]);

  const clearFilters = useCallback(() => {
    setSort(isNews ? "viral" : "opportunity");
    setCompetitorId("");
    setFromDate("");
    setToDate("");
    setRegion("");
    setMediaType("");
    setTheme("");
    setMinFit(0);
    setShow("active");
    setQInput("");
    setQ("");
    setOffset(0);
  }, [isNews]);

  // --- derived -------------------------------------------------------------

  const notDismissed = useMemo(
    () =>
      show === "active" && items
        ? items.filter((p) => p.status !== "dismissed")
        : items ?? [],
    [items, show],
  );
  // There WAS a client-side "UAE only" filter here, on `region`. Industry
  // Signal is no longer scoped to the UAE and news rows carry no region at all,
  // so a toggle over that column would filter everything to nothing.
  const visible = useMemo(() => {
    if (!fromDate && !toDate) return notDismissed;
    const from = fromDate ? new Date(`${fromDate}T00:00:00`).getTime() : -Infinity;
    const to = toDate ? new Date(`${toDate}T23:59:59.999`).getTime() : Infinity;
    return notDismissed.filter((p) => {
      const t = p.posted_at ? new Date(p.posted_at).getTime() : NaN;
      return Number.isFinite(t) && t >= from && t <= to;
    });
  }, [notDismissed, fromDate, toDate]);
  // Every measured competitor result we hold: what Viral potential is ranked
  // against, so it grows (and sharpens) with every scan.
  const viralPeers = useMemo(
    () =>
      (items ?? [])
        .filter((p) => p.source !== "news" && p.outperformance != null && Number.isFinite(p.outperformance))
        .map((p) => p.outperformance as number),
    [items],
  );
  const hiddenDismissed = (items?.length ?? 0) - notDismissed.length;
  // On news, the default fit bar is not a filter the user chose, so it does not
  // light up "Clear filters" on its own.
  const filtersActive =
    !!region || !!mediaType || !!theme || !!q || show !== "active" ||
    minFit > 0 || !!fromDate || !!toDate;
  const hasMore = (items?.length ?? 0) < total;
  const firstLoad = loading && items === null;

  /**
   * How many of the PANEL's controls are away from their default.
   *
   * This is the number on the Filters button, and it is the only thing that
   * makes collapsing those controls honest: a shut panel still has to say how
   * much is going on behind it.
   *
   * Search and sort are deliberately NOT counted. Both are on screen in the
   * toolbar with their current value visible, and counting a control the reader
   * can already see is exactly how a badge stops meaning anything: it would
   * read "2" on a page where nothing is filtered.
   *
   * The fit bar is measured against the DEFAULT for the category, not against
   * zero. News opens with the technical bar already raised, so on news "the
   * server's default" is the un-filtered state and both a higher bar and a
   * lowered one count as a change.
   *
   * Every axis below is sent to the API whenever it is set, even on a category
   * whose panel does not offer the control (`selectCategory` clears those, but
   * the request does not check), so each is counted whenever it is set rather
   * than only when its select is on screen. Counting only the visible ones
   * would let a filter remove rows with nothing on screen admitting it.
   */
  const filterCount = useMemo(() => {
    let n = 0;
    if (region) n += 1;
    if (mediaType) n += 1;
    if (theme) n += 1;
    if (minFit > 0) n += 1;
    if (show !== "active") n += 1;
    return n;
  }, [region, mediaType, theme, minFit, show]);

  return (
    <div className="space-y-5">
      {/* ---------------------------------------------------------------- */}
      {/* Category — which discovery channel the feed is showing.           */}
      {/* The first three share one score, so "All" can rank them together. */}
      {/* Viral Social Trends is a different table + score, so it swaps    */}
      {/* the body for its own tab rather than joining the ranked list.     */}
      {/* ---------------------------------------------------------------- */}
      {/* The sentinel the "have we scrolled" observer watches, and the anchor
          "Top" scrolls back to. Zero height: it marks a position, it is not a
          spacer. */}
      <div ref={topRef} aria-hidden className="h-0" />

      {/*
        The category row doubles as the feed's sticky header once the page
        scrolls, because this list is thousands of pixels long and the two
        things a reader loses first are which channel they are in and how far
        through it they are.

        At rest it is EXACTLY what it was — the border, blur and shadow are
        applied only once `scrolled`, so nothing about the resting page moved.
        Sticky rather than fixed so it keeps the content column's width and
        needs no knowledge of the sidebar.
      */}
      <div
        className={
          scrolled
            ? "sticky top-0 z-20 -mx-2 flex flex-wrap items-center justify-between gap-3 rounded-lg border border-border bg-bg/85 px-2 py-2 shadow-sm backdrop-blur"
            : "sticky top-0 z-20 -mx-2 flex flex-wrap items-center justify-between gap-3 px-2 py-2"
        }
      >
        <div
          role="tablist"
          aria-label="Trend category"
          className="inline-flex flex-wrap rounded-lg border border-border bg-bg-subtle p-0.5"
        >
          {CATEGORIES.map((c) => (
            <CategoryTab
              key={c.value || "all"}
              label={c.label}
              hint={c.hint}
              Icon={c.Icon}
              on={!isGeneral && !isReport && c.value === category}
              count={
                c.value === "competitor"
                  ? stats?.posts_competitor
                  : c.value === "news"
                    ? stats?.posts_news
                    : stats?.posts_total
              }
              onClick={() => selectCategory(c.value)}
            />
          ))}
          {/* The fourth channel is hidden while Viral Social Trends is being
              researched — see featureFlags.ts. This button is the only thing
              that can select it, so not rendering it is half of what keeps the
              body unreachable; `isGeneral` above is the other half. */}
          {VIRAL_SOCIAL_TRENDS_ENABLED && (
            <CategoryTab
              label={GENERAL_TAB.label}
              hint={GENERAL_TAB.hint}
              Icon={GENERAL_TAB.Icon}
              on={isGeneral}
              count={generalCount}
              onClick={() => setChannel("general")}
            />
          )}
          {/* Last on the row on purpose: the chips to its left are the things
              the report is written FROM, so it reads as the end of that
              sentence rather than as a fourth kind of list. */}
          <CategoryTab
            label={REPORT_TAB.label}
            hint={REPORT_TAB.hint}
            Icon={REPORT_TAB.Icon}
            on={isReport}
            onClick={() => setChannel("report")}
          />
        </div>

        {/* Only once the filter bar has scrolled away, because until then both
            of these are already on screen a few rows down and a second copy is
            just noise. This is the whole reason the row is sticky: after a
            couple of thousand pixels, "where am I and how do I get back" has no
            other answer. */}
        {scrolled && !isGeneral && !isReport && !firstLoad && (
          <div className="flex items-center gap-2">
            <span
              className="font-mono text-2xs text-muted tabular-nums"
              title="How many are on screen, out of everything that matches your filters"
            >
              {visible.length} of {total}
            </span>
            <button
              type="button"
              onClick={scrollToTop}
              title="Back to the top of the feed, with the search box focused"
              className="btn btn-ghost text-xs"
            >
              <ArrowUp className="h-3.5 w-3.5" aria-hidden />
              Top
            </button>
          </div>
        )}
      </div>

      {isReport ? (
        <>
          {/* -------------------------------------------------------------- */}
          {/* Report — the panel embedded whole. The sentence above it is the */}
          {/* one this section's heading used to carry while Reports was a    */}
          {/* page tab: the chip is two words, and two words cannot say what  */}
          {/* the document is.                                               */}
          {/* -------------------------------------------------------------- */}
          <p className="font-mono text-2xs text-muted px-0.5">
            Everything we found, written up as one document. Read it here, or
            download it as a PDF.
          </p>
          <ReportsPanel />
        </>
      ) : isGeneral ? (
        <>
          {/* -------------------------------------------------------------- */}
          {/* Viral Social Trends — the tab embedded whole, minus its own    */}
          {/* page header. Same component, same fetching, same cards; the    */}
          {/* alternative was a second copy of a grid that already exists.   */}
          {/* -------------------------------------------------------------- */}
          <div className="flex flex-wrap items-center gap-x-3 gap-y-1.5 px-0.5">
            <p className="font-mono text-2xs text-muted">
              Formats people are copying right now. These are ranked against
              each other, not against the posts on the other tabs.
            </p>
            <button
              type="button"
              onClick={() => goToTrendsTab("general")}
              title="The full tab adds what this one leaves out: Refresh now, Re-score all, and when the sources last answered"
              className="btn btn-ghost text-xs"
            >
              <Globe className="h-3.5 w-3.5" aria-hidden />
              Open the full tab
            </button>
          </div>

          <GeneralTrendsTab embedded />
        </>
      ) : (
        <>
          {/*
            One wrapper, one job: `scroll-margin-top` on everything below the
            sticky category tablist.

            The tablist pins itself to the top of the viewport once the page
            scrolls, and a browser scrolling a newly focused control into view
            aims for the top of the viewport, which is the space the tablist is
            now occupying. Tab into the grid from off-screen and the focused
            button lands underneath it: `focus-not-obscured` in the UX rules,
            and a WCAG 2.2 AA failure.

            The textbook fix is `scroll-padding-top` on the scrolling element,
            but that is the document here and the document's stylesheet is
            shared with the rest of the app. `scroll-margin-top` on the target
            does the same job from this side, and it has to reach every
            descendant rather than this element, because the thing the browser
            scrolls to is the focused control itself, never its container.

            6rem clears the tablist at its resting height and still clears it
            when the tab labels wrap to a second line on a narrow window. The
            "Top" anchor is deliberately OUTSIDE this wrapper: it sits above
            the tablist, so an offset there would only scroll past the page
            heading and make the reader come back down.
          */}
          <div className="space-y-5 [&_*]:scroll-mt-24">
            {/* ---------------------------------------------------------------- */}
            {/* The search. Nothing fills this feed on its own, so this control    */}
            {/* is how stories get here: pick a ceiling, press the button.         */}
            {/* ---------------------------------------------------------------- */}
            {isNews && (
              <NewsSearchBar
                health={newsHealth}
                limit={storyCap}
                onLimit={setStoryCap}
                fetching={fetching}
                cancelling={cancelling}
                onFetchNow={() => void fetchNow()}
                onCancel={() => void cancelSearch()}
              />
            )}

            {/* ---------------------------------------------------------------- */}
            {/* Controls - competitors only.                                      */}
            {/*                                                                   */}
            {/* Industry Signal has none of this, on purpose. It shows at most ten */}
            {/* stories a morning and keeps a fortnight, so it is a short list     */}
            {/* somebody reads top to bottom: search, sort, outlet, theme, a fit   */}
            {/* bar and a chip row were all machinery for finding a needle in a    */}
            {/* haystack that is not there. The fit bar in particular was hiding   */}
            {/* rows we should never have written; the judge now drops those       */}
            {/* before they reach the table, so there is nothing left to filter.   */}
            {/*                                                                   */}
            {/* Competitors keeps every control: that feed is hundreds of posts    */}
            {/* across many accounts, which is the case all of it was built for.   */}
            {/* ---------------------------------------------------------------- */}
            {!isNews && (
            <div className="card space-y-3 p-4">
              <Toolbar>
                <SearchField
                  value={qInput}
                  onChange={setQInput}
                  // One handler for the clear button AND for Escape, so both empty
                  // the box, the query it applied and the page offset. Clearing
                  // only the input would leave the feed filtered by a term that is
                  // no longer written anywhere on screen.
                  onClear={() => {
                    setQInput("");
                    setQ("");
                    setOffset(0);
                  }}
                  inputRef={searchRef}
                  placeholder="Topic, caption, handle…"
                  className="min-w-[13rem] flex-1"
                />

                {/* Which account the feed shows. In the open row, where the sort
                    used to be: it is the control people reach for here ("only
                    MBZUAI"), and its value explains what is on screen. */}
                <label className="inline-flex items-center gap-1.5">
                  <span className="eyebrow">Competitor</span>
                  <select
                    value={competitorId === "" ? "" : String(competitorId)}
                    onChange={(e) => {
                      setCompetitorId(e.target.value ? Number(e.target.value) : "");
                      setOffset(0);
                    }}
                    className={SELECT_CLS}
                  >
                    <option value="">All competitors</option>
                    {competitors.map((c) => (
                      <option key={c.id} value={c.id}>
                        {competitorName(c)}
                      </option>
                    ))}
                  </select>
                </label>

                <label className="inline-flex items-center gap-1.5">
                  <span className="eyebrow">From</span>
                  <input
                    type="date"
                    value={fromDate}
                    max={toDate || undefined}
                    onChange={(e) => setFromDate(e.target.value)}
                    className={SELECT_CLS}
                  />
                </label>
                <label className="inline-flex items-center gap-1.5">
                  <span className="eyebrow">To</span>
                  <input
                    type="date"
                    value={toDate}
                    min={fromDate || undefined}
                    onChange={(e) => setToDate(e.target.value)}
                    className={SELECT_CLS}
                  />
                </label>

                <ToolbarSpacer />

                <FilterToggle
                  open={filtersOpen}
                  count={filterCount}
                  onClick={() => setFiltersOpen((v) => !v)}
                  panelId={filterPanelId}
                />

                {/* Real but rare: run once after the brand profile changes, never
                    during a normal session. Not rendered at all when it would be
                    empty, because a kebab that opens onto nothing is worse than
                    no kebab. */}
                {stats && (stats.posts_total > 0 || stats.posts_unscored > 0) && (
                  <OverflowMenu label="Scoring actions">
                    {stats.posts_total > 0 && (
                      <MenuItem
                        onClick={() => void runRescore()}
                        disabled={rescoring}
                        Icon={Sparkles}
                        title="Wipes every score and asks the AI to read every post again. For after the brand profile changes."
                      >
                        {rescoring ? "Re-scoring…" : "Re-score all"}
                      </MenuItem>
                    )}
                    {/* Was a standing amber line in the filter bar. It is a note
                        about work already scheduled, not something to act on, so
                        it sits beside the one button that would change it. */}
                    {stats.posts_unscored > 0 && (
                      <MenuNote>
                        {stats.posts_unscored} post
                        {stats.posts_unscored === 1 ? " has" : "s have"} no topic or
                        fit score yet. The next scan fills them in.
                      </MenuNote>
                    )}
                  </OverflowMenu>
                )}
              </Toolbar>

              {/* ---------------------------------------------------------------- */}
              {/* Chips: every reason the grid is showing less than it has.         */}
              {/*                                                                   */}
              {/* Two kinds in one row. Neutral chips are filters the reader set    */}
              {/* and can see the effect of; amber ones are rows a DEFAULT is       */}
              {/* withholding, which is the case nobody asked for and so the only   */}
              {/* case that needs a colour. Both say the number and the reason in   */}
              {/* words, so the tone only ever reinforces what is already written.  */}
              {/* Every chip is its own one-click undo.                             */}
              {/* ---------------------------------------------------------------- */}
              {(filterCount > 0 ||
                (!firstLoad && (hiddenByFit > 0 || hiddenDismissed > 0))) && (
                <ChipRow>
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

                  {mediaType && (
                    <FilterChip
                      label="Format"
                      value={formatLabel(mediaType)}
                      onRemove={() => {
                        setMediaType("");
                        setOffset(0);
                      }}
                    />
                  )}

                  {region && (
                    <FilterChip
                      label="Region"
                      value={regionLabel(region)}
                      onRemove={() => {
                        setRegion("");
                        setOffset(0);
                      }}
                    />
                  )}

                  {minFit > 0 && (
                    <FilterChip
                      label="Fit"
                      value={`${minFit} and up`}
                      title={`Hiding anything the AI scored below ${minFit} for how usable it is`}
                      onRemove={() => {
                        setMinFit(0);
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

                  {/* The amber ones. Not filters anyone chose: a default is
                      holding these rows back, and a feed that quietly drops a
                      quarter of its rows is a feed nobody can trust. */}

                  {!firstLoad && hiddenByFit > 0 && (
                    <FilterChip
                      tone="hidden"
                      value={`${hiddenByFit} too technical`}
                      title="Show the stories below the fit bar: mostly model releases, benchmarks and funding news"
                      removeLabel="Show the stories below the fit bar as well"
                      onRemove={() => {
                        setMinFit(0);
                        setOffset(0);
                      }}
                    />
                  )}

                  {!firstLoad && hiddenDismissed > 0 && (
                    <FilterChip
                      tone="hidden"
                      value={`${hiddenDismissed} dismissed`}
                      title="Show dismissed ideas as well"
                      removeLabel="Show dismissed ideas as well"
                      onRemove={() => {
                        setShow("all");
                        setOffset(0);
                      }}
                    />
                  )}
                </ChipRow>
              )}

              {/* How much of the list is on screen. Its own component so the digit
                  width is fixed and the line is reserved before the first fetch
                  lands, which is what stops the grid stepping up by a row the
                  moment a number appears. */}
              <ResultCount
                loading={firstLoad}
                shown={visible.length}
                total={total}
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
                {/* Region is not offered on news: the UAE toggle in the toolbar is
                    the same axis, and two controls for one axis is how you get
                    contradictory state. */}
                {!isNews && (
                  <Field label="Region">
                    <select
                      value={region}
                      onChange={(e) => {
                        setRegion(e.target.value as "" | TrendRegion);
                        setOffset(0);
                      }}
                      className={`${SELECT_CLS} w-full`}
                    >
                      <option value="">All regions</option>
                      <option value="uae">UAE</option>
                      <option value="gcc">GCC</option>
                      <option value="international">International</option>
                    </select>
                  </Field>
                )}

                {/* An article has no Instagram media type, so news swaps Format for
                    the one facet it does have: who published it. */}
                {(
                  <Field label="Format">
                    <select
                      value={mediaType}
                      onChange={(e) => {
                        setMediaType(e.target.value as "" | TrendMediaType);
                        setOffset(0);
                      }}
                      className={`${SELECT_CLS} w-full`}
                    >
                      <option value="">All formats</option>
                      {FORMAT_OPTIONS.map((o) => (
                        <option key={o.value} value={o.value}>
                          {o.label}
                        </option>
                      ))}
                    </select>
                  </Field>
                )}

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
                        {t.theme} ({t.post_count})
                      </option>
                    ))}
                  </select>
                </Field>

                <Field label="Minimum fit">
                  <select
                    value={String(minFit)}
                    onChange={(e) => {
                      setMinFit(Number(e.target.value));
                      setOffset(0);
                    }}
                    className={`${SELECT_CLS} w-full`}
                  >
                    {minFitOptions(minFit).map((o) => (
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

              </FilterPanel>
            </div>
            )}

            {error && <ErrorCard message={error} />}

            {/* ---------------------------------------------------------------- */}
            {/* Grid                                                              */}
            {/* ---------------------------------------------------------------- */}
            <RadarPicks which={category} />

            {firstLoad ? (
              <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-3">
                {Array.from({ length: 6 }).map((_, i) => (
                  <PostCardSkeleton key={i} />
                ))}
              </div>
            ) : visible.length === 0 && isNews ? (
              // The competitor empty states talk about tracked accounts, which means
              // nothing here: news has no accounts to add.
              <NewsEmptyState
                health={newsHealth}
                fetching={fetching}
                onFetchNow={() => void fetchNow()}
              />
            ) : visible.length === 0 ? (
              <FeedEmptyState
                stats={stats}
                filtersActive={filtersActive}
                hiddenDismissed={hiddenDismissed}
                onClearFilters={clearFilters}
                onShowDismissed={() => {
                  setShow("all");
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
                  {visible.map((post) => (
                    <PostCard
                      key={post.id}
                      post={post}
                      onStatus={setStatus}
                      onMakeThis={makeThis}
                      viralPeers={viralPeers}
                    />
                  ))}
                </div>

                {hasMore && (
                  // The sentinel is the element the observer watches, and the
                  // button inside it is deliberately NOT redundant: it is the
                  // keyboard and screen-reader route to the next page, and the
                  // place the count and any load error are announced. Auto-load
                  // is the shortcut, not the mechanism.
                  <div ref={loadMoreRef} className="flex justify-center pt-2">
                    <button
                      type="button"
                      disabled={loading}
                      onClick={() => setOffset(items?.length ?? 0)}
                      className="btn btn-ghost text-sm"
                    >
                      {loading
                        ? "Loading…"
                        : `Load more (${total - (items?.length ?? 0)} left)`}
                    </button>
                  </div>
                )}
              </>
            )}
          </div>

          {makeTarget && (
            <MakeThisDialog post={makeTarget} onClose={() => setMakeTarget(null)} />
          )}
        </>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// The channel tablist
// ---------------------------------------------------------------------------

/**
 * One tab in the category tablist.
 *
 * A component rather than a loop over four descriptors, because Viral Social Trends is not
 * a `TrendSource`: it selects a different body, not a different `source=` in
 * the same query. Sharing the button keeps the four looking like one control.
 *
 * `count` is optional and rendered only when it is a real number — a category
 * whose count could not be read shows nothing rather than a zero it does not
 * mean.
 */
function CategoryTab({
  label,
  hint,
  Icon,
  on,
  count,
  onClick,
}: {
  label: string;
  hint: string;
  Icon: typeof Newspaper;
  on: boolean;
  count?: number | null;
  onClick: () => void;
}) {
  return (
    <button
      type="button"
      role="tab"
      aria-selected={on}
      onClick={onClick}
      title={hint}
      className={[
        "flex items-center gap-1.5 rounded-md px-3 py-1.5 text-[13px] font-medium transition-colors",
        on ? "bg-bg text-fg shadow-sm" : "text-muted hover:text-fg",
      ].join(" ")}
    >
      <Icon className="h-3.5 w-3.5" strokeWidth={1.8} />
      {label}
      {count != null && (
        <span className="font-mono text-3xs tabular-nums text-muted">
          {count}
        </span>
      )}
    </button>
  );
}

// ---------------------------------------------------------------------------
// Card
// ---------------------------------------------------------------------------

function PostCard({
  post,
  onStatus,
  onMakeThis,
  viralPeers,
}: {
  post: TrendPost;
  onStatus: (post: TrendPost, next: TrendPostStatus) => void | Promise<void>;
  onMakeThis: (post: TrendPost) => void | Promise<void>;
  viralPeers: readonly number[];
}) {
  const dismissed = post.status === "dismissed";
  const saved = post.status === "saved";
  const isNews = post.source === "news";
  const headline = headlineFor(post);
  const handle = post.username?.replace(/^@/, "") ?? "";
  // News extras live in metadata — they're source-specific, so none of them
  // earned a column of their own.
  const meta = (post.metadata ?? {}) as Record<string, unknown>;
  // We show OUR plain rewrite as the headline, so the date can be our estimate
  // too when the story arrived without one. The pill says "approx." rather than
  // quietly presenting a guess as the publication date.
  const dateIsEditTime = meta.date_is_estimated === true;
  const coverKind = coverKindOf(post);
  const hasImage = coverKind !== "none";
  // Who the story is about, when the judge named a company. A logo tile is
  // captioned with that name rather than the publisher, because the mark on
  // it is theirs.
  const company = typeof meta.company === "string" ? meta.company : "";
  // "How 42 could post about this" is on EVERY news card. The judge writes an
  // angle for every story now; a story from before it did shows why the story
  // matters instead, which is the next best sentence to stand there. Never
  // blank, because a card with the block missing looked like a card with
  // nothing to say.
  const whyItMatters =
    typeof meta.why_it_matters === "string" ? meta.why_it_matters : "";
  const angle = post.suggested_angle || (isNews ? whyItMatters : "");
  // Could this go viral? The one score a news card shows, and the order the
  // news channel opens in. Null until the housekeeping tick has scored a story
  // from before the score existed.
  const viral = isNews ? viralOf(post) : pastViralOf(post.outperformance, viralPeers);
  // The detail dialog, owned by the card. A dedicated BUTTON rather than a
  // clickable card body: the card already holds "Make this", save, dismiss, the
  // outbound permalink and (on news) a publisher line, so a click target
  // wrapped around all of it would make every one of them ambiguous.
  const [detailOpen, setDetailOpen] = useState(false);
  // With no image the tile becomes the headline block, so the heading below it
  // is suppressed — otherwise the same sentence printed twice, adjacently. Not
  // scoped to news: a competitor post whose thumbnail failed to download hits
  // exactly the same path.
  const tileCarriesHeadline = !hasImage;
  // Who published it — the tint's seed and the tile's caption.
  //
  // The seed is the PUBLISHER and never the headline or the post id: a colour
  // per outlet is something a reader picks up without being told, while a
  // colour per story is noise that happens to be stable. On news `username`
  // holds the outlet name (news_ingest writes `item.outlet or source.name`
  // into it), so it is a real caption rather than an invented @handle.
  const outlet = (isNews ? post.platform || post.username : handle) || "";
  const coverSeed = outlet || (isNews ? "news" : "competitor");
  const coverLabel = isNews ? outlet || "Industry Signal" : handle ? `@${handle}` : "";

  return (
    <article
      // One fixed box for every card. The height used to follow the content (a
      // 4/5 photo, an optional angle, a metrics row), so the grid went ragged
      // row by row; the text blocks below are clamped to fit this height.
      // 36.5rem, not 33rem: a news card with its headline under the tile also
      // carries the viral-potential row, and at 33rem that pushed the bottom
      // button row (Dismiss, open the story) out of the box.
      className={`card card-hover flex h-[36.5rem] flex-col overflow-hidden ${
        dismissed ? "opacity-55" : ""
      }`}
    >
      {/* Thumbnail. The locally cached thumb is the primary source because IG
          CDN URLs expire within days; the live URL is the fallback.

          A small fixed preview, the same size on every card: it identifies the
          post, and Details is where the full image is.

          A news story usually has no image — many feeds ship none at all — so
          it gets a text tile rather than an empty frame. Skipping <Thumb>
          entirely also avoids a guaranteed-404 request per card.

          13.5rem rather than h-40 when the tile carries the headline: it takes
          the room of the two-line heading it replaces (2 x 22px plus the gap),
          so the card is the same height, the rows under it start on the same
          line as on a card with a picture, and the headline has room for six
          lines instead of three. */}
      <div
        className={`relative shrink-0 bg-bg-muted ${
          tileCarriesHeadline ? "h-[13.5rem]" : "h-40"
        }`}
      >
        {!hasImage ? (
          // Most news sources ship no image at all: OpenAI, tahawultech and
          // the Google News lane among them, so this is the normal card, not
          // a degraded one. The tile IS the headline here, which is why the
          // heading below is suppressed rather than printing it twice.
          //
          // Seeded on the PUBLISHER, never the headline: the point of the tint
          // is that every story from one outlet arrives the same colour, which
          // is what makes a screen of imageless cards scannable instead of
          // uniformly grey.
          <CoverTile
            seed={coverSeed}
            label={coverLabel}
            title={headline}
            Icon={isNews ? Newspaper : Users}
          />
        ) : coverKind === "logo" ? (
          // A brand mark, not a photograph: the company the story is about,
          // or the publisher when it gave no picture. Drawn small and centred
          // on the same tinted card the imageless stories get, captioned with
          // whose mark it is. Stretched to fill the tile through <Thumb> it
          // would be a blurry square, which is the one thing worse than the
          // drawn house cover it replaced.
          <CoverTile
            seed={company || coverSeed}
            label={company || coverLabel}
            logoSrc={trendsApi.thumbUrl(post.id)}
          />
        ) : (
          <Thumb
            src={trendsApi.thumbUrl(post.id)}
            fallbackSrc={post.thumbnail_url ?? post.media_url ?? null}
            alt={headline}
            className="h-full w-full"
            // The SAME tile the imageless cards get. Instagram's CDN URLs
            // expire within days of collection, so this fires constantly on
            // older posts — and the old treatment (flat grey, "no preview" in
            // 10px mono) made a normal, expected outcome look like breakage.
            // No `title` here, deliberately. `tileCarriesHeadline` is false on
            // this path — the card believed it had an image — so the heading
            // still renders below, and passing the headline in would print the
            // same sentence twice, adjacently.
            empty={
              <CoverTile
                seed={coverSeed}
                label={coverLabel}
                Icon={isNews ? Newspaper : Users}
              />
            }
          />
        )}

        <div className="pointer-events-none absolute inset-x-0 top-0 flex items-start justify-between gap-2 p-2.5">
          {/* HOW OLD IT IS, on every card, news or competitor.
              A competitor post used to show "4.7x / their average" here. That
              pill is gone by request: a multiple of a number the reader has
              never seen is not something anyone can act on, and it was the
              loudest thing on the card. Outperformance is still collected, and
              "Beat their average by most" still sorts by it, so the ranking is
              unchanged. It just is not shouted at the reader any more. */}
          <FreshnessPill postedAt={post.posted_at} approximate={dateIsEditTime} />
          {/* Media format, which only a competitor post has. News deliberately
              shows nothing here: the publisher already has a home in the
              provenance footer, and printing it in both places meant the same
              word appeared twice on one card. */}
          {post.media_type && (
            <span className="rounded bg-black/60 px-1.5 py-0.5 font-mono text-[9px] uppercase tracking-wider2 text-white/90">
              {MEDIA_LABELS[post.media_type] ?? post.media_type}
            </span>
          )}
        </div>

        {dismissed && (
          <span className="absolute bottom-2 left-2.5 rounded bg-black/65 px-2 py-0.5 font-mono text-[9px] uppercase tracking-wider2 text-white/90">
            dismissed
          </span>
        )}
      </div>

      {/*
        Reading order, loudest first. Four things, and the card says each one
        exactly once:

          1. what is it            the headline, the only large text
          2. what would we do      42's angle, labelled as ours
          3. how do we know        the measured numbers and the source
          4. act                   one primary button, then the rest

        A fifth block sat between 1 and 2: "is it any good", as four score and
        label chips. It is gone by request. The scores were never a reason to
        make something, they were the ordering the feed had already applied
        before the card was drawn, printed again in the reader's face.

        `gap-3` between blocks and `gap-1.5` inside one is the whole grouping
        mechanism, so 3 reads as supporting detail rather than as its own
        announcement.
      */}
      <div className="flex flex-1 flex-col gap-3 p-4">
        {/* The one thing to read first. A step up from the 11px supporting text
            below rather than half a step, which is what made a card of small
            grey rows with a slightly darker row on top.

            Two lines, so every card fits its fixed height; `title` plus the
            Details button underneath are the two ways to the full text:
            `truncation-strategy` says a clamp is only honest when the whole
            string is reachable, and a headline is exactly that kind of text.

            Skipped when the imageless tile above is already showing it as the
            heading, which would print the same sentence twice, adjacently. */}
        {!tileCarriesHeadline && (
          <h3
            title={headline}
            className="text-base font-semibold leading-snug text-fg line-clamp-2"
          >
            {headline}
          </h3>
        )}

        {/* A row of chips stood here: "fit 100", "uae 100", the theme and the
            effort. All four are gone by request. They are the machine's own
            working, not information: a bare 0-100 with no scale beside it tells
            a reader nothing they can act on, and four of them were the first
            thing the eye hit after the headline.

            NOTHING WAS TURNED OFF TO DO THIS. Every score is still computed,
            still stored, still what orders the feed, and still filterable by
            "Minimum fit" and by theme. Details shows all of it with its
            provenance, which is the surface built for exactly that question. */}

        {(isNews || viral) && (
          <div>
            {viral ? (
              <ViralPill verdict={viral} />
            ) : (
              <span className="inline-flex items-center gap-1.5 rounded-md border border-border bg-bg-muted px-2 py-1 text-xs text-muted">
                Viral potential: not scored yet
              </span>
            )}
          </div>
        )}

        {angle && (
          <div className="rounded-lg border border-accent/20 bg-accent-fade p-2.5">
            {/* Labelled as 42's idea, never as part of the story: on a news
                card the headline above is someone else's reporting and this is
                ours, and the two must not blur together. */}
            <p className="eyebrow mb-1">
              {isNews ? "How 42 could post about this" : "How 42 could do this"}
            </p>
            {/* Two lines, so the card keeps its fixed 36.5rem height (more
                pushed "Make this" out of the box); the whole sentence is in
                `title` and in Details. */}
            <p
              title={angle}
              className="text-xs leading-relaxed text-fg/85 line-clamp-2"
            >
              {angle}
            </p>
            {post.suggested_format && (
              <p className="mt-1.5 font-mono text-3xs text-muted">
                as a {post.suggested_format}
              </p>
            )}
          </div>
        )}

        {/* Supporting metrics, and only for a competitor post. A news story
            carried a clamped two-line "why it matters" here; it is gone by
            request, because a paragraph cut mid-sentence ("...has broken")
            makes a reader stop and squint at a card built to be scanned. The
            whole sentence is still on the story, in Details, and the angle 42
            would take is the block above this one. */}
        {!isNews && (
          <div className="flex flex-wrap items-center gap-3 font-mono text-2xs text-muted">
            <span className="inline-flex items-center gap-1" title="Likes">
              <Heart className="h-3 w-3" aria-hidden />
              {fmtCompact(post.like_count)}
            </span>
            <span className="inline-flex items-center gap-1" title="Comments">
              <MessageCircle className="h-3 w-3" aria-hidden />
              {fmtCompact(post.comments_count)}
            </span>
            {post.media_type === "VIDEO" && post.view_count != null && (
              <span className="inline-flex items-center gap-1" title="Views">
                <Play className="h-3 w-3" aria-hidden />
                {fmtCompact(post.view_count)}
              </span>
            )}
            {post.posted_at && (
              <span className="ml-auto">{fmtDate(post.posted_at)}</span>
            )}
          </div>
        )}

        {/* Where it came from. A news story has no account behind it, so it
            names the publisher rather than rendering an avatar and a follower
            count that were never measured. `platform` holds the real outlet —
            for an aggregated story that is the original publisher, never the
            aggregator, so the reader knows where a click lands. */}
        {isNews ? (
          <div className="flex items-center gap-2 border-t border-border pt-3">
            <span className="inline-flex h-6 w-6 shrink-0 items-center justify-center rounded-full bg-bg-muted">
              <Newspaper className="h-3 w-3 text-muted" strokeWidth={1.8} />
            </span>
            <div className="min-w-0 flex-1">
              <p className="truncate text-xs font-medium text-fg">
                {post.platform || "Industry Signal"}
              </p>
              {/* "via <feed name>" stood here. There are no feeds to name any
                  more: the outlet above IS the provenance. */}
              <p className="truncate font-mono text-3xs text-muted">
                {post.posted_at ? fmtDate(post.posted_at) : "recent"}
              </p>
            </div>
          </div>
        ) : (
          <div className="flex items-center gap-2 border-t border-border pt-3">
            <Avatar src={post.competitor_avatar} name={handle} size={24} />
            <div className="min-w-0 flex-1">
              <p className="truncate text-xs font-medium text-fg">@{handle}</p>
              <p className="truncate font-mono text-3xs text-muted">
                {fmtCompact(post.competitor_followers)} followers ·{" "}
                {regionLabel(post.competitor_region)}
              </p>
            </div>
          </div>
        )}

        {/* Actions, in two tiers.
            "Make this" is what the whole screen exists for, so it gets its own
            full-width row and is the only filled button on the card. The other
            four used to share one wrapping row with it, where a 3-column grid
            left them breaking after "Make this" at an unpredictable point and
            the primary action was just the widest of five equals. */}
        <div className="mt-auto space-y-1.5 pt-1">
          <button
            type="button"
            onClick={() => void onMakeThis(post)}
            className="btn btn-primary w-full text-xs"
          >
            <Sparkles className="h-3.5 w-3.5" aria-hidden />
            Make this
          </button>
          <div className="flex flex-wrap items-center gap-1.5">
            <button
              type="button"
              onClick={() => setDetailOpen(true)}
              title={
                isNews
                  ? "Everything we know about this story: what we measured, what the AI made of it, and the timeline"
                  : "Everything we know about this post: every measured number, what the AI made of it, and the timeline"
              }
              className="btn btn-ghost text-xs"
            >
              <Info className="h-3.5 w-3.5" aria-hidden />
              Details
            </button>
            <button
              type="button"
              onClick={() => void onStatus(post, saved ? "new" : "saved")}
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
              onClick={() => void onStatus(post, dismissed ? "new" : "dismissed")}
              title={dismissed ? "Put this back in the feed" : "Not for us"}
              className="btn btn-ghost text-xs"
            >
              {dismissed ? (
                <Undo2 className="h-3.5 w-3.5" aria-hidden />
              ) : (
                <X className="h-3.5 w-3.5" aria-hidden />
              )}
              {dismissed ? "Restore" : "Dismiss"}
            </button>
            {post.permalink && (
              <a
                href={post.permalink}
                target="_blank"
                rel="noopener noreferrer"
                title={isNews ? "Read the story" : "Open on Instagram"}
                aria-label={isNews ? "Read the story" : "Open on Instagram"}
                className="btn btn-ghost text-xs"
              >
                <ExternalLink className="h-3.5 w-3.5" aria-hidden />
              </a>
            )}
          </div>
        </div>
      </div>

      {detailOpen && (
        <TrendDetailDialog
          target={{ kind: "post", post }}
          onClose={() => setDetailOpen(false)}
          pastViral={isNews ? null : viral}
        />
      )}
    </article>
  );
}

/**
 * The card's shape while it loads.
 *
 * Shaped to what actually arrives, not to a generic block: two headline lines,
 * the badge row, the angle callout, the source line and the primary action.
 * That is `content-jumping` in the UX rules, and it is worth the extra bars
 * here because six of these are on screen at once, so any height they get
 * wrong is multiplied by six the moment the first page lands.
 */
function PostCardSkeleton() {
  return (
    <div className="card h-[36.5rem] overflow-hidden">
      <Skeleton className="h-40 w-full" rounded="rounded-none" />
      <div className="space-y-2 p-4">
        <Skeleton h="h-4" className="w-4/5" />
        <Skeleton h="h-4" className="w-3/5" />
        <Skeleton h="h-3" className="w-2/5" />
        <Skeleton h="h-14" className="w-full" rounded="rounded-lg" />
        <Skeleton h="h-3" className="w-3/5" />
        <Skeleton h="h-7" className="w-full" rounded="rounded-lg" />
      </div>
    </div>
  );
}

/*
 * `UndoChip` used to live here: a 10px mono button reading "12 dismissed ×".
 *
 * It is now `FilterChip tone="hidden"` from controls.tsx, which does the same
 * job for the same three reasons (the fit bar, the UAE toggle, dismissals) and
 * gets two things this version never had: an X that is a real 24x24 button with
 * its own accessible name, and a shared row with the chips for the filters the
 * reader set. One row, one way to undo anything the grid is withholding.
 */

// ---------------------------------------------------------------------------
// The collector's status line
// ---------------------------------------------------------------------------

/**
 * A time, with the day attached when it is not today.
 *
 * A bare clock time was fine when the collector ran hourly. It is not fine now
 * that it runs once a day: "next 09:14" on a Tuesday afternoon reads as a
 * fourteen-hour-old promise rather than tomorrow morning, and the one thing
 * this line exists to say is when the next run actually is.
 */
function fmtClock(iso: string | null | undefined): string {
  if (!iso) return "not scheduled";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "not scheduled";

  const time = d.toLocaleTimeString(undefined, CLOCK_12H);
  // Compared as calendar days rather than as a 24h difference: 23:50 tonight
  // and 00:10 tomorrow are twenty minutes apart and still different days,
  // which is exactly the distinction a reader is making here.
  const midnight = new Date();
  midnight.setHours(0, 0, 0, 0);
  const days = Math.floor((d.getTime() - midnight.getTime()) / 86_400_000);

  if (days === 0) return time;
  if (days === 1) return `tomorrow ${time}`;
  if (days === -1) return `yesterday ${time}`;
  return `${d.toLocaleDateString("en-GB", { weekday: "short", day: "numeric", month: "short" })}, ${time}`;
}

/**
 * The control that finds stories, and the one line saying how the last go went.
 *
 * NOTHING RUNS ON A TIMER. This feed used to fill itself at 7am and the strip
 * here had to say so, because a reader who did not know the cadence read a feed
 * with four new cards on it as broken. That whole sentence is gone with the
 * schedule it was explaining: a person decides when to look, so there is
 * nothing to explain.
 *
 * THE CAP IS PICKED EVERY TIME, and it sits in front of the button rather than
 * in a settings screen, because it is part of this decision and not a standing
 * preference: "find me a few things" and "find me everything you can" are
 * different asks on different days. It remembers where it was left so nobody
 * re-picks the same number daily, and that is all the memory it has.
 *
 * "UP TO" IS THE WHOLE WORDING PROBLEM SOLVED. A number next to a search button
 * reads as a promise, and then four results looks like a failure. It is a
 * ceiling: the search stops there, and coming back with fewer is the normal,
 * correct outcome on a quiet day.
 */
const RUN_TROUBLE: Record<string, string> = {
  ungrounded: "the AI answered without searching, so we ignored it",
  search_failed: "the search did not finish",
  judge_failed: "we could not read the results back",
  no_budget: "we ran out of AI budget before we could look",
  unavailable: "the AI is not set up correctly",
};

/** What the reader can ask for. Bounded by the backend at 3 and 20. */
const STORY_CAPS = [5, 10, 15, 20];

/** The offered option closest to a remembered value. A `<select>` whose value
 *  matches no option renders empty, so nothing outside this list may reach it. */
function nearestStoryCap(n: number): number {
  return STORY_CAPS.reduce((best, c) =>
    Math.abs(c - n) < Math.abs(best - n) ? c : best, STORY_CAPS[0]);
}

/**
 * How long a run may claim to be running before this screen stops believing it.
 *
 * A run row is opened before the work and closed after, so a backend killed in
 * between leaves one at `running` for ever. That is not hypothetical: a row
 * from 11 Sep sat open for a day and a half and locked this button behind a
 * spinner with nothing behind it, which is exactly what the owner reported.
 *
 * The backend closes stale rows on its own now, but this guard stays, because
 * the screen must never be the thing that cannot recover: the worst case here
 * is a genuinely long search whose button re-enables early, and pressing it
 * again is answered with "one is already running".
 *
 * A real run measures at about two minutes.
 */
const RUN_LOOKS_STUCK_MS = 15 * 60_000;

function isRunningNow(health: TrendNewsHealth | null): boolean {
  if (!health) return false;
  // `running` is the server's own answer and the field to trust. The run list
  // is what ages it: the server says "a row is open", and only this side knows
  // whether an open row that old is still believable.
  if (!health.running) return false;
  const open = (health.runs ?? []).filter((r) => r.status === "running");
  if (!open.length) return false;
  return open.some((r) => {
    const started = r.started_at ? new Date(r.started_at).getTime() : NaN;
    if (Number.isNaN(started)) return false;
    return Date.now() - started < RUN_LOOKS_STUCK_MS;
  });
}

function NewsSearchBar({
  health,
  fetching,
  cancelling,
  limit,
  onLimit,
  onFetchNow,
  onCancel,
}: {
  health: TrendNewsHealth | null;
  fetching: boolean;
  cancelling: boolean;
  limit: number;
  onLimit: (n: number) => void;
  onFetchNow: () => void;
  onCancel: () => void;
}) {
  if (!health) return null;

  const last = health.last_run;
  const trouble = last ? RUN_TROUBLE[last.status] : undefined;
  const running = isRunningNow(health);
  const busy = fetching || running;

  return (
    <div className="flex flex-wrap items-center gap-x-3 gap-y-2 px-0.5">
      <div className="flex items-center gap-2">
        {/* A real label, not a placeholder: the number means nothing without
            the two words in front of it. */}
        <label
          htmlFor="news-cap"
          className="text-xs text-muted"
          title="The search stops at this many. Coming back with fewer is normal."
        >
          Up to
        </label>
        <select
          id="news-cap"
          value={String(limit)}
          onChange={(e) => onLimit(Number(e.target.value))}
          disabled={busy}
          className={SELECT_CLS}
        >
          {STORY_CAPS.map((n) => (
            <option key={n} value={String(n)}>
              {n} stories
            </option>
          ))}
        </select>

        <button
          type="button"
          onClick={onFetchNow}
          disabled={busy}
          title="Search the web for stories worth posting about"
          className="btn btn-primary text-xs"
        >
          <Search
            className={`h-3.5 w-3.5 ${busy ? "animate-pulse" : ""}`}
            aria-hidden
          />
          {busy ? "Searching…" : "Find stories"}
        </button>

        {/* Only while something is actually going on. A Stop button on a screen
            with nothing to stop is a button that teaches people to ignore it. */}
        {busy && (
          <button
            type="button"
            onClick={onCancel}
            disabled={cancelling}
            title="Stop this search. Anything already found is kept."
            className="btn btn-ghost text-xs"
          >
            <X className="h-3.5 w-3.5" aria-hidden />
            {cancelling ? "Stopping…" : "Stop"}
          </button>
        )}
      </div>

      {/* One line, and never a cadence. Either the search is going, or the last
          one is worth a sentence, or there is nothing to say yet. */}
      <p role="status" aria-live="polite" className="text-2xs text-muted">
        {busy ? (
          "Looking now. A minute or two."
        ) : trouble ? (
          <span className="text-warning">Last time, {trouble}.</span>
        ) : last && last.status === "cancelled" ? (
          /* Somebody pressed Stop. A choice, not a fault, so it gets neither a
             warning colour nor an apology. */
          <>Last search was stopped.</>
        ) : last && last.status === "empty" ? (
          /* Not a failure, and it must not read as one: some days there is
             genuinely nothing worth posting about, and refusing to invent
             something is the feature working. */
          <>Last search at {fmtClock(last.started_at)} found nothing worth posting.</>
        ) : last ? (
          <>
            Last search at {fmtClock(last.started_at)}, kept {last.kept} of{" "}
            {last.found}.
          </>
        ) : (
          "Nothing searched for yet."
        )}
      </p>

      {last?.error && !busy && (
        <p className="w-full font-mono text-3xs text-warning">{last.error}</p>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Empty states — each one names the next action
// ---------------------------------------------------------------------------

/**
 * The news ladder. Five genuinely different reasons for an empty grid, and the
 * fix for each is different. What used to lead this list was "no feeds are
 * switched on" and "every feed is failing"; there are no feeds any more, and
 * the two states that replaced them are the ones a search can actually be in:
 * it could not run, or it ran and kept nothing.
 *
 * The order matters. "It broke" has to come before "there was no news",
 * because a broken search and a quiet morning produce an identical empty grid
 * and only one of them is somebody's problem to fix.
 */
function NewsEmptyState({
  health,
  fetching,
  onFetchNow,
}: {
  health: TrendNewsHealth | null;
  fetching: boolean;
  onFetchNow: () => void;
}) {
  const last = health?.last_run ?? null;

  // A "switched off" branch stood here. It read `health.enabled`, which was the
  // schedule's on/off switch, and the schedule is gone: there is nothing to
  // switch off, because nothing runs unless somebody presses the button.

  // b. It ran and it broke. Named separately from a quiet day, because the two
  //    look identical here and only one of them needs somebody to do something.
  if (last && RUN_TROUBLE[last.status]) {
    return (
      <EmptyState
        title="The last search did not work"
        body={
          <>
            {RUN_TROUBLE[last.status]}.
            {last.error ? ` ${last.error}` : ""}
          </>
        }
        action={
          <button
            type="button"
            className="btn btn-primary text-sm"
            onClick={onFetchNow}
            disabled={fetching}
          >
            {fetching ? "Searching…" : "Try again"}
          </button>
        }
      />
    );
  }

  // c. Never run. Distinct from "ran and found nothing" - hence `ever_ran`.
  if (health && !health.ever_ran) {
    return (
      <EmptyState
        title="No stories yet"
        body="Press Find stories and we search the web for AI news worth posting about, then write each one up in plain English. Nothing happens until you ask."
        action={
          <button
            type="button"
            className="btn btn-primary text-sm"
            onClick={onFetchNow}
            disabled={fetching}
          >
            {fetching ? "Searching…" : "Find stories"}
          </button>
        }
      />
    );
  }

  // e. It ran, it worked, and there was nothing worth posting about. This is
  //    the feature behaving correctly and the copy has to say so plainly:
  //    padding a quiet day is exactly what it refuses to do.
  return (
    <EmptyState
      title="Nothing worth posting about"
      body="We looked and nothing passed the test. Showing you nothing beats filling the page with stories your audience would not care about. Widen what we look for, or search again."
      action={
        <div className="flex flex-wrap items-center justify-center gap-2">
          <button
            type="button"
            className="btn btn-primary text-sm"
            onClick={onFetchNow}
            disabled={fetching}
          >
            {fetching ? "Searching…" : "Search again"}
          </button>
          {/* The other half of the advice above, as a control rather than an
              instruction. Telling somebody to go and widen the brief while
              giving them no way to get there is how the old copy ended up
              naming a tab that had been deleted. */}
          <button
            type="button"
            className="btn btn-ghost text-sm"
            onClick={() => goToTrendsTab("news")}
          >
            Change what we look for
          </button>
        </div>
      }
    />
  );
}

function FeedEmptyState({
  stats,
  filtersActive,
  hiddenDismissed,
  onClearFilters,
  onShowDismissed,
}: {
  stats: TrendStats | null;
  filtersActive: boolean;
  hiddenDismissed: number;
  onClearFilters: () => void;
  onShowDismissed: () => void;
}) {
  if (stats && stats.competitors_active === 0) {
    return (
      <EmptyState
        title="No competitors to watch yet"
        body={
          <>
            The feed is built from accounts you track. Add a few Instagram
            handles (schools, universities, tech communities, anyone whose
            audience overlaps with 42's) and the scanner will start finding
            the posts that did better than usual for them.
          </>
        }
        action={
          <button
            type="button"
            className="btn btn-primary text-sm"
            onClick={() => goToTrendsTab("accounts")}
          >
            Go to Accounts we watch
          </button>
        }
      />
    );
  }

  if (stats && stats.posts_total === 0) {
    return (
      <EmptyState
        title="Nothing scanned yet"
        body={
          <>
            You're tracking {stats.competitors_active} account
            {stats.competitors_active === 1 ? "" : "s"} but haven't pulled their
            posts. Run a scan: it fetches recent posts, compares each one to that
            account's own average, and scores how usable the idea is for 42.
          </>
        }
        action={
          <button
            type="button"
            className="btn btn-primary text-sm"
            onClick={() => goToTrendsTab("accounts")}
          >
            Go to Accounts we watch
          </button>
        }
      />
    );
  }

  if (filtersActive) {
    return (
      <EmptyState
        title="Nothing matches these filters"
        body="Try a lower minimum fit, a wider region, or a shorter search. Every filter that is hiding ideas is a chip above the grid, and each chip removes itself."
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
        body={`All ${hiddenDismissed} idea${hiddenDismissed === 1 ? " has" : "s have"} been marked as "not for us". Run a fresh scan for new posts, or bring the dismissed ones back to reconsider them.`}
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
      title="No ideas in the feed"
      body="Once a scan finishes, the competitor posts that did better than usual show up here, ranked by how usable they are for 42."
      action={
        <button
          type="button"
          className="btn btn-ghost text-sm"
          onClick={() => goToTrendsTab("accounts")}
        >
          Go to Accounts we watch
        </button>
      }
    />
  );
}

// ---------------------------------------------------------------------------
// helpers
// ---------------------------------------------------------------------------

/**
 * `topic` is the transferable angle. Unscored posts fall back to the caption.
 *
 * News inverts that: the real headline IS the story, and the scorer's angle is
 * 42's idea about it, which already has its own labelled callout further down
 * the card. Showing the angle up top would present our interpretation as the
 * news — the one thing a school's account must never do.
 *
 * A news headline is returned WHOLE. Every place it is drawn clamps it to the
 * lines it has room for, ending on an ellipsis, and the full sentence is what
 * the `title` tooltip promises; a 150-character cut here stopped it mid-word
 * ("with teacher tr…") on a tile with room to spare, and in the tooltip too.
 * A competitor caption keeps its cut: it can be 2,200 characters of hashtags.
 */
function headlineFor(post: TrendPost): string {
  const caption = post.caption?.replace(/\s+/g, " ").trim();
  if (post.source === "news" && caption) return caption;
  const topic = post.topic?.trim();
  if (topic) return topic;
  if (caption) return caption.length > 120 ? `${caption.slice(0, 120)}…` : caption;
  return "Not scored yet";
}

function fmtDate(iso: string): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  return d.toLocaleDateString("en-GB", { day: "numeric", month: "short" });
}
