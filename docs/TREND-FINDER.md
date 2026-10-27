# Trend Finder

Finds what's working and ranks it by how practical it is for **42 Abu Dhabi** to
actually make. The point is a shortlist of things worth posting — not a wall of
vanity metrics.

Two sources feed one ranked feed:

| category | where it comes from | what "it worked" means |
|---|---|---|
| **Competitors / Sentiment** | Instagram accounts we watch | a post beat **its own account's** median engagement |
| **Industry Signal** | the AI labs, the tech press, UAE outlets | a story was picked up by **more outlets than most** |

Nav: **Trends** (between Ideation and Library). Four tabs: Feed · Viral Social
Trends · Themes · Competitors / Sentiment. The Feed's category strip switches
between the two sources, or shows **All** — possible because both are scored by
the same pass and blended into the same `opportunity_score`.

> Two tabs were renamed on 2026-08-20 — **Competitors → Competitors /
> Sentiment** and **AI News → Industry Signal** — and the display names above
> are the new ones. Nothing underneath moved: the `trend_posts.source` values
> are still `competitor` and `news`, the API filter is still `?source=…`, and
> the sub-tab routes are still `#trends/competitors` and `#trends/general`.

---

## The idea: outperformance, not likes

Likes don't compare across accounts of different sizes. Measured on live data:

| account | followers | typical engagement |
|---|---:|---|
| `@42abudhabi` | 22,130 | ~0.3% of followers |
| `@visitabudhabi` | 1,062,417 | ~0.05% of followers |

A post with 600 likes can be a flop and a post with 60 can be a hit. So every
post is measured against **the median of its own account**:

```
outperformance = post_engagement / that account's median engagement
engagement     = likes + comments      (views excluded — video-only, see below)
```

"**4.7× their average**" is the number the feed leads with. On a real scan of
`@42abudhabi` the top three by this measure were a student competition win, an
Emirati-culture Mother's Day piece and another competition placement — real
editorial signal, which is the whole test of whether the metric works.

Three refinements that matter:

* **Log compression.** A linear cap flattened the top of the feed: `@visitabudhabi`
  produced posts at 44×, 34× and 22× its own median, which all tied at full marks
  and stopped discriminating. Outperformance is compressed logarithmically and
  saturates at 8×, so the busy 1×–5× band stays well spread (1×→32, 2×→50,
  3×→63, 4×→73, 5×→82) while a freak viral post can't run away with the feed.
* **Thin samples are damped and capped.** A median from three posts is not a
  baseline. Ratios are shrunk toward 1.0 in proportion to sample size *and*
  capped, because shrinking alone isn't enough — a "100×" from three posts is
  still 50× after halving. A new account can't rocket to the top on noise; the
  raw ratio is kept in `metadata.raw_outperformance` for transparency.
* **A failed scan never zeroes a competitor's post count.** A rate limit is not
  evidence that an account has no content.

## The AI layer

Engagement says a post worked *for them*. It says nothing about whether 42 — a
tuition-free, peer-to-peer coding school — could or should make something like
it. The scoring pass adds:

* `topic` — the transferable **angle**, not a summary of their post
  (bad: "Emirates cabin crew video"; good: "A day in the life, told by the people who live it")
* `theme` — a reusable bucket, which is what the Themes tab groups on
* `fit_score` 0–100 + `fit_reason` — can 42 realistically make this
* `uae_relevance` 0–100
* `suggested_angle` — how 42 would do it

Scoring is **visual**: the thumbnail goes to the model with the caption, because
captions routinely misread a post. The scan already downloaded those thumbnails,
so it costs nothing extra.

`opportunity_score` is computed **in Python, never by the model**, from three
weights (40 / 40 / 20). They were briefly tunable from sliders in the feed;
those were removed, so the blend is the server's and re-ranking needs no
re-scan or re-score either way.

## Competitors / Sentiment

Adding a handle **validates it against Instagram first**. This matters more than
it sounds: Business Discovery only reaches **Business/Creator** accounts, and
plenty of real brands aren't reachable — verified: `@adnoc`, `@42network` and
`@42lausanne` all fail with *Invalid user id*. The UI explains that in plain
language rather than looking broken.

Two distinct ways to stop watching an account, because they mean different things:

| action | effect |
|---|---|
| **Exclude** (`status='excluded'`) | Row survives. Scans skip it, and **AI research never suggests it again**. Reversible — the Competitors / Sentiment tab shows excluded accounts with an Include button. |
| **Delete** | Gone, and its discovered posts cascade away. The handle can be suggested again later. |

**Research competitors** asks the model for handles worth watching (weighted
toward UAE/GCC), then **verifies every candidate against the real API** before
showing it. Handles that can't be read appear in a "couldn't be read" list with
the reason. "Ignore" on a suggestion writes an excluded row so it never returns.

## Industry Signal

A **source**, not a second trend system:

```
RSS / Atom / sitemap -> normalise -> trend_posts -> the SAME AI pass
                                                 -> the SAME opportunity blend
                                                 -> the SAME ranked feed
```

A story becomes an ordinary `trend_posts` row with `source='news'` and
`competitor_id NULL`, so scoring, ranking, Save/Dismiss and "Make this" all work
on it with no branch downstream.

**Outperformance** has to mean something here too, since the feed ranks on it.
A story's ratio is how many outlets ran it against the **median coverage for the
window** — "picked up more widely than the other things this week", which is the
same question the competitor metric asks. Thin windows are damped and capped by
the same `_trusted_outperformance` the Instagram path uses, so a two-story
window can't manufacture a "50×". Coverage comes free from deduplication:
near-duplicate headlines cluster, and only the first-seen member stays visible.

**Region is never NULL**, because the server's `?region=` filter excludes NULLs
and would make the UAE toggle look broken. Every story is classified by its own
text, which is what catches an OpenAI story that happens to be about Abu Dhabi.

Collection is unattended — each source carries its own `next_fetch_at` and
`workers/news_poller.py` reads it. Nothing auto-generates: a human picks the
story and presses the button.

**Full detail, including the source catalogue, the traps in it, and every
measurement taken of the scorer: [AI-NEWS.md](AI-NEWS.md).**

## Architecture

```
backend/app/services/trend_store.py     3 self-creating tables + all DB access
backend/app/services/trend_finder.py    the scan engine (Instagram -> posts)
backend/app/services/trend_scoring.py   the AI pass + the ranking maths
backend/app/services/trend_research.py  AI competitor discovery + validation
backend/app/services/news_search.py     the two model calls + the code filters
backend/app/services/news_ingest.py     found story -> trend_posts row
backend/app/services/news_store.py      news_config (the brief) + news_runs (the log)
backend/app/routers/trend_finder.py     /api/trends/*  (24 routes)
backend/app/workers/trend_scan_poller.py  runs queued competitor scans
backend/app/workers/news_poller.py      collects Industry Signal, unattended
frontend/src/pages/TrendFinder.tsx      the page shell
frontend/src/components/trends/*        Feed · Themes · Competitors / Sentiment
```

The two collectors are deliberately **not** on one queue. `trend_scans` is
serialised so competitor scans cannot collide on the Instagram rate limit;
putting an unattended news collector behind it would mean news silently stops
arriving whenever someone is scanning competitors. They share only the scoring
pass, and each scopes `score_pending` to its own source so a tick cannot pay for
the other's batch.

Tables (`trend_competitors`, `trend_posts`, `trend_scans`) self-create via
`CREATE TABLE IF NOT EXISTS` on first use — **no migration step**. To create them
ahead of time: `backend/.venv/bin/python -m app.services.trend_store`.

**Scans are a DB queue, not a background task.** `POST /scan` writes a `queued`
row and returns; `trend_scan_poller` picks it up. A scan takes minutes (~3s per
50 posts, ~12s per account), and an `asyncio.create_task` would neither survive a
restart nor be safe from garbage collection. A scan interrupted by a redeploy
resumes; one orphaned for 30 minutes is closed out so the UI never polls forever.

### Security note

`instagram_client` interpolates the handle **straight into the Graph API field
DSL** (`business_discovery.username(X)`) with no escaping. `trend_store.validate_username`
is therefore a security boundary, not a tidy-up: it enforces
`^(?=.*[A-Za-z0-9])[A-Za-z0-9._]{1,30}$`. The positive lookahead is load-bearing —
without it `../etc` normalises to `..`, which is all dots and would otherwise pass.

## What Instagram does and doesn't give us

Available for third-party accounts via Business Discovery:
`like_count`, `comments_count`, `view_count`, `caption`, `media_type`,
`permalink`, `timestamp`, plus account-level `followers_count` / `media_count`.

**`view_count` is VIDEO-only** (measured: exactly 74 of 150 posts — precisely the
74 videos). The UI shows "—" for images and carousels; it never invents a zero.

**Not available at all** without the owner's token: reach, impressions, plays,
saved, shares, story metrics. Any ranking built on those is impossible here.

CDN URLs expire, so thumbnails are downloaded at scan time and served from
`GET /posts/{id}/thumb`, with the CDN URL as fallback.

## Degraded modes

The feature is built so each layer can fail without taking the rest down:

| what's down | what happens |
|---|---|
| Instagram not configured | Endpoints return a plain-language 422; the page shows a setup banner. No 500s. |
| A single handle unreachable | Recorded on that competitor with the business-account explanation; the scan continues. |
| AI scoring unavailable (e.g. Vertex billing off) | The scan still completes as `done`, posts are stored, and the feed **still ranks on outperformance**. The scan message says so and tells you to hit Re-score once it's fixed. Verified live: 33 posts stored and ranked with 23 distinct scores while Vertex returned 403. |
| The Industry Signal search fails | Recorded on a `news_runs` row with a readable reason, shown amber in the status line, and rescheduled for the next morning. The feed keeps yesterday's stories rather than emptying. |
| The search answers without searching | Detected by an empty grounding source list and the whole reply is discarded, rather than putting remembered stories in a feed that promises found ones. |
| Nothing worth posting about | Recorded as `empty` and said plainly on screen. This is the feature working: it refuses to pad a quiet morning, so this must never read as a failure. |
| A single trend row fails to store | Logged and skipped; the rest of the batch is still stored and the scan completes. |

## Verification status

Verified on this machine, against **live data**:

* Instagram Business Discovery works — `@42abudhabi` (22,130 followers) and
  `@visitabudhabi` (1,062,417) resolve; paging, timestamps and the video-only
  `view_count` rule all confirmed.
* Full DB round trip: tables created, competitor added, real scan stored 33
  posts, feed ranked, exclude honoured, delete cascaded 33 → 0.
* Unreachable-account and AI-unavailable paths both exercised end to end.
* **2,039 backend tests pass** (82 of them cover Industry Signal).
* **AI scoring works.** Vertex billing is now enabled and the pass has been run
  against real data: 111 rows scored, and the scorer itself measured across
  three repeat runs. The numbers, including what they say about how far the
  score can be trusted, are in [AI-NEWS.md](AI-NEWS.md#scoring-and-what-is-actually-known-about-it).
* **Industry Signal verified live end to end** on 2026-09-05: one grounded
  search returned 7 candidates, the grounding cross-check dropped 2 ungrounded
  links, the judge kept 4, and every headline came back in plain English
  ("OpenAI Launches GPT-6 Astra After A Curious False Start" arrived as "OpenAI
  releases a new AI that can find security flaws"). Full round trip against a
  real Postgres: migration, run row, stories stored with scores, feed query,
  and a repeat run correctly storing 0 new.

**Not verified from the deploy host.** Every source check ran from a residential
IP. Cloudflare-fronted hosts treat datacentre addresses harder, so re-run the
sweep there before trusting the catalogue in production — that is the most
likely "works locally, empty in production" failure.

**Not verified: the news collector against a live database.** The ingest,
clustering, region-tagging and scheduling logic are covered by pure-function
tests, and the fetch path has been exercised against the real feeds, but no
story has yet been written to Postgres on this machine. See the verification
commands in [AI-NEWS.md](AI-NEWS.md#verifying-a-deployment).

## First run

1. Relaunch the backend (new router + poller) and the frontend.
2. Open **Trends → Competitors / Sentiment**, add a handle (try `visitabudhabi`) or press
   **Research competitors**.
3. **Scan all**, and watch the progress strip.
4. Open the **Feed**. Without billing you'll see engagement-ranked results;
   with it, fit scores and suggested angles fill in too.

### First run — Industry Signal

There is nothing to configure. No API keys, no accounts, no cost: every source
is a public feed.

1. Relaunch the backend. `news_poller` registers the 17 sources on boot and the
   first collection starts within a minute — look for `news_poller started`.
2. Open **Trends → Industry Signal**. The status line shows when it last ran and when it
   runs next; **Fetch now** skips the wait.
3. Stories arrive ranked newest-first, with the technical ones already hidden
   behind the fit bar. The count line says how many are hidden and lifts the bar
   when clicked.
4. **Make this** on a story offers four tools rather than eight — poster,
   poster + human, Human Element Video, animation.
