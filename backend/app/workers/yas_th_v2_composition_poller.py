"""Background worker that stitches Yas Talking Head final compositions.

Runs alongside `veo_poller` and `yas_animations_poller`. Every 15s it
scans `yas_generations` for talking-head-v2 runs whose Veo clip just landed,
posts a render job to the Remotion service on `:3001`, then watches for
the render to finish and patches the row's metadata.

State machine (lives on `yas_generations.metadata.composition_status`):

    queued     — finalize() ran; Veo is still generating.
    rendering  — Veo is ready; Remotion job submitted.
    ready      — Remotion produced the MP4; composition_path is set.
    failed     — something went wrong; metadata.composition_error set.

A reel finalized by services/th_reel (Finalize, which can run again) carries
`render_owner` = the instance that finalized it and starts with
`render_job_id: ""`: only that instance's poller submits it (the frozen
`composition_plan.payload`, to final-<seq>.mp4) and watches it; a failed
re-render goes back to `ready` with the last good reel and
`composition_error`. Rows without `render_owner` take the code below exactly
as before (old code on another machine never picks a new row up: it selects
work by `NOT (metadata ? 'render_job_id')` and skips an empty job id).

The Remotion service is already wired for producer_compositions. We add a
new template_id `talking_head_with_cards` (registered in
`services/remotion/server.ts`) that takes intro / main_video / outro paths
and returns a stitched 9:16 MP4.
"""
from __future__ import annotations

import asyncio
import copy
import logging
import re
from pathlib import Path
from typing import Optional

import httpx
from psycopg.types.json import Jsonb

from ..config import config
from ..services.storage import first_existing
from ..db import get_conn

log = logging.getLogger("archivist.yas_th_v2_composition_poller")

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
MODE_PREVIEW = "talking_head_v2_preview"
ANIM_STATUS_READY = "ready"
ANIM_STATUS_GENERATING = "generating"
ANIM_STATUS_FAILED = "failed"

# Output dir for composed MP4s — under the shared producer data root so the
# Remotion service (which mounts the same dir) can write the result.
COMP_DIR = (config.asset_root.parent / "yas_producer" / "talking-head-v2").resolve()


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
            "talking-head-v2 input %s is outside backend/data — Remotion may "
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


async def _pending_rendering_owned() -> list[dict]:
    """A Finalize of THIS instance (services/th_reel): rendering, owned here,
    not submitted yet (its render_job_id is the empty string)."""
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT id, topic, local_path, metadata
                  FROM yas_generations
                 WHERE mode = %s
                   AND metadata->>'composition_status' = %s
                   AND metadata->>'render_owner' = %s
                   AND COALESCE(metadata->>'render_job_id', '') = ''
                """,
                (MODE_PREVIEW, COMP_RENDERING, _instance()),
            )
            return await cur.fetchall()


async def _pending_rendering_submitted() -> list[dict]:
    """Remotion job is in flight — poll its status. A Finalize's render is
    watched only by the instance that owns it (the file is on its disk)."""
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT id, metadata
                  FROM yas_generations
                 WHERE mode = %s
                   AND metadata->>'composition_status' = %s
                   AND metadata ? 'render_job_id'
                   AND COALESCE(metadata->>'render_job_id', '') <> ''
                   AND (NOT (metadata ? 'render_owner') OR metadata->>'render_owner' = %s)
                """,
                (MODE_PREVIEW, COMP_RENDERING, _instance()),
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


async def _fetch_upload_path(upload_id: int | None) -> Optional[Path]:
    """Resolve a yas_uploads row to a local Path — for a DIRECT-video intro/outro
    clip the user uploaded, which is stitched in as-is (no card-gen / Veo)."""
    if not upload_id:
        return None
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT local_path FROM yas_uploads WHERE id = %s",
                (int(upload_id),),
            )
            row = await cur.fetchone()
    if not row or not row.get("local_path"):
        return None
    p = _abs_from_rel(row["local_path"])
    return p if p.exists() else None


def _probe_video_duration(path: Path) -> Optional[float]:
    """Duration (sec) of a video via the bundled imageio-ffmpeg binary — parses
    ffmpeg's stderr 'Duration: HH:MM:SS.ss' line (ffmpeg with no output file
    errors out but still prints the header). Returns None on failure."""
    try:
        import re
        import subprocess

        import imageio_ffmpeg

        ff = imageio_ffmpeg.get_ffmpeg_exe()
        proc = subprocess.run(
            [ff, "-i", str(path)], capture_output=True, text=True, timeout=30,
        )
        m = re.search(r"Duration:\s*(\d+):(\d+):(\d+\.?\d*)", proc.stderr or "")
        if not m:
            return None
        h, mnt, s = int(m.group(1)), int(m.group(2)), float(m.group(3))
        return h * 3600 + mnt * 60 + s
    except Exception:
        return None


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
    # We could download from the bucket here, but the talking-head-v2 pipeline
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
    # v2 editor — EXCLUDED clips are intentionally left out of the stitch, so
    # don't wait on them (and a failed excluded clip must not fail the run).
    intro_excluded = bool(md.get("intro_excluded"))
    outro_excluded = bool(md.get("outro_excluded"))
    if scene_count > 1:
        for sm in (md.get("scenes") or []):
            if sm.get("excluded"):
                continue
            aid = sm.get("animation_id")
            if aid:
                expected_anim_ids.append(int(aid))
        if not intro_excluded and md.get("intro_animation_id"):
            expected_anim_ids.append(int(md["intro_animation_id"]))
        if not outro_excluded and md.get("outro_animation_id"):
            expected_anim_ids.append(int(md["outro_animation_id"]))
    else:
        if md.get("main_animation_id"):
            expected_anim_ids.append(int(md["main_animation_id"]))
        if not intro_excluded and md.get("intro_animation_id"):
            expected_anim_ids.append(int(md["intro_animation_id"]))
        if not outro_excluded and md.get("outro_animation_id"):
            expected_anim_ids.append(int(md["outro_animation_id"]))

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
    all_scenes = md.get("scenes") or []
    # v2 editor — only INCLUDED scenes go into the stitch. Excluded scenes are
    # intentionally skipped (not "missing"), and durations recompute from the
    # included set so the reel shortens correctly.
    scenes = [sm for sm in all_scenes if not sm.get("excluded")]
    scene_count = len(scenes)
    if scene_count < 1:
        await _patch_metadata(run_id, {
            "composition_status": COMP_FAILED,
            "composition_error": "multi-scene render with no included scenes",
        })
        return

    # Resolve each included scene's Veo path. All must be ready.
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

    # Intro / outro (same resolution logic as single-scene: an uploaded video
    # as it is first, at its full length). Skipped entirely when the user
    # excluded that bookend → null path → omitted by Remotion.
    intro_path: Optional[Path] = None
    intro_is_direct_video = False
    if not md.get("intro_excluded"):
        if refs.get("intro_video_upload_id"):
            intro_path = await _fetch_upload_path(int(refs["intro_video_upload_id"]))
            intro_is_direct_video = intro_path is not None
        if intro_path is None and md.get("intro_animation_id"):
            intro_path = await _fetch_veo_path(int(md["intro_animation_id"]))
        if intro_path is None:
            if md.get("intro_image_id"):
                intro_path = await _fetch_card_path(int(md["intro_image_id"]))
            if intro_path is None and refs.get("intro_asset_id"):
                intro_path = await _fetch_asset_path(int(refs["intro_asset_id"]))

    outro_path: Optional[Path] = None
    outro_is_direct_video = False
    if not md.get("outro_excluded"):
        if refs.get("outro_video_upload_id"):
            outro_path = await _fetch_upload_path(int(refs["outro_video_upload_id"]))
            outro_is_direct_video = outro_path is not None
        if outro_path is None and md.get("outro_animation_id"):
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

    # A direct-video bookend plays its FULL length (as single-scene).
    if intro_is_direct_video and intro_path is not None:
        intro_dur = _probe_video_duration(intro_path) or intro_dur
    if outro_is_direct_video and outro_path is not None:
        outro_dur = _probe_video_duration(outro_path) or outro_dur

    out_path = COMP_DIR / f"{run_id}" / "final.mp4"
    out_path.parent.mkdir(parents=True, exist_ok=True)

    comp_w, comp_h = (1920, 1080) if md.get("aspect_ratio") == "16:9" else (1080, 1920)
    payload = {
        "template": "talking-head-v2-multi-scene",
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
            "width": comp_w,
            "height": comp_h,
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

    # v2 editor — bookends omitted when excluded (null path → Remotion skips).
    intro_path: Optional[Path] = None
    intro_is_direct_video = False
    if not md.get("intro_excluded"):
        # Highest priority: a user-uploaded video used AS-IS (no card-gen / Veo).
        if refs.get("intro_video_upload_id"):
            intro_path = await _fetch_upload_path(int(refs["intro_video_upload_id"]))
            intro_is_direct_video = intro_path is not None
        if intro_path is None and md.get("intro_animation_id"):
            intro_path = await _fetch_veo_path(int(md["intro_animation_id"]))
        if intro_path is None:
            # Fall back to the still card / user-provided reference (v1 path)
            if md.get("intro_image_id"):
                intro_path = await _fetch_card_path(int(md["intro_image_id"]))
            if intro_path is None and refs.get("intro_asset_id"):
                intro_path = await _fetch_asset_path(int(refs["intro_asset_id"]))

    outro_path: Optional[Path] = None
    outro_is_direct_video = False
    if not md.get("outro_excluded"):
        if refs.get("outro_video_upload_id"):
            outro_path = await _fetch_upload_path(int(refs["outro_video_upload_id"]))
            outro_is_direct_video = outro_path is not None
        if outro_path is None and md.get("outro_animation_id"):
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

    # A direct-video bookend plays its FULL length — probe the uploaded clip and
    # use its real duration (falls back to the default if the probe fails).
    if intro_is_direct_video and intro_path is not None:
        intro_dur = _probe_video_duration(intro_path) or intro_dur
    if outro_is_direct_video and outro_path is not None:
        outro_dur = _probe_video_duration(outro_path) or outro_dur

    out_path = COMP_DIR / f"{run_id}" / "final.mp4"
    out_path.parent.mkdir(parents=True, exist_ok=True)

    comp_w, comp_h = (1920, 1080) if md.get("aspect_ratio") == "16:9" else (1080, 1920)
    payload = {
        # NB: must be hyphenated — Remotion's validateCompositionId rejects
        # underscores (allowed chars: a-z, A-Z, 0-9, hyphen, CJK).
        "template": "talking-head-v2-with-cards",
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
            "width": comp_w,
            "height": comp_h,
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
    if md.get("render_owner"):
        await _check_plan_render(run)
        return
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
        # Back the stitched MP4 up to the durable bucket so a redeploy that wipes
        # the ephemeral disk can't 410 the finished video (the file route then
        # recovers from the bucket). Bucket failure degrades to local-only.
        from ..services import asset_storage
        patch = {
            "composition_status": COMP_READY,
            "composition_path": out_rel,
            "composition_cost_usd": float(body.get("cost_usd") or 0.0),
        }
        bucket, key = await asset_storage.persist_local_file(
            abs_path=out_path,
            bucket_key=f"yas/compositions/talking-head/{run_id}.mp4",
        )
        if bucket and key:
            patch["composition_storage_bucket"] = bucket
            patch["composition_storage_path"] = key
        await _patch_metadata(run_id, patch)
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
# A Finalize's render (services/th_reel): the frozen plan, final-<seq>.mp4
# ---------------------------------------------------------------------------

# How many consecutive /jobs/:id 404s (the job gone: Remotion restarted) a
# Finalize's render survives before it is given up (~1 min): a just-submitted
# job can 404 for a tick (yas_video_edit_composition_poller's rule).
_RENDER_404_GIVEUP = 4
# Ticks a submit waits while the studio's in-house work holds the house gate
# (a scene analysis or a draft render): at most ~2 min, then it goes anyway.
GATE_WAIT_TICKS = 8
_GATE_WAITS: dict[int, int] = {}
# How long backing the finished reel up to the bucket may take (a restricted
# bucket must not hold the poller).
BACKUP_TIMEOUT_SEC = 60.0


def _reel():
    # Late: th_reel imports this module (its URL and path helpers).
    from ..services import th_reel

    return th_reel


def _instance() -> str:
    return _reel().INSTANCE_ID


def _house_busy() -> bool:
    try:
        from ..services.rebrand_harness import studio

        return studio._gate().locked()
    except Exception:  # noqa: BLE001 — no studio: nothing to wait for
        return False


def _seq_file(run_id: int, seq: int) -> Path:
    return COMP_DIR / str(int(run_id)) / f"final-{int(seq)}.mp4"


def _counted_frames(path: Path) -> Optional[int]:
    try:
        from ..services import rebrand_media

        return rebrand_media.frame_count(path)
    except Exception:  # noqa: BLE001 — unreadable: not a whole reel
        return None


async def _kick_plan(run: dict) -> None:
    """Submit a Finalize's frozen payload, unchanged but for the file it is
    rendered to (final-<seq>.mp4, beside the last good reel it replaces)."""
    run_id = int(run["id"])
    md = run.get("metadata") or {}
    plan = md.get("composition_plan") or {}
    seq = int(md.get("composition_seq") or 0)
    body = plan.get("payload")
    if not isinstance(body, dict) or not seq:
        await _plan_failed(run_id, md, "the reel's plan is missing")
        return
    if _house_busy() and _GATE_WAITS.get(run_id, 0) < GATE_WAIT_TICKS:
        _GATE_WAITS[run_id] = _GATE_WAITS.get(run_id, 0) + 1
        log.info("run=%d reel render waits for the studio (%d/%d)", run_id, _GATE_WAITS[run_id], GATE_WAIT_TICKS)
        return
    _GATE_WAITS.pop(run_id, None)
    out_path = _seq_file(run_id, seq)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {**copy.deepcopy(body), "output_path": str(out_path)}
    try:
        async with httpx.AsyncClient(timeout=RENDER_TIMEOUT_SEC) as client:
            r = await client.post(f"{REMOTION_BASE_URL}/render", json=payload)
        if r.status_code >= 300:
            log.warning("run=%d Remotion /render returned %d: %s", run_id, r.status_code, r.text[:240])
            await _plan_failed(run_id, md, f"remotion_kick_failed: {r.status_code} {r.text[:240]}")
            return
        body = r.json() or {}
        job_id = body.get("job_id") or body.get("id")
        if not job_id:
            await _plan_failed(run_id, md, "remotion response missing job_id")
            return
        await _reel().store().patch(run_id, {
            "render_job_id": str(job_id),
            "composition_output_path": _to_rel(out_path),
        })
        log.info("run=%d reel render kicked: job=%s seq=%d out=%s", run_id, job_id, seq, _to_rel(out_path))
    except Exception as e:
        log.exception("run=%d reel render kickoff crashed", run_id)
        await _plan_failed(run_id, md, f"remotion_kick_crash: {str(e)[:240]}")


def _seq_of(p: Path) -> int:
    """final-<seq>.mp4's seq; a legacy final.mp4 counts as older than any."""
    m = re.fullmatch(r"final-(\d+)\.mp4", p.name)
    return int(m.group(1)) if m else -1


def _prune(out_path: Path, before: Optional[Path]) -> None:
    """Keep the new reel and the one before it (a page may still be playing
    it: the last good reel, else the newest older file); remove every older
    final*.mp4 of the run."""
    older = sorted((p.resolve() for p in out_path.parent.glob("final*.mp4")
                    if (p.name == "final.mp4" or _seq_of(p) >= 0) and _seq_of(p) < _seq_of(out_path)),
                   key=_seq_of)
    before = before.resolve() if before is not None else None
    keep = before if before is not None and before in older else (older[-1] if older else None)
    for p in older:
        if p == keep:
            continue
        try:
            p.unlink()
        except OSError:
            log.warning("couldn't remove the old reel %s", p)


async def _plan_ready(run_id: int, md: dict, out_path: Path, cost: float) -> None:
    """The new reel is the good one: served at once, the older ones pruned,
    then backed up to the run's one bucket key (overwritten, never a new
    object) — composition_storage_seq says which reel the bucket holds."""
    seq = int(md.get("composition_seq") or 0)
    before = _reel().good_path(md)
    await _reel().store().patch(run_id, {
        "composition_status": COMP_READY,
        "composition_path": _to_rel(out_path),
        "composition_good_seq": seq,
        "composition_good_sha": md.get("composition_inputs_sha"),
        "composition_cost_usd": float(cost or 0.0),
        "render_404s": 0,
    })
    log.info("run=%d reel ready: seq=%d %s", run_id, seq, _to_rel(out_path))
    _prune(out_path, before)
    # What shipped, scene by scene (rebrand_memory), in the background:
    # bounded, never this poller's wait nor its failure.
    try:
        from ..services.rebrand_harness import rebrand_memory

        rebrand_memory.start_snapshot(run_id, seq, md)
    except Exception:  # noqa: BLE001
        log.warning("run=%d the reel's choices weren't kept", run_id, exc_info=True)
    from ..services import asset_storage
    try:
        bucket, key = await asyncio.wait_for(asset_storage.persist_local_file(
            abs_path=out_path, bucket_key=f"yas/compositions/talking-head/{run_id}.mp4",
        ), BACKUP_TIMEOUT_SEC)
    except Exception as e:  # noqa: BLE001 — kept local only, as a failed upload is
        log.warning("run=%d reel backup to the bucket failed: %s", run_id, str(e)[:200] or type(e).__name__)
        return
    if bucket and key:
        await _reel().store().patch(run_id, {
            "composition_storage_bucket": bucket,
            "composition_storage_path": key,
            "composition_storage_seq": seq,
        })


async def _plan_failed(run_id: int, md: dict, error: str) -> None:
    """A Finalize's render that failed or was lost. A whole final-<seq>.mp4
    (every frame the plan counts) is taken as done; otherwise the last good
    reel stays (`ready`, with the error and the seq that failed), or, with
    none, the run fails as today."""
    seq = int(md.get("composition_seq") or 0)
    total = (md.get("composition_plan") or {}).get("total_frames")
    out = _seq_file(run_id, seq)
    # Counting is a whole decode: in a thread, never on the poller's loop.
    if seq and total and out.is_file() and await asyncio.to_thread(_counted_frames, out) == int(total):
        log.info("run=%d reel render said %r but final-%d.mp4 is whole: taken", run_id, error[:80], seq)
        await _plan_ready(run_id, md, out, 0.0)
        return
    _GATE_WAITS.pop(run_id, None)
    if _reel().good_reel(md):
        await _reel().store().patch(run_id, {
            "composition_status": COMP_READY,
            "composition_error": error[:240],
            "render_failed_seq": seq,
        })
    else:
        await _reel().store().patch(run_id, {
            "composition_status": COMP_FAILED,
            "composition_error": error[:240],
        })
    log.warning("run=%d reel render seq=%d failed: %s", run_id, seq, error[:240])


async def _check_plan_render(run: dict) -> None:
    """Watch a Finalize's render (only its owner's poller sees it)."""
    run_id = int(run["id"])
    md = run.get("metadata") or {}
    job_id = md.get("render_job_id")
    out_rel = md.get("composition_output_path")
    if not job_id or not out_rel:
        return
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(f"{REMOTION_BASE_URL}/jobs/{job_id}")
        if r.status_code == 404:
            lost = int(md.get("render_404s") or 0) + 1
            if lost >= _RENDER_404_GIVEUP:
                await _plan_failed(run_id, md, "the render job was lost (Remotion restarted?)")
            else:
                await _reel().store().patch(run_id, {"render_404s": lost})
            return
        if r.status_code >= 300:
            log.warning("run=%d render-job %s returned %d", run_id, job_id, r.status_code)
            return
        body = r.json()
    except Exception:
        log.exception("run=%d render-job check crashed", run_id)
        return
    if md.get("render_404s"):
        await _reel().store().patch(run_id, {"render_404s": 0})
    status = (body.get("status") or "").lower()
    if status in {"pending", "running", "rendering", "queued"}:
        return
    if status in {"ready", "done", "success", "completed"}:
        out_path = _abs_from_rel(out_rel)
        if not out_path.exists():
            log.warning("run=%d Remotion reported %s but file not on disk yet at %s", run_id, status, out_path)
            return
        await _plan_ready(run_id, md, out_path, float(body.get("cost_usd") or 0.0))
        return
    if status in {"failed", "error"}:
        await _plan_failed(run_id, md, (body.get("error") or "remotion failed")[:240])
        return
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
        log.exception("yas_th_v2_composition_poller: queued-query failed")
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
        log.exception("yas_th_v2_composition_poller: rendering-unsubmitted query failed")
        to_submit = []
    for run in to_submit:
        try:
            await _kick_render(run)
        except Exception:
            log.exception("yas_th: kick render crashed for run=%s", run.get("id"))

    # Phase 2b — a Finalize of this instance (its frozen plan) to submit.
    try:
        owned = await _pending_rendering_owned()
    except Exception:
        log.exception("yas_th_v2_composition_poller: rendering-owned query failed")
        owned = []
    for run in owned:
        try:
            await _kick_plan(run)
        except Exception:
            log.exception("yas_th: kick plan crashed for run=%s", run.get("id"))

    # Phase 3 — Remotion job in flight; check its status.
    try:
        rendering = await _pending_rendering_submitted()
    except Exception:
        log.exception("yas_th_v2_composition_poller: rendering-submitted query failed")
        rendering = []
    for run in rendering:
        try:
            await _check_render(run)
        except Exception:
            log.exception("yas_th: check render crashed for run=%s", run.get("id"))


async def _loop() -> None:
    log.info("yas_th_v2_composition_poller started (every %ds)", POLL_INTERVAL_SEC)
    try:
        while True:
            await _poll_once()
            await asyncio.sleep(POLL_INTERVAL_SEC)
    except asyncio.CancelledError:
        log.info("yas_th_v2_composition_poller cancelled")
        raise
    except Exception:
        log.exception("yas_th_v2_composition_poller loop crashed")


def start() -> asyncio.Task:
    global _task
    if _task is not None and not _task.done():
        return _task
    _task = asyncio.create_task(_loop(), name="yas_th_v2_composition_poller")
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
