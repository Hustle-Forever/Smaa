"""Omni's pattern reference: which pattern, which colours, and what gets sent.

What these guard is the owner's instruction taken literally — "a randomly
selected pattern, coloured to match the intro's background" — and the ways it
goes quietly wrong: the same pattern every time, a third colour sneaking into
the image Omni is told to copy, the intro's "background" being the black
letterbox bar around the card, or a dark intro killing the pattern outright
instead of degrading to 42's own colours.

No ffmpeg, no network: intros are synthetic stills (or palettes measured off
real intro cards), the video path has its frame picker mocked, and the kit is
a set of synthetic masks in a temp dir — plus checks against the real masks,
which ship with the code (BUNDLED_DIR) because production has no kit.
"""
import json
import random
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np
import pytest
from PIL import Image

from app.services import pattern_kit
from app.services import rebrand_moments as moments
from app.services import rebrand_pattern_ref as ref

SLUGS = [s.slug for s in pattern_kit.SPECS]
REAL_KIT = Path(pattern_kit.KIT_DIR)
HAVE_KIT = all((REAL_KIT / f"{s}.mask.png").exists() for s in SLUGS)
HAVE_MASKS = all(ref._mask_path(s).exists() for s in SLUGS)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _write_mask(path: Path, w: int = 200, h: int = 80) -> None:
    """White RGB, shape in alpha — the kit's format. Hard stripes plus a soft
    ramp, so both the extremes and the in-between blend get exercised."""
    a = np.zeros((h, w), dtype="uint8")
    a[:, ::10] = 255                                    # hairlines
    a[h // 2:, :] = np.linspace(0, 255, w).astype("uint8")[None, :]   # ramp
    rgba = np.dstack([np.full((h, w, 3), 255, "uint8"), a])
    Image.fromarray(rgba).save(path)


@pytest.fixture
def kit(tmp_path, monkeypatch):
    """A kit dir with a synthetic mask for every slug."""
    d = tmp_path / "kit"
    d.mkdir()
    for slug in SLUGS:
        _write_mask(d / f"{slug}.mask.png")
    monkeypatch.setattr(pattern_kit, "KIT_DIR", d)
    # No bundled fallback either, so a mask a test deletes is really gone.
    monkeypatch.setattr(ref, "BUNDLED_DIR", tmp_path / "no_bundle")
    return d


def _still(path: Path, size, bg, rects=()) -> Path:
    im = Image.new("RGB", size, bg)
    px = im.load()
    for (x0, y0, x1, y1, colour) in rects:
        for y in range(y0, y1):
            for x in range(x0, x1):
                px[x, y] = colour
    im.save(path)
    return path


def _violet_card(path: Path, *, letterboxed: bool = False) -> Path:
    """A 42-style intro card: violet ground, white headline blocks."""
    violet, white = (0x7F, 0x2B, 0xF2), (0xFF, 0xFF, 0xFF)
    if not letterboxed:
        return _still(path, (360, 640), violet, [
            (40, 200, 320, 250, white), (40, 270, 280, 320, white),
            (40, 340, 300, 380, white),
        ])
    # TH v2's intro: a 16:9 card band in the middle of a black 9:16 frame —
    # 68% of the pixels are letterbox.
    im = Image.new("RGB", (360, 640), (0, 0, 0))
    card = Image.new("RGB", (360, 203), violet)
    band = np.asarray(card).copy()
    band[60:90, 30:300] = white
    band[110:140, 30:250] = white
    im.paste(Image.fromarray(band), (0, 218))
    im.save(path)
    return path


def _pal(rows) -> list[dict]:
    """(hex, weight, ring) rows -> an intro_palette()-shaped palette."""
    return [{"hex": h, "weight": w, "ring": r} for h, w, r in rows]


# ---------------------------------------------------------------------------
# Picking
# ---------------------------------------------------------------------------

def test_pick_is_uniform_over_all_five(kit):
    rng = random.Random(42)
    n = 2000                    # 400 expected each; the band is ~4.5 sigma wide
    counts = Counter(ref.pick_pattern(rng) for _ in range(n))
    assert set(counts) == set(SLUGS), counts
    for slug in SLUGS:
        assert 0.16 <= counts[slug] / n <= 0.24, counts


def test_a_seeded_rng_picks_the_same_pattern(kit):
    a = [ref.pick_pattern(random.Random(7)) for _ in range(3)]
    b = [ref.pick_pattern(random.Random(7)) for _ in range(3)]
    assert a == b and len(set(a)) == 1
    run1 = random.Random(11)
    run2 = random.Random(11)
    assert [ref.pick_pattern(run1) for _ in range(20)] == \
        [ref.pick_pattern(run2) for _ in range(20)]


def test_only_patterns_with_a_prepared_mask_are_picked(kit):
    for slug in ("structure", "grid", "beads_field"):
        (kit / f"{slug}.mask.png").unlink()
    rng = random.Random(3)
    seen = {ref.pick_pattern(rng) for _ in range(300)}
    assert seen == {"beads_row", "blueprint"}


def test_a_missing_kit_is_prepared_from_the_patterns_folder(tmp_path, monkeypatch):
    empty = tmp_path / "nokit"
    empty.mkdir()
    monkeypatch.setattr(pattern_kit, "KIT_DIR", empty)
    monkeypatch.setattr(ref, "BUNDLED_DIR", tmp_path / "no_bundle")
    calls = []

    def fake_build(src_dir, force=False):
        calls.append(Path(src_dir))
        for slug in SLUGS:
            _write_mask(empty / f"{slug}.mask.png")
        return []

    monkeypatch.setattr(pattern_kit, "build_kit", fake_build)
    assert ref.pick_pattern(random.Random(1)) in SLUGS
    assert calls == [ref.PATTERNS_DIR]
    assert ref.PATTERNS_DIR.name == "patterns"
    assert (ref.PATTERNS_DIR.parent / "backend").is_dir(), "PATTERNS_DIR is not at the repo root"


def test_no_masks_and_no_kit_raises(tmp_path, monkeypatch):
    empty = tmp_path / "nokit"
    empty.mkdir()
    monkeypatch.setattr(pattern_kit, "KIT_DIR", empty)
    monkeypatch.setattr(ref, "BUNDLED_DIR", tmp_path / "no_bundle")
    monkeypatch.setattr(pattern_kit, "build_kit", lambda src_dir, force=False: [])
    with pytest.raises(FileNotFoundError):
        ref.pick_pattern()


def test_the_masks_ship_with_the_code():
    """Production reads these. The kit under backend/data/ is gitignored, kept
    out of the Docker image and covered by the data volume on the VPS, and the
    source art it is built from is gitignored too — so the copy that reaches
    production has to live under backend/app/."""
    backend = Path(__file__).resolve().parents[1]
    bundled = Path(ref.BUNDLED_DIR).resolve()
    assert (backend / "app") in bundled.parents
    assert (backend / "data") not in bundled.parents
    for slug in SLUGS:
        with Image.open(bundled / f"{slug}.mask.png") as im:
            assert im.convert("RGBA").getchannel("A").getextrema()[1] > 0, slug


def test_an_empty_kit_falls_back_to_the_bundled_masks(tmp_path, monkeypatch):
    """The VPS: KIT_DIR empty and no source art. Every pattern must still be
    pickable and colourable, and nothing may try to rebuild the kit."""
    empty = tmp_path / "nokit"
    empty.mkdir()
    monkeypatch.setattr(pattern_kit, "KIT_DIR", empty)
    monkeypatch.setattr(ref, "PATTERNS_DIR", tmp_path / "no_sources")

    def no_build(*_a, **_kw):
        raise AssertionError("rebuilt the kit although bundled masks exist")

    monkeypatch.setattr(pattern_kit, "build_kit", no_build)
    rng = random.Random(4)
    assert {ref.pick_pattern(rng) for _ in range(200)} == set(SLUGS)
    pr = ref.build(None, tmp_path / "exp", aspect="9:16")
    assert Image.open(pr.path).size == (1440, 1280)          # OMNI_SCALE x the frame width
    assert ref._mask_path(pr.slug).parent == Path(ref.BUNDLED_DIR)


@pytest.mark.skipif(not HAVE_KIT, reason="prepared pattern kit not present")
@pytest.mark.parametrize("slug", SLUGS)
def test_the_bundled_masks_match_the_prepared_kit(slug):
    """Regenerating the kit without refreshing the shipped copy would make
    production colour different art from every dev machine."""
    with Image.open(REAL_KIT / f"{slug}.mask.png") as a, \
            Image.open(Path(ref.BUNDLED_DIR) / f"{slug}.mask.png") as b:
        assert a.size == b.size, f"{slug}: refresh backend/app/assets/patterns from the kit"
        assert np.array_equal(np.asarray(a.convert("RGBA").getchannel("A")),
                              np.asarray(b.convert("RGBA").getchannel("A"))), \
            f"{slug}: refresh backend/app/assets/patterns from the kit"


# ---------------------------------------------------------------------------
# Colouring
# ---------------------------------------------------------------------------

def _alpha(path) -> np.ndarray:
    return np.asarray(Image.open(path).getchannel("A")).astype("float64") / 255.0


def test_colorize_is_one_colour_on_a_transparent_background(kit, tmp_path):
    out = ref.colorize("blueprint", "#7f2bf2", tmp_path / "p.png", width=720, height=1280)
    im = Image.open(out)
    assert im.mode == "RGBA", "the background must be transparent, not a filled ground"
    assert im.size == (720, 1280)
    arr = np.asarray(im)
    assert (arr[..., :3].reshape(-1, 3) == ref._rgb("#7f2bf2")).all(), "only the pattern colour"
    a = arr[..., 3]
    assert a.min() == 0, "the empty parts of the pattern are fully transparent"
    assert a.max() == 255, "the lines are solid"
    assert len(np.unique(a)) > 2, "the soft edge must fade, not posterise"


def test_colorize_scales_to_width_and_tiles_down_with_no_gap(kit, tmp_path):
    # Mask 200x80 at width 400 -> a 160px tile; 400px tall = 2.5 tiles.
    out = ref.colorize("grid", "#ffffff", tmp_path / "t.png", width=400, height=400)
    arr = np.asarray(Image.open(out)).astype("int16")
    assert arr.shape == (400, 400, 4)
    assert np.array_equal(arr[0:160], arr[160:320]), "tiles must repeat exactly"
    assert np.array_equal(arr[0:80], arr[320:400]), "overflow is cropped, not squeezed"


def test_colorize_16_9(kit, tmp_path):
    out = ref.colorize("beads_row", "#8d01fa", tmp_path / "w.png", width=1280, height=720)
    assert Image.open(out).size == (1280, 720)


def test_colorize_rejects_an_unknown_slug(kit, tmp_path):
    with pytest.raises(ValueError):
        ref.colorize("logo", "#000000", tmp_path / "x.png", width=10, height=10)


@pytest.mark.skipif(not HAVE_MASKS, reason="no prepared pattern masks")
@pytest.mark.parametrize("slug", SLUGS)
def test_the_real_kit_is_the_colour_on_transparency(slug, tmp_path):
    """The prepared masks, not synthetic ones — including structure, whose
    alpha carries shading, and grid, whose hairlines need their alpha lifted."""
    colour = "#024f92"
    out = ref.colorize(slug, colour, tmp_path / f"{slug}.png", width=720, height=1280)
    arr = np.asarray(Image.open(out))
    assert arr.shape == (1280, 720, 4)
    assert (arr[..., :3].reshape(-1, 3) == ref._rgb(colour)).all()
    a = arr[..., 3].astype("float64") / 255.0
    assert (a > 0.5).mean() > 0.002, f"{slug} came out nearly blank"
    assert (a == 0).mean() > 0.2, f"{slug} has no transparent background"


@pytest.mark.skipif(not HAVE_MASKS, reason="no prepared pattern masks")
@pytest.mark.parametrize("width,height", [(720, 1280), (1280, 720)])
def test_the_grid_reads_as_graph_paper_not_dots(width, height, tmp_path):
    """The grid's peak is its crosshair dots. Levelled to them, its major
    lines sat at ~0.2 opacity (1.1x gain at 16:9) — dots on a clear field.
    The major lines must be solid while the page stays mostly transparent:
    graph paper, not a wash over everything."""
    out = ref.colorize("grid", "#2a2656", tmp_path / "grid.png", width=width, height=height)
    a = _alpha(out)
    assert (a > 0.5).mean() >= 0.08, "the major lines did not come through"
    assert a.mean() <= 0.3 and float(np.median(a)) < 0.05, "the page turned solid"


# ---------------------------------------------------------------------------
# Digging the intro's colours
# ---------------------------------------------------------------------------

def test_a_violet_card_gives_violet_ground_and_its_white_type_as_ink(tmp_path):
    card = _violet_card(tmp_path / "intro.png")
    ground, ink, fell_back, frame = ref.intro_colours(card, tmp_path / "w", is_still=True)
    assert not fell_back
    assert ref._distance(ground, "#7f2bf2") < 12, ground
    assert ref._distance(ink, "#ffffff") < 20, ink
    assert frame is not None and frame.exists()


def test_the_letterbox_is_not_the_intro_background(tmp_path):
    """TH v2 intros are a card band in a black frame; black is the heaviest
    colour of the frame and must still not become the ground."""
    card = _violet_card(tmp_path / "intro_lb.png", letterboxed=True)
    ground, ink, fell_back, frame = ref.intro_colours(card, tmp_path / "w", is_still=True)
    assert not fell_back
    assert ref._distance(ground, "#7f2bf2") < 12, ground
    assert ref._distance(ink, "#ffffff") < 20, ink
    assert Image.open(frame).size[1] < 640, "the measured frame should be the trimmed card"


def test_a_flat_dark_intro_gets_a_lighter_ink_in_its_own_hue(tmp_path):
    flat = _still(tmp_path / "blue.png", (180, 320), (0x1E, 0x3A, 0x8A))
    ground, ink, fell_back, _ = ref.intro_colours(flat, tmp_path / "w", is_still=True)
    assert not fell_back
    assert ref._distance(ground, "#1e3a8a") < 6, ground
    assert ref.contrast(ground, ink) >= ref.INK_MIN_CONTRAST
    assert ref._relative_luminance(ink) > ref._relative_luminance(ground)
    assert ref._hue_gap(ink, ground) < 8, (ground, ink)


def test_a_flat_light_intro_gets_a_darker_ink_in_its_own_hue(tmp_path):
    flat = _still(tmp_path / "cream.png", (180, 320), (0xF0, 0xE0, 0xB8))
    ground, ink, fell_back, _ = ref.intro_colours(flat, tmp_path / "w", is_still=True)
    assert not fell_back
    assert ref.contrast(ground, ink) >= ref.INK_MIN_CONTRAST
    assert ref._relative_luminance(ink) < ref._relative_luminance(ground)
    assert ref._hue_gap(ink, ground) < 8, (ground, ink)


def test_no_intro_means_brand_colours():
    assert ref.intro_colours(None, Path("unused")) == (
        ref.BRAND_GROUND, ref.BRAND_INK, True, None)


def test_a_black_still_means_brand_colours(tmp_path):
    black = _still(tmp_path / "black.png", (180, 320), (2, 2, 2))
    ground, ink, fell_back, frame = ref.intro_colours(black, tmp_path / "w", is_still=True)
    assert (ground, ink, fell_back, frame) == (ref.BRAND_GROUND, ref.BRAND_INK, True, None)


def test_an_unreadable_intro_degrades_to_brand_colours(tmp_path):
    ground, ink, fell_back, _ = ref.intro_colours(
        tmp_path / "missing.png", tmp_path / "w", is_still=True)
    assert (ground, ink, fell_back) == (ref.BRAND_GROUND, ref.BRAND_INK, True)


def test_a_video_intro_is_read_at_its_last_content_frame(tmp_path, monkeypatch):
    card = _violet_card(tmp_path / "picked.png")
    calls = {}

    def fake_pick(video, out_dir, *, edge, label="frame"):
        calls.update(video=video, edge=edge)
        return moments.PickedFrame(card, 3.1, moments.measure(card), fell_back=False)

    monkeypatch.setattr(moments, "pick_content_frame", fake_pick)
    ground, ink, fell_back, frame = ref.intro_colours(tmp_path / "intro.mp4", tmp_path / "w")
    assert calls == {"video": tmp_path / "intro.mp4", "edge": "last"}
    assert not fell_back and frame is not None
    assert ref._distance(ground, "#7f2bf2") < 12
    assert ref._distance(ink, "#ffffff") < 20


def test_a_video_intro_dark_throughout_falls_back(tmp_path, monkeypatch):
    black = _still(tmp_path / "black.png", (90, 160), (0, 0, 0))

    def fake_pick(video, out_dir, *, edge, label="frame"):
        return moments.PickedFrame(black, 0.0, moments.measure(black), fell_back=True)

    monkeypatch.setattr(moments, "pick_content_frame", fake_pick)
    assert ref.intro_colours(tmp_path / "intro.mp4", tmp_path / "w") == (
        ref.BRAND_GROUND, ref.BRAND_INK, True, None)


def test_ink_prefers_the_intros_own_second_colour_over_a_derived_one():
    palette = [
        {"hex": "#7f2bf2", "weight": 0.80, "sat": 0.88, "lum": 0.56},
        {"hex": "#9a5cf4", "weight": 0.05, "sat": 0.87, "lum": 0.66},   # AA fringe: too close
        {"hex": "#f4f4f8", "weight": 0.12, "sat": 0.2, "lum": 0.96},    # the type
        {"hex": "#fefefe", "weight": 0.01, "sat": 0.0, "lum": 1.0},     # a sliver
    ]
    assert ref.ground_and_ink(palette) == ("#7f2bf2", "#f4f4f8")


def test_an_off_hue_colour_loses_to_neutral_type_of_similar_weight():
    """A skin tone in the intro should not become the brand pattern's colour
    when the card's own type is there to use."""
    palette = [
        {"hex": "#1a1446", "weight": 0.70, "sat": 0.55, "lum": 0.18},
        {"hex": "#e7af8c", "weight": 0.12, "sat": 0.65, "lum": 0.73},   # skin
        {"hex": "#f4f4f8", "weight": 0.10, "sat": 0.2, "lum": 0.96},    # type
    ]
    assert ref.ground_and_ink(palette)[1] == "#f4f4f8"


# Palettes measured off real intro cards with intro_palette() (hex, weight,
# share of the border ring). A navy gradient card, as the video intro of
# rebrand experiments 1-2 (letterbox trimmed) and as a still: its navy is
# spread over six bins of 8-18%, and the heaviest one is a darker indigo.
NAVY_VIDEO = _pal([
    ("#2a2656", 0.1802, 0.1133), ("#144991", 0.1505, 0.0604), ("#2a1b48", 0.1248, 0.1060),
    ("#1b3672", 0.1122, 0.0169), ("#253169", 0.0883, 0.0640), ("#0c57a7", 0.0779, 0.2069),
    ("#2b143b", 0.0423, 0.1632), ("#2b5492", 0.0400, 0.0496), ("#f9fbfc", 0.0282, 0.0006),
    ("#316aad", 0.0174, 0.0195), ("#514b71", 0.0135, 0.0100), ("#173e82", 0.0126, 0.0000),
    ("#1d275a", 0.0100, 0.0000), ("#2d4578", 0.0085, 0.0166), ("#706d90", 0.0084, 0.0081),
    ("#4876b1", 0.0083, 0.0074),
])
NAVY_STILL = _pal([
    ("#14498f", 0.2105, 0.0962), ("#262555", 0.1746, 0.0670), ("#291439", 0.1229, 0.3435),
    ("#1c3a76", 0.1228, 0.0593), ("#281c46", 0.1059, 0.0503), ("#233167", 0.0964, 0.0475),
    ("#0954a3", 0.0707, 0.3166), ("#fbfbfc", 0.0250, 0.0000), ("#2f4579", 0.0160, 0.0013),
    ("#2b5490", 0.0095, 0.0054), ("#2c66a6", 0.0044, 0.0055), ("#c9ccd8", 0.0036, 0.0000),
    ("#afb5c8", 0.0035, 0.0000), ("#4a5985", 0.0032, 0.0000), ("#4a4e77", 0.0032, 0.0007),
    ("#6d7395", 0.0030, 0.0000),
])
# Blue at the top fading to white at the bottom, white type on the blue.
BLUE_TO_WHITE = _pal([
    ("#024f90", 0.1923, 0.2815), ("#036fae", 0.1881, 0.1133), ("#f8fbfc", 0.1435, 0.2503),
    ("#70aed3", 0.0716, 0.0335), ("#b0d2e6", 0.0681, 0.0574), ("#4a99c8", 0.0528, 0.0213),
    ("#d3e6f1", 0.0481, 0.0484), ("#87bbda", 0.0403, 0.0262), ("#3790c2", 0.0354, 0.0173),
    ("#2889be", 0.0298, 0.0109), ("#1783bb", 0.0263, 0.0126), ("#5aa2cd", 0.0243, 0.0128),
    ("#94c3de", 0.0223, 0.0151), ("#c4ddec", 0.0166, 0.0130), ("#9bc7e0", 0.0131, 0.0124),
    ("#013f81", 0.0108, 0.0368),
])
# An orange / purple / cyan mesh card with a huge, blurred dark-brown headline:
# the headline is the heaviest colour, and it never touches the border.
BLURRED_HEADLINE = _pal([
    ("#312613", 0.1382, 0.0000), ("#11120c", 0.1085, 0.0000), ("#8142c9", 0.0994, 0.1739),
    ("#f05c38", 0.0885, 0.1997), ("#53d7e6", 0.0724, 0.1732), ("#4b3519", 0.0559, 0.0000),
    ("#904e2b", 0.0398, 0.0539), ("#704b2b", 0.0344, 0.0000), ("#ad5932", 0.0276, 0.0557),
    ("#574c2a", 0.0221, 0.0000), ("#6d714f", 0.0209, 0.0017), ("#e15ea1", 0.0175, 0.0493),
    ("#d0643b", 0.0173, 0.0363), ("#758d6f", 0.0169, 0.0140), ("#dd5a9c", 0.0156, 0.0346),
    ("#905590", 0.0121, 0.0053),
])


def test_a_gradient_background_split_across_bins_is_one_ground():
    """The heaviest single bin of the navy card is its indigo corner (#2a2656);
    the card itself reads royal navy (median pixel #253771). Its white type was
    not even in the top 6 bins, so the ink used to be a derived lavender."""
    ground, ink = ref.ground_and_ink(NAVY_VIDEO)
    assert ref._distance(ground, "#253771") < 12, ground
    assert ref._distance(ink, "#f9fbfc") < 6, ink


def test_the_video_and_the_still_of_one_card_agree():
    """Same card, two ways in: they used to give #2a2656 and #14498f."""
    g_video, _ = ref.ground_and_ink(NAVY_VIDEO)
    g_still, _ = ref.ground_and_ink(NAVY_STILL)
    assert ref._distance(g_video, g_still) < 16, (g_video, g_still)


def test_a_blue_to_white_gradient_is_not_chained_into_its_type():
    """Merging must stay local: neighbour-to-neighbour linking walks this card
    from its blue through the pale blues into its white type, averaging the
    lot into a mid-blue it never shows and leaving no ink to find."""
    ground, ink = ref.ground_and_ink(BLUE_TO_WHITE)
    # The deep blue, not the chained mid-blue (#5c9cc5, luminance ~0.31).
    assert ref._hue_gap(ground, "#036fae") < 10, ground
    assert ref._relative_luminance(ground) < 0.2, ground
    assert ref._relative_luminance(ink) > 0.7 and ref.contrast(ground, ink) >= 4.0, ink


def test_a_huge_inset_headline_is_not_the_ground():
    ground, ink = ref.ground_and_ink(BLURRED_HEADLINE)
    assert ref._distance(ground, "#312613") > 80, ground
    assert ref._distance(ground, "#f05c38") < 40, ground
    assert ref.contrast(ground, ink) >= ref.INK_MIN_CONTRAST


def test_the_border_ring_is_read_off_the_frame(tmp_path):
    """End to end on pixels: a dark headline block holding half the frame,
    inset from every edge, on a card that is orange on the left and purple on
    the right. The headline is the heaviest colour and must not be the ground."""
    orange, purple, dark = (240, 92, 56), (129, 66, 201), (49, 38, 19)
    card = _still(tmp_path / "mesh.png", (360, 640), orange, [
        (180, 0, 360, 640, purple),
        (40, 120, 320, 520, dark),            # 49% of the frame, touches no edge
    ])
    pal = ref.intro_palette(card)
    top = max(pal, key=lambda c: c["weight"])
    assert ref._distance(top["hex"], "#312613") < 6 and top["ring"] == 0.0
    ground, ink, fell_back, _ = ref.intro_colours(card, tmp_path / "w", is_still=True)
    assert not fell_back
    assert min(ref._distance(ground, "#f05c38"), ref._distance(ground, "#8142c9")) < 12, ground
    assert ref.contrast(ground, ink) >= ref.INK_MIN_CONTRAST


# ---------------------------------------------------------------------------
# The whole reference
# ---------------------------------------------------------------------------

def test_build_returns_a_ref_whose_png_exists_and_is_json_safe(kit, tmp_path):
    card = _violet_card(tmp_path / "intro.png")
    pr = ref.build(card, tmp_path / "exp", aspect="9:16", is_still=True)
    assert isinstance(pr, ref.PatternRef)
    assert pr.slug == "structure"
    assert pr.source == next(s.source for s in pattern_kit.SPECS if s.slug == pr.slug)
    # The art at twice the frame's width, the frame's height: never cut to the frame.
    assert pr.path.exists() and Image.open(pr.path).size == (1440, 1280)
    assert not pr.fell_back
    arr = np.asarray(Image.open(pr.path))
    # A violet card's ground IS a colour, so the pattern is drawn in it.
    assert pr.colour == pr.ground
    assert arr.shape[2] == 4 and (arr[..., :3].reshape(-1, 3) == ref._rgb(pr.colour)).all()

    pub = pr.public()
    assert set(pub) == {"slug", "source", "path", "colour", "ground", "ink", "fell_back",
                        "intro_frame", "where", "name", "layout", "animation"}
    assert pub["layout"] == {"placement": "left", "scale": 2.0, "reach": 0.5, "anchor": "left",
                             "rotate": 0}
    back = json.loads(json.dumps(pub))
    assert back["path"] == str(pr.path) and back["ground"] == pr.ground


def test_build_with_no_intro_is_brand_coloured_16_9(kit, tmp_path):
    pr = ref.build(None, tmp_path / "exp", aspect="16:9")
    assert pr.fell_back and (pr.ground, pr.ink) == (ref.BRAND_GROUND, ref.BRAND_INK)
    assert Image.open(pr.path).size == (2560, 720)
    assert json.loads(json.dumps(pr.public()))["intro_frame"] is None


def test_omni_always_gets_the_left_strip_pattern(kit, tmp_path):
    """The owner's placement: 2.png (structure) is the left strip, which is
    what Omni's scene edit is. No random pick."""
    for i in range(5):
        pr = ref.build(None, tmp_path / str(i), aspect="9:16")
        assert (pr.slug, pr.source) == ("structure", "2.png")
        assert pr.where == "the left half of the frame, behind the speaker"
        assert pr.public()["where"] == pr.where


def _target(tmp_path, frame_rgb=(10, 200, 30)):
    frame = tmp_path / "frame.png"
    Image.new("RGB", (720, 1280), frame_rgb).save(frame)
    pr = ref.build(None, tmp_path / "exp", aspect="9:16")
    out = ref.strip_preview(pr, frame, tmp_path / "target.png")
    return pr, Image.open(out)


def test_strip_preview_lays_the_pattern_in_from_the_left_only(kit, tmp_path):
    """What Omni is shown: the clip's own frame, the pattern in from the left
    edge, the footage between the lines, and nothing else touched."""
    pr, im = _target(tmp_path)
    assert im.mode == "RGB" and im.size == (720, 1280)
    arr = np.asarray(im).astype("int16")
    reach = round(720 * ref.OMNI_REACH_FRAC)
    assert (arr[:, reach:] == (10, 200, 30)).all(), "past its reach the frame is untouched"
    left = arr[:, :reach].reshape(-1, 3)
    green = (left == (10, 200, 30)).all(axis=1).mean()
    patterned = (np.abs(left - np.array(ref._rgb(pr.colour))).sum(axis=1) <= 6).mean()
    assert green > 0.05, "the footage must show between the lines"
    assert patterned > 0.05, "the pattern must be there, in its colour"


@pytest.mark.skipif(not HAVE_MASKS, reason="no prepared pattern masks")
def test_the_real_structure_keeps_its_own_outline(tmp_path):
    """The owner: "looks cutted to the right side ... keep its regular
    shape". A rectangle cut out of the art ended every row at the same
    column — a ruler-straight edge through the bars. The whole art now
    slides in, so its right edge is its own: the last stroke lands exactly
    at the reach, and the rows end at many different points before it."""
    pr, im = _target(tmp_path)
    arr = np.asarray(im).astype("int16")
    painted = np.abs(arr - np.array(ref._rgb(pr.colour))).sum(axis=2) <= 6
    cols = np.nonzero(painted.any(axis=0))[0]
    reach = round(720 * ref.OMNI_REACH_FRAC)
    assert cols[-1] == reach - 1, "the art's own last stroke lands at the reach"
    row_ends = np.array([np.nonzero(r)[0][-1] for r in painted if r.any()])
    at_the_edge = (row_ends >= reach - 3).mean()
    assert at_the_edge < 0.10, f"{at_the_edge:.0%} of rows end at one column: that is a cut"
    assert row_ends.std() > 25, "a natural outline ends its rows at many different points"


def test_colorize_scale_makes_the_art_bigger_not_cropped(kit, tmp_path):
    one = ref.colorize("grid", "#ffffff", tmp_path / "1.png", width=400, height=400)
    two = ref.colorize("grid", "#ffffff", tmp_path / "2.png", width=400, height=400, scale=2.0)
    assert Image.open(one).size == (400, 400) and Image.open(two).size == (800, 400)
    # The 200x80 mask at 800 wide is a 320px tile: twice the 160px one.
    a2 = np.asarray(Image.open(two).getchannel("A")).astype("int16")
    assert np.array_equal(a2[0:80], a2[320:400])


def _person_left_of_centre():
    """A person-shaped block: x 200..520, y 300..1280 of a 720x1280 frame."""
    m = np.zeros((1280, 720), dtype="float32")
    m[300:, 200:520] = 1.0
    return m


@pytest.mark.skipif(not HAVE_MASKS, reason="no prepared pattern masks")
def test_the_pattern_is_twice_as_big_and_reaches_the_middle(tmp_path):
    """Owner: "make it big 2wice the size ... it start from the left but it
    can go until the middle of the screen"."""
    pr, im = _target(tmp_path)
    arr = np.asarray(im).astype("int16")
    painted = np.abs(arr - np.array(ref._rgb(pr.colour))).sum(axis=2) <= 6
    cols = np.nonzero(painted.any(axis=0))[0]
    assert ref.OMNI_SCALE == 2.0 and ref.OMNI_REACH_FRAC == 0.5
    assert cols[0] == 0 and cols[-1] == 360 - 1, "from the left edge to the middle"


@pytest.mark.skipif(not HAVE_MASKS, reason="no prepared pattern masks")
def test_the_speaker_covers_the_pattern(tmp_path):
    """Behind the character: where the person is, not one pattern pixel; the
    background beside them keeps it."""
    frame = tmp_path / "frame.png"
    Image.new("RGB", (720, 1280), (10, 200, 30)).save(frame)
    pr = ref.build(None, tmp_path / "exp", aspect="9:16")
    person = _person_left_of_centre()
    out = ref.strip_preview(pr, frame, tmp_path / "t.png", person=person)
    arr = np.asarray(Image.open(out)).astype("int16")
    untouched = (arr == (10, 200, 30)).all(axis=2)
    assert untouched[300:, 200:520].all(), "no pattern over the speaker"
    beside = untouched[:, :200]
    assert (~beside).mean() > 0.05, "the pattern still shows on the background beside them"
    # Without a mask the same pattern covers that region — the mask did it.
    plain = np.asarray(Image.open(ref.strip_preview(pr, frame, tmp_path / "p.png"))).astype("int16")
    assert not (plain[300:, 200:360] == (10, 200, 30)).all()


def _pal_rows(rows):
    return [{"hex": h, "weight": w, "ring": r} for h, w, r in rows]


def test_a_ground_that_is_a_colour_colours_the_pattern():
    """The blue card the owner approved: blue ground, blue lines."""
    assert ref.pattern_colour(_pal_rows([
        ("#0461a2", 0.70, 0.95), ("#f4f4f8", 0.08, 0.0)])) == "#0461a2"


def test_a_neutral_ground_hands_the_colour_to_the_intros_vivid_art():
    """Experiment 15: a charcoal card whose colour is all in its cyan and
    violet artwork. Charcoal lines came back as a pattern the owner read as
    black; the pattern takes the most prominent vivid colour instead."""
    colour = ref.pattern_colour(_pal_rows([
        ("#3d3c3d", 0.62, 0.90), ("#2ed8e8", 0.10, 0.02),
        ("#7f2bf2", 0.06, 0.01), ("#f4f4f8", 0.05, 0.0)]))
    assert colour == "#2ed8e8"


def test_a_dark_navy_card_is_lifted_not_drawn_in_navy():
    """"What is 42?" — a navy gradient with white type and nothing vivid.
    Navy lines over footage read as black; the navy's own hue, lifted."""
    colour = ref.pattern_colour(_pal_rows([
        ("#1d3873", 0.75, 0.95), ("#f4f4f8", 0.06, 0.0)]))
    assert ref._value(colour) > ref._value("#1d3873")
    assert ref._hue_gap(colour, "#1d3873") < 15
    assert ref.contrast(colour, "#1d3873") >= ref.INK_MIN_CONTRAST


def test_a_black_and_white_card_gets_its_lighter_tone():
    colour = ref.pattern_colour(_pal_rows([("#111111", 0.80, 0.95), ("#f4f4f4", 0.12, 0.0)]))
    assert colour == "#f4f4f4"
    assert ref.pattern_colour([]) == ref.BRAND_GROUND


def test_build_draws_a_charcoal_intro_in_its_vivid_colour(kit, tmp_path):
    card = _still(tmp_path / "intro.png", (360, 640), (0x3D, 0x3D, 0x3D), [
        (40, 200, 320, 300, (0x2E, 0xD8, 0xE8)), (60, 330, 200, 360, (0xFF, 0xFF, 0xFF))])
    pr = ref.build(card, tmp_path / "exp", aspect="9:16", is_still=True)
    assert pr.ground == "#3d3d3d" and pr.colour == "#2ed8e8"
    arr = np.asarray(Image.open(pr.path))
    assert (arr[..., :3].reshape(-1, 3) == ref._rgb("#2ed8e8")).all()
    assert pr.public()["colour"] == "#2ed8e8"


@pytest.mark.skipif(not HAVE_MASKS, reason="no prepared pattern masks")
def test_structure_strokes_are_solid_not_a_faded_ghost(tmp_path):
    """Structure's alpha is shading; raw, 3 in 4 inked pixels were under half
    opacity and Omni drew the strip faded. Levelled, most strokes are solid."""
    a = _alpha(ref.colorize("structure", "#0461a2", tmp_path / "s.png", width=720, height=1280))
    inked = a[a > 0.02]
    assert (inked >= 0.5).mean() >= 0.5
    assert (a == 0).mean() > 0.3, "the gaps must stay transparent"


@pytest.mark.parametrize("aspect,dims", [
    ("9:16", (720, 1280)), ("16:9", (1280, 720)), ("1:1", (720, 720)),
    ("4:5", (720, 900)), ("nonsense", (720, 1280)),
])
def test_canvas_for_aspects(aspect, dims):
    assert ref.canvas_for(aspect) == dims
