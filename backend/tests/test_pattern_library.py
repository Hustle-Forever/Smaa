"""The Rebrand pattern library: the built-ins and the owner's placements, the
entrance catalogue (with the approved left-to-right text pinned word for
word), every layout's geometry, and uploads — keyed, masked, analysed by AI,
embedded and stored — plus the API over it."""
from __future__ import annotations

import asyncio
import io
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from app.services import pattern_library as lib
from app.services import rebrand_pattern_ref as ref
from app.services.rebrand_pattern_ref import Layout

REAL_MASKS = all(ref._mask_path(s).exists() for s in lib.BUILTINS)


# ---------------------------------------------------------------------------
# Built-ins and entrances
# ---------------------------------------------------------------------------

def test_the_builtins_follow_the_owners_placements():
    b = lib.BUILTINS
    assert list(b) == ["structure", "beads_row", "beads_field", "blueprint", "grid"]
    assert {k: e.source_file for k, e in b.items()} == {
        "structure": "2.png", "beads_row": "1.png", "beads_field": "5.png",
        "blueprint": "3.png", "grid": "4.png"}
    # 2.png: the approved setup, exactly (checkpoint-omni-pattern-behind-speaker).
    assert b["structure"].layout == Layout(placement="left", scale=2.0, reach=0.5)
    assert b["structure"].candidates == (b["structure"].layout,) and not b["structure"].auto
    assert b["structure"].default_animation == "left_to_right"
    assert lib.DEFAULT_SLUG == "structure"
    # 5.png: turned 90° counter-clockwise and used like 2.png — the left half.
    # 5.png: turned 90° counter-clockwise, used like 2.png, anchored on the
    # frame's bottom so its dense beads (not its hairline end) fill the side.
    assert b["beads_field"].layout == Layout(placement="left", scale=0.75, reach=0.5, rotate=270,
                                             valign="bottom")
    assert not b["beads_field"].auto and b["beads_field"].default_animation == "left_to_right"
    # 3.png and 4.png: the whole screen — one covering copy / a centred grid.
    assert b["blueprint"].layout == Layout(placement="full", scale=1.25, reach=1.0, fit="cover")
    assert b["grid"].layout == Layout(placement="full", scale=6.0, reach=1.0, fit="centre")
    assert not b["blueprint"].auto and not b["grid"].auto
    # 1.png: anywhere — auto-placed per clip between six calculated places,
    # the left strip first (its default).
    row = b["beads_row"]
    assert row.auto and row.layout == row.candidates[0]
    assert [ref.placement_label(c) for c in row.candidates] == [
        "left strip", "right strip", "top-left band", "top-right band",
        "bottom-left band", "bottom-right band"]
    assert all(c.rotate == 270 for c in row.candidates[:2])
    assert all(c.rotate == 180 and c.scale == 2.0 for c in row.candidates[2:])
    assert all(e.source == "builtin" and e.description for e in b.values())


def test_beads_field_reads_to_omni_exactly_like_the_approved_setup():
    field_ = lib.BUILTINS["beads_field"]
    structure = lib.BUILTINS["structure"]
    assert ref.where_text(field_.layout) == ref.where_text(structure.layout)
    assert (lib.entrance_prompt(field_.default_animation, field_.layout)
            == lib.entrance_prompt(structure.default_animation, structure.layout)
            == APPROVED_ENTRANCE)


APPROVED_ENTRANCE = (
    "ENTRANCE: at the very first frame there is no pattern yet, just the original video. "
    "Over the first second the pattern's own lines fade in smoothly from LEFT to RIGHT: "
    "the lines themselves become visible progressively, starting at the frame's left edge "
    "and travelling towards the middle, passing BEHIND the speaker; then they hold still, "
    "exactly as in the attached image, for the rest of the clip. Nothing else is part of "
    "the entrance: no panel, card, frosted-glass sheet, white or grey shape, blur, haze, "
    "glow or light sweep appears at any moment, and the room stays exactly as shot the "
    "whole time."
)


def test_the_approved_entrance_is_word_for_word():
    assert lib.entrance_prompt("left_to_right", lib.BUILTINS["structure"].layout) == APPROVED_ENTRANCE


def test_every_entrance_keeps_the_room_and_draws_no_panel():
    for value, (label, help_, prompt) in lib.ANIMATIONS.items():
        assert label and help_
        if value == "none":
            assert prompt is None and lib.entrance_prompt("none", Layout()) is None
            continue
        assert "BEHIND the speaker" in prompt, value
        assert "no panel, card, frosted-glass sheet" in prompt, value
        assert "wipe" not in prompt.lower(), value            # the word that made Omni draw a panel
    assert lib.normalize_animation(" Glitch ") == "glitch"
    assert lib.normalize_animation("spin") is None and lib.normalize_animation(None) is None
    assert {a["value"] for a in lib.animations_public()} == set(lib.ANIMATIONS)


def test_where_text_per_placement():
    assert ref.where_text(Layout()) == "the left half of the frame, behind the speaker"
    assert ref.where_text(Layout(placement="right", reach=0.4)) == "the right 40% of the frame, behind the speaker"
    assert "top edge" in ref.where_text(Layout(placement="top", anchor="right"))
    assert "top-right corner" in ref.where_text(Layout(placement="top", anchor="right"))
    assert ref.where_text(Layout(placement="full")) == "the whole background of the frame, behind the speaker"


# ---------------------------------------------------------------------------
# Layout geometry (a synthetic mask: a solid block of art)
# ---------------------------------------------------------------------------

def _block_mask(path: Path, size=(400, 100)) -> Path:
    im = Image.new("RGBA", size, (255, 255, 255, 0))
    a = np.zeros((size[1], size[0]), np.uint8)
    a[10:-10, 10:-10] = 255
    im.putalpha(Image.fromarray(a))
    im.save(path)
    return path


def _ref_for(tmp_path, layout, mask) -> ref.PatternRef:
    out = tmp_path / "p.png"
    pr = ref.PatternRef(slug="u-test", source="t.png", path=out, ground="#000000", ink="#ffffff",
                        fell_back=False, colour="#ff0000", layout=layout, mask=mask,
                        where=ref.where_text(layout))
    if layout.placement in ("left", "right"):
        ref.colorize("u-test", "#ff0000", out, width=200, height=360, scale=layout.scale, mask=mask)
    return pr


def _alpha(layer) -> np.ndarray:
    return np.asarray(layer.getchannel("A"))


def test_a_top_band_sits_on_the_top_edge_from_its_corner(tmp_path):
    mask = _block_mask(tmp_path / "m.png")
    a = _alpha(ref._layer(_ref_for(tmp_path, Layout(placement="top", scale=0.8, reach=0.3), mask), 200, 360))
    rows = np.nonzero(a.any(axis=1))[0]
    cols = np.nonzero(a.any(axis=0))[0]
    assert rows[0] < 5 and rows[-1] < 0.3 * 360, "along the top, within its reach"
    assert cols[0] < 5 and cols[-1] < 0.85 * 200, "from the left corner, not centred"


def test_a_bottom_band_from_the_right_corner(tmp_path):
    mask = _block_mask(tmp_path / "m.png")
    a = _alpha(ref._layer(_ref_for(tmp_path, Layout(placement="bottom", scale=0.6, reach=0.3,
                                                    anchor="right"), mask), 200, 360))
    rows = np.nonzero(a.any(axis=1))[0]
    cols = np.nonzero(a.any(axis=0))[0]
    assert rows[-1] > 350 and rows[0] > 0.7 * 360
    assert cols[-1] > 190 and cols[0] > 0.3 * 200


def test_a_band_wider_than_the_frame_bleeds_off_its_far_edge(tmp_path):
    mask = _block_mask(tmp_path / "m.png")
    a = _alpha(ref._layer(_ref_for(tmp_path, Layout(placement="top", scale=1.5, reach=0.5), mask), 200, 360))
    assert a.shape == (360, 200) and a[:, -1].any() and a[:, 0:5].sum() == 0


def test_full_covers_the_whole_background(tmp_path):
    mask = _block_mask(tmp_path / "m.png")
    a = _alpha(ref._layer(_ref_for(tmp_path, Layout(placement="full", scale=0.5), mask), 200, 360))
    assert (a > 0).mean() > 0.4
    assert a[: 360 // 2].any() and a[360 // 2:].any()


def test_right_mirrors_left(tmp_path):
    mask = _block_mask(tmp_path / "m.png")
    a = _alpha(ref._layer(_ref_for(tmp_path, Layout(placement="right", scale=1.0, reach=0.5), mask), 200, 360))
    cols = np.nonzero(a.any(axis=0))[0]
    assert cols[0] == 100, "its first stroke lands half way across"
    assert cols[-1] == 199, "and it runs off the right edge"


def _thinning_mask(path: Path) -> Path:
    """A long art (10:1) like 1.png / 5.png: solid beads over its left 60%,
    then bare 1-px threads to its right end."""
    a = np.zeros((100, 1000), np.uint8)
    for x in range(0, 600, 40):
        a[20:80, x + 5:x + 30] = 255
    a[30:32, :] = 255
    a[68:70, :] = 255
    return _mask_file(path, a)


def test_a_stood_up_strip_on_a_landscape_frame_shows_its_dense_end(tmp_path):
    """Scale is a multiple of the frame's width, worked out on 9:16. On a
    16:9 frame 5.png's stood-up tile is twice as big and a quarter of it
    shows — from the top, only its bare hairline end. A landscape frame
    shows the dense end; a portrait one is untouched (the top, as before)."""
    mask = _thinning_mask(tmp_path / "long.mask.png")        # turned 270: the beads at the BOTTOM

    def drawn(w, h, scale):
        out = ref.colorize("u-long", "#ff0000", tmp_path / f"s{w}.png", width=w, height=h,
                           scale=scale, mask=mask, rotate=270)
        return np.asarray(Image.open(out).getchannel("A")).astype(float) / 255.0

    wide = drawn(400, 200, 0.5)                               # a 2000-px tall tile, 200 shown
    assert (wide >= 0.5).mean() > 0.3, "the beads, not the threads"
    tall = drawn(200, 400, 1.0)                               # portrait: the top 400 of 2000 rows
    tile = ref._alpha_at(mask, "u-long", 200, rotate=270)
    assert np.array_equal(np.rint(tall * 255), np.rint(tile[:400] * 255)), "portrait is unchanged"
    assert (tall >= 0.5).mean() < 0.1, "(which is the thread end on this art)"


def test_a_band_shrunk_inside_the_frame_ends_at_its_last_solid_column(tmp_path):
    """A band is sized so its thinning far end stays off-frame (1.png's
    bare-thread start); shrunk to its height cap on a landscape frame it
    ended inside the frame and trailed its lone threads. It now ends with
    its art's solid run — from either corner — and a band that is not
    shrunk is drawn exactly as before."""
    mask = _thinning_mask(tmp_path / "long.mask.png")
    for anchor in ("left", "right"):
        lay = Layout(placement="top", scale=2.0, reach=0.3, anchor=anchor)
        a = _alpha(ref._layer(_ref_for(tmp_path, lay, mask), 800, 200))
        cols = np.nonzero(a.any(axis=0))[0]
        span = cols[-1] - cols[0] + 1
        assert 0.55 * 600 < span < 0.66 * 600, (anchor, span)  # 600 px of art, 60% of it solid
        assert (cols[0] == 0) if anchor == "left" else (cols[-1] == 799), "from its corner"
    lay = Layout(placement="top", scale=2.0, reach=0.3)
    a = _alpha(ref._layer(_ref_for(tmp_path, lay, mask), 400, 800))
    full = ref._alpha_at(mask, "u-long", 800)[:, :400]
    assert np.array_equal(a[:full.shape[0]], np.rint(full * 255).astype(a.dtype)), "not shrunk"


@pytest.mark.skipif(not REAL_MASKS, reason="pattern masks not on this machine")
def test_every_builtin_renders_behind_the_speaker(tmp_path):
    frame = tmp_path / "f.png"
    Image.new("RGB", (720, 1280), (10, 200, 30)).save(frame)
    person = np.zeros((1280, 720), np.float32)
    person[300:, 200:520] = 1.0
    for slug, e in lib.BUILTINS.items():
        pr = ref.build(None, tmp_path / slug, aspect="9:16", slug=e.slug, layout=e.layout,
                       mask=e.mask_path(), animation=e.default_animation, name=e.name,
                       source_file=e.source_file)
        arr = np.asarray(Image.open(ref.strip_preview(pr, frame, tmp_path / f"{slug}.png",
                                                      person=person))).astype(int)
        untouched = (arr == (10, 200, 30)).all(axis=2)
        assert untouched[305:, 205:515].all(), f"{slug}: a line over the speaker"
        assert (~untouched).mean() > 0.01, f"{slug}: nothing drawn"


def test_a_speck_on_a_letterbox_bar_cuts_no_hole(tmp_path):
    """MODNet leaves specks on letterbox bars (16% of a real reel's bottom
    bar), and they punched see-through holes into every band, grid and
    blueprint line drawn there. A bar is never the speaker: the pattern in
    it stays solid, the speaker in the picture still covers it — and
    structure, whose approved pixels never change, is cut as before."""
    import dataclasses

    frame = tmp_path / "lb.png"
    px = np.zeros((360, 200, 3), np.uint8)
    px[100:260] = (10, 200, 30)                        # the picture; black bars above and below
    Image.fromarray(px).save(frame)
    person = np.zeros((360, 200), np.float32)
    person[150:260, 70:130] = 1.0                      # the speaker, in the picture
    person[300:330, 20:60] = 0.6                       # a speck on the bottom bar
    mask = _block_mask(tmp_path / "m.png", size=(400, 400))
    pr = _ref_for(tmp_path, Layout(placement="full", scale=1.0), mask)
    inked = _alpha(ref._layer(pr, 200, 360)) == 255
    assert inked[300:330, 20:60].all(), "the layer is solid under the speck"

    def drawn(r, name):
        out = np.asarray(Image.open(ref.strip_preview(r, frame, tmp_path / name, person=person)))
        return (out.astype(int) == (255, 0, 0)).all(axis=2)

    d = drawn(pr, "t.png")
    assert d[300:330, 20:60].all(), "solid on the bar"
    assert not d[155:255, 75:125].any(), "still behind the speaker"
    structure = drawn(dataclasses.replace(pr, slug=ref.OMNI_SLUG), "s.png")
    assert not structure[305:325, 25:55].any(), "structure is cut by the mask as approved"
    assert not structure[155:255, 75:125].any()


def test_bar_mask_finds_the_bars_and_only_them():
    px = np.full((100, 60, 3), 128, np.uint8)
    assert ref.bar_mask(px) is None, "no bars"
    px[:20] = 0
    px[-30:] = 3
    bars = ref.bar_mask(px)
    assert bars[:20].all() and bars[-30:].all() and not bars[20:70].any()
    assert ref.bar_mask(np.zeros((100, 60, 3), np.uint8)) is None, "dark throughout is not bars"


# ---------------------------------------------------------------------------
# Uploads
# ---------------------------------------------------------------------------

def _png_bytes(im: Image.Image) -> bytes:
    buf = io.BytesIO()
    im.save(buf, "PNG")
    return buf.getvalue()


def _lines_on(bg, ink, size=(600, 300)) -> Image.Image:
    im = Image.new("RGB", size, bg)
    px = np.asarray(im).copy()
    px[::12, :] = ink
    px[:, ::20] = ink
    return Image.fromarray(px)


def test_detect_key():
    rgba = np.zeros((50, 50, 4), np.uint8)
    rgba[..., 3] = 0
    rgba[10:20, :, 3] = 255
    assert lib.detect_key(rgba) == "alpha"
    assert lib.detect_key(np.asarray(_lines_on((250, 250, 250), (20, 20, 20)).convert("RGBA"))) == "ink"
    assert lib.detect_key(np.asarray(_lines_on((10, 10, 10), (240, 240, 240)).convert("RGBA"))) == "glow"


def test_heuristic_layout_by_shape():
    strip, anim, role = lib.heuristic_layout(5.0, 0.3)
    assert role == "strip" and anim == "left_to_right"
    assert {c.placement for c in strip} == {"left", "right", "top", "bottom"}, "a strip goes anywhere"
    assert strip[0].placement == "left" and len(strip) == 6
    field_, anim, _ = lib.heuristic_layout(1.0, 0.05)
    assert [c.placement for c in field_] == ["full"] and field_[0].fit == "cover" and anim == "fade"
    texture, _, _ = lib.heuristic_layout(1.0, 0.4, lib.ArtProfile(aspect=1.0, tiles=True, cell=0.05))
    assert texture == (Layout(placement="full", scale=2.5, reach=1.0, fit="centre"),)
    hero, anim, role = lib.heuristic_layout(1.2, 0.4)
    assert hero == (Layout(placement="left", scale=2.15, reach=0.5),) and role == "structure"


# ---------------------------------------------------------------------------
# Placement arithmetic
# ---------------------------------------------------------------------------

@pytest.mark.skipif(not REAL_MASKS, reason="pattern masks not on this machine")
def test_the_art_is_measured():
    row = lib.measure_art(ref._mask_path("beads_row"))
    assert row.dense_end == "right" and 0.70 < row.run < 0.80 and not row.tiles
    assert abs(row.aspect - 5.161) < 0.01
    field_ = lib.measure_art(ref._mask_path("beads_field"))
    assert field_.dense_end == "left" and not field_.tiles
    grid = lib.measure_art(ref._mask_path("grid"))
    assert grid.tiles and abs(grid.cell * 2400 - 50) < 1, "the graph paper's 50-px cell"
    assert not lib.measure_art(ref._mask_path("blueprint")).tiles
    assert not lib.measure_art(ref._mask_path("structure")).tiles


def _mask_file(path: Path, alpha: np.ndarray) -> Path:
    im = Image.new("RGBA", (alpha.shape[1], alpha.shape[0]), (255, 255, 255, 0))
    im.putalpha(Image.fromarray(alpha.astype(np.uint8)))
    im.save(path)
    return path


def test_a_symmetric_figure_is_not_a_texture(tmp_path):
    """Art symmetric on both axes has matching opposite edges — what the wrap
    test reads as a texture — so a round emblem was drawn as a stack of
    three copies, and concentric rectangles (whose column profile does
    repeat) were blown up to a fragment. Neither repeats evenly inside
    itself: both are one figure."""
    import math

    from PIL import ImageDraw

    w = 800
    emblem = Image.new("L", (w, w), 0)
    d = ImageDraw.Draw(emblem)
    d.ellipse((0, 0, w - 1, w - 1), outline=255, width=16)
    c = w / 2
    d.polygon([(c + (360 if k % 2 == 0 else 170) * math.cos(math.pi * k / 8),
                c + (360 if k % 2 == 0 else 170) * math.sin(math.pi * k / 8)) for k in range(16)],
              outline=255, width=12)
    d.ellipse((c - 80, c - 80, c + 80, c + 80), outline=255, width=12)
    path = _mask_file(tmp_path / "emblem.mask.png", np.asarray(emblem))
    a = np.asarray(emblem, np.float32) / 255.0
    assert min(lib._wrap(a)) > lib.TILE_WRAP_MIN, "the trap: its edges match"
    assert not lib.measure_art(path).tiles

    hero = Image.new("L", (1000, 800), 0)
    d = ImageDraw.Draw(hero)
    for k in range(0, 360, 24):
        d.rectangle((100 + k, 40 + 0.8 * k, 900 - k, 760 - 0.8 * k), outline=255, width=6)
    d.line((0, 0, 1000, 800), fill=255, width=8)
    d.line((0, 800, 1000, 0), fill=255, width=8)
    hero_art = lib.measure_art(_mask_file(tmp_path / "hero.mask.png", np.asarray(hero)))
    assert not hero_art.tiles and hero_art.cell is None
    # Without Gemini a figure is never tiled: it goes where its coverage says.
    cands, _, role = lib.heuristic_layout(hero_art.aspect, 0.2, hero_art)
    assert role == "structure" and cands[0].fit == "tile" and cands[0].placement == "left"


@pytest.mark.parametrize("name,starts,size", [
    ("line on one border", 0, 1200),           # range(0, W, step): a line on the left, paper on the right
    ("lines on both borders", 0, 1145),        # trimmed to its ink: a line on both borders
    ("no line on a border", 30, 1200),
])
def test_a_lattice_is_a_texture_however_its_borders_fell(tmp_path, name, starts, size):
    """A lattice whose border IS a line gave a flat edge profile, and a flat
    profile correlates with nothing: judged a composition, it was boxed into
    the left half as graph paper ending in a straight edge. Any lattice is a
    texture with its own cell, and fills the whole background."""
    a = np.zeros((size, size), np.uint8)
    for v in range(starts, size, 60):
        a[:, v:v + 5] = 255
        a[v:v + 5, :] = 255
    art = lib.measure_art(_mask_file(tmp_path / "lattice.mask.png", a))
    assert art.tiles, name
    assert abs(art.cell * size - 60) < 1, name
    cands, _, role = lib.heuristic_layout(art.aspect, 0.16, art)
    assert role == "field" and cands == (Layout(placement="full", scale=cands[0].scale, reach=1.0,
                                                 fit="centre"),), name


@pytest.mark.skipif(not REAL_MASKS, reason="pattern masks not on this machine")
def test_fit_layouts_recalculates_the_builtins_from_their_art():
    """The study's placements are what the arithmetic gives, so an upload of
    the same art would be placed the same way."""
    fit = lib.fit_profile
    row = lib.measure_art(ref._mask_path("beads_row"))
    assert fit(row, ["left", "right", "top", "bottom"]) == lib.BUILTINS["beads_row"].candidates
    assert fit(lib.measure_art(ref._mask_path("grid")), ["full"]) == (lib.BUILTINS["grid"].layout,)
    assert (fit(lib.measure_art(ref._mask_path("blueprint")), ["full"])
            == (lib.BUILTINS["blueprint"].layout,))


def test_fit_layouts_formulas():
    # A long strip stood up: dense end at the top, reaching the middle; a
    # shorter solid run needs a bigger strip so the sparse end stays off-frame.
    (left,) = lib.fit_layouts(4.0, ["left"], dense_end="right")
    assert left == Layout(placement="left", scale=0.5, reach=0.5, rotate=270)
    (left,) = lib.fit_layouts(4.0, ["left"], dense_end="left", run=0.5)
    assert left.rotate == 90 and left.scale == 0.9            # 1280 / (720 * 4 * 0.5) = 0.89
    # Seam-free: a short strip is drawn big enough to cover the height once.
    (left,) = lib.fit_layouts(2.0, ["left"])
    assert left.scale * 720 * 2.0 >= 1280
    # A band: as tall as a 1:1 source's letterbox bar, from each corner when
    # there are several places, its dense end in the corner.
    bands = lib.fit_layouts(5.161, ["top", "bottom"], dense_end="right")
    assert [(c.placement, c.anchor) for c in bands] == [
        ("top", "left"), ("top", "right"), ("bottom", "left"), ("bottom", "right")]
    assert all(c.scale == 2.0 and c.rotate == 180 and c.reach == 0.3 for c in bands)
    band_h = 720 * bands[0].scale / 5.161
    assert band_h <= (1280 - 720) / 2, "inside the bar of any source up to 1:1"
    # One place only = one layout (fixed), anchored on the dense end's corner.
    assert lib.fit_layouts(5.0, ["top"]) == (Layout(placement="top", scale=1.9, reach=0.3),)
    # A wide composition covering a portrait frame is turned a quarter.
    (full,) = lib.fit_layouts(2.0, ["full"])
    assert full.fit == "cover" and full.rotate == 90
    # Nonsense places are dropped; the order given is kept (first = default).
    assert [c.placement for c in lib.fit_layouts(1.0, ["full", "diagonal", "left"])] == ["full", "left"]


def test_an_auto_entry_in_public():
    pub = lib.BUILTINS["beads_row"].public()
    assert pub["placement"] == "auto"
    assert [p["label"] for p in pub["placements"]][:3] == ["left strip", "right strip", "top-left band"]
    assert set(pub["placements"][0]) == {"placement", "anchor", "rotate", "label"}
    assert pub["where"].startswith("placed for each clip where it fits best")
    assert "top or bottom edge" in pub["where"] and "left or right side" in pub["where"]
    fixed = lib.BUILTINS["structure"].public()
    assert fixed["placement"] == "left" and len(fixed["placements"]) == 1
    assert fixed["where"] == "the left half of the frame, behind the speaker"


def test_the_entrance_is_aimed_at_the_drawn_place():
    row = lib.BUILTINS["beads_row"].candidates
    aimed = [lib.aimed_animation("left_to_right", c) for c in row]
    assert aimed == ["left_to_right", "right_to_left", "left_to_right", "right_to_left",
                     "left_to_right", "right_to_left"]
    assert lib.aimed_animation("fade", row[1]) == "fade"
    assert lib.aimed_animation("right_to_left", row[0]) == "left_to_right"
    # A left strip keeps the approved entrance word for word.
    assert lib.entrance_prompt(aimed[0], row[0]) == APPROVED_ENTRANCE


@pytest.fixture
def upload_env(tmp_path, monkeypatch):
    """No network, no database: the analysis, the embedding and the insert
    are faked and recorded."""
    monkeypatch.setattr(lib, "CACHE_DIR", tmp_path / "lib")
    state: dict = {"analysis": {"name": "Circuit Mesh", "description": "A fine circuit mesh.",
                                "role": "grid", "density": "medium", "placements": ["full"],
                                "animation": "glitch", "tags": ["tech"]},
                   "inserted": None}

    async def analyse(prev, thumb, *, aspect, coverage):
        state["analysed"] = (Path(prev).exists(), Path(thumb).exists(), aspect, coverage)
        return dict(state["analysis"])

    async def insert(entry, embed_text, embedding):
        state["inserted"] = (entry, embed_text, embedding)

    from app.services import vertex_client

    async def embed(texts, **kw):
        return [[0.1] * 768 for _ in texts]

    monkeypatch.setattr(lib, "analyse", analyse)
    monkeypatch.setattr(lib, "_insert", insert)
    monkeypatch.setattr(vertex_client, "gemini_embed", embed)
    return state


def test_an_upload_is_masked_analysed_embedded_and_stored(upload_env):
    data = _png_bytes(_lines_on((250, 250, 250), (30, 30, 30)))
    e = asyncio.run(lib.add_upload(data, "my mesh.png", None))
    assert e.source == "upload" and e.slug.startswith("u-my-mesh-")
    assert e.name == "Circuit Mesh", "the AI's name when the user gave none"
    assert e.description == "A fine circuit mesh."
    assert e.layout.placement == "full" and e.default_animation == "glitch"
    assert e.analysis["key"] == "ink" and e.analysis["analysed_by"] == "gemini"
    assert upload_env["analysed"][0] and upload_env["analysed"][1]
    entry, embed_text, embedding = upload_env["inserted"]
    assert entry is e and len(embedding) == 768 and "Circuit Mesh" in embed_text
    # The mask the renderer reads is there, and so is the picker's preview.
    assert e.mask_bytes and e.mask_path().exists()
    assert lib.preview(e).exists()
    pub = e.public()
    assert pub["deletable"] and pub["preview_url"] == f"/api/rebrand/patterns/{e.slug}/preview"


def test_a_users_name_wins_and_no_ai_falls_back_to_the_rules(upload_env):
    upload_env["analysis"] = {}
    strip = Image.new("RGBA", (1000, 150), (0, 0, 0, 0))
    a = np.zeros((150, 1000), np.uint8)
    a[40:110:10, 20:980] = 255
    strip.putalpha(Image.fromarray(a))
    e = asyncio.run(lib.add_upload(_png_bytes(strip), "row.png", "Our Row"))
    assert e.name == "Our Row"
    assert e.analysis["analysed_by"] == "heuristic" and e.analysis["key"] == "alpha"
    # A long strip can go anywhere, like 1.png: auto-placed, each place calculated.
    assert e.auto and e.analysis["placed_by"] == "heuristic"
    assert {c.placement for c in e.candidates} == {"left", "right", "top", "bottom"}
    assert e.candidates == lib.fit_profile(lib.measure_art(e.mask_path()),
                                           ["left", "right", "top", "bottom"])
    assert e.analysis["profile"]["dense_end"] is None and e.analysis["profile"]["aspect"] > 6
    assert e.public()["placement"] == "auto"


def test_bad_uploads_are_refused(upload_env):
    with pytest.raises(ValueError):
        asyncio.run(lib.add_upload(b"", "x.png"))
    with pytest.raises(ValueError):
        asyncio.run(lib.add_upload(b"not an image at all", "x.png"))
    blank = _png_bytes(Image.new("RGB", (200, 200), (255, 255, 255)))
    with pytest.raises(ValueError, match="No line work"):
        asyncio.run(lib.add_upload(blank, "blank.png"))


def test_an_upload_renders_like_a_builtin(upload_env, tmp_path):
    e = asyncio.run(lib.add_upload(_png_bytes(_lines_on((250, 250, 250), (30, 30, 30))), "m.png"))
    frame = tmp_path / "f.png"
    Image.new("RGB", (720, 1280), (10, 200, 30)).save(frame)
    pr = ref.build(None, tmp_path / "exp", aspect="9:16", slug=e.slug, layout=e.layout,
                   mask=e.mask_path(), animation=e.default_animation, name=e.name,
                   source_file=e.source_file)
    arr = np.asarray(Image.open(ref.strip_preview(pr, frame, tmp_path / "t.png"))).astype(int)
    assert (~(arr == (10, 200, 30)).all(axis=2)).mean() > 0.05
    assert pr.public()["layout"]["placement"] == "full" and pr.animation == "glitch"


def _strip_png() -> bytes:
    strip = Image.new("RGBA", (1000, 150), (0, 0, 0, 0))
    a = np.zeros((150, 1000), np.uint8)
    a[40:110:10, 20:980] = 255
    strip.putalpha(Image.fromarray(a))
    return _png_bytes(strip)


def test_an_upload_allowed_several_places_is_auto_placed(upload_env):
    upload_env["analysis"] = {"name": "Ticker", "description": "A ticker row.", "role": "strip",
                              "density": "sparse", "placements": ["top", "left", "bogus", "top"],
                              "animation": "left_to_right"}
    e = asyncio.run(lib.add_upload(_strip_png(), "ticker.png"))
    assert e.auto and e.analysis["placed_by"] == "gemini"
    # The AI's order is kept (its best place is the default); each place calculated.
    assert [ref.placement_label(c) for c in e.candidates] == [
        "top-left band", "top-right band", "left strip"]
    assert e.layout == e.candidates[0]
    assert e.analysis["placements"] == ["top", "left"]
    pub = e.public()
    assert pub["placement"] == "auto" and len(pub["placements"]) == 3
    entry, embed_text, _ = upload_env["inserted"]
    assert "fits best" in embed_text
    # ...and it is stored so it reads back the same.
    assert lib._layouts_from(lib._layout_json(entry.candidates)) == e.candidates


def test_an_upload_allowed_one_place_is_fixed(upload_env):
    upload_env["analysis"] = {"name": "Hero", "description": "A hero.", "role": "structure",
                              "density": "dense", "placement": "left", "animation": "fade"}
    e = asyncio.run(lib.add_upload(_strip_png(), "hero.png"))
    assert not e.auto and e.layout.placement == "left", "an old-style single placement still reads"
    assert e.public()["where"] == ref.where_text(e.layout)


def test_old_single_layout_rows_still_load_as_fixed():
    import json

    row = {"slug": "u-old-abc123", "name": "Old", "description": "", "source_file": "o.png",
           "layout": {"placement": "top", "scale": 1.3, "reach": 0.3, "anchor": "right", "rotate": 0},
           "default_animation": "left_to_right", "analysis": None, "mask_png": b"x",
           "created_at": None}
    e = lib._entry_from_row(row)
    assert not e.auto
    assert e.layout == Layout(placement="top", scale=1.3, reach=0.3, anchor="right")
    assert e.layout.fit == "tile", "an old full-screen row keeps tiling as it did"
    row["layout"] = json.dumps(row["layout"])            # some drivers hand jsonb back as text
    assert lib._entry_from_row(row).layout == e.layout
    assert e.public()["placement"] == "top"


def test_candidates_round_trip_through_the_row():
    import json

    cands = lib.BUILTINS["beads_row"].candidates
    stored = lib._layout_json(cands)
    # The first candidate's fields at the top level: what a reader that knows
    # nothing of candidates sees.
    assert {k: stored[k] for k in cands[0].public()} == cands[0].public()
    assert lib._layouts_from(json.loads(json.dumps(stored))) == cands
    assert "candidates" not in lib._layout_json((Layout(),))
    grid = lib.BUILTINS["grid"].layout
    assert lib._layouts_from(lib._layout_json((grid,))) == (grid,), "scale 6 and fit survive"


# ---------------------------------------------------------------------------
# Full-screen fits
# ---------------------------------------------------------------------------

def test_the_default_fit_still_tiles_from_the_top_left(tmp_path):
    mask = _block_mask(tmp_path / "m.png")
    tiled = _alpha(ref._layer(_ref_for(tmp_path, Layout(placement="full", scale=0.5), mask), 200, 360))
    explicit = _alpha(ref._layer(_ref_for(tmp_path, Layout(placement="full", scale=0.5, fit="tile"),
                                          mask), 200, 360))
    assert (tiled == explicit).all()
    assert Layout().fit == "tile"


def test_a_centred_texture_is_symmetric_about_the_speaker(tmp_path):
    mask = _block_mask(tmp_path / "m.png", size=(100, 100))
    a = _alpha(ref._layer(_ref_for(tmp_path, Layout(placement="full", scale=0.3, fit="centre"),
                                   mask), 200, 360)).astype(int)
    assert np.abs(a - a[:, ::-1]).max() <= 2, "the tiling is centred on the frame"


@pytest.mark.skipif(not REAL_MASKS, reason="pattern masks not on this machine")
def test_the_grid_is_solid_lines_not_a_haze():
    """Every inked pixel sits next to a fully opaque one (the 50-px lines are
    solid wherever the kit's downscale left them), and the faint sub-grid is
    gone. Before: 31% had a solid core and the pattern covered 29% of the
    frame as a haze."""
    from PIL import ImageFilter

    e = lib.BUILTINS["grid"]
    r = ref.PatternRef(slug="grid", source="4.png", path=Path("unused.png"), ground="#000000",
                       ink="#ffffff", fell_back=False, layout=e.layout)
    for w, h in ((720, 1280), (1280, 720)):
        a = np.asarray(ref._layer(r, w, h).getchannel("A"))
        inked = a > 12
        core = np.asarray(Image.fromarray(a).filter(ImageFilter.MaxFilter(3))) >= 250
        assert core[inked].mean() > 0.97, (w, h)
        assert 0.04 < inked.mean() < 0.15, "lines, not a haze over the frame"
        # 90-px cells on the short side, a line through the frame's centre column.
        assert a[:, w // 2 - 2: w // 2 + 3].max(axis=1).mean() > 0.9 * 255


@pytest.mark.skipif(not REAL_MASKS, reason="pattern masks not on this machine")
def test_blueprint_covers_the_frame_with_one_copy():
    e = lib.BUILTINS["blueprint"]
    r = ref.PatternRef(slug="blueprint", source="3.png", path=Path("unused.png"), ground="#000000",
                       ink="#ffffff", fell_back=False, layout=e.layout)
    for w, h in ((720, 1280), (1280, 720)):
        a = np.asarray(ref._layer(r, w, h).getchannel("A"))
        assert (a > 0).any(axis=1).all() and (a > 0).any(axis=0).all(), "every row and column"
        assert (a == np.asarray(ref._layer(r, w, h).getchannel("A"))).all(), "deterministic"
        # One copy: the window is a crop of the single covering render.
        src = ref._mask_path("blueprint")
        with Image.open(src) as im:
            mw, mh = im.size
        one = ref._alpha_at(src, "blueprint", max(w, int(np.ceil(max(w, h * mw / mh) * 1.25))))
        assert one.shape[0] >= h and one.shape[1] >= w
        x0, y0 = ref._focus_crop(one, w, h)
        assert np.abs(np.rint(one[y0:y0 + h, x0:x0 + w] * 255) - a).max() <= 1


# ---------------------------------------------------------------------------
# The API
# ---------------------------------------------------------------------------

@pytest.fixture
def client(monkeypatch, tmp_path):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.routers import rebrand as rt

    monkeypatch.setattr(lib, "CACHE_DIR", tmp_path / "lib")

    async def no_uploads(slug=None):
        return []

    monkeypatch.setattr(lib, "_uploads", no_uploads)
    app = FastAPI()
    app.include_router(rt.router)
    return TestClient(app)


def test_the_list_has_every_builtin_and_the_entrances(client):
    r = client.get("/api/rebrand/patterns")
    assert r.status_code == 200
    body = r.json()
    assert [p["slug"] for p in body["patterns"]] == list(lib.BUILTINS)
    assert body["default_pattern"] == "structure"
    assert {a["value"] for a in body["animations"]} == set(lib.ANIMATIONS)
    s = body["patterns"][0]
    assert s["where"] == "the left half of the frame, behind the speaker" and not s["deletable"]


@pytest.mark.skipif(not REAL_MASKS, reason="pattern masks not on this machine")
def test_a_preview_is_a_png(client):
    r = client.get("/api/rebrand/patterns/structure/preview")
    assert r.status_code == 200 and r.headers["content-type"] == "image/png"
    assert client.get("/api/rebrand/patterns/nope/preview").status_code == 404


def test_builtins_cannot_be_deleted_and_bad_uploads_are_422(client, monkeypatch):
    assert client.delete("/api/rebrand/patterns/structure").status_code == 403

    async def refuse(data, filename, name=None):
        raise ValueError("No line work found in the image.")

    monkeypatch.setattr(lib, "add_upload", refuse)
    r = client.post("/api/rebrand/patterns", files={"file": ("x.png", b"123", "image/png")})
    assert r.status_code == 422 and "No line work" in r.json()["detail"]


def test_bottom_valign_puts_the_arts_bottom_on_the_frames_bottom(tmp_path):
    """A tall art (taller than the canvas) anchored at the bottom shows its
    BOTTOM rows; the default ("top") shows its top rows, as before."""
    mask = tmp_path / "tall.png"
    a = np.zeros((400, 100), np.uint8)
    a[:200, 40:60] = 255                          # ink only in the top half ...
    a[300:, 10:90] = 255                          # ... and a solid block at the bottom
    im = Image.new("RGBA", (100, 400), (255, 255, 255, 0))
    im.putalpha(Image.fromarray(a))
    im.save(mask)
    top = np.asarray(Image.open(ref.colorize("u-t", "#ff0000", tmp_path / "top.png", width=100,
                                             height=200, mask=mask)).getchannel("A"))
    bot = np.asarray(Image.open(ref.colorize("u-t", "#ff0000", tmp_path / "bot.png", width=100,
                                             height=200, mask=mask, valign="bottom")).getchannel("A"))
    assert top[:, 10:30].max() == 0 and top[:, 45:55].min() > 200, "top: the thin top half"
    assert bot[150:, 15:85].min() > 200, "bottom: the solid block sits on the bottom edge"
    assert Layout().valign == "top" and "valign" not in Layout().public()
    assert Layout(valign="bottom").public()["valign"] == "bottom"
    assert lib._layout_from({"valign": "bottom"}).valign == "bottom"
    assert lib._layout_from({"valign": "sideways"}).valign == "top"
