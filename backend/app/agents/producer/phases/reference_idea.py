"""Mode 3 (Reference): turn a described reference image into ONE producer_idea.

Reads the reference's AI description + style notes + Veo prompt hint, optionally
combines with user-supplied notes, and produces a single idea via Gemini. The
reference is marked `approved` (we committed to it) and the run advances to
`ideas_pending`.

The reference image itself reaches Veo later — `phase_generate_script` picks up
the run's approved/described references and stamps their paths onto the
producer_scripts row, which `phase_generate_video` then passes to Veo as a
`VideoGenerationReferenceImage`.
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
from ..prompts import REFERENCE_IDEA_SYSTEM, SINGLE_IDEA_SCHEMA

log = logging.getLogger("archivist.producer.reference_idea")


async def run(reference_id: int, *, user_notes: str | None = None) -> dict | None:
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT id, run_id, kind, original_path, ai_description, metadata, status
                  FROM producer_references WHERE id=%s
                """,
                (reference_id,),
            )
            ref = await cur.fetchone()
    if not ref:
        raise ValueError(f"reference {reference_id} not found")
    if ref["kind"] != "image":
        raise ValueError(f"reference {reference_id} is kind={ref['kind']}; only image is supported")

    meta = ref.get("metadata") or {}
    description = (ref.get("ai_description") or "").strip()
    style_notes = (meta.get("style_notes") or "").strip()
    subject = (meta.get("subject_summary") or "").strip()
    veo_hint = (meta.get("veo_prompt_hint") or "").strip()
    notes = (user_notes or "").strip()

    if not description and not veo_hint:
        raise RuntimeError(
            f"reference {reference_id} has no description yet — run analysis first"
        )

    query_for_taste = f"{subject}. {description}".strip()
    context = await build_context_block(query_text=query_for_taste or None)
    brand_req = await build_brand_requirements()
    req_block = format_brand_requirements(brand_req)
    vt = await get_run_video_type(ref["run_id"])
    vt_directive = format_video_type_directive(vt, "idea")

    user_prompt = (
        f"{req_block}\n"
        f"{vt_directive}\n"
        f"{context}\n"
        f"REFERENCE IMAGE INTERPRETATION:\n"
        f"  Subject: {subject or '(unspecified)'}\n"
        f"  Description: {description or '(unspecified)'}\n"
        f"  Style notes: {style_notes or '(unspecified)'}\n"
        f"  Veo-prompt hint: {veo_hint or '(unspecified)'}\n"
        f"\nUSER NOTES (optional, may be empty):\n  {notes or '(none)'}\n"
        "\nDesign a 4-8 second video concept that lives inside this reference's "
        "visual world. Apply the 5 RULES OF A SHOOTABLE BEAT. Return ONE JSON "
        "idea object with all required fields including mood, subject_type, "
        "and brand_anchors."
    )

    data = await _call(user_prompt)
    if not isinstance(data, dict) or not data.get("title"):
        log.warning("gemini returned no usable idea; raw=%s", str(data)[:300])
        return None

    # v5.1: subject_type adherence check — one retry if the idea drifted
    # outside the run's video_type allowed set.
    mismatched, feedback = single_idea_subject_mismatch(data, vt)
    if mismatched:
        log.info(
            "[ref=%d] reference_idea: subject_type='%s' mismatched video_type — retrying once",
            reference_id, data.get("subject_type"),
        )
        retry = await _call(user_prompt + "\n" + feedback)
        if isinstance(retry, dict) and retry.get("title"):
            data = retry

    embed_seed = f"{data.get('title','')}. {data.get('concept','')} {data.get('visual_direction','')}"
    embeddings = await gemini_embed([embed_seed]) if embed_seed.strip() else []
    emb = embeddings[0] if embeddings else None

    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                INSERT INTO producer_ideas
                  (run_id, idea_index, title, concept, visual_direction,
                   reason, style_mood, embedding, metadata)
                VALUES (%s, 1, %s, %s, %s, %s, %s, %s, %s)
                RETURNING id, run_id, idea_index, title, concept, visual_direction,
                          reason, style_mood, status, created_at
                """,
                (
                    ref["run_id"],
                    (data.get("title") or "").strip()[:200],
                    (data.get("concept") or "").strip(),
                    (data.get("visual_direction") or "").strip(),
                    (data.get("reason") or "").strip(),
                    (data.get("style_mood") or "").strip()[:200],
                    emb,
                    Jsonb({
                        "source": "reference",
                        "reference_id": reference_id,
                        "user_notes": notes[:1000] if notes else "",
                        "mood": (data.get("mood") or "").strip() or None,
                        "subject_type": (data.get("subject_type") or "").strip() or None,
                        "brand_anchors": data.get("brand_anchors") or {},
                    }),
                ),
            )
            row = await cur.fetchone()
            # Mark the reference as approved (we committed to using it) and
            # advance the run.
            await cur.execute(
                "UPDATE producer_references SET status='approved' "
                " WHERE id=%s AND status IN ('described','uploaded')",
                (reference_id,),
            )
            await cur.execute(
                "UPDATE producer_runs SET status='ideas_pending' WHERE id=%s",
                (ref["run_id"],),
            )
    log.info("[ref=%d] generated idea=%d from reference", reference_id, row["id"])
    return row


async def _call(user_prompt: str):
    """One Gemini call returning the single-idea dict (or None)."""
    return await gemini_text(
        user_prompt,
        system=REFERENCE_IDEA_SYSTEM,
        json_schema=SINGLE_IDEA_SCHEMA,
        temperature=0.5,
        max_tokens=2500,
    )
