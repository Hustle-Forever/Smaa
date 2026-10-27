"""/api/agents/brand-doc-ingest/* — owner uploads + processing + re-synth.

  POST   /api/agents/brand-doc-ingest/upload     multipart files + optional free_text → ingest + run
  POST   /api/agents/brand-doc-ingest/run        kick off processing on already-uploaded rows
  GET    /api/agents/brand-doc-ingest/runs       list recent agent runs (scrape_runs where target_domain='brand-doc-ingest')
  GET    /api/agents/brand-doc-ingest/runs/{id}  one run + phase progress (in-memory registry)
  POST   /api/agents/brand-doc-ingest/runs/{id}/cancel
  GET    /api/agents/brand-doc-ingest/runs/{id}/events    SSE stream
  GET    /api/agents/brand-doc-ingest/documents  list brand_documents rows + counts
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any, AsyncIterator, Optional

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, RedirectResponse
from sse_starlette.sse import EventSourceResponse

from ..agents.brand_doc_ingest.agent import (
    BrandDocIngestAgent,
    RunOptions,
    request_cancel,
)
from ..agents.brand_doc_ingest.upload import (
    classify,
    guess_mime,
    insert_file_doc,
    insert_free_text_doc,
    save_file,
)
from ..config import config
from ..db import get_conn
from ..services import asset_storage
from ..models.brand_docs import (
    BrandDocIngestRunRequest,
    BrandDocIngestRunResponse,
    BrandDocRow,
)
from ..workers.runner import get_registry

log = logging.getLogger("archivist.router.brand_doc")

router = APIRouter(prefix="/api/agents/brand-doc-ingest", tags=["agents:brand-doc-ingest"])


def _serialise(v: Any) -> Any:
    if isinstance(v, datetime):
        return v.isoformat()
    return v


def _row(row: dict | None) -> dict | None:
    return {k: _serialise(v) for k, v in row.items()} if row else None


def _resolve_upload_file(storage_path: str | None) -> Path | None:
    """Locate the original uploaded file on disk from its stored path.

    `save_file` always writes to ``config.upload_root/<uuid>.<ext>``, but the
    persisted `storage_path` varies (absolute, "data/uploads/x", or a legacy
    Windows "uploads\\x" with backslashes). We normalise and try the likely
    locations, ending with a by-filename lookup under the current upload_root —
    which is robust across machines. Returns None if the file isn't present
    (e.g. the row was created on another machine)."""
    if not storage_path:
        return None
    norm = storage_path.replace("\\", "/")
    p = Path(norm)
    candidates = [
        config.upload_root / p.name,   # canonical: by filename under upload_root
        Path(norm),                    # as-stored (absolute or cwd-relative)
        Path.cwd() / norm,             # cwd-relative fallback
    ]
    for c in candidates:
        try:
            if c.is_file():
                return c
        except OSError:
            continue
    return None


@router.post("/upload", response_model=BrandDocIngestRunResponse)
async def upload(
    files: Optional[list[UploadFile]] = File(default=None),
    free_text: Optional[str] = Form(default=None),
    uploader_note: Optional[str] = Form(default=None),
):
    """Multipart upload + immediate agent run.

    Body:
      - files: zero or more files (pdf/docx/txt/md/image/...)
      - free_text: optional free-form text
      - uploader_note: optional context string applied to every inserted row

    Returns:
      - run_id of the agent run kicked off
      - inserted_doc_ids of the new brand_documents rows
      - skipped_files: filenames that couldn't be classified
    """
    inserted: list[int] = []
    skipped: list[str] = []

    if files:
        for up in files:
            if not up.filename:
                continue
            kind = classify(up.filename)
            if not kind:
                skipped.append(up.filename)
                continue
            bucket, sp, bytes_ = await save_file(up)
            mime = guess_mime(Path(up.filename or sp), default=up.content_type)
            doc_id = await insert_file_doc(
                kind=kind,
                source_filename=up.filename,
                storage_bucket=bucket,
                storage_path=sp,
                bytes_=bytes_,
                mime_type=mime,
                uploader_note=uploader_note,
            )
            inserted.append(doc_id)
            log.info("Inserted brand_doc id=%d kind=%s bytes=%d", doc_id, kind, bytes_)

    if free_text and free_text.strip():
        doc_id = await insert_free_text_doc(free_text.strip(), uploader_note)
        inserted.append(doc_id)
        log.info("Inserted brand_doc id=%d kind=free_text", doc_id)

    if not inserted and not skipped:
        raise HTTPException(status_code=400, detail="no files or free_text supplied")

    # Kick off the agent
    options = RunOptions(run_synthesis=True, inserted_doc_ids=inserted)
    registry = get_registry()

    async def factory(emit):
        agent = BrandDocIngestAgent(options, on_event=emit)
        return await agent.run()

    tmp_run_id = await registry.start(factory)
    entry = registry.get(tmp_run_id)
    real_run_id = tmp_run_id
    if entry:
        import asyncio as _aio
        for _ in range(50):
            if entry.run_id > 0:
                real_run_id = entry.run_id
                break
            await _aio.sleep(0.05)

    return BrandDocIngestRunResponse(
        run_id=real_run_id,
        status="started",
        inserted_doc_ids=inserted,
        skipped_files=skipped,
    )


@router.post("/upload-file")
async def upload_file(file: UploadFile = File(...)):
    """Store ONE uploaded file and insert a pending brand_documents row — does
    NOT kick a run. This is the bulk-friendly endpoint: the frontend calls it
    once per file (with bounded concurrency) so thousands of large videos can be
    uploaded without a single giant multipart request. Videos are then drained
    by the brand_video_poller; non-video docs are processed by a follow-up
    /run call from the client.

    Videos are stored on local disk (force_local) since they're large and the
    poller discards them after analysis.
    """
    if not file.filename:
        raise HTTPException(status_code=400, detail="file has no filename")
    kind = classify(file.filename)
    if not kind:
        raise HTTPException(
            status_code=400, detail=f"unsupported file type: {file.filename}"
        )
    bucket, sp, bytes_ = await save_file(file, force_local=(kind == "video"))
    mime = guess_mime(Path(file.filename or sp), default=file.content_type)
    doc_id = await insert_file_doc(
        kind=kind,
        source_filename=file.filename,
        storage_bucket=bucket,
        storage_path=sp,
        bytes_=bytes_,
        mime_type=mime,
        uploader_note=None,
    )
    log.info("upload-file: brand_doc id=%d kind=%s bytes=%d", doc_id, kind, bytes_)
    return {"doc_id": doc_id, "kind": kind, "bytes": bytes_}


@router.get("/status")
async def ingest_status():
    """Aggregate brand_documents counts by upload_status, with a video-only
    breakdown — drives the bulk-upload progress UI."""
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT doc_kind, upload_status, COUNT(*) AS n
                  FROM brand_documents
                 GROUP BY doc_kind, upload_status
                """
            )
            rows = await cur.fetchall()

    def _blank() -> dict:
        return {"uploaded": 0, "processing": 0, "completed": 0, "failed": 0, "total": 0}

    overall = _blank()
    video = _blank()
    for r in rows:
        n = int(r["n"])
        status = r["upload_status"] or "uploaded"
        overall[status] = overall.get(status, 0) + n
        overall["total"] += n
        if r["doc_kind"] == "video":
            video[status] = video.get(status, 0) + n
            video["total"] += n
    # videos still being analysed = uploaded + processing
    video["pending"] = video["uploaded"] + video["processing"]
    return {"overall": overall, "video": video}


@router.post("/retry-failed-videos")
async def retry_failed_videos():
    """Re-queue failed brand videos for analysis (e.g. after adding GCP creds).
    Flips them back to 'uploaded' and clears the error; the brand_video_poller
    picks them up again. Rows whose raw file was already discarded will fail
    again with a clear 'not on disk' message — re-upload those."""
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "UPDATE brand_documents "
                "   SET upload_status='uploaded', "
                "       metadata = (COALESCE(metadata,'{}'::jsonb) - 'error') "
                " WHERE doc_kind='video' AND upload_status='failed' "
                "RETURNING id"
            )
            rows = await cur.fetchall()
    log.info("retry-failed-videos: re-queued %d video(s)", len(rows))
    return {"requeued": len(rows)}


@router.post("/run", response_model=BrandDocIngestRunResponse)
async def run_only(body: BrandDocIngestRunRequest):
    """Re-run ingest on already-uploaded rows. No new file uploads."""
    options = RunOptions(
        run_synthesis=body.run_synthesis,
        skip_phases=body.skip_phases,
        force_re_extract=body.force_re_extract,
    )
    registry = get_registry()

    async def factory(emit):
        agent = BrandDocIngestAgent(options, on_event=emit)
        return await agent.run()

    tmp_run_id = await registry.start(factory)
    entry = registry.get(tmp_run_id)
    real_run_id = tmp_run_id
    if entry:
        import asyncio as _aio
        for _ in range(50):
            if entry.run_id > 0:
                real_run_id = entry.run_id
                break
            await _aio.sleep(0.05)

    return BrandDocIngestRunResponse(
        run_id=real_run_id, status="started", inserted_doc_ids=[], skipped_files=[],
    )


@router.get("/runs")
async def list_runs(limit: int = 20) -> dict:
    limit = max(1, min(int(limit), 100))
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT id, target_domain, status, started_at, finished_at, notes
                  FROM scrape_runs
                 WHERE target_domain='brand-doc-ingest'
                 ORDER BY id DESC LIMIT %s
                """,
                (limit,),
            )
            rows = [_row(r) for r in await cur.fetchall()]
    return {"items": rows}


@router.get("/runs/{run_id}")
async def get_run(run_id: int) -> dict:
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT id, target_domain, status, started_at, finished_at, notes "
                "  FROM scrape_runs WHERE id=%s",
                (run_id,),
            )
            row = await cur.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="run not found")

    phases: dict = {}
    entry = get_registry().get(run_id)
    if entry:
        for evt in entry.events:
            if evt.get("type") == "phase_completed":
                phases[evt["phase"]] = {
                    "name": evt["phase"],
                    "processed": evt.get("processed", 0),
                    "failed": evt.get("failed", 0),
                    "skipped": evt.get("skipped", 0),
                    "total": evt.get("total", 0),
                    "extra": evt.get("extra", {}),
                }
            elif evt.get("type") == "phase_started":
                phases.setdefault(evt["phase"], {"name": evt["phase"], "processed": 0, "total": 0})

    base = _row(row) or {}
    base["phases"] = phases
    return base


@router.post("/runs/{run_id}/cancel")
async def cancel_run(run_id: int) -> dict:
    db_flagged = await request_cancel(run_id)
    in_flight = await get_registry().cancel(run_id)
    if not db_flagged and not in_flight:
        raise HTTPException(status_code=404, detail="run not active or not found")
    return {"run_id": run_id, "cancel_requested": True, "in_flight_cancelled": in_flight}


@router.get("/runs/{run_id}/events")
async def stream_run_events(run_id: int):
    registry = get_registry()

    async def event_source() -> AsyncIterator[dict]:
        entry = registry.get(run_id)
        if entry:
            async for evt in registry.subscribe(run_id):
                yield {"event": evt.get("type", "message"), "data": json.dumps(evt)}
            return

        async with get_conn() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT status, notes FROM scrape_runs WHERE id=%s",
                    (run_id,),
                )
                row = await cur.fetchone()
        if not row:
            yield {
                "event": "info",
                "data": json.dumps({"type": "no_active_run", "run_id": run_id}),
            }
            return
        if row["status"] == "completed":
            yield {
                "event": "run_completed",
                "data": json.dumps({
                    "type": "run_completed",
                    "run_id": run_id,
                    "result": {"status": "completed", "notes": row.get("notes")},
                }),
            }
        else:
            yield {
                "event": "run_failed",
                "data": json.dumps({
                    "type": "run_failed",
                    "run_id": run_id,
                    "error": row.get("notes") or (
                        "Run orphaned (backend restart)."
                        if row["status"] == "started" else row["status"]
                    ),
                }),
            }

    return EventSourceResponse(event_source())


@router.get("/documents")
async def list_documents(limit: int = 50) -> dict:
    """List recent brand_documents rows + counts (mirrors legacy /api/submissions)."""
    limit = max(1, min(int(limit), 200))
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT id, doc_kind, source_filename, upload_status,
                       bytes, mime_type, uploader_note,
                       (ai_summary IS NOT NULL) AS summarized,
                       (embedding IS NOT NULL) AS embedded,
                       (ai_description IS NOT NULL) AS described,
                       (visual_embedding IS NOT NULL) AS clip_embedded,
                       length(raw_text) AS text_chars,
                       LEFT(COALESCE(ai_summary, raw_text, ''), 240) AS preview,
                       created_at, updated_at
                  FROM brand_documents
                 ORDER BY id DESC LIMIT %s
                """,
                (limit,),
            )
            items = [_row(r) for r in await cur.fetchall()]
            await cur.execute(
                """
                SELECT
                  COUNT(*)::int AS total,
                  COUNT(*) FILTER (WHERE upload_status='completed')::int AS completed,
                  COUNT(*) FILTER (WHERE upload_status='failed')::int AS failed,
                  COUNT(*) FILTER (WHERE upload_status IN ('uploaded','processing'))::int AS in_progress,
                  COUNT(*) FILTER (WHERE doc_kind='pdf')::int       AS pdf,
                  COUNT(*) FILTER (WHERE doc_kind='docx')::int      AS docx,
                  COUNT(*) FILTER (WHERE doc_kind='text')::int      AS text,
                  COUNT(*) FILTER (WHERE doc_kind='image')::int     AS image,
                  COUNT(*) FILTER (WHERE doc_kind='free_text')::int AS free_text
                FROM brand_documents
                """
            )
            row = _row(await cur.fetchone()) or {}
    totals = {
        "total": row.get("total", 0),
        "completed": row.get("completed", 0),
        "failed": row.get("failed", 0),
        "in_progress": row.get("in_progress", 0),
        "by_kind": {
            "pdf": row.get("pdf", 0),
            "docx": row.get("docx", 0),
            "text": row.get("text", 0),
            "image": row.get("image", 0),
            "free_text": row.get("free_text", 0),
        },
    }
    return {"items": items, "totals": totals}


@router.get("/documents/{doc_id}")
async def get_document(doc_id: int) -> dict:
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT id, doc_kind, source_filename, bytes, mime_type,
                       storage_bucket, storage_path, raw_text, ai_description, ai_summary,
                       uploader_note, upload_status, metadata,
                       (embedding IS NOT NULL) AS has_embedding,
                       (visual_embedding IS NOT NULL) AS has_visual_embedding,
                       created_at, updated_at
                  FROM brand_documents
                 WHERE id=%s
                """,
                (doc_id,),
            )
            row = await cur.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="document not found")
    out = _row(row)
    # Whether the ORIGINAL file is retrievable (so the UI shows a real preview vs.
    # falling back to extracted text): a bucket-stored doc always is; a legacy
    # local doc only if its file is on this server.
    out["has_file"] = bool(row.get("storage_bucket")) or (
        _resolve_upload_file(row.get("storage_path")) is not None
    )
    return out


@router.get("/documents/{doc_id}/file")
async def get_document_file(doc_id: int):
    """Serve the ORIGINAL uploaded file inline (for the Recent-uploads preview).

    Bucket-stored docs → redirect to a short-lived signed URL. Legacy local docs
    → stream from disk. 404s when the file isn't retrievable (e.g. a row created on
    another machine, or an object missing from the bucket); the UI then falls
    back to the extracted text.
    """
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT storage_bucket, storage_path, mime_type, source_filename "
                "FROM brand_documents WHERE id=%s",
                (doc_id,),
            )
            row = await cur.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="document not found")

    bucket = row.get("storage_bucket")
    if bucket and row.get("storage_path"):
        signed = await asset_storage.url_for(
            storage_bucket=bucket, storage_path=row["storage_path"],
            local_path=None, expires=3600,
        )
        if signed:
            return RedirectResponse(signed)
        raise HTTPException(status_code=404, detail="original file not available in storage")

    path = _resolve_upload_file(row.get("storage_path"))
    if not path:
        raise HTTPException(status_code=404, detail="original file not available on this server")
    return FileResponse(
        path,
        media_type=row.get("mime_type") or "application/octet-stream",
        filename=row.get("source_filename") or path.name,
        content_disposition_type="inline",
    )
