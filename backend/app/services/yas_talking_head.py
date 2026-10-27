"""Yas Producer — Talking Head category orchestration.

This module owns the end-to-end logic for the third Yas Producer flow:

  1. `generate_script()`     — OpenAI GPT-4o with full RAG context (brand +
                                past talking_head scripts + IG inspiration +
                                character library) → structured script row in
                                `yas_generations` (mode='talking_head_preview').
  2. `generate_previews()`   — Three image-gen calls in parallel: main
                                keyframe (the still Veo will animate), intro
                                card (skipped if user picked a reference),
                                outro card (skipped if user picked a reference).
  3. `retry_preview()`       — Re-runs ONE of the three image-gen calls with
                                user feedback text appended to the prompt.
  4. `finalize()`            — Kicks Veo on the main keyframe. The
                                yas_th_composition_poller picks up when Veo
                                finishes and triggers the Remotion stitch.
  5. `fetch_run()`           — Used by the frontend's 5s polling loop while
                                a finalize is in flight.
  6. `save_to_assets()`      — Copies the final composition MP4 to
                                producer_assets (asset_kind='video',
                                metadata.content_category='video_talking_head').

The router (`routers/yas_producer.py`) wraps these in HTTP endpoints under
`/api/yas-producer/talking-head/*`. The router does input validation; this
module does the work.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
from io import BytesIO
from pathlib import Path
from typing import Any, Optional

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
    VeoQuotaExhaustedError,
    VertexBudgetExceededError,
    gemini_embed,
    gemini_image,
    gemini_multimodal,
    veo_generate,
)
from ..agents.producer.memory import (
    _latest_brand_payload,
    build_brand_requirements,
    build_context_block,
)
from ..agents.producer.prompts import format_audio_clause  # used inside build_main_veo_prompt
from . import yas_talking_head_prompts as th_prompts


log = logging.getLogger("archivist.yas_talking_head")


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Same root the existing yas_producer flow uses (config.asset_root.parent /
# yas_producer). Talking-head artefacts live under a `talking-head/` subdir.
_YAS_ROOT = (config.asset_root.parent / "yas_producer").resolve()
TH_ROOT = _YAS_ROOT / "talking-head"

# Mode strings stored in yas_generations.mode for talking-head rows.
MODE_PREVIEW = "talking_head_preview"
MODE_INTRO = "talking_head_intro"
MODE_OUTRO = "talking_head_outro"
# v6 — sibling row for each main scene's keyframe when scene_count > 1.
# Stored alongside MODE_INTRO / MODE_OUTRO rows in yas_generations. The
# run row (MODE_PREVIEW) still holds scenes[0]'s keyframe for backwards
# compat; scenes[1..N-1] are sibling rows with this mode.
MODE_SCENE = "talking_head_scene"

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
GEMINI_PRO_MODEL = config.vertex_image_pro_model
GEMINI_PRO_LOCATION = config.vertex_image_pro_location

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

# Veo settings
VEO_DURATION_SEC = 8
VEO_ASPECT_RATIO = "9:16"

# Max number of Veo submissions in flight at once. Veo 3.1 Fast's
# per-base-model quota (long_running_online_prediction_requests_per_base_model)
# trips with a 429 RESOURCE_EXHAUSTED when a whole reel (N scenes + intro +
# outro = up to 7 jobs) is fired in a single burst. Bounding concurrent
# submissions — combined with the 429-retry inside veo_generate — keeps the
# submission rate under the ceiling. Tunable via VEO_MAX_CONCURRENT_SUBMITS.
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


# Max number of image-generation calls in flight at once. The talking-head
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
        raise HTTPException(404, f"talking-head run #{run_id} not found")
    # JSONB may come back as a string in rare cases
    md = row.get("metadata") or {}
    if isinstance(md, str):
        try:
            md = json.loads(md)
        except Exception:
            md = {}
    row["metadata"] = md
    return row


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
    ) -> None:
        self.character_asset_id = character_asset_id
        self.character_upload_id = character_upload_id
        self.bg_asset_id = bg_asset_id
        self.bg_upload_id = bg_upload_id
        self.intro_asset_id = intro_asset_id
        self.intro_upload_id = intro_upload_id
        self.outro_asset_id = outro_asset_id
        self.outro_upload_id = outro_upload_id

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
        }

    def has_intro(self) -> bool:
        return self.intro_asset_id is not None or self.intro_upload_id is not None

    def has_outro(self) -> bool:
        return self.outro_asset_id is not None or self.outro_upload_id is not None


# ---------------------------------------------------------------------------
# Image-model helpers — talking-head supports a separate model choice per
# tile (intro card, main keyframe, outro card).
# ---------------------------------------------------------------------------

DEFAULT_IMAGE_MODELS = {
    "main": ENGINE_OPENAI,
    "intro": ENGINE_OPENAI,
    "outro": ENGINE_OPENAI,
}


def _normalize_image_models(raw: dict | None) -> dict:
    """Coerce a partial / unknown-value image_models dict into the canonical
    shape with valid engine names."""
    out = dict(DEFAULT_IMAGE_MODELS)
    if not raw or not isinstance(raw, dict):
        return out
    for k in ("main", "intro", "outro"):
        v = raw.get(k)
        if isinstance(v, str) and v.lower() in (ENGINE_OPENAI, ENGINE_GEMINI_PRO):
            out[k] = v.lower()
    return out


# ---------------------------------------------------------------------------
# Upload-slot resolution — pulls a yas_uploads row's local path / preview.
# ---------------------------------------------------------------------------

async def _resolve_upload_row(upload_id: int) -> dict | None:
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT id, local_path, mime_type, width, height, bytes,
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
) -> dict:
    """Run GPT-4o to produce the talking-head script, persist a row, return it.

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
    """
    topic = (topic or "").strip()
    if not topic:
        raise HTTPException(400, "topic is required")
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

    # Build full RAG context using the producer's helper (brand + nearest
    # talking-head scripts + IG posts + IG scenes + character library).
    context_block = ""
    try:
        context_block = await build_context_block(
            query_text=topic,
            k=3,
            dialogue_language="en",
            video_type="talking_head",
        )
    except Exception:
        log.exception("RAG context build failed (non-fatal — script gen continues)")
        context_block = ""

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
    )

    # Call OpenAI with structured output. We use chat() but inject the
    # response_format kwarg so we get strict JSON back per our schema.
    try:
        raw = await openai_chat(
            user_prompt,
            system=th_prompts.TALKING_HEAD_SCRIPT_SYSTEM,
            model="gpt-4o",
            max_tokens=1200,
            temperature=0.7,
            response_format=_SCRIPT_RESPONSE_FORMAT,
        )
    except OpenAIBudgetExceededError as e:
        raise HTTPException(503, f"OpenAI budget exhausted: {e}")
    except Exception:
        log.exception("OpenAI script generation failed")
        raise HTTPException(502, "Script generation failed — try again in a moment.")

    try:
        script = json.loads(raw)
    except json.JSONDecodeError:
        log.error("OpenAI returned non-JSON despite structured output: %r", raw[:400])
        raise HTTPException(502, "Script generation returned invalid JSON.")

    # ----- Post-GPT verbatim guard + scenes-array normalisation ---------
    # The schema demands `scenes` is always present. Older GPT responses
    # (pre-v6 prompt) may omit it; if so, synthesize from the top-level
    # dialogue/hook fields so downstream code can rely on the unified shape.
    if isinstance(script, dict):
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

    # ----- Build the metadata payload ----------------------------------
    metadata: dict[str, Any] = {
        "category": "talking_head",
        "script": script,
        "refs": refs.to_dict(),
        "lock_character": bool(lock_character),
        "image_models": image_models,
        "intro_image_id": None,
        "outro_image_id": None,
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
        metadata["composition_template"] = "talking-head-multi-scene"
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

    log.info(
        "[yas-th] script generated for run #%d (topic=%r, models=%s, lock=%s)",
        run_id, topic[:60], image_models, lock_character,
    )

    # Return the same shape fetch_run does
    return await fetch_run(run_id)


# ---------------------------------------------------------------------------
# Step 2 — Preview generation (3 images in parallel)
# ---------------------------------------------------------------------------

async def _run_image_gen(
    *,
    prompt: str,
    image_model: str,
    source_images: list[Path] | None = None,
    aspect: str = "9:16",
    size: str = "1024x1536",   # gpt-image-1 valid: 1024x1024 / 1024x1536 / 1536x1024
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
    return data, "image/png"


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


async def generate_previews(run_id: int) -> dict:
    """Generate the three preview images (main + intro + outro).

    Each tile uses its own image_model from metadata.image_models.
    Skips intro/outro card gen when the user has supplied a reference
    (asset OR upload) for that slot — the composition pipeline uses the
    reference directly.
    """
    run = await _fetch_run_row(run_id)
    md = run["metadata"]
    script = md.get("script") or {}
    refs = md.get("refs") or {}
    lock_character = bool(md.get("lock_character"))
    image_models = _normalize_image_models(md.get("image_models"))
    log.info(
        "[yas-th] generate_previews run=%d engines main=%s intro=%s outro=%s (metadata raw=%s)",
        run_id, image_models["main"], image_models["intro"], image_models["outro"],
        md.get("image_models"),
    )

    # Resolve each slot — could be either asset OR upload OR empty.
    # For multi-scene runs where the user picked per-scene refs without
    # touching the (deprecated) global refs, fall back to scenes[0] so
    # the main keyframe prompt sees a character/bg brief.
    _scenes_for_brief: list[dict] = md.get("scenes") or []
    _scene0_brief: dict = _scenes_for_brief[0] if _scenes_for_brief else {}
    _, character_brief = await _slot_row_and_brief(
        asset_id=refs.get("character_asset_id") or _scene0_brief.get("character_asset_id"),
        upload_id=refs.get("character_upload_id") or _scene0_brief.get("character_upload_id"),
        kind="character",
    )
    _, bg_brief = await _slot_row_and_brief(
        asset_id=refs.get("bg_asset_id") or _scene0_brief.get("bg_asset_id"),
        upload_id=refs.get("bg_upload_id") or _scene0_brief.get("bg_upload_id"),
        kind="bg",
    )
    intro_has_ref = bool(refs.get("intro_asset_id") or refs.get("intro_upload_id"))
    outro_has_ref = bool(refs.get("outro_asset_id") or refs.get("outro_upload_id"))
    _, intro_brief = await _slot_row_and_brief(
        asset_id=refs.get("intro_asset_id"),
        upload_id=refs.get("intro_upload_id"),
        kind="intro",
    )
    _, outro_brief = await _slot_row_and_brief(
        asset_id=refs.get("outro_asset_id"),
        upload_id=refs.get("outro_upload_id"),
        kind="outro",
    )

    brand_req = await build_brand_requirements()

    main_prompt = th_prompts.build_main_keyframe_prompt(
        script=script,
        character_brief=character_brief,
        bg_brief=bg_brief,
        lock_character=lock_character,
    )

    # v3: intro/outro card prompts always run image gen. When the user
    # provided a reference (asset OR upload), it goes in as a source_image
    # guidance to the image model; the prompt instructs the model to keep
    # the reference's style but replace the text with GPT-derived copy.
    intro_prompt = th_prompts.build_intro_card_prompt(
        topic=run["topic"],
        script=script,
        brand_summary=brand_req.get("summary") or "",
        brand_primary_colors=brand_req.get("primary_colors") or [],
        brand_fonts=brand_req.get("fonts") or [],
        reference_brief=intro_brief,
        has_reference_image=intro_has_ref,
    )
    outro_prompt = th_prompts.build_outro_card_prompt(
        topic=run["topic"],
        script=script,
        brand_summary=brand_req.get("summary") or "",
        brand_primary_colors=brand_req.get("primary_colors") or [],
        brand_fonts=brand_req.get("fonts") or [],
        reference_brief=outro_brief,
        has_reference_image=outro_has_ref,
    )

    # Resolve source_images for the main keyframe (= scene 0) when locking
    # character / using a bg reference. Both go in as guidance images.
    #
    # In MULTI-SCENE runs the user may have only set per-scene refs (no
    # global). For scene 0's main keyframe we fall back to scenes_meta[0]
    # when the global refs are null. Scenes 1..N-1 use the same
    # per-scene-with-global-fallback logic via `_scene_sources` below.
    #
    # When lock_character=True we duplicate the character image so it
    # appears TWICE in source_images — first as the subject, second as an
    # identity anchor. Image-edit models weight identity more strongly
    # when the reference is repeated. Cap at 3 (Gemini's limit); drop bg
    # first if the cap forces it (bg can still be hinted in text).
    scenes_meta_for_main: list[dict] = md.get("scenes") or []
    scene0_meta = scenes_meta_for_main[0] if scenes_meta_for_main else {}

    def _ref_or_scene0(key: str) -> int | None:
        # Global refs win when set; otherwise fall back to scene 0's per-scene pick.
        return refs.get(key) or scene0_meta.get(key)

    main_sources: list[Path] = []
    char_path: Path | None = None
    if lock_character:
        try:
            char_path = await _slot_path(
                asset_id=_ref_or_scene0("character_asset_id"),
                upload_id=_ref_or_scene0("character_upload_id"),
            )
            if char_path:
                main_sources.append(char_path)
                main_sources.append(char_path)  # identity anchor
        except Exception:
            log.exception("char ref path resolution failed for run #%d", run_id)
    try:
        bg_p = await _slot_path(
            asset_id=_ref_or_scene0("bg_asset_id"),
            upload_id=_ref_or_scene0("bg_upload_id"),
        )
        if bg_p:
            main_sources.append(bg_p)
    except Exception:
        log.exception("bg ref path resolution failed for run #%d", run_id)
    main_sources = main_sources[:3]

    # Run the three calls in parallel (skip intro/outro when refs supplied)
    async def _gen_main() -> tuple[bytes, str, str] | Exception:
        try:
            data, mime = await _run_image_gen(
                prompt=main_prompt, image_model=image_models["main"],
                source_images=main_sources or None,
            )
            return (data, mime, main_prompt)
        except Exception as e:
            log.exception("main keyframe gen failed for run #%d", run_id)
            return e

    # v3: always run intro/outro card gen. When the user has a reference
    # (asset or upload), it goes in as a source_image for style guidance —
    # the GPT-derived headline/subtitle replaces any text in the reference.
    intro_ref_path: Path | None = None
    if intro_has_ref:
        try:
            intro_ref_path = await _slot_path(
                asset_id=refs.get("intro_asset_id"),
                upload_id=refs.get("intro_upload_id"),
            )
        except Exception:
            log.exception("intro ref path resolution failed for run #%d", run_id)
    outro_ref_path: Path | None = None
    if outro_has_ref:
        try:
            outro_ref_path = await _slot_path(
                asset_id=refs.get("outro_asset_id"),
                upload_id=refs.get("outro_upload_id"),
            )
        except Exception:
            log.exception("outro ref path resolution failed for run #%d", run_id)

    async def _gen_intro() -> tuple[bytes, str, str] | Exception:
        try:
            data, mime = await _run_image_gen(
                prompt=intro_prompt, image_model=image_models["intro"],
                source_images=[intro_ref_path] if intro_ref_path else None,
            )
            return (data, mime, intro_prompt)
        except Exception as e:
            log.exception("intro card gen failed for run #%d", run_id)
            return e

    async def _gen_outro() -> tuple[bytes, str, str] | Exception:
        try:
            data, mime = await _run_image_gen(
                prompt=outro_prompt, image_model=image_models["outro"],
                source_images=[outro_ref_path] if outro_ref_path else None,
            )
            return (data, mime, outro_prompt)
        except Exception as e:
            log.exception("outro card gen failed for run #%d", run_id)
            return e

    main_res, intro_res, outro_res = await asyncio.gather(
        _gen_main(), _gen_intro(), _gen_outro(), return_exceptions=False,
    )

    # Persist the MAIN result onto the existing run row.
    if isinstance(main_res, Exception):
        await _mark_row_failed(run_id, f"main_keyframe_failed: {str(main_res)[:240]}")
    elif main_res is not None:
        data, mime, used_prompt = main_res
        width, height = _image_dims(data)
        ext = "png"
        local_dest = TH_ROOT / f"{run_id}" / "main.png"
        stored = await _store_image_bytes(
            image_bytes=data,
            bucket_path=f"yas/talking-head/{run_id}/main.{ext}",
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
                           prompt_used    = %s,
                           status         = %s,
                           updated_at     = NOW()
                     WHERE id = %s
                    """,
                    (
                        stored["storage_bucket"], stored["storage_path"],
                        stored["local_path"], mime, len(data),
                        width, height, used_prompt[:8000],
                        STATUS_READY, run_id,
                    ),
                )

    # Persist intro / outro as sibling rows
    intro_image_id: int | None = None
    outro_image_id: int | None = None
    if isinstance(intro_res, tuple):
        intro_image_id = await _insert_card_row(
            parent_run_id=run_id, topic=run["topic"],
            image_model=image_models["intro"],
            card_mode=MODE_INTRO, image_bytes=intro_res[0],
            mime=intro_res[1], used_prompt=intro_res[2], card_type="intro",
        )
    elif isinstance(intro_res, Exception):
        log.warning("intro card gen failed: %s", intro_res)
    if isinstance(outro_res, tuple):
        outro_image_id = await _insert_card_row(
            parent_run_id=run_id, topic=run["topic"],
            image_model=image_models["outro"],
            card_mode=MODE_OUTRO, image_bytes=outro_res[0],
            mime=outro_res[1], used_prompt=outro_res[2], card_type="outro",
        )
    elif isinstance(outro_res, Exception):
        log.warning("outro card gen failed: %s", outro_res)

    await _patch_metadata(run_id, {
        "intro_image_id": intro_image_id,
        "outro_image_id": outro_image_id,
        "intro_error": str(intro_res)[:480] if isinstance(intro_res, Exception) else None,
        "outro_error": str(outro_res)[:480] if isinstance(outro_res, Exception) else None,
        "main_error":  str(main_res)[:480]  if isinstance(main_res,  Exception) else None,
    })

    # ----- v6 — extra scene keyframes (scenes[1..N-1]) -----------------
    # scene_count == 1 → already done above (run row's local_path holds
    # the single scene's keyframe). Nothing to do.
    # scene_count > 1 → image-gen N-1 additional keyframes in parallel,
    # each as a sibling row. scenes[0] keeps the run row's keyframe.
    scene_count = int(md.get("scene_count") or 1)
    if scene_count > 1:
        scenes_meta = md.get("scenes") or []

        # Build per-scene source_images. Each scene inherits the global
        # character + bg unless its override is set in the scene metadata.
        async def _scene_sources(scene_idx: int) -> list[Path]:
            sm = scenes_meta[scene_idx] if scene_idx < len(scenes_meta) else {}
            srcs: list[Path] = []
            if lock_character:
                try:
                    p = await _slot_path(
                        asset_id=sm.get("character_asset_id") or refs.get("character_asset_id"),
                        upload_id=sm.get("character_upload_id") or refs.get("character_upload_id"),
                    )
                    if p:
                        srcs.append(p)
                        srcs.append(p)  # identity anchor (duplicate ref)
                except Exception:
                    log.exception("scene %d character path resolve failed", scene_idx)
            try:
                p = await _slot_path(
                    asset_id=sm.get("bg_asset_id") or refs.get("bg_asset_id"),
                    upload_id=sm.get("bg_upload_id") or refs.get("bg_upload_id"),
                )
                if p:
                    srcs.append(p)
            except Exception:
                log.exception("scene %d bg path resolve failed", scene_idx)
            return srcs[:3]  # Gemini cap

        async def _gen_extra_scene(scene_idx: int) -> tuple[int, bytes | None, str | None, str | None]:
            srcs = await _scene_sources(scene_idx)
            try:
                data, mime = await _run_image_gen(
                    prompt=main_prompt, image_model=image_models["main"],
                    source_images=srcs or None,
                )
                return (scene_idx, data, mime, None)
            except Exception as e:
                log.exception(
                    "scene %d keyframe gen failed for run #%d",
                    scene_idx, run_id,
                )
                return (scene_idx, None, None, str(e)[:480] or type(e).__name__)

        extra_results = await asyncio.gather(
            *[_gen_extra_scene(i) for i in range(1, scene_count)],
            return_exceptions=False,
        )

        # Persist each as a sibling scene row + update metadata.scenes[i].keyframe_id.
        # Also write metadata.scenes[i].error so the UI surfaces silent failures
        # as a red error tile instead of an empty "(auto-generated)" placeholder.
        fresh = await _fetch_run_row(run_id)
        fresh_md = fresh["metadata"] or {}
        fresh_scenes = list(fresh_md.get("scenes") or [])
        for scene_idx, data, mime, err in extra_results:
            if scene_idx < len(fresh_scenes):
                fresh_scenes[scene_idx]["error"] = err  # None on success
            if data is None or mime is None:
                continue
            row_id = await _insert_scene_row(
                parent_run_id=run_id, topic=run["topic"],
                image_model=image_models["main"],
                scene_index=scene_idx,
                image_bytes=data, mime=mime, used_prompt=main_prompt,
            )
            if scene_idx < len(fresh_scenes):
                fresh_scenes[scene_idx]["keyframe_id"] = row_id

        await _patch_metadata(run_id, {"scenes": fresh_scenes})
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
) -> int:
    """Create a sibling yas_generations row for an intro / outro card.

    Returns the new row id.
    """
    # Insert with empty paths first to get the id; then store the bytes.
    metadata = {
        "category": "talking_head_card",
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
        bucket_path=f"yas/talking-head/{parent_run_id}/{card_type}-{card_id}.png",
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
) -> int:
    """Create a sibling yas_generations row for a multi-scene keyframe."""
    metadata = {
        "category": "talking_head_scene",
        "parent_talking_head_id": parent_run_id,
        "scene_index": scene_index,
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
                    topic, MODE_SCENE, image_model, used_prompt[:8000],
                    STATUS_GENERATING, Jsonb(metadata),
                ),
            )
            new = await cur.fetchone()
            row_id = int(new["id"])

    width, height = _image_dims(image_bytes)
    local_dest = TH_ROOT / f"{parent_run_id}" / f"scene-{scene_index}-{row_id}.png"
    stored = await _store_image_bytes(
        image_bytes=image_bytes,
        bucket_path=f"yas/talking-head/{parent_run_id}/scene-{scene_index}-{row_id}.png",
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
    if not feedback_text:
        raise HTTPException(400, "feedback_text is required")

    run = await _fetch_run_row(run_id)
    md = run["metadata"]
    script = md.get("script") or {}
    refs = md.get("refs") or {}
    lock_character = bool(md.get("lock_character"))
    # User's current toggle selection wins over the engine stored on the run.
    # If an override arrived from the client, use it AND persist it back so
    # subsequent retries stay consistent with what the user picked.
    if image_models_override:
        image_models = _normalize_image_models(image_models_override)
        await _patch_metadata(run_id, {"image_models": image_models})
    else:
        image_models = _normalize_image_models(md.get("image_models"))
    # Scene tiles all use the run's main image model.
    image_model = image_models["main"] if scene_idx is not None else image_models[which]
    log.info(
        "[yas-th] retry_preview run=%d which=%s engine=%s (override=%s)",
        run_id, which, image_model, bool(image_models_override),
    )

    brand_req = await build_brand_requirements()

    # Same fallback as generate_previews — when global refs are empty but
    # per-scene refs exist (multi-scene runs), use scenes[0] for the main
    # keyframe brief and scenes[scene_idx] for scene retries.
    _scenes_for_brief: list[dict] = md.get("scenes") or []
    _brief_fallback: dict = {}
    if scene_idx is not None and scene_idx < len(_scenes_for_brief):
        _brief_fallback = _scenes_for_brief[scene_idx] or {}
    elif _scenes_for_brief:
        _brief_fallback = _scenes_for_brief[0] or {}
    _, character_brief = await _slot_row_and_brief(
        asset_id=refs.get("character_asset_id") or _brief_fallback.get("character_asset_id"),
        upload_id=refs.get("character_upload_id") or _brief_fallback.get("character_upload_id"),
        kind="character",
    )
    _, bg_brief = await _slot_row_and_brief(
        asset_id=refs.get("bg_asset_id") or _brief_fallback.get("bg_asset_id"),
        upload_id=refs.get("bg_upload_id") or _brief_fallback.get("bg_upload_id"),
        kind="bg",
    )
    intro_has_ref = bool(refs.get("intro_asset_id") or refs.get("intro_upload_id"))
    outro_has_ref = bool(refs.get("outro_asset_id") or refs.get("outro_upload_id"))

    # ---- Scene retry (v6, multi-scene only) -------------------------------
    if scene_idx is not None:
        scenes_meta = list((md or {}).get("scenes") or [])
        scene_count = int((md or {}).get("scene_count") or 1)
        if scene_idx >= scene_count or scene_idx >= len(scenes_meta):
            raise HTTPException(400, f"scene_{scene_idx} not in this run")
        # Reuse the main keyframe prompt; per-scene character/bg overrides
        # affect source_images, not the prompt text.
        sm = scenes_meta[scene_idx]
        base_prompt = th_prompts.build_main_keyframe_prompt(
            script=script,
            character_brief=character_brief,
            bg_brief=bg_brief,
            lock_character=lock_character,
        )
        full_prompt = th_prompts.append_feedback_to_prompt(base_prompt, feedback_text)
        sources: list[Path] = []
        if lock_character:
            try:
                p = await _slot_path(
                    asset_id=sm.get("character_asset_id") or refs.get("character_asset_id"),
                    upload_id=sm.get("character_upload_id") or refs.get("character_upload_id"),
                )
                if p:
                    sources.append(p)
                    sources.append(p)  # identity anchor
            except Exception:
                log.exception("scene %d char path resolve failed", scene_idx)
        try:
            bg_p = await _slot_path(
                asset_id=sm.get("bg_asset_id") or refs.get("bg_asset_id"),
                upload_id=sm.get("bg_upload_id") or refs.get("bg_upload_id"),
            )
            if bg_p:
                sources.append(bg_p)
        except Exception:
            log.exception("scene %d bg path resolve failed", scene_idx)
        sources = sources[:3]
        try:
            data, mime = await _run_image_gen(
                prompt=full_prompt, image_model=image_model,
                source_images=sources or None,
            )
        except Exception as e:
            log.exception("retry scene %d failed for run #%d", scene_idx, run_id)
            raise HTTPException(502, f"Scene {scene_idx + 1} retry failed.")

        if scene_idx == 0:
            # Scene 0 lives on the run row's image columns (legacy compat).
            # Overwrite in place + bump updated_at so the cache-buster
            # (?v=<updated_at>) forces the browser to refetch.
            width, height = _image_dims(data)
            local_dest = TH_ROOT / f"{run_id}" / "main.png"
            stored = await _store_image_bytes(
                image_bytes=data,
                bucket_path=f"yas/talking-head/{run_id}/main.png",
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
                               prompt_used    = %s,
                               status         = %s,
                               feedback_text  = %s,
                               image_model    = %s,
                               updated_at     = NOW()
                         WHERE id = %s
                        """,
                        (
                            stored["storage_bucket"], stored["storage_path"],
                            stored["local_path"], mime, len(data),
                            width, height, full_prompt[:8000],
                            STATUS_READY, feedback_text, image_model, run_id,
                        ),
                    )
        else:
            # Scenes 1..N-1: insert a new sibling row + repoint
            # metadata.scenes[i].keyframe_id at it (old row is left in DB).
            new_row_id = await _insert_scene_row(
                parent_run_id=run_id, topic=run["topic"],
                image_model=image_model,
                scene_index=scene_idx,
                image_bytes=data, mime=mime, used_prompt=full_prompt,
            )
            scenes_meta[scene_idx]["keyframe_id"] = new_row_id
            # Clear any prior failure marker now that retry succeeded.
            scenes_meta[scene_idx]["error"] = None
            await _patch_metadata(run_id, {"scenes": scenes_meta})
        return await fetch_run(run_id)

    if which == "main":
        base_prompt = th_prompts.build_main_keyframe_prompt(
            script=script,
            character_brief=character_brief,
            bg_brief=bg_brief,
            lock_character=lock_character,
        )
        full_prompt = th_prompts.append_feedback_to_prompt(base_prompt, feedback_text)
        # Resolve source_images same as on initial generation, with the
        # character ref duplicated as an identity anchor when lock_character
        # is on. Cap at 3 (Gemini's limit); drop bg first if needed. For
        # multi-scene runs, fall back to scenes[0]'s per-scene refs when
        # global refs are empty.
        _scenes_for_main: list[dict] = md.get("scenes") or []
        _scene0_for_main: dict = _scenes_for_main[0] if _scenes_for_main else {}
        sources: list[Path] = []
        if lock_character:
            try:
                p = await _slot_path(
                    asset_id=refs.get("character_asset_id") or _scene0_for_main.get("character_asset_id"),
                    upload_id=refs.get("character_upload_id") or _scene0_for_main.get("character_upload_id"),
                )
                if p:
                    sources.append(p)
                    sources.append(p)  # identity anchor
            except Exception:
                pass
        try:
            bg_p = await _slot_path(
                asset_id=refs.get("bg_asset_id") or _scene0_for_main.get("bg_asset_id"),
                upload_id=refs.get("bg_upload_id") or _scene0_for_main.get("bg_upload_id"),
            )
            if bg_p:
                sources.append(bg_p)
        except Exception:
            pass
        sources = sources[:3]
        try:
            data, mime = await _run_image_gen(
                prompt=full_prompt, image_model=image_model,
                source_images=sources or None,
            )
        except Exception as e:
            log.exception("retry main failed for run #%d", run_id)
            await _mark_row_failed(run_id, f"retry_main_failed: {str(e)[:240]}")
            raise HTTPException(502, "Main keyframe retry failed.")
        width, height = _image_dims(data)
        local_dest = TH_ROOT / f"{run_id}" / "main.png"
        stored = await _store_image_bytes(
            image_bytes=data,
            bucket_path=f"yas/talking-head/{run_id}/main.png",
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
                           prompt_used    = %s,
                           status         = %s,
                           feedback_text  = %s,
                           image_model    = %s,
                           updated_at     = NOW()
                     WHERE id = %s
                    """,
                    (
                        stored["storage_bucket"], stored["storage_path"],
                        stored["local_path"], mime, len(data),
                        width, height, full_prompt[:8000],
                        STATUS_READY, feedback_text, image_model, run_id,
                    ),
                )
        return await fetch_run(run_id)

    # Intro / outro retry. v3: references no longer block retry — they go
    # in as guidance images to the regen call.
    card_type = which
    ref_brief = intro_brief if card_type == "intro" else outro_brief
    has_ref = intro_has_ref if card_type == "intro" else outro_has_ref
    ref_path: Path | None = None
    if has_ref:
        try:
            ref_path = await _slot_path(
                asset_id=refs.get(f"{card_type}_asset_id"),
                upload_id=refs.get(f"{card_type}_upload_id"),
            )
        except Exception:
            log.exception("%s ref path resolve failed for run #%d", card_type, run_id)
    base_prompt = (
        th_prompts.build_intro_card_prompt(
            topic=run["topic"], script=script,
            brand_summary=brand_req.get("summary") or "",
            brand_primary_colors=brand_req.get("primary_colors") or [],
            brand_fonts=brand_req.get("fonts") or [],
            reference_brief=ref_brief,
            has_reference_image=has_ref,
        )
        if card_type == "intro"
        else th_prompts.build_outro_card_prompt(
            topic=run["topic"], script=script,
            brand_summary=brand_req.get("summary") or "",
            brand_primary_colors=brand_req.get("primary_colors") or [],
            brand_fonts=brand_req.get("fonts") or [],
            reference_brief=ref_brief,
            has_reference_image=has_ref,
        )
    )
    full_prompt = th_prompts.append_feedback_to_prompt(base_prompt, feedback_text)
    try:
        data, mime = await _run_image_gen(
            prompt=full_prompt, image_model=image_model,
            source_images=[ref_path] if ref_path else None,
        )
    except Exception as e:
        log.exception("retry %s card failed for run #%d", card_type, run_id)
        raise HTTPException(502, f"{card_type.capitalize()} card retry failed.")

    # Insert a NEW sibling row (we don't overwrite the prior card — its file
    # is still on disk; users can re-pick if they want).
    new_card_id = await _insert_card_row(
        parent_run_id=run_id, topic=run["topic"], image_model=image_model,
        card_mode=MODE_INTRO if card_type == "intro" else MODE_OUTRO,
        image_bytes=data, mime=mime, used_prompt=full_prompt,
        card_type=card_type,
    )
    patch_key = "intro_image_id" if card_type == "intro" else "outro_image_id"
    await _patch_metadata(run_id, {patch_key: new_card_id})
    return await fetch_run(run_id)


# ---------------------------------------------------------------------------
# Step 4a — Start videos (kicks THREE Veo calls: intro / main / outro)
# ---------------------------------------------------------------------------
#
# All three segments become Veo clips. The intro and outro use their card
# stills as i2v references (so they keep the layout but pick up subtle
# motion); the main keyframe drives the talking-head clip with audio.
# Composition stays manually-gated by the user — once all three Veos are
# ready the poller flips composition_status to `videos_ready`, and the
# user clicks `stitch_composition` to fire Remotion.

# Veo durations per segment
VEO_DURATION_BOOKEND = 4   # minimum Veo 3.1 duration; intro & outro cards
VEO_DURATION_MAIN = 8      # the talking-head clip


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


async def _resolve_intro_card_path(*, run_id: int, md: dict, refs: dict) -> Path | None:
    """For the intro segment, return the keyframe Veo should animate.

    v4 priority: auto-generated card FIRST. The card carries the topic-aware
    headline/subtitle text that the v3 image-gen step produced. Falling back
    to the user's reference would animate the ORIGINAL reference text — not
    what we want. The user refs are fallbacks for legacy runs or runs where
    the image-gen step failed.
    """
    auto = await _resolve_card_path(md.get("intro_image_id"))
    if auto is not None:
        return auto
    if refs.get("intro_upload_id"):
        try:
            return await _resolve_upload_path(int(refs["intro_upload_id"]))
        except Exception:
            log.exception("intro upload path resolve failed for run #%d", run_id)
    if refs.get("intro_asset_id"):
        try:
            return await _resolve_asset_path(int(refs["intro_asset_id"]))
        except Exception:
            log.exception("intro asset path resolve failed for run #%d", run_id)
    return None


async def _resolve_outro_card_path(*, run_id: int, md: dict, refs: dict) -> Path | None:
    """v4 priority: auto-generated card FIRST (see _resolve_intro_card_path)."""
    auto = await _resolve_card_path(md.get("outro_image_id"))
    if auto is not None:
        return auto
    if refs.get("outro_upload_id"):
        try:
            return await _resolve_upload_path(int(refs["outro_upload_id"]))
        except Exception:
            log.exception("outro upload path resolve failed for run #%d", run_id)
    if refs.get("outro_asset_id"):
        try:
            return await _resolve_asset_path(int(refs["outro_asset_id"]))
        except Exception:
            log.exception("outro asset path resolve failed for run #%d", run_id)
    return None


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
            f"You are shown the {role} card for a talking-head Instagram "
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
) -> int:
    """Insert a yas_animations row scoped to a talking-head run."""
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
                    VEO_ASPECT_RATIO, STATUS_GENERATING, cost_usd,
                    Jsonb({
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
    """True only for retryable SERVER-capacity errors — never for RAI/quota
    (those won't self-heal by re-submitting the identical request)."""
    m = (msg or "").lower()
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
        if attempts >= MAX_TRANSIENT_VEO_RETRIES:
            log.warning(
                "anim=%d transient-retry cap (%d) reached; marking failed",
                anim_id, MAX_TRANSIENT_VEO_RETRIES,
            )
            return False
        run_id = int(meta.get("parent_talking_head_id") or row.get("yas_generation_id"))
        role = str(meta.get("role") or "")
        run = await _fetch_run_row(run_id)
        md = run.get("metadata") or {}
        script = md.get("script") or {}
        refs = md.get("refs") or {}
        lock_character = bool(md.get("lock_character"))
        topic = run.get("topic")
        brand_req = await build_brand_requirements()
        negative = th_prompts.build_talking_head_negative_prompt(brand_req)

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
            prompt = th_prompts.build_main_veo_prompt(
                script=seg_script, brand_req=brand_req,
                has_character_ref=char_p is not None, has_bg_ref=bg_p is not None,
                dialogue_language=script.get("dialogue_language") or "en",
            )
            packed = [kf] + ([char_p] if char_p else []) + ([bg_p] if bg_p else [])
            op = await veo_generate(
                prompt, model=config.veo_model, duration_sec=VEO_DURATION_MAIN,
                aspect_ratio=VEO_ASPECT_RATIO, with_audio=True,
                reference_images=packed[:3], negative_prompt=negative,
            )
        elif role in ("intro", "outro"):
            if role == "intro":
                card = await _resolve_intro_card_path(run_id=run_id, md=md, refs=refs)
                style = md.get("intro_animation_style") or "fade_in"
                prompt = th_prompts.build_intro_veo_prompt(
                    topic=topic, script=script, style=style,
                )
            else:
                card = await _resolve_outro_card_path(run_id=run_id, md=md, refs=refs)
                style = md.get("outro_animation_style") or "fade_hold"
                prompt = th_prompts.build_outro_veo_prompt(
                    topic=topic, script=script, style=style,
                )
            if card is None:
                return False
            op = await veo_generate(
                prompt, model=config.veo_model, duration_sec=VEO_DURATION_BOOKEND,
                aspect_ratio=VEO_ASPECT_RATIO, with_audio=False,
                start_image=card, reference_images=None,
            )
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
    """Kick THREE Veo calls (intro / main / outro) in parallel.

    All three become yas_animations rows. The composition poller flips
    `composition_status` to `videos_ready` once all three land, at which
    point the UI lets the user preview each clip and click "Stitch".
    """
    run = await _fetch_run_row(run_id)
    if not run.get("local_path"):
        raise HTTPException(409, "Main keyframe not ready — generate previews first.")
    md = run["metadata"] or {}
    script = md.get("script") or {}
    refs = md.get("refs") or {}
    lock_character = bool(md.get("lock_character"))
    topic = run["topic"]

    # Refuse if any Veo gen is already in flight for this run
    for key in ("intro_animation_id", "main_animation_id", "outro_animation_id"):
        existing_id = md.get(key)
        if existing_id:
            existing = await _fetch_animation_row(int(existing_id))
            if existing and existing.get("status") == STATUS_GENERATING:
                raise HTTPException(
                    409, "Videos are already generating for this run.",
                )

    # Resolve segment keyframes
    main_path = _abs_from_rel(run["local_path"])
    if not main_path.exists():
        raise HTTPException(410, "Main keyframe file missing on disk")
    intro_card_path = await _resolve_intro_card_path(
        run_id=run_id, md=md, refs=refs,
    )
    outro_card_path = await _resolve_outro_card_path(
        run_id=run_id, md=md, refs=refs,
    )
    # Diagnostic — confirms Fix 4's auto-card-first priority is actually
    # taking effect. The user reported "Veo still animates the original
    # ref"; this log makes the actual chosen path inspectable.
    log.info(
        "[yas-th] start_videos run=%d intro_card=%s auto_id=%s ref_upload=%s ref_asset=%s",
        run_id, intro_card_path,
        md.get("intro_image_id"),
        refs.get("intro_upload_id"), refs.get("intro_asset_id"),
    )
    log.info(
        "[yas-th] start_videos run=%d outro_card=%s auto_id=%s ref_upload=%s ref_asset=%s",
        run_id, outro_card_path,
        md.get("outro_image_id"),
        refs.get("outro_upload_id"), refs.get("outro_asset_id"),
    )

    # ---- Resolve character + bg paths (for ref2v slots on main) -------------
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
            log.exception("char ref path resolution failed for run #%d", run_id)
    try:
        bg_path = await _slot_path(
            asset_id=refs.get("bg_asset_id"),
            upload_id=refs.get("bg_upload_id"),
        )
    except Exception:
        log.exception("bg ref path resolution failed for run #%d", run_id)

    # Pack the main clip's 3 reference slots: [keyframe, character?, bg?]
    # in that priority order. Veo accepts max 3.
    main_refs: list[Path] = [main_path]
    if character_path:
        main_refs.append(character_path)
    if bg_path:
        main_refs.append(bg_path)
    main_refs = main_refs[:3]

    # ---- Main Veo prompt (with IDENTITY + LOCATION locks) and negative prompt
    main_prompt = th_prompts.build_main_veo_prompt(
        script=script,
        brand_req=brand_req,
        has_character_ref=character_path is not None,
        has_bg_ref=bg_path is not None,
        dialogue_language=script.get("dialogue_language") or "en",
    )
    main_negative = th_prompts.build_talking_head_negative_prompt(brand_req)

    # ---- Vision-picked animation style per bookend (Fix 5) -----------------
    # Run the picks in parallel; either picker can fail without blocking the
    # other (each catches its own exceptions and returns a safe fallback).
    intro_style_task = (
        pick_bookend_animation(
            card_path=intro_card_path, script=script, topic=topic, role="intro",
        )
        if intro_card_path is not None
        else None
    )
    outro_style_task = (
        pick_bookend_animation(
            card_path=outro_card_path, script=script, topic=topic, role="outro",
        )
        if outro_card_path is not None
        else None
    )
    intro_style = (
        await intro_style_task if intro_style_task is not None else "fade_in"
    )
    outro_style = (
        await outro_style_task if outro_style_task is not None else "fade_hold"
    )

    # Persist picks BEFORE Veo kickoff so the UI can show them even while
    # the videos are still rendering.
    await _patch_metadata(run_id, {
        "intro_animation_style": intro_style,
        "outro_animation_style": outro_style,
    })

    # ---- Bookend Veo prompts (i2v mode at 4 s) ------------------------------
    intro_prompt = th_prompts.build_intro_veo_prompt(
        topic=topic, script=script, style=intro_style,
    )
    outro_prompt = th_prompts.build_outro_veo_prompt(
        topic=topic, script=script, style=outro_style,
    )

    # ---- Kick all three Veo calls in parallel -------------------------------
    # Main uses ref2v (reference_images=[3 paths], 8 s, with audio).
    # Intro/outro use i2v (start_image=card_path, NO reference_images,
    # 4 s, no audio) — ref2v can't do 4 s.
    async def _kick_main():
        return await veo_generate(
            main_prompt,
            model=config.veo_model,
            duration_sec=VEO_DURATION_MAIN,
            aspect_ratio=VEO_ASPECT_RATIO,
            with_audio=True,
            reference_images=main_refs,
            negative_prompt=main_negative,
        )

    async def _kick_bookend(prompt: str, card_path: Path, duration: int):
        # i2v mode: pass start_image (top-level), NO reference_images.
        # This supports 4 s; ref2v is hard-locked to 8 s by Vertex.
        return await veo_generate(
            prompt,
            model=config.veo_model,
            duration_sec=duration,
            aspect_ratio=VEO_ASPECT_RATIO,
            with_audio=False,
            start_image=card_path,
            reference_images=None,
        )

    tasks: list[Any] = [_kick_main()]
    if intro_card_path is not None:
        tasks.append(_kick_bookend(intro_prompt, intro_card_path, VEO_DURATION_BOOKEND))
    else:
        tasks.append(None)  # placeholder so indexing stays aligned
    if outro_card_path is not None:
        tasks.append(_kick_bookend(outro_prompt, outro_card_path, VEO_DURATION_BOOKEND))
    else:
        tasks.append(None)

    try:
        # Bound concurrent submissions to dodge Veo's per-base-model 429
        # quota (same ceiling the multi-scene path respects).
        results = await _run_veo_submits_bounded(
            [t for t in tasks if t is not None],
        )
    except VertexBudgetExceededError as e:
        raise HTTPException(503, f"Vertex budget exhausted: {e}")

    # Unpack results — order matches the input list. main is index 0; intro
    # then outro (with the placeholders replaced).
    idx = 0
    main_op = results[idx]; idx += 1
    intro_op = results[idx] if tasks[1] is not None else None
    if tasks[1] is not None: idx += 1
    outro_op = results[idx] if tasks[2] is not None else None

    if isinstance(main_op, Exception):
        log.exception("Veo main kickoff failed for run #%d", run_id)
        raise HTTPException(502, "Could not start the main video. Try again.")
    if isinstance(intro_op, Exception):
        log.warning("Veo intro kickoff failed: %s", intro_op)
        intro_op = None
    if isinstance(outro_op, Exception):
        log.warning("Veo outro kickoff failed: %s", outro_op)
        outro_op = None

    # Persist the rows + link them on the run's metadata.
    main_anim_id = await _insert_animation_row(
        parent_run_id=run_id,
        prompt_used=main_prompt,
        veo_op_name=main_op.get("name") or main_op.get("operation_name"),
        veo_model=main_op.get("model") or config.veo_model,
        duration_sec=VEO_DURATION_MAIN,
        with_audio=True,
        cost_usd=main_op.get("cost_usd"),
        role="main",
    )
    intro_anim_id = None
    outro_anim_id = None
    if intro_op:
        intro_anim_id = await _insert_animation_row(
            parent_run_id=run_id,
            prompt_used=intro_prompt,
            veo_op_name=intro_op.get("name") or intro_op.get("operation_name"),
            veo_model=intro_op.get("model") or config.veo_model,
            duration_sec=VEO_DURATION_BOOKEND,
            with_audio=False,
            cost_usd=intro_op.get("cost_usd"),
            role="intro",
        )
    if outro_op:
        outro_anim_id = await _insert_animation_row(
            parent_run_id=run_id,
            prompt_used=outro_prompt,
            veo_op_name=outro_op.get("name") or outro_op.get("operation_name"),
            veo_model=outro_op.get("model") or config.veo_model,
            duration_sec=VEO_DURATION_BOOKEND,
            with_audio=False,
            cost_usd=outro_op.get("cost_usd"),
            role="outro",
        )

    await _patch_metadata(run_id, {
        "main_animation_id": main_anim_id,
        "intro_animation_id": intro_anim_id,
        "outro_animation_id": outro_anim_id,
        # Backward-compat key for any caller still reading veo_animation_id.
        "veo_animation_id": main_anim_id,
        "composition_status": COMP_QUEUED,
    })

    log.info(
        "[yas-th] start_videos run=%d main=%d intro=%s outro=%s",
        run_id, main_anim_id, intro_anim_id, outro_anim_id,
    )
    return await fetch_run(run_id)


# Backward-compat alias — the router used to call `finalize`. start_videos
# replaces it semantically.
finalize = start_videos


# ---------------------------------------------------------------------------
# v6 — multi-scene Veo orchestration
# ---------------------------------------------------------------------------

async def start_videos_multi(run_id: int) -> dict:
    """Multi-scene variant of start_videos.

    Fires N + 2 Veo calls in parallel: N main-scene clips (ref2v, 8 s each
    with audio) + intro + outro bookends (i2v, 4 s each). Each scene's
    dialogue is whichever line landed in `metadata.scenes[i].dialogue`
    (which is verbatim when the user supplied one). Per-scene character +
    bg overrides apply when set on the scene metadata.

    Persists scene animation ids into `metadata.scenes[i].animation_id`
    and sets `composition_status = COMP_QUEUED`. The composition poller
    then waits for ALL N + 2 animations and dispatches the
    `talking-head-multi-scene` Remotion render.
    """
    run = await _fetch_run_row(run_id)
    md = run["metadata"] or {}
    scene_count = int(md.get("scene_count") or 1)
    if scene_count <= 1:
        raise HTTPException(400, "start_videos_multi requires scene_count > 1")
    scenes_meta = list(md.get("scenes") or [])
    if len(scenes_meta) != scene_count:
        raise HTTPException(
            409, "Scene metadata is inconsistent — regenerate the script.",
        )

    script = md.get("script") or {}
    refs = md.get("refs") or {}
    lock_character = bool(md.get("lock_character"))
    topic = run["topic"]

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

    # ---- Resolve every scene's keyframe path -------------------------------
    keyframe_paths: list[Path | None] = []
    for i in range(scene_count):
        kp = await _resolve_scene_keyframe_path(run_id=run_id, scene_index=i, md=md)
        keyframe_paths.append(kp)
    missing = [i for i, p in enumerate(keyframe_paths) if p is None]
    if missing:
        raise HTTPException(
            410, f"Scene keyframes missing on disk for indices: {missing}",
        )

    # ---- Resolve intro/outro paths (same as start_videos) ------------------
    intro_card_path = await _resolve_intro_card_path(
        run_id=run_id, md=md, refs=refs,
    )
    outro_card_path = await _resolve_outro_card_path(
        run_id=run_id, md=md, refs=refs,
    )
    log.info(
        "[yas-th] start_videos_multi run=%d scenes=%d intro_card=%s outro_card=%s",
        run_id, scene_count, intro_card_path, outro_card_path,
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

    # ---- Bookend animation styles ------------------------------------------
    intro_style_task = (
        pick_bookend_animation(
            card_path=intro_card_path, script=script, topic=topic, role="intro",
        )
        if intro_card_path is not None
        else None
    )
    outro_style_task = (
        pick_bookend_animation(
            card_path=outro_card_path, script=script, topic=topic, role="outro",
        )
        if outro_card_path is not None
        else None
    )
    intro_style = await intro_style_task if intro_style_task is not None else "fade_in"
    outro_style = await outro_style_task if outro_style_task is not None else "fade_hold"

    await _patch_metadata(run_id, {
        "intro_animation_style": intro_style,
        "outro_animation_style": outro_style,
    })

    # ---- Build N main Veo prompts + N source-image lists -------------------
    scene_prompts: list[str] = []
    scene_refs_packed: list[list[Path]] = []
    for i in range(scene_count):
        # Override the script's dialogue with this scene's line so
        # build_main_veo_prompt embeds the right audio clause.
        scene_script = dict(script)
        scene_script["dialogue"] = scenes_meta[i].get("dialogue") or ""
        scene_script["hook"] = scenes_meta[i].get("hook") or ""
        char_p, bg_p = await _scene_refs(i)
        prompt = th_prompts.build_main_veo_prompt(
            script=scene_script,
            brand_req=brand_req,
            has_character_ref=char_p is not None,
            has_bg_ref=bg_p is not None,
            dialogue_language=script.get("dialogue_language") or "en",
        )
        scene_prompts.append(prompt)
        packed: list[Path] = [keyframe_paths[i]]  # type: ignore[list-item]
        if char_p:
            packed.append(char_p)
        if bg_p:
            packed.append(bg_p)
        scene_refs_packed.append(packed[:3])

    main_negative = th_prompts.build_talking_head_negative_prompt(brand_req)

    intro_prompt = th_prompts.build_intro_veo_prompt(
        topic=topic, script=script, style=intro_style,
    )
    outro_prompt = th_prompts.build_outro_veo_prompt(
        topic=topic, script=script, style=outro_style,
    )

    # ---- Kick N + 2 jobs in parallel ---------------------------------------
    async def _kick_scene(idx: int):
        return await veo_generate(
            scene_prompts[idx],
            model=config.veo_model,
            duration_sec=VEO_DURATION_MAIN,
            aspect_ratio=VEO_ASPECT_RATIO,
            with_audio=True,
            reference_images=scene_refs_packed[idx],
            negative_prompt=main_negative,
        )

    async def _kick_bookend(prompt: str, card_path: Path, duration: int):
        return await veo_generate(
            prompt,
            model=config.veo_model,
            duration_sec=duration,
            aspect_ratio=VEO_ASPECT_RATIO,
            with_audio=False,
            start_image=card_path,
            reference_images=None,
        )

    # ---- Bound concurrent Veo submissions ---------------------------------
    # Firing N+2 jobs in a single burst reliably trips Veo 3.1 Fast's
    # per-base-model concurrent quota (429 RESOURCE_EXHAUSTED). We cap the
    # number of in-flight submissions (MAX_CONCURRENT_VEO_SUBMITS) so the rest
    # queue; combined with the 429-retry inside veo_generate this keeps the
    # submission rate under the ceiling.
    scene_coros = [_kick_scene(i) for i in range(scene_count)]
    intro_coro = (
        _kick_bookend(intro_prompt, intro_card_path, VEO_DURATION_BOOKEND)
        if intro_card_path is not None
        else None
    )
    outro_coro = (
        _kick_bookend(outro_prompt, outro_card_path, VEO_DURATION_BOOKEND)
        if outro_card_path is not None
        else None
    )

    all_awaitables: list[Any] = list(scene_coros)
    if intro_coro is not None:
        all_awaitables.append(intro_coro)
    if outro_coro is not None:
        all_awaitables.append(outro_coro)

    try:
        results = await _run_veo_submits_bounded(all_awaitables)
    except VertexBudgetExceededError as e:
        raise HTTPException(503, f"Vertex budget exhausted: {e}")

    # ---- Unpack ------------------------------------------------------------
    scene_ops: list[Any] = results[:scene_count]
    rest = results[scene_count:]
    intro_op = rest.pop(0) if intro_coro is not None else None
    outro_op = rest.pop(0) if outro_coro is not None else None

    # ---- Persist whatever DID succeed BEFORE raising on any scene failure --
    # Otherwise GCP keeps running those Veo ops with no DB row to track them
    # (orphaned cost). On a partial-success failure, the user can hit
    # Generate videos again and we resume.
    async def _persist_partial_scenes() -> None:
        for i, op in enumerate(scene_ops):
            if isinstance(op, Exception) or not isinstance(op, dict):
                continue
            try:
                anim_id = await _insert_animation_row(
                    parent_run_id=run_id,
                    prompt_used=scene_prompts[i],
                    veo_op_name=op.get("name") or op.get("operation_name"),
                    veo_model=op.get("model") or config.veo_model,
                    duration_sec=VEO_DURATION_MAIN,
                    with_audio=True,
                    cost_usd=op.get("cost_usd"),
                    role=f"scene_{i}",
                )
                # Mirror onto the scene metadata so subsequent calls can see it.
                if i < len(scenes_meta):
                    scenes_meta[i]["animation_id"] = anim_id
            except Exception:
                log.exception(
                    "[yas-th] persist partial-success scene %d failed (op=%s)",
                    i, op.get("name") or op.get("operation_name"),
                )
        try:
            await _patch_metadata(run_id, {"scenes": scenes_meta})
        except Exception:
            log.exception("[yas-th] persist partial-success metadata patch failed")

    # Any scene exception is fatal — the reel can't render without all scenes.
    # But first persist the ones that DID launch + map quota errors to a 503.
    quota_exhausted = any(
        isinstance(op, VeoQuotaExhaustedError) for op in scene_ops
    )
    has_scene_error = any(isinstance(op, Exception) for op in scene_ops)
    if has_scene_error:
        await _persist_partial_scenes()
        failed_idx = next(
            i for i, op in enumerate(scene_ops) if isinstance(op, Exception)
        )
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

    # ---- Persist yas_animations rows + update metadata ---------------------
    fresh_scenes = list(scenes_meta)
    for i, op in enumerate(scene_ops):
        anim_id = await _insert_animation_row(
            parent_run_id=run_id,
            prompt_used=scene_prompts[i],
            veo_op_name=op.get("name") or op.get("operation_name"),
            veo_model=op.get("model") or config.veo_model,
            duration_sec=VEO_DURATION_MAIN,
            with_audio=True,
            cost_usd=op.get("cost_usd"),
            role=f"scene_{i}",
        )
        fresh_scenes[i]["animation_id"] = anim_id

    intro_anim_id = None
    outro_anim_id = None
    if intro_op:
        intro_anim_id = await _insert_animation_row(
            parent_run_id=run_id,
            prompt_used=intro_prompt,
            veo_op_name=intro_op.get("name") or intro_op.get("operation_name"),
            veo_model=intro_op.get("model") or config.veo_model,
            duration_sec=VEO_DURATION_BOOKEND,
            with_audio=False,
            cost_usd=intro_op.get("cost_usd"),
            role="intro",
        )
    if outro_op:
        outro_anim_id = await _insert_animation_row(
            parent_run_id=run_id,
            prompt_used=outro_prompt,
            veo_op_name=outro_op.get("name") or outro_op.get("operation_name"),
            veo_model=outro_op.get("model") or config.veo_model,
            duration_sec=VEO_DURATION_BOOKEND,
            with_audio=False,
            cost_usd=outro_op.get("cost_usd"),
            role="outro",
        )

    await _patch_metadata(run_id, {
        "scenes": fresh_scenes,
        "intro_animation_id": intro_anim_id,
        "outro_animation_id": outro_anim_id,
        "composition_status": COMP_QUEUED,
        "composition_template": "talking-head-multi-scene",
    })

    log.info(
        "[yas-th] start_videos_multi run=%d scenes=%d anim_ids=%s intro=%s outro=%s",
        run_id, scene_count,
        [s.get("animation_id") for s in fresh_scenes],
        intro_anim_id, outro_anim_id,
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
    topic = run["topic"]

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
        packed: list[Path] = [scene_keyframe]
        if char_p:
            packed.append(char_p)
        if bg_p:
            packed.append(bg_p)
        packed = packed[:3]

        scene_script = dict(script)
        scene_script["dialogue"] = sm.get("dialogue") or ""
        scene_script["hook"] = sm.get("hook") or ""
        base_prompt = th_prompts.build_main_veo_prompt(
            script=scene_script,
            brand_req=brand_req,
            has_character_ref=char_p is not None,
            has_bg_ref=bg_p is not None,
            dialogue_language=script.get("dialogue_language") or "en",
        )
        full_prompt = (
            th_prompts.append_feedback_to_prompt(base_prompt, feedback_text)
            if feedback_text
            else base_prompt
        )
        negative = th_prompts.build_talking_head_negative_prompt(brand_req)
        try:
            op = await veo_generate(
                full_prompt,
                model=config.veo_model,
                duration_sec=VEO_DURATION_MAIN,
                aspect_ratio=VEO_ASPECT_RATIO,
                with_audio=True,
                reference_images=packed,
                negative_prompt=negative,
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
        )
        scenes_meta[scene_idx]["animation_id"] = new_anim_id
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
        main_refs: list[Path] = [main_path]
        if character_path:
            main_refs.append(character_path)
        if bg_path:
            main_refs.append(bg_path)
        main_refs = main_refs[:3]
        base_prompt = th_prompts.build_main_veo_prompt(
            script=script,
            brand_req=brand_req,
            has_character_ref=character_path is not None,
            has_bg_ref=bg_path is not None,
            dialogue_language=script.get("dialogue_language") or "en",
        )
        full_prompt = (
            th_prompts.append_feedback_to_prompt(base_prompt, feedback_text)
            if feedback_text
            else base_prompt
        )
        negative = th_prompts.build_talking_head_negative_prompt(brand_req)
        try:
            op = await veo_generate(
                full_prompt,
                model=config.veo_model,
                duration_sec=VEO_DURATION_MAIN,
                aspect_ratio=VEO_ASPECT_RATIO,
                with_audio=True,
                reference_images=main_refs,
                negative_prompt=negative,
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
        )
        await _patch_metadata(run_id, {
            "main_animation_id": new_anim_id,
            "veo_animation_id": new_anim_id,
            "composition_status": COMP_QUEUED,
        })
        log.info("[yas-th] retry_video main run=%d new_anim=%d", run_id, new_anim_id)
        return await fetch_run(run_id)

    # ---- Bookend retry (intro / outro) -------------------------------------
    # Use the auto-generated card as the keyframe (matches Fix 4 inversion).
    if which == "intro":
        card_path = await _resolve_intro_card_path(run_id=run_id, md=md, refs=refs)
    else:
        card_path = await _resolve_outro_card_path(run_id=run_id, md=md, refs=refs)
    if card_path is None:
        raise HTTPException(
            409,
            f"No {which} card image available — regenerate the preview first.",
        )

    # Reuse the existing animation style (vision-picked at first kickoff).
    # User feedback can override the style inline via natural language; we
    # keep the original style here so the retry stays consistent unless the
    # feedback explicitly says otherwise.
    style_key = f"{which}_animation_style"
    style = md.get(style_key) or ("fade_in" if which == "intro" else "fade_hold")

    if which == "intro":
        base_prompt = th_prompts.build_intro_veo_prompt(
            topic=topic, script=script, style=style,
        )
    else:
        base_prompt = th_prompts.build_outro_veo_prompt(
            topic=topic, script=script, style=style,
        )
    full_prompt = (
        th_prompts.append_feedback_to_prompt(base_prompt, feedback_text)
        if feedback_text
        else base_prompt
    )

    try:
        op = await veo_generate(
            full_prompt,
            model=config.veo_model,
            duration_sec=VEO_DURATION_BOOKEND,
            aspect_ratio=VEO_ASPECT_RATIO,
            with_audio=False,
            start_image=card_path,
            reference_images=None,
        )
    except VertexBudgetExceededError as e:
        raise HTTPException(503, f"Vertex budget exhausted: {e}")
    except Exception as e:
        log.exception("Veo %s retry kickoff failed for run #%d", which, run_id)
        raise HTTPException(502, f"{which.capitalize()} video retry failed: {str(e)[:240]}")

    new_anim_id = await _insert_animation_row(
        parent_run_id=run_id,
        prompt_used=full_prompt,
        veo_op_name=op.get("name") or op.get("operation_name"),
        veo_model=op.get("model") or config.veo_model,
        duration_sec=VEO_DURATION_BOOKEND,
        with_audio=False,
        cost_usd=op.get("cost_usd"),
        role=which,
    )
    patch_key = f"{which}_animation_id"
    await _patch_metadata(run_id, {
        patch_key: new_anim_id,
        "composition_status": COMP_QUEUED,
    })
    log.info(
        "[yas-th] retry_video %s run=%d new_anim=%d style=%s",
        which, run_id, new_anim_id, style,
    )
    return await fetch_run(run_id)


# ---------------------------------------------------------------------------
# Step 4b — Stitch composition (user-triggered after Veo previews look good)
# ---------------------------------------------------------------------------

VIDEOS_READY = "videos_ready"


async def stitch_composition(run_id: int) -> dict:
    """User-triggered composition kickoff. Only valid when all queued Veos
    are ready (composition_status == 'videos_ready').

    The actual Remotion job submission happens in the
    yas_th_composition_poller — this endpoint just flips a flag the poller
    watches for. We do this server-side instead of letting the poller fire
    automatically so the user gets a preview-before-stitch checkpoint.
    """
    run = await _fetch_run_row(run_id)
    md = run["metadata"] or {}
    if md.get("composition_status") != VIDEOS_READY:
        raise HTTPException(
            409,
            "Videos are not all ready yet — wait for the previews to land.",
        )
    # Flip to `rendering` with no render_job_id yet — the poller picks this
    # up on the next tick, submits the Remotion job, and records the job id.
    await _patch_metadata(run_id, {"composition_status": COMP_RENDERING})
    log.info("[yas-th] stitch_composition queued for run=%d", run_id)
    return await fetch_run(run_id)


# ---------------------------------------------------------------------------
# Step 5 — Fetch run (polling target)
# ---------------------------------------------------------------------------

async def fetch_run(run_id: int) -> dict:
    """Return a serialisable dict containing the run + linked card / animation
    rows so the frontend can render the full UI in one fetch.
    """
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

    anim_id = md.get("veo_animation_id")
    animation: dict | None = None
    if anim_id:
        animation = await _fetch_animation_row(int(anim_id))

    base_url = "/api/yas-producer/talking-head"
    refs = md.get("refs") or {}

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
        return {
            "id": cid,
            "status": c.get("status"),
            "card_type": (c.get("metadata") or {}).get("card_type")
                          if isinstance(c.get("metadata"), dict) else None,
            "width": c.get("width"),
            "height": c.get("height"),
            "url": _bust(url, c),
            "error": c.get("error"),
        }

    # If the user picked a reference asset for intro/outro, surface that
    # in the same shape so the UI can render uniformly.
    intro_payload = _card_payload(intro_card)
    outro_payload = _card_payload(outro_card)
    # When the user picked a saved producer_assets row as the intro/outro
    # source (and the auto-generated card row was skipped), surface that
    # in the same payload shape so the UI can render uniformly.
    if not intro_payload:
        if refs.get("intro_upload_id"):
            intro_payload = {
                "id": None, "status": "ready", "card_type": "intro",
                "upload_id": refs["intro_upload_id"],
                "url": f"/api/yas-producer/poster-human/upload/{refs.get('intro_upload_token','')}/file"
                if refs.get("intro_upload_token") else None,
                "source": "upload",
            }
        elif refs.get("intro_asset_id"):
            intro_payload = {
                "id": None, "status": "ready", "card_type": "intro",
                "asset_id": refs["intro_asset_id"],
                "url": f"/api/agents/producer/assets/{refs['intro_asset_id']}/file",
                "source": "asset",
            }
    if not outro_payload:
        if refs.get("outro_upload_id"):
            outro_payload = {
                "id": None, "status": "ready", "card_type": "outro",
                "upload_id": refs["outro_upload_id"],
                "url": f"/api/yas-producer/poster-human/upload/{refs.get('outro_upload_token','')}/file"
                if refs.get("outro_upload_token") else None,
                "source": "upload",
            }
        elif refs.get("outro_asset_id"):
            outro_payload = {
                "id": None, "status": "ready", "card_type": "outro",
                "asset_id": refs["outro_asset_id"],
                "url": f"/api/agents/producer/assets/{refs['outro_asset_id']}/file",
                "source": "asset",
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
                f"{base_url}/{run['id']}/video/{video_route}"
                if a.get("status") == STATUS_READY and a.get("local_path")
                else None
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

    # ----- v6 — unified scenes array ------------------------------------
    # ALWAYS emitted. For single-scene runs (default), the scenes array
    # is length 1 and synthesized from the legacy main + main_animation
    # fields. For multi-scene runs, it comes from metadata.scenes with
    # each entry's keyframe + animation hydrated.
    scenes_payload: list[dict] = []
    scene_count_md = int(md.get("scene_count") or 1)
    if scene_count_md > 1 and (md.get("scenes") or []):
        for i, sm in enumerate(md["scenes"]):
            # Keyframe URL: scene 0 = run row, scenes 1..N-1 = scene rows.
            keyframe_url: str | None
            if i == 0:
                keyframe_url = main_url
            else:
                kid = sm.get("keyframe_id")
                if kid:
                    krow = await _fetch_scene_row(int(kid))
                    if krow and krow.get("local_path"):
                        keyframe_url = _bust(
                            f"{base_url}/scenes/{kid}/file", krow,
                        )
                    else:
                        keyframe_url = None
                else:
                    keyframe_url = None
            # Animation hydration.
            anim_row: dict | None = None
            aid = sm.get("animation_id")
            if aid:
                anim_row = await _fetch_animation_row(int(aid))
            scenes_payload.append({
                "index": i,
                "dialogue": sm.get("dialogue") or "",
                "hook": sm.get("hook") or "",
                "user_authored": bool(sm.get("user_authored")),
                "keyframe_url": keyframe_url,
                "animation": _anim_payload(anim_row, video_route=f"scene_{i}"),
                "character_asset_id": sm.get("character_asset_id"),
                "character_upload_id": sm.get("character_upload_id"),
                "bg_asset_id": sm.get("bg_asset_id"),
                "bg_upload_id": sm.get("bg_upload_id"),
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
        scenes_payload.append({
            "index": 0,
            "dialogue": legacy_dlg,
            "hook": legacy_hook,
            "user_authored": False,
            "keyframe_url": main_url,
            "animation": _anim_payload(main_anim, video_route="main"),
            "character_asset_id": None,
            "character_upload_id": None,
            "bg_asset_id": None,
            "bg_upload_id": None,
            "error": md.get("main_error"),
        })

    return {
        "id": run["id"],
        "topic": run["topic"],
        "status": run["status"],
        "image_model": run.get("image_model"),
        "image_models": image_models,
        "lock_character": bool(md.get("lock_character")),
        "script": md.get("script"),
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
        "composition_status": md.get("composition_status"),
        "composition_url": (
            f"/api/yas-producer/talking-head/{run['id']}/composition/file"
            if md.get("composition_status") == COMP_READY and md.get("composition_path")
            else None
        ),
        "saved_asset_id": run.get("saved_asset_id"),
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
    run = await _fetch_run_row(run_id)
    md = run["metadata"] or {}
    if md.get("composition_status") != COMP_READY:
        raise HTTPException(409, "Composition is not ready yet.")
    comp_rel = md.get("composition_path")
    if not comp_rel:
        raise HTTPException(410, "Composition file path missing.")
    comp_path = _abs_from_rel(comp_rel)
    if not comp_path.exists():
        raise HTTPException(410, "Composition file missing on disk.")

    raw = comp_path.read_bytes()
    script = md.get("script") or {}
    topic = run.get("topic") or "Talking head"

    slug_base = (display_name or topic or "talking-head")[:48]
    # ASCII only: the slug becomes the bucket key (an Arabic topic would not
    # survive the /media proxy's signature check).
    slug = asset_storage.ascii_slug(slug_base, fallback="talking_head")
    slug = f"{slug}_{secrets.token_hex(3)}"
    bucket_path = f"video_talking_head/{slug}.mp4"

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
            log.exception("embedding failed for talking-head save")

    metadata_for_asset = {
        "content_category": "video_talking_head",
        "source": "yas_talking_head",
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

            await cur.execute(
                """
                UPDATE yas_generations
                   SET status = %s,
                       saved_asset_id = %s,
                       updated_at = NOW()
                 WHERE id = %s
                """,
                (STATUS_SAVED, asset_id, run_id),
            )

    log.info("[yas-th] saved run #%d → producer_assets #%d", run_id, asset_id)
    return {"id": asset_id, "yas_generation_id": run_id}


# ---------------------------------------------------------------------------
# Step 7 — Recent (rehydration)
# ---------------------------------------------------------------------------

async def list_recent(limit: int = 20) -> list[dict]:
    """Return talking-head runs from the last 24h that aren't saved-to-assets,
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
    return [await fetch_run(int(r["id"])) for r in rows]
