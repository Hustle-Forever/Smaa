/**
 * Feature flags for the Trends section.
 *
 * One flag, one reason. Viral Social Trends (internally: general trends) is
 * shipped but not finished: the owner's test is whether a card names a format
 * people are RECREATING, and too many rows are still things that merely
 * happened. That is a research problem in source selection and clustering, not
 * a bug with a patch, so the surface is hidden until the answer is known rather
 * than left on screen teaching people to distrust the tab.
 *
 * Hidden, NOT removed. Nothing was deleted: the components, the API facade, the
 * backend router, the collector poller and every stored row are exactly where
 * they were, and the code paths that render the tab are still compiled and
 * type-checked — they are guarded, not stripped. Flipping the constant below to
 * `true` is the entire revert; there is no second place to remember.
 *
 * Deliberately annotated `: boolean` rather than left to inference. As a
 * literal `false` type, TypeScript would narrow every `flag && …` to `false`,
 * report the guarded branches as unreachable and start "correcting" the code
 * behind the flag — which is precisely the code that has to survive untouched
 * for the one-line revert to work.
 *
 * What this flag covers: every Viral Social Trends surface, the Reports panel
 * included — the scope selector no longer offers it, the "all streams" wording
 * counts two, and the basis stat is not drawn. That is not a cosmetic choice.
 * The backend has now STOPPED COLLECTING (`GENERAL_TRENDS_ENABLED` in
 * config.py), so a viral section could only report a frozen snapshot ageing out
 * of the window — a number that looks measured and is not.
 *
 * THE TWO HALVES MUST BE FLIPPED TOGETHER. This file is a static build and
 * cannot read the backend's config, so the split is unavoidable: turning this
 * on alone shows a tab reading from a collector that is not collecting, and
 * turning the backend on alone collects into a UI nobody can reach. Both, or
 * neither.
 */

/** Show the Viral Social Trends tab and the feed channel that embeds it. */
export const VIRAL_SOCIAL_TRENDS_ENABLED: boolean = false;

/**
 * Show the Themes sub-tab.
 *
 * Off because the owner does not want it on the bar — a roll-up of the same
 * discoveries the Feed already lists, which earns a permanent tab only if
 * people actually read it. Nothing behind it changes: `ThemesTab`,
 * `GET /api/trends/themes` and the theme labels the scorer writes all stay, and
 * the Feed keeps its own "All themes" FILTER, which is the part people use.
 *
 * Cheaper to reverse than the viral switch: this one is frontend-only, because
 * nothing collects on its behalf. Flip it and the tab is back.
 *
 * Same `: boolean` annotation, for the same reason as above.
 */
export const THEMES_TAB_ENABLED: boolean = false;

