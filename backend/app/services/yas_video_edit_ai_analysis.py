"""Yas Producer — Video Editor: AI media analysis (planner groundwork).

This is the media-ANALYSIS stage of the AI video-editor engine. For every
unique media source referenced by a video-editor project's clips it builds a
compact, machine-usable description:

  VIDEO  → scene ranges (PySceneDetect), a LENGTH-SCALED set of mid-scene
           JPEG frames on disk (≈one sample per `VE_AI_FRAME_EVERY_SEC`
           seconds, clamped to [MIN,MAX]_FRAMES_PER_SOURCE), a Whisper
           transcript (Gemini fallback), and ONE Gemini multimodal pass over
           the frames (scene summaries, 3×3 occupancy "safe grid", face
           boxes, mood/palette/speech). That visual pass is the editor's
           EYES, so it runs on the Pro reasoning model with a real thinking
           budget — everything downstream (popup placement, look decisions,
           the agent's `look` tool) is only as good as what it saw.
  AUDIO  → duration + mood tags (reusing the Assets-Library `ai_description`
           when the source is a producer_assets row — never recomputed).

Everything is keyed in SOURCE-LOCAL integer frames @ 30fps (the timeline
clock). `build_project_digest` re-maps source frames into TIMELINE frames per
clip trim window for the planner; `candidate_popup_windows` turns the safe
grid + transcript into concrete, label-led popup placements so the planning
LLM picks rather than computes geometry.

STORAGE — CRITICAL: `save_timeline` rebuilds `metadata.video_edit` wholesale
on every frontend autosave, so the analysis cache lives at the SIBLING key
`metadata.video_edit_ai.analysis` (written via jsonb_set, parent ensured).
Image bytes never enter JSONB — sampled frames go to disk under
`VE_ROOT/ai/{kind}_{id}/scene_{i}.jpg`.

Best-effort throughout: any single failing source or external call degrades
to a partial entry with an `errors` list; `get_or_build_analysis` never
raises for one bad source.

TUNING (all optional env; defaults come from `config` and are what ships):
  VE_AI_VISUAL_MODEL            default `config.vertex_gemini_model` (Pro)
  VE_AI_VISUAL_THINKING_BUDGET  default 4096 thinking tokens
  VE_AI_VISUAL_MAX_TOKENS       default 8000 JSON tokens (thinking added on top)
  VE_AI_SCHEMA_MODEL            default `config.vertex_gemini_fast_model` (flash)
                                — the cheap transcript-fallback / audio-mood calls
  VE_AI_SCHEMA_THINKING_BUDGET  default 0 (off, as before)
  VE_AI_MIN_FRAMES_PER_SOURCE   default 8
  VE_AI_MAX_FRAMES_PER_SOURCE   default 24 (hard ceiling on cost)
  VE_AI_FRAME_EVERY_SEC         default 3.0 (sampling density before clamping)
Changing any of these does NOT invalidate cached analyses — bump
ANALYSIS_VERSION or re-run with `force=True` to re-analyze existing projects.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from psycopg.types.json import Jsonb

from ..config import config
from ..db import get_conn
from . import yas_video_editor as ve
from .openai_client import transcribe
from .vertex_client import gemini_multimodal

log = logging.getLogger("archivist.yas_ve_ai")


# ---------------------------------------------------------------------------
# Constants / guards
# ---------------------------------------------------------------------------

def _env_int(name: str, default: int) -> int:
    """Env override that never breaks import (bad value → default)."""
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        log.warning("[yas-ve-ai] %s=%r is not an integer — using %d", name, raw, default)
        return default


def _env_float(name: str, default: float) -> float:
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        log.warning("[yas-ve-ai] %s=%r is not a number — using %.1f", name, raw, default)
        return default


def _env_str(name: str, default: str) -> str:
    return (os.getenv(name) or "").strip() or default


# BUMP THIS whenever the analysis gets better/different: it is the ONLY
# cache-buster. `get_or_build_analysis` drops the cached `sources` map wholesale
# when the stored version differs, so without a bump every existing project
# keeps silently reusing the older, worse analysis forever.
#   1 → flash + thinking_budget=0 visual pass, flat 12 frames/source.
#   2 → Pro reasoning visual pass (thinking budget) + length-scaled sampling.
ANALYSIS_VERSION = 2

FPS = ve.FPS  # 30 — single source of truth for the timeline clock

MAX_SCENES_PER_SOURCE = 64

# --- Frame sampling budget -------------------------------------------------
# A flat cap made long sources as blind as short ones (a 90s interview and a 3s
# sting both got 12 frames). Budget now SCALES with source length — roughly one
# sample every `_FRAME_EVERY_SEC` seconds — floored so even a short clip gets a
# usable spread and hard-ceilinged so Gemini cost/latency stays bounded.
MIN_FRAMES_PER_SOURCE = max(1, _env_int("VE_AI_MIN_FRAMES_PER_SOURCE", 8))
MAX_FRAMES_PER_SOURCE = max(MIN_FRAMES_PER_SOURCE, _env_int("VE_AI_MAX_FRAMES_PER_SOURCE", 24))
_FRAME_EVERY_SEC = max(0.5, _env_float("VE_AI_FRAME_EVERY_SEC", 3.0))

MAX_FRAME_LONG_EDGE = 720
MAX_TRANSCRIPT_SEGMENTS = 400

# Gemini model for the CHEAP structured-schema calls (transcript fallback,
# audio mood) — these are extraction, not reasoning.
_SCHEMA_MODEL = _env_str("VE_AI_SCHEMA_MODEL", config.vertex_gemini_fast_model or "gemini-2.5-flash")
# Thinking budget for those cheap calls: 0 (off) by default, still overridable.
_SCHEMA_THINKING_BUDGET = max(0, _env_int("VE_AI_SCHEMA_THINKING_BUDGET", 0))

# The VISUAL pass is the editor's eyes — it gets the Pro reasoning model and a
# real thinking budget. Overridable so a cost-constrained deployment can pin
# VE_AI_VISUAL_MODEL back to flash (bump ANALYSIS_VERSION-cached entries with
# `force=True` if you change it mid-project).
_VISUAL_MODEL = _env_str("VE_AI_VISUAL_MODEL", config.vertex_gemini_model or "gemini-2.5-pro")
_VISUAL_THINKING_BUDGET = max(0, _env_int("VE_AI_VISUAL_THINKING_BUDGET", 4096))
# Pro spends thinking tokens out of max_output_tokens, so the JSON budget has
# to sit ON TOP of the thinking budget or the schema response truncates and
# `_parse_response` hands back an unparseable `_raw` fragment.
_VISUAL_JSON_TOKENS = max(2000, _env_int("VE_AI_VISUAL_MAX_TOKENS", 8000))

# Title-safe inset for popup placement (fraction of project dimension).
_SAFE_INSET = 0.05


# ---------------------------------------------------------------------------
# Gemini JSON schemas (Vertex style — mirrors asset_analyzer.py)
# ---------------------------------------------------------------------------

VISUAL_SCHEMA: dict = {
    "type": "OBJECT",
    "properties": {
        "scenes": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "index": {"type": "INTEGER"},
                    "summary": {"type": "STRING"},
                    "energy": {"type": "STRING", "enum": ["low", "medium", "high"]},
                    "dominant_subject": {"type": "STRING"},
                },
                "required": ["index", "summary", "energy", "dominant_subject"],
            },
        },
        "safe_grid": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "frame_f": {"type": "INTEGER"},
                    "cells": {
                        "type": "ARRAY",
                        "items": {
                            "type": "OBJECT",
                            "properties": {
                                "occupied": {"type": "BOOLEAN"},
                                "what": {"type": "STRING"},
                            },
                            "required": ["occupied", "what"],
                        },
                    },
                },
                "required": ["frame_f", "cells"],
            },
        },
        "faces": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "frame_f": {"type": "INTEGER"},
                    "x": {"type": "NUMBER"},
                    "y": {"type": "NUMBER"},
                    "w": {"type": "NUMBER"},
                    "h": {"type": "NUMBER"},
                },
                "required": ["frame_f", "x", "y", "w", "h"],
            },
        },
        "mood": {"type": "STRING"},
        "palette": {"type": "ARRAY", "items": {"type": "STRING"}},
        "speech_presence": {"type": "BOOLEAN"},
    },
    "required": ["scenes", "safe_grid", "faces", "mood", "palette", "speech_presence"],
}

# Whisper-fallback: Gemini listens to the mp3 directly and timestamps segments.
_TRANSCRIPT_SCHEMA: dict = {
    "type": "OBJECT",
    "properties": {
        "segments": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "start_sec": {"type": "NUMBER"},
                    "end_sec": {"type": "NUMBER"},
                    "text": {"type": "STRING"},
                },
                "required": ["start_sec", "end_sec", "text"],
            },
        },
    },
    "required": ["segments"],
}

# Mood tags for uploaded music with no Assets-Library description to reuse.
_AUDIO_MOOD_SCHEMA: dict = {
    "type": "OBJECT",
    "properties": {
        "mood": {"type": "STRING"},
        "tags": {"type": "ARRAY", "items": {"type": "STRING"}},
    },
    "required": ["mood", "tags"],
}


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _progress(on_progress: Optional[Callable[[str], Any]], msg: str) -> None:
    if on_progress is None:
        return
    try:
        on_progress(msg)
    except Exception:  # noqa: BLE001 — status callbacks must never break analysis
        pass


def _content_hash(path: Path) -> str | None:
    try:
        st = path.stat()
        return f"{st.st_size}:{int(st.st_mtime)}"
    except OSError:
        return None


def _to_frames(sec: float | int | None) -> int:
    try:
        return max(0, int(round(float(sec or 0) * FPS)))
    except (TypeError, ValueError):
        return 0


def _ai_dir(kind: str, source_id: int) -> Path:
    d = ve.VE_ROOT / "ai" / f"{kind}_{source_id}"
    d.mkdir(parents=True, exist_ok=True)
    return d


# ---------------------------------------------------------------------------
# ffmpeg / scenedetect (sync, run via asyncio.to_thread) — patterns copied
# from agents/website_ingest/phases/video.py (deliberately NOT imported).
# ---------------------------------------------------------------------------

def _ffmpeg_path() -> str:
    import imageio_ffmpeg
    return imageio_ffmpeg.get_ffmpeg_exe()


class _NoAudioStream(RuntimeError):
    """Source has no audio stream — a NORMAL outcome (silent Veo clips are
    common), not an analysis error. Callers return an empty transcript
    WITHOUT appending to `errors`, so the source still caches."""


# ffmpeg stderr signatures for "input simply has no audio" (vs a real failure).
_NO_AUDIO_SIGNATURES = (
    "does not contain any stream",   # "Output file #0 does not contain any stream"
    "matches no streams",            # "Stream map '0:a' matches no streams"
)


def _probe_has_audio_sync(video_path: Path) -> bool | None:
    """Cheap audio-stream probe: `ffmpeg -i` dumps stream info to stderr; an
    `Audio:` line means the source has at least one audio stream. Returns
    None when the container itself is unreadable (corrupt/missing) so the
    caller treats the original extract failure as a REAL error."""
    try:
        proc = subprocess.run(
            [_ffmpeg_path(), "-hide_banner", "-i", str(video_path)],
            capture_output=True, text=True, timeout=60,
        )
    except Exception:  # noqa: BLE001 — probe is advisory only
        return None
    stderr = proc.stderr or ""
    if "Input #0" not in stderr:
        return None  # ffmpeg couldn't even open/parse it — not a "silent clip"
    return "Audio:" in stderr


def _extract_audio_mp3_sync(video_path: Path, dest: Path) -> Path:
    """Mono 64 kbps mp3 — small enough for Whisper AND Gemini inline.
    Raises _NoAudioStream when the source simply has no audio track."""
    cmd = [
        _ffmpeg_path(), "-y",
        "-i", str(video_path),
        "-vn", "-ac", "1", "-ar", "16000", "-b:a", "64k", "-f", "mp3",
        str(dest),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    if proc.returncode != 0 or not dest.exists() or dest.stat().st_size == 0:
        stderr = proc.stderr or ""
        low = stderr.lower()
        if (
            any(sig in low for sig in _NO_AUDIO_SIGNATURES)
            or _probe_has_audio_sync(video_path) is False
        ):
            raise _NoAudioStream(f"no audio stream in {video_path.name}")
        raise RuntimeError(f"ffmpeg audio extract failed: {stderr[:500]}")
    return dest


def _save_frame_sync(video_path: Path, t_sec: float, dest: Path) -> Path:
    """One JPEG at t_sec, downscaled so the long edge is ≤720px (no upscale)."""
    scale = (
        f"scale='min({MAX_FRAME_LONG_EDGE},iw)':'min({MAX_FRAME_LONG_EDGE},ih)'"
        ":force_original_aspect_ratio=decrease"
    )
    cmd = [
        _ffmpeg_path(), "-y",
        "-ss", f"{max(0.0, t_sec):.3f}",
        "-i", str(video_path),
        "-frames:v", "1",
        "-q:v", "4",
        "-vf", scale,
        str(dest),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    if proc.returncode != 0 or not dest.exists():
        raise RuntimeError(f"ffmpeg frame extract failed: {(proc.stderr or '')[:500]}")
    return dest


def _detect_scene_ranges_sync(video_path: Path, dur_f: int) -> list[list[int]]:
    """[[start_f, end_f], ...] in 30fps frames. Falls back to one full-length
    scene; capped at MAX_SCENES_PER_SOURCE by even resampling."""
    ranges: list[list[int]] = []
    try:
        from scenedetect import detect, ContentDetector
        for start, end in detect(str(video_path), ContentDetector(threshold=27.0)):
            s = _to_frames(start.get_seconds())
            e = _to_frames(end.get_seconds())
            if e > s:
                ranges.append([s, e])
    except Exception as e:  # noqa: BLE001
        log.debug("[yas-ve-ai] scene detect failed (%s); using full-length fallback", e)
    if not ranges:
        ranges = [[0, max(1, dur_f)]]
    if len(ranges) > MAX_SCENES_PER_SOURCE:
        step = len(ranges) / MAX_SCENES_PER_SOURCE
        ranges = [ranges[int(i * step)] for i in range(MAX_SCENES_PER_SOURCE)]
    return ranges


def _frame_budget(dur_f: int) -> int:
    """How many frames THIS source deserves: ~one per `_FRAME_EVERY_SEC`
    seconds, clamped to [MIN_FRAMES_PER_SOURCE, MAX_FRAMES_PER_SOURCE]. Length
    scaling is the point — a 90s interview needs more eyes than a 3s sting —
    and the ceiling keeps the Gemini bill bounded."""
    secs = max(0.0, dur_f / float(FPS))
    n = int(secs // _FRAME_EVERY_SEC) + 1
    return max(MIN_FRAMES_PER_SOURCE, min(MAX_FRAMES_PER_SOURCE, n))


def _scene_index_for(ranges: list[list[int]], f: int) -> int:
    """Scene index containing source frame `f` (0 when nothing contains it) —
    keeps interval-sampled frames attributed to the right scene."""
    for i, r in enumerate(ranges):
        if r[0] <= f < r[1]:
            return i
    return 0


def _sample_frame_numbers(ranges: list[list[int]], dur_f: int) -> list[tuple[int, int]]:
    """(scene_index, frame_f) sample points, capped at `_frame_budget(dur_f)`
    (≤ MAX_FRAMES_PER_SOURCE) — scene midpoints, evenly thinned when there are
    more scenes than budget, topped up with evenly spaced interval samples when
    there are fewer (a 60s two-scene take must not be described by two JPEGs)."""
    budget = _frame_budget(dur_f)
    mids = [(i, (r[0] + r[1]) // 2) for i, r in enumerate(ranges)]
    if len(mids) > budget:
        step = len(mids) / budget
        mids = [mids[int(i * step)] for i in range(budget)]
    elif len(mids) < budget and dur_f > 3 * FPS:
        # Sparse scene info (one long take, or a couple of very long scenes) →
        # sample across the duration too. Existing midpoints win; candidates
        # that land on top of one are skipped, and we never exceed the budget
        # so the de-dup pass below can't bias the set toward the clip's start.
        have = {f for _, f in mids}
        min_gap = max(1, int(dur_f / (budget * 2)))
        interval = dur_f / budget
        for i in range(budget):
            if len(mids) >= budget:
                break
            f = int(interval * (i + 0.5))
            if any(abs(f - g) < min_gap for g in have):
                continue
            have.add(f)
            mids.append((_scene_index_for(ranges, f), f))
        mids.sort(key=lambda t: t[1])
    # de-dup + clamp into [0, dur_f)
    seen: set[int] = set()
    out: list[tuple[int, int]] = []
    for si, f in mids:
        f = max(0, min(f, max(0, dur_f - 1)))
        if f not in seen:
            seen.add(f)
            out.append((si, f))
    return out[:budget]


# ---------------------------------------------------------------------------
# DB — cache read/write at metadata.video_edit_ai.analysis (SIBLING of
# video_edit: save_timeline rebuilds video_edit wholesale, so anything under
# it would be dropped on the next autosave).
# ---------------------------------------------------------------------------

async def _fetch_project_meta(project_id: int) -> dict:
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT id, metadata FROM yas_generations WHERE id = %s AND mode = %s",
                (project_id, ve.MODE_PREVIEW),
            )
            row = await cur.fetchone()
    if not row:
        raise ValueError(f"video-editor project #{project_id} not found")
    md = row.get("metadata") or {}
    if isinstance(md, str):
        try:
            md = json.loads(md)
        except Exception:  # noqa: BLE001
            md = {}
    return md if isinstance(md, dict) else {}


def _cached_analysis(md: dict) -> dict:
    ai = md.get("video_edit_ai")
    if isinstance(ai, str):
        try:
            ai = json.loads(ai)
        except Exception:  # noqa: BLE001
            ai = None
    if not isinstance(ai, dict):
        return {}
    analysis = ai.get("analysis")
    return analysis if isinstance(analysis, dict) else {}


async def _store_analysis(project_id: int, analysis: dict) -> None:
    """jsonb_set at {video_edit_ai,analysis}, ensuring the parent object first
    (mirrors yas_video_editor._patch_render — a top-level `||` merge would
    race the autosave path; jsonb_set never touches video_edit)."""
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                UPDATE yas_generations
                   SET metadata = jsonb_set(
                           jsonb_set(metadata, '{video_edit_ai}',
                                     coalesce(metadata->'video_edit_ai', '{}'::jsonb), true),
                           '{video_edit_ai,analysis}', %s::jsonb, true),
                       updated_at = NOW()
                 WHERE id = %s AND mode = %s
                """,
                (Jsonb(analysis), project_id, ve.MODE_PREVIEW),
            )


async def _fetch_audio_asset_meta(asset_id: int) -> dict | None:
    """Assets-Library row (ai_description/attributes already computed by the
    library analyzer — reuse, don't recompute)."""
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT id, asset_kind, ai_description, attributes, duration_sec
                  FROM producer_assets WHERE id = %s
                """,
                (asset_id,),
            )
            row = await cur.fetchone()
    return dict(row) if row else None


# ---------------------------------------------------------------------------
# Source discovery
# ---------------------------------------------------------------------------

def _collect_sources(timeline: dict) -> dict[str, dict]:
    """Unique media sources referenced by the project's clips, keyed
    `f"{kind}:{id}"`, with the media type inferred from the referencing clips
    (clip.kind wins; source kind is the fallback)."""
    out: dict[str, dict] = {}
    for clip in timeline.get("clips") or []:
        kind, sid = ve._clip_source_kind_id(clip)
        if not kind or sid is None:
            continue  # text/shape layers have no media source
        key = f"{kind}:{int(sid)}"
        clip_kind = (clip.get("kind") or "video")
        src = clip.get("source") or {}
        if clip_kind == "audio" or src.get("audioFile"):
            media = "audio"
        elif clip_kind == "image" or kind in ("still",):
            media = "image"
        elif kind == "asset":
            media = "image"  # non-audio assets are stickers/images
        else:
            media = "video"
        prev = out.get(key)
        if prev is None:
            out[key] = {"kind": kind, "id": int(sid), "media": media}
        elif prev["media"] != "video" and media == "video":
            prev["media"] = "video"  # video wins if mixed usage
    return out


# ---------------------------------------------------------------------------
# Per-source analyzers (best-effort; each external call individually guarded)
# ---------------------------------------------------------------------------

async def _transcribe_source(local: Path, ai_dir: Path, errors: list[str]) -> dict:
    """Whisper transcript with a Gemini fallback (OpenAI billing hard-limit is
    a known incident). Returns {"segments":[{start_f,end_f,text}], "text",
    "engine"} — source-local frames."""
    mp3 = ai_dir / "audio.mp3"
    try:
        await asyncio.to_thread(_extract_audio_mp3_sync, local, mp3)
    except _NoAudioStream:
        # Silent clip (e.g. Veo animation) — empty transcript is the CORRECT
        # analysis, not an error: keep `errors` clean so the cache sticks and
        # the source isn't re-analyzed (scenedetect + Gemini) on every call.
        log.info("[yas-ve-ai] %s has no audio stream — empty transcript", local.name)
        return {"segments": [], "text": "", "engine": None}
    except Exception as e:  # noqa: BLE001 — genuine extract failure (retry next call)
        errors.append(f"audio_extract: {e}")
        return {"segments": [], "text": "", "engine": None}

    segments: list[dict] = []
    text = ""
    engine: str | None = None
    try:
        wresp = await transcribe(mp3)
        for seg in (wresp.get("segments") or [])[:MAX_TRANSCRIPT_SEGMENTS]:
            t = (seg.get("text") or "").strip()
            if not t:
                continue
            segments.append({
                "start_f": _to_frames(seg.get("start")),
                "end_f": _to_frames(seg.get("end")),
                "text": t,
            })
        text = (wresp.get("text") or "").strip()
        engine = "whisper"
    except Exception as e:  # noqa: BLE001
        errors.append(f"whisper: {e}")
        log.warning("[yas-ve-ai] Whisper failed (%s); falling back to Gemini", e)
        try:
            result = await gemini_multimodal(
                "Transcribe the speech in this audio file. Return timestamped "
                "segments (start_sec/end_sec in seconds from the start, text). "
                "If there is no speech, return an empty segments array.",
                images=[mp3],  # gemini_multimodal sends any file by MIME
                model=_SCHEMA_MODEL,
                json_schema=_TRANSCRIPT_SCHEMA,
                temperature=0.1,
                max_tokens=4000 + _SCHEMA_THINKING_BUDGET,
                thinking_budget=_SCHEMA_THINKING_BUDGET,
            )
            for seg in ((result or {}).get("segments") or [])[:MAX_TRANSCRIPT_SEGMENTS]:
                t = (seg.get("text") or "").strip()
                if not t:
                    continue
                segments.append({
                    "start_f": _to_frames(seg.get("start_sec")),
                    "end_f": _to_frames(seg.get("end_sec")),
                    "text": t,
                })
            text = " ".join(s["text"] for s in segments)
            engine = "gemini" if segments else None
        except Exception as e2:  # noqa: BLE001
            errors.append(f"gemini_transcript: {e2}")
    return {"segments": segments, "text": text, "engine": engine}


async def _visual_pass(
    frames: list[dict], scenes: list[dict], width: int | None, height: int | None,
    dur_f: int, errors: list[str],
) -> dict:
    """ONE Gemini multimodal call over the sampled JPEGs → scene summaries,
    3×3 safe grid, face boxes, mood/palette/speech.

    This is the editor's EYES: every downstream decision (where a popup can
    land, what the clip is "about", whether a face is in the way) reads only
    what this call returned. It therefore runs on the Pro reasoning model with
    a real thinking budget — occupancy + normalized face boxes are geometry
    judgements that a no-thinking flash pass guesses at. Schema-constrained
    and same return shape as before."""
    if not frames:
        return {}
    paths = [ve._abs_from_rel(f["path_rel"]) for f in frames]
    listing = "\n".join(
        f"  image {i + 1} -> frame_f={f['frame_f']} (scene index {f['scene_index']})"
        for i, f in enumerate(frames)
    )
    prompt = (
        f"You are analyzing {len(frames)} frames sampled from ONE video "
        f"({width or '?'}x{height or '?'} px, {dur_f} frames total at 30 fps).\n"
        f"The attached images correspond, in order, to:\n{listing}\n\n"
        "Return JSON with:\n"
        "1. scenes — ONE entry per distinct scene index listed above: a "
        "1-sentence summary, energy (low/medium/high) and the dominant "
        "subject.\n"
        "2. safe_grid — for EVERY attached frame (use its frame_f from the "
        "list): divide the frame into a 3x3 grid, cells ordered row-major "
        "(0=top-left, 2=top-right, 4=center, 8=bottom-right), exactly 9 "
        "cells, each with occupied=true if any important content (person, "
        "face, product, text, logo) sits in it and a short `what` label "
        "('background', 'sky', 'speaker face', ...).\n"
        "3. faces — every clearly visible face: the frame_f it appears in "
        "and its bounding box as NORMALIZED 0..1 x,y,w,h (x,y = top-left).\n"
        "4. mood (one short phrase), palette (3-5 dominant colors), and "
        "speech_presence (true if someone appears to be talking to camera).\n\n"
        "Look at each image properly before answering: occupancy and face "
        "boxes are used to place on-screen graphics, so a cell you wrongly "
        "call free will cover the subject, and a face box that is off by a "
        "lot will push graphics onto the face. Judge every cell of every "
        "frame individually rather than repeating one frame's answer."
    )
    log.info(
        "[yas-ve-ai] visual pass: %d frame(s) → %s (thinking_budget=%d)",
        len(frames), _VISUAL_MODEL, _VISUAL_THINKING_BUDGET,
    )
    try:
        result = await gemini_multimodal(
            prompt,
            images=paths,
            system=(
                "You are a video-editing vision analyst. You examine sampled "
                "frames and report precise, per-frame observations — layout "
                "occupancy and normalized face geometry above all. Never "
                "invent content you cannot see, and never copy one frame's "
                "answer onto another."
            ),
            model=_VISUAL_MODEL,
            json_schema=VISUAL_SCHEMA,
            temperature=0.2,
            # Thinking tokens come OUT of max_output_tokens on Pro — add the
            # budget on top of the JSON allowance or the schema truncates.
            max_tokens=_VISUAL_JSON_TOKENS + _VISUAL_THINKING_BUDGET,
            thinking_budget=_VISUAL_THINKING_BUDGET,
        )
    except Exception as e:  # noqa: BLE001
        errors.append(f"visual_pass: {e}")
        return {}
    if not isinstance(result, dict) or "_raw" in result:
        errors.append("visual_pass: unparseable response")
        return {}

    # Merge Gemini's scene summaries onto the scenedetect ranges by index.
    for sc in result.get("scenes") or []:
        try:
            idx = int(sc.get("index"))
        except (TypeError, ValueError):
            continue
        if 0 <= idx < len(scenes):
            scenes[idx]["summary"] = (sc.get("summary") or "").strip()
            scenes[idx]["energy"] = sc.get("energy") or "medium"
            scenes[idx]["dominant_subject"] = (sc.get("dominant_subject") or "").strip()
    return {
        "safe_grid": result.get("safe_grid") or [],
        "faces": result.get("faces") or [],
        "mood": (result.get("mood") or "").strip(),
        "palette": result.get("palette") or [],
        "speech_presence": bool(result.get("speech_presence")),
    }


async def _analyze_video_source(
    key: str, kind: str, sid: int, local: Path,
    on_progress: Optional[Callable[[str], Any]],
) -> dict:
    errors: list[str] = []
    dur_sec, width, height = ve._probe_video(local)
    dur_f = max(1, _to_frames(dur_sec))
    entry: dict[str, Any] = {
        "kind": kind, "id": sid, "media": "video",
        "path_rel": ve._to_rel(local),
        "duration_f": dur_f, "width": width, "height": height,
    }
    ai_dir = _ai_dir(kind, sid)

    # 1. Scene ranges (30fps frames)
    _progress(on_progress, f"{key}: detecting scenes…")
    ranges = await asyncio.to_thread(_detect_scene_ranges_sync, local, dur_f)
    scenes: list[dict] = [
        {"index": i, "start_f": r[0], "end_f": r[1],
         "summary": "", "energy": "medium", "dominant_subject": ""}
        for i, r in enumerate(ranges)
    ]

    # 2. Mid-scene frame samples → disk JPEGs (length-scaled count via
    #    _frame_budget, ≤ MAX_FRAMES_PER_SOURCE; long edge ≤720)
    samples = _sample_frame_numbers(ranges, dur_f)
    _progress(on_progress, f"{key}: sampling {len(samples)} frames…")
    frames: list[dict] = []
    for i, (scene_idx, frame_f) in enumerate(samples):
        dest = ai_dir / f"scene_{i}.jpg"
        try:
            await asyncio.to_thread(_save_frame_sync, local, frame_f / FPS, dest)
            frames.append({
                "frame_f": frame_f, "scene_index": scene_idx,
                "path_rel": ve._to_rel(dest),
            })
        except Exception as e:  # noqa: BLE001
            errors.append(f"frame_{i}: {e}")

    # 3. Transcript (Whisper → Gemini fallback)
    _progress(on_progress, f"{key}: transcribing…")
    transcript = await _transcribe_source(local, ai_dir, errors)

    # 4. ONE visual Gemini pass (also fills scene summaries in-place)
    _progress(on_progress, f"{key}: visual analysis…")
    visual = await _visual_pass(frames, scenes, width, height, dur_f, errors)

    entry.update({
        "scenes": scenes,
        "frames": frames,
        "transcript": transcript,
        "visual": visual,
        "errors": errors,
    })
    return entry


async def _analyze_audio_source(key: str, kind: str, sid: int, local: Path) -> dict:
    errors: list[str] = []
    dur_sec = ve._probe_audio_duration(local)
    entry: dict[str, Any] = {
        "kind": kind, "id": sid, "media": "audio",
        "path_rel": ve._to_rel(local),
        "duration_f": max(1, _to_frames(dur_sec)),
        "mood": "", "tags": [], "description": "",
    }
    reused = False
    if kind == "asset":
        try:
            row = await _fetch_audio_asset_meta(sid)
            if row and (row.get("ai_description") or row.get("attributes")):
                attrs = row.get("attributes") or {}
                if isinstance(attrs, str):
                    try:
                        attrs = json.loads(attrs)
                    except Exception:  # noqa: BLE001
                        attrs = {}
                entry["description"] = (row.get("ai_description") or "")[:600]
                entry["mood"] = str(attrs.get("mood") or attrs.get("vibe") or "")
                tags = attrs.get("tags") or attrs.get("genres") or []
                entry["tags"] = [str(t) for t in tags][:8] if isinstance(tags, list) else []
                if not dur_sec and row.get("duration_sec"):
                    entry["duration_f"] = max(1, _to_frames(row["duration_sec"]))
                reused = True
        except Exception as e:  # noqa: BLE001
            errors.append(f"asset_meta: {e}")
    if not reused:
        # Uploaded music with no library description — one cheap mood pass.
        try:
            result = await gemini_multimodal(
                "Describe this music/audio track for a video editor: overall "
                "mood (one short phrase) and 3-6 style tags (genre, tempo "
                "feel, instrumentation).",
                images=[local],
                model=_SCHEMA_MODEL,
                json_schema=_AUDIO_MOOD_SCHEMA,
                temperature=0.2,
                max_tokens=500 + _SCHEMA_THINKING_BUDGET,
                thinking_budget=_SCHEMA_THINKING_BUDGET,
            )
            if isinstance(result, dict) and "_raw" not in result:
                entry["mood"] = (result.get("mood") or "").strip()
                entry["tags"] = [str(t) for t in (result.get("tags") or [])][:8]
        except Exception as e:  # noqa: BLE001
            errors.append(f"audio_mood: {e}")
    entry["errors"] = errors
    return entry


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

async def get_or_build_analysis(
    project_id: int, *, force: bool = False,
    on_progress: Optional[Callable[[str], Any]] = None,
) -> dict:
    """Build (or return the cached) per-source media analysis for a
    video-editor project. Cached at metadata.video_edit_ai.analysis with a
    content hash per source; only changed/new sources are re-analyzed unless
    `force`. Never raises for a single bad source — partial results carry an
    `errors` list per source entry."""
    md = await _fetch_project_meta(project_id)
    timeline = ve._video_edit(md)
    wanted = _collect_sources(timeline)

    cached = {} if force else _cached_analysis(md)
    cached_sources = (cached.get("sources") or {}) if cached.get("version") == ANALYSIS_VERSION else {}

    analysis: dict[str, Any] = {
        "version": ANALYSIS_VERSION,
        "built_at": _now_iso(),
        "sources": {},
    }

    for key, ref in wanted.items():
        kind, sid, media = ref["kind"], ref["id"], ref["media"]
        try:
            local = await ve.resolve_source_local(kind, sid)
            if not local or not local.exists():
                _progress(on_progress, f"{key}: media file unavailable — skipped")
                analysis["sources"][key] = {
                    "kind": kind, "id": sid, "media": media,
                    "errors": ["unresolvable: no local media file"],
                }
                continue
            chash = _content_hash(local)

            prev = cached_sources.get(key)
            if (
                not force and isinstance(prev, dict)
                and chash and prev.get("hash") == chash
                and not prev.get("errors")
            ):
                _progress(on_progress, f"{key}: cached (unchanged)")
                analysis["sources"][key] = prev
                continue

            _progress(on_progress, f"{key}: analyzing {media}…")
            log.info("[yas-ve-ai] project #%d: analyzing %s (%s)", project_id, key, media)
            if media == "video":
                entry = await _analyze_video_source(key, kind, sid, local, on_progress)
            elif media == "audio":
                entry = await _analyze_audio_source(key, kind, sid, local)
            else:
                # Images/stills need no scene/transcript pass — record shape only.
                entry = {
                    "kind": kind, "id": sid, "media": "image",
                    "path_rel": ve._to_rel(local), "errors": [],
                }
            entry["hash"] = chash
            entry["analyzed_at"] = _now_iso()
            analysis["sources"][key] = entry
        except Exception as e:  # noqa: BLE001 — one bad source never sinks the rest
            log.exception("[yas-ve-ai] project #%d: source %s failed", project_id, key)
            analysis["sources"][key] = {
                "kind": kind, "id": sid, "media": media,
                "errors": [f"analysis_failed: {e}"],
            }

    try:
        await _store_analysis(project_id, analysis)
    except Exception:  # noqa: BLE001
        log.exception("[yas-ve-ai] project #%d: cache write failed (returning in-memory result)", project_id)

    _progress(on_progress, f"analysis ready ({len(analysis['sources'])} source(s))")
    return analysis


# ---------------------------------------------------------------------------
# Planner digest — analysis (source frames) → timeline frames
# ---------------------------------------------------------------------------

def _clip_window(clip: dict) -> tuple[int, int, int]:
    """(trim_before, trim_end_exclusive, source→timeline offset).
    Source frame f is visible iff trim_before <= f < trim_end; it appears on
    the timeline at f + offset (offset = from - trimBefore)."""
    try:
        tb = int(clip.get("trimBefore", 0) or 0)
    except (TypeError, ValueError):
        tb = 0
    try:
        dur = int(clip.get("durationInFrames", 0) or 0)
    except (TypeError, ValueError):
        dur = 0
    try:
        frm = int(clip.get("from", 0) or 0)
    except (TypeError, ValueError):
        frm = 0
    return tb, tb + dur, frm - tb


def _clip_source_key(clip: dict) -> str | None:
    kind, sid = ve._clip_source_kind_id(clip)
    return f"{kind}:{int(sid)}" if (kind and sid is not None) else None


def _trunc(s: Any, n: int) -> str:
    s = str(s or "").strip()
    return s if len(s) <= n else s[: n - 1] + "…"


def face_px_boxes(analysis: dict, width: float, height: float) -> list[tuple]:
    """Detected faces across all analysed video sources, as CENTER-RELATIVE pixel
    boxes (cx, cy, w_px, h_px) — matching the popup x/y convention (offset from
    canvas center, +right/+down). Used to keep popups off the talking head's
    face. The talking-head face is stable across a clip, so the union of all
    source faces is a good avoid-zone without per-frame remapping. Empty list
    when no faces (→ no relocation)."""
    boxes: list[tuple] = []
    for src in (analysis or {}).get("sources", {}).values():
        if not isinstance(src, dict):
            continue
        for fc in ((src.get("visual") or {}).get("faces") or []):
            try:
                fx, fy = float(fc["x"]), float(fc["y"])
                fw, fh = float(fc["w"]), float(fc["h"])
            except (KeyError, TypeError, ValueError):
                continue
            cx = (fx + fw / 2.0 - 0.5) * width
            cy = (fy + fh / 2.0 - 0.5) * height
            boxes.append((cx, cy, fw * width, fh * height))
    return boxes


def _creative_blurb(creative: dict, source_mode: str | None) -> str:
    """One-line 'what this clip IS' from its provenance creative metadata."""
    parts: list[str] = []
    if source_mode:
        parts.append(str(source_mode))
    if creative.get("role"):
        parts.append(str(creative["role"]))
    if creative.get("topic"):
        parts.append(f"topic: {_trunc(creative['topic'], 80)}")
    if creative.get("script_hook"):
        parts.append(f"hook: \"{_trunc(creative['script_hook'], 90)}\"")
    elif creative.get("prompt"):
        parts.append(f"prompt: {_trunc(creative['prompt'], 90)}")
    return " · ".join(parts)


def build_project_digest(
    timeline: dict, analysis: dict, roles: dict | None = None,
    memory: dict | None = None,
) -> dict:
    """A compact, planner-facing digest of the project: each clip's table row
    carries its source analysis re-mapped into TIMELINE frames (clip window =
    [trimBefore, trimBefore+durationInFrames) shifted by from-trimBefore).
    Stays well under ~8K tokens (string truncation + per-clip caps).

    `memory` (metadata.video_edit_memory) folds in clip PROVENANCE ("what each
    clip is") per row and a RECENT-EDITS tail ("what's been done") so the AI
    edits with continuity instead of from a blank slate."""
    proj = timeline.get("project") or {}
    tracks_by_id = {t.get("id"): t for t in (timeline.get("tracks") or [])}
    sources = (analysis or {}).get("sources") or {}

    clip_rows: list[dict] = []
    for clip in (timeline.get("clips") or [])[:80]:
        tb, te, offset = _clip_window(clip)
        key = _clip_source_key(clip)
        src_an = sources.get(key) or {}
        track = tracks_by_id.get(clip.get("trackId")) or {}
        row: dict[str, Any] = {
            "id": clip.get("id"),
            "track": track.get("kind") or "video",
            "kind": clip.get("kind") or "video",
            "from": int(clip.get("from", 0) or 0),
            "duration": te - tb,
            "source": key,
        }
        if roles and roles.get(clip.get("id")):
            row["role"] = roles[clip.get("id")]

        # Scenes overlapping the trim window → timeline frames.
        scenes_out: list[dict] = []
        for sc in (src_an.get("scenes") or []):
            s, e = int(sc.get("start_f", 0)), int(sc.get("end_f", 0))
            if e <= tb or s >= te:
                continue
            scenes_out.append({
                "at": max(s, tb) + offset,
                "until": min(e, te) + offset,
                "summary": _trunc(sc.get("summary"), 160),
                "energy": sc.get("energy") or "medium",
            })
            if len(scenes_out) >= 8:
                break
        if scenes_out:
            row["scenes"] = scenes_out

        # Transcript excerpt within the window.
        segs = ((src_an.get("transcript") or {}).get("segments") or [])
        spoken: list[str] = []
        for seg in segs:
            s, e = int(seg.get("start_f", 0)), int(seg.get("end_f", 0))
            if e <= tb or s >= te:
                continue
            spoken.append(str(seg.get("text") or "").strip())
        if spoken:
            row["transcript"] = _trunc(" ".join(spoken), 600)

        visual = src_an.get("visual") or {}
        if visual.get("mood"):
            row["mood"] = _trunc(visual.get("mood"), 80)
        if visual.get("speech_presence"):
            row["speech"] = True
        if src_an.get("media") == "audio":
            row["audio"] = {
                "mood": _trunc(src_an.get("mood"), 80),
                "tags": (src_an.get("tags") or [])[:6],
            }

        # Existing styling the planner must respect (only what's set).
        for fld in ("transition", "filter", "filters", "effects", "speed", "volume", "opacity", "fit"):
            if clip.get(fld) not in (None, "", {}, []):
                row[fld] = clip.get(fld)
        if clip.get("muted"):
            row["muted"] = True
        kfs = clip.get("keyframes") or {}
        keyed = [k for k, v in kfs.items() if v]
        if keyed:
            row["keyframed"] = keyed
        tx = clip.get("text")
        if isinstance(tx, dict):
            content = tx.get("content") or tx.get("text") or tx.get("value")
            if content:
                row["text"] = _trunc(content, 120)

        clip_rows.append(row)

    digest = {
        "project": {
            "width": proj.get("width"),
            "height": proj.get("height"),
            "fps": proj.get("fps") or FPS,
            "durationInFrames": proj.get("durationInFrames"),
            "aspect": proj.get("aspect"),
            "title": _trunc(proj.get("title"), 80),
        },
        "tracks": [
            {"id": t.get("id"), "kind": t.get("kind"), "index": t.get("index")}
            for t in (timeline.get("tracks") or [])
        ],
        "clips": clip_rows,
    }
    # Source-level moods (palette + speech) for global look decisions.
    moods = {}
    for key, src_an in sources.items():
        visual = src_an.get("visual") or {}
        if visual.get("mood") or visual.get("palette"):
            moods[key] = {
                "mood": _trunc(visual.get("mood"), 80),
                "palette": (visual.get("palette") or [])[:5],
            }
    if moods:
        digest["source_moods"] = moods

    # Fold in per-project memory: provenance ("what each clip is") onto rows +
    # a recent-edits tail ("what's been done"). Latest provenance row per clip.
    if memory:
        prov_by_clip: dict[str, dict] = {}
        for p in (memory.get("provenance") or []):
            cid = p.get("clip_id")
            if cid:
                prov_by_clip[cid] = p
        for row in clip_rows:
            p = prov_by_clip.get(row.get("id"))
            if p:
                blurb = _creative_blurb(p.get("creative") or {}, p.get("source_mode"))
                if blurb:
                    row["is"] = blurb
        recent = [
            {"rev": e.get("rev"), "by": e.get("actor"), "did": _trunc(e.get("summary"), 200)}
            for e in (memory.get("edit_log") or [])[-8:]
        ]
        if recent:
            digest["recent_edits"] = recent
    return digest


# ---------------------------------------------------------------------------
# Popup placement candidates — geometry done HERE so the LLM only picks
# ---------------------------------------------------------------------------

def _rects_overlap(a: tuple[float, float, float, float],
                   b: tuple[float, float, float, float]) -> bool:
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    return ax < bx + bw and bx < ax + aw and ay < by + bh and by < ay + ah


# Edge/corner cells make better popup homes than dead center.
_CELL_PREFERENCE = {0: 3, 2: 3, 6: 3, 8: 3, 1: 2, 3: 2, 5: 2, 7: 2, 4: 1}
_CELL_NAMES = {
    0: "top-left", 1: "top-center", 2: "top-right",
    3: "mid-left", 4: "center", 5: "mid-right",
    6: "bottom-left", 7: "bottom-center", 8: "bottom-right",
}

# Edge-first cell cycle for fallback popups (skip dead center): bottom-center,
# top-center, then corners + mid-sides — so consecutive popups don't stack.
_FALLBACK_CELL_CYCLE = [7, 1, 6, 8, 0, 2, 3, 5]


def _video_clip_count(timeline: dict) -> int:
    return sum(
        1 for c in (timeline or {}).get("clips") or []
        if (c.get("kind") or "video") == "video"
    )


def _fallback_popup_windows(timeline: dict, *, max_windows: int = 6) -> list[dict]:
    """Geometry-free popup windows derived from the timeline ALONE — placed
    REGARDLESS of the analysis 'safe' grid. Used to top up (or replace) the
    safe-grid result so popups always have somewhere to land, even when the
    analysis is missing, every cell is face-covered, or clips are short.

    Deterministic: the same timeline yields the same windows, so the framed-
    popup backfill stays consistent with plan time. Same shape as
    candidate_popup_windows: {from, durationInFrames, x, y, why} in timeline
    frames, x/y center-relative px clamped title-safe (±0.45·dim)."""
    proj = (timeline or {}).get("project") or {}
    width = int(proj.get("width") or 1080)
    height = int(proj.get("height") or 1920)
    max_x = 0.45 * width
    max_y = 0.45 * height
    vclips = sorted(
        [c for c in (timeline.get("clips") or []) if (c.get("kind") or "video") == "video"],
        key=lambda c: (int(c.get("from", 0) or 0), str(c.get("id") or "")),
    )
    out: list[dict] = []
    for i, clip in enumerate(vclips):
        if len(out) >= max_windows:
            break
        start = int(clip.get("from", 0) or 0)
        dur_clip = int(clip.get("durationInFrames", 0) or 0)
        if dur_clip <= 0:
            continue
        # Start a touch into the clip; show for ≤3s, clamped to the clip's end.
        from_f = start + min(max(6, dur_clip // 10), max(0, dur_clip - 1))
        durf = max(1, min(90, start + dur_clip - from_f))
        ci = _FALLBACK_CELL_CYCLE[i % len(_FALLBACK_CELL_CYCLE)]
        col, rowi = ci % 3, ci // 3
        px = min(max(((col + 0.5) / 3.0 - 0.5) * width, -max_x), max_x)
        py = min(max(((rowi + 0.5) / 3.0 - 0.5) * height, -max_y), max_y)
        out.append({
            "from": from_f,
            "durationInFrames": int(durf),
            "x": round(px),
            "y": round(py),
            "why": f"{_CELL_NAMES[ci]} cell (placed regardless — no safe window in analysis)",
        })
    return out


def candidate_popup_windows(
    timeline: dict, analysis: dict, *, max_windows: int = 6,
) -> list[dict]:
    """Labeled candidate popup placements derived from the safe grid +
    transcript: `{from, durationInFrames, x, y, why}` in TIMELINE frames;
    x/y are center-relative px offsets (renderer translate convention:
    0,0 = canvas center, +x right, +y down, clamped to ±0.45·dim like
    ops._title_safe_xy), each in a 3×3 grid cell that is unoccupied and
    face-free."""
    proj = (timeline or {}).get("project") or {}
    width = int(proj.get("width") or 1080)
    height = int(proj.get("height") or 1920)
    max_x = 0.45 * width  # matches _TITLE_SAFE in yas_video_edit_ai_ops
    max_y = 0.45 * height
    sources = (analysis or {}).get("sources") or {}

    candidates: list[dict] = []
    for clip in (timeline or {}).get("clips") or []:
        if (clip.get("kind") or "video") != "video":
            continue
        key = _clip_source_key(clip)
        src_an = sources.get(key) or {}
        visual = src_an.get("visual") or {}
        grid = visual.get("safe_grid") or []
        faces = visual.get("faces") or []
        segs = ((src_an.get("transcript") or {}).get("segments") or [])
        tb, te, offset = _clip_window(clip)
        clip_end_tl = te + offset

        for sample in grid:
            try:
                f = int(sample.get("frame_f", 0))
            except (TypeError, ValueError):
                continue
            if not (tb <= f < te):
                continue  # sampled frame falls outside this clip's trim window
            tl_f = f + offset
            durf = min(90, clip_end_tl - tl_f)  # default ≤3s, never past clip end
            if durf < 30:
                continue  # too short to read a popup

            near_faces: list[tuple[float, float, float, float]] = []
            for fc in faces:
                try:
                    if abs(int(fc.get("frame_f", 0)) - f) <= 15:
                        near_faces.append((
                            float(fc.get("x", 0)), float(fc.get("y", 0)),
                            float(fc.get("w", 0)), float(fc.get("h", 0)),
                        ))
                except (TypeError, ValueError):
                    continue

            line = ""
            for seg in segs:
                if int(seg.get("start_f", 0)) <= f < int(seg.get("end_f", 0)):
                    line = _trunc(seg.get("text"), 80)
                    break

            for ci, cell in enumerate((sample.get("cells") or [])[:9]):
                if cell.get("occupied"):
                    continue
                col, rowi = ci % 3, ci // 3
                cell_rect = (col / 3.0, rowi / 3.0, 1 / 3.0, 1 / 3.0)
                if any(_rects_overlap(cell_rect, fr) for fr in near_faces):
                    continue
                # Cell center as a center-relative offset, clamped title-safe.
                px = min(max(((col + 0.5) / 3.0 - 0.5) * width, -max_x), max_x)
                py = min(max(((rowi + 0.5) / 3.0 - 0.5) * height, -max_y), max_y)
                why = f"{_CELL_NAMES[ci]} cell is free ({cell.get('what') or 'background'})"
                if line:
                    why += f' — during: "{line}"'
                candidates.append({
                    "from": tl_f,
                    "durationInFrames": int(durf),
                    "x": round(px),
                    "y": round(py),
                    "why": why,
                    "_score": _CELL_PREFERENCE.get(ci, 1) + (2 if line else 0),
                })

    # Best-first, then spread out in time: at most 2 position alternatives per
    # overlapping time window (so the planner picks position, not geometry),
    # never two picks at the same spot.
    candidates.sort(key=lambda c: (-c["_score"], c["from"]))
    picked: list[dict] = []
    for cand in candidates:
        if len(picked) >= max_windows:
            break
        c0, c1 = cand["from"], cand["from"] + cand["durationInFrames"]
        overlapping = [
            p for p in picked
            if c0 < p["from"] + p["durationInFrames"] and p["from"] < c1
        ]
        if len(overlapping) >= 2:
            continue
        if any(p["x"] == cand["x"] and p["y"] == cand["y"] for p in overlapping):
            continue
        picked.append(cand)
    for p in picked:
        p.pop("_score", None)
    picked.sort(key=lambda c: c["from"])

    # Place popups REGARDLESS of "safe" windows: the safe grid can come up empty
    # (analysis missing, faces everywhere, short/occluded clips) or too sparse.
    # Top up with deterministic timeline-geometry fallbacks so popups always
    # land — skipping any fallback that would stack on an existing window's time.
    target = min(3, _video_clip_count(timeline))
    if len(picked) < target:
        spans = [(p["from"], p["from"] + p["durationInFrames"]) for p in picked]
        for fb in _fallback_popup_windows(timeline, max_windows=max_windows):
            if len(picked) >= max_windows:
                break
            f0, f1 = fb["from"], fb["from"] + fb["durationInFrames"]
            if any(f0 < e and s < f1 for (s, e) in spans):
                continue
            picked.append(fb)
            spans.append((f0, f1))
        picked.sort(key=lambda c: c["from"])
    return picked
