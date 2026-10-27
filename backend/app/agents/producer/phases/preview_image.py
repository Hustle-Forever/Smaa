"""Generate a still reference image for an approved script — brand-grounded.

v5 rewrite: drops the Gemini Flash "distill veo_prompt into image prompt"
step. The veo_prompt is itself bland (a paragraph) — distilling from it
inherits the blandness. Instead we build the image prompt deterministically
from:

  - the script's FIRST SCENE's structured fields (subject / cultural_anchor /
    palette / brand_motifs / typography / camera / lighting)
  - the brand requirements (hex codes, voice pillars, fonts) as a fallback
    when scene fields are sparse
  - optional `user_text` carrying refinement guidance

This guarantees the still always references brand palette + motifs, even
when the script LLM was lazy. The Veo call that downstream consumes this
image as a `VideoGenerationReferenceImage` inherits the brand pixels.
"""
from __future__ import annotations

import logging
import time
from typing import Any

from psycopg.types.json import Jsonb

from ....config import config
from ....db import get_conn
from ....services.vertex_client import gemini_image
from ..memory import build_brand_requirements

log = logging.getLogger("archivist.producer.preview_image")


def _assemble_image_prompt(
    scene: dict[str, Any] | None,
    brand_req: dict[str, Any],
    user_text: str | None,
) -> str:
    """Build a still-image prompt for Nano Banana from structured fields.

    Falls back to brand-requirement defaults when scene fields are empty
    (legacy scripts pre-v5 didn't have palette / brand_motifs / etc.).
    """
    scene = scene or {}

    # Subject + setting
    subject = (scene.get("subject") or "").strip()
    if not subject:
        subject = "a 42 Abu Dhabi student in a modern coding-school interior"
    cultural = (scene.get("cultural_anchor") or "").strip()
    if not cultural:
        cultural = "the 42 Abu Dhabi atrium at golden hour"

    # Palette: prefer scene-level, fall back to brand primary colors
    palette = list(scene.get("palette") or [])
    if not palette:
        primary = brand_req.get("primary_colors") or []
        palette = [f"{c} (primary palette)" for c in primary[:3]]

    # Motifs
    motifs = list(scene.get("brand_motifs") or [])
    if not motifs:
        style_pillars = brand_req.get("style_pillars") or []
        motifs = [str(p) for p in style_pillars[:2]]

    typography = (scene.get("typography") or "").strip()
    lighting = (scene.get("lighting") or "").strip()
    camera = (scene.get("camera") or "").strip()
    style = (scene.get("style") or "").strip()
    voice = brand_req.get("voice_pillars") or []

    parts: list[str] = [
        f"Subject: {subject}",
        f"Setting: {cultural}",
    ]
    if lighting:
        parts.append(f"Lighting: {lighting}")
    if camera:
        # For a still we use the camera framing, not the motion.
        parts.append(f"Framing: {camera.replace('slow push-in', 'medium shot').replace('locked-off', '')}".strip())
    if palette:
        parts.append(f"Color palette: {'; '.join(palette[:4])}")
    if motifs:
        parts.append(f"Visible brand motifs: {'; '.join(motifs[:3])}")
    if typography:
        parts.append(f"Typography on screen: {typography}")
    if style:
        style_clean = style.lstrip(", ").strip()
        if style_clean.lower().startswith("photorealistic"):
            parts.append(f"Photography style: {style_clean}, cinematic composition, 16:9")
        else:
            parts.append(f"Photography style: photorealistic still, {style_clean}, 16:9")
    else:
        parts.append("Photography style: photorealistic still, cinematic composition, 16:9")
    if voice:
        parts.append(f"Mood: {', '.join(voice[:3])}")
    if user_text:
        parts.append(f"Direction: {user_text.strip()}")

    return ". ".join(parts) + "."


async def run(
    script_id: int,
    *,
    user_text: str | None = None,
    aspect_ratio: str | None = None,
    scene_index: int | None = None,
) -> dict:
    """Generate a still for a script. When `scene_index` is provided (1-based),
    the image prompt is built from THAT scene's structured fields. Otherwise
    defaults to the first scene — matches legacy behavior for single-scene
    callers.
    """
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT s.id, s.veo_prompt, s.idea_id, s.scenes,
                       (SELECT i.run_id FROM producer_ideas i WHERE i.id=s.idea_id) AS run_id
                  FROM producer_scripts s WHERE s.id=%s
                """,
                (script_id,),
            )
            src = await cur.fetchone()
    if not src:
        raise ValueError(f"script {script_id} not found")

    # Pick the scene this still depicts. scene_index is 1-based.
    scenes = src.get("scenes") or []
    chosen_scene: dict | None = None
    effective_index: int | None = None
    if isinstance(scenes, list) and scenes:
        if scene_index is None:
            chosen_scene = scenes[0]
            effective_index = 1
        else:
            try:
                if 1 <= scene_index <= len(scenes):
                    chosen_scene = scenes[scene_index - 1]
                    effective_index = scene_index
                else:
                    # Out-of-range: fall back to scene 1 and log
                    log.warning(
                        "[script=%d] preview_image scene_index=%d out of range "
                        "(have %d scenes); falling back to scene 1",
                        script_id, scene_index, len(scenes),
                    )
                    chosen_scene = scenes[0]
                    effective_index = 1
            except TypeError:
                chosen_scene = scenes[0]
                effective_index = 1

    brand_req = await build_brand_requirements()
    image_prompt = _assemble_image_prompt(chosen_scene, brand_req, user_text)
    log.info(
        "[script=%d scene=%s] image prompt: %s",
        script_id, effective_index, image_prompt[:200],
    )

    aspect = aspect_ratio or config.veo_default_aspect
    image_bytes = await gemini_image(image_prompt, aspect_ratio=aspect)

    run_id = src["run_id"]
    out_dir = config.producer_root / str(run_id)
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = int(time.time() * 1000)
    suffix = ".jpg" if image_bytes[:3] == b"\xff\xd8\xff" else ".png"
    scene_tag = f"-s{effective_index}" if effective_index else ""
    out_path = out_dir / f"script-{script_id}{scene_tag}-preview-{ts}{suffix}"
    out_path.write_bytes(image_bytes)
    log.info(
        "[script=%d scene=%s] preview image saved → %s (%d bytes)",
        script_id, effective_index, out_path.name, len(image_bytes),
    )

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
                    image_prompt,
                    Jsonb({
                        "source": "auto_preview_v5",
                        "script_id": script_id,
                        "scene_index": effective_index,
                        "image_prompt": image_prompt[:2000],
                        "user_text": (user_text or "").strip()[:1000] or None,
                        "brand_palette_used": brand_req.get("primary_colors") or [],
                        "from_first_scene": effective_index == 1,
                    }),
                ),
            )
            row = await cur.fetchone()
    return row
