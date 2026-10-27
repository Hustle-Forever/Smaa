"""Rebrand studio v2, backend: a preview you can see working (M2), up to three
patterns in one scene (M3), and a finished version shown as its settled
first frames (M4). Omni, Gemini, the database and every paid call are faked;
the 42 default is held byte for byte by tests/test_rebrand_golden.py."""
from __future__ import annotations

import asyncio
import dataclasses
import json
import os
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from app.services import omni_client as omni
from app.services import pattern_library as lib
from app.services import pattern_planner
from app.services import person_matte
from app.services import rebrand_brief
from app.services import rebrand_media as media
from app.services import rebrand_pattern_ref as pattern_ref
from app.services import rebrand_pipeline as pipeline
from app.services import rebrand_store as store
from app.services import speaker_front
from app.services.rebrand_harness import base as hbase
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
COLOURS = pattern_ref.Colours(ground="#3d3c3d", ink="#f4f4f8", fell_back=False, frame=None,
                              colour="#2ed8e8")
cv2 = pytest.importorskip("cv2")


def _person() -> np.ndarray:
    return np.asarray(Image.open(PERSON).convert("L"), dtype="float32") / 255.0


def _pat(id_: str = "pattern", slug: str = "structure", anchor: str = "auto", **props) -> rc.Element:
    return rc.Element(id=id_, kind="pattern", placement=rc.Placement(anchor=anchor),
                      props={"slug": slug, "layout": None, "colour": "auto", **props})


def _recipe(*els: rc.Element, **kw) -> rc.Recipe:
    return rc.normalize(rc.Recipe(target="scene_0", seed="studio:1:scene_0", elements=list(els), **kw))


def _ref(slug: str, layout: pattern_ref.Layout, out: Path, animation: str = "left_to_right",
         colour: str = "#7f2bf2") -> pattern_ref.PatternRef:
    """A REAL rendered pattern (no intro dig: the colours are given)."""
    return pattern_ref.build(None, out, aspect="9:16", slug=slug, layout=layout, animation=animation,
                             colours=dataclasses.replace(COLOURS, colour=colour))


ROW_TOP_RIGHT = lib.BEADS_ROW_CANDIDATES[3]


# ---------------------------------------------------------------------------
# The studio (M2): a prepared scene, masks, parallel parts, one key
# ---------------------------------------------------------------------------

@pytest.fixture
def studio_env(tmp_path, monkeypatch):
    """A prepared scene of N parts over the golden frame, with the intro's
    colours already on disk (no dig) and the LOCAL mask counted. The paid
    mask must never be asked for."""
    seen: dict = {"local": [], "prepare": 0}
    monkeypatch.setattr(studio, "STUDIO_ROOT", tmp_path)
    monkeypatch.setattr(studio, "_PREPARED", {})
    monkeypatch.setattr(studio, "_MASK_TASKS", {})
    monkeypatch.setattr(studio, "_PREVIEWS", {})
    mask_value = {"mask": _person()}

    async def local_mask(frame):
        seen["local"].append(Path(frame).name)
        return mask_value["mask"]

    async def paid(frame):
        raise AssertionError("the studio asked for the paid (Gemini) mask")

    monkeypatch.setattr(person_matte, "local_mask", local_mask)
    monkeypatch.setattr(person_matte, "person_mask", paid)

    def make(parts: int = 1, *, index: int = 0) -> studio.Scene:
        work = tmp_path / "1" / f"scene_{index}_abc"
        work.mkdir(parents=True, exist_ok=True)
        firsts = []
        for j in range(parts):
            f = work / f"c{j}_first.png"
            shutil.copyfile(FRAME, f)
            firsts.append(f)
        (work / "colours.json").write_text(json.dumps({
            "ground": COLOURS.ground, "ink": COLOURS.ink, "fell_back": False, "frame": None,
            "colour": COLOURS.colour}), encoding="utf-8")
        intro = pipeline.Segment(kind="intro", index=0, source=INTRO, is_still=False)
        segs = [pipeline.Segment(kind="scene", index=i, source=firsts[0], is_still=False)
                for i in range(index + 1)]
        ctx = pipeline.Ctx(exp_id=0, run_id=1, aspect="9:16", width=720, height=1280, work=work,
                           options={}, run_ctx={}, intro=intro, scenes=segs, outro=None)
        ctx.target_seg = segs[index]
        if index == 0:
            ctx.frames["th_first"] = firsts[0]
        scene = studio.Scene(run_id=1, target=f"scene_{index}", aspect="9:16", width=720,
                             height=1280, work=work, ctx=ctx,
                             chunks=[work / f"c{j}.mp4" for j in range(parts)], firsts=firsts,
                             seconds=[8.0] * parts)

        async def prepare(run_id, target):
            seen["prepare"] += 1
            return scene

        monkeypatch.setattr(studio, "prepare", prepare)
        return scene

    seen["make"] = make
    seen["mask"] = mask_value
    return seen


def _target(body: dict, j: int = 0) -> np.ndarray:
    rel = body["chunks"][j]["target_url"].split("/file/", 1)[1]
    return np.asarray(Image.open(studio.resolve_file(rel)).convert("RGB"))


def test_a_prepared_scene_is_previewed_without_the_database(studio_env, monkeypatch):
    scene = studio_env["make"]()
    studio._PREPARED[(1, "scene_0")] = scene

    async def no_db(*a, **k):
        raise AssertionError("the database was asked")

    monkeypatch.setattr(pipeline.th, "_fetch_run_row", no_db)
    monkeypatch.setattr(pipeline, "_scenes", no_db)
    monkeypatch.setattr(studio, "prepare", no_db)
    body = asyncio.run(studio.preview(1, "scene_0", _recipe(_pat())))
    assert body["chunks"][0]["target_url"] and body["pattern"]["slug"] == "structure"


def test_the_body_says_what_it_did_and_what_omni_would_cost(studio_env):
    studio_env["make"](2)
    recipe = rc.preset_42_default("scene_0")
    body = asyncio.run(studio.preview(1, "scene_0", recipe))
    assert body["omni_parts"] == [0, 1]
    assert set(body["timings"]) == {"prepare", "colours", "pattern", "chunks", "total"}
    assert body["colours"] == {"intro": COLOURS.colour, "brand": pattern_ref.BRAND_GROUND,
                               "fell_back": False}
    [p] = body["patterns"]
    assert p["element_id"] == "pattern" and p["slug"] == "structure" and p["moved"] is None
    assert p["layout"] == body["pattern"]["layout"] and p["colour"] == body["pattern"]["colour"]
    box = p["box"]
    assert box["x"] == 0.0 and 0.3 < box["w"] <= 0.55 and box["h"] > 0.6, "the left half"
    assert body["recipe_hash"] == rc.recipe_hash(recipe)
    json.dumps(body)


def test_a_missing_speaker_mask_is_tried_once_and_warned(studio_env):
    scene = studio_env["make"]()
    studio_env["mask"]["mask"] = None
    for colour in ("auto", "#ff00dd"):
        body = asyncio.run(studio.preview(1, "scene_0", _recipe(_pat(colour=colour))))
        assert any("Couldn't find the speaker in part 1" in w for w in body["warnings"])
    assert studio_env["local"] == ["c0_first.png"], "a frame the matte cannot read is not retried"

    async def reopen():
        studio._warm(scene)                       # what prepare does on every open
        return str(scene.firsts[0]) in studio._MASK_TASKS

    assert asyncio.run(reopen()) is False, "reopening the studio forgets the miss"
    asyncio.run(studio.preview(1, "scene_0", _recipe(_pat(colour="#303030"))))
    assert studio_env["local"] == ["c0_first.png", "c0_first.png"]


def _saved() -> list[Path]:
    return list(Path(studio.STUDIO_ROOT).rglob("preview.json"))


def test_a_preview_without_the_speaker_is_not_kept_and_is_drawn_again(studio_env):
    """The 42 default's starting draft, drawn while the matte could not read
    the frame: served once, never stored — after a reopen that finds the
    speaker, the SAME recipe is drawn again, behind them, at a new address."""
    scene = studio_env["make"]()
    studio_env["mask"]["mask"] = None
    recipe = rc.preset_42_default("scene_0")
    first = asyncio.run(studio.preview(1, "scene_0", recipe))
    assert any("Couldn't find the speaker in part 1" in w for w in first["warnings"])
    assert first["kept"] is False and "?v=" in first["chunks"][0]["target_url"]
    assert _saved() == [], "not stored for the recipe"
    over = _target(first)

    studio_env["mask"]["mask"] = _person()

    async def reopen():
        studio._warm(scene)

    asyncio.run(reopen())
    again = asyncio.run(studio.preview(1, "scene_0", recipe))
    assert not any("speaker" in w for w in again["warnings"])
    assert again["kept"] is True and "?v=" not in again["chunks"][0]["target_url"]
    assert again["chunks"][0]["target_url"] != first["chunks"][0]["target_url"]
    behind = _target(again)
    on_speaker = _person() > 0.99
    assert (over[on_speaker] != behind[on_speaker]).any(), "the pattern was over the speaker"
    assert np.array_equal(behind[on_speaker], np.asarray(Image.open(FRAME).convert("RGB"))[on_speaker])
    assert len(_saved()) == 1
    assert asyncio.run(studio.preview(1, "scene_0", recipe))["chunks"] == again["chunks"], "kept now"


def test_a_pattern_drawn_in_place_of_another_is_not_kept(studio_env, monkeypatch):
    """The library could not be read while building (a pooler hiccup): the
    default was drawn instead. That is served, never stored."""
    studio_env["make"]()

    async def get(slug):
        return None if slug.startswith("u-") else lib.BUILTINS.get(slug)

    monkeypatch.setattr(lib, "get", get)
    body = asyncio.run(studio.preview(1, "scene_0", _recipe(_pat(slug="u-mine-ab12"))))
    assert body["kept"] is False and _saved() == []
    assert any("u-mine-ab12 not found" in w for w in body["warnings"])
    assert asyncio.run(studio.preview(1, "scene_0", _recipe(_pat())))["kept"] is True
    assert len(_saved()) == 1


def test_parts_render_in_parallel_and_in_order(studio_env, monkeypatch):
    studio_env["make"](3)
    lock = threading.Lock()
    live = {"now": 0, "max": 0}

    def target(frame, out, refs, recipe, *, person=None, alphas=None):
        with lock:
            live["now"] += 1
            live["max"] = max(live["max"], live["now"])
        time.sleep(0.3)
        with lock:
            live["now"] -= 1
        Path(out).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(frame, out)
        return Path(out)

    monkeypatch.setattr(runner, "target", target)
    body = asyncio.run(studio.preview(1, "scene_0", _recipe(_pat())))
    assert live["max"] > 1, "the parts are drawn at the same time"
    assert [c["index"] for c in body["chunks"]] == [0, 1, 2]
    assert [c["target_url"].rsplit("/", 1)[1] for c in body["chunks"]] == \
        ["c0_target.png", "c1_target.png", "c2_target.png"]


def test_two_requests_for_one_preview_share_the_work(studio_env, monkeypatch):
    studio_env["make"]()
    calls = []
    real = runner.target

    def target(*a, **kw):
        calls.append(1)
        time.sleep(0.2)
        return real(*a, **kw)

    monkeypatch.setattr(runner, "target", target)

    async def both():
        r = _recipe(_pat())
        return await asyncio.gather(studio.preview(1, "scene_0", r), studio.preview(1, "scene_0", r))

    a, b = asyncio.run(both())
    assert len(calls) == 1 and a == b
    saved = list(Path(studio.STUDIO_ROOT).rglob("preview.json"))
    assert len(saved) == 1 and not list(Path(studio.STUDIO_ROOT).rglob("*.tmp"))


def test_the_same_drawn_colour_is_one_preview(studio_env, monkeypatch):
    studio_env["make"]()
    calls = []
    real = runner.target
    monkeypatch.setattr(runner, "target", lambda *a, **kw: calls.append(1) or real(*a, **kw))
    recipes = [
        _recipe(_pat(), colours=rc.Colours(source="brand")),
        _recipe(_pat(), colours=rc.Colours(source="custom", custom="#7F2BF2")),
        _recipe(_pat(), colours=rc.Colours(source="brand", custom="#123456")),
        _recipe(_pat(colour="#7f2bf2")),
    ]
    bodies = [asyncio.run(studio.preview(1, "scene_0", r)) for r in recipes]
    assert len(calls) == 1, "one drawing for four ways of asking for 42 violet"
    assert len({b["chunks"][0]["target_url"] for b in bodies}) == 1
    assert [b["recipe_hash"] for b in bodies] == [rc.recipe_hash(r) for r in recipes]
    assert len({b["recipe_hash"] for b in bodies}) == 4


def test_the_preview_key_is_versioned_and_keys_what_shows(monkeypatch):
    r = _recipe(_pat())
    k = studio._preview_key(r, "s")
    monkeypatch.setattr(studio, "PREVIEW_VERSION", studio.PREVIEW_VERSION + 1)
    assert studio._preview_key(r, "s") != k
    monkeypatch.undo()
    noted = r.model_copy(update={"director": rc.Director(notes="calm")})
    assert studio._preview_key(noted, "s") == k, "the brief's placeholder does not show notes"
    harness = r.model_copy(update={"director": rc.Director(head="harness")})
    assert studio._preview_key(harness, "s") != k
    assert studio._preview_key(harness.model_copy(update={"director": rc.Director(
        head="harness", notes="calm")}), "s") != studio._preview_key(harness, "s")
    assert studio._preview_key(r, "s", base={"version_id": 3, "mode": "stack"}) != k


def test_the_plan_frame_is_extracted_once(studio_env, monkeypatch):
    studio_env["make"](index=1)
    extracted = []

    async def frame(video, out, at):
        extracted.append(Path(out).name)
        Path(out).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(FRAME, out)
        return Path(out)

    def choose(cands, frame_, person, *, seed, slug, **kw):
        return cands[0], {"mode": "auto", "seed": seed, "reason": "near-best",
                          "picked": {"index": 0}, "pool": [], "candidates": []}

    monkeypatch.setattr(pipeline, "_frame", frame)
    monkeypatch.setattr(pattern_planner, "choose", choose)
    for colour in ("auto", "#303030"):
        asyncio.run(studio.preview(1, "scene_1", _recipe(_pat(slug="beads_row", colour=colour))))
    assert extracted == ["plan_scene1_first.png"]


def test_warmup_reports_the_colours_and_the_masks(studio_env):
    scene = studio_env["make"](2)
    state = studio.warmup_state(scene)
    # The scene's analysis (every speaker frame, the camera) waits for the
    # masks; this scene has no clip to analyse.
    assert state == {"prepared": True, "colours": "ready", "masks": {"done": 0, "total": 2},
                     "speaker_frames": {"done": 0, "total": 0}, "camera": "waiting"}
    (scene.work / "colours.json").unlink()
    assert studio.warmup_state(scene)["colours"] == "working"
    for f in scene.firsts:
        Image.fromarray((_person() * 255).astype("uint8")).save(f.with_name(f"{f.stem}.person.png"))
    assert studio.warmup_state(scene)["masks"] == {"done": 2, "total": 2}


def test_the_scenes_of_one_run_share_one_dig_of_the_intro(studio_env, monkeypatch):
    first, second = studio_env["make"](index=0), studio_env["make"](index=1)
    for scene in (first, second):
        (scene.work / "colours.json").unlink()
    dug = []

    def dig(src, out, *, is_still=False):
        dug.append(Path(src).name)
        return COLOURS

    monkeypatch.setattr(pattern_ref, "dig_colours", dig)
    monkeypatch.setattr(studio, "_COLOUR_TASKS", {})
    got = [asyncio.run(studio._colours(s)) for s in (first, second)]
    assert dug == ["intro.mp4"], "the second scene reads the run's copy"
    assert got[0] == got[1] == COLOURS
    assert (second.work / "colours.json").exists()
    assert len(list(first.work.parent.glob("intro_*.colours.json"))) == 1

    # Another intro (a different file) is dug on its own.
    other = studio_env["make"](index=2)
    (other.work / "colours.json").unlink()
    other.ctx.intro = pipeline.Segment(kind="intro", index=0, source=FRAME, is_still=True)
    asyncio.run(studio._colours(other))
    assert dug == ["intro.mp4", "frame.png"]


def test_public_scene_has_the_original_video_and_duration(studio_env):
    scene = studio_env["make"](2)
    pub = studio.public_scene(scene)
    assert pub["video_url"] is None and pub["duration_sec"] == 16.0
    scene.ctx.target_seg.base = scene.work / "base" / "scene0.mp4"
    scene.ctx.target_seg.base.parent.mkdir(parents=True)
    scene.ctx.target_seg.base.write_bytes(b"mp4")
    assert studio.public_scene(scene)["video_url"].endswith("/scene_0_abc/base/scene0.mp4")


def test_capturing_alphas_changes_no_pixel(tmp_path):
    ref = _ref("structure", lib.BUILTINS["structure"].layout, tmp_path / "p")
    alphas: list = []
    a = runner.target(FRAME, tmp_path / "a.png", ref, rc.preset_42_default(), person=_person(),
                      alphas=alphas)
    b = runner.target(FRAME, tmp_path / "b.png", ref, rc.preset_42_default(), person=_person())
    assert a.read_bytes() == b.read_bytes()
    [one] = alphas
    assert (one["name"], one["z"], one["alpha"].shape) == ("pattern", "behind", (1280, 720))
    assert one["visible"].sum() < one["alpha"].sum(), "the speaker cuts it"


# ---------------------------------------------------------------------------
# Several patterns: building them (M3)
# ---------------------------------------------------------------------------

def _fake_ink(ref, width, height):
    a = np.zeros((height, width), np.float32)
    lay = ref.layout
    if lay.placement == "left":
        a[:, : width // 2] = 1.0
    elif lay.placement == "right":
        a[:, width // 2:] = 1.0
    elif lay.placement in ("top", "bottom"):
        rows = slice(0, height // 4) if lay.placement == "top" else slice(height - height // 4, height)
        cols = slice(0, width // 2) if lay.anchor == "left" else slice(width // 2, width)
        a[rows, cols] = 1.0
    else:
        a[:] = 1.0
    return a


@pytest.fixture
def many(tmp_path, monkeypatch):
    """build / build_all with the renderer, the colours and the planner
    faked. The fake planner takes the first candidate whose slot is free."""
    seen: dict = {"steps": [], "digs": 0, "builds": [], "chooses": []}

    def dig(intro, out_dir, *, is_still=False):
        seen["digs"] += 1
        return COLOURS

    def build(intro, out_dir, *, aspect, is_still=False, **kw):
        seen["builds"].append({"out_dir": Path(out_dir), **kw})
        c = kw.get("colours") or COLOURS
        return pattern_ref.PatternRef(slug=kw["slug"], source="x.png", path=tmp_path / "x.png",
                                      ground=c.ground, ink=c.ink, fell_back=False, colour=c.colour,
                                      where=pattern_ref.where_text(kw["layout"]),
                                      layout=kw["layout"], animation=kw["animation"],
                                      name=kw.get("name", ""))

    def choose(candidates, frame, person, *, seed, slug, mask=None, colour=None, work_dir=None,
               **several):
        seen["chooses"].append({"candidates": candidates, "seed": seed, **several})
        taken = set(several.get("taken") or ())
        pick = next((c for c in candidates if pattern_planner.slot(c) not in taken), candidates[0])
        return pick, {"mode": "auto", "seed": seed, "reason": "near-best",
                      "picked": {"index": candidates.index(pick)}, "pool": [], "candidates": []}

    async def person_mask(frame):
        return None

    seen["local"] = {"mask": None, "asked": 0}

    async def local_mask(frame):
        seen["local"]["asked"] += 1
        return seen["local"]["mask"]

    monkeypatch.setattr(pattern_ref, "dig_colours", dig)
    monkeypatch.setattr(pattern_ref, "build", build)
    monkeypatch.setattr(pattern_planner, "choose", choose)
    monkeypatch.setattr(pattern_planner, "summary", lambda report: "summary")
    monkeypatch.setattr(person_matte, "person_mask", person_mask)
    monkeypatch.setattr(person_matte, "local_mask", local_mask)
    monkeypatch.setattr(pattern_el, "_ink", _fake_ink)

    async def step(name, status="running", detail=""):
        seen["steps"].append((name, status, detail))

    async def plan_frame():
        return FRAME

    def job(seed="rebrand:9"):
        return pattern_el.PatternJob(exp_id=9, aspect="9:16", out_dir=tmp_path / "pattern",
                                     intro=INTRO, intro_is_still=False, seed=seed,
                                     plan_frame=plan_frame, step=step)

    seen["all"] = lambda els, colours=rc.Colours(), seed="rebrand:9": asyncio.run(
        pattern_el.build_all(els, colours, job(seed)))
    seen["one"] = lambda el, colours=rc.Colours(), seed="rebrand:9": asyncio.run(
        pattern_el.build(el, colours, job(seed)))
    seen["tmp"] = tmp_path
    return seen


def test_a_second_bead_field_moves_to_the_free_side_and_enters_from_it(many):
    a, b = many["all"]([_pat(), _pat("pattern-2", "beads_field")])
    assert a.ref.layout.placement == "left" and b.ref.layout.placement == "right"
    assert b.moved and "left strip was taken" in b.moved and "right strip" in b.moved
    assert b.ref.animation == "right_to_left", "it sweeps in from its own side"
    assert b.ref.layout.valign == "bottom", "the same curtain, mirrored"
    assert not many["chooses"], "each had one place left"


def test_an_auto_placed_second_pattern_plans_around_the_first(many):
    many["all"]([_pat(), _pat("pattern-2", "beads_row")])
    [plan] = many["chooses"]
    assert plan["seed"] == "rebrand:9:pattern-2:beads_row"
    assert plan["taken"] == ["left"] and plan["occupied"].shape == (1280, 720)
    assert plan["occupied"][:, :300].min() == 1.0, "the structure's ink is occupied"
    assert all(pattern_planner.slot(c) != "left" for c in plan["candidates"])


def test_the_first_pattern_keeps_the_approved_seed_and_path(many):
    a, b = many["all"]([_pat(slug="beads_row"), _pat("pattern-2", "beads_row")])
    first, second = many["chooses"]
    assert first["seed"] == "rebrand:9:beads_row" and "taken" not in first and "occupied" not in first
    assert second["seed"] == "rebrand:9:pattern-2:beads_row"
    assert a.slot != b.slot


def test_the_intro_is_dug_once_and_each_pattern_has_its_own_folder(many):
    many["all"]([_pat(slug="beads_row"), _pat("pattern-2", "beads_row"), _pat("pattern-3", "grid",
                                                                           anchor="edge-top")])
    assert many["digs"] == 1
    dirs = [b["out_dir"] for b in many["builds"]]
    tmp = many["tmp"]
    assert dirs == [tmp / "pattern", tmp / "pattern" / "pattern-2", tmp / "pattern" / "pattern-3"]


def test_an_explicit_place_is_kept_and_the_clash_is_named(many):
    a, b = many["all"]([_pat(), _pat("pattern-2", "beads_field", anchor="edge-left")])
    assert b.ref.layout.placement == "left" and b.moved is None
    assert b.warnings and "shares the left strip with Structure" in b.warnings[0]
    assert any("shares the left strip" in s[0] and s[1] == "failed" for s in many["steps"])


def test_the_overlap_is_measured_on_what_can_be_seen(many):
    """As the planner measures it: ink hidden behind the speaker is not an
    overlap (it was warned about while the planner let the place through).
    The mask is the local one — a warning never pays for a mask."""
    # Two strips meeting in the middle: the right one's inner edge is inside
    # the left one's grown footprint — right where the speaker stands.
    els = [_pat(), _pat("pattern-2", "beads_field", anchor="edge-right")]
    _, b = many["all"](els)
    assert b.overlap > pattern_planner.OVERLAP_MAX, "no speaker mask: all of its ink"
    assert b.warnings == ["Bead field overlaps Structure — try Another spot"]
    assert many["local"]["asked"] == 1
    behind = np.zeros((1280, 720), np.float32)
    behind[200:, 250:470] = 1.0                   # the speaker, in the middle
    many["local"]["mask"] = behind
    _, b = many["all"](els)
    assert b.overlap < pattern_planner.OVERLAP_MAX and b.warnings == []
    assert many["local"]["asked"] == 2, "once per build"
    rgb = np.asarray(Image.open(FRAME).convert("RGB"))
    assert np.array_equal(pattern_el._keep(FRAME, behind), pattern_planner.keep_grid(rgb, behind))
    assert np.array_equal(pattern_planner.frame_info(rgb, behind).keep, pattern_planner.keep_grid(rgb, behind))
    assert pattern_el._keep(FRAME, None) is None and pattern_el._keep(None, behind) is None


def test_a_pinned_place_is_the_only_place(many):
    pin = ROW_TOP_RIGHT.public()
    res = many["one"](_pat(slug="beads_row", layout=pin))
    assert res.ref.layout == ROW_TOP_RIGHT and not many["chooses"], "no planning: it is pinned"
    mirrored = dataclasses.replace(lib.BUILTINS["structure"].layout, placement="right").public()
    assert many["one"](_pat(layout=mirrored)).ref.layout.placement == "right", "a mirror is honoured"
    res = many["one"](_pat(layout={"placement": "full", "scale": 1.0, "reach": 1.0,
                                   "anchor": "left", "rotate": 0}))
    assert res.ref.layout.placement == "left"
    assert ("Structure's saved spot doesn't fit here, so it was placed again", "failed", "") \
        in many["steps"], "said to the owner as it is, once"
    many["steps"].clear()
    many["all"]([_pat(), _pat("pattern-2", "beads_row", layout={
        "placement": "full", "scale": 1.0, "reach": 1.0, "anchor": "left", "rotate": 0})])
    said = [s[0] for s in many["steps"] if s[1] == "failed"]
    assert "Bead row's saved spot doesn't fit here, so it was placed again" in said, \
        "several patterns: not named twice"


def test_a_pinned_upload_strip_or_the_grid_is_a_valid_pin():
    """The studio pins whatever the library drew (a colour change, a removed
    neighbour): every size the library draws must pass the recipe."""
    pins = [c.public() for e in lib.BUILTINS.values() for c in e.candidates]
    pins += [c.public() for c in lib.fit_layouts(3.0, ["left", "right", "top", "full"], run=0.1,
                                                 tiles=True, cell=50)]
    pins += [c.public() for c in lib.fit_layouts(2.0, ["left", "right", "top"], run=0.2,
                                                 dense_end="right")]
    assert max(p["scale"] for p in pins) > 4, "the sizes the old cap refused"
    for pin in pins:
        assert rc._layout_errors(pin) == [], pin
        rc.normalize(rc.Recipe(elements=[_pat(slug="structure", layout=pin)]))


def test_a_shuffle_moves_only_its_own_element(many):
    many["all"]([_pat(slug="beads_row"), _pat("pattern-2", "beads_row", shuffle="ab12")])
    first, second = many["chooses"]
    assert first["seed"] == "rebrand:9:beads_row"
    assert second["seed"] == "rebrand:9:pattern-2:beads_row:ab12"
    many["chooses"].clear()
    many["one"](_pat(slug="beads_row", shuffle="zz"))
    assert many["chooses"][0]["seed"] == "rebrand:9:beads_row:zz"


def test_adding_a_pattern_never_moves_the_ones_already_placed(many):
    els = [_pat(slug="beads_row"), _pat("pattern-2", "beads_row"), _pat("pattern-3", "beads_field")]
    two = [r.ref.layout for r in many["all"](els[:2])]
    three = [r.ref.layout for r in many["all"](els)]
    assert three[:2] == two


def test_the_library_rules_the_recipe_cannot_know(monkeypatch):
    async def lookup(slug):
        return lib.BUILTINS.get(slug)

    monkeypatch.setattr(lib, "lookup", lookup)
    check = lambda *els: asyncio.run(pattern_el.check(_recipe(*els)))  # noqa: E731
    assert check(_pat(slug="blueprint")) == [], "one full-screen pattern alone is fine"
    [e] = check(_pat(slug="blueprint"), _pat("pattern-2"))
    assert e["element_id"] == "pattern-2" and "Blueprint fills the whole frame" in e["message"]
    [e] = check(_pat(), _pat("pattern-2", "grid"))
    assert e["element_id"] == "pattern-2" and "goes alone" in e["message"]
    [e] = check(_pat(), _pat("pattern-2", "beads_row", anchor="full"))
    assert e["element_id"] == "pattern-2"
    [e] = check(_pat(), _pat("pattern-2", "u-gone-ab12"))
    assert (e["element_id"], e["field"]) == ("pattern-2", "props.slug")
    assert e["message"] == "That pattern isn't in the library any more — swap or remove it."
    assert check(_pat(), _pat("pattern-2", "beads_row"), _pat("pattern-3", "beads_field")) == []
    chain = [_pat("old-1"), _pat("old-2", "beads_row")]
    errs = asyncio.run(pattern_el.check(_recipe(_pat("pattern-3", "beads_row"),
                                                _pat("pattern-4", "beads_field")), chain))
    assert [e["element_id"] for e in errs] == ["pattern-4"]
    assert errs[0]["message"] == "A scene takes up to 3 patterns."


def test_a_library_that_cannot_be_read_is_try_again_not_no_such_pattern(monkeypatch):
    from fastapi import HTTPException

    async def uploads(slug=None):
        raise OSError("the pooler dropped the connection")

    monkeypatch.setattr(lib, "_uploads", uploads)
    assert asyncio.run(lib.get("u-mine-ab12")) is None, "a run still falls back"
    with pytest.raises(lib.LibraryUnavailable):
        asyncio.run(lib.lookup("u-mine-ab12"))
    assert asyncio.run(lib.lookup("structure")) is lib.BUILTINS["structure"], "no database asked"
    with pytest.raises(HTTPException) as err:
        asyncio.run(pattern_el.check(_recipe(_pat(), _pat("pattern-2", "u-mine-ab12"))))
    assert err.value.status_code == 503 and err.value.detail == pattern_el.LIBRARY_UNREACHABLE


def test_several_patterns_run_through_the_pipeline_build(many, monkeypatch):
    async def quiet(*a, **k):
        return None

    monkeypatch.setattr(store, "append_arm_step", quiet)
    ctx = pipeline.Ctx(exp_id=9, run_id=1, aspect="9:16", width=720, height=1280,
                       work=many["tmp"], options={}, run_ctx={}, intro=None, scenes=[], outro=None)
    ctx.recipe = _recipe(_pat(), _pat("pattern-2", "beads_row"))

    async def plan(_ctx):
        return FRAME

    monkeypatch.setattr(pipeline, "_plan_frame", plan)
    asyncio.run(pipeline._build_pattern_ref(ctx))
    assert [r.slug for r in ctx.pattern_refs] == ["structure", "beads_row"]
    assert ctx.pattern_ref is ctx.pattern_refs[0] and len(ctx.pattern_results) == 2
    assert runner.pattern_refs(ctx) == ctx.pattern_refs
    ctx.pattern_ref = ctx.pattern_refs[1]
    assert runner.pattern_refs(ctx) == [ctx.pattern_refs[1]], "a lone pattern_ref wins"


# ---------------------------------------------------------------------------
# What Omni is told about several patterns
# ---------------------------------------------------------------------------

def _stub(slug, layout, animation="left_to_right") -> pattern_ref.PatternRef:
    return pattern_ref.PatternRef(slug=slug, source="x.png", path=Path("x.png"), ground="#3d3c3d",
                                  ink="#f4f4f8", fell_back=False, colour="#2ed8e8",
                                  where=pattern_ref.where_text(layout), layout=layout,
                                  animation=animation)


def test_one_pattern_is_told_word_for_word_as_approved():
    head = "EDIT the clip; keep #0663a3 and '42 pattern' out."
    for entry in lib.BUILTINS.values():
        for lay in entry.candidates:
            ref = _stub(entry.slug, lay, lib.aimed_animation(entry.default_animation, lay))
            for entering in (True, False):
                want = hprompt.unpaintable(head) + hprompt.pattern_clause(ref, entrance=entering)
                assert hprompt.scene_prompt(head, [ref], entrance=entering) == want
                assert hprompt.scene_prompt(head, ref, entrance=entering) == want
                assert hprompt.has_entrance([ref]) == hprompt.has_entrance(ref)
    assert hprompt.scene_prompt(head, None) == hprompt.unpaintable(head)
    assert hprompt.scene_prompt(head, []) == hprompt.unpaintable(head)


def test_several_patterns_get_one_plural_clause_with_no_digits():
    structure = _stub("structure", lib.BUILTINS["structure"].layout)
    row = _stub("beads_row", ROW_TOP_RIGHT, "right_to_left")
    odd = _stub("upload", pattern_ref.Layout(placement="right", reach=0.35), "none")
    for refs, count in (([structure, row], "two"), ([structure, row, odd], "three")):
        for entering in (True, False):
            text = hprompt.patterns_clause(refs, entrance=entering)
            assert f"with {count} brand patterns" in text
            assert "#" not in text and not re.search(r"\d", text), text
            assert "behind the speaker, behind" not in text
            assert "never merge them" in text
            assert ("ENTRANCE:" in text) == entering
    text = hprompt.patterns_clause([structure, row, odd], entrance=True)
    assert "the first in the left half of the frame" in text
    assert "the second in a band along the top edge of the frame, from the top-right corner" in text
    assert "the third in a third of the frame along its right side" in text
    assert "the second pattern fades in smoothly from RIGHT to LEFT" in text
    assert "the third pattern is already there from the first frame" in text
    assert hprompt.has_entrance([odd]) is False and hprompt.has_entrance([odd, row]) is True
    hold = hprompt.patterns_clause([structure, row], entrance=False)
    assert "holds still throughout" in hold
    assert hprompt.scene_prompt("", [structure, row]).strip() == hold.strip()


def test_the_entrance_phrases_cover_every_animation():
    assert set(lib.ENTER_PHRASE) == set(lib.ANIMATIONS)
    assert lib.entrance_phrase("none") is None
    for phrase in filter(None, lib.ENTER_PHRASE.values()):
        assert "#" not in phrase and not re.search(r"\d", phrase)


def test_the_keep_and_icons_clauses_come_in_order_and_paint_nothing():
    ref = _stub("structure", lib.BUILTINS["structure"].layout)
    for clause in (hprompt.KEEP_CLAUSE, hprompt.ICONS_CLAUSE):
        assert "#" not in clause and not re.search(r"\d", clause)
    text = hprompt.scene_prompt("EDIT it.", ref, entrance=True, keep=True, no_icons=True)
    assert text == ("EDIT it. " + hprompt.KEEP_CLAUSE + hprompt.pattern_clause(ref, entrance=True)
                    + hprompt.ICONS_CLAUSE)


def test_a_brief_opening_that_names_the_wrong_place_gives_way():
    structure = _stub("structure", lib.BUILTINS["structure"].layout)
    row = _stub("beads_row", ROW_TOP_RIGHT)
    right = _stub("beads_row", lib.BEADS_ROW_CANDIDATES[1])
    two = [structure, row]
    assert hprompt.head_misplaces("EDIT it. Add graphics ONLY to the left half.", two)
    assert hprompt.head_misplaces("Add the brand graphics along the bottom edge.", two)
    assert hprompt.head_misplaces("EDIT this clip, keep the speaker as shot.", two) is None
    assert hprompt.head_misplaces("Graphics on the left side and the top edge.", two) is None
    assert hprompt.head_misplaces("Add graphics to the left half.", [structure, right]) is None


# ---------------------------------------------------------------------------
# Guards and the veil keep
# ---------------------------------------------------------------------------

def test_the_guards_for_several_patterns():
    structure = _stub("structure", lib.BUILTINS["structure"].layout)
    grid = _stub("grid", lib.BUILTINS["grid"].layout)
    g = gd.for_chunk(rc.Guards(), [structure, grid], entering=True, veil_keep=True)
    assert g.clear_bars and g.veil_keep and g.public()["veil_keep"] is True
    one = gd.for_chunk(rc.Guards(), [structure], entering=True)
    assert one == gd.for_chunk(rc.Guards(), structure, entering=True)
    assert one.public() == {"speaker_front": True, "clear_veil_sec": speaker_front.VEIL_GUARD_SEC,
                            "clear_bars": False}
    assert not gd.for_chunk(rc.Guards(), [structure, grid], entering=False, veil_keep=True).veil_keep
    assert gd.wants_veil_keep(rc.preset_42_default(), [structure]) is False
    assert gd.wants_veil_keep(_recipe(_pat(colour="#f4f4f8")), [structure]) is True
    assert gd.wants_veil_keep(_recipe(_pat(colour="#303030")), [structure]) is False
    assert gd.wants_veil_keep(_recipe(_pat(colour="#7f2bf2")), [structure]) is False
    assert gd.wants_veil_keep(_recipe(_pat(), _pat("pattern-2")), [structure, grid]) is True


def test_apply_passes_the_keep_only_when_there_is_one(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(speaker_front, "restore_speaker",
                        lambda e, o, out, **kw: calls.append(sorted(kw)) or out)
    g = gd.ChunkGuards(speaker_front=True, clear_veil_sec=3.0, clear_bars=False)
    gd.apply(tmp_path / "e", tmp_path / "o", tmp_path / "x", g)
    gd.apply(tmp_path / "e", tmp_path / "o", tmp_path / "x", g, veil_keep=np.ones((2, 2)))
    assert calls == [["clear_bars", "clear_veil_sec"], ["clear_bars", "clear_veil_sec", "veil_keep"]]


def _room(h=320, w=180, seed=3):
    rng = np.random.default_rng(seed)
    return cv2.GaussianBlur(rng.integers(40, 170, size=(h, w, 3)).astype(np.uint8), (0, 0), 2)


def _write_frames(path: Path, frames, fps=30) -> Path:
    h, w = frames[0].shape[:2]
    cmd = [media.ffmpeg_exe(), "-y", "-loglevel", "error", "-f", "rawvideo", "-pix_fmt", "bgr24",
           "-s", f"{w}x{h}", "-r", str(fps), "-i", "-", "-c:v", "libx264", "-crf", "0",
           "-preset", "ultrafast", "-pix_fmt", "yuv444p", str(path)]
    subprocess.run(cmd, input=b"".join(f.tobytes() for f in frames), check=True)
    return path


def _read_all(path: Path):
    cap = cv2.VideoCapture(str(path))
    out = []
    while True:
        ok, f = cap.read()
        if not ok:
            break
        out.append(f)
    cap.release()
    return out


def _whiten(img, rows, a):
    e = img.astype(np.float32).copy()
    e[rows] = e[rows] * (1 - a) + 245.0 * a
    return np.clip(e, 0, 255).astype(np.uint8)


def test_the_veil_keep_saves_a_white_band_and_still_clears_a_frosted_panel(tmp_path, monkeypatch):
    """A white pattern band fading in IS an even pull towards white: without
    the keep the veil guard erases it; inside the keep it stays, and a real
    frosted panel elsewhere is still cleared."""
    monkeypatch.setattr(speaker_front, "person_matte", lambda bgr: np.zeros(bgr.shape[:2], np.float32))
    room = _room()
    band, panel = slice(20, 80), slice(200, 300)
    omni_frames = [_whiten(_whiten(room, band, 0.4), panel, 0.3) for _ in range(6)]
    orig = _write_frames(tmp_path / "o.mp4", [room] * 6)
    edited = _write_frames(tmp_path / "e.mp4", omni_frames)
    keep = np.zeros(room.shape[:2], np.float32)
    keep[band] = 1.0
    erased = _read_all(speaker_front.restore_speaker(edited, orig, tmp_path / "a.mp4", clear_veil_sec=0.2))
    kept = _read_all(speaker_front.restore_speaker(edited, orig, tmp_path / "b.mp4", clear_veil_sec=0.2,
                                                   veil_keep=keep))
    diff = lambda f, rows: np.abs(f[rows, 10:170].astype(int) - room[rows, 10:170].astype(int)).mean()  # noqa: E731
    for i in range(6):
        assert diff(erased[i], slice(30, 70)) < 6, "no keep: the band is taken for a veil"
        assert diff(kept[i], slice(30, 70)) > 20, "kept: the white band survives"
        assert diff(kept[i], slice(215, 285)) < 6, "the frosted panel is still cleared"


# ---------------------------------------------------------------------------
# The target, the runner and the studio with two patterns
# ---------------------------------------------------------------------------

def test_the_target_lays_in_both_patterns_behind_the_speaker(tmp_path):
    refs = [_ref("structure", lib.BUILTINS["structure"].layout, tmp_path / "a"),
            _ref("beads_row", ROW_TOP_RIGHT, tmp_path / "b", "right_to_left", "#ff8800")]
    recipe = _recipe(_pat(), _pat("pattern-2", "beads_row"))
    layers = runner.layers(refs, recipe)
    assert [l.clears_bars for l in layers] == [True, True], "any asks: all clear the bars"
    assert [l.name for l in layers] == ["pattern", "pattern-2"]
    person = _person()
    out = np.asarray(Image.open(runner.target(FRAME, tmp_path / "t.png", refs, recipe, person=person)))
    frame = np.asarray(Image.open(FRAME).convert("RGB"))
    changed = np.any(out != frame, axis=2)
    h, w = changed.shape
    assert changed[h // 3:, : w // 4].mean() > 0.02, "the structure on the left"
    orange = (out[..., 0] > 200) & (out[..., 1] > 100) & (out[..., 1] < 170) & (out[..., 2] < 60)
    assert orange[: h // 3, w // 2:].sum() > 500, "the bead row in the top-right band"
    assert not changed[person > 0.99].any(), "never on the speaker"
    one = runner.target(FRAME, tmp_path / "one.png", refs[0], rc.preset_42_default(), person=person)
    want = pattern_ref.strip_preview(refs[0], FRAME, tmp_path / "want.png", person=person)
    assert one.read_bytes() == want.read_bytes()


@pytest.fixture
def two_run(tmp_path, monkeypatch):
    """A harness scene run of two parts with two patterns, Omni faked."""
    calls: list = []
    restores: list = []
    chunks = [tmp_path / "c0.mp4", tmp_path / "c1.mp4"]
    for i, c in enumerate(chunks):
        c.write_bytes(b"chunk-" + str(i).encode())

    def extract_frame(video, out, *, at):
        Path(out).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(FRAME, out)
        return Path(out)

    async def person_mask(frame):
        return _person()

    async def edit_video(video, prompt, *, aspect, out, images=None):
        calls.append({"video": Path(video).name, "prompt": prompt, "images": list(images or [])})
        Path(out).write_bytes(b"omni-" + str(len(calls)).encode())
        return {"path": out}

    def restore(e, o, out, **kw):
        restores.append({"original": Path(o).name, **kw})
        return out

    async def quiet(*a, **k):
        return None

    monkeypatch.setattr(media, "split_chunks", lambda src, out_dir: chunks)
    monkeypatch.setattr(media, "extract_frame", extract_frame)
    monkeypatch.setattr(person_matte, "person_mask", person_mask)
    monkeypatch.setattr(omni, "edit_video", edit_video)
    monkeypatch.setattr(media, "fit_video_to_audio", lambda raw, c, out, **kw: out)
    monkeypatch.setattr(speaker_front, "restore_speaker", restore)
    monkeypatch.setattr(media, "concat", lambda parts, out: out)
    monkeypatch.setattr(store, "append_arm_step", quiet)
    monkeypatch.setattr(store, "patch_arm", quiet)
    seg = pipeline.Segment(kind="scene", index=0, source=chunks[0], is_still=False)
    ctx = pipeline.Ctx(exp_id=21, run_id=1, aspect="9:16", width=720, height=1280, work=tmp_path,
                       options={"take": "t1"}, run_ctx={}, intro=None, scenes=[seg], outro=None)
    ctx.brief = {"omni_scene_prompt": "EDIT it."}
    ctx.pattern_refs = [_ref("structure", lib.BUILTINS["structure"].layout, tmp_path / "a"),
                        _ref("beads_row", ROW_TOP_RIGHT, tmp_path / "b", "right_to_left")]
    ctx.pattern_ref = ctx.pattern_refs[0]
    ctx.recipe = _recipe(_pat(), _pat("pattern-2", "beads_row"))

    def run(out_dir: str, prior=None) -> st.Manifest:
        m = st.Manifest(prior)
        asyncio.run(runner.edit_scene(ctx, "omni", seg, tmp_path / out_dir, "Scene 1", m))
        return m

    return {"run": run, "calls": calls, "restores": restores, "ctx": ctx}


def test_two_patterns_are_one_omni_image_per_part(two_run):
    m = two_run["run"]("v1")
    calls = two_run["calls"]
    assert [c["video"] for c in calls] == ["c0.mp4", "c1.mp4"]
    assert all(len(c["images"]) == 1 and c["images"][0].name.endswith("_target.png") for c in calls)
    refs = two_run["ctx"].pattern_refs
    assert calls[0]["prompt"] == "EDIT it." + hprompt.patterns_clause(refs, entrance=True)
    assert calls[1]["prompt"] == "EDIT it." + hprompt.patterns_clause(refs, entrance=False)
    guards = m.public()["guards"]
    assert [guards[c]["clear_veil_sec"] for c in ("c0", "c1")] == [3.0, 0.0]
    assert guards["c0"]["veil_keep"] is True and "veil_keep" not in guards["c1"]
    assert guards["c0"]["path"].endswith("scene_1_c0_front.mp4")
    first, second = two_run["restores"]
    assert isinstance(first["veil_keep"], np.ndarray) and first["veil_keep"].shape == (1280, 720)
    assert "veil_keep" not in second and first["clear_bars"] is True
    two_run["run"]("v2", m.public())
    assert len(calls) == 2, "an unchanged retry reuses both parts"
    two_run["ctx"].pattern_refs = two_run["ctx"].pattern_refs[:1]
    two_run["run"]("v3", m.public())
    assert len(calls) == 4, "a changed target is a new Omni call"


def test_the_studio_previews_two_patterns_in_separate_places(studio_env):
    studio_env["make"]()
    recipe = _recipe(_pat(), _pat("pattern-2", "beads_row", colour="#ff8800"))
    body = asyncio.run(studio.preview(1, "scene_0", recipe))
    a, b = body["patterns"]
    assert (a["element_id"], b["element_id"]) == ("pattern", "pattern-2")
    assert a["slot"] != b["slot"] and b["layout"]["placement"] != "left"
    assert body["pattern"]["slug"] == a["slug"] and body["pattern"]["layout"] == a["layout"]
    assert a["box"] and b["box"]
    assert body["chunks"][0]["guards"]["veil_keep"] is True
    assert "with two brand patterns" in body["chunks"][0]["prompt"]
    tgt = _target(body)
    orange = (tgt[..., 0] > 200) & (tgt[..., 1] > 100) & (tgt[..., 1] < 170) & (tgt[..., 2] < 60)
    x, y, w, h = (b["box"][k] for k in ("x", "y", "w", "h"))
    H, W = orange.shape
    assert orange[int(y * H):int((y + h) * H), int(x * W):int((x + w) * W)].sum() > 300
    assert orange.sum() < 1.2 * orange[int(y * H):int((y + h) * H), int(x * W):int((x + w) * W)].sum() + 50
    other = _recipe(_pat(), _pat("pattern-2", "beads_row", colour="#00ff88"))
    again = asyncio.run(studio.preview(1, "scene_0", other))
    assert again["chunks"][0]["target_url"] != body["chunks"][0]["target_url"]
    assert again["patterns"][0]["layout"] == a["layout"], "recolouring the second moves nothing"


def test_the_studio_warns_about_a_clash_it_was_asked_for(studio_env):
    studio_env["make"]()
    body = asyncio.run(studio.preview(1, "scene_0", _recipe(
        _pat(), _pat("pattern-2", "beads_field", anchor="edge-left"))))
    assert any("shares the left strip with Structure" in w for w in body["warnings"])
    assert body["patterns"][1]["layout"]["placement"] == "left"


# ---------------------------------------------------------------------------
# The API
# ---------------------------------------------------------------------------

@pytest.fixture
def client(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.routers import rebrand_harness as rt

    async def list_patterns():
        return list(lib.BUILTINS.values())

    async def get(slug):
        return lib.BUILTINS.get(slug)

    monkeypatch.setattr(lib, "list_patterns", list_patterns)
    monkeypatch.setattr(lib, "get", get)
    monkeypatch.setattr(lib, "lookup", get)
    app = FastAPI()
    app.include_router(rt.router)
    return TestClient(app)


def _el_json(id_, slug="structure", anchor="auto"):
    return {"id": id_, "kind": "pattern", "placement": {"anchor": anchor},
            "props": {"slug": slug, "layout": None, "colour": "auto"}}


def test_the_catalogue_offers_swatches_and_the_pattern_limit(client):
    body = client.get("/api/rebrand/harness/catalogue").json()
    assert body["limits"]["patterns"] == 3 and body["brand_colour"] == "#7f2bf2"
    assert body["swatches"] == [
        {"value": "auto", "label": "Auto"}, {"value": "#7f2bf2", "label": "42 violet"},
        {"value": "#8d01fa", "label": "42 purple"}, {"value": "#303030", "label": "Charcoal"},
        {"value": "#f4f4f8", "label": "White"}]


def test_too_many_or_a_full_screen_one_beside_another_is_422(client, monkeypatch):
    async def preview(run_id, target, recipe):
        return {"ok": True}

    monkeypatch.setattr(studio, "preview", preview)
    url = "/api/rebrand/harness/preview"
    four = [_el_json(f"p{i}") for i in range(4)]
    r = client.post(url, json={"run_id": 3, "target": "scene_2", "recipe": {"elements": four}})
    assert r.status_code == 422 and r.json()["detail"]["errors"][0]["element_id"] == "p3"
    assert r.json()["detail"]["errors"][0]["message"] == "A scene takes up to 3 patterns."
    full = [_el_json("pattern", "blueprint"), _el_json("pattern-2", "beads_row")]
    for path in (url, "/api/rebrand/harness/versions"):
        r = client.post(path, json={"run_id": 3, "target": "scene_2", "recipe": {"elements": full}})
        assert r.status_code == 422, path
        assert r.json()["detail"]["errors"][0]["element_id"] == "pattern-2"
    bad = client.post(url, json={"run_id": 3, "target": "scene_2", "recipe": {"elements": [
        {**_el_json("pattern"), "props": {"slug": "structure", "colour": "violet"}}]}})
    assert bad.status_code == 422 and bad.json()["detail"]["errors"][0]["field"] == "props.colour"
    assert bad.json()["detail"]["errors"][0]["message"] == "That colour isn't one we can use — pick another."
    ok =client.post(url, json={"run_id": 3, "target": "scene_2", "recipe": {"elements": [
        _el_json("pattern"), _el_json("pattern-2", "beads_row")]}})
    assert ok.status_code == 200

    async def unreachable(slug):
        raise lib.LibraryUnavailable("pooler")

    monkeypatch.setattr(lib, "lookup", unreachable)
    down = client.post(url, json={"run_id": 3, "target": "scene_2", "recipe": {"elements": [
        _el_json("pattern"), _el_json("pattern-2", "u-mine-ab12")]}})
    assert down.status_code == 503 and down.json()["detail"] == pattern_el.LIBRARY_UNREACHABLE


def test_warmup_says_how_far_the_scene_got(client, studio_env):
    r = client.get("/api/rebrand/harness/warmup", params={"run_id": 1, "target": "scene_0"}).json()
    assert r == {"prepared": False, "colours": "working", "masks": {"done": 0, "total": 0}}
    studio._PREPARED[(1, "scene_0")] = studio_env["make"]()
    r = client.get("/api/rebrand/harness/warmup", params={"run_id": 1, "target": "scene_0"}).json()
    assert r["prepared"] is True and r["colours"] == "ready" and r["masks"]["total"] == 1


def test_start_checks_every_pattern(monkeypatch):
    from fastapi import HTTPException

    async def run_row(run_id):
        return {"metadata": {}}

    async def get(slug):
        return lib.BUILTINS.get(slug)

    monkeypatch.setattr(pipeline.th, "_fetch_run_row", run_row)
    monkeypatch.setattr(omni, "providers", lambda: ["vertex"])
    monkeypatch.setattr(lib, "get", get)
    monkeypatch.setattr(lib, "lookup", get)
    with pytest.raises(HTTPException) as err:
        asyncio.run(pipeline.start(1, None, recipe=_recipe(_pat(), _pat("pattern-2", "nope"))))
    assert err.value.status_code == 422 and err.value.detail["errors"][0]["element_id"] == "pattern-2"


def test_start_stores_every_slug_past_normalize_options(monkeypatch):
    created: dict = {}

    async def run_row(run_id):
        return {"metadata": {}}

    async def none(*a, **k):
        return []

    async def get(slug):
        return lib.BUILTINS.get(slug)

    async def create(run_id, options):
        created.update(options)
        return {"id": 78}

    async def run(*a, **k):
        return None

    monkeypatch.setattr(pipeline.th, "_fetch_run_row", run_row)
    monkeypatch.setattr(omni, "providers", lambda: ["vertex"])
    monkeypatch.setattr(lib, "get", get)
    monkeypatch.setattr(store, "list_for_run", none)
    monkeypatch.setattr(store, "create_experiment", create)
    monkeypatch.setattr(pipeline, "_run", run)

    async def go():
        exp = await pipeline.start(1, None, recipe=_recipe(_pat(), _pat("pattern-2", "beads_row")))
        await asyncio.sleep(0)
        return exp

    asyncio.run(go())
    assert created["pattern"] == "structure" and created["patterns"] == ["structure", "beads_row"]
    assert len(created["recipe"]["elements"]) == 2


# ---------------------------------------------------------------------------
# The brief with several patterns
# ---------------------------------------------------------------------------

@pytest.fixture
def brief_call(monkeypatch):
    from app.agents.producer import memory
    from app.services import yas_edit_recipes

    seen: dict = {}

    async def empty(*a, **k):
        return []

    async def reqs():
        return {}

    async def multimodal(prompt, *, images, **kw):
        seen["prompt"], seen["images"] = prompt, list(images)
        return {"omni_scene_prompt": "EDIT it."}

    monkeypatch.setattr(rebrand_brief, "gemini_embed", empty)
    monkeypatch.setattr(memory, "build_brand_requirements", reqs)
    monkeypatch.setattr(rebrand_brief, "_ig_rows", empty)
    monkeypatch.setattr(rebrand_brief, "_ig_frames", empty)
    monkeypatch.setattr(yas_edit_recipes, "nearest_recipes", empty)
    monkeypatch.setattr(store, "past_lessons", empty)
    monkeypatch.setattr(rebrand_brief, "gemini_multimodal", multimodal)
    return seen


def test_the_brief_sees_every_pattern_only_when_there_are_several(brief_call, tmp_path):
    first = _ref("structure", lib.BUILTINS["structure"].layout, tmp_path / "a")
    second = _ref("beads_row", ROW_TOP_RIGHT, tmp_path / "b", "right_to_left")
    kw = dict(exp_id=1, run_ctx={"topic": "t"}, frames={}, logo=None, options={"target": "scene_0"},
              pattern_ref=first)
    asyncio.run(rebrand_brief.build_brief(**kw))
    alone = (brief_call["prompt"], brief_call["images"])
    asyncio.run(rebrand_brief.build_brief(**kw, extra_patterns=()))
    assert (brief_call["prompt"], brief_call["images"]) == alone, "no extras: the approved call"
    asyncio.run(rebrand_brief.build_brief(**kw, extra_patterns=[second]))
    assert brief_call["images"] == alone[1] + [second.path]
    assert "another brand pattern (beads_row)" in brief_call["prompt"]
    assert "must name every pattern in its own place" in brief_call["prompt"]


# ---------------------------------------------------------------------------
# A finished version as its settled first frames (M4)
# ---------------------------------------------------------------------------

GREEN, RED, BLUE = (40, 160, 40), (40, 40, 220), (220, 60, 30)     # BGR
SW, SH, FPS = 160, 288, 30


def _speaker_box(i):
    x0 = 20 + 2 * i
    return x0, 150, x0 + 40, 280


def _version_frame(i: int) -> np.ndarray:
    """A green room, a blue graphic fading in over the first second (the
    entrance), a red speaker walking right."""
    f = np.zeros((SH, SW, 3), np.float32)
    f[:] = GREEN
    a = min(1.0, i / FPS)
    f[10:60, :] = f[10:60, :] * (1 - a) + np.array(BLUE, np.float32) * a
    x0, y0, x1, y1 = _speaker_box(i)
    f[y0:y1, x0:x1] = RED
    return np.clip(np.rint(f), 0, 255).astype(np.uint8)


def _red(bgr):
    b, g, r = (bgr[:, :, k].astype(int) for k in range(3))
    return ((r > 150) & (g < 110) & (b < 110)).astype(np.float32)


def test_the_settled_first_frame_keeps_the_first_pose_and_the_settled_graphics(tmp_path):
    clip = _write_frames(tmp_path / "v.mp4", [_version_frame(i) for i in range(3 * FPS)], FPS)
    out = hbase.settled_first(clip, tmp_path / "s.png", entering=True, matte=_red)
    f = cv2.imread(str(out)).astype(int)
    x0, y0, x1, y1 = _speaker_box(0)
    assert (np.abs(f[y0 + 3:y1 - 3, x0 + 3:x1 - 3] - np.array(RED)).sum(axis=2) < 40).mean() > 0.98
    assert (np.abs(f[15:55, 10:150] - np.array(BLUE)).sum(axis=2) < 40).mean() > 0.98, "settled"
    sx0, _, sx1, _ = _speaker_box(45)                     # where the speaker stood at t_s
    ghost = f[y0 + 3:y1 - 3, max(x1 + 3, sx0):sx1 - 3]
    assert ghost.size and (np.abs(ghost - np.array(GREEN)).sum(axis=2) < 40).mean() > 0.98, \
        "no second speaker where they had walked to"
    plain = hbase.settled_first(clip, tmp_path / "p.png", entering=False, matte=_red)
    assert np.abs(cv2.imread(str(plain)).astype(int) - _version_frame(0).astype(int)).mean() < 2


def _exp(tmp_path: Path, **omni_arm) -> dict:
    return {"id": 501, "run_id": 1, "status": "ready", "options": {"target": "scene_0"},
            "arms": {"omni": {"status": "ready", **omni_arm}}}


def test_a_versions_parts_come_from_its_manifest_its_files_or_its_final(tmp_path, monkeypatch):
    monkeypatch.setattr(pipeline, "REBRAND_ROOT", tmp_path)
    run_dir = tmp_path / "501" / "omni"
    run_dir.mkdir(parents=True)
    elsewhere = tmp_path / "keep" / "part0.mp4"
    elsewhere.parent.mkdir()
    elsewhere.write_bytes(b"0")
    (run_dir / "scene_1_c1_front.mp4").write_bytes(b"1")
    stages = {"omni": {"c0": {"entering": True}, "c1": {"entering": False}},
              "guards": {"c0": {"path": str(elsewhere)}, "c1": {"path": str(tmp_path / "gone.mp4")}}}
    exp = _exp(tmp_path, stages=stages)
    assert hbase.part_count(exp) == 2 and hbase.entering(exp, 0) and not hbase.entering(exp, 1)
    assert asyncio.run(hbase.chunk_sources(exp)) == [elsewhere, run_dir / "scene_1_c1_front.mp4"]
    # Nothing per part left: cut from the final, split into equal parts.
    (run_dir / "scene_1_c1_front.mp4").unlink()
    final = _write_frames(tmp_path / "final.mp4", [_version_frame(i) for i in range(2 * FPS)], FPS)
    exp = _exp(tmp_path, stages={"omni": {"c0": {}, "c1": {}}}, final_path=str(final))
    parts = asyncio.run(hbase.chunk_sources(exp))
    assert [p.name for p in parts] == ["c0_src.mp4", "c1_src.mp4"]
    assert all(abs((media.probe_duration(p) or 0) - 1.0) < 0.1 for p in parts)
    assert hbase.still_urls(exp) == ["/api/rebrand/harness/versions/501/still/0",
                                     "/api/rebrand/harness/versions/501/still/1"]
    assert hbase.still_urls({**exp, "status": "running"}) == []


def test_the_still_route_serves_a_ready_versions_part(tmp_path, monkeypatch, client):
    monkeypatch.setattr(pipeline, "REBRAND_ROOT", tmp_path)
    monkeypatch.setattr(speaker_front, "person_matte", _red)
    run_dir = tmp_path / "501" / "omni"
    run_dir.mkdir(parents=True)
    _write_frames(run_dir / "scene_1_c0_front.mp4", [_version_frame(i) for i in range(3 * FPS)], FPS)
    rows = {501: _exp(tmp_path, stages={"omni": {"c0": {"entering": True}}})}

    async def get(exp_id):
        return rows.get(exp_id)

    monkeypatch.setattr(store, "get_experiment", get)
    ready = rows[501]
    rows[501] = {**ready, "status": "running"}
    r = client.get("/api/rebrand/harness/versions/501/still/0")
    assert r.status_code == 404 and "not ready" in r.json()["detail"]
    assert not (tmp_path / "501" / "stills").exists()
    rows[501] = ready
    r = client.get("/api/rebrand/harness/versions/501/still/0")
    assert r.status_code == 200 and r.headers["content-type"] == "image/png"
    assert (tmp_path / "501" / "stills" / "c0.png").exists(), "kept in the version's folder"
    assert client.get("/api/rebrand/harness/versions/501/still/1").status_code == 404
    assert client.get("/api/rebrand/harness/versions/999/still/0").status_code == 404
    assert client.get("/api/rebrand/harness/versions/501/still/-1").status_code == 404


def test_a_made_still_is_served_without_the_database(tmp_path, monkeypatch, client):
    monkeypatch.setattr(pipeline, "REBRAND_ROOT", tmp_path)
    made = hbase.still_path(501, 0)
    made.parent.mkdir(parents=True)
    Image.new("RGB", (4, 4), (9, 9, 9)).save(made)

    async def no_db(exp_id):
        raise AssertionError("the database was asked")

    monkeypatch.setattr(store, "get_experiment", no_db)
    r = client.get("/api/rebrand/harness/versions/501/still/0")
    assert r.status_code == 200 and r.content == made.read_bytes()
    assert r.headers["cache-control"] == "private, max-age=86400"


def test_the_stills_of_one_version_share_its_parts_and_take_turns(tmp_path, monkeypatch):
    """Every still of a version asked at once (the filmstrip's and the
    stage's): its parts are cut out of the final once, into one file each,
    and at most STILL_SLOTS stills are drawn at a time."""
    monkeypatch.setattr(pipeline, "REBRAND_ROOT", tmp_path)
    monkeypatch.setattr(speaker_front, "person_matte", _red)
    final = _write_frames(tmp_path / "final.mp4", [_version_frame(i) for i in range(3 * FPS)], FPS)
    exp = _exp(tmp_path, stages={"omni": {"c0": {}, "c1": {}, "c2": {}}}, final_path=str(final))
    cuts: list = []
    real_cut, real_settled = hbase._cut, hbase.settled_first
    lock = threading.Lock()
    live = {"now": 0, "max": 0}

    def cut(src, out, *, start, seconds):
        cuts.append(Path(out).name)
        time.sleep(0.1)
        return real_cut(src, out, start=start, seconds=seconds)

    def settled(clip, out, **kw):
        with lock:
            live["now"] += 1
            live["max"] = max(live["max"], live["now"])
        time.sleep(0.2)
        with lock:
            live["now"] -= 1
        return real_settled(clip, out, **kw)

    monkeypatch.setattr(hbase, "_cut", cut)
    monkeypatch.setattr(hbase, "settled_first", settled)

    async def all_at_once():
        return await asyncio.gather(*(hbase.still(exp, j) for j in (0, 1, 2, 0, 1)))

    got = asyncio.run(all_at_once())
    assert sorted(cuts) == ["c0_src.mp4", "c1_src.mp4", "c2_src.mp4"], "each part cut once"
    assert live["max"] <= hbase.STILL_SLOTS
    assert [p.name for p in got] == ["c0.png", "c1.png", "c2.png", "c0.png", "c1.png"]
    assert all(p.exists() for p in got) and not hbase._SOURCES


def test_the_versions_list_carries_the_stills(client, monkeypatch, tmp_path):
    monkeypatch.setattr(pipeline, "REBRAND_ROOT", tmp_path)
    ready = _exp(tmp_path, stages={"omni": {"c0": {}, "c1": {}}})
    running = {**_exp(tmp_path), "id": 502, "status": "running"}

    async def rows(run_id, limit=20, target=None):
        return [running, ready]

    async def same(e, live_ids):
        return e

    async def run_row(run_id):
        return {"metadata": {}}

    monkeypatch.setattr(store, "list_for_run", rows)
    monkeypatch.setattr(store, "mark_interrupted_if_orphaned", same)
    monkeypatch.setattr(pipeline.th, "_fetch_run_row", run_row)
    body = client.get("/api/rebrand/harness/versions", params={"run_id": 1, "target": "scene_0"}).json()
    assert [v["stills"] for v in body["versions"]] == [
        [], ["/api/rebrand/harness/versions/501/still/0", "/api/rebrand/harness/versions/501/still/1"]]


# ---------------------------------------------------------------------------
# Kept within bounds: preview folders on disk, versions built on in memory
# ---------------------------------------------------------------------------

def test_old_previews_and_the_alphas_only_they_named_are_pruned(tmp_path, monkeypatch):
    monkeypatch.setattr(studio, "STUDIO_ROOT", tmp_path)
    monkeypatch.setattr(studio, "PREVIEWS_KEPT", 3)
    folder = tmp_path / "1" / "scene_0_abc" / "preview"
    alphas = folder / studio.ALPHA_DIR
    alphas.mkdir(parents=True)
    old = time.time() - 2 * studio.ALPHAS_GRACE_SEC
    dirs = []
    for i in range(6):
        d = folder / f"k{i}"
        d.mkdir()
        (d / "c0_target.png").write_bytes(b"png")
        a = alphas / f"a{i}.png"
        a.write_bytes(b"alpha")
        os.utime(a, (old, old))
        studio._write(d / "preview.json", {"chunks": [{"layers": [
            {"element_id": "pattern", "alpha_url": studio.url_for(a)}]}]})
        os.utime(d / "preview.json", (old + i, old + i))
        dirs.append(d)
    stray_young = alphas / "young.png"          # an unkept preview's, just drawn
    stray_young.write_bytes(b"alpha")
    stray_old = alphas / "orphan.png"
    stray_old.write_bytes(b"alpha")
    os.utime(stray_old, (old, old))
    # k1 was served a moment ago: it counts as new.
    studio._served(dirs[1] / "preview.json")
    studio._prune_previews(folder, dirs[0], frozenset({str(dirs[2])}))
    left = sorted(p.name for p in folder.iterdir() if p.is_dir() and p.name != studio.ALPHA_DIR)
    assert left == ["k0", "k1", "k2", "k4", "k5"], "the newest three, the one just drawn and one being drawn"
    assert sorted(p.name for p in alphas.iterdir()) == ["a0.png", "a1.png", "a2.png", "a4.png", "a5.png",
                                                       "young.png"]
    assert not stray_old.exists() and stray_young.exists()


def test_few_previews_are_left_alone(tmp_path):
    folder = tmp_path / "preview"
    for i in range(3):
        (folder / f"k{i}").mkdir(parents=True)
    studio._prune_previews(folder, folder / "k0", frozenset())
    assert len(list(folder.iterdir())) == 3


def test_a_kept_preview_served_again_counts_as_new(studio_env):
    studio_env["make"]()
    recipe = rc.preset_42_default("scene_0")
    body = asyncio.run(studio.preview(1, "scene_0", recipe))
    [pj] = _saved()
    os.utime(pj, (1.0, 1.0))
    again = asyncio.run(studio.preview(1, "scene_0", recipe))
    assert again["chunks"] == body["chunks"]
    assert pj.stat().st_mtime > 1.0


def test_versions_built_on_are_held_only_so_many(monkeypatch):
    monkeypatch.setattr(studio, "_BASES", {})
    monkeypatch.setattr(studio, "_STACKS", {})
    loads: list = []

    async def load(spec, run_id, target):
        loads.append(spec.version_id)
        return spec.version_id

    monkeypatch.setattr(hbase, "load", load)

    async def go():
        for v in range(1, studio.BASES_KEPT + 3):
            await studio.load_base(1, "scene_0", hbase.BaseSpec(v, "rebuild"))
        await studio.load_base(1, "scene_0", hbase.BaseSpec(3, "rebuild"))     # used again: kept
        await studio.load_base(1, "scene_0", hbase.BaseSpec(99, "rebuild"))
        loop = asyncio.get_running_loop()
        busy = loop.create_future()
        studio._keep_stack("busy", busy)
        for k in range(studio.BASES_KEPT + 2):
            done = loop.create_future()
            done.set_result(k)
            studio._keep_stack(f"s{k}", done)
        stacks = list(studio._STACKS)
        busy.cancel()
        return stacks

    stacks = asyncio.run(go())
    assert len(studio._BASES) == studio.BASES_KEPT
    assert [k[2].version_id for k in studio._BASES][-2:] == [3, 99], "the most recent last"
    assert loads.count(3) == 1, "one still held is not loaded again"
    assert "busy" in stacks, "one being made is never let go"
    assert len(stacks) == studio.BASES_KEPT and stacks[-1] == f"s{studio.BASES_KEPT + 1}"


# ---------------------------------------------------------------------------
# The stray-drawing clean-up in the preview and in what start stores
# ---------------------------------------------------------------------------

def _with_guards(recipe: rc.Recipe, *, head: str = "brief", **guards) -> rc.Recipe:
    d = recipe.model_dump(mode="json")
    return rc.normalize({**d, "director": {**d["director"], "head": head},
                         "guards": {**d["guards"], **guards}})


def test_the_preview_shows_the_clean_up_the_run_will_make(studio_env):
    studio_env["make"]()
    d42 = rc.preset_42_default("scene_0")

    def part_guards(recipe):
        return asyncio.run(studio.preview(1, "scene_0", recipe))["chunks"][0]["guards"]

    assert "stray_clean" not in part_guards(d42)
    assert "stray_clean" not in part_guards(_with_guards(d42, stray="clean")), \
        "the approved look's brief asks Omni for icons: no clean-up"
    assert part_guards(_with_guards(d42, head="harness", stray="clean"))["stray_clean"] is True
    assert "stray_clean" not in part_guards(_with_guards(d42, head="harness"))


def test_start_stores_the_clean_up_only_for_a_run_that_makes_it(monkeypatch):
    created: list = []

    async def run_row(run_id):
        return {"metadata": {}}

    async def none(*a, **k):
        return []

    async def get(slug):
        return lib.BUILTINS.get(slug)

    async def create(run_id, options):
        created.append(options)
        return {"id": 78}

    async def run(*a, **k):
        return None

    exp = {"id": 9, "status": "ready", "options": {"target": "scene_0"}, "arms": {}, "evidence": {}}

    async def load(spec, run_id, target):
        return hbase.BaseVersion(spec=spec, exp=exp, chain=[exp], layers=[], frames=[])

    monkeypatch.setattr(pipeline.th, "_fetch_run_row", run_row)
    monkeypatch.setattr(omni, "providers", lambda: ["vertex"])
    monkeypatch.setattr(lib, "get", get)
    monkeypatch.setattr(store, "list_for_run", none)
    monkeypatch.setattr(store, "create_experiment", create)
    monkeypatch.setattr(pipeline, "_run", run)
    monkeypatch.setattr(hbase, "load", load)
    d42 = rc.preset_42_default("scene_0")

    async def go(recipe, base=None):
        await pipeline.start(1, None, recipe=recipe, base=base)
        await asyncio.sleep(0)
        return created[-1]

    assert "stray_clean" not in asyncio.run(go(_with_guards(d42, stray="clean"))), "the brief asks for icons"
    assert asyncio.run(go(_with_guards(d42, head="harness", stray="clean")))["stray_clean"] is True
    on_top = asyncio.run(go(_with_guards(_recipe(_pat("pattern-2", "beads_row")), head="harness", stray="clean"),
                            base={"version_id": 9, "mode": "stack"}))
    assert "stray_clean" not in on_top and on_top["base"]["mode"] == "stack", "the hold is the clean-up on top"
    assert "stray_clean" not in asyncio.run(go(d42))


def test_two_requests_making_the_same_picture_at_once_both_succeed(tmp_path, monkeypatch):
    """The icon plan's pattern layer (like a moment, or the speaker's masks)
    is made by whichever request gets there first. Two at once (the Icons tab
    asks each second while Generate asks too) each write their own temporary
    file, and one that Windows refuses to put over the file the other has
    just made (open, being served) is dropped rather than failing the plan
    (seen in the e2e check: a 500 on /icons/plan, WinError 32)."""
    monkeypatch.setattr(studio, "STUDIO_ROOT", tmp_path)
    out = tmp_path / "preview" / "k"
    out.mkdir(parents=True)
    target = np.zeros((64, 36, 3), np.uint8)
    target[:, :12] = (127, 43, 242)
    Image.fromarray(target).save(out / "c0_target.png")
    Image.fromarray(np.zeros((64, 36, 3), np.uint8)).save(tmp_path / "frame.png")
    start = threading.Barrier(6)
    got, errors = [], []

    def make() -> None:
        start.wait()
        try:
            got.append(studio._preview_ink(out, tmp_path / "frame.png", 9, 16))
        except Exception as e:  # noqa: BLE001 — the test says which
            errors.append(e)

    threads = [threading.Thread(target=make) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors and len(got) == 6
    assert all(url == got[0][1] for _ink, url in got) and (out / "icons_pattern.png").is_file()
    assert not list(out.glob("*.tmp.png"))
    # Each thread writes a temporary file of its own.
    names = []
    t = threading.Thread(target=lambda: names.append(studio._tmp_of(out / "x.png", ".tmp.png")))
    t.start()
    t.join()
    names.append(studio._tmp_of(out / "x.png", ".tmp.png"))
    assert names[0] != names[1]
    # Refused over a file that is there: dropped; with nothing there: raised.
    (out / "x.png").write_bytes(b"made")
    tmp = out / "x.tmp.png"
    tmp.write_bytes(b"made too")

    def refuse(_a, _b):
        raise PermissionError(32, "in use")

    monkeypatch.setattr(studio.os, "replace", refuse)
    studio._made(tmp, out / "x.png")
    assert not tmp.exists() and (out / "x.png").read_bytes() == b"made"
    tmp.write_bytes(b"alone")
    with pytest.raises(PermissionError):
        studio._made(tmp, out / "y.png")
