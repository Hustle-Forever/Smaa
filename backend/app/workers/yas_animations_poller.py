"""Background worker that polls Veo LROs for yas_animations.

Started in main.lifespan alongside veo_poller; cancelled on shutdown.
Survives backend restarts — any yas_animations row left at status='generating'
with a stored veo_operation_name is picked up here on the next tick.

For each completed LRO:
  - success: download MP4 from GCS to data/yas_producer/animations/anim-{id}.mp4,
             then upload to producer-assets bucket at yas/animations/{id}.mp4
             (or stay on local disk if Supabase Storage isn't configured),
             flip status='ready'.
  - failure: flip status='failed', record metadata.error, mark cost refunded
             (we still paid GCP — the flag is for UI honesty).

Mirrors backend/app/workers/veo_poller.py — the pattern is identical, only
the table and the destination path differ.
"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from psycopg.types.json import Jsonb

from ..config import config
from ..db import get_conn
from ..services import asset_storage
from ..services.vertex_client import download_gcs, veo_poll

log = logging.getLogger("archivist.yas_animations_poller")

POLL_INTERVAL_SEC = 15

_task: asyncio.Task | None = None


# Local disk root for animation MP4s — matches ANIMATIONS_DIR in the router.
ANIMATIONS_DIR = (config.asset_root.parent / "yas_producer" / "animations").resolve()


async def _pending() -> list[dict]:
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT id, veo_operation_name, duration_sec
                  FROM yas_animations
                 WHERE status='generating'
                   AND veo_operation_name IS NOT NULL
                """
            )
            return await cur.fetchall()


async def _check_th_bookend(anim_id: int, path: Path) -> None:
    """yas_talking_head_v2.check_bookend_clip, never raising: a check that
    breaks leaves the clip as Veo made it."""
    try:
        from ..services import yas_talking_head_v2 as th_v2_service
        await th_v2_service.check_bookend_clip(anim_id, path)
    except Exception:
        log.exception("anim=%d bookend read-back failed; keeping the clip", anim_id)


async def _mark_ready(anim_id: int, gcs_uri: str) -> None:
    """Download the finished MP4, upload to bucket (or keep local), update row."""
    ANIMATIONS_DIR.mkdir(parents=True, exist_ok=True)
    local_dest = ANIMATIONS_DIR / f"anim-{anim_id}.mp4"
    try:
        await download_gcs(gcs_uri, local_dest)
    except Exception as e:
        log.exception("anim=%d download from %s failed", anim_id, gcs_uri)
        async with get_conn() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    UPDATE yas_animations
                       SET status='failed',
                           gcs_uri=%s,
                           error=%s,
                           metadata = metadata || %s::jsonb,
                           updated_at=NOW()
                     WHERE id=%s
                    """,
                    (
                        gcs_uri,
                        f"download_failed: {str(e)[:300]}",
                        Jsonb({"cost_refunded": True, "download_error": str(e)[:300]}),
                        anim_id,
                    ),
                )
        return

    # A Talking Head v2 bookend made by Veo is read back against its card
    # first: when Veo changed the card's text or look, the file becomes the
    # card's slow zoom. A no-op for every other clip.
    await _check_th_bookend(anim_id, local_dest)

    mp4_bytes = local_dest.read_bytes()
    # Upload to bucket (or fall back to local). Path mirrors the existing
    # yas/generations/{id}.png pattern used for static posters.
    try:
        result = await asset_storage.upload(
            data=mp4_bytes,
            path=f"yas/animations/{anim_id}.mp4",
            mime="video/mp4",
        )
    except Exception:
        log.exception("anim=%d bucket upload failed; falling back to local-only", anim_id)
        result = {
            "storage_bucket": None,
            "storage_path": None,
            "local_path": str(local_dest),
            "bytes": len(mp4_bytes),
            "mime_type": "video/mp4",
        }

    storage_bucket = result.get("storage_bucket")
    storage_path = result.get("storage_path")
    local_path = result.get("local_path") or str(local_dest)

    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                UPDATE yas_animations
                   SET status='ready',
                       gcs_uri=%s,
                       storage_bucket=%s,
                       storage_path=%s,
                       local_path=%s,
                       mime_type='video/mp4',
                       bytes=%s,
                       updated_at=NOW()
                 WHERE id=%s
                """,
                (gcs_uri, storage_bucket, storage_path, local_path, len(mp4_bytes), anim_id),
            )
    log.info(
        "anim=%d ready (%s → bucket=%s path=%s local=%s, %d bytes)",
        anim_id, gcs_uri, storage_bucket, storage_path, local_path, len(mp4_bytes),
    )


async def _mark_failed(anim_id: int, error: str) -> None:
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                UPDATE yas_animations
                   SET status='failed',
                       error=%s,
                       metadata = metadata || %s::jsonb,
                       updated_at=NOW()
                 WHERE id=%s
                """,
                (error[:500], Jsonb({"cost_refunded": True}), anim_id),
            )
    log.warning("anim=%d failed: %s", anim_id, error[:200])


async def _poll_once() -> None:
    try:
        rows = await _pending()
    except Exception:
        log.exception("yas_animations_poller: pending-query failed")
        return
    if not rows:
        return
    log.debug("yas_animations_poller: %d pending animation(s)", len(rows))
    for r in rows:
        try:
            result = await veo_poll(r["veo_operation_name"])
        except Exception as e:
            log.warning("veo_poll(%s) raised: %s", r["veo_operation_name"], e)
            continue
        if not result.get("done"):
            continue
        if result.get("error"):
            # Transient server-capacity failures ("high load") self-heal on a
            # fresh submit — auto-resubmit before giving up. Try the talking-head
            # v1 and v2 services in turn: each only handles animations whose
            # parent run belongs to its own mode (the other returns False with
            # no side effects). Any problem falls through to _mark_failed.
            from ..services import yas_talking_head as th_service
            from ..services import yas_talking_head_v2 as th_v2_service
            resubmitted = False
            for _svc in (th_service, th_v2_service):
                try:
                    if await _svc.resubmit_animation_if_transient(
                        r["id"], result["error"],
                    ):
                        resubmitted = True
                        break
                except Exception:
                    log.exception(
                        "transient auto-resubmit hook (%s) failed for anim=%d",
                        _svc.__name__, r["id"],
                    )
            if resubmitted:
                continue
            await _mark_failed(r["id"], result["error"])
        elif result.get("gcs_uri"):
            await _mark_ready(r["id"], result["gcs_uri"])
        else:
            await _mark_failed(r["id"], "done but no gcs_uri returned")


async def _loop() -> None:
    log.info("yas_animations_poller started (every %ds)", POLL_INTERVAL_SEC)
    try:
        while True:
            await _poll_once()
            await asyncio.sleep(POLL_INTERVAL_SEC)
    except asyncio.CancelledError:
        log.info("yas_animations_poller cancelled")
        raise
    except Exception:
        log.exception("yas_animations_poller loop crashed")


def start() -> asyncio.Task:
    global _task
    if _task is not None and not _task.done():
        return _task
    _task = asyncio.create_task(_loop(), name="yas_animations_poller")
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
