"""What Omni is told — the prompt for one chunk of one scene.

A scene prompt is a HEAD (what the edit is: the Gemini brief's
omni_scene_prompt, or the harness's own opening) followed by one clause per
element Omni must draw. Today that is the pattern clause, moved here word
for word from rebrand_pipeline (tests/test_rebrand_golden.py pins every
word): the approved Omni run is exactly unpaintable(brief head) + this
clause. Two or three patterns get one plural clause instead
(patterns_clause), written from scratch so the approved one never moves.
"""
from __future__ import annotations

import re
from typing import Optional, Sequence, Union

from .. import pattern_library
from .. import rebrand_pattern_ref as pattern_ref

Refs = Union[pattern_ref.PatternRef, Sequence[pattern_ref.PatternRef], None]

# Things Omni paints when a prompt names them: a colour code comes back as
# on-screen text, and "42" as a logo. The brief is told not to write either;
# this strips any that slip through.
PAINTABLE = re.compile(r"\s*#[0-9a-fA-F]{6}\b|\s*#[0-9a-fA-F]{3}\b|\b42\b\s*")

# The harness's own opening, for a recipe the brief cannot direct (the brief
# is told never to place a logo, letters or icons). Nothing in it names a
# colour or a mark: the target image carries both.
HARNESS_HEAD = (
    "EDIT this talking-head clip. Keep the speaker, their face, expression, lip movements, "
    "voice timing, clothes and every movement exactly as shot, and keep the camera, the "
    "framing and the lighting unchanged. Only add the brand graphics described below."
)
INTENSITY_NOTE = {
    "subtle": "Keep the graphics quiet: thin, calm, never louder than the speaker.",
    "balanced": "",
    "bold": "Let the graphics read clearly and confidently, still behind the speaker's words.",
}
# Building on a finished version ("Add on top"): the clip Omni edits already
# carries the version's graphics, and they must come back untouched.
KEEP_CLAUSE = (
    "This clip already carries brand graphics. Every line, shape and mark in it that is not "
    "part of the room stays exactly as it is: same place, size and colour, holding still; never "
    "redrawn, moved, faded or animated again."
)
# When the recipe's icons are laid in by us after Omni (an icon element that
# hides Omni's own: recipe.icons_by_pipeline), so Omni must add none of its
# own — it once drew a line-art head nobody asked for (experiment 23). Worded
# as ICON_SPEC has it, without "faces" or "icons": a prompt that names a
# thing is how Omni gets the idea to paint it.
ICONS_CLAUSE = (
    " Add no new drawn symbols, pictograms, badges or figures anywhere in the frame; only the "
    "patterns are added; the speaker stays exactly as shot."
)
_COUNT_WORDS = {2: "two", 3: "three"}
_ORDINALS = ("first", "second", "third")
# Fractions of the frame as words — digits in a prompt come back painted.
_FRACTIONS = (
    (0.25, "quarter"), (1 / 3, "third"), (0.40, "two fifths"), (0.50, "half"),
    (0.60, "three fifths"), (2 / 3, "two thirds"), (0.75, "three quarters"),
)


def unpaintable(text: str) -> str:
    t = re.sub(r"[ \t]{2,}", " ", PAINTABLE.sub(" ", text))
    return re.sub(r"[ \t]+([.,;:!?])", r"\1", t).strip()


def harness_head(intensity: str = "balanced", notes: str = "") -> str:
    parts = [HARNESS_HEAD, INTENSITY_NOTE.get(intensity, "")]
    if notes.strip():
        parts.append(unpaintable(notes.strip()))
    return " ".join(p for p in parts if p)


def as_refs(refs: Refs) -> list[pattern_ref.PatternRef]:
    """One ref, a list of them or None, as a list."""
    if refs is None:
        return []
    if isinstance(refs, pattern_ref.PatternRef):
        return [refs]
    return [r for r in refs if r is not None]


def has_entrance(refs: Refs) -> bool:
    """Whether a pattern enters (its animation is not "none"; with several,
    any of them): only then does a clip's first chunk get the entrance
    prompt and the veil guard."""
    return any(pattern_library.entrance_prompt(r.animation, r.layout) for r in as_refs(refs))


def pattern_clause(ref: pattern_ref.PatternRef, *, entrance: bool = False) -> str:
    """Name the attached image and pin its colours. This replaces the old
    logo clause: the owner took the logo off Omni altogether, and nothing
    else ever told Omni what the strip the icons sit on should be made of,
    so it made something up. The pattern IS that strip, already coloured
    from the intro, so the only job left to the model is to reproduce it.

    What Omni is shown is the TARGET frame (compose.compose_target: the
    clip's own frame with the pattern already laid into the strip), because
    every looser reference was reinterpreted — a transparent PNG came back as
    a checkerboard, a whole-frame lattice as a solid panel of bars.

    Nothing Omni could PAINT goes into this text: no colour codes and no
    "42". Omni wrote "#0663a3" and a "42" mark into the strip when the
    prompt named them. The image carries the colour.

    `entrance` makes the pattern reveal left to right at the start of the
    clip. Only the first chunk of a clip gets it — every later chunk is told
    the strip is already there, or the pattern would re-enter at each 10 s
    chunk seam.

    The pattern is part of the BACKGROUND (owner, after the approved strip:
    "make it big ... behind the character"): the target already shows the
    speaker covering it, and the text says so, because a model left to itself
    draws graphics on top of everything."""
    # The entrance is the pattern's (or the run's) chosen animation, from
    # pattern_library.ANIMATIONS. None of them says "a soft-edged wipe": Omni
    # sometimes drew that literally, as a frosted white panel sliding in over
    # the room (experiment 19).
    enter = pattern_library.entrance_prompt(ref.animation, ref.layout) if entrance else None
    motion = enter or (
        "The pattern is already exactly as in the attached image from the first frame of this "
        "clip and holds still throughout: no entrance, no fade, no flicker."
    )
    # Only structure (2.png) is made of bars; the bead strips that now also
    # stand on the left (5.png, 1.png) end in beads, so they get the general
    # sentence rather than being called bars.
    outline = (
        "Keep the pattern's own outline: on the right its bars end in their own tips at "
        "different points, exactly as in the image; never trim it to a straight edge or box it "
        "into a strip."
        if ref.layout.placement == "left" and ref.slug == pattern_ref.OMNI_SLUG else
        "Keep the pattern's own outline: it ends where the art itself ends, exactly as in the "
        "image; never trim it to a straight edge or box it into a strip."
    )
    return (
        "\nThe attached image is the TARGET: this clip's own frame with the brand pattern "
        f"already laid into {ref.where}. Make the video look exactly like it: reproduce that "
        "exact line pattern there, with the same shapes, the same large scale, the same "
        "position and the same colour, as crisp solid lines, with the video's own background "
        "visible between the lines. The pattern is part of the BACKGROUND, BEHIND the speaker: "
        "the speaker stands in front of it, so their hair, head covering, face, body, clothes "
        "and hands cover it completely wherever they overlap it, exactly as in the image, and "
        f"it stays behind them as they move. Never draw a single line over the speaker. {outline} "
        "Do not draw a panel, a solid block of colour, a checkerboard or any other "
        "texture behind it or instead of it. Do not write or draw anything else: no logo, no "
        "letters, no numbers, no colour codes. The speaker and everything the pattern does not "
        f"cover stay exactly as shot. {motion}"
    )


def _fraction(reach: float) -> str:
    """A share of the frame in words ("half", "a third" …) — the nearest
    simple fraction, never a digit."""
    if reach >= 0.9:
        return "all"
    word = min(_FRACTIONS, key=lambda f: abs(f[0] - reach))[1]
    return word if " " in word or word == "half" else f"a {word}"


def place_words(layout: pattern_ref.Layout) -> str:
    """Where a pattern is, in words with no digit in them. For the built-in
    places this reads as where_text without ", behind the speaker"."""
    if layout.placement in ("left", "right"):
        share = _fraction(layout.reach)
        if share == "all":
            return f"the whole frame, running in from the {layout.placement} edge"
        if share == "half":
            return f"the {layout.placement} half of the frame"
        return f"{share} of the frame along its {layout.placement} side"
    if layout.placement in ("top", "bottom"):
        return (f"a band along the {layout.placement} edge of the frame, from the "
                f"{layout.placement}-{layout.anchor} corner")
    return "the whole background of the frame"


def patterns_clause(refs: Sequence[pattern_ref.PatternRef], *, entrance: bool = False) -> str:
    """The pattern clause for TWO or THREE patterns in one frame. Every guard
    sentence of the approved single clause, made plural, plus the one thing
    only several patterns need: they stay separate. Word numbers only ("two",
    "the second"), no colour and no digit — Omni paints what a prompt names,
    and the target image carries the colours and the places.

    `entrance`: this chunk opens the clip, and every pattern that enters does
    so together within the first second (the veil guard covers the first
    VEIL_GUARD_SEC); the rest are there from the first frame. Later chunks are
    told everything holds still."""
    refs = list(refs)
    count = _COUNT_WORDS.get(len(refs), "several")
    places = "; ".join(f"the {_ORDINALS[i] if i < len(_ORDINALS) else 'next'} in {place_words(r.layout)}"
                       for i, r in enumerate(refs))
    enters = [pattern_library.entrance_phrase(r.animation)
              if pattern_library.entrance_prompt(r.animation, r.layout) else None for r in refs]
    if entrance and any(enters):
        each = "; ".join(
            f"the {_ORDINALS[i] if i < len(_ORDINALS) else 'next'} pattern "
            + (phrase or "is already there from the first frame and does not move")
            for i, phrase in enumerate(enters))
        motion = (
            "ENTRANCE: at the very first frame the patterns that enter are not there yet, just "
            "the original video. Over the first second, BEHIND the speaker, " + each + "; then "
            "every pattern holds still, exactly as in the attached image, for the rest of the "
            "clip. " + pattern_library._NOTHING_ELSE
        )
    else:
        motion = ("Every pattern is already exactly as in the attached image from the first frame "
                  "of this clip and holds still throughout: no entrance, no fade, no flicker.")
    return (
        f"\nThe attached image is the TARGET: this clip's own frame with {count} brand patterns "
        f"already laid in: {places}. Make the video look exactly like it: reproduce each of those "
        "exact line patterns in its own place, with the same shapes, the same scale, the same "
        "position and the same colour as in the image, as crisp solid lines, with the video's own "
        "background visible between the lines. They are separate patterns: never merge them, join "
        "them, swap their places or add another. Every pattern is part of the BACKGROUND, BEHIND "
        "the speaker: the speaker stands in front of the patterns, so the speaker's hair, head "
        "covering, face, body, clothes and hands cover them completely wherever they overlap, "
        "exactly as in the image, and the patterns stay behind the speaker as they move. Never "
        "draw a single line over the speaker. Keep each pattern's own outline: each ends where its "
        "art itself ends, exactly as in the image; never trim one to a straight edge or box it "
        "into a strip. Do not draw a panel, a solid block of colour, a checkerboard or any other "
        "texture behind them or instead of them. Do not write or draw anything else: no logo, no "
        "letters, no numbers, no colour codes. The speaker and everything the patterns do not "
        f"cover stay exactly as shot. {motion}"
    )


def elements_clause(refs: Refs, *, entrance: bool = False) -> str:
    """What Omni is told about the recipe's patterns: nothing for none, the
    approved clause word for word for one, the plural clause for more."""
    found = as_refs(refs)
    if not found:
        return ""
    if len(found) == 1:
        return pattern_clause(found[0], entrance=entrance)
    return patterns_clause(found, entrance=entrance)


def scene_prompt(head: str, refs: Refs, *, entrance: bool = False, no_icons: bool = False,
                 keep: bool = False) -> str:
    """One chunk's prompt: the head made unpaintable, then (building on a
    finished version, `keep`) KEEP_CLAUSE, then the pattern clause (none
    without a pattern — Omni then runs bare; the plural clause for several),
    then (icons laid in by us, `no_icons`) ICONS_CLAUSE. One ref and no
    flags is exactly the approved prompt."""
    p = unpaintable(head or "")
    if keep:
        p += " " + KEEP_CLAUSE
    p += elements_clause(refs, entrance=entrance)
    if no_icons:
        p += ICONS_CLAUSE
    return p


# ---------------------------------------------------------------------------
# A brief's opening that names the wrong place
# ---------------------------------------------------------------------------

_REGION = re.compile(r"\b(left|right|top|bottom|upper|lower)[- ](half|side|edge|strip|third|"
                     r"band|corner|column|area|part|portion)s?\b", re.I)
_REGION_WORD = {"upper": "top", "lower": "bottom"}
_ONLY = re.compile(r"\bonly\b", re.I)


def pattern_regions(refs: Refs) -> set[str]:
    """The frame's sides the patterns cover: a strip its side, a band its edge
    and corner side, a full background every side."""
    out: set[str] = set()
    for r in as_refs(refs):
        lay = r.layout
        if lay.placement in ("left", "right"):
            out.add(lay.placement)
        elif lay.placement in ("top", "bottom"):
            out |= {lay.placement, lay.anchor}
        else:
            out |= {"left", "right", "top", "bottom"}
    return out


def head_misplaces(head: str, refs: Refs) -> Optional[str]:
    """Why a brief's opening would steer Omni away from some pattern, or None.

    Brief openings hard-code a region ("Add graphics ONLY to the left half"),
    written for the one pattern they were told about. With several patterns
    that sends Omni away from the others: an opening that names a side no
    pattern covers, or confines the graphics to fewer sides than the
    patterns cover, is not used (the harness's own opening is)."""
    found = as_refs(refs)
    if not found or not head:
        return None
    named = {_REGION_WORD.get(m.group(1).lower(), m.group(1).lower()) for m in _REGION.finditer(head)}
    if not named:
        return None
    covered = pattern_regions(found)
    stray = sorted(named - covered)
    if stray:
        return f"it names the {stray[0]} side, where no pattern is"
    if _ONLY.search(head) and len(found) > 1 and not covered <= named:
        return "it keeps the graphics to " + " and ".join(sorted(named)) + " only"
    return None
