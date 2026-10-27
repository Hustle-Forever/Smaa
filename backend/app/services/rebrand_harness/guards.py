"""Guards — what Omni must never get wrong, fixed after it has drawn.

  speaker in front   Omni draws graphics over the speaker whatever it is told,
                     so the speaker is put back in front of every behind
                     element, frame by frame, from the original chunk
                     (speaker_front.restore_speaker).
  entrance veil      the chunk carrying an entrance is cleared of the frosted
                     panel Omni sometimes slides in with it, for its first
                     VEIL_GUARD_SEC (speaker_front.veil_restore_mask).
  letterbox bars     MODNet's specks on a black bar are not the speaker: the
                     mask is ignored there, for the target frame and for the
                     restore alike, so the bar keeps the pattern drawn in it.
  icons              the recipe's icons are laid in by the same pass, behind
                     the speaker (and, on a frame they show, the mask is
                     ignored where they sit on a bar).
  stray drawings     opt-in (guards.stray "clean"): outside the FOOTPRINT of
                     what the recipe draws — its patterns' ink grown by
                     STRAY_GROW_FRAC and feathered, and our icons' boxes —
                     the same pass puts the frame back to what Omni was
                     given, so a mark it drew on its own is gone (experiment
                     23's line-art head). Off, the run looks for such marks
                     instead (stray.find) and the version says so. Either
                     only when Omni is asked for nothing but the patterns
                     (asks_omni_for_more): the approved 42 default's brief
                     asks it for icons of its own, which are no strays.

  one speaker        a check, not a fix (one_speaker): an output frame whose
                     speaker's matte is not the source frame's shows a
                     second person — Omni's old pose around the restored
                     one, as a Veo tail flash made on v25's last frames.
                     Local MODNet only.

The recipe's guards pick these per run; "auto" bars follow the pattern (every
pattern but the approved structure strip, whose pixels stay as approved).
With several patterns "auto" is on when any of them asks, for every layer
and for the restore alike.

The veil test undoes even pulls TOWARDS WHITE, which is also what a white or
light-grey pattern fading in looks like. With several patterns, or a light
neutral colour picked for one, the patterns' own ink is kept out of the veil
(`veil_keep`): the runner hands restore_speaker the patterns' union alpha,
grown a little, and the veil is only undone outside it.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional, Sequence, Union

from .. import rebrand_pattern_ref as pattern_ref
from .. import speaker_front
from . import recipe as rc

Refs = Union[pattern_ref.PatternRef, Sequence[pattern_ref.PatternRef], None]

# A picked colour at least this bright (HSV value) and at most this colourful
# (max - min, 0..1) is a light neutral: fading in, it is an even pull towards
# white — a veil to the veil test. Charcoal and every vivid colour are not.
LIGHT_VALUE = 0.55
LIGHT_CHROMA = 0.20

# The clean-up's footprint: the patterns' ink grown by this share of the
# frame's short side (Omni's lines land near, not exactly on, the target's;
# the hold of "Add on top" grows by the same), then fading out over this
# much more — so it is empty past 5 %; a mark the check would flag (clear of
# the patterns by stray.NEAR, 4 %) is all but gone. Set on experiment 23's
# real Omni clip: where Omni ran a pattern on past the target, a narrower
# fade cut its lines off short; this one lets them taper out. No seam shows
# where Omni's room meets the original's (frame-to-frame change along it
# 1.91 against 1.90 without the clean-up and 1.89 in the original).
STRAY_GROW_FRAC = 0.03
STRAY_FEATHER_FRAC = 0.02
# A target frame's pixel this far (any channel, of 255) from the frame it
# was drawn on carries a layer (base.INK_MIN).
STRAY_INK_MIN = 12


@dataclass(frozen=True)
class ChunkGuards:
    speaker_front: bool
    clear_veil_sec: float
    clear_bars: bool
    veil_keep: bool = False
    stray_clean: bool = False

    def public(self) -> dict:
        out = {"speaker_front": self.speaker_front, "clear_veil_sec": self.clear_veil_sec,
               "clear_bars": self.clear_bars}
        # Only when set, so every record from before it (and the approved
        # run's call log) keeps the shape it had.
        if self.veil_keep:
            out["veil_keep"] = True
        if self.stray_clean:
            out["stray_clean"] = True
        return out


def _refs(refs: Refs) -> list[pattern_ref.PatternRef]:
    if refs is None:
        return []
    if isinstance(refs, pattern_ref.PatternRef):
        return [refs]
    return [r for r in refs if r is not None]


def clears_bars(guards: rc.Guards, refs: Refs) -> bool:
    if guards.bars == "auto":
        return any(r.clears_bars for r in _refs(refs))
    return guards.bars == "on"


def light_neutral(colour: Optional[str]) -> bool:
    """Whether a #rrggbb colour is a light neutral (white, light grey)."""
    if not colour:
        return False
    try:
        r, g, b = (c / 255.0 for c in pattern_ref._rgb(colour))
    except (TypeError, ValueError):
        return False
    return max(r, g, b) >= LIGHT_VALUE and (max(r, g, b) - min(r, g, b)) <= LIGHT_CHROMA


def wants_veil_keep(recipe: rc.Recipe, refs: Refs) -> bool:
    """Keep the patterns' ink out of the veil guard: two or more patterns,
    or one drawn in a light neutral the recipe picked. The 42 default (one
    pattern in the intro's colour) never does."""
    found = _refs(refs)
    if len(found) > 1:
        return True
    from .elements import pattern as pattern_el

    return any(light_neutral(pattern_el.colour_override(recipe.colours, el))
               for el in rc.pattern_elements(recipe))


def for_chunk(guards: rc.Guards, refs: Refs, *, entering: bool, veil_keep: bool = False,
              stray_clean: bool = False) -> ChunkGuards:
    clear_veil_sec = speaker_front.VEIL_GUARD_SEC if (entering and guards.veil == "entrance") else 0.0
    return ChunkGuards(
        speaker_front=guards.speaker_front,
        clear_veil_sec=clear_veil_sec,
        clear_bars=clears_bars(guards, refs),
        veil_keep=bool(veil_keep and clear_veil_sec > 0),
        stray_clean=bool(stray_clean),
    )


def omni_icons(recipe: rc.Recipe) -> bool:
    """Whether Omni is asked for icons of its own (an icon element that
    keeps Omni's): they may go anywhere, so nothing outside the patterns is
    a stray then."""
    return rc.icon_element(recipe) is not None and not rc.icons_by_pipeline(recipe)


def asks_omni_for_more(recipe: rc.Recipe) -> bool:
    """Whether Omni is asked for more than the recipe's patterns, so what it
    adds outside them may be what it was asked for: icons of its own (an
    icon element that keeps Omni's; or, with none of ours stamped, the
    brief's — rebrand_brief asks for some unless icons_by_pipeline), or,
    with no pattern for the brief to describe, the brand graphics the brief
    makes up. The harness's own opening asks for the patterns only."""
    if omni_icons(recipe):
        return True
    if recipe.director.head != "brief":
        return False
    return not (rc.icons_by_pipeline(recipe) and rc.pattern_elements(recipe))


def cleans_strays(recipe: rc.Recipe) -> bool:
    """Whether a run puts the frame back outside what the recipe draws: the
    clean-up is on (guards.stray "clean") and Omni is asked for nothing
    else (asks_omni_for_more), which it would take away."""
    return recipe.guards.stray == "clean" and not asks_omni_for_more(recipe)


def checks_strays(recipe: rc.Recipe) -> bool:
    """Whether a run looks for marks Omni drew on its own (stray.find) and
    says so on the version: the clean-up is off — so the studio can offer
    it — and Omni is asked for nothing else. Not the approved 42 default,
    whose brief asks Omni for icons: its run is as it was."""
    return recipe.guards.stray is None and not asks_omni_for_more(recipe)


def drawn_ink(target: Path, frame: Path, width: int, height: int) -> Any:
    """Where a TARGET frame differs from the frame it was drawn on (bool
    HxW at width x height): the layers laid into it. Both are our own
    composites, so it is the layers exactly, not Omni's re-render."""
    import numpy as np
    from PIL import Image

    with Image.open(target) as t, Image.open(frame) as f:
        a = np.asarray(t.convert("RGB").resize((width, height)), dtype=np.int16)
        b = np.asarray(f.convert("RGB").resize((width, height)), dtype=np.int16)
    return np.abs(a - b).max(axis=2) >= STRAY_INK_MIN


def stray_footprint(ink: Any, *, boxes: Sequence = ()) -> Any:
    """The clean-up's footprint (float HxW, 0..1: 1 = keep Omni's pixel)
    from `ink` (HxW, > 0.02 = a layer's) and our icons' `boxes` (pixel
    x0, y0, x1, y1): 1 within STRAY_GROW_FRAC of the short side of either,
    fading to 0 over STRAY_FEATHER_FRAC more, 0 beyond. All zeros: nothing
    drawn — the whole frame goes back."""
    import cv2
    import numpy as np

    drawn = np.asarray(ink, dtype=np.float32) > 0.02
    h, w = drawn.shape
    drawn = drawn.copy()
    for x0, y0, x1, y1 in boxes or ():
        drawn[max(0, int(y0)):max(0, int(y1)), max(0, int(x0)):max(0, int(x1))] = True
    if not drawn.any():
        return np.zeros((h, w), dtype=np.float32)
    short = min(w, h)
    grow = short * STRAY_GROW_FRAC
    feather = max(1.0, short * STRAY_FEATHER_FRAC)
    # Each pixel's distance to the nearest drawn one.
    dist = cv2.distanceTransform((~drawn).astype(np.uint8), cv2.DIST_L2, cv2.DIST_MASK_PRECISE)
    return np.clip((grow + feather - dist) / feather, 0.0, 1.0).astype(np.float32)


def apply(edited: Path, original: Path, out: Path, g: ChunkGuards, *, veil_keep: Any = None,
          backdrop: Optional[Path] = None, hold: Any = None, stamps: Any = None,
          clean: Optional[Path] = None, footprint: Any = None, cuts: Sequence[int] = ()) -> Path:
    """The speaker put back in front (and the veil, the bars) — synchronous,
    callers wrap it in a thread. Raises on failure; the runner keeps Omni's
    clip then rather than losing the edit.

    `veil_keep` (float HxW, 1 = pattern ink the veil guard must not undo),
    `backdrop` (the finished version Omni drew on, "Add on top"), `hold`
    (float HxW, 1 = keep the backdrop's pixel), `stamps` (the icons, an
    icon_render.Stamper, laid in behind the speaker), `clean` (a second
    output without them) and `footprint` (float HxW, the stray-drawing
    clean-up: outside it the frame goes back) each reach restore_speaker
    only when given, so a run that has none makes exactly the call it
    always made (tests/test_rebrand_golden.py). So do `cuts` (the original
    part's own jump cuts, media.cuts_of: the matte never unions across one),
    only when there are any."""
    extra = {k: v for k, v in (("veil_keep", veil_keep), ("backdrop", backdrop), ("hold", hold),
                               ("stamps", stamps), ("clean", clean), ("footprint", footprint))
             if v is not None}
    if cuts:
        extra["cuts"] = [int(c) for c in cuts]
    return speaker_front.restore_speaker(
        edited, original, out, clear_veil_sec=g.clear_veil_sec, clear_bars=g.clear_bars, **extra,
    )


# The one-speaker check. An output frame whose speaker (MODNet, > 0.5)
# overlaps the source frame's by less than ONE_SPEAKER_IOU of their union,
# or covers more than ONE_SPEAKER_GROW x its area, shows someone the source
# does not. Measured on v25: 0.995-0.999 on every good frame; its last
# frame, two people, 0.889 and 1.11x.
ONE_SPEAKER_IOU = 0.95
ONE_SPEAKER_GROW = 1.05
# Its frames by default: every ONE_SPEAKER_EVERY-th, the last
# ONE_SPEAKER_TAIL (where a finished video rests) and ONE_SPEAKER_NEAR
# either side of each of the source's own cuts.
ONE_SPEAKER_EVERY = 10
ONE_SPEAKER_TAIL = 12
ONE_SPEAKER_NEAR = 2


def one_speaker_frames(n: int, cuts: Sequence[int] = ()) -> list[int]:
    """The frames one_speaker checks by default in an output of n frames."""
    picked = set(range(0, n, ONE_SPEAKER_EVERY)) | set(range(max(0, n - ONE_SPEAKER_TAIL), n))
    for c in cuts:
        picked |= {i for i in range(int(c) - ONE_SPEAKER_NEAR, int(c) + ONE_SPEAKER_NEAR + 1) if 0 <= i < n}
    return sorted(picked)


def one_speaker(output: Path, source: Path, *, frames: Optional[Sequence[int]] = None,
                matte: Optional[Callable[[Any], Any]] = None) -> list[int]:
    """The frames of `output` that show a second person: its speaker's
    matte against the SOURCE frame's (the same index; the source's last
    frame past its end), by the thresholds above. [] when every checked
    frame shows the one speaker.

    `frames` (output frame numbers) default to one_speaker_frames — about
    36 for an 8 s scene, ~20 s of MODNet on this CPU. `matte` (BGR frame ->
    float HxW 0..1) replaces speaker_front's raw MODNet matte, which is
    local: this never pays for a matte. The letterbox bars (the source
    frame's) are left out of both. Synchronous and read-only; it holds one
    frame of each clip at a time."""
    import cv2
    import numpy as np

    from .. import rebrand_media as media

    matte = matte or speaker_front.matte
    output, source = Path(output), Path(source)
    if frames is None:
        frames = one_speaker_frames(speaker_front._count(output), media.shot_cuts(source))
    wanted = {int(i) for i in frames if int(i) >= 0}
    if not wanted:
        return []
    top = max(wanted)
    flagged: list[int] = []
    out_cap, src_cap = cv2.VideoCapture(str(output)), cv2.VideoCapture(str(source))
    fs = None
    try:
        i = 0
        while i <= top:
            ok, fe = out_cap.read()
            if not ok:
                break
            ok_src, got = src_cap.read()
            if ok_src:
                fs = got
            if i in wanted and fs is not None:
                h, w = fs.shape[:2]
                if fe.shape[:2] != (h, w):
                    fe = cv2.resize(fe, (w, h), interpolation=cv2.INTER_AREA)
                ms = np.asarray(matte(fs), dtype=np.float32) > 0.5
                me = np.asarray(matte(fe), dtype=np.float32) > 0.5
                bars = pattern_ref.bar_mask(fs)
                if bars is not None:
                    ms, me = ms & ~bars, me & ~bars
                union = int((ms | me).sum())
                iou = float((ms & me).sum()) / union if union else 1.0
                grow = float(me.sum()) / max(1, int(ms.sum())) if me.any() else 0.0
                if iou < ONE_SPEAKER_IOU or grow > ONE_SPEAKER_GROW:
                    flagged.append(i)
            i += 1
    finally:
        out_cap.release()
        src_cap.release()
    return flagged
