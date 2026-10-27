"""Video Editor — B-ROLL auto-edit preset: deterministic helpers.

Opt-in preset (project.preset == "b_roll", feature == "broll") that turns a
bin of raw B-roll clips into a flowing montage WITHOUT any LLM planning:

  1. ORDER  — CLIP embeddings of each clip's first/last frame; a greedy
              best-chain walk maximizes visual continuity between the OUT
              frame of one clip and the IN frame of the next
              (best_order). Falls back to the current order when CLIP is
              disabled (USE_CLIP=false) or embeddings are unavailable.
  2. FLOW   — reorder_clips + short fade joins between adjacent clips that
              are long enough (build_flow_ops).
  3. CARDS  — "light" intro/outro: text-only title cards via the
              executor-only `add_title_card` op (build_light_card_ops).
              The "veo" style reuses the shared bookend pipeline instead
              (orchestrated in yas_video_edit_ai._run_broll).

Everything here is PURE timeline math + media probing — providers, job
state and apply_plan calls live in the orchestrator
(yas_video_edit_ai._run_broll) and the poller
(workers/yas_video_edit_ai_poller._check_broll).
"""
from __future__ import annotations

import asyncio
import logging
import math
import shutil
from pathlib import Path
from typing import Optional

from . import clip_service
from . import yas_video_editor as ve
from .yas_video_edit_ai_ops import AI_VIDEO_TRACK_ID

log = logging.getLogger("archivist.yas_video_edit_ai_broll")


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_EDGE_OFFSET_SEC = 0.3   # sample frames this far inside each clip edge
_JOIN_FADE_F = 12        # fade join length between adjacent clips
_MIN_JOIN_CLIP_F = 45    # both sides of a join must be at least this long
_CARD_DUR_F = 75         # light title-card duration (~2.5s @ 30fps)


# Deterministic plan ids — ONE per project per half, so re-running the preset
# REPLACES its previous output (apply_plan idempotency) instead of stacking.
def PLAN_MAIN(pid: int) -> str:
    return f"broll{pid}m"


def PLAN_MUSIC(pid: int) -> str:
    return f"broll{pid}mu"


def PLAN_BK(pid: int) -> str:
    return f"broll{pid}b"


# ---------------------------------------------------------------------------
# Embedding math
# ---------------------------------------------------------------------------

def _cosine(a: Optional[list], b: Optional[list]) -> float:
    """Cosine similarity; 0.0 whenever either vector is missing/degenerate."""
    if a is None or b is None or len(a) != len(b):
        return 0.0
    dot = 0.0
    na = 0.0
    nb = 0.0
    for x, y in zip(a, b):
        dot += x * y
        na += x * x
        nb += y * y
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (math.sqrt(na) * math.sqrt(nb))


# ---------------------------------------------------------------------------
# Timeline selection
# ---------------------------------------------------------------------------

def primary_video_clips(timeline: dict) -> tuple[Optional[dict], list[dict]]:
    """(primary user video track, its video clips sorted by (from, id)).

    The primary track is the lowest-index kind=='video' track that is NOT the
    AI overlay lane — the same lane add_clip targets."""
    tracks = (timeline or {}).get("tracks") or []
    user_video_tracks = [
        t for t in tracks
        if t.get("kind") == "video" and t.get("id") != AI_VIDEO_TRACK_ID
    ]
    if not user_video_tracks:
        return None, []
    track = min(user_video_tracks, key=lambda t: int(t.get("index", 0) or 0))
    clips = [
        c for c in ((timeline or {}).get("clips") or [])
        if c.get("trackId") == track.get("id")
        and (c.get("kind") or "video") == "video"
    ]
    clips.sort(key=lambda c: (int(c.get("from", 0) or 0), str(c.get("id") or "")))
    return track, clips


# ---------------------------------------------------------------------------
# Edge-frame embeddings (CLIP)
# ---------------------------------------------------------------------------

async def extract_edge_embeddings(
    project_id: int, clips: list[dict],
) -> dict[str, tuple[Optional[list], Optional[list]]]:
    """{clip_id: (first_frame_vec, last_frame_vec)} for each resolvable clip.

    Extracts one JPG just inside each trimmed edge (ffmpeg via ve._save_thumb)
    and CLIP-encodes them in ONE batch. Best-effort throughout: {} when CLIP
    is disabled; missing sources/failed frames simply yield None vectors."""
    if not clip_service.enabled():
        return {}

    dest = ve.VE_ROOT / "ai" / "broll" / f"p{project_id}"
    dest.mkdir(parents=True, exist_ok=True)

    paths: list[Path] = []
    slots: list[tuple[str, int]] = []  # (clip_id, 0=first | 1=last) per path
    try:
        for clip in clips:
            clip_id = str(clip.get("id") or "")
            if not clip_id:
                continue
            kind, sid = ve._clip_source_kind_id(clip)
            if not kind or sid is None:
                continue
            src = await ve.resolve_source_local(kind, int(sid))
            if src is None or not src.exists():
                continue
            tb = int(clip.get("trimBefore") or 0)
            ta = int(clip.get("trimAfter") or clip.get("durationInFrames") or 0)
            first_t = tb / ve.FPS + _EDGE_OFFSET_SEC
            last_t = ta / ve.FPS - _EDGE_OFFSET_SEC
            if last_t <= first_t:  # tiny clip — hug the edges instead
                first_t = tb / ve.FPS
                last_t = max(first_t, ta / ve.FPS - 0.05)
            for slot, (jpg, t) in enumerate((
                (dest / f"{clip_id}_a.jpg", first_t),
                (dest / f"{clip_id}_b.jpg", last_t),
            )):
                ok = await asyncio.to_thread(ve._save_thumb, src, jpg, t)
                if ok:
                    paths.append(jpg)
                    slots.append((clip_id, slot))

        emb: dict[str, tuple[Optional[list], Optional[list]]] = {}
        if paths:
            vecs = await asyncio.to_thread(clip_service.encode_image_paths, paths)
            for (clip_id, slot), vec in zip(slots, vecs):
                first, last = emb.get(clip_id, (None, None))
                if slot == 0:
                    emb[clip_id] = (vec or None, last)
                else:
                    emb[clip_id] = (first, vec or None)
        return emb
    except Exception:
        # This step is documented as best-effort in two places (the module
        # docstring's "Falls back to the current order ... when embeddings are
        # unavailable" and this function's own), but nothing enforced it: the
        # try block only had a `finally`, so ANY failure below — a broken CLIP
        # install, an ffmpeg that cannot decode a source, a vanished upload —
        # propagated out of `_run_broll` and failed the entire B-roll job with
        # zero ops applied (#32). Ordering is the one part of the preset that
        # degrades cleanly: `best_order` already returns the clips' current
        # order when it has no vectors to compare.
        log.exception(
            "[broll] edge-frame embeddings failed for project #%d — "
            "keeping the clips' current order",
            project_id,
        )
        return {}
    finally:
        shutil.rmtree(dest, ignore_errors=True)


# ---------------------------------------------------------------------------
# Ordering — greedy best-flow chain
# ---------------------------------------------------------------------------

def best_order(
    clips: list[dict],
    emb: dict[str, tuple[Optional[list], Optional[list]]],
) -> list[str]:
    """Clip ids reordered for the smoothest visual flow: similarity between
    clip i's LAST frame and clip j's FIRST frame scores the join i→j; a greedy
    chain is built from every possible start and the best total wins.
    Deterministic (ties break on the original index). Falls back to the
    current order when there is nothing to score."""
    ids = [str(c.get("id") or "") for c in clips]
    if len(ids) < 2 or not emb:
        return ids

    n = len(ids)
    S = [[0.0] * n for _ in range(n)]
    for i in range(n):
        for j in range(n):
            if i == j:
                continue
            S[i][j] = _cosine(
                emb.get(ids[i], (None, None))[1],
                emb.get(ids[j], (None, None))[0],
            )

    best_chain = list(range(n))
    best = -math.inf
    for s in range(n):
        chain = [s]
        used = {s}
        total = 0.0
        while len(chain) < n:
            cur = chain[-1]
            nxt = min(
                (j for j in range(n) if j not in used),
                key=lambda j: (-S[cur][j], j),
            )
            total += S[cur][nxt]
            chain.append(nxt)
            used.add(nxt)
        if total > best:
            best = total
            best_chain = chain
    return [ids[i] for i in best_chain]


# ---------------------------------------------------------------------------
# Op builders (consumed by apply_plan — all executor-vocabulary ops)
# ---------------------------------------------------------------------------

def pin_and_order(
    clips: list[dict],
    emb: dict[str, tuple[Optional[list], Optional[list]]],
    intro_upload_id: Optional[int],
    outro_upload_id: Optional[int],
) -> list[dict]:
    """Order clips for flow, but PIN an uploaded intro clip first and an uploaded
    outro clip last (the user's own bookends). Only the middle clips get the
    CLIP best-flow chain. Returns the clip dicts in their final order.

    A clip matches when its source is ("upload", <id>) — see
    ve._clip_source_kind_id."""
    def _is_upload(c: dict, uid: Optional[int]) -> bool:
        if uid is None:
            return False
        kind, sid = ve._clip_source_kind_id(c)
        return kind == "upload" and sid is not None and int(sid) == int(uid)

    intro = next((c for c in clips if _is_upload(c, intro_upload_id)), None)
    outro = next((c for c in clips if _is_upload(c, outro_upload_id)), None)
    pinned_ids = {str((intro or {}).get("id")), str((outro or {}).get("id"))}
    middle = [c for c in clips if str(c.get("id")) not in pinned_ids]

    order_ids = best_order(middle, emb)
    by_id = {str(c.get("id")): c for c in middle}
    ordered_middle = [by_id[cid] for cid in order_ids if cid in by_id]

    final: list[dict] = []
    if intro is not None:
        final.append(intro)
    final.extend(ordered_middle)
    if outro is not None:
        final.append(outro)
    return final


def build_flow_ops(track_id: str, ordered: list[dict]) -> list[dict]:
    """reorder_clips into the chosen order + a short fade join before each
    clip whose neighbours are both long enough. `ordered` is the clip dicts
    in their NEW order."""
    if len(ordered) < 2:
        return []
    ops: list[dict] = [{
        "op": "reorder_clips",
        "track_id": track_id,
        "clip_ids": [c["id"] for c in ordered],
    }]
    for prev, cur in zip(ordered, ordered[1:]):
        if (
            int(prev.get("durationInFrames") or 0) >= _MIN_JOIN_CLIP_F
            and int(cur.get("durationInFrames") or 0) >= _MIN_JOIN_CLIP_F
        ):
            ops.append({
                "op": "add_transition",
                "clip_id": cur["id"],
                "preset": "fade",
                "durationInFrames": _JOIN_FADE_F,
                "timing": "linear",
            })
    return ops


def build_light_card_ops(texts_by_role: dict) -> list[dict]:
    """One add_title_card op per role (intro FIRST — its shift must land
    before the outro's end-of-timeline math runs in the executor)."""
    ops: list[dict] = []
    for role in ("intro", "outro"):
        t = texts_by_role.get(role) or {}
        ops.append({
            "op": "add_title_card",
            "role": role,
            "text": (t.get("headline") or ("Thanks for watching" if role == "outro" else "")),
            "subtitle": (t.get("subtitle") or ("Follow for more" if role == "outro" else "")),
            "durationInFrames": _CARD_DUR_F,
        })
    return ops
