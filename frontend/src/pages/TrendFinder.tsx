/**
 * Trend finder — a topic scout for 42 Abu Dhabi.
 *
 * It watches competitor Instagram accounts, finds the posts that OVERPERFORMED
 * for their own account, and scores how practical each idea is for 42 to make.
 * The bar is Feed | Configure:
 *   • Feed                    → the ranked opportunities (TrendsFeed), and with
 *                               them the Report, which is a CHIP in the feed's
 *                               own category row rather than a tab up here
 *   • Configure               → the two things you set up rather than read:
 *                               Accounts we watch (whose posts we read) and
 *                               News to look for (what a search goes and finds)
 *   • Viral Social Trends     → what the internet is recreating (GeneralTrendsTab)
 *                               HIDDEN, see components/trends/featureFlags.ts
 *   • Themes                  → the same posts rolled up into reusable buckets
 *                               HIDDEN, see components/trends/featureFlags.ts
 *
 * REPORTS CAME OFF THIS BAR. It was "Feed | Reports" here with the feed's own
 * category chips in a row directly underneath, which is two rows of tabs, one
 * above the other, the top one holding two buttons. The report is one more chip
 * on the row that was already there, so the feed has one row of controls and
 * the bar has one fewer button to choose between.
 *
 * CONFIGURE IS BACK. Both of its screens spent a spell as sections of the
 * Settings page, on the argument that everything you change belongs in one
 * place. They are back because the stronger argument is the other one: these
 * two are not settings for the app, they are the inputs to THIS section's
 * output, and the person tuning either one is reading the Feed while they do
 * it. See components/trends/ConfigurePanel.tsx, which is the bar and nothing
 * else: both bodies print their own heading now.
 *
 * EVERY HASH EITHER SCREEN HAS EVER HAD STILL WORKS, and that is four of them.
 * #trends/competitors is the oldest spelling, from when the accounts screen
 * was a top-level tab here; #trends/accounts and #trends/news are what the bar
 * writes today; #trends/configure is what somebody types after reading the
 * tab's name. `normSub` below folds all of them onto the two real routes.
 * #settings/accounts and #settings/news, from the Settings spell, redirect
 * here from pages/Settings.tsx rather than being swallowed.
 *
 * The active view is driven by the second hash segment (#trends/general,
 * #trends/themes, #trends/accounts, #trends/news, #trends/reports) so each
 * view is deep-linkable and the back button works; the LANDING tab (the first
 * one still on the bar) is the bare #trends route.
 *
 * Configure's TWO sub-tabs ride that same second segment, because the hash
 * grammar has exactly two (App.routeParamFromHash = split("/")[1]) - there is
 * no third to put them in. So #trends/accounts and #trends/news both select
 * Configure and differ only in which sub-tab opens, and the back button steps
 * through sub-tabs like any other tab.
 *
 * The Feed also carries Viral Social Trends as a category in its OWN tablist,
 * which renders `GeneralTrendsTab embedded`. That selection is local state
 * inside the feed: it does not touch the hash, so #trends/general keeps
 * meaning this page's full-page Viral Social Trends tab and the back button
 * keeps behaving. The full tab is what the feed's "Open the full tab" links to.
 *
 * TWO sub-tabs are currently hidden: Viral Social Trends (the owner is working
 * out what belongs in it) and Themes (a roll-up of what the Feed already lists,
 * which the owner does not want on the bar). Both go through the same two
 * mechanisms, and both are needed: the button is not rendered, AND normSub()
 * folds a bookmarked or shared #trends/<tab> back to the landing tab. Without
 * the second, such a link prints a header for a tab with no button and no body
 * beneath it. Nothing else here changed, so flipping a flag brings its tab back
 * exactly as written, and the bar with it.
 *
 * The heading is constant: "SMAA intelligence" on every tab. It
 * used to be the open tab's own title, so the largest text on the page changed
 * every time somebody clicked, and it changed into a longer way of saying the
 * tab label they had just pressed. Only the sentence under it varies, and only
 * where a tab has something to say that its own controls do not, which is why
 * the Feed's is empty: its category chips already name every list behind them.
 * The report's own sentence moved with it, and is rendered by the feed above
 * the embedded panel.
 *
 * This shell owns the two things that are true across every tab: whether
 * Instagram can be read at all (/health), and whether a scan is running right
 * now (/scan/latest, polled). PageTransition remounts this page on every tab
 * change, so both are re-synced from the server on mount — nothing is cached
 * in module scope.
 *
 * The scan poll never stops while the page is open, only slows down. It used
 * to ask once at mount and then only keep asking while it had seen a scan
 * running, which meant a scan started from the accounts screen (the only place
 * one CAN be started) was invisible to this shell: no progress card on any
 * other tab, and no "Scan finished" toast from anywhere, because the toast
 * only fired for scans the shell had watched. PageTransition keys on the
 * top-level tab, so moving between #trends/* routes does not remount this page
 * and never gave it a second chance to look.
 */
import { useCallback, useEffect, useRef, useState, type ComponentType } from "react";
import {
  AlertTriangle,
  Globe,
  Layers,
  Loader2,
  SlidersHorizontal,
  TrendingUp,
} from "lucide-react";

import type { Tab } from "../App";
import { api } from "../lib/api";
import type { TrendHealth, TrendScan } from "../lib/types";
import { usePoll } from "../lib/usePoll";
import { useToast } from "../components/ToastProvider";
import {
  THEMES_TAB_ENABLED,
  VIRAL_SOCIAL_TRENDS_ENABLED,
} from "../components/trends/featureFlags";
// The Instagram banner is drawn with the shared Notice rather than markup of
// its own, so the one warning this page can raise looks like every other
// warning in the section instead of like a page-wide failure.
import { Notice } from "../components/trends/controls";
import { TabBar } from "../components/trends/TabBar";
import { TrendsFeed } from "../components/trends/TrendsFeed";
import { GeneralTrendsTab } from "../components/trends/GeneralTrendsTab";
import { ThemesTab } from "../components/trends/ThemesTab";
// The Configure tab's body, and the only thing this page imports that is not a
// tab of its own. It is a controlled component: it holds NO sub-tab state, the
// open sub-tab arrives as a prop, a click goes straight back out through
// `onSub`, and this page turns that into a hash change, which is what makes the
// back button step through sub-tabs. Its two bodies are lazy inside it, so the
// Feed does not pay for a 2,200-line screen it never shows.
//
// `ConfigureSub` and `CONFIGURE_DEFAULT` are imported rather than re-declared:
// the panel's own props are typed against them, and the default sub-tab is the
// spelling this page writes for a Configure click.
import {
  ConfigurePanel,
  CONFIGURE_DEFAULT,
  type ConfigureSub,
} from "../components/trends/ConfigurePanel";
// ReportsPanel is not imported: the feed mounts it as a chip now.

/**
 * A tab on the page bar.
 *
 * "reports" is NOT here any more. The report is a chip in the feed's own
 * category row now (components/trends/TrendsFeed.tsx), which is what took the
 * page bar down to a single button and therefore off the screen. It survives as
 * a Route below, because `#trends/reports` is in bookmarks and still has to
 * open the report.
 */
type SubTab = "feed" | "general" | "themes" | "configure";

/**
 * Every spelling the second hash segment resolves to. It is NOT `SubTab`, and
 * keeping the two vocabularies apart is what lets a screen be renamed without
 * breaking a bookmark: "accounts" and "news" both select the Configure tab and
 * differ only in which sub-tab opens, and "reports" selects the Feed with its
 * report chip open.
 */
type Route = "feed" | "general" | "themes" | "reports" | ConfigureSub;

interface TabDef {
  key: SubTab;
  label: string;
  /**
   * The one sentence under the page heading. "" for a tab that needs none.
   *
   * Called `lead` for the class it renders as. The per-tab `title` that used to
   * sit beside it is gone with the per-tab heading: every one of them was a
   * longer way of saying the tab's own label.
   */
  lead: string;
  Icon: ComponentType<any>;
}

const TABS: TabDef[] = [
  {
    key: "feed",
    label: "Feed",
    // No sentence on purpose. The category buttons at the top of the feed
    // already say what each list holds, and a paragraph above them could only
    // repeat them, at the top of the page, in the largest body text on it.
    lead: "",
    Icon: TrendingUp,
  },
  {
    key: "general",
    label: "Viral Social Trends",
    lead: "Formats people are RECREATING (memes, sounds and challenges from TikTok, Know Your Meme, YouTube Shorts and Instagram), grouped into one card each and ranked by how well 42 could make its own version. News and search spikes are things that happened, not things you can join in on, so they sit behind the 'News and events' filter instead.",
    Icon: Globe,
  },
  {
    key: "themes",
    label: "Themes",
    lead: "The same posts grouped into topics, so you can see which kinds of stories keep working.",
    Icon: Layers,
  },
  {
    key: "configure",
    label: "Configure",
    // No sentence, and unlike every other tab here that is not a judgement
    // call. Both bodies print their own heading and their own description
    // directly above their own controls, so anything written up here would be
    // a second, competing description of the screen, an inch further from the
    // thing it describes, and it would have to change with the sub-tab.
    lead: "",
    Icon: SlidersHorizontal,
  },
];

/**
 * What the tab bar actually renders.
 *
 * The hidden tabs' descriptors above stay where they are, because they are the
 * copy those tabs come back with. They simply do not reach the bar while their
 * flag is off, and filtering here rather than deleting the entries keeps the
 * label, the sentence, the icon and the ordering in one place for whoever flips
 * them back on.
 *
 * `TAB_FLAGS` maps each FLAGGED tab to whether its flag is ON. A tab absent
 * from it has no flag and is always shown, so gating a new one later is one
 * line here and one in `normSub` below — which is the pair that must move
 * together, since hiding the button without folding the route leaves a
 * bookmarked link on a header with no body.
 */
const TAB_FLAGS: Partial<Record<SubTab, boolean>> = {
  general: VIRAL_SOCIAL_TRENDS_ENABLED,
  themes: THEMES_TAB_ENABLED,
};

const VISIBLE_TABS: TabDef[] = TABS.filter((t) => TAB_FLAGS[t.key] !== false);

/**
 * Where a hidden tab's route lands, and what the bare `#trends` route means:
 * the first tab still on the bar.
 *
 * It was the literal "feed" everywhere below, which was safe only while the
 * feed was the one tab that could never be hidden. Now that it can be, the fold
 * has to follow the bar, or hiding it would send every bookmark to a tab with
 * no button and no body beneath it: the exact failure the fold exists to
 * prevent. The `?? "feed"` covers every flag being off at once, which cannot
 * happen today because the feed carries no flag and is always the last tab
 * standing.
 */
const LANDING: SubTab = VISIBLE_TABS[0]?.key ?? "feed";

/**
 * The same tab, as a ROUTE, which is what the folds below actually return.
 *
 * Configure is the one tab whose key is not a hash spelling of its own: it has
 * two, one per sub-tab, so a fold onto it has to pick one. Every other tab's
 * key is its route, and the cast says so. Today this is always "feed" and the
 * branch is dead, which is the point of writing it: it is the line that keeps
 * the fold honest if Configure ever ends up first on the bar.
 */
const LANDING_ROUTE: Route =
  LANDING === "configure" ? CONFIGURE_DEFAULT : (LANDING as Route);

function normSub(s?: string | null): Route {
  // Fold a hidden tab's route back to the landing tab. Bookmarks and links
  // pasted into chat outlive a feature being pulled from the UI, and landing on
  // a sub-tab with no button to leave it by is worse than landing on the bar's
  // first tab.
  if (s === "general") return VIRAL_SOCIAL_TRENDS_ENABLED ? "general" : LANDING_ROUTE;
  if (s === "themes") return THEMES_TAB_ENABLED ? "themes" : LANDING_ROUTE;
  // Configure's two screens, and every spelling either has ever had.
  // "competitors" is what the accounts screen was called when it was a
  // top-level tab here, and it is still in bookmarks and in chat. "configure"
  // is what somebody types after reading the tab's name: nothing links to it,
  // because the bar writes the open sub-tab's own spelling, but it opens the
  // default sub-tab rather than bouncing to the feed.
  if (s === "accounts" || s === "competitors") return "accounts";
  if (s === "news") return "news";
  if (s === "configure") return CONFIGURE_DEFAULT;
  if (s === "reports") return "reports";
  return LANDING_ROUTE;
}

/**
 * Which page tab a route selects.
 *
 * Both Configure routes select Configure. `#trends/reports` selects the landing
 * tab: the report lives inside the feed now, so the feed is the tab that shows
 * it, and which CHIP it opens on is the `initialChannel` prop below.
 */
function tabOf(route: Route): SubTab {
  if (route === "accounts" || route === "news") return "configure";
  return route === "reports" ? LANDING : (route as SubTab);
}

/** Which Configure sub-tab a route opens. Anything else opens the default. */
function configureSubOf(route: Route): ConfigureSub {
  return route === "news" ? "news" : CONFIGURE_DEFAULT;
}

/** How often to re-check a running scan. */
const SCAN_POLL_MS = 3000;
/**
 * How often to ask while nothing is running. Not never: a scan can start from
 * the accounts screen under Configure, or from another browser entirely, and a
 * shell that only asked at mount learnt about neither. Ten seconds is slow enough to
 * cost nothing and fast enough that a scan is on screen before anyone wonders
 * whether the button worked. Paused while the tab is hidden, since nobody is
 * reading it then.
 */
const SCAN_IDLE_POLL_MS = 10_000;

interface Props {
  subtab?: string | null;
  navigate: (t: Tab, param?: string) => void;
}

export function TrendFinder({ subtab, navigate }: Props) {
  const route = normSub(subtab);
  const active = tabOf(route);
  const configureSub = configureSubOf(route);
  const tab = VISIBLE_TABS.find((t) => t.key === active) ?? VISIBLE_TABS[0];
  // Derived in render, like `route` and `active` above it. Held in state it
  // would gain nothing except a way to disagree with the hash it came from.
  // Each tab's own sentence; the Feed's and Configure's are both empty.
  const lead = tab.lead;
  const toast = useToast();

  const [health, setHealth] = useState<TrendHealth | null>(null);
  const [scan, setScan] = useState<TrendScan | null>(null);

  // Can we read Instagram at all? One call on mount — the answer only changes
  // when someone edits credentials, which means a reload anyway.
  useEffect(() => {
    let cancelled = false;
    api.trends
      .health()
      .then((h) => {
        if (!cancelled) setHealth(h);
      })
      .catch(() => {
        // A failed /health call is itself the "can't reach it" answer.
        if (!cancelled) {
          setHealth({ ok: false, reason: "unreachable", error: null });
        }
      });
    return () => {
      cancelled = true;
    };
  }, []);

  // Scan progress. A scan runs in a background worker and survives a backend
  // restart, so the page can't hold the state — it has to ask.
  //
  // The last SETTLED scan this shell has seen, by id. `undefined` until the
  // first answer arrives, so whatever finished before the page opened is the
  // baseline and not news. Announcing by id rather than by "did I watch it
  // run" means a scan short enough to start and finish between two ticks is
  // still announced, which the old sawActive flag could never do.
  const settledRef = useRef<number | null | undefined>(undefined);

  const absorb = useCallback(
    (latest: TrendScan | null) => {
      setScan(latest);
      if (!latest || latest.active) {
        if (settledRef.current === undefined) settledRef.current = null;
        return;
      }
      if (settledRef.current === undefined) {
        settledRef.current = latest.id;
        return;
      }
      if (latest.id === settledRef.current) return;
      settledRef.current = latest.id;
      if (latest.status === "failed") {
        toast.error("Scan failed", latest.error ?? undefined);
      } else {
        toast.success(
          "Scan finished",
          latest.posts_found != null ? `${latest.posts_found} posts found` : undefined,
        );
      }
    },
    [toast],
  );

  // One answer straight away: usePoll's first tick waits a whole interval, and
  // a scan caught at page load should be on screen with the page, not ten
  // seconds after it. A failure here is not handled because the poll below is
  // the retry.
  useEffect(() => {
    let cancelled = false;
    api.trends
      .latestScan()
      .then(({ scan: latest }) => {
        if (!cancelled) absorb(latest);
      })
      .catch(() => {});
    return () => {
      cancelled = true;
    };
  }, [absorb]);

  usePoll({
    enabled: true,
    intervalMs: scan?.active ? SCAN_POLL_MS : SCAN_IDLE_POLL_MS,
    pauseWhenHidden: true,
    onTick: async () => {
      const { scan: latest } = await api.trends.latestScan();
      absorb(latest);
    },
  });

  function go(key: SubTab) {
    // The landing tab is the bare #trends route; the others carry the sub-tab
    // as the param so #trends stays the canonical entry point.
    //
    // Configure writes its DEFAULT SUB-TAB's spelling, not its own. That keeps
    // #trends/accounts the URL you get from clicking Configure, the same one
    // the sub-bar and every `goToTrendsTab("accounts")` call in this section
    // already write, so that screen has one address instead of two.
    if (key === "configure") {
      goSub(CONFIGURE_DEFAULT);
      return;
    }
    navigate("trends", key === LANDING ? undefined : key);
  }

  function goSub(s: ConfigureSub) {
    // Sub-tabs are hash changes, not local state, so back/forward step through
    // them and a copied URL reopens the one you were looking at.
    navigate("trends", s);
  }

  const done = scan?.competitors_done ?? 0;
  const total = scan?.competitors_total ?? 0;
  const pct = total > 0 ? Math.min(100, Math.round((done / total) * 100)) : 0;

  return (
    <div className="space-y-6 animate-pageEnter">
      <header>
        {/* One name for the whole section, identical on every tab, so the
            heading stops changing underneath the reader as they move along the
            bar. Which tab is open is the tab bar's job, and it is already
            saying so with an underline and aria-current.

            A "42 Abu Dhabi" eyebrow sat above this. Removed: the school's name
            is on the logo in the rail to the left of it, every screen in this
            app is about 42 Abu Dhabi, and an eyebrow that says the same thing
            on every page is a line the eye learns to skip. */}
        <h1 className="h1">SMAA intelligence</h1>
        {/* Only when the tab has one. An empty string renders nothing at all
            rather than an empty paragraph holding open a line of space. */}
        {lead && <p className="lead mt-1.5 max-w-2xl">{lead}</p>}
      </header>

      {/* Instagram cannot be read. A WARNING, not a failure: Instagram feeds
          competitor scanning and nothing else, and the news half of every list
          on this page carries on without it. So the banner has to name the half
          it is talking about, or it reads as "nothing here works" over a page
          that mostly does.

          One row, in the shared Notice, for the same reason. It sits above the
          thing the reader came for, and the amber box this replaced was four
          lines of headline, paragraph and raw error, which made the smallest
          problem on the page the loudest thing on it.

          Viral Social Trends do not touch Instagram, so the banner stays off
          there. While that tab is hidden normSub() never returns "general", so
          the guard is inert rather than wrong: it stays so that flipping the
          flag restores the exemption without a second edit here.

          The news exemption is checked on the ROUTE rather than the tab,
          because Configure's other sub-tab is exactly where a dead token is
          worth saying out loud. Exempting the whole Configure tab would hide
          the banner on the one screen it most belongs on.

          What to do about it is deliberately unglamorous, because no button in
          this app fixes it: the token lives in the server's own configuration,
          and the old copy sent people to a Settings screen that has no such
          field. Meta's own error text is diagnostics for whoever does the
          reconnecting, not something the reader can act on, so it hangs off the
          sentence as a tooltip instead of being printed across the top of the
          page in monospace. */}
      {health && !health.ok && active !== "general" && route !== "news" && (
        <Notice tone="warning" Icon={AlertTriangle}>
          <span title={health.error ?? undefined}>
            {health.reason === "not_configured"
              ? "Instagram isn't connected, so no new competitor posts are coming in until someone reconnects it. AI news is not affected."
              : "Instagram didn't answer, so no new competitor posts are coming in. That is usually temporary. AI news is not affected."}
          </span>
        </Notice>
      )}

      {/* A scan is running — the sentence comes from the backend, show it as-is.

          Not on the accounts route: that screen draws its own indicator with
          the same sentence, the same count and the same bar, and it is the only
          screen that can start a scan, so it is the one place the two were
          guaranteed to stack. Same idiom as the health banner's route exemption
          above. The poll keeps running there regardless: the "Scan finished"
          toast is still this shell's to say, wherever it was started from. */}
      {scan?.active && route !== "accounts" && (
        <div className="rounded-xl border border-border bg-bg-muted px-4 py-3">
          <div className="flex items-center gap-2.5">
            <Loader2
              className="w-4 h-4 shrink-0 text-accent animate-spin"
              strokeWidth={1.8}
            />
            <p className="min-w-0 flex-1 text-[13px] text-fg truncate">
              {scan.progress || "Scanning competitor accounts…"}
            </p>
            {total > 0 && (
              <span className="shrink-0 font-mono text-2xs text-muted tabular-nums">
                {done}/{total} accounts
              </span>
            )}
          </div>
          {total > 0 && (
            <div className="mt-2.5 h-1 rounded-full bg-border overflow-hidden">
              <div
                className="h-full rounded-full bg-accent transition-all duration-500"
                style={{ width: `${pct}%` }}
              />
            </div>
          )}
        </div>
      )}

      {/* Underline tab bar, drawn only when there is a choice to make. With
          one tab left it would be a row holding a single button that selects
          the thing already on screen, under a heading that names the section:
          the reader gains nothing and loses a line of the page. Configure is
          the second button, so it draws again. */}
      {VISIBLE_TABS.length > 1 && (
        <TabBar
          items={VISIBLE_TABS}
          active={active}
          onSelect={go}
          label="Trends sections"
        />
      )}

      {/* Active panel — each tab fetches its own data. */}
      <div>
        {active === "feed" && (
          <TrendsFeed initialChannel={route === "reports" ? "report" : undefined} />
        )}
        {active === "general" && <GeneralTrendsTab />}
        {active === "themes" && <ThemesTab />}
        {active === "configure" && (
          <ConfigurePanel sub={configureSub} onSub={goSub} />
        )}
      </div>
    </div>
  );
}
