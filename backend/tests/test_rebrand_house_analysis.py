"""The scene analysed once for the in-house engine: the speaker on every frame
(mattes.MatteBank) and the camera (camera.CameraTrack).

The owner's talking-head clips are a slow push-in (~12% over 8 s about the
frame's centre), so a pattern painted "in the room" must grow with it, and
the speaker must be cut out of the graphics on every frame by the same matte
in the studio and in a run. Both are worked out once per scene clip.

Synthetic clips are built with numpy through ffmpeg: a textured room zooming
about its centre with a flat green "speaker" standing still in it (so a
tracker that followed her instead of the room would be caught), a spliced
cut, a flat grey clip, a letterboxed one. MODNet is faked by that green,
except in the two tests that read run 815's and 806's real clips (skipped
when they, or the local model, are not here). Every paid call is refused."""
from __future__ import annotations

import asyncio
import os
import subprocess
from pathlib import Path

import numpy as np
import pytest

from app.services import omni_client as omni
from app.services import person_matte
from app.services import rebrand_media as media
from app.services import rebrand_pipeline as pipeline
from app.services import speaker_front
from app.services import vertex_client
from app.services.rebrand_harness import camera
from app.services.rebrand_harness import guards as gd
from app.services.rebrand_harness import mattes
from app.services.rebrand_harness import stages as st
from app.services.rebrand_harness import studio

cv2 = pytest.importorskip("cv2")

DATA = Path(__file__).resolve().parents[1] / "data" / "yas_producer"
ANIM_351 = DATA / "animations" / "anim-351.mp4"
S806 = DATA / "talking-head-v2" / "rebrand" / "_studio" / "806" / "scene_0_b85f33fb47" / "base" / "scene0.mp4"
GREEN = (40, 200, 60)                    # the "speaker", BGR


@pytest.fixture(autouse=True)
def _no_paid_calls(monkeypatch, tmp_path):
    async def refuse(*_a, **_k):
        raise AssertionError("a paid model call in a test")

    monkeypatch.setattr(vertex_client, "gemini_multimodal", refuse)
    monkeypatch.setattr(vertex_client, "gemini_image", refuse)
    monkeypatch.setattr(omni, "edit_video", refuse)
    monkeypatch.setattr(omni, "interpolate", refuse)
    monkeypatch.setattr(person_matte, "person_mask", refuse)
    monkeypatch.setattr(studio, "ANALYSIS_ROOT", tmp_path / "_cache" / "scene")
    monkeypatch.setattr(studio, "_ANALYSES", {})


# ---------------------------------------------------------------------------
# Synthetic clips
# ---------------------------------------------------------------------------

def _texture(seed: int, w: int, h: int, cell: int = 4) -> np.ndarray:
    """A room: coarse random colour in `cell`-px cells, smoothed (plenty of
    corners, and nothing in it the speaker's green)."""
    r = np.random.default_rng(seed)
    coarse = r.random((max(2, h // cell), max(2, w // cell), 3)).astype(np.float32) * 0.8
    t = cv2.GaussianBlur(cv2.resize(coarse, (w, h), interpolation=cv2.INTER_CUBIC), (0, 0), 1.5)
    t[:, :, 1] *= 0.6                    # never as green as the speaker
    return np.clip(t * 255, 0, 255).astype(np.uint8)


def _zoom(w: int, h: int, s: float, cx: float, cy: float) -> np.ndarray:
    """The similarity zooming by s about (cx, cy): frame-0 px -> this frame's."""
    return np.array([[s, 0.0, (1 - s) * cx], [0.0, s, (1 - s) * cy]], np.float64)


def _room_frame(tex: np.ndarray, m: np.ndarray, *, speaker: bool = True) -> np.ndarray:
    h, w = tex.shape[:2]
    f = cv2.warpAffine(tex, m, (w, h), flags=cv2.INTER_CUBIC, borderMode=cv2.BORDER_REFLECT)
    if speaker:
        cv2.ellipse(f, (w // 2, int(h * 0.62)), (w // 5, h // 4), 0, 0, 360, GREEN, -1)
    return f


def _encode(path: Path, frames, w: int, h: int, *, fps: int = 30, lossless: bool = False) -> Path:
    """The frames (an iterable of BGR arrays) as an H.264 clip, fed one by one."""
    path.parent.mkdir(parents=True, exist_ok=True)
    codec = (["-c:v", "libx264", "-qp", "0", "-pix_fmt", "yuv444p"] if lossless
             else ["-c:v", "libx264", "-preset", "ultrafast", "-crf", "12", "-pix_fmt", "yuv420p"])
    proc = subprocess.Popen([media.ffmpeg_exe(), "-y", "-loglevel", "error", "-f", "rawvideo", "-pix_fmt", "bgr24",
                             "-s", f"{w}x{h}", "-r", str(fps), "-i", "-", *codec, str(path)],
                            stdin=subprocess.PIPE)
    for f in frames:
        proc.stdin.write(np.ascontiguousarray(f).tobytes())
    proc.stdin.close()
    assert proc.wait(timeout=120) == 0
    return path


def _fake_raw(bgr) -> np.ndarray:
    """MODNet, faked: the speaker is the green, at half size (as MODNet's own
    output is smaller than the frame)."""
    d = np.abs(bgr.astype(np.int16) - np.array(GREEN, np.int16)).max(axis=2)
    g = (d < 40).astype(np.float32)
    return cv2.resize(g, (max(1, bgr.shape[1] // 2), max(1, bgr.shape[0] // 2)), interpolation=cv2.INTER_AREA)


def _zoom_clip(path: Path, *, w: int = 720, h: int = 1280, n: int = 40, zoom: float = 0.10,
               seed: int = 1, cell: int = 4) -> tuple[Path, list[np.ndarray]]:
    """A room zooming 1.00 -> 1 + zoom about the frame's centre over n
    frames, the speaker standing still in front of it; and the true matrix
    of each frame."""
    tex = _texture(seed, w, h, cell)
    cx, cy = (w - 1) / 2, (h - 1) / 2
    truth = [_zoom(w, h, 1.0 + zoom * i / (n - 1), cx, cy) for i in range(n)]
    return _encode(path, (_room_frame(tex, m) for m in truth), w, h), truth


def _centre_err(m, want, w: int, h: int) -> float:
    c = np.array([(w - 1) / 2, (h - 1) / 2])
    m, want = np.asarray(m, np.float64), np.asarray(want, np.float64)
    return float(np.hypot(*((m[:, :2] @ c + m[:, 2]) - (want[:, :2] @ c + want[:, 2]))))


def _corner_err(m, want) -> float:
    """How far frame 0's top-left corner lands from where it should (px)."""
    m, want = np.asarray(m, np.float64), np.asarray(want, np.float64)
    return float(np.hypot(*(m[:, 2] - want[:, 2])))


# ---------------------------------------------------------------------------
# matte_keys: cut-aware, and unchanged without cuts
# ---------------------------------------------------------------------------

def _old_matte_keys(i: int, n: int, every: int = 3) -> tuple[int, int]:
    """speaker_front.matte_keys as it was before cuts (the golden's rule)."""
    every = max(1, int(every))
    k = (int(i) // every) * every
    if k + every <= n - 1:
        return k, k + every
    last = n - 1
    return (k, last) if last > k else (k, k)


def test_matte_keys_without_cuts_is_exactly_the_old_rule():
    for n in range(1, 41):
        for every in range(1, 6):
            for i in range(n):
                old = _old_matte_keys(i, n, every)
                assert speaker_front.matte_keys(i, n, every) == old
                assert speaker_front.matte_keys(i, n, every, ()) == old
                assert speaker_front.matte_keys(i, n, every, []) == old
                # cuts outside the clip are no cuts
                assert speaker_front.matte_keys(i, n, every, [0, n, n + 5]) == old


def test_matte_keys_never_straddle_a_cut():
    rng = np.random.default_rng(3)
    for _ in range(300):
        n = int(rng.integers(2, 61))
        every = int(rng.integers(1, 6))
        cuts = sorted({int(c) for c in rng.integers(1, n, size=int(rng.integers(1, 5)))}) if n > 1 else []
        bounds = [0, *cuts, n]
        for i in range(n):
            ka, kb = speaker_front.matte_keys(i, n, every, cuts)
            start = max(b for b in bounds[:-1] if b <= i)
            end = min(b for b in bounds[1:] if b > i)
            assert start <= ka <= i <= kb < end, (n, every, cuts, i, ka, kb)
            # the grid keyframe at or before i, pulled into the shot; the next
            # one (or the shot's last frame) after it
            k = (i // every) * every
            assert ka == max(k, start)
            assert kb == max(i, min(k + every, end - 1))
            for c in cuts:
                assert not (ka < c <= kb), "a union across a cut"


# ---------------------------------------------------------------------------
# restore_speaker / frame_at: the cuts reach the pass, and only when there are any
# ---------------------------------------------------------------------------

def _level_clip(path: Path, n: int, w: int = 32, h: int = 32) -> Path:
    """Frame i is a flat grey 10 + 10 i (lossless), so the matte faked from
    it can say which frame it was made from."""
    return _encode(path, (np.full((h, w, 3), 10 + 10 * i, np.uint8) for i in range(n)), w, h, lossless=True)


def _which(fo) -> int:
    return int(round((float(np.asarray(fo).mean()) - 10) / 10))


def _keyed_matte(calls: list):
    def speaker_matte(fo, *, clear_bars=False):
        k = _which(fo)
        calls.append(k)
        m = np.zeros(fo.shape[:2], np.float32)
        m[:, k] = 1.0
        return m

    return speaker_matte


@pytest.mark.parametrize("cuts", [(), (7, 8, 13), (9,), (1, 19)])
def test_the_pass_unions_each_frame_within_its_own_shot(tmp_path, monkeypatch, cuts):
    """restore_speaker streams the clip once: every frame's matte is the
    union matte_keys names (with cuts: never across one), each keyframe
    matted once. With no cuts it is the pass as it always was."""
    n = 20
    clip = _level_clip(tmp_path / "levels.mp4", n)
    calls: list[int] = []
    seen: list[tuple] = []
    monkeypatch.setattr(speaker_front, "speaker_matte", _keyed_matte(calls))

    def finish(fe, fo, m, stamps, t):
        seen.append(tuple(int(k) for k in np.flatnonzero(m[0] > 0.5)))
        return fe, False

    monkeypatch.setattr(speaker_front, "_finish", finish)
    kw = {"cuts": list(cuts)} if cuts else {}
    speaker_front.restore_speaker(clip, clip, tmp_path / "out.mp4", **kw)
    want = [tuple(sorted(set(speaker_front.matte_keys(i, n, 3, cuts)))) for i in range(n)]
    assert seen == want
    assert sorted(calls) == sorted({k for pair in want for k in pair}), "each keyframe matted once"
    if not cuts:
        assert want == [tuple(sorted(set(_old_matte_keys(i, n, 3)))) for i in range(n)]


def test_frame_at_takes_the_same_union(tmp_path, monkeypatch):
    n = 20
    clip = _level_clip(tmp_path / "levels.mp4", n)
    monkeypatch.setattr(speaker_front, "speaker_matte", _keyed_matte([]))
    got: list = []

    def compose(fe, fo, m, **kw):
        got.append(tuple(int(k) for k in np.flatnonzero(m[0] > 0.5)))
        return fe

    monkeypatch.setattr(speaker_front, "compose_frame", compose)
    for i in (5, 6, 7, 12, 13, 19):
        speaker_front.frame_at(clip, clip, i / 30.0, cuts=[7, 8, 13])
        assert got[-1] == tuple(sorted(set(speaker_front.matte_keys(i, n, 3, [7, 8, 13])))), i
    speaker_front.frame_at(clip, clip, 6 / 30.0)
    assert got[-1] == (6, 9), "no cuts: the union as before"


def test_the_guards_hand_the_pass_its_cuts_only_when_there_are_any(tmp_path, monkeypatch):
    seen: list = []
    monkeypatch.setattr(speaker_front, "restore_speaker",
                        lambda e, o, out, **kw: seen.append(sorted(kw)) or out)
    g = gd.ChunkGuards(speaker_front=True, clear_veil_sec=0.0, clear_bars=False)
    gd.apply(tmp_path / "e.mp4", tmp_path / "o.mp4", tmp_path / "out.mp4", g)
    gd.apply(tmp_path / "e.mp4", tmp_path / "o.mp4", tmp_path / "out.mp4", g, cuts=[])
    gd.apply(tmp_path / "e.mp4", tmp_path / "o.mp4", tmp_path / "out.mp4", g, cuts=[53])
    assert seen[0] == seen[1] == ["clear_bars", "clear_veil_sec"], "the golden's call, unchanged"
    assert seen[2] == ["clear_bars", "clear_veil_sec", "cuts"]


def test_a_clip_s_cuts_come_from_its_description_or_are_found(tmp_path):
    tex_a, tex_b = _texture(1, 96, 160), _texture(2, 96, 160)
    frames = [_room_frame(tex_a, np.eye(2, 3)) for _ in range(12)] + \
             [_room_frame(tex_b, np.eye(2, 3)) for _ in range(12)]
    clip = _encode(tmp_path / "cut.mp4", frames, 96, 160)
    assert media.cuts_of(clip) == [12]
    media.scene_info_path(clip).write_text('{"source_fps": 30, "source_cut": null, "trimmed_sec": 0, "cuts": [5]}',
                                           encoding="utf-8")
    assert media.cuts_of(clip) == [5], "normalize_scene's description wins"
    assert media.cuts_of(tmp_path / "missing.mp4") == []


# ---------------------------------------------------------------------------
# The MODNet session
# ---------------------------------------------------------------------------

def test_modnet_runs_on_up_to_8_threads_without_spinning(monkeypatch):
    ort = pytest.importorskip("onnxruntime")
    made: list = []

    class FakeSession:
        def __init__(self, path, so, providers=None):
            made.append((path, so, providers))

    monkeypatch.setattr(ort, "InferenceSession", FakeSession)
    monkeypatch.setattr(speaker_front, "_model_path", lambda: "modnet.onnx")
    monkeypatch.setattr(speaker_front, "_session", None)
    monkeypatch.delenv("MODNET_THREADS", raising=False)
    speaker_front._get_session()
    speaker_front._get_session()                    # made once
    assert len(made) == 1
    path, so, providers = made[0]
    assert path == "modnet.onnx" and providers == ["CPUExecutionProvider"]
    assert so.intra_op_num_threads == min(8, os.cpu_count() or 4)
    assert so.get_session_config_entry("session.intra_op.allow_spinning") == "0"
    monkeypatch.setattr(speaker_front, "_session", None)
    monkeypatch.setenv("MODNET_THREADS", "3")
    speaker_front._get_session()
    assert made[-1][1].intra_op_num_threads == 3
    monkeypatch.setenv("MODNET_THREADS", "lots")
    assert speaker_front.modnet_threads() == min(8, os.cpu_count() or 4)


def test_the_matte_is_modnets_output_scaled_to_the_frame(monkeypatch):
    """matte() is raw_matte() at the frame's size, clipped — as it always was."""
    out = np.linspace(-0.2, 1.2, 8 * 14, dtype=np.float32).reshape(1, 1, 14, 8)

    class Sess:
        def get_inputs(self):
            return [type("I", (), {"name": "input"})()]

        def run(self, _names, feed):
            assert feed["input"].shape == (1, 3, 448, 256)
            return [out]

    monkeypatch.setattr(speaker_front, "_session", Sess())
    frame = np.zeros((1280, 720, 3), np.uint8)
    raw = speaker_front.raw_matte(frame)
    assert raw.shape == (14, 8)
    want = np.clip(cv2.resize(out[0, 0], (720, 1280), interpolation=cv2.INTER_LINEAR), 0.0, 1.0)
    assert np.array_equal(speaker_front.matte(frame), want)


# ---------------------------------------------------------------------------
# The matte bank
# ---------------------------------------------------------------------------

def _speaker_clip(path: Path, *, n: int = 24, cut: int = 10, w: int = 96, h: int = 160,
                  bars: int = 0) -> Path:
    """The speaker walking right a pixel a frame, then — from `cut` — another
    shot where she stands elsewhere; black bars `bars` px deep top and bottom."""
    tex_a, tex_b = _texture(4, w, h - 2 * bars), _texture(5, w, h - 2 * bars)
    frames = []
    for i in range(n):
        pic = (tex_a if i < cut else tex_b).copy()
        x = (w // 3 + i) if i < cut else (2 * w // 3)
        cv2.ellipse(pic, (x, pic.shape[0] // 2), (w // 6, pic.shape[0] // 4), 0, 0, 360, GREEN, -1)
        f = np.zeros((h, w, 3), np.uint8)
        f[bars:h - bars] = pic
        frames.append(f)
    return _encode(path, frames, w, h)


def _frames(clip: Path) -> list[np.ndarray]:
    cap = cv2.VideoCapture(str(clip))
    out = []
    while True:
        ok, f = cap.read()
        if not ok:
            break
        out.append(f)
    cap.release()
    return out


def _counted(calls: list):
    def raw(bgr):
        calls.append(1)
        return _fake_raw(bgr)

    return raw


def test_the_bank_is_the_union_of_its_cleaned_keyframes_within_a_shot(tmp_path):
    clip = _speaker_clip(tmp_path / "scene0.mp4")
    calls: list = []
    bank = mattes.MatteBank.open(clip, tmp_path / "cache", raw=_counted(calls))
    assert (bank.n, bank.width, bank.height, bank.cuts) == (24, 96, 160, [10])
    assert bank.missing() == bank.keyframes() and bank.progress() == (0, len(bank.keyframes()))
    ticks: list = []
    bank.fill(progress=lambda done, total: ticks.append((done, total)))
    # every 3rd frame, plus each shot's last and first frame and the clip's last
    assert bank.keyframes() == [0, 3, 6, 9, 10, 12, 15, 18, 21, 23]
    assert len(calls) == len(bank.keyframes()) and bank.missing() == []
    assert ticks[-1] == (10, 10)
    frames = _frames(clip)
    for k in bank.keyframes():
        want = np.clip(np.rint(np.clip(_fake_raw(frames[k]), 0, 1) * 255), 0, 255).astype(np.uint8)
        assert np.array_equal(bank.raw[k], want), f"keyframe {k} is MODNet's own output"

    def cleaned(k):
        up = cv2.resize(bank.raw[k].astype(np.float32) / 255.0, (96, 160), interpolation=cv2.INTER_LINEAR)
        return speaker_front.clean(np.clip(up, 0, 1))

    for i in range(bank.n):
        ka, kb = speaker_front.matte_keys(i, 24, 3, [10])
        got = bank.at(i)
        assert got.dtype == np.float32 and got.shape == (160, 96)
        assert np.array_equal(got, np.maximum(cleaned(ka), cleaned(kb))), i
    # Frame 9, the old shot's last: never cut by where she stands after the cut.
    after = bank.at(10) > 0.5
    before = bank.at(9) > 0.5
    assert (after & ~before).sum() > 100 and np.array_equal(bank.keys_of(9), (9, 9))


def test_the_bank_is_kept_and_read_back_without_matting_again(tmp_path):
    clip = _speaker_clip(tmp_path / "scene0.mp4")
    calls: list = []
    bank = mattes.MatteBank.open(clip, tmp_path / "cache", raw=_counted(calls)).fill()
    files = list((tmp_path / "cache").glob("mattes_*.npz"))
    assert [f.name for f in files] == [f"mattes_{bank.key}.npz"]
    assert not list((tmp_path / "cache").glob("*.tmp*")), "written whole"
    again = mattes.MatteBank.open(clip, tmp_path / "cache", raw=_counted(calls))
    assert len(calls) == 10 and again.missing() == [] and again.filled
    assert sorted(again.raw) == sorted(bank.raw)
    assert all(np.array_equal(again.raw[k], bank.raw[k]) for k in bank.raw)
    assert np.array_equal(again.at(17), bank.at(17))
    assert again.key == mattes.bank_key(clip, 3)
    assert len(calls) == 10, "read back, never matted again"


def test_a_missing_keyframe_is_made_when_it_is_asked_for(tmp_path):
    clip = _speaker_clip(tmp_path / "scene0.mp4")
    filled = mattes.MatteBank.open(clip, tmp_path / "a", raw=_fake_raw).fill()
    calls: list = []
    lazy = mattes.MatteBank.open(clip, tmp_path / "b", raw=_counted(calls))
    got = lazy.at(16)
    assert len(calls) == 2 and sorted(lazy.raw) == [15, 18], "only frame 16's two keyframes"
    assert np.array_equal(got, filled.at(16))
    assert (tmp_path / "b" / f"mattes_{lazy.key}.npz").is_file(), "kept for the next time"


def test_the_bank_says_where_the_bars_are_and_the_matte_is_empty_there(tmp_path):
    clip = _speaker_clip(tmp_path / "scene0.mp4", bars=30, n=12, cut=99)
    bank = mattes.MatteBank.open(clip, tmp_path / "cache", raw=lambda f: np.ones(f.shape[:2], np.float32)).fill()
    assert bank.bars is not None
    assert bank.bars[:30].all() and bank.bars[-30:].all() and not bank.bars[40:120].any()
    m = bank.at(5, bars=bank.bars)
    assert m[bank.bars].max() == 0.0 and m[~bank.bars].min() > 0.99
    assert bank.at(5)[:30].min() > 0.99, "only where asked"
    again = mattes.MatteBank.open(clip, tmp_path / "cache")
    assert again.bars_read and np.array_equal(again.bars, bank.bars)


# ---------------------------------------------------------------------------
# The camera
# ---------------------------------------------------------------------------

def test_the_camera_follows_a_push_in_within_a_pixel(tmp_path):
    clip, truth = _zoom_clip(tmp_path / "zoom.mp4")
    bank = mattes.MatteBank.open(clip, tmp_path / "cache", raw=_fake_raw).fill()
    track = camera.build(clip, tmp_path / "cache", bank=bank)
    assert track.method == "features" and track.shots == [(0, 40)] and track.held == []
    assert track.warnings == []
    assert np.array_equal(track.at(0), np.eye(2, 3, dtype=np.float32)), "frame 0 is itself, exactly"
    for i in (10, 20, 39):
        m, want = track.at(i), truth[i]
        assert abs(track.scale_at(i) - want[0, 0]) <= 0.003, (i, track.scale_at(i))
        assert abs(float(m[1, 0])) <= 0.003, "no turn"
        assert _centre_err(m, want, 720, 1280) <= 1.0
        assert _corner_err(m, want) <= 1.0, (i, m, want)
    body = track.public()
    assert body["n"] == 40 and len(body["m"]) == 40 and body["m"][0] == [1.0, 0.0, 0.0, 0.0]
    assert body["zoom_max"] == pytest.approx(1.10, abs=0.003) and body["shift_max_px"] <= 1.0
    assert body["cuts_sec"] == [] and body["held_from_sec"] is None and body["method"] == "features"
    a, b, tx, ty = body["m"][-1]
    assert abs(tx - truth[-1][0, 2]) <= 1.0 and abs(ty - truth[-1][1, 2]) <= 1.0


def test_the_camera_is_kept_and_read_back(tmp_path, monkeypatch):
    clip, _truth = _zoom_clip(tmp_path / "zoom.mp4", w=360, h=640, n=20)
    bank = mattes.MatteBank.open(clip, tmp_path / "cache", raw=_fake_raw).fill()
    first = camera.build(clip, tmp_path / "cache", bank=bank)
    assert camera.path_for(tmp_path / "cache", first.key).is_file()

    def no(*_a, **_k):
        raise AssertionError("followed again")

    monkeypatch.setattr(camera._Room, "mask", no)
    again = camera.build(clip, tmp_path / "cache", bank=mattes.MatteBank.open(clip, tmp_path / "cache"))
    assert again.key == first.key and np.array_equal(again.mats, first.mats)
    assert again.public() == first.public()


def test_a_cut_starts_the_next_shot_where_the_last_left_the_room(tmp_path):
    """Each shot is followed on its own and chained: across the cut the
    pattern keeps its place on screen, then moves with the new room."""
    w, h, half = 720, 1280, 20
    cx, cy = (w - 1) / 2, (h - 1) / 2
    # The other shot: another room, lit brighter (a cut the plain frame
    # difference finds, as media.shot_cuts finds Veo's).
    tex_a, tex_b = _texture(1, w, h), np.clip(_texture(9, w, h).astype(np.int16) + 70, 0, 255).astype(np.uint8)
    zooms = [_zoom(w, h, 1.0 + 0.05 * i / (half - 1), cx, cy) for i in range(half)]
    frames = [_room_frame(tex_a, m) for m in zooms] + [_room_frame(tex_b, m) for m in zooms]
    clip = _encode(tmp_path / "cut.mp4", frames, w, h)
    bank = mattes.MatteBank.open(clip, tmp_path / "cache", raw=_fake_raw).fill()
    assert bank.cuts == [half]
    track = camera.build(clip, tmp_path / "cache", bank=bank)
    assert track.shots == [(0, half), (half, 2 * half)] and track.cuts == [half]
    assert track.held == [] and track.held_from is None and track.warnings == []
    assert np.allclose(track.at(half), track.at(half - 1), atol=1e-4), "no jump at the cut"
    assert abs(track.scale_at(half - 1) - 1.05) <= 0.003
    both = _zoom(w, h, 1.05 * 1.05, cx, cy)
    assert abs(track.scale_at(2 * half - 1) - 1.1025) <= 0.004
    assert _centre_err(track.at(2 * half - 1), both, w, h) <= 1.0
    body = track.public()
    assert body["cuts_sec"] == [round(half / 30, 3)] and body["held_from_sec"] is None


def test_a_flat_clip_holds_still_and_says_so(tmp_path):
    clip = _encode(tmp_path / "flat.mp4", (np.full((640, 360, 3), 128, np.uint8) for _ in range(15)), 360, 640)
    bank = mattes.MatteBank.open(clip, tmp_path / "cache", raw=_fake_raw).fill()
    track = camera.build(clip, tmp_path / "cache", bank=bank)
    assert track.method == "still" and track.warnings == [camera.STILL_WARNING]
    assert all(np.array_equal(track.at(i), np.eye(2, 3, dtype=np.float32)) for i in range(15))
    assert track.held == [(1, 15)] and track.public()["held_from_sec"] == pytest.approx(1 / 30, abs=1e-3)


def test_the_letterbox_bars_are_left_out_of_the_room(tmp_path):
    """A 16:9 picture in a 9:16 frame (as run 806): the picture zooms inside
    its bars, which stay put — the camera follows the picture, not the bars'
    edges."""
    w, h, pic_h, n = 720, 1280, 405, 36
    y0 = (h - pic_h) // 2
    tex = _texture(3, w, pic_h)
    pcx, pcy = (w - 1) / 2, (pic_h - 1) / 2

    def frame(s):
        f = np.zeros((h, w, 3), np.uint8)
        f[y0:y0 + pic_h] = _room_frame(tex, _zoom(w, pic_h, s, pcx, pcy))
        return f

    scales = [1.0 + 0.10 * i / (n - 1) for i in range(n)]
    clip = _encode(tmp_path / "lb.mp4", (frame(s) for s in scales), w, h)
    bank = mattes.MatteBank.open(clip, tmp_path / "cache", raw=_fake_raw).fill()
    assert bank.bars is not None and bank.bars[:y0 - 2].all() and bank.bars[y0 + pic_h + 2:].all()
    assert not bank.bars[y0 + 10:y0 + pic_h - 10].any()
    track = camera.build(clip, tmp_path / "cache", bank=bank)
    want = _zoom(w, h, scales[-1], pcx, pcy + y0)
    assert track.method == "features"
    assert abs(track.scale_at(n - 1) - scales[-1]) <= 0.003, track.scale_at(n - 1)
    assert _centre_err(track.at(n - 1), want, w, h) <= 1.0
    # The bars' edges alone would say "no zoom": without the bars left out
    # the estimate would be pulled towards 1.
    assert track.public()["zoom_max"] == pytest.approx(1.10, abs=0.003)


def test_a_room_with_too_few_corners_is_followed_by_ecc(tmp_path, monkeypatch):
    """A plain room (big soft shapes, as a wall): ECC at a quarter size."""
    clip, truth = _zoom_clip(tmp_path / "zoom.mp4", n=30, cell=48)
    bank = mattes.MatteBank.open(clip, tmp_path / "cache", raw=_fake_raw).fill()
    monkeypatch.setattr(camera, "MIN_CORNERS", 10 ** 6)
    track = camera.build(clip, tmp_path / "cache", bank=bank)
    assert track.method == "ecc" and track.held == [] and track.warnings == []
    assert abs(track.scale_at(29) - truth[29][0, 0]) <= 0.005
    assert _centre_err(track.at(29), truth[29], 720, 1280) <= 1.5
    assert min(track.quality[1:]) >= camera.ECC_MIN_CC


def test_a_lost_camera_holds_from_there_and_says_when(tmp_path):
    """A jump the cut detector did not call a cut (here: the room pans 60 px
    in one frame, half way): the camera is held from there, and a hold over
    half a second is said."""
    w, h, n = 360, 640, 36
    tex = _texture(6, w, h)
    ms = [np.eye(2, 3) if i < 12 else np.array([[1.0, 0.0, 60.0], [0.0, 1.0, 0.0]]) for i in range(n)]
    clip = _encode(tmp_path / "jump.mp4", (_room_frame(tex, m) for m in ms), w, h)
    bank = mattes.MatteBank.open(clip, tmp_path / "cache", raw=_fake_raw, cuts=[]).fill()
    track = camera.build(clip, tmp_path / "cache", bank=bank)
    assert track.held == [(12, n)] and track.held_from == 12
    assert np.allclose(track.at(n - 1), track.at(11), atol=1e-6)
    assert track.warnings == [camera.held_warning(12 / 30)]
    assert track.warnings[0] == ("The camera can't be followed from 0:00.4, so the patterns hold still "
                                 "on screen from there.")


# ---------------------------------------------------------------------------
# The studio: analysed once, after the masks; its state; GET /house/camera
# ---------------------------------------------------------------------------

def _scene(tmp_path: Path, clip: Path) -> studio.Scene:
    work = tmp_path / "_studio" / "1" / "scene_0_abc"
    work.mkdir(parents=True, exist_ok=True)
    seg = pipeline.Segment(kind="scene", index=0, source=clip, is_still=False)
    seg.base = clip
    ctx = pipeline.Ctx(exp_id=0, run_id=1, aspect="9:16", width=360, height=640, work=work, options={},
                       run_ctx={}, intro=None, scenes=[seg], outro=None)
    ctx.target_seg = seg
    return studio.Scene(run_id=1, target="scene_0", aspect="9:16", width=360, height=640, work=work, ctx=ctx,
                        chunks=[clip], firsts=[], seconds=[1.0])


def test_the_studio_analyses_a_scene_once_and_says_how_far_it_is(tmp_path, monkeypatch):
    clip, _truth = _zoom_clip(tmp_path / "base" / "scene0.mp4", w=360, h=640, n=30)
    monkeypatch.setattr(speaker_front, "raw_matte", _fake_raw)
    scene = _scene(tmp_path, clip)
    before = studio.warmup_state(scene)
    assert before["speaker_frames"] == {"done": 0, "total": 0} and before["camera"] == "waiting"

    async def run():
        task = studio._analyse_scene(scene)
        assert studio._analyse_scene(scene) is task, "one analysis per scene clip"
        await task

    asyncio.run(run())
    state = studio.warmup_state(scene)
    assert state["camera"] == "ready"
    assert state["speaker_frames"]["done"] == state["speaker_frames"]["total"] == 11
    folder = studio.analysis_dir(clip)
    assert folder.parent == studio.ANALYSIS_ROOT and folder.name == st.digest(clip)[:16]
    assert len(list(folder.glob("mattes_*.npz"))) == 1 and len(list(folder.glob("camera_*.npz"))) == 1

    async def scene_of(run_id, target):
        return scene

    monkeypatch.setattr(studio, "_scene", scene_of)
    body = asyncio.run(studio.house_camera(1, "scene_0"))
    assert body["state"] == "ready" and body["n"] == 30 and body["method"] == "features"
    assert body["zoom_max"] == pytest.approx(1.10, abs=0.003)

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.routers import rebrand_harness as rt

    app = FastAPI()
    app.include_router(rt.router)
    got = TestClient(app).get("/api/rebrand/harness/house/camera", params={"run_id": 1, "target": "scene_0"})
    assert got.status_code == 200 and got.json() == body


def test_a_failed_analysis_is_said_and_tried_again_on_the_next_open(tmp_path, monkeypatch):
    scene = _scene(tmp_path, tmp_path / "base" / "gone.mp4")

    async def scene_of(run_id, target):
        return scene

    monkeypatch.setattr(studio, "_scene", scene_of)

    async def run():
        await studio._analyse_scene(scene)
        failed = studio.warmup_state(scene)
        body = await studio.house_camera(1, "scene_0")
        first = studio._ANALYSES[str(scene.ctx.target_seg.base)].task
        assert studio._analyse_scene(scene, retry=False) is first, "not on every ask"
        again = studio._analyse_scene(scene)
        assert again is not first, "a reopen tries it again"
        await again
        return failed, body

    failed, body = asyncio.run(run())
    assert failed["camera"] == "failed"
    assert body["state"] == "failed" and body["error"]


def test_the_analysis_starts_after_the_masks(tmp_path, monkeypatch):
    scene = _scene(tmp_path, tmp_path / "base" / "scene0.mp4")
    order: list = []

    async def plan_frame(sc):
        order.append("plan")

    async def mask(frame):
        order.append("mask")

    monkeypatch.setattr(studio, "_plan_frame", plan_frame)
    monkeypatch.setattr(studio, "_mask", mask)
    monkeypatch.setattr(studio, "_mask_frames", lambda sc: [Path("c0_first.png")])
    monkeypatch.setattr(studio, "_analyse_scene", lambda sc, **kw: order.append("analyse"))
    asyncio.run(studio._warm_masks(scene))
    assert order == ["plan", "mask", "analyse"]


def test_the_analysis_can_never_reach_a_paid_model():
    """The grep gate (the plan's rule for the in-house modules): no paid
    image or text model, and never person_mask (its Gemini fallback)."""
    for mod in (mattes, camera):
        src = Path(mod.__file__).read_text(encoding="utf-8")
        for paid in ("gemini_image(", "gemini_multimodal(", "person_mask(", "omni_client", "vertex_client"):
            assert paid not in src, f"{Path(mod.__file__).name} mentions {paid}"


# ---------------------------------------------------------------------------
# The owner's clips (free: local MODNet; skipped when not on this machine)
# ---------------------------------------------------------------------------

def _modnet_or_skip() -> None:
    try:
        speaker_front._get_session()
    except Exception as e:  # noqa: BLE001 — no local MODNet here
        pytest.skip(f"MODNet is not available: {e}")


@pytest.mark.skipif(not ANIM_351.is_file(), reason="anim-351.mp4 is not on this machine")
def test_run_815s_push_in_is_about_12_percent(tmp_path):
    _modnet_or_skip()
    clip = media.normalize_scene(ANIM_351, tmp_path / "base" / "scene0.mp4", width=720, height=1280)
    bank = mattes.MatteBank.open(clip, tmp_path / "cache").fill()
    assert bank.cuts == [] and bank.n == 234, "the tail flash is trimmed: one shot"
    track = camera.build(clip, tmp_path / "cache", bank=bank)
    body = track.public()
    assert track.method == "features" and track.held == [] and body["warnings"] == []
    assert 1.10 <= body["zoom_max"] <= 1.14, body["zoom_max"]
    assert abs(track.scale_at(120) - 1.095) <= 0.01, "the lab's 1.095 at 4 s"
    assert body["shift_max_px"] <= 3.0, "a zoom about the centre, not a pan"


@pytest.mark.skipif(not S806.is_file(), reason="run 806's studio clip is not on this machine")
def test_run_806s_letterboxed_push_in(tmp_path):
    _modnet_or_skip()
    bank = mattes.MatteBank.open(S806, tmp_path / "cache").fill()
    assert bank.bars is not None, "806 is letterboxed"
    track = camera.build(S806, tmp_path / "cache", bank=bank)
    assert track.method == "features" and track.held == []
    assert abs(track.scale_at(120) - 1.062) <= 0.01, "the lab's 1.062 at 4 s"
    assert 1.10 <= track.public()["zoom_max"] <= 1.15
