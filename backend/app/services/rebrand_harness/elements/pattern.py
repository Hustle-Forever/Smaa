"""The pattern element — a brand pattern laid into the background, behind the
speaker, coloured from the intro.

Moved here from rebrand_pipeline._build_pattern_ref / _plan_placement and
driven by the recipe instead of the run's options:

  props.slug          which pattern (pattern_library; structure by default)
  placement.anchor    auto   the pattern's own place (one fixed place, or
                             several the planner chooses between per clip)
                      edge-left / edge-right / edge-top / edge-bottom / full
                             that side: the pattern's own layout there if it
                             has one, the same strip mirrored for the other
                             side, else a layout worked out from its art
                             (pattern_library.fit_profile) — so any upload
                             can go on any edge
  animation.entrance  auto = the pattern's own, aimed at the edge it grows from
  colours / props.colour   the intro's colour, the brand's, or a picked one
  props.layout        a pinned place, kept while it is still one of the
                      pattern's places (or their mirror) — see _pinned
  props.shuffle       mixed into this element's planner seed only

The in-house engine also reads its time (timing(): a span in scene seconds
with an entrance at its start and an exit at its end) and its hand move
(recipe.hand_of): made from its pinned place, the move rides on its ref
(PatternRef.hand — rebrand_pattern_ref.placed_layer draws it there, for the
target, the ink the others keep off and the plate alike), it holds no slot
(hand_slot), and a pin that no longer fits takes the move away with it (and
says so). A client's props.resolved is never read (strip_resolved);
Generate writes one (materialise: the place and colour the preview drew),
and the run draws a frozen pattern from it (from_resolved) — no planner.

Up to three patterns (build_all): the intro's colours are dug once, each
pattern renders into its own folder, and they are placed in list order —
each keeps off the slots and the ink of the ones before it, so adding one
never moves those already placed.

The 42 default (structure, auto, auto, intro) takes exactly the approved
path — tests/test_rebrand_golden.py.
"""
from __future__ import annotations

import asyncio
import dataclasses
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Iterable, Optional, Sequence

from ... import pattern_library
from ... import pattern_planner
from ... import person_matte
from ... import rebrand_pattern_ref as pattern_ref
from .. import recipe as rc

log = logging.getLogger("archivist.rebrand.harness.pattern")

EDGE_PLACEMENT = {"edge-left": "left", "edge-right": "right", "edge-top": "top",
                  "edge-bottom": "bottom", "full": "full"}

Step = Callable[..., Awaitable[None]]            # step(name, status="running", detail="")


@dataclass
class PatternJob:
    """What building the pattern needs from the run it is part of."""
    exp_id: int
    aspect: str
    out_dir: Path                                 # <work>/pattern
    intro: Optional[Path]                         # the intro's RAW source (its colours)
    intro_is_still: bool
    seed: str                                     # the planner's seed, before ":<slug>"
    plan_frame: Callable[[], Awaitable[Optional[Path]]]   # the frame an auto place is chosen on
    step: Step
    # The intro's colours when already dug (the studio digs them once per
    # scene: scanning an intro video back past its fade-out takes up to a
    # minute). None: dug here, as a run always has.
    colours: Optional[pattern_ref.Colours] = None
    # The speaker's mask on the plan frame. None: person_matte.person_mask
    # (a run); the studio passes its own, local-only and shared.
    person_mask: Optional[Callable[[Path], Awaitable[Any]]] = None


@dataclass
class PatternResult:
    ref: Optional[pattern_ref.PatternRef] = None
    report: Optional[dict] = None                 # the planner's report (auto places)
    error: str = ""                               # why there is no ref, when building failed
    # The library had no such pattern (or could not be reached): the default
    # was drawn in its place. A preview drawn so is never kept (studio).
    substituted: bool = False
    # Several patterns (build_all):
    element_id: str = ""
    slot: Optional[str] = None                    # pattern_planner.slot of its place
    moved: Optional[str] = None                   # why it is not in its own place
    overlap: float = 0.0                          # share of its VISIBLE ink on the earlier patterns'
    warnings: list[str] = field(default_factory=list)


async def _thread(fn, *args, **kwargs):
    return await asyncio.to_thread(fn, *args, **kwargs)


def colour_override(colours: rc.Colours, el: rc.Element) -> Optional[str]:
    """The colour the recipe asks for instead of the intro's, or None."""
    own = el.props.get("colour")
    if isinstance(own, str) and own.startswith("#") and len(own) == 7:
        return own.lower()
    if colours.source == "brand":
        return pattern_ref.BRAND_GROUND
    if colours.source == "custom" and colours.custom:
        return colours.custom.lower()
    return None


def seed_for(el: rc.Element, slug: str, seed: str) -> str:
    """One element's planner seed. The first pattern ("pattern") keeps the
    approved `<seed>:<slug>`; any other is seeded by its own id too, so two
    of the same pattern draw independently; a shuffle tag moves that element
    alone ("Another spot")."""
    out = f"{seed}:{slug}" if el.id == "pattern" else f"{seed}:{el.id}:{slug}"
    shuffle = el.props.get("shuffle")
    return f"{out}:{shuffle}" if shuffle else out


slot = pattern_planner.slot


def hand_slot(el: rc.Element) -> str:
    """The slot of a pattern moved by hand: its own ("hand:<id>"), which no
    other pattern's place is — it holds no side or corner; the patterns
    after it keep off its ink only."""
    return f"hand:{el.id}"


# ---------------------------------------------------------------------------
# Time (the in-house engine)
# ---------------------------------------------------------------------------

# Each ramp keeps at least this much when a short span squeezes both.
RAMP_SQUEEZE_MIN = 0.1


@dataclass(frozen=True)
class Timing:
    """When a pattern shows, in scene seconds: its entrance runs t_in ->
    t_full, it holds to t_hold_end, and its exit runs to t_out (gone).
    `entrance` / `exit` are resolved ("auto" is never left): the drawn
    entrance, and the exit that ends it ("none" holds to the cut)."""
    t_in: float
    t_full: float
    t_hold_end: float
    t_out: float
    entrance: str
    exit: str
    runs_to_end: bool

    def phase(self, t: float) -> tuple[str, float]:
        """("before" | "in" | "hold" | "out" | "after", q) at scene time t —
        q the linear progress 0..1 of that phase (the entrance's, the
        exit's; 1 while it holds, 0 before, 1 after)."""
        if t < self.t_in:
            return "before", 0.0
        if t >= self.t_out:
            return "after", 1.0
        if t < self.t_full:
            return "in", (t - self.t_in) / (self.t_full - self.t_in)
        if t < self.t_hold_end:
            return "hold", 1.0
        return "out", (t - self.t_hold_end) / (self.t_out - self.t_hold_end)

    def public(self) -> dict:
        return {"t_in": round(self.t_in, 4), "t_full": round(self.t_full, 4),
                "t_hold_end": round(self.t_hold_end, 4), "t_out": round(self.t_out, 4),
                "entrance": self.entrance, "exit": self.exit, "runs_to_end": self.runs_to_end}


def _ramp(sec: Optional[float], default: float) -> float:
    v = default if sec is None else float(sec)
    return min(rc.RAMP_SEC[1], max(rc.RAMP_SEC[0], v))


def timing(el: rc.Element, *, duration: float, entrance: str) -> Optional[Timing]:
    """A pattern's time on a scene `duration` seconds long, `entrance` being
    the entrance the build resolved (the drawn place's aimed_animation, or
    the one picked). None: it starts at or after the scene's end (it does
    not show). lib/rebrandTiming.ts mirrors this rule for rule; a shared
    fixture (tests/fixtures/rebrand_timing.json) holds the two together.

      1. The span runs from its start (0 for clip_start) to its end (the
         scene's end for clip_end), both clamped to the scene; a span a
         shorter scene cut below SPAN_MIN_SEC keeps its end and starts
         earlier.
      2. The entrance lasts animation.duration_sec (0 for "none"), the exit
         animation.exit_sec or EXIT_SEC (0 for "none"), each within
         RAMP_SEC; when both are longer than the span they shrink in
         proportion, each keeping RAMP_SQUEEZE_MIN.
      3. An "auto" exit is "none" when the span runs to the scene's end (the
         cut takes it away, as always), else the entrance played backwards
         (pattern_library.EXIT_OF_ENTRANCE).
      4. A pattern with no time set is today's: in over 1 s from 0, held to
         the end, no exit."""
    d = max(0.0, float(duration))
    start, end = rc.pattern_span(el)
    if start >= d:
        return None
    s = max(0.0, start)
    e = d if end is None else min(max(float(end), 0.0), d)
    if e <= s:
        return None
    if e - s < rc.SPAN_MIN_SEC:
        s = max(0.0, e - rc.SPAN_MIN_SEC)
    runs_to_end = e >= d - 1e-6
    ex = el.animation.exit or "auto"
    if ex == "auto":
        ex = "none" if runs_to_end else pattern_library.EXIT_OF_ENTRANCE.get(entrance, "fade")
    a = 0.0 if entrance == "none" else _ramp(el.animation.duration_sec, 1.0)
    b = 0.0 if ex == "none" else _ramp(el.animation.exit_sec, rc.EXIT_SEC)
    span = e - s
    if a + b > span:
        if a > 0.0 and b > 0.0:
            least = min(RAMP_SQUEEZE_MIN, span / 2.0)
            a2 = span * a / (a + b)
            b2 = span - a2
            if a2 < least:
                a2, b2 = least, span - least
            elif b2 < least:
                a2, b2 = span - least, least
            a, b = a2, b2
        else:
            a, b = min(a, span), min(b, span)
    return Timing(t_in=s, t_full=s + a, t_hold_end=e - b, t_out=e, entrance=entrance, exit=ex,
                  runs_to_end=runs_to_end)


def strip_resolved(recipe: rc.Recipe) -> rc.Recipe:
    """The recipe with no frozen pattern (props.resolved) on any pattern
    element: a preview and a run never read one a client sent — only
    Generate writes it, from the preview the user saw (as for the icons'
    icon.strip_resolved)."""
    if not any(e.kind == "pattern" and "resolved" in e.props for e in recipe.elements):
        return recipe
    return recipe.model_copy(update={"elements": [
        e.model_copy(update={"props": {k: v for k, v in e.props.items() if k != "resolved"}})
        if e.kind == "pattern" and "resolved" in e.props else e for e in recipe.elements]})


def materialise(recipe: rc.Recipe, frozen: dict[str, dict]) -> rc.Recipe:
    """The recipe as Generate stores it for the in-house engine: each
    pattern in `frozen` ({element id: resolved}) carries props.resolved —
    exactly as the preview the user saw drew it — so the run draws it there
    and in that colour, with no planner and no colour dig (from_resolved).
    A new recipe; nothing else in it changes."""
    if not frozen:
        return recipe
    return rc.normalize(recipe.model_copy(update={"elements": [
        e.model_copy(update={"props": {**e.props, "resolved": frozen[e.id]}})
        if e.kind == "pattern" and e.id in frozen else e for e in recipe.elements]}))


def frozen(el: rc.Element) -> Optional[dict]:
    """A pattern element's frozen place (props.resolved), or None."""
    res = el.props.get("resolved") if el.kind == "pattern" else None
    return res if isinstance(res, dict) else None


_FITTED: dict[tuple[str, str], tuple[pattern_ref.Layout, ...]] = {}


def _fitted(entry: pattern_library.PatternEntry, place: str, mask: Optional[Path]) -> tuple:
    """The layouts worked out from the pattern's art for one side (cached per
    pattern: measuring a mask reads the whole sheet)."""
    key = (entry.slug, place)
    if key not in _FITTED:
        profile = pattern_library.measure_art(pattern_ref._mask_for(entry.slug, mask))
        _FITTED[key] = pattern_library.fit_profile(profile, [place], both_corners=True)
    return _FITTED[key]


def candidates(entry: pattern_library.PatternEntry, anchor: str,
               mask: Optional[Path]) -> tuple[pattern_ref.Layout, ...]:
    """The places the pattern may take for this anchor (the first is the
    default; more than one is chosen between by the planner)."""
    if anchor == "auto" or anchor not in EDGE_PLACEMENT:
        return entry.candidates
    place = EDGE_PLACEMENT[anchor]
    own = tuple(c for c in entry.candidates if c.placement == place)
    if own:
        return own
    if place in ("left", "right"):
        # The same strip on the other side: the renderer mirrors a right
        # strip so the art's natural end still faces the speaker.
        side = next((c for c in entry.candidates if c.placement in ("left", "right")), None)
        if side is not None:
            return (dataclasses.replace(side, placement=place),)
    return _fitted(entry, place, mask) or entry.candidates


def alternates(entry: pattern_library.PatternEntry,
               cands: Sequence[pattern_ref.Layout]) -> tuple[pattern_ref.Layout, ...]:
    """Where an auto-placed pattern may go when every place of its own is
    taken: a strip the same strip on the other side; a band the other
    corner, then the other edge. A full background has none. Never one of
    `cands` itself."""
    out: list[pattern_ref.Layout] = []
    for c in cands:
        if c.placement in ("left", "right"):
            alts = [dataclasses.replace(c, placement="right" if c.placement == "left" else "left")]
        elif c.placement in ("top", "bottom"):
            edge = "bottom" if c.placement == "top" else "top"
            corner = "right" if c.anchor == "left" else "left"
            alts = [dataclasses.replace(c, anchor=corner), dataclasses.replace(c, placement=edge),
                    dataclasses.replace(c, placement=edge, anchor=corner)]
        else:
            alts = []
        for a in alts:
            if a not in cands and a not in out:
                out.append(a)
    return tuple(out)


def is_full(entry: pattern_library.PatternEntry, el: rc.Element) -> bool:
    """Whether the element fills the whole frame: anchored "full", or auto
    on a pattern whose every place is the whole background (blueprint,
    grid, a full-only upload). A full pattern goes alone."""
    anchor = el.placement.anchor
    if anchor == "full":
        return True
    return anchor == "auto" and all(c.placement == "full" for c in entry.candidates)


def _pinned(entry: pattern_library.PatternEntry, cands: Sequence[pattern_ref.Layout],
            pin: dict) -> Optional[pattern_ref.Layout]:
    """A pinned place (props.layout, a Layout.public()), honoured only while
    it is still one of the anchor's places or their mirror — a pin left over
    from another pattern or another side is not."""
    try:
        lay = pattern_ref.Layout(**pin)
    except (TypeError, ValueError):
        return None
    # The library's own object, not the one read from JSON (2 == 2.0 there,
    # but the renderer should see the types it always sees).
    return next((c for c in (*cands, *alternates(entry, cands)) if c == lay), None)


def _named(taken: dict[str, str], slot_: str) -> str:
    return taken.get(slot_) or "another pattern"


async def _plan(job: PatternJob, entry: pattern_library.PatternEntry,
                cands: tuple[pattern_ref.Layout, ...], mask: Optional[Path],
                colour: str, *, seed: Optional[str] = None, occupied: Any = None,
                taken: Optional[Iterable[str]] = None) -> tuple[pattern_ref.Layout, dict]:
    """(the place an auto-placed pattern takes in this run, the planner's
    report). Planned on the first frame of the clip Omni edits first, ONCE
    per run, so the pattern cannot move between chunks or scenes, and the
    brief is told the place Omni is shown.

    Seeded by the run (the experiment, or the recipe's own seed so a
    preview and every retry draw the same place) and the pattern. Never
    raises — no frame, or any failure, is the first candidate.

    `occupied` / `taken` (several patterns): the ink and slots of the
    patterns placed before this one; passed on only when given, so a single
    pattern plans exactly as it always has."""
    seed = seed or f"{job.seed}:{entry.slug}"
    frame = await job.plan_frame()
    if frame is None:
        return cands[0], {"mode": "auto", "seed": seed, "reason": "no frame",
                          "picked": {"index": 0, "label": pattern_ref.placement_label(cands[0]),
                                     "layout": cands[0].public()},
                          "pool": [], "candidates": []}
    try:
        person = await (job.person_mask or person_matte.person_mask)(frame)
    except Exception as e:  # noqa: BLE001 — scored without a speaker mask
        log.warning("[rebrand] experiment #%d: no speaker mask for placing the pattern (%s)",
                    job.exp_id, str(e)[:200])
        person = None
    several: dict[str, Any] = {}
    if occupied is not None:
        several["occupied"] = occupied
    if taken is not None:
        several["taken"] = sorted(taken)
    return await _thread(
        pattern_planner.choose, cands, frame, person, seed=seed, slug=entry.slug,
        mask=mask, colour=colour, work_dir=job.out_dir / "plan", **several,
    )


async def build(el: Optional[rc.Element], colours: rc.Colours, job: PatternJob, *,
                occupied: Any = None, taken: Optional[dict[str, str]] = None) -> PatternResult:
    """The run's pattern, coloured (rebrand_pattern_ref.build) from the
    intro's RAW source: a still card is measured directly, a video is
    scanned back past its fade-out, so the ground is the card's colour and
    not a black frame's.

    Failure degrades, never kills: Omni then runs with no reference image
    and no pattern clause (still no logo), and the reason is kept for the
    evidence.

    An AUTO-placed pattern (several candidate places) has its colours dug
    first, the planner scores every place in that colour on the clip's frame
    and speaker, and the pattern is built in the place it drew. A fixed
    pattern is built in its one place, its entrance aimed the same way.

    `occupied` (float HxW ink) and `taken` ({slot: name}) come from
    build_all for every pattern after the first: its places whose slot is
    taken are out; an auto one with none left moves to an alternate (and
    says so); an explicit place is kept and warned about. None for both is
    the single-pattern path."""
    if el is None:
        await job.step("Pattern: off", status="done", detail="turned off for this run")
        return PatternResult()
    out = PatternResult(element_id=el.id)
    try:
        slug = el.props.get("slug") or pattern_library.DEFAULT_SLUG
        entry = await pattern_library.get(slug)
        if entry is None:
            await job.step(f"Pattern {slug} not found", status="failed",
                           detail=f"using {pattern_library.DEFAULT_SLUG} instead")
            entry = pattern_library.BUILTINS[pattern_library.DEFAULT_SLUG]
            out.substituted = True
        picked = None if el.animation.entrance == "auto" else el.animation.entrance
        animation = picked or entry.default_animation
        mask = await _thread(entry.mask_path)
        cands = await _thread(candidates, entry, el.placement.anchor, mask)
        own = cands
        pinned = False
        hand = rc.hand_of(el)
        pin = el.props.get("layout")
        if isinstance(pin, dict):
            lay = await _thread(_pinned, entry, cands, pin)
            if lay is not None:
                cands, pinned = (lay,), True
            else:
                # Shown to the owner word for word (the preview's warnings).
                # A move by hand was made from that spot: it goes with it.
                await job.step(f"{entry.name}'s saved spot doesn't fit here, so it was placed again"
                               + ("" if hand.identity else " (your move was reset)"), status="failed")
        # Moved by hand (from its pinned place): it claims no slot — the
        # patterns after it keep off its ink only (a left strip dragged to
        # the right must not keep a later one off the left side).
        by_hand = pinned and not hand.identity
        if taken is not None and not by_hand:
            free = tuple(c for c in cands if slot(c) not in taken)
            if not free and el.placement.anchor == "auto" and not pinned:
                free = tuple(a for a in alternates(entry, cands) if slot(a) not in taken)
                if free:
                    out.moved = "own"                     # worded once the place is known
            if not free:
                free = cands
                out.warnings.append(
                    f"{entry.name} shares the {pattern_ref.placement_label(cands[0])} with "
                    f"{_named(taken, slot(cands[0]))} — try Another spot")
            cands = free
        override = colour_override(colours, el)
        dug = job.colours
        if dug is None and (len(cands) > 1 or override):
            dug = await _thread(pattern_ref.dig_colours, job.intro, job.out_dir,
                                is_still=job.intro_is_still)
        if dug is not None and override:
            dug = dataclasses.replace(dug, colour=override)
        if len(cands) > 1:
            layout, report = await _plan(job, entry, cands, mask, dug.colour,
                                         seed=seed_for(el, entry.slug, job.seed),
                                         occupied=occupied,
                                         taken=list(taken) if taken is not None else None)
            out.report = report
            await job.step(
                f"Pattern placed: {pattern_ref.placement_label(layout)} (auto)",
                status="failed" if report.get("reason") == "error" else "done",
                detail=pattern_planner.summary(report),
            )
        else:
            layout = cands[0]
        if out.moved == "own":
            was = pattern_ref.placement_label(own[0]) if len(own) == 1 else "its own places"
            out.moved = (f"The {was} was taken, so it moved to the "
                         f"{pattern_ref.placement_label(layout)}." if len(own) == 1 else
                         f"Its own places were taken, so it moved to the "
                         f"{pattern_ref.placement_label(layout)}.")
        out.slot = hand_slot(el) if by_hand else slot(layout)
        # The entrance starts where the place starts (a right strip sweeps in
        # from the right) — unless the recipe picked one itself. Every
        # built-in's own place is a left strip or the whole background, so
        # theirs never change.
        if not picked:
            animation = pattern_library.aimed_animation(entry.default_animation, layout)
        kwargs = dict(slug=entry.slug, layout=layout, mask=mask, animation=animation,
                      name=entry.name, source_file=entry.source_file)
        if dug is not None:
            kwargs["colours"] = dug
        out.ref = await _thread(
            pattern_ref.build, job.intro, job.out_dir,
            aspect=job.aspect, is_still=job.intro_is_still, **kwargs,
        )
        if by_hand:
            out.ref.hand = hand
    except Exception as e:  # noqa: BLE001 — a missing pattern must never cost the run
        log.exception("[rebrand] experiment #%d: pattern ref failed — Omni runs without one", job.exp_id)
        out.ref = None
        out.error = (str(e) or type(e).__name__)[:300]
        await job.step("Pattern: none", status="failed",
                       detail=f"Omni runs with no reference image ({out.error})")
        return out
    ref = out.ref
    if override:
        where_from = ("the 42 brand colour" if override == pattern_ref.BRAND_GROUND
                      else "the colour picked for this run")
    elif ref.fell_back:
        where_from = "42 brand colours (no usable intro colour)"
    elif ref.colour == ref.ground:
        where_from = "the intro's background colour"
    else:
        where_from = f"the intro's own colour (its background {ref.ground} is neutral)"
    await job.step(
        f"Pattern: {ref.slug} ({ref.source}) — {ref.colour}, transparent background",
        status="done",
        detail=where_from + f" · {ref.where} · entrance: "
               + pattern_library.ANIMATIONS.get(ref.animation, ("",))[0].lower(),
    )
    return out


# ---------------------------------------------------------------------------
# Several patterns
# ---------------------------------------------------------------------------

def _ink(ref: pattern_ref.PatternRef, width: int, height: int):
    """The pattern's own alpha in its place — moved when it was by hand
    (placed_layer) — float HxW 0..1 (zeros when it has no ink at this
    size)."""
    import numpy as np

    layer = pattern_ref.placed_layer(ref, width, height)
    if layer is None:
        return np.zeros((height, width), dtype="float32")
    return np.asarray(layer.getchannel("A"), dtype="float32") / 255.0


# Named in a warning when a pattern added on top of a finished version lands
# on that version's own graphics.
ALREADY_THERE = "what's already there"


def _as_frame(ink: Any, width: int, height: int):
    """Ink (float, any size, 0..1) at the plan frame's size."""
    import numpy as np

    a = np.clip(np.asarray(ink, dtype="float32"), 0.0, 1.0)
    return a if a.shape == (height, width) else pattern_planner._resize(a, width, height)


def _frame_size(frame: Optional[Path], aspect: str) -> tuple[int, int]:
    if frame is not None:
        try:
            from PIL import Image

            with Image.open(frame) as im:
                return im.size
        except OSError:
            pass
    return pattern_ref.canvas_for(aspect)


def _keep(frame: Optional[Path], person: Any):
    """Where a pattern can be seen on the plan frame, at the planner's grid
    resolution (pattern_planner.keep_grid), or None with no frame or no
    speaker mask."""
    if frame is None or person is None:
        return None
    rgb = pattern_planner._load_rgb(frame)
    h, w = rgb.shape[:2]
    return pattern_planner.keep_grid(rgb, pattern_planner._load_person(person, w, h))


def _overlaps(ink, occupied, footprints: list[tuple[str, Any]],
              keep: Any = None) -> tuple[float, Optional[str]]:
    """(share of `ink` on the earlier patterns, the one it overlaps most) —
    measured as the planner measures it: the VISIBLE ink (outside the
    speaker: `keep`) against the grown footprint of the earlier ones'
    `occupied` ink, at grid resolution. So a place the planner let through
    is not then warned about for ink hidden behind the speaker."""
    if not footprints:
        return 0.0, None
    h, w = footprints[0][1].shape
    small = pattern_planner._resize(ink, w, h)
    if keep is not None and getattr(keep, "shape", None) == small.shape:
        small = small * keep
    worst, name = 0.0, None
    for n, fp in footprints:
        share = pattern_planner.overlap_share(small, fp)
        if share > worst:
            worst, name = share, n
    return pattern_planner.overlap_share(small, pattern_planner.footprint(occupied, w, h)), name


def _measure(ref: pattern_ref.PatternRef, width: int, height: int, occupied: Any,
             footprints: list[tuple[str, Any]], keep: Any) -> tuple[float, Optional[str], Any, Any]:
    """One placed pattern against the ones before it that show with it
    (synchronous, run off the event loop): (overlap, the one it overlaps
    most, its own ink, its own footprint)."""
    ink = _ink(ref, width, height)
    share, other = _overlaps(ink, occupied, footprints, keep)
    grid_w, grid_h = max(1, width // pattern_planner.GRID), max(1, height // pattern_planner.GRID)
    return share, other, ink, pattern_planner.footprint(ink, grid_w, grid_h)


@dataclass
class _Placed:
    """A pattern build_all has placed, as the ones after it keep off it:
    while their spans overlap (rc.spans_overlap) — patterns that never show
    together may share a place."""
    span: tuple[float, Optional[float]]
    ink: Any
    footprint: Any
    slot: str
    name: str


def _occupancy(there: Any, placed: Sequence[_Placed], span: tuple[float, Optional[float]]) -> Any:
    """The ink a pattern showing over `span` keeps off: what was there before
    (a version added to: always) and every earlier pattern that shows at the
    same time. Synchronous."""
    import numpy as np

    occupied = there
    for p in placed:
        if rc.spans_overlap(p.span, span):
            occupied = np.maximum(occupied, p.ink)
    return occupied


async def build_all(els: Sequence[Optional[rc.Element]], colours: rc.Colours,
                    job: PatternJob, *, occupied: Any = None,
                    taken: Optional[dict[str, str]] = None) -> list[PatternResult]:
    """Every pattern of the recipe, in list order (one result each).

    One element (or none) is exactly build(). Several: the intro's colours
    are dug once and shared; each element renders into its own folder
    (`out_dir/<element id>`; the first, "pattern", keeps `out_dir`) so the
    same pattern twice does not overwrite itself; each is placed avoiding
    the slots and the ink of the ones before it (planning order = list order
    = draw order, so appending never moves those already placed).

    Each one's overlap with those before it is measured as the planner
    measures it — on its visible ink, with the speaker's mask on the plan
    frame (job.person_mask, else the LOCAL matte: a warning never pays for
    a mask; the planner's own mask is on disk by then and read back).

    `occupied` (float HxW ink) / `taken` ({slot: name}): what is already in
    the frame before the first of them — a finished version the patterns
    are added on top of ("Add on top"). Every pattern, the first included,
    then keeps off it. None for both: nothing is there yet."""
    els = [e for e in els if e is not None]
    based = occupied is not None or taken is not None
    if len(els) <= 1 and not (based and els):
        return [await build(els[0] if els else None, colours, job)]
    import numpy as np

    if job.colours is None:
        job = dataclasses.replace(job, colours=await _thread(
            pattern_ref.dig_colours, job.intro, job.out_dir, is_still=job.intro_is_still))
    memo: dict[str, Any] = {}

    async def plan_frame() -> Optional[Path]:
        if "frame" not in memo:
            memo["frame"] = await job.plan_frame()
        return memo["frame"]

    async def keep() -> Any:
        if "keep" not in memo:
            frame = await plan_frame()
            person = None
            if frame is not None:
                try:
                    person = await (job.person_mask or person_matte.local_mask)(frame)
                except Exception as e:  # noqa: BLE001 — measured on all of its ink
                    log.warning("[rebrand] experiment #%d: no speaker mask for the overlap (%s)",
                                job.exp_id, str(e)[:200])
            memo["keep"] = await _thread(_keep, frame, person)
        return memo["keep"]

    width, height = await _thread(_frame_size, await plan_frame(), job.aspect)
    already = dict(taken or {})
    there = np.zeros((height, width), dtype="float32")
    before: list[tuple[str, Any]] = []
    if occupied is not None:
        there = await _thread(_as_frame, occupied, width, height)
        if there.max() > 0:
            grid_w, grid_h = max(1, width // pattern_planner.GRID), max(1, height // pattern_planner.GRID)
            before.append((ALREADY_THERE, await _thread(pattern_planner.footprint, there, grid_w, grid_h)))
    # Occupancy follows time: each pattern keeps off the earlier ones that
    # show while it does (its span: rc.pattern_span). With every span the
    # whole scene (every pattern made before spans) each keeps off all of
    # the earlier ones, exactly as it always has.
    placed: list[_Placed] = []
    results: list[PatternResult] = []
    for el in els:
        entry = await pattern_library.get(el.props.get("slug") or pattern_library.DEFAULT_SLUG)
        name = entry.name if entry else (el.props.get("slug") or el.id)

        async def step(n: str, status: str = "running", detail: str = "", _name: str = name) -> None:
            # A step that already names its pattern is not named twice.
            await job.step(n if n.startswith(_name) else f"{_name} — {n}", status=status, detail=detail)

        sub = dataclasses.replace(
            job, out_dir=job.out_dir if el.id == "pattern" else job.out_dir / el.id,
            step=step, plan_frame=plan_frame)
        span = rc.pattern_span(el)
        together = [p for p in placed if rc.spans_overlap(p.span, span)]
        occupied = await _thread(_occupancy, there, together, span) if together else there
        taken = dict(already)
        for p in together:
            taken[p.slot] = p.name
        footprints = [*before, *((p.name, p.footprint) for p in together)]
        first = not footprints and not based
        res = await build(el, colours, sub,
                          occupied=None if first else occupied,
                          taken=None if first else dict(taken))
        results.append(res)
        if res.ref is None:
            continue
        seen = await keep() if footprints else None
        share, other, ink, own = await _thread(_measure, res.ref, width, height, occupied, footprints, seen)
        res.overlap = round(share, 3)
        if res.overlap > pattern_planner.OVERLAP_MAX and not res.warnings:
            res.warnings.append(f"{name} overlaps {other or 'another pattern'} — try Another spot")
        for w in res.warnings:
            await job.step(w, status="failed")
        placed.append(_Placed(span=span, ink=ink, footprint=own, slot=res.slot or slot(res.ref.layout),
                              name=name))
    return results


async def from_resolved(el: rc.Element, job: PatternJob) -> PatternResult:
    """A pattern exactly as Generate froze it (props.resolved — the
    in-house engine): its frozen place, drawn in its frozen colour — no
    planner, no colour dig, no speaker mask, so the run cannot draw it
    anywhere but where the preview did. Its entrance is worked out as
    build() works it out (the picked one, else the pattern's own aimed at
    its place). A slug the library no longer has draws the default in its
    place (substituted); failure degrades, as build() does."""
    res = frozen(el) or {}
    out = PatternResult(element_id=el.id, report={"mode": "frozen"})
    folder = job.out_dir if el.id == "pattern" else job.out_dir / el.id
    try:
        slug = el.props.get("slug") or pattern_library.DEFAULT_SLUG
        entry = await pattern_library.get(slug)
        if entry is None:
            await job.step(f"Pattern {slug} not found", status="failed",
                           detail=f"using {pattern_library.DEFAULT_SLUG} instead")
            entry = pattern_library.BUILTINS[pattern_library.DEFAULT_SLUG]
            out.substituted = True
        mask = await _thread(entry.mask_path)
        cands = await _thread(candidates, entry, el.placement.anchor, mask)
        # The library's own object when the place is still one of its own
        # (the renderer sees the types it always sees), else the frozen one.
        layout = await _thread(_pinned, entry, cands, res.get("layout") or {})
        if layout is None:
            layout = pattern_ref.Layout(**res["layout"])
        picked = None if el.animation.entrance == "auto" else el.animation.entrance
        animation = picked or pattern_library.aimed_animation(entry.default_animation, layout)
        colour = str(res["colour"]).lower()
        dug = pattern_ref.Colours(ground=str(res.get("ground") or colour).lower(),
                                  ink=str(res.get("ink") or pattern_ref.BRAND_INK).lower(),
                                  fell_back=False, frame=None, colour=colour)
        out.ref = await _thread(
            pattern_ref.build, job.intro, folder, aspect=job.aspect, is_still=job.intro_is_still,
            slug=entry.slug, layout=layout, mask=mask, animation=animation, name=entry.name,
            source_file=entry.source_file, colours=dug,
        )
        # Its hand move: the recipe's own (placement), as the preview drew
        # it — the renderer clamps a full-screen one the same way here.
        hand = rc.hand_of(el)
        out.ref.hand = hand
        out.slot = slot(layout) if hand.identity else hand_slot(el)
    except Exception as e:  # noqa: BLE001 — a missing pattern must never cost the run
        log.exception("[rebrand] experiment #%d: frozen pattern %s failed", job.exp_id, el.id)
        out.ref = None
        out.error = (str(e) or type(e).__name__)[:300]
        await job.step("Pattern: none", status="failed", detail=out.error)
        return out
    await job.step(f"Pattern: {out.ref.slug} — {out.ref.colour}, as previewed", status="done",
                   detail=f"{out.ref.where} · entrance: {out.ref.animation.replace('_', ' ')}")
    return out


# ---------------------------------------------------------------------------
# What a recipe may hold (library-aware, so outside pydantic)
# ---------------------------------------------------------------------------

LIBRARY_UNREACHABLE = "Couldn't reach the pattern library — try again"


async def _lookup(slug: str) -> Optional[pattern_library.PatternEntry]:
    """The library's entry, or None when it has no such pattern. A library
    that cannot be read right now is a 503 (the studio offers Try again),
    never "there is no such pattern" (a refusal the owner cannot retry)."""
    from fastapi import HTTPException

    try:
        return await pattern_library.lookup(slug)
    except pattern_library.LibraryUnavailable as e:
        log.warning("[rebrand] pattern library unreachable checking %s: %s", slug, e)
        raise HTTPException(503, LIBRARY_UNREACHABLE) from e


def _clock(sec: float) -> str:
    """0:01.5 (0:06 on a whole second) — a time as the studio writes it."""
    tenths = int(round(max(0.0, float(sec)) * 10))
    m, rest = divmod(tenths, 600)
    s, d = divmod(rest, 10)
    return f"{m}:{s:02d}" + (f".{d}" if d else "")


def alone_message(name: str, span: tuple[float, Optional[float]]) -> str:
    """A full-screen pattern beside another that shows at the same time, in
    the owner's words: over the whole scene as always, or while it shows."""
    start, end = span
    if start <= 0.0 and end is None:
        return f"{name} fills the whole frame, so it goes alone"
    until = "the end" if end is None else _clock(end)
    return f"{name} fills the whole frame from {_clock(start)} to {until}, so nothing else can show then"


async def check(recipe: rc.Recipe, chain: Sequence[rc.Element] = ()) -> list[dict]:
    """What makes the recipe's patterns impossible, one error per element
    ({element_id, field, message}; the router answers 422), in the owner's
    words (the studio shows them as they are):

      a pattern that is not in the library;
      a full-screen pattern with any other that shows at the same time (it
      fills the frame: it goes alone WHILE it shows — spans that only touch
      are fine, so a short full-screen transition can lead into a strip);
      more than rc.PATTERN_MAX patterns — counting `chain`, the patterns a
      version being built on already carries.

    Raises HTTPException(503) when the library cannot be read right now."""
    errors: list[dict] = []
    els = rc.pattern_elements(recipe)
    # (name, full, span) of the chain and the patterns so far.
    seen: list[tuple[str, bool, tuple[float, Optional[float]]]] = []
    for el in chain:
        entry = await _lookup(el.props.get("slug") or pattern_library.DEFAULT_SLUG)
        seen.append(((entry.name if entry else el.id), bool(entry and is_full(entry, el)),
                     rc.pattern_span(el)))
    for el in els:
        slug = el.props.get("slug") or pattern_library.DEFAULT_SLUG
        entry = await _lookup(slug)
        if entry is None:
            errors.append({"element_id": el.id, "field": "props.slug",
                           "message": "That pattern isn't in the library any more — swap or remove it."})
            continue
        full = is_full(entry, el)
        span = rc.pattern_span(el)
        together = [(n, f, sp) for n, f, sp in seen if rc.spans_overlap(sp, span)]
        earlier_full = next(((n, sp) for n, f, sp in together if f), None)
        if full and together:
            errors.append({"element_id": el.id, "field": "placement.anchor",
                           "message": alone_message(entry.name, span)})
        elif earlier_full:
            errors.append({"element_id": el.id, "field": "placement.anchor",
                           "message": alone_message(*earlier_full)})
        if len(seen) >= rc.PATTERN_MAX:
            errors.append({"element_id": el.id, "field": "kind", "message": rc.pattern_cap_message()})
        seen.append((entry.name, full, span))
    return errors
