"""Fire Veo LROs for ALL scenes of an approved script — in parallel.

v5.1 — the multi-scene fan-out path. Each scene becomes a separate Veo
LRO, each with its own brand-grounded Nano Banana still as reference. The
veo_poller picks them up individually as the LROs complete. The final
Remotion "compose all scenes" step is fired manually by the user after
every scene reaches status='ready'.

Six cost-guard layers protect against runaway spending:

  1. MAX_SCENES_PER_SCRIPT (from prompts.py) — defense-in-depth cap. Even
     if a script's scenes JSONB somehow claims 60 entries, we slice [:N].
  2. Pre-flight cost estimate — sum estimated Veo cost across all scenes.
     If > config.producer_max_script_cost_usd ($3 default), refuse the
     batch BEFORE any Veo call goes out.
  3. Idempotency by (script_id, scene_index) — skip any scene that already
     has a producer_videos row with status IN ('generating','ready','approved').
     Prevents accidental double-firing.
  4. VertexCostMeter / VEO_BUDGET_USD — already in place as the outermost
     global ring. Hard-stops any further Vertex call once crossed.
  5. scene_index validation in `generate_video.run` — refuses anything
     outside [1, MAX_SCENES_PER_SCRIPT] even if a guard above is misconfigured.
  6. Per-scene exceptions are caught and reported; a single failing scene
     does NOT abort the others. Returned `submitted` + `errors` give the FE
     the data it needs to render partial state.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from ....config import config
from ....db import get_conn
from ....services.vertex_client import get_meter
from ..prompts import ALLOWED_SCENE_DURATIONS_SEC, MAX_SCENES_PER_SCRIPT
from . import generate_video as phase_generate_video

log = logging.getLogger("archivist.producer.render_all_scenes")


# -----------------------------------------------------------------------------
# Public surface
# -----------------------------------------------------------------------------

async def estimate(
    script_id: int,
    *,
    use_premium_model: bool = False,
    with_audio: bool = False,
    auto_reference: bool = True,
) -> dict:
    """Return a cost preview WITHOUT firing anything. Used by the frontend's
    confirm dialog so the user sees the exact dollar figure before clicking.

    When `auto_reference=True` (the render-all default) OR when the script
    has stored reference_image_paths, every scene's actual Veo call uses
    duration=8 regardless of what the LLM picked. The estimate honors that
    coercion so the preview matches the real cost.
    """
    info = await _load_script_with_live_scenes(script_id)
    scenes = info["scenes"]
    live = info["live_scene_indexes"]
    script_has_refs = info["script_has_refs"]
    # Will every scene use a reference image at render time?
    refs_will_attach = auto_reference or script_has_refs

    model = config.veo_model_premium if use_premium_model else config.veo_model
    meter = get_meter()

    per_scene: list[dict] = []
    total = 0.0
    for s in scenes[:MAX_SCENES_PER_SCRIPT]:
        si = int(s.get("scene_index") or 0)
        intent_dur = _legal_duration(s.get("duration_sec"))
        # Veo 3.1: reference_to_video locks duration to 8s.
        effective_dur = 8 if (refs_will_attach and intent_dur != 8) else intent_dur
        already = si in live
        est = 0.0 if already else meter.estimate_veo(model, effective_dur, with_audio)
        per_scene.append({
            "scene_index": si,
            "duration_sec": effective_dur,
            "intent_duration_sec": intent_dur,
            "coerced_to_8_for_ref": effective_dur != intent_dur,
            "model": model,
            "est_usd": round(est, 4),
            "already_live": already,
        })
        if not already:
            total += est

    return {
        "scene_count": len(per_scene),
        "per_scene": per_scene,
        "total_est_usd": round(total, 4),
        "live_scene_indexes": sorted(live),
        "auto_reference": auto_reference,
        "refs_will_attach": refs_will_attach,
        "max_script_cost_usd": config.producer_max_script_cost_usd,
        "over_cap": total > config.producer_max_script_cost_usd,
    }


async def run(
    script_id: int,
    *,
    use_premium_model: bool = False,
    with_audio: bool = False,
    aspect_ratio: str | None = None,
    auto_reference: bool = True,
) -> dict:
    """Submit Veo LROs for every scene in `script_id` that doesn't already
    have a live video. Returns the same shape as `estimate()` plus
    `submitted` and `errors`.

    Cost safety: this method refuses to submit ANY scene if the estimated
    aggregate cost exceeds `config.producer_max_script_cost_usd`. Idempotency
    skips scenes that already have producer_videos rows in
    ('generating','ready','approved').
    """
    info = await _load_script_with_live_scenes(script_id)
    scenes: list[dict] = info["scenes"]
    live: set[int] = info["live_scene_indexes"]

    if not scenes:
        raise ValueError(f"script {script_id} has no scenes")

    # Layer 1 — cap at MAX_SCENES_PER_SCRIPT
    capped_scenes = scenes[:MAX_SCENES_PER_SCRIPT]
    if len(scenes) > MAX_SCENES_PER_SCRIPT:
        log.warning(
            "[script=%d] scenes JSONB has %d entries; capping to %d",
            script_id, len(scenes), MAX_SCENES_PER_SCRIPT,
        )

    # Resolve scene_indexes to render (idempotency filter)
    todo: list[dict] = []
    skipped: list[int] = []
    for s in capped_scenes:
        si = int(s.get("scene_index") or 0)
        # Layer 5 (early) — scene_index must be in legal range
        if si < 1 or si > MAX_SCENES_PER_SCRIPT:
            log.warning(
                "[script=%d] skipping scene with out-of-range scene_index=%d",
                script_id, si,
            )
            continue
        if si in live:
            skipped.append(si)
        else:
            todo.append(s)

    if not todo:
        log.info(
            "[script=%d] all %d scenes already have live videos; nothing to fire",
            script_id, len(capped_scenes),
        )
        return {
            "script_id": script_id,
            "submitted": [],
            "skipped_scene_indexes": sorted(skipped),
            "errors": [],
            "total_est_usd": 0.0,
            "max_script_cost_usd": config.producer_max_script_cost_usd,
        }

    # Layer 2 — pre-flight cost gate (honors the ref→8s coercion so the
    # cap math matches what we'll actually submit to Veo)
    model = config.veo_model_premium if use_premium_model else config.veo_model
    meter = get_meter()
    refs_will_attach = auto_reference or info["script_has_refs"]
    total_est = 0.0
    for s in todo:
        intent_dur = _legal_duration(s.get("duration_sec"))
        effective_dur = 8 if (refs_will_attach and intent_dur != 8) else intent_dur
        total_est += meter.estimate_veo(model, effective_dur, with_audio)
    if total_est > config.producer_max_script_cost_usd:
        raise RuntimeError(
            f"Estimated ${total_est:.2f} for {len(todo)} scene(s) exceeds "
            f"PRODUCER_MAX_SCRIPT_COST_USD=${config.producer_max_script_cost_usd:.2f}. "
            "Reduce scene count, switch off audio, use the fast model, or raise the env cap."
        )

    log.info(
        "[script=%d] firing %d scene(s) in parallel "
        "(scene_indexes=%s, model=%s, with_audio=%s, est=$%.4f, skipped=%s)",
        script_id, len(todo), [s.get("scene_index") for s in todo],
        model, with_audio, total_est, skipped,
    )

    # Layer 6 — per-scene try/except so one failure doesn't abort the batch
    async def _fire_one(scene: dict) -> tuple[int, dict | None, str | None]:
        si = int(scene.get("scene_index") or 0)
        try:
            row = await phase_generate_video.run(
                script_id,
                scene_index=si,
                use_premium_model=use_premium_model,
                with_audio=with_audio,
                aspect_ratio=aspect_ratio,
                auto_reference=auto_reference,
            )
            return si, row, None
        except Exception as e:
            log.exception("[script=%d scene=%d] scene fire failed", script_id, si)
            return si, None, str(e)[:300]

    results = await asyncio.gather(*(_fire_one(s) for s in todo))

    submitted: list[dict] = []
    errors: list[dict] = []
    for si, row, err in results:
        if err is not None:
            errors.append({"scene_index": si, "error": err})
        elif row is not None:
            submitted.append(row)

    return {
        "script_id": script_id,
        "submitted": submitted,
        "skipped_scene_indexes": sorted(skipped),
        "errors": errors,
        "total_est_usd": round(total_est, 4),
        "max_script_cost_usd": config.producer_max_script_cost_usd,
    }


# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------

def _legal_duration(raw: Any) -> int:
    """Clamp a raw duration to the legal Veo set, defaulting to 8 on garbage."""
    try:
        v = int(round(float(raw))) if raw is not None else 8
    except (TypeError, ValueError):
        v = 8
    if v in ALLOWED_SCENE_DURATIONS_SEC:
        return v
    return min(ALLOWED_SCENE_DURATIONS_SEC, key=lambda x: abs(x - v))


async def _load_script_with_live_scenes(script_id: int) -> dict:
    """Return `{"scenes": [...], "live_scene_indexes": {1, 2, ...}, "script_has_refs": bool}`.

    `live_scene_indexes` is the set of scene_index values that already have a
    producer_videos row in ('generating','ready','approved') — the
    idempotency filter.
    `script_has_refs` indicates that the script already has stored
    reference_image_paths (reference-mode runs). If true, every Veo call
    on this script will use the reference_to_video feature → 8s lock.
    """
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT scenes, reference_image_paths FROM producer_scripts WHERE id=%s",
                (script_id,),
            )
            row = await cur.fetchone()
            if not row:
                raise ValueError(f"script {script_id} not found")
            scenes = row.get("scenes") or []
            if not isinstance(scenes, list):
                scenes = []
            ref_paths = row.get("reference_image_paths") or []
            script_has_refs = isinstance(ref_paths, list) and len(ref_paths) > 0

            await cur.execute(
                """
                SELECT DISTINCT scene_index
                  FROM producer_videos
                 WHERE script_id=%s
                   AND scene_index IS NOT NULL
                   AND status IN ('generating','ready','approved')
                """,
                (script_id,),
            )
            live = {r["scene_index"] for r in await cur.fetchall() if r["scene_index"] is not None}

    return {
        "scenes": scenes,
        "live_scene_indexes": live,
        "script_has_refs": script_has_refs,
    }
