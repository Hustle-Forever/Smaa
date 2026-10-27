"""Producer Agent state-machine entry points.

Each function is one transition the user can drive via the REST router.
They are functional (call, write to DB, return) — there is no long-lived
asyncio task per producer_runs row.

The exception is video generation, which submits a long-running Veo operation;
the background `veo_poller` worker watches for completion and flips status.
"""
from __future__ import annotations

import logging
from typing import Literal

from psycopg.types.json import Jsonb

from ...db import get_conn
from ...services.vertex_client import gemini_embed

log = logging.getLogger("archivist.producer.agent")

Mode = Literal["auto", "user_idea", "reference"]
Decision = Literal["approve", "decline", "retry"]


# -----------------------------------------------------------------------------
# Lifecycle
# -----------------------------------------------------------------------------

async def start_run(
    mode: Mode,
    *,
    title: str | None = None,
    video_type: str | None = None,
    dialogue_language: str | None = None,
) -> dict:
    """Create a producer_runs row tagged with the latest completed brand
    profile (snapshot semantics — later brand changes don't rewrite history).

    `video_type` (optional) is stored in metadata.video_type and used by the
    generation phases to inject type-specific prompt biases.
    `dialogue_language` (optional) is stored in metadata.dialogue_language
    and read by the SCRIPT phase to pick the spoken-language directive.
    Only one of {en, ar, mixed} is accepted; anything else falls back to en.
    """
    if mode not in ("auto", "user_idea", "reference"):
        raise ValueError(f"unknown mode: {mode}")

    # Normalise: empty / "generic" stored as omitted (no addon applied).
    vt = (video_type or "").strip() or None
    metadata: dict = {}
    if vt and vt != "generic":
        metadata["video_type"] = vt

    lang = (dialogue_language or "").strip().lower()
    if lang in ("en", "ar", "mixed"):
        metadata["dialogue_language"] = lang

    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT MAX(id) AS id FROM brand_profile_runs WHERE status='completed'"
            )
            brand_row = await cur.fetchone()
            brand_run_id = brand_row["id"] if brand_row and brand_row.get("id") else None

            await cur.execute(
                """
                INSERT INTO producer_runs (mode, title, brand_profile_run_id, status, metadata)
                VALUES (%s, %s, %s, 'started', %s)
                RETURNING id, mode, title, status, brand_profile_run_id,
                          started_at, created_at, metadata
                """,
                (
                    mode,
                    (title or "").strip()[:200] or None,
                    brand_run_id,
                    Jsonb(metadata),
                ),
            )
            row = await cur.fetchone()
    log.info(
        "producer_runs id=%d mode=%s video_type=%s brand_run=%s",
        row["id"], mode, vt or "generic", brand_run_id,
    )
    return row


async def cancel_run(run_id: int) -> bool:
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                UPDATE producer_runs SET status='cancelled', finished_at=NOW()
                 WHERE id=%s AND status NOT IN ('completed','cancelled','failed')
                """,
                (run_id,),
            )
            return cur.rowcount > 0


# -----------------------------------------------------------------------------
# Decisions (idea / script / video)
# -----------------------------------------------------------------------------

_TARGET_TABLE = {
    "idea":        "producer_ideas",
    "script":      "producer_scripts",
    "video":       "producer_videos",
    "composition": "producer_compositions",
}


async def decide(
    kind: Literal["idea", "script", "video", "composition"],
    target_id: int,
    action: Decision,
    reason: str | None = None,
) -> dict:
    """Apply a decision to one row. Writes a producer_feedback row too,
    embedding the reason so future generations can learn from it.

    Status transitions:
      approve → status='approved' (and the parent run advances)
      decline → status='declined'
      retry   → status='retried'  (caller is expected to spawn a sibling)
    """
    if action not in ("approve", "decline", "retry"):
        raise ValueError(f"unknown action: {action}")
    table = _TARGET_TABLE[kind]
    new_status = {"approve": "approved", "decline": "declined", "retry": "retried"}[action]

    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                f"""
                UPDATE {table}
                   SET status=%s, decision_reason=%s, decision_at=NOW()
                 WHERE id=%s
                 RETURNING *
                """,
                (new_status, (reason or "").strip() or None, target_id),
            )
            row = await cur.fetchone()
            if not row:
                raise ValueError(f"{kind} {target_id} not found")
            # Strip pgvector columns — they come back as numpy.ndarray which
            # the FastAPI JSON serializer can't encode, and the API consumer
            # has no use for the raw embedding anyway.
            for _k in ("embedding", "description_embedding", "visual_embedding"):
                row.pop(_k, None)

            # Advance the parent run's status indicator on approval. v3 note:
            # producer_runs.status is informational only — UI computes "where
            # am I" from row-level aggregate counts since multi-approve is
            # now allowed at every layer (no supersede cascade).
            if action == "approve":
                if kind == "idea":
                    await cur.execute(
                        "UPDATE producer_runs SET status='idea_approved' WHERE id=%s",
                        (row["run_id"],),
                    )
                elif kind == "script":
                    await cur.execute(
                        "UPDATE producer_runs SET status='script_approved' "
                        "WHERE id=(SELECT run_id FROM producer_ideas WHERE id=%s)",
                        (row["idea_id"],),
                    )
                elif kind == "video":
                    await cur.execute(
                        """
                        UPDATE producer_runs SET status='completed', finished_at=NOW()
                         WHERE id=(SELECT i.run_id FROM producer_ideas i
                                    JOIN producer_scripts s ON s.idea_id=i.id
                                    WHERE s.id=%s)
                        """,
                        (row["script_id"],),
                    )

    # Best-effort: embed the reason for taste-learning. Failures here are
    # non-fatal — the row's status is already updated above.
    if reason and reason.strip():
        try:
            embeddings = await gemini_embed([reason.strip()])
            emb = embeddings[0] if embeddings else None
        except Exception:
            log.exception("feedback embed failed (non-fatal)")
            emb = None
    else:
        emb = None

    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT MAX(id) AS id FROM brand_profile_runs WHERE status='completed'"
            )
            brow = await cur.fetchone()
            brand_run_id = brow["id"] if brow and brow.get("id") else None
            await cur.execute(
                """
                INSERT INTO producer_feedback
                  (target_kind, target_id, intent, reason, embedding, brand_profile_run_id, metadata)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    kind,
                    target_id,
                    action,
                    (reason or "").strip() or None,
                    emb,
                    brand_run_id,
                    Jsonb({}),
                ),
            )
    log.info("decision: %s.%d → %s (reason=%r)", kind, target_id, new_status, reason)
    return dict(row)


# -----------------------------------------------------------------------------
# Hydration (run detail with all children)
# -----------------------------------------------------------------------------

async def get_run_detail(run_id: int) -> dict | None:
    """Return the run + all its ideas/scripts/videos/references in nested form."""
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT id, mode, status, title, brand_profile_run_id,
                       started_at, finished_at, notes, metadata, created_at, updated_at
                  FROM producer_runs WHERE id=%s
                """,
                (run_id,),
            )
            run = await cur.fetchone()
            if not run:
                return None

            await cur.execute(
                """
                SELECT id, idea_index, title, concept, visual_direction, reason,
                       style_mood, status, decision_reason, decision_at,
                       parent_idea_id, metadata, created_at, updated_at
                  FROM producer_ideas WHERE run_id=%s ORDER BY idea_index ASC, id ASC
                """,
                (run_id,),
            )
            ideas = await cur.fetchall()
            idea_ids = [i["id"] for i in ideas]

            scripts: list[dict] = []
            videos: list[dict] = []
            if idea_ids:
                await cur.execute(
                    """
                    SELECT id, idea_id, version_index, scenes, veo_prompt,
                           reference_image_paths, status, decision_reason, decision_at,
                           parent_script_id, metadata, created_at, updated_at
                      FROM producer_scripts WHERE idea_id = ANY(%s) ORDER BY id ASC
                    """,
                    (idea_ids,),
                )
                scripts = await cur.fetchall()
                script_ids = [s["id"] for s in scripts]
                if script_ids:
                    await cur.execute(
                        """
                        SELECT id, script_id, scene_index, veo_model, veo_operation_name,
                               gcs_uri, local_path, thumbnail_path, duration_sec, cost_usd,
                               status, decision_reason, decision_at, parent_video_id,
                               metadata, created_at, updated_at
                          FROM producer_videos WHERE script_id = ANY(%s) ORDER BY id ASC
                        """,
                        (script_ids,),
                    )
                    videos = await cur.fetchall()

            await cur.execute(
                """
                SELECT id, kind, original_path, enhanced_path, ai_description,
                       status, metadata, created_at, updated_at
                  FROM producer_references WHERE run_id=%s ORDER BY id ASC
                """,
                (run_id,),
            )
            refs = await cur.fetchall()

            await cur.execute(
                """
                SELECT id, run_id, template_id, source_video_ids, render_job_id,
                       output_path, duration_sec, cost_usd, status,
                       decision_reason, decision_at, error, metadata,
                       created_at, updated_at
                  FROM producer_compositions WHERE run_id=%s ORDER BY id ASC
                """,
                (run_id,),
            )
            compositions = await cur.fetchall()

    return {
        "run": run,
        "ideas": ideas,
        "scripts": scripts,
        "videos": videos,
        "references": refs,
        "compositions": compositions,
    }


# Run-level aggregate SQL — shared between list_runs() and library_productions.
# All counts are computed in subqueries so the result row carries everything
# the UI needs without N+1 queries.
RUN_AGGREGATE_COLUMNS = """
  r.id, r.mode, r.status, r.title, r.notes, r.started_at, r.finished_at,
  r.brand_profile_run_id, r.metadata,
  (SELECT COUNT(*) FROM producer_ideas i
     WHERE i.run_id=r.id AND i.status='generated')::int AS ideas_pending,
  (SELECT COUNT(*) FROM producer_ideas i
     WHERE i.run_id=r.id AND i.status='approved')::int AS ideas_approved,
  (SELECT COUNT(*) FROM producer_ideas i
     WHERE i.run_id=r.id)::int AS ideas_count,
  (SELECT COUNT(*) FROM producer_scripts s
     JOIN producer_ideas i ON i.id=s.idea_id
    WHERE i.run_id=r.id AND s.status='generated')::int AS scripts_pending,
  (SELECT COUNT(*) FROM producer_scripts s
     JOIN producer_ideas i ON i.id=s.idea_id
    WHERE i.run_id=r.id AND s.status='approved')::int AS scripts_approved,
  (SELECT COUNT(*) FROM producer_videos v
     JOIN producer_scripts s ON s.id=v.script_id
     JOIN producer_ideas   i ON i.id=s.idea_id
    WHERE i.run_id=r.id AND v.status='generating')::int AS videos_generating,
  (SELECT COUNT(*) FROM producer_videos v
     JOIN producer_scripts s ON s.id=v.script_id
     JOIN producer_ideas   i ON i.id=s.idea_id
    WHERE i.run_id=r.id AND v.status='ready')::int AS videos_ready,
  (SELECT COUNT(*) FROM producer_videos v
     JOIN producer_scripts s ON s.id=v.script_id
     JOIN producer_ideas   i ON i.id=s.idea_id
    WHERE i.run_id=r.id AND v.status='approved')::int AS videos_approved,
  -- approved ideas that have NO live (generated/approved) script
  (SELECT COUNT(*) FROM producer_ideas i
    WHERE i.run_id=r.id AND i.status='approved'
      AND NOT EXISTS (
        SELECT 1 FROM producer_scripts s
         WHERE s.idea_id=i.id AND s.status IN ('generated','approved')
      ))::int AS ideas_needing_script,
  -- approved scripts that have NO live (generating/ready/approved) video
  (SELECT COUNT(*) FROM producer_scripts s
     JOIN producer_ideas i ON i.id=s.idea_id
    WHERE i.run_id=r.id AND s.status='approved'
      AND NOT EXISTS (
        SELECT 1 FROM producer_videos v
         WHERE v.script_id=s.id AND v.status IN ('generating','ready','approved')
      ))::int AS scripts_needing_video
"""


def compute_next_action(row: dict) -> str:
    """Pick the most actionable next step for a run, based on its aggregates.

    Priority: user-visible decisions first (review_video > review_script >
    review_idea), then in-flight (video_generating), then generation tasks
    (generate_video > generate_script), finally done/empty.
    """
    if row.get("status") in ("failed", "cancelled"):
        return row["status"]
    if (row.get("videos_ready") or 0) > 0:
        return "review_video"
    if (row.get("scripts_pending") or 0) > 0:
        return "review_script"
    if (row.get("ideas_pending") or 0) > 0:
        return "review_idea"
    if (row.get("videos_generating") or 0) > 0:
        return "video_generating"
    if (row.get("scripts_needing_video") or 0) > 0:
        return "generate_video"
    if (row.get("ideas_needing_script") or 0) > 0:
        return "generate_script"
    if (row.get("videos_approved") or 0) > 0:
        return "done"
    return "empty"


async def list_runs(limit: int = 20) -> list[dict]:
    limit = max(1, min(int(limit), 100))
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                f"SELECT {RUN_AGGREGATE_COLUMNS} "
                "FROM producer_runs r "
                "ORDER BY r.id DESC LIMIT %s",
                (limit,),
            )
            rows = await cur.fetchall()
    for r in rows:
        r["next_action"] = compute_next_action(r)
    return rows
