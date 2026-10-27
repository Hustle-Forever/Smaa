"""The two-stage poster generation pipeline, as a service.

Stage 1: OpenAI gpt-4o expands a topic into N radically different scene
concepts (JSON). Stage 2: Gemini `gemini-3-pro-image-preview` renders each one,
with the 42 logomark and any style/background references sent as ordered image
parts. Each render is its own `yas_poster_generations` row; siblings of one
batch share a `variant_set_id` and split the batch cost.

This code used to live inline in `routers/yas_producer.py`. It moved here
unchanged so NON-request callers can drive it too — a worker cannot import a
router (`main.py` imports both; that is an import cycle). The router still owns
every HTTP concern and simply re-binds these names, so its call sites and
behaviour are untouched.

Five features ride this pipeline: Create Poster, Human Character, the
Create-Animation frame, Poster + Human (partially), and Talking Head v2's
intro/outro bookend cards. The seams that make that work are `PosterStyle`
(which swaps the Stage-1 and Stage-2 system prompts) and `render_directive`
(which carries the aspect ratio, because the model has no aspect parameter).

`HTTPException` is raised from here on hard failure. That is deliberate and
matches the rest of the codebase (`yas_talking_head_v2` does the same) — it is
a plain exception, so a worker can catch it and branch on `.status_code`:
429 means the hourly cap is full (defer, do not burn a retry), 503 means a
provider budget is exhausted, 502/504 are transient.
"""
from __future__ import annotations

import asyncio
import json
import logging
from io import BytesIO
from pathlib import Path
from typing import Any, Optional

from fastapi import HTTPException

from ..config import config
from . import asset_storage
from . import yas_local_store as ystore
from .openai_client import BudgetExceededError as OpenAIBudgetExceededError
from .openai_client import chat as openai_chat
from .openai_client import get_meter as openai_meter
from .poster_prompts import (
    BACKGROUND_REF_INSTRUCTION,
    LOGO_INSTRUCTION,
    STYLE_REF_INSTRUCTION,
    VARIATION_REF_INSTRUCTION,
    build_concepts_user_message,
    dedupe_concepts,
    parse_concepts,
)
from .poster_styles import PosterStyle
from .vertex_client import (
    VertexBudgetExceededError,
    gemini_image_multipart,
    gemini_text,
    get_meter,
)
from . import gen_errors

log = logging.getLogger("archivist.service.poster_pipeline")


# ---------------------------------------------------------------------------
# Tunables (single definition — the router imports these back)
# ---------------------------------------------------------------------------

POSTER_MODE = "style_inspired"

GEN_TIMEOUT_SEC = 120.0
GEN_TIMEOUT_GEMINI_PRO_SEC = 180.0     # Nano Banana Pro is slower than 2.5 Flash

# Image-engine choices — what the user toggles between in the UI.
ENGINE_OPENAI = "openai"
ENGINE_GEMINI_PRO = "gemini_pro"
# Nano Banana Pro. Config-driven because access is granted PER GCP PROJECT —
# on a project without it the pair 404s, and vertex_client then degrades to the
# standard image model automatically (see resolve_image_target).
GEMINI_PRO_MODEL = config.vertex_image_pro_model
GEMINI_PRO_LOCATION = config.vertex_image_pro_location  # preview models: one endpoint only

# Status constants (mirror the CHECK constraint in the migration)
STATUS_GENERATING = "generating"
STATUS_READY = "ready"
STATUS_FAILED = "failed"
STATUS_SAVED = "saved_to_assets"

POSTER_MAX_REFS = 3
POSTER_VARIANT_COUNT = 4  # Midjourney-style: every generate/explore yields 4

# Stage-1 (gpt-4o) text budget. Single rich scene description (used by the
# Create-Animation frame step) ...
POSTER_STAGE1_MAX_TOKENS = 2000
# ... and the larger budget for the Create-Poster concepts call, which returns
# N full voxel concepts as one JSON object (each a scene description + text block).
POSTER_CONCEPTS_MAX_TOKENS = 4000

# The white 42 Abu Dhabi logomark, sent as an image part in Stage 2 so the
# model replicates the real mark instead of inventing one. Lives under
# backend/data/branding/ (config.asset_root is backend/data/assets).
POSTER_LOGO_PATH = (config.asset_root.parent / "branding" / "42_logo_white.png").resolve()

# ---------------------------------------------------------------------------
# Output shape
# ---------------------------------------------------------------------------
# Create Poster offers three shapes, and a render sends BOTH the text directive
# and the API-level aspect. They are not redundant:
#   - `aspect_ratio` becomes ImageConfig(aspect_ratio=...) inside
#     `gemini_image_multipart`, and it is the ONLY thing that actually forces the
#     output shape. That function's own note is blunt about it: "a text directive
#     alone is NOT honoured by Gemini image models".
#   - the directive still earns its place, because it tells the model to COMPOSE
#     for that shape rather than to draw a square and have it stretched.
# The General AI Video still already sends both; this brings posters in line.
POSTER_ASPECTS: tuple[str, ...] = ("1:1", "9:16", "16:9")
DEFAULT_POSTER_ASPECT = "1:1"

RENDER_DIRECTIVES: dict[str, str] = {
    "1:1": "RENDER A 1:1 SQUARE IMAGE.",
    "9:16": "RENDER A 9:16 VERTICAL (tall, full-height portrait) IMAGE.",
    "16:9": "RENDER A 16:9 HORIZONTAL (wide, landscape) IMAGE.",
}

# Still a name of its own: three signatures below take it as a default argument,
# and Human Character passes its own directive through the same parameter.
DEFAULT_RENDER_DIRECTIVE = RENDER_DIRECTIVES[DEFAULT_POSTER_ASPECT]

# Width/height per offered shape, for reading a finished render back.
_ASPECT_VALUES: dict[str, float] = {
    a: int(a.split(":")[0]) / int(a.split(":")[1]) for a in POSTER_ASPECTS
}


def normalize_aspect(value: Any) -> str:
    """One of POSTER_ASPECTS, or the square default.

    NEVER raises. An unknown value here is a square poster, not a 422: the router
    validates what a user explicitly asked for, while this is the floor for
    everything that arrives second-hand (an inherited parent row, a legacy record
    with no shape at all, an older client)."""
    aspect = str(value or "").strip()
    return aspect if aspect in RENDER_DIRECTIVES else DEFAULT_POSTER_ASPECT


def render_directive_for(aspect: Any) -> str:
    """The text half of the pair. See the note above for why both halves exist."""
    return RENDER_DIRECTIVES[normalize_aspect(aspect)]


def aspect_from_dims(width: Any, height: Any) -> str:
    """The shape a finished render actually came out as, read off its pixels.

    THE PIXELS ARE THE RECORD. `yas_poster_generations` has no aspect column, and
    this deliberately does not add one: a child render (Explore / Refine / Remove
    logo) inherits its parent's shape by measuring the parent, which needs no
    migration and cannot drift from what the image really is.

    Snaps to the NEAREST offered shape, so a model that answers a 9:16 request
    with 1024x1792 is still read as 9:16."""
    try:
        w, h = float(width or 0), float(height or 0)
    except (TypeError, ValueError):
        return DEFAULT_POSTER_ASPECT
    if w <= 0 or h <= 0:
        return DEFAULT_POSTER_ASPECT
    ratio = w / h
    return min(_ASPECT_VALUES, key=lambda a: abs(ratio - _ASPECT_VALUES[a]))


# ---------------------------------------------------------------------------
# Small pure helpers
# ---------------------------------------------------------------------------

def _is_permission_denied(exc: Exception) -> bool:
    s = str(exc).lower()
    return any(t in s for t in ("permission_denied", "permission denied", "suspended", " 403 "))


async def _fail_gen(gen_id: int, rec: dict, friendly: str, technical: str) -> dict:
    """Mark one render failed with a sentence a person can act on, and put the
    raw text in the log.

    `or rec` matches what every call site did before: a failed UPDATE must still
    return the row the caller is holding, because a batch tolerates one variant
    dying and the grid still has to render a card for it."""
    log.warning("[poster] gen #%s failed: %s", gen_id, technical)
    return await ystore.update(gen_id, status=STATUS_FAILED, error=friendly[:500]) or rec


def _ext_for_image_bytes(data: bytes) -> str:
    if data[:3] == b"\xff\xd8\xff":
        return "jpg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    return "png"  # default


def _image_dims(data: bytes) -> tuple[Optional[int], Optional[int]]:
    try:
        from PIL import Image
        with Image.open(BytesIO(data)) as im:
            return im.size
    except Exception:
        return None, None


# ---------------------------------------------------------------------------
# Image-generation slots
#
# There used to be a self-imposed cap here: a rolling-hour ledger that refused
# the 21st render before any API call was made. It was removed by request.
# There is now NO local limit on how many images can be generated.
#
# The three functions stay as no-ops because a dozen call sites, the campaign
# worker and the `remaining_this_hour` field in several API responses all speak
# this vocabulary. Keeping the seam means removing the limit changed behaviour
# in exactly one place instead of rippling through the router.
#
# What still protects spend: the Vertex USD budget meter (VEO_BUDGET_USD), and
# Google's own quota, which answers 429 on its own.
# ---------------------------------------------------------------------------

# Reported as "slots left" wherever a response still carries the field. Large
# enough that no caller ever thinks it is running out.
UNCAPPED_SLOTS = 999_999


def poster_slots_remaining() -> int:
    """No cap. Always plenty."""
    return UNCAPPED_SLOTS


def poster_reserve(n: int) -> int:
    """No cap, so nothing is reserved and this never raises."""
    return UNCAPPED_SLOTS


def poster_refund(n: int) -> None:
    """No cap, so there is nothing to give back."""
    return None


# ---------------------------------------------------------------------------
# Stage 1 — concepts
# ---------------------------------------------------------------------------

async def poster_recent_summaries(limit: int = 8) -> list[str]:
    """Recent concept signatures (newest first) for cross-batch anti-repetition.

    Pulled from the local store's `concept` field — persists across restarts,
    unlike the old in-memory `_voxel_history`.
    """
    try:
        rows = await ystore.recent(
            mode=POSTER_MODE,
            within_hours=24,
            limit=limit,
            statuses=(STATUS_READY, STATUS_SAVED),
        )
    except Exception:
        return []
    return [s for r in rows if (s := (r.get("concept") or "").strip())]


# The shape Stage 1 has to come back in. Only needed for the Gemini fallback:
# OpenAI is asked with `response_format={"type":"json_object"}`, which makes the
# provider itself guarantee parseable JSON.
#
# WHY THE SCHEMA IS NOT OPTIONAL HERE. Asked in free text, Gemini returns the
# right STRUCTURE and invalid JSON — a poster prompt ends with a text block like
#
#     TEXT_LINE_1: "BUILD YOUR FUTURE"
#
# and those inner quotes come back unescaped inside the string value, so
# `json.loads` throws and `parse_concepts` returns []. Measured: 5,337
# characters of perfectly good concepts, 0 parsed. With a schema the SDK
# serialises the value properly and the quotes survive.
_GEMINI_CONCEPTS_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "concepts": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "summary": {"type": "string",
                                "description": "one line naming what makes this concept different"},
                    "prompt": {"type": "string",
                               "description": "the full scene description to render, "
                                              "including its [TEXT TO RENDER] block"},
                },
                "required": ["summary", "prompt"],
            },
        },
    },
    "required": ["concepts"],
}


async def _concepts_via_gemini(topic: str, n: int, avoid: list[str],
                               style: PosterStyle) -> str:
    """Stage 1 without OpenAI, using the SAME prompt and the SAME parser.

    Returns a JSON *string* rather than the dict it has in hand, so the reply
    goes through `parse_concepts` exactly as OpenAI's does. Re-serialising is
    cheap and it means the fallback cannot drift from the primary path in how a
    reply is read — one parser, one set of rules about what counts as a usable
    concept.

    Fast model with thinking OFF, the setting the Talking Head script fallback
    arrived at the hard way: Pro spends hidden thinking tokens out of the SAME
    output budget and truncates the JSON mid-object, and a truncated reply is
    indistinguishable from a bad one.
    """
    raw = await gemini_text(
        build_concepts_user_message(topic, n, avoid),
        system=style.concepts_system,
        json_schema=_GEMINI_CONCEPTS_SCHEMA,
        model=config.vertex_gemini_fast_model,
        temperature=0.95,
        max_tokens=POSTER_CONCEPTS_MAX_TOKENS,
        thinking_budget=0,
    )
    # gemini_text NEVER raises on unparseable output — it hands back the
    # {"_raw": …} sentinel. Both halves of this guard are load-bearing.
    if not isinstance(raw, dict) or raw.get("_raw") is not None:
        raise RuntimeError("the fallback model returned an unreadable answer")
    return json.dumps(raw)


async def poster_concepts(topic: str, n: int, style: PosterStyle) -> tuple[list[dict], float]:
    """Stage 1: ask gpt-4o for `n` RADICALLY different concepts as JSON, in the
    chosen `style` (voxel / voxel_classic / pattern / popup — each has its own concepts system).

    Dedupes, and tops up with extra calls (feeding the accumulated summaries as
    an avoid-list) if the model returned fewer than `n` distinct concepts — the
    durable form of "variant 2 must not repeat variant 1". Also seeds the
    avoid-list from recent batches so successive grids don't repeat either.

    Returns (concepts[:n], openai_cost_usd). Raises HTTPException on hard failure.
    """
    spend_before = openai_meter().spent_usd
    recent = await poster_recent_summaries()

    concepts: list[dict] = []
    attempts = 0
    # Kept so the final failure can say WHY rather than "nothing usable". It is
    # the last thing that went wrong across both providers, which is the one a
    # person can act on.
    last_error: BaseException | None = None
    used_fallback = False
    while len(concepts) < n and attempts < 3:
        attempts += 1
        avoid = recent + [c["summary"] for c in concepts]
        try:
            raw = await asyncio.wait_for(
                openai_chat(
                    build_concepts_user_message(topic, n, avoid),
                    system=style.concepts_system,
                    model="gpt-4o",
                    temperature=0.95,
                    max_tokens=POSTER_CONCEPTS_MAX_TOKENS,
                    response_format={"type": "json_object"},
                ),
                timeout=GEN_TIMEOUT_SEC,
            )
        except asyncio.TimeoutError:
            if concepts:
                break  # use what we have rather than fail the whole batch
            raw = None
            last_error = asyncio.TimeoutError("stage 1 timed out")
        except Exception as exc:
            # EVERY OpenAI failure falls through to Gemini, the spend cap
            # included. Stage 1 is just "write me some concepts as JSON" — there
            # is nothing about it only OpenAI can do — so an OpenAI billing
            # limit stopping the poster tool was a dependency nobody chose. The
            # same fallback shape as the Talking Head script generator
            # (yas_talking_head_v2:1224), for the same reason.
            raw = None
            last_error = exc
            if isinstance(exc, OpenAIBudgetExceededError):
                log.warning("[poster] OpenAI spend cap hit (%s) — falling back to Gemini", exc)
            else:
                log.warning("[poster] stage 1 via OpenAI failed (%s) — falling back to Gemini",
                            gen_errors._text(exc)[:200])

        if raw is None:
            try:
                raw = await asyncio.wait_for(
                    _concepts_via_gemini(topic, n, avoid, style), timeout=GEN_TIMEOUT_SEC
                )
                used_fallback = True
                log.info("[poster] stage 1 concepts generated via Gemini fallback")
            except Exception as exc:  # noqa: BLE001
                if concepts:
                    break  # partial batch beats no batch
                log.exception("[poster] stage 1 Gemini fallback also failed")
                last_error = exc
                raw = None

        if raw is None:
            if concepts:
                break
            continue  # both providers failed this round; `attempts` bounds it

        concepts = dedupe_concepts(concepts + parse_concepts(raw, n))

    # Only OpenAI is metered here. A Gemini fallback round bills on the Vertex
    # meter instead, so reporting its cost against OpenAI would corrupt the
    # spend figure on the dashboard — 0.0 is the honest answer for that round.
    cost = max(0.0, openai_meter().spent_usd - spend_before)
    if not concepts:
        # Both providers, three rounds, nothing usable. The user gets the reason
        # rather than "returned nothing usable", which describes our parser
        # rather than anything they can act on.
        if last_error is not None:
            friendly, technical = gen_errors.explain(last_error, what="the poster ideas")
            log.error("[poster] stage 1 exhausted: %s", technical)
            raise HTTPException(502, friendly)
        raise HTTPException(
            502,
            "The model returned nothing usable for this topic — try rewording it, "
            "or add a bit more detail.",
        )
    if used_fallback:
        log.info("[poster] stage 1 completed on the Gemini fallback")
    return concepts[:n], cost


# ---------------------------------------------------------------------------
# Stage-2 batch — render the same expanded prompt N times concurrently.
# Each render is its own local-store record; siblings share a variant_set_id.
# Cost is measured as a batch delta then split evenly across successful images
# (per-call deltas are unreliable since the N Gemini calls interleave on the
# shared process meter).
# ---------------------------------------------------------------------------

async def single_stage2(
    spec: dict,
    ref_paths: list[Path],
    image_usage: str,
    *,
    topic: str,
    reference_asset_ids: list[int],
    reference_upload_ids: list[int],
    parent_generation_id: int | None,
    image_model: str = ENGINE_GEMINI_PRO,
    style: PosterStyle,
    mode: str = POSTER_MODE,
    include_logo: bool = True,
    storage_prefix: str = "poster",
    render_directive: str = DEFAULT_RENDER_DIRECTIVE,
    aspect_ratio: str | None = None,
) -> dict:
    """Render one variant from a per-variant spec. Creates its own local record;
    never raises — failures are recorded on the record and returned.

    spec keys:
      prompt              — the scene text to render (concept prompt, or for
                            Explore the parent prompt + variation directive)
      concept             — short concept signature (persisted for anti-repeat)
      variation_ref_path  — optional Path to the CHOSEN poster (Explore anchor)
      variation_of        — optional parent id when this is an Explore child

    `style` selects the look (voxel / voxel_classic / pattern / popup, or the
    human-character
    portrait style): it swaps the Stage-2 Gemini system instruction.

    `aspect_ratio` ("1:1" / "9:16" / "16:9") forces the output shape at the Gemini
    API level. It is OPTIONAL and defaults to None, which keeps the old
    text-directive-only behaviour for the callers that have not opted in
    (Human Character, the Create-Animation frame).

    `mode` is the local-store row mode (POSTER_MODE, or "human_character").
    `include_logo` bakes the 42 logomark in (posters) — set False for human
    characters, which must carry no logo and no text. `storage_prefix` is the
    bucket folder the rendered image lands in.
    """
    expanded_prompt = (spec.get("prompt") or "").strip()
    variation_ref_path = spec.get("variation_ref_path")
    rec = await ystore.create(
        topic=topic,
        status=STATUS_GENERATING,
        mode=mode,
        image_model=image_model,
        style=style.key,
        prompt_used=expanded_prompt[:8000],
        concept=(spec.get("concept") or "")[:300],
        variation_of=spec.get("variation_of"),
        image_usage=image_usage,
        reference_asset_ids=reference_asset_ids,
        reference_upload_ids=reference_upload_ids,
        parent_generation_id=parent_generation_id,
    )
    gen_id = int(rec["id"])

    try:
        # Nano Banana (gemini-3-pro-image): logo + refs as ordered image parts.
        parts: list[tuple[str, Any]] = []
        # The 42 logomark is baked into posters; human characters carry NO logo
        # and NO text, so the logo firewall is skipped for them.
        if include_logo and POSTER_LOGO_PATH.exists():
            parts.append(("text", LOGO_INSTRUCTION))
            parts.append(("image", POSTER_LOGO_PATH))
        # Explore: anchor on the chosen poster image so the variation stays NEAR it.
        if variation_ref_path is not None and Path(variation_ref_path).exists():
            parts.append(("text", VARIATION_REF_INSTRUCTION))
            parts.append(("image", Path(variation_ref_path)))
        if ref_paths:
            parts.append((
                "text",
                BACKGROUND_REF_INSTRUCTION if image_usage == "background" else STYLE_REF_INSTRUCTION,
            ))
            for rp in ref_paths:
                parts.append(("image", rp))
        # Aspect lives in THIS render directive, not in the Stage-2 system prompt
        # — that prompt is aspect-neutral, so Create Poster (1:1), the
        # Create-Animation frame (9:16/16:9), and Human Character (user-selected)
        # all reuse it and just vary the directive.
        parts.append(("text", f"{render_directive}\n\n{expanded_prompt}"))
        image_bytes = await asyncio.wait_for(
            gemini_image_multipart(
                parts,
                system_instruction=style.gemini_stage2_system,
                model=GEMINI_PRO_MODEL,
                location=GEMINI_PRO_LOCATION,
                aspect_ratio=aspect_ratio,
            ),
            timeout=GEN_TIMEOUT_GEMINI_PRO_SEC,
        )
    # Every failure below lands on the CARD, so it is written for whoever is
    # looking at the grid; the raw text goes to `metadata.error_technical` and
    # the log, where it is still there for whoever is debugging. See
    # services/gen_errors.py for why it is a pair and not a replacement.
    except (VertexBudgetExceededError, OpenAIBudgetExceededError) as exc:
        return await _fail_gen(gen_id, rec, gen_errors.budget_message(exc), str(exc))
    except asyncio.TimeoutError as exc:
        friendly, technical = gen_errors.explain(exc, what="this poster")
        return await _fail_gen(gen_id, rec, friendly, technical)
    except Exception as exc:
        log.exception("[poster] stage 2 failed (gen #%d, engine=%s)", gen_id, image_model)
        friendly, technical = gen_errors.explain(exc, what="this poster")
        return await _fail_gen(gen_id, rec, friendly, technical)

    if not image_bytes:
        # Distinct from an exception on purpose: the call SUCCEEDED and handed
        # back nothing, which is what a silent safety refusal looks like from
        # here, and "empty image bytes" gave nobody that hint.
        return await _fail_gen(
            gen_id, rec,
            "The image model answered but sent no picture back. That usually "
            "means it quietly refused the prompt — try rewording the topic.",
            "empty image bytes from gemini_image_multipart",
        )

    ext = _ext_for_image_bytes(image_bytes)
    mime = {"png": "image/png", "jpg": "image/jpeg", "webp": "image/webp"}.get(ext, "image/png")
    width, height = _image_dims(image_bytes)
    up = await asset_storage.upload(
        data=image_bytes,
        path=f"{storage_prefix}/{gen_id}.{ext}",
        mime=mime,
        bucket=config.producer_media_bucket,
    )
    updated = await ystore.update(
        gen_id,
        status=STATUS_READY,
        mime_type=mime,
        bytes=len(image_bytes),
        width=width,
        height=height,
        storage_bucket=up.get("storage_bucket"),
        storage_path=up.get("storage_path"),
        local_path=up.get("local_path"),
    )
    log.info("[poster] gen #%d ready · %dx%d", gen_id, width or 0, height or 0)
    return updated or rec


async def run_stage2_batch(
    specs: list[dict],
    ref_paths: list[Path],
    image_usage: str,
    *,
    topic: str,
    reference_asset_ids: list[int],
    reference_upload_ids: list[int],
    parent_generation_id: int | None,
    stage1_cost_usd: float,
    image_model: str = ENGINE_GEMINI_PRO,
    style: PosterStyle,
    mode: str = POSTER_MODE,
    include_logo: bool = True,
    storage_prefix: str = "poster",
    file_route: str = "poster",
    render_directive: str = DEFAULT_RENDER_DIRECTIVE,
    aspect_ratio: str | None = None,
    sequential: bool = False,
) -> list[dict]:
    """Render one variant per spec, split cost across the successful ones, stamp a
    shared variant_set_id, and return the records (with file_url).

    By default the renders run CONCURRENTLY. Pass ``sequential=True`` to render them
    ONE AT A TIME (the poster tool does this): parallel Gemini submits trip the
    image-gen 429 (RESOURCE_EXHAUSTED) quota, so serialising trades speed for far
    fewer rate-limit failures. ``single_stage2`` never raises (a failed render
    becomes a 'failed' record), so a plain loop is safe — one bad render won't abort
    the rest, exactly like ``asyncio.gather`` without ``return_exceptions``.

    `specs` is a list of per-variant dicts (see `single_stage2`) — distinct
    concepts for an initial grid, or parent-anchored variations for Explore.
    `image_model` selects the Stage-2 engine (Nano Banana vs OpenAI gpt-image-1);
    `style` selects the look (voxel / voxel_classic / pattern / popup, or the
    human-character portrait).
    `mode` / `include_logo` / `storage_prefix` are forwarded to `single_stage2`;
    `file_route` is the URL segment used to build each record's `file_url`
    (`poster` or `human-character`)."""
    n = len(specs)
    spend_before_vertex = get_meter().spent_usd
    spend_before_openai = openai_meter().spent_usd

    def _render(spec: dict):
        return single_stage2(
            spec, ref_paths, image_usage,
            topic=topic,
            reference_asset_ids=reference_asset_ids,
            reference_upload_ids=reference_upload_ids,
            parent_generation_id=parent_generation_id,
            image_model=image_model,
            style=style,
            mode=mode,
            include_logo=include_logo,
            storage_prefix=storage_prefix,
            render_directive=render_directive,
            aspect_ratio=aspect_ratio,
        )

    if sequential:
        # One Gemini call at a time — avoids the parallel-submit 429 quota.
        recs = [await _render(spec) for spec in specs]
    else:
        recs = await asyncio.gather(*[_render(spec) for spec in specs])
    # Capture BOTH meters — Stage 2 may run on Vertex (Nano Banana) or OpenAI.
    total_render = max(0.0, (get_meter().spent_usd - spend_before_vertex)
                       + (openai_meter().spent_usd - spend_before_openai))
    ok = [r for r in recs if r.get("status") == STATUS_READY]
    per_image = (stage1_cost_usd + total_render) / max(len(ok), 1)
    variant_set_id = min((int(r["id"]) for r in recs), default=None)

    out: list[dict] = []
    for r in recs:
        gen_id = int(r["id"])
        cost = round(per_image, 6) if r.get("status") == STATUS_READY else r.get("cost_usd")
        fresh = dict(await ystore.update(gen_id, variant_set_id=variant_set_id, cost_usd=cost) or r)
        if fresh.get("status") in (STATUS_READY, STATUS_SAVED):
            fresh["file_url"] = f"/api/yas-producer/{file_route}/{gen_id}/file"
        out.append(fresh)
    log.info("[poster] batch done · %d/%d ready · engine=%s · variant_set=%s · $%.4f total",
             len(ok), n, image_model, variant_set_id, stage1_cost_usd + total_render)
    return out
