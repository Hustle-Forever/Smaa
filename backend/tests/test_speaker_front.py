"""The speaker back IN FRONT of Omni's pattern.

Omni drew the lattice straight across the speaker (experiment 17) however it
was told not to; restore_speaker composites the original speaker over Omni's
frames with a per-frame matte. These tests use synthetic clips: a green room,
a red "speaker" block that walks right, and Omni's output = the same clip
with blue "pattern" stripes over EVERYTHING, speaker included."""
from __future__ import annotations

import subprocess
from pathlib import Path

import numpy as np
import pytest

cv2 = pytest.importorskip("cv2")

from app.services import speaker_front as sf
from app.services.rebrand_media import ffmpeg_exe

W, H, N, FPS = 160, 288, 20, 30
GREEN, RED, BLUE = (40, 160, 40), (40, 40, 220), (220, 60, 30)   # BGR


def _speaker_box(i):
    x0 = 30 + 3 * i                           # walks right 3 px a frame
    return x0, 90, x0 + 50, 260


def _frame(i, *, pattern: bool):
    f = np.zeros((H, W, 3), np.uint8)
    f[:] = GREEN
    x0, y0, x1, y1 = _speaker_box(i)
    f[y0:y1, x0:x1] = RED
    if pattern:
        f[:, ::8] = BLUE                      # Omni's lines, over everything
        f[:, 1::8] = BLUE
    return f


def _write(path: Path, *, pattern: bool) -> Path:
    cmd = [ffmpeg_exe(), "-y", "-loglevel", "error", "-f", "rawvideo", "-pix_fmt", "bgr24",
           "-s", f"{W}x{H}", "-r", str(FPS), "-i", "-", "-c:v", "libx264", "-crf", "0",
           "-preset", "ultrafast", "-pix_fmt", "yuv444p", str(path)]
    data = b"".join(_frame(i, pattern=pattern).tobytes() for i in range(N))
    subprocess.run(cmd, input=data, check=True)
    return path


def _red_matte(bgr):
    """Stand-in for MODNet: the speaker is whatever is red."""
    b, g, r = (bgr[:, :, k].astype(int) for k in range(3))
    return ((r > 150) & (g < 110) & (b < 110)).astype(np.float32)


def _read_all(path: Path):
    cap = cv2.VideoCapture(str(path))
    frames = []
    while True:
        ok, f = cap.read()
        if not ok:
            break
        frames.append(f)
    cap.release()
    return frames


@pytest.fixture
def clips(tmp_path, monkeypatch):
    monkeypatch.setattr(sf, "person_matte", _red_matte)
    orig = _write(tmp_path / "orig.mp4", pattern=False)
    edited = _write(tmp_path / "omni.mp4", pattern=True)
    return orig, edited, tmp_path


def test_the_speaker_is_back_in_front_in_every_frame(clips):
    orig, edited, tmp = clips
    out = sf.restore_speaker(edited, orig, tmp / "front.mp4", every=3)
    frames = _read_all(out)
    assert len(frames) == N
    for i, f in enumerate(frames):
        x0, y0, x1, y1 = _speaker_box(i)
        body = f[y0 + 2:y1 - 2, x0 + 2:x1 - 2].astype(int)
        blue = (body[:, :, 0] > 150) & (body[:, :, 2] < 110)
        assert not blue.any(), f"frame {i}: a pattern line crosses the speaker"
        assert (np.abs(body - np.array(RED)).sum(axis=2) < 60).mean() > 0.98, f"frame {i}"


def test_the_pattern_stays_on_the_background(clips):
    orig, edited, tmp = clips
    frames = _read_all(sf.restore_speaker(edited, orig, tmp / "front.mp4", every=3))
    for f in frames:
        room = f[10:80, :].astype(int)            # above the speaker's head: pure background
        blue = (room[:, :, 0] > 150) & (room[:, :, 2] < 110)
        assert blue[:, ::8].all(), "Omni's lines on the room are kept"


def test_a_failed_matte_raises_so_the_caller_keeps_omnis_clip(clips, monkeypatch):
    orig, edited, tmp = clips

    def broken(_):
        raise RuntimeError("no model")

    monkeypatch.setattr(sf, "person_matte", broken)
    with pytest.raises(RuntimeError):
        sf.restore_speaker(edited, orig, tmp / "front.mp4")


def test_clean_fills_holes_inside_the_speaker():
    """MODNet reads the darkest folds of a black abaya as background."""
    a = np.zeros((200, 100), np.float32)
    a[40:190, 20:80] = 1.0
    a[120:150, 40:60] = 0.0                       # a hole in the robe
    c = sf.clean(a)
    assert c[130:140, 45:55].min() > 0.99, "the hole is filled"
    assert c[5:15, :].max() < 0.01, "the room is not"
    assert c[100, 19] > 0.3, "grown a little past the edge (1 px at this size, ~3 px at 720)"


def test_the_real_modnet_finds_a_speaker_when_it_is_cached():
    hub = pytest.importorskip("huggingface_hub")
    pytest.importorskip("onnxruntime")
    cached = hub.try_to_load_from_cache(sf.MODNET_REPO, sf.MODNET_FILE, revision=sf.MODNET_REVISION)
    frame = Path("data/yas_producer/talking-head-v2/rebrand/15/omni/scene_1_c0_first.png")
    if not isinstance(cached, str) or not frame.exists():
        pytest.skip("MODNet not in the local HF cache, or no real frame on this machine")
    m = sf.person_matte(cv2.imread(str(frame)))
    share = float((m > 0.5).mean())
    assert 0.25 < share < 0.6, share             # the speaker, not the room


# ---------------------------------------------------------------------------
# The entrance veil — Omni's frosted panel (experiment 19, frames 11-30)
# ---------------------------------------------------------------------------

def _room(h=320, w=180, seed=0):
    """A textured mid-tone room, so a veil has room to lighten it."""
    rng = np.random.default_rng(seed)
    base = rng.integers(40, 170, size=(h, w, 3)).astype(np.uint8)
    return cv2.GaussianBlur(base, (0, 0), 2)


VIOLET = np.array([230, 60, 110], np.float32)       # BGR — a pattern-line colour


def _veiled(orig, box, a=0.3, lines=True):
    e = orig.astype(np.float32).copy()
    x0, y0, x1, y1 = box
    e[y0:y1, x0:x1] = e[y0:y1, x0:x1] * (1 - a) + 245.0 * a     # frosted white sheet
    if lines:
        e[y0:y1, x0:x1:10] = VIOLET                             # pattern lines under it
    return np.clip(e, 0, 255).astype(np.uint8)


def test_a_frosted_panel_is_found_and_its_lines_are_not():
    orig = _room()
    box = (0, 40, 70, 300)                                      # ~32% of the frame
    edited = _veiled(orig, box)
    m = sf.veil_restore_mask(edited, orig)
    assert m is not None
    x0, y0, x1, y1 = box
    inside = m[y0 + 10:y1 - 10, x0 + 2:x1 - 10]
    line_cols = np.zeros(inside.shape[1], bool)
    line_cols[(np.arange(x0 + 2, x1 - 10) - x0) % 10 == 0] = True
    assert inside[:, ~line_cols].mean() > 0.8, "the veil is put back to the room"
    assert inside[:, line_cols].mean() < 0.3, "the coloured lines under it are kept"
    assert m[:, 100:].max() < 0.05, "nothing outside the panel is touched"


def test_a_clean_frame_a_pattern_and_a_small_white_icon_are_never_touched():
    rng = np.random.default_rng(1)
    orig = _room()
    noisy = np.clip(orig + rng.normal(0, 3, orig.shape), 0, 255).astype(np.uint8)
    assert sf.veil_restore_mask(noisy, orig) is None, "Omni's re-render noise"
    lines = orig.copy()
    lines[:, 0:90:6] = VIOLET.astype(np.uint8)
    assert sf.veil_restore_mask(lines, orig) is None, "the pattern itself"
    icon = orig.copy()
    icon[100:120, 100:120] = 255                               # an opaque white icon
    assert sf.veil_restore_mask(icon, orig) is None


def _write_frames(path: Path, frames) -> Path:
    h, w = frames[0].shape[:2]
    cmd = [ffmpeg_exe(), "-y", "-loglevel", "error", "-f", "rawvideo", "-pix_fmt", "bgr24",
           "-s", f"{w}x{h}", "-r", str(FPS), "-i", "-", "-c:v", "libx264", "-crf", "0",
           "-preset", "ultrafast", "-pix_fmt", "yuv444p", str(path)]
    subprocess.run(cmd, input=b"".join(f.tobytes() for f in frames), check=True)
    return path


def test_the_entrance_clip_loses_its_veil_and_nothing_else(tmp_path, monkeypatch):
    """Veil on frames 0-5 of a 12-frame clip; the guard covers the first
    0.2 s (6 frames). No speaker in these frames."""
    monkeypatch.setattr(sf, "person_matte", lambda bgr: np.zeros(bgr.shape[:2], np.float32))
    room = _room(seed=3)
    orig = _write_frames(tmp_path / "o.mp4", [room] * 12)
    omni = [_veiled(room, (0, 40, 70, 300), lines=False) for _ in range(6)] + [room] * 6
    edited = _write_frames(tmp_path / "e.mp4", omni)

    cleared = _read_all(sf.restore_speaker(edited, orig, tmp_path / "c.mp4", clear_veil_sec=0.2))
    kept = _read_all(sf.restore_speaker(edited, orig, tmp_path / "k.mp4", clear_veil_sec=0.0))
    panel = (slice(60, 280), slice(5, 60))
    for i in range(6):
        diff = np.abs(cleared[i][panel].astype(int) - room[panel].astype(int)).mean()
        assert diff < 6, f"frame {i}: the veil is gone ({diff:.1f})"
        assert np.abs(kept[i][panel].astype(int) - room[panel].astype(int)).mean() > 15, \
            "without the guard the veil stays — only the entrance clip is cleared"


def test_a_speck_on_a_letterbox_bar_does_not_paste_the_bar_back(tmp_path, monkeypatch):
    """MODNet leaves specks on letterbox bars. With clear_bars (every pattern
    but structure) the matte is ignored there, so Omni's lines in the bar
    are kept; without it — structure's approved pass — they are not."""
    bar = 60

    def matte(bgr):
        m = _red_matte(bgr)
        m[H - 40:H - 20, 20:60] = 0.9            # a speck on the bottom bar
        return m

    monkeypatch.setattr(sf, "person_matte", matte)

    def boxed(i, *, pattern):
        f = _frame(i, pattern=pattern)
        if not pattern:
            f[:bar] = 0
            f[-bar:] = 0
        return f

    orig = _write_frames(tmp_path / "o.mp4", [boxed(i, pattern=False) for i in range(N)])
    edited = _write_frames(tmp_path / "e.mp4", [boxed(i, pattern=True) for i in range(N)])
    speck = (slice(H - 38, H - 22), slice(24, 56))

    def blue_share(frames):
        return min(float(((f[speck][:, :, 0] > 150) & (f[speck][:, :, 2] < 110))[:, ::8].mean())
                   for f in frames)

    cleared = _read_all(sf.restore_speaker(edited, orig, tmp_path / "c.mp4", clear_bars=True))
    kept = _read_all(sf.restore_speaker(edited, orig, tmp_path / "k.mp4"))
    assert blue_share(cleared) == 1.0, "Omni's lines on the bar survive the speck"
    assert blue_share(kept) == 0.0, "structure's pass is unchanged"
    for i, f in enumerate(cleared):
        x0, y0, x1, y1 = _speaker_box(i)
        body = f[y0 + 2:min(y1, H - bar) - 2, x0 + 2:x1 - 2].astype(int)
        assert not ((body[:, :, 0] > 150) & (body[:, :, 2] < 110)).any(), f"frame {i}"
