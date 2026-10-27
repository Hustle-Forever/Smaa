"""A finished version as something to look at and build on — its per-part
clips, each part's SETTLED FIRST FRAME, its lineage, and what a new version
built on it starts from.

Two ways to build on a finished version (options.base = {version_id, mode}):

  rebuild  "Change it": the edited recipe is redone from the ORIGINAL, as a
           fresh version is, but keeping the version's take and handed its
           manifest (stages), so a part whose chunk, prompt and target did
           not change reuses its Omni clip; its brief is reused when what the
           brief was told (and the clip it saw) did not change
           (brief_reusable). "fresh" (Try again) is a new take: nothing is
           reused.
  stack    "Add on top": the new layers are drawn ON the version's own clip,
           part by part (conformed to the original part, frame for frame),
           from its settled first frame; outside the new layers' footprint
           (hold_mask) the output IS the version, and the speaker comes from
           the TRUE original. A version's recipe then holds only the layers
           its own pass drew; effective_layers walks the chain.

A version made in house ON TOP of one Omni made (a "house pass": its own
layers laid over that version's clean video) is added to over that same
video: the pass is flattened onto its GROUND — the chain's first version
that is not a house pass, whose video it was drawn over — and draws the
passes' own frozen layers again (read from their rows) under the new ones
(house.flatten). Unchanged, the draft is then that version's very spec (its
render key), and Omni's video stays under everything. A version made in
house from the original is not added to: it is changed (its recipe is the
whole picture).

  chunk_sources(exp)      the version's own final clip for every part (≤10 s
                          chunk) of its scene, in part order. Found, in
                          order of trust: the path the run's guards manifest
                          recorded (made in house: its one render — a house
                          version is one part, its `render` stage); the
                          run's file names
                          (<label>_c<j>_front.mp4, else <label>_c<j>.mp4);
                          and last a cut of the version's final at the part
                          boundaries (split_chunks cuts equal parts).
  settled_first(clip, …)  one still per part: the speaker exactly as in the
                          part's FIRST frame, the graphics as they stand once
                          the entrance has settled. Flipping between a
                          version and the next preview (drawn on the
                          original's first frame) then keeps the speaker's
                          pose — a version shown at its playhead put a second
                          pose of the speaker on the stage.
  still(exp, j)           settled_first of part j, cached in the version's
                          own folder (GET /api/rebrand/harness/versions/{id}/still/{j}).
  load(spec, run, target) the version a request builds on, checked: this
                          clip's, ready, not stacked too deep (else the
                          owner's words, 404/409/410).
  stack_parts(bv, …)      a stack pass's starting point: each part of the
                          version conformed to the original part, its settled
                          first frame, and the ink and slots its graphics
                          hold (the planner keeps new patterns off them).
  hold_mask(layers, …)    where the version must stay exactly as it is.
  lost(exp)               a version made in house whose video is gone: it is
                          made again from its recipe and the original, for
                          free (studio.remake — POST /versions/{id}/remake).

Our icons are stamped after Omni, so a version that has them (an "icons"
stage) keeps each part's PRE-STAMP clip (Omni's clip fitted to the part,
before the speaker pass) and its CLEAN clip (that pass with none of our
icons), with how the pass ran (replays):

  an icon-only "Add on top"  runs the version's own pass again on its
                             pre-stamp parts with the new icons — no Omni;
  new layers on top          are drawn on its clean parts, so its icons are
                             never drawn twice (a cut from its final would
                             carry them baked in, so it is never used).

A version whose brief asked Omni for its own icons (omni_icons_in) cannot
take ours on top: those are redone without them ("Change it"), or kept.

The speaker mask is the local MODNet matte (speaker_front.person_matte) —
never a paid call. "Add on top" (stack mode) builds its target frames from
these same functions, so its preview and its run agree.

Opening the studio asks for every ready version's first still at once: the
parts of one version are found once for all its stills (two cutting the same
part out of the final would share a temp file), and at most STILL_SLOTS
stills or cuts run at a time, so they do not crowd out the scene's first
preview.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import subprocess
import weakref
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

from fastapi import HTTPException

from . import recipe as rc
from . import stages as st

log = logging.getLogger("archivist.rebrand.harness.base")

# How long an entering part's graphics take to settle: every entrance is over
# within its first second, and Omni's settle at the chunk seam is under one
# more. Capped at half the part for a short part.
BASE_SETTLE_SEC = 2.0
# Stills (two MODNet passes each) and cuts from a final drawn at once.
STILL_SLOTS = 2

MODES = ("stack", "rebuild")
# "Add on top" of an "Add on top" …: at most this many passes on top of the
# first version. Each pass is a second Omni generation inside its footprint,
# and a chain multiplies the cost.
MAX_STACK_DEPTH = 3
# The hold (stack mode): the new layers' footprint is their ink grown by this
# share of the frame's short side (Omni's lines land near, not exactly on,
# the target's), then feathered by this share, so where Omni's pass gives way
# to the version kept there is no hard edge.
HOLD_GROW_FRAC = 0.03
HOLD_FEATHER_FRAC = 0.015
# A target frame's pixel this far (any channel, of 255) from the frame it was
# drawn on carries a layer — a version's ink, which a new pattern keeps off.
INK_MIN = 12
# The original's first frame against the one a version was made from: more
# than this mean difference (of 255, grey, small) is another take of the clip.
SAME_SCENE_MAX = 10.0

# Why a version cannot be built on — shown to the owner as they are.
UNKNOWN = "There's no such version any more."
OTHER_CLIP = "That version is of another clip."
RUNNING = "That version is still being made."
NOT_MADE = "That version didn't finish, so there's nothing to build on — pick another."
TOO_DEEP = "That version has been added to as far as it goes — use Change it instead."
GONE = "That version's video is gone — use Change it instead."
CLIP_CHANGED = "This clip has changed since that version was made — use Change it instead."
OMNI_ICONS = ("That version already has Omni's own icons — use Change it to redo it without them, "
              "or keep them.")
KEEP_ONLY_ON_OMNI = "Keeping Omni's own icons is only for a version that already has them."
ADD_ON_HOUSE = ("That version was made in house — use Change it: it keeps everything it has and adds "
                "yours, the same picture.")
# Omni's pass on top of a version made in house would lose its layers (Omni
# edits the video under them): new work is made in house.
OMNI_ON_HOUSE = "That version was made in house — add to it in house."
# A house pass whose own layers can't be drawn again where they were.
PASS_UNREADABLE = "That version's own layers can't be drawn again — use Change it instead."
# Making a version again (POST /versions/{id}/remake).
REMAKE_OMNI = "Only a version made in house can be made again for free — Omni's can't be redrawn the same."
REMAKE_NOT_MADE = "That version didn't finish, so there's nothing to make again — use Try again."
# The one-speaker check on a pass drawn over a version Omni made (guards.one_speaker).
SECOND_PERSON = ("A second person shows at {t} in the video under it — Change it redoes it in house "
                 "from the original.")

Fetch = Callable[[int], Awaitable[Optional[dict]]]

_STILLS: dict[str, asyncio.Task] = {}
# chunk_sources in flight, per version (and destination).
_SOURCES: dict[str, asyncio.Task] = {}
_SLOTS: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Semaphore]" = \
    weakref.WeakKeyDictionary()


def _slots() -> asyncio.Semaphore:
    """This event loop's STILL_SLOTS (a semaphore belongs to one loop)."""
    loop = asyncio.get_running_loop()
    sem = _SLOTS.get(loop)
    if sem is None:
        sem = _SLOTS[loop] = asyncio.Semaphore(STILL_SLOTS)
    return sem


def _root() -> Path:
    # Late: the pipeline imports the harness, and tests move its root.
    from .. import rebrand_pipeline as pipeline

    return pipeline.REBRAND_ROOT


def version_dir(exp: dict) -> Path:
    return _root() / str(exp["id"])


def still_path(exp_id: int, j: int) -> Path:
    """Where part j's still is kept (still())."""
    return _root() / str(int(exp_id)) / "stills" / f"c{int(j)}.png"


def _omni(exp: dict) -> dict:
    return (exp.get("arms") or {}).get("omni") or {}


def _stages(exp: dict) -> dict:
    return _omni(exp).get("stages") or {}


def is_ready(exp: dict) -> bool:
    return exp.get("status") == "ready" and _omni(exp).get("status") == "ready"


def made_in_house(exp: dict) -> bool:
    """Whether the version was made by the in-house engine (its options say
    so, or its recipe does): our own graphics over the footage, no Omni."""
    opts = exp.get("options") or {}
    if opts.get("engine") == "house" or _omni(exp).get("engine") == "house":
        return True
    recipe = opts.get("recipe")
    return isinstance(recipe, dict) and recipe.get("engine") == "house"


def house_pass(exp: dict) -> bool:
    """Whether the version was made in house ON TOP of another ("Add on
    top" of a version Omni made): its own layers over that version's video,
    which stays under them (the module doc)."""
    b = base_of(exp)
    return made_in_house(exp) and bool(b) and b.get("mode") == "stack"


def ground_index(chain: list[dict]) -> Optional[int]:
    """Where in a chain (newest first) the video everything is drawn over
    is: the first version that is not a house pass. None when the chain
    ends on a house pass (the version under it could not be read)."""
    return next((i for i, e in enumerate(chain) if not house_pass(e)), None)


def recorded_under(exp: dict) -> Optional[Path]:
    """The video a version made in house was drawn over (its render stage
    says), when it is still on disk: the very bytes under its plate."""
    rec = (_stages(exp).get("render") or {}).get("c0") or {}
    p = Path(rec["under"]) if rec.get("under") else None
    return p if p is not None and p.is_file() else None


def label(exp: dict) -> str:
    """The run's file label for its scene: scene_0 is edited as "Scene 1",
    whose files are scene_1_c<j>_*.mp4 (runner._slug); an intro or outro
    made in house as "Intro" / "Outro" (intro.mp4)."""
    target = str((exp.get("options") or {}).get("target") or "")
    if target in ("intro", "outro"):
        return target
    m = re.fullmatch(r"scene_(\d{1,2})", target)
    return f"scene_{int(m.group(1)) + 1}" if m else "scene_1"


def _items(stage: dict) -> list[int]:
    return sorted(int(k[1:]) for k in stage if re.fullmatch(r"c\d+", k))


def part_count(exp: dict) -> int:
    """How many parts the version's scene was edited in: from its manifest
    (made in house: its one render), else from the run's own files, else
    one."""
    stages = _stages(exp)
    for name in ("render", "omni", "guards", "target"):
        found = _items(stages.get(name) or {})
        if found:
            return found[-1] + 1
    out = version_dir(exp) / "omni"
    n = 0
    while (out / f"{label(exp)}_c{n}.mp4").exists():
        n += 1
    return max(1, n)


def entering(exp: dict, j: int) -> bool:
    """Whether part j carries an entrance of what the version shows (the
    manifest says; a version without one entered on its first part, if
    anywhere). A pass added on top records `settles`: its own entrance or
    any of the version under it, which it keeps. Made in house, its render
    records it."""
    stages = _stages(exp)
    rec = (stages.get("render") or {}).get(f"c{j}") or {}
    if "settles" not in rec and "entering" not in rec:
        rec = (stages.get("omni") or {}).get(f"c{j}") or {}
    if "settles" in rec:
        return bool(rec["settles"])
    if "entering" in rec:
        return bool(rec["entering"])
    return j == 0


def _final(exp: dict) -> Optional[str]:
    """The version's final cut: its local file, else None (the bucket copy is
    looked up by chunk_sources, which may need the network)."""
    from .. import yas_talking_head_v2 as th

    rel = _omni(exp).get("final_path")
    if rel:
        p = th._abs_from_rel(rel)
        if p.exists():
            return str(p)
    return None


def _recorded_part(exp: dict, j: int) -> Optional[Path]:
    """Part j's clip where the version's manifest says it is, when it is on
    disk: the guards' output (every pass made by Omni), else — made in house
    — its render."""
    stages = _stages(exp)
    for name in ("guards", "render"):
        rec = (stages.get(name) or {}).get(f"c{j}") or {}
        if rec.get("path") and Path(rec["path"]).is_file():
            return Path(rec["path"])
    return None


def final_file(exp: dict) -> Path:
    """Where a version made in house keeps its video: its arm's final, else
    its render (the run's own folder) — whether or not it is there now."""
    from .. import yas_talking_head_v2 as th

    rel = _omni(exp).get("final_path")
    if rel:
        return th._abs_from_rel(rel)
    rec = (_stages(exp).get("render") or {}).get("c0") or {}
    if rec.get("path"):
        return Path(rec["path"])
    return version_dir(exp) / "omni" / f"{label(exp)}.mp4"


def lost(exp: dict) -> bool:
    """A finished version made in house whose video is gone from this
    computer, with no copy in the bucket to stand in: it can be made again,
    for free (POST /versions/{id}/remake — its recipe and the original are
    all it is made of)."""
    if not (is_ready(exp) and made_in_house(exp)):
        return False
    arm = _omni(exp)
    return not final_file(exp).is_file() and not (arm.get("storage_bucket") and arm.get("storage_path"))


def _cut(src: str, out: Path, *, start: float, seconds: float) -> Path:
    """[start, start+seconds) of `src` (a path or a URL), re-encoded so the
    part starts on its own first frame."""
    from .. import rebrand_media as media

    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_name(out.stem + ".part" + out.suffix)
    cmd = [media.ffmpeg_exe(), "-y", "-loglevel", "error", "-i", src,
           "-ss", f"{start:.3f}", "-t", f"{seconds:.3f}",
           "-c:v", "libx264", "-preset", "veryfast", "-crf", "16",
           "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart", str(tmp)]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    if proc.returncode != 0 or not tmp.exists():
        raise RuntimeError(f"cutting part of the version failed: {(proc.stderr or '')[-300:]}")
    os.replace(tmp, out)
    return out


async def chunk_sources(exp: dict, out_dir: Optional[Path] = None) -> list[Path]:
    """The version's final clip per part, in part order (see the module
    doc). Raises FileNotFoundError when neither the parts nor the final can
    be found. Concurrent callers for one version share the work."""
    key = f"{exp.get('id')}|{out_dir or ''}"
    task = _SOURCES.get(key)
    if task is None or task.done():
        task = _SOURCES[key] = asyncio.ensure_future(_chunk_sources(exp, out_dir))

        def forget(t: asyncio.Task, k: str = key) -> None:
            if _SOURCES.get(k) is t:
                _SOURCES.pop(k, None)
            if not t.cancelled():
                t.exception()                     # retrieved: a failure is the caller's

        task.add_done_callback(forget)
    return list(await asyncio.shield(task))


async def _chunk_sources(exp: dict, out_dir: Optional[Path]) -> list[Path]:
    from .. import asset_storage
    from .. import rebrand_media as media

    n = part_count(exp)
    run_dir = version_dir(exp) / "omni"
    found: list[Optional[Path]] = []
    for j in range(n):
        pick = _recorded_part(exp, j)
        if pick is None:
            for name in (f"{label(exp)}_c{j}_front.mp4", f"{label(exp)}_c{j}.mp4"):
                if (run_dir / name).is_file():
                    pick = run_dir / name
                    break
        found.append(pick)
    if all(found):
        return [p for p in found if p is not None]

    # Cut the parts out of the final: split_chunks cut the scene into n equal
    # parts, and the final is those parts joined back in order.
    src = _final(exp)
    arm = _omni(exp)
    if src is None and arm.get("storage_bucket") and arm.get("storage_path"):
        # ffmpeg reads it from here, the server: the internal URL, not the
        # public /media one a browser gets.
        src = await asset_storage.url_for(storage_bucket=arm["storage_bucket"],
                                          storage_path=arm["storage_path"],
                                          local_path=None, expires=3600, internal=True)
    if not src:
        raise FileNotFoundError(f"version #{exp.get('id')} has no clips left to show")
    if n == 1 and Path(src).is_file():
        return [Path(src)]
    dest = out_dir or (version_dir(exp) / "parts")
    total = await asyncio.to_thread(media.probe_duration, Path(src)) if Path(src).is_file() \
        else float(arm.get("duration_sec") or 0.0)
    if not total:
        raise FileNotFoundError(f"version #{exp.get('id')}: the final's length is unknown")
    step = total / n
    out: list[Path] = []
    for j in range(n):
        if found[j] is not None:
            out.append(found[j])  # type: ignore[arg-type]
            continue
        piece = dest / f"c{j}_src.mp4"
        if not piece.exists():
            async with _slots():
                await asyncio.to_thread(_cut, src, piece, start=j * step, seconds=step)
        out.append(piece)
    return out


def _read_frames(clip: Path, last: int) -> tuple[Any, Any]:
    """(frame 0, frame `last` — or the clip's last frame when it is
    shorter), BGR uint8, read in order so the frame is exact."""
    import cv2

    cap = cv2.VideoCapture(str(clip))
    try:
        ok, first = cap.read()
        if not ok:
            raise RuntimeError(f"no frames in {Path(clip).name}")
        at = first
        for _ in range(max(0, last)):
            ok, f = cap.read()
            if not ok:
                break
            at = f
    finally:
        cap.release()
    return first, at


def _fps_and_seconds(clip: Path) -> tuple[float, float]:
    import cv2

    cap = cv2.VideoCapture(str(clip))
    try:
        fps = cap.get(cv2.CAP_PROP_FPS) or 0.0
        count = cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0.0
    finally:
        cap.release()
    fps = fps if fps > 1 else 30.0
    return fps, (count / fps if count > 0 else 0.0)


def settled_first(clip: Path, out: Path, *, entering: bool, settle_sec: float = BASE_SETTLE_SEC,
                  matte: Optional[Callable[[Any], Any]] = None, at_sec: Optional[float] = None) -> Path:
    """One part's still (synchronous): the speaker from its first frame over
    the background as it stands at t_s — BASE_SETTLE_SEC in (at most half
    the part) on the part that carries the entrance, else frame 0 itself
    (`at_sec`: that moment instead, part seconds — a version's icon fully in):

        bg = S(t_s) * (1 - M_ts) + S(0) * M_ts
        F  = bg * (1 - M_0) + S(0) * M_0

    where M is the local speaker matte (speaker_front.person_matte). Without
    a matte the settled frame is used whole: the graphics matter more than
    the pose there. An opaque PNG, written atomically."""
    import cv2
    import numpy as np

    fps, seconds = _fps_and_seconds(clip)
    t_s = min(settle_sec, seconds / 2.0) if entering else 0.0
    if at_sec is not None:
        t_s = max(0.0, min(float(at_sec), seconds - 1.0 / max(fps, 1.0)))
    k = int(round(t_s * fps))
    f0, fs = _read_frames(clip, k)
    frame = f0
    if k > 0:
        if matte is None:
            from .. import speaker_front

            matte = speaker_front.person_matte
        try:
            m0 = np.clip(np.asarray(matte(f0), dtype=np.float32), 0.0, 1.0)[:, :, None]
            ms = np.clip(np.asarray(matte(fs), dtype=np.float32), 0.0, 1.0)[:, :, None]
        except Exception as e:  # noqa: BLE001 — no matte: the settled frame as it is
            log.warning("[base] no speaker matte for %s (%s): using the settled frame",
                        Path(clip).name, str(e)[:160])
            frame = fs
        else:
            a, s = f0.astype(np.float32), fs.astype(np.float32)
            bg = s * (1.0 - ms) + a * ms
            frame = np.clip(np.rint(bg * (1.0 - m0) + a * m0), 0, 255).astype(np.uint8)
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_name(out.stem + ".part.png")
    if not cv2.imwrite(str(tmp), frame):
        raise RuntimeError(f"could not write {out.name}")
    os.replace(tmp, out)
    return out


# A version's still shows its first icon this long after it is fully in (the
# studio's exact-frame moment, elements/icon.MOMENT_AFTER).
ICON_STILL_AFTER = 0.4


def icon_still_at(exp: dict, j: int) -> Optional[float]:
    """Where in part j (part seconds) a version with our icons shows its
    first icon fully in — its still is taken there, so the icons show in it
    (the speaker still from frame 0). None: no icon of ours in that part."""
    parts = icon_parts(exp)
    if j >= len(parts) or not isinstance(parts[j].get("span"), list):
        return None
    start, sec = (float(x) for x in parts[j]["span"][:2])
    # Fully in for a moment inside this part (not one just leaving at its start).
    on = sorted((r for r in icons_of(exp)
                 if float(r["settle_sec"]) < start + sec and float(r["to_sec"]) > start + 0.2),
                key=lambda r: float(r["settle_sec"]))
    if not on:
        return None
    r = on[0]
    at = max(float(r["settle_sec"]) - start, 0.0) + ICON_STILL_AFTER
    return round(min(at, float(r["to_sec"]) - start - 0.1, sec - 0.1), 3)


async def still(exp: dict, j: int) -> Path:
    """Part j's settled first frame (with a version's own icons: at its
    first icon's moment), made once and kept in the version's folder.
    Concurrent callers share the work. A version made in house (one part)
    whose run's still is gone gets its design still again (house.
    version_still: frame 0's speaker, never two moments of the footage
    mixed); when that can't be, one is taken from its video as for any."""
    out = still_path(exp["id"], j)
    if out.exists():
        return out
    key = str(out)
    task = _STILLS.get(key)
    if task is None:
        async def make() -> Path:
            if made_in_house(exp) and j == 0:
                from .house import version_still

                try:
                    async with _slots():
                        return await version_still(exp, out)
                except Exception as e:  # noqa: BLE001 — taken from its video instead
                    log.warning("[base] #%s: its design still could not be made again (%s)", exp.get("id"),
                                str(e)[:200])
            sources = await chunk_sources(exp)
            at = icon_still_at(exp, j)
            async with _slots():
                if at is None:
                    return await asyncio.to_thread(settled_first, sources[j], out,
                                                   entering=entering(exp, j))
                return await asyncio.to_thread(settled_first, sources[j], out,
                                               entering=entering(exp, j), at_sec=at)

        task = _STILLS[key] = asyncio.ensure_future(make())
        task.add_done_callback(lambda t, k=key: (_STILLS.pop(k, None),
                                                 t.cancelled() or t.exception()))
    return await asyncio.shield(task)


def still_urls(exp: dict) -> list[str]:
    """The version's per-part still URLs (only for a ready version)."""
    if not is_ready(exp):
        return []
    return [f"/api/rebrand/harness/versions/{exp['id']}/still/{j}" for j in range(part_count(exp))]


# ---------------------------------------------------------------------------
# Lineage: what a version was built on, and the layers it really shows
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class BaseSpec:
    """What a new version is built on, as the request (and then its
    options.base) says: the version, "stack" or "rebuild", and — rebuild
    only — `fresh`: a new take, nothing reused ("Try again")."""
    version_id: int
    mode: str = "stack"
    fresh: bool = False

    @classmethod
    def of(cls, raw: Any) -> Optional["BaseSpec"]:
        """A spec from a request model, a dict (options.base) or a spec;
        None for nothing, or for anything that is not one."""
        if raw is None or isinstance(raw, BaseSpec):
            return raw
        if hasattr(raw, "model_dump"):
            raw = raw.model_dump()
        if not isinstance(raw, dict) or raw.get("mode") not in MODES:
            return None
        try:
            vid = int(raw.get("version_id"))
        except (TypeError, ValueError):
            return None
        return cls(version_id=vid, mode=raw["mode"],
                   fresh=bool(raw.get("fresh")) and raw["mode"] == "rebuild")

    def public(self) -> dict:
        """As options.base stores it (and the API shows it)."""
        out: dict[str, Any] = {"version_id": self.version_id, "mode": self.mode}
        if self.fresh:
            out["fresh"] = True
        return out


@dataclass
class BaseVersion:
    """A checked version to build on (load), and — stack mode, once
    stack_parts ran — the pass's starting point."""
    spec: BaseSpec
    exp: dict                              # the version's row
    chain: list[dict]                      # it, then each version it was stacked on
    layers: list[dict]                     # its effective layers (layers_of)
    chunks: list[Path] = field(default_factory=list)    # the ORIGINAL's parts
    sources: list[Path] = field(default_factory=list)   # the version's parts, conformed to them
    frames: list[Path] = field(default_factory=list)    # each part's settled first frame
    occupied: Any = None                   # its graphics' ink on part 0 (float HxW)
    taken: dict[str, str] = field(default_factory=dict)  # the slots its patterns hold -> names
    # How each part is made again with other icons (stack_parts): the
    # version's own pass for one with our icons (replays), else the part.
    replays: list = field(default_factory=list)
    # A house pass (flattened onto its ground): the video its last pass was
    # drawn over, when it is still on disk (else made from `sources`), and
    # every pass's own patterns as they froze them (house.flatten: None
    # until then; [] for passes that drew none).
    under: Optional[Path] = None
    kept: Optional[list] = None

    @property
    def version_id(self) -> int:
        return int(self.exp["id"])

    @property
    def mode(self) -> str:
        return self.spec.mode

    @property
    def depth(self) -> int:
        """How many passes were added on top of the chain's first version."""
        return len(self.chain) - 1

    @property
    def ground(self) -> Optional[dict]:
        """The version whose video a pass on top is drawn over: the version
        itself, or — a house pass — the one Omni made under its passes
        (None when that could not be read)."""
        i = ground_index(self.chain)
        return self.chain[i] if i is not None else None

    @property
    def passes(self) -> list[dict]:
        """The house passes between the ground and the version (it
        included), oldest first: their layers are drawn again over the
        ground's video. [] for a version Omni made."""
        i = ground_index(self.chain)
        return list(reversed(self.chain[:len(self.chain) if i is None else i]))


def base_of(exp: dict) -> Optional[dict]:
    from .. import rebrand_store as store

    return store.base_of(exp)


def target_of(exp: dict) -> str:
    return str((exp.get("options") or {}).get("target") or "reel")


def take_of(exp: dict) -> str:
    """The version's Omni take (stages): its own id, unless it kept the take
    of the version it was changed from."""
    return str((exp.get("options") or {}).get("take") or exp["id"])


def omni_stages(exp: dict) -> dict:
    """The version's manifest (what a rebuild may reuse)."""
    return _stages(exp)


def recipe_of(exp: dict) -> Optional[rc.Recipe]:
    """The recipe the version followed: its own, else the one its legacy
    options imply; None when neither reads."""
    opts = exp.get("options") or {}
    try:
        if opts.get("recipe"):
            return rc.normalize(opts["recipe"])
        return rc.recipe_from_options(opts)
    except Exception:  # noqa: BLE001 — an old or odd row: no layers known
        return None


def _slot(layout: dict) -> str:
    """pattern_planner.slot of a Layout.public()."""
    from .. import pattern_planner
    from .. import rebrand_pattern_ref as pattern_ref

    try:
        return pattern_planner.slot(pattern_ref.Layout(**layout))
    except (TypeError, ValueError):
        return "full"


def _placed(exp: dict, el: rc.Element, first: bool) -> Optional[dict]:
    """Where the version really drew one of its patterns — the place its
    planner chose, from its evidence — or None when it is not known."""
    ev = exp.get("evidence") or {}
    many = ev.get("omni_patterns")
    if many:
        found = next((p for p in many if isinstance(p, dict) and p.get("element_id") == el.id), None)
    else:
        found = ev.get("omni_pattern") if first else None
    if not isinstance(found, dict) or not isinstance(found.get("layout"), dict):
        return None
    # Moved by hand, it held no side or corner of its own: a new pattern
    # keeps off its ink only (elements/pattern.hand_slot).
    held = f"hand:{el.id}" if rc.is_hand(el) else _slot(found["layout"])
    return {"slug": found.get("slug"), "name": found.get("name") or found.get("slug"),
            "where": found.get("where"), "layout": found["layout"], "slot": held}


def own_layers(exp: dict) -> list[dict]:
    """The layers the version's own pass drew: its recipe's switched-on
    patterns, each as the recipe holds it plus `from_version` and `placed`
    (where it really went, or None)."""
    recipe = recipe_of(exp)
    if recipe is None:
        return []
    out = []
    for i, el in enumerate(rc.pattern_elements(recipe)):
        out.append({**el.model_dump(mode="json"), "from_version": int(exp["id"]),
                    "placed": _placed(exp, el, i == 0)})
    return out


def layers_of(chain: list[dict]) -> list[dict]:
    """The layers a chain shows, back to front: the first version's, then
    each pass added on top of it."""
    out: list[dict] = []
    for exp in reversed(chain):
        out += own_layers(exp)
    return out


async def chain_of(exp: dict, fetch: Fetch) -> list[dict]:
    """The version, then each version it was stacked on ("Add on top"),
    newest first — up to the first that was not (a fresh or a rebuilt
    version draws everything it shows). A missing link ends the chain."""
    out = [exp]
    seen = {exp.get("id")}
    while len(out) <= MAX_STACK_DEPTH:
        b = base_of(out[-1])
        if not b or b.get("mode") != "stack":
            break
        try:
            parent = await fetch(int(b["version_id"]))
        except Exception:  # noqa: BLE001 — shown with the layers known so far
            log.warning("[base] version #%s: its base #%s could not be read", exp.get("id"),
                        b.get("version_id"))
            parent = None
        if not parent or parent.get("id") in seen:
            break
        seen.add(parent.get("id"))
        out.append(parent)
    return out


async def effective_layers(exp: dict, fetch: Fetch) -> list[dict]:
    """Every layer the version shows, each with the version that drew it."""
    return layers_of(await chain_of(exp, fetch))


def elements_of(layers: list[dict]) -> list[rc.Element]:
    """Effective layers as recipe elements (pattern_el.check's `chain`)."""
    keys = set(rc.Element.model_fields)
    out = []
    for layer in layers:
        try:
            out.append(rc.Element.model_validate({k: v for k, v in layer.items() if k in keys}))
        except Exception:  # noqa: BLE001 — an unreadable old layer counts for nothing
            continue
    return out


def _refuse(status: int, message: str) -> HTTPException:
    return HTTPException(status, message)


async def load(spec: BaseSpec, run_id: int, target: Optional[str], *,
               fetch: Optional[Fetch] = None) -> BaseVersion:
    """The version `spec` builds on, checked: it exists (404), is this
    clip's (409), is ready with its Omni result (409: still being made, or
    it did not finish), and — stack — can still be added to (409) and still
    has its clips (410). The messages are the owner's words."""
    from .. import rebrand_store as store

    fetch = fetch or store.get_experiment
    exp = await fetch(int(spec.version_id))
    if not exp:
        raise _refuse(404, UNKNOWN)
    if int(exp.get("run_id") or 0) != int(run_id) or target_of(exp) != (target or "reel"):
        raise _refuse(409, OTHER_CLIP)
    if exp.get("status") in store.ACTIVE_STATUSES:
        raise _refuse(409, RUNNING)
    if not is_ready(exp):
        raise _refuse(409, NOT_MADE)
    chain = await chain_of(exp, fetch)
    if spec.mode == "stack":
        if len(chain) - 1 >= MAX_STACK_DEPTH:
            raise _refuse(409, TOO_DEEP)
        # A house pass is added to over the video it was drawn over (its
        # ground's): that is what must still be there.
        i = ground_index(chain)
        if i is None or not sources_known(chain[i]):
            raise _refuse(410, GONE)
    return BaseVersion(spec=spec, exp=exp, chain=chain, layers=layers_of(chain))


def sources_known(exp: dict) -> bool:
    """Whether chunk_sources can still find (or cut) the version's parts —
    looked up, nothing cut. A version with our icons is built on from its
    clean parts only (on disk or in the bucket): never cut from its final,
    which has them baked in."""
    if has_icons(exp):
        recs = icon_parts(exp)
        return bool(recs) and all(_file_known((r.get("files") or {}).get("clean"))
                                  and _file_known((r.get("files") or {}).get("prestamp")) for r in recs)
    run_dir = version_dir(exp) / "omni"
    for j in range(part_count(exp)):
        if _recorded_part(exp, j) is not None:
            continue
        if any((run_dir / n).is_file() for n in (f"{label(exp)}_c{j}_front.mp4", f"{label(exp)}_c{j}.mp4")):
            continue
        arm = _omni(exp)
        return bool(_final(exp) or (arm.get("storage_bucket") and arm.get("storage_path")))
    return True


# ---------------------------------------------------------------------------
# Icons: what a version stamped, and how to make its parts again
# ---------------------------------------------------------------------------

# The files a part of a version with our icons keeps, by name: Omni's clip
# fitted to the part before the speaker pass, that pass without our icons,
# and what the pass read besides the original (the veil's keep mask, the
# stray-drawing clean-up's footprint, and — added on top of another version —
# its backdrop and hold).
KEPT_FILES = ("prestamp", "clean", "veil_keep", "backdrop", "hold", "footprint")
_KEPT_MASKS = ("veil_keep", "hold", "footprint")         # .npy; the rest .mp4


def icon_stage(exp: dict) -> dict:
    return _stages(exp).get("icons") or {}


def icon_parts(exp: dict) -> list[dict]:
    """The version's icons records, one per part, in part order."""
    stage = icon_stage(exp)
    return [stage[f"c{j}"] for j in _items(stage)]


def has_icons(exp: dict) -> bool:
    """Whether the version's run laid our icons in (its final has them)."""
    return bool(icon_parts(exp))


def icons_of(exp: dict) -> list[dict]:
    """The icons the version shows, as they were stamped (IconResolved
    dicts): its icons stage's plan, else its recipe's frozen plan."""
    rec = icon_stage(exp).get("plan") or {}
    if isinstance(rec.get("resolved"), list):
        return list(rec["resolved"])
    recipe = recipe_of(exp)
    el = rc.icon_element(recipe) if recipe is not None else None
    if el is None:
        return []
    try:
        props = rc.icon_props(el)
    except rc.RecipeError:
        return []
    return [r.model_dump(mode="json") for r in props.resolved or []]


def inherited_icons(recipe: rc.Recipe, bv: Optional["BaseVersion"]) -> Optional[list]:
    """The icons a pass on top stamps when its recipe has no icon element
    at all: those the version under it shows — a house pass's exactly as it
    stamped them (its own, or the ones it carried over; none when it took
    them away), a version Omni made its own when it has ours. None: none
    are carried over (or the recipe has its own)."""
    if bv is None or bv.mode != "stack" or rc.icon_element(recipe, enabled=False) is not None:
        return None
    if bv.passes:
        return icons_of(bv.exp) or None
    return icons_of(bv.exp) if has_icons(bv.exp) else None


def omni_drew_icons(exp: dict) -> bool:
    """Whether Omni was asked for its own icons on this version's pass: its
    opening came from the brief (which asks for a few beside the speaker
    unless told the pipeline places them) and the pipeline did not. A pass
    opened with the harness's own words asked for none, and one made in
    house asked Omni for nothing at all."""
    opts = exp.get("options") or {}
    if opts.get("icons_by_pipeline") or made_in_house(exp):
        return False
    recipe = recipe_of(exp)
    if recipe is not None and recipe.director.head == "harness":
        return False
    if (exp.get("evidence") or {}).get("brief_head_fallback"):
        return False                     # the brief's opening gave way to ours
    return True


def omni_icons_in(chain: list[dict]) -> bool:
    """Whether a version (or one under it) shows Omni's own icons."""
    return any(omni_drew_icons(e) for e in chain)


def icon_refusal(recipe: rc.Recipe, bv: Optional["BaseVersion"]) -> Optional[tuple[int, str]]:
    """Why a recipe's icons cannot go with the version it builds on, as
    (status, the owner's words) — or None. Omni's own icons are kept
    (omni_own "keep") only on a version that has them; and ours never go
    ON TOP of one that has them (two sets): it is redone without them
    ("Change it") or they are kept."""
    el = rc.icon_element(recipe)
    if el is None:
        return None
    try:
        props = rc.icon_props(el)
    except rc.RecipeError:
        return None                        # normalize() refuses it with its reasons
    theirs = bv is not None and omni_icons_in(bv.chain)
    if props.omni_own == "keep" and not theirs:
        return 422, KEEP_ONLY_ON_OMNI
    if props.omni_own == "hide" and theirs and bv.mode == "stack":
        return 409, OMNI_ICONS
    return None


def _file_known(rec: Optional[dict]) -> bool:
    if not isinstance(rec, dict):
        return False
    if rec.get("path") and Path(rec["path"]).is_file():
        return True
    return bool(rec.get("bucket") and rec.get("key"))


async def kept_file(rec: Optional[dict], dest: Path) -> Optional[Path]:
    """A file a version kept ({path, bucket?, key?}): where it was written,
    else fetched once from the bucket to `dest`. None when neither."""
    if not isinstance(rec, dict):
        return None
    if rec.get("path") and Path(rec["path"]).is_file():
        return Path(rec["path"])
    if not (rec.get("bucket") and rec.get("key")):
        return None
    if dest.is_file():
        return dest
    from .. import asset_storage

    try:
        data = await asset_storage.download_bytes(storage_bucket=rec["bucket"], storage_path=rec["key"])
    except Exception:  # noqa: BLE001 — gone from the bucket too
        log.warning("[base] a kept file could not be fetched from %s/%s", rec.get("bucket"), rec.get("key"))
        return None
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.stem + ".part" + dest.suffix)
    tmp.write_bytes(data)
    os.replace(tmp, dest)
    return dest


@dataclass
class Replay:
    """One part of a version made again with other icons: the pass that
    made it (restore_speaker on `edited` against the original part, with
    these guards and — added on top of another version — its backdrop and
    hold, and its stray-drawing clean-up), and that pass's output without
    our icons (`clean`). `files`: where each came from, handed on to the
    next pass's icons stage."""
    edited: Path
    clean: Path
    clear_veil_sec: float = 0.0
    clear_bars: bool = True
    veil_keep: Optional[Path] = None           # .npy (float HxW)
    backdrop: Optional[Path] = None
    hold: Optional[Path] = None                # .npy (float HxW)
    files: dict = field(default_factory=dict)
    footprint: Optional[Path] = None           # .npy (float HxW)

    def kwargs(self) -> dict:
        """restore_speaker's arguments for the pass (the arrays loaded)."""
        import numpy as np

        out: dict[str, Any] = {"clear_veil_sec": float(self.clear_veil_sec), "clear_bars": bool(self.clear_bars)}
        if self.veil_keep is not None:
            out["veil_keep"] = np.load(self.veil_keep)
        if self.backdrop is not None:
            out["backdrop"] = self.backdrop
        if self.hold is not None:
            out["hold"] = np.load(self.hold)
        if self.footprint is not None:
            out["footprint"] = np.load(self.footprint)
        return out

    def record(self) -> dict:
        """As an icons record keeps it (guards, files)."""
        return {"guards": {"clear_veil_sec": float(self.clear_veil_sec), "clear_bars": bool(self.clear_bars)},
                "files": dict(self.files)}


def replay_of_source(source: Path) -> Replay:
    """A part of a version WITHOUT our icons, made again with some: the pass
    runs on the part itself (the speaker put back from the original, the
    bars cleared — the speaker is never in one), and the part is its clean."""
    source = Path(source)
    return Replay(edited=source, clean=source, clear_veil_sec=0.0, clear_bars=True,
                  files={"prestamp": {"path": str(source)}, "clean": {"path": str(source)}})


async def replays(exp: dict, out_dir: Path) -> list[Replay]:
    """Every part of a version WITH our icons, as it can be made again: its
    pre-stamp clip, its guards and inputs, its clean clip — each on disk or
    fetched from the bucket into `out_dir`. FileNotFoundError when a part's
    pre-stamp or clean clip is gone (it is never cut from the final, which
    has the icons in it)."""
    out: list[Replay] = []
    for j, rec in enumerate(icon_parts(exp)):
        files = dict(rec.get("files") or {})
        got: dict[str, Optional[Path]] = {}
        for name in KEPT_FILES:
            if name not in files:
                got[name] = None
                continue
            suffix = ".npy" if name in _KEPT_MASKS else ".mp4"
            got[name] = await kept_file(files[name], Path(out_dir) / f"c{j}_{name}{suffix}")
            if got[name] is None:
                raise FileNotFoundError(f"version #{exp.get('id')} part {j + 1}: its {name} is gone")
        if got["prestamp"] is None or got["clean"] is None:
            raise FileNotFoundError(f"version #{exp.get('id')} part {j + 1} has no pre-stamp clip")
        g = rec.get("guards") or {}
        out.append(Replay(edited=got["prestamp"], clean=got["clean"],
                          clear_veil_sec=float(g.get("clear_veil_sec") or 0.0),
                          clear_bars=bool(g.get("clear_bars", True)), veil_keep=got["veil_keep"],
                          backdrop=got["backdrop"], hold=got["hold"], files=files,
                          footprint=got["footprint"]))
    if not out:
        raise FileNotFoundError(f"version #{exp.get('id')} has no icon parts")
    return out


# ---------------------------------------------------------------------------
# "Rebuild": when the version's brief may open the new run too
# ---------------------------------------------------------------------------

def _norm(v: Any) -> Any:
    return json.loads(json.dumps(v, sort_keys=True, default=str))


def places_of(exp: dict) -> list[tuple]:
    """The patterns the version's brief was told about, in order: (slug,
    layout, colour) from its evidence."""
    ev = exp.get("evidence") or {}
    many = ev.get("omni_patterns")
    found = many if many else ([ev["omni_pattern"]] if isinstance(ev.get("omni_pattern"), dict) else [])
    return _norm([[p.get("slug"), p.get("layout"), (p.get("colour") or "").lower()]
                  for p in found if isinstance(p, dict)])


def brief_reusable(recipe: Optional[rc.Recipe], refs: Any, options: dict, exp: dict, *,
                   first: Optional[Path] = None) -> bool:
    """Whether the version's brief may open a rebuild of it: the same
    director (who writes the opening, its feel and notes), the same patterns
    in the same places and colours (slug, layout and the colour drawn — what
    the brief was shown and told: its opening names them, "the attached
    purple line-art pattern", "graphics ONLY in the left half"), and the
    same icons rule. Any of those changed means a new brief.

    `first`: the first frame of the clip the rebuild edits. When the
    version's own first frame is still on disk and this one is another take
    of the clip (the scene was made again since), the brief describes
    footage that is gone — a new one."""
    from . import prompt as hprompt

    theirs = recipe_of(exp)
    if recipe is None or theirs is None or not (exp.get("brief") or {}).get("omni_scene_prompt"):
        return False
    if _norm(recipe.director.model_dump(mode="json")) != _norm(theirs.director.model_dump(mode="json")):
        return False
    mine_icons = bool(options.get("icons_by_pipeline"))
    if mine_icons != bool((exp.get("options") or {}).get("icons_by_pipeline")):
        return False
    ours = _norm([[r.slug, r.layout.public(), (r.colour or "").lower()] for r in hprompt.as_refs(refs)])
    if ours != places_of(exp):
        return False
    return not (first is not None and clip_changed(exp, first))


def clip_changed(exp: dict, first: Path) -> bool:
    """Whether `first` (the clip's first frame now) is another take of the
    clip than the one the version was made from — judged against the frame
    its first part was drawn on, while that is still on disk (unknown: not
    changed). A version added on top of another drew on that one's frames,
    not the original's: unknown too."""
    b = base_of(exp)
    if b and b.get("mode") == "stack":
        return False
    theirs = _target_pair(exp, 0)
    if theirs is None:
        return False
    diff = _first_diff(Path(first), theirs[1])
    return diff is not None and diff > SAME_SCENE_MAX


# ---------------------------------------------------------------------------
# "Add on top": the pass's starting point, the version's ink, the hold
# ---------------------------------------------------------------------------

def quantised(mask: Any) -> Any:
    """A speaker mask in the 0..255 steps its .person.png cache holds. The
    studio's mask of a frame may come from that cache and a run's is fresh,
    so both are made this before a target is drawn on a version's frame —
    the preview and the run then draw the same bytes."""
    if mask is None:
        return None
    import numpy as np

    m = np.clip(np.rint(np.asarray(mask, dtype=np.float32) * 255.0), 0, 255).astype(np.uint8)
    return m.astype(np.float32) / 255.0


async def run_mask(frame: Path) -> Any:
    """A stack run's speaker mask for one of its frames (person_matte, as
    every run's), quantised as the studio's."""
    from .. import person_matte

    return quantised(await person_matte.person_mask(Path(frame)))


def _first_diff(a: Path, b: Path) -> Optional[float]:
    """Mean grey difference (of 255) of two frames at a small size, or None
    when either cannot be read."""
    import numpy as np
    from PIL import Image

    try:
        with Image.open(a) as ia, Image.open(b) as ib:
            x = np.asarray(ia.convert("L").resize((90, 160)), dtype=np.float32)
            y = np.asarray(ib.convert("L").resize((90, 160)), dtype=np.float32)
    except OSError:
        return None
    return float(np.abs(x - y).mean())


def _target_pair(exp: dict, j: int) -> Optional[tuple[Path, Path]]:
    """(part j's target frame, the frame it was drawn on) of one pass, when
    both are still on disk."""
    rec = (_stages(exp).get("target") or {}).get(f"c{j}") or {}
    run_dir = version_dir(exp) / "omni"
    tgt = Path(rec["path"]) if rec.get("path") else run_dir / f"{label(exp)}_c{j}_target.png"
    frame = Path(rec["frame"]) if rec.get("frame") else run_dir / f"{label(exp)}_c{j}_first.png"
    return (tgt, frame) if tgt.is_file() and frame.is_file() else None


def ink(chain: list[dict], j: int, width: int, height: int) -> Any:
    """What a version and every pass under it laid into part j, float HxW
    (0 or 1): wherever a pass's TARGET frame differs from the frame it was
    drawn on. Both are our own composites, so the difference is the layers
    exactly — not Omni's re-render of the room. A pass whose frames are gone
    adds nothing (its patterns' slots still count: taken_slots)."""
    import numpy as np
    from PIL import Image

    acc = np.zeros((height, width), dtype=np.float32)
    for exp in chain:
        pair = _target_pair(exp, j)
        if pair is None:
            continue
        try:
            with Image.open(pair[0]) as t, Image.open(pair[1]) as f:
                a = np.asarray(t.convert("RGB").resize((width, height)), dtype=np.int16)
                b = np.asarray(f.convert("RGB").resize((width, height)), dtype=np.int16)
        except OSError:
            continue
        acc = np.maximum(acc, (np.abs(a - b).max(axis=2) >= INK_MIN).astype(np.float32))
    return acc


def taken_slots(layers: list[dict]) -> dict[str, str]:
    """{slot: name} of the effective layers placed where the version drew
    them (a new pattern never shares one)."""
    out: dict[str, str] = {}
    for layer in layers:
        placed = layer.get("placed") or {}
        if placed.get("slot"):
            out.setdefault(placed["slot"], placed.get("name") or placed.get("slug") or "a pattern")
    return out


async def stack_parts(bv: BaseVersion, chunks: list[Path], out_dir: Path, *,
                      width: int, height: int) -> BaseVersion:
    """A stack pass's starting point, in `out_dir` (made once; the studio's
    preview and the run make it the same way, from the same files, so the
    frames are the same bytes):

      sources   each of the version's parts conformed to the ORIGINAL part
                (media.conform: its frame count, its audio) — what Omni edits;
                for a version with our icons, its CLEAN parts (never a cut
                of its final: the icons are baked into that);
      frames    each part's settled first frame (settled_first) — what the
                new layers are drawn on, the target and the plan frame;
      occupied  the version's ink on part 0 and `taken` its slots — what a
                new pattern keeps off;
      replays   how each part is made again with other icons.

    A house pass (bv.passes) is flattened onto its ground: the sources,
    frames and replays are the GROUND's (the video its passes were drawn
    over), and `under` is the very file its last pass was drawn over while
    it is on disk; house.flatten then lays the passes' own layers on the
    frames and adds their ink.

    410 when the version's clips are gone; 409 when the clip is not the one
    the version was made from (another take, another length)."""
    from .. import rebrand_media as media

    ground = bv.ground
    if ground is None:
        raise _refuse(410, GONE)
    kept: list[Replay] = []
    try:
        if has_icons(ground):
            kept = await replays(ground, out_dir / "kept")
            raw = [r.clean for r in kept]
        else:
            raw = await chunk_sources(ground, out_dir / "parts")
    except FileNotFoundError as e:
        raise _refuse(410, GONE) from e
    if len(raw) != len(chunks):
        raise _refuse(409, CLIP_CHANGED)
    out_dir.mkdir(parents=True, exist_ok=True)
    theirs = _target_pair(bv.chain[-1], 0) if bv.chain else None
    if theirs is not None:
        ours = out_dir / "c0_original.png"
        if not ours.exists():
            await asyncio.to_thread(media.extract_frame, chunks[0], ours, at="first")
        diff = await asyncio.to_thread(_first_diff, ours, theirs[1])
        if diff is not None and diff > SAME_SCENE_MAX:
            raise _refuse(409, CLIP_CHANGED)
    sources: list[Path] = []
    frames: list[Path] = []
    for j, (src, chunk) in enumerate(zip(raw, chunks)):
        conformed = out_dir / f"c{j}_src.mp4"
        if not conformed.exists():
            async with _slots():
                await asyncio.to_thread(media.conform, src, chunk, conformed, width=width, height=height)
        frame = out_dir / f"c{j}_first.png"
        if not frame.exists():
            async with _slots():
                await asyncio.to_thread(settled_first, conformed, frame, entering=entering(ground, j))
        sources.append(conformed)
        frames.append(frame)
    from PIL import Image

    with Image.open(frames[0]) as im:
        fw, fh = im.size
    bv.chunks, bv.sources, bv.frames = list(chunks), sources, frames
    bv.occupied = await asyncio.to_thread(ink, bv.chain, 0, fw, fh)
    bv.taken = taken_slots(bv.layers)
    bv.replays = kept or [replay_of_source(s) for s in sources]
    bv.under = recorded_under(bv.exp) if bv.passes else None
    return bv


def new_ink(layers: list, width: int, height: int) -> Any:
    """The union of the new layers' own alpha (float HxW, 0..1) — every
    layer, before the speaker cuts it: their footprint."""
    import numpy as np

    acc = np.zeros((height, width), dtype=np.float32)
    for layer in layers:
        img = layer.render(width, height)
        if img is None:
            continue
        acc = np.maximum(acc, np.asarray(img.getchannel("A"), dtype=np.float32) / 255.0)
    return acc


def hold_mask(layers: list, width: int, height: int) -> Any:
    """K for restore_speaker's hold (float HxW): 1 where the version stays
    exactly as it is, 0 on the new layers' footprint — their ink grown by
    HOLD_GROW_FRAC of the short side and feathered by HOLD_FEATHER_FRAC. So
    Omni's second pass is kept only where it drew something new (and a
    little around it), and a stray drawing anywhere else cannot survive.
    All ones: nothing new to draw here. The ink itself is always all Omni's
    (0): the feather only softens the margin around it."""
    import cv2
    import numpy as np

    footprint = (new_ink(layers, width, height) > 0.02).astype(np.uint8)
    if not footprint.any():
        return np.ones((height, width), dtype=np.float32)
    short = min(width, height)
    grow = max(1, int(round(short * HOLD_GROW_FRAC)))
    grown = cv2.dilate(footprint, cv2.getStructuringElement(cv2.MORPH_ELLIPSE,
                                                            (2 * grow + 1, 2 * grow + 1)))
    soft = cv2.GaussianBlur(grown.astype(np.float32), (0, 0),
                            sigmaX=max(0.5, short * HOLD_FEATHER_FRAC))
    soft = np.maximum(soft, footprint.astype(np.float32))
    return np.clip(1.0 - soft, 0.0, 1.0).astype(np.float32)
