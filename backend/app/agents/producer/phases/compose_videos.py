"""Compose an approved extension + its parent into one MP4 via Remotion.

v4 scope: 2-clip concat with optional crossfade. The schema's
`source_video_ids BIGINT[]` field is array-shaped so future chains (3+
clips) don't need a migration — just a richer caller.

Flow:
  1. Load the extension video. Require parent_video_id to be set.
  2. Load the parent video. Both must have downloaded local files.
  3. Build the `clips` array (parent first, extension second) as URLs
     served by the Express /files static route.
  4. POST /render to the Remotion service; get a job_id back.
  5. Insert a producer_compositions row tracking the job.
  6. Return the row. The composition_poller flips status → ready/failed
     when Express finishes.
"""
from __future__ import annotations

import logging
import time
from pathlib import Path

from psycopg.types.json import Jsonb

from ....config import config
from ....db import get_conn
from ....services import remotion_client

log = logging.getLogger("archivist.producer.compose_videos")


async def run(
    extension_video_id: int,
    *,
    crossfade_sec: float | None = None,
) -> dict:
    crossfade = (
        crossfade_sec
        if crossfade_sec is not None
        else config.remotion_default_crossfade_sec
    )
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT v.id, v.script_id, v.parent_video_id, v.local_path,
                       v.duration_sec,
                       (SELECT i.run_id FROM producer_ideas i
                          JOIN producer_scripts s ON s.idea_id=i.id
                          WHERE s.id=v.script_id) AS run_id
                  FROM producer_videos v WHERE v.id=%s
                """,
                (extension_video_id,),
            )
            ext = await cur.fetchone()
    if not ext:
        raise ValueError(f"video {extension_video_id} not found")
    if not ext.get("parent_video_id"):
        raise RuntimeError(
            f"video {extension_video_id} has no parent — nothing to stitch with"
        )
    if not ext.get("local_path"):
        raise RuntimeError(
            f"video {extension_video_id} has no local file yet — wait for it to download"
        )

    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT id, local_path, duration_sec FROM producer_videos WHERE id=%s",
                (ext["parent_video_id"],),
            )
            parent = await cur.fetchone()
    if not parent:
        raise RuntimeError(f"parent video #{ext['parent_video_id']} not found")
    if not parent.get("local_path"):
        raise RuntimeError(
            f"parent video #{parent['id']} has no local file (still rendering or failed)"
        )

    parent_path = Path(parent["local_path"])
    ext_path = Path(ext["local_path"])
    if not parent_path.exists() or not ext_path.exists():
        raise RuntimeError(
            f"missing file on disk — parent_exists={parent_path.exists()}, "
            f"ext_exists={ext_path.exists()}"
        )

    parent_duration = float(parent.get("duration_sec") or 8.0)
    ext_duration = float(ext.get("duration_sec") or 8.0)
    total_duration_sec = parent_duration + ext_duration - crossfade

    run_id = ext["run_id"]
    out_dir = config.producer_root / str(run_id)
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = int(time.time() * 1000)
    output_path = out_dir / f"composed-{ts}.mp4"

    clips = [
        {
            "url": remotion_client.file_url_for_video(parent_path),
            "durationSec": parent_duration,
        },
        {
            "url": remotion_client.file_url_for_video(ext_path),
            "durationSec": ext_duration,
        },
    ]
    props = {"clips": clips, "crossfadeSec": float(crossfade)}

    job_id = await remotion_client.submit_render("Concat", output_path, props)

    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                INSERT INTO producer_compositions
                  (run_id, template_id, source_video_ids, render_job_id,
                   output_path, duration_sec, status, metadata)
                VALUES (%s, %s, %s, %s, %s, %s, 'rendering', %s)
                RETURNING id, run_id, template_id, source_video_ids,
                          render_job_id, output_path, duration_sec, cost_usd,
                          status, decision_reason, decision_at, error,
                          metadata, created_at, updated_at
                """,
                (
                    run_id,
                    "concat",
                    [parent["id"], ext["id"]],
                    job_id,
                    str(output_path),
                    total_duration_sec,
                    Jsonb({
                        "crossfade_sec": float(crossfade),
                        "extension_video_id": ext["id"],
                        "parent_video_id": parent["id"],
                    }),
                ),
            )
            row = await cur.fetchone()
    log.info(
        "composition=%d run=%d sources=[%d,%d] job=%s → %s",
        row["id"], run_id, parent["id"], ext["id"], job_id, output_path,
    )
    return row


# -----------------------------------------------------------------------------
# v5.1: compose ALL scenes of a script into one MP4
# -----------------------------------------------------------------------------

async def compose_all_scenes(
    script_id: int,
    *,
    crossfade_sec: float | None = None,
) -> dict:
    """Concat every ready scene-video of `script_id` into one MP4.

    Pulls all `producer_videos` rows where script_id=X AND status IN
    ('ready','approved'), ordered by scene_index ASC, and submits them as
    a single Remotion `Concat` render. Refuses to fire if fewer than 2
    scenes are ready (would just be a copy of one video — use the regular
    scene video directly).

    Returns the new `producer_compositions` row (status='rendering'). The
    composition_poller flips it to ready/failed when Express finishes.
    """
    crossfade = (
        crossfade_sec
        if crossfade_sec is not None
        else config.remotion_default_crossfade_sec
    )

    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT v.id, v.scene_index, v.local_path, v.duration_sec
                  FROM producer_videos v
                 WHERE v.script_id=%s
                   AND v.scene_index IS NOT NULL
                   AND v.status IN ('ready','approved')
                 ORDER BY v.scene_index ASC
                """,
                (script_id,),
            )
            videos = await cur.fetchall()
            await cur.execute(
                """
                SELECT i.run_id
                  FROM producer_ideas i
                  JOIN producer_scripts s ON s.idea_id=i.id
                 WHERE s.id=%s
                """,
                (script_id,),
            )
            run_row = await cur.fetchone()

    if not run_row:
        raise ValueError(f"script {script_id} not found")
    run_id = run_row["run_id"]

    if not videos:
        raise RuntimeError(
            f"script {script_id} has no ready scenes — nothing to compose"
        )
    if len(videos) < 2:
        raise RuntimeError(
            f"script {script_id} has only {len(videos)} ready scene; "
            "need ≥2 to compose (single scenes are already standalone clips)"
        )

    # Validate every video has a local file.
    paths: list[tuple[int, Path, float]] = []
    for v in videos:
        if not v.get("local_path"):
            raise RuntimeError(
                f"video #{v['id']} (scene {v['scene_index']}) has no local file"
            )
        p = Path(v["local_path"])
        if not p.exists():
            raise RuntimeError(
                f"video #{v['id']} local file missing from disk: {p}"
            )
        paths.append((v["id"], p, float(v.get("duration_sec") or 8.0)))

    out_dir = config.producer_root / str(run_id)
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = int(time.time() * 1000)
    output_path = out_dir / f"composed-script-{script_id}-{ts}.mp4"

    clips = [
        {
            "url": remotion_client.file_url_for_video(p),
            "durationSec": dur,
        }
        for _vid, p, dur in paths
    ]
    props = {"clips": clips, "crossfadeSec": float(crossfade)}

    job_id = await remotion_client.submit_render("Concat", output_path, props)

    total_duration_sec = sum(dur for _v, _p, dur in paths) - crossfade * (len(paths) - 1)
    source_video_ids = [vid for vid, _p, _d in paths]

    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                INSERT INTO producer_compositions
                  (run_id, template_id, source_video_ids, render_job_id,
                   output_path, duration_sec, status, metadata)
                VALUES (%s, %s, %s, %s, %s, %s, 'rendering', %s)
                RETURNING id, run_id, template_id, source_video_ids,
                          render_job_id, output_path, duration_sec, cost_usd,
                          status, decision_reason, decision_at, error,
                          metadata, created_at, updated_at
                """,
                (
                    run_id,
                    "concat",
                    source_video_ids,
                    job_id,
                    str(output_path),
                    total_duration_sec,
                    Jsonb({
                        "crossfade_sec": float(crossfade),
                        "script_id": script_id,
                        "source_kind": "all_scenes",
                        "scene_count": len(paths),
                    }),
                ),
            )
            row = await cur.fetchone()
    log.info(
        "composition=%d run=%d script=%d scenes=%d job=%s → %s",
        row["id"], run_id, script_id, len(paths), job_id, output_path,
    )
    return row
