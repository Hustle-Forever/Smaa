"""Fire a Veo LRO from an approved script scene and persist the operation name.

v5.1 changes:
  - Accepts `scene_index` (default 1). Assembles the Veo prompt fresh from
    that scene's structured fields (not from script.veo_prompt which was
    pre-baked for scene 1 only).
  - Uses the LLM-picked per-scene `duration_sec` from scenes[scene_index-1]
    unless the caller overrides via `duration_sec`.
  - Auto-reference image (Nano Banana) is generated PER SCENE — preview_image
    is called with `scene_index` so the still depicts THAT scene.
  - INSERT stores the real `scene_index` (was hardcoded 1).

v5 (still in effect):
  - `negative_prompt` built from brand_donts is passed to Veo on every call.
  - `auto_reference=True` default: when no explicit reference is supplied,
    a brand-grounded still is auto-generated.
"""
from __future__ import annotations

import logging
from pathlib import Path

from psycopg.types.json import Jsonb

from ....config import config
from ....db import get_conn
from ....services.vertex_client import veo_generate
from ..memory import build_brand_requirements
from ..prompts import ALLOWED_SCENE_DURATIONS_SEC, MAX_SCENES_PER_SCRIPT
from . import preview_image as phase_preview_image
from .generate_script import _assemble_negative_prompt, _assemble_veo_prompt

log = logging.getLogger("archivist.producer.generate_video")


async def _ensure_reference_image(
    script_id: int,
    scene_index: int,
    explicit_refs: list[str] | None,
    script_refs: list[str] | None,
    auto_reference: bool,
) -> tuple[list[str], bool]:
    """Return (paths, auto_generated_used).

    Priority:
      1. Caller's explicit `reference_image_paths` (the user attached something).
      2. The script's stored refs (reference-mode runs auto-fill these).
      3. If still empty AND auto_reference is True, fire `preview_image` with
         this scene_index to generate a brand-grounded still depicting THIS
         scene.
    """
    if explicit_refs:
        return list(explicit_refs), False
    if script_refs:
        return list(script_refs), False
    if not auto_reference:
        return [], False
    try:
        ref_row = await phase_preview_image.run(script_id, scene_index=scene_index)
        path = ref_row.get("original_path") if isinstance(ref_row, dict) else None
        if path:
            log.info(
                "[script=%d scene=%d] auto-generated brand-grounded reference image ref=%d",
                script_id, scene_index, ref_row.get("id"),
            )
            return [str(path)], True
    except Exception as e:
        log.warning(
            "[script=%d scene=%d] auto-reference generation failed (proceeding without): %s",
            script_id, scene_index, e,
        )
    return [], False


async def run(
    script_id: int,
    *,
    use_premium_model: bool = False,
    with_audio: bool = False,
    duration_sec: int | None = None,
    aspect_ratio: str | None = None,
    reference_image_paths: list[str] | None = None,
    auto_reference: bool = True,
    scene_index: int = 1,
) -> dict | None:
    """Submit Veo for scene `scene_index` of `script_id` and return the new
    producer_videos row (status='generating', veo_operation_name set).

    `scene_index` is 1-based (matches the value persisted in producer_scripts.scenes).
    `duration_sec` (when provided) overrides the per-scene LLM-picked duration.
    """
    if scene_index < 1 or scene_index > MAX_SCENES_PER_SCRIPT:
        raise ValueError(
            f"scene_index {scene_index} out of range "
            f"[1, {MAX_SCENES_PER_SCRIPT}]"
        )

    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT s.id, s.idea_id, s.reference_image_paths, s.scenes,
                       s.metadata AS script_metadata,
                       (SELECT run_id FROM producer_ideas WHERE id=s.idea_id) AS run_id,
                       (SELECT MAX(id) FROM producer_videos
                         WHERE script_id=s.id AND scene_index=%s) AS parent_video_id
                  FROM producer_scripts s
                 WHERE s.id=%s
                """,
                (scene_index, script_id),
            )
            script = await cur.fetchone()
    if not script:
        raise ValueError(f"script {script_id} not found")

    scenes = script.get("scenes") or []
    if not isinstance(scenes, list) or len(scenes) < scene_index:
        raise ValueError(
            f"script {script_id} has no scene at index {scene_index} "
            f"(has {len(scenes) if isinstance(scenes, list) else 0} scenes)"
        )
    scene = scenes[scene_index - 1]
    if not isinstance(scene, dict):
        raise ValueError(f"script {script_id} scene {scene_index} is malformed")

    # Build the per-scene veo_prompt + negative_prompt fresh. The
    # script's stored veo_prompt is scene 1's only — never use it for
    # scenes 2+.
    # v6: read dialogue_language from the script's metadata so the audio
    # clause we append to the Veo prompt is in the right language. Fall back
    # to the run's flag if the script predates v6.
    script_meta = script.get("script_metadata") or {}
    dialogue_language: str | None = None
    if isinstance(script_meta, dict):
        dialogue_language = (script_meta.get("dialogue_language") or "").strip() or None
    if not dialogue_language:
        # Older scripts: re-read from the run as a fallback.
        from ..memory import get_run_dialogue_language  # local import: keeps the module DAG shallow
        try:
            dialogue_language = await get_run_dialogue_language(script["run_id"])
        except Exception:
            dialogue_language = None

    brand_req = await build_brand_requirements()
    per_scene_veo_prompt = _assemble_veo_prompt(
        scene, brand_req, dialogue_language=dialogue_language,
    )
    if not per_scene_veo_prompt.strip():
        raise ValueError(
            f"script {script_id} scene {scene_index} assembles to an empty Veo prompt"
        )

    # v6: auto-enable audio when the scene carries dialogue. The audio
    # clause in the Veo prompt is useless if generate_audio=False, and
    # we'd rather respect the script writer's intent than the caller's
    # default. Caller can still set with_audio=True explicitly for
    # non-dialogue scenes (ambient music etc.).
    if (scene.get("dialogue") or "").strip() and not with_audio:
        log.info(
            "[script=%d scene=%d] scene has dialogue → forcing with_audio=True",
            script_id, scene_index,
        )
        with_audio = True

    # Per-scene duration: prefer caller override, then the LLM-picked value
    # stored on the scene, then config default. Clamp to legal set as a
    # last line of defence.
    if duration_sec is None:
        scene_dur = scene.get("duration_sec")
        try:
            duration_sec = int(scene_dur) if scene_dur is not None else config.veo_max_duration_sec
        except (TypeError, ValueError):
            duration_sec = config.veo_max_duration_sec
    if duration_sec not in ALLOWED_SCENE_DURATIONS_SEC:
        duration_sec = min(
            ALLOWED_SCENE_DURATIONS_SEC,
            key=lambda v: abs(v - int(duration_sec)),
        )

    model = config.veo_model_premium if use_premium_model else config.veo_model

    # Resolve reference images (caller > script > auto-generated per scene)
    chosen_refs, auto_ref_used = await _ensure_reference_image(
        script_id,
        scene_index,
        reference_image_paths,
        script.get("reference_image_paths"),
        auto_reference,
    )
    ref_paths: list[Path] = []
    for p in chosen_refs[:3]:
        path = Path(p)
        if not path.is_absolute():
            path = Path.cwd() / path
        if path.exists():
            ref_paths.append(path)

    # Veo 3.1 constraint: the `reference_to_video` feature (i.e. any call with
    # a reference image attached) ONLY supports duration=8. The Fast model
    # supports 4/6/8 ONLY in pure text-to-video. When a ref is attached we
    # must coerce the duration to 8, regardless of what the LLM picked. The
    # scene's stored duration_sec keeps the LLM's intent for the record; the
    # coercion is logged + persisted in metadata.
    duration_coerced_for_ref = False
    if ref_paths and duration_sec != 8:
        log.info(
            "[script=%d scene=%d] ref image attached → coercing duration %ds → 8s "
            "(Veo 3.1 reference_to_video only supports 8s)",
            script_id, scene_index, duration_sec,
        )
        duration_sec = 8
        duration_coerced_for_ref = True

    # Negative prompt: prefer the one the script captured at gen time (so
    # script and video stay consistent); fall back to building fresh from the
    # current brand profile. (script_meta was hydrated above for dialogue_language.)
    neg_prompt = (
        script_meta.get("negative_prompt")
        if isinstance(script_meta, dict)
        else None
    )
    if not neg_prompt:
        neg_prompt = _assemble_negative_prompt(brand_req)

    op = await veo_generate(
        per_scene_veo_prompt,
        model=model,
        duration_sec=duration_sec,
        aspect_ratio=aspect_ratio,
        with_audio=with_audio,
        reference_images=ref_paths or None,
        negative_prompt=neg_prompt,
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
                          duration_sec, cost_usd, status, created_at
                """,
                (
                    script_id,
                    scene_index,
                    op["model"],
                    op["operation_name"],
                    op["duration_sec"],
                    op["cost_usd"],
                    script["parent_video_id"],
                    Jsonb({
                        "with_audio": op["with_audio"],
                        "aspect_ratio": op["aspect_ratio"],
                        "negative_prompt": neg_prompt,
                        "auto_reference_used": auto_ref_used,
                        "reference_image_count": len(ref_paths),
                        "scene_veo_prompt": per_scene_veo_prompt[:4000],
                        "duration_coerced_for_ref": duration_coerced_for_ref,
                        "scene_intent_duration_sec": int(scene.get("duration_sec") or 8),
                    }),
                ),
            )
            row = await cur.fetchone()
            await cur.execute(
                "UPDATE producer_runs SET status='video_pending' WHERE id=%s",
                (script["run_id"],),
            )
    log.info(
        "[script=%d scene=%d] video=%d submitted (op=%s model=%s dur=%ds refs=%d auto_ref=%s)",
        script_id, scene_index, row["id"], op["operation_name"], op["model"],
        duration_sec, len(ref_paths), auto_ref_used,
    )
    return row
