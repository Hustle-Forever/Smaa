"""The Rebrand studio's free side — a scene prepared once, any recipe previewed
on it, with no Omni call.

Omni is switched off for new work (recipe.omni_on; the router refuses a
recipe for Omni, and preview() refuses one too): every draft is made in
house. The Omni side below (the TARGET frames, the prompts, the parts a
"Change it" run would reuse) is kept for a server that switches Omni back
on, and for the versions Omni made, which are still shown and added on top
of.

  prepare(run_id, target)   the scene exactly as a run would cut it: the
                            normalised 720p copy, its ≤10 s chunks, each
                            chunk's first frame and speaker mask, the frame
                            an auto place is planned on. Kept on disk under
                            REBRAND_ROOT/_studio/<run>/<target>_<source hash>
                            so a changed scene video is a new preparation
                            (scene_base; "_t<frame>" after it when a Veo
                            tail flash is cut off the clip's end, as a run
                            cuts it too: media.normalize_scene).
                            It starts the slow, one-off work at once (the
                            intro's colours, every speaker mask) so the first
                            preview mostly finds it done — warmup_state says
                            how far it got — and, after the masks, the
                            scene's analysis for the in-house engine: the
                            speaker on every frame (mattes.MatteBank) and
                            the camera (camera.CameraTrack), made once per
                            scene clip under REBRAND_ROOT/_cache/scene/.
  house_camera(…)           that camera, as the studio draws it (GET
                            /house/camera), or how far its analysis is —
                            with the speaker's masks as the matte bank cuts
                            her (the moving preview's stand-in cuts by them).
  preview(…, recipe)        (made in house: the first part is the DESIGN
                            STILL — the plate the run lays over frame 0,
                            house.design_still — and `house` says how each
                            pattern moves; $0 to generate; final_patterns
                            gives Generate the places to freeze) what Omni
                            would be shown and told for
                            that recipe: the TARGET frame per chunk, the
                            prompt per chunk
                            (the brief's opening is written at Generate), each
                            pattern's place, colour and box, the guards, and
                            each part's layers as colour-free alphas (the
                            studio repaints a colour change from them at
                            once, while the exact frame is made). Cached
                            by the parts of the recipe that change them — but
                            only a CLEAN one: a preview drawn without a
                            speaker mask, or with a pattern that failed,
                            fell back or was placed without its planner, is
                            served once ("kept": false) and drawn again next
                            time, so it cannot stick to that recipe for good.
                            A pattern moved by hand is drawn moved everywhere
                            (rebrand_pattern_ref.placed_layer); each pattern
                            says what the stage's handles need (its box
                            before the move, the pivot, the move as drawn)
                            and warns when the move hides it.
  pattern_art(…, id)        one pattern unmoved and uncut past the frame's
                            edges: the stage's stand-in while a hand moves it.
  icon_plan(…, recipe)      the icons a recipe's icon element shows: which,
                            on which words, where, in what colours
                            (elements/icon.py). Its slow inputs — the
                            words (local Whisper), the speaker track (local
                            MODNet) and the icon director (one cached Gemini
                            call) — are started only once a draft has icons,
                            shared, and a failure is remembered rather than
                            retried on every poll; until they are in it says
                            "warming", and it never waits on the pattern
                            preview (place and colour are "provisional"
                            until that is made). Each shown icon comes with
                            its settled sprite: the very pixels the run
                            stamps (icon_render.Stamper), a local PNG under
                            icons/sprites served by /file — the stage draws
                            it, so what it shows at rest is what Generate
                            makes. "Keep v3's icons" (props.keep) is that
                            version's icons exactly as it froze them —
                            nothing planned, no AI asked.
  icon_moment(…, t)         one frame with the plan's icons, made exactly as
                            the run makes it (speaker_front.frame_at) — on
                            the scene, or on a version being added to (a
                            draft made in house: moment).
  as_generated / house_spec a draft made in house as Generate stores it (its
                            patterns frozen as previewed, the icons of the
                            plan shown) and resolved as house.edit_scene
                            resolves it: the plate spec and its render key.
  moment(…, t)              its frame at t exactly as the run makes it
                            (plate.plate_at over the footage's frame).
  house_render(…)           its exact video, rendered in the background
                            (single-flight by render key, one at a time with
                            the analyses, an edit stops the stale one) as
                            house.draft_path — which a Generate of the same
                            draft copies instead of rendering again (and a
                            run hands its own render back to: the draft that
                            carries on from the version it made plays it).
  remake(version)           a version made in house whose video is lost,
                            made again for $0 — the same frames (its stored
                            recipe resolved as its run resolved it).
  prepare_base(…, version)  "Add on top" of a finished version: its parts
                            conformed to this scene's and their settled first
                            frames (base.stack_parts, as the run makes them),
                            under <scene>/on_v<id>/. A preview on it is drawn
                            on those frames — the exact image Omni will get —
                            with only the new layers, so the stage never shows
                            the original's speaker beside the version's.
                            A "Change it" preview is drawn as a fresh one, and
                            also works out which parts' Omni clips the run
                            will reuse (omni_parts).

An intro or outro (target "intro" | "outro") is prepared from the very clip
the reel uses there (th_reel.resolve_sources: its Veo clip, card or upload),
put on the canvas as the reel's templates show it (cover, a card held as the
stitch holds it) and never trimmed. It is done by hand only: nothing is said
on it, so no words are heard and the icon director is never asked
(_icon_words, _icon_jobs, _icon_director), whatever a client sends. Its
reel's own card or card animation has no one in it (an uploaded video is
judged by its analysis: house.person_of): nothing is cut out, every pattern
holds still on screen (house.plate_spec's `person`), and a part with no
speaker's mask is no warning.

Every edit in the window is a preview, so it has to be quick and honest:
a prepared scene is used straight from memory (no database round trip), the
parts are drawn in parallel, two requests for one preview share the work,
the speaker masks are the LOCAL matte only (a preview never pays Gemini; a
frame the matte cannot read is remembered and warned about, not retried per
preview), and the key holds the colour a pattern is really drawn in, so
"42 violet" picked two ways is one preview.

The seed matters: an auto-placed pattern draws its place from it, so the
preview and the run agree only when both use the recipe's seed —
default_seed() is what both fall back to.

The full-length variant (variant "full", S10): a scene whose clip ends on a
Veo tail flash is prepared a second time, whole (media.normalize with no
end, described whole: the flash is one of its own cuts), under
<target>_<source hash> — the folder it had before the trim existed, kept
when it is there — so an end past the automatic cut can be branded. It is
the same scene to the icon director: its director context is the trimmed
scene's (director_from: its words, the first frame and spots of its speaker
track, its take and its answer, read in place), so the director's key is
the one the trimmed scene answered under and nothing is asked again; its
own words are the trimmed scene's, carried over (no Whisper). Everything
else (previews, plans, renders) is its own, keyed by its own clip.

The scene's end (`end_frame`, the reel's 30 fps frames, sent with every
request once the user moves the end grip): the out-point n = min(the
clip's frames, end_frame). Only an end BEFORE the clip's own last frame
changes anything (_out_n), and only then does n enter a key — the spec memo,
the preview key, the render (house.render_key holds spec.n), and the icon
plan's duration (min(its seconds, n/30)) — so every key made without one
stays as it was. The director's key never holds it: a trim never re-asks.
"""
from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import itertools
import json
import logging
import os
import re
import threading
import time
import weakref
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

from fastapi import HTTPException

from .. import omni_client as omni
from .. import person_matte
from .. import rebrand_pattern_ref as pattern_ref
from .. import rebrand_media as media
from .. import rebrand_pipeline as pipeline
from .. import yas_talking_head_v2 as th
from . import base as hbase
from . import camera as camera_mod
from . import compose
from . import director as dmod
from . import guards as gd
from . import mattes
from . import prompt as hprompt
from . import recipe as rc
from . import runner
from . import speaker_track
from . import stages as st
from . import words as wordsmod
from .elements import icon as icon_el
from .elements import pattern as pattern_el

log = logging.getLogger("archivist.rebrand.studio")

STUDIO_ROOT = pipeline.REBRAND_ROOT / "_studio"
BRIEF_PLACEHOLDER = "[The brief writes this opening when you generate.]"
# Bumped whenever a preview body gains or changes a field, so a preview cached
# by an older build is made again instead of served without it. 3: "kept",
# and a preview drawn without the speaker's mask is no longer stored (an
# older build stored them, so those are dropped with the bump). 4: each
# part lists its layers' alphas (live recolour). 5: made in house, the first
# part is the design still (the plate over frame 0) and the body says how
# each pattern moves (house). 6: each pattern says what the stage's handles
# need (hand, base_box, pivot, full, art_key, warning) and the first part of
# a draft made in house its speaker cut (keep_url). 7: each pattern says when
# it shows (timing: its span, in scene seconds, with its entrance and exit),
# and a draft made in house lists on its first part only the layers its
# design still shows (the patterns showing at its moment).
PREVIEW_VERSION = 7
# Each drawn layer's colour-free alpha, as the target lays it in (after the
# speaker's cut), under <scene>/preview/_alpha/<content hash>.png: a colour
# change draws the very same alphas, so their addresses do not change and
# the studio repaints the new colour itself before the server's frame lands.
ALPHA_DIR = "_alpha"
# A pattern's art unmoved and uncut past the frame (pattern_art: the stand-in
# a hand moves on the stage), under <scene>/preview/_art/<art key>.png.
ART_DIR = "_art"
# A pattern moved by hand is warned about (the preview's warnings, its row)
# when the speaker's head halo holds more than 1 - HAND_HEAD_MIN of its
# visible ink, or she hides more than 1 - HAND_VISIBLE_MIN of it — each only
# when that is HAND_WORSE worse than at its own place, unmoved — or when less
# than HAND_ONSCREEN_MIN of it is left on the picture: the planner's own
# factors (pattern_planner.factors), on the first part's frame.
HAND_HEAD_MIN = 0.7
HAND_VISIBLE_MIN = 0.35
HAND_WORSE = 0.1
HAND_ONSCREEN_MIN = 0.15
ART_VERSION = 1
# A pattern timed to start at or after the scene's end (a span set on a
# longer scene): it does not show here, and the preview says so.
SPAN_AFTER_END = "{name} starts after this scene ends, so it doesn't show"
# Previews kept per scene (each ~0.7 MB: its targets, its patterns' layers,
# preview.json), the most recently served; an older one asked for again is
# drawn again. The alphas no kept preview names go with them, once they are
# an hour old (the browser keeps a studio image that long, and a preview
# drawn but not kept names its alphas nowhere on disk).
PREVIEWS_KEPT = 40
ALPHAS_GRACE_SEC = 3600

_LOCKS: dict[tuple[int, str], asyncio.Lock] = {}
_PREPARED: dict[tuple, "Scene"] = {}
# In flight, per preview folder: a second request for the same preview (a
# retry, a second tab) joins the running one instead of racing its files.
_PREVIEWS: dict[str, asyncio.Task] = {}
# Speaker masks, per frame: in flight, or found to be missing this session (a
# frame the local matte cannot read is not retried on every preview). Found
# masks are not held: person_matte reads them back from their .person.png.
_MASK_TASKS: dict[str, asyncio.Task] = {}
# Versions built on: checked once per session (a ready version does not
# change, so a preview on one never asks the database again) …
_BASES: dict[tuple, hbase.BaseVersion] = {}
# … and, for "Add on top", prepared once per scene (in flight, or done).
_STACKS: dict[str, asyncio.Task] = {}
# Both most recently used last; only the last BASES_KEPT are held (a
# prepared one carries its ink, a float frame of ~3.7 MB at 720x1280, and
# its replays) — one dropped is only made again, from its files on disk.
BASES_KEPT = 8
# The speaker's mask as a RUN gets it for a frame it has just cut (fresh,
# unrounded), per frame: what a "Change it" run's target is drawn with. The
# most recently used last; only the last RUN_MASKS_KEPT are held (each is a
# float frame, ~3.7 MB at 720x1280).
_RUN_MASKS: dict[tuple[str, int], Any] = {}
RUN_MASKS_KEPT = 8


def default_seed(run_id: int, target: str) -> str:
    return f"studio:{run_id}:{target}"


@dataclass
class Scene:
    run_id: int
    target: str
    aspect: str
    width: int
    height: int
    work: Path
    ctx: pipeline.Ctx                     # a run context with no experiment (exp_id 0)
    chunks: list[Path] = field(default_factory=list)
    firsts: list[Path] = field(default_factory=list)
    seconds: list[float] = field(default_factory=list)
    colours: Optional[pattern_ref.Colours] = None   # the intro's, dug once (_colours)
    # The frame an auto place is planned on, found once (_plan_frame): a run
    # re-extracts it, which made it newer than its cached speaker mask.
    plan_frame: Optional[Path] = None
    plan_task: Optional[asyncio.Task] = None
    # The scene's own jump cuts (frames of its normalised clip; they stay in
    # it) and the seconds of a Veo tail flash cut off its end
    # (media.normalize_scene).
    cuts: list[int] = field(default_factory=list)
    trimmed_sec: float = 0.0
    # scene | intro | outro (_target_of), and — an intro or outro — the
    # reel's source for it (th_reel: anim:<id> | card:<id> | upload:<id> |
    # asset:<id>).
    kind: str = "scene"
    src: Optional[str] = None
    # Its clip's frames as a matte bank counts them (0: not counted yet), and
    # what the end grip needs (_end_info: public_scene's `end`).
    frames: int = 0
    end_info: Optional[dict] = None
    # "full": the clip kept whole past its tail flash (the module doc); and
    # then the trimmed scene's folder, whose director context it reads.
    variant: Optional[str] = None
    director_from: Optional[Path] = None


def url_for(path: Path) -> str:
    rel = Path(path).resolve().relative_to(STUDIO_ROOT.resolve()).as_posix()
    return f"/api/rebrand/harness/file/{rel}"


def resolve_file(rel: str) -> Path:
    """A studio file by its URL path (a "?v=" a redrawn picture carries is
    not part of it), never outside STUDIO_ROOT."""
    root = STUDIO_ROOT.resolve()
    p = (root / rel.split("?", 1)[0]).resolve()
    if root not in p.parents or p.suffix.lower() not in (".png", ".jpg", ".mp4") or not p.is_file():
        raise HTTPException(404, "No such studio file.")
    return p


BOOKENDS = ("intro", "outro")
# A card (a still) is held this long when the reel's own length for it is
# not known (th_reel.BOOKEND_DEFAULT_SEC: the stitch's).
BOOKEND_CARD_SEC = 4.0
NOT_IN_REEL = "The {name} isn't in the reel."
NO_BOOKEND = "The {name}'s clip isn't on this computer."


def _target_of(target: str) -> tuple[str, int]:
    """(kind, index) of a studio target: ("scene", N) for scene_N, (target,
    0) for an intro or outro. 400 for anything else."""
    if target in BOOKENDS:
        return target, 0
    if not target.startswith("scene_") or not target[6:].isdigit():
        raise HTTPException(400, "The studio works on one part of the reel: target scene_N, intro or outro.")
    return "scene", int(target[6:])


def is_bookend(target: str) -> bool:
    return target in BOOKENDS


async def _thread(fn, *args, **kwargs):
    return await asyncio.to_thread(fn, *args, **kwargs)


def _variant(target: str, variant: Optional[str]) -> Optional[str]:
    """The variant asked for, as the studio keeps it: "full" for a scene,
    none for an intro or outro (never trimmed: it is always whole)."""
    return "full" if variant == "full" and target not in BOOKENDS else None


def _pkey(run_id: int, target: str, variant: Optional[str] = None) -> tuple:
    """A prepared scene's key: (run, target) — and the variant when it is one."""
    v = _variant(target, variant)
    return (run_id, target) if v is None else (run_id, target, v)


def prepared(run_id: int, target: str, variant: Optional[str] = None) -> Optional[Scene]:
    """The scene prepared in this process, or None."""
    return _PREPARED.get(_pkey(run_id, target, variant))


async def prepare(run_id: int, target: str, variant: Optional[str] = None) -> Scene:
    kind, idx = _target_of(target)
    lock = _LOCKS.setdefault((run_id, target), asyncio.Lock())
    async with lock:
        if kind != "scene":
            return await _prepare_bookend(run_id, target)
        return await _prepare(run_id, target, idx, _variant(target, variant))


async def _scene(run_id: int, target: str, variant: Optional[str] = None) -> Scene:
    """The prepared scene for a preview: straight from memory when the
    studio has prepared it (the window's open called prepare, which checks
    the scene against the database), else prepared now."""
    _target_of(target)
    known = _PREPARED.get(_pkey(run_id, target, variant))
    if known is not None and known.work.exists():
        return known
    return await prepare(run_id, target, **_opt(variant=variant))


def scene_base(run_id: int, target: str, source: Path, *, full: bool = False) -> Path:
    """The scene's studio folder: REBRAND_ROOT/_studio/<run>/<target>_<the
    source's hash>, and "_t<frame>" after it when the source ends on a Veo
    tail flash that is cut off from that frame (media.tail_cut) — so a scene
    prepared before the flash was trimmed is prepared again, once, and one
    with no flash keeps every file it has. Its normalised clip is
    base/scene<N>.mp4 (media.normalize_scene), which a run of the same
    source copies rather than making again. Synchronous: it reads the
    source (its hash, and each frame's change from the one before for
    media.tail_cut — both remembered). An intro or outro is never trimmed:
    its folder never ends "_t<frame>"; nor does the full-length variant
    (`full`: the scene kept whole, the module doc)."""
    src_key = (st.digest(Path(source)) or "none")[:10]
    cut = (media.tail_cut(Path(source)) if src_key != "none" and target not in BOOKENDS and not full
           else None)
    name = f"{target}_{src_key}" + (f"_t{cut}" if cut is not None else "")
    return STUDIO_ROOT / str(run_id) / name


def _untrimmed(work: Path) -> Optional[Path]:
    """The folder the same scene had before its tail flash was trimmed
    (scene_base without "_t<frame>"), or None for a scene with no flash."""
    name = re.sub(r"_t\d+$", "", work.name)
    return work.with_name(name) if name != work.name else None


def _full_clip(source: Path, out: Path, *, width: int, height: int) -> Path:
    """A scene kept whole past its tail flash: normalize() with no end (the
    very command a clean clip gets), described whole — never normalize_scene,
    which always cuts. Written whole or not at all. Synchronous."""
    out = Path(out)
    tmp = out.with_name(f"{out.stem}.part{out.suffix}")
    media.normalize(Path(source), tmp, width=width, height=height, audio="keep", end_frame=None)
    media.describe_scene(Path(source), tmp, info=media.scene_info_path(out), whole=True)
    os.replace(tmp, out)
    return out


def _carry_to_full(scene: Scene, trimmed: Path) -> None:
    """The full-length variant hears what the trimmed scene heard: its words
    (re-keyed to this clip, their digest unchanged — the reverse of
    _carry_icons), so the icons are placed on the same words and no Whisper
    runs. Only when this folder has none of its own (a folder prepared before
    the trim existed keeps its own). Synchronous; a failure only means the
    words are worked out again."""
    clip = _scene_clip(scene)
    new_dir = _icon_dir(scene)
    if clip is None or (new_dir / "words.json").exists():
        return
    body = _read(Path(trimmed) / "icons" / "words.json")
    if not body or body.get("version") != wordsmod.WORDS_VERSION or not body.get("key"):
        return
    line, _others, _topic = _scene_lines(scene)
    if body["key"] != wordsmod.cache_key(Path(trimmed) / "base" / clip.name, line):
        return                                   # heard in another line, or its clip is gone
    try:
        key = wordsmod.cache_key(clip, line)
        if key is not None:
            wordsmod._write(new_dir / "words.json",
                            wordsmod.carried(body, key=key, duration=float(body.get("duration") or 0.0)))
    except Exception:  # noqa: BLE001 — the words are worked out again instead
        log.exception("[studio] carrying %s's words to its full-length clip failed", Path(trimmed).name)


async def _prepare(run_id: int, target: str, idx: int, variant: Optional[str] = None) -> Scene:
    full = variant == "full"
    trimmed: Optional[Scene] = None
    if full:
        # The trimmed scene first (its director context is this one's).
        trimmed = _PREPARED.get(_pkey(run_id, target)) or await _prepare(run_id, target, idx)
    run = await th._fetch_run_row(run_id)
    md = run["metadata"] or {}
    refs = dict(md.get("refs") or {})
    aspect = th._norm_aspect(md.get("aspect_ratio"))
    width, height = media.dims_for(aspect)
    cache = pipeline.REBRAND_ROOT / "_cache"
    intro = await pipeline._bookend("intro", run_id=run_id, md=md, refs=refs, cache=cache)
    scenes = await pipeline._scenes(md, cache, strict=False, force_index=idx)
    seg = next(sg for sg in scenes if sg.index == idx)
    work = await _thread(scene_base, run_id, target, seg.source, full=full)
    known = _PREPARED.get(_pkey(run_id, target, variant))
    # The whole folder: a scene trimmed of its tail flash ends "_t<frame>",
    # not with the source's hash.
    if known and known.work == work and known.aspect == aspect:
        _warm(known)                              # a reopen retries what was missing
        return known

    base = work / "base"
    seg.base = base / f"scene{idx}.mp4"
    if not seg.base.exists():
        if full:
            await _thread(_full_clip, seg.source, seg.base, width=width, height=height)
        else:
            await _thread(media.normalize_scene, seg.source, seg.base, width=width, height=height)
    elif media.scene_info(seg.base) is None:
        # Made by an older build, with no flash to cut (the folder would say
        # so): the very bytes normalize_scene makes, only described — or,
        # kept whole, the clip made before the trim existed.
        await _thread(media.describe_scene, seg.source, seg.base, whole=full)
    info = media.scene_info(seg.base) or {}
    options = pipeline.normalize_options({"target": target})
    # The reel's words as a run reads them (the topic, each scene's line):
    # what the icon director is given.
    ctx = pipeline.Ctx(
        exp_id=0, run_id=run_id, aspect=aspect, width=width, height=height, work=work,
        options=options, run_ctx=pipeline._run_ctx(run, md, scenes, aspect), intro=intro,
        scenes=scenes, outro=None, logo=None,
    )
    ctx.run_ctx["target"] = target
    ctx.target_seg = seg
    if seg is scenes[0]:
        # What a run plans an auto place on for the reel's first scene.
        ctx.frames["th_first"] = await pipeline._frame(scenes[0].source, base / "th_first.png", "first")

    listing = work / "chunks.json"
    if listing.exists():
        chunks = [work / c for c in json.loads(listing.read_text(encoding="utf-8"))]
    else:
        chunks = []
    if not chunks or not all(c.exists() for c in chunks):
        chunks = await _thread(media.split_chunks, seg.base, work / "chunks")
        listing.write_text(json.dumps([Path(c).relative_to(work).as_posix() for c in chunks]),
                           encoding="utf-8")
    scene = Scene(run_id=run_id, target=target, aspect=aspect, width=width, height=height,
                  work=work, ctx=ctx, chunks=list(chunks),
                  cuts=[int(c) for c in info.get("cuts") or []],
                  trimmed_sec=float(info.get("trimmed_sec") or 0.0))
    if ctx.frames.get("th_first"):
        scene.plan_frame = ctx.frames["th_first"]
    for j, chunk in enumerate(scene.chunks):
        first = work / f"c{j}_first.png"
        if not first.exists():
            await _thread(media.extract_frame, chunk, first, at="first")
        scene.firsts.append(first)
        scene.seconds.append(round(await _thread(media.probe_duration, chunk) or 0.0, 2))
    if info.get("source_cut") is not None:
        await _thread(_carry_icons, scene, info)
    scene.variant = variant
    if trimmed is not None and trimmed.work != work:
        scene.director_from = trimmed.work
        await _thread(_carry_to_full, scene, trimmed.work)
    await _count_scene(scene, seg.source, main=True, still=False)
    _PREPARED[_pkey(run_id, target, variant)] = scene
    _COLOUR_TASKS.pop(str(work), None)
    _warm(scene)
    return scene


async def _bookend_target(run: dict, target: str) -> Any:
    """The reel's own segment for an intro or outro (th_reel.Target: its
    source by the stitch's priorities; a Veo clip on no disk here fetched
    from the bucket), or None."""
    from .. import th_reel

    return await th_reel.resolve_target(run, target, remote=True)


def _bookend_clip(source: Path, out: Path, *, still: bool, seconds: float, width: int, height: int) -> Path:
    """An intro or outro on the canvas as the reel shows it (cover): a video
    with its sound, a card held `seconds` in silence — whole, never
    trimmed, described (media.describe_scene) before it takes its place.
    Synchronous."""
    out = Path(out)
    tmp = out.with_name(f"{out.stem}.part{out.suffix}")
    if still:
        media.still_to_clip(Path(source), tmp, seconds=seconds, width=width, height=height, fit="cover")
    else:
        media.normalize(Path(source), tmp, width=width, height=height, audio="keep", fit="cover")
    media.describe_scene(Path(source), tmp, info=media.scene_info_path(out), whole=True)
    os.replace(tmp, out)
    return out


async def _prepare_bookend(run_id: int, target: str) -> Scene:
    """An intro or outro, prepared (the module doc): the reel's own source
    for it on the canvas (_bookend_clip) under <target>_<source hash>, its
    parts' first frames, and the frame an auto place is planned on — its
    own first frame. No tail trim and nothing carried over: it is never
    cut, and nothing is said on it. 409 when the reel leaves it out or its
    clip is not here."""
    run = await th._fetch_run_row(run_id)
    md = run["metadata"] or {}
    refs = dict(md.get("refs") or {})
    aspect = th._norm_aspect(md.get("aspect_ratio"))
    width, height = media.dims_for(aspect)
    t = await _bookend_target(run, target)
    if t is None or t.excluded:
        raise HTTPException(409, NOT_IN_REEL.format(name=target))
    if t.path is None or not Path(t.path).is_file():
        raise HTTPException(409, NO_BOOKEND.format(name=target))
    source = Path(t.path)
    work = await _thread(scene_base, run_id, target, source)
    known = _PREPARED.get((run_id, target))
    if known and known.work == work and known.aspect == aspect:
        _warm(known)
        return known
    # The reel's intro: every part of the reel digs its colours from it.
    intro = await pipeline._bookend("intro", run_id=run_id, md=md, refs=refs,
                                    cache=pipeline.REBRAND_ROOT / "_cache")
    clip = work / "base" / f"{target}.mp4"
    if not clip.exists():
        # A card is held as long as the stitch holds it (the Veo row's
        # seconds, else BOOKEND_CARD_SEC).
        await _thread(_bookend_clip, source, clip, still=bool(t.still),
                      seconds=float(t.seconds or BOOKEND_CARD_SEC), width=width, height=height)
    elif media.scene_info(clip) is None:
        await _thread(media.describe_scene, source, clip, whole=True)
    info = media.scene_info(clip) or {}
    seg = pipeline.Segment(kind=target, index=0, source=source, is_still=bool(t.still), base=clip)
    ctx = pipeline.Ctx(
        exp_id=0, run_id=run_id, aspect=aspect, width=width, height=height, work=work,
        options=pipeline.normalize_options({"target": target}), run_ctx=pipeline._run_ctx(run, md, [], aspect),
        intro=intro, scenes=[], outro=None, logo=None,
    )
    ctx.run_ctx["target"] = target
    ctx.target_seg = seg
    listing = work / "chunks.json"
    chunks = [work / c for c in json.loads(listing.read_text(encoding="utf-8"))] if listing.exists() else []
    if not chunks or not all(c.exists() for c in chunks):
        chunks = await _thread(media.split_chunks, clip, work / "chunks")
        listing.write_text(json.dumps([Path(c).relative_to(work).as_posix() for c in chunks]), encoding="utf-8")
    scene = Scene(run_id=run_id, target=target, aspect=aspect, width=width, height=height, work=work, ctx=ctx,
                  chunks=list(chunks), cuts=[int(c) for c in info.get("cuts") or []], kind=target, src=t.src)
    for j, chunk in enumerate(scene.chunks):
        first = work / f"c{j}_first.png"
        if not first.exists():
            await _thread(media.extract_frame, chunk, first, at="first")
        scene.firsts.append(first)
        scene.seconds.append(round(await _thread(media.probe_duration, chunk) or 0.0, 2))
    scene.plan_frame = scene.firsts[0] if scene.firsts else None
    await _count_scene(scene, source, main=False, still=bool(t.still))
    _PREPARED[(run_id, target)] = scene
    _COLOUR_TASKS.pop(str(work), None)
    _warm(scene)
    return scene


def _warm(scene: Scene) -> None:
    """Start the scene's slow one-off work — the intro's colours and every
    speaker mask, then its analysis (every speaker frame, the camera) — so
    the first preview mostly finds it done. A mask found missing earlier,
    or an analysis that failed, is tried again (the studio was reopened)."""
    for frame in _mask_frames(scene):
        task = _MASK_TASKS.get(str(frame))
        if task is not None and task.done():
            _MASK_TASKS.pop(str(frame), None)
    asyncio.ensure_future(_colours(scene))
    asyncio.ensure_future(_warm_masks(scene))


def _carry_icons(scene: Scene, info: dict) -> None:
    """A scene trimmed of its tail flash keeps what the same scene, prepared
    before the trim, already heard and was told: its words (local Whisper)
    and the icon director's answer (a paid call) — so the director is not
    asked again for a clip that only lost its last few frames.

    Only when every word said ends before the cut (the sound up to it is
    the very same), the words were heard in this scene's current line, and
    this folder has no words of its own yet. The words keep their digest (the
    director's key holds it) and are keyed to the trimmed clip; the take
    ("Ask the AI again") comes with them, as it is in the key too. The
    speaker track is made again on the trimmed clip: its first frame
    decodes to the same bytes, so the spots the director read are the same.
    Synchronous; a failure only means the words are worked out again."""
    old = _untrimmed(scene.work)
    clip = _scene_clip(scene)
    new_dir = _icon_dir(scene)
    if old is None or clip is None or (new_dir / "words.json").exists():
        return
    old_dir = old / "icons"
    body = _read(old_dir / "words.json")
    if not body or body.get("version") != wordsmod.WORDS_VERSION or not body.get("key"):
        return
    line, _others, _topic = _scene_lines(scene)
    if body["key"] != wordsmod.cache_key(old / "base" / clip.name, line):
        return                                   # heard in another line, or its clip is gone
    fps = float(info.get("source_fps") or 0.0)
    cut_sec = int(info["source_cut"]) / fps if fps > 0 else 0.0
    said = [w for w in body.get("words") or [] if w.get("said") and w.get("e") is not None]
    late = [w for w in said if float(w["e"]) > cut_sec]
    if cut_sec <= 0 or late:
        if late:
            log.warning("[studio] %s: the trimmed tail flash cuts into %r (%.2f s > %.2f s); "
                        "its words are heard again", scene.work.name, late[0].get("text"),
                        float(late[0]["e"]), cut_sec)
        return
    try:
        key = wordsmod.cache_key(clip, line)
        if key is None:
            return
        wordsmod._write(new_dir / "words.json", wordsmod.carried(body, key=key, duration=cut_sec))
        for name in ("director.json", "take.json"):
            if (old_dir / name).is_file() and not (new_dir / name).exists():
                tmp = new_dir / f"{name}.{os.getpid()}.tmp"
                tmp.write_bytes((old_dir / name).read_bytes())
                os.replace(tmp, new_dir / name)
    except Exception:  # noqa: BLE001 — the words are worked out again instead
        log.exception("[studio] carrying %s's words over to its trimmed clip failed", old.name)


def _end_info(source: Path, frames: int, *, main: bool, still: bool) -> dict:
    """What the end grip needs (public_scene's `end`), in the reel's 30 fps
    frames: the source's own rate and frames, where the reel cuts it by
    itself (a main scene's Veo tail flash — th_reel's auto end; None for an
    intro or outro, never cut), the prepared clip's frames (what can be
    branded on it) and how far the clip really runs (the ruler's reach).
    Synchronous: it decodes the source once (th_reel.clip_facts, kept)."""
    from .. import th_reel

    if still:
        return {"src_fps": None, "src_frames": None, "auto": None, "frames": int(frames), "extent": int(frames)}
    f = th_reel.clip_facts(Path(source))
    return {"src_fps": round(float(f.fps), 3), "src_frames": int(f.frames), "auto": f.auto_end if main else None,
            "frames": int(frames), "extent": max(int(frames), int(f.full))}


async def _count_scene(scene: Scene, source: Path, *, main: bool, still: bool) -> None:
    """The scene's frames and its end grip's numbers (_end_info), counted
    once at prepare — best-effort: a clip that can't be read leaves them
    unset (no end is then taken as a cut: _out_n)."""
    clip = _scene_clip(scene)
    try:
        scene.frames = await _thread(mattes.frames_of, clip)
        scene.end_info = await _thread(_end_info, source, scene.frames, main=main, still=still)
    except Exception as e:  # noqa: BLE001 — the end grip waits for a clip it can read
        log.warning("[studio] %s: its frames couldn't be counted (%s)", scene.work.name, str(e)[:200])


def _opt(**kw: Any) -> dict:
    """The keywords that are set: a call without them is the very call it
    always was (and a stand-in of the callee from before them still takes
    it)."""
    return {k: v for k, v in kw.items() if v is not None}


def _cut_n(scene: Scene, end_frame: Optional[int]) -> Optional[int]:
    """The out-point a user end sets: end_frame when it is before the clip's
    own last frame, else None (the whole clip, exactly as ever)."""
    if end_frame is None or not scene.frames:
        return None
    return int(end_frame) if int(end_frame) < int(scene.frames) else None


async def _out_n(scene: Scene, end_frame: Optional[int]) -> Optional[int]:
    """_cut_n, the clip's frames counted first when they are not yet (a
    scene prepared elsewhere): only ever for a request with an end."""
    if end_frame is None:
        return None
    if not scene.frames:
        clip = _scene_clip(scene)
        if clip is not None and clip.exists():
            try:
                scene.frames = await _thread(mattes.frames_of, clip)
            except Exception:  # noqa: BLE001 — not counted: no cut (the whole clip, as ever)
                log.warning("[studio] %s: its frames couldn't be counted", scene.work.name)
    return _cut_n(scene, end_frame)


def _plan_duration(scene: Scene, n: Optional[int]) -> float:
    """The icon plan's length: the parts' seconds, as ever — or, cut short by
    a user end, min(those, n/30)."""
    whole = round(sum(scene.seconds), 3)
    return whole if n is None else min(whole, round(n / float(media.FPS), 3))


def public_scene(scene: Scene, *, end_frame: Optional[int] = None) -> dict:
    seg = scene.ctx.target_seg
    video = seg.base if seg is not None else None
    n = _cut_n(scene, end_frame)
    return {
        "run_id": scene.run_id,
        "target": scene.target,
        "aspect": scene.aspect,
        "width": scene.width,
        "height": scene.height,
        "chunks": [{"index": j, "seconds": scene.seconds[j], "frame_url": url_for(f)}
                   for j, f in enumerate(scene.firsts)],
        "default_seed": default_seed(scene.run_id, scene.target),
        # The scene as the studio normalised it (what "Original" plays).
        "video_url": url_for(video) if video is not None and Path(video).is_file() else None,
        # Its length — cut short by the request's end, when it is.
        "duration_sec": round(sum(scene.seconds), 2) if n is None else round(n / float(media.FPS), 2),
        # Where the footage itself jumps to another shot (seconds), and how
        # much of a Veo tail flash was cut off its end.
        "cuts_sec": [round(c / media.FPS, 3) for c in scene.cuts],
        "trimmed_sec": round(scene.trimmed_sec, 3),
        # scene | intro | outro; and, for an intro or outro, whether a person
        # is in it (None until its analysis says; a scene: None).
        "kind": scene.kind,
        "has_person": has_person(scene),
        # The end grip (_end_info): {src_fps, src_frames, auto, frames,
        # extent}, frames in the reel's 30 fps.
        "end": scene.end_info,
        # "full": this is the clip kept whole past its automatic cut.
        "variant": scene.variant,
    }


_COLOUR_TASKS: dict[str, asyncio.Task] = {}


async def _colours(scene: Scene) -> pattern_ref.Colours:
    """The intro's colours for this scene, dug once and kept beside it (and
    beside the run, for its other scenes: _run_colours): scanning an intro
    video back past its fade-out takes up to a minute, and every preview
    needs them. prepare() starts the dig, so it is usually done (or under
    way) by the first preview; concurrent callers share it."""
    if scene.colours is not None:
        return scene.colours
    key = str(scene.work)
    task = _COLOUR_TASKS.get(key)
    if task is None or (task.done() and (task.cancelled() or task.exception() is not None)):
        task = _COLOUR_TASKS[key] = asyncio.ensure_future(_dig(scene))
    scene.colours = await asyncio.shield(task)
    return scene.colours


def _read_colours(saved: Path) -> Optional[pattern_ref.Colours]:
    try:
        d = json.loads(saved.read_text(encoding="utf-8"))
        frame = Path(d["frame"]) if d.get("frame") else None
        if frame is None or frame.exists():
            return pattern_ref.Colours(ground=d["ground"], ink=d["ink"],
                                       fell_back=bool(d["fell_back"]), frame=frame,
                                       colour=d["colour"])
    except (OSError, ValueError, KeyError):
        pass
    return None


def _run_colours(scene: Scene) -> Optional[Path]:
    """Where the intro's colours are kept for the whole run, keyed by the
    intro's bytes: every scene of a reel shares its intro, so a second scene
    opens without digging it again (a dig takes up to a minute)."""
    intro = scene.ctx.intro
    key = st.digest(intro.source) if intro is not None else None
    return scene.work.parent / f"intro_{key[:10]}.colours.json" if key else None


async def _dig(scene: Scene) -> pattern_ref.Colours:
    saved = scene.work / "colours.json"
    found = _read_colours(saved)
    if found is not None:
        return found
    shared = await _thread(_run_colours, scene)
    found = _read_colours(shared) if shared is not None else None
    if found is None:
        intro = scene.ctx.intro
        found = await _thread(pattern_ref.dig_colours, intro.source if intro else None,
                              scene.work / "intro", is_still=bool(intro and intro.is_still))
    body = {"ground": found.ground, "ink": found.ink, "fell_back": found.fell_back,
            "frame": str(found.frame) if found.frame else None, "colour": found.colour}
    for path in (saved, shared):
        if path is not None:
            _write(path, body)
    return found


# ---------------------------------------------------------------------------
# Speaker masks and the plan frame, found once per scene
# ---------------------------------------------------------------------------

def _forget_found(key: str, task: asyncio.Task) -> None:
    """A mask found is on disk (.person.png) — only a missing one (None) is
    remembered in memory."""
    if task.cancelled() or task.exception() is not None:
        return
    if task.result() is not None:
        _MASK_TASKS.pop(key, None)


async def _mask(frame: Path) -> Any:
    """The speaker's mask for one frame (float HxW) or None — the LOCAL matte
    only (person_matte.local_mask), shared between concurrent previews, and
    a None kept for the session."""
    key = str(frame)
    task = _MASK_TASKS.get(key)
    if task is None or (task.done() and (task.cancelled() or task.exception() is not None)):
        task = _MASK_TASKS[key] = asyncio.ensure_future(person_matte.local_mask(Path(frame)))
        task.add_done_callback(lambda t, k=key: _forget_found(k, t))
    return await asyncio.shield(task)


async def _plan_frame(scene: Scene) -> Optional[Path]:
    """The frame an auto place is planned on (pipeline._plan_frame, the one a
    run uses), found once per scene."""
    if scene.plan_frame is not None and scene.plan_frame.exists():
        return scene.plan_frame
    task = scene.plan_task
    if task is None or (task.done() and (task.cancelled() or task.exception() is not None)):
        task = scene.plan_task = asyncio.ensure_future(pipeline._plan_frame(scene.ctx))
    scene.plan_frame = await asyncio.shield(task)
    return scene.plan_frame


def _mask_frames(scene: Scene) -> list[Path]:
    frames = list(scene.firsts)
    if scene.plan_frame is not None and scene.plan_frame not in frames:
        frames.append(scene.plan_frame)
    return frames


async def _warm_masks(scene: Scene) -> None:
    """Every speaker mask the scene's previews need, one after the other
    (MODNet takes about a second each) — then the scene's analysis
    (_analyse_scene), which no preview waits on yet."""
    try:
        await _plan_frame(scene)
        for frame in _mask_frames(scene):
            await _mask(frame)
    except Exception:  # noqa: BLE001 — a warm-up is a head start, never a failure
        log.exception("[studio] warming the speaker masks failed")
    _analyse_scene(scene)


def _mask_done(frame: Path) -> bool:
    cache = frame.with_name(f"{frame.stem}.person.png")
    try:
        if cache.exists() and cache.stat().st_mtime >= frame.stat().st_mtime:
            return True
    except OSError:
        pass
    task = _MASK_TASKS.get(str(frame))
    return bool(task is not None and task.done())


def has_person(scene: Scene) -> Optional[bool]:
    """Whether an intro or outro has anyone in it (house.person_of): the
    reel's own card or card animation never; an uploaded video once its
    analysis says (None before). None for a scene (a talking head has its
    speaker)."""
    if scene.kind == "scene":
        return None
    if not str(scene.src or "").startswith("upload:"):
        return False
    clip = _scene_clip(scene)
    a = _ANALYSES.get(str(clip)) if clip is not None else None
    return a.person if a is not None else None


def warmup_state(scene: Scene) -> dict:
    """How far the scene's one-off work has got: the intro's colours
    (ready | working | failed), the speaker masks found (or found missing)
    out of how many, and its analysis — the speaker frames made out of how
    many (speaker_frames) and the camera (waiting | working | ready |
    failed: _analysis_state)."""
    if scene.colours is not None:
        colours = "ready"
    else:
        task = _COLOUR_TASKS.get(str(scene.work))
        if task is None:
            colours = "ready" if (scene.work / "colours.json").exists() else "working"
        elif not task.done():
            colours = "working"
        elif task.cancelled() or task.exception() is not None:
            colours = "failed"
        else:
            colours = "ready"
    frames = _mask_frames(scene)
    done, total, cam = _analysis_state(scene)
    body = {"prepared": True, "colours": colours,
            "masks": {"done": sum(1 for f in frames if _mask_done(f)), "total": len(frames)},
            "speaker_frames": {"done": done, "total": total}, "camera": cam}
    if scene.kind != "scene":
        body["has_person"] = has_person(scene)
    return body


# ---------------------------------------------------------------------------
# The scene analysed once for the in-house engine: the speaker on every
# frame, and the camera
# ---------------------------------------------------------------------------

# Where a scene clip's analysis is kept: <its hash, 16>/mattes_<key>.npz and
# camera_<key>.npz — keyed by the clip's bytes, so the studio and a run of the
# same clip (which copies the studio's) share it. Local only.
ANALYSIS_ROOT = pipeline.REBRAND_ROOT / "_cache" / "scene"
# Scenes analysed at once (MODNet on ~80 frames, then the camera): this PC is
# short of memory, and the in-house renders will share the gate.
HOUSE_CONCURRENCY = max(1, int(os.getenv("REBRAND_HOUSE_CONCURRENCY", "1") or 1))


@dataclass
class _Analysis:
    """One scene clip's analysis, in memory: how far it is, its matte bank
    (ONE per clip: the stills, the moments and the draft renders all cut by
    it, and a keyframe made on demand is made once) and the camera once it
    is followed."""
    clip: Path
    cuts: list[int]
    task: Optional[asyncio.Task] = None
    step: str = "waiting"                  # waiting | speaker | camera | ready | failed
    frames: tuple[int, int] = (0, 0)       # speaker keyframes made, needed
    track: Optional[camera_mod.CameraTrack] = None
    error: str = ""
    bank: Optional[mattes.MatteBank] = None
    # Whether anyone is in the clip (MatteBank.has_person), once filled:
    # what an intro or outro's studio says (has_person).
    person: Optional[bool] = None


_ANALYSES: dict[str, _Analysis] = {}
_GATES: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Semaphore]" = weakref.WeakKeyDictionary()
_BANK_LOCK = threading.Lock()


def _gate() -> asyncio.Semaphore:
    """The one gate of this event loop (tests run several loops)."""
    loop = asyncio.get_running_loop()
    gate = _GATES.get(loop)
    if gate is None:
        gate = _GATES[loop] = asyncio.Semaphore(HOUSE_CONCURRENCY)
    return gate


def analysis_dir(clip: Path) -> Path:
    """REBRAND_ROOT/_cache/scene/<the clip's hash, 16 hex>: its analysis.
    Synchronous (it hashes the clip once; remembered)."""
    return ANALYSIS_ROOT / (st.digest(Path(clip)) or "none")[:16]


def _analysis(scene: Scene) -> Optional[_Analysis]:
    clip = _scene_clip(scene)
    if clip is None:
        return None
    a = _ANALYSES.get(str(clip))
    if a is None:
        a = _ANALYSES[str(clip)] = _Analysis(clip=clip, cuts=list(scene.cuts))
    return a


def _analyse_scene(scene: Scene, *, retry: bool = True) -> Optional[asyncio.Task]:
    """Start the scene's analysis once (it is shared: a second call joins
    it); one that failed starts again when `retry`. None: the scene has no
    clip."""
    a = _analysis(scene)
    if a is None:
        return None
    failed = a.task is not None and a.task.done() and a.step != "ready"
    if a.task is None or (failed and retry):
        a.step, a.error = "waiting", ""
        a.task = asyncio.ensure_future(_analyse(a))
    return a.task


def _bank_of(a: _Analysis) -> mattes.MatteBank:
    """The clip's one matte bank (opened once: what is kept of it on disk,
    read back). Synchronous (it counts the clip's frames the first time)."""
    with _BANK_LOCK:
        if a.bank is None:
            a.bank = mattes.MatteBank.open(a.clip, analysis_dir(a.clip), cuts=a.cuts)
        return a.bank


async def _analyse(a: _Analysis) -> None:
    """The matte bank filled, then the camera followed — each read back from
    disk when a studio or a run made it before. Never raises: a failure is
    kept (step "failed") and said by warmup_state and /house/camera."""
    try:
        async with _gate():
            folder = await _thread(analysis_dir, a.clip)
            a.step = "speaker"
            bank = await _thread(_bank_of, a)
            a.frames = bank.progress()

            def made(done: int, total: int) -> None:
                a.frames = (done, total)

            await _thread(bank.fill, progress=made)
            a.frames = bank.progress()
            a.person = await _thread(bank.has_person)
            a.step = "camera"
            a.track = await _thread(camera_mod.build, a.clip, folder, bank=bank)
            a.step = "ready"
    except Exception as e:  # noqa: BLE001 — the analysis is said to have failed, never raised
        a.step, a.error = "failed", str(e)[:200] or type(e).__name__
        with _BANK_LOCK:
            a.bank = None                  # opened again on the next try
        log.exception("[studio] analysing %s (speaker frames, camera) failed", a.clip.name)


def _analysis_state(scene: Scene) -> tuple[int, int, str]:
    """(speaker frames made, needed, the camera: waiting | working | ready |
    failed). "waiting" until the analysis starts (after the masks)."""
    a = _ANALYSES.get(str(_scene_clip(scene))) if _scene_clip(scene) is not None else None
    if a is None:
        return 0, 0, "waiting"
    done, total = a.frames
    if a.step == "ready":
        return total, total, "ready"
    if a.step == "failed":
        return done, total, "failed"
    return done, total, "working" if a.step in ("speaker", "camera") else "waiting"


async def house_camera(run_id: int, target: str, variant: Optional[str] = None) -> dict:
    """GET /house/camera: the scene's camera once it is followed —
    {state: "ready", **CameraTrack.public()} (per frame [a, b, tx, ty], M =
    [[a, -b, tx], [b, a, ty]] from frame-0 pixels to frame i's) — else how
    far its analysis is: {state: waiting | working | failed, speaker_frames,
    error?}. Starts the analysis if it never was (a failed one is tried
    again on the studio's next open, not on every ask). Free: local only."""
    scene = await _scene(run_id, target, **_opt(variant=variant))
    _analyse_scene(scene, retry=False)
    a = _analysis(scene)
    done, total, state = _analysis_state(scene)
    if a is None:
        return {"state": "failed", "speaker_frames": {"done": 0, "total": 0},
                "error": "This scene has no clip to follow."}
    extra = {"has_person": has_person(scene)} if scene.kind != "scene" else {}
    if state == "ready" and a.track is not None:
        body = {"state": "ready", **a.track.public(), **extra}
        masks = await _thread(_bank_masks, scene, a)
        if masks is not None:
            body["masks"] = masks
        return body
    body: dict[str, Any] = {"state": state, "speaker_frames": {"done": done, "total": total}, **extra}
    if state == "failed":
        body["error"] = a.error
    return body


def _bank_masks(scene: Scene, a: _Analysis) -> Optional[dict]:
    """The speaker exactly as the matte bank cuts her, for the moving
    preview's quick stand-in: frame i's matte is the same for every frame
    of its keyframe window (mattes.MatteBank.at), so one sample per window
    — at its first frame, 1/MASK_DIV size, bars cleared — in one grey PNG
    strip (sample k in rows [k·h, (k+1)·h)). `step`: hold each sample to
    the next (never blended: that is how the bank cuts), and a window never
    spans a jump cut (`cuts`, seconds). Made once per bank; None when there
    is none, or it can't be served. Synchronous."""
    import cv2
    import numpy as np
    from PIL import Image

    bank = a.bank
    if bank is None:
        return None
    try:
        h8 = max(1, bank.height // speaker_track.MASK_DIV)
        w8 = max(1, bank.width // speaker_track.MASK_DIV)
        starts = [i for i in range(bank.n) if i == 0 or bank.keys_of(i) != bank.keys_of(i - 1)]
        out = scene.work / HOUSE_DIR / f"masks_{bank.key}.png"
        url = url_for(out)
        if not out.exists():
            rows = []
            for i in starts:
                m = bank.at(i, bars=bank.bars)
                rows.append(np.clip(np.rint(cv2.resize(m, (w8, h8), interpolation=cv2.INTER_AREA) * 255.0),
                                    0, 255).astype(np.uint8))
            out.parent.mkdir(parents=True, exist_ok=True)
            tmp = out.with_name(f"{out.stem}.{os.getpid()}.{threading.get_ident()}.tmp.png")
            Image.fromarray(np.concatenate(rows, axis=0), "L").save(tmp, format="PNG")
            os.replace(tmp, out)
    except (OSError, ValueError, RuntimeError):
        log.exception("[studio] the speaker's masks for the moving preview could not be made")
        return None
    return {"url": url, "times": [round(i / bank.fps, 4) for i in starts], "width": int(w8), "height": int(h8),
            "cuts": [round(c / bank.fps, 4) for c in bank.cuts], "step": True}


# ---------------------------------------------------------------------------
# Made in house: the design still, the exact moment, the draft render
# ---------------------------------------------------------------------------

# Under the scene's folder: the design stills' pattern art, the frozen
# patterns the renders draw, the exact moments and the draft renders
# (house.draft_path: a Generate of the same spec copies one).
HOUSE_DIR = "house"
HOUSE_STILL = "c0_still.png"
DRAFTS_KEPT = 6                  # draft renders kept per scene (~1-3 MB each)
HOUSE_SPECS_KEPT = 4             # specs held in memory (their art: a few MB each)
HOUSE_ONLY = "Only a draft made in house has a render to play."
ICONS_NOT_SHOWN = "The icons shown aren't here any more — look at them again."
NOT_FROZEN = ("A pattern of this draft couldn't be drawn in the preview, so its exact video can't be made "
              "yet — Generate still places it.")
ANALYSIS_FAILED = "This scene couldn't be analysed (the speaker, the camera), so nothing exact can be made."


@dataclass
class HouseSpec:
    """A draft as the in-house run will make it: the plate spec (house.
    plate_spec — the run's own resolver), its render key, the scene clip
    (and, added on top of a version Omni made, that version's clip under the
    plate), the scene's matte bank and camera, and the icons it stamps."""
    spec: Any
    key: Optional[str]
    clip: Path
    under: Optional[Path]
    bank: Any
    track: Any
    icons: list
    lost: list


_HOUSE_SPECS: dict[str, asyncio.Task] = {}


async def _quiet_step(*_a, **_k) -> None:
    return None


async def as_generated(run_id: int, target: str, recipe: rc.Recipe, base: Optional[hbase.BaseSpec] = None,
                       plan_key: Optional[str] = None, *, end_frame: Optional[int] = None,
                       variant: Optional[str] = None) -> rc.Recipe:
    """The draft as Generate will store it: the icons of the plan shown
    (`plan_key`, the one this scene showed — its props.resolved) and each
    pattern frozen as its preview drew it (final_patterns) — so a draft
    render, an exact moment and the run are made from the same recipe. 409
    when the draft has icons and that plan isn't here any more."""
    plain = icon_el.strip_resolved(pattern_el.strip_resolved(recipe))
    r = plain
    if rc.icons_by_pipeline(plain):
        kept = kept_plan(run_id, target, plan_key, variant=variant) if plan_key else None
        el = rc.icon_element(plain)
        if kept is None or el is None:
            raise HTTPException(409, ICONS_NOT_SHOWN)
        props = {**el.props, "resolved": kept.get("resolved") or []}
        r = rc.normalize(plain.model_copy(update={"elements": [
            e.model_copy(update={"props": props}) if e.id == el.id else e for e in plain.elements]}))
    if rc.pattern_elements(plain):
        r = pattern_el.materialise(r, await final_patterns(run_id, target, plain, base,
                                                                  **_opt(end_frame=end_frame, variant=variant)))
    return r


def _spec_memo(scene: Scene, stored: rc.Recipe, base: Optional[hbase.BaseSpec], n: Optional[int] = None) -> str:
    body = [str(scene.work), stored.model_dump(mode="json"),
            [base.version_id, base.mode] if base is not None and base.mode == "stack" else None]
    if n is not None:
        body.append({"n": int(n)})              # a user end only (_out_n): every other memo as it was
    return hashlib.sha256(json.dumps(body, sort_keys=True, default=str).encode("utf-8")).hexdigest()[:20]


async def house_spec(run_id: int, target: str, stored: rc.Recipe,
                     base: Optional[hbase.BaseSpec] = None, *, end_frame: Optional[int] = None,
                     variant: Optional[str] = None) -> HouseSpec:
    """A recipe as Generate stores it (as_generated: every pattern frozen,
    the icons frozen) resolved exactly as house.edit_scene resolves it: the
    scene's analysis (waited for), each pattern from its frozen place
    (pattern_el.from_resolved), our icons in one Stamper for the scene
    (added on top of a version with ours and no icon element, that
    version's), the plate spec and its render key. Shared by concurrent
    callers and the last HOUSE_SPECS_KEPT kept. 409 when a pattern is not
    frozen; 503 when the scene could not be analysed. `end_frame`: the
    scene's end (module doc) — the plate stops at n."""
    scene = await _scene(run_id, target, **_opt(variant=variant))
    n = await _out_n(scene, end_frame)
    memo = _spec_memo(scene, stored, base, n)
    task = _HOUSE_SPECS.get(memo)
    if task is None or (task.done() and (task.cancelled() or task.exception() is not None)):
        task = asyncio.ensure_future(_house_spec(scene, stored, base, n))
    _HOUSE_SPECS.pop(memo, None)
    _HOUSE_SPECS[memo] = task                       # the most recent last
    for old in list(_HOUSE_SPECS):
        if len(_HOUSE_SPECS) <= HOUSE_SPECS_KEPT:
            break
        if old != memo and _HOUSE_SPECS[old].done():
            _HOUSE_SPECS.pop(old, None)
    return await asyncio.shield(task)


async def _house_spec(scene: Scene, stored: rc.Recipe, base: Optional[hbase.BaseSpec],
                      n: Optional[int] = None) -> HouseSpec:
    from . import house

    clip = _scene_clip(scene)
    task = _analyse_scene(scene)
    if clip is None or task is None:
        raise HTTPException(404, "This scene has no clip.")
    await asyncio.shield(task)
    a = _analysis(scene)
    if a is None or a.step != "ready" or a.track is None or a.bank is None:
        raise HTTPException(503, ANALYSIS_FAILED)
    bank, track = a.bank, a.track
    els = rc.pattern_elements(stored)
    if els and not all(pattern_el.frozen(e) for e in els):
        raise HTTPException(409, NOT_FROZEN)
    intro = scene.ctx.intro
    # Frozen patterns are never planned; the job still carries the LOCAL
    # mask, as every pattern job the studio makes does (the run's own
    # fallback, person_matte.person_mask, can pay Gemini).
    job = pattern_el.PatternJob(
        exp_id=0, aspect=scene.aspect, out_dir=scene.work / HOUSE_DIR / "frozen" / _spec_memo(scene, stored, None)[:12],
        intro=intro.source if intro else None, intro_is_still=bool(intro and intro.is_still),
        seed=stored.seed or default_seed(scene.run_id, scene.target), plan_frame=_no_frame, step=_quiet_step,
        person_mask=_mask)
    pairs: list[tuple] = []
    for el in els:
        res = await pattern_el.from_resolved(el, job)
        if res.ref is not None:
            pairs.append((el, res.ref))
    under = None
    inherited = None
    if base is not None and base.mode == "stack":
        bv = await prepare_base(scene.run_id, scene.target, base.version_id)
        parts = [Path(p) for p in bv.sources]
        # A house pass: over the very clip it was drawn over, its passes'
        # own patterns under the new ones (prepare_base: house.flatten).
        under = bv.under or (parts[0] if len(parts) == 1 else await _thread(
            media.concat_exact, parts, scene.work / f"on_v{bv.version_id}" / "under.mp4"))
        pairs = [*(bv.kept or []), *pairs]
        inherited = hbase.inherited_icons(stored, bv)
    # As runner.icons_for: the frozen plan (or the version's own, carried
    # over), those whose glyph can be read, one Stamper over the whole clip.
    resolved: list = []
    if rc.icons_by_pipeline(stored):
        resolved, _made = await icon_el.plan_for_run(stored)
    elif inherited is not None:
        resolved = list(inherited)
    stamper = None
    lost: list = []
    if rc.icons_by_pipeline(stored) or inherited is not None:
        resolved, lost = await runner._loadable(resolved)
        spans = await _thread(runner._spans, [clip])
        stamper = (await icon_el.stampers(resolved, spans, frame_size=(scene.width, scene.height)))[0]
    person = await _thread(house.person_of, scene.kind, bank, scene.src)
    spec = await _thread(house.plate_spec, stored, pairs, width=bank.width, height=bank.height, fps=bank.fps,
                         n=min(bank.n, n) if n is not None else bank.n, bars=bank.bars, stamper=stamper,
                         icons=resolved, person=person)
    key = await _thread(house.render_key, spec, source=clip, under=under, camera_key=track.key, bank_key=bank.key)
    return HouseSpec(spec=spec, key=key, clip=clip, under=under, bank=bank, track=track, icons=list(resolved),
                     lost=list(lost))


async def _no_frame() -> Optional[Path]:
    return None


def _frame_of(clip: Path, i: int) -> Any:
    """Frame i of a clip (BGR), decoded in order from its start — the very
    frame, as the matte bank reads it. Synchronous."""
    import cv2

    cap = cv2.VideoCapture(str(clip))
    if not cap.isOpened():
        raise RuntimeError(f"cannot open {Path(clip).name}")
    try:
        got = None
        for _ in range(int(i) + 1):
            ok, f = cap.read()
            if not ok:
                break
            got = f
    finally:
        cap.release()
    if got is None:
        raise RuntimeError(f"no frames in {Path(clip).name}")
    return got


def _ingredients(hs: HouseSpec, i: int) -> dict:
    """What the moving preview's stand-in draws at frame i: the camera
    ([a, b, tx, ty]: frame 0's pixels to frame i's) and each pattern's
    phase and progress there."""
    import numpy as np

    m = np.asarray(hs.track.at(i), dtype=np.float64) if hs.track is not None else np.eye(3)[:2]
    t = i / float(hs.spec.fps)
    layers = []
    for ly in hs.spec.layers:
        phase, q = ly.timing.phase(t) if ly.timing is not None else ("after", 1.0)
        layers.append({"element_id": ly.element_id, "phase": phase, "q": round(float(q), 4)})
    return {"camera": [round(float(m[0, 0]), 6), round(float(m[1, 0]), 6), round(float(m[0, 2]), 3),
                       round(float(m[1, 2]), 3)], "layers": layers}


async def moment(run_id: int, target: str, recipe: rc.Recipe, t: float, base: Optional[hbase.BaseSpec] = None,
                 plan_key: Optional[str] = None, *, end_frame: Optional[int] = None,
                 variant: Optional[str] = None) -> dict:
    """POST /house/moment (and /icons/moment for a draft made in house):
    the frame at `t` (scene seconds) exactly as the run will make it —
    over_frame(the footage's frame i, plate.plate_at(spec, i)) from the very
    spec the run resolves (house_spec: the patterns frozen as previewed,
    the icons of the plan shown, the scene's matte bank and camera). Cached
    per render key and frame. {url, frame, t, key, ingredients}. Free. A
    scene cut short (`end_frame`) has no frame past its end: t is clamped
    below it."""
    from . import plate as pl

    stored = await as_generated(run_id, target, recipe, base, plan_key, **_opt(end_frame=end_frame, variant=variant))
    hs = await house_spec(run_id, target, stored, base, **_opt(end_frame=end_frame, variant=variant))
    scene = await _scene(run_id, target, **_opt(variant=variant))
    spec = hs.spec
    i = min(max(0, int(round(max(0.0, float(t)) * spec.fps))), max(0, spec.n - 1))
    tag = hs.key or _spec_memo(scene, stored, base)
    png = scene.work / HOUSE_DIR / "moments" / f"{tag}_{i}.png"
    if not png.is_file():
        def make() -> None:
            frame = _frame_of(hs.under or hs.clip, i)
            plate = pl.plate_at(spec, i, camera=hs.track, bank=hs.bank)
            _write_png(png, pl.over_frame(frame, plate))

        await _thread(make)
        try:
            old = sorted(png.parent.glob("*.png"), key=lambda f: f.stat().st_mtime)[:-MOMENTS_KEPT]
            for f in old:
                f.unlink(missing_ok=True)
        except OSError:
            pass
    else:
        _served(png)
    return {"url": url_for(png), "frame": i, "t": round(i / spec.fps, 3), "key": hs.key,
            "ingredients": _ingredients(hs, i)}


@dataclass
class _Draft:
    """One draft render: its key, where it goes, its stop switch and how far
    it got (queued | running | ready | failed | cancelled)."""
    key: str
    folder: str
    out: Path
    stop: threading.Event = field(default_factory=threading.Event)
    task: Optional[asyncio.Task] = None
    state: str = "queued"
    done: int = 0
    total: int = 0
    error: str = ""
    seconds: float = 0.0


# By render key: every draft render asked for this session (a finished one
# is only a record; the file is the render).
_DRAFTS: dict[str, _Draft] = {}
# Each ask for a draft render is numbered as it arrives, and each scene
# folder remembers the newest ask that went on to claim its render. An ask
# that is resolved only after a newer one claimed (an edit, then a quick
# undo: the old draft's spec is slower to make) is stale: it neither starts
# a render nor stops the newer one's.
_ASK_NO = itertools.count(1)
_CLAIMED: dict[str, int] = {}


def _draft_public(d: _Draft) -> dict:
    body: dict[str, Any] = {"key": d.key, "state": d.state,
                            "progress": round(d.done / d.total, 3) if d.total else (1.0 if d.state == "ready" else 0.0)}
    if d.state == "ready" and d.out.is_file():
        body["url"] = url_for(d.out)
        body["seconds"] = round(d.seconds, 1)
    if d.error:
        body["error"] = d.error
    return body


def cancel_drafts(folder: Path, *, keep: Optional[str] = None) -> int:
    """Stop every draft render of this scene folder but `keep` (an edit
    made them stale; a Generate outranks them): a queued one is dropped, a
    running one stops at its next frame and leaves no file. How many."""
    stopped = 0
    for d in list(_DRAFTS.values()):
        if d.folder != str(folder) or d.key == keep or d.state not in ("queued", "running"):
            continue
        d.stop.set()
        if d.state == "queued" and d.task is not None:
            d.task.cancel()
        d.state = "cancelled"
        stopped += 1
    return stopped


async def draft_ready(key: Optional[str]) -> bool:
    """Wait for the studio's render of this very spec when one is under
    way (a Generate then copies it rather than render it again). Whether
    one was waited for."""
    d = _DRAFTS.get(key or "")
    if d is None or d.task is None or d.task.done() or d.state not in ("queued", "running"):
        return False
    try:
        await asyncio.shield(d.task)
    except (asyncio.CancelledError, Exception):  # noqa: BLE001 — the run renders it itself then
        pass
    return True


async def _draft_render(d: _Draft, hs: HouseSpec) -> None:
    from . import house

    t0 = time.monotonic()
    try:
        async with _gate():
            if d.stop.is_set():
                raise house.Cancelled()
            d.state = "running"

            def made(done: int, total: int) -> None:
                d.done, d.total = done, total

            await _thread(house.render, hs.spec, hs.clip, d.out, camera=hs.track, bank=hs.bank, under=hs.under,
                          progress=made, cancel=d.stop.is_set)
        d.state, d.seconds = "ready", time.monotonic() - t0
        await _thread(_prune_drafts, d.out.parent, d.out)
    except (house.Cancelled, asyncio.CancelledError):
        d.state = "cancelled"
    except Exception as e:  # noqa: BLE001 — said on the next poll; the stand-in plays meanwhile
        d.state, d.error = "failed", (str(e) or type(e).__name__)[:300]
        log.exception("[studio] a draft render failed (%s)", d.key)


def _prune_drafts(folder: Path, keep: Path) -> None:
    """The newest DRAFTS_KEPT renders of a scene kept (and `keep`, and any
    being made). Synchronous; what can't be removed now is left."""
    busy = {str(d.out) for d in _DRAFTS.values() if d.state in ("queued", "running")}
    try:
        files = sorted(folder.glob("*.mp4"), key=lambda f: f.stat().st_mtime)
    except OSError:
        return
    for f in files[:-DRAFTS_KEPT]:
        if f != keep and str(f) not in busy:
            try:
                f.unlink()
            except OSError:
                pass


async def house_render(run_id: int, target: str, recipe: rc.Recipe, base: Optional[hbase.BaseSpec] = None,
                       plan_key: Optional[str] = None, *, end_frame: Optional[int] = None,
                       variant: Optional[str] = None) -> dict:
    """POST /house/render: the draft rendered exactly as Generate will make
    it (house_spec → house.render), in the background: {key, state, progress,
    url once ready}. Single-flight by render key (asked again, the same
    render answers); one at a time with the scene analyses (the gate); every
    other draft render of this scene is stopped (the draft moved on) — unless
    a newer ask claimed the scene's render while this one was being resolved
    (_CLAIMED): then this one starts and stops nothing. Kept
    as house.draft_path(<scene>, key), which a Generate of the same spec
    copies. Free."""
    from . import house

    asked = next(_ASK_NO)
    stored = await as_generated(run_id, target, recipe, base, plan_key, **_opt(end_frame=end_frame, variant=variant))
    hs = await house_spec(run_id, target, stored, base, **_opt(end_frame=end_frame, variant=variant))
    if not hs.key:
        raise HTTPException(409, "The scene's clip is missing, so it can't be rendered.")
    scene = await _scene(run_id, target, **_opt(variant=variant))
    out = house.draft_path(scene.work, hs.key)
    d = _DRAFTS.get(hs.key)
    folder = str(scene.work)
    if _CLAIMED.get(folder, 0) > asked:
        # Stale: a newer ask claimed this scene's render meanwhile. What there
        # is of this one is said; nothing is started or stopped.
        if d is not None:
            return _draft_public(d)
        return _draft_public(_Draft(key=hs.key, folder=folder, out=out,
                                    state="ready" if out.is_file() else "cancelled"))
    _CLAIMED[folder] = asked
    if out.is_file():
        if d is None or d.state != "ready":
            d = _DRAFTS[hs.key] = _Draft(key=hs.key, folder=str(scene.work), out=out, state="ready")
        _served(out)
        cancel_drafts(scene.work, keep=hs.key)
        return _draft_public(d)
    if d is None or d.state in ("failed", "cancelled") or (d.state == "ready" and not out.is_file()):
        cancel_drafts(scene.work, keep=hs.key)
        d = _DRAFTS[hs.key] = _Draft(key=hs.key, folder=str(scene.work), out=out)
        d.task = asyncio.ensure_future(_draft_render(d, hs))
    else:
        cancel_drafts(scene.work, keep=hs.key)
    return _draft_public(d)


def house_render_state(run_id: int, target: str, key: str, variant: Optional[str] = None) -> dict:
    """GET /house/render/{key}: how far that draft render is — or, one made
    before (another session), ready from its file. 404 for one never asked
    for here."""
    from . import house

    d = _DRAFTS.get(key)
    if d is not None:
        return _draft_public(d)
    scene = _PREPARED.get(_pkey(run_id, target, variant))
    if scene is not None and re.fullmatch(r"[0-9a-f]{20}", key or ""):
        out = house.draft_path(scene.work, key)
        if out.is_file():
            return _draft_public(_Draft(key=key, folder=str(scene.work), out=out, state="ready"))
    raise HTTPException(404, "No such render — ask for it again.")


# ---------------------------------------------------------------------------
# A version made in house, made again (its video lost)
# ---------------------------------------------------------------------------

REMAKE_NOT_FROZEN = ("That version can't be made again exactly: one of its patterns wasn't pinned where it was "
                     "drawn — Change it to make it afresh.")
REMAKE_UNKNOWN = "That version isn't being made again — ask for it again."


@dataclass
class _Remake:
    """One version being made again: how far it got (running | ready |
    failed), whether the studio's render of the same spec was copied, and
    whether it came out of the very spec its run rendered (`same`)."""
    exp_id: int
    state: str = "running"
    done: int = 0
    total: int = 0
    error: str = ""
    reused: bool = False
    same: Optional[bool] = None
    task: Optional[asyncio.Task] = None


# By version id: this session's remakes (a finished one is only a record;
# the file is the video).
_REMAKES: dict[int, _Remake] = {}


def _remake_public(r: _Remake) -> dict:
    body: dict[str, Any] = {"version_id": r.exp_id, "state": r.state, "reused": r.reused,
                            "progress": round(r.done / r.total, 3) if r.total else (1.0 if r.state == "ready" else 0.0)}
    if r.same is not None:
        body["same"] = r.same
    if r.error:
        body["error"] = r.error
    return body


async def remake(exp_id: int) -> dict:
    """POST /versions/{id}/remake: a version made in house, made again for
    $0 from what it is made of — its stored recipe (every pattern and icon
    frozen as Generate froze them) over the original (added on top of a
    version Omni made: that version's clean clip), with the scene's own
    matte bank and camera — resolved exactly as its run resolved it
    (house_spec: house.edit_scene's own path), so it is the same frames. The
    studio's render of that spec is copied when it is here. Written where
    its run wrote it (base.final_file), its still again when that is gone.
    In the background: {version_id, state: running | ready | failed,
    progress, reused, same once known}; asked again while it runs, the same
    one answers. 404: no such version; 409: one Omni made (REMAKE_OMNI) or
    one that didn't finish."""
    from .. import rebrand_store as store

    got = _REMAKES.get(int(exp_id))
    if got is not None and got.state == "running":
        return _remake_public(got)
    exp = await store.get_experiment(int(exp_id))
    if not exp:
        raise HTTPException(404, hbase.UNKNOWN)
    if not hbase.made_in_house(exp):
        raise HTTPException(409, hbase.REMAKE_OMNI)
    if not hbase.is_ready(exp):
        raise HTTPException(409, hbase.REMAKE_NOT_MADE)
    got = _REMAKES.get(int(exp_id))
    if got is not None and got.state == "running":
        # Asked twice at once: the first one answers (never two renders
        # writing the same file).
        return _remake_public(got)
    r = _REMAKES[int(exp_id)] = _Remake(exp_id=int(exp_id))
    r.task = asyncio.ensure_future(_remake(r, exp))
    return _remake_public(r)


def _int_or_none(v: Any) -> Optional[int]:
    try:
        return int(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def remake_state(exp_id: int) -> dict:
    """GET /versions/{id}/remake: how far making it again is (404: never
    asked for in this session)."""
    r = _REMAKES.get(int(exp_id))
    if r is None:
        raise HTTPException(404, REMAKE_UNKNOWN)
    return _remake_public(r)


def _copy_whole(src: Path, out: Path) -> None:
    """`src` copied to `out`, written whole or not at all. Synchronous."""
    import shutil

    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_name(f"{out.stem}.{os.getpid()}.{threading.get_ident()}.part{out.suffix}")
    try:
        shutil.copyfile(src, tmp)
        os.replace(tmp, out)
    finally:
        tmp.unlink(missing_ok=True)


async def _remake(r: _Remake, exp: dict) -> None:
    from .. import rebrand_store as store
    from . import house

    t0 = time.monotonic()
    try:
        opts = exp.get("options") or {}
        stored = rc.normalize(opts["recipe"])
        base = hbase.BaseSpec.of(opts.get("base"))
        run_id, target = int(exp["run_id"]), str(opts.get("target") or "")
        try:
            # Made for its own end (options.end_frame): made again to it.
            hs = await house_spec(run_id, target, stored, base, **_opt(end_frame=_int_or_none(opts.get("end_frame")),
                                                                       variant=opts.get("variant")))
        except HTTPException as e:
            if e.status_code == 409 and e.detail == NOT_FROZEN:
                raise HTTPException(409, REMAKE_NOT_FROZEN) from e
            raise
        scene = await _scene(run_id, target, **_opt(variant=opts.get("variant")))
        out = hbase.final_file(exp)
        made = house.draft_path(scene.work, hs.key) if hs.key else None
        if made is not None and made.is_file():
            await _thread(_copy_whole, made, out)
            r.reused = True
        else:
            def step(done: int, total: int) -> None:
                r.done, r.total = done, total

            async with _gate():
                await _thread(house.render, hs.spec, hs.clip, out, camera=hs.track, bank=hs.bank, under=hs.under,
                              progress=step)
            if made is not None:
                await _thread(house.hand_over, out, made)
        stack = base is not None and base.mode == "stack"
        still = hbase.still_path(int(exp["id"]), 0)
        if not stack and not still.is_file():
            await _thread(house.write_still, hs.spec, hs.clip, still, camera=hs.track, bank=hs.bank,
                          t_s=house.version_still_at(hs.spec))
        ran = ((((exp.get("arms") or {}).get("omni") or {}).get("stages") or {}).get("render") or {}).get("c0") or {}
        r.same = bool(hs.key) and hs.key == ran.get("key")
        await store.patch_arm(int(exp["id"]), "omni", {
            "final_path": th._to_rel(out),
            "remade": {"at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "key": hs.key, "same": r.same,
                       "reused_preview": r.reused, "seconds": round(time.monotonic() - t0, 2)},
        })
        r.state = "ready"
    except asyncio.CancelledError:
        r.state = "failed"
        raise
    except HTTPException as e:
        detail = e.detail.get("message") if isinstance(e.detail, dict) else e.detail
        r.state, r.error = "failed", str(detail)[:300]
    except Exception as e:  # noqa: BLE001 — said on the next poll
        log.exception("[studio] version #%s could not be made again", r.exp_id)
        r.state, r.error = "failed", (str(e) or type(e).__name__)[:300]


# ---------------------------------------------------------------------------
# The preview
# ---------------------------------------------------------------------------

def _keyed_pattern(el: rc.Element, colours: rc.Colours) -> dict:
    """A pattern element as its preview key holds it: with the colour it is
    really drawn in (brand, custom #7f2bf2 and a leftover custom under brand
    are one preview)."""
    d = el.model_dump(mode="json")
    d["props"] = {**(d.get("props") or {}),
                  "colour": pattern_el.colour_override(colours, el) or "intro"}
    return d


def _preview_key(recipe: rc.Recipe, seed: str, *, base: Optional[dict] = None, n: Optional[int] = None) -> str:
    """What changes a preview: every pattern (in its drawn colour), the seed,
    the guards, who writes the opening — and what the harness's own opening
    says (notes, feel), which the brief's placeholder does not show — plus
    the version it is built on, when it is. Made in house nothing is told
    anything and nothing is painted: the opening, the veil and the stray
    clean-up are left out, the engine and its renderer's version are in (its
    first part is the design still)."""
    body: dict[str, Any] = {
        "v": PREVIEW_VERSION,
        "patterns": [_keyed_pattern(el, recipe.colours) for el in rc.pattern_elements(recipe)],
        "seed": seed,
        "guards": recipe.guards.model_dump(mode="json"),
        "head": recipe.director.head,
    }
    if recipe.engine == "house":
        from .plate import HOUSE_VERSION

        body.pop("head")
        body["guards"] = {k: v for k, v in body["guards"].items() if k not in ("veil", "stray")}
        body["engine"] = "house"
        body["house"] = HOUSE_VERSION
    elif recipe.director.head == "harness":
        body["director"] = recipe.director.model_dump(mode="json")
    if base:
        body["base"] = base
    if n is not None:
        # Cut short by a user end (_out_n): the patterns' times and the design
        # still's moment follow it. Only then — every other key as it was.
        body["n"] = int(n)
    # Icons stamped by us: every prompt tells Omni to draw none of its own
    # (only then — a recipe without them keys exactly as before).
    if rc.icons_by_pipeline(recipe):
        body["icons"] = True
    elif base and base.get("mode") == "stack" and rc.icon_element(recipe, enabled=False) is not None:
        # On top of a version, an icon element switched off takes its icons
        # away (none stamped: Omni is not told about them) — no element at
        # all stamps the version's own again.
        body["icons"] = "off"
    return hashlib.sha256(json.dumps(body, sort_keys=True).encode("utf-8")).hexdigest()[:16]


def _read(saved: Path) -> Optional[dict]:
    try:
        return json.loads(saved.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _write(saved: Path, body: dict) -> None:
    """preview.json written whole or not at all (a reader never sees half)."""
    saved.parent.mkdir(parents=True, exist_ok=True)
    tmp = saved.with_name(f"{saved.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(body, default=str), encoding="utf-8")
    os.replace(tmp, saved)


def _tmp_of(path: Path, suffix: str) -> Path:
    """A writer's own temporary name beside `path`: this process AND this
    thread — two requests of one scene can make the same file at once (the
    Icons tab asks for its plan each second, while Generate asks too)."""
    return path.with_name(f"{path.stem}.{os.getpid()}.{threading.get_ident()}{suffix}")


def _made(tmp: Path, path: Path) -> None:
    """`tmp` put in place as `path` — or, when Windows refuses because another
    request has just made that very file (made from the same inputs: its
    name is its key) and has it open, `tmp` dropped. Synchronous."""
    try:
        os.replace(tmp, path)
    except PermissionError:
        if not path.exists():
            raise
        tmp.unlink(missing_ok=True)


def on_top(recipe: rc.Recipe) -> rc.Recipe:
    """A recipe added on top of a version opens with the harness's words (a
    brief describes the first look and would ask for it again)."""
    if recipe.director.head == "harness":
        return recipe
    return recipe.model_copy(update={"director": recipe.director.model_copy(update={"head": "harness"})})


async def preview(run_id: int, target: str, recipe: rc.Recipe,
                  base: Optional[hbase.BaseSpec] = None, *, end_frame: Optional[int] = None,
                  variant: Optional[str] = None) -> dict:
    """The draft's picture for `recipe` on this scene — and, for a recipe
    for Omni (only while Omni is switched on: rc.runnable), what Omni would
    be shown and told. Free.

    `base` builds on a finished version: "stack" draws the recipe's (new)
    layers on the version's own settled frames (prepare_base) — what Omni
    will be shown; "rebuild" draws as a fresh preview, and its omni_parts
    are only the parts whose Omni clip the run cannot reuse. A fresh take
    of a rebuild (Try again) is a fresh preview.

    Made in house (engine "house") it is the same picture — each pattern in
    its place, fully in, behind the speaker on frame 0 — and the places
    Generate freezes (final_patterns); nothing goes to Omni (omni_parts
    []), and the body says engine "house". `end_frame`: the scene's end
    (module doc): the patterns' times on the timeline run to it."""
    try:
        rc.runnable(recipe)
    except rc.RecipeError as e:
        raise HTTPException(400, {"message": str(e), "errors": e.errors}) from e
    t0 = time.monotonic()
    scene = await _scene(run_id, target, **_opt(variant=variant))
    if rc.icon_element(recipe) is not None:
        # The icon plan's slow inputs (the words, the spots, the director),
        # started as soon as a draft has icons — never before.
        _icon_warm(scene, recipe)
    stack = rebuild = None
    if base is not None and base.mode == "stack":
        stack = await prepare_base(run_id, target, base.version_id, **_opt(variant=variant))
        recipe = on_top(recipe)
    elif base is not None and not base.fresh:
        rebuild = await load_base(run_id, target, base)
    keyed: Optional[dict] = None
    if stack is not None:
        keyed = {"version_id": stack.version_id, "mode": "stack"}
    elif rebuild is not None:
        # Its parts' Omni keys hang on the opening too, which the key leaves
        # out for a brief-written one (notes and feel do not show in it).
        keyed = {"version_id": rebuild.version_id, "mode": "rebuild",
                 "director": recipe.director.model_dump(mode="json")}
    prepare_sec = time.monotonic() - t0
    seed = recipe.seed or default_seed(run_id, target)
    n = await _out_n(scene, end_frame) if recipe.engine == "house" else None
    out = scene.work / "preview" / _preview_key(recipe, seed, base=keyed, n=n)
    body = _read(out / "preview.json")
    if body is not None:
        _served(out / "preview.json")
    else:
        key = str(out)
        task = _PREVIEWS.get(key)
        if task is None:
            task = _PREVIEWS[key] = asyncio.ensure_future(
                _render(scene, recipe, seed, out, prepare_sec=prepare_sec, stack=stack,
                        rebuild=rebuild, n=n))
            task.add_done_callback(lambda t, k=key: (_PREVIEWS.pop(k, None),
                                                     t.cancelled() or t.exception()))
        body = await asyncio.shield(task)
    keys = body.get("omni_keys")
    body = {k: v for k, v in body.items() if k != "omni_keys"}
    if recipe.engine == "house":
        # Made in house: nothing goes to Omni (Generate is $0), and nothing
        # is told anything.
        body["omni_parts"] = []
        body["engine"] = "house"
    elif rebuild is not None:
        # Worked out per request: a clip of the version's that went missing
        # since is made again.
        body["omni_parts"] = _omni_parts(keys, rebuild.exp, len(body["chunks"]))
    body.update(_lineage(stack or rebuild))
    # One preview serves every recipe that draws the same thing; each gets
    # its own hash back.
    return {**body, "recipe_hash": rc.recipe_hash(recipe)}


async def _render(scene: Scene, recipe: rc.Recipe, seed: str, out: Path, *,
                  prepare_sec: float = 0.0, stack: Optional[hbase.BaseVersion] = None,
                  rebuild: Optional[hbase.BaseVersion] = None, n: Optional[int] = None) -> dict:
    t0 = time.monotonic()
    steps: list[dict] = []

    async def step(name: str, status: str = "running", detail: str = "") -> None:
        steps.append({"name": name, "status": status, "detail": detail[:300]})

    ctx = scene.ctx
    intro = ctx.intro
    colours = await _colours(scene)
    t_colours = time.monotonic()
    # On a version ("Add on top"): its settled frames, and the speaker's mask
    # rounded as the run's is (base.quantised) — the same target bytes.
    frames = list(stack.frames) if stack is not None else list(scene.firsts)

    async def mask(frame: Path) -> Any:
        m = await _mask(frame)
        return hbase.quantised(m) if stack is not None else m

    async def plan_frame() -> Optional[Path]:
        if stack is not None:
            return stack.frames[0]
        return await _plan_frame(scene)

    els = rc.pattern_elements(recipe)
    # Made in house: nothing is told anything, and its first part is the
    # design still (_design_still) — the plate the run lays over frame 0.
    made_in_house = recipe.engine == "house"
    alpha_dir = scene.work / "preview" / ALPHA_DIR
    job = pattern_el.PatternJob(
        exp_id=0, aspect=scene.aspect, out_dir=out / "pattern",
        intro=intro.source if intro else None, intro_is_still=bool(intro and intro.is_still),
        seed=seed, plan_frame=plan_frame, step=step, colours=colours,
        person_mask=mask if stack is not None else _mask,
    )
    if stack is not None:
        results = await pattern_el.build_all(els, recipe.colours, job, occupied=stack.occupied,
                                             taken=stack.taken)
    else:
        results = await pattern_el.build_all(els, recipe.colours, job)
    t_pattern = time.monotonic()
    built = [r for r in results if r.ref is not None]
    refs = [r.ref for r in built]
    # Which element each target layer is (runner.layers: one per built ref).
    layer_ids = [el.id for el, r in zip(els, results) if r.ref is not None]
    first_ref = refs[0] if refs else None
    brief = recipe.director.head == "brief"
    opening = "" if made_in_house else BRIEF_PLACEHOLDER if brief else runner.head(recipe, {})
    enters = hprompt.has_entrance(refs)
    keep_ink = gd.wants_veil_keep(recipe, refs)
    # Our icons are stamped: the recipe's own, or — on top of a version that
    # has ours, with no icon element of its own — that version's again
    # (runner._edit_stacked). Omni draws none.
    no_icons = rc.icons_by_pipeline(recipe) or (
        stack is not None and rc.icon_element(recipe, enabled=False) is None and hbase.has_icons(stack.exp))
    no_mask: list[int] = []

    clean_up = gd.cleans_strays(recipe)

    def guards(entering: bool) -> Optional[dict]:
        if not refs:
            return None
        if stack is None:
            # As runner.edit_scene: the icons need the speaker pass, and the
            # stray-drawing clean-up rides on it.
            g = gd.for_chunk(recipe.guards, refs, entering=entering, veil_keep=keep_ink,
                             stray_clean=clean_up and (no_icons or recipe.guards.speaker_front))
            if no_icons:
                g = dataclasses.replace(g, speaker_front=True)
            return g.public()
        # As runner._edit_stacked: the guards always run, clear the bars and
        # hold the version.
        return {**runner.stack_guards(recipe, refs, entering=entering, veil_keep=keep_ink).public(),
                "hold": True}

    async def part(j: int, first: Path) -> tuple[dict, Optional[list]]:
        entering = j == 0 and enters
        item = {"index": j, "seconds": scene.seconds[j], "frame_url": url_for(first),
                "entering": entering,
                "prompt": "" if made_in_house
                else hprompt.scene_prompt("", refs, entrance=entering, no_icons=no_icons).strip()
                if brief else hprompt.scene_prompt(opening, refs, entrance=entering, keep=stack is not None,
                                                   no_icons=no_icons),
                "target_url": None,
                "guards": guards(entering)}
        alphas: Optional[list] = None
        if refs:
            person = await mask(first)
            if person is None:
                no_mask.append(j)
            alphas = []
            tgt = await _thread(runner.target, first, out / f"c{j}_target.png", refs, recipe,
                                person=person, alphas=alphas)
            item["target_url"] = url_for(tgt)
            # The target is frame_url with these laid in, in this order, each
            # in its element's colour: what the studio repaints a colour
            # change with, before this preview is made again.
            item["layers"] = await _thread(_save_alphas, alphas, layer_ids, alpha_dir)
        return item, alphas

    parts = await asyncio.gather(*(part(j, f) for j, f in enumerate(frames)))
    t_chunks = time.monotonic()
    if refs and frames and rc.icon_element(recipe) is not None:
        # The patterns as one layer, uncut by the speaker: what the icons'
        # exact still lays under them (icon_moment) before the speaker goes back.
        await _thread(_save_layers, refs, recipe, frames[0], out / LAYERS_PNG)
    chunks = [item for item, _ in parts]
    house_body: Optional[dict] = None
    still_failed = False
    if made_in_house and refs and stack is None and chunks and _scene_clip(scene) is not None:
        # Next is the real frame: the plate the run lays over frame 0, the
        # patterns fully in, cut by the scene's own matte (one pose, never
        # two moments mixed). Added on top of a version Omni made, Next
        # stays drawn on that version's settled frames.
        try:
            house_body = await _thread(_design_still, scene, recipe,
                                       [(el, r.ref) for el, r in zip(els, results) if r.ref is not None], out,
                                       alpha_dir, n)
            chunks[0]["target_url"] = house_body["still_url"]
            if house_body.get("keep_url"):
                chunks[0]["keep_url"] = house_body["keep_url"]
        except Exception:  # noqa: BLE001 — Next is the target frame then, drawn again next time
            log.exception("[studio] the design still failed")
            still_failed = True
    # When each pattern shows (elements/pattern.timing: its span on this
    # scene, its entrance at the start and its exit at the end) — on the
    # plate's own clock (the clip's frames) when made in house, the parts'
    # seconds otherwise; what the timeline draws.
    duration = (house_body["n"] / float(house_body["fps"]) if house_body is not None
                else _plan_duration(scene, n))
    timings = {el.id: pattern_el.timing(el, duration=duration, entrance=res.ref.animation)
               for el, res in zip(els, results) if res.ref is not None}
    if house_body is not None and chunks[0].get("layers"):
        # The design still shows the patterns showing at its moment only (a
        # short transition and a strip after it never show together): its
        # layers — what the stage recolours and picks — are those.
        at = float(house_body["at"])
        shown = {i for i, tm in timings.items() if tm is not None and tm.phase(at)[0] not in ("before", "after")}
        still_layers = [ly for ly in chunks[0]["layers"] if ly.get("element_id") in shown]
        if len(still_layers) != len(chunks[0]["layers"]):
            chunks[0]["layers"] = still_layers
    boxes: dict[int, Optional[dict]] = {}
    for a in (parts[0][1] if parts else None) or []:
        boxes[a["index"]] = compose.bbox(a["alpha"])
    # What the stage's handles need of each pattern, and what a move by hand
    # did (on the first part's frame, with its speaker).
    hands: dict[int, dict] = {}
    if frames and refs:
        person0 = await mask(frames[0]) if any(not r.hand.identity for r in refs) else None
        for i, r in enumerate(refs):
            hands[i] = await _thread(_hand_info, r, frames[0], person0, boxes.get(i))

    patterns = []
    hand_warnings: list[str] = []
    for el, res in zip(els, results):
        ref = res.ref
        idx = next((i for i, r in enumerate(built) if r is res), None)
        info = hands.get(idx, {}) if idx is not None else {}
        if info.get("warning"):
            hand_warnings.append(info["warning"])
        tm = timings.get(el.id)
        if ref is not None and tm is None:
            hand_warnings.append(SPAN_AFTER_END.format(name=ref.name or ref.slug))
        patterns.append({
            "element_id": el.id,
            "slug": ref.slug if ref else el.props.get("slug"),
            "name": (ref.name or ref.slug) if ref else el.props.get("slug"),
            "colour": ref.colour if ref else None,
            "where": ref.where if ref else None,
            "animation": ref.animation if ref else None,
            "layout": ref.layout.public() if ref else None,
            "fell_back": bool(ref.fell_back) if ref else False,
            "placement": res.report,
            "moved": res.moved,
            "overlap": res.overlap,
            "box": boxes.get(idx) if idx is not None else None,
            "slot": res.slot,
            "error": res.error or None,
            # By hand: the move as drawn (a full-screen one's size clamped),
            # its box before the move, the pivot, whether it fills the frame
            # (pan / zoom / turn), its art's key (pattern_art), a warning.
            "hand": info.get("hand"),
            "base_box": info.get("base_box"),
            "pivot": info.get("pivot"),
            "full": bool(info.get("full")) if ref else False,
            "art_key": info.get("art_key"),
            "warning": info.get("warning"),
            # When it shows (scene s): null when it starts after the scene
            # ends, or when it could not be drawn.
            "timing": tm.public() if tm is not None else None,
        })
    warnings = [s["name"] + (f" — {s['detail']}" if s["detail"] else "")
                for s in steps if s["status"] == "failed"] + hand_warnings
    # An intro or outro: no one to find is no warning (a card has no
    # speaker), and one with no one in it is clean without a mask.
    bookend = scene.kind != "scene"
    person = has_person(scene) if bookend else True
    for j in sorted(no_mask) if person is True else ():
        warnings.append(f"Couldn't find the speaker in part {j + 1}, so the pattern shows over "
                        "them here. The real run tries again.")
    errors = [r.error for r in results if r.error]
    kept = _clean(results, no_mask, no_person=bookend and person is False) and not still_failed
    if bookend and house_body is not None and house_body.get("person") is None:
        kept = False                  # drawn before its analysis said who is in it
    # The parts a Generate of this recipe would send to Omni: every one for
    # a fresh version; on a version, only those the new layers show in (a
    # pattern shows in every part); a rebuild's are worked out per request
    # from the keys its run would have (omni_keys, _omni_parts).
    omni_parts = list(range(len(chunks)))
    if stack is not None:
        drawn = (parts[0][1] if parts else None) or []
        if not any(int(a["alpha"].max()) > 5 for a in drawn if a.get("alpha") is not None):
            omni_parts = []
    # Which of a "Change it"'s Omni clips can be reused (none to reuse in
    # house: nothing goes to Omni).
    omni_keys = (await _run_keys(scene, recipe, refs, rebuild, out, enters)
                 if rebuild and recipe.engine != "house" else None)
    if not kept:
        # Drawn again next time into the same files: a new address, so the
        # browser (which keeps these an hour) never shows this one for it.
        stamp = int(time.time() * 1000)
        for item in chunks:
            if item["target_url"]:
                item["target_url"] += f"?v={stamp}"
    body = {
        "recipe_hash": rc.recipe_hash(recipe),
        "seed": seed,
        # The brief's opening is only written at Generate; the harness's own
        # is already at the head of each part's prompt.
        "opening": opening if brief else "",
        "pattern": ({k: v for k, v in first_ref.public().items() if k not in ("path", "intro_frame")}
                    if first_ref else None),
        "pattern_error": "; ".join(errors) or None,
        "placement": built[0].report if built else (results[0].report if results else None),
        "patterns": patterns,
        "steps": steps,
        "warnings": warnings,
        "chunks": chunks,
        "colours": {"intro": colours.colour, "brand": pattern_ref.BRAND_GROUND,
                    "fell_back": bool(colours.fell_back)},
        "omni_parts": omni_parts,
        "timings": {"prepare": round(prepare_sec, 2), "colours": round(t_colours - t0, 2),
                    "pattern": round(t_pattern - t_colours, 2),
                    "chunks": round(t_chunks - t_pattern, 2),
                    "total": round(prepare_sec + time.monotonic() - t0, 2)},
        # Stored for this recipe (false: served once, drawn again next time —
        # the studio does not keep it either).
        "kept": kept,
    }
    if stack is not None or rebuild is not None:
        on = stack or rebuild
        body["base"] = {"version_id": on.version_id, "mode": on.mode}
    if omni_keys is not None:
        body["omni_keys"] = omni_keys
    if house_body is not None:
        body["house"] = house_body
    body = json.loads(json.dumps(body, default=str))
    if kept:
        _write(out / "preview.json", body)
        await _thread(_prune_previews, out.parent, out, frozenset(_PREVIEWS))
    return body


def _served(saved: Path) -> None:
    """A kept preview served again: it counts as new (_prune_previews)."""
    try:
        os.utime(saved)
    except OSError:
        pass


def _prune_previews(folder: Path, keep: Path, busy: frozenset) -> None:
    """A scene's newest PREVIEWS_KEPT preview folders kept — by when each was
    last served (its preview.json's time, else the folder's) — and `keep`
    and those being drawn (`busy`, their paths) always; then the alphas no
    kept preview names (_prune_alphas). Synchronous; what can't be removed
    now is left for next time."""
    import shutil

    try:
        dirs = [d for d in folder.iterdir() if d.is_dir() and d.name not in (ALPHA_DIR, ART_DIR)]
    except OSError:
        return
    if len(dirs) <= PREVIEWS_KEPT:
        return

    def when(d: Path) -> float:
        for f in (d / "preview.json", d):
            try:
                return f.stat().st_mtime
            except OSError:
                continue
        return 0.0

    gone = 0
    for d in sorted(dirs, key=when)[:-PREVIEWS_KEPT]:
        if d == keep or str(d) in busy:
            continue
        shutil.rmtree(d, ignore_errors=True)
        gone += 1
    if gone:
        _prune_alphas(folder)


def _prune_alphas(folder: Path) -> None:
    """The alphas under <folder>/ALPHA_DIR that no kept preview.json names,
    once ALPHAS_GRACE_SEC old. Synchronous."""
    named: set[str] = set()
    for saved in folder.glob("*/preview.json"):
        for chunk in (_read(saved) or {}).get("chunks") or []:
            for layer in chunk.get("layers") or []:
                named.add(str(layer.get("alpha_url") or "").split("?")[0].rsplit("/", 1)[-1])
    cutoff = time.time() - ALPHAS_GRACE_SEC
    for f in (folder / ALPHA_DIR).glob("*.png"):
        try:
            if f.name not in named and f.stat().st_mtime < cutoff:
                f.unlink()
        except OSError:
            pass


# The recipe's patterns as one RGBA layer, uncut, beside a preview of a
# recipe with icons (the icons' exact still lays it under them).
LAYERS_PNG = "layers.png"


def _save_layers(refs: list, recipe: rc.Recipe, frame: Path, out: Path) -> Path:
    """Every pattern layer of the recipe, composited in draw order at the
    frame's size — before the speaker cuts them. Written once, whole."""
    from PIL import Image

    if out.exists():
        return out
    with Image.open(frame) as im:
        width, height = im.size
    canvas = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    for layer in runner.layers(refs, recipe):
        img = layer.render(width, height)
        if img is not None:
            canvas.alpha_composite(img)
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_name(f"{out.stem}.{os.getpid()}.tmp.png")
    canvas.save(tmp, format="PNG")
    os.replace(tmp, out)
    return out


def _design_still(scene: Scene, recipe: rc.Recipe, pairs: list, out: Path,
                  alpha_dir: Optional[Path] = None, n: Optional[int] = None) -> dict:
    """A draft made in house, as its first part shows on Next: the design
    still (house.design_still — the scene's frame 0 with the plate at rest:
    the patterns that show at its moment fully in, cut by the scene's own
    matte bank; the camera is the identity on frame 0) as out/HOUSE_STILL,
    and each pattern as the moving preview's quick stand-in draws it — its
    art uncut (straight RGBA: frame-sized, or moved by hand its place
    unmoved past the frame, the frame's origin at `margin`), its ink box,
    its time, whether it follows the camera, the edge a slide comes from, its
    hand move. The icons are the stage's own to draw (their sprites). With
    `alpha_dir`, `keep_url` too: where the still's speaker cut leaves a
    pattern seen (white RGBA, alpha = 1 - the bank's matte on frame 0, the
    bars kept) — what the stage's stand-in cuts a pattern a hand moves by.
    Synchronous."""
    import numpy as np
    from PIL import Image

    from . import house
    from . import plate as pl

    a = _analysis(scene)
    if a is None:
        raise RuntimeError("the scene has no clip")
    bank = _bank_of(a)
    bars = bank.read_bars()
    # An intro or outro: whether anyone is in it is known once its bank is
    # filled (its analysis) — until then it is drawn as a scene is, and the
    # preview is not kept (`person` None: _render).
    person = (house.person_of(scene.kind, bank, scene.src) if scene.kind == "scene" or bank.filled
              or not str(scene.src or "").startswith("upload:") else None)
    spec = house.plate_spec(recipe, pairs, width=bank.width, height=bank.height, fps=bank.fps,
                            n=min(bank.n, n) if n is not None else bank.n, bars=bars, person=person)
    at = house.still_moment(spec)
    still = house.write_still(spec, a.clip, out / HOUSE_STILL, bank=bank, t_s=at)
    keep_url = None
    if alpha_dir is not None and spec.speaker_front:
        keep = np.clip(np.rint((1.0 - bank.at(0, bars=bars)) * 255.0), 0, 255).astype(np.uint8)
        keep_url = url_for(_white_png(keep, alpha_dir))
    layers = []
    for ly in spec.layers:
        art = _art_path(out, ly.element_id)
        if not art.exists():
            tmp = art.with_name(f"{art.stem}.{os.getpid()}.{threading.get_ident()}.tmp.png")
            Image.fromarray(pl.straight(ly.art), "RGBA").save(tmp, format="PNG")
            os.replace(tmp, art)
        layers.append({
            "element_id": ly.element_id, "art_url": url_for(art), "ink_box": [int(v) for v in ly.ink_box],
            "margin": [int(v) for v in ly.margin], "timing": ly.timing.public() if ly.timing is not None else None,
            "follow": ly.follow, "z": ly.z, "edge": ly.edge, "colour": ly.colour, "seed": ly.seed,
            "hand": [[round(float(v), 6) for v in row] for row in ly.hand] if ly.hand is not None else None,
        })
    body = {"engine_v": spec.engine_v, "still_url": url_for(still), "at": round(at, 4), "width": spec.width,
            "height": spec.height, "fps": spec.fps, "n": spec.n, "layers": layers, "keep_url": keep_url}
    if scene.kind != "scene":
        body["person"] = person
    return body


def _art_path(out: Path, element_id: str) -> Path:
    """Where a draft made in house keeps one pattern's art beside its preview
    (the stand-in's, and the icons' timed ink)."""
    return out / f"art_{re.sub(r'[^A-Za-z0-9_-]', '_', element_id)}.png"


def _white_png(alpha: Any, folder: Path) -> Path:
    """An alpha (uint8 HxW) as a white RGBA PNG named by its content under
    `folder`, written once, whole. Synchronous."""
    import numpy as np
    from PIL import Image

    seen = np.ascontiguousarray(alpha, dtype=np.uint8)
    digest = hashlib.sha256(repr(seen.shape).encode("ascii") + seen.tobytes()).hexdigest()[:16]
    path = Path(folder) / f"{digest}.png"
    if path.exists():
        _served(path)
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    rgba = np.full(seen.shape + (4,), 255, dtype=np.uint8)
    rgba[:, :, 3] = seen
    tmp = path.with_name(f"{digest}.{os.getpid()}.{threading.get_ident()}.tmp.png")
    Image.fromarray(rgba, mode="RGBA").save(tmp, format="PNG")
    try:
        os.replace(tmp, path)
    except OSError:
        tmp.unlink(missing_ok=True)
        if not path.exists():
            raise
    return path


def _art_key(ref: pattern_ref.PatternRef, width: int, height: int) -> str:
    """What a pattern's art (pattern_art) is made of: the pattern, its place
    and its mask, at this frame's size — not its colour (the art is white)
    nor its hand move (the art is unmoved)."""
    mask = None
    if ref.mask is not None:
        try:
            mask = [str(ref.mask), Path(ref.mask).stat().st_mtime_ns]
        except OSError:
            mask = [str(ref.mask), 0]
    body = [ART_VERSION, ref.slug, ref.layout.public(), mask, int(width), int(height)]
    return hashlib.sha256(json.dumps(body, sort_keys=True).encode("utf-8")).hexdigest()[:16]


def _hand_info(ref: pattern_ref.PatternRef, frame: Path, person: Any, box: Optional[dict] = None) -> dict:
    """What the stage's handles need of a pattern on this frame (its size),
    and what a move by hand did (preview patterns[i]): `base_box` its visible
    box before any move (frame fractions: the box a first move is grabbed
    at, whose centre is the pivot), `hand` the move as drawn (a full-screen
    composition's size clamped so it fills the frame; None: none), `pivot`,
    `full` (a full-screen place: panned, zoomed and turned, never boxed),
    `art_key`, and `warning` — a pattern moved by hand behind the speaker's
    head, mostly behind her, or mostly off the picture (the planner's own
    factors on this frame and `person`). `box`: its box as the target drew
    it — with no move, its box before any move, so it is not drawn again.
    Synchronous."""
    import numpy as np
    from PIL import Image

    from .. import pattern_planner

    if ref.hand.identity:
        with Image.open(frame) as im:
            w, h = im.size
        unmoved = pattern_ref._layer(ref, w, h) if box is None else None
        base_box = box if box is not None else (compose.bbox(unmoved.getchannel("A")) if unmoved is not None else None)
        return {"base_box": base_box, "full": ref.layout.placement == "full", "art_key": _art_key(ref, w, h),
                "hand": None, "warning": None,
                "pivot": ([round(base_box["x"] + base_box["w"] / 2, 4), round(base_box["y"] + base_box["h"] / 2, 4)]
                          if base_box else None)}
    with Image.open(frame) as im:
        rgb = np.asarray(im.convert("RGB"))
    h, w = rgb.shape[:2]
    unmoved = pattern_ref._layer(ref, w, h)
    base_box = compose.bbox(unmoved.getchannel("A")) if unmoved is not None else None
    hand = pattern_ref.drawn_hand(ref, w, h)
    out: dict[str, Any] = {"base_box": base_box, "full": ref.layout.placement == "full",
                           "art_key": _art_key(ref, w, h), "hand": None, "warning": None,
                           "pivot": ([round(base_box["x"] + base_box["w"] / 2, 4),
                                      round(base_box["y"] + base_box["h"] / 2, 4)] if base_box else None)}
    if hand.identity:
        return out
    pub = pattern_ref.hand_public(hand)
    out["hand"] = {k: pub[k] for k in ("dx", "dy", "scale", "rotate")}
    out["pivot"] = pub["pivot"]
    name = ref.name or ref.slug
    placed = pattern_ref.placed_layer(ref, w, h)
    ink = (np.asarray(placed.getchannel("A"), dtype=np.float32) / 255.0 if placed is not None
           else np.zeros((h, w), dtype=np.float32))
    whole = (float(np.asarray(unmoved.getchannel("A"), dtype=np.float64).sum()) / 255.0 * hand.scale ** 2
             if unmoved is not None else 0.0)
    if whole > 0 and float(ink.sum()) < HAND_ONSCREEN_MIN * whole and ref.layout.placement != "full":
        out["warning"] = f"Most of {name} is off the picture."
        return out
    fi = pattern_planner.frame_info(rgb, person)
    f = pattern_planner.factors(fi, pattern_planner._resize(ink, fi.w, fi.h), ref.colour, ref.layout)
    # Against its own place, unmoved: the approved strip already sits near
    # the head on a close framing (815: head 0.55), so a nudge is never
    # warned about — only a move that made it clearly worse.
    was = (pattern_planner.factors(fi, pattern_planner._resize(
        np.asarray(unmoved.getchannel("A"), dtype=np.float32) / 255.0, fi.w, fi.h), ref.colour, ref.layout)
        if unmoved is not None else {"head": 1.0, "visible": 1.0})
    if f["head"] < min(HAND_HEAD_MIN, was["head"] - HAND_WORSE):
        out["warning"] = f"{name} sits behind the speaker's head."
    elif f["visible"] < min(HAND_VISIBLE_MIN, was["visible"] - HAND_WORSE):
        out["warning"] = f"The speaker hides most of {name}."
    return out


def _save_alphas(alphas: list, ids: list[str], folder: Path) -> list[dict]:
    """One part's layers as its target lays them in (compose_target's
    capture: each alpha after the speaker's cut), in draw order — behind
    layers in list order, then front ones — each with the element it draws.
    Saved as white RGBA PNGs named by their content, so a colour change
    (the same alphas) keeps every address, and each is written once. A layer
    the speaker covers entirely is left out. Synchronous."""
    import threading

    import numpy as np
    from PIL import Image

    out = []
    for a in sorted(alphas, key=lambda x: x["z"] == "front"):
        seen = np.ascontiguousarray(a["visible"], dtype=np.uint8)
        if not seen.any():
            continue
        digest = hashlib.sha256(repr(seen.shape).encode("ascii") + seen.tobytes()).hexdigest()[:16]
        path = folder / f"{digest}.png"
        if path.exists():
            _served(path)                   # in use again (_prune_alphas)
        else:
            folder.mkdir(parents=True, exist_ok=True)
            rgba = np.full(seen.shape + (4,), 255, dtype=np.uint8)
            rgba[:, :, 3] = seen
            tmp = path.with_name(f"{digest}.{os.getpid()}.{threading.get_ident()}.tmp.png")
            Image.fromarray(rgba, mode="RGBA").save(tmp, format="PNG")
            try:
                os.replace(tmp, path)
            except OSError:
                # Another part wrote the same alpha a moment earlier (and it
                # may be being read): theirs is this one, byte for byte.
                tmp.unlink(missing_ok=True)
                if not path.exists():
                    raise
        i = a["index"]
        out.append({"element_id": ids[i] if i < len(ids) else None, "alpha_url": url_for(path)})
    return out


NO_SUCH_PATTERN = "That pattern isn't in this preview any more — look at it again."


async def pattern_art(run_id: int, target: str, recipe: rc.Recipe, element_id: str,
                      base: Optional[hbase.BaseSpec] = None, *, end_frame: Optional[int] = None,
                      variant: Optional[str] = None) -> dict:
    """POST /pattern/art: one pattern of the draft UNMOVED and uncut, past
    the frame (the frame padded by rebrand_pattern_ref.ART_PAD of a frame on
    every side, cut to its ink) as a white RGBA PNG, and where it lies —
    {art_url, art_key, art_window {x, y, w, h} (frame fractions; x and y may
    be negative), art_rect (a full-screen composition's own outline, frame
    fractions — its zoom never goes below covering the frame; null for
    every other)}. What the stage draws under the hand while a hand moves
    the pattern, before the exact frame lands. Placed as the draft's
    preview placed it (made now when it is not); cached on disk by its art
    key. Free, local. 404 when the preview has no such pattern."""
    from PIL import Image

    from .. import pattern_library

    body = await preview(run_id, target, recipe, base=base, **_opt(end_frame=end_frame, variant=variant))
    p = next((x for x in body.get("patterns") or [] if isinstance(x, dict) and x.get("element_id") == element_id),
             None)
    if not p or not isinstance(p.get("layout"), dict) or not p.get("slug"):
        raise HTTPException(404, NO_SUCH_PATTERN)
    scene = await _scene(run_id, target, **_opt(variant=variant))
    frame = (scene.firsts or [None])[0]
    if frame is not None and Path(frame).exists():
        with Image.open(frame) as im:
            width, height = im.size
    else:
        width, height = scene.width, scene.height
    entry = await pattern_library.get(str(p["slug"]))
    mask = await _thread(entry.mask_path) if entry is not None else None
    try:
        layout = pattern_ref.Layout(**p["layout"])
    except (TypeError, ValueError) as e:
        raise HTTPException(404, NO_SUCH_PATTERN) from e
    ref = pattern_ref.PatternRef(slug=str(p["slug"]), source="", path=Path("art.png"), ground="#ffffff",
                                 ink="#000000", fell_back=False, colour="#ffffff", layout=layout, mask=mask)
    key = _art_key(ref, width, height)
    folder = scene.work / "preview" / ART_DIR
    png, meta = folder / f"{key}.png", folder / f"{key}.json"
    body_out = _read(meta) if png.is_file() else None
    if body_out is None:
        def make() -> dict:
            got = pattern_ref.art_layer(ref, width, height)
            if got is None:
                raise HTTPException(404, NO_SUCH_PATTERN)
            img, (x, y, w, h) = got
            folder.mkdir(parents=True, exist_ok=True)
            tmp = png.with_name(f"{key}.{os.getpid()}.{threading.get_ident()}.tmp.png")
            img.save(tmp, format="PNG")
            os.replace(tmp, png)
            rect = pattern_ref.cover_rect(ref, width, height)
            out = {"art_key": key, "width": width, "height": height,
                   "art_window": {"x": round(x / width, 6), "y": round(y / height, 6),
                                  "w": round(w / width, 6), "h": round(h / height, 6)},
                   "art_rect": ({"x": round(rect[0] / width, 6), "y": round(rect[1] / height, 6),
                                 "w": round((rect[2] - rect[0]) / width, 6),
                                 "h": round((rect[3] - rect[1]) / height, 6)} if rect is not None else None)}
            _write(meta, out)
            return out

        body_out = await _thread(make)
    else:
        _served(png)
    return {**body_out, "art_url": url_for(png)}


async def final_patterns(run_id: int, target: str, recipe: rc.Recipe,
                         base: Optional[hbase.BaseSpec] = None, *,
                         end_frame: Optional[int] = None, variant: Optional[str] = None) -> dict[str, dict]:
    """What Generate freezes for the in-house engine, by element id: each
    pattern's props.resolved exactly as the preview the user saw drew it —
    its place (layout) and colour, the intro's ground and ink it was
    coloured from, its hand move and follow when it has them, and the
    renderer's version. Waits for the preview (made now when it is not). A
    pattern the preview could not draw is not frozen: the run places it
    itself, as a preview would."""
    from .house import HOUSE_VERSION

    body = await preview(run_id, target, recipe, base=base, **_opt(end_frame=end_frame, variant=variant))
    first = body.get("pattern") or {}
    drawn = {p.get("element_id"): p for p in body.get("patterns") or [] if isinstance(p, dict)}
    out: dict[str, dict] = {}
    for el in rc.pattern_elements(recipe):
        p = drawn.get(el.id) or {}
        if not isinstance(p.get("layout"), dict) or not p.get("colour"):
            continue
        res: dict[str, Any] = {"layout": p["layout"], "colour": str(p["colour"]).lower(), "engine_v": HOUSE_VERSION}
        for key in ("ground", "ink"):
            if first.get(key):
                res[key] = str(first[key]).lower()
        # The move as the preview drew it (a full-screen composition's size
        # clamped so it fills the frame) — the run draws it from the
        # recipe's own placement the same way; this is its record.
        hand = rc.hand_of(el)
        if isinstance(p.get("hand"), dict):
            res["hand"] = {**{k: float(p["hand"][k]) for k in ("dx", "dy", "scale", "rotate")},
                           "pivot": list(p.get("pivot") or hand.pivot)}
        elif not hand.identity:
            res["hand"] = {"dx": hand.dx, "dy": hand.dy, "scale": hand.scale, "rotate": hand.rotate,
                           "pivot": list(hand.pivot)}
        if el.props.get("follow") in ("room", "screen"):
            res["follow"] = el.props["follow"]
        out[el.id] = res
    return out


def _clean(results: list, no_mask: list[int], *, no_person: bool = False) -> bool:
    """Whether a preview may be kept for its recipe: every part had the
    speaker's mask (an intro or outro with no one in it, `no_person`, needs
    none), and every pattern was built as asked and placed by its planner.
    Anything else can come out differently next time (a mask found on
    reopen, a library that answers again) — kept, it would be served for
    that recipe for good, the 42 default's starting draft included."""
    if no_mask and not no_person:
        return False
    for r in results:
        reason = (r.report or {}).get("reason")
        if r.error or r.substituted or reason in ("error", "no frame"):
            return False
    return True


# ---------------------------------------------------------------------------
# Building on a finished version
# ---------------------------------------------------------------------------

async def load_base(run_id: int, target: str, spec: hbase.BaseSpec) -> hbase.BaseVersion:
    """The version a preview builds on, checked (base.load: the owner's
    words when it cannot be) once per session — a ready version does not
    change, so the previews after the first never ask the database."""
    key = (run_id, target, spec)
    bv = _BASES.pop(key, None)
    if bv is None:
        bv = await hbase.load(spec, run_id, target)
    _BASES[key] = bv                                    # the most recent last
    while len(_BASES) > BASES_KEPT:
        _BASES.pop(next(iter(_BASES)), None)            # the least recent
    return bv


def _keep_stack(key: str, task: asyncio.Task) -> None:
    """`task` as the most recently used prepared version; the least recent
    finished ones past BASES_KEPT are let go (one in flight never is)."""
    _STACKS.pop(key, None)
    _STACKS[key] = task
    for old in list(_STACKS):
        if len(_STACKS) <= BASES_KEPT:
            break
        if old != key and _STACKS[old].done():
            _STACKS.pop(old, None)


async def prepare_base(run_id: int, target: str, version_id: int, *,
                       variant: Optional[str] = None) -> hbase.BaseVersion:
    """"Add on top" of a version on this scene: its parts conformed to the
    scene's, their settled first frames and the ink its graphics hold
    (base.stack_parts — as the run makes them, so the preview on it is what
    Omni will be shown), made once under <scene>/on_v<id>/ and shared by
    every preview on it. The speaker masks of those frames are warmed at
    once. 409/410 (the owner's words) when it cannot be built on."""
    scene = await _scene(run_id, target, **_opt(variant=variant))
    checked = await load_base(run_id, target, hbase.BaseSpec(version_id=version_id, mode="stack"))
    key = f"{scene.work}|{version_id}"
    task = _STACKS.get(key)
    if task is None or (task.done() and (task.cancelled() or task.exception() is not None)):
        async def make() -> hbase.BaseVersion:
            from . import house

            bv = dataclasses.replace(checked)
            folder = scene.work / f"on_v{version_id}"
            await hbase.stack_parts(bv, list(scene.chunks), folder, width=scene.width, height=scene.height)
            if bv.passes:
                # Made in house over a version Omni made: its own patterns
                # laid on that video's frames, as the run lays them.
                intro = scene.ctx.intro

                async def mask(frame: Path) -> Any:
                    return hbase.quantised(await _mask(frame))

                job = pattern_el.PatternJob(
                    exp_id=0, aspect=scene.aspect, out_dir=folder, intro=intro.source if intro else None,
                    intro_is_still=bool(intro and intro.is_still), seed=default_seed(scene.run_id, scene.target),
                    plan_frame=_no_frame, step=_quiet_step, person_mask=mask)
                await house.flatten(bv, job, mask=mask)
            for frame in bv.frames:
                asyncio.ensure_future(_mask(frame))
            if hbase.has_icons(bv.exp) and len(bv.sources) > 1:
                # Its clean parts as one clip, for the icons' moving preview.
                joined = Path(bv.sources[0]).parent / CLEAN_MP4
                if not joined.exists():
                    try:
                        await _thread(media.concat, list(bv.sources), joined)
                    except Exception:  # noqa: BLE001 — the preview plays the version's own then
                        log.exception("[studio] joining version #%s's clean parts failed", version_id)
            return bv

        task = asyncio.ensure_future(make())
    _keep_stack(key, task)
    return await asyncio.shield(task)


async def base_scene(run_id: int, target: str, version_id: int, *, variant: Optional[str] = None) -> dict:
    """POST /base: a version made ready to add on top of — each part's
    settled frame (what the stage shows under the new layers), its layers
    (each with the version that drew it) and how many passes it already has."""
    bv = await prepare_base(run_id, target, version_id, **_opt(variant=variant))
    scene = await _scene(run_id, target, **_opt(variant=variant))
    return {
        "version_id": bv.version_id,
        "chunks": [{"index": j, "seconds": scene.seconds[j] if j < len(scene.seconds) else 0.0,
                    "frame_url": url_for(f)} for j, f in enumerate(bv.frames)],
        "layers": bv.layers,
        "depth": bv.depth,
        "max_depth": hbase.MAX_STACK_DEPTH,
    }


def _omni_parts(keys: Optional[list], exp: dict, parts: int) -> list[int]:
    """The parts a "Change it" run still sends to Omni: those whose key is
    not known, or not the one the version's clip was made with, or whose
    clip is gone (stages.Manifest.reusable — exactly the run's test)."""
    if keys is None:
        return list(range(parts))
    prior = st.Manifest(hbase.omni_stages(exp))
    return [j for j, k in enumerate(keys) if prior.reusable("omni", f"c{j}", k) is None]


def _run_opening(recipe: rc.Recipe, refs: list, bv: hbase.BaseVersion,
                 first: Optional[Path] = None) -> Optional[str]:
    """The opening a "Change it" run of `bv` will put at the head of every
    prompt, or None when it cannot be known now: the harness's own, or the
    version's brief when the run will reuse it (pipeline._kept_brief — on
    this clip, whose first frame is `first`), after the same several-pattern
    fallback (runner.run_brief). A brief the run writes anew comes back in
    other words — every part is then made again. Synchronous (it reads
    frames)."""
    if recipe.director.head == "harness":
        return runner.head(recipe, {})
    if not hbase.brief_reusable(recipe, refs, rc.options_from_recipe(recipe), bv.exp, first=first):
        return None
    brief, _ = runner.run_brief(recipe, dict(bv.exp.get("brief") or {}), refs)
    return runner.head(recipe, brief)


async def _run_mask(frame: Path) -> Any:
    """The speaker's mask exactly as a run gets it for this frame: a run
    cuts its own copy, so person_matte.person_mask answers from the local
    matte, unrounded (a studio mask may be the rounded .person.png). None
    when the local matte cannot read it — the run would ask Gemini there,
    which a preview never does, so that part is counted as made again.
    The last RUN_MASKS_KEPT are kept (a mask is a few MB)."""
    try:
        key = (str(frame), os.stat(frame).st_mtime_ns)
    except OSError:
        return None
    if key in _RUN_MASKS:
        _RUN_MASKS[key] = _RUN_MASKS.pop(key)           # the most recent last
        return _RUN_MASKS[key]
    mask = await _thread(person_matte._local_mask, Path(frame))
    _RUN_MASKS[key] = mask
    while len(_RUN_MASKS) > RUN_MASKS_KEPT:
        _RUN_MASKS.pop(next(iter(_RUN_MASKS)), None)   # the least recent
    return mask


async def _run_keys(scene: Scene, recipe: rc.Recipe, refs: list, bv: hbase.BaseVersion,
                    out: Path, enters: bool) -> list[Optional[str]]:
    """Each part's Omni key in a "Change it" run of `bv`, computed as
    runner.edit_scene computes it: the part's chunk (the scene's normalised
    copy, the same bytes a run cuts), the prompt (_run_opening + the
    patterns' clause), the TARGET drawn with the mask a run gets (_run_mask),
    the model, the aspect and the version's take. None where it cannot be
    known now — the part is then counted as made again (conservative: the
    estimate can be too high, never too low, as long as the run finds the
    same local matte)."""
    opening = await _thread(_run_opening, recipe, refs, bv, scene.firsts[0] if scene.firsts else None)
    take = hbase.take_of(bv.exp)
    no_icons = rc.icons_by_pipeline(recipe)

    async def key(j: int, first: Path) -> Optional[str]:
        if opening is None or j >= len(scene.chunks):
            return None
        text = hprompt.scene_prompt(opening, refs, entrance=j == 0 and enters, no_icons=no_icons)
        images: list[Path] = []
        if refs:
            person = await _run_mask(first)
            if person is None:
                return None
            images = [await _thread(runner.target, first, out / f"c{j}_run_target.png", refs,
                                    recipe, person=person)]
        return st.key(chunk=scene.chunks[j], prompt=text, images=images, model=omni.OMNI_MODEL,
                      aspect=scene.aspect, take=take)

    return list(await asyncio.gather(*(key(j, f) for j, f in enumerate(scene.firsts))))


# ---------------------------------------------------------------------------
# Icons: the plan
# ---------------------------------------------------------------------------

# A failed input (MODNet cannot run here, a clip that cannot be read) is kept
# this long before it is tried again: the studio asks every second while the
# icons warm up, and a failure must not be redone (or re-billed) on each ask.
ICON_RETRY_SEC = 600
ICON_STEPS = ("words", "spots", "director")
PLANS_KEPT = 40
SPRITES = "sprites"               # icons/sprites/<sha16>.png: each shown icon's settled sprite
SPRITES_KEPT = 120                # per scene (content-addressed; a live one is touched on reuse)
MOMENTS_KEPT = 120                 # exact stills kept per scene (icons/moments)
# Scenes whose icon inputs are held in memory (a track and its colours are
# ~1 MB); an older scene's are read back from its icons/ folder when asked.
ICON_SCENES_KEPT = 8


@dataclass
class _IconJob:
    """One of a scene's icon inputs: being worked out, done or failed."""
    task: asyncio.Task
    started: float
    finished: Optional[float] = None
    failed_at: Optional[float] = None

    @property
    def ok(self) -> bool:
        return self.task.done() and not self.task.cancelled() and self.task.exception() is None


# Per scene and input: "<work>|words", "<work>|spots",
# "<work>|director|<take>|<ai>". Started lazily (_icon_warm, icon_plan) —
# only once a draft has an icon element.
_ICON_TASKS: dict[str, _IconJob] = {}


def _icon_job(key: str, make) -> _IconJob:
    """The job for `key`: the one under way or done; a failed one is kept
    (and shown failed) for ICON_RETRY_SEC, then started again."""
    job = _ICON_TASKS.get(key)
    if job is not None:
        if not job.task.done() or job.ok:
            return job
        if job.failed_at is not None and time.time() - job.failed_at < ICON_RETRY_SEC:
            return job
    job = _IconJob(task=asyncio.ensure_future(make()), started=time.monotonic())
    _ICON_TASKS.pop(key, None)
    _ICON_TASKS[key] = job                          # the most recent last
    scenes = list(dict.fromkeys(k.split("|", 1)[0] for k in _ICON_TASKS))
    for old in scenes[:-ICON_SCENES_KEPT]:
        for k in [k for k, j in _ICON_TASKS.items() if k.startswith(old + "|") and j.task.done()]:
            _ICON_TASKS.pop(k, None)

    def done(t: asyncio.Task, j: _IconJob = job) -> None:
        j.finished = time.monotonic()
        if t.cancelled() or t.exception() is not None:
            j.failed_at = time.time()
            if not t.cancelled():
                log.warning("[studio] icons: %s failed: %s", key.split("|", 1)[-1], str(t.exception())[:200])

    job.task.add_done_callback(done)
    return job


def _icon_dir(scene: Scene) -> Path:
    return scene.work / "icons"


def _scene_clip(scene: Scene) -> Optional[Path]:
    """The scene's normalised clip (what a run edits)."""
    seg = scene.ctx.target_seg
    return Path(seg.base) if seg is not None and seg.base is not None else None


def _scene_lines(scene: Scene) -> tuple[str, list[str], str]:
    """(this scene's line, the other scenes' lines, the reel's topic)."""
    rcx = scene.ctx.run_ctx or {}
    kind, idx = _target_of(scene.target)
    lines = {int(k): str(v or "") for k, v in (rcx.get("scene_dialogue") or {}).items()}
    if kind != "scene":
        # Nothing is said on an intro or outro: no line of its own.
        return "", [lines[k] for k in sorted(lines) if lines[k].strip()], str(rcx.get("topic") or "")
    others = [lines[k] for k in sorted(lines) if k != idx and lines[k].strip()]
    return lines.get(idx, ""), others, str(rcx.get("topic") or "")


def _director_dir(scene: Scene) -> Path:
    """Where the scene's director keeps its answer and take: its own icons
    folder — the trimmed scene's for the full-length variant (director_from:
    one director for both)."""
    return (Path(scene.director_from) / "icons") if scene.director_from is not None else _icon_dir(scene)


def _take(scene: Scene) -> int:
    """The director's take for this scene ("Ask the AI again" bumps it)."""
    try:
        return int(json.loads((_director_dir(scene) / "take.json").read_text(encoding="utf-8"))["take"])
    except (OSError, ValueError, KeyError, TypeError):
        return 0


def _ask_again(scene: Scene) -> int:
    take = _take(scene) + 1
    _write(_director_dir(scene) / "take.json", {"take": take})
    return take


def _may_ask_again(scene: Scene, ai: bool) -> bool:
    """Whether "Ask the AI again" starts a new take: only once this take's
    director has answered without the AI (it was down, or failed) — never
    while it is still asking (each click would be another paid call), nor
    after the AI answered. A take this process never started (a restart)
    may be asked again."""
    job = _ICON_TASKS.get(f"{scene.work}|director|{_take(scene)}|{int(bool(ai))}")
    if job is None:
        return True
    if not job.task.done():
        return False
    if not job.ok:
        return True
    body = job.task.result() or {}
    return body.get("source") != "ai" or bool(body.get("failed_at"))


def _estimate(scene: Scene) -> dict:
    """The scene's words spread over its length (no Whisper)."""
    line, _others, _topic = _scene_lines(scene)
    return wordsmod.build_words(None, line, None, round(sum(scene.seconds), 3), note="no word timing")


async def _icon_words(scene: Scene) -> dict:
    clip = _scene_clip(scene)
    if clip is None or not clip.exists() or scene.kind != "scene":
        # An intro or outro has nothing said (its music is not words): no
        # Whisper run for it.
        return _estimate(scene)
    line, _others, _topic = _scene_lines(scene)
    return await _thread(wordsmod.scene_words, clip, line, _icon_dir(scene))


async def _icon_track(scene: Scene) -> tuple[Any, Optional[icon_el.Backgrounds]]:
    """The speaker over the scene (local MODNet only) and the scene's
    colours at its samples. Raises when the local matte cannot run — the
    plan then goes without (its spots are a guess)."""
    clip = _scene_clip(scene)
    if clip is None or not clip.exists():
        raise RuntimeError("the scene's clip is not on disk")
    track = await _thread(speaker_track.build, clip, _icon_dir(scene), cuts=list(scene.cuts))
    try:
        bg = await _thread(icon_el.backgrounds, [(clip, 0.0)], track, _icon_dir(scene) / "bg.npz")
    except Exception:  # noqa: BLE001 — colours then judged without the room
        log.exception("[studio] icons: reading the scene's colours failed")
        bg = None
    return track, bg


async def _words_of(scene: Scene, job: _IconJob) -> dict:
    try:
        return await asyncio.shield(job.task)
    except Exception:  # noqa: BLE001 — the estimate stands in
        return _estimate(scene)


async def _track_of(job: _IconJob) -> tuple[Any, Optional[icon_el.Backgrounds]]:
    try:
        return await asyncio.shield(job.task)
    except Exception:  # noqa: BLE001 — no track: the spots are a guess
        return None, None


async def _icon_director(scene: Scene, words_job: _IconJob, spots_job: _IconJob, take: int,
                         ai: bool) -> dict:
    if scene.kind != "scene":
        # An intro or outro is done by hand: the director is never asked
        # (never billed) there, whatever the recipe says.
        return dmod._body(None, "keywords", [], {}, [], note="nothing said")
    if scene.director_from is not None:
        # The full-length variant: the trimmed scene's director, as it is.
        return await dmod.direct(await director_ctx_from(scene, take=take, ai=ai))
    words = await _words_of(scene, words_job)
    track, _bg = await _track_of(spots_job)
    line, others, topic = _scene_lines(scene)
    return await dmod.direct(dmod.DirectorCtx(
        words=words, script=line, work_dir=_icon_dir(scene), topic=topic, others=others,
        frame0=track.frame0 if track is not None else None,
        spots=list(track.spots) if track is not None else [], take=take, ai=ai))


async def director_ctx_from(scene: Scene, *, take: int, ai: bool) -> dmod.DirectorCtx:
    """The full-length variant's director context, read from the trimmed
    scene it points to (director_from): that scene's words (its words.json,
    else heard on its clip — local), the first frame and spots of its
    speaker track (read back, else made on its clip — local MODNet), its
    folder for the answer — so the key is the very one the trimmed scene is
    answered under, whatever this clip's bytes: never a paid re-ask for the
    frames past the cut."""
    src = Path(scene.director_from)
    clip = _scene_clip(scene)
    trimmed = src / "base" / clip.name if clip is not None else None
    icons = src / "icons"
    line, others, topic = _scene_lines(scene)
    words = (await _thread(wordsmod.scene_words, trimmed, line, icons)
             if trimmed is not None and trimmed.exists() else _estimate(scene))
    track = None
    if trimmed is not None and trimmed.exists():
        try:
            track = await _thread(speaker_track.build, trimmed, icons, cuts=media.cuts_of(trimmed))
        except Exception:  # noqa: BLE001 — the spots are a guess then, as on the trimmed scene
            log.exception("[studio] icons: the trimmed scene's speaker track could not be read")
    return dmod.DirectorCtx(words=words, script=line, work_dir=icons, topic=topic, others=others,
                            frame0=track.frame0 if track is not None else None,
                            spots=list(track.spots) if track is not None else [], take=take, ai=ai)


async def _director_of(job: _IconJob, words: dict) -> dict:
    try:
        return await asyncio.shield(job.task)
    except Exception:  # noqa: BLE001 — keyword suggestions, never automatic icons
        cands = dmod.candidates(words)
        return {"version": dmod.DIRECTOR_VERSION, "key": None, "source": "keywords",
                "ideas": dmod.fallback(words, cands, dmod.default_catalog(), "AI unavailable"),
                "room": {"spots": [], "home": None, "home_n": None, "home_why": ""},
                "candidates": cands, "note": "AI unavailable", "failed_at": time.time()}


def _icon_jobs(scene: Scene, *, take: int, ai: bool) -> dict[str, _IconJob]:
    if scene.kind != "scene":
        ai = False                       # an intro or outro: never the AI (_icon_director)
    work = str(scene.work)
    was = _ICON_TASKS.get(f"{work}|words")
    words = _icon_job(f"{work}|words", lambda: _icon_words(scene))
    if was is not None and words is not was:
        # The words were worked out again (a failed try, retried): an answer
        # the director gave on the old ones indexes other words — asked
        # again on these (director.json keeps the words' digest in its key,
        # so the same words are never billed twice).
        for k in [k for k in _ICON_TASKS if k.startswith(f"{work}|director|")]:
            _ICON_TASKS.pop(k, None)
    spots = _icon_job(f"{work}|spots", lambda: _icon_track(scene))
    director = _icon_job(f"{work}|director|{take}|{int(bool(ai))}",
                         lambda: _icon_director(scene, words, spots, take, ai))
    return {"words": words, "spots": spots, "director": director}


def _icon_warm(scene: Scene, recipe: rc.Recipe) -> None:
    """Start the scene's icon inputs for a draft with an icon element."""
    el = rc.icon_element(recipe)
    if el is None:
        return
    try:
        ai = rc.icon_props(el).ai
    except rc.RecipeError:
        ai = True
    _icon_jobs(scene, take=_take(scene), ai=ai)


def _icon_steps(jobs: dict[str, _IconJob]) -> list[dict]:
    """The steps the studio lists while the icons warm up."""
    now = time.monotonic()
    out = []
    for name in ICON_STEPS:
        job = jobs.get(name)
        if job is None:
            out.append({"name": name, "state": "queued", "seconds": None})
            continue
        if not job.task.done():
            state, secs = "running", now - job.started
        else:
            state, secs = ("done" if job.ok else "failed"), (job.finished or now) - job.started
            # The AI could not answer: the keyword suggestions stand in.
            if job.ok and name == "director" and (job.task.result() or {}).get("failed_at"):
                state = "failed"
        out.append({"name": name, "state": state, "seconds": round(max(0.0, secs), 1)})
    return out


def _chunks(scene: Scene, n: Optional[int] = None) -> list[dict]:
    """The scene's parts on the timeline — cut short at a user end (n)."""
    spans = icon_el.chunk_spans(scene.seconds)
    if n is not None:
        end = n / float(media.FPS)
        spans = [(start, round(min(float(sec), end - start), 3)) for start, sec in spans if start < end]
    return [{"start": start, "seconds": sec} for start, sec in spans]


def _icon_enters(recipe: rc.Recipe, stack: Optional[hbase.BaseVersion]) -> bool:
    """Whether a pattern comes in over the first chunk (the icons then wait
    for it): a switched-on pattern with an entrance — its own (auto) or a
    picked one — or, added on top of a version, that version's own."""
    if any(e.animation.entrance != "none" for e in rc.pattern_elements(recipe)):
        return True
    return bool(stack is not None and hbase.entering(stack.exp, 0))


def _icon_zones(recipe: rc.Recipe, stack: Optional[hbase.BaseVersion], duration: float) -> Optional[list[dict]]:
    """Where no icon settles while a pattern comes in or goes out, from each
    pattern's time on this scene (elements/pattern.timing: the plate's own
    rule — icon.pattern_zones, C6), or None when they are exactly the zone
    the icons have always kept (_icon_enters: 0 to 1.5 s, or none) — so
    every plan made before spans keeps its key. Made in house only (an Omni
    pattern always comes in at the start). An entrance left "auto" counts
    as one, as _icon_enters counts it (what it is drawn as does not move a
    zone: only whether there is one)."""
    enters = _icon_enters(recipe, stack)
    if recipe.engine != "house":
        return None
    rows = []
    for el in rc.pattern_elements(recipe):
        entrance = el.animation.entrance if el.animation.entrance != "auto" else "fade"
        tm = pattern_el.timing(el, duration=duration, entrance=entrance)
        rows.append(tm.public() if tm is not None else None)
    zones = icon_el.pattern_zones(rows)
    if stack is not None and hbase.entering(stack.exp, 0):
        # The version added to comes in at the start, as it always has.
        both = {(z["from"], z["to"], z["why"]): z for z in [*zones, *icon_el.zones_of(True, None)]}
        zones = [both[k] for k in sorted(both)]
    return None if zones == icon_el.zones_of(enters, None) else zones


CLEAN_MP4 = "clean.mp4"


def _clean_of(stack: hbase.BaseVersion) -> Optional[Path]:
    """A version with our icons, without them: its clean part, or its clean
    parts joined (prepare_base) — what the icons being planned replace."""
    if not (hbase.has_icons(stack.exp) and stack.sources):
        return None
    if len(stack.sources) == 1:
        return Path(stack.sources[0])
    joined = Path(stack.sources[0]).parent / CLEAN_MP4
    return joined if joined.is_file() else None


def _underlay(scene: Scene, stack: Optional[hbase.BaseVersion]) -> dict:
    """What the moving preview plays under the icons: the scene, or the
    version being added to — without our icons when it has some (the plan's
    replace them; its final has them baked in)."""
    if stack is not None:
        clean = _clean_of(stack)
        if clean is not None:
            try:
                return {"kind": "version", "video_url": url_for(clean), "version_id": stack.version_id,
                        "clean": True}
            except ValueError:
                pass                              # not a studio file: the version's own
        arm = (pipeline.public(stack.exp).get("arms") or {}).get("omni") or {}
        # `baked`: its video carries our icons (no clean clip to play; a
        # house pass stamped its own, or carried them over): the moving
        # preview never draws icons over it (two sets).
        baked = hbase.has_icons(stack.exp) or (bool(stack.passes) and bool(hbase.icons_of(stack.exp)))
        return {"kind": "version", "video_url": arm.get("url"), "version_id": stack.version_id,
                "baked": baked}
    return {"kind": "scene", "video_url": public_scene(scene)["video_url"]}


def _small(mask: Any, w8: int, h8: int) -> Any:
    import cv2
    import numpy as np

    a = np.clip(np.asarray(mask, dtype=np.float32), 0.0, 1.0)
    return a if a.shape == (h8, w8) else cv2.resize(a, (w8, h8), interpolation=cv2.INTER_AREA)


def _preview_ink(out: Path, frame: Path, w8: int, h8: int) -> tuple[Any, Optional[str]]:
    """Where a preview's first part laid the patterns in (its target against
    the frame it was drawn on, as base.ink reads a version's), at the track's
    mask size — and that layer as an image (made once, beside the preview),
    which the moving preview lays only when the preview has no uncut
    LAYERS_PNG: it has the first frame's speaker cut out of it. Synchronous."""
    import numpy as np
    from PIL import Image

    tgt = out / "c0_target.png"
    if not tgt.is_file() or not Path(frame).is_file():
        return None, None
    with Image.open(tgt) as t, Image.open(frame) as f:
        a = np.asarray(t.convert("RGB"), dtype=np.int16)
        b = np.asarray(f.convert("RGB").resize((a.shape[1], a.shape[0])), dtype=np.int16)
    ink = (np.abs(a - b).max(axis=2) >= hbase.INK_MIN).astype(np.float32)
    layer = out / "icons_pattern.png"
    if not layer.exists():
        rgba = np.dstack([a.astype(np.uint8), (ink * 255).astype(np.uint8)])
        tmp = _tmp_of(layer, ".tmp.png")
        Image.fromarray(rgba, "RGBA").save(tmp, format="PNG")
        _made(tmp, layer)
    return _small(ink, w8, h8), url_for(layer)


def _track_masks(scene: Scene, track: Any) -> Optional[dict]:
    """The speaker's masks over the scene (the track's, 1/8 size, 2 a
    second) as one PNG strip — sample k in rows [k·h, (k+1)·h) — so the
    moving preview can put the speaker back over the pattern and the icons
    frame by frame, as the run does. Made once per track. Synchronous."""
    import numpy as np
    from PIL import Image

    masks = getattr(track, "masks", None)
    if masks is None or not len(masks):
        return None
    n, h8, w8 = masks.shape
    out = _icon_dir(scene) / f"masks_{track.key or 'track'}.png"
    if not out.exists():
        out.parent.mkdir(parents=True, exist_ok=True)
        tmp = _tmp_of(out, ".tmp.png")
        Image.fromarray(np.ascontiguousarray(masks.reshape(n * h8, w8), dtype=np.uint8), "L").save(tmp, format="PNG")
        _made(tmp, out)
    # The clip's jump cuts (s): the moving preview never blends the samples
    # either side of one (speaker_track: a sample each side of every cut).
    # And what the planner judges a box by — each sample's head box (frame
    # px; null: none found), the frame, the samples' spacing without a cut
    # (sample_frames at fps) and the letterbox bars at the masks' size (runs
    # [row, x0, x1) of bar cells, never the speaker) — so an icon dragged by
    # hand is outlined by the planner's own rule (speaker_track.Track.cover,
    # frontend lib/rebrandCover.ts).
    heads = [[int(v) for v in h] if int(h[0]) >= 0 else None for h in track.heads]
    bars = []
    b8 = track._bars8()
    for y in np.flatnonzero(b8.any(axis=1)):
        edge = np.diff(np.concatenate([[0], b8[y].astype(np.int8), [0]]))
        bars += [[int(y), int(a), int(b)] for a, b in zip(np.flatnonzero(edge == 1), np.flatnonzero(edge == -1))]
    return {"url": url_for(out), "times": [round(float(t), 3) for t in track.times], "width": int(w8),
            "height": int(h8), "cuts": [round(float(t), 4) for t in getattr(track, "cut_times", [])],
            "heads": heads, "frame": [int(track.width), int(track.height)], "sample_frames": int(track.step),
            "fps": float(track.fps), "bars": bars}


def _parent_colour(stack: Optional[hbase.BaseVersion]) -> Optional[str]:
    if stack is None:
        return None
    return ((stack.exp.get("evidence") or {}).get("omni_pattern") or {}).get("colour")


async def _icon_pattern(scene: Scene, recipe: rc.Recipe, seed: str,
                        stack: Optional[hbase.BaseVersion], keyed: Optional[dict], *,
                        spec: Optional[hbase.BaseSpec], final: bool,
                        n: Optional[int] = None) -> Optional[icon_el.IconPattern]:
    """The patterns the icons sit beside, for their place and colour: read
    from the recipe's pattern preview when it is made (its first pattern's
    colour and box, the ink of all), else a guess marked provisional — the
    plan never waits on the preview (`final`: Generate, which does)."""
    els = rc.pattern_elements(recipe)
    h8 = max(1, scene.height // speaker_track.MASK_DIV)
    w8 = max(1, scene.width // speaker_track.MASK_DIV)
    parent = None
    if stack is not None and stack.occupied is not None:
        parent = await _thread(_small, stack.occupied, w8, h8)
    if not els:
        if parent is None:
            return None
        box = compose.bbox((parent * 255).astype("uint8"))
        return icon_el.IconPattern(key=f"v{stack.version_id}", colour=_parent_colour(stack), box=box,
                                   ink=parent)
    out = scene.work / "preview" / _preview_key(recipe, seed, base=keyed,
                                                n=n if recipe.engine == "house" else None)
    body = _read(out / "preview.json")
    if body is None and final:
        try:
            await preview(scene.run_id, scene.target, recipe, base=spec,
                          **({"end_frame": n} if n is not None else {}),
                          **({"variant": scene.variant} if scene.variant else {}))
        except Exception:  # noqa: BLE001 — placed on the guess instead
            log.exception("[studio] icons: the pattern preview failed")
        body = _read(out / "preview.json")
    if body is None:
        guess = (pattern_el.colour_override(recipe.colours, els[0])
                 or (scene.colours.colour if scene.colours is not None else None) or _parent_colour(stack))
        return icon_el.IconPattern(key=out.name, colour=guess, ink=parent, provisional=True)
    first = next((p for p in body.get("patterns") or [] if p.get("colour")), {})
    frame = stack.frames[0] if stack is not None and stack.frames else scene.firsts[0]
    ink, url = await _thread(_preview_ink, out, frame, w8, h8)
    if (out / LAYERS_PNG).is_file():
        # The moving preview lays the patterns UNCUT (the preview's layers)
        # and cuts the speaker out of them at each moment, as the run does:
        # the target's ink has the first frame's speaker cut out of it, a
        # hole shaped like her that shows as soon as she moves. That ink
        # only says where the patterns are.
        url = url_for(out / LAYERS_PNG)
    if parent is not None:
        import numpy as np

        ink = parent if ink is None else np.maximum(ink, parent)
    house_body = body.get("house") if recipe.engine == "house" else None
    timed = await _thread(_timed_inks, out, house_body) if house_body else None
    if timed:
        # Patterns timed on the timeline: an icon is judged against the ones
        # on screen with it (a Blueprint transition over the first 1.5 s is
        # not under an icon at 4 s), and sits beside the one on screen the
        # longest.
        rows = {p.get("element_id"): p for p in body.get("patterns") or []}
        longest = max(timed, key=lambda r: (r["t_out"] - r["t_in"], -timed.index(r)))
        beside = rows.get(longest["element_id"]) or first
        return icon_el.IconPattern(key=out.name, colour=beside.get("colour") or first.get("colour"),
                                   box=beside.get("box") or first.get("box"), ink=ink, url=url,
                                   timed=[(r["t_in"], r["t_out"], r["ink"]) for r in timed], always=parent)
    return icon_el.IconPattern(key=out.name, colour=first.get("colour"), box=first.get("box"), ink=ink,
                               url=url)


# The timed inks of the newest previews (by preview folder: its content key).
_TIMED: dict[str, list] = {}
TIMED_KEPT = 8


def _timed_inks(out: Path, house_body: dict) -> Optional[list[dict]]:
    """A draft made in house whose patterns are timed on the timeline (any
    not showing the whole scene): each one's ink over its span at the
    speaker track's mask size (house.patterns_for_icons over the art its
    preview kept — the camera held: the ink says where a pattern is, not
    frame by frame), with its span. None when every pattern shows the whole
    scene — then the icons read the patterns as they always have.
    Synchronous; kept per preview."""
    import numpy as np
    from PIL import Image

    from . import house
    from . import plate as pl

    layers = house_body.get("layers") or []
    tms = [ly.get("timing") for ly in layers]
    if not any(tm is None or tm["t_in"] > 1e-6 or not tm["runs_to_end"] for tm in tms):
        return None
    key = str(out)
    if key in _TIMED:
        return _TIMED[key]
    specs = []
    for ly in layers:
        tm = ly.get("timing")
        art = _art_path(out, str(ly["element_id"]))
        if tm is None or not art.is_file():
            continue
        with Image.open(art) as im:
            rgba = np.asarray(im.convert("RGBA"))
        specs.append(pl.LayerSpec(
            element_id=str(ly["element_id"]), art=rgba, ink_box=tuple(int(v) for v in ly["ink_box"]),
            timing=pattern_el.Timing(**tm), margin=tuple(int(v) for v in ly.get("margin") or (0, 0)),
            follow=str(ly.get("follow") or "room"), colour=str(ly.get("colour") or ""),
            hand=np.asarray(ly["hand"], dtype=np.float64) if ly.get("hand") else None))
    rows = house.patterns_for_icons(specs, None, fps=float(house_body["fps"]), n=int(house_body["n"]),
                                    width=int(house_body["width"]), height=int(house_body["height"]),
                                    div=speaker_track.MASK_DIV)
    _TIMED[key] = rows
    while len(_TIMED) > TIMED_KEPT:
        _TIMED.pop(next(iter(_TIMED)))
    return rows


def _keep_plan(scene: Scene, p: icon_el.IconPlan) -> None:
    """The plan by its key (icons/plans/<key>.json), for the exact still and
    Generate; the newest PLANS_KEPT are kept."""
    folder = _icon_dir(scene) / "plans"
    saved = folder / f"{p.plan_key}.json"
    if not saved.exists():
        _write(saved, {"plan": p.public(), "resolved": p.resolved()})
        try:
            old = sorted(folder.glob("*.json"), key=lambda f: f.stat().st_mtime)[:-PLANS_KEPT]
            for f in old:
                f.unlink(missing_ok=True)
        except OSError:
            pass


def _sprite_png(rgba: Any) -> bytes:
    """A sprite (straight RGBA uint8) as PNG bytes — lossless, so the studio
    shows the stamped pixels exactly."""
    import io

    from PIL import Image

    buf = io.BytesIO()
    Image.fromarray(rgba, "RGBA").save(buf, format="PNG")
    return buf.getvalue()


def _write_sprites(scene: Scene, resolved: list[dict], glyphs: dict) -> list[Optional[dict]]:
    """Each shown icon's settled sprite, made by the run's own Stamper (the
    same function and cache key it stamps with), as icons/sprites/<sha16 of
    the PNG>.png — {url, w, h, ox, oy, at} per icon: its size in px, where
    its body sits inside it (the shadow's margin), and where the PNG rests
    on the frame (fractions). None for one whose glyph cannot be drawn.
    Synchronous."""
    import threading

    from . import icon_render

    folder = _icon_dir(scene) / SPRITES
    W, H = scene.width, scene.height
    out: list[Optional[dict]] = []
    made = False
    for r in resolved:
        try:
            stamps = icon_render.Stamper([r], fps=icon_el.FPS, frame_size=(W, H), glyphs=glyphs)
            sprite = stamps.sprite_of(0)
        except Exception:  # noqa: BLE001 — the stage draws it itself
            log.exception("[studio] icons: a sprite could not be drawn")
            out.append(None)
            continue
        it = stamps.items[0]
        png = _sprite_png(sprite.rgba)
        path = folder / f"{hashlib.sha256(png).hexdigest()[:16]}.png"
        if path.is_file():
            os.utime(path)
        else:
            folder.mkdir(parents=True, exist_ok=True)
            tmp = path.with_name(f"{path.stem}.{os.getpid()}.{threading.get_ident()}.tmp.png")
            tmp.write_bytes(png)
            try:
                os.replace(tmp, path)
            except OSError:
                # Two plans wrote the same sprite at once (and it may be
                # being read): theirs is this one, byte for byte.
                tmp.unlink(missing_ok=True)
                if not path.exists():
                    raise
            made = True
        h, w = sprite.rgba.shape[:2]
        x, y = it.box[0] - sprite.pad, it.box[1] - sprite.pad
        out.append({"url": url_for(path), "w": int(w), "h": int(h), "ox": int(sprite.pad), "oy": int(sprite.pad),
                    "at": {"x": round(x / W, 5), "y": round(y / H, 5), "w": round(w / W, 5), "h": round(h / H, 5)}})
    if made:
        try:
            old = sorted(folder.glob("*.png"), key=lambda f: f.stat().st_mtime)[:-SPRITES_KEPT]
            for f in old:
                f.unlink(missing_ok=True)
        except OSError:
            pass
    return out


async def _plan_sprites(scene: Scene, p: icon_el.IconPlan) -> list[Optional[dict]]:
    """_write_sprites for a plan's shown icons (a drawn one's glyph read from
    its store first; one that cannot be read gets none)."""
    from . import icon_catalog

    resolved = p.resolved()
    if not resolved:
        return []
    try:
        glyphs = await icon_catalog.glyphs_for(resolved)
    except Exception:  # noqa: BLE001 — each is tried on its own below
        glyphs = {}
        for r in resolved:
            try:
                glyphs.update(await icon_catalog.glyphs_for([r]))
            except Exception:  # noqa: BLE001 — gone from its store: no sprite
                pass
    return await _thread(_write_sprites, scene, resolved, glyphs)


def kept_plan(run_id: int, target: str, plan_key: str, *, variant: Optional[str] = None) -> Optional[dict]:
    """A plan this scene showed, by its key ({plan, resolved}), or None."""
    scene = _PREPARED.get(_pkey(run_id, target, variant))
    if scene is None or not re.fullmatch(r"[0-9a-f]{16}", plan_key or ""):
        return None
    return _read(_icon_dir(scene) / "plans" / f"{plan_key}.json")


def _icons_off() -> dict:
    return {"state": "ready", "off": True, "steps": [], "plan_key": None, "timing_key": None,
            "provisional": False, "source": None, "items": [], "ideas": [], "warnings": [],
            "timeline": None, "underlay": None}


@dataclass
class _IconBase:
    """What an icon plan is made on: the scene, the recipe as it is planned
    (added on top of a version it opens with the harness's words), the
    version built on (stack: prepared) and its part of the preview key."""
    scene: Scene
    recipe: rc.Recipe
    stack: Optional[hbase.BaseVersion] = None
    on: Optional[hbase.BaseVersion] = None
    keyed: Optional[dict] = None


async def _icon_base(run_id: int, target: str, recipe: rc.Recipe,
                     base: Optional[hbase.BaseSpec], variant: Optional[str] = None) -> _IconBase:
    scene = await _scene(run_id, target, **_opt(variant=variant))
    out = _IconBase(scene=scene, recipe=recipe)
    if base is not None and base.mode == "stack":
        out.stack = out.on = await prepare_base(run_id, target, base.version_id, **_opt(variant=variant))
        out.recipe = on_top(recipe)
        out.keyed = {"version_id": out.stack.version_id, "mode": "stack"}
    elif base is not None and not base.fresh:
        out.on = await load_base(run_id, target, base)
        out.keyed = {"version_id": out.on.version_id, "mode": "rebuild",
                     "director": recipe.director.model_dump(mode="json")}
    return out


def _lineage(on: Optional[hbase.BaseVersion]) -> dict:
    """What the studio is told about the version it builds on, for icons:
    whether it shows Omni's own (then the Icons tab offers Change it — redo
    without them — or keeping them). Only with a base: a fresh draft's
    bodies stay as they were."""
    return {"parent_has_omni_icons": hbase.omni_icons_in(on.chain)} if on is not None else {}


async def _plan_ctx(ib: _IconBase, jobs: dict[str, _IconJob], base: Optional[hbase.BaseSpec], *,
                    final: bool, n: Optional[int] = None) -> icon_el.IconCtx:
    """The plan's inputs, once every job is done (or failed)."""
    scene, recipe, stack = ib.scene, ib.recipe, ib.stack
    words = await _words_of(scene, jobs["words"])
    track, bg = await _track_of(jobs["spots"])
    director = await _director_of(jobs["director"], words)
    seed = recipe.seed or default_seed(scene.run_id, scene.target)
    pattern = await _icon_pattern(scene, recipe, seed, stack, ib.keyed, spec=base, final=final, n=n)
    if stack is not None and track is not None:
        # Added on top of a version: judged against that version's pixels.
        clips = [(src, c["start"]) for src, c in zip(stack.sources, _chunks(scene))]
        try:
            bg = await _thread(icon_el.backgrounds, clips, track,
                               scene.work / f"on_v{stack.version_id}" / "icons_bg.npz")
        except Exception:  # noqa: BLE001
            log.exception("[studio] icons: reading the version's colours failed")
    # Cut short by a user end (n): min(its seconds, n/30) — only then, so
    # every other plan keeps its timing key.
    duration = _plan_duration(scene, n) or float(words.get("duration") or 0.0)
    from . import icon_catalog

    team = icon_catalog.team_known()
    return icon_el.IconCtx(
        words=words, director=director, track=track, width=scene.width, height=scene.height,
        duration=duration, fps=icon_el.FPS,
        chunks=_chunks(scene, n), enters=_icon_enters(recipe, stack), zones=_icon_zones(recipe, stack, duration),
        seed=seed, face=recipe.guards.face, pattern=pattern, backgrounds=bg, base=ib.keyed,
        notes=[] if track is not None else [icon_el.W_NO_TRACK], terms=icon_terms(team), team=team,
    )


def icon_terms(team: list[dict]) -> Callable[[str], list[str]]:
    """The words an icon is called by, for where an added one lands: a
    library icon's (icon_el.catalog_terms), or a team icon's name, label and
    tags as the team's icons were last read (icon_catalog.team_known — no
    database read here; one the team has hidden has its own label only)."""
    from . import icon_catalog

    rows = {str(r.get("id")): r for r in team if r.get("id")}

    def terms(gid: str) -> list[str]:
        row = rows.get(gid)
        if row is None:
            return [] if icon_catalog.is_custom_id(gid) else icon_el.catalog_terms(gid)
        return [t for t in (row.get("name"), row.get("label"), *(row.get("tags") or [])) if t]

    return terms


async def icon_ctx(run_id: int, target: str, recipe: rc.Recipe,
                   base: Optional[hbase.BaseSpec] = None, *, end_frame: Optional[int] = None,
                   variant: Optional[str] = None) -> icon_el.IconCtx:
    """The plan's inputs for a run whose recipe came without a frozen plan
    (runner.icons_for — an API client): Generate's own (final_icon_plan),
    every input and the pattern preview waited for."""
    recipe = icon_el.strip_resolved(recipe)
    el = rc.icon_element(recipe)
    if el is None:
        raise ValueError("the recipe has no icon element")
    ib = await _icon_base(run_id, target, recipe, base, variant)
    jobs = _icon_jobs(ib.scene, take=_take(ib.scene), ai=rc.icon_props(el).ai)
    await asyncio.gather(*(asyncio.shield(j.task) for j in jobs.values()), return_exceptions=True)
    return await _plan_ctx(ib, jobs, base, final=True, n=await _out_n(ib.scene, end_frame))


KEEP_GONE = "That version's icons can't be kept any more, so they are placed afresh."


async def _kept_icons(ib: _IconBase, el: rc.Element, props: rc.IconProps, *,
                      final: bool, n: Optional[int] = None) -> Optional[tuple[dict, Optional[icon_el.IconPlan]]]:
    """"Keep v3's icons" (props.keep): that version's icons exactly as it
    froze them (icon_el.kept_plan) — read from its row, never planned: no
    director is asked, nothing is placed. Only a finished version of this
    clip with icons of ours; None otherwise (the icons are then planned as
    ever, and the plan says why). The lane gets the speech: the words are
    the one input this waits for ("warming" until they are in; at Generate,
    waited for)."""
    from .. import rebrand_store as store

    scene = ib.scene
    try:
        exp = await store.get_experiment(int(props.keep or 0))
    except Exception:  # noqa: BLE001 — unreadable now: planned as ever
        log.exception("[studio] icons: version #%s could not be read to keep its icons", props.keep)
        exp = None
    if (not exp or int(exp.get("run_id") or 0) != int(scene.run_id) or hbase.target_of(exp) != scene.target
            or not hbase.is_ready(exp)):
        return None
    resolved = hbase.icons_of(exp)
    if not resolved:
        return None
    words_job = _icon_job(f"{scene.work}|words", lambda: _icon_words(scene))
    if final and not words_job.task.done():
        await asyncio.gather(asyncio.shield(words_job.task), return_exceptions=True)
    elif not words_job.task.done():
        # The speech for the lane (local, cached after the first time): a
        # moment — asked again, as while any plan warms.
        steps = [s for s in _icon_steps({"words": words_job}) if s["name"] == "words"]
        return ({**_icons_off(), "off": False, "state": "warming", "provisional": True, "steps": steps,
                 "underlay": _underlay(scene, ib.stack), **_lineage(ib.on)}, None)
    words = words_job.task.result() if words_job.ok else None
    entrance = el.animation.entrance if el.animation.entrance in rc.ICON_ENTRANCES else "auto"
    duration = _plan_duration(scene, n)
    p = await _thread(icon_el.kept_plan, resolved, version_id=int(exp["id"]), width=scene.width,
                      height=scene.height, words=words, duration=duration,
                      chunks=_chunks(scene, n), enters=_icon_enters(ib.recipe, ib.stack), style=props.style,
                      entrance="glitch" if entrance == "auto" else entrance,
                      zones=_icon_zones(ib.recipe, ib.stack, duration))
    await _thread(_keep_plan, scene, p)
    body = {"state": "ready", "off": False, "steps": [], **p.public(),
            "underlay": {**_underlay(scene, ib.stack), "pattern_url": None, "masks": None},
            **_lineage(ib.on), "kept": {"version_id": int(exp["id"])}}
    try:
        sprites = await _plan_sprites(scene, p)
    except Exception:  # noqa: BLE001 — the stage then draws them itself
        log.exception("[studio] icons: writing the kept icons' sprites failed")
        sprites = []
    for item, sprite in zip(body["items"], sprites):
        item["sprite"] = sprite
    return body, p


async def _icon_plan(run_id: int, target: str, recipe: rc.Recipe, base: Optional[hbase.BaseSpec], *,
                     ask_again: bool, reorder: Optional[dict], final: bool, end_frame: Optional[int] = None,
                     variant: Optional[str] = None
                     ) -> tuple[dict, Optional[icon_el.IconPlan]]:
    recipe = icon_el.strip_resolved(recipe)
    el = rc.icon_element(recipe)
    if el is None:
        return _icons_off(), None
    props = rc.icon_props(el)
    ib = await _icon_base(run_id, target, recipe, base, variant)
    scene, recipe, stack = ib.scene, ib.recipe, ib.stack
    n = await _out_n(scene, end_frame)
    if props.keep is not None:
        kept = await _kept_icons(ib, el, props, final=final, n=n)
        if kept is not None:
            return kept
    if ask_again and scene.kind == "scene" and _may_ask_again(scene, props.ai):
        _ask_again(scene)
    jobs = _icon_jobs(scene, take=_take(scene), ai=props.ai)
    underlay = _underlay(scene, stack)
    if final:
        await asyncio.gather(*(asyncio.shield(j.task) for j in jobs.values()), return_exceptions=True)
    elif not all(j.task.done() for j in jobs.values()):
        body = {**_icons_off(), "off": False, "state": "warming", "provisional": True,
                "steps": _icon_steps(jobs), "underlay": underlay, **_lineage(ib.on)}
        if jobs["words"].ok:
            words = jobs["words"].task.result()
            duration = _plan_duration(scene, n)
            body["timeline"] = icon_el.timeline_of(words, duration=duration, chunks=_chunks(scene, n),
                                                   enters=_icon_enters(recipe, stack),
                                                   zones=_icon_zones(recipe, stack, duration))
            body["words_digest"] = words.get("digest")
        return body, None
    ctx = await _plan_ctx(ib, jobs, base, final=final, n=n)
    moved = None
    if reorder:
        moved = await _thread(icon_el.reorder, el, ctx, str(reorder.get("glyph") or ""),
                              int(reorder.get("to") or 0))
    p = await _thread(icon_el.plan, el, ctx)
    await _thread(_keep_plan, scene, p)
    try:
        masks = await _thread(_track_masks, scene, ctx.track) if ctx.track is not None else None
    except Exception:  # noqa: BLE001 — the moving preview goes without
        log.exception("[studio] icons: writing the speaker's masks failed")
        masks = None
    body = {"state": "ready", "off": False, "steps": _icon_steps(jobs), **p.public(),
            "underlay": {**underlay, "pattern_url": p.pattern_url, "masks": masks}, **_lineage(ib.on)}
    if scene.kind != "scene":
        # Nothing is said on an intro or outro: no word timing to be off.
        body["warnings"] = [w for w in body["warnings"] if w != icon_el.W_ESTIMATE]
    if props.keep is not None:
        # "Keep v3's icons" asked of a version that can't give them.
        body["warnings"] = [KEEP_GONE, *body["warnings"]]
    try:
        sprites = await _plan_sprites(scene, p)
    except Exception:  # noqa: BLE001 — the stage then draws them itself
        log.exception("[studio] icons: writing the sprites failed")
        sprites = []
    for item, sprite in zip(body["items"], sprites):
        item["sprite"] = sprite
    if moved is not None:
        body["reorder"] = moved
    return body, p


async def icon_plan(run_id: int, target: str, recipe: rc.Recipe, base: Optional[hbase.BaseSpec] = None, *,
                    ask_again: bool = False, reorder: Optional[dict] = None,
                    end_frame: Optional[int] = None, variant: Optional[str] = None) -> dict:
    """POST /icons/plan: the recipe's icon plan on this scene (module doc),
    or {state: "warming", steps, timeline once the words are in} while its
    inputs are still being worked out — the client asks again each second.
    Free. `ask_again` bumps the director's take ("Ask the AI again": one new
    call) — only once this take has answered without the AI, so repeated
    clicks while it asks cost nothing (_may_ask_again); `reorder` {glyph,
    to} adds `reorder` to the body — the pins that
    show that icon at that place in the order (elements/icon.reorder)."""
    body, _plan = await _icon_plan(run_id, target, recipe, base, ask_again=ask_again, reorder=reorder,
                                   final=False, end_frame=end_frame, variant=variant)
    return body


async def final_icon_plan(run_id: int, target: str, recipe: rc.Recipe,
                          base: Optional[hbase.BaseSpec] = None, *,
                          end_frame: Optional[int] = None,
                          variant: Optional[str] = None) -> tuple[dict, Optional[icon_el.IconPlan]]:
    """The plan Generate freezes (materialise): the same plan, waiting for
    every input and for the pattern preview, so its place and colour are
    final. (body, IconPlan) — (body, None) without an icon element."""
    return await _icon_plan(run_id, target, recipe, base, ask_again=False, reorder=None, final=True,
                            end_frame=end_frame, variant=variant)


# ---------------------------------------------------------------------------
# Icons: the exact still
# ---------------------------------------------------------------------------

def _moment_layer(out: Path) -> Any:
    """A preview's patterns as one RGBA layer (uncut: LAYERS_PNG; else the
    one the icon plan cut out of the target, icons_pattern.png) — or None."""
    import numpy as np
    from PIL import Image

    for name in (LAYERS_PNG, "icons_pattern.png"):
        p = out / name
        if p.is_file():
            with Image.open(p) as im:
                return np.asarray(im.convert("RGBA"))
    return None


def _write_png(path: Path, frame: Any) -> Path:
    import cv2

    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = _tmp_of(path, ".tmp.png")
    if not cv2.imwrite(str(tmp), frame):
        raise RuntimeError(f"could not write {path.name}")
    _made(tmp, path)
    return path


async def icon_moment(run_id: int, target: str, recipe: rc.Recipe, plan_key: str, t: float,
                      version_id: Optional[int] = None, *, variant: Optional[str] = None) -> dict:
    """POST /icons/moment: the frame at `t` (scene seconds) with the plan's
    icons, made by the run's own code — speaker_front.frame_at: the same
    compose_frame, the same matte keyframes, every frame decoded in order.

      on the scene     the scene's frame, the recipe's patterns laid over it
                       (uncut — the speaker goes back over them, as Omni's
                       would be cut), the icons, the speaker: what the run
                       will make, but for Omni's own redraw of the room;
      on a version     (`version_id`, added on top of it) its pre-stamp part
                       and its own pass (base.Replay) with these icons — the
                       very frame an icon-only pass on it writes. With new
                       patterns too, its clean part with them laid over.

    Cached per plan, place and frame. {url, frame, t}: the still, its frame
    number in the scene and the time of that frame."""
    from .. import speaker_front
    from . import icon_catalog
    from . import icon_render

    scene = await _scene(run_id, target, **_opt(variant=variant))
    kept = kept_plan(run_id, target, plan_key, variant=variant)
    if kept is None:
        raise HTTPException(404, "That icon plan isn't here any more — look at the icons again.")
    spans = icon_el.chunk_spans(scene.seconds)
    if not spans or not scene.chunks:
        raise HTTPException(404, "This scene has no parts.")
    at = max(0.0, float(t))
    j = max(k for k, (s, _sec) in enumerate(spans) if s <= at + 1e-9)
    j = min(j, len(scene.chunks) - 1)
    start, sec = spans[j]
    local = max(0.0, min(at - start, float(sec)))
    on = [r for r in kept.get("resolved") or [] if icon_el.overlaps(r, start, sec)]
    # One whose glyph can't be drawn (a library icon a re-bake dropped, a
    # drawing gone from its store) is left out, as the run leaves it out.
    on, _lost = await runner._loadable(on)
    stamps = None
    if on:
        glyphs = await icon_catalog.glyphs_for(on)
        stamps = icon_render.Stamper(on, fps=icon_el.FPS, offset_sec=start, frame_size=(scene.width, scene.height),
                                     glyphs=glyphs)
    recipe = icon_el.strip_resolved(recipe)
    seed = recipe.seed or default_seed(run_id, target)
    original = scene.chunks[j]
    if version_id is None:
        out = scene.work / "preview" / _preview_key(recipe, seed)
        part = ((_read(out / "preview.json") or {}).get("chunks") or [])
        guards = (part[j].get("guards") if j < len(part) else None) or {}
        clear = bool(guards["clear_bars"]) if "clear_bars" in guards else gd.clears_bars(recipe.guards, [])
        edited, src = original, "scene"
        kw: dict[str, Any] = {"clear_bars": clear, "layer": await _thread(_moment_layer, out)}
    else:
        bv = await prepare_base(run_id, target, int(version_id), **_opt(variant=variant))
        src = f"v{bv.version_id}"
        stacked = on_top(recipe)
        if rc.pattern_elements(stacked):
            out = scene.work / "preview" / _preview_key(stacked, seed,
                                                        base={"version_id": bv.version_id, "mode": "stack"})
            edited = bv.sources[j]
            kw = {"clear_bars": True, "layer": await _thread(_moment_layer, out)}
        else:
            rp = (bv.replays or [hbase.replay_of_source(s) for s in bv.sources])[j]
            edited, kw = rp.edited, rp.kwargs()
    # The part's own jump cuts, as the run's pass is given them: the matte
    # never unions across one (a still made before this knew of them is
    # another file).
    cuts = await _thread(media.cuts_of, original)
    if cuts:
        kw["cuts"] = cuts
    fps = await _thread(speaker_front._probe_fps, edited)
    guess = int(round(local * fps))
    png = _icon_dir(scene) / "moments" / f"{plan_key}_{src}_c{j}_{guess}{'_k' if cuts else ''}.png"
    meta = png.with_suffix(".json")
    found = _read(meta) if png.is_file() else None
    if found is None:
        frame, i, fps = await _thread(speaker_front.frame_at, edited, original, local, stamps=stamps, **kw)
        await _thread(_write_png, png, frame)
        found = {"frame": int(round((start + i / fps) * fps)), "t": round(start + i / fps, 3)}
        _write(meta, found)
        try:
            old = sorted(png.parent.glob("*.png"), key=lambda f: f.stat().st_mtime)[:-MOMENTS_KEPT]
            for f in old:
                f.unlink(missing_ok=True)
                f.with_suffix(".json").unlink(missing_ok=True)
        except OSError:
            pass
    return {"url": url_for(png), "frame": found["frame"], "t": found["t"]}
