"""Patterns on the timeline (I14): each pattern shows over a span, comes in at
its start and goes out at its end.

  the rule       a full-screen pattern goes alone WHILE it shows: a Blueprint
                 transition and then Structure pass, a pair that shows
                 together is refused (pattern_el.check)
  occupancy      a pattern keeps off the earlier ones that show with it — two
                 that never show together may share a place; with every span
                 the whole scene run 815's places are exactly what they were
                 before spans (tests/fixtures/rebrand_815_places.json,
                 recorded before the change)
  the preview    each pattern says when it shows (timing); one that starts
                 after the scene ends is said; the design still shows what
                 shows at its moment, and its layers are those (G5)
  the exits      every exit drawn: all there at its start, gone at its end,
                 each wipe going back towards its own edge (the tables)
  the icons      keep clear of each pattern's entrance AND its exit (C6),
                 the times read from patterns_for_icons (C5) as from the
                 recipe; the 42 default keeps its one zone (so every plan
                 key); an icon is judged against the patterns on screen with
                 it, not a transition long gone
  the library    Blueprint is a 1.5 s transition (0.3 s in, 0.5 s out); the
                 catalogue serves the exits

The pattern kit's masks are real (skipped when absent), MODNet is faked by
the green (test_rebrand_house_studio), every paid client raises, and nothing
touches the database, a bucket or a server."""
from __future__ import annotations

import asyncio
import dataclasses
import json
import shutil
import subprocess
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from app.routers import rebrand_harness as router
from app.services import pattern_library as lib
from app.services import pattern_planner
from app.services import rebrand_pattern_ref as ref
from app.services.rebrand_harness import house
from app.services.rebrand_harness import plate as pl
from app.services.rebrand_harness import recipe as rc
from app.services.rebrand_harness import studio
from app.services.rebrand_harness.elements import icon as I
from app.services.rebrand_harness.elements import pattern as pattern_el

from test_rebrand_house_studio import (FPS, N, W as SW, H as SH, _decoded, _png, paid,  # noqa: F401
                                       scene)

cv2 = pytest.importorskip("cv2")

FIXTURES = Path(__file__).resolve().parent / "fixtures"
REPO = Path(__file__).resolve().parents[2]
HAVE_MASKS = all(ref._mask_path(s).exists() for s in lib.BUILTINS)
needs_masks = pytest.mark.skipif(not HAVE_MASKS, reason="the pattern kit's masks are not here")
DUG = ref.Colours(ground="#7f2bf2", ink="#ffffff", fell_back=False, frame=None, colour="#7f2bf2")
STUDIO_815 = (Path(__file__).resolve().parents[1] / "data" / "yas_producer" / "talking-head-v2" / "rebrand"
              / "_studio" / "815" / "scene_0_84f81b9967")
T = lib.BLUEPRINT_TRANSITION


def _el(eid: str, slug: str, *, start: float | None = None, end: float | None = None, anchor: str = "auto",
        **animation) -> rc.Element:
    a: dict = {"entrance": "auto", **animation}
    if start is not None:
        a["start"] = {"mode": "at_sec", "sec": start}
    if end is not None:
        a["end"] = {"mode": "at_sec", "sec": end}
    return rc.Element(id=eid, kind="pattern", placement={"anchor": anchor}, animation=a,
                      props={"slug": slug, "layout": None, "colour": "auto"})


def _house(*els: rc.Element, seed: str = "studio:1:scene_0") -> rc.Recipe:
    return rc.normalize(rc.Recipe(engine="house", target="scene_0", seed=seed, elements=list(els)))


def _transition() -> rc.Element:
    """Blueprint as the studio adds it: a 1.5 s full-screen transition."""
    return _el("pattern", "blueprint", end=T.sec, duration_sec=T.in_sec, exit_sec=T.out_sec)


@pytest.fixture
def library(monkeypatch):
    async def get(slug):
        return lib.BUILTINS.get(slug)

    monkeypatch.setattr(lib, "get", get)
    monkeypatch.setattr(lib, "lookup", get)


# ---------------------------------------------------------------------------
# A full-screen pattern goes alone while it shows
# ---------------------------------------------------------------------------

def test_a_blueprint_transition_then_structure_passes_and_an_overlap_is_refused(library):
    ok = _house(_transition(), _el("pattern-2", "structure", start=T.sec))
    assert asyncio.run(pattern_el.check(ok)) == [], "they only touch at 1.5 s"
    both = _house(_el("pattern", "blueprint", end=2.0), _el("pattern-2", "structure", start=1.5))
    assert asyncio.run(pattern_el.check(both)) == [{
        "element_id": "pattern-2", "field": "placement.anchor",
        "message": "Blueprint fills the whole frame from 0:00 to 0:02, so nothing else can show then"}]
    # A strip first, then a full screen over part of it: the full screen is refused.
    late = _house(_el("pattern", "structure"), _el("pattern-2", "grid", start=6.0))
    [e] = asyncio.run(pattern_el.check(late))
    assert e["element_id"] == "pattern-2" and e["message"].startswith("Grid fills the whole frame from 0:06")
    # Every recipe before spans says what it always said.
    old = _house(_el("pattern", "blueprint"), _el("pattern-2", "structure"))
    assert asyncio.run(pattern_el.check(old))[0]["message"] == "Blueprint fills the whole frame, so it goes alone"


def test_the_transition_is_a_valid_recipe_with_its_own_time():
    r = _house(_transition(), _el("pattern-2", "structure", start=T.sec))
    b, s = rc.pattern_elements(r)
    tb = pattern_el.timing(b, duration=8.0, entrance="fade")
    assert tb.public() == {"t_in": 0.0, "t_full": 0.3, "t_hold_end": 1.0, "t_out": 1.5, "entrance": "fade",
                           "exit": "fade", "runs_to_end": False}, "0.3 s in, held, 0.5 s out by 1.5 s"
    ts = pattern_el.timing(s, duration=8.0, entrance="left_to_right")
    assert (ts.t_in, ts.t_full, ts.t_out, ts.exit, ts.runs_to_end) == (1.5, 2.5, 8.0, "none", True)
    # The recipe keeps only what was set (hash-neutral for everything else).
    dumped = b.model_dump(mode="json")["animation"]
    assert dumped["end"] == {"mode": "at_sec", "sec": 1.5} and dumped["exit_sec"] == 0.5 and "exit" not in dumped


# ---------------------------------------------------------------------------
# Occupancy follows time
# ---------------------------------------------------------------------------

def _job(tmp_path: Path, frame: Path, mask) -> pattern_el.PatternJob:
    async def plan_frame():
        return frame

    async def person(_frame):
        return mask

    async def step(*_a, **_k):
        return None

    return pattern_el.PatternJob(exp_id=0, aspect="9:16", out_dir=tmp_path / "pattern", intro=None,
                                 intro_is_still=False, seed="studio:815:scene_0", plan_frame=plan_frame, step=step,
                                 colours=DUG, person_mask=person)


@needs_masks
@pytest.mark.skipif(not (STUDIO_815 / "c0_first.png").exists(), reason="run 815's frame is not on this machine")
def test_default_spans_leave_run_815s_places_exactly_as_they_were(tmp_path, library, paid):
    """Run 815's real frame and speaker mask (read only), the planner for
    real: every combination places exactly as before time-aware occupancy
    (recorded before the change: scratchpad/build/i14/baseline_815.py)."""
    want = json.loads((FIXTURES / "rebrand_815_places.json").read_text(encoding="utf-8"))
    mask = np.asarray(Image.open(STUDIO_815 / "c0_first.person.png"), dtype=np.float32) / 255.0
    for name, slugs in want["combos"].items():
        els = [_el("pattern" if i == 0 else f"pattern-{i + 1}", s) for i, s in enumerate(slugs)]
        res = asyncio.run(pattern_el.build_all(els, rc.Colours(), _job(tmp_path / name, STUDIO_815 / "c0_first.png",
                                                                       mask)))
        got = [{"element_id": r.element_id, "layout": r.ref.layout.public() if r.ref else None, "slot": r.slot,
                "overlap": r.overlap, "moved": r.moved, "warnings": r.warnings} for r in res]
        assert got == want["places"][name], name
    assert paid == []


@needs_masks
def test_patterns_that_never_show_together_may_share_a_place(tmp_path, library, monkeypatch):
    """The planner is asked with the places and ink of only the earlier
    patterns that show at the same time; with none, it plans as a lone
    pattern does (no occupied, no taken)."""
    asked: list[dict] = []

    def choose(cands, frame, person, *, seed, slug, mask=None, colour="", work_dir=None, occupied=None,
               taken=None):
        asked.append({"seed": seed, "taken": taken, "occupied": occupied is not None,
                      "cands": [pattern_planner.slot(c) for c in cands]})
        return cands[0], {"mode": "auto", "reason": "one place", "seed": seed}

    monkeypatch.setattr(pattern_planner, "choose", choose)
    frame = tmp_path / "frame.png"
    Image.new("RGB", (720, 1280), (90, 90, 96)).save(frame)
    job = _job(tmp_path, frame, np.zeros((1280, 720), np.float32))
    els = [_el("pattern", "beads_row", end=3.0), _el("pattern-2", "beads_row", start=3.0),
           _el("pattern-3", "beads_row", start=2.0, end=4.0)]
    first, second, third = asyncio.run(pattern_el.build_all(els, rc.Colours(), job))
    assert asked[0]["taken"] is None and not asked[0]["occupied"]
    assert asked[1]["taken"] is None and not asked[1]["occupied"], "0-3 s and 3 s on never show together"
    assert second.slot == first.slot == "left", "so the second may take the very place of the first"
    assert second.overlap == 0.0 and second.warnings == []
    assert sorted(asked[2]["taken"]) == ["left"] and asked[2]["occupied"], "2-4 s shows with both"
    assert "left" not in asked[2]["cands"] and third.slot != "left"
    # Every span the whole scene: each keeps off every one before it, as always.
    asked.clear()
    whole = [_el("pattern", "beads_row"), _el("pattern-2", "beads_row"), _el("pattern-3", "beads_row")]
    asyncio.run(pattern_el.build_all(whole, rc.Colours(), _job(tmp_path / "whole", frame,
                                                                np.zeros((1280, 720), np.float32))))
    assert asked[0]["taken"] is None and len(asked[1]["taken"]) == 1 and len(asked[2]["taken"]) == 2


# ---------------------------------------------------------------------------
# The preview: when each pattern shows; the design still (G5)
# ---------------------------------------------------------------------------

@needs_masks
def test_the_preview_says_when_each_pattern_shows_and_the_still_shows_what_shows_then(scene, paid):
    assert studio.PREVIEW_VERSION == 7
    recipe = _house(_transition(), _el("pattern-2", "structure", start=T.sec))
    body = asyncio.run(studio.preview(1, "scene_0", recipe))
    blue, strip = body["patterns"]
    end = N / FPS
    assert blue["timing"] == {"t_in": 0.0, "t_full": 0.3, "t_hold_end": 1.0, "t_out": 1.5, "entrance": "fade",
                              "exit": "fade", "runs_to_end": False}
    assert strip["timing"]["t_in"] == 1.5 and strip["timing"]["t_full"] == 2.5
    assert strip["timing"]["t_out"] == pytest.approx(end, abs=1e-4) and strip["timing"]["exit"] == "none"
    h = body["house"]
    assert [ly["timing"] for ly in h["layers"]] == [blue["timing"], strip["timing"]], "the plate's own clock"
    # The design still is Structure's moment: Blueprint is long gone then.
    assert h["at"] == pytest.approx(2.5)
    assert [ly["element_id"] for ly in body["chunks"][0]["layers"]] == ["pattern-2"], "its layers: what it shows"
    stored = asyncio.run(studio.as_generated(1, "scene_0", recipe, None, None))
    hs = asyncio.run(studio.house_spec(1, "scene_0", stored, None))
    assert [ly.element_id for ly in hs.spec.layers] == ["pattern", "pattern-2"]
    still = _png(h["still_url"])
    want = pl.over_frame(_decoded(scene.ctx.target_seg.base, 0),
                         pl.plate_at(hs.spec, 0, bank=hs.bank, settled=True, at=house.still_moment(hs.spec)))
    assert np.array_equal(still, want), "Next is the run's own plate over frame 0"
    alone = dataclasses.replace(hs.spec, layers=tuple(ly for ly in hs.spec.layers if ly.element_id == "pattern-2"))
    strip_only = pl.over_frame(_decoded(scene.ctx.target_seg.base, 0),
                               pl.plate_at(alone, 0, bank=hs.bank, settled=True, at=2.5))
    assert np.array_equal(still, strip_only), "no Blueprint in it (it and Structure never show together)"
    # Early in the scene the plate is Blueprint alone, then nothing of it.
    early = pl.plate_at(hs.spec, 15, camera=hs.track, bank=hs.bank)          # 0.5 s: Blueprint held
    later = pl.plate_at(hs.spec, 105, camera=hs.track, bank=hs.bank)         # 3.5 s: Structure held
    blueprint_only = dataclasses.replace(hs.spec, layers=hs.spec.layers[:1])
    assert np.array_equal(early, pl.plate_at(blueprint_only, 15, camera=hs.track, bank=hs.bank))
    assert np.array_equal(later, pl.plate_at(alone, 105, camera=hs.track, bank=hs.bank))
    # The icons read each pattern's ink over its own span (_timed_inks), and
    # sit beside the one on screen the longest (Structure).
    out = scene.work / "preview" / studio._preview_key(recipe, recipe.seed)
    rows = studio._timed_inks(out, h)
    assert [(r["element_id"], r["t_in"], r["t_out"]) for r in rows] == [("pattern", 0.0, 1.5),
                                                                        ("pattern-2", 1.5, pytest.approx(end, abs=1e-4))]
    assert rows[0]["box"] == {"x": 0.0, "y": 0.0, "w": 1.0, "h": 1.0} and rows[1]["box"]["x"] == 0.0
    pat = asyncio.run(studio._icon_pattern(scene, recipe, recipe.seed, None, None, spec=None, final=False))
    assert pat.timed is not None and len(pat.timed) == 2 and pat.box == strip["box"]
    assert np.array_equal(pat.ink_at(3.0, 4.0), rows[1]["ink"]), "at 3-4 s: Structure's ink alone"
    plain = _house(_el("pattern", "structure"))
    plain_body = asyncio.run(studio.preview(1, "scene_0", plain))
    plain_out = scene.work / "preview" / studio._preview_key(plain, plain.seed)
    assert studio._timed_inks(plain_out, plain_body["house"]) is None, "every pattern the whole scene: as always"
    assert asyncio.run(studio._icon_pattern(scene, plain, plain.seed, None, None, spec=None, final=False)).timed is None
    assert paid == []


@needs_masks
def test_a_pattern_timed_past_the_scenes_end_is_said_not_drawn(scene, paid):
    recipe = _house(_el("pattern", "structure", start=6.0, end=7.0))          # the scene is 4.27 s
    body = asyncio.run(studio.preview(1, "scene_0", recipe))
    [p] = body["patterns"]
    assert p["timing"] is None and p["error"] is None
    assert studio.SPAN_AFTER_END.format(name="Structure") in body["warnings"]
    [layer] = body["house"]["layers"]
    assert layer["timing"] is None, "the plate never shows it either"
    assert paid == []


# ---------------------------------------------------------------------------
# The exits (tables)
# ---------------------------------------------------------------------------

def _solid(box=(10, 20, 50, 70), w=64, h=96) -> np.ndarray:
    rgba = np.zeros((h, w, 4), np.uint8)
    x0, y0, x1, y1 = box
    rgba[y0:y1, x0:x1] = (0x7F, 0x2B, 0xF2, 255)
    return pl.premultiplied(rgba)


def _alpha_of(moved: pl.Moved | None) -> np.ndarray:
    if moved is None:
        return np.zeros((96, 64), np.float32)
    return np.asarray(moved.art[..., 3], np.float32)


@pytest.mark.parametrize("kind", [e for e in rc.PATTERN_EXITS if e != "auto"])
def test_every_exit_is_all_there_at_its_start_and_gone_at_its_end(kind):
    box = (10, 20, 50, 70)
    art = _solid(box)
    start = pl.motion(art, ink_box=box, kind=kind, direction="out", q=0.0, seed="s", frame=0, edge="left",
                      frame_size=(64, 96))
    assert start is not None and np.array_equal(_alpha_of(start), art[..., 3].astype(np.float32))
    assert (start.dx, start.dy) == (0.0, 0.0), "at its start: where it holds"
    end = pl.motion(art, ink_box=box, kind=kind, direction="out", q=1.0, seed="s", frame=30, edge="left",
                    frame_size=(64, 96))
    if kind == "none":
        assert end is not None and end.art is art, "none: it holds to its last frame"
    elif kind == "slide_out":
        assert end is not None and end.dx == -(box[2]), "slid out past the left edge"
    else:
        assert end is None or not _alpha_of(end).any(), "gone"


@pytest.mark.parametrize("kind,keeps", [("to_left", "left"), ("to_right", "right"), ("to_top", "top"),
                                        ("to_bottom", "bottom")])
def test_a_wipe_out_goes_back_towards_its_own_edge(kind, keeps):
    box = (10, 20, 50, 70)
    half = pl.motion(_solid(box), ink_box=box, kind=kind, direction="out", q=0.5, seed="s", frame=15)
    a = _alpha_of(half) > 0
    cols, rows = np.nonzero(a.any(axis=0))[0], np.nonzero(a.any(axis=1))[0]
    x0, y0, x1, y1 = box
    edge = {"left": cols[0] == x0 and cols[-1] < x1 - 1, "right": cols[-1] == x1 - 1 and cols[0] > x0,
            "top": rows[0] == y0 and rows[-1] < y1 - 1, "bottom": rows[-1] == y1 - 1 and rows[0] > y0}
    assert edge[keeps], f"{kind}: what is left hugs the {keeps} edge"
    # It is its entrance from that edge played backwards.
    back = {"to_left": "left_to_right", "to_right": "right_to_left", "to_top": "top_to_bottom",
            "to_bottom": "bottom_to_top"}[kind]
    assert lib.EXIT_OF_ENTRANCE[back] == kind


@pytest.mark.parametrize("edge,dx,dy", [("left", -50, 0), ("right", 64 - 10, 0), ("top", 0, -70),
                                        ("bottom", 0, 96 - 20)])
def test_a_slide_out_leaves_by_the_edge_it_came_from(edge, dx, dy):
    box = (10, 20, 50, 70)
    out = pl.motion(_solid(box), ink_box=box, kind="slide_out", direction="out", q=1.0, seed="s", frame=0,
                    edge=edge, frame_size=(64, 96))
    assert (out.dx, out.dy) == (dx, dy)


def test_the_auto_exit_is_none_at_the_scenes_end_else_the_entrance_backwards():
    el = _el("pattern", "structure")
    for entrance in lib.ENTRANCES:
        whole = pattern_el.timing(el, duration=8.0, entrance=entrance)
        assert whole.exit == "none" and whole.t_hold_end == whole.t_out == 8.0, entrance
        short = pattern_el.timing(_el("pattern", "structure", end=5.0), duration=8.0, entrance=entrance)
        assert short.exit == lib.EXIT_OF_ENTRANCE[entrance], entrance
        assert short.t_out == 5.0 and short.t_hold_end == pytest.approx(5.0 - (0.6 if short.exit != "none" else 0.0))
    picked = pattern_el.timing(_el("pattern", "structure", end=5.0, exit="glitch", exit_sec=1.2), duration=8.0,
                               entrance="left_to_right")
    assert (picked.exit, picked.t_hold_end) == ("glitch", pytest.approx(3.8))


# ---------------------------------------------------------------------------
# The icons keep clear of each pattern's entrance and exit (C5, C6)
# ---------------------------------------------------------------------------

def _zones(recipe: rc.Recipe, duration: float = 8.0, stack=None):
    return studio._icon_zones(recipe, stack, duration)


def test_the_42_default_keeps_its_one_zone_and_every_plan_key():
    one = I.zones_of(True, None)
    assert one == [{"from": 0.0, "to": 1.5, "why": I.ZONE_ENTRANCE}]
    default = pattern_el.timing(rc.pattern_elements(rc.preset_42_house("scene_0"))[0], duration=8.0,
                                entrance="left_to_right")
    assert I.pattern_zones([default.public()]) == one
    assert _zones(rc.preset_42_house("scene_0")) is None, "the zone it always had: no new key"
    assert _zones(rc.preset_42_default("scene_0")) is None, "Omni: as ever"
    two = _house(_el("pattern", "structure"), _el("pattern-2", "beads_row"))
    assert _zones(two) is None, "two patterns coming in together keep one zone"
    none = _house(_el("pattern", "structure", entrance="none"))
    assert _zones(none) is None and studio._icon_enters(none, None) is False
    # A plan with no zones of its own keys exactly as before (IconCtx.zones None).
    ctx = I.IconCtx(words={}, director={}, enters=True)
    assert ctx.zones is None and I.zones_of(ctx.enters, ctx.zones) == one


def test_a_trimmed_pattern_keeps_icons_off_its_entrance_and_its_exit():
    trimmed = _house(_el("pattern", "structure", start=2.0, end=5.0))
    assert _zones(trimmed) == [{"from": 2.0, "to": 3.5, "why": I.ZONE_ENTRANCE},
                               {"from": 4.4, "to": 5.0, "why": I.ZONE_EXIT}]
    slow = _house(_el("pattern", "structure", duration_sec=2.0))
    assert _zones(slow) == [{"from": 0.0, "to": 2.5, "why": I.ZONE_ENTRANCE}], "a long entrance: its ramp + 0.5 s"
    transition = _house(_transition(), _el("pattern-2", "structure", start=T.sec))
    assert _zones(transition) == [{"from": 0.0, "to": 1.5, "why": I.ZONE_ENTRANCE},
                                  {"from": 1.0, "to": 1.5, "why": I.ZONE_EXIT},
                                  {"from": 1.5, "to": 3.0, "why": I.ZONE_ENTRANCE}]


def test_the_zones_read_from_patterns_for_icons_are_the_recipes():
    """C5: house.patterns_for_icons gives each layer's time (and its
    entrance and exit), and the icons' zones made from it are the zones the
    studio makes from the recipe."""
    recipe = _house(_transition(), _el("pattern-2", "structure", start=T.sec, end=6.0))
    layers = []
    for el in rc.pattern_elements(recipe):
        entrance = "fade" if el.props["slug"] == "blueprint" else "left_to_right"
        art = _solid((0, 0, 64, 96) if entrance == "fade" else (0, 10, 30, 90))
        layers.append(pl.LayerSpec(element_id=el.id, art=art, ink_box=pl.ink_box(art[..., 3]),
                                   timing=pattern_el.timing(el, duration=8.0, entrance=entrance)))
    rows = house.patterns_for_icons(layers, None, fps=30.0, n=240, width=64, height=96)
    assert [(r["entrance"], r["exit"]) for r in rows] == [("fade", "fade"), ("left_to_right", "to_left")]
    assert I.pattern_zones(rows) == _zones(recipe)
    assert rows[1]["ink"].shape == (12, 8) and rows[1]["box"]["x"] == 0.0


def test_the_scheduler_keeps_settles_out_of_every_zone():
    zones = [{"from": 0.0, "to": 1.5, "why": I.ZONE_ENTRANCE}, {"from": 4.4, "to": 5.0, "why": I.ZONE_EXIT}]

    def cue(key, at):
        return I.Cue(key=key, glyph={"source": "library", "id": key}, label=key.capitalize(), priority=5,
                     rank=(-5, 0, int(at * 10)), anchors=[(0, int(at * 10), at, at + 0.3)])

    s = I.Scheduler(duration=8.0, enters=False, zones=zones)
    slots, drops = s.run([cue("hook", 1.0), cue("goes", 4.6), cue("fine", 3.2)])
    assert [x.cue.key for x in slots] == ["fine"]
    assert drops == {"hook": I.IN_ENTRANCE, "goes": I.IN_EXIT}
    assert {I._state(r) for r in drops.values()} == {"zone"}, "said in a zone: not 'no room'"
    # The same words added by the user always show: each moved to the
    # nearest time allowed, keeping its word (said: `shifts`), and the AI's
    # own pick gives way to them.
    mine = [cue("hook", 1.0), cue("goes", 4.6)]
    for c in mine:
        c.tier, c.source = 1, "user"
    s = I.Scheduler(duration=8.0, enters=False, zones=zones)
    slots, drops = s.run([*mine, cue("fine", 3.2)])
    got = {x.cue.key: x for x in slots}
    assert set(got) == {"hook", "goes"} and drops == {"fine": "No room right after 'Hook'"}
    assert got["hook"].settle_f == 45 and got["hook"].anchor[2] == 1.0, "1.5 s, the first time allowed, on its word"
    assert s.shifts["hook"] == {"at": 0.93, "said": 1.0, "why": "entrance"}
    assert got["goes"].settle_f == 131 and s.shifts["goes"]["why"] == "exit_before", "just before it goes out"
    assert all(s._zone_at(x.settle_f) is None for x in slots)
    assert s.rescued == {"hook": "shift", "goes": "shift"}
    # Without zones the same words show (the two apart by the clear air).
    slots, _ = I.Scheduler(duration=8.0, enters=False).run([cue("goes", 4.6), cue("fine", 2.2)])
    assert sorted(x.cue.key for x in slots) == ["fine", "goes"]
    # A time set by hand is kept, and said.
    fixed = cue("hand", 4.6)
    fixed.fixed, fixed.off_word, fixed.pinned, fixed.anchors, fixed.tier = 4.6, True, True, [], 0
    s = I.Scheduler(duration=8.0, enters=False, zones=zones)
    slots, drops = s.run([fixed])
    assert drops == {} and s.hand_notes["hand"] == [I.W_HAND_EXIT.format(label="Hand")]
    # The default zone as frames is today's first 1.5 s, anything before it too.
    assert I.Scheduler(duration=8.0, enters=True).zones_f == [(-np.inf, 45, I.ZONE_ENTRANCE)]


def test_the_lane_shows_the_patterns_zones():
    words = {"duration": 8.0, "words": [], "digest": "d"}
    tl = I.timeline_of(words, duration=8.0, enters=False,
                       zones=[{"from": 2.0, "to": 3.5, "why": I.ZONE_ENTRANCE},
                              {"from": 4.4, "to": 5.0, "why": I.ZONE_EXIT}])
    assert [(z["from"], z["to"], z["why"]) for z in tl["zones"]] == [
        (0.0, I.HEAD_CLEAR, I.ZONE_START), (2.0, 3.5, I.ZONE_ENTRANCE), (4.4, 5.0, I.ZONE_EXIT),
        (7.7, 8.0, I.ZONE_END)]
    old = I.timeline_of(words, duration=8.0, enters=True)
    assert [z["why"] for z in old["zones"]] == [I.ZONE_START, I.ZONE_ENTRANCE, I.ZONE_END], "as always"


def test_an_icon_is_judged_against_the_patterns_on_screen_with_it():
    full = np.ones((12, 8), np.float32)
    strip = np.zeros((12, 8), np.float32)
    strip[:, :3] = 1.0
    p = I.IconPattern(key="k", ink=np.maximum(full, strip), timed=[(0.0, 1.5, full), (1.5, 8.0, strip)])
    assert np.array_equal(p.ink_at(0.2, 0.8), full), "the transition"
    assert np.array_equal(p.ink_at(3.0, 5.0), strip), "Structure alone"
    assert np.array_equal(p.ink_at(1.0, 2.0), full), "both, while one gives way to the other"
    untimed = I.IconPattern(key="k", ink=strip)
    assert untimed.inks() is strip and untimed.ink_at(0, 9) is strip, "every pattern the whole scene: as always"

    class Bg:
        times = np.array([0.5, 3.0, 5.0])
        thumbs = np.full((3, 12, 8, 3), 200, np.uint8)

    box = {"x": 0.7, "y": 0.4, "w": 0.25, "h": 0.2}                  # the right side: off the strip
    _, over = I.colour_samples([(box, 3.0, 5.0)], None, Bg(), p.inks())
    assert over == [0.0, 0.0, 0.0], "at 3-5 s only the strip is on screen, and not under it"
    _, over = I.colour_samples([(box, 3.0, 5.0)], None, Bg(), p.ink)
    assert over == [1.0, 1.0, 1.0], "judged against every pattern, it would sit on the transition"


# ---------------------------------------------------------------------------
# The studio's timeline edits, held to the server's rules
# ---------------------------------------------------------------------------

def test_the_studio_edits_time_by_the_servers_rules(library):
    """tests/fixtures/rebrand_pattern_time.json (scratchpad/build/i14/
    make_time_fixture.py, regenerated only on purpose): the server answers it
    as written — and frontend/scripts/check-rebrand-pattern-time.mjs holds
    lib/rebrandTiming.ts to it (node 22.6+; skipped where there is none)."""
    fx = json.loads((FIXTURES / "rebrand_pattern_time.json").read_text(encoding="utf-8"))
    for c in fx["overlap"]:
        assert rc.spans_overlap(tuple(c["a"]), tuple(c["b"])) is c["expect"], c
    for c in fx["clock"]:
        assert pattern_el._clock(c["sec"]) == c["expect"], c
    refused = 0
    for c in fx["clash"]:
        els = [_el("pattern" if i == 0 else f"pattern-{i + 1}", "grid" if p["full"] else "structure",
                   start=p["span"][0] or None, end=p["span"][1])
               for i, p in enumerate([*c["before"], c["who"]])]
        errors = [e for e in asyncio.run(pattern_el.check(_house(*els))) if e["field"] == "placement.anchor"]
        assert bool(errors) is c["refused"], c
        refused += bool(errors)
    assert 0 < refused < len(fx["clash"]), "both answers are in it"
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed here")
    version = subprocess.run([node, "--version"], capture_output=True, text=True, timeout=30).stdout.strip()
    major, minor = (int(x) for x in version.lstrip("v").split(".")[:2])
    if (major, minor) < (22, 6):
        pytest.skip(f"node {version} cannot strip types")
    out = subprocess.run([node, "--experimental-strip-types", "scripts/check-rebrand-pattern-time.mjs"],
                         capture_output=True, text=True, timeout=120, cwd=REPO / "frontend")
    assert out.returncode == 0, out.stdout + out.stderr
    assert "cases agree" in out.stdout


# ---------------------------------------------------------------------------
# The library and the catalogue
# ---------------------------------------------------------------------------

def test_blueprint_is_a_transition_and_the_catalogue_serves_the_exits(monkeypatch):
    async def no_uploads(*_a, **_k):
        return []

    monkeypatch.setattr(lib, "_uploads", no_uploads)             # never the database
    assert lib.BUILTINS["blueprint"].public()["transition"] == {"sec": 1.5, "in_sec": 0.3, "out_sec": 0.5}
    for slug in ("structure", "beads_row", "beads_field", "grid"):
        assert "transition" not in lib.BUILTINS[slug].public(), f"{slug} keeps its shape: the whole scene"
    assert rc.RAMP_SEC[0] <= T.in_sec <= rc.RAMP_SEC[1] and rc.RAMP_SEC[0] <= T.out_sec <= rc.RAMP_SEC[1]
    assert T.sec >= rc.SPAN_MIN_SEC
    cat = asyncio.run(router.catalogue())
    assert cat["exits"] == lib.exits_public()
    assert [e["value"] for e in cat["exits"]] == list(rc.PATTERN_EXITS)
