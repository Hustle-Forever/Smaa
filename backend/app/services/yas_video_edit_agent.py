"""Agentic video editor — the model looks, thinks, acts, then checks its work.

The engine this replaces was a blind one-shot JSON emitter: a no-thinking Flash
model was handed a JSON digest of the timeline and asked to output a whole edit
in a single call, having never seen a frame of the footage. Everything it got
wrong was patched up afterwards by a large deterministic repair layer. That is
why the output felt templated — the LLM was really only picking words.

This module makes the editor an actual agent:

  * It can LOOK. `look()` returns REAL JPEG frames (pulled from the analysis
    cache, or extracted on demand with ffmpeg at any timestamp) as image content.
    `preview()` renders the CURRENT timeline through Remotion so the agent sees
    its own composite — overlays on top of footage — and can fix what it sees.
  * It can DO a lot. Beyond raw ops it can run the deterministic passes the app
    already owns (captions, ducking, silence trim, music), search the asset
    library, and generate a brand-new sticker image on the spot.
  * It ITERATES. Every apply re-grounds the timeline + digest, so the next tool
    call sees the edit that just landed instead of a stale snapshot.
  * It REASONS. The loop runs on whichever brain the deployment has (Claude on
    Vertex, Claude direct, or Gemini) via `model_gateway.reasoner_tool_loop`,
    always with thinking enabled.

Hard-won constraints this module obeys (breaking any one of them corrupts a
project — see the notes on each):

  1. NEVER call `start_job` from in here. There is one job slot per project and
     the guard would 409 against our own running job, killing it.
  2. NEVER call the `_run_*` feature helpers. They each write a terminal
     `status: done` into the shared job blob, which ends the parent job in the
     UI mid-flight and un-pauses the browser's autosave — which then clobbers
     everything the agent does next. We call their PURE cores instead and route
     every mutation through this module's single `_apply_ops`.
  3. Applies are SERIALISED. `apply_plan` -> `save_timeline` is last-write-wins
     with no base_rev check, so two concurrent applies silently lose one. The
     tool loop dispatches sequentially and nothing here uses gather.
  4. Additive work uses a RANDOM plan id. Stable plan ids ("captions{pid}") make
     `apply_plan` sweep the previous clips of that plan first.

Imports stay one-directional: `yas_video_edit_ai` imports THIS module lazily, so
anything needed from it is imported inside a function, never at module scope.
"""
from __future__ import annotations

import asyncio
import json
import logging
import secrets
from pathlib import Path
from typing import Any, Optional

import httpx

from ..config import config
from ..db import get_conn
from . import yas_video_editor as ve
from .model_gateway import reasoner_tool_loop, resolve_reasoner
from .yas_asset_rag import get_popup_examples, search_assets_svc
from .yas_video_edit_ai_analysis import (
    _ai_dir,
    _clip_source_key,
    _clip_window,
    _save_frame_sync,
    build_project_digest,
    candidate_popup_windows,
    get_or_build_analysis,
)
from .yas_video_edit_ai_ops import (
    AI_MEDIA_TRACK_ID,
    AI_VIDEO_TRACK_ID,
    apply_plan,
    drop_empty_text_popups,
    drop_unapplyable_transitions,
    group_popups_by_time,
    validate_and_filter_ops,
)

# Lanes that carry AI overlays rather than footage — `look()` must see through
# them to the actual video underneath.
_OVERLAY_TRACKS = {AI_VIDEO_TRACK_ID}

log = logging.getLogger("archivist.yas_ve_agent")

FPS = ve.FPS  # 30

# A preview still costs a Remotion render; keep the agent from burning the whole
# step budget on screenshots.
_MAX_PREVIEWS_PER_RUN = 6
_MAX_LOOKS_PER_RUN = 12


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

async def _enabled_asset_ids() -> set[int]:
    """Asset ids the planner may reference (enabled producer_assets)."""
    try:
        async with get_conn(autocommit=True) as conn:
            async with conn.cursor() as cur:
                await cur.execute("SELECT id FROM producer_assets WHERE enabled")
                return {int(r["id"]) for r in await cur.fetchall()}
    except Exception:  # noqa: BLE001
        log.exception("[ve-agent] _enabled_asset_ids failed")
        return set()


def _clip_ids(timeline: dict) -> set[str]:
    return {str(c.get("id")) for c in (timeline.get("clips") or []) if c.get("id")}


def _valid_clip_sources(timeline: dict) -> set[tuple[str, int]]:
    """(kind, id) pairs the agent may place with add_media_clip — the media bin."""
    out: set[tuple[str, int]] = set()
    for b in ((timeline.get("project") or {}).get("media_bin") or []):
        if not isinstance(b, dict):
            continue
        kind, bid = str(b.get("kind") or ""), b.get("id")
        if kind and bid is not None:
            try:
                out.add((kind, int(bid)))
            except (TypeError, ValueError):
                pass
    return out


def _f2s(frames: Any) -> float:
    try:
        return round(float(frames) / FPS, 2)
    except (TypeError, ValueError):
        return 0.0


def _s2f(seconds: Any) -> int:
    try:
        return max(0, int(round(float(seconds) * FPS)))
    except (TypeError, ValueError):
        return 0


# ---------------------------------------------------------------------------
# The agent run context — owns the timeline, the digest and every mutation
# ---------------------------------------------------------------------------

class _Run:
    """One agent run against one project. Holds the freshest timeline/digest and
    is the ONLY thing that writes to the project."""

    def __init__(self, project_id: int, analysis: dict) -> None:
        self.project_id = project_id
        self.analysis = analysis or {}
        self.timeline: dict = {}
        self.digest: dict = {}
        self.memory: dict = {}
        self.applied = 0
        self.rev: Any = None
        # Every rejection across the WHOLE run, not just the last apply — the
        # user's "N skipped" hint should count all of them.
        self.all_rejected: list[dict] = []
        self.previews = 0
        self.looks = 0
        self.notes: list[str] = []

    async def reground(self) -> None:
        """Re-read the project. Called once at the start and after EVERY apply —
        stale clip ids are the number-one cause of rejected ops."""
        row = await ve._fetch_row(self.project_id)
        self.timeline = ve._video_edit(row["metadata"]) or {}
        self.memory = ve._video_edit_memory(row["metadata"]) or {}
        self.digest = build_project_digest(
            self.timeline, self.analysis, None, memory=self.memory,
        )

    async def apply(self, ops: list[dict], *, label: str) -> dict:
        """Validate + apply ops, then re-ground. The single write path."""
        if not isinstance(ops, list) or not ops:
            return {"applied": 0, "rejected": [], "note": "no ops given"}

        clips = self.timeline.get("clips") or []
        ops = drop_unapplyable_transitions(list(ops), clips)
        ops = drop_empty_text_popups(ops)
        accepted, rejected = validate_and_filter_ops(
            ops, feature="edit",
            valid_clip_ids=_clip_ids(self.timeline),
            valid_asset_ids=await _enabled_asset_ids(),
            valid_clip_sources=_valid_clip_sources(self.timeline),
        )
        self.all_rejected.extend(rejected)
        if not accepted:
            return {"applied": 0, "rejected": rejected,
                    "note": "every op was rejected — check the ids against inspect_timeline"}

        # Random plan id: additive work must never sweep a previous plan's clips.
        result = await apply_plan(
            self.project_id,
            plan_id=f"ag{self.project_id}_{secrets.token_hex(3)}",
            ops=accepted, analysis=self.analysis,
        )
        n = int(result.get("applied", 0))
        self.applied += n
        if result.get("rev") is not None:
            self.rev = result.get("rev")
        await self.reground()
        log.info("[ve-agent] project #%d %s: applied %d/%d op(s)",
                 self.project_id, label, n, len(ops))
        return {
            "applied": n,
            "failed": list(result.get("failed") or []),
            "rejected": rejected,
            "rev": result.get("rev"),
        }


# ---------------------------------------------------------------------------
# Vision: turn a timeline moment into an actual JPEG the model can see
# ---------------------------------------------------------------------------

def _clip_at(timeline: dict, timeline_f: int, *, kind: str = "video") -> Optional[dict]:
    """The clip covering a timeline frame, preferring the MAIN video lane when
    an overlay sits on top of it (we want the footage, not the sticker)."""
    hits = []
    for c in (timeline.get("clips") or []):
        if (c.get("kind") or "video") != kind:
            continue
        start = int(c.get("from") or 0)
        end = start + int(c.get("durationInFrames") or 0)
        if start <= timeline_f < end:
            hits.append(c)
    if not hits:
        return None
    # Skip the overlay lane — we want the footage, not a sticker we placed.
    footage = [c for c in hits if c.get("trackId") not in _OVERLAY_TRACKS] or hits
    # When AI B-roll covers the base video at this moment, the B-roll is what
    # the viewer actually sees, so that is the frame to judge. Picking by list
    # order would be arbitrary.
    on_top = [c for c in footage if c.get("trackId") == AI_MEDIA_TRACK_ID]
    return (on_top or footage)[0]


def source_frame_for(clip: dict, timeline_f: int) -> int:
    """Map a TIMELINE frame to the SOURCE-LOCAL frame of `clip`.

    The usual identity is `source_f = timeline_f - (from - trimBefore)`, but it
    only holds at playbackRate 1. `set_speed` keeps trimBefore/trimAfter and
    shrinks durationInFrames instead, so on a re-timed clip both that identity
    and `_clip_window`'s `te` point at the wrong moment — the agent would look
    at a frame seconds away from the one it asked about and place overlays
    against footage that isn't there."""
    tb, te, offset = _clip_window(clip)
    speed = float(clip.get("playbackRate") or 1.0) or 1.0
    if abs(speed - 1.0) > 1e-6:
        frm = int(clip.get("from") or 0)
        out_point = int(clip.get("trimAfter") or te)
        source_f = int(round(tb + (int(timeline_f) - frm) * speed))
        return max(tb, min(source_f, max(tb, out_point - 1)))
    return max(tb, min(int(timeline_f) - offset, max(tb, te - 1)))


async def _frame_jpeg(clip: dict, timeline_f: int, analysis: dict) -> Optional[Path]:
    """A JPEG for this timeline moment: the nearest cached analysis frame when
    it is close enough, else a fresh ffmpeg grab at the exact second."""
    key = _clip_source_key(clip)
    if not key:
        return None
    source_f = source_frame_for(clip, timeline_f)

    # 1. Reuse a sampled frame when one sits within half a second.
    try:
        entry = None
        frames = (((analysis.get("sources") or {}).get(key) or {}).get("frames")) or []
        for f in frames:
            if entry is None or abs(int(f.get("frame_f", 0)) - source_f) < abs(
                int(entry.get("frame_f", 0)) - source_f
            ):
                entry = f
        if entry is not None and abs(int(entry.get("frame_f", 0)) - source_f) <= FPS // 2:
            p = ve._abs_from_rel(entry["path_rel"])
            if p.is_file():
                return p
    except Exception:  # noqa: BLE001 — cache miss is fine, fall through to ffmpeg
        pass

    # 2. Extract exactly what was asked for.
    try:
        kind, sid = key.split(":", 1)
        local = await ve.resolve_source_local(kind, int(sid))
        if not local:
            return None
        dest = _ai_dir(kind, int(sid)) / f"agent_{source_f}.jpg"
        if dest.is_file():
            return dest
        # ffmpeg is blocking — never run it on the event loop.
        return await asyncio.to_thread(_save_frame_sync, local, source_f / FPS, dest)
    except Exception:  # noqa: BLE001
        log.exception("[ve-agent] frame extract failed for %s @%d", key, timeline_f)
        return None


async def _render_preview(project_id: int, timeline_f: int) -> Optional[Path]:
    """Render ONE composed frame of the current timeline through Remotion.

    This is what lets the agent grade its own work. It degrades gracefully: an
    older Remotion build without /still simply reports that preview is
    unavailable and the agent carries on with source frames."""
    try:
        props, _out = await ve.build_render_props(project_id)
    except Exception:  # noqa: BLE001
        log.exception("[ve-agent] build_render_props failed")
        return None
    dest = ve.VE_ROOT / "ai" / f"preview_{project_id}" / f"f{int(timeline_f)}.jpg"
    dest.parent.mkdir(parents=True, exist_ok=True)
    url = f"{config.remotion_base_url.rstrip('/')}/still"
    try:
        async with httpx.AsyncClient(timeout=180) as client:
            r = await client.post(url, json={
                "template": "video-edit",
                "output_path": str(dest),
                "props": props,
                "frame": int(timeline_f),
            })
        if r.status_code == 404:
            log.info("[ve-agent] Remotion /still not available (older build)")
            return None
        r.raise_for_status()
    except Exception:  # noqa: BLE001
        log.exception("[ve-agent] preview render failed")
        return None
    return dest if dest.is_file() else None


# ---------------------------------------------------------------------------
# Tool definitions
# ---------------------------------------------------------------------------

# One edit op. The fields are the UNION across op types — each op uses the few
# that apply to it, and the executor validates the rest.
#
# This has to be a concrete schema, not a free-form object: Gemini's function
# declarations reject an OBJECT with no properties, so `{"type":"object"}` here
# would make every tool call fail on the Gemini path. Spelling the fields out
# also measurably improves what both providers emit.
_OP_ITEM_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "op": {
            "type": "string",
            "description": "The operation name, e.g. add_text_popup / trim_clip.",
            "enum": [
                "reorder_clips", "set_clip_position", "trim_clip", "add_transition",
                "add_text_popup", "add_framed_popup", "add_sticker", "set_text",
                "set_keyframes", "set_filters", "resize_clip", "set_speed",
                "mute_clip", "delete_clip", "add_music", "add_media_clip",
                "trim_to_video_end",
            ],
        },
        "clip_id": {"type": "string", "description": "Target clip id."},
        "track_id": {"type": "string"},
        "clip_ids": {"type": "array", "items": {"type": "string"},
                     "description": "reorder_clips: FULL permutation of the track's clips."},
        "from": {"type": "integer", "description": "Timeline start, INTEGER FRAMES @30fps."},
        "durationInFrames": {"type": "integer"},
        "trimBefore": {"type": "integer"},
        "trimAfter": {"type": "integer"},
        "text": {"type": "string"},
        "preset": {"type": "string", "description": "add_transition: fade/slide/wipe/flip/iris…"},
        "x": {"type": "number", "description": "Pixels from canvas CENTRE (+x right)."},
        "y": {"type": "number", "description": "Pixels from canvas CENTRE (+y down)."},
        "scale": {"type": "number"},
        "factor": {"type": "number", "description": "resize_clip: relative multiplier."},
        "fontSize": {"type": "integer"},
        "color": {"type": "string"},
        "fontFamily": {"type": "string"},
        "font_asset_id": {"type": "integer"},
        "frame_asset_id": {"type": "integer", "description": "add_framed_popup: REQUIRED."},
        "asset_id": {"type": "integer", "description": "add_sticker: REQUIRED."},
        "anim": {"type": "string", "enum": ["fade", "pop", "slide_up", "title"]},
        "property": {"type": "string",
                     "enum": ["opacity", "scale", "x", "y", "rotation", "volume"]},
        "points": {
            "type": "array",
            "description": "set_keyframes: [{f: CLIP-LOCAL frame, v: value}].",
            "items": {
                "type": "object",
                "properties": {"f": {"type": "number"}, "v": {"type": "number"}},
                "required": ["f", "v"],
            },
        },
        "brightness": {"type": "number"}, "contrast": {"type": "number"},
        "saturate": {"type": "number"}, "blur": {"type": "number"},
        "speed": {"type": "number"},
        "muted": {"type": "boolean",
                  "description": "mute_clip: REQUIRED — true to mute, false to unmute."},
        "style_or_query": {"type": "string", "description": "add_music: the vibe."},
        "source_asset_id": {"type": "integer"},
        "source_upload_id": {"type": "integer"},
        "source_kind": {"type": "string", "description": "add_media_clip: bin item kind."},
        "source_id": {"type": "integer", "description": "add_media_clip: bin item id."},
        "overlay": {"type": "boolean", "description": "add_media_clip: small PiP inset."},
        "placement": {"type": "string", "enum": ["append", "at"]},
        "duck": {"type": "boolean"},
    },
    "required": ["op"],
}


def _tool_defs() -> list[dict]:
    return [
        {
            "name": "inspect_timeline",
            "description": (
                "The current timeline: every clip with its id, what it IS, its "
                "start/duration in SECONDS, track, and current styling — plus the "
                "media bin and the popups already on screen. Call this first, and "
                "again after edits if you need fresh ids."
            ),
            "input_schema": {"type": "object", "properties": {}},
        },
        {
            "name": "read_transcript",
            "description": (
                "What is SAID in a clip, with timings. Use it to find the hook, "
                "the payoff, and the dead air before you cut or caption."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "clip_id": {"type": "string"},
                    "from_s": {"type": "number", "description": "optional window start (seconds)"},
                    "to_s": {"type": "number", "description": "optional window end (seconds)"},
                },
                "required": ["clip_id"],
            },
        },
        {
            "name": "look",
            "description": (
                "SEE the footage: returns the actual frame at a moment, plus the "
                "scene summary, mood and where faces are. Use this before placing "
                "anything on screen so overlays miss the face and land in empty "
                "space. `at_s` is a TIMELINE time in seconds."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "at_s": {"type": "number", "description": "timeline time in seconds"},
                },
                "required": ["at_s"],
            },
        },
        {
            "name": "preview",
            "description": (
                "Render the CURRENT edit at a moment and look at it — footage with "
                "your overlays composited on top. Use it after adding text or "
                "stickers to check size, position and readability, then fix what "
                "looks wrong. Costs a render, so use it on the moments that matter."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "at_s": {"type": "number", "description": "timeline time in seconds"},
                },
                "required": ["at_s"],
            },
        },
        {
            "name": "suggest_overlay_slots",
            "description": (
                "Safe places to put text/stickers, computed from the footage: each "
                "slot gives from/duration in seconds, an x/y that avoids faces, and "
                "why that moment matters. Prefer these over inventing coordinates."
            ),
            "input_schema": {"type": "object", "properties": {}},
        },
        {
            "name": "search_assets",
            "description": (
                "Find saved stickers, images, frames, fonts or music in the brand "
                "library (semantic search). Returns ids to use in ops."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "kind": {"type": "string", "enum": ["image", "font", "audio"]},
                },
                "required": ["query"],
            },
        },
        {
            "name": "get_popup_examples",
            "description": (
                "How this brand's best-performing Instagram posts use popups "
                "(placement, scale, timing). Copy the STYLE, never the words."
            ),
            "input_schema": {
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
            },
        },
        {
            "name": "read_edit_log",
            "description": "Edits already made to this project, so you don't redo work.",
            "input_schema": {
                "type": "object",
                "properties": {"limit": {"type": "integer"}},
            },
        },
        {
            "name": "generate_image",
            "description": (
                "Create a NEW graphic (sticker, icon, badge, arrow) when the "
                "library has nothing suitable. Returns an asset_id you can place "
                "with add_sticker. Ask for a transparent-background PNG-style "
                "graphic; keep it simple and bold so it reads at small size."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "prompt": {"type": "string", "description": "what the graphic shows"},
                    "name": {"type": "string", "description": "short label for the library"},
                },
                "required": ["prompt"],
            },
        },
        {
            "name": "run_pass",
            "description": (
                "Run one of the deterministic passes the app already owns, instead "
                "of hand-rolling it: 'captions' (burn synced captions from the "
                "transcript), 'duck' (dip the music under the speech), 'silence' "
                "(trim dead air at clip edges), 'music' (lay a library music bed "
                "matching `query`). These are exact and cheap — prefer them over "
                "emitting dozens of ops yourself."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "pass_name": {"type": "string",
                                  "enum": ["captions", "duck", "silence", "music"]},
                    "query": {"type": "string", "description": "music vibe (music only)"},
                    "clip_id": {"type": "string", "description": "limit to one clip (silence only)"},
                },
                "required": ["pass_name"],
            },
        },
        {
            "name": "edit",
            "description": (
                "Apply edit operations to the timeline. This is how you change "
                "anything. Times are INTEGER FRAMES at 30fps unless the op takes "
                "seconds. Returns what applied and what was rejected — read it, "
                "fix the ids, and try again if something bounced."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "ops": {
                        "type": "array",
                        "items": _OP_ITEM_SCHEMA,
                        "description": (
                            "Ops. Common shapes: "
                            "{op:'reorder_clips',track_id,clip_ids:[full permutation]}; "
                            "{op:'trim_clip',clip_id,trimBefore,trimAfter}; "
                            "{op:'set_clip_position',clip_id,from}; "
                            "{op:'add_transition',clip_id(LATER clip),preset,durationInFrames}; "
                            "{op:'add_text_popup',text,from,durationInFrames,x,y,fontSize,color,anim}; "
                            "{op:'add_framed_popup',text,from,durationInFrames,frame_asset_id,font_asset_id,fontSize,color}; "
                            "{op:'add_sticker',asset_id,from,durationInFrames,x,y,scale,anim}; "
                            "{op:'set_text',clip_id,text}; "
                            "{op:'set_keyframes',clip_id,property,points:[{f,v}]}; "
                            "{op:'set_filters',clip_id,brightness,contrast,saturate,blur}; "
                            "{op:'resize_clip',clip_id,factor}; "
                            "{op:'set_speed',clip_id,speed}; "
                            "{op:'mute_clip',clip_id,muted:true}; {op:'delete_clip',clip_id}; "
                            "{op:'add_media_clip',source_kind,source_id,overlay,placement}"
                        ),
                    },
                },
                "required": ["ops"],
            },
        },
        {
            "name": "finish",
            "description": (
                "Call when the instruction is satisfied. Give a one-or-two sentence "
                "summary of what you changed, in plain language for the user."
            ),
            "input_schema": {
                "type": "object",
                "properties": {"summary": {"type": "string"}},
                "required": ["summary"],
            },
        },
    ]


_SYSTEM = (
    "You are a senior short-form video editor working directly on a real "
    "timeline, and you can SEE the footage.\n\n"
    "HOW TO WORK\n"
    "- Start with inspect_timeline. Then LOOK before you decide: read_transcript "
    "for what is said, look() for what is on screen. Do not place anything on "
    "screen without looking at that moment first.\n"
    "- After you add or move something visible, preview() that moment and judge "
    "it like an editor: is the text readable, is it covering the face, is it the "
    "right size, does it sit in dead space? If it is wrong, fix it and check "
    "again. You are allowed to iterate — that is the point.\n"
    "- Prefer run_pass for captions / ducking / silence / music: those passes are "
    "exact and cost you one call instead of thirty ops.\n"
    "- Use real ids from inspect_timeline. Never invent a clip_id or asset_id. If "
    "an op is rejected, read the reason, re-inspect, and retry — do not repeat "
    "the same broken op.\n"
    "- Call finish() when the instruction is satisfied.\n\n"
    "EDITORIAL TASTE\n"
    "- Serve the instruction. If the user asked for one small thing, do that one "
    "thing well and stop — do not redecorate the video.\n"
    "- Keep overlays off faces and inside the title-safe area. Two on screen at "
    "once is already a lot; usually one.\n"
    "- Text earns its place: 2-6 punchy words that reinforce what is being said "
    "or shown right then. Never generic filler like 'Welcome' or 'Amazing'.\n"
    "- Cut dead air, but never clip into speech.\n"
    "- Transitions are seasoning. Vary them, and let a hard cut land when the "
    "energy calls for it.\n"
    "- When you are asked for a full edit, deliver something clearly edited: a "
    "hook-first order, trimmed dead air, deliberate cuts, a readable text layer "
    "and a music bed that fits the mood. Let the content decide the shape — a "
    "calm explainer and a hype reel should not come out looking identical."
)


# ---------------------------------------------------------------------------
# Deterministic passes exposed as one tool (their PURE cores, never _run_*)
# ---------------------------------------------------------------------------

async def _pass_captions(run: _Run) -> tuple[list[dict], str]:
    from . import yas_video_edit_ai as veai  # lazy: avoids an import cycle

    segs = veai._caption_segments_timeline(run.timeline, run.analysis)
    if not segs:
        return [], "no transcript — run the analysis first or pick another pass"
    style_color, style_font = await veai._brand_caption_style()
    # Mirror _run_captions exactly. Two traps live here: the segments are keyed
    # start_f/end_f (NOT from/durationInFrames), and `y` is a CENTRE-RELATIVE
    # PIXEL offset — passing the 0.34 lower-third fraction raw would plant every
    # caption on the speaker's face.
    height = int(((run.timeline.get("project") or {}).get("height")) or 1920)
    caption_y = round(0.34 * height)
    ops = [{
        "op": "add_text_popup",
        "text": s["text"][:120],
        "from": int(s["start_f"]),
        "durationInFrames": max(18, int(s["end_f"]) - int(s["start_f"])),
        "y": caption_y, "fontSize": 54, "color": style_color,
        "fontFamily": style_font, "anim": "fade",
    } for s in segs[:120]]
    return ops, f"{len(ops)} caption(s) from the transcript"


async def _pass_duck(run: _Run) -> tuple[list[dict], str]:
    from . import yas_video_edit_ai as veai
    from . import yas_video_edit_ai_ops as ai_ops

    music = veai._find_music_clip(run.timeline.get("clips") or [])
    if not music:
        return [], "there is no music clip to duck — add music first"
    speech = veai._speech_segments_timeline(run.timeline, run.analysis)
    if not speech:
        return [], "no speech detected, so there is nothing to duck under"
    points = ai_ops._ducking_volume_keyframes(
        clip_from=int(music.get("from") or 0),
        # A zero-length music clip would produce an empty curve — fall back to
        # the project duration, exactly as _run_duck does.
        duration=(int(music.get("durationInFrames") or 0)
                  or veai._project_duration_f(run.timeline)),
        speech_segments=speech,
        bed=0.5,
    )
    return ([{"op": "set_keyframes", "clip_id": music.get("id"),
              "property": "volume", "points": points}],
            "music ducked under the speech")


async def _pass_silence(run: _Run, clip_id: Optional[str]) -> tuple[list[dict], str]:
    from . import yas_video_edit_ai as veai

    ops, note = veai._silence_trim_plan(run.timeline, run.analysis, clip_id)
    return ops, note or "trimmed dead air"


async def _pass_music(run: _Run, query: Optional[str]) -> tuple[list[dict], str]:
    from . import yas_video_edit_ai as veai

    # mode="library" so the provider can never park the job at waiting_provider.
    kind, result, base_op = await veai._resolve_music(
        run.project_id, run.timeline, run.analysis, query, "library",
    )
    if kind == "asset" and result.get("asset_id"):
        base_op["source_asset_id"] = int(result["asset_id"])
    elif kind == "upload" and result.get("upload_id"):
        base_op["source_upload_id"] = int(result["upload_id"])
    else:
        return [], "no music in the library matched that — try a different vibe"
    base_op["duck"] = True
    return [base_op], "music bed laid from the library"


# ---------------------------------------------------------------------------
# The run
# ---------------------------------------------------------------------------

async def run_agent(
    project_id: int,
    instruction: str,
    target_clip_id: Optional[str],
    analysis: dict,
    *,
    on_step=None,
    extra_context: str = "",
) -> dict:
    """Drive the look -> think -> act loop. Returns
    {applied, rev, reply, steps, provider, model, stopped}."""
    run = _Run(project_id, analysis)
    await run.reground()
    finished: dict = {}

    async def dispatch(name: str, inp: dict) -> Any:
        # ---- read / look -------------------------------------------------
        if name == "inspect_timeline":
            clips = []
            for row in (run.digest.get("clips") or []):
                clips.append({
                    "id": row.get("id"), "kind": row.get("kind"),
                    "track": row.get("track"), "is": row.get("is"),
                    "role": row.get("role"),
                    "start_s": _f2s(row.get("from")),
                    "duration_s": _f2s(row.get("duration")),
                    "text": row.get("text"), "muted": row.get("muted"),
                    "keyframed": row.get("keyframed"),
                })
            bin_items = [
                {"kind": b.get("kind"), "id": b.get("id"), "name": b.get("name")}
                for b in ((run.timeline.get("project") or {}).get("media_bin") or [])
                if isinstance(b, dict)
            ]
            popups = [{"index": g.get("index"), "label": g.get("label"),
                       "at_s": _f2s(g.get("from"))}
                      for g in group_popups_by_time(run.timeline.get("clips") or [])[:12]]
            return json.dumps({
                "project": run.digest.get("project"),
                "tracks": run.digest.get("tracks"),
                "clips": clips,
                "media_bin": bin_items,
                "popups_on_screen": popups,
                "selected_clip_id": target_clip_id,
            }, default=str)

        if name == "read_transcript":
            cid = str(inp.get("clip_id") or "")
            row = next((r for r in (run.digest.get("clips") or [])
                        if str(r.get("id")) == cid), None)
            if not row:
                return f"no clip {cid!r} — call inspect_timeline for the real ids"
            return json.dumps({
                "clip_id": cid,
                "transcript": row.get("transcript"),
                "scenes": [{"at_s": _f2s(s.get("at")), "until_s": _f2s(s.get("until")),
                            "summary": s.get("summary"), "energy": s.get("energy")}
                           for s in (row.get("scenes") or [])],
            }, default=str)[:6000]

        if name == "look":
            if run.looks >= _MAX_LOOKS_PER_RUN:
                return "look budget for this run is used up — decide with what you have"
            run.looks += 1
            tf = _s2f(inp.get("at_s"))
            clip = _clip_at(run.timeline, tf)
            if not clip:
                return f"nothing on the video track at {inp.get('at_s')}s"
            jpeg = await _frame_jpeg(clip, tf, run.analysis)
            row = next((r for r in (run.digest.get("clips") or [])
                        if str(r.get("id")) == str(clip.get("id"))), {}) or {}
            meta = {
                "at_s": _f2s(tf), "clip_id": clip.get("id"),
                "mood": row.get("mood"), "speech": row.get("speech"),
                "scene": next((s.get("summary") for s in (row.get("scenes") or [])
                               if int(s.get("at", 0)) <= tf <= int(s.get("until", 0))), None),
                "canvas": {"width": (run.digest.get("project") or {}).get("width"),
                           "height": (run.digest.get("project") or {}).get("height")},
                "note": ("Overlay x/y are pixels relative to the CANVAS CENTRE "
                         "(0,0 = centre, +x right, +y down)."),
            }
            if not jpeg:
                return json.dumps({**meta, "frame": "unavailable"}, default=str)
            return {"text": json.dumps(meta, default=str), "images": [jpeg]}

        if name == "preview":
            if run.previews >= _MAX_PREVIEWS_PER_RUN:
                return "preview budget for this run is used up — finish with what you have"
            run.previews += 1
            tf = _s2f(inp.get("at_s"))
            shot = await _render_preview(project_id, tf)
            if not shot:
                return ("preview is unavailable in this deployment (the render "
                        "service has no /still endpoint yet) — judge from look() instead")
            return {"text": json.dumps({"at_s": _f2s(tf),
                                        "note": "This is the CURRENT edit composited."}),
                    "images": [shot]}

        if name == "suggest_overlay_slots":
            try:
                cands = candidate_popup_windows(run.timeline, run.analysis)
            except Exception:  # noqa: BLE001
                cands = []
            return json.dumps([{
                "from_s": _f2s(c.get("from")),
                "duration_s": _f2s(c.get("durationInFrames")),
                "x": c.get("x"), "y": c.get("y"), "why": c.get("why"),
            } for c in cands[:12]], default=str)[:5000]

        if name == "search_assets":
            res = await search_assets_svc(inp.get("query", ""), kind=inp.get("kind"), limit=8)
            return json.dumps(res, default=str)[:6000]

        if name == "get_popup_examples":
            return (await get_popup_examples(inp.get("query", ""))) or "(no proven examples yet)"

        if name == "read_edit_log":
            elog = (run.memory.get("edit_log") or []) if isinstance(run.memory, dict) else []
            return json.dumps(elog[-int(inp.get("limit") or 12):], default=str)[:4000]

        # ---- act ---------------------------------------------------------
        if name == "generate_image":
            return await _tool_generate_image(inp)

        if name == "run_pass":
            pass_name = str(inp.get("pass_name") or "")
            try:
                if pass_name == "captions":
                    ops, note = await _pass_captions(run)
                elif pass_name == "duck":
                    ops, note = await _pass_duck(run)
                elif pass_name == "silence":
                    ops, note = await _pass_silence(run, inp.get("clip_id"))
                elif pass_name == "music":
                    ops, note = await _pass_music(run, inp.get("query"))
                else:
                    return f"unknown pass {pass_name!r}"
            except Exception as exc:  # noqa: BLE001 — a failed pass must not kill the run
                log.exception("[ve-agent] pass %s failed", pass_name)
                return f"the {pass_name} pass failed: {exc}"
            if not ops:
                return note
            out = await run.apply(ops, label=f"pass:{pass_name}")
            return json.dumps({"pass": pass_name, "note": note, **out}, default=str)[:2000]

        if name == "edit":
            ops = inp.get("ops") or []
            if not isinstance(ops, list):
                return "ops must be an array"
            out = await run.apply([o for o in ops if isinstance(o, dict)], label="edit")
            return json.dumps(out, default=str)[:2500]

        if name == "finish":
            finished["summary"] = str(inp.get("summary") or "").strip()
            return "acknowledged — the run is complete"

        return f"unknown tool {name!r}"

    opening = _opening_message(run, instruction, target_clip_id, extra_context)

    error: Optional[str] = None
    try:
        res = await reasoner_tool_loop(
            system=_SYSTEM,
            user_message=opening,
            tools=_tool_defs(),
            tool_dispatch=dispatch,
            on_step=on_step,
            max_tokens=16000,
            effort="high",
        )
    except Exception as exc:  # noqa: BLE001
        # The loop died (provider outage, bad request, timeout). Edits already
        # applied are REAL and committed, so report them rather than returning
        # applied=0 — otherwise the UI never refetches and the user's timeline
        # silently diverges from what they see.
        log.exception("[ve-agent] project #%d: reasoning loop failed", project_id)
        error = str(exc)[:300]
        res = {"text": "", "steps": [], "provider": None, "model": None,
               "stopped": "error"}

    reply = finished.get("summary") or (res.get("text") or "").strip()
    if not reply:
        if error:
            reply = (f"I hit a problem partway through"
                     f"{f' after {run.applied} change(s)' if run.applied else ''}.")
        else:
            reply = (f"Made {run.applied} change(s)." if run.applied
                     else "I had a look but didn't find a change worth making.")
    return {
        "applied": run.applied,
        "rev": run.rev,
        "reply": reply,
        "steps": res.get("steps") or [],
        "provider": res.get("provider"),
        "model": res.get("model"),
        "stopped": res.get("stopped"),
        "rejected": run.all_rejected,
        "error": error,
    }


def _opening_message(run: _Run, instruction: str, target_clip_id: Optional[str],
                     extra_context: str) -> str:
    proj = run.digest.get("project") or {}
    clips = [{
        "id": r.get("id"), "kind": r.get("kind"), "is": r.get("is"),
        "start_s": _f2s(r.get("from")), "duration_s": _f2s(r.get("duration")),
    } for r in (run.digest.get("clips") or [])]
    head = (
        f"INSTRUCTION: {instruction.strip()}\n"
        f"SELECTED clip: {target_clip_id or '(none)'}\n"
        f"CANVAS: {proj.get('width')}x{proj.get('height')} "
        f"({proj.get('aspect')}), {_f2s(proj.get('durationInFrames'))}s long\n\n"
        "CLIPS (detail via tools):\n"
        + json.dumps(clips, default=str)[:5000]
    )
    if extra_context:
        head += "\n\n" + extra_context.strip()[:2500]
    return head


async def _tool_generate_image(inp: dict) -> str:
    """Nano Banana -> a real library asset the agent can then place."""
    prompt = str(inp.get("prompt") or "").strip()
    if not prompt:
        return "prompt is required"
    label = str(inp.get("name") or prompt)[:60]
    try:
        from .vertex_client import gemini_image

        data = await gemini_image(
            "Flat vector sticker graphic for a social video overlay. "
            "Bold, simple, high contrast, centred, plain solid background, "
            "no text unless asked. Subject: " + prompt,
            aspect_ratio="1:1",
        )
    except Exception as exc:  # noqa: BLE001
        log.exception("[ve-agent] generate_image failed")
        return f"image generation failed: {exc}"

    try:
        asset_id = await _store_generated_asset(data, label, prompt)
    except Exception as exc:  # noqa: BLE001
        log.exception("[ve-agent] storing generated image failed")
        return f"the image was generated but could not be saved: {exc}"
    return json.dumps({
        "asset_id": asset_id, "name": label,
        "note": "place it with {op:'add_sticker', asset_id, from, durationInFrames, scale}",
    })


async def _store_generated_asset(data: bytes, label: str, description: str) -> int:
    """Persist generated bytes as a real library asset so `add_sticker` (which
    validates against enabled producer_assets) can place it. Mirrors the
    talking-head save-to-assets path: storage upload, then one insert."""
    from psycopg.types.json import Jsonb

    from . import asset_storage, thumbs

    slug = f"ai-sticker-{secrets.token_hex(5)}"
    up = await asset_storage.upload(
        data=data, path=f"generated/{slug}.png", mime="image/png",
    )
    # A stored 480px derivative, so the Assets grid shows this sticker as a
    # small tile (on S3 a row without one gets no thumb_url at all).
    thumb_key = await thumbs.ensure_thumb_object(
        raw=data, storage_path=up.get("storage_path"),
        storage_bucket=up.get("storage_bucket"), mime="image/png",
    )
    # Best-effort embedding so the sticker is findable by search_assets later.
    embedding = None
    try:
        from .vertex_client import gemini_embed

        vecs = await gemini_embed([f"{label}. {description}"])
        embedding = vecs[0] if vecs else None
    except Exception:  # noqa: BLE001 — searchability is a bonus, not a requirement
        embedding = None

    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                INSERT INTO producer_assets
                  (slug, asset_kind, category, display_name, ai_description,
                   storage_bucket, storage_path, local_path,
                   mime_type, bytes, attributes, embedding, metadata, enabled)
                VALUES (%s, 'image', 'free', %s, %s,
                        %s, %s, %s,
                        %s, %s, %s, %s, %s, TRUE)
                RETURNING id
                """,
                (
                    slug, label[:80], description[:500],
                    up.get("storage_bucket"), up.get("storage_path"), up.get("local_path"),
                    "image/png", len(data),
                    Jsonb({"_thumb_path": thumb_key} if thumb_key else {}), embedding,
                    Jsonb({"source": "video_editor_agent", "prompt": description[:1000]}),
                ),
            )
            row = await cur.fetchone()
    return int(row["id"])


# ---------------------------------------------------------------------------
# Job entry
# ---------------------------------------------------------------------------

async def agent_available() -> bool:
    """True when a reasoning brain is actually reachable."""
    try:
        info = await resolve_reasoner()
        return bool(info.ok)
    except Exception:  # noqa: BLE001
        return False


async def run_job(project_id: int, params: dict, analysis: Optional[dict] = None,
                  *, on_step=None) -> dict:
    """Entry for feature='agent'. Returns a dict the caller folds into the job
    blob / chat reply."""
    instruction = str(params.get("instruction") or params.get("message") or "").strip()
    target = params.get("clip_id")
    if not instruction:
        return {"applied": 0, "reply": "Tell me what you'd like to change.", "steps": []}

    if analysis is None:
        analysis = await get_or_build_analysis(project_id)

    return await run_agent(
        project_id, instruction, target, analysis,
        on_step=on_step,
        extra_context=str(params.get("context") or ""),
    )
