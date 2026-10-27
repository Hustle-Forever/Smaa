"""Rebrand studio v2, M7: a colour change repainted at once, from the
preview's own layers.

A preview's part lists each drawn layer's colour-free alpha (after the
speaker's cut, in draw order). The studio lays them on the part's frame in
the draft's colours the moment a colour changes; the server's exact target
replaces that once it lands. For that to be honest, frame + each alpha in
its colour must BE the target: these tests compose the served files with
numpy exactly as the studio's canvas does (rebrandRecolour.ts) and compare
them with compose_target's frame, on the original and on a version added to.
The paid mask is never asked for; the 42 default is held byte for byte by
tests/test_rebrand_golden.py."""
from __future__ import annotations

import asyncio
import json
import shutil
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from app.services import pattern_library as lib
from app.services import person_matte
from app.services import rebrand_pattern_ref as pattern_ref
from app.services import rebrand_pipeline as pipeline
from app.services.rebrand_harness import base as hbase
from app.services.rebrand_harness import recipe as rc
from app.services.rebrand_harness import studio
from app.services.rebrand_harness.elements import pattern as pattern_el

FIX = Path(__file__).parent / "fixtures" / "rebrand_golden"
INTRO, FRAME, PERSON = FIX / "intro.mp4", FIX / "frame.png", FIX / "person.png"
REAL_MASKS = all(pattern_ref._mask_path(s).exists() for s in lib.BUILTINS)
INTRO_COLOUR = "#2ed8e8"
ROW_TOP_RIGHT = lib.BEADS_ROW_CANDIDATES[3]

needs_masks = pytest.mark.skipif(not REAL_MASKS, reason="the pattern kit's masks are not on this machine")


def _person() -> np.ndarray:
    return np.asarray(Image.open(PERSON).convert("L"), dtype="float32") / 255.0


def _pat(id_: str = "pattern", slug: str = "structure", **props) -> rc.Element:
    return rc.Element(id=id_, kind="pattern", placement=rc.Placement(anchor="auto"),
                      props={"slug": slug, "layout": None, "colour": "auto", **props})


def _recipe(*els: rc.Element, **kw) -> rc.Recipe:
    return rc.normalize(rc.Recipe(target="scene_0", seed="studio:1:scene_0", elements=list(els), **kw))


def _two(first: str, second: str, **kw) -> rc.Recipe:
    """Structure on the left and the bead row pinned top right."""
    return _recipe(_pat(colour=first),
                   _pat("pattern-2", "beads_row", colour=second, layout=ROW_TOP_RIGHT.public()), **kw)


@pytest.fixture
def scene(tmp_path, monkeypatch):
    """A prepared two-part scene over the golden frame, the intro's colours
    already on disk, the LOCAL mask only."""
    monkeypatch.setattr(studio, "STUDIO_ROOT", tmp_path)
    monkeypatch.setattr(studio, "_PREPARED", {})
    monkeypatch.setattr(studio, "_MASK_TASKS", {})
    monkeypatch.setattr(studio, "_PREVIEWS", {})

    async def local_mask(frame):
        return _person()

    async def paid(frame):
        raise AssertionError("the studio asked for the paid (Gemini) mask")

    monkeypatch.setattr(person_matte, "local_mask", local_mask)
    monkeypatch.setattr(person_matte, "person_mask", paid)

    work = tmp_path / "1" / "scene_0_abc"
    work.mkdir(parents=True)
    firsts = []
    for j in range(2):
        f = work / f"c{j}_first.png"
        shutil.copyfile(FRAME, f)
        firsts.append(f)
    (work / "colours.json").write_text(json.dumps({
        "ground": "#3d3c3d", "ink": "#f4f4f8", "fell_back": False, "frame": None,
        "colour": INTRO_COLOUR}), encoding="utf-8")
    intro = pipeline.Segment(kind="intro", index=0, source=INTRO, is_still=False)
    seg = pipeline.Segment(kind="scene", index=0, source=firsts[0], is_still=False)
    ctx = pipeline.Ctx(exp_id=0, run_id=1, aspect="9:16", width=720, height=1280, work=work,
                       options={}, run_ctx={}, intro=intro, scenes=[seg], outro=None)
    ctx.target_seg = seg
    ctx.frames["th_first"] = firsts[0]
    made = studio.Scene(run_id=1, target="scene_0", aspect="9:16", width=720, height=1280,
                        work=work, ctx=ctx, chunks=[work / f"c{j}.mp4" for j in range(2)],
                        firsts=firsts, seconds=[8.0, 8.0], plan_frame=firsts[0])

    async def prepare(run_id, target):
        return made

    monkeypatch.setattr(studio, "prepare", prepare)
    return made


def _file(url: str) -> Path:
    return studio.resolve_file(url.split("/file/", 1)[1])


def _rgb(h: str) -> tuple[int, int, int]:
    return tuple(int(h[i:i + 2], 16) for i in (1, 3, 5))


def _blend(dst: np.ndarray, rgb: tuple[int, int, int], a: np.ndarray) -> np.ndarray:
    """One layer of colour `rgb`, opacity `a` (uint8), over an opaque frame —
    PIL's alpha_composite in integers, as rebrandRecolour.ts blends it."""
    d = dst.astype(np.uint32)
    al = a.astype(np.uint32)[..., None]
    tmp = (np.array(rgb, np.uint32) * al + d * (255 - al)) * 128 + (0x80 << 7)
    out = (((tmp >> 8) + tmp) >> 8) >> 7
    return np.where(al == 0, d, out).astype(np.uint8)


def _colour_of(recipe: rc.Recipe, element_id: str, intro: str) -> str:
    """The colour the studio paints a layer in, from the DRAFT
    (rebrandRecipe.colourOf: its own hex, else the palette's, else the
    intro's)."""
    el = next(e for e in recipe.elements if e.id == element_id)
    return pattern_el.colour_override(recipe.colours, el) or intro


def _repaint(body: dict, recipe: rc.Recipe, j: int) -> np.ndarray:
    chunk = body["chunks"][j]
    out = np.asarray(Image.open(_file(chunk["frame_url"])).convert("RGB"))
    for layer in chunk["layers"]:
        with Image.open(_file(layer["alpha_url"])) as im:
            assert im.mode == "RGBA"
            rgba = np.asarray(im)
        assert (rgba[..., :3] == 255).all(), "an alpha carries no colour"
        colour = _colour_of(recipe, layer["element_id"], body["colours"]["intro"])
        out = _blend(out, _rgb(colour), rgba[..., 3])
    return out


def _target(body: dict, j: int) -> np.ndarray:
    return np.asarray(Image.open(_file(body["chunks"][j]["target_url"])).convert("RGB"))


def _diff(a: np.ndarray, b: np.ndarray) -> int:
    return int(np.abs(a.astype(np.int16) - b.astype(np.int16)).max())


@needs_masks
def test_the_layers_repaint_every_colour_to_the_exact_target(scene):
    """Two patterns, in two sets of colours (a picked hex and Auto from the
    intro, then charcoal and Auto from 42 violet): the same alphas, and in
    each set the frame + the alphas in the draft's colours IS the target."""
    first = _two("#ff00dd", "auto")
    second = _two("#303030", "auto", colours=rc.Colours(source="brand"))
    bodies = [asyncio.run(studio.preview(1, "scene_0", r)) for r in (first, second)]
    for body, recipe in zip(bodies, (first, second)):
        assert body["kept"] is True
        drawn = {p["element_id"]: p["colour"] for p in body["patterns"]}
        for j in range(2):
            chunk = body["chunks"][j]
            assert [x["element_id"] for x in chunk["layers"]] == ["pattern", "pattern-2"], "draw order"
            for layer in chunk["layers"]:
                assert _colour_of(recipe, layer["element_id"], body["colours"]["intro"]) == \
                    drawn[layer["element_id"]], "the draft's colour is the one drawn"
            assert _diff(_repaint(body, recipe, j), _target(body, j)) <= 1
    assert [c["layers"] for c in bodies[0]["chunks"]] == [c["layers"] for c in bodies[1]["chunks"]], \
        "a colour change keeps every alpha's address"
    assert bodies[0]["chunks"][0]["target_url"] != bodies[1]["chunks"][0]["target_url"]

    # The other colours, painted on the FIRST preview's layers — what the
    # studio shows while the second is made — are the second's target.
    for j in range(2):
        assert _diff(_repaint(bodies[0], second, j), _target(bodies[1], j)) <= 1

    folder = scene.work / "preview" / studio.ALPHA_DIR
    # Both parts are the same frame here: one file per layer, written once.
    assert len(list(folder.glob("*.png"))) == 2
    assert not list(folder.glob("*.tmp*"))


@needs_masks
def test_the_42_default_repaints_to_its_approved_target(scene):
    recipe = rc.preset_42_default("scene_0")
    body = asyncio.run(studio.preview(1, "scene_0", recipe))
    for j in range(2):
        assert [x["element_id"] for x in body["chunks"][j]["layers"]] == ["pattern"]
        assert _diff(_repaint(body, recipe, j), _target(body, j)) <= 1
    violet = recipe.model_copy(update={"colours": rc.Colours(source="brand")})
    again = asyncio.run(studio.preview(1, "scene_0", violet))
    assert again["chunks"][0]["layers"] == body["chunks"][0]["layers"]
    assert _diff(_repaint(body, violet, 0), _target(again, 0)) <= 1


@needs_masks
def test_added_on_top_of_a_version_the_layers_are_laid_on_its_frames(scene, monkeypatch):
    """"Add on top": the new layers are drawn on the version's own frames, so
    that is the base the studio repaints on."""
    on = scene.work / "on_v9"
    on.mkdir()
    frames = []
    for j in range(2):
        f = on / f"c{j}_settled.png"
        img = np.asarray(Image.open(FRAME).convert("RGB")).copy()
        img[: 200] = (40, 40, 40)                       # the version's own look
        Image.fromarray(img).save(f)
        frames.append(f)
    exp = {"id": 9, "status": "ready", "options": {}, "arms": {}, "evidence": {}}
    bv = hbase.BaseVersion(spec=hbase.BaseSpec(9, "stack"), exp=exp, chain=[exp], layers=[],
                           frames=frames)

    async def prepare_base(run_id, target, version_id):
        assert version_id == 9
        return bv

    monkeypatch.setattr(studio, "prepare_base", prepare_base)
    recipe = _recipe(_pat("pattern-2", "beads_row", colour="#ff00dd", layout=ROW_TOP_RIGHT.public()))
    body = asyncio.run(studio.preview(1, "scene_0", recipe, base=hbase.BaseSpec(9, "stack")))
    for j in range(2):
        chunk = body["chunks"][j]
        assert _file(chunk["frame_url"]) == frames[j].resolve()
        assert [x["element_id"] for x in chunk["layers"]] == ["pattern-2"]
        assert _diff(_repaint(body, recipe, j), _target(body, j)) <= 1


def test_the_saved_alphas_are_in_draw_order_and_skip_a_hidden_layer(tmp_path, monkeypatch):
    monkeypatch.setattr(studio, "STUDIO_ROOT", tmp_path)
    a = np.zeros((8, 6), np.uint8)
    a[2:4, 1:3] = 200
    b = np.zeros((8, 6), np.uint8)
    b[5:, :] = 255
    captured = [
        {"index": 0, "name": "front", "z": "front", "alpha": b, "visible": b},
        {"index": 1, "name": "pattern", "z": "behind", "alpha": a, "visible": a},
        {"index": 2, "name": "pattern-3", "z": "behind", "alpha": a, "visible": np.zeros_like(a)},
    ]
    out = studio._save_alphas(captured, ["icons", "pattern", "pattern-3"], tmp_path / "_alpha")
    assert [x["element_id"] for x in out] == ["pattern", "icons"], "behind first, a covered one left out"
    again = studio._save_alphas(captured, ["icons", "pattern", "pattern-3"], tmp_path / "_alpha")
    assert again == out, "the same alpha is the same address"
    with Image.open(_file(out[0]["alpha_url"])) as im:
        got = np.asarray(im)
    assert np.array_equal(got[..., 3], a) and (got[..., :3] == 255).all()
