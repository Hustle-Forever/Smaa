"""/api/agents/producer/* — Producer Agent (Vertex Gemini + Veo 3.1).

REST + state-machine. Each endpoint advances a producer_runs row through
idea → script → video, gated by user approvals.
"""
from __future__ import annotations

import logging
import mimetypes
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from psycopg.types.json import Jsonb

from ..agents.producer.agent import (
    RUN_AGGREGATE_COLUMNS as _RUN_AGGREGATE_COLUMNS,
    cancel_run as agent_cancel_run,
    compute_next_action as agent_compute_next_action,
    decide as agent_decide,
    get_run_detail,
    list_runs as agent_list_runs,
    start_run as agent_start_run,
)


def agent_aggregate_columns() -> str:
    return _RUN_AGGREGATE_COLUMNS
from ..agents.producer.phases import (
    analyze_reference as phase_analyze_reference,
    compose_videos as phase_compose_videos,
    extend_video as phase_extend_video,
    generate_ideas as phase_generate_ideas,
    generate_script as phase_generate_script,
    generate_video as phase_generate_video,
    preview_image as phase_preview_image,
    reference_idea as phase_reference_idea,
    refine_reference as phase_refine_reference,
    render_all_scenes as phase_render_all_scenes,
    user_idea as phase_user_idea,
)
from ..config import config
from ..db import get_conn
from ..models.producer import (
    BatchVideoRequest,
    ComposeAllScenesRequest,
    ComposeRequest,
    CompositionDecision,
    ExtendVideoRequest,
    GenerateIdeasRequest,
    GenerateScriptRequest,
    GenerateVideoRequest,
    IdeaDecision,
    PreviewImageRequest,
    ReferenceDecision,
    ReferenceIdeaRequest,
    RefineReferenceRequest,
    RenderAllScenesEstimateRequest,
    RenderAllScenesRequest,
    RunCreate,
    ScriptDecision,
    UserIdeaRequest,
    VideoDecision,
)
from ..services.vertex_client import get_meter

log = logging.getLogger("archivist.router.producer")

router = APIRouter(prefix="/api/agents/producer", tags=["agents:producer"])


# -----------------------------------------------------------------------------
# Serialisation helpers
# -----------------------------------------------------------------------------

def _serialise(v: Any) -> Any:
    if isinstance(v, datetime):
        return v.isoformat()
    # Postgres NUMERIC → Python Decimal. Pydantic encodes Decimals as strings
    # by default which breaks `.toFixed()` on the JS side. Cost/duration fit
    # safely in float64, so coerce eagerly.
    if isinstance(v, Decimal):
        return float(v)
    # pgvector columns deserialize as numpy.ndarray; flatten to plain list so
    # FastAPI's JSON encoder can handle them. Belt-and-suspenders against
    # accidental embedding leaks via `RETURNING *`.
    if v.__class__.__name__ == "ndarray":
        return v.tolist()
    if isinstance(v, (list, tuple)):
        return [_serialise(x) for x in v]
    if isinstance(v, dict):
        return {k: _serialise(val) for k, val in v.items()}
    return v


def _row(row: dict | None) -> dict | None:
    return _serialise(row) if row else None


# -----------------------------------------------------------------------------
# Runs
# -----------------------------------------------------------------------------

@router.post("/runs")
async def create_run(body: RunCreate) -> dict:
    row = await agent_start_run(
        body.mode,
        title=body.title,
        video_type=body.video_type,
        dialogue_language=body.dialogue_language,
    )
    return _row(row) or {}


@router.get("/types")
async def list_video_types() -> dict:
    """Public catalog of video-type presets. Frontend renders pick-cards from this."""
    from ..agents.producer.prompts import VIDEO_TYPES
    items = [
        {
            "id": key,
            "name": v["name"],
            "description": v["description"],
            "default_audio": v.get("default_audio", False),
            "recommended_aspect": v.get("recommended_aspect", "16:9"),
        }
        for key, v in VIDEO_TYPES.items()
    ]
    # Append "generic" sentinel last
    items.append({
        "id": "generic",
        "name": "Generic (no preset)",
        "description": "No type bias — the agent works from brand profile alone.",
        "default_audio": False,
        "recommended_aspect": "16:9",
    })
    return {"items": items}


@router.get("/runs")
async def list_runs(limit: int = 20) -> dict:
    rows = await agent_list_runs(limit=limit)
    return {"items": [_row(r) for r in rows]}


@router.get("/runs/{run_id}")
async def get_run(run_id: int) -> dict:
    detail = await get_run_detail(run_id)
    if not detail:
        raise HTTPException(404, detail="run not found")
    return _serialise(detail)


@router.post("/runs/{run_id}/cancel")
async def cancel_run(run_id: int) -> dict:
    ok = await agent_cancel_run(run_id)
    if not ok:
        raise HTTPException(404, detail="run not active or not found")
    return {"run_id": run_id, "cancelled": True}


# -----------------------------------------------------------------------------
# Ideas
# -----------------------------------------------------------------------------

@router.post("/runs/{run_id}/ideas")
async def generate_ideas(run_id: int, body: GenerateIdeasRequest | None = None) -> dict:
    """Auto mode: generate `count` ideas (default 5). Idempotency is the
    caller's concern — back-to-back calls produce siblings, not duplicates."""
    count = (body or GenerateIdeasRequest()).count
    rows = await phase_generate_ideas.run(run_id, count=count)
    if not rows:
        raise HTTPException(502, detail="Gemini returned no usable ideas")
    return {"items": [_row(r) for r in rows]}


@router.post("/runs/{run_id}/user-idea")
async def submit_user_idea(run_id: int, body: UserIdeaRequest) -> dict:
    row = await phase_user_idea.run(run_id, rough_idea=body.rough_idea)
    if not row:
        raise HTTPException(502, detail="Gemini returned no usable idea")
    return _row(row) or {}


@router.post("/ideas/{idea_id}/decision")
async def decide_idea(idea_id: int, body: IdeaDecision) -> dict:
    row = await agent_decide("idea", idea_id, body.action, body.reason)
    return _row(row) or {}


# -----------------------------------------------------------------------------
# References (mode 3)
# -----------------------------------------------------------------------------

@router.post("/runs/{run_id}/references")
async def upload_reference(
    run_id: int,
    file: Optional[UploadFile] = File(default=None),
    raw_text: Optional[str] = Form(default=None),
    kind: Optional[str] = Form(default=None),
) -> dict:
    """Upload a reference (image file OR free text). Image references are
    auto-described/embedded via Gemini multimodal."""
    if file is None and not (raw_text and raw_text.strip()):
        raise HTTPException(400, detail="provide a file or raw_text")

    resolved_kind = kind
    storage_path: str | None = None
    if file is not None:
        mime = file.content_type or mimetypes.guess_type(file.filename or "")[0] or ""
        if not resolved_kind:
            resolved_kind = "image" if mime.startswith("image/") else "video" if mime.startswith("video/") else "prompt"
        # Save under data/producer/<run_id>/refs/<safe_name>
        if not file.filename:
            raise HTTPException(400, detail="file is missing a filename")
        safe = Path(file.filename).name
        dest_dir = config.producer_root / str(run_id) / "refs"
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / safe
        body = await file.read()
        dest.write_bytes(body)
        storage_path = str(dest)

    if not resolved_kind:
        resolved_kind = "prompt"
    if resolved_kind not in ("image", "video", "script_notes", "prompt"):
        raise HTTPException(400, detail=f"unsupported kind: {resolved_kind}")

    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                INSERT INTO producer_references
                  (run_id, kind, original_path, raw_text, metadata)
                VALUES (%s, %s, %s, %s, %s)
                RETURNING id, run_id, kind, original_path, status, created_at
                """,
                (
                    run_id, resolved_kind, storage_path,
                    (raw_text or "").strip() or None,
                    Jsonb({}),
                ),
            )
            row = await cur.fetchone()

    # Best-effort auto-analyse for images.
    if resolved_kind == "image":
        try:
            await phase_analyze_reference.run(row["id"])
        except Exception:
            log.exception("ref=%d analyse failed (non-fatal)", row["id"])

    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT * FROM producer_references WHERE id=%s",
                (row["id"],),
            )
            fresh = await cur.fetchone()
    return _row(fresh) or {}


@router.post("/references/{reference_id}/analyze")
async def analyze_reference(reference_id: int) -> dict:
    """Re-run analysis on an existing image reference (idempotent override)."""
    result = await phase_analyze_reference.run(reference_id)
    return _serialise(result)


@router.post("/references/{reference_id}/idea")
async def generate_idea_from_reference(
    reference_id: int,
    body: ReferenceIdeaRequest | None = None,
) -> dict:
    """Turn a described image reference into ONE concrete idea.

    The reference is also marked `approved`. Subsequent script generation will
    auto-attach its image path as a visual anchor for Veo.
    """
    notes = (body.user_notes if body else None) or None
    try:
        row = await phase_reference_idea.run(reference_id, user_notes=notes)
    except RuntimeError as e:
        raise HTTPException(409, detail=str(e))
    if not row:
        raise HTTPException(502, detail="Gemini returned no usable idea")
    return _row(row) or {}


@router.get("/references/{reference_id}/file")
async def get_reference_file(reference_id: int):
    """Stream the uploaded reference image so the UI can preview it."""
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT original_path, kind FROM producer_references WHERE id=%s",
                (reference_id,),
            )
            row = await cur.fetchone()
    if not row:
        raise HTTPException(404, detail="reference not found")
    if not row["original_path"]:
        raise HTTPException(409, detail="reference has no file on disk")
    path = Path(row["original_path"])
    if not path.exists():
        raise HTTPException(410, detail="file missing from disk")
    media_type = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
    return FileResponse(str(path), media_type=media_type, filename=path.name)


@router.post("/references/{reference_id}/decision")
async def decide_reference(reference_id: int, body: ReferenceDecision) -> dict:
    new_status = "approved" if body.action == "approve" else "declined"
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                UPDATE producer_references
                   SET status=%s,
                       metadata = metadata || %s::jsonb
                 WHERE id=%s
                 RETURNING id, run_id, kind, status
                """,
                (new_status, Jsonb({"decision_reason": body.reason or ""}), reference_id),
            )
            row = await cur.fetchone()
    if not row:
        raise HTTPException(404, detail="reference not found")
    return _row(row) or {}


# -----------------------------------------------------------------------------
# Scripts
# -----------------------------------------------------------------------------

@router.post("/ideas/{idea_id}/script")
async def generate_script(idea_id: int, body: GenerateScriptRequest | None = None) -> dict:
    b = body or GenerateScriptRequest()
    row = await phase_generate_script.run(
        idea_id,
        retry_feedback=b.retry_feedback,
        scene_count=b.scene_count,
        scene_duration_mode=b.scene_duration_mode,
    )
    if not row:
        raise HTTPException(502, detail="Gemini returned no usable script")
    return _row(row) or {}


@router.post("/scripts/{script_id}/decision")
async def decide_script(script_id: int, body: ScriptDecision) -> dict:
    row = await agent_decide("script", script_id, body.action, body.reason)
    return _row(row) or {}


# -----------------------------------------------------------------------------
# Videos
# -----------------------------------------------------------------------------

@router.post("/scripts/{script_id}/video")
async def generate_video(script_id: int, body: GenerateVideoRequest | None = None) -> dict:
    """Submit a Veo LRO. Returns immediately with status='generating' and the
    LRO name. The background veo_poller flips status to 'ready' when done."""
    b = body or GenerateVideoRequest()
    try:
        row = await phase_generate_video.run(
            script_id,
            use_premium_model=b.use_premium_model,
            with_audio=b.with_audio,
            duration_sec=b.duration_sec,
            aspect_ratio=b.aspect_ratio,
            reference_image_paths=b.reference_image_paths,
            auto_reference=b.auto_reference,
            scene_index=b.scene_index,
        )
    except RuntimeError as e:
        raise HTTPException(503, detail=str(e))
    except ValueError as e:
        raise HTTPException(400, detail=str(e))
    if not row:
        raise HTTPException(502, detail="Veo did not return an operation")
    return _row(row) or {}


@router.post("/scripts/{script_id}/render-all-estimate")
async def render_all_estimate(
    script_id: int,
    body: RenderAllScenesEstimateRequest | None = None,
) -> dict:
    """Preview the cost of rendering all scenes WITHOUT firing anything.

    The frontend calls this before showing the confirm dialog. Returns the
    per-scene cost breakdown, total, idempotency-skipped scene_indexes, and
    a hard `over_cap` flag the UI uses to block the render button.
    """
    b = body or RenderAllScenesEstimateRequest()
    try:
        preview = await phase_render_all_scenes.estimate(
            script_id,
            use_premium_model=b.use_premium_model,
            with_audio=b.with_audio,
            auto_reference=b.auto_reference,
        )
    except ValueError as e:
        raise HTTPException(404, detail=str(e))
    return _serialise(preview)


@router.post("/scripts/{script_id}/render-all")
async def render_all_scenes(
    script_id: int,
    body: RenderAllScenesRequest | None = None,
) -> dict:
    """Fire Veo for every scene of `script_id` in parallel.

    Six cost-guard layers (see render_all_scenes.py) prevent the catastrophic
    "60 Veo calls by mistake" failure mode the user explicitly flagged:
        1. Hard MAX_SCENES_PER_SCRIPT slice
        2. Pre-flight cost cap (config.producer_max_script_cost_usd)
        3. Idempotency by (script_id, scene_index)
        4. VEO_BUDGET_USD global stop in VertexCostMeter
        5. scene_index range validation in generate_video.run
        6. Per-scene try/except so one failure doesn't abort the rest

    Returns { script_id, submitted: [video rows], skipped_scene_indexes,
    errors, total_est_usd, max_script_cost_usd }.
    """
    b = body or RenderAllScenesRequest()
    try:
        result = await phase_render_all_scenes.run(
            script_id,
            use_premium_model=b.use_premium_model,
            with_audio=b.with_audio,
            aspect_ratio=b.aspect_ratio,
            auto_reference=b.auto_reference,
        )
    except ValueError as e:
        raise HTTPException(404, detail=str(e))
    except RuntimeError as e:
        # Pre-flight cost gate fires this. 402 (Payment Required) signals
        # "you can fix this by changing your request, not by retrying."
        raise HTTPException(402, detail=str(e))
    return _serialise(result)


@router.post("/scripts/{script_id}/compose-all")
async def compose_all_scenes(
    script_id: int,
    body: ComposeAllScenesRequest | None = None,
) -> dict:
    """Concat every ready scene-video of a script into one MP4 via Remotion.

    Manual trigger (per user choice — auto-compose was rejected in plan
    review). Refuses if fewer than 2 scenes are ready.
    """
    b = body or ComposeAllScenesRequest()
    try:
        row = await phase_compose_videos.compose_all_scenes(
            script_id,
            crossfade_sec=b.crossfade_sec,
        )
    except (ValueError, RuntimeError) as e:
        raise HTTPException(409, detail=str(e))
    return _row(row) or {}


@router.post("/scripts/{script_id}/upload-image")
async def upload_script_image(
    script_id: int,
    file: UploadFile = File(...),
) -> dict:
    """Manual upload of a still image to attach to a script.

    The image is saved under `data/producer/<run>/script-<id>-upload-<ts>.<ext>`
    and stored as a `producer_references` row (kind='image',
    metadata.source='user_upload', metadata.script_id=<n>). The frontend then
    passes the path through `reference_image_paths` to the Veo call.
    """
    if not file.filename:
        raise HTTPException(400, detail="file missing a filename")
    mime = file.content_type or mimetypes.guess_type(file.filename or "")[0] or ""
    if not mime.startswith("image/"):
        raise HTTPException(400, detail=f"not an image type: {mime}")
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT s.id, (SELECT i.run_id FROM producer_ideas i WHERE i.id=s.idea_id) AS run_id
                  FROM producer_scripts s WHERE s.id=%s
                """,
                (script_id,),
            )
            row = await cur.fetchone()
    if not row:
        raise HTTPException(404, detail="script not found")
    run_id = row["run_id"]
    ext = Path(file.filename).suffix.lower() or ".png"
    import time as _time
    safe_name = f"script-{script_id}-upload-{int(_time.time() * 1000)}{ext}"
    dest_dir = config.producer_root / str(run_id)
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / safe_name
    dest.write_bytes(await file.read())

    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                INSERT INTO producer_references
                  (run_id, kind, original_path, status, metadata)
                VALUES (%s, 'image', %s, 'described', %s)
                RETURNING id, run_id, kind, original_path, ai_description,
                          status, metadata, created_at
                """,
                (
                    run_id,
                    str(dest),
                    Jsonb({
                        "source": "user_upload",
                        "script_id": script_id,
                        "original_filename": file.filename,
                    }),
                ),
            )
            new_row = await cur.fetchone()
    return _row(new_row) or {}


@router.post("/references/{reference_id}/variation")
async def refine_reference(
    reference_id: int,
    body: RefineReferenceRequest | None = None,
) -> dict:
    """Generate an AI variation of an existing image reference via Nano Banana
    (image-to-image). Works on any image reference — uploaded by the user,
    auto-generated by the preview-image phase, or another prior variation.
    """
    b = body or RefineReferenceRequest()
    try:
        row = await phase_refine_reference.run(
            reference_id,
            user_text=b.user_text,
            aspect_ratio=b.aspect_ratio,
        )
    except (ValueError, RuntimeError) as e:
        raise HTTPException(409, detail=str(e))
    return _row(row) or {}


@router.post("/scripts/{script_id}/preview-image")
async def preview_image(
    script_id: int,
    body: PreviewImageRequest | None = None,
) -> dict:
    """Generate a still reference image for an approved script.

    Distills the script's veo_prompt into an image prompt and runs Nano Banana
    (or Imagen, depending on VERTEX_IMAGE_MODEL). The image is stored as a
    `producer_references` row with `metadata.source='auto_preview'` so the
    workspace can preview it before deciding whether to attach to Veo.
    """
    b = body or PreviewImageRequest()
    try:
        row = await phase_preview_image.run(
            script_id,
            user_text=b.user_text,
            aspect_ratio=b.aspect_ratio,
        )
    except (ValueError, RuntimeError) as e:
        raise HTTPException(409, detail=str(e))
    return _row(row) or {}


@router.post("/runs/{run_id}/batch-videos")
async def batch_videos(run_id: int, body: BatchVideoRequest | None = None) -> dict:
    """Fire Veo for every approved script on this run that lacks a live video.

    "Live video" = status in ('generating', 'ready', 'approved'). Scripts with
    only failed/declined/retried videos are still eligible. Returns the list
    of submitted `producer_videos` rows.

    Each submission is an independent Veo long-running operation; they run
    concurrently on GCP and resolve at different times. The veo_poller
    downloads each as it completes — the UI sees them appear one-by-one.
    """
    b = body or BatchVideoRequest()
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT s.id FROM producer_scripts s
                  JOIN producer_ideas i ON i.id=s.idea_id
                 WHERE i.run_id=%s
                   AND s.status='approved'
                   AND NOT EXISTS (
                     SELECT 1 FROM producer_videos v
                      WHERE v.script_id=s.id
                        AND v.status IN ('generating','ready','approved')
                   )
                 ORDER BY s.id ASC
                """,
                (run_id,),
            )
            eligible_ids = [r["id"] for r in await cur.fetchall()]
    if b.script_ids:
        keep = set(b.script_ids)
        eligible_ids = [i for i in eligible_ids if i in keep]

    if not eligible_ids:
        return {"items": [], "submitted": 0, "skipped": 0}

    submitted: list[dict] = []
    errors: list[dict] = []
    for sid in eligible_ids:
        try:
            row = await phase_generate_video.run(
                sid,
                use_premium_model=b.use_premium_model,
                with_audio=b.with_audio,
                duration_sec=b.duration_sec,
                aspect_ratio=b.aspect_ratio,
            )
            if row:
                submitted.append(row)
        except Exception as e:
            log.exception("batch_videos: script=%d failed to submit", sid)
            errors.append({"script_id": sid, "error": str(e)[:300]})

    return {
        "items": [_row(r) for r in submitted],
        "submitted": len(submitted),
        "skipped": len(eligible_ids) - len(submitted),
        "errors": errors,
    }


@router.get("/videos/{video_id}")
async def get_video(video_id: int) -> dict:
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT id, script_id, scene_index, veo_model, veo_operation_name,
                       gcs_uri, local_path, thumbnail_path, duration_sec, cost_usd,
                       status, decision_reason, decision_at, parent_video_id,
                       metadata, created_at, updated_at
                  FROM producer_videos WHERE id=%s
                """,
                (video_id,),
            )
            row = await cur.fetchone()
    if not row:
        raise HTTPException(404, detail="video not found")
    return _row(row) or {}


@router.get("/videos/{video_id}/file")
async def get_video_file(video_id: int):
    """Stream the local MP4. Frontend uses this URL in <video> elements."""
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT local_path, status FROM producer_videos WHERE id=%s",
                (video_id,),
            )
            row = await cur.fetchone()
    if not row:
        raise HTTPException(404, detail="video not found")
    if not row["local_path"]:
        raise HTTPException(409, detail=f"video not ready (status={row['status']})")
    path = Path(row["local_path"])
    if not path.exists():
        raise HTTPException(410, detail="video file missing from disk")
    return FileResponse(str(path), media_type="video/mp4", filename=path.name)


@router.post("/videos/{video_id}/compose")
async def compose_with_parent(video_id: int, body: ComposeRequest | None = None) -> dict:
    """Stitch this video (an approved extension) with its parent into one MP4.

    The composition is async — returns immediately with status='rendering'.
    The composition_poller watches the Express job and flips status when done.
    """
    b = body or ComposeRequest()
    try:
        row = await phase_compose_videos.run(
            video_id,
            crossfade_sec=b.crossfade_sec,
        )
    except (ValueError, RuntimeError) as e:
        raise HTTPException(409, detail=str(e))
    return _row(row) or {}


@router.post("/videos/{video_id}/extend")
async def extend_video(video_id: int, body: ExtendVideoRequest | None = None) -> dict:
    """Generate a continuation of an existing video (Veo 3.1 last_frame).

    The source video must have a downloaded local file. The extension lands as
    a sibling under the same script with `parent_video_id` set to the source.
    """
    b = body or ExtendVideoRequest()
    try:
        row = await phase_extend_video.run(
            video_id,
            user_text=b.user_text,
            use_premium_model=b.use_premium_model,
            with_audio=b.with_audio,
        )
    except (ValueError, RuntimeError) as e:
        raise HTTPException(409, detail=str(e))
    if not row:
        raise HTTPException(502, detail="Veo did not return an operation")
    return _row(row) or {}


@router.post("/videos/{video_id}/decision")
async def decide_video(video_id: int, body: VideoDecision) -> dict:
    row = await agent_decide("video", video_id, body.action, body.reason)
    return _row(row) or {}


# -----------------------------------------------------------------------------
# Cost meter
# -----------------------------------------------------------------------------

# -----------------------------------------------------------------------------
# Library — flat browse views across all runs
# -----------------------------------------------------------------------------

def _clamp_limit(limit: int, default: int = 50, hard_max: int = 200) -> int:
    try:
        n = int(limit)
    except (TypeError, ValueError):
        n = default
    return max(1, min(n, hard_max))


def _clamp_offset(offset: int) -> int:
    try:
        n = int(offset)
    except (TypeError, ValueError):
        n = 0
    return max(0, n)


# Filter key → predicate on aggregate row. Frontend dropdown sends the key.
_PRODUCTION_FILTERS: dict[str, Any] = {
    "all":             lambda r: True,
    "pending":         lambda r: r["next_action"] in {
        "review_video", "review_script", "review_idea",
        "video_generating", "generate_video", "generate_script",
    },
    "awaiting_idea":   lambda r: (r.get("ideas_pending") or 0) > 0,
    "awaiting_script": lambda r: (r.get("scripts_pending") or 0) > 0,
    "awaiting_video":  lambda r: (r.get("videos_ready") or 0) > 0,
    "generating":      lambda r: (r.get("videos_generating") or 0) > 0,
    "done":            lambda r: r["next_action"] == "done",
    "failed":          lambda r: r.get("status") in {"failed", "cancelled"},
}


@router.get("/library/productions")
async def library_productions(
    limit: int = 50,
    offset: int = 0,
    status: Optional[str] = None,
    mode: Optional[str] = None,
) -> dict:
    """List runs with per-run aggregate counts + a `next_action` hint.

    The `status` query param filters by *workflow category* (not raw run.status):
      all / pending / awaiting_idea / awaiting_script / awaiting_video /
      generating / done / failed.
    """
    limit = _clamp_limit(limit)
    offset = _clamp_offset(offset)
    where: list[str] = []
    params: list[Any] = []
    if mode:
        where.append("r.mode = %s")
        params.append(mode)
    # Over-fetch when a Python-side filter will trim — keeps pagination accurate.
    filter_key = (status or "").strip().lower() or "all"
    pred = _PRODUCTION_FILTERS.get(filter_key, _PRODUCTION_FILTERS["all"])
    # If we're filtering, fetch a larger window; the absolute hard cap of 500
    # keeps the worst case bounded.
    fetch_limit = min(500, max(limit * 4, limit + offset + 100)) if filter_key != "all" else (limit + offset)
    sql = f"SELECT {agent_aggregate_columns()} FROM producer_runs r"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY r.id DESC LIMIT %s"
    params.append(fetch_limit)
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(sql, tuple(params))
            rows = await cur.fetchall()
    for r in rows:
        r["next_action"] = agent_compute_next_action(r)
    filtered = [r for r in rows if pred(r)]
    paged = filtered[offset : offset + limit]
    return {
        "items": [_row(r) for r in paged],
        "total": len(filtered),
        "filter": filter_key,
    }


@router.get("/library/ideas")
async def library_ideas(
    limit: int = 50,
    offset: int = 0,
    status: Optional[str] = None,
    mode: Optional[str] = None,
) -> dict:
    limit = _clamp_limit(limit)
    offset = _clamp_offset(offset)
    where: list[str] = []
    params: list[Any] = []
    if status:
        where.append("i.status = %s")
        params.append(status)
    if mode:
        where.append("r.mode = %s")
        params.append(mode)
    sql = (
        "SELECT i.id, i.run_id, i.idea_index, i.title, i.concept, i.visual_direction, "
        "       i.style_mood, i.status, i.decision_reason, i.created_at, "
        "       r.mode AS run_mode, r.title AS run_title, r.status AS run_status "
        "  FROM producer_ideas i "
        "  JOIN producer_runs  r ON r.id = i.run_id "
    )
    if where:
        sql += " WHERE " + " AND ".join(where) + " "
    sql += "ORDER BY i.id DESC LIMIT %s OFFSET %s"
    params.extend([limit, offset])
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(sql, tuple(params))
            rows = await cur.fetchall()
    return {"items": [_row(r) for r in rows]}


@router.get("/library/scripts")
async def library_scripts(
    limit: int = 50,
    offset: int = 0,
    status: Optional[str] = None,
) -> dict:
    limit = _clamp_limit(limit)
    offset = _clamp_offset(offset)
    where: list[str] = []
    params: list[Any] = []
    if status:
        where.append("s.status = %s")
        params.append(status)
    sql = (
        "SELECT s.id, s.idea_id, s.version_index, s.scenes, s.veo_prompt, "
        "       s.reference_image_paths, s.status, s.decision_reason, s.created_at, "
        "       i.run_id, i.title AS idea_title, "
        "       r.mode AS run_mode, r.title AS run_title "
        "  FROM producer_scripts s "
        "  JOIN producer_ideas   i ON i.id = s.idea_id "
        "  JOIN producer_runs    r ON r.id = i.run_id "
    )
    if where:
        sql += "WHERE " + " AND ".join(where) + " "
    sql += "ORDER BY s.id DESC LIMIT %s OFFSET %s"
    params.extend([limit, offset])
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(sql, tuple(params))
            rows = await cur.fetchall()
    return {"items": [_row(r) for r in rows]}


@router.get("/library/videos")
async def library_videos(
    limit: int = 50,
    offset: int = 0,
    status: Optional[str] = None,
) -> dict:
    limit = _clamp_limit(limit)
    offset = _clamp_offset(offset)
    where: list[str] = []
    params: list[Any] = []
    if status:
        where.append("v.status = %s")
        params.append(status)
    sql = (
        "SELECT v.id, v.script_id, v.status, v.veo_model, v.duration_sec, "
        "       v.cost_usd, v.local_path, v.gcs_uri, v.decision_reason, v.created_at, "
        "       s.idea_id, "
        "       i.run_id, i.title AS idea_title, "
        "       r.mode AS run_mode, r.title AS run_title "
        "  FROM producer_videos  v "
        "  JOIN producer_scripts s ON s.id = v.script_id "
        "  JOIN producer_ideas   i ON i.id = s.idea_id "
        "  JOIN producer_runs    r ON r.id = i.run_id "
    )
    if where:
        sql += "WHERE " + " AND ".join(where) + " "
    sql += "ORDER BY v.id DESC LIMIT %s OFFSET %s"
    params.extend([limit, offset])
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(sql, tuple(params))
            rows = await cur.fetchall()
    return {"items": [_row(r) for r in rows]}


# -----------------------------------------------------------------------------
# Compositions (Remotion-stitched videos)
# -----------------------------------------------------------------------------

@router.get("/compositions/{composition_id}")
async def get_composition(composition_id: int) -> dict:
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT id, run_id, template_id, source_video_ids, render_job_id,
                       output_path, duration_sec, cost_usd, status,
                       decision_reason, decision_at, error, metadata,
                       created_at, updated_at
                  FROM producer_compositions WHERE id=%s
                """,
                (composition_id,),
            )
            row = await cur.fetchone()
    if not row:
        raise HTTPException(404, detail="composition not found")
    return _row(row) or {}


@router.get("/compositions/{composition_id}/file")
async def get_composition_file(composition_id: int):
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT output_path, status FROM producer_compositions WHERE id=%s",
                (composition_id,),
            )
            row = await cur.fetchone()
    if not row:
        raise HTTPException(404, detail="composition not found")
    if not row["output_path"]:
        raise HTTPException(409, detail=f"composition not ready (status={row['status']})")
    path = Path(row["output_path"])
    if not path.exists():
        raise HTTPException(410, detail="composition file missing from disk")
    return FileResponse(str(path), media_type="video/mp4", filename=path.name)


@router.post("/compositions/{composition_id}/decision")
async def decide_composition(composition_id: int, body: CompositionDecision) -> dict:
    row = await agent_decide("composition", composition_id, body.action, body.reason)
    return _row(row) or {}


@router.get("/runs/{run_id}/compositions")
async def list_run_compositions(run_id: int) -> dict:
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT id, run_id, template_id, source_video_ids, render_job_id,
                       output_path, duration_sec, cost_usd, status,
                       decision_reason, decision_at, error, metadata,
                       created_at, updated_at
                  FROM producer_compositions WHERE run_id=%s ORDER BY id DESC
                """,
                (run_id,),
            )
            rows = await cur.fetchall()
    return {"items": [_row(r) for r in rows]}


@router.get("/budget")
async def get_budget() -> dict:
    m = get_meter()
    return {
        "spent_usd": round(m.spent_usd, 4),
        "budget_usd": m.budget_usd,
        "by_model": {k: round(v, 4) for k, v in m.by_model.items()},
    }
