"""Turn an approved idea into a Veo-ready script.

v5 — server-side veo_prompt assembly from structured scene fields. Brand
vocabulary force-injected even if the LLM tried to skip it.

v5.1 — additions:
  - scene_count and scene_duration_mode kwargs let the caller dictate how
    many scenes and what duration policy. None = let the LLM choose.
  - video_type directive injected into the USER message (after brand
    requirements), not the system prompt. Same fix that made brand
    requirements land — directives in user-message form get honored.
  - "Each scene runs 8 seconds." removed from the user prompt — the LLM
    now picks per-scene duration from {4, 6, 8} based on motion complexity.
  - _sanitise_scenes reads the LLM's duration_sec, clamps to the allowed
    set, falls back to 8 if missing/invalid. The `fixed_duration` lock
    is gone.
  - MAX_SCENES_PER_SCRIPT is enforced explicitly (was implicit `out[:3]`).
"""
from __future__ import annotations

import logging
import re
from typing import Any, Optional

from psycopg.types.json import Jsonb

from ....config import config
from ....db import get_conn
from ....services import asset_storage
from ....services.vertex_client import gemini_embed, gemini_text
from ..memory import (
    build_brand_requirements,
    build_context_block,
    format_brand_requirements,
    format_video_type_directive,
    get_run_dialogue_language,
    get_run_video_type,
    get_run_video_type_key,
)
from ..prompts import (
    ALLOWED_SCENE_DURATIONS_SEC,
    DIALOGUE_REQUIRED_VIDEO_TYPES,
    MAX_SCENES_PER_SCRIPT,
    SCRIPT_SCHEMA,
    SCRIPT_SYSTEM,
    format_audio_clause,
    format_dialogue_language_directive,
)

# Producer character references in scene.subject look like '#7 yousef_b ...'
_CHAR_REF_RE = re.compile(r"#(\d+)\b")

log = logging.getLogger("archivist.producer.generate_script")


# -----------------------------------------------------------------------------
# v5: server-side Veo prompt assembly
# -----------------------------------------------------------------------------

def _assemble_veo_prompt(
    scene: dict,
    brand_req: dict,
    *,
    dialogue_language: str | None = None,
) -> str:
    """Build the final Veo prompt deterministically from structured scene fields.

    Veo responds best to structured, specific prompts. We emit a labelled
    section per concern (Subject/Setting/Action/...) so the model has clear
    handles on each element. Brand vocabulary (palette hex codes, motifs,
    cultural anchor, voice pillars) is force-injected even if the LLM tried
    to skip it.

    When `scene.dialogue` is non-empty, an Audio: clause is appended at the
    end. Veo 3.1 generates speech directly from the visual prompt — there's
    no separate SDK parameter — so the dialogue goes into the prompt string
    itself, in quotes, with a language/tone hint.
    """
    parts: list[str] = []

    subject = (scene.get("subject") or "").strip()
    action = (scene.get("action") or "").strip()
    cultural = (scene.get("cultural_anchor") or "").strip()
    camera = (scene.get("camera") or "").strip()
    lighting = (scene.get("lighting") or "").strip()
    style = (scene.get("style") or "").strip()
    palette = list(scene.get("palette") or [])
    motifs = list(scene.get("brand_motifs") or [])
    typography = (scene.get("typography") or "").strip()
    dialogue = (scene.get("dialogue") or "").strip()

    # Subject + setting + action
    if subject:
        parts.append(f"Subject: {subject}")
    if cultural:
        parts.append(f"Setting: {cultural}")
    else:
        # Cultural anchor is REQUIRED by schema, but if the LLM emitted ""
        # we still ground the scene in Abu Dhabi rather than nowhere.
        parts.append("Setting: 42 Abu Dhabi atrium with generative-pattern walls, Abu Dhabi")
    if action:
        parts.append(f"Action: {action}")

    # Camera + lighting
    if camera:
        parts.append(f"Camera: {camera}")
    if lighting:
        parts.append(f"Lighting: {lighting}")

    # Brand-specific vocabulary — palette + motifs + typography
    if palette:
        parts.append(f"Color palette: {'; '.join(palette)}")
    else:
        # Force at least the primary palette even if the LLM left palette empty
        pcs = brand_req.get("primary_colors") or []
        if pcs:
            parts.append("Color palette: " + ", ".join(pcs[:3]) + " (brand-required)")
    if motifs:
        parts.append(f"Brand motifs visible in frame: {'; '.join(motifs)}")
    if typography:
        parts.append(f"Typography on screen: {typography}")

    # Style + voice pillars (avoid "photorealistic, photorealistic" duplication)
    if style:
        style_clean = style.lstrip(", ").strip()
        if style_clean.lower().startswith("photorealistic"):
            parts.append(f"Style: {style_clean}")
        else:
            parts.append(f"Style: photorealistic, {style_clean}")
    else:
        parts.append("Style: photorealistic, cinematic")
    voice = brand_req.get("voice_pillars") or []
    if voice:
        parts.append(f"Mood / voice pillars: {', '.join(voice[:4])}")

    # Audio / dialogue — Veo 3.1 reads speech instructions directly from the
    # visual prompt. This is the ONLY way to get the model to render specific
    # spoken words; without it Veo invents ambient audio.
    audio = format_audio_clause(dialogue, dialogue_language)
    if audio:
        parts.append(audio)

    return ". ".join(parts) + "."


def scene_needs_dialogue(video_type_key: str | None) -> bool:
    """True when the run's video_type forces non-empty dialogue per scene."""
    if not video_type_key:
        return False
    return video_type_key in DIALOGUE_REQUIRED_VIDEO_TYPES


def _assemble_negative_prompt(brand_req: dict) -> str:
    """Build Veo's negative_prompt from common failure modes + brand DONTs."""
    base = [
        "corporate stock-photo classroom",
        "generic handshake meeting",
        "people typing at laptops with floating UI overlays",
        "low-quality animation",
        "amateur lighting",
        "blurry text",
        "text overlays added in post",
        "watermarks",
    ]
    brand_dont = [d.lower() for d in (brand_req.get("brand_donts") or [])]
    return ", ".join(base + brand_dont)


# -----------------------------------------------------------------------------
# v5.1: user-prompt directive builders
# -----------------------------------------------------------------------------

def _scene_count_directive(scene_count: Optional[int]) -> str:
    """Tell the LLM exactly how many scenes to produce, or to choose freely."""
    if scene_count is None:
        return (
            f"SCENE COUNT: produce 1-{MAX_SCENES_PER_SCRIPT} scenes. Pick the "
            f"number that best fits the idea — a single decisive moment is "
            f"often stronger than three weak ones."
        )
    n = max(1, min(int(scene_count), MAX_SCENES_PER_SCRIPT))
    return (
        f"SCENE COUNT: produce EXACTLY {n} scene{'s' if n != 1 else ''}. "
        f"Do not produce more or fewer."
    )


def _scene_duration_directive(scene_duration_mode: Optional[str]) -> str:
    """Tell the LLM whether to vary per-scene durations or force a fixed value."""
    mode = (scene_duration_mode or "auto").lower()
    if mode in ("4", "6", "8"):
        return (
            f"SCENE DURATION: set duration_sec = {mode} on EVERY scene. "
            f"Do not vary durations."
        )
    return (
        "SCENE DURATION: pick duration_sec ∈ {4, 6, 8} per scene based on motion "
        "complexity. 4s = a held reaction or a single beat with near-zero motion. "
        "6s = a contained action (a push-in, a gesture, one line). 8s = full "
        "camera move + action + reveal. VARY the durations across scenes — "
        "don't default everything to 8s."
    )


# -----------------------------------------------------------------------------
# Main phase
# -----------------------------------------------------------------------------

async def run(
    idea_id: int,
    *,
    retry_feedback: str | None = None,
    scene_count: int | None = None,
    scene_duration_mode: str | None = None,
) -> dict | None:
    """Generate a producer_scripts row for an approved idea. Returns the row.

    Args:
        idea_id: the approved idea to script.
        retry_feedback: optional free-form note from a prior decline.
        scene_count: 1/2/3 (capped at MAX_SCENES_PER_SCRIPT), or None to
            let the LLM choose.
        scene_duration_mode: "auto" (LLM picks 4/6/8 per scene) or "4"/"6"/"8"
            to force a uniform duration. None = "auto".
    """
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT i.id, i.run_id, i.title, i.concept, i.visual_direction,
                       i.reason, i.style_mood, i.metadata AS idea_metadata,
                       (SELECT MAX(version_index) FROM producer_scripts WHERE idea_id=i.id) AS prev_version,
                       (SELECT MAX(id) FROM producer_scripts WHERE idea_id=i.id) AS parent_script_id
                  FROM producer_ideas i
                 WHERE i.id=%s
                """,
                (idea_id,),
            )
            idea = await cur.fetchone()
            if not idea:
                raise ValueError(f"idea {idea_id} not found")
            await cur.execute(
                """
                SELECT original_path FROM producer_references
                 WHERE run_id=%s AND kind='image'
                   AND original_path IS NOT NULL
                   AND status IN ('described','approved')
                 ORDER BY id ASC LIMIT 3
                """,
                (idea["run_id"],),
            )
            ref_paths = [r["original_path"] for r in await cur.fetchall()]

    version = (idea["prev_version"] or 0) + 1
    parent_script_id = idea["parent_script_id"] if version > 1 else None

    query_for_taste = f"{idea['title']}. {idea['concept']} {idea['visual_direction']}"
    dialogue_language = await get_run_dialogue_language(idea["run_id"])
    vt_key = await get_run_video_type_key(idea["run_id"])
    dialogue_required = scene_needs_dialogue(vt_key)
    soft_context = await build_context_block(
        query_text=query_for_taste,
        dialogue_language=dialogue_language,
        # SCRIPT phase: no AVOID block — repetition pressure lives at idea-gen,
        # and the script just executes one approved idea.
        avoid_recent_run_id=None,
    )
    brand_req = await build_brand_requirements()
    req_block = format_brand_requirements(brand_req)
    vt = await get_run_video_type(idea["run_id"])
    vt_directive = format_video_type_directive(vt, "script")
    lang_directive = format_dialogue_language_directive(
        dialogue_language, required=dialogue_required,
    )

    # Pass any brand_anchors carried from the idea forward so the script
    # builds on them rather than re-inventing.
    idea_meta = idea.get("idea_metadata") or {}
    idea_anchors = idea_meta.get("brand_anchors") if isinstance(idea_meta, dict) else None
    anchors_hint = ""
    if isinstance(idea_anchors, dict):
        anchors_hint = (
            "\nBRAND ANCHORS CARRIED FROM THE IDEA (build on these):\n"
            f"  palette_used: {idea_anchors.get('palette_used') or []}\n"
            f"  motif: {idea_anchors.get('motif') or ''}\n"
            f"  tagline_echo: {idea_anchors.get('tagline_echo') or ''}\n"
            f"  voice_pillars: {idea_anchors.get('voice_pillars') or []}\n"
        )

    retry_block = ""
    if retry_feedback:
        retry_block = (
            f"\nThe previous script for this idea was rejected. User feedback:\n"
            f"```\n{retry_feedback.strip()}\n```\nIncorporate this feedback.\n"
        )

    scene_count_block = _scene_count_directive(scene_count)
    duration_block = _scene_duration_directive(scene_duration_mode)

    # USER prompt: requirements + video_type + language FIRST (highest LLM
    # attention), then soft context, then the actual task. Scene count +
    # duration are ALSO in the user message since they're concrete output
    # constraints.
    dialogue_reminder = (
        "EVERY scene MUST have a non-empty `dialogue` field (the EXACT words "
        "spoken / narrated). "
        if dialogue_required
        else "The `dialogue` field is OPTIONAL for this video_type — leave it "
             "as \"\" unless a spoken line is essential. "
    )
    user_prompt = (
        f"{req_block}\n"
        f"{vt_directive}\n"
        f"{lang_directive}\n"
        f"{soft_context}\n"
        f"APPROVED IDEA TO SCRIPT:\n"
        f"  Title: {idea['title']}\n"
        f"  Concept: {idea['concept']}\n"
        f"  Visual direction: {idea['visual_direction']}\n"
        f"  Style/mood: {idea['style_mood']}\n"
        f"  Reason: {idea['reason']}\n"
        f"{anchors_hint}"
        f"{retry_block}"
        f"\n{scene_count_block}\n"
        f"{duration_block}\n"
        f"\n{dialogue_reminder}"
        "Fill EVERY required field in the schema for each scene, especially "
        "`palette`, `brand_motifs`, and `cultural_anchor`. Apply the 5 RULES OF "
        "A SHOOTABLE BEAT to each scene — concrete, showable, singular, "
        "culturally-anchored, no clichés."
    )
    data = await gemini_text(
        user_prompt,
        system=SCRIPT_SYSTEM,
        json_schema=SCRIPT_SCHEMA,
        temperature=0.6,
        max_tokens=5000,
    )
    if not isinstance(data, dict) or not data.get("scenes"):
        log.warning("gemini returned no usable script; raw=%s", str(data)[:300])
        return None

    scenes = _sanitise_scenes(data["scenes"])
    if not scenes:
        log.warning("script had zero usable scenes after sanitisation")
        return None

    # Post-hoc dialogue validator — only fires when the run's video_type
    # requires dialogue. Mirrors the subject_type validator pattern in
    # generate_ideas: one retry max with explicit feedback.
    if dialogue_required:
        missing = [
            s["scene_index"] for s in scenes if not (s.get("dialogue") or "").strip()
        ]
        if missing:
            log.info(
                "[idea=%d] dialogue retry — scenes %s missing dialogue",
                idea_id, missing,
            )
            feedback = (
                "\nYOUR PREVIOUS OUTPUT WAS REJECTED. Regenerate the script.\n"
                f"DIALOGUE VIOLATION — scenes {missing} had empty `dialogue`. "
                "This video_type REQUIRES a non-empty `dialogue` string on "
                "every scene. Write the EXACT spoken words (in the language "
                "from the LANGUAGE directive), opening with a hook in the "
                "first 1.5 seconds. Do not return empty dialogue.\n"
            )
            retry_data = await gemini_text(
                user_prompt + feedback,
                system=SCRIPT_SYSTEM,
                json_schema=SCRIPT_SCHEMA,
                temperature=0.6,
                max_tokens=5000,
            )
            if isinstance(retry_data, dict) and retry_data.get("scenes"):
                retry_scenes = _sanitise_scenes(retry_data["scenes"])
                # Accept the retry even if some scenes are still missing —
                # better partial dialogue than no retry. Worst case the
                # downstream Veo prompt has no audio clause for the bad scene.
                if retry_scenes:
                    scenes = retry_scenes

    # v3 — resolve character library references like "#7 yousef_b" in any
    # scene.subject. For each match: pull the producer_assets row, append
    # its file path to ref_paths, increment usage_count.
    character_paths, used_character_ids = await _resolve_character_refs(scenes)
    if character_paths:
        # Combine with any existing image references (de-duped, order preserved)
        seen = set(ref_paths or [])
        combined = list(ref_paths or [])
        for p in character_paths:
            if p not in seen:
                combined.append(p)
                seen.add(p)
        ref_paths = combined
        await _bump_character_usage(used_character_ids)

    # SERVER-ASSEMBLE the Veo prompt from the FIRST scene's structured fields
    # (this is what the legacy single-scene /scripts/{id}/video path uses).
    # render_all_scenes builds per-scene prompts at render-time from each
    # scene's structured fields independently — it needs to read the run's
    # dialogue_language too; see metadata.dialogue_language we persist below.
    veo_prompt = _assemble_veo_prompt(
        scenes[0], brand_req, dialogue_language=dialogue_language,
    )
    negative_prompt = _assemble_negative_prompt(brand_req)
    # When ANY scene has dialogue, audio MUST be on — the audio clause in
    # the Veo prompt is useless if generate_audio=False.
    any_dialogue = any((s.get("dialogue") or "").strip() for s in scenes)

    embed_text = veo_prompt
    embeddings = await gemini_embed([embed_text]) if embed_text else []
    emb = embeddings[0] if embeddings else None

    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                INSERT INTO producer_scripts
                  (idea_id, version_index, scenes, veo_prompt, embedding,
                   parent_script_id, reference_image_paths, metadata)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING id, idea_id, version_index, scenes, veo_prompt, status,
                          reference_image_paths, created_at
                """,
                (
                    idea_id,
                    version,
                    Jsonb(scenes),
                    veo_prompt[:8000],
                    emb,
                    parent_script_id,
                    ref_paths or None,
                    Jsonb({
                        "reference_count": len(ref_paths),
                        "negative_prompt": negative_prompt,
                        "veo_prompt_source": "server_assembled_v6",
                        "brand_palette_used": brand_req.get("primary_colors") or [],
                        "scene_count_requested": scene_count,
                        "scene_duration_mode": (scene_duration_mode or "auto").lower(),
                        "scene_durations": [s["duration_sec"] for s in scenes],
                        # v6: dialogue + language carried so render_all_scenes
                        # and the Veo poller can read them back without
                        # re-querying the run.
                        "dialogue_language": dialogue_language,
                        "dialogue_required": dialogue_required,
                        "has_dialogue": any_dialogue,
                    }),
                ),
            )
            row = await cur.fetchone()
            await cur.execute(
                "UPDATE producer_runs SET status='script_pending' "
                " WHERE id=(SELECT run_id FROM producer_ideas WHERE id=%s)",
                (idea_id,),
            )
    log.info(
        "[idea=%d] script=%d (v%d) generated — %d scenes, durations=%s",
        idea_id, row["id"], version, len(scenes),
        [s["duration_sec"] for s in scenes],
    )
    return row


async def _local_character_ref(r: dict) -> str | None:
    """An absolute local file path for one character row, or None (logged).

    Never a URL: generate_video keeps only refs that exist on disk, so a signed
    URL here was silently dropped and the character never reached Veo. Uses
    the row's own local file when it is there, else caches the stored copy
    under producer_root/refs_cache (reused on the next script)."""
    aid = r.get("id")
    if r.get("local_path"):
        try:
            p = asset_storage.local_to_abs(r["local_path"])
            if p.is_file():
                return str(p)
        except Exception:  # noqa: BLE001 - an unreadable path is just "not local"
            pass
    bucket, key = r.get("storage_bucket"), r.get("storage_path")
    if not (bucket and key):
        log.info("char ref #%s skipped: no local file and no stored copy", aid)
        return None
    ext = asset_storage.safe_ext(key, r.get("mime_type"))
    dest = config.producer_root / "refs_cache" / f"asset-{int(aid)}{ext}"
    if dest.is_file() and dest.stat().st_size > 0:
        return str(dest)
    try:
        data = await asset_storage.download_bytes(storage_bucket=bucket, storage_path=key)
        if not data:
            raise ValueError("empty object")
        dest.parent.mkdir(parents=True, exist_ok=True)
        # Write then rename, so a crash mid-write never leaves a torn file that
        # the reuse check above would take for a good one.
        tmp = dest.with_name(dest.name + ".part")
        tmp.write_bytes(data)
        tmp.replace(dest)
    except Exception as e:  # noqa: BLE001
        log.warning("char ref #%s skipped: could not fetch %s/%s: %s", aid, bucket, key, e)
        return None
    return str(dest)


async def _resolve_character_refs(
    scenes: list[dict],
) -> tuple[list[str], list[int]]:
    """Scan every scene's `subject` for '#<id>' tags. For each unique id,
    fetch the producer_assets row and return an absolute local file path for
    it (see _local_character_ref). Returns (paths, character_ids_used)."""
    ids: list[int] = []
    seen: set[int] = set()
    for s in scenes:
        subj = (s.get("subject") or "")
        for m in _CHAR_REF_RE.finditer(subj):
            try:
                cid = int(m.group(1))
            except ValueError:
                continue
            if cid in seen:
                continue
            seen.add(cid)
            ids.append(cid)
    if not ids:
        return [], []

    rows: list[dict] = []
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT id, storage_bucket, storage_path, local_path, mime_type, slug
                  FROM producer_assets
                 WHERE id = ANY(%s::bigint[])
                   AND category = 'character'
                   AND enabled = TRUE
                """,
                (ids,),
            )
            rows = await cur.fetchall()

    paths: list[str] = []
    used_ids: list[int] = []
    for r in rows:
        path = await _local_character_ref(r)
        if path:
            paths.append(path)
            used_ids.append(int(r["id"]))
    if used_ids:
        log.info(
            "script: resolved %d character refs → %d ref paths",
            len(used_ids), len(paths),
        )
    return paths, used_ids


async def _bump_character_usage(character_ids: list[int]) -> None:
    if not character_ids:
        return
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                UPDATE producer_assets
                   SET usage_count = usage_count + 1,
                       last_used_at = NOW()
                 WHERE id = ANY(%s::bigint[])
                """,
                (character_ids,),
            )


def _sanitise_scenes(raw: Any) -> list[dict]:
    if not isinstance(raw, list):
        return []
    out: list[dict] = []
    allowed_durations = set(ALLOWED_SCENE_DURATIONS_SEC)
    for i, s in enumerate(raw, start=1):
        if not isinstance(s, dict):
            continue

        # Per-scene duration: read the LLM's value, clamp to {4,6,8}, fallback 8.
        raw_dur = s.get("duration_sec")
        try:
            dur_int = int(round(float(raw_dur))) if raw_dur is not None else 8
        except (TypeError, ValueError):
            dur_int = 8
        if dur_int not in allowed_durations:
            # Snap to the nearest legal value
            dur_int = min(allowed_durations, key=lambda v: abs(v - dur_int))

        # Lists may come back as None or strings — coerce defensively.
        palette = s.get("palette")
        if isinstance(palette, str):
            palette = [palette]
        elif not isinstance(palette, list):
            palette = []
        motifs = s.get("brand_motifs")
        if isinstance(motifs, str):
            motifs = [motifs]
        elif not isinstance(motifs, list):
            motifs = []
        out.append({
            "scene_index":     i,
            "duration_sec":    dur_int,
            "subject":         (s.get("subject") or "").strip(),
            "action":          (s.get("action") or "").strip(),
            "camera":          (s.get("camera") or "").strip(),
            "lighting":        (s.get("lighting") or "").strip(),
            "style":           (s.get("style") or "").strip(),
            "palette":         [str(p).strip() for p in palette if str(p).strip()],
            "typography":      (s.get("typography") or "").strip(),
            "brand_motifs":    [str(m).strip() for m in motifs if str(m).strip()],
            "cultural_anchor": (s.get("cultural_anchor") or "").strip(),
            # v6: exact spoken words per scene. Empty string is valid for
            # non-speech video_types (campus_tour, lifestyle_mood, etc.).
            # For talking_head / voiceover_narrative the upstream validator
            # already retried once if any scene came back empty.
            "dialogue":        (s.get("dialogue") or "").strip()[:2000],
            # Keep `prompt` field for backwards compatibility with older code paths
            # but it's no longer the source of truth.
            "prompt":          (s.get("prompt") or "").strip()[:4000],
        })
    return out[:MAX_SCENES_PER_SCRIPT]
