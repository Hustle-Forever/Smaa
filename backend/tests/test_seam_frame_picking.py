"""Picking a frame that has something on it.

Intro cards end on a fade to black and generated clips often open on one, so
the literal boundary frame of a clip is regularly a black rectangle. A
transition designed from a black rectangle is designed from nothing: no
palette to inherit, no busy map, no subject.

These build real clips with ffmpeg and check the scan walks past the fade.
"""
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from app.services import rebrand_media as media
from app.services import rebrand_moments as moments


def _ffmpeg():
    try:
        return media.ffmpeg_exe()
    except Exception:  # noqa: BLE001
        return None


pytestmark = pytest.mark.skipif(_ffmpeg() is None, reason="ffmpeg unavailable")


def _clip(out: Path, *, seconds: float, fade_out: float = 0.0,
          fade_in: float = 0.0) -> Path:
    """A clip with visible structure, optionally fading at either end."""
    out.parent.mkdir(parents=True, exist_ok=True)
    vf = [f"testsrc=size=240x426:rate=30:duration={seconds}"]
    filters = []
    if fade_in > 0:
        filters.append(f"fade=t=in:st=0:d={fade_in}")
    if fade_out > 0:
        filters.append(f"fade=t=out:st={seconds - fade_out}:d={fade_out}")
    cmd = [media.ffmpeg_exe(), "-y", "-f", "lavfi", "-i", vf[0]]
    if filters:
        cmd += ["-vf", ",".join(filters)]
    cmd += ["-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
            str(out)]
    subprocess.run(cmd, capture_output=True, check=True)
    return out


def test_the_last_frame_of_a_fading_card_is_black(tmp_path):
    """The premise. If this ever stops holding, the scan is unnecessary."""
    clip = _clip(tmp_path / "fade.mp4", seconds=2.0, fade_out=0.8)
    frame = media.extract_frame(clip, tmp_path / "last.png", at="last")
    m = moments.measure(frame)
    assert m.mean_lum < moments.MIN_MEAN_LUM, (
        f"expected a black tail frame, got luminance {m.mean_lum}")


def test_the_scan_walks_back_to_real_content(tmp_path):
    """The contract is relative, not a fixed brightness: the chosen frame must
    still look like the clip does before it starts fading."""
    clip = _clip(tmp_path / "fade.mp4", seconds=2.0, fade_out=0.8)
    picked = moments.pick_content_frame(clip, tmp_path / "scan", edge="last",
                                        label="intro")
    assert not picked.fell_back
    assert picked.measured.stddev >= moments.MIN_STDDEV

    # Measure the clip before the fade begins (it starts at 1.2s) and require
    # the picked frame to have kept most of that brightness.
    ref = moments._extract_at(clip, tmp_path / "ref.png", 0.9)
    assert ref is not None
    unfaded = moments.measure(ref).mean_lum
    assert picked.measured.mean_lum >= moments.FADE_FLOOR * unfaded, (
        f"picked a frame at {picked.at_sec}s with luminance "
        f"{picked.measured.mean_lum} against an unfaded {unfaded}")

    # And it must be a frame the fade had not yet eaten: comfortably before the
    # end, well clear of the black the clip finishes on.
    assert picked.at_sec < 1.6, picked.at_sec
    last = moments.measure(media.extract_frame(clip, tmp_path / "l.png", at="last"))
    assert picked.measured.mean_lum > last.mean_lum * 4


def test_a_clip_that_fades_in_yields_a_real_first_frame(tmp_path):
    clip = _clip(tmp_path / "fadein.mp4", seconds=2.0, fade_in=0.6)
    picked = moments.pick_content_frame(clip, tmp_path / "scan", edge="first",
                                        label="main")
    assert not picked.fell_back
    assert picked.at_sec >= 0.2, "should have skipped into the fade-in"
    assert picked.measured.mean_lum >= moments.MIN_MEAN_LUM


def test_a_clip_with_no_fade_uses_its_own_edge(tmp_path):
    """No fade means no scanning: the boundary frame IS the content frame."""
    clip = _clip(tmp_path / "clean.mp4", seconds=1.5)
    picked = moments.pick_content_frame(clip, tmp_path / "scan", edge="last",
                                        label="intro")
    assert not picked.fell_back
    assert picked.scanned == 1
    assert picked.at_sec > 1.4


def test_an_entirely_black_clip_admits_it_rather_than_pretending(tmp_path):
    """Honest failure. A caller that is handed a black frame needs to know it
    was handed a black frame, so it can stop trimming and stop inferring a
    palette from it."""
    out = tmp_path / "black.mp4"
    subprocess.run(
        [media.ffmpeg_exe(), "-y", "-f", "lavfi",
         "-i", "color=c=black:size=240x426:rate=30:duration=1.2",
         "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
         str(out)],
        capture_output=True, check=True)
    picked = moments.pick_content_frame(out, tmp_path / "scan", edge="last",
                                        label="intro")
    assert picked.fell_back
    assert picked.scanned > 1, "it should have looked before giving up"


def test_measurements_describe_the_frame_they_are_given(tmp_path):
    """The measured half of the analysis is what everything degrades to when
    Vertex is unreachable, so it has to stand on its own."""
    out = tmp_path / "red.mp4"
    subprocess.run(
        [media.ffmpeg_exe(), "-y", "-f", "lavfi",
         "-i", "color=c=0xD22F2F:size=240x426:rate=30:duration=0.5",
         "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
         str(out)],
        capture_output=True, check=True)
    frame = media.extract_frame(out, tmp_path / "f.png", at="first")
    m = moments.measure(frame)
    r, g, b = (int(m.accent[i:i + 2], 16) for i in (1, 3, 5))
    assert r > 150 and r > g + 60 and r > b + 60, m.accent
    assert m.accent_sat > 0.3
    assert len(m.busy) == moments.GRID_COLS * moments.GRID_ROWS


def test_slice_and_join_keep_sound_under_picture(tmp_path):
    """A seam is only invisible if the voice stays with the lips across it.

    Joins the same clip to itself through concat_exact and checks the result is
    exactly as long as its parts — the drift this guards against was a third of
    a frame per join, which is inaudible once and obvious three times."""
    src = tmp_path / "tone.mp4"
    subprocess.run(
        [media.ffmpeg_exe(), "-y",
         "-f", "lavfi", "-i", "testsrc=size=240x426:rate=30:duration=2",
         "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
         "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
         "-c:a", "aac", "-shortest", str(src)],
        capture_output=True, check=True)

    a = media.slice_clip(src, tmp_path / "a.mp4", start=0.0, end=1.0)
    b = media.slice_clip(src, tmp_path / "b.mp4", start=1.0)
    assert a and b
    joined = media.concat_exact([a, b], tmp_path / "joined.mp4",
                                width=240, height=426)

    video = media.video_seconds(joined)
    expected = media.video_seconds(a) + media.video_seconds(b)
    assert abs(video - expected) < 0.05, (video, expected)

    # And the audio must not have drifted away from it.
    raw = subprocess.run(
        [media.ffmpeg_exe(), "-v", "error", "-i", str(joined), "-map", "0:a",
         "-ac", "1", "-ar", "16000", "-f", "s16le", "-"],
        capture_output=True, check=True).stdout
    audio = len(raw) / 2 / 16000
    assert abs(audio - video) < 0.05, (audio, video)


def test_an_empty_span_is_dropped_not_written(tmp_path):
    src = tmp_path / "x.mp4"
    subprocess.run(
        [media.ffmpeg_exe(), "-y", "-f", "lavfi",
         "-i", "testsrc=size=160x160:rate=30:duration=1",
         "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
         str(src)],
        capture_output=True, check=True)
    assert media.slice_clip(src, tmp_path / "none.mp4", start=0.9, end=0.905) is None
