/**
 * Cross-tab hand-off inside the Trends section.
 *
 * The tab bodies take no props and `PageTransition` remounts them, so a
 * "See posts" click in Themes cannot pass a filter down through React. Two
 * mechanisms cover both orderings:
 *
 *   1. a module-level slot, which SURVIVES the remount and is drained by the
 *      feed on mount (Themes -> Feed, where the feed is not mounted yet);
 *   2. a window event, for when the feed is already mounted and only needs to
 *      re-filter.
 *
 * The hash is nudged to `#trends/<sub-tab>` as well so the section shell — and
 * a copied URL — lands on the right sub-tab. The hash grammar has exactly two
 * segments (App.routeParamFromHash), so the theme itself rides the slot, not
 * the URL.
 */

/**
 * The second hash segment, as the shell reads it (pages/TrendFinder.tsx
 * `normSub`).
 *
 * "accounts" and "news" are the two Configure sub-tabs, spelled the way the
 * sub-bar writes them. "competitors" is the older spelling of the accounts
 * screen: the shell still accepts it, because bookmarks and pasted links
 * outlive a rename, but nothing in this app should write it any more.
 */
export type TrendsSubTab =
  | "feed"
  | "general"
  | "themes"
  | "reports"
  | "accounts"
  | "news";

export const TRENDS_THEME_EVENT = "trends:show-theme";

let pendingTheme: string | null = null;

/**
 * Move to a sub-tab. The feed is the BARE `#trends` route — that is the page
 * shell's canonical spelling for it (pages/TrendFinder.tsx `go()`), and
 * `#trends/feed` would work but leave a non-canonical URL behind.
 */
export function goToTrendsTab(tab: TrendsSubTab): void {
  const next = tab === "feed" ? "#trends" : `#trends/${tab}`;
  if (window.location.hash !== next) window.location.hash = next;
}

/** Themes -> Feed, filtered by one theme. */
export function showTheme(theme: string): void {
  pendingTheme = theme;
  window.dispatchEvent(
    new CustomEvent<string>(TRENDS_THEME_EVENT, { detail: theme }),
  );
  goToTrendsTab("feed");
}

/** Read-and-clear. Called once by the feed on mount. */
export function consumePendingTheme(): string | null {
  const t = pendingTheme;
  pendingTheme = null;
  return t;
}
