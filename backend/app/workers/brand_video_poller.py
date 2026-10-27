"""Background worker that analyses uploaded BRAND videos into the brand corpus.

Owner uploads brand-identity videos (the 42 manifesto, brand films) on the
Brand → upload tab. Each lands as a `brand_documents` row with
doc_kind='video', upload_status='uploaded'. This poller drains that queue:

  1. atomically claim a batch (FOR UPDATE SKIP LOCKED) so we never double-process
  2. per video (bounded concurrency): sample a few frames + extract audio →
     ONE gemini_multimodal call → {transcript, summary, brand signals}
  3. embed the summary (1536d, same space as the rest of brand_documents)
  4. write raw_text/ai_summary/metadata.extracted/embedding, status='completed'
  5. DISCARD the raw video + temp frames/audio (we keep only text + vectors)

Non-video brand docs are handled by the run-based DAG (process/summarize/
brand_extract phases now skip doc_kind='video'); videos are owned entirely here.
When the queue drains, a single brand-profile re-synthesis is kicked so the new
videos feed the profile.

Idempotent + resumable: status-driven, survives restarts. Started in
main.lifespan; cancelled on shutdown.
"""
from __future__ import annotations

import asyncio
import logging
import tempfile
from pathlib import Path

from psycopg.types.json import Jsonb

from ..config import config
from ..db import get_conn
from ..agents._common import materialize_doc
from ..agents.website_ingest.phases.video import (
    _detect_scenes,
    _extract_audio_sync,
    _probe_duration,
    _save_frame_sync,
)
from ..services.openai_client import embed_batch
from ..services.vertex_client import gemini_multimodal

log = logging.getLogger("archivist.brand_video_poller")

POLL_INTERVAL_SEC = 10
BATCH = 6            # rows claimed per cycle
MAX_FRAMES = 6      # frames sampled per video for the visual signal
_CONCURRENCY = 2    # heavy (Gemini + ffmpeg) — keep low

_task: asyncio.Task | None = None

_SYSTEM = (
    "You are a brand strategist analysing a BRAND-IDENTITY video for the owner's "
    "brand (e.g. a manifesto, culture film, or 'about us' reel). Transcribe the "
    "speech, then extract durable brand signals: how the brand sounds, looks, and "
    "what it stands for. Ground every signal in what is actually seen/heard — do "
    "NOT invent. If a field has no evidence, return an empty list/string."
)

_PROMPT = (
    "Analyse this brand video. Use the audio for the transcript and spoken voice, "
    "and the sampled frames for the visual identity. Return the JSON object."
)

# JSON schema → forces structured output (parsed by gemini_multimodal).
_SCHEMA = {
    "type": "object",
    "properties": {
        "transcript": {"type": "string"},
        "summary": {
            "type": "string",
            "description": "3-6 sentence brand-aware summary of the video.",
        },
        "voice_descriptors": {"type": "array", "items": {"type": "string"}},
        "colors": {"type": "array", "items": {"type": "string"}},
        "taglines": {"type": "array", "items": {"type": "string"}},
        "dos": {"type": "array", "items": {"type": "string"}},
        "donts": {"type": "array", "items": {"type": "string"}},
        "audiences": {"type": "array", "items": {"type": "string"}},
        "visual_style_notes": {"type": "string"},
    },
    "required": ["transcript", "summary"],
}


# ---------------------------------------------------------------------------
# DB
# ---------------------------------------------------------------------------

async def _claim_batch(limit: int) -> list[dict]:
    """Atomically flip up to `limit` pending video rows to 'processing' and
    return them. FOR UPDATE SKIP LOCKED makes this safe under concurrency."""
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                UPDATE brand_documents
                   SET upload_status='processing'
                 WHERE id IN (
                   SELECT id FROM brand_documents
                    WHERE doc_kind='video' AND upload_status='uploaded'
                    ORDER BY id ASC
                    LIMIT %s
                    FOR UPDATE SKIP LOCKED
                 )
                RETURNING id, source_filename, storage_bucket, storage_path, mime_type
                """,
                (limit,),
            )
            return await cur.fetchall()


async def _remaining_pending() -> int:
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT COUNT(*) AS n FROM brand_documents "
                "WHERE doc_kind='video' AND upload_status IN ('uploaded','processing')"
            )
            row = await cur.fetchone()
    return int(row["n"]) if row else 0


async def _mark_completed(
    doc_id: int, *, transcript: str, summary: str, extracted: dict,
    embedding: list[float] | None, video_meta: dict,
) -> None:
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                UPDATE brand_documents
                   SET raw_text=%s,
                       ai_summary=%s,
                       embedding=%s,
                       metadata = COALESCE(metadata, '{}'::jsonb)
                                  || jsonb_build_object('extracted', %s::jsonb, 'video', %s::jsonb),
                       upload_status='completed'
                 WHERE id=%s
                """,
                (
                    transcript or None,
                    summary or None,
                    embedding,
                    Jsonb(extracted),
                    Jsonb(video_meta),
                    doc_id,
                ),
            )


async def _mark_failed(doc_id: int, reason: str) -> None:
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "UPDATE brand_documents "
                "   SET upload_status='failed', "
                "       metadata=COALESCE(metadata, '{}'::jsonb) || jsonb_build_object('error', %s::text) "
                " WHERE id=%s",
                (reason[:500], doc_id),
            )
    log.warning("brand video doc=%d failed: %s", doc_id, reason[:200])


# ---------------------------------------------------------------------------
# Per-video analysis
# ---------------------------------------------------------------------------

async def _analyse_one(row: dict) -> bool:
    """Returns True on a successful 'completed'.

    On SUCCESS the raw video is discarded (we keep only text + vectors). On
    FAILURE the raw file is KEPT so the user can fix the cause (e.g. add GCP
    creds) and retry without re-uploading. Temp frames/audio are always cleaned.
    """
    doc_id = row["id"]
    path, cleanup = await materialize_doc(row)
    tmpdir = Path(tempfile.mkdtemp(prefix=f"brandvid_{doc_id}_"))
    ok = False
    try:
        if not path.exists():
            raise FileNotFoundError(f"video not on disk: {path}")

        duration = await asyncio.to_thread(_probe_duration, path)
        if duration and duration > config.max_video_duration_sec:
            raise RuntimeError(
                f"video too long ({int(duration)}s > {config.max_video_duration_sec}s cap)"
            )

        # 1. Sample frames (scene-aware, fixed-interval fallback).
        timestamps = await asyncio.to_thread(_detect_scenes, path, MAX_FRAMES)
        frames: list[Path] = []
        for i, t in enumerate(timestamps[:MAX_FRAMES]):
            dest = tmpdir / f"f{i:02d}.jpg"
            try:
                await asyncio.to_thread(_save_frame_sync, path, t, dest)
                frames.append(dest)
            except Exception:
                log.debug("[brand video %d] frame @%.1fs failed", doc_id, t)

        # 2. Extract audio for native transcription.
        audio: list[Path] = []
        wav = tmpdir / "audio.wav"
        try:
            await asyncio.to_thread(_extract_audio_sync, path, wav)
            audio.append(wav)
        except Exception:
            log.info("[brand video %d] no/failed audio — visual-only analysis", doc_id)

        if not frames and not audio:
            raise RuntimeError("could not extract any frames or audio")

        # 3. ONE Gemini multimodal call → transcript + summary + signals.
        result = await gemini_multimodal(
            _PROMPT,
            images=frames or None,
            audio=audio or None,
            system=_SYSTEM,
            json_schema=_SCHEMA,
            max_tokens=2400,
        )
        if not isinstance(result, dict) or result.get("_raw") is not None:
            raise RuntimeError("Gemini returned no structured analysis")

        transcript = (result.get("transcript") or "").strip()
        summary = (result.get("summary") or "").strip()
        if not summary:
            raise RuntimeError("analysis produced no summary")

        extracted = {
            "voice_descriptors": result.get("voice_descriptors") or [],
            "colors": result.get("colors") or [],
            "taglines": result.get("taglines") or [],
            "dos": result.get("dos") or [],
            "donts": result.get("donts") or [],
            "audience": result.get("audiences") or [],
            "visual_style_notes": result.get("visual_style_notes") or "",
            "source": "brand_video",
        }

        # 4. Embed the summary (1536d, matches brand_documents.embedding space).
        embedding: list[float] | None = None
        try:
            vecs = await embed_batch([summary])
            embedding = vecs[0] if vecs else None
        except Exception:
            log.exception("[brand video %d] embed failed (kept summary)", doc_id)

        await _mark_completed(
            doc_id,
            transcript=transcript,
            summary=summary,
            extracted=extracted,
            embedding=embedding,
            video_meta={
                "duration_sec": round(duration, 2) if duration else None,
                "frames": len(frames),
                "had_audio": bool(audio),
            },
        )
        log.info(
            "[brand video %d] completed (%d frames, audio=%s, %d-char transcript)",
            doc_id, len(frames), bool(audio), len(transcript),
        )
        ok = True
        return True
    except Exception as e:
        await _mark_failed(doc_id, str(e))
        return False
    finally:
        cleanup()
        _clean_tmp(tmpdir)              # scratch frames/audio — always
        if ok:
            _discard_raw(row)           # original video — only after success


def _clean_tmp(tmpdir: Path) -> None:
    try:
        for p in tmpdir.glob("*"):
            p.unlink(missing_ok=True)
        tmpdir.rmdir()
    except Exception:
        pass


def _discard_raw(row: dict) -> None:
    """Delete the original local video (videos are stored force_local on upload).
    Only called after a successful analysis — failures keep the file for retry."""
    if not row.get("storage_bucket") and row.get("storage_path"):
        try:
            from ..agents._common import resolve_storage_path
            resolve_storage_path(str(row["storage_path"])).unlink(missing_ok=True)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Synthesis trigger (debounced — fires once when the queue drains)
# ---------------------------------------------------------------------------

async def _kick_synthesis() -> None:
    try:
        from ..agents.brand_doc_ingest.agent import BrandDocIngestAgent, RunOptions
        from .runner import get_registry

        options = RunOptions(run_synthesis=True)
        registry = get_registry()

        async def factory(emit):
            agent = BrandDocIngestAgent(options, on_event=emit)
            return await agent.run()

        await registry.start(factory)
        log.info("brand_video_poller: kicked brand-profile re-synthesis")
    except Exception:
        log.exception("brand_video_poller: synthesis kick failed (non-fatal)")


# ---------------------------------------------------------------------------
# Loop
# ---------------------------------------------------------------------------

async def _poll_once() -> None:
    try:
        rows = await _claim_batch(BATCH)
    except Exception:
        log.exception("brand_video_poller: claim failed")
        return
    if not rows:
        return
    log.info("brand_video_poller: analysing %d brand video(s)", len(rows))

    sem = asyncio.Semaphore(_CONCURRENCY)

    async def _guarded(r: dict) -> bool:
        async with sem:
            return await _analyse_one(r)

    results = await asyncio.gather(*(_guarded(r) for r in rows), return_exceptions=True)
    completed = sum(1 for x in results if x is True)

    # Debounced synthesis: only when we finished some videos AND the queue is now
    # fully drained (nothing left uploaded/processing).
    if completed and await _remaining_pending() == 0:
        await _kick_synthesis()


async def _loop() -> None:
    log.info("brand_video_poller started (every %ds)", POLL_INTERVAL_SEC)
    try:
        while True:
            await _poll_once()
            await asyncio.sleep(POLL_INTERVAL_SEC)
    except asyncio.CancelledError:
        log.info("brand_video_poller cancelled")
        raise
    except Exception:
        log.exception("brand_video_poller loop crashed")


def start() -> asyncio.Task:
    global _task
    if _task is not None and not _task.done():
        return _task
    _task = asyncio.create_task(_loop(), name="brand_video_poller")
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
