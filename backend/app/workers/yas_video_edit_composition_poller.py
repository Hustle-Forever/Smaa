"""Background worker that renders Yas Video-Editor exports.

A focused twin of `yas_th_v2_composition_poller` (kept separate so the Talking
Head path stays untouched). Every 15s it:

  1. finds video-edit projects flagged `render.composition_status = 'rendering'`
     with no `render_job_id` yet → builds the `video-edit` composition props
     (sources resolved to /data URLs) and POSTs a render job to Remotion :3001,
  2. finds rendering projects with a `render_job_id` → polls /jobs/:id and, on
     completion, patches the render sub-state to `ready` (or `failed`).

State lives at metadata.video_edit.render.composition_status:
    draft → rendering → ready / failed
"""
from __future__ import annotations

import asyncio
import logging

import httpx

from ..config import config
from ..db import get_conn
from ..services import asset_storage
from ..services import yas_video_editor as ve

log = logging.getLogger("archivist.yas_video_edit_composition_poller")


async def _mark_ready(project_id: int, out_rel: str) -> None:
    """Flip the export to `ready` AND back the MP4 up to the durable bucket.

    Remotion only writes the file to the ephemeral container disk; persisting it
    means a redeploy that wipes the disk can't 410 the finished video (the file
    route recovers from the bucket). Bucket failure degrades to local-only."""
    patch = {"composition_status": ve.COMP_READY, "composition_path": out_rel}
    bucket, key = await asset_storage.persist_local_file(
        abs_path=ve._abs_from_rel(out_rel),
        bucket_key=f"yas/compositions/video-editor/{project_id}.mp4",
    )
    if bucket and key:
        patch["composition_storage_bucket"] = bucket
        patch["composition_storage_path"] = key
    await ve._patch_render(project_id, patch)

POLL_INTERVAL_SEC = 15
# Configurable so the backend can reach the Remotion sidecar by service name in
# Docker (REMOTION_BASE_URL=http://remotion:3001); defaults to local dev.
REMOTION_BASE_URL = config.remotion_base_url.rstrip("/")
TEMPLATE = "video-edit"
# How many consecutive /jobs/:id 404s (with NO output file yet) to tolerate
# before declaring the job genuinely lost. A just-submitted job can 404 for a
# tick or two before Remotion re-surfaces it; failing on the first 404 wrongly
# kills renders that actually complete. ~4 ticks ≈ 60s.
_RENDER_404_GIVEUP = 4

_task: asyncio.Task | None = None


async def _pending_unsubmitted() -> list[dict]:
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT id
                  FROM yas_generations
                 WHERE mode = %s
                   AND COALESCE(metadata->'video_edit'->'render'->>'render_owner', '') = %s
                   AND COALESCE(metadata->'video_edit'->'render'->>'render_job_id', '') = ''
                   AND metadata->'video_edit'->'render'->>'composition_status' IN (%s, %s)
                   AND COALESCE((metadata->'video_edit'->'render'->>'render_kick_misses')::int, 0) < %s
                """,
                # Pick up our own un-submitted renders — INCLUDING ones a competing
                # instance (shared DB, no local media) wrongly marked 'failed'
                # before we could render them. We re-kick (reclaim) up to the miss
                # cap; once WE submit, render_job_id is set so it leaves this set.
                (ve.MODE_PREVIEW, ve.INSTANCE_ID, ve.COMP_RENDERING, ve.COMP_FAILED, _RENDER_404_GIVEUP),
            )
            return await cur.fetchall()


async def _pending_submitted() -> list[dict]:
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT id, metadata->'video_edit'->'render'->>'render_job_id' AS job_id,
                       metadata->'video_edit'->'render'->>'composition_output_path' AS out_rel
                  FROM yas_generations
                 WHERE mode = %s
                   AND metadata->'video_edit'->'render'->>'composition_status' = %s
                   AND COALESCE(metadata->'video_edit'->'render'->>'render_job_id', '') <> ''
                   AND COALESCE(metadata->'video_edit'->'render'->>'render_owner', '') = %s
                """,
                (ve.MODE_PREVIEW, ve.COMP_RENDERING, ve.INSTANCE_ID),
            )
            return await cur.fetchall()


async def _kick_render(run: dict) -> None:
    project_id = int(run["id"])
    try:
        props, out_path = await ve.build_render_props(project_id)
    except Exception:
        log.exception("[yas-ve] build props failed for project=%d", project_id)
        await ve._patch_render(project_id, {
            "composition_status": ve.COMP_FAILED,
            "composition_error": "failed to assemble timeline",
        })
        return

    clips = props.get("clips", [])
    # Renderable = sourceless layers (text/shape) OR media clips whose source
    # resolved. A freshly-imported upload can momentarily fail to resolve (the
    # file/db write races the poller read), so DON'T fail on the first empty
    # pass — leave the project queued and retry a few ticks. Only give up if it
    # stays empty (a genuinely sourceless timeline).
    renderable = sum(1 for c in clips if ve.clip_is_renderable(c))
    if clips and renderable == 0:
        fresh = await ve._fetch_row(project_id)
        render = (ve._video_edit(fresh["metadata"]) or {}).get("render") or {}
        kmiss = int(render.get("render_kick_misses") or 0) + 1
        if kmiss >= _RENDER_404_GIVEUP:
            await ve._patch_render(project_id, {
                "composition_status": ve.COMP_FAILED,
                "composition_error": "no clip sources could be resolved",
            })
            log.warning("[yas-ve] project=%d export aborted — 0 renderable clips after %d tries", project_id, kmiss)
        else:
            await ve._patch_render(project_id, {"render_kick_misses": kmiss})
            log.info("[yas-ve] project=%d 0 renderable (%d/%d) — retrying next tick", project_id, kmiss, _RENDER_404_GIVEUP)
        return

    out_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"template": TEMPLATE, "output_path": str(out_path), "props": props}
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            r = await client.post(f"{REMOTION_BASE_URL}/render", json=payload)
        if r.status_code >= 300:
            raise RuntimeError(f"remotion /render {r.status_code}: {r.text[:240]}")
        job_id = r.json().get("job_id")
    except Exception as e:  # noqa: BLE001
        log.exception("[yas-ve] render kickoff failed for project=%d", project_id)
        await ve._patch_render(project_id, {
            "composition_status": ve.COMP_FAILED,
            "composition_error": f"render kickoff failed: {e}"[:240],
        })
        return

    await ve._patch_render(project_id, {
        "composition_status": ve.COMP_RENDERING,  # in case we reclaimed a 'failed' one
        "composition_error": None,
        "render_job_id": job_id,
        "composition_output_path": ve._to_rel(out_path),
        "render_check_misses": 0,  # fresh job → reset the 404 tolerance counter
        "render_kick_misses": 0,
    })
    log.info("[yas-ve] project=%d render submitted job=%s (%d clips)", project_id, job_id, renderable)


async def _check_render(run: dict) -> None:
    project_id = int(run["id"])
    job_id = run.get("job_id")
    out_rel = run.get("out_rel")
    if not job_id or not out_rel:
        return
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(f"{REMOTION_BASE_URL}/jobs/{job_id}")
        log.info("[yas-ve] check project=%d http=%d", project_id, r.status_code)
        if r.status_code == 404:
            # Don't treat a single 404 as fatal — the render may have just been
            # submitted (Remotion not surfacing it yet) or finished. If the
            # output file is already on disk, the render succeeded → accept it.
            out_path = ve._abs_from_rel(out_rel)
            if out_path.exists():
                await _mark_ready(project_id, out_rel)
                log.info("[yas-ve] project=%d job 404 but output present → ready", project_id)
                return
            # No file yet → retry a few ticks before giving up.
            fresh = await ve._fetch_row(project_id)
            render = (ve._video_edit(fresh["metadata"]) or {}).get("render") or {}
            misses = int(render.get("render_check_misses") or 0) + 1
            if misses >= _RENDER_404_GIVEUP:
                await ve._patch_render(project_id, {
                    "composition_status": ve.COMP_FAILED,
                    "composition_error": "render job lost (service restart?)",
                })
                log.warning("[yas-ve] project=%d render job lost after %d 404s", project_id, misses)
            else:
                await ve._patch_render(project_id, {"render_check_misses": misses})
                log.info("[yas-ve] project=%d job 404 (%d/%d) — retrying", project_id, misses, _RENDER_404_GIVEUP)
            return
        if r.status_code >= 300:
            return
        body = r.json()
    except Exception:
        log.exception("[yas-ve] project=%d render check crashed", project_id)
        return

    status = (body.get("status") or "").lower()
    if status in {"pending", "running", "rendering", "queued"}:
        return
    if status in {"ready", "done", "success", "completed"}:
        out_path = ve._abs_from_rel(out_rel)
        if not out_path.exists():
            log.warning("[yas-ve] project=%d reported %s but file missing at %s", project_id, status, out_path)
            return
        await _mark_ready(project_id, out_rel)
        log.info("[yas-ve] project=%d export ready: %s", project_id, out_rel)
        return
    if status in {"failed", "error"}:
        await ve._patch_render(project_id, {
            "composition_status": ve.COMP_FAILED,
            "composition_error": (body.get("error") or "remotion failed")[:240],
        })
        log.warning("[yas-ve] project=%d export failed: %s", project_id, body.get("error"))


async def _poll_once() -> None:
    try:
        for run in await _pending_unsubmitted():
            try:
                await _kick_render(run)
            except Exception:
                log.exception("[yas-ve] kick render crashed for project=%s", run.get("id"))
    except Exception:
        log.exception("[yas-ve] unsubmitted query failed")

    try:
        for run in await _pending_submitted():
            try:
                await _check_render(run)
            except Exception:
                log.exception("[yas-ve] check render crashed for project=%s", run.get("id"))
    except Exception:
        log.exception("[yas-ve] submitted query failed")


async def _loop() -> None:
    log.info("yas_video_edit_composition_poller started (every %ds)", POLL_INTERVAL_SEC)
    try:
        while True:
            await _poll_once()
            await asyncio.sleep(POLL_INTERVAL_SEC)
    except asyncio.CancelledError:
        log.info("yas_video_edit_composition_poller cancelled")
        raise
    except Exception:
        log.exception("yas_video_edit_composition_poller loop crashed")


def start() -> asyncio.Task:
    global _task
    if _task is not None and not _task.done():
        return _task
    _task = asyncio.create_task(_loop(), name="yas_video_edit_composition_poller")
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
