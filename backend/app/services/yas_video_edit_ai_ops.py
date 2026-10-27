"""
Video Editor — AI edit-engine ops: plan schema, validation, deterministic executor.

This module is the DETERMINISTIC half of the AI edit engine for the multi-track
video editor (`yas_video_editor.py`). The LLM half (Gemini planning, music/voice
provider resolution, speech analysis) lives in the orchestrator — by the time
`apply_plan` runs, every op is fully concrete (e.g. `add_music` carries a real
`source_upload_id` / `source_asset_id`, never just a style query).

Pipeline:
    Gemini (constrained by EDIT_PLAN_SCHEMA)
        → validate_and_filter_ops(...)        # feature gating + reference checks
        → orchestrator resolves media         # NOT here (no providers/ffmpeg/LLM)
        → apply_plan(project_id, plan_id, ops)  # this module: pure timeline math

Contract recap (see services/remotion/src/compositions/schema.ts — keep in sync):
  - All time fields are INTEGER FRAMES @ 30fps.
  - Keyframe `f` is CLIP-LOCAL; arrays sorted ascending by `f`.
  - x/y keyframes are PIXEL OFFSETS from center (translate transform).
  - A transition lives on the LATER clip (`transitionIn`) and requires that clip
    to physically overlap the earlier one on the same track.
  - Playable length of a sourced clip = trimAfter - trimBefore = durationInFrames.

Idempotency: every clip created by a plan gets the deterministic id
`clip_ai_{plan_id}_{op_index}`, and apply_plan deletes all `clip_ai_{plan_id}_*`
clips before applying — re-running a plan replaces its output, never duplicates.
(Destructive ops that mutate pre-existing clips — trim/split/mute/move — are
inherently not perfectly idempotent; re-runs operate on the already-mutated doc.)

Persistence: exactly ONE `yas_video_editor.save_timeline(...)` call per apply
(rev bump, checkpoint, render-state preservation, duration re-derive).
"""

from __future__ import annotations

import copy
import logging
import re
from typing import Any, Optional

from . import yas_video_editor as ve

log = logging.getLogger("archivist.yas_video_edit_ai_ops")


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

FPS = ve.FPS  # 30 — single source of truth lives in yas_video_editor

# AI-owned lanes, created lazily on first use.
AI_VIDEO_TRACK_ID = "trk_v_ai"      # text popups + stickers (overlay layer)
AI_MEDIA_TRACK_ID = "trk_v_media"   # full-frame AI B-roll (above base video, below overlays)
AI_MUSIC_TRACK_ID = "trk_a_music"   # music bed
AI_VOICE_TRACK_ID = "trk_a_voice"   # replacement voice-overs
_AI_MUSIC_INDEX = 101
_AI_VOICE_INDEX = 102
AI_SFX_TRACK_ID = "trk_a_sfx"       # short sound effects under popups/stickers
_AI_SFX_INDEX = 103

_TRANSITION_PRESETS = {"fade", "slide", "slideLeft", "slideRight", "slideUp", "slideDown",
                       "wipe", "flip", "clockWipe", "iris", "zoom", "blur"}
_TRANSITION_TIMINGS = {"linear", "spring"}
_KEYFRAME_PROPS = {"opacity", "scale", "x", "y", "rotation", "volume"}
_ANIM_PRESETS = {"fade", "pop", "slide_up", "title"}
_MUSIC_MODES = {"auto", "suno", "library"}
_ADD_CLIP_SOURCE_KINDS = {"animation", "upload", "still"}
_ADD_CLIP_ROLES = {"intro", "outro"}
# add_media_clip accepts the same kinds plus stitched compositions (MP4s).
_ADD_MEDIA_SOURCE_KINDS = {"animation", "upload", "still", "composition"}

# Minimum clip length (frames) for either side of a transition (~1s @ 30fps).
_MIN_TRANSITION_CLIP_FRAMES = 30

# Defaults for OPTIONAL frame fields the LLM routinely omits (a missing value
# must never hard-fail an op — see _opt_dur/_req_f).
_DEFAULT_TRANSITION_F = 14   # add_transition durationInFrames (~0.47s)
_DEFAULT_POPUP_F = 90        # add_text_popup durationInFrames (~3s)
_DEFAULT_FRAMED_POPUP_F = 90  # add_framed_popup durationInFrames (~3s)
_TEXT_STAGGER_F = 5          # framed popup: text enters this many frames after the frame
_DEFAULT_STICKER_F = 90      # add_sticker durationInFrames (~3s)
_DEFAULT_MUSIC_F = 300       # add_music fallback when the timeline is empty (~10s)
_DEFAULT_SFX_F = 24          # add_sfx fallback length (~0.8s)
_DEFAULT_MEDIA_F = 150       # add_media_clip fallback length when omitted (~5s)

# Title-safe inset: keep popup/sticker centers within 45% of half-extent
# (i.e. a 5% margin on each edge of the canvas).
_TITLE_SAFE = 0.45

# Music ducking constants (deterministic — see _ducking_volume_keyframes).
_DUCK_BED = 0.65          # default bed level
_DUCK_UNDER_SPEECH = 0.22  # level while someone is talking
_DUCK_RAMP_F = 10          # ramp length at each speech boundary
_DUCK_MERGE_GAP_F = 30     # merge speech segments closer than this
_MUSIC_FADE_IN_F = 30
_MUSIC_FADE_OUT_F = 45

# Per-property keyframe value clamps (None = unbounded, e.g. x/y pixel offsets).
_KF_VALUE_CLAMPS: dict[str, Optional[tuple[float, float]]] = {
    "opacity": (0.0, 1.0),
    "volume": (0.0, 1.0),
    "scale": (0.1, 4.0),
    "x": None,
    "y": None,
    "rotation": None,
}

# Filter clamps (mirror the editor inspector's ranges).
_FILTER_CLAMPS: dict[str, tuple[float, float]] = {
    "brightness": (0.5, 1.6),
    "contrast": (0.5, 1.6),
    "saturate": (0.5, 1.6),
    "blur": (0.0, 8.0),
}


# ---------------------------------------------------------------------------
# 1) EDIT_PLAN_SCHEMA — the Gemini response_schema for a plan
# ---------------------------------------------------------------------------
# Gemini's structured-output subset rejects anyOf/oneOf, so every op type is the
# SAME flat object: an `op` discriminator + the union of all optional fields.
# Only `op` is required; validate_and_filter_ops enforces per-op semantics.

_KEYFRAME_POINT_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "f": {"type": "number", "description": "Clip-local frame (30fps)."},
        "v": {"type": "number", "description": "Value at that frame."},
    },
    "required": ["f", "v"],
}

_SPEECH_SEGMENT_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "start_f": {"type": "number", "description": "Speech start, timeline frames."},
        "end_f": {"type": "number", "description": "Speech end, timeline frames."},
    },
    "required": ["start_f", "end_f"],
}

_OP_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "op": {
            "type": "string",
            "enum": [
                "set_clip_position",
                "reorder_clips",
                "add_transition",
                "add_text_popup",
                "add_framed_popup",
                "add_sticker",
                "set_keyframes",
                "resize_clip",
                "set_filters",
                "add_music",
                "replace_voice",
                "trim_clip",
                "trim_to_video_end",
                "split_clip",
                "mute_clip",
                "delete_clip",
                "set_speed",
                "set_text",
                "add_clip",
                "add_media_clip",
            ],
            "description": "The edit operation to perform.",
        },
        # --- shared references -------------------------------------------
        "clip_id": {"type": "string", "description": "Target clip id."},
        "track_id": {"type": "string", "description": "Target track id."},
        "clip_ids": {
            "type": "array",
            "items": {"type": "string"},
            "description": "reorder_clips: the track's clip ids in the new order (a full permutation).",
        },
        # --- timing --------------------------------------------------------
        "from": {"type": "integer", "description": "Timeline start frame (30fps)."},
        "durationInFrames": {"type": "integer", "description": "Length in frames (>=1)."},
        "from_seconds": {
            "type": "number",
            "description": "PREFERRED for user-given times: timeline start in SECONDS "
                           "(e.g. 'at 4 seconds' → 4). The system converts to frames @30fps — "
                           "never compute frames yourself.",
        },
        "duration_seconds": {
            "type": "number",
            "description": "Duration in SECONDS (converted to durationInFrames @30fps).",
        },
        # --- add_transition --------------------------------------------------
        "preset": {
            "type": "string",
            "enum": ["fade", "slide", "slideLeft", "slideRight", "slideUp", "slideDown",
                     "wipe", "flip", "clockWipe", "iris", "zoom", "blur"],
            "description": "add_transition: transition preset.",
        },
        "timing": {"type": "string", "enum": ["linear", "spring"]},
        # --- add_text_popup / add_framed_popup --------------------------------
        "text": {"type": "string", "description": "add_text_popup / add_framed_popup: the popup text. "
                                                  "set_text: the NEW words for an existing text/caption clip."},
        "x": {"type": "number", "description": "Pixel offset from canvas center (+right)."},
        "y": {"type": "number", "description": "Pixel offset from canvas center (+down)."},
        "fontSize": {"type": "integer"},
        "color": {"type": "string", "description": "CSS color, e.g. #FFFFFF."},
        "fontFamily": {"type": "string"},
        "font_asset_id": {"type": "integer", "description": "Brand font (producer_assets id)."},
        "frame_asset_id": {
            "type": "integer",
            "description": "add_framed_popup: textbox-frame image (producer_assets id) behind the text.",
        },
        # --- add_sticker / add_text_popup -------------------------------------
        "asset_id": {"type": "integer", "description": "add_sticker: producer_assets id."},
        "asset_query": {
            "type": "string",
            "description": "add_sticker: describe a saved sticker/image to find in the "
                           "Assets Library (e.g. 'mouse cursor icon'). The orchestrator "
                           "resolves it to asset_id — use this when you don't have a concrete id.",
        },
        "scale": {"type": "number", "description": "add_sticker: base scale (0.1-4)."},
        "factor": {
            "type": "number",
            "description": "resize_clip: relative size multiplier for the clip/unit "
                           "(0.5 = half size, 2 = double; <1 shrinks). Resizes a whole "
                           "framed-popup UNIT (frame + text) together, preserving alignment.",
        },
        "anim": {
            "type": "string",
            "enum": ["fade", "pop", "slide_up", "title"],
            "description": "Entrance/exit animation for popups and stickers. "
                           "'title' = eased pop entrance + subtle breathing hold, for intro/title cards.",
        },
        # --- set_keyframes -----------------------------------------------------
        "property": {
            "type": "string",
            "enum": ["opacity", "scale", "x", "y", "rotation", "volume"],
            "description": "set_keyframes: which property the points drive.",
        },
        "points": {
            "type": "array",
            "items": _KEYFRAME_POINT_SCHEMA,
            "description": "set_keyframes: clip-local keyframe points, ascending by f.",
        },
        # --- set_filters ---------------------------------------------------------
        "brightness": {"type": "number", "description": "0.5-1.6, 1 = neutral."},
        "contrast": {"type": "number", "description": "0.5-1.6, 1 = neutral."},
        "saturate": {"type": "number", "description": "0.5-1.6, 1 = neutral."},
        "blur": {"type": "number", "description": "0-8 px."},
        # --- add_music --------------------------------------------------------------
        "mode": {
            "type": "string",
            "enum": ["auto", "suno", "library"],
            "description": "add_music: how the orchestrator sources the track.",
        },
        "style_or_query": {
            "type": "string",
            "description": "add_music: music style description or library search query.",
        },
        "duck": {"type": "boolean", "description": "add_music: duck under speech."},
        "gain": {"type": "number", "description": "add_music: bed volume 0-1 (default 0.65)."},
        "source_upload_id": {
            "type": "integer",
            "description": "Resolved by the orchestrator (yas_uploads id). Leave unset.",
        },
        "source_asset_id": {
            "type": "integer",
            "description": "Resolved by the orchestrator (producer_assets id). Leave unset.",
        },
        "speech_segments": {
            "type": "array",
            "items": _SPEECH_SEGMENT_SCHEMA,
            "description": "add_music ducking: speech spans in timeline frames.",
        },
        # --- trim / split / mute -----------------------------------------------------
        "trimBefore": {"type": "integer", "description": "trim_clip: source in-point (frames)."},
        "trimAfter": {"type": "integer", "description": "trim_clip: source out-point (frames)."},
        "at_f": {"type": "integer", "description": "split_clip: CLIP-LOCAL split frame."},
        "muted": {"type": "boolean", "description": "mute_clip: new muted state."},
        "speed": {
            "type": "number",
            "description": "set_speed: playback rate 0.25-4 (1 = normal, 2 = 2x faster/shorter, "
                           "0.5 = half-speed/slow-mo). Scales the clip's timeline length + keyframes.",
        },
        # --- add_clip / add_media_clip (source resolved by the orchestrator) ---------
        "source_kind": {
            "type": "string",
            "enum": ["animation", "upload", "still", "composition"],
            "description": "add_clip / add_media_clip: resolved media kind. For add_media_clip "
                           "use the exact source_kind of the MEDIA BIN item (or attached media "
                           "clip); for add_clip it is orchestrator-resolved — leave unset.",
        },
        "source_id": {
            "type": "integer",
            "description": "add_clip / add_media_clip: resolved media id. For add_media_clip use "
                           "the MEDIA BIN item's (or attached clip's) id; for add_clip it is "
                           "orchestrator-resolved.",
        },
        "role": {
            "type": "string",
            "enum": ["intro", "outro"],
            "description": "add_clip: place at the timeline start (intro) or end (outro).",
        },
        "overlay": {
            "type": "boolean",
            "description": "add_media_clip: true → small picture-in-picture inset (corner overlay) "
                           "sized via scale + x/y; false (default) → full-frame B-roll covering the "
                           "base video while it plays.",
        },
        "placement": {
            "type": "string",
            "enum": ["append", "at"],
            "description": "add_media_clip (non-overlay): 'append' → add to the MAIN video track in "
                           "sequence, one after another after the last clip (use this for 'add to "
                           "the timeline' / 'continue' / 'add all my media'); 'at' (or unset) → drop "
                           "as B-roll at `from` (or the playhead) on the AI media lane.",
        },
    },
    "required": ["op"],
}

EDIT_PLAN_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "summary": {
            "type": "string",
            "description": "One-or-two-sentence human summary of the planned edits.",
        },
        "ops": {
            "type": "array",
            "items": _OP_SCHEMA,
            "description": "Edit operations, applied in order.",
        },
    },
    "required": ["summary", "ops"],
}


# ---------------------------------------------------------------------------
# 2) ALLOWED_OPS — feature → op whitelist
# ---------------------------------------------------------------------------

_ALL_OPS: set[str] = {
    "set_clip_position",
    "reorder_clips",
    "add_transition",
    "add_text_popup",
    "add_framed_popup",
    "add_sticker",
    "set_keyframes",
    "set_filters",
    "add_music",
    "add_sfx",
    "replace_voice",
    "trim_clip",
    "trim_to_video_end",
    "split_clip",
    "mute_clip",
    "delete_clip",
    "set_speed",
    "set_text",
    "add_clip",
    "add_media_clip",
    "resize_clip",
}

ALLOWED_OPS: dict[str, set[str]] = {
    "popups": {"add_text_popup", "add_framed_popup", "add_sticker"},
    # compose: add_text_popup lets the planner add a title/CTA card.
    # add_clip is whitelisted ONLY so orchestrator-injected bookend-placement
    # ops pass validation — the empty `valid_clip_sources` default still
    # rejects every LLM-emitted add_clip.
    "compose": {"reorder_clips", "set_clip_position", "add_transition",
                "add_text_popup", "add_clip"},
    "edit": set(_ALL_OPS),
    "voice": {"replace_voice", "mute_clip"},
    "music": {"add_music"},
    "sfx": {"add_sfx"},
    "bookend": {"add_clip", "add_transition"},
}


# ---------------------------------------------------------------------------
# Low-level coercion helpers
# ---------------------------------------------------------------------------

def _to_f(x: Any) -> int:
    """THE single int-frame choke point: round, int, floor at 0."""
    try:
        return max(0, int(round(float(x))))
    except (TypeError, ValueError):
        raise ValueError(f"not a frame number: {x!r}")


def _req_f(x: Any, name: str) -> int:
    """Frame coercion for REQUIRED fields: missing/None gets a CLEAR error
    (not the generic 'not a frame number: None')."""
    if x is None:
        raise ValueError(f"{name} is required (integer frames, missing/null)")
    return _to_f(x)


def _opt_dur(x: Any, default: int) -> int:
    """Frame coercion for OPTIONAL durations with a documented default:
    missing/None/<=0 → default. Never raises for an omitted field (LLMs
    routinely leave these out); present-but-garbage values still raise."""
    if x is None:
        return default
    v = _to_f(x)
    return v if v >= 1 else default


def _num(x: Any, name: str) -> float:
    try:
        v = float(x)
    except (TypeError, ValueError):
        raise ValueError(f"{name} is not a number: {x!r}")
    if v != v or v in (float("inf"), float("-inf")):  # NaN / inf guard
        raise ValueError(f"{name} is not finite: {x!r}")
    return v


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def _is_num(x: Any) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool)


def _clip_dur(clip: dict) -> int:
    return max(1, _to_f(clip.get("durationInFrames", 1)))


def _find_clip(clips: list[dict], clip_id: str) -> dict:
    for c in clips:
        if c.get("id") == clip_id:
            return c
    raise ValueError(f"clip not found: {clip_id}")


def _track_by_id(tracks: list[dict], track_id: str) -> Optional[dict]:
    for t in tracks:
        if t.get("id") == track_id:
            return t
    return None


def _clips_on_track(clips: list[dict], track_id: str) -> list[dict]:
    return sorted(
        (c for c in clips if c.get("trackId") == track_id),
        key=lambda c: (_to_f(c.get("from", 0)), str(c.get("id"))),
    )


def _sanitize_keyframes(points: list, duration: int, prop: str) -> list[dict]:
    """Coerce → clamp f to [0, duration] → clamp v per property → sort →
    dedupe by f (when frames collide, keep the LOWER value: ducks/fades win
    over bed levels, deterministically)."""
    clamp = _KF_VALUE_CLAMPS.get(prop)
    by_f: dict[int, float] = {}
    for p in points or []:
        if not isinstance(p, dict):
            raise ValueError(f"keyframe point is not an object: {p!r}")
        f = min(_to_f(p.get("f")), duration)
        v = _num(p.get("v"), "keyframe v")
        if clamp:
            v = _clamp(v, clamp[0], clamp[1])
        if f in by_f:
            by_f[f] = min(by_f[f], v)
        else:
            by_f[f] = v
    return [{"f": f, "v": by_f[f]} for f in sorted(by_f)]


def _title_safe_xy(x: Any, y: Any, project: dict) -> tuple[float, float]:
    """Clamp center-relative pixel offsets to a 5% title-safe inset."""
    w = float(project.get("width") or 1080)
    h = float(project.get("height") or 1920)
    max_x = w * _TITLE_SAFE
    max_y = h * _TITLE_SAFE
    return (
        _clamp(_num(x, "x"), -max_x, max_x),
        _clamp(_num(y, "y"), -max_y, max_y),
    )


def _ensure_track(tracks: list[dict], track_id: str, kind: str, index: int, name: str) -> dict:
    """Lazily create an AI-owned lane with the _empty_timeline track shape."""
    existing = _track_by_id(tracks, track_id)
    if existing:
        return existing
    track = {"id": track_id, "kind": kind, "index": index, "muted": False, "locked": False, "name": name}
    tracks.append(track)
    return track


def _ensure_ai_video_track(tracks: list[dict]) -> dict:
    """Overlay lane above every user video track (index between top video
    index and 100, where the audio lanes start)."""
    existing = _track_by_id(tracks, AI_VIDEO_TRACK_ID)
    if existing:
        return existing
    video_indexes = [int(t.get("index", 0)) for t in tracks if t.get("kind") == "video"]
    index = min(99, (max(video_indexes) + 1) if video_indexes else 1)
    return _ensure_track(tracks, AI_VIDEO_TRACK_ID, "video", index, "AI overlays")


def _ensure_ai_media_track(tracks: list[dict]) -> dict:
    """B-roll lane for full-frame AI media: sits ABOVE the user video tracks (so
    it covers the base while it plays) but BELOW the popup/sticker overlay lane
    (so text/stickers stay on top). The overlay lane is bumped above it if
    needed — integer track indexes leave no gap when user video is at index 0."""
    media = _track_by_id(tracks, AI_MEDIA_TRACK_ID)
    if not media:
        user_idx = [
            int(t.get("index", 0)) for t in tracks
            if t.get("kind") == "video" and t.get("id") not in (AI_VIDEO_TRACK_ID, AI_MEDIA_TRACK_ID)
        ]
        index = min(98, (max(user_idx) + 1) if user_idx else 1)
        media = _ensure_track(tracks, AI_MEDIA_TRACK_ID, "video", index, "AI media")
    overlay = _track_by_id(tracks, AI_VIDEO_TRACK_ID)
    if overlay and int(overlay.get("index", 0)) <= int(media.get("index", 0)):
        overlay["index"] = min(99, int(media.get("index", 0)) + 1)
    return media


def _ensure_ai_music_track(tracks: list[dict]) -> dict:
    return _ensure_track(tracks, AI_MUSIC_TRACK_ID, "audio", _AI_MUSIC_INDEX, "AI music")


def _ensure_ai_voice_track(tracks: list[dict]) -> dict:
    return _ensure_track(tracks, AI_VOICE_TRACK_ID, "audio", _AI_VOICE_INDEX, "AI voice")


def _ensure_ai_sfx_track(tracks: list[dict]) -> dict:
    return _ensure_track(tracks, AI_SFX_TRACK_ID, "audio", _AI_SFX_INDEX, "AI SFX")


def _timeline_end(clips: list[dict]) -> int:
    end = 0
    for c in clips:
        try:
            end = max(end, _to_f(c.get("from", 0)) + _clip_dur(c))
        except ValueError:
            continue
    return end


def _track_end(clips: list[dict], track_id: str) -> int:
    """End frame (from + duration) of the last clip on ONE track — the append
    point for sequential main-track placement."""
    end = 0
    for c in clips:
        if c.get("trackId") != track_id:
            continue
        try:
            end = max(end, _to_f(c.get("from", 0)) + _clip_dur(c))
        except ValueError:
            continue
    return end


def _main_user_video_track(tracks: list[dict]) -> dict:
    """The primary user video lane (lowest-index video track that is NOT an
    AI-owned lane) — where 'add to the timeline' sequential clips land. Mirrors
    _op_add_clip's track pick, but also excludes the AI media lane."""
    user = [
        t for t in tracks
        if t.get("kind") == "video"
        and t.get("id") not in (AI_VIDEO_TRACK_ID, AI_MEDIA_TRACK_ID)
    ]
    if not user:
        raise ValueError("no user video track to place the clip on")
    return min(user, key=lambda t: int(t.get("index", 0) or 0))


# ---------------------------------------------------------------------------
# 3) validate_and_filter_ops — feature gating + reference checks
# ---------------------------------------------------------------------------

# Ops that MUST reference an existing clip via `clip_id`.
_CLIP_REF_OPS = {
    "set_clip_position",
    "add_transition",
    "set_keyframes",
    "resize_clip",
    "set_filters",
    "replace_voice",
    "trim_clip",
    "trim_to_video_end",
    "split_clip",
    "mute_clip",
    "delete_clip",
    "set_speed",
    "set_text",
}


def _validate_one(
    op: dict,
    valid_clip_ids: set[str],
    valid_asset_ids: set[int],
    valid_clip_sources: set[tuple[str, int]],
) -> None:
    """Raise ValueError on the first malformed/unknown-reference field."""
    name = op.get("op")

    if name in _CLIP_REF_OPS:
        cid = op.get("clip_id")
        if not isinstance(cid, str) or cid not in valid_clip_ids:
            raise ValueError(f"unknown clip_id: {cid!r}")

    if name == "set_clip_position":
        _req_f(op.get("from"), "set_clip_position.from")
        tid = op.get("track_id")
        if tid is not None and not isinstance(tid, str):
            raise ValueError(f"track_id must be a string: {tid!r}")

    elif name == "reorder_clips":
        if not isinstance(op.get("track_id"), str) or not op["track_id"]:
            raise ValueError("reorder_clips requires track_id")
        ids = op.get("clip_ids")
        if not isinstance(ids, list) or not ids:
            raise ValueError("reorder_clips requires a non-empty clip_ids list")
        unknown = [i for i in ids if i not in valid_clip_ids]
        if unknown:
            raise ValueError(f"unknown clip_ids: {unknown}")
        if len(set(ids)) != len(ids):
            raise ValueError("clip_ids contains duplicates")

    elif name == "add_transition":
        # preset / durationInFrames / timing are all defaultable (fade / 12f /
        # linear) — only reject values that are PRESENT and invalid.
        if op.get("preset") is not None and op["preset"] not in _TRANSITION_PRESETS:
            raise ValueError(f"invalid transition preset: {op.get('preset')!r}")
        if op.get("durationInFrames") is not None:
            _to_f(op["durationInFrames"])  # non-numeric still rejects; <=0 → default
        if op.get("timing") is not None and op["timing"] not in _TRANSITION_TIMINGS:
            raise ValueError(f"invalid timing: {op.get('timing')!r}")

    elif name == "add_text_popup":
        if not isinstance(op.get("text"), str) or not op["text"].strip():
            raise ValueError("add_text_popup requires non-empty text")
        _req_f(op.get("from"), "add_text_popup.from")
        if op.get("durationInFrames") is not None:
            _to_f(op["durationInFrames"])  # missing/<=0 → executor defaults 90f
        if op.get("x") is not None:
            _num(op["x"], "x")  # missing → center (0)
        if op.get("y") is not None:
            _num(op["y"], "y")
        fid = op.get("font_asset_id")
        if fid is not None and int(fid) not in valid_asset_ids:
            raise ValueError(f"unknown font_asset_id: {fid!r}")
        if op.get("anim") is not None and op["anim"] not in _ANIM_PRESETS:
            raise ValueError(f"invalid anim: {op.get('anim')!r}")

    elif name == "add_framed_popup":
        if not isinstance(op.get("text"), str) or not op["text"].strip():
            raise ValueError("add_framed_popup requires non-empty text")
        _req_f(op.get("from"), "add_framed_popup.from")
        if op.get("durationInFrames") is not None:
            _to_f(op["durationInFrames"])  # missing/<=0 → executor defaults 90f
        if op.get("x") is not None:
            _num(op["x"], "x")  # missing → center (0)
        if op.get("y") is not None:
            _num(op["y"], "y")
        faid = op.get("frame_asset_id")
        if not _is_num(faid) or int(faid) not in valid_asset_ids:
            raise ValueError(f"unknown frame_asset_id: {faid!r}")
        fid = op.get("font_asset_id")
        if fid is not None and int(fid) not in valid_asset_ids:
            raise ValueError(f"unknown font_asset_id: {fid!r}")
        if op.get("anim") is not None and op["anim"] not in _ANIM_PRESETS:
            raise ValueError(f"invalid anim: {op.get('anim')!r}")

    elif name == "add_sticker":
        aid = op.get("asset_id")
        if not _is_num(aid) or int(aid) not in valid_asset_ids:
            raise ValueError(f"unknown asset_id: {aid!r}")
        _req_f(op.get("from"), "add_sticker.from")
        if op.get("durationInFrames") is not None:
            _to_f(op["durationInFrames"])  # missing/<=0 → executor defaults 90f
        if op.get("x") is not None:
            _num(op["x"], "x")  # missing → center (0)
        if op.get("y") is not None:
            _num(op["y"], "y")
        if op.get("anim") is not None and op["anim"] not in _ANIM_PRESETS:
            raise ValueError(f"invalid anim: {op.get('anim')!r}")

    elif name == "set_keyframes":
        if op.get("property") not in _KEYFRAME_PROPS:
            raise ValueError(f"invalid keyframe property: {op.get('property')!r}")
        pts = op.get("points")
        if not isinstance(pts, list) or not pts:
            raise ValueError("set_keyframes requires a non-empty points list")
        for p in pts:
            if not isinstance(p, dict) or not _is_num(p.get("f")) or not _is_num(p.get("v")):
                raise ValueError(f"malformed keyframe point: {p!r}")

    elif name == "resize_clip":
        f = op.get("factor")
        if not _is_num(f) or float(f) <= 0:
            raise ValueError("resize_clip requires a positive factor (0.5 = half size)")

    elif name == "set_filters":
        if not any(_is_num(op.get(k)) for k in _FILTER_CLAMPS):
            raise ValueError("set_filters requires at least one numeric filter field")

    elif name == "add_music":
        if op.get("mode") is not None and op["mode"] not in _MUSIC_MODES:
            raise ValueError(f"invalid music mode: {op.get('mode')!r}")
        # source_upload_id is orchestrator-resolved later; source_asset_id (a
        # library pick) must reference a known asset when already present.
        aid = op.get("source_asset_id")
        if aid is not None and int(aid) not in valid_asset_ids:
            raise ValueError(f"unknown source_asset_id: {aid!r}")
        segs = op.get("speech_segments")
        if segs is not None:
            if not isinstance(segs, list):
                raise ValueError("speech_segments must be a list")
            for s in segs:
                if not isinstance(s, dict) or not _is_num(s.get("start_f")) or not _is_num(s.get("end_f")):
                    raise ValueError(f"malformed speech segment: {s!r}")

    elif name == "replace_voice":
        # source_upload_id is filled by the orchestrator's STS step; accept it
        # missing here, apply_plan fails the op if it never materializes.
        pass

    elif name == "trim_clip":
        if op.get("trimBefore") is None and op.get("trimAfter") is None:
            raise ValueError("trim_clip requires trimBefore and/or trimAfter")
        if op.get("trimBefore") is not None:
            _to_f(op["trimBefore"])
        if op.get("trimAfter") is not None:
            _to_f(op["trimAfter"])

    elif name == "split_clip":
        if _req_f(op.get("at_f"), "split_clip.at_f") < 1:
            raise ValueError("split_clip at_f must be >= 1 (clip-local)")

    elif name == "mute_clip":
        if not isinstance(op.get("muted"), bool):
            raise ValueError("mute_clip requires boolean muted")

    elif name == "set_speed":
        s = _num(op.get("speed"), "speed")
        if not (0.25 <= s <= 4.0):
            raise ValueError(f"set_speed speed must be 0.25-4: {s!r}")

    elif name == "set_text":
        # Rewrite the words AND/OR restyle. Valid with non-empty `text`, OR with
        # at least one style field (so "use this font" can restyle without
        # touching the words — a dragged-font attachment).
        has_text = isinstance(op.get("text"), str) and bool(op["text"].strip())
        has_style = any(
            op.get(k) is not None
            for k in ("font_asset_id", "fontFamily", "fontSize", "color")
        )
        if not has_text and not has_style:
            raise ValueError("set_text needs new text or at least one style field (font/size/color)")

    elif name == "add_sfx":
        # source_upload_id is orchestrator-resolved; `from` defaults to 0.
        if op.get("from") is not None:
            _to_f(op["from"])

    elif name == "add_clip":
        # Full-screen clip from ALREADY-RESOLVED media. The orchestrator is the
        # only legal author: valid_clip_sources defaults to empty, so any
        # LLM-emitted add_clip is rejected here.
        role = op.get("role")
        if role not in _ADD_CLIP_ROLES:
            raise ValueError(f"add_clip requires role in {sorted(_ADD_CLIP_ROLES)}: {role!r}")
        sk = op.get("source_kind")
        if sk not in _ADD_CLIP_SOURCE_KINDS:
            raise ValueError(f"invalid add_clip source_kind: {sk!r}")
        sid = op.get("source_id")
        if not _is_num(sid):
            raise ValueError(f"add_clip requires a numeric source_id: {sid!r}")
        if _req_f(op.get("durationInFrames"), "add_clip.durationInFrames") < 1:
            raise ValueError("add_clip durationInFrames must be >= 1")
        if (str(sk), int(sid)) not in valid_clip_sources:
            raise ValueError(
                f"add_clip source not orchestrator-resolved: ({sk!r}, {sid!r})"
            )

    elif name == "add_media_clip":
        # Place/overlay a media-bin clip the user DRAGGED into chat. Like
        # add_clip, the source must be in valid_clip_sources (built from the
        # request attachments) — the LLM can't conjure a clip from nothing.
        sk = op.get("source_kind")
        if sk not in _ADD_MEDIA_SOURCE_KINDS:
            raise ValueError(f"invalid add_media_clip source_kind: {sk!r}")
        sid = op.get("source_id")
        if not _is_num(sid):
            raise ValueError(f"add_media_clip requires a numeric source_id: {sid!r}")
        if op.get("from") is not None:
            _to_f(op["from"])
        if op.get("durationInFrames") is not None:
            _to_f(op["durationInFrames"])  # missing/<=0 → executor defaults ~5s
        if op.get("x") is not None:
            _num(op["x"], "x")
        if op.get("y") is not None:
            _num(op["y"], "y")
        if (str(sk), int(sid)) not in valid_clip_sources:
            raise ValueError(
                f"add_media_clip source not attached/resolved: ({sk!r}, {sid!r}) — "
                f"the user must drag the clip into the chat"
            )


def validate_and_filter_ops(
    ops: list[dict],
    *,
    feature: str,
    valid_clip_ids: set[str],
    valid_asset_ids: set[int],
    valid_clip_sources: Optional[set[tuple[str, int]]] = None,
) -> tuple[list[dict], list[dict]]:
    """Gate a Gemini plan's ops by feature + sanity. Returns
    (accepted, rejected) where rejected items are {"op": <original>, "error": str}.

    `valid_clip_sources` is the set of (source_kind, source_id) tuples an
    add_clip op may reference — only the orchestrator passes it (for media it
    resolved itself); the default empty set rejects every LLM-emitted add_clip.
    """
    if valid_clip_sources is None:
        valid_clip_sources = set()
    allowed = ALLOWED_OPS.get(feature)
    if allowed is None:
        raise ValueError(f"unknown feature: {feature!r} (expected one of {sorted(ALLOWED_OPS)})")

    accepted: list[dict] = []
    rejected: list[dict] = []
    for op in ops or []:
        if not isinstance(op, dict):
            rejected.append({"op": op, "error": "op is not an object"})
            continue
        name = op.get("op")
        if name not in _ALL_OPS:
            rejected.append({"op": op, "error": f"unknown op: {name!r}"})
            continue
        if name not in allowed:
            rejected.append({"op": op, "error": f"op {name!r} not allowed for feature {feature!r}"})
            continue
        try:
            _validate_one(op, valid_clip_ids, valid_asset_ids, valid_clip_sources)
        except (ValueError, TypeError) as exc:
            rejected.append({"op": op, "error": str(exc)})
            continue
        accepted.append(op)

    if rejected:
        log.info(
            "validate_and_filter_ops feature=%s accepted=%d rejected=%d (%s)",
            feature, len(accepted), len(rejected),
            "; ".join(str(r["error"]) for r in rejected[:5]),
        )
    return accepted, rejected


def drop_unapplyable_transitions(ops: list[dict], clips: list[dict]) -> list[dict]:
    """Remove add_transition ops the executor would reject so they never count
    as failures: the target (LATER) clip or its nearest same-track predecessor
    is shorter than _MIN_TRANSITION_CLIP_FRAMES, or there is no predecessor.
    Mirrors the adjacency + length checks in _apply_transition. Returns a new
    filtered list; dropped ops are silently discarded (not 'failed')."""
    by_id = {str(c.get("id")): c for c in clips if c.get("id") is not None}
    out: list[dict] = []
    for op in ops or []:
        if not isinstance(op, dict) or op.get("op") != "add_transition":
            out.append(op)
            continue
        later = by_id.get(str(op.get("clip_id")))
        if later is None:
            continue  # unknown clip — would raise; drop cleanly
        track_id = later.get("trackId")
        from_l = _to_f(later.get("from", 0))
        cands = [
            c for c in clips
            if c.get("trackId") == track_id and str(c.get("id")) != str(op.get("clip_id"))
            and _to_f(c.get("from", 0)) < from_l
        ]
        if not cands:
            continue  # no predecessor — would raise; drop
        earlier = max(cands, key=lambda c: _to_f(c.get("from", 0)))
        if _clip_dur(earlier) < _MIN_TRANSITION_CLIP_FRAMES or _clip_dur(later) < _MIN_TRANSITION_CLIP_FRAMES:
            continue  # too short — would raise; drop
        out.append(op)
    return out


def drop_empty_text_popups(ops: list[dict]) -> list[dict]:
    """Remove add_text_popup / add_framed_popup ops whose text is still blank
    after backfill — the executor would reject them as 'requires non-empty
    text'. Drop cleanly so they don't show as failures. Returns a new list."""
    out: list[dict] = []
    for op in ops or []:
        if isinstance(op, dict) and op.get("op") in ("add_text_popup", "add_framed_popup"):
            txt = op.get("text")
            if not isinstance(txt, str) or not txt.strip():
                continue
        out.append(op)
    return out


# ---------------------------------------------------------------------------
# Animation synthesis (popups + stickers)
# ---------------------------------------------------------------------------

def _wrapped_line_count(text: str, chars_per_line: int) -> int:
    """Greedy WORD-wrap line count: words are never split, each line packs as
    many whole words as fit in `chars_per_line` (+1 char per joining space).
    A single word longer than the line still occupies one line — callers that
    care check the longest word separately. Always >= 1."""
    cpl = max(1, chars_per_line)
    lines = 0
    cur = 0
    for word in text.split():
        wl = len(word)
        if cur == 0:
            lines += 1
            cur = wl
        elif cur + 1 + wl <= cpl:
            cur += 1 + wl
        else:
            lines += 1
            cur = wl
    return max(1, lines)


def _popup_keyframes(
    anim: Optional[str],
    duration: int,
    *,
    x: float,
    y: float,
    base_scale: float = 1.0,
) -> dict:
    """Tasteful in/out keyframes for a popup/sticker. The renderer interpolates
    LINEARLY between points, so easing is faked with EXTRA intermediate points
    (fast start, slow settle). The in-animation runs over the first ~12 frames,
    the eased out-animation over the last ~9. For very short clips the windows
    shrink so in/out never cross."""
    dur = max(1, duration)
    in_f = min(12, max(1, dur // 3))           # entrance window
    out_f = min(9, max(1, dur // 3))           # exit window
    out_start = max(in_f, dur - out_f)

    opacity: list[dict] = []
    scale: list[dict] = []
    xs: list[dict] = [{"f": 0, "v": x}]
    ys: list[dict] = [{"f": 0, "v": y}]

    # Eased entrance milestones (max(1, …) so they never clobber the f=0 point;
    # _sanitize_keyframes keeps the LOWER value on frame collisions, which on
    # tiny clips degrades these curves gracefully instead of spiking).
    in_mid = max(1, int(round(in_f * 0.5)))    # overshoot peak
    in_settle = max(1, int(round(in_f * 0.8)))  # undershoot before settling
    in_vis = max(1, int(round(in_f * 0.6)))    # opacity fully in (before scale settles)

    if anim == "pop":
        # Pop: fade in quickly while scale overshoots and settles:
        # 0.90 → 1.05 (50%) → 0.985 (80%) → 1.0 — a springy ease, not a blink.
        opacity = [{"f": 0, "v": 0.0}, {"f": in_vis, "v": 1.0}]
        scale = [
            {"f": 0, "v": base_scale * 0.90},
            {"f": in_mid, "v": base_scale * 1.05},
            {"f": in_settle, "v": base_scale * 0.985},
            {"f": in_f, "v": base_scale},
        ]
        # Eased out: fade + gentle shrink.
        opacity += [{"f": out_start, "v": 1.0}, {"f": dur, "v": 0.0}]
        scale += [{"f": out_start, "v": base_scale}, {"f": dur, "v": base_scale * 0.94}]
    elif anim == "slide_up":
        # Slide up: gentle 28px rise shaped fast-then-settle (78% of the travel
        # in the first 55% of the window); small +14px drift on the way out.
        opacity = [{"f": 0, "v": 0.0}, {"f": in_f, "v": 1.0},
                   {"f": out_start, "v": 1.0}, {"f": dur, "v": 0.0}]
        rise_mid = max(1, int(round(in_f * 0.55)))
        ys = [{"f": 0, "v": y + 28.0}, {"f": rise_mid, "v": y + 6.0},
              {"f": in_f, "v": y},
              {"f": out_start, "v": y}, {"f": dur, "v": y + 14.0}]
        if base_scale != 1.0:
            scale = [{"f": 0, "v": base_scale}]
    elif anim == "title":
        # Title card (intro hooks, 2-2.5s): pop-style eased entrance, then a
        # subtle "breathing" hold (scale drifts 1.0 → 1.03 across the body) so
        # the card feels alive, then an eased fade/shrink out.
        opacity = [{"f": 0, "v": 0.0}, {"f": in_vis, "v": 1.0},
                   {"f": out_start, "v": 1.0}, {"f": dur, "v": 0.0}]
        scale = [
            {"f": 0, "v": base_scale * 0.92},
            {"f": in_mid, "v": base_scale * 1.04},
            {"f": in_settle, "v": base_scale * 0.995},
            {"f": in_f, "v": base_scale},
            {"f": out_start, "v": base_scale * 1.03},
            {"f": dur, "v": base_scale * 0.97},
        ]
    else:
        # Default/fade: opacity 0 → 1 over the entrance window, mirrored
        # fade-out over the tail.
        opacity = [{"f": 0, "v": 0.0}, {"f": in_f, "v": 1.0},
                   {"f": out_start, "v": 1.0}, {"f": dur, "v": 0.0}]
        if base_scale != 1.0:
            scale = [{"f": 0, "v": base_scale}]

    kf: dict = {
        "opacity": _sanitize_keyframes(opacity, dur, "opacity"),
        "x": _sanitize_keyframes(xs, dur, "x"),
        "y": _sanitize_keyframes(ys, dur, "y"),
    }
    if scale:
        kf["scale"] = _sanitize_keyframes(scale, dur, "scale")
    return kf


# ---------------------------------------------------------------------------
# Music ducking
# ---------------------------------------------------------------------------

def _ducking_volume_keyframes(
    *,
    clip_from: int,
    duration: int,
    speech_segments: list[dict],
    bed: float,
) -> list[dict]:
    """Deterministic ducking curve: bed level with dips to _DUCK_UNDER_SPEECH
    under (merged) speech spans, 10f ramps at each boundary, global fade-in
    30f / fade-out 45f. `speech_segments` are TIMELINE frames; output keyframe
    `f` values are CLIP-LOCAL."""
    dur = max(1, duration)
    duck_v = min(bed, _DUCK_UNDER_SPEECH)

    # 1) Convert to clip-local, clamp into [0, dur], drop empty spans.
    spans: list[tuple[int, int]] = []
    for seg in speech_segments or []:
        s = max(0, _to_f(seg.get("start_f")) - clip_from)
        e = min(dur, _to_f(seg.get("end_f")) - clip_from)
        if e > s:
            spans.append((s, e))
    spans.sort()

    # 2) Merge spans separated by < _DUCK_MERGE_GAP_F (one continuous duck).
    merged: list[list[int]] = []
    for s, e in spans:
        if merged and s - merged[-1][1] < _DUCK_MERGE_GAP_F:
            merged[-1][1] = max(merged[-1][1], e)
        else:
            merged.append([s, e])

    # 3) Bed + global fades.
    pts: list[dict] = [
        {"f": 0, "v": 0.0},
        {"f": min(_MUSIC_FADE_IN_F, dur), "v": bed},
        {"f": max(0, dur - _MUSIC_FADE_OUT_F), "v": bed},
        {"f": dur, "v": 0.0},
    ]

    # 4) Duck dips with 10f ramps at each boundary.
    for s, e in merged:
        pts.append({"f": max(0, s - _DUCK_RAMP_F), "v": bed})
        pts.append({"f": s, "v": duck_v})
        pts.append({"f": e, "v": duck_v})
        pts.append({"f": min(dur, e + _DUCK_RAMP_F), "v": bed})

    # _sanitize_keyframes sorts + dedupes keeping the LOWER value per frame, so
    # ducks and the global fades deterministically win over bed-level points.
    return _sanitize_keyframes(pts, dur, "volume")


# ---------------------------------------------------------------------------
# Transition overlap routine (correctness-critical)
# ---------------------------------------------------------------------------

def _apply_transition(
    clips: list[dict],
    *,
    clip_id: str,
    preset: str,
    requested: int,
    timing: str,
) -> None:
    """Make the LATER clip physically overlap its predecessor and tag it with
    transitionIn. Shifts only the MISSING overlap so re-runs don't double-shift,
    and drags every later clip on the track along to keep relative spacing."""
    later = _find_clip(clips, clip_id)
    track_id = later.get("trackId")
    from_l = _to_f(later.get("from", 0))
    dur_l = _clip_dur(later)

    # E = the nearest clip on the same track that starts before L (i.e. the
    # clip the transition crosses out of).
    candidates = [
        c for c in clips
        if c.get("trackId") == track_id and c.get("id") != clip_id
        and _to_f(c.get("from", 0)) < from_l
    ]
    if not candidates:
        raise ValueError(f"no earlier clip on track {track_id!r} to transition from")
    earlier = max(candidates, key=lambda c: _to_f(c.get("from", 0)))
    from_e = _to_f(earlier.get("from", 0))
    dur_e = _clip_dur(earlier)

    if dur_e < _MIN_TRANSITION_CLIP_FRAMES or dur_l < _MIN_TRANSITION_CLIP_FRAMES:
        raise ValueError(
            f"both clips must be >= {_MIN_TRANSITION_CLIP_FRAMES}f for a transition "
            f"(earlier={dur_e}f, later={dur_l}f)"
        )

    # Effective overlap can never consume a whole clip.
    overlap = min(requested, dur_e - 1, dur_l - 1)
    if overlap < 1:
        raise ValueError("transition overlap collapsed to zero")

    # Only add the overlap that's MISSING (existing physical overlap counts),
    # so re-applying the same transition is a no-op rather than a double shift.
    existing_overlap = max(0, (from_e + dur_e) - from_l)
    shift = overlap - existing_overlap

    old_from_l = from_l
    later["from"] = max(0, from_l - shift)
    # Drag everything that sat at/after L left by the same amount so the rest
    # of the track keeps its relative spacing (no new gaps/overlaps downstream).
    for c in clips:
        if c is later or c.get("trackId") != track_id:
            continue
        c_from = _to_f(c.get("from", 0))
        if c_from >= old_from_l:
            c["from"] = max(0, c_from - shift)

    later["transitionIn"] = {
        "preset": preset,
        "durationInFrames": overlap,
        "timing": timing if timing in _TRANSITION_TIMINGS else "linear",
    }


# ---------------------------------------------------------------------------
# Per-op executors. Each takes the mutable doc + the op and mutates in place.
# `new_clip_id` is the deterministic id for any clip the op creates.
# ---------------------------------------------------------------------------

def _op_set_clip_position(project: dict, tracks: list, clips: list, op: dict, new_clip_id: str) -> None:
    # Move a clip in time (and optionally across tracks, kind-compatible only).
    clip = _find_clip(clips, op["clip_id"])
    clip["from"] = _req_f(op.get("from"), "set_clip_position.from")
    tid = op.get("track_id")
    if tid:
        track = _track_by_id(tracks, tid)
        if not track:
            raise ValueError(f"unknown track_id: {tid!r}")
        clip_kind = clip.get("kind") or "video"
        # Invariant: audio clips only on audio tracks (and vice versa).
        if (clip_kind == "audio") != (track.get("kind") == "audio"):
            raise ValueError(
                f"cannot move {clip_kind!r} clip onto {track.get('kind')!r} track {tid!r}"
            )
        clip["trackId"] = tid


def _op_reorder_clips(project: dict, tracks: list, clips: list, op: dict, new_clip_id: str) -> None:
    # Re-sequence a track: clip_ids must be a permutation of the track's clips;
    # they're laid back-to-back from the track's current first frame, with each
    # clip's EXISTING transition overlap re-applied (the later clip slides left
    # under its predecessor by the transition length).
    track_id = op["track_id"]
    if not _track_by_id(tracks, track_id):
        raise ValueError(f"unknown track_id: {track_id!r}")
    current = _clips_on_track(clips, track_id)
    if not current:
        raise ValueError(f"track {track_id!r} has no clips")
    want = list(op["clip_ids"])
    if set(want) != {c["id"] for c in current} or len(want) != len(current):
        raise ValueError("clip_ids is not a permutation of the track's clips")

    by_id = {c["id"]: c for c in current}
    cursor = min(_to_f(c.get("from", 0)) for c in current)
    prev: Optional[dict] = None
    for cid in want:
        clip = by_id[cid]
        dur = _clip_dur(clip)
        trans = clip.get("transitionIn") or {}
        preset = trans.get("preset")
        if prev is not None and preset and preset != "none":
            # Same overlap math as _apply_transition, replayed at the new slot.
            overlap = min(_to_f(trans.get("durationInFrames") or 0), _clip_dur(prev) - 1, dur - 1)
            if overlap >= 1:
                clip["from"] = max(0, cursor - overlap)
                clip["transitionIn"] = {
                    "preset": preset,
                    "durationInFrames": overlap,
                    "timing": trans.get("timing", "linear"),
                }
            else:
                clip["from"] = cursor
                clip["transitionIn"] = None
        else:
            clip["from"] = cursor
            if prev is None and preset and preset != "none":
                clip["transitionIn"] = None  # first clip can't transition in
        cursor = _to_f(clip["from"]) + dur
        prev = clip


def _op_add_transition(project: dict, tracks: list, clips: list, op: dict, new_clip_id: str) -> None:
    # Cross-clip transition: overlap the LATER clip (op.clip_id) into its
    # predecessor and tag transitionIn — see _apply_transition for the math.
    # The LLM routinely omits the optional fields: durationInFrames
    # missing/None/<=0 → 12f (then the usual overlap clamp), preset → 'fade',
    # timing → 'linear'. A missing duration must NEVER fail the op.
    preset = op.get("preset") or "fade"
    if preset not in _TRANSITION_PRESETS:
        preset = "fade"
    _apply_transition(
        clips,
        clip_id=op["clip_id"],
        preset=preset,
        requested=_opt_dur(op.get("durationInFrames"), _DEFAULT_TRANSITION_F),
        timing=op.get("timing") or "linear",
    )


def _op_add_text_popup(project: dict, tracks: list, clips: list, op: dict, new_clip_id: str) -> None:
    # Text overlay on the AI video lane, with synthesized in/out animation and
    # title-safe placement. font_asset_id → brand-font URL the renderer resolves.
    track = _ensure_ai_video_track(tracks)
    start = _req_f(op.get("from"), "add_text_popup.from")
    dur = _opt_dur(op.get("durationInFrames"), _DEFAULT_POPUP_F)
    # x/y missing/None → canvas center (0, 0).
    x, y = _title_safe_xy(op.get("x") or 0, op.get("y") or 0, project)

    font_size = max(12, min(300, _to_f(op.get("fontSize")))) if op.get("fontSize") is not None else 64
    text_block: dict = {
        "content": str(op["text"]),
        "fontFamily": str(op.get("fontFamily") or "Inter"),
        "fontSize": font_size,
        "color": str(op.get("color") or "#FFFFFF"),
        "fontWeight": 700,
        "textAlign": "center",
    }
    fid = op.get("font_asset_id")
    if fid is not None:
        # Brand font from the Assets Library; the export path rewrites this to
        # a /data URL via yas_video_editor._resolve_text_font.
        text_block["fontUrl"] = f"/api/agents/producer/assets/{int(fid)}/file"

    clips.append({
        "id": new_clip_id,
        "trackId": track["id"],
        "kind": "text",
        "trimBefore": 0,
        "trimAfter": dur,
        "from": start,
        "durationInFrames": dur,
        "text": text_block,
        "keyframes": _popup_keyframes(op.get("anim"), dur, x=x, y=y),
    })


def _op_add_framed_popup(project: dict, tracks: list, clips: list, op: dict, new_clip_id: str) -> None:
    # Composite popup: a textbox-frame image (Library asset) with brand-font
    # text stacked on top, both on the AI video lane. The text clip is appended
    # AFTER the frame so it renders above it, and both ids share the
    # `clip_ai_{plan}_` prefix so apply_plan's idempotent cleanup removes them.
    # The TEXT entrance is STAGGERED ~5 frames after the frame (the frame lands
    # first, then the text arrives) by shifting the text clip's `from` while
    # keeping both clips ending on the same frame.
    track = _ensure_ai_video_track(tracks)
    start = _req_f(op.get("from"), "add_framed_popup.from")
    dur = _opt_dur(op.get("durationInFrames"), _DEFAULT_FRAMED_POPUP_F)
    # x/y missing/None → canvas center (0, 0).
    x, y = _title_safe_xy(op.get("x") or 0, op.get("y") or 0, project)

    # An opening title card (first ~2s of the video) gets hero treatment:
    # wider frame, bigger/bolder text, higher shrink floor.
    is_intro_card = start < 60

    # --- deterministic frame sizing + text fit ------------------------------
    w = float(project.get("width") or 1080)
    h = float(project.get("height") or 1920)
    # Frame aspect ratio stamped by the orchestrator (_enrich_framed_popup_ops)
    # from producer_assets.width/height; falls back to a wide 3:1 textbox.
    ar_raw = op.get("_frame_aspect")
    ar = float(ar_raw) if _is_num(ar_raw) and float(ar_raw) > 0 else 3.0
    # Contain-fit base size of the frame on this canvas, then scale so the
    # rendered frame spans a tasteful fraction of the canvas width. Intro/title
    # cards stay hero-sized (~78%); section popups are deliberately smaller
    # (~62%) so they read as overlays and never blanket the talking head — the
    # AI can still resize_clip from here.
    fw0 = min(w, h * ar)
    fh0 = fw0 / ar
    width_frac = 0.78 if is_intro_card else 0.62
    frame_scale = _clamp(width_frac * w / fw0, 0.3, 1.0)
    fw = fw0 * frame_scale
    fh = fh0 * frame_scale
    # Body box under the title bar (~16% of frame height): the text must fit
    # inside ~78% x 62% of the frame.
    bw = 0.78 * fw
    bh = 0.62 * fh

    # Font fit: start at the requested size and shrink in 4px steps until the
    # WORD-wrapped block (words never split mid-word, so short multi-word
    # titles don't over-shrink) fits the body box and the longest word fits
    # one line (avg glyph width 0.58*fs, lineHeight 1.15). Floor 28; intro
    # cards start at 72/weight-600 with floor 34 so a 3-5 word hook stays BIG.
    text = str(op["text"])
    fs_default = 72 if is_intro_card else 64
    fs_floor = 34 if is_intro_card else 28
    fs = max(12, min(300, _to_f(op.get("fontSize")))) if op.get("fontSize") is not None else fs_default
    longest_word = max((len(word) for word in text.split()), default=1)
    while fs > fs_floor:
        chars_per_line = max(1, int(bw / (0.58 * fs)))
        lines = _wrapped_line_count(text, chars_per_line)
        if lines * fs * 1.15 <= bh and longest_word <= chars_per_line:
            break
        fs -= 4
    fs = max(fs_floor, fs)

    # Vertically center the text block in the frame body. The renderer anchors
    # the block at its CENTER (flex-centered + translate), so centering means
    # placing the block center at the body-box center: the ~16% title bar
    # pushes the body-box center down by 0.08*fh from the frame center. The
    # estimated block height then clamps the center so a floor-limited block
    # never spills past the bar or the frame's bottom edge.
    lines = _wrapped_line_count(text, max(1, int(bw / (0.58 * fs))))
    block_h = lines * fs * 1.15
    text_y = y + 0.08 * fh
    body_top = y - 0.5 * fh + 0.16 * fh  # frame-local bottom of the title bar
    body_bottom = y + 0.47 * fh          # small inset off the frame's bottom edge
    text_y = max(text_y, body_top + block_h / 2.0)
    text_y = min(text_y, body_bottom - block_h / 2.0)
    text_y = max(text_y, y)  # never above the frame center if the block is too tall

    # 1) The frame graphic (structurally an add_sticker clip).
    clips.append({
        "id": f"{new_clip_id}_frame",
        "trackId": track["id"],
        "kind": "image",
        "source": {"kind": "asset", "asset_id": int(op["frame_asset_id"])},
        "trimBefore": 0,
        "trimAfter": dur,
        "from": start,
        "durationInFrames": dur,
        "fit": "contain",
        "keyframes": _popup_keyframes(op.get("anim"), dur, x=x, y=y, base_scale=frame_scale),
    })

    # 2) The text, stacked above the frame (structurally an add_text_popup
    # clip), entering _TEXT_STAGGER_F frames after the frame but ending on the
    # same frame so the pair exits together.
    stagger = _TEXT_STAGGER_F if dur > 2 * _TEXT_STAGGER_F else 0
    text_from = start + stagger
    text_dur = dur - stagger

    text_block: dict = {
        "content": text,
        "fontFamily": str(op.get("fontFamily") or "Inter"),
        "fontSize": fs,
        "color": str(op.get("color") or "#1A1A1A"),
        "fontWeight": 600 if is_intro_card else 400,
        "textAlign": "center",
    }
    fid = op.get("font_asset_id")
    if fid is not None:
        # Brand font from the Assets Library; the export path rewrites this to
        # a /data URL via yas_video_editor._resolve_text_font.
        text_block["fontUrl"] = f"/api/agents/producer/assets/{int(fid)}/file"

    clips.append({
        "id": f"{new_clip_id}_text",
        "trackId": track["id"],
        "kind": "text",
        "trimBefore": 0,
        "trimAfter": text_dur,
        "from": text_from,
        "durationInFrames": text_dur,
        "text": text_block,
        "keyframes": _popup_keyframes(op.get("anim"), text_dur, x=x, y=text_y, base_scale=1.0),
    })


def _op_add_sticker(project: dict, tracks: list, clips: list, op: dict, new_clip_id: str) -> None:
    # Image asset overlay on the AI video lane (contain-fit; sized via scale
    # keyframes like the editor's own sticker drop), animated like a popup.
    track = _ensure_ai_video_track(tracks)
    start = _req_f(op.get("from"), "add_sticker.from")
    dur = _opt_dur(op.get("durationInFrames"), _DEFAULT_STICKER_F)
    # x/y missing/None → canvas center (0, 0); scale missing/None → 0.45 (a
    # tasteful overlay size — a full-frame asset at 0.6 read as oversized).
    x, y = _title_safe_xy(op.get("x") or 0, op.get("y") or 0, project)
    raw_scale = op.get("scale")
    base_scale = _clamp(_num(raw_scale, "scale"), 0.1, 4.0) if raw_scale is not None else 0.45

    clips.append({
        "id": new_clip_id,
        "trackId": track["id"],
        "kind": "image",
        "source": {"kind": "asset", "asset_id": int(op["asset_id"])},
        "trimBefore": 0,
        "trimAfter": dur,
        "from": start,
        "durationInFrames": dur,
        "fit": "contain",
        "keyframes": _popup_keyframes(op.get("anim"), dur, x=x, y=y, base_scale=base_scale),
    })


def _op_set_keyframes(project: dict, tracks: list, clips: list, op: dict, new_clip_id: str) -> None:
    # Replace one property's keyframe lane (sanitized: sorted, deduped, clamped).
    clip = _find_clip(clips, op["clip_id"])
    prop = op["property"]
    kf = dict(clip.get("keyframes") or {})
    kf[prop] = _sanitize_keyframes(op["points"], _clip_dur(clip), prop)
    clip["keyframes"] = kf


# Suffixes that mark the two clips of a framed-popup UNIT (added by
# _op_add_framed_popup as {base}_frame and {base}_text).
_UNIT_SUFFIXES = ("_frame", "_text")


def _unit_clip_ids(clip_id: str, clips: list) -> set[str]:
    """All clip ids in the same popup UNIT as clip_id. A framed popup is two
    clips ({base}_frame + {base}_text); everything else is a unit of one."""
    base = clip_id
    for suf in _UNIT_SUFFIXES:
        if clip_id.endswith(suf):
            base = clip_id[: -len(suf)]
            break
    existing = {c.get("id") for c in clips}
    ids = {clip_id}
    for cand in (base, base + "_frame", base + "_text"):
        if cand in existing:
            ids.add(cand)
    return ids


def _scale_lane(lane, factor: float) -> list:
    """Multiply every keyframe value in a lane by `factor` (for scale / x / y)."""
    out: list = []
    for p in (lane or []):
        if isinstance(p, dict) and _is_num(p.get("v")):
            q = dict(p)
            q["v"] = float(p["v"]) * factor
            out.append(q)
    return out


def _op_resize_clip(project: dict, tracks: list, clips: list, op: dict, new_clip_id: str) -> None:
    # ROOT-CAUSE FIX (popup huge + "scale down" doesn't work): resize a clip — or
    # a whole framed-popup UNIT — by a relative FACTOR in ONE op. Each clip renders
    # as `translate(x,y) scale(s)`, so multiplying the scale AND x/y lanes of every
    # unit clip by the same factor shrinks the unit toward canvas-center while
    # keeping the frame + its text perfectly aligned. factor<1 shrinks, >1 grows.
    factor = _clamp(_num(op.get("factor"), "factor"), 0.05, 5.0)
    target = _find_clip(clips, op["clip_id"])  # raises if missing
    unit_ids = _unit_clip_ids(target.get("id"), clips)
    for clip in clips:
        if clip.get("id") not in unit_ids:
            continue
        kf = dict(clip.get("keyframes") or {})
        # Size: scale the existing lane, or set a flat scale when there is none.
        kf["scale"] = _scale_lane(kf["scale"], factor) if kf.get("scale") else [{"f": 0, "v": factor}]
        # Position: scale the offset lanes so the unit shrinks toward center,
        # preserving the frame↔text gap proportionally (centered popups stay put).
        for axis in ("x", "y"):
            if kf.get(axis):
                kf[axis] = _scale_lane(kf[axis], factor)
        clip["keyframes"] = kf


def _op_set_filters(project: dict, tracks: list, clips: list, op: dict, new_clip_id: str) -> None:
    # Merge CSS-filter adjustments into the clip, clamped to sane ranges.
    clip = _find_clip(clips, op["clip_id"])
    filters = dict(clip.get("filters") or {})
    for key, (lo, hi) in _FILTER_CLAMPS.items():
        if op.get(key) is not None:
            filters[key] = _clamp(_num(op[key], key), lo, hi)
    clip["filters"] = filters


def _op_add_music(project: dict, tracks: list, clips: list, op: dict, new_clip_id: str) -> None:
    # Music bed on the AI music lane. The orchestrator already resolved the
    # provider — this op MUST carry a concrete source_upload_id (yas_uploads)
    # or source_asset_id (producer_assets, asset_kind=audio).
    upload_id = op.get("source_upload_id")
    asset_id = op.get("source_asset_id")
    if upload_id is None and asset_id is None:
        raise ValueError("add_music reached the executor without a resolved source "
                         "(source_upload_id / source_asset_id)")

    track = _ensure_ai_music_track(tracks)
    # Single music bed: drop any prior AI music on this lane so a regenerate
    # REPLACES instead of stacking. (User audio lives on the normal audio track.)
    _pre = len(clips)
    clips[:] = [
        c for c in clips
        if not (c.get("trackId") == track["id"] and (c.get("kind") or "audio") == "audio")
    ]
    if len(clips) < _pre:
        log.info("_op_add_music: replaced %d prior music clip(s)", _pre - len(clips))
    # `from` missing/None → 0 (start of timeline).
    start = _to_f(op["from"]) if op.get("from") is not None else 0
    if op.get("durationInFrames") is not None and _to_f(op["durationInFrames"]) >= 1:
        dur = _to_f(op["durationInFrames"])
    else:
        # Default: run from `start` to the end of the current timeline;
        # if the timeline is empty/shorter than `start`, fall back to ~10s.
        dur = _timeline_end(clips) - start
        if dur < 1:
            dur = _DEFAULT_MUSIC_F

    if asset_id is not None:
        source = {"kind": "asset", "asset_id": int(asset_id), "audioFile": True}
    else:
        source = {"kind": "upload", "upload_id": int(upload_id), "audioFile": True}

    # gain missing/None → default bed level.
    raw_gain = op.get("gain")
    bed = _clamp(_num(raw_gain, "gain"), 0.0, 1.0) if raw_gain is not None else _DUCK_BED
    if op.get("duck"):
        volume = _ducking_volume_keyframes(
            clip_from=start,
            duration=dur,
            speech_segments=op.get("speech_segments") or [],
            bed=bed,
        )
    else:
        # No ducking → just the global fade-in/out around the bed level.
        volume = _ducking_volume_keyframes(
            clip_from=start, duration=dur, speech_segments=[], bed=bed,
        )

    clips.append({
        "id": new_clip_id,
        "trackId": track["id"],
        "kind": "audio",
        "source": source,
        "trimBefore": 0,
        "trimAfter": dur,
        "from": start,
        "durationInFrames": dur,
        "keyframes": {"volume": volume},
        "muted": False,
    })


def _op_replace_voice(project: dict, tracks: list, clips: list, op: dict, new_clip_id: str) -> None:
    # Swap a clip's voice: the orchestrator's STS step produced a yas_uploads
    # audio file; lay it on the AI voice lane over the original clip's window
    # and mute the original so only the new voice is heard.
    upload_id = op.get("source_upload_id")
    if upload_id is None:
        raise ValueError("replace_voice reached the executor without a resolved source_upload_id")
    original = _find_clip(clips, op["clip_id"])
    track = _ensure_ai_voice_track(tracks)

    start = _to_f(op["from"]) if op.get("from") is not None else _to_f(original.get("from", 0))
    if op.get("durationInFrames") is not None:
        dur = max(1, _to_f(op["durationInFrames"]))
    else:
        dur = _clip_dur(original)

    clips.append({
        "id": new_clip_id,
        "trackId": track["id"],
        "kind": "audio",
        "source": {"kind": "upload", "upload_id": int(upload_id), "audioFile": True},
        "trimBefore": 0,
        "trimAfter": dur,
        "from": start,
        "durationInFrames": dur,
        "keyframes": {},
        "muted": False,
    })
    original["muted"] = True


def _op_add_sfx(project: dict, tracks: list, clips: list, op: dict, new_clip_id: str) -> None:
    # Short sound effect on the AI SFX lane. The orchestrator resolved the
    # source (a yas_uploads audio file) — this op MUST carry a concrete
    # source_upload_id, exactly like add_music.
    upload_id = op.get("source_upload_id")
    if upload_id is None:
        raise ValueError("add_sfx reached the executor without a resolved source_upload_id")
    track = _ensure_ai_sfx_track(tracks)
    start = _to_f(op["from"]) if op.get("from") is not None else 0
    dur = _opt_dur(op.get("durationInFrames"), _DEFAULT_SFX_F)
    # Gentle fade so the effect never clicks at its edges.
    fade = min(3, max(1, dur // 6))
    vol_pts = [
        {"f": 0, "v": 0.0},
        {"f": fade, "v": 1.0},
        {"f": max(fade, dur - fade), "v": 1.0},
        {"f": dur, "v": 0.0},
    ]
    clips.append({
        "id": new_clip_id,
        "trackId": track["id"],
        "kind": "audio",
        "source": {"kind": "upload", "upload_id": int(upload_id), "audioFile": True},
        "trimBefore": 0,
        "trimAfter": dur,
        "from": start,
        "durationInFrames": dur,
        "keyframes": {"volume": _sanitize_keyframes(vol_pts, dur, "volume")},
        "muted": False,
    })


def _op_trim_clip(project: dict, tracks: list, clips: list, op: dict, new_clip_id: str) -> None:
    # Adjust source in/out points; timeline length follows (dur = after - before).
    clip = _find_clip(clips, op["clip_id"])
    before = _to_f(op["trimBefore"]) if op.get("trimBefore") is not None else _to_f(clip.get("trimBefore", 0))
    after = (
        _to_f(op["trimAfter"]) if op.get("trimAfter") is not None
        else _to_f(clip.get("trimAfter", before + _clip_dur(clip)))
    )
    if after <= before:
        raise ValueError(f"trimAfter ({after}) must be > trimBefore ({before})")
    clip["trimBefore"] = before
    clip["trimAfter"] = after
    clip["durationInFrames"] = after - before


def _op_trim_to_video_end(project: dict, tracks: list, clips: list, op: dict, new_clip_id: str) -> None:
    # Trim a clip (typically the music bed) so it ENDS exactly with the video.
    # Deterministic: the executor computes the video's end itself, so "make the
    # music finish with the video" never depends on the LLM doing frame math.
    # The video end = latest end across REAL video tracks (the AI overlay lane,
    # which holds popups/stickers, is excluded). The clip's source IN-point
    # (trimBefore) is kept; only its length/OUT-point is shortened.
    clip = _find_clip(clips, op["clip_id"])
    video_track_ids = {
        t.get("id") for t in tracks
        if t.get("kind") == "video" and t.get("id") != AI_VIDEO_TRACK_ID
    }
    vend = 0
    for c in clips:
        if c.get("trackId") in video_track_ids:
            vend = max(vend, _to_f(c.get("from", 0)) + _clip_dur(c))
    if vend <= 0:
        raise ValueError("no video clips to match the end to")

    start = _to_f(clip.get("from", 0))
    new_dur = vend - start
    if new_dur < 1:
        raise ValueError("clip starts at or after the video's end — nothing to trim")

    tb = _to_f(clip.get("trimBefore", 0))
    # Never extend past the available source when its natural length is known
    # (trimming can only SHORTEN; it can't invent more audio than the source has).
    natural = (clip.get("source") or {}).get("naturalDurationInFrames")
    if natural is not None:
        avail = _to_f(natural) - tb
        if avail >= 1:
            new_dur = min(new_dur, avail)

    clip["trimBefore"] = tb
    clip["trimAfter"] = tb + new_dur
    clip["durationInFrames"] = new_dur


def _op_split_clip(project: dict, tracks: list, clips: list, op: dict, new_clip_id: str) -> None:
    # Cut one clip into two at a CLIP-LOCAL frame: both halves share the source;
    # trims are adjusted so playback is gapless; keyframes are partitioned at
    # the cut (second half's f values re-based to its own clip-local clock).
    clip = _find_clip(clips, op["clip_id"])
    dur = _clip_dur(clip)
    at = _req_f(op.get("at_f"), "split_clip.at_f")
    if not (1 <= at <= dur - 1):
        raise ValueError(f"at_f ({at}) must be inside the clip (1..{dur - 1})")

    second = copy.deepcopy(clip)
    second["id"] = new_clip_id

    before = _to_f(clip.get("trimBefore", 0))

    # First half keeps the original id; ends at the cut.
    clip["durationInFrames"] = at
    clip["trimAfter"] = before + at
    clip["transitionOut"] = None

    # Second half starts at the cut.
    second["from"] = _to_f(clip.get("from", 0)) + at
    second["durationInFrames"] = dur - at
    second["trimBefore"] = before + at
    second["transitionIn"] = None  # the in-transition stays with the first half

    # Partition each keyframe lane at the cut (boundary frame goes to BOTH
    # halves' nearest side: <= at stays, >= at moves shifted by -at).
    src_kf = clip.get("keyframes") or {}
    first_kf: dict = {}
    second_kf: dict = {}
    for prop, points in src_kf.items():
        if not isinstance(points, list):
            continue
        first_pts = [p for p in points if _to_f(p.get("f", 0)) <= at]
        second_pts = [
            {"f": _to_f(p.get("f", 0)) - at, "v": p.get("v")}
            for p in points if _to_f(p.get("f", 0)) >= at
        ]
        if first_pts:
            first_kf[prop] = _sanitize_keyframes(first_pts, at, prop)
        if second_pts:
            second_kf[prop] = _sanitize_keyframes(second_pts, dur - at, prop)
    clip["keyframes"] = first_kf
    second["keyframes"] = second_kf

    clips.append(second)


def _op_mute_clip(project: dict, tracks: list, clips: list, op: dict, new_clip_id: str) -> None:
    # Toggle a clip's own mute flag (track mutes are user-owned, untouched).
    clip = _find_clip(clips, op["clip_id"])
    clip["muted"] = bool(op.get("muted"))


def _op_delete_clip(project: dict, tracks: list, clips: list, op: dict, new_clip_id: str) -> None:
    # Remove a clip by id (validated to exist). Mutates `clips` in place so
    # apply_plan saves without it. One undo step → an accidental delete is
    # reversible with Ctrl+Z.
    cid = op["clip_id"]
    before = len(clips)
    clips[:] = [c for c in clips if c.get("id") != cid]
    if len(clips) == before:
        raise ValueError(f"clip not found: {cid}")


def _op_set_speed(project: dict, tracks: list, clips: list, op: dict, new_clip_id: str) -> None:
    # Change a clip's playback rate. The source range (trimBefore/trimAfter)
    # stays; the TIMELINE length scales by 1/speed and keyframe times scale to
    # match, so animations stay aligned. The renderer reads clip.playbackRate on
    # the <OffthreadVideo>/<Audio> (1 = normal). Non-video/audio clips (text,
    # image, shape) have no media to re-time, so speed is a no-op for them.
    clip = _find_clip(clips, op["clip_id"])
    speed = _clamp(_num(op.get("speed"), "speed"), 0.25, 4.0)

    old_dur = _clip_dur(clip)
    # Source frames consumed = trimAfter - trimBefore (falls back to old_dur for
    # generated clips that don't carry a trim window).
    tb = _to_f(clip.get("trimBefore", 0))
    ta = _to_f(clip.get("trimAfter", 0))
    src_frames = (ta - tb) if ta > tb else old_dur
    new_dur = max(1, int(round(src_frames / speed)))

    clip["playbackRate"] = speed
    clip["durationInFrames"] = new_dur

    # Scale keyframe times so curves keep their relative position after re-timing.
    ratio = new_dur / old_dur if old_dur else 1.0
    if ratio != 1.0 and isinstance(clip.get("keyframes"), dict):
        kf = dict(clip["keyframes"])
        for prop, lane in list(kf.items()):
            if not isinstance(lane, list):
                continue
            scaled = [{**p, "f": min(new_dur, _to_f(_num(p.get("f"), "f") * ratio))} for p in lane]
            kf[prop] = _sanitize_keyframes(scaled, new_dur, prop)
        clip["keyframes"] = kf


def _op_set_text(project: dict, tracks: list, clips: list, op: dict, new_clip_id: str) -> None:
    # Rewrite an EXISTING text/caption clip's words AND/OR restyle it (font /
    # size / colour) in place — so "change the caption to HELLO 42" rewrites the
    # words, while "use this font" (a dragged-font attachment) changes ONLY the
    # font and KEEPS the words. Text layers only.
    clip = _find_clip(clips, op["clip_id"])
    if (clip.get("kind") or "") != "text":
        raise ValueError(f"set_text targets a text clip; {op['clip_id']!r} is {clip.get('kind')!r}")
    block = dict(clip.get("text") or {})
    if isinstance(op.get("text"), str) and op["text"].strip():
        block["content"] = str(op["text"])  # only rewrite the words when given
    if op.get("color") is not None:
        block["color"] = str(op["color"])
    if op.get("fontFamily") is not None:
        block["fontFamily"] = str(op["fontFamily"])
    if op.get("fontSize") is not None:
        block["fontSize"] = max(12, min(300, _to_f(op["fontSize"])))
    if op.get("font_asset_id") is not None:
        block["fontUrl"] = f"/api/agents/producer/assets/{int(op['font_asset_id'])}/file"
    clip["text"] = block


def _shift_clips_for_intro(clips: list, dur: int) -> None:
    # Make room for a `dur`-frame intro at frame 0 (shared by add_clip and
    # add_title_card). Re-apply idempotency: apply_plan removed this plan's
    # prior clips but can't un-shift, so reuse the leftover head gap — only
    # shift by what's MISSING (never double-shift). The shift is applied to
    # EVERY clip on ALL tracks so audio stays in sync with video. A clip's
    # transitionIn overlap counts toward the gap (the clip's SLOT starts
    # overlap frames after its physical `from`) — otherwise a re-applied
    # intro+fade plan would re-shift by the fade length on every run.
    def _eff_from(c: dict) -> int:
        f = _to_f(c.get("from", 0))
        trans = c.get("transitionIn") or {}
        if trans.get("preset") and trans.get("preset") != "none":
            f += _to_f(trans.get("durationInFrames") or 0)
        return f

    gap = min((_eff_from(c) for c in clips), default=0)
    shift = max(0, dur - gap)
    if shift:
        for c in clips:
            c["from"] = _to_f(c.get("from", 0)) + shift


def _op_add_clip(project: dict, tracks: list, clips: list, op: dict, new_clip_id: str) -> None:
    # Full-screen clip from ALREADY-RESOLVED media at the timeline start
    # (role=intro) or end (role=outro). Clip shape mirrors
    # yas_video_editor.create_project_from_sources; source field naming follows
    # ve._clip_source_kind_id ({kind}_id). Targets the LOWEST-index video track
    # (the primary user lane — never the AI overlay lane).
    role = op.get("role")
    if role not in _ADD_CLIP_ROLES:
        raise ValueError(f"add_clip requires role in {sorted(_ADD_CLIP_ROLES)}: {role!r}")
    src_kind = op.get("source_kind")
    if src_kind not in _ADD_CLIP_SOURCE_KINDS:
        raise ValueError(f"invalid add_clip source_kind: {src_kind!r}")
    sid = int(op["source_id"])
    dur = _req_f(op.get("durationInFrames"), "add_clip.durationInFrames")
    if dur < 1:
        raise ValueError("add_clip durationInFrames must be >= 1")

    user_video_tracks = [
        t for t in tracks
        if t.get("kind") == "video" and t.get("id") != AI_VIDEO_TRACK_ID
    ]
    if not user_video_tracks:
        raise ValueError("no user video track to place the clip on")
    track = min(user_video_tracks, key=lambda t: int(t.get("index", 0) or 0))

    source: dict = {"kind": src_kind, "naturalDurationInFrames": dur}
    source[f"{src_kind}_id"] = sid
    clip: dict = {
        "id": new_clip_id,
        "trackId": track["id"],
        "kind": "image" if src_kind == "still" else "video",
        "source": source,
        "trimBefore": 0,
        "trimAfter": dur,
        "durationInFrames": dur,
        "keyframes": {},
        "muted": False,
        "fit": "cover",
    }

    if role == "intro":
        # Gap-aware shift (see _shift_clips_for_intro for the idempotency
        # rationale) — make room at frame 0, then drop the clip there.
        _shift_clips_for_intro(clips, dur)
        clip["from"] = 0
    else:  # outro
        clip["from"] = _timeline_end(clips)

    clips.append(clip)


def _op_add_media_clip(project: dict, tracks: list, clips: list, op: dict, new_clip_id: str) -> None:
    # Place a media-bin clip the user dragged in. NON-DESTRUCTIVE: never shifts
    # existing clips. overlay=false → full-frame B-roll on its own AI media lane
    # (above base video, below text/stickers); overlay=true → picture-in-picture
    # inset on the overlay lane, sized/positioned like a sticker.
    src_kind = op.get("source_kind")
    if src_kind not in _ADD_MEDIA_SOURCE_KINDS:
        raise ValueError(f"invalid add_media_clip source_kind: {src_kind!r}")
    sid = int(op["source_id"])
    dur = _opt_dur(op.get("durationInFrames"), _DEFAULT_MEDIA_F)
    start = _to_f(op["from"]) if op.get("from") is not None else 0
    source: dict = {"kind": src_kind, "naturalDurationInFrames": dur}
    source[f"{src_kind}_id"] = sid
    kind = "image" if src_kind == "still" else "video"

    if not bool(op.get("overlay")) and op.get("placement") == "append":
        # SEQUENTIAL main-track append (like clicking a bin item): drop full-frame
        # on the primary user video lane, right after its last clip. apply_plan
        # runs ops in order and mutates `clips`, so a batch of appends cascades —
        # each recomputes the track end fresh, no cross-op arithmetic needed.
        track = _main_user_video_track(tracks)
        clip = {
            "id": new_clip_id,
            "trackId": track["id"],
            "kind": kind,
            "source": source,
            "trimBefore": 0,
            "trimAfter": dur,
            "from": _track_end(clips, track["id"]),
            "durationInFrames": dur,
            "fit": "cover",
            "muted": False,
            "keyframes": {},
        }
        clips.append(clip)
        return

    if bool(op.get("overlay")):
        track = _ensure_ai_video_track(tracks)
        x, y = _title_safe_xy(op.get("x") or 0, op.get("y") or 0, project)
        raw_scale = op.get("scale")
        base_scale = _clamp(_num(raw_scale, "scale"), 0.1, 4.0) if raw_scale is not None else 0.4
        clip: dict = {
            "id": new_clip_id,
            "trackId": track["id"],
            "kind": kind,
            "source": source,
            "trimBefore": 0,
            "trimAfter": dur,
            "from": start,
            "durationInFrames": dur,
            "fit": "contain",
            "muted": False,
            "keyframes": _popup_keyframes(op.get("anim"), dur, x=x, y=y, base_scale=base_scale),
        }
    else:
        track = _ensure_ai_media_track(tracks)
        clip = {
            "id": new_clip_id,
            "trackId": track["id"],
            "kind": kind,
            "source": source,
            "trimBefore": 0,
            "trimAfter": dur,
            "from": start,
            "durationInFrames": dur,
            "fit": "cover",
            "muted": False,
            "keyframes": {},
        }
    clips.append(clip)


def _op_add_title_card(project: dict, tracks: list, clips: list, op: dict, new_clip_id: str) -> None:
    # EXECUTOR-ONLY op (deliberately absent from _OP_SCHEMA/_ALL_OPS/
    # ALLOWED_OPS — no LLM feature may emit it): a text-only intro/outro title
    # card on the AI overlay lane, used by the B-roll preset's "light" bookend
    # style. Intro shifts the whole document (same gap-aware math as add_clip);
    # outro appends at the timeline end.
    role = op.get("role")
    if role not in ("intro", "outro"):
        raise ValueError(f"add_title_card requires role in ['intro', 'outro']: {role!r}")
    dur = int(op.get("durationInFrames") or 75)
    headline = str(op.get("text") or "").strip()
    if not headline:
        raise ValueError("add_title_card requires non-empty text")
    subtitle = str(op.get("subtitle") or "").strip()
    content = headline + ("\n" + subtitle if subtitle else "")

    track = _ensure_ai_video_track(tracks)
    if role == "intro":
        _shift_clips_for_intro(clips, dur)
        start = 0
    else:
        start = _timeline_end(clips)

    clips.append({
        "id": new_clip_id,
        "trackId": track["id"],
        "kind": "text",
        "trimBefore": 0,
        "trimAfter": dur,
        "from": start,
        "durationInFrames": dur,
        "text": {
            "content": content,
            "fontFamily": "Inter",
            "fontSize": 72,
            "color": "#FFFFFF",
            "fontWeight": 700,
            "textAlign": "center",
        },
        "keyframes": _popup_keyframes("title", dur, x=0, y=0),
    })


_OP_EXECUTORS = {
    "set_clip_position": _op_set_clip_position,
    "reorder_clips": _op_reorder_clips,
    "add_transition": _op_add_transition,
    "add_text_popup": _op_add_text_popup,
    "add_framed_popup": _op_add_framed_popup,
    "add_sticker": _op_add_sticker,
    "set_keyframes": _op_set_keyframes,
    "resize_clip": _op_resize_clip,
    "set_filters": _op_set_filters,
    "add_music": _op_add_music,
    "replace_voice": _op_replace_voice,
    "add_sfx": _op_add_sfx,
    "trim_clip": _op_trim_clip,
    "trim_to_video_end": _op_trim_to_video_end,
    "split_clip": _op_split_clip,
    "mute_clip": _op_mute_clip,
    "delete_clip": _op_delete_clip,
    "set_speed": _op_set_speed,
    "set_text": _op_set_text,
    "add_clip": _op_add_clip,
    "add_media_clip": _op_add_media_clip,
    # EXECUTOR-ONLY (not in _OP_SCHEMA/_ALL_OPS/ALLOWED_OPS): deterministic
    # callers only (B-roll preset) — the LLM can never plan it.
    "add_title_card": _op_add_title_card,
}


# ---------------------------------------------------------------------------
# Overlay popup grouping + collision resolution
#
# The AI overlay lane (trk_v_ai) is where text popups, framed popups and
# stickers land. Nothing in the op executors avoids collisions, so the planner
# routinely stacks two popups dead-center on the same frames. These helpers run
# at the END of apply_plan to (a) drop exact duplicates, (b) spread popups that
# share a time window into DISTINCT title-safe cells, and (c) cap concurrency to
# <=2. They ONLY touch trk_v_ai, so hand-placed editor captions are never moved.
# group_popups_by_time is also reused by the chatbot to ask "which popup?".
# ---------------------------------------------------------------------------

# Title-safe cell centers for spreading stacked popups, in PREFERRED order
# (most natural reading spots first). Each is (fx, fy) as a fraction of the
# title-safe half-extent (project dim * _TITLE_SAFE); +y is down.
_POPUP_CELL_FRACTIONS = [
    (0.0, 0.78),    # lower third — default caption spot
    (0.0, -0.78),   # upper third
    (0.0, 0.0),     # dead center
    (-0.62, 0.78),  # lower-left
    (0.62, 0.78),   # lower-right
    (-0.62, -0.78),  # upper-left
    (0.62, -0.78),  # upper-right
]


def _overlay_xy(clip: dict) -> tuple[float, float]:
    """The resting (f=0) x/y of an overlay clip, read from its keyframe lanes."""
    kf = clip.get("keyframes") or {}

    def _first(prop: str) -> float:
        lane = kf.get(prop) or []
        for p in lane:
            try:
                if _to_f(p.get("f")) == 0:
                    return _num(p.get("v"), prop)
            except (ValueError, TypeError):
                continue
        if lane:
            try:
                return _num(lane[0].get("v"), prop)
            except (ValueError, TypeError):
                return 0.0
        return 0.0

    return _first("x"), _first("y")


def _shift_overlay_xy(clip: dict, dx: float, dy: float) -> None:
    """Translate every x/y keyframe of an overlay clip by (dx, dy) so the whole
    animation moves rigidly to a new cell (relative motion preserved)."""
    if not dx and not dy:
        return
    kf = dict(clip.get("keyframes") or {})
    for prop, d in (("x", dx), ("y", dy)):
        if not d:
            continue
        lane = [dict(p) for p in (kf.get(prop) or [])]
        if lane:
            for p in lane:
                try:
                    p["v"] = _num(p.get("v"), prop) + d
                except (ValueError, TypeError):
                    p["v"] = d
        else:
            lane = [{"f": 0, "v": d}]
        kf[prop] = lane
    clip["keyframes"] = kf


def _unit_base_id(clip_id: str) -> str:
    """Framed popups append _frame/_text to a shared base id — collapse those two
    clips into ONE logical unit so the resolver never splits a frame from its
    caption."""
    for suf in ("_frame", "_text"):
        if clip_id.endswith(suf):
            return clip_id[: -len(suf)]
    return clip_id


def _overlay_unit_label(primary: dict, frm: int) -> str:
    secs = frm // FPS
    t = f"{secs // 60}:{secs % 60:02d}"
    if primary.get("kind") == "text":
        txt = ((primary.get("text") or {}).get("content") or "").strip()
        txt = (txt[:24] + "…") if len(txt) > 24 else txt
        return f'text "{txt}" @{t}' if txt else f"text @{t}"
    return f"sticker @{t}"


def _overlay_unit_sig(members: list[dict]) -> str:
    """Content signature for exact-duplicate detection (content + start frame)."""
    parts: list[str] = []
    for m in sorted(members, key=lambda c: str(c.get("id"))):
        if m.get("kind") == "text":
            parts.append("t:" + ((m.get("text") or {}).get("content") or "").strip().lower())
        else:
            parts.append("i:" + str((m.get("source") or {}).get("asset_id")))
        parts.append(f"@{_to_f(m.get('from', 0))}")
    return "|".join(parts)


def _overlay_content_key(members: list[dict]) -> str:
    """Content-only key (NO frame) — collapses the SAME sticker/text added twice
    within one command, even when the planner placed them at different times."""
    parts: list[str] = []
    for m in sorted(members, key=lambda c: str(c.get("id"))):
        if m.get("kind") == "text":
            parts.append("t:" + ((m.get("text") or {}).get("content") or "").strip().lower())
        else:
            parts.append("i:" + str((m.get("source") or {}).get("asset_id")))
    return "|".join(parts)


def _overlay_units(clips: list[dict]) -> list[dict]:
    """Logical overlay units on the AI video lane: a standalone text/sticker is
    one unit; a framed popup's frame+text are one unit. Each carries its time
    span, anchor x/y, a short human label and a content signature."""
    by_base: dict[str, list[dict]] = {}
    order: list[str] = []
    for c in clips:
        if c.get("trackId") != AI_VIDEO_TRACK_ID:
            continue
        base = _unit_base_id(str(c.get("id") or ""))
        if base not in by_base:
            by_base[base] = []
            order.append(base)
        by_base[base].append(c)

    units: list[dict] = []
    for base in order:
        members = by_base[base]
        frm = min(_to_f(m.get("from", 0)) for m in members)
        end = max(_to_f(m.get("from", 0)) + _clip_dur(m) for m in members)
        primary = next((m for m in members if str(m.get("id", "")).endswith("_frame")), members[0])
        ax, ay = _overlay_xy(primary)
        units.append({
            "base": base, "members": members, "primary": primary,
            "from": frm, "end": end, "x": ax, "y": ay,
            "label": _overlay_unit_label(primary, frm),
            "sig": _overlay_unit_sig(members),
        })
    units.sort(key=lambda u: (u["from"], u["base"]))
    return units


def group_popups_by_time(clips: list[dict]) -> list[dict]:
    """Cluster overlay UNITS that overlap in time into logical 'popups' (a text
    and a sticker shown together count as one popup). Returns
    [{index, from, end, units, label}] sorted by start. Used by the chatbot to
    ask 'which popup?' and by resolve_overlay_collisions to de-collide."""
    groups: list[dict] = []
    for u in _overlay_units(clips):
        placed = None
        for g in groups:
            if u["from"] < g["end"] and u["end"] > g["from"]:
                placed = g
                break
        if placed is None:
            groups.append({"from": u["from"], "end": u["end"], "units": [u]})
        else:
            placed["from"] = min(placed["from"], u["from"])
            placed["end"] = max(placed["end"], u["end"])
            placed["units"].append(u)

    out: list[dict] = []
    for i, g in enumerate(groups, start=1):
        out.append({
            "index": i, "from": g["from"], "end": g["end"], "units": g["units"],
            "label": ", ".join(u["label"] for u in g["units"]),
        })
    return out


def resolve_overlay_collisions(
    tracks: list[dict], clips: list[dict], project: dict, new_prefix: Optional[str] = None,
    faces: Optional[list] = None,
) -> int:
    """End-of-plan safety net for AI overlays. It de-collides only the clips ADDED
    by THIS plan (id starts with `new_prefix`); clips that were already on the
    timeline are FIXED ANCHORS that are never moved or dropped — so adding a
    sticker never shifts the user's existing text/popups. For the new clips it:
    (A) drops a new clip that exactly duplicates any clip, or that repeats another
    new clip's content (the planner double-added the same item); (B) caps
    concurrency to <=2 by pushing surplus NEW clips later; (C) moves a NEW clip
    that lands on top of an existing/earlier one to a free title-safe cell.
    `new_prefix=None` makes every clip movable (legacy behaviour). Returns the
    number of clips changed/removed."""
    w = float(project.get("width") or 1080)
    h = float(project.get("height") or 1920)
    tx = w * _TITLE_SAFE
    ty = h * _TITLE_SAFE
    cells = [(_clamp(fx * tx, -tx, tx), _clamp(fy * ty, -ty, ty)) for fx, fy in _POPUP_CELL_FRACTIONS]
    near = 0.16 * min(w, h)   # centers closer than this read as "stacked"
    near_sq = near * near
    changed = 0

    def _is_new(u: dict) -> bool:
        return (not new_prefix) or str(u.get("base", "")).startswith(new_prefix)

    # Phase A — dedup. Only NEW clips are ever dropped:
    #  • a NEW clip that EXACTLY duplicates any clip (same content + same frame), or
    #  • a NEW clip whose content repeats an earlier NEW clip in THIS command
    #    (the planner added the same sticker/text twice).
    units = _overlay_units(clips)
    exact_seen: set[str] = {u["sig"] for u in units if not _is_new(u)}
    new_content_seen: set[str] = set()
    drop_ids: set[str] = set()
    for u in units:
        if not _is_new(u):
            continue
        ckey = _overlay_content_key(u["members"])
        if u["sig"] in exact_seen or ckey in new_content_seen:
            drop_ids.update(str(m.get("id")) for m in u["members"])
        else:
            exact_seen.add(u["sig"])
            new_content_seen.add(ckey)
    if drop_ids:
        pre = len(clips)
        clips[:] = [c for c in clips if str(c.get("id")) not in drop_ids]
        changed += pre - len(clips)

    # Phase B — cap to <=2 concurrent: push only surplus NEW units later in time
    # (existing clips are never moved).
    for group in group_popups_by_time(clips):
        gunits = sorted(group["units"], key=lambda u: u["from"])
        if len(gunits) <= 2:
            continue
        anchor_end = min(u["end"] for u in gunits[:2])
        for u in gunits[2:]:
            if not _is_new(u):
                continue
            delta = anchor_end - u["from"]
            if delta > 0:
                for m in u["members"]:
                    m["from"] = _to_f(m.get("from", 0)) + delta
                changed += 1

    # Phase C — spread overlapping units to distinct cells. EXISTING units are
    # fixed anchors (their spots are reserved); only NEW units relocate to a free
    # cell, so a freshly-added sticker dodges the existing layout instead of
    # bumping it.
    for group in group_popups_by_time(clips):
        gunits = group["units"]
        if len(gunits) < 2:
            continue
        existing = [u for u in gunits if not _is_new(u)]
        new_units = [u for u in gunits if _is_new(u)]
        placed: list[tuple[float, float]] = [(u["x"], u["y"]) for u in existing]
        used_cells: set[int] = set()
        for u in new_units:
            ux, uy = u["x"], u["y"]
            collide = any((ux - px) ** 2 + (uy - py) ** 2 < near_sq for px, py in placed)
            if collide:
                ci = next(
                    (i for i in range(len(cells))
                     if i not in used_cells
                     and all((cells[i][0] - px) ** 2 + (cells[i][1] - py) ** 2 >= near_sq for px, py in placed)),
                    None,
                )
                if ci is not None:
                    used_cells.add(ci)
                    cx, cy = cells[ci]
                    for m in u["members"]:
                        _shift_overlay_xy(m, cx - u["x"], cy - u["y"])
                    u["x"], u["y"] = cx, cy
                    changed += 1
            placed.append((u["x"], u["y"]))

    # Phase D — FACE AVOIDANCE (root-cause fix for "popups land on the talking
    # head's face"): the default popup position is canvas-center (0,0), exactly
    # where a centered face is. `faces` are center-relative px boxes from the
    # analysis. Any NEW popup whose center sits on a face is relocated to the
    # nearest face-free title-safe spot — lower-third first (the caption zone).
    if faces:
        def _on_face(px: float, py: float) -> bool:
            for (fcx, fcy, fw, fh) in faces:
                if abs(px - fcx) <= fw / 2.0 + 0.04 * w and abs(py - fcy) <= fh / 2.0 + 0.04 * h:
                    return True
            return False
        prefs = [(0.0, 0.30 * h), (0.0, 0.38 * h), (0.0, -0.34 * h), *cells]
        prefs = [(_clamp(cx, -tx, tx), _clamp(cy, -ty, ty)) for cx, cy in prefs]
        used: list[tuple[float, float]] = []
        for u in _overlay_units(clips):
            if not _is_new(u) or not _on_face(u["x"], u["y"]):
                continue
            dest = next(
                ((cx, cy) for cx, cy in prefs
                 if not _on_face(cx, cy)
                 and all((cx - px) ** 2 + (cy - py) ** 2 >= near_sq for px, py in used)),
                None,
            )
            if dest is not None:
                cx, cy = dest
                for m in u["members"]:
                    _shift_overlay_xy(m, cx - u["x"], cy - u["y"])
                u["x"], u["y"] = cx, cy
                used.append((cx, cy))
                changed += 1

    return changed


# ---------------------------------------------------------------------------
# 4) apply_plan — the executor
# ---------------------------------------------------------------------------

def _safe_plan_id(plan_id: str) -> str:
    """plan_id is embedded in clip ids — keep it filesystem/JSON-path safe."""
    return re.sub(r"[^A-Za-z0-9_-]", "_", str(plan_id))[:64] or "plan"


async def apply_plan(project_id: int, *, plan_id: str, ops: list[dict],
                     analysis: Optional[dict] = None) -> dict:
    """Apply a validated, fully-resolved op list to a video-editor project.

    - Re-reads the CURRENT timeline (never applies onto stale state).
    - Idempotent per plan: clips with ids `clip_ai_{plan_id}_*` from a previous
      run of the same plan are removed first; new clips get deterministic ids
      `clip_ai_{plan_id}_{op_index}`.
    - Ops apply IN ORDER; each failure is recorded and the plan continues.
    - Persists with EXACTLY ONE save_timeline call (rev bump + checkpoint +
      render preservation + duration re-derive all happen there).

    Returns {"plan_id", "applied", "failed": [{index, op, error}], "rev"}.
    """
    pid = _safe_plan_id(plan_id)
    prefix = f"clip_ai_{pid}_"

    # Fresh read so we never clobber concurrent editor autosaves.
    row = await ve._fetch_row(project_id)
    timeline = ve._video_edit(row["metadata"]) or ve._empty_timeline(
        row.get("topic") or "Untitled project", ve.DEFAULT_ASPECT
    )
    project: dict = dict(timeline.get("project") or {})
    tracks: list[dict] = copy.deepcopy(timeline.get("tracks") or [])
    clips: list[dict] = copy.deepcopy(timeline.get("clips") or [])

    # IDEMPOTENCY: drop this plan's previous output before re-applying.
    pre = len(clips)
    clips = [c for c in clips if not str(c.get("id", "")).startswith(prefix)]
    if pre != len(clips):
        log.info("apply_plan project=%s plan=%s: removed %d clip(s) from a prior run",
                 project_id, pid, pre - len(clips))

    applied = 0
    failed: list[dict] = []
    for index, op in enumerate(ops or []):
        name = op.get("op") if isinstance(op, dict) else None
        executor = _OP_EXECUTORS.get(name or "")
        try:
            if executor is None:
                raise ValueError(f"unknown op: {name!r}")
            executor(project, tracks, clips, op, f"{prefix}{index}")
            applied += 1
        except Exception as exc:  # per-op isolation — the plan continues
            log.warning("apply_plan project=%s plan=%s op[%d]=%s failed: %s",
                        project_id, pid, index, name, exc)
            failed.append({"index": index, "op": name, "error": str(exc)})

    # Face boxes (center-relative px) from the analysis keep new popups off the
    # talking head — passed into the overlay resolver's face-avoidance phase.
    faces = None
    if analysis:
        try:
            from .yas_video_edit_ai_analysis import face_px_boxes
            faces = face_px_boxes(
                analysis, float(project.get("width") or 1080),
                float(project.get("height") or 1920),
            )
        except Exception:  # face data is best-effort — never block a plan
            faces = None

    # Overlay safety net: de-collide/de-dup AI popups so two never stack on the
    # same frames (runs for EVERY plan — popups feature, edit, chatbot, captions).
    try:
        adjusted = resolve_overlay_collisions(
            tracks, clips, project, new_prefix=prefix, faces=faces)
        if adjusted:
            log.info("apply_plan project=%s plan=%s: overlay resolver adjusted %d clip(s)",
                     project_id, pid, adjusted)
    except Exception as exc:  # never let de-collision break a valid plan
        log.warning("apply_plan project=%s plan=%s: overlay resolver failed (non-fatal): %s",
                    project_id, pid, exc)

    # ONE sanctioned write: save_timeline bumps rev, appends a checkpoint,
    # preserves the render sub-state, and re-derives durationInFrames. Tag the
    # write as an AI edit + summarize the applied ops for the project memory's
    # edit log (so a later "scale that popup down" can resolve the right clip).
    applied_ops = [op for i, op in enumerate(ops or [])
                   if not any(f.get("index") == i for f in failed)]
    op_counts: dict[str, int] = {}
    for op in applied_ops:
        n = (op.get("op") if isinstance(op, dict) else None) or "?"
        op_counts[n] = op_counts.get(n, 0) + 1
    ai_summary = "AI: " + (", ".join(f"{n}×{c}" if c > 1 else n
                                     for n, c in op_counts.items()) or "no-op")
    saved = await ve.save_timeline(
        project_id, project=project, tracks=tracks, clips=clips,
        actor="ai", change_summary=ai_summary,
    )
    rev = int((saved.get("history") or {}).get("rev", 0))

    log.info("apply_plan project=%s plan=%s: applied=%d failed=%d rev=%d",
             project_id, pid, applied, len(failed), rev)
    return {"plan_id": plan_id, "applied": applied, "failed": failed, "rev": rev}
