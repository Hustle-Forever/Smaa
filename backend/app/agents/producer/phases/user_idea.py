"""Mode 2 (User idea): polish the user's rough text into one concrete idea.

v5.1: brand requirements + video_type directive injected into the USER
message (not system), so the polished idea adheres to the run's video_type
and satisfies the new required mood/subject_type/brand_anchors schema fields.
"""
from __future__ import annotations

import logging

from psycopg.types.json import Jsonb

from ....db import get_conn
from ....services.vertex_client import gemini_embed, gemini_text
from ..memory import (
    build_brand_requirements,
    build_context_block,
    format_brand_requirements,
    format_video_type_directive,
    get_run_video_type,
    single_idea_subject_mismatch,
)
from ..prompts import SINGLE_IDEA_SCHEMA, USER_IDEA_SYSTEM

log = logging.getLogger("archivist.producer.user_idea")


async def run(run_id: int, *, rough_idea: str) -> dict | None:
    """Polish `rough_idea` into one producer_ideas row. Returns the row."""
    rough = (rough_idea or "").strip()
    if not rough:
        raise ValueError("rough_idea is empty")

    context = await build_context_block(query_text=rough)
    brand_req = await build_brand_requirements()
    req_block = format_brand_requirements(brand_req)
    vt = await get_run_video_type(run_id)
    vt_directive = format_video_type_directive(vt, "idea")

    user_prompt = (
        f"{req_block}\n"
        f"{vt_directive}\n"
        f"{context}\n"
        f"User's rough idea:\n```\n{rough}\n```\n"
        "Refine this into a concrete, Veo-renderable concept. Apply the 5 RULES "
        "OF A SHOOTABLE BEAT. Return ONE JSON idea object with all required "
        "fields including mood, subject_type, and brand_anchors."
    )
    # Gemini 2.5 Pro consumes part of max_tokens for "thinking" tokens that
    # aren't part of the response. 2500 gives ample headroom for thinking +
    # a fully-populated idea object.
    data = await _call(user_prompt)
    if not isinstance(data, dict) or not data.get("title"):
        log.warning("gemini returned no usable idea; raw=%s", str(data)[:300])
        return None

    # v5.1: subject_type adherence check — one retry if the polished idea
    # drifted outside the run's video_type allowed set.
    mismatched, feedback = single_idea_subject_mismatch(data, vt)
    if mismatched:
        log.info(
            "[run=%d] user_idea: subject_type='%s' mismatched video_type — retrying once",
            run_id, data.get("subject_type"),
        )
        retry = await _call(user_prompt + "\n" + feedback)
        if isinstance(retry, dict) and retry.get("title"):
            data = retry

    embed_text = f"{data.get('title','')}. {data.get('concept','')} {data.get('visual_direction','')}"
    embeddings = await gemini_embed([embed_text])
    emb = embeddings[0] if embeddings else None

    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                INSERT INTO producer_ideas
                  (run_id, idea_index, title, concept, visual_direction,
                   reason, style_mood, embedding, metadata)
                VALUES (%s, 1, %s, %s, %s, %s, %s, %s, %s)
                RETURNING id, idea_index, title, concept, visual_direction,
                          reason, style_mood, status, created_at
                """,
                (
                    run_id,
                    (data.get("title") or "").strip()[:200],
                    (data.get("concept") or "").strip(),
                    (data.get("visual_direction") or "").strip(),
                    (data.get("reason") or "").strip(),
                    (data.get("style_mood") or "").strip()[:200],
                    emb,
                    Jsonb({
                        "source": "user_idea",
                        "rough_idea": rough[:1000],
                        "mood": (data.get("mood") or "").strip() or None,
                        "subject_type": (data.get("subject_type") or "").strip() or None,
                        "brand_anchors": data.get("brand_anchors") or {},
                    }),
                ),
            )
            row = await cur.fetchone()
            await cur.execute(
                "UPDATE producer_runs SET status='ideas_pending' WHERE id=%s",
                (run_id,),
            )
    log.info("[run=%d] polished user idea → idea=%d", run_id, row["id"])
    return row


async def _call(user_prompt: str):
    """One Gemini call returning the single-idea dict (or None)."""
    return await gemini_text(
        user_prompt,
        system=USER_IDEA_SYSTEM,
        json_schema=SINGLE_IDEA_SCHEMA,
        temperature=0.5,
        max_tokens=2500,
    )
