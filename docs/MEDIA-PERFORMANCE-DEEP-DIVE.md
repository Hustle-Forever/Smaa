# 42 Command Center — Why the images were broken and slow, explained from scratch

**Written:** 28 July 2026
**Branch:** `yasin`
**Status:** all changes made, verified, **not committed**

---

## How to read this

This is written for someone who knows the frontend but not much backend. **Every term is
defined before it's used.** Nothing assumes you already know what a CDN or a round trip is.

It's long on purpose. Read it in chunks:

| If you have… | Read |
|---|---|
| 10 minutes | Part 1 (vocabulary) + Part 4 (the three bugs) |
| 30 minutes | Parts 1–7 — this is the actual teaching content |
| An hour | The whole thing |
| You just want the changes | Part 8, and Appendix A |

Parts 1–2 are **foundations** — general knowledge that applies to any web app.
Parts 3–7 are **this specific investigation**.
Parts 8–12 are **what we did about it**.
Part 13 is a **glossary** you can jump back to.

---

## Table of contents

**Foundations**
1. [Vocabulary — every term you need](#part-1--vocabulary)
2. [How this app actually stores and serves a picture](#part-2--how-this-app-serves-a-picture)

**The investigation**
3. [What you reported](#part-3--what-you-reported)
4. [The diagnosis: three unrelated bugs, not one](#part-4--the-diagnosis)
5. [Why milliseconds in Tokyo matter](#part-5--why-tokyo-matters)
6. [Thumbnails: what they are, how they work, and the three ways to make one](#part-6--thumbnails)
7. [Videos: when do they actually download?](#part-7--videos)

**The work**
8. [Everything we changed](#part-8--everything-we-changed)
9. [The measurements](#part-9--measurements)
10. [What we deliberately did NOT do](#part-10--what-we-did-not-do)
11. [The Tokyo/region question](#part-11--the-region-question)
12. [Still open — your decisions](#part-12--still-open)

**Reference**
13. [Glossary](#part-13--glossary)
14. [How to check any of this yourself](#part-14--verify-it-yourself)
15. [Appendix A — file-by-file changes](#appendix-a--file-by-file)
16. [Appendix B — the transferable lessons](#appendix-b--lessons)

---

# Part 1 — Vocabulary

Read this once. Everything later depends on it.

## 1.1 The three programs involved

Your app is not one program. It's three, on different machines:

```
┌──────────────────┐     ┌──────────────────┐     ┌────────────────────┐
│  BROWSER         │     │  BACKEND         │     │  DATABASE          │
│  (your laptop)   │────▶│  (your laptop,   │────▶│  (Supabase,        │
│  React, the UI   │◀────│   port 8000)     │◀────│   TOKYO)           │
└──────────────────┘     │  Python/FastAPI  │     └────────────────────┘
         │               └──────────────────┘
         │                                        ┌────────────────────┐
         └───────────────────────────────────────▶│  FILE STORAGE      │
              (images fetched DIRECTLY,           │  (Supabase Storage │
               not through the backend)           │   + CDN)           │
                                                  └────────────────────┘
```

- **Browser / frontend** — the React code in `frontend/src`. Draws the UI. Knows nothing
  except what the backend tells it.
- **Backend** — the Python code in `backend/app`. Answers questions from the browser, talks
  to the database and to file storage. Runs on port 8000.
- **Database** — Postgres, hosted by Supabase. Stores *rows*: "generation #746 exists, it's
  a template_fill, its image is at this path." It does **not** store the images themselves.
- **File storage** — Supabase Storage. Stores the actual image and video **bytes**.

**The single most important idea in this whole document:** the database stores *where a
picture is*, the file storage stores *the picture*. They are separate systems, and a lot of
this session's bugs came from the two disagreeing.

## 1.2 Frontend vs backend, concretely

| | Frontend | Backend |
|---|---|---|
| Language | TypeScript / React | Python |
| Lives in | `frontend/src` | `backend/app` |
| Runs on | Your browser | Your laptop, port 8000 |
| Can it read the database? | **No, never** | Yes |
| Can it hold secrets? | **No** — anyone can read it | Yes |

The frontend can never talk to the database. It *has* to ask the backend. That's why every
picture involves at least one backend request before the browser even knows the URL.

## 1.3 API, endpoint, route

- An **API** is the list of questions the backend will answer.
- Each question is an **endpoint**, identified by a URL like `/api/yas-producer/library`.
- In the code, an endpoint is defined by a **route** — a Python function with a decorator:

```python
@router.get("/library")                      # ← this is the route
async def yas_library(request, limit=200):   # ← this function runs when you hit it
    ...
    return {"items": [...]}                  # ← this becomes JSON the browser reads
```

So "**the `/library` route**" and "**the `/library` endpoint**" mean the same thing from
different sides: the route is the code, the endpoint is the URL.

**Where routes live in this app:** `backend/app/routers/`. The big one is
`yas_producer.py` (7,700 lines — it holds the Library, Poster, Talking Head, Video Editor
routes). Assets live in `producer_assets.py`.

## 1.4 Service

A **service** is a module of reusable logic that is *not* a route. Routes are thin (parse
the request, call a service, return JSON); services do the real work.

They live in `backend/app/services/`. The ones that mattered this session:

| Service | What it does |
|---|---|
| `asset_storage.py` | Everything about uploading, downloading, and building URLs for stored files |
| `yas_video_editor.py` | Video-editor timelines, thumbnails from video frames |
| `instagram_publish.py` | Talks to Instagram's API |
| `yas_local_store.py` | Reads/writes the poster-generations table |

Why the split matters: `asset_storage.py` is called from *dozens* of places. One bug in it
appears everywhere at once — which is exactly what happened.

## 1.5 Request, response, round trip

- **Request** — the browser (or the backend) asks for something.
- **Response** — the answer comes back.
- **Round trip** — one complete there-and-back. This is the unit that matters for speed.

A round trip's cost is mostly **distance**, not the work done. More on this in Part 5, but
the headline: your database is in Tokyo, and one round trip to it takes **~285
milliseconds** no matter how trivial the question.

## 1.6 Payload

The **payload** is the actual data in a response — the bytes.

Two different payloads mattered here:
- The **JSON payload** of `/library` — a text description of 200 tiles (was 168 kB).
- The **image payload** — the pictures themselves (was hundreds of megabytes).

They're independent problems. The JSON has to arrive *before* the browser even knows which
images to request, so a fat JSON delays everything behind it.

## 1.7 Latency vs bandwidth — the crucial distinction

People say "slow internet" for two completely different things:

- **Bandwidth** = how *much* data per second. Fixing this = send fewer bytes.
- **Latency** = how *long* before the first byte arrives. Fixing this = make fewer requests,
  or move the server closer.

**Adding bandwidth does not fix latency.** If Tokyo is 285 ms away, a 1-byte response still
takes 285 ms on a gigabit line.

This session had **both** problems, and they needed opposite fixes:
- Family B (huge images) was a **bandwidth** problem → fixed by making files smaller.
- Family C (slow pages) was a **latency** problem → fixed by making fewer round trips.

Confusing the two is why performance work often fails: people optimise the query (making
the *work* faster) when the cost was the *distance*.

## 1.8 CDN — and what a "CDN URL" is

**CDN** = Content Delivery Network.

A CDN is a network of servers spread around the world that keep **copies** of your files. When
someone requests a file, they're served from the nearest copy instead of from the original
("origin") server.

Why it matters: the original file might live in Tokyo, but a CDN edge server in Dubai can
hold a copy. Your browser then fetches from ~15 ms away instead of ~285 ms away.

A **CDN URL** is simply a URL that points at the CDN instead of at your backend. For example:

```
https://buzqjslfktktvzzpyhkc.supabase.co/storage/v1/object/public/producer-media/poster/412.png
└──────────────── the CDN, near you ────────────────┘└──── which file ────┘
```

**Why we care so much:** compare the two possible paths for one tile.

```
WITHOUT a CDN URL (the "proxy" path):
  browser → your backend → Supabase → your backend → browser
  4 hops, your backend holds the whole image in memory, and it's
  single-threaded so other requests queue behind it.

WITH a CDN URL:
  browser → CDN edge → browser
  1 hop, your backend isn't involved at all.
```

**Measured on your project:** the CDN edge answers in **~81 ms**, while the database is
**~285 ms** away. Your file storage was *never* the slow part. That's a genuinely
counter-intuitive finding and it redirected the whole investigation.

## 1.9 Bucket — and public vs private

A **bucket** is a named container for files, like a top-level folder. Your project has four.
Two matter:

| Bucket | Contains | Public? |
|---|---|---|
| `producer-media` | Create Poster / Animation output | **Public** |
| `producer-assets` | Uploaded reference photos, brand docs, Poster+Human output, Talking Head keyframes | **Private** |

**Public bucket** = anyone with the URL can fetch the file. No password, no token.
**Private bucket** = a plain URL is rejected. You need a *signed* URL (next section).

**Why `producer-assets` must stay private:** it holds **1.5 GB**, including 537 MB of
portrait and location photography and Poster+Human output — which is *a student's face
composited onto a template*. Making that bucket public would publish real people's
photographs at guessable URLs, permanently. You cannot un-publish a URL that's been crawled.

This constraint shaped every decision in Part 6.

## 1.10 Signed URL, token, TTL

To let a browser fetch from a **private** bucket without giving it a password, the backend
creates a **signed URL**: a normal URL with a cryptographic **token** attached.

```
https://…/storage/v1/object/sign/producer-assets/yas/generations/746.png?token=eyJraWQi…
                                                                        └── the token ──┘
```

The token says, in effect: *"whoever holds this may read this one file, until this
timestamp."* Supabase signed it, so it can verify nobody tampered with it.

- **TTL** = Time To Live. How long the token stays valid.
- Anyone holding the URL can use it until it expires — it's a **bearer token**. That's why
  a long TTL is a real (if small) security decision, not just a tuning knob.

We chose **24 hours** for thumbnail URLs: long enough that your browser caches images for a
full working day, short enough that an auth-free link to a private photo doesn't live for a
week. (The original code used 7 days in places; we brought those down.)

## 1.11 Cache, cache key, cache headers

A **cache** is a saved copy kept so you don't fetch the same thing twice. Your browser has
one on disk.

**The cache key is the exact URL.** This is the part people get wrong. If a URL changes by
even one character, the browser treats it as a *different file* and downloads it again.

**Cache headers** are instructions the server sends alongside a file telling the browser how
long it may reuse it:

```
Cache-Control: public, max-age=31536000, immutable
               │       │                 └─ never even ask if it changed
               │       └─ reusable for 31,536,000 seconds (1 year)
               └─ shared caches (CDNs) may store it too
```

vs.

```
Cache-Control: no-cache      ← ask me every single time
```

**Two real bugs in this session came from cache headers:**

1. Files were uploaded with **no** `Cache-Control` at all, so Supabase served them as
   `no-cache` — every view re-downloaded the full image, forever.

2. A **redirect** was cached for a year with `immutable`, while the signed URL it pointed at
   expired in 7 days. So eight days after you first viewed an image, your browser would
   keep replaying a dead redirect — a broken image for the remaining ~357 days, with no way
   to recover except clearing your cache. A genuine time bomb, and one that fixing the main
   bug would *not* have cleared.

## 1.12 Read time vs write time

This is one of the concepts you asked about, and it's the most important architectural idea
in the whole session.

Any derived value — like "the URL of this asset's thumbnail" — can be produced at one of two
moments:

**WRITE TIME** — computed once, when the data is created, and stored.

```
User uploads photo
   ├─▶ save the original
   ├─▶ MAKE THE THUMBNAIL NOW, save it        ← write time work
   └─▶ save a row pointing at both
```

- ✅ Fast to read later — it's already done.
- ❌ Anything created *before* you added the step has nothing (needs a **backfill**).
- ❌ Any code path that forgets to call the step silently produces broken data.

**READ TIME** — computed fresh, every time someone asks.

```
Browser asks for the asset list
   └─▶ backend looks at each row and BUILDS the URL right now   ← read time work
```

- ✅ Works for old and new data automatically. No backfill. Nothing to forget.
- ❌ Costs work on every request.

**How this played out in your app — three times:**

| Thing | When | Consequence |
|---|---|---|
| The broken `/public/` URL | **Read time** | Once we fixed the one function, **all 66 broken tiles** were fixed at once. No data migration. |
| The bucket choice on upload | **Write time** | Different upload paths were written at different times, and the newer ones forgot to pass the bucket. 203 rows silently got the *private* bucket. **This was the root cause of the entire "no preview" bug.** |
| Thumbnail derivatives | **Write time** (with a backfill) | We resize once at upload. 133 existing assets needed a one-off backfill. |

**The lesson:** read-time derivation is more robust — it can't drift and it can't be
forgotten. Write-time is for expensive work you don't want to repeat (like resizing an
image). We ended up using read time for *URLs* and write time for *resizing*, which is the
right split.

**Your question — "will new stuff automatically get thumbnails?"** — was exactly the right
question to ask, because the answer depends entirely on this distinction. Answer in §6.7.

## 1.13 Backfill

A **backfill** is a one-off job that applies a new rule to data that already exists.

If you add thumbnail generation at write time, every asset uploaded *before* that change has
no thumbnail. The backfill loops over them and creates the missing ones.

We ran one: **133 assets, 636 MB of originals → 2.74 MB of thumbnails.** Two failed on a
transient network error; a retry fixed both. Backfills are ordinary and expected — the thing
to watch is that they're **one-off** and must be **safe to re-run** (ours skips rows that
already have a thumbnail).

## 1.14 Thumbnail / derivative

A **thumbnail** is a small copy of an image for display in a grid. A **derivative** is the
general term for any file generated from another (thumbnail, preview, video still).

Your grid tiles are about **250 pixels** wide. Your source photos are up to **9.3 MB** and
several thousand pixels wide. Downloading the full photo to draw it 250 px wide wastes
roughly **99.7%** of the bytes — and the browser *also* has to decode the full-size image in
memory before shrinking it, which costs CPU and can make scrolling stutter.

More in Part 6.

## 1.15 WebP

An image **format**, like PNG or JPEG, but with much better compression — typically 25–35%
smaller than JPEG and *dramatically* smaller than PNG for photos.

Why PNG was hurting you: PNG is **lossless** — it stores every pixel exactly. Great for
screenshots and logos, terrible for photographs. Your generated posters were saved as PNG at
~1.4 MB each. The *same image* as WebP is ~20 kB. That's a **67×** difference from nothing
but a format change.

One subtlety we hit: PNG being lossless means the `quality=70` parameter **does nothing** to
a PNG. You have to actually convert the format.

## 1.16 Lazy loading

By default, a browser downloads every `<img>` on the page immediately — even ones far below
the fold that you may never scroll to.

```html
<img src="…" loading="lazy" />   <!-- wait until it's near the viewport -->
```

**A trap we found:** `loading="lazy"` only works on real `<img>` elements in the document.
This app had code doing:

```js
const img = new Image();      // not in the document
img.src = someUrl;            // downloads IMMEDIATELY, lazy is impossible
```

That pattern was downloading **60 full-resolution originals — 331 MB —** the moment a
creator page mounted.

**A second trap:** hiding an element with CSS `display: none` does **not** stop an eager
`<img>` inside it from downloading. This app keeps visited creator pages mounted-but-hidden,
so their history strips were quietly competing for bandwidth while you were on a completely
different page. `loading="lazy"` *does* fix that; `display:none` alone doesn't.

## 1.17 The event loop, and "blocking"

The backend is **async**. Simplified: it's a single worker that juggles many requests by
switching between them whenever one is *waiting* (for the database, for the network). That
switching machinery is the **event loop**.

The rule: while a request is waiting, others make progress. But if some code does slow work
**without waiting** — a heavy CPU computation, or a subprocess — the loop can't switch. It's
**blocked**. Every other request in the entire app freezes until it finishes.

We found five of these: calls to `ffmpeg` (video processing, up to 60 seconds) and `cv2`
(OpenCV) sitting directly inside async functions. **One cold video thumbnail could freeze
Dashboard, Library and Assets simultaneously.**

The fix is `asyncio.to_thread(...)` — run the blocking work on a separate thread so the loop
stays free. With a **semaphore** (a counter that caps concurrency) so 20 simultaneous
ffmpeg jobs can't exhaust the thread pool.

## 1.18 Connection pool, transactions, autocommit

Opening a database connection is expensive: a TCP handshake, a TLS handshake, then
authentication — several round trips, so **~1.1 seconds** to Tokyo.

A **connection pool** keeps a few connections open and lends them out. `DB_POOL_MIN=1`,
`DB_POOL_MAX=4` in your `.env` means: keep 1 idle, allow up to 4.

A **transaction** groups statements so they all succeed or all fail. In SQL:

```sql
BEGIN;                    -- start
UPDATE …;
UPDATE …;
COMMIT;                   -- make it permanent
```

`BEGIN` and `COMMIT` are **each their own round trip**. Essential when writing. Pure waste
when only reading.

**autocommit** means "no transaction, just run the statement." For a read-only endpoint it
saves two round trips — ~570 ms against Tokyo.

Your codebase's own `db.py` documentation *recommends* autocommit for read-only endpoints.
**1 of 418 call sites followed it.** That one line of guidance, unfollowed, was worth about
half a second on every single API call.

There's also a pool setting called `check` that validates a connection before lending it —
which costs *another* round trip. It sounds like an obvious thing to delete, but it's there
to catch connections that Supabase's pooler killed while they sat idle, and the library does
**not** silently retry. Deleting it would trade a reliable ~285 ms for occasional hard
errors. We kept it and got the win elsewhere.

---

# Part 2 — How this app serves a picture

Now let's trace one Library tile end to end.

## 2.1 The tables

| Table | Rows | What it holds |
|---|---|---|
| `yas_generations` | ~290 | Poster+Human, Talking Head, Video Editor records |
| `producer_assets` | 135 | Uploaded reference material — photos, fonts, stickers, audio |
| `yas_animations` | ~150 | Veo video clips |
| `yas_poster_generations` | ~395 | Create Poster records |

Note a wrinkle you'll see referenced later: a code comment described
`yas_poster_generations` as a "local JSON store." **That comment is stale** — it's a normal
Supabase table. That mattered because it made three "cheap local reads" actually three
round trips to Tokyo.

## 2.2 What a row says about its image

Every media row carries some combination of:

```
storage_bucket : 'producer-assets'              ← which bucket
storage_path   : 'yas/generations/746.png'      ← which file inside it
local_path     : 'data/producer/…'              ← a copy on the backend's own disk (fallback)
mime_type      : 'image/png'                    ← what kind of file
```

## 2.3 The chain, step by step

```
1. You open the Library.

2. Browser  ──GET /api/yas-producer/library──▶  Backend

3. Backend queries Postgres (Tokyo):
      "give me the 200 newest generations"                       ~285 ms

4. For EACH row, the backend decides what URL the tile should use:
      _library_item()  in yas_producer.py
        └─▶ _public_media_url()   ← THE FUNCTION AT THE HEART OF THE BUG

5. Backend returns JSON:
      { "items": [ { "id": 746, "thumbnail_url": "https://…", … }, … ] }

6. Browser renders <img src={item.thumbnail_url}>

7. Browser fetches each image — ideally from the CDN, directly.
```

Step 4 is where everything went wrong. That one function decided, for every tile in the app,
whether the browser would get a fast CDN URL or a slow backend URL — or, as it turned out, a
URL that simply doesn't work.

## 2.4 The two possible URL shapes

**Fast — a CDN URL** (browser → CDN, backend not involved):
```
https://<project>.supabase.co/storage/v1/object/public/producer-media/poster/412.png
```

**Slow — a proxy URL** (browser → backend → Supabase → backend → browser):
```
/api/yas-producer/poster-human/746/file
```

The proxy path exists as a fallback: for files that only live on the backend's own disk, and
for private files when no signed URL could be made. It always *works* — it's just slow, and
it burns your backend's single worker.

---

# Part 3 — What you reported

Your words:

1. Dashboard takes too long to load, **and I get a lot of "no preview" which I don't want**
2. Library takes too long to load, even the images take too long, **and I also get no preview there**
3. Same for Assets, it takes time to load
4. Sometimes when I'm using reference images saved in Assets, it takes time to load — e.g.
   the background images in Talking Head Studio

And your conclusion: *"all of them have images loading issues in common."*

That conclusion was **reasonable but wrong**, and it's the most interesting thing about this
whole session.

The screenshot showed twelve tiles all reading **"no preview."** That looks like a loading
problem. It wasn't. Those images were never going to load — they were returning a hard
error. But because the code handles a failed image by swapping in a placeholder, a **crash
was disguised as slowness**.

---

# Part 4 — The diagnosis

Four symptoms. **Three unrelated bugs.**

| | Family | Type | Symptom it caused |
|---|---|---|---|
| **A** | Public URL on a private bucket | **Correctness** | "no preview" — never loaded at all |
| **B** | No thumbnails anywhere | **Bandwidth** | Slow *images* |
| **C** | Round trips × distance | **Latency** | Slow *pages*, before any image started |

---

## 4A — The "no preview" bug

### What the code did

`backend/app/routers/yas_producer.py`, in `_public_media_url()`:

```python
if config.producer_media_public and bucket and path:
    base = asset_storage.public_render_url(bucket, path, …)
```

Read that carefully. It asks: *"is the `producer_media_public` setting turned on?"*

But `producer_media_public` is a **single on/off switch describing one specific bucket**
(`producer-media`). Meanwhile `bucket` is **whatever this particular row says** — and could
be any bucket.

Your `.env` has `PRODUCER_MEDIA_PUBLIC=1`, so the switch was on. And **every single row** in
`yas_generations` says its bucket is `producer-assets` — the **private** one.

So the backend confidently built a *public* URL for a *private* bucket:

```
GET /storage/v1/render/image/public/producer-assets/yas/generations/746.png?width=480
→ HTTP 400  {"statusCode":"404","error":"Bucket not found"}
```

The browser's `<img>` fails, the `onError` handler fires, and the tile renders the literal
text "no preview."

### How bad it was — measured

- **66 of 200** items in the Library payload emitted this broken URL.
- On the **default first page (48 tiles): 38 were broken.**
- In the **top 24 tiles I actually fetched: 0 of 24 loaded.** 20 were this 400, one was a
  row with no image at all, three were redirects to whole MP4 files.

### Why the code drifted into this state

`asset_storage.upload()` has a default:

```python
dest_bucket = bucket or config.producer_assets_bucket   # ← PRIVATE by default
```

If a caller doesn't say which bucket, files go to the **private** one.

- Create Poster and Create Animation **pass the bucket explicitly** → public bucket ✅
- The newer Poster+Human and Talking Head writers **don't** → private bucket ❌

Same function, five call sites, two different destinations, and **nothing failed at write
time.** The files uploaded fine. The rows saved fine. The bug only appeared much later, in a
different file, when something tried to build a public URL for them.

**This is the lesson worth keeping:** a *default* that is safe in one context (private is the
safe default for user uploads) became a *bug* in another (generated output that needs to be
publicly servable). Defaults hide decisions. A required argument would have caught this at
the moment the code was written.

### The trap inside the fix — and a mistake I made

Early on I tested the thumbnail system and reported it was "excellent": 1.5 MB PNG → 42 kB.
**The size was right. The image was wrong.**

Supabase's image transformer takes parameters like `width=480`. If you give it *only* a
width, it defaults to a resize mode called `fill`, which **keeps the source height** and
slices the width off. Measured on a real poster:

| Parameters | Result |
|---|---|
| `width=480` | **480 × 1024** — cropped, headline cut off, 42,226 B |
| `width=480&height=480&resize=contain` | **480 × 480** — correct, and *smaller*: 34,646 B |

Why this mattered operationally: if we had fixed the bucket bug alone, 38 tiles would have
flipped from "no preview" to **a wrongly-cropped image** — which is *worse*, because it looks
like it worked. So the two changes had to land in the same commit.

---

## 4B — There were no thumbnails. Anywhere.

Not "the thumbnails were too big." **Zero of 135 assets had a thumbnail.** Every grid in the
app loaded the full-resolution original.

Measured:

| | Value |
|---|---|
| `producer-assets` bucket | 503 objects, **1,543 MB**, average **3,142 kB** |
| Largest single asset | **94 MB** |
| `img_bg` (Background) category | 59 files, **563 MB**, average **9.3 MB each** |
| A Talking Head tile | **1.34 MB PNG**, taking 1.5–2.2 s, to fill a ~200 px box |

So opening **Assets → Background** meant downloading **563 MB** to draw 59 squares 250 px
wide.

### And it got worse: the 331 MB prefetch

`frontend/src/lib/prefetchAssets.ts` was *trying* to help. On creator mount it pre-loaded 60
asset images so the picker would feel instant later.

Except it pre-loaded `fileUrl` — the **full-resolution original**. At ~3.1 MB average that's
**331 MB**, fired eagerly on mount.

Three separate things made it worse:

1. It used `new Image()`, which **bypasses** `loading="lazy"` — so all 60 fired at once,
   saturating the browser's ~6 connections-per-origin limit.
2. Its own comment claimed the files were immutable-cached, so the work would pay off. **The
   comment was false** — that header only applied to the *other* bucket, so these files came
   back `no-cache` and were re-validated anyway. The prefetch bought **nothing**.
3. Because visited creators stay mounted under `hidden`, the burst kept competing for
   bandwidth while you were on Dashboard, Library or Assets.

It was **net-negative work**: pure cost, zero benefit. That single line was the highest-value
deletion in the codebase.

> **A general lesson:** a comment asserting an invariant that no code enforces is worse than
> no comment. It stops the next person from checking.

---

## 4C — Slow pages: distance × repetition

This is the part with the counter-intuitive finding.

### The setup

```
DB_HOST = aws-1-ap-northeast-1.pooler.supabase.com
                   └── ap-northeast-1 = TOKYO ──┘
```

You're in Abu Dhabi. Your database is in Tokyo. Measured TCP round trip: **~285 ms**.

### One "read" cost four round trips

```
1. pool `check`   — validate the borrowed connection      ~285 ms
2. BEGIN          — start a transaction                   ~285 ms
3. SELECT …        — the actual query                     ~285 ms
4. COMMIT         — end the transaction                   ~285 ms
                                             ─────────────────────
                                                        ≈ 1.1 s
```

Three of those four do **no useful work for a read.**

### And `/library` did that four times, in a row

```
yas_producer.py:2762   main query
yas_producer.py:2810   ystore.recent()            ← "local JSON store" (it isn't)
yas_producer.py:2814   anim_store.recent()
yas_producer.py:2845   roadmap_state.all_active()
```

Four sequential connection acquisitions × 4 round trips = **16 round trips ≈ 4.4 s**, before
any images were even mentioned.

### The finding that reframed everything

I ran `EXPLAIN ANALYZE` on the main query — that's Postgres telling you exactly how long it
spent:

```
Execution Time: 3.858 ms
```

**The query takes 3.9 milliseconds. The endpoint took 10–15 seconds.**

The database was doing its job **0.1%** of the time. The other 99.9% was data travelling to
Tokyo and back, repeatedly.

This is why I didn't spend a minute optimising SQL or adding indexes. **When latency is high,
the only variable that matters is the *number* of round trips.**

### The biggest single cause of a slow Dashboard wasn't the database at all

`Dashboard.tsx` fired four fetches in parallel — correctly, with `Promise.allSettled`. Then
it threw that away:

```tsx
const [s, a, ig, lib] = await Promise.allSettled([...]);  // ✅ parallel
setLoading(false);                                        // ❌ waits for ALL four
if (loading) return <SkeletonDashboard />;                // ❌ nothing renders
```

One of those four was the Instagram summary, which looped **40–60 sequential** calls to
Instagram's API — **11 to 17 seconds**.

And because the page returned a skeleton until *everything* settled, the twelve tile `<img>`
elements **did not exist in the DOM**. The browser couldn't start downloading images it
hadn't been told about yet. The entire image pipeline was queued behind a chart nobody was
waiting for.

> **Lesson:** parallel fetches collapsed into one shared `loading` boolean are only as fast
> as the slowest one. The fix isn't more parallelism — it's letting each section render
> independently.

### The fat JSON

Profiling the query showed one more thing: it selected `g.metadata` — a JSON blob — wholesale.

```
200 rows of metadata = 856 kB
```

For video-editor rows that blob holds an entire editable timeline, averaging 10 kB per row.
Shipping 856 kB from Tokyo cost about **1.2 seconds** (measured: query 1.73 s with it, 0.55 s
without).

And the tile builder reads exactly **two things** out of it.

---

# Part 5 — Why Tokyo matters

You asked specifically why milliseconds of distance make a difference. This is worth
understanding properly because it drives most backend performance work.

## 5.1 Where the 285 ms comes from

Light in fibre travels about **200,000 km/s**. Abu Dhabi → Tokyo is ~8,000 km, and cables
don't go straight — call it 12,000 km of actual fibre.

```
12,000 km ÷ 200,000 km/s = 60 ms one way
                         = 120 ms there and back
```

Then add real-world overhead: routers, switches, undersea cable hops, queuing. Measured
reality: **~285 ms**.

**This is physics plus infrastructure. No code change reduces it.** A faster database, a
better query, more RAM — none of it moves this number.

## 5.2 Why the count is everything

```
1 round trip   =  0.285 s
4 round trips  =  1.14 s     ← what one "read" cost
16 round trips =  4.56 s     ← what /library cost
```

Now compare optimising the two halves:

| Change | Saving |
|---|---|
| Make the 3.9 ms query twice as fast | **0.002 s** |
| Remove one round trip | **0.285 s** |

Removing a single round trip is worth **142× more** than halving the query time. That ratio
is why the whole fix was about *restructuring calls*, not tuning SQL.

## 5.3 Sequential vs parallel

```
SEQUENTIAL — 3 reads, one after another:
  ├──285──┤
          ├──285──┤
                  ├──285──┤        total 855 ms

PARALLEL — 3 reads at once:
  ├──285──┤
  ├──285──┤
  ├──285──┤                        total 285 ms
```

Same work, same data, **one third of the time** — purely from overlapping the waiting. This
is what `asyncio.gather` does, and it's why we used it on the three independent Library
reads.

## 5.4 The one thing that isn't slow

Measured on your project:

| | Round trip |
|---|---|
| Postgres (Tokyo) | **~285 ms** |
| Supabase Storage CDN | **~81 ms** |

The **file storage is fine.** Its CDN has an edge node near you. So "images are slow" was
never about where the images lived — it was about how *many* bytes and how *many* database
trips stood in front of them.

## 5.5 What would actually fix the floor

`/api/health` does one trivial read. After our fixes it takes **0.45 s**. That's the floor:
about two round trips of pure distance.

Moving the database to `me-central-1` (UAE) would cut ~285 ms to ~15 ms — dividing the
remaining constant by roughly **five**. That's worth more than every remaining code
optimisation combined. See Part 11.

---

# Part 6 — Thumbnails

You asked: *what does a thumbnail even do, and how?* Here's the whole picture, including the
three approaches and why we ended up using two of them together.

## 6.1 What the waste actually is

A grid tile is ~250 × 250 CSS pixels. On a 2× (retina) screen you want ~500 × 500 real
pixels.

One of your background photos:

```
Source:  4000 × 3000 pixels, 9.3 MB JPEG   = 12,000,000 pixels
Needed:   500 ×  500 pixels                =    250,000 pixels
                                              ────────────────
                                              98% of pixels thrown away
```

Two costs, not one:

1. **Bandwidth** — 9.3 MB downloaded to show ~25 kB worth of detail.
2. **CPU and memory** — the browser must *decode* the full 12-megapixel image (roughly
   48 MB uncompressed in RAM) before it can scale it down. Do that for 59 tiles and the tab
   stutters or the page freezes even after the bytes have arrived.

A thumbnail is just a **small copy**, made once, so neither cost is paid.

## 6.2 The three ways to make one

**Option 1 — On-the-fly transform (the CDN resizes for you)**

You request a special URL and the CDN generates the size you asked for, caching the result.

```
https://…/storage/v1/render/image/public/producer-media/poster/412.png?width=480&height=480&resize=contain
                     └── "render", not "object" ──┘                    └── the size you want ──┘
```

- ✅ No storage, no pre-processing, works for anything already uploaded.
- ❌ **Only works on PUBLIC buckets.** Verified: on a private bucket it returns HTTP 400.
- ❌ It's a paid Supabase feature (you're on Pro, so it's available).

**Option 2 — Stored derivative (make a small copy at upload)**

Resize once when the file arrives; store the small version next to the original.

- ✅ Smallest possible files, no per-request work, works on private buckets.
- ❌ Write-time — needs a **backfill** for existing files, and any upload path that forgets
  the step produces nothing.

**Option 3 — Let the browser do it**

Send the full image, set `width: 250px` in CSS.

- ❌ This is what the app was doing. It's not a thumbnail at all — it pays every cost and
  saves nothing.

## 6.3 The private-bucket problem, and the journey to solving it

Here's the actual sequence of discovery, because the reasoning matters more than the answer.

**Step 1 — the obvious fix was unacceptable.**
Option 1 needs a public bucket. Making `producer-assets` public would publish 1.5 GB
including students' faces at guessable URLs, irreversibly. **Rejected.**

**Step 2 — signed transforms exist.** I tested whether Supabase can transform a *private*
file via a signed URL. It can:

```
POST /storage/v1/object/sign/producer-assets/yas/generations/746.png
     {"expiresIn": 86400, "transform": {"width":480,"height":480,"resize":"contain"}}

→ /render/image/sign/…?token=…    (the size is baked INTO the token)
→ 1,370,135 B PNG became 20,460 B WebP  — 67× smaller, no password needed
```

Excellent result. But:

**Step 3 — it didn't scale.** Signing is one HTTP request *per file*. For a 100-tile page
that's 100 requests. Measured: **26 seconds**, and Supabase started dropping connections.
Worse than the bug we were fixing.

**Step 4 — batch signing.** Supabase can sign **many paths in one request**:

```
POST /storage/v1/object/sign/producer-assets
     {"expiresIn": 86400, "paths": ["a.png", "b.png", … 100 of them]}

→ 100 signed URLs in 0.67 seconds
```

From 26 s to 0.67 s. **But** the batch endpoint **ignores the `transform` field** — I
verified this by decoding the returned token: no transformation claim, just a download scope.

**Step 5 — the useful accident.** I tried taking a plain (non-transform) batch token and
simply rewriting the URL path from `/object/sign/` to `/render/image/sign/`. Supabase accepts
it, and still **re-encodes the image to WebP**:

```
1,370,135 B PNG  →  63,220 B WebP     (21.7× smaller, one round trip for the whole page)
```

Not as good as 20 kB — it re-encodes but doesn't *resize* — but one round trip instead of 100.
That's what shipped first.

**Step 6 — closing the gap properly.** 429 kB average for the big backgrounds was still more
than a 250 px tile needs. So we added Option 2 *underneath* Option 1's delivery mechanism:

- **Resize once at upload** (Python + Pillow) to a 480 px WebP.
- Store it **in the same private bucket** under `thumbs/…` — a derivative is still the
  picture, so it must not go anywhere more public than the original.
- Record its path in the row's `attributes._thumb_path`.
- The list endpoint **batch-signs the derivative** instead of the original.
- If no derivative exists, it signs the original — larger, but never broken.

Final measured result on the Background picker, all 59 tiles actually fetched:

| | Bytes |
|---|---|
| Originals (the old behaviour) | **563.2 MB** |
| After step 5 (re-encode only) | 25.9 MB |
| **After step 6 (real resize)** | **1.42 MB** — avg 24 kB, 0 failures |

**~400× smaller**, and the backend isn't involved in serving any of it.

## 6.4 Why this needed a cache-key fix too

The signed-URL cache stored URLs keyed by `(bucket, path, expiry)`. Once the same file could
be requested at *different sizes*, that key became wrong: a thumbnail request could be
handed back the full-size URL that happened to be cached first.

We added a fourth part to the key describing the transform. Small change, but without it the
whole design silently mis-serves images — the kind of bug that's very hard to spot because
everything still "works."

## 6.5 Why the TTL is a real decision

Signed transform responses carry **no `Cache-Control` header** — only an expiry. So:

> **How long your browser can cache a thumbnail = the token's TTL.**

- Longer TTL → better caching, but a longer-lived auth-free link to a private photo sitting
  in your page's HTML.
- Shorter TTL → tighter security, but the URL rotates and images re-download.

We chose **24 hours**: a full working day of cache hits; rotation cost is ~2.7 MB/day. The
original code had 7 days in places — six extra days of exposure for negligible benefit.

## 6.6 The mandatory fallback

Supabase image transformation is a **paid feature**, and this design makes it load-bearing
for every grid in the product. If the plan lapsed or a quota tripped, *every* thumbnail would
400 at once.

So the shared `<Thumb>` component has a two-step fallback: **thumbnail → full-res proxy →
neutral placeholder.** That turns a catastrophic failure into a merely slow one. It's
insurance, not polish.

## 6.7 Your actual question: will new uploads get thumbnails automatically?

**Yes.** And it's worth understanding *why*, because it's the read/write-time distinction
from §1.12 in practice:

- **The URL is derived at READ time.** Every time the asset list is requested, the backend
  looks at each row and mints a fresh signed URL. Nothing is stored, nothing can go stale,
  and there's no step to forget.
- **The resize happens at WRITE time**, hooked into all three upload paths. New uploads get a
  derivative automatically.
- **If the derivative is ever missing** — a new upload path someone adds later, a failed
  resize — the read-time signing falls back to the original. Larger, but the tile still
  works.

I verified the two conditions that could break it:

- `mime_type` is always resolved through a fallback chain, so it's never empty for an image.
- `storage_bucket` comes from the upload helper, which uses Supabase whenever configured.

The 2 assets still without thumbnails are **correct** exclusions, not gaps:
one is a **video** (Supabase can't transform video), one is a **legacy local-disk row** with
no Supabase copy. Neither can happen to a new upload.

---

# Part 7 — Videos

You asked: *do we load real videos, or only when the user clicks?*

Before this session: **partly on load, and one case was catastrophic.** Now: **only on
interaction.**

## 7.1 The `preload` attribute

```html
<video src="movie.mp4" preload="none" />
```

| Value | What the browser downloads before you interact |
|---|---|
| `none` | **Nothing.** Just reserves space. |
| `metadata` | The file header — duration, dimensions, codec. Usually 50–500 kB, but it's a **real network request per video**. |
| `auto` | As much as it feels like. Potentially the whole file. |

Your grids used **`preload="metadata"`**. With ~48 video tiles under the Talking Head filter,
that's ~48 network requests before you touched anything — competing for the same ~6
connections your images needed.

Since hover-to-play was already wired up (the video fetches on demand when you hover),
`metadata` was buying nothing. Now both grids use **`preload="none"`**.

## 7.2 The 29 MB poster bug

`<video>` has a `poster` attribute — a still image shown before playback:

```html
<video src="clip.mp4" poster="thumbnail.jpg" />
```

The backend, for a finished video-editor project, set:

```python
video_url = "/api/…/composition/file"
thumbnail_url = video_url          # ← the same URL
```

The frontend then did:

```tsx
<video src={previewVideoUrl} poster={item.thumbnail_url} />
```

So `poster` pointed at **an MP4**. The browser's *image* loader was handed a video file.
I measured one: **29,109,822 bytes — 29 MB** — being pulled through the image pipeline, where
it could never render.

There were **27** such rows. The fix guards the alias:

```tsx
poster={item.thumbnail_url && item.thumbnail_url !== previewVideoUrl
          ? item.thumbnail_url : undefined}
```

## 7.3 So what happens now

| Moment | What downloads |
|---|---|
| Grid renders | The **still image** only (~24 kB WebP) |
| You hover a video tile | Playback begins; the browser streams progressively |
| You move away | Paused, reset to the start |
| You click | Detail view opens and loads the real video |

## 7.4 Range requests — and why one "obvious optimisation" was forbidden

Video seeking works through **HTTP Range requests**: the browser asks for *"bytes 5,000,000
to 6,000,000"* so dragging the scrubber doesn't download the whole file.

This is why one tempting change was on the do-not-do list. Adding gzip compression across the
whole API (`GZipMiddleware`) sounds free — smaller JSON. But I verified in the installed
Starlette version that it has **no** exclusion for video content types and **no** Range
handling. It would have compressed video responses and broken seeking.

Production already compresses correctly at the edge (Caddy's `encode zstd gzip`), which
*does* skip media. So the right answer was: do nothing in the app.

---

# Part 8 — Everything we changed

Organised by the phase plan. Every item says *what* and *why in plain terms*.

## Phase 0 — Stop the bleeding

**0.1 — Deleted the 331 MB prefetch**
`prefetchAssets.ts` was eagerly downloading 60 full-resolution originals on creator mount,
for no benefit (§4B). Later rewritten (1.4) to do the job properly.
→ **−331 MB** on mount.

**0.2 — The "no preview" fix** *(four changes that had to land together)*
- `_public_media_url` now checks **whether this bucket is actually public**, via a new
  `bucket_is_public()` backed by a real Supabase query, cached for 5 minutes.
- Added `height` + `resize=contain` to the thumbnail transform so images scale instead of
  crop.
- The 5-minute TTL (rather than reading once at startup) matters because this is a *shared*
  project — a teammate flipping a bucket shouldn't require everyone to restart.
- Unknown bucket ⇒ treated as private ⇒ backend proxy. **Slow is the safe failure, not
  broken.**
→ **66 broken tiles fixed.** The 400 is now structurally impossible.

**0.3 — Defused the redirect time bomb**
A redirect was cached for a year with `immutable`, pointing at a 7-day signature (§1.11). Now
capped at 1 hour with revalidation allowed. Critically, this was done **at the call sites**,
not by editing the shared header constant — that same constant is correctly used for real
file responses where a year *is* right.

**0.4 — `autocommit=True` on read-only paths**
Removes `BEGIN` + `COMMIT` — two round trips — from reads. Applied selectively to provably
read-only blocks. **Not** flipped globally: 418 call sites, many of them writers relying on
transaction atomicity, and a missed one becomes a silent data-corruption bug.

**0.5 — Video hygiene** — `preload="metadata"` → `"none"`, and guarded the MP4-as-poster
alias (§7.2).

**0.6 — Un-gated the Dashboard**
Four independent fetches, four independent states, per-section skeletons. The 11–17 s
Instagram call moved off the first-paint path entirely.
→ First paint **12–17 s → ~1 s**, and images start immediately instead of after the chart.

**0.7 — Fixed an Assets refresh bug**
Two effects, `fetch` declared before `invalidate`. React runs effects in declaration order,
so after an upload the fetch hit a still-fresh cache and returned early — and *only then* was
the cache marked stale. Your new asset didn't appear until you reloaded (which re-paid the
whole download). Replaced with an explicit `force` flag: **order-dependence was the bug**, so
the fix removes the dependence rather than reshuffling.

**0.8 — `loading="lazy"` on hidden creator history strips** (§1.16, second trap).

## Phase 1 — Make the images small

**1.1 — Threaded transforms through the signing layer**, and **extended the cache key** with
a transform dimension (§6.4). Also made an upload evict stale cached signings — previously
only deletion did, so a regenerated image could serve stale art.

**1.2 — `thumb_url` in the assets list response**, batch-signed, one round trip per bucket.
Skips video and SVG (Supabase can't transform them) so those never get a URL guaranteed to
400.

**1.3 — Pointed 11 display sites at the thumbnail** — Assets grid, asset picker, slot chips,
and the creator reference chips.

> **A check worth highlighting:** before switching these, I verified none of those
> `thumbnailUrl` fields flow into a **request body**. If a thumbnail URL had been sent to the
> AI model as its input reference, we'd have silently degraded generation quality — a bug
> that would have been very hard to trace back. They're display-only; the generate call sends
> IDs (`template_asset_id`, `student_asset_id`).

**1.4 — Rewrote the prefetch properly** — thumbnails not originals, budgeted by **bytes** not
count (so a few 9 MB files can't blow it), and wrapped in `requestIdleCallback` so it never
competes with what you're waiting for.

**1.7 — A mid-size tier for the detail drawer** (1400 px, ~150 kB instead of 9–15 MB).
Also **deleted** a `signed_url` field the backend minted on every drawer open that **no
caller ever read** — a wasted Supabase round trip, every time.

## Phase 2 — Round trips and blocking

**2.1 — Parallelised the three independent Library reads** with `asyncio.gather` (§5.3).

**2.2 — Offloaded five blocking `ffmpeg`/`cv2` calls** to threads with a `Semaphore(2)`
(§1.17). The semaphore is required, not decorative: without it ~23 cold thumbnails could each
hold a thread for up to 60 s and starve the whole process.

**2.3 — Fixed the Talking Head file proxy** — it was signing with a 600-second TTL (which
collapsed the URL cache to a 30-second floor, so it re-signed constantly), opening a **fresh
HTTP client per image**, and buffering **1.4 MB through backend memory**. Now it redirects
and lets the CDN serve the bytes.

**2.4 — Removed a duplicate query** — `resolve_still_serving` fetched a row, then called a
function that fetched the same row again.

**2.5 — `Cache-Control` on uploads**, chosen by whether the path is overwritten. Files
written once get a year; the Talking Head keyframe path is *upserted* on regenerate, so
immutable would pin the old image for a year — that one gets 24 hours. **New uploads only, no
backfill.**

**2.6 — Cache headers on four bare file routes.** Note: this Starlette version has no
`if-none-match` handling, so a 304 is impossible here — the header is what saves the round
trip, not a validator.

**2.7 — Fixed the Instagram fan-out** — 40–180 sequential API calls became concurrent, capped
at **4**. Deliberately low: the **publish** path shares this token, and throttling ourselves
would block actual posting — far worse than a slow chart. Also memoised whether the token can
read insights at all (it was making two doomed requests per post) and raised the cache from
2 minutes to 15.

**2.8 — Made the font stylesheet non-blocking.** The Google Fonts `<link>` was
**render-blocking**: the browser wouldn't paint until Google answered. Now loaded
asynchronously with a `<noscript>` fallback.
*(The Supabase `preconnect` from the plan needs a `VITE_SUPABASE_URL` env var, and no `VITE_`
variables exist in your repo — inventing deploy config for ~50 ms wasn't worth it.)*

**2.9 — A shared `<Thumb>` component** — shimmer while loading, fade-in on load, and the
two-step error fallback from §6.6. Before this, **0 of 51** `<img>` sites in the app had an
`onLoad`, so a slow grid looked *frozen* rather than *loading*.

**2.10 — Per-params client cache** so Dashboard ↔ Library navigation is instant.
The trap here: the store keyed freshness on a **single** `lastParams`, so the Dashboard
(wanting 12 items) and the Library (wanting 300) would each see the other's entry as a miss
and **evict it** — routing the Dashboard through the store naively would have made things
*worse*. Now cached per parameter set. And `remove()` prunes **every** cached set, or a
deleted item reappears when you switch pages.

## Phase 3 (partial)

**3.2 — Added a route that never existed.**
`/poster-human/animations/{id}/file` was **referenced in three places but never defined** —
so every animated Poster+Human tile 404'd.
The tempting one-line fix was to alias it to the existing animation route. That would have
been a **data leak**: the two routes read *different tables* with *independent* ID sequences,
so for any colliding ID it would serve a stranger's video under your tile. A 404 is strictly
better than wrong data. We added the real route.

**3.3 — The metadata projection** — **856 kB → 141 kB** by selecting only the two fields the
tile builder reads (§4C).

**3.2b — Fixed a cache-key mismatch.** The backend stringified a `datetime`, giving a **space**
separator (`2026-07-26 17:03:25+00`), while the frontend built its version tag from the JSON
ISO form with a **`T`**. Same image, two different URLs → **cached twice**, and a Library tile
could never reuse the creator grid's copy. Both now use `isoformat()`.

## Data integrity — the rows with no image

Two populations that looked identical in the UI but aren't the same thing at all:

**The 14 "ready" Talking Head rows with no image are not corrupt.**

```python
# yas_talking_head_v2.py:1411
STATUS_READY,   # row's `status` reflects the SCRIPT being ready
```

The row is inserted with `status='ready'` when the **script** finishes — *before any image
exists*. So `status` means "the script is ready" while the Library read it as "the artifact is
ready." **One column carrying two meanings.**

These are abandoned drafts. The bytes were never lost because they were never created — no
repair job would help. The fix was to make Library eligibility test **media presence**, not
status.

**The 70 video-editor rows with no image are correct by design** — a timeline is a *document*,
not an image. Of those, **11** legitimately have nothing to show (empty or audio-only
timelines).

So the UI now distinguishes three states that used to collapse into one grey box:

| State | Rendering |
|---|---|
| Loading | Shimmer |
| **Nothing to show** | Neutral "Empty timeline" — *not an error* |
| **Broken** | Warning + "Preview unavailable" — should now be rare |

> A URL that always fails is strictly worse than an honest `null`: it costs a round trip
> **and** it makes a data state look like an outage.

---

# Part 9 — Measurements

## 9.1 Before and after

| | Before | After |
|---|---|---|
| `/library?limit=200` | 10–15 s | **2.53 s** |
| `/library?limit=12` (Dashboard) | — | **1.44 s** |
| `/assets?limit=500` | 3.45 s | **1.44 s** |
| `/api/health` | 1.05 s | **0.45 s** |
| `/stats` | 1.85 s | 1.66 s |
| **Library tiles that load (top 24)** | **0 of 24** | **24 of 24** |
| Broken public-URL-on-private tiles | 66 | **0** |
| **Background picker (59 tiles)** | **563.2 MB** | **1.42 MB** |
| Assets with a thumbnail | 0 of 135 | **133 of 135** |
| Detail drawer | 9–15 MB | ~150 kB |
| Eager prefetch on mount | 331 MB | 0 (then ~1.2 MB, idle) |
| Library JSON metadata | 856 kB | 141 kB |
| Dashboard first paint | 12–17 s | ~1 s |

Backfill: **133 assets, 636.1 MB → 2.74 MB (232× smaller, avg 20 kB).**

Verification: **185 backend tests pass**, TypeScript compiles clean, 26 files changed,
1,576 insertions.

## 9.2 How I measured — and its limits

Partway through, your backend stopped responding. With `AUTO_RELOAD=1` and ~12 files being
edited, it reloaded repeatedly, and this app's `run.py` uses a configuration the review had
already flagged as fragile across reload-time restarts. **I did not restart it** — that's your
call, and killing a server mid-work can lose state.

Instead I measured **in-process**: importing the backend as a library and calling its
endpoints directly, without a network server. Same code, same database, same storage.

**Two honest caveats:**

1. In-process runs pay connection-pool startup each time, so my "cold" numbers are
   pessimistic; a long-running server won't pay that.
2. I forced `DB_POOL_MIN=4` for the steady-state numbers. Your `.env` has `1`, so your first
   burst after idle will be slower until connections open.

**Your backend still needs a manual restart to pick any of this up.**

---

# Part 10 — What we did NOT do

Restraint mattered here. A review pass explicitly checked every proposed fix for whether it
was *safe*, and killed several that looked reasonable.

Of 79 findings, the impact review upheld 37, marked **18 as overstated**, and folded 24 as
duplicates. **Knowing what isn't the problem is as valuable as knowing what is.**

| Not done | Why |
|---|---|
| Make `producer-assets` public | 1.5 GB including students' faces, at guessable URLs, **irreversibly**. |
| Move the 203 existing objects to the public bucket | Same privacy issue, plus it races live background workers and could break Instagram scheduling. Unnecessary once signed transforms worked. |
| **Move NEW generation output to the public bucket** | This was *in* the plan as a tidy-up worth "0 MB today". I **declined**: `template_fill` output is a student's face on a template. Publishing those at guessable URLs undoes the entire reason we chose signed transforms. **Your call if you disagree.** |
| Delete the pool's `check` setting | It looks like free savings, but it's what retires connections Supabase killed while idle, and the driver does **not** retry. We took the win from `autocommit` instead. |
| Make `autocommit` the global default | 418 call sites, many writers depending on atomicity. Every missed one becomes a silent partial-write bug. |
| App-wide gzip | Verified: no media exclusion, no Range handling → would **break video seeking** (§7.4). |
| Alias the missing animations route | Cross-table ID collision → would serve the wrong person's video (§8, 3.2). |
| Blanket `immutable` on the private bucket | The Talking Head keyframe path is overwritten on regenerate; immutable would pin stale art for a year. |
| 7-day thumbnail tokens | An auth-free bearer link to a private photo, sitting in your HTML. 24 h. |
| Cap the AI model's input image "for caching" | That **changes model input**, therefore output quality and identity fidelity. A performance tweak must never silently change what the AI sees. |
| Add more server workers instead of fixing blocking calls | Wouldn't compose with this app's runner, and would split the URL cache N ways. Fix the blocking, don't hide it. |
| Rewrite `/library` as one big query | Three different row shapes and status vocabularies. A day of risk for the last ~0.2 s. |

---

# Part 11 — The region question

## 11.1 It cannot be moved in place — by anyone

Supabase's own documentation:

> *"Each Supabase project is provisioned on hardware in the chosen region, so it is bound to
> a region at the infrastructure level."*

Changing region means **creating a new project and migrating into it**. There's no in-place
move. (Project *transfer* moves between organisations, not regions — easy to confuse.)

I also confirmed the MCP connection I have has **no project-management capability** — only
schema, SQL, functions, logs, and docs. So I can't do it even in principle.

## 11.2 What it would involve

| Piece | Size | Difficulty |
|---|---|---|
| Postgres schema + data | **104 MB**, 71 tables, PG 17.6 | **Easy** — ~15 min. On Pro with physical backups, "Restore to another project" does it for you. |
| Storage objects | **2,204 MB**, 938 objects | **The real work.** `pg_dump` does *not* copy files — a separate bucket-to-bucket copy, with `storage.objects` rows kept in sync. |
| Credentials | Every teammate + prod | New project ref, URL, keys. `.env` doesn't travel with git, so every machine changes in lockstep. |

## 11.3 The alternative: read replicas

A **read replica** is a second, read-only copy of your database kept in sync, which can live
in a different region. Available on Pro. Three catches:

1. **Replication is asynchronous.** Your creator flows write a row then immediately read it
   back. A replica can legitimately answer "not found." Those paths must stay on the primary.
2. **Cost is a full second compute instance** plus 1.25× disk, and replicas aren't covered by
   the spend cap.
3. **Storage and Auth never use a replica** — documented. Since your storage CDN is already
   fast (81 ms), a replica only addresses the Postgres half.

## 11.4 My recommendation

**Don't move yet.** We took `/library` from ~16 round trips to a handful; the remaining Tokyo
penalty is roughly 0.5 s. A move would take that to ~0.15 s.

Still real — but you'd now be doing a credential-rotating migration for a *known* ~400 ms
rather than a guessed 15 s. That's a much better decision to make, and it's the same decision
in a month.

---

# Part 12 — Still open

Things I deliberately left for you, because they're not mine to decide:

| Item | The tradeoff |
|---|---|
| **`DB_POOL_MIN` 1 → 4** | Removes ~1.1 s from the first request after idle, but permanently holds Supabase pooler slots your **teammates share**. Needs a team decision. |
| **Pagination** (`limit` 200 → 48) | Measured 3.26 s → 1.87 s. But it's a **product** change: filter-chip counts get *more* wrong under paging unless a server-side counts block ships in the same commit. |
| **Database indexes** | **0 ms benefit today** at your row counts (395 and 88 rows). A growth hedge, and DDL on a shared database. |
| **Public bucket for new generation output** | Declined on privacy grounds (Part 10). Say the word if you want it. |
| **Region move** | Part 11. |
| **Commit the work** | 26 files, 1,576 insertions, nothing staged. |
| **Restart the backend** | Required before you'll see any of this in the browser. |

Also worth knowing: `run_tests.sh` is **deleted** in your working tree. The other root
markdown files were moved into `docs/`, but that script wasn't moved — it's just gone. I used
`pytest` directly instead. Say the word and I'll restore it.

---

# Part 13 — Glossary

| Term | Meaning |
|---|---|
| **API** | The set of questions a backend will answer |
| **autocommit** | Run a statement without a transaction — saves 2 round trips on reads |
| **backfill** | A one-off job applying a new rule to data that already exists |
| **bandwidth** | How much data per second. Fix by sending fewer bytes |
| **bearer token** | A credential where merely holding it grants access |
| **bucket** | A named container for files in object storage |
| **cache** | A saved copy kept to avoid re-fetching |
| **cache key** | What identifies a cached item — for the browser, **the exact URL** |
| **CDN** | A worldwide network of servers holding copies of files, so users fetch from nearby |
| **CDN URL** | A URL pointing at the CDN rather than your backend |
| **derivative** | Any file generated from another (thumbnail, preview) |
| **endpoint** | One URL your API answers |
| **event loop** | The machinery that lets one async worker juggle many requests |
| **blocking** | Slow work that stops the event loop, freezing every other request |
| **immutable** | A cache instruction meaning "never even ask if this changed" |
| **latency** | How long until the first byte. Fix by making fewer requests, or moving closer |
| **lazy loading** | Deferring an image download until it's near the viewport |
| **payload** | The actual data in a response |
| **pool (connection)** | Reused open database connections, so you don't pay setup each time |
| **preload** | How much of a video the browser fetches before you interact |
| **private bucket** | Files need a signed URL; a plain URL is rejected |
| **proxy** | Routing a file through your backend instead of straight from the CDN |
| **public bucket** | Anyone with the URL can fetch the file |
| **Range request** | Asking for a byte range — how video seeking works |
| **read time** | Computing a value fresh on every request |
| **write time** | Computing a value once when data is created, and storing it |
| **round trip** | One request-and-response. The unit that matters for latency |
| **route** | The backend code that answers one endpoint |
| **semaphore** | A counter limiting how many things run at once |
| **service** | Reusable backend logic that isn't a route |
| **signed URL** | A URL with a token proving you may read one private file |
| **thumbnail** | A small copy of an image, for grids |
| **transaction** | A group of SQL statements that all succeed or all fail |
| **TTL** | Time To Live — how long something stays valid |
| **WebP** | An image format much smaller than PNG/JPEG |

---

# Part 14 — Verify it yourself

Read-only. Nothing here changes anything.

**Which buckets are actually public?** (the predicate the whole fix rests on)
```bash
SU=$(grep -m1 '^SUPABASE_URL=' .env | cut -d= -f2- | tr -d '"\r')
SK=$(grep -m1 '^SUPABASE_SERVICE_ROLE_KEY=' .env | cut -d= -f2- | tr -d '"\r')
curl -s "$SU/storage/v1/bucket" -H "Authorization: Bearer $SK" -H "apikey: $SK" \
  | python3 -c 'import sys,json;[print("%-24s public=%s"%(b["id"],b["public"])) for b in json.load(sys.stdin)]'
# producer-assets MUST be public=False
```

**Prove the old bug** — a public URL on the private bucket:
```bash
curl -s -o /dev/null -w '%{http_code}\n' \
  "$SU/storage/v1/render/image/public/producer-assets/yas/generations/746.png?width=480"
# 400 — this is what every "no preview" tile was hitting
```

**See the crop bug for yourself:**
```bash
for Q in "width=480" "width=480&height=480&resize=contain"; do
  curl -s -o /tmp/t.bin -H 'Accept: image/webp,*/*' \
    "$SU/storage/v1/render/image/public/producer-media/poster/412.png?$Q&quality=70"
  python3 -c "from PIL import Image; print('$Q ->', Image.open('/tmp/t.bin').size)"
done
# width-only  -> (480, 1024)  CROPPED
# with height -> (480, 480)   correct
```

**How long is one database round trip?**
```bash
python3 - <<'PY'
import socket, time
h = "aws-1-ap-northeast-1.pooler.supabase.com"
t = time.time(); s = socket.create_connection((h, 5432), timeout=10); s.close()
print(f"{(time.time()-t)*1000:.0f} ms to Tokyo")
PY
```

**Is the query the bottleneck?** Run in the Supabase SQL editor:
```sql
EXPLAIN ANALYZE SELECT * FROM yas_generations ORDER BY created_at DESC LIMIT 200;
-- Execution Time will be single-digit milliseconds
```

**Did the backfill work?**
```sql
SELECT count(*) FILTER (WHERE attributes->>'_thumb_path' IS NOT NULL) AS with_thumb,
       count(*) AS total_images
  FROM producer_assets
 WHERE asset_kind = 'image'
   AND storage_bucket IS NOT NULL;
```

**Run the tests:**
```bash
cd backend && python3 -m pytest tests/ -q          # 185 pass
cd frontend && npx tsc --noEmit                   # silent = clean
```

**In the browser:** DevTools → Network → filter **Img** → clear cache → open
**Assets → Background**. Transferred should be **~1.4 MB**, per-tile ~24 kB,
`content-type: image/webp`. Reload *without* clearing: near-zero.

---

# Appendix A — File by file

**Backend (10 files)**

| File | What changed |
|---|---|
| `services/asset_storage.py` | **+350** — `bucket_is_public()`, batch signing, signed transforms, transform-aware cache key, `Cache-Control` on upload, evict-on-overwrite |
| `routers/yas_producer.py` | **+452** — the gate fix, thumbnail dimensions, signed-thumb pass, TH tiles routed through the shared builder, metadata projection, autocommit, parallel reads, media-less rows excluded, redirect TTLs, the missing animations route, buster fix |
| `routers/producer_assets.py` | **+235** — `thumb_url` in the list, `preview_url` for detail, thumbnail generation + storage, redirect TTL |
| `services/instagram_publish.py` | **+148** — both fan-outs parallelised, insights memoised, throttle-aware |
| `services/yas_video_editor.py` | **+56** — five blocking calls offloaded, duplicate query removed |
| `db.py` | `ping()` autocommit |
| `main.py` | Warm the bucket-publicness map at startup (read-only — never writes publicness) |
| `services/yas_local_store.py`, `yas_animation_store.py` | autocommit on reads |
| `tests/test_media_caching.py` | **+110** — updated contracts, plus a regression guard so a private bucket can never emit a public URL again |

**Frontend (16 files)**

| File | What changed |
|---|---|
| `components/Thumb.tsx` | **NEW** — shimmer, fade-in, two-step error fallback |
| `pages/Dashboard.tsx` | **+125** — un-gated, per-section states, video hygiene, empty states, shared store |
| `lib/store.ts` | **+74** — per-params library cache, `force` flag, delete-sync across cached sets |
| `lib/prefetchAssets.ts` | **+81** — rewritten: thumbnails, byte budget, idle |
| `components/yas/YasLibrary.tsx` | **+49** — video hygiene, three empty states |
| `pages/Assets.tsx` | **+27** — thumbnails via `<Thumb>`, refresh fix |
| `lib/api.ts` | **+16** — `thumbUrlFor()` |
| `lib/types.ts` | `thumb_url`, `preview_url` |
| `index.html` | Non-blocking fonts |
| 7 creator/picker components | Point at thumbnails; lazy-load hidden strips |

---

# Appendix B — Lessons

The transferable parts, worth more than the specific fixes:

1. **A silent fallback turns a crash into a mystery.** `onError` → "no preview" made a hard
   HTTP 400 look like slowness for weeks. Failure states should be *distinguishable*: loading,
   empty, and broken are three different things.

2. **A boolean cannot stand in for a per-object property.** `producer_media_public` described
   one bucket; the code applied it to any bucket. The correct predicate was queryable all
   along.

3. **Defaults hide decisions.** A default bucket that was correct for uploads was wrong for
   generated output. Nothing failed at write time; the bug surfaced elsewhere, much later.

4. **When latency is high, count round trips — not milliseconds of compute.** A 3.9 ms query
   inside a 15-second endpoint. Removing one round trip was worth 142× more than halving the
   query.

5. **Parallel work collapsed into one flag is sequential work.** `Promise.allSettled` used
   correctly, then undone by a single shared `loading` boolean.

6. **A comment asserting an invariant no code enforces is worse than no comment.** The
   prefetch's docstring is why nobody re-checked whether it helped.

7. **One column, one meaning.** `status='ready'` meant "script ready" in one place and
   "artifact ready" in another. That ambiguity created 14 phantom rows.

8. **Measure before and after, on real data.** Every number here was measured, and two of my
   own early claims were wrong until I checked (the "excellent" transform was cropping; the
   first signing design was 26 s).

9. **The cache key is the whole URL.** Two spellings of the same timestamp meant every image
   was cached twice.

10. **Make the safe direction the fallback.** Unknown bucket ⇒ private ⇒ proxy. Slow, never
    broken. Every fallback in this work degrades to "slow", never to "blank".

11. **Fast and correct sometimes aren't a tradeoff — sometimes it's a false choice.** I told
    you the last 10× of thumbnail savings wasn't worth 26 s of cold latency. That framing was
    wrong: resizing once at upload got both.
