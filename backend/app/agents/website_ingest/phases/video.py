"""Phase 9 — process downloaded videos.

Per video asset (download_status='downloaded', analyze_status='pending'):

  1. Probe duration; skip if > MAX_VIDEO_DURATION_SEC
  2. ffmpeg → 16 kHz mono WAV → OpenAI Whisper → video_transcripts (+embeddings)
  3. Sample frames via PySceneDetect (fallback fixed interval), cap at
     MAX_FRAMES_PER_VIDEO; persist video_frames rows
  4. CLIP-encode each frame (when enabled) + OpenAI vision_describe (one-liner)
  5. Aggregate transcript + frame descs → chat → video_summaries.summary
     + embedding

Idempotent. On failure marks analyze_status='failed' with metadata.error.
"""
from __future__ import annotations

import asyncio
import logging
import subprocess
import tempfile
from pathlib import Path

from ....config import config
from ....db import get_conn
from ....services.clip_service import enabled as clip_enabled, encode_image_paths
from ....services.openai_client import chat, embed_batch, transcribe, vision_describe
from ....services.storage import frames_dir, relativise, resolve_local
from ..types import CancellationError, PhaseContext, PhaseResult

log = logging.getLogger("archivist.phase.video")

CLIP_BATCH = 8


# ---------------------------------------------------------------------------
# ffmpeg helpers (sync, called via to_thread)
# ---------------------------------------------------------------------------

def _ffmpeg_path() -> str:
    import imageio_ffmpeg
    return imageio_ffmpeg.get_ffmpeg_exe()


def _probe_duration(video_path: Path) -> float | None:
    """Return duration in seconds via OpenCV."""
    try:
        import cv2
        cap = cv2.VideoCapture(str(video_path))
        try:
            fps = cap.get(cv2.CAP_PROP_FPS) or 0.0
            frames = cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0.0
            if fps > 0 and frames > 0:
                return float(frames) / float(fps)
        finally:
            cap.release()
    except Exception as e:
        log.debug("OpenCV probe failed: %s", e)
    return None


def _extract_audio_sync(video_path: Path, dest: Path) -> Path:
    cmd = [
        _ffmpeg_path(), "-y",
        "-i", str(video_path),
        "-vn", "-ac", "1", "-ar", "16000", "-f", "wav",
        str(dest),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg audio extract failed: {(proc.stderr or '')[:500]}")
    return dest


def _save_frame_sync(video_path: Path, t_sec: float, dest: Path) -> Path:
    cmd = [
        _ffmpeg_path(), "-y",
        "-ss", f"{t_sec:.3f}",
        "-i", str(video_path),
        "-frames:v", "1",
        "-q:v", "3",
        str(dest),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg frame extract failed: {(proc.stderr or '')[:500]}")
    return dest


def _detect_scenes(video_path: Path, max_frames: int) -> list[float]:
    duration = _probe_duration(video_path) or 0.0
    timestamps: list[float] = []
    try:
        from scenedetect import detect, ContentDetector
        scenes = detect(str(video_path), ContentDetector(threshold=27.0))
        for start, end in scenes:
            mid = (start.get_seconds() + end.get_seconds()) / 2.0
            timestamps.append(mid)
    except Exception as e:
        log.debug("Scene detect failed; using fixed interval: %s", e)

    if len(timestamps) < 3 and duration > 0:
        interval = max(5.0, duration / max(max_frames, 1))
        t = interval / 2.0
        timestamps = []
        while t < duration and len(timestamps) < max_frames:
            timestamps.append(t)
            t += interval

    timestamps = sorted({round(t, 3) for t in timestamps if t > 0})
    if len(timestamps) > max_frames:
        step = len(timestamps) / max_frames
        timestamps = [timestamps[int(i * step)] for i in range(max_frames)]
    return timestamps


# ---------------------------------------------------------------------------
# DB
# ---------------------------------------------------------------------------

async def _select_pending(limit: int, *, asset_types: list[str] | None = None) -> list[dict]:
    """Pending video assets, oldest/smallest first.

    asset_types lets the IG orchestrator scope the queue to its own rows so a
    big IG cap doesn't bleed into website-only assets. Default = the full set.
    """
    types = asset_types or ['video_native', 'video_youtube', 'video_vimeo', 'instagram']
    placeholders = ','.join(['%s'] * len(types))
    sql = (
        "SELECT id, page_id, asset_type, canonical_url, local_path, bytes "
        "  FROM assets "
        f" WHERE asset_type IN ({placeholders}) "
        "   AND download_status='downloaded' "
        "   AND analyze_status='pending' "
        "   AND local_path IS NOT NULL "
        " ORDER BY COALESCE(bytes, 0) ASC "
        f"LIMIT {int(limit)}"
    )
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(sql, tuple(types))
            return await cur.fetchall()


async def _mark_failed(aid: int, reason: str) -> None:
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                UPDATE assets
                   SET analyze_status='failed',
                       metadata=COALESCE(metadata, '{}'::jsonb)
                                || jsonb_build_object('analyze_error', %s::text)
                 WHERE id=%s
                """,
                (reason, aid),
            )


# ---------------------------------------------------------------------------
# Per-video routine
# ---------------------------------------------------------------------------

async def _process_video(ctx: PhaseContext, row: dict) -> str:
    """Returns 'completed' | 'skipped' | 'failed'."""
    from ....services.settings_service import Hints
    hints: Hints = ctx.hints  # type: ignore[assignment]
    aid = row["id"]
    video_path = resolve_local(row["local_path"])
    if not video_path.exists():
        await _mark_failed(aid, "local_file_missing")
        return "failed"

    duration = await asyncio.to_thread(_probe_duration, video_path) or 0.0
    log.info("[run=%s] [video %d] %s (duration=%.1fs)", ctx.run_id, aid, video_path.name, duration)

    if duration > config.max_video_duration_sec:
        async with get_conn() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    UPDATE assets
                       SET analyze_status='skipped', embed_status='skipped',
                           metadata=COALESCE(metadata, '{}'::jsonb)
                                    || jsonb_build_object('skip_reason', 'too_long'::text, 'duration_sec', %s::float8)
                     WHERE id=%s
                    """,
                    (duration, aid),
                )
        log.info("Skipped #%d (too long: %.1fs)", aid, duration)
        return "skipped"

    transcript_text = ""
    segment_count = 0

    # 1. Audio extract → Whisper
    with tempfile.TemporaryDirectory() as tmpd:
        audio_path = Path(tmpd) / f"{aid}.wav"
        try:
            await asyncio.to_thread(_extract_audio_sync, video_path, audio_path)
            wresp = await transcribe(audio_path)
            segments = wresp.get("segments") or []
            transcript_text = (wresp.get("text") or "").strip()
            async with get_conn() as conn:
                for seg in segments:
                    async with conn.cursor() as cur:
                        await cur.execute(
                            """
                            INSERT INTO video_transcripts
                              (asset_id, segment_index, start_sec, end_sec, text)
                            VALUES (%s, %s, %s, %s, %s)
                            ON CONFLICT (asset_id, segment_index) DO NOTHING
                            """,
                            (aid, seg.get("id", 0), seg.get("start", 0), seg.get("end", 0),
                             (seg.get("text") or "").strip()),
                        )
            segment_count = len(segments)
        except Exception:
            log.exception("Transcription failed for #%d", aid)

    # 2. Embed transcript segments
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT id, text FROM video_transcripts WHERE asset_id=%s AND embedding IS NULL ORDER BY segment_index",
                (aid,),
            )
            seg_rows = await cur.fetchall()
    if seg_rows:
        try:
            vecs = await embed_batch([r["text"] for r in seg_rows])
            async with get_conn() as conn:
                async with conn.cursor() as cur:
                    for r, v in zip(seg_rows, vecs):
                        await cur.execute(
                            "UPDATE video_transcripts SET embedding=%s WHERE id=%s",
                            (v, r["id"]),
                        )
        except Exception:
            log.exception("Transcript embedding failed for #%d", aid)

    # 3. Sample frames
    timestamps = await asyncio.to_thread(_detect_scenes, video_path, config.max_frames_per_video)
    log.info("[run=%s] [video %d] sampling %d frames", ctx.run_id, aid, len(timestamps))

    fdir = frames_dir(aid)
    frame_paths: list[tuple[int, float, Path]] = []
    for i, t in enumerate(timestamps):
        dest = fdir / f"{i:03d}.jpg"
        try:
            await asyncio.to_thread(_save_frame_sync, video_path, t, dest)
            frame_paths.append((i, t, dest))
        except Exception:
            log.exception("Frame save failed for #%d t=%.2f", aid, t)

    # 4. CLIP + describe per frame
    descriptions: list[str] = []
    clip_vectors: list[list[float] | None] = []
    if frame_paths:
        for batch_start in range(0, len(frame_paths), CLIP_BATCH):
            batch = frame_paths[batch_start:batch_start + CLIP_BATCH]
            paths = [p for _, _, p in batch]
            vecs = await asyncio.to_thread(encode_image_paths, paths, batch_size=CLIP_BATCH)
            clip_vectors.extend(vecs)

            sem = asyncio.Semaphore(config.run_concurrency_openai)

            async def describe(p: Path) -> str:
                async with sem:
                    try:
                        return await vision_describe(
                            p,
                            prompt="Describe this video frame in one sentence: subject, action, setting, mood.",
                            max_tokens=80, detail="low",
                        )
                    except Exception:
                        log.exception("Frame describe failed for %s", p)
                        return ""

            descs = await asyncio.gather(*(describe(p) for p in paths))
            descriptions.extend(descs)

        # Persist video_frames
        async with get_conn() as conn:
            for (idx, t, p), cv, desc in zip(frame_paths, clip_vectors, descriptions):
                async with conn.cursor() as cur:
                    await cur.execute(
                        """
                        INSERT INTO video_frames
                          (asset_id, timestamp_sec, frame_index, local_path, ai_description, embedding)
                        VALUES (%s, %s, %s, %s, %s, %s)
                        ON CONFLICT (asset_id, frame_index) DO UPDATE
                          SET ai_description=EXCLUDED.ai_description,
                              embedding=EXCLUDED.embedding
                        """,
                        (aid, float(t), idx, relativise(p), desc or None, cv),
                    )

    # 5. Aggregate summary
    summary_text = ""
    summary_vec = None
    parts: list[str] = []
    if transcript_text:
        parts.append(f"TRANSCRIPT:\n{transcript_text[:6000]}")
    if frame_paths:
        joined = "\n".join(
            f"  t={t:.1f}s: {d}" for (_, t, _), d in zip(frame_paths, descriptions) if d
        )
        if joined:
            parts.append(f"FRAMES:\n{joined[:4000]}")

    if parts:
        try:
            summary_text = await chat(
                "\n\n".join(parts),
                system=(
                    "You write video summaries for the 42 Abu Dhabi brand archive. "
                    "Read the transcript and frame descriptions and write a single "
                    "paragraph (4-7 sentences) capturing: what the video shows, who "
                    "appears, what is said, the visual style, and what it "
                    "communicates about the brand."
                ),
                max_tokens=400, temperature=0.3,
            )
        except Exception:
            log.exception("Video summary failed for #%d", aid)
        if summary_text:
            try:
                vecs = await embed_batch([summary_text])
                summary_vec = vecs[0] if vecs else None
            except Exception:
                log.exception("Summary embed failed for #%d", aid)

    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                INSERT INTO video_summaries
                  (asset_id, duration_sec, frame_count, transcript_text, summary, embedding)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (asset_id) DO UPDATE
                  SET duration_sec=EXCLUDED.duration_sec,
                      frame_count=EXCLUDED.frame_count,
                      transcript_text=EXCLUDED.transcript_text,
                      summary=EXCLUDED.summary,
                      embedding=EXCLUDED.embedding
                """,
                (aid, duration, len(frame_paths), transcript_text or None,
                 summary_text or None, summary_vec),
            )
            await cur.execute(
                """
                UPDATE assets
                   SET analyze_status='completed', embed_status='completed',
                       metadata=COALESCE(metadata, '{}'::jsonb)
                                || jsonb_build_object('duration_sec', %s::float8,
                                                      'frame_count', %s::int,
                                                      'transcript_segments', %s::int)
                 WHERE id=%s
                """,
                (duration, len(frame_paths), segment_count, aid),
            )
    log.info(
        "[run=%s] ✓ #%d done (%.1fs, %d frames, %d segs, summary=%d chars)",
        ctx.run_id, aid, duration, len(frame_paths), segment_count, len(summary_text),
    )
    return "completed"


async def run(ctx: PhaseContext) -> PhaseResult:
    from ....services.settings_service import Hints
    hints: Hints = ctx.hints  # type: ignore[assignment]

    if hints.skip_videos:
        log.info("[run=%s] Video phase skipped (hints.skip_videos=true)", ctx.run_id)
        return PhaseResult(name="video", total=0, notes="skipped per settings")

    # IG orchestrator can pass a higher cap + restrict to its asset types so a
    # large IG backfill doesn't disturb website pipeline rate limits.
    meta = (ctx.metadata or {})
    cap = int(meta.get("video_phase_cap") or config.max_videos_per_run)
    asset_types = meta.get("video_phase_asset_types")  # list[str] | None
    rows = await _select_pending(cap, asset_types=asset_types)
    if not rows:
        log.info("[run=%s] Video: nothing pending", ctx.run_id)
        return PhaseResult(name="video", total=0)

    log.info("[run=%s] Video: %d to process (CLIP=%s)", ctx.run_id, len(rows), clip_enabled())

    processed = 0
    skipped = 0
    failed = 0

    for i, row in enumerate(rows, 1):
        if ctx.is_cancelled and ctx.is_cancelled():
            raise CancellationError("video cancelled mid-batch")
        try:
            outcome = await _process_video(ctx, row)
            if outcome == "completed":
                processed += 1
            elif outcome == "skipped":
                skipped += 1
            else:
                failed += 1
        except Exception:
            failed += 1
            log.exception("Video processing failed for asset #%s", row["id"])
            await _mark_failed(row["id"], "video_pipeline_exception")
        if ctx.report_progress:
            ctx.report_progress("video", processed + skipped + failed, len(rows))

    log.info(
        "[run=%s] Video done: %d ok, %d skipped, %d failed",
        ctx.run_id, processed, skipped, failed,
    )
    return PhaseResult(
        name="video", processed=processed, skipped=skipped, failed=failed, total=len(rows),
    )
