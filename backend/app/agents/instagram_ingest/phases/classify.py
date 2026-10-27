"""Phase 2b — classify.

Categorises every downloaded Instagram post into one of five content types
using OpenAI GPT-4o-mini vision:

  img_poster          — static image, no realistic human face
  img_poster_human    — static image with at least one realistic human face
  img_poster_animated — video without a realistic human on camera
  video_interview     — video with real humans talking to camera or each other
  other               — doesn't clearly fit any of the above

Results are stored in instagram_posts.metadata as content_category,
content_category_confidence, and content_category_reason.

Idempotent: posts where metadata->>'content_category' is already set are
skipped.
"""
from __future__ import annotations

import asyncio
import json as _json
import logging
import tempfile
from pathlib import Path

from psycopg.types.json import Jsonb

from ....db import get_conn
from ....services.openai_client import (
    _image_to_data_url,
    get_client,
    get_meter,
    is_vision_supported,
    uses_vertex,
)
from ....services.vertex_client import gemini_multimodal
from ....services.storage import resolve_local
from ...website_ingest.phases.video import _save_frame_sync, _probe_duration
from ...website_ingest.types import CancellationError, PhaseContext, PhaseResult

log = logging.getLogger("archivist.phase.ig_classify")

# ---------------------------------------------------------------------------
# Prompt
# ---------------------------------------------------------------------------

_VALID_CATEGORIES = {
    "img_poster",
    "img_poster_human",
    "img_poster_animated",
    "video_interview",
    "other",
}

CLASSIFY_SYSTEM = (
    "You classify Instagram posts into exactly one content category. "
    "Accuracy matters — this category drives downstream UI flows that are "
    "expensive to misroute. When in doubt, choose 'other' rather than "
    "forcing a fit.\n\n"
    "The media type is provided to you (IMAGE or VIDEO).\n\n"
    "Definition of 'realistic human face': it looks like a camera photograph of "
    "a real person. This includes AI-generated faces that are photorealistic. "
    "It does NOT include drawn, illustrated, 3-D rendered, cartoon, or stylised "
    "characters — those are NOT realistic even if they depict a human.\n\n"
    "Category rules:\n\n"
    "  • IMAGE + no realistic human face  → **img_poster**\n\n"
    "  • IMAGE + designed/styled poster that features a realistic human face "
    "(e.g. promotional graphic, quote card with a person, event flyer with "
    "a photo of someone) → **img_poster_human**. A raw candid photo of a "
    "person that is NOT a designed poster does NOT belong here — put it in "
    "other.\n\n"
    "  • VIDEO + a real human is on camera SPEAKING (to the viewer or to "
    "another person on camera) with their face visible for the MAJORITY of "
    "the runtime, and their mouth moving in sync with the audio — i.e. the "
    "voice belongs to the person on screen, not a voice-over narrator. → "
    "**video_interview**.\n"
    "    REJECT video_interview (route to img_poster_animated or other) when:\n"
    "      ‑ the dominant content is animated graphics, kinetic typography, "
    "or motion design with a voice-over track;\n"
    "      ‑ the speaker only appears in the first or last few seconds (an "
    "intro greeting or sign-off, not the body of the video);\n"
    "      ‑ no face is visible for most of the runtime (b-roll-dominant, "
    "montage, product reel, slideshow of stills);\n"
    "      ‑ the speaker's face is consistently obscured (back-of-head, "
    "masks, hard backlight, heavy filters obscuring features).\n"
    "    A brief title card or end card framing a real talking-head body is "
    "fine. The test: **strip the graphics — is the remaining footage a "
    "person on camera speaking? If no, it's NOT video_interview.**\n\n"
    "  • VIDEO that does NOT meet the video_interview bar  → "
    "**img_poster_animated**. This includes animated graphics, kinetic "
    "typography, motion design with voice-over, product reels, montages, "
    "b-roll over music, slideshows of stills, and anything that's not a "
    "person speaking on camera most of the time.\n\n"
    "  • Anything that doesn't clearly fit the above  → **other**.\n\n"
    "Tie-breaker: if you cannot confidently call a video a talking-head "
    "interview, prefer img_poster_animated over video_interview. The "
    "downstream extraction flow treats them very differently and a "
    "misrouted video_interview wastes the user's time.\n\n"
    "Respond with ONLY a JSON object: "
    '{\"category\": \"...\", \"confidence\": 0.0, \"reason\": \"...\"}\n'
    "category must be one of: img_poster, img_poster_human, img_poster_animated, "
    "video_interview, other.\n"
    "confidence is 0.0–1.0.\n"
    "reason is one sentence."
)

# How many frames to extract from videos for classification
_VIDEO_FRAME_COUNT = 4

# Seconds to wait between each API call
_DELAY_BETWEEN_CALLS = 1.0

# Max posts to classify per run. Set to 0 for unlimited.
_BATCH_LIMIT = 0


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

async def _pending_posts(*, force_reclassify: bool = False) -> list[dict]:
    """Posts with downloaded assets that need classification.

    By default, skips posts whose metadata.content_category is already set
    (idempotent re-runs). When `force_reclassify=True`, returns every post
    with a downloaded asset — used by the "Re-classify existing posts" Sync
    option to redo classifications after the prompt is tightened.

    For carousels, picks the first child (lowest child_index).
    DISTINCT ON prevents duplicate rows for multi-asset carousels.

    Also returns the post's `metadata->'video_analysis'` JSON when it
    exists; the classifier uses these deeper signals as disqualifying
    evidence for video_interview.
    """
    skip_already_classified = (
        "AND COALESCE(ip.metadata->>'content_category', '') = ''"
        if not force_reclassify
        else ""
    )
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                f"""
                WITH first_asset AS (
                  SELECT DISTINCT ON (ip.id)
                         ip.id          AS post_id,
                         ip.media_type  AS media_type,
                         ip.metadata    AS post_metadata,
                         a.id           AS asset_id,
                         a.asset_type   AS asset_type,
                         a.local_path   AS local_path
                    FROM instagram_posts ip
                    JOIN instagram_post_assets ipa ON ipa.post_id = ip.id
                    JOIN assets a ON a.id = ipa.asset_id
                   WHERE a.download_status = 'downloaded'
                     AND a.local_path IS NOT NULL
                     {skip_already_classified}
                   ORDER BY ip.id, ipa.child_index
                )
                SELECT fa.post_id,
                       fa.media_type,
                       fa.post_metadata,
                       fa.asset_id,
                       fa.asset_type,
                       fa.local_path
                  FROM first_asset fa
                 ORDER BY fa.post_id
                """
            )
            return await cur.fetchall()


def _format_deepen_block(post_metadata: dict | None) -> str:
    """Build the 'Prior deep analysis' prompt block from video_deepen output.

    Only fires when `metadata.video_analysis` is populated (the post has
    already been through the deepen phase). Surfaces signals that strongly
    disqualify a video_interview classification when they're missing:
    no on-camera people, hook_type=text_on_screen, no speech, etc.
    """
    if not post_metadata or not isinstance(post_metadata, dict):
        return ""
    va = post_metadata.get("video_analysis")
    if not isinstance(va, dict) or not va:
        return ""

    lines: list[str] = []
    audio = va.get("audio_profile") or {}
    if isinstance(audio, dict):
        has_speech = audio.get("has_speech")
        energy = audio.get("energy")
        if has_speech is not None:
            lines.append(f"audio.has_speech: {bool(has_speech)}")
        if energy:
            lines.append(f"audio.energy: {energy}")

    hook = va.get("hook_type")
    if hook:
        lines.append(f"hook_type: {hook}")
    pacing = va.get("pacing")
    if pacing:
        lines.append(f"pacing: {pacing}")

    brand = va.get("brand_presence") or {}
    if isinstance(brand, dict):
        people = brand.get("people_described") or []
        if isinstance(people, list):
            lines.append(f"people_described_count: {len(people)}")

    on_screen_text = va.get("on_screen_text") or []
    if isinstance(on_screen_text, list):
        lines.append(f"on_screen_text_items: {len(on_screen_text)}")

    if not lines:
        return ""

    return (
        "\n\nPrior deep analysis (use as DISQUALIFYING signals — "
        "e.g. hook_type=text_on_screen + people_described_count=0 means "
        "this is NOT video_interview):\n"
        + "\n".join(f"  - {ln}" for ln in lines)
    )


async def _classify_call(text_prompt: str, image_paths: list[Path]) -> str:
    """One answer from whichever provider `LLM_PROVIDER` names.

    Gemini gets the frames as bytes and the rules as the system instruction,
    on the fast model with thinking off: this is a short label, and five of
    these run side by side. The OpenAI branch is the call that was always
    here, kept for the day the switch goes back.
    """
    from ....config import config

    if uses_vertex():
        out = await gemini_multimodal(
            text_prompt, images=list(image_paths), system=CLASSIFY_SYSTEM,
            model=config.vertex_gemini_fast_model, temperature=0.2,
            max_tokens=600, thinking_budget=0,
        )
        return str(out or "").strip()

    content: list[dict] = [{"type": "text", "text": text_prompt}]
    for img_path in image_paths:
        content.append({
            "type": "image_url",
            "image_url": {"url": _image_to_data_url(img_path), "detail": "low"},
        })
    client = get_client()
    resp = await client.chat.completions.create(
        model=config.openai_model_vision,
        messages=[
            {"role": "system", "content": CLASSIFY_SYSTEM},
            {"role": "user", "content": content},
        ],
        max_completion_tokens=300,
        temperature=0.2,
    )
    if resp.usage:
        cost = get_meter().estimate_text(
            config.openai_model_vision,
            resp.usage.prompt_tokens,
            resp.usage.completion_tokens,
        )
        get_meter().add(config.openai_model_vision, cost)
    return (resp.choices[0].message.content or "").strip()


async def _persist_classification(
    *, post_id: int, category: str, confidence: float, reason: str,
) -> None:
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                UPDATE instagram_posts
                   SET metadata = metadata || %s::jsonb
                 WHERE id = %s
                """,
                (
                    Jsonb({
                        "content_category": category,
                        "content_category_confidence": confidence,
                        "content_category_reason": reason,
                    }),
                    post_id,
                ),
            )


# ---------------------------------------------------------------------------
# Frame extraction for videos
# ---------------------------------------------------------------------------

def _extract_frames_sync(video_path: Path, n_frames: int = 4) -> list[Path]:
    """Extract n evenly-spaced frames from a video to a temp directory.
    Returns list of frame paths (may be fewer than n if video is very short)."""
    duration = _probe_duration(video_path)
    if not duration or duration < 0.5:
        return []

    # Evenly space frames, avoiding the very start/end
    margin = min(0.5, duration * 0.05)
    usable = duration - 2 * margin
    if usable <= 0:
        timestamps = [duration / 2.0]
    elif n_frames == 1:
        timestamps = [duration / 2.0]
    else:
        step = usable / (n_frames - 1)
        timestamps = [margin + i * step for i in range(n_frames)]

    tmp_dir = Path(tempfile.mkdtemp(prefix="ig_classify_"))
    frames: list[Path] = []
    for i, ts in enumerate(timestamps):
        dest = tmp_dir / f"frame_{i:02d}.jpg"
        try:
            _save_frame_sync(video_path, ts, dest)
            if dest.exists() and dest.stat().st_size > 0:
                frames.append(dest)
        except Exception:
            log.debug("Frame extract failed at %.1fs for %s", ts, video_path)
    return frames


# ---------------------------------------------------------------------------
# Per-post classification
# ---------------------------------------------------------------------------

def _is_video_type(post: dict) -> bool:
    """Determine if this post should be classified as video content."""
    media_type = (post.get("media_type") or "").upper()
    asset_type = (post.get("asset_type") or "").lower()
    if media_type == "VIDEO":
        return True
    if asset_type == "instagram":
        return True
    return False


async def _classify_post(post: dict) -> tuple[str, float, str] | None:
    """Classify a single post via OpenAI vision. Returns (category, confidence, reason) or None."""
    post_id = int(post["post_id"])
    local_path = resolve_local(post["local_path"])

    if not local_path.exists():
        log.warning("post #%d: file missing at %s", post_id, local_path)
        return None

    is_video = _is_video_type(post)
    media_label = "VIDEO" if is_video else "IMAGE"

    # Prepare image paths
    image_paths: list[Path] = []
    temp_frames: list[Path] = []

    if is_video:
        temp_frames = await asyncio.to_thread(
            _extract_frames_sync, local_path, _VIDEO_FRAME_COUNT,
        )
        if not temp_frames:
            log.warning("post #%d: could not extract frames from video", post_id)
            return None
        image_paths = temp_frames
    else:
        image_paths = [local_path]

    # Build the question: text prompt + image(s).
    # If video_deepen has already populated structural signals on this post,
    # surface them as additional disqualifying evidence — they catch failure
    # modes that 4 still frames don't (voice-over over graphics, montages
    # with a brief on-camera intro, etc.).
    deepen_block = _format_deepen_block(post.get("post_metadata"))
    text_prompt = (
        f"Media type: {media_label}\n\n"
        f"Classify this {'video (you are shown ' + str(len(image_paths)) + ' evenly-spaced frames)' if is_video else 'image'}. "
        f"Determine the content category based on the rules provided."
        f"{deepen_block}"
    )

    try:
        usable = [p for p in image_paths if is_vision_supported(p)]
        for p in image_paths:
            if p not in usable:
                log.debug("post #%d: unsupported image format %s", post_id, p.name)
        if not usable:
            log.warning("post #%d: no valid images to send", post_id)
            return None
        raw_text = await _classify_call(text_prompt, usable)
    except Exception:
        log.exception("classify call failed for post #%d", post_id)
        return None
    finally:
        for f in temp_frames:
            try:
                f.unlink(missing_ok=True)
            except Exception:
                pass
        if temp_frames:
            try:
                temp_frames[0].parent.rmdir()
            except Exception:
                pass

    # Parse JSON from response
    try:
        # Strip code fences if present
        text = raw_text
        if text.startswith("```"):
            text = text.split("```", 2)[1]
            if text.startswith("json"):
                text = text[4:]
            text = text.strip()
        result = _json.loads(text)
    except _json.JSONDecodeError:
        log.warning("post #%d: couldn't parse JSON from response: %s", post_id, raw_text[:200])
        return None

    category = (result.get("category") or "").strip().lower()
    confidence = float(result.get("confidence") or 0.0)
    reason = (result.get("reason") or "").strip()

    # Validate category
    if category not in _VALID_CATEGORIES:
        log.warning(
            "post #%d: model returned invalid category %r, mapping to other",
            post_id, category,
        )
        category = "other"

    # Sanity check: image posts can't be video categories and vice versa.
    # When the cross-modality category is wrong, do the safe coercion (no
    # face → no face, face → unknown). DON'T silently promote a "designed
    # human poster" verdict into "talking-head interview" — that's a likely
    # source of false positives and the right answer is "we don't know".
    if is_video and category == "img_poster":
        category = "img_poster_animated"
        reason = f"(corrected from image category) {reason}"
    elif is_video and category == "img_poster_human":
        # The model saw a face on a designed graphic but this is a video.
        # Could be a video with a brief talking-head intro, could be a
        # poster animation. Route to "other" so it doesn't leak into the
        # Talking-head extraction flow.
        category = "other"
        reason = (
            f"(silent-corrected from img_poster_human → other; "
            f"needs manual review) {reason}"
        )
    elif not is_video and category == "video_interview":
        category = "img_poster_human"
        reason = f"(corrected from video category) {reason}"
    elif not is_video and category == "img_poster_animated":
        category = "img_poster"
        reason = f"(corrected from video category) {reason}"

    return category, confidence, reason


# ---------------------------------------------------------------------------
# Phase entry point
# ---------------------------------------------------------------------------

async def run(ctx: PhaseContext) -> PhaseResult:
    # The Sync panel's "Re-classify existing posts" toggle threads through
    # the agent and lands here as PhaseContext.metadata["force_reclassify"].
    # When set, we drop the "skip already-classified" gate so the tightened
    # prompt (and any newly-available video_deepen signals) can overwrite
    # bad existing categories.
    force_reclassify = bool((ctx.metadata or {}).get("force_reclassify"))
    pending = await _pending_posts(force_reclassify=force_reclassify)
    if not pending:
        log.info("[run=%s] classify: nothing pending", ctx.run_id)
        return PhaseResult(name="classify", total=0)

    # Cap to batch limit if set
    if _BATCH_LIMIT > 0 and len(pending) > _BATCH_LIMIT:
        log.info(
            "[run=%s] classify: %d posts pending, capping to %d this run",
            ctx.run_id, len(pending), _BATCH_LIMIT,
        )
        pending = pending[:_BATCH_LIMIT]
    else:
        mode = "re-classify" if force_reclassify else "fresh"
        log.info(
            "[run=%s] classify (%s): %d posts to classify",
            ctx.run_id, mode, len(pending),
        )

    processed = 0
    failed = 0
    skipped = 0

    for post in pending:
        if ctx.is_cancelled and ctx.is_cancelled():
            raise CancellationError("classify cancelled mid-batch")

        post_id = int(post["post_id"])
        try:
            result = await _classify_post(post)
            if result is None:
                failed += 1
            else:
                category, confidence, reason = result
                await _persist_classification(
                    post_id=post_id,
                    category=category,
                    confidence=confidence,
                    reason=reason,
                )
                processed += 1
                log.debug(
                    "post #%d → %s (%.0f%%): %s",
                    post_id, category, confidence * 100, reason,
                )
        except CancellationError:
            raise
        except Exception:
            failed += 1
            log.exception("classify failed for post #%d", post_id)

        if ctx.report_progress:
            ctx.report_progress("classify", processed + failed + skipped, len(pending))

        await asyncio.sleep(_DELAY_BETWEEN_CALLS)

    log.info(
        "[run=%s] classify done: %d ok, %d failed, %d skipped",
        ctx.run_id, processed, failed, skipped,
    )
    return PhaseResult(
        name="classify",
        processed=processed,
        failed=failed,
        skipped=skipped,
        total=len(pending),
    )
