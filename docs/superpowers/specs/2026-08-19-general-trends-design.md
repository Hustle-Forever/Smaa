# General Trends — design spec (2026-08-19)

A second kind of trend in the **Trends** page: not "what worked for a competitor on
Instagram" but "what the whole internet is doing right now" — the Nihilist Penguin,
a filter, a sound, a moment. Found automatically from free public sources, clustered
into one card per trend, scored for how well **42 Abu Dhabi** could ride it, and
handed to Ideation with the same **Make this** flow the competitor feed already has.

**Hard rule:** the existing competitor finder (Feed / Themes / Competitors tabs, their
components, their backend modules, their tests, `trend_*` tables) is **not modified**.
The only edits to existing files are additive: a new sub-tab entry in
`pages/TrendFinder.tsx`, one union member in `components/trends/bus.ts`, new
methods in `lib/api.ts`, new types in `lib/types.ts`, router + poller registration in
`backend/app/main.py`, new settings in `backend/app/config.py`, docs.

---

## 1. Sources (all verified reachable 2026-08-19, all free, no keys except YouTube)

| id | what | fetch | fields we take |
|---|---|---|---|
| `google_trends` | Daily search spikes per country | `GET https://trends.google.com/trending/rss?geo={GEO}` (RSS, ns `https://trends.google.com/trending/rss`) | `title`, `ht:approx_traffic` ("2000+" → 2000), `ht:picture`, `pubDate`, `ht:news_item` × N (`ht:news_item_title`, `ht:news_item_url`, `ht:news_item_source`) |
| `bluesky` | Bluesky trending topics | `GET https://public.api.bsky.app/xrpc/app.bsky.unspecced.getTrends?limit=25` (JSON) | `trends[].displayName`, `description`, `link` (→ `https://bsky.app{link}`), `startedAt`, `postCount`, `status`, `category` |
| `reddit` | Rising posts on meme/video subs | `GET https://www.reddit.com/r/{sub}/rising.rss?limit=25` (Atom; `.json` is 403 — do NOT use it) with UA `42studio-trends/1.0` | `entry.title`, `link@href`, `published`, `media:thumbnail@url`, `content` (HTML → text) |
| `kym` | Know Your Meme newsfeed (trend explainers) | `GET https://knowyourmeme.com/newsfeed.rss` (RSS) | `title`, `link` (the meme entry URL), `pubDate`, `description` (HTML → first `<img src>` + text) |
| `wikipedia` | Most-viewed articles (today vs yesterday) | `GET https://wikimedia.org/api/rest_v1/metrics/pageviews/top/en.wikipedia/all-access/{Y}/{M}/{D}` for D-1 and D-2 (JSON) | `articles[].article`, `views`, `rank`; keep only rising (views_today > views_yesterday), skip `Main_Page`, `Special:*`, `Wikipedia:*`, `File:*`, `Portal:*`, `Help:*`, `Talk:*`, `-` |
| `youtube` | Most popular videos per region | `GET https://www.googleapis.com/youtube/v3/videos?part=snippet,statistics&chart=mostPopular&regionCode={REGION}&maxResults=25&key={KEY}` — only when `YOUTUBE_API_KEY` set | `snippet.title`, `channelTitle`, `publishedAt`, `thumbnails.high.url`, `statistics.viewCount`, video id → `https://www.youtube.com/watch?v={id}` |
| `x_trends` | X/Twitter trends via the trends24.in mirror | `GET https://trends24.in/{country-slug}/` (HTML) — the FIRST `<ol class="trend-card__list">` (most recent hour); items `<a class="trend-link">text</a>` + `<span class="tweet-count" data-count="N">` | trend text, tweet count (may be empty), link |

Regions: `GENERAL_TRENDS_GEOS` (default `US,AE`) drives Google Trends `geo`, YouTube
`regionCode`, trends24 slug (`US→united-states`, `AE→united-arab-emirates`,
`GB→united-kingdom`, `IN→india`, `SA→saudi-arabia`, `EG→egypt`; unknown → skipped).
Bluesky, Reddit, KYM, Wikipedia are region `global`.

Every fetch: `httpx.AsyncClient(timeout=20, follow_redirects=True)`, UA =
`config.user_agent` except Reddit which needs the bot UA above. Reddit's anonymous RSS is
throttled per IP (measured: `x-ratelimit-remaining: 0` after ONE request, `x-ratelimit-reset: N`
seconds); `fetch_reddit` paces itself off those headers, subreddit units run through a single
lane in `fetch_all` (`SERIAL_SOURCES`), and a 429 is retried after the reset. A source failure never
fails the run: it is recorded in `run.metadata.sources[source] = {"ok": false, "error": …}`
and the run continues (mirrors "single handle unreachable → recorded, scan continues").

Parsers are **pure functions over text/JSON** (`parse_google_trends_rss(xml, geo)`,
`parse_bluesky(json)`, `parse_reddit_atom(xml, sub)`, `parse_kym_rss(xml)`,
`parse_wikipedia_top(today_json, yesterday_json)`, `parse_youtube(json, region)`,
`parse_trends24(html, geo)`) — so tests feed fixtures and never touch the network.
Fetchers are `async fetch_<source>(client, …) -> list[RawItem]` and are the monkeypatch
seam (`monkeypatch.setattr(gts, "fetch_google_trends", …)`).

### RawItem (plain dict, produced by every parser)
```python
{
  "source": "google_trends",            # one of the ids above
  "source_key": "US:pete hegseth",      # stable id within the source (geo:title / bsky topic id / reddit post id / kym slug / wiki article / yt video id / geo:trend text)
  "title": "pete hegseth",              # as the source shows it
  "url": "https://…",                   # best link (first news url / bsky feed / reddit post / kym entry / wiki page / yt watch / x search)
  "summary": "…",                       # short evidence text (news titles joined " · " / description / stripped content), ≤ 600 chars, may be ""
  "image_url": "https://…" | None,
  "metric_value": 2000.0 | None,        # numeric size signal
  "metric_label": "searches" | "posts" | "views" | "tweets" | None,
  "published_at": "2026-08-18T19:30:00+00:00" | None,   # ISO string
  "region": "US" | "AE" | "global",
  "category": "politics" | None,        # only bluesky/youtube give one
  "extra": {}                           # source-specific (news_items, rank, views_delta, sub, channel …)
}
```

---

## 2. Data model (`backend/app/services/general_trends_store.py`, self-creating DDL, same pattern as `trend_store.py`)

```sql
CREATE TABLE IF NOT EXISTS general_trend_runs (
    id            BIGSERIAL PRIMARY KEY,
    status        TEXT NOT NULL DEFAULT 'queued',      -- queued|fetching|clustering|scoring|done|failed
    phase         TEXT,
    progress      TEXT,
    sources_total INT NOT NULL DEFAULT 0,
    sources_done  INT NOT NULL DEFAULT 0,
    items_found   INT NOT NULL DEFAULT 0,
    trends_new    INT NOT NULL DEFAULT 0,
    trends_touched INT NOT NULL DEFAULT 0,
    trends_scored INT NOT NULL DEFAULT 0,
    error         TEXT,
    started_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at   TIMESTAMPTZ,
    metadata      JSONB NOT NULL DEFAULT '{}'::jsonb   -- {"trigger":"manual"|"auto", "sources": {id: {ok, count, error, ms}}}
);
CREATE INDEX IF NOT EXISTS idx_general_trend_runs_recent ON general_trend_runs (started_at DESC);

CREATE TABLE IF NOT EXISTS general_trends (
    id               BIGSERIAL PRIMARY KEY,
    slug             TEXT NOT NULL UNIQUE,              -- normalise_title(title) of the FIRST sighting; exact-match key
    title            TEXT NOT NULL,                     -- display name (AI clean_title overrides after scoring)
    summary          TEXT,                              -- best source evidence text
    url              TEXT,
    image_url        TEXT,
    local_thumb_path TEXT,
    category         TEXT,
    regions          JSONB NOT NULL DEFAULT '[]'::jsonb,   -- distinct region codes seen
    sources          JSONB NOT NULL DEFAULT '[]'::jsonb,   -- distinct source ids seen
    source_count     INT NOT NULL DEFAULT 0,
    sighting_count   INT NOT NULL DEFAULT 0,
    peak_metric      REAL,
    velocity         REAL,                              -- 0..1 heat, recomputed every run
    first_seen_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    embedding        vector(768),
    -- AI (mirrors trend_posts so the card can share the same chips)
    topic            TEXT, topic_summary TEXT, theme TEXT,
    trend_kind       TEXT,                              -- meme|format|sound|challenge|news|event|product|culture|other
    sensitivity      TEXT,                              -- ok|caution|avoid
    fit_score        INT, fit_reason TEXT, uae_relevance INT,
    suggested_angle  TEXT, suggested_format TEXT, effort TEXT,
    tags             JSONB NOT NULL DEFAULT '[]'::jsonb,
    opportunity_score REAL,
    scored_at        TIMESTAMPTZ,
    status           TEXT NOT NULL DEFAULT 'new',       -- new|saved|dismissed|used
    metadata         JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_general_trends_rank ON general_trends (status, opportunity_score DESC NULLS LAST);
CREATE INDEX IF NOT EXISTS idx_general_trends_seen ON general_trends (last_seen_at DESC);
CREATE INDEX IF NOT EXISTS idx_general_trends_unscored ON general_trends (scored_at) WHERE scored_at IS NULL;

CREATE TABLE IF NOT EXISTS general_trend_sightings (
    id           BIGSERIAL PRIMARY KEY,
    trend_id     BIGINT NOT NULL REFERENCES general_trends(id) ON DELETE CASCADE,
    run_id       BIGINT REFERENCES general_trend_runs(id) ON DELETE SET NULL,
    source       TEXT NOT NULL,
    source_key   TEXT NOT NULL,
    title        TEXT, url TEXT, image_url TEXT, summary TEXT,
    metric_value REAL, metric_label TEXT,
    region       TEXT, category TEXT,
    published_at TIMESTAMPTZ,
    seen_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    extra        JSONB NOT NULL DEFAULT '{}'::jsonb,
    UNIQUE (run_id, source, source_key)
);
CREATE INDEX IF NOT EXISTS idx_general_trend_sightings_trend ON general_trend_sightings (trend_id, seen_at DESC);
CREATE INDEX IF NOT EXISTS idx_general_trend_sightings_key ON general_trend_sightings (source, source_key, seen_at DESC);
```
Reads `get_conn(autocommit=True)`, writes `get_conn()`, JSONB via `Jsonb`, embeddings
passed as `list[float]` with `%s::vector` (as `yas_asset_rag.py` does), `_ensured` flag +
`ensure_tables()` at the top of every public function, `python -m app.services.general_trends_store`
pre-creates (with the Windows selector policy lines).

Store API (all `async`): `ensure_tables()`, `create_run(metadata) -> dict`, `update_run(run_id, **fields)`
(whitelist status/phase/progress/sources_total/sources_done/items_found/trends_new/trends_touched/trends_scored/error/metadata;
auto `finished_at` on done/failed), `pending_runs(limit=1)`, `claim_run(run_id) -> bool`,
`fail_stale_runs() -> int` (RUN_STALE_AFTER_SEC = 30*60), `latest_run() -> dict|None` (adds `active`),
`run_is_active() -> bool`, `find_trend_by_slug(slug)`, `find_trend_by_source_key(source, source_key, since_days=30)`
(via sightings), `nearest_trends(embedding, *, limit=5, since_days=14) -> list[{id, slug, title, distance}]`
(`embedding <=> %s::vector`), `create_trend(fields) -> dict`, `touch_trend(trend_id, *, item: RawItem, seen_at)`
(bumps sighting_count, merges sources/regions, keeps best image/url/summary if missing, updates last_seen_at/peak_metric),
`insert_sighting(trend_id, run_id, item)` (ON CONFLICT DO NOTHING), `sightings_for(trend_ids, *, per_trend=6) -> dict[int, list]`,
`recent_sightings_for_velocity(since_hours=72) -> dict[int, list[{seen_at, source}]]`, `save_velocity(trend_id, velocity)`,
`unscored_trends(limit=200)`, `save_score(trend_id, score)`, `clear_scores(ids=None) -> int`,
`update_status(trend_id, status)`, `set_local_thumb(trend_id, rel_path)`, `get_trend(trend_id)`,
`query_trends(*, status, region, source, theme, kind, min_fit, q, include_sensitive, since_days=14, cap=1000) -> list[dict]`
(no status filter ⇒ `status <> 'dismissed'`; `include_sensitive=False` ⇒ `sensitivity IS DISTINCT FROM 'avoid'`;
`region` matches `regions ? %s`; `source` matches `sources ? %s`; `q` ILIKE on title/summary/topic),
`theme_rollup()`, `stats()` → `{trends_total, trends_unscored, trends_saved, trends_new_24h, last_run_at, sources_ok, sources_total}`.

---

## 3. Pipeline (`backend/app/services/general_trends_pipeline.py`)

`async run_refresh(run_id: int) -> None` — owns the terminal run status, never raises.

1. **fetch** (`status=fetching`): build the fetch plan from config (`enabled sources × geos`),
   run all with `asyncio.gather(return_exceptions=True)` under a `Semaphore(6)`; each result
   → list[RawItem]; per-source stats into `run.metadata.sources`. Cap `GENERAL_TRENDS_MAX_ITEMS_PER_SOURCE`
   (default 40) per (source, geo). Skip items with empty title.
2. **cluster** (`status=clustering`) — `cluster_items(items, *, embed) -> ClusterResult`:
   - `slug = normalise_title(title)`: lowercase, strip `#`/`@`, unicode-NFKC, drop punctuation
     except spaces, collapse whitespace, drop leading articles (`the`, `a`, `an`), strip. Empty slug ⇒ item skipped.
   - order: exact `find_trend_by_source_key` (same source saw the same thing recently) → exact
     `find_trend_by_slug` → **lexical name match** against every trend seen in the last 14 days
     (`store.recent_trend_slugs` → `lexical_match`: token-bounded containment of a multi-word
     name ≥ 8 chars, or ≥ 2 shared significant tokens with Jaccard ≥ 0.5 — added after two live
     runs produced zero cross-source merges: cosine is not discriminative for short titles, a
     true pair `hayden panettiere` ~ `hayden panettiere dies at 36` = 0.158 sits BELOW false
     pairs at 0.123) → embedding KNN (`nearest_trends`, attach when `distance <= 0.12` —
     `KNN_DISTANCE_MAX`; the first draft said 0.18, but measured on gemini-embedding-001 unrelated
     short titles sit at 0.13–0.23 and 0.18 folded 17 unrelated X trends into one card in a real
     run, so the line is 0.12: a false split is far cheaper than a false merge)
     → new trend. Within one run, newly created trends are matched in-memory (numpy cosine over
     the same threshold) so the same story from two sources in one run merges.
   - embeddings via `gemini_embed([f"{title}. {summary[:300]}"...])` in batches of 32; if it raises
     or Vertex is unconfigured, `embed=None` and clustering degrades to exact-match only
     (recorded as `run.metadata.embeddings = "unavailable"`); the run still finishes.
   - for each attached/created trend: `insert_sighting`, `touch_trend`.
   - thumbnails: for trends with `image_url` and no `local_thumb_path`, download best-effort into
     `asset_local_path("general_trend_thumbs", sha256(url)[:32], "jpg")` (own helper, do NOT
     import `trend_finder._download_thumb`).
3. **velocity**: `compute_velocity(trend, sightings, *, now) -> float` (pure, in
   `general_trends_ranking.py`) for every trend seen in the last 14 days, `save_velocity`.
   ```
   source_score = min(source_count / 3, 1.0)                       # cross-platform presence
   recency      = exp(-hours_since_last_seen / 48)
   last24 = sightings in [now-24h, now]; prev24 = sightings in [now-48h, now-24h]
   growth       = min(log1p(last24) / log1p(max(prev24,0) + 1) / 2, 1.0) if last24 else 0
   newness      = 1.0 if first_seen within 72h else 0.5
   velocity     = round(0.40*source_score + 0.30*recency + 0.20*growth + 0.10*newness, 4)
   ```
   `heat_band(v)`: `>=0.7 "hot"`, `>=0.45 "rising"`, else `"warm"`.
4. **score** (`status=scoring`): `score_pending(on_progress)` — `unscored_trends` (seen in
   last 14 days), batches of 8, `gemini_text(prompt, system=_SYSTEM, json_schema=_SCHEMA,
   temperature=0.3, max_tokens=6000, model=config.general_trends_model or None)`. `_raw`
   sentinel ⇒ batch skipped. `trend_scoring._is_unavailable(exc)` ⇒ raise `ScoringUnavailable`
   (from trend_scoring) ⇒ run finishes `done` with a caveat in `progress` (mirror competitor scan).
   Prompt per trend: name, `what we saw` (up to 6 evidence lines `source · region · title · metric`),
   sources list, regions, first/last seen, summary. Brand note via `trend_scoring._brand_note()`.
   Schema item (echoed `index`, 1-based) — **all of these in `required`** because Gemini drops
   optional fields: `index, clean_title (≤60 chars, human name of the trend), topic (transferable
   angle ≤8 words), topic_summary (what the trend IS, 1–2 plain sentences), theme (bucket),
   trend_kind (enum above), sensitivity (ok|caution|avoid — deaths, tragedies, politics, religion,
   sexual, drugs ⇒ avoid; grief/health/controversy ⇒ caution), fit_score 0-100, fit_reason,
   uae_relevance 0-100, suggested_angle (how 42 rides it — the specific reframe for a coding school
   in Abu Dhabi), suggested_format (reel|carousel|static), effort (low|medium|high), tags`.
   `parse_batch_response` mirrors trend_scoring's clamping (`_clamp_int`, `_one_of`, bounds).
   `save_score` also sets `title = clean_title` when present.
5. `status=done`, `progress = "N trends from M sources · K new"`.

`start_refresh(*, trigger="manual") -> dict` raises `RefreshBusy` if `run_is_active()`, else
`create_run(metadata={"trigger": trigger})`.

### Ranking (`general_trends_ranking.py`, pure)
`DEFAULT_WEIGHTS = {"velocity": 0.4, "fit": 0.4, "uae": 0.2}`, `normalise_weights`,
`compute_opportunity(trend, weights)` = `100*(w_v*velocity + w_f*fit/100 + w_u*uae/100)`
(velocity-only when unscored), `SORTS = ("opportunity","velocity","fit","recent","sources")`,
`rank_trends(rows, *, sort, weights)` — always recomputes `opportunity_score`.

### Poller (`backend/app/workers/general_trends_poller.py`)
Same shape as `trend_scan_poller.py`, `POLL_INTERVAL_SEC = 30`. `_poll_once`: `fail_stale_runs`
→ if no active run and `config.general_trends_refresh_hours > 0` and (no run ever OR latest
`finished_at` older than that many hours) ⇒ `create_run(metadata={"trigger":"auto"})` (pure
helper `auto_refresh_due(latest_run, *, now, every_hours) -> bool` is tested) → `pending_runs(1)`
→ `claim_run` → `run_refresh`. Registered unconditionally in `main.py` lifespan next to
`trend_scan_poller` (start after, stop before).

### Ideation (`backend/app/services/general_trend_ideation.py`)
`generate_prefill(*, trend_id, tool) -> dict` — same error taxonomy as `trend_ideation`
(`ValueError` unknown tool, `LookupError` missing, `IdeationUnavailable`, `RuntimeError`).
Builds its own prompt (`build_prompt(*, trend, sightings, tool, brand_note)` — "THE TREND:" block,
evidence lines, scorer output, then `trend_ideation._TOOL_BRIEFS[tool]`), reuses
`trend_ideation.schema_for(tool)`, `trend_ideation.IdeationUnavailable`, and
`trend_ideation.parse_prefill(raw, tool=tool, post={"id": trend.id, "topic": trend.topic,
"caption": trend.summary, "suggested_angle": …, "topic_summary": …, "permalink": trend.url})`
so the frontend gets the identical `TrendPrefill` envelope (`post_id` = trend id).

### Config (`config.py`, frozen dataclass fields + `from_env` lines)
`general_trends_sources: list[str]` (`GENERAL_TRENDS_SOURCES`, default
`google_trends,bluesky,reddit,kym,wikipedia,youtube,x_trends`), `general_trends_geos: list[str]`
(`GENERAL_TRENDS_GEOS`, `US,AE`), `general_trends_subreddits: list[str]`
(`GENERAL_TRENDS_SUBREDDITS`, `memes,TikTokCringe,videos,interestingasfuck,OutOfTheLoop,Damnthatsinteresting,popular`),
`general_trends_refresh_hours: int` (`GENERAL_TRENDS_REFRESH_HOURS`, 6; 0 = manual only),
`general_trends_max_items_per_source: int` (40), `general_trends_model: str` (`GENERAL_TRENDS_MODEL`, "" ⇒ default Gemini),
`youtube_api_key: str` (`YOUTUBE_API_KEY`, ""). Document all in `backend/.env.example` if it exists.

---

## 4. API (`backend/app/routers/general_trends.py`, `APIRouter(prefix="/api/trends/general", tags=["trends"])`)

| Method | Path | Body / query | Returns |
|---|---|---|---|
| GET | `/stats` | — | `store.stats()` + `{"refresh_every_hours": int, "next_auto_refresh_at": iso|null}` |
| GET | `/sources` | — | `{"items": [{id, label, enabled, regions: [..], ok: bool|null, count: int|null, error: str|null}]}` from config + latest run metadata |
| POST | `/refresh` | — | `{"run": …}`; 409 `RefreshBusy` |
| GET | `/runs/latest` | — | `{"run": {...RUN_COLS, active} | null}` |
| GET | `/items` | `sort=opportunity`, `status?`, `region?`, `source?`, `theme?`, `kind?`, `min_fit?` (0-100), `q?`, `include_sensitive=false`, `limit=60` (1-200), `offset=0`, `w_vel?/w_fit?/w_uae?` (0-1) | `{"items": [trend + "evidence": [{source, region, title, url, metric_value, metric_label, seen_at}] + "heat": "hot|rising|warm"], "total", "weights"}` (evidence via `sightings_for` for the page only) |
| PATCH | `/items/{id}` | `{"status"}` | `{"trend"}`; 422 bad status; 404 |
| GET | `/items/{id}/thumb` | — | FileResponse jpeg; 404 none; 410 gone |
| POST | `/items/rescore` | `{"ids"?: [..]}` | `{"scored": n}`; 422 ScoringUnavailable |
| POST | `/items/{id}/ideas` | `{"tool"}` | `{"prefill": TrendPrefill}` (same mapping as `/posts/{id}/ideas`; `IdeationUnavailable` before `RuntimeError`) |
| GET | `/themes` | — | `{"items": [{theme, trend_count, avg_fit, avg_uae, best_score}]}` |

Registered in `main.py` right after `trend_finder.router`.

---

## 5. Frontend

- `pages/TrendFinder.tsx`: `SubTab` += `"general"`; TABS gets, **second** (after Feed):
  `{ key: "general", label: "General trends", title: "What the internet is doing right now",
  sub: "Memes, formats and moments rising across Google, Reddit, Bluesky, Know Your Meme, Wikipedia,
  YouTube and X — clustered into one card each and ranked by how well 42 could ride them.", Icon: Globe }`;
  `normSub` accepts `"general"`; render `{active === "general" && <GeneralTrendsTab />}`.
  Nothing else in the file changes (health banner + scan card stay competitor-only).
- `components/trends/bus.ts`: `TrendsSubTab` += `"general"`.
- `lib/types.ts` (additive): `GeneralTrendKind`, `GeneralTrendSensitivity`, `GeneralTrendHeat`,
  `GeneralTrendSort`, `GeneralTrendEvidence`, `GeneralTrend` (all columns above minus embedding,
  plus `evidence`, `heat`), `GeneralTrendWeights {velocity, fit, uae}`, `GeneralTrendsList`,
  `GeneralTrendRun`, `GeneralTrendStats`, `GeneralTrendSource`, `GeneralTrendTheme`.
- `lib/api.ts` (additive): `api.generalTrends = { stats, sources, refresh, latestRun, list(params),
  patch(id, status), thumbUrl(id), rescore(ids?), ideas(id, tool), themes }`.
- NEW `components/trends/generalTrendsApi.ts`: typed adapter (`generalTrendsApi`) over
  `api.generalTrends` + re-export `humanError` from `./trendsApi`. **No `as unknown as` casts.**
- NEW `components/trends/GeneralTrendsTab.tsx` (prop-less, self-fetching, mirrors `TrendsFeed`):
  - header row: `lead` copy + last-refresh sentence (`Refreshed 2h ago · 6/7 sources`) + `Refresh now`
    (`btn btn-primary`, RefreshCw, disabled while `run.active`, inline mono progress while running,
    3s poll of `/runs/latest` while active, toast on finish) + `Re-score all` (`btn btn-ghost`, confirm).
  - filter card identical in structure to TrendsFeed: Search, Sort (Opportunity blend / Heat / Fit for 42 /
    Most recent / Most sources), Region (All / US / UAE / Global — built from `GENERAL_TRENDS_GEOS` returned by
    `/sources`), Source (All + each source label), Theme (from `/themes`), Min fit, Show (Not dismissed / Everything /
    New only / Saved / Used / Dismissed); second row: `Tune ranking weights` toggle, count line,
    `Clear filters`, a `Show sensitive` toggle (`btn btn-ghost text-xs`, off by default → `include_sensitive`).
    Tuner: three `WeightSlider`s **Heat** ("how fast and how widely it's spreading") / **Fit for 42** /
    **UAE relevance**, disabled unless sort = opportunity, debounced like the feed, `weightsTouched` ref.
  - grid `grid gap-4 sm:grid-cols-2 xl:grid-cols-3` of `GeneralTrendCard`; skeleton ×6; empty-state cascade:
    no run ever → "Nothing fetched yet" + `Refresh now`; run active → "Fetching…"; filters → "Nothing matches"
    + Clear; hidden dismissed → Show dismissed; else "No trends yet".
  - Load more.
- NEW `GeneralTrendCard` (inside the tab file or `GeneralTrendCard.tsx`) — same skeleton as `PostCard`:
  `article.card.card-hover` → image block `aspect-[4/3] bg-bg-muted` with `Thumb src=thumbUrl(id)
  fallbackSrc=image_url` (empty state: a big source glyph, no "no preview" text) → overlay top-left
  **HeatPill** (same visual grammar as `OutperformancePill`: strong=hot `bg-accent text-white`, medium=rising,
  muted=warm; big text "HOT"/"RISING"/"WARM", sub `4 sources`) and top-right kind chip
  (`rounded bg-black/60 … uppercase` — meme/format/news …) → body: title (`clean` title, line-clamp-2),
  chip row `ScoreBadge fit` / `ScoreBadge uae` / theme chip / effort chip / sensitivity chip
  (`caution` → `text-warning border-warning/25 bg-warning/10`, `avoid` → danger colours; `ok` → no chip),
  "What it is" (`topic_summary`, `text-xs text-muted line-clamp-3`), accent box "How 42 could do this"
  (`suggested_angle` + `as a {format}`), **evidence row** (`flex flex-wrap gap-1.5`): one chip per evidence
  item `a.rounded.border.border-border.bg-bg-muted.px-1.5.py-0.5.text-[10px]` with source label
  (`Google` / `Bluesky` / `Reddit` / `KYM` / `Wikipedia` / `YouTube` / `X`) + region + compact metric,
  `target=_blank`; metrics row (`font-mono text-[11px] text-muted`): `Flame velocity%` · `Layers N sightings`
  · `ml-auto first seen {fmtRel}`; footer actions identical: **Make this** (`btn btn-primary flex-1`),
  Save/Saved (`btn-accent` when saved), Dismiss/Restore, external link.
- NEW `components/trends/GeneralMakeThisDialog.tsx`: same markup/flow as `MakeThisDialog` but takes
  `{ trend: GeneralTrend, onClose }`, calls `generalTrendsApi.ideas(trend.id, tool)`, recommended tool =
  `trend_kind in (sound, challenge, format) ? "talking_head_v2" : suggested_format === "reel" ? "general_video" : "poster"`,
  and sends the brief with `postId: p.post_id, permalink: p.permalink` via `sendProducerBrief` (unchanged
  producerBrief contract). `TOOL_ORDER`/`TOOL_BLURB` are re-declared locally (the originals are
  module-private and we don't touch that file).
- Type-check must pass with `npm run typecheck`. Every colour band is a complete literal class string.

---

## 6. Degraded modes

| what's down | what happens |
|---|---|
| a source 4xx/5xx/timeout | recorded in run metadata, chip shows red in `/sources`, run continues |
| Vertex embeddings unavailable | exact-title clustering only, run finishes, `run.metadata.embeddings="unavailable"` |
| Vertex scoring unavailable (billing/403) | run finishes `done` with caveat, cards show unscored (heat only), `Re-score all` later |
| `YOUTUBE_API_KEY` missing | youtube source `enabled: false`, silently skipped |
| refresh already running | POST /refresh → 409, button disabled |
| DB unreachable | poller logs and retries next tick; API 500s like every other store |

## 7. Tests (`backend/tests/test_general_trends.py`, plain pytest + `asyncio.run`, everything mocked)
Parsers (one fixture per source incl. edge cases: empty feed, missing image, "2000+" traffic, Wikipedia
filter list, trends24 first list only), `normalise_title`, `compute_velocity` curve + `heat_band`,
`normalise_weights`/`compute_opportunity`/`rank_trends`, `cluster_items` (exact-slug merge, source_key merge,
KNN merge with fake embed, in-run merge, degrade when embed raises), scoring prompt + parse (index echo,
clamps, `_raw` sentinel, unavailable ⇒ ScoringUnavailable), `auto_refresh_due`, ideation prompt/parse
(uses trend fields, `post_id` = trend id, IdeationUnavailable ordering), store SQL capture for
`query_trends` filters (`include_sensitive`, region `?`), `run_refresh` integration with fake store +
fake fetchers + fake model (terminal status done, source failure recorded, ScoringUnavailable ⇒ done).

## 8. Verification
`cd backend && ./.venv/Scripts/python.exe -m pytest tests/ -q` (all green, incl. the existing 186 trend
tests untouched), `cd frontend && npm run typecheck`, then one real `python -m app.services.general_trends_pipeline`
run from this machine (Windows selector policy) against the live sources + DB, and a screenshot pass of the tab.

## 9. Out of scope (deliberately)
TikTok Creative Center (login-gated), paid aggregators, per-trend detail page, push notifications,
touching any competitor-finder file.
