"""Background worker that finishes General AI Video clips, on either engine.

Mirrors yas_local_animation_poller.py. The record lives in Postgres
(yas_general_video_generations via yas_general_video_store); the finished MP4 is
uploaded to Supabase Storage (producer-media bucket). Started in main.lifespan
alongside the other Veo pollers; cancelled on shutdown.

Survives backend restarts — any record left at status='animating' with a stored
veo_operation_name or seedance_status_url is picked up on the next tick.

For each completed job:
  - Veo: download the MP4 from GCS → upload to Supabase, flip status='ready'.
  - Seedance: download the MP4 from Higgsfield's URL (kept there about a week)
    → upload to Supabase, flip status='ready'. The clip is paid for, so a failed
    download or upload is retried on later ticks before it is given up on.
  - failure: flip status='failed', record the error. A Seedance job that ended
    failed / nsfw / canceled was not charged, so its spend is written off.

Each tick also fails claims whose request died before the engine took the job
(see yas_general_video_store.claim_for_animation).
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timedelta, timezone

import httpx

from ..config import config
from ..services import asset_storage
from ..services import higgsfield_client as higgsfield
from ..services import yas_general_video_store as store
from ..services.vertex_client import download_gcs, veo_poll

log = logging.getLogger("archivist.yas_general_video_poller")

POLL_INTERVAL_SEC = 15

_task: asyncio.Task | None = None


SEEDANCE_DOWNLOAD_TIMEOUT_SEC = 300.0
# A finished Seedance video that cannot be downloaded or stored is retried on
# every tick until this long after it was submitted.
SEEDANCE_SAVE_GIVE_UP_HOURS = 24
# How often to repeat the warning that Seedance clips are waiting because this
# process has no Higgsfield keys.
MISSING_KEYS_WARN_EVERY_SEC = 600.0
_missing_keys_warned_at: float | None = None

# Outcomes Higgsfield does not charge for, and what a person reads for each.
_SEEDANCE_UNCHARGED = {
    "nsfw": "Seedance refused this clip as unsafe content. Nothing was charged.",
    "canceled": "The Seedance job was cancelled. Nothing was charged.",
    "failed": "Seedance could not make this clip. Nothing was charged.",
}


async def _mark_ready(gen_id: int, gcs_uri: str) -> None:
    """Download the finished MP4 from GCS, upload it to Supabase Storage, and
    flip the record to ready. The GCS download lands in a temp file removed once
    the bytes are read (no permanent local copy)."""
    import tempfile
    from pathlib import Path

    tmp = Path(tempfile.gettempdir()) / f"yas-genvid-{gen_id}.mp4"
    try:
        await download_gcs(gcs_uri, tmp)
        data = tmp.read_bytes()
    except Exception as e:
        log.exception("gen=%d download from %s failed", gen_id, gcs_uri)
        await store.update(
            gen_id, status="failed", gcs_uri=gcs_uri,
            error=f"download_failed: {str(e)[:300]}",
        )
        return
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except Exception:
            pass
    try:
        up = await _upload_video(gen_id, data)
    except Exception as e:
        log.exception("gen=%d upload to Supabase failed", gen_id)
        await store.update(
            gen_id, status="failed", gcs_uri=gcs_uri,
            error=f"upload_failed: {str(e)[:300]}",
        )
        return
    await _mark_video_ready(gen_id, data, up, gcs_uri=gcs_uri)


async def _upload_video(gen_id: int, data: bytes) -> dict:
    return await asset_storage.upload(
        data=data,
        path=f"general-video/{gen_id}.mp4",
        mime="video/mp4",
        bucket=config.producer_media_bucket,
    )


async def _mark_video_ready(gen_id: int, data: bytes, up: dict, *, gcs_uri: str | None) -> None:
    await store.update(
        gen_id,
        status="ready",
        gcs_uri=gcs_uri,
        video_storage_bucket=up.get("storage_bucket"),
        video_storage_path=up.get("storage_path"),
        video_local_path=up.get("local_path"),
        mime_type="video/mp4",
        bytes=len(data),
    )
    log.info("gen=%d ready (%s → supabase, %s bytes)", gen_id, gcs_uri or "seedance", len(data))


def _seedance_overdue(r: dict) -> bool:
    """True once a Seedance clip submitted this long ago still is not saved."""
    raw = r.get("seedance_submitted_at") or r.get("updated_at")
    if not raw:
        return False
    try:
        when = raw if isinstance(raw, datetime) else datetime.fromisoformat(str(raw))
    except ValueError:
        return False
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - when > timedelta(hours=SEEDANCE_SAVE_GIVE_UP_HOURS)


async def _finish_seedance(r: dict, video_url: str) -> None:
    """Download a finished Seedance MP4 and store it like a Veo one. The clip is
    already paid for, so a failed download or upload leaves it animating to be
    tried again next tick, until SEEDANCE_SAVE_GIVE_UP_HOURS have passed."""
    gen_id = int(r["id"])
    try:
        async with httpx.AsyncClient(timeout=SEEDANCE_DOWNLOAD_TIMEOUT_SEC, follow_redirects=True) as client:
            resp = await client.get(video_url)
            resp.raise_for_status()
            data = resp.content
        if not data:
            raise ValueError("Higgsfield sent an empty file")
        up = await _upload_video(gen_id, data)
    except Exception as e:
        if _seedance_overdue(r):
            log.error("gen=%d Seedance video still not saved after %dh; giving up: %s",
                      gen_id, SEEDANCE_SAVE_GIVE_UP_HOURS, e)
            await store.update(
                gen_id, status="failed",
                error=f"The finished Seedance video could not be saved: {str(e)[:300]}",
            )
        else:
            log.warning("gen=%d Seedance video not saved yet (%s); trying again next tick", gen_id, str(e)[:200])
        return
    await _mark_video_ready(gen_id, data, up, gcs_uri=None)


async def _poll_seedance(r: dict) -> None:
    gen_id = int(r["id"])
    try:
        result = await higgsfield.get_status(r["seedance_status_url"])
    except higgsfield.HiggsfieldError as e:
        if e.code == "not_found":
            await store.update(gen_id, status="failed", error=e.message[:500])
        else:
            log.warning("seedance status gen=%d raised: %s (%s)", gen_id, e.message, e.code)
        return
    if not result.get("terminal"):
        return
    status = result.get("status") or ""
    if status == "completed" and result.get("video_url"):
        await _finish_seedance(r, result["video_url"])
        return
    if status in _SEEDANCE_UNCHARGED:
        msg = _SEEDANCE_UNCHARGED[status]
        detail = result.get("error")
        if detail and status == "failed":
            msg += f" ({str(detail)[:200]})"
        # Not charged, so it stops counting against SEEDANCE_BUDGET_USD.
        await store.write_off_seedance_spend(request_id=r.get("seedance_request_id"))
        await store.update(gen_id, status="failed", error=msg[:500], seedance_cost_usd=0)
        return
    await store.update(gen_id, status="failed", error="Seedance finished but sent back no video.")


async def _poll_veo(r: dict) -> None:
    op_name = r.get("veo_operation_name")
    try:
        result = await veo_poll(op_name)
    except Exception as e:
        log.warning("veo_poll(%s) raised: %s", op_name, e)
        return
    if not result.get("done"):
        return
    if result.get("error"):
        await store.update(int(r["id"]), status="failed", error=str(result["error"])[:500])
    elif result.get("gcs_uri"):
        await _mark_ready(int(r["id"]), result["gcs_uri"])
    else:
        await store.update(int(r["id"]), status="failed", error="done but no gcs_uri returned")


def _warn_parked(gen_ids: list[int]) -> None:
    """Say, at most every MISSING_KEYS_WARN_EVERY_SEC, which Seedance clips are
    waiting for Higgsfield keys in this process."""
    global _missing_keys_warned_at
    now = time.monotonic()
    if _missing_keys_warned_at is not None and now - _missing_keys_warned_at < MISSING_KEYS_WARN_EVERY_SEC:
        return
    _missing_keys_warned_at = now
    log.warning(
        "Higgsfield keys are not set in this process, so %d Seedance clip(s) cannot be "
        "finished here (ids %s). Add HIGGSFIELD_API_KEY_ID and HIGGSFIELD_API_KEY_SECRET "
        "to the .env of the server that runs the pollers.",
        len(gen_ids), ", ".join(str(i) for i in gen_ids[:20]),
    )


async def _poll_once() -> None:
    try:
        swept = await store.fail_stale_claims()
        if swept:
            log.warning("failed %d general-video claim(s) whose request never reached the engine", swept)
    except Exception:
        log.exception("yas_general_video_poller: stale-claim sweep failed")
    try:
        rows = await store.pending()
    except Exception:
        log.exception("yas_general_video_poller: pending-query failed")
        return
    if not rows:
        return
    log.debug("yas_general_video_poller: %d pending clip(s)", len(rows))
    parked: list[int] = []
    for r in rows:
        # One clip's trouble (a database hiccup, an odd answer) must not stop
        # the others, or end the loop.
        try:
            if r.get("engine") == "seedance":
                if not r.get("seedance_status_url"):
                    continue
                if not higgsfield.connected():
                    parked.append(int(r["id"]))  # keys removed mid-render: wait for them
                    continue
                await _poll_seedance(r)
            elif r.get("veo_operation_name"):
                await _poll_veo(r)
        except Exception:
            log.exception("gen=%s: poll failed; trying again next tick", r.get("id"))
    if parked:
        _warn_parked(parked)


async def _loop() -> None:
    log.info("yas_general_video_poller started (every %ds)", POLL_INTERVAL_SEC)
    try:
        while True:
            await _poll_once()
            await asyncio.sleep(POLL_INTERVAL_SEC)
    except asyncio.CancelledError:
        log.info("yas_general_video_poller cancelled")
        raise
    except Exception:
        log.exception("yas_general_video_poller loop crashed")


def start() -> asyncio.Task:
    global _task
    if _task is not None and not _task.done():
        return _task
    _task = asyncio.create_task(_loop(), name="yas_general_video_poller")
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
