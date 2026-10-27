"""Background worker that finishes waiting-provider jobs for the AI video editor.

A structural twin of `yas_video_edit_composition_poller` (15s loop). The
orchestrator (`services/yas_video_edit_ai.py`) parks a job at
metadata.video_edit_ai.job.status = 'waiting_provider' with one of two shapes:

  SUNO music (provider unset / "suno"):
    provider_job_id  — the Suno task id
    pending_op       — a pre-built add_music op missing only its source
    plan_id          — the plan id to apply under (idempotent re-apply)

  VEO bookends (provider == "veo"):
    bookends         — [{role, animation_id, duration_sec}] Veo renders in
                       flight (yas_animations rows owned by the run-level
                       workers/yas_animations_poller, which downloads the MP4s
                       and flips status='ready' — this poller only WATCHES)
    plan_id          — the plan id to apply under (idempotent re-apply)

  B-ROLL preset (provider == "broll") — up to BOTH halves at once:
    pending_music    — {provider_job_id, pending_op, plan_id, applied}
    bookends         — the veo shape above, applied under bookends_plan_id
                       with bookends_applied guarding re-runs; the job goes
                       'done' once every parked half has applied

Every cycle this poller:
  1. finds video-edit projects whose AI job is 'waiting_provider',
  2. music: polls SunoMusicProvider.check_job(provider_job_id); on done
     downloads audio_url → saves it to the Assets Library as music (falling
     back to ve.import_audio) → completes the pending add_music op →
     ops.apply_plan → job 'done' (on failure → job 'failed'),
  3. bookends: reads the yas_animations statuses; when ALL are ready, builds
     the deterministic placement ops (bookends.build_placement_ops: add_clip
     + fade joins) → ops.apply_plan → job 'done'; any failed render → 'failed'.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

import httpx

from ..db import get_conn
from ..services import yas_video_editor as ve
from ..services import yas_video_edit_ai as ve_ai
from ..services import yas_video_edit_ai_bookends as bookends
from ..services.media_providers import ProviderError, SunoMusicProvider
from ..services.yas_video_edit_ai_ops import apply_plan

log = logging.getLogger("archivist.yas_video_edit_ai_poller")

POLL_INTERVAL_SEC = 15
DOWNLOAD_TIMEOUT_SEC = 180.0

_task: asyncio.Task | None = None


async def _waiting_jobs() -> list[dict]:
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT id, metadata->'video_edit_ai'->'job' AS job
                  FROM yas_generations
                 WHERE mode = %s
                   AND metadata->'video_edit_ai'->'job'->>'status' = 'waiting_provider'
                """,
                (ve.MODE_PREVIEW,),
            )
            return await cur.fetchall()


async def _fail(project_id: int, message: str) -> None:
    await ve_ai._merge_job(project_id, {
        "status": "failed",
        "error": message[:500],
        "finished_at": ve_ai._now_iso(),
    })
    log.warning("[yas-ve-ai-poll] project=%d waiting job failed: %s", project_id, message)


async def _download_first(project_id: int, audio_urls: list[str]) -> bytes:
    """Download the first reachable mirror; b'' when ALL fail (the caller
    leaves the job waiting and retries next cycle).

    sunoapi returns several mirrors per track (sourceAudioUrl on cdn1.suno.ai,
    audioUrl on a gateway tempfile host, stream URLs); some are unreachable from
    a given network. Try each in priority order (the downloadable URL can also
    lag the SUCCESS status by ~2-3 min — the poller's 30-min cap bounds a dead
    URL)."""
    raw = b""
    last_err = ""
    for audio_url in audio_urls:
        if not audio_url:
            continue
        try:
            async with httpx.AsyncClient(timeout=DOWNLOAD_TIMEOUT_SEC, follow_redirects=True) as client:
                r = await client.get(audio_url)
            if r.status_code >= 300 or not r.content:
                raise RuntimeError(f"download {r.status_code} ({len(r.content)} bytes)")
            raw = r.content
            break
        except Exception as e:  # noqa: BLE001
            last_err = str(e) or e.__class__.__name__
            continue
    if not raw:
        log.warning("[yas-ve-ai-poll] project=%d no music mirror downloadable yet, "
                    "will retry next cycle (last: %s)", project_id, last_err)
    return raw


async def _library_track_for(provider_job_id: str) -> int | None:
    """The Assets Library track already saved for this Suno job, if any."""
    if not provider_job_id:
        return None
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT id FROM producer_assets "
                "WHERE asset_kind = 'audio' AND metadata->>'suno_job_id' = %s "
                "ORDER BY id LIMIT 1",
                (provider_job_id,),
            )
            row = await cur.fetchone()
    return int(row["id"]) if row else None


async def save_to_music_library(
    *, raw: bytes, provider_job_id: str, brief: str, project_id: int,
) -> int:
    """Add a generated Suno track to the Assets Library as music and return its
    asset id. Idempotent per Suno job. Raises on failure.

    Same pipeline as "+ Add music" (producer_assets, asset_kind=audio), so the
    track appears in the editor's Music tab and on the Assets page, gets the
    mood/genre analysis and embedding the auto-editor picks music by, and is
    stored in the bucket rather than on this instance's disk."""
    existing = await _library_track_for(provider_job_id)
    if existing is not None:
        return existing
    # Imported here: the router module is heavy and only this path needs it.
    from ..routers.producer_assets import _process_asset_bytes

    title = SunoMusicProvider._title_from_brief(brief) if brief.strip() else ""
    row = await _process_asset_bytes(
        raw=raw,
        filename=f"suno_{provider_job_id or 'track'}.mp3",
        mime="audio/mpeg",
        asset_kind="audio",
        category=None,
        archetype=None,
        display_name=f"AI music: {title}" if title else "AI music",
        hint=None,
        extra_metadata={
            "generated_by": "suno",
            "suno_job_id": provider_job_id,
            "project_id": project_id,
            "style_brief": brief[:900],
        },
    )
    return int(row["id"])


async def _suno_track_source(
    project_id: int, raw: bytes, provider_job_id: str, brief: str,
) -> tuple[dict, str]:
    """The add_music source for a downloaded Suno track, plus a label for logs.

    Library music when it saves (see save_to_music_library). If that fails the
    track still goes on the timeline as a plain upload, the old path, rather
    than failing a job the user already waited for."""
    try:
        asset_id = await save_to_music_library(
            raw=raw, provider_job_id=provider_job_id, brief=brief, project_id=project_id,
        )
        return {"source_asset_id": asset_id}, f"music asset #{asset_id}"
    except Exception:
        log.exception("[yas-ve-ai-poll] project=%d could not save suno track %s to the "
                      "music library; adding it as a plain upload", project_id, provider_job_id)
    imported = await ve.import_audio(
        raw=raw, filename=f"suno_{provider_job_id or 'track'}.mp3", mime="audio/mpeg",
    )
    return {"source_upload_id": int(imported["id"])}, f"upload #{imported['id']}"


async def _finish(project_id: int, job: dict, audio_urls: list[str]) -> None:
    """Download the generated track, keep it as library music, and apply the
    pending op. When no mirror is downloadable yet, leave the job waiting
    (retry next cycle — see _download_first)."""
    provider_job_id = str(job.get("provider_job_id") or "")
    raw = await _download_first(project_id, audio_urls)
    if not raw:
        return

    op = dict(job.get("pending_op") or {})
    if op.get("op") != "add_music":
        await _fail(project_id, "waiting_provider job has no pending add_music op")
        return
    await ve_ai._merge_job(project_id, {"progress": "saving the track to your music library…"})
    source, kept_as = await _suno_track_source(
        project_id, raw, provider_job_id, str(op.get("style_or_query") or ""),
    )
    op.update(source)

    plan_id = str(job.get("plan_id") or "") or f"music_{project_id}"
    await ve_ai._merge_job(project_id, {"status": "applying", "progress": "adding music bed…"})
    result = await apply_plan(project_id, plan_id=plan_id, ops=[op])

    failed = list(result.get("failed") or [])
    await ve_ai._merge_job(project_id, {
        "status": "failed" if failed and not result.get("applied") else "done",
        "error": (failed[0].get("error") if failed and not result.get("applied") else None),
        "progress": f"applied {result.get('applied', 0)} op(s) (Suno track imported)",
        "ops_applied": int(result.get("applied", 0)),
        "ops_failed": failed,
        "rev_after": int(result.get("rev", 0)),
        "finished_at": ve_ai._now_iso(),
    })
    log.info("[yas-ve-ai-poll] project=%d suno job %s applied (%s, rev=%s)",
             project_id, provider_job_id, kept_as, result.get("rev"))


async def _animations_status(items: list[dict]) -> str | tuple[str, str]:
    """Aggregate status of the parked bookends' yas_animations rows: "ready"
    (all rendered — each item's duration_sec is refreshed IN PLACE from its
    row, the authoritative value; build_placement_ops falls back to the parked
    value when NULL), "waiting" (some still generating), or ("failed", reason)."""
    anim_ids = []
    for b in items:
        try:
            anim_ids.append(int(b.get("animation_id")))
        except (TypeError, ValueError):
            return ("failed", f"bookend {b.get('role')!r} has no animation_id")

    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT id, status, error, duration_sec FROM yas_animations WHERE id = ANY(%s)",
                (anim_ids,),
            )
            rows = {int(r["id"]): r for r in await cur.fetchall()}

    for b in items:
        role = str(b.get("role") or "bookend")
        row = rows.get(int(b["animation_id"]))
        if row is None:
            return ("failed", f"{role} animation #{b['animation_id']} row is missing")
        if row.get("status") == "failed":
            return ("failed", f"{role} render failed: {row.get('error') or 'unknown error'}")
        if row.get("status") != "ready":
            return "waiting"  # still generating — wait for the next cycle
        if row.get("duration_sec"):
            b["duration_sec"] = float(row["duration_sec"])
    return "ready"


async def _check_veo_bookends(project_id: int, job: dict) -> None:
    """Finish a parked bookend job: wait for every yas_animations row, then
    apply the deterministic placement plan (idempotent, one save_timeline)."""
    items = [b for b in (job.get("bookends") or []) if isinstance(b, dict)]
    if not items:
        await _fail(project_id, "waiting_provider veo job has no bookends")
        return

    status = await _animations_status(items)
    if isinstance(status, tuple):
        await _fail(project_id, status[1])
        return
    if status != "ready":
        return  # still generating — wait for the next cycle

    plan_id = str(job.get("plan_id") or "") or f"bookend_{project_id}"
    await ve_ai._merge_job(project_id, {"status": "applying", "progress": "placing intro/outro…"})
    fresh = await ve._fetch_row(project_id)
    timeline = ve._video_edit(fresh["metadata"]) or {}
    ops = bookends.build_placement_ops(plan_id, items, timeline)
    result = await apply_plan(project_id, plan_id=plan_id, ops=ops)

    failed = list(result.get("failed") or [])
    await ve_ai._merge_job(project_id, {
        "status": "failed" if failed and not result.get("applied") else "done",
        "error": (failed[0].get("error") if failed and not result.get("applied") else None),
        "progress": f"placed {result.get('applied', 0)} clip(s) "
                    f"({'+'.join(str(b.get('role')) for b in items)} rendered)",
        "ops_applied": int(result.get("applied", 0)),
        "ops_failed": failed,
        "rev_after": int(result.get("rev", 0)),
        "finished_at": ve_ai._now_iso(),
    })
    log.info("[yas-ve-ai-poll] project=%d bookends applied (%s, rev=%s)",
             project_id, [b.get("role") for b in items], result.get("rev"))


async def _check_broll(project_id: int, job: dict) -> None:
    """Finish a parked B-roll job (provider == "broll"). Up to two independent
    halves, each applied under its OWN idempotent plan as it becomes ready:

      pending_music     — {provider_job_id, pending_op, plan_id, applied}
                          (a Suno track; same finish path as the music job)
      bookends          — [{role, animation_id, duration_sec}] Veo renders,
                          placed via build_placement_ops under
                          job.bookends_plan_id; bookends_applied guards re-runs

    The job flips 'done' once every parked half has applied."""
    job = dict(job)

    # ---- half 1: Suno music -------------------------------------------------
    pm = job.get("pending_music") if isinstance(job.get("pending_music"), dict) else None
    if pm and not pm.get("applied"):
        provider_job_id = str(pm.get("provider_job_id") or "").strip()
        if not provider_job_id:
            await _fail(project_id, "B-roll waiting job has no music provider_job_id")
            return
        suno = SunoMusicProvider()
        if not suno.available():
            # TRANSIENT, not fatal: the key can momentarily vanish from this
            # cycle (the backend just restarted mid-job, or the key is being
            # added). Keep the job PARKED and retry next cycle instead of
            # killing an in-flight Suno generation — the waiting_provider
            # timeout fails it eventually if the key never returns.
            log.warning("[yas-ve-ai-poll] project=%d: SUNO_API_KEY not visible "
                        "this cycle — keeping broll music job parked (will retry)",
                        project_id)
            return
        try:
            status = await suno.check_job(provider_job_id)
        except ProviderError as e:
            # Transient poll failure — keep waiting, try again next cycle.
            log.warning("[yas-ve-ai-poll] project=%d broll music poll error (will retry): %s",
                        project_id, e)
            return
        if status.get("status") == "failed":
            await _fail(project_id, f"Suno generation failed: {status.get('error') or 'unknown error'}")
            return
        if status.get("status") != "pending":
            audio_urls = status.get("audio_urls") or (
                [status["audio_url"]] if status.get("audio_url") else []
            )
            if not audio_urls:
                await _fail(project_id, "Suno reported done but returned no audio URL")
                return
            raw = await _download_first(project_id, [str(u) for u in audio_urls])
            if raw:  # no mirror yet → leave the half pending, retry next cycle
                op = dict(pm.get("pending_op") or {})
                if op.get("op") != "add_music":
                    await _fail(project_id, "B-roll waiting job has no pending add_music op")
                    return
                source, kept_as = await _suno_track_source(
                    project_id, raw, provider_job_id, str(op.get("style_or_query") or ""),
                )
                op.update(source)
                plan_id = str(pm.get("plan_id") or "") or f"broll{project_id}mu"
                result = await apply_plan(project_id, plan_id=plan_id, ops=[op])
                pm = {**pm, "applied": True}
                job["pending_music"] = pm
                job["ops_applied"] = int(job.get("ops_applied") or 0) + int(result.get("applied", 0))
                job["ops_failed"] = list(job.get("ops_failed") or []) + list(result.get("failed") or [])
                job["rev_after"] = int(result.get("rev", 0))
                await ve_ai._merge_job(project_id, {
                    "pending_music": pm,
                    "ops_applied": job["ops_applied"],
                    "ops_failed": job["ops_failed"],
                    "rev_after": job["rev_after"],
                    "progress": "B-roll music bed applied",
                })
                log.info("[yas-ve-ai-poll] project=%d broll music applied (%s, rev=%s)",
                         project_id, kept_as, result.get("rev"))

    # ---- half 2: Veo bookends -------------------------------------------------
    items = [b for b in (job.get("bookends") or []) if isinstance(b, dict)]
    if items and not job.get("bookends_applied"):
        status_bk = await _animations_status(items)
        if isinstance(status_bk, tuple):
            await _fail(project_id, status_bk[1])
            return
        if status_bk == "ready":
            plan_id = str(job.get("bookends_plan_id") or "") or f"broll{project_id}b"
            fresh = await ve._fetch_row(project_id)
            timeline = ve._video_edit(fresh["metadata"]) or {}
            ops = bookends.build_placement_ops(plan_id, items, timeline)
            result = await apply_plan(project_id, plan_id=plan_id, ops=ops)
            job["bookends_applied"] = True
            job["ops_applied"] = int(job.get("ops_applied") or 0) + int(result.get("applied", 0))
            job["ops_failed"] = list(job.get("ops_failed") or []) + list(result.get("failed") or [])
            job["rev_after"] = int(result.get("rev", 0))
            await ve_ai._merge_job(project_id, {
                "bookends": items,  # duration_sec refreshed from the rows
                "bookends_applied": True,
                "ops_applied": job["ops_applied"],
                "ops_failed": job["ops_failed"],
                "rev_after": job["rev_after"],
                "progress": "B-roll intro/outro placed",
            })
            log.info("[yas-ve-ai-poll] project=%d broll bookends applied (%s, rev=%s)",
                     project_id, [b.get("role") for b in items], result.get("rev"))

    # ---- done once every parked half has applied -------------------------------
    music_done = (pm is None) or bool(pm.get("applied"))
    bookends_done = (not items) or bool(job.get("bookends_applied"))
    if music_done and bookends_done:
        applied = int(job.get("ops_applied") or 0)
        await ve_ai._merge_job(project_id, {
            "status": "done",
            "progress": f"B-roll edit complete ({applied} op(s) applied)",
            "finished_at": ve_ai._now_iso(),
        })
        log.info("[yas-ve-ai-poll] project=%d broll job done (applied=%d, rev=%s)",
                 project_id, applied, job.get("rev_after"))


async def _check_one(run: dict) -> None:
    project_id = int(run["id"])
    job = run.get("job") or {}
    provider = str(job.get("provider") or "suno").strip().lower()
    try:
        started = datetime.fromisoformat(str(job.get("started_at")))
        if started.tzinfo is None:
            started = started.replace(tzinfo=timezone.utc)
        age = (datetime.now(timezone.utc) - started).total_seconds()
    except (TypeError, ValueError):
        age = float("inf")  # no usable started_at — treat as expired
    if age > ve_ai._WAITING_MAX_AGE_SEC:
        if provider == "veo":
            await _fail(project_id, "intro/outro generation timed out after 30 minutes — "
                                    "try again (a finished render is reused)")
        elif provider == "broll":
            await _fail(project_id, "B-roll music/intro-outro generation timed out after "
                                    "30 minutes — run the B-roll edit again "
                                    "(finished pieces are reused)")
        else:
            await _fail(project_id, "music generation timed out after 30 minutes — "
                                    "try again, or pick from the library")
        return

    if provider == "veo":
        await _check_veo_bookends(project_id, job)
        return

    if provider == "broll":
        await _check_broll(project_id, job)
        return

    provider_job_id = str(job.get("provider_job_id") or "").strip()
    if not provider_job_id:
        await _fail(project_id, "waiting_provider job has no provider_job_id")
        return

    suno = SunoMusicProvider()
    if not suno.available():
        # TRANSIENT, not fatal: a single poll cycle with the key missing (backend
        # just restarted mid-job, or the key is being added) must NOT kill an
        # in-flight Suno generation. Keep it parked and retry; the
        # waiting_provider timeout fails it eventually if the key never returns.
        log.warning("[yas-ve-ai-poll] project=%d: SUNO_API_KEY not visible this "
                    "cycle — keeping music job parked (will retry)", project_id)
        return
    try:
        status = await suno.check_job(provider_job_id)
    except ProviderError as e:
        # Transient poll failure — keep waiting, try again next cycle.
        log.warning("[yas-ve-ai-poll] project=%d poll error (will retry): %s", project_id, e)
        return

    if status.get("status") == "pending":
        return
    if status.get("status") == "failed":
        await _fail(project_id, f"Suno generation failed: {status.get('error') or 'unknown error'}")
        return
    audio_urls = status.get("audio_urls") or (
        [status["audio_url"]] if status.get("audio_url") else []
    )
    if not audio_urls:
        await _fail(project_id, "Suno reported done but returned no audio URL")
        return
    await _finish(project_id, job, [str(u) for u in audio_urls])


async def _poll_once() -> None:
    try:
        runs = await _waiting_jobs()
    except Exception:
        log.exception("[yas-ve-ai-poll] waiting-jobs query failed")
        return
    for run in runs:
        try:
            await _check_one(run)
        except Exception:
            log.exception("[yas-ve-ai-poll] check crashed for project=%s", run.get("id"))


async def _loop() -> None:
    log.info("yas_video_edit_ai_poller started (every %ds)", POLL_INTERVAL_SEC)
    try:
        while True:
            await _poll_once()
            await asyncio.sleep(POLL_INTERVAL_SEC)
    except asyncio.CancelledError:
        log.info("yas_video_edit_ai_poller cancelled")
        raise
    except Exception:
        log.exception("yas_video_edit_ai_poller loop crashed")


def start() -> asyncio.Task:
    global _task
    if _task is not None and not _task.done():
        return _task
    _task = asyncio.create_task(_loop(), name="yas_video_edit_ai_poller")
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
