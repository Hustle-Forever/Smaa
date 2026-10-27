"""The in-house engine's plate (rebrand_harness.plate), pure: no ffmpeg, no
MODNet, no disk.

  identity    a pattern that has not moved is its art, byte for byte (the
              warp is skipped on a whole-pixel move: frame 0 of every
              camera track is the identity)
  over        premultiplied "over", back to front
  the cut     plate × (1 − matte), the matte the bank gives for frame i
              (the bars handed to it); the icons in front are never cut
  camera      a "room" layer is warped once by the camera, a "screen" one
              holds still
  motion      wipe edges, fades, slides, exits (an entrance played
              backwards), the seeded glitch, nothing before its start or
              after its end
  settled     the design still: fully in, only what shows at `at`
  house       the hand matrix, auto follow on the letterbox bars, the
              still's moment, and a grep gate: nothing in the engine's
              modules can reach a paid model"""
from __future__ import annotations

import re
from pathlib import Path

import numpy as np
import pytest

from app.services.rebrand_harness import house
from app.services.rebrand_harness import icon_render as ir
from app.services.rebrand_harness import plate as pl
from app.services.rebrand_harness import recipe as rc
from app.services.rebrand_harness.elements import pattern as pattern_el

cv2 = pytest.importorskip("cv2")

W, H, FPS = 64, 96, 30.0
VIOLET = (0x7F, 0x2B, 0xF2)


def _art(box=(10, 20, 30, 70), colour=VIOLET, alpha=255, w=W, h=H) -> np.ndarray:
    """A premultiplied layer: a solid rectangle of ink in `box`."""
    rgba = np.zeros((h, w, 4), np.uint8)
    x0, y0, x1, y1 = box
    rgba[y0:y1, x0:x1, :3] = colour
    rgba[y0:y1, x0:x1, 3] = alpha
    return pl.premultiplied(rgba)


def _timing(t_in=0.0, t_full=0.0, t_hold_end=10.0, t_out=10.0, entrance="none", exit="none", end=True):
    return pattern_el.Timing(t_in=t_in, t_full=t_full, t_hold_end=t_hold_end, t_out=t_out,
                             entrance=entrance, exit=exit, runs_to_end=end)


def _layer(art=None, box=(10, 20, 30, 70), **kw) -> pl.LayerSpec:
    art = _art(box) if art is None else art
    kw.setdefault("timing", _timing())
    return pl.LayerSpec(element_id=kw.pop("element_id", "pattern"), art=art,
                        ink_box=pl.ink_box(art[..., 3]) or box, **kw)


def _spec(*layers, n=60, stamper=None, bars=None, speaker=True) -> pl.PlateSpec:
    return pl.PlateSpec(width=W, height=H, fps=FPS, n=n, layers=tuple(layers), stamper=stamper, bars=bars,
                        speaker_front=speaker)


class Bank:
    """A matte bank that hands back one fixed matte, and remembers what it
    was asked (the frame, the bars)."""

    def __init__(self, m):
        self.m = np.asarray(m, np.float32)
        self.asked: list = []

    def at(self, i, *, bars=None):
        self.asked.append((i, bars))
        m = self.m.copy()
        if bars is not None:
            m[np.asarray(bars, bool)] = 0.0
        return m


class Camera:
    """A camera zooming about the frame's centre by 1 + z·i."""

    def __init__(self, z=0.01):
        self.z = z

    def at(self, i):
        s = 1.0 + self.z * i
        cx, cy = (W - 1) / 2, (H - 1) / 2
        return np.array([[s, 0, (1 - s) * cx], [0, s, (1 - s) * cy]], np.float32)


# ---------------------------------------------------------------------------
# Identity, over, the cut
# ---------------------------------------------------------------------------

def test_a_pattern_that_has_not_moved_is_its_art_byte_for_byte():
    ly = _layer()
    got = pl.plate_at(_spec(ly), 0, camera=Camera(0.02))
    assert np.array_equal(got, ly.art)
    # Frame 0 of a real track is the identity: the same with no camera.
    assert np.array_equal(pl.plate_at(_spec(ly), 7), ly.art)


def test_layers_are_laid_over_each_other_premultiplied_back_to_front():
    back = _layer(_art((5, 5, 40, 60), (255, 0, 0)), element_id="a")
    front = _layer(_art((20, 30, 50, 80), (0, 0, 255), alpha=128), element_id="b")
    got = pl.plate_at(_spec(back, front), 0).astype(np.float32)
    a = back.art.astype(np.float32)
    b = front.art.astype(np.float32)
    want = b + a * (1.0 - b[..., 3:4] / 255.0)
    assert np.abs(got - want).max() <= 0.5
    # Where the half-see-through front lies on the red: both, blended.
    assert tuple(got[40, 25].astype(int)) == (127, 0, 128, 255)


def test_the_speaker_is_cut_out_of_the_patterns_and_the_icons_behind_her_only():
    m = np.zeros((H, W), np.float32)
    m[:, :20] = 1.0
    m[:, 20:25] = 0.5
    bank = Bank(m)
    bars = np.zeros((H, W), bool)
    bars[:10] = True
    front = [{"glyph": {"source": "library", "id": "code"}, "label": "Code", "from_sec": 0.0, "settle_sec": 0.3,
              "to_sec": 1.5, "box": {"x": 0.0, "y": 0.6, "w": 0.4, "h": 0.25}, "fill": "#7f2bf2",
              "ink": "#ffffff", "fx": {"entrance": "none", "seed": "s"}, "style": "circle", "look_v": 1,
              "weight": "bold", "layer": "front"}]
    stamper = ir.Stamper(front, fps=FPS, frame_size=(W, H))
    ly = _layer(_art((0, 0, 40, 50)))
    got = pl.plate_at(_spec(ly, stamper=stamper, bars=bars), 3, bank=bank)
    assert bank.asked == [(3, bars)], "the bank is asked for this frame's matte, with the bars"
    assert got[30, 5, 3] == 0 and got[30, 22, 3] == 128 and got[30, 30, 3] == 255
    assert got[5, 5, 3] == 255, "nothing is cut on a letterbox bar"
    # The icon in front is drawn over the cut, whole.
    rgba, x, y = stamper.drawn(0.1, layer="front")[0]
    inside = rgba[..., 3] == 255
    ys, xs = np.nonzero(inside)
    assert (got[ys + y, xs + x, 3] == 255).all()
    # Switched off, nothing is cut.
    uncut = pl.plate_at(_spec(ly, speaker=False), 3, bank=Bank(m))
    assert uncut[30, 5, 3] == 255


def test_an_icon_behind_the_speaker_is_cut_with_the_patterns():
    m = np.ones((H, W), np.float32)
    behind = [{"glyph": {"source": "library", "id": "code"}, "label": "Code", "from_sec": 0.0, "settle_sec": 0.3,
               "to_sec": 1.5, "box": {"x": 0.1, "y": 0.1, "w": 0.4, "h": 0.25}, "fill": "#7f2bf2",
               "ink": "#ffffff", "fx": {"entrance": "none", "seed": "s"}, "style": "badge"}]
    stamper = ir.Stamper(behind, fps=FPS, frame_size=(W, H))
    assert pl.plate_at(_spec(stamper=stamper), 2)[..., 3].max() > 0
    assert pl.plate_at(_spec(stamper=stamper), 2, bank=Bank(m))[..., 3].max() == 0


# ---------------------------------------------------------------------------
# The camera
# ---------------------------------------------------------------------------

def test_a_room_layer_is_warped_once_by_the_camera_and_a_screen_one_holds_still():
    cam = Camera(0.01)
    room = _layer(_art((10, 20, 40, 70)))
    got = pl.plate_at(_spec(room), 10, camera=cam)
    want = cv2.warpAffine(room.art, cam.at(10).astype(np.float64), (W, H), flags=cv2.INTER_LINEAR,
                          borderMode=cv2.BORDER_CONSTANT, borderValue=(0, 0, 0, 0))
    assert np.abs(got.astype(int) - want.astype(int)).max() <= 1
    assert got[..., 3].astype(bool).sum() > room.art[..., 3].astype(bool).sum(), "it grows with the push-in"
    screen = _layer(_art((10, 20, 40, 70)), follow="screen")
    assert np.array_equal(pl.plate_at(_spec(screen), 10, camera=cam), screen.art)


def test_a_hand_move_is_part_of_the_one_warp():
    hand = house.hand_matrix(rc.Hand(dx=0.25), W, H)
    ly = _layer(_art((10, 20, 30, 70)), hand=hand)
    got = pl.plate_at(_spec(ly), 0)
    assert got[40, 10 + 16, 3] == 255 and got[40, 10, 3] == 0, "moved a quarter of the width"
    assert house.hand_matrix(rc.Hand(), W, H) is None
    turn = house.hand_matrix(rc.Hand(rotate=90.0, scale=2.0, pivot=(0.5, 0.5)), W, H)
    pivot = np.array([0.5 * W, 0.5 * H, 1.0])
    assert np.allclose(turn @ pivot, pivot[:2]), "it turns about its pivot"
    assert np.allclose(turn @ (pivot + [1, 0, 0]), pivot[:2] + [0, 2]), "clockwise on screen, sized x2"


# ---------------------------------------------------------------------------
# Motion
# ---------------------------------------------------------------------------

def _alpha_cols(arr):
    return (np.asarray(arr)[..., 3] > 0).any(axis=0)


def test_a_wipe_sweeps_the_ink_box_with_a_soft_edge():
    box = (10, 20, 50, 70)
    art = _art(box)
    assert pl.motion(art, ink_box=box, kind="left_to_right", direction="in", q=0.0, seed="s", frame=0) is None
    half = pl.motion(art, ink_box=box, kind="left_to_right", direction="in", q=0.3, seed="s", frame=9)
    cols = np.nonzero(_alpha_cols(half.art))[0]
    e = pl.shown_share("in", 0.3)
    lead = 10 - 0.12 * 40 + e * (40 + 2 * 0.12 * 40)
    assert cols[0] == 10 and abs(cols[-1] + 0.5 - lead) <= 1.0, "the lines show from the left end up to the edge"
    full = pl.motion(art, ink_box=box, kind="left_to_right", direction="in", q=1.0, seed="s", frame=30)
    assert full.art is art, "fully in: the art itself"
    back = pl.motion(art, ink_box=box, kind="right_to_left", direction="in", q=0.3, seed="s", frame=9)
    cols = np.nonzero(_alpha_cols(back.art))[0]
    assert cols[-1] == 49 and cols[0] > 10, "from the right end"
    down = pl.motion(art, ink_box=box, kind="top_to_bottom", direction="in", q=0.3, seed="s", frame=9)
    rows = np.nonzero((down.art[..., 3] > 0).any(axis=1))[0]
    assert rows[0] == 20 and rows[-1] < 69


def test_an_exit_is_its_entrance_played_backwards():
    box = (10, 20, 50, 70)
    art = _art(box)
    for q in (0.2, 0.5, 0.8):
        e = pl.shown_share("out", q)
        out = pl.motion(art, ink_box=box, kind="to_left", direction="out", q=q, seed="s", frame=0)
        ramp = pl.wipe_ramp(W, 10, 50, e, from_high=False)
        want = art.astype(np.float32) * ramp[None, :, None]
        assert np.array_equal(out.art, want)
    assert pl.shown_share("out", 0.0) == 1.0 and pl.shown_share("out", 1.0) == 0.0
    assert pl.shown_share("in", 0.0) == 0.0 and pl.shown_share("in", 1.0) == 1.0
    fade = pl.motion(art, ink_box=box, kind="fade", direction="out", q=0.5, seed="s", frame=0)
    assert np.allclose(fade.art, art.astype(np.float32) * pl.shown_share("out", 0.5))
    assert pl.motion(art, ink_box=box, kind="fade", direction="out", q=1.0, seed="s", frame=0) is None


def test_a_slide_comes_in_from_the_edge_it_grows_from():
    box = (0, 20, 30, 70)
    art = _art(box)
    first = pl.motion(art, ink_box=box, kind="slide", direction="in", q=0.0, seed="s", frame=0, edge="left")
    assert first.art is art and first.dx == -30 and first.dy == 0, "its ink just past the left edge"
    done = pl.motion(art, ink_box=box, kind="slide", direction="in", q=1.0, seed="s", frame=0, edge="left")
    assert (done.dx, done.dy) == (0.0, 0.0)
    right = pl.motion(art, ink_box=(40, 20, 64, 70), kind="slide", direction="in", q=0.0, seed="s", frame=0,
                      edge="right", frame_size=(W, H))
    assert right.dx == W - 40
    out = pl.motion(art, ink_box=box, kind="slide_out", direction="out", q=1.0, seed="s", frame=0, edge="left")
    assert out.dx == -30
    ly = _layer(art, timing=_timing(t_full=1.0, entrance="slide"), edge="left")
    assert pl.plate_at(_spec(ly), 0)[..., 3].max() == 0, "off the frame at its first frame"
    assert np.array_equal(pl.plate_at(_spec(ly), 30), art), "home once it is in"
    assert house.slide_edge(pattern_el.pattern_ref.Layout(placement="right")) == "right"
    assert house.slide_edge(pattern_el.pattern_ref.Layout(placement="top", anchor="right")) == "right"
    assert house.slide_edge(pattern_el.pattern_ref.Layout(placement="full", fit="cover")) == "left"


def test_the_glitch_is_seeded_and_hands_over_to_the_art():
    box = (10, 20, 50, 70)
    art = _art(box)
    frames = [pl.motion(art, ink_box=box, kind="glitch", direction="in", q=q, seed="s", frame=f)
              for f, q in ((4, 0.4), (10, 0.6))]
    again = [pl.motion(art, ink_box=box, kind="glitch", direction="in", q=q, seed="s", frame=f)
             for f, q in ((4, 0.4), (10, 0.6))]
    for a, b in zip(frames, again):
        assert (a is None) == (b is None)
        if a is not None:
            assert np.array_equal(a.art, b.art) and a.box == b.box
    other = [pl.motion(art, ink_box=box, kind="glitch", direction="in", q=0.4, seed=f"t{k}", frame=4)
             for k in range(4)]
    assert any(o is not None and (frames[0] is None or not np.array_equal(o.art, frames[0].art)) for o in other)
    assert pl.motion(art, ink_box=box, kind="glitch", direction="in", q=1.0, seed="s", frame=30).art is art
    got = next(m for m in frames + other if m is not None)
    assert got.box[0] < box[0] and got.box[2] > box[2], "its slices may jump past the ink box"


def test_nothing_shows_before_its_start_or_from_its_end():
    art = _art()
    ly = _layer(art, timing=_timing(t_in=0.5, t_full=1.0, t_hold_end=1.4, t_out=1.8, entrance="fade",
                                    exit="fade", end=False))
    spec = _spec(ly)
    assert pl.plate_at(spec, 14)[..., 3].max() == 0            # 0.47 s
    assert 0 < pl.plate_at(spec, 20)[..., 3].max() < 255        # coming in
    assert np.array_equal(pl.plate_at(spec, 36), art)           # holding
    assert 0 < pl.plate_at(spec, 48)[..., 3].max() < 255        # going out
    assert pl.plate_at(spec, 54)[..., 3].max() == 0             # 1.8 s: gone
    assert _layer(art, timing=None) and pl.plate_at(_spec(_layer(art, timing=None)), 3)[..., 3].max() == 0


# ---------------------------------------------------------------------------
# The design still
# ---------------------------------------------------------------------------

def test_the_design_still_shows_what_shows_then_fully_in():
    a = _layer(_art((5, 5, 20, 40)), element_id="a",
               timing=_timing(t_in=0.0, t_full=1.0, t_hold_end=1.5, t_out=1.5, entrance="left_to_right",
                              exit="none", end=False))
    b = _layer(_art((40, 50, 60, 90)), element_id="b", timing=_timing(t_in=1.5, t_full=2.5, entrance="glitch"))
    spec = _spec(a, b)
    every = pl.plate_at(spec, 0, settled=True)
    assert every[20, 10, 3] == 255 and every[70, 50, 3] == 255, "no `at`: every layer, fully in"
    early = pl.plate_at(spec, 0, settled=True, at=0.2)
    assert early[20, 10, 3] == 255 and early[70, 50, 3] == 0
    late = pl.plate_at(spec, 0, settled=True, at=3.0)
    assert late[20, 10, 3] == 0 and late[70, 50, 3] == 255
    assert house.still_moment(spec) == 2.5, "the longest stretch with the most patterns fully in"
    frame = np.full((H, W, 3), 40, np.uint8)
    still = pl.over_frame(frame, late)
    assert tuple(still[70, 50]) == (0xF2, 0x2B, 0x7F) and tuple(still[20, 10]) == (40, 40, 40)


def test_straight_and_premultiplied_round_trip():
    rgba = np.zeros((4, 4, 4), np.uint8)
    rgba[..., :3] = (200, 100, 50)
    rgba[..., 3] = np.array([[0, 64, 128, 255]] * 4, np.uint8)
    back = pl.straight(pl.premultiplied(rgba))
    assert np.array_equal(back[..., 3], rgba[..., 3])
    assert np.abs(back[..., :3].astype(int) - rgba[..., :3].astype(int))[rgba[..., 3] > 0].max() <= 2
    assert (back[..., :3][rgba[..., 3] == 0] == 0).all()


# ---------------------------------------------------------------------------
# The house's choices
# ---------------------------------------------------------------------------

def test_auto_follow_holds_a_pattern_on_the_bars_still():
    el = rc.Element(id="pattern", kind="pattern", props={"slug": "structure"})
    bars = np.zeros((H, W), bool)
    bars[:30] = True
    on_bars = np.zeros((H, W), np.uint8)
    on_bars[:28, 5:60] = 255
    in_room = np.zeros((H, W), np.uint8)
    in_room[40:80, 5:60] = 255
    assert house.resolve_follow(el, on_bars, bars) == "screen"
    assert house.resolve_follow(el, in_room, bars) == "room"
    assert house.resolve_follow(el, on_bars, None) == "room"
    own = el.model_copy(update={"props": {"slug": "structure", "follow": "room"}})
    assert house.resolve_follow(own, on_bars, bars) == "room"


ENGINE = ["house.py", "plate.py", "mattes.py", "camera.py"]
PAID = re.compile(r"\b(gemini_image|gemini_multimodal|gemini_embed|person_mask|edit_video|interpolate|"
                  r"build_brief|critique|store_memory)\s*\(")


@pytest.mark.parametrize("name", ENGINE)
def test_nothing_in_the_engine_can_reach_a_paid_model(name):
    src = (Path(__file__).resolve().parents[1] / "app" / "services" / "rebrand_harness" / name).read_text(
        encoding="utf-8")
    assert not PAID.findall(src), f"{name} names a paid call"
