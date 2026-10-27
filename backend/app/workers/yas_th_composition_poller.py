"""Background worker that stitches Yas Talking Head final compositions.

Runs alongside `veo_poller` and `yas_animations_poller`. Every 15s it
scans `yas_generations` for talking-head runs whose Veo clip just landed,
posts a render job to the Remotion service on `:3001`, then watches for
the render to finish and patches the row's metadata.

State machine (lives on `yas_generations.metadata.composition_status`):

    queued     — finalize() ran; Veo is still generating.
    rendering  — Veo is ready; Remotion job submitted.
    ready      — Remotion produced the MP4; composition_path is set.
    failed     — something went wrong; metadata.composition_error set.

The Remotion service is already wired for producer_compositions. We add a
new template_id `talking_head_with_cards` (registered in
`services/remotion/server.ts`) that takes intro / main_video / outro paths
and returns a stitched 9:16 MP4.
"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Optional

import httpx
from psycopg.types.json import Jsonb

from ..config import config
from ..services.storage import first_existing
from ..db import get_conn

log = logging.getLogger("archivist.yas_th_composition_poller")

POLL_INTERVAL_SEC = 15
# Configurable so the backend can reach the Remotion sidecar by service name in
# Docker (REMOTION_BASE_URL=http://remotion:3001); defaults to local dev.
REMOTION_BASE_URL = config.remotion_base_url.rstrip("/")
RENDER_TIMEOUT_SEC = 60.0  # only the kickoff request — the render itself is async
# The Remotion server mounts backend/data at /data — convert local file paths
# to URLs the headless Chrome renderer can fetch.
DATA_URL_PREFIX = f"{REMOTION_BASE_URL}/data"

_task: asyncio.Task | None = None


# Status constants — must match `yas_talking_head.py`. Kept inline here to
# avoid a circular import (services/yas_talking_head imports from this
# package's neighbours).
COMP_QUEUED = "queued"
COMP_VIDEOS_READY = "videos_ready"   # all 3 Veos done; awaiting user confirm
COMP_RENDERING = "rendering"
COMP_READY = "ready"
COMP_FAILED = "failed"
MODE_PREVIEW = "talking_head_preview"
ANIM_STATUS_READY = "ready"
ANIM_STATUS_GENERATING = "generating"
ANIM_STATUS_FAILED = "failed"

# Output dir for composed MP4s — under the shared producer data root so the
# Remotion service (which mounts the same dir) can write the result.
COMP_DIR = (config.asset_root.parent / "yas_producer" / "talking-head").resolve()


def _abs_from_rel(rel_or_abs: str) -> Path:
    p = Path(rel_or_abs)
    if p.is_absolute():
        return p
    # Local-mode uploads store backend/-relative paths; see first_existing.
    return first_existing(config.asset_root.parent / p, config.producer_assets_root.parent.parent / p)


def _to_rel(p: Path) -> str:
    try:
        return str(p.relative_to(config.asset_root.parent))
    except ValueError:
        return str(p)


def _to_remotion_url(p: Path | None) -> str | None:
    """Convert a local filesystem path under backend/data/ to a URL the
    Remotion renderer (headless Chrome) can fetch via the /data static mount.
    """
    if not p:
        return None
    try:
        rel = p.relative_to(config.asset_root.parent)
    except ValueError:
        # File outside backend/data — fall back to absolute path; renderer
        # may still resolve it on local dev (Remotion accepts file:// in some
        # configs) but generally this is a misconfiguration we should log.
        log.warning(
            "talking-head input %s is outside backend/data — Remotion may "
            "not be able to fetch it. Move the file under backend/data/.",
            p,
        )
        return None
    # POSIX separators for URL path component; quote unsafe chars.
    from urllib.parse import quote
    url_path = "/".join(quote(part) for part in rel.parts)
    return f"{DATA_URL_PREFIX}/{url_path}"


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

async def _pending_queued() -> list[dict]:
    """Runs whose Veos are still generating — check if all 3 have landed."""
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT id, topic, local_path, metadata
                  FROM yas_generations
                 WHERE mode = %s
                   AND metadata->>'composition_status' = %s
                """,
                (MODE_PREVIEW, COMP_QUEUED),
            )
            return await cur.fetchall()


async def _pending_rendering_unsubmitted() -> list[dict]:
    """User clicked Stitch (status=rendering) but no render_job_id yet —
    need to submit the Remotion job."""
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT id, topic, local_path, metadata
                  FROM yas_generations
                 WHERE mode = %s
                   AND metadata->>'composition_status' = %s
                   AND NOT (metadata ? 'render_job_id')
                """,
                (MODE_PREVIEW, COMP_RENDERING),
            )
            return await cur.fetchall()


async def _pending_rendering_submitted() -> list[dict]:
    """Remotion job is in flight — poll its status."""
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT id, metadata
                  FROM yas_generations
                 WHERE mode = %s
                   AND metadata->>'composition_status' = %s
                   AND metadata ? 'render_job_id'
                """,
                (MODE_PREVIEW, COMP_RENDERING),
            )
            return await cur.fetchall()


async def _fetch_card_path(card_id: int | None) -> Optional[Path]:
    if not card_id:
        return None
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT local_path FROM yas_generations WHERE id = %s",
                (card_id,),
            )
            row = await cur.fetchone()
    if not row or not row.get("local_path"):
        return None
    p = _abs_from_rel(row["local_path"])
    return p if p.exists() else None


async def _fetch_asset_path(asset_id: int | None) -> Optional[Path]:
    """For intro/outro slots that point at a producer_assets row (when the
    user picked a reference asset instead of auto-generating)."""
    if not asset_id:
        return None
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT storage_bucket, storage_path, local_path
                  FROM producer_assets
                 WHERE id = %s
                """,
                (asset_id,),
            )
            row = await cur.fetchone()
    if not row:
        return None
    if row.get("local_path"):
        from ..services import asset_storage
        p = asset_storage.local_to_abs(row["local_path"])
        if p.exists():
            return p
    # We could download from the bucket here, but the talking-head pipeline
    # has already cached intro/outro asset bytes via _resolve_asset_path
    # during script gen. If that cache miss happens, fall back to bucket.
    if row.get("storage_bucket") and row.get("storage_path"):
        from ..services import asset_storage
        # Fetched below by this worker, so the internal origin (no hairpin).
        signed = await asset_storage.url_for(
            storage_bucket=row["storage_bucket"],
            storage_path=row["storage_path"],
            local_path=None, expires=600, internal=True,
        )
        if not signed:
            return None
        cache_dir = COMP_DIR / "refs_cache"
        cache_dir.mkdir(parents=True, exist_ok=True)
        ext = Path(row["storage_path"]).suffix.lstrip(".") or "jpg"
        cached = cache_dir / f"asset-{asset_id}.{ext}"
        if not cached.exists():
            async with httpx.AsyncClient(timeout=60.0) as client:
                r = await client.get(signed)
                if r.status_code >= 300:
                    return None
                cached.write_bytes(r.content)
        return cached
    return None


async def _fetch_veo_path(anim_id: int) -> Optional[Path]:
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT local_path, status FROM yas_animations WHERE id = %s",
                (anim_id,),
            )
            row = await cur.fetchone()
    if not row or row.get("status") != ANIM_STATUS_READY:
        return None
    lp = row.get("local_path")
    if not lp:
        return None
    p = _abs_from_rel(lp)
    return p if p.exists() else None


async def _patch_metadata(run_id: int, patch: dict) -> None:
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                UPDATE yas_generations
                   SET metadata = metadata || %s::jsonb,
                       updated_at = NOW()
                 WHERE id = %s
                """,
                (Jsonb(patch), run_id),
            )


# ---------------------------------------------------------------------------
# Phase 1 — queued: wait for all 3 Veos, then flip to videos_ready
# ---------------------------------------------------------------------------

async def _check_videos_ready(run: dict) -> None:
    """For runs in `queued` state: poll the linked yas_animations to see
    if all expected Veos have landed. When they have, flip the run to
    `videos_ready` so the UI can show the preview-before-stitch step.
    """
    run_id = int(run["id"])
    md = run.get("metadata") or {}

    # Collect the animation ids that were created by start_videos.
    expected_anim_ids: list[int] = []
    # v6 — multi-scene runs persist N main scene animations in
    # metadata.scenes[*].animation_id PLUS intro/outro on the run.
    scene_count = int(md.get("scene_count") or 1)
    if scene_count > 1:
        for sm in (md.get("scenes") or []):
            aid = sm.get("animation_id")
            if aid:
                expected_anim_ids.append(int(aid))
        for key in ("intro_animation_id", "outro_animation_id"):
            aid = md.get(key)
            if aid:
                expected_anim_ids.append(int(aid))
    else:
        for key in ("main_animation_id", "intro_animation_id", "outro_animation_id"):
            aid = md.get(key)
            if aid:
                expected_anim_ids.append(int(aid))

    # Fallback for legacy single-anim runs that pre-date the 3-Veo refactor.
    if not expected_anim_ids and md.get("veo_animation_id"):
        expected_anim_ids.append(int(md["veo_animation_id"]))

    if not expected_anim_ids:
        log.debug("run=%d queued but no animation ids yet; skipping", run_id)
        return

    # Check statuses
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT id, status, error FROM yas_animations WHERE id = ANY(%s)",
                (expected_anim_ids,),
            )
            rows = await cur.fetchall()
    by_id = {int(r["id"]): r for r in rows}
    for aid in expected_anim_ids:
        anim = by_id.get(aid)
        if not anim:
            await _patch_metadata(run_id, {
                "composition_status": COMP_FAILED,
                "composition_error": f"animation {aid} missing",
            })
            return
        st = anim.get("status")
        if st == ANIM_STATUS_FAILED:
            await _patch_metadata(run_id, {
                "composition_status": COMP_FAILED,
                "composition_error": (
                    f"veo animation {aid} failed: {(anim.get('error') or '')[:240]}"
                ),
            })
            return
        if st == ANIM_STATUS_GENERATING:
            return  # still in flight; check next tick
    # All animations are status='ready' — surface to the user.
    await _patch_metadata(run_id, {"composition_status": COMP_VIDEOS_READY})
    log.info(
        "run=%d videos_ready (%d anims done) — user can preview before stitch",
        run_id, len(expected_anim_ids),
    )


# ---------------------------------------------------------------------------
# Phase 2 — rendering (unsubmitted): user pressed Stitch, submit Remotion
# ---------------------------------------------------------------------------

async def _kick_render_multi(run_id: int, md: dict, refs: dict) -> None:
    """v6 — submit the multi-scene Remotion job. Each scene's Veo clip
    is fetched as a /data URL; intro/outro fall back to still-card paths
    if the Veo bookend was skipped."""
    scenes = md.get("scenes") or []
    scene_count = len(scenes)
    if scene_count < 2:
        await _patch_metadata(run_id, {
            "composition_status": COMP_FAILED,
            "composition_error": "multi-scene render with <2 scenes",
        })
        return

    # Resolve each scene's Veo path. All must be ready.
    scene_paths: list[Path] = []
    scene_durs: list[int] = []
    for sm in scenes:
        aid = sm.get("animation_id")
        if not aid:
            await _patch_metadata(run_id, {
                "composition_status": COMP_FAILED,
                "composition_error": f"scene {sm.get('index')} missing animation_id",
            })
            return
        p = await _fetch_veo_path(int(aid))
        if not p:
            log.warning(
                "run=%d scene %d veo not on disk; retry next tick",
                run_id, sm.get("index"),
            )
            return
        scene_paths.append(p)
        # Each scene main clip = 8s with audio.
        async with get_conn() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT duration_sec FROM yas_animations WHERE id = %s",
                    (int(aid),),
                )
                r = await cur.fetchone()
        scene_durs.append(int(r["duration_sec"]) if r and r.get("duration_sec") else 8)

    # Intro / outro (same resolution logic as single-scene).
    intro_path: Optional[Path] = None
    if md.get("intro_animation_id"):
        intro_path = await _fetch_veo_path(int(md["intro_animation_id"]))
    if intro_path is None:
        if md.get("intro_image_id"):
            intro_path = await _fetch_card_path(int(md["intro_image_id"]))
        if intro_path is None and refs.get("intro_asset_id"):
            intro_path = await _fetch_asset_path(int(refs["intro_asset_id"]))

    outro_path: Optional[Path] = None
    if md.get("outro_animation_id"):
        outro_path = await _fetch_veo_path(int(md["outro_animation_id"]))
    if outro_path is None:
        if md.get("outro_image_id"):
            outro_path = await _fetch_card_path(int(md["outro_image_id"]))
        if outro_path is None and refs.get("outro_asset_id"):
            outro_path = await _fetch_asset_path(int(refs["outro_asset_id"]))

    intro_url = _to_remotion_url(intro_path)
    outro_url = _to_remotion_url(outro_path)
    scene_urls = [_to_remotion_url(p) for p in scene_paths]
    if any(u is None for u in scene_urls):
        await _patch_metadata(run_id, {
            "composition_status": COMP_FAILED,
            "composition_error": "one or more scene paths outside backend/data; renderer can't fetch",
        })
        return

    # Intro / outro durations from the animation rows (fall back to 4s).
    async def _anim_dur(anim_id: int | None, default: int) -> int:
        if not anim_id:
            return default
        async with get_conn() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT duration_sec FROM yas_animations WHERE id = %s",
                    (anim_id,),
                )
                r = await cur.fetchone()
        return int(r["duration_sec"]) if r and r.get("duration_sec") else default

    intro_dur = await _anim_dur(md.get("intro_animation_id"), 4)
    outro_dur = await _anim_dur(md.get("outro_animation_id"), 4)

    out_path = COMP_DIR / f"{run_id}" / "final.mp4"
    out_path.parent.mkdir(parents=True, exist_ok=True)

    payload = {
        "template": "talking-head-multi-scene",
        "props": {
            "intro_video_path": intro_url,
            "outro_video_path": outro_url,
            "scenes": [
                {"path": scene_urls[i], "duration_sec": float(scene_durs[i])}
                for i in range(scene_count)
            ],
            "intro_duration_sec": float(intro_dur),
            "outro_duration_sec": float(outro_dur),
            "fade_sec": 0.4,
        },
        "output_path": str(out_path),
    }

    try:
        async with httpx.AsyncClient(timeout=RENDER_TIMEOUT_SEC) as client:
            r = await client.post(f"{REMOTION_BASE_URL}/render", json=payload)
        if r.status_code >= 300:
            log.warning(
                "run=%d (multi) Remotion /render returned %d: %s",
                run_id, r.status_code, r.text[:240],
            )
            await _patch_metadata(run_id, {
                "composition_status": COMP_FAILED,
                "composition_error": f"remotion_kick_failed: {r.status_code} {r.text[:240]}",
            })
            return
        body = r.json()
        job_id = body.get("job_id") or body.get("id")
        if not job_id:
            log.warning("run=%d (multi) Remotion response had no job_id: %s", run_id, body)
            await _patch_metadata(run_id, {
                "composition_status": COMP_FAILED,
                "composition_error": "remotion response missing job_id",
            })
            return
        await _patch_metadata(run_id, {
            "render_job_id": str(job_id),
            "composition_output_path": _to_rel(out_path),
        })
        log.info(
            "run=%d multi-scene render kicked: job=%s scenes=%d intro=%s outro=%s",
            run_id, job_id, scene_count,
            "yes" if intro_path else "skipped",
            "yes" if outro_path else "skipped",
        )
    except Exception as e:
        log.exception("run=%d multi-scene Remotion kickoff crashed", run_id)
        await _patch_metadata(run_id, {
            "composition_status": COMP_FAILED,
            "composition_error": f"remotion_kick_crash: {str(e)[:240]}",
        })


async def _kick_render(run: dict) -> None:
    run_id = int(run["id"])
    md = run.get("metadata") or {}
    refs = md.get("refs") or {}
    scene_count = int(md.get("scene_count") or 1)

    # ----- v6 multi-scene render branch ---------------------------------
    if scene_count > 1:
        await _kick_render_multi(run_id, md, refs)
        return

    main_anim_id = md.get("main_animation_id") or md.get("veo_animation_id")
    if not main_anim_id:
        await _patch_metadata(run_id, {
            "composition_status": COMP_FAILED,
            "composition_error": "no main animation linked to this run",
        })
        return
    main_path = await _fetch_veo_path(int(main_anim_id))
    if not main_path:
        log.warning(
            "run=%d main veo file not yet on disk; retry next tick", run_id,
        )
        return

    intro_path: Optional[Path] = None
    if md.get("intro_animation_id"):
        intro_path = await _fetch_veo_path(int(md["intro_animation_id"]))
    if intro_path is None:
        # Fall back to the still card / user-provided reference (v1 path)
        if md.get("intro_image_id"):
            intro_path = await _fetch_card_path(int(md["intro_image_id"]))
        if intro_path is None and refs.get("intro_asset_id"):
            intro_path = await _fetch_asset_path(int(refs["intro_asset_id"]))

    outro_path: Optional[Path] = None
    if md.get("outro_animation_id"):
        outro_path = await _fetch_veo_path(int(md["outro_animation_id"]))
    if outro_path is None:
        if md.get("outro_image_id"):
            outro_path = await _fetch_card_path(int(md["outro_image_id"]))
        if outro_path is None and refs.get("outro_asset_id"):
            outro_path = await _fetch_asset_path(int(refs["outro_asset_id"]))

    intro_url = _to_remotion_url(intro_path)
    main_url = _to_remotion_url(main_path)
    outro_url = _to_remotion_url(outro_path)
    if not main_url:
        await _patch_metadata(run_id, {
            "composition_status": COMP_FAILED,
            "composition_error": "veo file not under backend/data; renderer cannot fetch",
        })
        return

    # Derive durations from the actual yas_animations rows when available
    # (intro/outro default 4s, main 8s).
    async def _anim_dur(anim_id: int | None, default: int) -> int:
        if not anim_id:
            return default
        async with get_conn() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT duration_sec FROM yas_animations WHERE id = %s",
                    (anim_id,),
                )
                r = await cur.fetchone()
        return int(r["duration_sec"]) if r and r.get("duration_sec") else default

    intro_dur = await _anim_dur(md.get("intro_animation_id"), 4)
    main_dur = await _anim_dur(main_anim_id, 8)
    outro_dur = await _anim_dur(md.get("outro_animation_id"), 4)

    out_path = COMP_DIR / f"{run_id}" / "final.mp4"
    out_path.parent.mkdir(parents=True, exist_ok=True)

    payload = {
        # NB: must be hyphenated — Remotion's validateCompositionId rejects
        # underscores (allowed chars: a-z, A-Z, 0-9, hyphen, CJK).
        "template": "talking-head-with-cards",
        "props": {
            # v2 — the three segments are videos. The legacy *_image_path
            # keys are kept null; the composition falls back to *_video_path.
            "intro_image_path": None,
            "intro_video_path": intro_url,
            "main_video_path": main_url,
            "outro_image_path": None,
            "outro_video_path": outro_url,
            "intro_duration_sec": float(intro_dur),
            "outro_duration_sec": float(outro_dur),
            "fade_sec": 0.4,
            "main_duration_sec": main_dur,
        },
        "output_path": str(out_path),
    }

    try:
        async with httpx.AsyncClient(timeout=RENDER_TIMEOUT_SEC) as client:
            r = await client.post(f"{REMOTION_BASE_URL}/render", json=payload)
        if r.status_code >= 300:
            log.warning(
                "run=%d Remotion /render returned %d: %s",
                run_id, r.status_code, r.text[:240],
            )
            await _patch_metadata(run_id, {
                "composition_status": COMP_FAILED,
                "composition_error": f"remotion_kick_failed: {r.status_code} {r.text[:240]}",
            })
            return
        body = r.json()
        job_id = body.get("job_id") or body.get("id")
        if not job_id:
            log.warning("run=%d Remotion response had no job_id: %s", run_id, body)
            await _patch_metadata(run_id, {
                "composition_status": COMP_FAILED,
                "composition_error": "remotion response missing job_id",
            })
            return
        await _patch_metadata(run_id, {
            "render_job_id": str(job_id),
            "composition_output_path": _to_rel(out_path),
        })
        log.info(
            "run=%d render kicked: job=%s out=%s intro=%s outro=%s",
            run_id, job_id, _to_rel(out_path),
            "yes" if intro_path else "skipped",
            "yes" if outro_path else "skipped",
        )
    except Exception as e:
        log.exception("run=%d Remotion kickoff crashed", run_id)
        await _patch_metadata(run_id, {
            "composition_status": COMP_FAILED,
            "composition_error": f"remotion_kick_crash: {str(e)[:240]}",
        })


# ---------------------------------------------------------------------------
# Status check — rendering → ready / failed
# ---------------------------------------------------------------------------

async def _check_render(run: dict) -> None:
    run_id = int(run["id"])
    md = run.get("metadata") or {}
    job_id = md.get("render_job_id")
    out_rel = md.get("composition_output_path")
    if not job_id or not out_rel:
        return

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(f"{REMOTION_BASE_URL}/jobs/{job_id}")
        if r.status_code >= 300:
            log.warning(
                "run=%d render-job %s returned %d", run_id, job_id, r.status_code,
            )
            return
        body = r.json()
    except Exception:
        log.exception("run=%d render-job check crashed", run_id)
        return

    status = (body.get("status") or "").lower()
    if status in {"pending", "running", "rendering", "queued"}:
        return
    if status in {"ready", "done", "success", "completed"}:
        out_path = _abs_from_rel(out_rel)
        if not out_path.exists():
            log.warning(
                "run=%d Remotion reported %s but file not on disk yet at %s",
                run_id, status, out_path,
            )
            return
        await _patch_metadata(run_id, {
            "composition_status": COMP_READY,
            "composition_path": out_rel,
            "composition_cost_usd": float(body.get("cost_usd") or 0.0),
        })
        log.info("run=%d composition ready: %s", run_id, out_rel)
        return
    if status in {"failed", "error"}:
        await _patch_metadata(run_id, {
            "composition_status": COMP_FAILED,
            "composition_error": (body.get("error") or "remotion failed")[:240],
        })
        log.warning("run=%d composition failed: %s", run_id, body.get("error"))
        return
    # Unknown status — log once, leave the row alone
    log.debug("run=%d render-job %s reported unknown status %r", run_id, job_id, status)


# ---------------------------------------------------------------------------
# Poll loop
# ---------------------------------------------------------------------------

async def _poll_once() -> None:
    # Phase 1 — runs still waiting for their Veos. Check if all 3 are done
    # and flip to `videos_ready` (the user-confirm checkpoint).
    try:
        queued = await _pending_queued()
    except Exception:
        log.exception("yas_th_composition_poller: queued-query failed")
        queued = []
    for run in queued:
        try:
            await _check_videos_ready(run)
        except Exception:
            log.exception("yas_th: check_videos_ready crashed for run=%s", run.get("id"))

    # Phase 2 — user pressed Stitch (composition_status=rendering, no
    # render_job_id yet). Submit the Remotion job.
    try:
        to_submit = await _pending_rendering_unsubmitted()
    except Exception:
        log.exception("yas_th_composition_poller: rendering-unsubmitted query failed")
        to_submit = []
    for run in to_submit:
        try:
            await _kick_render(run)
        except Exception:
            log.exception("yas_th: kick render crashed for run=%s", run.get("id"))

    # Phase 3 — Remotion job in flight; check its status.
    try:
        rendering = await _pending_rendering_submitted()
    except Exception:
        log.exception("yas_th_composition_poller: rendering-submitted query failed")
        rendering = []
    for run in rendering:
        try:
            await _check_render(run)
        except Exception:
            log.exception("yas_th: check render crashed for run=%s", run.get("id"))


async def _loop() -> None:
    log.info("yas_th_composition_poller started (every %ds)", POLL_INTERVAL_SEC)
    try:
        while True:
            await _poll_once()
            await asyncio.sleep(POLL_INTERVAL_SEC)
    except asyncio.CancelledError:
        log.info("yas_th_composition_poller cancelled")
        raise
    except Exception:
        log.exception("yas_th_composition_poller loop crashed")


def start() -> asyncio.Task:
    global _task
    if _task is not None and not _task.done():
        return _task
    _task = asyncio.create_task(_loop(), name="yas_th_composition_poller")
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
