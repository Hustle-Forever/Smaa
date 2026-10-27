"""The in-house engine: a scene made by laying our own graphics over its
untouched footage — for $0, with no Omni (the owner, 2026-09-29: "no need
Omni").

  plate_spec(recipe, layers, …)   the scene's plate spec (plate.PlateSpec):
                                  each pattern as the plate draws it and our
                                  icons (one Stamper for the whole scene)
  render_key(spec, …)             everything a render is made of, as a key
  render(spec, source, out, …)    the scene with its plate on it: ffmpeg lays
                                  each frame's plate (plate.plate_at) over
                                  the SOURCE and encodes once, the source's
                                  sound copied as it is
  design_still(spec, source, …)   a still: frame 0 with the plate at rest
                                  (the patterns showing at t_s fully in)
  patterns_for_icons(layers, …)   where the patterns' ink goes over their
                                  spans (the camera moving them), for the
                                  icons' keep-out zones
  edit_scene(ctx, …)              the run's branch for engine "house"
  version_still(exp, out)         a house version's still made again
                                  (studio.remake makes its video again)
  flatten(bv, job, mask=)         added on top of a house pass (a version
                                  made in house over one Omni made): its
                                  passes' own patterns (kept_layers — as
                                  they froze them, read from their rows)
                                  drawn again over the video Omni made, under
                                  the new ones — Omni's video stays under
                                  everything, and an unchanged draft is that
                                  version's very spec

The render (house.render) in one ffmpeg pass: the source (or, added on top
of a version Omni made, that version's own clip, the clean one) is the main
input, the plates arrive as raw RGBA frames on a pipe, and

    overlay=format=yuv444:alpha=straight   the plate over the footage in
                                           YUV — the footage's pixels are
                                           never turned into RGB and back
                                           (that darkened today's Omni
                                           restores 1.8 levels)
    scale=out_color_matrix=bt709           the plate's colours made with the
                                           matrix browsers show HD with, so
                                           42 violet comes out exact; the
                                           output is tagged bt709 (metadata:
                                           the footage's own values are kept)
    -c:a copy, no -shortest                the source's sound, byte for byte,
                                           and every frame
    -t n/fps                               only when the scene is cut short
                                           (a user end: spec.n under the
                                           clip's own frames) — its sound
                                           stops with its picture; otherwise
                                           the command is what it always was

A straight-alpha plate is what this ffmpeg blends right once the plate's
colours are converted with an explicit matrix (measured on a flat grey: the
straight blend lands within 1 level of the sum at every alpha, the
premultiplied one ~18 levels dark at full alpha), so plate_at's
premultiplied frame is handed over as straight (plate.straight).

One pass per scene: no chunks, no fit, no concat — nothing for the sound to
drift on. The slow part of a scene (its matte bank and camera track) is made
once per clip and shared with the studio (studio.analysis_dir); after that a
render takes seconds. Renders and analyses take one gate
(studio._gate, REBRAND_HOUSE_CONCURRENCY): this PC is short of memory.

$0 by construction: no Omni, no brief, no critique, no memory embed, and the
local matte only — nothing here can reach a paid model.
"""
from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import logging
import os
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Callable, NamedTuple, Optional, Sequence

import numpy as np

from .. import rebrand_media as media
from .. import rebrand_pattern_ref as pattern_ref
from .. import rebrand_store as store
from . import base as hbase
from . import camera as camera_mod
from . import mattes
from . import plate as pl
from . import recipe as rc
from . import runner
from . import stages as st
from .elements import pattern as pattern_el

log = logging.getLogger("archivist.rebrand.harness.house")

HOUSE_VERSION = pl.HOUSE_VERSION
# What a house version's arm says drew it (arms.omni: the key every reader
# of a version reads; renaming it is later cleanup).
PROVIDER = "house"
HOUSE_MODEL = f"house-v{HOUSE_VERSION}"
# "auto" follow: a layer with at least this share of its ink on the
# letterbox bars holds still on screen (the bars are padding, not the room).
FOLLOW_SCREEN_SHARE = 0.6
RENDER_CRF = 18
RENDER_PRESET = "veryfast"
RENDER_TIMEOUT_SEC = 900
COLOUR_TAGS = "colorspace=bt709:color_primaries=bt709:color_trc=bt709:range=tv"
# A version's still shows its first icon this long after it is fully in.
STILL_ICON_AFTER = hbase.ICON_STILL_AFTER
Cancelled = mattes.Cancelled


async def _thread(fn, *args, **kwargs):
    return await asyncio.to_thread(fn, *args, **kwargs)


# ---------------------------------------------------------------------------
# The spec: each pattern as the plate draws it
# ---------------------------------------------------------------------------

# A hand move as a 2x3 matrix on frame pixels (turned and sized about its
# pivot, then moved; None for none) — the renderer's own.
hand_matrix = pattern_ref.hand_matrix


def slide_edge(layout: pattern_ref.Layout) -> str:
    """The edge a pattern slides in from: the edge it grows from — a strip's
    own side, a band's corner (as its sweep is aimed:
    pattern_library.aimed_animation) — and the left for the whole frame."""
    if layout.placement in ("left", "right"):
        return layout.placement
    if layout.placement in ("top", "bottom"):
        return "right" if layout.anchor == "right" else "left"
    return "left"


def resolve_follow(el: rc.Element, alpha: Any, bars: Any, *, person: Optional[bool] = None) -> str:
    """Whether a pattern moves with the camera ("room") or holds still on
    screen ("screen"): its own follow (frozen, or set), else "screen" on an
    intro or outro with no one in it (`person` False: there is no room the
    camera moves through, only a card), else "screen" when most of its ink
    lies on the letterbox bars, else "room"."""
    frozen = pattern_el.frozen(el) or {}
    own = frozen.get("follow") or el.props.get("follow")
    if own in ("room", "screen"):
        return own
    if person is False:
        return "screen"
    if bars is None:
        return "room"
    ink = np.asarray(alpha) > 0
    total = int(ink.sum())
    if not total:
        return "room"
    return "screen" if int((ink & np.asarray(bars, dtype=bool)).sum()) / total >= FOLLOW_SCREEN_SHARE else "room"


def layer_spec(el: rc.Element, ref: pattern_ref.PatternRef, *, width: int, height: int, duration: float,
               seed: str, bars: Any = None, person: Optional[bool] = None) -> Optional[pl.LayerSpec]:
    """One pattern as the plate draws it: its layer in its place (rebrand_
    pattern_ref.windowed — with no hand move, _layer itself: the very pixels
    the studio's preview lays in), its span on this scene, whether it
    follows the camera, its hand move. Moved by hand (ref.hand), the art is
    the UNMOVED place drawn past the frame as far as the move and
    HAND_MARGIN need (its own art carries on where the frame used to cut
    it), and the move is its matrix: the plate warps it once, camera x hand
    (a full-screen composition's size clamped so it always fills the
    frame). None when it has no ink at this size. Synchronous."""
    moved = not ref.hand.identity
    got = pattern_ref.windowed(ref, width, height, margin=pattern_ref.HAND_MARGIN if moved else 0.0)
    if got is None:
        return None
    rgba = got.art
    box = pl.ink_box(rgba[..., 3])
    if box is None:
        return None
    if moved:
        placed = pattern_ref.placed_layer(ref, width, height)
        seen = (np.asarray(placed.getchannel("A")) if placed is not None
                else np.zeros((height, width), dtype=np.uint8))
    else:
        seen = rgba[..., 3]
    return pl.LayerSpec(
        element_id=el.id, art=pl.premultiplied(rgba), ink_box=box,
        timing=pattern_el.timing(el, duration=duration, entrance=ref.animation),
        margin=(int(got.origin[0]), int(got.origin[1])),
        follow=resolve_follow(el, seen, bars, person=person), z=el.z or "behind", seed=f"{seed}:{el.id}:fx",
        hand=got.matrix, edge=slide_edge(ref.layout), colour=ref.colour,
    )


def plate_spec(recipe: rc.Recipe, layers: Sequence[tuple], *,
               width: int, height: int, fps: float, n: int, bars: Any = None, stamper: Any = None,
               icons: Sequence[dict] = (), person: Optional[bool] = None) -> pl.PlateSpec:
    """THE shared resolver: the recipe's patterns (each with the place it
    was built in, in draw order: (element, ref), or a Kept layer of a house
    pass under them, drawn with its own pass's seed) and our icons as one
    plate spec for a scene of n frames at fps. `person` (person_of): an
    intro or outro with no one in it (False) cuts nothing out and holds its
    patterns still on screen; None (every scene) changes nothing.
    Synchronous."""
    duration = n / float(fps)
    seed = recipe.seed or ""
    specs = []
    for item in layers:
        el, ref = item[0], item[1]
        own = item[2] if len(item) > 2 else seed
        got = layer_spec(el, ref, width=width, height=height, duration=duration, seed=own, bars=bars,
                         person=person)
        if got is not None:
            specs.append(got)
    return pl.PlateSpec(width=int(width), height=int(height), fps=float(fps), n=int(n), layers=tuple(specs),
                        stamper=stamper, icons=tuple(icons), bars=bars,
                        speaker_front=bool(recipe.guards.speaker_front) and person is not False)


def person_of(kind: str, bank: Any, src: Optional[str] = None) -> Optional[bool]:
    """For an intro or outro, whether anyone is in it — which plate_spec's
    `person` takes; None for a scene (a talking head has its speaker:
    nothing changes). The reel's own bookend (its Veo card animation, its
    card, a reference asset: `src` anim: / card: / asset:) has no one: MODNet
    reads ghosts on their shapes and text (mattes.PERSON_MIN, U6), which
    would cut holes in them. Only an uploaded video (upload:) is judged by
    its matte bank, filled (MatteBank.has_person). The studio and the run
    ask the same way, so they resolve alike."""
    if kind not in ("intro", "outro"):
        return None
    if not str(src or "").startswith("upload:"):
        return False
    return bool(bank.has_person())


def _digest(a: Any) -> Optional[str]:
    if a is None:
        return None
    arr = np.ascontiguousarray(a)
    return hashlib.sha256(repr((arr.shape, str(arr.dtype))).encode("ascii") + arr.tobytes()).hexdigest()[:20]


def render_key(spec: pl.PlateSpec, *, source: Path, under: Optional[Path] = None,
               camera_key: Optional[str] = None, bank_key: Optional[str] = None) -> Optional[str]:
    """Everything a render is made of: each layer (its pixels, place, span,
    motion seed, follow, hand), the icons as frozen (and the glyph
    catalogues they are drawn from), the bars, the clip (and the version
    under it), the scene's camera and matte bank, the renderer and its
    encode. None when a file is missing. Synchronous (it hashes files)."""
    from . import icon_catalog

    layers = [{"id": ly.element_id, "art": _digest(ly.art), "margin": list(ly.margin), "box": list(ly.ink_box),
               "follow": ly.follow, "z": ly.z, "timing": ly.timing.public() if ly.timing is not None else None,
               "seed": ly.seed, "edge": ly.edge,
               "hand": np.round(np.asarray(ly.hand, dtype=np.float64), 6).tolist() if ly.hand is not None else None}
              for ly in spec.layers]
    glyphs = [icon_catalog.catalogue_version(), icon_catalog.bold_version()] if spec.icons else None
    return st.key(v=HOUSE_VERSION, size=[spec.width, spec.height], fps=round(spec.fps, 6), n=spec.n,
                  layers=layers, icons=list(spec.icons), glyphs=glyphs, bars=_digest(spec.bars),
                  speaker=spec.speaker_front, source=Path(source), under=Path(under) if under else None,
                  camera=camera_key, bank=bank_key, encode=[RENDER_CRF, RENDER_PRESET, COLOUR_TAGS])


def render(spec: pl.PlateSpec, source: Path, out: Path, *, camera: Any = None, bank: Any = None,
           under: Optional[Path] = None, progress: Optional[Callable[[int, int], None]] = None,
           cancel: Optional[Callable[[], bool]] = None,
           on_plate: Optional[Callable[[int, Any], None]] = None) -> Path:
    """The scene with its plate on it (the module doc), written whole to
    `out` or not at all. `under`: the video the plate goes over instead of
    the source (the sound is still the source's). `progress(done, total)`
    per frame; `cancel()` returning True stops it (Cancelled); `on_plate(i,
    plate)` sees each premultiplied plate as it goes (tests). Blocking: call
    it from a thread."""
    W, H, n = int(spec.width), int(spec.height), int(spec.n)
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_name(f"{out.stem}.{os.getpid()}.{threading.get_ident()}.part{out.suffix}")
    cmd = [media.ffmpeg_exe(), "-y", "-loglevel", "error", "-i", str(under or source)]
    audio = 0
    if under is not None:
        cmd += ["-i", str(source)]
        audio = 1
    plates = audio + 1
    cmd += ["-f", "rawvideo", "-pix_fmt", "rgba", "-s", f"{W}x{H}", "-r", f"{spec.fps:.6f}", "-i", "-",
            "-filter_complex",
            f"[0:v]setparams={COLOUR_TAGS}[m];"
            f"[{plates}:v]scale=out_color_matrix=bt709:out_range=tv,format=yuva444p[p];"
            "[m][p]overlay=format=yuv444:alpha=straight:eof_action=pass,format=yuv420p[v]",
            "-map", "[v]", "-map", f"{audio}:a?", "-frames:v", str(n)]
    if bank is not None and n < int(bank.n):
        # Cut short by a user end: the copied sound stops with the picture
        # (to within one AAC frame).
        cmd += ["-t", f"{n / float(spec.fps):.6f}"]
    cmd += ["-c:v", "libx264", "-preset", RENDER_PRESET, "-crf", str(RENDER_CRF),
            "-c:a", "copy", "-movflags", "+faststart", str(tmp)]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=subprocess.PIPE)
    errors: list[bytes] = []
    drain = threading.Thread(target=lambda: errors.append(proc.stderr.read()), daemon=True)
    drain.start()
    try:
        for i in range(n):
            if cancel is not None and cancel():
                raise Cancelled()
            frame = pl.plate_at(spec, i, camera=camera, bank=bank)
            if on_plate is not None:
                on_plate(i, frame)
            proc.stdin.write(pl.straight(frame).tobytes())
            if progress is not None:
                progress(i + 1, n)
        proc.stdin.close()
        code = proc.wait(timeout=RENDER_TIMEOUT_SEC)
    except BaseException:
        proc.kill()
        proc.wait()
        tmp.unlink(missing_ok=True)
        raise
    drain.join(timeout=5)
    if code != 0 or not tmp.is_file():
        tmp.unlink(missing_ok=True)
        raise RuntimeError(f"the in-house render failed: {b''.join(errors).decode(errors='replace')[-300:]}")
    os.replace(tmp, out)
    return out


# ---------------------------------------------------------------------------
# Stills, and where the patterns go
# ---------------------------------------------------------------------------

def still_moment(spec: pl.PlateSpec) -> float:
    """The design still's default moment: the start of the longest stretch
    in which the most patterns are fully in (0 when none shows)."""
    spans = [(ly.timing.t_full, ly.timing.t_hold_end if ly.timing.t_hold_end > ly.timing.t_full
              else ly.timing.t_out) for ly in spec.layers if ly.timing is not None]
    if not spans:
        return 0.0
    cuts = sorted({v for s in spans for v in s})
    best, best_n, best_len = spans[0][0], -1, -1.0
    for a, b in zip(cuts, cuts[1:]):
        count = sum(1 for s, e in spans if s <= a and b <= e)
        if count > best_n or (count == best_n and b - a > best_len):
            best, best_n, best_len = a, count, b - a
    return float(best)


def version_still_at(spec: pl.PlateSpec) -> float:
    """Where a version's still is taken: its first icon fully in (and a
    moment more, as its exact-frame moment), else still_moment."""
    settles = sorted(float(r["settle_sec"]) for r in spec.icons if float(r["to_sec"]) > float(r["settle_sec"]))
    if settles:
        first = next(r for r in spec.icons if float(r["settle_sec"]) == settles[0])
        return round(min(settles[0] + STILL_ICON_AFTER, float(first["to_sec"]) - 1.0 / spec.fps), 3)
    return still_moment(spec)


def _first_frame(clip: Path) -> Any:
    import cv2

    cap = cv2.VideoCapture(str(clip))
    try:
        ok, frame = cap.read()
    finally:
        cap.release()
    if not ok:
        raise RuntimeError(f"no frames in {Path(clip).name}")
    return frame


def design_still(spec: pl.PlateSpec, source: Path, *, camera: Any = None, bank: Any = None,
                 t_s: Optional[float] = None) -> Any:
    """The still (BGR uint8): the clip's frame 0 — the speaker always from
    frame 0 — with the plate at rest over it: the patterns that show at t_s
    (still_moment by default) fully in, the icons on screen then settled.
    Synchronous."""
    at = still_moment(spec) if t_s is None else float(t_s)
    frame = _first_frame(Path(source))
    return pl.over_frame(frame, pl.plate_at(spec, 0, camera=camera, bank=bank, settled=True, at=at))


def write_still(spec: pl.PlateSpec, source: Path, out: Path, *, camera: Any = None, bank: Any = None,
                t_s: Optional[float] = None) -> Path:
    """design_still as a PNG, written whole."""
    import cv2

    img = design_still(spec, source, camera=camera, bank=bank, t_s=t_s)
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_name(f"{out.stem}.{os.getpid()}.tmp.png")
    if not cv2.imwrite(str(tmp), img):
        raise RuntimeError(f"could not write {out.name}")
    os.replace(tmp, out)
    return out


def patterns_for_icons(layers: Sequence[pl.LayerSpec], camera: Any = None, *, fps: float, n: int,
                       width: int, height: int, div: int = 8, samples: int = 5) -> list[dict]:
    """Where each pattern's ink goes while it shows, for the icons: {element_id,
    colour, box (frame fractions), ink (float32 H/div x W/div, 0..1: the
    union over its span, at `samples` moments, moved by the camera),
    t_in, t_full, t_hold_end, t_out, entrance, exit, follow} per layer that
    shows — its time is what the icons keep clear of (elements/icon.
    pattern_zones: no settle while it comes in or goes out). Synchronous."""
    import cv2

    out = []
    w8, h8 = max(1, width // div), max(1, height // div)
    for ly in layers:
        tm = ly.timing
        if tm is None:
            continue
        a, b = int(round(tm.t_in * fps)), max(int(round(tm.t_in * fps)), int(round(tm.t_out * fps)) - 1)
        frames = sorted({min(max(0, int(round(a + (b - a) * k / max(1, samples - 1)))), max(0, n - 1))
                         for k in range(samples)})
        alpha = np.ascontiguousarray(ly.art[..., 3])
        union = np.zeros((height, width), dtype=np.float32)
        for f in frames:
            cam = pl._m3(camera.at(f)) if (camera is not None and ly.follow == "room") else np.eye(3)
            m = cam @ pl._m3(ly.hand) @ pl._shift(-ly.margin[0], -ly.margin[1])
            warped = cv2.warpAffine(alpha, m[:2], (width, height), flags=cv2.INTER_LINEAR,
                                    borderMode=cv2.BORDER_CONSTANT, borderValue=0)
            union = np.maximum(union, warped.astype(np.float32) / 255.0)
        small = cv2.resize(union, (w8, h8), interpolation=cv2.INTER_AREA)
        box = pl.ink_box(union > 0.02)
        out.append({"element_id": ly.element_id, "colour": ly.colour,
                    "box": ({"x": round(box[0] / width, 4), "y": round(box[1] / height, 4),
                             "w": round((box[2] - box[0]) / width, 4), "h": round((box[3] - box[1]) / height, 4)}
                            if box is not None else None),
                    "ink": small, "t_in": tm.t_in, "t_full": tm.t_full, "t_hold_end": tm.t_hold_end,
                    "t_out": tm.t_out, "entrance": tm.entrance, "exit": tm.exit, "follow": ly.follow})
    return out


async def version_still(exp: dict, out: Path) -> Path:
    """A version made in house, its still made again (base.still, when the
    one its run wrote is gone): the design still of its own stored recipe,
    resolved as its run resolved it (studio.house_spec: the frozen patterns
    and icons, the scene's matte bank and camera) — the very picture the run
    wrote: frame 0's speaker, the plate at rest at its first icon. Raises
    when it can't be (added on top of a version Omni made — its still is
    that version's footage —, a pattern that was not frozen, the scene gone):
    base.still then takes one from its video."""
    from . import studio

    opts = exp.get("options") or {}
    recipe = rc.normalize(opts["recipe"])
    base = hbase.BaseSpec.of(opts.get("base"))
    if base is not None and base.mode == "stack":
        raise ValueError("added on top of a version: its still is taken from its video")
    hs = await studio.house_spec(int(exp["run_id"]), str(opts.get("target") or ""), recipe, base)
    return await _thread(write_still, hs.spec, hs.clip, out, camera=hs.track, bank=hs.bank,
                         t_s=version_still_at(hs.spec))


# ---------------------------------------------------------------------------
# Adding on top of a house pass: flattened onto the video Omni made
# ---------------------------------------------------------------------------

class Kept(NamedTuple):
    """A pattern a house pass drew, as a pass on top of it draws it again:
    its element (props.resolved: as its Generate froze it), its ref, the
    seed its own recipe drew it with, and that version's id."""
    el: rc.Element
    ref: pattern_ref.PatternRef
    seed: str
    version_id: int


def _drawn_as(exp: dict, el: rc.Element, first: bool) -> Optional[rc.Element]:
    """A pass's pattern with its frozen place: its own props.resolved, else
    the place and colour its run recorded (evidence: a pattern the preview
    could not draw was placed by the run itself), else None."""
    if pattern_el.frozen(el) is not None:
        return el
    ev = exp.get("evidence") or {}
    many = ev.get("omni_patterns")
    found = (next((p for p in many if isinstance(p, dict) and p.get("element_id") == el.id), None) if many
             else ev.get("omni_pattern") if first else None)
    if not isinstance(found, dict) or not isinstance(found.get("layout"), dict) or not found.get("colour"):
        return None
    res = {"layout": found["layout"], "colour": str(found["colour"]).lower(), "engine_v": HOUSE_VERSION}
    for k in ("ground", "ink"):
        if found.get(k):
            res[k] = str(found[k]).lower()
    return el.model_copy(update={"props": {**el.props, "resolved": res}})


async def kept_layers(bv: hbase.BaseVersion, job: pattern_el.PatternJob) -> list[Kept]:
    """Added on top of a house pass (bv.passes), the patterns every pass
    drew, back to front — each exactly as its Generate froze it, read from
    its row (never from a client), with its own seed: drawn again over the
    ground's video under the new ones, an unchanged draft is that version's
    very spec. [] for a version Omni made. Made once (bv.kept). 409
    (base.PASS_UNREADABLE) when one can't be drawn where it was."""
    if not bv.passes:
        return []
    if bv.kept is not None:
        return bv.kept
    from fastapi import HTTPException

    def refuse(exp: dict, why: str) -> HTTPException:
        log.warning("[house] version #%s can't be added to: %s", exp.get("id"), why)
        return HTTPException(409, hbase.PASS_UNREADABLE)

    out: list[Kept] = []
    for exp in bv.passes:
        recipe = hbase.recipe_of(exp)
        if recipe is None:
            raise refuse(exp, "its recipe can't be read")
        for i, el in enumerate(rc.pattern_elements(recipe)):
            drawn = _drawn_as(exp, el, i == 0)
            if drawn is None:
                raise refuse(exp, f"{el.id} wasn't pinned where it was drawn")
            # Each pass's own folder: its "pattern" is not the new one's.
            res = await pattern_el.from_resolved(
                drawn, dataclasses.replace(job, out_dir=Path(job.out_dir) / "kept" / f"v{int(exp['id'])}"))
            if res.ref is None:
                raise refuse(exp, f"{el.id} couldn't be drawn again ({res.error})")
            out.append(Kept(drawn, res.ref, recipe.seed or "", int(exp["id"])))
    bv.kept = out
    return out


async def flatten(bv: hbase.BaseVersion, job: pattern_el.PatternJob, *,
                  mask: Callable[[Path], Any]) -> hbase.BaseVersion:
    """A house pass made ready to add on top of (after base.stack_parts,
    which gave it its ground's parts): its passes' patterns (kept_layers)
    laid on each part's settled frame as a preview lays new ones (runner.
    target, cut by the speaker's `mask`) — the version as it looks, without
    its icons, as a version Omni made is shown from its clean parts — and
    their ink added to what a new pattern (and an icon) keeps off. Nothing
    for a version Omni made. The same for the studio and the run."""
    kept = await kept_layers(bv, job)
    if not kept or not bv.frames:
        return bv
    refs = [k.ref for k in kept]
    last = hbase.recipe_of(bv.passes[-1]) or rc.preset_42_house()
    frames: list[Path] = []
    for j, frame in enumerate(bv.frames):
        out = Path(frame).with_name(f"c{j}_kept.png")
        if not out.exists():
            person = await mask(Path(frame))
            await _thread(runner.target, Path(frame), out, refs, last, person=person)
        frames.append(out)
    bv.frames = frames
    from PIL import Image

    with Image.open(frames[0]) as im:
        fw, fh = im.size
    footprint = await _thread(hbase.new_ink, runner.layers(refs, last), fw, fh)
    ink = (np.asarray(footprint) > 0.02).astype(np.float32)
    bv.occupied = ink if bv.occupied is None else np.maximum(np.asarray(bv.occupied, dtype=np.float32), ink)
    return bv


def draft_path(scene_folder: Path, key: str) -> Path:
    """Where the studio keeps its render of a draft (<scene>/house/<key>.mp4):
    a Generate of the same spec copies it rather than render again."""
    return Path(scene_folder) / "house" / f"{key}.mp4"


def hand_over(out: Path, made: Path) -> None:
    """A run's render copied to where the studio keeps its draft of the very
    same spec (draft_path), when none is there yet: the draft that carries on
    from the version it made ("Add more") is that spec, so it plays its exact
    video at once. Written whole or not at all; a copy that fails costs only
    that (the studio renders it again). Synchronous."""
    made = Path(made)
    if made.is_file():
        return
    tmp = made.with_name(f"{made.stem}.{os.getpid()}.{threading.get_ident()}.part{made.suffix}")
    try:
        made.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(out, tmp)
        os.replace(tmp, made)
    except OSError:
        tmp.unlink(missing_ok=True)
        log.warning("[house] the render could not be handed to the studio (%s)", made.name)


def one_speaker(out: Path, source: Path) -> list[int]:
    """guards.one_speaker on a render drawn over a version Omni made: the
    frames showing a second person against the original's speaker (local
    MODNet, never a paid matte). [] when the check itself can't run (said in
    the log: the version is still made). Synchronous."""
    from . import guards as gd

    try:
        return gd.one_speaker(out, source)
    except Exception:  # noqa: BLE001 — a check, never a failure of the run
        log.exception("[house] the one-speaker check could not run on %s", Path(out).name)
        return []


# ---------------------------------------------------------------------------
# The run
# ---------------------------------------------------------------------------

def _layers_public(spec: pl.PlateSpec, pairs: Sequence[tuple]) -> list[dict]:
    """Each drawn layer as the arm records it. Matched to the pairs in draw
    order (plate_spec keeps it, leaving out a layer with no ink): a house
    pass's own layer and a new one may share an element id. A pass's own
    says which version drew it (`kept`)."""
    out = []
    at = 0
    for ly in spec.layers:
        while at < len(pairs) and (pairs[at][0].id != ly.element_id or pairs[at][1].colour != ly.colour):
            at += 1
        item = pairs[at] if at < len(pairs) else None
        at += 1
        ref = item[1] if item is not None else None
        row = {"element_id": ly.element_id, "slug": ref.slug if ref else None, "colour": ly.colour,
               "layout": ref.layout.public() if ref else None, "follow": ly.follow,
               "hand": ly.hand is not None, "timing": ly.timing.public() if ly.timing else None}
        if isinstance(item, Kept):
            row["kept"] = item.version_id
        out.append(row)
    return out


async def edit_scene(ctx: Any, arm: str, seg: Any, out_dir: Path, label: str,
                     manifest: st.Manifest) -> Path:
    """One scene made in house from ctx.recipe (engine "house"). Raises on
    failure; `ctx` is the pipeline's run context, its patterns already built
    (pipeline._build_pattern_ref: frozen at Generate, else placed with the
    local matte).

      1. the scene's matte bank and camera — made once per clip, shared with
         the studio (the run's clip is the studio's own copy);
      2. the patterns as layers (added on top of a house pass, its passes'
         own first: ctx.base.kept), our icons (the plan frozen at Generate;
         added on top of a version with ours and no icon element, that
         version's again — base.inherited_icons) in one Stamper for the
         scene;
      3. the render — or the studio's render of this very spec, copied
         (waited for when it is under way; the studio's drafts of anything
         else are stopped: a Generate outranks them); added on top of a
         version Omni made, over that version's own clip (its clean one: it
         is never drawn over twice) — of a house pass, over the very clip it
         was drawn over (ctx.base.under);
      4. the version's still (frame 0, the plate at rest at its first icon);
      5. a render made here is handed to the studio's draft folder (the
         draft that carries on from this version is this very spec: its
         exact video plays at once); one drawn over a version Omni made is
         checked for a second person (guards.one_speaker, local), and the
         version says where one shows.

    Recorded: the stages mattes, camera and render (entering: base.py reads
    a house version from it); "omni" c0 and "guards" c0 as well, for any
    reader from before; the arm says it was made in house, at $0.

    options.end_frame (the scene end the version is made for, in the reel's
    30 fps frames): the plate stops there (spec.n = min(the clip's frames,
    end)), as the studio's house_spec resolves the same end."""
    from . import studio               # late: the studio imports the pipeline, which imports this

    recipe: rc.Recipe = ctx.recipe
    exp_id = ctx.exp_id
    name = runner._slug(label)
    src = Path(seg.base or seg.source)
    stack = runner.stacked(ctx)
    await runner._step(exp_id, arm, f"{label}: made in house",
                       detail="our own graphics over the untouched footage — no Omni, $0")
    folder = await _thread(studio.analysis_dir, src)
    # Stopped (the version's Stop): the work in its thread stops too.
    stop = threading.Event()
    t0 = time.monotonic()
    try:
        bank = await _thread(mattes.MatteBank.open, src, folder)
        if bank.filled:
            # Analysed already (the studio did, on this very clip): read
            # back, without waiting behind a draft render for the gate.
            track = await _thread(camera_mod.build, src, folder, bank=bank, cancel=stop.is_set)
        else:
            async with studio._gate():
                await _thread(bank.fill, cancel=stop.is_set)
                track = await _thread(camera_mod.build, src, folder, bank=bank, cancel=stop.is_set)
    except asyncio.CancelledError:
        stop.set()
        raise
    manifest.record("mattes", "c0", bank.key, path=bank.path, seconds=time.monotonic() - t0,
                    every=bank.every, keyframes=bank.progress()[1])
    cam = track.public()
    manifest.record("camera", "c0", track.key, method=track.method, zoom_max=cam["zoom_max"],
                    shift_max_px=cam["shift_max_px"], cuts_sec=cam["cuts_sec"],
                    held_from_sec=cam["held_from_sec"], warnings=list(track.warnings))
    warnings: list[str] = list(track.warnings)

    els = {e.id: e for e in rc.pattern_elements(recipe)}
    pairs: list[tuple] = [(els[r.element_id], r.ref) for r in (getattr(ctx, "pattern_results", None) or [])
                          if r.ref is not None and r.element_id in els]
    if stack and ctx.base.passes:
        # Added on top of a house pass: its passes' own patterns again, under
        # the new ones, over the video Omni made (pipeline: house.flatten).
        if ctx.base.kept is None:
            raise RuntimeError("the version's own layers weren't laid out")
        pairs = [*ctx.base.kept, *pairs]
    inherited = hbase.inherited_icons(recipe, ctx.base) if stack else None
    icons = await runner.icons_for(ctx, arm, label, [src], manifest, inherited=inherited)
    if icons is not None:
        warnings += list(icons.warnings)
    under = None
    if stack:
        parts = [Path(p) for p in ctx.base.sources]
        under = ctx.base.under or (parts[0] if len(parts) == 1 else await _thread(
            media.concat_exact, parts, out_dir / f"{name}_under.mp4"))
    # An intro or outro with no one in it: nothing cut out, its patterns
    # held still (person_of — the studio resolves it from the same bank).
    person = await _thread(person_of, str(getattr(seg, "kind", "scene")), bank,
                           ((getattr(ctx, "options", None) or {}).get("source") or {}).get("src"))
    end = (getattr(ctx, "options", None) or {}).get("end_frame")
    spec = await _thread(plate_spec, recipe, pairs, width=bank.width, height=bank.height, fps=bank.fps,
                         n=min(bank.n, int(end)) if end else bank.n, bars=bank.bars,
                         stamper=icons.stamps[0] if icons is not None else None,
                         icons=icons.resolved if icons is not None else (), person=person)
    key = await _thread(render_key, spec, source=src, under=under, camera_key=track.key, bank_key=bank.key)
    out = out_dir / f"{name}.mp4"
    out.parent.mkdir(parents=True, exist_ok=True)
    t1 = time.monotonic()
    reused = False
    scene_folder = await _thread(studio.scene_base, ctx.run_id, str(ctx.options.get("target") or ""), seg.source,
                                 full=ctx.options.get("variant") == "full")
    made = draft_path(scene_folder, key) if key else None
    if made is not None and not made.is_file():
        # The studio is rendering this very spec: waited for, then copied
        # (one render, never two).
        await studio.draft_ready(key)
    if made is not None and made.is_file():
        await _thread(shutil.copyfile, made, out)
        reused = True
        await runner._step(exp_id, arm, f"{label}: the preview's render reused", status="done",
                           detail="made already, exactly as asked")
    else:
        # A Generate outranks the studio's drafts of anything else.
        studio.cancel_drafts(scene_folder)
        try:
            async with studio._gate():
                await _thread(render, spec, src, out, camera=track, bank=bank, under=under, cancel=stop.is_set)
        except asyncio.CancelledError:
            stop.set()
            raise
        if made is not None:
            # The studio carries on from this version (Add more): its draft
            # is this very spec, so its exact video is this file.
            await _thread(hand_over, out, made)
    seconds = time.monotonic() - t1
    enters = any(ly.timing is not None and ly.timing.t_full > ly.timing.t_in for ly in spec.layers)
    settles = enters or bool(stack and hbase.entering(ctx.base.exp, 0))
    manifest.record("render", "c0", key, path=out, seconds=seconds, reused=reused, engine_v=HOUSE_VERSION,
                    frames=spec.n, under=str(under) if under else None, entering=enters, settles=settles)
    manifest.record("omni", "c0", None, path=out, engine="house", entering=enters, settles=settles)
    manifest.record("guards", "c0", None, path=out, engine="house", speaker_front=spec.speaker_front)
    second: list[int] = []
    if under is not None:
        # Drawn over a version Omni made: its speaker is the original's, put
        # back by its own pass — checked (local MODNet, under the gate: this
        # PC is short of memory), never assumed.
        async with studio._gate():
            second = await _thread(one_speaker, out, src)
        if second:
            warnings.append(hbase.SECOND_PERSON.format(t=f"{second[0] / spec.fps:.1f} s"))
    if not stack:
        # The version's still: frame 0's speaker, the plate at rest at its
        # first icon — never two moments of the footage mixed.
        try:
            await _thread(write_still, spec, src, hbase.still_path(exp_id, 0), camera=track, bank=bank,
                          t_s=version_still_at(spec))
        except Exception:  # noqa: BLE001 — base.still makes one from the video instead
            log.exception("[rebrand] #%s: the in-house still failed", exp_id)
    await store.patch_arm(exp_id, arm, {
        "provider": PROVIDER, "engine": "house", "model": HOUSE_MODEL, "cost_usd": 0.0, "warnings": warnings,
        "house": {"engine_v": HOUSE_VERSION,
                  "camera": {k: cam[k] for k in ("method", "zoom_max", "shift_max_px", "cuts_sec",
                                                 "held_from_sec", "warnings")},
                  "mattes": {"every": bank.every, "keyframes": bank.progress()[1]},
                  "render": {"seconds": round(seconds, 2), "reused_preview": reused, "frames": spec.n},
                  "layers": _layers_public(spec, pairs), "on": "version" if stack else "original",
                  **({"one_speaker": {"checked": True, "frames": second}} if under is not None else {})},
    })
    await runner._step(exp_id, arm, f"{label}: made", status="done",
                       detail=f"in house in {round(time.monotonic() - t0)} s — $0")
    return out
