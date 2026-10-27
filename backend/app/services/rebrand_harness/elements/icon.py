"""The icon element — which icons a scene shows, on which spoken words, where
and in what colours: worked out from what is SAID, never guessed.

  plan(el, ctx) -> IconPlan       one scene's plan (pure, deterministic)
  materialise(recipe, plan)       the plan frozen into props.resolved (Generate)
  lanes_of(plan)                  each frozen icon's track, kept beside them
                                  (Generate: options.icon_lanes)
  plan_for_run(recipe, make_ctx)  what a run stamps: props.resolved, and only it
  stampers(resolved, chunks, …)   one icon_render.Stamper per chunk (the run's
                                  hand-off: Stamper.under(frame, t) inside
                                  restore_speaker, after the veil, before the
                                  speaker is blended back on top)
  reorder(el, ctx, glyph, to)     "show this one first": the pins that do it
                                  without breaking what it means, or the
                                  question to ask

WHICH icons (membership) is the live AI set — the icon director's auto ideas
(director.auto_ideas: kept, priority >= 3, and a STRONG keyword or priority
>= 4) — plus props.added, minus props.dismissed, by glyph id. Nothing is
frozen by an edit, so the AI keeps choosing and ordering whatever the user
has not touched. Without the AI there are no automatic icons at all: the
keyword hits are only suggestions (ideas, state "available").

WHEN (schedule, 30 fps): an icon comes in over IN_F frames and lands LAND_F
frames before its word; it must settle within [onset - 0.20, onset + 0.30] s
(else it is not "on point" and is not shown there). It may land on any word of
its idea's phrase — the head word first — or on another mention of the same
idea the director confirmed. It holds until the phrase ends + 0.3 s (1.0 to
3.0 s; or the user's hold) and leaves over OUT_F frames. Never two at once:
0.30 s of clear air between two key ideas (priority >= 4, or the user's),
0.60 s otherwise. Nothing before 0.5 s, the first settle at 1.5 s or later
while the pattern comes in, everything gone by the end - 0.30 s. Patterns
timed on the timeline (the in-house engine) bring their own zones
(pattern_zones, IconCtx.zones): no settle while one comes in — from its
start for 1.5 s, or its entrance + 0.5 s when that is longer — nor while
one goes out; the 42 default's zones are exactly the one above. At most
clamp(round(voiced / 3.5), 1, 3) automatic icons and ICON_ITEMS_MAX in all.
Who wins: the user's pins > the user's adds > the AI by priority > keywords;
each loser says why ("No room right after 'Free'"). Among the ways a set can
fit, the one closest to every word wins (the sum of squared frame drifts).
An icon the user timed by hand — off its word, at a time and for a length
they chose (props.tweaks: off_word + sec, hold_sec 0.2-30 s) — is a FIXED
slot: placed first, exactly there, inside the scene; the opening, the
pattern's entrance and the end are only warned about. Fixed slots may
overlap one another; every other icon keeps clear of them (their window ±
the clear air), as it does of the rest.

TRACKS (the owner's call, 2026-10-01: an icon the user adds always shows).
All of the above is track 0, the AI's: one at a time. The AI's picks only
ever go there, and keep clear of every other track. One of the user's that
has no room on track 0, or that they put on a track (props.lanes), is never
left out (Scheduler._own). It takes its own best time, clear of no other
icon: on its word; with its words all in a zone, the allowed time nearest
them, keeping its word (the item's `shifted`: "moved to 1.5 s, after the
pattern comes in", with the way back: shown at its word, timed by hand);
with no allowed time in the whole scene, at its word as if timed by hand.
It goes on its pinned track when that is free there, on track 0 when it
keeps clear of everything on it, else on the first free track. An add never
said shows at the moment it was added at (IconAdd.at) as one said shows on
its word: on track 0 when it fits there at that moment, else on a track of
its own there (two added at one moment show together, on two tracks); moved
off it only to the allowed time nearest it (the opening, a zone, the end),
and then said, with the way back ("Show at 0.2 s anyway": that moment,
timed by hand). One said, added at a moment, keeps the word it takes on its
own the same way (never pushed to another mention by those on track 0
before it). One added with no moment (before moments) is placed as it
always was: the earliest clear time, or any mention that fits on track 0.
An AI pick that has no room only because one of the user's shows on a
track of its own then gives way to it, and says so (GIVE_WAY). Nor does a
spot refuse one of the user's (place): with no clear place it shows behind
the speaker where she takes least of it, and says so. Two on screen
together never share a place the planner picked. Tracks are the
timeline's, not a layer: the frozen render list has none (Generate keeps
each one's track beside it, options.icon_lanes: lanes_of, for a version's
read-only timeline).

WHERE (place): the speaker track's fixed spot grid. Hard rules over each
icon's window (± WINDOW_PAD): off the head box grown 0.02 W, the speaker over
at most 3 % of it, inside the frame. Preferences, in order: a box the user
dragged → the Where chip (a side of the grid) → the AI's home spot → the
score (one home for all: +0.30; the pattern's side: +0.15; a calm wall:
+0.15; the eye line: +0.10; ties by the seed). A spot the AI said to avoid
is never picked by the planner itself (Where: Auto, or a shift by the face
guard); on a side the user chose it is only scored down (-0.40). A
letterboxed scene keeps them in the bars while a bar spot is clear. A
preference that is blocked follows the recipe's face guard: shift (moved,
with a warning), reject (dropped), warn (kept, with a warning) — for the
places the planner picks. A box the user put an icon in by hand is kept
EXACTLY there (only kept inside the frame), over the speaker too: behind
her, said ("'Code' is partly behind the speaker at 2.3 s"), with the clear
spot nearest to it offered (the item's `clear`: "Move to a clear spot").
Two icons on screen at once never share a place the planner picked; two
the user put in one place are kept, and said to overlap.

LOOK (look_of): each icon's own look (props.looks, by glyph id) over the
element's scene-wide style, size and entrance — Circle (a disc, the bold
glyph), Circle + words (a chip), or the old badge / bare glyph. Its body box
comes from its spot (Room.box_for): a square at its size, or a chip as tall
as that square's 0.74 and as wide as its words, pinned like the spot. A chip
with no clear place in its window shows as a circle there, and says so. "In
front" of the speaker is the user's own choice (props.looks), so it holds
wherever the icon is, over her face too.

COLOURS: one fill + ink for the icons that take the scene's colour
(icon_render.pick_colours) against the background under each at three
moments, and the pattern where they overlap; an icon with its own colour
keeps it (its ink worked out from it, unless given) and says when it is hard
to see where it is.

The plan is keyed in two halves: the timing (words, director, track,
membership, pins) never waits on the pattern preview; place and colour are
keyed with it and marked `provisional` until the studio has it — a new
scene's icons show at once, then settle.
"""
from __future__ import annotations

import asyncio
import hashlib
import itertools
import json
import logging
import math
import re
from bisect import bisect_right
from dataclasses import dataclass, field, replace
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable, Optional, Sequence

from ... import brand_icons
from .. import recipe as rc

log = logging.getLogger("archivist.rebrand.harness.icon")

PLAN_VERSION = 1

# --- timing (frames at FPS; seconds otherwise) ------------------------------
FPS = 30.0
IN_F = 8                    # the entrance
OUT_F = 6                   # the exit
LAND_F = 2                  # the entrance ends this many frames before the word
TOL_EARLY = 0.20            # a settle this much before the word is still on it...
TOL_LATE = 0.30             # ...and this much after
HOLD_MIN = 1.0
HOLD_MAX = 3.0
HOLD_AFTER = 0.3            # held until the phrase ends + this
HOLD_DEFAULT = 1.6          # an icon with no phrase (shown off its word)
GAP_KEY = 0.30              # clear air between two key ideas (or the user's)
GAP = 0.60                  # ... between any others: "not everywhere"
KEY_PRIORITY = 4
HEAD_CLEAR = 0.5            # nothing on screen before this
ENTRANCE_CLEAR = 1.5        # the first settle, while the pattern comes in
ENTRANCE_AFTER = 0.5        # a longer entrance keeps its ramp + this clear
TAIL_CLEAR = 0.30           # everything gone this long before the end
AUTO_PER_SEC = 3.5          # one automatic icon per this much speech
ITEMS_MAX = rc.ICON_ITEMS_MAX
AUTO_MAX = 3                                   # the AI's own icons in one scene, at most
# An icon the user added that nothing they say is tied to: any clear time in
# the scene, tried every FREE_STEP frames, the earliest first.
FREE_STEP = 3
FREE_COST = 1e-3
MAGNET = 0.5                # a drop this close to its own mention lands on it (client)
RANK_COST = 10_000.0        # any other word of the phrase costs more than any drift
# The rules for what the user set by hand (a box kept exactly; a time off the
# word as a fixed slot). In the timing key only when a recipe has such a pin,
# so every other plan keeps the key it had (and a plan kept by key from
# before these rules is never reused for one).
HAND_RULES = 1
# The tracks (module docstring: TRACKS): in the timing key only when a plan
# placed one of the user's that these rules alone show (another track, a time
# moved out of a zone, a spot the speaker takes), so a plan kept by key from
# before them, which left it out, is never reused for it; every other plan
# keeps the key it had.
TRACK_RULES = 1
# One of the user's added at a moment (IconAdd.at) kept there on a time of
# its own: never said, at that moment; said, on its word (Scheduler._own,
# _at_moment; the round-2 QA, 2026-10-01: before, it went after the others
# on track 0, or to another mention): in the timing key only when a plan
# did, as TRACK_RULES.
MOMENT_RULES = 1
# A time off its word (moved out of a zone) costs more than any on it.
SHIFT_COST = 1e9
# One moved out of a zone looks for a clear spot this close to the nearest
# allowed time, else shows there anyway (place() puts it behind the speaker).
SHIFT_REACH = 0.5

# --- place ------------------------------------------------------------------
WINDOW_PAD = 0.25           # the hard rules hold this long around an icon
SIZES = {"S": 0.12, "M": 0.16, "L": 0.20}
W_HOME = 0.30
W_AVOID = -0.40
W_SIDE = 0.15
W_CALM = 0.15
W_EYE = 0.10
SIDE_REACH = 0.05           # "on the pattern's side": this share of W from its ink
INK_ON = 0.5                # a pattern ink value this high is ink
MOMENT_AFTER = 0.4          # the exact still worth showing: this long after it settles
GROUPS = ("left", "right", "top", "bottom")

# --- the words the studio shows ----------------------------------------------
NO_ROOM_AFTER = "No room right after '{label}'"
NO_ROOM_BEFORE = "No room right before '{label}'"
GIVE_WAY = "Gives way to your '{label}'"
NEAR_END = "Said too close to the end"
NEAR_START = "Said too close to the start"
IN_ENTRANCE = "Said while the pattern comes in"
IN_EXIT = "Said while the pattern goes out"
OVER_LIMIT = "Over the limit for this length ({n})"
OVER_MAX = "A scene shows {n} icons at most"
NO_SPOT = "No clear spot — the speaker's hands are up at {t}"
NO_SPOT_AI = "No clear spot — the AI saw something in each one"
NOT_TIED = "Not tied to anything they say — pick a word"
REMOVED = "Removed by you"
GONE = "Not in the icon library any more — remove it"
W_AI_DOWN = "AI unavailable — suggestions from keywords only"
W_ESTIMATE = "Word timing is estimated — the times may be a little off"
W_NO_TRACK = "Couldn't find the speaker, so the spots are a guess"
W_OMNI_KEEP = "Omni's own icons are kept, so none of ours are added"
W_MOVED = "'{label}' moved: the speaker is in the way at {t}"
W_COVERS = "'{label}' is partly behind the speaker at {t}"
W_COVERS_FACE = "'{label}' is behind the speaker's head at {t}"
W_OVERLAP = "'{a}' and '{b}' overlap at {t}"
W_HAND_START = "'{label}' shows in the opening half second, where you put it"
W_HAND_ENTRANCE = "'{label}' shows while the pattern comes in, where you put it"
W_HAND_EXIT = "'{label}' shows while the pattern goes out, where you put it"
W_HAND_END = "'{label}' is still on screen at the very end, where you put it"
W_SHIFTED = "'{label}' moved to {t}, {why}"
W_NO_CLEAR_TIME = "'{label}' shows on its word: this scene has no clear time for it"
# Why one of the user's moved off its word (the item's `shifted.why`).
SHIFT_WHY = {"entrance": "after the pattern comes in", "exit_after": "after the pattern goes out",
             "exit_before": "before the pattern goes out", "start": "after the opening",
             "end": "so it's gone before the end"}
W_LOST_WORD = "'{label}' lost its word after the new take"
W_REPINNED = "'{label}' was pinned on an older take — it's on '{word}' now"
W_NOT_SAID_THERE = "'{label}' isn't said there, so it stays on '{word}'"
W_GONE = "'{label}' isn't in the icon library any more, so it's left out"
W_CHIP_CIRCLE = "'{label}' is too wide here, so it shows as a circle — shorten its words or place it yourself"
W_FRONT_FACE = "In front, '{label}' would cover the face, so it stays behind"
W_FRONT_UNKNOWN = "'{label}' stays behind: the speaker wasn't found, so in front could cover the face"
W_NO_FONT = "The words' font isn't installed, so '{label}' shows as a circle"
W_ICON_COLOUR = "'{label}' in {colour} is hard to see on the {where}"
ZONE_START = "Too early"
ZONE_ENTRANCE = "The pattern comes in"
ZONE_EXIT = "The pattern goes out"
ZONE_END = "Too close to the end"
# "Why?" (why_of, idea_why): who picked a shown icon and why, and why an
# idea is not shown — the facts, with these words for scripts and reports
# (the studio says them in its own words).
WHY_AI = "Picked by the AI"
WHY_KEYWORD = "A keyword match"
WHY_YOU = "Added by you"
WHY_KEPT = "The AI's pick, kept by you"
WHY_TEAM = "Your team's icon '{label}'"
WHY_VERSION = "Kept from v{n}"
WHY_KEY = "key idea {p} of 5"
WHY_SAID = "said '{word}' at {t}"
WHY_TIMED = "timed by you at {t}"
TEAM_SAID = "Your team's icon — said at {t}"
TEAM_SUGGEST_MAX = 3            # team icons offered per scene (never placed on their own)

_STOP = frozenset("a an the of and or for to in on at with by from your you our we is are be it its "
                  "this that these those as into over up out new".split())
# Tags too broad to say an icon is meant ("places", "objects").
_GENERIC = frozenset("places locations objects things stuff general other misc symbol symbols icon "
                     "icons shape shapes sign signs".split())


def rules() -> dict:
    """The numbers the studio's timeline clamps with, the circle / chip
    geometry its own quick drawing uses while a plan is on its way, and the
    speaker rules an icon dragged by hand is outlined by (GET
    /icons/catalogue)."""
    from .. import icon_render as ir
    from .. import speaker_track

    return {
        "overlap_max": speaker_track.OVERLAP_MAX, "head_grow": speaker_track.HEAD_GROW,
        "person": speaker_track.PERSON,
        "fps": FPS, "in_f": IN_F, "out_f": OUT_F, "land_f": LAND_F,
        "tol_early": TOL_EARLY, "tol_late": TOL_LATE,
        "hold_min": HOLD_MIN, "hold_max": HOLD_MAX, "hold_after": HOLD_AFTER, "hold_default": HOLD_DEFAULT,
        "gap_user": GAP_KEY, "gap_auto": GAP, "key_priority": KEY_PRIORITY,
        "head_clear": HEAD_CLEAR, "entrance_clear": ENTRANCE_CLEAR, "tail_clear": TAIL_CLEAR,
        "auto_per_sec": AUTO_PER_SEC, "items_max": ITEMS_MAX, "added_max": rc.ICON_ADDED_MAX,
        "dismissed_max": rc.ICON_DISMISSED_MAX, "tweaks_max": rc.ICON_TWEAKS_MAX,
        "lanes_max": rc.ICON_LANES_MAX, "lane_max": rc.ICON_LANE_MAX,
        "window_pad": WINDOW_PAD, "magnet": MAGNET, "moment_after": MOMENT_AFTER,
        "user_hold_min": rc.ICON_USER_HOLD[0], "user_hold_max": rc.ICON_USER_HOLD[1],
        "size_min": rc.ICON_SIZE_RANGE[0], "size_max": rc.ICON_SIZE_RANGE[1],
        "label_max": rc.CHIP_LABEL_MAX, "looks_max": rc.ICON_LOOKS_MAX,
        "circle_glyph": ir.CIRCLE_GLYPH, "chip_h": ir.CHIP_H, "chip_glyph": ir.CHIP_GLYPH,
        "chip_pad_glyph": ir.CHIP_PAD_GLYPH, "chip_gap": ir.CHIP_GAP, "chip_pad_words": ir.CHIP_PAD_WORDS,
        "chip_em": ir.CHIP_EM, "chip_em_min": ir.CHIP_EM_MIN, "chip_max_w": ir.CHIP_MAX_W,
        "shadow_sigma": ir.SHADOW_SIGMA, "shadow_alpha": ir.SHADOW_ALPHA, "shadow_dy": ir.SHADOW_DY,
    }


def auto_max(voiced_sec: float) -> int:
    """How many icons the AI may show on its own: one per ~3.5 s of speech,
    at most AUTO_MAX (the user's own adds may take the scene to ITEMS_MAX)."""
    return max(1, min(AUTO_MAX, int(round(max(0.0, voiced_sec) / AUTO_PER_SEC))))


def _sha(body: Any) -> str:
    return hashlib.sha256(json.dumps(body, sort_keys=True, default=str, separators=(",", ":"))
                          .encode("utf-8")).hexdigest()[:16]


def _t(sec: float) -> str:
    return f"{sec:.1f} s"


# ---------------------------------------------------------------------------
# The inputs
# ---------------------------------------------------------------------------

@dataclass
class IconPattern:
    """The pattern(s) the icons sit beside, as the studio's preview drew them."""
    key: str                          # the pattern preview's key ("none" without one)
    colour: Optional[str] = None      # the first pattern's drawn colour
    box: Optional[dict] = None        # the first pattern's ink bounding box (fractions)
    ink: Any = None                   # every pattern's ink, float (H/8, W/8) 0..1
    provisional: bool = False         # a guess: the preview is not made yet
    url: Optional[str] = None         # the pattern layer image, for the moving preview
    # Patterns timed on the timeline (the in-house engine): each one's ink
    # over its span, (t_in, t_out, ink H/8 x W/8) — an icon is judged against
    # the ones on screen with it (ink_at). None: every pattern shows the
    # whole scene, and `ink` is all of them, as it always was.
    timed: Optional[list] = None
    # Ink that shows the whole scene whatever the patterns' spans (a version
    # added to): under every window.
    always: Any = None

    def ink_at(self, t0: float, t1: float) -> Any:
        """The patterns' ink on screen at some moment in [t0, t1)."""
        if self.timed is None:
            return self.ink
        import numpy as np

        out = None if self.always is None else np.asarray(self.always, dtype=np.float32)
        for a, b, ink in self.timed:
            if a < t1 and t0 < b:
                out = ink if out is None else np.maximum(out, ink)
        if out is None and self.ink is not None:
            out = np.zeros_like(np.asarray(self.ink, dtype=np.float32))
        return out

    def inks(self) -> Any:
        """What colour_samples is handed: the one map, or ink_at by window."""
        return self.ink if self.timed is None else self.ink_at


@dataclass
class Backgrounds:
    """The scene's frames at the track's sample times, shrunk to its mask
    size (RGB uint8 (N, H/8, W/8, 3)) — what an icon's colour is judged
    against."""
    key: str
    times: Any
    thumbs: Any


@dataclass
class IconCtx:
    """Everything one scene's plan is made from (the studio gathers it)."""
    words: dict                                   # words.scene_words
    director: dict                                # director.direct
    track: Any = None                             # speaker_track.Track; None = not found
    width: int = 720
    height: int = 1280
    duration: float = 0.0                         # the scene's seconds (0: the words')
    fps: float = FPS
    chunks: list[dict] = field(default_factory=list)   # [{start, seconds}]
    enters: bool = False                          # a pattern comes in on the first chunk
    # Where no icon settles while a pattern comes in or goes out
    # (pattern_zones: [{from, to, why}], seconds) — patterns timed on the
    # timeline. None: from `enters`, as always (0 to ENTRANCE_CLEAR).
    zones: Optional[list[dict]] = None
    seed: str = ""
    face: str = "shift"                           # recipe.guards.face
    pattern: Optional[IconPattern] = None
    backgrounds: Optional[Backgrounds] = None
    base: Optional[dict] = None                   # the version built on, as keyed
    notes: list[str] = field(default_factory=list)    # warnings from gathering
    # glyph id -> its display name / the words it is called by / whether the
    # library has it (icon_catalog when None). A team icon's words are its
    # name, label and tags (the studio's lookup reads them from the team's
    # icons): an added one lands where they are said.
    names: Optional[Callable[[str], Optional[str]]] = None
    terms: Optional[Callable[[str], list[str]]] = None
    has: Optional[Callable[[str], bool]] = None
    # The team's icons [{id, name, label, tags}]: one that is said (and
    # meant) is offered as a suggestion — never placed on its own.
    team: Optional[list[dict]] = None


def catalog_name(glyph_id: str) -> Optional[str]:
    """A library icon's studio name ("Free" for piggy-bank), else its name."""
    try:
        from .. import icon_catalog

        got = icon_catalog.icon(glyph_id)
    except Exception:  # noqa: BLE001 — a name is a nicety
        return None
    return (got.get("label") or got.get("name")) if got else None


def catalog_has(glyph_id: str) -> bool:
    """Whether the baked library still has this icon (an older draft can
    name one a re-bake dropped)."""
    try:
        from .. import icon_catalog

        return icon_catalog.library_d(glyph_id) is not None
    except Exception:  # noqa: BLE001 — no catalogue: nothing is known missing
        return True


def catalog_terms(glyph_id: str) -> list[str]:
    """The words a library icon is called by: its name, the studio's name and
    aliases, and its tags."""
    out: list[str] = []
    try:
        from .. import icon_catalog

        got = icon_catalog.icon(glyph_id)
        if got:
            out += [got.get("name") or "", got.get("label") or "", *(got.get("tags") or [])]
        for name, gid, aliases in icon_catalog.NAMES:
            if gid == glyph_id:
                out += [name, *aliases]
    except Exception:  # noqa: BLE001
        pass
    return [t for t in out if t]


# ---------------------------------------------------------------------------
# Words
# ---------------------------------------------------------------------------

def _by_w(words: dict) -> dict[int, dict]:
    return {int(w["w"]): w for w in words.get("words") or []}


def _stem(tok: str) -> str:
    t = tok.lower()
    for suf in ("ings", "ing", "ers", "er", "es", "ed", "s"):
        if t.endswith(suf) and len(t) - len(suf) >= 3:
            return t[: -len(suf)]
    return t


def _content(text: str) -> list[str]:
    return [t for t in re.findall(r"[a-z0-9]+", (text or "").lower())
            if len(t) >= 3 and t not in _STOP and t not in _GENERIC]


def said_mentions(words: dict, terms: Sequence[str]) -> list[dict]:
    """Where a word sharing a content stem with `terms` (a glyph's names,
    aliases and tags) is SAID — not inside a negation, an idiom or the
    brand: [{w, w_end, quote, s}], in speech order. How an icon the user
    added from the library finds its own moment; nothing is invented when
    it is not said."""
    stems = {_stem(t) for term in terms for t in _content(term)}
    if not stems:
        return []
    text = words.get("transcript") or ""
    out = []
    for w in words.get("words") or []:
        if not w.get("said") or w.get("s") is None or w.get("tchar") is None:
            continue
        toks = _content(w["text"])
        if not any(_stem(t) in stems for t in toks):
            continue
        at = int(w["tchar"])
        if brand_icons.is_brand(w["text"]) or brand_icons.negated_in_clause(text, at) \
                or brand_icons.in_idiom(text, at):
            continue
        out.append({"w": int(w["w"]), "w_end": int(w["w"]), "quote": w["text"], "s": float(w["s"])})
    return out


# ---------------------------------------------------------------------------
# Membership: who is on the list, and what each one may land on
# ---------------------------------------------------------------------------

@dataclass
class Cue:
    """One icon the scheduler may place: where it may land and how much it
    matters."""
    key: str                                   # its glyph id
    glyph: dict                                # {source, id}
    label: str
    purpose: str = ""
    priority: int = 3
    source: str = "ai"                         # ai | keyword | user
    tier: int = 2                              # 0 pinned, 1 added, 2 AI, 3 keyword
    rank: tuple = ()
    # (preference, word, onset, phrase end): the head word of its phrase
    # first, then the rest, then its other mentions.
    anchors: list[tuple] = field(default_factory=list)
    fixed: Optional[float] = None              # off its word: the settle the user chose
    hold: Optional[float] = None               # the user's hold (kept)
    hold_default: Optional[float] = None       # the element's hold (trimmed to fit)
    mentions: list[dict] = field(default_factory=list)   # the idea's confirmed mentions
    box: Optional[dict] = None                 # dragged by the user
    pinned: bool = False
    off_word: bool = False
    idea: Optional[dict] = None
    strong: bool = False
    look: Optional["Look"] = None              # how it looks (look_of)
    at: Optional[float] = None                 # never said: shown nearest this (IconAdd.at)
    lane: Optional[int] = None                 # the track the user put it on (props.lanes)

    @property
    def user(self) -> bool:
        return self.tier <= 1

    @property
    def key_idea(self) -> bool:
        return self.user or self.priority >= KEY_PRIORITY

    @property
    def primary(self) -> Optional[float]:
        if self.fixed is not None:
            return self.fixed
        return self.anchors[0][2] if self.anchors else None


def _anchors(words: dict, spans: Sequence[tuple[int, int]]) -> list[tuple]:
    """(preference, w, onset, phrase end) for every said word of each span,
    the first span first and each span's head word first."""
    by = _by_w(words)
    out: list[tuple] = []
    seen: set[int] = set()
    for w0, w1 in spans:
        end = by.get(w1, {}).get("e")
        for w in range(w0, w1 + 1):
            x = by.get(w)
            if not x or not x.get("said") or x.get("s") is None or w in seen:
                continue
            seen.add(w)
            out.append((len(out), w, float(x["s"]), float(end if end is not None else x["s"])))
    return out


def _idea_spans(idea: dict) -> list[tuple[int, int]]:
    spans = [(int(idea["w"]), int(idea["w_end"]))]
    for m in idea.get("mentions") or []:
        span = (int(m["w"]), int(m.get("w_end", m["w"])))
        if span[0] != spans[0][0]:
            spans.append(span)
    return spans


def _glyph(gid: str) -> dict:
    return {"source": "custom" if re.match(r"^c-[0-9a-f]{12}$", gid) else "library", "id": gid}


def _pin(cue: Cue, tw: rc.IconTweak, words: dict, notes: list[str]) -> None:
    """A user's pin on one icon (props.tweaks[glyph]): its word — another
    mention of its own idea; a word it had none of; re-anchored to the
    nearest mention after a new take — or a time off its word."""
    by = _by_w(words)
    if tw.off_word and tw.sec is not None:
        cue.fixed, cue.off_word, cue.pinned = float(tw.sec), True, True
        cue.anchors = []
    elif tw.w is not None:
        stale = bool(tw.words_digest) and tw.words_digest != words.get("digest")
        said = by.get(int(tw.w)) if not stale else None
        if said is not None and not said.get("said"):
            said = None
        spans = _idea_spans(cue.idea) if cue.idea is not None and int(cue.idea.get("w", -1)) >= 0 else \
            [(int(m["w"]), int(m.get("w_end", m["w"]))) for m in cue.mentions]
        inside = next((s for s in spans if said is not None and s[0] <= tw.w <= s[1]), None)
        if inside is not None:
            cue.anchors = [(0, int(tw.w), float(said["s"]), float(by.get(inside[1], said).get("e") or said["s"]))]
            cue.pinned = True
        elif said is not None and not spans:
            # Not said anywhere as itself: the word the user picked for it.
            cue.anchors = [(0, int(tw.w), float(said["s"]), float(said.get("e") or said["s"]))]
            cue.pinned = True
        elif spans:
            # Another word, or the word of an older take: the nearest mention
            # of its own idea (never a word it has nothing to do with).
            ref = float(said["s"]) if said is not None else None
            if ref is not None:
                pick = min(spans, key=lambda s: (abs(float(by[s[0]]["s"]) - ref), s[0]))
            else:
                pick = min(spans, key=lambda s: (abs(s[0] - int(tw.w)), s[0]))
            cue.anchors = _anchors(words, [pick])
            cue.pinned = bool(cue.anchors)
            if cue.anchors:
                notes.append((W_REPINNED if stale else W_NOT_SAID_THERE).format(
                    label=cue.label, word=by[pick[0]]["text"]))
        elif stale:
            notes.append(W_LOST_WORD.format(label=cue.label))
    if tw.hold_sec is not None:
        cue.hold = float(tw.hold_sec)
    if tw.box is not None:
        cue.box = tw.box.model_dump(mode="json")
    if cue.pinned:
        cue.tier = 0


def _ideas_by_icon(director: dict) -> dict[str, dict]:
    """One director idea per icon: the auto one, else a kept one, else the
    best of the rest."""
    order = {"keep": 0, "suggest": 1, "refuse": 2}
    out: dict[str, dict] = {}
    for i in director.get("ideas") or []:
        icon = i.get("icon")
        if not icon:
            continue
        have = out.get(icon)
        rank = (0 if i.get("auto") else 1, order.get(i.get("verdict"), 3), -int(i.get("priority") or 0),
                int(i.get("w") or 0))
        if have is None or rank < have["_rank"]:
            out[icon] = {**i, "_rank": rank}
    return out


def cues(el: rc.Element, props: rc.IconProps, words: dict, director: dict, *,
         names: Callable[[str], Optional[str]] = catalog_name,
         terms: Callable[[str], list[str]] = catalog_terms,
         has: Callable[[str], bool] = catalog_has,
         notes: Optional[list[str]] = None) -> list[Cue]:
    """The icons on the list (module docstring): the AI's auto picks not
    dismissed, and the user's adds — each with the words it may land on and
    the user's pins applied. An add the AI refused (said as a "no", say)
    never lands on the words it refused: only where its name is said and
    meant (said_mentions), else it waits for the user to pick a word. A
    library icon the catalogue no longer has is left out, and said."""
    from .. import director as dmod

    notes = notes if notes is not None else []
    dismissed = set(props.dismissed)
    ideas = _ideas_by_icon(director)
    out: dict[str, Cue] = {}
    for idea in dmod.auto_ideas(director):
        gid = idea["icon"]
        if gid in dismissed or gid in out:
            continue
        out[gid] = Cue(key=gid, glyph=_glyph(gid), label=idea.get("label") or names(gid) or gid,
                       purpose=idea.get("purpose") or "", priority=int(idea.get("priority") or 3),
                       source="ai" if idea.get("source") == "ai" else "keyword",
                       tier=2 if idea.get("source") == "ai" else 3, rank=dmod.rank_key(idea),
                       anchors=_anchors(words, _idea_spans(idea)), mentions=list(idea.get("mentions") or []),
                       idea=idea, strong=bool(idea.get("strong")))
    # The user's adds rank in the order they were added, an AI pick they
    # added too: one added later never goes before (and so never moves) one
    # added earlier.
    first = len(out)
    for n, add in enumerate(props.added):
        gid = add.glyph.id
        have = out.get(gid)
        if have is not None:
            have.tier, have.source, have.rank = 1, "user", (first + n,)
            have.label = add.label or have.label
            have.at = add.at
            continue
        idea = ideas.get(gid)
        label = add.label or (idea or {}).get("label") or names(gid) or gid
        if add.glyph.source == "library" and not has(gid):
            notes.append(W_GONE.format(label=label))
            continue
        if idea is not None and int(idea.get("w", -1)) >= 0 and idea.get("verdict") != "refuse":
            cue = Cue(key=gid, glyph=add.glyph.model_dump(mode="json"), label=label,
                      purpose=idea.get("purpose") or "", priority=int(idea.get("priority") or 3),
                      anchors=_anchors(words, _idea_spans(idea)), mentions=list(idea.get("mentions") or []),
                      idea=idea, strong=bool(idea.get("strong")))
        else:
            # Where its name is said: a library icon's names, aliases and
            # tags; a team icon's name, label and tags (the lookup's).
            found = said_mentions(words, [label, *terms(gid)])
            cue = Cue(key=gid, glyph=add.glyph.model_dump(mode="json"), label=label,
                      purpose=f"Says '{found[0]['quote']}'" if found else "", priority=3,
                      anchors=_anchors(words, [(m["w"], m["w_end"]) for m in found]), mentions=found)
        cue.source, cue.tier, cue.rank = "user", 1, (first + n,)
        cue.at = add.at
        out[gid] = cue
    for gid, tw in props.tweaks.items():
        cue = out.get(gid)
        if cue is not None:
            _pin(cue, tw, words, notes)
    # A track the user put an icon on: it is theirs from then on (the AI's
    # picks only ever go on track 0).
    for gid, lane in props.lanes.items():
        cue = out.get(gid)
        if cue is not None:
            cue.lane, cue.tier = int(lane), min(cue.tier, 1)
    for cue in out.values():
        if cue.hold is None and props.hold_sec is not None:
            cue.hold_default = float(props.hold_sec)
    return list(out.values())


# ---------------------------------------------------------------------------
# Look: each icon's own, over the scene's
# ---------------------------------------------------------------------------

SQUARE_STYLES = ("circle", "badge", "glyph")     # a body of one side L (the chip is wider)


@dataclass(frozen=True)
class Look:
    """One icon's look, as asked (look_of): its own (props.looks) over the
    element's scene-wide choices.

      style   circle | chip | badge | glyph
      size    its size, a share of the frame's short side (None: the spot
              grid's own, M)
      fill    #rrggbb of its body (None: the scene's colour — the element's,
              or the automatic pick)
      ink     #rrggbb of its glyph and words (None: worked out from the fill)
      label   a chip's words, as asked (chip_text cuts them to fit)
      layer   behind | front (the speaker)
      enter   its entrance; leave its exit (None: the entrance mirrored)
      own     it has a look of its own (the studio's "edited" dot)"""
    style: str
    size: Optional[float] = None
    fill: Optional[str] = None
    ink: Optional[str] = None
    label: str = ""
    layer: str = "behind"
    enter: str = "glitch"
    leave: Optional[str] = None
    own: bool = False

    @property
    def square(self) -> bool:
        return self.style != "chip"

    def as_circle(self) -> "Look":
        return replace(self, style="circle")


def look_of(cue: Cue, props: rc.IconProps, el: rc.Element) -> Look:
    """The icon `cue`'s look (module docstring: LOOK): props.looks[its
    glyph id] field by field, else the element's style, size and entrance;
    its words its own, else its name in the plan (the AI's label for the
    idea, the user's for an add, else the library's). A motion the renderer
    does not draw yet (normalize refuses it) is the scene's."""
    own = props.looks.get(cue.key)
    entrance = el.animation.entrance if el.animation.entrance in rc.ICON_ENTRANCES else "auto"
    entrance = "glitch" if entrance == "auto" else entrance

    def mine(key: str) -> Any:
        return getattr(own, key) if own is not None else None

    enter, leave = mine("enter"), mine("leave")
    return Look(
        style=mine("style") or props.style,
        size=mine("size") if mine("size") is not None else el.placement.size,
        fill=(mine("colour") or "").lower() or None,
        ink=(mine("ink") or "").lower() or None,
        label=mine("label") or cue.label,
        layer=mine("layer") or "behind",
        enter=enter if enter in rc.ICON_MOTIONS_READY else entrance,
        leave=leave if leave in rc.ICON_MOTIONS_READY else None,
        own=own is not None and bool(own.model_dump(exclude_none=True)),
    )


def chip_text(label: str) -> str:
    """A chip's words as asked: spaces collapsed, at most CHIP_LABEL_MAX
    characters (a longer name is cut with "…"); chip_geometry may cut them
    further to fit the frame."""
    from .. import icon_render

    words = icon_render.chip_words(label)
    if len(words) > rc.CHIP_LABEL_MAX:
        words = words[: rc.CHIP_LABEL_MAX - 1].rstrip() + icon_render.ELLIPSIS
    return words


def chip_height(side_px: float) -> int:
    """A chip's height for an icon of side L px (icon_render.CHIP_H)."""
    from .. import icon_render

    return max(4, int(round(icon_render.CHIP_H * side_px)))


@lru_cache(maxsize=512)
def chip_geometry(label: str, h: int, width: int, font: str) -> Any:
    """icon_render.chip_geometry of `label`'s words (chip_text) at height h
    on a frame `width` px wide — the width the Stamper gives the body."""
    from .. import icon_render

    return icon_render.chip_geometry(chip_text(label), h, font, max_w=icon_render.CHIP_MAX_W * width)


def label_key() -> Optional[str]:
    """The label font new chips are drawn with (None: not installed)."""
    try:
        from .. import label_font

        return label_font.default_key()
    except Exception:  # noqa: BLE001 — chips then show as circles, and say so
        return None


# ---------------------------------------------------------------------------
# When: the scheduler
# ---------------------------------------------------------------------------

@dataclass
class Slot:
    """One icon placed in time (frames)."""
    cue: Cue
    settle_f: int
    anchor: Optional[tuple]                    # the (pref, w, onset, end) it landed on
    hold_f: int = 0
    lane: int = 0                              # its track on the timeline (module docstring: TRACKS)

    @property
    def from_f(self) -> int:
        return self.settle_f - IN_F

    @property
    def to_f(self) -> int:
        return self.settle_f + self.hold_f + OUT_F


Placeable = Callable[[Cue, int, int], tuple[bool, Optional[float]]]


def pattern_zones(timings: Sequence[Optional[dict]]) -> list[dict]:
    """Where no icon settles while a pattern moves ([{from, to, why}],
    scene seconds, in time order): each entrance from its start for
    ENTRANCE_CLEAR, or its ramp + ENTRANCE_AFTER when that is longer (an
    entrance "none" has none); each exit over its ramp (the cut ending one
    that lasts to the end is none). `timings`: each pattern's time —
    elements/pattern Timing.public(), or house.patterns_for_icons' rows — a
    None (it does not show) has none. The 42 default (in over 1 s from 0,
    to the end) is exactly the one zone the icons have always kept: 0 to
    1.5 s."""
    out: list[dict] = []
    for tm in timings:
        if not tm:
            continue
        t_in, t_full = float(tm["t_in"]), float(tm["t_full"])
        t_hold_end, t_out = float(tm["t_hold_end"]), float(tm["t_out"])
        if tm.get("entrance", "auto") != "none":
            out.append({"from": round(t_in, 3),
                        "to": round(t_in + max(ENTRANCE_CLEAR, t_full - t_in + ENTRANCE_AFTER), 3),
                        "why": ZONE_ENTRANCE})
        if tm.get("exit", "auto") != "none" and t_out > t_hold_end:
            out.append({"from": round(t_hold_end, 3), "to": round(t_out, 3), "why": ZONE_EXIT})
    # Two patterns coming in together keep one zone.
    seen = {(z["from"], z["to"], z["why"]): z for z in out}
    return [seen[k] for k in sorted(seen)]


def zones_of(enters: bool, zones: Optional[Sequence[dict]]) -> list[dict]:
    """A scene's pattern zones: its own (the timeline's), else the one the
    icons have always kept while a pattern comes in (`enters`)."""
    if zones is not None:
        return [dict(z) for z in zones]
    return [{"from": 0.0, "to": ENTRANCE_CLEAR, "why": ZONE_ENTRANCE}] if enters else []


class Scheduler:
    """Places a list of cues in time (module docstring: WHEN). `placeable`
    (cue, from_f, to_f) -> (ok, blocked at): whether the icon has a clear
    spot for that window — a window it has none in is never used.

    A cue the user timed by hand (`fixed`: off its word, at a time they
    chose) takes a FIXED slot (fixed_slot): placed first, exactly there,
    inside the scene; the zones are only warned about (`hand_notes`, by
    glyph id). Fixed slots may overlap one another; every other cue keeps
    clear of each (its window ± the clear air).

    `zones` (pattern_zones): where no settle may be while a pattern comes in
    or goes out; None — `enters` alone, the first 1.5 s as always.

    Tracks (module docstring: TRACKS): what run() places one after another
    is track 0; a cue of the user's with no room there (or put on a track
    of its own) takes its own time (_own) and a track; the AI's keep clear
    of it, theirs only when it is on track 0. `rescued` says which of the
    user's only these rules showed (glyph id -> how: lane | shift | hand |
    spot) and `shifts` which moved off a zone, and where they were said (or,
    never said, the moment they were added at). `yielded` holds the AI's
    picks these rules left out for the user's (one that would push theirs off
    its word, or take the room of an add); `moments` the user's added at a
    moment kept there (never said: that moment; said: their word) on a time
    of their own."""

    def __init__(self, *, duration: float, enters: bool, voiced: Optional[Sequence[float]] = None,
                 fps: float = FPS, placeable: Optional[Placeable] = None,
                 zones: Optional[Sequence[dict]] = None):
        self.fps = float(fps)
        self.duration = float(duration)
        self.enters = bool(enters)
        v = list(voiced) if voiced else [0.0, self.duration]
        self.auto_max = auto_max(float(v[1]) - float(v[0]))
        self.placeable = placeable
        self.head_f = int(math.ceil(HEAD_CLEAR * self.fps - 1e-6))
        self.entrance_f = int(math.ceil(ENTRANCE_CLEAR * self.fps - 1e-6))
        # Each zone as frames [from, to): a settle frame inside one is out. A
        # zone from the scene's start holds everything before it too (as the
        # entrance's always has).
        self.zones_f: list[tuple[float, int, str]] = [
            (-math.inf if float(z["from"]) <= 0.0 else int(math.floor(float(z["from"]) * self.fps + 1e-6)),
             int(math.ceil(float(z["to"]) * self.fps - 1e-6)), str(z.get("why") or ZONE_ENTRANCE))
            for z in zones_of(self.enters, zones)]
        self.tail_f = int(math.floor((self.duration - TAIL_CLEAR) * self.fps + 1e-6))
        self.frames = int(round(self.duration * self.fps))
        self.fixed: list[Slot] = []                     # the hand-timed slots (run() places them first)
        self.hand_notes: dict[str, list[str]] = {}
        self.extra: list[Slot] = []                     # the user's on their own time (_own)
        self.rescued: dict[str, str] = {}
        self.shifts: dict[str, dict] = {}
        self.yielded: set[str] = set()
        self.moments: set[str] = set()
        self._memo: dict[tuple, Any] = {}

    # -- numbers ---------------------------------------------------------------
    def _f(self, sec: float) -> int:
        return int(round(sec * self.fps))

    def min_hold(self, c: Cue) -> int:
        return self._f(c.hold) if c.hold is not None else self._f(HOLD_MIN)

    def gap(self, a: Cue, b: Cue) -> int:
        return self._f(GAP_KEY if (a.key_idea and b.key_idea) else GAP)

    def wanted_hold(self, c: Cue, slot: Slot) -> int:
        if c.hold is not None:
            return self._f(c.hold)
        if c.hold_default is not None:
            want = c.hold_default * self.fps
        elif slot.anchor is not None:
            want = (slot.anchor[3] + HOLD_AFTER) * self.fps - slot.settle_f
        else:
            want = HOLD_DEFAULT * self.fps
        return int(max(self._f(HOLD_MIN), min(self._f(HOLD_MAX), round(want))))

    # -- one cue's possible settles -------------------------------------------
    def raw_states(self, c: Cue) -> list[tuple[float, int, Optional[tuple]]]:
        """(cost, settle frame, anchor) with no zone or place rule applied."""
        out: list[tuple[float, int, Optional[tuple]]] = []
        if c.fixed is not None:
            out.append((0.0, self._f(c.fixed), None))
            return out
        if not c.anchors and c.user:
            # Added by the user, never said: any clear time (the owner's
            # call, 2026-09-30: an icon they add always shows): the one
            # nearest the moment it was added at, else the earliest.
            frames = range(IN_F, max(IN_F, self.frames - OUT_F), FREE_STEP)
            if c.at is not None:
                want = self._f(c.at)
                return [(abs(f - want) * FREE_COST, f, None) for f in frames]
            return [(f * FREE_COST, f, None) for f in frames]
        for a in c.anchors:
            onset = a[2]
            target = onset * self.fps - LAND_F
            lo = int(math.ceil((onset - TOL_EARLY) * self.fps - 1e-6))
            hi = int(math.floor((onset + TOL_LATE) * self.fps + 1e-6))
            for f in range(lo, hi + 1):
                out.append((a[0] * RANK_COST + (f - target) ** 2, f, a))
        return out

    def _near(self, f: int, at: int) -> bool:
        """A settle at frame f is at the moment `at` (a frame): within a
        word's tolerance of it, as one on its word is."""
        return at - int(round(TOL_EARLY * self.fps)) <= f <= at + int(round(TOL_LATE * self.fps))

    def _asked_f(self, c: Cue) -> Optional[int]:
        """The settle frame one of the user's never said was added at
        (IconAdd.at, the playhead); None for any other (said, timed by hand,
        or added with no moment)."""
        if not c.user or c.anchors or c.fixed is not None or c.at is None:
            return None
        return self._f(c.at)

    def _moment_f(self, c: Cue) -> Optional[int]:
        """Where one of the user's never said shows by right: the settle
        nearest the moment it was added at that is out of the opening, the
        zones and the end (others' times and the spots aside); None with no
        moment, or no time allowed in the whole scene."""
        want = self._asked_f(c)
        if want is None:
            return None
        ok = [f for _cost, f, _a in self.raw_states(c) if self._in_zones(c, f)]
        return min(ok, key=lambda f: (abs(f - want), f)) if ok else None

    def _at_moment(self, c: Cue, slots: Sequence[Slot], alone: Optional[Sequence[Slot]]) -> bool:
        """Whether `c`, one of the user's added at a moment (IconAdd.at), is
        where it shows by right in `slots`: never said, at its moment; said,
        on the word it takes on its own (`alone`), not pushed to another
        mention by the ones on track 0 before it. Any other cue: always."""
        if not c.user or c.fixed is not None or c.at is None:
            return True
        s = next((x for x in slots if x.cue is c), None)
        if not c.anchors:
            m = self._moment_f(c)
            return m is None or (s is not None and self._near(s.settle_f, m))
        own = next((x for x in alone or [] if x.cue is c), None)
        if own is None or own.anchor is None:
            return True
        return s is not None and s.anchor is not None and s.anchor[1] == own.anchor[1]

    def _moment_shift(self, s: Slot) -> Optional[dict]:
        """One of the user's never said shown away from the moment it was
        added at (the opening, a zone or the end in the way), as _shift_of
        says one moved off its word: {at, said (None: never said), asked
        (that moment: the studio's "Show at … anyway"), why}; None at its
        moment."""
        want = self._asked_f(s.cue)
        if want is None or self._near(s.settle_f, want):
            return None
        lo, hi = sorted((want, s.settle_f))
        crossed = {why for z0, z1, why in self.zones_f if z0 < hi and lo < z1}
        if s.settle_f > want:
            why = "entrance" if ZONE_ENTRANCE in crossed else "exit_after" if ZONE_EXIT in crossed else "start"
        else:
            why = "exit_before" if ZONE_EXIT in crossed else "end"
        at = round(float(s.cue.at or 0.0), 2)
        return {"at": at, "said": None, "asked": at, "why": why}

    def _zone_at(self, f: int) -> Optional[str]:
        """Why a settle at frame f is in a pattern's zone (its entrance, its
        exit), or None."""
        return next((why for a, b, why in self.zones_f if a <= f < b), None)

    def _in_zones(self, c: Cue, f: int) -> bool:
        """A settle at frame f keeps out of the opening, the patterns'
        entrances and exits, and the end."""
        if f - IN_F < self.head_f:
            return False
        if self._zone_at(f) is not None:
            return False
        return f + self.min_hold(c) + OUT_F <= self.tail_f

    def _blockers(self, c: Cue) -> list[Slot]:
        """What `c` keeps clear of beside its own track's chain: every fixed
        slot, and the user's icons on their own time (_own): an AI pick all
        of them, one of the user's only those on track 0 (theirs may show
        together on two tracks)."""
        return self.fixed + [s for s in self.extra if s.lane == 0 or not c.user]

    def _clear_of_fixed(self, c: Cue, f: int) -> bool:
        """A settle at frame f keeps its window (at its shortest hold) clear
        of every fixed slot (and the rest of _blockers), with the clear air
        between them."""
        lo, hi = f - IN_F, f + self.min_hold(c) + OUT_F
        for s in self._blockers(c):
            g = self.gap(s.cue, c)
            if not (lo >= s.to_f + g or hi + g <= s.from_f):
                return False
        return True

    def _zoned(self, c: Cue, f: int) -> bool:
        return self._in_zones(c, f) and self._clear_of_fixed(c, f)

    # -- a time set by hand ------------------------------------------------------
    def fixed_slot(self, c: Cue, at: Optional[float] = None) -> Slot:
        """The fixed slot of a cue the user timed by hand (c.fixed): its
        settle exactly there, held for its own length (c.hold, else as long
        as an icon off its word is), kept inside the scene — it comes in no
        earlier than its first frame and is gone by its last, held at least
        the shortest length a hand may set — and the zones it is in noted
        (hand_notes), never refused. `at`: that settle for one that wasn't
        timed by hand (_own, a scene with no clear time for it)."""
        settle = self._f(float(at if at is not None else (c.fixed or 0.0)))
        slot = Slot(cue=c, settle_f=settle, anchor=None)
        hold = self._f(c.hold) if c.hold is not None else self.wanted_hold(c, slot)
        shortest = max(1, self._f(rc.ICON_USER_HOLD[0]))
        if self.frames > 0:
            settle = min(settle, self.frames - OUT_F - shortest)
        settle = max(IN_F, settle)
        hold = max(1, hold if self.frames <= 0 else min(hold, self.frames - OUT_F - settle))
        slot = Slot(cue=c, settle_f=settle, anchor=None, hold_f=hold)
        notes = []
        if slot.from_f < self.head_f:
            notes.append(W_HAND_START.format(label=c.label))
        zone = self._zone_at(slot.settle_f)
        if zone is not None:
            notes.append((W_HAND_EXIT if zone == ZONE_EXIT else W_HAND_ENTRANCE).format(label=c.label))
        if slot.to_f > self.tail_f:
            notes.append(W_HAND_END.format(label=c.label))
        self.hand_notes[c.key] = notes
        return slot

    def states(self, c: Cue) -> list[tuple[float, int, Optional[tuple]]]:
        """The settles `c` may take, cheapest first per frame, in frame order."""
        memo = self._memo.get(("states", id(c)))
        if memo is not None:
            return memo
        best: dict[int, tuple[float, int, Optional[tuple]]] = {}
        for cost, f, a in self.raw_states(c):
            if not self._zoned(c, f):
                continue
            if f in best and best[f][0] <= cost:
                continue
            best[f] = (cost, f, a)
        out = []
        for f in sorted(best):
            if self.placeable is not None:
                ok, _t = self.placeable(c, f - IN_F, f + self.min_hold(c) + OUT_F)
                if not ok:
                    continue
            out.append(best[f])
        self._memo[("states", id(c))] = out
        return out

    # -- a set of cues ---------------------------------------------------------
    def solve(self, cues: Sequence[Cue]) -> Optional[list[Slot]]:
        """The best way `cues` fit together (time order), or None."""
        if not cues:
            return []
        states = [self.states(c) for c in cues]
        if any(not s for s in states):
            return None
        best_cost, best_path = None, None
        for perm in itertools.permutations(range(len(cues))):
            layer = [(s[0], s[1], s[2], None) for s in states[perm[0]]]
            ok = True
            for prev_i, i in zip(perm, perm[1:]):
                need = self.min_hold(cues[prev_i]) + OUT_F + self.gap(cues[prev_i], cues[i]) + IN_F
                frames = [e[1] for e in layer]
                # The cheapest earlier entry at or before each point.
                pmin: list[int] = []
                for k, e in enumerate(layer):
                    pmin.append(k if not pmin or e[0] < layer[pmin[-1]][0] else pmin[-1])
                nxt = []
                for cost, f, a in states[i]:
                    k = bisect_right(frames, f - need) - 1
                    if k < 0:
                        continue
                    back = layer[pmin[k]]
                    nxt.append((back[0] + cost, f, a, back))
                if not nxt:
                    ok = False
                    break
                layer = nxt
            if not ok:
                continue
            end = min(layer, key=lambda e: (e[0], e[1]))
            if best_cost is None or end[0] < best_cost:
                best_cost, best_path = end[0], (perm, end)
        if best_path is None:
            return None
        perm, end = best_path
        chain = []
        node = end
        while node is not None:
            chain.append(node)
            node = node[3]
        chain.reverse()
        slots = [Slot(cue=cues[i], settle_f=e[1], anchor=e[2], hold_f=self.min_hold(cues[i]))
                 for i, e in zip(perm, chain)]
        return self._extend(slots)

    def _extend(self, slots: list[Slot]) -> list[Slot]:
        """Each hold stretched towards the one wanted (until its phrase
        ends + HOLD_AFTER), never into the next icon's clear air — a fixed
        slot's too — the end, or a moment the speaker takes its spot."""
        for k, s in enumerate(slots):
            want = self.wanted_hold(s.cue, s)
            limit = self.tail_f - OUT_F - s.settle_f
            later = slots[k + 1:k + 2] + [x for x in self._blockers(s.cue) if x.from_f > s.settle_f]
            for nxt in later:
                limit = min(limit, nxt.from_f - self.gap(s.cue, nxt.cue) - OUT_F - s.settle_f)
            hold = max(s.hold_f, min(want, limit))
            while hold > s.hold_f and self.placeable is not None and \
                    not self.placeable(s.cue, s.from_f, s.settle_f + hold + OUT_F)[0]:
                hold -= 1
            s.hold_f = hold
        return slots

    # -- the list ----------------------------------------------------------------
    def why_alone(self, c: Cue) -> str:
        """Why `c` cannot show even on its own."""
        if c.fixed is None and not c.anchors:
            return NOT_TIED
        raw = self.raw_states(c)
        zoned = [s for s in raw if self._zoned(c, s[1])]
        if zoned and self.placeable is not None:
            blocked = [self.placeable(c, f - IN_F, f + self.min_hold(c) + OUT_F)[1] for _c, f, _a in zoned]
            t = min((b for b in blocked if b is not None), default=None)
            return NO_SPOT.format(t=_t(t if t is not None else zoned[0][1] / self.fps))
        blockers = self._blockers(c)
        if blockers and any(self._in_zones(c, f) for _c, f, _a in raw):
            # In time, but where an icon timed by hand (or one of the user's
            # on a track of its own) is.
            return self._no_room(c, sorted(blockers, key=lambda s: s.settle_f))
        mid = self.duration / 2
        onset = c.primary or 0.0
        near_end = all(f + self.min_hold(c) + OUT_F > self.tail_f for _c, f, _a in raw)
        zoned_at = [self._zone_at(f) for _c, f, _a in raw]
        if raw and not near_end and all(z == ZONE_EXIT for z in zoned_at):
            # Said while a pattern goes out (anywhere in the scene).
            return IN_EXIT
        if near_end or onset > mid:
            return NEAR_END
        if raw and all(z is not None for z in zoned_at):
            return IN_EXIT if all(z == ZONE_EXIT for z in zoned_at) else IN_ENTRANCE
        return NEAR_START

    def run(self, cues: Sequence[Cue]) -> tuple[list[Slot], dict[str, str]]:
        """(the icons shown, in time order; {glyph id: why not} for the rest).
        The ones timed by hand first, each exactly where it was put (fixed
        slots); then the rest, kept clear of them: the user's on track 0
        when they fit there at their own moment (one never said: where it
        was added, _at_moment) without pushing one of theirs already there
        off its word (_users_stay), else each on its own time and track
        (_own); then the AI's, on track 0 and clear of everything, never
        moving one of the user's off its word (one with no room only for one
        of the user's on a track of its own gives way to it, and says so:
        _gave_way). Only the AI's are ever left out, and the user's past
        ITEMS_MAX in all. The user's adds come first for that cap: an AI
        pick, even one the user pinned, timed or moved, only takes the room
        the adds leave."""
        order = sorted(cues, key=lambda c: (c.tier, c.rank, c.primary if c.primary is not None else 1e9, c.key))
        kept: list[Cue] = []
        drops: dict[str, str] = {}
        placed: list[Slot] = []
        n_auto = 0
        self.fixed, self.hand_notes, self.extra, self.rescued, self.shifts = [], {}, [], {}, {}
        self.yielded, self.moments = set(), set()
        adds_left = sum(1 for c in order if c.source == "user")

        def over_max(c: Cue, shown: int) -> bool:
            # Past the cap, counting the room the adds not placed yet keep.
            nonlocal adds_left
            if c.source == "user":
                adds_left -= 1
                return shown >= ITEMS_MAX
            if shown >= ITEMS_MAX:
                return True
            if shown + adds_left >= ITEMS_MAX:
                self.yielded.add(c.key)
                return True
            return False

        for c in order:
            if c.fixed is None:
                continue
            if over_max(c, len(self.fixed)):
                drops[c.key] = OVER_MAX.format(n=ITEMS_MAX)
                continue
            s = self.fixed_slot(c)
            if self.placeable is not None:
                ok, t = self.placeable(c, s.from_f, s.to_f)
                if not ok and not c.user:
                    self.hand_notes.pop(c.key, None)
                    drops[c.key] = NO_SPOT.format(t=_t(t if t is not None else s.settle_f / self.fps))
                    continue
                if not ok:
                    # Where they put it, whatever the speaker does there
                    # (place() puts it behind her, and says so).
                    self.rescued[c.key] = "spot"
            self.fixed.append(s)
        self._lay(self.fixed)
        self._memo.clear()
        for c in order:
            if c.fixed is not None:
                continue
            if not c.anchors and not c.user:
                drops[c.key] = NOT_TIED
                continue
            shown = len(kept) + len(self.fixed) + len(self.extra)
            if c.user:
                if over_max(c, shown):
                    drops[c.key] = OVER_MAX.format(n=ITEMS_MAX)
                    continue
                if not c.lane:
                    alone = self.solve([c])
                    got = self.solve(kept + [c]) if alone is not None else None
                    # Track 0 when it fits there at its own moment without
                    # moving one of the user's already there off its word;
                    # else a track of its own (owner decision: busy, a new
                    # track below). One added at a moment is no more pushed
                    # along track 0 (to a later time, another mention) than
                    # one already there is.
                    fits = got is not None and self._users_stay(placed, got)
                    if fits and self._at_moment(c, got, alone):
                        kept.append(c)
                        placed = got
                        continue
                    if fits:
                        self.moments.add(c.key)
                self.extra.append(self._own(c, placed))
                self._memo.clear()
                continue
            # One that cannot show even on its own says why first: "over the
            # limit" would send the user to Add it, and it still wouldn't show.
            if self.solve([c]) is None:
                # Kept out by the user's on tracks of their own: it gives way
                # when track 0 has room for it; when track 0 has none either
                # (its own chain), that is why; else why it cannot show.
                drops[c.key] = self._gave_way(c, kept, placed) or (
                    self._no_room_on_track0(c, placed) if self._fits_aside(c) else self.why_alone(c))
                continue
            if n_auto >= self.auto_max:
                drops[c.key] = OVER_LIMIT.format(n=self.auto_max)
                continue
            if over_max(c, shown):
                drops[c.key] = OVER_MAX.format(n=ITEMS_MAX)
                continue
            got = self.solve(kept + [c])
            if got is not None and not self._users_stay(placed, got):
                # The AI gives way: fitting it would move one of the user's
                # off its word (to another mention, or away from its moment).
                self.yielded.add(c.key)
                drops[c.key] = self._no_room(c, sorted(placed + self._blockers(c), key=lambda s: s.settle_f))
                continue
            if got is None:
                drops[c.key] = self._gave_way(c, kept, placed) or self._no_room_on_track0(c, placed)
                continue
            kept.append(c)
            placed = got
            n_auto += 1
        out = placed + self.fixed + self.extra
        self._compact(out)
        # One never said shown away from its moment (a zone in the way):
        # said, as one moved off its word is.
        for s in out:
            moved = self._moment_shift(s)
            if moved is not None:
                self.shifts[s.cue.key] = moved
        return sorted(out, key=lambda s: (s.settle_f, s.cue.key)), drops

    def _gave_way(self, c: Cue, kept: Sequence[Cue], placed: Sequence[Slot]) -> Optional[str]:
        """Why an AI pick with no room is left out when only the user's on a
        track of their own are in its way (track 0 has room for it): it
        gives way to the one of theirs nearest where it would show
        (GIVE_WAY), not "No room" by an icon on another track. None when
        track 0 itself has no room for it."""
        theirs = self._theirs_aside()
        if not theirs or c.user:
            return None
        got = self._solve_aside(list(kept) + [c])
        if got is not None and not self._users_stay(placed, got):
            got = None
        if got is None:
            return None
        at = next(s for s in got if s.cue is c)
        near = min(theirs, key=lambda s: (max(s.from_f - at.to_f, at.from_f - s.to_f, 0), s.settle_f))
        return GIVE_WAY.format(label=near.cue.label)

    def _theirs_aside(self) -> list[Slot]:
        """The user's icons on tracks of their own (not track 0)."""
        return [s for s in self.fixed + self.extra if s.lane > 0 and s.cue.user]

    def _solve_aside(self, cues: Sequence[Cue]) -> Optional[list[Slot]]:
        """solve(cues) with the user's on tracks of their own out of the way."""
        gone = {id(s) for s in self._theirs_aside()}
        fixed, extra = self.fixed, self.extra
        self.fixed = [s for s in fixed if id(s) not in gone]
        self.extra = [s for s in extra if id(s) not in gone]
        self._memo.clear()
        try:
            return self.solve(cues)
        finally:
            self.fixed, self.extra = fixed, extra
            self._memo.clear()

    def _fits_aside(self, c: Cue) -> bool:
        """Whether an AI pick with no room even on its own would have some
        with the user's on tracks of their own out of the way."""
        return not c.user and bool(self._theirs_aside()) and self._solve_aside([c]) is not None

    def _no_room_on_track0(self, c: Cue, placed: Sequence[Slot]) -> str:
        """No room for an AI pick that does not give way (_gave_way: track 0
        has no room for it either): said by what is on track 0 and the slots
        timed by hand, not by one of the user's on a track of its own (it is
        not why), unless nothing else is near."""
        aside = {id(s) for s in self._theirs_aside()}
        every = list(placed) + self._blockers(c)
        rest = [s for s in every if id(s) not in aside]
        return self._no_room(c, sorted(rest or every, key=lambda s: s.settle_f))

    def _users_stay(self, before: Sequence[Slot], after: Sequence[Slot]) -> bool:
        """Whether every one of the user's in `before` (track 0) is still on
        its word in `after`: the same word, or, never said, within a word's
        tolerance of where it settled. Nothing added later, and no AI pick,
        pushes one of theirs to another mention or moment."""
        now = {s.cue.key: s for s in after}
        early = int(round(TOL_EARLY * self.fps))
        late = int(round(TOL_LATE * self.fps))
        for s in before:
            if not s.cue.user:
                continue
            n = now.get(s.cue.key)
            if n is None:
                return False
            if s.anchor is not None:
                if n.anchor is None or n.anchor[1] != s.anchor[1]:
                    return False
            elif not s.settle_f - early <= n.settle_f <= s.settle_f + late:
                return False
        return True

    # -- the user's on their own time, and the tracks ----------------------------------
    def _own(self, c: Cue, placed: Sequence[Slot]) -> Slot:
        """One of the user's with no room on track 0, or put on a track of
        its own (module docstring: TRACKS): its own best time, clear of no
        other icon. On its word (a never-said add: at its moment, _moment_f,
        within a word's tolerance of it; added with no moment: nearest the
        earliest); its words all in a zone: the allowed time nearest them,
        keeping its word (`shifts`); no allowed time in the scene: at its
        word (or moment), as if timed by hand. A time with a clear spot
        first; with none, the cheapest anyway (place() puts it behind the
        speaker, and says so). Then its track (_lane_for)."""
        how = "lane"
        cands = [s for s in self.raw_states(c) if self._in_zones(c, s[1])]
        moment = self._moment_f(c)
        if moment is not None:
            # Never said: at the moment it was added at, never at another
            # time far from it for a clearer spot (as one on its word).
            cands = [s for s in cands if self._near(s[1], moment)]
            self.moments.add(c.key)
        if not cands and c.anchors:
            cands = self._shifted(c)
            how = "shift" if cands else how
        if cands:
            slot = self._best(c, cands, shifted=how == "shift")
        else:
            how = "hand"
            onset = c.anchors[0][2] if c.anchors else None
            settle = onset - LAND_F / self.fps if onset is not None else (c.at if c.at is not None else 0.0)
            slot = self.fixed_slot(c, at=max(0.0, settle))
            slot.anchor = c.anchors[0] if c.anchors else None
            self.hand_notes[c.key] = [W_NO_CLEAR_TIME.format(label=c.label)] if c.anchors else []
        if how == "shift":
            self.shifts[c.key] = self._shift_of(c, slot)
        slot.lane = self._lane_for(slot, c.lane, placed)
        self.rescued[c.key] = how
        return slot

    def _shifted(self, c: Cue) -> list[tuple[float, int, Optional[tuple]]]:
        """Every allowed settle in the scene for a cue whose words all fall
        where none may be, dearer than any on a word: the nearest to its
        words first (its first word's before the rest)."""
        out: list[tuple[float, int, Optional[tuple]]] = []
        ok = [f for f in range(IN_F, max(IN_F, self.frames - OUT_F) + 1) if self._in_zones(c, f)]
        for a in c.anchors:
            target = a[2] * self.fps - LAND_F
            out += [(SHIFT_COST + a[0] * RANK_COST + (f - target) ** 2, f, a) for f in ok]
        return out

    def _best(self, c: Cue, cands: Sequence[tuple[float, int, Optional[tuple]]], *, shifted: bool = False) -> Slot:
        """The cheapest of `cands` with a clear spot for its window (else the
        cheapest), held as long as it would be: its phrase, or, moved off its
        word, as long as an icon off its word is; inside the scene's end and
        while its spot is clear. Moved out of a zone (`shifted`), it is the
        nearest allowed time: a clear spot is looked for only within
        SHIFT_REACH of it, else it shows there anyway (place() puts it
        behind the speaker, and says so), so the zone stays the reason."""
        order = sorted(cands, key=lambda s: (s[0], s[1]))
        best, clear = order[0], self.placeable is None
        if self.placeable is not None:
            reach = self._f(SHIFT_REACH) if shifted else None
            for s in order:
                if reach is not None and abs(s[1] - order[0][1]) > reach:
                    continue
                if self.placeable(c, s[1] - IN_F, s[1] + self.min_hold(c) + OUT_F)[0]:
                    best, clear = s, True
                    break
        _cost, f, a = best
        slot = Slot(cue=c, settle_f=f, anchor=a, hold_f=self.min_hold(c))
        want = self.wanted_hold(c, Slot(cue=c, settle_f=f, anchor=None if shifted else a))
        hold = max(slot.hold_f, min(want, self.tail_f - OUT_F - f))
        while clear and hold > slot.hold_f and self.placeable is not None and \
                not self.placeable(c, slot.from_f, f + hold + OUT_F)[0]:
            hold -= 1
        slot.hold_f = hold
        return slot

    def _shift_of(self, c: Cue, slot: Slot) -> dict:
        """Where one moved out of a zone was said, and why it moved: {at (the
        settle it would have on its word, s), said (the word's onset, s),
        why (SHIFT_WHY's key)}, and, added at a moment (IconAdd.at) in the
        stretch it was moved across (from its word to where it shows, within
        a word's tolerance), asked: that moment, the one "Show at … anyway"
        offers (the user asked for it there, not on the word). Not for a
        word the user picked (they chose the word, not that moment), nor a
        moment far from the word and the zone (it has nothing to do with
        the move): those offer the word."""
        a = slot.anchor or c.anchors[0]
        on = int(round(a[2] * self.fps - LAND_F))
        lo, hi = sorted((on, slot.settle_f))
        crossed = {why for z0, z1, why in self.zones_f if z0 < hi and lo < z1}
        if slot.settle_f > on:
            why = "entrance" if ZONE_ENTRANCE in crossed else "exit_after" if ZONE_EXIT in crossed else "start"
        else:
            why = "exit_before" if ZONE_EXIT in crossed else "end"
        out = {"at": round(max(0.0, a[2] - LAND_F / self.fps), 2), "said": round(float(a[2]), 3), "why": why}
        if c.at is not None and not c.pinned and \
                lo - int(round(TOL_EARLY * self.fps)) <= self._f(c.at) <= hi + int(round(TOL_LATE * self.fps)):
            out["asked"] = round(float(c.at), 2)
        return out

    def _lane_for(self, s: Slot, pin: Optional[int], placed: Sequence[Slot]) -> int:
        """The track of one of the user's on its own time: its pinned one
        when free there, track 0 when it keeps clear of everything on it
        (with the clear air), else the first free one."""
        on: dict[int, list[Slot]] = {0: list(placed)}
        for o in self.fixed + self.extra:
            on.setdefault(o.lane, []).append(o)

        def free(n: int) -> bool:
            if n == 0:
                return all(s.from_f >= o.to_f + self.gap(o.cue, s.cue) or s.to_f + self.gap(o.cue, s.cue) <= o.from_f
                           for o in on[0])
            return all(s.from_f >= o.to_f or s.to_f <= o.from_f for o in on.get(n, []))

        tries = ([pin] if pin is not None else []) + list(range(0, rc.ICON_LANE_MAX + 1))
        return next((n for n in tries if free(n)), max(on) + 1)

    def _lay(self, slots: Sequence[Slot]) -> None:
        """The tracks of the slots timed by hand: each on its pinned track
        when free there, else the first free one (two timed by hand may show
        together: then on two)."""
        done: list[Slot] = []
        for s in sorted(slots, key=lambda x: (x.from_f, x.cue.key)):
            tries = ([s.cue.lane] if s.cue.lane is not None else []) + list(range(0, rc.ICON_LANE_MAX + 1))
            s.lane = next((n for n in tries if all(o.lane != n or s.from_f >= o.to_f or s.to_f <= o.from_f
                                                   for o in done)), len(done))
            done.append(s)

    @staticmethod
    def _compact(slots: Sequence[Slot]) -> None:
        """The tracks numbered with no empty one between them (track 0, the
        AI's, stays 0)."""
        used = sorted({s.lane for s in slots if s.lane > 0})
        to = {n: i + 1 for i, n in enumerate(used)}
        for s in slots:
            if s.lane > 0:
                s.lane = to[s.lane]

    def _no_room(self, c: Cue, placed: list[Slot]) -> str:
        at = (c.primary or 0.0) * self.fps
        before = [s for s in placed if s.settle_f <= at]
        if before:
            return NO_ROOM_AFTER.format(label=before[-1].cue.label)
        after = [s for s in placed if s.settle_f > at]
        return NO_ROOM_BEFORE.format(label=after[0].cue.label) if after else NEAR_END


# ---------------------------------------------------------------------------
# Where: the room
# ---------------------------------------------------------------------------

def _near(box: dict, other: dict, reach: float, width: int, height: int) -> bool:
    """Whether two fractional boxes are within `reach` x W of each other."""
    ax0, ay0 = box["x"] * width, box["y"] * height
    ax1, ay1 = ax0 + box["w"] * width, ay0 + box["h"] * height
    bx0, by0 = other["x"] * width, other["y"] * height
    bx1, by1 = bx0 + other["w"] * width, by0 + other["h"] * height
    dx = max(0.0, bx0 - ax1, ax0 - bx1)
    dy = max(0.0, by0 - ay1, ay0 - by1)
    return math.hypot(dx, dy) <= reach * width


def _hash01(*key: object) -> float:
    h = hashlib.blake2b("|".join(map(str, key)).encode(), digest_size=8).digest()
    return int.from_bytes(h, "big") / 2 ** 64


def _bar_spot(spot: dict) -> bool:
    return spot["id"].split("-")[0] in ("TB", "BB", "LB", "RB")


def _named(name: str, box: dict) -> str:
    """The key of a place that is not its spot's own box (another size, a
    chip): the name and the box, so each box has its own row of clear
    samples."""
    return f"{name}@{box['x']}:{box['y']}:{box['w']}:{box['h']}"


def _sid(key: str) -> str:
    """The spot id of a place's key."""
    return key.split("@", 1)[0]


class Room:
    """The places an icon may go in this scene and when each is clear:
    the speaker track's spots at the recipe's size, the hard rules per
    track sample (so a window is a lookup), and the preferences.

    `avoid`: the spots the AI said to avoid (a sign, a screen, a busy
    shelf). A veto on every spot the planner picks by itself — Where: Auto,
    and wherever the face guard shifts an icon to; a side the user chose
    keeps its avoided spots, scored down (their choice outranks the AI's).

    Each icon takes its own body box at a spot (box_for): a square at its
    size — the spot's own box at the element's — or a chip, pinned like the
    spot. A chip with no clear place in its window tries a circle of the same
    size there (looks_of / fits)."""

    def __init__(self, track: Any, el: rc.Element, *, face: str = "shift", width: int = 720,
                 height: int = 1280, fps: float = FPS, avoid: Sequence[str] = ()):
        from .. import speaker_track

        self.track = track
        self.fps = float(fps)
        self.face = face if face in ("shift", "reject", "warn") else "shift"
        self.width, self.height = (track.width, track.height) if track is not None else (int(width), int(height))
        self.layout = track.layout if track is not None else None
        spots = track.spots if track is not None else speaker_track.spot_grid(self.width, self.height)
        # Without a track (the speaker could not be found) the grid is sized
        # the same way, on a track of no samples.
        sizer = track if track is not None else speaker_track.Track(
            width=self.width, height=self.height, fps=self.fps, frames=0, step=1, times=[], heads=[],
            masks=None, layout=speaker_track.layout_of(self.width, self.height, (0, self.height, 0, self.width),
                                                       False))
        size = el.placement.size
        self.size = size
        self.sizer = sizer
        self.grid = {s["id"]: s for s in spots}
        self.spots = [{**s, "box": sizer.sized(s, size) if size else dict(s["box"])} for s in spots]
        self.available = track.anchors_available if track is not None else speaker_track.anchors_of(spots)
        self.anchor = el.placement.anchor
        self.custom = el.placement.box.model_dump(mode="json") if el.placement.box is not None else None
        self.avoid = {s["id"] for s in self.spots if s["id"] in set(avoid)}
        self.open = [s for s in self.spots if s["id"] not in self.avoid]
        self.font = label_key()
        self._ok: dict[str, Any] = {}
        self._boxes: dict[tuple, dict] = {}

    @property
    def shut(self) -> bool:
        """Every spot the planner may pick by itself is one the AI vetoed."""
        return not self.open and not (self.anchor == "custom" and self.custom is not None) \
            and not (self.anchor in GROUPS and self.anchor in self.available)

    # -- each icon's box ------------------------------------------------------
    def box_for(self, spot: dict, look: Optional[Look] = None) -> dict:
        """The body box an icon of `look` takes at `spot` (fractions): the
        spot at the icon's size (a square: the circle, the badge, the bare
        glyph) — the spot's own box at the element's size — or a chip: as
        tall as that square's CHIP_H, as wide as its words, pinned like the
        spot (a left-column spot keeps its left edge, a corner its corner, a
        bar spot its centre), inside the frame."""
        if look is None or (look.square and look.size in (None, self.size)):
            return spot["box"]
        key = (spot["id"], look.square, look.size, None if look.square else look.label)
        got = self._boxes.get(key)
        if got is None:
            size = look.size if look.size is not None else self.size
            base = self.grid.get(spot["id"], spot)
            square = self.sizer.sized(base, size) if size else dict(base["box"])
            got = square if look.square else self._chip(square, base.get("pin", "c"), look)
            self._boxes[key] = got
        return got

    def _chip(self, square: dict, pin: str, look: Look) -> dict:
        from .. import speaker_track

        W, H = self.width, self.height
        x0, y0 = square["x"] * W, square["y"] * H
        x1, y1 = x0 + square["w"] * W, y0 + square["h"] * H
        h = chip_height(square["w"] * W)
        w = chip_geometry(look.label, h, W, self.font or "").w
        cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
        nx, ny = {"l": (x0, cy - h / 2), "r": (x1 - w, cy - h / 2), "tl": (x0, y0),
                  "tr": (x1 - w, y0)}.get(pin, (cx - w / 2, cy - h / 2))
        nx = min(max(0.0, nx), W - w)
        ny = min(max(0.0, ny), H - h)
        return speaker_track._frac(nx, ny, w, h, W, H)

    def keyed(self, spot: dict, look: Optional[Look] = None) -> tuple[str, dict]:
        """(key, body box) of an icon of `look` at `spot`."""
        box = self.box_for(spot, look)
        return (spot["id"] if box is spot["box"] else _named(spot["id"], box)), box

    def user_box(self, name: str, box: dict, look: Optional[Look] = None) -> tuple[str, dict]:
        """(key, body box) of a box the user put an icon in: exactly as it is
        (moved inside the frame when it is not), or for a chip its height and
        the edge nearest the side of the frame it sits on kept, its width from
        its words."""
        from .. import speaker_track

        if look is None or look.square:
            x, y, w, h = (float(box[k]) for k in ("x", "y", "w", "h"))
            e = 1e-4
            if x >= -e and y >= -e and x + w <= 1 + e and y + h <= 1 + e:
                return name, box
            w, h = min(w, 1.0), min(h, 1.0)
            got = {"x": round(min(max(0.0, x), 1.0 - w), 4), "y": round(min(max(0.0, y), 1.0 - h), 4),
                   "w": round(w, 4), "h": round(h, 4)}
            return _named(name, got), got

        W, H = self.width, self.height
        h = max(4, int(round(float(box["h"]) * H)))
        w = chip_geometry(look.label, h, W, self.font or "").w
        x0 = float(box["x"]) * W
        x1 = x0 + float(box["w"]) * W
        nx = min(max(0.0, x0 if (x0 + x1) / 2 <= W / 2 else x1 - w), W - w)
        ny = min(max(0.0, float(box["y"]) * H), H - h)
        got = speaker_track._frac(nx, ny, w, h, W, H)
        return _named(name, got), got

    def looks_of(self, cue: Optional[Cue]) -> list[Optional[Look]]:
        """The looks an icon may show with, best first: its own; a chip with
        no box of the user's may show as a circle of its size instead."""
        look = cue.look if cue is not None else None
        if look is not None and look.style == "chip" and cue.box is None:
            return [look, look.as_circle()]
        return [look]

    def fits(self, cue: Cue, t0: float, t1: float) -> tuple[bool, Optional[Look]]:
        """(whether the icon has a clear place for [t0, t1], the look it has
        it with): its own look in a preferred place, then a chip as a circle
        there, then — with the face guard's shift — the same anywhere the AI
        did not veto."""
        looks = self.looks_of(cue)
        for look in looks:
            if any(self.clear(k, b, t0, t1) for k, b in self.preferred(cue, look)):
                return True, look
        if self.face == "shift":
            for look in looks:
                if any(self.clear(k, b, t0, t1) for k, b in self.everywhere(look)):
                    return True, look
        return False, None

    # -- the hard rules --------------------------------------------------------
    def _table(self, key: str, box: dict) -> Any:
        got = self._ok.get(key)
        if got is None:
            tr = self.track
            got = [bool(tr.feasible(box, float(t), float(t))) for t in tr.times]
            self._ok[key] = got
        return got

    def clear(self, key: str, box: dict, t0: float, t1: float) -> bool:
        """Box `key` clear of the speaker for the whole of [t0, t1]."""
        if self.track is None:
            return True
        if not self.track.inside(box):
            return False
        ok = self._table(key, box)
        return all(ok[i] for i in self.track.samples_in(t0, t1))

    def blocked_at(self, key: str, box: dict, t0: float, t1: float) -> Optional[float]:
        if self.track is None:
            return None
        ok = self._table(key, box)
        for i in self.track.samples_in(t0, t1):
            if not ok[i]:
                return round(float(self.track.times[i]), 2)
        return None

    def overlap(self, box: dict, t0: float, t1: float) -> float:
        return float(self.track.overlap(box, t0, t1)) if self.track is not None else 0.0

    def cover(self, box: dict, t0: float, t1: float) -> Optional[str]:
        """"face", "speaker" or None (speaker_track.Track.cover); None
        without a track (nothing is known)."""
        return self.track.cover(box, t0, t1) if self.track is not None else None

    # -- the preferences ------------------------------------------------------
    def preferred(self, cue: Optional[Cue], look: Optional[Look] = None) -> list[tuple[str, dict]]:
        """(key, box) an icon should go to, best kind first: its dragged box,
        the custom box, the Where chip's side, else every spot the AI did
        not veto — each as the body box of `look` (the cue's own when not
        given)."""
        look = look if look is not None else (cue.look if cue is not None else None)
        if cue is not None and cue.box is not None:
            return [self.user_box(f"you:{cue.key}", cue.box, look)]
        if self.anchor == "custom" and self.custom is not None:
            return [self.user_box("custom", self.custom, look)]
        if self.anchor in GROUPS and self.anchor in self.available:
            return [self.keyed(s, look) for s in self.spots if s["group"] == self.anchor]
        return [self.keyed(s, look) for s in self.open]

    def everywhere(self, look: Optional[Look] = None) -> list[tuple[str, dict]]:
        """Where the face guard may shift an icon to: any spot the AI did not veto."""
        return [self.keyed(s, look) for s in self.open]

    def placeable(self, cue: Cue, from_f: int, to_f: int) -> tuple[bool, Optional[float]]:
        """The scheduler's test (the face guard decides a blocked preference;
        a box the user put the icon in is kept whatever is there)."""
        if cue.box is not None:
            return True, None
        t0, t1 = from_f / self.fps - WINDOW_PAD, to_f / self.fps + WINDOW_PAD
        pref = self.preferred(cue)
        if not pref:
            return False, None                    # every spot vetoed by the AI
        if self.fits(cue, t0, t1)[0] or self.face == "warn":
            return True, None
        blocked = [t for t in (self.blocked_at(k, b, t0, t1) for k, b in pref) if t is not None]
        return False, (min(blocked) if blocked else None)

    # -- the score ------------------------------------------------------------
    def eye(self, box: dict) -> float:
        """How near a spot is to the band beside the speaker's shoulder (head
        centre down to the chin + half a head), 0..1."""
        tr = self.track
        if tr is None:
            return 0.5
        import numpy as np

        heads = np.asarray(tr.heads)
        heads = heads[heads[:, 0] >= 0] if len(heads) else heads
        if not len(heads):
            return 0.5
        x0, y0, x1, y1 = np.median(heads, axis=0)
        hh = max(1.0, y1 - y0)
        top, bottom = (y0 + y1) / 2, y1 + 0.5 * hh
        cy = (box["y"] + box["h"] / 2) * self.height
        if top <= cy <= bottom:
            return 1.0
        dist = top - cy if cy < top else cy - bottom
        return max(0.0, 1.0 - dist / (0.25 * self.height))

    def calm(self, spot: dict) -> float:
        if self.track is None:
            return 0.5
        c = spot.get("calm")
        return float(c) if c is not None else float(self.track.calm(spot["box"]))


def _where(spot: Optional[dict], layout: Optional[str]) -> str:
    if spot is None:
        return "Your spot"
    g = spot["group"]
    if layout == "letterbox" and _bar_spot(spot):
        return {"top": "In the top bar", "bottom": "In the bottom bar"}.get(g, f"In the {g} bar")
    if g == "top":
        return "Top corner"
    return g.capitalize()


def place(slots: list[Slot], room: Room, *, director_room: Optional[dict] = None,
          pattern: Optional[IconPattern] = None, seed: str = "",
          notes: Optional[list[str]] = None,
          forced: Optional[set[str]] = None) -> tuple[dict[str, dict], dict, list[dict], dict[str, str]]:
    """Where each scheduled icon goes (module docstring: WHERE).

    Returns ({glyph id: {spot, box, max_overlap, look, covered, clear}},
    home {spot, why, by}, the spots for "Show spots", {glyph id: why
    dropped}). `look` is the look it shows with: its own, or a chip that
    fits nowhere clear as a circle (said in `notes`). A box the user put an
    icon in is kept exactly; `covered` says what the speaker does to it
    ("speaker", "face", None) from `covered_at` (s), and `clear` is the
    clear spot nearest to it (the box "Move to a clear spot" writes; None
    when it is clear, or none is). plan() says it, unless the icon is put
    in front of her (off her face).

    One of the user's is never dropped for its place (module docstring:
    TRACKS): where the rules would leave it out, it takes a clear spot
    anywhere (one the AI said to avoid too: their call), else the place
    the speaker takes least of, behind her and said, as a box of theirs is.
    Two on screen together that would share a place take one each, partly
    behind the speaker before they overlap. `forced` collects the glyph ids
    these rules placed (the timing key says so: TRACK_RULES)."""
    notes = notes if notes is not None else []
    forced = forced if forced is not None else set()
    fps = room.fps
    windows = {s.cue.key: (s.from_f / fps - WINDOW_PAD, s.to_f / fps + WINDOW_PAD) for s in slots}
    whole = (0.0, (room.track.duration if room.track is not None else 0.0) + WINDOW_PAD)
    ai = {r["id"]: r for r in (director_room or {}).get("spots") or [] if r.get("id")}
    ai_home = (director_room or {}).get("home")
    out: dict[str, dict] = {}
    dropped: dict[str, str] = {}
    shared = [s for s in slots if s.cue.box is None]

    # Each icon's shape for its window: its own look, or a chip with no
    # clear place as a circle of its size.
    shape: dict[str, Optional[Look]] = {}
    for s in slots:
        if s.cue.box is not None:
            shape[s.cue.key] = s.cue.look          # the user's box: as asked, where asked
            continue
        ok, look = room.fits(s.cue, *windows[s.cue.key])
        shape[s.cue.key] = look if ok else s.cue.look
        if ok and s.cue.look is not None and look is not None and look.style != s.cue.look.style:
            notes.append(W_CHIP_CIRCLE.format(label=s.cue.label))

    def at(spot: dict, s: Slot) -> tuple[str, dict]:
        return room.keyed(spot, shape[s.cue.key])

    def clear_all(spot: dict, items: Sequence[Slot]) -> bool:
        if not items:
            return room.clear(spot["id"], spot["box"], *whole)
        return all(room.clear(*at(spot, s), *windows[s.cue.key]) for s in items)

    def score(spot: dict, items: Sequence[Slot]) -> float:
        v = W_CALM * room.calm(spot) + W_EYE * room.eye(spot["box"])
        if clear_all(spot, items):
            v += W_HOME
        if (ai.get(spot["id"]) or {}).get("verdict") == "avoid":
            v += W_AVOID
        if pattern is not None and pattern.box and _near(spot["box"], pattern.box, SIDE_REACH,
                                                         room.width, room.height):
            v += W_SIDE
        return v

    by_id = {s["id"]: s for s in room.spots}

    def ranked(keys: Sequence[str], items: Sequence[Slot]) -> list[dict]:
        cands = [by_id[k] for k in keys if k in by_id]
        bars_first = room.layout == "letterbox" and room.anchor == "auto"
        return sorted(cands, key=lambda s: (0 if (not bars_first or _bar_spot(s)) else 1,
                                            -round(score(s, items), 6), -_hash01(seed, s["id"])))

    def blocked(label: str, key: str, box: dict, win: tuple[float, float]) -> str:
        t = room.blocked_at(key, box, *win)
        return _t(t) if t is not None else "some point"

    def settle(slot: Slot, key: str, box: dict, spot: Optional[str]) -> None:
        win = windows[slot.cue.key]
        out[slot.cue.key] = {"spot": spot, "box": {k: round(float(v), 4) for k, v in box.items()},
                             "max_overlap": round(room.overlap(box, *win), 4), "look": shape[slot.cue.key],
                             "covered": None, "covered_at": None, "clear": None}

    def together(a: Slot, b: Slot) -> bool:
        """On screen at the same time (only icons timed by hand, and the
        user's on tracks of their own, can be)."""
        return a.from_f < b.to_f and b.from_f < a.to_f

    def nearest_clear(slot: Slot, box: dict) -> Optional[dict]:
        """The face guard's old shift, now only on request ("Move to a clear
        spot"): the clear spot nearest to where the user put the icon, at the
        size they gave it, off every icon on screen with it."""
        win = windows[slot.cue.key]
        look = _sized_as(shape[slot.cue.key], box, room)
        cx, cy = box["x"] + box["w"] / 2, box["y"] + box["h"] / 2
        near = []
        for sp in room.open:
            k, b = room.keyed(sp, look)
            near.append((math.hypot(b["x"] + b["w"] / 2 - cx, b["y"] + b["h"] / 2 - cy), sp["id"], k, b))
        others = [out[o.cue.key]["box"] for o in slots if o is not slot and o.cue.key in out and together(o, slot)]
        for _d, _id, k, b in sorted(near, key=lambda e: (e[0], e[1])):
            if room.clear(k, b, *win) and not any(_hit(b, o) for o in others):
                return {kk: round(float(v), 4) for kk, v in b.items()}
        return None

    def fallback(slot: Slot, pref: list[tuple[str, dict]]) -> None:
        """A blocked preference, by the face guard (one of the user's is
        never dropped: rescue)."""
        win = windows[slot.cue.key]
        if not pref:
            if slot.cue.user:
                rescue(slot, [at(sp, slot) for sp in ranked([sp["id"] for sp in room.spots], [slot])])
                return
            dropped[slot.cue.key] = NO_SPOT_AI
            return
        key, box = pref[0]
        if room.face == "warn":
            settle(slot, key, box, _sid(key) if _sid(key) in by_id else None)
            notes.append(W_COVERS.format(label=slot.cue.label, t=blocked(slot.cue.label, key, box, win)))
            return
        if room.face == "shift":
            cands = [s for s in ranked([k for k, _b in room.everywhere()], [slot])
                     if room.clear(*at(s, slot), *win)]
            if cands:
                notes.append(W_MOVED.format(label=slot.cue.label, t=blocked(slot.cue.label, key, box, win)))
                settle(slot, *at(cands[0], slot), cands[0]["id"])
                return
        if slot.cue.user:
            rescue(slot, pref)
            return
        t = room.blocked_at(key, box, *win)
        dropped[slot.cue.key] = NO_SPOT.format(t=_t(t if t is not None else win[0] + WINDOW_PAD))

    def harm(slot: Slot, box: dict, others: Sequence[dict] = ()) -> tuple:
        """How bad a place is that isn't clear (the least first): on an icon
        on screen with it, over the speaker's head, how much of it she
        covers."""
        win = windows[slot.cue.key]
        return (any(_hit(box, o) for o in others), room.cover(box, *win) == "face",
                round(room.overlap(box, *win), 4))

    def behind(slot: Slot, key: str, box: dict) -> None:
        """Placed where the speaker is in the way: behind her, and said (as
        a box of the user's over her is), the nearest clear spot offered."""
        win = windows[slot.cue.key]
        settle(slot, key, box, _sid(key) if _sid(key) in by_id else None)
        out[slot.cue.key]["covered"] = room.cover(box, *win) or "speaker"
        out[slot.cue.key]["covered_at"] = blocked(slot.cue.label, key, box, win)
        covered.append(slot)

    def rescue(slot: Slot, pref: list[tuple[str, dict]]) -> None:
        """One of the user's the face guard (or the AI's vetoes) would leave
        out: a clear spot anywhere, the ones the AI did not veto first; else
        the place the speaker takes least of, behind her."""
        forced.add(slot.cue.key)
        win = windows[slot.cue.key]
        vetoed = [sp["id"] for sp in room.spots if sp["id"] in room.avoid]
        for sp in ranked([sp["id"] for sp in room.open], [slot]) + ranked(vetoed, [slot]):
            k, b = at(sp, slot)
            if room.clear(k, b, *win):
                if pref:
                    notes.append(W_MOVED.format(label=slot.cue.label, t=blocked(slot.cue.label, *pref[0], win)))
                settle(slot, k, b, sp["id"])
                return
        cands = list(pref) + [at(sp, slot) for sp in room.spots]
        if not cands:                     # a room with no spots at all: nowhere to be
            dropped[slot.cue.key] = NO_SPOT_AI
            return
        key, box = min(cands, key=lambda kb: harm(slot, kb[1]))
        behind(slot, key, box)

    # The user's own boxes, exactly where they put them — over the speaker
    # too (behind her; in front only off her face, plan()'s layer rule).
    covered: list[Slot] = []
    for s in slots:
        if s.cue.box is None:
            continue
        key, box = room.preferred(s.cue, shape[s.cue.key])[0]
        settle(s, key, box, None)
        win = windows[s.cue.key]
        if not room.clear(key, box, *win):
            out[s.cue.key]["covered"] = room.cover(box, *win) or "speaker"
            out[s.cue.key]["covered_at"] = blocked(s.cue.label, key, box, win)
            covered.append(s)

    home: dict = {"spot": None, "why": "", "by": "score"}
    pref = room.preferred(None)
    if room.anchor == "custom" and room.custom is not None:
        home = {"spot": None, "why": "Your spot", "by": "you"}
        for s in shared:
            mine = room.preferred(s.cue, shape[s.cue.key])
            if room.clear(*mine[0], *windows[s.cue.key]):
                settle(s, *mine[0], None)
            else:
                fallback(s, mine)
    else:
        keys = [_sid(k) for k, _b in pref]
        order = ranked(keys, shared)
        chosen = None
        if ai_home in keys and clear_all(by_id[ai_home], shared) and \
                (room.layout != "letterbox" or room.anchor != "auto" or _bar_spot(by_id[ai_home])
                 or not any(_bar_spot(s) and clear_all(s, shared) for s in order)):
            chosen, by = by_id[ai_home], "ai"
        else:
            chosen = next((s for s in order if clear_all(s, shared)), None)
            by = "you" if room.anchor != "auto" else "score"
        if chosen is not None:
            for s in shared:
                settle(s, *at(chosen, s), chosen["id"])
            home = {"spot": chosen["id"], "why": _home_why(chosen, by, room, director_room, pattern),
                    "by": "you" if room.anchor != "auto" else by}
        else:
            # No one spot for all: each its own, the home's side first.
            lead = order[0] if order else None
            for s in shared:
                win = windows[s.cue.key]
                mine = sorted(order, key=lambda sp: 0 if lead and sp["group"] == lead["group"] else 1)
                spot = next((sp for sp in mine if room.clear(*at(sp, s), *win)), None)
                if spot is not None:
                    settle(s, *at(spot, s), spot["id"])
                else:
                    fallback(s, [at(sp, s) for sp in mine] or room.preferred(s.cue, shape[s.cue.key]))
            if lead is not None:
                home = {"spot": lead["id"], "why": _home_why(lead, "score", room, director_room, pattern),
                        "by": "you" if room.anchor != "auto" else "score"}

    # Icons timed by hand may be on screen together: one the planner placed
    # never sits on another then — it takes the next clear place of its own
    # (the user's boxes stay where they are, and are said to overlap).
    placed = sorted((s for s in slots if s.cue.key in out), key=lambda s: (s.from_f, s.cue.key))
    done: list[Slot] = [s for s in placed if s.cue.box is not None]
    for s in placed:
        if s.cue.box is not None:
            continue
        with_it = [o for o in done if together(o, s)]
        if any(_hit(out[s.cue.key]["box"], out[o.cue.key]["box"]) for o in with_it):
            keys = [_sid(k) for k, _b in room.preferred(s.cue, shape[s.cue.key])]
            keys += [sp["id"] for sp in room.open if sp["id"] not in keys]
            others = [out[o.cue.key]["box"] for o in with_it]
            for sp in ranked(keys, [s]):
                k, b = at(sp, s)
                if room.clear(k, b, *windows[s.cue.key]) and not any(_hit(b, o) for o in others):
                    settle(s, k, b, sp["id"])
                    break
            else:
                if s.cue.user:
                    # No clear place off the others: one of theirs takes a
                    # place of its own partly behind the speaker before the
                    # two overlap (any spot: the AI's vetoes too).
                    apart = [at(sp, s) for sp in ranked([sp["id"] for sp in room.spots], [s])]
                    apart = [kb for kb in apart if not any(_hit(kb[1], o) for o in others)]
                    if apart:
                        forced.add(s.cue.key)
                        k, b = min(apart, key=lambda kb: harm(s, kb[1]))
                        if room.clear(k, b, *windows[s.cue.key]):
                            settle(s, k, b, _sid(k) if _sid(k) in by_id else None)
                        else:
                            behind(s, k, b)
        done.append(s)
    for i, a in enumerate(placed):
        for b in placed[i + 1:]:
            if together(a, b) and _hit(out[a.cue.key]["box"], out[b.cue.key]["box"]):
                # The time an icon is said to be at is its settle (as the
                # list and the Moments chips say it), not its entrance.
                notes.append(W_OVERLAP.format(a=a.cue.label, b=b.cue.label,
                                              t=_t(max(a.settle_f, b.settle_f) / fps)))
    # "Move to a clear spot", for a box of the user's over the speaker (and
    # one of theirs put behind her for want of a clear place).
    for s in covered:
        if s.cue.key in out and out[s.cue.key]["covered"]:
            out[s.cue.key]["clear"] = nearest_clear(s, out[s.cue.key]["box"])

    shown = [s for s in slots if s.cue.key in out]
    spots_public = []
    for sp in room.spots:
        verdict = ai.get(sp["id"])
        spots_public.append({
            "id": sp["id"], "group": sp["group"], "box": sp["box"],
            "ok": clear_all(sp, shown),
            "ai": {"verdict": verdict["verdict"], "why": verdict.get("why") or ""} if verdict else None,
            "home": sp["id"] == home.get("spot"),
            "calm": round(room.calm(sp), 3),
        })
    return out, home, spots_public, dropped


def _hit(a: dict, b: dict) -> bool:
    """Two fractional boxes share some of the frame."""
    return a["x"] < b["x"] + b["w"] and b["x"] < a["x"] + a["w"] and \
        a["y"] < b["y"] + b["h"] and b["y"] < a["y"] + a["h"]


def _sized_as(look: Optional[Look], box: dict, room: Room) -> Optional[Look]:
    """`look` at the size of an icon in `box` (a square's side, or a chip's
    height over CHIP_H — a share of the frame's short side)."""
    from .. import icon_render

    if look is None:
        return None
    short = float(min(room.width, room.height))
    if look.square:
        side = min(box["w"] * room.width, box["h"] * room.height)
    else:
        side = box["h"] * room.height / icon_render.CHIP_H
    return replace(look, size=round(side / short, 4))


def _home_why(spot: dict, by: str, room: Room, director_room: Optional[dict],
              pattern: Optional[IconPattern]) -> str:
    where = _where(spot, room.layout)
    if by == "ai":
        why = (director_room or {}).get("home_why") or "the AI's pick"
        return f"{where} — AI: {why}"
    if room.anchor != "auto":
        return f"{where} — your pick"
    if room.layout == "letterbox" and _bar_spot(spot):
        return f"{where} — never on the picture"
    if pattern is not None and pattern.box and _near(spot["box"], pattern.box, SIDE_REACH,
                                                     room.width, room.height):
        return f"{where}, on the pattern's side"
    if room.track is not None and room.eye(spot["box"]) >= 0.8:
        return f"{where}, beside the speaker's shoulder"
    return f"{where}, a calm clear spot"


# ---------------------------------------------------------------------------
# Colours
# ---------------------------------------------------------------------------

def _cells(box: dict, h8: int, w8: int) -> tuple[int, int, int, int]:
    x0 = int(max(0, min(w8 - 1, math.floor(box["x"] * w8))))
    y0 = int(max(0, min(h8 - 1, math.floor(box["y"] * h8))))
    x1 = int(max(x0 + 1, min(w8, math.ceil((box["x"] + box["w"]) * w8))))
    y1 = int(max(y0 + 1, min(h8, math.ceil((box["y"] + box["h"]) * h8))))
    return x0, y0, x1, y1


def colour_samples(boxes: Sequence[tuple[dict, float, float]], track: Any,
                   bg: Optional[Backgrounds], ink: Any = None) -> tuple[list, list[float]]:
    """The background under each (box, from, to) at its start, middle and
    end (the speaker's own pixels left out), and how much of the box the
    patterns' ink covers there: (rgb samples, overlap shares). `ink`: one
    map for every window, or — patterns timed on the timeline — a function
    of the window giving the ink on screen then (IconPattern.ink_at)."""
    import numpy as np

    samples: list = []
    overlaps: list[float] = []
    if bg is None or bg.thumbs is None or not len(bg.thumbs):
        return samples, overlaps
    times = np.asarray(bg.times, dtype=np.float64)
    n, h8, w8 = bg.thumbs.shape[:3]
    for box, t0, t1 in boxes:
        x0, y0, x1, y1 = _cells(box, h8, w8)
        share = 0.0
        now = ink(t0, t1) if callable(ink) else ink
        if now is not None:
            a = np.asarray(now, dtype=np.float32)
            if a.shape == (h8, w8):
                share = float((a[y0:y1, x0:x1] >= INK_ON).mean())
        for t in (t0, (t0 + t1) / 2, t1):
            k = int(np.argmin(np.abs(times - t)))
            px = bg.thumbs[k, y0:y1, x0:x1].reshape(-1, 3).astype(np.float64)
            if track is not None and getattr(track, "masks", None) is not None and k < len(track.masks) \
                    and track.masks.shape[1:] == (h8, w8):
                keep = (track.masks[k, y0:y1, x0:x1] <= 127).reshape(-1)
                if keep.any():
                    px = px[keep]
            samples.append(tuple(int(round(v)) for v in px.mean(axis=0)))
            overlaps.append(share)
    return samples, overlaps


def backgrounds(clips: Sequence[tuple[Path, float]], track: Any, out: Optional[Path] = None) -> Optional[Backgrounds]:
    """The scene's frames at the track's sample times, each shrunk to the
    track's mask size — read once (sequentially: exact frames) and cached as
    `out` (npz). `clips`: (clip, where it starts in the scene) — the scene's
    own clip, or a version's parts. Blocking."""
    import cv2
    import numpy as np

    from .. import stages as st

    if track is None or not len(track.times):
        return None
    key = _sha([track.key, [[st.digest(Path(c)), round(float(o), 3)] for c, o in clips]])
    if out is not None and Path(out).is_file():
        try:
            with np.load(out, allow_pickle=False) as z:
                if str(z["key"]) == key:
                    return Backgrounds(key=key, times=z["times"], thumbs=z["thumbs"])
        except Exception:  # noqa: BLE001 — made again
            pass
    h8, w8 = track.masks.shape[1:]
    times = np.asarray(track.times, dtype=np.float64)
    thumbs = np.zeros((len(times), h8, w8, 3), np.uint8)
    got = np.zeros(len(times), bool)
    for clip, offset in clips:
        cap = cv2.VideoCapture(str(clip))
        if not cap.isOpened():
            continue
        fps = cap.get(cv2.CAP_PROP_FPS) or 0.0
        fps = fps if fps > 1 else FPS
        want = {}
        for k, t in enumerate(times):
            if not got[k] and t >= offset - 1e-6:
                want.setdefault(int(round((t - offset) * fps)), []).append(k)
        i = 0
        last = max(want) if want else -1
        try:
            while i <= last:
                ok, f = cap.read()
                if not ok:
                    break
                for k in want.get(i, ()):
                    small = cv2.resize(f, (w8, h8), interpolation=cv2.INTER_AREA)
                    thumbs[k] = small[:, :, ::-1]
                    got[k] = True
                i += 1
        finally:
            cap.release()
    if not got.any():
        return None
    for k in range(len(times)):                  # a sample past a clip's end: its neighbour
        if not got[k]:
            j = int(np.argmin(np.where(got, np.abs(times - times[k]), np.inf)))
            thumbs[k] = thumbs[j]
    body = Backgrounds(key=key, times=times, thumbs=thumbs)
    if out is not None:
        out = Path(out)
        out.parent.mkdir(parents=True, exist_ok=True)
        tmp = out.with_name(out.stem + ".tmp.npz")
        np.savez_compressed(tmp, key=np.asarray(key), times=times, thumbs=thumbs)
        tmp.replace(out)
    return body


def pick(shown: Sequence[tuple[dict, float, float]], ctx: IconCtx, props: rc.IconProps,
         pattern_colour: Optional[str]) -> dict:
    """One fill + ink for the scene (icon_render.pick_colours)."""
    from .. import icon_render

    ink = ctx.pattern.inks() if ctx.pattern is not None else None
    samples, overlaps = colour_samples(shown, ctx.track, ctx.backgrounds, ink)
    return icon_render.pick_colours(samples, pattern_colour, overlaps or 0.0, colour=props.colour)


# ---------------------------------------------------------------------------
# The plan
# ---------------------------------------------------------------------------

@dataclass
class IconPlan:
    """One scene's icon plan: what shows, when, where, in what colours — and
    everything else the studio lists (ideas, spots, the words)."""
    timing_key: str
    plan_key: str
    provisional: bool
    fps: float
    items: list[dict]                   # shown, in time order (frames inside)
    ideas: list[dict]
    colours: dict
    home: dict
    spots: list[dict]
    anchors_available: list[str]
    warnings: list[str]
    timeline: dict
    source: dict
    style: str
    entrance: str
    words_digest: Optional[str]
    pattern_url: Optional[str] = None
    # A version's icons kept as it froze them (kept_plan): what resolved()
    # gives, exactly — never made again from the items.
    frozen: Optional[list[dict]] = None
    # The director's answer this plan was made from, as it is stored (what
    # Generate logs its picks against: icon_feedback). Not in public().
    director: Optional[dict] = None

    def key(self) -> str:
        return self.plan_key

    def resolved(self) -> list[dict]:
        """Every shown icon as the run stamps it (recipe.IconResolved): each
        with its own colours, style, words, layer and motion (an icon with
        the old look dumps exactly as before: its new fields are left out).
        A kept plan's are the version's own, as it froze them."""
        if self.frozen is not None:
            return [dict(r) for r in self.frozen]
        out = []
        for it in self.items:
            r = rc.IconResolved.model_validate({
                "glyph": it["glyph"], "label": it["label"][: rc.ICON_LABEL_MAX],
                "from_sec": round(it["from_f"] / self.fps, 4), "settle_sec": round(it["settle_f"] / self.fps, 4),
                "to_sec": round(it["to_f"] / self.fps, 4), "box": it["box"],
                "fill": it["fill"], "ink": it["ink"],
                "fx": {"entrance": it["enter"], "seed": it["seed"], "exit": it["leave"]}, "style": it["style"],
                "word": (it.get("word") or None) and it["word"][:60],
                "purpose": (it.get("purpose") or "")[: rc.ICON_PURPOSE_MAX],
                "max_overlap": min(1.0, max(0.0, float(it.get("max_overlap") or 0.0))),
                "weight": it["weight"], "text": it["text"], "font": it["font"], "layer": it["layer"],
                "look_v": it["look_v"],
            })
            out.append(r.model_dump(mode="json"))
        return out

    def stamper(self, *, frame_size: tuple[int, int], offset_sec: float = 0.0,
                fps: Optional[float] = None, glyphs: Optional[dict] = None):
        """The icon_render.Stamper of this plan for a chunk starting at
        `offset_sec` (custom glyphs must be fetched first: glyphs_for)."""
        from .. import icon_render

        return icon_render.Stamper(self.resolved(), fps=fps or self.fps, offset_sec=offset_sec,
                                   frame_size=frame_size, glyphs=glyphs)

    def public(self) -> dict:
        """POST /icons/plan's plan part (the studio adds state, steps and
        the underlay)."""
        f = self.fps
        items = []
        for it in self.items:
            settle = it["settle_f"] / f
            to = it["to_f"] / f
            items.append({
                "glyph": it["glyph"], "label": it["label"], "source": it["source"],
                "pinned": it["pinned"], "off_word": it["off_word"],
                "from": round(it["from_f"] / f, 3), "settle": round(settle, 3), "to": round(to, 3),
                "hold": round((it["to_f"] - it["settle_f"] - OUT_F) / f, 3),
                "box": it["box"], "spot": it.get("spot"), "word": it.get("word"), "w": it.get("w"),
                "purpose": it.get("purpose") or "", "priority": it.get("priority"),
                "mentions": it.get("mentions") or [],
                "moment_t": round(min(settle + MOMENT_AFTER, to - 1.0 / f), 3),
                "max_overlap": it.get("max_overlap", 0.0),
                # Its look as the run draws it (`asked`: the style asked for —
                # a chip with no room shows as a circle), and its glyph's path
                # in that weight (the studio's quick drawing while a plan is
                # on its way).
                "style": it["style"], "asked": it["asked"], "weight": it["weight"],
                "fill": it["fill"], "ink": it["ink"], "text": it["text"], "font": it["font"],
                "layer": it["layer"], "look_v": it["look_v"], "enter": it["enter"], "leave": it["leave"],
                "size": it["size"], "notes": list(it["notes"]), "edited": dict(it["edited"]),
                "d": _glyph_d(it["glyph"], it["weight"]),
                # A box of the user's over the speaker: what covers it
                # ("speaker", "face") and the clear spot nearest to it.
                "covered": it.get("covered"), "clear": it.get("clear"),
                # "Why?": who picked it, what was said and when, why it
                # helps, where it sits and why that colour (why_of).
                "why": it.get("why"),
                # Its track on the timeline (0: the AI's, one at a time; None:
                # a version's own, kept as it froze them), and, moved out of
                # a zone, {at, said, why} (Scheduler._shift_of).
                "lane": it.get("lane"), "shifted": it.get("shifted"),
            })
        return {
            "plan_key": self.plan_key, "timing_key": self.timing_key, "provisional": self.provisional,
            "source": self.source, "items": items, "ideas": self.ideas, "colours": self.colours,
            "home": self.home, "spots": self.spots, "anchors_available": self.anchors_available,
            "warnings": self.warnings, "timeline": self.timeline, "words_digest": self.words_digest,
            "style": self.style, "entrance": self.entrance,
        }


def _glyph_d(glyph: dict, weight: str) -> Optional[str]:
    """A library icon's path in `weight` (None for a drawn one)."""
    if glyph.get("source") != "library":
        return None
    try:
        from .. import icon_catalog

        return icon_catalog.library_d(glyph["id"], weight)
    except Exception:  # noqa: BLE001 — the studio then fetches it by id
        return None


def timeline_of(words: dict, *, duration: float = 0.0, chunks: Sequence[dict] = (),
                enters: bool = False, zones: Optional[Sequence[dict]] = None) -> dict:
    """The scene's timeline lane: its seconds, the speech (voiced span,
    loudness, every word and when it starts), its chunks and where no icon
    may be (the opening, the patterns' zones — `zones`, else the entrance
    when `enters` — and the end). Shown as soon as the words are in."""
    duration = float(duration or words.get("duration") or 0.0)
    zones = [{"from": 0.0, "to": HEAD_CLEAR, "why": ZONE_START}, *zones_of(enters, zones)]
    if duration > 0:
        zones.append({"from": round(max(0.0, duration - TAIL_CLEAR), 3), "to": round(duration, 3),
                      "why": ZONE_END})
    return {
        "seconds": round(duration, 3), "voiced": words.get("voiced"), "envelope": words.get("envelope") or [],
        "words": [{"w": int(w["w"]), "text": w["text"], "s": round(float(w["s"]), 3) if w.get("s") is not None
                   else None, "said": bool(w.get("said"))} for w in words.get("words") or []],
        "chunks": list(chunks), "zones": zones, "digest": words.get("digest"),
    }


def _director_digest(director: dict) -> str:
    return _sha([director.get("key"), director.get("source"), director.get("ideas"), director.get("room")])


def _mentions_public(cue: Cue, words: dict) -> list[dict]:
    by = _by_w(words)
    out = []
    for m in cue.mentions:
        w = int(m["w"])
        s = m.get("s", by.get(w, {}).get("s"))
        if s is not None:
            out.append({"w": w, "at": round(float(s), 3)})
    return out


# Why an idea isn't shown, as its state: no room by another icon, a zone (the
# opening, a pattern coming in or going out), too near the end, a limit, no
# clear spot.
DROP_STATES = ("no_room", "zone", "near_end", "limit", "no_spot")


def _state(reason: str) -> str:
    """The state of an idea left out for `reason` (DROP_STATES)."""
    if reason == NEAR_END:
        return "near_end"
    if reason in (IN_ENTRANCE, IN_EXIT, NEAR_START):
        return "zone"
    if reason.startswith(OVER_LIMIT.split("{", 1)[0]) or reason.startswith(OVER_MAX.split("{", 1)[0]):
        return "limit"
    if reason == NO_SPOT_AI or reason.startswith(NO_SPOT.split("{", 1)[0]):
        return "no_spot"
    return "no_room"


def _team_ids(team: Optional[Sequence[dict]]) -> dict[str, dict]:
    return {str(r.get("id")): r for r in team or [] if r.get("id")}


def why_of(c: Cue, it: dict, words: dict, tw: Optional[rc.IconTweak], *, home: dict,
           team: Optional[Sequence[dict]] = None, fps: float = FPS) -> dict:
    """A shown icon's "Why?" — the facts, and one line of them (`text`):

      by        who picked it: "ai" (the AI, on its own) | "keyword" | "you"
                (added by you: from the picker, at a moment, even an icon
                the AI picked too) | "kept" (the AI's pick, kept by you: its
                Keep adds it as it is, with no moment) | "team" (your team's
                icon, added by you)
      priority  the AI's 1-5 for this idea (None when the AI didn't judge it)
      said      {word, at}: the word it lands on and when it is said (None
                off its word)
      time      "word" (on its word) | "pinned" (a word you picked) | "hand"
                (a time you set)
      purpose   why it helps the viewer (the AI's line)
      place     {by, text}: "you" (your box) | the home spot's "ai" / "score"
                / "you" with its why | "spot" (a place of its own)
      colour    {by, text}: "you" (its own) | "scene" (the scene's, and why)
                — filled in once the colours are picked."""
    by_w = _by_w(words)
    team_row = _team_ids(team).get(c.key) if c.glyph.get("source") == "custom" else None
    if c.source == "user":
        # An icon picked in the picker carries the moment it was added at:
        # the user's own add even when the AI picked that icon too (the
        # round-2 QA: the library's Team over the AI's Peer); Keep adds the
        # AI's pick with none.
        by = ("team" if c.glyph.get("source") == "custom" else
              "kept" if c.idea is not None and c.idea.get("auto") and c.at is None else "you")
    else:
        by = c.source
    w = it.get("w")
    said = None
    if w is not None and it.get("word"):
        at = by_w.get(int(w), {}).get("s")
        said = {"word": it["word"], "at": round(float(at if at is not None else it["settle_f"] / fps), 3)}
    time = "hand" if c.off_word else "pinned" if c.pinned else "word"
    priority = int(c.idea["priority"]) if c.idea is not None and c.idea.get("source") == "ai" else None
    if tw is not None and tw.box is not None:
        place = {"by": "you", "text": ""}
    elif it.get("spot") and it["spot"] == home.get("spot"):
        place = {"by": home.get("by") or "score", "text": home.get("why") or ""}
    else:
        place = {"by": "spot", "text": ""}
    lead = {"ai": WHY_AI, "keyword": WHY_KEYWORD, "you": WHY_YOU, "kept": WHY_KEPT}.get(by) or WHY_TEAM.format(
        label=(team_row or {}).get("name") or c.label)
    parts = [lead]
    if priority is not None and by in ("ai", "kept"):
        parts.append(WHY_KEY.format(p=priority))
    if time == "hand":
        parts.append(WHY_TIMED.format(t=_t(it["settle_f"] / fps)))
    elif said is not None:
        parts.append(WHY_SAID.format(word=said["word"], t=_t(said["at"])))
    if c.purpose:
        parts.append(c.purpose)
    return {"by": by, "text": " · ".join(parts), "priority": priority, "said": said, "time": time,
            "purpose": c.purpose or "", "place": place, "colour": None}


def idea_why(idea: dict, state: str, reason: Optional[str], *, raw: Any = None) -> dict:
    """Why an idea is where it is (not shown, or shown): {by, text}.

      on        it shows (its item's why says the rest)
      you       removed by you
      word      one of yours that waits for a word to land on
      room      no room / the limit / too near the end or a pattern's move
      ai        the AI left it out (its own words)
      check     the AI kept it, one of our rules left it out (a "no", the
                library, a list …)
      silent    the AI gave it no verdict
      hopeful   hopeful but general: yours to add, never shown on its own
      suggest   the AI suggests it (never on its own)
      keyword   a keyword match (the AI was off or down)
      team      your team's icon, said here
      gone      no longer in the icon library"""
    from .. import director as dmod

    text = reason or ""
    if state == "on":
        return {"by": "on", "text": ""}
    if state == "dismissed":
        return {"by": "you", "text": REMOVED}
    if state == "gone":
        return {"by": "gone", "text": GONE}
    if reason == NOT_TIED:
        return {"by": "word", "text": NOT_TIED}
    if state in DROP_STATES:
        return {"by": "room", "text": text}
    if state == "refused":
        return {"by": dmod.refused_by({**idea, "verdict": "refuse"}, raw) or "ai", "text": text}
    if idea.get("source") == "team":
        return {"by": "team", "text": text}
    if idea.get("hopeful"):
        return {"by": "hopeful", "text": idea.get("ai_reason") or dmod.WHY_HOPEFUL}
    if idea.get("verdict") == "suggest" and idea.get("source") == "ai":
        return {"by": "suggest", "text": text}
    return {"by": "keyword" if idea.get("source") == "keyword" else "suggest", "text": text}


def team_ideas(words: dict, team: Optional[Sequence[dict]], taken: set[str]) -> list[dict]:
    """The team's icons that are SAID here (their name, label or tags, said
    and meant: not in a negation, an idiom or the brand — said_mentions), as
    ideas to add with one tap: state "available", source "team", never
    placed on their own. At most TEAM_SUGGEST_MAX, in speech order; none
    already listed (`taken`: shown, added, removed, or the AI's)."""
    out = []
    for gid, row in _team_ids(team).items():
        if gid in taken or not rc._CUSTOM_ID_RE.match(gid):
            continue
        name = str(row.get("name") or "")
        label = str(row.get("label") or "") or name[: rc.ICON_LABEL_MAX]
        found = said_mentions(words, [name, label, *(row.get("tags") or [])])
        if not found:
            continue
        m = found[0]
        at = round(float(m["s"]), 3)
        reason = TEAM_SAID.format(t=_t(at))
        out.append({
            "glyph": {"source": "custom", "id": gid}, "label": label or name or gid, "word": m["quote"],
            "w": int(m["w"]), "at": at, "purpose": "", "priority": 0, "state": "available",
            "reason": reason, "added": False, "source": "team", "auto": False,
            "why": {"by": "team", "text": reason},
        })
    out.sort(key=lambda i: (i["at"], i["label"]))
    return out[:TEAM_SUGGEST_MAX]


def _ideas(director: dict, cues_: list[Cue], shown: set[str], drops: dict[str, str],
           props: rc.IconProps, words: dict, names: Callable[[str], Optional[str]],
           has: Callable[[str], bool] = catalog_has, *, team: Optional[Sequence[dict]] = None) -> list[dict]:
    """Every idea the AI (or the keyword list) found, every icon the user
    added, and the team's icons said here (team_ideas) — one per glyph, with
    its state, its reason and its "Why?" (idea_why)."""
    by = _by_w(words)
    added = {a.glyph.id: a for a in props.added}
    dismissed = set(props.dismissed)
    cue_by = {c.key: c for c in cues_}
    out = []
    seen: set[str] = set()
    for gid, idea in sorted(_ideas_by_icon(director).items(), key=lambda kv: (int(kv[1].get("w") or 0), kv[0])):
        seen.add(gid)
        w = int(idea["w"]) if int(idea.get("w", -1)) >= 0 else None
        word = idea.get("word") or (by.get(w, {}).get("text") if w is not None else None)
        at = round(float(idea["s"]), 3) if idea.get("s") is not None else None
        cue = cue_by.get(gid)
        if gid in shown:
            state, reason = "on", None
        elif gid in dismissed:
            state, reason = "dismissed", REMOVED
        elif gid in added and drops.get(gid) == NOT_TIED:
            # Added, but the AI's words for it were refused (a "no"): it
            # waits for a word the user picks, like any other of theirs.
            state, reason, w, word, at = "available", NOT_TIED, None, None, None
        elif gid in drops:
            state, reason = _state(drops[gid]), drops[gid]
        elif idea.get("verdict") == "refuse":
            state, reason = "refused", idea.get("reason") or "Refused by the AI"
        else:
            state, reason = "available", (idea.get("reason") or None) if idea.get("verdict") == "suggest" else None
        out.append({
            "glyph": _glyph(gid), "label": (cue.label if cue else None) or idea.get("label") or names(gid) or gid,
            "word": word, "w": w, "at": at,
            "purpose": idea.get("purpose") or "", "priority": int(idea.get("priority") or 0),
            "state": state, "reason": reason, "added": gid in added,
            "source": idea.get("source") or "ai",
            # The AI shows it on its own (so a removed one comes back with
            # "Add back"; any other is added by the user).
            "auto": bool(idea.get("auto")),
            "why": idea_why(idea, state, reason, raw=director.get("raw")),
        })
    for gid, add in added.items():
        if gid in seen:
            continue
        cue = cue_by.get(gid)
        head = cue.anchors[0] if cue and cue.anchors else None
        if gid in shown:
            state, reason = "on", None
        elif cue is None and add.glyph.source == "library" and not has(gid):
            # Listed so it can be removed (it holds one of the user's places).
            state, reason = "gone", GONE
        elif gid in drops:
            state, reason = (_state(drops[gid]) if drops[gid] != NOT_TIED else "available"), drops[gid]
        else:
            state, reason = "available", NOT_TIED
        out.append({
            "glyph": add.glyph.model_dump(mode="json"), "label": add.label or (cue.label if cue else gid),
            "word": by.get(head[1], {}).get("text") if head else None, "w": head[1] if head else None,
            "at": round(head[2], 3) if head else None, "purpose": cue.purpose if cue else "",
            "priority": cue.priority if cue else 0, "state": state, "reason": reason, "added": True,
            "source": "user", "auto": False,
            "why": idea_why({"source": "user"}, state, reason),
        })
    taken = seen | set(added) | dismissed | shown
    return out + team_ideas(words, team, taken)


def plan(el: rc.Element, ctx: IconCtx) -> IconPlan:
    """The scene's icon plan for the icon element `el` (module docstring).
    Pure and deterministic: the same inputs give the same plan and key.
    Synchronous (a few ms warm; call it off the event loop anyway)."""
    from .. import icon_render

    props = rc.icon_props(el)
    pdump = props.model_dump(mode="json", exclude={"resolved"})
    words = ctx.words or {}
    director = ctx.director or {}
    names = ctx.names or catalog_name
    terms = ctx.terms or catalog_terms
    fps = float(ctx.fps or FPS)
    duration = float(ctx.duration or words.get("duration") or 0.0)
    track = ctx.track
    notes: list[str] = list(ctx.notes)
    if words.get("source") == "estimate":
        notes.append(W_ESTIMATE)
    if props.ai and director.get("source") == "keywords" and director.get("failed_at"):
        notes.append(W_AI_DOWN)
    entrance = el.animation.entrance if el.animation.entrance in rc.ICON_ENTRANCES else "auto"
    entrance = "glitch" if entrance == "auto" else entrance
    seed = ctx.seed or ""

    tprops = {k: pdump[k] for k in ("ai", "omni_own", "hold_sec", "added", "dismissed", "tweaks")}
    # An icon's own look, and the circle / chip style, change its box — so
    # where it finds room: in the timing's key, but only when set, so every
    # key made before them stays as it was.
    if props.looks:
        tprops["looks"] = pdump["looks"]
    if props.style not in ("badge", "glyph"):
        tprops["style"] = props.style
    # A box or a time the user set by hand: kept exactly / a fixed slot.
    if any(tw.box is not None or tw.off_word for tw in props.tweaks.values()):
        tprops["hand"] = HAND_RULES
    # A team icon the user added lands where its name, label and tags are
    # said: a teammate re-tagging it moves it, so its words are in the key —
    # only when there is one, so every other key stays as it was (a library
    # icon's words come with the code).
    team_terms = {a.glyph.id: sorted({t.strip().lower() for t in terms(a.glyph.id) if (t or "").strip()})
                  for a in props.added if a.glyph.source == "custom"}
    team_terms = {g: t for g, t in team_terms.items() if t}
    if team_terms:
        tprops["terms"] = {g: _sha(t) for g, t in sorted(team_terms.items())}
    # The tracks the user put icons on: only when there is one (an add's
    # moment, IconAdd.at, is in `added` only when set).
    if props.lanes:
        tprops["lanes"] = pdump["lanes"]
    timed = {
        "v": PLAN_VERSION, "words": words.get("digest"), "director": _director_digest(director),
        "track": getattr(track, "key", None), "duration": round(duration, 3), "fps": fps,
        "enters": bool(ctx.enters), "face": ctx.face, "seed": seed, "base": ctx.base,
        "props": tprops,
        "placement": {"anchor": el.placement.anchor, "size": el.placement.size,
                      "box": el.placement.box.model_dump(mode="json") if el.placement.box else None},
    }
    # Patterns timed on the timeline: their zones (only then — every other
    # key stays as it was).
    if ctx.zones is not None:
        timed["zones"] = [dict(z) for z in ctx.zones]
    pat = ctx.pattern
    pattern_colour = pat.colour.lower() if pat is not None and pat.colour else None
    provisional = bool(pat is not None and pat.provisional)

    droom = director.get("room") or {}
    room = Room(track, el, face=ctx.face, width=ctx.width, height=ctx.height, fps=fps,
                avoid=[r["id"] for r in droom.get("spots") or [] if r.get("id") and r.get("verdict") == "avoid"])
    cue_list = cues(el, props, words, director, names=names, terms=terms, has=ctx.has or catalog_has,
                    notes=notes)
    if props.omni_own == "keep":
        # Omni's own icons stay (a version that already has them): none of
        # ours, so two sets never show.
        notes.append(W_OMNI_KEEP)
        cue_list = []
    for c in cue_list:
        c.look = look_of(c, props, el)
        if c.look.style == "chip" and room.font is None:
            c.look = c.look.as_circle()
            notes.append(W_NO_FONT.format(label=c.label))
    sched = Scheduler(duration=duration, enters=ctx.enters, voiced=words.get("voiced"), fps=fps,
                      placeable=room.placeable, zones=ctx.zones)
    slots, drops = sched.run(cue_list)
    if room.shut:
        # Nowhere to go but spots the AI vetoed: say so, not "hands up".
        blocked = NO_SPOT.split("{", 1)[0]
        for c in cue_list:
            if c.box is None and drops.get(c.key, "").startswith(blocked):
                drops[c.key] = NO_SPOT_AI
    forced: set[str] = set()
    placed, home, spots, dropped = place(slots, room, director_room=director.get("room"), pattern=pat,
                                        seed=seed, notes=notes, forced=forced)
    drops.update(dropped)
    slots = [s for s in slots if s.cue.key in placed]
    # One of the user's that only the tracks' rules show, or an AI pick they
    # left out for one (TRACK_RULES): the timing key says so, so a plan kept
    # from before them is never reused.
    if sched.rescued or forced or sched.yielded:
        tprops["tracks"] = TRACK_RULES
    # One added at a moment kept there on a time of its own (MOMENT_RULES):
    # likewise, so a plan kept from before (it went after the others on
    # track 0, or to another mention) is never reused.
    if sched.moments:
        tprops["moment"] = MOMENT_RULES
    timing_key = _sha(timed)
    bg_key = ctx.backgrounds.key if ctx.backgrounds is not None else None
    keyed = {"timing": timing_key, "pattern": pat.key if pat is not None else None,
             "provisional": provisional, "pattern_colour": pattern_colour,
             "colour": pdump["colour"], "style": pdump["style"], "entrance": entrance, "bg": bg_key}
    if props.style == "chip" or any(look.style == "chip" for look in props.looks.values()):
        keyed["font"] = label_key()                # a chip's words are drawn in it
    plan_key = _sha(keyed)

    by = _by_w(words)
    short = float(min(room.width, room.height))
    items = []
    for s in slots:
        c = s.cue
        w = s.anchor[1] if s.anchor is not None else None
        p = placed[c.key]
        look = p.get("look") or c.look
        box = p["box"]
        tw = props.tweaks.get(c.key)
        it = {
            "glyph": c.glyph, "label": c.label, "source": c.source, "pinned": c.pinned,
            "off_word": c.off_word, "from_f": s.from_f, "settle_f": s.settle_f, "to_f": s.to_f,
            "box": box, "spot": p["spot"], "max_overlap": p["max_overlap"],
            "word": by.get(w, {}).get("text") if w is not None else None, "w": w,
            "purpose": c.purpose, "priority": c.priority, "mentions": _mentions_public(c, words),
            "seed": f"{seed}:{c.key}",
            "style": look.style, "asked": c.look.style, "layer": look.layer, "enter": look.enter,
            "leave": look.leave, "weight": "fill", "look_v": 0, "text": None, "font": None,
            "own_fill": look.fill, "own_ink": look.ink, "notes": list(sched.hand_notes.get(c.key, [])),
            "edited": {"look": c.look.own, "place": bool(tw is not None and tw.box is not None),
                       "time": bool(c.pinned or (tw is not None and tw.hold_sec is not None))},
            "covered": p.get("covered"), "clear": p.get("clear"),
            # Its track on the timeline, and (moved out of a zone) where it
            # was said: the studio's "Show at 0.2 s anyway".
            "lane": s.lane, "shifted": sched.shifts.get(c.key),
        }
        it["why"] = why_of(c, it, words, tw, home=home, team=ctx.team, fps=fps)
        notes.extend(sched.hand_notes.get(c.key, []))
        if it["shifted"] is not None:
            msg = W_SHIFTED.format(label=c.label, t=_t(s.settle_f / fps), why=SHIFT_WHY[it["shifted"]["why"]])
            it["notes"].append(msg)
            notes.append(msg)
        if look.style != c.look.style:
            it["notes"].append(W_CHIP_CIRCLE.format(label=c.label))
        if look.style in ("circle", "chip"):
            it["look_v"] = icon_render.LOOK_V
            it["weight"] = "bold" if c.glyph.get("source") == "library" else "fill"
        if look.style == "chip":
            h = max(4, int(round(box["h"] * room.height)))
            it["text"], it["font"] = chip_geometry(look.label, h, room.width, room.font).text, room.font
            it["size"] = round(h / icon_render.CHIP_H / short, 3)
        else:
            it["size"] = round(min(box["w"] * room.width, box["h"] * room.height) / short, 3)
        # "In front" is the user's own choice (props.looks): it holds, over
        # the face too (the owner's call, 2026-09-30).
        if it["covered"] and it["layer"] == "behind":
            # A box of the user's over the speaker, behind her (or one of
            # theirs with no clear place anywhere): said.
            msg = (W_COVERS_FACE if it["covered"] == "face" else W_COVERS).format(label=c.label, t=p["covered_at"])
            it["notes"].append(msg)
            notes.append(msg)
        items.append(it)

    # The scene's colour for the icons that take it; the others their own.
    auto = [it for it in items if it["own_fill"] is None]
    shown = [(it["box"], it["from_f"] / fps, it["to_f"] / fps) for it in (auto or items)]
    if not shown and home.get("spot"):
        hb = next((sp["box"] for sp in spots if sp["id"] == home["spot"]), None)
        if hb is not None:
            shown = [(hb, 0.0, duration)]
    colours = pick(shown, ctx, props, pattern_colour)
    if colours.get("warning") and (auto or not items):
        notes.append(colours["warning"])
    ink_map = ctx.pattern.inks() if ctx.pattern is not None else None
    for it in items:
        own, ink = it.pop("own_fill"), it.pop("own_ink")
        it["why"]["colour"] = ({"by": "you", "text": ""} if own is not None
                               else {"by": "scene", "text": colours.get("why") or ""})
        if own is None:
            it["fill"], it["ink"] = colours["fill"], ink or colours["ink"]
            continue
        it["fill"], it["ink"] = own, ink or icon_render.ink_for(own, pattern_colour)
        samples, overlaps = colour_samples([(it["box"], it["from_f"] / fps, it["to_f"] / fps)], ctx.track,
                                           ctx.backgrounds, ink_map)
        where = icon_render.hard_to_see(own, samples, pattern_colour, overlaps or 0.0) if samples else None
        if where is not None:
            msg = W_ICON_COLOUR.format(label=it["label"], colour=icon_render.COLOUR_NAMES.get(own, own),
                                       where="pattern" if where == "pattern" else "background")
            it["notes"].append(msg)
            notes.append(msg)

    timeline = timeline_of(words, duration=duration, chunks=ctx.chunks, enters=ctx.enters, zones=ctx.zones)
    seen: set[str] = set()
    warnings = [n for n in notes if not (n in seen or seen.add(n))]
    # The lists show the AI's refusal of a hopeful word as a suggestion
    # (director.as_suggestions) — the lists only: what shows, and the keys,
    # come from its answer as it is.
    from .. import director as dmod

    listed = dmod.as_suggestions(director, words)
    return IconPlan(
        timing_key=timing_key, plan_key=plan_key, provisional=provisional, fps=fps, items=items,
        ideas=_ideas(listed, cue_list, {it["glyph"]["id"] for it in items}, drops, props, words, names,
                     ctx.has or catalog_has, team=ctx.team),
        colours={"fill": colours["fill"], "ink": colours["ink"], "why": colours["why"]},
        home=home, spots=spots, anchors_available=list(room.available), warnings=warnings,
        timeline=timeline,
        source={"words": words.get("source") or "estimate", "director": director.get("source") or "keywords"},
        style=props.style, entrance=entrance, words_digest=words.get("digest"),
        pattern_url=pat.url if pat is not None else None, director=director,
    )


# ---------------------------------------------------------------------------
# Reordering ("show this one first")
# ---------------------------------------------------------------------------

OWN_TRACK = "That icon is on a track of its own: move it on the timeline"


def _track0(items: Sequence[dict]) -> list[str]:
    """The glyph ids on track 0 (the AI's, one at a time), in time order."""
    return [it["glyph"]["id"] for it in items if not it.get("lane")]


def with_tweaks(el: rc.Element, tweaks: dict[str, dict]) -> rc.Element:
    """`el` with `tweaks` merged into props.tweaks (validated; a copy)."""
    props = dict(el.props)
    merged = {**(props.get("tweaks") or {})}
    for gid, tw in tweaks.items():
        merged[gid] = {**(merged.get(gid) or {}), **tw}
    props["tweaks"] = merged
    out = el.model_copy(update={"props": props})
    rc.icon_props(out)
    return out


def reorder(el: rc.Element, ctx: IconCtx, glyph: str, to: int) -> dict:
    """Move the shown icon `glyph` to position `to` of the on-screen order,
    keeping what every icon means — never by swapping their moments (that
    would put </> on screen while they say "free"):

      1. it lands on another mention of its own idea that comes in the
         right place (the smallest move);
      2. else the ones it jumps over land on another mention of theirs;
      3. else nothing moves: `choice` asks — show it there anyway, off its
         word (`anyway`: the pin that does it), or keep the speech order.

    Returns {ok, tweaks (to merge into props.tweaks), message} or
    {ok: False, choice: {message, anyway: {tweaks, sec} | None}}.

    The order is track 0's (the AI's, one at a time): `to` is a place in
    it, and an icon on a track of its own has none (it is moved on the
    timeline)."""
    base = plan(el, ctx)
    order = _track0(base.items)
    if glyph not in order:
        on = any(it["glyph"]["id"] == glyph for it in base.items)
        return {"ok": False, "message": OWN_TRACK if on else "That icon isn't on screen now", "tweaks": {}}
    cur = order.index(glyph)
    to = max(0, min(len(order) - 1, int(to)))
    if to == cur:
        return {"ok": True, "tweaks": {}, "message": None}
    want = [g for g in order if g != glyph]
    want.insert(to, glyph)
    digest = ctx.words.get("digest")
    by = _by_w(ctx.words)
    items = {it["glyph"]["id"]: it for it in base.items}
    earlier = to < cur
    jumped = order[to:cur] if earlier else order[cur + 1:to + 1]

    def ordered(tweaks: dict) -> bool:
        try:
            got = plan(with_tweaks(el, tweaks), ctx)
        except rc.RecipeError:
            return False
        return _track0(got.items) == want

    def moves(gid: str, direction_earlier: bool) -> list[int]:
        at = items[gid]["settle_f"] / base.fps
        ms = [m for m in items[gid]["mentions"] if (m["at"] < at - 1e-3 if direction_earlier else m["at"] > at + 1e-3)]
        ms.sort(key=lambda m: -m["at"] if direction_earlier else m["at"])
        return [m["w"] for m in ms]

    for w in moves(glyph, earlier):
        tweaks = {glyph: {"w": w, "words_digest": digest, "off_word": False, "sec": None}}
        if ordered(tweaks):
            return {"ok": True, "tweaks": tweaks,
                    "message": f"'{items[glyph]['label']}' now shows {'first' if to == 0 else 'there'} — "
                               f"on the {'earlier' if earlier else 'later'} '{by[w]['text']}' at {_t(by[w]['s'])}"}
    options = [moves(g, not earlier) for g in jumped]
    if all(options):
        for combo in itertools.islice(itertools.product(*options), 32):
            tweaks = {g: {"w": w, "words_digest": digest, "off_word": False, "sec": None}
                      for g, w in zip(jumped, combo)}
            if ordered(tweaks):
                return {"ok": True, "tweaks": tweaks,
                        "message": f"'{items[glyph]['label']}' now shows {'first' if to == 0 else 'there'}"}
    # Off its word, if the user wants that: the earliest moment it fits in
    # its new place — after the icon now before it, clear of the next.
    label = items[glyph]["label"]
    other = items[jumped[0] if earlier else jumped[-1]]
    said_at = items[glyph]["w"]
    word_at = by.get(said_at, {}).get("s") if said_at is not None else None
    message = (f"'{label}' is only said at {_t(word_at)}, {'after' if earlier else 'before'} "
               f"'{other['label']}'" if word_at is not None else
               f"'{label}' isn't said {'before' if earlier else 'after'} '{other['label']}'")
    f = base.fps
    lo_f = int(math.ceil(HEAD_CLEAR * f)) + IN_F
    for z in zones_of(ctx.enters, ctx.zones):
        # After a pattern coming in at the scene's start.
        if float(z["from"]) <= 0.0:
            lo_f = max(lo_f, int(math.ceil(float(z["to"]) * f)))
    before = want[want.index(glyph) - 1] if want.index(glyph) > 0 else None
    if before is not None:
        lo_f = max(lo_f, items[before]["to_f"] + int(round(GAP_KEY * f)) + IN_F)
    anyway = None
    for k in range(0, int(round(1.2 * f)), 2):
        sec = round((lo_f + k) / f, 3)
        tweaks = {glyph: {"off_word": True, "sec": sec, "w": None}}
        if ordered(tweaks):
            anyway = {"tweaks": tweaks, "sec": sec}
            break
    return {"ok": False, "tweaks": {}, "choice": {"message": message, "anyway": anyway}}


# ---------------------------------------------------------------------------
# Keeping a version's icons ("Keep v3's icons")
# ---------------------------------------------------------------------------

KEPT_WHY = "kept"


def kept_plan(resolved: Sequence[dict], *, version_id: int, width: int, height: int, fps: float = FPS,
              words: Optional[dict] = None, duration: float = 0.0, chunks: Sequence[dict] = (),
              enters: bool = False, style: str = "badge", entrance: str = "glitch",
              zones: Optional[Sequence[dict]] = None) -> IconPlan:
    """A version's icons exactly as it froze them (its props.resolved), as a
    plan: no director, no scheduler, no placing. Its resolved() IS that list
    — so Generate freezes the very same icons and the run stamps them where
    that version did — and its items say each one as the studio lists it
    (pinned: they don't move). `words` (when they are in) give the lane its
    speech. The key names the version and its icons. Pure."""
    from .. import icon_render

    frozen = [rc.IconResolved.model_validate(r).model_dump(mode="json") for r in resolved]
    short = float(min(width, height)) or 1.0
    items = []
    for r in frozen:
        box, fx, look = r["box"], r["fx"], r.get("style") or "badge"
        if look == "chip":
            size = round(box["h"] * height / icon_render.CHIP_H / short, 3)
        else:
            size = round(min(box["w"] * width, box["h"] * height) / short, 3)
        items.append({
            "glyph": r["glyph"], "label": r.get("label") or "", "source": KEPT_WHY, "pinned": True,
            "off_word": False, "from_f": int(round(float(r["from_sec"]) * fps)),
            "settle_f": int(round(float(r["settle_sec"]) * fps)), "to_f": int(round(float(r["to_sec"]) * fps)),
            "box": box, "spot": None, "max_overlap": float(r.get("max_overlap") or 0.0), "word": r.get("word"),
            "w": None, "purpose": r.get("purpose") or "", "priority": 0, "mentions": [], "seed": fx.get("seed"),
            "style": look, "asked": look, "layer": r.get("layer") or "behind", "enter": fx.get("entrance"),
            "leave": fx.get("exit"), "weight": r.get("weight") or "fill", "look_v": int(r.get("look_v") or 0),
            "text": r.get("text"), "font": r.get("font"), "fill": r["fill"], "ink": r["ink"], "size": size,
            "notes": [], "edited": {"look": False, "place": False, "time": False},
            "why": {"by": "version", "text": WHY_VERSION.format(n=int(version_id)), "priority": None,
                    "said": ({"word": r["word"], "at": None} if r.get("word") else None), "time": "version",
                    "purpose": r.get("purpose") or "", "place": {"by": "version", "text": ""},
                    "colour": {"by": "version", "text": ""}},
        })
    first = frozen[0] if frozen else None
    key = _sha({"v": PLAN_VERSION, "kept": int(version_id), "resolved": frozen})
    return IconPlan(
        timing_key=key, plan_key=key, provisional=False, fps=float(fps), items=items, ideas=[],
        colours={"fill": first["fill"] if first else "#7f2bf2", "ink": first["ink"] if first else "#ffffff",
                 "why": KEPT_WHY},
        home={}, spots=[], anchors_available=[], warnings=[],
        timeline=timeline_of(words, duration=duration, chunks=chunks, enters=enters, zones=zones) if words else None,
        source={"words": (words or {}).get("source") or "estimate", "director": KEPT_WHY}, style=style,
        entrance=entrance, words_digest=(words or {}).get("digest"), frozen=frozen,
    )


# ---------------------------------------------------------------------------
# Generate and the run
# ---------------------------------------------------------------------------

def strip_resolved(recipe: rc.Recipe) -> rc.Recipe:
    """The recipe with no frozen plan on its icon element: a preview and a
    plan never read one a client sent (only the router's materialise writes
    it)."""
    el = rc.icon_element(recipe, enabled=False)
    if el is None or "resolved" not in el.props:
        return recipe
    props = {k: v for k, v in el.props.items() if k != "resolved"}
    return recipe.model_copy(update={"elements": [
        e.model_copy(update={"props": props}) if e.id == el.id else e for e in recipe.elements]})


def materialise(recipe: rc.Recipe, p: IconPlan) -> rc.Recipe:
    """The recipe as Generate stores it: the icon element's props.resolved =
    the plan's shown icons, exactly as they will be stamped (a new recipe;
    nothing else in it changes)."""
    el = rc.icon_element(recipe)
    if el is None:
        return recipe
    props = {**el.props, "resolved": p.resolved()}
    return rc.normalize(recipe.model_copy(update={"elements": [
        e.model_copy(update={"props": props}) if e.id == el.id else e for e in recipe.elements]}))


def lanes_of(p: Any) -> Optional[list[int]]:
    """Each shown icon's track on the timeline (0: the AI's, one at a time;
    module docstring: TRACKS), in the order p.resolved() freezes them: what
    Generate keeps beside a version's frozen icons (options.icon_lanes), so
    its read-only timeline has them on the tracks its draft had. Never in
    the frozen list itself (the render list has no track). None for a plan
    with no tracks of the planner's (a version's own, kept as it froze them:
    kept_plan) or anything that isn't a plan."""
    items = getattr(p, "items", None)
    if not isinstance(items, list) or not items:
        return None
    lanes = [it.get("lane") if isinstance(it, dict) else None for it in items]
    if any(isinstance(n, bool) or not isinstance(n, int) or n < 0 for n in lanes):
        return None
    return lanes


async def plan_for_run(recipe: rc.Recipe, make_ctx: Optional[Callable[[], Any]] = None
                       ) -> tuple[list[dict], Optional[IconPlan]]:
    """What a run stamps: (resolved icons, the plan made now or None).

    Reads ONLY props.resolved (the plan the user saw, frozen at Generate).
    A recipe that has an icon element and no resolved (an API client) is
    planned here by the same code — `make_ctx` (async) gathers its inputs —
    and the plan is returned so the run can record it. No icon element: []."""
    el = rc.icon_element(recipe)
    if el is None:
        return [], None
    props = rc.icon_props(el)
    if props.resolved is not None:
        return [r.model_dump(mode="json") for r in props.resolved], None
    if make_ctx is None:
        return [], None
    ctx = await make_ctx()
    p = await asyncio.to_thread(plan, el, ctx)
    return p.resolved(), p


def overlaps(item: Any, start: float, seconds: float) -> bool:
    """Whether a resolved icon is on screen in the chunk [start, start + seconds)."""
    get = item.get if isinstance(item, dict) else (lambda k: getattr(item, k))
    return float(get("from_sec")) < start + seconds and float(get("to_sec")) > start


def chunk_spans(seconds: Sequence[float]) -> list[tuple[float, float]]:
    """(start, seconds) of each chunk in the scene from each chunk's length —
    as the studio's timeline has them, so a run stamps each icon on the very
    frames the plan (and its exact still) put it on."""
    out: list[tuple[float, float]] = []
    start = 0.0
    for sec in seconds:
        out.append((round(start, 3), float(sec)))
        start += float(sec)
    return out


# stamp_only lays the icons on with no matte (the speaker pass failed): only
# those the speaker covers at most this much of in their window are kept.
UNMATTED_MAX = 0.03


def unmatted(resolved: Sequence[Any]) -> list:
    """The resolved icons that may be stamped with no matte at all: those
    the speaker (almost) never covers, and those in front of her (they need
    no matte)."""
    get = (lambda r, k: r.get(k)) if resolved and isinstance(resolved[0], dict) else getattr
    return [r for r in resolved
            if float(get(r, "max_overlap") or 0.0) <= UNMATTED_MAX or (get(r, "layer") or "behind") == "front"]


async def stampers(resolved: Sequence[Any], chunks: Sequence[tuple[float, float]], *,
                   frame_size: tuple[int, int], fps: float = FPS) -> list:
    """One icon_render.Stamper per chunk (start, seconds) of the scene — None
    for a chunk no icon shows in (restore_speaker then gets no stamps, and
    stays byte-identical). Custom glyphs are fetched once."""
    from .. import icon_catalog
    from .. import icon_render

    if not resolved:
        return [None for _ in chunks]
    glyphs = await icon_catalog.glyphs_for(resolved)
    out = []
    for start, seconds in chunks:
        on = [r for r in resolved if overlaps(r, start, seconds)]
        out.append(icon_render.Stamper(on, fps=fps, offset_sec=start, frame_size=frame_size, glyphs=glyphs)
                   if on else None)
    return out
