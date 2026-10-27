"""Extend an already-generated video by ~8 seconds using Veo 3.1's last_frame
conditioning. Extracts the source video's final frame via ffmpeg and passes
it to a fresh Veo call along with a continuation prompt.

The resulting `producer_videos` row is a sibling under the same script with
`parent_video_id` set to the source — so the workspace tree shows extensions
clustered with the original.
"""
from __future__ import annotations

import asyncio
import logging
import subprocess
from pathlib import Path

from psycopg.types.json import Jsonb

from ....config import config
from ....db import get_conn
from ....services.vertex_client import veo_generate

log = logging.getLogger("archivist.producer.extend_video")


def _extract_last_frame_sync(video_path: Path, out_path: Path) -> Path:
    """Extract the final frame of `video_path` to a PNG at `out_path`.

    Uses imageio_ffmpeg's bundled ffmpeg binary so we don't depend on a
    system install. `-sseof -1` seeks to 1s before EOF, then `-update 1`
    overwrites the output until we end up with the absolute last decoded
    frame.
    """
    try:
        import imageio_ffmpeg
    except ImportError as e:
        raise RuntimeError(
            "imageio-ffmpeg not installed — needed to extract video frames"
        ) from e
    ffmpeg_bin = imageio_ffmpeg.get_ffmpeg_exe()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        ffmpeg_bin,
        "-y",
        "-sseof", "-1",
        "-i", str(video_path),
        "-update", "1",
        "-q:v", "2",
        str(out_path),
    ]
    result = subprocess.run(cmd, capture_output=True)
    if result.returncode != 0 or not out_path.exists():
        raise RuntimeError(
            f"ffmpeg failed: {result.stderr.decode('utf-8', errors='replace')[:400]}"
        )
    return out_path


async def _extract_last_frame(video_path: Path, out_path: Path) -> Path:
    return await asyncio.to_thread(_extract_last_frame_sync, video_path, out_path)


def _build_extend_prompt(veo_prompt: str | None, user_text: str | None) -> str:
    user = (user_text or "").strip()
    parts: list[str] = []
    if veo_prompt:
        parts.append(
            "The scene that just ended (visible in the conditioning frame):"
        )
        parts.append(veo_prompt.strip())
        parts.append("")
    if user:
        parts.append(
            "Continue from where the previous shot left off. The next "
            "8 seconds: " + user
        )
    else:
        parts.append(
            "Continue from where the previous shot left off. Carry the "
            "action forward naturally for 8 seconds — maintain the same "
            "camera move, lighting, palette, and subject framing."
        )
    return "\n".join(parts)


async def run(
    source_video_id: int,
    *,
    user_text: str | None = None,
    use_premium_model: bool = False,
    with_audio: bool = False,
) -> dict | None:
    """Submit a Veo extension. Returns the new producer_videos row."""
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT v.id, v.script_id, v.local_path, v.gcs_uri, v.status,
                       v.veo_model,
                       (v.metadata->>'aspect_ratio') AS aspect_ratio,
                       s.veo_prompt,
                       s.idea_id,
                       (SELECT i.run_id FROM producer_ideas i WHERE i.id=s.idea_id) AS run_id
                  FROM producer_videos v
                  JOIN producer_scripts s ON s.id=v.script_id
                 WHERE v.id=%s
                """,
                (source_video_id,),
            )
            src = await cur.fetchone()
    if not src:
        raise ValueError(f"video {source_video_id} not found")
    if not src["local_path"]:
        raise RuntimeError(
            f"video {source_video_id} has no local file yet — wait for it to download"
        )
    src_path = Path(src["local_path"])
    if not src_path.exists():
        raise RuntimeError(f"video file missing on disk: {src_path}")

    run_id = src["run_id"]
    frame_path = (
        config.producer_root / str(run_id) / f"extend-from-{source_video_id}.png"
    )
    await _extract_last_frame(src_path, frame_path)
    log.info(
        "[video=%d] extracted last frame → %s (%d bytes)",
        source_video_id, frame_path, frame_path.stat().st_size,
    )

    prompt = _build_extend_prompt(src.get("veo_prompt"), user_text)
    model = config.veo_model_premium if use_premium_model else config.veo_model

    op = await veo_generate(
        prompt,
        model=model,
        duration_sec=config.veo_max_duration_sec,
        aspect_ratio=src.get("aspect_ratio") or config.veo_default_aspect,
        with_audio=with_audio,
        start_image=frame_path,
    )

    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                INSERT INTO producer_videos
                  (script_id, scene_index, veo_model, veo_operation_name,
                   duration_sec, cost_usd, parent_video_id, metadata)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING id, script_id, scene_index, veo_model, veo_operation_name,
                          duration_sec, cost_usd, parent_video_id, status, created_at
                """,
                (
                    src["script_id"],
                    None,
                    op["model"],
                    op["operation_name"],
                    op["duration_sec"],
                    op["cost_usd"],
                    source_video_id,
                    Jsonb({
                        "with_audio": op["with_audio"],
                        "aspect_ratio": op["aspect_ratio"],
                        "extension_of": source_video_id,
                        "user_continuation": (user_text or "").strip()[:1000],
                    }),
                ),
            )
            row = await cur.fetchone()
    log.info(
        "[video=%d → %d] extension submitted (op=%s)",
        source_video_id, row["id"], op["operation_name"],
    )
    return row
