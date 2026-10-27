"""Background worker that polls Veo LROs for the Create-Animation store.

Mirrors yas_animations_poller.py. The animation record lives in Postgres
(yas_animation_generations via yas_animation_store); the finished MP4 is
uploaded to Supabase Storage (producer-media bucket). Started in main.lifespan
alongside the other Veo pollers; cancelled on shutdown.

Survives backend restarts — any animation record left at status='animating'
with a stored veo_operation_name is picked up on the next tick.

For each completed LRO:
  - success: download MP4 from GCS → upload to Supabase, flip status='ready'.
  - failure: flip status='failed', record the error.
"""
from __future__ import annotations

import asyncio
import logging

from ..config import config
from ..services import asset_storage
from ..services import yas_animation_store as store
from ..services.vertex_client import download_gcs, veo_poll

log = logging.getLogger("archivist.yas_local_animation_poller")

POLL_INTERVAL_SEC = 15

_task: asyncio.Task | None = None


async def _mark_ready(anim_id: int, gcs_uri: str) -> None:
    """Download the finished MP4 from GCS, upload it to Supabase Storage, and
    flip the record to ready. The GCS download lands in a temp file that's
    removed once the bytes are read (no permanent local copy)."""
    import tempfile
    from pathlib import Path

    tmp = Path(tempfile.gettempdir()) / f"yas-anim-{anim_id}.mp4"
    try:
        await download_gcs(gcs_uri, tmp)
        data = tmp.read_bytes()
    except Exception as e:
        log.exception("anim=%d download from %s failed", anim_id, gcs_uri)
        await store.update(
            anim_id, status="failed", gcs_uri=gcs_uri,
            error=f"download_failed: {str(e)[:300]}",
        )
        return
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except Exception:
            pass

    try:
        up = await asset_storage.upload(
            data=data,
            path=f"animation/{anim_id}.mp4",
            mime="video/mp4",
            bucket=config.producer_media_bucket,
        )
    except Exception as e:
        log.exception("anim=%d upload to Supabase failed", anim_id)
        await store.update(
            anim_id, status="failed", gcs_uri=gcs_uri,
            error=f"upload_failed: {str(e)[:300]}",
        )
        return

    await store.update(
        anim_id,
        status="ready",
        gcs_uri=gcs_uri,
        video_storage_bucket=up.get("storage_bucket"),
        video_storage_path=up.get("storage_path"),
        video_local_path=up.get("local_path"),
        mime_type="video/mp4",
        bytes=len(data),
    )
    log.info("anim=%d ready (%s → supabase, %s bytes)", anim_id, gcs_uri, len(data))


async def _poll_once() -> None:
    try:
        rows = await store.pending()
    except Exception:
        log.exception("yas_local_animation_poller: pending-query failed")
        return
    if not rows:
        return
    log.debug("yas_local_animation_poller: %d pending animation(s)", len(rows))
    for r in rows:
        op_name = r.get("veo_operation_name")
        try:
            result = await veo_poll(op_name)
        except Exception as e:
            log.warning("veo_poll(%s) raised: %s", op_name, e)
            continue
        if not result.get("done"):
            continue
        if result.get("error"):
            await store.update(int(r["id"]), status="failed", error=str(result["error"])[:500])
        elif result.get("gcs_uri"):
            await _mark_ready(int(r["id"]), result["gcs_uri"])
        else:
            await store.update(int(r["id"]), status="failed", error="done but no gcs_uri returned")


async def _loop() -> None:
    log.info("yas_local_animation_poller started (every %ds)", POLL_INTERVAL_SEC)
    try:
        while True:
            await _poll_once()
            await asyncio.sleep(POLL_INTERVAL_SEC)
    except asyncio.CancelledError:
        log.info("yas_local_animation_poller cancelled")
        raise
    except Exception:
        log.exception("yas_local_animation_poller loop crashed")


def start() -> asyncio.Task:
    global _task
    if _task is not None and not _task.done():
        return _task
    _task = asyncio.create_task(_loop(), name="yas_local_animation_poller")
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
