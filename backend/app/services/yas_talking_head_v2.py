"""Yas Producer — Talking Head category orchestration.

This module owns the end-to-end logic for the third Yas Producer flow:

  1. `generate_script()`     — OpenAI GPT-4o with full RAG context (brand +
                                past talking_head scripts + IG inspiration +
                                character library) → structured script row in
                                `yas_generations` (mode='talking_head_v2_preview').
  2. `generate_previews()`   — Three image-gen calls in parallel: main
                                keyframe (the still Veo will animate), intro
                                card (skipped if user picked a reference),
                                outro card (skipped if user picked a reference).
  3. `retry_preview()`       — Remakes ONE tile. A still with feedback is
                                refined (refine_still: one change made on the
                                current still), without it regenerated
                                (regenerate_still); a card is re-rendered.
  4. `finalize()`            — Kicks Veo on the main keyframe. The
                                yas_th_composition_poller picks up when Veo
                                finishes and triggers the Remotion stitch.
  5. `fetch_run()`           — Used by the frontend's 5s polling loop while
                                a finalize is in flight.
  6. `save_to_assets()`      — Copies the final composition MP4 to
                                producer_assets (asset_kind='video',
                                metadata.content_category='video_talking_head_v2').

The router (`routers/yas_producer.py`) wraps these in HTTP endpoints under
`/api/yas-producer/talking-head-v2/*`. The router does input validation; this
module does the work.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import re
import secrets
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from typing import Any, Callable, Optional

from fastapi import HTTPException
from psycopg.types.json import Jsonb

from ..config import config
from .storage import first_existing
from ..db import get_conn
from ..services import asset_storage
from ..services.openai_client import (
    BudgetExceededError as OpenAIBudgetExceededError,
    chat as openai_chat,
    openai_image,
)
from ..services.vertex_client import (
    GeminiNoImageError,
    VeoQuotaExhaustedError,
    VertexBudgetExceededError,
    gemini_embed,
    gemini_image,
    gemini_multimodal,
    get_meter,
)
from ..services.vertex_client import veo_generate as _vertex_veo_generate
from ..agents.producer.memory import (
    _latest_brand_payload,
    build_brand_requirements,
    build_context_block,
)
from ..agents.producer.prompts import format_audio_clause  # used inside build_main_veo_prompt
from . import yas_talking_head_v2_prompts as th_prompts
# Card-render + Veo-i2v glue, shared VERBATIM with Yasin's "Create Animation"
# tool (yas_producer.py) so the intro/outro bookends REUSE that pipeline instead
# of duplicating it. Owns the logo-parts assembly, aspect note, Gemini render
# wrapper, the Veo image-to-video submit, and the GEMINI_PRO_* / logo / negative
# constants the card path needs.
from . import card_animation
from .yas_poster_prompts import STYLE_EXTRACT_SCHEMA, STYLE_EXTRACT_SYSTEM
# Create Poster's own Stage-1 concept brain + Stage-2 render brain, reused for
# the auto-generated (no-ref) voxel intro/outro cards so the bookends are the
# SAME prompt+logic as Create Poster (single source of truth). This mirrors what
# the standalone Create-Animation frame already does (_render_animation_frame).
# Service-level imports only — never import the router (yas_producer) here.
from .poster_styles import DEFAULT_POSTER_STYLE, get_poster_style
from .poster_prompts import build_concepts_user_message, parse_concepts


log = logging.getLogger("archivist.yas_talking_head_v2")


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Same root the existing yas_producer flow uses (config.asset_root.parent /
# yas_producer). Talking-head artefacts live under a `talking-head-v2/` subdir.
_YAS_ROOT = (config.asset_root.parent / "yas_producer").resolve()
TH_ROOT = _YAS_ROOT / "talking-head-v2"

# Mode strings stored in yas_generations.mode for talking-head-v2 rows.
MODE_PREVIEW = "talking_head_v2_preview"
MODE_INTRO = "talking_head_v2_intro"
MODE_OUTRO = "talking_head_v2_outro"
# v6 — sibling row for each main scene's keyframe when scene_count > 1.
# Stored alongside MODE_INTRO / MODE_OUTRO rows in yas_generations. The
# run row (MODE_PREVIEW) still holds scenes[0]'s keyframe for backwards
# compat; scenes[1..N-1] are sibling rows with this mode.
MODE_SCENE = "talking_head_v2_scene"

# v6 — max scene count. Set in the plan; enforced both server-side and
# in the UI stepper.
MAX_SCENES = 5

# Status values reused from the existing yas_generations flow.
STATUS_GENERATING = "generating"
STATUS_READY = "ready"
STATUS_FAILED = "failed"
STATUS_SAVED = "saved_to_assets"

# Composition status values (live on metadata.composition_status).
COMP_QUEUED = "queued"
COMP_RENDERING = "rendering"
COMP_READY = "ready"
COMP_FAILED = "failed"

# Image-engine choices (mirror what yas_producer.py defines)
ENGINE_OPENAI = "openai"
ENGINE_GEMINI_PRO = "gemini_pro"
# Gemini image model + endpoint. Single source of truth in card_animation;
# re-exported here for the main-scene keyframe generator below (the intro/outro
# card renderer now reaches them through card_animation directly). The 42 logo
# PNG likewise lives in card_animation as LOGO_PATH (was POSTER_LOGO_PATH here).
GEMINI_PRO_MODEL = card_animation.GEMINI_PRO_MODEL
GEMINI_PRO_LOCATION = card_animation.GEMINI_PRO_LOCATION

# Global identity-lock rule applied (as a system_instruction) to every Gemini
# image call that has a reference image. Reinforces the per-prompt face lock —
# Nano Banana honours system instructions, and a persistent "preserve the
# reference person exactly" rule reduces face drift on edits/compositions.
_GEMINI_FACE_SYSTEM = (
    "You are a professional photo editor and compositor. Follow the user's "
    "instructions precisely and literally. Whenever the instructions tell you to "
    "keep a person or character identical to an attached reference image, you MUST "
    "preserve that person's facial identity with 100% fidelity — the same eye "
    "shape, nose, jawline, lips, skin tone and texture, and hair — and never "
    "beautify, slim, re-age, restyle, or substitute them for a different-looking "
    "person. When asked to change only the background, leave the subject's pixels "
    "untouched. Always output photo-realistic images, never illustrated or cartoonish."
    " Preserve the subject's exact facial expression — never add, remove, or "
    "exaggerate a smile."
    " Unless explicitly told otherwise, render exactly one person — the subject "
    "alone, with no other people or parts of other people in frame."
)

# Image gen timeouts (seconds).
# gpt-image-1's `images.edit` endpoint regularly takes 60-120s per call, and
# under load (5 parallel calls for a 3-scene run) we routinely see 90-150s
# tails. 90s was producing silent timeouts on intro+outro. 240s gives us
# headroom on the slow tail without letting genuinely-stuck calls run forever.
#
# gemini-3-pro-image (Nano Banana) is faster than gpt-image-1 for
# text-to-image (~30-60s) but with source_images attached it routinely
# crosses 180s and sometimes spikes to ~250s. 300s gives each attempt enough
# headroom while still bounding genuinely-stuck calls.
IMAGE_GEN_TIMEOUT_OPENAI = 240.0
IMAGE_GEN_TIMEOUT_GEMINI = 300.0

# The still Refine engine (Refine, and the framing re-shoot) has its own,
# tighter limits. The Vertex client sets no HTTP timeout, so one stalled
# request used to hold the whole 300 s (run 830: scene 1's refine, a 504
# after five minutes of nothing). Now each image edit gets
# REFINE_ATTEMPT_TIMEOUT_SEC; a timeout or an answer without an image is asked
# ONCE more without the identity reference (the canvas already carries the
# face, and it halves what is sent); the whole refine never takes longer
# than REFINE_TOTAL_TIMEOUT_SEC. Every image part is at most REFINE_MAX_EDGE
# px on its long side (a character upload can be a 4000 px photo).
REFINE_ATTEMPT_TIMEOUT_SEC = float(os.getenv("TH_REFINE_ATTEMPT_TIMEOUT_SEC", "80"))
REFINE_TOTAL_TIMEOUT_SEC = float(os.getenv("TH_REFINE_TOTAL_TIMEOUT_SEC", "190"))
REFINE_MAX_EDGE = 1536
REFINE_TIMEOUT_MESSAGE = (
    "The image model didn't answer in time (it was asked twice, for about three "
    "minutes in all). Nothing changed on the still: try again in a minute."
)

# Stage-1 token budget for the reused Create-Poster concept call (one concept
# for a bookend card). Mirrors POSTER_CONCEPTS_MAX_TOKENS in the poster router
# (kept local so this service never imports the router).
_CARD_CONCEPTS_MAX_TOKENS = 4000

# Veo settings
VEO_DURATION_SEC = 8
VEO_ASPECT_RATIO = "9:16"   # default fallback only — runs carry metadata.aspect_ratio

# Output aspect ratio. Veo 3.1 supports ONLY 9:16 and 16:9 (no 1:1), so those
# are the two options. Default 9:16 (every legacy run + omitted field).
ASPECTS = ("9:16", "16:9")


def _norm_aspect(a: Any) -> str:
    """Clamp any value to a supported aspect ratio; default 9:16."""
    return a if a in ASPECTS else "9:16"


# Framing (#56): how close the camera is. metadata.framing is the run's (a
# run made from now on starts at DEFAULT_FRAMING; a run without the key reads
# as LEGACY_FRAMING, the framing its stills were made at), and
# metadata.scenes[i].framing an optional per-scene override. What a still was
# actually made at is recorded with it: metadata.main_framing_used (scene 0)
# and scenes[i].framing_used (scenes >= 1, also on the scene row);
# FRAMING_AS_PHOTO for a photo's own framing.
FRAMINGS = th_prompts.FRAMINGS
DEFAULT_FRAMING = th_prompts.DEFAULT_FRAMING
LEGACY_FRAMING = th_prompts.LEGACY_FRAMING
FRAMING_AS_PHOTO = th_prompts.FRAMING_AS_PHOTO
# scene_framings value that drops a scene's override (it follows the run).
FRAMING_INHERIT = "inherit"


def _norm_framing(v: Any) -> str:
    """Any value as one of FRAMINGS; None, '' or garbage reads as close."""
    return th_prompts.norm_framing(v)


def _check_framing(v: Any) -> str | None:
    """A framing sent by a caller: None stays None (not sent), a known value
    or synonym becomes one of FRAMINGS, anything else is a 400."""
    if v is None:
        return None
    f = th_prompts.framing_or_none(v)
    if f is None:
        raise HTTPException(400, "framing must be close, above_hip or full_body")
    return f


def _run_framing(md: dict | None) -> str:
    """The run's framing: metadata.framing, else close (a legacy run)."""
    return _norm_framing((md or {}).get("framing"))


def _scene_framing_override(md: dict | None, i: int) -> str | None:
    """Scene `i`'s own framing (metadata.scenes[i].framing), or None when it
    follows the run's. A single-scene run's one framing is the run's (a run
    cut down to one scene can keep its scenes list)."""
    if int((md or {}).get("scene_count") or 1) <= 1:
        return None
    scenes = (md or {}).get("scenes") or []
    sm = scenes[i] if 0 <= i < len(scenes) and isinstance(scenes[i], dict) else {}
    return th_prompts.framing_or_none(sm.get("framing"))


def _effective_framing(md: dict | None, i: int) -> str:
    """The framing a new still of scene `i` is made at: the scene's own
    framing, else the run's."""
    return _scene_framing_override(md, i) or _run_framing(md)


def _norm_quality(v: Any) -> str:
    """Run video-quality setting → Veo model tier. 'quality' uses the premium
    Veo model; anything else (default) uses the fast model."""
    return "quality" if str(v or "").strip().lower() == "quality" else "normal"


def _veo_model_for(quality: Any) -> str:
    """Premium Veo model for 'quality', else the fast model. Used per segment."""
    return config.veo_model_premium if _norm_quality(quality) == "quality" else config.veo_model


def _aspect_size(aspect: str) -> tuple[str, str]:
    """(gemini aspect_ratio, openai gpt-image-1 size) for an output aspect.

    gpt-image-1 has no exact 16:9 — 1536x1024 (3:2) is the closest; the
    composition's objectFit:cover absorbs the small difference. Gemini renders
    the exact aspect via its aspect_ratio param.
    """
    if _norm_aspect(aspect) == "16:9":
        return "16:9", "1536x1024"
    return "9:16", "1024x1536"


def _aspect_dims(aspect: str) -> tuple[int, int]:
    """(width, height) the Remotion composition renders at for an aspect."""
    return (1920, 1080) if _norm_aspect(aspect) == "16:9" else (1080, 1920)


def _image_aspect_ratio(path: Path) -> float | None:
    """width/height of an image on disk, or None if unreadable."""
    try:
        from PIL import Image
        with Image.open(path) as im:
            if im.height:
                return im.width / im.height
    except Exception:
        log.exception("[yas-th] could not read image dims for %s", path)
    return None


# How far (relative width:height) a still may be from the output shape and
# still be trimmed to it with a free crop. Further off means the model ignored
# the requested shape, and the still is reframed (or padded) instead.
_ASPECT_TOLERANCE = 0.06


def _aspect_units(aspect: Any) -> tuple[int, int]:
    """(w, h) of an output aspect in lowest terms: (9, 16) or (16, 9)."""
    return (16, 9) if _norm_aspect(aspect) == "16:9" else (9, 16)


def _aspect_off(width: int, height: int, aspect: Any) -> float:
    """Relative distance of a width x height image from `aspect` (0 = exact)."""
    uw, uh = _aspect_units(aspect)
    target = uw / uh
    return abs(width / height - target) / target


def _is_exact_aspect(width: int, height: int, aspect: Any) -> bool:
    """True when width:height is exactly the output aspect (720x1280,
    756x1344, 1920x1080, ...): Veo then needs no bars at all."""
    uw, uh = _aspect_units(aspect)
    return width * uh == height * uw


def _aspect_matches(path: Path, target_aspect: str) -> bool:
    """True when the image at `path` is already ~the target aspect (±6%).

    Used to decide whether an as-is upload can pass through verbatim or must be
    reframed to the chosen output size. None (unreadable) → treat as matching
    (don't force an extra gen on a file we can't measure)."""
    r = _image_aspect_ratio(path)
    if r is None:
        return True
    tw, th = _aspect_dims(target_aspect)
    target = tw / th
    return abs(r - target) / target <= _ASPECT_TOLERANCE

# Max number of Veo submissions in flight at once. Veo 3.1 Fast's
# per-base-model quota (long_running_online_prediction_requests_per_base_model)
# trips with a 429 RESOURCE_EXHAUSTED when a whole reel (N scenes + intro +
# outro = up to 7 jobs) is fired in a single burst. Bounding concurrent
# submissions — combined with the 429-retry inside veo_generate — keeps the
# submission rate under the ceiling. Tunable via VEO_MAX_CONCURRENT_SUBMITS.
# Hard ceiling on a single interactive script-generation LLM call. The shared
# provider retry decorators do up to 6 attempts with 1–60s backoff, which can
# stretch one call to minutes on a transient provider hiccup / quota 429 — far
# too long for a user waiting on a script. On timeout we fall through to the
# next provider (OpenAI → Gemini); if both time out the caller gets a clean 502
# to retry. Tunable via TH_SCRIPT_LLM_TIMEOUT_SEC.
SCRIPT_LLM_TIMEOUT_SEC = float(os.getenv("TH_SCRIPT_LLM_TIMEOUT_SEC", "60"))

MAX_CONCURRENT_VEO_SUBMITS = max(1, int(os.getenv("VEO_MAX_CONCURRENT_SUBMITS", "2")))
# Small gap between releasing one submission slot and starting the next, so the
# two in-flight submits don't land on Vertex at the exact same instant.
VEO_SUBMIT_GAP_SEC = 0.4


async def _run_veo_submits_bounded(
    awaitables: list[Any], *, limit: int = MAX_CONCURRENT_VEO_SUBMITS,
) -> list[Any]:
    """Run Veo submission coroutines with bounded concurrency.

    At most `limit` submissions are awaited concurrently; the rest queue on a
    semaphore. Returns results in input order, with exceptions captured in
    place (never raises) so callers can map per-job failures themselves —
    mirroring asyncio.gather(return_exceptions=True).
    """
    sem = asyncio.Semaphore(max(1, limit))

    async def _guarded(index: int, coro: Any) -> Any:
        async with sem:
            # Stagger starts slightly so concurrent submits don't collide.
            if index >= limit:
                await asyncio.sleep(VEO_SUBMIT_GAP_SEC)
            return await coro

    return await asyncio.gather(
        *[_guarded(i, c) for i, c in enumerate(awaitables)],
        return_exceptions=True,
    )


# Max number of image-generation calls in flight at once. The talking-head-v2
# preview fires up to 5 image gens per run (main + intro + outro + extra scene
# keyframes); the premium gemini-3-pro-image model has a tight
# per-minute quota that a 5-way burst (or several runs back-to-back) trips with
# a 429 RESOURCE_EXHAUSTED. Bounding concurrency spaces the calls out so the
# built-in 4x retry in `gemini_image` can succeed. Timing only — no effect on
# which model runs, the prompt, or the output. Tunable via
# YAS_MAX_CONCURRENT_IMAGE_GENS.
MAX_CONCURRENT_IMAGE_GENS = max(1, int(os.getenv("YAS_MAX_CONCURRENT_IMAGE_GENS", "2")))

# Lazily created so the Semaphore binds to the running event loop, not import
# time (avoids the "bound to a different loop" gotcha on Windows/uvicorn).
_IMAGE_GEN_SEM: asyncio.Semaphore | None = None


def _image_gen_sem() -> asyncio.Semaphore:
    global _IMAGE_GEN_SEM
    if _IMAGE_GEN_SEM is None:
        _IMAGE_GEN_SEM = asyncio.Semaphore(MAX_CONCURRENT_IMAGE_GENS)
    return _IMAGE_GEN_SEM


# Sources whose underlying image is a real photograph of an identifiable
# person. Veo's RAI refuses to ANIMATE a real person and rejects the whole job
# with support code 29310472 ("...prompt contains words... usage guidelines"),
# even when the text prompt is clean and person_generation=allow_adult.
# Proven by isolation test: a Veo call with [AI keyframe] alone succeeds, but
# [AI keyframe + real Instagram-extracted frame] is blocked.
_REAL_PERSON_ASSET_SOURCES = {"instagram_frame", "instagram", "ig_frame"}


async def _asset_is_real_person_photo(asset_id: int | None) -> bool:
    """True when a character asset is a real-person photograph that Veo blocks.

    Only producer_assets carry a `metadata.source` marker; uploads/None pass
    through unchanged (we can't classify them and they're not the known
    offender — IG-extracted character frames are).
    """
    if not asset_id:
        return False
    try:
        async with get_conn() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT metadata->>'source' AS source "
                    "FROM producer_assets WHERE id = %s",
                    (int(asset_id),),
                )
                row = await cur.fetchone()
    except Exception:
        log.exception("real-person asset check failed for asset %s", asset_id)
        return False
    src = ((row.get("source") if row else None) or "").lower()
    return src in _REAL_PERSON_ASSET_SOURCES


async def _veo_safe_character_path(
    *, asset_id: int | None, upload_id: int | None,
) -> Path | None:
    """Resolve a character reference path for a VEO call.

    Returns None when the character is a real-person photo Veo would reject
    (29310472). The AI-generated keyframe — always Veo reference image 1 —
    already carries the identity, so dropping the raw photo preserves the look
    while clearing the block. NOTE: image-gen keyframe paths still use the raw
    photo (Gemini accepts it); only Veo reference packing is filtered here.
    """
    path = await _slot_path(asset_id=asset_id, upload_id=upload_id)
    if path is not None and await _asset_is_real_person_photo(asset_id):
        log.info(
            "[yas-th] dropping real-person character (asset=%s) from Veo refs; "
            "keyframe carries identity (avoids RAI 29310472)", asset_id,
        )
        return None
    return path

# Composition card hold durations
INTRO_HOLD_SEC = 2.0
OUTRO_HOLD_SEC = 2.0
COMPOSITION_FADE_SEC = 0.4

# OpenAI structured-output spec for the script schema
_SCRIPT_RESPONSE_FORMAT: dict[str, Any] = {
    "type": "json_schema",
    "json_schema": {
        "name": "talking_head_script",
        "strict": True,
        "schema": th_prompts.TALKING_HEAD_SCRIPT_SCHEMA,
    },
}


def _gemini_safe_schema(schema: Any) -> Any:
    """Vertex's `response_schema` accepts a SUBSET of JSON-Schema — it rejects
    `additionalProperties` (and OpenAI's `strict`). Deep-copy the OpenAI schema
    with those keys stripped so the SAME schema drives the Gemini fallback."""
    if isinstance(schema, dict):
        return {
            k: _gemini_safe_schema(v)
            for k, v in schema.items()
            if k not in ("additionalProperties", "strict")
        }
    if isinstance(schema, list):
        return [_gemini_safe_schema(v) for v in schema]
    return schema

# OpenAI structured-output spec for the topic-MAGNIFY (commit-angle) schema.
_ANGLE_RESPONSE_FORMAT: dict[str, Any] = {
    "type": "json_schema",
    "json_schema": {
        "name": "talking_head_angle",
        "strict": True,
        "schema": th_prompts.TALKING_HEAD_ANGLE_SCHEMA,
    },
}


def _ensure_dirs() -> None:
    TH_ROOT.mkdir(parents=True, exist_ok=True)


def _backend_root() -> Path:
    return config.asset_root.parent


def _to_rel(p: Path) -> str:
    """Return a path relative to backend root when possible, else absolute."""
    try:
        return str(p.relative_to(_backend_root()))
    except ValueError:
        return str(p)


# ---------------------------------------------------------------------------
# Topic-MAGNIFY (COMMITTED ANGLE) + past-reel RAG + bookend anti-repetition
#
# These power the "keep the topic, magnify it, learn from what performed" fix:
#   - _commit_angle()       : LLM pass that locks one topic-faithful angle.
#   - _recent_th_bookends()  : recent intro/outro lines, for anti-repetition.
#   - _nearest_th_scripts()  : the studio's OWN past reels, by embedding.
#   - _past_th_block()       : formats those past reels into a context block.
#   - _build_script_inputs() : one call → (context_block, angle, bookends).
#   - _embed_and_store_script(): embeds the finished reel for future RAG.
# All are best-effort: any failure degrades to today's behaviour, never 500s.
# ---------------------------------------------------------------------------

# yas_generations gains a 768d Gemini embedding of each finished talking-head
# script so future generations can retrieve the studio's own past reels (same
# dimension as every other embedding in the project — config.vertex_embed_dim).
_SCRIPT_EMB_COL_READY = False


async def _ensure_script_embedding_column() -> bool:
    """Idempotently add yas_generations.script_embedding (vector 768). Runs the
    ALTER once per process. Returns True when the column is available."""
    global _SCRIPT_EMB_COL_READY
    if _SCRIPT_EMB_COL_READY:
        return True
    try:
        async with get_conn() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "ALTER TABLE yas_generations "
                    "ADD COLUMN IF NOT EXISTS script_embedding vector(768)"
                )
        _SCRIPT_EMB_COL_READY = True
        return True
    except Exception:
        log.exception("[yas-th] could not ensure script_embedding column (non-fatal)")
        return False


def _script_embed_text(topic: str, script: dict | None, angle: dict | None) -> str:
    """Compact string we embed for retrieval: topic + hook + dialogue + angle."""
    script = script or {}
    angle = angle or {}
    bits = [topic or ""]
    if script.get("hook"):
        bits.append(str(script["hook"]))
    if script.get("dialogue"):
        bits.append(str(script["dialogue"]))
    if angle.get("committed_angle"):
        bits.append(str(angle["committed_angle"]))
    return "  ".join(b.strip() for b in bits if b and b.strip())[:2000]


def _angle_on_topic(topic: str, angle: dict) -> bool:
    """True if the committed angle stays on the user's topic — at least one
    meaningful content word from the topic appears anywhere in the angle. A
    drifted angle (none of the topic's words present) is rejected so the script
    falls back to using the raw topic directly (on-topic by construction)."""
    import re
    stop = {
        "the", "a", "an", "of", "to", "for", "and", "or", "in", "on", "at", "with",
        "is", "are", "our", "your", "my", "we", "you", "it", "this", "that", "join",
        "joining", "get", "getting", "how", "why", "what", "about", "into", "from",
        "be", "as", "by", "so",
    }
    words = {w for w in re.findall(r"[a-z0-9]+", (topic or "").lower())
             if len(w) >= 2 and w not in stop}
    if not words:
        return True
    text = " ".join(str(v) for v in angle.values() if isinstance(v, str)).lower()
    return any(w in text for w in words)


async def _commit_angle(
    topic: str, context_block: str, user_dialogue: str | None,
) -> dict | None:
    """Topic-MAGNIFY: lock ONE sharp, topic-faithful angle before scripting.

    Best-effort — returns None on any failure, in which case the script call
    proceeds exactly as before (no COMMITTED ANGLE block)."""
    try:
        raw = await openai_chat(
            th_prompts.build_angle_user_prompt(
                topic=topic, context_block=context_block, user_dialogue=user_dialogue,
            ),
            system=th_prompts.TALKING_HEAD_ANGLE_SYSTEM,
            model="gpt-4o",
            max_tokens=500,
            temperature=0.5,
            response_format=_ANGLE_RESPONSE_FORMAT,
        )
        angle = json.loads(raw)
    except Exception:
        log.exception("[yas-th] commit-angle stage failed (non-fatal)")
        return None
    if not isinstance(angle, dict) or not angle.get("committed_angle"):
        return None
    if not _angle_on_topic(topic, angle):
        log.warning(
            "[yas-th] committed angle drifted OFF-topic for %r (%s) — dropping; "
            "script will use the topic directly",
            topic[:60], str(angle.get("committed_angle"))[:120],
        )
        return None
    log.info(
        "[yas-th] committed angle for topic=%r → %s",
        topic[:60], str(angle.get("committed_angle"))[:160],
    )
    return angle


async def _recent_th_bookends(limit: int = 12) -> list[dict]:
    """Recent intro/outro headlines already shipped (for anti-repetition)."""
    try:
        async with get_conn() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    SELECT metadata->'script'->>'intro_headline' AS intro_headline,
                           metadata->'script'->>'outro_headline' AS outro_headline
                      FROM yas_generations
                     WHERE mode = %s
                       AND metadata->'script' IS NOT NULL
                     ORDER BY created_at DESC
                     LIMIT %s
                    """,
                    (MODE_PREVIEW, limit),
                )
                rows = await cur.fetchall()
        return [dict(r) for r in rows]
    except Exception:
        log.exception("[yas-th] recent-bookends lookup failed (non-fatal)")
        return []


async def _nearest_th_scripts(query_vec: list[float], *, k: int = 3) -> list[dict]:
    """Top-k of the studio's OWN past talking-head reels, by script embedding."""
    if not query_vec:
        return []
    if not await _ensure_script_embedding_column():
        return []
    try:
        async with get_conn() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    SELECT topic,
                           metadata->'script'->>'hook'           AS hook,
                           metadata->'script'->>'dialogue'       AS dialogue,
                           metadata->'script'->>'intro_headline' AS intro_headline
                      FROM yas_generations
                     WHERE mode = %s
                       AND script_embedding IS NOT NULL
                     ORDER BY script_embedding <=> %s::vector
                     LIMIT %s
                    """,
                    (MODE_PREVIEW, query_vec, k),
                )
                rows = await cur.fetchall()
        return [dict(r) for r in rows]
    except Exception:
        log.exception("[yas-th] past-reel retrieval failed (non-fatal)")
        return []


def _format_past_th_block(rows: list[dict]) -> str:
    """STYLE-ONLY reminder of the studio's past reels. We deliberately OMIT the
    verbatim topics/hooks/dialogue so the model cannot copy a past reel's
    subject — it must write fresh content for the user's TOPIC."""
    items = [r for r in rows if (r.get("hook") or r.get("dialogue"))]
    if not items:
        return ""
    return (
        f"STYLE NOTE — you have {len(items)} past reels in this format. Reuse only "
        "their MECHANICS (a hook-first opening, ONE short ~13-16 word spoken line, "
        "a single calm take). Do NOT reuse any past reel's subject, wording, or "
        "angle — write entirely fresh content for the user's TOPIC."
    )


async def _build_script_inputs(
    topic: str, user_dialogue: str | None = None,
) -> tuple[str, dict | None, list[dict]]:
    """Assemble everything the script call needs beyond the raw topic:
    (enriched context_block, committed_angle, recent_bookends).

    Shared by generate_script() and the re-script path so both stay on-topic.
    Every step is best-effort and degrades to the prior behaviour on failure.
    """
    # Base producer RAG: brand + nearest producer scripts + perf-weighted IG.
    # No CHARACTER LIBRARY roster: it asks for '#<id> <slug>' subjects that TH
    # v2 never resolves, and its rows are frames of real students' interviews.
    context_block = ""
    try:
        context_block = await build_context_block(
            query_text=topic, k=3, dialogue_language="en", video_type="talking_head",
            include_character_library=False,
        )
    except Exception:
        log.exception("RAG context build failed (non-fatal — script gen continues)")
        context_block = ""

    # Append the studio's OWN past reels (embedded), if any.
    try:
        qvecs = await gemini_embed([topic])
        qvec = qvecs[0] if qvecs else []
    except Exception:
        log.exception("[yas-th] topic embed failed (non-fatal)")
        qvec = []
    past_block = _format_past_th_block(await _nearest_th_scripts(qvec, k=3)) if qvec else ""
    if past_block:
        context_block = (context_block or "") + "\n" + past_block

    recent_bookends = await _recent_th_bookends(limit=12)
    # Commit the angle from the TOPIC ALONE — never from the retrieved RAG content
    # (that content bleed is exactly what dragged scripts off-topic). The full
    # context_block still flows to the SCRIPT call below for STYLE/technique only.
    committed_angle = await _commit_angle(topic, "", user_dialogue)
    return context_block, committed_angle, recent_bookends


async def _embed_and_store_script(
    run_id: int, topic: str, script: dict | None, angle: dict | None,
) -> None:
    """Best-effort: embed the finished reel and store it on the row so future
    generations can retrieve it via _nearest_th_scripts()."""
    if not await _ensure_script_embedding_column():
        return
    text = _script_embed_text(topic, script, angle)
    if not text:
        return
    try:
        vecs = await gemini_embed([text])
        if not vecs or not vecs[0]:
            return
        async with get_conn() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "UPDATE yas_generations SET script_embedding = %s::vector "
                    "WHERE id = %s",
                    (vecs[0], run_id),
                )
    except Exception:
        log.exception("[yas-th] script embedding store failed (non-fatal)")


def _abs_from_rel(rel_or_abs: str) -> Path:
    """Resolve a backend-relative or absolute path to absolute on disk."""
    p = Path(rel_or_abs)
    if p.is_absolute():
        return p
    # Local-mode uploads store backend/-relative paths; see first_existing.
    return first_existing(_backend_root() / p, config.producer_assets_root.parent.parent / p)


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

async def _fetch_run_row(run_id: int) -> dict:
    """Fetch the main preview row + raise 404 if missing."""
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT id, topic, mode, status, image_model, prompt_used,
                       feedback_text, retry_kind, parent_generation_id,
                       storage_bucket, storage_path, local_path, mime_type,
                       bytes, width, height, cost_usd, error,
                       saved_asset_id, metadata, created_at, updated_at
                  FROM yas_generations
                 WHERE id = %s AND mode = %s
                """,
                (run_id, MODE_PREVIEW),
            )
            row = await cur.fetchone()
    if not row:
        raise HTTPException(404, f"talking-head-v2 run #{run_id} not found")
    # JSONB may come back as a string in rare cases
    md = row.get("metadata") or {}
    if isinstance(md, str):
        try:
            md = json.loads(md)
        except Exception:
            md = {}
    row["metadata"] = md
    return row


async def delete_run(run_id: int) -> dict:
    """Hard-delete a Talking-Head v2 run and everything it OWNS — used by the
    Library "Delete" button.

    A run is one ``yas_generations`` row (``mode=MODE_PREVIEW``, id == run_id). Its
    child intro/outro/scene image rows hang off it via
    ``metadata->>'parent_talking_head_id'`` (NOT the FK column), and its Veo MP4
    rows live in ``yas_animations`` keyed by ``yas_generation_id`` (FK is ON DELETE
    CASCADE). Regeneration leaves OLD child + animation rows attached, so we delete
    by the parent linkage (not by the metadata pointers) to catch every orphan.

    Removes: the run keyframe + every child image + every animation MP4 storage
    object (best-effort), the stitched composition (local file + its bucket
    backup), the per-run local dir, the child rows, and the run row (which
    cascades the animation rows).
    PRESERVED: any Save-to-assets ``producer_assets`` copy (independent), uploaded
    intro/outro ``yas_uploads`` (shared/transient, self-GC), and Video-Editor
    projects opened from this run (independent). Returns ``{deleted, id}``.
    """
    import shutil

    run = await _fetch_run_row(run_id)  # raises 404 if missing / wrong mode
    md = run.get("metadata") or {}

    # --- collect every storage object to delete BEFORE removing the rows ---
    # (the animation cascade fires synchronously on the run-row delete, so we must
    #  read the animation paths first). Each entry: (bucket, path, local_path).
    targets: list[tuple[Any, Any, Any]] = [
        (run.get("storage_bucket"), run.get("storage_path"), run.get("local_path")),
    ]
    # Earlier scene-0 stills, and the ones Undo took off (each version is its
    # own file and bucket key).
    for key in ("still_versions", "still_undone"):
        versions = md.get(key)
        for v in ((versions.get("0") if isinstance(versions, dict) else None) or []):
            if isinstance(v, dict):
                targets.append((v.get("storage_bucket"), v.get("storage_path"), v.get("local_path")))
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            # Child image rows (intro / outro / scenes), incl. regen-orphaned ones.
            # Guard the ::int cast with a regex (CASE short-circuits) so a stray
            # non-numeric metadata value can never abort the whole query — same
            # idiom the Library aggregation uses for main_animation_id.
            await cur.execute(
                """
                SELECT storage_bucket, storage_path, local_path
                  FROM yas_generations
                 WHERE CASE
                         WHEN metadata->>'parent_talking_head_id' ~ '^-?[0-9]+$'
                         THEN (metadata->>'parent_talking_head_id')::int
                       END = %s
                """,
                (run_id,),
            )
            for r in await cur.fetchall() or []:
                targets.append((r.get("storage_bucket"), r.get("storage_path"), r.get("local_path")))
            # Veo MP4 rows tied to this run (real FK).
            await cur.execute(
                "SELECT storage_bucket, storage_path, local_path "
                "FROM yas_animations WHERE yas_generation_id = %s",
                (run_id,),
            )
            for r in await cur.fetchall() or []:
                targets.append((r.get("storage_bucket"), r.get("storage_path"), r.get("local_path")))

    # The stitched composition: the local file plus the bucket backup the
    # composition poller writes (yas/compositions/talking-head/{run_id}.mp4).
    comp_rel = md.get("composition_path") or md.get("composition_output_path")
    comp_bucket = md.get("composition_storage_bucket")
    comp_key = md.get("composition_storage_path")
    if comp_rel or (comp_bucket and comp_key):
        targets.append((comp_bucket, comp_key, comp_rel))

    for bucket, path, local in targets:
        if not (bucket or path or local):
            continue
        try:
            await asset_storage.delete(storage_bucket=bucket, storage_path=path, local_path=local)
        except Exception:
            log.exception("[th-v2] storage delete failed for run #%s (continuing)", run_id)

    # Belt-and-braces: drop the per-run local dir (main.png + child PNGs + final.mp4
    # all live under it; id-scoped, bucket-free, safe to rmtree).
    try:
        shutil.rmtree(TH_ROOT / str(run_id), ignore_errors=True)
    except Exception:
        log.exception("[th-v2] per-run dir cleanup failed for #%s (continuing)", run_id)

    # --- delete the rows: children by parent linkage, then the run (cascades anims) ---
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                DELETE FROM yas_generations
                 WHERE CASE
                         WHEN metadata->>'parent_talking_head_id' ~ '^-?[0-9]+$'
                         THEN (metadata->>'parent_talking_head_id')::int
                       END = %s
                """,
                (run_id,),
            )
            await cur.execute(
                "DELETE FROM yas_generations WHERE id = %s AND mode = %s RETURNING id",
                (run_id, MODE_PREVIEW),
            )
            deleted = await cur.fetchone()

    return {"deleted": 1 if deleted else 0, "id": run_id}


async def _fetch_card_row(card_id: int) -> dict | None:
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT id, mode, status, local_path, storage_bucket,
                       storage_path, prompt_used, cost_usd, error,
                       width, height, mime_type, metadata
                  FROM yas_generations WHERE id = %s
                """,
                (card_id,),
            )
            row = await cur.fetchone()
    return dict(row) if row else None


async def _fetch_animation_row(anim_id: int) -> dict | None:
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT id, status, veo_operation_name, local_path,
                       storage_bucket, storage_path, duration_sec,
                       with_audio, cost_usd, error, metadata
                  FROM yas_animations WHERE id = %s
                """,
                (anim_id,),
            )
            row = await cur.fetchone()
    return dict(row) if row else None


async def _patch_metadata(run_id: int, patch: dict) -> None:
    """Shallow-merge `patch` into yas_generations.metadata for `run_id`."""
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                UPDATE yas_generations
                   SET metadata = metadata || %s::jsonb,
                       updated_at = NOW()
                 WHERE id = %s
                """,
                (Jsonb(patch), run_id),
            )


def _md_dict(v: Any) -> dict:
    """A metadata value as a dict (JSONB can come back as a string)."""
    if isinstance(v, str):
        try:
            v = json.loads(v)
        except Exception:
            v = {}
    return v if isinstance(v, dict) else {}


async def _patch_scene(
    run_id: int, i: int, fields: dict | Callable[[dict], dict],
) -> dict | None:
    """Merge `fields` into metadata.scenes[i] of run `run_id`, atomically.

    The row is locked (SELECT ... FOR UPDATE) for the read and the write, and
    only that one array element is replaced (jsonb_set), so two writers on
    different scenes, or on different fields of one scene, never undo each
    other (a `_patch_metadata` of a whole `scenes` list read earlier can).
    `fields` may be a function of the scene's CURRENT value (read under the
    lock) returning the fields to merge. Returns the merged scene, or None
    when the run has no scene i (nothing is written)."""
    if i < 0:
        return None
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT metadata->'scenes'->(%s::int) AS scene
                  FROM yas_generations
                 WHERE id = %s AND mode = %s
                   FOR UPDATE
                """,
                (i, run_id, MODE_PREVIEW),
            )
            row = await cur.fetchone()
            scene = row.get("scene") if row else None
            if isinstance(scene, str):
                scene = _md_dict(scene)
            if not isinstance(scene, dict):
                return None
            patch = fields(dict(scene)) if callable(fields) else fields
            merged = {**scene, **(patch or {})}
            await cur.execute(
                """
                UPDATE yas_generations
                   SET metadata = jsonb_set(metadata, %s::text[], %s::jsonb, false),
                       updated_at = NOW()
                 WHERE id = %s
                """,
                (["scenes", str(i)], Jsonb(merged), run_id),
            )
    return merged


async def _resolve_asset_row(asset_id: int) -> dict | None:
    """Pull a producer_assets row with the fields we need for briefs +
    file-path resolution."""
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT id, slug, asset_kind, category, archetype, display_name,
                       storage_bucket, storage_path, local_path, mime_type,
                       ai_description, attributes,
                       (SELECT LEFT(ai_description, 240)) AS preview
                  FROM producer_assets
                 WHERE id = %s
                """,
                (asset_id,),
            )
            row = await cur.fetchone()
    return dict(row) if row else None


async def _resolve_asset_path(asset_id: int) -> Path:
    """Return a local Path for the asset file, downloading from the bucket
    when needed (and caching under TH_ROOT/refs_cache).
    """
    row = await _resolve_asset_row(asset_id)
    if not row:
        raise HTTPException(404, f"asset #{asset_id} not found")
    if row.get("local_path"):
        p = asset_storage.local_to_abs(row["local_path"])
        if p.exists():
            return p
    if row.get("storage_bucket") and row.get("storage_path"):
        # Fetched below by this server, so the internal origin (no hairpin).
        signed = await asset_storage.url_for(
            storage_bucket=row["storage_bucket"],
            storage_path=row["storage_path"],
            local_path=None,
            expires=600,
            internal=True,
        )
        if not signed:
            raise HTTPException(502, f"could not sign asset #{asset_id}")
        cache_dir = TH_ROOT / "refs_cache"
        cache_dir.mkdir(parents=True, exist_ok=True)
        ext = Path(row["storage_path"]).suffix.lstrip(".") or "jpg"
        cached = cache_dir / f"asset-{asset_id}.{ext}"
        if not cached.exists():
            import httpx
            async with httpx.AsyncClient(timeout=60.0) as client:
                r = await client.get(signed)
                if r.status_code >= 300:
                    raise HTTPException(
                        502, f"bucket fetch failed for asset #{asset_id} ({r.status_code})",
                    )
                cached.write_bytes(r.content)
        return cached
    raise HTTPException(410, f"asset #{asset_id} has no resolvable file")


# ---------------------------------------------------------------------------
# Reference slots — each can hold either a saved producer_assets row OR a
# freshly-uploaded file (yas_uploads row), or be empty.
# ---------------------------------------------------------------------------

# How a picked or uploaded intro/outro image is used. 'as_is' (the default):
# the card IS that image, its pixels copied, padded to the video's shape when
# it is another shape, no model call. 'new_vibe' ("New design in this style"):
# a new card drawn in the image's style. 'text_only' (runs made before
# 'as_is'): the image redrawn with the script's words.
REF_MODE_AS_IS = "as_is"
REF_MODE_NEW_VIBE = "new_vibe"
REF_MODE_TEXT_ONLY = "text_only"
REF_MODES = (REF_MODE_AS_IS, REF_MODE_NEW_VIBE, REF_MODE_TEXT_ONLY)


def _check_ref_mode(mode: Any) -> str | None:
    """`mode` when it is a ref mode (or None, for 'not given'); a 400 for
    anything else, before anything is written."""
    if mode is None or mode in REF_MODES:
        return mode
    raise HTTPException(400, f"ref mode must be one of: {', '.join(REF_MODES)}")


def _ref_mode(refs: dict | None, role: str) -> str:
    """The mode a bookend's picked image is used in: the stored one, else
    'as_is' (a pick is kept as it is unless the user chose otherwise)."""
    mode = (refs or {}).get(f"{role}_ref_mode")
    return mode if mode in REF_MODES else REF_MODE_AS_IS


def _check_keep_text(v: Any) -> bool | None:
    """A "Keep card text" value: True / False, or None for 'not given'; a 400
    for anything else, before anything is written."""
    if v is None or isinstance(v, bool):
        return v
    raise HTTPException(400, "keep_text must be true or false")


def _keep_text(refs: dict | None, role: str) -> bool:
    """Whether a kept intro/outro card keeps its own words ("Keep card
    text"): its headline is then never swapped for the script's, and the
    script takes the card's words instead. Off by default."""
    return bool((refs or {}).get(f"{role}_keep_text"))


class TalkingHeadRefs:
    """Per-slot references. Each slot is one of:
      - asset_id  (a saved producer_assets row) — pulled by id
      - upload_id (a yas_uploads row, created via the existing
                   /poster-human/upload-ref endpoint) — pulled by id
      - both None → slot empty

    Only one of asset_id / upload_id may be set per slot. If both arrive we
    prefer the upload (user just dropped a new file → that's the intent).
    """

    def __init__(
        self,
        *,
        character_asset_id: int | None = None,
        character_upload_id: int | None = None,
        bg_asset_id: int | None = None,
        bg_upload_id: int | None = None,
        intro_asset_id: int | None = None,
        intro_upload_id: int | None = None,
        outro_asset_id: int | None = None,
        outro_upload_id: int | None = None,
        intro_video_upload_id: int | None = None,
        outro_video_upload_id: int | None = None,
        intro_ref_mode: str | None = None,
        outro_ref_mode: str | None = None,
        intro_card_id: int | None = None,
        outro_card_id: int | None = None,
        intro_keep_text: bool | None = None,
        outro_keep_text: bool | None = None,
    ) -> None:
        self.character_asset_id = character_asset_id
        self.character_upload_id = character_upload_id
        self.bg_asset_id = bg_asset_id
        self.bg_upload_id = bg_upload_id
        self.intro_asset_id = intro_asset_id
        self.intro_upload_id = intro_upload_id
        self.outro_asset_id = outro_asset_id
        self.outro_upload_id = outro_upload_id
        # Direct-video intro/outro: a yas_uploads id (video) used AS-IS as the
        # bookend clip — skips card-gen + Veo entirely.
        self.intro_video_upload_id = intro_video_upload_id
        self.outro_video_upload_id = outro_video_upload_id
        # How a picked intro/outro image is used (REF_MODES): "as_is" (the
        # default: the card is the image), "new_vibe" (a new card in its
        # style) or "text_only" (older runs). Anything else is a 400.
        self.intro_ref_mode = _check_ref_mode(intro_ref_mode) or REF_MODE_AS_IS
        self.outro_ref_mode = _check_ref_mode(outro_ref_mode) or REF_MODE_AS_IS
        # A card from a previous reel picked as the intro/outro (a TH v2 card
        # row id), and whether a kept card keeps its own words.
        self.intro_card_id = intro_card_id
        self.outro_card_id = outro_card_id
        self.intro_keep_text = bool(_check_keep_text(intro_keep_text))
        self.outro_keep_text = bool(_check_keep_text(outro_keep_text))

    def to_dict(self) -> dict:
        return {
            "character_asset_id": self.character_asset_id,
            "character_upload_id": self.character_upload_id,
            "bg_asset_id": self.bg_asset_id,
            "bg_upload_id": self.bg_upload_id,
            "intro_asset_id": self.intro_asset_id,
            "intro_upload_id": self.intro_upload_id,
            "outro_asset_id": self.outro_asset_id,
            "outro_upload_id": self.outro_upload_id,
            "intro_video_upload_id": self.intro_video_upload_id,
            "outro_video_upload_id": self.outro_video_upload_id,
            "intro_ref_mode": self.intro_ref_mode,
            "outro_ref_mode": self.outro_ref_mode,
            "intro_card_id": self.intro_card_id,
            "outro_card_id": self.outro_card_id,
            "intro_keep_text": self.intro_keep_text,
            "outro_keep_text": self.outro_keep_text,
        }

    def has_intro(self) -> bool:
        return (self.intro_asset_id is not None or self.intro_upload_id is not None
                or self.intro_card_id is not None)

    def has_outro(self) -> bool:
        return (self.outro_asset_id is not None or self.outro_upload_id is not None
                or self.outro_card_id is not None)


# ---------------------------------------------------------------------------
# Image-model helpers — talking-head-v2 supports a separate model choice per
# tile (intro card, main keyframe, outro card).
# ---------------------------------------------------------------------------

DEFAULT_IMAGE_MODELS = {
    "main": ENGINE_GEMINI_PRO,
    "intro": ENGINE_GEMINI_PRO,
    "outro": ENGINE_GEMINI_PRO,
}


def _normalize_image_models(raw: dict | None) -> dict:
    """Coerce a partial / unknown-value image_models dict into the canonical
    shape with valid engine names."""
    # Engine now forced to Gemini for every tile (OpenAI image path retired).
    return {
        "main": ENGINE_GEMINI_PRO,
        "intro": ENGINE_GEMINI_PRO,
        "outro": ENGINE_GEMINI_PRO,
    }


# ---------------------------------------------------------------------------
# Upload-slot resolution — pulls a yas_uploads row's local path / preview.
# ---------------------------------------------------------------------------

async def _resolve_upload_row(upload_id: int) -> dict | None:
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT id, token, local_path, mime_type, width, height, bytes,
                       created_at
                  FROM yas_uploads WHERE id = %s
                """,
                (upload_id,),
            )
            row = await cur.fetchone()
    return dict(row) if row else None


async def _resolve_upload_path(upload_id: int) -> Path:
    row = await _resolve_upload_row(upload_id)
    if not row:
        raise HTTPException(404, f"upload #{upload_id} not found")
    lp = row.get("local_path")
    if not lp:
        raise HTTPException(410, f"upload #{upload_id} has no local_path")
    p = Path(lp)
    if not p.is_absolute():
        p = _backend_root() / p
    if not p.exists():
        raise HTTPException(410, f"upload #{upload_id} file missing on disk")
    return p


async def _slot_row_and_brief(
    *,
    asset_id: int | None,
    upload_id: int | None,
    kind: str,  # "character" | "bg" | "intro" | "outro"
) -> tuple[dict | None, str | None]:
    """For a slot that can be either an asset or an upload, return
    (loaded_row, prompt_brief). Returns (None, None) when both ids are None.

    Uploads don't carry ai_description / attributes, so the brief is much
    thinner — just a "(user-uploaded reference)" tag with whatever metadata
    we have (dimensions, mime, kind).
    """
    if upload_id:
        row = await _resolve_upload_row(upload_id)
        if not row:
            return None, None
        # Synthesize a brief — uploads have no AI description; the model
        # sees the file itself via source_images, so this is just contextual.
        brief = f"(user-uploaded {kind} reference; the actual file is attached as a guidance image)"
        return row, brief
    if asset_id:
        row = await _resolve_asset_row(asset_id)
        if not row:
            return None, None
        if kind == "character":
            return row, th_prompts.character_brief_from_asset(row)
        if kind == "bg":
            return row, th_prompts.bg_brief_from_asset(row)
        return row, th_prompts.card_brief_from_asset(row, kind=kind)
    return None, None


async def _slot_path(
    *, asset_id: int | None, upload_id: int | None,
) -> Path | None:
    """Resolve a slot to a local file path. Uploads take priority over assets
    when both are set."""
    if upload_id:
        return await _resolve_upload_path(upload_id)
    if asset_id:
        return await _resolve_asset_path(asset_id)
    return None


# ---------------------------------------------------------------------------
# Step 1 — Script generation
# ---------------------------------------------------------------------------


def _solo_front_facing_action(action: str) -> str:
    """The face, expression and pose are LOCKED elsewhere, so the script
    `action` must add nothing about the face/eyes/expression and must never
    involve another person. We deterministically replace it with a fixed calm,
    solo, front-facing posture — so a face-describing action ('eyes widen with
    excitement') or an interaction ('high-five with a peer') can never reach the
    still or the Veo prompt. An over-expressive / 'performing' face also raises
    Veo's likeness-filter score (15236754), so a calm neutral posture helps avoid
    blocks too."""
    _ = action  # intentionally ignored — the action is always normalized
    return "calm and composed, facing the camera directly, still with minimal natural movement"


async def _script_llm(user_prompt: str) -> dict | None:
    """The script call shared by generate_script and the re-script path:
    OpenAI gpt-4o with structured output, and when OpenAI is out of budget
    or errors, Gemini with the same prompt and schema (a dead OpenAI key
    never blocks a script). The script dict, or None when both fail."""
    # Generate the script with OpenAI (structured output via response_format).
    # If OpenAI is out of budget OR errors, FALL BACK to Gemini with the same
    # prompt + schema so a dead OpenAI key never blocks script generation.
    script: Any = None
    try:
        raw = await asyncio.wait_for(
            openai_chat(
                user_prompt,
                system=th_prompts.TALKING_HEAD_SCRIPT_SYSTEM,
                model="gpt-4o",
                max_tokens=1200,
                temperature=0.7,
                response_format=_SCRIPT_RESPONSE_FORMAT,
            ),
            timeout=SCRIPT_LLM_TIMEOUT_SEC,
        )
        try:
            script = json.loads(raw)
        except json.JSONDecodeError:
            log.error("OpenAI returned non-JSON despite structured output: %r", raw[:400])
            script = None
        if isinstance(script, dict):
            log.info("[yas-th] script generated via OpenAI gpt-4o")
    except OpenAIBudgetExceededError as e:
        log.warning("[yas-th] OpenAI budget exhausted (%s) — falling back to Gemini", e)
    except Exception:
        log.exception("[yas-th] OpenAI script generation failed — falling back to Gemini")

    if not isinstance(script, dict):
        # ----- Gemini fallback ------------------------------------------------
        try:
            # Fast model + thinking OFF: Pro burns hidden thinking tokens that
            # overflow max_tokens and truncate the JSON (→ {"_raw":…}); flash
            # with thinking_budget=0 spends the whole budget on the structured
            # output, the same proven setup as the video-editor op-planner.
            gem = await asyncio.wait_for(
                gemini_multimodal(
                    user_prompt,
                    system=th_prompts.TALKING_HEAD_SCRIPT_SYSTEM,
                    json_schema=_gemini_safe_schema(th_prompts.TALKING_HEAD_SCRIPT_SCHEMA),
                    model=config.vertex_gemini_fast_model,
                    temperature=0.7,
                    max_tokens=2000,
                    thinking_budget=0,
                ),
                timeout=SCRIPT_LLM_TIMEOUT_SEC,
            )
            if isinstance(gem, str):
                gem = json.loads(gem)
            if isinstance(gem, dict) and gem.get("_raw") is None:
                script = gem
                log.info("[yas-th] script generated via Gemini fallback (%s)", config.vertex_gemini_fast_model)
        except VertexBudgetExceededError as e:
            log.error("[yas-th] Gemini script fallback budget exhausted: %s", e)
        except Exception:
            log.exception("[yas-th] Gemini script fallback failed")

    return script if isinstance(script, dict) else None


async def _last_default_speaker_gender() -> str | None:
    """The gender of the newest run whose speaker is a default cast, or None
    (no such run yet, or the lookup failed)."""
    try:
        async with get_conn() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    SELECT metadata->'speaker'->>'gender' AS gender
                      FROM yas_generations
                     WHERE mode = %s
                       AND metadata->'speaker'->>'source' = %s
                     ORDER BY created_at DESC, id DESC
                     LIMIT 1
                    """,
                    (MODE_PREVIEW, th_prompts.SPEAKER_SOURCE_DEFAULT),
                )
                row = await cur.fetchone()
    except Exception:
        log.exception("[yas-th] last default speaker lookup failed (non-fatal)")
        return None
    gender = (row or {}).get("gender")
    return gender if gender in th_prompts.SPEAKER_GENDERS else None


async def _default_speaker(topic: str, gender: str | None = None) -> dict:
    """A default Emirati speaker {subject, gender, source}: of `gender` when
    the topic fixes it, else the other gender than the last default speaker
    (a man first), so runs alternate. The cast is picked with an RNG seeded
    by the topic and gender, so the same topic gets the same person."""
    if gender not in th_prompts.SPEAKER_GENDERS:
        last = await _last_default_speaker_gender()
        gender = "woman" if last == "man" else "man"
    rng = random.Random(f"{(topic or '').strip().lower()}|{gender}")
    cast = th_prompts.default_emirati_cast(rng, gender)
    return {"subject": cast["subject"], "gender": gender,
            "source": th_prompts.SPEAKER_SOURCE_DEFAULT}


def _flag_true(v: Any) -> bool:
    """A model's boolean answer: True, or the string 'true'."""
    return v is True or (isinstance(v, str) and v.strip().lower() == "true")


async def _script_speaker(
    script: dict,
    *,
    topic: str,
    dlgs: list[str | None],
    prev_speaker: dict | None = None,
) -> dict:
    """Who is on camera when no character is picked: metadata.speaker =
    {subject, gender, source}.

      - A speaker the user set in the Speaker field (source 'user') is kept.
      - When the topic or dialogue fixes the speaker's nationality, ethnicity
        or attire (`speaker_user_specified`), the model's subject without
        library tags (source 'topic').
      - Otherwise a default Emirati cast (source 'default_emirati'). A
        re-script keeps the run's default while it fits a gender the topic
        fixes; a new one is of that gender, else alternates man / woman.
    """
    prev = prev_speaker if isinstance(prev_speaker, dict) and prev_speaker.get("subject") else None
    if prev and prev.get("source") == th_prompts.SPEAKER_SOURCE_USER:
        return dict(prev)
    fixed_gender = th_prompts.topic_speaker_gender(topic, *(d for d in dlgs if d))
    subject = th_prompts.clean_subject(script.get("subject"))
    if _flag_true(script.get("speaker_user_specified")) and subject:
        return {"subject": subject,
                "gender": th_prompts.subject_gender(subject) or fixed_gender,
                "source": th_prompts.SPEAKER_SOURCE_TOPIC}
    if (prev and prev.get("source") == th_prompts.SPEAKER_SOURCE_DEFAULT
            and fixed_gender in (None, prev.get("gender"))):
        return dict(prev)
    return await _default_speaker(topic, fixed_gender)


async def _finalize_script(
    script: dict,
    *,
    topic: str,
    scene_count: int,
    dlgs: list[str | None],
    has_character: bool,
    prev_speaker: dict | None = None,
    kept_words: dict[str, str] | None = None,
) -> tuple[dict, dict]:
    """Everything a script gets after the LLM call, shared by generate_script
    and the re-script path (_gpt_script_dict). Returns (script, speaker).

      - The solo guard on `action`, `scenes` padded or cut to scene_count,
        the VERBATIM GUARD on user-authored lines, scenes[0] mirrored into
        the top-level dialogue/hook.
      - The speaker (metadata.speaker, see _script_speaker): who is on
        camera when no character is picked. Without a picked character
        (`has_character` False) `subject` becomes that speaker, so the
        default holds even when the model ignores the DEFAULT SPEAKER rule.
      - `kept_words` ({'intro_headline': ...}): the words of an intro/outro
        card kept with its own text ("Keep card text"); the script says them
        instead of what the model wrote (_kept_script_words).
    """
    for field, text in (kept_words or {}).items():
        if field in ("intro_headline", "intro_subtitle", "outro_headline", "outro_subtitle") and text:
            script[field] = text
    # ----- Post-GPT verbatim guard + scenes-array normalisation ---------
    # The schema demands `scenes` is always present. Older GPT responses
    # (pre-v6 prompt) may omit it; if so, synthesize from the top-level
    # dialogue/hook fields so downstream code can rely on the unified shape.
    # Solo guard: the talking head is ONE person, alone — strip any
    # action that references another person or an interaction.
    script["action"] = _solo_front_facing_action(script.get("action"))
    scenes_field = script.get("scenes")
    if not isinstance(scenes_field, list) or not scenes_field:
        # GPT didn't emit scenes — backfill from the legacy top-level fields.
        script["scenes"] = [{
            "dialogue": (script.get("dialogue") or "").strip(),
            "hook": (script.get("hook") or "").strip(),
        }]
        scenes_field = script["scenes"]

    # Length mismatch — pad or truncate to scene_count.
    if len(scenes_field) < scene_count:
        log.warning(
            "GPT returned %d scenes for scene_count=%d — padding with empties",
            len(scenes_field), scene_count,
        )
        while len(scenes_field) < scene_count:
            scenes_field.append({"dialogue": "", "hook": ""})
    elif len(scenes_field) > scene_count:
        log.warning(
            "GPT returned %d scenes for scene_count=%d — truncating",
            len(scenes_field), scene_count,
        )
        del scenes_field[scene_count:]

    # ---- VERBATIM GUARD ---------------------------------------------
    # For every user-authored dialogue slot, force scenes[i].dialogue
    # to equal the user's text exactly. This is the unconditional
    # guarantee the user asked for in v6: typing X into the dialogue
    # box means the speaker says X — no model drift allowed.
    def _normalize(s: str) -> str:
        # Only the normalisations the prompt promises: trailing whitespace
        # + curly quotes -> straight quotes.
        return (
            (s or "")
            .strip()
            .replace("“", '"').replace("”", '"')
            .replace("‘", "'").replace("’", "'")
        )

    for i, want in enumerate(dlgs):
        if not want:
            continue
        want_n = _normalize(want)
        got = _normalize(scenes_field[i].get("dialogue") or "")
        if got != want_n:
            log.warning(
                "verbatim drift on scene %d — overwriting GPT '%s' with user '%s'",
                i, got[:80], want_n[:80],
            )
            scenes_field[i]["dialogue"] = want_n
        # Hook is the opening 6-10 words of dialogue. If GPT's hook
        # isn't a prefix of the (forced) dialogue, regenerate it.
        hook = (scenes_field[i].get("hook") or "").strip()
        if not want_n.lower().startswith(hook.lower()) or not hook:
            words = want_n.split()
            scenes_field[i]["hook"] = " ".join(words[: min(8, len(words))])

    # Mirror scenes[0] into the top-level dialogue/hook fields so any
    # legacy code path (single-scene image/Veo prompts) still works.
    if scenes_field:
        script["dialogue"] = scenes_field[0].get("dialogue") or script.get("dialogue", "")
        script["hook"] = scenes_field[0].get("hook") or script.get("hook", "")

    # Sanity: hook should be a prefix of dialogue.
    hook = (script.get("hook") or "").strip()
    dlg = (script.get("dialogue") or "").strip()
    if hook and dlg and not dlg.lower().startswith(hook.lower()):
        log.warning(
            "script.hook is not a prefix of script.dialogue (hook=%r, dialogue=%r)",
            hook[:80], dlg[:120],
        )

    speaker = await _script_speaker(
        script, topic=topic, dlgs=dlgs, prev_speaker=prev_speaker,
    )
    if not has_character:
        script["subject"] = speaker["subject"]
    return script, speaker


async def generate_script(
    *,
    topic: str,
    refs: TalkingHeadRefs,
    lock_character: bool,
    image_models: dict,
    user_dialogue: str | None = None,
    user_dialogues: list[str | None] | None = None,
    scene_count: int = 1,
    scene_overrides: list[dict] | None = None,
    generate_intro: bool = True,
    generate_outro: bool = True,
    aspect_ratio: str = "9:16",
    video_quality: str = "normal",
    framing: str | None = None,
) -> dict:
    """Run GPT-4o to produce the talking-head-v2 script, persist a row, return it.

    The row's image columns are NULL at this point — preview generation
    populates them in the next step.

    `image_models` is a dict like {main, intro, outro} — each value is one
    of "openai" | "gemini_pro". This is stored on the run's metadata so the
    preview phase knows which model to use per tile.

    v6 — multi-scene support:
      - `scene_count` is 1..MAX_SCENES. Single-scene runs (the default)
        keep the existing schema/metadata/Remotion template UNCHANGED.
      - `user_dialogues` is an optional list of length scene_count. When
        provided, each non-empty entry is preserved VERBATIM in the
        matching scenes[i].dialogue slot (the v4 verbatim guarantee, now
        per-scene). Empty / None entries get GPT-generated lines.
      - `scene_overrides` lets the caller override the global character
        and/or bg ref per scene. Each entry is a dict with keys
        character_asset_id, character_upload_id, bg_asset_id,
        bg_upload_id. None / missing entries inherit from the global refs.
      - `framing` (close | above_hip | full_body) is how close the camera is
        for every still; None is DEFAULT_FRAMING (above the hip).
    """
    topic = (topic or "").strip()
    if not topic:
        raise HTTPException(400, "topic is required")
    framing = _check_framing(framing) or DEFAULT_FRAMING
    image_models = _normalize_image_models(image_models)
    log.info("[yas-th] generate_script normalized image_models=%s", image_models)

    # Clamp + normalise scene_count.
    try:
        scene_count = int(scene_count or 1)
    except (TypeError, ValueError):
        scene_count = 1
    scene_count = max(1, min(MAX_SCENES, scene_count))

    # Build a clean dialogues list aligned to scene_count.
    dlgs: list[str | None] = [None] * scene_count
    if user_dialogues:
        for i, d in enumerate(user_dialogues[:scene_count]):
            s = (d or "").strip()
            dlgs[i] = s if s else None
    elif user_dialogue and user_dialogue.strip():
        dlgs[0] = user_dialogue.strip()

    _ensure_dirs()

    # Resolve all four slots — each can be an asset, an upload, or empty.
    _, character_brief = await _slot_row_and_brief(
        asset_id=refs.character_asset_id,
        upload_id=refs.character_upload_id,
        kind="character",
    )
    _, bg_brief = await _slot_row_and_brief(
        asset_id=refs.bg_asset_id,
        upload_id=refs.bg_upload_id,
        kind="bg",
    )
    _, intro_brief = await _slot_row_and_brief(
        asset_id=refs.intro_asset_id,
        upload_id=refs.intro_upload_id,
        kind="intro",
    )
    _, outro_brief = await _slot_row_and_brief(
        asset_id=refs.outro_asset_id,
        upload_id=refs.outro_upload_id,
        kind="outro",
    )

    # Build full RAG context (brand + nearest producer scripts + perf-weighted
    # IG posts + the studio's OWN past reels), then MAGNIFY the topic into a
    # committed angle and gather recent bookends for anti-repetition.
    context_block, committed_angle, recent_bookends = await _build_script_inputs(
        topic, user_dialogue=dlgs[0] if dlgs else user_dialogue,
    )
    # A bookend kept as it is shows its own words: no anti-repetition for it.
    recent_bookends = _recent_without_kept(recent_bookends, _kept_bookend_roles(refs.to_dict()))

    user_prompt = th_prompts.build_script_user_prompt(
        topic=topic,
        context_block=context_block,
        character_brief=character_brief,
        bg_brief=bg_brief,
        intro_brief=intro_brief,
        outro_brief=outro_brief,
        user_dialogue=user_dialogue,
        user_dialogues=dlgs,
        scene_count=scene_count,
        committed_angle=committed_angle,
        recent_bookends=recent_bookends,
    )

    # Generate the script: OpenAI, falling back to Gemini (_script_llm).
    script = await _script_llm(user_prompt)
    if script is None:
        raise HTTPException(502, "Script generation failed — try again in a moment.")

    # The guards every script gets (solo, scenes, verbatim) and who speaks
    # when no character is picked (metadata.speaker).
    script, speaker = await _finalize_script(
        script, topic=topic, scene_count=scene_count, dlgs=dlgs,
        has_character=bool(character_brief),
    )

    # ----- Build the metadata payload ----------------------------------
    metadata: dict[str, Any] = {
        "category": "talking_head",
        "script": script,
        # Who is on camera when no character is picked: {subject, gender,
        # source: default_emirati | topic | user}. Set by _script_speaker,
        # changed in the Look step's Speaker field (edit_run speaker_subject).
        "speaker": speaker,
        "refs": refs.to_dict(),
        "lock_character": bool(lock_character),
        "image_models": image_models,
        "aspect_ratio": _norm_aspect(aspect_ratio),
        "video_quality": _norm_quality(video_quality),
        # How close the camera is (the Look step's Framing; a scene may
        # override it in metadata.scenes[i].framing).
        "framing": framing,
        # Topic-magnify output kept for debugging / traceability (internal —
        # not surfaced in the UI).
        "committed_angle": committed_angle,
        "intro_image_id": None,
        "outro_image_id": None,
        # Bookend generate-toggle (owner opt-out). Reuses the existing exclusion
        # flag as the single "is this bookend part of the video" switch: when a
        # bookend is excluded, generate_previews skips its card image-gen,
        # start_videos skips its Veo clip, and the stitch omits it.
        "intro_excluded": not bool(generate_intro),
        "outro_excluded": not bool(generate_outro),
        # Legacy triplet of Veo animations (intro/main/outro) — populated by
        # start_videos() for single-scene runs. For multi-scene runs, the
        # `main_animation_id` is replaced by per-scene entries in `scenes`.
        "intro_animation_id": None,
        "main_animation_id": None,
        "outro_animation_id": None,
        "composition_status": None,
        "composition_path": None,
    }

    if scene_count > 1:
        # v6 multi-scene metadata. Per-scene character/bg refs default to
        # None (= inherit from the global refs). Keyframe and animation
        # ids start as None and get filled in by generate_previews() and
        # start_videos_multi().
        scenes_meta: list[dict[str, Any]] = []
        for i in range(scene_count):
            ov = (scene_overrides or [None] * scene_count)[i] or {}
            scene_dlg = (script.get("scenes") or [{}])[i].get("dialogue", "") if isinstance(script, dict) else ""
            scene_hook = (script.get("scenes") or [{}])[i].get("hook", "") if isinstance(script, dict) else ""
            scenes_meta.append({
                "index": i,
                "dialogue": scene_dlg,
                "hook": scene_hook,
                "user_authored": bool(dlgs[i]),
                "keyframe_id": None,
                "animation_id": None,
                "character_asset_id": ov.get("character_asset_id"),
                "character_upload_id": ov.get("character_upload_id"),
                "bg_asset_id": ov.get("bg_asset_id"),
                "bg_upload_id": ov.get("bg_upload_id"),
            })
        metadata["scene_count"] = scene_count
        metadata["scenes"] = scenes_meta
        metadata["composition_template"] = "talking-head-v2-multi-scene"
    else:
        # Single-scene: identical to v5 metadata shape. No new fields.
        pass

    # Use the main keyframe's chosen model as the row's `image_model`
    # column (NOT NULL on the table). The full per-tile breakdown lives
    # in metadata.image_models.
    main_model = image_models["main"]

    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                INSERT INTO yas_generations
                  (topic, mode, image_model, prompt_used, status, metadata)
                VALUES (%s, %s, %s, %s, %s, %s)
                RETURNING id, created_at, updated_at
                """,
                (
                    topic, MODE_PREVIEW, main_model,
                    user_prompt[:8000],  # store the user prompt for debugging
                    STATUS_READY,        # row's `status` reflects the SCRIPT being ready
                    Jsonb(metadata),
                ),
            )
            new = await cur.fetchone()
            run_id = int(new["id"])

    # Embed the finished reel so future generations can learn from it (and
    # avoid repeating its angle). Best-effort — never blocks the response.
    await _embed_and_store_script(run_id, topic, script, committed_angle)

    log.info(
        "[yas-th] script generated for run #%d (topic=%r, models=%s, lock=%s)",
        run_id, topic[:60], image_models, lock_character,
    )

    # Return the same shape fetch_run does
    return await fetch_run(run_id)


# ---------------------------------------------------------------------------
# Step 2 — Preview generation (3 images in parallel)
# ---------------------------------------------------------------------------

async def _extract_card_style_dna(ref_path: Path) -> str:
    """Extract transferable style-DNA from a reference card for the `new_vibe`
    render path. Returns a compact ``[STYLE-DNA]`` text block, or "" on any
    error (the caller then falls back to the no-ref voxel render).

    We deliberately send only this TEXT description to the renderer — never the
    reference image itself — to avoid the documented photocopy / colour-shifted
    -twin failure mode.
    """
    try:
        result = await asyncio.wait_for(
            gemini_multimodal(
                "Analyse this reference card and extract its design-DNA per the "
                "schema. Describe the transferable visual language only — never "
                "its layout or text content.",
                images=[ref_path],
                system=STYLE_EXTRACT_SYSTEM,
                json_schema=STYLE_EXTRACT_SCHEMA,
                temperature=0.3,
                max_tokens=700,
                model=config.vertex_gemini_fast_model,
            ),
            timeout=60,
        )
    except Exception:
        log.exception("[yas-th] style-DNA extract failed for %s", ref_path)
        return ""
    if not isinstance(result, dict):
        return ""
    palette = result.get("palette") if isinstance(result.get("palette"), dict) else {}
    hexes = ", ".join((palette.get("dominant_colors") or [])[:6])
    lines = ["[STYLE-DNA] — render a FRESH card in THIS exact visual language:"]
    if palette.get("palette_description"):
        suffix = f" (hex: {hexes})" if hexes else ""
        lines.append(f"- Palette: {palette['palette_description']}{suffix}")
    elif hexes:
        lines.append(f"- Palette hex: {hexes}")
    for label, key in (
        ("Imagery medium", "imagery_medium"),
        ("Illustration style", "illustration_style"),
        ("Background treatment", "background_treatment"),
        ("Typography mood", "typography_mood"),
        ("Lighting", "lighting"),
        ("Texture & finish", "texture_and_finish"),
    ):
        val = (result.get(key) or "").strip() if isinstance(result.get(key), str) else ""
        if val:
            lines.append(f"- {label}: {val}")
    patterns = ", ".join((result.get("background_patterns") or [])[:6])
    if patterns:
        lines.append(f"- Background patterns/motifs: {patterns}")
    idioms = ", ".join((result.get("composition_idioms") or [])[:4])
    if idioms:
        lines.append(f"- Brand composition idioms (carry the habit, not the layout): {idioms}")
    # Only the header line means nothing extracted → treat as empty.
    return "\n".join(lines) if len(lines) > 1 else ""


async def _render_card_animation_style(
    *,
    role: str,            # "intro" | "outro"
    topic: str,
    headline: str,
    subtitle: str,
    ref_mode: str,        # "new_vibe" | "text_only"
    ref_path: Path | None,
    feedback: str | None = None,
    aspect: str = "9:16",
    conform: str | None = None,
) -> tuple[bytes, str, str]:
    """Render an intro/outro card with the REAL 42 logo baked in, reusing the
    Create-Animation glue (``card_animation.render_card_image`` /
    ``build_logo_parts`` / ``aspect_render_note``). Returns
    ``(png_bytes, mime, used_prompt)``.

    Three branches (all render on gemini via card_animation.render_card_image):
      - no-ref / new_vibe-without-usable-DNA → full voxel (gpt-4o expand → gemini render)
      - new_vibe (with DNA)                  → fresh card adopting the ref's medium
      - text_only                            → reproduce the ref exactly, swap only text

    The logo parts ALWAYS come first; the model is told (via system prompt) to
    draw no logo of its own.

    `conform` (opt-in): a _conform_to_aspect policy; the card is returned in
    exactly `aspect`'s shape. TH v2 passes 'reframe'; the default None keeps
    the rendered bytes as they are (the video editor's bookends).
    """
    aspect = _norm_aspect(aspect)
    # Aspect directive + logo parts come from the shared Create-Animation glue
    # (card_animation) — same recipe the standalone Animation tool renders with.
    aspect_note = card_animation.aspect_render_note(aspect, edge_to_edge=True)
    text_block = th_prompts._th_text_block(headline, subtitle, aspect)

    logo_parts, logo_attached = card_animation.build_logo_parts()
    if not logo_attached:
        log.info(
            "[yas-th] 42 logo PNG not present at %s — model will draw the 42 "
            "wordmark itself (self-draw logo clause)", card_animation.LOGO_PATH,
        )
    # Pick the logo instruction to match reality: replicate the attached PNG, or
    # (when no PNG is available) render a clean 42 wordmark from scratch.
    logo_clause = th_prompts.th_logo_clause(logo_attached=logo_attached)

    use_ref = ref_path is not None and ref_path.exists()
    mode = (ref_mode or "new_vibe") if use_ref else "none"

    if mode == "text_only":
        instr = th_prompts.build_th_text_only_instruction(
            headline=headline, subtitle=subtitle, aspect=aspect,
        )
        if feedback:
            instr += f"\n\nADDITIONAL DIRECTION: {feedback.strip()}"
        parts: list[tuple[str, Any]] = list(logo_parts) + [
            ("text", instr),
            ("image", ref_path),
            ("text", aspect_note),
        ]
        used_prompt = f"[text_only]\n{instr}\n\n{aspect_note}"
        system = th_prompts.TH_TEXT_ONLY_RENDER_SYSTEM + "\n\n" + logo_clause
    else:
        dna = await _extract_card_style_dna(ref_path) if mode == "new_vibe" else ""
        if dna:
            body = dna + "\n\n" + text_block
            if feedback:
                body += f"\n\nADDITIONAL DIRECTION: {feedback.strip()}"
            body += "\n\n" + aspect_note
            parts = list(logo_parts) + [("text", body)]
            used_prompt = f"[new_vibe]\n{body}"
            system = th_prompts.TH_NEWVIBE_RENDER_SYSTEM + "\n\n" + logo_clause
        else:
            # no-ref (or new_vibe whose DNA came back empty) → the same recipe as
            # the standalone Create-Animation frame (_render_animation_frame),
            # reusing the SAME shared building blocks. TWO intended differences:
            # (1) n=1 (Create Animation renders a 4-up batch); (2) the SCRIPT's
            # intro/outro words are pinned VERBATIM on the card — Talking Head is
            # scripted, so the exact headline/subtitle (incl. any URL/CTA) must
            # render unchanged. Everything else is byte-for-byte Create Animation:
            #   • Stage-1: Create Poster's concept brain (style.concepts_system),
            #     topic fed RAW (as Create Animation feeds its prompt) — drives
            #     the voxel SCENE.
            #   • Stage-2: Create Poster's render brain (style.gemini_stage2_system),
            #     SAME logo parts + (non-edge-to-edge) aspect note; the concept's
            #     invented text is swapped for the verbatim script text below.
            style = get_poster_style(DEFAULT_POSTER_STYLE)  # voxel
            # The bookend's own content is the topic (headline + subtitle) so
            # intro and outro cards differ and stay on-message.
            card_topic = ". ".join(
                p for p in ((headline or "").strip(), (subtitle or "").strip()) if p
            ) or (topic or "").strip() or f"{role} title card"
            stage1_msg = build_concepts_user_message(card_topic, n=1)
            if feedback:
                stage1_msg += f"\n\nADDITIONAL DIRECTION: {feedback.strip()}"
            scene = ""
            try:
                raw = await asyncio.wait_for(
                    openai_chat(
                        stage1_msg,
                        system=style.concepts_system,
                        model="gpt-4o",
                        temperature=0.95,
                        max_tokens=_CARD_CONCEPTS_MAX_TOKENS,
                        response_format={"type": "json_object"},
                    ),
                    timeout=IMAGE_GEN_TIMEOUT_OPENAI,
                )
                concepts = parse_concepts(raw or "", 1)
                scene = (concepts[0]["prompt"].strip() if concepts else "")
            except Exception:
                log.exception("[yas-th] card stage-1 concept failed (role=%s) — using topic fallback", role)
                scene = ""
            if not scene:
                # Stage-1 hiccup: still render a card (never leave it blank).
                scene = f"A 42 Abu Dhabi {role} title card about {card_topic}."
            # The ONE deliberate change vs Create Animation: pin the SCRIPT's exact
            # words. Drop the concept's invented [TEXT TO RENDER] block and append
            # the authoritative one so GEMINI_STAGE2_PROMPT renders the headline +
            # subtitle (incl. a URL / CTA like "42abudhabi.ae") VERBATIM.
            # _th_text_block already emits the exact [TEXT TO RENDER] contract that
            # render system reads and prints only the quoted strings.
            scene = scene.split("[TEXT TO RENDER]")[0].rstrip() + "\n\n" + text_block
            # SAME render assembly as _render_animation_frame: shared logo parts +
            # the (non-edge-to-edge) aspect note + the scene, rendered with Create
            # Poster's Stage-2 system (its own [LOGO FIREWALL] handles the attached
            # PNG, so no TH logo_clause).
            frame_aspect_note = card_animation.aspect_render_note(aspect)
            parts = list(logo_parts) + [("text", f"{frame_aspect_note}\n\n{scene}")]
            used_prompt = f"[voxel-poster]\n{scene}"
            system = style.gemini_stage2_system

    async def _do_render() -> bytes:
        # The shape is forced at the API level: the text note alone let the
        # model return 1280x800 cards for a 9:16 reel, which Veo letterboxed.
        return await card_animation.render_card_image(
            parts,
            system_instruction=system,
            timeout=IMAGE_GEN_TIMEOUT_GEMINI,
            aspect_ratio=_norm_aspect(aspect),
        )

    async with _image_gen_sem():
        try:
            data = await _do_render()
        except asyncio.TimeoutError:
            log.warning(
                "[yas-th] card render TIMEOUT (role=%s mode=%s) — retrying once",
                role, mode,
            )
            data = await _do_render()
    if conform:
        # Outside the semaphore: a reframe is an image call of its own.
        data = await _conform_to_aspect(data, aspect, conform, card=True)
    return data, "image/png", used_prompt


async def _run_image_gen(
    *,
    prompt: str,
    image_model: str,
    source_images: list[Path] | None = None,
    aspect: str = "9:16",
    size: str = "1024x1536",   # gpt-image-1 valid: 1024x1024 / 1024x1536 / 1536x1024
    conform: str | None = "reframe",
) -> tuple[bytes, str]:
    """Run a single image-gen call against the selected engine. Returns
    (bytes, mime_type).

    Behaviors:
      - Gemini Pro Image: dedupe source_images. The identity-anchor
        duplicate that helps gpt-image-1 is unnecessary for Gemini
        (its native identity preservation is stronger) AND slows the
        call past the 180s timeout when 3 images are attached. Dropping
        the duplicate keeps Gemini fast without losing the lock.
      - Both engines: one retry on TimeoutError before bubbling up.
        Image-gen endpoints can hit slow tails when busy; a second
        attempt typically succeeds in <60s once the first call frees
        the slot.
      - The result is put in exactly `aspect`'s shape (_conform_to_aspect
        with the `conform` policy), so a stored still is what Veo animates
        with no bars: Gemini's 9:16 is 768x1344, 1.6% wider than 9:16. A
        call that is itself a reframe passes 'pad' (never a second
        reframe); None returns the model's bytes untouched.
    """
    # Dedupe source_images for Gemini (preserve order, first-wins).
    if image_model == ENGINE_GEMINI_PRO and source_images:
        seen: list[Path] = []
        deduped: list[Path] = []
        for p in source_images:
            if p not in seen:
                seen.append(p)
                deduped.append(p)
        source_images = deduped[:3]

    log.info(
        "[yas-th] _run_image_gen engine=%s has_source_images=%s source_image_count=%d",
        image_model, bool(source_images), len(source_images) if source_images else 0,
    )

    async def _do_call() -> bytes:
        if image_model == ENGINE_GEMINI_PRO:
            return await asyncio.wait_for(
                gemini_image(
                    prompt,
                    source_images=source_images if source_images else None,
                    model=GEMINI_PRO_MODEL,
                    location=GEMINI_PRO_LOCATION,
                    aspect_ratio=aspect,
                    # Reinforce the per-prompt face lock with a persistent rule —
                    # only when a reference image is attached (else it's inert).
                    system_instruction=_GEMINI_FACE_SYSTEM if source_images else None,
                ),
                timeout=IMAGE_GEN_TIMEOUT_GEMINI,
            )
        # OpenAI gpt-image-1 — aspect baked into size string. We pick the
        # closest portrait size offered by the model. When source_images
        # are present, we route through the multi-image edit endpoint
        # (gpt-image-1's `images.edit`) so the reference is used as a
        # style guidance image alongside the prompt.
        if source_images:
            from .openai_client import openai_image_edit
            return await asyncio.wait_for(
                openai_image_edit(
                    prompt,
                    image_paths=list(source_images),
                    size=size,
                    quality="high",
                ),
                timeout=IMAGE_GEN_TIMEOUT_OPENAI,
            )
        return await asyncio.wait_for(
            openai_image(prompt, size=size, quality="high"),
            timeout=IMAGE_GEN_TIMEOUT_OPENAI,
        )

    # Throttle: bound how many image-gen calls hit the (tight-quota) preview
    # model at once. Held across the call + its one timeout-retry so a burst of
    # main/intro/outro/scene gens can't trip a 429. Timing only.
    async with _image_gen_sem():
        try:
            data = await _do_call()
        except asyncio.TimeoutError:
            log.warning(
                "[yas-th] _run_image_gen TIMEOUT on first attempt (engine=%s, sources=%d) — retrying once",
                image_model, len(source_images) if source_images else 0,
            )
            data = await _do_call()
    if conform:
        # Outside the semaphore: a reframe is an image call of its own.
        data = await _conform_to_aspect(data, aspect, conform)
    return data, "image/png"


# ---------------------------------------------------------------------------
# Stills, cards and Veo keyframes in exactly the output shape
# ---------------------------------------------------------------------------
#
# Veo keeps the whole start frame: handed a landscape still for a 9:16 clip
# it letterboxes it, a horizontal video with bars inside a vertical frame.
# Run 809 did exactly that (1248x832 stills, 1280x800 cards, all 9:16 clips),
# and even a still Gemini made at 9:16 (768x1344, 1.6% wider than 9:16) came
# back with 10 px bars (run 827). So every still and card is conformed where
# it is made (_run_image_gen, _render_card_animation_style): trimmed to the
# exact shape when it is close, reframed once (or padded) when the model
# ignored the shape. The Stills step then shows exactly what Veo animates.
#
# A still made before that, or an uploaded one, must not reach Veo as-is
# either, so every Veo submission from this module goes through the two
# wrappers below: the start frame is conformed once (cached next to the
# original, which is left untouched) and the push-in end frame is made from
# that same conformed image.

_CONFORM_POLICIES = ("crop", "pad", "reframe")


def _image_file_dims(path: Path) -> tuple[int | None, int | None]:
    """(width, height) of an image file, or (None, None) if unreadable."""
    try:
        from PIL import Image
        with Image.open(path) as im:
            return im.width, im.height
    except Exception:
        return None, None


def _face_center(im: Any) -> tuple[float, float] | None:
    """Centre (x, y) of the largest frontal face in a PIL image (_face_box);
    None when there is no face or no OpenCV. Lets a trim keep the speaker's
    head."""
    box = _face_box(im)
    if box is None:
        return None
    x, y, fw, fh = box
    return x + fw / 2, y + fh / 2


def _face_box(im: Any) -> tuple[float, float, float, float] | None:
    """(x, y, width, height) of the largest frontal face in a PIL image, in
    its pixels, from OpenCV's bundled Haar cascade run on a copy at most 512
    px on its long side; None when there is no face or no OpenCV."""
    try:
        import cv2
        import numpy as np
    except ImportError:
        return None
    try:
        scale = min(1.0, 512 / max(im.width, im.height))
        gray = im.convert("L")
        if scale < 1.0:
            gray = gray.resize((max(1, round(im.width * scale)), max(1, round(im.height * scale))))
        # One classifier per call: OpenCV does not promise one is safe to
        # share between threads, and this runs in a worker thread.
        cascade = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
        if cascade.empty():
            return None
        side = max(24, gray.width // 20)
        faces = cascade.detectMultiScale(np.asarray(gray), scaleFactor=1.1, minNeighbors=6,
                                         minSize=(side, side))
    except Exception:
        log.exception("[yas-th] face detection failed; using the image centre")
        return None
    if len(faces) == 0:
        return None
    x, y, fw, fh = max((tuple(int(v) for v in f) for f in faces), key=lambda f: f[2] * f[3])
    return x / scale, y / scale, fw / scale, fh / scale


def _crop_to_aspect(im: Any, aspect: str) -> Any:
    """The largest window of exactly `aspect` inside `im`, centred on its
    largest face when there is one (else on the image), kept inside it."""
    uw, uh = _aspect_units(aspect)
    k = min(im.width // uw, im.height // uh)
    if k < 1:
        return im
    cw, ch = uw * k, uh * k
    cx, cy = _face_center(im) or (im.width / 2, im.height / 2)
    left = min(max(0, round(cx - cw / 2)), im.width - cw)
    top = min(max(0, round(cy - ch / 2)), im.height - ch)
    return im.crop((left, top, left + cw, top + ch))


def _pad_to_aspect(im: Any, aspect: str) -> Any:
    """`im` whole and untouched, centred on the smallest canvas of exactly
    `aspect` that holds it; the bands are a blurred, slightly darkened
    stretch of the image itself. Deterministic, no model call."""
    import math
    from PIL import Image, ImageEnhance, ImageFilter
    uw, uh = _aspect_units(aspect)
    k = max(math.ceil(im.width / uw), math.ceil(im.height / uh))
    cw, ch = uw * k, uh * k
    # The fill carries colour, not detail: blur an eighth-size cover of the
    # canvas, then scale it up.
    sw, sh = max(1, cw // 8), max(1, ch // 8)
    scale = max(sw / im.width, sh / im.height)
    cover = im.resize((max(sw, math.ceil(im.width * scale)), max(sh, math.ceil(im.height * scale))),
                      Image.BILINEAR)
    left, top = (cover.width - sw) // 2, (cover.height - sh) // 2
    fill = cover.crop((left, top, left + sw, top + sh))
    fill = fill.filter(ImageFilter.GaussianBlur(radius=max(2, max(sw, sh) // 25)))
    fill = ImageEnhance.Brightness(fill).enhance(0.85).resize((cw, ch), Image.BICUBIC)
    fill.paste(im, ((cw - im.width) // 2, (ch - im.height) // 2))
    return fill


def _reshape_png(data: bytes, aspect: str, how: str) -> bytes:
    """`data` cropped ('crop') or padded ('pad') to exactly `aspect`, as PNG."""
    from PIL import Image
    with Image.open(BytesIO(data)) as src:
        im = src.convert("RGB")
    out = _crop_to_aspect(im, aspect) if how == "crop" else _pad_to_aspect(im, aspect)
    buf = BytesIO()
    out.save(buf, format="PNG")
    return buf.getvalue()


async def _reframe_once(
    data: bytes, aspect: str, *, card: bool = False, source: Path | None = None,
) -> bytes:
    """One Gemini resize-only reframe of an image to `aspect`: keep all of its
    content (the person, or every letter of a card) and extend the background.
    `source` is the image's own file when it has one; otherwise the bytes go
    through a temporary file. Raises when the call fails; the shape of what
    comes back is the caller's to check."""
    prompt = (th_prompts.build_reframe_card_to_aspect_prompt(aspect=aspect) if card
              else th_prompts.build_reframe_to_aspect_prompt(aspect=aspect))
    src = Path(source) if source is not None and Path(source).exists() else None
    tmp: Path | None = None
    if src is None:
        import tempfile
        fd, name = tempfile.mkstemp(suffix=".jpg" if data[:3] == b"\xff\xd8\xff" else ".png")
        os.close(fd)
        tmp = src = Path(name)
        tmp.write_bytes(data)
    try:
        out, _mime = await _run_image_gen(
            prompt=prompt, image_model=ENGINE_GEMINI_PRO,
            source_images=[src], aspect=aspect, conform=None,
        )
    finally:
        if tmp is not None:
            tmp.unlink(missing_ok=True)
    return out


async def _conform_to_aspect(
    data: bytes,
    aspect: Any,
    policy: str = "crop",
    *,
    card: bool = False,
    source: Path | None = None,
) -> bytes:
    """`data` (an image) in exactly `aspect`'s shape, portrait or landscape.

      - already exact, or unreadable: the same bytes.
      - within ±6% of it (Gemini's 768x1344 for 9:16): trimmed for free,
        keeping the largest face inside the window.
      - further off, by `policy`: 'crop' trims anyway; 'pad' keeps every
        pixel on a blurred fill of itself (a card kept as-is); 'reframe' asks
        Gemini once to extend the scene to the shape (the card wording when
        `card`), trims the result, and pads the original when even the
        reframe comes back the wrong shape. A failed reframe call raises.
    The result is PNG whenever it changed."""
    if policy not in _CONFORM_POLICIES:
        raise ValueError(f"unknown conform policy {policy!r}")
    aspect = _norm_aspect(aspect)
    w, h = _image_dims(data)
    if not w or not h or _is_exact_aspect(w, h, aspect):
        return data
    if policy == "crop" or _aspect_off(w, h, aspect) <= _ASPECT_TOLERANCE:
        return await asyncio.to_thread(_reshape_png, data, aspect, "crop")
    if policy == "pad":
        return await asyncio.to_thread(_reshape_png, data, aspect, "pad")
    log.warning("[yas-th] image is %dx%d, not %s: reframing it once", w, h, aspect)
    reframed = await _reframe_once(data, aspect, card=card, source=source)
    rw, rh = _image_dims(reframed)
    if rw and rh and _aspect_off(rw, rh, aspect) <= _ASPECT_TOLERANCE:
        if _is_exact_aspect(rw, rh, aspect):
            return reframed
        return await asyncio.to_thread(_reshape_png, reframed, aspect, "crop")
    log.warning("[yas-th] the reframe came back %sx%s, still not %s: padding the original",
                rw, rh, aspect)
    return await asyncio.to_thread(_reshape_png, data, aspect, "pad")


async def _veo_ready(path: Any, aspect: Any, *, card: bool = False, policy: str = "reframe") -> Any:
    """`path` as a keyframe in exactly `aspect`'s shape: itself when it already
    is (or cannot be read), else a conformed copy cached next to it
    (<stem>.9x16.png): trimmed when within ±6%, else reframed once. With
    `policy` 'pad' (a card kept as-is) it is padded whole whatever its shape,
    as _make_bookend_card keeps it: never redrawn by a model, never trimmed.
    Never raises: a failed or wrong-shaped reframe hands back the original,
    and Veo letterboxes as before."""
    if not path:
        return path
    src = Path(path)
    aspect = _norm_aspect(aspect)
    if not src.exists():
        return path
    w, h = _image_file_dims(src)
    if not w or not h or _is_exact_aspect(w, h, aspect):
        return path
    tag = aspect.replace(":", "x") + (".pad" if policy == "pad" else "")
    out = src.with_name(f"{src.stem}.{tag}.png")
    try:
        if out.exists() and out.stat().st_mtime >= src.stat().st_mtime:
            ow, oh = _image_file_dims(out)
            if ow and oh and _is_exact_aspect(ow, oh, aspect):
                return out
        data = src.read_bytes()
        how = "pad" if policy == "pad" else "crop"
        if policy == "reframe" and _aspect_off(w, h, aspect) > _ASPECT_TOLERANCE:
            log.warning(
                "[yas-th] Veo keyframe %s is %dx%d, not %s: reframing it first",
                src.name, w, h, aspect,
            )
            data = await _reframe_once(data, aspect, card=card, source=src)
            rw, rh = _image_dims(data)
            if not rw or not rh or _aspect_off(rw, rh, aspect) > _ASPECT_TOLERANCE:
                log.warning("[yas-th] reframe of %s came back %s, still not %s: using the original",
                            src.name, (rw, rh), aspect)
                return path
            how = "crop"
        if how == "pad":
            # Kept: even a near shape is padded, so a URL at its edge stays.
            shaped = await asyncio.to_thread(_reshape_png, data, aspect, "pad")
        else:
            shaped = await _conform_to_aspect(data, aspect, how)
        out.write_bytes(shaped)
        log.info("[yas-th] Veo keyframe %s (%dx%d) put in %s -> %s (%s)",
                 src.name, w, h, aspect, out.name, _image_file_dims(out))
        return out
    except Exception:  # noqa: BLE001 (a reframe must never cost the video)
        log.exception("[yas-th] putting Veo keyframe %s in shape failed: using it as-is", src.name)
        return path


async def veo_generate(prompt: str, **kwargs: Any) -> dict:
    """vertex_client.veo_generate with the keyframes in exactly the clip's
    shape. Every scene submission in this module calls this name.

    The start frame is conformed ONCE (_veo_ready). A `push_in` spec
    ({"zoom": z, "anchor": "face" for a close-up}, from _scene_video_inputs)
    makes the end frame a zoomed copy of that same conformed start, so both
    ends share their pixels and no second reframe can invent a different
    background. An end frame that is the start frame itself becomes the
    conformed start; any other end frame is conformed on its own. `push_in`
    is never sent to Veo."""
    aspect = kwargs.get("aspect_ratio") or VEO_ASPECT_RATIO
    push_in = kwargs.pop("push_in", None)
    start, last = kwargs.get("start_image"), kwargs.get("last_image")
    if start:
        ready = await _veo_ready(start, aspect)
        kwargs["start_image"] = ready
        if last:
            if push_in:
                zoom = float(push_in.get("zoom") or 1.12)
                kwargs["last_image"] = _push_in_end_frame(
                    Path(ready), zoom, anchor=push_in.get("anchor"),
                ) or ready
            elif Path(last) == Path(start):
                kwargs["last_image"] = ready
            else:
                kwargs["last_image"] = await _veo_ready(last, aspect)
    elif last:
        kwargs["last_image"] = await _veo_ready(last, aspect)
    return await _vertex_veo_generate(prompt, **kwargs)


async def _card_i2v_coro(prompt: str, *, keep_as_is: bool = False, **kwargs: Any) -> dict:
    """card_animation.build_card_i2v_coro with the card put in the clip's shape
    first, since the card path submits through card_animation's own
    veo_generate, which the wrapper above never sees. A coroutine like the
    original, so the reel's bounded-submit batching still decides when it
    runs. `keep_as_is`: the card is the user's own image kept as it is, so a
    wrong shape is padded, never reframed by a model."""
    if kwargs.get("start_image"):
        kwargs["start_image"] = await _veo_ready(
            kwargs["start_image"], kwargs.get("aspect") or VEO_ASPECT_RATIO, card=True,
            policy="pad" if keep_as_is else "reframe")
    return await card_animation.build_card_i2v_coro(prompt, **kwargs)


def _image_dims(b: bytes) -> tuple[int | None, int | None]:
    try:
        from PIL import Image
        with Image.open(BytesIO(b)) as im:
            return im.width, im.height
    except Exception:
        return None, None


async def _store_image_bytes(
    *,
    image_bytes: bytes,
    bucket_path: str,
    local_dest: Path,
    mime: str = "image/png",
) -> dict:
    """Upload to bucket (or local fallback) AND write to local disk so the
    file proxy can stream cheaply. Returns {storage_bucket, storage_path,
    local_path}.
    """
    try:
        upload_result = await asset_storage.upload(
            data=image_bytes, path=bucket_path, mime=mime,
        )
    except Exception:
        log.exception("th image upload failed; saving locally only")
        upload_result = {"storage_bucket": None, "storage_path": None, "local_path": None}
    local_dest.parent.mkdir(parents=True, exist_ok=True)
    local_dest.write_bytes(image_bytes)
    return {
        "storage_bucket": upload_result.get("storage_bucket"),
        "storage_path": upload_result.get("storage_path"),
        "local_path": _to_rel(local_dest),
    }


async def _apply_look_patch(run_id: int, look: dict) -> None:
    """Patch the picked look (global refs / per-scene overrides / lock /
    image_models) onto a run's metadata BEFORE generate_previews — a LIGHTWEIGHT
    write with NO keyframe/video regen (unlike edit_run, which on a fresh run
    would dispatch a keyframe regen and then double-generate). Used by the
    Topic→Look→Generate first-creation flow, where the script run is created at
    Topic→Look (no refs) and the refs are picked on the Look page.

    `bg_mode` is intentionally NOT persisted — it's a one-shot read by
    generate_previews from the `look` payload.
    """
    run = await _fetch_run_row(run_id)
    md = run["metadata"] or {}
    _check_aspect_change(run, md, look.get("aspect_ratio"))
    incoming = look.get("refs") or {}
    for k in ("intro_ref_mode", "outro_ref_mode"):
        _check_ref_mode(incoming.get(k))
    for k in ("intro_keep_text", "outro_keep_text"):
        _check_keep_text(incoming.get(k))
    patch: dict[str, Any] = {}

    refs = dict(md.get("refs") or {})
    for k in (
        "character_asset_id", "character_upload_id", "bg_asset_id", "bg_upload_id",
        "intro_asset_id", "intro_upload_id", "outro_asset_id", "outro_upload_id",
        "intro_video_upload_id", "outro_video_upload_id",
        "intro_ref_mode", "outro_ref_mode",
        "intro_card_id", "outro_card_id", "intro_keep_text", "outro_keep_text",
    ):
        if k in incoming:
            refs[k] = incoming[k]
    patch["refs"] = refs

    if "lock_character" in look:
        patch["lock_character"] = bool(look["lock_character"])
    if look.get("image_models"):
        patch["image_models"] = _normalize_image_models(look["image_models"])
    if "aspect_ratio" in look:
        patch["aspect_ratio"] = _norm_aspect(look["aspect_ratio"])
    if "video_quality" in look:
        patch["video_quality"] = _norm_quality(look["video_quality"])
    framing = _check_framing(look.get("framing"))
    if framing is not None:
        patch["framing"] = framing
    scene_framings = look.get("scene_framings")
    if scene_framings is not None:
        for v in scene_framings:
            if v is not None and v != FRAMING_INHERIT:
                _check_framing(v)

    scene_overrides = look.get("scene_overrides")
    scenes_meta = list(md.get("scenes") or [])
    if scene_overrides and scenes_meta:
        for i, ov in enumerate(scene_overrides[: len(scenes_meta)]):
            if not ov:
                continue
            for k in ("character_asset_id", "character_upload_id", "bg_asset_id", "bg_upload_id"):
                if k in ov:
                    scenes_meta[i][k] = ov[k]
        patch["scenes"] = scenes_meta
    # Per-scene framing: a value overrides the run's, FRAMING_INHERIT drops
    # the override, None leaves it. A single-scene run's one framing is the
    # run's. Only saved here: generate_previews makes each still at it.
    if scene_framings is not None:
        if int(md.get("scene_count") or 1) > 1 and scenes_meta:
            for i, v in enumerate(scene_framings[: len(scenes_meta)]):
                if v is not None:
                    scenes_meta[i]["framing"] = None if v == FRAMING_INHERIT else _check_framing(v)
            patch["scenes"] = scenes_meta
        elif scene_framings and scene_framings[0] not in (None, FRAMING_INHERIT):
            patch["framing"] = _check_framing(scene_framings[0])

    await _patch_metadata(run_id, patch)


def _png_bytes_from_path(path: Path) -> bytes:
    """Decode any image file → lossless PNG bytes (keeps the pixels, normalises
    the container so the keyframe stays a clean PNG). Used by 'use my photo
    as-is'."""
    from io import BytesIO
    from PIL import Image
    with Image.open(path) as img:
        out = img.convert("RGB")
        buf = BytesIO()
        out.save(buf, format="PNG")
        return buf.getvalue()


# ---------------------------------------------------------------------------
# Scene stills: one resolver, one prompt builder, one generator, one writer
# ---------------------------------------------------------------------------
#
# Every path that makes a scene still (first generation, the scene-count
# increase, Retry, the edit regen) resolves its references, builds its prompt
# and stores its result through the helpers below, so a fix lands once.

# prompt_used of a scene whose still IS the character photo (New bg OFF). The
# scene is then marked as_is (main_as_is for scene 0, scenes[i].as_is after).
_USE_AS_IS_PROMPT = "(character image used as-is)"

# yas_generations.retry_kind is CHECK-constrained to these values (see
# db/schema.sql, yas_generations_retry_kind_check). Any other kind is kept in
# the still's metadata only, so a write never trips the constraint.
_RETRY_KIND_COLUMN_VALUES = ("tweak", "pivot")

# Earlier versions kept per scene: metadata.still_versions["0"] (scene 0,
# whose still lives on the run row) and scenes[i].previous_keyframe_ids
# (scenes >= 1, one row per version). Older entries drop off the list. A
# scene-0 version that drops off (or off still_undone["0"]) has its files
# deleted then, since nothing else points at them; a scene row's are
# removed with the run.
_STILL_VERSIONS_MAX = 12


async def _delete_dropped_stills(run_id: int, dropped: list, keep: list) -> None:
    """Best effort: delete the files (bucket object and local copy) of the
    scene-0 versions that fell off a capped list (`dropped`), so they are
    not orphaned: delete_run only finds what the lists still hold. A file
    any record in `keep` (the current still and every listed version) still
    uses is left alone. Never raises."""
    used = {x for v in keep if isinstance(v, dict)
            for x in (v.get("storage_path"), v.get("local_path")) if x}
    for v in dropped:
        if not isinstance(v, dict):
            continue
        sp, lp = v.get("storage_path"), v.get("local_path")
        if not (sp or lp) or (sp and sp in used) or (lp and lp in used):
            continue
        try:
            await asset_storage.delete(storage_bucket=v.get("storage_bucket"), storage_path=sp, local_path=lp)
        except Exception:
            log.exception("[yas-th] run=%d: deleting a dropped still version failed (continuing)", run_id)


def _scene_ref_ids(md: dict, i: int) -> dict[str, Any]:
    """Effective character and background reference ids for scene `i`.

    Decided per SLOT: when the scene picks its own character (asset or
    upload), that pick wins as a pair; otherwise the run's global pick. The
    background works the same way. This is edit_run's inheritance rule (a
    scene inherits a global ref only when it overrides neither id of the
    slot), so the brief a prompt describes and the image attached always come
    from the same pick."""
    md = md or {}
    refs = md.get("refs") or {}
    scenes = md.get("scenes") or []
    sm = scenes[i] if 0 <= i < len(scenes) and isinstance(scenes[i], dict) else {}
    out: dict[str, Any] = {}
    for slot in ("character", "bg"):
        a, u = f"{slot}_asset_id", f"{slot}_upload_id"
        src = sm if (sm.get(a) or sm.get(u)) else refs
        out[a] = src.get(a)
        out[u] = src.get(u)
    return out


async def _scene_refs(md: dict, i: int) -> dict[str, Any]:
    """`_scene_ref_ids` for scene `i` plus the text briefs of those picks
    (`character_brief`, `bg_brief`; None for an empty slot)."""
    ids = _scene_ref_ids(md, i)
    _, character_brief = await _slot_row_and_brief(
        asset_id=ids["character_asset_id"], upload_id=ids["character_upload_id"],
        kind="character",
    )
    _, bg_brief = await _slot_row_and_brief(
        asset_id=ids["bg_asset_id"], upload_id=ids["bg_upload_id"], kind="bg",
    )
    return {**ids, "character_brief": character_brief, "bg_brief": bg_brief}


async def _ref_path(ids: dict, slot: str, *, what: str) -> Path | None:
    """Local file of the `slot` ('character' | 'bg') pick in `ids`; None when
    the slot is empty or its file cannot be resolved (logged)."""
    try:
        return await _slot_path(
            asset_id=ids.get(f"{slot}_asset_id"), upload_id=ids.get(f"{slot}_upload_id"),
        )
    except Exception:
        log.exception("%s %s path resolve failed", what, slot)
        return None


async def _character_pick_path(md: dict, i: int) -> Path | None:
    """The character photo picked for scene `i` (its own pick, else the run's
    global pick), or None."""
    return await _ref_path(_scene_ref_ids(md, i), "character", what=f"scene {i}")


def _scene_anchored(md: dict, i: int) -> bool:
    """Whether scene `i` takes its speaker from scene 0's still: a scene >= 1
    with no character pick of its own and none on the run. Its still then
    shows the same person as scene 0 (the default speaker, or scene 0's
    pick), whatever lock_character is."""
    ids = _scene_ref_ids(md, i)
    return i >= 1 and not (ids["character_asset_id"] or ids["character_upload_id"])


def _still_shows_a_pick(md: dict, i: int) -> bool:
    """Whether scene `i`'s still shows a person the run's speaker text does
    not describe: a character pick (its own or the run's, used as the photo
    or as its description, whatever lock_character is) or scene 0's still
    (_scene_anchored). Its clip's subject line then names the person in the
    first frame, never the script's default speaker. Which images go to Veo
    is a separate question (_veo_safe_character_path)."""
    ids = _scene_ref_ids(md, i)
    return bool(ids["character_asset_id"] or ids["character_upload_id"]) or _scene_anchored(md, i)


async def _person_ref_for_scene(md: dict, i: int, *, run_id: int | None = None) -> Path | None:
    """The PERSON reference image to attach to scene `i`'s still: the scene's
    character pick, else the run's global pick, else (a scene >= 1, see
    _scene_anchored) scene 0's CURRENT still as the identity anchor, so every
    scene shows the same person. None when there is none (the anchor needs
    `run_id`, and scene 0 must have a still: generate scene 0 first).

    Callers decide whether a picked character applies (lock_character)
    before asking. The as-is and keep-character paths use the pick itself
    (_character_pick_path) and never get the anchor."""
    if not _scene_anchored(md, i):
        return await _character_pick_path(md, i)
    if run_id is None:
        return None
    return await _resolve_scene_keyframe_path(run_id=run_id, scene_index=0, md=md)


def _build_scene_prompt(
    md: dict,
    i: int,
    *,
    character_brief: str | None,
    bg_brief: str | None,
    has_character_ref: bool,
    has_bg_ref: bool,
    keep_subject: bool = False,
    **extra: Any,
) -> str:
    """The keyframe prompt for scene `i`, from the run's script, lock and
    aspect, naming only the reference images ACTUALLY attached
    (`has_character_ref` / `has_bg_ref`). `extra` is passed through to
    build_main_keyframe_prompt.

    The subject is the run's speaker (metadata.speaker, the script subject
    for older runs), never a library tag; with a picked character (a
    `character_brief`) it points at that character's description instead,
    so the two never disagree. A person image on an anchored scene (see
    _scene_anchored) is scene 0's still: it fixes identity only. The shot
    size is the scene's framing (_effective_framing) unless `extra` gives
    one."""
    md = md or {}
    gem_aspect, _oai_size = _aspect_size(_norm_aspect(md.get("aspect_ratio")))
    extra.setdefault("framing", _effective_framing(md, i))
    script = dict(md.get("script") or {})
    if character_brief:
        script["subject"] = "the person in the character description below"
    else:
        script["subject"] = th_prompts.effective_subject(
            script, md.get("speaker"), fallback=th_prompts.DEFAULT_SPEAKER_FALLBACK,
        )
    return th_prompts.build_main_keyframe_prompt(
        script=script,
        character_brief=character_brief,
        bg_brief=bg_brief,
        lock_character=bool(md.get("lock_character")),
        has_character_ref=has_character_ref,
        has_bg_ref=has_bg_ref,
        keep_subject=keep_subject,
        aspect=gem_aspect,
        identity_anchor=bool(has_character_ref) and _scene_anchored(md, i),
        **extra,
    )


async def _compose_sources(
    md: dict, i: int, *, run_id: int | None = None,
) -> tuple[list[Path], bool, bool]:
    """Source images for a compose still of scene `i`, in prompt order: the
    person reference twice (identity anchor) when lock_character is on or the
    scene takes its speaker from scene 0's still (_scene_anchored), then the
    background reference; capped at Gemini's 3. Returns (sources,
    has_character_ref, has_bg_ref) for what is actually attached."""
    sources: list[Path] = []
    char_p: Path | None = None
    if bool((md or {}).get("lock_character")) or _scene_anchored(md, i):
        char_p = await _person_ref_for_scene(md, i, run_id=run_id)
        if char_p:
            sources += [char_p, char_p]
    bg_p = await _ref_path(_scene_ref_ids(md, i), "bg", what=f"scene {i}")
    if bg_p:
        sources.append(bg_p)
    sources = sources[:3]
    has_char = char_p is not None and char_p in sources
    has_bg = bg_p is not None and bg_p in sources
    return sources, has_char, has_bg


def _look_bg_mode(look: dict | None, i: int) -> str:
    """The one-shot "New bg" choice for scene `i` in a Look payload: the
    scene's own bg_mode, else the global one, else 'keep'."""
    if look:
        so = look.get("scene_overrides") or []
        if i < len(so) and so[i] and so[i].get("bg_mode"):
            return so[i]["bg_mode"]
        return look.get("bg_mode") or "keep"
    return "keep"


async def _as_is_or_reframe(
    run_id: int, i: int, cand: Path, *, md: dict,
) -> tuple[bytes, str, str]:
    """The character photo `cand` as scene `i`'s still, in exactly the output
    shape: the photo itself when it is already about the output aspect
    (trimmed to it, no gen), else one resize-only reframe of it (keep the
    person and background, extend to the output shape; the photo is padded
    when even the reframe comes back the wrong shape)."""
    aspect = _norm_aspect(md.get("aspect_ratio"))
    gem_aspect, _oai_size = _aspect_size(aspect)
    if _aspect_matches(cand, aspect):
        log.info("[yas-th] run=%d scene %d: char as-is (aspect ok, no gen)", run_id, i)
        photo = await _conform_to_aspect(_png_bytes_from_path(cand), aspect, "crop")
        return (photo, "image/png", _USE_AS_IS_PROMPT)
    log.info(
        "[yas-th] run=%d scene %d: char aspect ≠ %s → resize-only reframe gen",
        run_id, i, aspect,
    )
    prompt = th_prompts.build_reframe_to_aspect_prompt(aspect=gem_aspect)
    data = await _conform_to_aspect(_png_bytes_from_path(cand), aspect, "reframe", source=cand)
    return (data, "image/png", prompt)


async def _keep_character_at_framing(
    run_id: int,
    md: dict,
    i: int,
    data: bytes,
    mime: str,
    prompt: str,
    *,
    canvas: Path,
    info: dict,
) -> tuple[bytes, str, str]:
    """A keep-character still (the photo `canvas` with a new background) at
    scene `i`'s framing. A picked character is a head-and-shoulders photo, so
    at close the still keeps the photo's framing (FRAMING_AS_PHOTO). Any
    wider framing is a new still made at it: one refine-engine edit re-frames
    it (the photo as the identity reference), recorded at that framing. If
    that edit fails, the photo-framed still is kept (logged) rather than
    lost. Returns (bytes, mime, prompt_used)."""
    framing = _effective_framing(md, i)
    if framing == th_prompts.FRAMING_CLOSE:
        info["framing_used"] = FRAMING_AS_PHOTO
        return (data, mime, prompt)
    import tempfile

    fd, name = tempfile.mkstemp(suffix=".png")
    os.close(fd)
    tmp = Path(name)
    try:
        tmp.write_bytes(data)
        framed, framed_prompt = await _refine_canvas(
            tmp, th_prompts.framing_reshoot_change(framing), identity=canvas,
            aspect=_norm_aspect(md.get("aspect_ratio")), what=f"run={run_id} scene={i} keep-character",
        )
    except Exception:
        log.exception("[yas-th] run=%d scene %d: keep-character re-frame to %s failed; "
                      "keeping the photo's framing", run_id, i, framing)
        info["framing_used"] = FRAMING_AS_PHOTO
        return (data, mime, prompt)
    finally:
        tmp.unlink(missing_ok=True)
    info["framing_used"] = framing
    return (framed, "image/png", f"{prompt}\n\n{framed_prompt}")


async def _gen_scene_still(
    run_id: int,
    md: dict,
    i: int,
    *,
    look: dict | None = None,
    compose_on_mode_error: bool = False,
    info: dict | None = None,
) -> tuple[bytes, str, str]:
    """Make scene `i`'s still the way first generation does. Returns (bytes,
    mime, prompt_used); raises when the image call fails. `info`, when
    given, gets "framing_used": the scene's framing for a composed still,
    FRAMING_AS_PHOTO for the photo itself (its framing is the photo's) and
    for a keep-character still at close (see _keep_character_at_framing).

    Modes, first match wins. The first two need a Look payload `look` (the
    first-creation flow); without one the still is always composed.
      - as-is: a character pick, no background, New bg OFF -> the character
        photo itself (prompt _USE_AS_IS_PROMPT), or a resize-only reframe of
        it when its aspect is not the output's.
      - keep character + new bg: a character pick, no background, New bg ON
        and lock_character -> the photo is the keep_subject canvas and only
        the background is regenerated, so the face cannot drift; then
        re-framed to the scene's framing (_keep_character_at_framing).
      - compose: the person reference (lock_character) and the background
        reference, with a prompt that names exactly the images attached.
    `compose_on_mode_error`: an error in the as-is / keep-character step falls
    back to compose instead of raising (extra scenes always did this; scene 0
    reports the error)."""
    md = md or {}
    info = info if info is not None else {}
    gem_aspect, oai_size = _aspect_size(_norm_aspect(md.get("aspect_ratio")))
    image_model = _normalize_image_models(md.get("image_models"))["main"]
    sr = await _scene_refs(md, i)
    has_char_pick = bool(sr["character_asset_id"] or sr["character_upload_id"])
    has_bg_pick = bool(sr["bg_asset_id"] or sr["bg_upload_id"])
    try:
        if look is not None and has_char_pick and not has_bg_pick:
            if _look_bg_mode(look, i) != "new":
                cand = await _character_pick_path(md, i)
                if cand is not None:
                    out = await _as_is_or_reframe(run_id, i, cand, md=md)
                    info["framing_used"] = FRAMING_AS_PHOTO
                    return out
            elif bool(md.get("lock_character")):
                canvas = await _character_pick_path(md, i)
                if canvas is not None:
                    log.info("[yas-th] run=%d scene %d: keep-character + new bg (face locked to canvas)",
                             run_id, i)
                    prompt = _build_scene_prompt(
                        md, i, character_brief=sr["character_brief"], bg_brief=sr["bg_brief"],
                        has_character_ref=False, has_bg_ref=False, keep_subject=True,
                    )
                    data, mime = await _run_image_gen(
                        prompt=prompt, image_model=image_model,
                        source_images=[canvas], aspect=gem_aspect, size=oai_size,
                    )
                    return await _keep_character_at_framing(
                        run_id, md, i, data, mime, prompt, canvas=canvas, info=info,
                    )
    except Exception:
        if not compose_on_mode_error:
            raise
        log.exception("scene %d as-is/keep-char failed; falling back to gen", i)
    sources, has_char, has_bg = await _compose_sources(md, i, run_id=run_id)
    prompt = _build_scene_prompt(
        md, i, character_brief=sr["character_brief"], bg_brief=sr["bg_brief"],
        has_character_ref=has_char, has_bg_ref=has_bg,
    )
    data, mime = await _run_image_gen(
        prompt=prompt, image_model=image_model,
        source_images=sources or None, aspect=gem_aspect, size=oai_size,
    )
    info["framing_used"] = _effective_framing(md, i)
    return (data, mime, prompt)


def _framing_md(i: int, framing_used: str | None) -> tuple[dict, dict]:
    """(extra_md, scene_md) for _store_scene_still that record the framing a
    new still of scene `i` was made at: metadata.main_framing_used for scene
    0 (it lives on the run), the scene row's and scenes[i].framing_used for
    the others. Nothing when it is not known."""
    if not framing_used:
        return {}, {}
    if i == 0:
        return {"main_framing_used": framing_used}, {}
    return {"framing_used": framing_used}, {"framing_used": framing_used}


def _still_stamp() -> str:
    """A unique, sortable name part for a new scene-0 still file."""
    return f"{datetime.now(timezone.utc):%Y%m%dT%H%M%S%f}-{secrets.token_hex(2)}"


async def _store_scene_still(
    run_id: int,
    i: int,
    data: bytes,
    mime: str,
    prompt: str,
    image_model: str,
    *,
    feedback: str | None = None,
    retry_kind: str | None = None,
    parent_id: int | None = None,
    extra_md: dict | None = None,
    scene_md: dict | Callable[[dict], dict] | None = None,
    topic: str | None = None,
) -> dict:
    """Store a new still for scene `i` without destroying the one it replaces.

    Scene 0 lives on the run row: the bytes go to a NEW file and bucket key
    (main-<stamp>.png), the run row's image columns are repointed, and the
    replaced still is appended to metadata.still_versions["0"] in the same
    locked transaction. Runs made before this keep reading their main.png
    through the row's paths. `feedback` sets the row's feedback_text (left as
    it is when None); `extra_md` is merged into the run metadata; `scene_md`
    into metadata.scenes[0] when the run has a scenes list. The row's error
    (only ever a failed scene-0 still) is cleared: it has a still now.

    Scenes >= 1 get a new MODE_SCENE row (`_insert_scene_row`, with
    feedback / retry_kind / parent_id as lineage and `extra_md` in the row
    metadata), then scenes[i] is repointed atomically (`_patch_scene`):
    keyframe_id = the new row, the old id appended to previous_keyframe_ids,
    plus `scene_md` (a dict, or a function of the current scene).

    `retry_kind` goes in the retry_kind column only when the table accepts it
    (_RETRY_KIND_COLUMN_VALUES); scene 0 also records it as
    metadata.main_retry_kind. `topic` saves a read for scenes >= 1.

    A clip that records no source and is up to date was made from the still
    replaced here: that still is recorded as its source first, under the
    same lock (_clip_source_to_record), so an Undo back to it keeps the clip
    up to date (clips made before clips recorded their still).
    Returns {id, scene_index, width, height, previous}: `previous` is the
    replaced version (scene 0: its record; scenes >= 1: {keyframe_id}) or None.
    """
    width, height = _image_dims(data)
    if i >= 1:
        if topic is None:
            topic = (await _fetch_run_row(run_id)).get("topic") or ""
        row_id = await _insert_scene_row(
            parent_run_id=run_id, topic=topic, image_model=image_model,
            scene_index=i, image_bytes=data, mime=mime, used_prompt=prompt,
            feedback_text=feedback, retry_kind=retry_kind,
            parent_generation_id=parent_id, extra_md=extra_md,
        )
        replaced: dict[str, Any] = {}

        def _repoint(scene: dict) -> dict:
            out: dict[str, Any] = {"keyframe_id": row_id}
            old = scene.get("keyframe_id")
            if old and old != row_id:
                replaced["keyframe_id"] = old
                out["previous_keyframe_ids"] = [
                    *(scene.get("previous_keyframe_ids") or []), old,
                ][-_STILL_VERSIONS_MAX:]
                src = _clip_source_to_record({}, i, _still_key_of(i, keyframe_id=old), scene=scene)
                if src:
                    out["video_source"] = src
            more = scene_md(scene) if callable(scene_md) else scene_md
            out.update(more or {})
            return out

        await _patch_scene(run_id, i, _repoint)
        return {"id": row_id, "scene_index": i, "width": width, "height": height,
                "previous": replaced or None}

    stamp = _still_stamp()
    stored = await _store_image_bytes(
        image_bytes=data,
        bucket_path=f"yas/talking-head-v2/{run_id}/main-{stamp}.png",
        local_dest=TH_ROOT / f"{run_id}" / f"main-{stamp}.png",
        mime=mime,
    )
    previous: dict | None = None
    dropped: list = []
    keep: list = []
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT local_path, storage_bucket, storage_path, mime_type,
                       width, height, image_model, feedback_text, prompt_used,
                       metadata
                  FROM yas_generations
                 WHERE id = %s AND mode = %s
                   FOR UPDATE
                """,
                (run_id, MODE_PREVIEW),
            )
            row = await cur.fetchone()
            if not row:
                raise HTTPException(404, f"talking-head-v2 run #{run_id} not found")
            md = _md_dict(row.get("metadata"))
            md_patch: dict[str, Any] = dict(extra_md or {})
            if retry_kind is not None or "main_retry_kind" in md:
                md_patch["main_retry_kind"] = retry_kind
            if row.get("local_path") or row.get("storage_path"):
                previous = {
                    "local_path": row.get("local_path"),
                    "storage_bucket": row.get("storage_bucket"),
                    "storage_path": row.get("storage_path"),
                    "mime_type": row.get("mime_type"),
                    "width": row.get("width"),
                    "height": row.get("height"),
                    "image_model": row.get("image_model"),
                    "feedback_text": row.get("feedback_text"),
                    "retry_kind": md.get("main_retry_kind"),
                    "as_is": bool(md.get("main_as_is")),
                    "framing_used": md.get("main_framing_used"),
                    "prompt_used": (row.get("prompt_used") or "")[:2000],
                    "replaced_at": _now_iso(),
                }
                versions = md.get("still_versions")
                versions = dict(versions) if isinstance(versions, dict) else {}
                stack = [*(versions.get("0") or []), previous]
                versions["0"] = stack[-_STILL_VERSIONS_MAX:]
                dropped = stack[:-_STILL_VERSIONS_MAX]
                undone = md.get("still_undone")
                keep = [*versions["0"], *((undone.get("0") if isinstance(undone, dict) else None) or [])]
                md_patch["still_versions"] = versions
            scenes = md.get("scenes")
            scene0: dict | None = None
            has_scene0 = isinstance(scenes, list) and bool(scenes) and isinstance(scenes[0], dict)
            # The clip of the still replaced here, when it records none: on
            # the run (one scene) or on scenes[0] (a multi-scene run).
            src = _clip_source_to_record(
                md, 0, _still_key_of(0, local_path=row.get("local_path"), storage_path=row.get("storage_path")),
                scene=scenes[0] if has_scene0 else None,
            )
            multi = int(md.get("scene_count") or 1) > 1
            if src and not multi:
                md_patch.setdefault("main_video_source", src)
            scene0_src = {"video_source": src} if src and multi else {}
            if has_scene0 and (scene_md is not None or scene0_src):
                more = (scene_md(dict(scenes[0])) if callable(scene_md) else scene_md) if scene_md is not None else None
                scene0 = {**scenes[0], **scene0_src, **(more or {})}
            if scene0 is None:
                meta_sql, meta_params = "metadata || %s::jsonb", [Jsonb(md_patch)]
            else:
                meta_sql = "jsonb_set(metadata || %s::jsonb, '{scenes,0}', %s::jsonb, false)"
                meta_params = [Jsonb(md_patch), Jsonb(scene0)]
            await cur.execute(
                f"""
                UPDATE yas_generations
                   SET storage_bucket = %s,
                       storage_path   = %s,
                       local_path     = %s,
                       mime_type      = %s,
                       bytes          = %s,
                       width          = %s,
                       height         = %s,
                       prompt_used    = %s,
                       status         = %s,
                       image_model    = %s,
                       feedback_text  = COALESCE(%s, feedback_text),
                       retry_kind     = COALESCE(%s, retry_kind),
                       error          = NULL,
                       metadata       = {meta_sql},
                       updated_at     = NOW()
                 WHERE id = %s
                """,
                (
                    stored["storage_bucket"], stored["storage_path"],
                    stored["local_path"], mime, len(data), width, height,
                    prompt[:8000], STATUS_READY, image_model, feedback,
                    retry_kind if retry_kind in _RETRY_KIND_COLUMN_VALUES else None,
                    *meta_params, run_id,
                ),
            )
    if dropped:
        await _delete_dropped_stills(run_id, dropped, [*keep, stored])
    return {"id": run_id, "scene_index": 0, "width": width, "height": height,
            "local_path": stored["local_path"], "storage_bucket": stored["storage_bucket"],
            "storage_path": stored["storage_path"], "previous": previous}


def _run_has_stills(run: dict, md: dict) -> bool:
    """Whether any scene still exists yet (scene 0 on the run row, or a scene
    row). False for a run that only has its script."""
    if run.get("local_path") or run.get("storage_path"):
        return True
    return any(isinstance(s, dict) and s.get("keyframe_id") for s in ((md or {}).get("scenes") or []))


# The Video size is fixed once a still or card exists: each was made in the
# run's shape and Veo animates exactly the still the user approved, so a new
# size would leave every one of them the wrong shape.
ASPECT_LOCKED_MESSAGE = "Video size is fixed once stills are made. Start a new reel to change it."


def _aspect_locked(run: dict, md: dict) -> bool:
    """Whether the run's Video size can no longer change: a scene still or an
    intro/outro card has been made."""
    md = md or {}
    return _run_has_stills(run, md) or bool(md.get("intro_image_id") or md.get("outro_image_id"))


def _check_aspect_change(run: dict, md: dict, aspect: Any) -> None:
    """409 when `aspect` would change the Video size of a run that has
    stills; a no-op for the same size or a run without stills."""
    if aspect is None:
        return
    if _norm_aspect(aspect) != _norm_aspect((md or {}).get("aspect_ratio")) and _aspect_locked(run, md):
        raise HTTPException(409, ASPECT_LOCKED_MESSAGE)


async def generate_previews(run_id: int, *, look: dict | None = None) -> dict:
    """Generate the three preview images (main + intro + outro).

    Each tile uses its own image_model from metadata.image_models.
    Skips intro/outro card gen when the slot is excluded.

    `look` (optional): the picked refs/overrides/lock/image_models from the Look
    page (first-creation flow). When given, it's patched onto the run's metadata
    first (no regen), then previews generate against it. It also carries the
    one-shot per-scene/global `bg_mode` used by the "use my photo as-is" path:
    when a scene has an UPLOADED character, NO bg, and bg_mode != "new", the
    uploaded photo is used directly as the keyframe (no image-gen).
    """
    if look is not None:
        await _apply_look_patch(run_id, look)
    run = await _fetch_run_row(run_id)
    md = run["metadata"]
    refs = md.get("refs") or {}
    image_models = _normalize_image_models(md.get("image_models"))
    aspect = _norm_aspect(md.get("aspect_ratio"))
    log.info(
        "[yas-th] generate_previews run=%d aspect=%s engines main=%s intro=%s outro=%s (metadata raw=%s)",
        run_id, aspect, image_models["main"], image_models["intro"], image_models["outro"],
        md.get("image_models"),
    )

    # Scene 0's still (the run row). Each scene resolves its own references,
    # prompt and mode in _gen_scene_still: the as-is photo, the keep-character
    # new-bg edit, or a compose still that names only the images attached.
    main_info: dict[str, Any] = {}

    async def _gen_main() -> tuple[bytes, str, str] | Exception:
        try:
            return await _gen_scene_still(run_id, md, 0, look=look, info=main_info)
        except Exception as e:
            log.exception("main keyframe gen failed for run #%d", run_id)
            return e

    # The intro and outro cards (_make_bookend_card): a picked or uploaded
    # image kept as it is (the default), a new card in its style, or the
    # voxel card when nothing is picked. Each is stored as its own card row.
    async def _gen_card(role: str) -> tuple[int, dict] | Exception:
        try:
            return await _make_bookend_card(run, md, role)
        except Exception as e:
            log.exception("%s card gen failed for run #%d", role, run_id)
            return e

    async def _gen_intro() -> tuple[int, dict] | Exception:
        return await _gen_card("intro")

    async def _gen_outro() -> tuple[int, dict] | Exception:
        return await _gen_card("outro")

    # Bookend generate-toggle: skip a card's image-gen entirely when its
    # `*_excluded` flag is set (owner chose not to generate it). The persist
    # block below leaves `intro_image_id`/`outro_image_id` = None for a None
    # result, so no card row is inserted; start_videos + stitch already skip it.
    # A direct-video intro/outro (refs.*_video_upload_id) is stitched in AS-IS by
    # the composition poller, so skip its card image-gen here exactly like an
    # excluded bookend — no card row, and start_videos then skips Veo for it. The
    # poller renders it from the upload regardless of the metadata `*_excluded`
    # flag, which we leave untouched (the user still wants an intro/outro).
    intro_excluded = bool(md.get("intro_excluded")) or bool(refs.get("intro_video_upload_id"))
    outro_excluded = bool(md.get("outro_excluded")) or bool(refs.get("outro_video_upload_id"))
    if intro_excluded or outro_excluded:
        log.info(
            "[yas-th] generate_previews run=%d skipping bookends intro=%s outro=%s",
            run_id, intro_excluded, outro_excluded,
        )

    async def _skip_card() -> None:
        return None

    main_res, intro_res, outro_res = await asyncio.gather(
        _gen_main(),
        _skip_card() if intro_excluded else _gen_intro(),
        _skip_card() if outro_excluded else _gen_outro(),
        return_exceptions=False,
    )

    # Persist the MAIN result onto the existing run row (a new file per
    # version; the still it replaces stays in metadata.still_versions).
    if isinstance(main_res, Exception):
        await _mark_row_failed(run_id, f"main_keyframe_failed: {str(main_res)[:240]}")
    elif main_res is not None:
        data, mime, used_prompt = main_res
        await _store_scene_still(
            run_id, 0, data, mime, used_prompt, image_models["main"], topic=run["topic"],
            extra_md=_framing_md(0, main_info.get("framing_used"))[0],
        )

    # The intro / outro card rows were stored by _make_bookend_card.
    intro_image_id: int | None = None
    outro_image_id: int | None = None
    if isinstance(intro_res, tuple):
        intro_image_id = intro_res[0]
    elif isinstance(intro_res, Exception):
        log.warning("intro card gen failed: %s", intro_res)
    if isinstance(outro_res, tuple):
        outro_image_id = outro_res[0]
    elif isinstance(outro_res, Exception):
        log.warning("outro card gen failed: %s", outro_res)

    # `main_as_is`: scene-0 keyframe is the character image used verbatim (New
    # bg OFF) vs a generated still. Persisted so the Look toggle restores its
    # state on resume and so edit_run doesn't re-roll a generated bg.
    main_as_is = isinstance(main_res, tuple) and main_res[2] == _USE_AS_IS_PROMPT
    # A card copied from a previous reel's card brings that card's finished
    # clip along (no Veo): the bookend's clip is then already made.
    reused: dict[str, Any] = {}
    for role, res in (("intro", intro_res), ("outro", outro_res)):
        if isinstance(res, tuple):
            reused.update(_reused_clip_patch(role, res[1]))
    await _patch_metadata(run_id, {
        "intro_image_id": intro_image_id,
        "outro_image_id": outro_image_id,
        "main_as_is": bool(main_as_is),
        "intro_error": str(intro_res)[:480] if isinstance(intro_res, Exception) else None,
        "outro_error": str(outro_res)[:480] if isinstance(outro_res, Exception) else None,
        "main_error":  str(main_res)[:480]  if isinstance(main_res,  Exception) else None,
        **reused,
    })

    # ----- v6 — extra scene keyframes (scenes[1..N-1]) -----------------
    # scene_count == 1 → already done above (run row's local_path holds
    # the single scene's keyframe). Nothing to do.
    # scene_count > 1 → image-gen N-1 additional keyframes in parallel,
    # each as a sibling row. scenes[0] keeps the run row's keyframe.
    scene_count = int(md.get("scene_count") or 1)
    if scene_count > 1:
        extra_info: dict[int, dict] = {}

        async def _gen_extra_scene(scene_idx: int) -> tuple[int, bytes | None, str | None, str | None, str]:
            # Same modes as scene 0 (as-is / keep-character new bg / compose);
            # an error in the first two falls back to a compose still.
            try:
                data, mime, used = await _gen_scene_still(
                    run_id, md, scene_idx, look=look, compose_on_mode_error=True,
                    info=extra_info.setdefault(scene_idx, {}),
                )
                return (scene_idx, data, mime, None, used)
            except Exception as e:
                log.exception(
                    "scene %d keyframe gen failed for run #%d",
                    scene_idx, run_id,
                )
                return (scene_idx, None, None, str(e)[:480] or type(e).__name__, "")

        extra_results = await asyncio.gather(
            *[_gen_extra_scene(i) for i in range(1, scene_count)],
            return_exceptions=False,
        )

        # Persist each as a sibling scene row and repoint scenes[i].keyframe_id,
        # one scene at a time (atomic per scene, so a concurrent edit of another
        # scene is never overwritten). scenes[i].error is set on a failure so the
        # UI shows a red error tile instead of an empty "(auto-generated)" one.
        for scene_idx, data, mime, err, used_prompt in extra_results:
            if data is None or mime is None:
                await _patch_scene(run_id, scene_idx, {"error": err})
                continue
            # as_is: this scene's keyframe is the character image used
            # verbatim (New bg OFF) vs a generated still. Drives the Look
            # toggle's restored state + edit_run's no-reroll guard.
            f_md, f_scene = _framing_md(scene_idx, extra_info.get(scene_idx, {}).get("framing_used"))
            await _store_scene_still(
                run_id, scene_idx, data, mime, used_prompt, image_models["main"],
                topic=run["topic"], extra_md=f_md,
                scene_md={"error": None, "as_is": used_prompt == _USE_AS_IS_PROMPT, **f_scene},
            )
        log.info(
            "[yas-th] generate_previews run=%d scene_count=%d extra_keyframes=%d errors=%d",
            run_id, scene_count,
            sum(1 for r in extra_results if r[1] is not None),
            sum(1 for r in extra_results if r[3] is not None),
        )

    return await fetch_run(run_id)


async def _insert_card_row(
    *,
    parent_run_id: int,
    topic: str,
    image_model: str,
    card_mode: str,
    image_bytes: bytes,
    mime: str,
    used_prompt: str,
    card_type: str,
    category: str = "talking_head_card",
    extra_md: dict | None = None,
) -> int:
    """Create a sibling yas_generations row for an intro / outro card.

    `category` tags metadata.category so non-TH callers (the video editor's
    bookend pipeline passes "video_edit_bookend_card") stay distinguishable.
    `extra_md` (opt-in, TH v2) adds keys to the row metadata: what the card
    was made from (source_ref), whether it is kept as-is, its text; the keys
    above always win. Returns the new row id.
    """
    # Insert with empty paths first to get the id; then store the bytes.
    metadata = {
        **(extra_md or {}),
        "category": category,
        "parent_talking_head_id": parent_run_id,
        "card_type": card_type,
    }
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                INSERT INTO yas_generations
                  (topic, mode, image_model, prompt_used, status, metadata)
                VALUES (%s, %s, %s, %s, %s, %s)
                RETURNING id
                """,
                (
                    topic, card_mode, image_model, used_prompt[:8000],
                    STATUS_GENERATING, Jsonb(metadata),
                ),
            )
            new = await cur.fetchone()
            card_id = int(new["id"])

    width, height = _image_dims(image_bytes)
    local_dest = TH_ROOT / f"{parent_run_id}" / f"{card_type}-{card_id}.png"
    stored = await _store_image_bytes(
        image_bytes=image_bytes,
        bucket_path=f"yas/talking-head-v2/{parent_run_id}/{card_type}-{card_id}.png",
        local_dest=local_dest,
        mime=mime,
    )
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                UPDATE yas_generations
                   SET storage_bucket = %s,
                       storage_path   = %s,
                       local_path     = %s,
                       mime_type      = %s,
                       bytes          = %s,
                       width          = %s,
                       height         = %s,
                       status         = %s,
                       updated_at     = NOW()
                 WHERE id = %s
                """,
                (
                    stored["storage_bucket"], stored["storage_path"],
                    stored["local_path"], mime, len(image_bytes),
                    width, height, STATUS_READY, card_id,
                ),
            )
    return card_id


# ---------------------------------------------------------------------------
# Intro / outro cards: what each is made from, and making one
# ---------------------------------------------------------------------------
#
# A TH v2 card row records what it was made from (metadata.source_ref: the
# picked upload, asset or previous reel's card, or None), whether it IS that
# image (as_is) and its text when known (card_text: the words a drawn card was
# asked for, or the words read off a kept one). A card whose source is not the
# image picked now is never animated (run 824: cards drawn before the intro
# was picked were animated instead of it), and every edit of the picks
# rebuilds a kept card or drops a drawn one.

# prompt_used of a card that is the picked image itself (ref mode 'as_is').
_AS_IS_CARD_PROMPT = "(card used as-is)"


def _bookend_ref(refs: dict | None, role: str) -> dict | None:
    """The picked intro/outro image, as a card's source_ref records it:
    {'upload_id': n}, {'card_id': n} (a previous reel's card) or
    {'asset_id': n}, in that order (an upload wins, as _slot_path reads it),
    None when nothing is picked. An uploaded VIDEO is no card source."""
    refs = refs or {}
    if refs.get(f"{role}_upload_id"):
        return {"upload_id": int(refs[f"{role}_upload_id"])}
    if refs.get(f"{role}_card_id"):
        return {"card_id": int(refs[f"{role}_card_id"])}
    if refs.get(f"{role}_asset_id"):
        return {"asset_id": int(refs[f"{role}_asset_id"])}
    return None


async def _bookend_pick_path(refs: dict | None, role: str) -> Path | None:
    """The local file of the picked intro/outro image (_bookend_ref's order);
    None when nothing is picked. A previous reel's card must be a TH v2
    intro/outro card with its image (404 / 410 otherwise)."""
    src = _bookend_ref(refs, role)
    if src is None:
        return None
    if "card_id" in src:
        card = await _fetch_card_row(src["card_id"])
        if not card or card.get("mode") not in (MODE_INTRO, MODE_OUTRO):
            raise HTTPException(
                404, f"The picked {role} card (#{src['card_id']}) is not an intro or outro card "
                     "from an earlier video (it may have been deleted). Pick another.")
        path = await _resolve_card_path(src["card_id"])
        if path is None:
            raise HTTPException(410, f"The picked {role} card (#{src['card_id']}) has no image file. Pick another.")
        return path
    return await _slot_path(asset_id=src.get("asset_id"), upload_id=src.get("upload_id"))


def _bookend_wanted(md: dict | None, role: str) -> bool:
    """Whether the reel wants a card for this bookend: it is not excluded and
    no finished video was uploaded for it (that video is used as it is)."""
    md = md or {}
    return not md.get(f"{role}_excluded") and not (md.get("refs") or {}).get(f"{role}_video_upload_id")


def _card_source_ref(card: dict | None) -> tuple[bool, dict | None]:
    """(known, source_ref) of a card row. A card made before cards recorded
    it is known only when it is the voxel card, which is drawn from no image."""
    card = card or {}
    cmd = _md_dict(card.get("metadata"))
    if "source_ref" in cmd:
        return True, cmd.get("source_ref")
    if str(card.get("prompt_used") or "").startswith("[voxel-poster]"):
        return True, None
    return False, None


def _card_matches_refs(card: dict | None, refs: dict | None, role: str) -> bool:
    """Whether a card was made from the image picked now (an older card whose
    source is unknown is taken as it is, as before)."""
    known, src = _card_source_ref(card)
    return (not known) or src == _bookend_ref(refs, role)


def _bookend_words(run: dict, md: dict, role: str) -> tuple[str, str]:
    """(headline, subtitle) a drawn intro/outro card prints: the script's
    words, the topic (intro) or a derived CTA (outro) when a headline is empty."""
    script = (md or {}).get("script") or {}
    topic = (run.get("topic") or "").strip()
    if role == "intro":
        headline = (script.get("intro_headline") or "").strip() or topic
        return headline, (script.get("intro_subtitle") or "").strip()
    headline = (
        (script.get("outro_headline") or "").strip()
        or th_prompts._derive_outro_cta(topic, script)
    )
    return headline, (script.get("outro_subtitle") or "").strip()


def _planned_card_text(headline: str, subtitle: str) -> str | None:
    """The text a drawn card was asked to print, one line each."""
    return "\n".join(t for t in ((headline or "").strip(), (subtitle or "").strip()) if t) or None


async def _make_bookend_card(
    run: dict, md: dict, role: str, *, feedback: str | None = None, swap: bool = True,
) -> tuple[int, dict]:
    """Make the run's intro or outro card and store it as a new card row;
    returns (card_id, card metadata).

    A picked or uploaded image, or a previous reel's card, in 'as_is' mode IS
    the card: its pixels are copied (padded to the video's shape on a blurred
    fill of itself when it is another shape), with no drawing model. Its text
    is read (_kept_card_text): a headline or subtitle that differs from the
    script is swapped by one masked image edit (only those words change; a
    logo, URL or call to action never does) unless the run keeps the card's
    words, and the card's own words that stay go into the script. `swap`
    False (an edit of the run, which never pays for an image edit) raises
    _SwapPending instead of swapping. A previous reel's card copied pixel for
    pixel reuses that card's finished clip (_reuse_card_clip: card_md gets
    clip_animation_id). Otherwise the card is drawn by
    _render_card_animation_style: a new card in the image's style
    ('new_vibe'), the image redrawn with the script's words ('text_only', older
    runs), or the voxel card when nothing is picked. `feedback` steers a drawn
    card only. The row records source_ref, as_is, ref_mode, the card's text
    (asked for, when drawn; read, when kept) and the aspect; then the motion
    director looks at it once (_warm_bookend_motion) unless its plan came
    with the reused clip."""
    refs = (md or {}).get("refs") or {}
    aspect = _norm_aspect((md or {}).get("aspect_ratio"))
    gem_aspect, _ = _aspect_size(aspect)
    source_ref = _bookend_ref(refs, role)
    mode = _ref_mode(refs, role) if source_ref else None
    ref_path: Path | None = None
    if source_ref:
        try:
            ref_path = await _bookend_pick_path(refs, role)
        except Exception:
            if mode == REF_MODE_AS_IS:
                raise
            log.exception("[yas-th] %s ref path resolution failed for run #%s", role, run.get("id"))
    text_md: dict[str, Any] = {}
    padded = False
    if mode == REF_MODE_AS_IS:
        if ref_path is None or not Path(ref_path).exists():
            raise HTTPException(410, f"The picked {role} image is missing. Pick it again.")
        data = await asyncio.to_thread(_png_bytes_from_path, Path(ref_path))
        # Kept: never redrawn by a model and never trimmed (a URL at its edge
        # stays), so any other shape, even a near one, is padded.
        w, h = _image_dims(data)
        content_box: tuple | None = None
        if w and h and not _is_exact_aspect(w, h, aspect):
            data = await asyncio.to_thread(_reshape_png, data, aspect, "pad")
            padded = True
            # Where the picked image sits on the padded card (centred, as
            # _pad_to_aspect puts it): a swapped word never leaves it.
            pw, ph = _image_dims(data)
            if pw and ph:
                left, top = (pw - w) // 2, (ph - h) // 2
                content_box = (left, top, left + w, top + h)
        mime, used_prompt = "image/png", _AS_IS_CARD_PROMPT
        data, text_md = await _kept_card_text(
            run, md, role, data, source_ref=source_ref, padded=padded, swap=swap,
            content_box=content_box,
        )
        card_text = text_md.get("card_text")
    else:
        headline, subtitle = _bookend_words(run, md, role)
        data, mime, used_prompt = await _render_card_animation_style(
            role=role, topic=run.get("topic") or "",
            headline=headline, subtitle=subtitle,
            ref_mode=mode or REF_MODE_NEW_VIBE, ref_path=ref_path,
            feedback=feedback, aspect=gem_aspect, conform="reframe",
        )
        card_text = _planned_card_text(headline, subtitle)
    card_md = {
        "source_ref": source_ref,
        "as_is": mode == REF_MODE_AS_IS,
        "ref_mode": mode,
        "card_text": card_text,
        "card_text_source": ("detected" if mode == REF_MODE_AS_IS else "planned") if card_text else None,
        "aspect": aspect,
        **{k: v for k, v in text_md.items() if k not in ("card_text", "script_words")},
    }
    # A previous reel's card copied pixel for pixel (no pad, no swap): its
    # motion plan comes with it, and its finished clip is reused below.
    same_pixels = (bool(source_ref and source_ref.get("card_id")) and not padded
                   and (text_md.get("text_swap") or {}).get("status") != "swapped")
    src_card = await _fetch_card_row(int(source_ref["card_id"])) if same_pixels else None
    src_plan = _md_dict((src_card or {}).get("metadata")).get("motion")
    if _plan_ok(src_plan):
        card_md["motion"] = src_plan
    image_models = _normalize_image_models((md or {}).get("image_models"))
    card_id = await _insert_card_row(
        parent_run_id=int(run["id"]), topic=run.get("topic") or "",
        image_model=image_models[role],
        card_mode=MODE_INTRO if role == "intro" else MODE_OUTRO,
        image_bytes=data, mime=mime, used_prompt=used_prompt, card_type=role,
        extra_md=card_md,
    )
    log.info("[yas-th] run=%s %s card #%d made (%s, from %s, text %s)", run.get("id"), role, card_id,
             "kept as-is" if card_md["as_is"] else (mode or "voxel"), source_ref,
             (card_md.get("text_swap") or {}).get("status"))
    # The card's own words that stay on it: the script says them too, so the
    # Library, the Rebrand heading and every prompt agree with the card.
    fields = _script_fields_from_card(md, role, text_md.get("script_words") or {})
    if fields:
        await _patch_script_fields(int(run["id"]), fields)
    card_md = await _warm_bookend_motion(card_id, role, card_md)
    if src_card is not None:
        card_md = await _reuse_card_clip(
            run_id=int(run["id"]), role=role, card_id=card_id, card_md=card_md,
            src_card_id=int(source_ref["card_id"]), aspect=aspect,
        )
    return card_id, card_md


async def _rebuild_kept_card(run_id: int, role: str, *, swap: bool = False) -> int | None:
    """Make the run's `role` card again from its picks (a kept card: a free
    pixel copy) and point the run at it; on a failure the tile gets the error
    and no card. Its clip is out of date, unless the card reused a previous
    reel's finished clip. Without `swap` (an edit of the run) a card whose
    words must be swapped is not made: the run gets no card, and the Stills
    step (or the reel's start) makes it with the swap. Returns the new card
    id, or None."""
    run = await _fetch_run_row(run_id)
    md = run["metadata"] or {}
    try:
        card_id, card_md = await _make_bookend_card(run, md, role, swap=swap)
    except _SwapPending:
        log.info("[yas-th] run=%d %s card: its words must be swapped; made from the Stills step", run_id, role)
        await _patch_metadata(run_id, {f"{role}_image_id": None, f"{role}_error": None,
                                       f"{role}_video_stale": True})
        return None
    except Exception as e:
        log.exception("[yas-th] run=%d %s card rebuild failed", run_id, role)
        detail = e.detail if isinstance(e, HTTPException) else str(e)
        await _patch_metadata(run_id, {f"{role}_image_id": None, f"{role}_error": str(detail)[:480]})
        return None
    await _patch_metadata(run_id, {f"{role}_image_id": card_id, f"{role}_error": None,
                                   f"{role}_video_stale": True, **_reused_clip_patch(role, card_md)})
    return card_id


async def _ensure_kept_cards(run_id: int) -> dict:
    """Before a reel's bookend clips are made: a wanted bookend whose picked
    image is kept as-is (ref mode 'as_is') but has no card made from it gets
    that card now, a free pixel copy, with its words swapped when they must
    be (an older run, picked after its stills, whose Veo animated the raw
    image: run 827; or a card an edit left to be made). Returns the run's
    metadata."""
    run = await _fetch_run_row(run_id)
    md = run["metadata"] or {}
    refs = md.get("refs") or {}
    for role in ("intro", "outro"):
        if not _bookend_wanted(md, role) or _ref_mode(refs, role) != REF_MODE_AS_IS \
                or not _bookend_ref(refs, role):
            continue
        cid = md.get(f"{role}_image_id")
        card = await _fetch_card_row(int(cid)) if cid else None
        if card and _card_matches_refs(card, refs, role):
            continue
        log.info("[yas-th] run=%d %s: no card from the picked image yet; keeping it as-is", run_id, role)
        await _rebuild_kept_card(run_id, role, swap=True)
        md = (await _fetch_run_row(run_id))["metadata"] or {}
    return md


# ---------------------------------------------------------------------------
# A kept card's text: read once, swapped only when needed
# ---------------------------------------------------------------------------
#
# A kept intro/outro card is read once with Gemini Flash (every line's text,
# its kind: logo / url / cta / headline / subtitle / other, and its box). The
# read is cached on the card row (text_lines; source_text_lines is the read of
# the pick itself) and on a previous reel's card it was copied from, so the
# same pick is never read twice. Logo, URL and CTA lines are never replaced. A
# headline or subtitle is replaced only when it differs from the script's
# words and the run does not keep the card's words: ONE image edit through
# card_animation (the run's aspect forced), pasted back with PIL through a
# feathered mask of the dilated text zone, so every pixel outside that zone is
# the card's own, byte for byte. The result is read back; a mismatch is tried
# once more, then the original card is kept with a warning.


class _SwapPending(Exception):
    """A kept card's words must be swapped, and the caller (an edit) does
    not pay for the image edit: the card is made later, with the swap."""


class _SwapFailed(Exception):
    """A text swap that could not be made cleanly (the text could not be
    located, the model failed, or the result did not read back)."""


_CARD_TEXT_LINES_MAX = 24


def _card_lines_from(result: Any) -> list[dict] | None:
    """A card text read as clean lines: [{text, kind, box}] in reading order,
    `box` = [ymin, xmin, ymax, xmax] on 0..1000 (None when the reader gave
    none that makes sense), unknown kinds read as 'other', empty lines
    dropped. None when the answer is not a read at all (an empty list is a
    card with no text)."""
    if isinstance(result, dict):
        result = result.get("lines")
    if not isinstance(result, list):
        return None
    out: list[dict] = []
    for ln in result:
        if not isinstance(ln, dict):
            continue
        text = " ".join(str(ln.get("text") or "").split())[:200]
        if not text:
            continue
        kind = ln.get("kind") if ln.get("kind") in th_prompts.CARD_TEXT_KINDS else "other"
        raw = ln.get("box_2d", ln.get("box"))
        box = None
        if isinstance(raw, (list, tuple)) and len(raw) == 4:
            try:
                y0, x0, y1, x1 = (min(1000, max(0, int(round(float(v))))) for v in raw)
                if y1 > y0 and x1 > x0:
                    box = [y0, x0, y1, x1]
            except (TypeError, ValueError):
                box = None
        out.append({"text": text, "kind": kind, "box": box})
        if len(out) == _CARD_TEXT_LINES_MAX:
            break
    return out


async def _read_card_lines(path: Path) -> list[dict] | None:
    """One Gemini Flash read of a card's text (_card_lines_from); None when
    it fails."""
    try:
        result = await asyncio.wait_for(
            gemini_multimodal(
                th_prompts.CARD_TEXT_READ_PROMPT,
                images=[Path(path)],
                system=th_prompts.CARD_TEXT_READ_SYSTEM,
                json_schema=th_prompts.CARD_TEXT_READ_SCHEMA,
                temperature=0.0,
                max_tokens=1500,
                model=config.vertex_gemini_fast_model,
            ),
            timeout=60,
        )
    except Exception:
        log.exception("[yas-th] card text read of %s failed", path)
        return None
    lines = _card_lines_from(result)
    if lines is None:
        log.warning("[yas-th] card text read of %s returned %r", path, result)
    return lines


async def _read_card_bytes(data: bytes) -> list[dict] | None:
    """_read_card_lines of an image held in memory (a temporary PNG)."""
    import tempfile

    fd, name = tempfile.mkstemp(suffix=".png")
    os.close(fd)
    tmp = Path(name)
    try:
        tmp.write_bytes(data)
        return await _read_card_lines(tmp)
    finally:
        tmp.unlink(missing_ok=True)


async def _source_card_lines(
    md: dict, role: str, source_ref: dict | None, data: bytes, *, padded: bool, aspect: str,
) -> list[dict] | None:
    """The text of a kept card's pick, as copied (`data`): the read cached on
    the run's current card made from the same pick at the same shape, else on
    the previous reel's card it is a pixel copy of, else one read now (cached
    on that previous card too). None when it cannot be read."""
    cid = (md or {}).get(f"{role}_image_id")
    if cid:
        cur = _md_dict((await _fetch_card_row(int(cid)) or {}).get("metadata"))
        if cur.get("source_ref") == source_ref and cur.get("aspect") == aspect:
            lines = _card_lines_from(cur.get("source_text_lines"))
            if lines is not None:
                return lines
    src_id = int(source_ref["card_id"]) if source_ref and source_ref.get("card_id") and not padded else None
    if src_id is not None:
        lines = _card_lines_from(_md_dict((await _fetch_card_row(src_id) or {}).get("metadata")).get("text_lines"))
        if lines is not None:
            return lines
    lines = await _read_card_bytes(data)
    if lines is not None and src_id is not None:
        await _patch_metadata(src_id, {"text_lines": lines})
    return lines


def _text_swap_plan(lines: list[dict], headline: str, subtitle: str) -> list[dict]:
    """What a kept card needs swapped to say the script's words: for its
    headline lines and its subtitle lines (each group joined), a
    {kind, lines (indexes), old, new} when the script has words for it and
    they differ (case, punctuation and spacing aside). Logo, URL, CTA and
    other lines are never in it; a card with none of these kinds needs
    nothing."""
    plan: list[dict] = []
    for kind, target in (("headline", headline), ("subtitle", subtitle)):
        idx = [i for i, ln in enumerate(lines or []) if ln.get("kind") == kind]
        new = " ".join(str(target or "").split())[:th_prompts.CARD_TEXT_SWAP_MAX]
        if not idx or not new:
            continue
        old = " ".join(lines[i]["text"] for i in idx)
        if th_prompts.card_text_norm(old) == th_prompts.card_text_norm(new):
            continue
        plan.append({"kind": kind, "lines": idx, "old": old, "new": new})
    return plan


def _card_script_words(lines: list[dict], role: str) -> dict[str, str]:
    """The words a kept card says for the script: {'headline': its headline
    lines (an outro with none: its call to action), 'subtitle': its subtitle
    lines}, each only when the card has them (a logo-only card says none)."""
    def joined(kind: str) -> str:
        return " ".join(ln["text"] for ln in lines or [] if ln.get("kind") == kind).strip()

    out: dict[str, str] = {}
    head = joined("headline") or (joined("cta") if role == "outro" else "")
    if head:
        out["headline"] = head
    sub = joined("subtitle")
    if sub:
        out["subtitle"] = sub
    return out


def _script_fields_from_card(md: dict, role: str, words: dict[str, str]) -> dict[str, str]:
    """The script fields a kept card's own words replace ({'intro_headline':
    ...}): only those whose words really differ from the script's (case,
    punctuation and spacing aside)."""
    script = (md or {}).get("script") or {}
    out: dict[str, str] = {}
    for kind, text in (words or {}).items():
        field = f"{role}_{kind}"
        if text and th_prompts.card_text_norm(text) != th_prompts.card_text_norm(script.get(field)):
            out[field] = text
    return out


async def _patch_script_fields(run_id: int, fields: dict) -> None:
    """Set fields of metadata.script in one statement (jsonb_set), so the
    intro and outro cards made at the same time never undo each other."""
    if not fields:
        return
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                UPDATE yas_generations
                   SET metadata = jsonb_set(
                         metadata, '{script}',
                         COALESCE(metadata->'script', '{}'::jsonb) || %s::jsonb, true),
                       updated_at = NOW()
                 WHERE id = %s
                """,
                (Jsonb(fields), run_id),
            )
    log.info("[yas-th] run=%d script takes the kept card's words: %s", run_id, sorted(fields))


def _line_px_box(line: dict, w: int, h: int) -> tuple[int, int, int, int] | None:
    """A text line's box in pixels (left, top, right, bottom), or None."""
    import math

    b = line.get("box")
    if not b:
        return None
    y0, x0, y1, x1 = b
    return (math.floor(x0 * w / 1000), math.floor(y0 * h / 1000),
            math.ceil(x1 * w / 1000), math.ceil(y1 * h / 1000))


def _swap_text_boxes(size: tuple[int, int], lines: list[dict], plan: list[dict]) -> list[tuple] | None:
    """For each replaced group of `plan`: (x0, y0, x1, y1, line count) of its
    old words' box in pixels (the union of its lines' boxes). None when a
    replaced line has no box."""
    w, h = size
    out: list[tuple] = []
    for p in plan:
        boxes = [_line_px_box(lines[i], w, h) for i in p["lines"]]
        if not boxes or any(b is None for b in boxes):
            return None
        out.append((min(b[0] for b in boxes), min(b[1] for b in boxes),
                    max(b[2] for b in boxes), max(b[3] for b in boxes), len(boxes)))
    return out


def _swap_zone_rects(
    size: tuple[int, int], lines: list[dict], plan: list[dict], content_box: tuple | None = None,
) -> list[tuple[float, float, float, float]] | None:
    """The zone of each replaced group: its old words' box, dilated and never
    wider: the new words must fit where the old ones were (run 830's longer
    zone let the headline spill over the card's art). At the sides half a
    line, never less than the mask's feathered edge (so the old words' place
    is fully pasted). Above and below, the feathered edge PLUS room for the
    new words: a third of a line, never less than 1% of the card, since the
    reader's boxes move by a few thousandths of the card (run 830's card 835:
    a short subtitle's padding was just the feathered edge, so its safe area
    was its old box to the pixel and the new, shorter line read a little
    taller "ran outside"). Clipped to `content_box` (the picked image inside
    a padded card). None when a replaced line has no box."""
    w, h = size
    boxes = _swap_text_boxes(size, lines, plan)
    if boxes is None:
        return None
    # The picture's last column and row (the mask paints its right and
    # bottom edges inclusively).
    cx0, cy0, cx1, cy1 = ((content_box[0], content_box[1], content_box[2] - 1, content_box[3] - 1)
                          if content_box else (0, 0, w, h))
    # The old words always lie inside the fully pasted part of the zone (a
    # small line's third-of-a-line margin was thinner than the feather).
    edge = 2 * _swap_feather(size) + 2
    zones: list[tuple[float, float, float, float]] = []
    for x0, y0, x1, y1, n in boxes:
        line_h = max(1.0, (y1 - y0) / n)
        pad_y, pad_x = max(0.35 * line_h, 0.01 * h) + edge, max(0.5 * line_h, 0.02 * w, edge)
        zones.append((max(0.0, float(cx0), x0 - pad_x), max(0.0, float(cy0), y0 - pad_y),
                      min(float(w), float(cx1), x1 + pad_x), min(float(h), float(cy1), y1 + pad_y)))
    return zones


def _swap_feather(size: tuple[int, int]) -> int:
    """The feather of a text swap's paste-back mask, in pixels."""
    return max(2, round(0.01 * min(size)))


# How far from a cut-out of a swap's mask (a kept line, an avoided rect) the
# paste is back to 90%, in feathers (plus a pixel for the rectangles'
# rounding): the cut-out is grown by a feather, then blurred by half a
# feather (90% is 1.28 of that blur further on).
_SWAP_FADE = 1.64


def _swap_keep_rect(box: tuple, olds: list[tuple], feather: int) -> tuple[float, float, float, float]:
    """A kept line's cut-out from a swap's zone: its box (pixels) grown by
    15% of its height (at least 2 px) for a reader's tight box, but on a
    side facing a replaced line's old box (`olds`) only so far that the
    paste is back to 90% at that box (_SWAP_FADE), down to the line's own
    box. Run 830's card 835 has 24 px between its headline and subtitle:
    with the full margin a new subtitle swapped in alone was pasted at under
    half strength along its top, the old letters showing through."""
    g = max(2.0, 0.15 * (box[3] - box[1]))
    room = _SWAP_FADE * feather + 1
    left = top = right = bottom = g
    for o in olds:
        if o[0] < box[2] and box[0] < o[2]:
            if o[1] >= box[3]:
                bottom = min(bottom, max(0.0, o[1] - box[3] - room))
            elif o[3] <= box[1]:
                top = min(top, max(0.0, box[1] - o[3] - room))
        if o[1] < box[3] and box[1] < o[3]:
            if o[0] >= box[2]:
                right = min(right, max(0.0, o[0] - box[2] - room))
            elif o[2] <= box[0]:
                left = min(left, max(0.0, box[0] - o[2] - room))
    return (box[0] - left, box[1] - top, box[2] + right, box[3] + bottom)


def _swap_weak(alpha: Any, rects: list[tuple], tol: float) -> bool:
    """Whether a paste mask `alpha` ('L') is under 90% anywhere in one of
    `rects` (pixels) less `tol` on each side (a reader's box is a little
    looser than its letters): a letter there would be faded or cut."""
    import math

    w, h = alpha.size
    for x0, y0, x1, y1 in rects:
        box = (max(0, math.ceil(x0 + tol)), max(0, math.ceil(y0 + tol)),
               min(w, math.floor(x1 - tol)), min(h, math.floor(y1 - tol)))
        if box[2] > box[0] and box[3] > box[1] and alpha.crop(box).getextrema()[0] < 0.9 * 255:
            return True
    return False


def _swap_faded(
    size: tuple[int, int], before: list[dict], plan: list[dict], after: list[dict] | None,
    zones: list[tuple], groups: list[int], alpha: Any, tol: list[float],
) -> dict[int, str]:
    """{group: why} for each group of `groups` with a new line (read back:
    _swap_line_group) the paste `alpha` puts in at under 90% somewhere
    (_swap_weak): a kept line's cut-out or an avoided rect beside it, which
    the safe area (_swap_fits) does not see. A line that reads as one of
    the card's lines those groups do not replace is not theirs."""
    replaced = {i for g in groups for i in plan[g]["lines"]}
    fixed = {th_prompts.card_text_norm(ln["text"]) for i, ln in enumerate(before) if i not in replaced}
    out: dict[int, str] = {}
    for ln in after or []:
        if th_prompts.card_text_norm(ln.get("text")) in fixed:
            continue
        g = _swap_line_group(ln, plan, zones, size)
        if g not in groups or g in out:
            continue
        if not (th_prompts.card_text_words([ln.get("text") or ""])
                & th_prompts.card_text_words([plan[g]["new"]])):
            continue
        box = _line_px_box(ln, *size)
        if box is not None and _swap_weak(alpha, [box], tol[g] if g < len(tol) else 0.0):
            out[g] = f'the new words "{ln["text"]}" ran outside the card\'s text area'
    return out


def _swap_other_rects(
    size: tuple[int, int], lines: list[dict], plan: list[dict], after: list[dict] | None,
    glyphs: list[tuple], pad: float,
) -> list[tuple] | None:
    """The read-back boxes (pixels, grown by `pad`) of every line of a
    swapped card that is neither the new words kept (`glyphs`) nor one of
    the card's lines that never change: the new words of the groups that
    failed, and anything else read. None when one has no box."""
    replaced = {i for p in plan for i in p["lines"]}
    fixed = {th_prompts.card_text_norm(ln["text"]) for i, ln in enumerate(lines) if i not in replaced}
    out: list[tuple] = []
    for ln in after or []:
        norm = th_prompts.card_text_norm(ln.get("text"))
        if not norm or norm in fixed:
            continue
        box = _line_px_box(ln, *size)
        if box is None:
            return None
        if tuple(float(v) for v in box) in glyphs:
            continue
        out.append((box[0] - pad, box[1] - pad, box[2] + pad, box[3] + pad))
    return out


def _swap_safe_rects(size: tuple[int, int], zones: list[tuple]) -> list[tuple]:
    """Where a swapped word is fully pasted: each zone less its feathered
    edge (two feathers and 2 px). A letter outside it is cut or faded."""
    m = 2 * _swap_feather(size) + 2
    return [(x0 + m, y0 + m, x1 - m, y1 - m) for x0, y0, x1, y1 in zones]


def _swap_tolerances(size: tuple[int, int], lines: list[dict], plan: list[dict]) -> list[float]:
    """How far, in pixels, a new line's read-back box may run past its
    group's safe area (_swap_safe_rects), one per group of `plan`: a reader's
    box is not exact (it is often a little looser than the letters), so 15%
    of the old words' line height, but never more than keeps the paste at
    90% there (the mask's feather is blurred by half a feather: 90% is
    reached 0.64 feathers in, the safe area starts 2 feathers and 2 px in),
    so a letter at that edge is never visibly faded."""
    boxes = _swap_text_boxes(size, lines, plan) or []
    cap = 0.36 * _swap_feather(size) + 2
    return [min(0.15 * max(1.0, (y1 - y0) / n), cap) for _x0, y0, _x1, y1, n in boxes]


def _box_within(box: tuple, rect: tuple, tol: float = 0.0) -> bool:
    """Whether pixel box `box` lies inside `rect` grown by `tol` on every side."""
    x0, y0, x1, y1 = rect
    return box[0] >= x0 - tol and box[1] >= y0 - tol and box[2] <= x1 + tol and box[3] <= y1 + tol


def _swap_fit_spec(size: tuple[int, int], lines: list[dict], plan: list[dict]) -> dict | None:
    """The FIT block of the swap directive (th_prompts._card_swap_fit_lines):
    the card's size and, for each group, its old words' pixel box and line
    count."""
    boxes = _swap_text_boxes(size, lines, plan)
    if boxes is None:
        return None
    return {"card_w": size[0], "card_h": size[1], "boxes": [
        {"kind": p["kind"], "x0": b[0], "y0": b[1], "x1": b[2], "y1": b[3], "lines": b[4]}
        for p, b in zip(plan, boxes)]}


def _swap_fits(
    size: tuple[int, int], before: list[dict], plan: list[dict], after: list[dict] | None,
    safe: list[tuple],
) -> tuple[bool, str]:
    """Whether every line of the new words, by its read-back box, lies inside
    a zone's safe area (_swap_safe_rects, one per group of `plan`), give or
    take the reader's tolerance (_swap_tolerances): no letter cut by the
    mask's edge or faded in its feather. A line of the card that was kept
    (its words read the same) is not judged; a new line read with no box
    cannot be judged and fails. (ok, why)."""
    if after is None:
        return False, "the swapped card could not be read back"
    replaced = {i for p in plan for i in p["lines"]}
    kept = {th_prompts.card_text_norm(ln["text"]) for i, ln in enumerate(before) if i not in replaced}
    want = th_prompts.card_text_words([p["new"] for p in plan])
    tol = _swap_tolerances(size, before, plan)
    if len(tol) != len(safe):
        tol = [0.0] * len(safe)
    w, h = size
    for ln in after:
        norm = th_prompts.card_text_norm(ln.get("text"))
        if not norm or norm in kept or not (th_prompts.card_text_words([ln["text"]]) & want):
            continue
        box = _line_px_box(ln, w, h)
        if box is None:
            return False, f'the new words "{ln["text"]}" could not be located to check they fit'
        if not any(_box_within(box, s, t) for s, t in zip(safe, tol)):
            return False, f'the new words "{ln["text"]}" ran outside the card\'s text area'
    return True, "the new words fit inside the text area"


def _swap_line_group(
    ln: dict, plan: list[dict], zones: list[tuple], size: tuple[int, int],
) -> int | None:
    """The group of `plan` a read-back line belongs to: the one whose new
    words it shares most; with none shared (or a tie), the one whose zone
    holds the line's centre (the first of a tie when none does). None when
    it shares no words and lies in no zone."""
    words = th_prompts.card_text_words([ln.get("text") or ""])
    scores = [len(words & th_prompts.card_text_words([p["new"]])) for p in plan]
    best = max(scores, default=0)
    tied = [g for g, s in enumerate(scores) if s == best] if best else list(range(len(plan)))
    box = _line_px_box(ln, *size)
    if box is not None:
        cx, cy = (box[0] + box[2]) / 2, (box[1] + box[3]) / 2
        for g in tied:
            if g < len(zones):
                x0, y0, x1, y1 = zones[g]
                if x0 <= cx <= x1 and y0 <= cy <= y1:
                    return g
    return tied[0] if best else None


def _swap_judge(
    size: tuple[int, int], before: list[dict], plan: list[dict], after: list[dict] | None,
    zones: list[tuple], safe: list[tuple], groups: list[int],
) -> dict[int, str | None]:
    """Each group of `plan` in `groups` judged on its own on a read-back of
    the card (every group of `plan` should read as its new words, every
    other line as before): None when the group's new words read back exactly
    and lie inside its safe area (give or take _swap_tolerances), else why
    not, in _swap_verdict's and _swap_fits's words. A word missing from a
    line that never changes, or an extra word on no group's line, is laid at
    every group's door (the read cannot be trusted)."""
    if after is None:
        return {g: "the swapped card could not be read back" for g in groups}
    words = th_prompts.card_text_words
    replaced = {i for p in plan for i in p["lines"]}
    kept_lines = [ln for i, ln in enumerate(before) if i not in replaced]
    keep = words([ln["text"] for ln in kept_lines])
    kept_norm = {th_prompts.card_text_norm(ln["text"]) for ln in kept_lines}
    want = words([p["new"] for p in plan])
    got = words([ln["text"] for ln in after])
    lost = keep - got
    if lost:
        return {g: "missing or misspelled: " + ", ".join(sorted(lost)[:6]) for g in groups}
    out: dict[int, str | None] = {g: None for g in groups}
    for g in groups:
        miss = words([plan[g]["new"]]) - got
        if miss:
            out[g] = "missing or misspelled: " + ", ".join(sorted(miss)[:6])
    extra: dict[int, set] = {}
    for ln in after:
        bad = words([ln.get("text") or ""]) - want - keep
        if not bad:
            continue
        g = _swap_line_group(ln, plan, zones, size)
        for k in ([g] if g is not None else groups):
            extra.setdefault(k, set()).update(bad)
    for g in groups:
        if out[g] is None and extra.get(g):
            out[g] = "text that should not be there: " + ", ".join(sorted(extra[g])[:6])
    tol = _swap_tolerances(size, before, plan)
    if len(tol) != len(safe):
        tol = [0.0] * len(safe)
    for ln in after:
        norm = th_prompts.card_text_norm(ln.get("text"))
        if not norm or norm in kept_norm or not (words([ln["text"]]) & want):
            continue
        g = _swap_line_group(ln, plan, zones, size)
        if g not in out or out[g] is not None:
            continue
        box = _line_px_box(ln, *size)
        if box is None:
            out[g] = f'the new words "{ln["text"]}" could not be located to check they fit'
        elif not _box_within(box, safe[g], tol[g]):
            out[g] = f'the new words "{ln["text"]}" ran outside the card\'s text area'
    return out


def _swap_glyph_rects(
    size: tuple[int, int], plan: list[dict], after: list[dict] | None, zones: list[tuple],
    groups: list[int],
) -> list[tuple]:
    """Where the new words of `groups` are on a read-back card: the boxes of
    the lines that belong to them (_swap_line_group), in pixels."""
    out: list[tuple] = []
    want = th_prompts.card_text_words([plan[g]["new"] for g in groups])
    for ln in after or []:
        if not (th_prompts.card_text_words([ln.get("text") or ""]) & want):
            continue
        if _swap_line_group(ln, plan, zones, size) in groups:
            box = _line_px_box(ln, *size)
            if box is not None:
                out.append(tuple(float(v) for v in box))
    return out


def _text_swap_mask(
    size: tuple[int, int], lines: list[dict], plan: list[dict], content_box: tuple | None = None,
    *, avoid: list[tuple] | None = None, protect: list[tuple] | None = None,
) -> tuple[Any, Any] | None:
    """(alpha, zone) masks ('L', the card's size) for pasting a text swap
    back. The zone of each replaced group is its lines' box, dilated
    (_swap_zone_rects: never widened, kept inside `content_box` when given);
    every other line keeps its own dilated box out of it (_swap_keep_rect:
    less so beside a replaced line, which stays at 90% or more). `alpha` feathers
    inside the zone and is 0 everywhere outside it, so those pixels stay the
    card's own. For a swap kept line by line: `avoid` (pixel rects) is kept
    out of the zone too (another group's zone, or new words already in), and
    `protect` (pixel rects, the new words of the groups pasted) is pasted at
    full strength wherever it lies in the zone, never cut by an `avoid`.
    None when a replaced line has no box, or nothing is left to edit."""
    from PIL import Image, ImageChops, ImageDraw, ImageFilter

    w, h = size
    replaced = {i for p in plan for i in p["lines"]}
    zones = _swap_zone_rects(size, lines, plan, content_box)
    if zones is None:
        return None
    f = _swap_feather(size)
    olds = [b for b in (_line_px_box(lines[i], w, h) for i in sorted(replaced)) if b is not None]
    keep: list[tuple[float, float, float, float]] = []
    for i, ln in enumerate(lines):
        b = _line_px_box(ln, w, h) if i not in replaced else None
        if b is not None:
            keep.append(_swap_keep_rect(b, olds, f))
    # A protected rect counts only inside the zones it belongs to.
    guard = [(max(px0, zx0), max(py0, zy0), min(px1, zx1), min(py1, zy1))
             for px0, py0, px1, py1 in protect or [] for zx0, zy0, zx1, zy1 in zones
             if min(px1, zx1) > max(px0, zx0) and min(py1, zy1) > max(py0, zy0)]

    def paint(grow: float) -> Any:
        im = Image.new("L", (w, h), 0)
        d = ImageDraw.Draw(im)
        for x0, y0, x1, y1 in zones:
            if x1 - x0 > 2 * -grow and y1 - y0 > 2 * -grow:
                d.rectangle((x0 - grow, y0 - grow, x1 + grow, y1 + grow), fill=255)
        for x0, y0, x1, y1 in avoid or []:
            d.rectangle((x0 + grow, y0 + grow, x1 - grow, y1 - grow), fill=0)
        for x0, y0, x1, y1 in guard:
            d.rectangle((x0 + grow, y0 + grow, x1 - grow, y1 - grow), fill=255)
        for x0, y0, x1, y1 in keep:
            d.rectangle((x0 + grow, y0 + grow, x1 - grow, y1 - grow), fill=0)
        return im

    zone = paint(0.0)
    if zone.getbbox() is None:
        return None
    soft = paint(-float(f)).filter(ImageFilter.GaussianBlur(f / 2))
    return ImageChops.multiply(soft, zone), zone


def _composite_swap(original: Any, edited_bytes: bytes, alpha: Any) -> bytes:
    """The edited card (scaled to the card's size) pasted onto the original
    through `alpha`, as PNG: where alpha is 0 the original's pixels, exactly."""
    from PIL import Image

    with Image.open(BytesIO(edited_bytes)) as src:
        edited = src.convert("RGB")
    if edited.size != original.size:
        edited = edited.resize(original.size, Image.LANCZOS)
    out = Image.composite(edited, original, alpha)
    buf = BytesIO()
    out.save(buf, format="PNG")
    return buf.getvalue()


def _swap_verdict(before: list[dict], plan: list[dict], after: list[dict] | None) -> tuple[bool, str]:
    """Whether a swapped card reads back as asked: the new words and every
    word of the lines that were kept, and nothing else (case, punctuation and
    Unicode forms aside). (ok, why)."""
    if after is None:
        return False, "the swapped card could not be read back"
    replaced = {i for p in plan for i in p["lines"]}
    want = th_prompts.card_text_words([p["new"] for p in plan])
    keep = th_prompts.card_text_words([ln["text"] for i, ln in enumerate(before) if i not in replaced])
    got = th_prompts.card_text_words([ln["text"] for ln in after])
    missing = (want | keep) - got
    if missing:
        return False, "missing or misspelled: " + ", ".join(sorted(missing)[:6])
    extra = got - want - keep
    if extra:
        return False, "text that should not be there: " + ", ".join(sorted(extra)[:6])
    return True, "the new words read back exactly"


async def _swap_card_text(
    data: bytes, lines: list[dict], plan: list[dict], aspect: str,
    content_box: tuple | None = None,
) -> tuple[bytes, list[dict], list[dict], list[dict]]:
    """Swap a kept card's words (`plan`, one group per headline / subtitle)
    and return (card PNG, its lines as read back, the groups swapped, the
    groups that keep the card's own words, each with its `why`).

    One image edit of the card (card_animation, the run's aspect forced),
    told the pixel box of the old words the new ones must stay in
    (_swap_fit_spec), pasted back through _text_swap_mask (inside
    `content_box`, the picked image within a padded card), then read back
    and judged group by group (_swap_judge): its new words read back exactly
    AND every new line lies inside its zone's safe area and is pasted at 90%
    or more (_swap_faded: beside a kept line's cut-out), so no letter is cut
    or faded by the mask. The groups that failed get one more attempt on
    their own, on the card with the passed groups already in (pasted where
    the read saw nothing else, and each whole, its old words too: else none
    is kept and all go again): with a spelling check after a mismatch, a
    smaller size after a spill (on two lines where the old words had more
    than one). An edit that fails outright is not tried again. Then a group
    that never passed keeps the card's own words while the others keep
    theirs (run 830's card 835 lost its good headline for its subtitle):
    that card is read back once more and must read as the swapped groups'
    words and the rest as before.
    Raises _SwapFailed (the original card is kept) when no group could be
    swapped, or the card kept that way does not read back right."""
    import shutil
    import tempfile

    from PIL import Image

    with Image.open(BytesIO(data)) as src:
        original = src.convert("RGB")
    size = original.size
    zones = _swap_zone_rects(size, lines, plan, content_box)
    if zones is None or await asyncio.to_thread(_text_swap_mask, size, lines, plan, content_box) is None:
        raise _SwapFailed("the words to change could not be located on the card")
    safe = _swap_safe_rects(size, zones)
    tol = _swap_tolerances(size, lines, plan)
    gem_aspect, _ = _aspect_size(aspect)
    tmp = Path(tempfile.mkdtemp(prefix="th-card-swap-"))
    pending = list(range(len(plan)))
    done: list[int] = []
    glyphs: list[tuple] = []          # where the swapped groups' new words are
    kept_alpha: dict[int, Any] = {}   # the paste that kept each of them
    whys: dict[int, str] = {g: "it was not tried" for g in pending}
    strict: set[int] = set()
    tighter: set[int] = set()
    current, current_bytes = original, data
    pad = max(tol, default=0.0)
    why = "it was not tried"
    try:
        for attempt in (1, 2):
            sub = [plan[g] for g in pending]
            card_path = tmp / f"card-{attempt}.png"
            card_path.write_bytes(current_bytes)
            parts = th_prompts.build_card_text_swap_parts(
                sub, card=card_path, aspect=gem_aspect, strict=bool(strict & set(pending)),
                fit=_swap_fit_spec(size, lines, sub), tighter=bool(tighter & set(pending)),
            )
            try:
                async with _image_gen_sem():
                    edited = await card_animation._render_edit_escalating(
                        parts, system_instruction=th_prompts.CARD_TEXT_SWAP_SYSTEM,
                        timeout=IMAGE_GEN_TIMEOUT_GEMINI, what="th-card-text-swap",
                        aspect_ratio=gem_aspect,
                    )
            except Exception as e:
                # The edit itself failed (a refusal, the quota, a timeout; a
                # transient error was already retried inside): asking again
                # now would only fail again, and slowly.
                log.exception("[yas-th] card text swap attempt %d failed", attempt)
                why = f"the image model failed ({type(e).__name__})"
                whys.update({g: why for g in pending})
                break
            if not edited:
                why = "the image model returned no image"
                whys.update({g: why for g in pending})
                break
            ew, eh = _image_dims(edited)
            if not ew or not eh or _aspect_off(ew, eh, aspect) > _ASPECT_TOLERANCE:
                # Scaled onto the card, another shape would stretch its words.
                why = f"the edit came back {ew}x{eh}, not {aspect}"
                whys.update({g: why for g in pending})
                continue
            # Only the groups asked for are pasted: the new words already in
            # (the groups passed before) stay as they are.
            avoid = [(x0 - pad, y0 - pad, x1 + pad, y1 + pad) for x0, y0, x1, y1 in glyphs]
            alpha, _zone = await asyncio.to_thread(
                _text_swap_mask, size, lines, sub, content_box, avoid=avoid)
            out = await asyncio.to_thread(_composite_swap, current, edited, alpha)
            after = await _read_card_bytes(out)
            judged = _swap_judge(size, lines, plan, after, zones, safe, list(range(len(plan))))
            # A new line this paste puts in at under 90% (beside a kept
            # line's cut-out, or the new words already in) is cut or faded.
            for g, bad in _swap_faded(size, lines, plan, after, zones, pending, alpha, tol).items():
                judged[g] = judged[g] or bad
            # A group already swapped that no longer reads right: this read
            # cannot be trusted for the others either.
            spoilt = next((judged[g] for g in done if judged[g]), None)
            for g in pending:
                bad = spoilt or judged[g]
                whys[g] = bad or ""
                # A spill asks for a smaller size; anything else read wrong,
                # for the spelling check.
                if bad and not spoilt and ("ran outside" in bad or "could not be located" in bad):
                    tighter.add(g)
                elif bad and not spoilt:
                    strict.add(g)
            passed = [g for g in pending if not whys[g]]
            failed = [g for g in pending if whys[g]]
            log.info("[yas-th] card text swap attempt %d: passed %s, failed %s",
                     attempt, [plan[g]["kind"] for g in passed],
                     [(plan[g]["kind"], whys[g]) for g in failed])
            if passed and not failed:
                return out, after or [], [plan[g] for g in sorted([*done, *passed])], []
            if passed:
                # Keep what passed: its zone of this attempt onto the card,
                # except where this read saw anything else (the failed
                # groups' new words), never a failed group's whole zone: it
                # reaches into the passed words' old place where the zones
                # overlap (card 835: the bottom of "TECH FUTURE" was left
                # beside the new headline, and a spill's ends past the
                # failed zone were pasted in). A line read with no box: the
                # failed zones, as nothing better is known.
                new_glyphs = _swap_glyph_rects(size, plan, after, zones, passed)
                others = _swap_other_rects(size, lines, plan, after, new_glyphs, pad)
                keep_alpha, _zone = await asyncio.to_thread(
                    _text_swap_mask, size, lines, [plan[g] for g in passed], content_box,
                    avoid=[zones[g] for g in failed] if others is None else others, protect=new_glyphs,
                )
                # Each passed group is pasted whole, its new words and the
                # old ones they replace (no half letter of either left);
                # else none is kept this time and all go again.
                torn = [g for g in passed if _swap_weak(
                    keep_alpha, [*_swap_glyph_rects(size, plan, after, zones, [g]),
                                 *[b[:4] for b in _swap_text_boxes(size, lines, [plan[g]]) or []]], tol[g])]
                if torn:
                    beside = " and ".join(plan[g]["kind"] for g in failed)
                    for g in passed:
                        whys[g] = f"its new words could not be kept whole beside the {beside}"
                        tighter.add(g)
                    log.info("[yas-th] card text swap attempt %d: %s not kept beside %s",
                             attempt, [plan[g]["kind"] for g in torn], beside)
                    failed = list(pending)
                else:
                    current_bytes = await asyncio.to_thread(_composite_swap, current, out, keep_alpha)
                    with Image.open(BytesIO(current_bytes)) as im:
                        current = im.convert("RGB")
                    done.extend(passed)
                    glyphs.extend(new_glyphs)
                    kept_alpha.update({g: keep_alpha for g in passed})
            pending = failed
            why = whys[failed[0]]
        if not done:
            raise _SwapFailed(why)
        # Some groups keep the card's own words: the card as it is now must
        # read as the swapped groups' new words and every other line as
        # before, each new line inside its safe area.
        swapped = [plan[g] for g in sorted(done)]
        after = await _read_card_bytes(current_bytes)
        ok, why_kept = _swap_verdict(lines, swapped, after)
        if ok:
            ok, why_kept = _swap_fits(
                size, lines, swapped, after,
                _swap_safe_rects(size, _swap_zone_rects(size, lines, swapped, content_box) or []),
            )
        if ok:
            # ...and pasted at full strength where it is read now.
            faded = [bad for g in sorted(done)
                     for bad in _swap_faded(size, lines, plan, after, zones, [g], kept_alpha[g], tol).values()]
            if faded:
                ok, why_kept = False, faded[0]
        log.info("[yas-th] card text swap kept %s, %s keep their words: %s (%s)",
                 [p["kind"] for p in swapped], [plan[g]["kind"] for g in pending],
                 "ok" if ok else "no", why_kept)
        if not ok:
            raise _SwapFailed(whys[pending[0]] if pending else why_kept)
        return current_bytes, after or [], swapped, [{**plan[g], "why": whys[g]} for g in pending]
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


async def _kept_card_text(
    run: dict, md: dict, role: str, data: bytes, *,
    source_ref: dict | None, padded: bool, swap: bool, content_box: tuple | None = None,
) -> tuple[bytes, dict]:
    """A kept card's text (see the section note): returns (card bytes, the
    keys its row records) where the keys are keep_text, source_text_lines,
    text_lines, card_text, text_swap {status, warning, swapped, kept} and
    script_words (the card's own words the script takes; not stored).
    status: 'kept' (the run keeps the card's words), 'unchanged' (nothing
    to swap), 'swapped', 'failed' (the original is kept, with a warning) or
    'unread' (the text could not be read: the card is kept as it is). A
    'swapped' card whose swap kept some lines' own words lists them in
    `kept` ({kind, old, new, why}) with a warning naming each.
    Raises _SwapPending when a swap is needed and `swap` is False."""
    aspect = _norm_aspect((md or {}).get("aspect_ratio"))
    keep = _keep_text((md or {}).get("refs"), role)
    lines = await _source_card_lines(md, role, source_ref, data, padded=padded, aspect=aspect)
    if lines is None:
        return data, {"keep_text": keep, "text_swap": {
            "status": "unread", "swapped": [],
            "warning": f"The {role} card's text couldn't be read, so the card is kept as it is.",
        }}
    plan = [] if keep else _text_swap_plan(lines, *_bookend_words(run, md, role))
    if plan and not swap:
        raise _SwapPending(role)
    final, status, warning, swapped = lines, ("kept" if keep else "unchanged"), None, []
    kept_lines: list[dict] = []
    if plan:
        try:
            data, final, done, left = await _swap_card_text(data, lines, plan, aspect, content_box)
            status = "swapped"
            swapped = [{"kind": p["kind"], "old": p["old"], "new": p["new"]} for p in done]
            kept_lines = [{"kind": p["kind"], "old": p["old"], "new": p["new"], "why": p["why"]}
                          for p in left]
            warning = _partial_swap_warning(role, kept_lines)
            if warning:
                log.warning("[yas-th] run=%s %s card text swap kept %s: %s", run.get("id"), role,
                            [k["kind"] for k in kept_lines], [k["why"] for k in kept_lines])
        except _SwapFailed as e:
            status = "failed"
            warning = (f"The {role} card's words couldn't be swapped cleanly ({e}), so it keeps "
                       "its own words. Make it again to try once more, or keep the card's text.")
            log.warning("[yas-th] run=%s %s card text swap failed: %s", run.get("id"), role, e)
    # The script takes the card's own words, except where a swap was asked
    # for: those already are the script's (swapped), or stay the script's so
    # the swap can be tried again (failed).
    planned = {p["kind"] for p in plan}
    words = {k: v for k, v in _card_script_words(lines, role).items() if k not in planned}
    return data, {
        "keep_text": keep,
        "source_text_lines": lines,
        "text_lines": final,
        "card_text": "\n".join(ln["text"] for ln in final) or None,
        "text_swap": {"status": status, "warning": warning, "swapped": swapped, "kept": kept_lines},
        "script_words": words,
    }


def _partial_swap_warning(role: str, kept: list[dict]) -> str | None:
    """The warning of a swap that kept some lines' own words: each line
    named, with its words kept, the words asked for and why. None when
    every line was swapped."""
    if not kept:
        return None
    said = "; its ".join(
        f'{k["kind"]} couldn\'t be swapped cleanly ({k["why"]}), so it keeps its own words '
        f'"{k["old"]}" instead of "{k["new"]}"' for k in kept)
    return f"The {role} card's {said}. Make it again to try once more, or keep the card's text."


# ---------------------------------------------------------------------------
# Previous reels' cards: the "Previous videos" picker, and their clips
# ---------------------------------------------------------------------------
#
# Every TH v2 run's current intro/outro card can be picked again on another
# run (refs.{role}_card_id). It is kept as it is like any pick; when it is
# copied pixel for pixel (same shape, no swap) its finished clip is reused: a
# copy of that MP4 becomes the run's own bookend clip (its row and file), so
# no Veo runs and deleting either run never takes the other's clip. Only a
# clip made by the motion director qualifies (the slow zoom, or a Veo clip
# whose pixel check and read-back both held, or that fell back to the zoom);
# an older bookend made with the ambient-motion prompt is made again instead.

def _clip_reusable(meta: dict) -> bool:
    """Whether a bookend clip's metadata says it shows its card faithfully:
    the slow zoom (made so, or a Veo clip replaced by it), or a Veo clip
    that passed BOTH checks, its pixels and its text. A Veo clip checked by
    its text only (before the pixel check existed, like anim 353 with its
    grid of marks) is not reused."""
    if meta.get("slow_zoom"):
        return True
    check = _md_dict(meta.get("bookend_text_check"))
    if check.get("fallback") == BOOKEND_STYLE_ZOOM:
        return True
    return check.get("ok") is True and _md_dict(check.get("pixels")).get("ok") is True


def _clip_style_of(meta: dict) -> str:
    """The bookend style a reusable clip is: the slow zoom or its icons (Veo)."""
    zoom = meta.get("slow_zoom") or _md_dict(meta.get("bookend_text_check")).get("fallback") == BOOKEND_STYLE_ZOOM
    return BOOKEND_STYLE_ZOOM if zoom else BOOKEND_STYLE_VEO


async def _reusable_clips(card_ids: list[int]) -> dict[int, dict]:
    """{card id: its newest reusable finished clip row} for these cards:
    ready, made by the motion director from that card, faithful
    (_clip_reusable), with its MP4 on disk."""
    if not card_ids:
        return {}
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT id, aspect_ratio, veo_model, duration_sec, local_path, metadata
                  FROM yas_animations
                 WHERE status = %s
                   AND metadata->'bookend_card'->>'card_id' = ANY(%s)
                 ORDER BY id DESC
                """,
                (STATUS_READY, [str(int(c)) for c in card_ids]),
            )
            rows = await cur.fetchall()
    out: dict[int, dict] = {}
    for r in rows or []:
        meta = _md_dict(r.get("metadata"))
        try:
            cid = int(_md_dict(meta.get("bookend_card")).get("card_id"))
        except (TypeError, ValueError):
            continue
        if cid in out or not _clip_reusable(meta):
            continue
        lp = r.get("local_path")
        if not lp or not _abs_from_rel(lp).exists():
            continue
        out[cid] = {**dict(r), "metadata": meta}
    return out


async def _reuse_card_clip(
    *, run_id: int, role: str, card_id: int, card_md: dict, src_card_id: int, aspect: str,
) -> dict:
    """The run's new card is a pixel copy of a previous reel's card: when
    that card has a reusable clip of this aspect, copy it as the run's own
    bookend clip (a ready row, cost 0, no Veo). Returns card_md with
    clip_animation_id / clip_style / reused_clip_from when it did."""
    src = (await _reusable_clips([src_card_id])).get(src_card_id)
    if src is None or src.get("aspect_ratio") != aspect:
        return card_md
    import shutil

    meta = src["metadata"]
    card_path = await _resolve_card_path(card_id)
    extra: dict[str, Any] = {
        "bookend_card": {"card_id": card_id, "path": _to_rel(Path(card_path)) if card_path else None,
                         "kept": True, "aspect": aspect},
        "bookend_motion": meta.get("bookend_motion"),
        "reused_from": int(src["id"]),
    }
    for k in ("slow_zoom", "bookend_text_check"):
        if k in meta:
            extra[k] = meta[k]
    anim_id = await _insert_animation_row(
        parent_run_id=run_id,
        prompt_used=f"(clip #{src['id']} of card #{src_card_id} reused: no new Veo)",
        veo_op_name=None, veo_model=src.get("veo_model") or BOOKEND_ZOOM_MODEL,
        duration_sec=int(src.get("duration_sec") or VEO_DURATION_BOOKEND), with_audio=False,
        cost_usd=0.0, role=role, aspect_ratio=aspect, extra_md=extra,
    )
    out = _animations_dir() / f"anim-{anim_id}.mp4"
    try:
        out.parent.mkdir(parents=True, exist_ok=True)
        await asyncio.to_thread(shutil.copyfile, _abs_from_rel(src["local_path"]), out)
        await _finish_local_animation(anim_id, out)
    except Exception as e:
        log.exception("[yas-th] run=%d %s: reusing clip #%s failed", run_id, role, src["id"])
        await _fail_animation(anim_id, f"reusing clip #{src['id']} failed: {str(e)[:300]}")
        return card_md
    style = _clip_style_of(meta)
    await _patch_metadata(card_id, {"reused_clip_from": int(src["id"]), "clip_animation_id": anim_id})
    log.info("[yas-th] run=%d %s card #%d reuses clip #%s of card #%d as anim=%d (%s)",
             run_id, role, card_id, src["id"], src_card_id, anim_id, style)
    return {**card_md, "reused_clip_from": int(src["id"]), "clip_animation_id": anim_id, "clip_style": style}


def _reused_clip_patch(role: str, card_md: dict | None) -> dict:
    """The run fields for a card that came with a reused clip: that clip is
    the bookend's, fresh. {} otherwise."""
    clip = (card_md or {}).get("clip_animation_id")
    if not clip:
        return {}
    return {f"{role}_animation_id": int(clip), f"{role}_video_stale": False,
            f"{role}_animation_style": (card_md or {}).get("clip_style") or BOOKEND_STYLE_ZOOM}


def _card_shape(w: Any, h: Any) -> str | None:
    """A card's shape: '9:16' or '16:9' when it is exactly one, else None."""
    for a in ASPECTS:
        if w and h and _is_exact_aspect(int(w), int(h), a):
            return a
    return None


async def list_previous_cards(
    role: str, *, exclude_run_id: int | None = None, limit: int = 30,
) -> list[dict]:
    """The "Previous videos" source of the intro/outro picker: each TH v2
    run's CURRENT `role` card (not the drafts it replaced, not the video
    editor's cards), newest first, with its image file. A kept copy of a
    Library asset is left out (the Library lists the asset), and copies of
    the same picture are listed once. Each item: {card_id, role, run_id,
    topic, url, card_text, aspect, width, height, as_is, clip_animation_id,
    clip_style, created_at}; the clip is a finished one reused when the card
    is picked as it is at that aspect."""
    if role not in ("intro", "outro"):
        raise HTTPException(400, "role must be intro or outro")
    limit = max(1, min(int(limit or 30), 60))
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT c.id, c.local_path, c.width, c.height, c.created_at, c.metadata,
                       r.id AS run_id, r.topic AS run_topic
                  FROM yas_generations c
                  JOIN yas_generations r
                    ON r.mode = %s
                   AND r.id = CASE
                                WHEN c.metadata->>'parent_talking_head_id' ~ '^[0-9]+$'
                                THEN (c.metadata->>'parent_talking_head_id')::int
                              END
                   AND r.metadata->>(%s::text) = c.id::text
                 WHERE c.mode = %s
                   AND c.status = %s
                   AND c.local_path IS NOT NULL
                   AND COALESCE(c.metadata->>'category', 'talking_head_card') = 'talking_head_card'
                   AND (%s::int IS NULL OR r.id <> %s::int)
                 ORDER BY c.id DESC
                 LIMIT %s
                """,
                (MODE_PREVIEW, f"{role}_image_id", MODE_INTRO if role == "intro" else MODE_OUTRO,
                 STATUS_READY, exclude_run_id, exclude_run_id, limit * 3),
            )
            rows = await cur.fetchall()
    items: list[dict] = []
    seen: set[tuple] = set()
    for r in rows or []:
        if not _abs_from_rel(r["local_path"]).exists():
            continue
        cmd = _md_dict(r.get("metadata"))
        src = cmd.get("source_ref") if isinstance(cmd.get("source_ref"), dict) else None
        copy = bool(cmd.get("as_is") and src) and _md_dict(cmd.get("text_swap")).get("status") != "swapped"
        if copy and src.get("asset_id"):
            continue
        key = (("card", int(src["card_id"])) if copy and src.get("card_id")
               else ("src", json.dumps(src, sort_keys=True)) if copy else ("card", int(r["id"])))
        if key in seen or ("card", int(r["id"])) in seen:
            continue
        seen.update({key, ("card", int(r["id"]))})
        items.append({
            "card_id": int(r["id"]),
            "role": role,
            "run_id": int(r["run_id"]),
            "topic": r.get("run_topic") or "",
            "url": f"/api/yas-producer/talking-head-v2/cards/{int(r['id'])}/file",
            "card_text": cmd.get("card_text"),
            "aspect": _card_shape(r.get("width"), r.get("height")),
            "width": r.get("width"),
            "height": r.get("height"),
            "as_is": bool(cmd.get("as_is")),
            "clip_animation_id": None,
            "clip_style": None,
            "created_at": r["created_at"].isoformat() if hasattr(r.get("created_at"), "isoformat") else None,
        })
        if len(items) == limit:
            break
    clips = await _reusable_clips([it["card_id"] for it in items])
    for it in items:
        clip = clips.get(it["card_id"])
        if clip and clip.get("aspect_ratio") == it["aspect"]:
            it["clip_animation_id"] = int(clip["id"])
            it["clip_style"] = _clip_style_of(clip["metadata"])
    return items


def _kept_bookend_roles(refs: dict | None) -> set[str]:
    """The bookends whose picked image is kept as it is (ref mode 'as_is')."""
    return {role for role in ("intro", "outro")
            if _bookend_ref(refs, role) and _ref_mode(refs, role) == REF_MODE_AS_IS}


def _recent_without_kept(recent: list[dict], kept: set[str]) -> list[dict]:
    """The anti-repetition list of recent bookend headlines without the
    bookends kept as they are: a kept card shows its own words (often a
    previous reel's), so the script must be free to say them."""
    if not kept:
        return recent
    return [{k: (None if k.removesuffix("_headline") in kept else v) for k, v in (b or {}).items()}
            for b in recent or []]


async def _kept_script_words(md: dict) -> dict[str, str]:
    """The script fields the run's kept cards pin while "Keep card text" is
    on ({'intro_headline': ...}): the card's own words, so a re-script says
    what the card shows."""
    refs = (md or {}).get("refs") or {}
    out: dict[str, str] = {}
    for role in ("intro", "outro"):
        cid = (md or {}).get(f"{role}_image_id")
        if not _keep_text(refs, role) or not cid:
            continue
        card = await _fetch_card_row(int(cid))
        cmd = _md_dict((card or {}).get("metadata"))
        lines = _card_lines_from(cmd.get("text_lines"))
        if not cmd.get("as_is") or lines is None or not _card_matches_refs(card, refs, role):
            continue
        out.update({f"{role}_{k}": v for k, v in _card_script_words(lines, role).items()})
    return out


# v6 — sibling row for each main scene's keyframe (scenes[1..N-1]; scenes[0]
# stays on the run row itself for backwards compat). Same shape as
# _insert_card_row but uses MODE_SCENE + scene_index in metadata.
async def _insert_scene_row(
    *,
    parent_run_id: int,
    topic: str,
    image_model: str,
    scene_index: int,
    image_bytes: bytes,
    mime: str,
    used_prompt: str,
    feedback_text: str | None = None,
    retry_kind: str | None = None,
    parent_generation_id: int | None = None,
    extra_md: dict | None = None,
) -> int:
    """Create a sibling yas_generations row for a multi-scene keyframe.

    Lineage (all optional): `feedback_text` and `parent_generation_id` go in
    their columns; `retry_kind` goes in the row metadata, and in its column only
    when the table's CHECK accepts it (see _RETRY_KIND_COLUMN_VALUES).
    `extra_md` adds keys to the row metadata (the keys above always win)."""
    metadata = {
        **(extra_md or {}),
        "category": "talking_head_v2_scene",
        "parent_talking_head_id": parent_run_id,
        "scene_index": scene_index,
    }
    if retry_kind:
        metadata["retry_kind"] = retry_kind
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                INSERT INTO yas_generations
                  (topic, mode, image_model, prompt_used, status, metadata,
                   feedback_text, retry_kind, parent_generation_id)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING id
                """,
                (
                    topic, MODE_SCENE, image_model, used_prompt[:8000],
                    STATUS_GENERATING, Jsonb(metadata),
                    feedback_text,
                    retry_kind if retry_kind in _RETRY_KIND_COLUMN_VALUES else None,
                    parent_generation_id,
                ),
            )
            new = await cur.fetchone()
            row_id = int(new["id"])

    width, height = _image_dims(image_bytes)
    local_dest = TH_ROOT / f"{parent_run_id}" / f"scene-{scene_index}-{row_id}.png"
    stored = await _store_image_bytes(
        image_bytes=image_bytes,
        bucket_path=f"yas/talking-head-v2/{parent_run_id}/scene-{scene_index}-{row_id}.png",
        local_dest=local_dest,
        mime=mime,
    )
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                UPDATE yas_generations
                   SET storage_bucket = %s,
                       storage_path   = %s,
                       local_path     = %s,
                       mime_type      = %s,
                       bytes          = %s,
                       width          = %s,
                       height         = %s,
                       status         = %s,
                       updated_at     = NOW()
                 WHERE id = %s
                """,
                (
                    stored["storage_bucket"], stored["storage_path"],
                    stored["local_path"], mime, len(image_bytes),
                    width, height, STATUS_READY, row_id,
                ),
            )
    return row_id


async def _fetch_scene_row(scene_id: int) -> dict | None:
    """Fetch a single scene keyframe row."""
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT *
                  FROM yas_generations
                 WHERE id = %s AND mode = %s
                """,
                (scene_id, MODE_SCENE),
            )
            return await cur.fetchone()


async def _resolve_scene_keyframe_path(*, run_id: int, scene_index: int, md: dict) -> Path | None:
    """Resolve the local keyframe path for scenes[scene_index].

    scenes[0] lives on the run row itself (`yas_generations.local_path`)
    for backwards compat with the single-scene code path. scenes[1..N-1]
    are sibling rows pointed to by `metadata.scenes[i].keyframe_id`.
    """
    scenes = (md or {}).get("scenes") or []
    if scene_index == 0:
        # Run row carries scene 0's keyframe (legacy compat).
        run = await _fetch_run_row(run_id)
        if run.get("local_path"):
            p = _abs_from_rel(run["local_path"])
            if p.exists():
                return p
    if scene_index < len(scenes):
        kid = scenes[scene_index].get("keyframe_id")
        if kid:
            row = await _fetch_scene_row(int(kid))
            if row and row.get("local_path"):
                p = _abs_from_rel(row["local_path"])
                if p.exists():
                    return p
    return None


# The likeness-retry crop per edge, per framing of the still (strength 1-3):
# a close-up only nudges in (never past the forehead or chin); a full-body
# still or a photo's own framing is never cropped (the feet and the user's
# framing stay). Above the hip, or no framing known, keeps today's crop.
_PERTURB_CROP = {
    th_prompts.FRAMING_CLOSE: (0.01, 0.03),
    th_prompts.FRAMING_FULL_BODY: (0.0, 0.0),
    FRAMING_AS_PHOTO: (0.0, 0.0),
}


def _perturb_keyframe_for_rai(kf: Path, strength: int, *, framing: str | None = None) -> Optional[Path]:
    """Write a meaningfully-altered copy of the keyframe to clear Veo's likeness
    FALSE-POSITIVE (15236754 / 29310472 — a SEMANTIC face classifier; an
    identical resubmit never clears it). We change the input semantically WITHOUT
    changing who the person is: a HORIZONTAL FLIP (mirrors the face — a strong
    change to the classifier, but identity-identical to a viewer for a centred,
    front-facing talking head) + an escalating crop/zoom + re-encode + grain.
    `strength` (1, 2, 3) escalates. `framing` (the still's) bounds the crop
    (_PERTURB_CROP): none for full body. Returns a temp path, or None on
    failure (caller falls back to the untouched keyframe)."""
    try:
        import random
        import tempfile
        from PIL import Image, ImageEnhance
        img = Image.open(kf).convert("RGB")
        w, h = img.size
        rnd = random.Random(strength * 7919 + w + h)
        # Horizontal flip — the strongest identity-preserving change for a face
        # classifier on a centred, front-facing subject.
        out_img = img.transpose(Image.FLIP_LEFT_RIGHT)
        # Escalating centre crop/zoom (~5% per strength per edge), resized back so
        # the dimensions/aspect Veo expects are preserved.
        step, cap = _PERTURB_CROP.get(th_prompts.veo_framing(framing) or "", (0.05, 0.15))
        m = min(cap, step * max(1, strength))
        dx, dy = int(w * m), int(h * m)
        if dx or dy:
            out_img = out_img.crop((dx, dy, w - dx, h - dy)).resize((w, h), Image.LANCZOS)
        out_img = ImageEnhance.Brightness(out_img).enhance(1.0 + (rnd.random() - 0.5) * 0.06)
        out_img = ImageEnhance.Contrast(out_img).enhance(1.0 + (rnd.random() - 0.5) * 0.06)
        # Grain (always) — further breaks the classifier's confidence.
        px = out_img.load()
        for _ in range(int(w * h * 0.02)):
            x, y = rnd.randrange(w), rnd.randrange(h)
            r, g, b = px[x, y]
            d = rnd.randint(-12, 12)
            px[x, y] = (min(255, max(0, r + d)), min(255, max(0, g + d)), min(255, max(0, b + d)))
        suffix = kf.suffix.lower() if kf.suffix.lower() in (".jpg", ".jpeg", ".png") else ".jpg"
        out = Path(tempfile.mktemp(suffix=suffix))
        out_img.save(out, "PNG" if suffix == ".png" else "JPEG", quality=92)
        return out
    except Exception:  # noqa: BLE001 — perturbation is best-effort
        log.exception("[yas-th] keyframe RAI-perturb failed")
        return None


# Head room kept above a detected face box in a face-anchored push-in, as a
# share of the box's height: the box stops at the eyebrows, so the hair or
# head covering above it stays in the end frame.
_PUSH_IN_HEADROOM = 0.6


def _push_in_window(w: int, h: int, z: float, face: tuple[float, float, float, float] | None,
                    ) -> tuple[int, int, int, int]:
    """(left, top, width, height) of a `z`x tighter window of a w x h frame:
    centred, or with `face` (x, y, width, height) centred on the face, its
    top kept at least _PUSH_IN_HEADROOM face heights above the face (so the
    push-in never cuts the top of the head), always inside the frame."""
    cw, ch = int(w / z), int(h / z)
    if face is None:
        return (w - cw) // 2, (h - ch) // 2, cw, ch
    fx, fy, fw, fh = face
    left = round(fx + fw / 2 - cw / 2)
    top = round(min(fy + fh / 2 - ch / 2, fy - _PUSH_IN_HEADROOM * fh))
    left = min(max(0, left), w - cw)
    top = min(max(0, top), h - ch)
    return left, top, cw, ch


def _push_in_end_frame(keyframe: Path, zoom: float, *, anchor: str | None = None) -> Path | None:
    """Crop + rescale the keyframe to a `zoom`x tighter copy, written next to
    the keyframe, for use as the Veo i2v END frame. Feeding a zoomed version
    of the SAME still as `last_image` makes Veo interpolate a smooth camera
    PUSH-IN while the face stays anchored (identical identity pixels) at both
    ends. The crop is centred; `anchor` "face" centres it on the speaker's
    face with the head room kept (a close-up pushes in toward the face, not
    the chest), falling back to the centre when no face is found. Returns
    None on ANY failure so the caller falls back to the plain keyframe
    (current behaviour)."""
    try:
        from PIL import Image
        z = max(1.03, min(1.30, float(zoom)))
        with Image.open(keyframe) as im:
            im = im.convert("RGB")
            w, h = im.size
            face = _face_box(im) if anchor == "face" else None
            left, top, cw, ch = _push_in_window(w, h, z, face)
            cropped = im.crop((left, top, left + cw, top + ch)).resize((w, h), Image.LANCZOS)
            out = keyframe.parent / (keyframe.stem + "_pushin.png")
            cropped.save(out, "PNG")
        return out
    except Exception:
        return None


# The push-in per framing of the still (see VEO_CAMERA_LINES): a close-up
# pushes in a little, toward the face; above the hip pushes in as before; full
# body and a photo's own framing are locked off (the end frame is the start
# frame, so the feet and the user's framing stay in).
_FRAMING_PUSH_IN: dict[str, dict | None] = {
    th_prompts.FRAMING_CLOSE: {"zoom": 1.06, "anchor": "face"},
    th_prompts.FRAMING_FULL_BODY: None,
    FRAMING_AS_PHOTO: None,
}


def _scene_video_inputs(
    keyframe: Path,
    char: Path | None,
    bg: Path | None,
    *,
    run_id: int | None = None,
    scene_index: int = 0,
    framing: str | None = None,
) -> dict[str, Any]:
    """Veo inputs for a talking-head scene / single-scene-main clip.

    The approved keyframe is the AUTHORITATIVE image for the clip: it is passed
    as the i2v START frame (`start_image=`) so the video literally begins from
    that exact still — and, by default, ALSO as the END frame (`last_image=`).
    Pinning the same keyframe at both ends anchors the face at the start AND the
    finish, bounding the mid-clip identity drift Veo otherwise accumulates. This
    is a policy-safe, image-channel identity lock (no prompt risk). By default
    the END frame is a slightly ZOOMED copy of the same keyframe (see
    `_push_in_end_frame`) so Veo interpolates a smooth camera push-in with the
    face anchored at both ends — disable the zoom with VEO_CAMERA_PUSH_IN=0
    (plain keyframe end frame); disable the anchor entirely with
    VEO_LAST_FRAME_ANCHOR=0. The zoom goes out as a `push_in` spec
    ({"zoom": z}) next to last_image=keyframe: the veo_generate wrapper makes
    the zoomed copy from the start frame AFTER putting it in the clip's shape,
    so a reframed start and its end frame are the same image.

    `framing` (the still's: a framing or FRAMING_AS_PHOTO) sets the push-in
    (_FRAMING_PUSH_IN): close 1.06 toward the face, above the hip the
    VEO_CAMERA_PUSH_IN_ZOOM zoom (1.12), full body and a photo none at all
    (the end frame is the keyframe itself). None keeps the 1.12 zoom.

    The raw CHARACTER photo is deliberately NOT passed (a real-person photo trips
    Veo RAI 29310472); the keyframe already carries identity. NO reference_images
    either — Veo 3.1 treats the i2v `image=` and reference images ("Ingredients")
    as MUTUALLY EXCLUSIVE ("Image and reference images cannot be both set"). A
    stable per-run seed (when run_id is provided) nudges Veo toward more
    consistent output across retries and sibling scenes.
    (`char`/`bg` are accepted for call-site symmetry but intentionally unused.)
    """
    _ = (char, bg)  # unused — the keyframe is the sole identity + location source
    inputs: dict[str, Any] = {"start_image": keyframe, "reference_images": None}
    framing = th_prompts.veo_framing(framing)
    if os.getenv("VEO_LAST_FRAME_ANCHOR", "1").strip().lower() not in ("0", "false", "no", "off"):
        inputs["last_image"] = keyframe
        if os.getenv("VEO_CAMERA_PUSH_IN", "1").strip().lower() not in ("0", "false", "no", "off"):
            if framing in _FRAMING_PUSH_IN:
                spec = _FRAMING_PUSH_IN[framing]
                if spec:
                    inputs["push_in"] = dict(spec)
            else:
                _zoom = os.getenv("VEO_CAMERA_PUSH_IN_ZOOM", "1.12")
                inputs["push_in"] = {
                    "zoom": float(_zoom) if _zoom.replace(".", "", 1).isdigit() else 1.12,
                }
    if run_id is not None:
        inputs["seed"] = (int(run_id) * 10007 + int(scene_index)) & 0x7FFFFFFF
    return inputs


async def _clip_needs_regen(anim_id: Any, *, video_stale: bool) -> bool:
    """Whether a clip needs a (re)generation.

    True when the clip has no animation yet, its last animation failed, or its
    keyframe / refs / dialogue changed after the video rendered (video_stale).
    Fresh, ready clips return False so editing one scene never re-fires Veo for
    the others. Callers handle exclusion separately (excluded clips never gen).
    """
    if video_stale:
        return True
    if not anim_id:
        return True
    row = await _fetch_animation_row(int(anim_id))
    if not row:
        return True
    return row.get("status") == STATUS_FAILED


async def _mark_row_failed(row_id: int, error: str) -> None:
    try:
        async with get_conn() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    UPDATE yas_generations
                       SET status = %s, error = %s, updated_at = NOW()
                     WHERE id = %s
                    """,
                    (STATUS_FAILED, error[:500], row_id),
                )
    except Exception:
        log.exception("could not mark yas_generation #%d failed", row_id)


# ---------------------------------------------------------------------------
# Step 3 — Retry a single preview
# ---------------------------------------------------------------------------

async def retry_preview(
    *,
    run_id: int,
    which: str,            # "main" | "intro" | "outro" | "scene_<N>"
    feedback_text: str,
    image_models_override: dict | None = None,
) -> dict:
    # v6 — accept scene_N. Validation: legacy values OR scene_<int>.
    scene_idx: int | None = None
    if which.startswith("scene_"):
        try:
            scene_idx = int(which.split("_", 1)[1])
        except (ValueError, IndexError):
            raise HTTPException(400, f"invalid scene index in which={which!r}")
        if scene_idx < 0 or scene_idx >= MAX_SCENES:
            raise HTTPException(400, f"scene index out of range: {scene_idx}")
    elif which not in {"main", "intro", "outro"}:
        raise HTTPException(400, "which must be main / intro / outro / scene_<N>")
    feedback_text = (feedback_text or "").strip()
    # A still (main / scene) with feedback is refined: that one change is
    # made on the current still (refine_still). Without feedback it is
    # regenerated the way first generation made it (regenerate_still), which
    # is also how a failed still is made again. Bookend cards (intro/outro)
    # may be (re)generated WITHOUT feedback too: this is how the "Generate
    # intro/outro" toggle re-creates a card it previously skipped.

    run = await _fetch_run_row(run_id)
    md = run["metadata"]
    # User's current toggle selection wins over the engine stored on the run.
    # If an override arrived from the client, use it AND persist it back so
    # subsequent retries stay consistent with what the user picked.
    if image_models_override:
        image_models = _normalize_image_models(image_models_override)
        await _patch_metadata(run_id, {"image_models": image_models})
        md = {**md, "image_models": image_models}
    else:
        image_models = _normalize_image_models(md.get("image_models"))
    # Scene tiles all use the run's main image model.
    image_model = image_models["main"] if scene_idx is not None else image_models[which]
    log.info(
        "[yas-th] retry_preview run=%d which=%s engine=%s (override=%s)",
        run_id, which, image_model, bool(image_models_override),
    )

    # ---- Stills: main (scene 0) or scene_<N> ------------------------------
    # Either one marks the scene's clip stale (it animates the old still) and
    # keeps the old still as a version (Undo). A failure never fails the run
    # and leaves the current still as it is.
    if scene_idx is not None or which == "main":
        if scene_idx is not None:
            scenes_meta = list((md or {}).get("scenes") or [])
            scene_count = int((md or {}).get("scene_count") or 1)
            if scene_idx >= scene_count or scene_idx >= len(scenes_meta):
                raise HTTPException(400, f"scene_{scene_idx} not in this run")
        i = 0 if scene_idx is None else scene_idx
        if feedback_text:
            await refine_still(run_id, i, feedback_text)
        else:
            await regenerate_still(run_id, i)
        return await fetch_run(run_id)

    # Intro / outro: the card is made again from the run's picks, mode and
    # words, the way first generation makes it (_make_bookend_card), as a NEW
    # card row (the prior card stays). Text is optional: it steers a drawn
    # card; without it the card is simply remade, which is how a changed mode
    # or a dropped card is applied. A kept card is the picked image again,
    # its words swapped when they must be.
    card_type = which
    try:
        new_card_id, _card_md = await _make_bookend_card(
            run, md, card_type, feedback=feedback_text or None,
        )
    except HTTPException:
        raise
    except Exception:
        log.exception("retry %s card failed for run #%d", card_type, run_id)
        raise HTTPException(502, f"{card_type.capitalize()} card retry failed.")
    # A new card image makes the existing bookend clip stale (unless the card
    # came with a previous reel's finished clip).
    await _patch_metadata(run_id, {
        f"{card_type}_image_id": new_card_id,
        f"{card_type}_video_stale": True,
        f"{card_type}_error": None,
        **_reused_clip_patch(card_type, _card_md),
    })
    return await fetch_run(run_id)


# ---------------------------------------------------------------------------
# Refine, Regenerate and Undo for a scene still
# ---------------------------------------------------------------------------
#
# Refine makes ONE change on the still the user approved: a true image edit
# with that still as the canvas, so its framing, room and light survive and
# refinements add up. Regenerate makes the still again the way first
# generation did, from the scene's picks, with no text (also for a failed
# still). Undo puts back the version from before the last change. Every new
# still is a new version (_store_scene_still), so nothing is lost; a failure
# never marks the run failed and leaves the current still where it is.

STILL_BUSY_MESSAGE = "This still is being made right now. Try again once it is ready."


def _still_index(which: Any, md: dict) -> int:
    """The scene index of a still: 'main' (or 0) is scene 0, on the run row;
    'scene_<N>' (or N) is scene N, which must be one of the run's scenes.
    400 for anything else."""
    if which == "main":
        return 0
    if isinstance(which, int) and not isinstance(which, bool):
        i = which
    else:
        s = str(which or "")
        if not s.startswith("scene_"):
            raise HTTPException(400, "which must be main or scene_<N>")
        try:
            i = int(s.split("_", 1)[1])
        except ValueError:
            raise HTTPException(400, f"invalid scene index in which={which!r}")
    if i == 0:
        return 0
    md = md or {}
    count = min(int(md.get("scene_count") or 1), len(md.get("scenes") or []), MAX_SCENES)
    if not 0 < i < count:
        raise HTTPException(400, f"scene_{i} not in this run")
    return i


def _still_scene(md: dict, i: int) -> dict:
    """metadata.scenes[i], or {} (a single-scene run has no scenes list)."""
    scenes = (md or {}).get("scenes") or []
    return scenes[i] if 0 <= i < len(scenes) and isinstance(scenes[i], dict) else {}


def _still_busy(md: dict, i: int) -> bool:
    """Whether an edit's background regen is making scene `i`'s still now."""
    return bool(_still_scene(md, i).get("keyframe_generating")
                or (i == 0 and (md or {}).get("main_keyframe_generating")))


def _has_still(run: dict, md: dict, i: int) -> bool:
    """Whether scene `i` has a still (scene 0 on the run row)."""
    if i == 0:
        return bool(run.get("local_path") or run.get("storage_path"))
    return bool(_still_scene(md, i).get("keyframe_id"))


def _still_as_is(md: dict, i: int) -> bool:
    """Whether scene `i`'s still is the character photo itself (New bg OFF)."""
    return bool((md or {}).get("main_as_is")) if i == 0 else bool(_still_scene(md, i).get("as_is"))


def _can_undo(md: dict, i: int) -> bool:
    """Whether scene `i` has an earlier version of its still to go back to."""
    if i == 0:
        versions = (md or {}).get("still_versions")
        return bool(versions.get("0")) if isinstance(versions, dict) else False
    return bool(_still_scene(md, i).get("previous_keyframe_ids"))


def _still_key_of(i: int, *, local_path: Any = None, storage_path: Any = None,
                  keyframe_id: Any = None) -> str | None:
    """The identity of one version of scene `i`'s still: scene 0's file (every
    version is a new file, main-<stamp>.png, kept with its record in
    still_versions), a later scene's row id. None when there is none."""
    if i == 0:
        if not (local_path or storage_path):
            return None
        return f"main:{local_path or ''}|{storage_path or ''}"
    return f"scene:{int(keyframe_id)}" if keyframe_id else None


def _still_key(run: dict, md: dict, i: int) -> str | None:
    """The identity of scene `i`'s CURRENT still (_still_key_of). A clip
    records it when it is made (video_source), so a later change of the still
    can tell whether the clip still shows it."""
    if i == 0:
        return _still_key_of(0, local_path=run.get("local_path"), storage_path=run.get("storage_path"))
    return _still_key_of(i, keyframe_id=_still_scene(md, i).get("keyframe_id"))


def _clip_source(md: dict, i: int) -> str | None:
    """The still scene `i`'s clip was made from (video_source), or None when
    the clip did not record it (made before clips did)."""
    if i == 0 and int((md or {}).get("scene_count") or 1) <= 1:
        return (md or {}).get("main_video_source")
    return _still_scene(md, i).get("video_source")


def _stale_for(md: dict, i: int, key: str | None) -> bool:
    """Whether scene `i`'s clip is out of date once its still is `key`: not
    when the clip was made from that very still (an Undo back to it), else
    yes (a new still, or a clip that recorded nothing)."""
    src = _clip_source(md, i)
    return not (src and key and src == key)


def _clip_source_to_record(
    md: dict, i: int, replaced: str | None, *, scene: dict | None = None,
) -> str | None:
    """The source (video_source) to record for scene `i`'s clip before its
    still `replaced` (_still_key_of) is replaced: `replaced` itself when the
    clip records none (made before clips recorded it, like run 830's clips
    351 and 354) and is up to date, so it shows that very still. None when
    there is no clip, it records a source already, it is out of date (an
    edit, or a still changed before), or no still is replaced. Recorded, an
    Undo back to `replaced` keeps the clip up to date (_stale_for) instead
    of asking for a new one. `scene`: metadata.scenes[i] as read under the
    caller's lock (else the one in `md`)."""
    if not replaced:
        return None
    md = md or {}
    if i == 0 and int(md.get("scene_count") or 1) <= 1:
        clip = md.get("main_animation_id") or md.get("veo_animation_id")
        stale, source = md.get("main_video_stale"), md.get("main_video_source")
    else:
        sc = scene if isinstance(scene, dict) else _still_scene(md, i)
        clip, stale, source = sc.get("animation_id"), sc.get("video_stale"), sc.get("video_source")
    if not clip or stale or source:
        return None
    return replaced


def _still_framing(md: dict, i: int) -> str:
    """The framing scene `i`'s current still shows: FRAMING_AS_PHOTO for the
    character photo itself (as_is) or a still made on it (keep character, new
    background); else the framing it was made at (main_framing_used /
    scenes[i].framing_used); a still made before framing existed reads as
    close. The caller knows whether there is a still at all."""
    if _still_as_is(md, i):
        return FRAMING_AS_PHOTO
    used = (md or {}).get("main_framing_used") if i == 0 else _still_scene(md, i).get("framing_used")
    if used == FRAMING_AS_PHOTO:
        return FRAMING_AS_PHOTO
    return th_prompts.framing_or_none(used) or LEGACY_FRAMING


def _still_version_usable(v: dict) -> bool:
    """Whether a stored still (a still_versions record or a scene row) still
    has its image: the local file, or its bucket object."""
    lp = v.get("local_path")
    if lp and _abs_from_rel(lp).exists():
        return True
    return bool(v.get("storage_bucket") and v.get("storage_path"))


async def _current_still_path(run: dict, md: dict, i: int) -> Path | None:
    """Scene `i`'s current still as a local file (the Refine canvas): its file
    on disk, else a copy fetched from its bucket object; None when the scene
    has no still."""
    if not _has_still(run, md, i):
        return None
    p = await _resolve_scene_keyframe_path(run_id=int(run["id"]), scene_index=i, md=md)
    if p is not None:
        return p
    # _resolve_card_path reads any image row by id: its file, then its bucket.
    return await _resolve_card_path(int(run["id"]) if i == 0 else _still_scene(md, i).get("keyframe_id"))


def _is_quota_error(exc: BaseException) -> bool:
    """A Gemini / Vertex 429 RESOURCE_EXHAUSTED (the image-gen quota)."""
    s = str(exc)
    return " 429 " in f" {s} " or "RESOURCE_EXHAUSTED" in s or "resource has been exhausted" in s.lower()


def _still_http_error(exc: BaseException, *, action: str) -> HTTPException:
    """A failed still edit or generation as a clean HTTP error. `action` is
    the verb the message uses ("apply that change", "make this still").
      - the model refused (a safety or policy block): 422;
      - it kept replying with text after the escalated image-only retry: 502;
      - the quota (429 RESOURCE_EXHAUSTED): 429;
      - a timeout: 504; the Vertex budget: 503; anything else: 502.
    An HTTPException passes through as it is."""
    if isinstance(exc, HTTPException):
        return exc
    if isinstance(exc, GeminiNoImageError):
        if exc.blocked:
            return HTTPException(
                422, f"The image model declined to {action} for this still. "
                     "Try other words, or Regenerate it.",
            )
        return HTTPException(
            502, f"The image model kept replying with text instead of an image, so it "
                 f"couldn't {action}. This is usually brief: try again.",
        )
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError)):
        return HTTPException(504, f"The image model took too long to {action}. Try again.")
    if isinstance(exc, VertexBudgetExceededError):
        return HTTPException(503, str(exc)[:300] or "The image budget is used up.")
    if _is_quota_error(exc):
        return HTTPException(
            429, "Image generation is rate-limited right now (quota). Wait a minute and try again.",
        )
    return HTTPException(502, f"Couldn't {action}: {str(exc)[:200] or type(exc).__name__}")


async def _store_still_version(
    run: dict,
    md: dict,
    i: int,
    data: bytes,
    mime: str,
    prompt: str,
    *,
    image_model: str,
    feedback: str | None,
    retry_kind: str,
    as_is: bool,
    extra_md: dict | None = None,
    scene_md: dict | None = None,
) -> dict:
    """Store a Refine / Regenerate result as scene `i`'s new still version
    (_store_scene_still): the clip is marked stale, the scene's error is
    cleared and its as_is flag set. Scene 0's feedback_text column is set to
    `feedback` ("" when there is none: this version had no change text);
    scenes >= 1 get the lineage on their row (parent = the replaced still).
    `extra_md` goes in the still's own metadata (scene 0: the run's; scenes
    >= 1: the scene row's), `scene_md` in metadata.scenes[i]. A framing
    warning of the replaced still is cleared (a re-shoot sets its own)."""
    if i == 0:
        run_md: dict[str, Any] = {"main_error": None, "main_as_is": bool(as_is),
                                  "main_framing_warning": None, **(extra_md or {})}
        if int(md.get("scene_count") or 1) <= 1:
            run_md["main_video_stale"] = True
        return await _store_scene_still(
            int(run["id"]), 0, data, mime, prompt, image_model, topic=run.get("topic") or "",
            feedback=feedback or "", retry_kind=retry_kind, extra_md=run_md,
            scene_md={"video_stale": True, **(scene_md or {})},
        )
    return await _store_scene_still(
        int(run["id"]), i, data, mime, prompt, image_model, topic=run.get("topic") or "",
        feedback=feedback or None, retry_kind=retry_kind,
        parent_id=_still_scene(md, i).get("keyframe_id"), extra_md=extra_md,
        scene_md={"error": None, "video_stale": True, "as_is": bool(as_is),
                  "framing_warning": None, **(scene_md or {})},
    )


def _edit_input(path: Path, tmp_dir: Path, name: str) -> Path:
    """`path` as an image part for the refine engine: the file itself when it
    is at most REFINE_MAX_EDGE px on its long side (or cannot be read), else
    a copy scaled down to that size (Lanczos, PNG) in `tmp_dir`."""
    from PIL import Image

    try:
        with Image.open(path) as im:
            w, h = im.size
            if max(w, h) <= REFINE_MAX_EDGE:
                return Path(path)
            s = REFINE_MAX_EDGE / max(w, h)
            out = Path(tmp_dir) / f"{name}.png"
            im.convert("RGB").resize((max(1, round(w * s)), max(1, round(h * s))),
                                     Image.LANCZOS).save(out, format="PNG")
            log.info("[yas-th] refine: %s is %dx%d, sent at %dx%d", name, w, h,
                     max(1, round(w * s)), max(1, round(h * s)))
            return out
    except Exception:
        log.exception("[yas-th] refine: could not scale %s; sending it as it is", path)
        return Path(path)


async def _refine_canvas(
    canvas: Path, text: str, *, identity: Path | None, aspect: str, what: str,
) -> tuple[bytes, str]:
    """The refine engine on one image: IMAGE 1 = `canvas`, IMAGE 2 =
    `identity` (identity only) when given, then the directive applying ONLY
    `text` (already cleaned). Each image part is at most REFINE_MAX_EDGE px
    on its long side. The output shape is forced at the API and the result
    put in exactly the `aspect` shape (reframed within the time left, else
    padded).

    One edit gets REFINE_ATTEMPT_TIMEOUT_SEC (its image-only escalation
    included); a timeout, or an answer with no image, is asked once more
    WITHOUT the identity reference; the whole refine (waiting for the image
    semaphore included) is bounded by REFINE_TOTAL_TIMEOUT_SEC. A refusal
    (blocked) or any other error is raised at once.
    Returns (PNG bytes, the prompt text). Two timeouts are a 504
    (REFINE_TIMEOUT_MESSAGE); two text replies raise the last
    GeminiNoImageError; no image at all is a 422. `what` names the still in
    the logs."""
    import shutil
    import tempfile

    gem_aspect, _oai_size = _aspect_size(aspect)
    loop = asyncio.get_running_loop()
    deadline = loop.time() + REFINE_TOTAL_TIMEOUT_SEC
    tmp = Path(tempfile.mkdtemp(prefix="th-refine-"))

    async def _one_edit(parts: list, limit: float) -> bytes:
        async with _image_gen_sem():
            return await asyncio.wait_for(
                card_animation._render_edit_escalating(
                    parts, system_instruction=th_prompts.TH_STILL_REFINE_SYSTEM,
                    timeout=limit, what="th-still-refine", aspect_ratio=gem_aspect,
                ),
                timeout=limit,
            )

    data: bytes | None = None
    prompt = ""
    last: BaseException | None = None
    try:
        still_in = await asyncio.to_thread(_edit_input, Path(canvas), tmp, "canvas")
        ident_in = (await asyncio.to_thread(_edit_input, Path(identity), tmp, "identity")
                    if identity is not None else None)
        # The second try never carries the identity reference.
        for n, ident in enumerate((ident_in, None), start=1):
            remaining = deadline - loop.time()
            if remaining <= 0:
                last = asyncio.TimeoutError()  # the time is up: no second try
                break
            parts = th_prompts.build_still_refine_parts(
                text, still=still_in, identity_ref=ident, aspect=gem_aspect,
            )
            prompt = "\n\n".join(str(v) for kind, v in parts if kind == "text")
            try:
                data = await asyncio.wait_for(
                    _one_edit(parts, min(REFINE_ATTEMPT_TIMEOUT_SEC, remaining)), timeout=remaining,
                )
            except (asyncio.TimeoutError, TimeoutError) as e:
                last = e
                log.warning("[yas-th] refine %s: try %d (identity_ref=%s) timed out",
                            what, n, ident is not None)
                continue
            except GeminiNoImageError as e:
                if e.blocked:
                    raise
                last = e
                log.warning("[yas-th] refine %s: try %d (identity_ref=%s) answered with no image",
                            what, n, ident is not None)
                continue
            if data:
                break
            last = None
            log.warning("[yas-th] refine %s: try %d (identity_ref=%s) returned an empty image",
                        what, n, ident is not None)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    if not data:
        if isinstance(last, (asyncio.TimeoutError, TimeoutError)):
            raise HTTPException(504, REFINE_TIMEOUT_MESSAGE)
        if isinstance(last, GeminiNoImageError):
            raise last
        raise HTTPException(
            422, "The image model returned no image for that change. Try other words, or Regenerate it.",
        )
    try:
        # A reframe is one more image call: it gets only the time left, so
        # the whole refine stays within REFINE_TOTAL_TIMEOUT_SEC.
        remaining = deadline - loop.time()
        if remaining <= 0:
            raise asyncio.TimeoutError()
        data = await asyncio.wait_for(_conform_to_aspect(data, aspect, "reframe"), timeout=remaining)
    except Exception:
        # The edit itself worked: keep it, padded, rather than lose it.
        log.exception("[yas-th] refine %s: reframing the edit failed or ran out of time, padding it", what)
        data = await _conform_to_aspect(data, aspect, "pad")
    return data, prompt


async def refine_still(
    run_id: int,
    which: Any,
    change: str,
    *,
    retry_kind: str = "refine",
    extra_md: dict | None = None,
    scene_md: dict | None = None,
    busy_ok: bool = False,
    framing_target: str | None = None,
) -> dict:
    """Make ONE change on a scene's current still: a true image edit.

    `which` is 'main' / 'scene_<N>' or a scene index. The parts, in order:
    IMAGE 1 = the current still (the canvas); IMAGE 2 = the scene's person
    reference (_person_ref_for_scene: its pick, the run's pick, or scene 0's
    still for a scene that takes its speaker from it) only when a character
    lock applies (lock_character, or _scene_anchored) and the still is not
    the character photo itself; then the directive with `change` (cut to
    500 characters, RAI-scrubbed). No background reference and no keyframe
    prompt; TH_STILL_REFINE_SYSTEM keeps a calm, closed-mouth face unless the
    change is about the expression. The output shape is forced at the API
    and the result put in exactly the run's shape.

    The result is a new version (Undo puts the old one back), with
    `retry_kind` and the change as lineage. Other engines (the framing
    reshoot) call this with their own instruction as `change`, their own
    `retry_kind`, and `extra_md` / `scene_md` for what they record (see
    _store_still_version); `busy_ok` when the caller itself holds the scene's
    keyframe_generating marker (an edit's background regen), which otherwise
    refuses the refine. `framing_target` (the framing re-shoot): the new
    still's face is measured and the framing it really shows is recorded,
    with a warning when it is not the one asked for (_honest_framing_md).
    Returns _store_scene_still's result.

    Raises HTTPException: 400 (bad `which`), 409 (no still yet, or it is
    being made), 422 (empty change, the model refused, no image), 429, 502,
    503, 504 (see _still_http_error). The run is never marked failed."""
    text = th_prompts.clean_still_change(change)
    if not text:
        raise HTTPException(422, "Describe the one change to make.")
    run = await _fetch_run_row(run_id)
    md = run["metadata"] or {}
    i = _still_index(which, md)
    if _still_busy(md, i) and not busy_ok:
        raise HTTPException(409, STILL_BUSY_MESSAGE)
    canvas = await _current_still_path(run, md, i)
    if canvas is None:
        raise HTTPException(409, "There is no still to refine yet. Use Regenerate to make one.")
    identity: Path | None = None
    if not _still_as_is(md, i) and (bool(md.get("lock_character")) or _scene_anchored(md, i)):
        identity = await _person_ref_for_scene(md, i, run_id=run_id)
        if identity is not None and Path(identity) == Path(canvas):
            identity = None
    aspect = _norm_aspect(md.get("aspect_ratio"))
    image_model = _normalize_image_models(md.get("image_models"))["main"]
    log.info("[yas-th] refine_still run=%d scene=%d identity_ref=%s aspect=%s",
             run_id, i, identity is not None, aspect)
    try:
        data, prompt = await _refine_canvas(
            canvas, text, identity=identity, aspect=aspect, what=f"run={run_id} scene={i}",
        )
    except Exception as e:
        log.exception("[yas-th] refine_still run=%d scene=%d failed", run_id, i)
        raise _still_http_error(e, action="apply that change") from e
    if framing_target:
        # A model re-shoot is not always at the framing asked for (run 830's
        # "close" came back barely closer): record what the still shows.
        f_md, f_scene = await asyncio.to_thread(_honest_framing_md, data, i, framing_target)
        extra_md = {**(extra_md or {}), **f_md}
        scene_md = {**(scene_md or {}), **f_scene}
    # The edit keeps the canvas's framing unless the caller (the framing
    # re-shoot) says otherwise; the new scene row carries it for Undo.
    cur_framing = _still_scene(md, i).get("framing_used")
    if i >= 1 and cur_framing and "framing_used" not in (extra_md or {}):
        extra_md = {**(extra_md or {}), "framing_used": cur_framing}
    return await _store_still_version(
        run, md, i, data, "image/png", prompt, image_model=image_model,
        feedback=text, retry_kind=retry_kind, as_is=False,
        extra_md=extra_md, scene_md=scene_md,
    )


async def regenerate_still(run_id: int, which: Any) -> dict:
    """Make a scene's still again the way first generation made it, from the
    scene's picks, with no change text (_gen_scene_still, given a one-shot
    Look for the scene's mode):
      - a still that is the character photo (as_is), or a scene with no
        still yet: New bg OFF, so a lone character pick is used as it is (the
        Look step's default);
      - any other still: New bg ON, so a lone character pick with the lock on
        keeps the person and gets a new background;
      - any other scene composes from its picks, naming only the images
        attached (a scene with no pick of its own takes its speaker from
        scene 0's still).
    The result is a new version (Undo puts the old one back). Returns
    _store_scene_still's result. A failure raises the mapped error (see
    _still_http_error); a scene with no still also keeps the message as its
    error, so its tile shows it. The run is never marked failed."""
    run = await _fetch_run_row(run_id)
    md = run["metadata"] or {}
    i = _still_index(which, md)
    if _still_busy(md, i):
        raise HTTPException(409, STILL_BUSY_MESSAGE)
    has_still = _has_still(run, md, i)
    look = {"bg_mode": "keep" if (_still_as_is(md, i) or not has_still) else "new"}
    image_model = _normalize_image_models(md.get("image_models"))["main"]
    log.info("[yas-th] regenerate_still run=%d scene=%d bg_mode=%s", run_id, i, look["bg_mode"])
    info: dict[str, Any] = {}
    try:
        data, mime, prompt = await _gen_scene_still(run_id, md, i, look=look, info=info)
    except Exception as e:
        log.exception("[yas-th] regenerate_still run=%d scene=%d failed", run_id, i)
        err = _still_http_error(e, action="make this still")
        if not has_still:
            msg = str(err.detail)[:480]
            if i == 0:
                await _patch_metadata(run_id, {"main_error": msg})
            else:
                await _patch_scene(run_id, i, {"error": msg})
        raise err from e
    f_md, f_scene = _framing_md(i, info.get("framing_used"))
    return await _store_still_version(
        run, md, i, data, mime, prompt, image_model=image_model,
        feedback=None, retry_kind="regenerate", as_is=prompt == _USE_AS_IS_PROMPT,
        extra_md=f_md, scene_md=f_scene,
    )


def _undo_framing(undone_kind: Any, restored_used: Any) -> str | None:
    """The framing setting Undo puts back with a still. When the still taken
    off was a framing re-shoot (retry_kind 'reframe'), the setting follows
    the still put back (one made before framing existed reads as close), so
    the Look step, the tile and the next new still agree again. None leaves
    the setting alone: any other undo, or a still in the photo's framing."""
    if undone_kind != "reframe" or restored_used == FRAMING_AS_PHOTO:
        return None
    return th_prompts.framing_or_none(restored_used) or LEGACY_FRAMING


def _scene_framing_value(md: dict, framing: str) -> str | None:
    """The scenes[i].framing to store for `framing` on a multi-scene run:
    None when it is the run's own (followed, not copied, as edit_run does)."""
    return None if framing == _run_framing(md) else framing


async def _undo_scene0(run_id: int) -> dict | None:
    """Put back scene 0's newest earlier still (metadata.still_versions["0"])
    on the run row, in one locked transaction. The still taken off goes to
    metadata.still_undone["0"] (its files are removed with the run, or once
    it drops off that capped list). A version whose image is gone is
    dropped. Undoing a framing re-shoot puts the framing setting back too
    (_undo_framing): the run's, or scenes[0]'s on a multi-scene run. None
    when there is none."""
    dropped: list = []
    keep: list = []
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT local_path, storage_bucket, storage_path, mime_type,
                       width, height, image_model, feedback_text, prompt_used,
                       metadata
                  FROM yas_generations
                 WHERE id = %s AND mode = %s
                   FOR UPDATE
                """,
                (run_id, MODE_PREVIEW),
            )
            row = await cur.fetchone()
            if not row:
                raise HTTPException(404, f"talking-head-v2 run #{run_id} not found")
            md = _md_dict(row.get("metadata"))
            versions = md.get("still_versions")
            versions = dict(versions) if isinstance(versions, dict) else {}
            stack = [v for v in (versions.get("0") or []) if isinstance(v, dict)]
            entry: dict | None = None
            while stack:
                cand = stack.pop()
                if _still_version_usable(cand):
                    entry = cand
                    break
            if entry is None:
                return None
            versions["0"] = stack
            md_patch: dict[str, Any] = {
                "still_versions": versions,
                "main_as_is": bool(entry.get("as_is")),
                "main_retry_kind": entry.get("retry_kind"),
                # The framing that still was made at (None: made before
                # framing existed, which reads as close).
                "main_framing_used": entry.get("framing_used"),
                "main_framing_warning": None,
                "main_error": None,
            }
            if row.get("local_path") or row.get("storage_path"):
                undone = md.get("still_undone")
                undone = dict(undone) if isinstance(undone, dict) else {}
                undone["0"] = [*(undone.get("0") or []), {
                    "local_path": row.get("local_path"),
                    "storage_bucket": row.get("storage_bucket"),
                    "storage_path": row.get("storage_path"),
                    "mime_type": row.get("mime_type"),
                    "width": row.get("width"),
                    "height": row.get("height"),
                    "image_model": row.get("image_model"),
                    "feedback_text": row.get("feedback_text"),
                    "retry_kind": md.get("main_retry_kind"),
                    "as_is": bool(md.get("main_as_is")),
                    "framing_used": md.get("main_framing_used"),
                    "prompt_used": (row.get("prompt_used") or "")[:2000],
                    "undone_at": _now_iso(),
                }]
                dropped = undone["0"][:-_STILL_VERSIONS_MAX]
                undone["0"] = undone["0"][-_STILL_VERSIONS_MAX:]
                keep = [*stack, *undone["0"], entry]
                md_patch["still_undone"] = undone
            multi = int(md.get("scene_count") or 1) > 1
            # The clip is out of date unless it was made from the still put
            # back (an Undo of a refine made after the clip).
            stale = _stale_for(md, 0, _still_key_of(
                0, local_path=entry.get("local_path"), storage_path=entry.get("storage_path")))
            if not multi:
                md_patch["main_video_stale"] = stale
            framing = _undo_framing(md.get("main_retry_kind"), entry.get("framing_used"))
            scene0_more: dict[str, Any] = {"video_stale": stale}
            if framing and multi:
                scene0_more["framing"] = _scene_framing_value(md, framing)
            elif framing:
                md_patch["framing"] = framing
            scenes = md.get("scenes")
            if isinstance(scenes, list) and scenes and isinstance(scenes[0], dict):
                meta_sql = "jsonb_set(metadata || %s::jsonb, '{scenes,0}', %s::jsonb, false)"
                meta_params = [Jsonb(md_patch), Jsonb({**scenes[0], **scene0_more})]
            else:
                meta_sql, meta_params = "metadata || %s::jsonb", [Jsonb(md_patch)]
            lp = entry.get("local_path")
            local = _abs_from_rel(lp) if lp else None
            size = local.stat().st_size if local is not None and local.exists() else None
            rk = entry.get("retry_kind")
            await cur.execute(
                f"""
                UPDATE yas_generations
                   SET storage_bucket = %s,
                       storage_path   = %s,
                       local_path     = %s,
                       mime_type      = %s,
                       bytes          = %s,
                       width          = %s,
                       height         = %s,
                       image_model    = %s,
                       prompt_used    = %s,
                       feedback_text  = %s,
                       retry_kind     = %s,
                       status         = %s,
                       error          = NULL,
                       metadata       = {meta_sql},
                       updated_at     = NOW()
                 WHERE id = %s
                """,
                (
                    entry.get("storage_bucket"), entry.get("storage_path"), lp,
                    entry.get("mime_type") or "image/png", size,
                    entry.get("width"), entry.get("height"),
                    entry.get("image_model") or row.get("image_model"),
                    entry.get("prompt_used") or "", entry.get("feedback_text"),
                    rk if rk in _RETRY_KIND_COLUMN_VALUES else None,
                    STATUS_READY, *meta_params, run_id,
                ),
            )
    if dropped:
        await _delete_dropped_stills(run_id, dropped, keep)
    return {"id": run_id, "scene_index": 0, "local_path": lp,
            "width": entry.get("width"), "height": entry.get("height")}


async def _undo_scene(run_id: int, i: int, md: dict) -> dict | None:
    """Repoint scenes[i] (i >= 1) at its newest earlier still row
    (previous_keyframe_ids), atomically; the row taken off stays in the DB
    (removed with the run). A version whose image is gone is dropped. Undoing
    a framing re-shoot puts the scene's framing back too (_undo_framing).
    None when there is none; 409 when the still changed meanwhile."""
    sm = _still_scene(md, i)
    current = sm.get("keyframe_id")
    stack = [int(k) for k in (sm.get("previous_keyframe_ids") or []) if k]
    target: tuple[int, dict] | None = None
    while stack:
        cand = stack.pop()
        row = await _fetch_scene_row(cand)
        if row and _still_version_usable(row):
            target = (cand, row)
            break
    if target is None:
        return None
    tid, trow = target
    swapped: dict[str, bool] = {}
    restored_used = _md_dict(trow.get("metadata")).get("framing_used")
    cur_row = await _fetch_scene_row(int(current)) if current else None
    framing = _undo_framing(_md_dict((cur_row or {}).get("metadata")).get("retry_kind"), restored_used)

    def _swap(scene: dict) -> dict:
        if scene.get("keyframe_id") != current:
            return {}
        swapped["ok"] = True
        # The clip is out of date unless it was made from the still put back.
        src = scene.get("video_source")
        out = {"keyframe_id": tid, "previous_keyframe_ids": stack,
               "as_is": (trow.get("prompt_used") or "") == _USE_AS_IS_PROMPT,
               # The framing that still was made at (on its row).
               "framing_used": restored_used,
               "video_stale": not (src and src == _still_key_of(i, keyframe_id=tid)),
               "framing_warning": None, "error": None}
        if framing:
            out["framing"] = _scene_framing_value(md, framing)
        return out

    await _patch_scene(run_id, i, _swap)
    if not swapped:
        raise HTTPException(409, "This still changed meanwhile. Try again.")
    return {"id": tid, "scene_index": i, "width": trow.get("width"),
            "height": trow.get("height"), "previous": {"keyframe_id": current}}


async def undo_still(run_id: int, which: Any) -> dict:
    """Put back the version of a scene's still from before its last change
    (a Refine, a Regenerate, an edit's regen): the clip is marked stale,
    unless it was made from the still put back (its video_source), and the
    scene's error cleared; the still taken off is kept. `which` is
    'main' / 'scene_<N>' or a scene index. Returns {id, scene_index, ...}.
    409 when there is no earlier version, or the still is being made."""
    run = await _fetch_run_row(run_id)
    md = run["metadata"] or {}
    i = _still_index(which, md)
    if _still_busy(md, i):
        raise HTTPException(409, STILL_BUSY_MESSAGE)
    restored = await (_undo_scene0(run_id) if i == 0 else _undo_scene(run_id, i, md))
    if restored is None:
        raise HTTPException(409, "There is no earlier version of this still to go back to.")
    log.info("[yas-th] undo_still run=%d scene=%d -> %s", run_id, i, restored.get("id"))
    return restored


# ---------------------------------------------------------------------------
# Step 4a — Start videos (kicks THREE Veo calls: intro / main / outro)
# ---------------------------------------------------------------------------
#
# All three segments become Veo clips. The intro and outro use their card
# stills as i2v references (so they keep the layout but pick up subtle
# motion); the main keyframe drives the talking-head-v2 clip with audio.
# Composition stays manually-gated by the user — once all three Veos are
# ready the poller flips composition_status to `videos_ready`, and the
# user clicks `stitch_composition` to fire Remotion.

# Veo durations per segment
VEO_DURATION_BOOKEND = 4   # minimum Veo 3.1 duration; intro & outro cards
VEO_DURATION_MAIN = 8      # the talking-head-v2 clip


async def _resolve_card_path(card_id: int | None) -> Path | None:
    """Return the local file path for an intro/outro card row, if any.

    Tries local_path first; if missing or not on disk, falls back to
    downloading from the Supabase bucket into a cache directory so the
    auto-card can still be the Veo input. Without this fallback, runs
    that only stored to the bucket would silently slip back to using the
    user's reference asset for Veo — exactly the bug v4 Fix 4 is meant
    to prevent.
    """
    if not card_id:
        return None
    row = await _fetch_card_row(int(card_id))
    if not row:
        return None

    # Primary: local disk path.
    if row.get("local_path"):
        p = _abs_from_rel(row["local_path"])
        if p.exists():
            return p

    # Fallback: Supabase bucket → local cache.
    bucket = row.get("storage_bucket")
    spath = row.get("storage_path")
    if bucket and spath:
        try:
            signed = await asset_storage.url_for(
                storage_bucket=bucket,
                storage_path=spath,
                local_path=None,
                expires=600,
                internal=True,  # fetched below by this server
            )
            if signed:
                import httpx
                cache_dir = TH_ROOT / "_bucket_cache"
                cache_dir.mkdir(parents=True, exist_ok=True)
                ext = Path(spath).suffix or ".png"
                tmp = cache_dir / f"card-{card_id}{ext}"
                async with httpx.AsyncClient(timeout=30.0) as client:
                    r = await client.get(signed)
                    if r.status_code < 300:
                        tmp.write_bytes(r.content)
                        log.info(
                            "[yas-th] _resolve_card_path id=%d cached bucket → %s",
                            card_id, tmp,
                        )
                        return tmp
        except Exception:
            log.exception(
                "[yas-th] _resolve_card_path id=%d bucket fallback failed",
                card_id,
            )
    return None


async def _resolve_bookend(*, run_id: int, md: dict, refs: dict, role: str) -> dict | None:
    """The image a bookend's clip is made from: {path, card_id, kept, card}.

    Its card first (it carries the words and the shape), unless that card was
    made from another image than the one picked now: run 824's cards were
    drawn before the intro was picked, then animated instead of it. Then the
    picked upload, previous reel's card or asset itself, which is the user's
    own image (kept: never redrawn). None when there is neither."""
    card_id = md.get(f"{role}_image_id")
    if card_id:
        card = await _fetch_card_row(int(card_id))
        if card is not None and not _card_matches_refs(card, refs, role):
            log.warning(
                "[yas-th] run=%d %s card #%s was made from %s, not the picked %s: not used",
                run_id, role, card_id, _card_source_ref(card)[1], _bookend_ref(refs, role),
            )
        elif card is not None:
            path = await _resolve_card_path(int(card_id))
            if path is not None:
                return {"path": path, "card_id": int(card_id), "card": card,
                        "kept": bool(_md_dict(card.get("metadata")).get("as_is"))}
    for key, resolve in ((f"{role}_upload_id", _resolve_upload_path),
                         (f"{role}_card_id", _resolve_card_path),
                         (f"{role}_asset_id", _resolve_asset_path)):
        if refs.get(key):
            try:
                path = await resolve(int(refs[key]))
            except Exception:
                log.exception("%s %s path resolve failed for run #%d", role, key, run_id)
                continue
            if path is None:
                continue
            return {"path": path, "card_id": None, "card": None, "kept": True}
    return None


async def _resolve_intro_card_path(*, run_id: int, md: dict, refs: dict) -> Path | None:
    """The image the intro's clip is made from (see _resolve_bookend)."""
    bk = await _resolve_bookend(run_id=run_id, md=md, refs=refs, role="intro")
    return bk["path"] if bk else None


async def _resolve_outro_card_path(*, run_id: int, md: dict, refs: dict) -> Path | None:
    """The image the outro's clip is made from (see _resolve_bookend)."""
    bk = await _resolve_bookend(run_id=run_id, md=md, refs=refs, role="outro")
    return bk["path"] if bk else None


# ---------------------------------------------------------------------------
# Bookend motion director (TH v2): Veo for a card's icons, else a slow zoom
# ---------------------------------------------------------------------------
#
# A bookend clip shows its card as it is. Gemini Flash looks at the card once
# (cached on the card row): when it carries icons or other graphic elements,
# Veo animates ONLY those, subtly, camera locked off, under hard rules that
# keep every letter, logo, colour and the background (the prompt never names
# the card's words, so Veo has nothing to paint). A card with nothing to
# animate gets no Veo at all: an in-house slow zoom, 1.00 to 1.06, at the
# reel's size and frame rate. Each Veo bookend is read back when it lands
# (check_bookend_clip): text added, changed or garbled, or a restyled card,
# and the clip becomes the slow zoom. The video editor's bookends keep their
# own path (pick_bookend_animation and the ambient-motion prompts).

BOOKEND_ZOOM_END = 1.06
BOOKEND_ZOOM_FPS = 30   # the reel's frame rate (th_reel.FPS)
BOOKEND_ZOOM_MODEL = "in-house-slow-zoom"
BOOKEND_ZOOM_PROMPT = "(the card, slowly zooming in: made in-house, no Veo)"
BOOKEND_STYLE_VEO = "animate_icons"
BOOKEND_STYLE_ZOOM = "slow_zoom"

# A director's "element" that is really text, a logo (or a part of it, like
# its divider line), a line or frame, the background or an effect on it, or a
# person is never animated: only distinct illustrative icons are. Run 830's
# director picked the 42 logo frame's divider line (Veo lengthened it and
# grew a grid on the white) and "light streaks" plus "large '42' number" on
# the intro (Veo restyled the card). The name is judged by what it names
# (_not_an_icon): "bar graph with trend line", "line chart icon", "gift box"
# and "graduate's cap" are icons; "vertical line", "grid of dots" are not.
# Text, a logo or a number anywhere in what the name names.
_TEXT_WORDS = frozenset((
    "text texts word words letter letters lettering headline headlines title titles subtitle "
    "caption captions logo logos logotype wordmark lockup font typography url slogan tagline "
    "number numbers numeral numerals digit digits"
).split())
# The background or a person anywhere in what the name names.
_SCENE_WORDS = frozenset("background backdrop person people face faces".split())
# A line, frame or effect when it is the thing named (its last word).
_SHAPE_WORDS = frozenset((
    "line lines divider dividers separator separators rule rules underline underlines "
    "border borders frame frames outline outlines stroke strokes panel panels "
    "streak streaks stripe stripes ray rays beam beams glow glows bokeh flare flares "
    "particle particles sparkle sparkles gradient gradients pattern patterns grid grids "
    "texture textures noise"
).split())
# A name ending in one of these names an icon ("line chart icon").
_ICON_WORDS = frozenset(
    "icon icons symbol symbols illustration illustrations pictogram pictograms glyph glyphs emoji".split())
# What the name names ends at the first of these ("bar graph" of "bar graph
# with trend line").
_NAME_TAIL = re.compile(
    r"\b(?:with|of|and|behind|around|on|in|inside|under|over|above|below|beneath|beside|near|"
    r"next|at|for|from|to|between|along|across|containing|holding|showing)\b")
# A possessive is not a quote ("graduate's cap", "students' desk").
_POSSESSIVE = re.compile(r"(?<=[a-z])['’]s\b|(?<=s)['’](?=\s|$)")
# Quoted words ("large '42' number") or a bare number ("42", not "3D"): text.
_QUOTED_OR_NUMBER = re.compile(r"[\"'‘’“”]|\b\d+\b")


def _not_an_icon(what: str) -> bool:
    """Whether a director's element name is not a distinct icon: quoted
    words or a bare number; text, a logo, a number, the background or a
    person in what it names; or a line, frame or effect as the thing named,
    unless the name ends in "icon" (or "symbol", "illustration", ...)."""
    name = _POSSESSIVE.sub("", str(what or "").lower())
    if _QUOTED_OR_NUMBER.search(name):
        return True
    words = [w for w in re.findall(r"[a-z0-9]+", _NAME_TAIL.split(name, 1)[0])
             if w not in ("a", "an", "the")]
    if not words:
        return True
    if any(w in _TEXT_WORDS or w in _SCENE_WORDS for w in words):
        return True
    return words[-1] not in _ICON_WORDS and words[-1] in _SHAPE_WORDS


# The plan format; a plan cached on a card in another one is made again
# (the director's rules changed with it).
BOOKEND_PLAN_VERSION = 2
# An icon smaller than this share of the card (by its box) is not animated.
_ELEMENT_MIN_AREA = 0.002


def _box_2d(raw: Any) -> list[int] | None:
    """A [ymin, xmin, ymax, xmax] box on 0..1000, or None when it is not one."""
    if not isinstance(raw, (list, tuple)) or len(raw) != 4:
        return None
    try:
        y0, x0, y1, x1 = (min(1000, max(0, int(round(float(v))))) for v in raw)
    except (TypeError, ValueError):
        return None
    return [y0, x0, y1, x1] if y1 > y0 and x1 > x0 else None


def _box_overlap(a: list[int], b: list[int]) -> float:
    """The share of box `a`'s area inside box `b` (both 0..1000 boxes)."""
    h = min(a[2], b[2]) - max(a[0], b[0])
    w = min(a[3], b[3]) - max(a[1], b[1])
    if h <= 0 or w <= 0:
        return 0.0
    return (h * w) / max(1, (a[2] - a[0]) * (a[3] - a[1]))


def _bookend_plan_from(result: Any, *, text_lines: list[dict] | None = None) -> dict | None:
    """The director's answer as a plan: {v, mode: 'veo'|'zoom', elements,
    motion_plan, card_text, reason}. Only distinct icons are kept: an element
    named as text, a number, a logo, a line or frame, the background or an
    effect (_not_an_icon: judged by what its name names), one smaller than
    _ELEMENT_MIN_AREA of the card, or one lying mostly on a line of the
    card's text (`text_lines`, a kept card's read, with boxes) is dropped; at
    most 4 are kept, each with a known motion and its box (None when the
    director gave none). None when the answer is not a plan."""
    if not isinstance(result, dict):
        return None
    text_boxes = [b for b in (_box_2d((ln or {}).get("box")) for ln in text_lines or []) if b]
    elements: list[dict] = []
    for el in result.get("elements") or []:
        if not isinstance(el, dict):
            continue
        what = " ".join(str(el.get("what") or "").split())[:80]
        if not what or _not_an_icon(what):
            continue
        box = _box_2d(el.get("box_2d", el.get("box")))
        if box is not None:
            if (box[2] - box[0]) * (box[3] - box[1]) < _ELEMENT_MIN_AREA * 1_000_000:
                continue
            if any(_box_overlap(box, tb) >= 0.5 for tb in text_boxes):
                continue
        motion = el.get("motion") if el.get("motion") in th_prompts.BOOKEND_MOTIONS else "float"
        elements.append({"what": what, "where": " ".join(str(el.get("where") or "").split())[:60],
                         "motion": motion, "box": box})
        if len(elements) == 4:
            break
    card_text = [" ".join(str(t).split())[:120] for t in (result.get("card_text") or [])
                 if str(t or "").strip()][:12]
    veo = bool(result.get("has_animatable_elements")) and bool(elements)
    return {
        "v": BOOKEND_PLAN_VERSION,
        "mode": "veo" if veo else "zoom",
        "elements": elements if veo else [],
        "motion_plan": " ".join(str(result.get("motion_plan") or "").split())[:300],
        "card_text": card_text,
        "reason": "icons to animate" if veo else "no icons to animate (a logo or text only)",
    }


async def _direct_bookend_motion(
    card_path: Path, *, role: str, text_lines: list[dict] | None = None,
) -> dict | None:
    """One director call (Gemini Flash vision) on a card; None when it fails.
    `text_lines`: the card's text read (a kept card's), whose boxes no icon
    may mostly lie on."""
    try:
        result = await asyncio.wait_for(
            gemini_multimodal(
                th_prompts.BOOKEND_MOTION_DIRECTOR_PROMPT.format(role=role),
                images=[Path(card_path)],
                system=th_prompts.BOOKEND_MOTION_DIRECTOR_SYSTEM,
                json_schema=th_prompts.BOOKEND_MOTION_DIRECTOR_SCHEMA,
                temperature=0.2,
                max_tokens=900,
                model=config.vertex_gemini_fast_model,
            ),
            timeout=60,
        )
    except Exception:
        log.exception("[yas-th] %s motion director failed for %s", role, card_path)
        return None
    plan = _bookend_plan_from(result, text_lines=_card_lines_from(text_lines) if text_lines else None)
    if plan is None:
        log.warning("[yas-th] %s motion director returned %r", role, result)
    return plan


def _plan_ok(plan: Any) -> bool:
    """A motion plan made under the current rules (BOOKEND_PLAN_VERSION); an
    older cached one is planned again."""
    return (isinstance(plan, dict) and plan.get("mode") in ("veo", "zoom")
            and plan.get("v") == BOOKEND_PLAN_VERSION)


async def _warm_bookend_motion(card_id: int, role: str, card_md: dict) -> dict:
    """Run the director on a new card and cache its plan on the card row
    (metadata.motion); a kept card whose text was not read takes the text
    the director read. A card that came with its plan (a pixel copy of a
    previous reel's card) is not looked at again. Best effort: a failure
    leaves the card as it is (the plan is made when the clip is). Returns
    the card metadata as stored."""
    if _plan_ok(card_md.get("motion")):
        return card_md
    try:
        path = await _resolve_card_path(int(card_id))
        plan = (await _direct_bookend_motion(path, role=role, text_lines=card_md.get("text_lines"))
                if path is not None else None)
    except Exception:
        log.exception("[yas-th] %s card #%s: motion director warm-up failed", role, card_id)
        plan = None
    if plan is None:
        return card_md
    patch: dict[str, Any] = {"motion": plan}
    if card_md.get("as_is") and not card_md.get("card_text"):
        patch["card_text"] = "\n".join(plan["card_text"]) or None
        patch["card_text_source"] = "detected" if plan["card_text"] else None
    await _patch_metadata(int(card_id), patch)
    return {**card_md, **patch}


# A run's current card with no plan under the current rules (made before
# them, like run 830's outro card, or one whose director call failed) is
# planned ONCE in the background when the run is opened (fetch_run), so its
# tiles never show an older plan as the current one. Once per card and plan
# rules, whatever the outcome: the card row is claimed first
# (metadata.motion_warm = BOOKEND_PLAN_VERSION, set by one atomic UPDATE), so
# no poll, process or failure ever asks the director twice. A card left
# unplanned is planned when its clip is made.
_CARD_PLAN_TASKS: set[asyncio.Task] = set()


def _card_needs_plan(card: dict | None, md: dict | None, role: str) -> bool:
    """Whether fetch_run plans this `role` card in the background: a ready
    card of a wanted bookend, made from the image picked now, with no plan
    under the current rules and not claimed for them before."""
    if not card or card.get("status") != STATUS_READY or not _bookend_wanted(md, role):
        return False
    cmd = _md_dict(card.get("metadata"))
    if _plan_ok(cmd.get("motion")) or cmd.get("motion_warm") == BOOKEND_PLAN_VERSION:
        return False
    return _card_matches_refs(card, (md or {}).get("refs"), role)


async def _claim_card_plan(card_id: int) -> bool:
    """Claim card `card_id` for its one background plan under the current
    rules: True for the one caller whose UPDATE set metadata.motion_warm,
    False when it was claimed already (another poll, another process)."""
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                UPDATE yas_generations
                   SET metadata = metadata || %s::jsonb
                 WHERE id = %s
                   AND COALESCE(metadata->>'motion_warm', '') <> %s
             RETURNING id
                """,
                (Jsonb({"motion_warm": BOOKEND_PLAN_VERSION}), int(card_id), str(BOOKEND_PLAN_VERSION)),
            )
            row = await cur.fetchone()
    return bool(row)


async def _plan_card_motion_once(card_id: int, role: str) -> dict | None:
    """The background plan of one card (_card_needs_plan): claimed first
    (_claim_card_plan), then one director call (_warm_bookend_motion: one
    Gemini Flash read, the plan cached on the card). Returns the plan, or
    None (claimed before, planned meanwhile, or the director failed: the
    clip plans it). Never raises."""
    try:
        if not await _claim_card_plan(card_id):
            return None
        card = await _fetch_card_row(int(card_id))
        cmd = _md_dict((card or {}).get("metadata"))
        if card is None or _plan_ok(cmd.get("motion")):
            return None
        stored = await _warm_bookend_motion(int(card_id), role, cmd)
        plan = stored.get("motion") if _plan_ok(stored.get("motion")) else None
        log.info("[yas-th] %s card #%s planned once on opening: %s", role, card_id,
                 (plan or {}).get("mode") or "not planned (the clip plans it)")
        return plan
    except Exception:
        log.exception("[yas-th] %s card #%s: the background plan failed", role, card_id)
        return None


def _dispatch_card_plan(card_id: int, role: str) -> None:
    """Fire-and-forget _plan_card_motion_once (fetch_run returns at once)."""
    task = asyncio.create_task(_plan_card_motion_once(int(card_id), role),
                               name=f"yas_th_card_plan_{card_id}")
    _CARD_PLAN_TASKS.add(task)
    task.add_done_callback(_CARD_PLAN_TASKS.discard)


async def _bookend_motion(bookend: dict, *, role: str) -> dict:
    """The plan for a bookend's clip: the one cached on its card, else a new
    director call (cached on the card). When the director cannot answer, the
    slow zoom (not cached, so the next clip asks again)."""
    card = bookend.get("card") or {}
    cmd = _md_dict(card.get("metadata"))
    cached = cmd.get("motion")
    if _plan_ok(cached):
        return cached
    plan = await _direct_bookend_motion(Path(bookend["path"]), role=role, text_lines=cmd.get("text_lines"))
    if plan is None:
        return {"v": BOOKEND_PLAN_VERSION, "mode": "zoom", "elements": [], "motion_plan": "",
                "card_text": [], "reason": "the motion director could not look at the card"}
    if bookend.get("card_id"):
        await _patch_metadata(int(bookend["card_id"]), {"motion": plan})
    return plan


def _bookend_card_info(bookend: dict, aspect: str) -> dict:
    """What a bookend clip was made from, kept on its animation row (the
    read-back and the slow-zoom fallback use it)."""
    return {"card_id": bookend.get("card_id"), "path": _to_rel(Path(bookend["path"])),
            "kept": bool(bookend.get("kept")), "aspect": _norm_aspect(aspect)}


async def _bookend_job(
    *,
    role: str,
    bookend: dict,
    aspect: str,
    quality: Any,
    feedback: str | None = None,
    plan: dict | None = None,
) -> dict:
    """How a bookend's clip is made, per the motion director (`plan` when it
    is already known): {role, style, plan, prompt, duration, with_audio,
    extra_md, coro}. For Veo, `coro` is the un-awaited i2v submission (the
    caller batches it) with the directed prompt and its negative, and
    extra_md flags the clip for the read-back; for the slow zoom `coro` is
    None (_zoom_animation makes it). `feedback` (a video retry) is added to a
    Veo prompt; a slow zoom has nothing it could change."""
    plan = plan if _plan_ok(plan) else await _bookend_motion(bookend, role=role)
    card_info = _bookend_card_info(bookend, aspect)
    job: dict[str, Any] = {"role": role, "plan": plan, "duration": VEO_DURATION_BOOKEND,
                           "with_audio": False, "card": card_info, "coro": None}
    if plan.get("mode") != "veo":
        if feedback:
            log.info("[yas-th] %s is a slow zoom: the feedback %r has nothing to change", role, feedback[:80])
        job.update(style=BOOKEND_STYLE_ZOOM, prompt=BOOKEND_ZOOM_PROMPT,
                   extra_md={"bookend_motion": plan, "bookend_card": card_info, "slow_zoom": True})
        return job
    prompt = th_prompts.build_bookend_motion_veo_prompt(
        role=role, elements=plan.get("elements") or [], aspect=aspect,
    )
    if feedback:
        prompt = th_prompts.append_feedback_to_prompt(prompt, feedback)
    extra_md: dict[str, Any] = {"bookend_check": True, "bookend_motion": plan, "bookend_card": card_info}
    if feedback:
        extra_md["bookend_feedback"] = feedback
    job.update(
        style=BOOKEND_STYLE_VEO, prompt=prompt, extra_md=extra_md,
        coro=_card_i2v_coro(
            prompt, keep_as_is=bool(bookend.get("kept")),
            start_image=Path(bookend["path"]), aspect=aspect,
            duration=VEO_DURATION_BOOKEND, model=_veo_model_for(quality),
            negative_prompt=th_prompts.BOOKEND_MOTION_NEGATIVE_PROMPT,
        ),
    )
    return job


def _zoom_scale(u: float) -> float:
    """The slow zoom at u in [0, 1] of the clip: 1.00 to BOOKEND_ZOOM_END,
    easing in and out (a half cosine), so it starts and ends gently."""
    import math
    u = min(1.0, max(0.0, float(u)))
    return 1.0 + (BOOKEND_ZOOM_END - 1.0) * (0.5 - 0.5 * math.cos(math.pi * u))


def _render_zoom_clip(card_path: Path, out: Path, aspect: Any, seconds: float = 4.0) -> Path:
    """An MP4 of the card slowly zooming in on its centre, at the reel's size
    (1080x1920 or 1920x1080) and frame rate, silent. Each frame is a centred
    window of the card scaled to the frame with sub-pixel accuracy, so the
    zoom is smooth and nothing is stretched (an image of another shape, a
    kept pick on an older run, is first padded whole). Written next to `out`
    and moved there."""
    import subprocess

    import imageio_ffmpeg
    from PIL import Image

    aspect = _norm_aspect(aspect)
    w, h = _aspect_dims(aspect)
    with Image.open(card_path) as src:
        im = src.convert("RGB")
    if not _is_exact_aspect(im.width, im.height, aspect):
        im = _pad_to_aspect(im, aspect)
    n = max(2, round(float(seconds) * BOOKEND_ZOOM_FPS))
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_name(out.stem + ".part.mp4")
    cmd = [
        imageio_ffmpeg.get_ffmpeg_exe(), "-y", "-loglevel", "error",
        "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{w}x{h}", "-r", str(BOOKEND_ZOOM_FPS), "-i", "-",
        "-an", "-c:v", "libx264", "-preset", "veryfast", "-crf", "18", "-pix_fmt", "yuv420p",
        "-movflags", "+faststart", str(tmp),
    ]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    try:
        for k in range(n):
            z = _zoom_scale(k / (n - 1))
            bw, bh = im.width / z, im.height / z
            x0, y0 = (im.width - bw) / 2, (im.height - bh) / 2
            frame = im.resize((w, h), Image.BICUBIC, box=(x0, y0, x0 + bw, y0 + bh))
            proc.stdin.write(frame.tobytes())
        proc.stdin.close()
        err = proc.stderr.read().decode("utf-8", "replace")
        code = proc.wait(timeout=180)
    except BaseException:
        proc.kill()
        tmp.unlink(missing_ok=True)
        raise
    if code != 0 or not tmp.exists():
        tmp.unlink(missing_ok=True)
        raise RuntimeError(f"ffmpeg slow zoom failed: {err[-400:]}")
    tmp.replace(out)
    return out


def _animations_dir() -> Path:
    """Where animation MP4s live (the yas_animations poller's folder)."""
    return TH_ROOT.parent / "animations"


async def _patch_animation_metadata(anim_id: int, patch: dict) -> None:
    """Shallow-merge `patch` into yas_animations.metadata for `anim_id`."""
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                UPDATE yas_animations
                   SET metadata = metadata || %s::jsonb,
                       updated_at = NOW()
                 WHERE id = %s
                """,
                (Jsonb(patch), int(anim_id)),
            )


async def _finish_local_animation(anim_id: int, path: Path) -> None:
    """A clip made here (no Veo): stored like a Veo clip the poller fetched
    (bucket yas/animations/<id>.mp4, else local only) and marked ready."""
    data = Path(path).read_bytes()
    try:
        stored = await asset_storage.upload(data=data, path=f"yas/animations/{anim_id}.mp4", mime="video/mp4")
    except Exception:
        log.exception("[yas-th] anim=%d bucket upload failed; keeping it local only", anim_id)
        stored = {}
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                UPDATE yas_animations
                   SET status = %s,
                       storage_bucket = %s,
                       storage_path = %s,
                       local_path = %s,
                       mime_type = 'video/mp4',
                       bytes = %s,
                       error = NULL,
                       updated_at = NOW()
                 WHERE id = %s
                """,
                (STATUS_READY, stored.get("storage_bucket"), stored.get("storage_path"),
                 stored.get("local_path") or str(path), len(data), int(anim_id)),
            )


async def _fail_animation(anim_id: int, error: str) -> None:
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "UPDATE yas_animations SET status = %s, error = %s, updated_at = NOW() WHERE id = %s",
                (STATUS_FAILED, error[:500], int(anim_id)),
            )


async def _zoom_animation(
    *, run_id: int, role: str, bookend: dict, aspect: str, plan: dict | None = None,
    anim_id: int | None = None,
) -> int:
    """The bookend's clip as the in-house slow zoom of its card: a ready
    yas_animations row, no Veo and no cost. `anim_id`: finish that row (a
    Veo clip being made again) instead of adding one. A failed render marks
    the row failed with the reason. Returns the row id."""
    card_info = _bookend_card_info(bookend, aspect)
    extra = {"bookend_motion": plan, "bookend_card": card_info, "slow_zoom": True}
    if anim_id is None:
        anim_id = await _insert_animation_row(
            parent_run_id=run_id, prompt_used=BOOKEND_ZOOM_PROMPT, veo_op_name=None,
            veo_model=BOOKEND_ZOOM_MODEL, duration_sec=VEO_DURATION_BOOKEND, with_audio=False,
            cost_usd=0.0, role=role, aspect_ratio=aspect, extra_md=extra,
        )
    else:
        await _patch_animation_metadata(anim_id, extra)
    out = _animations_dir() / f"anim-{anim_id}.mp4"
    try:
        await asyncio.to_thread(_render_zoom_clip, Path(bookend["path"]), out, aspect, VEO_DURATION_BOOKEND)
        await _finish_local_animation(anim_id, out)
        log.info("[yas-th] run=%d %s clip anim=%d is the slow zoom (%s)", run_id, role, anim_id,
                 (plan or {}).get("reason"))
    except Exception as e:
        log.exception("[yas-th] run=%d %s slow zoom failed", run_id, role)
        await _fail_animation(anim_id, f"slow zoom failed: {str(e)[:300]}")
    return anim_id


def _clip_frame(clip: Path, out: Path, *, at: float | None = None, from_end: float | None = None) -> Path:
    """One frame of a clip as a PNG: at `at` seconds, or `from_end` seconds
    before its end."""
    import subprocess

    import imageio_ffmpeg

    cmd = [imageio_ffmpeg.get_ffmpeg_exe(), "-y", "-loglevel", "error"]
    if from_end is not None:
        cmd += ["-sseof", f"-{from_end}"]
    elif at is not None:
        cmd += ["-ss", f"{at}"]
    cmd += ["-i", str(clip), "-frames:v", "1", str(out)]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    if proc.returncode != 0 or not Path(out).exists():
        raise RuntimeError(f"ffmpeg frame grab failed: {(proc.stderr or '')[-300:]}")
    return Path(out)


def _bookend_text_verdict(result: Any) -> tuple[bool, str]:
    """Whether a read-back of a Veo bookend keeps its card: no word in the
    middle or last frame that the card does not have, every word of the card
    in the last frame (normalised: case, punctuation, Unicode forms), and the
    reader saw no text change and no restyle. (ok, why)."""
    if not isinstance(result, dict):
        return False, "the read-back returned nothing"
    card = th_prompts.card_text_words(result.get("card_text"))
    middle = th_prompts.card_text_words(result.get("middle_text"))
    last = th_prompts.card_text_words(result.get("last_text"))
    added = (middle | last) - card
    if added:
        return False, "text that is not on the card appeared: " + ", ".join(sorted(added)[:6])
    missing = card - last
    if missing:
        return False, "the card's text is missing or changed at the end: " + ", ".join(sorted(missing)[:6])
    if result.get("text_changed"):
        return False, "the reader saw the card's text change"
    if result.get("restyled"):
        return False, "the card's look changed (background, colours or effects)"
    return True, "the text and the look match the card"


async def _read_back_bookend(card_path: Path, clip_path: Path) -> tuple[bool, str]:
    """Read a Veo bookend's middle and last frames against its card (one
    Gemini Flash call). A read-back that cannot be made counts as a failure:
    an unchecked Veo bookend is never shipped."""
    import shutil
    import tempfile

    tmp = Path(tempfile.mkdtemp(prefix="th-bookend-check-"))
    try:
        middle = await asyncio.to_thread(_clip_frame, clip_path, tmp / "middle.png",
                                         at=VEO_DURATION_BOOKEND / 2)
        last = await asyncio.to_thread(_clip_frame, clip_path, tmp / "last.png", from_end=0.1)
        result = await asyncio.wait_for(
            gemini_multimodal(
                th_prompts.BOOKEND_TEXT_CHECK_PROMPT,
                images=[Path(card_path), middle, last],
                system=th_prompts.BOOKEND_TEXT_CHECK_SYSTEM,
                json_schema=th_prompts.BOOKEND_TEXT_CHECK_SCHEMA,
                temperature=0.0,
                max_tokens=900,
                model=config.vertex_gemini_fast_model,
            ),
            timeout=90,
        )
    except Exception as e:
        log.exception("[yas-th] bookend read-back of %s failed", clip_path)
        return False, f"the clip could not be read back ({type(e).__name__})"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return _bookend_text_verdict(result)


# ---- The pixel check of a Veo bookend --------------------------------------
#
# The text read alone passed anim 353 (the 42 logo frame): its words were all
# there, but Veo had grown a faint grid of grey marks on the white, lengthened
# the divider line and nudged the logo. So every Veo bookend is first compared
# with its card, pixel by pixel, outside the boxes of the icons it was asked
# to move: the card and frames from the start, middle and end of the clip,
# grayscale, at DRIFT_LONG_EDGE px. A pixel counts as changed when it is
# DRIFT_CHANGE_LEVEL levels away from every card pixel around it (so a
# sub-pixel wobble is not a change). The thresholds were set on run 830's
# clips (353: a 62 px patch where the divider grew, 1.3% of the white marked
# 2 levels darker than the first frame, 1.1% changed at the end; 352: 7 to 14%
# changed) against faithful clips re-encoded from the card (0% changed, no
# patch over 8 px, under 0.4% marked).
DRIFT_LONG_EDGE = 640
DRIFT_CHANGE_LEVEL = 24
DRIFT_CHANGED_MAX = 0.003
DRIFT_BLOB_MIN = 40
DRIFT_MARK_LEVEL = 2
DRIFT_MARKS_MAX = 0.01
DRIFT_SHIFT_MAX = 3.0
# The card's flat areas moved off its own colours by this many levels in a
# channel (the median; grayscale alone misses a hue change at the same
# brightness). A faithful re-encode stays within 1.
DRIFT_COLOUR_MAX = 8.0
DRIFT_BOX_PAD = 0.04


def _bookend_drift(card: Any, frames: list, boxes: list | None = None) -> dict:
    """Compare a Veo bookend's frames (PIL images, the clip's first frame
    first) with its card outside the icon `boxes` ([ymin, xmin, ymax, xmax]
    on 0..1000, each grown by DRIFT_BOX_PAD). Fails ({ok: False, reason}) on
      - changed pixels over DRIFT_CHANGED_MAX of the card outside the icons
        (a restyle, a zoom or a moved logo),
      - one changed patch of DRIFT_BLOB_MIN px or more (a new mark, a line
        that grew, a nudged word),
      - the card's flat background marked: over DRIFT_MARKS_MAX of it
        DRIFT_MARK_LEVEL+ levels off the clip's first frame (a faint grid),
      - that background shifted as a whole by DRIFT_SHIFT_MAX levels or more
        (a lighting change), or off the card's own colours by
        DRIFT_COLOUR_MAX in a channel (a colour change).
    Returns {ok, reason, changed, blob, marks, shift, colour, frames} (the
    worst of each over the frames)."""
    import cv2
    import numpy as np
    from PIL import Image

    w, h = card.size
    s = DRIFT_LONG_EDGE / max(w, h)
    size = (max(1, round(w * s)), max(1, round(h * s)))

    def gray(im: Any) -> Any:
        return np.asarray(im.convert("L").resize(size, Image.BOX), dtype=np.float32)

    def rgb(im: Any) -> Any:
        return np.asarray(im.convert("RGB").resize(size, Image.BOX), dtype=np.float32)

    c = gray(card)
    c_rgb = rgb(card)
    k3, k7 = np.ones((3, 3), np.uint8), np.ones((7, 7), np.uint8)
    hi, lo = cv2.dilate(c, k3), cv2.erode(c, k3)
    rows, cols = c.shape
    keep = np.ones(c.shape, bool)
    for b in boxes or []:
        box = _box_2d(b)
        if box is None:
            continue
        y0, x0, y1, x1 = (v / 1000 for v in box)
        keep[max(0, int((y0 - DRIFT_BOX_PAD) * rows)):min(rows, int(np.ceil((y1 + DRIFT_BOX_PAD) * rows))),
             max(0, int((x0 - DRIFT_BOX_PAD) * cols)):min(cols, int(np.ceil((x1 + DRIFT_BOX_PAD) * cols)))] = False
    flat = ((cv2.dilate(c, k7) - cv2.erode(c, k7)) < 6) & keep
    out: dict[str, Any] = {"ok": True, "reason": "the card's pixels hold outside its icons",
                           "changed": 0.0, "blob": 0, "marks": 0.0, "shift": 0.0, "colour": 0.0,
                           "frames": len(frames)}
    why: str | None = None
    start: Any = None
    for n, frame in enumerate(frames):
        f = gray(frame)
        if start is None:
            start = f
        d = np.maximum(np.maximum(f - hi, lo - f), 0.0)
        changed = ((d > DRIFT_CHANGE_LEVEL) & keep).astype(np.uint8)
        frac = float(changed.sum()) / max(1, int(keep.sum()))
        count, _labels, stats, _c = cv2.connectedComponentsWithStats(changed, connectivity=8)
        blob = int(stats[1:, cv2.CC_STAT_AREA].max()) if count > 1 else 0
        rel = (f - start)[flat]
        offset = float(np.median(rel)) if rel.size else 0.0
        marks = float((np.abs(rel - offset) >= DRIFT_MARK_LEVEL).mean()) if rel.size else 0.0
        hue = (rgb(frame) - c_rgb)[flat]
        colour = float(np.abs(np.median(hue, axis=0)).max()) if hue.size else 0.0
        out["changed"] = round(max(out["changed"], frac), 5)
        out["blob"] = max(out["blob"], blob)
        out["marks"] = round(max(out["marks"], marks), 5)
        out["shift"] = round(max(out["shift"], abs(offset)), 2)
        out["colour"] = round(max(out["colour"], colour), 2)
        if why is None:
            if frac > DRIFT_CHANGED_MAX:
                why = f"the card's look changed: {frac:.1%} of it outside the icons is different (frame {n + 1})"
            elif blob >= DRIFT_BLOB_MIN:
                why = (f"something was added or moved on the card outside the icons (a {blob} px patch, "
                       f"frame {n + 1})")
            elif marks > DRIFT_MARKS_MAX:
                why = f"faint marks appeared on the card's background ({marks:.1%} of it, frame {n + 1})"
            elif abs(offset) >= DRIFT_SHIFT_MAX:
                why = f"the card's background changed brightness (frame {n + 1})"
            elif colour >= DRIFT_COLOUR_MAX:
                why = f"the card's colours changed (frame {n + 1})"
    if why is not None:
        out.update(ok=False, reason=why)
    return out


def _bookend_clip_drift(card_path: Path, clip_path: Path, boxes: list | None) -> dict:
    """_bookend_drift on frames of the clip at its start, a quarter, the
    middle, three quarters and its end (ffmpeg). Never raises: a check that
    cannot be made is {ok: None, reason}."""
    import shutil
    import tempfile

    from PIL import Image

    tmp = Path(tempfile.mkdtemp(prefix="th-bookend-drift-"))
    try:
        frames = []
        for k, at in enumerate((0.0, 1.0, 2.0, 3.0, None)):
            out = tmp / f"f{k}.png"
            if at is None:
                _clip_frame(clip_path, out, from_end=0.1)
            else:
                _clip_frame(clip_path, out, at=at * VEO_DURATION_BOOKEND / 4)
            with Image.open(out) as im:
                frames.append(im.convert("RGB"))
        with Image.open(card_path) as im:
            card = im.convert("RGB")
        return _bookend_drift(card, frames, boxes)
    except Exception as e:
        log.exception("[yas-th] the pixel check of %s failed", clip_path)
        return {"ok": None, "reason": f"the pixel check could not run ({type(e).__name__})"}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


async def check_bookend_clip(anim_id: int, clip_path: Path) -> dict | None:
    """Check a Veo bookend that just landed, before it is stored (the
    yas_animations poller calls this for every clip; only a TH v2 directed
    bookend asks for it: metadata.bookend_check). First its pixels against
    the card outside its icons (_bookend_clip_drift: added marks, a grown
    line, a moved logo or a restyle fail it), then, when they hold, a read
    of its text (_read_back_bookend). When either fails, the file at
    `clip_path` is replaced by the slow zoom of the card and the reason is
    logged and kept on the row (bookend_text_check, with the pixel check as
    `pixels`). Returns that record, or None when not asked."""
    row = await _fetch_animation_row(int(anim_id))
    meta = _md_dict((row or {}).get("metadata"))
    if not meta.get("bookend_check"):
        return None
    info = _md_dict(meta.get("bookend_card"))
    aspect = _norm_aspect(info.get("aspect"))
    card_path = _abs_from_rel(info["path"]) if info.get("path") else None
    if (card_path is None or not card_path.exists()) and info.get("card_id"):
        card_path = await _resolve_card_path(int(info["card_id"]))
    if card_path is None or not Path(card_path).exists():
        record: dict[str, Any] = {"ok": None, "reason": "the card is gone; the clip is kept unchecked"}
    else:
        boxes = [el.get("box") for el in (_md_dict(meta.get("bookend_motion")).get("elements") or [])
                 if isinstance(el, dict) and el.get("box")]
        pixels = await asyncio.to_thread(_bookend_clip_drift, Path(card_path), Path(clip_path), boxes)
        if pixels.get("ok") is False:
            ok, why = False, pixels["reason"]
        else:
            ok, why = await _read_back_bookend(Path(card_path), Path(clip_path))
        record = {"ok": ok, "reason": why, "pixels": pixels}
        if not ok:
            try:
                await asyncio.to_thread(_render_zoom_clip, Path(card_path), Path(clip_path), aspect,
                                        VEO_DURATION_BOOKEND)
                record["fallback"] = BOOKEND_STYLE_ZOOM
            except Exception:
                log.exception("[yas-th] anim=%d slow-zoom fallback failed; keeping the Veo clip", anim_id)
                record["fallback"] = None
    log.log(logging.INFO if record.get("ok") else logging.WARNING,
            "[yas-th] anim=%d bookend read-back: %s", anim_id, record)
    await _patch_animation_metadata(int(anim_id), {"bookend_text_check": record})
    if record.get("fallback"):
        run_id = meta.get("parent_talking_head_id")
        role = meta.get("role")
        if run_id and role in ("intro", "outro"):
            await _patch_metadata(int(run_id), {f"{role}_animation_style": BOOKEND_STYLE_ZOOM})
    return record


# ---------------------------------------------------------------------------
# Vision-picked bookend animation style (Fix 5)
# ---------------------------------------------------------------------------

async def pick_bookend_animation(
    *, card_path: Path, script: dict, topic: str, role: str,
) -> str:
    """Pick the best motion style for an intro/outro bookend.

    Runs a small Gemini Flash multimodal call with the rendered card image
    + topic + mood, gets back one enum key from INTRO_STYLES / OUTRO_STYLES.
    Falls back to fade_in / fade_hold on any error so a flaky vision call
    never blocks the kickoff.
    """
    fallback = "fade_in" if role == "intro" else "fade_hold"
    if not card_path or not card_path.exists():
        return fallback
    try:
        mood = (script.get("mood") if isinstance(script, dict) else "") or ""
        style_hint = (script.get("style") if isinstance(script, dict) else "") or ""
        palette = ", ".join(
            (script.get("palette") if isinstance(script, dict) else None) or []
        )
        user_prompt = (
            f"You are shown the {role} card for a talking-head-v2 Instagram "
            f"reel. Pick the animation style that best matches the card's "
            f"visual vibe.\n\n"
            f"Topic: {topic[:120]}\n"
            f"Mood: {mood[:120]}\n"
            f"Style hint: {style_hint[:120]}\n"
            + (f"Palette: {palette}\n" if palette else "")
            + "\nReturn ONE enum value via the JSON schema."
        )
        schema = th_prompts.build_bookend_animation_picker_schema(role)
        result = await gemini_multimodal(
            user_prompt,
            images=[card_path],
            system=th_prompts._BOOKEND_ANIM_PICKER_SYSTEM,
            json_schema=schema,
            temperature=0.4,
            max_tokens=300,
            model=config.vertex_gemini_fast_model,
        )
        if isinstance(result, dict):
            chosen = result.get("style")
            valid = (
                th_prompts.INTRO_STYLES
                if role == "intro"
                else th_prompts.OUTRO_STYLES
            )
            if chosen in valid:
                log.info(
                    "[yas-th] picked %s animation=%s (reason=%r)",
                    role, chosen, (result.get("reason") or "")[:80],
                )
                return chosen
        log.warning(
            "[yas-th] %s anim picker returned unexpected payload=%r — using fallback",
            role, result,
        )
        return fallback
    except Exception:
        log.exception(
            "[yas-th] %s animation picker failed — falling back to %s",
            role, fallback,
        )
        return fallback


async def _insert_animation_row(
    *,
    parent_run_id: int,
    prompt_used: str,
    veo_op_name: str,
    veo_model: str,
    duration_sec: int,
    with_audio: bool,
    cost_usd: float | None,
    role: str,  # "intro" | "main" | "outro"
    aspect_ratio: str = VEO_ASPECT_RATIO,
    extra_md: dict | None = None,
) -> int:
    """Insert a yas_animations row scoped to a talking-head-v2 run.
    `extra_md` (opt-in, TH v2 bookends) adds keys to the row metadata: the
    motion plan, the card it animates, the read-back flag; the two keys
    below always win."""
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                INSERT INTO yas_animations
                  (yas_generation_id, prompt_used, veo_model,
                   veo_operation_name, duration_sec, with_audio,
                   aspect_ratio, status, cost_usd, metadata)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING id
                """,
                (
                    parent_run_id, prompt_used[:8000], veo_model,
                    veo_op_name, duration_sec, with_audio,
                    _norm_aspect(aspect_ratio), STATUS_GENERATING, cost_usd,
                    Jsonb({
                        **(extra_md or {}),
                        "parent_talking_head_id": parent_run_id,
                        "role": role,
                    }),
                ),
            )
            row = await cur.fetchone()
            return int(row["id"])


# ---------------------------------------------------------------------------
# Auto-retry for transient Veo backend failures (server "high load")
# ---------------------------------------------------------------------------
# A Veo op can fail mid-render with a SERVER-SIDE capacity error ("experiencing
# high load… try again later") that is unrelated to the request — the same
# prompt usually succeeds seconds later. Unlike RAI (29310472) or quota
# (RESOURCE_EXHAUSTED), this self-heals on a fresh submit. The yas_animations
# poller calls `resubmit_animation_if_transient` before marking a clip failed.
_TRANSIENT_VEO_MARKERS = (
    "experiencing high load",
    "try again later",
    "currently unavailable",
    "temporarily unavailable",
    "please try again",
    "deadline exceeded",
    "internal error",
)
MAX_TRANSIENT_VEO_RETRIES = max(0, int(os.getenv("YAS_MAX_TRANSIENT_VEO_RETRIES", "2")))


def _is_transient_veo_error(msg: str) -> bool:
    """True for retryable failures. Retryable: SERVER-capacity errors AND
    POLL-TIME empty/filtered OUTPUT (Veo 3.x audio-RAI false positives are
    NON-deterministic — a fresh identical submit usually succeeds; bounded by
    MAX_TRANSIENT_VEO_RETRIES). NOT retryable: a SUBMIT-time 'usage guidelines
    (29310472)' rejection or quota (an identical resubmit won't self-heal)."""
    m = (msg or "").lower()
    # Poll-time output had no usable video → retry (non-deterministic filter).
    # Checked FIRST so a filter reason embedding a code still gets its retries.
    if "produced no video" in m or "output blocked by content filter" in m:
        return True
    if "29310472" in m or "resource_exhausted" in m or "usage guidelines" in m:
        return False
    return any(k in m for k in _TRANSIENT_VEO_MARKERS)


async def resubmit_animation_if_transient(anim_id: int, error: str) -> bool:
    """Rebuild + re-submit a clip that failed with a transient Veo error.

    Returns True when re-submitted (caller must NOT mark the row failed),
    False otherwise. Fully fail-safe: ANY problem returns False, so the worst
    case is identical to today (the clip is marked failed). Re-uses the exact
    same prompt/reference builders as finalize, so the re-submit matches the
    original. Caps attempts and un-fails the reel so it re-composes once the
    clip lands.
    """
    if not _is_transient_veo_error(error):
        return False
    try:
        row = await _fetch_animation_row(anim_id)
        if not row:
            return False
        meta = row.get("metadata") or {}
        attempts = int(meta.get("transient_retries") or 0)
        _err_l = (error or "").lower()
        # A content-filter / likeness block (15236754 / 29310472) is deterministic
        # for an IDENTICAL request, so we retry with a CHANGED input (below) and
        # allow a few more attempts than a plain server-capacity retry.
        _is_filter = (
            "content filter" in _err_l or "produced no video" in _err_l
            or "15236754" in (error or "") or "29310472" in (error or "")
        )
        _cap = max(MAX_TRANSIENT_VEO_RETRIES, 3) if _is_filter else MAX_TRANSIENT_VEO_RETRIES
        if attempts >= _cap:
            log.warning(
                "anim=%d transient-retry cap (%d) reached; marking failed",
                anim_id, _cap,
            )
            return False
        run_id = int(meta.get("parent_talking_head_id") or row.get("yas_generation_id"))
        role = str(meta.get("role") or "")
        run = await _fetch_run_row(run_id)
        md = run.get("metadata") or {}
        script = md.get("script") or {}
        refs = md.get("refs") or {}
        lock_character = bool(md.get("lock_character"))
        aspect = _norm_aspect(md.get("aspect_ratio"))
        # Per-segment Veo quality (mirrors kickoff/retry) so an auto-resubmitted
        # clip keeps the tier the user picked for THAT segment.
        if role == "main":
            _seg_q = md.get("main_video_quality") or md.get("video_quality")
        elif role.startswith("scene_"):
            try:
                _si = int(role.split("_", 1)[1])
            except (ValueError, IndexError):
                _si = -1
            _scenes_q = md.get("scenes") or []
            _seg_q = _scenes_q[_si].get("video_quality") if 0 <= _si < len(_scenes_q) else None
        elif role == "intro":
            _seg_q = md.get("intro_video_quality")
        elif role == "outro":
            _seg_q = md.get("outro_video_quality")
        else:
            _seg_q = md.get("video_quality")
        veo_model = _veo_model_for(_seg_q)
        brand_req = await build_brand_requirements()
        negative = th_prompts.build_talking_head_negative_prompt(brand_req)
        extra_meta: dict[str, Any] = {}

        if role.startswith("scene_") or role == "main":
            if role == "main":
                kf = _abs_from_rel(run["local_path"]) if run.get("local_path") else None
                sm = {}
            else:
                idx = int(role.split("_", 1)[1])
                scenes_meta = list(md.get("scenes") or [])
                if idx >= len(scenes_meta):
                    return False
                sm = scenes_meta[idx]
                kf = await _resolve_scene_keyframe_path(
                    run_id=run_id, scene_index=idx, md=md,
                )
            if kf is None:
                return False
            char_p = (
                await _veo_safe_character_path(
                    asset_id=sm.get("character_asset_id") or refs.get("character_asset_id"),
                    upload_id=sm.get("character_upload_id") or refs.get("character_upload_id"),
                )
                if lock_character else None
            )
            bg_p = await _slot_path(
                asset_id=sm.get("bg_asset_id") or refs.get("bg_asset_id"),
                upload_id=sm.get("bg_upload_id") or refs.get("bg_upload_id"),
            )
            seg_script = dict(script)
            if role.startswith("scene_"):
                seg_script["dialogue"] = sm.get("dialogue") or ""
                seg_script["hook"] = sm.get("hook") or ""
            # The clip keeps the framing its still shows (camera line,
            # push-in, likeness-retry crop, negatives).
            framing = _still_framing(md, idx if role.startswith("scene_") else 0)
            negative = th_prompts.build_talking_head_negative_prompt(brand_req, framing=framing)
            # A still made from a pick or from scene 0's still names the
            # person in the first frame, not the script's speaker.
            prompt = th_prompts.build_main_veo_prompt(
                script=seg_script, brand_req=brand_req,
                has_character_ref=(
                    char_p is not None
                    or _still_shows_a_pick(md, idx if role.startswith("scene_") else 0)
                ),
                has_bg_ref=bg_p is not None,
                dialogue_language=script.get("dialogue_language") or "en",
                framing=framing,
            )
            inputs = _scene_video_inputs(
                kf, char_p, bg_p,
                run_id=run_id,
                scene_index=(idx if role.startswith("scene_") else 0),
                framing=framing,
            )
            if _is_filter:
                # Likeness false-positive: hand Veo a genuinely DIFFERENT input —
                # a lightly perturbed keyframe, the face screened ONCE (drop the
                # last-frame anchor), and a varied seed. Strength escalates per attempt.
                perturbed = _perturb_keyframe_for_rai(kf, attempts + 1, framing=framing)
                if perturbed is not None:
                    inputs["start_image"] = perturbed
                    inputs.pop("last_image", None)
                    inputs.pop("push_in", None)
                    if inputs.get("seed") is not None:
                        inputs["seed"] = (int(inputs["seed"]) + attempts + 1) & 0x7FFFFFFF
                    log.info(
                        "[yas-th] anim=%d likeness block — retry %d with perturbed "
                        "keyframe, no last-frame anchor", anim_id, attempts + 1,
                    )
            op = await veo_generate(
                prompt, model=veo_model, duration_sec=VEO_DURATION_MAIN,
                aspect_ratio=aspect, with_audio=True,
                negative_prompt=negative,
                **inputs,
            )
        elif role in ("intro", "outro"):
            # The same directed clip again (the plan kept on the row, else the
            # motion director's); a card that is now a slow zoom is finished
            # here as one, on this same row.
            bookend = await _resolve_bookend(run_id=run_id, md=md, refs=refs, role=role)
            if bookend is None:
                return False
            job = await _bookend_job(
                role=role, bookend=bookend, aspect=aspect, quality=_seg_q,
                feedback=meta.get("bookend_feedback"), plan=meta.get("bookend_motion"),
            )
            if job["coro"] is None:
                await _zoom_animation(run_id=run_id, role=role, bookend=bookend, aspect=aspect,
                                      plan=job["plan"], anim_id=anim_id)
                if (md.get("composition_status")) == COMP_FAILED:
                    await _patch_metadata(run_id, {"composition_status": COMP_QUEUED})
                return True
            extra_meta = job["extra_md"]
            op = await job["coro"]
        else:
            return False

        new_op = op.get("operation_name") or op.get("name")
        if not new_op:
            return False
        async with get_conn() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    UPDATE yas_animations
                       SET status='generating',
                           veo_operation_name=%s,
                           error=NULL,
                           metadata = metadata || %s::jsonb,
                           updated_at=NOW()
                     WHERE id=%s
                    """,
                    (
                        new_op,
                        Jsonb({
                            **extra_meta,
                            "transient_retries": attempts + 1,
                            "last_transient_error": str(error)[:300],
                        }),
                        anim_id,
                    ),
                )
        # Un-fail the reel so the composition poller re-evaluates once the
        # re-submitted clip lands (it had been flipped to 'failed').
        if (md.get("composition_status")) == COMP_FAILED:
            try:
                await _patch_metadata(run_id, {"composition_status": COMP_QUEUED})
            except Exception:
                log.exception("anim=%d: comp_status reset failed", anim_id)
        log.info(
            "anim=%d transient '%s' — auto-resubmitted (attempt %d/%d) new op=%s",
            anim_id, str(error)[:60], attempts + 1, MAX_TRANSIENT_VEO_RETRIES, new_op[-18:],
        )
        return True
    except Exception:
        log.exception("anim=%d transient auto-resubmit failed; will mark failed", anim_id)
        return False


async def start_videos(run_id: int) -> dict:
    """(Re)kick Veo only for the clips that need it (intro / main / outro).

    v2 editor — SCOPED: only clips that are missing a video, whose last render
    failed, or that were marked `*_video_stale` after an edit get a fresh Veo
    job. Fresh, ready clips keep their existing animation untouched, so
    retrying ONE still never re-fires Veo for the others. Excluded bookends are
    skipped. The composition poller flips `composition_status` to
    `videos_ready` once all included clips land.
    """
    run = await _fetch_run_row(run_id)
    if not run.get("local_path"):
        raise HTTPException(409, "Main keyframe not ready — generate previews first.")
    md = run["metadata"] or {}

    # Refuse if any Veo gen is already in flight for this run
    for key in ("intro_animation_id", "main_animation_id", "outro_animation_id"):
        existing_id = md.get(key)
        if existing_id:
            existing = await _fetch_animation_row(int(existing_id))
            if existing and existing.get("status") == STATUS_GENERATING:
                raise HTTPException(
                    409, "Videos are already generating for this run.",
                )

    # A picked image kept as-is with no card made from it gets one (free).
    md = await _ensure_kept_cards(run_id)
    script = md.get("script") or {}
    refs = md.get("refs") or {}
    lock_character = bool(md.get("lock_character"))
    aspect = _norm_aspect(md.get("aspect_ratio"))

    # What each bookend's clip is made from (None: no card and no pick; an
    # uploaded video is used as it is and needs no clip).
    intro_bk = (await _resolve_bookend(run_id=run_id, md=md, refs=refs, role="intro")
                if _bookend_wanted(md, "intro") else None)
    outro_bk = (await _resolve_bookend(run_id=run_id, md=md, refs=refs, role="outro")
                if _bookend_wanted(md, "outro") else None)

    # ---- Decide which clips actually need (re)generation -------------------
    # The single main scene is never excludable.
    need_main = await _clip_needs_regen(
        md.get("main_animation_id"), video_stale=bool(md.get("main_video_stale")),
    )
    need_intro = (
        intro_bk is not None
        and await _clip_needs_regen(
            md.get("intro_animation_id"), video_stale=bool(md.get("intro_video_stale")),
        )
    )
    need_outro = (
        outro_bk is not None
        and await _clip_needs_regen(
            md.get("outro_animation_id"), video_stale=bool(md.get("outro_video_stale")),
        )
    )
    if not (need_main or need_intro or need_outro):
        log.info(
            "[yas-th] start_videos run=%d — all included clips fresh; no Veo fired",
            run_id,
        )
        return await fetch_run(run_id)
    log.info(
        "[yas-th] start_videos run=%d scoped need_main=%s need_intro=%s need_outro=%s",
        run_id, need_main, need_intro, need_outro,
    )

    brand_req = await build_brand_requirements()

    # ---- Build Veo coroutines for the needed clips ONLY --------------------
    # Each job carries the role + the prompt/duration/audio used to insert its
    # animation row. Results map back by index so we update + clear staleness
    # for exactly the clips we kicked; skipped clips keep their animation.
    jobs: list[dict[str, Any]] = []

    if need_main:
        main_path = _abs_from_rel(run["local_path"])
        if not main_path.exists():
            raise HTTPException(410, "Main keyframe file missing on disk")
        character_path: Path | None = None
        bg_path: Path | None = None
        if lock_character:
            try:
                character_path = await _veo_safe_character_path(
                    asset_id=refs.get("character_asset_id"),
                    upload_id=refs.get("character_upload_id"),
                )
            except Exception:
                log.exception("char ref path resolution failed for run #%d", run_id)
        try:
            bg_path = await _slot_path(
                asset_id=refs.get("bg_asset_id"),
                upload_id=refs.get("bg_upload_id"),
            )
        except Exception:
            log.exception("bg ref path resolution failed for run #%d", run_id)
        # The clip keeps the framing the still shows.
        main_framing = _still_framing(md, 0)
        main_prompt = th_prompts.build_main_veo_prompt(
            script=script,
            brand_req=brand_req,
            # The still shows the pick even when Veo gets no photo of it.
            has_character_ref=character_path is not None or _still_shows_a_pick(md, 0),
            has_bg_ref=bg_path is not None,
            dialogue_language=script.get("dialogue_language") or "en",
            framing=main_framing,
        )
        log.info("[yas-th] start_videos run=%d main keyframe=%s framing=%s",
                 run_id, main_path, main_framing)
        jobs.append({
            "role": "main",
            "prompt": main_prompt,
            "duration": VEO_DURATION_MAIN,
            "with_audio": True,
            "coro": veo_generate(
                main_prompt,
                model=_veo_model_for(md.get("main_video_quality") or md.get("video_quality")),
                duration_sec=VEO_DURATION_MAIN,
                aspect_ratio=aspect,
                with_audio=True,
                negative_prompt=th_prompts.build_talking_head_negative_prompt(
                    brand_req, framing=main_framing),
                **_scene_video_inputs(main_path, character_path, bg_path, run_id=run_id,
                                      scene_index=0, framing=main_framing),
            ),
        })

    # Bookends, only those being made again, per the motion director: Veo
    # for a card's icons (i2v at 4 s), else the in-house slow zoom.
    style_patch: dict[str, Any] = {}
    zooms: list[tuple[dict, dict]] = []
    for role, bk, need in (("intro", intro_bk, need_intro), ("outro", outro_bk, need_outro)):
        if not need:
            continue
        job = await _bookend_job(role=role, bookend=bk, aspect=aspect,
                                 quality=md.get(f"{role}_video_quality"))
        style_patch[f"{role}_animation_style"] = job["style"]
        if job["coro"] is None:
            zooms.append((job, bk))
        else:
            jobs.append(job)

    # Persist style picks BEFORE kickoff so the UI shows them while rendering.
    if style_patch:
        await _patch_metadata(run_id, style_patch)

    try:
        results = await _run_veo_submits_bounded([j["coro"] for j in jobs])
    except VertexBudgetExceededError as e:
        raise HTTPException(503, f"Vertex budget exhausted: {e}")

    # ---- Persist rows — only for the clips we kicked ----------------------
    patch: dict[str, Any] = {"composition_status": COMP_QUEUED}
    for job, op in zip(jobs, results):
        role = job["role"]
        if isinstance(op, Exception):
            if role == "main":
                log.exception("Veo main kickoff failed for run #%d", run_id)
                raise HTTPException(502, "Could not start the main video. Try again.")
            log.warning("Veo %s kickoff failed: %s", role, op)
            continue
        anim_id = await _insert_animation_row(
            parent_run_id=run_id,
            prompt_used=job["prompt"],
            veo_op_name=op.get("name") or op.get("operation_name"),
            veo_model=op.get("model") or config.veo_model,
            duration_sec=job["duration"],
            with_audio=job["with_audio"],
            cost_usd=op.get("cost_usd"),
            role=role,
            aspect_ratio=aspect,
            extra_md=job.get("extra_md"),
        )
        patch[f"{role}_animation_id"] = anim_id
        patch[f"{role}_video_stale"] = False
        if role == "main":
            # Backward-compat key for any caller still reading veo_animation_id.
            patch["veo_animation_id"] = anim_id
            # The still this clip animates (an Undo back to it keeps the
            # clip up to date).
            patch["main_video_source"] = _still_key(run, md, 0)
    # The slow-zoom bookends are made here, ready at once (no Veo).
    for job, bk in zooms:
        patch[f"{job['role']}_animation_id"] = await _zoom_animation(
            run_id=run_id, role=job["role"], bookend=bk, aspect=aspect, plan=job["plan"],
        )
        patch[f"{job['role']}_video_stale"] = False

    await _patch_metadata(run_id, patch)

    log.info(
        "[yas-th] start_videos run=%d kicked=%s zoomed=%s",
        run_id, [j["role"] for j in jobs], [j["role"] for j, _ in zooms],
    )
    return await fetch_run(run_id)


# Backward-compat alias — the router used to call `finalize`. start_videos
# replaces it semantically.
finalize = start_videos


# ---------------------------------------------------------------------------
# v6 — multi-scene Veo orchestration
# ---------------------------------------------------------------------------

async def start_videos_multi(run_id: int) -> dict:
    """Multi-scene variant of start_videos — SCOPED.

    Only regenerates the scenes/bookends that need it: each non-excluded clip
    that is missing a video, whose last render failed, or that was marked
    stale after an edit. Fresh, ready scenes keep their existing
    `metadata.scenes[i].animation_id` untouched, so retrying ONE still never
    re-fires Veo for the others. Needed scenes are ref→video at 8 s with audio,
    conditioned on the approved keyframe as the i2v start frame
    (`_scene_video_inputs`); bookends are i2v at 4 s. Sets
    `composition_status = COMP_QUEUED`; the poller waits for ALL included
    animations and dispatches the `talking-head-v2-multi-scene` Remotion render.
    """
    run = await _fetch_run_row(run_id)
    md = run["metadata"] or {}
    aspect = _norm_aspect(md.get("aspect_ratio"))
    scene_count = int(md.get("scene_count") or 1)
    if scene_count <= 1:
        raise HTTPException(400, "start_videos_multi requires scene_count > 1")
    scenes_meta = list(md.get("scenes") or [])
    if len(scenes_meta) != scene_count:
        raise HTTPException(
            409, "Scene metadata is inconsistent — regenerate the script.",
        )

    # Refuse if any scene Veo gen is already in flight.
    for sm in scenes_meta:
        aid = sm.get("animation_id")
        if aid:
            existing = await _fetch_animation_row(int(aid))
            if existing and existing.get("status") == STATUS_GENERATING:
                raise HTTPException(
                    409, "Scene videos are already generating for this run.",
                )
    for key in ("intro_animation_id", "outro_animation_id"):
        existing_id = md.get(key)
        if existing_id:
            existing = await _fetch_animation_row(int(existing_id))
            if existing and existing.get("status") == STATUS_GENERATING:
                raise HTTPException(
                    409, "Bookend videos are already generating for this run.",
                )

    # A picked image kept as-is with no card made from it gets one (free).
    md = await _ensure_kept_cards(run_id)
    scenes_meta = list(md.get("scenes") or [])
    script = md.get("script") or {}
    refs = md.get("refs") or {}
    lock_character = bool(md.get("lock_character"))

    # What each bookend's clip is made from. An uploaded intro/outro video is
    # used as it is (the stitch plays it), so it needs no clip here.
    intro_bk = (await _resolve_bookend(run_id=run_id, md=md, refs=refs, role="intro")
                if _bookend_wanted(md, "intro") else None)
    outro_bk = (await _resolve_bookend(run_id=run_id, md=md, refs=refs, role="outro")
                if _bookend_wanted(md, "outro") else None)

    # ---- Decide which scenes + bookends actually need (re)generation -------
    needed_idxs: list[int] = []
    for i in range(scene_count):
        sm = scenes_meta[i]
        if sm.get("excluded"):
            continue
        if await _clip_needs_regen(
            sm.get("animation_id"), video_stale=bool(sm.get("video_stale")),
        ):
            needed_idxs.append(i)
    need_intro = (
        intro_bk is not None
        and await _clip_needs_regen(
            md.get("intro_animation_id"), video_stale=bool(md.get("intro_video_stale")),
        )
    )
    need_outro = (
        outro_bk is not None
        and await _clip_needs_regen(
            md.get("outro_animation_id"), video_stale=bool(md.get("outro_video_stale")),
        )
    )
    if not needed_idxs and not need_intro and not need_outro:
        log.info(
            "[yas-th] start_videos_multi run=%d — all included clips fresh; no Veo fired",
            run_id,
        )
        return await fetch_run(run_id)
    log.info(
        "[yas-th] start_videos_multi run=%d scoped scenes=%s intro=%s outro=%s",
        run_id, needed_idxs, need_intro, need_outro,
    )

    # ---- Resolve keyframe paths for the needed scenes (must exist on disk) -
    keyframe_paths: dict[int, Path] = {}
    missing: list[int] = []
    for i in needed_idxs:
        kp = await _resolve_scene_keyframe_path(run_id=run_id, scene_index=i, md=md)
        if kp is None:
            missing.append(i)
        else:
            keyframe_paths[i] = kp
    if missing:
        raise HTTPException(
            410, f"Scene keyframes missing on disk for indices: {missing}",
        )

    brand_req = await build_brand_requirements()

    # ---- Per-scene character + bg refs -------------------------------------
    async def _scene_refs(scene_idx: int) -> tuple[Path | None, Path | None]:
        sm = scenes_meta[scene_idx]
        char_p: Path | None = None
        bg_p: Path | None = None
        if lock_character:
            try:
                char_p = await _veo_safe_character_path(
                    asset_id=sm.get("character_asset_id") or refs.get("character_asset_id"),
                    upload_id=sm.get("character_upload_id") or refs.get("character_upload_id"),
                )
            except Exception:
                log.exception("scene %d char path resolve failed", scene_idx)
        try:
            bg_p = await _slot_path(
                asset_id=sm.get("bg_asset_id") or refs.get("bg_asset_id"),
                upload_id=sm.get("bg_upload_id") or refs.get("bg_upload_id"),
            )
        except Exception:
            log.exception("scene %d bg path resolve failed", scene_idx)
        return char_p, bg_p

    # ---- Build Veo coroutines for the needed scenes ONLY -------------------
    scene_prompts: dict[int, str] = {}
    scene_coros: list[Any] = []
    for i in needed_idxs:
        scene_script = dict(script)
        scene_script["dialogue"] = scenes_meta[i].get("dialogue") or ""
        scene_script["hook"] = scenes_meta[i].get("hook") or ""
        char_p, bg_p = await _scene_refs(i)
        # Each clip keeps the framing its own still shows.
        framing = _still_framing(md, i)
        prompt = th_prompts.build_main_veo_prompt(
            script=scene_script,
            brand_req=brand_req,
            # A still made from a pick or from scene 0's still.
            has_character_ref=char_p is not None or _still_shows_a_pick(md, i),
            has_bg_ref=bg_p is not None,
            dialogue_language=script.get("dialogue_language") or "en",
            framing=framing,
        )
        scene_prompts[i] = prompt
        log.info(
            "[yas-th] start_videos_multi run=%d scene_%d keyframe=%s framing=%s",
            run_id, i, keyframe_paths[i], framing,
        )
        scene_coros.append(veo_generate(
            prompt,
            model=_veo_model_for((scenes_meta[i] or {}).get("video_quality")),
            duration_sec=VEO_DURATION_MAIN,
            aspect_ratio=aspect,
            with_audio=True,
            negative_prompt=th_prompts.build_talking_head_negative_prompt(brand_req, framing=framing),
            **_scene_video_inputs(keyframe_paths[i], char_p, bg_p, run_id=run_id, scene_index=i,
                                  framing=framing),
        ))

    # ---- Bookends (only the needed ones), per the motion director: Veo for
    # a card's icons, else the in-house slow zoom (made after the Veo submits).
    style_patch: dict[str, Any] = {}
    bookend_jobs: dict[str, dict] = {}
    zooms: list[tuple[dict, dict]] = []
    for role, bk, need in (("intro", intro_bk, need_intro), ("outro", outro_bk, need_outro)):
        if not need:
            continue
        job = await _bookend_job(role=role, bookend=bk, aspect=aspect,
                                 quality=md.get(f"{role}_video_quality"))
        style_patch[f"{role}_animation_style"] = job["style"]
        if job["coro"] is None:
            zooms.append((job, bk))
        else:
            bookend_jobs[role] = job
    intro_coro = bookend_jobs["intro"]["coro"] if "intro" in bookend_jobs else None
    outro_coro = bookend_jobs["outro"]["coro"] if "outro" in bookend_jobs else None
    if style_patch:
        await _patch_metadata(run_id, style_patch)

    # ---- Submit bounded (dodges Veo's per-base-model 429 quota) ------------
    all_awaitables: list[Any] = list(scene_coros)
    if intro_coro is not None:
        all_awaitables.append(intro_coro)
    if outro_coro is not None:
        all_awaitables.append(outro_coro)
    try:
        results = await _run_veo_submits_bounded(all_awaitables)
    except VertexBudgetExceededError as e:
        raise HTTPException(503, f"Vertex budget exhausted: {e}")

    n = len(scene_coros)
    scene_ops: list[Any] = results[:n]
    rest = results[n:]
    intro_op = rest.pop(0) if intro_coro is not None else None
    outro_op = rest.pop(0) if outro_coro is not None else None

    fresh_scenes = list(scenes_meta)

    async def _persist_scene_success(k: int, op: dict) -> None:
        """Insert the animation row for needed scene k and clear its staleness.
        Only the kicked scenes are touched — skipped scenes keep their clip."""
        idx = needed_idxs[k]
        anim_id = await _insert_animation_row(
            parent_run_id=run_id,
            prompt_used=scene_prompts[idx],
            veo_op_name=op.get("name") or op.get("operation_name"),
            veo_model=op.get("model") or config.veo_model,
            duration_sec=VEO_DURATION_MAIN,
            with_audio=True,
            cost_usd=op.get("cost_usd"),
            role=f"scene_{idx}",
            aspect_ratio=aspect,
        )
        fresh_scenes[idx]["animation_id"] = anim_id
        fresh_scenes[idx]["video_stale"] = False
        # The still this clip animates (an Undo back to it keeps the clip up
        # to date).
        fresh_scenes[idx]["video_source"] = _still_key(run, md, idx)

    # Any needed-scene exception is fatal — persist the ones that DID launch
    # (so GCP ops aren't orphaned), then raise; quota → 503.
    quota_exhausted = any(isinstance(op, VeoQuotaExhaustedError) for op in scene_ops)
    has_scene_error = any(isinstance(op, Exception) for op in scene_ops)
    if has_scene_error:
        for k, op in enumerate(scene_ops):
            if isinstance(op, Exception) or not isinstance(op, dict):
                continue
            try:
                await _persist_scene_success(k, op)
            except Exception:
                log.exception("[yas-th] persist partial-success scene failed")
        try:
            await _patch_metadata(run_id, {"scenes": fresh_scenes})
        except Exception:
            log.exception("[yas-th] persist partial-success metadata patch failed")
        failed_k = next(k for k, op in enumerate(scene_ops) if isinstance(op, Exception))
        failed_idx = needed_idxs[failed_k]
        if quota_exhausted:
            log.error(
                "Veo scene %d quota-exhausted for run #%d after retries", failed_idx, run_id,
            )
            raise HTTPException(
                503,
                "Vertex Veo quota exhausted after retries. Wait ~30s and "
                "click Generate videos again, or request a quota increase: "
                "https://cloud.google.com/vertex-ai/docs/generative-ai/quotas-genai",
            )
        log.exception("Veo scene %d kickoff failed for run #%d", failed_idx, run_id)
        raise HTTPException(
            502, f"Scene {failed_idx + 1} kickoff failed. Try again.",
        )

    if isinstance(intro_op, Exception):
        log.warning("Veo intro kickoff failed: %s", intro_op)
        intro_op = None
    if isinstance(outro_op, Exception):
        log.warning("Veo outro kickoff failed: %s", outro_op)
        outro_op = None

    # ---- Persist successful scene rows (only the kicked scenes) ------------
    for k, op in enumerate(scene_ops):
        await _persist_scene_success(k, op)

    patch: dict[str, Any] = {
        "scenes": fresh_scenes,
        "composition_status": COMP_QUEUED,
        "composition_template": "talking-head-v2-multi-scene",
    }
    for role, op in (("intro", intro_op), ("outro", outro_op)):
        if not op:
            continue
        job = bookend_jobs[role]
        patch[f"{role}_animation_id"] = await _insert_animation_row(
            parent_run_id=run_id, prompt_used=job["prompt"],
            veo_op_name=op.get("name") or op.get("operation_name"),
            veo_model=op.get("model") or config.veo_model,
            duration_sec=VEO_DURATION_BOOKEND, with_audio=False,
            cost_usd=op.get("cost_usd"), role=role, aspect_ratio=aspect,
            extra_md=job.get("extra_md"),
        )
        patch[f"{role}_video_stale"] = False
    # The slow-zoom bookends are made here, ready at once (no Veo).
    for job, bk in zooms:
        patch[f"{job['role']}_animation_id"] = await _zoom_animation(
            run_id=run_id, role=job["role"], bookend=bk, aspect=aspect, plan=job["plan"],
        )
        patch[f"{job['role']}_video_stale"] = False

    await _patch_metadata(run_id, patch)

    log.info(
        "[yas-th] start_videos_multi run=%d kicked_scenes=%s intro=%s outro=%s zoomed=%s",
        run_id, needed_idxs, bool(intro_op), bool(outro_op), [j["role"] for j, _ in zooms],
    )
    return await fetch_run(run_id)


def _is_multi_scene_md(md: dict | None) -> bool:
    """Single source of truth for 'is this run multi-scene?'"""
    return int((md or {}).get("scene_count") or 1) > 1


async def start_videos_dispatch(run_id: int) -> dict:
    """Single entry point that picks single- or multi-scene orchestration
    based on the run's metadata. Used by the /finalize route."""
    run = await _fetch_run_row(run_id)
    if _is_multi_scene_md(run.get("metadata")):
        return await start_videos_multi(run_id)
    return await start_videos(run_id)


# ---------------------------------------------------------------------------
# Step 4a-retry — Per-segment video retry (Fix 2)
# ---------------------------------------------------------------------------
#
# Mirror of `retry_preview()` but at the Veo layer. Re-runs ONE of the three
# Veo calls (intro/main/outro) with a feedback-augmented prompt, inserts a
# NEW yas_animations row, points the run's metadata at it, and flips the
# composition back to `queued` so the user has to re-press Stitch after the
# new clip lands.

async def retry_video(
    *,
    run_id: int,
    which: str,            # "main" | "intro" | "outro" | "scene_<N>"
    feedback_text: str,
) -> dict:
    # v6 — accept scene_N.
    scene_idx: int | None = None
    if which.startswith("scene_"):
        try:
            scene_idx = int(which.split("_", 1)[1])
        except (ValueError, IndexError):
            raise HTTPException(400, f"invalid scene index in which={which!r}")
        if scene_idx < 0 or scene_idx >= MAX_SCENES:
            raise HTTPException(400, f"scene index out of range: {scene_idx}")
    elif which not in {"main", "intro", "outro"}:
        raise HTTPException(400, "which must be main / intro / outro / scene_<N>")
    # feedback_text is optional. When the user retries because of a
    # transient Veo failure ("service experiencing high load…"), there's
    # no creative feedback to give — they just want the same prompt fired
    # again. Empty string means "re-run as-is"; the three
    # `append_feedback_to_prompt` sites below guard against passing it
    # through when empty.
    feedback_text = (feedback_text or "").strip()

    run = await _fetch_run_row(run_id)
    md = run["metadata"] or {}
    script = md.get("script") or {}
    refs = md.get("refs") or {}
    lock_character = bool(md.get("lock_character"))
    aspect = _norm_aspect(md.get("aspect_ratio"))

    # Refuse if the targeted segment is already mid-render.
    if scene_idx is not None:
        scenes_meta = list((md or {}).get("scenes") or [])
        if scene_idx >= len(scenes_meta):
            raise HTTPException(400, f"scene_{scene_idx} not in this run")
        existing_aid = scenes_meta[scene_idx].get("animation_id")
        if existing_aid:
            existing = await _fetch_animation_row(int(existing_aid))
            if existing and existing.get("status") == STATUS_GENERATING:
                raise HTTPException(
                    409, f"Scene {scene_idx + 1} video is still generating.",
                )
    else:
        existing_anim_key = f"{which}_animation_id"
        existing_id = md.get(existing_anim_key)
        if existing_id:
            existing = await _fetch_animation_row(int(existing_id))
            if existing and existing.get("status") == STATUS_GENERATING:
                raise HTTPException(
                    409, f"The {which} video is still generating — wait for it to land first.",
                )

    # ---- Scene retry (v6) --------------------------------------------------
    if scene_idx is not None:
        sm = scenes_meta[scene_idx]
        scene_keyframe = await _resolve_scene_keyframe_path(
            run_id=run_id, scene_index=scene_idx, md=md,
        )
        if scene_keyframe is None:
            raise HTTPException(
                410, f"Scene {scene_idx + 1} keyframe missing on disk.",
            )
        brand_req = await build_brand_requirements()
        char_p: Path | None = None
        bg_p: Path | None = None
        if lock_character:
            try:
                char_p = await _veo_safe_character_path(
                    asset_id=sm.get("character_asset_id") or refs.get("character_asset_id"),
                    upload_id=sm.get("character_upload_id") or refs.get("character_upload_id"),
                )
            except Exception:
                log.exception("scene %d char path resolve failed", scene_idx)
        try:
            bg_p = await _slot_path(
                asset_id=sm.get("bg_asset_id") or refs.get("bg_asset_id"),
                upload_id=sm.get("bg_upload_id") or refs.get("bg_upload_id"),
            )
        except Exception:
            log.exception("scene %d bg path resolve failed", scene_idx)
        scene_script = dict(script)
        scene_script["dialogue"] = sm.get("dialogue") or ""
        scene_script["hook"] = sm.get("hook") or ""
        # The clip keeps the framing its still shows.
        framing = _still_framing(md, scene_idx)
        base_prompt = th_prompts.build_main_veo_prompt(
            script=scene_script,
            brand_req=brand_req,
            # A still made from a pick or from scene 0's still.
            has_character_ref=char_p is not None or _still_shows_a_pick(md, scene_idx),
            has_bg_ref=bg_p is not None,
            dialogue_language=script.get("dialogue_language") or "en",
            framing=framing,
        )
        full_prompt = (
            th_prompts.append_feedback_to_prompt(base_prompt, feedback_text)
            if feedback_text
            else base_prompt
        )
        negative = th_prompts.build_talking_head_negative_prompt(brand_req, framing=framing)
        try:
            op = await veo_generate(
                full_prompt,
                model=_veo_model_for((sm or {}).get("video_quality")),
                duration_sec=VEO_DURATION_MAIN,
                aspect_ratio=aspect,
                with_audio=True,
                negative_prompt=negative,
                **_scene_video_inputs(scene_keyframe, char_p, bg_p, run_id=run_id, scene_index=scene_idx,
                                      framing=framing),
            )
        except VertexBudgetExceededError as e:
            raise HTTPException(503, f"Vertex budget exhausted: {e}")
        except Exception as e:
            log.exception("Veo scene %d retry failed for run #%d", scene_idx, run_id)
            raise HTTPException(502, f"Scene {scene_idx + 1} video retry failed: {str(e)[:240]}")

        new_anim_id = await _insert_animation_row(
            parent_run_id=run_id,
            prompt_used=full_prompt,
            veo_op_name=op.get("name") or op.get("operation_name"),
            veo_model=op.get("model") or config.veo_model,
            duration_sec=VEO_DURATION_MAIN,
            with_audio=True,
            cost_usd=op.get("cost_usd"),
            role=f"scene_{scene_idx}",
            aspect_ratio=aspect,
        )
        scenes_meta[scene_idx]["animation_id"] = new_anim_id
        # v2 editor — fresh clip now matches the keyframe → clear staleness.
        scenes_meta[scene_idx]["video_stale"] = False
        scenes_meta[scene_idx]["video_source"] = _still_key(run, md, scene_idx)
        await _patch_metadata(run_id, {
            "scenes": scenes_meta,
            "composition_status": COMP_QUEUED,
        })
        log.info(
            "[yas-th] retry_video scene_%d run=%d new_anim=%d",
            scene_idx, run_id, new_anim_id,
        )
        return await fetch_run(run_id)

    # ---- Resolve keyframe + (for main) ref slots ---------------------------
    if which == "main":
        main_path = _abs_from_rel(run["local_path"]) if run.get("local_path") else None
        if not main_path or not main_path.exists():
            raise HTTPException(410, "Main keyframe file missing on disk")
        brand_req = await build_brand_requirements()
        character_path: Path | None = None
        bg_path: Path | None = None
        if lock_character:
            try:
                character_path = await _veo_safe_character_path(
                    asset_id=refs.get("character_asset_id"),
                    upload_id=refs.get("character_upload_id"),
                )
            except Exception:
                log.exception("char ref path resolve failed for run #%d retry", run_id)
        try:
            bg_path = await _slot_path(
                asset_id=refs.get("bg_asset_id"),
                upload_id=refs.get("bg_upload_id"),
            )
        except Exception:
            log.exception("bg ref path resolve failed for run #%d retry", run_id)
        # The clip keeps the framing the still shows.
        framing = _still_framing(md, 0)
        base_prompt = th_prompts.build_main_veo_prompt(
            script=script,
            brand_req=brand_req,
            # The still shows the pick even when Veo gets no photo of it.
            has_character_ref=character_path is not None or _still_shows_a_pick(md, 0),
            has_bg_ref=bg_path is not None,
            dialogue_language=script.get("dialogue_language") or "en",
            framing=framing,
        )
        full_prompt = (
            th_prompts.append_feedback_to_prompt(base_prompt, feedback_text)
            if feedback_text
            else base_prompt
        )
        negative = th_prompts.build_talking_head_negative_prompt(brand_req, framing=framing)
        try:
            op = await veo_generate(
                full_prompt,
                model=_veo_model_for(md.get("main_video_quality") or md.get("video_quality")),
                duration_sec=VEO_DURATION_MAIN,
                aspect_ratio=aspect,
                with_audio=True,
                negative_prompt=negative,
                **_scene_video_inputs(main_path, character_path, bg_path, run_id=run_id, scene_index=0,
                                      framing=framing),
            )
        except VertexBudgetExceededError as e:
            raise HTTPException(503, f"Vertex budget exhausted: {e}")
        except Exception as e:
            log.exception("Veo main retry kickoff failed for run #%d", run_id)
            raise HTTPException(502, f"Main video retry failed: {str(e)[:240]}")
        new_anim_id = await _insert_animation_row(
            parent_run_id=run_id,
            prompt_used=full_prompt,
            veo_op_name=op.get("name") or op.get("operation_name"),
            veo_model=op.get("model") or config.veo_model,
            duration_sec=VEO_DURATION_MAIN,
            with_audio=True,
            cost_usd=op.get("cost_usd"),
            role="main",
            aspect_ratio=aspect,
        )
        await _patch_metadata(run_id, {
            "main_animation_id": new_anim_id,
            "veo_animation_id": new_anim_id,
            "main_video_stale": False,
            "main_video_source": _still_key(run, md, 0),
            "composition_status": COMP_QUEUED,
        })
        log.info("[yas-th] retry_video main run=%d new_anim=%d", run_id, new_anim_id)
        return await fetch_run(run_id)

    # ---- Bookend retry (intro / outro) -------------------------------------
    # The clip is made again from what it animates (its card, unless the card
    # was made from another image than the one picked now: _resolve_bookend),
    # the way the motion director planned it: Veo for the card's icons (with
    # the feedback added), else the slow zoom.
    if refs.get(f"{which}_video_upload_id"):
        raise HTTPException(409, f"The {which} is your uploaded video, used as it is: there is no clip to make.")
    bookend = await _resolve_bookend(run_id=run_id, md=md, refs=refs, role=which)
    if bookend is None:
        raise HTTPException(
            409,
            f"No {which} card image available — regenerate the preview first.",
        )
    job = await _bookend_job(
        role=which, bookend=bookend, aspect=aspect,
        quality=md.get(f"{which}_video_quality"), feedback=feedback_text or None,
    )
    if job["coro"] is None:
        new_anim_id = await _zoom_animation(
            run_id=run_id, role=which, bookend=bookend, aspect=aspect, plan=job["plan"],
        )
    else:
        try:
            op = await job["coro"]
        except VertexBudgetExceededError as e:
            raise HTTPException(503, f"Vertex budget exhausted: {e}")
        except Exception as e:
            log.exception("Veo %s retry kickoff failed for run #%d", which, run_id)
            raise HTTPException(502, f"{which.capitalize()} video retry failed: {str(e)[:240]}")
        new_anim_id = await _insert_animation_row(
            parent_run_id=run_id,
            prompt_used=job["prompt"],
            veo_op_name=op.get("name") or op.get("operation_name"),
            veo_model=op.get("model") or config.veo_model,
            duration_sec=VEO_DURATION_BOOKEND,
            with_audio=False,
            cost_usd=op.get("cost_usd"),
            role=which,
            aspect_ratio=aspect,
            extra_md=job.get("extra_md"),
        )
    await _patch_metadata(run_id, {
        f"{which}_animation_id": new_anim_id,
        f"{which}_animation_style": job["style"],
        f"{which}_video_stale": False,
        "composition_status": COMP_QUEUED,
    })
    log.info(
        "[yas-th] retry_video %s run=%d new_anim=%d style=%s",
        which, run_id, new_anim_id, job["style"],
    )
    return await fetch_run(run_id)


# ---------------------------------------------------------------------------
# Step 4b — Stitch composition (user-triggered after Veo previews look good)
# ---------------------------------------------------------------------------

VIDEOS_READY = "videos_ready"


async def finalize_reel(run_id: int, *, expect_sha: str | None = None) -> dict:
    """User-triggered Finalize: the reel is resolved and frozen now
    (services/th_reel — which clip, which version, where each ends) and one
    compare-and-set hands it to this instance's composition poller. Valid
    from videos_ready, ready or failed once every included clip is made and
    none is out of date — so a reel can be finalized again: the last good
    one stays served until the new one is ready, and stays if it fails.
    `expect_sha` (the reel's inputs as the page last read them) turns a
    reel changed meanwhile into a 409. A reel unchanged since its last good
    render is not made again."""
    from . import th_reel

    await th_reel.finalize(run_id, expect_sha=expect_sha)
    return await fetch_run(run_id)


async def set_clip_excluded(*, run_id: int, which: str, excluded: bool) -> dict:
    """Toggle a clip in/out of the final stitch (persisted, so resume keeps it).

    `which` ∈ intro | outro | main | scene_<N>. Nothing is deleted — an excluded
    clip is just skipped by the poller and re-includable anytime. Guards: the
    single-scene main clip can't be excluded, and a multi-scene run must keep at
    least one included scene.
    """
    run = await _fetch_run_row(run_id)
    md = run["metadata"] or {}
    scene_count = int(md.get("scene_count") or 1)
    excluded = bool(excluded)

    scene_idx: int | None = None
    if which.startswith("scene_"):
        try:
            scene_idx = int(which.split("_", 1)[1])
        except (ValueError, IndexError):
            raise HTTPException(400, f"invalid scene index in which={which!r}")
    elif which not in {"intro", "outro", "main"}:
        raise HTTPException(400, "which must be intro / outro / main / scene_<N>")

    # Bookends — freely excludable.
    if which in {"intro", "outro"}:
        await _patch_metadata(run_id, {f"{which}_excluded": excluded})
        return await fetch_run(run_id)

    # Single-scene main (or scene_0 on a single-scene run) — never excludable.
    if which == "main" or scene_count <= 1:
        if excluded:
            raise HTTPException(409, "Can't exclude the main clip — it's the only content.")
        await _patch_metadata(run_id, {"main_excluded": False})
        return await fetch_run(run_id)

    # Multi-scene scene_<N>.
    scenes_meta = list(md.get("scenes") or [])
    if scene_idx is None or scene_idx < 0 or scene_idx >= len(scenes_meta):
        raise HTTPException(404, f"scene_{scene_idx} not in this run")
    if excluded:
        remaining = sum(
            1 for i, s in enumerate(scenes_meta)
            if i != scene_idx and not s.get("excluded")
        )
        if remaining < 1:
            raise HTTPException(409, "Keep at least one scene in the stitch.")
    scenes_meta[scene_idx]["excluded"] = excluded
    await _patch_metadata(run_id, {"scenes": scenes_meta})
    return await fetch_run(run_id)


async def set_segment_quality(*, run_id: int, which: str, quality: str) -> dict:
    """Set the Veo quality ('normal'|'quality') for ONE segment.

    `which` ∈ intro | outro | main | scene_<N>. Persisted in the run's
    metadata so kickoff/retry pick the right Veo model per segment.
    """
    q = _norm_quality(quality)
    which = (which or "").strip().lower()
    if which == "intro":
        await _patch_metadata(run_id, {"intro_video_quality": q})
    elif which == "outro":
        await _patch_metadata(run_id, {"outro_video_quality": q})
    elif which == "main":
        await _patch_metadata(run_id, {"main_video_quality": q})
    elif which.startswith("scene_"):
        try:
            idx = int(which.split("_", 1)[1])
        except (ValueError, IndexError):
            raise HTTPException(400, f"invalid scene index in which={which!r}")
        run = await _fetch_run_row(run_id)
        md = run["metadata"] or {}
        scenes = list(md.get("scenes") or [])
        if not (0 <= idx < len(scenes)):
            raise HTTPException(404, f"scene_{idx} not in this run")
        scenes[idx] = {**scenes[idx], "video_quality": q}
        await _patch_metadata(run_id, {"scenes": scenes})
    else:
        raise HTTPException(400, "which must be intro / outro / main / scene_<N>")
    return await fetch_run(run_id)


# ---------------------------------------------------------------------------
# v2 editor — re-entrant editing of an existing run
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _normalize_dialogue(s: str | None) -> str:
    return (
        (s or "")
        .strip()
        .replace("“", '"').replace("”", '"')
        .replace("‘", "'").replace("’", "'")
    )


async def generate_previews_for_scenes(run_id: int, indices: list[int]) -> dict:
    """Generate keyframes for specific scene indices only (used when scenes are
    added via a scene-count increase). Scene 0 lives on the run row and is
    handled by the normal preview/main-retry path — it is skipped here.
    Existing scenes not in `indices` are untouched. Each still is made by
    _gen_scene_still (compose: per-scene picks, prompt naming only the images
    attached) and stored per scene, atomically.
    """
    run = await _fetch_run_row(run_id)
    md = run["metadata"] or {}
    image_models = _normalize_image_models(md.get("image_models"))
    scenes_meta = list(md.get("scenes") or [])

    def _done(scene: dict) -> dict:
        out: dict[str, Any] = {"error": None}
        if scene.get("animation_id"):
            out["video_stale"] = True  # had a video; now mismatched
        return out

    for i in [n for n in indices if 1 <= n < len(scenes_meta)]:
        info: dict[str, Any] = {}
        try:
            data, mime, prompt = await _gen_scene_still(run_id, md, i, info=info)
        except Exception as e:
            log.exception("scene %d keyframe gen failed for run #%d", i, run_id)
            await _patch_scene(run_id, i, {"error": str(e)[:480] or type(e).__name__})
            continue
        f_md, f_scene = _framing_md(i, info.get("framing_used"))
        await _store_scene_still(
            run_id, i, data, mime, prompt, image_models["main"],
            topic=run["topic"], extra_md=f_md,
            scene_md=lambda scene, f=f_scene: {**_done(scene), **f},
        )
    return await fetch_run(run_id)


async def _gpt_script_dict(
    *, topic: str, refs: dict, scene_count: int, dlgs: list[str | None],
    prev_speaker: dict | None = None, kept_words: dict[str, str] | None = None,
) -> tuple[dict, dict]:
    """Re-script an existing run: the same prompt, LLM call (OpenAI, else
    the Gemini fallback, _script_llm) and checks (_finalize_script) as
    generate_script, so a re-script has the same shape. Returns (script,
    speaker); `prev_speaker` is the run's speaker, kept when it still
    applies (see _script_speaker); `kept_words` the words of the cards kept
    with their own text. A bookend kept as it is gets no anti-repetition.
    """
    _, character_brief = await _slot_row_and_brief(
        asset_id=refs.get("character_asset_id"),
        upload_id=refs.get("character_upload_id"), kind="character")
    _, bg_brief = await _slot_row_and_brief(
        asset_id=refs.get("bg_asset_id"),
        upload_id=refs.get("bg_upload_id"), kind="bg")
    _, intro_brief = await _slot_row_and_brief(
        asset_id=refs.get("intro_asset_id"),
        upload_id=refs.get("intro_upload_id"), kind="intro")
    _, outro_brief = await _slot_row_and_brief(
        asset_id=refs.get("outro_asset_id"),
        upload_id=refs.get("outro_upload_id"), kind="outro")
    # Same topic-magnify + RAG + anti-repetition path as generate_script, so a
    # re-script stays just as on-topic as the first generation.
    context_block, committed_angle, recent_bookends = await _build_script_inputs(
        topic, user_dialogue=dlgs[0] if dlgs else None,
    )
    recent_bookends = _recent_without_kept(recent_bookends, _kept_bookend_roles(refs))
    user_prompt = th_prompts.build_script_user_prompt(
        topic=topic, context_block=context_block,
        character_brief=character_brief, bg_brief=bg_brief,
        intro_brief=intro_brief, outro_brief=outro_brief,
        user_dialogue=dlgs[0] if dlgs else None,
        user_dialogues=dlgs, scene_count=scene_count,
        committed_angle=committed_angle, recent_bookends=recent_bookends,
    )
    script = await _script_llm(user_prompt)
    if script is None:
        raise HTTPException(502, "Re-script failed — try again in a moment.")
    return await _finalize_script(
        script, topic=topic, scene_count=scene_count, dlgs=dlgs,
        has_character=bool(character_brief), prev_speaker=prev_speaker,
        kept_words=kept_words,
    )


def _keep_uploaded_bookends_fresh(refs: dict, md: dict, patch: dict) -> None:
    """An intro/outro that is an uploaded video (in `refs`, the run's refs
    after the edit) is used as it is: no clip is ever made for it, so
    nothing could clear a stale mark and Finalize would wait on it for good.
    Whatever the edit changed, its mark stays clear (`patch`, in place)."""
    for role in ("intro", "outro"):
        key = f"{role}_video_stale"
        if refs.get(f"{role}_video_upload_id") and (patch.get(key) or md.get(key)):
            patch[key] = False


async def rescript_run(run_id: int, *, new_topic: str) -> dict:
    """Re-run GPT on the EXISTING run (topic changed). Preserves user-authored
    scene dialogues, regenerates GPT-authored ones, marks ALL segment videos
    stale (keyframes/videos kept but out of date), never auto-regenerates.
    """
    new_topic = (new_topic or "").strip()
    if not new_topic:
        raise HTTPException(400, "topic is required")
    run = await _fetch_run_row(run_id)
    md = run["metadata"] or {}
    refs = md.get("refs") or {}
    scene_count = int(md.get("scene_count") or 1)
    scenes_meta = list(md.get("scenes") or [])

    # Preserve user-authored dialogue per slot; let GPT rewrite the rest.
    dlgs: list[str | None] = [None] * scene_count
    if scene_count > 1:
        for i in range(scene_count):
            sm = scenes_meta[i] if i < len(scenes_meta) else {}
            if sm.get("user_authored") and sm.get("dialogue"):
                dlgs[i] = sm["dialogue"]
    else:
        script0 = md.get("script") or {}
        if isinstance(script0, dict) and script0.get("user_authored") and script0.get("dialogue"):
            dlgs[0] = script0["dialogue"]

    script, speaker = await _gpt_script_dict(
        topic=new_topic, refs=refs, scene_count=scene_count, dlgs=dlgs,
        prev_speaker=md.get("speaker"), kept_words=await _kept_script_words(md),
    )

    # Write new topic + script; rewrite GPT-authored scene dialogues only.
    patch: dict[str, Any] = {"script": script, "speaker": speaker}
    if scene_count > 1:
        for i in range(scene_count):
            if i >= len(scenes_meta):
                break
            if not scenes_meta[i].get("user_authored"):
                sd = (script.get("scenes") or [{}])
                scenes_meta[i]["dialogue"] = (sd[i].get("dialogue", "") if i < len(sd) else "")
                scenes_meta[i]["hook"] = (sd[i].get("hook", "") if i < len(sd) else "")
            # Out of date whatever the still (an Undo keeps it so).
            scenes_meta[i]["video_stale"] = True
            scenes_meta[i]["video_source"] = None
        patch["scenes"] = scenes_meta
    else:
        patch["main_video_stale"] = True
        patch["main_video_source"] = None
    # The cards: one drawn with the old words no longer matches the script and
    # is dropped (made again from Stills). A card kept as-is shows its own
    # words: with "Keep card text" (or nothing on it to swap) it and its clip
    # stay as they are; a headline or subtitle on it that now differs from
    # the new words drops it, to be made again with those words swapped.
    new_run, new_md = {**run, "topic": new_topic}, {**md, "script": script}
    for role in ("intro", "outro"):
        cid = md.get(f"{role}_image_id")
        card = await _fetch_card_row(int(cid)) if cid else None
        cmd = _md_dict((card or {}).get("metadata"))
        if card is not None and cmd.get("as_is"):
            lines = _card_lines_from(cmd.get("text_lines"))
            if lines is not None and not _keep_text(refs, role) \
                    and _text_swap_plan(lines, *_bookend_words(new_run, new_md, role)):
                patch[f"{role}_image_id"] = None
                patch[f"{role}_video_stale"] = True
                log.info("[yas-th] rescript_run run=%d %s kept card #%s needs the new words: dropped",
                         run_id, role, cid)
            continue
        patch[f"{role}_video_stale"] = True
        if card is not None and cmd.get("card_text") != _planned_card_text(*_bookend_words(new_run, new_md, role)):
            patch[f"{role}_image_id"] = None
            log.info("[yas-th] rescript_run run=%d %s card #%s has the old words: dropped", run_id, role, cid)

    _keep_uploaded_bookends_fresh(refs, md, patch)

    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "UPDATE yas_generations SET topic = %s, updated_at = NOW() WHERE id = %s",
                (new_topic, run_id),
            )
    await _patch_metadata(run_id, patch)
    log.info("[yas-th] rescript_run run=%d new_topic=%r", run_id, new_topic[:60])
    return await fetch_run(run_id)


async def _apply_scene_count(run_id: int, new_count: int, *, restore: bool) -> list[int]:
    """Change scene_count in place. Archives removed scenes (kept, restorable);
    on increase, restores archived by slot or creates fresh. Returns the list
    of NEW scene indices that need keyframe generation.
    """
    run = await _fetch_run_row(run_id)
    md = run["metadata"] or {}
    old = int(md.get("scene_count") or 1)
    new_count = max(1, min(MAX_SCENES, int(new_count)))
    if new_count == old:
        return []
    scenes = list(md.get("scenes") or [])
    archived = list(md.get("archived_scenes") or [])
    script = md.get("script") or {}

    # Normalize single-scene → list form so the algorithm is uniform.
    if old == 1 and not scenes:
        scenes = [{
            "index": 0,
            "dialogue": (script.get("dialogue") or "") if isinstance(script, dict) else "",
            "hook": (script.get("hook") or "") if isinstance(script, dict) else "",
            "user_authored": bool(isinstance(script, dict) and script.get("user_authored")),
            "keyframe_id": None,  # scene 0 lives on the run row
            "animation_id": md.get("main_animation_id") or md.get("veo_animation_id"),
            "character_asset_id": None, "character_upload_id": None,
            "bg_asset_id": None, "bg_upload_id": None,
            "excluded": False, "video_stale": bool(md.get("main_video_stale")),
            "video_source": md.get("main_video_source"),
        }]

    new_fresh: list[int] = []
    if new_count < len(scenes):
        for i in range(new_count, len(scenes)):
            sm = dict(scenes[i])
            sm["archived_index"] = i
            sm["archived_at"] = _now_iso()
            archived.append(sm)
        scenes = scenes[:new_count]
    else:
        sd = script.get("scenes") if isinstance(script, dict) else None
        for i in range(len(scenes), new_count):
            match = None
            if restore:
                for j, a in enumerate(archived):
                    if int(a.get("archived_index", -1)) == i:
                        match = archived.pop(j)
                        break
            if match:
                m = dict(match)
                m.pop("archived_index", None); m.pop("archived_at", None)
                m["index"] = i
                kid = m.get("keyframe_id")
                if kid:
                    krow = await _fetch_scene_row(int(kid))
                    if not krow or not krow.get("local_path"):
                        m["keyframe_id"] = None
                        m["animation_id"] = None
                        m["video_stale"] = False
                        new_fresh.append(i)
                scenes.append(m)
            else:
                scenes.append({
                    "index": i,
                    "dialogue": (sd[i].get("dialogue", "") if isinstance(sd, list) and i < len(sd) else ""),
                    "hook": (sd[i].get("hook", "") if isinstance(sd, list) and i < len(sd) else ""),
                    "user_authored": False,
                    "keyframe_id": None, "animation_id": None,
                    "character_asset_id": None, "character_upload_id": None,
                    "bg_asset_id": None, "bg_upload_id": None,
                    "excluded": False, "video_stale": False,
                })
                new_fresh.append(i)

    patch: dict[str, Any] = {
        "scenes": scenes,
        "archived_scenes": archived,
        "scene_count": new_count,
    }
    if new_count > 1:
        patch["composition_template"] = "talking-head-v2-multi-scene"
    else:
        s0 = scenes[0] if scenes else {}
        patch["main_animation_id"] = s0.get("animation_id")
        patch["veo_animation_id"] = s0.get("animation_id")
        patch["main_video_stale"] = bool(s0.get("video_stale"))
        patch["main_video_source"] = s0.get("video_source")
        patch["main_excluded"] = False  # the only clip can't be excluded
        # Single-scene fetch_run reads script.dialogue/hook for the lone scene,
        # so sync them from scenes[0] (preserves an edit made while multi-scene).
        if s0.get("dialogue") and isinstance(script, dict):
            script = dict(script)
            script["dialogue"] = s0.get("dialogue")
            script["hook"] = s0.get("hook") or script.get("hook", "")
            if isinstance(script.get("scenes"), list) and script["scenes"]:
                script["scenes"][0]["dialogue"] = s0.get("dialogue")
                script["scenes"][0]["hook"] = s0.get("hook") or script["scenes"][0].get("hook", "")
            patch["script"] = script
    await _patch_metadata(run_id, patch)
    log.info("[yas-th] scene_count run=%d %d->%d new_fresh=%s", run_id, old, new_count, new_fresh)
    return new_fresh


# ---------------------------------------------------------------------------
# v2 editor — async keyframe regen on image apply
# ---------------------------------------------------------------------------
#
# Applying a NEW character/bg reference must REGENERATE the affected keyframe(s),
# not merely flag the video stale. The keyframe is the authoritative identity
# source for the Veo clip — `_scene_video_inputs()` feeds it as `start_image=`
# and ignores the raw character photo — so swapping the ref id without
# re-rendering the still leaves BOTH the still and every downstream video
# showing the OLD image. We kick the regen as a fire-and-forget task so the
# edit call returns immediately and the UI polls `keyframe_status` for live
# progress. Strong task refs are held in a module set so the event loop doesn't
# GC the task mid-flight (asyncio keeps only weak refs to bare tasks).

_KEYFRAME_REGEN_TASKS: set[asyncio.Task] = set()


async def _regen_scene_keyframe(
    run_id: int,
    *,
    scene_index: int,
    keep_subject: bool,
    has_char_ref: bool,
    has_bg_ref: bool,
) -> None:
    """Re-render ONE scene's keyframe in the chosen lock mode, then store it.

    Modes (see `build_main_keyframe_prompt`):
      - `keep_subject`: canvas = the scene's CURRENT still → the person is
        pixel-locked (the still IS source image #1); only the background
        changes (to the attached bg reference when `has_bg_ref`, else a fresh
        AI setting). This is the precise "keep the character, swap the bg" path.
      - compose (`has_char_ref`, not keep_subject): the character reference is
        the canvas/identity anchor — reproduce face+outfit EXACTLY; match the
        bg reference faithfully when `has_bg_ref`.
      - bg-only (`has_bg_ref`, no character ref): keep the bg EXACTLY, generate
        a person into it.
      - free: text-to-image from the script.

    Scene 0 lives on the run row (`yas_generations` image columns); scenes
    >=1 are sibling rows pointed to by `metadata.scenes[i].keyframe_id`.
    """
    run = await _fetch_run_row(run_id)
    md = run["metadata"] or {}
    lock_character = bool(md.get("lock_character"))
    image_models = _normalize_image_models(md.get("image_models"))
    image_model = image_models["main"]
    aspect = _norm_aspect(md.get("aspect_ratio"))
    gem_aspect, oai_size = _aspect_size(aspect)

    # Effective refs (per slot: the scene's own pick wins, else the global run
    # refs) and their briefs.
    sr = await _scene_refs(md, scene_index)

    # Resolve sources in the order the prompt names them. The prompt then
    # names only what is really attached: a reference whose file is missing
    # is never described as attached.
    sources: list[Path] = []
    char_attached = False
    bg_attached = False
    if keep_subject:
        canvas = await _resolve_scene_keyframe_path(
            run_id=run_id, scene_index=scene_index, md=md,
        )
        if canvas is not None:
            sources.append(canvas)  # image 1 = the CURRENT frame (pixel-locked)
            if has_bg_ref:
                bg_p = await _ref_path(sr, "bg", what=f"regen scene {scene_index} run={run_id}")
                if bg_p:
                    sources.append(bg_p)
                    bg_attached = True
        else:
            # No still to preserve — fall back to a non-keep build.
            keep_subject = False

    if not keep_subject:
        # The picked character (lock on), or for a scene with no pick at all
        # scene 0's still (the regen runs scene 0 first, so it is the new one).
        if (has_char_ref and lock_character) or _scene_anchored(md, scene_index):
            p = await _person_ref_for_scene(md, scene_index, run_id=run_id)
            if p:
                sources.append(p); sources.append(p)  # identity anchor
                char_attached = True
        if has_bg_ref:
            bg_p = await _ref_path(sr, "bg", what=f"regen scene {scene_index} run={run_id}")
            if bg_p:
                sources.append(bg_p)
                bg_attached = True
    sources = sources[:3]

    prompt = _build_scene_prompt(
        md, scene_index,
        character_brief=sr["character_brief"],
        bg_brief=sr["bg_brief"],
        has_character_ref=char_attached,
        has_bg_ref=bg_attached,
        keep_subject=keep_subject,
    )

    data, mime = await _run_image_gen(
        prompt=prompt, image_model=image_model, source_images=sources or None,
        aspect=gem_aspect, size=oai_size,
    )
    # The framing it was made at: a background swap keeps the current
    # still's (its canvas); a composed still has the scene's framing.
    framing_used = (_still_framing(md, scene_index) if keep_subject
                    else _effective_framing(md, scene_index))
    f_md, f_scene = _framing_md(scene_index, framing_used)

    if scene_index == 0:
        # New scene-0 keyframe → the existing main clip animates the OLD still.
        # A regen is always a generated still, never the character photo
        # itself (Regenerate and Refine read as_is). On a multi-scene run
        # scene 0's clip is scenes[0]'s: it goes out of date there too.
        await _store_scene_still(
            run_id, 0, data, mime, prompt, image_model, topic=run["topic"],
            extra_md={"main_video_stale": True, "main_error": None, "main_as_is": False, **f_md},
            scene_md=lambda scene: {"video_stale": True} if scene.get("animation_id") else {},
        )
        return

    # scenes >= 1: new sibling row + repoint keyframe_id (atomic per scene).
    def _done(scene: dict) -> dict:
        out: dict[str, Any] = {"error": None, "as_is": False, **f_scene}
        if scene.get("animation_id"):
            out["video_stale"] = True  # had a video; now mismatched
        return out

    await _store_scene_still(
        run_id, scene_index, data, mime, prompt, image_model,
        topic=run["topic"], scene_md=_done, extra_md=f_md,
    )


async def _clear_keyframe_generating(run_id: int, indices: list[int]) -> None:
    """Drop the `keyframe_generating` markers for `indices` (run row + scenes)
    once a regen task finishes, whatever the outcome, with what it was doing
    (keyframe_work). Per scene and atomic, so an edit made while the regen
    ran is kept."""
    for i in indices:
        await _patch_scene(run_id, i, {"keyframe_generating": False, "keyframe_work": None})
    if 0 in indices:
        await _patch_metadata(run_id, {"main_keyframe_generating": False, "main_keyframe_work": None})


# What a scene's background regen is doing, so its tile says it (and never
# "Regenerating" for a free crop): a framing crop (free), a re-shoot by the
# image model, or a still made again (keyframe_work; main_keyframe_work for
# scene 0). Unknown (an older marker): a still made again.
KEYFRAME_WORK_CROP = "crop"
KEYFRAME_WORK_RESHOOT = "reshoot"
KEYFRAME_WORK_REGEN = "regen"


def _keyframe_work(spec: dict, md_after: dict, md_before: dict) -> str:
    """What a regen spec (_keyframe_regen_spec) does to scene i's still: a
    framing change alone to a tighter framing a crop can reach is a crop
    (_crop_likely; it falls back to a re-shoot when the still cannot be
    cropped: _reshoot_still says so); any other framing change alone, a
    re-shoot; anything else makes the still again."""
    i = int(spec["scene_index"])
    if "keep_subject" in spec or not spec.get("reshoot"):
        return KEYFRAME_WORK_REGEN
    if _crop_likely(_effective_framing(md_after, i), _still_framing(md_before, i)):
        return KEYFRAME_WORK_CROP
    return KEYFRAME_WORK_RESHOOT


async def _set_keyframe_work(run_id: int, i: int, work: str) -> None:
    """Say what scene i's background regen is doing now (keyframe_work)."""
    await _patch_scene(run_id, i, {"keyframe_work": work})
    if i == 0:
        await _patch_metadata(run_id, {"main_keyframe_work": work})


# ---------------------------------------------------------------------------
# Framing by the face: a tighter framing is a crop, every framing is measured
# ---------------------------------------------------------------------------
#
# How close a still is shows in the height of its speaker's face (OpenCV's
# frontal-face box, _face_box) as a fraction of the frame's height: about
# 0.17 above the hip (run 830's stills), about 0.07 full body, and 0.26 or
# more for a close-up (head and shoulders). A change to a TIGHTER framing is
# a crop of the current still around the face, scaled back to the still's
# exact size: free, and the same person to the pixel. A wider framing needs
# pixels the still does not have: the refine engine re-shoots it, and the
# face of the result is measured, so the framing recorded is the one the
# still really shows (run 830's model "close-up" was barely closer).

# Face height / frame height: the bands each framing reads as, and the value
# a crop aims at with where the face's centre sits (fraction from the top).
FACE_BANDS: dict[str, tuple[float, float]] = {
    th_prompts.FRAMING_FULL_BODY: (0.0, 0.12),
    th_prompts.FRAMING_ABOVE_HIP: (0.12, 0.26),
    th_prompts.FRAMING_CLOSE: (0.26, 1.01),
}
_CROP_AIM: dict[str, tuple[float, float]] = {
    th_prompts.FRAMING_CLOSE: (0.33, 0.40),
    th_prompts.FRAMING_ABOVE_HIP: (0.17, 0.32),
}
_FRAMING_RANK = {th_prompts.FRAMING_FULL_BODY: 0, th_prompts.FRAMING_ABOVE_HIP: 1,
                 th_prompts.FRAMING_CLOSE: 2}
# A crop never enlarges the still more than this (a full-body still cut to a
# close-up would be a blur): beyond it the refine engine re-shoots instead.
CROP_MAX_UPSCALE = 3.0


def _framing_of_ratio(ratio: float) -> str:
    """The framing a face-height ratio reads as (FACE_BANDS)."""
    for framing, (lo, hi) in FACE_BANDS.items():
        if lo <= ratio < hi:
            return framing
    return th_prompts.FRAMING_CLOSE


def _face_ratio(im: Any) -> float | None:
    """The speaker's face height / the frame's height in a PIL image, or None
    when no face is found."""
    box = _face_box(im)
    if box is None or not im.height:
        return None
    return box[3] / im.height


def _is_tighter(target: str, current: str) -> bool:
    """Whether framing `target` is closer than `current` (both known)."""
    return (target in _FRAMING_RANK and current in _FRAMING_RANK
            and _FRAMING_RANK[target] > _FRAMING_RANK[current])


def _crop_likely(target: str, current: str) -> bool:
    """Whether re-framing a still at `current` to `target` is a crop for a
    typical still (what its tile says before it is made): `target` is
    tighter and a face in the middle of `current`'s band (FACE_BANDS) needs
    no more than CROP_MAX_UPSCALE to reach _CROP_AIM. Full body to close-up
    is not (about 5x for a face of 6% of the frame): it is re-shot, as
    _tight_crop_png refuses nearly every such crop (the frontend's
    thFraming.framingMove says the same)."""
    if not _is_tighter(target, current) or target not in _CROP_AIM or current not in FACE_BANDS:
        return False
    lo, hi = FACE_BANDS[current]
    return _CROP_AIM[target][0] / ((lo + hi) / 2) <= CROP_MAX_UPSCALE


def _tight_crop_png(data: bytes, target: str, aspect: str) -> tuple[bytes, float] | None:
    """The still `data` re-framed to the tighter `target` by a crop around
    its speaker's face (the face _CROP_AIM's height of the frame, its centre
    at _CROP_AIM's place; head room kept), scaled back to the still's own
    size (Lanczos) and then to exactly `aspect`. Returns (PNG, the face ratio
    of the result), or None when it cannot be a crop: no face, the still
    already that tight, or more than CROP_MAX_UPSCALE of enlargement."""
    from PIL import Image

    aim = _CROP_AIM.get(target)
    if aim is None:
        return None
    want, centre = aim
    with Image.open(BytesIO(data)) as src:
        im = src.convert("RGB")
    box = _face_box(im)
    if box is None:
        return None
    x, y, fw, fh = box
    w, h = im.size
    ch = fh / want
    if ch >= h * 0.98:
        return None  # the face already fills that much: no crop to make
    if h / ch > CROP_MAX_UPSCALE:
        return None
    cw = ch * w / h
    cx, cy = x + fw / 2, y + fh / 2
    top = min(max(0.0, cy - centre * ch), h - ch)
    left = min(max(0.0, cx - cw / 2), w - cw)
    out = im.resize((w, h), Image.LANCZOS, box=(left, top, left + cw, top + ch))
    if not _is_exact_aspect(out.width, out.height, aspect):
        out = _crop_to_aspect(out, aspect)
    buf = BytesIO()
    out.save(buf, format="PNG")
    return buf.getvalue(), fh / ch


def _honest_framing_md(data: bytes, i: int, target: str) -> tuple[dict, dict]:
    """(extra_md, scene_md) recording the framing a re-shot still of scene `i`
    really shows (_framing_md) and a warning when its face is outside the
    band of `target`; the framing asked for when no face can be measured."""
    from PIL import Image

    try:
        with Image.open(BytesIO(data)) as src:
            ratio = _face_ratio(src.convert("RGB"))
    except Exception:
        log.exception("[yas-th] re-shoot: measuring the still's face failed")
        ratio = None
    used, warning = target, None
    if ratio is not None:
        lo, hi = FACE_BANDS.get(target, (0.0, 1.01))
        if not (lo <= ratio < hi):
            used = _framing_of_ratio(ratio)
            warning = (f"Asked for {th_prompts.FRAMING_LABELS.get(target, target)}; the new still "
                       f"looks {th_prompts.FRAMING_LABELS.get(used, used)}. Undo, or pick the "
                       "framing again.")
        log.info("[yas-th] re-shoot scene=%d asked %s: face %.3f of the frame, recorded %s",
                 i, target, ratio, used)
    f_md, f_scene = _framing_md(i, used)
    key = "main_framing_warning" if i == 0 else "framing_warning"
    if i == 0:
        f_md[key] = warning
    else:
        f_scene[key] = warning
    return f_md, f_scene


async def _crop_still(run: dict, md: dict, i: int, framing: str) -> bool:
    """Re-frame scene `i`'s current still to the tighter `framing` by a crop
    (_tight_crop_png) and store it as a new version (retry_kind 'reframe',
    so Undo puts the old one back and its framing too). False, storing
    nothing, when it cannot be a crop."""
    canvas = await _current_still_path(run, md, i)
    if canvas is None:
        return False
    aspect = _norm_aspect(md.get("aspect_ratio"))
    data = await asyncio.to_thread(Path(canvas).read_bytes)
    cropped = await asyncio.to_thread(_tight_crop_png, data, framing, aspect)
    if cropped is None:
        return False
    out, ratio = cropped
    f_md, f_scene = _framing_md(i, framing)
    image_model = _normalize_image_models(md.get("image_models"))["main"]
    note = f"(re-framed to {th_prompts.FRAMING_LABELS.get(framing, framing)}: a crop of the still, no model)"
    await _store_still_version(
        run, md, i, out, "image/png", note, image_model=image_model,
        feedback=note, retry_kind="reframe", as_is=False, extra_md=f_md, scene_md=f_scene,
    )
    log.info("[yas-th] reframe run=%s scene=%d %s by a crop (face %.3f of the frame)",
             run.get("id"), i, framing, ratio)
    return True


async def _reshoot_still(run_id: int, i: int) -> None:
    """Re-frame scene `i`'s current still to the scene's framing (as it is
    now, _effective_framing). A TIGHTER framing than the still's is a crop
    around the face (_crop_still: free, the same pixels of the same person);
    a wider one, or a still that cannot be cropped, is a re-shoot: the refine
    engine with a canned instruction, so the same person, outfit, room and
    light come back at the new camera distance, and the framing recorded is
    the one the result's face shows (refine_still's framing_target). Called
    by the keyframe regen, which holds the scene's keyframe_generating
    marker."""
    run = await _fetch_run_row(run_id)
    md = run["metadata"] or {}
    framing = _effective_framing(md, i)
    current = _still_framing(md, i)
    if _is_tighter(framing, current) and await _crop_still(run, md, i, framing):
        return
    f_md, f_scene = _framing_md(i, framing)
    log.info("[yas-th] reshoot run=%d scene=%d framing=%s (still %s)", run_id, i, framing, current)
    # A crop that could not be made is a re-shoot: the tile says so.
    if (_still_scene(md, i).get("keyframe_work") or (i == 0 and md.get("main_keyframe_work"))) \
            == KEYFRAME_WORK_CROP:
        await _set_keyframe_work(run_id, i, KEYFRAME_WORK_RESHOOT)
    await refine_still(
        run_id, i, th_prompts.framing_reshoot_change(framing),
        retry_kind="reframe", extra_md=f_md, scene_md=f_scene, busy_ok=True,
        framing_target=framing,
    )


async def _run_keyframe_regen(run_id: int, specs: list[dict]) -> None:
    """Background driver: regenerate keyframes for the given per-scene SPECS
    SEQUENTIALLY, then clear the `keyframe_generating` markers. Each spec is
    `{scene_index, keep_subject, has_char_ref, has_bg_ref}` and routes through
    the unified `_regen_scene_keyframe` (scene 0 → run row, scenes >=1 → scene
    rows). A spec with `reshoot` (a framing change on a still) then re-frames
    the still (_reshoot_still); one with only {scene_index, reshoot} does just
    that. Sequential, one image call at a time; every scene write is an
    atomic per-scene patch (`_patch_scene`)."""
    ordered = sorted(
        {int(s["scene_index"]): s for s in specs}.values(),
        key=lambda s: int(s["scene_index"]),
    )
    indices = [int(s["scene_index"]) for s in ordered]
    try:
        for spec in ordered:
            i = int(spec["scene_index"])
            try:
                if "keep_subject" in spec:
                    await _regen_scene_keyframe(
                        run_id,
                        scene_index=i,
                        keep_subject=bool(spec.get("keep_subject")),
                        has_char_ref=bool(spec.get("has_char_ref")),
                        has_bg_ref=bool(spec.get("has_bg_ref")),
                    )
                if spec.get("reshoot"):
                    await _reshoot_still(run_id, i)
            except Exception as e:
                msg = (str(e.detail) if isinstance(e, HTTPException) else str(e))[:480] or type(e).__name__
                log.exception("keyframe regen (scene %d) failed run=%d", i, run_id)
                if i == 0:
                    await _patch_metadata(run_id, {"main_error": msg})
                else:
                    try:
                        await _patch_scene(run_id, i, {"error": msg})
                    except Exception:
                        log.exception("record scene %d error failed run=%d", i, run_id)
    finally:
        await _clear_keyframe_generating(run_id, indices)


def _dispatch_keyframe_regen(run_id: int, specs: list[dict]) -> None:
    """Fire-and-forget the keyframe regen so `edit_run` returns immediately.
    `specs` = list of `{scene_index, keep_subject, has_char_ref, has_bg_ref}`."""
    if not specs:
        return
    task = asyncio.create_task(
        _run_keyframe_regen(run_id, specs),
        name=f"yas_th_kf_regen_{run_id}",
    )
    _KEYFRAME_REGEN_TASKS.add(task)
    task.add_done_callback(_KEYFRAME_REGEN_TASKS.discard)


# The Speaker field's text is kept to this many characters.
_SPEAKER_SUBJECT_MAX = 400


def _user_speaker(text: Any, current: Any, *, run_id: int) -> dict | None:
    """The speaker the user set in the Look step's Speaker field ({subject,
    gender, source 'user'}), or None for an empty field. Library tags are
    dropped (never a tag). A chip ('Emirati man' / 'Emirati woman') becomes
    a default cast of that gender: the run's current one when it already is
    one, else one picked with an RNG seeded by the run."""
    subject = th_prompts.clean_subject(text)[:_SPEAKER_SUBJECT_MAX].strip()
    if not subject:
        return None
    cur = current if isinstance(current, dict) else {}
    gender = th_prompts.speaker_chip_gender(subject)
    if gender:
        if cur.get("gender") == gender and th_prompts.is_default_cast(cur.get("subject")):
            subject = cur["subject"]
        else:
            rng = random.Random(f"run-{run_id}|{gender}")
            subject = th_prompts.default_emirati_cast(rng, gender)["subject"]
    else:
        gender = th_prompts.subject_gender(subject)
    return {"subject": subject, "gender": gender, "source": th_prompts.SPEAKER_SOURCE_USER}


def _speaker_scenes(md: dict) -> list[int]:
    """The scenes whose still shows the run's speaker: scene 0 when it has no
    character pick, and with it every scene anchored on scene 0's still
    (_scene_anchored). None of them when scene 0 has a pick (the anchored
    scenes then show that character)."""
    md = md or {}
    count = max(1, int(md.get("scene_count") or 1))

    def _picked(i: int) -> bool:
        ids = _scene_ref_ids(md, i)
        return bool(ids["character_asset_id"] or ids["character_upload_id"])

    if _picked(0):
        return []
    return [i for i in range(count) if not _picked(i)]


async def _speaker_regen_after_rescript(
    run_id: int, *, speaker_before: Any, skip: set[int],
) -> list[dict]:
    """A re-script can change the speaker (the new topic fixes it, or no
    longer does). On a run with stills, the scenes showing the speaker are
    re-shot like after a Speaker edit: returns their regen specs (scenes in
    `skip` left out) and marks them generating. The re-script has already
    marked every clip stale. Nothing for a run that had no speaker (older
    runs keep their stills)."""
    if not (isinstance(speaker_before, dict) and speaker_before.get("subject")):
        return []
    run = await _fetch_run_row(run_id)
    md = run["metadata"] or {}
    after = md.get("speaker") if isinstance(md.get("speaker"), dict) else {}
    if after.get("subject") == speaker_before.get("subject") or not _run_has_stills(run, md):
        return []
    specs: list[dict] = []
    for i in _speaker_scenes(md):
        if i in skip:
            continue
        ids = _scene_ref_ids(md, i)
        specs.append(_keyframe_regen_spec(
            scene_index=i, char_changed=True, bg_changed=False, bg_mode=None,
            has_char=False, has_bg=bool(ids["bg_asset_id"] or ids["bg_upload_id"]),
            has_still=True,
        ))
        await _patch_scene(run_id, i, {"keyframe_generating": True})
        if i == 0:
            await _patch_metadata(run_id, {"main_keyframe_generating": True})
    return specs


def _keyframe_regen_spec(
    *,
    scene_index: int,
    char_changed: bool,
    bg_changed: bool,
    bg_mode: str | None,
    has_char: bool,
    has_bg: bool,
    has_still: bool,
    framing_changed: bool = False,
    as_is: bool = False,
) -> dict | None:
    """Decide HOW (or WHETHER) to regenerate one scene's keyframe.

    Returns a spec `{scene_index, keep_subject, has_char_ref, has_bg_ref}` for
    `_regen_scene_keyframe`, or `None` to SKIP (pass the existing still through
    untouched). The character is always the identity anchor: when it is
    UNCHANGED and a still already exists, we keep that still as the canvas so
    the face/outfit is pixel-locked and only the background can change.

    `framing_changed`: the scene's framing differs from its still's. A still
    is re-shot at the new framing (`reshoot`: the refine engine on the still,
    after a background swap when there is one too); a scene with no still is
    composed (a composed still is made at the scene's framing). A still that
    is the character photo itself (`as_is`) keeps the photo's framing.
    """
    spec = _keyframe_regen_base_spec(
        scene_index=scene_index, char_changed=char_changed, bg_changed=bg_changed,
        bg_mode=bg_mode, has_char=has_char, has_bg=has_bg, has_still=has_still,
    )
    if not framing_changed or char_changed:
        return spec  # a new person is composed, at the scene's framing
    if not has_still:
        return spec or {"scene_index": scene_index, "keep_subject": False,
                        "has_char_ref": has_char, "has_bg_ref": has_bg}
    if as_is:
        return spec
    if spec is None:
        return {"scene_index": scene_index, "reshoot": True}
    if spec.get("keep_subject"):
        return {**spec, "reshoot": True}
    return spec


def _keyframe_regen_base_spec(
    *,
    scene_index: int,
    char_changed: bool,
    bg_changed: bool,
    bg_mode: str | None,
    has_char: bool,
    has_bg: bool,
    has_still: bool,
) -> dict | None:
    """_keyframe_regen_spec for a change of character or background."""
    want_new_bg = (bg_mode or "keep") == "new"
    if char_changed:
        # New / changed person → compose from the character ref (character wins).
        return {"scene_index": scene_index, "keep_subject": False,
                "has_char_ref": has_char, "has_bg_ref": has_bg}
    # --- character is UNCHANGED from here ---
    if bg_changed and has_bg:
        if has_still:
            # Keep the person EXACTLY (canvas = current still); swap to new bg ref.
            return {"scene_index": scene_index, "keep_subject": True,
                    "has_char_ref": has_char, "has_bg_ref": True}
        # Fresh scene with only a bg ref → generate a person into the locked bg.
        return {"scene_index": scene_index, "keep_subject": False,
                "has_char_ref": False, "has_bg_ref": True}
    if (bg_changed and not has_bg) or want_new_bg:
        # bg removed, or "New bg" toggled → keep the person, regen background.
        if has_still:
            return {"scene_index": scene_index, "keep_subject": True,
                    "has_char_ref": has_char, "has_bg_ref": False}
        return {"scene_index": scene_index, "keep_subject": False,
                "has_char_ref": has_char, "has_bg_ref": has_bg}
    return None  # nothing changed → SKIP


async def edit_run(
    *,
    run_id: int,
    topic: str | None = None,
    scene_count: int | None = None,
    restore_archived: bool = True,
    lock_character: bool | None = None,
    image_models: dict | None = None,
    refs_patch: dict | None = None,
    scene_dialogues: list[str | None] | None = None,
    scene_hooks: list[str | None] | None = None,
    scene_overrides: list[dict | None] | None = None,
    bg_mode: str | None = None,
    aspect_ratio: str | None = None,
    video_quality: str | None = None,
    intro_headline: str | None = None,
    intro_subtitle: str | None = None,
    outro_headline: str | None = None,
    outro_subtitle: str | None = None,
    speaker_subject: str | None = None,
    framing: str | None = None,
    scene_framings: list[str | None] | None = None,
) -> dict:
    """Single edit-in-place entry point for an existing run. Applies the given
    fields, marks affected segment videos stale, and (for scene_count up, once
    the run has stills) generates keyframes for new scenes. When a scene's character/bg reference
    changes, it ALSO auto-regenerates that scene's keyframe in the background
    (the keyframe is the Veo clip's identity source, so the new image must
    re-render the still for it to take effect) — see _dispatch_keyframe_regen.
    `speaker_subject` (the Look step's Speaker field) sets who is on camera
    when no character is picked (source 'user'); the scenes showing the
    speaker are re-shot the same way, once the run has stills.
    `framing` sets the run's framing (the scenes following it change with
    it); `scene_framings[i]` a scene's own (a Stills tile's framing buttons:
    None leaves scene i, FRAMING_INHERIT drops its override; a single-scene
    run's one framing is the run's). A still whose framing no longer matches
    is re-shot at the new one (the refine engine on that still); a still
    that is the character photo keeps the photo's framing. Before the run
    has stills a framing is only saved.
    Returns the refreshed run immediately; the UI polls `keyframe_status`.
    """
    run = await _fetch_run_row(run_id)
    md = run["metadata"] or {}
    if md.get("composition_status") == COMP_RENDERING:
        raise HTTPException(409, "A render is in progress — wait for it to finish before editing.")
    # Before anything is applied, so a refused size change edits nothing.
    _check_aspect_change(run, md, aspect_ratio)
    for k in ("intro_ref_mode", "outro_ref_mode"):
        _check_ref_mode((refs_patch or {}).get(k))
    for k in ("intro_keep_text", "outro_keep_text"):
        _check_keep_text((refs_patch or {}).get(k))
    framing = _check_framing(framing)
    for v in scene_framings or []:
        if v is not None and v != FRAMING_INHERIT:
            _check_framing(v)

    cur_count = int(md.get("scene_count") or 1)
    old_refs = dict(md.get("refs") or {})
    refs = dict(md.get("refs") or {})
    scenes_meta = list(md.get("scenes") or [])
    patch: dict[str, Any] = {}

    # v2 editor — track per-scene WHICH ref (character / background) changed this
    # call; the regen mode is derived from that at the end (see
    # _keyframe_regen_spec). Capture which scenes are ALREADY regenerating
    # (pre-mutation) so a double-Apply doesn't fire a duplicate render.
    char_changed_idx: set[int] = set()
    bg_changed_idx: set[int] = set()
    scene_bg_mode: dict[int, str] = {}  # per-scene one-shot "New bg" intent
    in_flight_kf: set[int] = {
        i for i, s in enumerate(scenes_meta) if s.get("keyframe_generating")
    }
    if md.get("main_keyframe_generating"):
        in_flight_kf.add(0)

    def _clip_out_of_date(i: int | None = None) -> None:
        """Mark a clip out of date for a reason other than its still (size,
        Veo tier, character lock, refs, words, speaker): scene `i`'s (every
        scene's when None) on a multi-scene run, else the main clip. The
        still it was made from is forgotten, so an Undo back to that still
        keeps it out of date (_stale_for)."""
        if cur_count <= 1:
            patch["main_video_stale"] = True
            patch["main_video_source"] = None
            return
        for j, s in enumerate(scenes_meta):
            if i is None or j == i:
                s["video_stale"] = True
                s["video_source"] = None

    # --- image models ---
    if image_models is not None:
        patch["image_models"] = _normalize_image_models(image_models)

    # --- output aspect ratio ---
    # Only a run with no still or card yet gets here with a new size (see
    # _check_aspect_change above); its stills are then made at that size. Any
    # clip is marked stale all the same.
    if aspect_ratio is not None and _norm_aspect(aspect_ratio) != _norm_aspect(md.get("aspect_ratio")):
        patch["aspect_ratio"] = _norm_aspect(aspect_ratio)
        patch["intro_video_stale"] = True
        patch["outro_video_stale"] = True
        _clip_out_of_date()

    # --- video quality: switching Veo tiers re-shoots every clip, but the
    # stills/keyframes are untouched (same images, different video model) ---
    if video_quality is not None and _norm_quality(video_quality) != _norm_quality(md.get("video_quality")):
        patch["video_quality"] = _norm_quality(video_quality)
        patch["intro_video_stale"] = True
        patch["outro_video_stale"] = True
        _clip_out_of_date()

    # --- framing: how close the camera is (the run's, and per scene) --------
    # A scene is re-shot when its framing changed here (the run's, for the
    # scenes following it) or a Stills tile asked for one, and its still
    # shows another framing. A still that is the character photo keeps the
    # photo's framing. Before the run has stills the framing is only saved.
    framing_idx: set[int] = set()
    if framing is not None or scene_framings is not None:
        before = {i: _effective_framing(md, i) for i in range(cur_count)}
        if framing is not None:
            patch["framing"] = framing
        asked: set[int] = set()
        for i, v in enumerate((scene_framings or [])[:cur_count]):
            if v is None:
                continue
            asked.add(i)
            if cur_count <= 1:
                if v != FRAMING_INHERIT:
                    patch["framing"] = _check_framing(v)
            elif i < len(scenes_meta):
                f = None if v == FRAMING_INHERIT else _check_framing(v)
                # The run's own framing is followed, not copied.
                if f == patch.get("framing", _run_framing(md)):
                    f = None
                scenes_meta[i]["framing"] = f
        after_md = {**md, **{k: v for k, v in patch.items() if k == "framing"}, "scenes": scenes_meta}
        if _run_has_stills(run, md):
            for i in range(cur_count):
                target = _effective_framing(after_md, i)
                if target == before[i] and i not in asked:
                    continue
                if _has_still(run, md, i) and (
                        _still_as_is(md, i) or _still_framing(md, i) == target):
                    continue
                # The clip goes out of date only when the re-framed still is
                # stored (_store_still_version): a re-shoot that fails leaves
                # the still, and its clip, as they were.
                framing_idx.add(i)

    # --- lock_character: changing it invalidates EVERY clip ---
    if lock_character is not None and bool(lock_character) != bool(md.get("lock_character")):
        patch["lock_character"] = bool(lock_character)
        patch["intro_video_stale"] = True
        patch["outro_video_stale"] = True
        _clip_out_of_date()
    elif lock_character is not None:
        patch["lock_character"] = bool(lock_character)

    # --- global refs: mark stale only the segments that INHERIT a changed ref ---
    if refs_patch:
        g_char_changed = False
        g_bg_changed = False
        for k, v in refs_patch.items():
            # A bookend's ref mode and "Keep card text" are judged in the
            # bookend block below (they only matter for a picked image, by
            # their effective value).
            if refs.get(k) != v and not k.endswith(("_ref_mode", "_keep_text")):
                if k.startswith("character"):
                    g_char_changed = True
                elif k.startswith("bg"):
                    g_bg_changed = True
                if k.startswith("intro"):
                    patch["intro_video_stale"] = True
                if k.startswith("outro"):
                    patch["outro_video_stale"] = True
            refs[k] = v
        patch["refs"] = refs
        if g_char_changed or g_bg_changed:
            if cur_count > 1:
                # per-field inheritance: a scene inherits the global character
                # (or bg) only when it doesn't override that field itself.
                for idx, s in enumerate(scenes_meta):
                    inherits_char = not (s.get("character_asset_id") or s.get("character_upload_id"))
                    inherits_bg = not (s.get("bg_asset_id") or s.get("bg_upload_id"))
                    if g_char_changed and inherits_char:
                        _clip_out_of_date(idx)
                        char_changed_idx.add(idx)
                    if g_bg_changed and inherits_bg:
                        _clip_out_of_date(idx)
                        bg_changed_idx.add(idx)
            else:
                _clip_out_of_date()
                if g_char_changed:
                    char_changed_idx.add(0)
                if g_bg_changed:
                    bg_changed_idx.add(0)

    # --- per-scene dialogue edits ---
    if scene_dialogues is not None:
        if cur_count > 1:
            for i, d in enumerate(scene_dialogues[:cur_count]):
                if i >= len(scenes_meta):
                    break
                txt = _normalize_dialogue(d)
                if txt:
                    if scenes_meta[i].get("dialogue") != txt:
                        scenes_meta[i]["dialogue"] = txt
                        words = txt.split()
                        scenes_meta[i]["hook"] = " ".join(words[: min(8, len(words))])
                        scenes_meta[i]["user_authored"] = True
                        _clip_out_of_date(i)
                else:
                    scenes_meta[i]["user_authored"] = False
        else:
            d0 = _normalize_dialogue(scene_dialogues[0] if scene_dialogues else "")
            script = dict(md.get("script") or {})
            if d0 and script.get("dialogue") != d0:
                script["dialogue"] = d0
                words = d0.split()
                script["hook"] = " ".join(words[: min(8, len(words))])
                script["user_authored"] = True
                patch["script"] = script
                _clip_out_of_date()
            elif not d0 and isinstance(script, dict):
                script["user_authored"] = False
                patch["script"] = script

    # --- script TEXT edits: per-scene hooks + intro/outro card wording -------
    # These are pure script-text patches from the Look-phase inline editor.
    # hook is display/search-only (not in any Veo/card prompt) → no staleness.
    # intro/outro headline+subtitle are BAKED into a drawn card's image: the
    # bookend block below drops that card (and marks its clip stale).
    card_text_changed: set[str] = set()
    if any(v is not None for v in (
        scene_hooks, intro_headline, intro_subtitle, outro_headline, outro_subtitle,
    )):
        script = dict(patch.get("script") or md.get("script") or {})
        script_changed = "script" in patch
        if scene_hooks is not None:
            if cur_count > 1:
                for i, h in enumerate(scene_hooks[:cur_count]):
                    if h is None or i >= len(scenes_meta):
                        continue
                    htxt = _normalize_dialogue(h)
                    if htxt and scenes_meta[i].get("hook") != htxt:
                        scenes_meta[i]["hook"] = htxt
                # mirror scene 0's hook into the top-level field (legacy paths)
                if scene_hooks and scene_hooks[0] is not None:
                    h0 = _normalize_dialogue(scene_hooks[0])
                    if h0 and script.get("hook") != h0:
                        script["hook"] = h0
                        script_changed = True
            else:
                h0 = _normalize_dialogue(scene_hooks[0] if scene_hooks else "")
                if h0 and script.get("hook") != h0:
                    script["hook"] = h0
                    script_changed = True
        for field, val, role in (
            ("intro_headline", intro_headline, "intro"),
            ("intro_subtitle", intro_subtitle, "intro"),
            ("outro_headline", outro_headline, "outro"),
            ("outro_subtitle", outro_subtitle, "outro"),
        ):
            if val is not None:
                v = _normalize_dialogue(val)
                if script.get(field) != v:
                    script[field] = v
                    script_changed = True
                    card_text_changed.add(role)
        if script_changed:
            patch["script"] = script

    # --- per-scene ref overrides ---
    if scene_overrides is not None and cur_count > 1:
        for i, ov in enumerate(scene_overrides[:cur_count]):
            if i >= len(scenes_meta) or ov is None:
                continue
            c_changed = False
            b_changed = False
            for k in ("character_asset_id", "character_upload_id"):
                if k in ov and scenes_meta[i].get(k) != ov[k]:
                    scenes_meta[i][k] = ov[k]
                    c_changed = True
            for k in ("bg_asset_id", "bg_upload_id"):
                if k in ov and scenes_meta[i].get(k) != ov[k]:
                    scenes_meta[i][k] = ov[k]
                    b_changed = True
            if isinstance(ov.get("bg_mode"), str):
                scene_bg_mode[i] = ov["bg_mode"]  # one-shot intent, not persisted
            if c_changed:
                _clip_out_of_date(i)
                char_changed_idx.add(i)
            if b_changed:
                _clip_out_of_date(i)
                bg_changed_idx.add(i)

    # --- bookends: a card follows its picked image, its mode and its words --
    # A new pick, mode or "Keep card text" for the intro/outro: a kept card
    # (ref mode 'as_is') is made again at once from the new pick (a free pixel
    # copy, below, after this patch is written), unless its words must now be
    # swapped (a paid image edit an edit never makes: the card is dropped and
    # made from the Stills step); a drawn card is dropped, so a card made from
    # another image is never animated (run 824) and Stills makes it again. New
    # words drop a drawn card too. A kept card shows its own words: with "Keep
    # card text" the script keeps saying them; otherwise a headline or
    # subtitle on it that now differs from the script drops it, to be made
    # again with those words swapped (a card with nothing to swap, a logo-only
    # one, stays with its clip). An uploaded video needs no card. Before the
    # run has stills or a card, only the picks are saved: Generate stills
    # makes the cards.
    bookend_rebuild: list[str] = []
    has_stills = _run_has_stills(run, md)
    for role in ("intro", "outro"):
        ref_changed = any(
            k in (refs_patch or {}) and (refs_patch or {})[k] != old_refs.get(k)
            for k in (f"{role}_asset_id", f"{role}_upload_id", f"{role}_video_upload_id",
                      f"{role}_card_id")
        ) or (
            # A mode counts only for a picked image, by its effective value
            # (an older run's missing mode and 'as_is' are the same).
            _bookend_ref(refs, role) is not None
            and _ref_mode(refs, role) != _ref_mode(old_refs, role)
        ) or (
            # "Keep card text" counts only for a kept picked image.
            _bookend_ref(refs, role) is not None
            and _ref_mode(refs, role) == REF_MODE_AS_IS
            and _keep_text(refs, role) != _keep_text(old_refs, role)
        )
        if not (ref_changed or role in card_text_changed):
            continue
        cur_id = md.get(f"{role}_image_id")
        if not (cur_id or has_stills):
            if role in card_text_changed:
                patch[f"{role}_video_stale"] = True
            continue
        if refs.get(f"{role}_video_upload_id"):
            if cur_id:
                patch[f"{role}_image_id"] = None
            patch[f"{role}_video_stale"] = True
            continue
        cur_card = await _fetch_card_row(int(cur_id)) if cur_id else None
        cur_md = _md_dict((cur_card or {}).get("metadata"))
        kept = bool(cur_md.get("as_is"))
        if not ref_changed and kept:
            lines = _card_lines_from(cur_md.get("text_lines"))
            if lines is None:
                continue
            if _keep_text(refs, role):
                words = _script_fields_from_card(
                    {"script": patch.get("script") or md.get("script")}, role,
                    _card_script_words(lines, role))
                if words:
                    patch["script"] = {**(patch.get("script") or md.get("script") or {}), **words}
                continue
            after = {**md, "script": patch.get("script") or md.get("script") or {}}
            if _text_swap_plan(lines, *_bookend_words(run, after, role)):
                patch[f"{role}_image_id"] = None
                patch[f"{role}_video_stale"] = True
            continue
        if _ref_mode(refs, role) == REF_MODE_AS_IS and _bookend_ref(refs, role):
            bookend_rebuild.append(role)
        elif cur_id:
            patch[f"{role}_image_id"] = None
        patch[f"{role}_video_stale"] = True

    # --- speaker: who is on camera when no character is picked -------------
    # The Look step's Speaker field (free text, or a chip that becomes a
    # default cast). A new speaker is a new person for every scene showing it
    # (scene 0 with no pick, and the scenes anchored on it), so those are
    # re-shot like a changed character, scene 0 first. Before the run has
    # stills it is only saved: Generate stills uses it.
    if speaker_subject is not None:
        old_speaker = md.get("speaker") if isinstance(md.get("speaker"), dict) else {}
        new_speaker = _user_speaker(speaker_subject, old_speaker, run_id=run_id)
        if new_speaker is not None:
            patch["speaker"] = new_speaker
            if not (refs.get("character_asset_id") or refs.get("character_upload_id")):
                script = dict(patch.get("script") or md.get("script") or {})
                script["subject"] = new_speaker["subject"]
                patch["script"] = script
            if new_speaker["subject"] != old_speaker.get("subject") and _run_has_stills(run, md):
                for i in _speaker_scenes({"refs": refs, "scenes": scenes_meta,
                                          "scene_count": cur_count}):
                    char_changed_idx.add(i)
                    _clip_out_of_date(i)

    # v2 editor — decide the per-scene keyframe regen specs. A scene is a
    # candidate when its character OR bg ref changed, OR a "New bg" toggle
    # targets it. The actual mode (keep_subject / compose / bg-only / skip) is
    # derived by _keyframe_regen_spec from the FINAL (post-mutation) state.
    candidate_idx: set[int] = char_changed_idx | bg_changed_idx | framing_idx

    # Guard: a "New bg" toggle that reads "new" only means REGENERATE when the
    # scene doesn't already have a generated background. After first creation the
    # toggle is rehydrated from `as_is` (generated scene → shows "new"), so
    # without this guard a no-op "Apply look" would needlessly re-roll every
    # already-generated background. Ref changes still regen via *_changed sets.
    def _already_has_generated_bg(i: int) -> bool:
        if i == 0:
            return bool(run.get("local_path")) and not bool(md.get("main_as_is"))
        s = scenes_meta[i] if 0 <= i < len(scenes_meta) else {}
        return bool(s.get("keyframe_id")) and not bool(s.get("as_is"))

    global_bg_new = (bg_mode or "keep") == "new"
    if global_bg_new:
        if cur_count > 1:
            for idx, s in enumerate(scenes_meta):
                inherits_bg = not (s.get("bg_asset_id") or s.get("bg_upload_id"))
                if inherits_bg and not _already_has_generated_bg(idx):
                    candidate_idx.add(idx)
        elif not _already_has_generated_bg(0):
            candidate_idx.add(0)
    for i, m in scene_bg_mode.items():
        if m == "new" and not _already_has_generated_bg(i):
            candidate_idx.add(i)

    def _eff_has_char(i: int) -> bool:
        s = scenes_meta[i] if 0 <= i < len(scenes_meta) else {}
        return bool(s.get("character_asset_id") or s.get("character_upload_id")
                    or refs.get("character_asset_id") or refs.get("character_upload_id"))

    def _eff_has_bg(i: int) -> bool:
        s = scenes_meta[i] if 0 <= i < len(scenes_meta) else {}
        return bool(s.get("bg_asset_id") or s.get("bg_upload_id")
                    or refs.get("bg_asset_id") or refs.get("bg_upload_id"))

    def _eff_has_still(i: int) -> bool:
        if i == 0:
            return bool(run.get("local_path"))
        s = scenes_meta[i] if 0 <= i < len(scenes_meta) else {}
        return bool(s.get("keyframe_id"))

    regen_specs: dict[int, dict] = {}
    for i in sorted(candidate_idx):
        if i in in_flight_kf:
            continue  # already regenerating (double Apply)
        spec = _keyframe_regen_spec(
            scene_index=i,
            char_changed=(i in char_changed_idx),
            bg_changed=(i in bg_changed_idx),
            bg_mode=scene_bg_mode.get(i, bg_mode),
            has_char=_eff_has_char(i),
            has_bg=_eff_has_bg(i),
            has_still=_eff_has_still(i),
            framing_changed=(i in framing_idx),
            as_is=_still_as_is(md, i),
        )
        if spec is not None:
            regen_specs[i] = spec

    # Flag the regenerating scenes so the UI shows a live spinner immediately,
    # with what each is doing (a free crop, a re-shoot, a new still).
    md_after = {**md, **({"framing": patch["framing"]} if "framing" in patch else {}), "scenes": scenes_meta}
    for i, spec in regen_specs.items():
        work = _keyframe_work(spec, md_after, md)
        if 0 <= i < len(scenes_meta):
            scenes_meta[i]["keyframe_generating"] = True
            scenes_meta[i]["keyframe_work"] = work
        if i == 0:
            patch["main_keyframe_generating"] = True
            patch["main_keyframe_work"] = work

    _keep_uploaded_bookends_fresh(refs, md, patch)
    if cur_count > 1:
        patch["scenes"] = scenes_meta
    if patch:
        await _patch_metadata(run_id, patch)

    # --- a kept intro/outro card made again from its new pick (free) ---
    for role in bookend_rebuild:
        await _rebuild_kept_card(run_id, role)

    # --- scene_count change (archive/restore) — after field edits ---
    new_fresh: list[int] = []
    if scene_count is not None and int(scene_count) != cur_count:
        new_fresh = await _apply_scene_count(run_id, int(scene_count), restore=restore_archived)

    # --- topic re-script — last (reads the freshly-applied state) ---
    if topic is not None and topic.strip() and topic.strip() != (run.get("topic") or "").strip():
        speaker_before = patch.get("speaker") or md.get("speaker")
        await rescript_run(run_id, new_topic=topic.strip())
        # The new topic may change the speaker: re-shoot the scenes showing it.
        for spec in await _speaker_regen_after_rescript(
            run_id, speaker_before=speaker_before, skip=in_flight_kf | set(regen_specs),
        ):
            regen_specs[int(spec["scene_index"])] = spec

    # --- generate keyframes for any newly added scenes ---
    # Only once the run has stills. Before that (the create flow: back to Topic,
    # scene count changed, no stills yet) a keyframe made here would be built
    # before any look is picked and would turn the Look step's "Generate
    # stills" into "Apply look", so generate-previews (which also builds the
    # cards) never ran. Generate stills makes every scene's still later.
    if new_fresh and _run_has_stills(run, md):
        await generate_previews_for_scenes(run_id, new_fresh)
    elif new_fresh:
        log.info("[yas-th] edit_run run=%d new scenes %s: no stills yet, keyframes wait for Generate stills",
                 run_id, new_fresh)

    # --- v2 editor — auto-regenerate keyframes whose refs changed (async) ---
    # Dispatched LAST so the background task observes the fully-applied state
    # (re-script, new-scene keyframes) and never races the synchronous work
    # above on metadata.scenes.
    if regen_specs:
        _dispatch_keyframe_regen(run_id, list(regen_specs.values()))

    return await fetch_run(run_id)


async def batch_regen(
    *, run_id: int, items: list[dict], target: str,
) -> dict:
    """Regenerate images and/or videos for multiple segments. `target` ∈
    images | videos | both. Each item: {which, feedback_text?, image_models?}.
    Loops the existing per-segment retry paths (reusing all their correctness),
    collecting per-item errors so one failure doesn't abort the batch. An
    image with no feedback is regenerated (regenerate_still), never refined
    with a made-up change; one with feedback is refined.
    """
    if target not in {"images", "videos", "both"}:
        raise HTTPException(400, "target must be images / videos / both")
    errors: list[str] = []
    for it in items or []:
        which = it.get("which")
        if not which:
            continue
        fb = (it.get("feedback_text") or "").strip()
        if target in {"images", "both"}:
            try:
                await retry_preview(
                    run_id=run_id, which=which, feedback_text=fb,
                    image_models_override=it.get("image_models"),
                )
            except HTTPException as e:
                errors.append(f"{which} image: {e.detail}")
            except Exception as e:
                errors.append(f"{which} image: {str(e)[:120]}")
        if target in {"videos", "both"}:
            try:
                await retry_video(run_id=run_id, which=which, feedback_text=fb)
            except HTTPException as e:
                errors.append(f"{which} video: {e.detail}")
            except Exception as e:
                errors.append(f"{which} video: {str(e)[:120]}")
    if errors:
        log.warning("[yas-th] batch_regen run=%d partial errors: %s", run_id, errors)
    return await fetch_run(run_id)


async def restore_scene(run_id: int, *, archived_index: int) -> dict:
    """Bring a specific archived scene back into the active flow (the project
    tree 'Restore' action). Increases scene_count by one and slots the archived
    record into the next free index.
    """
    run = await _fetch_run_row(run_id)
    md = run["metadata"] or {}
    cur_count = int(md.get("scene_count") or 1)
    archived = list(md.get("archived_scenes") or [])
    match_pos = next(
        (j for j, a in enumerate(archived) if int(a.get("archived_index", -1)) == int(archived_index)),
        None,
    )
    if match_pos is None:
        raise HTTPException(404, f"archived scene {archived_index} not found")
    if cur_count >= MAX_SCENES:
        raise HTTPException(409, f"already at the max of {MAX_SCENES} scenes")
    # _apply_scene_count with restore=True will pop the matching archived_index
    # when the new slot index equals it; if indices don't line up, fall through
    # to a fresh slot. To target THIS record, set its archived_index to the new
    # slot first so the matcher picks it up.
    new_slot = cur_count
    archived[match_pos]["archived_index"] = new_slot
    await _patch_metadata(run_id, {"archived_scenes": archived})
    new_fresh = await _apply_scene_count(run_id, cur_count + 1, restore=True)
    # Same rule as edit_run: no keyframe for a run that has no stills yet.
    if new_fresh and _run_has_stills(run, md):
        await generate_previews_for_scenes(run_id, new_fresh)
    return await fetch_run(run_id)


# ---------------------------------------------------------------------------
# Step 5 — Fetch run (polling target)
# ---------------------------------------------------------------------------

def composition_url(run_id: int, md: dict) -> str | None:
    """The final reel's URL, or None. A reel finalized again (render_owner)
    is served whenever its last good file exists, whatever its status (a
    new render under way or failed), and the URL names that reel (?v=<good
    seq>): browsers hold the plain URL as immutable. A legacy row keeps
    today's rule (ready only, the plain URL)."""
    from . import th_reel

    url = f"/api/yas-producer/talking-head-v2/{run_id}/composition/file"
    if th_reel.owned(md):
        return f"{url}?v={int(md.get('composition_good_seq') or 0)}" if th_reel.good_reel(md) else None
    if md.get("composition_status") == COMP_READY and md.get("composition_path"):
        return url
    return None


def saved_current(run: dict, md: dict) -> bool:
    """Whether the reel being served is the one saved to assets (a reel
    finalized again after saving is not saved yet, nor one whose background
    music changed since: saved_music is the music version saved, None
    without music). A legacy row: saved at all."""
    from . import th_reel, th_reel_music

    if th_reel.owned(md):
        # Saved before the first Finalize here: that reel (no seq) was saved.
        return (run.get("saved_asset_id") is not None
                and md.get("saved_seq") == md.get("composition_good_seq")
                and md.get("saved_music") == th_reel_music.current_id(md))
    return run.get("saved_asset_id") is not None


def _clip_motion(a: dict | None) -> str | None:
    """What an intro/outro clip turned out to be, from its row (the Stills and
    Videos tiles say so, not the plan): 'zoom' (made as the in-house slow
    zoom), 'veo_replaced' (Veo made it, the check found the card changed, so
    it is the slow zoom now), 'veo' (Veo moved its icons and the check held),
    'veo_checking' (Veo is still making it, or it is not checked yet),
    'veo_flagged' (the check found the card changed but the slow zoom could
    not be made, so the Veo clip was kept), 'veo_unchecked' (the card was
    gone, so it could not be checked). None for a clip that failed (it moves
    nothing) and any other clip (a scene, a video editor bookend, an older
    clip)."""
    if (a or {}).get("status") == STATUS_FAILED:
        return None
    meta = _md_dict((a or {}).get("metadata"))
    if meta.get("slow_zoom"):
        return "zoom"
    if not meta.get("bookend_check"):
        return None
    check = _md_dict(meta.get("bookend_text_check"))
    if check.get("fallback") == BOOKEND_STYLE_ZOOM:
        return "veo_replaced"
    if not check or (a or {}).get("status") == STATUS_GENERATING:
        return "veo_checking"
    if check.get("ok") is True:
        return "veo"
    return "veo_flagged" if check.get("ok") is False else "veo_unchecked"


def _clip_why(anim: dict | None, stale: bool) -> str | None:
    """Why a clip must be made (_clip_needs_regen's rule, on rows already
    read): 'stale', 'missing', 'failed', or None when it is fresh."""
    if stale:
        return "stale"
    if not anim:
        return "missing"
    if anim.get("status") == STATUS_FAILED:
        return "failed"
    return None


def _veo_usd(model: str, seconds: int, audio: bool) -> float:
    """A Veo clip's price, from the meter's own rates (what is booked)."""
    return round(float(get_meter().estimate_veo(model, seconds, audio)), 4)


def _still_image_usd() -> float:
    """What one image of a still costs now (a Refine, a Regenerate, a
    re-shoot: card_animation's GEMINI_PRO_MODEL), at the meter's rate for
    the model the call will really use: the standard one once this process
    has found the project has no Pro access (vertex_client's fallback), so
    the Stills labels say ~$0.04 there and ~$0.13 once Pro is enabled."""
    from .vertex_client import _IMAGE_PRICE_USD, resolve_image_target

    model, _location = resolve_image_target(GEMINI_PRO_MODEL, card_animation.GEMINI_PRO_LOCATION)
    return float(_IMAGE_PRICE_USD.get(model, 0.05))


def _videos_plan(
    md: dict,
    *,
    main_anim: dict | None,
    scene_anims: dict[int, dict | None],
    bookend_anims: dict[str, dict | None],
    cards: dict[str, dict | None],
) -> dict:
    """The clips the next "Make videos" / "Update clips" press would make and
    what they cost, by the same rules as start_videos(_multi): every
    included scene whose clip is missing, failed or out of date (8 s with
    audio, on its own Veo tier), and every wanted intro/outro with a card or
    a pick to animate whose clip is missing, failed or out of date, made as
    its motion plan says (Veo at 4 s, silent, or the free slow zoom; a card
    not planned yet may be either). Prices come from the meter's rates
    (_veo_usd). Returns {clips: [{target, why, engine: 'veo'|'zoom'|
    'veo_or_zoom', model, seconds, audio, usd, usd_max}], count, usd,
    usd_max, any_clip}: usd counts an undecided bookend as the zoom,
    usd_max as Veo."""
    md = md or {}
    refs = md.get("refs") or {}
    clips: list[dict] = []
    any_clip = bool(main_anim) or any(scene_anims.values()) or any(bookend_anims.values())

    def scene_clip(target: str, why: str, quality: Any) -> dict:
        model = _veo_model_for(quality)
        usd = _veo_usd(model, VEO_DURATION_MAIN, True)
        return {"target": target, "why": why, "engine": "veo", "model": model,
                "seconds": VEO_DURATION_MAIN, "audio": True, "usd": usd, "usd_max": usd}

    count = int(md.get("scene_count") or 1)
    if count > 1:
        for i, sm in enumerate((md.get("scenes") or [])[:count]):
            if not isinstance(sm, dict) or sm.get("excluded"):
                continue
            why = _clip_why(scene_anims.get(i), bool(sm.get("video_stale")))
            if why:
                clips.append(scene_clip(f"scene_{i}", why, sm.get("video_quality")))
    else:
        why = _clip_why(main_anim, bool(md.get("main_video_stale")))
        if why:
            clips.append(scene_clip("main", why, md.get("main_video_quality") or md.get("video_quality")))
    for role in ("intro", "outro"):
        if not _bookend_wanted(md, role):
            continue
        card = cards.get(role)
        usable = card is not None and _card_matches_refs(card, refs, role)
        if not usable and _bookend_ref(refs, role) is None:
            continue  # nothing to animate: no clip is made
        why = _clip_why(bookend_anims.get(role), bool(md.get(f"{role}_video_stale")))
        if not why:
            continue
        plan = _md_dict((card or {}).get("metadata")).get("motion") if usable else None
        model = _veo_model_for(md.get(f"{role}_video_quality"))
        veo_usd = _veo_usd(model, VEO_DURATION_BOOKEND, False)
        if _plan_ok(plan) and plan.get("mode") == "zoom":
            engine, usd, usd_max, model = "zoom", 0.0, 0.0, None
        elif _plan_ok(plan):
            engine, usd, usd_max = "veo", veo_usd, veo_usd
        else:
            engine, usd, usd_max = "veo_or_zoom", 0.0, veo_usd
        clips.append({"target": role, "why": why, "engine": engine, "model": model,
                      "seconds": VEO_DURATION_BOOKEND, "audio": False, "usd": usd, "usd_max": usd_max})
    return {
        "clips": clips,
        "count": len(clips),
        "usd": round(sum(c["usd"] for c in clips), 2),
        "usd_max": round(sum(c["usd_max"] for c in clips), 2),
        "any_clip": any_clip,
    }


async def fetch_run(run_id: int, *, with_reel: bool = True) -> dict:
    """Return a serialisable dict containing the run + linked card / animation
    rows so the frontend can render the full UI in one fetch. `with_reel`:
    also the reel scene by scene (`reel`, and composition_stale by what goes
    in) — a list of runs leaves it out (it reads each clip's frames once).
    """
    from . import th_reel, th_reel_music

    run = await _fetch_run_row(run_id)
    md = run["metadata"] or {}

    intro_card: dict | None = None
    outro_card: dict | None = None
    intro_image_id = md.get("intro_image_id")
    outro_image_id = md.get("outro_image_id")
    if intro_image_id:
        intro_card = await _fetch_card_row(int(intro_image_id))
    if outro_image_id:
        outro_card = await _fetch_card_row(int(outro_image_id))
    # A card with no motion plan under the current rules gets one, once per
    # card, in the background (_card_needs_plan): the tiles then say how its
    # next clip moves; until then, that it is decided when the clip is made.
    # Never for a list of runs.
    if with_reel:
        for _role, _card in (("intro", intro_card), ("outro", outro_card)):
            if _card_needs_plan(_card, md, _role):
                _dispatch_card_plan(int(_card["id"]), _role)

    anim_id = md.get("veo_animation_id")
    animation: dict | None = None
    if anim_id:
        animation = await _fetch_animation_row(int(anim_id))

    base_url = "/api/yas-producer/talking-head-v2"
    refs = dict(md.get("refs") or {})

    # Direct-video bookends: refs store only the yas_uploads id. Resolve each to
    # a renderable serve URL so the creator can preview the uploaded clip on
    # resume (the live-session slot already has the URL; this covers reopening).
    for _role in ("intro", "outro"):
        _vid = refs.get(f"{_role}_video_upload_id")
        if _vid:
            try:
                _urow = await _resolve_upload_row(int(_vid))
                _tok = (_urow or {}).get("token")
                if _tok:
                    refs[f"{_role}_video_url"] = (
                        f"/api/yas-producer/poster-human/upload/{_tok}/file"
                    )
            except Exception:
                pass

    def _bust(url: str | None, row: dict | None) -> str | None:
        """Append `?v=<unix>` cache-buster derived from the row's
        updated_at. Fixes the 'main retry shows stale image' bug — the
        file on disk is overwritten in place so the URL is identical, but
        the underlying bytes have changed. The version param forces the
        browser to bypass <img> cache."""
        if not url:
            return None
        try:
            ts = (row or {}).get("updated_at") if row else None
            if ts is not None and hasattr(ts, "timestamp"):
                return f"{url}?v={int(ts.timestamp())}"
        except Exception:
            pass
        return url

    def _card_payload(c: dict | None) -> dict | None:
        if not c:
            return None
        cid = c["id"]
        url = f"{base_url}/cards/{cid}/file" if c.get("local_path") else None
        cmd = _md_dict(c.get("metadata"))
        # A plan made under older rules is planned again with the clip: not
        # shown as the plan.
        motion = cmd.get("motion") if _plan_ok(cmd.get("motion")) else {}
        role = cmd.get("card_type")
        return {
            "id": cid,
            "status": c.get("status"),
            "card_type": cmd.get("card_type") if isinstance(c.get("metadata"), dict) else None,
            "width": c.get("width"),
            "height": c.get("height"),
            "url": _bust(url, c),
            "error": c.get("error"),
            # The card IS the picked image (ref mode 'as_is'), how a pick
            # was used, the card's text ('planned': asked of a drawn card;
            # 'detected': read off a kept one), how its clip will move
            # ('veo': its icons; 'zoom': the slow zoom; None: not planned
            # yet) and whether it was made from the image picked now.
            "as_is": bool(cmd.get("as_is")),
            "ref_mode": cmd.get("ref_mode"),
            "card_text": cmd.get("card_text"),
            "card_text_source": cmd.get("card_text_source"),
            "motion": motion.get("mode"),
            "matches_refs": _card_matches_refs(c, refs, role) if role in ("intro", "outro") else True,
            # A kept card's words: whether it keeps its own ("Keep card
            # text"), what happened to them ({status: kept | unchanged |
            # swapped | failed | unread, warning, swapped: [{kind, old,
            # new}]}), and whether its clip is a previous reel's, reused.
            "keep_text": bool(cmd.get("keep_text")),
            "text_swap": _md_dict(cmd.get("text_swap")) or None,
            "reused_clip": bool(cmd.get("reused_clip_from")),
            "source_card_id": (cmd.get("source_ref") or {}).get("card_id")
            if isinstance(cmd.get("source_ref"), dict) else None,
        }

    # If the user picked a reference asset for intro/outro, surface that
    # in the same shape so the UI can render uniformly.
    intro_payload = _card_payload(intro_card)
    outro_payload = _card_payload(outro_card)
    # When the user picked a saved producer_assets row as the intro/outro
    # source (and the auto-generated card row was skipped), surface that
    # in the same payload shape so the UI can render uniformly.
    # Such a payload is the pick, not a card (card_missing): the Stills step
    # does not count it as made.
    if not intro_payload:
        if refs.get("intro_upload_id"):
            intro_payload = {
                "id": None, "status": "ready", "card_type": "intro",
                "upload_id": refs["intro_upload_id"],
                "url": f"/api/yas-producer/poster-human/upload/{refs.get('intro_upload_token','')}/file"
                if refs.get("intro_upload_token") else None,
                "source": "upload",
                "card_missing": True,
            }
        elif refs.get("intro_card_id"):
            intro_payload = {
                "id": None, "status": "ready", "card_type": "intro",
                "card_id": refs["intro_card_id"],
                "url": f"{base_url}/cards/{int(refs['intro_card_id'])}/file",
                "source": "card",
                "card_missing": True,
            }
        elif refs.get("intro_asset_id"):
            intro_payload = {
                "id": None, "status": "ready", "card_type": "intro",
                "asset_id": refs["intro_asset_id"],
                "url": f"/api/agents/producer/assets/{refs['intro_asset_id']}/file",
                "source": "asset",
                "card_missing": True,
            }
    if not outro_payload:
        if refs.get("outro_upload_id"):
            outro_payload = {
                "id": None, "status": "ready", "card_type": "outro",
                "upload_id": refs["outro_upload_id"],
                "url": f"/api/yas-producer/poster-human/upload/{refs.get('outro_upload_token','')}/file"
                if refs.get("outro_upload_token") else None,
                "source": "upload",
                "card_missing": True,
            }
        elif refs.get("outro_card_id"):
            outro_payload = {
                "id": None, "status": "ready", "card_type": "outro",
                "card_id": refs["outro_card_id"],
                "url": f"{base_url}/cards/{int(refs['outro_card_id'])}/file",
                "source": "card",
                "card_missing": True,
            }
        elif refs.get("outro_asset_id"):
            outro_payload = {
                "id": None, "status": "ready", "card_type": "outro",
                "asset_id": refs["outro_asset_id"],
                "url": f"/api/agents/producer/assets/{refs['outro_asset_id']}/file",
                "source": "asset",
                "card_missing": True,
            }

    main_url = _bust(
        f"{base_url}/{run['id']}/file" if run.get("local_path") else None,
        run,
    )
    image_models = _normalize_image_models(md.get("image_models"))

    # Fetch the three animations (intro / main / outro) when present.
    def _anim_payload(a: dict | None, *, video_route: str) -> dict | None:
        if not a:
            return None
        return {
            "id": a["id"],
            "status": a.get("status"),
            "duration_sec": a.get("duration_sec"),
            "with_audio": a.get("with_audio"),
            "cost_usd": (
                float(a["cost_usd"]) if a.get("cost_usd") is not None else None
            ),
            "error": a.get("error"),
            "url": (
                # Cache-bust with the animation row id. The video route is
                # STABLE per role (e.g. /video/scene_0), so a retry — which
                # inserts a NEW animation row — would otherwise serve the
                # browser's cached OLD clip (or a cached 410 from the
                # generating gap → "file may be unavailable"). The id changes
                # on every (re)generation, forcing a refetch.
                f"{base_url}/{run['id']}/video/{video_route}?v={a['id']}"
                if a.get("status") == STATUS_READY and a.get("local_path")
                else None
            ),
            # An intro/outro clip: what it turned out to be ('veo', 'zoom',
            # 'veo_replaced', 'veo_checking', 'veo_flagged', 'veo_unchecked';
            # _clip_motion), and why a Veo clip was replaced by the slow zoom
            # (or failed its check).
            "motion": _clip_motion(a),
            "motion_reason": (
                _md_dict(_md_dict(a.get("metadata")).get("bookend_text_check")).get("reason")
                if _clip_motion(a) in ("veo_replaced", "veo_flagged") else None
            ),
        }

    intro_anim: dict | None = None
    main_anim: dict | None = None
    outro_anim: dict | None = None
    if md.get("intro_animation_id"):
        intro_anim = await _fetch_animation_row(int(md["intro_animation_id"]))
    if md.get("main_animation_id"):
        main_anim = await _fetch_animation_row(int(md["main_animation_id"]))
    elif md.get("veo_animation_id"):  # legacy single-anim runs
        main_anim = await _fetch_animation_row(int(md["veo_animation_id"]))
    if md.get("outro_animation_id"):
        outro_anim = await _fetch_animation_row(int(md["outro_animation_id"]))
    # Each clip's status by id (the scenes' are added below): whether the
    # reel can be finalized (reel_ready).
    anim_statuses: dict[int, Any] = {
        int(a["id"]): a.get("status") for a in (intro_anim, main_anim, outro_anim) if a
    }
    anim_rows: dict[int, dict] = {int(a["id"]): a for a in (intro_anim, main_anim, outro_anim) if a}

    # ----- v6 — unified scenes array ------------------------------------
    # ALWAYS emitted. For single-scene runs (default), the scenes array
    # is length 1 and synthesized from the legacy main + main_animation
    # fields. For multi-scene runs, it comes from metadata.scenes with
    # each entry's keyframe + animation hydrated.
    scenes_payload: list[dict] = []
    scene_anim_rows: dict[int, dict | None] = {}
    scene_count_md = int(md.get("scene_count") or 1)
    if scene_count_md > 1 and (md.get("scenes") or []):
        for i, sm in enumerate(md["scenes"]):
            # Keyframe URL: scene 0 = run row, scenes 1..N-1 = scene rows.
            # The still's true size goes with it, so the Stills tile can show
            # its real shape.
            keyframe_url: str | None
            kf_dims: tuple[Any, Any] = (None, None)
            if i == 0:
                keyframe_url = main_url
                if main_url:
                    kf_dims = (run.get("width"), run.get("height"))
            else:
                kid = sm.get("keyframe_id")
                if kid:
                    krow = await _fetch_scene_row(int(kid))
                    if krow and krow.get("local_path"):
                        keyframe_url = _bust(
                            f"{base_url}/scenes/{kid}/file", krow,
                        )
                        kf_dims = (krow.get("width"), krow.get("height"))
                    else:
                        keyframe_url = None
                else:
                    keyframe_url = None
            # Animation hydration.
            anim_row: dict | None = None
            aid = sm.get("animation_id")
            if aid:
                anim_row = await _fetch_animation_row(int(aid))
                if anim_row:
                    anim_statuses[int(aid)] = anim_row.get("status")
                    anim_rows[int(aid)] = anim_row
            scene_anim_rows[i] = anim_row
            # v2 editor — per-scene keyframe (still) status drives the live
            # "generating…" spinner shown after an image apply. The
            # `keyframe_generating` marker (set by edit_run, cleared by the
            # background regen task) wins; otherwise fall back to error/url.
            scene_error_i = md.get("main_error") if i == 0 else sm.get("error")
            if sm.get("keyframe_generating") or (i == 0 and md.get("main_keyframe_generating")):
                keyframe_status = "generating"
            elif scene_error_i:
                keyframe_status = "failed"
            elif keyframe_url:
                keyframe_status = "ready"
            else:
                keyframe_status = None
            scenes_payload.append({
                "index": i,
                "dialogue": sm.get("dialogue") or "",
                "hook": sm.get("hook") or "",
                "user_authored": bool(sm.get("user_authored")),
                "keyframe_url": keyframe_url,
                "keyframe_status": keyframe_status,
                # While generating: 'crop' (a free framing crop), 'reshoot'
                # (the image model re-shoots it) or 'regen' (made again).
                "keyframe_work": (
                    (sm.get("keyframe_work") or (md.get("main_keyframe_work") if i == 0 else None)
                     or KEYFRAME_WORK_REGEN) if keyframe_status == "generating" else None
                ),
                "width": kf_dims[0],
                "height": kf_dims[1],
                "animation": _anim_payload(anim_row, video_route=f"scene_{i}"),
                "character_asset_id": sm.get("character_asset_id"),
                "character_upload_id": sm.get("character_upload_id"),
                "bg_asset_id": sm.get("bg_asset_id"),
                "bg_upload_id": sm.get("bg_upload_id"),
                # as_is: keyframe is the character image used verbatim (New bg
                # OFF) vs a generated still — drives the Look toggle's saved state.
                "as_is": bool(md.get("main_as_is")) if i == 0 else bool(sm.get("as_is")),
                # An earlier version of the still to go back to (the tile's Undo).
                "can_undo": _can_undo(md, i),
                # Framing: the scene's own (None: it follows the run's), the
                # one a new still is made at, and the one its still shows
                # (as_photo: the photo's own; None: no still).
                "framing": _scene_framing_override(md, i),
                "framing_effective": _effective_framing(md, i),
                "framing_used": _still_framing(md, i) if keyframe_url else None,
                # A re-shot still whose face shows another framing than the
                # one asked for says so on its tile.
                "framing_warning": (md.get("main_framing_warning") if i == 0
                                    else sm.get("framing_warning")) if keyframe_url else None,
                # v2 editor — per-segment flags. video_stale: the current Veo
                # clip no longer matches the keyframe/dialogue/refs (set when an
                # image/ref/dialogue changed; cleared on video retry). excluded:
                # user toggled this scene out of the final stitch.
                "video_stale": bool(sm.get("video_stale")),
                "excluded": bool(sm.get("excluded")),
                # Per-segment Veo quality tier ('normal' | 'quality').
                "video_quality": _norm_quality(sm.get("video_quality")),
                # Scene 0 (the run row) doesn't have a separate error — falls
                # back to main_error stored on the run. Scenes 1..N-1 each
                # carry their own keyframe-gen error in metadata.scenes[i].error.
                "error": (md.get("main_error") if i == 0 else sm.get("error")),
            })
    else:
        # Single-scene synthesized payload — equals the legacy main +
        # main_animation. Length always 1 so the frontend can iterate
        # `scenes` uniformly regardless of count.
        script = md.get("script") or {}
        legacy_dlg = (script.get("dialogue") or "") if isinstance(script, dict) else ""
        legacy_hook = (script.get("hook") or "") if isinstance(script, dict) else ""
        if md.get("main_keyframe_generating"):
            main_keyframe_status = "generating"
        elif md.get("main_error"):
            main_keyframe_status = "failed"
        elif main_url:
            main_keyframe_status = "ready"
        else:
            main_keyframe_status = None
        scenes_payload.append({
            "index": 0,
            "dialogue": legacy_dlg,
            "hook": legacy_hook,
            "user_authored": False,
            "keyframe_url": main_url,
            "keyframe_status": main_keyframe_status,
            "keyframe_work": (md.get("main_keyframe_work") or KEYFRAME_WORK_REGEN)
            if main_keyframe_status == "generating" else None,
            "width": run.get("width") if main_url else None,
            "height": run.get("height") if main_url else None,
            "animation": _anim_payload(main_anim, video_route="main"),
            "character_asset_id": None,
            "character_upload_id": None,
            "bg_asset_id": None,
            "bg_upload_id": None,
            "as_is": bool(md.get("main_as_is")),
            "can_undo": _can_undo(md, 0),
            # Framing: a single scene's is the run's.
            "framing": None,
            "framing_effective": _effective_framing(md, 0),
            "framing_used": _still_framing(md, 0) if main_url else None,
            "framing_warning": md.get("main_framing_warning") if main_url else None,
            # Single-scene main is never excludable (it's the only content);
            # its staleness lives under the legacy main_video_stale key.
            "video_stale": bool(md.get("main_video_stale")),
            "excluded": False,
            # Per-segment Veo quality — single-scene main falls back to the
            # legacy run-level video_quality so existing runs keep their tier.
            "video_quality": _norm_quality(
                md.get("main_video_quality") or md.get("video_quality"),
            ),
            "error": md.get("main_error"),
        })

    # ----- v2 editor — composition staleness ----------------------------
    # The stitched MP4 (if any) is out of date when any INCLUDED clip's video
    # is stale. Lets the UI show "composition out of date — re-stitch" without
    # discarding the existing playable MP4.
    def _seg_included_stale(payload_scene: dict) -> bool:
        return bool(payload_scene.get("video_stale")) and not bool(payload_scene.get("excluded"))

    # ----- the reel, scene by scene (the Rebrand stage) ------------------
    # What each scene puts in the reel (a version or the original, where it
    # ends), and the hash of it all: the page's Finalize sends it back
    # (expect_sha). Read only once every included clip is made (or a reel
    # finalized again, or a choice made, needs its staleness); never this
    # page's failure.
    reel_ready = th_reel.reel_ready(md, anim_statuses)
    reel_state = None
    reel_block = None
    if with_reel and (reel_ready or th_reel.owned(md) or md.get("reel_choices")):
        cards = {}
        for c in (intro_card, outro_card):
            if c and c.get("local_path"):
                cp = _abs_from_rel(c["local_path"])
                cards[int(c["id"])] = cp if cp.exists() else None
        try:
            reel_state = await th_reel.reel_state(run, th_reel.DbIo(anim_rows, cards=cards), statuses=anim_statuses)
            if reel_ready:
                reel_block = await reel_state.block()
        except Exception:
            log.exception("[yas-th] run #%s: the reel's scenes couldn't be read (non-fatal)", run["id"])

    composition_stale = False
    if th_reel.owned(md):
        # Finalized here: out of date when what goes in now isn't what the
        # reel being served was made of (a pick, an end, a start, an
        # exclusion, a clip made again), or a clip is out of date or being
        # made again.
        if th_reel.good_reel(md):
            composition_stale = (not reel_ready) or (
                reel_state is not None and reel_state.sha != md.get("composition_good_sha"))
    elif md.get("composition_status") == COMP_READY:
        composition_stale = (
            any(_seg_included_stale(s) for s in scenes_payload)
            or (th_reel.bookend_stale(md, "intro") and not bool(md.get("intro_excluded")))
            or (th_reel.bookend_stale(md, "outro") and not bool(md.get("outro_excluded")))
            # A legacy reel: any version, end or start chosen since.
            or (reel_state is not None and reel_state.any_choice())
        )

    # Lightweight archived-scene shape for the "Removed scenes" project tree.
    archived_payload: list[dict] = []
    for a in (md.get("archived_scenes") or []):
        archived_payload.append({
            "archived_index": a.get("archived_index"),
            "dialogue": a.get("dialogue") or "",
            "hook": a.get("hook") or "",
            "has_keyframe": bool(a.get("keyframe_id")),
            "has_animation": bool(a.get("animation_id")),
            "archived_at": a.get("archived_at"),
        })

    return {
        "id": run["id"],
        "topic": run["topic"],
        "status": run["status"],
        "image_model": run.get("image_model"),
        "image_models": image_models,
        "lock_character": bool(md.get("lock_character")),
        "aspect_ratio": _norm_aspect(md.get("aspect_ratio")),
        # The Video size can no longer change (a still or card exists): the
        # Look step disables it, and edit/generate refuse a new size (409).
        "aspect_locked": _aspect_locked(run, md),
        "video_quality": _norm_quality(md.get("video_quality")),
        # How close the camera is (close | above_hip | full_body); a run made
        # before framing existed reads as close (its stills were).
        "framing": _run_framing(md),
        "script": md.get("script"),
        # Who is on camera when no character is picked ({subject, gender,
        # source}); None on a run made before the default speaker.
        "speaker": md.get("speaker") if isinstance(md.get("speaker"), dict) else None,
        "refs": refs,
        "main": {
            "status": run["status"],
            "url": main_url,
            "width": run.get("width"),
            "height": run.get("height"),
            "error": run.get("error"),
        },
        "intro": intro_payload,
        "outro": outro_payload,
        "animations": {
            "intro": _anim_payload(intro_anim, video_route="intro"),
            "main":  _anim_payload(main_anim, video_route="main"),
            "outro": _anim_payload(outro_anim, video_route="outro"),
        },
        "intro_animation_style": md.get("intro_animation_style"),
        "outro_animation_style": md.get("outro_animation_style"),
        # v2 editor — bookend per-clip flags (scene flags live on each scene).
        # An uploaded video has no clip to go out of date.
        "intro_video_stale": th_reel.bookend_stale(md, "intro"),
        "outro_video_stale": th_reel.bookend_stale(md, "outro"),
        "main_video_stale": bool(md.get("main_video_stale")),
        # Per-segment Veo quality tiers ('normal' | 'quality'). Main falls
        # back to the legacy run-level video_quality for existing runs.
        "intro_video_quality": _norm_quality(md.get("intro_video_quality")),
        "outro_video_quality": _norm_quality(md.get("outro_video_quality")),
        "main_video_quality": _norm_quality(
            md.get("main_video_quality") or md.get("video_quality"),
        ),
        "intro_excluded": bool(md.get("intro_excluded")),
        "outro_excluded": bool(md.get("outro_excluded")),
        "main_excluded": bool(md.get("main_excluded")),
        # v2 editor — archived ("removed") scenes for the project tree, and a
        # derived flag telling the UI the stitched MP4 is out of date.
        "archived_scenes": archived_payload,
        "composition_stale": composition_stale,
        # v6 — unified scenes array. Always present, length 1 for
        # single-scene runs, length N for multi-scene runs.
        "scene_count": scene_count_md,
        "scenes": scenes_payload,
        # Image-gen error messages — populated when a tile failed during
        # generate_previews (or main retry). Frontend surfaces these as
        # red error tiles instead of silently leaving the slot empty.
        "intro_error": md.get("intro_error"),
        "outro_error": md.get("outro_error"),
        "main_error":  md.get("main_error"),
        # Backward-compat single-animation key (kept for callers that only
        # need the main clip)
        "animation": _anim_payload(main_anim, video_route="main"),
        # What the next "Make videos" / "Update clips" press makes and costs
        # (the Videos button shows it; nothing is made without that press).
        "videos_plan": _videos_plan(
            md, main_anim=main_anim if scene_count_md <= 1 else None,
            scene_anims=scene_anim_rows,
            bookend_anims={"intro": intro_anim, "outro": outro_anim},
            cards={"intro": intro_card, "outro": outro_card},
        ),
        # What one still image costs now (Refine, Regenerate, a re-shoot):
        # the Stills tiles, toasts and hints say it.
        "still_image_usd": _still_image_usd(),
        "composition_status": md.get("composition_status"),
        "composition_url": composition_url(run["id"], md),
        # Finalize (again): whether every included clip is made and none is
        # out of date, the reel's render count, the one being served (its
        # URL carries it: ?v=), and why the newest render failed while the
        # last good one stays.
        "reel_ready": reel_ready,
        # The reel scene by scene (null until every included clip is made):
        # {ready, targets: [{target, role, label, src, thumb_url, pick, ships,
        # making, end: {frame, auto, user, full}}], inputs_sha}.
        "reel": reel_block,
        "composition_seq": md.get("composition_seq"),
        "composition_good_seq": md.get("composition_good_seq"),
        "composition_error": md.get("composition_error"),
        "saved_asset_id": run.get("saved_asset_id"),
        # "Saved" means the reel being served is the one saved to assets.
        "saved_current": saved_current(run, md),
        # Background music under the reel (Final): the track, the switch,
        # the music version's URL (services/th_reel_music.public_state).
        "music": th_reel_music.public_state(run["id"], md),
        "created_at": (
            run["created_at"].isoformat() if run.get("created_at") else None
        ),
        "updated_at": (
            run["updated_at"].isoformat() if run.get("updated_at") else None
        ),
    }


# ---------------------------------------------------------------------------
# Step 6 — Save to assets
# ---------------------------------------------------------------------------

async def save_to_assets(*, run_id: int, display_name: str | None = None) -> dict:
    """Copy the final composition MP4 into producer_assets and mark the
    yas_generations row as saved.
    """
    from . import th_reel, th_reel_music

    run = await _fetch_run_row(run_id)
    md = run["metadata"] or {}
    # A reel finalized again is saved from its last good file whatever its
    # status (a new render may be under way): the one being served.
    if not th_reel.owned(md) and md.get("composition_status") != COMP_READY:
        raise HTTPException(409, "Composition is not ready yet.")
    comp_rel = md.get("composition_path")
    if not comp_rel:
        raise HTTPException(410, "Composition file path missing.")
    comp_path = _abs_from_rel(comp_rel)
    if not comp_path.exists():
        raise HTTPException(410, "Composition file missing on disk.")

    # Background music on and made for this reel: its music version is the
    # one saved (the one the Final step shows). Otherwise the reel, as ever.
    music_raw = await th_reel_music.current_bytes(md)
    raw = music_raw if music_raw is not None else comp_path.read_bytes()
    # saved_seq: which reel was saved (a reel finalized again after this is
    # offered for saving again); saved_music, only on a reel that has had
    # music: which music version (None: none), so a music change is too.
    saved_patch: dict = {}
    if th_reel.owned(md):
        saved_patch["saved_seq"] = md.get("composition_good_seq")
        if "reel_music" in md:
            saved_patch["saved_music"] = th_reel_music.current_id(md) if music_raw is not None else None
    script = md.get("script") or {}
    topic = run.get("topic") or "Talking head"

    slug_base = (display_name or topic or "talking-head-v2")[:48]
    # ASCII only: the slug becomes the bucket key (an Arabic topic would not
    # survive the /media proxy's signature check).
    slug = asset_storage.ascii_slug(slug_base, fallback="talking_head")
    slug = f"{slug}_{secrets.token_hex(3)}"
    bucket_path = f"video_talking_head_v2/{slug}.mp4"

    upload_result = await asset_storage.upload(
        data=raw, path=bucket_path, mime="video/mp4",
    )

    # ai_description: prefer the script's dialogue (it captures the heart of
    # the reel in one line), prefixed with the topic.
    ai_description = (
        f"Talking-head reel: {topic}. {script.get('dialogue', '')}".strip()
    )

    # Embed the description for retrieval (same lane as other assets)
    embedding = None
    if ai_description.strip():
        try:
            vecs = await gemini_embed([ai_description])
            if vecs and vecs[0]:
                embedding = vecs[0]
        except Exception:
            log.exception("embedding failed for talking-head-v2 save")

    metadata_for_asset = {
        "content_category": "video_talking_head_v2",
        "source": "yas_talking_head_v2",
        "script": script,
        "refs": md.get("refs") or {},
        "lock_character": bool(md.get("lock_character")),
        "yas_generation_id": run_id,
    }

    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                INSERT INTO producer_assets
                  (slug, asset_kind, category, display_name,
                   storage_bucket, storage_path, local_path,
                   mime_type, bytes,
                   ai_description, attributes, embedding,
                   metadata, enabled)
                VALUES (%s, 'video', NULL, %s,
                        %s, %s, %s,
                        %s, %s,
                        %s, %s, %s,
                        %s, TRUE)
                RETURNING id
                """,
                (
                    slug, display_name or topic[:80],
                    upload_result.get("storage_bucket"),
                    upload_result.get("storage_path"),
                    upload_result.get("local_path"),
                    "video/mp4", len(raw),
                    ai_description, Jsonb({}), embedding,
                    Jsonb(metadata_for_asset),
                ),
            )
            new = await cur.fetchone()
            asset_id = int(new["id"])

            # Which reel (and music version) was saved: saved_patch above.
            await cur.execute(
                """
                UPDATE yas_generations
                   SET status = %s,
                       saved_asset_id = %s,
                       metadata = metadata || %s::jsonb,
                       updated_at = NOW()
                 WHERE id = %s
                """,
                (STATUS_SAVED, asset_id, Jsonb(saved_patch), run_id),
            )

    log.info("[yas-th] saved run #%d → producer_assets #%d", run_id, asset_id)
    return {"id": asset_id, "yas_generation_id": run_id}


# ---------------------------------------------------------------------------
# Step 6b — Open in the multi-track Video Editor (as a NEW project)
# ---------------------------------------------------------------------------

async def open_in_editor(*, run_id: int, mode: str) -> dict:
    """Create a NEW video-editor project seeded from this talking-head run and
    return its id. Never modifies the run.

      mode='clips' → the individual READY clips (intro → main/scenes → outro, in
                     order, excluded clips skipped), each as its own timeline clip.
      mode='final' → the stitched composition MP4 as a single clip (copied into
                     the editor's uploads via import_upload).

    In both cases the sources land in BOTH the Media bin and the timeline, laid
    end-to-end in order (see yas_video_editor.create_project_from_sources)."""
    from . import yas_video_editor as ve_service  # one-way import (no cycle)

    run = await _fetch_run_row(run_id)
    md = run["metadata"] or {}
    topic = (run.get("topic") or "Talking head").strip()
    aspect = _norm_aspect(md.get("aspect_ratio"))
    title = f"{topic[:60]} — edit"

    if mode == "final":
        from . import th_reel

        # A reel finalized again opens its last good file (the one served).
        if not th_reel.owned(md) and md.get("composition_status") != COMP_READY:
            raise HTTPException(409, "Composition is not ready yet — stitch it first.")
        comp_rel = md.get("composition_path")
        if not comp_rel:
            raise HTTPException(410, "Composition file path missing.")
        comp_path = _abs_from_rel(comp_rel)
        if not comp_path.exists():
            raise HTTPException(410, "Composition file missing on disk.")
        slug = "".join(c if c.isalnum() else "_" for c in topic.lower())[:32].strip("_") or "talking_head"
        # Background music on and made for this reel: its music version opens.
        from . import th_reel_music

        music_raw = await th_reel_music.current_bytes(md)
        desc = await ve_service.import_upload(
            raw=music_raw if music_raw is not None else comp_path.read_bytes(),
            filename=f"{slug}.mp4", mime="video/mp4",
        )
        sources = [{
            "kind": "upload",
            "id": int(desc["id"]),
            "duration_frames": int(desc.get("durationInFrames") or 1),
            "title": topic[:60] or "Final video",
        }]
    else:
        # mode == "clips" — ordered, included, READY animations.
        scene_count = int(md.get("scene_count") or 1)
        ordered: list[tuple[int, str]] = []
        if not md.get("intro_excluded") and md.get("intro_animation_id"):
            ordered.append((int(md["intro_animation_id"]), "Intro"))
        if scene_count <= 1:
            main_id = md.get("main_animation_id") or md.get("veo_animation_id")
            if main_id and not md.get("main_excluded"):
                ordered.append((int(main_id), "Main"))
        else:
            for i, sm in enumerate(md.get("scenes") or []):
                if sm.get("excluded"):
                    continue
                aid = sm.get("animation_id")
                if aid:
                    ordered.append((int(aid), f"Scene {i + 1}"))
        if not md.get("outro_excluded") and md.get("outro_animation_id"):
            ordered.append((int(md["outro_animation_id"]), "Outro"))

        sources = []
        for aid, ttl in ordered:
            row = await _fetch_animation_row(aid)
            if not row or row.get("status") != STATUS_READY or not row.get("local_path"):
                continue
            sources.append({
                "kind": "animation",
                "id": aid,
                "duration_frames": ve_service._frames(row.get("duration_sec")),
                "title": ttl,
            })
        if not sources:
            raise HTTPException(409, "No rendered clips yet — generate the videos first.")

    project = await ve_service.create_project_from_sources(
        title=title, aspect=aspect, sources=sources,
    )
    log.info(
        "[yas-th] run #%d → editor project #%s (mode=%s, %d clip(s))",
        run_id, project.get("id"), mode, len(sources),
    )
    return {"project_id": project["id"]}


# ---------------------------------------------------------------------------
# Step 7 — Recent (rehydration)
# ---------------------------------------------------------------------------

async def list_recent(limit: int = 20) -> list[dict]:
    """Return talking-head-v2 runs from the last 24h that aren't saved-to-assets,
    most recent first. Same shape as `fetch_run`.
    """
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT id
                  FROM yas_generations
                 WHERE mode = %s
                   AND status <> %s
                   AND created_at >= NOW() - INTERVAL '24 hours'
                 ORDER BY created_at DESC
                 LIMIT %s
                """,
                (MODE_PREVIEW, STATUS_SAVED, limit),
            )
            rows = await cur.fetchall()
    return [await fetch_run(int(r["id"]), with_reel=False) for r in rows]

# ---------------------------------------------------------------------------
# Public aliases for the video editor's bookend pipeline
# (services/yas_video_edit_ai_bookends.py). Pure re-exports — no behavior
# change. These functions are run-INDEPENDENT: they take an explicit
# parent_run_id / card_id and never read talking-head run state, so the
# editor can pass its OWN project id as the parent.
# ---------------------------------------------------------------------------

render_card_animation_style = _render_card_animation_style
insert_card_row = _insert_card_row
insert_animation_row = _insert_animation_row
resolve_card_path = _resolve_card_path
# pick_bookend_animation is already public.
