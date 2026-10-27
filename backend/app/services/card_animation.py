"""Shared "card → animation" primitives.

The standalone **Create-Animation** tool (``backend/app/routers/yas_producer.py``)
and the **Talking-Head v2 intro/outro bookends**
(``backend/app/services/yas_talking_head_v2.py``) do the SAME two things:

  1. render a branded still card with the real 42 logo baked in (Gemini), then
  2. submit that card to Veo as image-to-video (``start_image=card``).

Historically each pipeline re-implemented the glue around the shared leaf
primitives (``gemini_image_multipart`` / ``veo_generate``): the aspect-render
directive, the logo-parts assembly, the timeout-wrapped Gemini render, the Veo
i2v submission, and the model/path/negative-prompt constants were copy-pasted on
both sides (and the Veo submit was duplicated 6× inside the Talking-Head file
alone). The Talking-Head card renderer is literally documented as "Yasin's
Create-Animation technique" — it *mimicked* the animation tool rather than
*reused* it.

This module is the single home for that glue, so the bookends genuinely REUSE
the animation recipe instead of duplicating it. It deliberately owns ONLY the
shared render/submit compute and the shared constants. Everything that legitimately
differs stays caller-side:

  * Stage-1 prompt building (the animation tool's poster-concept expansion vs the
    Talking-Head card's voxel / new-vibe / text-only branches),
  * retry / concurrency policy (the Talking-Head reel bounds concurrent Veo
    submits with a semaphore; the animation tool wraps a single call in a kickoff
    timeout and an hourly rate cap),
  * storage rows + buckets + background pollers (separate tables by design),
  * the vision-picked bookend motion style, direct-video uploads, and the
    Remotion composition hand-off.

Because every helper here is a pure construction step (``build_card_i2v_coro``
returns the coroutine *un-awaited*), wiring a caller through this module is a
behaviour-preserving refactor: same call, same args, one implementation.
"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

from ..config import config
from .poster_prompts import (
    IMAGE_ONLY_RETRY_ENFORCER,
    LOGO_INSTRUCTION,
    REFINE_REF_INSTRUCTION,
    REFINE_SYSTEM,
    REMOVE_LOGO_INSTRUCTION,
    REMOVE_LOGO_REF_INSTRUCTION,
    REMOVE_LOGO_SYSTEM,
    refine_instruction,
)
from .vertex_client import GeminiNoImageError, gemini_image_multipart, veo_generate
from .yas_poster_prompts import ANIMATE_NEGATIVE_PROMPT

log = logging.getLogger("archivist.card_animation")

# --- Shared constants (previously duplicated across the two pipelines) --------

# Gemini image model used for every still card (Nano Banana Pro). The preview
# model is only served on the 'global' endpoint. Single source of truth — was
# defined identically in both yas_producer.py and yas_talking_head_v2.py.
GEMINI_PRO_MODEL = config.vertex_image_pro_model
GEMINI_PRO_LOCATION = config.vertex_image_pro_location

# The white 42 Abu Dhabi logomark, baked into a card as an image part so the
# model replicates the real mark instead of inventing one. Same file the poster
# flow uses; canonical here so the two card pipelines share one definition.
LOGO_PATH = (config.asset_root.parent / "branding" / "42_logo_white.png").resolve()

# The image-to-video negative prompt for animating a finished still card. Aliased
# from the canonical poster-prompts definition so the animation tool and the
# bookends share the EXACT string (the wording is RAI-classifier-sensitive — do
# not "tidy" it). Both pipelines previously reached this same constant via two
# different re-export hops; this is the one true name for the card path.
CARD_I2V_NEGATIVE_PROMPT = ANIMATE_NEGATIVE_PROMPT


def aspect_render_note(aspect: str, *, edge_to_edge: bool = False) -> str:
    """The text directive that reinforces the output aspect at Gemini render time.

    ``aspect`` is ``"9:16"`` or ``"16:9"`` (anything else is treated as vertical,
    matching both call sites, which validate/normalise to those two first). Pass
    ``edge_to_edge=True`` to append the ", filled edge-to-edge." clause the
    Talking-Head cards use (the standalone animation frame omits it). The exact
    strings are asserted in tests so the two pipelines stay byte-identical.
    """
    base = (
        "RENDER A 16:9 HORIZONTAL (wide, landscape) IMAGE"
        if aspect == "16:9"
        else "RENDER A 9:16 VERTICAL (tall, portrait) IMAGE"
    )
    return base + (", filled edge-to-edge." if edge_to_edge else ".")


def build_logo_parts() -> tuple[list[tuple[str, Any]], bool]:
    """The leading ``(kind, value)`` Gemini parts that bake the real 42 logo in.

    Returns ``(parts, logo_attached)``. When the logo PNG exists the parts are
    ``[("text", LOGO_INSTRUCTION), ("image", LOGO_PATH)]``; when it is missing the
    parts are empty and ``logo_attached`` is ``False`` so the caller can decide how
    to degrade — the animation tool just drops the logo with a warning, while the
    Talking-Head card swaps to a "draw the wordmark yourself" clause. That policy
    difference stays caller-side; only the parts-assembly is shared here.
    """
    if LOGO_PATH.exists():
        return [("text", LOGO_INSTRUCTION), ("image", LOGO_PATH)], True
    return [], False


async def render_card_image(
    parts: list[tuple[str, Any]],
    *,
    system_instruction: str,
    timeout: float,
    aspect_ratio: str | None = None,
) -> bytes:
    """Render a still card via ``gemini_image_multipart`` on the Nano-Banana-Pro
    model, bounded by ``timeout`` seconds. A single attempt — callers wrap their
    own retry / concurrency / HTTP-error mapping around it (the policies differ:
    the animation endpoint maps exceptions to HTTP codes; the Talking-Head card
    retries once under an image-gen semaphore).

    ``aspect_ratio`` (e.g. "9:16" / "16:9"), when given, is enforced at the
    Gemini API level via ImageConfig so the still is actually that shape. Default
    ``None`` keeps the existing text-directive-only behaviour for callers that
    don't opt in (the Animation tool). TH v2 bookends opt in: without it they
    came back 1280x800 for a 9:16 reel.
    """
    return await asyncio.wait_for(
        gemini_image_multipart(
            parts,
            system_instruction=system_instruction,
            model=GEMINI_PRO_MODEL,
            location=GEMINI_PRO_LOCATION,
            aspect_ratio=aspect_ratio,
        ),
        timeout=timeout,
    )


async def _render_edit_escalating(
    parts: list[tuple[str, Any]],
    *,
    system_instruction: str,
    timeout: float,
    what: str,
    aspect_ratio: str | None = None,
) -> bytes:
    """Run an image *edit* (de-logo / refine) and recover the "model replied with
    text instead of an image" failure that ``render_card_image`` alone can't:
    ``gemini_image_multipart`` retries the IDENTICAL request 6×, which is futile
    when the model deterministically chats back for this artwork.

    First attempt is byte-identical to the proven prompt. If it comes back as a
    NON-blocked ``GeminiNoImageError`` (text, no image), re-issue ONCE with a blunt
    "image only" contract appended to the final directive — the model's
    text-vs-image choice is prompt-sensitive, so a restructured demand recovers
    most stubborn cases. A genuine safety/policy block (``blocked=True``) is
    permanent, so it is re-raised immediately rather than wasting a second ~minute.

    ``aspect_ratio`` (opt-in, e.g. "9:16"): forces the output shape at the API
    on both attempts, through ``render_card_image``. ``None`` (the default) sends
    the exact same call as before, so Poster / Animation edits are unchanged.
    """
    shape: dict[str, Any] = {} if aspect_ratio is None else {"aspect_ratio": aspect_ratio}
    try:
        return await render_card_image(
            parts, system_instruction=system_instruction, timeout=timeout, **shape,
        )
    except GeminiNoImageError as exc:
        if exc.blocked:
            raise  # safety / policy block — escalation can't help; surface it
        log.warning(
            "[%s] model returned text, no image (finish=%s) — escalating once "
            "with an image-only directive", what, exc.finish_reason,
        )
        strong = list(parts)
        kind, value = strong[-1]
        strong[-1] = (kind, str(value) + IMAGE_ONLY_RETRY_ENFORCER)
        return await render_card_image(
            strong, system_instruction=system_instruction, timeout=timeout, **shape,
        )


async def render_delogo_image(
    src_path: Path,
    *,
    system_instruction: str = REMOVE_LOGO_SYSTEM,
    timeout: float,
    aspect_ratio: str | None = None,
) -> bytes:
    """Re-render a FINISHED still with the 42 brand mark removed — the exact
    inverse of the add-logo path (``build_logo_parts`` + ``render_card_image``).

    Feeds the artwork back to Gemini as the SOLE image part, preceded by
    ``REMOVE_LOGO_INSTRUCTION``, on the same Nano-Banana-Pro model every branded
    card is rendered with. It deliberately does **not** call ``build_logo_parts()``
    — there is no logo part on this path; the whole point is to omit it and instruct
    removal. The single source of truth for "remove the logo from an existing
    image", so Create Poster / Poster+Human / Animation frame all share one recipe.

    This is instruction-only editing (no mask, no seed in the image pipeline), so
    the rest of the frame stays very close but is NOT guaranteed pixel-identical;
    callers keep the original and let the user re-roll. Returns the edited bytes.
    """
    # Shape the request EXACTLY like the proven generate/explore calls: a reference
    # instruction, then the source image, then a FINAL "render the image" directive.
    # The request must END on a render instruction (not on the image) and frame the
    # task as image GENERATION — ending on the image, or framing it as "edit and
    # return", makes the model reply with text and no image (the "no image" failure).
    # gemini_image_multipart already retries transients patiently (@_image_retry,
    # 6 attempts incl. 429 region-failover), so this is a single call.
    parts: list[tuple[str, Any]] = [
        ("text", REMOVE_LOGO_REF_INSTRUCTION),
        ("image", src_path),
        ("text", REMOVE_LOGO_INSTRUCTION),
    ]
    return await _render_edit_escalating(
        parts, system_instruction=system_instruction, timeout=timeout, what="delogo",
        aspect_ratio=aspect_ratio,
    )


async def render_refine_image(
    src_path: Path,
    *,
    change: str,
    system_instruction: str = REFINE_SYSTEM,
    timeout: float,
    aspect_ratio: str | None = None,
) -> bytes:
    """Re-render a FINISHED still applying ONLY the user-described ``change`` ("make the
    title bigger", "change the background to blue") — the general case of the de-logo
    edit (``render_delogo_image``), which is just this with a fixed "remove the 42 mark"
    change. Single source of truth for "edit an existing image with a free-text change",
    shared by Create Poster / Animation frame refine.

    Shaped EXACTLY like the proven generate/explore/de-logo calls — a reference
    instruction, then the source image, then a FINAL render directive carrying the
    change. The request ends on the render instruction (not on the image) and is framed
    as image GENERATION, so the model returns an image rather than a text reply (the
    "no image" failure mode). NO logo part — the source image already carries its own
    branding; this path only edits what the user asked. ``gemini_image_multipart``
    retries transients patiently (@_image_retry, incl. 429 region-failover), so this is
    a single call. Instruction-only editing (no mask/seed): the rest of the frame stays
    close but is NOT pixel-identical; callers keep the original and let the user re-roll.

    ``aspect_ratio`` (opt-in): passed to ``_render_edit_escalating`` to force the
    output shape. ``None`` keeps the Poster / Animation request unchanged.
    """
    parts: list[tuple[str, Any]] = [
        ("text", REFINE_REF_INSTRUCTION),
        ("image", src_path),
        ("text", refine_instruction(change)),
    ]
    return await _render_edit_escalating(
        parts, system_instruction=system_instruction, timeout=timeout, what="refine",
        aspect_ratio=aspect_ratio,
    )


def build_card_i2v_coro(
    prompt: str,
    *,
    start_image: Path,
    aspect: str,
    duration: int,
    model: str,
    with_audio: bool = False,
    negative_prompt: str = CARD_I2V_NEGATIVE_PROMPT,
) -> Any:
    """Build — but deliberately do NOT await — the Veo image-to-video submission
    coroutine for a still card.

    Returning the coroutine un-awaited is the whole point: the Talking-Head reel
    batches several of these under a concurrency semaphore
    (``_run_veo_submits_bounded``) to dodge Veo's per-model 429 quota, while the
    animation tool wraps the single call in a kickoff ``asyncio.wait_for``. A
    shared helper that awaited internally would break the reel's concurrency cap,
    so awaiting stays the caller's job.

    ``reference_images`` is always ``None`` — a card animates itself from its own
    first frame; there is no character/face reference (that is the talking-head
    *main scene* path, which does not come through here).
    """
    return veo_generate(
        prompt,
        model=model,
        duration_sec=duration,
        aspect_ratio=aspect,
        with_audio=with_audio,
        start_image=start_image,
        reference_images=None,
        negative_prompt=negative_prompt,
    )
