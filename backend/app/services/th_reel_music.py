"""Background music under a finished talking-head-v2 reel (the Final step).

The user describes a vibe ("slow jazz piano"), Suno makes an INSTRUMENTAL
track, and it is laid under the whole finished reel (intro, every scene,
outro) at the reel's exact length, ducked under the speaker so the voice
stays clear.

  start_track    the one paid call, only ever from the explicit POST: Suno
                 is asked for a track (reel_music.track = generating)
  poll_tracks    the composition poller's extra phase (its own task, see
                 kick): a track Suno finished is downloaded, kept (this disk
                 and the bucket) and laid under the reel; a reel finalized
                 again gets its music again (free, seconds)
  apply_mix      the mix itself (ffmpeg; the video stream is copied, never
                 re-encoded), or switching the music off
  public_state   what fetch_run says about it (run.music)

The no-music reel stays the canonical one: composition_path, its seqs, its
bucket key and its pruning are never touched here. The music version is a
file of its own beside it (music-<seq>-<id>.mp4, never final*.mp4, so the
poller's _prune leaves it alone) with its own bucket key, served by the same
file route with ?music=1.

State lives in metadata.reel_music, written only through jsonb_set on that
one key (MUSIC_SQL): no other TH v2 writer's shallow `metadata || patch`
carries it, so none can clobber it, and it cannot clobber theirs.

  on, level        the music is wanted on the reel, how loud (soft|medium|loud)
  removed          "Remove music": off, and the panel starts over
  track            the newest track asked for: {status: generating|ready|
                   failed, brief, suno_job_id, rel, bucket, key, sec, error,
                   started_at, at}
  mix              the last good mix: {id, for_seq (the composition_good_seq
                   it was made on), made ("<job>|<level>"), level, rel,
                   bucket, key, sec, at, track: the track it was made from}
  mix_error        {for: "<seq>|<job>|<level>", error, at}: that mix failed
                   (the poller does not retry it; a press does)
  track_storage_job  whose track the one bucket key holds

While a new track is being made the last mix keeps playing: the track used
for a mix is the newest READY one, else the one the last mix was made from.
The music wanted ("<job>|<level>", _made_of) is always worked out from that
and the level, never stored, so it cannot go out of step with them.
"""
from __future__ import annotations

import asyncio
import copy
import json
import logging
import os
import re
import secrets
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional, Protocol, Sequence

import httpx
from fastapi import HTTPException
from psycopg.types.json import Jsonb

from ..db import get_conn
from ..workers import yas_th_v2_composition_poller as poller
from . import th_reel
from .media_providers import ProviderError, SunoMusicProvider

log = logging.getLogger("archivist.th_reel_music")

MODE_PREVIEW = th_reel.MODE_PREVIEW

# The bed's gain per level, in dB, after it is normalised to -26 LUFS.
LEVELS = {"soft": -8.0, "medium": -4.0, "loud": 0.0}
DEFAULT_LEVEL = "medium"
BRIEF_MAX = 200
# Suno's custom mode takes the brief as `style`; this keeps it a bed.
BRIEF_SUFFIX = ", instrumental background music, no vocals"
# A track Suno has not delivered by then is given up (the video editor's cap).
GIVE_UP_MIN = 30
# A claim whose Suno call never came back (the process died in it).
CLAIM_LOST_MIN = 5
DOWNLOAD_TIMEOUT_SEC = 120.0
# Backing a file up must not hold the poll (a restricted bucket).
BACKUP_TIMEOUT_SEC = 60.0
TRACK_KEY = "yas/compositions/talking-head/{run_id}-music-track.mp3"
MIX_KEY = "yas/compositions/talking-head/{run_id}-music.mp4"

ST_GENERATING = "generating"
ST_READY = "ready"
ST_FAILED = "failed"

NOT_FINISHED = "Finish the reel first: the music goes under the finished video."
ALREADY_MAKING = "Music is already being made for this reel."
NO_KEY = "Background music isn't set up on this server (no Suno key)."
NO_TRACK = "Make the music first."
TOO_LONG = "Suno took too long. Try again."
CLAIM_LOST = "The music request was lost. Try again."
MIX_FAILED = "Couldn't lay the music under the reel. Try again."


class MixError(RuntimeError):
    """The mix failed; the message is what the page shows."""


# ---------------------------------------------------------------------------
# The store: every write of reel_music (and the reads they depend on)
# ---------------------------------------------------------------------------

# One merge into metadata.reel_music, never a write of the whole metadata.
# The guarded form is a compare-and-set on one value inside it (a claim, or
# a track that must still be the one asked for).
_MUSIC_SET = """
UPDATE yas_generations
   SET metadata = jsonb_set(metadata, '{reel_music}',
                            COALESCE(metadata->'reel_music', '{}'::jsonb) || %(patch)s::jsonb),
       updated_at = NOW()
 WHERE id = %(id)s AND mode = %(mode)s<guard>
RETURNING metadata->'reel_music' AS reel_music
"""
MUSIC_SQL = _MUSIC_SET.replace("<guard>", "")
MUSIC_IF_SQL = _MUSIC_SET.replace(
    "<guard>", "\n   AND COALESCE(metadata #>> %(gpath)s::text[], '') = %(gval)s")

RUN_SQL = "SELECT id, metadata FROM yas_generations WHERE id = %s AND mode = %s"

# The reels with music work to do: a track Suno is making, or music on whose
# last mix is not the music wanted on the reel being served (another reel,
# track or level) and has not failed for exactly that. `want` is _made_of
# (the newest ready track, else the last mix's; the level, medium by
# default) and `seq` _seq; _due is the same rule, and re-checks every row.
DUE_SQL = """
SELECT id, metadata
  FROM (SELECT id, metadata, rm, seq,
               (CASE WHEN rm->'track'->>'status' = 'ready'
                          AND COALESCE(rm->'track'->>'suno_job_id', '') <> ''
                          AND COALESCE(rm->'track'->>'rel', '') <> ''
                     THEN rm->'track'->>'suno_job_id'
                     ELSE NULLIF(rm->'mix'->'track'->>'suno_job_id', '') END)
               || '|' ||
               (CASE WHEN rm->>'level' IN ('soft', 'medium', 'loud') THEN rm->>'level' ELSE 'medium' END) AS want
          FROM (SELECT id, metadata, metadata->'reel_music' AS rm,
                       COALESCE(metadata->>'composition_good_seq', '0') AS seq
                  FROM yas_generations
                 WHERE mode = %s AND metadata ? 'reel_music') r) w
 WHERE rm->'track'->>'status' = 'generating'
    OR ( rm->>'on' = 'true' AND want IS NOT NULL
         AND ( COALESCE(rm->'mix'->>'for_seq', '') <> seq OR COALESCE(rm->'mix'->>'made', '') <> want )
         AND COALESCE(rm->'mix_error'->>'for', '') <> seq || '|' || want )
"""

Guard = tuple[Sequence[str], str]


class Store(Protocol):
    async def run(self, run_id: int) -> Optional[dict]: ...
    async def put(self, run_id: int, patch: dict, guard: Optional[Guard] = None) -> Optional[dict]: ...
    async def due(self) -> list[dict]: ...


def _md(v: Any) -> dict:
    if isinstance(v, str):
        try:
            v = json.loads(v)
        except ValueError:
            v = {}
    return v if isinstance(v, dict) else {}


class PgStore:
    """The shared database (reads autocommit, as th_reel's)."""

    async def run(self, run_id: int) -> Optional[dict]:
        async with get_conn(autocommit=True) as conn:
            async with conn.cursor() as cur:
                await cur.execute(RUN_SQL, (int(run_id), MODE_PREVIEW))
                row = await cur.fetchone()
        if not row:
            return None
        return {"id": int(row["id"]), "metadata": _md(row.get("metadata"))}

    async def put(self, run_id: int, patch: dict, guard: Optional[Guard] = None) -> Optional[dict]:
        params: dict[str, Any] = {"patch": Jsonb(patch), "id": int(run_id), "mode": MODE_PREVIEW}
        if guard is not None:
            params.update(gpath=list(guard[0]), gval=str(guard[1]))
        async with get_conn() as conn:
            async with conn.cursor() as cur:
                await cur.execute(MUSIC_IF_SQL if guard is not None else MUSIC_SQL, params)
                row = await cur.fetchone()
        return _md(row.get("reel_music")) if row else None

    async def due(self) -> list[dict]:
        async with get_conn(autocommit=True) as conn:
            async with conn.cursor() as cur:
                await cur.execute(DUE_SQL, (MODE_PREVIEW,))
                rows = await cur.fetchall()
        return [{"id": int(r["id"]), "metadata": _md(r.get("metadata"))} for r in rows]


def _at(md: dict, path: Sequence[str]) -> str:
    """`metadata #>> path` as the guard reads it ('' for missing or null)."""
    v: Any = md
    for k in path:
        if not isinstance(v, dict) or k not in v:
            return ""
        v = v[k]
    if v is None:
        return ""
    if isinstance(v, bool):
        return "true" if v else "false"
    return v if isinstance(v, str) else json.dumps(v)


class MemoryStore:
    """The same statements on rows in memory: tests. `rows` are {id: {id,
    metadata}}."""

    def __init__(self, rows: Optional[dict] = None) -> None:
        self.rows: dict[int, dict] = {int(k): v for k, v in (rows or {}).items()}
        self.writes: list[tuple[int, dict]] = []

    async def run(self, run_id: int) -> Optional[dict]:
        row = self.rows.get(int(run_id))
        return copy.deepcopy(row) if row is not None else None

    async def put(self, run_id: int, patch: dict, guard: Optional[Guard] = None) -> Optional[dict]:
        row = self.rows.get(int(run_id))
        if row is None:
            return None
        md = row.get("metadata") or {}
        if guard is not None and _at(md, guard[0]) != str(guard[1]):
            return None
        self.writes.append((int(run_id), copy.deepcopy(patch)))
        rm = {**_rm(md), **copy.deepcopy(patch)}
        row["metadata"] = {**md, "reel_music": rm}
        return copy.deepcopy(rm)

    async def due(self) -> list[dict]:
        return [copy.deepcopy(r) for r in self.rows.values() if _due(r.get("metadata") or {})]


_store: Store = PgStore()


def store() -> Store:
    return _store


def set_store(new: Store) -> Store:
    """Swap where the music reads and writes (tests); returns the previous."""
    global _store
    old, _store = _store, new
    return old


def provider() -> SunoMusicProvider:
    """Suno (a seam for tests)."""
    return SunoMusicProvider()


# ---------------------------------------------------------------------------
# Reading the state
# ---------------------------------------------------------------------------

def _rm(md: dict) -> dict:
    rm = (md or {}).get("reel_music")
    return rm if isinstance(rm, dict) else {}


def _sub(rm: dict, key: str) -> dict:
    v = rm.get(key)
    return v if isinstance(v, dict) else {}


def _level(v: Any) -> str:
    return v if v in LEVELS else DEFAULT_LEVEL


def _seq(md: dict) -> int:
    """The reel being served (a legacy row, which has no seq: 0)."""
    try:
        return int(md.get("composition_good_seq") or 0)
    except (TypeError, ValueError):
        return 0


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _source(rm: dict) -> Optional[dict]:
    """The track a mix is made from: the newest ready one, else the one the
    last mix was made from (so the music stays while a new track is made)."""
    t = _sub(rm, "track")
    if t.get("status") == ST_READY and t.get("suno_job_id") and t.get("rel"):
        return {k: t.get(k) for k in ("suno_job_id", "brief", "rel", "bucket", "key", "sec")}
    m = _sub(_sub(rm, "mix"), "track")
    return m if m.get("suno_job_id") else None


def _made_of(rm: dict) -> str:
    """The music wanted under the reel, "<job>|<level>" ('' with no track)."""
    src = _source(rm)
    return f"{src['suno_job_id']}|{_level(rm.get('level'))}" if src else ""


def _current(md: dict) -> bool:
    """The last mix is the wanted music on the reel being served."""
    rm = _rm(md)
    mix = _sub(rm, "mix")
    want = _made_of(rm)
    return bool(want) and mix.get("for_seq") == _seq(md) and mix.get("made") == want


def _wants_mix(md: dict) -> bool:
    """Music is on and its mix is not made yet (nor failed for exactly this)."""
    rm = _rm(md)
    want = _made_of(rm)
    if not rm.get("on") or not want or _current(md):
        return False
    return _sub(rm, "mix_error").get("for") != f"{_seq(md)}|{want}"


def _due(md: dict) -> bool:
    """A reel with music work to do (DUE_SQL's rule)."""
    return _sub(_rm(md), "track").get("status") == ST_GENERATING or _wants_mix(md)


def _local(rel: Optional[str]) -> Optional[Path]:
    if not rel:
        return None
    p = poller._abs_from_rel(rel)
    return p if p.is_file() else None


def _shown(md: dict) -> Optional[dict]:
    """The mix to show (and to save, open or post): music on, and the last
    mix made on the reel being served, its file here or in the bucket.
    During a re-mix on the same reel (another track or level) it is the
    previous one, until the new one is ready."""
    rm = _rm(md)
    mix = _sub(rm, "mix")
    if not rm.get("on") or not mix.get("rel") or mix.get("for_seq") != _seq(md):
        return None
    if _local(mix.get("rel")) is None and not (mix.get("bucket") and mix.get("key")):
        return None
    return mix


def _plan_seconds(md: dict) -> Optional[float]:
    """The reel's length from its frozen plan, when the plan is the served
    reel's (a legacy row has none)."""
    plan = md.get("composition_plan")
    if not isinstance(plan, dict) or not plan.get("total_frames"):
        return None
    if int(plan.get("seq") or 0) != _seq(md):
        return None
    return round(int(plan["total_frames"]) / th_reel.FPS, 2)


def music_url(run_id: int, mix: dict) -> str:
    """The music version's URL: ?v names the reel and m the mix, so a page
    swaps players when either changes (the browser revalidates)."""
    return (f"/api/yas-producer/talking-head-v2/{int(run_id)}/composition/file"
            f"?music=1&v={int(mix.get('for_seq') or 0)}&m={mix.get('id') or ''}")


_OFF = {
    "available": False, "on": False, "level": DEFAULT_LEVEL, "track_status": None, "brief": None,
    "error": None, "has_track": False, "track_brief": None, "sec": None, "applied": False,
    "mixing": False, "mix_error": None, "music_url": None, "mix_id": None, "reel_sec": None,
}


def public_state(run_id: int, md: dict) -> dict:
    """run.music for the page:

      available     Suno is set up on this server
      on, level     the music is wanted on the reel, how loud
      track_status  the newest track asked for: null | generating | ready | failed
      brief, error  that track's vibe, and why it failed
      has_track     a track to put under the reel (track_brief, sec: its
                    vibe and length)
      applied       the wanted music is on the reel being served
      mixing        the poller is (about to be) laying it under the reel
      mix_error     why laying it under the reel failed
      music_url     the music version to show (null: show the reel)
      mix_id        which music version that is
      reel_sec      the reel's length, when its plan knows it
    """
    try:
        rm = _rm(md)
        track = _sub(rm, "track")
        removed = bool(rm.get("removed"))
        status = track.get("status") if track.get("status") in (ST_GENERATING, ST_READY, ST_FAILED) else None
        # Removed: the panel starts over (a track asked for since still shows).
        if removed and status == ST_READY:
            status = None
        src = None if removed else _source(rm)
        shown = _shown(md)
        applied = _current(md) and shown is not None
        want = _made_of(rm)
        failed = _sub(rm, "mix_error")
        mix_error = (failed.get("error") if rm.get("on") and want and not applied
                     and failed.get("for") == f"{_seq(md)}|{want}" else None)
        return {
            "available": provider().available(),
            "on": bool(rm.get("on")),
            "level": _level(rm.get("level")),
            "track_status": status,
            "brief": track.get("brief") if status else None,
            "error": track.get("error") if status == ST_FAILED else None,
            "has_track": src is not None,
            "track_brief": src.get("brief") if src else None,
            "sec": src.get("sec") if src else None,
            "applied": applied,
            "mixing": _wants_mix(md) and th_reel.good_reel(md),
            "mix_error": mix_error,
            "music_url": music_url(run_id, shown) if shown else None,
            "mix_id": shown.get("id") if shown else None,
            "reel_sec": _plan_seconds(md),
        }
    except Exception:  # noqa: BLE001 — the run's page never fails over its music
        log.exception("[reel-music] run=%s state unreadable", run_id)
        return dict(_OFF)


def current_id(md: dict) -> Optional[str]:
    """Which music version is the one shown (None: the reel without music)."""
    shown = _shown(md)
    return shown.get("id") if shown else None


def served_mix(md: dict) -> Optional[dict]:
    """The file route's ?music=1: the last mix, whatever the switch says (a
    page may still be playing it) — {path: on this disk or None, bucket,
    key} — or None when no music version was ever made."""
    mix = _sub(_rm(md), "mix")
    if not mix.get("rel"):
        return None
    return {"path": _local(mix.get("rel")), "bucket": mix.get("bucket"), "key": mix.get("key")}


async def current_bytes(md: dict) -> Optional[bytes]:
    """The music version's bytes when it is the one shown (saved to assets,
    opened in the editor, posted), else None: the reel without music."""
    shown = _shown(md)
    if shown is None:
        return None
    p = _local(shown.get("rel"))
    if p is not None:
        return await asyncio.to_thread(p.read_bytes)
    from . import asset_storage

    try:
        return await asset_storage.download_bytes(storage_bucket=shown["bucket"], storage_path=shown["key"])
    except Exception as e:  # noqa: BLE001 — the reel itself is still there
        log.warning("[reel-music] the music version's bucket copy is unreadable: %s", str(e)[:200])
        return None


# ---------------------------------------------------------------------------
# Files
# ---------------------------------------------------------------------------

def _run_dir(run_id: int) -> Path:
    """The run's composition dir (the poller's COMP_DIR/<run>)."""
    return poller.COMP_DIR / str(int(run_id))


def _music_dir(run_id: int) -> Path:
    return _run_dir(run_id) / "music"


def _safe(job: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]", "", str(job))[:64] or "track"


async def _backup(path: Path, key: str, mime: str) -> tuple[Optional[str], Optional[str]]:
    from . import asset_storage

    try:
        return await asyncio.wait_for(
            asset_storage.persist_local_file(abs_path=path, bucket_key=key, mime=mime), BACKUP_TIMEOUT_SEC)
    except Exception as e:  # noqa: BLE001 — kept on this disk only, as a failed upload is
        log.warning("[reel-music] backup of %s failed: %s", path.name, str(e)[:200] or type(e).__name__)
        return None, None


async def _fetch(bucket: Optional[str], key: Optional[str], dest: Path) -> Optional[Path]:
    """A bucket copy fetched to `dest` (None: no copy, or unreadable)."""
    if not (bucket and key):
        return None
    from . import asset_storage

    try:
        raw = await asset_storage.download_bytes(storage_bucket=bucket, storage_path=key)
    except Exception as e:  # noqa: BLE001
        log.warning("[reel-music] %s/%s unreadable: %s", bucket, key, str(e)[:200])
        return None
    if not raw:
        return None
    dest.parent.mkdir(parents=True, exist_ok=True)
    await asyncio.to_thread(dest.write_bytes, raw)
    return dest


def _prune(folder: Path, pattern: str, keep: set[Path]) -> None:
    keep = {p.resolve() for p in keep if p is not None}
    for p in folder.glob(pattern):
        if p.resolve() in keep:
            continue
        try:
            p.unlink()
        except OSError:
            log.warning("[reel-music] couldn't remove %s", p)


# ---------------------------------------------------------------------------
# The mix (ffmpeg; sync, run in a thread)
# ---------------------------------------------------------------------------

def mix_under(reel: Path, track: Path, out: Path, *, gain_db: float) -> float:
    """Lay `track` under `reel` into `out`, at the reel's exact length, and
    return that length (s).

    The track loops when shorter than the reel and is cut when longer,
    normalised to -26 LUFS and set to the level's gain, faded in and out,
    then ducked under the reel's own sound (the speaker) by a sidechain
    compressor keyed on it: the voice stays clear, the music comes up
    between lines. The reel's video stream is copied, never re-encoded, so
    no frame changes. Both sidechain inputs are pinned to dbl: the bundled
    ffmpeg 4.2 can't negotiate them otherwise. amix scales each input by
    1/2 in every version (`normalize` only exists from 4.4), so volume=2
    restores a plain sum: both inputs run the whole length (the voice is
    padded), so the scale never changes mid-way."""
    from . import rebrand_media as media

    dur = media.video_seconds(reel)
    if dur <= 0:
        raise media.MediaError("the reel has no video to put music under")
    rate = media.AUDIO_RATE
    d = f"{dur:.3f}"
    fade_in = min(1.5, dur / 4)
    fade_out = min(2.5, dur / 3)
    pin = f"aformat=sample_fmts=dbl:sample_rates={rate}:channel_layouts=stereo"
    bed = (f"[1:a]aresample={rate},aformat=sample_fmts=fltp:channel_layouts=stereo,"
           f"atrim=duration={d},asetpts=PTS-STARTPTS,"
           f"loudnorm=I=-26:TP=-3:LRA=11,aresample={rate},volume={gain_db:g}dB,"
           f"afade=t=in:st=0:d={fade_in:.3f},afade=t=out:st={dur - fade_out:.3f}:d={fade_out:.3f}")
    limiter = "alimiter=limit=0.95:level=0"
    if media.has_audio(reel):
        graph = (
            f"[0:a]aresample={rate},aformat=sample_fmts=fltp:channel_layouts=stereo,"
            f"apad,atrim=duration={d},asplit=2[voice][k];[k]{pin}[key];"
            f"{bed},{pin}[bed];"
            "[bed][key]sidechaincompress=threshold=0.015:ratio=8:attack=20:release=400:knee=3[ducked];"
            f"[voice][ducked]amix=inputs=2:duration=first:dropout_transition=0,volume=2,{limiter}[a]"
        )
    else:
        # A reel with no sound at all: the bed alone.
        graph = f"{bed},{limiter}[a]"
    out.parent.mkdir(parents=True, exist_ok=True)
    # Written under another name first: a half-made file is never served.
    part = out.with_name(out.stem + ".part")
    cmd = [
        media.ffmpeg_exe(), "-y", "-i", str(reel), "-stream_loop", "-1", "-i", str(track),
        "-filter_complex", graph,
        "-map", "0:v:0", "-map", "[a]",
        "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-ar", str(rate), "-ac", "2",
        "-t", d, "-movflags", "+faststart", "-f", "mp4", str(part),
    ]
    try:
        media._run(cmd, what=f"music under {reel.name}")
        os.replace(part, out)
    finally:
        part.unlink(missing_ok=True)
    return dur


def _audio_seconds(path: Path) -> Optional[float]:
    from . import rebrand_media as media

    return media.probe_duration(path)


# One mix at a time per reel on this instance (the poller's and a press's).
_LOCKS: dict[int, tuple[asyncio.AbstractEventLoop, asyncio.Lock]] = {}


def _lock(run_id: int) -> asyncio.Lock:
    loop = asyncio.get_running_loop()
    got = _LOCKS.get(int(run_id))
    if got is None or got[0] is not loop:
        got = (loop, asyncio.Lock())
        _LOCKS[int(run_id)] = got
    return got[1]


async def _mix(run_id: int, *, force: bool = False) -> dict:
    """Make the wanted music version of the reel being served, unless it is
    made already (or, `force` off, it failed for exactly this). Returns the
    reel_music after. A failure is recorded (mix_error) and raised as
    MixError; the last good mix stays."""
    async with _lock(run_id):
        run = await store().run(run_id)
        if run is None:
            raise HTTPException(404, f"talking-head-v2 run #{run_id} not found")
        md = run.get("metadata") or {}
        rm = _rm(md)
        src = _source(rm)
        if not rm.get("on") or src is None or not th_reel.good_reel(md):
            return rm
        level = _level(rm.get("level"))
        seq = _seq(md)
        made = f"{src['suno_job_id']}|{level}"
        target = f"{seq}|{made}"
        mix = _sub(rm, "mix")
        if mix.get("for_seq") == seq and mix.get("made") == made and _shown(md) is not None:
            return rm
        if not force and _sub(rm, "mix_error").get("for") == target:
            return rm
        folder = _music_dir(run_id)
        fetched: Optional[Path] = None
        mix_id = f"{seq}-{secrets.token_hex(3)}"
        out = _run_dir(run_id) / f"music-{mix_id}.mp4"
        try:
            reel = th_reel.good_path(md)
            if reel is None and th_reel.bucket_current(md):
                reel = fetched = await _fetch(md.get("composition_storage_bucket"),
                                              md.get("composition_storage_path"), folder / f"reel-{seq}.mp4")
            if reel is None:
                raise MixError("The finished reel isn't on this computer or in the bucket.")
            track = _local(src.get("rel"))
            if track is None and rm.get("track_storage_job") == src.get("suno_job_id"):
                track = await _fetch(src.get("bucket"), src.get("key"),
                                     folder / f"track-{_safe(src['suno_job_id'])}.mp3")
            if track is None:
                raise MixError("The music track is gone. Make the music again.")
            sec = await asyncio.to_thread(mix_under, reel, track, out, gain_db=LEVELS[level])
        except Exception as e:  # noqa: BLE001 — recorded; the reel and the last mix stay
            msg = str(e) if isinstance(e, MixError) else MIX_FAILED
            log.warning("[reel-music] run=%d mix %s failed: %s", int(run_id), target, str(e)[-400:])
            await store().put(run_id, {"mix_error": {"for": target, "error": msg, "at": _now_iso()}})
            raise MixError(msg) from e
        finally:
            if fetched is not None:
                fetched.unlink(missing_ok=True)
        bucket, key = await _backup(out, MIX_KEY.format(run_id=int(run_id)), "video/mp4")
        new = {"id": mix_id, "for_seq": seq, "made": made, "level": level, "rel": poller._to_rel(out),
               "bucket": bucket, "key": key, "sec": round(float(sec), 3), "at": _now_iso(),
               "track": src}
        got = await store().put(run_id, {"mix": new, "mix_error": None})
        log.info("[reel-music] run=%d music laid under reel seq=%d (%s, %.2fs)", int(run_id), seq, made, sec)
        # The new version and the one a page may still be playing stay; the
        # tracks kept are the one used and the newest.
        _prune(_run_dir(run_id), "music-*.mp4", {out, _local(mix.get("rel"))})
        _prune(folder, "track-*.mp3", {track, _local(_sub(rm, "track").get("rel"))})
        return got or rm


# ---------------------------------------------------------------------------
# The page's presses
# ---------------------------------------------------------------------------

async def _run_md(run_id: int) -> dict:
    run = await store().run(run_id)
    if run is None:
        raise HTTPException(404, f"talking-head-v2 run #{run_id} not found")
    return run.get("metadata") or {}


def _utc(v: Any) -> Optional[datetime]:
    return th_reel._utc(v)


def _stale(track: dict) -> bool:
    """A generating track nobody will finish: past the give-up, or a claim
    whose Suno call never came back."""
    started = _utc(track.get("started_at"))
    if started is None:
        return True
    age = datetime.now(timezone.utc) - started
    return age > timedelta(minutes=GIVE_UP_MIN) or (
        not track.get("suno_job_id") and age > timedelta(minutes=CLAIM_LOST_MIN))


async def _reel_seconds(md: dict) -> float:
    sec = _plan_seconds(md)
    if sec:
        return sec
    p = th_reel.good_path(md)
    if p is not None:
        from . import rebrand_media as media

        try:
            return float(await asyncio.to_thread(media.video_seconds, p))
        except Exception:  # noqa: BLE001 — only a hint to Suno
            pass
    return 30.0


async def start_track(run_id: int, brief: str) -> dict:
    """Ask Suno for an instrumental track for this reel (the paid call: only
    ever from the explicit POST). 400 without a vibe, 409 without a finished
    reel or while a track is being made, 503 when Suno isn't set up, 502
    with Suno's own words when it refuses. Returns reel_music after."""
    brief = " ".join(str(brief or "").split())
    if not brief:
        raise HTTPException(400, "Describe the music first.")
    if len(brief) > BRIEF_MAX:
        raise HTTPException(400, f"Keep the description under {BRIEF_MAX} characters.")
    md = await _run_md(run_id)
    if not th_reel.good_reel(md):
        raise HTTPException(409, NOT_FINISHED)
    rm = _rm(md)
    track = _sub(rm, "track")
    if track.get("status") == ST_GENERATING and not _stale(track):
        raise HTTPException(409, ALREADY_MAKING)
    suno = provider()
    if not suno.available():
        raise HTTPException(503, NO_KEY)
    # Claimed before the call: a second press (another tab) is a 409, not a
    # second paid track. The claim is a compare-and-set on the track read.
    started = _now_iso()
    claim = {"status": ST_GENERATING, "brief": brief, "suno_job_id": None, "started_at": started,
             "error": None, "at": started}
    path = ["reel_music", "track", "started_at"]
    got = await store().put(run_id, {"track": claim}, guard=(path, _at(md, path)))
    if got is None:
        raise HTTPException(409, ALREADY_MAKING)
    mine = (path, started)
    try:
        res = await suno.make_music(style_brief=brief + BRIEF_SUFFIX, duration_sec=await _reel_seconds(md))
        job = str(res.get("provider_job_id") or "")
        if not job:
            raise ProviderError("Suno didn't say which job it started.")
    except ProviderError as e:
        await store().put(run_id, {"track": {**claim, "status": ST_FAILED, "error": str(e)[:300],
                                             "at": _now_iso()}}, guard=mine)
        raise HTTPException(502, str(e)[:300]) from e
    except Exception as e:
        await store().put(run_id, {"track": {**claim, "status": ST_FAILED,
                                             "error": "Suno couldn't be reached. Try again.",
                                             "at": _now_iso()}}, guard=mine)
        raise HTTPException(502, "Suno couldn't be reached. Try again.") from e
    log.info("[reel-music] run=%d track asked of Suno: job=%s brief=%r", int(run_id), job, brief[:80])
    return await store().put(run_id, {"track": {**claim, "suno_job_id": job}}, guard=mine) or rm


async def apply_mix(run_id: int, on: bool, level: Optional[str] = None, *, remove: bool = False) -> dict:
    """Music on (laid under the reel being served, at `level` or the one
    set) or off (the files are kept; `remove`: the panel starts over).
    409 with no track or no finished reel. Returns reel_music after."""
    if level is not None and level not in LEVELS:
        raise HTTPException(400, f"level must be one of {', '.join(LEVELS)}")
    md = await _run_md(run_id)
    rm = _rm(md)
    if not on:
        patch: dict[str, Any] = {"on": False}
        if remove:
            patch["removed"] = True
        return await store().put(run_id, patch) or rm
    if _source(rm) is None or rm.get("removed"):
        raise HTTPException(409, NO_TRACK)
    if not th_reel.good_reel(md):
        raise HTTPException(409, NOT_FINISHED)
    await store().put(run_id, {"on": True, "level": _level(level or rm.get("level"))})
    try:
        return await _mix(run_id, force=True)
    except MixError as e:
        raise HTTPException(500, str(e)) from e


# ---------------------------------------------------------------------------
# The poller's phase
# ---------------------------------------------------------------------------

async def _download_first(run_id: int, urls: list[str]) -> bytes:
    """The first mirror that downloads; b'' when none does yet (the URL can
    lag Suno's SUCCESS by 2-3 min: the next tick tries again). The video
    editor's poller's rule (yas_video_edit_ai_poller._download_first)."""
    last = ""
    for url in urls:
        if not url:
            continue
        try:
            async with httpx.AsyncClient(timeout=DOWNLOAD_TIMEOUT_SEC, follow_redirects=True) as client:
                r = await client.get(url)
            if r.status_code >= 300 or not r.content:
                raise RuntimeError(f"download {r.status_code} ({len(r.content)} bytes)")
            return r.content
        except Exception as e:  # noqa: BLE001
            last = str(e) or type(e).__name__
    log.info("[reel-music] run=%d no track mirror downloadable yet (last: %s)", int(run_id), last[:200])
    return b""


async def _track_failed(run_id: int, track: dict, error: str) -> None:
    path = ["reel_music", "track", "started_at"]
    await store().put(run_id, {"track": {**track, "status": ST_FAILED, "error": error[:300], "at": _now_iso()}},
                      guard=(path, str(track.get("started_at") or "")))
    log.warning("[reel-music] run=%d track failed: %s", int(run_id), error[:200])


async def _track_ready(run_id: int, track: dict, raw: bytes) -> bool:
    """Keep a downloaded track (this disk, then the bucket) and make it the
    one under the reel (music on). False when it isn't audio."""
    job = str(track["suno_job_id"])
    path = _music_dir(run_id) / f"track-{_safe(job)}.mp3"
    path.parent.mkdir(parents=True, exist_ok=True)
    await asyncio.to_thread(path.write_bytes, raw)
    sec = await asyncio.to_thread(_audio_seconds, path)
    if not sec:
        path.unlink(missing_ok=True)
        await _track_failed(run_id, track, "Suno's track couldn't be read. Try again.")
        return False
    bucket, key = await _backup(path, TRACK_KEY.format(run_id=int(run_id)), "audio/mpeg")
    ready = {**track, "status": ST_READY, "rel": poller._to_rel(path), "bucket": bucket, "key": key,
             "sec": round(float(sec), 2), "error": None, "at": _now_iso()}
    patch: dict[str, Any] = {"track": ready, "on": True, "removed": False}
    if bucket and key:
        patch["track_storage_job"] = job
    got = await store().put(run_id, patch, guard=(["reel_music", "track", "suno_job_id"], job))
    if got is None:
        log.info("[reel-music] run=%d track %s was replaced meanwhile", int(run_id), job)
        return False
    log.info("[reel-music] run=%d track %s ready (%.1fs)", int(run_id), job, sec)
    return True


async def _check_track(run_id: int, rm: dict) -> None:
    """One look at a generating track: still cooking (kept; given up after
    GIVE_UP_MIN), failed, or done (downloaded and kept)."""
    track = _sub(rm, "track")
    job = track.get("suno_job_id")
    if _stale(track):
        await _track_failed(run_id, track, TOO_LONG if job else CLAIM_LOST)
        return
    if not job:
        return                                   # the press is still asking Suno
    try:
        got = await provider().check_job(str(job))
    except ProviderError as e:                   # transient: next tick
        log.info("[reel-music] run=%d Suno poll: %s", int(run_id), str(e)[:200])
        return
    status = got.get("status")
    if status == "failed":
        await _track_failed(run_id, track, str(got.get("error") or "Suno couldn't make the track."))
        return
    if status != "done":
        return
    urls = [u for u in (got.get("audio_urls") or [got.get("audio_url")]) if u]
    raw = await _download_first(run_id, urls)
    if raw:
        await _track_ready(run_id, track, raw)


async def _poll_one(row: dict) -> None:
    run_id = int(row["id"])
    md = row.get("metadata") or {}
    if _sub(_rm(md), "track").get("status") == ST_GENERATING:
        await _check_track(run_id, _rm(md))
        fresh = await store().run(run_id)
        md = (fresh or {}).get("metadata") or {}
    # A track just made, or a reel finalized again: the music under it again.
    if _wants_mix(md) and th_reel.good_reel(md):
        try:
            await _mix(run_id)
        except MixError:
            pass                                 # recorded: the page says so


async def poll_tracks() -> None:
    """Every reel with music work to do: a track Suno is making, a mix to
    make (the track just landed, or the reel was finalized again)."""
    for row in await store().due():
        try:
            await _poll_one(row)
        except Exception:  # noqa: BLE001 — one reel's music never stops another's
            log.exception("[reel-music] run=%s poll crashed", row.get("id"))


_poll_task: Optional[asyncio.Task] = None


async def _poll_quietly() -> None:
    try:
        await poll_tracks()
    except Exception:  # noqa: BLE001
        log.exception("[reel-music] poll failed")


def kick() -> asyncio.Task:
    """poll_tracks in a task of its own, unless the last one is still
    running: a slow download or mix never holds the reel's render phases."""
    global _poll_task
    if _poll_task is None or _poll_task.done():
        _poll_task = asyncio.create_task(_poll_quietly(), name="th_reel_music.poll")
    return _poll_task
