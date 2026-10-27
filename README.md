# 42 Studio

The in-house content studio for **42 Abu Dhabi**, a tuition-free coding school.

It runs the whole social-media loop in one application: work out what is worth
posting, make the thing, review it, schedule it, and publish it to Instagram.
Underneath it all sits a synthesized model of the school's brand, so what comes
out sounds like 42 rather than like a generic content bot.

Built bottom-up on FastAPI, React and Postgres. No n8n, no Zapier, no
third-party orchestrator.

Live at **https://42studio.fit**.

```
    FIND                    MAKE                 REVIEW        SCHEDULE     PUBLISH
 ┌──────────┐      ┌──────────────────┐      ┌─────────┐   ┌──────────┐  ┌─────────┐
 │ SMAA     │─────▶│ Ideation         │─────▶│ Preview │──▶│ Calendar │─▶│Instagram│
 │ intel    │      │ 7 creation tools │      │ + edits │   │          │  │  Graph  │
 └──────────┘      └──────────────────┘      └─────────┘   └──────────┘  └─────────┘
      ▲                     ▲                      ▲                          │
      │                     │              a human presses Post ──────────────┘
 competitors +         Brand profile
 AI news feed         (voice, palette,
                       dos and don'ts)
```

---

## What is in the app

Eight tabs in the sidebar. The two marked beta are new, not unfinished.

| Tab | What it is for |
|---|---|
| **Dashboard** | Counts, spend, recent work, quick actions |
| **Brand** | The school's brand profile. A tabbed workspace: Profile, Upload, Synthesizer, Instagram |
| **Assets** | Everything collected or generated: posters, characters, backgrounds, clips |
| **Ideation** | The hands-on studio. Seven creation tools, each running the five-stage roadmap |
| **Producer** (beta) | The autonomous studio. Campaigns that generate a run of content on a schedule |
| **SMAA intelligence** (beta) | What to post about: competitor posts, AI news, general trends, reports |
| **Library** | Everything ever produced, filterable by tool |
| **Settings** | One home for everything you change: your brand, what news to look for, the accounts we watch, and whether posting and email are connected |

### Brand is the foundation

Three ingest agents build a picture of the school and one synthesizer turns it
into a profile that everything downstream reads:

| Agent | What it does |
|---|---|
| `website-ingest` | Crawls `42abudhabi.ae`, extracts text, images and video, summarizes and embeds |
| `brand-doc-ingest` | Owner uploads a PDF, DOCX, image or text file, it is extracted and embedded |
| `brand-synthesizer` | Re-runs synthesis across the whole corpus and writes the brand profile: voice, palette, fonts, dos and don'ts |
| `instagram-ingest` | Pulls the school's own Instagram presence via Business Discovery |

### SMAA intelligence decides what is worth making

- **Competitors.** Add accounts, scan them, and every discovered post is scored
  and ranked so you can see what is actually working. Themes group the topics.
- **Industry Signal.** One grounded Gemini web search, run when you press
  **Find stories**, against a brief you write in your own words. Every candidate story faces one test:
  *would somebody learning to code, studying tech, or building software care
  about this?* Survivors are rewritten into plain English, scored, and filed.
  Anything dull, technical, duplicated or stale is dropped before it is ever
  written to a row.
- **Reports.** A two-page brief drawn from the real data, produced as a document
  rather than as the end of a form.

Anything in either feed can be pushed straight into a creation tool as a
starting idea, prefilled by the model.

### Ideation makes the thing

Seven tools, and whatever you pick moves through the same five stages:

```
Ideation ──▶ Editing ──▶ Finalization ──▶ Scheduling ──▶ Approval
```

| Tool | What comes out |
|---|---|
| Poster | A designed picture, no people in it |
| Animation | A poster that moves, a few seconds long |
| Poster + Human | A designed picture with people in it |
| Human Character | A person you can reuse as a presenter |
| Human Element Video | A short video of someone talking |
| General video | AI video, or a montage built from your own clips |
| Video Editor | A multi-track timeline for cutting and joining clips |

Every visited tool stays mounted while you work, so switching tools or stepping
back never resets a generation in flight.

### Producer runs it without you

Autonomous campaigns, with **two human gates**. Gate one approves the content.
Gate two is a person pressing Post. Publishing never happens on a timer.

---

## Architecture

Three containers behind Caddy, talking to a remote Supabase Postgres. There is
no database container.

```
Internet ─443/80─▶ web (Caddy: static SPA + TLS + /api reverse proxy)
                     ├─ /api/* ──▶ backend   (FastAPI, :8000)
                     └ backend ──▶ remotion  (:3001, internal only, no auth)

        backend ⇄ remotion share one volume at /app/backend/data
        backend ──▶ Supabase Postgres + Vertex AI + Instagram Graph
```

**One public origin.** The frontend calls the API with relative `/api` paths, so
the API must live under the same hostname. Do not create `api.` or `remotion.`
subdomains. Remotion is reachable only on the private Docker network, because it
serves and renders files and has no authentication.

| Service | Tech | Port | Role |
|---|---|---|---|
| **backend** | FastAPI, Python 3.12, psycopg-pool async, pgvector | `:8000` | 280 API routes, all agents, DB access, 15 background pollers |
| **frontend** | Vite, React 18, TypeScript, Tailwind v3 | `:4242` | The whole studio UI |
| **services/remotion** | Express 4, Remotion 4, Node 22 | `:3001` | Renders React video compositions, stitches clips into final MP4s |

### Almost nothing is synchronous

This is the defining property of the codebase. Generating video takes minutes,
so the backend runs **15 background pollers** inside the FastAPI lifespan, each
guarded separately so one failing stage cannot stop the others:

| Poller | Watches |
|---|---|
| `veo_poller`, `yas_animations_poller`, `yas_local_animation_poller`, `yas_general_video_poller` | Veo long-running operations, downloading finished MP4s from GCS |
| `composition_poller`, `yas_th_composition_poller`, `yas_th_v2_composition_poller`, `yas_video_edit_composition_poller` | Remotion render jobs |
| `yas_video_edit_ai_poller` | Media provider jobs for the video-editor agent |
| `campaign_poller` | Autonomous campaigns, one tick at a time |
| `news_poller` | The Industry Signal morning run |
| `trend_scan_poller`, `general_trends_poller` | Queued competitor and general-trend scans |
| `scheduled_post_poller` | Reminder dispatch for scheduled posts |
| `brand_video_poller` | Uploaded brand videos into the corpus |

`POLLERS_ENABLED=false` turns every one of them off. Useful when a second
instance is pointed at the same database.

### One publisher

There is exactly one piece of code in the repo that can publish to Instagram
(`services/instagram_publish.py`), reached through one endpoint. Everything else
routes to it. A double post is structurally impossible rather than defended
against, and no new code may grow a second Instagram call.

---

## External services

| Service | Used for | Auth |
|---|---|---|
| **Vertex AI** | Gemini 2.5 Pro and Flash for text and judgement, Veo 3.1 for video, Nano Banana (`gemini-2.5-flash-image`) for images, `gemini-embedding-001` for embeddings | ADC via `gcloud auth application-default login` |
| **GCS** `studio-veo-out` | Veo writes MP4 output, the backend downloads it | ADC |
| **Supabase** | Postgres 17 with pgvector, plus Storage buckets for media | Pooler connection, service-role key |
| **Instagram Graph API** | Publishing, and Business Discovery for competitor scanning | Long-lived tokens |
| **OpenAI** | The three brand ingest agents only | `OPENAI_API_KEY` |
| **Anthropic / Claude** | The video-editor agent, when `VE_AGENT_PROVIDER` selects it | API key or Vertex |
| **ElevenLabs** | Voice | API key |
| **Suno** | Music | API key |
| **CLIP** ViT-B/32 | Visual similarity, runs locally | none |

Vertex calls go through one client that gives every request retry, multi-region
failover on 429, a concurrency semaphore and a hard USD budget meter.

---

## Data

Supabase Postgres 17 with pgvector. All tables RLS-enabled, the backend
connects as `postgres` and bypasses.

| Group | Tables |
|---|---|
| Website and assets | `scrape_runs`, `website_pages`, `text_chunks`, `page_links`, `assets`, `image_embeddings`, `video_frames`, `video_transcripts`, `video_summaries` |
| Brand | `brand_documents`, `brand_profile_runs`, `brand_profile_artifacts`, `brand_profile_settings` |
| Producer (original) | `producer_runs`, `producer_ideas`, `producer_scripts`, `producer_videos`, `producer_references`, `producer_feedback`, `producer_compositions` |
| Ideation studio | `yas_poster_generations`, `yas_animation_generations`, `yas_general_video_generations`, `producer_roadmap`, `popup_recipes`, `edit_recipes`, `ve_voice_clone` |
| Campaigns | `producer_campaigns`, `producer_campaign_items` |
| Intelligence | `trend_competitors`, `trend_posts`, `trend_scans`, `general_trends`, `general_trend_runs`, `general_trend_sightings`, `news_config`, `news_runs` |
| Publishing | `scheduled_instagram_posts` |

Embeddings: OpenAI `text-embedding-3-small` (1536d) for website text and brand
docs, Vertex `gemini-embedding-001` (768d) for production rows, CLIP (512d) for
images.

---

## Running it locally

Three processes. Start them in any order.

```bash
# 1. backend
cd backend
python -m venv .venv && source .venv/bin/activate   # Python 3.12
pip install -r requirements.txt
cp .env.example .env        # then fill it in
python run.py               # :8000

# 2. remotion
cd services/remotion
npm install
npm run dev                 # :3001

# 3. frontend
cd frontend
npm install
npm run dev                 # :4242
```

Open http://127.0.0.1:4242. The Vite dev server proxies `/api` to the backend,
reading the port from the same `.env` the backend reads, so the two cannot drift
apart.

Set `AUTO_RELOAD=1` in `backend/.env` for Python hot reload. The trade-off is
that the DB pool is rebuilt on every save, costing a few seconds.

`run.py` exists rather than a bare `uvicorn` command because psycopg async
refuses to run on the Windows ProactorEventLoop, and uvicorn's own `loop`
option overwrites the policy. The launcher sets the Selector policy first.

For the VPS deployment, see [`docs/DEPLOY.md`](docs/DEPLOY.md).

---

## Configuration

Everything lives in `backend/.env`. Start from `backend/.env.example`, which is
grouped and commented. The variables that decide the most:

```
DB_HOST, DB_USER, DB_PASSWORD, DB_NAME     Supabase pooler connection
GOOGLE_CLOUD_PROJECT, GOOGLE_CLOUD_LOCATION
VEO_MODEL, VEO_BUDGET_USD                  the shared Vertex spend cap
VERTEX_GEMINI_MODEL, VERTEX_GEMINI_FAST_MODEL, VERTEX_IMAGE_MODEL
SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY    media storage
INSTAGRAM_ACCESS_TOKEN, INSTAGRAM_ACCOUNT_ID, INSTAGRAM_PUBLISH_TOKEN
POLLERS_ENABLED                            every background worker, one switch
APP_ACCESS_PASSWORD                        arms the sign-in gate for public previews
AUTO_CRAWL_CRON, SCHEDULER_TIMEZONE
```

`DB_PORT` matters more than it looks. Port 6543 is Supabase's transaction
pooler, which does not support prepared statements, so they are disabled.

---

## Tests

```bash
cd backend && .venv/bin/python -m pytest tests/ -q
```

**1,598 tests**, roughly 26,000 lines of them, and no network or model calls in
any of them. Ten fail on purpose when `GENERAL_TRENDS_ENABLED=true`, which is
documented in commit `e1b9394`: they pin the behaviour of the stream while it is
paused, so they are supposed to go red the moment somebody un-pauses it.

Frontend:

```bash
cd frontend && npm run typecheck && npm run build
```

---

## Repo layout

```
42-studio/
├── backend/                    FastAPI, ~95k lines
│   ├── app/
│   │   ├── main.py             entrypoint, lifespan, poller startup
│   │   ├── config.py           every env-driven setting
│   │   ├── db.py               psycopg-pool, pgvector adapter
│   │   ├── routers/            18 routers, 280 endpoints
│   │   ├── agents/             website_ingest, brand_doc_ingest,
│   │   │                       brand_synthesizer, producer
│   │   ├── services/           vertex_client, instagram_publish, news_search,
│   │   │                       trend_store, asset_storage, clip_service, ...
│   │   ├── workers/            the 15 pollers
│   │   └── models/             Pydantic shapes
│   ├── tests/                  1,598 tests
│   └── run.py                  the uvicorn launcher
├── frontend/                   Vite + React, ~53k lines
│   └── src/
│       ├── pages/              one per tab
│       ├── components/
│       │   ├── trends/         SMAA intelligence
│       │   ├── yas/            the Ideation studio and video editor
│       │   ├── campaigns/      autonomous Producer
│       │   └── ui/             the shared primitives layer
│       └── lib/                api.ts, types.ts, stores, hooks
├── services/remotion/          Express + Remotion, video composition
├── design-system/              the written design system
├── docs/                       DEPLOY, TREND-FINDER, AI-NEWS, VIDEO-EDITOR-AI, ...
├── web/                        Caddyfile + Dockerfile for the edge container
└── docker-compose.yml
```

---

## House rules

Two conventions run through the whole codebase. Both are load-bearing.

**Every screen reads as plain English.** The people using this run a school's
social media, they do not operate a pipeline. No jargon, no model names, no
file concepts, no arrow notation on a button. If a fact cannot be said in
ordinary words, it does not go on the screen.

**No em dash.** The character `—` is not used anywhere: not in UI copy, code
comments, docs or commit messages. A comma, colon, period or parentheses
instead. Ranges take an en dash.
