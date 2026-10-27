# Industry Signal

AI news that 42 Abu Dhabi could actually post about, found by one grounded
search and written in plain English. Nothing runs on a timer: a person presses
**Find stories** on the feed and says how many they want at most.

Naming note: the tab reads **SMAA intelligence** and the category reads
**Industry Signal**. Every identifier is still `news`: `trend_posts.source='news'`,
`?source=news`, `/api/trends/news/*`, `news_poller`.

---

## What it is for

Not "collect AI news". The purpose is to find stories that could realistically
become useful, interesting social content for a coding school. The audience is
students, people thinking about learning to code, developers, parents, and
anyone curious about technology and education.

Every story faces one test:

> Would someone learning to code, studying tech, or building software care about
> this or find it useful?

Being about AI is not a reason to include it.

### What gets in, and what does not

A new model, tool or product launch is a **good** story when you can say what it
lets people *do*, and a **bad** story when the story is a number.

| | |
|---|---|
| GOOD | "OpenAI released a new AI model that helps people code and learn faster" |
| BAD | "OpenAI's new model scores 4% higher on benchmark X" |

Benchmarks, funding rounds, research papers and API/pricing changes are not
searched for and are rejected by default. One gets in only when it has an
obvious, large, real-world consequence.

The judgement is about **impact**, never about category. That is a deliberate
replacement for the previous rule, which rejected model releases outright and
therefore buried launches a student would genuinely care about.

### High level only

Every story reaches the feed as exactly two things: a plain headline of at most
twelve words, and one or two ordinary sentences on why it matters. No detail, no
numbers, no mechanism. A story can be *selected* for a technical reason and must
still *arrive* written for someone with no technical background.

Three layers enforce that, and they drop rather than repair:

1. The judging prompt carries worked before/after examples.
2. The model marks its own work (`plain_enough`): "would a 10-year-old
   understand this headline?"
3. A pure function in code (`news_search.jargon_reason`) rejects version
   numbers, four-letter-plus abbreviations, a short blocklist of jargon words,
   and anything over twelve words.

---

## The flow

One search, when somebody asks for one. Two model calls, plus one cheap HTTP
request per kept story for its cover image.

It used to run itself at **07:00 Asia/Dubai**, with the schedule in a column.
That was removed: a search nobody asked for spends money on mornings nobody
reads, and the screen had to explain a cadence just to stop a quiet feed from
looking broken. `POST /api/trends/news/fetch {limit}` is the only way in, and
`limit` is a CEILING, not a target, chosen on the button every time.

1. **Check we can run.** Nothing already in flight, and the shared Vertex budget
   has at least $0.50 of headroom. Below that the run records `no_budget`, so
   "we could not afford to look" never reads as "there was no news".
2. **Search** (`gemini_search`, grounded, prose out). Built from the user's
   brief and the last seven days of headlines. If the grounding source list
   comes back **empty**, the whole reply is discarded: the model answered from
   memory.
3. **Judge and rewrite** (`gemini_text`, JSON schema). Per story: is this right
   for the audience, why, the plain headline, why it matters, an angle for 42, a
   relevance score, whether it repeats something we have, and whether it is
   plain enough.
4. **Filter, in code.** Drop unusable, not-plain, jargon-tripping, duplicate and
   too-old stories, clamping future dates to now.
5. **Write** to `trend_posts` with `source='news'` and the scores already
   filled. There is no separate scoring pass.
6. **Prune** anything past fourteen days.

## What the model decides, and what code decides

| The LLM | Plain code |
|---|---|
| Search the live web | Save rows to Postgres |
| Understand what happened | Check required fields |
| Decide whether it matters | Reject exact duplicate URLs (`sha1` of canonical URL) |
| Explain why it matters | Parse and clamp dates |
| Score relevance 0-100 | Delete past the retention window |
| Decide two stories are one event | Stop two runs overlapping |
| Rewrite headlines plainly | Enforce length caps and the jargon backstop |
| Judge understandability | Record what ran, kept and dropped, and why |

---

## What was measured, and why the prompts look like this

Live calls on 2026-09-05. Every one of these cost real result quality when
phrased any other way, and the findings are repeated in `news_search`'s
docstring so nobody "tidies" them away:

| Prompt shape | What actually came back |
|---|---|
| "Today is `<date>`" + "the last 48 hours" | **Nothing at all**, and an empty grounding list. The model declined rather than searched. |
| No recency framing | Evergreen SEO listicles dated across nine months. Domains garbled 4 times in 8. |
| "Never give a redirect link, give the publisher's own" | The model **fabricated** `https://www.youtube.com/watch?v=VIDEO_ID_PLACEHOLDER`. |
| "news published in the last 7 days", guides excluded, "never invent, leave it out" | 6 real news stories, 6 real publisher URLs, 6 domains present in the grounding list. |

So: the window is soft, the definition of news is spelled out at length, and no
instruction ever forbids a link shape the model may not be able to supply.
Forbidding one is an instruction to fabricate.

## The one verification left

We do not fetch pages. What survives costs nothing: `gemini_search` returns the
pages Google actually retrieved, and their titles are bare domains. A story
whose link points somewhere that never appeared in that list did not come from
the search, and is dropped. That is exactly what catches the fabricated YouTube
link above.

It cannot catch a real domain carrying an invented path. That residual risk is
accepted deliberately, and stated rather than hidden: a person reads every story
before anything is posted, and the run log makes it visible if it starts
happening.

---

## Tables

**`news_config`** - one row. `brief` (what to look for, in the owner's own
words) is the entire configuration surface, edited at Settings -> **News to look
for**. `stories_per_day` only remembers where the feed's "Up to" control was
left. `enabled` and `next_run_at` are vestigial: they drove the deleted
schedule, and are kept only because this is a shared production database and
dropping columns to tidy a feature is not a trade worth making.

**`news_runs`** - one row per search. `status`, `found`, `kept`, `drops`
(a `{reason: count}` map keyed by sentences a person can read), `error`,
`trigger`. Kept for fourteen days.

A run row is opened before the work and closed after, so a backend killed in
between leaves one at `running` for ever, and the screen reads "a search is
running" off exactly that. `fail_stale_runs` closes anything older than fifteen
minutes, and the Stop button (`POST /news/cancel`) closes it on demand.

## The row a story becomes

Stories go into the shared `trend_posts` table, so they inherit the feed,
ranking, reports and "Make this" for free.

| column | value |
|---|---|
| `ig_media_id` | `news:<sha1(canonical url)>` - the idempotency key |
| `caption` | the **plain-English rewrite** |
| `platform`, `username` | the outlet |
| `permalink` | the article link |
| `fit_score` | the relevance score, written at ingest |
| `outperformance`, `region`, `uae_relevance` | **NULL** |
| `metadata` | `original_headline`, `why_it_matters`, `canonical_url`, `date_is_estimated`, `brief_used`, `found_at` |

Those three NULLs are load-bearing. A search cannot honestly count how many
outlets ran a story, and Industry Signal is no longer scoped to a region. Because
of them, `trend_scoring.compute_opportunity` had to be made symmetric:
`outperformance` used to be the one signal never dropped from the blend, so a
NULL scored 0.0 and still took 40% of the weight. A news story now ranks on its
relevance alone, across the full 0-100 range.

## Endpoints

| Method | Path | What |
|---|---|---|
| GET | `/api/trends/news/health` | Did it run, what did it find, when is the next one. Never raises. |
| GET/PUT | `/api/trends/news/config` | The brief, the count, the switch. |
| POST | `/api/trends/news/brief/suggest` | Rough note in, briefs to pick from out. Never saves. |
| POST | `/api/trends/news/fetch` | "Find news now". Fire-and-forget. |
| GET | `/api/trends/news/outlets` | Publishers with counts, for the feed's filter. |

## Failure modes

Every one degrades; none stops the poller.

| Failure | Recorded as |
|---|---|
| Vertex outage or timeout | `search_failed`, with the error |
| Budget spent | `no_budget` - deliberately not `empty` |
| Empty grounding list | `ungrounded`, whole reply discarded |
| Unparseable judging reply | `judge_failed` |
| Nothing worth posting | `ok`/`empty` with 0 stories. **Not an error, and never padded** |

## Cost

Two Gemini calls a day, no HTTP. Roughly $0.05-0.10 daily against the shared
`VEO_BUDGET_USD` meter.

## The changeover

`news_store._MIGRATION` runs once, keyed on the old `news_sources` table still
existing: it deletes every row the RSS collector left in `trend_posts` and drops
`news_sources` and `news_settings`. Every later boot finds nothing to do. The
delete is unqualified and takes saved rows too, which was the owner's call: a
half-migrated feed mixing publisher headlines with plain-English ones is harder
to read than an empty one.
