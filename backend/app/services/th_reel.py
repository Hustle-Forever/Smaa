"""What a talking-head-v2 reel is made of, and finalizing it (again).

The single owner of "which clip goes where, for how long" in a TH v2 reel:
the stitch (Finalize), the Rebrand stage's strip and the studio all read it
from here, so they cannot disagree about a scene's source.

  targets / resolve_sources   the reel's segments in order (intro, main 1..n,
                              outro), each with the source the stitch uses,
                              by today's per-template priorities
  inputs / inputs_sha         what the reel is made of, as one hash: a reel
                              whose inputs are unchanged since its last good
                              render is not out of date
  resolve / payload           the frozen plan Finalize stores on the row
                              (composition_plan) and the poller posts to
                              Remotion unchanged
  finalize                    Finalize: one compare-and-set on the run row
                              (the poller of the instance that set it renders
                              it; the last good reel stays served meanwhile)

Rows finalized here carry `render_owner` and `render_job_id: ""`. Old code
(the production VPS polls the same database) selects new work by
`NOT (metadata ? 'render_job_id')` and returns at once on an empty job id,
so it never touches them; rows WITHOUT render_owner keep today's poller code
byte for byte.

Every database write goes through a Store (PgStore; MemoryStore for tests —
the autouse conftest guard swaps it in, so no test writes the shared row).
"""
from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import logging
import math
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional, Protocol, Sequence

from fastapi import HTTPException
from psycopg.types.json import Jsonb

from ..db import get_conn
from ..workers import yas_th_v2_composition_poller as poller
from .yas_video_editor import INSTANCE_ID

log = logging.getLogger("archivist.th_reel")

MODE_PREVIEW = "talking_head_v2_preview"
COMP_QUEUED = "queued"
COMP_VIDEOS_READY = "videos_ready"
COMP_RENDERING = "rendering"
COMP_READY = "ready"
COMP_FAILED = "failed"
ANIM_READY = "ready"
# The statuses a Finalize may start from (a render in flight is only taken
# over when its owner has been gone STALE_OWNER_MIN).
FINALIZE_FROM = (COMP_VIDEOS_READY, COMP_READY, COMP_FAILED)
STALE_OWNER_MIN = 20

TEMPLATE_SINGLE = "talking-head-v2-with-cards"
TEMPLATE_MULTI = "talking-head-v2-multi-scene"
FPS = 30
FADE_SEC = 0.4
MAIN_DEFAULT_SEC = 8
BOOKEND_DEFAULT_SEC = 4

CHANGED_ELSEWHERE = "The reel changed in another tab. Check it, then Finalize again."
NOT_READY = "Videos are not all ready yet — wait for the previews to land."
ALREADY_RENDERING = "The reel is already being made."


# ---------------------------------------------------------------------------
# The store: every database write of the reel (and the reads they depend on)
# ---------------------------------------------------------------------------

# One compare-and-set: the row moves to `rendering` only from a status a
# Finalize may start from, or from a render whose owner vanished (a Docker
# rebuild changes the hostname-based INSTANCE_ID) more than 20 minutes ago,
# and only while its seq is still the one the plan was made after (a press
# whose read came before another press's whole render would otherwise take
# the same seq, and render over the file being served).
# composition_path is not in the patch: it keeps pointing at the last good
# reel while the new one renders.
FINALIZE_SQL = """
UPDATE yas_generations
   SET metadata = (metadata - 'composition_error' - 'render_failed_seq') || %s::jsonb, updated_at = NOW()
 WHERE id = %s
   AND COALESCE((metadata->>'composition_seq')::int, 0) = %s
   AND ( metadata->>'composition_status' = ANY(%s)
      OR ( metadata->>'composition_status' = 'rendering' AND metadata ? 'render_owner'
           AND (metadata->>'render_started_at')::timestamptz < NOW() - INTERVAL '20 minutes') )
RETURNING id
"""

PATCH_SQL = """
UPDATE yas_generations
   SET metadata = metadata || %s::jsonb,
       updated_at = NOW()
 WHERE id = %s
"""

RUN_SQL = "SELECT id, topic, saved_asset_id, metadata FROM yas_generations WHERE id = %s AND mode = %s"
ANIMS_SQL = ("SELECT id, status, local_path, duration_sec, storage_bucket, storage_path, error "
             "FROM yas_animations WHERE id = ANY(%s)")

# What goes in the reel per scene: metadata.reel_choices = {target: {src,
# pick, pending, end_frame, at}} — the version that ships (pick; the one on
# its way: pending; None: the original) and where the scene ends (None:
# automatic), for the clip `src`. One jsonb_set path per target, so neither
# another target's write nor any TH v2 writer's shallow `metadata || patch`
# (none carries reel_choices) can clobber it; a write for another clip
# replaces the entry instead of merging into it (its pick and end were made
# on the old clip).
CHOICE_SQL = """
UPDATE yas_generations
   SET metadata = jsonb_set(metadata || jsonb_build_object('reel_choices', COALESCE(metadata->'reel_choices', '{}'::jsonb)),
                            ARRAY['reel_choices', %(t)s::text],
                            CASE WHEN metadata->'reel_choices'->%(t)s::text->>'src' IS DISTINCT FROM %(src)s::text THEN %(patch)s::jsonb
                                 ELSE COALESCE(metadata->'reel_choices'->%(t)s::text, '{}'::jsonb) || %(patch)s::jsonb END),
       updated_at = NOW()
 WHERE id = %(id)s AND mode = %(mode)s
RETURNING metadata->'reel_choices' AS reel_choices
"""


def _exps_sql() -> str:
    from . import rebrand_store

    return f"SELECT {rebrand_store._COLS} FROM yas_rebrand_experiments WHERE id = ANY(%s)"


class Store(Protocol):
    async def run(self, run_id: int) -> Optional[dict]: ...
    async def anims(self, ids: Sequence[int]) -> dict[int, dict]: ...
    async def patch(self, run_id: int, patch: dict) -> None: ...
    async def finalize(self, run_id: int, patch: dict, statuses: Sequence[str], seq: int) -> bool: ...
    async def set_choice(self, run_id: int, target: str, src: str, patch: dict) -> Optional[dict]: ...
    async def experiments(self, ids: Sequence[int]) -> dict[int, dict]: ...
    async def interrupt_if_orphaned(self, exp: dict, live_ids: set[int]) -> dict: ...


def _md(v: Any) -> dict:
    if isinstance(v, str):
        try:
            v = json.loads(v)
        except ValueError:
            v = {}
    return v if isinstance(v, dict) else {}


class PgStore:
    """The shared database. Reads are autocommit (nothing is held open while
    Python works on the rows)."""

    async def run(self, run_id: int) -> Optional[dict]:
        async with get_conn(autocommit=True) as conn:
            async with conn.cursor() as cur:
                await cur.execute(RUN_SQL, (int(run_id), MODE_PREVIEW))
                row = await cur.fetchone()
        if not row:
            return None
        row = dict(row)
        row["metadata"] = _md(row.get("metadata"))
        return row

    async def anims(self, ids: Sequence[int]) -> dict[int, dict]:
        ids = sorted({int(i) for i in ids if i})
        if not ids:
            return {}
        async with get_conn(autocommit=True) as conn:
            async with conn.cursor() as cur:
                await cur.execute(ANIMS_SQL, (ids,))
                rows = await cur.fetchall()
        return {int(r["id"]): dict(r) for r in rows}

    async def patch(self, run_id: int, patch: dict) -> None:
        async with get_conn() as conn:
            async with conn.cursor() as cur:
                await cur.execute(PATCH_SQL, (Jsonb(patch), int(run_id)))

    async def finalize(self, run_id: int, patch: dict, statuses: Sequence[str], seq: int) -> bool:
        async with get_conn() as conn:
            async with conn.cursor() as cur:
                await cur.execute(FINALIZE_SQL, (Jsonb(patch), int(run_id), int(seq), list(statuses)))
                return (await cur.fetchone()) is not None

    async def set_choice(self, run_id: int, target: str, src: str, patch: dict) -> Optional[dict]:
        async with get_conn() as conn:
            async with conn.cursor() as cur:
                await cur.execute(CHOICE_SQL, {"t": target, "src": src, "patch": Jsonb(patch),
                                               "id": int(run_id), "mode": MODE_PREVIEW})
                row = await cur.fetchone()
        return _md(row.get("reel_choices")) if row else None

    async def experiments(self, ids: Sequence[int]) -> dict[int, dict]:
        from . import rebrand_store

        ids = sorted({int(i) for i in ids if i})
        if not ids:
            return {}
        await rebrand_store.ensure_table()
        async with get_conn(autocommit=True) as conn:
            async with conn.cursor() as cur:
                await cur.execute(_exps_sql(), (ids,))
                rows = await cur.fetchall()
        return {int(r["id"]): rebrand_store._row(r) for r in rows}

    async def interrupt_if_orphaned(self, exp: dict, live_ids: set[int]) -> dict:
        from . import rebrand_store

        return await rebrand_store.mark_interrupted_if_orphaned(exp, live_ids=live_ids)


def _utc(v: Any) -> Optional[datetime]:
    if isinstance(v, datetime):
        return v if v.tzinfo else v.replace(tzinfo=timezone.utc)
    if isinstance(v, str) and v:
        try:
            d = datetime.fromisoformat(v)
        except ValueError:
            return None
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    return None


class MemoryStore:
    """The same statements applied to rows in memory: tests, and anything
    that must not reach the database. `rows` are {id: {id, metadata, ...}}
    (TH v2 runs), `anim_rows` {id: yas_animations row}, `exps` {id:
    yas_rebrand_experiments row}."""

    def __init__(self, rows: Optional[dict] = None, anim_rows: Optional[dict] = None,
                 exps: Optional[dict] = None) -> None:
        self.rows: dict[int, dict] = {int(k): v for k, v in (rows or {}).items()}
        self.anim_rows: dict[int, dict] = {int(k): v for k, v in (anim_rows or {}).items()}
        self.exps: dict[int, dict] = {int(k): v for k, v in (exps or {}).items()}
        self.writes: list[tuple[str, int, Any]] = []

    async def set_choice(self, run_id: int, target: str, src: str, patch: dict) -> Optional[dict]:
        self.writes.append(("choice", int(run_id), {"target": target, "src": src, **copy.deepcopy(patch)}))
        row = self.rows.get(int(run_id))
        if row is None:
            return None
        md = row.get("metadata") or {}
        choices = dict(md.get("reel_choices") or {})
        cur = choices.get(target)
        new = copy.deepcopy(patch)
        if isinstance(cur, dict) and cur.get("src") == src:
            new = {**cur, **new}
        choices[target] = new
        row["metadata"] = {**md, "reel_choices": choices}
        return copy.deepcopy(choices)

    async def experiments(self, ids: Sequence[int]) -> dict[int, dict]:
        return {int(i): copy.deepcopy(self.exps[int(i)]) for i in ids if i and int(i) in self.exps}

    async def interrupt_if_orphaned(self, exp: dict, live_ids: set[int]) -> dict:
        from . import rebrand_store

        if not rebrand_store.is_orphaned(exp, live_ids=live_ids):
            return exp
        row = self.exps.get(int(exp["id"]))
        if row is not None:
            row.update(status=rebrand_store.ST_INTERRUPTED, error=rebrand_store.ORPHAN_ERROR,
                       updated_at=datetime.now(timezone.utc))
            return copy.deepcopy(row)
        return {**exp, "status": rebrand_store.ST_INTERRUPTED, "error": rebrand_store.ORPHAN_ERROR}

    async def run(self, run_id: int) -> Optional[dict]:
        row = self.rows.get(int(run_id))
        return copy.deepcopy(row) if row is not None else None

    async def anims(self, ids: Sequence[int]) -> dict[int, dict]:
        return {int(i): copy.deepcopy(self.anim_rows[int(i)]) for i in ids if i and int(i) in self.anim_rows}

    async def patch(self, run_id: int, patch: dict) -> None:
        self.writes.append(("patch", int(run_id), copy.deepcopy(patch)))
        row = self.rows.get(int(run_id))
        if row is not None:
            row["metadata"] = {**(row.get("metadata") or {}), **copy.deepcopy(patch)}

    async def finalize(self, run_id: int, patch: dict, statuses: Sequence[str], seq: int) -> bool:
        self.writes.append(("finalize", int(run_id), copy.deepcopy(patch)))
        row = self.rows.get(int(run_id))
        if row is None:
            return False
        md = row.get("metadata") or {}
        if int(md.get("composition_seq") or 0) != int(seq):
            return False
        status = md.get("composition_status")
        started = _utc(md.get("render_started_at"))
        stale = (status == COMP_RENDERING and "render_owner" in md and started is not None
                 and started < datetime.now(timezone.utc) - timedelta(minutes=STALE_OWNER_MIN))
        if status not in statuses and not stale:
            return False
        md = {k: v for k, v in md.items() if k not in ("composition_error", "render_failed_seq")}
        row["metadata"] = {**md, **copy.deepcopy(patch)}
        return True


_store: Store = PgStore()


def store() -> Store:
    return _store


def set_store(new: Store) -> Store:
    """Swap where the reel reads and writes (tests); returns the previous."""
    global _store
    old, _store = _store, new
    return old


# ---------------------------------------------------------------------------
# What goes in: the clips a Finalize waits for, and their staleness
# ---------------------------------------------------------------------------

def is_multi(md: dict) -> bool:
    return int(md.get("scene_count") or 1) > 1


def included_anim_ids(md: dict) -> list[int]:
    """The Veo clips an included segment of the reel needs (the set the
    poller waits on before videos_ready): each included scene's, and the
    bookends' unless excluded; a legacy single-clip run's one clip."""
    ids: list[int] = []
    intro_out, outro_out = bool(md.get("intro_excluded")), bool(md.get("outro_excluded"))
    if is_multi(md):
        for sm in md.get("scenes") or []:
            if sm.get("excluded"):
                continue
            if sm.get("animation_id"):
                ids.append(int(sm["animation_id"]))
    elif md.get("main_animation_id"):
        ids.append(int(md["main_animation_id"]))
    if not intro_out and md.get("intro_animation_id"):
        ids.append(int(md["intro_animation_id"]))
    if not outro_out and md.get("outro_animation_id"):
        ids.append(int(md["outro_animation_id"]))
    if not ids and md.get("veo_animation_id"):
        ids.append(int(md["veo_animation_id"]))
    return ids


def uploaded_bookend(md: dict, role: str) -> bool:
    """Whether the intro/outro `role` is an uploaded video. The reel plays
    it as it is, so it has no clip that could go out of date."""
    return bool((md.get("refs") or {}).get(f"{role}_video_upload_id"))


def bookend_stale(md: dict, role: str) -> bool:
    """Whether the intro/outro `role` has a clip marked out of date. An
    uploaded video never does: no clip is made for it, so nothing could
    clear the mark."""
    return bool(md.get(f"{role}_video_stale")) and not uploaded_bookend(md, role)


def stale_offenders(md: dict) -> list[str]:
    """Included clips whose video no longer matches their still, words or
    refs (they must be made again before the reel is), by name."""
    out: list[str] = []
    if is_multi(md):
        for s in md.get("scenes") or []:
            if not s.get("excluded") and s.get("video_stale"):
                out.append(f"Scene {int(s.get('index', 0)) + 1}")
    elif md.get("main_video_stale"):
        out.append("Main")
    if not md.get("intro_excluded") and bookend_stale(md, "intro"):
        out.append("Intro")
    if not md.get("outro_excluded") and bookend_stale(md, "outro"):
        out.append("Outro")
    return out


def _has_main(md: dict) -> bool:
    if is_multi(md):
        included = [s for s in md.get("scenes") or [] if not s.get("excluded")]
        return bool(included) and all(s.get("animation_id") for s in included)
    return bool(md.get("main_animation_id") or md.get("veo_animation_id"))


def reel_ready(md: dict, statuses: dict[int, Any]) -> bool:
    """Every included clip made (its yas_animations status `ready`, from
    `statuses` {id: status}), none out of date, and a main clip to build on:
    the reel can be finalized."""
    ids = included_anim_ids(md)
    if not ids or not _has_main(md) or stale_offenders(md):
        return False
    return all(statuses.get(int(i)) == ANIM_READY for i in ids)


# ---------------------------------------------------------------------------
# The reel's targets and their sources (today's per-template priorities)
# ---------------------------------------------------------------------------

@dataclass
class Target:
    """One segment of the reel. `src` names what the stitch uses:
    anim:<id> | card:<image id> | upload:<id> | asset:<id>, or None when
    nothing is found for it — a main scene's is always its clip's (`path`
    None: not on this computer), a bookend's is whichever source is found.
    `seconds` is what today's stitch sends for it (int or float, exactly
    as it would)."""
    target: str                      # intro | scene_<i> | outro
    role: str                        # intro | main | outro
    label: str                       # Intro | Main | Main <n> | Outro
    src: Optional[str] = None
    path: Optional[Path] = None
    still: bool = False
    seconds: Any = None
    anim_id: Optional[int] = None    # the Veo clip this segment names (a bookend's, even when its card is used)
    excluded: bool = False


def all_targets(md: dict) -> list[Target]:
    """Every segment the reel can have, in reel order, included or not
    (sources not looked up)."""
    out = [Target("intro", "intro", "Intro", excluded=bool(md.get("intro_excluded")),
                  anim_id=int(md["intro_animation_id"]) if md.get("intro_animation_id") else None)]
    if is_multi(md):
        for i, sm in enumerate(md.get("scenes") or []):
            out.append(Target(f"scene_{i}", "main", f"Main {i + 1}", excluded=bool(sm.get("excluded")),
                              anim_id=int(sm["animation_id"]) if sm.get("animation_id") else None))
    else:
        aid = md.get("main_animation_id") or md.get("veo_animation_id")
        out.append(Target("scene_0", "main", "Main", anim_id=int(aid) if aid else None))
    out.append(Target("outro", "outro", "Outro", excluded=bool(md.get("outro_excluded")),
                      anim_id=int(md["outro_animation_id"]) if md.get("outro_animation_id") else None))
    return out


def _abs(rel_or_abs: str) -> Path:
    """A stored path on this disk (the poller's rule: relative to backend/data)."""
    return poller._abs_from_rel(rel_or_abs)


def anim_dir() -> Path:
    """backend/data/yas_producer/animations: where this computer keeps Veo clips."""
    return poller.config.asset_root.parent / "yas_producer" / "animations"


def cache_dir() -> Path:
    """The rebrand pipeline's clip cache (REBRAND_ROOT/_cache): a clip fetched
    from the bucket lands here as anim-<id>.mp4."""
    return poller.COMP_DIR / "rebrand" / "_cache"


def locate_anim(anim_id: int, row: Optional[dict], *, cache: Optional[Path] = None) -> Optional[Path]:
    """Where a ready Veo clip is on this disk, the one lookup the stitch, the
    strip and the studio share: its own local_path; else the same file name
    under animations/ (a row made on another machine carries its path — a
    teammate's /Users/apple/…, Docker's /app/…, which on Windows resolve
    under C:\\ and don't exist, while the file sits in animations/); else
    anim-<id>.mp4 there; else the rebrand cache's anim-<id>.mp4 (a copy the
    studio fetched from the bucket). None: not on this computer."""
    if not row or row.get("status") != ANIM_READY:
        return None
    tries: list[Path] = []
    lp = row.get("local_path")
    if lp:
        tries.append(_abs(lp))
        name = str(lp).replace("\\", "/").rsplit("/", 1)[-1]
        if name:
            tries.append(anim_dir() / name)
    tries += [anim_dir() / f"anim-{int(anim_id)}.mp4", (cache or cache_dir()) / f"anim-{int(anim_id)}.mp4"]
    return next((p for p in tries if p.is_file()), None)


async def fetch_anim(anim_id: int, row: Optional[dict], *, cache: Optional[Path] = None) -> Optional[Path]:
    """locate_anim, then the bucket: a clip on no disk here is downloaded
    into the rebrand cache (async callers only — preparing a scene and
    Finalize — never a page read). None when it can't be had: a download
    that fails (the bucket's answer, the network) is logged and treated as
    not on this computer, so the caller's 409 names the scene."""
    p = locate_anim(anim_id, row, cache=cache)
    if p is not None or not row or row.get("status") != ANIM_READY:
        return p
    if not (row.get("storage_bucket") and row.get("storage_path")):
        return None
    from . import asset_storage

    dest = (cache or cache_dir()) / f"anim-{int(anim_id)}.mp4"
    tmp = dest.with_name(f"{dest.name}.part")
    try:
        # Fetched by this server, so the internal URL (no trip out and back in
        # through the public domain).
        signed = await asset_storage.url_for(storage_bucket=row["storage_bucket"],
                                             storage_path=row["storage_path"], local_path=None,
                                             expires=900, internal=True)
        if not signed:
            return None
        dest.parent.mkdir(parents=True, exist_ok=True)
        if not dest.exists():
            import httpx

            async with httpx.AsyncClient(timeout=180.0, follow_redirects=True) as c:
                r = await c.get(signed)
                r.raise_for_status()
                tmp.write_bytes(r.content)
                tmp.replace(dest)
    except Exception as e:  # noqa: BLE001 — not on this computer, as far as the reel is concerned
        log.warning("[th-reel] clip #%s couldn't be fetched from the bucket: %s",
                    anim_id, str(e)[:200] or type(e).__name__)
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        return None
    return dest


@dataclass(frozen=True)
class Facts:
    """A source clip as decoded: its frames, its own rate, and the first
    frame of a Veo tail flash (rebrand_media.tail_cut; None: it ends on its
    own shot)."""
    frames: int
    fps: float
    cut: Optional[int]

    @property
    def full(self) -> int:
        """Its length in the reel's 30 fps frames."""
        return int(math.floor(self.frames * FPS / self.fps + 1e-9)) if self.fps > 0 else self.frames

    @property
    def auto_end(self) -> Optional[int]:
        """Where the reel cuts it automatically (30 fps frames): before the
        flash, so the flash is never sampled (Remotion shows the nearest
        source frame: output 234 of a 24 fps clip cut at 188 shows 187)."""
        if self.cut is None or self.fps <= 0:
            return None
        return int(math.floor(self.cut * FPS / self.fps + 1e-9))


# Each clip's Facts by (path, size, mtime): a page polling the reel reads
# them again every few seconds.
_FACTS: dict[tuple, Facts] = {}
FACTS_KEPT = 128


def clip_facts(path: Path) -> Facts:
    """A clip's Facts (synchronous: it decodes the clip once; remembered by
    path, size and mtime)."""
    from . import rebrand_media as media

    p = Path(path)
    try:
        st = os.stat(p)
        memo: Optional[tuple] = (str(p.resolve()), st.st_size, st.st_mtime_ns)
    except OSError:
        memo = None
    if memo is not None and memo in _FACTS:
        return _FACTS[memo]
    found = Facts(frames=len(media._frame_diffs(p)), fps=float(media.video_fps(p) or FPS), cut=media.tail_cut(p))
    if memo is not None:
        _FACTS[memo] = found
        while len(_FACTS) > FACTS_KEPT:
            _FACTS.pop(next(iter(_FACTS)), None)
    return found


# An uploaded bookend's probed seconds by (path, size, mtime), as _FACTS:
# a single-scene reel with one is probed on every read of the reel.
_PROBES: dict[tuple, float] = {}


def probe_seconds(path: Path) -> Optional[float]:
    """A video's seconds as today's stitch probes them (synchronous: it runs
    ffmpeg; remembered by path, size and mtime once found)."""
    p = Path(path)
    try:
        st = os.stat(p)
        memo: Optional[tuple] = (str(p.resolve()), st.st_size, st.st_mtime_ns)
    except OSError:
        memo = None
    if memo is not None and memo in _PROBES:
        return _PROBES[memo]
    found = poller._probe_video_duration(p)
    if memo is not None and found is not None:
        _PROBES[memo] = found
        while len(_PROBES) > FACTS_KEPT:
            _PROBES.pop(next(iter(_PROBES)), None)
    return found


class Io(Protocol):
    """Everything resolving a reel looks up."""
    async def anim(self, anim_id: int) -> Optional[dict]: ...
    async def card(self, card_id: int) -> Optional[Path]: ...
    async def upload(self, upload_id: int) -> Optional[Path]: ...
    async def asset(self, asset_id: int) -> Optional[Path]: ...
    async def bucket_anim(self, anim_id: int, row: Optional[dict]) -> Optional[Path]: ...
    def probe(self, path: Path) -> Optional[float]: ...
    def facts(self, path: Path) -> Facts: ...


class DbIo:
    """The lookups as the poller makes them, against the database and this
    disk (anim rows remembered for the call)."""

    def __init__(self, anims: Optional[dict[int, dict]] = None,
                 cards: Optional[dict[int, Optional[Path]]] = None) -> None:
        self._anims: dict[int, Optional[dict]] = {int(k): v for k, v in (anims or {}).items()}
        self._cards: dict[int, Optional[Path]] = {int(k): v for k, v in (cards or {}).items()}

    async def anim(self, anim_id: int) -> Optional[dict]:
        anim_id = int(anim_id)
        if anim_id not in self._anims:
            self._anims[anim_id] = (await store().anims([anim_id])).get(anim_id)
        return self._anims[anim_id]

    async def card(self, card_id: int) -> Optional[Path]:
        if int(card_id) in self._cards:
            return self._cards[int(card_id)]
        return await poller._fetch_card_path(int(card_id))

    async def upload(self, upload_id: int) -> Optional[Path]:
        return await poller._fetch_upload_path(int(upload_id))

    async def asset(self, asset_id: int) -> Optional[Path]:
        return await poller._fetch_asset_path(int(asset_id))

    async def bucket_anim(self, anim_id: int, row: Optional[dict]) -> Optional[Path]:
        return await fetch_anim(int(anim_id), row)

    def probe(self, path: Path) -> Optional[float]:
        return probe_seconds(Path(path))

    def facts(self, path: Path) -> Facts:
        return clip_facts(Path(path))


def _dur(row: Optional[dict], default: int) -> int:
    """A clip's seconds as today's stitch reads them: its duration_sec as
    an int, else the default."""
    return int(row["duration_sec"]) if row and row.get("duration_sec") else default


async def _veo(io: Io, anim_id: int, row: Optional[dict], remote: bool) -> Optional[Path]:
    p = locate_anim(int(anim_id), row)
    if p is None and remote:
        p = await io.bucket_anim(int(anim_id), row)
    return p


async def _bookend(io: Io, md: dict, t: Target, *, uploads: bool, remote: bool) -> Target:
    """A bookend's source, in today's order: an uploaded video as it is (a
    multi-scene reel too); its Veo clip; its card; the reference asset.
    Its seconds are the Veo row's (4 without one), or an upload's probed
    length."""
    refs = md.get("refs") or {}
    role = t.role
    row = await io.anim(t.anim_id) if t.anim_id else None
    t.seconds = _dur(row, BOOKEND_DEFAULT_SEC)
    if t.excluded:
        return t
    if uploads and refs.get(f"{role}_video_upload_id"):
        uid = int(refs[f"{role}_video_upload_id"])
        p = await io.upload(uid)
        if p is not None:
            t.src, t.path = f"upload:{uid}", p
            t.seconds = (await asyncio.to_thread(io.probe, p)) or t.seconds
            return t
    if t.anim_id:
        p = await _veo(io, t.anim_id, row, remote)
        if p is not None:
            t.src, t.path = f"anim:{t.anim_id}", p
            return t
    if md.get(f"{role}_image_id"):
        cid = int(md[f"{role}_image_id"])
        p = await io.card(cid)
        if p is not None:
            t.src, t.path, t.still = f"card:{cid}", p, True
            return t
    if refs.get(f"{role}_asset_id"):
        aid = int(refs[f"{role}_asset_id"])
        p = await io.asset(aid)
        if p is not None:
            t.src, t.path = f"asset:{aid}", p
            t.still = p.suffix.lower() in (".png", ".jpg", ".jpeg", ".webp")
    return t


async def resolve_sources(run: dict, io: Optional[Io] = None, *, remote: bool = False) -> list[Target]:
    """Every segment of the reel in order — the included ones with their
    source (src/path; path None when nothing is on this computer for it, and
    a bookend's src too: its source is the one found) — and,
    for each bookend, the seconds today's stitch sends for it even when it
    is excluded. Mirrors today's priorities: a bookend is an uploaded video,
    else its Veo clip, else its card, else the reference asset (a multi-scene
    reel plays an uploaded video too, at its probed length); a single main
    is main_animation_id or the legacy veo_animation_id.
    A Veo clip is found by locate_anim (and, `remote`, fetched from the
    bucket): a clip today's stitch missed on a foreign path is used."""
    io = io or DbIo()
    md = run.get("metadata") or {}
    return [await _resolve_one(io, md, t, remote=remote) for t in all_targets(md)]


async def _resolve_one(io: Io, md: dict, t: Target, *, remote: bool) -> Target:
    if t.role in ("intro", "outro"):
        return await _bookend(io, md, t, uploads=True, remote=remote)
    if t.excluded or not t.anim_id:
        return t
    row = await io.anim(t.anim_id)
    t.seconds = _dur(row, MAIN_DEFAULT_SEC)
    # A main scene is its clip whether or not the file is here: only `path`
    # says where it is. (A clip the studio fetched from the bucket must not
    # change the reel's inputs, or every Finalize after it is a 409.)
    t.src = f"anim:{t.anim_id}"
    p = await _veo(io, t.anim_id, row, remote)
    if p is not None:
        t.path = p
    return t


async def resolve_target(run: dict, target: str, io: Optional[Io] = None, *,
                         remote: bool = False) -> Optional[Target]:
    """One segment of the reel with its source, exactly as resolve_sources
    finds it (the studio's intro or outro is the very clip the stitch
    uses), without looking the others up; None when the reel has no such
    segment."""
    md = run.get("metadata") or {}
    t = next((x for x in all_targets(md) if x.target == target), None)
    return await _resolve_one(io or DbIo(), md, t, remote=remote) if t is not None else None


def included(targets: list[Target]) -> list[Target]:
    return [t for t in targets if not t.excluded]


# ---------------------------------------------------------------------------
# The inputs of a reel, as one hash
# ---------------------------------------------------------------------------

def template_of(md: dict) -> str:
    return TEMPLATE_MULTI if is_multi(md) else TEMPLATE_SINGLE


def aspect_of(md: dict) -> str:
    return "16:9" if md.get("aspect_ratio") == "16:9" else "9:16"


def inputs(md: dict, targets: list[Target], choices: Optional[dict[str, "Choice"]] = None) -> dict:
    """What the reel is made of, canonically: each segment's target, source,
    whether it is excluded, the version that goes in (None: the original)
    and the end the user set (None: automatic), plus the aspect and the
    template. An excluded segment is only that (its pick and end don't
    count)."""
    rows = []
    for t in targets:
        c = (choices or {}).get(t.target)
        if t.excluded:
            rows.append([t.target, None, True, None, None])
        else:
            rows.append([t.target, t.src, False, c.ships if c else None, c.user_end if c else None])
    return {"segments": rows, "aspect": aspect_of(md), "template": template_of(md)}


def inputs_sha(body: dict) -> str:
    raw = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------------------
# A pick and an end per scene
# ---------------------------------------------------------------------------

OLDER_CLIP = "Made on an older clip."
NOT_READY_VERSION = "That version isn't ready yet."
NOT_THIS_SCENE = "That version isn't one of this scene's."
# Generate never waits on setting its version on the way into the reel
# longer than this, and never fails for it.
PENDING_TIMEOUT_SEC = 3.0
# A version's states, as the reel sees it.
V_READY, V_RUNNING, V_FAILED, V_LOST, V_OTHER = "ready", "running", "failed", "lost", "other_clip"


def entry_of(md: dict, t: Target) -> dict:
    """The scene's stored choice, only while it is for the scene's clip now
    (a Veo retry makes a new clip: the choices made on the old one void)."""
    e = (md.get("reel_choices") or {}).get(t.target)
    return e if isinstance(e, dict) and t.src is not None and e.get("src") == t.src else {}


def _int(v: Any) -> Optional[int]:
    try:
        return int(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def version_file(exp: dict) -> Optional[Path]:
    """Where a version's video is on this computer, whether or not it is
    there now (made in house: its render; by Omni: its arm's final)."""
    from .rebrand_harness import base as hbase

    if hbase.made_in_house(exp):
        return hbase.final_file(exp)
    rel = ((exp.get("arms") or {}).get("omni") or {}).get("final_path")
    return _abs(rel) if rel else None


def made_for(exp: dict, t: Target, *, run_id: int, first: Optional[Path] = None) -> bool:
    """Whether a version was made of this very scene's clip as it is now:
    its options.source names the scene's src — an older version (none
    recorded) is judged by its first frame (base.clip_changed; unknown:
    yes). A bookend takes only a version made in house (an Omni-era card
    has the transition baked in)."""
    from .rebrand_harness import base as hbase

    opts = exp.get("options") or {}
    if _int(exp.get("run_id")) != int(run_id) or (opts.get("target") or "reel") != t.target:
        return False
    if t.role != "main" and not hbase.made_in_house(exp):
        return False
    source = opts.get("source")
    if isinstance(source, dict) and source.get("src"):
        return source["src"] == t.src
    return not (first is not None and hbase.clip_changed(exp, first))


def version_state(exp: Optional[dict], t: Target, *, run_id: int, first: Optional[Path] = None) -> str:
    """running (being made), failed (failed, interrupted, cancelled or
    gone), other_clip (made on another clip), lost (its video isn't on
    this computer) or ready."""
    from . import rebrand_store
    from .rebrand_harness import base as hbase

    if exp is None:
        return V_FAILED
    if exp.get("status") in rebrand_store.ACTIVE_STATUSES:
        return V_RUNNING
    if not hbase.is_ready(exp):
        return V_FAILED
    if not made_for(exp, t, run_id=run_id, first=first):
        return V_OTHER
    f = version_file(exp)
    return V_READY if f is not None and f.is_file() else V_LOST


@dataclass
class Choice:
    """What one scene puts in the reel. `ships`: the version that goes in
    (None: the original) — the pending one once ready, else the pick, each
    only if made of this clip; `pick`: the newest choice and its state (the
    pending version, else the pick); `making`: a version still being made."""
    target: str
    user_end: Optional[int] = None
    ships: Optional[int] = None
    ships_exp: Optional[dict] = None
    pick: Optional[dict] = None
    making: Optional[int] = None


def _live_ids() -> set[int]:
    from . import rebrand_pipeline

    return rebrand_pipeline.live_ids()


def _first_frame(run_id: int, t: Target) -> Optional[Path]:
    """The first frame the studio took of this scene's clip (to judge an
    older version by), when it has prepared it; None: unknown."""
    try:
        from .rebrand_harness import studio

        f = studio.scene_base(int(run_id), t.target, t.path) / "c0_first.png"
    except Exception:  # noqa: BLE001 — unknown: the version is taken as made of this clip
        return None
    return f if f.is_file() else None


async def evaluate(run: dict, targets: list[Target]) -> dict[str, Choice]:
    """Every scene's Choice: one read of the versions named, a pending one
    whose task is gone interrupted at once (rebrand_store.is_orphaned)."""
    md = run.get("metadata") or {}
    run_id = int(run["id"])
    entries = {t.target: entry_of(md, t) for t in targets}
    ids = sorted({i for e in entries.values() for i in (_int(e.get("pick")), _int(e.get("pending"))) if i})
    exps = await store().experiments(ids) if ids else {}
    pending_ids = {_int(e.get("pending")) for e in entries.values()} - {None}
    if pending_ids & set(exps):
        live = _live_ids()
        for i in pending_ids & set(exps):
            exps[i] = await store().interrupt_if_orphaned(exps[i], live)
    out: dict[str, Choice] = {}
    for t in targets:
        e = entries[t.target]
        c = Choice(t.target, user_end=_int(e.get("end_frame")))
        named = [i for i in (_int(e.get("pending")), _int(e.get("pick"))) if i]
        first = None
        if any(i in exps and not (exps[i].get("options") or {}).get("source") for i in named) and t.path:
            first = await asyncio.to_thread(_first_frame, run_id, t)
        states = {i: version_state(exps.get(i), t, run_id=run_id, first=first) for i in named}
        pend, pick = _int(e.get("pending")), _int(e.get("pick"))
        for i in (pend, pick):
            if i and states[i] in (V_READY, V_LOST):
                c.ships, c.ships_exp = i, exps.get(i)
                break
        newest = pend or pick
        if newest:
            c.pick = {"version_id": newest, "state": states[newest]}
        if pend and states[pend] == V_RUNNING:
            c.making = pend
        out[t.target] = c
    return out


async def _facts_of(t: Target, io: Io) -> Optional[Facts]:
    """A video segment's Facts (decoded once, in a thread); None for a
    still, or nothing found."""
    if t.path is None or t.still or not t.src or t.src.startswith("card:"):
        return None
    return await asyncio.to_thread(io.facts, t.path)


def _full(t: Target, facts: Optional[Facts]) -> int:
    """The segment's whole length in the reel's frames: a clip's own, a
    still's (or an upload's) as today's stitch holds it."""
    if facts is not None and facts.frames > 0:
        return facts.full
    return js_round(float(t.seconds or 0) * FPS)


def _auto(t: Target, facts: Optional[Facts]) -> Optional[int]:
    """The automatic end: a main Veo scene's tail flash only (0 of 75 four-
    second clips flash, and the detector false-cuts on an animated card)."""
    return facts.auto_end if facts is not None and _is_main_veo(t) else None


def _thumb(run_id: int, md: dict, t: Target, c: Optional[Choice]) -> Optional[str]:
    """The strip's picture: the shipped version's first still, else the
    scene's keyframe (its clip's id names it: a new still makes a new clip),
    else the bookend's card."""
    if c is not None and c.ships and c.ships_exp and c.ships_exp.get("status") == "ready":
        return f"/api/rebrand/harness/versions/{c.ships}/still/0"
    base = "/api/yas-producer/talking-head-v2"
    if t.role == "main":
        i = int(t.target.split("_", 1)[1])
        v = f"?v=a{t.anim_id}" if t.anim_id else ""
        if i == 0:
            return f"{base}/{run_id}/file{v}"
        kid = ((md.get("scenes") or [])[i] if i < len(md.get("scenes") or []) else {}).get("keyframe_id")
        return f"{base}/scenes/{kid}/file{v}" if kid else None
    cid = md.get(f"{t.role}_image_id")
    return f"{base}/cards/{cid}/file" if cid else None


@dataclass
class ReelState:
    """The reel as it stands: its segments with their sources, each scene's
    Choice, the inputs' hash, and whether it can be finalized."""
    run: dict
    targets: list[Target]
    choices: dict[str, Choice]
    sha: str
    ready: bool
    io: Any = None

    def any_choice(self) -> bool:
        """A version or an end chosen on an included scene of this clip."""
        return any(not t.excluded and (self.choices[t.target].ships or self.choices[t.target].user_end)
                   for t in self.targets)

    async def block(self) -> dict:
        """The reel block (fetch_run.reel, GET/PUT …/reel): each included
        segment the reel has — every included main scene, a bookend when
        something is found for it — in reel order, and the inputs' hash
        (Finalize's expect_sha)."""
        md = self.run.get("metadata") or {}
        rows = []
        shown = [t for t in included(self.targets) if t.role == "main" or t.src]
        facts = await asyncio.gather(*(_facts_of(t, self.io or DbIo()) for t in shown))
        for t, f in zip(shown, facts):
            c = self.choices.get(t.target) or Choice(t.target)
            full, auto = _full(t, f), _auto(t, f)
            rows.append({
                "target": t.target, "role": t.role, "label": t.label, "src": t.src,
                "thumb_url": _thumb(int(self.run["id"]), md, t, c),
                "pick": c.pick, "ships": c.ships, "making": c.making,
                "end": {"frame": c.user_end or auto or full, "auto": auto, "user": c.user_end, "full": full},
            })
        # `bookends`: this server's studio opens an intro or outro (the
        # page shows their tiles only then).
        return {"ready": self.ready, "targets": rows, "inputs_sha": self.sha, "bookends": True}


async def reel_state(run: dict, io: Optional[Io] = None, *, statuses: Optional[dict[int, Any]] = None,
                     remote: bool = False) -> ReelState:
    """The reel as it stands now (local lookups unless `remote`)."""
    io = io or DbIo()
    md = run.get("metadata") or {}
    targets = await resolve_sources(run, io, remote=remote)
    choices = await evaluate(run, targets)
    if statuses is None:
        statuses = await statuses_of(md)
    return ReelState(run=run, targets=targets, choices=choices, sha=inputs_sha(inputs(md, targets, choices)),
                     ready=reel_ready(md, statuses), io=io)


def all_anim_ids(md: dict) -> list[int]:
    return [t.anim_id for t in all_targets(md) if t.anim_id]


async def reel_of(run_id: int) -> dict:
    """GET …/reel: the reel block, whatever the reel's readiness (`ready`)."""
    run = await store().run(run_id)
    if run is None:
        raise HTTPException(404, f"talking-head-v2 run #{run_id} not found")
    md = run.get("metadata") or {}
    anims = await store().anims(all_anim_ids(md))
    state = await reel_state(run, DbIo(anims), statuses={i: r.get("status") for i, r in anims.items()})
    return await state.block()


async def put_choice(run_id: int, target: str, fields: dict) -> dict:
    """PUT …/reel/{target} {pick?, end_frame?} (partial: a key left out is
    kept). A pick is a version of this scene, ready and made of its clip
    now (else 409 "Made on an older clip."), or None: the original; it
    clears a pending version. An end is 30 frames up to the clip's whole
    length, or None: automatic. The reel block back (with inputs_sha)."""
    run = await store().run(run_id)
    if run is None:
        raise HTTPException(404, f"talking-head-v2 run #{run_id} not found")
    md = run.get("metadata") or {}
    anims = await store().anims(all_anim_ids(md))
    io = DbIo(anims)
    targets = await resolve_sources(run, io)
    t = next((x for x in targets if x.target == target), None)
    if t is None:
        raise HTTPException(404, f"{target} isn't part of this reel.")
    if t.src is None or t.path is None:
        raise HTTPException(409, f"{t.label}'s clip isn't on this computer.")
    patch: dict = {"src": t.src, "at": _now_iso()}
    if "pick" in fields:
        vid = _int(fields["pick"])
        if vid is not None:
            exp = (await store().experiments([vid])).get(vid)
            opts = (exp or {}).get("options") or {}
            if exp is None or _int(exp.get("run_id")) != int(run_id) or (opts.get("target") or "reel") != target:
                raise HTTPException(404, NOT_THIS_SCENE)
            first = None
            if not opts.get("source"):
                first = await asyncio.to_thread(_first_frame, int(run_id), t)
            state = version_state(exp, t, run_id=int(run_id), first=first)
            if state == V_OTHER:
                raise HTTPException(409, OLDER_CLIP)
            if state not in (V_READY, V_LOST):
                raise HTTPException(409, NOT_READY_VERSION)
        patch.update(pick=vid, pending=None)
    if "end_frame" in fields:
        end = _int(fields["end_frame"])
        if end is not None:
            full = _full(t, await _facts_of(t, io))
            if not MIN_FRAMES <= end <= full:
                raise HTTPException(422, f"The end must be between {MIN_FRAMES} and {full} frames.")
        patch["end_frame"] = end
    if len(patch) > 2:
        await store().set_choice(int(run_id), target, t.src, patch)
    return await reel_of(run_id)


def role_of(target: str) -> str:
    return target if target in ("intro", "outro") else "main"


async def version_options(run: dict, target: str, *, end_frame: Optional[int] = None) -> dict:
    """What pipeline.start stores beside a studio version's recipe (JSON
    only: recipe_hash does not move): the clip it is made of ({src,
    digest10} — the reel checks a picked version against the scene's clip
    now), the kind of scene, the end it is made for and the server making
    it (the orphan rule). Best-effort: without a clip found here, no
    source (the older first-frame check covers it)."""
    out: dict = {"scene_kind": role_of(target), "end_frame": end_frame, "instance": INSTANCE_ID}
    try:
        t = next((x for x in await resolve_sources(run, DbIo()) if x.target == target), None)
        if t is not None and t.src and t.path is not None:
            from .rebrand_harness import stages

            digest = await asyncio.to_thread(stages.digest, t.path)
            out = {"source": {"src": t.src, "digest10": (digest or "")[:10]}, **out}
    except Exception:  # noqa: BLE001 — the version starts all the same
        log.warning("[th-reel] run=%s %s: its clip couldn't be named", run.get("id"), target, exc_info=True)
    return out


async def after_generate(exp: dict) -> bool:
    """A version Generate just started is on its way into the reel: it takes
    the scene's `pending` (and the ✓ once ready). Awaited by the route, but
    bounded (PENDING_TIMEOUT_SEC) and never raising: Generate never fails
    for it. Whether it was set."""
    try:
        opts = exp.get("options") or {}
        source = opts.get("source") or {}
        if exp.get("id") is None or not source.get("src") or not opts.get("target"):
            return False
        patch = {"src": source["src"], "pending": int(exp["id"]), "at": _now_iso()}
        got = await asyncio.wait_for(
            store().set_choice(int(exp["run_id"]), str(opts["target"]), str(source["src"]), patch),
            PENDING_TIMEOUT_SEC)
        return got is not None
    except Exception as e:  # noqa: BLE001 — a mark on the reel, never Generate's failure
        log.warning("[th-reel] version #%s not marked as on its way into the reel: %s",
                    exp.get("id"), str(e)[:200] or type(e).__name__)
        return False


# ---------------------------------------------------------------------------
# The frozen plan: what Finalize stores and the poller posts unchanged
# ---------------------------------------------------------------------------

@dataclass
class Segment:
    target: str
    role: str
    src: str
    path: Path
    frames: int
    seconds: Any                     # what the template is sent (exactly today's when untouched)
    version_id: Optional[int] = None
    facts: Optional[Facts] = None    # the source clip as decoded (main Veo scenes; any segment chosen for)
    user_end: Optional[int] = None
    auto_end: Optional[int] = None
    full: Optional[int] = None

    def public(self) -> dict:
        body = {"target": self.target, "role": self.role, "src": self.src, "version_id": self.version_id,
                "file_rel": poller._to_rel(self.path), "frames": int(self.frames)}
        if self.facts is not None or self.user_end is not None:
            body["end"] = {"src_fps": round(self.facts.fps, 3) if self.facts else None,
                           "src_frames": self.facts.frames if self.facts else None,
                           "full": self.full, "auto": self.auto_end, "user": self.user_end}
        return body


@dataclass
class Plan:
    template: str
    segments: list[Segment]
    payload: dict
    total_frames: int
    sha: str
    extra: dict = field(default_factory=dict)

    def frozen(self, seq: int) -> dict:
        return {
            "seq": int(seq),
            "template": self.template,
            "segments": [s.public() for s in self.segments],
            "total_frames": int(self.total_frames),
            "payload": self.payload,
        }


# A segment is at least a second long (the 12-frame crossfades need 25).
MIN_FRAMES = 30


def seconds_for(frames: int) -> float:
    """The seconds that carry an exact frame count through the templates'
    own rounding: each segment's Math.round(sec * 30) gives `frames` back,
    and calculateMetadata's Math.ceil of the sum gives exactly
    sum(frames) - 12 * seams (plain frames / 30 drifts a frame in ~1 in 7
    reels; the -0.1 bias in none of 1.8M simulated, 1-9 segments)."""
    return (int(frames) - 0.1) / FPS


def js_round(x: float) -> int:
    """Math.round: halves go up."""
    return int(math.floor(x + 0.5))


def _js_or(v: Any, default: float) -> float:
    return v if v else default


def expected_frames(payload: dict) -> int:
    """The reel's length in frames as the template's calculateMetadata works
    it out (the same float maths in the same order), from the payload."""
    p = payload["props"]
    fade = max(0.0, _js_or(p.get("fade_sec"), 0))
    if payload["template"] == TEMPLATE_MULTI:
        durs = []
        if p.get("intro_video_path"):
            durs.append(max(0.1, _js_or(p.get("intro_duration_sec"), BOOKEND_DEFAULT_SEC)))
        for s in p.get("scenes") or []:
            if s and s.get("path"):
                durs.append(max(0.5, _js_or(s.get("duration_sec"), 8)))
        if p.get("outro_video_path"):
            durs.append(max(0.1, _js_or(p.get("outro_duration_sec"), BOOKEND_DEFAULT_SEC)))
        total = 0.0
        for d in durs:
            total = total + d
        total = total - max(0, len(durs) - 1) * fade
        return max(1, math.ceil(max(0.5, total) * FPS))
    intro_src = p.get("intro_video_path") or p.get("intro_image_path")
    outro_src = p.get("outro_video_path") or p.get("outro_image_path")
    intro = max(0.1, _js_or(p.get("intro_duration_sec"), BOOKEND_DEFAULT_SEC)) if intro_src else 0
    outro = max(0.1, _js_or(p.get("outro_duration_sec"), BOOKEND_DEFAULT_SEC)) if outro_src else 0
    main = max(0.5, _js_or(p.get("main_duration_sec"), MAIN_DEFAULT_SEC))
    seams = (1 if intro > 0 else 0) + (1 if outro > 0 else 0)
    total = max(0.5, intro + main + outro - seams * fade)
    return max(1, math.ceil(total * FPS))


def _url(path: Optional[Path]) -> Optional[str]:
    return poller._to_remotion_url(path) if path is not None else None


def payload(md: dict, targets: list[Target], segments: list[Segment]) -> dict:
    """The Remotion /render body (no output_path: the poller adds the file
    it renders to), key for key as today's stitch sends it."""
    by = {s.target: s for s in segments}
    intro = next((by.get(t.target) for t in targets if t.role == "intro"), None)
    outro = next((by.get(t.target) for t in targets if t.role == "outro"), None)
    t_intro = next(t for t in targets if t.role == "intro")
    t_outro = next(t for t in targets if t.role == "outro")
    intro_url = _url(intro.path) if intro else None
    outro_url = _url(outro.path) if outro else None
    comp_w, comp_h = (1920, 1080) if md.get("aspect_ratio") == "16:9" else (1080, 1920)
    intro_sec = intro.seconds if intro else t_intro.seconds
    outro_sec = outro.seconds if outro else t_outro.seconds
    mains = [s for s in segments if s.role == "main"]
    if is_multi(md):
        return {
            "template": TEMPLATE_MULTI,
            "props": {
                "intro_video_path": intro_url,
                "outro_video_path": outro_url,
                "scenes": [{"path": _url(s.path), "duration_sec": float(s.seconds)} for s in mains],
                "intro_duration_sec": float(intro_sec),
                "outro_duration_sec": float(outro_sec),
                "fade_sec": FADE_SEC,
                "width": comp_w,
                "height": comp_h,
            },
        }
    main = mains[0]
    return {
        "template": TEMPLATE_SINGLE,
        "props": {
            "intro_image_path": None,
            "intro_video_path": intro_url,
            "main_video_path": _url(main.path),
            "outro_image_path": None,
            "outro_video_path": outro_url,
            "intro_duration_sec": float(intro_sec),
            "outro_duration_sec": float(outro_sec),
            "fade_sec": FADE_SEC,
            "main_duration_sec": main.seconds,
            "width": comp_w,
            "height": comp_h,
        },
    }


def _is_main_veo(t: Target) -> bool:
    return t.role == "main" and bool(t.src and t.src.startswith("anim:"))


def count_frames(path: Path) -> int:
    """A version's frames, counted (decoded)."""
    from . import rebrand_media as media

    return media.frame_count(Path(path))


async def _segment(t: Target, io: Io, c: Optional[Choice] = None) -> Segment:
    """One segment's file and length. One end rule for every segment:
    end = the user's end, else the automatic cut (a main Veo scene's tail
    flash), else the whole clip. The original takes F = end; a picked
    version takes F = min(its frames, end) — so a version made on the uncut
    clip is cut at the flash too. A segment with no cut, no end and no pick
    is sent exactly today's seconds (a clean reel's payload is today's, byte
    for byte); otherwise its frames F go as seconds_for(F), at least
    MIN_FRAMES."""
    seg = Segment(target=t.target, role=t.role, src=t.src or "", path=t.path,
                  frames=js_round(float(t.seconds) * FPS), seconds=t.seconds)
    user_end = c.user_end if c else None
    version = c.ships_exp if c and c.ships else None
    facts = None
    if _is_main_veo(t) or user_end is not None or version is not None:
        # Bookends are decoded only when chosen for: 0 of 75 four-second
        # clips flash, and the detector false-cuts on an animated card, so
        # they are never cut automatically.
        facts = await _facts_of(t, io)
    auto = _auto(t, facts)
    seg.facts, seg.user_end, seg.auto_end = facts, user_end, auto
    seg.full = _full(t, facts) if (facts is not None or user_end is not None or version is not None) else None
    if user_end is None and auto is None and version is None:
        return seg
    end = user_end if user_end is not None else (auto if auto is not None else seg.full)
    if version is not None:
        f = version_file(version)
        if f is None or not f.is_file():
            raise HTTPException(409, f"The version picked for {t.label} isn't on this computer: open it and "
                                     "press Remake.")
        if poller._to_remotion_url(f) is None:
            raise HTTPException(409, f"The version picked for {t.label} isn't where the renderer can read it.")
        seg.path, seg.version_id = f, int(c.ships)
        end = min(await asyncio.to_thread(count_frames, f), end)
    seg.frames = max(MIN_FRAMES, int(end))
    seg.seconds = seconds_for(seg.frames)
    return seg


async def resolve(run: dict, io: Optional[Io] = None, *, remote: bool = True,
                  choices: Optional[dict[str, Choice]] = None) -> Plan:
    """The reel as Finalize freezes it: every included segment's file (the
    original, or the version that ships) and frames, and the /render
    payload. A clip missing from this computer (and, `remote`, from the
    bucket) is a 409 naming the scene — today's stitch waited for it
    forever; so is a picked version whose video is gone."""
    io = io or DbIo()
    md = run.get("metadata") or {}
    targets = await resolve_sources(run, io, remote=remote)
    if choices is None:
        choices = await evaluate(run, targets)
    segments: list[Segment] = []
    for t in included(targets):
        if t.role == "main":
            if not t.anim_id:
                raise HTTPException(409, f"{t.label} has no clip yet.")
            if t.path is None:
                raise HTTPException(409, f"{t.label}'s clip isn't on this computer.")
        elif t.path is None:
            continue                                  # a bookend with nothing: left out, as today
        if poller._to_remotion_url(t.path) is None:
            raise HTTPException(409, f"{t.label}'s clip isn't where the renderer can read it.")
        segments.append(await _segment(t, io, choices.get(t.target)))
    if not any(s.role == "main" for s in segments):
        raise HTTPException(409, "Every scene is excluded — include at least one scene to finalize.")
    body = payload(md, targets, segments)
    return Plan(template=body["template"], segments=segments, payload=body,
                total_frames=expected_frames(body), sha=inputs_sha(inputs(md, targets, choices)))


# ---------------------------------------------------------------------------
# Finalize
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def good_path(md: dict) -> Optional[Path]:
    """The last good reel's file on this disk (composition_path), or None."""
    rel = md.get("composition_path")
    if not rel:
        return None
    p = _abs(rel)
    return p if p.is_file() else None


def bucket_current(md: dict) -> bool:
    """The run's bucket copy is the last good reel: the one key per run is
    overwritten by each backup, so it counts only when it was made from the
    good seq (a failed backup leaves the older reel there). A legacy row's
    backup (neither seq recorded) is its reel."""
    return (bool(md.get("composition_storage_bucket") and md.get("composition_storage_path"))
            and md.get("composition_storage_seq") == md.get("composition_good_seq"))


def good_reel(md: dict) -> bool:
    """A finished reel exists to serve (on this disk, or its bucket copy)."""
    return bool(md.get("composition_path")) and (good_path(md) is not None or bucket_current(md))


def owned(md: dict) -> bool:
    """A row finalized by this round's code (its render has an owner)."""
    return bool(md.get("render_owner"))


def _owner_gone(md: dict) -> bool:
    started = _utc(md.get("render_started_at"))
    return (owned(md) and started is not None
            and started < datetime.now(timezone.utc) - timedelta(minutes=STALE_OWNER_MIN))


def _cancel_run_drafts(run_id: int) -> int:
    """Stop the studio's draft renders of this run's scenes: the reel's own
    render must not queue behind them."""
    try:
        from .rebrand_harness import studio
    except Exception:  # noqa: BLE001 — no studio, no drafts
        return 0
    root = studio.STUDIO_ROOT / str(int(run_id))
    folders = {d.folder for d in list(studio._DRAFTS.values()) if Path(d.folder).parent == root}
    return sum(studio.cancel_drafts(Path(f)) for f in folders)


def _made(md: dict, sha: str) -> bool:
    """The reel served is the one these inputs make: ready, no error, made
    of them, its file here (Finalize has nothing to render)."""
    return (md.get("composition_status") == COMP_READY and not md.get("composition_error")
            and md.get("composition_good_sha") == sha and good_path(md) is not None)


async def statuses_of(md: dict) -> dict[int, Any]:
    rows = await store().anims(included_anim_ids(md))
    return {i: r.get("status") for i, r in rows.items()}


async def current_sha(run: dict, io: Optional[Io] = None) -> str:
    """The reel's inputs_sha now (what a page reads in fetch_run.reel)."""
    return (await reel_state(run, io, statuses={})).sha


async def finalize(run_id: int, *, expect_sha: Optional[str] = None, io: Optional[Io] = None) -> dict:
    """Finalize the reel: {status, rendering: bool, sha}. 409 when the clips
    aren't all ready or one is out of date, when a version is still being
    made for a scene, when the reel changed since the page read it
    (expect_sha), or a clip or a picked version is missing. A reel whose
    inputs are those of its last good render (and whose file is here) is
    not made again; a second press while its render is under way changes
    nothing, and one that read the row before another press's render took
    the next seq renders nothing either (the compare-and-set checks the
    seq)."""
    run = await store().run(run_id)
    if run is None:
        raise HTTPException(404, f"talking-head-v2 run #{run_id} not found")
    md = run.get("metadata") or {}
    status = md.get("composition_status")
    io = io or DbIo()
    state = await reel_state(run, io, statuses=await statuses_of(md))
    sha = state.sha
    if expect_sha and expect_sha != sha:
        raise HTTPException(409, CHANGED_ELSEWHERE)
    if status == COMP_RENDERING:
        if owned(md) and md.get("composition_inputs_sha") == sha and not _owner_gone(md):
            return {"status": COMP_RENDERING, "rendering": True, "sha": sha}
        if not _owner_gone(md):
            raise HTTPException(409, ALREADY_RENDERING)
    elif status not in FINALIZE_FROM:
        raise HTTPException(409, NOT_READY)
    offenders = stale_offenders(md)
    if offenders:
        raise HTTPException(
            409, "These clips are out of date — regenerate them before finalizing: " + ", ".join(offenders))
    if is_multi(md) and not any(not s.get("excluded") for s in md.get("scenes") or []):
        raise HTTPException(409, "Every scene is excluded — include at least one scene to finalize.")
    if not state.ready:
        raise HTTPException(409, NOT_READY)
    # A version on its way would ship the older pick without saying so.
    making = [t.label for t in included(state.targets) if state.choices[t.target].making]
    if making:
        raise HTTPException(409, f"{making[0]} is still being made.")
    if _made(md, sha):
        return {"status": COMP_READY, "rendering": False, "sha": sha}
    plan = await resolve(run, io, choices=state.choices)
    seq_was = int(md.get("composition_seq") or 0)
    seq = seq_was + 1
    patch = {
        "composition_status": COMP_RENDERING,
        "render_job_id": "",
        "render_owner": INSTANCE_ID,
        "render_started_at": _now_iso(),
        "render_404s": 0,
        "composition_seq": seq,
        "composition_plan": plan.frozen(seq),
        "composition_inputs_sha": plan.sha,
    }
    _cancel_run_drafts(int(run_id))
    if not await store().finalize(int(run_id), patch, FINALIZE_FROM, seq_was):
        again = await store().run(run_id) or {}
        amd = again.get("metadata") or {}
        if amd.get("composition_status") == COMP_RENDERING and amd.get("composition_inputs_sha") == plan.sha:
            return {"status": COMP_RENDERING, "rendering": True, "sha": plan.sha}
        # Another press got there first and has already made this very reel.
        if _made(amd, plan.sha):
            return {"status": COMP_READY, "rendering": False, "sha": plan.sha}
        raise HTTPException(409, ALREADY_RENDERING if amd.get("composition_status") == COMP_RENDERING
                            else CHANGED_ELSEWHERE)
    log.info("[th-reel] run=%d finalized: seq=%d sha=%s %d segments, %d frames",
             int(run_id), seq, plan.sha, len(plan.segments), plan.total_frames)
    return {"status": COMP_RENDERING, "rendering": True, "sha": plan.sha}
