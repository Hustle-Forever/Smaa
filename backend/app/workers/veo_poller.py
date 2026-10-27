"""Background worker that polls Veo long-running operations to completion.

Started in main.lifespan; cancelled on shutdown. Survives backend restarts —
on boot, any producer_videos row left at status='generating' with a stored
veo_operation_name is picked up here and continues polling.

For each completed LRO:
  - success: download MP4 from GCS to data/producer/<run_id>/<video_id>.mp4,
             flip status='ready', set local_path + gcs_uri.
  - failure: flip status='failed', record metadata.error, mark cost refunded
             (we still paid GCP — the flag is for UI honesty).
"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from psycopg.types.json import Jsonb

from ..config import config
from ..db import get_conn
from ..services.vertex_client import download_gcs, veo_poll

log = logging.getLogger("archivist.veo_poller")

POLL_INTERVAL_SEC = 15

_task: asyncio.Task | None = None


async def _pending_videos() -> list[dict]:
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT v.id, v.script_id, v.veo_operation_name, v.veo_model,
                       v.duration_sec, v.cost_usd, v.metadata,
                       (SELECT i.run_id FROM producer_ideas i
                          JOIN producer_scripts s ON s.idea_id=i.id
                          WHERE s.id=v.script_id) AS run_id
                  FROM producer_videos v
                 WHERE v.status='generating'
                   AND v.veo_operation_name IS NOT NULL
                """
            )
            return await cur.fetchall()


async def _mark_ready(video_id: int, run_id: int, gcs_uri: str) -> None:
    dest = config.producer_root / str(run_id) / f"video-{video_id}.mp4"
    try:
        await download_gcs(gcs_uri, dest)
        local_path = str(dest)
    except Exception as e:
        log.exception("video=%d download from %s failed", video_id, gcs_uri)
        async with get_conn() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    UPDATE producer_videos
                       SET status='failed',
                           gcs_uri=%s,
                           metadata = metadata || %s::jsonb
                     WHERE id=%s
                    """,
                    (gcs_uri, Jsonb({"error": str(e)[:300], "cost_refunded": True}), video_id),
                )
        return

    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                UPDATE producer_videos
                   SET status='ready', gcs_uri=%s, local_path=%s
                 WHERE id=%s
                """,
                (gcs_uri, local_path, video_id),
            )
            await cur.execute(
                "UPDATE producer_runs SET status='video_ready' WHERE id=%s AND status='video_pending'",
                (run_id,),
            )
    log.info("video=%d ready (%s → %s)", video_id, gcs_uri, local_path)


async def _mark_failed(video_id: int, error: str) -> None:
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                UPDATE producer_videos
                   SET status='failed',
                       metadata = metadata || %s::jsonb
                 WHERE id=%s
                """,
                (Jsonb({"error": error[:300], "cost_refunded": True}), video_id),
            )
    log.warning("video=%d failed: %s", video_id, error[:200])


async def _poll_once() -> None:
    try:
        rows = await _pending_videos()
    except Exception:
        log.exception("veo_poller: pending-query failed")
        return
    if not rows:
        return
    log.debug("veo_poller: %d pending video(s)", len(rows))
    for r in rows:
        try:
            result = await veo_poll(r["veo_operation_name"])
        except Exception as e:
            log.warning("veo_poll(%s) raised: %s", r["veo_operation_name"], e)
            continue
        if not result.get("done"):
            continue
        if result.get("error"):
            await _mark_failed(r["id"], result["error"])
        elif result.get("gcs_uri"):
            await _mark_ready(r["id"], r["run_id"], result["gcs_uri"])
        else:
            await _mark_failed(r["id"], "done but no gcs_uri returned")


async def _loop() -> None:
    log.info("veo_poller started (every %ds)", POLL_INTERVAL_SEC)
    try:
        while True:
            await _poll_once()
            await asyncio.sleep(POLL_INTERVAL_SEC)
    except asyncio.CancelledError:
        log.info("veo_poller cancelled")
        raise
    except Exception:
        log.exception("veo_poller loop crashed")


def start() -> asyncio.Task:
    global _task
    if _task is not None and not _task.done():
        return _task
    _task = asyncio.create_task(_loop(), name="veo_poller")
    return _task


async def stop() -> None:
    global _task
    if _task is None:
        return
    _task.cancel()
    try:
        await _task
    except (asyncio.CancelledError, Exception):
        pass
    _task = None
