"""The recipe — the single contract between the Rebrand window and the harness.

A recipe is plain data: a list of ELEMENTS (pattern, logo, text, icon, slab,
beads, plate), each switched on or off, placed, layered in front of or behind
the speaker, and given an entrance; plus clip-wide settings (where colours
come from, who writes the Omni prompt's opening, which guards run). The window
edits it, the harness renders it, and a version stores it — so "edit & retry"
is just a changed recipe.

Nothing here draws or generates anything. It validates, fills each kind's
defaults, and gives every recipe a canonical form and hash (the stage cache
and the version history key on it).

The "42 default" preset reproduces the owner-approved hard-coded run exactly:
one pattern element, structure (2.png), placed and animated by the pattern's
own defaults ("auto"), the brief writing the prompt's opening, the intro's
colour, and today's guards. tests/test_rebrand_golden.py locks that run.
preset_42_house is the same element made in house (engine "house": our own
renderer, $0) — what the studio starts every new scene from.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
from dataclasses import dataclass
from typing import Any, Literal, Optional, get_args

from pydantic import (BaseModel, ConfigDict, Field, ValidationError, field_validator, model_serializer,
                      model_validator)

RECIPE_VERSION = 1

# Who draws the recipe:
#   omni    the Omni video model repaints each part with our target frame
#           (paid; every version made before 2026-09-29)
#   house   our own renderer lays a graphics layer over the untouched footage
#           ($0) — the only engine for new work (the owner, 2026-09-29);
#           Omni is switched off for new work (omni_on, 2026-09-30)
# The model's default stays "omni", so every stored recipe parses as what it
# was and keeps its hash; the studio writes "house" on every new draft.
Engine = Literal["omni", "house"]
# What each engine can draw today (runnable()).
ENGINE_READY: dict[str, bool] = {"omni": True, "house": True}


def omni_on() -> bool:
    """Whether new work may still be drawn by Omni: REBRAND_OMNI, OFF unless
    set to on / 1 / true / yes (the owner, 2026-09-30: "no need Omni").
    Off, runnable() refuses an Omni recipe and the pipeline starts no Omni
    run at all (the whole reel, the intro and outro cards, a scene); the
    in-house engine never depends on it, and versions Omni made before still
    list, play, rate, download and are added to ($0, over Omni's video)
    either way."""
    return os.getenv("REBRAND_OMNI", "off").strip().lower() in ("1", "on", "true", "yes")

Kind = Literal["pattern", "logo", "text", "icon", "slab", "beads", "plate"]
Z = Literal["behind", "front"]
Anchor = Literal[
    "auto",            # the element's own default place (a pattern's library layout)
    "brand42",         # the 42 brand frame's geometry (bars / full layout)
    "full",            # the whole frame
    "edge-left", "edge-right", "edge-top", "edge-bottom",
    "top-left", "top", "top-right", "left", "centre", "right",
    "bottom-left", "bottom", "bottom-right",
    "custom",          # contained in placement.box
]
Region = Literal["frame", "picture", "top_bar", "bottom_bar"]
Entrance = Literal[
    "auto", "none", "fade", "left_to_right", "right_to_left", "top_to_bottom",
    "bottom_to_top", "glitch", "pop", "slide", "typewriter", "drop_in",
]

# The entrances a pattern has (pattern_library.ENTRANCES, plus "auto" = the
# pattern's own, aimed at the edge it grows from). Omni is told one of its
# own (pattern_library.ANIMATIONS); our renderer also slides a pattern in
# from the edge it grows from. Kept here as data so the contract does not
# import the renderer; a test holds the lists together.
OMNI_PATTERN_ENTRANCES = ("auto", "none", "fade", "left_to_right", "right_to_left",
                          "top_to_bottom", "bottom_to_top", "glitch")
PATTERN_ENTRANCES = OMNI_PATTERN_ENTRANCES + ("slide",)
# How a pattern goes out at the end of its span (pattern_library.EXITS; the
# in-house engine only):
#   auto      none when it lasts to the end of the scene (the cut takes it
#             away, as always); else its entrance played backwards
#   to_*      the lines vanish towards that edge (the reverse of a sweep in)
#   slide_out the whole pattern slides out to the edge it grew from
PatternExit = Literal["auto", "none", "fade", "glitch", "to_left", "to_right", "to_top", "to_bottom",
                      "slide_out"]
PATTERN_EXITS: tuple[str, ...] = get_args(PatternExit)
# A pattern's time (the in-house engine), in scene seconds: a span from its
# start to its end, with an entrance ramp at the start (animation.duration_sec)
# and an exit ramp at the end (animation.exit_sec).
SPAN_MIN_SEC = 0.5          # the least a pattern may show
RAMP_SEC = (0.2, 3.0)       # an entrance's or an exit's length
EXIT_SEC = 0.6              # an exit's length when none is set
# A pattern moved by hand (the in-house engine): placement.offset (a move in
# frame widths / heights), placement.size (x the size it was placed at) and
# placement.rotate (degrees, clockwise) about the centre of placement.box
# (the visible box it was grabbed at, stored so the pivot never drifts).
HAND_SCALE = (0.5, 3.0)
HAND_OFFSET_MAX = 1.0
HAND_ROTATE_MAX = 180.0
# Whether a pattern follows the camera (the in-house engine): "room" moves
# with the footage's push-in, "screen" holds still on screen; "auto" (or
# unset) picks room, or screen for a pattern on the letterbox bars.
PATTERN_FOLLOW = ("auto", "room", "screen")
# Where a pattern may go: its own place(s) from the library, or an edge / the
# whole frame worked out from its art. Boxes and corners come with the
# element kinds that need them.
PATTERN_ANCHORS = ("auto", "edge-left", "edge-right", "edge-top", "edge-bottom", "full")
# Up to three patterns, each in its own place: one takes the approved single
# clause word for word, two or three take prompt.patterns_clause. Behind a
# talking head only the two sides and a top or bottom band stay free, so a
# fourth has nowhere clean to go — and every pattern is one more place Omni
# must copy exactly.
PATTERN_MAX = 3

# A pattern's props (read, never rewritten: the 42 default's canonical form
# and hash must not move).
#   slug     which pattern (pattern_library)
#   colour   "auto" (the recipe's colours) or #rrggbb
#   layout   a pinned place: a Layout.public() the element keeps whatever the
#            planner would draw now (a colour change or removing a sibling
#            must not move it); honoured only while it is still one of the
#            pattern's places (elements/pattern.py)
#   shuffle  a short tag mixed into this element's own planner seed ("Another
#            spot" for one pattern, not every auto element)
#   follow   the in-house engine: "auto" | "room" | "screen" (PATTERN_FOLLOW);
#            absent = auto
#   resolved the pattern exactly as Generate drew it ({layout, colour, ground,
#            ink, hand, follow, engine_v}) — written only by the router at
#            Generate; a client's is never read (the router strips it)
PATTERN_PROPS = ("slug", "colour", "layout", "shuffle", "follow", "resolved")
PATTERN_RESOLVED_KEYS = ("layout", "colour", "ground", "ink", "hand", "follow", "engine_v")
_SLUG_RE = re.compile(r"^[a-z0-9_-]{1,64}$")
_HEX_RE = re.compile(r"^#[0-9a-fA-F]{6}$")
_SHUFFLE_RE = re.compile(r"^[a-z0-9]{1,16}$")
# Kept here as data so the contract does not import the renderer; a test holds
# them to rebrand_pattern_ref.PLACEMENTS / FITS.
LAYOUT_PLACEMENTS = ("left", "right", "top", "bottom", "full")
LAYOUT_FITS = ("tile", "centre", "cover")
LAYOUT_KEYS = ("placement", "scale", "reach", "anchor", "rotate", "fit", "valign")
# The largest scale the library draws (pattern_library.SCALE_LIMITS[1]): the
# grid is x6, and an upload's strip is worked out up to x12 — any of them can
# be pinned by the studio (a colour change, a removed neighbour).
LAYOUT_SCALE_MAX = 12.0

# Icons: ONE icon element, up to ICON_ITEMS_MAX icons on screen one after
# another, BEHIND the speaker (the speaker's matte wins: hands pass in front,
# the face is never covered; one icon may be put in front of her with its own
# look, only where it stays off the face) and stamped by us. Where: "auto"
# (the planner's pick), a side of the frame's fixed spot grid, or a custom box.
# Size S / M / L as a share of the frame's short side (None = M).
ICON_ANCHORS = ("auto", "left", "right", "top", "bottom", "custom")
ICON_ENTRANCES = ("auto", "none", "fade", "pop", "slide", "glitch")    # auto = glitch
ICON_ITEMS_MAX = 5                 # was 3; the AI alone still shows at most 3 (icon.AUTO_MAX)
ICON_SIZES = (0.12, 0.16, 0.20)
ICON_ADDED_MAX = 5
ICON_DISMISSED_MAX = 20
ICON_TWEAKS_MAX = 8                # the user's 5 adds + the AI's 3 picks, each pinned
# The timeline's tracks (icon.Scheduler): 0 is the AI's (one at a time), the
# rest hold the user's icons that show together with another. A track the
# user put an icon on (IconProps.lanes) is a number up to ICON_LANE_MAX, for
# at most ICON_LANES_MAX icons.
ICON_LANES_MAX = 8
ICON_LANE_MAX = 7
ICON_LABEL_MAX = 40
ICON_PURPOSE_MAX = 80
ICON_HOLD = (1.0, 3.0)
# How an icon looks:
#   circle  "Circle": a solid disc, the bold glyph in white (E)
#   chip    "Circle + words": a pill, the glyph and its words (H)
#   badge   the 42 badge — every version made before the circle (the model's
#           default: an unset style keeps meaning the old look)
#   glyph   just the icon
IconStyle = Literal["circle", "chip", "badge", "glyph"]
# The styles the planner and the renderer draw today (normalize refuses any
# other).
ICON_STYLES_READY = ("circle", "chip", "badge", "glyph")
# An icon's own entrance or exit ("grow": a chip's circle pops, then the pill
# unfolds; any other style grows as a pop).
IconMotion = Literal["none", "fade", "pop", "slide", "glitch", "grow"]
# The motions the renderer draws (normalize refuses any other in an icon's
# own look). The scene-wide entrance stays ICON_ENTRANCES: grow is an icon's own.
ICON_MOTIONS_READY = ("none", "fade", "pop", "slide", "glitch", "grow")
CHIP_LABEL_MAX = 24
ICON_LOOKS_MAX = 8
ICON_SIZE_RANGE = (0.08, 0.30)      # one icon's own size, a share of the short side
ICON_USER_HOLD = (0.2, 30.0)        # a length the user set by hand (s)
# A library icon's id (Phosphor's, "piggy-bank") or an AI-drawn one's
# ("c-" + 12 hex of its name, icon_catalog.custom_id).
GLYPH_ID = r"^(?:[a-z0-9-]{1,48}|c-[0-9a-f]{12})$"
_CUSTOM_ID_RE = re.compile(r"^c-[0-9a-f]{12}$")

# What each kind is by default, and how many a recipe may hold.
#   z         behind: part of the set (the speaker stands in front of it);
#             front:  an overlay graphic (hands never cut holes in letters).
#   exact     a guard re-stamps the exact rendered element after its entrance,
#             because Omni redraws letters and marks imperfectly.
#   anchors / entrances   what this kind accepts (None: every one).
#   ready     the harness can draw it today; the rest arrive milestone by
#             milestone and are refused until then.
KIND_RULES: dict[str, dict[str, Any]] = {
    # Up to PATTERN_MAX patterns, each in its own place (see PATTERN_MAX).
    "pattern": {"z": "behind", "exact": False, "exact_allowed": False, "max": PATTERN_MAX,
                "anchors": PATTERN_ANCHORS, "entrances": PATTERN_ENTRANCES, "ready": True},
    "plate":   {"z": "behind", "exact": False, "exact_allowed": False, "max": 1,
                "anchors": None, "entrances": None, "ready": False},
    "slab":    {"z": "behind", "exact": False, "exact_allowed": True, "max": 4,
                "anchors": None, "entrances": None, "ready": False},
    "beads":   {"z": "behind", "exact": False, "exact_allowed": True, "max": 4,
                "anchors": None, "entrances": None, "ready": False},
    "logo":    {"z": "front", "exact": True, "exact_allowed": True, "max": 1,
                "anchors": None, "entrances": None, "ready": False},
    "text":    {"z": "front", "exact": True, "exact_allowed": True, "max": 1,
                "anchors": None, "entrances": None, "ready": False},
    # One element holding up to ICON_ITEMS_MAX icons; stamped exactly by us.
    "icon":    {"z": "behind", "exact": True, "exact_allowed": True, "max": 1,
                "anchors": ICON_ANCHORS, "entrances": ICON_ENTRANCES, "ready": True},
}
MAX_ELEMENTS = 10
HEADING_MAX = 40
SUB_MAX = 140
NOTES_MAX = 600


class _Strict(BaseModel):
    # A typo from a client is an error, not a silently ignored field.
    model_config = ConfigDict(extra="forbid")


class Box(_Strict):
    """A rectangle in frame fractions (0..1, top-left origin)."""
    x: float = Field(ge=-1.0, le=2.0)
    y: float = Field(ge=-1.0, le=2.0)
    w: float = Field(gt=0.0, le=3.0)
    h: float = Field(gt=0.0, le=3.0)


class Placement(_Strict):
    """Where an element goes. A pattern reads `anchor`, and — moved by hand
    (the in-house engine) — `offset` (the move, frame fractions), `size`
    (x the size it was placed at; None = as placed), `rotate` (degrees,
    clockwise) and `box` (its visible box when it was grabbed: the centre is
    the pivot). An icon reads `anchor`, `size` (S/M/L) and `box`."""
    anchor: Anchor = "auto"
    region: Region = "frame"
    size: Optional[float] = Field(default=None, gt=0.0, le=4.0)   # fraction of the SHORT side
    margin: float = Field(default=0.04, ge=0.0, le=0.5)
    box: Optional[Box] = None
    rotate: float = 0.0
    offset: tuple[float, float] = (0.0, 0.0)


class Start(_Strict):
    mode: Literal["clip_start", "at_sec", "at_word"] = "clip_start"
    sec: Optional[float] = Field(default=None, ge=0.0)
    word: Optional[str] = Field(default=None, max_length=60)


class End(_Strict):
    """When a pattern's span ends: with the scene, or at a time (scene s)."""
    mode: Literal["clip_end", "at_sec"] = "clip_end"
    sec: Optional[float] = Field(default=None, ge=0.0, le=600.0)


class Animation(_Strict):
    """An element's entrance and — for a pattern (the in-house engine) — its
    span: `start` (clip_start = 0, or at_sec), `end` (None = the scene's
    end), the entrance's length `duration_sec`, the exit (None = "auto") and
    its length `exit_sec` (None = EXIT_SEC)."""
    entrance: Entrance = "auto"
    start: Start = Start()
    duration_sec: float = Field(default=1.0, gt=0.0, le=6.0)
    end: Optional[End] = None
    exit: Optional[PatternExit] = None
    exit_sec: Optional[float] = Field(default=None, gt=0.0, le=6.0)

    # Left out of the dump while unset, as Guards.stray: every stored recipe
    # (and the 42 default) keeps its canonical form and hash.
    @model_serializer(mode="wrap")
    def _unset_span_left_out(self, handler):
        out = handler(self)
        if isinstance(out, dict):
            for key in ("end", "exit", "exit_sec"):
                if out.get(key) is None:
                    out.pop(key, None)
        return out


class Element(_Strict):
    id: str = Field(pattern=r"^[a-z0-9_-]{1,32}$")
    kind: Kind
    enabled: bool = True
    z: Optional[Z] = None            # None -> the kind's default
    exact: Optional[bool] = None     # None -> the kind's default
    placement: Placement = Placement()
    animation: Animation = Animation()
    props: dict[str, Any] = Field(default_factory=dict)


class Colours(_Strict):
    source: Literal["intro", "brand", "custom"] = "intro"
    custom: Optional[str] = Field(default=None, pattern=r"^#[0-9a-fA-F]{6}$")


class Director(_Strict):
    """Who writes the Omni prompt's opening: the Gemini brief (the approved
    flow — IG brand DNA, past lessons) or the harness's own fixed opening
    (needed once the recipe has a logo or text, which the brief is told
    never to place). Our icons may go with either: they are stamped after
    Omni, and the brief is told to ask Omni for none (icons_by_pipeline)."""
    head: Literal["brief", "harness"] = "brief"
    intensity: Literal["subtle", "balanced", "bold"] = "balanced"
    notes: str = Field(default="", max_length=NOTES_MAX)


class Guards(_Strict):
    speaker_front: bool = True
    face: Literal["shift", "reject", "warn"] = "shift"
    veil: Literal["entrance", "off"] = "entrance"
    bars: Literal["auto", "on", "off"] = "auto"
    # "clean": outside the recipe's drawn layers (the patterns' ink, grown a
    # little, and our icons' boxes) the frame is put back to what Omni was
    # given, so anything it drew there on its own goes (guards.cleans_strays).
    # Opt-in, and left out of the dump while unset: the 42 default's
    # canonical form and hash stay exactly what they were.
    stray: Optional[Literal["clean"]] = None

    @model_serializer(mode="wrap")
    def _unset_stray_left_out(self, handler):
        out = handler(self)
        if isinstance(out, dict) and out.get("stray") is None:
            out.pop("stray", None)
        return out


class Recipe(_Strict):
    recipe_version: Literal[1] = RECIPE_VERSION
    engine: Engine = "omni"
    # A scene, or an intro or outro (made in house, by hand). Only the value
    # is dumped, so widening the pattern moves no hash.
    target: Optional[str] = Field(default=None, pattern=r"^(scene_\d{1,2}|intro|outro)$")
    preset: Optional[str] = Field(default=None, max_length=64)
    seed: Optional[str] = Field(default=None, max_length=120)
    colours: Colours = Colours()
    director: Director = Director()
    guards: Guards = Guards()
    elements: list[Element] = Field(default_factory=list, max_length=MAX_ELEMENTS)

    @field_validator("elements")
    @classmethod
    def _ids_unique(cls, v: list[Element]) -> list[Element]:
        ids = [e.id for e in v]
        if len(ids) != len(set(ids)):
            raise ValueError("element ids must be unique")
        return v


# ---------------------------------------------------------------------------
# The icon element's props (Element.props of kind "icon")
# ---------------------------------------------------------------------------
# Checked READ-ONLY on a copy (normalize never rewrites el.props, so a recipe
# whose props leave a default unset keeps its canonical form), and every model
# forbids unknown keys: a field the client adds by mistake is an error, never
# a silent change to the plan's key (a false "the icons changed since you
# looked"). The planner reads them through icon_props().
#
# WHICH icons show is the live AI set (the icon director + the scheduler)
# plus `added`, minus `dismissed` — by glyph id, never frozen by an edit, so
# the AI keeps choosing and ordering whatever the user has not touched. Their
# order is always the order they are said in; a pin (`tweaks`) moves one to
# another mention of its own idea, or — only when the user chose it — off its
# word to a time (`off_word` + `sec`). `looks` gives one icon a look of its
# own (style, colour, words, size, in front, its motion) over the element's
# scene-wide ones — by glyph id too, so an AI pick keeps its look whenever the
# AI shows it. `lanes` puts one on a track of the timeline (a track of its
# own may show together with the others). `resolved` is the plan frozen at
# Generate (written by the router, read by the run); a draft never holds it
# and a preview or a plan ignores it. `keep` ("Keep v3's icons") names a version whose icons show
# exactly as it froze them — no planner, no AI: what a draft carrying on
# from that version sets when a new plan would place them otherwise (every
# icon edit in the studio drops it). Left out of the dump while unset, as
# every field added since.

class IconGlyph(_Strict):
    """A library icon (Phosphor fill, by id) or one the AI drew (c-<12 hex>)."""
    source: Literal["library", "custom"]
    id: str = Field(pattern=GLYPH_ID)

    @model_validator(mode="after")
    def _source_matches_id(self) -> "IconGlyph":
        if bool(_CUSTOM_ID_RE.match(self.id)) != (self.source == "custom"):
            raise ValueError("a drawn icon's id is c-<12 hex> with source custom; a library icon's is not")
        return self


class IconAdd(_Strict):
    """An icon the user added (from the library, a name, or drawn). `at`:
    the moment it was added at (the studio's playhead, scene seconds): where
    one that nothing they say is tied to shows (the clear time nearest it);
    unset, the earliest clear time, as always. Left out of the dump while
    unset, so an add made before it keys as it did."""
    glyph: IconGlyph
    label: str = Field(default="", max_length=ICON_LABEL_MAX)
    at: Optional[float] = Field(default=None, ge=0.0, le=600.0)

    @model_serializer(mode="wrap")
    def _unset_at_left_out(self, handler):
        out = handler(self)
        if isinstance(out, dict) and out.get("at") is None:
            out.pop("at", None)
        return out


class IconTweak(_Strict):
    """The user's pins for one icon, by its glyph id. `w`: the word (an index
    into the scene's words, whose digest is `words_digest`) — another mention
    of the icon's own idea; `off_word` + `sec`: shown at that time, off any
    word (only ever set by the user choosing it); `box`: where they dragged
    it (the body's box); `hold_sec`: how long it stays fully in — a length
    set by hand, 0.2 s to 30 s (ICON_USER_HOLD; the scene's own hold,
    IconProps.hold_sec, stays 1-3 s)."""
    w: Optional[int] = Field(default=None, ge=0)
    words_digest: Optional[str] = Field(default=None, max_length=64)
    off_word: bool = False
    sec: Optional[float] = Field(default=None, ge=0.0, le=600.0)
    box: Optional[Box] = None
    hold_sec: Optional[float] = Field(default=None, ge=ICON_USER_HOLD[0], le=ICON_USER_HOLD[1])

    @model_validator(mode="after")
    def _off_word_needs_a_time(self) -> "IconTweak":
        if self.off_word and self.sec is None:
            raise ValueError("off_word needs sec (the time it shows at)")
        if self.sec is not None and not self.off_word:
            raise ValueError("sec is only for an icon shown off its word (off_word)")
        return self


class IconFx(_Strict):
    """An icon's entrance and exit (None = the entrance mirrored, as every
    version before per-icon exits)."""
    entrance: IconMotion
    seed: str = Field(max_length=200)
    exit: Optional[IconMotion] = None

    @model_serializer(mode="wrap")
    def _unset_exit_left_out(self, handler):
        out = handler(self)
        if isinstance(out, dict) and out.get("exit") is None:
            out.pop("exit", None)
        return out


# IconResolved fields added with the circle and the chip, and the value that
# draws exactly what every earlier version drew. Left out of the dump while
# at it, so a plan made today freezes (and hashes) exactly as before.
_RESOLVED_SAME_AS_BEFORE: dict[str, Any] = {"weight": "fill", "text": None, "font": None,
                                            "layer": "behind", "look_v": 0}


class IconResolved(_Strict):
    """One icon exactly as the run stamps it (icon_render.Stamper): scene
    seconds, the box in frame fractions, the colours, the entrance. Written
    by the router at Generate from the plan the user saw.

    The circle and the chip add: `weight` (the glyph's Phosphor weight),
    `text` + `font` (a chip's words as drawn, and the label font's key),
    `layer` (behind the speaker, or in front where the face stays clear) and
    `look_v` (the sprite recipe: 0 = the badge / glyph as always, 1 = the
    circle and chip v1). Their defaults reproduce today's pixels."""
    glyph: IconGlyph
    label: str = Field(default="", max_length=ICON_LABEL_MAX)
    from_sec: float = Field(ge=0.0)
    settle_sec: float = Field(ge=0.0)
    to_sec: float = Field(ge=0.0)
    box: Box
    fill: str = Field(pattern=r"^#[0-9a-fA-F]{6}$")
    ink: str = Field(pattern=r"^#[0-9a-fA-F]{6}$")
    fx: IconFx
    style: IconStyle = "badge"
    word: Optional[str] = Field(default=None, max_length=60)
    purpose: str = Field(default="", max_length=ICON_PURPOSE_MAX)
    # The most of it the speaker covers in its window (a restore that fails
    # stamps without the matte, and then leaves out any above 3 %).
    max_overlap: float = Field(default=0.0, ge=0.0, le=1.0)
    weight: Literal["fill", "bold"] = "fill"
    text: Optional[str] = Field(default=None, min_length=1, max_length=CHIP_LABEL_MAX + 1)
    font: Optional[str] = Field(default=None, min_length=1, max_length=64)
    layer: Literal["behind", "front"] = "behind"
    look_v: int = Field(default=0, ge=0, le=9)

    @model_validator(mode="after")
    def _in_order(self) -> "IconResolved":
        if not self.from_sec <= self.settle_sec < self.to_sec:
            raise ValueError("an icon comes in (from_sec), settles, then goes (to_sec), in that order")
        if self.style in ("circle", "chip") and self.look_v < 1:
            raise ValueError("a circle or a chip is drawn with look_v 1 or later")
        if self.style == "chip" and (self.text is None or self.font is None):
            raise ValueError("a chip carries its words (text) and their font")
        return self

    @model_serializer(mode="wrap")
    def _same_as_before_left_out(self, handler):
        out = handler(self)
        if isinstance(out, dict):
            for key, same in _RESOLVED_SAME_AS_BEFORE.items():
                if key in out and out[key] == same:
                    out.pop(key)
        return out


class IconLook(_Strict):
    """One icon's own look, by its glyph id (IconProps.looks). Every field is
    optional: None is the scene's (the element's style, colour, size and
    entrance; the ink worked out from the fill; behind the speaker; an exit
    that mirrors the entrance)."""
    style: Optional[IconStyle] = None
    colour: Optional[str] = Field(default=None, pattern=r"^#[0-9a-fA-F]{6}$")    # the body
    ink: Optional[str] = Field(default=None, pattern=r"^#[0-9a-fA-F]{6}$")       # glyph + words
    label: Optional[str] = Field(default=None, min_length=1, max_length=CHIP_LABEL_MAX)
    size: Optional[float] = Field(default=None, ge=ICON_SIZE_RANGE[0], le=ICON_SIZE_RANGE[1])
    layer: Optional[Literal["behind", "front"]] = None
    enter: Optional[IconMotion] = None
    leave: Optional[IconMotion] = None


class IconProps(_Strict):
    ai: bool = True                                   # False: the keyword list only (free)
    # The scene's icon look; unset = the 42 badge (every older recipe), new
    # drafts write "circle".
    style: IconStyle = "badge"
    colour: str = "auto"                              # "auto" (brand + scene) or #rrggbb
    omni_own: Literal["hide", "keep"] = "hide"        # hide: Omni is told to draw none
    hold_sec: Optional[float] = Field(default=None, ge=ICON_HOLD[0], le=ICON_HOLD[1])
    added: list[IconAdd] = Field(default_factory=list, max_length=ICON_ADDED_MAX)
    dismissed: list[str] = Field(default_factory=list, max_length=ICON_DISMISSED_MAX)
    tweaks: dict[str, IconTweak] = Field(default_factory=dict)
    # Each icon's own look, by glyph id (at most ICON_LOOKS_MAX).
    looks: dict[str, IconLook] = Field(default_factory=dict)
    # The track the user put an icon on, by glyph id (1 to ICON_LANE_MAX:
    # one of its own, so it may show with another; 0: the AI's, one at a
    # time). It is the user's from then on (icon.Scheduler).
    lanes: dict[str, int] = Field(default_factory=dict)
    # "Keep v3's icons": that version's (by id) exactly as it froze them.
    keep: Optional[int] = Field(default=None, ge=1)
    resolved: Optional[list[IconResolved]] = Field(default=None, max_length=ICON_ITEMS_MAX)

    @field_validator("colour")
    @classmethod
    def _colour(cls, v: str) -> str:
        if v != "auto" and not _HEX_RE.match(v or ""):
            raise ValueError("That colour isn't one we can use — pick another.")
        return v

    @field_validator("dismissed")
    @classmethod
    def _dismissed(cls, v: list[str]) -> list[str]:
        bad = [g for g in v if not re.match(GLYPH_ID, g or "")]
        if bad:
            raise ValueError(f"not an icon id: {bad[0]!r}")
        if len(v) != len(set(v)):
            raise ValueError("an icon is removed once")
        return v

    @field_validator("tweaks")
    @classmethod
    def _tweaks(cls, v: dict[str, IconTweak]) -> dict[str, IconTweak]:
        if len(v) > ICON_TWEAKS_MAX:
            raise ValueError(f"at most {ICON_TWEAKS_MAX} icons can be pinned")
        bad = [g for g in v if not re.match(GLYPH_ID, g or "")]
        if bad:
            raise ValueError(f"not an icon id: {bad[0]!r}")
        return v

    @field_validator("looks")
    @classmethod
    def _looks(cls, v: dict[str, IconLook]) -> dict[str, IconLook]:
        if len(v) > ICON_LOOKS_MAX:
            raise ValueError(f"at most {ICON_LOOKS_MAX} icons can have their own look")
        bad = [g for g in v if not re.match(GLYPH_ID, g or "")]
        if bad:
            raise ValueError(f"not an icon id: {bad[0]!r}")
        return v

    @field_validator("lanes", mode="before")
    @classmethod
    def _lanes(cls, v: Any) -> Any:
        if not isinstance(v, dict):
            return v
        if len(v) > ICON_LANES_MAX:
            raise ValueError(f"at most {ICON_LANES_MAX} icons can be put on a track")
        bad = [g for g in v if not re.match(GLYPH_ID, g or "")]
        if bad:
            raise ValueError(f"not an icon id: {bad[0]!r}")
        # A whole track number (a bool or 1.5 is not one).
        off = [g for g, n in v.items() if isinstance(n, bool) or not isinstance(n, int)
               or not 0 <= n <= ICON_LANE_MAX]
        if off:
            raise ValueError(f"a track is a whole number from 0 to {ICON_LANE_MAX} ({off[0]})")
        return v

    @model_validator(mode="after")
    def _added_once(self) -> "IconProps":
        ids = [a.glyph.id for a in self.added]
        if len(ids) != len(set(ids)):
            raise ValueError("an icon is added once")
        both = sorted(set(ids) & set(self.dismissed))
        if both:
            raise ValueError(f"{both[0]} is both added and removed")
        return self


class RecipeError(ValueError):
    """A recipe the harness cannot run, with one message per element."""

    def __init__(self, errors: list[dict]):
        self.errors = errors
        super().__init__("; ".join(f"{e.get('element_id') or 'recipe'}: {e['message']}" for e in errors))


ENGINE_NOT_READY = "Making it in house isn't ready yet."
OMNI_OFF = "Omni is switched off — new versions are made in house, for $0."


def runnable(recipe: Recipe) -> None:
    """Raises RecipeError when the recipe's engine is not built yet
    (ENGINE_READY) or switched off (Omni, omni_on()), or an enabled element
    is of a kind the harness cannot draw yet (KIND_RULES "ready").
    normalize() does not refuse them, so a saved recipe survives the
    milestone that makes them real — and a recipe for an engine that is not
    there is never drawn by another one."""
    errors = []
    if not ENGINE_READY.get(recipe.engine, False):
        errors.append({"element_id": None, "field": "engine", "message": ENGINE_NOT_READY})
    elif recipe.engine == "omni" and not omni_on():
        errors.append({"element_id": None, "field": "engine", "message": OMNI_OFF})
    errors += [{"element_id": e.id, "field": "kind", "message": f"{e.kind} is not available yet"}
               for e in recipe.elements if e.enabled and not KIND_RULES[e.kind]["ready"]]
    if errors:
        raise RecipeError(errors)


def _number(v: Any) -> Optional[float]:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    return float(v)


def _layout_errors(pin: Any) -> list[tuple[str, str]]:
    """(field, message) for a pinned layout that is not a Layout.public()."""
    if not isinstance(pin, dict):
        return [("props.layout", "a pinned place must be an object")]
    out: list[tuple[str, str]] = []
    unknown = sorted(k for k in pin if k not in LAYOUT_KEYS)
    if unknown:
        out.append(("props.layout", f"unknown key(s) {', '.join(unknown)}"))
    if pin.get("placement") not in LAYOUT_PLACEMENTS:
        out.append(("props.layout.placement", f"placement must be one of {', '.join(LAYOUT_PLACEMENTS)}"))
    scale = _number(pin.get("scale"))
    if "scale" in pin and (scale is None or not 0.0 < scale <= LAYOUT_SCALE_MAX):
        out.append(("props.layout.scale", f"scale must be above 0 and at most {LAYOUT_SCALE_MAX:g}"))
    reach = _number(pin.get("reach"))
    if "reach" in pin and (reach is None or not 0.0 < reach <= 1.0):
        out.append(("props.layout.reach", "reach must be above 0 and at most 1"))
    if "anchor" in pin and pin["anchor"] not in ("left", "right"):
        out.append(("props.layout.anchor", "anchor must be left or right"))
    rotate = pin.get("rotate", 0)
    if isinstance(rotate, bool) or rotate not in (0, 90, 180, 270):
        out.append(("props.layout.rotate", "rotate must be 0, 90, 180 or 270"))
    if "fit" in pin and pin["fit"] not in LAYOUT_FITS:
        out.append(("props.layout.fit", f"fit must be one of {', '.join(LAYOUT_FITS)}"))
    if "valign" in pin and pin["valign"] not in ("top", "bottom"):
        out.append(("props.layout.valign", "valign must be top or bottom"))
    return out


def _pattern_prop_errors(el: Element) -> list[dict]:
    """A pattern's props checked READ-ONLY: a copy is read and el.props is
    never rewritten, so a valid recipe's canonical form (and the 42
    default's hash) stays exactly what the client sent. A bad colour used
    to be ignored silently by colour_override, which looked like "no
    change"."""
    props = dict(el.props)
    errors: list[tuple[str, str]] = []
    unknown = sorted(k for k in props if k not in PATTERN_PROPS)
    if unknown:
        errors.append(("props", f"unknown pattern setting(s): {', '.join(unknown)}"))
    slug = props.get("slug")
    if slug is not None and not (isinstance(slug, str) and _SLUG_RE.match(slug)):
        errors.append(("props.slug", "a pattern slug is 1-64 of a-z, 0-9, _ and -"))
    colour = props.get("colour")
    if colour is not None and colour != "auto" and not (isinstance(colour, str) and _HEX_RE.match(colour)):
        # Shown to the owner as it is ("auto" or #rrggbb is the rule).
        errors.append(("props.colour", "That colour isn't one we can use — pick another."))
    if props.get("layout") is not None:
        errors += _layout_errors(props["layout"])
    shuffle = props.get("shuffle")
    if shuffle is not None and not (isinstance(shuffle, str) and _SHUFFLE_RE.match(shuffle)):
        errors.append(("props.shuffle", "a shuffle tag is 1-16 of a-z and 0-9"))
    follow = props.get("follow")
    if follow is not None and follow not in PATTERN_FOLLOW:
        errors.append(("props.follow", f"follow is one of {', '.join(PATTERN_FOLLOW)}"))
    if props.get("resolved") is not None:
        errors += _pattern_resolved_errors(props["resolved"])
    return [{"element_id": el.id, "field": f, "message": m} for f, m in errors]


def _pattern_resolved_errors(res: Any) -> list[tuple[str, str]]:
    """(field, message) for a frozen pattern (props.resolved) that is not
    what Generate writes: {layout (a Layout.public()), colour, ground?, ink?
    (#rrggbb), hand? ({dx, dy, scale, rotate, pivot?} as drawn), follow?
    (room | screen: resolved, never auto), engine_v?}."""
    if not isinstance(res, dict):
        return [("props.resolved", "a frozen pattern must be an object")]
    out: list[tuple[str, str]] = []
    unknown = sorted(k for k in res if k not in PATTERN_RESOLVED_KEYS)
    if unknown:
        out.append(("props.resolved", f"unknown key(s) {', '.join(unknown)}"))
    out += [(f.replace("props.layout", "props.resolved.layout", 1), m)
            for f, m in _layout_errors(res.get("layout"))]
    for key in ("colour", "ground", "ink"):
        v = res.get(key)
        if (key == "colour" or v is not None) and not (isinstance(v, str) and _HEX_RE.match(v)):
            out.append((f"props.resolved.{key}", f"{key} must be #rrggbb"))
    if res.get("follow") is not None and res["follow"] not in ("room", "screen"):
        out.append(("props.resolved.follow", "a frozen pattern follows the room or the screen"))
    engine_v = res.get("engine_v")
    if engine_v is not None and (isinstance(engine_v, bool) or not isinstance(engine_v, int)
                                 or not 0 <= engine_v <= 99):
        out.append(("props.resolved.engine_v", "engine_v is a whole number from 0 to 99"))
    if res.get("hand") is not None and not _frozen_hand_ok(res["hand"]):
        out.append(("props.resolved.hand", "a frozen hand move is {dx, dy, scale, rotate, pivot?}"))
    return out


def _frozen_hand_ok(hand: Any) -> bool:
    """A hand move as Generate freezes it: {dx, dy, scale, rotate} numbers
    (scale above 0, as the renderer used it after its clamps) and an
    optional pivot [x, y]."""
    if not isinstance(hand, dict) or any(k not in ("dx", "dy", "scale", "rotate", "pivot") for k in hand):
        return False
    nums = [_number(hand.get(k)) for k in ("dx", "dy", "scale", "rotate")]
    if any(v is None or not math.isfinite(v) for v in nums) or not 0.0 < nums[2] <= LAYOUT_SCALE_MAX:
        return False
    pivot = hand.get("pivot")
    return pivot is None or (isinstance(pivot, (list, tuple)) and len(pivot) == 2
                             and all(_number(v) is not None for v in pivot))


def _pattern_place_errors(el: Element) -> list[dict]:
    """A pattern's time and hand move checked READ-ONLY, one message each in
    the owner's words (the studio shows them as they are): a span in scene
    seconds of at least SPAN_MIN_SEC, ramps within RAMP_SEC, a hand move
    within its limits and made from a pinned place (props.layout) whose box
    it was grabbed at (placement.box: the pivot)."""
    a, p = el.animation, el.placement
    errors: list[tuple[str, str]] = []
    if a.start.mode == "at_word":
        errors.append(("animation.start.mode", "A pattern starts at a time."))
    if a.start.mode == "at_sec" and a.start.sec is None:
        errors.append(("animation.start.sec", "Say when the pattern starts."))
    if a.end is not None and a.end.mode == "at_sec":
        if a.end.sec is None:
            errors.append(("animation.end.sec", "Say when the pattern ends."))
        else:
            start, _ = pattern_span(el)
            if a.end.sec - start < SPAN_MIN_SEC - 1e-9:
                errors.append(("animation.end.sec", "A pattern shows for at least half a second."))
    if not RAMP_SEC[0] <= a.duration_sec <= RAMP_SEC[1]:
        errors.append(("animation.duration_sec", "A pattern comes in over 0.2 to 3 seconds."))
    if a.exit_sec is not None and not RAMP_SEC[0] <= a.exit_sec <= RAMP_SEC[1]:
        errors.append(("animation.exit_sec", "A pattern goes out over 0.2 to 3 seconds."))
    if any(not math.isfinite(v) or abs(v) > HAND_OFFSET_MAX for v in p.offset):
        errors.append(("placement.offset", "A pattern moves at most one frame's width or height each way."))
    if not math.isfinite(p.rotate) or abs(p.rotate) > HAND_ROTATE_MAX:
        errors.append(("placement.rotate", "A pattern turns at most half a turn each way."))
    if p.size is not None and not HAND_SCALE[0] <= p.size <= HAND_SCALE[1]:
        errors.append(("placement.size", "A pattern is sized from half to three times its own size."))
    if is_hand(el) and not (isinstance(el.props.get("layout"), dict) and p.box is not None):
        errors.append(("placement.box", "A pattern moved by hand keeps the place it was moved from."))
    return [{"element_id": el.id, "field": f, "message": m} for f, m in errors]


# What only our own renderer draws, in the owner's words: Omni repaints each
# part from its first frame, so an Omni recipe that asks for one of these is
# refused rather than drawn otherwise than the studio shows it.
HOUSE_ONLY_SLIDE = "Sliding a pattern in comes with making it in house."
HOUSE_ONLY_TIME = "Timing a pattern comes with making it in house."
HOUSE_ONLY_HAND = "Moving a pattern by hand comes with making it in house."
HOUSE_ONLY_FOLLOW = "Following the camera comes with making it in house."


def _omni_pattern_errors(el: Element) -> list[dict]:
    """An Omni recipe's pattern asking for what only the in-house engine
    draws: the slide entrance, a span or an exit, a hand move, a follow."""
    a = el.animation
    errors: list[tuple[str, str]] = []
    if a.entrance not in OMNI_PATTERN_ENTRANCES:
        errors.append(("animation.entrance", HOUSE_ONLY_SLIDE))
    if a.start.mode == "at_sec":
        errors.append(("animation.start", HOUSE_ONLY_TIME))
    for key in ("end", "exit", "exit_sec"):
        if getattr(a, key) is not None:
            errors.append((f"animation.{key}", HOUSE_ONLY_TIME))
    if is_hand(el):
        errors.append(("placement", HOUSE_ONLY_HAND))
    if el.props.get("follow") not in (None, "auto"):
        errors.append(("props.follow", HOUSE_ONLY_FOLLOW))
    return [{"element_id": el.id, "field": f, "message": m} for f, m in errors]


ICONS_OWN_TIMES = "Icons have their own times."


def _icon_time_errors(el: Element) -> list[dict]:
    """An icon element takes no element-level span or exit: each icon has
    its own time (props.tweaks)."""
    a = el.animation
    fields = (["animation.start"] if a.start.mode != "clip_start" else []) + [
        f"animation.{key}" for key in ("end", "exit", "exit_sec") if getattr(a, key) is not None]
    return [{"element_id": el.id, "field": f, "message": ICONS_OWN_TIMES} for f in fields]


ICON_STYLE_NOT_READY = "The circle icons aren't ready yet."
ICON_MOTION_NOT_READY = "That entrance isn't ready yet."


def _icon_prop_errors(el: Element) -> list[dict]:
    """An icon element checked READ-ONLY: its props validated as IconProps
    on a copy (el.props is never rewritten), its place and size. One error
    per problem, with the field path the studio can point at."""
    errors: list[tuple[str, str]] = []
    try:
        props = IconProps.model_validate(dict(el.props))
    except ValidationError as e:
        props = None
        for err in e.errors(include_url=False):
            loc = ".".join(str(p) for p in err.get("loc") or ())
            msg = str(err.get("msg") or "not valid")
            errors.append((f"props.{loc}" if loc else "props", msg.removeprefix("Value error, ")))
    if props is not None:
        # A style the planner does not draw yet (ICON_STYLES_READY).
        styles = [("props.style", props.style)] + [
            (f"props.looks.{g}.style", look.style) for g, look in props.looks.items() if look.style]
        errors += [(f, ICON_STYLE_NOT_READY) for f, s in styles if s not in ICON_STYLES_READY]
        # A motion the renderer does not draw yet (ICON_MOTIONS_READY).
        motions = [(f"props.looks.{g}.{k}", getattr(look, k)) for g, look in props.looks.items()
                   for k in ("enter", "leave") if getattr(look, k)]
        errors += [(f, ICON_MOTION_NOT_READY) for f, m in motions if m not in ICON_MOTIONS_READY]
    if el.z == "front":
        errors.append(("z", "icons always sit behind the speaker"))
    size = el.placement.size
    if size is not None and not any(abs(size - s) < 1e-9 for s in ICON_SIZES):
        errors.append(("placement.size", "an icon's size is S, M or L (0.12, 0.16 or 0.20)"))
    return [{"element_id": el.id, "field": f, "message": m} for f, m in errors]


def pattern_cap_message() -> str:
    """Too many patterns, in the owner's words (the studio shows it)."""
    return f"A scene takes up to {PATTERN_MAX} patterns."


OMNI_OFF_ICONS = "Omni is off, so there are no Omni icons to keep."


def normalize(recipe: Recipe | dict) -> Recipe:
    """The recipe with every kind default filled in (z, exact) and the
    per-kind limits checked. Raises RecipeError listing what is wrong.

    The in-house engine ("house") draws no Omni prompt: the director's
    settings and the veil / stray guards are ignored (never refused), and
    Omni's own icons cannot be kept. An Omni recipe cannot ask for what only
    the in-house engine draws (_omni_pattern_errors)."""
    r = recipe if isinstance(recipe, Recipe) else Recipe.model_validate(recipe)
    house = r.engine == "house"
    errors: list[dict] = []
    counts: dict[str, int] = {}
    out: list[Element] = []
    for el in r.elements:
        rules = KIND_RULES[el.kind]
        counts[el.kind] = counts.get(el.kind, 0) + 1
        if counts[el.kind] > rules["max"]:
            errors.append({"element_id": el.id, "field": "kind",
                           "message": pattern_cap_message() if el.kind == "pattern"
                           else f"at most {rules['max']} {el.kind} element(s)"})
        exact = rules["exact"] if el.exact is None else el.exact
        if exact and not rules["exact_allowed"]:
            errors.append({"element_id": el.id, "field": "exact",
                           "message": f"{el.kind} cannot be stamped exactly"})
        if el.placement.anchor == "custom" and el.placement.box is None:
            errors.append({"element_id": el.id, "field": "placement.box",
                           "message": "a custom place needs a box"})
        if rules["anchors"] is not None and el.placement.anchor not in rules["anchors"]:
            errors.append({"element_id": el.id, "field": "placement.anchor",
                           "message": f"a {el.kind} cannot go {el.placement.anchor!r}"})
        if rules["entrances"] is not None and el.animation.entrance not in rules["entrances"]:
            errors.append({"element_id": el.id, "field": "animation.entrance",
                           "message": f"a {el.kind} has no {el.animation.entrance!r} entrance"})
        if el.kind == "pattern":
            errors += _pattern_prop_errors(el)
            errors += _pattern_place_errors(el)
            if not house:
                errors += _omni_pattern_errors(el)
        if el.kind == "icon":
            errors += _icon_prop_errors(el)
            errors += _icon_time_errors(el)
            if house and el.props.get("omni_own") == "keep":
                errors.append({"element_id": el.id, "field": "props.omni_own", "message": OMNI_OFF_ICONS})
        if el.kind == "text":
            for key, cap in (("heading", HEADING_MAX), ("sub", SUB_MAX)):
                val = el.props.get(key)
                if isinstance(val, str) and len(val) > cap:
                    errors.append({"element_id": el.id, "field": f"props.{key}",
                                   "message": f"{key} is longer than {cap} characters"})
        out.append(el.model_copy(update={"z": el.z or rules["z"], "exact": exact}))
    # Icons are stamped by us after Omni, so the brief may open a recipe that
    # has them (it is told the pipeline places them: icons_by_pipeline). The
    # in-house engine has no brief.
    if not house and r.director.head == "brief" and any(
            e.enabled and e.kind in ("logo", "text") for e in out):
        errors.append({"element_id": None, "field": "director.head",
                       "message": "the brief never places a logo or text — "
                                  "use the harness opening for this recipe"})
    if r.colours.source == "custom" and not r.colours.custom:
        errors.append({"element_id": None, "field": "colours.custom",
                       "message": "a custom colour needs a hex value"})
    if errors:
        raise RecipeError(errors)
    return r.model_copy(update={"elements": out})


def pattern_elements(recipe: Recipe) -> list[Element]:
    """The recipe's switched-on patterns in list order — which is also the
    order they are placed in (each avoids the ones before it) and drawn in
    (the first is the back-most)."""
    return [e for e in recipe.elements if e.kind == "pattern" and e.enabled]


def pattern_element(recipe: Recipe) -> Optional[Element]:
    """The recipe's (first) pattern, or None when it has none switched on."""
    return next(iter(pattern_elements(recipe)), None)


def pattern_span(el: Element) -> tuple[float, Optional[float]]:
    """(start, end) of a pattern's span in scene seconds, as the recipe says
    it — end None: to the scene's end. Not clamped to any scene (timing()
    in elements/pattern.py does that, with the scene's length). Every
    pattern made before spans is (0.0, None): the whole scene."""
    start = el.animation.start
    s = float(start.sec) if start.mode == "at_sec" and start.sec is not None else 0.0
    end = el.animation.end
    e = float(end.sec) if end is not None and end.mode == "at_sec" and end.sec is not None else None
    return s, e


def spans_overlap(a: tuple[float, Optional[float]], b: tuple[float, Optional[float]]) -> bool:
    """Whether two spans (pattern_span) show at the same time. Spans that
    only touch (one ends at 1.5 s, the next starts at 1.5 s) do not."""
    a_end = math.inf if a[1] is None else a[1]
    b_end = math.inf if b[1] is None else b[1]
    return a[0] < b_end - 1e-9 and b[0] < a_end - 1e-9


@dataclass(frozen=True)
class Hand:
    """A pattern moved by hand, as the recipe says it: a move (frame
    fractions), a size (x the size it was placed at), a turn (degrees,
    clockwise) about a pivot (frame fractions: the centre of the visible
    box it was grabbed at). The renderer builds its matrix from this."""
    dx: float = 0.0
    dy: float = 0.0
    scale: float = 1.0
    rotate: float = 0.0
    pivot: tuple[float, float] = (0.5, 0.5)

    @property
    def identity(self) -> bool:
        return self.dx == 0.0 and self.dy == 0.0 and self.scale == 1.0 and self.rotate == 0.0


def is_hand(el: Element) -> bool:
    """Whether a pattern was moved, sized or turned by hand."""
    p = el.placement
    return el.kind == "pattern" and (tuple(p.offset) != (0.0, 0.0) or p.rotate != 0.0 or p.size is not None)


def hand_of(el: Element) -> Hand:
    """A pattern's hand move (Hand(): none)."""
    if not is_hand(el):
        return Hand()
    p = el.placement
    pivot = (p.box.x + p.box.w / 2.0, p.box.y + p.box.h / 2.0) if p.box is not None else (0.5, 0.5)
    return Hand(dx=float(p.offset[0]), dy=float(p.offset[1]),
                scale=float(p.size) if p.size is not None else 1.0, rotate=float(p.rotate), pivot=pivot)


def new_pattern_id(recipe: Recipe) -> str:
    """The id for one more pattern: "pattern", then "pattern-2", "pattern-3"
    — the next that is free."""
    taken = {e.id for e in recipe.elements}
    if "pattern" not in taken:
        return "pattern"
    n = 2
    while f"pattern-{n}" in taken:
        n += 1
    return f"pattern-{n}"


def icon_element(recipe: Recipe, *, enabled: bool = True) -> Optional[Element]:
    """The recipe's icon element (there is at most one) — only when it is
    switched on, unless `enabled` is False — or None."""
    return next((e for e in recipe.elements
                 if e.kind == "icon" and (e.enabled or not enabled)), None)


def icon_props(el: Element) -> IconProps:
    """An icon element's props as IconProps — a validated COPY with every
    default filled in; el.props is not touched. Raises RecipeError."""
    errors = _icon_prop_errors(el) if el.kind == "icon" else [
        {"element_id": el.id, "field": "kind", "message": "not an icon element"}]
    props_errors = [e for e in errors if e["field"].startswith("props")]
    if props_errors:
        raise RecipeError(props_errors)
    return IconProps.model_validate(dict(el.props))


def icons_by_pipeline(recipe: Recipe) -> bool:
    """Whether the icons are ours, so Omni is told to draw none (and the
    brief knows it): a switched-on icon element that hides Omni's own."""
    el = icon_element(recipe)
    if el is None:
        return False
    try:
        return icon_props(el).omni_own == "hide"
    except RecipeError:
        return False


def canonical(recipe: Recipe) -> str:
    """Sorted-key JSON of the recipe — two recipes that mean the same thing
    have the same canonical form."""
    return json.dumps(recipe.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))


def recipe_hash(recipe: Recipe) -> str:
    return hashlib.sha256(canonical(recipe).encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Presets and the legacy options
# ---------------------------------------------------------------------------

def preset_42_default(target: Optional[str] = None) -> Recipe:
    """The owner-approved run as a recipe (read-only built-in preset): Omni's
    — every version made before the in-house engine, and the golden lock
    (tests/test_rebrand_golden.py)."""
    return normalize(Recipe(
        target=target, preset="42-default",
        elements=[Element(id="pattern", kind="pattern",
                          props={"slug": "structure", "layout": None, "colour": "auto"})],
    ))


def preset_42_house(target: Optional[str] = None) -> Recipe:
    """The 42 default made in house (the owner, 2026-09-29: no Omni for new
    work): the same element — structure, its own place, entrance and the
    intro's colour — drawn by our own renderer over the untouched footage.
    What the studio starts every scene from."""
    return normalize(preset_42_default(target).model_copy(update={"engine": "house"}))


BUILTIN_PRESETS = {"42-default": preset_42_house}


def recipe_from_options(options: dict) -> Recipe:
    """A legacy request (pattern_on / pattern / pattern_animation / intensity
    / notes) as the recipe that runs it. Veo and brand-frame options are not
    part of a scene recipe and are dropped."""
    target = options.get("target")
    pattern_on = options.get("pattern_on", True)
    elements = []
    if pattern_on:
        animation = options.get("pattern_animation") or "auto"
        elements.append(Element(
            id="pattern", kind="pattern",
            animation=Animation(entrance=animation),
            props={"slug": options.get("pattern") or "structure", "layout": None, "colour": "auto"},
        ))
    return normalize(Recipe(
        target=target if isinstance(target, str) and target.startswith("scene_") else None,
        preset="42-default" if pattern_on and (options.get("pattern") or "structure") == "structure"
        and not options.get("pattern_animation") else None,
        director=Director(intensity=options.get("intensity") or "balanced",
                          notes=(options.get("notes") or "")[:NOTES_MAX]),
        elements=elements,
    ))


def options_from_recipe(recipe: Recipe) -> dict:
    """The legacy option keys a recipe implies, written beside the recipe on
    the experiment row so old list queries and old clients keep working."""
    patterns = pattern_elements(recipe)
    first = patterns[0] if patterns else None
    entrance = first.animation.entrance if first else None
    out = {
        "target": recipe.target or "reel",
        "run_omni": True,
        "run_brand": False,
        "pattern_on": bool(first),
        "pattern": (first.props.get("slug") if first else None) or "structure",
        "pattern_animation": None if entrance in (None, "auto") else entrance,
        "intensity": recipe.director.intensity,
        "notes": recipe.director.notes,
    }
    # Several patterns: every slug, so a list query can say so ("pattern" is
    # still the first). One pattern writes exactly what it always did.
    if len(patterns) > 1:
        out["patterns"] = [p.props.get("slug") or "structure" for p in patterns]
    # Icons stamped by us: Omni is told to draw none and the brief knows the
    # pipeline places them. Absent otherwise (the 42 default is unchanged).
    if icons_by_pipeline(recipe):
        out["icons_by_pipeline"] = True
    # The stray-drawing clean-up, likewise only when it is on.
    if recipe.guards.stray == "clean":
        out["stray_clean"] = True
    # Made in house: said on the row, so a list query (and the studio) can
    # tell it from Omni's. Absent for Omni (every older row is unchanged).
    if recipe.engine == "house":
        out["engine"] = "house"
    return out


def json_schema() -> dict:
    return Recipe.model_json_schema()


def icon_props_schema() -> dict:
    """The icon element's props (Element.props of kind "icon")."""
    return IconProps.model_json_schema()
