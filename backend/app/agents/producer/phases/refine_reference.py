"""Image-to-image variation of an existing reference via Nano Banana.

Works on any `producer_references` row that's kind='image' with a local file:

  - Auto / user-idea modes: refine an auto-generated preview image based on
    the current image + user feedback.
  - Reference mode: refine a user-uploaded image into a styled variation
    before the user commits to it.

The new image becomes a sibling `producer_references` row with
`metadata.source='nano_banana_variation'` and `metadata.parent_reference_id`
pointing at the source. Both rows stay in the run's reference list so the
user can switch between them or chain multiple refinements.
"""
from __future__ import annotations

import logging
import time
from pathlib import Path

from psycopg.types.json import Jsonb

from ....config import config
from ....db import get_conn
from ....services.vertex_client import gemini_image, gemini_text

log = logging.getLogger("archivist.producer.refine_reference")

_REFINE_SYSTEM = """\
You compose an image-to-image prompt: a short instruction telling an image
model how to modify the provided source image into a new version.

Rules:
- ONE concise prompt (1-3 sentences).
- Refer to the source image as "the image" or "the source".
- Honour the user's intent verbatim — don't over-interpret.
- Output ONLY the prompt, no preamble.
"""


async def _compose_refine_prompt(
    source_description: str | None,
    user_text: str | None,
) -> str:
    desc = (source_description or "").strip()
    user = (user_text or "").strip()
    if not user and not desc:
        return "Generate a refined variation of the source image, preserving its subject and palette but improving composition and lighting."
    if not user:
        # User didn't say anything — produce a "make it better" variation
        return (
            f"Source: {desc}\n\n"
            "Generate a refined variation of the source image that preserves "
            "its subject and palette but improves composition and lighting."
        )
    # Use Gemini Flash to compose a clean image-edit prompt
    payload = (
        (f"SOURCE IMAGE DESCRIPTION:\n{desc}\n\n" if desc else "")
        + f"USER INSTRUCTION:\n{user}\n\nIMAGE-TO-IMAGE PROMPT:"
    )
    out = await gemini_text(
        payload,
        system=_REFINE_SYSTEM,
        model=config.vertex_gemini_fast_model,
        temperature=0.4,
        max_tokens=600,
    )
    return (out or "").strip().strip('"') or user


async def run(
    source_reference_id: int,
    *,
    user_text: str | None = None,
    aspect_ratio: str | None = None,
) -> dict:
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT id, run_id, kind, original_path, ai_description, metadata
                  FROM producer_references WHERE id=%s
                """,
                (source_reference_id,),
            )
            src = await cur.fetchone()
    if not src:
        raise ValueError(f"reference {source_reference_id} not found")
    if src["kind"] != "image":
        raise RuntimeError(
            f"reference {source_reference_id} is kind={src['kind']}; "
            "only image references can be refined"
        )
    if not src["original_path"]:
        raise RuntimeError(
            f"reference {source_reference_id} has no file on disk"
        )
    src_path = Path(src["original_path"])
    if not src_path.exists():
        raise RuntimeError(f"reference file missing: {src_path}")

    prompt = await _compose_refine_prompt(src.get("ai_description"), user_text)
    log.info(
        "[ref=%d] refining with prompt: %s",
        source_reference_id, prompt[:200],
    )

    aspect = aspect_ratio or config.veo_default_aspect
    image_bytes = await gemini_image(
        prompt,
        aspect_ratio=aspect,
        source_images=[src_path],
    )

    run_id = src["run_id"]
    out_dir = config.producer_root / str(run_id)
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = int(time.time() * 1000)
    suffix = ".jpg" if image_bytes[:3] == b"\xff\xd8\xff" else ".png"
    out_path = (
        out_dir / f"refine-from-{source_reference_id}-{ts}{suffix}"
    )
    out_path.write_bytes(image_bytes)
    log.info(
        "[ref=%d → file=%s] refined image saved (%d bytes)",
        source_reference_id, out_path.name, len(image_bytes),
    )

    src_meta = src.get("metadata") or {}
    script_id = src_meta.get("script_id") if isinstance(src_meta, dict) else None

    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                INSERT INTO producer_references
                  (run_id, kind, original_path, ai_description, status, metadata)
                VALUES (%s, 'image', %s, %s, 'described', %s)
                RETURNING id, run_id, kind, original_path, ai_description,
                          status, metadata, created_at
                """,
                (
                    run_id,
                    str(out_path),
                    prompt,
                    Jsonb({
                        "source": "nano_banana_variation",
                        "parent_reference_id": source_reference_id,
                        "image_prompt": prompt[:2000],
                        "user_text": (user_text or "").strip()[:1000] or None,
                        **({"script_id": script_id} if script_id else {}),
                    }),
                ),
            )
            row = await cur.fetchone()
    return row
