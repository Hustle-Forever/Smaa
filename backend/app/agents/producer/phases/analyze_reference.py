"""Mode 3 (Reference): describe + embed a user-uploaded reference image."""
from __future__ import annotations

import logging
from pathlib import Path

from psycopg.types.json import Jsonb

from ....db import get_conn
from ....services.vertex_client import gemini_embed, gemini_multimodal
from ..prompts import REFERENCE_ANALYSIS_SYSTEM, REFERENCE_DESCRIPTION_SCHEMA

log = logging.getLogger("archivist.producer.analyze_reference")


async def run(reference_id: int) -> dict:
    """Analyse a producer_references row (kind='image'). Updates the row in
    place with ai_description + description_embedding."""
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT id, run_id, kind, original_path FROM producer_references WHERE id=%s",
                (reference_id,),
            )
            row = await cur.fetchone()
    if not row:
        raise ValueError(f"reference {reference_id} not found")
    if row["kind"] != "image":
        raise ValueError(f"reference {reference_id} is kind={row['kind']}; only 'image' is supported here")

    image_path = Path(row["original_path"])
    if not image_path.exists():
        raise FileNotFoundError(f"reference file missing: {image_path}")

    data = await gemini_multimodal(
        "Analyse this reference image for video production. Return the JSON shape requested.",
        images=[image_path],
        system=REFERENCE_ANALYSIS_SYSTEM,
        json_schema=REFERENCE_DESCRIPTION_SCHEMA,
        temperature=0.3,
        max_tokens=2500,
    )
    if not isinstance(data, dict):
        data = {"_raw": str(data)[:1000]}

    description = (data.get("description") or "").strip()
    subject = (data.get("subject_summary") or "").strip()
    style_notes = (data.get("style_notes") or "").strip()
    veo_hint = (data.get("veo_prompt_hint") or "").strip()
    embed_seed = f"{subject}. {description} {style_notes}"

    embeddings = await gemini_embed([embed_seed]) if embed_seed.strip() else []
    emb = embeddings[0] if embeddings else None

    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                UPDATE producer_references
                   SET ai_description=%s,
                       description_embedding=%s,
                       status='described',
                       metadata = metadata || %s::jsonb
                 WHERE id=%s
                """,
                (
                    description or veo_hint or None,
                    emb,
                    Jsonb({
                        "style_notes": style_notes,
                        "subject_summary": subject,
                        "veo_prompt_hint": veo_hint,
                        "dominant_colors": data.get("dominant_colors") or [],
                    }),
                    reference_id,
                ),
            )
    log.info("[ref=%d] described + embedded", reference_id)
    return {**row, "ai_description": description, "veo_prompt_hint": veo_hint}
