"""pattern_planner: where an auto-placed pattern goes in one clip — the score
ranks the places a person would, the choice is random among the near-best
but reproducible from its seed, nothing it does can break a run, and every
place it can pick keeps the speaker in front of the pattern."""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from app.services import pattern_library as lib
from app.services import pattern_planner as planner
from app.services import rebrand_pattern_ref as ref
from app.services.rebrand_pattern_ref import Layout

W, H = 720, 1280
REAL_MASKS = all(ref._mask_path(s).exists() for s in lib.BUILTINS)

LEFT = Layout(placement="left", scale=0.4, reach=0.4)
RIGHT = Layout(placement="right", scale=0.4, reach=0.4)
TOP = Layout(placement="top", scale=1.0, reach=0.3)
BOTTOM = Layout(placement="bottom", scale=1.0, reach=0.3)


@pytest.fixture
def art(tmp_path) -> Path:
    """A synthetic pattern: rows of solid 3-px bars and posts, 400 x 100."""
    a = np.zeros((100, 400), np.uint8)
    for y in range(10, 88, 8):
        a[y:y + 3, 10:390] = 255
    for x in range(10, 388, 24):
        a[10:90, x:x + 3] = 255
    im = Image.new("RGBA", (400, 100), (255, 255, 255, 0))
    im.putalpha(Image.fromarray(a))
    path = tmp_path / "art.mask.png"
    im.save(path)
    return path


def _frame(tmp_path: Path, name: str, *, fill=(128, 128, 128), bars: int = 0,
           busy_right: bool = False) -> Path:
    a = np.full((H, W, 3), fill, np.uint8)
    if busy_right:
        a[:, W // 2:][:, ::2] = 20                   # fine stripes: a busy shelf of detail
    if bars:
        a[:bars] = 0
        a[-bars:] = 0
    path = tmp_path / f"{name}.png"
    Image.fromarray(a).save(path)
    return path


def _speaker(cx: int, top: int, *, half: int = 170, bottom: int = H) -> np.ndarray:
    """A talking-head silhouette: a head on shoulders, down to `bottom`."""
    m = np.zeros((H, W), np.float32)
    m[top:top + 170, cx - 70:cx + 70] = 1.0
    m[top + 170:bottom, max(0, cx - half):min(W, cx + half)] = 1.0
    return m


def _scores(cands, frame, person, art, colour="#7f2bf2") -> list[float]:
    s = planner.score(cands, frame, person, slug="u-test", mask=art, colour=colour)
    return [r["score"] for r in s["candidates"]]


# ---------------------------------------------------------------------------
# The score ranks what a person would
# ---------------------------------------------------------------------------

def test_the_side_away_from_the_speaker_wins(tmp_path, art):
    frame = _frame(tmp_path, "f")
    left, right = _scores([LEFT, RIGHT], frame, _speaker(210, 240), art)
    assert right > 1.5 * left, "the left strip is behind the speaker and next to their head"
    left, right = _scores([LEFT, RIGHT], frame, _speaker(510, 240), art)
    assert left > 1.5 * right


def test_a_band_in_a_letterbox_bar_beats_a_strip_over_the_picture(tmp_path, art):
    frame = _frame(tmp_path, "lb", bars=300)
    person = _speaker(360, 450, half=120, bottom=H - 300)
    s = planner.score([LEFT, TOP, BOTTOM], frame, person, slug="u-test", mask=art, colour="#7f2bf2")
    by = {r["label"]: r for r in s["candidates"]}
    assert s["letterbox"] > 0.4
    assert by["top-left band"]["score"] > by["left strip"]["score"]
    assert by["bottom-left band"]["factors"]["ground"] == pytest.approx(1.0), "a bar is clean"
    assert by["top-left band"]["factors"]["head"] == pytest.approx(1.0), "never next to the head"


def test_a_speck_on_a_bar_does_not_hide_a_band(tmp_path, art):
    """The planner sees what strip_preview draws: MODNet's specks on a
    letterbox bar are ignored there, so a band in the bar is fully visible."""
    frame = _frame(tmp_path, "lb", bars=300)
    person = _speaker(360, 450, half=120, bottom=H - 300)
    speck = person.copy()
    speck[H - 280:H - 40, 100:620] = 0.6
    clean, specked = (planner.score([BOTTOM], frame, p, slug="u-test", mask=art, colour="#7f2bf2")
                      ["candidates"][0]["factors"]["visible"] for p in (person, speck))
    assert specked == pytest.approx(clean) and clean > 0.99


def test_a_bottom_band_on_footage_is_marked_down(tmp_path, art):
    s = planner.score([BOTTOM], _frame(tmp_path, "f"), _speaker(360, 400, half=100), slug="u-test",
                      mask=art, colour="#7f2bf2")
    assert s["candidates"][0]["factors"]["ground"] == pytest.approx(1 - planner.GROUND_PENALTY)


def test_calm_background_beats_busy_detail(tmp_path, art):
    frame = _frame(tmp_path, "busy", busy_right=True)
    left, right = _scores([LEFT, RIGHT], frame, _speaker(360, 240, half=130), art)
    assert left > right


def test_a_pattern_the_colour_of_the_room_is_illegible(tmp_path, art):
    violet = _frame(tmp_path, "violet", fill=(127, 43, 242))
    grey = _frame(tmp_path, "grey")
    person = _speaker(360, 240, half=130)
    on_violet = planner.score([LEFT], violet, person, slug="u-test", mask=art, colour="#7f2bf2")
    on_grey = planner.score([LEFT], grey, person, slug="u-test", mask=art, colour="#7f2bf2")
    assert on_violet["candidates"][0]["factors"]["legible"] < 0.1
    assert on_grey["candidates"][0]["factors"]["legible"] > 0.8


def test_the_mask_may_come_at_another_size_or_not_at_all(tmp_path, art):
    frame = _frame(tmp_path, "f")
    small = np.asarray(Image.fromarray((_speaker(210, 240) * 255).astype(np.uint8)).resize((180, 320)),
                       np.float32) / 255.0
    left, right = _scores([LEFT, RIGHT], frame, small, art)
    assert right > left
    s = planner.score([LEFT, RIGHT], frame, None, slug="u-test", mask=art)
    assert s["head"] == "none" and all(r["factors"]["visible"] == 1.0 for r in s["candidates"])


# ---------------------------------------------------------------------------
# The choice: random among the near-best, seeded
# ---------------------------------------------------------------------------

def test_the_choice_is_reproducible_from_its_seed_and_varies_across_seeds(tmp_path, art):
    frame = _frame(tmp_path, "f")
    person = _speaker(360, 240, half=130)                     # centred: both sides fit
    kw = dict(slug="u-test", mask=art, colour="#7f2bf2")
    first = planner.choose([LEFT, RIGHT], frame, person, seed="rebrand:7:u-test", **kw)
    again = planner.choose([LEFT, RIGHT], frame, person, seed="rebrand:7:u-test", **kw)
    assert first[0] == again[0] and first[1] == again[1]
    report = first[1]
    assert report["reason"] == "near-best" and len(report["pool"]) == 2
    assert sum(p["p"] for p in report["pool"]) == pytest.approx(1.0, abs=0.01)
    picks = {planner.choose([LEFT, RIGHT], frame, person, seed=f"rebrand:{i}:u-test", **kw)[0]
             for i in range(12)}
    assert picks == {LEFT, RIGHT}, "different experiments draw different places"
    json.dumps(report)                                        # evidence-safe


def test_a_place_far_below_the_best_is_never_drawn(tmp_path, art):
    frame = _frame(tmp_path, "f")
    person = _speaker(180, 240, half=180)                     # the left strip is buried
    kw = dict(slug="u-test", mask=art, colour="#7f2bf2")
    picks = {planner.choose([LEFT, RIGHT], frame, person, seed=f"s{i}", **kw)[0] for i in range(10)}
    assert picks == {RIGHT}


def test_nothing_fits_keeps_the_default_place(tmp_path, art):
    frame = _frame(tmp_path, "f")
    everywhere = np.ones((H, W), np.float32)                  # a speaker filling the frame
    layout, report = planner.choose([LEFT, RIGHT, TOP], frame, everywhere, seed="x", slug="u-test",
                                    mask=art)
    assert layout == LEFT and report["reason"] == "nothing fits"


def test_the_planner_never_raises(tmp_path, art):
    layout, report = planner.choose([RIGHT, LEFT], tmp_path / "missing.png", None, seed="x",
                                    slug="u-test", mask=art)
    assert layout == RIGHT and report["reason"] == "error" and report["error"]
    layout, report = planner.choose([RIGHT, LEFT], _frame(tmp_path, "f"), np.zeros((2, 2, 2)),
                                    seed="x", slug="u-test", mask=art)
    assert layout == RIGHT and report["reason"] == "error"
    assert "failed" in planner.summary(report)


def test_one_candidate_is_simply_returned(tmp_path, art):
    layout, report = planner.choose([TOP], tmp_path / "missing.png", None, seed="x", slug="u-test",
                                    mask=art)
    assert layout == TOP and report["mode"] == "fixed"


def test_the_summary_names_the_pool(tmp_path, art):
    _, report = planner.choose([LEFT, RIGHT], _frame(tmp_path, "f"), _speaker(360, 240, half=130),
                               seed="x", slug="u-test", mask=art)
    line = planner.summary(report)
    assert "left strip" in line and "right strip" in line and len(line) <= 300


# ---------------------------------------------------------------------------
# The real 1.png: every place it can take keeps the speaker in front
# ---------------------------------------------------------------------------

@pytest.mark.skipif(not REAL_MASKS, reason="pattern masks not on this machine")
def test_every_beads_row_place_renders_behind_the_speaker(tmp_path):
    frame = _frame(tmp_path, "green", fill=(10, 200, 30))
    person = np.zeros((H, W), np.float32)
    person[300:, 200:520] = 1.0
    entry = lib.BUILTINS["beads_row"]
    for i, lay in enumerate(entry.candidates):
        pr = ref.build(None, tmp_path / f"c{i}", aspect="9:16", slug=entry.slug, layout=lay,
                       animation=lib.aimed_animation(entry.default_animation, lay))
        arr = np.asarray(Image.open(ref.strip_preview(pr, frame, tmp_path / f"t{i}.png",
                                                      person=person))).astype(int)
        untouched = (arr == (10, 200, 30)).all(axis=2)
        label = ref.placement_label(lay)
        assert untouched[305:, 205:515].all(), f"{label}: a line over the speaker"
        assert (~untouched).mean() > 0.01, f"{label}: nothing drawn"


@pytest.mark.skipif(not REAL_MASKS, reason="pattern masks not on this machine")
def test_beads_row_on_a_letterbox_goes_into_a_bar(tmp_path):
    frame = _frame(tmp_path, "lb", bars=300)
    person = _speaker(360, 450, half=120, bottom=H - 300)
    entry = lib.BUILTINS["beads_row"]
    s = planner.score(entry.candidates, frame, person, slug="beads_row", colour="#7f2bf2")
    best = max(s["candidates"], key=lambda r: r["score"])
    assert best["layout"].placement in ("top", "bottom")


# ---------------------------------------------------------------------------
# Several patterns: occupied ink and taken slots
# ---------------------------------------------------------------------------

REGRESSION = Path(__file__).parent / "fixtures" / "rebrand_planner" / "choose_regression.json"
GOLDEN = Path(__file__).parent / "fixtures" / "rebrand_golden"


@pytest.mark.skipif(not REAL_MASKS, reason="pattern masks not on this machine")
def test_without_occupancy_the_choice_is_byte_for_byte_what_it_was():
    """Recorded with the planner as it was before occupied/taken existed
    (1.png on the golden frame, three seeds, two colours, with and without
    a speaker): None for both reproduces every report exactly."""
    import PIL

    want = json.loads(REGRESSION.read_text(encoding="utf-8"))
    if want["env"] != {"numpy": np.__version__, "pillow": PIL.__version__}:
        pytest.skip(f"recorded with {want['env']}")
    frame = GOLDEN / "frame.png"
    person = np.asarray(Image.open(GOLDEN / "person.png").convert("L"), dtype="float32") / 255.0
    row = lib.BUILTINS["beads_row"]
    for case in want["cases"]:
        for kw in ({}, {"occupied": None, "taken": None}):
            lay, rep = planner.choose(row.candidates, frame,
                                      person if case["person"] == "person" else None,
                                      seed=case["seed"], slug="beads_row", colour=case["colour"], **kw)
            rep = json.loads(json.dumps(rep, default=str))
            rep.pop("frame", None)
            assert lay.public() == case["layout"] and rep == case["report"], case["seed"]


def _left_ink(width=W, height=H, reach=0.45):
    a = np.zeros((height, width), np.float32)
    a[:, : int(width * reach)] = 1.0
    return a


def test_occupied_ink_keeps_a_new_pattern_off_it(tmp_path, art):
    frame = _frame(tmp_path, "f")
    person = _speaker(360, 240, half=100)
    kw = dict(slug="u-test", mask=art, colour="#7f2bf2")
    for i in range(8):
        lay, rep = planner.choose([LEFT, RIGHT, TOP], frame, person, seed=f"s{i}",
                                  occupied=_left_ink(), taken=[], **kw)
        assert lay != LEFT, "the left strip lies on the first pattern's ink"
        blocked = {c["label"]: c["blocked"] for c in rep["candidates"]}
        assert blocked["left strip"] and not blocked["right strip"]
        assert rep["candidates"][0]["factors"]["overlap"] > planner.OVERLAP_MAX
        assert "apart" in rep["candidates"][1]["factors"]


def test_a_taken_slot_is_out_of_the_pool(tmp_path, art):
    frame = _frame(tmp_path, "f")
    person = _speaker(360, 240, half=100)
    kw = dict(slug="u-test", mask=art, colour="#7f2bf2")
    empty = np.zeros((H, W), np.float32)
    picks = {planner.choose([LEFT, RIGHT], frame, person, seed=f"s{i}", occupied=empty,
                            taken=["right"], **kw)[0] for i in range(8)}
    assert picks == {LEFT}


def test_with_every_place_taken_the_least_overlap_is_used(tmp_path, art):
    frame = _frame(tmp_path, "f")
    person = _speaker(360, 240, half=100)
    lay, rep = planner.choose([LEFT, RIGHT], frame, person, seed="x", slug="u-test", mask=art,
                              occupied=_left_ink(), taken=["left", "right"])
    assert lay == RIGHT and rep["reason"] == "no free place"
    assert "taken" in planner.summary(rep)


def test_the_pick_with_occupancy_is_reproducible(tmp_path, art):
    frame = _frame(tmp_path, "f")
    person = _speaker(360, 240, half=100)
    kw = dict(slug="u-test", mask=art, colour="#7f2bf2", occupied=_left_ink(reach=0.2), taken=[])
    a = planner.choose([LEFT, RIGHT, TOP, BOTTOM], frame, person, seed="rebrand:3:pattern-2:u", **kw)
    b = planner.choose([LEFT, RIGHT, TOP, BOTTOM], frame, person, seed="rebrand:3:pattern-2:u", **kw)
    assert a == b


def test_slots_and_footprints():
    assert planner.slot(LEFT) == "left" and planner.slot(TOP) == "top-left"
    assert planner.slot(Layout(placement="bottom", anchor="right")) == "bottom-right"
    assert planner.slot(Layout(placement="full")) == "full"
    ink = np.zeros((320, 180), np.float32)
    ink[:, :20] = 1.0
    fp = planner.footprint(ink, 180, 320)
    grow = round(planner.APART_MARGIN * 180)
    assert fp[:, : 20 + grow].min() == 1.0 and fp[:, 20 + grow + 1:].max() == 0.0
    assert planner.overlap_share(ink, fp) == pytest.approx(1.0)
    assert planner.overlap_share(np.zeros_like(ink), fp) == 0.0
