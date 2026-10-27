"""Patterns by hand: moved, sized, turned (I13).

  the window     _layer(window=) draws a pattern's place over any window of
                 frame pixels — at the frame itself it is _layer, byte for
                 byte, for every built-in place, 9:16 and 16:9 — so a moved
                 strip shows its own art where the frame used to cut it
  placed_layer   with no hand move, _layer itself (every version before is
                 drawn as it was); moved, the plate's own art in the plate's
                 own warp: plate frame 0 (identity camera) has its very alpha
  one warp       the plate resamples a moved pattern ONCE per frame, camera
                 and hand together
  full screen    a composition (blueprint) moved, shrunk or turned keeps the
                 frame covered (the clamp); a tiled grid keeps its ink
  the fixture    lib/rebrandHand.ts works the hand matrix and the clamp out
                 as the renderer does (tests/fixtures/rebrand_hand.json)
  the planner    a pattern moved by hand holds no slot (hand:<id>), those
                 after it keep off its moved ink; a pin that no longer fits
                 resets the move and says so
  the studio     the preview says what the stage's handles need and warns
                 when a move hides a pattern; the design still of a moved
                 pattern is the run's own plate; /pattern/art

The pattern kit's masks are real (skipped when absent), MODNet is faked by
the green (test_rebrand_house_studio), every paid client raises, and
nothing touches the database, a bucket or a server."""
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
from app.services import rebrand_pattern_ref as ref
from app.services.rebrand_harness import base as hbase
from app.services.rebrand_harness import compose
from app.services.rebrand_harness import house
from app.services.rebrand_harness import plate as pl
from app.services.rebrand_harness import recipe as rc
from app.services.rebrand_harness import studio
from app.services.rebrand_harness.elements import pattern as pattern_el

from test_rebrand_house_studio import (FPS, H as SH, PLAN_KEY, W as SW, _decoded, _png, paid,  # noqa: F401
                                       scene)

cv2 = pytest.importorskip("cv2")

FIXTURES = Path(__file__).resolve().parent / "fixtures"
REPO = Path(__file__).resolve().parents[2]
HAVE_MASKS = all(ref._mask_path(s).exists() for s in lib.BUILTINS)
needs_masks = pytest.mark.skipif(not HAVE_MASKS, reason="the pattern kit's masks are not here")
DUG = ref.Colours(ground="#7f2bf2", ink="#ffffff", fell_back=False, frame=None, colour="#7f2bf2")
STUDIO_815 = (Path(__file__).resolve().parents[1] / "data" / "yas_producer" / "talking-head-v2" / "rebrand"
              / "_studio" / "815" / "scene_0_84f81b9967")


def _built(tmp_path: Path, slug: str, aspect: str = "9:16", k: int = 0, **hand) -> ref.PatternRef:
    lay = lib.BUILTINS[slug].candidates[k]
    r = ref.build(None, tmp_path / f"{slug}_{aspect.replace(':', 'x')}_{k}", aspect=aspect, slug=slug, layout=lay,
                  colours=DUG)
    if hand:
        r = dataclasses.replace(r, hand=_pivoted(r, rc.Hand(**hand), *ref.canvas_for(aspect)))
    return r


def _pivoted(r: ref.PatternRef, hand: rc.Hand, w: int, h: int) -> rc.Hand:
    """The hand made about the pattern's own visible box, as the studio makes
    it (placement.box: its box when grabbed)."""
    b = compose.bbox(np.asarray(ref._layer(r, w, h).getchannel("A")))
    return dataclasses.replace(hand, pivot=(b["x"] + b["w"] / 2, b["y"] + b["h"] / 2))


def _alpha(img) -> np.ndarray:
    return np.asarray(img.getchannel("A")) if img is not None else None


# ---------------------------------------------------------------------------
# The window, and the layer with no move
# ---------------------------------------------------------------------------

@needs_masks
@pytest.mark.parametrize("aspect", ["9:16", "16:9"])
def test_the_window_at_the_frame_is_the_layer_byte_for_byte(tmp_path, aspect):
    W, H = ref.canvas_for(aspect)
    for slug, entry in lib.BUILTINS.items():
        for k in range(len(entry.candidates)):
            r = _built(tmp_path, slug, aspect, k)
            base = np.asarray(ref._layer(r, W, H))
            assert np.array_equal(np.asarray(ref._layer(r, W, H, window=(0, 0, W, H))), base), (slug, k)
            big = np.asarray(ref._layer(r, W, H, window=(-W // 2, -H // 2, 2 * W, 2 * H)))
            assert np.array_equal(big[H // 2:H // 2 + H, W // 2:W // 2 + W], base), f"{slug}[{k}] cropped back"
            # No hand move: every reader draws it exactly as before.
            assert np.array_equal(np.asarray(ref.placed_layer(r, W, H)), base), (slug, k)


@needs_masks
def test_an_unmoved_pattern_keeps_its_record_and_its_plate(tmp_path):
    r = _built(tmp_path, "structure")
    assert "hand" not in r.public(), "evidence keeps its old shape"
    ly = house.layer_spec(rc.Element(id="pattern", kind="pattern"), r, width=720, height=1280, duration=8.0,
                          seed="s")
    assert ly.margin == (0, 0) and ly.hand is None and ly.art.shape == (1280, 720, 4)
    assert np.array_equal(ly.art, pl.premultiplied(np.asarray(ref._layer(r, 720, 1280)))), "the very art of before"
    moved = dataclasses.replace(r, hand=rc.Hand(dx=0.1, rotate=12.0))
    assert moved.public()["hand"] == {"dx": 0.1, "dy": 0.0, "scale": 1.0, "rotate": 12.0, "pivot": [0.5, 0.5]}


@needs_masks
@pytest.mark.parametrize("slug", ["structure", "beads_field"])
def test_a_moved_strip_shows_its_own_art_where_the_frame_cut_it(tmp_path, slug):
    """A left strip moved 120 px right: the band on its left is its own art
    carrying on — not the empty band a moved frame-sized layer leaves."""
    r = _built(tmp_path, slug, dx=120 / 720, dy=60 / 1280)
    a = _alpha(ref.placed_layer(r, 720, 1280))
    inked = int((a[:, :120].max(axis=0) > 8).sum())
    assert inked >= 110, f"{inked}/120 columns of the uncovered band are inked"
    naive = np.roll(_alpha(ref._layer(r, 720, 1280)), 120, axis=1)
    naive[:, :120] = 0
    assert int((naive[:, :120].max(axis=0) > 8).sum()) == 0, "what a straight cut would have left"


@needs_masks
@pytest.mark.parametrize("hand", [dict(scale=0.6), dict(scale=1.8), dict(rotate=30.0), dict(rotate=-75.0, scale=1.3)])
def test_a_sized_or_turned_grid_keeps_its_ink(tmp_path, hand):
    """A tiled background repeats past the frame: sized or turned, the frame
    holds about as much of its ink as before — no bare corner. (Within 10%:
    shrunk to 0.6, its one-pixel lines lose ~8% of their ink to the one
    bilinear warp; turned or grown, ~3%.)"""
    r = _built(tmp_path, "grid", **hand)
    before = float(_alpha(ref._layer(r, 720, 1280)).mean())
    after = float(_alpha(ref.placed_layer(r, 720, 1280)).mean())
    assert abs(after - before) <= 0.10 * before, (hand, before, after)
    # Every corner of the frame still has lines near it.
    a = _alpha(ref.placed_layer(r, 720, 1280))
    for ys, xs in ((slice(0, 160), slice(0, 160)), (slice(0, 160), slice(-160, None)),
                   (slice(-160, None), slice(0, 160)), (slice(-160, None), slice(-160, None))):
        assert a[ys, xs].max() > 128


@needs_masks
@pytest.mark.parametrize("turn", [0.0, 15.0, 45.0, 90.0])
def test_a_moved_composition_always_fills_the_frame(tmp_path, turn):
    """Blueprint is one copy: shrunk, moved and turned, its size is raised
    just as far as keeps the frame inside it (and no further)."""
    r0 = _built(tmp_path, "blueprint")
    rect = ref.cover_rect(r0, 720, 1280)
    assert rect is not None and rect[0] <= 0 and rect[1] <= 0 and rect[2] >= 720 and rect[3] >= 1280
    for dx in (0.0, 0.35):
        asked = rc.Hand(dx=dx, scale=0.6, rotate=turn)
        got = ref.drawn_hand(dataclasses.replace(r0, hand=asked), 720, 1280)
        assert ref.covers_frame(got, rect, 720, 1280), (turn, dx)
        assert got.scale > asked.scale and not ref.covers_frame(dataclasses.replace(got, scale=got.scale - 2e-3),
                                                                rect, 720, 1280), "the least that covers"
        # The art drawn so fills the frame: every pixel comes from inside
        # its one copy.
        back = ref._unmapped(got, 720, 1280, np.stack(np.meshgrid(np.arange(0, 720, 8) + 0.5,
                                                                  np.arange(0, 1280, 8) + 0.5), -1).reshape(-1, 2))
        inside = ((back[:, 0] >= rect[0]) & (back[:, 0] < rect[2]) & (back[:, 1] >= rect[1]) & (back[:, 1] < rect[3]))
        assert inside.all()
    # A strip is never clamped: it may go anywhere.
    strip = _built(tmp_path, "structure", dx=0.6, scale=0.5)
    assert ref.drawn_hand(strip, 720, 1280) == strip.hand and ref.cover_rect(strip, 720, 1280) is None


def test_the_hand_fixture_is_the_renderers():
    """tests/fixtures/rebrand_hand.json (lib/rebrandHand.ts is checked
    against it) is what the renderer works out today."""
    fx = json.loads((FIXTURES / "rebrand_hand.json").read_text(encoding="utf-8"))
    assert (fx["cover_scale_max"], fx["cover_steps"], fx["cover_edge"]) == (
        ref.COVER_SCALE_MAX, ref.COVER_STEPS, ref.COVER_EDGE)
    for c in fx["matrix"]:
        h = rc.Hand(**{**c["hand"], "pivot": tuple(c["hand"]["pivot"])})
        m = ref.hand_matrix(h, *c["size"])
        assert (m is None) == (c["matrix"] is None)
        if m is not None:
            assert np.allclose(m, c["matrix"], atol=1e-9)
    moved = 0
    for c in fx["clamp"]:
        h = rc.Hand(**{**c["hand"], "pivot": tuple(c["hand"]["pivot"])})
        rect = tuple(c["rect"])
        assert ref.covers_frame(h, rect, *c["size"]) == c["covers"]
        got = ref.clamp_cover(h, rect, *c["size"])
        want = c["clamped"]
        assert (got.dx, got.dy, got.scale, got.rotate) == pytest.approx(
            (want["dx"], want["dy"], want["scale"], want["rotate"]), abs=1e-9)
        moved += got.dx != h.dx
    assert moved > 0, "the fixture has cases whose move is drawn back"


def test_the_studio_works_the_hand_out_as_the_renderer():
    """frontend/scripts/check-rebrand-hand.mjs runs lib/rebrandHand.ts on
    the shared fixture (node 22.6+; skipped where there is none)."""
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed here")
    version = subprocess.run([node, "--version"], capture_output=True, text=True, timeout=30).stdout.strip()
    if tuple(int(x) for x in version.lstrip("v").split(".")[:2]) < (22, 6):
        pytest.skip(f"node {version} cannot strip types")
    out = subprocess.run([node, "--experimental-strip-types", "scripts/check-rebrand-hand.mjs"],
                         capture_output=True, text=True, timeout=120, cwd=REPO / "frontend")
    assert out.returncode == 0, out.stdout + out.stderr
    assert "cases agree" in out.stdout and "FAIL" not in out.stdout


# ---------------------------------------------------------------------------
# The plate: one warp
# ---------------------------------------------------------------------------

def _pattern_el(**placement) -> rc.Element:
    return rc.Element(id="pattern", kind="pattern", placement={"anchor": "auto", **placement},
                      animation={"entrance": "auto"}, props={"slug": "structure"})


@needs_masks
@pytest.mark.parametrize("hand", [dict(dx=0.17, dy=0.05), dict(scale=0.6), dict(rotate=30.0),
                                  dict(dx=0.1, dy=-0.05, scale=1.4, rotate=-20.0)])
def test_plate_frame_0_is_the_placed_layer(tmp_path, hand):
    """The plate's frame 0 (the camera there is the identity) draws a moved
    pattern exactly where placed_layer — what the preview, the ink and the
    icons read — has it: the same alpha, array_equal; its colour within a
    level (premultiplied rounding)."""
    r = _built(tmp_path, "structure", **hand)
    placed = np.asarray(ref.placed_layer(r, 720, 1280))
    ly = house.layer_spec(_pattern_el(), r, width=720, height=1280, duration=8.0, seed="s")
    assert ly.hand is not None and ly.art.shape[:2] != (1280, 720), "the unmoved art past the frame, and the move"
    p0 = pl.plate_at(pl.PlateSpec(width=720, height=1280, fps=30.0, n=240, layers=(ly,)), 0, settled=True)
    assert np.array_equal(p0[..., 3], placed[..., 3])
    want = pl.premultiplied(placed).astype(int)
    assert np.abs(p0[..., :3].astype(int) - want[..., :3]).max() <= 1


class _Push:
    """A camera pushing in about the frame's centre: x1.06 by frame 60."""

    def at(self, i):
        s = 1.0 + 0.001 * i
        return np.array([[s, 0, (1 - s) * 360], [0, s, (1 - s) * 640]], np.float32)


@needs_masks
def test_a_moved_pattern_is_resampled_once_per_frame(tmp_path, monkeypatch):
    """Camera x hand in ONE warp: a moved, turned pattern following the
    camera's push-in costs one cv2.warpAffine a frame — never a warp of an
    already-warped layer (thin lines shimmer twice as much)."""
    r = _built(tmp_path, "structure", dx=0.1, rotate=17.0, scale=0.9)
    ly = house.layer_spec(_pattern_el(), r, width=720, height=1280, duration=8.0, seed="s")
    spec = pl.PlateSpec(width=720, height=1280, fps=30.0, n=240, layers=(ly,))
    calls = []
    real = cv2.warpAffine

    def counted(*a, **k):
        calls.append(1)
        return real(*a, **k)

    monkeypatch.setattr(cv2, "warpAffine", counted)
    got = pl.plate_at(spec, 60, camera=_Push())
    assert len(calls) == 1 and got[..., 3].any()
    # …and it is the pattern moved by hand, then by the camera.
    m = np.eye(3)
    m[:2] = _Push().at(60)
    back = np.linalg.inv(m @ pl._m3(ly.hand) @ pl._shift(-ly.margin[0], -ly.margin[1]))
    ys, xs = np.nonzero(got[..., 3] > 200)
    src = np.stack([xs, ys, np.ones_like(xs)], 0).astype(np.float64)
    u, v = (back @ src)[:2].round().astype(int)
    ok = (u >= 0) & (v >= 0) & (u < ly.art.shape[1]) & (v < ly.art.shape[0])
    assert ok.mean() > 0.99 and (ly.art[v[ok], u[ok], 3] > 0).mean() > 0.95


# ---------------------------------------------------------------------------
# The planner: no slot, its ink moved; a stale pin
# ---------------------------------------------------------------------------

def _job(tmp_path: Path, steps: list) -> pattern_el.PatternJob:
    frame = tmp_path / "plan.png"
    frame.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (720, 1280), (90, 110, 120)).save(frame)

    async def plan_frame():
        return frame

    async def mask(_frame):
        return np.zeros((1280, 720), np.float32)

    async def step(name, status="running", detail=""):
        steps.append((name, status))

    return pattern_el.PatternJob(exp_id=0, aspect="9:16", out_dir=tmp_path / "pattern", intro=None,
                                 intro_is_still=False, seed="s", plan_frame=plan_frame, step=step, colours=DUG,
                                 person_mask=mask)


def _pin(slug: str = "structure", k: int = 0) -> dict:
    return lib.BUILTINS[slug].candidates[k].public()


@needs_masks
def test_a_move_by_hand_holds_no_slot_and_the_next_keeps_off_its_moved_ink(tmp_path, monkeypatch):
    """The bead row's left strip dragged to the right half: the bead field
    after it takes the left side (the row holds no slot, "hand:pattern"),
    and keeps off where the row's ink really is (its moved ink)."""
    async def get(slug):
        return lib.BUILTINS.get(slug)

    monkeypatch.setattr(lib, "get", get)
    moved = rc.Element(id="pattern", kind="pattern",
                       placement={"anchor": "auto", "offset": (0.55, 0.0), "box": {"x": 0, "y": 0, "w": 0.5, "h": 1}},
                       props={"slug": "beads_row", "layout": _pin("beads_row")})
    second = rc.Element(id="pattern-2", kind="pattern", props={"slug": "beads_field"})
    recipe = rc.normalize(rc.Recipe(engine="house", elements=[moved, second]))
    first, nxt = asyncio.run(pattern_el.build_all(rc.pattern_elements(recipe), recipe.colours, _job(tmp_path, [])))
    assert first.slot == "hand:pattern" and first.ref.hand == rc.hand_of(moved)
    assert nxt.slot == "left" and not nxt.moved and not nxt.warnings, "the left side is free: the row left it"
    ink = pattern_el._ink(first.ref, 720, 1280)
    assert np.array_equal(ink, _alpha(ref.placed_layer(first.ref, 720, 1280)).astype(np.float32) / 255.0)
    assert ink[:, :360].max() == 0 and ink[:, 396:].max() > 0.9, "the ink the others keep off is the moved ink"
    # Unmoved, the row holds the left side: the field moves to the right.
    still = rc.normalize(rc.Recipe(engine="house", elements=[
        rc.Element(id="pattern", kind="pattern", props={"slug": "beads_row", "layout": _pin("beads_row")}), second]))
    a, b = asyncio.run(pattern_el.build_all(rc.pattern_elements(still), still.colours, _job(tmp_path / "x", [])))
    assert a.slot == "left" and b.slot == "right" and b.moved


@needs_masks
def test_a_pin_that_no_longer_fits_resets_the_move_and_says_so(tmp_path, monkeypatch):
    async def get(slug):
        return lib.BUILTINS.get(slug)

    monkeypatch.setattr(lib, "get", get)
    steps: list = []
    stale = {**_pin(), "placement": "top", "anchor": "left"}
    el = rc.Element(id="pattern", kind="pattern",
                    placement={"anchor": "auto", "rotate": 20.0, "box": {"x": 0, "y": 0, "w": 0.5, "h": 1}},
                    props={"slug": "structure", "layout": stale})
    [res] = asyncio.run(pattern_el.build_all([el], rc.Colours(), _job(tmp_path, steps)))
    assert res.ref.hand.identity and res.slot == "left"
    assert any("your move was reset" in n and s == "failed" for n, s in steps), steps


def test_a_version_moved_by_hand_holds_no_slot():
    el = rc.Element(id="pattern-2", kind="pattern",
                    placement={"anchor": "auto", "size": 1.5, "box": {"x": 0, "y": 0, "w": 0.5, "h": 1}},
                    props={"slug": "structure", "layout": _pin()})
    exp = {"id": 7, "options": {"recipe": rc.Recipe(engine="house", elements=[el]).model_dump(mode="json")},
           "evidence": {"omni_patterns": [{"element_id": "pattern-2", "slug": "structure",
                                           "layout": _pin()}]}}
    [layer] = hbase.own_layers(exp)
    assert layer["placed"]["slot"] == "hand:pattern-2"
    assert hbase.taken_slots([layer]) == {"hand:pattern-2": "structure"}


# ---------------------------------------------------------------------------
# The studio
# ---------------------------------------------------------------------------

def _hand_recipe(**placement) -> rc.Recipe:
    lay = lib.BUILTINS["structure"].candidates[0].public()
    el = rc.Element(id="pattern", kind="pattern",
                    placement={"anchor": "auto", "box": {"x": 0.0, "y": 0.0, "w": 0.5, "h": 1.0}, **placement},
                    props={"slug": "structure", "layout": lay, "colour": "auto"})
    return rc.normalize(rc.Recipe(engine="house", target="scene_0", seed="studio:1:scene_0", elements=[el]))


@needs_masks
def test_the_preview_says_what_the_handles_need_and_draws_the_move(scene, paid):
    assert studio.PREVIEW_VERSION >= 6, "6: what the handles need (7 adds when each pattern shows)"
    plain = asyncio.run(studio.preview(1, "scene_0", _hand_recipe()))
    [p0] = plain["patterns"]
    assert p0["hand"] is None and p0["full"] is False and p0["warning"] is None
    assert p0["base_box"] == p0["box"] and len(p0["art_key"]) == 16
    body = asyncio.run(studio.preview(1, "scene_0", _hand_recipe(offset=(0.2, 0.05), rotate=15.0, size=0.8)))
    [p] = body["patterns"]
    assert p["hand"] == {"dx": 0.2, "dy": 0.05, "scale": 0.8, "rotate": 15.0}
    assert p["pivot"] == [0.25, 0.5], "the centre of the box it was grabbed at (placement.box)"
    assert p["base_box"] == p0["base_box"] and p["box"] != p0["box"], "the box before the move, the box after"
    assert p["slot"] == "hand:pattern" and p["art_key"] == p0["art_key"], "the art is unmoved: the same key"
    keep = body["chunks"][0].get("keep_url")
    assert keep, "the still's speaker cut, for the stage's stand-in"
    k = cv2.imread(str(studio.resolve_file(keep.split("/file/", 1)[1])), cv2.IMREAD_UNCHANGED)
    assert k.shape == (SH, SW, 4) and k[..., 3].min() == 0 and k[..., 3].max() == 255
    # The house layer: the unmoved art past the frame, the move as its matrix.
    [layer] = body["house"]["layers"]
    assert layer["hand"] is not None and layer["margin"] != [0, 0]
    # Next (the design still) is the run's own plate over frame 0, moved.
    stored = asyncio.run(studio.as_generated(1, "scene_0", _hand_recipe(offset=(0.2, 0.05), rotate=15.0, size=0.8),
                                             None, PLAN_KEY))
    frozen = stored.elements[0].props["resolved"]
    assert frozen["hand"] == {"dx": 0.2, "dy": 0.05, "scale": 0.8, "rotate": 15.0, "pivot": [0.25, 0.5]}
    hs = asyncio.run(studio.house_spec(1, "scene_0", stored, None))
    [ly] = hs.spec.layers
    assert ly.hand is not None and ly.margin == tuple(layer["margin"])
    want = pl.over_frame(_decoded(scene.ctx.target_seg.base, 0),
                         pl.plate_at(hs.spec, 0, bank=hs.bank, settled=True, at=house.still_moment(hs.spec)))
    assert np.array_equal(_png(body["house"]["still_url"]), want), "Next is the run's own plate"
    # The target and its alphas read the moved layer too (placed_layer).
    assert p["box"] == compose.bbox(_alpha(ref.placed_layer(_ref_of(hs, stored), SW, SH)))
    assert paid == []


def _ref_of(hs, stored) -> ref.PatternRef:
    """The stored recipe's pattern as the run draws it (from_resolved: its
    frozen place, its hand move from the recipe)."""
    job = pattern_el.PatternJob(exp_id=0, aspect="9:16", out_dir=Path(hs.clip).parent / "x", intro=None,
                                intro_is_still=False, seed="s", plan_frame=studio._no_frame, step=studio._quiet_step)
    return asyncio.run(pattern_el.from_resolved(stored.elements[0], job)).ref


@needs_masks
def test_pattern_art_is_the_pattern_unmoved_past_the_frame(scene, paid):
    recipe = _hand_recipe(offset=(0.3, 0.0))
    got = asyncio.run(router.pattern_art(router.PatternArtRequest(
        run_id=1, target="scene_0", recipe=recipe.model_dump(mode="json"), element_id="pattern")))
    assert len(got["art_key"]) == 16 and got["art_rect"] is None
    win = got["art_window"]
    assert win["x"] < 0, "structure's art carries on left of the frame"
    img = cv2.imread(str(studio.resolve_file(got["art_url"].split("/file/", 1)[1])), cv2.IMREAD_UNCHANGED)
    assert img.shape[1] == round(win["w"] * SW) and img.shape[0] == round(win["h"] * SH)
    assert (img[..., :3] == 255).all() and img[..., 3].any(), "white: the stage colours it"
    again = asyncio.run(studio.pattern_art(1, "scene_0", recipe, "pattern"))
    assert again == got, "made once, then read back"
    with pytest.raises(Exception) as e:
        asyncio.run(studio.pattern_art(1, "scene_0", recipe, "nope"))
    assert getattr(e.value, "status_code", None) == 404
    full = rc.normalize(rc.Recipe(engine="house", target="scene_0", seed="studio:1:scene_0", elements=[
        rc.Element(id="pattern", kind="pattern", props={"slug": "blueprint"})]))
    bp = asyncio.run(studio.pattern_art(1, "scene_0", full, "pattern"))
    assert bp["art_rect"] is not None and bp["art_rect"]["x"] <= 0 and bp["art_rect"]["w"] >= 1.0
    assert paid == []


@needs_masks
@pytest.mark.skipif(not (STUDIO_815 / "c0_first.png").exists(), reason="run 815's frame is not on this machine")
def test_a_strip_dragged_over_the_head_is_warned_about_and_its_own_spot_is_not(tmp_path):
    """Run 815's real frame and speaker mask (read only): Structure moved to
    lie over the speaker's head is told so; at its automatic spot, nothing."""
    frame = STUDIO_815 / "c0_first.png"
    person = np.asarray(Image.open(frame.with_name("c0_first.person.png")).convert("L"), np.float32) / 255.0
    r = _built(tmp_path, "structure")
    assert studio._hand_info(r, frame, person)["warning"] is None
    over = dataclasses.replace(r, hand=_pivoted(r, rc.Hand(dx=0.3, dy=-0.2), 720, 1280))
    info = studio._hand_info(over, frame, person)
    assert info["warning"] and "head" in info["warning"], info
    gone = dataclasses.replace(r, hand=_pivoted(r, rc.Hand(dx=-0.9), 720, 1280))
    assert "off the picture" in (studio._hand_info(gone, frame, person)["warning"] or "")
