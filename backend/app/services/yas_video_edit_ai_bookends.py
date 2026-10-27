"""Yas Producer — Video Editor: AI intro/outro BOOKEND generation.

Reuses Talking Head v2's card→Veo pipeline for the multi-track editor, kept out
of the orchestrator (`yas_video_edit_ai.py`) so that file stays slim. Only
run-INDEPENDENT th_v2 functions are used (public aliases at the bottom of
`yas_talking_head_v2.py`): they take an explicit parent id and never read
talking-head run state, so the EDITOR project id is passed as `parent_run_id`.

Flow per role (intro / outro):

  derive_bookend_texts   — headline/subtitle/mood from the project title + the
                           first/last clip's analysis (transcript / scene
                           summary); deterministic fallback when analysis or
                           Gemini is unavailable.
  kickoff_bookend        — render the card (th_v2.render_card_animation_style,
                           42 logo baked in) → yas_generations card row
                           (category="video_edit_bookend_card", mode
                           talking_head_v2_intro/outro so the picker's suffix
                           rule tags it) → vision-picked motion style →
                           Veo i2v submit → yas_animations row
                           (metadata.role=intro|outro, parent = editor project).
                           The existing workers/yas_animations_poller.py
                           finishes the Veo render — no new wiring.
  find_ready_bookend     — newest ready bookend animation for reuse.
  build_placement_ops    — deterministic add_clip (+ tasteful fade
                           add_transition) ops the executor applies.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from ..config import config
from ..db import get_conn
from . import yas_talking_head_v2 as th_v2
from . import yas_video_editor as ve
from . import yas_video_edit_ai_ops as ops_mod
from .vertex_client import gemini_multimodal, veo_generate
from .yas_animation_prompts import ANIMATE_NEGATIVE_PROMPT
from .yas_talking_head_v2_prompts import build_intro_veo_prompt, build_outro_veo_prompt

log = logging.getLogger("archivist.yas_ve_ai_bookends")


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

FPS = ve.FPS  # 30

ROLES = ("intro", "outro")

# Veo supports 9:16 / 16:9 only — 1:1 editor projects render a 9:16 bookend
# (the clip's fit:"cover" crops it to the square canvas).
_VEO_ASPECTS = {"9:16", "16:9"}

# Fade join between a bookend and its neighbour (mirrors the compose system
# prompt's taste rules: 10-18f fades, never on clips shorter than 45 frames).
_JOIN_FADE_F = 12
_MIN_JOIN_CLIP_F = 45

# Texts model: cheap, schema-constrained, thinking disabled (mirrors the
# orchestrator's _PLANNER_MODEL choice).
_TEXTS_MODEL = "gemini-2.5-flash"

_TEXTS_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "headline": {"type": "string", "description": "Card headline, at most 7 words."},
        "subtitle": {"type": "string", "description": "Card subtitle, at most 10 words."},
        "mood": {"type": "string", "description": "One short mood phrase for the animation."},
    },
    "required": ["headline", "subtitle", "mood"],
}

_TEXTS_SYSTEM = (
    "You are a senior short-form video copywriter writing the text for a "
    "branded intro or outro title card. Punchy, concrete, on-topic. "
    "Headline at most 7 words; subtitle at most 10 words; no hashtags, no "
    "emoji, no quotes around the text. The mood is one short phrase (e.g. "
    "'confident, inviting') that an animator can act on."
)


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def _norm_role(role: str) -> str:
    r = (role or "").strip().lower()
    if r not in ROLES:
        raise ValueError(f"bookend role must be one of {ROLES}: {role!r}")
    return r


def _norm_veo_aspect(aspect: Any) -> str:
    return aspect if aspect in _VEO_ASPECTS else "9:16"


def _cap_words(text: Any, max_words: int) -> str:
    words = str(text or "").strip().split()
    return " ".join(words[:max_words])


def _fallback_texts(title: str, role: str) -> dict:
    """Deterministic texts when analysis/Gemini are unavailable."""
    if role == "outro":
        return {
            "headline": "Thanks for watching",
            "subtitle": "Follow for more",
            "mood": "decisive, inviting",
        }
    return {
        "headline": _cap_words(title, 7) or "Welcome",
        "subtitle": "",
        "mood": "confident, inviting",
    }


def _edge_clip_context(timeline: dict, analysis: dict, role: str) -> str:
    """Transcript excerpt + scene summary + mood of the FIRST (intro) or LAST
    (outro) video clip, from the analyze cache. Empty string when unavailable."""
    clips = [
        c for c in (timeline or {}).get("clips") or []
        if (c.get("kind") or "video") == "video"
    ]
    if not clips:
        return ""

    def _i(v: Any) -> int:
        try:
            return int(v or 0)
        except (TypeError, ValueError):
            return 0

    if role == "intro":
        clip = min(clips, key=lambda c: _i(c.get("from")))
    else:
        clip = max(clips, key=lambda c: _i(c.get("from")) + _i(c.get("durationInFrames")))

    kind, sid = ve._clip_source_kind_id(clip)
    if not kind or sid is None:
        return ""
    src = ((analysis or {}).get("sources") or {}).get(f"{kind}:{int(sid)}") or {}

    parts: list[str] = []
    segs = ((src.get("transcript") or {}).get("segments") or [])
    spoken = " ".join(str(s.get("text") or "").strip() for s in segs if s.get("text"))
    if spoken.strip():
        parts.append(f"Spoken: {spoken.strip()[:600]}")
    scenes = src.get("scenes") or []
    edge_scene = scenes[0] if role == "intro" else scenes[-1] if scenes else None
    if isinstance(edge_scene, dict) and (edge_scene.get("summary") or "").strip():
        parts.append(f"Scene: {str(edge_scene['summary']).strip()[:200]}")
    visual = src.get("visual") or {}
    if (visual.get("mood") or "").strip():
        parts.append(f"Mood: {str(visual['mood']).strip()[:80]}")
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# 1) derive_bookend_texts
# ---------------------------------------------------------------------------

async def derive_bookend_texts(
    timeline: dict, analysis: dict, role: str,
    topic_override: Optional[str] = None,
) -> dict:
    """{headline, subtitle, mood, topic} for one bookend card.

    Topic comes from the project title; the first-clip (intro) / last-clip
    (outro) transcript + scene summary ground one schema-constrained
    gemini-2.5-flash call. Analysis is OPTIONAL — and ANY failure degrades to
    the deterministic fallback (never raises).

    When `topic_override` is a non-empty string (user-supplied title), it is
    used VERBATIM-ish as the card text — no Gemini call."""
    role = _norm_role(role)
    ov = (topic_override or "").strip()
    if ov:
        return {
            "headline": _cap_words(ov, 10),
            "subtitle": "",
            "mood": ("confident, inviting" if role == "intro" else "decisive, inviting"),
            "topic": ov,
        }
    title = str(((timeline or {}).get("project") or {}).get("title") or "").strip() or "Untitled project"
    texts = _fallback_texts(title, role)
    texts["topic"] = title

    context = _edge_clip_context(timeline, analysis, role)
    try:
        ask = (
            f"Write the {role} title-card text for this short-form video.\n"
            f"Video title/topic: {title[:160]}\n"
            + (f"\nWhat the {'opening' if role == 'intro' else 'closing'} clip "
               f"shows/says:\n{context}\n" if context else "")
            + ("\nThe intro card hooks the viewer into the topic."
               if role == "intro"
               else "\nThe outro card closes the video with a short call to action "
                    "(e.g. thanks / follow / next step).")
        )
        result = await gemini_multimodal(
            ask,
            system=_TEXTS_SYSTEM,
            json_schema=_TEXTS_SCHEMA,
            model=_TEXTS_MODEL,
            temperature=0.6,
            max_tokens=300,
            thinking_budget=0,
        )
        if isinstance(result, dict) and "_raw" not in result:
            headline = _cap_words(result.get("headline"), 7)
            subtitle = _cap_words(result.get("subtitle"), 10)
            mood = str(result.get("mood") or "").strip()[:80]
            if headline:
                texts["headline"] = headline
                texts["subtitle"] = subtitle
            if mood:
                texts["mood"] = mood
    except Exception:  # noqa: BLE001 — texts degrade, the bookend still renders
        log.exception("[yas-ve-ai] %s bookend text derivation failed — using fallback", role)
    return texts


# ---------------------------------------------------------------------------
# 2) kickoff_bookend — card render → Veo submit (async; the yas_animations
#    poller downloads the MP4 and flips status='ready')
# ---------------------------------------------------------------------------

async def kickoff_bookend(project_id: int, role: str, texts: dict, aspect: str) -> dict:
    """Render the bookend card, insert its yas_generations row, and submit the
    Veo i2v render. Returns {"animation_id", "card_id", "duration_sec",
    "cost_usd"}. Raises on any failure (incl. VertexBudgetExceededError —
    already-inserted rows survive for reuse)."""
    role = _norm_role(role)
    aspect = _norm_veo_aspect(aspect)
    topic = str(texts.get("topic") or "").strip() or "Untitled project"
    headline = str(texts.get("headline") or "").strip() or topic
    subtitle = str(texts.get("subtitle") or "").strip()
    mood = str(texts.get("mood") or "").strip() or (
        "confident, inviting" if role == "intro" else "decisive, inviting"
    )

    # 1) Card render (42 logo baked in; new_vibe with no ref → voxel pipeline).
    png_bytes, mime, used_prompt = await th_v2.render_card_animation_style(
        role=role,
        topic=topic,
        headline=headline,
        subtitle=subtitle,
        ref_mode="new_vibe",
        ref_path=None,
        aspect=aspect,
    )

    # 2) Card row — parent is the EDITOR project; mode reuses the th_v2
    # intro/outro modes so list_sources' `_intro`/`_outro` suffix rule tags it
    # bookend_intro/bookend_outro; category marks it as editor-generated.
    card_id = await th_v2.insert_card_row(
        parent_run_id=project_id,
        topic=topic,
        image_model=th_v2.ENGINE_GEMINI_PRO,
        card_mode=th_v2.MODE_INTRO if role == "intro" else th_v2.MODE_OUTRO,
        image_bytes=png_bytes,
        mime=mime,
        used_prompt=used_prompt,
        card_type=role,
        category="video_edit_bookend_card",
    )
    card_path = await th_v2.resolve_card_path(card_id)
    if card_path is None:
        raise RuntimeError(f"{role} card #{card_id} has no resolvable image file")

    # 3) Motion style (vision pick; falls back internally) + Veo prompt.
    script: dict[str, Any] = {"mood": mood}
    if role == "outro":
        script["outro_headline"] = headline  # the prompt's CTA matches the card
    style = await th_v2.pick_bookend_animation(
        card_path=card_path, script=script, topic=topic, role=role,
    )
    builder = build_intro_veo_prompt if role == "intro" else build_outro_veo_prompt
    prompt = builder(topic=topic, script=script, style=style, aspect=aspect)

    # 4) Veo i2v submit (full quality, async — poller finishes it).
    op = await veo_generate(
        prompt,
        model=config.veo_model,
        duration_sec=th_v2.VEO_DURATION_BOOKEND,
        aspect_ratio=aspect,
        with_audio=False,
        start_image=card_path,
        reference_images=None,
        negative_prompt=ANIMATE_NEGATIVE_PROMPT,
    )

    # 5) Animation row — status='generating'; metadata.role drives reuse +
    # the picker's bookend categorization.
    animation_id = await th_v2.insert_animation_row(
        parent_run_id=project_id,
        prompt_used=prompt,
        veo_op_name=op.get("operation_name") or op.get("name") or "",
        veo_model=op.get("model") or config.veo_model,
        duration_sec=th_v2.VEO_DURATION_BOOKEND,
        with_audio=False,
        cost_usd=op.get("cost_usd"),
        role=role,
        aspect_ratio=aspect,
    )
    log.info(
        "[yas-ve-ai] project #%d: %s bookend kicked (card #%d, anim #%d, style=%s, $%.4f)",
        project_id, role, card_id, animation_id, style, float(op.get("cost_usd") or 0),
    )
    return {
        "animation_id": animation_id,
        "card_id": card_id,
        "duration_sec": th_v2.VEO_DURATION_BOOKEND,
        "cost_usd": op.get("cost_usd"),
    }


# ---------------------------------------------------------------------------
# 3) find_ready_bookend — newest ready bookend for reuse
# ---------------------------------------------------------------------------

async def find_ready_bookend(project_id: int, role: str) -> Optional[dict]:
    """{"animation_id", "duration_sec"} of the newest READY bookend animation
    of this role under the editor project, or None."""
    role = _norm_role(role)
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT id, duration_sec
                  FROM yas_animations
                 WHERE yas_generation_id = %s
                   AND status = 'ready'
                   AND metadata->>'role' = %s
                 ORDER BY created_at DESC
                 LIMIT 1
                """,
                (project_id, role),
            )
            row = await cur.fetchone()
    if not row:
        return None
    return {
        "animation_id": int(row["id"]),
        "duration_sec": float(row.get("duration_sec") or th_v2.VEO_DURATION_BOOKEND),
    }


# ---------------------------------------------------------------------------
# 4) build_placement_ops — deterministic add_clip (+ fade) ops
# ---------------------------------------------------------------------------

def build_placement_ops(plan_id: str, bookends: list[dict], timeline: dict) -> list[dict]:
    """The add_clip (+ optional add_transition) ops placing the rendered
    bookends. `bookends` items are {"role", "animation_id", "duration_sec"};
    intro is placed first so the outro's end-of-timeline math sees the shifted
    document. Fade joins only when BOTH sides are >= 45 frames; the outro fade
    targets the outro clip's own deterministic id (clip_ai_{plan}_{index}).

    Clips left over from a prior run of the SAME plan are ignored when picking
    the join neighbours (apply_plan removes them before executing)."""
    prefix = f"clip_ai_{ops_mod._safe_plan_id(plan_id)}_"
    tracks = (timeline or {}).get("tracks") or []
    clips = [
        c for c in (timeline or {}).get("clips") or []
        if not str(c.get("id", "")).startswith(prefix)
    ]

    # The primary user lane — same pick as the executor's _op_add_clip.
    user_video_tracks = [
        t for t in tracks
        if t.get("kind") == "video" and t.get("id") != ops_mod.AI_VIDEO_TRACK_ID
    ]
    primary = (
        min(user_video_tracks, key=lambda t: int(t.get("index", 0) or 0))
        if user_video_tracks else None
    )

    def _i(v: Any) -> int:
        try:
            return int(v or 0)
        except (TypeError, ValueError):
            return 0

    lane = sorted(
        (c for c in clips if primary and c.get("trackId") == primary.get("id")),
        key=lambda c: _i(c.get("from")),
    )

    order = {"intro": 0, "outro": 1}
    items = sorted(
        (b for b in bookends or [] if (b or {}).get("role") in order),
        key=lambda b: order[b["role"]],
    )

    ops: list[dict] = []
    for b in items:
        role = b["role"]
        dur_f = ve._frames(b.get("duration_sec") or th_v2.VEO_DURATION_BOOKEND)
        add_index = len(ops)
        ops.append({
            "op": "add_clip",
            "role": role,
            "source_kind": "animation",
            "source_id": int(b["animation_id"]),
            "durationInFrames": dur_f,
        })
        if dur_f < _MIN_JOIN_CLIP_F or not lane:
            continue
        if role == "intro":
            # Fade lives on the LATER clip = the first user clip on the lane.
            neighbour = lane[0]
        else:
            # Fade lives on the LATER clip = the outro clip itself; the
            # earlier side is the last user clip on the lane.
            neighbour = lane[-1]
        if _i(neighbour.get("durationInFrames")) < _MIN_JOIN_CLIP_F:
            continue
        later_id = (
            str(neighbour.get("id")) if role == "intro" else f"{prefix}{add_index}"
        )
        if not later_id:
            continue
        ops.append({
            "op": "add_transition",
            "clip_id": later_id,
            "preset": "fade",
            "durationInFrames": _JOIN_FADE_F,
            "timing": "linear",
        })
    return ops
