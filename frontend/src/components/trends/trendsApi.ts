/**
 * Typed facade over `api.trends`.
 *
 * The `/api/trends` namespace on `lib/api.ts` is owned by another module; this
 * file is the ONE place the trend tabs bind to it, so a rename there is a
 * one-line fix here instead of a rewrite across three screens. The cast is
 * deliberate and narrow: the namespace is declared by its contract below, and
 * every call re-reads it through `ns()` so a not-yet-wired namespace produces a
 * legible error instead of "cannot read properties of undefined".
 */
import { api } from "../../lib/api";
import type { RoadmapTool } from "../yas/roadmap/types";
// An idea is the same object whichever feed proposed it; the three news-config
// bodies are the only request shapes this file names, because they are built by
// hand in a form rather than being handed straight back from a previous reply.
import type {
  TrendIdea,
  TrendNewsConfigPatch,
} from "../../lib/types";
// Only the types this module NAMES in its own signatures. Everything else flows
// through from `api.trends` already typed — an earlier version re-declared the
// whole set here, and eight of them outlived the code that used them.
import type {
  CompetitorStatus,
  ResearchFocus,
  TrendPostStatus,
  TrendPostsQuery,
  TrendRegion,
} from "./types";

export interface AddCompetitorBody {
  username: string;
  category?: string | null;
  region?: TrendRegion | null;
  why?: string | null;
  /** Record an "ignore" without ever calling Instagram. */
  exclude?: boolean;
}

export interface PatchCompetitorBody {
  status?: CompetitorStatus;
  category?: string | null;
  region?: TrendRegion | null;
  why?: string | null;
}

/**
 * A thin, FULLY TYPE-CHECKED adapter over `api.trends`.
 *
 * It maps the short names these components read nicely with onto the client's
 * canonical ones. There is deliberately no cast here: an earlier version reached
 * the namespace through `api as unknown as {…}`, which silently disabled
 * checking — six of the fifteen names didn't match and every Competitors
 * / Sentiment and Feed action would have thrown "x is not a function" on
 * click. Binding directly means a rename in api.ts fails the build instead.
 */
export const trendsApi = {
  health: () => api.trends.health(),
  stats: () => api.trends.stats(),
  competitors: (params: { status?: CompetitorStatus; region?: TrendRegion } = {}) =>
    api.trends.listCompetitors(params),
  addCompetitor: (body: AddCompetitorBody) => api.trends.addCompetitor(body),
  updateCompetitor: (id: number, body: PatchCompetitorBody) =>
    api.trends.patchCompetitor(id, body),
  deleteCompetitor: (id: number) => api.trends.deleteCompetitor(id),
  research: (body: { hint?: string; limit?: number } & ResearchFocus = {}) =>
    api.trends.research(body),
  scanCompetitor: (id: number, body: { max_posts?: number } = {}) =>
    api.trends.scanCompetitor(id, body),
  scan: (
    body: {
      competitor_ids?: number[];
      lookback_days?: number;
      max_posts?: number;
    } = {},
  ) => api.trends.startScan(body),
  latestScan: () => api.trends.latestScan(),
  posts: (params: TrendPostsQuery = {}) => api.trends.listPosts(params),
  updatePost: (id: number, body: { status: TrendPostStatus }) =>
    api.trends.patchPost(id, body.status),
  rescore: (body: { post_ids?: number[] } = {}) => api.trends.rescorePosts(body.post_ids),
  /**
   * "Make this" step 1 → three ideas for how 42 could do this post.
   * `feedback` = what the owner typed when they liked none of them;
   * `exclude` = every title shown so far, so the next three are different.
   *
   * Serves Industry Signal news items too — same endpoint, same shape. Unlike
   * the general-trends twin the reply carries no `research`: the caption and
   * the scorer's read of it are already on the card, so there is nothing to
   * look up first.
   */
  postBrainstorm: (
    postId: number,
    opts: { feedback?: string; exclude?: string[] } = {},
  ) => api.trends.postBrainstorm(postId, opts),
  /**
   * "Make this" step 2 → an AI brief for one Producer tool. Passing the idea
   * the owner picked makes the brief execute THAT idea; omitting it keeps the
   * original behaviour for any caller that never asked for ideas.
   */
  postIdeas: (postId: number, tool: RoadmapTool, idea?: TrendIdea) =>
    api.trends.postIdeas(postId, tool, idea),
  themes: () => api.trends.themes(),
  /** Locally cached thumbnail — IG CDN URLs expire, so prefer this. */
  thumbUrl: (id: number) => api.trends.thumbUrl(id),

  // ----- Industry Signal -----
  //
  // Five, down from thirteen. There is no feed catalogue to manage any more,
  // so there is nothing to add, test, reset or delete: the whole configuration
  // is one sentence saying what to look for.
  newsHealth: () => api.trends.newsHealth(),
  newsConfig: () => api.trends.newsConfig(),
  saveNewsConfig: (body: TrendNewsConfigPatch) => api.trends.saveNewsConfig(body),
  /** Rough note in, briefs out. Proposes only - nothing is saved until chosen. */
  suggestNewsBriefs: (notes: string) => api.trends.suggestNewsBriefs(notes),
  /** "Find news now". Fire-and-forget; watch `newsHealth` for the outcome. */
  newsFetch: (limit?: number) => api.trends.newsFetch(limit),
  newsCancel: () => api.trends.newsCancel(),

  /** What we follow + where stories have come from, for the Industry Insight
   *  screen. One call, because the screen prints them together. */
  newsWatch: () => api.trends.newsWatch(),
  newsWatchAdd: (value: string, kind?: "name" | "site") =>
    api.trends.newsWatchAdd(value, kind),
  newsWatchRemove: (id: number) => api.trends.newsWatchRemove(id),
};

/**
 * `http()` throws Error("404 Not found") — the status prefix is for logs, not
 * for people. Mirrors store.ts `_veHumanError`.
 */
// Moved to lib/format.ts — every surface that calls the API needs it, not just
// the trend tabs. Re-exported so the dozen import sites in this folder are
// unchanged and there is still exactly one implementation.
export { humanError } from "../../lib/format";
