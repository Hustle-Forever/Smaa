"""The stray-drawing clean-up (the recipe's guards.stray "clean", opt-in).

Omni sometimes draws what nobody asked for (experiment 23: a line-art head
at the top left, outside every layer). With the clean-up on, the speaker
pass puts everything outside the FOOTPRINT of what the recipe draws (its
patterns' ink grown a little and feathered, and our icons' boxes) back to
the original — or, on top of a version, to that version — after the veil
and the hold, and the speaker still goes back over it all.

  recipe          the field stays out of the 42 default's canonical form:
                  its hash and options do not move
  speaker_front   a blob outside the footprint is gone with it and kept
                  without; the footprint's own pixels are exactly the pass's;
                  on top of a version they go back to the version; with no
                  footprint every byte is what the pass always wrote
  guards          the footprint's shape; apply forwards it only when given;
                  who cleans and who checks (only a run that asks Omni for
                  nothing but the patterns — never the approved 42 default,
                  whose brief asks for icons)
  base            a version's clean-up is kept with its parts and replayed

Synthetic clips (ffmpeg, lossless) and a colour key standing in for MODNet;
no model is called. The 42 default's run is held call for call by
tests/test_rebrand_golden.py."""
from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

import numpy as np
import pytest
from pydantic import ValidationError

from app.services import rebrand_media as media
from app.services import rebrand_pipeline as pipeline
from app.services import speaker_front as sf
from app.services.rebrand_harness import base as hbase
from app.services.rebrand_harness import guards as gd
from app.services.rebrand_harness import recipe as rc

cv2 = pytest.importorskip("cv2")

W, H, FPS, N = 160, 288, 30, 12
GREEN, RED, BLUE = (40, 160, 40), (40, 40, 220), (220, 60, 30)      # BGR
YELLOW, MAGENTA = (30, 220, 230), (200, 40, 200)
STRIP = 40                                   # the pattern: stripes in the left 40 columns
BLOB = (slice(30, 60), slice(110, 140))      # Omni's own mark, far from the strip
BAND = (slice(200, 230), slice(128, 156))    # a version's own graphic, clear of the speaker


def _write(path: Path, frames) -> Path:
    cmd = [media.ffmpeg_exe(), "-y", "-loglevel", "error", "-f", "rawvideo", "-pix_fmt", "bgr24",
           "-s", f"{W}x{H}", "-r", str(FPS), "-i", "-", "-c:v", "libx264", "-crf", "0",
           "-preset", "ultrafast", "-pix_fmt", "yuv444p", str(path)]
    subprocess.run(cmd, input=b"".join(f.tobytes() for f in frames), check=True)
    return path


def _read_all(path: Path) -> list:
    cap = cv2.VideoCapture(str(path))
    out = []
    while True:
        ok, f = cap.read()
        if not ok:
            break
        out.append(f)
    cap.release()
    return out


def _box(i):
    x0 = 60 + 2 * i                          # the speaker walks right, clear of the blob
    return x0, 110, x0 + 40, 280


def _room(i):
    f = np.zeros((H, W, 3), np.uint8)
    f[:] = GREEN
    x0, y0, x1, y1 = _box(i)
    f[y0:y1, x0:x1] = RED
    return f


def _omni(i, *, under=None):
    """Omni's clip: what it was given (the room, or a version), the pattern
    strip drawn in, and a mark of its own nobody asked for."""
    f = (_room(i) if under is None else under(i)).copy()
    f[:, 0:STRIP:8] = BLUE                  # two-pixel lines: they survive yuv420p
    f[:, 1:STRIP:8] = BLUE
    f[BLOB] = YELLOW
    return f


def _red(bgr):
    """Stand-in for MODNet: the speaker is whatever is red."""
    b, g, r = (bgr[:, :, k].astype(int) for k in range(3))
    return ((r > 150) & (g < 110) & (b < 110)).astype(np.float32)


def _near(f, colour, tol=40) -> np.ndarray:
    return np.abs(f.astype(int) - np.array(colour)).sum(axis=2) < tol


def _footprint():
    ink = np.zeros((H, W), np.float32)
    ink[:, :STRIP] = 1.0
    return gd.stray_footprint(ink)


class Capture:
    """speaker_front._Encoder, keeping every frame it is handed (the pass's
    pre-encode frames) instead of encoding them."""
    frames: dict[str, list] = {}

    def __init__(self, out, width, height, fps, audio_from, *, shortest=True):
        self.name = Path(out).name
        Capture.frames[self.name] = []

    def write(self, frame) -> None:
        Capture.frames[self.name].append(np.array(frame, copy=True))

    def close(self, what: str) -> None:
        pass

    def kill(self) -> None:
        pass


@pytest.fixture
def clips(tmp_path, monkeypatch):
    monkeypatch.setattr(sf, "person_matte", _red)
    orig = _write(tmp_path / "o.mp4", [_room(i) for i in range(N)])
    edited = _write(tmp_path / "e.mp4", [_omni(i) for i in range(N)])
    return orig, edited, tmp_path


# ---------------------------------------------------------------------------
# The recipe: opt-in, and out of the 42 default's canonical form
# ---------------------------------------------------------------------------

def test_the_42_default_does_not_move():
    d = rc.preset_42_default()
    assert rc.recipe_hash(d) == "1ab8816f2e8a7feb"
    assert rc.recipe_hash(rc.preset_42_default("scene_0")) == "a05a67c9c4c84f13"
    assert d.guards.stray is None and "stray" not in rc.canonical(d)
    assert rc.options_from_recipe(d) == {
        "target": "reel", "run_omni": True, "run_brand": False, "pattern_on": True, "pattern": "structure",
        "pattern_animation": None, "intensity": "balanced", "notes": ""}
    dump = d.model_dump(mode="json")
    off = rc.normalize({**dump, "guards": {**dump["guards"], "stray": None}})
    assert rc.recipe_hash(off) == rc.recipe_hash(d), "off, said or not, is the 42 default"


def test_the_clean_up_is_a_recipe_of_its_own():
    dump = rc.preset_42_default("scene_0").model_dump(mode="json")
    on = rc.normalize({**dump, "guards": {**dump["guards"], "stray": "clean"}})
    assert on.guards.stray == "clean" and '"stray":"clean"' in rc.canonical(on)
    assert rc.recipe_hash(on) != rc.recipe_hash(rc.preset_42_default("scene_0"))
    assert rc.normalize(on.model_dump(mode="json")) == on, "it survives being stored"
    assert rc.options_from_recipe(on)["stray_clean"] is True
    with pytest.raises(ValidationError):
        rc.normalize({**dump, "guards": {**dump["guards"], "stray": "all"}})


def test_who_cleans_and_who_checks():
    dump = rc.preset_42_default("scene_0").model_dump(mode="json")
    icon = {"id": "icons", "kind": "icon", "props": {}}

    def made(stray=None, *, elements=None, head="brief"):
        guards = {**dump["guards"], **({"stray": stray} if stray else {})}
        return rc.normalize({**dump, "guards": guards, "director": {**dump["director"], "head": head},
                             "elements": dump["elements"] if elements is None else elements})

    def both(r):
        return gd.cleans_strays(r), gd.checks_strays(r)

    # The approved 42 default: its brief asks Omni for icons of its own, so
    # what it adds beside the pattern is what it was asked for — its run is
    # as it was, looked at by nothing, and the clean-up would take them away.
    assert gd.asks_omni_for_more(made())
    assert both(made()) == (False, False) and both(made("clean")) == (False, False)
    # Our icons stamped (the studio's own starting draft): Omni is told to
    # draw none, so anything else is a stray.
    ours = [*dump["elements"], icon]
    assert not gd.asks_omni_for_more(made(elements=ours))
    assert both(made(elements=ours)) == (False, True), "off: looked for, and said"
    assert both(made("clean", elements=ours)) == (True, False), "on: taken away"
    # The harness's own opening asks for the patterns only.
    assert both(made(head="harness")) == (False, True)
    assert both(made("clean", head="harness")) == (True, False)
    # Omni asked for icons of its own: they may be anywhere — neither.
    keep = [*dump["elements"], {**icon, "props": {"omni_own": "keep"}}]
    for head in ("brief", "harness"):
        omnis = made("clean", elements=keep, head=head)
        assert gd.omni_icons(omnis) and both(omnis) == (False, False)
        assert both(made(elements=keep, head=head)) == (False, False)
    # No pattern for the brief to describe: it makes up brand graphics of
    # its own for Omni to draw — neither; the harness's opening asks for none.
    assert both(made("clean", elements=[icon])) == (False, False)
    assert both(made("clean", elements=[icon], head="harness")) == (True, False)


# ---------------------------------------------------------------------------
# The footprint
# ---------------------------------------------------------------------------

def test_the_footprint_is_the_ink_grown_then_fading_out():
    ink = np.zeros((H, W), np.float32)
    ink[100:120, 70:90] = 1.0
    fp = gd.stray_footprint(ink, boxes=[(10, 10, 30, 30)])
    short = min(W, H)
    grow, feather = short * gd.STRAY_GROW_FRAC, short * gd.STRAY_FEATHER_FRAC
    last = 89                                # the ink's right-most column
    assert fp.dtype == np.float32 and fp.shape == (H, W)
    assert fp[100:120, 70:90].min() == 1.0 and fp[10:30, 10:30].min() == 1.0, "the ink and the icon's box"
    assert fp[110, last + int(grow)] == 1.0, "grown by STRAY_GROW_FRAC"
    assert 0.0 < fp[110, last + int(round(grow + feather / 2))] < 1.0, "then fading"
    assert fp[110, last + int(np.ceil(grow + feather)) + 1] == 0.0, "and gone past it"
    assert fp[200:, :].max() == 0.0
    assert gd.stray_footprint(np.zeros((H, W), np.float32)).max() == 0.0, "nothing drawn: all goes back"


def test_the_ink_is_where_the_target_differs_from_its_frame(tmp_path):
    from PIL import Image

    frame = np.full((H, W, 3), 90, np.uint8)
    tgt = frame.copy()
    tgt[40:60, 20:50] = (200, 30, 120)
    tgt[70:72, 20:50] = 95                   # a PNG round trip's worth: not ink
    Image.fromarray(frame).save(tmp_path / "f.png")
    Image.fromarray(tgt).save(tmp_path / "t.png")
    ink = gd.drawn_ink(tmp_path / "t.png", tmp_path / "f.png", W, H)
    assert ink[40:60, 20:50].all() and ink.sum() == 20 * 30


# ---------------------------------------------------------------------------
# restore_speaker: the clean-up after the veil and the hold
# ---------------------------------------------------------------------------

def test_a_mark_outside_the_footprint_is_gone_with_the_clean_up_and_kept_without(clips):
    orig, edited, tmp = clips
    fp = _footprint()
    cleaned = _read_all(sf.restore_speaker(edited, orig, tmp / "c.mp4", footprint=fp))
    kept = _read_all(sf.restore_speaker(edited, orig, tmp / "k.mp4"))
    assert len(cleaned) == len(kept) == N
    for i, (c, k) in enumerate(zip(cleaned, kept)):
        assert _near(c[BLOB], GREEN).mean() > 0.98, f"frame {i}: Omni's own mark is gone"
        assert _near(k[BLOB], YELLOW).mean() > 0.98, f"frame {i}: without the clean-up it stays"
        strip = c[:, 0:STRIP:8]
        assert _near(strip, BLUE).mean() > 0.98, f"frame {i}: the pattern it was asked for stays"
        x0, y0, x1, y1 = _box(i)
        assert _near(c[y0 + 3:y1 - 3, x0 + 3:x1 - 3], RED).mean() > 0.98, f"frame {i}: the speaker"


def test_the_footprints_own_pixels_are_exactly_the_passs(clips, monkeypatch):
    orig, edited, tmp = clips
    Capture.frames = {}
    monkeypatch.setattr(sf, "_Encoder", Capture)
    fp = _footprint()
    sf.restore_speaker(edited, orig, tmp / "c.mp4", footprint=fp)
    sf.restore_speaker(edited, orig, tmp / "k.mp4")
    inside = fp >= 1.0
    outside = fp <= 0.0
    room = _read_all(orig)
    for i, (c, k) in enumerate(zip(Capture.frames["c.mp4"], Capture.frames["k.mp4"])):
        assert np.array_equal(c[inside], k[inside]), f"frame {i}: inside the footprint, untouched"
        assert np.array_equal(c[outside], room[i][outside]), f"frame {i}: outside it, the original exactly"


def test_on_top_of_a_version_it_goes_back_to_the_version(clips):
    orig, _edited, tmp = clips

    def version(i):
        f = _room(i)
        f[BAND] = MAGENTA
        return f

    back = _write(tmp / "b.mp4", [version(i) for i in range(N)])
    edited = _write(tmp / "e2.mp4", [_omni(i, under=version) for i in range(N)])
    out = _read_all(sf.restore_speaker(edited, orig, tmp / "x.mp4", backdrop=back, footprint=_footprint()))
    for i, f in enumerate(out):
        assert _near(f[BLOB], GREEN).mean() > 0.98, f"frame {i}: the mark is gone"
        assert _near(f[BAND], MAGENTA).mean() > 0.98, f"frame {i}: the version's own graphic, not the original"
        assert _near(f[:, 0:STRIP:8], BLUE).mean() > 0.98, f"frame {i}: the new pattern"


def test_the_veil_the_hold_then_the_clean_up_then_the_speaker():
    rng = np.random.default_rng(4)
    fe = rng.integers(0, 255, (H, W, 3), dtype=np.uint8)
    fo = rng.integers(0, 255, (H, W, 3), dtype=np.uint8)
    fb = rng.integers(0, 255, (H, W, 3), dtype=np.uint8)
    m = np.zeros((H, W), np.float32)
    m[100:200, 60:100] = 1.0
    hold = np.zeros((H, W), np.float32)
    hold[:, 120:] = 1.0
    fp = _footprint()
    got = sf.compose_frame(fe, fo, m, backdrop=fb, hold=hold, footprint=fp)
    assert np.array_equal(got[:, :STRIP][m[:, :STRIP] == 0], fe[:, :STRIP][m[:, :STRIP] == 0]), "Omni's, inside"
    assert np.array_equal(got[:, 120:], fb[:, 120:]), "the version's, held"
    assert np.array_equal(got[:10, 60:100], fb[:10, 60:100]), "outside the footprint: the version's"
    assert np.array_equal(got[100:200, 60:100], fo[100:200, 60:100]), "the speaker, from the original, last"
    no_back = sf.compose_frame(fe, fo, m, footprint=fp)
    assert np.array_equal(no_back[:10, 60:100], fo[:10, 60:100]), "no version: the original"
    assert np.array_equal(sf.compose_frame(fe, fo, m, footprint=None), sf.compose_frame(fe, fo, m))


# restore_speaker's output bytes with no clean-up: the pass as recorded before
# backdrop/hold existed (tests/test_rebrand_build_on.py), pinned to the ffmpeg /
# OpenCV / numpy they were recorded with, skipped elsewhere.
BEFORE_ENV = {"ffmpeg": "7.1-essentials_build-www.gyan.dev", "cv2": "4.13.0", "numpy": "2.4.4"}
BEFORE = {
    "plain": "17c09a07afa9cb71cb7f65598959c807fee095ab6039ea2a3c98c7cd66dd4de3",
    "veil": "9ccfdd84a2f5436b21ac33b1224c4a3e2d1eab1b9a863ffaa2591767a69d0071",
    "veil_keep": "bc879f281d9f8c21af90831a490368a5a121659eccef5a7b8b30e2e3e79b530b",
}


def _baseline_clips(tmp: Path) -> tuple[Path, Path]:
    """tests/test_rebrand_build_on.py's _baseline_clips, as recorded."""
    bw, bh, n = 160, 288, 20

    def frame(i, pattern, veil=False):
        f = np.zeros((bh, bw, 3), np.uint8)
        f[:] = (40, 160, 40)
        x0 = 30 + 3 * i
        f[90:260, x0:x0 + 50] = (40, 40, 220)
        if pattern:
            f[:, ::8] = (220, 60, 30)
            f[:, 1::8] = (220, 60, 30)
        if veil and i < 6:
            g = f.astype(np.float32)
            g[20:120, 0:60] = g[20:120, 0:60] * 0.7 + 245 * 0.3
            f = np.clip(g, 0, 255).astype(np.uint8)
        return f

    def write(path, frames):
        cmd = [media.ffmpeg_exe(), "-y", "-loglevel", "error", "-f", "rawvideo", "-pix_fmt", "bgr24",
               "-s", f"{bw}x{bh}", "-r", str(FPS), "-i", "-", "-c:v", "libx264", "-crf", "0",
               "-preset", "ultrafast", "-pix_fmt", "yuv444p", str(path)]
        subprocess.run(cmd, input=b"".join(f.tobytes() for f in frames), check=True)
        return path

    orig = write(tmp / "o.mp4", [frame(i, False) for i in range(n)])
    edited = write(tmp / "e.mp4", [frame(i, True, veil=True) for i in range(n)])
    return orig, edited


def test_no_footprint_is_byte_identical(tmp_path, monkeypatch):
    import hashlib

    version = subprocess.run([media.ffmpeg_exe(), "-version"], capture_output=True,
                             text=True).stdout.split()[2]
    env = {"ffmpeg": version, "cv2": cv2.__version__, "numpy": np.__version__}
    monkeypatch.setattr(sf, "person_matte", _red)
    orig, edited = _baseline_clips(tmp_path)
    keep = np.zeros((288, 160), np.float32)
    keep[40:90, :] = 1.0
    cases = {"plain": {}, "veil": {"clear_veil_sec": 0.2},
             "veil_keep": {"clear_veil_sec": 0.2, "veil_keep": keep}}
    got = {}
    for name, kw in cases.items():
        a = sf.restore_speaker(edited, orig, tmp_path / f"{name}.mp4", **kw)
        b = sf.restore_speaker(edited, orig, tmp_path / f"{name}_none.mp4", **kw, footprint=None)
        assert a.read_bytes() == b.read_bytes(), name
        got[name] = hashlib.sha256(a.read_bytes()).hexdigest()
    if env != BEFORE_ENV:
        pytest.skip(f"recorded with {BEFORE_ENV}, this is {env}")
    assert got == BEFORE, "restore_speaker with no footprint is not what it was"


def test_apply_passes_the_footprint_only_when_there_is_one(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(sf, "restore_speaker", lambda e, o, out, **kw: calls.append(sorted(kw)) or out)
    g = gd.ChunkGuards(speaker_front=True, clear_veil_sec=3.0, clear_bars=False)
    gd.apply(tmp_path / "e", tmp_path / "o", tmp_path / "x", g)
    gd.apply(tmp_path / "e", tmp_path / "o", tmp_path / "x", g, footprint=None)
    gd.apply(tmp_path / "e", tmp_path / "o", tmp_path / "x", g, footprint=np.ones((2, 2)))
    assert calls == [["clear_bars", "clear_veil_sec"], ["clear_bars", "clear_veil_sec"],
                     ["clear_bars", "clear_veil_sec", "footprint"]]
    assert "stray_clean" not in g.public(), "a record from before it keeps its shape"
    assert gd.ChunkGuards(True, 0.0, False, stray_clean=True).public()["stray_clean"] is True


# ---------------------------------------------------------------------------
# A version's clean-up is kept with its parts, and replayed
# ---------------------------------------------------------------------------

def test_a_versions_clean_up_is_replayed_with_its_parts(tmp_path, monkeypatch):
    monkeypatch.setattr(pipeline, "REBRAND_ROOT", tmp_path / "rb")
    run_dir = tmp_path / "rb" / "81" / "omni"
    run_dir.mkdir(parents=True)
    icons = {}
    for j in range(2):
        files = {}
        for kind in ("prestamp", "clean"):
            p = run_dir / f"scene_1_c{j}_{kind}.mp4"
            p.write_bytes(f"{kind}-{j}".encode())
            files[kind] = {"path": str(p)}
        if j == 0:
            fp = run_dir / "scene_1_c0_footprint.npy"
            np.save(fp, np.full((4, 4), 0.25, np.float32))
            files["footprint"] = {"path": str(fp)}
        icons[f"c{j}"] = {"span": [4.0 * j, 4.0], "stamped": [], "files": files,
                          "guards": {"clear_veil_sec": 0.0, "clear_bars": False}}
    exp = {"id": 81, "status": "ready", "options": {"target": "scene_0"},
           "arms": {"omni": {"status": "ready", "stages": {"icons": icons}}}}
    kept = asyncio.run(hbase.replays(exp, tmp_path / "kept"))
    assert kept[0].footprint is not None and kept[1].footprint is None
    assert np.array_equal(kept[0].kwargs()["footprint"], np.full((4, 4), 0.25, np.float32))
    assert "footprint" not in kept[1].kwargs(), "a part made without it is made again without it"
    assert "footprint" in hbase.KEPT_FILES
