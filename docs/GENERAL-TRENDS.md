# Viral Social Trends

> ## ⏸ PAUSED since 2026-08-25 — read this before anything below
>
> The whole stream is switched off. Not collecting, not shown, not in the report.
> Everything below still describes the code accurately — it is paused, not removed —
> but none of it is *running*.
>
> **Why.** The rebuilt feed still fails the owner's own test: too many cards are things
> that HAPPENED rather than formats people are RECREATING (the section below defines the
> difference). That is a research problem in source selection and clustering, not a bug
> with a patch, so the surface is hidden until the research is done rather than left on
> screen teaching people to distrust the tab.
>
> **What is off.**
> - The poller never starts, so nothing auto-refreshes and no queued run is claimed.
> - `POST /api/trends/general/refresh` answers **423**, so a manual refresh cannot
>   collect either. Runs already queued stay `pending` and replay when it comes back.
> - The Trends page has no Viral Social Trends tab and the Feed has no viral channel.
> - The social-listening report is a **two-stream** document: no `scope=viral`, no
>   *Emerging Trends* section, no `kind=trend` item report, no viral count in the basis.
>   The report follows the collector on purpose — with nothing being collected, a viral
>   number could only be a frozen snapshot ageing out of the window.
>
> **What is untouched.** Every collector, parser, scorer, ranker, table and stored row.
> The three-stream report still works and is still fully tested
> (`tests/test_social_report.py` turns it back on for its own duration).
>
> **Turning it back on — both halves, or neither:**
> 1. `GENERAL_TRENDS_ENABLED=true` in `backend/.env`, then restart the backend.
> 2. `VIRAL_SOCIAL_TRENDS_ENABLED = true` in
>    `frontend/src/components/trends/featureFlags.ts`, then rebuild the frontend.
>
> A static frontend build cannot read the backend's config, which is why the switch has
> two halves. Half-on is the one broken state: a tab reading from a stopped collector, or
> a collector filling a UI nobody can reach. The paused behaviour is pinned by
> `backend/tests/test_general_trends_paused.py`, including a revert test that fails if
> re-enabling ever stops being a config change.

The second kind of trend in the **Trends** page. The competitor finder (Feed / Themes /
Competitors / Sentiment) answers "what worked for accounts like ours on Instagram";
**Viral Social Trends** answers "what format is the internet making right now, and
could we make one too" — and ranks each one by how well **42 Abu Dhabi** could ride
it. Same card grammar, same **Make this -> Ideation** hand-off, none of the
competitor code touched.

> Display name only. The tab was called **General trends** in the UI until
> 2026-08-20 and the file keeps its old name. Every identifier is unchanged:
> the sub-tab key is still `general`, the route is still `#trends/general`, and
> the API is still `/api/trends/general`. Rename labels, never keys.

Nav: **Trends -> Viral Social Trends** (`#trends/general`). The original design spec is at
`docs/superpowers/specs/2026-08-19-general-trends-design.md`; it is a HISTORICAL record and
several of its decisions have since been overturned by measurement — see the notes below.

---

## What counts as a trend

**A trend is a format people are RECREATING.** Many different people are making their own
version of the same thing, and a brand can join in by making one too.

- The **penguin** walking away from its colony toward the mountains became a trend when
  thousands of people restaged it for their own situation — a UAE shop posted the penguin
  walking to THEIR store. The trend is the reusable joke, not the penguin.
- Couples proposing at the top of a skyscraper holding a handwritten **notice** started as a
  news moment and BECAME a trend once other people put their own message on the notice. A news
  moment is a trend only once people are remaking it.
- An **election**, an **earthquake**, a **product launch**, a **football result**, a page lots
  of people **searched**: these are things that HAPPENED. Nobody can make their own version.
  They are not trends, however big they are.

The whole pipeline is built around that one distinction, in three places:

1. **Minting is an allowlist.** Only a registry of things people are MAKING may create a card
   — `tiktok`, `kym`, `instagram`. Every other source can attach to a card and add evidence,
   but can never create one. A source added later is non-minting until someone deliberately
   decides otherwise, and `NON_MINTING_SOURCES` is derived as the complement so the two sets
   cannot drift apart.
2. **The participation gate.** The feed carries only `meme`, `format`, `sound` and `challenge`
   (`GENERAL_TRENDS_KINDS`). News, events, products and culture are held back; "Show news too"
   reveals them. A row the model has not judged yet is treated as *pending*, not as news.
3. **The scorer is told the definition**, with both examples above, and must answer
   `replication` — "who is making their own version, and what one looks like" — BEFORE it
   picks `trend_kind`. Structured output is autoregressive, so the kind follows that
   description instead of being rationalised after it. The answer is kept in
   `metadata.replication` so a kind you disagree with can be audited.

### Why this was needed

The first build treated any spike as a trend, and the numbers were unambiguous. Of 363 stored
trends only **103** were a participatory kind — the rest were news 126, culture 70, event 40,
other 19, product 5. A full live fetch showed the same shape upstream: **328 items kept, only
28 from a format registry**, against google_news 80, reddit 45, lemmy 40, wikipedia 40,
github 30, hacker_news 28, bluesky 25, google_trends 12. Roughly three quarters of everything
the team saw was something nobody could act on.

---

## The idea: one card per trend, heat x fit

Free public sources are pulled on a schedule, every item is filed under a **trend** (existing
or new), and each trend gets:

- **Heat** (`velocity`, 0-1, recomputed every run) — amplitude against the source's own median,
  growth, and freshness from CONTENT time, multiplied by a corroboration trust term. Bands:
  >=0.7 **hot**, >=0.45 **rising**, else **warm**. A single uncorroborated item that is not
  outstanding on its own feed is capped below the "rising" band, so it can never carry a hot
  pill.
- **Fit for 42 / UAE relevance** (0-100) from the AI layer below, on an anchored 0/20/40/60/
  80/100 scale.
- **Opportunity** — a weighted GEOMETRIC mean of heat, fit and UAE relevance, so no single
  axis can buy the ranking. Weights are fixed at 0.4 / 0.4 / 0.2 on the server; the
  sliders that used to re-tune them were removed from the feed, so the blend is the
  server's and only the server's. Unscored trends rank on heat alone.

## Sources

A source either **mints** cards (its items can create a trend) or only **corroborates** them
(its items attach to a trend some other source minted, adding heat and evidence). The
distinction exists because a single Reddit post is not "what the internet is doing": in the
first live run 47% of the feed was individual `/rising` posts, and **0 of 167 of them survived
even 80 minutes**.

| id | mints? | what | how | notes |
|---|---|---|---|---|
| `kym` | yes | Know Your Meme newsfeed | `knowyourmeme.com/newsfeed.rss` | best signal-to-noise in the system, and the only source shipping a trend's NAME *and* an explanation of it. Also how TikTok trends reach us by name |
| `google_trends` | yes | daily search spikes per country | RSS `trends.google.com/trending/rss?geo=...` | traffic "2,000+", picture, news links |
| `bluesky` | yes | trending topics | `public.api.bsky.app/xrpc/app.bsky.unspecced.getTrends` | postCount, category, startedAt |
| `tiktok` | yes | Creative Center trending hashtags | `ads.tiktok.com/creative/creativeCenter/trends/hashtag?region=..&period=7|30` | **`Accept: text/html` is load-bearing** - the default wildcard Accept returns HTTP 200 with the data silently absent, which is why this was long believed to be login-gated. Ships `popularityCurve`, a daily series that gives the ranker real growth on run #1 |
| `hacker_news` | yes | front page | `hn.algolia.com/api/v1/search?tags=front_page` | `points` + `num_comments` - a real engagement number, dead centre of 42's lane |
| `lemmy` | yes | top of day | `lemmy.world/api/v3/post/list?sort=TopDay` | the Reddit substitute **with** the score and comment count Reddit refuses to give (v3 only; v4 is 404) |
| `github` | yes | newest fast-starring repos | `api.github.com/search/repositories?q=created:>{date}&sort=stars` | `created:>` enforces recency **server-side** |
| `google_news` | yes | Gulf + tech headlines | `news.google.com/rss/search?q=...when:7d&...&gl=AE` | replaced `x_trends`: real English Gulf signal with headlines, publishers and dates. `when:7d` is server-side recency |
| `youtube` | yes | most-popular per region | Data API `videos?chart=mostPopular` | **only when `YOUTUBE_API_KEY` is set** (free, 10k units/day; this uses 8). Silently absent otherwise |
| `instagram` | yes | hashtag + co-hashtag mining | `ig_hashtag_search` then `{id}/recent_media` | needs a live Graph token **and** `instagram_manage_insights`. `recent_media` is capped server-side to the last 24h. One item per hashtag, never per post |
| `reddit` | no | top of day | `reddit.com/r/{sub}/top.rss?t=day` (`.json` is 403 on `www` and `old` alike) | **corroboration only.** `/rising` is pre-consensus by construction. Anonymous RSS is ~1 request/60s per IP, so the fetcher paces itself off `x-ratelimit-remaining/reset` and runs subs in one lane. No engagement number exists on any anonymous endpoint, so feed *position* is the only magnitude signal |
| `wikipedia` | no | most-viewed articles | Wikimedia pageviews `top/.../{D-1}` vs `{D-2}` | **corroboration only**, and now a spike detector rather than a top-list mirror: an article must clear `wiki_is_rising` (3x for an already-popular page, 1.5x otherwise, and +20k absolute). That alone drops "ChatGPT" (1.71x), "Erection" and "Ejaculation" |
| `instagram_watch` | no | posts from trend-setting accounts | `business_discovery` per handle | **corroboration only** - one post is not a trend. Ranked against each account's own median |
| `x_trends` | off | X/Twitter via trends24.in | - | **off by default.** Contributed 80 cards and zero wins: 240/240 sightings had an empty summary and none a usable count, so the scorer invented English meanings for bare Arabic hashtags. Code retained; re-enable via `GENERAL_TRENDS_SOURCES` |

Regions come from `GENERAL_TRENDS_GEOS` (default `US,AE`); the rest are `global`. A failing
source never fails the run - it is recorded in the run's `metadata.sources` and shown red in
the sources strip.

## The ingest screen: what never gets in

Three deterministic gates run **before** embedding or scoring, so junk costs no tokens. Every
drop is counted in `metadata.dropped_by` and surfaced per source on `/sources`.

1. **Safety** (`general_trends_safety.py`) - four tiers. `EXPLICIT` and `HARM` **drop** and
   match title + summary + subreddit + url; `BAIT` (dating / thirst / relationship) and
   `AVOID_TOPIC` **flag** and match the **title only**. Title-only is measured, not timid: a
   naive relationship word list matched 15 live items and 11 were false positives, every one
   of them from matching the summary. Word and phrase alternation with boundaries, plus two
   normalisations, so `p0rn` and `p.o.r.n` are caught while `top ornament`, `Essex` and `gfx`
   are not. Bare `sex`, `hot`, `date`, `wife` are excluded **by design** - they are the
   false-positive engine. `GENERAL_TRENDS_ALLOW_TERMS` kills a false positive without a
   deploy; `GENERAL_TRENDS_SAFETY=on|flag_only|off`, and an unknown value fails **closed**.
2. **Freshness** (`general_trends_freshness.py`) - one global age rule would be wrong, because
   three groups of sources mean three different things by their timestamp. `AGE_FROM_ITEM` is
   real content age, so it is gated. `AGE_FROM_WINDOW` (x_trends, wikipedia, tiktok) has a
   timestamp that is a property of the feed window rather than the content - both measured as
   constants - so there is nothing to gate. `AGE_FROM_TREND` (bluesky `startedAt`) is the
   trend's *birth*: a 435h-old trend can still be legitimately trending, so only the 30-day
   ceiling applies. A **missing** date keeps and logs, never drops: every source populates it
   today, so a sudden `None` means a parser regression, and dropping would turn that into a
   silent source outage.
3. **Shape** (`general_trends_sources.screen_shape`) - sports fixtures (`X vs Y`, `X ضد Y`;
   the separator must be a whole word, so `Spider-Man` and `Versace` survive) and bare tokens
   (at most 2 words, no summary, no metric - unidentifiable, so the scorer invents a meaning
   for them).

## How we tell "people are making it" from "people are watching it"

The definition is only useful if something measures it. These are the signals that do, all
verified on live data.

**views-per-post is the cheapest and the strongest.** A TikTok hashtag row carries both a post
count and a view count, and the ratio separates the two cases better than anything else:

| hashtag | posts | views/post | what it is |
|---|---|---|---|
| `#marvelrivalssummerfest2` | 179,946 | **196** | everyone is MAKING one |
| `#tiktokshopbacktoschool` | 302,482 | **393** | a format |
| `#rushready` | 3,401,397 | **524** | a format |
| `#haydenpanettiere` | 36,890 | **10,725** | a celebrity died — everyone is WATCHING |
| `#lindsayclancy` | 77,989 | **22,936** | a crime story |

A 55x separation between the extremes, on rows that look identical if you rank on views. It
costs nothing — both operands are already in the row.

It is a **clue, not a verdict**: `#tiktokcookbook` measured 6,937 views/post and is a real
format. So the number is written into the item summary and explained to the scorer, and is
never used as a hard gate.

Other signals, in rough order of usefulness:

- **`topCreators[].followedCnt`** — three mega-accounts carrying a tag is a campaign; small
  accounts carrying it is a grassroots format. AE's `#happyindependenceday` is led by a
  2.2M-follower account.
- **Know Your Meme's `Type:` taxonomy** — the definition in the source's own vocabulary:
  `participatory-media`, `exploitable`, `snowclone`, `image-macro`, `dance`, `catchphrase`,
  `remix`, `reaction`, `copypasta` are formats; `death`, `film`, `character`, `viral-debate`,
  `viral-video`, `influencer` are not. It sets `trend_kind` from the source instead of paying
  a model to guess.
- **A sound's usage** — using a sound IS recreating the format, so a Shorts-usage chart or a
  sound's video count is participation by construction.

### The recency trap, and why the obvious fix is wrong

Know Your Meme's `Year` field is the format's **origin**, not its current **wave**. The
penguin entry — the owner's own example of a real trend — is `Year 2008`. A "Year must be
recent" filter would delete exactly the thing the feature exists to find.

So freshness for an encyclopedia-style source is a three-way judgement, not a date comparison:

| shape | verdict |
|---|---|
| fresh feed + recent Year | a new format |
| fresh feed + old Year, with real numbers (penguin: 71,594 views, 5 videos, 17 photos) | a **revival** — keep, but expect corroboration |
| fresh `lastmod` only, old Year, thin numbers (Mrs. Puff: 135 views, 0 videos) | someone edited an encyclopedia article — drop |
| `Status: Deadpool` | KYM's own editors rejected it as not a real meme — drop |

## Dead ends — measured, so nobody probes them again

- **Tenor** — API discontinued January 2026. v1 returns an explicit "discontinued" error, v2 is
  403 unregistered, and Google is not accepting new clients. There is no keyless path.
- **Giphy** — the well-known public beta key is BANNED, not expired. A free key is self-serve,
  but items carry no participation count: corroboration only.
- **Reddit score / comment counts** — do not exist on any anonymous endpoint. Not in the Atom
  body, and `.json` is 403 on `www` and `old` alike. Feed POSITION is the only magnitude
  signal Reddit will give us.
- **Instagram hashtag counts** — the Hashtag node exposes exactly two fields, `id` and `name`.
  There is no post count, ever. **Reels audio has no API at all** (`media_audio_type` on your
  own media is the only audio field that exists). The one real win is paginating
  `recent_media`, which is server-capped to the last 24 hours, and counting.
- **TikTok Creative Center song / creator / keyword tabs** — they do not exist. The route
  manifest carries ten routes, all hashtag or video, and the other tabs return an empty shell.
  Hashtags are capped at **three rows per region per period**, which is why region breadth is
  the only volume lever we have.
- **Also dead:** Pinterest Trends (needs an approved OAuth business app), Snapchat trends,
  Imgur, tokboard, CapCut's API, the YouTube Data API for formats, and `FEtrending`
  (400 INVALID_ARGUMENT).

## Make this: look it up, then recommend

A trend's name is a **label for a thing, not a description of it**, and reading
it literally is how this feature produced its worst output.

The owner pressed "Make this" on `#spidermanbrandnewday` and got three ideas
about *"a fresh start"*: **Leaving my old life behind**, **My 'brand new day' is
every day**, **My first day with zero experience**. `#spidermanbrandnewday` is
the title of the 2026 film *Spider-Man: Brand New Day*. The words "brand new
day" are a title. Not one of the three ideas mentioned Spider-Man.

It was not a careless model — it was a model with nothing to go on. A TikTok
hashtag arrives as a name and a row of numbers:

    TikTok hashtag #spidermanbrandnewday — 24,297 posts, 57,160,324 views
    in the last 30 days in AE. 2,353 views per post. Top creators: @mariiamm66…

Nothing in there says "film". So the model guessed, and it hedged while doing
it: **8 of 18 hashtag trends** in the live corpus had a stored summary
containing *likely*, *suggests*, *appears to be* or *probably*. On this one it
guessed "a famous Spider-Man comic storyline" — the wrong wave, since the
current one is the movie.

**So "Make this" now searches the web before it writes anything.** One
Google-grounded call per trend asks what the name actually refers to right now,
who is posting under it and why. On the same trend that returns: *"the recently
released MCU movie Spider-Man: Brand New Day … its plot is inspired by a 2008
comic book storyline of the same name"*, with its sources.

The answer is cached on `general_trends.metadata.research` (via
`general_trends_store.set_metadata`, a one-column jsonb merge — `save_score`
cannot be reused for this because its SET list would blank the trend's whole AI
verdict). Pressing the button twice costs one lookup, and the answer survives a
restart.

Two rules turn the lookup into better ideas:

- **NAME_IS_A_LABEL_RULE** — never read the name literally; build the idea
  around what the thing actually is. The Spider-Man case is written into the
  prompt as the worked example, and an idea about "fresh starts" is named as a
  wrong answer.
- **CONNECT_TO_42_RULE** — the subject has to meet the school. *Spider-Man at
  42*, not a generic post about new beginnings. An idea that would still make
  sense with the subject deleted has failed. And 42 is a coding school: never a
  games studio, a sponsor or a partner of whatever the trend is about.

The research block outranks everything else in the prompt, and when it is
present the scorer's own notes are explicitly demoted to *"written from the name
with NO search, so treat every line as a guess that the block above may have
just disproved"*.

The same three ideas, after:

> **Spider-Man would be fine here** — *A student in a red hoodie sits alone at a
> computer in a busy lab, looking lost. The shot widens to show everyone else
> working in groups. On-screen text: "In the new Spider-Man movie, the whole
> world forgets him. At our coding school, you're never on your own."*
>
> **How Spider-Man finds his people** — *A student in a Spider-Man mask
> struggles with their code. Another leans over and points at the screen, then a
> third brings them both a coffee. "The world forgot Peter Parker. At 42, you
> find your people on day one."*
>
> **My Spider-Man villain origin story** — *A student stares at their screen in
> disbelief, then turns to their phone camera, deadpan: "Peter Parker's life got
> erased by a magic spell. My project just failed because of one comma. I get
> it."*

Degrades on every failure path: a search outage, a timeout (60s cap), a budget
cap or an unparseable reply all fall back to writing the ideas without the
lookup. `GENERAL_TRENDS_RESEARCH=false` turns it off entirely.

**Grounding and structured JSON cannot be combined** — Gemini rejects a
`response_schema` when a search tool is attached — which is why this is two
calls: a grounded plain-text lookup, then the structured ideas call that reads
it. `gemini_text(search=True, json_schema=…)` raises rather than degrading
silently.

**Still a guess: the card's own blurb.** The lookup runs when you press "Make
this", so the *ideas* are researched but the card's "What it is" is still
whatever the scorer inferred at scoring time. Wiring the lookup into the scoring
pass for evidence-thin trends is the obvious follow-up; it is not done, because
it would add a grounded call per thin trend to every refresh.

## Clustering: how items become one card

Ladder, in order, per item (`general_trends_pipeline.cluster_items`):

1. same source saw the same thing recently (`source_key`)
2. exact normalised title (`slug`: lowercase, `#/@` stripped, NFKC, punctuation dropped,
   leading article dropped), now windowed to 30 days so a generic slug like `supreme court`
   cannot become a permanent magnet
3. **canonical name** from the cheap labelling pass - an exact match on a name the model
   agreed on, so run N+1 matches run N with no model call and the Arabic and English names
   for one story land on one card
4. filed earlier in this run (slug map)
5. **name match against recent trends** - token-bounded containment, or 2+ shared significant
   tokens with Jaccard >= 0.5. Repaired: the stopword list used to strip the *content* words
   (`controversy`, `reveal`) and keep the *template* words, so twelve "What's going on with X"
   posts chained into one mega-card. The templates are now the stopwords, a contained name
   must start in the first 40% of the longer title (a passing mention is not the topic), and
   the best candidate wins rather than the first one encountered
6. new trend - **unless the source only corroborates** (see the table above)

**There is no cosine merge threshold any more.** Over all 65,703 pairs of the 363 stored
vectors, *zero* sat within the old 0.12 cut; the closest pair in the entire corpus was 0.1230,
and that closest pair was **false** (`juju smith` ~ `najee harris`). The true and false
distributions are interleaved rather than merely overlapping, so no threshold exists. The
vectors survive as a candidate *hint* (`nearest_trends(max_distance=0.20)`) feeding the name
rung.

If embeddings are unavailable (Vertex 403 / no project) the run still finishes on rungs
1–4 and records `metadata.embeddings = "unavailable"`.

## The AI layer

Unscored trends are scored in batches of 8 with `gemini_text` (default model, JSON schema
with **every field required** — Gemini drops optional ones): `clean_title`, `topic`
(transferable angle), `topic_summary` (what it is), `theme`, `trend_kind`
(meme/format/sound/challenge/news/event/product/culture/other), `sensitivity`
(ok/caution/**avoid** — deaths, tragedies, politics, religion, sexual, drugs), `fit_score`
+ `fit_reason`, `uae_relevance`, `suggested_angle` (how 42 rides it), `suggested_format`,
`effort`, `tags`. `avoid` trends are hidden unless **Show sensitive** is on; `caution`
shows a chip. Vertex billing/403 ⇒ the run still ends `done` with a caveat and cards show
heat only; **Re-score all** later.

**Make this** → `POST /api/trends/general/items/{id}/ideas {tool}` →
`general_trend_ideation.generate_prefill` builds a "THE TREND" prompt (evidence lines +
scorer output) and reuses `trend_ideation.schema_for / parse_prefill`, so the frontend
receives the identical `TrendPrefill` envelope (`post_id` = trend id) and
`sendProducerBrief` opens the chosen creator pre-filled — verified live (KYM "Dream Girl
Wallahi" → Poster brief "Dream Code, Wallahi").

## Architecture

```
backend/app/services/general_trends_sources.py    parsers (pure) + fetchers + build_fetch_plan/fetch_all
backend/app/services/general_trends_store.py      3 self-creating tables + all DB access
backend/app/services/general_trends_ranking.py    velocity / weights / opportunity / sorts (pure)
backend/app/services/general_trends_pipeline.py   run_refresh: fetch → cluster → velocity → score
backend/app/services/general_trend_ideation.py    Make this prefill for a trend
backend/app/routers/general_trends.py             /api/trends/general/* (10 routes)
backend/app/workers/general_trends_poller.py      DB-queue poller (30s) + auto refresh every N hours
frontend/src/components/trends/GeneralTrendsTab.tsx / GeneralTrendCard.tsx / GeneralMakeThisDialog.tsx / generalTrendsApi.ts
```

Tables (`CREATE TABLE IF NOT EXISTS`, no migration step; pre-create with
`backend/.venv/Scripts/python.exe -m app.services.general_trends_store`):
`general_trend_runs` (queue + per-source stats + heartbeat), `general_trends` (one row per
trend: aggregates, `embedding vector(768)`, AI fields, status), `general_trend_sightings`
(one row per source item per run — the evidence and the velocity history).

**Refreshes are a DB queue, not a background task.** `POST /refresh` inserts a `queued`
run; the poller claims it atomically and runs it; **runs also queue themselves** every
`GENERAL_TRENDS_REFRESH_HOURS` (default 6, `0` = manual only). Progress writes double as a
heartbeat, so a long healthy run isn't declared stale (30 min without heartbeat ⇒ failed).
Manual E2E: `PYTHONUTF8=1 ./.venv/Scripts/python.exe -m app.services.general_trends_pipeline`
(claims its own run so a running poller can't double-execute it).

Routes: `GET /stats`, `GET /sources`, `POST /refresh` (409 while one runs),
`GET /runs/latest`, `GET /items` (sort/status/region/source/theme/kind/min_fit/q/
include_sensitive/**include_filtered**/**days**/**content_days**/limit/offset/w_vel/w_fit/
w_uae -> `{items, total, weights}`; each item carries `evidence[]` and `heat`),
`PATCH /items/{id}` `{status}`, `GET /items/{id}/thumb`, `POST /items/rescore`,
`POST /items/{id}/ideas`, `GET /themes`.

`days` is the **last-seen** window (default `GENERAL_TRENDS_FEED_DAYS`, 7) and `content_days`
is the **content-age** ceiling (default 30, the owner's "nothing older than a month"). They do
disjoint jobs: `last_seen_at` answers "does a source still list this", `latest_content_at`
answers "is anything new actually happening". `first_seen_at` deliberately gates nothing - a
trend first seen three weeks ago but still collecting fresh sightings is exactly what we want
to keep.

Config (`backend/.env`), all documented in `backend/.env.example`:

| setting | default | what it does |
|---|---|---|
| `GENERAL_TRENDS_SOURCES` | the minting + corroborating set above | which sources run. `x_trends` is deliberately absent |
| `GENERAL_TRENDS_GEOS` | `US,AE` | Google Trends geo, YouTube region, Google News edition |
| `GENERAL_TRENDS_SUBREDDITS` | `memes,ProgrammerHumor,Damnthatsinteresting,technology` | cut from 7: `videos` was 19/24 `avoid`, `OutOfTheLoop` 68% junk, `popular` pet photos, `TikTokCringe` ragebait with no NSFW flag to catch it |
| `GENERAL_TRENDS_SOURCE_CAPS` | `reddit:15,kym:40` | per-source item caps. Reddit's budget used to be 7x40=280 while KYM - the best source - was capped at 8 |
| `GENERAL_TRENDS_FEED_DAYS` | `7` | default `days` |
| `GENERAL_TRENDS_MAX_CONTENT_AGE_DAYS` | `30` | default `content_days` |
| `GENERAL_TRENDS_SAFETY` | `on` | `on` / `flag_only` / `off`; an unknown value fails closed |
| `GENERAL_TRENDS_ALLOW_TERMS` | empty | suppress a safety false positive without a deploy |
| `GENERAL_TRENDS_DROP_UNDATED` | `false` | drop items whose date is missing (off: a missing date is a parser regression, not stale content) |
| `GENERAL_TRENDS_TIKTOK_REGIONS` | `AE,SA,US,GB` | Creative Center regions; validated against the 27-code allow-list |
| `GENERAL_TRENDS_IG_HASHTAGS` | empty | leave empty until `instagram_manage_insights` is granted |
| `GENERAL_TRENDS_IG_WATCH_ACCOUNTS` | empty | trend-setting handles; needs no new scopes |
| `GENERAL_TRENDS_TRIAGE_MODEL` | empty | model for the cheap labelling pass |
| `GENERAL_TRENDS_REFRESH_HOURS`, `GENERAL_TRENDS_MAX_ITEMS_PER_SOURCE`, `GENERAL_TRENDS_MODEL`, `YOUTUBE_API_KEY` | | unchanged |

## Degraded modes

| what's down | what happens |
|---|---|
| a source 4xx/5xx/timeout | recorded per source in the run, red chip in the sources strip, run continues |
| Reddit 429 | fetcher waits for the window reset (headers), retries; whole-sub failure is recorded, not fatal |
| Vertex embeddings | exact/name clustering only, `metadata.embeddings="unavailable"` |
| Vertex scoring (billing/403) | run `done` with caveat, cards show heat only, Re-score later |
| `YOUTUBE_API_KEY` missing | youtube disabled, silently skipped |
| refresh already running | 409, button disabled |

## Verification status (2026-08-20, first real end-to-end run)

Run #8, local Docker stack, live sources and real Vertex. **551 items from 14 sources ->
162 new trends -> 123 scored.** Fetch ~6 min (Reddit's 1-req/60s lane is almost all of it),
clustering and scoring a few minutes more.

What the run proved that reading the code could not:

- **The minting allowlist holds.** The only sources that created a card were `youtube_shorts`
  (103), `tiktok` (31) and `kym_entries` (28). Zero from reddit, google_news, wikipedia,
  bluesky, github, hacker_news, lemmy or google_trends — all of which used to mint freely.
- **Clustering merges across regions.** 149 Shorts items -> 103 trends (the same song charting
  in AE, SA and US), 48 TikTok hashtags -> 31 (the same tag across 8 regions).
- **`fit_score` is no longer bimodal.** Across the top of the feed:
  90 / 85 / 65 / 65 / 60 / 50 / 80 / 85 / 80 / 75 / 55, against the old 0-or-95.
- **Both content layers work, and they catch different things.** The deterministic screen
  caught `Dream Girl Wallahi` (bait) and a celebrity death (avoid_topic); the model's
  `sensitivity` verdict caught what no word list would — a sexually explicit funk track, a
  crime story, an earthquake. Everything hidden by default, revealed by "Show hidden".
- **The views-per-post signal reached the model and it used it unprompted**, writing:
  *"The tag has generated a very high number of posts with a low average view count,
  indicating mass participation from players."*
- **The Instagram lane degraded exactly as designed** — one circuit-breaker log line carrying
  the token-refresh recipe, the source recorded not-ok, the run finished `done`.

Two things the run exposed and that are now fixed: `youtube_shorts` at 50 rows x 3 regions
minted 103 of 167 cards and the feed became a song list (its cap is now 15); and the card's
title-as-visual fallback only fired for titles starting with `#`, which the scorer renames
away, so imageless cards showed a grey letter.

**Vertex works on this dev machine** (text + embeddings, verified) — earlier notes in this repo
saying otherwise are stale. That is why the local run could exercise scoring at all.

Still unverified: Instagram (token expired, so `general_trends_instagram.py` has never run
against Graph), and the VPS, whose datacenter IP may be fingerprinted differently by TikTok
and Reddit than this residential AE connection.

## First run

1. Relaunch the backend (tables self-create; the poller queues a refresh within 30 s if
   none has run) and the frontend.
2. Trends → **Viral Social Trends** → the header shows *Refreshing now…* with the sources
   count; first run is ~10–30 min (Reddit pacing + scoring of every new trend). Later runs
   only score what's new.
3. Filter, tune weights, **Make this** on a card → the creator opens pre-filled.
