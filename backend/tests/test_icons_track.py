"""Rebrand studio icons, the speaker track (rebrand_harness/speaker_track.py):
the fixed spot grid per layout, the hard rules over an icon's window (off the
head, the speaker's overlap, bars excluded), resizing a spot, the cache.

The matte is faked except in the real-clip test at the bottom, which runs only
where the pinned MODNet model is already cached and the studio's 806/815
scenes are on disk (local, free). No paid call can run."""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from app.services import pattern_planner
from app.services import speaker_front
from app.services import vertex_client
from app.services.rebrand_harness import speaker_track as T

cv2 = pytest.importorskip("cv2")
STUDIO = Path(__file__).resolve().parents[1] / "data" / "yas_producer" / "talking-head-v2" / "rebrand" / "_studio"


@pytest.fixture(autouse=True)
def _no_paid_calls(monkeypatch):
    async def refuse(*_a, **_k):
        raise AssertionError("a paid model call in a test")

    monkeypatch.setattr(vertex_client, "gemini_multimodal", refuse)
    monkeypatch.setattr(vertex_client, "gemini_image", refuse)


def ids(spots):
    return [s["id"] for s in spots]


def px(box, W, H):
    return T._px(box, W, H)


# ---------------------------------------------------------------------------
# The spot grid
# ---------------------------------------------------------------------------

def test_full_bleed_portrait_grid():
    spots = T.spot_grid(720, 1280)
    assert ids(spots) == ["L1", "L2", "L3", "L4", "R1", "R2", "R3", "R4", "TL", "TR"]
    L = 0.16 * 720
    for s in spots:
        x0, y0, x1, y1 = px(s["box"], 720, 1280)
        assert abs((x1 - x0) - L) <= 1 and abs((y1 - y0) - L) <= 1
    for k, yf in enumerate(T.FULL_YS, 1):
        l, r = T._px(spots[k - 1]["box"], 720, 1280), T._px(spots[k + 3]["box"], 720, 1280)
        assert abs(l[0] - 0.04 * 720) <= 1 and abs(720 - r[2] - 0.04 * 720) <= 1
        assert abs((l[1] + l[3]) / 2 - yf * 1280) <= 1
    assert {s["group"] for s in spots} == {"left", "right", "top"}
    assert T.anchors_of(spots) == ["auto", "left", "right", "top"]
    tl = px(spots[8]["box"], 720, 1280)
    assert abs(tl[0] - 29) <= 1 and abs(tl[1] - 29) <= 1


def test_letterboxed_grid_is_the_bars_plus_the_picture_edges():
    # 806: a 720 x 480 picture between two 400-row bars.
    spots = T.spot_grid(720, 1280, (400, 880, 0, 720), "letterbox")
    assert ids(spots) == ["TB-L", "TB-C", "TB-R", "BB-L", "BB-C", "BB-R", "PL", "PR"]
    for s in spots[:6]:
        x0, y0, x1, y1 = px(s["box"], 720, 1280)
        assert abs((x1 - x0) - 158) <= 1                                  # min(0.7 x 400, 0.22 x 720)
    tb = px(spots[1]["box"], 720, 1280)
    assert abs((tb[1] + tb[3]) / 2 - 200) <= 1 and abs((tb[0] + tb[2]) / 2 - 360) <= 1
    bb = px(spots[4]["box"], 720, 1280)
    assert abs((bb[1] + bb[3]) / 2 - 1080) <= 1
    pl = px(spots[6]["box"], 720, 1280)
    assert 400 <= pl[1] and pl[3] <= 880                                  # on the picture
    assert T.anchors_of(spots) == ["auto", "left", "right", "top", "bottom"]


def test_pillar_box_bars_get_side_spots():
    spots = T.spot_grid(1280, 720, (0, 720, 280, 1000), "letterbox")
    assert {"LB-1", "LB-2", "RB-1", "RB-2", "PL", "PR"} <= set(ids(spots))
    assert not any(i.startswith(("TB", "BB")) for i in ids(spots))


def test_landscape_grid_is_the_outer_thirds():
    spots = T.spot_grid(1280, 720)
    assert ids(spots) == ["L1", "L2", "R1", "R2"]
    for s, cx, cy in zip(spots, (1280 / 6, 1280 / 6, 5 * 1280 / 6, 5 * 1280 / 6), (216, 360, 216, 360)):
        x0, y0, x1, y1 = px(s["box"], 1280, 720)
        assert abs((x0 + x1) / 2 - cx) <= 1 and abs((y0 + y1) / 2 - cy) <= 1


def test_thin_bars_are_not_a_letterbox():
    assert T.layout_of(720, 1280, (0, 1280, 2, 718), True) == "full"
    assert T.layout_of(720, 1280, (40, 1240, 0, 720), True) == "full"     # 40 rows < 0.06 H
    assert T.layout_of(720, 1280, (400, 880, 0, 720), True) == "letterbox"
    assert T.layout_of(1280, 720, (0, 720, 0, 1280), False) == "landscape"


# ---------------------------------------------------------------------------
# A synthetic track: a speaker in the middle, a hand up at 2.5 s
# ---------------------------------------------------------------------------

W, H, FPS = 720, 1280, 30.0


def _masks(n: int, hand_from: int = 5, bar_speck: bool = False) -> np.ndarray:
    h8, w8 = H // 8, W // 8
    out = np.zeros((n, h8, w8), np.uint8)
    out[:, 70:, 25:65] = 255                                              # body: x 200-520, y 560-
    out[:, 25:70, 30:60] = 255                                            # head: x 240-480, y 200-560
    for i in range(hand_from, n):
        out[i, 60:75, 65:88] = 255                                        # a hand: x 520-704, y 480-600
    if bar_speck:
        out[:, 0:10, 0:20] = 255                                          # MODNet specks in the top bar
    return out


def _track(n: int = 8, *, hand_from: int = 5, bars=None, layout="full", picture=None, frame0=None,
           bar_speck=False) -> T.Track:
    heads = np.tile(np.array([[240, 200, 480, 560]], np.int32), (n, 1))
    pic = picture or (0, H, 0, W)
    return T.Track(width=W, height=H, fps=FPS, frames=n * 15, step=15,
                   times=np.arange(n, dtype=np.float32) * 0.5, heads=heads,
                   masks=_masks(n, hand_from, bar_speck), bars=bars, layout=layout, picture=pic,
                   spots=T.spot_grid(W, H, picture if layout == "letterbox" else None, layout),
                   frame0=frame0, key="k")


def test_samples_in_brackets_the_window():
    tr = _track()
    assert tr.samples_in(0.0, 0.0) == [0]
    assert tr.samples_in(0.5, 0.5) == [1]
    assert tr.samples_in(0.6, 1.2) == [1, 2, 3]
    assert tr.samples_in(-1.0, 99.0) == list(range(8))


def test_overlap_and_first_blocked_see_the_hand_come_up():
    tr = _track(hand_from=5)                                              # the hand is up from 2.5 s
    r3 = tr.spot("R3")["box"]                                             # x 576-691, y 480-595
    assert tr.overlap(r3, 0.0, 2.0) == 0.0 and tr.feasible(r3, 0.0, 2.0)
    assert tr.overlap(r3, 2.0, 3.0) > T.OVERLAP_MAX and not tr.feasible(r3, 2.0, 3.0)
    assert tr.first_blocked(r3, 0.0, 3.5) == 2.5
    assert tr.first_blocked(tr.spot("L1")["box"], 0.0, 3.5) is None
    m, heads = tr.union(2.4, 2.6)                                         # samples 2.0, 2.5, 3.0
    assert m.shape == (H // 8, W // 8) and m.max() == 1.0 and heads == [(240, 200, 480, 560)] * 3
    assert m[60:75, 65:88].min() == 1.0                                   # the hand is in the union


def test_head_clear_uses_the_grown_head_box():
    tr = _track()
    near = {"x": 490 / W, "y": 300 / H, "w": 100 / W, "h": 100 / H}      # 10 px right of the head
    assert not tr.head_clear(near, 0, 1)                                  # grown by 0.02 W = 14 px
    assert tr.head_clear(near, 0, 1, grow=0.0)
    assert tr.head_clear(tr.spot("L1")["box"], 0, 4)
    # icons.md's 0.35-head growth would leave no side spot at all on a close-up.
    grown = [s["id"] for s in tr.spots if s["group"] != "top" and tr.head_clear(s["box"], 0, 4, grow=0.35)]
    assert grown == []


def test_the_bars_are_never_the_speaker():
    bars = np.zeros((H, W), bool)
    bars[:400] = True
    bars[880:] = True
    tr = _track(bars=bars, layout="letterbox", picture=(400, 880, 0, W), bar_speck=True)
    tb = tr.spot("TB-L")["box"]
    assert tr.overlap(tb, 0, 4) == 0.0
    m, _h = tr.union(0, 4)
    assert m[:10, :20].max() == 0.0
    assert tr.bar_mask() is bars


def test_sized_keeps_the_pinned_side_and_stays_in_the_frame():
    tr = _track()
    l1 = tr.spot("L1")
    big = tr.sized(l1, 0.20)
    assert big["x"] == pytest.approx(l1["box"]["x"], abs=1e-3)           # the left edge stays
    assert big["w"] * W == pytest.approx(0.20 * 720, abs=1)
    r1 = tr.sized(tr.spot("R1"), 0.20)
    assert (r1["x"] + r1["w"]) == pytest.approx(tr.spot("R1")["box"]["x"] + tr.spot("R1")["box"]["w"], abs=1e-3)
    tr_corner = tr.sized(tr.spot("TR"), 0.12)
    assert tr_corner["y"] == pytest.approx(tr.spot("TR")["box"]["y"], abs=1e-3)
    assert tr.sized(l1, None) == l1["box"]
    assert tr.inside(tr.sized(tr.spot("L4"), 0.30))


def test_bar_spots_scale_from_their_own_size():
    bars = np.zeros((H, W), bool)
    bars[:400] = True
    bars[880:] = True
    tr = _track(bars=bars, layout="letterbox", picture=(400, 880, 0, W))
    tb = tr.spot("TB-C")
    big = tr.sized(tb, 0.20)
    assert big["w"] * W == pytest.approx(158 * 1.25, abs=1.5)
    cx = lambda b: b["x"] + b["w"] / 2  # noqa: E731
    assert cx(big) == pytest.approx(cx(tb["box"]), abs=1e-3)


def test_calm_is_high_on_a_plain_wall_and_low_on_stripes(tmp_path):
    img = np.full((H, W, 3), 180, np.uint8)
    img[:, 560:, :][:, :, :] = 180
    for x in range(560, W, 6):
        img[:, x:x + 3] = 20                                              # a striped poster on the right
    path = tmp_path / "f0.png"
    cv2.imwrite(str(path), img)
    tr = _track(frame0=path)
    left = tr.calm({"box": tr.spot("L2")["box"]})
    right = tr.calm({"box": tr.spot("R2")["box"]})
    assert left == 1.0 and right < 0.6
    stored = dict(tr.spot("R2"), calm=0.42)
    assert tr.calm(stored) == 0.42


def test_public_has_no_arrays():
    pub = _track().public()
    assert pub["layout"] == "full" and pub["anchors_available"] == ["auto", "left", "right", "top"]
    assert {"id", "group", "box", "calm"} == set(pub["spots"][0])
    assert pub["samples"] == 8 and pub["bars"] is False


# ---------------------------------------------------------------------------
# build(): sampling, bars, the cache
# ---------------------------------------------------------------------------

def _video(path: Path, *, seconds: float = 3.0, bars: bool = False) -> Path:
    vw = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"mp4v"), FPS, (W // 4, H // 4))
    assert vw.isOpened()
    rng = np.random.default_rng(3)
    for i in range(int(seconds * FPS)):
        f = rng.integers(60, 200, (H // 4, W // 4, 3), dtype=np.uint8)
        if bars:
            f[:100] = 0
            f[220:] = 0
        vw.write(f)
    vw.release()
    return path


def _fake_matte(calls: list):
    def matte(bgr):
        calls.append(bgr.shape)
        m = np.zeros(bgr.shape[:2], np.float32)
        h, w = m.shape
        m[h // 3:, w // 3: 2 * w // 3] = 1.0
        return m
    return matte


def test_build_samples_twice_a_second_and_is_cached(tmp_path):
    clip = _video(tmp_path / "scene0.mp4", seconds=3.0)
    calls: list = []
    tr = T.build(clip, tmp_path / "icons", matte=_fake_matte(calls))
    assert len(calls) == 6 and len(tr.times) == 6                         # 90 frames, one each 15
    assert list(np.round(tr.times, 2)) == [0.0, 0.5, 1.0, 1.5, 2.0, 2.5]
    assert tr.masks.shape == (6, (H // 4) // 8, (W // 4) // 8)
    assert (tmp_path / "icons" / "track.npz").is_file() and (tmp_path / "icons" / "track_frame0.png").is_file()
    assert tr.layout == "full" and tr.bars is None
    again = T.build(clip, tmp_path / "icons", matte=_fake_matte(calls))
    assert len(calls) == 6                                                 # from the cache
    assert again.key == tr.key and ids(again.spots) == ids(tr.spots)
    assert np.array_equal(again.masks, tr.masks) and np.array_equal(again.heads, tr.heads)
    assert again.spots[0]["calm"] == tr.spots[0]["calm"]
    clip.write_bytes(clip.read_bytes() + b"\0")                           # a new take
    assert T.track_key(clip) != tr.key


def test_build_finds_the_letterbox_and_keeps_it_through_the_cache(tmp_path):
    clip = _video(tmp_path / "scene0.mp4", seconds=2.0, bars=True)
    tr = T.build(clip, tmp_path / "icons", matte=_fake_matte([]))
    assert tr.layout == "letterbox"
    assert abs(tr.picture[0] - 100) <= 8 and abs(tr.picture[1] - 220) <= 8   # mp4v blurs the edge
    assert ids(tr.spots)[:6] == ["TB-L", "TB-C", "TB-R", "BB-L", "BB-C", "BB-R"]
    again = T.load(tmp_path / "icons", tr.key)
    assert again is not None and np.array_equal(again.bars, tr.bars)
    assert T.load(tmp_path / "icons", "another key") is None


def test_build_uses_the_local_matte_and_head_box(tmp_path, monkeypatch):
    seen = []
    monkeypatch.setattr(speaker_front, "person_matte", _fake_matte(seen))
    real_head = pattern_planner.head_box
    heads = []

    def head_box(rgb, m):
        heads.append(m.shape)
        return real_head(rgb, m)

    monkeypatch.setattr(pattern_planner, "head_box", head_box)
    tr = T.build(_video(tmp_path / "scene0.mp4", seconds=1.0), tmp_path / "icons")
    assert len(seen) == 2 == len(heads) and (tr.heads[:, 0] >= 0).all()


# ---------------------------------------------------------------------------
# The real scenes (local MODNet; skipped unless cached here)
# ---------------------------------------------------------------------------

def _modnet_cached() -> bool:
    if speaker_front.MODNET_PATH:
        return Path(speaker_front.MODNET_PATH).is_file()
    try:
        from huggingface_hub import try_to_load_from_cache

        got = try_to_load_from_cache(speaker_front.MODNET_REPO, speaker_front.MODNET_FILE,
                                     revision=speaker_front.MODNET_REVISION)
        return isinstance(got, str)
    except Exception:  # noqa: BLE001
        return False


def _scene(rid: int):
    return next((STUDIO / str(rid)).glob("scene_0_*/base/scene0.mp4"), None)


@pytest.mark.skipif(not (_modnet_cached() and _scene(806) and _scene(815)),
                    reason="MODNet not cached here, or the studio scenes are not on disk")
def test_real_scenes(tmp_path):
    t815 = T.build(_scene(815), tmp_path / "815")
    dur = t815.duration
    assert t815.layout == "full"
    feasible = [s["id"] for s in t815.spots if t815.feasible(s["box"], 0, dur)]
    assert {"L1", "L2", "L3", "R1", "R2", "R3"} <= set(feasible)
    assert t815.overlap(t815.spot("L4")["box"], 0, dur) > T.OVERLAP_MAX       # the sleeve
    assert t815.overlap(t815.spot("R4")["box"], 0, dur) > T.OVERLAP_MAX       # the hand
    # Size L leaves the left column (the head reaches x 175) infeasible.
    assert not any(t815.feasible(t815.sized(t815.spot(i), 0.20), 0, dur) for i in ("L1", "L2", "L3"))
    t806 = T.build(_scene(806), tmp_path / "806")
    assert t806.layout == "letterbox" and t806.picture[:2] == (400, 880)
    x0, _y0, x1, _y1 = T._px(t806.spot("TB-C")["box"], t806.width, t806.height)
    assert abs((x1 - x0) - 158) <= 1
    assert all(t806.overlap(t806.spot(i)["box"], 0, t806.duration) == 0.0
               for i in ("TB-L", "TB-C", "TB-R", "BB-L", "BB-C", "BB-R"))
