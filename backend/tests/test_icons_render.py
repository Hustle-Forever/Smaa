"""Icon pixels (rebrand_harness/icon_render.py): the Phosphor rasteriser,
the 42 badge, the automatic colours, the entrances and the Stamper.

No model is ever called here: an autouse guard makes the paid Vertex calls
raise, and every test is pure numpy / PIL.
"""
from __future__ import annotations

import hashlib
import io
import math
import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.rebrand_harness import icon_catalog as ic  # noqa: E402
from app.services.rebrand_harness import icon_render as ir  # noqa: E402
from app.services.rebrand_harness import label_font as lf  # noqa: E402


@pytest.fixture(autouse=True)
def _no_paid_calls(monkeypatch):
    """Any Gemini / Nano Banana call made by the code under test fails the test."""
    from app.services import vertex_client

    calls: list[str] = []

    def refuse(name):
        async def _refuse(*a, **k):
            calls.append(name)
            raise AssertionError(f"paid call to vertex_client.{name} in a test")
        return _refuse

    monkeypatch.setattr(vertex_client, "gemini_multimodal", refuse("gemini_multimodal"))
    monkeypatch.setattr(vertex_client, "gemini_image", refuse("gemini_image"))
    yield
    assert not calls, f"unexpected paid calls: {calls}"


def _holes(alpha: np.ndarray) -> int:
    """Transparent regions that do not reach the image border."""
    import cv2

    n, lab = cv2.connectedComponents((alpha < 0.5).astype(np.uint8), connectivity=4)
    border = set(lab[0]) | set(lab[-1]) | set(lab[:, 0]) | set(lab[:, -1])
    return sum(1 for i in range(1, n) if i not in border)


# ---------------------------------------------------------------------------
# path_alpha
# ---------------------------------------------------------------------------

def test_code_glyph_keeps_its_holes():
    a = ir.path_alpha(ic.library_d("code"), 128)
    assert a.shape == (128, 128) and a.dtype == np.float32
    assert a[26, 64] == pytest.approx(1.0)          # the rounded square's body
    assert a[64, 64] == pytest.approx(0.0)          # inside the "/" of </>
    assert _holes(a) == 3                           # "<", "/" and ">" are holes


def test_nonzero_winding_not_even_odd():
    # Two overlapping squares drawn the same way round: nonzero fills the
    # overlap (even-odd would punch it out).
    same = "M20,20 H140 V140 H20 Z M100,100 H236 V236 H100 Z"
    a = ir.path_alpha(same, 64)
    assert a[30, 30] == pytest.approx(1.0)          # the overlap, at 120,120 in path units
    # A square inside another, the other way round: a hole.
    hole = "M20,20 H236 V236 H20 Z M80,80 V176 H176 V80 Z"
    b = ir.path_alpha(hole, 64)
    assert b[8, 8] == pytest.approx(1.0) and b[32, 32] == pytest.approx(0.0)
    assert _holes(b) == 1


def test_antialiased_edges_and_exact_coverage():
    # A square covering x 0..128.5 of 256 at 64 px: column 32 is half covered.
    a = ir.path_alpha("M0,0 H130 V256 H0 Z", 64)
    assert a[10, 31] == pytest.approx(1.0)
    assert 0.3 < a[10, 32] < 0.7
    assert a[10, 34] == pytest.approx(0.0)


def test_path_alpha_is_cached_and_read_only():
    d = ic.library_d("clock")
    a, b = ir.path_alpha(d, 48), ir.path_alpha(d, 48)
    assert a is b
    with pytest.raises(ValueError):
        a[0, 0] = 1.0
    assert _holes(a) >= 1                           # the face is cut out of the disc


def test_every_catalogue_icon_rasterises_with_sane_coverage():
    bad = []
    for icon_id in ic.library_ids():
        cov = float(ir.path_alpha(ic.library_d(icon_id), 64).mean())
        if not 0.05 <= cov <= 0.95:
            bad.append((icon_id, round(cov, 3)))
    assert not bad, bad


def test_glyph_alpha_takes_every_form():
    d = ic.library_d("heart")
    ref = ir.glyph_alpha(d, 32)
    assert np.array_equal(ir.glyph_alpha(("svg", d), 32), ref)
    png = io.BytesIO()
    from PIL import Image

    rgba = np.zeros((64, 64, 4), np.uint8)
    rgba[16:48, 16:48, 3] = 255
    Image.fromarray(rgba, "RGBA").save(png, format="PNG")
    a = ir.glyph_alpha(("png", png.getvalue()), 32)
    assert a[16, 16] == pytest.approx(1.0) and a[2, 2] == pytest.approx(0.0)
    # A PNG with no transparency reads as black ink on white.
    flat = np.full((64, 64, 3), 255, np.uint8)
    flat[16:48, 16:48] = 0
    png2 = io.BytesIO()
    Image.fromarray(flat, "RGB").save(png2, format="PNG")
    assert ir.glyph_alpha(png2.getvalue(), 32)[16, 16] == pytest.approx(1.0)
    assert ir.glyph_alpha(np.ones((10, 10), np.float32), 20).shape == (20, 20)
    with pytest.raises(TypeError):
        ir.glyph_alpha(42, 10)


# ---------------------------------------------------------------------------
# Sprites
# ---------------------------------------------------------------------------

def test_badge_is_the_iconbadge_port():
    L = 100
    s = ir.badge_sprite(ic.library_d("code"), L, "#7f2bf2", "#ffffff")
    assert s.shape == (L, L, 4) and s.dtype == np.uint8
    # The 45-degree cut: the top-right corner is empty, legs of 0.24 L.
    assert s[1, L - 2, 3] == 0 and s[5, L - 5, 3] == 0      # x - y > 0.76 L: cut away
    assert s[30, L - 2, 3] == 255                   # below the cut
    assert s[1, L - 30, 3] == 255                   # left of the cut
    # The other three corners are square and filled with the fill colour.
    for y, x in ((0, 0), (L - 1, 0), (L - 1, L - 1)):
        assert tuple(s[y, x]) == (0x7F, 0x2B, 0xF2, 255)
    # The glyph sits in the centred 0.6 L box, in the ink colour: the body
    # of the code square is white, its "/" shows the fill through.
    assert tuple(s[20 + int(0.6 * L * 26 / 128), 50, :3]) == (255, 255, 255)
    assert tuple(s[50, 50, :3]) == (0x7F, 0x2B, 0xF2)
    assert s[:, :, 3][19, 50] == 255 and tuple(s[10, 50, :3]) == (0x7F, 0x2B, 0xF2)


def test_badge_pixels_are_deterministic():
    ir._SPRITES.clear()
    ir._path_alpha_cached.cache_clear()
    a = ir.badge_sprite(ic.library_d("piggy-bank"), 96, "#303030", "#ffffff").tobytes()
    ir._path_alpha_cached.cache_clear()
    b = ir.badge_sprite(ic.library_d("piggy-bank"), 96, "#303030", "#ffffff").tobytes()
    assert hashlib.sha256(a).hexdigest() == hashlib.sha256(b).hexdigest()


def test_badge_from_a_custom_png_glyph():
    from PIL import Image

    rgba = np.zeros((256, 256, 4), np.uint8)
    rgba[64:192, 64:192, 3] = 255
    buf = io.BytesIO()
    Image.fromarray(rgba, "RGBA").save(buf, format="PNG")
    s = ir.badge_sprite(("png", buf.getvalue()), 100, "#303030", "#ffffff")
    assert tuple(s[50, 50, :3]) == (255, 255, 255)     # the glyph's middle
    assert tuple(s[22, 22, :3]) == (0x30, 0x30, 0x30)  # the glyph's margin: fill


def test_glyph_style_is_the_glyph_with_a_soft_halo():
    L = 100
    s = ir.glyph_sprite(ic.library_d("heart"), L, "#7f2bf2", "#ffffff")
    assert s.shape == (L, L, 4)
    # The heart itself takes the fill: pick_colours checked THAT colour
    # against the background, and with no badge the glyph is what must show.
    assert tuple(s[50, 50]) == (0x7F, 0x2B, 0xF2, 255)
    assert s[0, 0, 3] == 0                              # nothing in the far corner
    # Just outside the heart's side: the halo, in the ink, at most 35 %.
    a = ir.path_alpha(ic.library_d("heart"), 80)
    row = 40
    edge = 10 + int(np.flatnonzero(a[row] > 0.5)[0])
    halo = s[row + 10, edge - 3]
    assert 0 < halo[3] <= round(0.35 * 255) + 1
    assert tuple(halo[:3]) == (255, 255, 255)
    assert ir.sprite("glyph", ic.library_d("heart"), L, "#7f2bf2", "#ffffff").tobytes() == s.tobytes()


# ---------------------------------------------------------------------------
# Colour
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("lab1, lab2, want", [
    ((50.0, 2.6772, -79.7751), (50.0, 0.0, -82.7485), 2.0425),
    ((50.0, 3.1571, -77.2803), (50.0, 0.0, -82.7485), 2.8615),
    ((50.0, 2.8361, -74.0200), (50.0, 0.0, -82.7485), 3.4412),
    ((50.0, -1.3802, -84.2814), (50.0, 0.0, -82.7485), 1.0000),
    ((50.0, 0.0, 0.0), (50.0, -1.0, 2.0), 2.3669),
    ((2.0776, 0.0795, -1.1350), (0.9033, -0.0636, -0.5514), 0.9082),
    ((63.0109, -31.0961, -5.8663), (62.8187, -29.7946, -4.0864), 1.2630),
    ((61.2901, 3.7196, -5.3901), (61.4292, 2.2480, -4.9620), 1.8731),
])
def test_ciede2000_matches_sharmas_reference(lab1, lab2, want):
    assert float(ir.delta_e2000(lab1, lab2)) == pytest.approx(want, abs=1e-4)
    assert float(ir.delta_e2000(lab2, lab1)) == pytest.approx(want, abs=1e-4)


def test_lab_and_contrast():
    assert ir.srgb_to_lab([255, 255, 255]) == pytest.approx([100.0, 0.0, 0.0], abs=0.01)
    assert ir.srgb_to_lab([0, 0, 0]) == pytest.approx([0.0, 0.0, 0.0], abs=0.01)
    assert ir.contrast_ratio("#ffffff", "#000000") == pytest.approx(21.0)
    assert ir.contrast_ratio("#ffffff", "#7f2bf2") > 3.0
    assert ir.hex_colour((127, 43, 242)) == "#7f2bf2" and ir.hex_colour("#FFF") == "#ffffff"


def test_light_room_gets_the_pattern_colour_with_white_ink():
    got = ir.pick_colours(["#e8e4dc", "#d9d4cc", "#f2efe9"], "#7f2bf2", 0.0)
    assert (got["fill"], got["ink"], got["warning"]) == ("#7f2bf2", "#ffffff", None)
    assert got["why"]
    # The pattern's own colour comes first even when it is not a brand one.
    teal = ir.pick_colours(["#e8e4dc"], "#1f6f78", 0.0)
    assert teal["fill"] == "#1f6f78" and teal["ink"] == "#ffffff"
    assert "pattern" in teal["why"].lower()


def test_spot_over_a_violet_pattern_goes_charcoal():
    got = ir.pick_colours(["#7a2ee8", "#8330f0"], "#7f2bf2", 0.3)
    assert (got["fill"], got["ink"]) == ("#303030", "#ffffff")
    assert "pattern" in got["why"]


def test_pattern_overlap_under_ten_percent_does_not_count():
    # Background far from violet, pattern violet but barely touching the icon.
    assert ir.pick_colours(["#1a1a1a"], "#7f2bf2", 0.05)["fill"] == "#7f2bf2"
    # Per-sample overlaps: one spot on the pattern is enough to rule violet out.
    got = ir.pick_colours(["#1a1a1a", "#eeeeee"], "#7f2bf2", [0.0, 0.5])
    assert got["fill"] not in ("#7f2bf2", "#8d01fa")


def test_white_fill_takes_the_pattern_colour_as_ink():
    got = ir.pick_colours(["#7f2bf2", "#303030"], "#7f2bf2", 0.0)
    assert (got["fill"], got["ink"]) == ("#ffffff", "#7f2bf2")


def test_ink_turns_charcoal_on_a_light_fill():
    got = ir.pick_colours(["#1b3a4b"], "#f69287", 0.0)
    assert got["fill"] == "#f69287" and got["ink"] == "#303030"


def test_one_palette_must_work_on_every_spot():
    # Violet is fine on the wall but not on the second spot: it is refused.
    got = ir.pick_colours(["#e8e4dc", "#7c2cef"], None, 0.0)
    assert got["fill"] == "#303030"


def test_no_colour_fits_everywhere_warns():
    got = ir.pick_colours(["#f0f0f0", "#7f2bf2", "#303030", "#ffffff"], None, 0.0)
    assert got["warning"] and got["fill"] in ("#7f2bf2", "#8d01fa", "#303030", "#ffffff")


def test_a_picked_colour_is_kept_with_a_warning_when_weak():
    got = ir.pick_colours(["#353535"], "#7f2bf2", 0.0, colour="#303030")
    assert got["fill"] == "#303030" and got["why"] == "Your colour" and got["warning"]
    ok = ir.pick_colours(["#eeeeee"], None, 0.0, colour="#303030")
    assert ok["warning"] is None


def test_background_samples_in_lab():
    labs = ir.srgb_to_lab(np.array([[232, 228, 220], [217, 212, 204]], dtype=float))
    a = ir.pick_colours(labs, "#7f2bf2", 0.0, space="lab")
    b = ir.pick_colours([{"lab": labs[0]}, {"rgb": (217, 212, 204)}], "#7f2bf2", 0.0)
    c = ir.pick_colours(["#e8e4dc", "#d9d4cc"], "#7f2bf2", 0.0)
    assert a["fill"] == b["fill"] == c["fill"] == "#7f2bf2"
    # One bare colour in any form, and no samples at all.
    for one in ("#e8e4dc", (232, 228, 220), np.array([232, 228, 220]), {"rgb": (232, 228, 220)}):
        assert ir.pick_colours(one, "#7f2bf2", 0.0)["fill"] == "#7f2bf2"
    assert ir.pick_colours([], None)["fill"] == "#7f2bf2"


# ---------------------------------------------------------------------------
# Entrances
# ---------------------------------------------------------------------------

def _badge(L=64):
    return ir.badge_sprite(ic.library_d("code"), L, "#7f2bf2", "#ffffff")


def test_glitch_in_eight_frames_out_six_and_deterministic():
    s = _badge()
    span = 60 / 30
    frames = [ir.fx("glitch", f / 30, s, "seed-a", span=span) for f in range(60)]
    assert ir.fx("glitch", -1 / 30, s, "seed-a", span=span) is None
    assert ir.fx("glitch", 60 / 30, s, "seed-a", span=span) is None
    # Settled from frame 7 (the 8th) to the exit.
    for f in range(7, 54):
        assert frames[f] is not None and frames[f].rgba is s and (frames[f].dx, frames[f].dy) == (0, 0)
    # The entrance and exit frames are torn (or flicker off), never the clean sprite.
    for f in list(range(0, 7)) + list(range(55, 60)):
        assert frames[f] is None or frames[f].rgba.tobytes() != s.tobytes()
    again = [ir.fx("glitch", f / 30, s, "seed-a", span=span) for f in range(60)]
    for x, y in zip(frames, again):
        assert (x is None) == (y is None)
        if x is not None:
            assert x.rgba.tobytes() == y.rgba.tobytes() and (x.dx, x.dy) == (y.dx, y.dy)
    other = [ir.fx("glitch", f / 30, s, "seed-b", span=span) for f in range(8)]
    assert any((x is None) != (y is None) or (x is not None and x.rgba.tobytes() != y.rgba.tobytes())
               for x, y in zip(frames[:8], other))


def test_glitch_splits_red_and_cyan():
    s = _badge()
    got = [ir.fx("glitch", f / 30, s, "rgb", span=2.0) for f in range(3)]
    pix = np.concatenate([g.rgba.reshape(-1, 4) for g in got if g is not None])
    rgb = pix[pix[:, 3] > 0][:, :3].astype(int)
    reddish = (rgb[:, 0] > 200) & (rgb[:, 1] < 80)
    cyanish = (rgb[:, 0] < 80) & (rgb[:, 1] > 180) & (rgb[:, 2] > 200)
    assert reddish.any() and cyanish.any()


def test_pop_fade_slide_and_none():
    s = _badge()
    p0 = ir.fx("pop", 0.0, s, "x", span=2.0)
    assert p0.rgba.shape[0] < s.shape[0] and p0.dx > 0          # smaller, centred
    assert ir.fx("pop", 8 / 30, s, "x", span=2.0).rgba is s
    fades = [ir.fx("fade", f / 30, s, "x", span=2.0).rgba[..., 3].max() for f in range(8)]
    assert fades == sorted(fades) and fades[0] < 255 and fades[-1] == 255
    out = [ir.fx("fade", f / 30, s, "x", span=2.0).rgba[..., 3].max() for f in range(54, 60)]
    assert out == sorted(out, reverse=True) and out[-1] < 255
    sl = ir.fx("slide", 0.0, s, "x", span=2.0, edge="left", travel=100)
    assert sl.dx < -30 and sl.dy == 0                            # easeOutExpo: most of it at once
    assert (ir.fx("slide", 8 / 30, s, "x", span=2.0, edge="left", travel=100).dx) == 0
    assert ir.fx("slide", 0.0, s, "x", span=2.0, edge="bottom", travel=100).dy > 30
    for f in (0, 30, 59):
        assert ir.fx("none", f / 30, s, "x", span=2.0).rgba is s
    for f in range(8):
        a, g = ir.fx("auto", f / 30, s, "x", span=2.0), ir.fx("glitch", f / 30, s, "x", span=2.0)
        assert (a is None) == (g is None)
        assert a is None or (a.rgba.tobytes(), a.dx) == (g.rgba.tobytes(), g.dx)
    with pytest.raises(ValueError):
        ir.fx("spin", 0.0, s, "x", span=2.0)


def test_nearest_edge():
    assert ir.nearest_edge((20, 500, 100, 100), (720, 1280)) == ("left", 120)
    assert ir.nearest_edge((600, 500, 100, 100), (720, 1280)) == ("right", 120)
    assert ir.nearest_edge((300, 10, 100, 100), (720, 1280)) == ("top", 110)
    assert ir.nearest_edge((300, 1170, 100, 100), (720, 1280)) == ("bottom", 110)


# ---------------------------------------------------------------------------
# Stamper
# ---------------------------------------------------------------------------

def _item(**kw):
    it = {"glyph": {"source": "library", "id": "code"}, "label": "Developer",
          "from_sec": 1.0, "settle_sec": 1.0 + 8 / 30, "to_sec": 3.0,
          "box": {"x": 0.1, "y": 0.2, "w": 60 / 320, "h": 60 / 180},
          "fill": "#7f2bf2", "ink": "#ffffff", "fx": {"entrance": "glitch", "seed": "s1"},
          "style": "badge", "word": "coding", "purpose": "Says coding"}
    it.update(kw)
    return it


def _frame(W=320, H=180, v=(40, 90, 160)):
    f = np.zeros((H, W, 3), np.uint8)
    f[:] = v
    return f


def test_stamper_draws_only_while_on_and_only_in_its_box():
    st = ir.Stamper([_item()], fps=30, offset_sec=0.0, frame_size=(320, 180))
    f = _frame()
    assert st.under(f, 0.5) is f and st.active(0.5) == []
    assert st.under(f, 3.0) is f                               # to_sec is exclusive
    assert st.active(2.0) == [0]
    x0, y0, x1, y1 = st.boxes(2.0)[0]
    assert (x1 - x0, y1 - y0) == (60, 60)                      # square, in px
    out = st.under(f, 2.0)
    assert out is not f and np.array_equal(f, _frame())        # the input is not touched
    changed = np.any(out != f, axis=-1)
    ys, xs = np.nonzero(changed)
    assert ys.min() >= y0 and ys.max() < y1 and xs.min() >= x0 and xs.max() < x1
    # Settled: exactly the badge sprite composited (BGR), opaque parts equal the sprite.
    spr = st.sprite_for(0)
    region = out[y0:y1, x0:x1]
    opaque = spr[..., 3] == 255
    assert np.array_equal(region[opaque], spr[..., 2::-1][opaque])


def test_stamper_uses_the_chunk_offset():
    st = ir.Stamper([_item()], fps=30, offset_sec=2.0, frame_size=(320, 180))
    assert st.active(0.5) == [0] and st.active(1.0) == []
    first = ir.Stamper([_item()], fps=30, offset_sec=0.0, frame_size=(320, 180))
    assert np.array_equal(st.under(_frame(), 0.5), first.under(_frame(), 2.5))


def test_stamper_is_deterministic_across_instances():
    items = [_item(), _item(glyph={"source": "library", "id": "piggy-bank"}, from_sec=3.3,
                            settle_sec=3.3 + 8 / 30, to_sec=5.0, fx={"entrance": "pop", "seed": "s2"},
                            style="glyph")]
    a = ir.Stamper(items, fps=30, frame_size=(320, 180))
    b = ir.Stamper(items, fps=30, frame_size=(320, 180))
    for t in np.arange(0, 5.5, 1 / 30):
        fa, fb = a.under(_frame(), float(t)), b.under(_frame(), float(t))
        assert np.array_equal(fa, fb)


def test_stamper_takes_custom_glyphs_and_needs_them():
    from PIL import Image

    rgba = np.zeros((256, 256, 4), np.uint8)
    rgba[48:208, 48:208, 3] = 255
    buf = io.BytesIO()
    Image.fromarray(rgba, "RGBA").save(buf, format="PNG")
    item = _item(glyph={"source": "custom", "id": "c-0123456789ab"})
    with pytest.raises(KeyError):
        ir.Stamper([item], frame_size=(320, 180))
    st = ir.Stamper([item], frame_size=(320, 180), glyphs={"c-0123456789ab": ("png", buf.getvalue())})
    x0, y0, x1, y1 = st.boxes(2.0)[0]
    out = st.under(_frame(), 2.0)
    cy, cx = (y0 + y1) // 2, (x0 + x1) // 2
    assert tuple(out[cy, cx]) == (255, 255, 255)               # the custom glyph, in the ink


def test_stamper_entrance_frames_and_clipping():
    # A slide from the left edge starts outside the frame and is clipped.
    it = _item(box={"x": 0.0, "y": 0.2, "w": 60 / 320, "h": 60 / 180},
               fx={"entrance": "slide", "seed": "s"})
    st = ir.Stamper([it], fps=30, frame_size=(320, 180))
    first = st.under(_frame(), 1.0)
    settled = st.under(_frame(), 1.0 + 8 / 30)
    assert np.any(first != settled)
    assert st.frame_of(0, 1.0).dx < 0 and st.frame_of(0, 1.0 + 8 / 30).dx == 0
    assert st.frame_of(0, 0.5) is None
    # boxes(drawn=True): where the pixels really are this frame, clipped.
    x0, y0, x1, y1 = st.boxes(1.0, drawn=True)[0]
    assert x0 == 0 and x1 < 60 and (y0, y1) == (36, 96)
    assert st.boxes(1.0 + 8 / 30, drawn=True) == st.boxes(1.0 + 8 / 30) == [(0, 36, 60, 96)]
    # Every pixel the frame changed lies inside the drawn box.
    ys, xs = np.nonzero(np.any(first != _frame(), axis=-1))
    assert xs.max() < x1 and ys.min() >= y0 and ys.max() < y1
    # A glitch tears wider than the settled box.
    g = ir.Stamper([_item()], fps=30, frame_size=(320, 180))
    wide = [b for f in range(30, 37) for b in g.boxes(f / 30, drawn=True)]
    assert any(b[2] - b[0] > 60 for b in wide)


def test_stamper_accepts_models_and_auto_entrance():
    class Obj:
        def __init__(self, d):
            self.__dict__.update(d)

    it = _item(fx={"entrance": "auto", "seed": "s1"})
    a = ir.Stamper([Obj(it)], frame_size=(320, 180))
    b = ir.Stamper([_item()], frame_size=(320, 180))
    for t in (1.0, 1.1, 2.0, 2.9):
        assert np.array_equal(a.under(_frame(), t), b.under(_frame(), t))


# ---------------------------------------------------------------------------
# Old versions render the same (the lock), E / H, drawn / under / over
# ---------------------------------------------------------------------------

# sha256[:16] of today's sprites at L 115, 42 violet on white — computed on
# HEAD 0f06454 before the circle and the chip existed (icon_look §2.7).
OLD_SPRITES = {
    ("badge", "code"): "01869817e9452efa", ("glyph", "code"): "755376b1d919730f",
    ("badge", "piggy-bank"): "03f77de0682b0c26", ("glyph", "piggy-bank"): "9f5399dfbf123a58",
    ("badge", "clock"): "577c5237d3fe8209", ("glyph", "clock"): "c49a6a235d51bd46",
    ("badge", "users-three"): "7bc72e8966da09df", ("glyph", "users-three"): "eafee39d2b3434e1",
}


@pytest.mark.parametrize("style, gid", sorted(OLD_SPRITES))
def test_old_sprites_are_locked(style, gid):
    s = ir.sprite(style, ic.library_d(gid), 115, "#7f2bf2", "#ffffff")
    assert hashlib.sha256(s.tobytes()).hexdigest()[:16] == OLD_SPRITES[(style, gid)]
    got = ir.make_sprite(style, ic.library_d(gid), 115, "#7f2bf2", "#ffffff")
    assert (got.pad, got.w, got.h) == (0, 115, 115) and got.rgba.tobytes() == s.tobytes()


# Every entrance, both old styles, square and tall boxes, a box on the edge,
# two chunk rates and offsets: the old Stamper's frames, boxes and indices,
# hashed on HEAD 0f06454 before the w x h bodies, drawn() and the layers.
_LOCK_W, _LOCK_H = 320, 180
_LOCK_ITEMS = [
    {"glyph": {"source": "library", "id": "code"}, "from_sec": 0.2, "settle_sec": 0.2 + 8 / 30, "to_sec": 1.4,
     "box": {"x": 0.1, "y": 0.2, "w": 60 / 320, "h": 60 / 180}, "fill": "#7f2bf2", "ink": "#ffffff",
     "fx": {"entrance": "glitch", "seed": "s1"}, "style": "badge"},
    {"glyph": {"source": "library", "id": "piggy-bank"}, "from_sec": 0.5, "settle_sec": 0.5 + 8 / 30, "to_sec": 1.9,
     "box": {"x": 0.55, "y": 0.1, "w": 0.2, "h": 0.4}, "fill": "#303030", "ink": "#ffffff",
     "fx": {"entrance": "pop", "seed": "s2"}, "style": "glyph"},
    {"glyph": {"source": "library", "id": "clock"}, "from_sec": 1.0, "settle_sec": 1.0 + 8 / 30, "to_sec": 2.2,
     "box": {"x": 0.7, "y": 0.55, "w": 0.15, "h": 0.3}, "fill": "#8d01fa", "ink": "#ffffff",
     "fx": {"entrance": "fade", "seed": "s3"}, "style": "badge"},
    {"glyph": {"source": "library", "id": "users-three"}, "from_sec": 0.0, "settle_sec": 8 / 30, "to_sec": 1.2,
     "box": {"x": 0.0, "y": 0.6, "w": 0.18, "h": 0.33}, "fill": "#ffffff", "ink": "#7f2bf2",
     "fx": {"entrance": "slide", "seed": "s4"}, "style": "badge"},
    {"glyph": {"source": "library", "id": "heart"}, "from_sec": 1.5, "to_sec": 2.6,
     "box": {"x": 0.4, "y": 0.02, "w": 0.13, "h": 0.2}, "fill": "#7f2bf2", "ink": "#ffffff",
     "fx": {"entrance": "none", "seed": "s5"}, "style": "badge"},
]


def _lock_frame():
    f = np.zeros((_LOCK_H, _LOCK_W, 3), np.uint8)
    f[..., 0] = np.arange(_LOCK_W, dtype=np.uint8)[None, :] // 2
    f[..., 1] = np.arange(_LOCK_H, dtype=np.uint8)[:, None]
    f[..., 2] = 90
    return f


def test_old_stamped_frames_are_locked():
    h = hashlib.sha256()
    for fps, off in ((30.0, 0.0), (25.0, 0.4)):
        st = ir.Stamper(_LOCK_ITEMS, fps=fps, offset_sec=off, frame_size=(_LOCK_W, _LOCK_H))
        for i in range(int(3.0 * fps)):
            t = i / fps
            h.update(st.under(_lock_frame(), t).tobytes())
            h.update(repr(st.boxes(t)).encode())
            h.update(repr(st.boxes(t, drawn=True)).encode())
            h.update(repr(st.active(t)).encode())
    assert h.hexdigest()[:16] == "0eaba5d97e44dcb2"


def test_drawn_composited_is_under_and_under_ignores_the_front():
    st = ir.Stamper(_LOCK_ITEMS, fps=30, frame_size=(_LOCK_W, _LOCK_H))
    for i in range(0, 80, 3):
        t = i / 30
        f = _lock_frame()
        mine = f.copy()
        for rgba, x, y in st.drawn(t):
            ir.composite(mine, rgba, x, y)
        under = st.under(f, t)
        assert np.array_equal(mine, under)
        assert st.over(f, t) is f                                   # nothing in front
        assert st.drawn(t, layer="front") == [] and len(st.drawn(t, layer=None)) == len(st.drawn(t))
    # The same items in FRONT: over draws exactly what under drew, under nothing.
    front = [dict(it, layer="front") for it in _LOCK_ITEMS]
    fs = ir.Stamper(front, fps=30, frame_size=(_LOCK_W, _LOCK_H))
    for i in range(0, 80, 3):
        t = i / 30
        f = _lock_frame()
        assert fs.under(f, t) is f and fs.boxes(t) == [] and fs.boxes(t, drawn=True) == []
        assert np.array_equal(fs.over(f, t), st.under(f, t))
        assert fs.boxes(t, layer="front") == st.boxes(t)
        assert fs.boxes(t, drawn=True, layer=None) == st.boxes(t, drawn=True)


# --- E: the circle ----------------------------------------------------------

def _centroid(a: np.ndarray) -> tuple[float, float]:
    ys, xs = np.indices(a.shape)
    return float((a * ys).sum() / a.sum()), float((a * xs).sum() / a.sum())


def test_circle_geometry():
    L = 115
    s = ir.circle_sprite(ic.library_d("code", "bold"), L, "#7f2bf2", "#ffffff")
    pad = ir.shadow_pad(L)
    assert (s.w, s.h, s.pad) == (L, L, pad) == (115, 115, 34)
    assert s.rgba.shape == (L + 2 * pad, L + 2 * pad, 4)
    # The disc: area pi r^2 within 1 %, centred in the sprite.
    body = ir._disc(L)
    assert abs(float(body.sum()) / (math.pi * (L / 2) ** 2) - 1) < 0.01
    cy, cx = _centroid(body)
    assert abs(cy - (L - 1) / 2) < 0.05 and abs(cx - (L - 1) / 2) < 0.05
    # Opaque fill at the rim, white ink in the </>.
    rgba = s.rgba
    assert tuple(rgba[pad + L // 2, pad + 3]) == (0x7F, 0x2B, 0xF2, 255)
    ink = rgba[..., 3].astype(np.float64) / 255 * (rgba[..., 0].astype(np.float64) - 0x7F) / (255 - 0x7F)
    ink = np.clip(ink, 0, 1)
    assert ink.max() == pytest.approx(1.0)
    # The bold </> sits in the centred 0.53 L box: its ink is where the
    # glyph's own centre (measured on the 256 box) lands, within half a pixel.
    wy, wx = _centroid(ink)
    g = ir.path_alpha(ic.library_d("code", "bold"), 256).astype(np.float64)
    gy, gx = _centroid(g)
    k = ir.CIRCLE_GLYPH * L / 256
    off = (L - ir.CIRCLE_GLYPH * L) / 2
    assert abs(wx - (pad + off + (gx + 0.5) * k - 0.5)) < 0.5
    assert abs(wy - (pad + off + (gy + 0.5) * k - 0.5)) < 0.5
    # The shadow: soft, faded out inside the pad, and dropped by 0.05 L.
    assert rgba[0, 0, 3] == 0 and rgba[-1, -1, 3] == 0
    below = rgba[pad + L + 3, pad + L // 2]
    assert 0 < below[3] <= round(0.4 * 255) + 1 and tuple(below[:3]) == (0, 0, 0)
    above = rgba[pad - 3, pad + L // 2]
    assert above[3] < below[3]                                     # it falls below, not above
    blur = ir._gaussian(np.pad(body.astype(np.float64), pad), ir.SHADOW_SIGMA * L, ir.SHADOW_DY * L)
    by, bx = _centroid(blur)
    assert abs(by - (pad + (L - 1) / 2) - ir.SHADOW_DY * L) < 0.05
    assert abs(bx - (pad + (L - 1) / 2)) < 0.05


def test_circle_is_deterministic_and_takes_png_glyphs():
    from PIL import Image

    a = ir.make_sprite("circle", ic.library_d("code", "bold"), 96, "#303030", "#ffffff").rgba.tobytes()
    ir._path_alpha_cached.cache_clear()
    b = ir.make_sprite("circle", ic.library_d("code", "bold"), 96, "#303030", "#ffffff").rgba.tobytes()
    assert a == b
    rgba = np.zeros((256, 256, 4), np.uint8)
    rgba[64:192, 64:192, 3] = 255
    buf = io.BytesIO()
    Image.fromarray(rgba, "RGBA").save(buf, format="PNG")
    s = ir.make_sprite("circle", ("png", buf.getvalue()), 100, "#303030", "#ffffff")
    assert tuple(s.rgba[s.pad + 50, s.pad + 50, :3]) == (255, 255, 255)
    with pytest.raises(ValueError):
        ir.make_sprite("circle", ic.library_d("code", "bold"), 100, "#303030", "#ffffff", look_v=2)


# --- H: the chip --------------------------------------------------------------

def test_chip_geometry():
    font = lf.default_key()
    h = 85                                              # M on a 720-wide frame: 0.74 x 115
    g = ir.chip_geometry("Developer", h, font)
    fixed = (0.24 + 0.70 + 0.15 + 0.28) * h
    assert g.w == round(fixed + lf.units("Developer", font) * 0.37 * h) and g.h == h
    assert (g.text, g.rtl, g.shrunk, g.cut) == ("Developer", False, False, False)
    assert abs(g.w - 0.37 * 720) < 0.02 * 720           # the lab: "Developer" = 0.37 W
    ar = ir.chip_geometry("مطوّر", h, font)
    assert ar.rtl and not ar.cut
    # Too wide: the words shrink down to 0.30 h, then are cut with "...".
    long = "Peer learning with real projects every week"
    free = ir.chip_geometry(long, h, font)
    shrunk = ir.chip_geometry(long, h, font, max_w=free.w - 40)
    assert shrunk.shrunk and not shrunk.cut and shrunk.w <= free.w - 40
    assert 0.30 * h <= shrunk.em < 0.37 * h and shrunk.text == long
    cut = ir.chip_geometry(long, h, font, max_w=0.92 * 360)
    assert cut.cut and cut.text.endswith("…") and not cut.text[:-1].endswith(" ")
    assert cut.w <= 0.92 * 360 + 0.5 and long.startswith(cut.text[:-1])
    # Its own fixed point: the words it draws measure the same (the planner
    # stores .text and boxes it; the Stamper measures that .text again).
    def size(c):
        return c.w, c.h, c.text, c.em, c.rtl

    for got, mw in ((g, None), (shrunk, free.w - 40), (cut, 0.92 * 360), (ar, None)):
        assert size(ir.chip_geometry(got.text, h, font, max_w=mw)) == size(got)
    with pytest.raises(KeyError):
        ir.chip_geometry("Developer", h, "label-600@00000000")


def _white_ink(body: np.ndarray) -> np.ndarray:
    return ((body[..., 3] == 255) & np.all(body[..., :3] > 200, axis=-1)).astype(np.float64)


def test_chip_sprite_and_rtl_mirror():
    font = lf.default_key()
    h = 85
    d = ic.library_d("code", "bold")
    s = ir.chip_sprite(d, h, "Developer", font, "#7f2bf2", "#ffffff")
    g = ir.chip_geometry("Developer", h, font)
    pad = ir.shadow_pad(h / 0.74)
    assert (s.w, s.h, s.pad) == (g.w, h, pad) and s.rgba.shape == (h + 2 * pad, g.w + 2 * pad, 4)
    body = s.rgba[pad:pad + h, pad:pad + s.w]
    # A pill: the far corners are clear, the middle of each end is filled.
    assert body[0, 0, 3] < 60 and body[h - 1, s.w - 1, 3] < 60 and body[h // 2, 1, 3] == 255
    cols = np.flatnonzero(_white_ink(body).any(axis=0))
    # The glyph starts 0.24 h in; the words stop short of the far end.
    assert abs(cols.min() - 0.24 * h) < 0.12 * h
    assert s.w - 1 - cols.max() >= 0.2 * h
    # Arabic reads right-to-left: the glyph moves to the right end.
    a = ir.chip_sprite(d, h, "مطور", font, "#7f2bf2", "#ffffff")
    ink = _white_ink(a.rgba[pad:pad + h, pad:pad + a.w])
    gx = a.w - 0.24 * h - 0.70 * h
    right = ir._glyph_at(d, a.w, h, gx, 0.15 * h, 0.70 * h) > 0.9
    assert (ink * right).sum() > 0.9 * right.sum()
    # ... and the words sit to its left, from 0.28 h in.
    words = ink[:, :int(gx)]
    assert words.sum() > 0 and np.flatnonzero(words.any(axis=0)).min() >= 0.2 * h
    assert ink[:, int(math.ceil(gx + 0.70 * h)) + 1:].sum() == 0


def _chip_item(**kw):
    font = lf.default_key()
    h = 60
    g = ir.chip_geometry("Developer", h, font, max_w=0.92 * 720)
    it = {"glyph": {"source": "library", "id": "code"}, "from_sec": 1.0, "settle_sec": 1.0 + 8 / 30,
          "to_sec": 3.0, "box": {"x": 40 / 720, "y": 100 / 1280, "w": g.w / 720, "h": h / 1280},
          "fill": "#7f2bf2", "ink": "#ffffff", "fx": {"entrance": "glitch", "seed": "chip"},
          "style": "chip", "weight": "bold", "text": "Developer", "font": font, "look_v": 1}
    it.update(kw)
    return it


def test_stamper_draws_a_chip_body_where_the_box_says():
    it = _chip_item()
    st = ir.Stamper([it], fps=30, frame_size=(720, 1280))
    x0, y0, x1, y1 = st.boxes(2.0)[0]
    g = ir.chip_geometry("Developer", 60, lf.default_key(), max_w=0.92 * 720)
    assert (x0, y0, x1 - x0, y1 - y0) == (40, 100, g.w, 60)            # the body box, exactly
    sp = st.sprite_of(0)
    assert (sp.w, sp.h) == (g.w, 60) and sp.pad == ir.shadow_pad(60 / 0.74)
    assert np.array_equal(sp.rgba, ir.make_sprite("chip", ic.library_d("code", "bold"), 60, "#7f2bf2", "#ffffff",
                                                  text="Developer", font=lf.default_key(),
                                                  max_w=0.92 * 720).rgba)
    # Settled: the sprite laid with its body on the box (the shadow outside it).
    f = np.zeros((1280, 720, 3), np.uint8)
    out = st.under(f, 2.0)
    [(rgba, x, y)] = st.drawn(2.0)
    assert (x, y) == (40 - sp.pad, 100 - sp.pad) and rgba is sp.rgba
    assert st.boxes(2.0, drawn=True) == [(40 - sp.pad, 100 - sp.pad, 40 + g.w + sp.pad, 160 + sp.pad)]
    opaque = sp.rgba[..., 3] == 255
    region = out[100 - sp.pad:160 + sp.pad, 40 - sp.pad:40 + g.w + sp.pad]
    assert np.array_equal(region[opaque], sp.rgba[..., 2::-1][opaque])
    # Deterministic across two Stampers (a fresh sprite cache too).
    ir._SPRITES.clear()
    other = ir.Stamper([dict(it)], fps=30, frame_size=(720, 1280))
    for t in np.arange(0.9, 3.1, 1 / 30):
        assert np.array_equal(st.under(f, float(t)), other.under(f, float(t)))


def test_a_wide_chip_glitches_by_its_height_not_its_width():
    st = ir.Stamper([_chip_item()], fps=30, frame_size=(720, 1280))
    sp = st.sprite_for(0)
    torn = [st.frame_of(0, (30 + f) / 30) for f in range(7)]
    widest = max(t.rgba.shape[1] for t in torn if t is not None)
    # The canvas grows by the tear on each side: sized by the body's 60 px
    # height, never by its ~200 px width.
    grown = (widest - sp.shape[1]) // 2
    assert 0 < grown <= math.ceil(2.6 * ir.GLITCH_SHIFT * 60 + ir.GLITCH_SPLIT * 60 + 2)


def test_a_circle_slides_in_from_fully_outside():
    L = 100
    it = {"glyph": {"source": "library", "id": "code"}, "from_sec": 1.0, "settle_sec": 1.0 + 8 / 30,
          "to_sec": 3.0, "box": {"x": 0.02, "y": 0.4, "w": L / 720, "h": L / 1280}, "fill": "#7f2bf2",
          "ink": "#ffffff", "fx": {"entrance": "slide", "seed": "s"}, "style": "circle", "weight": "bold",
          "look_v": 1}
    st = ir.Stamper([it], fps=30, frame_size=(720, 1280))
    pad = ir.shadow_pad(L)
    x_body = st.items[0].box[0]
    assert st.items[0].edge == "left" and st.items[0].travel == x_body + L + pad
    [(_rgba, x, _y)] = st.drawn(2.0)
    assert x == x_body - pad                                       # settled on its box
    # The slide travels the body AND its shadow's pad off the frame (the
    # first frame is already 1/8 of the easeOutExpo in).
    travel = x_body + L + pad
    [(_rgba, x0, _y)] = st.drawn(1.0)
    assert x0 == x - int(round((1 - ir._out_expo(1 / 8)) * travel))


def test_bold_is_asked_of_the_lookup_only_for_bold_items():
    asked: list[tuple] = []

    def lookup(source, gid, *weight):
        asked.append((gid, *weight))
        return ic.library_d(gid, *weight)

    items = [_item(), _chip_item(glyph={"source": "library", "id": "clock"})]
    ir.Stamper(items, frame_size=(720, 1280), lookup=lookup)
    assert asked == [("code",), ("clock", "bold")]


# --- the words' shapes: pinned HarfBuzz + our outlines -------------------------

# sha256[:16] of text_path(text, the label font, 34 px).d and its advance, on
# the shipped fonts and uharfbuzz 0.56: a new HarfBuzz or font that shapes
# differently would change a stored chip, so it must change these on purpose.
TEXT_GOLDENS = {
    "Developer": ("fef605393ae46c6b", 159.018),
    "مطوّر 42": ("0f8c38bdbca00d69", 141.032),
}


@pytest.mark.parametrize("text", sorted(TEXT_GOLDENS))
def test_text_path_goldens(text):
    tp = lf.text_path(text, lf.default_key(), 34)
    got = (hashlib.sha256(tp.d.encode()).hexdigest()[:16], round(tp.advance, 3))
    assert got == TEXT_GOLDENS[text]


def test_bidi_runs_in_visual_order():
    key = lf.default_key()
    runs, rtl = lf.shape("Developer", key)
    assert not rtl and [(r.text, r.rtl) for r in runs] == [("Developer", False)]
    # "developer 42" in Arabic: the Arabic word on the right, the number on
    # its left and read left to right, in Jost.
    runs, rtl = lf.shape("مطور 42", key)
    assert rtl and [(r.text, r.rtl) for r in runs] == [("42", False), ("مطور ", True)]
    assert runs[0].face.startswith("jost-") and runs[1].face.startswith("kufi-")
    runs, rtl = lf.shape("AI مطور", key)
    assert rtl and [(r.text, r.rtl) for r in runs] == [(" مطور", True), ("AI", False)]
    # Brackets in a right-to-left line are mirrored by the shaper.
    runs, _ = lf.shape("مطور (AI)", key)
    assert [r.text for r in runs] == [")", "AI", "مطور ("]
    # The width is the sum of the runs' advances: units * em exactly.
    tp = lf.text_path("AI مطور", key, 30)
    assert tp.advance == lf.units("AI مطور", key) * 30 and tp.rtl


def test_a_font_that_does_not_match_its_key_is_refused(tmp_path, monkeypatch):
    face = lf._label(lf.default_key())[0]
    good = lf.face_file(face)
    (tmp_path / good.name).write_bytes(good.read_bytes()[:-4] + b"\0\0\0\0")
    monkeypatch.setattr(lf, "FONTS_DIR", tmp_path)
    lf._face.cache_clear()
    try:
        with pytest.raises(lf.FontError):
            lf._face(face)
    finally:
        monkeypatch.undo()
        lf._face.cache_clear()


def test_front_icons_are_drawn_over_the_speaker():
    """speaker_front: icons behind -> the speaker -> icons in front. With
    none in front the frame is what the pass has always written."""
    from app.services import speaker_front as sf

    W, H = 320, 180
    fe = np.full((H, W, 3), 40, np.uint8)
    fo = np.full((H, W, 3), 200, np.uint8)
    m = np.ones((H, W), np.float32)                 # the speaker covers everything
    behind = ir.Stamper([_item()], fps=30, frame_size=(W, H))
    front = ir.Stamper([_item(layer="front")], fps=30, frame_size=(W, H))
    assert np.array_equal(sf.compose_frame(fe, fo, m, stamps=behind, t=2.0), fo)   # hidden by her
    got = sf.compose_frame(fe, fo, m, stamps=front, t=2.0)
    assert np.array_equal(got, front.over(fo, 2.0)) and not np.array_equal(got, fo)
    assert sf._finish(fe, fo, m, front, 2.0)[1] is True
    frame, drawn = sf._finish(fe, fo, m, front, 0.5)                # not on screen yet
    assert drawn is False and np.array_equal(frame, fo)
    # Both layers at once: the behind one under her, the front one over.
    both = ir.Stamper([_item(), _item(layer="front", box={"x": 0.6, "y": 0.2, "w": 60 / 320, "h": 60 / 180})],
                      fps=30, frame_size=(W, H))
    got = sf.compose_frame(fe, fo, m, stamps=both, t=2.0)
    assert np.array_equal(got, both.over(fo, 2.0))


def test_a_front_icon_needs_no_matte():
    from app.services.rebrand_harness.elements import icon as icon_el

    rows = [{"max_overlap": 0.5, "layer": "front"}, {"max_overlap": 0.5}, {"max_overlap": 0.01}]
    assert icon_el.unmatted(rows) == [rows[0], rows[2]]
