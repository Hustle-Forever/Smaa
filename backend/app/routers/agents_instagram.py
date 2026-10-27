"""/api/agents/instagram-ingest/* — Instagram Business Discovery ingestion.

  POST   /api/agents/instagram-ingest/run                        kick off discovery/download/processing
  GET    /api/agents/instagram-ingest/runs                       list recent agent runs
  GET    /api/agents/instagram-ingest/runs/{id}                  one run + in-memory phase progress
  POST   /api/agents/instagram-ingest/runs/{id}/cancel
  GET    /api/agents/instagram-ingest/runs/{id}/events           SSE stream
  GET    /api/agents/instagram-ingest/posts                      list ingested posts (paginated)
  GET    /api/agents/instagram-ingest/posts/{id}                 single post + children + transcript + summary
  GET    /api/agents/instagram-ingest/posts/{id}/assets/{aid}/file  proxy local media file
  GET    /api/agents/instagram-ingest/health                     token check + account stats

Mirrors agents_brand_doc.py's shape so the existing RunPanel/SSE UX in the
frontend works without changes.
"""
from __future__ import annotations

import json
import logging
import mimetypes
from datetime import datetime
from pathlib import Path
from typing import Any, AsyncIterator, Optional

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sse_starlette.sse import EventSourceResponse

from ..agents.instagram_ingest.agent import (
    InstagramIngestAgent,
    RunOptions,
    request_cancel,
)
from ..config import config
from ..db import get_conn
from ..services.instagram_client import (
    InstagramAPIError,
    InstagramConfigError,
    ping as ig_ping,
)
from ..services.storage import resolve_local
from ..workers.runner import get_registry

log = logging.getLogger("archivist.router.instagram")

router = APIRouter(prefix="/api/agents/instagram-ingest", tags=["agents:instagram-ingest"])


# ---------------------------------------------------------------------------
# Request / Response models
# ---------------------------------------------------------------------------

class InstagramIngestRunRequest(BaseModel):
    target_username: Optional[str] = None
    max_posts: Optional[int] = Field(default=None, ge=1, le=10000)
    metrics_only: bool = False
    skip_phases: Optional[list[str]] = None
    force_re_download: bool = False
    # When true, the classify phase ignores its "skip already-classified"
    # gate and re-runs the (tightened) prompt across every downloaded post,
    # overwriting their existing content_category. Driven from the Sync UI's
    # "Re-classify existing posts" checkbox.
    force_reclassify: bool = False
    page_size: int = Field(default=100, ge=1, le=100)


class InstagramIngestRunResponse(BaseModel):
    run_id: int
    status: str


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _serialise(v: Any) -> Any:
    if isinstance(v, datetime):
        return v.isoformat()
    return v


def _row(row: dict | None) -> dict | None:
    return {k: _serialise(v) for k, v in row.items()} if row else None


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.post("/run", response_model=InstagramIngestRunResponse)
async def run_agent(body: InstagramIngestRunRequest) -> InstagramIngestRunResponse:
    """Kick off an Instagram ingest run.

    With no body, runs a full sync using the env-configured target username.
    Use `metrics_only=true` for a cheap engagement refresh.
    """
    # Surface missing creds with a clear 400 instead of silently failing later
    if not config.instagram_access_token or not config.instagram_account_id:
        raise HTTPException(
            status_code=400,
            detail=(
                "Instagram credentials not configured. Set INSTAGRAM_ACCESS_TOKEN "
                "and INSTAGRAM_ACCOUNT_ID in backend/.env (use a long-lived "
                "60-day token from /oauth/access_token?grant_type=ig_exchange_token)."
            ),
        )

    options = RunOptions(
        target_username=body.target_username,
        max_posts=body.max_posts,
        metrics_only=body.metrics_only,
        skip_phases=body.skip_phases,
        force_re_download=body.force_re_download,
        force_reclassify=body.force_reclassify,
        page_size=body.page_size,
    )

    registry = get_registry()

    async def factory(emit):
        agent = InstagramIngestAgent(options, on_event=emit)
        return await agent.run()

    tmp_run_id = await registry.start(factory)
    entry = registry.get(tmp_run_id)
    real_run_id = tmp_run_id
    if entry:
        import asyncio as _aio
        for _ in range(50):
            if entry.run_id > 0:
                real_run_id = entry.run_id
                break
            await _aio.sleep(0.05)

    return InstagramIngestRunResponse(run_id=real_run_id, status="started")


@router.get("/runs")
async def list_runs(limit: int = 20) -> dict:
    limit = max(1, min(int(limit), 100))
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT id, target_domain, status, started_at, finished_at, notes
                  FROM scrape_runs
                 WHERE target_domain='instagram-ingest'
                 ORDER BY id DESC LIMIT %s
                """,
                (limit,),
            )
            rows = [_row(r) for r in await cur.fetchall()]
    return {"items": rows}


@router.get("/runs/{run_id}")
async def get_run(run_id: int) -> dict:
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT id, target_domain, status, started_at, finished_at, notes "
                "  FROM scrape_runs WHERE id=%s",
                (run_id,),
            )
            row = await cur.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="run not found")

    phases: dict = {}
    entry = get_registry().get(run_id)
    if entry:
        for evt in entry.events:
            if evt.get("type") == "phase_completed":
                phases[evt["phase"]] = {
                    "name": evt["phase"],
                    "processed": evt.get("processed", 0),
                    "failed": evt.get("failed", 0),
                    "skipped": evt.get("skipped", 0),
                    "total": evt.get("total", 0),
                    "extra": evt.get("extra", {}),
                }
            elif evt.get("type") == "phase_started":
                phases.setdefault(evt["phase"], {"name": evt["phase"], "processed": 0, "total": 0})

    base = _row(row) or {}
    base["phases"] = phases
    return base


@router.post("/runs/{run_id}/cancel")
async def cancel_run(run_id: int) -> dict:
    db_flagged = await request_cancel(run_id)
    in_flight = await get_registry().cancel(run_id)
    if not db_flagged and not in_flight:
        raise HTTPException(status_code=404, detail="run not active or not found")
    return {"run_id": run_id, "cancel_requested": True, "in_flight_cancelled": in_flight}


@router.get("/runs/{run_id}/events")
async def stream_run_events(run_id: int):
    registry = get_registry()

    async def event_source() -> AsyncIterator[dict]:
        entry = registry.get(run_id)
        if entry:
            async for evt in registry.subscribe(run_id):
                yield {"event": evt.get("type", "message"), "data": json.dumps(evt)}
            return

        async with get_conn() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT status, notes FROM scrape_runs WHERE id=%s",
                    (run_id,),
                )
                row = await cur.fetchone()
        if not row:
            yield {
                "event": "info",
                "data": json.dumps({"type": "no_active_run", "run_id": run_id}),
            }
            return
        if row["status"] == "completed":
            yield {
                "event": "run_completed",
                "data": json.dumps({
                    "type": "run_completed",
                    "run_id": run_id,
                    "result": {"status": "completed", "notes": row.get("notes")},
                }),
            }
        else:
            yield {
                "event": "run_failed",
                "data": json.dumps({
                    "type": "run_failed",
                    "run_id": run_id,
                    "error": row.get("notes") or (
                        "Run orphaned (backend restart)."
                        if row["status"] == "started" else row["status"]
                    ),
                }),
            }

    return EventSourceResponse(event_source())


# ---------------------------------------------------------------------------
# Posts browser
# ---------------------------------------------------------------------------

@router.get("/posts")
async def list_posts(
    limit: int = 50,
    offset: int = 0,
    username: Optional[str] = None,
    order: str = "newest",
    category: Optional[str] = None,
) -> dict:
    limit = max(1, min(int(limit), 200))
    offset = max(0, int(offset))
    sort_clause = (
        "timestamp_posted DESC NULLS LAST, id DESC"
        if order != "engagement"
        else "(COALESCE(like_count,0) + COALESCE(comments_count,0)) DESC NULLS LAST, id DESC"
    )

    where = "WHERE 1=1"
    params: list = []
    if username:
        where += " AND account_username = %s"
        params.append(username)
    if category:
        where += " AND metadata->>'content_category' = %s"
        params.append(category)
    params.extend([limit, offset])

    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                f"""
                SELECT id, ig_media_id, account_username, media_type, media_product_type,
                       caption, permalink, timestamp_posted,
                       like_count, comments_count, view_count, children_count,
                       (caption_embedding IS NOT NULL) AS embedded,
                       metadata->>'content_category' AS content_category,
                       LEFT(COALESCE(ai_summary, caption, ''), 240) AS preview,
                       (SELECT COUNT(*) FROM instagram_post_assets ipa WHERE ipa.post_id = p.id) AS asset_count,
                       (SELECT asset_id FROM instagram_post_assets ipa
                          WHERE ipa.post_id = p.id ORDER BY child_index ASC LIMIT 1) AS first_asset_id,
                       (SELECT a.asset_type FROM instagram_post_assets ipa
                          JOIN assets a ON a.id = ipa.asset_id
                          WHERE ipa.post_id = p.id ORDER BY ipa.child_index ASC LIMIT 1) AS first_asset_type,
                       last_metrics_at, first_seen_at
                  FROM instagram_posts p
                 {where}
                 ORDER BY {sort_clause}
                 LIMIT %s OFFSET %s
                """,
                tuple(params),
            )
            items = [_row(r) for r in await cur.fetchall()]

            await cur.execute(
                """
                SELECT
                  COUNT(*)::int AS total,
                  COUNT(*) FILTER (WHERE media_type='IMAGE')::int AS images,
                  COUNT(*) FILTER (WHERE media_type='VIDEO')::int AS videos,
                  COUNT(*) FILTER (WHERE media_type='CAROUSEL_ALBUM')::int AS carousels,
                  COUNT(*) FILTER (WHERE caption_embedding IS NOT NULL)::int AS embedded,
                  COALESCE(SUM(like_count), 0)::bigint AS total_likes,
                  COALESCE(SUM(comments_count), 0)::bigint AS total_comments,
                  COALESCE(SUM(view_count), 0)::bigint AS total_views,
                  COUNT(*) FILTER (WHERE metadata->>'content_category' = 'img_poster')::int AS img_poster,
                  COUNT(*) FILTER (WHERE metadata->>'content_category' = 'img_poster_human')::int AS img_poster_human,
                  COUNT(*) FILTER (WHERE metadata->>'content_category' = 'img_poster_animated')::int AS img_poster_animated,
                  COUNT(*) FILTER (WHERE metadata->>'content_category' = 'video_interview')::int AS video_interview,
                  COUNT(*) FILTER (WHERE metadata->>'content_category' = 'other')::int AS other,
                  COUNT(*) FILTER (WHERE metadata->>'content_category' IS NULL)::int AS unclassified
                FROM instagram_posts
                """
            )
            totals = _row(await cur.fetchone()) or {}

    return {"items": items, "totals": totals}


@router.get("/posts/{post_id}")
async def get_post(post_id: int) -> dict:
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT id, ig_media_id, account_username, media_type, media_product_type,
                       caption, permalink, timestamp_posted,
                       like_count, comments_count, view_count, children_count,
                       reach, impressions, plays, saved,
                       ai_summary, first_seen_at, last_metrics_at, metadata,
                       metadata->>'content_category' AS content_category,
                       metadata->'video_analysis' AS video_analysis,
                       metadata->>'video_deepened_at' AS video_deepened_at,
                       (caption_embedding IS NOT NULL) AS embedded
                  FROM instagram_posts
                 WHERE id=%s
                """,
                (post_id,),
            )
            post = await cur.fetchone()
            if not post:
                raise HTTPException(status_code=404, detail="post not found")

            await cur.execute(
                """
                SELECT ipa.id, ipa.child_index, ipa.ig_child_media_id,
                       a.id AS asset_id, a.asset_type, a.local_path, a.mime_type,
                       a.width, a.height, a.bytes, a.ai_description,
                       a.analyze_status, a.embed_status,
                       vs.summary AS video_summary, vs.duration_sec
                  FROM instagram_post_assets ipa
                  JOIN assets a ON a.id = ipa.asset_id
                  LEFT JOIN video_summaries vs ON vs.asset_id = a.id
                 WHERE ipa.post_id=%s
                 ORDER BY ipa.child_index
                """,
                (post_id,),
            )
            assets = [_row(r) for r in await cur.fetchall()]

            await cur.execute(
                """
                SELECT segment_index, start_sec, end_sec, text
                  FROM video_transcripts
                 WHERE asset_id IN (
                    SELECT asset_id FROM instagram_post_assets WHERE post_id=%s
                 )
                 ORDER BY asset_id, segment_index
                """,
                (post_id,),
            )
            transcript = [_row(r) for r in await cur.fetchall()]

            await cur.execute(
                """
                SELECT captured_at, like_count, comments_count, view_count
                  FROM instagram_metrics_snapshots
                 WHERE post_id=%s
                 ORDER BY captured_at DESC
                 LIMIT 30
                """,
                (post_id,),
            )
            history = [_row(r) for r in await cur.fetchall()]

            await cur.execute(
                """
                SELECT s.id, s.scene_index, s.start_sec, s.end_sec,
                       s.description, s.transcript_excerpt,
                       s.representative_frame_id,
                       vf.local_path AS frame_local_path
                  FROM instagram_video_scenes s
                  LEFT JOIN video_frames vf ON vf.id = s.representative_frame_id
                 WHERE s.post_id=%s
                 ORDER BY s.scene_index
                """,
                (post_id,),
            )
            scenes = [_row(r) for r in await cur.fetchall()]

    out = _row(post) or {}
    out["assets"] = assets
    out["transcript"] = transcript
    out["metrics_history"] = history
    out["scenes"] = scenes
    return out


class UpdateCategoryRequest(BaseModel):
    category: str


@router.patch("/posts/{post_id}/category")
async def update_post_category(post_id: int, body: UpdateCategoryRequest) -> dict:
    """Manually override a post's content_category."""
    valid = {"img_poster", "img_poster_human", "img_poster_animated", "video_interview", "other"}
    if body.category not in valid:
        raise HTTPException(status_code=400, detail=f"Invalid category. Must be one of: {', '.join(sorted(valid))}")
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                UPDATE instagram_posts
                   SET metadata = metadata || %s::jsonb
                 WHERE id = %s
                RETURNING id
                """,
                (
                    json.dumps({
                        "content_category": body.category,
                        "content_category_confidence": 1.0,
                        "content_category_reason": "manually set by user",
                    }),
                    post_id,
                ),
            )
            row = await cur.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="post not found")
    return {"id": post_id, "category": body.category}


@router.get("/posts/{post_id}/frames/{frame_id}/file")
async def get_frame_file(post_id: int, frame_id: int):
    """Stream a single video frame file (used by the scene-by-scene UI)."""
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT vf.local_path
                  FROM video_frames vf
                  JOIN instagram_post_assets ipa ON ipa.asset_id = vf.asset_id
                 WHERE ipa.post_id=%s AND vf.id=%s
                """,
                (post_id, frame_id),
            )
            row = await cur.fetchone()
    if not row or not row["local_path"]:
        raise HTTPException(status_code=404, detail="frame not found")
    p: Path = resolve_local(row["local_path"])
    if not p.exists():
        raise HTTPException(status_code=410, detail="frame file missing on disk")
    media_type = mimetypes.guess_type(str(p))[0] or "image/jpeg"
    return FileResponse(str(p), media_type=media_type, filename=p.name)


@router.get("/posts/{post_id}/assets/{asset_id}/file")
async def get_asset_file(post_id: int, asset_id: int):
    """Stream the local media file for a given post asset."""
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT a.local_path, a.mime_type
                  FROM instagram_post_assets ipa
                  JOIN assets a ON a.id = ipa.asset_id
                 WHERE ipa.post_id=%s AND ipa.asset_id=%s
                """,
                (post_id, asset_id),
            )
            row = await cur.fetchone()
    if not row or not row["local_path"]:
        raise HTTPException(status_code=404, detail="asset file not found")
    p: Path = resolve_local(row["local_path"])
    if not p.exists():
        raise HTTPException(status_code=410, detail="file missing on disk")
    media_type = row.get("mime_type") or mimetypes.guess_type(str(p))[0] or "application/octet-stream"
    return FileResponse(str(p), media_type=media_type, filename=p.name)


# ---------------------------------------------------------------------------
# Health (token check + account stats)
# ---------------------------------------------------------------------------

@router.get("/health")
async def health() -> dict:
    """Verify the IG token works + report DB counts. Used by the Instagram tab
    to show a credentials badge."""
    out: dict = {
        "configured": bool(config.instagram_access_token and config.instagram_account_id),
        "target_username": config.instagram_target_username,
        "api_version": config.instagram_api_version,
    }
    if not out["configured"]:
        out["token_ok"] = False
        out["error"] = "INSTAGRAM_ACCESS_TOKEN / INSTAGRAM_ACCOUNT_ID not set"
    else:
        try:
            ping = await ig_ping()
            out["token_ok"] = bool(ping.get("ok"))
            out["account"] = {
                "username": ping.get("username"),
                "name": ping.get("name"),
                "id": ping.get("id"),
            }
        except InstagramConfigError as e:
            out["token_ok"] = False
            out["error"] = str(e)
        except InstagramAPIError as e:
            out["token_ok"] = False
            out["error"] = f"{e} (status={e.status})"
        except Exception as e:
            out["token_ok"] = False
            out["error"] = str(e)[:300]

    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT
                  COUNT(*)::int AS total_posts,
                  COUNT(*) FILTER (WHERE caption_embedding IS NOT NULL)::int AS embedded_posts,
                  MAX(timestamp_posted) AS latest_post_at,
                  MAX(last_metrics_at) AS latest_metrics_at
                FROM instagram_posts
                """
            )
            out["db"] = _row(await cur.fetchone()) or {}
    return out
