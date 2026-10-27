/**
 * Typed facade over `api.generalTrends`.
 *
 * Same idea as `trendsApi.ts` for the competitor feed: the General trends tab
 * binds to the `/api/trends/general` namespace in exactly ONE place, fully
 * type-checked against `lib/api.ts` — no `as unknown as` cast anywhere, so a
 * rename there fails the build instead of throwing "x is not a function" on
 * click.
 */
import { api } from "../../lib/api";
import type { RoadmapTool } from "../yas/roadmap/types";
import type {
  GeneralTrendsQuery,
  TrendIdea,
  TrendPostStatus,
} from "../../lib/types";

export { humanError } from "./trendsApi";

export const generalTrendsApi = {
  stats: () => api.generalTrends.stats(),
  /** Configured sources + how each did on the latest run. */
  sources: () => api.generalTrends.sources(),
  /** Queue a background refresh; 409 while one is already running. */
  refresh: () => api.generalTrends.refresh(),
  latestRun: () => api.generalTrends.latestRun(),
  list: (params: GeneralTrendsQuery = {}) => api.generalTrends.list(params),
  updateTrend: (id: number, body: { status: TrendPostStatus }) =>
    api.generalTrends.patch(id, body.status),
  /** Locally cached thumbnail — source image URLs go stale, so prefer this. */
  thumbUrl: (id: number) => api.generalTrends.thumbUrl(id),
  rescore: (body: { ids?: number[] } = {}) => api.generalTrends.rescore(body.ids),
  /**
   * "Make this" step 1 → three ideas for how 42 could do this trend.
   * `feedback` = what the owner typed when they liked none of them;
   * `exclude` = every title shown so far, so the next three are different.
   *
   * The reply also carries `research`: what a live web lookup found the trend
   * ACTUALLY is, which is what the ideas were built from. It can be absent or
   * empty (a failed lookup degrades to ideas-without-it), so read it defensively.
   */
  brainstorm: (
    id: number,
    opts: { feedback?: string; exclude?: string[] } = {},
  ) => api.generalTrends.brainstorm(id, opts),
  /**
   * "Make this" step 2 → an AI brief for one Producer tool. Passing the idea
   * the owner picked makes the brief execute THAT idea; omitting it keeps the
   * original behaviour for any caller that never asked for ideas.
   */
  ideas: (id: number, tool: RoadmapTool, idea?: TrendIdea) =>
    api.generalTrends.ideas(id, tool, idea),
  themes: () => api.generalTrends.themes(),
};
