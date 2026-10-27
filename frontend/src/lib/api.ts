// Tiny typed wrapper around fetch + EventSource.
//
// All requests are same-origin thanks to Vite's proxy (vite.config.ts), so
// no CORS handling is needed in dev. In a build/deploy, swap BASE if the
// frontend and backend are served from different origins.

import type {
  AgentId,
  AssetList,
  BrandDocDetail,
  BrandDocList,
  BrandResponse,
  Campaign,
  CampaignCreateRequest,
  CampaignDetail,
  CampaignItem,
  CampaignList,
  CampaignPatchRequest,
  CampaignReviewQueue,
  CampaignSummary,
  Connections,
  DialogueLanguage,
  ExtractFramesResult,
  FrameExtractionMode,
  FromFrameRequest,
  Health,
  InstagramOwnSummary,
  ReelBlock,
  ReelChoicePatch,
  ReelMusicLevel,
  TalkingHeadGenerateScriptRequest,
  TalkingHeadRetryRequest,
  TalkingHeadV2EditRequest,
  TalkingHeadV2GeneratePreviewsRequest,
  TalkingHeadV2ExcludeRequest,
  TalkingHeadV2BatchRequest,
  TalkingHeadRun,
  TalkingHeadPreviousCard,
  VideoEditAiJob,
  VideoEditAiProviders,
  VideoEditAiStatus,
  VideoEditVoiceClone,
  VideoEditorAudio,
  VideoEditorFont,
  VideoEditorProject,
  VideoEditorRevision,
  VideoEditorSaveRequest,
  VideoSource,
  VideoSourceList,
  YasGenerateRequest,
  YasGeneration,
  YasGenerationBatchResponse,
  YasLibraryList,
  YasImageModel,
  YasStats,
  YasPosterOnlyGeneration,
  YasPosterOnlyGenerateRequest,
  YasPosterBatchResponse,
  YasHumanCharacterGeneration,
  YasHumanCharacterGenerateRequest,
  YasHumanCharacterBatchResponse,
  YasAnimationGen,
  YasAnimationBatchResponse,
  YasAnimationFrameRequest,
  YasAnimationAnimateRequest,
  YasAnimationFromImageRequest,
  YasGeneralVideoGen,
  YasGeneralVideoEngine,
  YasGeneralVideoEngines,
  SeedanceAspect,
  SeedanceResolution,
  YasTemplateFillRequest,
  YasUploadRef,
  InstagramHealth,
  InstagramPostDetail,
  InstagramPostsList,
  LibraryIdeaRow,
  LibraryScriptRow,
  LibraryVideoRow,
  FolderImportResult,
  FolderPreviewResult,
  ProducerAssetCategory,
  ProducerAssetDetail,
  ProducerAssetKind,
  ProducerAssetRow,
  ProducerAssetsList,
  ProducerBudget,
  ProducerComposition,
  ProducerIdea,
  ProducerMode,
  ProducerReference,
  ProducerRunDetail,
  ProducerRunRow,
  ProducerScript,
  ProducerVideo,
  ProducerVideoType,
  RunDetail,
  RunStartResponse,
  RunSummary,
  Settings,
  SettingsPatch,
  GeneralTrend,
  GeneralTrendDetail,
  GeneralTrendRun,
  GeneralTrendSource,
  GeneralTrendStats,
  GeneralTrendTheme,
  GeneralTrendsList,
  GeneralTrendsQuery,
  PostIdeaList,
  ReportItemKind,
  GeneratedReport,
  ReportMode,
  ReportScope,
  ReportGrain,
  ReportPeriod,
  SavedReport,
  SocialReport,
  TrendCompetitor,
  TrendCompetitorStatus,
  TrendHealth,
  TrendIdea,
  TrendIdeaList,
  TrendNewsBriefSuggestion,
  TrendNewsConfig,
  TrendNewsConfigPatch,
  TrendNewsHealth,
  TrendNewsOutlet,
  TrendNewsWatch,
  TrendPost,
  TrendPostDetail,
  TrendPostsList,
  TrendPostStatus,
  TrendPrefill,
  TrendRegion,
  TrendResearchFocus,
  TrendResearchResult,
  TrendScan,
  TrendSort,
  TrendSource,
  TrendStats,
  TrendTheme,
  WebsiteList,
} from "./types";

const BASE = ""; // same-origin via Vite proxy

async function http<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${BASE}${path}`, {
    ...init,
    headers: {
      "Content-Type": "application/json",
      ...(init?.headers || {}),
    },
  });
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const body = await res.json();
      detail = body?.detail || JSON.stringify(body);
    } catch {
      /* ignore */
    }
    throw new Error(`${res.status} ${detail}`);
  }
  if (res.status === 204) return undefined as T;
  return res.json() as Promise<T>;
}

// ----- Trend Finder (/api/trends) -----
//
// Watches competitor Instagram accounts, finds the posts that OVERPERFORMED
// for their own account, and scores how practical each idea is for 42.
// Flow: add/research competitors → scan (background, poll scan.latest) →
// read the ranked feed. `outperformance` is the hero metric, not raw likes.

const trendsBase = {
  // Whether scanning can work at all. `ok:false` → show the setup banner and
  // use `reason` ("not_configured" | "unreachable") to say what to do.
  health: () => http<TrendHealth>("/api/trends/health"),

  stats: () => http<TrendStats>("/api/trends/stats"),

  // ----- Competitors -----

  listCompetitors: (
    params: { status?: TrendCompetitorStatus; region?: TrendRegion } = {},
  ) => {
    const q = new URLSearchParams();
    if (params.status) q.set("status", params.status);
    if (params.region) q.set("region", params.region);
    const qs = q.toString();
    return http<{ items: TrendCompetitor[] }>(
      `/api/trends/competitors${qs ? "?" + qs : ""}`,
    );
  },

  // Validates against Instagram BEFORE saving: 422 carries a plain-language
  // message (Business Discovery only reaches Business/Creator accounts — this
  // is the common case), 409 when the handle is already tracked.
  // `exclude: true` records an "ignore" WITHOUT calling Instagram.
  // The optional fields accept null as well as undefined: the server models
  // them as Optional[...] = None, and a form that clears a field naturally
  // produces null.
  addCompetitor: (body: {
    username: string;
    category?: string | null;
    region?: TrendRegion | null;
    why?: string | null;
    exclude?: boolean;
  }) =>
    http<{ competitor: TrendCompetitor }>("/api/trends/competitors", {
      method: "POST",
      body: JSON.stringify(body),
    }),

  // `status` is the include / don't-include toggle.
  patchCompetitor: (
    id: number,
    body: {
      status?: TrendCompetitorStatus;
      category?: string | null;
      region?: TrendRegion | null;
      why?: string | null;
    },
  ) =>
    http<{ competitor: TrendCompetitor }>(`/api/trends/competitors/${id}`, {
      method: "PATCH",
      body: JSON.stringify(body),
    }),

  // Hard delete — cascades this competitor's discovered posts. To just stop
  // watching (keeping the row + its posts), PATCH status:"excluded" instead.
  deleteCompetitor: (id: number) =>
    http<{ ok: boolean }>(`/api/trends/competitors/${id}`, { method: "DELETE" }),

  // AI suggestions, each verified against the real API first. `candidates`
  // are reachable; `rejected` explains per handle why it couldn't be used.
  // The focus dials steer which accounts the model proposes; the server
  // ignores unknown keys and defaults each dial, so they are all optional.
  research: (body: { hint?: string; limit?: number } & TrendResearchFocus = {}) =>
    http<TrendResearchResult>("/api/trends/competitors/research", {
      method: "POST",
      body: JSON.stringify(body),
    }),

  scanCompetitor: (id: number, body: { max_posts?: number } = {}) =>
    http<{ scan: TrendScan }>(`/api/trends/competitors/${id}/scan`, {
      method: "POST",
      body: JSON.stringify(body),
    }),

  // ----- Scans -----

  // Queues a background scan and returns immediately (409 if one is already
  // running). Poll `latestScan()` while scan.active.
  // `max_posts` is the ceiling per account, picked on the button every time.
  startScan: (
    body: {
      competitor_ids?: number[];
      lookback_days?: number;
      max_posts?: number;
    } = {},
  ) =>
    http<{ scan: TrendScan }>("/api/trends/scan", {
      method: "POST",
      body: JSON.stringify(body),
    }),

  // `auto_scan_hours`: how often the server reads every account by itself, 0
  // when it never does.
  latestScan: () =>
    http<{ scan: TrendScan | null; auto_scan_hours?: number }>("/api/trends/scan/latest"),

  // ----- Posts -----

  // The ranked feed. w_out/w_fit/w_uae are the 0..1 opportunity-blend weights —
  // the server re-blends on the fly, so moving a slider re-ranks instantly
  // with no re-scan and no re-score.
  listPosts: (
    params: {
      sort?: TrendSort;
      status?: TrendPostStatus;
      region?: TrendRegion;
      media_type?: string;
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
      w_out?: number;
      w_fit?: number;
      w_uae?: number;
    } = {},
  ) => {
    const qp = new URLSearchParams();
    if (params.sort) qp.set("sort", params.sort);
    if (params.status) qp.set("status", params.status);
    if (params.region) qp.set("region", params.region);
    if (params.media_type) qp.set("media_type", params.media_type);
    if (params.theme) qp.set("theme", params.theme);
    if (params.competitor_id != null) qp.set("competitor_id", String(params.competitor_id));
    if (params.min_fit != null) qp.set("min_fit", String(params.min_fit));
    if (params.source) qp.set("source", params.source);
    if (params.platform) qp.set("platform", params.platform);
    if (params.language) qp.set("language", params.language);
    if (params.q) qp.set("q", params.q);
    if (params.limit != null) qp.set("limit", String(params.limit));
    if (params.offset != null) qp.set("offset", String(params.offset));
    if (params.w_out != null) qp.set("w_out", String(params.w_out));
    if (params.w_fit != null) qp.set("w_fit", String(params.w_fit));
    if (params.w_uae != null) qp.set("w_uae", String(params.w_uae));
    const qs = qp.toString();
    return http<TrendPostsList>(`/api/trends/posts${qs ? "?" + qs : ""}`);
  },

  patchPost: (id: number, status: TrendPostStatus) =>
    http<{ post: TrendPost }>(`/api/trends/posts/${id}`, {
      method: "PATCH",
      body: JSON.stringify({ status }),
    }),

  // One post, everything we know — what the detail dialog reads. Same row as
  // the feed carries, fetched fresh so an open dialog is never showing numbers
  // from a list request made twenty minutes ago. 404 when the id is gone.
  postDetail: (id: number) =>
    http<TrendPostDetail>(`/api/trends/posts/${id}`),

  // Locally-cached thumbnail. ALWAYS use this rather than post.thumbnail_url —
  // Instagram CDN URLs expire, the stored copy is the only one that keeps loading.
  thumbUrl: (postId: number) => `${BASE}/api/trends/posts/${postId}/thumb`,

  // Re-run the AI scoring pass — e.g. after the brand profile changed.
  rescorePosts: (post_ids?: number[]) =>
    http<{ scored: number }>("/api/trends/posts/rescore", {
      method: "POST",
      body: JSON.stringify({ post_ids: post_ids ?? null }),
    }),

  // "Make this" step 1 → THREE different ideas for how 42 could do this post.
  // Serves the competitor feed AND Industry Signal, which is why the reply is
  // `PostIdeaList` and not `TrendIdeaList`: there is no `research` to return,
  // because the post's own caption and the scorer's read of it are already
  // known — only a bare hashtag has to be looked up before anyone can write
  // about it. `feedback` is what the owner typed when they liked none of the
  // three; `exclude` is every title shown so far, so a re-roll comes back
  // different instead of rewording the same three.
  postBrainstorm: (
    postId: number,
    opts: { feedback?: string; exclude?: string[] } = {},
  ) =>
    http<PostIdeaList>(`/api/trends/posts/${postId}/brainstorm`, {
      method: "POST",
      body: JSON.stringify({
        feedback: opts.feedback ?? "",
        exclude: opts.exclude ?? [],
      }),
    }),

  // "Make this" step 2 → an AI brief shaped for ONE Producer tool. 422 when the
  // tool key is unknown or the model is unconfigured; 502 when the call fails.
  //
  // `idea` is additive, and the no-idea body is a COMPATIBILITY PROMISE: with
  // `idea` omitted this still sends exactly `{ tool }`, byte-identical to what
  // shipped before ideas existed, so every call site that never asks for them
  // — and any backend that has not learned the field — behaves as it always
  // did. Supplied, the brief must execute THAT idea instead of inventing a
  // fourth one.
  postIdeas: (postId: number, tool: string, idea?: TrendIdea) =>
    http<{ prefill: TrendPrefill }>(`/api/trends/posts/${postId}/ideas`, {
      method: "POST",
      body: JSON.stringify(idea ? { tool, idea } : { tool }),
    }),

  // ----- Themes -----

  themes: () => http<{ items: TrendTheme[] }>("/api/trends/themes"),

  // ----- Industry Signal -----
  //
  // Five calls, down from thirteen. The eight that managed a feed catalogue
  // went with the catalogue: stories are found by one grounded search a
  // morning, so there is nothing to add, test, reset or delete. What is left is
  // one sentence saying what to look for, and a log of what that found.

  // Did it run, what did it find, and when does it run next. Drives both the
  // Configure screen's run log and the status line under the feed's pills.
  // Never throws for a caller: a failure comes back as `ok: false` with enough
  // shape to render, because a status line that breaks the page is worse than
  // one saying it does not know.
  newsHealth: () => http<TrendNewsHealth>("/api/trends/news/health"),

  // What we look for, how many stories a day, and whether it is switched on.
  newsConfig: () => http<{ config: TrendNewsConfig }>("/api/trends/news/config"),

  // Save any subset. `JSON.stringify` drops undefined-valued keys and the
  // server applies only what it receives, so a screen that toggled the switch
  // does not resend the brief and quietly overwrite an edit made in another
  // tab. Out-of-range values come back as a 422 with a readable sentence
  // rather than being silently clamped to something else.
  saveNewsConfig: (body: TrendNewsConfigPatch) =>
    http<{ config: TrendNewsConfig }>("/api/trends/news/config", {
      method: "PUT",
      body: JSON.stringify(body),
    }),

  // Rough note in, two or three complete briefs out. Propose-and-pick: nothing
  // is saved until the person chooses one, because a button that silently
  // rewrote the one field controlling the whole feature would be the worst
  // kind of helpful. One model call, so the UI must show it working.
  suggestNewsBriefs: (notes: string) =>
    http<{ suggestions: TrendNewsBriefSuggestion[] }>(
      "/api/trends/news/brief/suggest",
      { method: "POST", body: JSON.stringify({ notes }) },
    ),

  // "Find stories". The ONLY way a search ever starts: there is no timer behind
  // this feed any more. `limit` is the most stories to keep, chosen on the
  // button every time. Fire-and-forget, because the run makes two model calls;
  // watch `newsHealth` for the outcome. `started:false` means one was already
  // running, which is a sentence, not an error.
  newsFetch: (limit?: number) =>
    http<{ started: boolean; note: string }>("/api/trends/news/fetch", {
      method: "POST",
      body: JSON.stringify(limit != null ? { limit } : {}),
    }),

  // Stop a search, and clear anything left open by a crash. The second half is
  // why this is not only an abort: a run row stranded by a restart reads on
  // screen as "a search is running" for ever, and this is the way out of it.
  newsCancel: () =>
    http<{ stopped: boolean; note: string }>("/api/trends/news/cancel", {
      method: "POST",
    }),

  // What we follow, plus where the stories have actually come from. One call
  // because the screen shows them side by side: this is what we ask for, and
  // this is what came back.
  newsWatch: () =>
    http<{ watch: TrendNewsWatch[]; outlets: TrendNewsOutlet[] }>(
      "/api/trends/news/watch",
    ),

  // Follow one more thing. A 422 carries the sentence to print, so the caller
  // shows the server's words rather than inventing its own.
  // `kind` is the box it was typed into. The text still decides what it is;
  // the box only lets the server refuse a name typed where a website was
  // asked for, with a sentence the screen prints.
  newsWatchAdd: (value: string, kind?: "name" | "site") =>
    http<{ item: TrendNewsWatch }>("/api/trends/news/watch", {
      method: "POST",
      body: JSON.stringify(kind ? { value, kind } : { value }),
    }),

  newsWatchRemove: (id: number) =>
    http<{ removed: boolean }>(`/api/trends/news/watch/${id}`, {
      method: "DELETE",
    }),

};

// One canonical spelling per endpoint. `components/trends/trendsApi.ts` adapts
// these names for the tab components and is fully type-checked against them, so
// a rename here fails the build instead of throwing at click time.
const trends = trendsBase;

// ----- General trends (/api/trends/general) -----
//
// The second kind of trend: what the whole internet is doing right now, pulled
// from free public sources (Google Trends, Bluesky, Reddit, KYM, Wikipedia,
// YouTube, X), clustered into ONE card per trend and scored for 42.
// Flow: refresh (background, poll latestRun) → read the ranked list. `velocity`
// (0..1 heat) is the hero metric; fit/uae come from the AI pass.

const generalTrends = {
  stats: () => http<GeneralTrendStats>("/api/trends/general/stats"),

  // Configured sources + how each did on the latest run (ok/count/error).
  sources: () =>
    http<{ items: GeneralTrendSource[] }>("/api/trends/general/sources"),

  // Queues a background refresh and returns immediately (409 if one is already
  // running). Poll `latestRun()` while run.active.
  refresh: () =>
    http<{ run: GeneralTrendRun }>("/api/trends/general/refresh", {
      method: "POST",
      body: "{}",
    }),

  latestRun: () =>
    http<{ run: GeneralTrendRun | null }>("/api/trends/general/runs/latest"),

  // The ranked list. w_vel/w_fit/w_uae are the 0..1 opportunity-blend weights —
  // the server re-blends on the fly, so moving a slider re-ranks instantly.
  list: (params: GeneralTrendsQuery = {}) => {
    const qp = new URLSearchParams();
    if (params.sort) qp.set("sort", params.sort);
    if (params.status) qp.set("status", params.status);
    if (params.region) qp.set("region", params.region);
    if (params.source) qp.set("source", params.source);
    if (params.theme) qp.set("theme", params.theme);
    if (params.kind) qp.set("kind", params.kind);
    if (params.min_fit != null) qp.set("min_fit", String(params.min_fit));
    if (params.q) qp.set("q", params.q);
    if (params.include_sensitive != null)
      qp.set("include_sensitive", params.include_sensitive ? "true" : "false");
    if (params.include_filtered != null)
      qp.set("include_filtered", params.include_filtered ? "true" : "false");
    if (params.all_kinds != null)
      qp.set("all_kinds", params.all_kinds ? "true" : "false");
    // Both windows are omitted unless the user widens them, so the server's
    // own defaults (7-day feed / 30-day content age) stay the single source of
    // truth for "current" rather than being pinned by whatever the tab shipped.
    if (params.days != null) qp.set("days", String(params.days));
    if (params.content_days != null)
      qp.set("content_days", String(params.content_days));
    if (params.limit != null) qp.set("limit", String(params.limit));
    if (params.offset != null) qp.set("offset", String(params.offset));
    if (params.w_vel != null) qp.set("w_vel", String(params.w_vel));
    if (params.w_fit != null) qp.set("w_fit", String(params.w_fit));
    if (params.w_uae != null) qp.set("w_uae", String(params.w_uae));
    const qs = qp.toString();
    return http<GeneralTrendsList>(
      `/api/trends/general/items${qs ? "?" + qs : ""}`,
    );
  },

  patch: (id: number, status: TrendPostStatus) =>
    http<{ trend: GeneralTrend }>(`/api/trends/general/items/${id}`, {
      method: "PATCH",
      body: JSON.stringify({ status }),
    }),

  // One trend, everything we know: the row, EVERY sighting (the list payload
  // only embeds a short preview), and the cached "what this actually is"
  // lookup when one was ever run. 404 when the id is gone.
  detail: (id: number) =>
    http<GeneralTrendDetail>(`/api/trends/general/items/${id}`),

  // Locally-cached thumbnail; the source image URL is only the fallback.
  thumbUrl: (id: number) => `${BASE}/api/trends/general/items/${id}/thumb`,

  // Re-run the AI scoring pass (all trends when `ids` is omitted).
  rescore: (ids?: number[]) =>
    http<{ scored: number }>("/api/trends/general/items/rescore", {
      method: "POST",
      body: JSON.stringify({ ids: ids ?? null }),
    }),

  // "Make this" step 1 → THREE different ideas for how 42 could do this trend.
  // `feedback` is what the owner typed when they liked none of them; `exclude`
  // is every title shown so far, so a re-roll comes back different instead of
  // rewording the same three.
  brainstorm: (
    id: number,
    opts: { feedback?: string; exclude?: string[] } = {},
  ) =>
    http<TrendIdeaList>(`/api/trends/general/items/${id}/brainstorm`, {
      method: "POST",
      body: JSON.stringify({
        feedback: opts.feedback ?? "",
        exclude: opts.exclude ?? [],
      }),
    }),

  // "Make this" step 2 → an AI brief shaped for ONE Producer tool; same
  // TrendPrefill envelope as the competitor feed (post_id = trend id).
  // `idea` is additive: omitted, the body is byte-identical to what shipped
  // before and the model picks its own angle; supplied, the brief must execute
  // THAT idea.
  ideas: (id: number, tool: string, idea?: TrendIdea) =>
    http<{ prefill: TrendPrefill }>(`/api/trends/general/items/${id}/ideas`, {
      method: "POST",
      body: JSON.stringify(idea ? { tool, idea } : { tool }),
    }),

  themes: () =>
    http<{ items: GeneralTrendTheme[] }>("/api/trends/general/themes"),
};

// ----- Social-listening report (/api/reports) -----
//
// Rolls the three signal streams (competitor posts, Industry Signal, Viral
// Social Trends) into ONE payload that both the on-screen preview and the PDF
// render from, so the two can never disagree about a number.
//
// `days` is 1..90; the server clamps. `mode` picks the depth: "simple" is the
// three-group signal list, "full" is the 14-section Meltwater-shaped report.

//
// `scope` is optional on every call below, and OMITTED from the query string
// when it is not passed. That keeps the no-scope URLs byte-identical to what
// shipped before, so a backend that has not learned the parameter yet answers
// exactly as it always did instead of 422-ing the whole tab.

/** `&scope=` — only when a caller actually chose one. */
function scopeParam(scope?: ReportScope): string {
  return scope ? `&scope=${encodeURIComponent(scope)}` : "";
}

/**
 * `&end=` — the LAST DAY of the window, omitted for one that ends today.
 *
 * `days` is how wide the window is; this is where it sits. Omitting it is the
 * trailing report every caller got before windows could be chosen, and today's
 * date is deliberately sent as nothing: the backend files "the 7 days ending
 * today" and "the last 7 days" under one key, and a client that sent both
 * spellings would make the cache miss its own entries.
 */
function endParam(end?: string | null): string {
  const day = (end || "").slice(0, 10);
  if (!day || day === new Date().toISOString().slice(0, 10)) return "";
  return `&end=${encodeURIComponent(day)}`;
}

/** `&days=` — item reports let the server pick its own window. */
function daysParam(days?: number): string {
  return days == null ? "" : `&days=${encodeURIComponent(String(days))}`;
}

const reports = {
  // BUILDS ON EVERY CALL, and nothing stores the result. This pair is what made
  // opening the Reports tab cost two model passes every time. Prefer `generate`
  // + `saved` + `savedPdfUrl` below, which build once and keep it; these two
  // remain for a caller that genuinely wants a fresh document and no record of
  // it.
  //
  // `fast` skips BOTH model passes, exactly as it does on `pdfUrl`. Every
  // counted number is identical; only the two written sections come back
  // saying they were skipped. The Reports tab uses it for the cheap half of
  // its pair of fetches, where the only thing wanted is the measured signal
  // list and waiting ~35s for prose nobody reads there would be waste.
  get: (mode: ReportMode, days: number, scope?: ReportScope, fast?: boolean) =>
    http<SocialReport>(
      `/api/reports/social-listening?mode=${mode}&days=${days}` +
        `${scopeParam(scope)}${fast ? "&fast=true" : ""}`,
    ),

  // A URL, deliberately NOT a fetch. The response carries
  // `Content-Disposition: attachment; filename=…`, and the ONLY way the browser
  // uses that filename is if the browser does the request — pulling the bytes
  // into a Blob here would throw the server's name away and download
  // "download.pdf". Point an <a href> at this.
  // `fast` skips BOTH model passes and returns the measured document at once.
  // It is not a lesser report: every counted number is identical, and the two
  // written sections say they were skipped rather than sitting empty. Measured
  // on the dev box: a contended `competitors/full` took 270s, and 2.9s with it.
  pdfUrl: (
    mode: ReportMode,
    days: number,
    scope?: ReportScope,
    fast?: boolean,
  ) =>
    `${BASE}/api/reports/social-listening.pdf?mode=${mode}&days=${days}` +
    `${scopeParam(scope)}${fast ? "&fast=true" : ""}`,

  // ----- Reports that are KEPT -----
  //
  // A report is built when someone asks for one, and then it stays built. The
  // tab used to fire two builds on mount and two more on every window change,
  // so simply opening it cost two model passes every time; `saved` replaces
  // that with an index lookup and `generate` is the only thing that can spend.

  /**
   * Every window that may be asked for, newest first.
   *
   * THE WHOLE MENU, and there is nothing else on it: a report covers a whole
   * week, a half month or a calendar month, and only once that period has
   * finished. Each entry says how many records are inside it and, when one has
   * already been built, the id of that report, so the picker can send a reader
   * to the document instead of offering to build a second copy of it.
   */
  periods: (scope: ReportScope, grain: ReportGrain, limit = 26) =>
    http<{
      scope: ReportScope;
      grain: ReportGrain;
      today: string;
      items: ReportPeriod[];
    }>(
      `/api/reports/periods?grain=${grain}&limit=${limit}` +
        `${scope ? `&scope=${encodeURIComponent(scope)}` : ""}`,
    ),

  /** Every report already built, newest first, WITHOUT the documents. */
  saved: (scope?: ReportScope, limit = 40) =>
    http<{ items: SavedReport[] }>(
      `/api/reports/saved?limit=${limit}` +
        (scope ? `&scope=${encodeURIComponent(scope)}` : ""),
    ),

  /** One stored report, document included. */
  savedOne: (id: number) => http<SavedReport>(`/api/reports/saved/${id}`),

  /**
   * Build this window's report, or hand back the one already built.
   *
   * `refresh` is the explicit "do it again anyway", for when the collectors
   * have run since. Without it, a second ask on the same day is free and comes
   * back `cached: true`.
   */
  generate: (
    days: number,
    scope: ReportScope,
    opts: { mode?: ReportMode; refresh?: boolean; end?: string | null } = {},
  ) =>
    http<GeneratedReport>(
      `/api/reports/generate?mode=${opts.mode ?? "full"}&days=${days}` +
        `${scopeParam(scope)}${endParam(opts.end)}` +
        `${opts.refresh ? "&refresh=true" : ""}`,
      { method: "POST" },
    ),

  /**
   * The stored report as a PDF. A URL for an <a href>, never a fetch (see
   * `pdfUrl`).
   *
   * THE SAME DOCUMENT the screen is showing. The old download rebuilt the
   * report from scratch, so the PDF and the page it came from were two separate
   * builds minutes apart, with two separate narrative passes — which is exactly
   * why they did not match.
   */
  savedPdfUrl: (id: number) => `${BASE}/api/reports/saved/${id}.pdf`,

  /** Forget one stored report. Costs nothing that cannot be rebuilt. */
  remove: (id: number) =>
    http<{ deleted: number }>(`/api/reports/saved/${id}`, { method: "DELETE" }),

  // ----- ONE row, on its own -----
  //
  // Same {meta, basis, sections} envelope as the window report, so the section
  // renderers draw it unchanged. `kind=post` covers a competitor post AND an
  // Industry Signal story; `kind=trend` is a viral social trend. A row that is
  // gone is a 404, not an empty report.
  item: (kind: ReportItemKind, id: number, mode: ReportMode, days?: number) =>
    http<SocialReport>(
      `/api/reports/item?kind=${kind}&id=${encodeURIComponent(String(id))}&mode=${mode}${daysParam(days)}`,
    ),

  // Same rule as `pdfUrl`: a URL for an <a href>, never a fetch.
  itemPdfUrl: (kind: ReportItemKind, id: number, mode: ReportMode, days?: number) =>
    `${BASE}/api/reports/item.pdf?kind=${kind}&id=${encodeURIComponent(String(id))}&mode=${mode}${daysParam(days)}`,
};

// ----- Health / Settings / Brand -----

// ----- Trend Radar (/api/radar) -----
//
// The daily AI + coding top 10 for 42. `refresh` returns at once and the build
// runs in the background: poll `get()` while `building` is true.
export interface RadarCard {
  title: string;
  url: string;
  source: string;
  official: boolean;
  category: string;
  important: number;
  good_for_42: number;
  post_it: number;
  score: number;
  why: string;
  idea: string;
  when: string | null;
}
export interface Radar {
  date: string | null;
  built_at: string | null;
  items: RadarCard[];
  is_today: boolean;
  building: boolean;
  error: string | null;
}
const radar = {
  get: () => http<Radar>("/api/radar"),
  refresh: () => http<Radar>("/api/radar/refresh", { method: "POST", body: "{}" }),
};

export const api = {
  health: () => http<Health>("/api/health"),

  settings: () => http<Settings>("/api/settings"),
  updateSettings: (patch: SettingsPatch) =>
    http<Settings>("/api/settings", {
      method: "PUT",
      body: JSON.stringify(patch),
    }),

  // Is posting wired up, and will reminder emails send? Read-only and
  // secret-free: the token and the SMTP password are environment values and
  // stay there. This only answers "does it work", which is the part a person
  // on the Settings screen can act on.
  connections: () => http<Connections>("/api/settings/connections"),

  brand: () => http<BrandResponse>("/api/brand"),

  website: (limit = 50) =>
    http<WebsiteList>(`/api/website?limit=${limit}`),

  assets: (params: { type?: string; page_id?: number; limit?: number; only_completed?: boolean } = {}) => {
    const q = new URLSearchParams();
    if (params.type) q.set("type", params.type);
    if (params.page_id != null) q.set("page_id", String(params.page_id));
    if (params.limit != null) q.set("limit", String(params.limit));
    if (params.only_completed) q.set("only_completed", "true");
    const qs = q.toString();
    return http<AssetList>(`/api/assets${qs ? "?" + qs : ""}`);
  },

  // ----- Agent runs (any of the 3 agents share this shape) -----

  startRun: (agent: AgentId, body: Record<string, unknown> = {}) =>
    http<RunStartResponse>(`/api/agents/${agent}/run`, {
      method: "POST",
      body: JSON.stringify(body),
    }),

  listRuns: (agent: AgentId, limit = 20) =>
    http<{ items: RunSummary[] }>(`/api/agents/${agent}/runs?limit=${limit}`),

  getRun: (agent: AgentId, id: number) =>
    http<RunDetail>(`/api/agents/${agent}/runs/${id}`),

  cancelRun: (agent: AgentId, id: number) =>
    http<{ run_id: number; cancel_requested: boolean }>(
      `/api/agents/${agent}/runs/${id}/cancel`,
      { method: "POST" },
    ),

  // ----- Agent 2: brand docs -----

  documents: (limit = 50) =>
    http<BrandDocList>(`/api/agents/brand-doc-ingest/documents?limit=${limit}`),

  // Full content of one ingested document (raw_text + AI summary/description)
  // for the Recent-uploads detail drawer.
  document: (id: number) =>
    http<BrandDocDetail>(`/api/agents/brand-doc-ingest/documents/${id}`),

  // Inline URL for the ORIGINAL uploaded file (real preview — image/PDF).
  documentFileUrl: (id: number) =>
    `${BASE}/api/agents/brand-doc-ingest/documents/${id}/file`,

  upload: async (params: {
    files: File[];
    free_text?: string;
    uploader_note?: string;
  }) => {
    const form = new FormData();
    for (const f of params.files) form.append("files", f, f.name);
    if (params.free_text) form.append("free_text", params.free_text);
    if (params.uploader_note) form.append("uploader_note", params.uploader_note);
    const res = await fetch(`${BASE}/api/agents/brand-doc-ingest/upload`, {
      method: "POST",
      body: form,
    });
    if (!res.ok) {
      let detail = res.statusText;
      try {
        const j = await res.json();
        detail = j?.detail || JSON.stringify(j);
      } catch { /* */ }
      throw new Error(`${res.status} ${detail}`);
    }
    return (await res.json()) as RunStartResponse & {
      inserted_doc_ids: number[];
      skipped_files: string[];
    };
  },

  // Bulk-friendly: store ONE file (no run kicked). The caller uploads many of
  // these with bounded concurrency (for 1000+ videos). Videos are then drained
  // by the brand_video_poller; non-video docs are processed via runIngest().
  uploadFile: async (file: File) => {
    const form = new FormData();
    form.append("file", file, file.name);
    const res = await fetch(`${BASE}/api/agents/brand-doc-ingest/upload-file`, {
      method: "POST",
      body: form,
    });
    if (!res.ok) {
      let detail = res.statusText;
      try {
        const j = await res.json();
        detail = j?.detail || JSON.stringify(j);
      } catch { /* */ }
      throw new Error(`${res.status} ${detail}`);
    }
    return (await res.json()) as { doc_id: number; kind: string; bytes: number };
  },

  // Process already-uploaded (non-video) docs + re-synthesise. No new files.
  runIngest: (body?: { run_synthesis?: boolean }) =>
    http<RunStartResponse>("/api/agents/brand-doc-ingest/run", {
      method: "POST",
      body: JSON.stringify({ run_synthesis: true, ...(body || {}) }),
    }),

  // Re-queue failed brand videos (e.g. after fixing GCP creds).
  retryFailedVideos: () =>
    http<{ requeued: number }>("/api/agents/brand-doc-ingest/retry-failed-videos", {
      method: "POST",
    }),

  // Aggregate ingest counts (drives the bulk-upload progress UI).
  ingestStatus: () =>
    http<{
      overall: { uploaded: number; processing: number; completed: number; failed: number; total: number };
      video: { uploaded: number; processing: number; completed: number; failed: number; total: number; pending: number };
    }>("/api/agents/brand-doc-ingest/status"),

  // ----- Instagram ingest (Agent 5) -----

  instagramIngest: {
    health: () => http<InstagramHealth>("/api/agents/instagram-ingest/health"),

    run: (
      body: {
        target_username?: string;
        max_posts?: number | null;
        metrics_only?: boolean;
        skip_phases?: string[];
        force_re_download?: boolean;
        force_reclassify?: boolean;
        page_size?: number;
      } = {},
    ) =>
      http<RunStartResponse>("/api/agents/instagram-ingest/run", {
        method: "POST",
        body: JSON.stringify(body),
      }),

    listPosts: (
      params: { limit?: number; offset?: number; username?: string; order?: "newest" | "engagement"; category?: string } = {},
    ) => {
      const q = new URLSearchParams();
      if (params.limit != null) q.set("limit", String(params.limit));
      if (params.offset != null) q.set("offset", String(params.offset));
      if (params.username) q.set("username", params.username);
      if (params.order) q.set("order", params.order);
      if (params.category) q.set("category", params.category);
      const qs = q.toString();
      return http<InstagramPostsList>(
        `/api/agents/instagram-ingest/posts${qs ? "?" + qs : ""}`,
      );
    },

    getPost: (id: number) =>
      http<InstagramPostDetail>(`/api/agents/instagram-ingest/posts/${id}`),

    updateCategory: (postId: number, category: string) =>
      http<{ id: number; category: string }>(
        `/api/agents/instagram-ingest/posts/${postId}/category`,
        { method: "PATCH", body: JSON.stringify({ category }) },
      ),

    assetFileUrl: (postId: number, assetId: number) =>
      `${BASE}/api/agents/instagram-ingest/posts/${postId}/assets/${assetId}/file`,

    frameFileUrl: (postId: number, frameId: number) =>
      `${BASE}/api/agents/instagram-ingest/posts/${postId}/frames/${frameId}/file`,
  },

  // ----- Producer Assets Library (v3) -----

  producerAssets: {
    upload: async (params: {
      file: File;
      asset_kind: ProducerAssetKind;
      category?: ProducerAssetCategory | null;
      archetype?:
        | "emirati_male" | "emirati_female"
        | "casual_male" | "casual_female"
        | null;
      display_name?: string;
      hint?: string;
    }) => {
      const form = new FormData();
      form.append("file", params.file, params.file.name);
      form.append("asset_kind", params.asset_kind);
      if (params.category) form.append("category", params.category);
      if (params.archetype) form.append("archetype", params.archetype);
      if (params.display_name) form.append("display_name", params.display_name);
      if (params.hint) form.append("hint", params.hint);
      const res = await fetch(`${BASE}/api/agents/producer/assets`, {
        method: "POST",
        body: form,
      });
      if (!res.ok) {
        let detail = res.statusText;
        try { detail = (await res.json())?.detail || detail; } catch { /* */ }
        throw new Error(`${res.status} ${detail}`);
      }
      return (await res.json()) as ProducerAssetDetail;
    },

    uploadFromUrl: (body: {
      url: string;
      asset_kind?: "image" | "video" | "font" | "other";
      category?: "character" | "logo" | "mood" | "free" | null;
      archetype?:
        | "emirati_male" | "emirati_female"
        | "casual_male" | "casual_female"
        | null;
      display_name?: string;
      hint?: string;
    }) =>
      http<ProducerAssetDetail>("/api/agents/producer/assets/from-url", {
        method: "POST",
        body: JSON.stringify(body),
      }),

    previewFolder: (body: { url: string }) =>
      http<FolderPreviewResult>("/api/agents/producer/assets/folder-preview", {
        method: "POST",
        body: JSON.stringify(body),
      }),

    uploadFromFolder: (body: {
      url: string;
      default_category?: "character" | "logo" | "mood" | "free" | null;
      default_archetype?:
        | "emirati_male" | "emirati_female"
        | "casual_male" | "casual_female"
        | null;
      display_name_prefix?: string;
      hint?: string;
    }) =>
      http<FolderImportResult>("/api/agents/producer/assets/from-folder", {
        method: "POST",
        body: JSON.stringify(body),
      }),

    createFromPost: (postId: number, assetId?: number) =>
      http<ProducerAssetDetail>("/api/agents/producer/assets/from-post", {
        method: "POST",
        body: JSON.stringify({ post_id: postId, asset_id: assetId ?? null }),
      }),

    // Video IG categories: smart frame extraction → user picks → import.
    // Modal calls extractFrames once; then fromFrame N times for the user's picks.
    extractFrames: (
      postId: number,
      assetId: number,
      mode: FrameExtractionMode,
    ) =>
      http<ExtractFramesResult>(
        "/api/agents/producer/assets/from-post/extract-frames",
        {
          method: "POST",
          body: JSON.stringify({ post_id: postId, asset_id: assetId, mode }),
        },
      ),

    fromFrame: (body: FromFrameRequest) =>
      http<ProducerAssetDetail>(
        "/api/agents/producer/assets/from-post/from-frame",
        { method: "POST", body: JSON.stringify(body) },
      ),

    list: (
      params: { kind?: string; category?: string; archetype?: string; content_category?: string; limit?: number; offset?: number } = {},
    ) => {
      const q = new URLSearchParams();
      if (params.kind) q.set("kind", params.kind);
      if (params.category) q.set("category", params.category);
      if (params.archetype) q.set("archetype", params.archetype);
      if (params.content_category) q.set("content_category", params.content_category);
      if (params.limit != null) q.set("limit", String(params.limit));
      if (params.offset != null) q.set("offset", String(params.offset));
      const qs = q.toString();
      return http<ProducerAssetsList>(
        `/api/agents/producer/assets${qs ? "?" + qs : ""}`,
      );
    },

    get: (id: number) =>
      http<ProducerAssetDetail>(`/api/agents/producer/assets/${id}`),

    // Semantic search over the library (embeds the query → cosine NN). Used as
    // groundwork for the automatic AI editor.
    search: (body: { query: string; kind?: string; category?: string; limit?: number }) =>
      http<{ items: ProducerAssetRow[]; query: string }>(
        "/api/agents/producer/assets/search",
        { method: "POST", body: JSON.stringify(body) },
      ),

    /**
     * FULL-RESOLUTION original. Averages ~3.1 MB, up to 9.3 MB for backgrounds.
     * Use for downloads and for "View original" — NEVER for a grid tile or a
     * preview chip. For display, use `thumbUrlFor(row)`.
     */
    fileUrl: (id: number) =>
      `${BASE}/api/agents/producer/assets/${id}/file`,

    /**
     * Best DISPLAY url for an asset row: the server-minted CDN thumbnail when the
     * backend could mint one (raster images in Supabase), else the full-resolution
     * proxy so nothing ever renders blank.
     *
     * Takes the ROW, not an id, because the thumbnail is a signed URL the server
     * mints — it cannot be reconstructed client-side from an id alone.
     */
    thumbUrlFor: (a: { id: number; thumb_url?: string | null }) =>
      a.thumb_url || `${BASE}/api/agents/producer/assets/${a.id}/file`,

    previewUrl: (id: number) =>
      `${BASE}/api/agents/producer/assets/${id}/preview`,

    patch: (id: number, body: { display_name?: string; ai_description?: string; enabled?: boolean }) =>
      http<{ id: number; display_name: string | null; ai_description: string; enabled: boolean; updated_at: string }>(
        `/api/agents/producer/assets/${id}`,
        { method: "PATCH", body: JSON.stringify(body) },
      ),

    delete: (id: number) =>
      http<{ deleted: number }>(`/api/agents/producer/assets/${id}`, { method: "DELETE" }),
  },

  // ----- Yas Producer · Poster + Human (generative) -----

  yasProducer: {
    posterHuman: {
      // multipart upload of a reference image; returns the upload row + URL
      uploadRef: async (file: File): Promise<YasUploadRef> => {
        const form = new FormData();
        form.append("file", file, file.name);
        const res = await fetch(
          `${BASE}/api/yas-producer/poster-human/upload-ref`,
          { method: "POST", body: form },
        );
        if (!res.ok) {
          let detail = res.statusText;
          try { detail = (await res.json())?.detail || detail; } catch { /* */ }
          throw new Error(`${res.status} ${detail}`);
        }
        return (await res.json()) as YasUploadRef;
      },

      // multipart upload of a VIDEO clip (MP4/MOV/WebM) — used as a direct
      // intro/outro in Talking Head (stitched in as-is, no card-gen / Veo).
      uploadClip: async (file: File): Promise<YasUploadRef> => {
        const form = new FormData();
        form.append("file", file, file.name);
        const res = await fetch(
          `${BASE}/api/yas-producer/poster-human/upload-clip`,
          { method: "POST", body: form },
        );
        if (!res.ok) {
          let detail = res.statusText;
          try { detail = (await res.json())?.detail || detail; } catch { /* */ }
          throw new Error(`${res.status} ${detail}`);
        }
        return (await res.json()) as YasUploadRef;
      },

      // 2-stage batch: gpt-4o casts 4 DIFFERENT compositions of the SAME copy,
      // Nano Banana renders each → a 2x2 grid. Returns {items: [...4]}.
      generate: (body: YasGenerateRequest) =>
        http<YasGenerationBatchResponse>("/api/yas-producer/poster-human/generate", {
          method: "POST",
          body: JSON.stringify(body),
        }),

      // template-fill mode: composes a poster by dropping a student photo into a
      // saved asset's design (Nano Banana multi-image fusion). No concept stage —
      // renders the same template+face 4 times → a 2x2 grid. Returns {items:[...4]}.
      templateFill: (body: YasTemplateFillRequest) =>
        http<YasGenerationBatchResponse>("/api/yas-producer/poster-human/template-fill", {
          method: "POST",
          body: JSON.stringify(body),
        }),

      // Explore: 4 close riffs anchored on a chosen poster (both modes).
      variants: (parentId: number) =>
        http<YasGenerationBatchResponse>("/api/yas-producer/poster-human/variants", {
          method: "POST",
          body: JSON.stringify({ parent_id: parentId }),
        }),

      // copy a ready generation into producer_assets as img_poster_human
      saveToAssets: (genId: number, body: { display_name?: string } = {}) =>
        http<{ id: number; display_name: string; signed_url: string | null; ai_description: string | null; metadata: Record<string, unknown> }>(
          `/api/yas-producer/poster-human/${genId}/save-to-assets`,
          { method: "POST", body: JSON.stringify(body) },
        ),

      // file URLs (frontend-renderable, same-origin via Vite proxy). Optional
      // cache-buster `v` (e.g. created_at) for re-rendered children.
      fileUrl: (genId: number, v?: string | number) =>
        `${BASE}/api/yas-producer/poster-human/${genId}/file${v != null ? `?v=${encodeURIComponent(String(v))}` : ""}`,
      uploadFileUrl: (token: string) =>
        `${BASE}/api/yas-producer/poster-human/upload/${token}/file`,

      // last 24h unsaved generations — rehydrate the in-session history
      recent: () =>
        http<{ items: YasGeneration[] }>(
          "/api/yas-producer/poster-human/recent",
        ),

      // Full metadata for ONE generation (Library "Continue editing" → resume).
      fetch: (genId: number) =>
        http<YasGeneration>(`/api/yas-producer/poster-human/${genId}`),

      // "Remove logo" — re-render this image with the 42 logo removed, kept as a
      // NEW child generation (the original is preserved). Returns the new row.
      removeLogo: (genId: number) =>
        http<YasGeneration>(
          `/api/yas-producer/poster-human/${genId}/remove-logo`,
          { method: "POST" },
        ),
      // Hard-delete a Poster+Human generation (row + image + its animations) —
      // Library "Delete". Children (retries/de-logo) are independent and untouched.
      delete: (genId: number) =>
        http<{ deleted: number; id: number; scheduled_removed: number }>(
          `/api/yas-producer/poster-human/${genId}`,
          { method: "DELETE" },
        ),
    },

    // ----- Yas Producer · Poster-only (static, real-logo composited) -----
    //
    // Mirrors posterHuman but:
    //   - single Gemini image call per request
    //   - no local cap any more; `remaining_this_hour` is a leftover the server still sends in the response
    //   - two-stage pipeline: gpt-4o expands the topic → gemini-3-pro-image
    //     renders it with the real 42 logo + optional refs as image parts
    //   - no animate, no template-fill, no engine toggle, no retry chain
    posterOnly: {
      // Returns a batch of 4 variants (Midjourney style).
      generate: (body: YasPosterOnlyGenerateRequest) =>
        http<YasPosterBatchResponse>("/api/yas-producer/poster/generate", {
          method: "POST",
          body: JSON.stringify(body),
        }),
      // "Explore" — 4 close variations of an existing poster (engine-selectable).
      variants: (parentId: number, image_model?: YasImageModel, progressive?: boolean) =>
        http<YasPosterBatchResponse>("/api/yas-producer/poster/variants", {
          method: "POST",
          body: JSON.stringify({ parent_id: parentId, image_model, progressive }),
        }),
      // "Remove logo" — re-render this poster with the 42 logo removed, kept as a
      // NEW child record (the original is preserved). Returns the new poster row.
      removeLogo: (genId: number) =>
        http<YasPosterOnlyGeneration>(
          `/api/yas-producer/poster/${genId}/remove-logo`,
          { method: "POST" },
        ),
      // "Refine" — re-render this poster applying ONLY the user-described change,
      // kept as a NEW child record (the original is preserved). Returns the new row.
      refine: (genId: number, instructions: string) =>
        http<YasPosterOnlyGeneration>(
          `/api/yas-producer/poster/${genId}/refine`,
          { method: "POST", body: JSON.stringify({ instructions }) },
        ),
      saveToAssets: (genId: number, body: { display_name?: string } = {}) =>
        http<{
          id: number;
          display_name: string;
          signed_url: string | null;
          ai_description: string | null;
          metadata: Record<string, unknown>;
        }>(`/api/yas-producer/poster/${genId}/save-to-assets`, {
          method: "POST",
          body: JSON.stringify(body),
        }),
      // `v` is a content cache-buster (pass the record's created_at). The DB id
      // sequence restarts at 1 if the poster store is ever wiped/recreated, so a
      // NEW poster can reuse an OLD id — and the browser would serve the stale
      // cached image for that id-keyed URL. Tying the URL to created_at makes a
      // reused id resolve to a fresh URL. Backend ignores the param.
      fileUrl: (genId: number, v?: string | number | null) =>
        `${BASE}/api/yas-producer/poster/${genId}/file${v ? `?v=${encodeURIComponent(String(v))}` : ""}`,
      recent: () =>
        http<{ items: YasPosterOnlyGeneration[] }>(
          "/api/yas-producer/poster/recent",
        ),
      // Full metadata for ONE poster (Library "Continue editing" → resume).
      fetch: (genId: number) =>
        http<YasPosterOnlyGeneration>(`/api/yas-producer/poster/${genId}`),
      // Hard-delete a poster generation (DB row + stored image) — Library "Delete".
      // After this the poster is gone from the Library grid and the history strip.
      delete: (genId: number) =>
        http<{ deleted: number; id: number }>(
          `/api/yas-producer/poster/${genId}`,
          { method: "DELETE" },
        ),
    },

    // ----- Yas Producer · Human Character (Ideation → Human Character) -----
    //   - photorealistic person generator; reuses the poster two-stage pipeline
    //     (gpt-4o casts N distinct candidates → Nano Banana renders each), but
    //     with NO 42 logo and NO text
    //   - a chosen candidate is saved as a `character` asset (Talking Head ref)
    //   - shares the poster image-gen path (which no longer caps per hour) (`remaining_this_hour`)
    humanCharacter: {
      // Returns a batch of N distinct candidate people.
      generate: (body: YasHumanCharacterGenerateRequest) =>
        http<YasHumanCharacterBatchResponse>("/api/yas-producer/human-character/generate", {
          method: "POST",
          body: JSON.stringify(body),
        }),
      // Save a chosen candidate into Assets as a `character` (img_human_character).
      saveToAssets: (genId: number, body: { display_name?: string } = {}) =>
        http<{
          id: number;
          display_name: string;
          signed_url: string | null;
          ai_description: string | null;
          metadata: Record<string, unknown>;
        }>(`/api/yas-producer/human-character/${genId}/save-to-assets`, {
          method: "POST",
          body: JSON.stringify(body),
        }),
      // `v` is a content cache-buster (pass the record's created_at) — see the
      // posterOnly.fileUrl note: reused SERIAL ids can otherwise serve stale art.
      fileUrl: (genId: number, v?: string | number | null) =>
        `${BASE}/api/yas-producer/human-character/${genId}/file${v ? `?v=${encodeURIComponent(String(v))}` : ""}`,
      recent: () =>
        http<{ items: YasHumanCharacterGeneration[] }>(
          "/api/yas-producer/human-character/recent",
        ),
      fetch: (genId: number) =>
        http<YasHumanCharacterGeneration>(`/api/yas-producer/human-character/${genId}`),
    },

    // Create Animation — freeform intro/outro: brief → static frame (logo
    // baked in) → Veo image-to-video. Local-only (yas-output/). Two steps:
    //   generateFrame → record (frame_ready) ; animate → record (animating)
    // then poll get() until status === "ready".
    animation: {
      // Midjourney-style batch: one click renders 4 frame variants (mirrors
      // posterOnly.generate). Returns {items:[...]}; each frame is animatable.
      generateFrame: (body: YasAnimationFrameRequest) =>
        http<YasAnimationBatchResponse>("/api/yas-producer/animation/generate-frame", {
          method: "POST",
          body: JSON.stringify(body),
        }),
      // "Explore" — 4 CLOSE variations of an existing frame (anchored on it).
      variants: (parentId: number) =>
        http<YasAnimationBatchResponse>("/api/yas-producer/animation/variants", {
          method: "POST",
          body: JSON.stringify({ parent_id: parentId }),
        }),
      // "Remove logo" — re-render this frame with the 42 logo removed BEFORE
      // animating, kept as a NEW frame_ready record (the original is preserved).
      removeLogo: (animId: number) =>
        http<YasAnimationGen>(`/api/yas-producer/animation/${animId}/remove-logo`, {
          method: "POST",
        }),
      // "Refine" — re-render this frame applying ONLY the user-described change
      // BEFORE animating, kept as a NEW frame_ready record (the original is preserved).
      refine: (animId: number, instructions: string) =>
        http<YasAnimationGen>(`/api/yas-producer/animation/${animId}/refine`, {
          method: "POST",
          body: JSON.stringify({ instructions }),
        }),
      // Save the STATIC FRAME (a branded poster) to the Assets library.
      saveToAssets: (animId: number, body: { display_name?: string } = {}) =>
        http<{
          id: number;
          display_name: string;
          signed_url: string | null;
          ai_description: string | null;
          metadata: Record<string, unknown>;
        }>(`/api/yas-producer/animation/${animId}/save-to-assets`, {
          method: "POST",
          body: JSON.stringify(body),
        }),
      // Hard-delete an animation (row + frame + MP4) — Library "Delete".
      delete: (animId: number) =>
        http<{ deleted: number; id: number; scheduled_removed: number }>(
          `/api/yas-producer/animation/${animId}`,
          { method: "DELETE" },
        ),
      // Import an image the user ALREADY has (a generated poster, a saved asset,
      // or a file off their desktop) as a frame_ready record, then `animate` it
      // like any other frame. Costs nothing; it copies bytes, calls no model.
      fromImage: (body: YasAnimationFromImageRequest) =>
        http<YasAnimationGen>("/api/yas-producer/animation/from-image", {
          method: "POST",
          body: JSON.stringify(body),
        }),
      animate: (animId: number, body: YasAnimationAnimateRequest) =>
        http<YasAnimationGen>(`/api/yas-producer/animation/${animId}/animate`, {
          method: "POST",
          body: JSON.stringify(body),
        }),
      get: (animId: number) =>
        http<YasAnimationGen>(`/api/yas-producer/animation/${animId}`),
      recent: () =>
        http<{ items: YasAnimationGen[] }>("/api/yas-producer/animation/recent"),
      // `v` cache-buster (pass created_at) — same reused-id reasoning as posters:
      // the animation store's id sequence also restarts on a wipe.
      frameUrl: (animId: number, v?: string | number | null) =>
        `${BASE}/api/yas-producer/animation/${animId}/frame${v ? `?v=${encodeURIComponent(String(v))}` : ""}`,
      fileUrl: (animId: number, v?: string | number | null) =>
        `${BASE}/api/yas-producer/animation/${animId}/file${v ? `?v=${encodeURIComponent(String(v))}` : ""}`,
      // Seed a NEW Video Editor project with this animation MP4 as the first clip;
      // returns the new project id to deep-link into (#producer/video_editor:<id>).
      openInEditor: (animId: number) =>
        http<{ project_id: number }>(
          `/api/yas-producer/animation/${animId}/open-in-editor`,
          { method: "POST" },
        ),
    },

    // General AI Video — unbranded Veo generator (Ideation → General video).
    // Reference pics + prompt → still (generateStill) → image-to-video (animate);
    // poll get() until status === "ready". Generate one or more per session; the
    // recent() list is the in-creator history strip. Finalise: stitch a chosen
    // subset into one MP4 (a seeded Video Editor project), or open in the editor.
    // Reference uploads reuse posterHuman.uploadRef (same /upload-ref endpoint).
    generalVideo: {
      /** What Veo and Seedance each accept, and whether Seedance is connected. */
      engines: () => http<YasGeneralVideoEngines>("/api/yas-producer/general-video/engines"),
      generateStill: (body: {
        prompt: string;
        aspect_ratio: SeedanceAspect;
        reference_upload_ids: number[];
        reference_asset_ids: number[];
      }) =>
        http<YasGeneralVideoGen>("/api/yas-producer/general-video/generate-still", {
          method: "POST",
          body: JSON.stringify(body),
        }),
      animate: (
        genId: number,
        body: {
          /** The status the page showed; the server claims the still only from it. */
          expected_status?: string | null;
          engine?: YasGeneralVideoEngine;
          /** Veo: 4 | 6 | 8. Seedance: any whole number from 4 to 30. */
          duration_sec?: number | null;
          with_audio?: boolean | null;
          /** Veo only. */
          quality?: "normal" | "high" | null;
          /** Seedance only. */
          resolution?: SeedanceResolution | null;
        },
      ) =>
        http<YasGeneralVideoGen>(`/api/yas-producer/general-video/${genId}/animate`, {
          method: "POST",
          body: JSON.stringify(body),
        }),
      get: (genId: number) =>
        http<YasGeneralVideoGen>(`/api/yas-producer/general-video/${genId}`),
      recent: () =>
        http<{ items: YasGeneralVideoGen[] }>("/api/yas-producer/general-video/recent"),
      frameUrl: (genId: number, v?: string | number | null) =>
        `${BASE}/api/yas-producer/general-video/${genId}/frame${v ? `?v=${encodeURIComponent(String(v))}` : ""}`,
      fileUrl: (genId: number, v?: string | number | null) =>
        `${BASE}/api/yas-producer/general-video/${genId}/file${v ? `?v=${encodeURIComponent(String(v))}` : ""}`,
      downloadUrl: (genId: number) =>
        `${BASE}/api/yas-producer/general-video/${genId}/file?download=1`,
      downloadFrameUrl: (genId: number) =>
        `${BASE}/api/yas-producer/general-video/${genId}/frame?download=1`,
      // Save a STILL frame into the Assets library (content_category img_general_still).
      saveStillToAssets: (genId: number, body: { display_name?: string } = {}) =>
        http<{
          id: number;
          display_name: string;
          signed_url: string | null;
          ai_description: string | null;
          metadata: Record<string, unknown>;
        }>(`/api/yas-producer/general-video/${genId}/save-to-assets`, {
          method: "POST",
          body: JSON.stringify(body),
        }),
      // Clear the session — archive the last 24h of clips so the strip starts clean.
      clearSession: () =>
        http<{ cleared: number }>("/api/yas-producer/general-video/clear-session", {
          method: "POST",
        }),
      // Finalise → seed a VE project from the chosen clips + export it to one MP4.
      stitch: (clip_ids: number[], aspect?: string | null, title?: string | null) =>
        http<{ project_id: number }>("/api/yas-producer/general-video/stitch", {
          method: "POST",
          body: JSON.stringify({ clip_ids, aspect, title }),
        }),
      // Open the chosen clips in the Video Editor (seed project, no export).
      openInEditor: (clip_ids: number[], aspect?: string | null, title?: string | null) =>
        http<{ project_id: number }>("/api/yas-producer/general-video/open-in-editor", {
          method: "POST",
          body: JSON.stringify({ clip_ids, aspect, title }),
        }),
    },

    // ✨ Enhance, polish rough topic or feedback into poster-appropriate copy.
    // Gemini Flash, ~1-3s, basically free.
    //   • topic: pulls brand examples from img_poster_human assets to anchor voice.
    //   • feedback: when parent_generation_id is supplied, looks at THAT image
    //     so the rewritten instruction references actual visual elements.
    enhance: (
      text: string,
      kind: "topic" | "poster_human" | "feedback" | "dialogue" | "video_prompt" | "character",
      parent_generation_id?: number | null,
    ) =>
      http<{ enhanced: string }>("/api/yas-producer/enhance", {
        method: "POST",
        body: JSON.stringify({ text, kind, parent_generation_id: parent_generation_id ?? null }),
      }),

    // Talking Head — the 3rd Yas Producer category.
    // Flow:
    //   1. generateScript({topic, refs, lock_character, image_model})
    //      → OpenAI GPT-4o with full RAG context → returns the run.
    //   2. generatePreviews(runId)
    //      → parallel image gen for main keyframe + intro + outro cards.
    //   3. retryPreview(runId, {which, feedback_text}) — optional refinement.
    //   4. finalize(runId)
    //      → kicks Veo i2v; returns immediately. Poll fetch(runId) every 5s.
    //   5. saveToAssets(runId, {display_name}) once composition_status=ready.
    talkingHead: {
      generateScript: (body: TalkingHeadGenerateScriptRequest) =>
        http<TalkingHeadRun>(
          "/api/yas-producer/talking-head/generate-script",
          { method: "POST", body: JSON.stringify(body) },
        ),
      generatePreviews: (runId: number) =>
        http<TalkingHeadRun>(
          `/api/yas-producer/talking-head/${runId}/generate-previews`,
          { method: "POST", body: "{}" },
        ),
      retryPreview: (runId: number, body: TalkingHeadRetryRequest) =>
        http<TalkingHeadRun>(
          `/api/yas-producer/talking-head/${runId}/retry-preview`,
          { method: "POST", body: JSON.stringify(body) },
        ),
      retryVideo: (runId: number, body: TalkingHeadRetryRequest) =>
        http<TalkingHeadRun>(
          `/api/yas-producer/talking-head/${runId}/retry-video`,
          { method: "POST", body: JSON.stringify(body) },
        ),
      finalize: (runId: number) =>
        http<TalkingHeadRun>(
          `/api/yas-producer/talking-head/${runId}/finalize`,
          { method: "POST", body: "{}" },
        ),
      stitch: (runId: number) =>
        http<TalkingHeadRun>(
          `/api/yas-producer/talking-head/${runId}/stitch`,
          { method: "POST", body: "{}" },
        ),
      fetch: (runId: number) =>
        http<TalkingHeadRun>(`/api/yas-producer/talking-head/${runId}`),
      saveToAssets: (runId: number, body: { display_name?: string } = {}) =>
        http<{ id: number; yas_generation_id: number }>(
          `/api/yas-producer/talking-head/${runId}/save-to-assets`,
          { method: "POST", body: JSON.stringify(body) },
        ),
      recent: () =>
        http<{ items: TalkingHeadRun[] }>(
          "/api/yas-producer/talking-head/recent",
        ),
      mainFileUrl: (runId: number) =>
        `${BASE}/api/yas-producer/talking-head/${runId}/file`,
      cardFileUrl: (cardId: number) =>
        `${BASE}/api/yas-producer/talking-head/cards/${cardId}/file`,
      videoFileUrl: (runId: number) =>
        `${BASE}/api/yas-producer/talking-head/${runId}/video`,
      compositionFileUrl: (runId: number) =>
        `${BASE}/api/yas-producer/talking-head/${runId}/composition/file`,
    },

    // Talking Head v2 — isolated twin of `talkingHead`. Same shapes, but every
    // call hits the /talking-head-v2/* endpoints backed by yas_talking_head_v2.
    // v1 above is the untouched backup; this one is free to diverge.
    talkingHeadV2: {
      generateScript: (body: TalkingHeadGenerateScriptRequest) =>
        http<TalkingHeadRun>(
          "/api/yas-producer/talking-head-v2/generate-script",
          { method: "POST", body: JSON.stringify(body) },
        ),
      generatePreviews: (runId: number, look?: TalkingHeadV2GeneratePreviewsRequest) =>
        http<TalkingHeadRun>(
          `/api/yas-producer/talking-head-v2/${runId}/generate-previews`,
          { method: "POST", body: JSON.stringify(look ?? {}) },
        ),
      retryPreview: (runId: number, body: TalkingHeadRetryRequest) =>
        http<TalkingHeadRun>(
          `/api/yas-producer/talking-head-v2/${runId}/retry-preview`,
          { method: "POST", body: JSON.stringify(body) },
        ),
      // Stills ("main" or "scene_<N>"): Refine makes ONE change on the
      // current still, Regenerate makes it again the way first generation
      // did (no text), Undo puts back the version before the last change.
      refineStill: (runId: number, which: "main" | `scene_${number}`, change: string) =>
        http<TalkingHeadRun>(
          `/api/yas-producer/talking-head-v2/${runId}/still/${which}/refine`,
          { method: "POST", body: JSON.stringify({ change }) },
        ),
      regenerateStill: (runId: number, which: "main" | `scene_${number}`) =>
        http<TalkingHeadRun>(
          `/api/yas-producer/talking-head-v2/${runId}/still/${which}/regenerate`,
          { method: "POST", body: "{}" },
        ),
      undoStill: (runId: number, which: "main" | `scene_${number}`) =>
        http<TalkingHeadRun>(
          `/api/yas-producer/talking-head-v2/${runId}/still/${which}/undo`,
          { method: "POST", body: "{}" },
        ),
      retryVideo: (runId: number, body: TalkingHeadRetryRequest) =>
        http<TalkingHeadRun>(
          `/api/yas-producer/talking-head-v2/${runId}/retry-video`,
          { method: "POST", body: JSON.stringify(body) },
        ),
      // Makes the clips (paid Veo). Only the Videos button calls it, with
      // confirm: the backend refuses a call without it (409).
      finalize: (runId: number, body: { confirm: true }) =>
        http<TalkingHeadRun>(
          `/api/yas-producer/talking-head-v2/${runId}/finalize`,
          { method: "POST", body: JSON.stringify(body) },
        ),
      // Finalize. `expect_sha`: the reel's inputs_sha as this page last read
      // it (a reel changed since is a 409). An older backend gets "{}".
      stitch: (runId: number, body?: { expect_sha?: string | null }) =>
        http<TalkingHeadRun>(
          `/api/yas-producer/talking-head-v2/${runId}/stitch`,
          { method: "POST", body: body?.expect_sha ? JSON.stringify({ expect_sha: body.expect_sha }) : "{}" },
        ),
      fetch: (runId: number) =>
        http<TalkingHeadRun>(`/api/yas-producer/talking-head-v2/${runId}`),
      // The reel scene by scene (the Rebrand stage): what ships, where each
      // scene ends, a version on its way. Only a backend that sends
      // run.reel_ready has these two.
      reel: (runId: number) =>
        http<ReelBlock>(`/api/yas-producer/talking-head-v2/${runId}/reel`),
      setReel: (runId: number, target: string, patch: ReelChoicePatch) =>
        http<ReelBlock>(
          `/api/yas-producer/talking-head-v2/${runId}/reel/${encodeURIComponent(target)}`,
          { method: "PUT", body: JSON.stringify(patch) },
        ),
      // Background music under the finished reel (Final). Paid (Suno): only
      // the music panel's "Make music" press calls it. The run back, with
      // run.music generating; the server collects the track and lays it under.
      music: (runId: number, brief: string) =>
        http<TalkingHeadRun>(
          `/api/yas-producer/talking-head-v2/${runId}/music`,
          { method: "POST", body: JSON.stringify({ brief }) },
        ),
      // The music on (laid under the reel at `level`; free, seconds) or off
      // (`remove`: the panel starts over). The reel itself never changes.
      musicApply: (runId: number, body: { on: boolean; level?: ReelMusicLevel; remove?: boolean }) =>
        http<TalkingHeadRun>(
          `/api/yas-producer/talking-head-v2/${runId}/music/apply`,
          { method: "POST", body: JSON.stringify(body) },
        ),
      // Hard-delete a Talking Head run (run + all child cards/scenes + media +
      // composition) — Library "Delete". Saved assets + published IG preserved.
      delete: (runId: number) =>
        http<{ deleted: number; id: number; scheduled_removed: number }>(
          `/api/yas-producer/talking-head-v2/${runId}`,
          { method: "DELETE" },
        ),
      saveToAssets: (runId: number, body: { display_name?: string } = {}) =>
        http<{ id: number; yas_generation_id: number }>(
          `/api/yas-producer/talking-head-v2/${runId}/save-to-assets`,
          { method: "POST", body: JSON.stringify(body) },
        ),
      openInEditor: (runId: number, mode: "clips" | "final") =>
        http<{ project_id: number }>(
          `/api/yas-producer/talking-head-v2/${runId}/open-in-editor`,
          { method: "POST", body: JSON.stringify({ mode }) },
        ),
      recent: () =>
        http<{ items: TalkingHeadRun[] }>(
          "/api/yas-producer/talking-head-v2/recent",
        ),
      mainFileUrl: (runId: number) =>
        `${BASE}/api/yas-producer/talking-head-v2/${runId}/file`,
      cardFileUrl: (cardId: number) =>
        `${BASE}/api/yas-producer/talking-head-v2/cards/${cardId}/file`,
      // The intro/outro picker's "Previous videos": earlier reels' current
      // cards, newest first (each with its reusable clip, if any).
      previousCards: (role: "intro" | "outro", opts: { excludeRunId?: number | null; limit?: number } = {}) => {
        const q = new URLSearchParams({ role });
        if (opts.excludeRunId != null) q.set("exclude_run_id", String(opts.excludeRunId));
        if (opts.limit != null) q.set("limit", String(opts.limit));
        return http<{ items: TalkingHeadPreviousCard[] }>(
          `/api/yas-producer/talking-head-v2/cards/previous?${q.toString()}`,
        );
      },
      videoFileUrl: (runId: number) =>
        `${BASE}/api/yas-producer/talking-head-v2/${runId}/video`,
      // `v` = the run's composition_good_seq: the reel this URL names (a reel
      // finalized again is a new file behind the same path, and browsers hold
      // the plain URL as immutable). Absent on an older backend: plain URL.
      compositionFileUrl: (runId: number, v?: number | null, download?: boolean) => {
        const q = [v != null ? `v=${v}` : "", download ? "download=1" : ""].filter(Boolean).join("&");
        return `${BASE}/api/yas-producer/talking-head-v2/${runId}/composition/file${q ? `?${q}` : ""}`;
      },
      // The reel's music version (run.music.music_url names the same file):
      // `v` = the reel, `m` = run.music.mix_id, so a new mix is a new URL.
      musicFileUrl: (runId: number, v?: number | null, mixId?: string | null, download?: boolean) => {
        const q = ["music=1", v != null ? `v=${v}` : "", mixId ? `m=${encodeURIComponent(mixId)}` : "", download ? "download=1" : ""]
          .filter(Boolean).join("&");
        return `${BASE}/api/yas-producer/talking-head-v2/${runId}/composition/file?${q}`;
      },
      // v2 editor — edit-in-place, per-clip exclusion, batch regen, restore.
      editRun: (runId: number, body: TalkingHeadV2EditRequest) =>
        http<TalkingHeadRun>(
          `/api/yas-producer/talking-head-v2/${runId}/edit`,
          { method: "POST", body: JSON.stringify(body) },
        ),
      setExcluded: (runId: number, body: TalkingHeadV2ExcludeRequest) =>
        http<TalkingHeadRun>(
          `/api/yas-producer/talking-head-v2/${runId}/exclude`,
          { method: "POST", body: JSON.stringify(body) },
        ),
      setSegmentQuality: (runId: number, body: { which: string; quality: "normal" | "quality" }) =>
        http<TalkingHeadRun>(
          `/api/yas-producer/talking-head-v2/${runId}/segment-quality`,
          { method: "POST", body: JSON.stringify(body) },
        ),
      batchRegen: (runId: number, body: TalkingHeadV2BatchRequest) =>
        http<TalkingHeadRun>(
          `/api/yas-producer/talking-head-v2/${runId}/batch-regen`,
          { method: "POST", body: JSON.stringify(body) },
        ),
      restoreScene: (runId: number, archivedIndex: number) =>
        http<TalkingHeadRun>(
          `/api/yas-producer/talking-head-v2/${runId}/restore-scene`,
          { method: "POST", body: JSON.stringify({ archived_index: archivedIndex }) },
        ),
    },

    // Video Editor — multi-track NLE category. Pure assembly/render (no Veo).
    // The whole editable timeline is autosaved to the backend; the export
    // poller drives the Remotion render. `save` is debounced by the editor store.
    videoEditor: {
      create: (body: { title?: string; aspect?: string; preset?: "b_roll" | null } = {}) =>
        http<VideoEditorProject>("/api/yas-producer/video-editor/projects", {
          method: "POST",
          body: JSON.stringify(body),
        }),
      fetch: (projectId: number) =>
        http<VideoEditorProject>(`/api/yas-producer/video-editor/${projectId}`),
      // Saved versions, oldest first. The store puts them under Undo on load.
      history: (projectId: number) =>
        http<{ items: VideoEditorRevision[] }>(`/api/yas-producer/video-editor/${projectId}/history`),
      // Hard-delete a Video Editor project (row + local render output) — Library
      // "Delete". The shared bin source clips are never touched.
      delete: (projectId: number) =>
        http<{ deleted: number; id: number; scheduled_removed: number }>(
          `/api/yas-producer/video-editor/${projectId}`,
          { method: "DELETE" },
        ),
      save: (projectId: number, body: VideoEditorSaveRequest) =>
        http<VideoEditorProject>(
          `/api/yas-producer/video-editor/${projectId}/timeline`,
          { method: "PATCH", body: JSON.stringify(body) },
        ),
      export: (projectId: number) =>
        http<VideoEditorProject>(
          `/api/yas-producer/video-editor/${projectId}/export`,
          { method: "POST", body: "{}" },
        ),
      // Re-order the B-roll clips on their track (dedicated B-roll tool — no
      // timeline UI). `clipIds` is the full new order of the main track's clips.
      brollReorder: (projectId: number, clipIds: string[]) =>
        http<VideoEditorProject>(
          `/api/yas-producer/video-editor/${projectId}/broll/reorder`,
          { method: "POST", body: JSON.stringify({ clip_ids: clipIds }) },
        ),
      listSources: (params: { q?: string; kind?: string; limit?: number; offset?: number } = {}) => {
        const qp = new URLSearchParams();
        if (params.q) qp.set("q", params.q);
        if (params.kind) qp.set("kind", params.kind);
        if (params.limit != null) qp.set("limit", String(params.limit));
        if (params.offset != null) qp.set("offset", String(params.offset));
        const qs = qp.toString();
        return http<VideoSourceList>(
          `/api/yas-producer/video-editor/sources${qs ? "?" + qs : ""}`,
        );
      },
      // Upload a video source. XHR (not fetch) so we can report upload progress.
      upload: (file: File, onProgress?: (pct: number) => void): Promise<VideoSource> =>
        new Promise((resolve, reject) => {
          const form = new FormData();
          form.append("file", file, file.name);
          const xhr = new XMLHttpRequest();
          xhr.open("POST", `${BASE}/api/yas-producer/video-editor/upload`);
          xhr.upload.onprogress = (e) => {
            if (onProgress && e.lengthComputable) onProgress(Math.round((e.loaded / e.total) * 100));
          };
          xhr.onload = () => {
            if (xhr.status >= 200 && xhr.status < 300) {
              try {
                resolve(JSON.parse(xhr.responseText) as VideoSource);
              } catch {
                reject(new Error("bad upload response"));
              }
            } else {
              let detail = xhr.statusText;
              try { detail = JSON.parse(xhr.responseText)?.detail || detail; } catch { /* */ }
              reject(new Error(`${xhr.status} ${detail}`));
            }
          };
          xhr.onerror = () => reject(new Error("upload network error"));
          xhr.send(form);
        }),
      listFonts: () =>
        http<{ items: VideoEditorFont[] }>("/api/yas-producer/video-editor/assets/fonts"),
      listAudio: () =>
        http<{ items: VideoEditorAudio[] }>("/api/yas-producer/video-editor/assets/audio"),
      audioFileUrl: (id: number) =>
        `${BASE}/api/yas-producer/video-editor/assets/audio/${id}/file`,
      // Upload a music/audio file (XHR for progress).
      uploadAudio: (file: File, onProgress?: (pct: number) => void): Promise<VideoEditorAudio> =>
        new Promise((resolve, reject) => {
          const form = new FormData();
          form.append("file", file, file.name);
          const xhr = new XMLHttpRequest();
          xhr.open("POST", `${BASE}/api/yas-producer/video-editor/assets/audio/upload`);
          xhr.upload.onprogress = (e) => {
            if (onProgress && e.lengthComputable) onProgress(Math.round((e.loaded / e.total) * 100));
          };
          xhr.onload = () => {
            if (xhr.status >= 200 && xhr.status < 300) {
              try {
                resolve(JSON.parse(xhr.responseText) as VideoEditorAudio);
              } catch {
                reject(new Error("bad upload response"));
              }
            } else {
              let detail = xhr.statusText;
              try { detail = JSON.parse(xhr.responseText)?.detail || detail; } catch { /* */ }
              reject(new Error(`${xhr.status} ${detail}`));
            }
          };
          xhr.onerror = () => reject(new Error("upload network error"));
          xhr.send(form);
        }),
      compositionFileUrl: (projectId: number) =>
        `${BASE}/api/yas-producer/video-editor/${projectId}/composition/file`,
      sourceVideoUrl: (kind: string, id: number) =>
        `${BASE}/api/yas-producer/video-editor/sources/${kind}/${id}/video`,
      sourceThumbUrl: (kind: string, id: number) =>
        `${BASE}/api/yas-producer/video-editor/sources/${kind}/${id}/thumb`,
      // AI assist (Phase 3) — each POST enqueues ONE background job per project
      // (409 while one is active; 422 when a provider key / required field is
      // missing). Poll `status` until done/failed, then refetch the project.
      ai: {
        // Conversational op-planner: sees the conversation + clips + selection,
        // then applies ops directly (job null, applied>0) or routes to a
        // generation feature (job set). Reply-only turns: job null, applied 0.
        chat: (
          projectId: number,
          body: {
            message: string;
            history?: { role: string; text: string }[];
            selected_clip_ids?: string[];
            playhead_frame?: number;
            attachments?: Array<{
              kind: string;
              name: string;
              asset_id?: number;
              font_family?: string;
              brand_font?: boolean;
              source_kind?: string;
              source_id?: number;
              variant?: string;
            }>;
          },
        ) =>
          http<{
            job: VideoEditAiJob | null;
            reply: string;
            feature: string | null;
            applied?: number;
            rev?: number;
          }>(
            `/api/yas-producer/video-editor/${projectId}/ai/chat`,
            { method: "POST", body: JSON.stringify(body) },
          ),
        analyze: (projectId: number, force = false) =>
          http<{ job: VideoEditAiJob }>(
            `/api/yas-producer/video-editor/${projectId}/ai/analyze`,
            { method: "POST", body: JSON.stringify({ force }) },
          ),
        status: (projectId: number) =>
          http<VideoEditAiStatus>(
            `/api/yas-producer/video-editor/${projectId}/ai/status`,
          ),
        popups: (projectId: number, body: { text: string; hint?: string }) =>
          http<{ job: VideoEditAiJob }>(
            `/api/yas-producer/video-editor/${projectId}/ai/popups`,
            { method: "POST", body: JSON.stringify(body) },
          ),
        compose: (projectId: number, body: { keys?: string[]; style?: string; generate_bookends?: boolean } = {}) =>
          http<{ job: VideoEditAiJob }>(
            `/api/yas-producer/video-editor/${projectId}/ai/compose`,
            { method: "POST", body: JSON.stringify(body) },
          ),
        edit: (projectId: number, body: { instruction?: string } = {}) =>
          http<{ job: VideoEditAiJob }>(
            `/api/yas-producer/video-editor/${projectId}/ai/edit`,
            { method: "POST", body: JSON.stringify(body) },
          ),
        voice: (
          projectId: number,
          body: {
            clip_id: string;
            voice_id?: string | null;
            use_cloned?: boolean; // re-voice with the saved "my voice" clone
            use_as_is?: boolean; // drop the uploaded audio in directly (no cloning)
            source_upload_id?: number | null; // audio-asset id for use_as_is
          },
        ) =>
          http<{ job: VideoEditAiJob }>(
            `/api/yas-producer/video-editor/${projectId}/ai/voice`,
            { method: "POST", body: JSON.stringify(body) },
          ),
        // "My voice" clone — one per user (NOT per project). POST a short audio
        // sample (multipart, field `files`) → ElevenLabs instant voice clone.
        voiceClone: async (audio: File | Blob, name?: string): Promise<VideoEditVoiceClone> => {
          const form = new FormData();
          const fname = audio instanceof File ? audio.name : "my-voice.webm";
          form.append("files", audio, fname);
          if (name) form.append("name", name);
          const res = await fetch(`${BASE}/api/yas-producer/video-editor/ai/voice-clone`, {
            method: "POST",
            body: form, // browser sets the multipart Content-Type + boundary
          });
          if (!res.ok) {
            let detail = res.statusText;
            try { detail = (await res.json())?.detail || detail; } catch { /* */ }
            throw new Error(`${res.status} ${detail}`);
          }
          return res.json() as Promise<VideoEditVoiceClone>;
        },
        getVoiceClone: () =>
          http<VideoEditVoiceClone | null>(
            "/api/yas-producer/video-editor/ai/voice-clone",
          ),
        deleteVoiceClone: () =>
          http<{ ok: boolean }>("/api/yas-producer/video-editor/ai/voice-clone", {
            method: "DELETE",
          }),
        music: (projectId: number, body: { mode?: "auto" | "suno" | "library"; query?: string } = {}) =>
          http<{ job: VideoEditAiJob }>(
            `/api/yas-producer/video-editor/${projectId}/ai/music`,
            { method: "POST", body: JSON.stringify(body) },
          ),
        sfx: (projectId: number, body: { clip_id?: string; query?: string; mode?: string } = {}) =>
          http<{ job: VideoEditAiJob }>(
            `/api/yas-producer/video-editor/${projectId}/ai/sfx`,
            { method: "POST", body: JSON.stringify(body) },
          ),
        bookend: (projectId: number, body: { role: "intro" | "outro" | "both"; reuse: boolean }) =>
          http<{ job: VideoEditAiJob }>(
            `/api/yas-producer/video-editor/${projectId}/ai/bookend`,
            { method: "POST", body: JSON.stringify(body) },
          ),
        // B-roll preset — one-shot order/join/bookends/music pass.
        broll: (
          projectId: number,
          body: {
            intro_outro?: boolean;
            bookend_mode?: "generate" | "upload";
            music?: boolean;
            music_mode?: "auto" | "suno" | "library";
            title?: string;
            intro_topic?: string;
            outro_topic?: string;
            intro_upload_id?: number | null;
            outro_upload_id?: number | null;
          } = {},
        ) =>
          http<{ job: VideoEditAiJob }>(
            `/api/yas-producer/video-editor/${projectId}/ai/broll`,
            { method: "POST", body: JSON.stringify(body) },
          ),
        // Agentic loop — the model watches the video, thinks, and edits with
        // tools. Same job contract; job.agent_steps narrates what it did.
        agent: (
          projectId: number,
          body: { instruction: string; clip_id?: string | null },
        ) =>
          http<{ job: VideoEditAiJob }>(
            `/api/yas-producer/video-editor/${projectId}/ai/agent`,
            { method: "POST", body: JSON.stringify(body) },
          ),
        // Which reasoning brain is live (probed + cached server-side) — not
        // per-project, so no project id.
        providers: () =>
          http<VideoEditAiProviders>(
            "/api/yas-producer/video-editor/ai/providers",
          ),
      },
    },

    // Library — unified history of past Yas Producer work (talking-head runs +
    // poster+human images/videos + video-editor projects). Lightweight list for
    // the Library grid; type/video filtering is done client-side from the cache.
    library: {
      list: (params: { limit?: number; offset?: number } = {}) => {
        const q = new URLSearchParams();
        if (params.limit != null) q.set("limit", String(params.limit));
        if (params.offset != null) q.set("offset", String(params.offset));
        const qs = q.toString();
        return http<YasLibraryList>(
          `/api/yas-producer/library${qs ? "?" + qs : ""}`,
        );
      },
    },

    // Aggregate studio summary (counts by type/status, lifetime spend, 14-day
    // activity, content funnel) for the command-center Dashboard.
    stats: () => http<YasStats>("/api/yas-producer/stats"),

    // Summary of what WE posted to Instagram (count + real likes/comments).
    instagramOwn: () =>
      http<InstagramOwnSummary>("/api/yas-producer/instagram/own-summary"),
  },

  // Trend Finder — declared above the literal (see `trendsBase`/`trends`) so
  // the short aliases can reference their long-named twins without `api`
  // referring to itself mid-initialiser.
  trends,

  // General trends — same pattern, declared above as `generalTrends`.
  generalTrends,
  radar,

  // Social-listening report — declared above as `reports`.
  reports,

  // ----- Producer Agent -----

  producer: {
    createRun: (body: {
      mode: ProducerMode;
      title?: string;
      video_type?: string;
      dialogue_language?: DialogueLanguage;
    }) =>
      http<ProducerRunRow>("/api/agents/producer/runs", {
        method: "POST",
        body: JSON.stringify(body),
      }),

    types: () =>
      http<{ items: ProducerVideoType[] }>("/api/agents/producer/types"),

    listRuns: (limit = 20) =>
      http<{ items: ProducerRunRow[] }>(
        `/api/agents/producer/runs?limit=${limit}`,
      ),

    getRun: (id: number) =>
      http<ProducerRunDetail>(`/api/agents/producer/runs/${id}`),

    cancelRun: (id: number) =>
      http<{ run_id: number; cancelled: boolean }>(
        `/api/agents/producer/runs/${id}/cancel`,
        { method: "POST" },
      ),

    generateIdeas: (runId: number, count = 5) =>
      http<{ items: ProducerIdea[] }>(
        `/api/agents/producer/runs/${runId}/ideas`,
        { method: "POST", body: JSON.stringify({ count }) },
      ),

    submitUserIdea: (runId: number, roughIdea: string) =>
      http<ProducerIdea>(`/api/agents/producer/runs/${runId}/user-idea`, {
        method: "POST",
        body: JSON.stringify({ rough_idea: roughIdea }),
      }),

    decideIdea: (
      ideaId: number,
      body: { action: "approve" | "decline" | "retry"; reason?: string },
    ) =>
      http<ProducerIdea>(`/api/agents/producer/ideas/${ideaId}/decision`, {
        method: "POST",
        body: JSON.stringify(body),
      }),

    uploadReference: async (
      runId: number,
      params: { file?: File; raw_text?: string; kind?: string },
    ) => {
      const form = new FormData();
      if (params.file) form.append("file", params.file, params.file.name);
      if (params.raw_text) form.append("raw_text", params.raw_text);
      if (params.kind) form.append("kind", params.kind);
      const res = await fetch(`${BASE}/api/agents/producer/runs/${runId}/references`, {
        method: "POST",
        body: form,
      });
      if (!res.ok) {
        let detail = res.statusText;
        try {
          detail = (await res.json())?.detail || detail;
        } catch { /* */ }
        throw new Error(`${res.status} ${detail}`);
      }
      return (await res.json()) as ProducerReference;
    },

    decideReference: (
      refId: number,
      body: { action: "approve" | "decline"; reason?: string },
    ) =>
      http<ProducerReference>(
        `/api/agents/producer/references/${refId}/decision`,
        { method: "POST", body: JSON.stringify(body) },
      ),

    generateIdeaFromReference: (refId: number, user_notes?: string) =>
      http<ProducerIdea>(`/api/agents/producer/references/${refId}/idea`, {
        method: "POST",
        body: JSON.stringify({ user_notes }),
      }),

    referenceFileUrl: (refId: number) =>
      `${BASE}/api/agents/producer/references/${refId}/file`,

    generateScript: (
      ideaId: number,
      body: {
        retry_feedback?: string;
        scene_count?: 1 | 2 | 3 | null;
        scene_duration_mode?: "auto" | "4" | "6" | "8";
      } = {},
    ) =>
      http<ProducerScript>(`/api/agents/producer/ideas/${ideaId}/script`, {
        method: "POST",
        body: JSON.stringify(body),
      }),

    decideScript: (
      scriptId: number,
      body: { action: "approve" | "decline" | "retry"; reason?: string },
    ) =>
      http<ProducerScript>(`/api/agents/producer/scripts/${scriptId}/decision`, {
        method: "POST",
        body: JSON.stringify(body),
      }),

    generateVideo: (
      scriptId: number,
      body: {
        use_premium_model?: boolean;
        with_audio?: boolean;
        duration_sec?: number;
        aspect_ratio?: string;
        reference_image_paths?: string[];
        auto_reference?: boolean;
        scene_index?: number; // 1-based; default 1
      } = {},
    ) =>
      http<ProducerVideo>(`/api/agents/producer/scripts/${scriptId}/video`, {
        method: "POST",
        body: JSON.stringify(body),
      }),

    // v5.1: cost preview without firing. Returns per-scene cost breakdown.
    renderAllScenesEstimate: (
      scriptId: number,
      body: { with_audio?: boolean; use_premium_model?: boolean } = {},
    ) =>
      http<{
        scene_count: number;
        per_scene: Array<{
          scene_index: number;
          duration_sec: number;
          model: string;
          est_usd: number;
          already_live: boolean;
        }>;
        total_est_usd: number;
        live_scene_indexes: number[];
        max_script_cost_usd: number;
        over_cap: boolean;
      }>(`/api/agents/producer/scripts/${scriptId}/render-all-estimate`, {
        method: "POST",
        body: JSON.stringify(body),
      }),

    // v5.1: fire Veo for every scene in parallel. Six cost-guard layers
    // enforced server-side. Skips scenes that already have a live video.
    renderAllScenes: (
      scriptId: number,
      body: {
        with_audio?: boolean;
        use_premium_model?: boolean;
        aspect_ratio?: string;
        auto_reference?: boolean;
      } = {},
    ) =>
      http<{
        script_id: number;
        submitted: ProducerVideo[];
        skipped_scene_indexes: number[];
        errors: Array<{ scene_index: number; error: string }>;
        total_est_usd: number;
        max_script_cost_usd: number;
      }>(`/api/agents/producer/scripts/${scriptId}/render-all`, {
        method: "POST",
        body: JSON.stringify(body),
      }),

    // v5.1: manual Remotion concat of every ready scene-video.
    composeAllScenes: (
      scriptId: number,
      body: { crossfade_sec?: number } = {},
    ) =>
      http<ProducerComposition>(
        `/api/agents/producer/scripts/${scriptId}/compose-all`,
        { method: "POST", body: JSON.stringify(body) },
      ),

    previewScriptImage: (
      scriptId: number,
      body: { user_text?: string; aspect_ratio?: string } = {},
    ) =>
      http<ProducerReference>(
        `/api/agents/producer/scripts/${scriptId}/preview-image`,
        { method: "POST", body: JSON.stringify(body) },
      ),

    uploadScriptImage: async (scriptId: number, file: File) => {
      const form = new FormData();
      form.append("file", file, file.name);
      const res = await fetch(
        `${BASE}/api/agents/producer/scripts/${scriptId}/upload-image`,
        { method: "POST", body: form },
      );
      if (!res.ok) {
        let detail = res.statusText;
        try { detail = (await res.json())?.detail || detail; } catch { /* */ }
        throw new Error(`${res.status} ${detail}`);
      }
      return (await res.json()) as ProducerReference;
    },

    refineReference: (
      referenceId: number,
      body: { user_text?: string; aspect_ratio?: string } = {},
    ) =>
      http<ProducerReference>(
        `/api/agents/producer/references/${referenceId}/variation`,
        { method: "POST", body: JSON.stringify(body) },
      ),

    composeFromVideo: (videoId: number, body: { crossfade_sec?: number } = {}) =>
      http<ProducerComposition>(
        `/api/agents/producer/videos/${videoId}/compose`,
        { method: "POST", body: JSON.stringify(body) },
      ),

    getComposition: (compositionId: number) =>
      http<ProducerComposition>(
        `/api/agents/producer/compositions/${compositionId}`,
      ),

    compositionFileUrl: (compositionId: number) =>
      `${BASE}/api/agents/producer/compositions/${compositionId}/file`,

    decideComposition: (
      compositionId: number,
      body: { action: "approve" | "decline" | "retry"; reason?: string },
    ) =>
      http<ProducerComposition>(
        `/api/agents/producer/compositions/${compositionId}/decision`,
        { method: "POST", body: JSON.stringify(body) },
      ),

    batchVideos: (
      runId: number,
      body: {
        with_audio?: boolean;
        use_premium_model?: boolean;
        duration_sec?: number;
        aspect_ratio?: string;
        script_ids?: number[];
      } = {},
    ) =>
      http<{
        items: ProducerVideo[];
        submitted: number;
        skipped: number;
        errors?: Array<{ script_id: number; error: string }>;
      }>(`/api/agents/producer/runs/${runId}/batch-videos`, {
        method: "POST",
        body: JSON.stringify(body),
      }),

    getVideo: (id: number) =>
      http<ProducerVideo>(`/api/agents/producer/videos/${id}`),

    videoFileUrl: (id: number) =>
      `${BASE}/api/agents/producer/videos/${id}/file`,

    decideVideo: (
      videoId: number,
      body: { action: "approve" | "decline" | "retry"; reason?: string },
    ) =>
      http<ProducerVideo>(`/api/agents/producer/videos/${videoId}/decision`, {
        method: "POST",
        body: JSON.stringify(body),
      }),

    extendVideo: (
      videoId: number,
      body: {
        user_text?: string;
        use_premium_model?: boolean;
        with_audio?: boolean;
      } = {},
    ) =>
      http<ProducerVideo>(`/api/agents/producer/videos/${videoId}/extend`, {
        method: "POST",
        body: JSON.stringify(body),
      }),

    budget: () => http<ProducerBudget>("/api/agents/producer/budget"),

    library: {
      productions: (params: { limit?: number; offset?: number; status?: string; mode?: string } = {}) => {
        const q = new URLSearchParams();
        if (params.limit != null) q.set("limit", String(params.limit));
        if (params.offset != null) q.set("offset", String(params.offset));
        if (params.status) q.set("status", params.status);
        if (params.mode) q.set("mode", params.mode);
        const qs = q.toString();
        return http<{ items: ProducerRunRow[] }>(
          `/api/agents/producer/library/productions${qs ? "?" + qs : ""}`,
        );
      },
      ideas: (params: { limit?: number; offset?: number; status?: string; mode?: string } = {}) => {
        const q = new URLSearchParams();
        if (params.limit != null) q.set("limit", String(params.limit));
        if (params.offset != null) q.set("offset", String(params.offset));
        if (params.status) q.set("status", params.status);
        if (params.mode) q.set("mode", params.mode);
        const qs = q.toString();
        return http<{ items: LibraryIdeaRow[] }>(
          `/api/agents/producer/library/ideas${qs ? "?" + qs : ""}`,
        );
      },
      scripts: (params: { limit?: number; offset?: number; status?: string } = {}) => {
        const q = new URLSearchParams();
        if (params.limit != null) q.set("limit", String(params.limit));
        if (params.offset != null) q.set("offset", String(params.offset));
        if (params.status) q.set("status", params.status);
        const qs = q.toString();
        return http<{ items: LibraryScriptRow[] }>(
          `/api/agents/producer/library/scripts${qs ? "?" + qs : ""}`,
        );
      },
      videos: (params: { limit?: number; offset?: number; status?: string } = {}) => {
        const q = new URLSearchParams();
        if (params.limit != null) q.set("limit", String(params.limit));
        if (params.offset != null) q.set("offset", String(params.offset));
        if (params.status) q.set("status", params.status);
        const qs = q.toString();
        return http<{ items: LibraryVideoRow[] }>(
          `/api/agents/producer/library/videos${qs ? "?" + qs : ""}`,
        );
      },
    },
  },

  // ----- Autonomous campaigns ("Producer" tab, beta) -----
  // Note what is NOT here: publishing. Approving an item (gate 1) only says the
  // content is good; the final Post (gate 2) goes through roadmapApi.approve()
  // on the booked scheduled post, which is the app's single publish path.
  campaigns: {
    list: (includeArchived = false) =>
      http<CampaignList>(
        `/api/campaigns${includeArchived ? "?include_archived=true" : ""}`,
      ),
    get: (campaignId: number, cycle?: number) => {
      const q = new URLSearchParams();
      if (cycle != null) q.set("cycle", String(cycle));
      const qs = q.toString();
      return http<CampaignDetail>(
        `/api/campaigns/${campaignId}${qs ? "?" + qs : ""}`,
      );
    },
    create: (body: CampaignCreateRequest) =>
      http<Campaign>("/api/campaigns", {
        method: "POST",
        body: JSON.stringify(body),
      }),
    patch: (campaignId: number, body: CampaignPatchRequest) =>
      http<Campaign>(`/api/campaigns/${campaignId}`, {
        method: "PATCH",
        body: JSON.stringify(body),
      }),
    remove: (campaignId: number) =>
      http<{ deleted: boolean; id: number; bookings_cancelled: number }>(
        `/api/campaigns/${campaignId}`,
        { method: "DELETE" },
      ),
    /** Plan the next cycle on the worker's next tick. 409s if work is in flight. */
    runCycle: (campaignId: number) =>
      http<Campaign>(`/api/campaigns/${campaignId}/run-cycle`, { method: "POST" }),
    /** Everything awaiting gate 1, across all campaigns. */
    reviewQueue: () => http<CampaignReviewQueue>("/api/campaigns/review-queue"),
    /** Cheap poll for the sidebar badge. */
    summary: () => http<CampaignSummary>("/api/campaigns/summary"),
    item: (itemId: number) => http<CampaignItem>(`/api/campaigns/items/${itemId}`),
    /** GATE 1 — approve the CONTENT. Does not publish. 409 if already decided. */
    approve: (itemId: number, body: { caption?: string; proposed_time?: string } = {}) =>
      http<CampaignItem>(`/api/campaigns/items/${itemId}/approve`, {
        method: "POST",
        body: JSON.stringify(body),
      }),
    /** GATE 1 — reject it. `regenerate` (default true) asks for another attempt. */
    decline: (itemId: number, body: { note?: string; regenerate?: boolean } = {}) =>
      http<CampaignItem>(`/api/campaigns/items/${itemId}/decline`, {
        method: "POST",
        body: JSON.stringify(body),
      }),
    cancel: (itemId: number) =>
      http<CampaignItem>(`/api/campaigns/items/${itemId}/cancel`, { method: "POST" }),
  },
};

// ----- SSE helper -----

export function eventsUrl(agent: AgentId, runId: number): string {
  return `${BASE}/api/agents/${agent}/runs/${runId}/events`;
}
