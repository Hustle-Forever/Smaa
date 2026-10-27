"""Rebrand studio icons, the run: stamped by us inside the speaker pass.

  speaker_front   compose_frame (veil → hold → icons → speaker), restore_speaker
                  with stamps and a clean twin, frame_at (one frame exactly as
                  the pass writes it), stamp_only (no matte, only after a
                  failed pass)
  runner          a recipe with icons: every prompt ends with ICONS_CLAUSE,
                  the pass always runs, each part keeps its pre-stamp and clean
                  clips (uploaded) and the "icons" stage; an icon-only change on
                  a finished version is that version's own pass again — no
                  Omni; Omni's own marks are looked for (stray)
  lineage         a version with our icons is built on from its clean parts,
                  never a cut of its final; Omni's own icons are kept only on
                  a version that has them, and ours never go on top of those
  studio/router   the exact still (/icons/moment) is the run's pre-encode frame;
                  Generate re-derives the plan (409 when it moved) and freezes
                  it into the recipe
  brief           told the pipeline places the icons, only when it does

Omni, Gemini, the bucket and the database are faked (an autouse guard makes
every Vertex call and every download raise); ffmpeg and the synthetic clips
are real; the speaker matte is a colour key standing in for MODNet. The 42
default is held byte for byte by tests/test_rebrand_golden.py."""
from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
from pathlib import Path

import numpy as np
import pytest
from fastapi import HTTPException
from PIL import Image

from app.services import asset_storage
from app.services import omni_client as omni
from app.services import person_matte
from app.services import rebrand_brief
from app.services import rebrand_media as media
from app.services import rebrand_pipeline as pipeline
from app.services import rebrand_store as store
from app.services import speaker_front as sf
from app.services import vertex_client
from app.services.rebrand_harness import base as hbase
from app.services.rebrand_harness import guards as gd
from app.services.rebrand_harness import icon_catalog as ic
from app.services.rebrand_harness import icon_render as ir
from app.services.rebrand_harness import prompt as hprompt
from app.services.rebrand_harness import recipe as rc
from app.services.rebrand_harness import runner
from app.services.rebrand_harness import stages as st
from app.services.rebrand_harness import stray
from app.services.rebrand_harness import studio
from app.services.rebrand_harness.elements import icon as I

cv2 = pytest.importorskip("cv2")

GOLD = Path(__file__).parent / "fixtures" / "rebrand_golden"
INTRO, FRAME, PERSON = GOLD / "intro.mp4", GOLD / "frame.png", GOLD / "person.png"
BRIEF = {"omni_scene_prompt": "EDIT the clip; keep #0663a3 and '42 pattern' out."}

W, H, FPS = 160, 288, 30
GREEN, RED, BLUE = (40, 160, 40), (40, 40, 220), (220, 60, 30)      # BGR


class FakeStore:
    """yas_rebrand_icons, empty."""

    async def by_key(self, name_key):
        return None

    async def by_id(self, icon_id):
        return None

    async def insert(self, row):
        return row

    async def set_hidden(self, icon_id, hidden):
        return False

    async def listing(self, limit, include_hidden):
        return []


@pytest.fixture(autouse=True)
def guard(monkeypatch, tmp_path):
    """No paid call, no bucket, no database: every Vertex call and every
    download raises, uploads are recorded."""
    calls: list[str] = []

    def refuse(name):
        async def _refuse(*a, **k):
            calls.append(name)
            raise AssertionError(f"paid call to vertex_client.{name} in a test")
        return _refuse

    uploads: list[str] = []

    async def persist(*, abs_path, bucket_key, mime="video/mp4"):
        assert Path(abs_path).is_file()
        uploads.append(bucket_key)
        return "rebrand-test", bucket_key

    async def download(**kw):
        calls.append("download")
        raise AssertionError("a bucket download in a test")

    monkeypatch.setattr(vertex_client, "gemini_multimodal", refuse("gemini_multimodal"))
    monkeypatch.setattr(vertex_client, "gemini_image", refuse("gemini_image"))
    monkeypatch.setattr(asset_storage, "persist_local_file", persist)
    monkeypatch.setattr(asset_storage, "download_bytes", download)
    monkeypatch.setattr(ic, "CACHE_DIR", tmp_path / "icons_cache")
    ic._DRAW_FAILED.clear()
    old = ic.set_store(FakeStore())
    yield {"calls": calls, "uploads": uploads}
    ic.set_store(old)
    assert not calls, f"unexpected calls: {calls}"


async def _quiet(*a, **k):
    return None


# ---------------------------------------------------------------------------
# Synthetic clips and icons
# ---------------------------------------------------------------------------

def _write(path: Path, frames, fps=FPS, audio_sec: float | None = None) -> Path:
    h, w = frames[0].shape[:2]
    cmd = [media.ffmpeg_exe(), "-y", "-loglevel", "error", "-f", "rawvideo", "-pix_fmt", "bgr24",
           "-s", f"{w}x{h}", "-r", str(fps), "-i", "-"]
    if audio_sec:
        cmd += ["-f", "lavfi", "-t", f"{audio_sec:.4f}", "-i", "sine=frequency=330:sample_rate=44100",
                "-map", "0:v", "-map", "1:a", "-c:a", "aac"]
    cmd += ["-c:v", "libx264", "-crf", "0", "-preset", "ultrafast", "-pix_fmt", "yuv444p", str(path)]
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
    x0 = 30 + (i % 20)
    return x0, 110, x0 + 50, 280


def _room(i, *, bars: int = 0):
    f = np.zeros((H, W, 3), np.uint8)
    f[:] = GREEN
    x0, y0, x1, y1 = _box(i)
    f[y0:y1, x0:x1] = RED
    if bars:
        f[:bars], f[H - bars:] = 0, 0
    return f


def _omni(i, *, veil: bool = False, bars: int = 0):
    """Omni's clip: the room, its lines over everything (speaker too), a
    frosted panel on its first frames when asked."""
    f = _room(i)
    f[:, ::8] = BLUE
    f[:, 1::8] = BLUE
    if veil and i < 6:
        g = f.astype(np.float32)
        g[20:120, 0:60] = g[20:120, 0:60] * 0.7 + 245 * 0.3
        f = np.clip(g, 0, 255).astype(np.uint8)
    return f


def _red(bgr):
    """Stand-in for MODNet: the speaker is whatever is red."""
    b, g, r = (bgr[:, :, k].astype(int) for k in range(3))
    return ((r > 150) & (g < 110) & (b < 110)).astype(np.float32)


def _res(gid: str = "code", frm: float = 0.2, settle: float = 0.4667, to: float = 0.8,
         box: dict | None = None, entrance: str = "glitch", label: str = "Code",
         max_overlap: float = 0.0) -> dict:
    return rc.IconResolved.model_validate({
        "glyph": {"source": "library", "id": gid}, "label": label, "from_sec": frm, "settle_sec": settle,
        "to_sec": to, "box": box or {"x": 0.6, "y": 0.04, "w": 0.3, "h": 0.17},
        "fill": "#7f2bf2", "ink": "#ffffff", "fx": {"entrance": entrance, "seed": f"s:{gid}"},
        "style": "badge", "word": label.lower(), "purpose": "", "max_overlap": max_overlap,
    }).model_dump(mode="json")


class Capture:
    """speaker_front._Encoder, keeping every frame it is handed (the pass's
    pre-encode frames) instead of encoding them."""
    frames: dict[str, list] = {}

    def __init__(self, out, width, height, fps, audio_from, *, shortest=True):
        self.name = Path(out).name
        Capture.frames[self.name] = []

    def write(self, frame):
        Capture.frames[self.name].append(np.array(frame, copy=True))

    def close(self, what):
        pass

    def kill(self):
        pass


@pytest.fixture
def capture(monkeypatch):
    Capture.frames = {}
    monkeypatch.setattr(sf, "_Encoder", Capture)
    monkeypatch.setattr(sf, "person_matte", _red)
    return Capture.frames


# ---------------------------------------------------------------------------
# speaker_front: one frame, the pass, the clean twin
# ---------------------------------------------------------------------------

def test_compose_frame_with_nothing_to_stamp_is_the_pass_as_it_was():
    rng = np.random.default_rng(3)
    fe = rng.integers(0, 255, (H, W, 3), dtype=np.uint8)
    fo = rng.integers(0, 255, (H, W, 3), dtype=np.uint8)
    m = rng.random((H, W)).astype(np.float32)
    old = np.clip(np.rint(fe.astype(np.float32) * (1.0 - m[:, :, None]) + fo.astype(np.float32) * m[:, :, None]),
                  0, 255).astype(np.uint8)
    assert np.array_equal(sf.compose_frame(fe, fo, m), old)
    nothing = ir.Stamper([_res(frm=5.0, settle=5.3, to=6.0)], fps=FPS, frame_size=(W, H))
    assert np.array_equal(sf.compose_frame(fe, fo, m, stamps=nothing, t=0.1), old), "no icon on screen"


def test_no_stamps_writes_what_restore_always_wrote(tmp_path, monkeypatch):
    monkeypatch.setattr(sf, "person_matte", _red)
    orig = _write(tmp_path / "o.mp4", [_room(i) for i in range(12)])
    edited = _write(tmp_path / "e.mp4", [_omni(i, veil=True) for i in range(12)])
    a = sf.restore_speaker(edited, orig, tmp_path / "a.mp4", clear_veil_sec=0.2)
    b = sf.restore_speaker(edited, orig, tmp_path / "b.mp4", clear_veil_sec=0.2, stamps=None, clean=None)
    c = gd.apply(edited, orig, tmp_path / "c.mp4",
                 gd.ChunkGuards(speaker_front=True, clear_veil_sec=0.2, clear_bars=False), stamps=None)
    assert a.read_bytes() == b.read_bytes() == c.read_bytes()


def test_apply_passes_the_stamps_and_the_clean_twin_only_when_there_are_some(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(sf, "restore_speaker", lambda e, o, out, **kw: calls.append(sorted(kw)) or out)
    g = gd.ChunkGuards(speaker_front=True, clear_veil_sec=0.0, clear_bars=False)
    gd.apply(tmp_path / "e", tmp_path / "o", tmp_path / "x", g, stamps=None, clean=None)
    gd.apply(tmp_path / "e", tmp_path / "o", tmp_path / "x", g, stamps=object(), clean=tmp_path / "c")
    assert calls == [["clear_bars", "clear_veil_sec"], ["clean", "clear_bars", "clear_veil_sec", "stamps"]]


def test_stamps_change_pixels_only_in_their_box_and_only_in_their_window(tmp_path, capture):
    n = 30
    orig = _write(tmp_path / "o.mp4", [_room(i) for i in range(n)])
    edited = _write(tmp_path / "e.mp4", [_omni(i) for i in range(n)])
    stamps = ir.Stamper([_res()], fps=FPS, frame_size=(W, H))
    sf.restore_speaker(edited, orig, tmp_path / "plain.mp4")
    sf.restore_speaker(edited, orig, tmp_path / "icons.mp4", stamps=stamps, clean=tmp_path / "clean.mp4")
    plain, icons, clean = capture["plain.mp4"], capture["icons.mp4"], capture["clean.mp4"]
    assert len(plain) == len(icons) == len(clean) == n
    shown = 0
    for i in range(n):
        assert np.array_equal(clean[i], plain[i]), f"frame {i}: the clean twin is the pass without icons"
        diff = (icons[i] != plain[i]).any(axis=2)
        if not 6 <= i < 24:
            assert not diff.any(), f"frame {i}: outside the icon's window nothing changes"
            continue
        inside = np.zeros((H, W), bool)
        for x0, y0, x1, y1 in stamps.boxes(i / FPS, drawn=True):
            inside[y0:y1, x0:x1] = True
        assert not (diff & ~inside).any(), f"frame {i}: a pixel outside the icon changed"
        shown += int(diff.any())
    assert shown >= 10, "the icon is on screen through its window"


def test_the_icon_is_behind_the_speaker(tmp_path, capture):
    """An icon laid right across the speaker: wherever the matte is the
    speaker, the frame is the original's, pixel for pixel; beside them, the
    icon."""
    n = 12
    orig = _write(tmp_path / "o.mp4", [_room(0) for _ in range(n)])
    edited = _write(tmp_path / "e.mp4", [_omni(0) for _ in range(n)])
    over = _res(frm=0.0, settle=0.1, to=1.0, entrance="none", box={"x": 0.1, "y": 0.35, "w": 0.6, "h": 0.4})
    stamps = ir.Stamper([over], fps=FPS, frame_size=(W, H))
    sf.restore_speaker(edited, orig, tmp_path / "plain.mp4")
    sf.restore_speaker(edited, orig, tmp_path / "x.mp4", stamps=stamps)
    fo = _read_all(orig)
    x0, y0, x1, y1 = stamps.boxes(0.2)[0]
    box = np.zeros((H, W), bool)
    box[y0:y1, x0:x1] = True
    for i, (f, p) in enumerate(zip(capture["x.mp4"], capture["plain.mp4"])):
        speaker = _red(fo[i]) == 1.0
        assert (speaker & box).sum() > 500, "the icon is laid across the speaker"
        assert np.array_equal(f[speaker], fo[i][speaker]), f"frame {i}: the speaker in front of the icon"
        assert (f[box & ~speaker] != p[box & ~speaker]).any(), f"frame {i}: the icon beside them"


def test_a_speck_on_a_bar_never_punches_the_bar_through_an_icon(tmp_path, capture, monkeypatch):
    """Structure's pass keeps MODNet's specks on the bars (clear_bars off):
    over an icon in a bar they would lay the bar's black on it. On a frame
    an icon is drawn, the matte is emptied where the bar and the icon meet —
    and only there."""
    bar = 40

    def specky(bgr):
        m = _red(bgr)
        m[H - 36:H - 4, 10:150] = 0.9                  # specks across the bottom bar
        return m

    monkeypatch.setattr(sf, "person_matte", specky)
    n = 12
    orig = _write(tmp_path / "o.mp4", [_room(i, bars=bar) for i in range(n)])
    edited = _write(tmp_path / "e.mp4", [_omni(i) for i in range(n)])
    icon = _res(frm=0.0, settle=0.1, to=1.0, entrance="none", box={"x": 0.3, "y": 0.87, "w": 0.3, "h": 0.12})
    stamps = ir.Stamper([icon], fps=FPS, frame_size=(W, H))
    sf.restore_speaker(edited, orig, tmp_path / "x.mp4", stamps=stamps, clean=tmp_path / "c.mp4")
    x0, y0, x1, y1 = stamps.boxes(0.2)[0]
    omni_frames = _read_all(edited)
    for i, (f, c) in enumerate(zip(capture["x.mp4"], capture["c.mp4"])):
        pre = stamps.under(omni_frames[i], i / FPS)           # the icon laid on Omni's frame
        zone = (slice(max(y0, H - 36), min(y1, H - 4)), slice(x0, x1))
        assert np.array_equal(f[zone], pre[zone]), f"frame {i}: the icon, not the bar's black"
        assert (c[zone] != pre[zone]).any(), "without the icon the specks lay the black as ever"
        outside = (slice(H - 36, H - 4), slice(10, max(10, x0 - 1)))
        assert np.array_equal(f[outside], c[outside]), "beside the icon the pass is as it was"


def test_stamp_only_keeps_the_audio_every_frame_and_only_icons_clear_of_the_speaker(tmp_path):
    n = 20
    clip = _write(tmp_path / "c.mp4", [_omni(i) for i in range(n)], audio_sec=n / FPS)
    safe = _res(frm=0.1, settle=0.3, to=0.6, entrance="none")
    covers = _res("piggy-bank", frm=0.1, settle=0.3, to=0.6, entrance="none", label="Free", max_overlap=0.2,
                  box={"x": 0.1, "y": 0.5, "w": 0.3, "h": 0.17})
    assert I.unmatted([safe, covers]) == [safe], "the speaker covers more than 3 % of it"
    [stamps] = asyncio.run(I.stampers(I.unmatted([safe, covers]), [(0.0, n / FPS)], frame_size=(W, H)))
    out = sf.stamp_only(clip, tmp_path / "s.mp4", stamps)
    assert media.frame_count(out) == n

    def pcm(path):
        return subprocess.run([media.ffmpeg_exe(), "-loglevel", "error", "-i", str(path), "-map", "0:a",
                               "-f", "s16le", "-"], capture_output=True, check=True).stdout

    assert pcm(out) == pcm(clip), "the clip's own audio, copied"
    frames = _read_all(out)
    plain = _read_all(sf.stamp_only(clip, tmp_path / "p.mp4", None))
    x0, y0, x1, y1 = stamps.boxes(0.4)[0]
    assert np.abs(frames[12][y0:y1, x0:x1].astype(int) - plain[12][y0:y1, x0:x1].astype(int)).mean() > 20
    assert np.abs(frames[0].astype(int) - plain[0].astype(int)).mean() < 2, "nothing before it comes in"


# ---------------------------------------------------------------------------
# frame_at: the exact still is the run's frame
# ---------------------------------------------------------------------------

@pytest.fixture
def tail_clips(tmp_path):
    """239 frames (as fit_video_to_audio leaves a part): the original, Omni's
    with a frosted panel on its first frames, and a version under it."""
    n = 239
    orig = _write(tmp_path / "o.mp4", [_room(i) for i in range(n)])
    edited = _write(tmp_path / "e.mp4", [_omni(i, veil=True) for i in range(n)])
    back = [_room(i) for i in range(n)]
    for f in back:
        f[200:230, 100:150] = (200, 40, 200)
    backdrop = _write(tmp_path / "b.mp4", back)
    icons = [_res(frm=0.0, settle=0.2667, to=1.0, box={"x": 0.55, "y": 0.02, "w": 0.4, "h": 0.2}),
             _res("piggy-bank", frm=7.0, settle=7.2667, to=8.0, label="Free", entrance="fade",
                  box={"x": 0.05, "y": 0.02, "w": 0.4, "h": 0.2})]
    keep = np.zeros((H, W), np.float32)
    keep[:, :8] = 1.0
    hold = np.ones((H, W), np.float32)
    hold[:, :80] = 0.0
    footprint = np.zeros((H, W), np.float32)      # the stray-drawing clean-up's
    footprint[:, :40] = 1.0
    footprint[:, 40:48] = 0.5
    return {"n": n, "orig": orig, "edited": edited, "backdrop": backdrop, "icons": icons, "keep": keep,
            "hold": hold, "footprint": footprint, "tmp": tmp_path}


FRAMES_TO_CHECK = (0, 1, 2, 3, 5, 6, 7, 100, 219, 234, 235, 236, 237, 238)


@pytest.mark.parametrize("case", ["veil", "hold", "clean", "clean on top"])
def test_frame_at_is_the_passs_own_frame_to_the_last(tail_clips, capture, case):
    c = tail_clips
    stamps = ir.Stamper(c["icons"], fps=FPS, frame_size=(W, H))
    kw = {"veil": {"clear_veil_sec": 0.2, "veil_keep": c["keep"]},
          "hold": {"clear_bars": True, "backdrop": c["backdrop"], "hold": c["hold"]},
          "clean": {"clear_veil_sec": 0.2, "footprint": c["footprint"]},
          "clean on top": {"clear_veil_sec": 0.2, "backdrop": c["backdrop"], "hold": c["hold"],
                           "footprint": c["footprint"]}}[case]
    sf.restore_speaker(c["edited"], c["orig"], c["tmp"] / "run.mp4", stamps=stamps, **kw)
    run = capture["run.mp4"]
    assert len(run) == c["n"]
    for i in FRAMES_TO_CHECK:
        got, idx, fps = sf.frame_at(c["edited"], c["orig"], i / FPS, stamps=stamps, **kw)
        assert idx == i and fps == pytest.approx(FPS)
        assert np.array_equal(got, run[i]), f"frame {i} of {c['n']}"
    assert sf.matte_keys(237, 239) == (237, 238) and sf.matte_keys(238, 239) == (237, 238)
    assert sf.matte_keys(236, 239) == (234, 237) and sf.matte_keys(237, 238) == (237, 237)


# ---------------------------------------------------------------------------
# The run: a recipe with icons
# ---------------------------------------------------------------------------

def _person() -> np.ndarray:
    return np.asarray(Image.open(PERSON).convert("L"), dtype="float32") / 255.0


def with_icons(*resolved: dict, **props) -> rc.Recipe:
    d42 = rc.preset_42_default("scene_0").model_dump(mode="json")
    el = {"id": "icons", "kind": "icon", "props": {**props, **({"resolved": list(resolved)} if resolved else {})}}
    return rc.normalize({**d42, "seed": "studio:1:scene_0", "elements": [*d42["elements"], el]})


CODE = _res(frm=1.6, settle=1.8667, to=2.8, box={"x": 0.04, "y": 0.3, "w": 0.16, "h": 0.09})


@pytest.fixture
def icon_run(tmp_path, monkeypatch):
    """A per-scene harness run of two parts (4 s each) with Omni and the
    video tools faked, every call logged — the golden test's fakes."""
    def run(recipe: rc.Recipe, *, restore=None) -> dict:
        log: list = []
        finished: dict = {}
        work = tmp_path / f"run{len(list(tmp_path.glob('run*')))}"

        async def append_arm_step(exp_id, arm, step):
            log.append(["step", step["name"], step["status"]])

        async def patch_arm(exp_id, arm, patch):
            log.append(["patch", sorted(patch)])
            finished.setdefault("patches", []).append(patch)

        def split_chunks(src, out_dir):
            Path(out_dir).mkdir(parents=True, exist_ok=True)
            out = [Path(out_dir) / "c0.mp4", Path(out_dir) / "c1.mp4"]
            for p in out:
                p.write_bytes(b"chunk " + p.name.encode())
            return out

        def extract_frame(video, out, *, at):
            Path(out).parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(FRAME, out)
            return Path(out)

        async def person_mask(frame):
            return _person()

        async def edit_video(video, prompt, *, aspect, out, images=None):
            log.append(["omni", Path(video).name, prompt])
            Path(out).parent.mkdir(parents=True, exist_ok=True)
            Path(out).write_bytes(b"omni " + Path(video).name.encode())
            return {"path": out}

        def fit(raw, chunk, out, **kw):
            Path(out).write_bytes(b"fitted " + Path(chunk).name.encode())
            return out

        def fake_restore(edited, original, out, **kw):
            log.append(["restore", Path(edited).name, Path(original).name,
                        sorted(k for k in kw if k not in ("stamps",)), kw.get("stamps")])
            Path(out).write_bytes(b"front")
            if kw.get("clean"):
                Path(kw["clean"]).write_bytes(b"clean")
            return out

        async def finish(ctx, arm, final, parts, extra):
            finished["extra"] = extra

        monkeypatch.setattr(store, "append_arm_step", append_arm_step)
        monkeypatch.setattr(store, "patch_arm", patch_arm)
        monkeypatch.setattr(media, "split_chunks", split_chunks)
        monkeypatch.setattr(media, "extract_frame", extract_frame)
        monkeypatch.setattr(person_matte, "person_mask", person_mask)
        monkeypatch.setattr(omni, "edit_video", edit_video)
        monkeypatch.setattr(media, "fit_video_to_audio", fit)
        monkeypatch.setattr(sf, "restore_speaker", restore or fake_restore)
        monkeypatch.setattr(media, "concat", lambda parts, out: out)
        monkeypatch.setattr(pipeline, "_finish_arm", finish)
        monkeypatch.setattr(runner, "_spans", lambda chunks: [(0.0, 4.0), (4.0, 4.0)][:len(chunks)])
        intro = pipeline.Segment(kind="intro", index=0, source=INTRO, is_still=False)
        scene = pipeline.Segment(kind="scene", index=0, source=work / "scene0.mp4", is_still=False)
        scene.base = work / "base_scene0.mp4"
        ctx = pipeline.Ctx(exp_id=15, run_id=806, aspect="9:16", width=720, height=1280, work=work,
                           options=pipeline.normalize_options({"target": "scene_0"}), run_ctx={"topic": "t"},
                           intro=intro, scenes=[scene], outro=None)
        ctx.brief, ctx.target_seg, ctx.recipe, ctx.harness = dict(BRIEF), scene, recipe, True
        asyncio.run(pipeline._build_pattern_ref(ctx))
        asyncio.run(pipeline._omni_clip(ctx))
        return {"log": log, "stages": finished["extra"]["stages"], "patches": finished.get("patches", []),
                "work": work}

    return run


def _prompts(log) -> list[str]:
    return [e[2] for e in log if e[0] == "omni"]


def _restores(log) -> list:
    return [e for e in log if e[0] == "restore"]


def test_a_run_with_icons_tells_omni_to_draw_none_and_stamps_only_where_one_shows(icon_run, guard, monkeypatch):
    checked = []
    monkeypatch.setattr(stray, "find", lambda *a, **k: checked.append(k.get("footprint") is not None))
    default = icon_run(rc.normalize({**rc.preset_42_default("scene_0").model_dump(mode="json"),
                                     "seed": "studio:1:scene_0"}))
    got = icon_run(with_icons(CODE))
    assert _prompts(got["log"]) == [p + hprompt.ICONS_CLAUSE for p in _prompts(default["log"])], \
        "every prompt: the approved one, then ICONS_CLAUSE — nothing else moves"
    r0, r1 = _restores(got["log"])
    assert isinstance(r0[4], ir.Stamper) and r0[3] == ["clean", "clear_bars", "clear_veil_sec"]
    assert r1[4] is None and r1[3] == ["clear_bars", "clear_veil_sec"], "no icon in part 2: no stamps"
    assert [r[1] for r in (r0, r1)] == ["scene_1_c0.mp4", "scene_1_c1.mp4"], "the pass runs on Omni's clip"
    stages = got["stages"]
    assert sorted(stages) == ["guards", "icons", "omni", "target"]
    assert "icons" not in default["stages"], "the approved run records no icons stage"
    plan = stages["icons"]["plan"]
    assert plan["resolved"] == [CODE] and plan["planned"] is False, "the run stamps the frozen plan"
    c0, c1 = stages["icons"]["c0"], stages["icons"]["c1"]
    assert c0["stamped"] == ["Code"] and c1["stamped"] == [] and c0["span"] == [0.0, 4.0]
    assert c0["guards"] == {"clear_veil_sec": 3.0, "clear_bars": False}
    pre = Path(c0["files"]["prestamp"]["path"])
    assert pre.name == "scene_1_c0_prestamp.mp4" and pre.read_bytes() == b"fitted c0.mp4"
    assert Path(c0["files"]["clean"]["path"]).name == "scene_1_c0_clean.mp4"
    assert Path(c1["files"]["clean"]["path"]).name == "scene_1_c1_front.mp4", "no icon: the pass is its clean"
    assert c0["files"]["prestamp"]["key"] == "yas/rebrand/15/scene_1_c0_prestamp.mp4"
    assert "yas/rebrand/15/scene_1_c1_clean.mp4" in guard["uploads"]
    assert [stages["guards"][c]["speaker_front"] for c in ("c0", "c1")] == [True, True]
    assert got["patches"][-1]["warnings"] == [] and "warnings" not in default["patches"][-1]
    assert checked == [True, True], \
        "Omni's own marks looked for on each part, outside the pattern — with our icons only: the " \
        "approved run's brief asks Omni for icons of its own"


def test_omni_keeping_its_own_icons_is_the_approved_run_call_for_call(icon_run):
    d42 = rc.normalize({**rc.preset_42_default("scene_0").model_dump(mode="json"), "seed": "studio:1:scene_0"})
    default = icon_run(d42)
    kept = icon_run(with_icons(omni_own="keep"))
    assert kept["log"] == default["log"]
    assert _prompts(kept["log"]) == _prompts(default["log"])

    def untimed(stages: dict) -> dict:
        # How long the faked Omni call took is the clock's, not the run's.
        out = json.loads(json.dumps(stages))
        for rec in (out.get("omni") or {}).values():
            rec.pop("seconds", None)
        return out

    assert untimed(kept["stages"]) == untimed(json.loads(json.dumps(default["stages"]).replace(
        str(default["work"]).replace("\\", "\\\\"), str(kept["work"]).replace("\\", "\\\\"))))


def test_icons_on_a_scene_with_no_pattern_still_get_the_pass(icon_run, monkeypatch):
    monkeypatch.setattr(stray, "find", lambda *a, **k: None)
    bare = rc.normalize({"target": "scene_0", "seed": "studio:1:scene_0", "director": {"head": "harness"},
                         "elements": [{"id": "icons", "kind": "icon", "props": {"resolved": [CODE]}}]})
    got = icon_run(bare)
    assert all(p.endswith(hprompt.ICONS_CLAUSE) for p in _prompts(got["log"]))
    assert len(_restores(got["log"])) == 2, "the speaker pass runs with no pattern at all"
    assert got["stages"]["guards"]["c0"]["speaker_front"] is True


def test_a_failed_pass_lays_on_only_the_icons_the_speaker_never_reaches(icon_run, monkeypatch):
    monkeypatch.setattr(stray, "find", lambda *a, **k: None)
    laid: list = []

    def broken(edited, original, out, **kw):
        raise RuntimeError("MODNet cannot run here")

    def stamp_only(clip, out, stamps):
        laid.append([it.gkey for it in stamps.items])
        Path(out).write_bytes(b"stamped")
        return Path(out)

    monkeypatch.setattr(sf, "stamp_only", stamp_only)
    covers = _res("piggy-bank", frm=0.6, settle=0.8667, to=1.4, label="Free", max_overlap=0.2,
                  box={"x": 0.04, "y": 0.3, "w": 0.16, "h": 0.09})
    got = icon_run(with_icons(covers, CODE), restore=broken)
    assert laid == [["library:code"]], "the one the speaker reaches is left out; part 2 has none"
    assert got["stages"]["icons"]["c0"]["unmatted"] is True
    assert Path(got["stages"]["guards"]["c0"]["path"]).name == "scene_1_c0_stamped.mp4"
    assert Path(got["stages"]["guards"]["c1"]["path"]).name == "scene_1_c1.mp4", "Omni's clip, as always"


def test_an_icon_whose_drawing_is_gone_is_left_out_and_said(icon_run, monkeypatch):
    monkeypatch.setattr(stray, "find", lambda *a, **k: None)
    gone = rc.IconResolved.model_validate({**CODE, "glyph": {"source": "custom", "id": "c-0123456789ab"},
                                           "label": "Mine"}).model_dump(mode="json")
    got = icon_run(with_icons(gone))
    assert got["stages"]["icons"]["plan"]["resolved"] == []
    assert got["patches"][-1]["warnings"] == ["'Mine' couldn't be drawn, so it was left out"]
    assert all(r[4] is None for r in _restores(got["log"])), "nothing to stamp; the pass still runs"


def test_omni_drawing_its_own_symbol_is_a_version_warning(icon_run, monkeypatch):
    monkeypatch.setattr(stray, "find", lambda *a, **k: 1.25)
    got = icon_run(with_icons(CODE))
    assert got["patches"][-1]["warnings"] == [stray.warning(1.25), stray.warning(5.25)]
    assert got["patches"][-1]["warnings"][0].startswith("Omni drew its own symbol at 1.2 s — Try again")
    assert ["step", "Scene 1: Omni drew its own symbol", "failed"] in got["log"]


# ---------------------------------------------------------------------------
# The stray-drawing clean-up (guards.stray "clean"), and the check without it
# ---------------------------------------------------------------------------

def _d42(**guards) -> rc.Recipe:
    d = rc.preset_42_default("scene_0").model_dump(mode="json")
    return rc.normalize({**d, "seed": "studio:1:scene_0", "guards": {**d["guards"], **guards}})


def _harness(**guards) -> rc.Recipe:
    """The 42 default opened with the harness's own words: Omni is asked for
    the pattern only."""
    d = _d42(**guards).model_dump(mode="json")
    return rc.normalize({**d, "director": {**d["director"], "head": "harness"}})


def _clean(recipe: rc.Recipe) -> rc.Recipe:
    d = recipe.model_dump(mode="json")
    return rc.normalize({**d, "guards": {**d["guards"], "stray": "clean"}})


def test_the_approved_run_is_looked_at_by_nothing(icon_run, monkeypatch):
    """No icons of ours (the 42 default): its brief asks Omni for icons of
    its own, so what it adds beside the pattern is no stray — neither the
    check nor the clean-up runs, on or off, and the pass is the approved
    one, argument for argument."""
    monkeypatch.setattr(stray, "find", lambda *a, **k: pytest.fail("the approved run is not looked at"))
    for recipe in (_d42(), _clean(_d42())):
        got = icon_run(recipe)
        assert [r[3] for r in _restores(got["log"])] == [["clear_bars", "clear_veil_sec"]] * 2
        assert "warnings" not in got["patches"][-1]
        assert not any(e[0] == "step" and "own symbol" in e[1] for e in got["log"])
        assert "stray_clean" not in got["stages"]["guards"]["c0"]


def test_a_run_asking_for_the_pattern_only_says_when_omni_drew_its_own_symbol(icon_run, monkeypatch):
    """The harness's own opening, clean-up off: Omni's own marks are looked
    for, and said with the clean-up as the way to be rid of them — the pass
    itself as it would be."""
    seen: list = []
    monkeypatch.setattr(stray, "find", lambda *a, **k: seen.append(k) or 1.25)
    got = icon_run(_harness())
    assert len(seen) == 2 and all(k["footprint"] is not None and k["boxes"] is None for k in seen)
    assert [r[3] for r in _restores(got["log"])] == [["clear_bars", "clear_veil_sec"]] * 2
    assert got["patches"][-1]["warnings"] == [stray.warning(1.25), stray.warning(5.25)]
    assert got["patches"][-1]["warnings"][0].endswith("Try again, or Clean it up")
    assert ["step", "Scene 1: Omni drew its own symbol", "failed"] in got["log"]
    assert "stray_clean" not in got["stages"]["guards"]["c0"]


def test_the_clean_up_hands_the_pass_the_footprint_and_looks_for_nothing(icon_run, monkeypatch):
    def never(*a, **k):
        raise AssertionError("the check runs only with the clean-up off")

    monkeypatch.setattr(stray, "find", lambda *a, **k: None)
    default = icon_run(_harness())
    monkeypatch.setattr(stray, "find", never)
    passes: list = []

    def restore(edited, original, out, **kw):
        passes.append(kw)
        Path(out).write_bytes(b"front")
        return out

    got = icon_run(_clean(_harness()), restore=restore)
    assert _prompts(got["log"]) == _prompts(default["log"]), "nothing Omni is shown or told changes"
    assert [sorted(kw) for kw in passes] == [["clear_bars", "clear_veil_sec", "footprint"]] * 2
    for kw in passes:
        fp = kw["footprint"]
        assert fp.shape == (1280, 720) and fp.dtype == np.float32, "at the clip's size"
        assert fp.max() == 1.0 and fp.min() == 0.0 and 0.05 < fp.mean() < 0.95, "the pattern's, not the frame"
    assert [got["stages"]["guards"][c]["stray_clean"] for c in ("c0", "c1")] == [True, True]
    assert "warnings" not in got["patches"][-1]


def test_the_clean_up_with_icons_keeps_its_footprint_with_the_parts(icon_run, guard, monkeypatch):
    monkeypatch.setattr(stray, "find", lambda *a, **k: pytest.fail("no check with the clean-up on"))
    passes: list = []

    def restore(edited, original, out, **kw):
        passes.append(kw)
        Path(out).write_bytes(b"front")
        if kw.get("clean"):
            Path(kw["clean"]).write_bytes(b"clean")
        return out

    got = icon_run(_clean(with_icons(CODE)), restore=restore)
    assert all(kw["footprint"].shape == (1280, 720) for kw in passes) and len(passes) == 2
    for j in range(2):
        rec = got["stages"]["icons"][f"c{j}"]["files"]["footprint"]
        assert Path(rec["path"]).name == f"scene_1_c{j}_footprint.npy"
        assert np.array_equal(np.load(rec["path"]), passes[j]["footprint"]), "as the pass had it"
        assert rec["key"] == f"yas/rebrand/15/scene_1_c{j}_footprint.npy", "beside the version"
    assert got["stages"]["guards"]["c0"]["stray_clean"] is True


def test_the_footprint_takes_in_our_icons_boxes():
    code = ir.Stamper([CODE], fps=FPS, frame_size=(720, 1280))
    fp = runner.stray_footprint((720, 1280), [], _d42(), stamp=code)
    x0, y0, x1, y1 = runner.icon_boxes(code)[0]
    assert fp[y0:y1, x0:x1].min() == 1.0, "under the icon: Omni's pixel is kept (ours goes on top)"
    assert fp[:, x1 + 40:].max() == 0.0 and fp[y1 + 40:, :].max() == 0.0, "elsewhere: all back"
    assert runner.stray_footprint((720, 1280), [], _d42()).max() == 0.0, "nothing drawn: all back"


def test_omni_keeping_its_own_icons_is_never_cleaned_up(icon_run, monkeypatch):
    """Omni asked for icons of its own: they may be anywhere, so the
    clean-up would take them away — neither it nor the check runs."""
    checked: list = []
    monkeypatch.setattr(stray, "find", lambda *a, **k: checked.append(1))
    default = icon_run(_d42())
    kept = icon_run(_clean(with_icons(omni_own="keep")))
    harness = icon_run(_clean(_harness()))
    assert kept["log"] == default["log"] and checked == [], "neither was looked at"
    assert "stray_clean" not in kept["stages"]["guards"]["c0"]
    assert harness["stages"]["guards"]["c0"]["stray_clean"] is True, "while the pattern alone is cleaned"


# ---------------------------------------------------------------------------
# The stray check (synthetic)
# ---------------------------------------------------------------------------

def _textured(seed=1):
    rng = np.random.default_rng(seed)
    return cv2.GaussianBlur(rng.integers(60, 180, (H, W, 3)).astype(np.uint8), (0, 0), 3)


def test_a_symbol_omni_drew_is_found_and_the_rest_is_not(tmp_path):
    room = _textured()
    n = 60
    orig = _write(tmp_path / "o.mp4", [room] * n)

    def omni_frames(mark=None, noise=0):
        rng = np.random.default_rng(7)
        out = []
        for i in range(n):
            f = room.astype(np.int16) + (rng.normal(0, noise, room.shape) if noise else 0)
            f = np.clip(f, 0, 255).astype(np.uint8)
            if mark is not None and mark(i):
                cv2.circle(f, (110, 70), 14, (250, 250, 30), -1)
            out.append(f)
        return out

    nobody = lambda bgr: np.zeros(bgr.shape[:2], np.float32)  # noqa: E731
    drawn = _write(tmp_path / "d.mp4", omni_frames(lambda i: True))
    assert stray.find(drawn, orig, matte=nobody, skip_sec=0.2, tail_sec=0.1) == pytest.approx(0.2, abs=0.05)
    footprint = np.zeros((H, W), np.float32)
    footprint[50:90, 90:130] = 1.0
    assert stray.find(drawn, orig, footprint=footprint, matte=nobody, skip_sec=0.2, tail_sec=0.1) is None, \
        "inside what Omni was asked for"
    assert stray.find(drawn, orig, matte=nobody, skip_sec=0.2, tail_sec=0.1,
                      boxes=lambda t: [(90, 50, 130, 90)]) is None, "under one of our icons"
    once = _write(tmp_path / "once.mp4", omni_frames(lambda i: i == 6))
    assert stray.find(once, orig, matte=nobody, skip_sec=0.2, tail_sec=0.1) is None, "a flash is not a symbol"
    noisy = _write(tmp_path / "n.mp4", omni_frames(noise=6))
    assert stray.find(noisy, orig, matte=nobody, skip_sec=0.2, tail_sec=0.1) is None, "Omni's re-render"
    assert stray.warning(4.26) == "Omni drew its own symbol at 4.3 s — Try again, or Clean it up"


# ---------------------------------------------------------------------------
# The brief
# ---------------------------------------------------------------------------

@pytest.fixture
def brief_env(monkeypatch):
    from app.agents.producer import memory
    from app.services import yas_edit_recipes

    sent: dict = {"fail": False}

    async def embed(texts):
        return [[0.0] * 4]

    async def requirements():
        return {"primary_colors": ["violet"], "secondary_colors": ["white"]}

    async def empty(*_a, **_kw):
        return []

    async def multimodal(prompt, *, images, system, **_kw):
        sent.update(prompt=prompt, system=system, schema=_kw.get("json_schema"))
        if sent["fail"]:
            raise RuntimeError("model down")
        return {"omni_scene_prompt": "EDIT with the attached pattern."}

    monkeypatch.setattr(rebrand_brief, "gemini_embed", embed)
    monkeypatch.setattr(rebrand_brief, "gemini_multimodal", multimodal)
    monkeypatch.setattr(rebrand_brief, "_ig_rows", empty)
    monkeypatch.setattr(rebrand_brief, "_ig_frames", empty)
    monkeypatch.setattr(store, "past_lessons", empty)
    monkeypatch.setattr(memory, "build_brand_requirements", requirements)
    monkeypatch.setattr(yas_edit_recipes, "nearest_recipes", empty)
    return sent


def _options(recipe: rc.Recipe) -> dict:
    implied = rc.options_from_recipe(recipe)
    options = pipeline.normalize_options(implied)
    if implied.get("icons_by_pipeline"):
        options["icons_by_pipeline"] = True
    return options


def test_the_brief_is_told_the_pipeline_places_the_icons_only_when_it_does(brief_env):
    old = ("INTENSITY: balanced (subtle = backdrop + 1-2 icons, barely moving; balanced = clear branded "
           "set + 2-3 icons; bold = strong graphic set, 3-4 animated icons).\n")

    def prompt(recipe):
        asyncio.run(rebrand_brief.build_brief(exp_id=7, run_ctx={"topic": "t"}, frames={}, logo=None,
                                              options=_options(recipe)))
        return brief_env["prompt"]

    plain = prompt(rc.preset_42_default("scene_0"))
    assert old in plain and rebrand_brief.ICONS_BY_PIPELINE not in plain
    assert prompt(with_icons(omni_own="keep")) == plain, "Omni keeps its own: the brief as it was"
    told = prompt(with_icons())
    assert rebrand_brief.ICONS_BY_PIPELINE in told and old not in told
    assert "INTENSITY: balanced (subtle = quiet graphics, barely moving; balanced = a clear branded set; " \
           "bold = a strong graphic set).\n" in told
    assert told.replace(told[told.index("INTENSITY"):told.index("Write the rebrand brief")], "") == \
        plain.replace(plain[plain.index("INTENSITY"):plain.index("Write the rebrand brief")], ""), \
        "nothing else in the brief's prompt moves"
    # Its own words never ask for icons either (the 42 default's are as they were).
    prompt(rc.preset_42_default("scene_0"))
    assert brief_env["system"] is rebrand_brief.BRIEF_SYSTEM and brief_env["schema"] is rebrand_brief.BRIEF_SCHEMA
    prompt(with_icons())
    system, schema = brief_env["system"], brief_env["schema"]
    assert system is rebrand_brief.BRIEF_SYSTEM_NO_ICONS and schema is rebrand_brief.BRIEF_SCHEMA_NO_ICONS
    assert "icon" not in system.lower() and "icons' backing" not in schema["properties"]["omni_scene_prompt"]["description"]
    assert system.replace(" ", "") != rebrand_brief.BRIEF_SYSTEM.replace(" ", "")
    assert len(rebrand_brief.BRIEF_SYSTEM) - len(system) < 120, "only the icon sentences go"
    assert schema["required"] == rebrand_brief.BRIEF_SCHEMA["required"]


def test_the_fallback_brief_asks_omni_for_no_icons_when_ours_are_stamped(brief_env, tmp_path):
    from app.services import rebrand_pattern_ref as pattern_ref

    ref = pattern_ref.build(INTRO, tmp_path / "p", aspect="9:16")
    brief_env["fail"] = True
    ours, _ = asyncio.run(rebrand_brief.build_brief(exp_id=7, run_ctx={"topic": "t"}, frames={}, logo=None,
                                                    options=_options(with_icons()), pattern_ref=ref))
    theirs, _ = asyncio.run(rebrand_brief.build_brief(exp_id=7, run_ctx={"topic": "t"}, frames={}, logo=None,
                                                      options=_options(rc.preset_42_default("scene_0")),
                                                      pattern_ref=ref))
    assert ours["icons"] == [] and "icons" not in ours["omni_scene_prompt"]
    assert theirs["icons"] and "the icons on the background beside the speaker" in theirs["omni_scene_prompt"]
    assert theirs == rebrand_brief._fallback_brief({"primary_colors": ["violet"], "secondary_colors": ["white"]},
                                                   {"topic": "t"}, ref)


# ---------------------------------------------------------------------------
# A finished version with our icons: built on from its clean parts
# ---------------------------------------------------------------------------

def _icon_version(tmp_path: Path, vid: int = 81, *, parts: int = 2, files: bool = True,
                  stamps_on: tuple = (0,)) -> dict:
    """A version whose run stamped our icons: per part its pre-stamp and
    clean clips, the veil's keep mask on part 1, the icons stage."""
    run_dir = tmp_path / "rb" / str(vid) / "omni"
    run_dir.mkdir(parents=True, exist_ok=True)
    icons, guards = {"plan": {"resolved": [CODE], "planned": False}}, {}
    for j in range(parts):
        pre, clean, front = (run_dir / f"scene_1_c{j}_{k}.mp4" for k in ("prestamp", "clean", "front"))
        rec_files = {"prestamp": {"path": str(pre)}, "clean": {"path": str(clean)}}
        if files:
            pre.write_bytes(b"prestamp-" + str(j).encode())
            clean.write_bytes(b"clean-" + str(j).encode())
            front.write_bytes(b"front-" + str(j).encode())
        if j == 0:
            keep = run_dir / "scene_1_c0_veilkeep.npy"
            np.save(keep, np.full((4, 4), 0.5, np.float32))
            rec_files["veil_keep"] = {"path": str(keep)}
        icons[f"c{j}"] = {"span": [4.0 * j, 4.0], "stamped": ["Code"] if j in stamps_on else [],
                          "guards": {"clear_veil_sec": 3.0 if j == 0 else 0.0, "clear_bars": False},
                          "files": rec_files}
        guards[f"c{j}"] = {"path": str(front)}
    recipe = with_icons(CODE)
    final = tmp_path / "final.mp4"
    final.write_bytes(b"final")
    return {"id": vid, "run_id": 1, "status": "ready", "stage": "Done",
            "options": {"target": "scene_0", "recipe": recipe.model_dump(mode="json"), "icons_by_pipeline": True},
            "brief": dict(BRIEF), "evidence": {},
            "arms": {"omni": {"status": "ready", "final_path": str(final),
                              "stages": {"omni": {f"c{j}": {"entering": j == 0} for j in range(parts)},
                                         "guards": guards, "icons": icons}}}}


def test_a_version_with_icons_is_made_again_from_its_own_pass(tmp_path, monkeypatch, guard):
    monkeypatch.setattr(pipeline, "REBRAND_ROOT", tmp_path / "rb")
    exp = _icon_version(tmp_path)
    assert hbase.has_icons(exp) and hbase.icons_of(exp) == [CODE] and hbase.sources_known(exp)
    kept = asyncio.run(hbase.replays(exp, tmp_path / "kept"))
    assert [r.edited.read_bytes() for r in kept] == [b"prestamp-0", b"prestamp-1"]
    assert [r.clean.read_bytes() for r in kept] == [b"clean-0", b"clean-1"]
    kw = kept[0].kwargs()
    assert kw["clear_veil_sec"] == 3.0 and kw["clear_bars"] is False and kw["veil_keep"].shape == (4, 4)
    assert "veil_keep" not in kept[1].kwargs() and "backdrop" not in kept[1].kwargs()
    # Gone from disk: fetched from the bucket, where the run put it.
    Path(exp["arms"]["omni"]["stages"]["icons"]["c1"]["files"]["clean"]["path"]).unlink()
    exp["arms"]["omni"]["stages"]["icons"]["c1"]["files"]["clean"].update(bucket="b", key="k1")

    async def download(*, storage_bucket, storage_path):
        return b"from-the-bucket"

    monkeypatch.setattr(asset_storage, "download_bytes", download)
    again = asyncio.run(hbase.replays(exp, tmp_path / "kept2"))
    assert again[1].clean.read_bytes() == b"from-the-bucket" and again[1].clean.parent == tmp_path / "kept2"


def test_a_version_with_icons_is_never_cut_from_its_final(tmp_path, monkeypatch):
    """Its final has the icons baked in: with its parts gone it cannot be
    added to (Change it still works) — no cut from the final."""
    monkeypatch.setattr(pipeline, "REBRAND_ROOT", tmp_path / "rb")
    exp = _icon_version(tmp_path, files=False)
    assert not hbase.sources_known(exp), "its final does not count"
    bv = hbase.BaseVersion(spec=hbase.BaseSpec(81, "stack"), exp=exp, chain=[exp], layers=[])
    cut: list = []
    monkeypatch.setattr(hbase, "_cut", lambda *a, **k: cut.append(a))
    with pytest.raises(HTTPException) as err:
        asyncio.run(hbase.stack_parts(bv, [tmp_path / "c0.mp4", tmp_path / "c1.mp4"], tmp_path / "on_v81",
                                      width=W, height=H))
    assert err.value.status_code == 410 and err.value.detail == hbase.GONE and cut == []


def test_new_layers_on_a_version_with_icons_are_drawn_on_its_clean_parts(tmp_path, monkeypatch):
    monkeypatch.setattr(pipeline, "REBRAND_ROOT", tmp_path / "rb")
    monkeypatch.setattr(sf, "person_matte", _red)
    exp = _icon_version(tmp_path, parts=1)
    clean = _write(tmp_path / "clean.mp4", [_room(i) for i in range(20)])
    exp["arms"]["omni"]["stages"]["icons"]["c0"]["files"]["clean"]["path"] = str(clean)
    orig = _write(tmp_path / "orig.mp4", [_room(i) for i in range(21)], audio_sec=21 / FPS)
    conformed: list = []
    real = media.conform

    def conform(src, like, out, **kw):
        conformed.append(Path(src))
        return real(src, like, out, **kw)

    monkeypatch.setattr(media, "conform", conform)
    bv = hbase.BaseVersion(spec=hbase.BaseSpec(81, "stack"), exp=exp, chain=[exp], layers=[])
    asyncio.run(hbase.stack_parts(bv, [orig], tmp_path / "on_v81", width=W, height=H))
    assert conformed == [clean], "its clean part, never its final one with the icons in it"
    assert media.frame_count(bv.sources[0]) == 21 and bv.replays[0].edited.read_bytes() == b"prestamp-0"


# ---------------------------------------------------------------------------
# An icon-only change on a finished version: its own pass again, no Omni
# ---------------------------------------------------------------------------

@pytest.fixture
def restamp(tmp_path, monkeypatch):
    monkeypatch.setattr(pipeline, "REBRAND_ROOT", tmp_path / "rb")
    parent = _icon_version(tmp_path)
    restores: list = []

    async def never(*a, **k):
        raise AssertionError("Omni was called for an icon-only change")

    def restore(edited, original, out, **kw):
        restores.append({"edited": Path(edited), "original": Path(original), **kw})
        Path(out).write_bytes(b"restamped")
        return out

    monkeypatch.setattr(omni, "edit_video", never)
    monkeypatch.setattr(sf, "restore_speaker", restore)
    monkeypatch.setattr(media, "concat", lambda parts, out: out)
    monkeypatch.setattr(store, "append_arm_step", _quiet)
    patches: list = []

    async def patch_arm(exp_id, arm, patch):
        patches.append(patch)

    monkeypatch.setattr(store, "patch_arm", patch_arm)
    monkeypatch.setattr(runner, "_spans", lambda chunks: [(0.0, 4.0), (4.0, 4.0)])
    chunks, sources, frames = [], [], []
    for j in range(2):
        chunks.append(tmp_path / f"chunk{j}.mp4")
        chunks[-1].write_bytes(b"orig-" + str(j).encode())
        sources.append(tmp_path / f"c{j}_src.mp4")
        sources[-1].write_bytes(b"clean-conformed-" + str(j).encode())
        frames.append(tmp_path / f"c{j}_first.png")
        shutil.copyfile(FRAME, frames[-1])
    bv = hbase.BaseVersion(spec=hbase.BaseSpec(81, "stack"), exp=parent, chain=[parent], layers=[],
                           chunks=chunks, sources=sources, frames=frames,
                           replays=asyncio.run(hbase.replays(parent, tmp_path / "kept")))
    seg = pipeline.Segment(kind="scene", index=0, source=chunks[0], is_still=False)
    ctx = pipeline.Ctx(exp_id=82, run_id=1, aspect="9:16", width=720, height=1280, work=tmp_path,
                       options={"target": "scene_0"}, run_ctx={}, intro=None, scenes=[seg], outro=None)
    ctx.brief, ctx.base = dict(BRIEF), bv

    def run(recipe: rc.Recipe, out_dir: str = "v2") -> dict:
        ctx.recipe = studio.on_top(recipe)
        m = st.Manifest()
        asyncio.run(runner.edit_scene(ctx, "omni", seg, tmp_path / out_dir, "Scene 1", m))
        return m.public()

    return {"run": run, "restores": restores, "bv": bv, "parent": parent, "patches": patches, "tmp": tmp_path}


def _icons_only(*resolved) -> rc.Recipe:
    return rc.normalize({"target": "scene_0", "seed": "studio:1:scene_0",
                         "elements": [{"id": "icons", "kind": "icon", "props": {"resolved": list(resolved)}}]})


def test_an_icon_only_change_on_a_version_calls_omni_zero_times(restamp):
    free = _res("piggy-bank", frm=5.0, settle=5.2667, to=6.2, label="Free",
                box={"x": 0.04, "y": 0.3, "w": 0.16, "h": 0.09})
    stages = restamp["run"](_icons_only(free))
    bv, restores = restamp["bv"], restamp["restores"]
    assert len(restores) == 1, "only the part an icon shows in is made again"
    [r] = restores
    assert r["edited"] == bv.replays[1].edited and r["original"] == bv.chunks[1], \
        "its own pass: its pre-stamp part against the TRUE original"
    assert isinstance(r["stamps"], ir.Stamper) and r["clear_veil_sec"] == 0.0 and r["clear_bars"] is False
    assert "clean" not in r, "the version's clean part stays its own"
    assert stages["omni"]["c1"]["restamped"] is True and stages["omni"]["c1"]["reused"] is True
    assert Path(stages["guards"]["c0"]["path"]).read_bytes() == b"clean-conformed-0", \
        "no icon in part 1 now: the version's clean part"
    def paths(files):
        return {k: v["path"] for k, v in files.items()}

    assert paths(stages["icons"]["c1"]["files"]) == paths(bv.replays[1].files), "how it is made again, handed on"
    assert paths(stages["icons"]["c0"]["files"]) == paths(bv.replays[0].files)
    assert stages["icons"]["c0"]["stamped"] == []
    # ... and in the bucket, beside this version (a redeploy can wipe the disk).
    assert all(v.get("bucket") for j in ("c0", "c1") for v in stages["icons"][j]["files"].values())
    assert not any(v.get("bucket") for r in bv.replays for v in r.files.values()), "the version's own left as they are"
    assert stages["icons"]["plan"]["resolved"] == [free]
    assert "target" not in stages


def test_an_icon_only_change_replays_the_versions_guards(restamp):
    stages = restamp["run"](_icons_only(CODE), "v3")
    [r] = restamp["restores"]
    assert r["edited"] == restamp["bv"].replays[0].edited and r["clear_veil_sec"] == 3.0
    assert np.array_equal(r["veil_keep"], np.full((4, 4), 0.5, np.float32)), "its own veil mask"
    assert stages["guards"]["c0"]["restamped"] is True


def test_new_layers_on_a_version_with_icons_stamp_its_icons_again(restamp, monkeypatch):
    """A pattern added on top, no icon element: the version's own icons are
    stamped again in the new pass (its parts are its clean ones) and Omni is
    told to draw none."""
    from app.services import pattern_library as lib
    from app.services import rebrand_pattern_ref as pattern_ref

    calls: list = []

    async def edit_video(video, prompt, *, aspect, out, images=None):
        calls.append((Path(video), prompt))
        Path(out).write_bytes(b"omni")
        return {"path": out}

    async def person_mask(frame):
        return _person()

    monkeypatch.setattr(omni, "edit_video", edit_video)
    monkeypatch.setattr(person_matte, "person_mask", person_mask)
    def fit(raw, chunk, out, **kw):
        Path(out).write_bytes(b"fit")
        return out

    monkeypatch.setattr(media, "fit_video_to_audio", fit)
    colours = pattern_ref.Colours(ground="#3d3c3d", ink="#f4f4f8", fell_back=False, frame=None, colour="#7f2bf2")
    ref = pattern_ref.build(None, restamp["tmp"] / "p", aspect="9:16", slug="beads_row",
                            layout=lib.BEADS_ROW_CANDIDATES[3], animation="none", colours=colours)
    recipe = rc.normalize({"target": "scene_0", "seed": "studio:1:scene_0", "director": {"head": "harness"},
                           "elements": [{"id": "pattern", "kind": "pattern",
                                         "props": {"slug": "beads_row", "layout": None, "colour": "auto"}}]})
    ctx_ref = restamp["bv"]
    monkeypatch.setattr(runner, "pattern_refs", lambda ctx: [ref])
    stages = restamp["run"](recipe, "v4")
    assert [c[0] for c in calls] == ctx_ref.sources, "Omni edits the version's clean parts"
    assert all(c[1].endswith(hprompt.ICONS_CLAUSE) for c in calls)
    stamped = [r for r in restamp["restores"] if r.get("stamps") is not None]
    assert len(stamped) == 1 and stamped[0]["backdrop"] == ctx_ref.sources[0], "its icons, again, on part 1"
    assert stages["icons"]["plan"]["inherited"] is True and stages["icons"]["plan"]["resolved"] == [CODE]
    assert Path(stages["icons"]["c0"]["files"]["hold"]["path"]).suffix == ".npy"
    assert all("footprint" not in r for r in restamp["restores"]), "no clean-up asked for: none"

    # The same with the clean-up on: a pass on top has none of its own —
    # outside the new layers the hold already gives the version back, and
    # the version's own strays are "Change it"'s to clean.
    restamp["restores"].clear()
    stages = restamp["run"](_clean(recipe), "v4c")
    assert len(restamp["restores"]) == 2
    for r in restamp["restores"]:
        assert "footprint" not in r and "hold" in r and r["backdrop"] in ctx_ref.sources
    assert "footprint" not in stages["icons"]["c0"]["files"]
    assert "stray_clean" not in stages["guards"]["c0"]


def test_an_icon_only_change_keeps_the_versions_clean_up(restamp):
    """A version made with the clean-up, given other icons: its own pass
    again, clean-up and all — what it took away stays away."""
    rp = restamp["bv"].replays[0]
    fp = restamp["tmp"] / "c0_footprint.npy"
    np.save(fp, np.full((4, 4), 0.75, np.float32))
    rp.footprint = fp
    rp.files["footprint"] = {"path": str(fp)}
    stages = restamp["run"](_icons_only(CODE), "v6")
    [r] = restamp["restores"]
    assert np.array_equal(r["footprint"], np.full((4, 4), 0.75, np.float32)), "its own footprint"
    assert stages["guards"]["c0"]["stray_clean"] is True
    assert stages["icons"]["c0"]["files"]["footprint"]["path"] == str(fp), "handed on"


def _icons_off(*extra: dict) -> rc.Recipe:
    return rc.normalize({"target": "scene_0", "seed": "studio:1:scene_0", "director": {"head": "harness"},
                         "elements": [*extra, {"id": "icons", "kind": "icon", "enabled": False}]})


def test_icons_turned_off_on_a_version_take_its_icons_away_with_no_omni(restamp):
    """"Turn icons off" in Add to it: the version's clean parts, as they
    are — no Omni call, no speaker pass, no icons."""
    stages = restamp["run"](_icons_off(), "v5")
    assert restamp["restores"] == [], "nothing is made again: its parts without the icons"
    for j in range(2):
        assert Path(stages["guards"][f"c{j}"]["path"]).read_bytes() == f"clean-conformed-{j}".encode()
        assert stages["omni"][f"c{j}"]["kept"] is True
    assert "icons" not in stages and "target" not in stages
    assert "warnings" not in restamp["patches"][-1]


def test_new_layers_with_icons_turned_off_stamp_none(restamp, monkeypatch):
    """A pattern added on top with the icon element switched off: the
    version's icons are not stamped again, and Omni is not told about them."""
    from app.services import pattern_library as lib
    from app.services import rebrand_pattern_ref as pattern_ref

    prompts: list = []

    async def edit_video(video, prompt, *, aspect, out, images=None):
        prompts.append(prompt)
        Path(out).write_bytes(b"omni")
        return {"path": out}

    async def person_mask(frame):
        return _person()

    def fit(raw, chunk, out, **kw):
        Path(out).write_bytes(b"fit")
        return out

    monkeypatch.setattr(omni, "edit_video", edit_video)
    monkeypatch.setattr(person_matte, "person_mask", person_mask)
    monkeypatch.setattr(media, "fit_video_to_audio", fit)
    colours = pattern_ref.Colours(ground="#3d3c3d", ink="#f4f4f8", fell_back=False, frame=None, colour="#7f2bf2")
    ref = pattern_ref.build(None, restamp["tmp"] / "p", aspect="9:16", slug="beads_row",
                            layout=lib.BEADS_ROW_CANDIDATES[3], animation="none", colours=colours)
    monkeypatch.setattr(runner, "pattern_refs", lambda ctx: [ref])
    stages = restamp["run"](_icons_off({"id": "pattern", "kind": "pattern",
                                        "props": {"slug": "beads_row", "layout": None, "colour": "auto"}}), "v7")
    assert prompts and not any(p.endswith(hprompt.ICONS_CLAUSE) for p in prompts)
    assert all(r.get("stamps") is None for r in restamp["restores"]) and "icons" not in stages


def test_icons_on_a_version_without_ours_keep_its_parts_in_the_bucket(restamp, guard):
    """Icons added on top of a version without ours: its parts are how the
    next pass makes it again (their pre-stamp and clean), so they go up to
    the bucket beside the new version — one upload for the one file."""
    import copy

    bv = restamp["bv"]
    exp = copy.deepcopy(bv.exp)
    exp["arms"]["omni"]["stages"].pop("icons")
    bv.exp, bv.chain = exp, [exp]
    bv.replays = [hbase.replay_of_source(s) for s in bv.sources]
    free = _res("piggy-bank", frm=5.0, settle=5.2667, to=6.2, label="Free",
                box={"x": 0.04, "y": 0.3, "w": 0.16, "h": 0.09})
    stages = restamp["run"](_icons_only(free), "v6")
    for j in (0, 1):
        files = stages["icons"][f"c{j}"]["files"]
        assert files["prestamp"]["path"] == str(bv.sources[j]) and files["prestamp"]["bucket"]
        assert files["clean"]["key"] == files["prestamp"]["key"], "one file, sent up once"
    assert sorted(k for k in guard["uploads"] if k.startswith("yas/rebrand/82/")) == \
        ["yas/rebrand/82/scene_1_c0_prestamp.mp4", "yas/rebrand/82/scene_1_c1_prestamp.mp4"]


def test_an_icon_the_library_no_longer_has_is_left_out_of_the_run():
    gone = _res("gone-in-a-rebake", label="Gone")
    keep, lost = asyncio.run(runner._loadable([CODE, gone]))
    assert keep == [CODE] and lost == ["Gone"]


def test_a_brief_opening_that_asks_omni_for_icons_gives_way_when_ours_are_stamped():
    asks = {"omni_scene_prompt": "EDIT the clip. Lay the attached pattern; add two small icons beside the speaker."}
    no = {"omni_scene_prompt": "EDIT the clip. Lay the attached pattern. Add no icons, symbols or badges."}
    ours = with_icons()
    assert ours.director.head == "brief"
    got, why = runner.run_brief(ours, asks, [])
    assert why and "icons" in why
    assert got["omni_scene_prompt"] == hprompt.harness_head(ours.director.intensity, ours.director.notes)
    assert asks["omni_scene_prompt"].startswith("EDIT the clip. Lay"), "the stored brief is kept as written"
    assert runner.run_brief(ours, no, []) == (no, None), "a 'no' is not an ask"
    # Without ours (the 42 default, or Omni keeping its own): as it always was.
    assert runner.run_brief(rc.preset_42_default("scene_0"), asks, []) == (asks, None)
    assert runner.run_brief(with_icons(omni_own="keep"), asks, []) == (asks, None)
    # And the version it made is not taken for one with Omni's own icons.
    v = {"options": {"recipe": ours.model_dump(mode="json")}, "evidence": {"brief_head_fallback": why}}
    assert not hbase.omni_drew_icons(v)


# ---------------------------------------------------------------------------
# Omni's own icons: kept only where they are, never with ours on top
# ---------------------------------------------------------------------------

def _omni_icons_version(vid: int = 91) -> dict:
    """A 42-default version made before our icons: its brief asked Omni for its own."""
    return {"id": vid, "run_id": 1, "status": "ready", "options": {
        "target": "scene_0", "recipe": rc.preset_42_default("scene_0").model_dump(mode="json")},
        "brief": dict(BRIEF), "evidence": {}, "arms": {"omni": {"status": "ready", "stages": {}}}}


def test_which_versions_show_omnis_own_icons():
    v = _omni_icons_version()
    assert hbase.omni_drew_icons(v), "the brief's opening asks for a few"
    ours = {**v, "options": {**v["options"], "icons_by_pipeline": True}}
    assert not hbase.omni_drew_icons(ours)
    harness = {**v, "options": {"target": "scene_0", "recipe": rc.normalize(
        {**rc.preset_42_default("scene_0").model_dump(mode="json"), "director": {"head": "harness"}}
    ).model_dump(mode="json")}}
    assert not hbase.omni_drew_icons(harness), "our own opening asks for none"
    assert hbase.omni_icons_in([harness, v]), "one under it has them"

    def bv(exp, mode="stack"):
        return hbase.BaseVersion(spec=hbase.BaseSpec(exp["id"], mode), exp=exp, chain=[exp], layers=[])

    assert hbase.icon_refusal(with_icons(omni_own="keep"), None) == (422, hbase.KEEP_ONLY_ON_OMNI)
    assert hbase.icon_refusal(with_icons(omni_own="keep"), bv(v)) is None, "kept where they are"
    assert hbase.icon_refusal(_icons_only(), bv(v)) == (409, hbase.OMNI_ICONS), "never two sets"
    assert hbase.icon_refusal(with_icons(), bv(v, "rebuild")) is None, "Change it redoes it without them"
    assert hbase.icon_refusal(rc.preset_42_default("scene_0"), bv(v)) is None


def test_start_takes_an_icon_only_pass_and_refuses_two_sets_of_icons(monkeypatch, tmp_path):
    from app.services import pattern_library as lib

    monkeypatch.setattr(pipeline, "REBRAND_ROOT", tmp_path / "rb")
    ours = _icon_version(tmp_path)
    theirs = _omni_icons_version()
    theirs["arms"]["omni"]["final_path"] = str(tmp_path / "final.mp4")
    rows = {81: ours, 91: theirs}

    async def get(exp_id):
        return rows.get(int(exp_id))

    async def run_row(run_id):
        return {"metadata": {}}

    async def none(*a, **k):
        return []

    async def pattern(slug):
        return lib.BUILTINS.get(slug)

    created: dict = {}

    async def create(run_id, options):
        created.clear()
        created.update(options)
        return {"id": 99, "run_id": run_id, "options": options}

    monkeypatch.setattr(store, "get_experiment", get)
    monkeypatch.setattr(pipeline.th, "_fetch_run_row", run_row)
    monkeypatch.setattr(pipeline.th, "_abs_from_rel", lambda rel: Path(rel))
    monkeypatch.setattr(omni, "providers", lambda: ["vertex"])
    monkeypatch.setattr(lib, "get", pattern)
    monkeypatch.setattr(lib, "lookup", pattern)
    monkeypatch.setattr(store, "list_for_run", none)
    monkeypatch.setattr(store, "create_experiment", create)
    monkeypatch.setattr(pipeline, "_run", _quiet)

    def start(recipe, base):
        async def go():
            exp = await pipeline.start(1, None, recipe=recipe, base=base)
            await asyncio.sleep(0)
            return exp
        return asyncio.run(go())

    start(_icons_only(CODE), {"version_id": 81, "mode": "stack"})
    assert created["base"] == {"version_id": 81, "mode": "stack"} and created["icons_by_pipeline"] is True
    with pytest.raises(HTTPException) as err:
        start(_icons_only(CODE), {"version_id": 91, "mode": "stack"})
    assert err.value.status_code == 409 and err.value.detail == hbase.OMNI_ICONS
    with pytest.raises(HTTPException) as err:
        start(with_icons(omni_own="keep"), None)
    assert err.value.status_code == 422
    start(with_icons(omni_own="keep"), {"version_id": 91, "mode": "rebuild"})
    assert "icons_by_pipeline" not in created, "kept: Omni is asked for its own again, as before"
    start(with_icons(), {"version_id": 91, "mode": "rebuild"})
    assert created["icons_by_pipeline"] is True, "Change it: redone without them"
    keep_only = rc.normalize({"target": "scene_0", "director": {"head": "harness"},
                              "elements": [{"id": "icons", "kind": "icon", "props": {"omni_own": "keep"}}]})
    with pytest.raises(HTTPException) as err:
        start(keep_only, {"version_id": 91, "mode": "stack"})
    assert err.value.status_code == 400, "keeping theirs adds nothing on top"
    # "Turn icons off" in Add to it: takes our icons off a version with them
    # (no Omni) — and on one without them it adds nothing.
    start(_icons_off(), {"version_id": 81, "mode": "stack"})
    assert created["base"] == {"version_id": 81, "mode": "stack"} and "icons_by_pipeline" not in created
    with pytest.raises(HTTPException) as err:
        start(_icons_off(), {"version_id": 91, "mode": "stack"})
    assert err.value.status_code == 400


# ---------------------------------------------------------------------------
# The studio and the routes
# ---------------------------------------------------------------------------

COLOURS = {"ground": "#3d3c3d", "ink": "#f4f4f8", "fell_back": False, "frame": None, "colour": "#2ed8e8"}


@pytest.fixture
def scene_env(tmp_path, monkeypatch):
    """A prepared one-part studio scene: a real 20-frame clip of the golden
    frame (720x1280), its colours on disk, the local mask and the matte
    faked to the golden speaker mask."""
    monkeypatch.setattr(studio, "STUDIO_ROOT", tmp_path / "studio")
    monkeypatch.setattr(pipeline, "REBRAND_ROOT", tmp_path / "rb")
    for name in ("_PREPARED", "_MASK_TASKS", "_PREVIEWS", "_BASES", "_STACKS", "_RUN_MASKS", "_COLOUR_TASKS",
                 "_ICON_TASKS"):
        monkeypatch.setattr(studio, name, {})
    # A preview of a draft with icons starts their inputs (Whisper, the
    # track, the director) — not here: the plan is written by hand.
    monkeypatch.setattr(studio, "_icon_warm", lambda scene, recipe: None)
    person = _person()

    async def local_mask(frame):
        return person

    monkeypatch.setattr(person_matte, "local_mask", local_mask)
    monkeypatch.setattr(sf, "person_matte", lambda bgr: person)
    work = tmp_path / "studio" / "1" / "scene_0_abc"
    work.mkdir(parents=True)
    still = cv2.imread(str(FRAME))
    clip = _write(work / "c0.mp4", [still] * 20)
    first = work / "c0_first.png"
    shutil.copyfile(FRAME, first)
    (work / "colours.json").write_text(json.dumps(COLOURS), encoding="utf-8")
    intro = pipeline.Segment(kind="intro", index=0, source=INTRO, is_still=False)
    seg = pipeline.Segment(kind="scene", index=0, source=clip, is_still=False)
    seg.base = clip
    ctx = pipeline.Ctx(exp_id=0, run_id=1, aspect="9:16", width=720, height=1280, work=work, options={},
                       run_ctx={}, intro=intro, scenes=[seg], outro=None)
    ctx.target_seg = seg
    ctx.frames["th_first"] = first
    scene = studio.Scene(run_id=1, target="scene_0", aspect="9:16", width=720, height=1280, work=work, ctx=ctx,
                         chunks=[clip], firsts=[first], seconds=[0.67])
    studio._PREPARED[(1, "scene_0")] = scene
    return {"scene": scene, "still": still, "person": person, "tmp": tmp_path}


def _keep(scene, key: str, resolved: list) -> None:
    studio._write(studio._icon_dir(scene) / "plans" / f"{key}.json", {"plan": {"plan_key": key}, "resolved": resolved})


def test_the_exact_still_on_the_scene_has_the_icon_behind_the_speaker(scene_env):
    recipe = with_icons()
    asyncio.run(studio.preview(1, "scene_0", recipe))
    out = scene_env["scene"].work / "preview" / studio._preview_key(recipe, recipe.seed)
    layers = np.asarray(Image.open(out / studio.LAYERS_PNG))
    assert layers.shape == (1280, 720, 4) and layers[:, :, 3].max() == 255, "the patterns, uncut"
    assert layers[:, :, 3][scene_env["person"] > 0.99].max() == 255, "even where the speaker stands"
    icon = _res(frm=0.0, settle=0.1, to=0.66, entrance="none", box={"x": 0.3, "y": 0.2, "w": 0.4, "h": 0.25})
    _keep(scene_env["scene"], "a" * 16, [icon])
    _keep(scene_env["scene"], "e" * 16, [])

    def still(key):
        body = asyncio.run(studio.icon_moment(1, "scene_0", recipe, key, 0.3))
        return body, cv2.imread(str(studio.resolve_file(body["url"].split("/file/", 1)[1])))

    body, got = still("a" * 16)
    _none, bare = still("e" * 16)
    assert body["frame"] == 9 and body["t"] == pytest.approx(0.3)
    shot = _read_all(scene_env["scene"].chunks[0])[9]
    speaker = scene_env["person"] == 1.0
    assert np.array_equal(got[speaker], shot[speaker]), "the speaker in front, as shot"
    assert (bare[~speaker] != shot[~speaker]).any(), "the patterns laid under them"
    stamps = ir.Stamper([icon], fps=FPS, frame_size=(720, 1280))
    x0, y0, x1, y1 = stamps.boxes(0.3)[0]
    box = np.zeros((1280, 720), bool)
    box[y0:y1, x0:x1] = True
    diff = (got != bare).any(axis=2)
    assert diff.any() and not (diff & ~box).any(), "the icon, and nothing else, beside the speaker"
    again = asyncio.run(studio.icon_moment(1, "scene_0", recipe, "a" * 16, 0.3))
    assert again == body, "kept"
    with pytest.raises(HTTPException) as err:
        asyncio.run(studio.icon_moment(1, "scene_0", recipe, "b" * 16, 0.3))
    assert err.value.status_code == 404


def test_the_exact_still_on_a_version_is_the_icon_only_passs_frame(scene_env, tail_clips, capture, monkeypatch):
    """"/icons/moment on a version" and the icon-only pass on it: the same
    frame, array for array — to the last two of 239 (the matte's tail)."""
    c = tail_clips
    monkeypatch.setattr(sf, "person_matte", _red)
    scene = scene_env["scene"]
    scene.chunks, scene.seconds, scene.width, scene.height = [c["orig"]], [7.97], W, H
    keep = c["tmp"] / "veilkeep.npy"
    np.save(keep, c["keep"])
    rp = hbase.Replay(edited=c["edited"], clean=c["edited"], clear_veil_sec=0.2, clear_bars=False, veil_keep=keep,
                      files={})
    bv = hbase.BaseVersion(spec=hbase.BaseSpec(70, "stack"), exp={"id": 70}, chain=[], layers=[],
                           chunks=[c["orig"]], sources=[c["edited"]], frames=[], replays=[rp])

    async def prepare_base(run_id, target, version_id):
        assert version_id == 70
        return bv

    monkeypatch.setattr(studio, "prepare_base", prepare_base)
    _keep(scene, "c" * 16, c["icons"])
    [stamps] = asyncio.run(I.stampers(c["icons"], [(0.0, 7.97)], frame_size=(W, H)))
    sf.restore_speaker(c["edited"], c["orig"], c["tmp"] / "pass.mp4", stamps=stamps, **rp.kwargs())
    run = capture["pass.mp4"]
    for i in (0, 4, 120, 236, 237, 238):
        body = asyncio.run(studio.icon_moment(1, "scene_0", _icons_only(), "c" * 16, i / FPS, version_id=70))
        got = cv2.imread(str(studio.resolve_file(body["url"].split("/file/", 1)[1])), cv2.IMREAD_UNCHANGED)
        assert body["frame"] == i and np.array_equal(got, run[i]), f"frame {i} of 239"


@pytest.fixture
def client(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.routers import rebrand_harness as rt
    from app.services import pattern_library as lib

    async def get(slug):
        return lib.BUILTINS.get(slug)

    monkeypatch.setattr(lib, "get", get)
    monkeypatch.setattr(lib, "lookup", get)
    app = FastAPI()
    app.include_router(rt.router)
    with TestClient(app) as c:
        yield c


class _Plan:
    def __init__(self, key, resolved):
        self._key, self._resolved = key, resolved

    def key(self):
        return self._key

    def resolved(self):
        return list(self._resolved)


def test_generate_re_derives_the_plan_and_freezes_the_one_that_stands(client, monkeypatch):
    shown = {"state": "ready", "plan_key": "f" * 16, "items": [{"label": "Code"}]}
    asked: list = []

    async def final_icon_plan(run_id, target, recipe, base=None):
        asked.append(recipe)
        return shown, _Plan("f" * 16, [CODE])

    seen: dict = {}

    async def start(run_id, raw, recipe=None, base=None):
        seen["recipe"] = recipe
        return {"id": 5, "run_id": run_id, "options": {"target": recipe.target}}

    async def lineage(e, fetch):
        return {"base": None, "layers": [], "depth": 0}

    from app.routers import rebrand_harness as rt

    monkeypatch.setattr(studio, "final_icon_plan", final_icon_plan)
    monkeypatch.setattr(pipeline, "start", start)
    monkeypatch.setattr(rt, "_lineage", lineage)
    url = "/api/rebrand/harness/versions"
    req = {"run_id": 1, "target": "scene_0", "recipe": with_icons().model_dump(mode="json")}
    for key in (None, "0" * 16):
        r = client.post(url, json={**req, **({"icons_plan_key": key} if key else {})})
        assert r.status_code == 409 and r.json()["detail"] == {"message": "The icons changed since you looked — "
                                                                          "check them", "plan": shown}
    assert "recipe" not in seen, "nothing generated"
    r = client.post(url, json={**req, "icons_plan_key": "f" * 16})
    assert r.status_code == 200
    frozen = rc.icon_element(seen["recipe"]).props["resolved"]
    assert frozen == [CODE] and r.json()["recipe"]["elements"][-1]["props"]["resolved"] == [CODE]
    assert "resolved" not in rc.icon_element(asked[-1]).props, "planned from the draft, never a sent plan"
    sent = with_icons(_res(label="Mine")).model_dump(mode="json")
    client.post(url, json={**req, "recipe": sent, "icons_plan_key": "f" * 16})
    assert rc.icon_element(seen["recipe"]).props["resolved"] == [CODE], "a client's frozen plan is never used"
    plain = rc.preset_42_default("scene_0").model_dump(mode="json")
    n = len(asked)
    assert client.post(url, json={**req, "recipe": plain}).status_code == 200 and len(asked) == n, \
        "no icons: no plan"


def test_keeping_omnis_icons_is_refused_on_a_fresh_draft(client):
    keep = with_icons(omni_own="keep").model_dump(mode="json")
    for route in ("preview", "icons/plan", "versions"):
        r = client.post(f"/api/rebrand/harness/{route}", json={"run_id": 1, "target": "scene_0", "recipe": keep})
        assert r.status_code == 422, route
        assert r.json()["detail"]["errors"][0]["field"] == "props.omni_own"


def test_the_moment_route(client, scene_env):
    icon = _res(frm=0.0, settle=0.1, to=0.66, entrance="none", box={"x": 0.3, "y": 0.2, "w": 0.4, "h": 0.25})
    _keep(scene_env["scene"], "d" * 16, [icon])
    body = client.post("/api/rebrand/harness/icons/moment", json={
        "run_id": 1, "target": "scene_0", "recipe": with_icons().model_dump(mode="json"),
        "plan_key": "d" * 16, "t": 0.2}).json()
    assert set(body) == {"url", "frame", "t"} and body["frame"] == 6
    assert client.get(body["url"]).headers["content-type"] == "image/png"
    bad = client.post("/api/rebrand/harness/icons/moment", json={
        "run_id": 1, "target": "scene_0", "recipe": with_icons().model_dump(mode="json"), "plan_key": "x", "t": 0})
    assert bad.status_code == 422


def test_a_preview_and_a_plan_on_a_version_say_when_it_has_omnis_own_icons(scene_env, monkeypatch):
    theirs = _omni_icons_version()

    async def load(spec, run_id, target, fetch=None):
        return hbase.BaseVersion(spec=spec, exp=theirs, chain=[theirs], layers=[])

    monkeypatch.setattr(hbase, "load", load)
    monkeypatch.setattr(studio, "_run_keys", lambda *a, **k: _none_keys())
    body = asyncio.run(studio.preview(1, "scene_0", with_icons(), base=hbase.BaseSpec(91, "rebuild")))
    assert body["parent_has_omni_icons"] is True
    fresh = asyncio.run(studio.preview(1, "scene_0", with_icons()))
    assert "parent_has_omni_icons" not in fresh, "a fresh draft's body is as it was"
    assert studio._lineage(None) == {} and studio._lineage(
        hbase.BaseVersion(spec=hbase.BaseSpec(81, "stack"), exp={}, chain=[{"options": {"icons_by_pipeline": True}}],
                          layers=[])) == {"parent_has_omni_icons": False}


async def _none_keys():
    return None


def test_a_run_without_a_frozen_plan_plans_it_with_the_studios_code(scene_env, monkeypatch):
    """An API client's recipe with icons and no props.resolved: the run
    plans them itself (runner._plan_inputs → studio.icon_ctx), and gets the
    very plan Generate would have frozen."""
    from test_icons_plan import FREE_CODE, judged, track, words

    scene_env["scene"].seconds = [7.97]

    async def fake_words(scene):
        return words(815)

    async def fake_track(scene):
        return track(), None

    async def fake_direct(c):
        return judged(c.words, FREE_CODE)

    monkeypatch.setattr(studio, "_icon_words", fake_words)
    monkeypatch.setattr(studio, "_icon_track", fake_track)
    monkeypatch.setattr(studio.dmod, "direct", fake_direct)
    recipe = with_icons()
    ctx = pipeline.Ctx(exp_id=90, run_id=1, aspect="9:16", width=720, height=1280, work=scene_env["tmp"],
                       options={"target": "scene_0"}, run_ctx={}, intro=None, scenes=[], outro=None)
    ctx.recipe = recipe

    async def main():                        # one loop, as the server's
        got = await I.plan_for_run(recipe, runner._plan_inputs(ctx))
        _body, frozen = await studio.final_icon_plan(1, "scene_0", recipe)
        return got, frozen

    (resolved, made), frozen = asyncio.run(main())
    assert made is not None and [r["label"] for r in resolved] == ["Free", "Code"]
    assert made.key() == frozen.key() and resolved == frozen.resolved()


def test_an_icon_only_preview_on_a_version_sends_nothing_to_omni(scene_env, monkeypatch, tmp_path):
    exp = _icon_version(tmp_path)
    bv = hbase.BaseVersion(spec=hbase.BaseSpec(81, "stack"), exp=exp, chain=[exp], layers=[],
                           chunks=list(scene_env["scene"].chunks), sources=list(scene_env["scene"].chunks),
                           frames=list(scene_env["scene"].firsts), occupied=None)

    async def prepare_base(run_id, target, version_id):
        return bv

    monkeypatch.setattr(studio, "prepare_base", prepare_base)
    body = asyncio.run(studio.preview(1, "scene_0", _icons_only(), base=hbase.BaseSpec(81, "stack")))
    assert body["omni_parts"] == [] and body["parent_has_omni_icons"] is False
    assert body["chunks"][0]["prompt"].endswith(hprompt.ICONS_CLAUSE)
    # The moving preview plays the version without its icons (the plan's replace them).
    assert studio._underlay(scene_env["scene"], bv) == {
        "kind": "version", "video_url": studio.url_for(bv.sources[0]), "version_id": 81, "clean": True}


# ---------------------------------------------------------------------------
# A version with icons: its still shows them
# ---------------------------------------------------------------------------

def _stilled(parts: list[list[float]], resolved: list[dict]) -> dict:
    stage = {"plan": {"resolved": resolved}}
    for j, span in enumerate(parts):
        stage[f"c{j}"] = {"span": span, "stamped": [], "path": f"c{j}.mp4"}
    return {"id": 77, "run_id": 1, "status": "ready", "options": {"target": "scene_0"},
            "arms": {"omni": {"status": "ready", "stages": {"icons": stage}}}}


def test_a_version_with_icons_takes_each_parts_still_at_its_first_icon():
    exp = _stilled([[0.0, 5.0], [5.0, 3.0]],
                   [_res("piggy-bank", 2.8333, 3.1, 4.3, label="Free"),
                    _res("code", 5.6, 5.8667, 7.0, label="Code")])
    # Part 0: Free fully in, and a beat after — the still has it.
    assert hbase.icon_still_at(exp, 0) == pytest.approx(3.1 + hbase.ICON_STILL_AFTER)
    # Part 1 (from 5 s): Code, in part seconds.
    assert hbase.icon_still_at(exp, 1) == pytest.approx(0.8667 + hbase.ICON_STILL_AFTER, abs=1e-3)
    # An icon going out just after it settles: before it has gone.
    short = _stilled([[0.0, 5.0]], [_res("code", 1.0, 1.2667, 1.5)])
    assert hbase.icon_still_at(short, 0) == pytest.approx(1.4)
    # No icons of ours (or none in that part): the settled first frame as always.
    assert hbase.icon_still_at(_stilled([[0.0, 5.0], [5.0, 3.0]], [_res("code", 1.0, 1.2667, 2.5)]), 1) is None
    assert hbase.icon_still_at({"id": 1, "arms": {"omni": {"stages": {}}}}, 0) is None


def test_the_still_at_an_icon_keeps_the_first_pose(tmp_path):
    # The graphic (blue) appears only at 1.5 s: a still at 2.0 s has it,
    # with the speaker (red) as at frame 0.
    frames = []
    for i in range(3 * FPS):
        f = _room(i)
        if i >= int(1.5 * FPS):
            f[10:50, 100:150] = BLUE
        frames.append(f)
    clip = _write(tmp_path / "v.mp4", frames)
    out = hbase.settled_first(clip, tmp_path / "s.png", entering=False, matte=_red, at_sec=2.0)
    f = cv2.imread(str(out)).astype(int)
    assert (np.abs(f[15:45, 105:145] - np.array(BLUE)).sum(axis=2) < 40).mean() > 0.98
    x0, y0, x1, y1 = _box(0)
    assert (np.abs(f[y0 + 3:y1 - 3, x0 + 3:x1 - 3] - np.array(RED)).sum(axis=2) < 40).mean() > 0.98
    # Without at_sec (not entering): frame 0 itself, no graphic yet.
    plain = hbase.settled_first(clip, tmp_path / "p.png", entering=False, matte=_red)
    assert (np.abs(cv2.imread(str(plain)).astype(int)[15:45, 105:145] - np.array(GREEN)).sum(axis=2) < 40).mean() > 0.98


def test_an_icon_just_leaving_at_a_parts_start_is_not_its_still():
    exp = _stilled([[0.0, 5.0], [5.0, 3.0]],
                   [_res("piggy-bank", 4.6, 4.8667, 5.1, label="Free"),
                    _res("code", 5.6, 5.8667, 7.0, label="Code")])
    assert hbase.icon_still_at(exp, 1) == pytest.approx(0.8667 + hbase.ICON_STILL_AFTER, abs=1e-3)


# ---------------------------------------------------------------------------
# The circle and the chip: the studio's sprite is the run's, each icon its
# own exit, and the studio's moving preview moves them as the run does (I5)
# ---------------------------------------------------------------------------

FX_FIXTURE = Path(__file__).parent / "fixtures" / "rebrand_icon_fx.json"
REPO = Path(__file__).resolve().parents[2]


def _look(r: dict, **kw) -> dict:
    """A resolved item in the new look (the planner writes these fields)."""
    out = {**r, "style": kw.pop("style", "circle"), "weight": "bold", "look_v": 1, **kw}
    if out["style"] == "chip":
        out.setdefault("font", I.label_key())
        out.setdefault("text", out["label"])
    return rc.IconResolved.model_validate(out).model_dump(mode="json")


def test_the_studio_sprite_is_the_stampers_own_bytes(scene_env, client):
    """What the stage draws at rest is what the run stamps: each icon's
    sprite PNG decodes to exactly Stamper.sprite_of for that item (the
    same function and cache key), rests where the Stamper puts it, and is
    served for good under its own bytes' name."""
    scene = scene_env["scene"]
    circle = _look(_res("code", box={"x": 0.04, "y": 0.3, "w": 0.16, "h": 0.09}))
    chip = _look(_res("piggy-bank", label="Tuition-Free", box={"x": 0.6, "y": 0.02, "w": 0.3, "h": 0.0664}),
                 style="chip", fill="#303030", text="Tuition-Free")
    badge = _res("clock", label="Time")
    resolved = [circle, chip, badge]
    got = studio._write_sprites(scene, resolved, {})
    assert len(got) == 3 and all(got)
    for r, sp in zip(resolved, got):
        stamps = ir.Stamper([r], fps=FPS, frame_size=(720, 1280))
        want = stamps.sprite_of(0)
        path = studio.resolve_file(sp["url"].split("/file/", 1)[1])
        assert path.parent.name == studio.SPRITES
        png = np.asarray(Image.open(path))
        assert png.dtype == np.uint8 and np.array_equal(png, want.rgba), r["style"]
        x, y, _w, _h = stamps.items[0].box
        assert (sp["w"], sp["h"], sp["ox"], sp["oy"]) == (want.rgba.shape[1], want.rgba.shape[0], want.pad, want.pad)
        assert sp["at"]["x"] == round((x - want.pad) / 720, 5) and sp["at"]["y"] == round((y - want.pad) / 1280, 5)
    assert got[2]["ox"] == 0, "the old badge has no shadow"
    # Content-addressed: the same icon, the same file; a colour change a new one.
    assert studio._write_sprites(scene, [circle], {})[0]["url"] == got[0]["url"]
    other = _look(_res("code", box=circle["box"]), fill="#8d01fa")
    assert studio._write_sprites(scene, [other], {})[0]["url"] != got[0]["url"]
    res = client.get(got[1]["url"])
    assert res.status_code == 200 and "immutable" in res.headers["cache-control"]
    # One whose glyph cannot be drawn gets none (the stage draws it itself).
    gone = _look(_res("gone-in-a-rebake"))
    assert studio._write_sprites(scene, [gone, circle], {})[0] is None


def test_an_icon_leaves_its_own_way_and_the_mirror_is_as_before():
    frame = np.full((1280, 720, 3), 90, np.uint8)
    box = {"x": 0.3, "y": 0.3, "w": 0.16, "h": 0.09}
    pop = _look(_res("code", 0.0, 0.2667, 1.0, box=box, entrance="pop"))
    fade_out = _look(_res("code", 0.0, 0.2667, 1.0, box=box, entrance="pop"),
                     fx={"entrance": "pop", "seed": "s:code", "exit": "fade"})
    same = ir.Stamper([pop], fps=FPS, frame_size=(720, 1280))
    mine = ir.Stamper([fade_out], fps=FPS, frame_size=(720, 1280))
    for t in (0.1, 0.5):                               # coming in, holding: the same
        assert np.array_equal(same.under(frame, t), mine.under(frame, t))
    t = 26 / FPS                                       # going: faded, not shrunk
    a, b = same.drawn(t)[0], mine.drawn(t)[0]
    assert b[0].shape == mine.sprite_of(0).rgba.shape and a[0].shape != b[0].shape
    assert b[0][..., 3].max() < mine.sprite_of(0).rgba[..., 3].max()
    # An exit that names the entrance is the mirror, byte for byte.
    named = _look(_res("code", 0.0, 0.2667, 1.0, box=box, entrance="pop"),
                  fx={"entrance": "pop", "seed": "s:code", "exit": "pop"})
    twin = ir.Stamper([named], fps=FPS, frame_size=(720, 1280))
    for f in range(0, 30):
        assert np.array_equal(same.under(frame, f / FPS), twin.under(frame, f / FPS))
    # "none" out: whole to its last frame, then gone.
    none_out = ir.Stamper([_look(_res("code", 0.0, 0.2667, 1.0, box=box),
                                 fx={"entrance": "glitch", "seed": "s:code", "exit": "none"})],
                          fps=FPS, frame_size=(720, 1280))
    last = none_out.drawn(29 / FPS)[0]
    assert np.array_equal(last[0], none_out.sprite_of(0).rgba) and none_out.drawn(30 / FPS) == []


def test_the_fx_numbers_are_the_fixture_and_the_studios_port_agrees():
    """backend/tests/fixtures/rebrand_icon_fx.json is fx_progress (and
    nearest_edge) frame by frame; frontend/src/lib/rebrandIconFx.ts is
    checked against it by node (skipped where node cannot strip types)."""
    import shutil
    from dataclasses import asdict

    fixture = json.loads(FX_FIXTURE.read_text(encoding="utf-8"))
    for c in fixture["cases"]:
        for i, want in enumerate(c["frames"]):
            got = ir.fx_progress(c["entrance"], i - 1, end=c["end"], fin=c["fin"], fout=c["fout"], exit=c["exit"])
            if want is None:
                assert got is None
                continue
            got = asdict(got)
            assert {k: (round(v, 12) if isinstance(v, float) else v) for k, v in got.items()} == want, (c, i)
    for e in fixture["edges"]:
        assert list(ir.nearest_edge(tuple(e["box"]), tuple(e["frame"]))) == [e["edge"], e["travel"]]
    # fx() draws exactly those numbers: a fade's alpha, a slide's offset.
    sprite = np.full((20, 20, 4), 255, np.uint8)
    for f in range(12):
        pr = ir.fx_progress("fade", f, end=12, fin=4, fout=4)
        got = ir.fx("fade", f / FPS, sprite, "s", span=12 / FPS, in_sec=4 / FPS, out_sec=4 / FPS)
        assert int(got.rgba[0, 0, 3]) == int(np.clip(np.rint(255 * pr.alpha), 0, 255))
        pr = ir.fx_progress("slide", f, end=12, fin=4, fout=4)
        got = ir.fx("slide", f / FPS, sprite, "s", span=12 / FPS, in_sec=4 / FPS, out_sec=4 / FPS,
                    edge="left", travel=100)
        assert got.dx == -int(round(pr.slide * 100))
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed here")
    version = subprocess.run([node, "--version"], capture_output=True, text=True, timeout=30).stdout.strip()
    if tuple(int(x) for x in version.lstrip("v").split(".")[:2]) < (22, 6):
        pytest.skip(f"node {version} cannot strip types")
    out = subprocess.run([node, "--experimental-strip-types", "scripts/check-rebrand-icon-fx.mjs"],
                         capture_output=True, text=True, timeout=120, cwd=REPO / "frontend")
    assert out.returncode == 0, out.stdout + out.stderr
    total = len(fixture["cases"]) + len(fixture["timing"]) + len(fixture["edges"])
    assert f"all {total} cases agree" in out.stdout


def test_a_chip_in_front_is_the_same_on_the_exact_still_and_in_the_run(scene_env):
    """The exact still (/icons/moment) stamps a chip, in front, with the
    run's Stamper: its pixels are the icon and nothing else, and over the
    speaker where they overlap."""
    recipe = with_icons()
    asyncio.run(studio.preview(1, "scene_0", recipe))
    chip = _look(_res("piggy-bank", 0.0, 0.1, 0.66, label="Tuition-Free", entrance="none",
                      box={"x": 0.3, "y": 0.3, "w": 0.4, "h": 0.0664}), style="chip", layer="front")
    _keep(scene_env["scene"], "a" * 16, [chip])
    _keep(scene_env["scene"], "e" * 16, [])

    def still(key):
        body = asyncio.run(studio.icon_moment(1, "scene_0", recipe, key, 0.3))
        return cv2.imread(str(studio.resolve_file(body["url"].split("/file/", 1)[1])))

    got, bare = still("a" * 16), still("e" * 16)
    stamps = ir.Stamper([chip], fps=FPS, frame_size=(720, 1280))
    [(x0, y0, x1, y1)] = stamps.boxes(0.3, drawn=True, layer="front")
    inside = np.zeros((1280, 720), bool)
    inside[y0:y1, x0:x1] = True
    diff = (got != bare).any(axis=2)
    assert diff.any() and not (diff & ~inside).any(), "the chip and its shadow, nothing else"
    assert np.array_equal(got, stamps.over(bare, 0.3)), "drawn after the speaker: over the speaker"
