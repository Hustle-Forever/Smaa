"""Yas Producer — Video Editor: AI edit-engine ORCHESTRATOR.

Ties the three pre-built halves of the AI editor into one job pipeline:

  yas_video_edit_ai_analysis  — per-source media analysis (scenes/transcript/
                                safe-grid), planner digest, popup candidates
  yas_video_edit_ai_ops       — EDIT_PLAN_SCHEMA, feature gating, deterministic
                                executor (apply_plan)
  media_providers             — ElevenLabs speech-to-speech (voice), Suno /
                                Library music

One background job per project at a time. The flow per feature:

  popups / compose         → analyze → Gemini plans (EDIT_PLAN_SCHEMA) →
                             validate_and_filter_ops → apply_plan
                             (compose with generate_bookends=true then ALSO
                             frames the result intro → main → outro: ready
                             bookends are placed inline; missing ones are
                             generated via the bookend pipeline and the job
                             parks at waiting_provider like feature=bookend)
  edit                     → ONE-CLICK autonomous full edit (no instruction
                             needed) in TWO phases with separate plan ids:
                             Phase A structure (reorder/trim/transitions/
                             mute/filters) → apply → RE-GROUND on the fresh
                             timeline → Phase B overlays (framed intro title
                             card + section popups + stickers + ONE
                             library-resolved music bed) → apply. Fully
                             synchronous — music never parks on Suno here.
  voice                    → NO LLM: ffmpeg-extract the clip's trimmed audio →
                             ElevenLabs STS → import_audio → replace_voice op
                             (use_cloned=true swaps in the app-wide voice clone
                             from yas_voice_clone; use_as_is=true skips STS and
                             lays a pre-uploaded yas_uploads audio file directly)
  music                    → style brief from the analysis moods →
                             get_music_provider(mode) → add_music op
                             (Suno returns "pending" → job parks at
                             waiting_provider; workers/yas_video_edit_ai_poller
                             downloads + applies when the track is ready)
  bookend                  → texts (Gemini, analysis-grounded, degrades) →
                             TH v2 card render → Veo i2v submit
                             (yas_video_edit_ai_bookends) → job parks at
                             waiting_provider with provider="veo"; the same
                             poller places the intro/outro clips (add_clip)
                             once yas_animations_poller flips them ready

STORAGE — job state lives at metadata.video_edit_ai.job, a SIBLING of the
analysis cache (save_timeline rebuilds metadata.video_edit wholesale on every
frontend autosave, so nothing job-related may live under video_edit). All
writes go through jsonb_set with the `video_edit_ai` parent ensured — never a
top-level `||` merge (would race the autosave path).
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import secrets
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from fastapi import HTTPException
from psycopg.types.json import Jsonb

from ..config import config
from ..db import get_conn
from . import yas_edit_recipes
from . import yas_video_editor as ve
from . import yas_video_edit_ai_bookends as bookends
from . import yas_video_edit_ai_broll as broll
from . import yas_video_edit_ai_ops as ai_ops
from . import yas_sfx
from . import yas_voice_clone as voice_clone
from .media_providers import ProviderError, get_music_provider, get_voice_provider
from .vertex_client import (
    VertexBudgetExceededError,
    gemini_embed,
    gemini_multimodal,
    get_meter,
)
from .yas_video_edit_ai_analysis import (
    ANALYSIS_VERSION,
    build_project_digest,
    candidate_popup_windows,
    get_or_build_analysis,
)
from .yas_video_edit_ai_ops import (
    EDIT_PLAN_SCHEMA,
    _OP_SCHEMA,
    apply_plan,
    drop_empty_text_popups,
    drop_unapplyable_transitions,
    validate_and_filter_ops,
)

log = logging.getLogger("archivist.yas_ve_ai_orchestrator")


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

FPS = ve.FPS  # 30

FEATURES = {"popups", "compose", "edit", "voice", "music", "bookend", "sfx", "broll",
            "duck", "captions", "silence",
            # Phase 4: the Claude look->think->act agent (triage + tool-use loop).
            "agent"}

# "analyze" is accepted by start_job as a pseudo-feature: it runs only the
# analysis step (optionally forced) and never plans/applies ops.
_ANALYZE = "analyze"

ACTIVE_STATUSES = {
    "queued", "analyzing", "planning", "applying", "waiting_provider",
    "generating_card",  # bookend: card render + Veo submit in flight
}

# A non-waiting job older than this is considered orphaned (backend restarted
# mid-run kills its asyncio task) and no longer blocks new jobs.
_STALE_AFTER_SEC = 30 * 60

# A waiting_provider job older than this has timed out (the provider never
# finished) — it stops blocking new jobs and the poller fails it.
_WAITING_MAX_AGE_SEC = 30 * 60

# Planner model: cheap, schema-constrained, thinking disabled (mirrors the
# analysis module's _SCHEMA_MODEL choice).
_PLANNER_MODEL = "gemini-2.5-flash"


# ---------------------------------------------------------------------------
# System prompts (editorial taste lives HERE, not in user prompts)
# ---------------------------------------------------------------------------

_COMMON_RULES = (
    "Hard rules (violations get your op rejected):\n"
    "- All time values are INTEGER FRAMES at 30 fps. Timeline fields (`from`) "
    "are timeline frames; keyframe `f` values are CLIP-LOCAL frames.\n"
    "- Only reference clip_id / track_id / asset_id / font_asset_id values "
    "explicitly listed in the prompt. Never invent ids.\n"
    "- NEVER compute transition overlaps or move clips to create them — for a "
    "transition just emit add_transition with preset + durationInFrames on the "
    "LATER clip; the executor does the overlap math.\n"
    "- Leave source_upload_id / source_asset_id unset on add_music unless the "
    "prompt lists concrete music asset candidates to pick from.\n"
    "- Stay strictly on the user's stated intent. Fewer, better ops beat many "
    "mediocre ones. Return an empty ops array if nothing tasteful applies."
)

POPUPS_SYSTEM = (
    "You are a senior short-form video editor adding text popups (and the "
    "occasional sticker) to a vertical social video.\n\n"
    "Taste rules:\n"
    "- At most 2 popups visible at the same time; usually 1.\n"
    "- Each popup lasts 2-4 seconds (60-120 frames) and uses anim='fade', "
    "'pop' or 'slide_up' (entrance/exit is synthesized, >=8 frames each).\n"
    "- Pick placements ONLY from the provided candidate windows: copy their "
    "from/durationInFrames/x/y. You may shorten a window, never extend it.\n"
    "- Wordsmith the text: 2-6 punchy words, reinforcing what is being said "
    "or shown at that moment (the candidate's `why` quotes the transcript).\n"
    "- Big readable type (fontSize 48-90), high-contrast color against the "
    "scene. Use a provided font_asset_id when a brand font is listed.\n"
    "- When TEXTBOX FRAME candidates are listed, emit add_framed_popup (text + "
    "frame_asset_id + font_asset_id) instead of plain add_text_popup — the "
    "frame graphic renders behind the text. Use fontSize 40-72 and dark text "
    "(#1A1A1A) so it reads on the white frame body.\n\n"
    + _COMMON_RULES
)

COMPOSE_SYSTEM = (
    "You are a senior video editor sequencing and joining clips on a "
    "multi-track timeline.\n\n"
    "Taste rules:\n"
    "- Order clips for narrative flow (hook first, payoff last) using "
    "reorder_clips with a FULL permutation of the track's clip ids, or "
    "set_clip_position for single moves.\n"
    "- Transitions: DO add one between most adjacent clips (an occasional "
    "hard cut is fine for high energy, but a compose pass with zero "
    "transitions is almost always wrong). Every add_transition MUST include "
    "a `preset` ('fade' or 'slide' for most content) AND an integer "
    "`durationInFrames` between 10 and 18 (e.g. 12) — never omit either. "
    "NEVER put a transition on a clip shorter than 45 frames.\n"
    "- add_transition targets the LATER clip of the pair.\n"
    "- Structure: arrange the clips as intro → main → outro (a 3-act flow).\n"
    "- You MAY add at most one short title text popup at the very start and "
    "one CTA popup at the very end (2-3 s, ≤6 words, title-safe placement) "
    "ONLY when the video has no dedicated intro/outro clip.\n"
    "- NEVER emit add_clip — the system places intro/outro media itself.\n\n"
    + _COMMON_RULES
)

EDIT_SYSTEM = (
    "You are a confident senior short-form video editor. The user pressed ONE "
    "button — 'Edit my video' — and expects back a finished, watchable edit "
    "with no further input. You decide everything; never ask, never hedge. "
    "Work the four stages below IN ORDER — together they are the complete "
    "edit. (Each call scopes you to a subset of the stages; honor that scope "
    "but keep the whole edit in mind so the stages compose.)\n\n"
    "STAGE 1 — STRUCTURE (reorder_clips, set_clip_position, trim_clip, "
    "mute_clip, set_filters):\n"
    "- Reorder for a hook-first narrative: open on the single most "
    "attention-grabbing moment the digest shows (a bold claim, a reveal, peak "
    "energy), build the middle logically, land the payoff/CTA last. Use "
    "reorder_clips with a FULL permutation of that track's clip ids "
    "(set_clip_position only for a one-off move).\n"
    "- Trim dead air with trim_clip: silent lead-ins before the first spoken "
    "word, trailing padding after the last, stretches where the transcript "
    "and scenes show nothing happening. Cut conservatively — never clip into "
    "speech.\n"
    "- Filters only where the footage needs them, and SUBTLE: "
    "brightness/contrast/saturate within 0.9-1.15, blur 0.\n\n"
    "STAGE 2 — TRANSITIONS (add_transition):\n"
    "- Add a transition at EVERY adjacent cut — skip one only when a hard cut "
    "clearly serves a beat. A finished edit should feel deliberately cut, not "
    "a raw string of clips.\n"
    "- VARY the presets across the edit: rotate among 'fade', 'slide', 'wipe', "
    "'flip' and 'iris' so consecutive cuts feel different — never use the same "
    "preset for every cut. Favor 'fade'/'slide' for calm beats and "
    "'wipe'/'flip'/'iris' for energy or scene jumps.\n"
    "- Every add_transition MUST include a `preset` AND an integer "
    "`durationInFrames` between 12 and 20 (e.g. 14) — never omit either. "
    "Target the LATER clip of the pair; never a clip shorter than 45 frames.\n\n"
    "STAGE 3 — TEXT (add_framed_popup, add_text_popup, add_sticker):\n"
    "- Open the video with ONE add_framed_popup intro title card: a 3-5 word "
    "hook distilled from the OPENING transcript line (what the video is "
    "actually about — never a generic 'Welcome'), `frame_asset_id` from the "
    "TEXTBOX FRAME candidates, `font_asset_id` from the brand fonts, "
    "anim='title'. Take its timing/position from the intro-card candidate "
    "window (the FIRST candidate window listed).\n"
    "- Then 1-3 section popups at scene shifts / key spoken beats, picked "
    "ONLY from the CANDIDATE WINDOWS (copy from/durationInFrames/x/y "
    "verbatim; you may shorten a duration, never extend it). Max 2 popups "
    "concurrent; each 2-4 s (60-120 frames); text 2-6 punchy words echoing "
    "what is said or shown right then; fontSize 40-72 with dark text "
    "(#1A1A1A) on framed popups so it reads on the white frame body.\n\n"
    "STAGE 4 — MUSIC (add_music):\n"
    "- Exactly ONE add_music op for the whole video: from=0, duck=true, and a "
    "`style_or_query` matching the video's mood and energy. Pick a "
    "source_asset_id from the MUSIC candidates when one fits; otherwise "
    "leave the source unset and the system resolves it from the library.\n\n"
    + _COMMON_RULES
    + "\n\nOVERRIDE for this one-click full edit: the final common rule above "
    "does NOT apply here. There is no narrow 'stated intent' to stay inside — "
    "the intent IS a complete edit. Deliver a COMPLETE, BOLD edit the user can "
    "clearly see was edited; a good pass is typically 8-16 ops across the "
    "stages; never return an empty ops array."
)

# The two-phase edit: planner ops allowed per phase. Phase restriction happens
# in the ORCHESTRATOR (_restrict_ops_to_phase filters by op name BEFORE
# validate_and_filter_ops) because the ops module's per-feature whitelist is
# owned elsewhere and validate_and_filter_ops has no allowed-subset kwarg.
_EDIT_PHASE_A_OPS = frozenset({
    "reorder_clips", "set_clip_position", "trim_clip",
    "add_transition", "mute_clip", "set_filters",
})
_EDIT_PHASE_B_OPS = frozenset({
    "add_framed_popup", "add_text_popup", "add_sticker", "add_music",
})

_EDIT_PHASE_A_SYSTEM = EDIT_SYSTEM + (
    "\n\nTHIS CALL covers STAGE 1 + STAGE 2 only (structure + transitions). "
    "Emit ONLY these ops: reorder_clips, set_clip_position, trim_clip, "
    "add_transition, mute_clip, set_filters. Text, stickers and music are "
    "handled by a second pass — do NOT emit them here. Put a transition on "
    "MOST cuts (varied presets). A good structure pass is typically 5-10 ops; "
    "never return an empty ops array."
)

_EDIT_PHASE_B_SYSTEM = EDIT_SYSTEM + (
    "\n\nTHIS CALL covers STAGE 3 + STAGE 4 only (text + music). The timeline "
    "you see is ALREADY restructured with transitions in place — do not "
    "second-guess it. Emit ONLY these ops: add_framed_popup, add_text_popup, "
    "add_sticker, add_music. Do NOT reorder, trim or add transitions here. "
    "A good overlay pass is typically 4-7 ops (intro title card + 2-4 section "
    "popups + one add_music); never return an empty ops array."
)

_SYSTEMS = {"popups": POPUPS_SYSTEM, "compose": COMPOSE_SYSTEM, "edit": EDIT_SYSTEM}


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _ai_blob(md: dict) -> dict:
    ai = (md or {}).get("video_edit_ai")
    if isinstance(ai, str):
        try:
            ai = json.loads(ai)
        except Exception:  # noqa: BLE001
            ai = None
    return ai if isinstance(ai, dict) else {}


def _veo_preflight_422() -> None:
    """422 fast-fail shared by feature='bookend' and compose-with-bookends:
    Veo must be configured and the Vertex budget not exhausted."""
    if not config.veo_output_bucket:
        raise HTTPException(
            422,
            "Intro/outro generation needs VEO_OUTPUT_BUCKET — set it in backend/.env and retry.",
        )
    meter = get_meter()
    if meter.over_budget():
        raise HTTPException(
            422,
            f"Vertex budget exhausted (${meter.spent_usd:.2f} of "
            f"${meter.budget_usd:.2f}). Raise VEO_BUDGET_USD or wait for reset.",
        )


def _store_edit_recipe_bg(project_id: int, analysis: dict) -> None:
    """Fire-and-forget: re-fetch the FINAL saved timeline and store it as an
    edit recipe (yas_edit_recipes) so future edit/compose passes can learn
    this edit's STYLE via RAG. Best-effort by construction — the task is
    detached from the job and store_recipe itself never raises, so a failure
    can never affect the job result."""
    async def _store() -> None:
        try:
            row = await ve._fetch_row(project_id)
            timeline = ve._video_edit(row["metadata"])
            if not (timeline.get("clips") or []):
                return
            recipe = yas_edit_recipes.serialize_editor_recipe(timeline, analysis)
            await yas_edit_recipes.store_recipe(
                source="editor",
                source_generation_id=project_id,
                recipe=recipe,
            )
        except Exception:  # noqa: BLE001 — recipe capture is best-effort
            log.exception("[yas-ve-ai] project #%d: edit-recipe store failed (non-fatal)",
                          project_id)

    try:
        asyncio.create_task(_store(), name=f"yas_ve_ai_recipe_{project_id}")
    except RuntimeError:
        pass  # no running loop (sync test context) — skip silently


# ---------------------------------------------------------------------------
# DB — job state at metadata.video_edit_ai.job (jsonb_set, parent ensured;
# mirrors yas_video_edit_ai_analysis._store_analysis)
# ---------------------------------------------------------------------------

async def _patch_ai(project_id: int, path: list[str], value: Any) -> None:
    """jsonb_set metadata at {video_edit_ai,*path} (parent object ensured).
    REPLACES the value at that path."""
    full_path = ["video_edit_ai", *[str(p) for p in path]]
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                UPDATE yas_generations
                   SET metadata = jsonb_set(
                           jsonb_set(metadata, '{video_edit_ai}',
                                     coalesce(metadata->'video_edit_ai', '{}'::jsonb), true),
                           %s::text[], %s::jsonb, true),
                       updated_at = NOW()
                 WHERE id = %s AND mode = %s
                """,
                (full_path, Jsonb(value), project_id, ve.MODE_PREVIEW),
            )


async def _merge_job(project_id: int, fields: dict) -> None:
    """Shallow-merge fields into the job blob in ONE statement (no read-modify-
    write race with the progress callback)."""
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                UPDATE yas_generations
                   SET metadata = jsonb_set(
                           jsonb_set(metadata, '{video_edit_ai}',
                                     coalesce(metadata->'video_edit_ai', '{}'::jsonb), true),
                           '{video_edit_ai,job}',
                           coalesce(metadata#>'{video_edit_ai,job}', '{}'::jsonb) || %s::jsonb,
                           true),
                       updated_at = NOW()
                 WHERE id = %s AND mode = %s
                """,
                (Jsonb(fields), project_id, ve.MODE_PREVIEW),
            )


async def _read_job(project_id: int) -> dict:
    row = await ve._fetch_row(project_id)
    job = _ai_blob(row["metadata"]).get("job")
    return job if isinstance(job, dict) else {}


def _job_is_active(job: dict) -> bool:
    """Active = blocks new jobs. Orphaned non-waiting jobs (backend restart
    killed the task) expire after _STALE_AFTER_SEC; waiting_provider jobs
    expire after _WAITING_MAX_AGE_SEC (the poller fails them on the same
    clock) so a stuck provider task never bricks the panel."""
    status = (job or {}).get("status")
    if status not in ACTIVE_STATUSES:
        return False
    limit = _WAITING_MAX_AGE_SEC if status == "waiting_provider" else _STALE_AFTER_SEC
    try:
        started = datetime.fromisoformat(str(job.get("started_at")))
        if started.tzinfo is None:
            started = started.replace(tzinfo=timezone.utc)
        age = (datetime.now(timezone.utc) - started).total_seconds()
        if age > limit:
            log.warning("[yas-ve-ai] stale %s job %s (%.0fs old) — not blocking",
                        status, job.get("id"), age)
            return False
    except (TypeError, ValueError):
        if status == "waiting_provider":
            return False  # no usable started_at — treat as expired
    return True


# ---------------------------------------------------------------------------
# Timeline / analysis utilities
# ---------------------------------------------------------------------------

def _clip_window(clip: dict) -> tuple[int, int, int]:
    """(trim_before, trim_end_exclusive, source→timeline offset) — same math
    as the analysis module's digest mapping."""
    def _i(v: Any) -> int:
        try:
            return int(v or 0)
        except (TypeError, ValueError):
            return 0
    tb = _i(clip.get("trimBefore"))
    dur = _i(clip.get("durationInFrames"))
    frm = _i(clip.get("from"))
    return tb, tb + dur, frm - tb


def _speech_segments_timeline(timeline: dict, analysis: dict) -> list[dict]:
    """All transcript speech spans across the project's video clips, re-mapped
    from source-local to TIMELINE frames (clipped to each clip's trim window).
    Feeds add_music ducking."""
    sources = (analysis or {}).get("sources") or {}
    out: list[dict] = []
    for clip in (timeline or {}).get("clips") or []:
        if (clip.get("kind") or "video") != "video":
            continue
        kind, sid = ve._clip_source_kind_id(clip)
        if not kind or sid is None:
            continue
        src_an = sources.get(f"{kind}:{int(sid)}") or {}
        segs = ((src_an.get("transcript") or {}).get("segments") or [])
        tb, te, offset = _clip_window(clip)
        for seg in segs:
            try:
                s = max(int(seg.get("start_f", 0)), tb)
                e = min(int(seg.get("end_f", 0)), te)
            except (TypeError, ValueError):
                continue
            if e > s:
                out.append({"start_f": s + offset, "end_f": e + offset})
    out.sort(key=lambda x: x["start_f"])
    return out[:200]


def _project_duration_f(timeline: dict) -> int:
    proj = (timeline or {}).get("project") or {}
    try:
        dur = int(proj.get("durationInFrames") or 0)
    except (TypeError, ValueError):
        dur = 0
    if dur > 0:
        return dur
    end = 0
    for c in (timeline or {}).get("clips") or []:
        try:
            end = max(end, int(c.get("from", 0) or 0) + int(c.get("durationInFrames", 0) or 0))
        except (TypeError, ValueError):
            continue
    return max(end, FPS)  # at least 1s so duration_sec is never 0


def _style_brief(analysis: dict, query: Optional[str]) -> str:
    """Music style brief from the analysis moods/energy (+ the user's query)."""
    moods: list[str] = []
    energy_votes: dict[str, int] = {}
    for src in ((analysis or {}).get("sources") or {}).values():
        visual = src.get("visual") or {}
        m = (visual.get("mood") or "").strip()
        if m and m.lower() not in (x.lower() for x in moods):
            moods.append(m)
        for sc in src.get("scenes") or []:
            en = sc.get("energy")
            if en in ("low", "medium", "high"):
                energy_votes[en] = energy_votes.get(en, 0) + 1
        am = (src.get("mood") or "").strip()  # audio sources
        if am and am.lower() not in (x.lower() for x in moods):
            moods.append(am)
    parts: list[str] = []
    q = (query or "").strip()
    if q:
        parts.append(q)
    if moods:
        parts.append("mood: " + ", ".join(moods[:4]))
    if energy_votes:
        dominant = max(energy_votes, key=lambda k: energy_votes[k])
        parts.append(f"{dominant} energy")
    return "; ".join(parts) or "upbeat background music"


def _opening_line(digest: dict) -> str:
    """The FIRST spoken line on the timeline (digest clip rows are transcript
    excerpts already re-mapped to the trim window) — the edit pass derives the
    intro title-card hook from it. First sentence, capped ~120 chars."""
    rows = sorted(
        (r for r in (digest or {}).get("clips") or [] if r.get("transcript")),
        key=lambda r: int(r.get("from") or 0),
    )
    if not rows:
        return ""
    text = str(rows[0]["transcript"]).strip()
    for sep in (". ", "! ", "? "):
        i = text.find(sep)
        if 0 < i < 120:
            return text[: i + 1]
    return text[:120]


def _digest_transcript_excerpt(digest: dict, limit: int = 200) -> str:
    """Concatenated transcript text from the digest's clip rows, in timeline
    order — grounds the sticker/music searches in what the video is ABOUT
    (not its project title)."""
    rows = sorted(
        (r for r in (digest or {}).get("clips") or [] if r.get("transcript")),
        key=lambda r: int(r.get("from") or 0),
    )
    return " ".join(str(r["transcript"]).strip() for r in rows)[:limit]


# ---------------------------------------------------------------------------
# Assets Library lookups (legal ids + planner grounding)
# ---------------------------------------------------------------------------

async def _valid_asset_ids() -> set[int]:
    """Every enabled producer_assets id — the universe add_sticker.asset_id /
    font_asset_id / add_music.source_asset_id may reference."""
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute("SELECT id FROM producer_assets WHERE enabled")
            return {int(r["id"]) for r in await cur.fetchall()}


async def _font_assets(limit: int = 10) -> list[dict]:
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT id, display_name
                  FROM producer_assets
                 WHERE enabled AND asset_kind = 'font'
                 ORDER BY created_at DESC
                 LIMIT %s
                """,
                (limit,),
            )
            return [
                {"font_asset_id": int(r["id"]), "name": r.get("display_name") or f"font #{r['id']}"}
                for r in await cur.fetchall()
            ]


async def _frame_assets(limit: int = 6) -> list[dict]:
    """Textbox-frame images for add_framed_popup: the Library's 'popup_frame'
    image category first, semantic search as a fallback when it's empty."""
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT id, display_name, LEFT(ai_description, 160) AS preview
                  FROM producer_assets
                 WHERE enabled AND asset_kind = 'image' AND category = 'popup_frame'
                 ORDER BY created_at DESC
                 LIMIT %s
                """,
                (limit,),
            )
            frames = [
                {
                    "frame_asset_id": int(r["id"]),
                    "name": r.get("display_name") or f"frame #{r['id']}",
                    "description": r.get("preview") or "",
                }
                for r in await cur.fetchall()
            ]
    if frames:
        return frames
    found = await _search_assets(
        "retro browser-window caption textbox frame", kind="image", limit=4,
    )
    return [
        {
            "frame_asset_id": int(a["asset_id"]),
            "name": a["name"],
            "description": a.get("description") or "",
        }
        for a in found
    ]


async def _search_assets(query: str, *, kind: str, limit: int) -> list[dict]:
    """Semantic Library search (same ranking as POST /api/producer-assets/search:
    gemini-embedding-001 → pgvector cosine). Best-effort: [] on any failure."""
    q = (query or "").strip()
    if not q:
        return []
    try:
        vecs = await gemini_embed([q])
        qvec = vecs[0] if vecs else None
        if not qvec:
            return []
        async with get_conn() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    SELECT id, display_name, category,
                           LEFT(ai_description, 160) AS preview
                      FROM producer_assets
                     WHERE embedding IS NOT NULL AND enabled AND asset_kind = %s
                     ORDER BY embedding <=> %s::vector
                     LIMIT %s
                    """,
                    (kind, qvec, limit),
                )
                return [
                    {
                        "asset_id": int(r["id"]),
                        "name": r.get("display_name") or f"asset #{r['id']}",
                        "category": r.get("category"),
                        "description": r.get("preview") or "",
                    }
                    for r in await cur.fetchall()
                ]
    except Exception:  # noqa: BLE001 — grounding is optional, the plan degrades
        log.exception("[yas-ve-ai] asset search failed (kind=%s)", kind)
        return []


# ---------------------------------------------------------------------------
# ffmpeg — extract a clip's trimmed audio window (voice feature)
# ---------------------------------------------------------------------------

def _extract_clip_audio_sync(src: Path, dest: Path, start_sec: float, dur_sec: float) -> Path:
    import imageio_ffmpeg

    cmd = [
        imageio_ffmpeg.get_ffmpeg_exe(), "-y",
        "-ss", f"{max(0.0, start_sec):.3f}",
        "-t", f"{max(0.1, dur_sec):.3f}",
        "-i", str(src),
        "-vn", "-ac", "1", "-ar", "44100", "-b:a", "128k", "-f", "mp3",
        str(dest),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    if proc.returncode != 0 or not dest.exists() or dest.stat().st_size == 0:
        raise RuntimeError(f"ffmpeg clip-audio extract failed: {(proc.stderr or '')[:400]}")
    return dest


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

async def start_job(project_id: int, feature: str, params: dict) -> dict:
    """Validate + enqueue one AI edit job for the project and launch its
    background task. Raises HTTPException(400/404/409/422)."""
    feature = (feature or "").strip().lower()
    if feature not in FEATURES and feature != _ANALYZE:
        raise HTTPException(400, f"unknown feature {feature!r} (one of {sorted(FEATURES | {_ANALYZE})})")
    params = dict(params or {})

    # Project must exist (404 from _fetch_row) + no active job (409).
    row = await ve._fetch_row(project_id)
    existing = _ai_blob(row["metadata"]).get("job") or {}
    if _job_is_active(existing):
        raise HTTPException(
            409,
            f"an AI job is already {existing.get('status')} on this project "
            f"(job {existing.get('id')}) — wait for it to finish",
        )

    # Provider pre-flight: fail BEFORE creating a job so the UI gets a clean 422.
    if feature == "agent":
        if not str(params.get("instruction") or params.get("message") or "").strip():
            raise HTTPException(422, "tell the agent what you'd like changed")
        # Same courtesy every other feature gets: fail with a clear 422 BEFORE
        # creating a job, rather than starting one that dies on its first call.
        from . import yas_video_edit_agent as _agent_pref

        if not await _agent_pref.agent_available():
            raise HTTPException(
                422,
                "No reasoning model is reachable, so the agent can't run. Check "
                "GET /video-editor/ai/providers — either enable Claude in Vertex "
                "Model Garden (with billing on), set ANTHROPIC_API_KEY in "
                "backend/.env, or fix Gemini access.",
            )
    elif feature == "voice":
        if not str(params.get("clip_id") or "").strip():
            raise HTTPException(422, "voice change needs clip_id")
        if bool(params.get("use_as_is")):
            # Pre-uploaded replacement audio — no ElevenLabs involved at all.
            if not params.get("source_upload_id"):
                raise HTTPException(
                    422,
                    "use-as-is voice replacement needs source_upload_id — upload "
                    "the audio via POST /video-editor/assets/audio/upload first.",
                )
        else:
            if not get_voice_provider().available():
                raise HTTPException(
                    422,
                    "Voice changer needs ELEVENLABS_API_KEY — add it to backend/.env and retry.",
                )
            if bool(params.get("use_cloned")):
                clone = await voice_clone.get_cloned_voice()
                if not clone:
                    raise HTTPException(
                        422,
                        "No cloned voice yet — upload/record a voice sample first "
                        "(POST /video-editor/ai/voice-clone).",
                    )
                params["voice_id"] = clone["voice_id"]
    elif feature == "music":
        try:
            provider = get_music_provider(str(params.get("mode") or "auto"))
        except ProviderError as e:
            raise HTTPException(422, str(e)) from e
        if not provider.available():
            raise HTTPException(422, f"Music provider {provider.name!r} is not configured.")
    elif feature == "bookend":
        role = str(params.get("role") or "both").strip().lower()
        if role not in ("intro", "outro", "both"):
            raise HTTPException(422, f"bookend role must be intro, outro or both (got {role!r})")
        _veo_preflight_422()
    elif feature == "broll":
        bookend_style = str(params.get("bookend_style") or "light").strip().lower()
        if bookend_style not in ("light", "veo"):
            raise HTTPException(
                422, f"broll bookend_style must be light or veo (got {bookend_style!r})",
            )
        params["bookend_style"] = bookend_style
        if bool(params.get("music", True)):
            try:
                provider = get_music_provider(str(params.get("music_mode") or "auto"))
            except ProviderError as e:
                raise HTTPException(422, str(e)) from e
            if not provider.available():
                raise HTTPException(422, f"Music provider {provider.name!r} is not configured.")
        bookend_mode = str(params.get("bookend_mode") or "generate").strip().lower()
        # 'upload' mode uses the user's own intro/outro clips — no Veo spend, so
        # skip the Veo pre-flight entirely. Only GENERATING Veo bookends needs it.
        if (
            bool(params.get("intro_outro", True))
            and bookend_mode != "upload"
            and bookend_style == "veo"
        ):
            # Same Veo pre-flight as compose-with-bookends: only blocks when a
            # bookend would actually need GENERATING (both ready → no Veo spend).
            needs_gen = False
            for r in bookends.ROLES:
                if not await bookends.find_ready_bookend(project_id, r):
                    needs_gen = True
                    break
            if needs_gen:
                _veo_preflight_422()
    elif feature == "compose" and bool(params.get("generate_bookends")):
        # Same Veo pre-flight as feature="bookend" — but ONLY when a bookend
        # would actually need generating (both already rendered → no Veo
        # spend, so an unconfigured/exhausted Veo must not block compose).
        needs_gen = False
        for r in bookends.ROLES:
            if not await bookends.find_ready_bookend(project_id, r):
                needs_gen = True
                break
        if needs_gen:
            _veo_preflight_422()

    job_id = secrets.token_hex(6)
    job = {
        "id": job_id,
        "feature": feature,
        "status": "queued",
        "progress": "queued",
        "error": None,
        "plan_id": None,
        "ops_applied": 0,
        "ops_failed": [],
        "rev_after": None,
        "started_at": _now_iso(),
        "finished_at": None,
    }
    await _patch_ai(project_id, ["job"], job)  # full replace — new job, clean slate
    asyncio.create_task(
        _run(project_id, feature, params, job_id),
        name=f"yas_ve_ai_{feature}_{project_id}_{job_id}",
    )
    log.info("[yas-ve-ai] project #%d: started %s job %s", project_id, feature, job_id)
    return job


# ---------------------------------------------------------------------------
# Chat — a conversational, selection-aware op-planner WITH memory. One Gemini
# call sees the conversation + every clip + the user's selection, then either
# emits precise ops (adjust / move / delete / restyle / compound → apply_plan,
# ONE undo step) or routes to a generation feature (music/sfx/intro/voice →
# start_job).
# ---------------------------------------------------------------------------

_CHAT_FEATURES = ["music", "sfx", "bookend", "voice", "popups", "compose", "edit",
                  "duck", "captions", "silence",
                  # The look->think->act agent. Anything needing judgement or a
                  # look at the actual footage goes here rather than to the
                  # one-shot planner, which cannot see the video.
                  "agent"]

_CHAT_SYSTEM = (
    "You are the assistant inside a short-form video editor. The user types what "
    "they want in plain language. Decide ONE action and write a short, warm, "
    "jargon-free reply (<=20 words, present tense) describing what you're doing.\n\n"
    "PLAN FIRST, THEN ACT: before anything else, fill `plan` with EVERY distinct "
    "instruction in the message — one short line each. A message often asks for "
    "MORE THAN ONE thing ('add the mouse icon at 4s AND change the selected text "
    "to this font' = TWO items). Then emit ops so that EVERY plan item is carried "
    "out — if there are two items, emit BOTH ops; NEVER stop after the first. "
    "(Limitation: if one item needs an op and another needs a 'feature', do the "
    "op item now and tell the user in the reply to ask for the feature part next.)\n\n"
    "You are given: the conversation so far (MEMORY — 'it / this / that / again' "
    "refer to earlier turns), a DIGEST listing every clip with its id, kind "
    "(video/audio/text/image), timing and current settings (volume, opacity, "
    "muted, text…), and the ids of the clips the user has SELECTED.\n\n"
    "You are also given POPUP GROUPS (the on-screen text/sticker popups, numbered "
    "and timestamped) and the BRAND palette + fonts when set.\n\n"
    "ACTION='ops' → directly edit EXISTING clips OR add overlays. Use this to "
    "adjust, move, restyle, trim, mute, DELETE, recolor, change a caption's words, "
    "ADD A POPUP/CAPTION, add a transition, reorder, change SPEED, place a saved "
    "sticker, or do several small edits at once. Fill `ops`, targeting clips by "
    "their digest `id`. When "
    "the user says 'this/these/it/them/the selected', target the SELECTED clip "
    "ids; if nothing is selected, infer the clip from the digest (e.g. the music "
    "= the audio clip on the music track; 'the captions' = the text clips).\n"
    "  TIMING IN SECONDS: when the user gives a time ('at 4 seconds', 'after 6.5s', "
    "'for 3 seconds'), set `from_seconds` (=4, =6.5) and `duration_seconds` (=3) — "
    "the editor converts to frames for you. NEVER compute frames yourself and NEVER "
    "put seconds into `from`. Only omit the time entirely when the user gave none "
    "(it then lands at the current playhead).\n"
    "  Key ops: set_keyframes(clip_id, property in [volume,scale,x,y,rotation,"
    "opacity], points=[{f,v}]) — louder/quieter=volume, bigger/smaller=scale, "
    "move=x/y, fade=opacity; set_filters(clip_id, brightness/contrast/saturate/"
    "blur); set_speed(clip_id, speed) — speed up / slow-mo, 2=2x faster, 0.5=half; "
    "trim_to_video_end(clip_id) — make a clip (usually the MUSIC) END EXACTLY with "
    "the video; ALWAYS use this for 'trim/fit the music to the video', 'make the "
    "music finish/end with the video', 'cut the music to the video length' (NEVER "
    "compute the frames yourself — this op finds the video end for you); "
    "set_text(clip_id, text) — REWRITE an existing caption/popup's words in place "
    "(use this for 'change the caption to …', NOT a new popup); "
    "add_framed_popup(text) — a NEW popup/caption inside the branded WINDOW BOX "
    "(USE THIS whenever the user says 'add a popup' or 'add a caption' — it draws "
    "the box behind the words; you don't need a clip or a `from`, it auto-places); "
    "add_text_popup(text) — plain on-video text with NO box (only when they ask "
    "for plain/no-box text); "
    "trim_clip; mute_clip; delete_clip(clip_id); set_clip_position; "
    "add_transition(clip_id, preset in [fade,zoom,blur,slide,slideLeft,slideRight,"
    "slideUp,slideDown,wipe,flip,clockWipe,iris]); add_sticker(from, asset_query:<describe a "
    "SAVED sticker e.g. 'mouse cursor icon'>, anim) — the Library is searched for "
    "the best match, so you CAN place 'the mouse icon from our assets'. Add EXACTLY "
    "ONE sticker for a single 'add the X sticker' request — NEVER emit two. If a "
    "sticker is ATTACHED, add it ONCE with its asset_id and do NOT also add it by "
    "asset_query (that would create a duplicate). For a "
    "CONSTANT value emit two points at the clip's start (f=0) and end with the "
    "SAME v — volume 1.0=full, 0.4=quiet, 2.0=boosted; scale 1.0=normal, 1.4=bigger.\n"
    "  ON-BRAND: when asked to make text/captions on-brand or 'use our colors', "
    "set the popup `color`/`fontFamily` from the BRAND palette + fonts above.\n"
    "  ATTACHED ITEMS: when the context lists ATTACHED ITEMS, the user DRAGGED "
    "those exact items in — use their concrete ids DIRECTLY and never search by "
    "description. An attached FONT → apply it to the SELECTED text clips via "
    "set_text(clip_id, fontFamily[, font_asset_id]) WITH NO `text` field (keep the "
    "words); if no text is selected, apply to ALL text clips (ask which only if "
    "there are several and it's unclear). An attached sticker → add_sticker with "
    "its asset_id; attached audio → add_music with its source_asset_id; attached "
    "text style → add_text_popup in that style.\n"
    "  ADDING MEDIA (from the bin or dragged in): the MEDIA BIN block lists every "
    "clip the user can add — you may add ANY of them yourself with add_media_clip "
    "using its exact source_kind + source_id (you do NOT need the user to drag it "
    "in). Placement:\n"
    "    • 'add / put / bring in … on the timeline', 'continue', 'add all my "
    "media', 'add the rest' → add_media_clip with overlay=false, placement='append' "
    "(full-frame, appended one after another on the MAIN track). For 'add all/the "
    "rest', emit ONE append op per bin item, in the bin's listed order.\n"
    "    • 'overlay / PiP / in the corner / small / on top' → overlay=true (small "
    "picture-in-picture inset).\n"
    "    • 'as B-roll at N seconds / here' → overlay=false with from_seconds (or the "
    "playhead) — drops it on the B-roll lane at that spot.\n"
    "  Only reference source ids that appear in the MEDIA BIN / ATTACHED ITEMS — "
    "never invent a source. If the bin is empty and they name media that isn't "
    "listed, reply asking them to add it to the bin (the + Add button) first. "
    "For attached SHAPE items, reply that placing them from chat is coming soon "
    "and point to the Shapes tab.\n\n"
    "ACTION='feature' → GENERATE new media OR run a transcript-powered pass (these "
    "run in the background). Set `feature` + `feature_params`:\n"
    "  music    → {query:<vibe>, mode:'auto'}  add a background track\n"
    "  sfx      → {query:<effect>}             sound effect on the selected clip\n"
    "  bookend  → {role: intro|outro|both}     animated intro/outro card (the user "
    "can ask for just an intro, just an outro, or both)\n"
    "  voice    → {}                           change a selected clip's voice\n"
    "  duck     → {}                           lower the MUSIC under the speaking "
    "so the voice is heard (uses the speech transcript)\n"
    "  captions → {}                           auto-generate synced captions for "
    "the whole video from the transcript\n"
    "  silence  → {}                           cut / tighten the silent gaps\n"
    "  edit     → {instruction:<their words or ''>}  the ONE-TAP AUTO-EDIT — "
    "reorder, trim, captions, transitions + music. Use this for VAGUE whole-video "
    "asks: 'edit the video', 'auto-edit', 'just edit it', 'make it good/better', "
    "'do your thing' (instruction may be empty).\n"
    "  agent    → {instruction:<their words, verbatim>}  THE EDITOR THAT WATCHES "
    "THE VIDEO. It can look at any frame, read the transcript, render a preview "
    "of its own work and fix what it sees. Use it whenever the ask needs "
    "JUDGEMENT or depends on WHAT IS ACTUALLY ON SCREEN — 'the ending isn't "
    "nice', 'make the intro punchier', 'put a popup where it doesn't cover his "
    "face', 'this bit drags', 'tidy up the middle', 'why does this feel off', "
    "anything vague about a REGION rather than the whole video, and anything "
    "multi-step you can't express as a couple of exact ops. When you're torn "
    "between 'ops' and 'agent', pick agent — it checks its own work.\n\n"
    "ACTION='reply' → greeting, or you must ASK A CLARIFYING QUESTION, or it's "
    "genuinely unclear: reply only, change nothing.\n\n"
    "RULES: PREFER 'ops' for anything about EXISTING clips + adding popups/"
    "captions/stickers — it's the powerful path. Use 'feature' to CREATE media "
    "(music/intro/outro/voice) or for duck/captions/silence/auto-edit. 'edit the "
    "video' is ALWAYS feature='edit' — NEVER reply asking what they mean by it. "
    "voice/sfx need a selected clip — if none, action='reply' and ask them to "
    "click the clip. If the timeline has no clips but the MEDIA BIN has items and "
    "they ask to add/start, DO it (add_media_clip append) — don't ask them to add "
    "clips first. Only when BOTH the timeline and the bin are empty, action='reply' "
    "and ask them to add media to the bin. ALWAYS try to DO something — only fall "
    "back to 'reply' for a real "
    "greeting or a genuine which-one ambiguity; never dead-end a clear request.\n"
    "BE PRECISE & MINIMAL: do ONLY what the user asked — emit the FEWEST ops that "
    "satisfy it (usually one). Do NOT add extra clips, do NOT duplicate an item, "
    "and do NOT move, restyle, retime or touch any clip the user didn't mention "
    "(the editor auto-positions a NEW overlay around the existing ones — you must "
    "not reposition existing clips yourself).\n"
    "CLEAN UP POPUPS: if the user says 'clean up / fix the popups', 'I don't like "
    "the popups', or 'only one popup', KEEP ONE (the one they name, else the most "
    "recent in POPUP GROUPS), delete_clip ALL the other overlay clips, and "
    "set_text the survivor if they gave new words — the editor auto-repositions it. "
    "Do this in one ops plan; don't ask which to keep when they said 'only one'.\n"
    "CLARIFY (only when truly ambiguous — otherwise make a high-effort guess and "
    "DO it): if the user references 'the popup' and POPUP GROUPS has MORE THAN "
    "ONE, action='reply' and ask which, listing them (e.g. 'Which popup — 1) "
    "text \"Sale\" @0:02, or 2) sticker @0:05?'). If they say 'this/it' with "
    "nothing selected and several clips could match, ask which. Never answer with "
    "a generic 'I didn't catch that' when the digest has an obvious target."
)

_CHAT_SCHEMA = {
    "type": "object",
    "properties": {
        "action": {"type": "string", "enum": ["ops", "feature", "reply"]},
        # `plan` is FIRST so the model enumerates every distinct sub-task before
        # it writes ops — an explicit planning surface that stops it dropping the
        # second half of a compound request ('add a sticker AND change the font').
        "plan": {
            "type": "array",
            "items": {"type": "string"},
            "description": "EVERY distinct instruction in the user's message, one short line each "
                           "(e.g. ['add the mouse sticker at 4s','set the selected text font to "
                           "Montserrat']). Fill this FIRST, then emit ops/feature covering ALL of "
                           "them. Empty [] for a pure greeting/question.",
        },
        "reply": {"type": "string"},
        "ops": {"type": "array", "items": _OP_SCHEMA},
        "feature": {"type": "string", "enum": _CHAT_FEATURES},
        "feature_params": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "mode": {"type": "string", "enum": ["auto", "suno", "library"]},
                "role": {"type": "string", "enum": ["intro", "outro", "both"]},
                "text": {"type": "string"},
                "instruction": {"type": "string"},
            },
        },
    },
    "required": ["action", "plan", "reply"],
}


def _popup_groups_block(timeline: dict) -> str:
    """Compact, numbered list of the on-screen popups (text/sticker overlays),
    grouped by time, so the planner can refer to 'popup 1/2' and ask which one
    the user means."""
    try:
        groups = ai_ops.group_popups_by_time(timeline.get("clips") or [])
    except Exception:  # noqa: BLE001 — grounding is optional
        return ""
    if not groups:
        return "POPUP GROUPS: (none on screen)"
    lines = ["POPUP GROUPS (on-screen text/sticker overlays):"]
    for g in groups[:12]:
        lines.append(f"  {g['index']}) {g['label']}")
    return "\n".join(lines)


def _brand_block(brand: Optional[dict]) -> str:
    """One-line brand palette + fonts for on-brand styling, or '' when unset."""
    if not brand:
        return ""
    colors = list(brand.get("primary_colors") or [])[:5]
    fonts = list(brand.get("fonts") or [])[:4]
    if not colors and not fonts:
        return ""
    bits = []
    if colors:
        bits.append("colors: " + ", ".join(str(c) for c in colors))
    if fonts:
        bits.append("fonts: " + ", ".join(str(f) for f in fonts))
    return "BRAND (use for on-brand text/captions) — " + "; ".join(bits)


def _attachments_block(attachments: Optional[list]) -> str:
    """Items the user DRAGGED from the left sidebar onto the chat. Each line gives
    the concrete id + how to use it, so the planner applies the EXACT item the
    user picked (no description search). Shapes aren't placeable yet."""
    if not attachments:
        return ""
    lines = ["ATTACHED ITEMS (the user dragged these in — use these EXACT ids; do NOT search by description):"]
    for att in attachments[:12]:
        if not isinstance(att, dict):
            continue
        kind = str(att.get("kind") or "")
        name = str(att.get("name") or "").strip() or kind
        if kind == "sticker" and att.get("asset_id") is not None:
            lines.append(f"  • sticker '{name}' → add_sticker with asset_id={int(att['asset_id'])}")
        elif kind == "audio" and att.get("asset_id") is not None:
            lines.append(f"  • audio '{name}' → add_music with source_asset_id={int(att['asset_id'])}")
        elif kind == "font":
            fam = str(att.get("font_family") or name)
            if att.get("brand_font") and att.get("asset_id") is not None:
                lines.append(f"  • font '{name}' → set the text font: fontFamily='{fam}', font_asset_id={int(att['asset_id'])}")
            else:
                lines.append(f"  • font '{name}' → set the text font: fontFamily='{fam}'")
        elif kind == "text":
            lines.append(f"  • text style '{name}' → add_text_popup styled like a {name}")
        elif kind == "shape":
            lines.append(f"  • shape '{name}' — placing shapes from chat isn't supported yet; tell the user to use the Shapes tab")
        elif kind == "media" and att.get("source_kind") and att.get("source_id") is not None:
            lines.append(
                f"  • media clip '{name}' → add_media_clip with source_kind='{att['source_kind']}', "
                f"source_id={int(att['source_id'])} (overlay=false for full-frame B-roll, "
                f"overlay=true for a picture-in-picture inset)"
            )
        else:
            lines.append(f"  • {name} ({kind})")
    return "\n".join(lines)


def _attached_font(attachments) -> Optional[tuple[int, str]]:
    """The brand font the user dragged into the chat (asset_id, family), if any."""
    for att in (attachments or []):
        if (isinstance(att, dict) and att.get("kind") == "font"
                and att.get("brand_font") and att.get("asset_id") is not None):
            try:
                return int(att["asset_id"]), str(att.get("font_family") or "")
            except (TypeError, ValueError):
                return None
    return None


# Text ops whose font must honour a dragged-in font attachment.
_FONT_TEXT_OPS = ("set_text", "add_text_popup", "add_framed_popup")


def _force_attached_font(ops: list, attachments) -> None:
    """ROOT-CAUSE FIX (font drag-drop doesn't apply): when the user explicitly
    drags a brand font in, the planner often emits a text op with `fontFamily`
    but DROPS `font_asset_id` — without it no `fontUrl` is set, so Remotion's
    ensureBrandFont() early-returns and CSS falls back to sans-serif (the font
    visibly "doesn't change"). Deterministically stamp the attached font's id +
    family onto every text op so applying it never depends on the LLM. Runs
    BEFORE validation + _enrich_framed_popup_ops (which only fills a MISSING
    font, so this force is preserved)."""
    fnt = _attached_font(attachments)
    if not fnt:
        return
    fid, fam = fnt
    for op in ops:
        if isinstance(op, dict) and op.get("op") in _FONT_TEXT_OPS:
            op["font_asset_id"] = fid
            if fam and not op.get("fontFamily"):
                op["fontFamily"] = fam


def _media_bin_block(timeline: dict) -> str:
    """List the project's media bin so the planner can ADD bin items on its own
    (by source_kind + source_id) — not only what the user dragged into chat."""
    bin_items = (timeline.get("project") or {}).get("media_bin") or []
    rows: list[str] = []
    for b in bin_items[:40]:
        if not isinstance(b, dict):
            continue
        kind = str(b.get("kind") or "")
        sid = b.get("id")
        if not kind or sid is None:
            continue
        is_img = kind == "still"
        dur = b.get("durationInFrames")
        secs = f", ~{round(int(dur) / FPS)}s" if (dur and not is_img) else ""
        title = str(b.get("title") or f"{kind} #{sid}")[:60]
        rows.append(
            f"- source_kind={kind} source_id={sid} "
            f"({'image' if is_img else 'video'}{secs}) \"{title}\""
        )
    if not rows:
        return ""
    return (
        "MEDIA BIN (available to add to the timeline — reference by the exact "
        "source_kind + source_id below; use add_media_clip):\n" + "\n".join(rows)
    )


def _chat_context(timeline: dict, analysis: dict, history, selected_clip_ids,
                  brand: Optional[dict] = None, attachments: Optional[list] = None,
                  memory: Optional[dict] = None) -> str:
    """The planner's grounding: conversation memory + selection + a digest of
    every clip (so the LLM targets clips by their real id) + popup groups + brand
    + any items the user dragged onto the chat. `memory` folds in per-clip
    provenance + the recent-edits tail so the chat planner has continuity."""
    digest = build_project_digest(timeline, analysis, None, memory=memory)
    hist = ""
    for m in (history or [])[-8:]:
        who = "USER" if str((m or {}).get("role")) == "user" else "ASSISTANT"
        txt = str((m or {}).get("text") or "").strip()[:300]
        if txt:
            hist += f"{who}: {txt}\n"
    sel = [str(s) for s in (selected_clip_ids or []) if s]
    parts: list[str] = []
    if hist:
        parts.append("CONVERSATION SO FAR:\n" + hist)
    parts.append("SELECTED CLIP IDS: " + (json.dumps(sel) if sel else "(none)"))
    att_block = _attachments_block(attachments)
    if att_block:
        parts.append(att_block)
    media_bin = _media_bin_block(timeline)
    if media_bin:
        parts.append(media_bin)
    popups = _popup_groups_block(timeline)
    if popups:
        parts.append(popups)
    brand_block = _brand_block(brand)
    if brand_block:
        parts.append(brand_block)
    parts.append(
        "PROJECT DIGEST (every clip you can target by id):\n"
        + json.dumps(digest, ensure_ascii=False, default=str)[:12000]
    )
    return "\n\n".join(parts)


_TIME_SECONDS_RE = re.compile(r"(?<![\w.])(\d+(?:\.\d+)?)\s*(?:seconds?|secs?|s)(?![\w])", re.I)
_TIME_CLOCK_RE = re.compile(r"(?<![\w:])(\d{1,2}):([0-5]\d)(?![\w:])")


def _explicit_time_frame(message: str) -> Optional[int]:
    """Deterministic backstop for 'place it at N seconds': the planner emits
    from_seconds INCONSISTENTLY, so when the user named a SINGLE unambiguous time
    we compute it ourselves. Returns the timeline frame, or None if there isn't
    exactly one time in the message (then the planner/playhead decide)."""
    secs: set[float] = set()
    for m in _TIME_SECONDS_RE.finditer(message or ""):
        try:
            secs.add(round(float(m.group(1)), 3))
        except ValueError:
            pass
    for m in _TIME_CLOCK_RE.finditer(message or ""):
        secs.add(int(m.group(1)) * 60 + int(m.group(2)))
    if len(secs) != 1:
        return None  # zero or ambiguous (multiple) → don't guess
    return max(0, int(round(secs.pop() * FPS)))


async def route_chat(
    project_id: int,
    message: str,
    history: Optional[list] = None,
    selected_clip_ids: Optional[list] = None,
    playhead_frame: int = 0,
    attachments: Optional[list] = None,
) -> dict:
    """Conversational op-planner. Returns {job, reply, feature, applied?, rev?}.
    Direct edits run synchronously (apply_plan → ONE undo step; job=None,
    applied>0); generation routes to start_job (job set)."""
    message = (message or "").strip()
    if not message:
        return {"job": None, "reply": "Tell me what you'd like to change.", "feature": None}

    row = await ve._fetch_row(project_id)
    timeline = ve._video_edit(row["metadata"]) or {}
    all_clips = timeline.get("clips") or []
    media_bin = (timeline.get("project") or {}).get("media_bin") or []
    # Only dead-end when there's NOTHING to work with — no clips AND no media in
    # the bin. If the bin has items (even with an empty timeline) the planner can
    # place them ("add my media to the timeline"), so fall through to it.
    if not all_clips and not media_bin:
        return {"job": None, "feature": None,
                "reply": "Add some media to the bin first (the “+ Add” button in "
                         "the Media tab), then tell me what to do."}
    ai = _ai_blob(row["metadata"])
    analysis = ai.get("analysis") if isinstance(ai.get("analysis"), dict) else {}
    sel_ids = [str(s) for s in (selected_clip_ids or []) if s]

    # Brand palette/fonts ground the "make it on-brand" asks (best-effort).
    brand: Optional[dict] = None
    try:
        from ..agents.producer.memory import build_brand_requirements
        brand = await build_brand_requirements()
    except Exception:  # noqa: BLE001 — brand grounding is optional
        brand = None

    ask = (
        f"USER MESSAGE: {message[:600]}\n\n"
        + _chat_context(timeline, analysis, history, sel_ids, brand=brand,
                        attachments=attachments,
                        memory=ve._video_edit_memory(row["metadata"]))
    )
    # Plan on the FAST model with thinking OFF: the default Pro model burns
    # hidden thinking tokens that overflow max_tokens and truncate the JSON
    # (→ {"_raw": …} → the "didn't catch that" dead-end). Flash with
    # thinking_budget=0 spends the whole budget on output, and it's faster for a
    # structured op-planner. Retry ONCE with a bigger budget on a parse miss.
    res = None
    for attempt, budget in enumerate(((4096), (6500))):
        try:
            res = await gemini_multimodal(
                ask, system=_CHAT_SYSTEM, json_schema=_CHAT_SCHEMA,
                model=config.vertex_gemini_fast_model,
                temperature=0.2, max_tokens=budget, thinking_budget=0,
            )
        except Exception:
            log.exception("[yas-ve-ai] project #%d: chat planning failed (attempt %d)",
                          project_id, attempt + 1)
            res = None
        if isinstance(res, dict) and res.get("_raw") is None:
            break  # got a clean structured plan
        log.warning("[yas-ve-ai] project #%d: chat plan unparseable (attempt %d, budget=%d)",
                    project_id, attempt + 1, budget)
    if not isinstance(res, dict) or res.get("_raw") is not None:
        return {"job": None, "feature": None,
                "reply": "I couldn't process that just now — try again, or say it a bit simpler."}

    action = str(res.get("action") or "reply").strip().lower()
    reply = str(res.get("reply") or "").strip() or "On it."

    # ---- Direct ops on existing clips → apply synchronously (one undo step) ---
    if action == "ops":
        ops = res.get("ops") or []
        if not isinstance(ops, list) or not ops:
            return {"job": None, "feature": None,
                    "reply": reply or "I'm not sure what to change — can you say it differently?"}
        # SECONDS → FRAMES first: the planner expresses user-given times in
        # seconds (from_seconds/duration_seconds) because it can't reliably do
        # the *30 arithmetic. Convert here, BEFORE the playhead fallback, so an
        # explicit "at 4 seconds" wins over the playhead default.
        for op in ops:
            if not isinstance(op, dict):
                continue
            try:
                if op.get("from_seconds") is not None and op.get("from") is None:
                    op["from"] = max(0, int(round(float(op["from_seconds"]) * FPS)))
                if op.get("duration_seconds") is not None and op.get("durationInFrames") is None:
                    op["durationInFrames"] = max(1, int(round(float(op["duration_seconds"]) * FPS)))
            except (TypeError, ValueError):
                pass  # malformed seconds → leave it; validation/playhead handle it
        # DETERMINISTIC TIME BACKSTOP: the planner emits from_seconds
        # inconsistently, so if the user named ONE explicit time ('at 6 seconds')
        # and exactly ONE add-overlay/media op still lacks a time, place it there
        # ourselves — 'at N seconds' must always be honoured, not the playhead.
        _ADD_TIMED = ("add_text_popup", "add_framed_popup", "add_sticker", "add_media_clip")
        timeless = [op for op in ops if isinstance(op, dict)
                    and op.get("op") in _ADD_TIMED and op.get("from") is None]
        if len(timeless) == 1:
            ft = _explicit_time_frame(message)
            if ft is not None:
                timeless[0]["from"] = ft
        # NEW overlays/media don't target a clip — if the planner left `from`
        # blank (no time given at all), drop them at the playhead so "add a
        # popup" just lands where the user is looking instead of being rejected.
        # EXCEPTION: add_media_clip with placement="append" is meant to append
        # sequentially on the main track, so leave its `from` unset — the
        # executor computes the append point (a playhead would break the chain).
        for op in ops:
            if (isinstance(op, dict)
                    and op.get("op") in _ADD_TIMED
                    and op.get("from") is None
                    and not (op.get("op") == "add_media_clip" and op.get("placement") == "append")):
                op["from"] = max(0, int(playhead_frame or 0))
        # The user dragged a font in → FORCE it onto every text op so the brand
        # font actually applies (the planner otherwise drops font_asset_id and
        # the font silently falls back to sans-serif). Before validation so the
        # stamped op is what gets applied.
        _force_attached_font(ops, attachments)
        # Framed popups carry the branded textbox-WINDOW behind the text — fill
        # the frame image + placement the planner omits so they validate (same
        # backfill the popups feature uses).
        await _backfill_framed_popup_defaults(ops, timeline, analysis)
        # Resolve "add the mouse icon sticker" (asset_query) → asset_id BEFORE
        # validation so the sticker op can pass the asset check.
        unmatched_stickers = await _resolve_sticker_ops(ops)
        valid_clip_ids = {str(c.get("id")) for c in all_clips if c.get("id")}
        valid_asset_ids = await _valid_asset_ids()
        # add_media_clip may place any source the user has made available: an
        # item in the MEDIA BIN, or a media clip DRAGGED into chat. Build the
        # allow-set from both so the LLM can act on "add my media" but still can't
        # conjure a source that isn't actually in the project.
        valid_clip_sources: set[tuple[str, int]] = set()
        for att in (attachments or []):
            if not isinstance(att, dict) or att.get("kind") != "media":
                continue
            sk = str(att.get("source_kind") or "")
            sid = att.get("source_id")
            if sk and sid is not None:
                try:
                    valid_clip_sources.add((sk, int(sid)))
                except (TypeError, ValueError):
                    pass
        for b in ((timeline.get("project") or {}).get("media_bin") or []):
            if not isinstance(b, dict):
                continue
            bk = str(b.get("kind") or "")
            bid = b.get("id")
            if bk and bid is not None:
                try:
                    valid_clip_sources.add((bk, int(bid)))
                except (TypeError, ValueError):
                    pass
        accepted, _rejected = validate_and_filter_ops(
            ops, feature="edit",
            valid_clip_ids=valid_clip_ids, valid_asset_ids=valid_asset_ids,
            valid_clip_sources=valid_clip_sources,
        )
        if not accepted:
            if unmatched_stickers:
                return {"job": None, "feature": None,
                        "reply": f"I couldn't find a saved sticker matching "
                                 f"“{unmatched_stickers[0]}”. Upload it to the "
                                 f"Library first, or tell me which asset to use."}
            return {"job": None, "feature": None,
                    "reply": "I couldn't quite place that — tell me which clip or "
                             "popup you mean (or click it), and I'll do it."}
        # Stamp framed-popup aspect + brand font so the text fits the window box.
        await _enrich_framed_popup_ops(accepted)
        try:
            # UNIQUE plan id per command: apply_plan wipes a plan's PRIOR clips
            # first, so a stable id would make each new "add a popup" delete the
            # last one. Edits modify existing clips regardless of plan id.
            result = await apply_plan(
                project_id, plan_id=f"chat{project_id}_{secrets.token_hex(3)}",
                ops=accepted, analysis=analysis)
        except Exception:
            log.exception("[yas-ve-ai] project #%d: chat apply_plan failed", project_id)
            return {"job": None, "feature": None,
                    "reply": "Hmm, that change didn't take — want to try again?"}
        if unmatched_stickers:
            reply = (reply + f" (couldn't find a sticker for “{unmatched_stickers[0]}”)").strip()
        return {
            "job": None, "feature": "ops", "reply": reply,
            "applied": int(result.get("applied", 0)),
            "rev": result.get("rev"),
        }

    # ---- Generate new media → dispatch to the existing feature pipeline -------
    if action == "feature":
        feature = str(res.get("feature") or "edit").strip().lower()
        if feature not in _CHAT_FEATURES:
            feature = "edit"
        fp = res.get("feature_params") or {}
        params: dict = {}
        if feature == "music":
            params = {"mode": str(fp.get("mode") or "auto"),
                      "query": (fp.get("query") or "").strip() or None}
        elif feature == "sfx":
            if not sel_ids:
                return {"job": None, "feature": None,
                        "reply": "Click the clip you want the sound on, then ask me again."}
            params = {"clip_id": sel_ids[0],
                      "query": (fp.get("query") or "").strip() or None, "mode": "auto"}
        elif feature == "bookend":
            params = {"role": str(fp.get("role") or "both"), "reuse": True}
        elif feature == "voice":
            return {"job": None, "feature": None,
                    "reply": "Use the Voice section in this panel to set up and change a clip's voice."}
        elif feature == "popups":
            params = {"text": (fp.get("text") or "").strip(), "hint": None}
        elif feature == "compose":
            params = {"keys": None, "style": (fp.get("instruction") or message),
                      "generate_bookends": False}
        elif feature == "duck":
            params = {}
        elif feature == "captions":
            params = {}
        elif feature == "silence":
            # optional: limit to the selected clip, else the whole timeline
            params = {"clip_id": sel_ids[0] if sel_ids else None}
        elif feature == "agent":
            # The agent gets the user's words verbatim plus whatever they had
            # selected / dragged in, so it doesn't have to re-derive intent.
            from . import yas_video_edit_agent as _agent

            if not await _agent.agent_available():
                # No reasoning brain reachable — degrade to the deterministic
                # one-click edit rather than dead-ending the user.
                log.info("[yas-ve-ai] project #%d: no reasoner — agent → edit",
                         project_id)
                feature = "edit"
                params = {"instruction": (fp.get("instruction") or message)}
            else:
                params = {
                    "instruction": (fp.get("instruction") or message),
                    "clip_id": sel_ids[0] if sel_ids else None,
                    "context": _attachments_block(attachments),
                }
        else:  # edit
            params = {"instruction": (fp.get("instruction") or message)}
        try:
            job = await start_job(project_id, feature, params)
        except HTTPException as e:
            return {"job": None, "feature": None, "reply": str(e.detail)}
        return {"job": job, "reply": reply, "feature": feature}

    # ---- reply only ----------------------------------------------------------
    return {"job": None, "reply": reply, "feature": None}


async def get_status(project_id: int) -> dict:
    """Current job blob + whether a usable analysis cache exists."""
    row = await ve._fetch_row(project_id)
    ai = _ai_blob(row["metadata"])
    analysis = ai.get("analysis") if isinstance(ai.get("analysis"), dict) else {}
    analysis_ready = bool(
        analysis.get("version") == ANALYSIS_VERSION and analysis.get("sources")
    )
    job = ai.get("job")
    return {
        "job": job if isinstance(job, dict) else None,
        "analysis_ready": analysis_ready,
        # The AUTHORITATIVE "is this job still running" answer. A raw status of
        # "planning" is not enough: a backend restart mid-run leaves the blob
        # stuck there forever, and a client that adopts it pauses autosave and
        # undo capture for the rest of the session. `_job_is_active` applies the
        # same staleness rule start_job uses, so the two can never disagree.
        "job_active": _job_is_active(job if isinstance(job, dict) else {}),
    }


# ---------------------------------------------------------------------------
# The job runner
# ---------------------------------------------------------------------------

def _agent_step_label(step: dict) -> str:
    """One human sentence for a tool call, for the job's progress line."""
    tool = str(step.get("tool") or "")
    inp = step.get("input") or {}
    at = inp.get("at_s")
    labels = {
        "inspect_timeline": "reading the timeline…",
        "read_transcript": "reading what's said…",
        "look": f"watching {at}s…" if at is not None else "watching the footage…",
        "preview": f"checking how {at}s looks…" if at is not None else "checking the result…",
        "suggest_overlay_slots": "finding safe spots for text…",
        "search_assets": f"searching the library for “{str(inp.get('query') or '')[:40]}”…",
        "get_popup_examples": "checking your best-performing popups…",
        "read_edit_log": "reviewing earlier edits…",
        "generate_image": "drawing a graphic…",
        "run_pass": f"running the {inp.get('pass_name') or ''} pass…",
        "edit": "applying the edit…",
        "finish": "wrapping up…",
    }
    return labels.get(tool, f"{tool}…")[:240]


async def _run_agent_feature(project_id: int, params: dict, analysis: dict) -> dict:
    """Drive the look->think->act agent and stream its tool calls into the job
    blob, so the user watches it work instead of staring at a spinner.

    Owns the job's terminal write. Shared by feature='agent' and by the one-click
    edit, which prefers the agent when a reasoning brain is reachable."""
    from . import yas_video_edit_agent as _agent  # lazy: keeps the graph acyclic

    steps_seen: list[dict] = []
    pending: list[asyncio.Task] = []

    def _on_step(step: dict) -> None:
        # Fire-and-forget: progress reporting must never slow or break the run.
        steps_seen.append(step)
        try:
            pending.append(asyncio.get_running_loop().create_task(
                _merge_job(project_id, {
                    "progress": _agent_step_label(step),
                    "agent_steps": steps_seen[-14:],
                })
            ))
        except RuntimeError:
            pass

    await _merge_job(project_id, {"status": "planning",
                                  "progress": "watching your video…"})
    res = await _agent.run_job(project_id, params, analysis=analysis,
                               on_step=_on_step)

    # Drain the progress writes BEFORE the terminal one, or a late-landing step
    # update overwrites the final summary with a stale "watching 4.2s…".
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)

    applied = int(res.get("applied", 0))
    failed = _failed_entries(res.get("rejected") or [])
    error = res.get("error")
    terminal = {
        # A mid-run failure still committed whatever it applied — report that
        # honestly so the UI refetches and the user sees the real timeline.
        "status": "failed" if error else "done",
        "progress": (res.get("reply") or "done")[:240],
        "ops_applied": applied,
        # Surface what the validator bounced, so the finished-job card can show
        # "(N skipped)" instead of silently hiding rejected edits.
        "ops_failed": failed,
        "rev_after": res.get("rev"),
        "agent_steps": steps_seen[-24:],
        "agent_provider": res.get("provider"),
        "agent_model": res.get("model"),
        "finished_at": _now_iso(),
    }
    if error:
        terminal["error"] = error
    await _merge_job(project_id, terminal)
    log.info("[yas-ve-ai] project #%d agent: %d op(s) via %s/%s (%s)%s",
             project_id, applied, res.get("provider"), res.get("model"),
             res.get("stopped"), f" — FAILED: {error}" if error else "")
    if not error:
        # Capture the finished timeline as a style recipe, like the classic edit.
        _store_edit_recipe_bg(project_id, analysis)
    return res


async def _run(project_id: int, feature: str, params: dict, job_id: str) -> None:
    """Background task: analyze → plan → apply, with the job blob updated at
    every step. Any exception lands the job in status=failed (never raises)."""
    def _progress_cb(msg: str) -> None:
        # Called synchronously from inside the running loop by the analysis
        # module — fire-and-forget the DB patch.
        try:
            asyncio.get_running_loop().create_task(
                _patch_ai(project_id, ["job", "progress"], str(msg)[:300])
            )
        except RuntimeError:
            pass

    try:
        # ---- 1. ANALYZE -----------------------------------------------------
        await _merge_job(project_id, {"status": "analyzing", "progress": "analyzing media…"})
        force = bool(params.get("force")) if feature == _ANALYZE else False
        try:
            analysis = await get_or_build_analysis(
                project_id, force=force, on_progress=_progress_cb,
            )
        except Exception:
            # Bookends only use the analysis for the card TEXTS — they degrade
            # gracefully (deterministic fallback) instead of failing the job.
            # B-roll likewise: analysis only flavors card texts + music brief.
            # The agent degrades too: without an analysis it loses transcripts
            # and scene summaries, but it can still inspect the timeline, pull
            # frames with ffmpeg on demand, and edit.
            if feature not in ("bookend", "broll", "agent"):
                raise
            log.exception("[yas-ve-ai] project #%d: analysis failed — %s degrades",
                          project_id, feature)
            analysis = {}
        if feature == _ANALYZE:
            await _merge_job(project_id, {
                "status": "done",
                "progress": f"analysis ready ({len(analysis.get('sources') or {})} source(s))",
                "finished_at": _now_iso(),
            })
            return

        if feature == "agent":
            await _run_agent_feature(project_id, params, analysis)
            return

        if feature == "bookend":
            await _run_bookend(project_id, params, analysis)
            return

        if feature == "broll":
            # B-roll preset: fully deterministic (no LLM ops) — order for flow,
            # frame with title cards or Veo bookends, lay a music bed.
            await _run_broll(project_id, params, analysis)
            return

        if feature == "edit":
            # One-click full edit: its own TWO-PHASE runner (structure →
            # re-ground → overlays+music), mirroring the compose+bookend
            # two-plan pattern. Works with empty params (no instruction).
            await _run_edit(project_id, params, analysis, job_id)
            return

        if feature == "sfx":
            # Per-clip sound effect (no LLM): generate/pick an SFX for the
            # selected clip and lay it under it on the AI SFX lane.
            await _run_sfx(project_id, params, analysis)
            return

        if feature == "duck":
            # Transcript-powered: lower the music under the speaking voice.
            await _run_duck(project_id, params, analysis)
            return

        if feature == "captions":
            # Transcript-powered: synced lower-third captions for the whole video.
            await _run_captions(project_id, params, analysis)
            return

        if feature == "silence":
            # Transcript-powered: trim dead air from clips and repack the track.
            await _run_silence(project_id, params, analysis)
            return

        # ---- 2. PLAN ----------------------------------------------------------
        await _merge_job(project_id, {"status": "planning", "progress": "planning edits…"})
        row = await ve._fetch_row(project_id)
        timeline = ve._video_edit(row["metadata"])
        if not (timeline.get("clips") or []):
            raise RuntimeError("the timeline has no clips — add media before running the AI editor")
        _roles = await _resolve_clip_roles(timeline.get("clips") or [])
        digest = build_project_digest(
            timeline, analysis, _roles, memory=ve._video_edit_memory(row["metadata"]),
        )
        plan_id = secrets.token_hex(4)

        if feature == "voice":
            ops = [await _voice_op(project_id, timeline, params)]
        elif feature == "music":
            op = await _music_op(project_id, timeline, analysis, params, plan_id)
            if op is None:
                return  # parked at waiting_provider — the Suno poller finishes it
            ops = [op]
        else:
            ops = await _plan_with_llm(project_id, feature, timeline, analysis, digest, params)
            if feature == "popups":
                # The popup TEXT is the user's, verbatim — never depend on the
                # LLM echoing it back (it has returned empty `text` in the wild).
                _force_user_popup_text(ops, params)
                # The planner sometimes emits add_framed_popup without a
                # frame_asset_id or `from` (both required) — backfill sane
                # defaults so a flaky plan still lands a framed popup.
                await _backfill_framed_popup_defaults(ops, timeline, analysis)

        # ---- 3. APPLY -----------------------------------------------------------
        await _merge_job(project_id, {
            "status": "applying", "plan_id": plan_id,
            "progress": f"applying {len(ops)} op(s)…",
        })
        valid_clip_ids = {
            str(c.get("id")) for c in (timeline.get("clips") or []) if c.get("id")
        }
        valid_asset_ids = await _valid_asset_ids()
        accepted, rejected = validate_and_filter_ops(
            ops, feature=feature,
            valid_clip_ids=valid_clip_ids, valid_asset_ids=valid_asset_ids,
        )
        # Ground add_music ducking + resolve sourceless add_music via the
        # library (shared with the edit Phase B).
        await _resolve_music_ops(project_id, timeline, analysis, accepted, rejected, plan_id)

        # Stamp accepted add_framed_popup ops with the frame image's aspect
        # ratio so the executor's text-fit math matches the real asset.
        await _enrich_framed_popup_ops(accepted)

        result = await apply_plan(project_id, plan_id=plan_id, ops=accepted, analysis=analysis)

        ops_failed = _failed_entries(rejected) + list(result.get("failed") or [])

        # Compose with the bookend toggle ON: the main clips are sequenced —
        # now ensure intro/outro around them (reuse / generate+park / place).
        if feature == "compose" and bool(params.get("generate_bookends")):
            await _compose_bookends(project_id, params, analysis, result, ops_failed)
            return

        if feature == "compose":
            await _enforce_bookend_order(project_id)

        # ---- 4. DONE ----------------------------------------------------------------
        await _merge_job(project_id, {
            "status": "done",
            "progress": f"applied {result.get('applied', 0)} op(s)"
                        + (f", {len(ops_failed)} failed/rejected" if ops_failed else ""),
            "ops_applied": int(result.get("applied", 0)),
            "ops_failed": ops_failed,
            "rev_after": int(result.get("rev", 0)),
            "finished_at": _now_iso(),
        })
        log.info("[yas-ve-ai] project #%d job %s (%s): done — applied=%d failed=%d rev=%s",
                 project_id, job_id, feature, result.get("applied", 0),
                 len(ops_failed), result.get("rev"))

        # Edit-recipe RAG capture (compose path; the edit path stores inside
        # _run_edit). Fire-and-forget — never affects the job result.
        if feature == "compose":
            _store_edit_recipe_bg(project_id, analysis)

    except Exception as e:  # noqa: BLE001 — the job blob is the error channel
        log.exception("[yas-ve-ai] project #%d job %s (%s) failed", project_id, job_id, feature)
        _raw = str(e)[:500] or e.__class__.__name__
        _msg = (
            "The AI is rate-limited right now — wait a minute and try again."
            if ("429" in _raw or "RESOURCE_EXHAUSTED" in _raw.upper())
            else _raw
        )
        try:
            await _merge_job(project_id, {
                "status": "failed",
                "error": _msg,
                "finished_at": _now_iso(),
            })
        except Exception:  # noqa: BLE001
            log.exception("[yas-ve-ai] project #%d: could not record job failure", project_id)


# ---------------------------------------------------------------------------
# Feature: edit (one-click AUTONOMOUS full edit — two phases, two plan ids)
# ---------------------------------------------------------------------------

def _failed_entries(rejected: list[dict]) -> list[dict]:
    """Normalize validator rejections into the job blob's ops_failed shape."""
    return [
        {
            "op": (r["op"].get("op") if isinstance(r.get("op"), dict) else r.get("op")),
            "error": r.get("error"),
        }
        for r in rejected
    ]


def _restrict_ops_to_phase(
    ops: list[dict], allowed: frozenset[str], rejected: list[dict],
) -> list[dict]:
    """Phase gate for the two-phase edit: keep only ops whose name is in this
    phase's allowed set, recording the rest as rejected. Done HERE (not via a
    validate_and_filter_ops kwarg) because yas_video_edit_ai_ops is owned by
    another workstream — feature='edit' validation stays full-vocabulary and
    the orchestrator narrows it per phase."""
    kept: list[dict] = []
    for op in ops or []:
        name = op.get("op") if isinstance(op, dict) else None
        if name in allowed:
            kept.append(op)
        else:
            rejected.append({
                "op": op,
                "error": f"op {name!r} outside this edit phase "
                         f"(allowed: {sorted(allowed)})",
            })
    return kept


async def _resolve_music_ops(
    project_id: int, timeline: dict, analysis: dict,
    accepted: list[dict], rejected: list[dict], plan_id: str,
) -> None:
    """Ground add_music ducking with REAL speech spans whenever the planner
    produced one without segments (it only sees transcript text), and resolve
    sourceless add_music ops via the library (the planner may leave the source
    unset; mode is FORCED to "library" — "auto" could park the whole multi-op
    plan at waiting_provider, which the poller only knows how to finish for a
    single pending op). An empty library drops the op gracefully (recorded in
    rejected) instead of failing the plan. Mutates accepted/rejected in place."""
    for op in list(accepted):
        if op.get("op") != "add_music":
            continue
        op.setdefault("duck", True)
        if op.get("duck") and not op.get("speech_segments"):
            op["speech_segments"] = _speech_segments_timeline(timeline, analysis)
        if op.get("source_asset_id") or op.get("source_upload_id"):
            continue
        try:
            resolved = await _music_op(
                project_id, timeline, analysis,
                {"mode": "library", "query": op.get("style_or_query")}, plan_id,
            )
            if not resolved or not resolved.get("source_asset_id"):
                raise RuntimeError("library lookup returned no music asset")
            op["source_asset_id"] = int(resolved["source_asset_id"])
        except Exception as exc:  # noqa: BLE001 — drop the op, keep the plan
            accepted.remove(op)
            rejected.append({
                "op": op,
                "error": f"music source unresolved — dropped: {str(exc)[:200]}",
            })


async def _resolve_sticker_ops(ops: list[dict]) -> list[str]:
    """Fill add_sticker ops that name a sticker by DESCRIPTION (`asset_query`,
    e.g. 'mouse cursor icon') with a concrete producer_assets id via Library
    semantic search (mirror of _resolve_music_ops). Returns the queries that
    matched NOTHING so the caller can tell the user. Mutates `ops` in place; run
    BEFORE validate_and_filter_ops so the resolved op passes the asset check."""
    unmatched: list[str] = []
    for op in ops or []:
        if not isinstance(op, dict) or op.get("op") != "add_sticker":
            continue
        if op.get("asset_id"):  # already concrete
            continue
        query = str(op.get("asset_query") or "").strip()
        if not query:
            continue
        try:
            found = await _search_assets(query, kind="image", limit=1)
        except Exception:  # noqa: BLE001 — grounding is optional
            found = []
        if found:
            op["asset_id"] = int(found[0]["asset_id"])
        else:
            unmatched.append(query)
    return unmatched


async def _intro_title_window(project_id: int, timeline: dict, digest: dict) -> dict:
    """Synthesized candidate window for the edit pass's intro TITLE CARD:
    upper-third, ~2.5 s, starting just after the open — or just after the
    intro BOOKEND clip when one is already placed (per
    _bookend_roles_on_timeline), so the card never sits on the branded intro."""
    proj = (timeline or {}).get("project") or {}
    height = int(proj.get("height") or 1920)
    start = 12
    try:
        placed = await _bookend_roles_on_timeline(project_id, timeline)
        if "intro" in placed:
            vids = sorted(
                (c for c in (timeline.get("clips") or [])
                 if (c.get("kind") or "video") == "video"),
                key=lambda c: int(c.get("from") or 0),
            )
            if vids:
                first = vids[0]
                start = (int(first.get("from") or 0)
                         + int(first.get("durationInFrames") or 0) + 12)
    except Exception:  # noqa: BLE001 — the default open slot still works
        log.exception("[yas-ve-ai] intro-window bookend check failed")
    line = _opening_line(digest)
    return {
        "from": start,
        "durationInFrames": 75,
        "x": 0,
        "y": round(-0.18 * height),
        "why": "intro title card — hook from the opening line: "
               + (line or "(no transcript — use the visual subject)"),
    }


def _mark_intro_title_card(ops: list[dict]) -> None:
    """Stamp the OPENING add_framed_popup with anim='title' when the executor
    supports that preset (the popup-animation work lands in
    yas_video_edit_ai_ops._ANIM_PRESETS, owned by another workstream); until
    then fall back to 'pop' so validation never rejects the card. The plan
    schema's anim enum may not include 'title' yet, so this deterministic
    stamp — not the planner — is what actually sets it. Runs AFTER
    _backfill_framed_popup_defaults (so `from` is set) and BEFORE
    validate_and_filter_ops."""
    framed = [
        op for op in ops
        if isinstance(op, dict) and op.get("op") == "add_framed_popup"
        and op.get("from") is not None
    ]
    if not framed:
        return
    try:
        intro = min(framed, key=lambda o: int(o.get("from") or 0))
        if int(intro.get("from") or 0) > 4 * FPS:
            return  # nothing near the open — no title card to mark
    except (TypeError, ValueError):
        return
    presets = getattr(ai_ops, "_ANIM_PRESETS", None) or {"fade", "pop", "slide_up"}
    if "title" in presets:
        intro["anim"] = "title"
    elif intro.get("anim") not in presets:
        intro["anim"] = "pop"


def _backfill_edit_popup_text(ops: list, windows: list, digest: dict) -> None:
    """The one-click edit has no user-supplied popup text — the planner is told
    to WRITE the hook, but Gemini sometimes leaves `text` empty (the validator
    then drops the popup, so an autonomous edit lands zero captions). Derive a
    short hook from the transcript AT THAT MOMENT (the matching candidate window
    quotes it in `why`) or the opening line, so the intro card + section popups
    still appear. Runs before validation, only fills BLANK text."""
    import re
    by_from: dict[int, dict] = {}
    for w in windows or []:
        f = w.get("from")
        if f is not None:
            by_from.setdefault(int(f), w)
    opening = _opening_line(digest)
    topic = str(((digest or {}).get("project") or {}).get("title") or "").strip()

    def _short(s: str, n: int = 6) -> str:
        words = re.sub(r"\s+", " ", str(s or "")).strip().split(" ")
        return " ".join(w for w in words[:n] if w).strip().strip('"').rstrip(".,!?;:")

    for op in ops:
        if not (isinstance(op, dict) and op.get("op") in ("add_framed_popup", "add_text_popup")):
            continue
        if isinstance(op.get("text"), str) and op["text"].strip():
            continue
        text = ""
        try:
            w = by_from.get(int(op.get("from"))) if op.get("from") is not None else None
        except (TypeError, ValueError):
            w = None
        if w:
            why = str(w.get("why") or "")
            m = re.search(r'during:\s*"([^"]+)"', why)
            if m:
                text = m.group(1)
            elif "opening line:" in why:
                text = why.split("opening line:", 1)[1]
        if not text:
            text = opening or topic
        hook = _short(text)
        if hook:
            op["text"] = hook


async def _resolve_clip_roles(clips: list[dict]) -> dict[str, str]:
    """Map each animation-sourced clip id → its Talking-Head role
    ('intro'|'main'|'outro'|'scene_N'|…) from yas_animations.metadata.role.
    Non-animation clips are omitted. Best-effort (never raises)."""
    anim_to_clip: dict[int, str] = {}
    for c in clips or []:
        src = c.get("source") or {}
        if src.get("kind") == "animation" and src.get("animation_id") is not None:
            try:
                anim_to_clip[int(src["animation_id"])] = c.get("id")
            except (TypeError, ValueError):
                continue
    if not anim_to_clip:
        return {}
    out: dict[str, str] = {}
    try:
        async with get_conn() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT id, metadata->>'role' AS role FROM yas_animations WHERE id = ANY(%s)",
                    (list(anim_to_clip.keys()),),
                )
                for r in await cur.fetchall():
                    cid = anim_to_clip.get(int(r["id"]))
                    role = (r.get("role") or "").strip().lower()
                    if cid and role:
                        out[cid] = role
    except Exception:  # noqa: BLE001 — ordering is best-effort
        log.exception("[yas-ve-ai] clip role resolve failed (non-fatal)")
    return out


async def _enforce_bookend_order(project_id: int) -> None:
    """Deterministic guarantee: on each video track that has role'd clips, move
    every 'intro' clip to the FRONT and every 'outro' to the END, keeping the
    middle (main/scene/roleless) clips in their current relative order. No-op
    when nothing is mis-ordered or no roles exist. Best-effort — never raises."""
    try:
        row = await ve._fetch_row(project_id)
        timeline = ve._video_edit(row["metadata"])
        clips = timeline.get("clips") or []
        roles = await _resolve_clip_roles(clips)
        if not any(r in ("intro", "outro") for r in roles.values()):
            return
        from collections import defaultdict
        by_track: dict[str, list[dict]] = defaultdict(list)
        for c in clips:
            if (c.get("kind") or "video") == "video":
                by_track[c.get("trackId")].append(c)

        def _rank(c: dict) -> int:
            r = roles.get(c.get("id"))
            if r == "intro":
                return 0
            if r == "outro":
                return 2
            return 1

        for tid, tclips in by_track.items():
            if not any(roles.get(c.get("id")) in ("intro", "outro") for c in tclips):
                continue
            cur = sorted(tclips, key=lambda c: int(c.get("from", 0) or 0))
            desired = sorted(cur, key=_rank)  # stable sort → preserves order within each rank
            if [c.get("id") for c in desired] != [c.get("id") for c in cur]:
                op = {"op": "reorder_clips", "track_id": tid,
                      "clip_ids": [c.get("id") for c in desired]}
                await apply_plan(project_id, plan_id=secrets.token_hex(4), ops=[op])
                log.info("[yas-ve-ai] project #%d: enforced intro→main→outro order on track %s",
                         project_id, tid)
    except Exception:  # noqa: BLE001
        log.exception("[yas-ve-ai] enforce bookend order failed (non-fatal)")


async def _run_edit(project_id: int, params: dict, analysis: dict, job_id: str) -> None:
    """One-click 'Edit my video' (called from _run; exceptions land the job in
    status=failed there). Requires NO instruction — EDIT_SYSTEM's staged brief
    drives the whole pass; params.instruction, when present, is woven in as a
    nudge. Two phases, each with its OWN plan id (apply_plan is idempotent per
    plan, so the two never clobber each other — same pattern as
    compose + bookend placement):

      Phase A — STRUCTURE: reorder/trim/transitions/mute/filters → apply.
      Re-ground             fresh timeline + digest + popup windows + speech
                            spans (popup/ducking geometry must follow the
                            POST-structure layout).
      Phase B — OVERLAYS:   framed intro title card + section popups +
                            stickers + ONE library-resolved music bed → apply.

    Fully synchronous — music resolves via the LIBRARY (never Suno), so the
    job never parks at waiting_provider.

    PREFERRED PATH: when a reasoning brain is reachable, the whole edit is
    handed to the agent instead. It watches the footage, decides the shape from
    the content, and previews its own work — where this two-phase pass applies
    the same fixed template to every video. The template stays as the fallback
    so the button keeps working with no Claude and no Gemini reasoning."""
    instruction = str(params.get("instruction") or "").strip()

    from . import yas_video_edit_agent as _agent  # lazy: keeps the graph acyclic

    # Guard BEFORE the hand-off: the classic path fails cleanly on an empty
    # timeline and the agent should not be asked to edit nothing.
    _row0 = await ve._fetch_row(project_id)
    if not ((ve._video_edit(_row0["metadata"]) or {}).get("clips") or []):
        raise RuntimeError("the timeline has no clips — add media before running the AI editor")

    if await _agent.agent_available():
        brief = (
            "Edit this whole video into something finished and watchable. "
            "You decide everything; the user pressed one button and expects a "
            "result, so never ask and never hedge.\n"
            "Work it like an editor: watch the footage and read the transcript "
            "first, open on the strongest moment, cut the dead air, cut the "
            "clips together deliberately, add a text layer that reinforces what "
            "is actually said, and lay a music bed that matches the mood "
            "(run_pass handles captions/silence/duck/music exactly — use it). "
            "Preview the moments you changed and fix what looks wrong.\n"
            "Let the content decide the shape: a calm explainer and a hype reel "
            "must not come out looking the same."
        )
        if instruction:
            brief += f"\n\nThe user also said: {instruction}"
        log.info("[yas-ve-ai] project #%d: one-click edit → agent", project_id)
        res = await _run_agent_feature(project_id, {"instruction": brief}, analysis)
        if not res.get("error") or int(res.get("applied", 0)) > 0:
            # Either it worked, or it died partway but committed real edits —
            # in both cases the timeline has moved and re-running the template
            # over the top would fight it.
            return
        # The agent achieved nothing (a passing probe is weak evidence a 24-step
        # run survives). The user pressed ONE button, so fall through to the
        # deterministic pass rather than handing them a failed job.
        log.warning("[yas-ve-ai] project #%d: agent edit failed with no changes "
                    "(%s) — falling back to the classic two-phase edit",
                    project_id, res.get("error"))
        await _merge_job(project_id, {
            "status": "planning", "error": None,
            "progress": "the smart editor stalled — running the classic edit…",
        })

    log.info("[yas-ve-ai] project #%d: classic two-phase edit", project_id)

    # ---- PHASE A: structure --------------------------------------------------
    await _merge_job(project_id, {
        "status": "planning",
        "progress": "phase 1/2: planning the structure (order, trims, transitions)…",
    })
    row = await ve._fetch_row(project_id)
    timeline = ve._video_edit(row["metadata"])
    if not (timeline.get("clips") or []):
        raise RuntimeError("the timeline has no clips — add media before running the AI editor")
    _roles = await _resolve_clip_roles(timeline.get("clips") or [])
    digest = build_project_digest(
        timeline, analysis, _roles, memory=ve._video_edit_memory(row["metadata"]),
    )
    plan_a = secrets.token_hex(4)

    ops_a = await _plan_with_llm(
        project_id, "edit", timeline, analysis, digest, params, phase="structure",
    )
    rejected_a: list[dict] = []
    ops_a = _restrict_ops_to_phase(ops_a, _EDIT_PHASE_A_OPS, rejected_a)
    ops_a = drop_unapplyable_transitions(ops_a, timeline.get("clips") or [])
    valid_asset_ids = await _valid_asset_ids()
    valid_clip_ids = {
        str(c.get("id")) for c in (timeline.get("clips") or []) if c.get("id")
    }
    accepted_a, rej = validate_and_filter_ops(
        ops_a, feature="edit",
        valid_clip_ids=valid_clip_ids, valid_asset_ids=valid_asset_ids,
    )
    rejected_a += rej

    result_a: dict = {"applied": 0, "failed": [],
                      "rev": int((timeline.get("history") or {}).get("rev", 0))}
    if accepted_a:
        await _merge_job(project_id, {
            "status": "applying", "plan_id": plan_a,
            "progress": f"phase 1/2: applying {len(accepted_a)} structure op(s)…",
        })
        result_a = await apply_plan(project_id, plan_id=plan_a, ops=accepted_a, analysis=analysis)

    # ---- RE-GROUND on the post-structure timeline -----------------------------
    row = await ve._fetch_row(project_id)
    timeline = ve._video_edit(row["metadata"])
    _roles = await _resolve_clip_roles(timeline.get("clips") or [])
    digest = build_project_digest(
        timeline, analysis, _roles, memory=ve._video_edit_memory(row["metadata"]),
    )

    # ---- PHASE B: overlays + music --------------------------------------------
    await _merge_job(project_id, {
        "status": "planning",
        "ops_applied": int(result_a.get("applied", 0)),  # phase A survives a B failure
        "progress": "phase 2/2: planning the title card, popups and music…",
    })
    plan_b = secrets.token_hex(4)
    try:
        ops_b = await _plan_with_llm(
            project_id, "edit", timeline, analysis, digest, params, phase="overlays",
        )
    except Exception as exc:  # noqa: BLE001 — keep Phase A's applied structure
        if int(result_a.get("applied", 0)) > 0:
            log.warning(
                "[yas-ve-ai] project #%d: phase B planning failed (%s) — keeping "
                "phase A structure (done-partial)", project_id, str(exc)[:160],
            )
            await _merge_job(project_id, {
                "status": "done",
                "plan_id": plan_a,
                "plan_ids": [plan_a],
                "ops_applied": int(result_a.get("applied", 0)),
                "ops_failed": _failed_entries(rejected_a) + list(result_a.get("failed") or []),
                "rev_after": int(result_a.get("rev", 0)),
                "progress": (
                    f"structure done — {result_a.get('applied', 0)} op(s); overlays "
                    "skipped (the AI was busy). Run Edit again to add captions, music & SFX."
                ),
                "finished_at": _now_iso(),
            })
            _store_edit_recipe_bg(project_id, analysis)
            return
        raise
    rejected_b: list[dict] = []
    ops_b = _restrict_ops_to_phase(ops_b, _EDIT_PHASE_B_OPS, rejected_b)
    # Same popup hardening as feature='popups' — Phase B emits framed popups too.
    await _backfill_framed_popup_defaults(ops_b, timeline, analysis)
    _mark_intro_title_card(ops_b)
    # Derive popup text the planner left blank (no user text in a one-click edit)
    # from the transcript at that moment / the opening hook, so captions land.
    _edit_windows = [await _intro_title_window(project_id, timeline, digest)]
    _edit_windows += candidate_popup_windows(timeline, analysis)
    _backfill_edit_popup_text(ops_b, _edit_windows, digest)
    ops_b = drop_empty_text_popups(ops_b)
    valid_clip_ids = {
        str(c.get("id")) for c in (timeline.get("clips") or []) if c.get("id")
    }
    accepted_b, rej = validate_and_filter_ops(
        ops_b, feature="edit",
        valid_clip_ids=valid_clip_ids, valid_asset_ids=valid_asset_ids,
    )
    rejected_b += rej
    await _resolve_music_ops(project_id, timeline, analysis, accepted_b, rejected_b, plan_b)
    await _enrich_framed_popup_ops(accepted_b)
    await _weave_overlay_sfx(project_id, accepted_b)

    result_b: dict = {"applied": 0, "failed": [], "rev": int(result_a.get("rev", 0))}
    if accepted_b:
        await _merge_job(project_id, {
            "status": "applying", "plan_id": plan_b,
            "progress": f"phase 2/2: applying {len(accepted_b)} overlay/music op(s)…",
        })
        result_b = await apply_plan(project_id, plan_id=plan_b, ops=accepted_b, analysis=analysis)

    # Guarantee intro→main→outro regardless of what the planner emitted.
    await _enforce_bookend_order(project_id)

    # ---- DONE -------------------------------------------------------------------
    ops_failed = (
        _failed_entries(rejected_a) + list(result_a.get("failed") or [])
        + _failed_entries(rejected_b) + list(result_b.get("failed") or [])
    )
    applied_total = int(result_a.get("applied", 0)) + int(result_b.get("applied", 0))
    await _merge_job(project_id, {
        "status": "done",
        "plan_id": plan_b,
        "plan_ids": [plan_a, plan_b],
        "progress": (
            f"full edit done — {result_a.get('applied', 0)} structure + "
            f"{result_b.get('applied', 0)} overlay/music op(s)"
            + (f", {len(ops_failed)} failed/rejected" if ops_failed else "")
        ),
        "ops_applied": applied_total,
        "ops_failed": ops_failed,
        "rev_after": int(result_b.get("rev", 0)),
        "finished_at": _now_iso(),
    })
    log.info(
        "[yas-ve-ai] project #%d job %s (edit): done — structure=%d overlays=%d "
        "failed=%d rev=%s",
        project_id, job_id, result_a.get("applied", 0), result_b.get("applied", 0),
        len(ops_failed), result_b.get("rev"),
    )

    # Edit-recipe RAG capture: snapshot the FINAL (post-Phase-B) timeline as a
    # style recipe for future edits. Fire-and-forget — never affects the job.
    _store_edit_recipe_bg(project_id, analysis)


async def _run_sfx(project_id: int, params: dict, analysis: dict) -> None:
    """Per-clip sound effect. Resolves ONE sfx (generated when possible, else a
    bundled/synth fallback) and lays it at the selected clip's start. No LLM."""
    clip_id = str(params.get("clip_id") or "").strip()
    query = str(params.get("query") or "").strip()
    row = await ve._fetch_row(project_id)
    timeline = ve._video_edit(row["metadata"])
    clips = timeline.get("clips") or []
    clip = next((c for c in clips if str(c.get("id")) == clip_id), None) if clip_id else None

    # Category from the clip kind (text/title → whoosh, sticker/image → pop);
    # a free-text query overrides it. ElevenLabs generation is non-deterministic
    # so repeated calls give DIFFERENT sounds.
    kind = (clip or {}).get("kind")
    category = {"text": "whoosh", "image": "pop"}.get(kind, "pop")
    start = int((clip or {}).get("from") or 0)

    await _merge_job(project_id, {"status": "applying", "progress": "making a sound effect…"})
    sfx = await yas_sfx.resolve_sfx(
        category=None if query else category,
        prompt=query or None,
        duration_sec=0.9,
        avoid=set(),
    )
    if not sfx:
        await _merge_job(project_id, {
            "status": "done",
            "ops_applied": 0,
            "ops_failed": [],
            "progress": "couldn't make a sound effect — try again",
            "finished_at": _now_iso(),
        })
        return

    plan_id = secrets.token_hex(4)
    op = {
        "op": "add_sfx",
        "from": start,
        "durationInFrames": int(sfx.get("durationInFrames") or 24),
        "source_upload_id": int(sfx["source_upload_id"]),
    }
    result = await apply_plan(project_id, plan_id=plan_id, ops=[op])
    await _merge_job(project_id, {
        "status": "done",
        "plan_id": plan_id,
        "ops_applied": int(result.get("applied", 0)),
        "ops_failed": list(result.get("failed") or []),
        "rev_after": int(result.get("rev", 0)),
        "progress": f"added a sound effect ({sfx.get('key')})",
        "finished_at": _now_iso(),
    })
    log.info("[yas-ve-ai] project #%d sfx: applied=%d key=%s",
             project_id, result.get("applied", 0), sfx.get("key"))


# ---------------------------------------------------------------------------
# Transcript-powered quick features: duck / captions / silence (no LLM)
# ---------------------------------------------------------------------------

def _find_music_clip(clips: list[dict]) -> Optional[dict]:
    """The background-music clip to duck: prefer the AI music lane, else the
    longest audio-file clip that isn't the AI voice lane."""
    lane = [c for c in clips if c.get("trackId") == ai_ops.AI_MUSIC_TRACK_ID]
    if lane:
        return max(lane, key=lambda c: int(c.get("durationInFrames") or 0))
    audio = [
        c for c in clips
        if (c.get("source") or {}).get("audioFile")
        and c.get("trackId") != ai_ops.AI_VOICE_TRACK_ID
    ]
    return max(audio, key=lambda c: int(c.get("durationInFrames") or 0)) if audio else None


def _caption_segments_timeline(timeline: dict, analysis: dict) -> list[dict]:
    """Per-segment transcript spans (with text) re-mapped to TIMELINE frames —
    one entry per spoken phrase. Like _speech_segments_timeline but keeps the
    words and does NOT merge, so each becomes one caption."""
    sources = (analysis or {}).get("sources") or {}
    out: list[dict] = []
    for clip in (timeline or {}).get("clips") or []:
        if (clip.get("kind") or "video") != "video":
            continue
        kind, sid = ve._clip_source_kind_id(clip)
        if not kind or sid is None:
            continue
        src_an = sources.get(f"{kind}:{int(sid)}") or {}
        segs = ((src_an.get("transcript") or {}).get("segments") or [])
        tb, te, offset = _clip_window(clip)
        for seg in segs:
            txt = " ".join(str(seg.get("text") or "").split()).strip()
            if not txt:
                continue
            try:
                s = max(int(seg.get("start_f", 0)), tb)
                e = min(int(seg.get("end_f", 0)), te)
            except (TypeError, ValueError):
                continue
            if e > s:
                out.append({"start_f": s + offset, "end_f": e + offset, "text": txt[:90]})
    out.sort(key=lambda x: x["start_f"])
    return out[:120]


async def _brand_caption_style() -> tuple[str, str]:
    """(color, fontFamily) for captions — brand primary hex + first brand font
    when available, else white / Inter. Best-effort."""
    color, font = "#FFFFFF", "Inter"
    try:
        from ..agents.producer.memory import build_brand_requirements
        brand = await build_brand_requirements()
        cols = [str(c) for c in (brand.get("primary_colors") or []) if str(c).startswith("#")]
        if cols:
            color = cols[0]
        fonts = [str(f) for f in (brand.get("fonts") or []) if str(f).strip()]
        if fonts:
            font = fonts[0]
    except Exception:  # noqa: BLE001 — brand styling is optional
        pass
    return color, font


async def _run_duck(project_id: int, params: dict, analysis: dict) -> None:
    """Duck the music bed under the speaking voice using the transcript (no LLM):
    find the music clip and write a ducking volume curve as ONE set_keyframes op."""
    row = await ve._fetch_row(project_id)
    timeline = ve._video_edit(row["metadata"])
    clips = timeline.get("clips") or []
    music = _find_music_clip(clips)
    if not music:
        await _merge_job(project_id, {
            "status": "done", "ops_applied": 0, "ops_failed": [],
            "progress": "no music track to duck — add music first",
            "finished_at": _now_iso(),
        })
        return
    speech = _speech_segments_timeline(timeline, analysis)
    if not speech:
        await _merge_job(project_id, {
            "status": "done", "ops_applied": 0, "ops_failed": [],
            "progress": "no speech detected to duck under",
            "finished_at": _now_iso(),
        })
        return
    await _merge_job(project_id, {"status": "applying", "progress": "ducking music under the voice…"})
    clip_from = int(music.get("from") or 0)
    dur = int(music.get("durationInFrames") or 0) or _project_duration_f(timeline)
    points = ai_ops._ducking_volume_keyframes(
        clip_from=clip_from, duration=dur, speech_segments=speech, bed=0.5,
    )
    plan_id = f"duck{project_id}"
    op = {"op": "set_keyframes", "clip_id": str(music.get("id")),
          "property": "volume", "points": points}
    result = await apply_plan(project_id, plan_id=plan_id, ops=[op])
    await _merge_job(project_id, {
        "status": "done", "plan_id": plan_id,
        "ops_applied": int(result.get("applied", 0)),
        "ops_failed": list(result.get("failed") or []),
        "rev_after": int(result.get("rev", 0)),
        "progress": "ducked the music under the voice",
        "finished_at": _now_iso(),
    })
    log.info("[yas-ve-ai] project #%d duck: applied=%d spans=%d",
             project_id, result.get("applied", 0), len(speech))


async def _run_captions(project_id: int, params: dict, analysis: dict) -> None:
    """Auto-captions: one add_text_popup per transcript phrase, synced to the
    timeline, lower-third, on-brand. The overlay resolver guarantees no stacking;
    a STABLE plan_id makes re-running replace the prior caption set."""
    row = await ve._fetch_row(project_id)
    timeline = ve._video_edit(row["metadata"])
    segs = _caption_segments_timeline(timeline, analysis)
    if not segs:
        await _merge_job(project_id, {
            "status": "done", "ops_applied": 0, "ops_failed": [],
            "progress": "no speech found to caption",
            "finished_at": _now_iso(),
        })
        return
    await _merge_job(project_id, {"status": "applying", "progress": f"writing {len(segs)} captions…"})
    color, font = await _brand_caption_style()
    height = int(((timeline.get("project") or {}).get("height")) or 1920)
    caption_y = round(0.34 * height)  # lower third (title-safe)
    ops: list[dict] = []
    for s in segs:
        dur = max(18, int(s["end_f"]) - int(s["start_f"]))
        ops.append({
            "op": "add_text_popup",
            "from": int(s["start_f"]),
            "durationInFrames": dur,
            "text": s["text"],
            "y": caption_y,
            "anim": "fade",
            "color": color,
            "fontFamily": font,
            "fontSize": 54,
        })
    plan_id = f"captions{project_id}"
    result = await apply_plan(project_id, plan_id=plan_id, ops=ops)
    await _merge_job(project_id, {
        "status": "done", "plan_id": plan_id,
        "ops_applied": int(result.get("applied", 0)),
        "ops_failed": list(result.get("failed") or []),
        "rev_after": int(result.get("rev", 0)),
        "progress": f"added {result.get('applied', 0)} captions",
        "finished_at": _now_iso(),
    })
    log.info("[yas-ve-ai] project #%d captions: applied=%d", project_id, result.get("applied", 0))


def _silence_trim_plan(timeline: dict, analysis: dict, clip_id: Optional[str]) -> tuple[list[dict], str]:
    """Build trim_clip + repack ops that cut dead air from the START and END of
    each video clip on the PRIMARY video track (where speech bounds it), then
    pack the track contiguously. Returns (ops, note). Internal silences are left
    alone (cutting mid-clip needs splits — out of scope for this quick pass)."""
    clips = timeline.get("clips") or []
    sources = (analysis or {}).get("sources") or {}
    PAD = 6  # 0.2s breathing room around speech

    # Target the lowest-index real video track (never the AI overlay lane).
    vid_tracks = sorted(
        (t for t in (timeline.get("tracks") or [])
         if t.get("kind") == "video" and t.get("id") != ai_ops.AI_VIDEO_TRACK_ID),
        key=lambda t: int(t.get("index") or 0),
    )
    if not vid_tracks:
        return [], "no video track to tighten"
    track_id = vid_tracks[0]["id"]
    track_clips = sorted(
        (c for c in clips if c.get("trackId") == track_id and (c.get("kind") or "video") == "video"),
        key=lambda c: int(c.get("from") or 0),
    )
    if clip_id:
        track_clips = [c for c in track_clips if str(c.get("id")) == str(clip_id)]
    if not track_clips:
        return [], "no clip to tighten"

    other_lanes = any(
        (c.get("trackId") != track_id) and (c.get("kind") or "video") != "text"
        and (c.get("source") or {}).get("audioFile") is None
        and c.get("trackId") not in (ai_ops.AI_VIDEO_TRACK_ID,)
        for c in clips
    )

    ops: list[dict] = []
    # New duration per clip after trimming (for the contiguous repack).
    new_dims: list[tuple[dict, int, int]] = []  # (clip, new_tb, new_ta)
    for clip in track_clips:
        kind, sid = ve._clip_source_kind_id(clip)
        tb = int(clip.get("trimBefore") or 0)
        ta = int(clip.get("trimAfter") or (tb + int(clip.get("durationInFrames") or 0)))
        segs = []
        if kind and sid is not None:
            src_an = sources.get(f"{kind}:{int(sid)}") or {}
            segs = ((src_an.get("transcript") or {}).get("segments") or [])
        in_clip = []
        for seg in segs:
            try:
                s = int(seg.get("start_f", 0)); e = int(seg.get("end_f", 0))
            except (TypeError, ValueError):
                continue
            if e > max(s, tb) and s < ta:
                in_clip.append((max(s, tb), min(e, ta)))
        if not in_clip:
            new_dims.append((clip, tb, ta))  # no speech → leave as-is
            continue
        first_speech = min(s for s, _ in in_clip)
        last_speech = max(e for _, e in in_clip)
        new_tb = max(tb, first_speech - PAD)
        new_ta = min(ta, last_speech + PAD)
        if new_ta - new_tb < FPS // 2:  # never trim below ~0.5s
            new_dims.append((clip, tb, ta))
            continue
        if new_tb > tb + 2 or new_ta < ta - 2:
            ops.append({"op": "trim_clip", "clip_id": str(clip.get("id")),
                        "trimBefore": new_tb, "trimAfter": new_ta})
        new_dims.append((clip, new_tb, new_ta))

    if not ops:
        return [], "no dead air found to trim"

    # Repack the track contiguously from its first clip's current start.
    cursor = int(track_clips[0].get("from") or 0)
    for clip, new_tb, new_ta in new_dims:
        ops.append({"op": "set_clip_position", "clip_id": str(clip.get("id")), "from": cursor})
        cursor += (new_ta - new_tb)

    note = ""
    if other_lanes and not clip_id:
        note = " (other tracks left in place — check sync)"
    return ops, note


async def _run_silence(project_id: int, params: dict, analysis: dict) -> None:
    """Trim leading/trailing dead air from video clips and repack the track."""
    row = await ve._fetch_row(project_id)
    timeline = ve._video_edit(row["metadata"])
    clip_id = (params or {}).get("clip_id")
    ops, note = _silence_trim_plan(timeline, analysis, clip_id)
    if not ops:
        await _merge_job(project_id, {
            "status": "done", "ops_applied": 0, "ops_failed": [],
            "progress": note or "nothing to tighten",
            "finished_at": _now_iso(),
        })
        return
    await _merge_job(project_id, {"status": "applying", "progress": "cutting the silences…"})
    plan_id = secrets.token_hex(4)
    result = await apply_plan(project_id, plan_id=plan_id, ops=ops)
    await _merge_job(project_id, {
        "status": "done", "plan_id": plan_id,
        "ops_applied": int(result.get("applied", 0)),
        "ops_failed": list(result.get("failed") or []),
        "rev_after": int(result.get("rev", 0)),
        "progress": "tightened the silent gaps" + note,
        "finished_at": _now_iso(),
    })
    log.info("[yas-ve-ai] project #%d silence: applied=%d", project_id, result.get("applied", 0))


async def _weave_overlay_sfx(project_id: int, accepted: list[dict]) -> None:
    """Best-effort: for each accepted overlay op (title card / caption /
    sticker) in the edit's Phase B, append a matching add_sfx op at the same
    start frame so popups have sound. Varied (avoid-set) and capped. Never
    raises — a failure just means fewer/no SFX."""
    cat_by_op = {
        "add_framed_popup": "whoosh",
        "add_text_popup": "ding",
        "add_sticker": "pop",
    }
    overlays = [op for op in (accepted or []) if op.get("op") in cat_by_op]
    if not overlays:
        return
    avoid: set[str] = set()
    new_ops: list[dict] = []
    for op in overlays[:5]:  # cap SFX per edit to keep it tasteful
        try:
            sfx = await yas_sfx.resolve_sfx(
                category=cat_by_op[op["op"]], duration_sec=0.9, avoid=avoid,
            )
        except Exception:  # noqa: BLE001 — best-effort, never fail the edit
            log.exception("[yas-ve-ai] project #%d: overlay sfx resolve failed", project_id)
            sfx = None
        if not sfx:
            continue
        avoid.add(str(sfx.get("key")))
        new_ops.append({
            "op": "add_sfx",
            "from": int(op.get("from") or 0),
            "durationInFrames": int(sfx.get("durationInFrames") or 24),
            "source_upload_id": int(sfx["source_upload_id"]),
        })
    accepted.extend(new_ops)
    if new_ops:
        log.info("[yas-ve-ai] project #%d: wove %d overlay sfx", project_id, len(new_ops))


# ---------------------------------------------------------------------------
# Feature: bookend (AI intro/outro — TH v2 card→Veo pipeline, async)
# ---------------------------------------------------------------------------

async def _run_bookend(project_id: int, params: dict, analysis: dict) -> None:
    """Bookend job body (called from _run; exceptions land the job in
    status=failed there). Per requested role not satisfied by reuse:
    derive texts → kickoff card+Veo SEQUENTIALLY (intro then outro), then PARK
    at waiting_provider with provider="veo" — workers/yas_video_edit_ai_poller
    places the clips once every animation is ready. When ALL roles were
    satisfied by reuse, placement is applied inline (fast path, no Veo wait)."""
    role_param = str(params.get("role") or "both").strip().lower()
    if role_param not in ("intro", "outro", "both"):
        raise RuntimeError(f"bookend role must be intro, outro or both (got {role_param!r})")
    reuse = params.get("reuse")
    reuse = True if reuse is None else bool(reuse)
    roles = ["intro", "outro"] if role_param == "both" else [role_param]

    row = await ve._fetch_row(project_id)
    timeline = ve._video_edit(row["metadata"])
    if not (timeline.get("clips") or []):
        raise RuntimeError("the timeline has no clips — add media before adding an intro/outro")
    aspect = ((timeline.get("project") or {}).get("aspect")) or "9:16"
    plan_id = secrets.token_hex(4)

    job_bookends, kicked_any = await _ensure_bookends(
        project_id, roles, reuse, timeline, analysis, aspect,
    )

    if not kicked_any:
        # Fast path: every requested role already had a ready bookend.
        await _merge_job(project_id, {
            "status": "applying", "plan_id": plan_id,
            "progress": "placing intro/outro…",
        })
        ops = bookends.build_placement_ops(plan_id, job_bookends, timeline)
        result = await apply_plan(project_id, plan_id=plan_id, ops=ops)
        failed = list(result.get("failed") or [])
        await _merge_job(project_id, {
            "status": "failed" if failed and not result.get("applied") else "done",
            "error": (failed[0].get("error") if failed and not result.get("applied") else None),
            "progress": f"placed {result.get('applied', 0)} clip(s) (reused bookends)",
            "ops_applied": int(result.get("applied", 0)),
            "ops_failed": failed,
            "rev_after": int(result.get("rev", 0)),
            "finished_at": _now_iso(),
        })
        log.info("[yas-ve-ai] project #%d: bookend fast path applied=%d rev=%s",
                 project_id, result.get("applied", 0), result.get("rev"))
        return

    # PARK — the AI poller watches the yas_animations rows and applies the
    # placement ops once every bookend is ready.
    await _merge_job(project_id, {
        "status": "waiting_provider",
        "provider": "veo",
        "plan_id": plan_id,
        "bookends": job_bookends,
        "progress": "rendering intro/outro with Veo — usually 1-4 min…",
    })
    log.info("[yas-ve-ai] project #%d: bookend parked at waiting_provider (%s)",
             project_id, [b["role"] for b in job_bookends])


async def _ensure_bookends(
    project_id: int, roles: list[str], reuse: bool,
    timeline: dict, analysis: dict, aspect: str,
    topic_override: Optional[str] = None,
    topics_by_role: Optional[dict] = None,
) -> tuple[list[dict], bool]:
    """Reuse-or-kickoff one bookend per role (shared by feature='bookend' and
    compose-with-bookends). Returns (job_bookends, kicked_any) where
    job_bookends items are {"role", "animation_id", "duration_sec"} and
    kicked_any says whether ANY Veo render is now in flight (→ the caller must
    PARK at waiting_provider instead of placing inline).

    `topics_by_role` (e.g. {"intro": ..., "outro": ...}) gives each role its OWN
    topic; it wins over the single `topic_override` per role when set."""
    job_bookends: list[dict] = []
    kicked_any = False
    try:
        for role in roles:
            if reuse:
                hit = await bookends.find_ready_bookend(project_id, role)
                if hit:
                    log.info("[yas-ve-ai] project #%d: reusing ready %s bookend (anim #%d)",
                             project_id, role, hit["animation_id"])
                    job_bookends.append({
                        "role": role,
                        "animation_id": hit["animation_id"],
                        "duration_sec": hit["duration_sec"],
                    })
                    continue
            await _merge_job(project_id, {
                "status": "generating_card",
                "progress": f"designing the {role} card…",
            })
            per_role_topic = (topics_by_role or {}).get(role) or topic_override
            texts = await bookends.derive_bookend_texts(
                timeline, analysis, role, topic_override=per_role_topic,
            )
            await _merge_job(project_id, {
                "progress": f"rendering the {role} card + starting Veo…",
            })
            kicked = await bookends.kickoff_bookend(project_id, role, texts, aspect)
            kicked_any = True
            job_bookends.append({
                "role": role,
                "animation_id": kicked["animation_id"],
                "duration_sec": kicked["duration_sec"],
            })
    except VertexBudgetExceededError as e:
        # Already-inserted animations survive for `reuse` on the next attempt.
        raise RuntimeError(
            "Vertex budget exhausted — raise VEO_BUDGET_USD or wait for reset "
            f"({e})"
        ) from e
    return job_bookends, kicked_any


async def _bookend_roles_on_timeline(project_id: int, timeline: dict) -> set[str]:
    """Bookend roles (intro/outro) already PLACED on the timeline — i.e. some
    clip's animation source is one of this project's bookend animations.
    Compose-with-bookends skips these roles (no duplicate intro/outro)."""
    anim_ids: set[int] = set()
    for clip in (timeline or {}).get("clips") or []:
        kind, sid = ve._clip_source_kind_id(clip)
        if kind == "animation" and sid is not None:
            anim_ids.add(int(sid))
    if not anim_ids:
        return set()
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT DISTINCT metadata->>'role' AS role
                  FROM yas_animations
                 WHERE id = ANY(%s)
                   AND yas_generation_id = %s
                   AND metadata->>'role' IN ('intro', 'outro')
                """,
                (sorted(anim_ids), project_id),
            )
            return {str(r["role"]) for r in await cur.fetchall()}


async def _compose_bookends(
    project_id: int, params: dict, analysis: dict,
    compose_result: dict, compose_failed: list[dict],
) -> None:
    """Compose with generate_bookends=true, AFTER the compose plan applied:
    ensure an intro and an outro frame the just-composed main clips.

    Per role: already on the timeline → skip; a ready render exists →
    collect for placement; otherwise kick off generation (Phase 3 pipeline).
    If anything was kicked, PARK at waiting_provider/provider='veo' — the AI
    poller applies the deterministic placement plan when Veo finishes. If all
    needed bookends already existed, place them inline and finish 'done'.
    The placement uses its OWN plan id, separate from the compose plan (both
    idempotent)."""
    reuse = params.get("reuse")
    reuse = True if reuse is None else bool(reuse)

    # Fresh, post-compose timeline — placement must frame the composed order.
    row = await ve._fetch_row(project_id)
    timeline = ve._video_edit(row["metadata"])
    aspect = ((timeline.get("project") or {}).get("aspect")) or "9:16"

    base = {
        "ops_applied": int(compose_result.get("applied", 0)),
        "ops_failed": list(compose_failed or []),
        "rev_after": int(compose_result.get("rev", 0)),
    }
    placed = await _bookend_roles_on_timeline(project_id, timeline)
    roles = [r for r in bookends.ROLES if r not in placed]
    if not roles:
        await _merge_job(project_id, {
            "status": "done",
            "progress": f"composed {compose_result.get('applied', 0)} op(s) — "
                        "intro/outro already on the timeline",
            **base,
            "finished_at": _now_iso(),
        })
        log.info("[yas-ve-ai] project #%d: compose done — bookends already placed",
                 project_id)
        _store_edit_recipe_bg(project_id, analysis)  # recipe RAG capture
        return

    plan_id = secrets.token_hex(4)  # placement plan — NOT the compose plan
    job_bookends, kicked_any = await _ensure_bookends(
        project_id, roles, reuse, timeline, analysis, aspect,
    )

    if not kicked_any:
        # Fast path: every missing role had a ready render — place inline.
        await _merge_job(project_id, {
            "status": "applying", "plan_id": plan_id,
            "progress": "placing intro/outro…",
        })
        ops = bookends.build_placement_ops(plan_id, job_bookends, timeline)
        result = await apply_plan(project_id, plan_id=plan_id, ops=ops)
        failed = list(result.get("failed") or [])
        await _merge_job(project_id, {
            "status": "failed" if failed and not result.get("applied") else "done",
            "error": (failed[0].get("error") if failed and not result.get("applied") else None),
            "progress": f"composed {compose_result.get('applied', 0)} op(s) + "
                        f"placed {result.get('applied', 0)} clip(s) (reused bookends)",
            "ops_applied": int(compose_result.get("applied", 0)) + int(result.get("applied", 0)),
            "ops_failed": list(compose_failed or []) + failed,
            "rev_after": int(result.get("rev", 0)),
            "finished_at": _now_iso(),
        })
        log.info("[yas-ve-ai] project #%d: compose+bookend fast path applied=%d rev=%s",
                 project_id, result.get("applied", 0), result.get("rev"))
        _store_edit_recipe_bg(project_id, analysis)  # recipe RAG capture
        return

    # PARK — the AI poller places the bookends around the composed main clips
    # (deterministic build_placement_ops) once every Veo render is ready.
    await _merge_job(project_id, {
        "status": "waiting_provider",
        "provider": "veo",
        "plan_id": plan_id,
        "bookends": job_bookends,
        **base,
        "progress": "composed — rendering intro/outro with Veo, usually 1-4 min…",
    })
    log.info("[yas-ve-ai] project #%d: compose parked at waiting_provider (%s)",
             project_id, [b["role"] for b in job_bookends])


# ---------------------------------------------------------------------------
# Feature: broll (opt-in B-roll preset — fully deterministic, no LLM ops)
# ---------------------------------------------------------------------------

async def _run_broll(project_id: int, params: dict, analysis: dict) -> None:
    """One-click B-roll auto-edit (called from _run; exceptions land the job
    in status=failed there). Three deterministic moves under ONE main plan:

      1. ORDER  — CLIP edge-frame embeddings → greedy best-flow chain →
                  reorder_clips + fade joins (skips gracefully without CLIP).
      2. CARDS  — intro/outro: 'light' = add_title_card text cards (executor-
                  only op), 'veo' = the shared bookend pipeline (ready renders
                  place inline under PLAN_BK; kicked renders park the job).
      3. MUSIC  — _resolve_music: asset/upload results land inline in the main
                  plan; a pending Suno track parks the job.

    Anything parked is finished by workers/yas_video_edit_ai_poller._check_broll
    (provider="broll": pending_music and/or bookends halves)."""
    intro_outro = bool(params.get("intro_outro", True))
    bookend_mode = str(params.get("bookend_mode") or "generate").strip().lower()
    want_music = bool(params.get("music", True))
    title = (params.get("title") or "").strip() or None  # legacy single override

    def _int_or_none(v: Any) -> Optional[int]:
        try:
            return int(v) if v is not None else None
        except (TypeError, ValueError):
            return None

    # Per-role topics for generated bookends (fall back to the legacy title).
    intro_topic = (params.get("intro_topic") or "").strip() or title
    outro_topic = (params.get("outro_topic") or "").strip() or title
    # 'upload' mode: the user's own intro/outro clips, pinned first/last.
    intro_upload_id = _int_or_none(params.get("intro_upload_id"))
    outro_upload_id = _int_or_none(params.get("outro_upload_id"))
    use_uploaded_bookends = (
        bookend_mode == "upload" and (intro_upload_id is not None or outro_upload_id is not None)
    )

    row = await ve._fetch_row(project_id)
    timeline = ve._video_edit(row["metadata"])
    track, vclips = broll.primary_video_clips(timeline)
    if not vclips:
        raise RuntimeError("the timeline has no video clips — add media before running B-roll")
    aspect = ((timeline.get("project") or {}).get("aspect")) or "9:16"

    plan_main = broll.PLAN_MAIN(project_id)
    ops: list[dict] = []

    # ---- 1. ORDER (visual-flow chain) -----------------------------------
    await _merge_job(project_id, {
        "status": "planning", "plan_id": plan_main,
        "progress": "sequencing the B-roll for flow…",
    })
    emb = await broll.extract_edge_embeddings(project_id, vclips)
    if use_uploaded_bookends:
        # Pin the user's own intro/outro clips first/last; flow-order the middle.
        ordered = broll.pin_and_order(vclips, emb, intro_upload_id, outro_upload_id)
    else:
        order = broll.best_order(vclips, emb)
        by_id = {str(c.get("id")): c for c in vclips}
        ordered = [by_id[cid] for cid in order if cid in by_id]
    ops.extend(broll.build_flow_ops(track["id"], ordered))

    # ---- 2. INTRO/OUTRO ---------------------------------------------------
    # 'upload' mode already bookends the montage with the pinned clips above —
    # nothing to generate. Otherwise generate Veo bookends with per-role topics.
    job_bookends: list[dict] = []
    kicked_any = False
    if intro_outro and not use_uploaded_bookends:
        job_bookends, kicked_any = await _ensure_bookends(
            project_id, list(bookends.ROLES), True, timeline, analysis, aspect,
            topics_by_role={"intro": intro_topic, "outro": outro_topic},
        )

    # ---- 3. MUSIC -----------------------------------------------------------
    pending_music: Optional[dict] = None
    if want_music:
        kind, result, base_op = await _resolve_music(
            project_id, timeline, analysis, None, params.get("music_mode"),
        )
        if kind == "pending":
            pending_music = {
                "provider_job_id": str(result.get("provider_job_id")),
                "pending_op": base_op,
                "plan_id": broll.PLAN_MUSIC(project_id),
                "applied": False,
            }
        elif kind == "asset":
            base_op["source_asset_id"] = int(result["asset_id"])
            ops.append(base_op)
        elif kind == "upload":
            base_op["source_upload_id"] = int(result["upload_id"])
            ops.append(base_op)
        else:
            raise RuntimeError(f"music provider returned an unknown result kind: {kind!r}")

    # ---- APPLY (deterministic ops — no validate_and_filter_ops, like sfx) --
    await _merge_job(project_id, {
        "status": "applying", "progress": f"applying {len(ops)} op(s)…",
    })
    result_main = await apply_plan(project_id, plan_id=plan_main, ops=ops)
    ops_applied = int(result_main.get("applied", 0))
    ops_failed = list(result_main.get("failed") or [])
    rev_after = int(result_main.get("rev", 0))

    # Veo fast path: every role already had a READY render — place inline
    # under the bookend plan, on the FRESH (post-main-plan) timeline.
    plan_bk = broll.PLAN_BK(project_id)
    if job_bookends and not kicked_any:
        fresh = await ve._fetch_row(project_id)
        fresh_tl = ve._video_edit(fresh["metadata"]) or {}
        bops = bookends.build_placement_ops(plan_bk, job_bookends, fresh_tl)
        result_bk = await apply_plan(project_id, plan_id=plan_bk, ops=bops)
        ops_applied += int(result_bk.get("applied", 0))
        ops_failed += list(result_bk.get("failed") or [])
        rev_after = int(result_bk.get("rev", 0))

    if pending_music or kicked_any:
        waiting_on = (
            (["music (Suno)"] if pending_music else [])
            + (["intro/outro (Veo)"] if kicked_any else [])
        )
        fields: dict[str, Any] = {
            "status": "waiting_provider",
            "provider": "broll",
            "plan_id": plan_main,
            "ops_applied": ops_applied,
            "ops_failed": ops_failed,
            "rev_after": rev_after,
            "progress": "B-roll laid out — waiting for " + " + ".join(waiting_on) + "…",
        }
        if pending_music:
            fields["pending_music"] = pending_music
        if kicked_any:
            fields["bookends"] = job_bookends
            fields["bookends_plan_id"] = plan_bk
            fields["bookends_applied"] = False
        await _merge_job(project_id, fields)
        log.info("[yas-ve-ai] project #%d: broll parked at waiting_provider (%s)",
                 project_id, " + ".join(waiting_on))
        return

    await _merge_job(project_id, {
        "status": "done",
        "plan_id": plan_main,
        "ops_applied": ops_applied,
        "ops_failed": ops_failed,
        "rev_after": rev_after,
        "progress": f"B-roll edit applied ({ops_applied} op(s))"
                    + (f", {len(ops_failed)} failed" if ops_failed else ""),
        "finished_at": _now_iso(),
    })
    log.info("[yas-ve-ai] project #%d broll: done — applied=%d failed=%d rev=%s",
             project_id, ops_applied, len(ops_failed), rev_after)


# ---------------------------------------------------------------------------
# Feature: voice (deterministic — no LLM)
# ---------------------------------------------------------------------------

async def _voice_op(project_id: int, timeline: dict, params: dict) -> dict:
    """Resolve feature=voice into ONE fully-resolved replace_voice op.

    Two paths:
      * use_as_is — the user pre-uploaded the replacement audio
        (POST /video-editor/assets/audio/upload → yas_uploads id in
        params.source_upload_id): no extraction, no ElevenLabs — emit the op
        directly and the executor lays it on the voice lane + mutes the clip.
      * default   — ffmpeg-extract the clip's trimmed audio window →
        ElevenLabs STS (stock voice, explicit voice_id, or the app-wide clone
        injected by start_job when use_cloned) → import_audio → op.
    """
    clip_id = str(params.get("clip_id") or "").strip()
    clip = next(
        (c for c in (timeline.get("clips") or []) if str(c.get("id")) == clip_id),
        None,
    )
    if clip is None:
        raise RuntimeError(f"clip not found on the timeline: {clip_id!r}")

    tb, te, _ = _clip_window(clip)
    dur_f = max(1, te - tb)

    # USE-AS-IS: replacement audio already lives in yas_uploads — skip
    # extract → STS → import entirely.
    if bool(params.get("use_as_is")) and params.get("source_upload_id"):
        upload_id = int(params["source_upload_id"])
        if not await ve.resolve_audio_serving(upload_id):
            raise RuntimeError(
                f"use-as-is audio upload #{upload_id} is unavailable — re-upload it"
            )
        return {
            "op": "replace_voice",
            "clip_id": clip_id,
            "source_upload_id": upload_id,
            "from": int(clip.get("from", 0) or 0),
            "durationInFrames": dur_f,
        }

    kind, sid = ve._clip_source_kind_id(clip)
    if not kind or sid is None:
        raise RuntimeError(f"clip {clip_id} has no media source to take audio from")
    local = await ve.resolve_source_local(kind, int(sid))
    if not local or not local.exists():
        raise RuntimeError(f"clip {clip_id}: source media file unavailable ({kind} #{sid})")

    work_dir = ve.VE_ROOT / "ai" / "voice"
    work_dir.mkdir(parents=True, exist_ok=True)
    extracted = work_dir / f"p{project_id}_{secrets.token_hex(4)}.mp3"

    await _merge_job(project_id, {"progress": "extracting clip audio…"})
    await asyncio.to_thread(
        _extract_clip_audio_sync, local, extracted, tb / FPS, dur_f / FPS,
    )

    await _merge_job(project_id, {"progress": "converting voice (ElevenLabs)…"})
    voice = get_voice_provider()
    try:
        converted = await voice.speech_to_speech(
            extracted, voice_id=(params.get("voice_id") or None), out_dir=work_dir,
        )
    except ProviderError as e:
        # The stored clone can vanish out from under us (deleted in the
        # ElevenLabs dashboard / another tool) — forget it so the next attempt
        # starts clean instead of failing forever.
        msg = str(e).lower()
        if bool(params.get("use_cloned")) and (
            "voice_not_found" in msg or ("voice" in msg and "not found" in msg)
        ):
            await voice_clone.clear_cloned_voice()
            raise RuntimeError(
                "Your cloned voice no longer exists on ElevenLabs — re-upload "
                "your voice sample (Clone my voice) and try again."
            ) from e
        raise

    imported = await ve.import_audio(
        raw=converted.read_bytes(), filename=converted.name, mime="audio/mpeg",
    )
    return {
        "op": "replace_voice",
        "clip_id": clip_id,
        "source_upload_id": int(imported["id"]),
        "from": int(clip.get("from", 0) or 0),
        "durationInFrames": dur_f,
    }


# ---------------------------------------------------------------------------
# Feature: music (provider resolution; Suno parks at waiting_provider)
# ---------------------------------------------------------------------------

async def _resolve_music(
    project_id: int, timeline: dict, analysis: dict,
    query: Optional[str], mode: Optional[str],
) -> tuple[str, dict, dict]:
    """Build the style brief + duration, call the configured music provider,
    and pre-build the (source-less) add_music op. Returns (kind, result,
    base_op) — kind is the provider result kind ("pending" | "asset" |
    "upload"); the CALLER stamps the source / parks the job (shared by
    _music_op and _run_broll)."""
    brief = _style_brief(analysis, query)
    duration_sec = _project_duration_f(timeline) / FPS
    provider = get_music_provider(str(mode or "auto"))

    await _merge_job(project_id, {
        "progress": f"sourcing music via {provider.name} — {brief[:120]}",
    })
    result = await provider.make_music(style_brief=brief, duration_sec=duration_sec)

    base_op: dict[str, Any] = {
        "op": "add_music",
        "from": 0,
        "duck": True,
        "speech_segments": _speech_segments_timeline(timeline, analysis),
        "style_or_query": brief,
    }
    return result.get("kind"), result, base_op


async def _music_op(
    project_id: int, timeline: dict, analysis: dict, params: dict, plan_id: str,
) -> Optional[dict]:
    """Resolve a music bed via the configured provider. Returns the concrete
    add_music op, or None after parking the job at waiting_provider (Suno —
    workers/yas_video_edit_ai_poller.py finishes it)."""
    kind, result, base_op = await _resolve_music(
        project_id, timeline, analysis, params.get("query"), params.get("mode"),
    )

    if kind == "pending":
        await _merge_job(project_id, {
            "status": "waiting_provider",
            "plan_id": plan_id,
            "provider_job_id": str(result.get("provider_job_id")),
            "pending_op": base_op,
            "progress": "music generating (Suno) — waiting for the track…",
        })
        log.info("[yas-ve-ai] project #%d: music parked at waiting_provider (suno job %s)",
                 project_id, result.get("provider_job_id"))
        return None
    if kind == "asset":
        base_op["source_asset_id"] = int(result["asset_id"])
        return base_op
    if kind == "upload":
        base_op["source_upload_id"] = int(result["upload_id"])
        return base_op
    raise RuntimeError(f"music provider returned an unknown result kind: {kind!r}")


# ---------------------------------------------------------------------------
# Features: popups / compose / edit (the planner LLM)
# ---------------------------------------------------------------------------

def _json_block(label: str, obj: Any) -> str:
    return f"{label}:\n{json.dumps(obj, ensure_ascii=False, default=str)}\n"


async def _past_recipe_block(project_id: int, timeline: dict, digest: dict) -> str:
    """Edit-recipe RAG retrieval: build a query from the CURRENT video's
    content (title + transcript excerpt + scene summaries — all already in the
    digest), fetch ≤3 nearest past recipes (excluding this project's own), and
    format them as a cadence-only prompt section. Best-effort: '' on any
    failure, and '' when nothing has been stored yet."""
    try:
        bits: list[str] = []
        title = str(((timeline or {}).get("project") or {}).get("title") or "").strip()
        if title:
            bits.append(title)
        excerpt = _digest_transcript_excerpt(digest, 400)
        if excerpt:
            bits.append(excerpt)
        summaries: list[str] = []
        for row in (digest or {}).get("clips") or []:
            for sc in row.get("scenes") or []:
                s = str(sc.get("summary") or "").strip()
                if s:
                    summaries.append(s)
            if len(summaries) >= 6:
                break
        if summaries:
            bits.append(" — ".join(summaries[:6]))
        query = "  ".join(bits)[:1500]
        if not query:
            return ""
        recipes = await yas_edit_recipes.nearest_recipes(
            query, k=3, exclude_generation_id=project_id,
        )
        if not recipes:
            return ""
        return yas_edit_recipes.format_recipe_block(recipes)
    except Exception:  # noqa: BLE001 — style RAG is optional, the plan degrades
        log.exception("[yas-ve-ai] project #%d: past-recipe retrieval failed (non-fatal)",
                      project_id)
        return ""


async def _plan_with_llm(
    project_id: int, feature: str, timeline: dict, analysis: dict,
    digest: dict, params: dict, *, phase: Optional[str] = None,
) -> list[dict]:
    """One schema-constrained Gemini call → {summary, ops}. For feature='edit'
    `phase` selects the staged-brief scope: 'structure' (Phase A) or
    'overlays' (Phase B) — each gets its own system + prompt sections."""
    clip_ids = [str(c.get("id")) for c in (timeline.get("clips") or []) if c.get("id")]
    track_rows = digest.get("tracks") or []

    sections: list[str] = [_json_block("PROJECT DIGEST (all times are 30fps frames)", digest)]
    sections.append(_json_block("LEGAL clip_id values (the ONLY ids you may reference)", clip_ids))
    sections.append(_json_block("LEGAL track_id values", [t.get("id") for t in track_rows]))

    if feature == "popups":
        candidates = candidate_popup_windows(timeline, analysis)
        if not candidates:
            # candidate_popup_windows now falls back to timeline geometry, so the
            # only way this is empty is a timeline with no video clips to host a
            # popup at all.
            raise RuntimeError(
                "this video has no clips to place popups on — add a clip first"
            )
        sections.append(_json_block(
            "CANDIDATE POPUP WINDOWS — pick from these ONLY (copy "
            "from/durationInFrames/x/y verbatim; you may shorten a duration)",
            candidates,
        ))
        fonts = await _font_assets()
        if fonts:
            sections.append(_json_block("LEGAL font_asset_id values (brand fonts)", fonts))
        frames = await _frame_assets()
        if frames:
            sections.append(_json_block(
                "TEXTBOX FRAME candidates (frame_asset_id values for add_framed_popup)",
                frames,
            ))
            sections.append(
                "PREFER op add_framed_popup pairing one frame_asset_id with your "
                "text + a font_asset_id (the brand font); use plain "
                "add_text_popup only when no frame suits."
            )
        text = str(params.get("text") or "").strip()
        hint = str(params.get("hint") or "").strip()
        ask = "Add tasteful text popups to this video."
        if text:
            ask += f"\nThe user wants popups about / saying: {text}"
        if hint:
            ask += f"\nPlacement/style hint from the user: {hint}"
        sections.append(ask)

    elif feature == "compose":
        keys = params.get("keys")
        if isinstance(keys, list) and keys:
            sections.append(_json_block("USER-PRIORITIZED clips/sources (in intent order)", keys))
        style = str(params.get("style") or "").strip()
        ask = (
            "Sequence this timeline for the best narrative flow and join the "
            "cuts with tasteful transitions where they help."
        )
        if style:
            ask += f"\nUser style direction: {style}"
        sections.append(ask)

    elif feature == "edit" and phase == "structure":
        # Phase A — structure only (compose-style language).
        instruction = str(params.get("instruction") or "").strip()
        ask = (
            "Restructure this timeline now (STAGE 1 + STAGE 2): reorder the "
            "clips for a hook-first narrative, trim dead air, then join the "
            "cuts with tasteful transitions."
        )
        if instruction:
            ask += f"\nUser direction (weave it into the full edit): {instruction}"
        else:
            ask += ("\nNo user instruction — you decide everything; deliver "
                    "the complete structure pass.")
        sections.append(ask)

    else:  # edit — phase 'overlays': title card + popups + stickers + music
        instruction = str(params.get("instruction") or "").strip()
        excerpt = _digest_transcript_excerpt(digest, 200)
        topic = _opening_line(digest) or excerpt[:120]
        if topic:
            sections.append(f"VIDEO TOPIC: {topic}")
        # Candidate windows: a synthesized intro-card slot FIRST, then the
        # safe-grid/transcript windows computed on the post-structure layout.
        windows = [await _intro_title_window(project_id, timeline, digest)]
        windows += candidate_popup_windows(timeline, analysis)
        sections.append(_json_block(
            "CANDIDATE WINDOWS — the FIRST is the intro title-card slot; pick "
            "all other popups from these ONLY (copy from/durationInFrames/x/y "
            "verbatim; you may shorten a duration, never extend it)",
            windows,
        ))
        fonts = await _font_assets()
        if fonts:
            sections.append(_json_block("LEGAL font_asset_id values (brand fonts)", fonts))
        frames = await _frame_assets()
        if frames:
            sections.append(_json_block(
                "TEXTBOX FRAME candidates (frame_asset_id values for add_framed_popup)",
                frames,
            ))
            sections.append(
                "PREFER op add_framed_popup pairing one frame_asset_id with your "
                "text + a font_asset_id (the brand font); use plain "
                "add_text_popup only when no frame suits."
            )
        # Sticker/music grounding: search on what the video IS (style brief +
        # transcript), never the project title.
        search_q = _style_brief(analysis, instruction or None)
        if excerpt:
            search_q += f" — video about: {excerpt}"
        stickers = await _search_assets(search_q, kind="image", limit=8)
        if stickers:
            sections.append(_json_block(
                "STICKER/IMAGE asset candidates (use these asset_id values for add_sticker)",
                stickers,
            ))
        music = await _search_assets(f"background music: {search_q}", kind="audio", limit=5)
        if music:
            sections.append(_json_block(
                "MUSIC asset candidates (use as add_music source_asset_id)", music,
            ))
        ask = (
            "Add the text layer and the music bed now (STAGE 3 + STAGE 4): "
            "ONE framed intro title card in the intro slot, 1-3 section "
            "popups at scene shifts, and exactly one add_music with duck=true."
        )
        if instruction:
            ask += f"\nUser direction (weave it into the full edit): {instruction}"
        else:
            ask += ("\nNo user instruction — you decide everything; deliver "
                    "the complete overlay + music pass.")
        sections.append(ask)

    # Past-edit STYLE RAG (edit — both phases — and compose): retrieve ≤3
    # nearest past recipes by the CURRENT video's content and inject a
    # cadence-only block right BEFORE the final ask (after the candidate
    # sections) so the current-video digest still dominates. The block itself
    # forbids copying topics/text/timestamps; placements remain structurally
    # anchored to THIS video (candidate windows + validate_and_filter_ops).
    # Best-effort: a failure simply omits the block.
    if feature in ("edit", "compose"):
        recipe_block = await _past_recipe_block(project_id, timeline, digest)
        if recipe_block:
            sections.insert(len(sections) - 1, recipe_block)

    system = _SYSTEMS[feature]
    if feature == "edit":
        system = _EDIT_PHASE_A_SYSTEM if phase == "structure" else _EDIT_PHASE_B_SYSTEM

    prompt = "\n".join(sections)
    # Gemini occasionally returns a malformed / un-schema'd response (seen on
    # popups + compose). Retry a couple of times with a little temperature
    # variation before giving up — most parse failures are transient.
    result: object = None
    for attempt in range(3):
        result = await gemini_multimodal(
            prompt,
            system=system,
            json_schema=EDIT_PLAN_SCHEMA,
            model=_PLANNER_MODEL,
            temperature=0.3 + 0.15 * attempt,
            # The edit feature plans a FULL pass (many ops per phase) — give
            # it headroom; popups/compose stay on the original budget.
            max_tokens=10000 if feature == "edit" else 6000,
            thinking_budget=0,
        )
        if isinstance(result, dict) and "_raw" not in result:
            break
        log.warning("[yas-ve-ai] project #%d %s planner unparseable "
                    "(attempt %d/3) — retrying", project_id, feature, attempt + 1)
    if not isinstance(result, dict) or "_raw" in result:
        raise RuntimeError("the planner returned an unparseable response — try again")
    ops = result.get("ops")
    if not isinstance(ops, list):
        raise RuntimeError("the planner returned no ops array")
    summary = str(result.get("summary") or "").strip()
    if summary:
        await _merge_job(project_id, {"progress": f"plan: {summary[:240]}"})
    log.info("[yas-ve-ai] project #%d %s plan: %d op(s) — %s",
             project_id, feature, len(ops), summary[:160])
    return [op for op in ops if isinstance(op, dict)]


async def _backfill_framed_popup_defaults(
    ops: list[dict], timeline: dict, analysis: dict
) -> None:
    """Backfill the two REQUIRED add_framed_popup fields the planner sometimes
    omits — `frame_asset_id` (default to the first textbox frame) and `from`
    (default to a computed safe-area candidate window, else a sane fallback) —
    so a non-compliant plan still produces a framed popup instead of being
    rejected. Runs before validation. Best-effort: failures leave ops as-is."""
    framed = [
        op for op in ops
        if isinstance(op, dict) and op.get("op") == "add_framed_popup"
    ]
    if not framed:
        return
    try:
        default_frame_id = None
        if any(op.get("frame_asset_id") is None for op in framed):
            frames = await _frame_assets(limit=1)
            if frames:
                default_frame_id = frames[0].get("frame_asset_id")
        windows = (
            candidate_popup_windows(timeline, analysis)
            if any(op.get("from") is None for op in framed) else []
        )
        wi = 0
        for op in framed:
            if op.get("frame_asset_id") is None and default_frame_id is not None:
                op["frame_asset_id"] = default_frame_id
            if op.get("from") is None:
                if wi < len(windows):
                    w = windows[wi]
                    wi += 1
                    op["from"] = w.get("from")
                    op.setdefault("durationInFrames", w.get("durationInFrames"))
                    op.setdefault("x", w.get("x"))
                    op.setdefault("y", w.get("y"))
                else:
                    op["from"] = 30  # last-resort: just after the opening frame
    except Exception:  # noqa: BLE001 — backfill is best-effort
        log.exception("[yas-ve-ai] framed-popup default backfill failed")


async def _enrich_framed_popup_ops(ops: list[dict]) -> None:
    """Stamp every accepted add_framed_popup op with `_frame_aspect`
    (producer_assets width/height) so the executor's deterministic text-fit
    uses the frame's real shape. One query for all frames; NULL/zero dims are
    skipped (the executor falls back to a 3:1 textbox). Best-effort: any
    failure leaves the ops un-stamped rather than failing the plan."""
    frame_ids = sorted({
        int(op["frame_asset_id"])
        for op in ops
        if isinstance(op, dict)
        and op.get("op") == "add_framed_popup"
        and op.get("frame_asset_id") is not None
    })
    if not frame_ids:
        return
    brand_font_id: int | None = None
    try:
        async with get_conn() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT id, width, height FROM producer_assets WHERE id = ANY(%s)",
                    (frame_ids,),
                )
                dims = {
                    int(r["id"]): (r.get("width"), r.get("height"))
                    for r in await cur.fetchall()
                }
                # The textbox-frame look is the brand font; default to VG5000
                # when the planner didn't pick a font itself.
                await cur.execute(
                    "SELECT id FROM producer_assets WHERE asset_kind = 'font' "
                    "AND enabled AND lower(coalesce(display_name, '')) = 'vg5000' "
                    "ORDER BY created_at LIMIT 1"
                )
                frow = await cur.fetchone()
                if frow:
                    brand_font_id = int(frow["id"])
    except Exception:  # noqa: BLE001 — enrichment is optional, the fit degrades
        log.exception("[yas-ve-ai] framed-popup frame-aspect lookup failed")
        return
    for op in ops:
        if not (isinstance(op, dict) and op.get("op") == "add_framed_popup"):
            continue
        if op.get("frame_asset_id") is None:
            continue
        w, h = dims.get(int(op["frame_asset_id"]), (None, None))
        if w and h:
            op["_frame_aspect"] = float(w) / float(h)
        # Brand font default: stamp VG5000 (id + family) unless the planner
        # already chose a font, so framed popups read in the brand typeface.
        if brand_font_id is not None and not op.get("font_asset_id"):
            op["font_asset_id"] = brand_font_id
            op.setdefault("fontFamily", "VG5000")


def _force_user_popup_text(ops: list, params: dict) -> None:
    """Backfill the user's popup text into any add_text_popup / add_framed_popup
    op the planner left blank. Gemini has returned empty `text` in the wild, and
    the op validator drops blank-text popups — which would silently lose the
    user's caption. Only fills blanks; a real LLM-written caption is left
    untouched."""
    user_text = str(params.get("text") or "").strip()
    if not user_text:
        return
    for op in ops:
        if not (isinstance(op, dict) and op.get("op") in ("add_text_popup", "add_framed_popup")):
            continue
        text = op.get("text")
        if not (isinstance(text, str) and text.strip()):
            op["text"] = user_text
