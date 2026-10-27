/**
 * Trend Finder domain types for the tab components.
 *
 * These are RE-EXPORTS of the canonical definitions in `lib/types.ts`, which is
 * where every other app type lives. This module previously redeclared them, and
 * the two copies had already drifted — `created_at` was `string` here and
 * `string | null` there, which is what the server actually returns. The
 * duplication only survived because the API facade reached the client through a
 * cast that disabled type checking. One definition, aliased where these
 * components read better with a shorter name.
 */
export type {
  TrendCompetitor,
  TrendHealth,
  TrendMediaType,
  TrendNewsHealth,
  TrendNewsRun,
  TrendPost,
  TrendPostStatus,
  TrendPostsList,
  TrendPrefill,
  TrendRegion,
  TrendResearchEngagement,
  TrendResearchFocus,
  TrendResearchGeo,
  TrendResearchRelevance,
  TrendResearchSize,
  TrendScan,
  TrendSort,
  TrendSource,
  TrendStats,
  TrendTheme,
  TrendWeights,
} from "../../lib/types";

import type {
  TrendCompetitorStatus,
  TrendMediaType,
  TrendPostStatus,
  TrendPrefill,
  TrendRegion,
  TrendResearchCandidate,
  TrendResearchFocus,
  TrendResearchRejected,
  TrendResearchResult,
  TrendScan,
  TrendSort,
  TrendSource,
} from "../../lib/types";

/** Local spellings — shorter to read inside these components. */
export type CompetitorStatus = TrendCompetitorStatus;
export type CompetitorSource = "manual" | "ai_research";
export type ScanStatus = TrendScan["status"];
export type ResearchCandidate = TrendResearchCandidate;
export type ResearchRejection = TrendResearchRejected;
export type ResearchResult = TrendResearchResult;
export type ResearchFocus = TrendResearchFocus;
export type Prefill = TrendPrefill;

/** Query string for GET /api/trends/posts. */
export interface TrendPostsQuery {
  sort?: TrendSort;
  status?: TrendPostStatus;
  region?: TrendRegion;
  media_type?: TrendMediaType;
  theme?: string;
  competitor_id?: number;
  min_fit?: number;
  /** category: omit for every source, which is what "All" means */
  source?: TrendSource;
  platform?: string;
  language?: string;
  q?: string;
  limit?: number;
  offset?: number;
  /** 0..1 opportunity-blend weights. */
  w_out?: number;
  w_fit?: number;
  w_uae?: number;
}
