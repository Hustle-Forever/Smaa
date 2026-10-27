"""FastAPI entrypoint.

Wires logging, opens the DB pool, mounts routers, sets CORS for the frontend.

Run with:
    cd backend
    uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
or
    .\\start.ps1
"""
from __future__ import annotations

import asyncio
import logging
import sys
from contextlib import asynccontextmanager

# psycopg async + Windows: ProactorEventLoop is incompatible. Force Selector
# *before* uvicorn or any asyncio code creates an event loop.
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from . import access
from .config import config
from .db import cleanup_orphan_runs, close_pool, get_conn, open_pool, ping
from .logging_config import setup_logging
from .routers import (
    access as access_router,
    agents,
    agents_brand_doc,
    agents_brand_synth,
    agents_instagram,
    agents_producer,
    assets,
    brand,
    general_trends,
    campaigns,
    rebrand as rebrand_router,
    rebrand_harness as rebrand_harness_router,
    health,
    producer_assets,
    reports as reports_router,
    scheduler as scheduler_router,
    trend_radar as trend_radar_router,
    settings,
    trend_finder,
    website,
    yas_producer,
)
from .scheduler import setup_scheduler, shutdown_scheduler
from .workers import (
    brand_video_poller,
    campaign_poller,
    composition_poller,
    general_trends_poller,
    news_poller,
    scheduled_post_poller,
    trend_scan_poller,
    veo_poller,
    yas_animations_poller,
    yas_general_video_poller,
    yas_local_animation_poller,
    yas_th_composition_poller,
    yas_th_v2_composition_poller,
    yas_video_edit_ai_poller,
    yas_video_edit_composition_poller,
)
from .workers.runner import get_registry

setup_logging()
log = logging.getLogger("archivist.main")


@asynccontextmanager
async def lifespan(_: FastAPI):
    log.info(
        "Starting Studio backend (target=%s, supabase=%s, CLIP=%s)",
        config.target_url, config.supabase_project_ref, config.use_clip,
    )
    await open_pool()
    if not await ping():
        log.error("DB ping failed at startup; service will keep running but expect 5xx errors")
    else:
        # Any run still 'started' is a zombie from a prior crash/restart.
        await cleanup_orphan_runs()
    # Touch the registry so it exists at startup
    get_registry()
    # Brand docs can now live in Supabase — ensure the column exists (idempotent).
    try:
        async with get_conn() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "ALTER TABLE brand_documents ADD COLUMN IF NOT EXISTS storage_bucket TEXT"
                )
    except Exception:
        log.exception("brand_documents storage_bucket migration failed (non-fatal)")
    # Allow doc_kind='video' (brand-video ingest). Idempotently widen the CHECK
    # constraint — discover its current name (auto-generated) so we don't depend
    # on a hard-coded constraint name, then recreate it including 'video'.
    try:
        async with get_conn() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    DO $$
                    DECLARE cname text;
                    BEGIN
                      SELECT conname INTO cname FROM pg_constraint
                       WHERE conrelid='brand_documents'::regclass AND contype='c'
                         AND pg_get_constraintdef(oid) LIKE '%doc_kind%';
                      IF cname IS NOT NULL THEN
                        EXECUTE format('ALTER TABLE brand_documents DROP CONSTRAINT %I', cname);
                      END IF;
                      ALTER TABLE brand_documents ADD CONSTRAINT brand_documents_doc_kind_check
                        CHECK (doc_kind IN ('pdf','docx','text','image','free_text','video'));
                    END $$;
                    """
                )
    except Exception:
        log.exception("brand_documents doc_kind 'video' migration failed (non-fatal)")
    # Object storage (MinIO/S3, Supabase or local disk, per STORAGE_BACKEND):
    # create the buckets this app writes to and warm the bucket-publicness map
    # the sync URL builders read. Before the pollers start, so their first
    # uploads find the buckets in place. Idempotent and never raises: a store
    # that is down only means uploads fall back to local disk for now.
    from .services import asset_storage
    await asset_storage.bootstrap_storage()
    # Arm the in-process scheduler (no-op if AUTO_CRAWL_CRON is empty).
    #
    # Behind POLLERS_ENABLED for the same reason as the pollers below: with
    # AUTO_CRAWL_CRON set this fires a real crawl on a timer, so a laptop left
    # on overnight would run the nightly job a second time against the
    # production database, concurrently with the server's own.
    if config.pollers_enabled:
        await setup_scheduler()
    # ------------------------------------------------------------------
    # Background pollers
    # ------------------------------------------------------------------
    # One switch in front of all of them, because they are workers rather than
    # observers: they write rows, email reminders and spend model credits. With
    # localhost pointed at the production database, a dev machine running this
    # file is a SECOND worker on the same rows as the deployed server, and only
    # brand_video_poller and campaign_store claim work with FOR UPDATE SKIP
    # LOCKED. The rest would simply do it twice.
    #
    # Defaults to on, so the server's behaviour is unchanged and a missing
    # variable can never silently stop unattended work. Turn it OFF in a local
    # .env. The matching `stop()` calls after the yield are safe either way:
    # each poller's stop() is a no-op when its task was never started.
    if not config.pollers_enabled:
        log.warning(
            "POLLERS_ENABLED is false: every background poller is disabled. "
            "Scheduled work (Veo, compositions, campaigns, trend scans, news, "
            "post reminders) will NOT run in this process."
        )
    # Background poller for Veo long-running operations (Producer Agent).
    if config.pollers_enabled and config.google_cloud_project:
        veo_poller.start()
        yas_animations_poller.start()
        yas_local_animation_poller.start()
        # General AI Video tool — finishes Veo and Seedance clips (own store/table).
        yas_general_video_poller.start()
        # Brand-video ingest analyses uploaded brand videos via Gemini, so it
        # needs Vertex too — gate it on the same flag.
        brand_video_poller.start()
        # Autonomous campaigns ("Producer" tab): plans each week, generates the
        # posters, captions them, then STOPS for human approval. It also books
        # approved posts, but never publishes — a person still presses Post.
        # Needs Vertex for image generation, hence this block.
        campaign_poller.start()
    elif config.pollers_enabled:
        log.info("GOOGLE_CLOUD_PROJECT not set — Veo + yas-animations + brand-video "
                 "+ campaign pollers disabled")
    if config.pollers_enabled:
        # Background poller for Remotion compose jobs (sidecar service on :3001).
        composition_poller.start()
        # Background poller for Yas Talking Head compositions (also Remotion).
        yas_th_composition_poller.start()
        # Background poller for Yas Talking Head v2 compositions (isolated copy).
        yas_th_v2_composition_poller.start()
        # Background poller for Yas Video-Editor exports (Remotion `video-edit`).
        yas_video_edit_composition_poller.start()
        # Background poller that finishes Suno music jobs for the AI video editor.
        yas_video_edit_ai_poller.start()
        # Background poller that emails reminders for scheduled Instagram posts
        # (Producer Roadmap, Step 4). No-op if email isn't configured.
        scheduled_post_poller.start()
        # Background worker that runs queued Trend Finder competitor scans (and
        # queues one itself every TREND_AUTO_SCAN_HOURS). The queue is a DB
        # table, so a scan interrupted by a restart resumes here.
        trend_scan_poller.start()
        # Background worker that runs queued General Trends refreshes (and queues
        # one itself every GENERAL_TRENDS_REFRESH_HOURS). Same DB-queue shape.
        general_trends_poller.start()
        # Background collector for AI News. Unattended: each source carries its own
        # next_fetch_at, so this needs no trigger and resumes cleanly after a
        # restart. Deliberately NOT on the trend_scans queue — see the module.
        news_poller.start()
    # Verify Instagram token at startup (best-effort, never blocks boot).
    if config.instagram_access_token and config.instagram_account_id:
        try:
            from .services.instagram_client import ping as ig_ping
            res = await ig_ping()
            log.info("Instagram token OK (@%s)", res.get("username"))
        except Exception as e:
            log.warning(
                "Instagram token check failed (%s) — the instagram-ingest agent "
                "will return 4xx until this is resolved", e,
            )
    else:
        log.info("Instagram credentials not set — instagram-ingest agent disabled")
    yield
    await campaign_poller.stop()
    await brand_video_poller.stop()
    await news_poller.stop()
    await general_trends_poller.stop()
    await trend_scan_poller.stop()
    await scheduled_post_poller.stop()
    await yas_video_edit_ai_poller.stop()
    await yas_video_edit_composition_poller.stop()
    await yas_th_v2_composition_poller.stop()
    await yas_th_composition_poller.stop()
    await composition_poller.stop()
    await yas_general_video_poller.stop()
    await yas_local_animation_poller.stop()
    await yas_animations_poller.stop()
    await veo_poller.stop()
    await shutdown_scheduler()
    await close_pool()
    log.info("Studio backend shut down")


app = FastAPI(
    title="42 Abu Dhabi Studio API",
    version="0.1.0",
    description=(
        "Backend for 42 Abu Dhabi Studio — brand-aware content automation. "
        "Phase 1 owns the website-ingest, brand-doc-ingest, and brand-synthesizer "
        "agents (sitemap → text → image → video → OpenAI embeddings → brand "
        "synthesis). Future phases: video generation, automated posting."
    ),
    lifespan=lifespan,
)

# CORS for the Next.js frontend (or any whitelisted origin)
app.add_middleware(
    CORSMiddleware,
    allow_origins=config.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# --- Public-preview access gate (accounts) ---------------------------------
# When APP_ACCESS_PASSWORD is set the gate is armed: every /api/* request
# (except health + the auth endpoints) must carry the cookie that signing in
# sets, otherwise 401 BEFORE any paid OpenAI/Vertex call runs. Unset = gate
# disarmed (local dev). The accounts themselves live in app/access.py.
_GATE_PUBLIC_PREFIXES = ("/api/health", "/api/auth/")


@app.middleware("http")
async def access_gate(request, call_next):
    if request.method != "OPTIONS" and access.enabled():
        path = request.url.path
        if path.startswith("/api/") and not path.startswith(_GATE_PUBLIC_PREFIXES):
            if not access.token_ok(request.cookies.get(access.COOKIE_NAME)):
                return JSONResponse(
                    {"detail": "Access denied. Sign in to continue."},
                    status_code=401,
                )
    return await call_next(request)


# Routers
app.include_router(access_router.router)      # sign-in endpoints (account gate)
app.include_router(health.router)
app.include_router(settings.router)
app.include_router(website.router)
app.include_router(assets.router)
app.include_router(brand.router)
app.include_router(agents.router)            # Agent 1: website-ingest
app.include_router(agents_brand_doc.router)  # Agent 2: brand-doc-ingest
app.include_router(agents_brand_synth.router)  # Agent 3: brand-synthesizer
app.include_router(agents_producer.router)   # Agent 4: producer (Veo 3.1 video)
app.include_router(agents_instagram.router)  # Agent 5: instagram-ingest (Business Discovery)
app.include_router(producer_assets.router)   # v3: manual Assets Library
app.include_router(campaigns.router)         # autonomous campaigns ("Producer" tab)
app.include_router(scheduler_router.router)  # /api/scheduler/status
app.include_router(trend_finder.router)      # Trend Finder: competitor scanning
app.include_router(general_trends.router)    # Trend Finder: general internet trends
app.include_router(trend_radar_router.router)  # Trend Radar: daily AI + coding top 10
app.include_router(reports_router.router)     # Social-listening report (JSON + PDF)
app.include_router(yas_producer.router)      # Yas Producer chat
app.include_router(rebrand_router.router)    # TH v2 Rebrand experiment (Omni)
app.include_router(rebrand_harness_router.router)  # Rebrand harness: recipes of elements


@app.get("/", include_in_schema=False)
async def index() -> dict:
    """Convenience root — point your browser at /docs for OpenAPI."""
    return {
        "service": "archivist-backend",
        "version": app.version,
        "docs": "/docs",
        "health": "/api/health",
    }
