"""Background worker that polls Remotion render jobs to completion.

Started in main.lifespan; cancelled on shutdown. Survives backend restarts —
on boot, any producer_compositions row left at status IN ('queued','rendering')
with a stored render_job_id is picked up here.

For each completed job:
  - success: mark status='ready', set output_path if not already set.
  - failure: mark status='failed', record metadata.error.

Express keeps job state in memory — if it restarts, our /jobs/:id query
returns 404, and remotion_client.get_job_status() surfaces that as
"failed: job lost". The row flips to 'failed' and the user can re-fire.
"""
from __future__ import annotations

import asyncio
import logging

from psycopg.types.json import Jsonb

from ..db import get_conn
from ..services import remotion_client

log = logging.getLogger("archivist.composition_poller")

POLL_INTERVAL_SEC = 15

_task: asyncio.Task | None = None


async def _pending_compositions() -> list[dict]:
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT id, run_id, render_job_id, output_path, status, metadata
                  FROM producer_compositions
                 WHERE status IN ('queued','rendering')
                   AND render_job_id IS NOT NULL
                """
            )
            return await cur.fetchall()


async def _mark_ready(composition_id: int, output_path: str | None) -> None:
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                UPDATE producer_compositions
                   SET status='ready',
                       output_path = COALESCE(%s, output_path),
                       metadata = metadata || jsonb_build_object('progress', 1.0)
                 WHERE id=%s
                """,
                (output_path, composition_id),
            )
    log.info("composition=%d ready (%s)", composition_id, output_path)


async def _mark_failed(composition_id: int, error: str) -> None:
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                UPDATE producer_compositions
                   SET status='failed',
                       error=%s,
                       metadata = metadata || jsonb_build_object('error', %s)
                 WHERE id=%s
                """,
                (error[:1000], error[:300], composition_id),
            )
    log.warning("composition=%d failed: %s", composition_id, error[:200])


async def _update_progress(composition_id: int, progress: float) -> None:
    """Best-effort progress update — non-critical, swallow errors."""
    try:
        async with get_conn() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    UPDATE producer_compositions
                       SET metadata = metadata || %s::jsonb
                     WHERE id=%s
                    """,
                    (Jsonb({"progress": round(float(progress), 3)}), composition_id),
                )
    except Exception:
        log.debug("composition=%d progress update failed (non-fatal)", composition_id, exc_info=True)


async def _poll_once() -> None:
    try:
        rows = await _pending_compositions()
    except Exception:
        log.exception("composition_poller: pending-query failed")
        return
    if not rows:
        return
    log.debug("composition_poller: %d pending composition(s)", len(rows))
    for r in rows:
        try:
            result = await remotion_client.get_job_status(r["render_job_id"])
        except Exception as e:
            log.warning(
                "get_job_status(%s) raised: %s", r["render_job_id"], e,
            )
            continue
        status = result.get("status")
        if status == "done":
            await _mark_ready(r["id"], result.get("output_path") or r.get("output_path"))
        elif status == "failed":
            await _mark_failed(r["id"], result.get("error") or "unknown error")
        elif status in ("queued", "rendering"):
            progress = result.get("progress")
            if isinstance(progress, (int, float)):
                await _update_progress(r["id"], progress)


async def _loop() -> None:
    log.info("composition_poller started (every %ds)", POLL_INTERVAL_SEC)
    try:
        while True:
            await _poll_once()
            await asyncio.sleep(POLL_INTERVAL_SEC)
    except asyncio.CancelledError:
        log.info("composition_poller cancelled")
        raise
    except Exception:
        log.exception("composition_poller loop crashed")


def start() -> asyncio.Task:
    global _task
    if _task is not None and not _task.done():
        return _task
    _task = asyncio.create_task(_loop(), name="composition_poller")
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
