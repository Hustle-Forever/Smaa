"""The harness run pieces: the pattern element, the target composer, the
guards, stage keys and reuse, the runner switch, the studio preview and its
API. Omni, Gemini and the database are faked throughout."""
from __future__ import annotations

import asyncio
import dataclasses
import json
import shutil
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from app.services import omni_client as omni
from app.services import pattern_library as lib
from app.services import pattern_planner
from app.services import person_matte
from app.services import rebrand_media as media
from app.services import rebrand_pattern_ref as pattern_ref
from app.services import rebrand_pipeline as pipeline
from app.services import rebrand_store as store
from app.services import speaker_front
from app.services.rebrand_harness import compose
from app.services.rebrand_harness import guards as gd
from app.services.rebrand_harness import prompt as hprompt
from app.services.rebrand_harness import recipe as rc
from app.services.rebrand_harness import runner
from app.services.rebrand_harness import stages as st
from app.services.rebrand_harness import studio
from app.services.rebrand_harness.elements import pattern as pattern_el

FIX = Path(__file__).parent / "fixtures" / "rebrand_golden"
INTRO, FRAME, PERSON = FIX / "intro.mp4", FIX / "frame.png", FIX / "person.png"
REAL_MASKS = all(pattern_ref._mask_path(s).exists() for s in lib.BUILTINS)


def _person() -> np.ndarray:
    return np.asarray(Image.open(PERSON).convert("L"), dtype="float32") / 255.0


def _pattern(**kw) -> rc.Element:
    return rc.Element(id="pattern", kind="pattern", props={"slug": kw.pop("slug", "structure")}, **kw)


# ---------------------------------------------------------------------------
# The pattern element: where it goes, how it enters, what colour it is
# ---------------------------------------------------------------------------

def test_auto_keeps_the_patterns_own_places():
    for entry in lib.BUILTINS.values():
        assert pattern_el.candidates(entry, "auto", None) == entry.candidates


def test_the_other_side_is_the_same_strip_mirrored():
    structure = lib.BUILTINS["structure"]
    [right] = pattern_el.candidates(structure, "edge-right", None)
    assert right == dataclasses.replace(structure.layout, placement="right")
    assert pattern_el.candidates(structure, "edge-left", None) == (structure.layout,)
    assert pattern_ref.where_text(right) == "the right half of the frame, behind the speaker"


def test_an_edge_the_pattern_already_has_keeps_its_own_layouts():
    row = lib.BUILTINS["beads_row"]
    top = pattern_el.candidates(row, "edge-top", None)
    assert top == tuple(c for c in row.candidates if c.placement == "top") and len(top) == 2


@pytest.mark.skipif(not REAL_MASKS, reason="pattern masks not on this machine")
def test_an_edge_the_pattern_never_had_is_worked_out_from_its_art():
    blueprint = lib.BUILTINS["blueprint"]
    left = pattern_el.candidates(blueprint, "edge-left", None)
    assert left and all(c.placement == "left" for c in left)
    full = pattern_el.candidates(lib.BUILTINS["structure"], "full", None)
    assert full and full[0].placement == "full"


def test_the_recipes_colour_wins_over_the_intros():
    el = _pattern()
    assert pattern_el.colour_override(rc.Colours(), el) is None
    assert pattern_el.colour_override(rc.Colours(source="brand"), el) == pattern_ref.BRAND_GROUND
    assert pattern_el.colour_override(rc.Colours(source="custom", custom="#12AB34"), el) == "#12ab34"
    own = _pattern()
    own.props["colour"] = "#FFAA00"
    assert pattern_el.colour_override(rc.Colours(source="brand"), own) == "#ffaa00"


@pytest.fixture
def built(tmp_path, monkeypatch):
    """pattern_el.build with the renderer, the colours and the planner faked."""
    seen: dict = {"steps": []}
    colours = pattern_ref.Colours(ground="#3d3c3d", ink="#f4f4f8", fell_back=False, frame=None,
                                  colour="#2ed8e8")

    def dig(intro, out_dir, *, is_still=False):
        seen["dig"] = True
        return colours

    def build(intro, out_dir, *, aspect, is_still=False, **kw):
        seen["build"] = kw
        c = kw.get("colours") or colours
        return pattern_ref.PatternRef(slug=kw["slug"], source="x.png", path=tmp_path / "x.png",
                                      ground=c.ground, ink=c.ink, fell_back=False, colour=c.colour,
                                      where=pattern_ref.where_text(kw["layout"]),
                                      layout=kw["layout"], animation=kw["animation"])

    def choose(candidates, frame, person, *, seed, slug, mask=None, colour=None, work_dir=None):
        seen["choose"] = {"candidates": candidates, "seed": seed, "colour": colour}
        return candidates[-1], {"mode": "auto", "seed": seed, "reason": "near-best",
                                "picked": {"index": len(candidates) - 1}, "pool": [], "candidates": []}

    async def person_mask(frame):
        return None

    monkeypatch.setattr(pattern_ref, "dig_colours", dig)
    monkeypatch.setattr(pattern_ref, "build", build)
    monkeypatch.setattr(pattern_planner, "choose", choose)
    monkeypatch.setattr(pattern_planner, "summary", lambda report: "summary")
    monkeypatch.setattr(person_matte, "person_mask", person_mask)

    async def step(name, status="running", detail=""):
        seen["steps"].append((name, status, detail))

    async def plan_frame():
        return FRAME

    def run(el, colours_=rc.Colours(), seed="rebrand:9"):
        job = pattern_el.PatternJob(exp_id=9, aspect="9:16", out_dir=tmp_path / "pattern",
                                    intro=INTRO, intro_is_still=False, seed=seed,
                                    plan_frame=plan_frame, step=step)
        return asyncio.run(pattern_el.build(el, colours_, job))

    seen["run"] = run
    return seen


def test_structure_on_the_right_enters_from_the_right(built):
    res = built["run"](_pattern(placement=rc.Placement(anchor="edge-right")))
    assert res.ref.layout.placement == "right" and res.error == ""
    assert built["build"]["animation"] == "right_to_left"
    assert "colours" not in built["build"] and "dig" not in built, "intro colour: build digs it"
    assert "choose" not in built, "one place: nothing to plan"


def test_a_picked_entrance_is_kept_whatever_the_side(built):
    built["run"](_pattern(placement=rc.Placement(anchor="edge-right"),
                          animation=rc.Animation(entrance="glitch")))
    assert built["build"]["animation"] == "glitch"


def test_a_picked_colour_is_dug_once_and_replaces_the_intros(built):
    res = built["run"](_pattern(), rc.Colours(source="brand"))
    assert built["build"]["colours"].colour == pattern_ref.BRAND_GROUND
    assert built["build"]["colours"].ground == "#3d3c3d", "the intro's ground stays in the record"
    assert res.ref.colour == pattern_ref.BRAND_GROUND
    assert built["steps"][-1][2].startswith("the 42 brand colour")


def test_two_places_are_planned_with_the_recipes_seed(built):
    res = built["run"](_pattern(slug="beads_row", placement=rc.Placement(anchor="edge-top")),
                       seed="studio:5:scene_1")
    plan = built["choose"]
    assert plan["seed"] == "studio:5:scene_1:beads_row"
    assert [c.placement for c in plan["candidates"]] == ["top", "top"]
    assert res.ref.layout.anchor == "right" and res.report["seed"] == plan["seed"]
    assert built["build"]["animation"] == "right_to_left", "a top-right band sweeps in from the right"


def test_no_pattern_element_is_pattern_off(built):
    res = built["run"](None)
    assert res.ref is None and built["steps"] == [("Pattern: off", "done", "turned off for this run")]


# ---------------------------------------------------------------------------
# The target frame
# ---------------------------------------------------------------------------

def _solid(w, h, rgba):
    return Image.new("RGBA", (w, h), rgba)


def test_behind_layers_go_behind_the_speaker_and_front_layers_over_them(tmp_path):
    frame = tmp_path / "f.png"
    Image.new("RGB", (100, 100), (10, 10, 10)).save(frame)
    person = np.zeros((100, 100), np.float32)
    person[30:70, 30:70] = 1.0
    out = compose.compose_target(frame, tmp_path / "t.png", [
        compose.Layer(render=lambda w, h: _solid(w, h, (255, 0, 0, 255)), z="behind"),
        compose.Layer(render=lambda w, h: None, z="behind"),
    ], person=person)
    px = np.asarray(Image.open(out))
    assert tuple(px[50, 50]) == (10, 10, 10), "the speaker stays in front of a behind layer"
    assert tuple(px[5, 5]) == (255, 0, 0)
    out = compose.compose_target(frame, tmp_path / "t2.png", [
        compose.Layer(render=lambda w, h: _solid(w, h, (0, 0, 255, 255)), z="front"),
        compose.Layer(render=lambda w, h: _solid(w, h, (255, 0, 0, 255)), z="behind"),
    ], person=person)
    px = np.asarray(Image.open(out))
    assert tuple(px[50, 50]) == (0, 0, 255) and tuple(px[5, 5]) == (0, 0, 255), \
        "a front layer covers everything, whatever its order in the list"


def test_the_target_is_strip_preview_pixel_for_pixel(tmp_path):
    ref = pattern_ref.build(INTRO, tmp_path / "p", aspect="9:16")
    recipe = rc.preset_42_default("scene_0")
    a = runner.target(FRAME, tmp_path / "a.png", ref, recipe, person=_person())
    b = pattern_ref.strip_preview(ref, FRAME, tmp_path / "b.png", person=_person())
    assert np.array_equal(np.asarray(Image.open(a)), np.asarray(Image.open(b)))


# ---------------------------------------------------------------------------
# Guards
# ---------------------------------------------------------------------------

def test_the_guards_follow_the_recipe():
    ref = pattern_ref.PatternRef(slug="grid", source="4.png", path=Path("x"), ground="#000000",
                                 ink="#ffffff", fell_back=False)
    g = gd.for_chunk(rc.Guards(), ref, entering=True)
    assert (g.speaker_front, g.clear_veil_sec, g.clear_bars) == (True, speaker_front.VEIL_GUARD_SEC, True)
    assert gd.for_chunk(rc.Guards(), ref, entering=False).clear_veil_sec == 0.0
    assert gd.for_chunk(rc.Guards(veil="off"), ref, entering=True).clear_veil_sec == 0.0
    assert gd.for_chunk(rc.Guards(bars="off"), ref, entering=False).clear_bars is False
    structure = dataclasses.replace(ref, slug="structure")
    assert gd.for_chunk(rc.Guards(), structure, entering=False).clear_bars is False
    assert gd.for_chunk(rc.Guards(bars="on"), structure, entering=False).clear_bars is True


# ---------------------------------------------------------------------------
# Stage keys and reuse
# ---------------------------------------------------------------------------

def test_a_key_follows_the_bytes_and_needs_its_files(tmp_path):
    a = tmp_path / "a.bin"
    a.write_bytes(b"one")
    k1 = st.key(chunk=a, prompt="p", take="1")
    assert k1 and k1 == st.key(prompt="p", take="1", chunk=a)
    assert st.key(chunk=a, prompt="p", take="2") != k1
    assert st.key(chunk=tmp_path / "missing.bin", prompt="p") is None
    b = tmp_path / "b.bin"
    b.write_bytes(b"one")
    assert st.key(chunk=b, prompt="p", take="1") == k1, "same bytes, same key"


def test_the_manifest_reuses_only_a_matching_step_still_on_disk(tmp_path):
    out = tmp_path / "raw.mp4"
    out.write_bytes(b"x")
    m = st.Manifest({"omni": {"c0": {"key": "k", "path": str(out)}}})
    assert m.reusable("omni", "c0", "k") == out
    assert m.reusable("omni", "c0", "other") is None
    assert m.reusable("omni", "c1", "k") is None
    assert m.reusable("omni", "c0", None) is None
    out.unlink()
    assert m.reusable("omni", "c0", "k") is None
    m.record("omni", "c0", "k", path=out, seconds=1.234)
    assert m.public() == {"omni": {"c0": {"key": "k", "reused": False, "seconds": 1.23,
                                          "path": str(out)}}}
    assert m.counts() == {"omni": [1, 0]}


# ---------------------------------------------------------------------------
# The runner
# ---------------------------------------------------------------------------

def test_the_harness_runs_for_a_recipe_or_the_flag(monkeypatch):
    monkeypatch.delenv("REBRAND_HARNESS", raising=False)
    assert not runner.enabled({})
    assert runner.enabled({"recipe": {"elements": []}})
    assert not runner.enabled({"recipe": {"elements": []}, "path": "legacy"})
    monkeypatch.setenv("REBRAND_HARNESS", "scene")
    assert runner.enabled({}) and not runner.enabled({"path": "legacy"})
    assert pipeline.normalize_options({"path": "legacy"})["path"] == "legacy"
    assert "path" not in pipeline.normalize_options({})


def test_the_opening_is_the_briefs_or_the_harnesss():
    brief = {"omni_scene_prompt": "EDIT it."}
    assert runner.head(None, brief) == "EDIT it."
    assert runner.head(rc.preset_42_default(), brief) == "EDIT it."
    own = rc.Recipe(director=rc.Director(head="harness", intensity="subtle",
                                         notes="keep it calm, no #ff0000 and no 42 mark"))
    text = runner.head(own, brief)
    assert text.startswith(hprompt.HARNESS_HEAD) and "quiet" in text
    assert "#ff0000" not in text and " 42 " not in text and "keep it calm" in text


@pytest.fixture
def scene_run(tmp_path, monkeypatch):
    """A harness scene run over REAL files with Omni faked: one chunk."""
    calls: list = []
    chunk = tmp_path / "chunk.mp4"
    chunk.write_bytes(b"chunk-bytes")

    def split_chunks(src, out_dir):
        return [chunk]

    def extract_frame(video, out, *, at):
        Path(out).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(FRAME, out)
        return Path(out)

    async def person_mask(frame):
        return _person()

    async def edit_video(video, prompt, *, aspect, out, images=None):
        calls.append(prompt)
        Path(out).write_bytes(b"omni-" + str(len(calls)).encode())
        return {"path": out}

    async def quiet(*a, **k):
        return None

    monkeypatch.setattr(media, "split_chunks", split_chunks)
    monkeypatch.setattr(media, "extract_frame", extract_frame)
    monkeypatch.setattr(person_matte, "person_mask", person_mask)
    monkeypatch.setattr(omni, "edit_video", edit_video)
    monkeypatch.setattr(media, "fit_video_to_audio", lambda raw, c, out, **kw: out)
    monkeypatch.setattr(speaker_front, "restore_speaker", lambda e, o, out, **kw: out)
    monkeypatch.setattr(store, "append_arm_step", quiet)
    monkeypatch.setattr(store, "patch_arm", quiet)

    ref = pattern_ref.build(INTRO, tmp_path / "p", aspect="9:16")
    seg = pipeline.Segment(kind="scene", index=0, source=chunk, is_still=False)
    ctx = pipeline.Ctx(exp_id=21, run_id=1, aspect="9:16", width=720, height=1280, work=tmp_path,
                       options={"take": "t1"}, run_ctx={}, intro=None, scenes=[seg], outro=None)
    ctx.brief = {"omni_scene_prompt": "EDIT it."}
    ctx.pattern_ref = ref
    ctx.recipe = rc.preset_42_default("scene_0")

    def run(out_dir: str, prior: dict | None = None) -> st.Manifest:
        m = st.Manifest(prior)
        asyncio.run(runner.edit_scene(ctx, "omni", seg, tmp_path / out_dir, "Scene 1", m))
        return m

    return {"run": run, "calls": calls, "ctx": ctx}


def test_an_unchanged_retry_reuses_omni(scene_run):
    first = scene_run["run"]("v1")
    assert len(scene_run["calls"]) == 1 and first.counts()["omni"] == [1, 0]
    again = scene_run["run"]("v2", first.public())
    assert len(scene_run["calls"]) == 1, "nothing changed: no second Omni call"
    assert again.counts()["omni"] == [0, 1]
    assert Path(again.stages["omni"]["c0"]["path"]).read_bytes() == b"omni-1"


def test_a_changed_prompt_or_a_new_take_calls_omni_again(scene_run):
    first = scene_run["run"]("v1")
    scene_run["ctx"].brief = {"omni_scene_prompt": "EDIT it differently."}
    scene_run["run"]("v2", first.public())
    assert len(scene_run["calls"]) == 2
    scene_run["ctx"].brief = {"omni_scene_prompt": "EDIT it."}
    scene_run["ctx"].options = {"take": "t2"}
    scene_run["run"]("v3", first.public())
    assert len(scene_run["calls"]) == 3


# ---------------------------------------------------------------------------
# The studio preview (no Omni) and its API
# ---------------------------------------------------------------------------

@pytest.fixture
def prepared(tmp_path, monkeypatch):
    monkeypatch.setattr(studio, "STUDIO_ROOT", tmp_path)
    work = tmp_path / "1" / "scene_0_abc"
    work.mkdir(parents=True)
    first = work / "c0_first.png"
    shutil.copyfile(FRAME, first)
    intro = pipeline.Segment(kind="intro", index=0, source=INTRO, is_still=False)
    seg = pipeline.Segment(kind="scene", index=0, source=first, is_still=False)
    ctx = pipeline.Ctx(exp_id=0, run_id=1, aspect="9:16", width=720, height=1280, work=work,
                       options={}, run_ctx={}, intro=intro, scenes=[seg], outro=None)
    ctx.target_seg = seg
    ctx.frames["th_first"] = first
    scene = studio.Scene(run_id=1, target="scene_0", aspect="9:16", width=720, height=1280,
                         work=work, ctx=ctx, chunks=[work / "c0.mp4"], firsts=[first], seconds=[8.0])

    async def prepare(run_id, target):
        return scene

    async def person_mask(frame):
        return _person()

    monkeypatch.setattr(studio, "prepare", prepare)
    monkeypatch.setattr(person_matte, "person_mask", person_mask)
    # The studio's masks are the local matte only, shared per frame.
    monkeypatch.setattr(person_matte, "local_mask", person_mask)
    monkeypatch.setattr(studio, "_MASK_TASKS", {})
    monkeypatch.setattr(studio, "_PREVIEWS", {})
    monkeypatch.setattr(studio, "_PREPARED", {})
    return scene


def test_the_preview_shows_the_approved_target_for_the_42_default(tmp_path, prepared):
    recipe = rc.preset_42_default("scene_0")
    body = asyncio.run(studio.preview(1, "scene_0", recipe))
    [c] = body["chunks"]
    assert c["entering"] and c["guards"]["clear_veil_sec"] == speaker_front.VEIL_GUARD_SEC
    target = studio.resolve_file(c["target_url"].split("/file/", 1)[1])
    ref = pattern_ref.build(INTRO, tmp_path / "p", aspect="9:16")
    want = pattern_ref.strip_preview(ref, FRAME, tmp_path / "want.png", person=_person())
    assert np.array_equal(np.asarray(Image.open(target)), np.asarray(Image.open(want)))
    assert c["prompt"] == hprompt.pattern_clause(ref, entrance=True).strip()
    assert body["pattern"]["slug"] == "structure" and "path" not in body["pattern"]
    assert body["seed"] == studio.default_seed(1, "scene_0") and body["opening"] == studio.BRIEF_PLACEHOLDER
    json.dumps(body)
    again = asyncio.run(studio.preview(1, "scene_0", recipe))
    assert again == json.loads(json.dumps(body, default=str)), "cached by what changes it"


def test_a_house_draft_previews_the_same_picture_and_generate_freezes_it(tmp_path, prepared):
    """Made in house the preview is the same picture (its place and colour),
    nothing goes to Omni, and Generate freezes exactly that place."""
    omni_body = asyncio.run(studio.preview(1, "scene_0", rc.preset_42_default("scene_0")))
    house = rc.preset_42_house("scene_0")
    body = asyncio.run(studio.preview(1, "scene_0", house))
    assert body["engine"] == "house" and body["omni_parts"] == []
    assert "engine" not in omni_body and omni_body["omni_parts"] == [0]
    # Its own preview (a house draft keys its engine: I9), the same picture —
    # with no scene clip here there is no design still, only the target.
    assert body["chunks"][0]["target_url"] != omni_body["chunks"][0]["target_url"]

    def pixels(url):
        return np.asarray(Image.open(studio.resolve_file(url.split("/file/", 1)[1])))

    assert np.array_equal(pixels(body["chunks"][0]["target_url"]), pixels(omni_body["chunks"][0]["target_url"]))
    assert "house" not in body and body["opening"] == "" and body["chunks"][0]["prompt"] == ""
    frozen = asyncio.run(studio.final_patterns(1, "scene_0", house))
    drawn = body["patterns"][0]
    assert frozen == {"pattern": {"layout": drawn["layout"], "colour": drawn["colour"].lower(),
                                  "ground": body["pattern"]["ground"].lower(), "ink": body["pattern"]["ink"].lower(),
                                  "engine_v": 1}}
    stored = pattern_el.materialise(house, frozen)
    assert rc.pattern_element(stored).props["resolved"]["layout"] == drawn["layout"]


def test_the_preview_moves_the_pattern_to_the_side_asked(tmp_path, prepared):
    recipe = rc.normalize(rc.Recipe(target="scene_0", elements=[
        _pattern(placement=rc.Placement(anchor="edge-right"))]))
    body = asyncio.run(studio.preview(1, "scene_0", recipe))
    assert body["pattern"]["layout"]["placement"] == "right"
    assert body["pattern"]["animation"] == "right_to_left"
    target = np.asarray(Image.open(studio.resolve_file(body["chunks"][0]["target_url"].split("/file/", 1)[1])))
    frame = np.asarray(Image.open(FRAME).convert("RGB"))
    changed = np.any(target != frame, axis=2)
    w = changed.shape[1]
    assert changed[:, w // 2:].sum() > 20 * changed[:, : w // 4].sum(), "drawn on the right"


def test_studio_files_never_leave_the_studio(tmp_path, prepared):
    from fastapi import HTTPException

    with pytest.raises(HTTPException):
        studio.resolve_file("../../etc/passwd.png")
    with pytest.raises(HTTPException):
        studio.resolve_file("1/scene_0_abc/nothing.png")
    assert studio.resolve_file("1/scene_0_abc/c0_first.png").name == "c0_first.png"


@pytest.fixture
def client(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.routers import rebrand_harness as rt

    async def list_patterns():
        return list(lib.BUILTINS.values())

    monkeypatch.setattr(lib, "list_patterns", list_patterns)
    app = FastAPI()
    app.include_router(rt.router)
    return TestClient(app)


def test_the_catalogue_lists_what_the_studio_offers(client):
    body = client.get("/api/rebrand/harness/catalogue").json()
    kinds = {k["kind"]: k for k in body["kinds"]}
    assert kinds["pattern"]["ready"] and not kinds["logo"]["ready"]
    assert [a["value"] for a in body["anchors"]] == list(rc.PATTERN_ANCHORS)
    # Every entrance our own renderer draws (new work is made in house).
    assert [e["value"] for e in body["entrances"]] == ["auto", *lib.ENTRANCES]
    assert [p["slug"] for p in body["patterns"]] == list(lib.BUILTINS)
    assert body["presets"][0]["id"] == "42-default"


def test_preview_and_generate_fill_the_scene_and_the_seed(client, monkeypatch):
    seen: dict = {}

    async def preview(run_id, target, recipe):
        seen["preview"] = recipe
        return {"ok": True}

    async def start(run_id, raw, recipe=None):
        seen["start"] = recipe
        return {"id": 5, "run_id": run_id, "options": {"target": recipe.target}}

    monkeypatch.setattr(studio, "preview", preview)
    monkeypatch.setattr(pipeline, "start", start)
    recipe = rc.preset_42_default().model_dump(mode="json")
    r = client.post("/api/rebrand/harness/preview", json={"run_id": 3, "target": "scene_2", "recipe": recipe})
    assert r.status_code == 200
    assert seen["preview"].target == "scene_2" and seen["preview"].seed == "studio:3:scene_2"
    r = client.post("/api/rebrand/harness/versions", json={"run_id": 3, "target": "scene_2", "recipe": recipe})
    assert r.status_code == 200 and r.json()["recipe"]["seed"] == "studio:3:scene_2"
    assert rc.canonical(seen["start"]) == rc.canonical(seen["preview"])


def test_a_bad_recipe_is_refused_with_its_reasons(client):
    r = client.post("/api/rebrand/harness/preview", json={
        "run_id": 3, "target": "scene_2",
        "recipe": {"elements": [{"id": "p", "kind": "pattern", "placement": {"anchor": "top-left"}}]}})
    assert r.status_code == 422
    assert r.json()["detail"]["errors"][0]["field"] == "placement.anchor"
    r = client.post("/api/rebrand/harness/preview", json={
        "run_id": 3, "target": "scene_2", "recipe": {"elements": [{"id": "p", "kind": "sparkles"}]}})
    assert r.status_code == 422
    r = client.post("/api/rebrand/harness/preview", json={"run_id": 3, "target": "reel", "recipe": {}})
    assert r.status_code == 422


def test_start_refuses_a_recipe_it_cannot_draw_yet(monkeypatch):
    from fastapi import HTTPException

    async def run_row(run_id):
        return {"metadata": {}}

    monkeypatch.setattr(pipeline.th, "_fetch_run_row", run_row)
    logo = rc.normalize(rc.Recipe(target="scene_0", director=rc.Director(head="harness"),
                                  elements=[rc.Element(id="logo", kind="logo")]))
    with pytest.raises(HTTPException) as err:
        asyncio.run(pipeline.start(1, None, recipe=logo))
    assert err.value.status_code == 400 and "logo is not available yet" in err.value.detail
    with pytest.raises(HTTPException) as err:
        asyncio.run(pipeline.start(1, None, recipe=rc.preset_42_default()))
    assert err.value.status_code == 400


def test_start_stores_the_recipe_beside_the_options_it_implies(monkeypatch):
    created: dict = {}

    async def run_row(run_id):
        return {"metadata": {}}

    async def none(*a, **k):
        return []

    async def get(slug):
        return lib.BUILTINS.get(slug)

    async def create(run_id, options):
        created.update(options)
        return {"id": 77}

    async def run(*a, **k):
        return None

    monkeypatch.setattr(pipeline.th, "_fetch_run_row", run_row)
    monkeypatch.setattr(omni, "providers", lambda: ["vertex"])
    monkeypatch.setattr(lib, "get", get)
    monkeypatch.setattr(store, "list_for_run", none)
    monkeypatch.setattr(store, "create_experiment", create)
    monkeypatch.setattr(pipeline, "_run", run)
    recipe = rc.normalize(rc.Recipe(target="scene_1", seed="studio:1:scene_1", elements=[
        _pattern(slug="grid", placement=rc.Placement(anchor="full"),
                 animation=rc.Animation(entrance="glitch"))]))

    async def go():
        exp = await pipeline.start(1, None, recipe=recipe)
        await asyncio.sleep(0)
        return exp

    assert asyncio.run(go())["id"] == 77
    assert created["target"] == "scene_1" and created["pattern"] == "grid"
    assert created["pattern_animation"] == "glitch" and created["run_omni"] is True
    assert rc.canonical(rc.normalize(created["recipe"])) == rc.canonical(recipe)
    assert runner.enabled(created)
