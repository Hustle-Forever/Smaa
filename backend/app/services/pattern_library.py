"""The Rebrand pattern library — every pattern Omni can lay behind the speaker.

One place answers "which patterns exist, what are they, where do they go and
how do they come in", so the pipeline, the brief and the UI never special-case
a pattern:

  built-in   the five print files in patterns/ (pattern_kit prepares their
             masks), each with the place the owner gave it (2026-09-25/26):
               2.png structure    the LEFT half, big, behind the speaker —
                                  the approved setup, tagged
                                  checkpoint-omni-pattern-behind-speaker
               5.png beads_field  turned 90° counter-clockwise and used like
                                  2.png: the left half, behind the speaker
               3.png blueprint    the whole background, ONE copy cropped
                                  around the speaker — a transition: added
                                  to a scene it shows 1.5 s at its start
                                  (Transition), not the whole scene
               4.png grid         the whole background, a solid grid centred
                                  on the speaker
               1.png beads_row    AUTO: "can be basically anywhere ... make
                                  it random but calculated" — six candidate
                                  places (side strips, top/bottom bands from
                                  a corner), one chosen per clip by
                                  pattern_planner
  uploaded   any image a user adds. It is keyed (alpha / ink on light / light
             on dark), turned into a mask exactly like the built-ins, looked
             at by Gemini (what it is, which places it may take, how it
             should enter) and embedded, and stored in yas_rebrand_patterns —
             the mask bytes too, because the database is shared by every
             machine and the VPS, and a pattern uploaded on one must render
             on all. Its layouts are CALCULATED from its measured art
             (fit_layouts) for each place it may take: one place = fixed,
             several = auto-placed per clip like 1.png.

ANIMATIONS is the catalogue of entrances. Each is the sentence Omni is given
for the first second of the clip; "left_to_right" on the left layout is the
approved text, word for word. Our own renderer (the in-house engine) draws
ENTRANCES (those, plus a slide) and the EXITS a pattern goes out with at the
end of its span.
"""
from __future__ import annotations

import asyncio
import io
import logging
import math
import re
import secrets
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional

from ..config import config
from . import pattern_kit
from . import rebrand_pattern_ref as ref
from .rebrand_pattern_ref import Layout

log = logging.getLogger("archivist.rebrand.patterns")

DEFAULT_SLUG = ref.OMNI_SLUG

# Rendered masks of uploaded patterns and every pattern's preview, cached on
# this machine (the database holds the source of truth for uploads).
CACHE_DIR = (config.asset_root.parent / "branding" / "patterns" / "library").resolve()
PREVIEW_EDGE = 480
MAX_UPLOAD_BYTES = 40 * 1024 * 1024
MAX_UPLOAD_PIXELS = 200_000_000

# ---------------------------------------------------------------------------
# Entrances
# ---------------------------------------------------------------------------

_NOTHING_ELSE = (
    "Nothing else is part of the entrance: no panel, card, frosted-glass sheet, white or grey "
    "shape, blur, haze, glow or light sweep appears at any moment, and the room stays exactly "
    "as shot the whole time."
)
_START = "ENTRANCE: at the very first frame there is no pattern yet, just the original video. "
_HOLD = "then they hold still, exactly as in the attached image, for the rest of the clip. "


def _sweep(direction: str, start: str, travel: str) -> str:
    return (
        _START
        + f"Over the first second the pattern's own lines fade in smoothly from {direction}: "
        + f"the lines themselves become visible progressively, starting at {start} and "
        + f"travelling {travel}, passing BEHIND the speaker; " + _HOLD + _NOTHING_ELSE
    )


# value -> (label, help, prompt). The prompt is None for "none": no entrance.
ANIMATIONS: dict[str, tuple[str, str, Optional[str]]] = {
    "left_to_right": (
        "Left to right",
        "The lines fade in from the left edge towards the middle, passing behind the speaker.",
        _sweep("LEFT to RIGHT", "the pattern's left end", "to the right"),
    ),
    "right_to_left": (
        "Right to left",
        "The lines fade in from the right, travelling left.",
        _sweep("RIGHT to LEFT", "the pattern's right end", "to the left"),
    ),
    "top_to_bottom": (
        "Top to bottom",
        "The lines fade in from the top, travelling down.",
        _sweep("TOP to BOTTOM", "the top of the pattern", "down"),
    ),
    "bottom_to_top": (
        "Bottom to top",
        "The lines fade in from the bottom, travelling up.",
        _sweep("BOTTOM to TOP", "the bottom of the pattern", "up"),
    ),
    "fade": (
        "Fade in",
        "All of the lines fade in together.",
        _START
        + "Over the first second ALL of the pattern's own lines fade in together, smoothly, "
        + "from invisible to full strength, BEHIND the speaker; " + _HOLD + _NOTHING_ELSE,
    ),
    "glitch": (
        "Glitch in",
        "The lines flicker on in short digital bursts, then snap into place.",
        _START
        + "Over the first second the pattern's own lines glitch in: they flicker on in short "
        + "digital bursts, thin horizontal slices of the lines jumping a few pixels sideways and "
        + "briefly splitting into offset copies in the pattern's colour, BEHIND the speaker, "
        + "then snap into place; " + _HOLD + _NOTHING_ELSE,
    ),
    "none": (
        "No animation",
        "The pattern is there from the first frame.",
        None,
    ),
}

# The approved entrance for the approved layout, word for word.
_LEFT_TO_RIGHT_LEFT = (
    _START
    + "Over the first second the pattern's own lines fade in smoothly from LEFT to RIGHT: "
    + "the lines themselves become visible progressively, starting at the frame's left edge "
    + "and travelling towards the middle, passing BEHIND the speaker; then they hold still, "
    + "exactly as in the attached image, for the rest of the clip. " + _NOTHING_ELSE
)


def normalize_animation(value: Any) -> Optional[str]:
    v = str(value or "").strip().lower()
    return v if v in ANIMATIONS else None


def entrance_prompt(animation: str, layout: Layout) -> Optional[str]:
    """The ENTRANCE sentence for Omni's first chunk, or None (no entrance)."""
    if animation == "left_to_right" and layout.placement == "left":
        return _LEFT_TO_RIGHT_LEFT
    label_help_prompt = ANIMATIONS.get(animation) or ANIMATIONS["left_to_right"]
    return label_help_prompt[2]


# The same entrances as one clause each, for a prompt that names several
# patterns ("the first pattern <phrase>; the second pattern <phrase>"):
# prompt.patterns_clause. None for "none" (that pattern is there from the
# first frame). No colour, no number: the image carries both.
ENTER_PHRASE: dict[str, Optional[str]] = {
    "left_to_right": ("fades in smoothly from LEFT to RIGHT, its own lines becoming visible "
                      "progressively from its left end"),
    "right_to_left": ("fades in smoothly from RIGHT to LEFT, its own lines becoming visible "
                      "progressively from its right end"),
    "top_to_bottom": ("fades in smoothly from TOP to BOTTOM, its own lines becoming visible "
                      "progressively from its top"),
    "bottom_to_top": ("fades in smoothly from BOTTOM to TOP, its own lines becoming visible "
                      "progressively from its bottom"),
    "fade": "fades in all at once, every one of its lines together, from invisible to full strength",
    "glitch": ("glitches in, its lines flickering on in short digital bursts in its own colour, "
               "then snapping into place"),
    "none": None,
}


def entrance_phrase(animation: str) -> Optional[str]:
    """One pattern's entrance as a clause (ENTER_PHRASE), None for none."""
    if animation not in ENTER_PHRASE:
        animation = "left_to_right"
    return ENTER_PHRASE[animation]


def animations_public() -> list[dict]:
    return [{"value": k, "label": v[0], "help": v[1]} for k, v in ANIMATIONS.items()]


# Our own renderer (the in-house engine) draws every entrance Omni is told,
# plus "slide": the whole pattern slides in from the edge it grows from.
# value -> (label, help). recipe.PATTERN_ENTRANCES is these + "auto".
ENTRANCES: dict[str, tuple[str, str]] = {
    **{k: (v[0], v[1]) for k, v in ANIMATIONS.items()},
    "slide": ("Slide in", "The whole pattern slides in from the edge it grows from."),
}

# How a pattern goes out at the end of its span (the in-house engine only).
# value -> (label, help). recipe.PATTERN_EXITS is exactly these, in order.
EXITS: dict[str, tuple[str, str]] = {
    "auto": ("Its own", "Its entrance played backwards — or none when it lasts to the end of the scene."),
    "none": ("None", "It holds to the cut."),
    "fade": ("Fade out", "All of the lines fade out together."),
    "glitch": ("Glitch out", "The lines flicker off in short digital bursts."),
    "to_left": ("Back to the left", "The lines vanish towards the left edge."),
    "to_right": ("Back to the right", "The lines vanish towards the right edge."),
    "to_top": ("Back to the top", "The lines vanish towards the top."),
    "to_bottom": ("Back to the bottom", "The lines vanish towards the bottom."),
    "slide_out": ("Slide out", "The whole pattern slides out to the edge it grew from."),
}

# The exit that plays an entrance backwards ("auto" on a pattern that ends
# before the scene does): a sweep in from the left goes back to the left.
EXIT_OF_ENTRANCE: dict[str, str] = {
    "left_to_right": "to_left", "right_to_left": "to_right",
    "top_to_bottom": "to_top", "bottom_to_top": "to_bottom",
    "fade": "fade", "glitch": "glitch", "none": "none", "slide": "slide_out",
}


def entrances_public() -> list[dict]:
    return [{"value": k, "label": v[0], "help": v[1]} for k, v in ENTRANCES.items()]


def exits_public() -> list[dict]:
    return [{"value": k, "label": v[0], "help": v[1]} for k, v in EXITS.items()]


@dataclass(frozen=True)
class Transition:
    """A full-screen pattern used as a transition (the owner, 2026-09-29:
    "3.png blueprint = a full-screen transition ONLY"): added to a scene it
    shows from the scene's start for `sec` seconds, in over `in_sec`, out
    over `out_sec` — the studio writes that span into the recipe (a recipe
    with no span still shows it the whole scene, as before)."""
    sec: float
    in_sec: float
    out_sec: float

    def public(self) -> dict:
        return {"sec": self.sec, "in_sec": self.in_sec, "out_sec": self.out_sec}


# Blueprint's: 1.5 s at the scene's start, 0.3 s in, 0.5 s out (patterns_ui
# §6 question 3, the owner's default).
BLUEPRINT_TRANSITION = Transition(sec=1.5, in_sec=0.3, out_sec=0.5)


def _anchored_side(layout: Layout) -> Optional[str]:
    """The frame edge a layout grows from sideways: a strip's own side, a
    band's corner; None for the whole background."""
    if layout.placement in ("left", "right"):
        return layout.placement
    if layout.placement in ("top", "bottom"):
        return layout.anchor
    return None


def aimed_animation(animation: str, layout: Layout) -> str:
    """A pattern's entrance, turned to start where its layout starts: a
    sideways sweep enters from the edge the pattern grows from, so 1.png
    placed as a right strip or from a top-RIGHT corner sweeps in right to
    left, and as a left strip keeps the approved left-to-right text. Any
    other entrance is direction-free and kept as is. Used for the drawn
    place of an auto-placed pattern and the one place of a fixed one, only
    when the run did not pick an animation itself."""
    side = _anchored_side(layout)
    if animation == "left_to_right" and side == "right":
        return "right_to_left"
    if animation == "right_to_left" and side == "left":
        return "left_to_right"
    return animation


# ---------------------------------------------------------------------------
# Entries
# ---------------------------------------------------------------------------

def auto_where(candidates: Iterable[Layout]) -> str:
    """The sentence for an auto-placed pattern: which places it may take.
    The run's own place (where_text of the chosen layout) is what Omni and
    the brief are told; this is only for people choosing a pattern."""
    places = {c.placement for c in candidates}
    parts = []
    bands = [p for p in ("top", "bottom") if p in places]
    sides = [p for p in ("left", "right") if p in places]
    if bands:
        parts.append(f"a band along the {' or '.join(bands)} edge")
    if sides:
        parts.append(f"a strip down the {' or '.join(sides)} side")
    if "full" in places:
        parts.append("the whole background")
    return ("placed for each clip where it fits best — " + ", or ".join(parts)
            + " — behind the speaker")


@dataclass
class PatternEntry:
    """One pattern and where it may go. `candidates` are the layouts it can
    take: ONE is a fixed place (every built-in but 1.png, and an upload
    allowed a single place); several are auto-placed — pattern_planner picks
    one per clip from the clip's own frame. `layout` is always the first
    candidate: the default, used when there is no frame to plan on or
    nothing fits, and what callers that know nothing of candidates read."""
    slug: str
    name: str
    source: str                          # "builtin" | "upload"
    description: str
    layout: Layout
    default_animation: str
    source_file: Optional[str] = None
    created_at: Optional[str] = None
    analysis: dict = field(default_factory=dict)
    mask_bytes: Optional[bytes] = None   # uploads only (from the database)
    candidates: tuple[Layout, ...] = ()
    # A full-screen transition (TRANSITIONS): added to a scene, it shows for
    # this span at the scene's start, not the whole scene. None: the whole
    # scene, as every pattern did before spans.
    transition: Optional["Transition"] = None

    def __post_init__(self) -> None:
        self.candidates = tuple(self.candidates) or (self.layout,)
        self.layout = self.candidates[0]

    def set_candidates(self, candidates: Iterable[Layout]) -> None:
        self.candidates = tuple(candidates) or (self.layout,)
        self.layout = self.candidates[0]

    @property
    def auto(self) -> bool:
        return len(self.candidates) > 1

    def mask_path(self) -> Optional[Path]:
        """The mask to render from: None for a built-in (the kit's, by slug);
        an upload's is written from the database to the local cache once."""
        if self.source == "builtin":
            return None
        path = CACHE_DIR / f"{self.slug}.mask.png"
        if not path.exists() and self.mask_bytes:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(self.mask_bytes)
        return path

    def public(self) -> dict:
        return {
            "slug": self.slug,
            "name": self.name,
            "source": self.source,
            "description": self.description,
            "placement": "auto" if self.auto else self.layout.placement,
            "layout": self.layout.public(),
            "placements": [{"placement": c.placement, "anchor": c.anchor, "rotate": c.rotate,
                            "label": ref.placement_label(c)} for c in self.candidates],
            "where": auto_where(self.candidates) if self.auto else ref.where_text(self.layout),
            "default_animation": self.default_animation,
            "preview_url": f"/api/rebrand/patterns/{self.slug}/preview",
            "source_file": self.source_file,
            "created_at": self.created_at,
            "deletable": self.source == "upload",
            # Only on a transition: every other entry keeps its shape.
            **({"transition": self.transition.public()} if self.transition is not None else {}),
        }


def _spec_description(slug: str) -> str:
    return next((s.description for s in pattern_kit.SPECS if s.slug == slug), "")


# ---------------------------------------------------------------------------
# Placement arithmetic — a pattern's layouts, calculated from its art
# ---------------------------------------------------------------------------

# The canvas the arithmetic is done on: the reels are 9:16, 720 wide. A
# layout's scale is a multiple of the frame's width (the short side here), so
# the numbers hold on any 9:16 canvas.
DESIGN_CANVAS = (720, 1280)
# A side strip reaches the middle of the frame, like the approved 2.png.
STRIP_REACH = 0.5
# A compact hero graphic (neither long nor tall) is drawn at 2.png's size.
HERO_SCALE = 2.0
# Art at least this much wider than tall is a strip, stood up for a side
# strip; at most UPRIGHT_ASPECT it already stands.
STAND_UP_ASPECT = 1.6
UPRIGHT_ASPECT = 0.6
# A band is at most this share of the frame's height (Layout.reach).
BAND_REACH = 0.3
BAND_MAX_SCALE = 3.0
# A texture's repeat is drawn at 1/8 of the frame's short side: the grid's
# 50-px cell of a 2400-px sheet -> x6.0 -> 90-px cells, 8 across a 720 frame,
# its crosshair columns (every 3rd cell) at x = 90, 360 and 630 — one behind
# the speaker's centre line, one either side.
FULL_CELL = 0.125
FULL_CELL_SCALE = (0.5, 12.0)
# A composition covering the frame is zoomed this much past "just covering":
# blueprint at x1.25 fills 93% of the background cells with ~6-px strokes; at
# x1.0 its near-empty roof takes the top fifth, at x1.5 it becomes a maze.
FULL_COVER_ZOOM = 1.25
# Scales are kept on this grid: a minimum rounds UP (a seam-free minimum must
# stay seam-free), a maximum rounds DOWN (a band sized to a letterbox bar must
# stay inside it).
SCALE_STEP = 0.05
# Scale limits for a stored layout (the grid's texture scale is 6.0).
SCALE_LIMITS = (0.1, 12.0)


def _up(v: float) -> float:
    return round(math.ceil(v / SCALE_STEP - 1e-9) * SCALE_STEP, 2)


def _down(v: float) -> float:
    return round(math.floor(v / SCALE_STEP + 1e-9) * SCALE_STEP, 2)


@dataclass(frozen=True)
class ArtProfile:
    """What the placement arithmetic needs to know about a pattern's art,
    measured off its mask (measure_art).

      aspect     width / height of the art
      run        the share of the art's LENGTH (its long axis), counted from
                 its dense end, that holds solid ink — 1.png's first quarter
                 is bare thread, so its run is ~0.75; anything more than
                 that of it shown is the "faded" thread start
      dense_end  the edge of the long axis carrying more ink ("left" /
                 "right" for a wide art, "top" / "bottom" for a tall one),
                 None when the art is even
      tiles      a texture (the grid), which may repeat: its opposite edges
                 continue into each other AND it repeats evenly inside
                 itself along both axes; False for a composition — a
                 symmetric emblem's edges match too, but it is one figure
      cell       a texture's repeat as a share of its width, or None"""
    aspect: float
    run: float = 1.0
    dense_end: Optional[str] = None
    tiles: bool = False
    cell: Optional[float] = None

    def public(self) -> dict:
        return {"aspect": round(self.aspect, 4), "run": round(self.run, 4),
                "dense_end": self.dense_end, "tiles": self.tiles,
                "cell": round(self.cell, 5) if self.cell else None}


# A column of the art counts as solid when its smoothed share of pixels at
# >= half opacity is at least this fraction of the art's most solid column;
# 1.png's thread-only start never gets there, its beads always do. The
# renderer holds the rule (ref.solid_held): a band shrunk inside the frame
# ends where this run ends.
RUN_SOLID_FRAC = ref.RUN_SOLID_FRAC
# One end of the long axis is "dense" when its third holds this many times
# the ink of the other third (5.png: 2.9x, 1.png: 4x; the grid 1.0x).
DENSE_END_RATIO = 1.3
# Wrap continuity (the ink profile along each edge correlated with the
# opposite edge's) on both axes that a texture needs: the grid scores
# 1.00/1.00, every other built-in at most 0.59. Not enough on its own: any art
# symmetric on both axes scores 1.00/1.00 too (its edges are mirror images),
# and a round emblem judged a texture was drawn as a stack of three copies.
TILE_WRAP_MIN = 0.90
# So a texture must also REPEAT inside itself: a period along both axes
# (_period: at least four repeats) and even ink — the density of blocks of
# whole periods, about 1/EVEN_BLOCKS of the art each, varies by at most
# EVEN_CV_MAX (coefficient of variation). Measured: the grid 0.02, lattices
# 0.00; compositions with a period in their column profile — concentric
# rectangles 0.27, 5.png 0.39. A texture judged a composition is only drawn
# as one copy covering the frame; the reverse repeats a figure, so the bar
# sits near the textures.
EVEN_BLOCKS = 6
EVEN_CV_MAX = 0.12
# An edge profile this flat (std) is one solid line or empty paper along
# that edge; two such edges of similar mean ink match — a lattice with a
# line on both borders has no variance to correlate, and scored 0.
FLAT_EDGE_STD = 0.02
FLAT_EDGE_MEAN = 0.10


def _wrap(a, px: Optional[int] = None, py: Optional[int] = None) -> tuple[float, float]:
    """How well the art continues into itself across each seam when it is
    repeated — (left/right, top/bottom), correlations up to 1. The better of
    two readings per axis: the ink profile along each edge against the
    opposite edge's, and, given the art's period along that axis, the band
    just past the seam against the band one period before it — what a
    repeat must reproduce. A lattice drawn with range(0, W, step) has a line
    on one border and paper on the other: it repeats perfectly, yet its edge
    profiles do not correlate (it scored 0 and was boxed into a left strip)."""
    import numpy as np

    k = max(2, min(a.shape) // 400)

    def corr(u, v) -> float:
        if (float(u.std()) < FLAT_EDGE_STD and float(v.std()) < FLAT_EDGE_STD
                and abs(float(u.mean()) - float(v.mean())) < FLAT_EDGE_MEAN):
            return 1.0                                # both a solid line, or both empty
        u = u - u.mean()
        v = v - v.mean()
        d = float(np.sqrt((u * u).sum() * (v * v).sum()))
        return float((u * v).sum()) / d if d > 1e-9 else 0.0

    def seam(b, p: Optional[int]) -> float:           # the seam runs down b's columns
        if not p:
            return -1.0
        w = b.shape[1]
        band = min(p, max(k, p // 2))
        return corr(b[:, :band].ravel(), b[:, w - p:w - p + band].ravel())

    return (max(corr(a[:, :k].mean(1), a[:, -k:].mean(1)), seam(a, px)),
            max(corr(a[:k].mean(0), a[-k:].mean(0)), seam(a.T, py)))


def _period(a) -> Optional[int]:
    """The art's dominant horizontal repeat in mask pixels: the first
    autocorrelation peak of its column profile within 80% of the strongest
    one (the fundamental, not a multiple of it); None without a clear one."""
    import numpy as np

    p = a.mean(axis=0)
    p = p - p.mean()
    n = len(p)
    if n < 16 or not p.any():
        return None
    f = np.fft.rfft(p, 2 * n)
    ac = np.fft.irfft(f * np.conj(f))[:n]
    ac = ac / (ac[0] or 1.0)
    lo, hi = 4, n // 4
    peaks = [i for i in range(lo + 1, hi - 1)
             if ac[i] > ac[i - 1] and ac[i] >= ac[i + 1] and ac[i] > 0.3]
    if not peaks:
        return None
    best = max(ac[i] for i in peaks)
    return min(i for i in peaks if ac[i] >= 0.8 * best)


def _unevenness(a, px: int, py: int) -> float:
    """How unevenly the art's ink is spread: the coefficient of variation of
    its density over blocks of whole periods (px x py), each about
    1/EVEN_BLOCKS of the art — whole periods, so a texture's blocks hold the
    same ink whatever its phase."""
    h, w = a.shape
    bx = px * max(1, int((w / EVEN_BLOCKS) // px))
    by = py * max(1, int((h / EVEN_BLOCKS) // py))
    nx, ny = max(1, w // bx), max(1, h // by)
    d = a[:ny * by, :nx * bx].reshape(ny, by, nx, bx).mean(axis=(1, 3))
    return float(d.std() / max(float(d.mean()), 1e-9))


def measure_art(mask: Path) -> ArtProfile:
    """The ArtProfile of a prepared mask (alpha = the art)."""
    import numpy as np
    from PIL import Image

    with Image.open(mask) as im:
        a = np.asarray(im.convert("RGBA").getchannel("A"), dtype="float32") / 255.0
    h, w = a.shape
    wide = w >= h
    along = 0 if wide else 1                      # axis to average across
    ink = a.mean(axis=along)
    solid = (a >= 0.5).mean(axis=along)
    n = len(ink)
    third = max(1, n // 3)
    first, last = float(ink[:third].sum()), float(ink[-third:].sum())
    dense: Optional[str] = None
    if first >= DENSE_END_RATIO * max(last, 1e-9):
        dense = "left" if wide else "top"
    elif last >= DENSE_END_RATIO * max(first, 1e-9):
        dense = "right" if wide else "bottom"
    held = ref.solid_held(solid)
    if held.size and dense in ("right", "bottom"):
        run = (n - int(held[0])) / n
    elif held.size:
        run = (int(held[-1]) + 1) / n
    else:
        run = 1.0
    px, py = _period(a), _period(a.T)
    lr, tb = _wrap(a, px, py)
    tiles = bool(min(lr, tb) >= TILE_WRAP_MIN and px and py
                 and _unevenness(a, px, py) <= EVEN_CV_MAX)
    period = px if tiles else None
    return ArtProfile(aspect=w / h, run=float(min(1.0, max(0.05, run))), dense_end=dense,
                      tiles=tiles, cell=(period / w) if period else None)


def fit_layouts(
    art_aspect: float, placements: Iterable[str], *, run: float = 1.0,
    dense_end: Optional[str] = None, tiles: bool = False, cell: Optional[float] = None,
    canvas: tuple[int, int] = DESIGN_CANVAS, both_corners: Optional[bool] = None,
) -> tuple[Layout, ...]:
    """The layouts a pattern takes in each of `placements` (in that order;
    the first is its default), CALCULATED from its art the way the owner's
    placements were worked out on real frames — so an upload gets the same
    care as the built-ins:

      left/right  a LONG art (aspect >= STAND_UP_ASPECT) is stood up with its
                  dense end at the TOP, so what the frame crops at the bottom
                  is its sparse end; drawn STRIP_REACH of the width across
                  (reaching the middle), bigger only if that would show more
                  than its solid run or leave a seam where it repeats. A
                  compact hero is drawn at 2.png's size (x2), bigger only to
                  avoid a seam. A tall art already stands.
      top/bottom  one band from a corner, its dense end in the corner: as big
                  as fits inside the letterbox bar of any source up to 1:1
                  ((H/W - 1)/2 of the width tall: 280 px on 720x1280), and at
                  least big enough to show only its solid run.
      full        a texture (`tiles`) is centred with its repeat at 1/8 of the
                  frame's short side; a composition is ONE copy covering the
                  frame (turned a quarter if that shows much more of it).

    For 1.png (aspect 5.16, dense right, run 0.75) this gives exactly its six
    built-in candidates. `both_corners` (default: when there are several
    placements) gives a band both corner anchors, left first."""
    W, H = canvas
    a = max(1e-3, float(art_aspect))
    run = min(1.0, max(0.05, float(run or 1.0)))
    places = [p for p in dict.fromkeys(str(p).strip().lower() for p in placements)
              if p in ref.PLACEMENTS]
    if both_corners is None:
        both_corners = len(places) > 1
    out: list[Layout] = []
    for place in places:
        if place in ("left", "right"):
            if a >= STAND_UP_ASPECT:
                rotate = 90 if dense_end == "left" else 270   # dense end up
                need = H / (W * a * run)                       # tile height scale*W*a
                scale = max(STRIP_REACH, _up(need))
            elif a <= UPRIGHT_ASPECT:
                rotate = 180 if dense_end == "bottom" else 0
                scale = max(STRIP_REACH, _up(H * a / (W * run)))
            else:
                rotate = 0
                scale = max(HERO_SCALE, _up(H * a / W))
            out.append(Layout(placement=place, scale=min(SCALE_LIMITS[1], scale),
                              reach=STRIP_REACH, anchor="left", rotate=rotate))
        elif place in ("top", "bottom"):
            if a >= 1.0:
                rotate, along = (180 if dense_end == "right" else 0), a
            else:
                rotate, along = (90 if dense_end == "bottom" else 270), 1.0 / a
            bar = max(0.0, (H / W - 1.0) / 2.0)
            scale = _down(bar * along)
            if run < 0.98:
                scale = max(scale, _up(1.0 / run))
            scale = min(BAND_MAX_SCALE, max(SCALE_STEP, scale))
            for anchor in (("left", "right") if both_corners else ("left",)):
                out.append(Layout(placement=place, scale=scale, reach=BAND_REACH,
                                  anchor=anchor, rotate=rotate))
        else:                                              # full
            if tiles:
                scale = FULL_CELL / cell if cell else 1.0
                scale = round(min(FULL_CELL_SCALE[1], max(FULL_CELL_SCALE[0], scale)), 2)
                out.append(Layout(placement="full", scale=scale, reach=1.0, fit="centre"))
            else:
                frame = W / H

                def shown(r: float) -> float:
                    return min(r, frame) / max(r, frame)

                rotate = 90 if shown(1.0 / a) > 1.25 * shown(a) else 0
                out.append(Layout(placement="full", scale=FULL_COVER_ZOOM, reach=1.0,
                                  rotate=rotate, fit="cover"))
    return tuple(out)


def fit_profile(profile: ArtProfile, placements: Iterable[str], **kw: Any) -> tuple[Layout, ...]:
    """fit_layouts for a measured ArtProfile."""
    return fit_layouts(profile.aspect, placements, run=profile.run, dense_end=profile.dense_end,
                       tiles=profile.tiles, cell=profile.cell, **kw)


# 1.png's candidates — the placement study (2026-09-26, 15 frames, 13
# speakers/framings) and exactly what fit_layouts calculates from its art:
#   left/right strip  stood up 90° counter-clockwise, x0.5, reaching the
#                     middle: its cut end runs off the TOP, its thread-only
#                     start (the first quarter) stays below the frame, four
#                     bead columns, the inner one ending in its own beads.
#                     The left strip is the default (nothing fits / no frame).
#   top/bottom band   x2.0 from a corner, turned 180° so the dense, cut end is
#                     in the corner (at 0° the corner showed the faint thread
#                     start at 0.17 opacity): 279 px, inside the letterbox bar
#                     of any source up to 1:1. The planner only lets a bottom
#                     band win on a letterbox bar — on footage it reads as a
#                     rug or a print on the desk.
BEADS_ROW_CANDIDATES: tuple[Layout, ...] = (
    Layout(placement="left", scale=0.5, reach=0.5, anchor="left", rotate=270),
    Layout(placement="right", scale=0.5, reach=0.5, anchor="left", rotate=270),
    Layout(placement="top", scale=2.0, reach=0.3, anchor="left", rotate=180),
    Layout(placement="top", scale=2.0, reach=0.3, anchor="right", rotate=180),
    Layout(placement="bottom", scale=2.0, reach=0.3, anchor="left", rotate=180),
    Layout(placement="bottom", scale=2.0, reach=0.3, anchor="right", rotate=180),
)


BUILTINS: dict[str, PatternEntry] = {
    "structure": PatternEntry(
        slug="structure", name="Structure", source="builtin",
        description=_spec_description("structure"),
        layout=Layout(placement="left", scale=ref.OMNI_SCALE, reach=ref.OMNI_REACH_FRAC),
        default_animation="left_to_right", source_file="2.png",
    ),
    "beads_row": PatternEntry(
        slug="beads_row", name="Bead row", source="builtin",
        description=_spec_description("beads_row"),
        layout=BEADS_ROW_CANDIDATES[0], candidates=BEADS_ROW_CANDIDATES,
        default_animation="left_to_right", source_file="1.png",
    ),
    # The owner: "rotate it counter-clockwise 90 deg and use it as the
    # current 2.png pattern". Turned 270° its dense end is at the BOTTOM,
    # thinning to bare hairlines at the top; x0.6 is the smallest size with
    # no seam where it repeats (anything under x0.51 starts a second copy
    # inside the frame, a straight cut through the beads) that also runs its
    # outer bead column off the left edge the way structure does (x0.55 cut
    # a column in half there), and still keeps the dense end (by x0.75 the
    # hairline band takes 40% of the frame). Reaching the middle, its place
    # and its entrance read word for word as 2.png's.
    "beads_field": PatternEntry(
        slug="beads_field", name="Bead field", source="builtin",
        description=_spec_description("beads_field"),
        # Turned a quarter counter-clockwise (dense end at the bottom), used
        # like 2.png, and anchored on the frame's BOTTOM so the dense beads
        # fill the side and the hairline end runs off the top. x0.75: a full
        # bead curtain — 0.223 of the left half visible beside the speaker
        # over 6 real frames, against 2.png's 0.184 (tiled from the top at
        # x0.6 it was 0.143, mostly hairline).
        layout=Layout(placement="left", scale=0.75, reach=0.5, anchor="left", rotate=270,
                      valign="bottom"),
        default_animation="left_to_right", source_file="5.png",
    ),
    # "Fill the whole screen": ONE copy of the floor plan, just covering the
    # frame x1.25, cropped where it fills best around the speaker. Tiled (as
    # before) it stacked a second map at y=982 and left the left side bare.
    "blueprint": PatternEntry(
        slug="blueprint", name="Blueprint", source="builtin",
        description=_spec_description("blueprint"),
        layout=Layout(placement="full", scale=FULL_COVER_ZOOM, reach=1.0, fit="cover"),
        default_animation="fade", source_file="3.png",
        # "Full-screen transition ONLY": added, it shows 1.5 s at the start.
        transition=BLUEPRINT_TRANSITION,
    ),
    # "This I don't know, try something": the graph paper's own 50-px lines,
    # dots and crosshairs only (STROKE_MASS drops the hazy sub-grid and keeps
    # every line solid), at 90-px cells centred on the speaker.
    "grid": PatternEntry(
        slug="grid", name="Grid", source="builtin",
        description=_spec_description("grid"),
        layout=Layout(placement="full", scale=6.0, reach=1.0, fit="centre"),
        default_animation="fade", source_file="4.png",
    ),
}

# ---------------------------------------------------------------------------
# Storage (uploads)
# ---------------------------------------------------------------------------

_TABLE_READY = False
_DDL = """
CREATE TABLE IF NOT EXISTS yas_rebrand_patterns (
    slug              text PRIMARY KEY,
    name              text NOT NULL,
    description       text NOT NULL DEFAULT '',
    source_file       text,
    layout            jsonb NOT NULL,
    default_animation text NOT NULL,
    analysis          jsonb,
    mask_png          bytea NOT NULL,
    embed_text        text,
    embedding         vector(768),
    created_at        timestamptz DEFAULT now()
)
"""


async def ensure_table() -> None:
    global _TABLE_READY
    if _TABLE_READY:
        return
    from ..db import get_conn

    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(_DDL)
    _TABLE_READY = True


def _layout_from(d: Any) -> Layout:
    d = d if isinstance(d, dict) else {}
    placement = str(d.get("placement") or "left")
    if placement not in ref.PLACEMENTS:
        placement = "left"
    anchor = "right" if str(d.get("anchor") or "left") == "right" else "left"
    try:
        scale = min(SCALE_LIMITS[1], max(SCALE_LIMITS[0], float(d.get("scale") or 1.0)))
        reach = min(1.0, max(0.1, float(d.get("reach") or 0.5)))
    except (TypeError, ValueError):
        scale, reach = 1.0, 0.5
    try:
        rotate = int(d.get("rotate") or 0) % 360
    except (TypeError, ValueError):
        rotate = 0
    rotate = rotate if rotate in (0, 90, 180, 270) else 0
    fit = str(d.get("fit") or "tile")
    fit = fit if fit in ref.FITS else "tile"
    valign = "bottom" if str(d.get("valign") or "top") == "bottom" else "top"
    return Layout(placement=placement, scale=scale, reach=reach, anchor=anchor, rotate=rotate,
                  fit=fit, valign=valign)


def _layouts_from(d: Any) -> tuple[Layout, ...]:
    """The candidate layouts stored in a row's `layout` jsonb. A row stores
    its first candidate's fields at the top level (what every row written
    before candidates existed holds, and still reads as one fixed layout)
    plus, when it has several, all of them under "candidates"."""
    d = d if isinstance(d, dict) else {}
    many = d.get("candidates")
    if isinstance(many, list):
        found = tuple(_layout_from(c) for c in many if isinstance(c, dict))
        if found:
            return found
    return (_layout_from(d),)


def _layout_json(candidates: tuple[Layout, ...]) -> dict:
    out = dict(candidates[0].public())
    if len(candidates) > 1:
        out["candidates"] = [c.public() for c in candidates]
    return out


def _entry_from_row(r: Any) -> PatternEntry:
    import json

    analysis = r.get("analysis")
    if isinstance(analysis, str):
        try:
            analysis = json.loads(analysis)
        except ValueError:
            analysis = {}
    layout = r.get("layout")
    if isinstance(layout, str):
        layout = json.loads(layout)
    created = r.get("created_at")
    candidates = _layouts_from(layout)
    return PatternEntry(
        slug=r["slug"], name=r["name"], source="upload", description=r.get("description") or "",
        layout=candidates[0], candidates=candidates,
        default_animation=normalize_animation(r.get("default_animation")) or "fade",
        source_file=r.get("source_file"),
        created_at=created.isoformat() if hasattr(created, "isoformat") else created,
        analysis=analysis or {}, mask_bytes=bytes(r["mask_png"]) if r.get("mask_png") else None,
    )


async def _uploads(slug: Optional[str] = None) -> list[PatternEntry]:
    from ..db import get_conn

    await ensure_table()
    q = ("SELECT slug, name, description, source_file, layout, default_animation, analysis, "
         "mask_png, created_at FROM yas_rebrand_patterns")
    args: tuple = ()
    if slug:
        q += " WHERE slug = %s"
        args = (slug,)
    q += " ORDER BY created_at DESC"
    async with get_conn(autocommit=True) as conn:
        async with conn.cursor() as cur:
            await cur.execute(q, args)
            rows = await cur.fetchall()
    return [_entry_from_row(r) for r in rows]


async def list_patterns() -> list[PatternEntry]:
    """Built-ins first (in the owner's order), then uploads, newest first.
    A database failure still lists the built-ins."""
    out = list(BUILTINS.values())
    try:
        out += await _uploads()
    except Exception as e:  # noqa: BLE001
        log.warning("[patterns] uploads unavailable: %s", str(e)[:200])
    return out


class LibraryUnavailable(RuntimeError):
    """The uploads could not be read (a database or pooler error): not the
    same as "there is no such pattern"."""


async def lookup(slug: str) -> Optional[PatternEntry]:
    """The pattern, or None when there is no such pattern. A database
    failure RAISES LibraryUnavailable, so a check can tell "try again" from
    "pick another" (get() reads both as None)."""
    if slug in BUILTINS:
        return BUILTINS[slug]
    if not re.fullmatch(r"[a-z0-9_-]{1,64}", slug or ""):
        return None
    try:
        found = await _uploads(slug)
    except Exception as e:  # noqa: BLE001
        raise LibraryUnavailable(str(e)[:200] or type(e).__name__) from e
    return found[0] if found else None


async def get(slug: str) -> Optional[PatternEntry]:
    """lookup(), with a database failure read as None: a run falls back to
    the default pattern rather than fail."""
    try:
        return await lookup(slug)
    except LibraryUnavailable as e:
        log.warning("[patterns] lookup of %s failed: %s", slug, e)
        return None


async def delete_upload(slug: str) -> bool:
    from ..db import get_conn

    if slug in BUILTINS:
        return False
    await ensure_table()
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute("DELETE FROM yas_rebrand_patterns WHERE slug = %s", (slug,))
            gone = cur.rowcount > 0
    for p in (CACHE_DIR / f"{slug}.mask.png", CACHE_DIR / f"{slug}.preview.png"):
        p.unlink(missing_ok=True)
    return gone


# ---------------------------------------------------------------------------
# Previews
# ---------------------------------------------------------------------------

def preview(entry: PatternEntry) -> Path:
    """Light lines on a dark tile, PREVIEW_EDGE on the long side — what the
    picker shows. Cached; a built-in's is rebuilt if its mask changes.

    A pattern the renderer judges by stroke mass (the grid) is previewed the
    same way, so its card shows the solid lines it will be drawn with, not
    the hazy sub-grid the renderer drops (a separate cache name, so a card
    cached before that is not served)."""
    from PIL import Image

    src = entry.mask_path() or ref._mask_for(entry.slug, None)
    mass = ref.STROKE_MASS.get(entry.slug) if entry.source == "builtin" else None
    out = CACHE_DIR / f"{entry.slug}.preview{'.mass' if mass else ''}.png"
    if out.exists() and out.stat().st_mtime >= src.stat().st_mtime:
        return out
    with Image.open(src) as im:
        alpha = im.convert("RGBA").getchannel("A")
    if mass:
        alpha = ref._stroke_mass(alpha, *mass)
    alpha.thumbnail((PREVIEW_EDGE, PREVIEW_EDGE), Image.LANCZOS)
    ink = Image.new("RGBA", alpha.size, (236, 236, 244, 255))
    ink.putalpha(alpha)
    tile = Image.new("RGBA", alpha.size, (27, 27, 34, 255))
    tile.alpha_composite(ink)
    out.parent.mkdir(parents=True, exist_ok=True)
    tile.convert("RGB").save(out, format="PNG", optimize=True)
    return out


# ---------------------------------------------------------------------------
# Upload: key, mask, analysis, embedding
# ---------------------------------------------------------------------------

def detect_key(rgba) -> str:
    """How the art's shape is recovered (pattern_kit.PatternSpec.key):
    transparency when the file has real transparency, otherwise darkness on
    a light sheet or brightness on a dark ground, read off the border."""
    import numpy as np

    alpha = rgba[:, :, 3]
    if float((alpha < 250).mean()) > 0.05:
        return "alpha"
    rgb = rgba[:, :, :3].astype("float32")
    lum = rgb[:, :, 0] * 0.299 + rgb[:, :, 1] * 0.587 + rgb[:, :, 2] * 0.114
    h, w = lum.shape
    r = max(1, int(min(h, w) * 0.03))
    border = np.concatenate([lum[:r].ravel(), lum[-r:].ravel(), lum[:, :r].ravel(), lum[:, -r:].ravel()])
    return "ink" if float(np.median(border)) >= 128 else "glow"


# Art this long (either way) is a strip that can go anywhere, like 1.png.
STRIP_ART_ASPECT = 2.5


def heuristic_layout(
    aspect: float, coverage: float, profile: Optional[ArtProfile] = None,
) -> tuple[tuple[Layout, ...], str, str]:
    """(candidate layouts, entrance, role) when Gemini is not available: the
    owner's rules, by shape, each place's layout calculated (fit_layouts).

      a long strip       anywhere, like 1.png: side strips and corner bands,
                         auto-placed per clip (left strip first)
      a texture / sparse the whole background
      anything else      the hero graphic: the left half, like 2.png"""
    profile = profile or ArtProfile(aspect=aspect)
    if aspect >= STRIP_ART_ASPECT or aspect <= 1.0 / STRIP_ART_ASPECT:
        places, animation, role = ["left", "right", "top", "bottom"], "left_to_right", "strip"
    elif profile.tiles or coverage < 0.12:
        places, animation, role = ["full"], "fade", "field"
    else:
        places, animation, role = ["left"], "left_to_right", "structure"
    return fit_profile(profile, places), animation, role


_ANALYSIS_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "name": {"type": "STRING", "description": "A short name for the pattern, 1-3 words."},
        "description": {"type": "STRING", "description": "2-3 sentences: what it looks like (line work, density, direction, motifs) and what it suits."},
        "role": {"type": "STRING", "enum": ["strip", "structure", "blueprint", "grid", "field", "other"]},
        "density": {"type": "STRING", "enum": ["sparse", "medium", "dense"]},
        "placements": {
            "type": "ARRAY", "items": {"type": "STRING", "enum": list(ref.PLACEMENTS)},
            "description": "EVERY place this pattern may take, best first. One place = it always "
                           "goes there; several = the pipeline picks, per clip, whichever fits "
                           "that clip's framing best.",
        },
        "animation": {"type": "STRING", "enum": [k for k in ANIMATIONS if k != "none"]},
        "tags": {"type": "ARRAY", "items": {"type": "STRING"}},
    },
    "required": ["name", "description", "role", "density", "placements", "animation"],
}

_ANALYSIS_SYSTEM = """You look at a brand pattern — line art that will be drawn in ONE colour,
transparent between its lines, BEHIND a person talking to camera in a 9:16 video — and decide
which places in the frame it may take. The pipeline works out the size, rotation and corner of
each place from the art itself, and — when you allow more than one — picks, for every clip,
the allowed place that fits that clip's framing best. The house rules, from the brand owner:
- A dense, architectural hero graphic goes on the LEFT half of the frame, large, behind the
  speaker: placements ["left"] only.
- A long, thin strip or row of glyphs can go basically anywhere: along the TOP or BOTTOM edge
  from a corner, or stood up as a side strip on the LEFT or RIGHT — list every place it suits,
  e.g. ["left", "right", "top", "bottom"].
- A schematic, a floor plan, a fine grid or an even texture fills the WHOLE background:
  placements ["full"].
List the places, best first; never a place where the art would read badly.
Pick the entrance that suits its direction: a pattern that decays or reads left to right enters
"left_to_right"; an even texture "fade"; a techy, digital one may "glitch". Describe it
literally, the way a designer would to someone who cannot see it."""


def allowed_placements(analysis: dict) -> list[str]:
    """The valid, de-duplicated places in an analysis, best first. Also reads
    the single "placement" older analyses (and a model ignoring the schema)
    give."""
    raw = analysis.get("placements")
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, list):
        raw = [analysis.get("placement")] if analysis.get("placement") else []
    return [p for p in dict.fromkeys(str(x).strip().lower() for x in raw) if p in ref.PLACEMENTS]


async def analyse(preview_png: Path, thumb_png: Path, *, aspect: float, coverage: float) -> dict:
    """Gemini's reading of an uploaded pattern ({} when it is unreachable)."""
    from .vertex_client import gemini_multimodal

    prompt = (
        f"Image 1 is the pattern as it will be drawn (light lines on dark). Image 2 is the "
        f"file as uploaded. Measured: aspect (width/height) {aspect:.2f}, ink covers "
        f"{coverage:.0%} of its bounding box. Decide name, description, role, density, "
        f"the places it may take (placements) and entrance animation."
    )
    try:
        out = await asyncio.wait_for(
            gemini_multimodal(prompt, images=[preview_png, thumb_png], system=_ANALYSIS_SYSTEM,
                              model=config.vertex_gemini_fast_model, json_schema=_ANALYSIS_SCHEMA,
                              temperature=0.2, max_tokens=2000),
            timeout=90,
        )
        return out if isinstance(out, dict) else {}
    except Exception as e:  # noqa: BLE001 — the heuristic layout stands in
        log.warning("[patterns] analysis failed: %s", str(e)[:200])
        return {}


def _slugify(name: str) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")[:32] or "pattern"
    return f"u-{base}-{secrets.token_hex(3)}"


async def add_upload(data: bytes, filename: str, name: Optional[str] = None) -> PatternEntry:
    """Prepare, analyse, embed and store an uploaded pattern. Raises
    ValueError for anything that is not a usable pattern image."""
    import numpy as np
    from PIL import Image

    if not data:
        raise ValueError("The file is empty.")
    if len(data) > MAX_UPLOAD_BYTES:
        raise ValueError(f"The file is larger than {MAX_UPLOAD_BYTES // (1024 * 1024)} MB.")
    # Print-resolution pattern art is legitimately huge (the built-ins are up
    # to 16k wide); PIL's decompression-bomb guard would refuse it before the
    # pixel cap below is even checked. pattern_kit lifts it the same way.
    Image.MAX_IMAGE_PIXELS = None
    try:
        with Image.open(io.BytesIO(data)) as probe:
            if probe.width * probe.height > MAX_UPLOAD_PIXELS:
                raise ValueError("The image is too large (over 200 megapixels).")
            probe.verify()
    except ValueError:
        raise
    except Exception as e:
        raise ValueError(f"Not an image this can read: {e}") from e

    slug = _slugify(name or Path(filename or "").stem)
    work = CACHE_DIR / "uploads" / slug
    work.mkdir(parents=True, exist_ok=True)
    src = work / "source.png"
    with Image.open(io.BytesIO(data)) as im:
        rgba = im.convert("RGBA")
    rgba.save(src)
    key = await asyncio.to_thread(detect_key, np.asarray(rgba))

    spec = pattern_kit.PatternSpec(slug=slug, source=filename or "upload", role="field", key=key,
                                   keep_art=False, description="")
    try:
        prepared = await asyncio.to_thread(pattern_kit._prepare_one, spec, src, work)
    except ValueError as e:
        raise ValueError("No line work found in the image — a pattern needs visible lines or "
                         "shapes on a plain or transparent background.") from e
    mask_file = work / f"{slug}.mask.png"
    entry = PatternEntry(slug=slug, name=(name or Path(filename or "").stem or "Pattern")[:60],
                         source="upload", description="", layout=Layout(),
                         default_animation="left_to_right", source_file=filename or None,
                         mask_bytes=mask_file.read_bytes())
    prev = await asyncio.to_thread(preview, entry)
    thumb = work / "thumb.png"
    small = rgba.copy()
    small.thumbnail((PREVIEW_EDGE, PREVIEW_EDGE))
    small.save(thumb)

    # The art measured once: every place's layout is calculated from it,
    # whoever decides which places are allowed (Gemini, or the rules).
    profile = await asyncio.to_thread(measure_art, mask_file)
    candidates, animation, role = heuristic_layout(prepared.aspect, prepared.coverage, profile)
    analysis = await analyse(prev, thumb, aspect=prepared.aspect, coverage=prepared.coverage)
    places: list[str] = []
    if analysis:
        places = allowed_placements(analysis)
        if places:
            candidates = fit_profile(profile, places)
        animation = normalize_animation(analysis.get("animation")) or animation
        role = analysis.get("role") or role
    entry.set_candidates(candidates)
    entry.default_animation = animation
    if not name and analysis.get("name"):
        entry.name = str(analysis["name"])[:60]
    entry.description = str(analysis.get("description") or
                            f"Uploaded pattern, {prepared.coverage:.0%} ink, aspect {prepared.aspect:.2f}.")
    entry.analysis = {
        **analysis, "role": role, "key": key, "aspect": prepared.aspect,
        "coverage": prepared.coverage, "source_color": prepared.source_color,
        "width": prepared.width, "height": prepared.height,
        "profile": profile.public(),
        "placements": list(dict.fromkeys(c.placement for c in entry.candidates)),
        "placed_by": "gemini" if places else "heuristic",
        "analysed_by": "gemini" if analysis else "heuristic",
    }
    where = entry.public()["where"]

    embed_text = (f"{entry.name}. {entry.description} Role: {role}. Placement: "
                  f"{where}. Tags: {', '.join(analysis.get('tags') or [])}.")
    embedding = None
    try:
        from .vertex_client import gemini_embed

        vecs = await gemini_embed([embed_text])
        embedding = vecs[0] if vecs else None
    except Exception as e:  # noqa: BLE001 — stored without; still usable
        log.warning("[patterns] embedding %s failed: %s", slug, str(e)[:200])

    await _insert(entry, embed_text, embedding)
    # The cache copy the renderer reads; the database row is the truth.
    (CACHE_DIR / f"{slug}.mask.png").write_bytes(entry.mask_bytes or b"")
    log.info("[patterns] uploaded %s (%s): %s, %s, key=%s, by %s", slug, entry.name,
             where, animation, key, entry.analysis["analysed_by"])
    return entry


async def _insert(entry: PatternEntry, embed_text: str, embedding: Optional[list[float]]) -> None:
    from psycopg.types.json import Jsonb

    from ..db import get_conn

    await ensure_table()
    vec = "[" + ",".join(f"{v:.6f}" for v in embedding) + "]" if embedding else None
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                INSERT INTO yas_rebrand_patterns
                    (slug, name, description, source_file, layout, default_animation,
                     analysis, mask_png, embed_text, embedding)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s::vector)
                """,
                (entry.slug, entry.name, entry.description, entry.source_file,
                 Jsonb(_layout_json(entry.candidates)), entry.default_animation, Jsonb(entry.analysis),
                 entry.mask_bytes, embed_text, vec),
            )
