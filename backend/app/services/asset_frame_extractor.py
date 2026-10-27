"""On-demand frame extractor for IG videos → Asset library candidates.

Triggered by the "+ Add to Asset" button on PostDetail when the IG post's
content_category is a video category (img_poster_animated, video_interview).
Returns candidate frames so the user can pick which to import.

Two modes:

  talking_head     — detect every unique person, pick the cleanest portrait
                     frame per person. Result is per-person.

  animated_poster  — find the frame where the full poster is rendered
                     (no fade, no transition). Result is ranked candidates.

Pipeline (shared):
  1. Resolve the video file on disk via instagram_post_assets ⋈ assets.
  2. Build a candidate-timestamp set: existing video_frames + mode-specific
     augmentation. Cap at 12 frames.
  3. Materialise each frame to disk via ffmpeg (parallel via to_thread).
  4. Downscale each frame to max 768px long-edge before sending to Gemini.
  5. Budget guard via VertexCostMeter (>=95% of cap → BudgetGuardError).
  6. Single batched Gemini 2.5 Flash multimodal call with JSON schema.
     Wrapped in asyncio.wait_for(45s).
  7. Parse + rank into the typed result.

Notes:
  - Frame indices stored in video_frames start at 100 in video_deepen.
    We use 200+ to avoid collisions with deepen's 100-199 range.
  - We always persist newly-extracted frames to video_frames so the same
    asset_id can be re-imported later without redoing the ffmpeg work.
  - Original full-res frames stay on disk; only the downscaled copy is
    sent to Gemini (saves token budget on a 12-image call).
"""
from __future__ import annotations

import asyncio
import logging
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from ..config import config
from ..db import get_conn
from ..agents.instagram_ingest.phases.video_deepen import (
    _existing_frames,
    _insert_new_frame,
    _transcript_segments,
)
from ..agents.website_ingest.phases.video import (
    _probe_duration,
    _save_frame_sync,
)
from . import asset_frame_prompts as prompts
from .storage import frames_dir, relativise, resolve_local
from .vertex_client import (
    VertexBudgetExceededError,
    gemini_multimodal,
    get_meter,
)

log = logging.getLogger("archivist.asset_frame_extractor")


# Tunables — kept here, not in config.py, so they don't bloat the env.
MAX_FRAMES_TO_GEMINI = 12
INTRO_OUTRO_FRAMES = 6          # narrower window, fewer samples
DEDUPE_WINDOW_SEC = 1.5
INTRO_OUTRO_DEDUPE_SEC = 0.4    # samples in narrow windows are close together
FRAME_DOWNSCALE_LONG_EDGE_PX = 768
GEMINI_TIMEOUT_SEC = 45.0
BUDGET_RESERVATION_PCT = 0.95   # refuse new extractions once spend ≥ 95%
EXTRACT_BASE_FRAME_INDEX = 200  # avoid collisions with video_deepen's 100+
# Intro / outro frames carve their own bands too so a single video can hold
# talking-head AND intro AND outro extractions without frame_index collisions.
EXTRACT_BASE_FRAME_INDEX_INTRO = 300
EXTRACT_BASE_FRAME_INDEX_OUTRO = 400


# ---------------------------------------------------------------------------
# Public result types
# ---------------------------------------------------------------------------

Mode = Literal["talking_head", "animated_poster", "intro", "outro"]


class BudgetGuardError(RuntimeError):
    """Raised when Vertex spend is too close to the cap to safely extract."""


class VideoNotFoundError(RuntimeError):
    """The post/asset doesn't have a downloaded video file on disk."""


class GeminiTimeoutError(RuntimeError):
    """The single batched Gemini call exceeded GEMINI_TIMEOUT_SEC."""


@dataclass
class FrameInfo:
    """A single candidate frame, persisted in video_frames."""
    frame_id: int
    timestamp_sec: float


@dataclass
class PersonCandidate:
    """Talking-head mode: one person and their best portrait frame."""
    person_label: str
    description: str
    suggested_archetype: str | None
    best_frame: FrameInfo
    score: float
    alternates: list[FrameInfo] = field(default_factory=list)


@dataclass
class FrameCandidate:
    """Animated-poster mode: a ranked candidate frame."""
    frame: FrameInfo
    rank: int
    rejected_reason: str | None = None


@dataclass
class TalkingHeadResult:
    candidates: list[PersonCandidate]
    degrade_reason: str | None  # 'no_talking_head' if has_visible_speakers=false
    dominant_content: str | None
    suggest_animated_poster: bool


@dataclass
class AnimatedPosterResult:
    candidates: list[FrameCandidate]
    confidence: str  # 'high' | 'medium' | 'low'
    rejected: list[FrameCandidate]


@dataclass
class IntroOutroFrameCandidate:
    """Intro / outro mode: a candidate text-card frame with per-axis scores."""
    frame: FrameInfo
    rank: int  # 1-based, from ordered_ranking; -1 if rejected
    text_completeness: float
    visual_dominance: float
    clarity: float
    note: str | None
    rejected_reason: str | None = None


@dataclass
class IntroOutroResult:
    candidates: list[IntroOutroFrameCandidate]
    confidence: str  # 'high' | 'medium' | 'low'
    rejected: list[IntroOutroFrameCandidate]
    degrade_reason: str | None  # 'no_text_detected' | 'zero_duration' | ...
    position: str  # 'intro' | 'outro'


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _resolve_video(post_id: int, asset_id: int) -> tuple[Path, int]:
    """Return (absolute_path_to_video, resolved_asset_id).

    Caller already validated the post + asset; we just need the file path
    and to confirm it's actually a downloaded video.
    """
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT a.id AS asset_id, a.local_path, a.mime_type
                  FROM instagram_post_assets ipa
                  JOIN assets a ON a.id = ipa.asset_id
                 WHERE ipa.post_id = %s
                   AND a.id = %s
                   AND a.download_status = 'downloaded'
                   AND a.local_path IS NOT NULL
                """,
                (post_id, asset_id),
            )
            row = await cur.fetchone()
    if not row:
        raise VideoNotFoundError(
            f"No downloaded asset #{asset_id} for post #{post_id}"
        )
    mime = (row.get("mime_type") or "").lower()
    if not mime.startswith("video/"):
        raise VideoNotFoundError(
            f"asset #{asset_id} mime '{mime}' is not a video"
        )
    video_path = resolve_local(row["local_path"])
    if not video_path.exists():
        raise VideoNotFoundError(
            f"asset #{asset_id} local file missing at {video_path}"
        )
    return video_path, int(row["asset_id"])


def _budget_guard() -> None:
    """Refuse to start an extraction when we're near the budget cap.

    The plan agent surfaced this risk: a user clicking through 50 posts
    could silently blow VEO_BUDGET_USD. Reject upfront with a clear error
    so the router can surface it.
    """
    meter = get_meter()
    if meter.over_budget(BUDGET_RESERVATION_PCT):
        raise BudgetGuardError(
            f"Vertex spend at ${meter.spent_usd:.4f} is ≥ {int(BUDGET_RESERVATION_PCT*100)}%"
            f" of VEO_BUDGET_USD=${meter.budget_usd:.2f}. Refusing extraction."
        )


def _augment_timestamps_talking_head(
    *,
    existing_ts: list[float],
    transcript_segments: list[dict],
    duration: float,
) -> list[float]:
    """Talking-head augmentation: prefer frames at speech moments."""
    ts: list[float] = list(existing_ts)
    if not transcript_segments:
        return ts
    # Midpoints of each segment with non-empty text
    for seg in transcript_segments:
        start = float(seg.get("start_sec") or 0)
        end = float(seg.get("end_sec") or start)
        if (seg.get("text") or "").strip() and end > start:
            ts.append((start + end) / 2.0)
    # ±0.3s offsets around the longest speech segment (the most likely
    # clean-frame zone — speakers usually stabilise on camera there).
    speech = [
        (float(s.get("start_sec") or 0), float(s.get("end_sec") or 0))
        for s in transcript_segments if (s.get("text") or "").strip()
    ]
    speech_with_dur = sorted(
        speech, key=lambda x: (x[1] - x[0]), reverse=True,
    )
    if speech_with_dur:
        s, e = speech_with_dur[0]
        anchor = (s + e) / 2.0
        for offset in (-0.3, 0.3):
            t = anchor + offset
            if 0 < t < duration:
                ts.append(t)
    return ts


def _augment_timestamps_animated_poster(
    *,
    existing_ts: list[float],
    duration: float,
) -> list[float]:
    """Animated-poster augmentation: skip first/last 0.5s; add 25/50/75%."""
    ts: list[float] = [t for t in existing_ts if 0.5 < t < (duration - 0.5)]
    for pct in (0.25, 0.5, 0.75):
        t = pct * duration
        if 0.5 < t < (duration - 0.5):
            ts.append(t)
    return ts


def _dedupe_timestamps(timestamps: list[float], window: float) -> list[float]:
    """Merge timestamps that fall within `window` seconds of each other."""
    sorted_ts = sorted(t for t in timestamps if t >= 0)
    out: list[float] = []
    for t in sorted_ts:
        if not out or (t - out[-1]) > window:
            out.append(t)
    return out


def _max_frames_for_duration(duration: float) -> int:
    """Pick a candidate-frame budget that scales with video length.

    Short reels rarely contain multiple speakers; long reels often do, and
    each new speaker only appears for a few seconds so the sample grid must
    be denser to catch them. Caps at 32 to stay inside Flash's effective
    multi-image budget on a single call.
    """
    if duration < 30:
        return 12
    if duration < 60:
        return 18
    if duration < 120:
        return 24
    return 32


def _dedupe_window_for_budget(budget: int) -> float:
    """Use a tighter dedupe window when we want more frames per second.

    With budget=12 on a 30s reel we have ~2.5s per frame so the default 1.5s
    window is fine. With budget=24 on a 90s reel we have ~3.75s per frame
    but want to keep speakers that appear in 1-2s windows distinguishable —
    drop to 0.8s so the dedupe stops eating adjacent-but-distinct moments.
    """
    return 0.8 if budget >= 18 else DEDUPE_WINDOW_SEC


def _max_tokens_for_frames(n_frames: int) -> int:
    """Scale Gemini's output budget with the number of frames being analysed.

    Each frame produces a row in the schema's `frames` array plus possibly
    a row per detected person — for talking-head with multi-speaker reels
    the visible JSON output grows roughly linearly with frame count.
    """
    return max(8000, n_frames * 700)


def _downsample_evenly(timestamps: list[float], cap: int) -> list[float]:
    """Reduce a list to `cap` items spread evenly across its range.

    Preserves the first and last entries (so opening and closing speakers
    are never dropped) and picks intermediate indices proportionally. Used
    when augmentation produced more candidate timestamps than the budget.
    """
    if len(timestamps) <= cap or cap <= 0:
        return list(timestamps)
    if cap == 1:
        return [timestamps[len(timestamps) // 2]]
    n = len(timestamps)
    indices = sorted({round(i * (n - 1) / (cap - 1)) for i in range(cap)})
    return [timestamps[i] for i in indices]


async def _materialize_frames(
    *,
    video_path: Path,
    asset_id: int,
    target_timestamps: list[float],
    dedupe_window_sec: float = DEDUPE_WINDOW_SEC,
    base_frame_index: int = EXTRACT_BASE_FRAME_INDEX,
) -> list[FrameInfo]:
    """For each target timestamp, return a FrameInfo (db row id + ts).

    Reuses an existing video_frames row when one is within ±dedupe_window_sec.
    Extracts new frames in parallel via asyncio.to_thread (Semaphore(4) so
    we don't spawn 12 ffmpeg processes simultaneously on a 4-core box).

    `base_frame_index` lets the caller carve its own index band — talking-head
    uses 200+, intro 300+, outro 400+. Within a band the index still walks
    forward from `max(existing) + 1` to avoid (asset_id, frame_index) collisions.
    """
    # Snapshot what's already in video_frames for reuse
    existing = await _existing_frames(asset_id)
    existing_by_ts = [
        {
            "id": int(r["id"]),
            "timestamp_sec": float(r["timestamp_sec"]),
            "local_path": r["local_path"],
        }
        for r in existing
        if r.get("local_path")
    ]
    # Next frame_index to assign for newly extracted frames
    existing_indices = [int(r["frame_index"]) for r in existing]
    next_idx = max(
        max(existing_indices, default=-1) + 1,
        base_frame_index,
    )

    sem = asyncio.Semaphore(4)
    results: list[FrameInfo] = []
    seen_frame_ids: set[int] = set()

    async def _materialise_one(target_sec: float, idx_slot: int) -> FrameInfo | None:
        # Reuse if we have a frame within the dedup window
        nonlocal next_idx  # noqa: PLW0603 — extraction loop assigns
        for r in existing_by_ts:
            if abs(r["timestamp_sec"] - target_sec) <= dedupe_window_sec:
                local = resolve_local(r["local_path"])
                if local.exists():
                    if r["id"] in seen_frame_ids:
                        return None
                    seen_frame_ids.add(r["id"])
                    return FrameInfo(
                        frame_id=r["id"], timestamp_sec=r["timestamp_sec"],
                    )

        async with sem:
            fdir = frames_dir(asset_id)
            dest = fdir / f"extract-{idx_slot:03d}.jpg"
            try:
                await asyncio.to_thread(
                    _save_frame_sync, video_path, target_sec, dest,
                )
            except Exception:
                log.exception("frame extract failed at t=%.2f", target_sec)
                return None
            try:
                frame_id = await _insert_new_frame(
                    asset_id=asset_id,
                    frame_index=idx_slot,
                    timestamp_sec=target_sec,
                    local_path=relativise(dest),
                )
            except Exception:
                log.exception("video_frames insert failed at t=%.2f", target_sec)
                return None
            if frame_id in seen_frame_ids:
                return None
            seen_frame_ids.add(frame_id)
            return FrameInfo(frame_id=frame_id, timestamp_sec=target_sec)

    # Pre-assign frame_index slots so they don't race
    slots = list(range(next_idx, next_idx + len(target_timestamps)))
    extracts = await asyncio.gather(
        *(
            _materialise_one(ts, slot)
            for ts, slot in zip(target_timestamps, slots)
        ),
        return_exceptions=False,
    )
    for f in extracts:
        if f is not None:
            results.append(f)

    # Order by timestamp for stable downstream indexing
    results.sort(key=lambda f: f.timestamp_sec)
    return results


def _downscale_frame_sync(src: Path, dest: Path, max_edge: int) -> Path:
    """Downscale src to dest with longest edge = max_edge (PIL thumbnail)."""
    from PIL import Image

    with Image.open(src) as im:
        im = im.convert("RGB")
        im.thumbnail((max_edge, max_edge))
        # quality=82 is a good size/quality knee for face-detection use
        im.save(dest, format="JPEG", quality=82)
    return dest


async def _frame_path_for_gemini(frame: FrameInfo, work_dir: Path) -> Path:
    """Return a small JPEG to send to Gemini for this frame."""
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT local_path FROM video_frames WHERE id = %s",
                (frame.frame_id,),
            )
            row = await cur.fetchone()
    if not row or not row.get("local_path"):
        raise RuntimeError(f"video_frame #{frame.frame_id} has no local_path")
    src = resolve_local(row["local_path"])
    if not src.exists():
        raise RuntimeError(f"video_frame #{frame.frame_id} file missing on disk: {src}")
    dest = work_dir / f"f{frame.frame_id}.jpg"
    await asyncio.to_thread(
        _downscale_frame_sync, src, dest, FRAME_DOWNSCALE_LONG_EDGE_PX,
    )
    return dest


# ---------------------------------------------------------------------------
# Talking-head mode
# ---------------------------------------------------------------------------

async def extract_talking_head_candidates(
    *, post_id: int, asset_id: int,
) -> TalkingHeadResult:
    """Find best portrait frame per detected person in a video."""
    _budget_guard()
    video_path, resolved_asset_id = await _resolve_video(post_id, asset_id)
    duration = await asyncio.to_thread(_probe_duration, video_path) or 0.0
    if duration <= 0.5:
        return TalkingHeadResult(
            candidates=[], degrade_reason="zero_duration",
            dominant_content=None, suggest_animated_poster=False,
        )

    # Duration-aware budget — longer reels need denser sampling to catch
    # speakers who only appear for a few seconds.
    cap = _max_frames_for_duration(duration)
    dedupe_window = _dedupe_window_for_budget(cap)

    # Candidate timestamps: existing video_frames + speech-aligned augmentation
    existing = await _existing_frames(resolved_asset_id)
    existing_ts = [float(r["timestamp_sec"]) for r in existing if r.get("local_path")]
    segments = await _transcript_segments(resolved_asset_id)
    augmented = _augment_timestamps_talking_head(
        existing_ts=existing_ts,
        transcript_segments=segments,
        duration=duration,
    )
    deduped = _dedupe_timestamps(augmented, dedupe_window)
    if not deduped:
        # No scene-midpoint frames AND no transcript → sample evenly
        n = min(cap, 6)
        deduped = [(i + 1) * duration / (n + 1) for i in range(n)]
    # Downsample evenly when augmentation produced more than the budget
    # (preserves earliest/latest speakers; slicing [:cap] used to drop them).
    timestamps = _downsample_evenly(deduped, cap)

    log.info(
        "[asset_frames] talking_head post=%d asset=%d duration=%.1fs "
        "cap=%d augmented=%d deduped=%d actual=%d",
        post_id, resolved_asset_id, duration, cap,
        len(augmented), len(deduped), len(timestamps),
    )

    frames = await _materialize_frames(
        video_path=video_path,
        asset_id=resolved_asset_id,
        target_timestamps=timestamps,
    )
    if not frames:
        return TalkingHeadResult(
            candidates=[], degrade_reason="no_frames_extractable",
            dominant_content=None, suggest_animated_poster=False,
        )

    # Send to Gemini under a temp dir of downscaled JPEGs
    with tempfile.TemporaryDirectory(prefix="frame_extract_") as tmp_dir_str:
        tmp_dir = Path(tmp_dir_str)
        try:
            small_paths = [await _frame_path_for_gemini(f, tmp_dir) for f in frames]
        except Exception:
            log.exception("downscale step failed")
            return TalkingHeadResult(
                candidates=[], degrade_reason="frame_prep_failed",
                dominant_content=None, suggest_animated_poster=False,
            )

        try:
            # max_tokens scales with frame count — each frame contributes a
            # row in the schema's `frames` array (and possibly a person row
            # too), so a 24-frame call needs ~2x the output budget of a
            # 12-frame call. Flash 2.5 also burns thinking tokens against the
            # output budget, so we keep an 8000 floor for safety.
            result = await asyncio.wait_for(
                gemini_multimodal(
                    prompts.talking_head_user_prompt(len(small_paths)),
                    images=small_paths,
                    system=prompts.TALKING_HEAD_SYSTEM,
                    model=config.vertex_gemini_fast_model,
                    json_schema=prompts.TALKING_HEAD_SCHEMA,
                    temperature=0.2,
                    max_tokens=_max_tokens_for_frames(len(small_paths)),
                ),
                timeout=GEMINI_TIMEOUT_SEC,
            )
        except VertexBudgetExceededError:
            raise BudgetGuardError(
                "Vertex budget exhausted mid-extraction"
            )
        except asyncio.TimeoutError:
            raise GeminiTimeoutError(
                f"Gemini call exceeded {GEMINI_TIMEOUT_SEC}s"
            )
        except Exception:
            log.exception("Gemini talking_head call failed")
            return TalkingHeadResult(
                candidates=[], degrade_reason="model_call_failed",
                dominant_content=None, suggest_animated_poster=False,
            )

    if not isinstance(result, dict) or "_raw" in result:
        log.warning("talking_head Gemini returned non-JSON / _raw")
        return TalkingHeadResult(
            candidates=[], degrade_reason="model_call_failed",
            dominant_content=None, suggest_animated_poster=False,
        )

    has_speakers = bool(result.get("has_visible_speakers"))
    dominant = (result.get("dominant_content") or "").strip() or None

    if not has_speakers:
        return TalkingHeadResult(
            candidates=[], degrade_reason="no_talking_head",
            dominant_content=dominant,
            suggest_animated_poster=(dominant in {"text_overlay", "b_roll", "mixed"}),
        )

    persons = result.get("persons") or []
    frames_data = result.get("frames") or []

    # Build person → description / suggested archetype lookup
    person_info: dict[str, dict[str, Any]] = {}
    for p in persons:
        label = (p.get("label") or "").strip()
        if not label:
            continue
        desc = (p.get("description") or "").strip()
        arch = (p.get("suggested_archetype") or "").strip().lower()
        if arch in {"none", "", "null"}:
            arch = None
        person_info[label] = {
            "description": desc, "suggested_archetype": arch,
        }

    if not person_info:
        return TalkingHeadResult(
            candidates=[], degrade_reason="no_persons_detected",
            dominant_content=dominant, suggest_animated_poster=False,
        )

    # Score every (frame, person) pair; pick best frame per person
    # Composite: 0.4*clarity + 0.4*looking_at_camera + 0.2*lighting
    #            - 0.5*mid_blink_penalty + 0.0*mid_speech_natural (small tiebreak)
    # Reasoning: face clarity + gaze direction matter most for a portrait
    # reference; lighting is the next-most important; mid_blink kills the
    # frame outright; speech-mouth is a tiebreaker only.
    scored_by_person: dict[str, list[tuple[float, FrameInfo]]] = {
        label: [] for label in person_info
    }
    for f_data in frames_data:
        idx = f_data.get("frame_index")
        if not isinstance(idx, int) or idx < 0 or idx >= len(frames):
            continue
        frame_info = frames[idx]
        for v in (f_data.get("visible_persons") or []):
            label = (v.get("label") or "").strip()
            if label not in person_info:
                continue
            clarity = float(v.get("face_clarity") or 0)
            looking = float(v.get("looking_at_camera") or 0)
            lighting = float(v.get("lighting") or 0)
            blink = bool(v.get("mid_blink"))
            speech = float(v.get("mid_speech_natural") or 0)
            score = (
                0.4 * clarity
                + 0.4 * looking
                + 0.2 * lighting
                - (0.5 if blink else 0.0)
                + 0.05 * speech
            )
            scored_by_person[label].append((score, frame_info))

    candidates: list[PersonCandidate] = []
    for label, info in person_info.items():
        scored = sorted(scored_by_person[label], key=lambda x: x[0], reverse=True)
        if not scored:
            # Gemini named the person but never attributed a frame — skip
            continue
        top_score, best_frame = scored[0]
        # Alternates: any frame within 0.05 of top score (max 2)
        alternates = [
            f for s, f in scored[1:] if (top_score - s) <= 0.05
        ][:2]
        candidates.append(PersonCandidate(
            person_label=label,
            description=info["description"],
            suggested_archetype=info["suggested_archetype"],
            best_frame=best_frame,
            score=round(top_score, 4),
            alternates=alternates,
        ))

    return TalkingHeadResult(
        candidates=candidates,
        degrade_reason=None if candidates else "no_clean_portraits",
        dominant_content=dominant,
        suggest_animated_poster=False,
    )


# ---------------------------------------------------------------------------
# Animated-poster mode
# ---------------------------------------------------------------------------

async def extract_animated_poster_candidates(
    *, post_id: int, asset_id: int,
) -> AnimatedPosterResult:
    """Rank frames by how complete the poster appears."""
    _budget_guard()
    video_path, resolved_asset_id = await _resolve_video(post_id, asset_id)
    duration = await asyncio.to_thread(_probe_duration, video_path) or 0.0
    if duration <= 0.5:
        return AnimatedPosterResult(
            candidates=[], confidence="low", rejected=[],
        )

    existing = await _existing_frames(resolved_asset_id)
    existing_ts = [float(r["timestamp_sec"]) for r in existing if r.get("local_path")]
    augmented = _augment_timestamps_animated_poster(
        existing_ts=existing_ts, duration=duration,
    )
    timestamps = _dedupe_timestamps(augmented, DEDUPE_WINDOW_SEC)
    if not timestamps:
        # Last resort — sample 5 evenly across the safe interior
        safe_start = min(0.5, duration * 0.1)
        safe_end = max(duration - 0.5, duration * 0.9)
        n = 5
        if safe_end > safe_start:
            timestamps = [
                safe_start + (i + 1) * (safe_end - safe_start) / (n + 1)
                for i in range(n)
            ]
    timestamps = timestamps[:MAX_FRAMES_TO_GEMINI]

    log.info(
        "[asset_frames] animated_poster post=%d asset=%d duration=%.1fs candidates=%d",
        post_id, resolved_asset_id, duration, len(timestamps),
    )

    frames = await _materialize_frames(
        video_path=video_path,
        asset_id=resolved_asset_id,
        target_timestamps=timestamps,
    )
    if not frames:
        return AnimatedPosterResult(
            candidates=[], confidence="low", rejected=[],
        )

    with tempfile.TemporaryDirectory(prefix="frame_extract_") as tmp_dir_str:
        tmp_dir = Path(tmp_dir_str)
        try:
            small_paths = [await _frame_path_for_gemini(f, tmp_dir) for f in frames]
        except Exception:
            log.exception("downscale step failed")
            return AnimatedPosterResult(
                candidates=[], confidence="low", rejected=[],
            )

        try:
            # max_tokens=6000: visible output is small (just an ordered_ranking
            # array + confidence + a few rejection reasons), but Flash 2.5
            # still uses thinking tokens. 6000 gives comfortable margin.
            result = await asyncio.wait_for(
                gemini_multimodal(
                    prompts.animated_poster_user_prompt(len(small_paths)),
                    images=small_paths,
                    system=prompts.ANIMATED_POSTER_SYSTEM,
                    model=config.vertex_gemini_fast_model,
                    json_schema=prompts.ANIMATED_POSTER_SCHEMA,
                    temperature=0.2,
                    max_tokens=6000,
                ),
                timeout=GEMINI_TIMEOUT_SEC,
            )
        except VertexBudgetExceededError:
            raise BudgetGuardError(
                "Vertex budget exhausted mid-extraction"
            )
        except asyncio.TimeoutError:
            raise GeminiTimeoutError(
                f"Gemini call exceeded {GEMINI_TIMEOUT_SEC}s"
            )
        except Exception:
            log.exception("Gemini animated_poster call failed")
            return AnimatedPosterResult(
                candidates=[], confidence="low", rejected=[],
            )

    if not isinstance(result, dict) or "_raw" in result:
        log.warning("animated_poster Gemini returned non-JSON / _raw")
        return AnimatedPosterResult(
            candidates=[], confidence="low", rejected=[],
        )

    ordered = result.get("ordered_ranking") or []
    confidence = (result.get("confidence") or "low").strip().lower()
    if confidence not in {"high", "medium", "low"}:
        confidence = "low"

    rejected_data = result.get("rejected_with_reason") or []
    rejected_idx_to_reason: dict[int, str] = {}
    for rej in rejected_data:
        idx = rej.get("frame_index")
        reason = (rej.get("reason") or "").strip()
        if isinstance(idx, int) and reason:
            rejected_idx_to_reason[idx] = reason

    # Build ranked candidates (skip rejected; truncate to top 5)
    candidates: list[FrameCandidate] = []
    seen_idx: set[int] = set()
    rank = 1
    for idx in ordered:
        if not isinstance(idx, int) or idx < 0 or idx >= len(frames):
            continue
        if idx in seen_idx:
            continue
        seen_idx.add(idx)
        if idx in rejected_idx_to_reason:
            continue
        candidates.append(FrameCandidate(
            frame=frames[idx], rank=rank, rejected_reason=None,
        ))
        rank += 1
        if rank > 5:
            break

    rejected = [
        FrameCandidate(
            frame=frames[i], rank=-1, rejected_reason=rejected_idx_to_reason[i],
        )
        for i in rejected_idx_to_reason
        if 0 <= i < len(frames)
    ]

    # If the model gave us nothing usable, surface the raw frames at low conf
    if not candidates:
        candidates = [
            FrameCandidate(frame=f, rank=i + 1, rejected_reason=None)
            for i, f in enumerate(frames[:3])
        ]
        confidence = "low"

    return AnimatedPosterResult(
        candidates=candidates, confidence=confidence, rejected=rejected,
    )


# ---------------------------------------------------------------------------
# Intro / Outro mode — frames from start or end of video, optimised for
# on-screen text / title cards / end cards (NOT speaker faces).
# ---------------------------------------------------------------------------

async def _fetch_scene_ranges(asset_id: int) -> list[tuple[float, float]]:
    """Read all scenes for this asset in order. Empty list when not yet deepened."""
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT start_sec, end_sec
                  FROM instagram_video_scenes
                 WHERE asset_id = %s
                 ORDER BY scene_index
                """,
                (asset_id,),
            )
            rows = await cur.fetchall()
    return [(float(r["start_sec"]), float(r["end_sec"])) for r in rows]


def _window_for_intro(
    scenes: list[tuple[float, float]],
    duration: float,
) -> tuple[float, float] | None:
    """[start, end] within which we'll sample intro frames.

    Priority:
      1. First scene's range, padded 0.3s in from start to skip the hard cut-in.
      2. Fallback (no scenes / not deepened): [0.5s, min(duration*0.25, 4.0s)].
      3. Very short video (<3s total): [0.3s, duration*0.5].
    """
    if duration < 0.6:
        return None
    if duration < 3.0:
        return (0.3, duration * 0.5)
    if scenes:
        s0_start, s0_end = scenes[0]
        start = max(s0_start + 0.3, 0.3)
        end = min(s0_end, duration)
        if end - start >= 0.4:
            return (start, end)
    # Fallback
    return (0.5, min(duration * 0.25, 4.0))


def _window_for_outro(
    scenes: list[tuple[float, float]],
    duration: float,
) -> tuple[float, float] | None:
    """[start, end] within which we'll sample outro frames.

    Priority:
      1. Last scene's range, trimmed 0.3s before end to skip fade-to-black.
      2. Fallback: [max(duration-4, duration*0.75), duration-0.3].
      3. Very short video (<3s total): [duration*0.5, duration-0.2].
    """
    if duration < 0.6:
        return None
    if duration < 3.0:
        return (duration * 0.5, max(duration - 0.2, duration * 0.5 + 0.1))
    if scenes:
        s_last_start, s_last_end = scenes[-1]
        start = max(s_last_start, 0.3)
        end = min(s_last_end - 0.3, duration - 0.3)
        if end - start >= 0.4:
            return (start, end)
    # Fallback
    start = max(duration - 4.0, duration * 0.75)
    end = duration - 0.3
    if end - start < 0.4:
        # Very narrow tail — widen
        start = max(end - 1.5, 0.3)
    return (start, end)


def _sample_window(start: float, end: float, n: int) -> list[float]:
    """n evenly-spaced points strictly inside (start, end)."""
    if end <= start or n <= 0:
        return []
    span = end - start
    return [start + ((i + 1) / (n + 1)) * span for i in range(n)]


async def _extract_intro_outro_candidates(
    *,
    post_id: int,
    asset_id: int,
    position: str,  # 'intro' | 'outro'
) -> IntroOutroResult:
    """Shared pipeline for intro and outro. The only differences vs animated
    poster: time-window is constrained to first/last scene, the prompt asks
    for text-completeness (not poster-completeness), and we return per-frame
    scores so the UI can show the strength of each candidate.
    """
    assert position in ("intro", "outro"), position
    _budget_guard()
    video_path, resolved_asset_id = await _resolve_video(post_id, asset_id)
    duration = await asyncio.to_thread(_probe_duration, video_path) or 0.0
    if duration <= 0.5:
        return IntroOutroResult(
            candidates=[], confidence="low", rejected=[],
            degrade_reason="zero_duration", position=position,
        )

    scenes = await _fetch_scene_ranges(resolved_asset_id)
    window = (
        _window_for_intro(scenes, duration)
        if position == "intro"
        else _window_for_outro(scenes, duration)
    )
    if window is None:
        return IntroOutroResult(
            candidates=[], confidence="low", rejected=[],
            degrade_reason="window_unavailable", position=position,
        )
    win_start, win_end = window

    timestamps = _sample_window(win_start, win_end, INTRO_OUTRO_FRAMES)
    if not timestamps:
        return IntroOutroResult(
            candidates=[], confidence="low", rejected=[],
            degrade_reason="window_unavailable", position=position,
        )

    log.info(
        "[asset_frames] %s post=%d asset=%d duration=%.1fs window=[%.2fs..%.2fs] candidates=%d",
        position, post_id, resolved_asset_id, duration,
        win_start, win_end, len(timestamps),
    )

    base_idx = (
        EXTRACT_BASE_FRAME_INDEX_INTRO
        if position == "intro"
        else EXTRACT_BASE_FRAME_INDEX_OUTRO
    )
    frames = await _materialize_frames(
        video_path=video_path,
        asset_id=resolved_asset_id,
        target_timestamps=timestamps,
        dedupe_window_sec=INTRO_OUTRO_DEDUPE_SEC,
        base_frame_index=base_idx,
    )
    if not frames:
        return IntroOutroResult(
            candidates=[], confidence="low", rejected=[],
            degrade_reason="no_frames_extractable", position=position,
        )

    with tempfile.TemporaryDirectory(prefix="frame_extract_") as tmp_dir_str:
        tmp_dir = Path(tmp_dir_str)
        try:
            small_paths = [await _frame_path_for_gemini(f, tmp_dir) for f in frames]
        except Exception:
            log.exception("downscale step failed (%s)", position)
            return IntroOutroResult(
                candidates=[], confidence="low", rejected=[],
                degrade_reason="frame_prep_failed", position=position,
            )

        system = prompts.INTRO_SYSTEM if position == "intro" else prompts.OUTRO_SYSTEM
        try:
            # max_tokens=6000: 6 frames × ~150 tokens of scoring JSON each +
            # ranking + reasons + Flash's thinking budget ≈ 4000-5000. 6000 is
            # comfortable headroom.
            result = await asyncio.wait_for(
                gemini_multimodal(
                    prompts.intro_outro_user_prompt(position, len(small_paths)),
                    images=small_paths,
                    system=system,
                    model=config.vertex_gemini_fast_model,
                    json_schema=prompts.INTRO_OUTRO_SCHEMA,
                    temperature=0.2,
                    max_tokens=6000,
                ),
                timeout=GEMINI_TIMEOUT_SEC,
            )
        except VertexBudgetExceededError:
            raise BudgetGuardError("Vertex budget exhausted mid-extraction")
        except asyncio.TimeoutError:
            raise GeminiTimeoutError(f"Gemini call exceeded {GEMINI_TIMEOUT_SEC}s")
        except Exception:
            log.exception("Gemini %s call failed", position)
            return IntroOutroResult(
                candidates=[], confidence="low", rejected=[],
                degrade_reason="model_call_failed", position=position,
            )

    if not isinstance(result, dict) or "_raw" in result:
        log.warning("%s Gemini returned non-JSON / _raw", position)
        return IntroOutroResult(
            candidates=[], confidence="low", rejected=[],
            degrade_reason="model_call_failed", position=position,
        )

    # Per-frame scores keyed by frame_index
    frame_scores: dict[int, dict[str, Any]] = {}
    for f_data in (result.get("frames") or []):
        idx = f_data.get("frame_index")
        if not isinstance(idx, int) or idx < 0 or idx >= len(frames):
            continue
        frame_scores[idx] = {
            "text_completeness": float(f_data.get("text_completeness") or 0.0),
            "visual_dominance": float(f_data.get("visual_dominance") or 0.0),
            "clarity": float(f_data.get("clarity") or 0.0),
            "note": (f_data.get("note") or "").strip() or None,
        }

    ordered = result.get("ordered_ranking") or []
    confidence = (result.get("confidence") or "low").strip().lower()
    if confidence not in {"high", "medium", "low"}:
        confidence = "low"

    rejected_data = result.get("rejected_with_reason") or []
    rejected_idx_to_reason: dict[int, str] = {}
    for rej in rejected_data:
        idx = rej.get("frame_index")
        reason = (rej.get("reason") or "").strip()
        if isinstance(idx, int) and reason:
            rejected_idx_to_reason[idx] = reason

    def _build(idx: int, rank: int, rejected_reason: str | None) -> IntroOutroFrameCandidate:
        scores = frame_scores.get(idx) or {
            "text_completeness": 0.0, "visual_dominance": 0.0,
            "clarity": 0.0, "note": None,
        }
        return IntroOutroFrameCandidate(
            frame=frames[idx], rank=rank,
            text_completeness=round(scores["text_completeness"], 3),
            visual_dominance=round(scores["visual_dominance"], 3),
            clarity=round(scores["clarity"], 3),
            note=scores["note"],
            rejected_reason=rejected_reason,
        )

    # Build the gallery: every frame the model ranked, skipping rejected.
    # Multi-select UX wants ALL candidates visible, sorted best-first.
    candidates: list[IntroOutroFrameCandidate] = []
    seen_idx: set[int] = set()
    rank = 1
    for idx in ordered:
        if not isinstance(idx, int) or idx < 0 or idx >= len(frames):
            continue
        if idx in seen_idx:
            continue
        seen_idx.add(idx)
        if idx in rejected_idx_to_reason:
            continue
        candidates.append(_build(idx, rank, None))
        rank += 1

    # Any frame the model never ranked or rejected → tack it on at low rank
    # so user can still see / pick it.
    for i, _ in enumerate(frames):
        if i in seen_idx or i in rejected_idx_to_reason:
            continue
        candidates.append(_build(i, rank, None))
        rank += 1
        seen_idx.add(i)

    rejected = [
        _build(i, -1, rejected_idx_to_reason[i])
        for i in rejected_idx_to_reason
        if 0 <= i < len(frames)
    ]

    # Soft empty state: every candidate scored < 0.3 on text_completeness AND
    # confidence is low → this reel has no real intro/outro text.
    degrade_reason: str | None = None
    if confidence == "low" and (
        not candidates or all(c.text_completeness < 0.3 for c in candidates)
    ):
        degrade_reason = "no_text_detected"

    return IntroOutroResult(
        candidates=candidates,
        confidence=confidence,
        rejected=rejected,
        degrade_reason=degrade_reason,
        position=position,
    )


async def extract_intro_candidates(
    *, post_id: int, asset_id: int,
) -> IntroOutroResult:
    """Find text/title-card frames from the OPENING of a video."""
    return await _extract_intro_outro_candidates(
        post_id=post_id, asset_id=asset_id, position="intro",
    )


async def extract_outro_candidates(
    *, post_id: int, asset_id: int,
) -> IntroOutroResult:
    """Find text/end-card frames from the CLOSING of a video."""
    return await _extract_intro_outro_candidates(
        post_id=post_id, asset_id=asset_id, position="outro",
    )


__all__ = [
    "Mode",
    "BudgetGuardError",
    "VideoNotFoundError",
    "GeminiTimeoutError",
    "FrameInfo",
    "PersonCandidate",
    "FrameCandidate",
    "IntroOutroFrameCandidate",
    "TalkingHeadResult",
    "AnimatedPosterResult",
    "IntroOutroResult",
    "extract_talking_head_candidates",
    "extract_animated_poster_candidates",
    "extract_intro_candidates",
    "extract_outro_candidates",
]
