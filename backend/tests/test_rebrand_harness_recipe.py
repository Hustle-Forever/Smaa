"""The harness recipe — the contract between the Rebrand window and the harness.

The next round's fields (the in-house engine, a pattern's time and hand move,
per-icon looks) are left out of the dump while unset: the three pinned
hashes, and every recipe stored on a version, re-hash exactly as before."""
from __future__ import annotations

import asyncio
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.services import pattern_library as lib
from app.services.rebrand_harness import recipe as rc
from app.services.rebrand_harness.elements import pattern as pattern_el

FIXTURES = Path(__file__).resolve().parent / "fixtures"
REPO = Path(__file__).resolve().parents[2]
STORED = json.loads((FIXTURES / "rebrand_recipes" / "stored_815.json").read_text(encoding="utf-8"))["versions"]
TIMING = json.loads((FIXTURES / "rebrand_timing.json").read_text(encoding="utf-8"))


def _el(id_, kind, **kw):
    return rc.Element(id=id_, kind=kind, **kw)


def test_42_default_is_one_structure_pattern_with_todays_settings():
    r = rc.preset_42_default("scene_2")
    assert r.target == "scene_2" and r.preset == "42-default" and r.engine == "omni"
    [el] = r.elements
    assert (el.kind, el.z, el.exact, el.enabled) == ("pattern", "behind", False, True)
    assert el.props["slug"] == "structure"
    assert el.placement.anchor == "auto" and el.animation.entrance == "auto"
    assert r.director.head == "brief" and r.director.intensity == "balanced"
    assert r.colours.source == "intro"
    assert r.guards == rc.Guards(speaker_front=True, face="shift", veil="entrance", bars="auto")


def test_kind_defaults_fill_z_and_exact():
    r = rc.normalize(rc.Recipe(director=rc.Director(head="harness"), elements=[
        _el("p", "pattern"), _el("l", "logo"), _el("t", "text"), _el("i", "icon"),
        _el("s", "slab"), _el("b", "beads"), _el("pl", "plate"),
    ]))
    got = {e.id: (e.z, e.exact) for e in r.elements}
    # Icons (M6) are stamped exactly by us, always behind the speaker.
    assert got == {"p": ("behind", False), "l": ("front", True), "t": ("front", True),
                   "i": ("behind", True), "s": ("behind", False), "b": ("behind", False),
                   "pl": ("behind", False)}


def test_an_explicit_z_or_exact_is_kept():
    r = rc.normalize(rc.Recipe(director=rc.Director(head="harness"), elements=[
        _el("l", "logo", z="behind", exact=False)]))
    assert (r.elements[0].z, r.elements[0].exact) == ("behind", False)


def test_the_limits_are_per_kind_and_name_the_element():
    with pytest.raises(rc.RecipeError) as e:
        rc.normalize(rc.Recipe(director=rc.Director(head="harness"),
                               elements=[_el("t1", "text"), _el("t2", "text")]))
    assert e.value.errors == [{"element_id": "t2", "field": "kind",
                               "message": "at most 1 text element(s)"}]


def test_a_pattern_cannot_be_stamped_exactly():
    with pytest.raises(rc.RecipeError, match="pattern cannot be stamped exactly"):
        rc.normalize(rc.Recipe(elements=[_el("p", "pattern", exact=True)]))


def test_a_custom_place_needs_a_box():
    with pytest.raises(rc.RecipeError, match="custom place needs a box"):
        rc.normalize(rc.Recipe(elements=[
            _el("s", "slab", placement=rc.Placement(anchor="custom"))]))
    ok = rc.normalize(rc.Recipe(elements=[_el("s", "slab", placement=rc.Placement(
        anchor="custom", box=rc.Box(x=0.1, y=0.1, w=0.3, h=0.2)))]))
    assert ok.elements[0].placement.box.w == 0.3


def test_a_pattern_takes_only_its_places_and_entrances():
    for anchor in rc.PATTERN_ANCHORS:
        rc.normalize(rc.Recipe(elements=[_el("p", "pattern", placement=rc.Placement(anchor=anchor))]))
    with pytest.raises(rc.RecipeError, match="cannot go 'top-left'"):
        rc.normalize(rc.Recipe(elements=[
            _el("p", "pattern", placement=rc.Placement(anchor="top-left"))]))
    with pytest.raises(rc.RecipeError, match="no 'typewriter' entrance"):
        rc.normalize(rc.Recipe(elements=[
            _el("p", "pattern", animation=rc.Animation(entrance="typewriter"))]))
    three = rc.normalize(rc.Recipe(elements=[_el("p1", "pattern"), _el("p2", "pattern"),
                                             _el("p3", "pattern", enabled=False)]))
    assert [e.id for e in three.elements] == ["p1", "p2", "p3"]
    with pytest.raises(rc.RecipeError) as e:
        rc.normalize(rc.Recipe(elements=[_el(f"p{i}", "pattern") for i in range(4)]))
    assert e.value.errors == [{"element_id": "p3", "field": "kind",
                               "message": "A scene takes up to 3 patterns."}]


def test_kinds_not_drawn_yet_are_kept_but_not_run():
    r = rc.normalize(rc.Recipe(elements=[_el("p", "pattern"), _el("s", "slab")]))
    with pytest.raises(rc.RecipeError, match="slab is not available yet"):
        rc.runnable(r)
    off = rc.normalize(rc.Recipe(elements=[_el("p", "pattern"), _el("s", "slab", enabled=False)]))
    rc.runnable(off)
    assert rc.pattern_element(off).id == "p"
    assert rc.pattern_element(rc.Recipe(elements=[_el("p", "pattern", enabled=False)])) is None


def test_text_longer_than_the_brand_frame_caps_is_refused():
    with pytest.raises(rc.RecipeError, match="heading is longer than 40"):
        rc.normalize(rc.Recipe(director=rc.Director(head="harness"), elements=[
            _el("t", "text", props={"heading": "x" * 41})]))


def test_the_brief_head_cannot_carry_a_logo_text_or_icons():
    # The brief is told never to draw a logo; a recipe with one needs the
    # harness's own prompt opening. A switched-off logo is fine.
    with pytest.raises(rc.RecipeError, match="harness opening"):
        rc.normalize(rc.Recipe(elements=[_el("l", "logo")]))
    rc.normalize(rc.Recipe(elements=[_el("l", "logo", enabled=False)]))


def test_a_custom_colour_needs_a_hex():
    with pytest.raises(rc.RecipeError, match="hex"):
        rc.normalize(rc.Recipe(colours=rc.Colours(source="custom")))
    with pytest.raises(ValidationError):
        rc.Colours(source="custom", custom="purple")


def test_unknown_fields_bad_ids_and_duplicates_are_rejected():
    with pytest.raises(ValidationError):
        rc.Recipe.model_validate({"elements": [], "colour": "intro"})
    with pytest.raises(ValidationError):
        rc.Recipe.model_validate({"elements": [{"id": "Bad Id", "kind": "pattern"}]})
    with pytest.raises(ValidationError):
        rc.Recipe.model_validate({"elements": [{"id": "a", "kind": "pattern"},
                                               {"id": "a", "kind": "logo"}]})
    with pytest.raises(ValidationError):
        rc.Recipe.model_validate({"target": "reel"})
    with pytest.raises(ValidationError):
        rc.Recipe.model_validate({"elements": [{"id": f"e{i}", "kind": "slab"} for i in range(11)]})


def test_the_hash_is_canonical_and_moves_with_the_content():
    a = rc.preset_42_default("scene_1")
    b = rc.normalize(rc.Recipe.model_validate(a.model_dump(mode="json")))
    assert rc.canonical(a) == rc.canonical(b)
    assert rc.recipe_hash(a) == rc.recipe_hash(b) and len(rc.recipe_hash(a)) == 16
    moved = a.model_copy(update={"elements": [a.elements[0].model_copy(update={
        "placement": rc.Placement(anchor="edge-right")})]})
    assert rc.recipe_hash(moved) != rc.recipe_hash(a)


def test_normalize_is_idempotent():
    r = rc.normalize(rc.Recipe(director=rc.Director(head="harness"),
                               elements=[_el("p", "pattern"), _el("l", "logo")]))
    assert rc.canonical(rc.normalize(r)) == rc.canonical(r)
    assert rc.canonical(rc.normalize(r.model_dump(mode="json"))) == rc.canonical(r)


# --- the legacy options -----------------------------------------------------

def test_the_approved_request_maps_to_the_42_default():
    opts = {"target": "scene_3", "run_omni": True, "pattern_on": True, "pattern": "structure",
            "pattern_animation": None, "intensity": "balanced", "notes": ""}
    r = rc.recipe_from_options(opts)
    assert rc.canonical(r) == rc.canonical(rc.preset_42_default("scene_3"))


def test_a_picked_pattern_and_entrance_map_onto_the_element():
    r = rc.recipe_from_options({"target": "scene_1", "pattern_on": True, "pattern": "grid",
                                "pattern_animation": "glitch", "intensity": "bold",
                                "notes": "calm"})
    [el] = r.elements
    assert el.props["slug"] == "grid" and el.animation.entrance == "glitch"
    assert r.preset is None
    assert (r.director.intensity, r.director.notes) == ("bold", "calm")


def test_pattern_off_is_an_empty_recipe():
    r = rc.recipe_from_options({"target": "scene_1", "pattern_on": False})
    assert r.elements == []


def test_options_round_trip_through_a_recipe():
    for opts in (
        {"target": "scene_1", "run_omni": True, "run_brand": False, "pattern_on": True,
         "pattern": "structure", "pattern_animation": None, "intensity": "balanced", "notes": ""},
        {"target": "scene_4", "run_omni": True, "run_brand": False, "pattern_on": True,
         "pattern": "beads_field", "pattern_animation": "fade", "intensity": "subtle",
         "notes": "keep it quiet"},
    ):
        assert rc.options_from_recipe(rc.recipe_from_options(opts)) == opts


def test_the_pattern_entrances_are_the_librarys():
    # Omni is told its own (ANIMATIONS); our renderer draws those and a slide.
    assert set(rc.OMNI_PATTERN_ENTRANCES) == {"auto", *lib.ANIMATIONS}
    assert set(rc.PATTERN_ENTRANCES) == {"auto", *lib.ENTRANCES}
    assert set(lib.ENTRANCES) == {*lib.ANIMATIONS, "slide"}
    for anim in rc.PATTERN_ENTRANCES:
        rc.Animation(entrance=anim)


def test_the_pattern_exits_are_the_librarys():
    assert rc.PATTERN_EXITS == tuple(lib.EXITS)
    assert [e["value"] for e in lib.exits_public()] == list(rc.PATTERN_EXITS)
    # Every entrance has its reverse, and each is an exit.
    assert set(lib.EXIT_OF_ENTRANCE) == set(lib.ENTRANCES)
    assert set(lib.EXIT_OF_ENTRANCE.values()) <= set(rc.PATTERN_EXITS) - {"auto"}
    for ex in rc.PATTERN_EXITS:
        rc.Animation(exit=ex)
    with pytest.raises(ValidationError):
        rc.Animation(exit="to_middle")


# --- the 42 default is pinned -------------------------------------------------

def test_the_42_default_hash_and_options_never_move():
    """Recorded before several patterns, pins and props validation existed:
    none of them may touch the approved recipe's canonical form."""
    assert rc.recipe_hash(rc.preset_42_default()) == "1ab8816f2e8a7feb"
    assert rc.recipe_hash(rc.preset_42_default("scene_0")) == "a05a67c9c4c84f13"
    assert rc.options_from_recipe(rc.preset_42_default("scene_0")) == {
        "target": "scene_0", "run_omni": True, "run_brand": False, "pattern_on": True,
        "pattern": "structure", "pattern_animation": None, "intensity": "balanced", "notes": ""}


# --- several patterns ----------------------------------------------------------

def _pat(id_, slug="structure", **props):
    return _el(id_, "pattern", props={"slug": slug, **props})


def test_pattern_props_are_checked_and_never_rewritten():
    pin = {"placement": "right", "scale": 0.5, "reach": 0.5, "anchor": "left", "rotate": 270}
    ok = rc.normalize(rc.Recipe(elements=[_pat("pattern", "beads_row", colour="#7F2BF2",
                                               layout=pin, shuffle="ab12")]))
    assert ok.elements[0].props == {"slug": "beads_row", "colour": "#7F2BF2", "layout": pin,
                                    "shuffle": "ab12"}, "validated on a copy: kept as sent"
    for props, field in (
        ({"colour": "purple"}, "props.colour"),
        ({"colour": "#12345"}, "props.colour"),
        ({"layout": {"placement": "middle"}}, "props.layout.placement"),
        ({"layout": {**pin, "rotate": 45}}, "props.layout.rotate"),
        ({"layout": {**pin, "scale": 0}}, "props.layout.scale"),
        ({"layout": {**pin, "reach": 1.5}}, "props.layout.reach"),
        ({"layout": {**pin, "fit": "stretch"}}, "props.layout.fit"),
        ({"layout": "left"}, "props.layout"),
        ({"shuffle": "Not OK!"}, "props.shuffle"),
        ({"slug": "Bad Slug"}, "props.slug"),
        ({"glow": True}, "props"),
    ):
        with pytest.raises(rc.RecipeError) as e:
            rc.normalize(rc.Recipe(elements=[_el("pattern", "pattern", props=props)]))
        assert field in [x["field"] for x in e.value.errors], (props, e.value.errors)
    rc.normalize(rc.Recipe(elements=[_pat("pattern", colour=None, layout=None, shuffle=None)]))


def test_the_layout_rules_are_the_renderers():
    from app.services import rebrand_pattern_ref as ref

    assert rc.LAYOUT_PLACEMENTS == ref.PLACEMENTS and rc.LAYOUT_FITS == ref.FITS
    assert set(rc.LAYOUT_KEYS) >= set(ref.Layout(fit="cover", valign="bottom").public())
    # Every place the library draws can be pinned: the grid (x6) and an
    # upload's long strip (x4.45, x5.95) as much as a built-in's.
    assert rc.LAYOUT_SCALE_MAX == lib.SCALE_LIMITS[1]
    for entry in lib.BUILTINS.values():
        for c in entry.candidates:
            assert rc._layout_errors(c.public()) == [], (entry.slug, c)
    for c in (lib.fit_layouts(3.0, ["left", "right", "top", "full"], run=0.1, tiles=True, cell=50)
              + lib.fit_layouts(2.0, ["left", "right", "top"], run=0.2, dense_end="right")):
        assert rc._layout_errors(c.public()) == [], c
    assert rc._layout_errors({"placement": "left", "scale": 12.5}) == [
        ("props.layout.scale", "scale must be above 0 and at most 12")]


def test_pattern_elements_are_the_switched_on_ones_in_order():
    r = rc.normalize(rc.Recipe(elements=[_pat("pattern"), _pat("pattern-2", "grid"),
                                         _pat("pattern-3", "beads_row")]))
    r.elements[1].enabled = False
    assert [e.id for e in rc.pattern_elements(r)] == ["pattern", "pattern-3"]
    assert rc.pattern_element(r).id == "pattern"
    assert rc.new_pattern_id(rc.Recipe()) == "pattern"
    assert rc.new_pattern_id(rc.Recipe(elements=[_pat("pattern"), _pat("pattern-3")])) == "pattern-2"
    assert rc.new_pattern_id(r) == "pattern-4"


def test_the_legacy_options_list_every_pattern_only_when_there_are_several():
    one = rc.normalize(rc.Recipe(target="scene_1", elements=[_pat("pattern", "grid")]))
    assert "patterns" not in rc.options_from_recipe(one)
    two = rc.normalize(rc.Recipe(target="scene_1", elements=[
        _pat("pattern"), _pat("pattern-2", "beads_row"), _pat("pattern-3", "grid")]))
    two.elements[2].enabled = False
    opts = rc.options_from_recipe(two)
    assert opts["pattern"] == "structure" and opts["patterns"] == ["structure", "beads_row"]


# --- the API ------------------------------------------------------------------

@pytest.fixture
def client():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.routers import rebrand_harness as rt

    app = FastAPI()
    app.include_router(rt.router)
    return TestClient(app)


def test_the_schema_endpoint_serves_the_recipe_contract(client):
    r = client.get("/api/rebrand/harness/schema")
    assert r.status_code == 200
    body = r.json()
    assert body["recipe_version"] == 1 and body["max_elements"] == rc.MAX_ELEMENTS
    assert set(body["kinds"]) == {"pattern", "logo", "text", "icon", "slab", "beads", "plate"}
    schema = body["schema"]
    assert schema["title"] == "Recipe"
    assert {"Element", "Placement", "Animation", "Guards"} <= set(schema["$defs"])


def test_the_presets_endpoint_lists_the_42_default(client):
    body = client.get("/api/rebrand/harness/presets").json()
    [p] = body["presets"]
    assert (p["id"], p["name"], p["builtin"]) == ("42-default", "42 default", True)
    # Made in house (new work is): the approved element, our own renderer.
    assert rc.canonical(rc.normalize(p["recipe"])) == rc.canonical(rc.preset_42_house())
    house, omni = rc.preset_42_house("scene_0"), rc.preset_42_default("scene_0")
    assert house.engine == "house" and house.elements == omni.elements and house.preset == omni.preset


# --- the next round's contract: nothing stored moves ----------------------------

def test_every_stored_recipe_re_hashes_the_same():
    """Run 815's versions (v25 is the owner's) re-hash to what their runs
    recorded, and normalise to exactly what was stored."""
    assert set(STORED) == {"23", "24", "25"}
    for vid, v in STORED.items():
        r = rc.normalize(v["recipe"])
        assert rc.recipe_hash(r) == v["recipe_hash"], vid
        assert r.engine == "omni"
        assert json.loads(rc.canonical(r)) == v["recipe"], vid
    # v25's frozen icons read back and dump byte for byte (no new keys).
    icons = next(e for e in STORED["25"]["recipe"]["elements"] if e["kind"] == "icon")
    for item in icons["props"]["resolved"]:
        assert rc.IconResolved.model_validate(item).model_dump(mode="json") == item


def test_new_fields_are_left_out_while_unset():
    assert rc.Animation().model_dump(mode="json") == {
        "entrance": "auto", "start": {"mode": "clip_start", "sec": None, "word": None}, "duration_sec": 1.0}
    timed = rc.Animation(end=rc.End(mode="at_sec", sec=2.0), exit="fade", exit_sec=0.4).model_dump(mode="json")
    assert (timed["end"], timed["exit"], timed["exit_sec"]) == ({"mode": "at_sec", "sec": 2.0}, "fade", 0.4)
    assert set(rc.Animation(end=rc.End()).model_dump(mode="json")) == {"entrance", "start", "duration_sec", "end"}
    assert rc.IconFx(entrance="glitch", seed="s").model_dump(mode="json") == {"entrance": "glitch", "seed": "s"}
    assert rc.IconFx(entrance="grow", seed="s", exit="fade").model_dump(mode="json")["exit"] == "fade"
    # The 42 default's element dumps exactly the keys it always did.
    el = rc.preset_42_default().elements[0].model_dump(mode="json")
    assert set(el["animation"]) == {"entrance", "start", "duration_sec"}
    assert set(el["placement"]) == {"anchor", "region", "size", "margin", "box", "rotate", "offset"}
    house = rc.normalize({**rc.preset_42_default("scene_0").model_dump(mode="json"), "engine": "house"})
    assert json.loads(rc.canonical(house))["engine"] == "house"
    assert rc.recipe_hash(house) != rc.recipe_hash(rc.preset_42_default("scene_0"))


def _resolved(**kw):
    return {"glyph": {"source": "library", "id": "code"}, "from_sec": 1.0, "settle_sec": 1.2, "to_sec": 2.0,
            "box": {"x": 0.1, "y": 0.1, "w": 0.16, "h": 0.09}, "fill": "#7f2bf2", "ink": "#ffffff",
            "fx": {"entrance": "glitch", "seed": "s"}, **kw}


def test_a_frozen_icon_dumps_its_old_keys_and_new_ones_only_when_set():
    old = rc.IconResolved.model_validate(_resolved())
    assert set(old.model_dump(mode="json")) == {"glyph", "label", "from_sec", "settle_sec", "to_sec", "box",
                                                "fill", "ink", "fx", "style", "word", "purpose", "max_overlap"}
    assert (old.style, old.weight, old.layer, old.look_v, old.text, old.font) == (
        "badge", "fill", "behind", 0, None, None), "the defaults draw today's badge"
    with pytest.raises(ValidationError, match="look_v"):
        rc.IconResolved.model_validate(_resolved(style="circle"))
    circle = rc.IconResolved.model_validate(_resolved(style="circle", look_v=1, weight="bold")).model_dump(mode="json")
    assert (circle["style"], circle["look_v"], circle["weight"]) == ("circle", 1, "bold") and "text" not in circle
    with pytest.raises(ValidationError, match="words"):
        rc.IconResolved.model_validate(_resolved(style="chip", look_v=1))
    chip = rc.IconResolved.model_validate(_resolved(
        style="chip", look_v=1, weight="bold", text="Developer", font="jost-600@1a2b3c4d", layer="front",
        fx={"entrance": "grow", "seed": "s", "exit": "fade"})).model_dump(mode="json")
    assert (chip["text"], chip["font"], chip["layer"]) == ("Developer", "jost-600@1a2b3c4d", "front")
    assert chip["fx"] == {"entrance": "grow", "seed": "s", "exit": "fade"}
    assert rc.IconResolved.model_validate(chip).model_dump(mode="json") == chip
    with pytest.raises(ValidationError):
        rc.IconResolved.model_validate(_resolved(style="chip", look_v=1, text="x" * 26, font="f"))


# --- the engine -------------------------------------------------------------------

def test_the_engine_is_omni_or_house_and_house_is_drawn_from_its_milestone():
    omni = rc.preset_42_default("scene_0")
    house = rc.normalize({**omni.model_dump(mode="json"), "engine": "house"})
    assert house.engine == "house"
    with pytest.raises(ValidationError):
        rc.Recipe.model_validate({"engine": "veo"})
    rc.runnable(omni)
    if not rc.ENGINE_READY["house"]:
        with pytest.raises(rc.RecipeError) as e:
            rc.runnable(house)
        assert e.value.errors == [{"element_id": None, "field": "engine", "message": rc.ENGINE_NOT_READY}]


def test_house_ignores_the_omni_prompt_and_keeps_no_omni_icons():
    # The brief never places a logo or text — the in-house engine has no brief.
    with pytest.raises(rc.RecipeError, match="harness opening"):
        rc.normalize(rc.Recipe(elements=[_el("l", "logo")]))
    rc.normalize(rc.Recipe(engine="house", elements=[_el("l", "logo")]))
    # The director and the veil / stray guards are kept as sent, never refused.
    r = rc.normalize(rc.Recipe(engine="house", guards=rc.Guards(veil="off", stray="clean"),
                               director=rc.Director(intensity="bold", notes="calm"), elements=[_pat("pattern")]))
    assert (r.guards.stray, r.director.notes) == ("clean", "calm")
    with pytest.raises(rc.RecipeError) as e:
        rc.normalize(rc.Recipe(engine="house", elements=[_el("icons", "icon", props={"omni_own": "keep"})]))
    assert e.value.errors == [{"element_id": "icons", "field": "props.omni_own", "message": rc.OMNI_OFF_ICONS}]
    rc.normalize(rc.Recipe(elements=[_el("icons", "icon", props={"omni_own": "keep"})]))


# --- a pattern's time and hand move ----------------------------------------------

BOX = {"x": 0.0, "y": 0.2, "w": 0.5, "h": 0.6}
PIN = {"placement": "left", "scale": 2.0, "reach": 0.5}
FROZEN = {"layout": PIN, "colour": "#7f2bf2", "ground": "#7f2bf2", "ink": "#ffffff",
          "hand": {"dx": 0.1, "dy": 0.0, "scale": 1.2, "rotate": 15.0, "pivot": [0.25, 0.5]},
          "follow": "room", "engine_v": 1}


def test_a_timed_and_hand_moved_pattern_passes_in_house_and_is_never_rewritten():
    el = rc.Element(
        id="pattern", kind="pattern",
        animation=rc.Animation(entrance="slide", start=rc.Start(mode="at_sec", sec=1.0),
                               end=rc.End(mode="at_sec", sec=1.5), duration_sec=0.2, exit="to_left",
                               exit_sec=3.0),
        placement=rc.Placement(anchor="edge-left", offset=(0.25, -1.0), rotate=-180.0, size=3.0,
                               box=rc.Box(**BOX)),
        props={"slug": "structure", "layout": PIN, "follow": "screen", "resolved": FROZEN})
    r = rc.normalize(rc.Recipe(engine="house", elements=[el]))
    assert r.elements[0].props == el.props and r.elements[0].animation == el.animation
    assert rc.is_hand(el) and rc.hand_of(el) == rc.Hand(dx=0.25, dy=-1.0, scale=3.0, rotate=-180.0,
                                                         pivot=(0.25, 0.5))
    assert rc.pattern_span(el) == (1.0, 1.5)


def _timed(**kw):
    props = {"slug": "structure", **kw.pop("props", {})}
    return rc.Element(id="pattern", kind="pattern", props=props, **kw)


@pytest.mark.parametrize("el, field, message", [
    (_timed(animation=rc.Animation(start=rc.Start(mode="at_word", word="future"))),
     "animation.start.mode", "A pattern starts at a time."),
    (_timed(animation=rc.Animation(start=rc.Start(mode="at_sec"))),
     "animation.start.sec", "Say when the pattern starts."),
    (_timed(animation=rc.Animation(end=rc.End(mode="at_sec"))),
     "animation.end.sec", "Say when the pattern ends."),
    (_timed(animation=rc.Animation(start=rc.Start(mode="at_sec", sec=1.0), end=rc.End(mode="at_sec", sec=1.4))),
     "animation.end.sec", "A pattern shows for at least half a second."),
    (_timed(animation=rc.Animation(start=rc.Start(mode="at_sec", sec=3.0), end=rc.End(mode="at_sec", sec=2.0))),
     "animation.end.sec", "A pattern shows for at least half a second."),
    (_timed(animation=rc.Animation(duration_sec=0.1)),
     "animation.duration_sec", "A pattern comes in over 0.2 to 3 seconds."),
    (_timed(animation=rc.Animation(duration_sec=3.5)),
     "animation.duration_sec", "A pattern comes in over 0.2 to 3 seconds."),
    (_timed(animation=rc.Animation(exit_sec=0.1)),
     "animation.exit_sec", "A pattern goes out over 0.2 to 3 seconds."),
    (_timed(placement=rc.Placement(offset=(1.2, 0.0), box=rc.Box(**BOX)), props={"layout": PIN}),
     "placement.offset", "A pattern moves at most one frame's width or height each way."),
    (_timed(placement=rc.Placement(rotate=200.0, box=rc.Box(**BOX)), props={"layout": PIN}),
     "placement.rotate", "A pattern turns at most half a turn each way."),
    (_timed(placement=rc.Placement(size=0.4, box=rc.Box(**BOX)), props={"layout": PIN}),
     "placement.size", "A pattern is sized from half to three times its own size."),
    (_timed(placement=rc.Placement(size=3.5, box=rc.Box(**BOX)), props={"layout": PIN}),
     "placement.size", "A pattern is sized from half to three times its own size."),
    (_timed(placement=rc.Placement(offset=(0.1, 0.0), box=rc.Box(**BOX))),
     "placement.box", "A pattern moved by hand keeps the place it was moved from."),
    (_timed(placement=rc.Placement(rotate=10.0), props={"layout": PIN}),
     "placement.box", "A pattern moved by hand keeps the place it was moved from."),
    (_timed(props={"follow": "wall"}), "props.follow", "follow is one of auto, room, screen"),
    (_timed(props={"resolved": {**FROZEN, "colour": "violet"}}), "props.resolved.colour", "colour must be #rrggbb"),
    (_timed(props={"resolved": {**FROZEN, "glow": 1}}), "props.resolved", "unknown key(s) glow"),
    (_timed(props={"resolved": {**FROZEN, "layout": None}}), "props.resolved.layout",
     "a pinned place must be an object"),
    (_timed(props={"resolved": {**FROZEN, "follow": "auto"}}), "props.resolved.follow",
     "a frozen pattern follows the room or the screen"),
    (_timed(props={"resolved": {**FROZEN, "hand": {"dx": 0.1}}}), "props.resolved.hand",
     "a frozen hand move is {dx, dy, scale, rotate, pivot?}"),
    (_timed(props={"resolved": "left"}), "props.resolved", "a frozen pattern must be an object"),
])
def test_each_pattern_time_and_hand_refusal(el, field, message):
    with pytest.raises(rc.RecipeError) as e:
        rc.normalize(rc.Recipe(engine="house", elements=[el]))
    assert {"element_id": "pattern", "field": field, "message": message} in e.value.errors, e.value.errors


@pytest.mark.parametrize("el, field, message", [
    (_timed(animation=rc.Animation(entrance="slide")), "animation.entrance", rc.HOUSE_ONLY_SLIDE),
    (_timed(animation=rc.Animation(start=rc.Start(mode="at_sec", sec=1.0))), "animation.start", rc.HOUSE_ONLY_TIME),
    (_timed(animation=rc.Animation(end=rc.End())), "animation.end", rc.HOUSE_ONLY_TIME),
    (_timed(animation=rc.Animation(exit="fade")), "animation.exit", rc.HOUSE_ONLY_TIME),
    (_timed(animation=rc.Animation(exit_sec=0.5)), "animation.exit_sec", rc.HOUSE_ONLY_TIME),
    (_timed(placement=rc.Placement(offset=(0.1, 0.0), box=rc.Box(**BOX)), props={"layout": PIN}),
     "placement", rc.HOUSE_ONLY_HAND),
    (_timed(props={"follow": "room"}), "props.follow", rc.HOUSE_ONLY_FOLLOW),
])
def test_an_omni_recipe_cannot_ask_for_what_only_house_draws(el, field, message):
    """Omni repaints each part from its first frame: a recipe asking it for a
    slide, a span, a hand move or a follow is refused, never drawn otherwise
    than the studio shows it. The in-house engine takes each one."""
    with pytest.raises(rc.RecipeError) as e:
        rc.normalize(rc.Recipe(elements=[el]))
    assert e.value.errors == [{"element_id": "pattern", "field": field, "message": message}]
    rc.normalize(rc.Recipe(engine="house", elements=[el]))
    rc.normalize(rc.Recipe(elements=[_timed(props={"follow": "auto"})]))


def test_spans_and_hand_accessors():
    plain = rc.Element(id="p", kind="pattern")
    assert rc.pattern_span(plain) == (0.0, None) and not rc.is_hand(plain)
    assert rc.hand_of(plain) == rc.Hand() and rc.Hand().identity
    assert rc.pattern_span(_timed(animation=rc.Animation(start=rc.Start(mode="at_word", word="x")))) == (0.0, None)
    assert rc.spans_overlap((0.0, 1.5), (1.5, None)) is False, "spans that only touch"
    assert rc.spans_overlap((0.0, 1.5), (1.4, None)) is True
    assert rc.spans_overlap((0.0, None), (5.0, 6.0)) is True
    assert rc.spans_overlap((2.0, 3.0), (0.0, 2.0)) is False
    # An icon's placement.size is its S/M/L, never a hand move.
    assert not rc.is_hand(rc.Element(id="i", kind="icon", placement=rc.Placement(size=0.16)))
    assert not rc.Hand(scale=1.2).identity


def test_a_full_screen_pattern_goes_alone_only_while_it_shows(monkeypatch):
    async def lookup(slug):
        return lib.BUILTINS.get(slug)

    monkeypatch.setattr(lib, "lookup", lookup)

    def timed(id_, slug, start=None, end=None):
        return _el(id_, "pattern", props={"slug": slug}, animation=rc.Animation(
            start=rc.Start(mode="at_sec", sec=start) if start is not None else rc.Start(),
            end=rc.End(mode="at_sec", sec=end) if end is not None else None))

    def check(*els):
        return asyncio.run(pattern_el.check(rc.Recipe(engine="house", elements=list(els))))

    # Blueprint as a 1.5 s transition, then Structure: never on screen together.
    assert check(timed("pattern", "blueprint", end=1.5), timed("pattern-2", "structure", start=1.5)) == []
    [e] = check(timed("pattern", "blueprint", end=1.5), timed("pattern-2", "structure", start=1.0))
    assert e == {"element_id": "pattern-2", "field": "placement.anchor",
                 "message": "Blueprint fills the whole frame from 0:00 to 0:01.5, so nothing else can show then"}
    [e] = check(timed("pattern", "structure", start=1.0), timed("pattern-2", "blueprint", end=1.5))
    assert e["element_id"] == "pattern-2" and "from 0:00 to 0:01.5" in e["message"]
    [e] = check(timed("pattern", "structure"), timed("pattern-2", "grid", start=6.0))
    assert e["message"] == "Grid fills the whole frame from 0:06 to the end, so nothing else can show then"
    # Whole-scene spans: today's rule, in today's words.
    [e] = check(timed("pattern", "blueprint"), timed("pattern-2", "structure"))
    assert e["message"] == "Blueprint fills the whole frame, so it goes alone"
    # Three: a transition, then two strips that share the rest.
    assert check(timed("pattern", "blueprint", end=1.5), timed("pattern-2", "structure", start=1.5),
                 timed("pattern-3", "beads_row", start=2.0, end=6.0)) == []


# --- timing(): the table the studio's mirror shares -------------------------------

@pytest.mark.parametrize("case", TIMING["cases"], ids=[f"case{i}" for i in range(len(TIMING["cases"]))])
def test_the_timing_table(case):
    el = rc.Element(id="p", kind="pattern", animation=rc.Animation.model_validate(case["animation"]))
    got = pattern_el.timing(el, duration=case["duration"], entrance=case["entrance"])
    want = case["expect"]
    if want is None:
        assert got is None, case["name"]
        return
    pub = got.public()
    for k in ("t_in", "t_full", "t_hold_end", "t_out"):
        assert pub[k] == pytest.approx(want[k], abs=1e-4), (case["name"], k)
    assert {k: pub[k] for k in ("entrance", "exit", "runs_to_end")} == {
        k: want[k] for k in ("entrance", "exit", "runs_to_end")}, case["name"]
    for t, name, q in case.get("phases") or []:
        pn, pq = got.phase(t)
        assert pn == name and pq == pytest.approx(q, abs=1e-6), (case["name"], t)


def test_the_default_pattern_times_as_it_always_has():
    """No time set = today's look: in over 1 s from 0, held to the cut."""
    [el] = rc.preset_42_default("scene_0").elements
    got = pattern_el.timing(el, duration=7.8, entrance="left_to_right")
    assert (got.t_in, got.t_full, got.t_hold_end, got.t_out, got.exit) == (0.0, 1.0, 7.8, 7.8, "none")


def _ts_timing() -> str:
    p = REPO / "frontend" / "src" / "lib" / "rebrandTiming.ts"
    if not p.is_file():
        pytest.skip("no frontend checkout here")
    return p.read_text(encoding="utf-8")


def test_the_studio_mirror_has_the_same_numbers_and_words():
    ts = _ts_timing()
    exits = re.search(r"export const PATTERN_EXITS = \[(.*?)\] as const", ts, re.S).group(1)
    assert tuple(re.findall(r'"([a-z_]+)"', exits)) == rc.PATTERN_EXITS
    for name, value in (("SPAN_MIN_SEC", rc.SPAN_MIN_SEC), ("EXIT_SEC", rc.EXIT_SEC),
                        ("RAMP_SQUEEZE_MIN", pattern_el.RAMP_SQUEEZE_MIN)):
        assert float(re.search(rf"export const {name} = ([0-9.]+);", ts).group(1)) == value, name
    lo, hi = re.search(r"export const RAMP_SEC[^=]*= \[([0-9.]+), ([0-9.]+)\]", ts).groups()
    assert (float(lo), float(hi)) == rc.RAMP_SEC
    table = re.search(r"export const EXIT_OF_ENTRANCE[^{]*\{(.*?)\};", ts, re.S).group(1)
    assert dict(re.findall(r"([a-z_]+): \"([a-z_]+)\"", table)) == lib.EXIT_OF_ENTRANCE


def test_the_studio_mirror_answers_the_table_alike():
    """frontend/scripts/check-rebrand-timing.mjs runs the TS mirror on the
    same fixture (node 22.6+ strips the types; skipped where there is none,
    as on the VPS)."""
    _ts_timing()
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed here")
    version = subprocess.run([node, "--version"], capture_output=True, text=True, timeout=30).stdout.strip()
    major, minor = (int(x) for x in version.lstrip("v").split(".")[:2])
    if (major, minor) < (22, 6):
        pytest.skip(f"node {version} cannot strip types")
    out = subprocess.run([node, "--experimental-strip-types", "scripts/check-rebrand-timing.mjs"],
                         capture_output=True, text=True, timeout=120, cwd=REPO / "frontend")
    assert out.returncode == 0, out.stdout + out.stderr
    assert f"all {len(TIMING['cases'])} cases agree" in out.stdout


# --- icons: their own times, their own looks ----------------------------------

@pytest.mark.parametrize("anim, field", [
    (rc.Animation(start=rc.Start(mode="at_sec", sec=1.0)), "animation.start"),
    (rc.Animation(end=rc.End()), "animation.end"),
    (rc.Animation(exit="fade"), "animation.exit"),
    (rc.Animation(exit_sec=0.5), "animation.exit_sec"),
])
def test_icons_keep_their_own_times(anim, field):
    with pytest.raises(rc.RecipeError) as e:
        rc.normalize(rc.Recipe(engine="house", elements=[_el("icons", "icon", animation=anim)]))
    assert e.value.errors == [{"element_id": "icons", "field": field, "message": rc.ICONS_OWN_TIMES}]


def test_icon_looks_are_in_the_contract_and_new_motions_wait_for_their_renderer():
    rc.IconProps(style="circle")
    rc.IconProps(style="chip", looks={"code": {"style": "chip", "label": "Developer"}})
    assert rc.IconProps().style == "badge", "unset keeps meaning the old look"
    # The circle and the chip are drawn (I5), and a chip's grow (I15).
    assert set(rc.ICON_STYLES_READY) == {"circle", "chip", "badge", "glyph"}
    for props in ({"style": "circle"}, {"style": "chip", "looks": {"code": {"style": "circle"}}}):
        assert rc.normalize(rc.Recipe(elements=[_el("icons", "icon", props=props)])).elements[0].props == props
    assert set(rc.ICON_MOTIONS_READY) == {"none", "fade", "pop", "slide", "glitch", "grow"}
    for key in ("enter", "leave"):
        props = {"style": "chip", "looks": {"code": {key: "grow"}}}
        assert rc.normalize(rc.Recipe(elements=[_el("icons", "icon", props=props)])).elements[0].props == props
    # Grow is an icon's own: the scene-wide entrance stays as it was.
    assert "grow" not in rc.ICON_ENTRANCES
    looks = {"code": {"colour": "#7f2bf2", "ink": "#ffffff", "label": "Developer", "size": 0.3,
                      "layer": "front", "enter": "pop", "leave": "fade"},
             "c-0123456789ab": {"style": "glyph"}}
    r = rc.normalize(rc.Recipe(elements=[_el("icons", "icon", props={"looks": looks})]))
    assert r.elements[0].props == {"looks": looks}, "validated on a copy: kept as sent"
    for hold in (0.2, 30.0):
        rc.normalize(rc.Recipe(elements=[_el("icons", "icon", props={"tweaks": {"code": {"hold_sec": hold}}})]))


@pytest.mark.parametrize("props, field", [
    ({"looks": {f"g{i}": {} for i in range(9)}}, "props.looks"),
    ({"looks": {"Bad Id": {}}}, "props.looks"),
    ({"looks": {"code": {"label": "x" * 25}}}, "props.looks.code.label"),
    ({"looks": {"code": {"label": ""}}}, "props.looks.code.label"),
    ({"looks": {"code": {"size": 0.05}}}, "props.looks.code.size"),
    ({"looks": {"code": {"colour": "violet"}}}, "props.looks.code.colour"),
    ({"looks": {"code": {"enter": "spin"}}}, "props.looks.code.enter"),
    ({"looks": {"code": {"glow": True}}}, "props.looks.code.glow"),
    ({"tweaks": {"code": {"hold_sec": 0.1}}}, "props.tweaks.code.hold_sec"),
    ({"tweaks": {"code": {"hold_sec": 31}}}, "props.tweaks.code.hold_sec"),
    ({"hold_sec": 0.5}, "props.hold_sec"),
    ({"style": "pill"}, "props.style"),
])
def test_each_icon_look_and_hold_refusal(props, field):
    with pytest.raises(rc.RecipeError) as e:
        rc.normalize(rc.Recipe(elements=[_el("icons", "icon", props=props)]))
    assert field in [x["field"] for x in e.value.errors], e.value.errors


# --- the router -----------------------------------------------------------------

def test_the_router_never_reads_a_frozen_pattern_a_client_sends():
    from app.routers import rebrand_harness as rt

    recipe = rc.preset_42_default("scene_0").model_dump(mode="json")
    recipe["elements"][0]["props"]["resolved"] = FROZEN
    r = rt._recipe(rt.RecipeRequest(run_id=815, target="scene_0", recipe=recipe))
    assert "resolved" not in r.elements[0].props
    want = rc.preset_42_default("scene_0").model_copy(update={"seed": r.seed})
    assert rc.recipe_hash(r) == rc.recipe_hash(want)
    plain = rc.preset_42_default("scene_0")
    assert pattern_el.strip_resolved(plain) is plain


def test_a_house_recipe_is_not_previewed_before_its_engine_exists(client, monkeypatch):
    if rc.ENGINE_READY["house"]:
        pytest.skip("the in-house engine draws now")

    async def lookup(slug):
        return lib.BUILTINS.get(slug)

    monkeypatch.setattr(lib, "lookup", lookup)
    recipe = {**rc.preset_42_default("scene_0").model_dump(mode="json"), "engine": "house"}
    got = client.post("/api/rebrand/harness/preview", json={"run_id": 815, "target": "scene_0", "recipe": recipe})
    assert got.status_code == 400
    assert got.json()["detail"]["errors"] == [{"element_id": None, "field": "engine",
                                               "message": rc.ENGINE_NOT_READY}]
