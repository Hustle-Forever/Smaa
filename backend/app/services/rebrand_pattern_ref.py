"""Omni's one reference image — a 42 pattern, coloured in the intro's colours.

The Omni arm used to attach the real 42 logo to every scene edit. The owner
dropped that: Omni redraws what it is handed, a redrawn logo is a wrong logo,
and the thing the reel actually lacked was not a mark but a GROUND — the
branded strip the topic icons sit on. So Omni now gets a brand pattern
instead — the left-strip one, 2.png (see OMNI_SLUG) — coloured before it is
sent, because Omni must never be the one choosing the colours:

  GROUND  the intro's background colour, dug out of the intro clip itself: the
          heaviest colour of its palette once the bins one gradient is split
          across are merged back into one colour, and only a colour that also
          reaches the frame's edges (a huge headline is heavy too, but it is
          laid ON the background, inset from the border). Letterbox bars are
          trimmed away first — TH v2 intros are a 16:9 card band inside a
          black 9:16 frame, and "the heaviest colour" of that frame is the bar.
  INK     the pattern's line colour. Also from the intro: its second colour
          (usually the card's type) when that reads against the ground; else
          the ground's own hue pushed lighter or darker until it does. Never a
          colour the intro does not already imply.

The result is an RGBA PNG at the reel's size: the pattern's lines in the
GROUND colour (the intro's background colour — the one colour dug out of the
intro), and a TRANSPARENT background everywhere else. The owner asked for
exactly that: an earlier version filled the background with the ground and
drew the lines in INK, and Omni laid that solid block down as the strip. Only
the pattern's own lines may show; INK is still measured (the evidence reports
it) but is not drawn.

Everything here is synchronous pixel work (numpy + PIL, and ffmpeg frame
seeks through rebrand_moments for a video intro); the pipeline calls `build`
through `asyncio.to_thread`. No model is called and nothing costs money.
"""
from __future__ import annotations

import colorsys
import logging
import math
import random
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from . import pattern_kit
from . import rebrand_media as media
from . import rebrand_moments as moments
from .rebrand_harness.recipe import Hand

log = logging.getLogger("archivist.rebrand.pattern_ref")

# The print-resolution source art (1.png..5.png), at the repo root. Only read
# when the prepared kit is missing — pattern_kit does the heavy lifting once.
PATTERNS_DIR = Path(__file__).resolve().parents[3] / "patterns"

# The five prepared masks again, shipped WITH the code. The kit itself lives
# under backend/data/, which is gitignored, kept out of the Docker build
# context (.dockerignore: data/) and, on the VPS, buried under the shared
# studio-data volume; the sources it is built from (repo-root patterns/) are
# gitignored as well. So in production KIT_DIR is always empty and the kit
# can never be rebuilt — without this copy every Omni run there would go out
# with no pattern, and the only sign would be a failed "Pattern: none" step.
# Refresh it whenever the kit is regenerated (copy
# backend/data/branding/patterns/*.mask.png here); a test fails while the two
# disagree on a machine that has both.
BUNDLED_DIR = Path(__file__).resolve().parents[1] / "assets" / "patterns"

# 42's own violet and off-white, measured off the reference reels. Used when
# the intro has no colour worth inheriting (there is no intro, or it is dark
# from end to end) — a brand-coloured pattern beats one tinted from a black
# rectangle.
BRAND_GROUND = "#7f2bf2"
BRAND_INK = "#f4f4f8"

# Each pattern has its place, in the owner's words — pattern_library holds
# them (2.png the LEFT strip; 5.png stood up as a left strip too; 3.png and
# 4.png the whole background; 1.png placed per clip wherever it fits best).
# With no pattern picked, Omni's scene edit gets the approved left strip.
OMNI_SLUG = "structure"
# The pattern is part of the BACKGROUND, behind the speaker (owner,
# 2026-09-25: "make it big, 2x the size ... behind the character ... it
# starts from the left but can go until the middle of the screen"). The art is
# drawn at OMNI_SCALE x the frame's width — twice the strip version tagged
# checkpoint-omni-pattern-success — and slid in until its own right-most
# stroke reaches OMNI_REACH_FRAC of the width; the speaker covers whatever of
# it falls behind them (strip_preview + person_matte).
OMNI_SCALE = 2.0
OMNI_REACH_FRAC = 0.50
OMNI_WHERE = "the left half of the frame, behind the speaker"

PLACEMENTS = ("left", "right", "top", "bottom", "full")
# How a FULL-screen pattern covers the frame (Layout.fit). "tile" is how every
# full pattern was drawn before 2026-09-26 and stays the default, so a stored
# upload row without the field renders exactly as it did.
FITS = ("tile", "centre", "cover")


@dataclass(frozen=True)
class Layout:
    """Where one pattern goes in the frame — part of the pattern, not of the
    run (pattern_library holds one per pattern, or several candidates the
    planner chooses between per clip; the owner's rules for the built-ins,
    the upload analysis for the rest). Always behind the speaker.

      left / right  the art `scale` x the frame's width, tiled down, slid in
                    from that edge until its own last stroke is `reach` of
                    the way across (structure: 2x, half the frame).
      top / bottom  one band along that edge from the `anchor` corner, `scale`
                    of the frame's width, at most `reach` of its height.
      full          the whole background, as `fit` says:
                      tile    the art `scale` x the frame's width, tiled from
                              the top-left corner (the original behaviour)
                      centre  a TEXTURE (the grid): `scale` x the frame's
                              SHORT side, tiled with its centre on the
                              frame's centre, symmetric about the speaker
                      cover   a COMPOSITION (blueprint): ONE copy, scaled to
                              just cover the frame, x max(1, `scale`), cropped
                              where it fills best around the speaker — never
                              repeated, so no second map and no seam
    `rotate` (clockwise degrees: 0, 90, 180 or 270 — 270 is a quarter turn
    counter-clockwise) turns the art first: 5.png stood up as a left strip
    is 270.
    `valign` (left/right only) is which end of the art sits on the frame's
    edge: "top" tiles down from the top (the original behaviour); "bottom"
    puts the art's bottom on the frame's bottom edge and lets its top run off
    the frame's top. 5.png turned counter-clockwise is dense at its bottom,
    and tiled from the top the frame showed its bare hairline end beside the
    speaker — the faded look; bottom-anchored it is a full bead curtain."""
    placement: str = "left"
    scale: float = OMNI_SCALE
    reach: float = OMNI_REACH_FRAC
    anchor: str = "left"
    rotate: int = 0
    fit: str = "tile"
    valign: str = "top"

    def public(self) -> dict:
        # `fit` only when it is not the default, so every layout that existed
        # before it (the approved structure setup included) keeps the exact
        # record it had in evidence and in stored upload rows.
        out = {"placement": self.placement, "scale": self.scale, "reach": self.reach,
               "anchor": self.anchor, "rotate": self.rotate}
        if self.fit != "tile":
            out["fit"] = self.fit
        if self.valign != "top":
            out["valign"] = self.valign
        return out


def placement_label(layout: Layout) -> str:
    """A layout's place in a few words, for people and for the planner's
    report: "left strip", "top-right band", "full frame"."""
    if layout.placement in ("left", "right"):
        return f"{layout.placement} strip"
    if layout.placement in ("top", "bottom"):
        return f"{layout.placement}-{layout.anchor} band"
    return "full frame"


def where_text(layout: Layout) -> str:
    """The layout as the phrase Omni's prompt and the brief use."""
    pct = round(layout.reach * 100)
    if layout.placement in ("left", "right"):
        side = layout.placement
        span = f"the {side} half" if pct == 50 else f"the {side} {pct}%"
        return f"{span} of the frame, behind the speaker"
    if layout.placement in ("top", "bottom"):
        return (f"a band along the {layout.placement} edge of the frame, from the "
                f"{layout.placement}-{layout.anchor} corner, behind the speaker")
    return "the whole background of the frame, behind the speaker"

# Levels (black point, white point) that make a pattern's strokes solid.
# Structure's mask is keyed from brightness, so its alpha is shading: 3 in 4
# inked pixels sat under half opacity, and Omni reproduced the strip as a
# faded ghost. Measured on the 720-wide canvas.
#   beads_field  its thin hairlines and glyph edges sat at partial alpha: 31%
#                of its ink, down to 12% with these levels (alpha-weighted
#                solid share 85% -> 95%), at the rotated left-strip size.
#   beads_row    the threads stringing the beads rendered at 0.17-0.37
#                opacity — the "faded" look the owner complained about; they
#                become solid 1-px lines and the beads are unchanged.
#   grid         applied after STROKE_MASS (below), at the rendered size.
# Every entry is per slug: structure's approved pixels never see the others.
SOLID_LEVELS = {
    "structure": (0.10, 0.35),
    "beads_field": (0.10, 0.45),
    "beads_row": (0.0, 0.30),
    "grid": (0.04, 0.40),
}

# Patterns whose strokes are judged by their MASS, at the mask's own
# resolution, before any resampling: (blur sigma px, black point, white
# point, smallest blob kept px). The grid is graph paper with three tiers —
# a faint 10-px sub-grid (alpha ~0.11), the 50-px lines (~0.24) and the
# crosshairs/dots (1.0) — and the kit's non-integer downscale (13653 -> 2400)
# drifts the lines' phase, so across the sheet a 50-px line is one pixel at
# 0.24 or two at 0.19+0.16. Per-pixel levels then either keep the sub-grid
# (a haze over the whole frame: only 31% of its inked pixels had a solid
# core, the "faded" look) or break the split lines into dashes. Blurred by
# 1 px every 50-px line peaks at >= 0.105 whichever way it fell and the
# sub-grid at <= 0.056 (measured over all 70 lines), so the levels keep
# exactly the lines, dots and crosshairs, solid (100% of inked pixels with a
# solid core over six real frames); the sub-grid's own crossings, the only
# thing left between, are specks under 40 px and are dropped.
STROKE_MASS = {"grid": (1.0, 0.09, 0.105, 40)}

# Where a full-screen COMPOSITION (fit "cover") is cropped: the offset (on a
# FOCUS_STEPS x FOCUS_STEPS grid over the overflow) whose visible ink best
# fills the frame around a centred speaker and is balanced left/right of
# them. The speaker prior is a centred talking-head silhouette in frame
# fractions — head, shoulders, body (x0, x1, top), fitted to the mean of six
# real speaker mattes; cells are FOCUS_CELL of the short side, and a
# background cell counts as filled at FOCUS_FILL visible ink. Over six real
# frames blueprint went from fill 0.78 / balance 0.70 (tiled from the
# top-left, a second copy of the map stacked at y=982) to 0.93 / 0.88.
SPEAKER_PRIOR = ((0.375, 0.625, 0.20), (0.25, 0.75, 0.36), (0.125, 0.875, 0.50))
FOCUS_STEPS = 21
FOCUS_CELL = 0.125
FOCUS_FILL = 0.03

# A column of art counts as solid when its smoothed share of pixels at >= half
# opacity is at least this fraction of the art's most solid column
# (solid_held) — how pattern_library measures an art's solid run, and where a
# band shrunk inside the frame is made to end.
RUN_SOLID_FRAC = 0.10

# A palette colour is only a candidate ink if it is a real part of the frame,
# not a two-pixel highlight or an anti-aliasing fringe around the type.
INK_MIN_WEIGHT = 0.02
# WCAG contrast ratio the ink must reach against the ground. 2.5 is below the
# text threshold on purpose: this is texture, not copy, and a pattern at full
# text contrast shouts over the icons that sit on it.
INK_MIN_CONTRAST = 2.5
# When the ink has to be DERIVED, aim a little above the floor: Omni re-encodes
# what it copies and softens hairlines, which costs contrast on the way out.
DERIVED_CONTRAST = 3.0
# Two colours closer than this (RGB distance) are the same colour twice.
NEAR_DUPLICATE_RGB = 48.0
# How deep the intro's palette is read. measure()'s top 6 is enough to tint a
# transition, not to find a background: its bins are 32 levels wide, so one
# gradient background spreads over five or six of them — the heaviest single
# bin can be a minority shade, and the card's own type falls off the list.
PALETTE_BINS = 16
# The border ring the ground must reach, as a fraction of the short side on
# every edge, and how much of it: a colour whose share of the ring is below
# this fraction of its share of the whole frame is an inset thing — a
# headline, a blurred title, a subject — not what the card is laid on.
RING_FRAC = 0.06
RING_MIN_RATIO = 0.35
# Below this chroma (max-min, 0..1) a colour is a neutral — white or black
# type. HSL saturation cannot be used for this: it calls #f4f4f8 22% saturated.
NEUTRAL_CHROMA = 0.12
# An ink within this many degrees of the ground's hue belongs to it.
HARMONY_HUE_DEG = 45.0
# The pattern's own colour (see pattern_colour): "a colour" means at least
# this chroma and this brightness (HSV value) — below either, lines drawn
# over footage read as grey or black, which is what the owner saw when a
# charcoal intro (#3d3d3d) gave the pattern its colour. Measured over 26 real
# intro cards: every real brand ground (blue #0461a2 0.64, violet #4c3d90
# 0.56 and up) clears 0.50; the "What is 42?" navy (#1d3873, 0.45) does not,
# and navy lines over footage read as black too.
VIVID_CHROMA = 0.30
VIVID_VALUE = 0.50
# A vivid colour must hold at least this share of the frame to speak for the
# intro — a cyan cube counts, a two-pixel glint does not.
VIVID_MIN_WEIGHT = 0.01

# Letterbox detection: a border row/column is "bar" when its brightest channel
# is this dark and this uniform. Untagged limited-range video can leave black
# at ~16, and compression noise adds a few levels on top.
BAR_MAX_MEAN = 24.0
BAR_MAX_STD = 6.0
# Trim only if it leaves at least this fraction of the frame — otherwise the
# frame IS dark, and that is the fell-back case, not a letterbox.
BAR_MIN_KEEP = 0.12

_STILL_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".bmp"}


# ---------------------------------------------------------------------------
# The record
# ---------------------------------------------------------------------------

@dataclass
class PatternRef:
    """The pattern Omni is sent for one experiment, and where its colours came
    from — enough to reproduce it and to explain it in the evidence panel."""
    slug: str                        # pattern_kit slug, e.g. "blueprint"
    source: str                      # the source file it was prepared from, e.g. "3.png"
    path: Path                       # the coloured RGBA PNG (transparent background)
    ground: str                      # #rrggbb — the intro's background colour (measured)
    ink: str                         # #rrggbb — the intro's contrasting colour (measured, not drawn)
    fell_back: bool                  # True: brand colours, the intro gave none
    intro_frame: Optional[Path] = None   # the frame the colours were read from
    where: str = OMNI_WHERE              # where in the frame Omni lays it
    colour: str = ""                     # #rrggbb the pattern IS drawn in (pattern_colour); ground if unset
    layout: Layout = Layout()            # the pattern's place (pattern_library)
    animation: str = "left_to_right"     # its entrance (pattern_library.ANIMATIONS)
    name: str = ""                       # shown to people
    mask: Optional[Path] = None          # the mask it is rendered from (None: the kit's, by slug)
    # Moved, sized or turned by hand (the in-house engine: recipe.hand_of,
    # set by elements/pattern.py) — placed_layer draws it so. Hand(): none.
    hand: Hand = Hand()

    def __post_init__(self) -> None:
        if not self.colour:
            self.colour = self.ground

    @property
    def clears_bars(self) -> bool:
        """Whether the speaker's mask is ignored on letterbox bars (bar_mask)
        when this pattern is cut out behind the speaker and when the speaker
        is put back over Omni's output. Every pattern but the approved
        structure strip, whose pixels stay exactly as approved."""
        return self.slug != OMNI_SLUG

    def public(self) -> dict:
        """JSON-safe, for experiment evidence. `hand` only when it was moved
        by hand (every record from before keeps its shape)."""
        out = {
            "slug": self.slug,
            "source": self.source,
            "path": str(self.path),
            "colour": self.colour,
            "ground": self.ground,
            "ink": self.ink,
            "fell_back": bool(self.fell_back),
            "intro_frame": str(self.intro_frame) if self.intro_frame else None,
            "where": self.where,
            "name": self.name or self.slug,
            "layout": self.layout.public(),
            "animation": self.animation,
        }
        if not self.hand.identity:
            out["hand"] = hand_public(self.hand)
        return out


# ---------------------------------------------------------------------------
# Colour arithmetic (hex in, hex out)
# ---------------------------------------------------------------------------

def _rgb(h: str) -> tuple[int, int, int]:
    s = str(h).strip().lstrip("#")
    if len(s) == 3:
        s = "".join(c * 2 for c in s)
    if len(s) != 6:
        raise ValueError(f"not a #rrggbb colour: {h!r}")
    return int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16)


def _hex(rgb) -> str:
    r, g, b = (int(max(0, min(255, round(float(v))))) for v in rgb)
    return f"#{r:02x}{g:02x}{b:02x}"


def _relative_luminance(h: str) -> float:
    def chan(c: float) -> float:
        c /= 255.0
        return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4

    r, g, b = _rgb(h)
    return 0.2126 * chan(r) + 0.7152 * chan(g) + 0.0722 * chan(b)


def contrast(a: str, b: str) -> float:
    """WCAG contrast ratio, 1..21."""
    la, lb = _relative_luminance(a), _relative_luminance(b)
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


def _distance(a: str, b: str) -> float:
    return math.dist(_rgb(a), _rgb(b))


def _chroma(h: str) -> float:
    r, g, b = _rgb(h)
    return (max(r, g, b) - min(r, g, b)) / 255.0


def _hue_deg(h: str) -> float:
    r, g, b = (c / 255.0 for c in _rgb(h))
    return colorsys.rgb_to_hls(r, g, b)[0] * 360.0


def _hue_gap(a: str, b: str) -> float:
    d = abs(_hue_deg(a) - _hue_deg(b)) % 360.0
    return min(d, 360.0 - d)


# ---------------------------------------------------------------------------
# Ground and ink
# ---------------------------------------------------------------------------

def _harmony(ink: str, ground: str) -> float:
    """How well a candidate ink belongs next to the ground.

    Neutrals (the card's white or black type) and colours in the ground's own
    hue family score full; an unrelated hue — typically a skin tone when the
    intro shows a person — is halved, not banned, so it can still win when it
    is genuinely the intro's second colour."""
    if _chroma(ink) < NEUTRAL_CHROMA or _chroma(ground) < NEUTRAL_CHROMA:
        return 1.0
    return 1.0 if _hue_gap(ink, ground) <= HARMONY_HUE_DEG else 0.5


def _derive_ink(ground: str, *, target: float = DERIVED_CONTRAST) -> str:
    """The ground's own hue, moved in HSL lightness until it reads on it:
    lighter on a dark ground, darker on a light one. Which side is "dark" is
    decided by which extreme the ground contrasts with more, so the walk always
    terminates — every colour reaches at least 4.58:1 against white or black."""
    r, g, b = (c / 255.0 for c in _rgb(ground))
    hue, light, sat = colorsys.rgb_to_hls(r, g, b)
    lighter = contrast(ground, "#ffffff") >= contrast(ground, "#000000")
    steps = 60
    for i in range(1, steps + 1):
        f = i / steps
        l2 = light + (1.0 - light) * f if lighter else light * (1.0 - f)
        cand = _hex(c * 255.0 for c in colorsys.hls_to_rgb(hue, l2, sat))
        if contrast(cand, ground) >= target:
            return cand
    return "#ffffff" if lighter else "#000000"


def _weight(c: dict) -> float:
    return float(c.get("weight") or 0.0)


def _merge_palette(palette: list[dict]) -> list[dict]:
    """The palette with each colour that was split across neighbouring bins
    merged back into one, heaviest first.

    Each round seeds on the colour with the most weight within
    NEAR_DUPLICATE_RGB of it (the densest spot) and takes everything inside
    that radius: a bounded neighbourhood, not a chain — linking neighbour to
    neighbour walks a blue-to-white gradient card straight into its own white
    type and averages the lot into a mid-blue the card never shows. The merged
    colour carries the summed weight (and ring share, when known) and the
    weight-averaged hex of its substantial members; slivers under
    INK_MIN_WEIGHT (anti-aliasing fringes) add their weight but not their
    tint, so a colour alone with its fringe keeps its hex exactly."""
    merged: list[dict] = []
    left = list(palette)
    while left:
        def near(c: dict) -> list[dict]:
            return [o for o in left if _distance(o["hex"], c["hex"]) <= NEAR_DUPLICATE_RGB]

        seed = max(left, key=lambda c: (sum(_weight(o) for o in near(c)), _weight(c)))
        group = near(seed)
        tint = [o for o in group if _weight(o) >= INK_MIN_WEIGHT]
        tw = sum(_weight(o) for o in tint)
        entry: dict = {
            "hex": (_hex(sum(_rgb(o["hex"])[i] * _weight(o) for o in tint) / tw
                         for i in range(3)) if tw > 0 else _hex(_rgb(seed["hex"]))),
            "weight": round(sum(_weight(o) for o in group), 4),
        }
        if all(o.get("ring") is not None for o in group):
            entry["ring"] = round(sum(float(o["ring"]) for o in group), 4)
        merged.append(entry)
        taken = {id(o) for o in group}
        left = [o for o in left if id(o) not in taken]
    return sorted(merged, key=_weight, reverse=True)


def _pick_ground(merged: list[dict]) -> dict:
    """The heaviest substantial colour that also reaches the frame's edges.

    Weight alone picks the headline on a card whose title is huge and blurred
    (a dark smear holding more of the frame than any one of the gradient's
    colours behind it); the border ring tells them apart, because type and
    subjects are inset and the background is what they are inset IN. A
    palette without ring shares (a hand-built one) is ranked by weight only."""
    cands = [c for c in merged if _weight(c) >= INK_MIN_WEIGHT] or merged
    for c in cands:
        ring = c.get("ring")
        if ring is None or float(ring) >= RING_MIN_RATIO * _weight(c):
            return c
    return cands[0]


def ground_and_ink(palette: list[dict]) -> tuple[str, str]:
    """(ground, ink) from a measured palette ([{hex, weight, ...}], optionally
    with each colour's share of the frame's border ring as "ring").

    Split bins are merged first (_merge_palette). GROUND is then the heaviest
    colour that reaches the border (_pick_ground). INK is the best other
    colour that is substantial, clearly distinct and legible on the ground —
    scored by how much of the frame it holds, how well it reads (capped: past
    ~7:1 more contrast buys nothing) and whether it belongs to the ground's
    hue family. Derived from the ground when no palette colour qualifies."""
    if not palette:
        return BRAND_GROUND, BRAND_INK
    merged = _merge_palette(palette)
    picked = _pick_ground(merged)
    ground = _hex(_rgb(picked["hex"]))

    best: Optional[tuple[float, str]] = None
    for c in merged:
        if c is picked:
            continue
        hx = _hex(_rgb(c["hex"]))
        weight = _weight(c)
        ratio = contrast(hx, ground)
        if weight < INK_MIN_WEIGHT or ratio < INK_MIN_CONTRAST:
            continue
        if _distance(hx, ground) < NEAR_DUPLICATE_RGB:
            continue
        score = weight * min(ratio, 7.0) * _harmony(hx, ground)
        if best is None or score > best[0]:
            best = (score, hx)
    if best is not None:
        return ground, best[1]
    return ground, _derive_ink(ground)


def _bin_key(rgb) -> int:
    """rebrand_moments._palette's bin for a colour: 8 levels (32 wide) per
    channel. A bin's mean colour always lies inside that bin, so a palette
    entry's hex maps straight back to the bin it was measured from."""
    r, g, b = (int(v) // 32 for v in rgb)
    return r * 64 + g * 8 + b


def _value(h: str) -> float:
    """HSV value (brightness of the strongest channel), 0..1."""
    return max(_rgb(h)) / 255.0


def _vivid(h: str) -> bool:
    return _chroma(h) >= VIVID_CHROMA and _value(h) >= VIVID_VALUE


def pattern_colour(palette: list[dict]) -> str:
    """The ONE colour the pattern is drawn in, from the intro's palette.

    The intro's background colour when that background IS a colour — the
    blue card gave blue lines, which the owner approved. A neutral or murky
    background (charcoal, black, white, grey, near-black navy) is not what
    the intro looks like: experiment 15's intro was a charcoal card whose
    colour was all in its cyan and violet artwork, and the charcoal lines
    came back as a pattern the owner read as black. Then the pattern takes
    the intro's most prominent vivid colour instead (share of the frame x
    chroma). An intro with no vivid colour at all keeps its background's own
    hue, lifted until it reads (a navy card → lighter navy lines), and a
    truly black-and-white one gets its lighter tone."""
    if not palette:
        return BRAND_GROUND
    merged = _merge_palette(palette)
    ground = _hex(_rgb(_pick_ground(merged)["hex"]))
    if _vivid(ground):
        return ground
    vivid = [c for c in merged if _weight(c) >= VIVID_MIN_WEIGHT and _vivid(c["hex"])]
    if vivid:
        best = max(vivid, key=lambda c: _weight(c) * _chroma(c["hex"]))
        return _hex(_rgb(best["hex"]))
    if _chroma(ground) >= NEUTRAL_CHROMA:
        return _derive_ink(ground)
    _, ink = ground_and_ink(palette)
    return max((ground, ink), key=_relative_luminance)


def intro_palette(frame: Path) -> list[dict]:
    """The frame's palette as measure() bins it — same thumbnail, same 32-level
    bins — read PALETTE_BINS deep, with each colour's share of the frame's
    border ring added as "ring" (0..1)."""
    import numpy as np

    arr = np.asarray(moments._open_small(frame))
    palette = moments._palette(arr, k=PALETTE_BINS)
    h, w = arr.shape[:2]
    r = max(1, int(round(min(h, w) * RING_FRAC)))
    ring = np.zeros((h, w), dtype=bool)
    ring[:r, :] = True
    ring[-r:, :] = True
    ring[:, :r] = True
    ring[:, -r:] = True
    px = arr[ring].astype("int32") // 32
    counts = np.bincount(px[:, 0] * 64 + px[:, 1] * 8 + px[:, 2], minlength=512)
    total = float(counts.sum()) or 1.0
    for c in palette:
        c["ring"] = round(float(counts[_bin_key(_rgb(c["hex"]))]) / total, 4)
    return palette


def _picture_box(value) -> Optional[tuple[int, int, int, int]]:
    """(y0, y1, x0, x1) of the picture inside any letterbox bars, from a
    frame's brightest channel (float H x W); the whole frame when it has no
    bars, None when it is dark throughout (a dark frame, not a letterbox).
    A bar is a run of flat, dark rows (or columns) in from the frame's edge."""
    import numpy as np

    h, w = value.shape

    def bars(lines) -> np.ndarray:
        return (lines.mean(axis=1) <= BAR_MAX_MEAN) & (lines.std(axis=1) <= BAR_MAX_STD)

    def span(flags: np.ndarray) -> tuple[int, int]:
        n = len(flags)
        lo = 0
        while lo < n and flags[lo]:
            lo += 1
        hi = n
        while hi > lo and flags[hi - 1]:
            hi -= 1
        return lo, hi

    y0, y1 = span(bars(value))
    x0, x1 = span(bars(value.T))
    if (y1 - y0) < BAR_MIN_KEEP * h or (x1 - x0) < BAR_MIN_KEEP * w:
        return None
    return y0, y1, x0, x1


def bar_mask(frame) -> Optional["np.ndarray"]:
    """True on a frame's letterbox bars, or None when it has none.
    `frame` is an H x W x 3 uint8 array in either channel order (the rule
    reads the brightest channel, as _trim_letterbox does).

    Nothing in a bar is ever the speaker — the bar is padding outside the
    picture — but MODNet still leaves specks there: on a real letterboxed
    reel 16% of the bottom bar's pixels sat at 0.1-0.95, and cutting the
    pattern with that punched see-through holes into every band, grid and
    blueprint line drawn in the bar (the "faded" look). So a pattern's cut,
    and the speaker put back over Omni's output, ignore the mask there."""
    import numpy as np

    value = np.asarray(frame)[:, :, :3].astype("float32").max(axis=2)
    h, w = value.shape
    box = _picture_box(value)
    if box is None or box == (0, h, 0, w):
        return None
    y0, y1, x0, x1 = box
    bars = np.ones((h, w), dtype=bool)
    bars[y0:y1, x0:x1] = False
    return bars


def _trim_letterbox(frame: Path, out_dir: Path, label: str) -> Path:
    """The picture inside any black bars, written next to the frame; or the
    frame itself when there are no bars (or nothing but bars).

    TH v2 reels are letterboxed and so is their intro card — a 16:9 band in a
    9:16 frame, two thirds of it black. Measured as-is, the intro's "background
    colour" is the letterbox."""
    import numpy as np
    from PIL import Image

    with Image.open(frame) as im:
        rgb = im.convert("RGB")
    # Brightest channel, not luma: a deep navy card has a luma a bar could
    # have, but its blue channel says it is a colour, not a letterbox.
    value = np.asarray(rgb).astype("float32").max(axis=2)
    h, w = value.shape
    box = _picture_box(value)
    if box is None:
        return frame                                  # dark throughout — not bars
    y0, y1, x0, x1 = box
    if box == (0, h, 0, w):
        return frame                                  # no bars at all
    out = out_dir / f"{label}.picture.png"
    rgb.crop((x0, y0, x1, y1)).save(out)
    log.info("[pattern_ref] trimmed letterbox off %s: rows %d-%d, cols %d-%d of %dx%d",
             frame.name, y0, y1, x0, x1, w, h)
    return out


def intro_colours(
    intro: Optional[Path], out_dir: Path, *, is_still: bool = False,
) -> tuple[str, str, bool, Optional[Path]]:
    """(ground, ink, fell_back, frame_used) dug out of the reel's intro.

    A video intro is read at its LAST frame that is still a fair picture of the
    card (rebrand_moments.pick_content_frame — cards fade to black, so the
    literal last frame is a black rectangle). A still intro is read directly.
    Brand colours with fell_back=True when there is no intro, the clip is dark
    from end to end, or the frame cannot be read at all: a pattern in 42's own
    colours is still the right thing to send, so this never raises."""
    if intro is None:
        return BRAND_GROUND, BRAND_INK, True, None
    intro = Path(intro)
    out_dir.mkdir(parents=True, exist_ok=True)
    label = "pattern_intro"
    try:
        if is_still or intro.suffix.lower() in _STILL_SUFFIXES:
            frame = intro
        else:
            picked = moments.pick_content_frame(intro, out_dir, edge="last", label=label)
            if picked.fell_back:
                log.info("[pattern_ref] intro %s is dark throughout — brand colours",
                         intro.name)
                return BRAND_GROUND, BRAND_INK, True, None
            frame = Path(picked.path)

        measured_from = _trim_letterbox(frame, out_dir, label)
        m = moments.measure(measured_from)
        if m.mean_lum < moments.MIN_MEAN_LUM:
            # Only reachable for a still (a video would have fallen back
            # above): a black card has no colour to inherit.
            log.info("[pattern_ref] intro %s is black — brand colours", intro.name)
            return BRAND_GROUND, BRAND_INK, True, None
        # Not m.palette: its top 6 bins are too shallow to find a gradient
        # background (see PALETTE_BINS), and it carries no border shares.
        ground, ink = ground_and_ink(intro_palette(measured_from))
    except Exception as e:  # noqa: BLE001 — a bad intro read degrades to brand colours
        log.warning("[pattern_ref] could not read intro colours from %s: %s",
                    intro, str(e)[:200])
        return BRAND_GROUND, BRAND_INK, True, None

    log.info("[pattern_ref] intro colours from %s: ground %s, ink %s (%.2f:1)",
             measured_from.name, ground, ink, contrast(ground, ink))
    return ground, ink, False, measured_from


# ---------------------------------------------------------------------------
# Pattern choice and colouring
# ---------------------------------------------------------------------------

def _mask_path(slug: str) -> Path:
    """The prepared kit's mask when this machine has one (it tracks the source
    art), else the copy shipped with the code (what production reads)."""
    # Both dirs are read at call time, so a test (or a relocated kit) can move them.
    kit = Path(pattern_kit.KIT_DIR) / f"{slug}.mask.png"
    if kit.exists():
        return kit
    return Path(BUNDLED_DIR) / f"{slug}.mask.png"


def _spec(slug: str) -> pattern_kit.PatternSpec:
    spec = next((s for s in pattern_kit.SPECS if s.slug == slug), None)
    if spec is None:
        raise ValueError(f"unknown pattern {slug!r}")
    return spec


def _available() -> list[str]:
    return [s.slug for s in pattern_kit.SPECS if _mask_path(s.slug).exists()]


def _ensure_masks() -> list[str]:
    """The slugs with a prepared mask (in the kit or in the bundled copy).
    Only when neither place has a single mask is the kit prepared from
    PATTERNS_DIR, which is a one-off (and slow: the sources are
    print-resolution)."""
    slugs = _available()
    if not slugs:
        log.info("[pattern_ref] no pattern masks in %s or %s — preparing the kit from %s",
                 pattern_kit.KIT_DIR, BUNDLED_DIR, PATTERNS_DIR)
        pattern_kit.build_kit(PATTERNS_DIR)
        slugs = _available()
    if not slugs:
        raise FileNotFoundError(
            f"no prepared pattern masks in {pattern_kit.KIT_DIR} or {BUNDLED_DIR}")
    return slugs


def pick_pattern(rng: Optional[random.Random] = None) -> str:
    """One pattern slug, uniformly at random over those with a prepared mask.
    Omni's left strip no longer uses this — it is always OMNI_SLUG."""
    return (rng or random.Random()).choice(_ensure_masks())


def canvas_for(aspect: str) -> tuple[int, int]:
    """(width, height) for a "W:H" aspect: the media module's canvas for the
    two aspects the reels use, and the same 720-short-edge rule for any other
    (clamped to 4:1 either way, so a typo cannot ask for a 7200px strip)."""
    if aspect in ("9:16", "16:9"):
        return media.dims_for(aspect)
    try:
        w_s, h_s = str(aspect).split(":", 1)
        ratio = float(w_s) / float(h_s)
        if not math.isfinite(ratio) or ratio <= 0:
            raise ValueError(aspect)
    except (ValueError, ZeroDivisionError):
        return media.dims_for(aspect)
    ratio = min(4.0, max(0.25, ratio))

    def even(v: float) -> int:
        return max(2, int(round(v / 2.0)) * 2)

    return (even(720 * ratio), 720) if ratio >= 1 else (720, even(720 / ratio))


def _alpha_at(
    src: Path, slug: str, width: int, *, rotate: int = 0, mirror: bool = False,
) -> "np.ndarray":
    """The pattern's mask as float alpha (0..1), `width` wide at its own
    aspect, turned `rotate` degrees clockwise (0/90/180/270) and mirrored if asked,
    with the levels that keep its strokes solid applied at THIS size."""
    import numpy as np
    from PIL import Image

    with Image.open(src) as im:
        alpha = im.convert("RGBA").getchannel("A")
    if slug in STROKE_MASS:
        alpha = _stroke_mass(alpha, *STROKE_MASS[slug])
    turn = {90: Image.Transpose.ROTATE_270,                   # clockwise
            180: Image.Transpose.ROTATE_180,
            270: Image.Transpose.ROTATE_90}.get(rotate % 360)     # counter-clockwise
    if turn is not None:
        alpha = alpha.transpose(turn)
    if mirror:
        alpha = alpha.transpose(Image.Transpose.FLIP_LEFT_RIGHT)
    mw, mh = alpha.size
    tile_h = max(1, round(mh * width / mw))
    tile = np.asarray(alpha.resize((width, tile_h), Image.LANCZOS)).astype("float32") / 255.0

    # Downscaling thins hairlines to partial alpha — the grid peaks at 0.55 at
    # 720 wide, a pattern Omni would read as a flat fill. Only such art (its
    # peak did not survive the resample) is re-levelled; the others already
    # reach full ink, and structure's partial alpha is shading to keep.
    # Levels, not a plain divide-by-peak: the peak is the grid's crosshair
    # dots, and scaling to them left the major lines — nearly all of what
    # makes it graph paper — at ~0.2, i.e. dots on a flat field. So the black
    # point is the median (the paper between lines, including the sheet's
    # faint noise floor) and the white point the 95th percentile (the major
    # lines, the top ~5-13% of pixels); the dots clip at full ink. Still a
    # one colour: only how opaque a pixel is moves.
    peak = float(np.percentile(tile, 99.9))
    if 0.05 < peak < 0.98:
        paper = float(np.percentile(tile, 50))
        line = float(np.percentile(tile, 95))
        if line - paper > 0.01:
            tile = np.clip((tile - paper) / (line - paper), 0.0, 1.0)
        else:
            tile = np.clip(tile / peak, 0.0, 1.0)
    levels = SOLID_LEVELS.get(slug)
    if levels:
        lo, hi = levels
        tile = np.clip((tile - lo) / (hi - lo), 0.0, 1.0)
    return tile


def _stroke_mass(alpha, sigma: float, lo: float, hi: float, min_blob: int):
    """`alpha` (a PIL "L" mask) with its strokes levelled by their mass
    (STROKE_MASS): blurred `sigma` px, levelled lo..hi, blobs under
    `min_blob` px dropped. Same size, still an "L" image. Float throughout —
    the levels are 4 grey levels apart, which an 8-bit blur would erase."""
    import cv2
    import numpy as np
    from PIL import Image

    a = cv2.GaussianBlur(np.asarray(alpha, dtype="float32") / 255.0, (0, 0), sigma)
    a = np.clip((a - lo) / (hi - lo), 0.0, 1.0)
    if min_blob > 0:
        _n, labels, stats, _c = cv2.connectedComponentsWithStats(
            (a > 0.02).astype("uint8"), connectivity=8)
        small = stats[:, cv2.CC_STAT_AREA] < min_blob
        small[0] = False                                   # label 0 is the empty paper
        a[small[labels]] = 0.0
    return Image.fromarray(np.clip(np.rint(a * 255.0), 0, 255).astype("uint8"), mode="L")


def _prior_cells(width: int, height: int):
    """(cell edges x, cell edges y, background-cell mask) for SPEAKER_PRIOR on
    this frame: a cell is background when the silhouette covers under half."""
    import numpy as np

    side = max(1, round(min(width, height) * FOCUS_CELL))
    xs = np.linspace(0, width, max(2, round(width / side)) + 1).round().astype(int)
    ys = np.linspace(0, height, max(2, round(height / side)) + 1).round().astype(int)
    prior = np.zeros((height, width), dtype="float32")
    for x0, x1, top in SPEAKER_PRIOR:
        prior[int(top * height):, int(x0 * width):int(x1 * width)] = 1.0
    cover = np.array([[prior[ys[j]:ys[j + 1], xs[i]:xs[i + 1]].mean() for i in range(len(xs) - 1)]
                      for j in range(len(ys) - 1)])
    return xs, ys, cover < 0.5


def _focus_crop(a, width: int, height: int) -> tuple[int, int]:
    """(x0, y0) of the width x height window of the composition `a` (float
    alpha, at least the frame's size) that best fills the frame around a
    centred speaker: the share of background cells with visible ink, times
    the left/right balance of that ink. A summed-area table keeps the
    21 x 21 scan under ~0.1 s. Deterministic — the same window for every
    frame of the pattern at this size, so every chunk of a clip (and every
    clip of a reel) shows the same part of the art."""
    import numpy as np

    th, tw = a.shape
    sat = np.pad((a > 0.1).astype("float64").cumsum(0).cumsum(1), ((1, 0), (1, 0)))
    xs, ys, bg = _prior_cells(width, height)
    left = (xs[:-1] + xs[1:]) / 2.0 < width / 2.0
    cell_area = np.outer(np.diff(ys), np.diff(xs)).astype("float64")
    best, best_at = -1.0, ((tw - width) // 2, (th - height) // 2)
    for fy in np.linspace(0.0, 1.0, FOCUS_STEPS if th > height else 1):
        y0 = int(round(fy * (th - height)))
        for fx in np.linspace(0.0, 1.0, FOCUS_STEPS if tw > width else 1):
            x0 = int(round(fx * (tw - width)))
            yy, xx = ys[:, None] + y0, xs[None, :] + x0
            ink = (sat[yy[1:], xx[:, 1:]] - sat[yy[:-1], xx[:, 1:]]
                   - sat[yy[1:], xx[:, :-1]] + sat[yy[:-1], xx[:, :-1]])
            filled = ((ink / cell_area) >= FOCUS_FILL) & bg
            fill = filled.sum() / max(1, bg.sum())
            lw, rw = (ink * bg)[:, left].sum(), (ink * bg)[:, ~left].sum()
            score = fill * (min(lw, rw) / max(lw, rw, 1e-9))
            if score > best + 1e-9:
                best, best_at = score, (x0, y0)
    return best_at


# A full-screen composition's art and where it is cropped, per size (the
# focus scan reads the whole art; a pattern moved by hand asks for both again
# to know how far its size must go to keep the frame covered).
_COVERS: dict[tuple, tuple] = {}
_COVERS_MAX = 2


def _cover_art(ref: "PatternRef", src: Path, width: int, height: int) -> tuple:
    """(alpha, x0, y0): a full-screen COMPOSITION (fit "cover") as ONE copy
    of its art just covering a width x height frame (x max(1, its scale)),
    and where the frame sits on it (_focus_crop). The art is float alpha,
    never written to."""
    from PIL import Image

    lay = ref.layout
    try:
        stamp = Path(src).stat().st_mtime_ns
    except OSError:
        stamp = 0
    key = (str(src), stamp, ref.slug, lay, width, height)
    hit = _COVERS.get(key)
    if hit is not None:
        return hit
    with Image.open(src) as im:
        mw, mh = im.size
    aspect = (mh / mw) if lay.rotate % 180 == 90 else (mw / mh)   # the turned art's width/height
    cover_w = max(width, height * aspect)
    a = _alpha_at(src, ref.slug, max(width, math.ceil(cover_w * max(1.0, lay.scale))),
                  rotate=lay.rotate)
    if a.shape[0] < height:                                        # rounding: never leave a gap
        a = _alpha_at(src, ref.slug, math.ceil(a.shape[1] * height / a.shape[0]) + 1,
                      rotate=lay.rotate)
    x0, y0 = _focus_crop(a, width, height)
    while len(_COVERS) >= _COVERS_MAX:
        _COVERS.pop(next(iter(_COVERS)))
    _COVERS[key] = (a, x0, y0)
    return a, x0, y0


def _full_alpha(ref: "PatternRef", src: Path, width: int, height: int):
    """A full-screen pattern as float alpha, width x height, as its layout's
    `fit` says (see Layout)."""
    import numpy as np
    from PIL import Image

    lay = ref.layout
    if lay.fit == "centre":
        a = _alpha_at(src, ref.slug, max(1, round(min(width, height) * lay.scale)), rotate=lay.rotate)
        h, w = a.shape
        ox, oy = (w // 2 - width // 2) % w, (h // 2 - height // 2) % h
        return np.tile(a, (-(-(height + oy) // h), -(-(width + ox) // w)))[oy:oy + height, ox:ox + width]
    if lay.fit == "cover":
        a, x0, y0 = _cover_art(ref, src, width, height)
        return a[y0:y0 + height, x0:x0 + width]
    # "tile": the art `scale` x the frame's width, repeated from the top-left.
    a = _alpha_at(src, ref.slug, max(1, round(width * lay.scale)), rotate=lay.rotate)
    h, w = a.shape
    return np.tile(a, (-(-height // h), -(-width // w)))[:height, :width]


def _mask_for(slug: str, mask: Optional[Path]) -> Path:
    """An explicit mask (an uploaded pattern), else the kit's by slug."""
    if mask is not None:
        src = Path(mask)
    else:
        _spec(slug)                               # unknown slug: ValueError, not a KeyError deep in PIL
        src = _mask_path(slug)
    if not src.exists():
        raise FileNotFoundError(f"pattern mask missing: {src}")
    return src


def colorize(
    slug: str, colour: str, out: Path, *, width: int, height: int, scale: float = 1.0,
    mask: Optional[Path] = None, rotate: int = 0, mirror: bool = False, valign: str = "top",
) -> Path:
    """The pattern as an RGBA PNG `height` tall: every pixel is `colour`, and
    the mask decides how opaque it is — solid on the pattern's lines, fully
    transparent where the pattern is empty. No background is drawn.

    The mask is scaled to `scale` x the canvas WIDTH and tiled downwards with
    no added gap — the art carries its own spacing — then the overflow is
    cropped (a turned art on a landscape canvas shows its denser end
    instead; see below). The image is as wide as the art at that scale (the
    canvas width at scale 1), so a bigger pattern is never cut to the
    canvas: the caller decides which part of it shows."""
    import numpy as np
    from PIL import Image

    src = _mask_for(slug, mask)
    if width <= 0 or height <= 0:
        raise ValueError(f"bad canvas {width}x{height}")
    landscape = width > height
    width = max(1, round(width * scale))
    tile = _alpha_at(src, slug, width, rotate=rotate, mirror=mirror)
    tile_h = tile.shape[0]

    if valign == "bottom":
        # The art's bottom on the canvas's bottom edge; whatever does not fit
        # runs off the top (Layout.valign).
        reps = -(-height // tile_h)
        col = np.tile(tile, (reps, 1))
        a = col[col.shape[0] - height:, :, None]
    elif landscape and rotate % 180 == 90 and tile_h > height:
        # A stood-up strip (5.png, 1.png) on a LANDSCAPE canvas. Its scale
        # is a multiple of the frame's width, worked out on 9:16, where the
        # frame shows 69-85% of the tile from the top. A 1280x720 frame shows
        # a quarter of a tile twice as big, and from the top that is 5.png's
        # bare hairline end (11% of its ink). So the frame shows the tile's
        # DENSER end instead — 1.png's is already at the top, 5.png's is
        # the bottom. Portrait canvases are never windowed this way.
        top, bottom = tile[:height], tile[tile_h - height:]
        a = (bottom if float(bottom.sum()) > float(top.sum()) else top)[:, :, None]
    else:
        reps = -(-height // tile_h)
        a = np.tile(tile, (reps, 1))[:height, :, None]

    rgba = np.empty((height, width, 4), dtype="uint8")
    rgba[:, :, :3] = np.array(_rgb(colour), dtype="uint8")
    rgba[:, :, 3] = np.clip(np.rint(a[:, :, 0] * 255.0), 0, 255).astype("uint8")
    img = Image.fromarray(rgba, mode="RGBA")

    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    img.save(out, format="PNG", optimize=True)
    return out


def strip_preview(ref: PatternRef, frame: Path, out: Path, *, person=None) -> Path:
    """`frame` exactly as Omni must hand it back: the pattern laid in from
    the left edge BEHIND the speaker, the footage showing between the lines,
    nothing else touched. An opaque RGB PNG.

    This, not the bare pattern, is what Omni is shown. Handed the pattern
    as a whole-frame lattice and told to fit it into a strip, Omni drew its
    own idea of a strip (a solid panel of bars); handed transparency, it drew
    a checkerboard. A finished frame leaves it nothing to interpret.

    The art is never cut to a strip. Structure is one whole composition —
    every bar ends in its own tip, nothing touches its border — and cutting
    a rectangle out of it left a ruler-straight edge through the middle of
    the bars (the owner: "looks cutted to the right side ... keep its
    regular shape"). So the WHOLE pattern slides left until its own
    right-most stroke lands OMNI_REACH_FRAC across the frame: on the right
    it ends where the art ends, and the only edge it crosses is the frame's
    own left edge, which it runs off like any bleed.

    `person` (a float HxW mask, 0..1 — person_matte.person_mask) puts the
    pattern behind the speaker: wherever the person is, the pattern is
    removed, slightly past their edge so no line clings to hair or sleeve.
    None lays the pattern over everything, as before. On a letterbox bar the
    mask is ignored (bar_mask) for every pattern but structure.

    The layout (ref.layout) decides where the pattern goes; the left one is
    the approved structure setup above, and the others follow the same rules
    — the art's own outline, bleeding only off the frame's edges."""
    from .rebrand_harness import compose

    return compose.compose_target(frame, out, [compose.pattern_layer(ref)], person=person)


def solid_held(solid) -> "np.ndarray":
    """The indices along a profile (each column's — or row's — share of
    pixels at >= half opacity) that hold solid ink: smoothed over 1/50 of its
    length, at least RUN_SOLID_FRAC of its most solid point. 1.png's
    thread-only start never gets there; its beads always do."""
    import numpy as np

    solid = np.asarray(solid, dtype="float64")
    k = max(3, len(solid) // 50)
    smooth = np.convolve(solid, np.ones(k) / k, mode="same")
    return np.nonzero(smooth >= RUN_SOLID_FRAC * max(float(smooth.max()), 1e-9))[0]


def _drop_sparse_tail(a, anchor: str):
    """A band's alpha without what lies past its last solid column at the
    end AWAY from its `anchor` corner (solid_held) — the far end, where a
    band's art is drawn to thin out."""
    held = solid_held((a >= 0.5).mean(axis=0))
    if not held.size:
        return a
    if anchor == "right":                                    # mirrored: the far end is on the left
        return a[:, int(held[0]):]
    return a[:, :int(held[-1]) + 1]


def _layer(ref: PatternRef, width: int, height: int, *,
           window: Optional[tuple[int, int, int, int]] = None):
    """The pattern alone, as a width x height RGBA layer in its layout's
    place (transparent everywhere else), or None when it has no ink.

    `window` (x0, y0, w, h) in FRAME pixels — it may reach past the frame —
    draws the same place over that window instead (a w x h layer): the art
    carries on past the frame's edges as it would if the frame were bigger
    (a strip tiles on down in its own phase and shows the columns the frame
    cut off; a band keeps its far end; a tiled background repeats; a
    composition shows the rest of its one copy, and nothing past it). At
    (0, 0, width, height) it is this layer, byte for byte. A pattern moved by
    hand is drawn from it (placed_layer), so a move never shows a straight
    cut where the frame used to end."""
    if window is not None:
        return _window_layer(ref, width, height, window)
    import numpy as np
    from PIL import Image

    lay = ref.layout
    if lay.placement in ("left", "right"):
        with Image.open(ref.path) as p:
            pat = p.convert("RGBA")
        if lay.placement == "right":
            # Mirrored, so the art's natural end faces the speaker and its
            # other side runs off the frame's right edge.
            pat = pat.transpose(Image.Transpose.FLIP_LEFT_RIGHT)
        if pat.height != height:
            # Same art, this frame's height; the pattern is wider than the frame
            # on purpose (OMNI_SCALE), so only the height is matched.
            pat = pat.resize((max(1, round(pat.width * height / pat.height)), height), Image.LANCZOS)
        inked = np.nonzero(np.asarray(pat.getchannel("A")).max(axis=0) > 0)[0]
        if not inked.size:
            return None
        reach = max(1, round(width * lay.reach))
        layer = Image.new("RGBA", (width, height), (0, 0, 0, 0))
        if lay.placement == "left":
            right = int(inked[-1]) + 1                       # one past the art's last stroke
            shift = max(0, right - reach)
            visible = pat.crop((shift, 0, min(pat.width, shift + width), height))
            layer.paste(visible, (0, 0))
        else:
            left = int(inked[0])                             # the art's first stroke
            x = width - reach - left                         # lands `reach` in from the right
            layer.paste(pat, (x, 0))
        return layer

    src = _mask_for(ref.slug, ref.mask)
    mirror = lay.placement in ("top", "bottom") and lay.anchor == "right"
    if lay.placement in ("top", "bottom"):
        band_w = max(1, round(width * lay.scale))
        a = _alpha_at(src, ref.slug, band_w, rotate=lay.rotate, mirror=mirror)
        max_h = max(1, round(height * lay.reach))
        if a.shape[0] > max_h:
            band_w = max(1, int(band_w * max_h / a.shape[0]))
            a = _alpha_at(src, ref.slug, band_w, rotate=lay.rotate, mirror=mirror)
            if a.shape[1] < width:
                # Shrunk to its height cap, the band now ENDS inside the
                # frame, so its far end — the one the layout's size was
                # chosen to keep off-frame (1.png's bare-thread start, a
                # quarter of it) — would show: 1.png's top band on 16:9 ran
                # four lone threads 260 px past its last bead. It ends at its
                # last solid column instead (only threads are cut there).
                a = _drop_sparse_tail(a, lay.anchor)
        if a.shape[1] > width:                               # a band wider than the frame
            a = a[:, :width] if lay.anchor == "left" else a[:, a.shape[1] - width:]
        h, w = a.shape
        y = 0 if lay.placement == "top" else height - h
        x = 0 if lay.anchor == "left" else width - w
        full = np.zeros((height, width), dtype="float32")
        full[y:y + h, x:x + w] = a[: height - y, : width - x]
    else:                                                    # full
        full = _full_alpha(ref, src, width, height)
    if not full.any():
        return None
    rgba = np.empty((height, width, 4), dtype="uint8")
    rgba[:, :, :3] = np.array(_rgb(ref.colour), dtype="uint8")
    rgba[:, :, 3] = np.clip(np.rint(full * 255.0), 0, 255).astype("uint8")
    return Image.fromarray(rgba, mode="RGBA")


# ---------------------------------------------------------------------------
# By hand: a pattern moved, sized or turned (the in-house engine)
# ---------------------------------------------------------------------------

# How far past the frame a pattern moved by hand is drawn, a share of each
# side: what the camera's pans and zoom-outs could bring into the frame (its
# push-in never needs any — the compositor's MARGIN).
HAND_MARGIN = 0.06
# The most art a moved pattern is drawn over, x the frame each way (shrunk to
# half and turned it needs about 2.9x): past it, nothing.
WINDOW_MAX = 3.0
# A full-screen composition (fit "cover") is ONE copy of its art: moved,
# shrunk or turned by hand, its size is raised — at most to COVER_SCALE_MAX,
# found in COVER_STEPS halvings — until the frame stays inside the art by
# COVER_EDGE px; past that its move is drawn back too. It always fills the
# frame.
COVER_SCALE_MAX = 8.0
COVER_STEPS = 12
COVER_EDGE = 1.0
# The studio's stand-in while a hand moves a pattern (/pattern/art): its art
# unmoved over the frame padded by ART_PAD of a frame on every side.
ART_PAD = 0.5


def hand_public(hand: Hand) -> dict:
    """A hand move as the preview, the evidence and Generate record it."""
    return {"dx": round(float(hand.dx), 4), "dy": round(float(hand.dy), 4),
            "scale": round(float(hand.scale), 4), "rotate": round(float(hand.rotate), 2),
            "pivot": [round(float(hand.pivot[0]), 4), round(float(hand.pivot[1]), 4)]}


def hand_matrix(hand: Hand, width: int, height: int) -> Optional["np.ndarray"]:
    """A hand move as a 2x3 matrix on frame pixels — p' = c + d + s·R(θ)·(p
    − c): turned (clockwise, y down) and sized about its pivot c, then moved
    by d — or None for none. lib/rebrandHand.ts mirrors it (a shared
    fixture holds the two together)."""
    import numpy as np

    if hand.identity:
        return None
    px, py = float(hand.pivot[0]) * width, float(hand.pivot[1]) * height
    th = math.radians(float(hand.rotate))
    c, s = math.cos(th) * float(hand.scale), math.sin(th) * float(hand.scale)
    return np.array([[c, -s, px + float(hand.dx) * width - (c * px - s * py)],
                     [s, c, py + float(hand.dy) * height - (s * px + c * py)]], dtype=np.float64)


def _unmapped(hand: Hand, width: int, height: int, pts) -> "np.ndarray":
    """Frame points (N x 2) taken back through the hand move: where on the
    unmoved art each of them comes from."""
    import numpy as np

    m = np.eye(3)
    m[:2] = hand_matrix(hand, width, height)
    inv = np.linalg.inv(m)
    p = np.asarray(pts, dtype=np.float64)
    return p @ inv[:2, :2].T + inv[:2, 2]


def covers_frame(hand: Hand, rect: tuple[float, float, float, float], width: int, height: int) -> bool:
    """Whether a one-copy art whose outline is `rect` (x0, y0, x1, y1, frame
    px, unmoved) still covers the whole frame once moved by `hand`."""
    x0, y0, x1, y1 = rect
    back = _unmapped(hand, width, height, [(0, 0), (width, 0), (width, height), (0, height)])
    e = COVER_EDGE
    return bool(((back[:, 0] >= x0 + e) & (back[:, 0] <= x1 - e)
                 & (back[:, 1] >= y0 + e) & (back[:, 1] <= y1 - e)).all())


def clamp_cover(hand: Hand, rect: tuple[float, float, float, float], width: int, height: int) -> Hand:
    """`hand` made to keep the frame inside a one-copy art (outline `rect`):
    as it is when it already does; else the smallest size (from its own up
    to COVER_SCALE_MAX, COVER_STEPS halvings, rounded UP to 1/10000) that
    does; else the largest size and as much of its move as still does.
    lib/rebrandHand.ts mirrors it (the studio's zoom never goes below it)."""
    import dataclasses

    if hand.identity or covers_frame(hand, rect, width, height):
        return hand
    lo, hi = float(hand.scale), max(float(hand.scale), COVER_SCALE_MAX)
    if covers_frame(dataclasses.replace(hand, scale=hi), rect, width, height):
        for _ in range(COVER_STEPS):
            mid = (lo + hi) / 2.0
            if covers_frame(dataclasses.replace(hand, scale=mid), rect, width, height):
                hi = mid
            else:
                lo = mid
        up = math.ceil(hi * 1e4) / 1e4
        return dataclasses.replace(hand, scale=min(up, max(float(hand.scale), COVER_SCALE_MAX)))
    keep_lo, keep_hi = 0.0, 1.0
    for _ in range(COVER_STEPS):
        mid = (keep_lo + keep_hi) / 2.0
        if covers_frame(dataclasses.replace(hand, scale=hi, dx=hand.dx * mid, dy=hand.dy * mid),
                        rect, width, height):
            keep_lo = mid
        else:
            keep_hi = mid
    return dataclasses.replace(hand, scale=hi, dx=round(hand.dx * keep_lo, 4), dy=round(hand.dy * keep_lo, 4))


def cover_rect(ref: PatternRef, width: int, height: int) -> Optional[tuple[float, float, float, float]]:
    """A full-screen composition's own outline (x0, y0, x1, y1) in frame px,
    unmoved — the one copy of its art the frame is cropped from; None for
    every other pattern (a strip, a band, a tiled background)."""
    lay = ref.layout
    if lay.placement != "full" or lay.fit != "cover":
        return None
    a, cx0, cy0 = _cover_art(ref, _mask_for(ref.slug, ref.mask), width, height)
    return (float(-cx0), float(-cy0), float(a.shape[1] - cx0), float(a.shape[0] - cy0))


def drawn_hand(ref: PatternRef, width: int, height: int) -> Hand:
    """ref.hand as it is drawn: a full-screen composition's clamped so it
    always fills the frame (clamp_cover); every other one as it is."""
    hand = ref.hand
    if hand.identity:
        return hand
    rect = cover_rect(ref, width, height)
    return hand if rect is None else clamp_cover(hand, rect, width, height)


def _strip_window(ref: PatternRef, width: int, height: int, xs, ys):
    """A strip's alpha over window columns `xs` and rows `ys` (frame px):
    (uint8 rows x covered columns, which of `xs` the art covers), or (None,
    None) when it has no ink. The art as colorize draws it — `scale` x the
    width, tiled down in colorize's own phase (valign, a stood-up strip on a
    landscape frame) — laid where _layer lays it: its last stroke `reach`
    across (a right strip mirrored)."""
    import numpy as np

    lay = ref.layout
    src = _mask_for(ref.slug, ref.mask)
    aw = max(1, round(width * lay.scale))
    tile = _alpha_at(src, ref.slug, aw, rotate=lay.rotate)
    th = tile.shape[0]
    if lay.valign == "bottom":
        phase = (-height) % th
    elif width > height and lay.rotate % 180 == 90 and th > height:
        top, bottom = tile[:height], tile[th - height:]
        phase = (th - height) if float(bottom.sum()) > float(top.sum()) else 0
    else:
        phase = 0
    tile8 = np.clip(np.rint(tile * 255.0), 0, 255).astype("uint8")
    del tile
    if lay.placement == "right":
        tile8 = tile8[:, ::-1]
    inked = np.nonzero(tile8[(np.arange(height) + phase) % th].max(axis=0) > 0)[0]
    if not inked.size:
        return None, None
    reach = max(1, round(width * lay.reach))
    if lay.placement == "left":
        cols = np.asarray(xs) + max(0, int(inked[-1]) + 1 - reach)
    else:
        cols = np.asarray(xs) - (width - reach - int(inked[0]))
    ok = (cols >= 0) & (cols < aw)
    rows = (np.asarray(ys) + phase) % th
    return tile8[rows][:, cols[ok]], ok


def _window_alpha(ref: PatternRef, width: int, height: int, xs, ys):
    """A band's or a full background's alpha (float, rows x columns) over
    window columns `xs` and rows `ys` (frame px), placed as _layer places it
    — without its cut at the frame's edges."""
    import numpy as np

    lay = ref.layout
    src = _mask_for(ref.slug, ref.mask)
    xs, ys = np.asarray(xs), np.asarray(ys)
    out = np.zeros((len(ys), len(xs)), dtype="float32")
    if lay.placement in ("top", "bottom"):
        mirror = lay.anchor == "right"
        band_w = max(1, round(width * lay.scale))
        a = _alpha_at(src, ref.slug, band_w, rotate=lay.rotate, mirror=mirror)
        max_h = max(1, round(height * lay.reach))
        if a.shape[0] > max_h:
            band_w = max(1, int(band_w * max_h / a.shape[0]))
            a = _alpha_at(src, ref.slug, band_w, rotate=lay.rotate, mirror=mirror)
            if a.shape[1] < width:
                a = _drop_sparse_tail(a, lay.anchor)
        bh, bw = a.shape
        top = 0 if lay.placement == "top" else height - bh
        left = 0 if lay.anchor == "left" else width - bw
        r, c = ys - top, xs - left
        okr, okc = (r >= 0) & (r < bh), (c >= 0) & (c < bw)
        if okr.any() and okc.any():
            out[np.ix_(okr, okc)] = a[np.ix_(r[okr], c[okc])]
        return out
    if lay.fit == "cover":
        a, cx0, cy0 = _cover_art(ref, src, width, height)
        r, c = ys + cy0, xs + cx0
        okr, okc = (r >= 0) & (r < a.shape[0]), (c >= 0) & (c < a.shape[1])
        if okr.any() and okc.any():
            out[np.ix_(okr, okc)] = a[np.ix_(r[okr], c[okc])]
        return out
    if lay.fit == "centre":
        a = _alpha_at(src, ref.slug, max(1, round(min(width, height) * lay.scale)), rotate=lay.rotate)
        h, w = a.shape
        ox, oy = (w // 2 - width // 2) % w, (h // 2 - height // 2) % h
        return a[np.ix_((ys + oy) % h, (xs + ox) % w)]
    a = _alpha_at(src, ref.slug, max(1, round(width * lay.scale)), rotate=lay.rotate)
    h, w = a.shape
    return a[np.ix_(ys % h, xs % w)]


def _window_layer(ref: PatternRef, width: int, height: int, window: tuple[int, int, int, int]):
    """_layer over `window` (see _layer)."""
    import numpy as np
    from PIL import Image

    x0, y0, w, h = (int(v) for v in window)
    if w <= 0 or h <= 0:
        return None
    xs, ys = np.arange(x0, x0 + w), np.arange(y0, y0 + h)
    rgba = np.zeros((h, w, 4), dtype="uint8")
    colour = np.array(_rgb(ref.colour), dtype="uint8")
    if ref.layout.placement in ("left", "right"):
        alpha, ok = _strip_window(ref, width, height, xs, ys)
        if alpha is None:
            return None
        # As _layer pastes the colorized art: its colour where the art is,
        # nothing (0, 0, 0, 0) where it is not.
        rgba[:, ok, :3] = colour
        rgba[:, ok, 3] = alpha
    else:
        rgba[:, :, :3] = colour
        rgba[:, :, 3] = np.clip(np.rint(_window_alpha(ref, width, height, xs, ys) * 255.0), 0, 255
                                ).astype("uint8")
    if not rgba[:, :, 3].any():
        return None
    return Image.fromarray(rgba, mode="RGBA")


@dataclass(frozen=True, eq=False)
class Windowed:
    """A pattern as the in-house plate draws it (house.layer_spec): its art
    UNMOVED — its layout's place, drawn over a window of frame pixels big
    enough for its hand move (straight RGBA uint8, cut to its ink) —, where
    the frame's origin is on that art (art px: a frame pixel p is art pixel
    p + origin), and the hand move (a 2x3 matrix on frame px, None: none)
    with the Hand it was made from (drawn_hand: a full-screen composition's
    clamped). The plate warps it ONCE, camera x hand x origin, so a thin
    line is resampled a single time."""
    art: Any
    origin: tuple[int, int]
    matrix: Any
    hand: Hand


def _hand_window(hand: Hand, width: int, height: int, margin: float) -> tuple[int, int, int, int]:
    """The window (x0, y0, w, h) of unmoved art a hand move needs: the frame
    and `margin` of it each side, taken back through the move — two pixels
    more for the resampling — at most WINDOW_MAX x the frame, about its
    middle."""
    import numpy as np

    mw, mh = margin * width, margin * height
    back = _unmapped(hand, width, height, [(-mw, -mh), (width + mw, -mh), (width + mw, height + mh),
                                           (-mw, height + mh)])
    x0, y0 = int(math.floor(back[:, 0].min())) - 2, int(math.floor(back[:, 1].min())) - 2
    x1, y1 = int(math.ceil(back[:, 0].max())) + 2, int(math.ceil(back[:, 1].max())) + 2
    mid = _unmapped(hand, width, height, [(width / 2.0, height / 2.0)])[0]
    cap_w, cap_h = int(WINDOW_MAX * width), int(WINDOW_MAX * height)
    if x1 - x0 > cap_w:
        x0, x1 = int(np.floor(mid[0] - cap_w / 2.0)), int(np.floor(mid[0] - cap_w / 2.0)) + cap_w
    if y1 - y0 > cap_h:
        y0, y1 = int(np.floor(mid[1] - cap_h / 2.0)), int(np.floor(mid[1] - cap_h / 2.0)) + cap_h
    return x0, y0, x1 - x0, y1 - y0


# Windowed layers made in this process, the most recent last: a studio
# preview, its design still and a moment all ask for the same one.
_WINDOWED: dict[tuple, Optional[Windowed]] = {}
_WINDOWED_MAX = 6


def windowed(ref: PatternRef, width: int, height: int, *, margin: float = 0.0) -> Optional[Windowed]:
    """The pattern as the plate draws it (Windowed), or None when it has no
    ink. With no hand move and no margin it is _layer itself, at the frame's
    origin (the very pixels every version before hand moves was drawn
    from); with a margin, _layer over the frame and that margin each side;
    moved by hand, the unmoved art over the window the move needs
    (_hand_window: the frame and `margin` taken back through it), cut to its
    ink. Cached (the last _WINDOWED_MAX). Synchronous."""
    import numpy as np

    hand = drawn_hand(ref, width, height)
    key = (str(ref.path), ref.slug, ref.layout, ref.colour, str(ref.mask), hand, int(width), int(height),
           round(float(margin), 6))
    if key in _WINDOWED:
        got = _WINDOWED.pop(key)
        _WINDOWED[key] = got
        return got
    if hand.identity:
        mw, mh = int(round(margin * width)), int(round(margin * height))
        img = (_layer(ref, width, height) if not (mw or mh)
               else _layer(ref, width, height, window=(-mw, -mh, width + 2 * mw, height + 2 * mh)))
        out = None if img is None else Windowed(np.asarray(img.convert("RGBA"), dtype=np.uint8), (mw, mh),
                                                None, hand)
    else:
        x0, y0, w, h = _hand_window(hand, width, height, margin)
        img = _layer(ref, width, height, window=(x0, y0, w, h))
        out = None
        if img is not None:
            art = np.asarray(img, dtype=np.uint8)
            a = art[..., 3]
            rows, cols = np.flatnonzero(a.any(axis=1)), np.flatnonzero(a.any(axis=0))
            art = np.ascontiguousarray(art[rows[0]:rows[-1] + 1, cols[0]:cols[-1] + 1])
            out = Windowed(art, (-(x0 + int(cols[0])), -(y0 + int(rows[0]))), hand_matrix(hand, width, height),
                           hand)
    while len(_WINDOWED) >= _WINDOWED_MAX:
        _WINDOWED.pop(next(iter(_WINDOWED)))
    _WINDOWED[key] = out
    return out


def placed_layer(ref: PatternRef, width: int, height: int, *, margin: float = 0.0):
    """The pattern in its place WITH its hand move: an RGBA layer of (W +
    2·mW) x (H + 2·mH), the frame's origin at (mW, mH) (mW = margin x W), or
    None when none of it shows. No hand move and no margin: _layer, byte for
    byte (every pattern before hand moves). Moved by hand: the plate's own
    art for it (windowed, with HAND_MARGIN) warped by the plate's own one
    warp (plate._warp_over, INTER_LINEAR) — so its alpha is the alpha the
    in-house plate draws on frame 0 — in its one colour.

    Everything that reads where a pattern is (the preview's target and
    alphas, the ink the others keep off, the icons' room) reads this, so a
    move is seen by all of them at once. Synchronous."""
    import numpy as np
    from PIL import Image

    hand = drawn_hand(ref, width, height)
    if hand.identity:
        if margin == 0:
            return _layer(ref, width, height)
        got = windowed(ref, width, height, margin=margin)
        return None if got is None else Image.fromarray(got.art.copy(), mode="RGBA")
    from .rebrand_harness import plate as pl

    got = windowed(ref, width, height, margin=max(float(margin), HAND_MARGIN))
    if got is None:
        return None
    mw, mh = int(round(margin * width)), int(round(margin * height))
    box = pl.ink_box(got.art[..., 3])
    acc = np.zeros((height + 2 * mh, width + 2 * mw, 4), dtype=np.float32)
    m = pl._shift(mw, mh) @ pl._m3(got.matrix) @ pl._shift(-got.origin[0], -got.origin[1])
    if box is None or pl._warp_over(acc, pl.premultiplied(got.art), m, box) is None:
        return None
    alpha = np.clip(np.rint(acc[..., 3]), 0, 255).astype(np.uint8)
    if not alpha.any():
        return None
    rgba = np.empty(alpha.shape + (4,), dtype=np.uint8)
    rgba[..., :3] = np.array(_rgb(ref.colour), dtype=np.uint8)
    rgba[..., 3] = alpha
    return Image.fromarray(rgba, mode="RGBA")


def art_layer(ref: PatternRef, width: int, height: int):
    """(the pattern UNMOVED and uncut over the frame padded by ART_PAD of a
    frame each side, cut to its ink, as an RGBA image; that window (x, y, w,
    h) in frame px) — or None with no ink. The studio's stand-in draws a
    pattern a hand is moving from it, before the exact frame lands."""
    import numpy as np
    from PIL import Image

    pw, ph = int(round(ART_PAD * width)), int(round(ART_PAD * height))
    img = _layer(ref, width, height, window=(-pw, -ph, width + 2 * pw, height + 2 * ph))
    if img is None:
        return None
    art = np.asarray(img, dtype=np.uint8)
    a = art[..., 3]
    rows, cols = np.flatnonzero(a.any(axis=1)), np.flatnonzero(a.any(axis=0))
    cut = np.ascontiguousarray(art[rows[0]:rows[-1] + 1, cols[0]:cols[-1] + 1])
    return (Image.fromarray(cut, mode="RGBA"),
            (int(cols[0]) - pw, int(rows[0]) - ph, int(cols[-1] - cols[0] + 1), int(rows[-1] - rows[0] + 1)))


@dataclass(frozen=True)
class Colours:
    """Everything build() digs out of the intro, dug once. An auto-placed
    pattern needs its colour BEFORE it is rendered — the planner scores how
    well each candidate place reads in that colour — so the pipeline digs
    them first (dig_colours) and hands the same record to build()."""
    ground: str
    ink: str
    fell_back: bool
    frame: Optional[Path]
    colour: str


def dig_colours(intro: Optional[Path], out_dir: Path, *, is_still: bool = False) -> Colours:
    """The intro's ground and ink (intro_colours) and the one colour the
    pattern is drawn in (pattern_colour). Never raises (see intro_colours)."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    ground, ink, fell_back, frame = intro_colours(intro, out_dir, is_still=is_still)
    colour = ground
    if frame is not None:
        try:
            colour = pattern_colour(intro_palette(frame))
        except Exception as e:  # noqa: BLE001 — the measured ground is still a fair colour
            log.warning("[pattern_ref] pattern colour from %s failed (%s) — using the ground",
                        frame.name, str(e)[:200])
    return Colours(ground=ground, ink=ink, fell_back=fell_back, frame=frame, colour=colour)


def build(
    intro: Optional[Path], out_dir: Path, *, aspect: str, is_still: bool = False,
    slug: str = OMNI_SLUG, scale: float = OMNI_SCALE,
    layout: Optional[Layout] = None, mask: Optional[Path] = None,
    animation: str = "left_to_right", name: str = "", source_file: Optional[str] = None,
    colours: Optional[Colours] = None,
) -> PatternRef:
    """Dig the intro's colours and render Omni's reference from `slug` — the
    left-strip pattern unless told otherwise. A pattern_library entry passes
    its own layout, mask (uploads), animation, name and source file;
    `colours`, when already dug (dig_colours), are used instead of reading
    the intro a second time.

    Synchronous — callers wrap it in a thread. Raises only when no pattern can
    be rendered at all (no kit and no sources); the pipeline treats that as
    "Omni runs without a reference", never as a failed experiment."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    layout = layout or Layout(scale=scale)
    if mask is None:
        _spec(slug)
        if not _mask_path(slug).exists():
            _ensure_masks()
    dug = colours or dig_colours(intro, out_dir, is_still=is_still)
    ground, ink, fell_back, frame, colour = dug.ground, dug.ink, dug.fell_back, dug.frame, dug.colour
    width, height = canvas_for(aspect)
    out = out_dir / f"omni_pattern_{slug}.png"
    ref = PatternRef(
        slug=slug, source=source_file or _spec(slug).source, path=out, ground=ground, ink=ink,
        fell_back=fell_back, intro_frame=frame, colour=colour, where=where_text(layout),
        layout=layout, animation=animation, name=name, mask=mask,
    )
    if layout.placement in ("left", "right"):
        colorize(slug, colour, out, width=width, height=height, scale=layout.scale,
                 mask=mask, rotate=layout.rotate, valign=layout.valign)
    else:
        # A band or a full background: the image the brief model sees is the
        # pattern in its real place on a transparent frame.
        layer = _layer(ref, width, height)
        if layer is None:
            raise ValueError(f"pattern {slug} has no ink at this size")
        layer.save(out, format="PNG", optimize=True)
    path = out
    log.info("[pattern_ref] %s (%s) %s on transparent (intro ground %s)%s -> %s", slug,
             ref.source, colour, ground, " [brand fallback]" if fell_back else "", path.name)
    return ref
