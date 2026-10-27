"""Issue 7, "two speakers": a Veo clip can end on a flash of ANOTHER shot.

About a third of Veo's 8-second clips end with 1-5 frames of a wider
framing (run 815's scene, anim-351: frames 188-191 of 192). Omni never
follows that cut, and the speaker pasted back from the new shot over Omni's
old one made a version end on two people — the very frame a finished video
rests on. So a scene is normalised without the flash (media.normalize_scene),
in the studio and in the run alike, and the one-speaker check
(guards.one_speaker) finds such a frame in any output.

Synthetic clips are built with numpy through ffmpeg; the real ones (anim-351,
run 815's studio folder, v25) are used when they are on disk, and skipped
otherwise. Every model call is refused; MODNet is faked except in the one
test that measures v25's real frames (skipped without it)."""
from __future__ import annotations

import asyncio
import json
import shutil
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
from app.services.rebrand_harness import director as dmod
from app.services.rebrand_harness import guards as gd
from app.services.rebrand_harness import speaker_track
from app.services.rebrand_harness import stages as st
from app.services.rebrand_harness import studio
from app.services.rebrand_harness import words as wordsmod

cv2 = pytest.importorskip("cv2")

DATA = Path(__file__).resolve().parents[1] / "data" / "yas_producer"
ANIM_351 = DATA / "animations" / "anim-351.mp4"
S815 = DATA / "talking-head-v2" / "rebrand" / "_studio" / "815" / "scene_0_84f81b9967"
V25 = DATA / "talking-head-v2" / "rebrand" / "25"
LINE_815 = ("42 Abu Dhabi is where your future becomes uncoded: a tuition-free, peer-learning "
            "coding school without teachers, building real projects.")
W, H = 180, 320
AAC_FRAME = 1024 / media.AUDIO_RATE


@pytest.fixture(autouse=True)
def _no_paid_calls(monkeypatch):
    async def refuse(*_a, **_k):
        raise AssertionError("a paid model call in a test")

    monkeypatch.setattr(vertex_client, "gemini_multimodal", refuse)
    monkeypatch.setattr(vertex_client, "gemini_image", refuse)
    monkeypatch.setattr(omni, "edit_video", refuse)
    monkeypatch.setattr(omni, "interpolate", refuse)
    monkeypatch.setattr(person_matte, "person_mask", refuse)
    monkeypatch.setattr(studio, "_PREPARED", {})
    monkeypatch.setattr(studio, "_warm", lambda scene: None)


# ---------------------------------------------------------------------------
# Synthetic clips
# ---------------------------------------------------------------------------

def _frame(bg: tuple, x: int, y: int, r: tuple, w: int = W, h: int = H) -> np.ndarray:
    """A room (a vertical gradient on `bg`) with a light figure in it."""
    ramp = np.linspace(-25, 25, h, dtype=np.float32)[:, None, None]
    f = np.clip(np.asarray(bg, np.float32)[None, None, :] + ramp, 0, 255).astype(np.uint8)
    f = np.ascontiguousarray(np.broadcast_to(f, (h, w, 3)))
    cv2.ellipse(f, (int(x), int(y)), r, 0, 0, 360, (205, 195, 185), -1)
    return f


def _shot_a(n: int, w: int = W, h: int = H) -> list:
    """The scene's own shot: a dim room, the figure drifting a pixel a frame."""
    return [_frame((70, 60, 50), w // 3 + i, h // 2, (w // 5, h // 4), w, h) for i in range(n)]


def _shot_b(n: int, w: int = W, h: int = H) -> list:
    """Another shot: a bright, wider framing (the flash)."""
    return [_frame((175, 185, 195), w // 2, h // 2 + i, (w // 3, h // 3), w, h) for i in range(n)]


def _clip(path: Path, frames: list, fps: int = 24, audio: bool = True) -> Path:
    h, w = frames[0].shape[:2]
    cmd = [media.ffmpeg_exe(), "-y", "-loglevel", "error", "-f", "rawvideo", "-pix_fmt", "bgr24",
           "-s", f"{w}x{h}", "-r", str(fps), "-i", "-"]
    if audio:
        cmd += ["-f", "lavfi", "-i", f"sine=frequency=440:sample_rate={media.AUDIO_RATE}:"
                                     f"duration={len(frames) / fps:.4f}"]
    cmd += ["-c:v", "libx264", "-preset", "ultrafast", "-crf", "12", "-pix_fmt", "yuv420p"]
    if audio:
        cmd += ["-c:a", "aac", "-shortest"]
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(cmd + [str(path)], input=b"".join(f.tobytes() for f in frames), check=True)
    return path


def _audio_sec(path: Path) -> float:
    """The clip's sound, decoded and counted sample by sample."""
    pcm = subprocess.run([media.ffmpeg_exe(), "-v", "error", "-i", str(path), "-map", "0:a:0", "-ac", "1",
                          "-ar", str(media.AUDIO_RATE), "-f", "s16le", "-"], capture_output=True, check=True).stdout
    return len(pcm) / 2 / media.AUDIO_RATE


def _flash_clip(tmp_path: Path, name: str = "flash.mp4") -> Path:
    """30 frames of the scene's shot, then a 4-frame flash of another (24 fps)."""
    return _clip(tmp_path / "src" / name, _shot_a(30) + _shot_b(4))


# ---------------------------------------------------------------------------
# The cut, and the trim
# ---------------------------------------------------------------------------

def test_a_tail_flash_is_found_and_cut_off_with_its_sound(tmp_path):
    src = _flash_clip(tmp_path)
    assert media.shot_cuts(src) == [30]
    assert media.tail_cut(src) == 30
    out = media.normalize_scene(src, tmp_path / "base" / "scene0.mp4", width=W, height=H)
    n = media.frame_count(out)
    # The flash never enters the filter; the clip ends on its own shot's
    # last frame (1.25 s at 30 fps is 37.5 frames: the fps filter closes on
    # that frame's own time, a frame short of it at most).
    assert 36 <= n <= 38, n
    assert media.shot_cuts(out) == []
    assert float(np.max(media._frame_diffs(out)[-8:])) <= media.SHOT_CUT_ABS, "the flash is gone"
    info = media.scene_info(out)
    assert info["source_cut"] == 30 and info["source_fps"] == 24.0 and info["cuts"] == []
    assert info["trimmed_sec"] == pytest.approx(4 / 24, abs=1e-3)
    # The sound stops with the picture (within one AAC frame).
    assert abs(_audio_sec(out) - n / media.FPS) <= AAC_FRAME + 1e-3
    # Written whole: nothing half-made is left beside it.
    assert sorted(p.name for p in out.parent.iterdir()) == ["scene0.cuts.json", "scene0.mp4"]


def test_a_clip_ending_on_its_own_shot_is_made_exactly_as_before(tmp_path, monkeypatch):
    src = _clip(tmp_path / "src" / "clean.mp4", _shot_a(40))
    assert media.shot_cuts(src) == [] and media.tail_cut(src) is None

    # normalize() without end_frame: the very command it always ran.
    seen: list = []
    monkeypatch.setattr(media, "_run", lambda cmd, **kw: seen.append(cmd))
    media.normalize(src, tmp_path / "o.mp4", width=W, height=H, audio="keep")
    vf = (f"scale={W}:{H}:force_original_aspect_ratio=decrease,"
          f"pad={W}:{H}:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1,"
          f"setpts=PTS/1.00000,fps=30,format=yuv420p")
    assert seen == [[media.ffmpeg_exe(), "-y", "-i", str(src),
                     "-filter_complex", f"[0:v]{vf}[v];[0:a]aresample=44100[a]", "-map", "[v]", "-map", "[a]",
                     "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
                     "-c:a", "aac", "-b:a", "160k", "-ar", "44100", "-ac", "2",
                     "-shortest", "-movflags", "+faststart", str(tmp_path / "o.mp4")]]
    monkeypatch.undo()

    # normalize_scene of it: the same bytes normalize() makes, described.
    plain = media.normalize(src, tmp_path / "plain" / "scene0.mp4", width=W, height=H, audio="keep")
    scene = media.normalize_scene(src, tmp_path / "base" / "scene0.mp4", width=W, height=H)
    assert scene.read_bytes() == plain.read_bytes()
    assert media.scene_info(scene) == {"source_fps": 24.0, "source_cut": None, "trimmed_sec": 0.0, "cuts": []}


def test_a_jump_cut_inside_the_clip_stays(tmp_path):
    src = _clip(tmp_path / "src" / "push_in.mp4", _shot_a(40) + _shot_b(56))
    assert media.shot_cuts(src) == [40]
    assert media.tail_cut(src) is None, "a push-in 1.7 s into a 4 s clip is the clip's own"
    out = media.normalize_scene(src, tmp_path / "base" / "scene0.mp4", width=W, height=H)
    assert abs(media.frame_count(out) - 120) <= 1
    info = media.scene_info(out)
    assert info["source_cut"] is None and info["trimmed_sec"] == 0.0
    assert len(info["cuts"]) == 1 and abs(info["cuts"][0] - 50) <= 1, info


def test_a_letterboxed_clip_is_read_inside_its_picture(tmp_path):
    """A landscape scene on the portrait canvas: two thirds of every frame
    is black bar, which must not water the cut down."""
    lw, lh = 320, 180
    src = _clip(tmp_path / "src" / "wide.mp4", _shot_a(30, lw, lh) + _shot_b(4, lw, lh))
    boxed = media.normalize(src, tmp_path / "boxed.mp4", width=W, height=H, audio="keep")
    cut = media.tail_cut(boxed)
    assert cut is not None and abs(cut - 37.5) <= 1.5, cut
    assert float(media._frame_diffs(boxed)[cut]) > 3 * media.SHOT_CUT_ABS


def test_one_speaker_frames_cover_the_end_and_each_cut():
    picked = gd.one_speaker_frames(239, [120])
    assert picked[:3] == [0, 10, 20]
    assert set(range(227, 239)) <= set(picked), "the last frames, where a finished video rests"
    assert {118, 119, 120, 121, 122} <= set(picked)
    assert len(picked) <= 40


def test_one_speaker_finds_the_second_person(tmp_path):
    frames = _shot_a(36)
    src = _clip(tmp_path / "src.mp4", frames, fps=30, audio=False)
    same = _clip(tmp_path / "same.mp4", frames, fps=30, audio=False)
    # The last three frames of the output show someone else beside her.
    two = [f.copy() for f in frames]
    for f in two[-3:]:
        cv2.ellipse(f, (W - 30, H // 3), (22, 50), 0, 0, 360, (205, 195, 185), -1)
    doubled = _clip(tmp_path / "two.mp4", two, fps=30, audio=False)

    def matte(bgr):
        return (bgr.max(axis=2) > 150).astype(np.float32)

    assert gd.one_speaker(same, src, matte=matte) == []
    assert gd.one_speaker(doubled, src, matte=matte) == [33, 34, 35]
    assert gd.one_speaker(doubled, src, frames=[0, 34], matte=matte) == [34]


# ---------------------------------------------------------------------------
# The studio and the run make the scene the same way
# ---------------------------------------------------------------------------

def _fake_reel(monkeypatch, src: Path, line: str = "It's free, and you learn to code."):
    md = {"scene_count": 1, "aspect_ratio": "9:16", "script": {"dialogue": line}}

    async def run_row(run_id):
        return {"id": run_id, "topic": "Why 42", "metadata": md}

    async def no_bookend(*a, **k):
        return None

    async def scenes(md_, cache, *, strict=True, force_index=None):
        return [pipeline.Segment(kind="scene", index=0, source=src, is_still=False)]

    monkeypatch.setattr(studio.th, "_fetch_run_row", run_row)
    monkeypatch.setattr(pipeline, "_bookend", no_bookend)
    monkeypatch.setattr(pipeline, "_scenes", scenes)


def _record_normalize_scene(monkeypatch) -> list:
    calls: list = []
    real = media.normalize_scene

    def recorded(src, out, *, width, height):
        calls.append((Path(src), Path(out), width, height))
        return real(src, out, width=width, height=height)

    monkeypatch.setattr(media, "normalize_scene", recorded)
    return calls


def test_the_studio_and_the_run_cut_the_scene_the_same_way(tmp_path, monkeypatch):
    src = _flash_clip(tmp_path)
    monkeypatch.setattr(studio, "STUDIO_ROOT", tmp_path / "rebrand" / "_studio")
    monkeypatch.setattr(pipeline, "REBRAND_ROOT", tmp_path / "rebrand")
    _fake_reel(monkeypatch, src)
    calls = _record_normalize_scene(monkeypatch)

    scene = asyncio.run(studio.prepare(1, "scene_0"))
    key = st.digest(src)[:10]
    assert scene.work == studio.scene_base(1, "scene_0", src)
    assert scene.work.name == f"scene_0_{key}_t30"
    base = scene.work / "base" / "scene0.mp4"
    assert calls == [(src, base, 720, 1280)]
    body = studio.public_scene(scene)
    assert body["trimmed_sec"] == pytest.approx(4 / 24, abs=1e-3) and body["cuts_sec"] == []
    assert 1.15 <= body["duration_sec"] <= 1.26, "the source's 1.42 s, less the flash"
    # A reopen finds it prepared (the whole folder name is compared, D2).
    assert asyncio.run(studio.prepare(1, "scene_0")) is scene

    async def no_pattern(ctx):
        return None

    monkeypatch.setattr(pipeline, "_build_pattern_ref", no_pattern)
    options = pipeline.normalize_options({"target": "scene_0", "run_omni": True})
    ctx = asyncio.run(pipeline._prepare(7, 1, options))
    seg = ctx.target_seg
    # The run copies the studio's clip: the very bytes its previews came from.
    assert len(calls) == 1
    assert seg.base.read_bytes() == base.read_bytes()
    assert media.scene_info(seg.base) == media.scene_info(base)
    # The reel's last picture is the scene's own last frame, not the flash.
    last = cv2.imread(str(ctx.frames["th_last"]))
    own = None
    cap = cv2.VideoCapture(str(base))
    while True:
        ok, f = cap.read()
        if not ok:
            break
        own = f
    cap.release()
    assert last.shape == own.shape and float(np.abs(last.astype(int) - own.astype(int)).mean()) < 3.0
    assert float(last.mean()) < 120, "the flash is the bright shot"

    # With no studio copy (another reel), the run makes it the same way.
    ctx2 = asyncio.run(pipeline._prepare(8, 2, options))
    assert len(calls) == 2
    run_src, run_out, run_w, run_h = calls[1]
    assert (run_src, run_w, run_h) == (src, 720, 1280)
    assert run_out == tmp_path / "rebrand" / "8" / "base" / "scene0.mp4" == ctx2.target_seg.base
    assert media.scene_info(run_out)["source_cut"] == 30


def test_an_older_preparation_with_no_flash_keeps_its_folder(tmp_path, monkeypatch):
    """A scene with no flash keeps the folder (and every cache) it had; its
    clip, made by an older build, is described rather than made again."""
    src = _clip(tmp_path / "src" / "clean.mp4", _shot_a(40))
    monkeypatch.setattr(studio, "STUDIO_ROOT", tmp_path / "studio")
    _fake_reel(monkeypatch, src)
    old = tmp_path / "studio" / "1" / f"scene_0_{st.digest(src)[:10]}"
    made = media.normalize(src, old / "base" / "scene0.mp4", width=720, height=1280, audio="keep")
    before = made.read_bytes()
    calls = _record_normalize_scene(monkeypatch)
    scene = asyncio.run(studio.prepare(1, "scene_0"))
    assert scene.work == old and calls == []
    assert made.read_bytes() == before
    assert media.scene_info(made) == {"source_fps": 24.0, "source_cut": None, "trimmed_sec": 0.0, "cuts": []}
    assert studio.public_scene(scene)["trimmed_sec"] == 0.0


# ---------------------------------------------------------------------------
# The words and the director's answer come with the trim (D3)
# ---------------------------------------------------------------------------

def _old_scene(tmp_path: Path, src: Path, line: str, *, last_end: float) -> Path:
    """The same scene as a build before the trim prepared it: its folder,
    its (untrimmed) clip, its words keyed to that clip, a paid answer and a
    second take."""
    old = tmp_path / "studio" / "1" / f"scene_0_{st.digest(src)[:10]}"
    clip = media.normalize(src, old / "base" / "scene0.mp4", width=720, height=1280, audio="keep")
    words = wordsmod.build_words(None, line, None, 1.0, note="no word timing")
    said = [w for w in words["words"] if w["said"]]
    said[-1]["e"] = last_end
    words.update(duration=1.417, key=wordsmod.cache_key(clip, line), model=wordsmod.model_id())
    icons = old / "icons"
    icons.mkdir(parents=True)
    (icons / "words.json").write_text(json.dumps(words), encoding="utf-8")
    (icons / "director.json").write_text('{"version":1,"key":"k","source":"ai","ideas":[]}', encoding="utf-8")
    (icons / "take.json").write_text('{"take": 2}', encoding="utf-8")
    return old


def test_a_trimmed_scene_keeps_its_words_and_the_directors_answer(tmp_path, monkeypatch):
    line = "It's free, and you learn to code."
    src = _flash_clip(tmp_path)
    monkeypatch.setattr(studio, "STUDIO_ROOT", tmp_path / "studio")
    _fake_reel(monkeypatch, src, line)
    old = _old_scene(tmp_path, src, line, last_end=1.2)

    def no_whisper(*a, **k):
        raise AssertionError("Whisper ran again")

    monkeypatch.setattr(wordsmod, "transcribe", no_whisper)
    scene = asyncio.run(studio.prepare(1, "scene_0"))
    assert scene.work.name.endswith("_t30") and scene.work.parent == old.parent
    was = json.loads((old / "icons" / "words.json").read_text(encoding="utf-8"))
    now = json.loads((scene.work / "icons" / "words.json").read_text(encoding="utf-8"))
    clip = studio._scene_clip(scene)
    assert now["key"] == wordsmod.cache_key(clip, line) != was["key"]
    assert now["digest"] == was["digest"] == wordsmod.words_digest(now), "the director's key holds it"
    assert now["words"] == was["words"] and now["duration"] == 1.25
    assert len(now["envelope"]) == len(was["envelope"])
    for name in ("director.json", "take.json"):
        assert (scene.work / "icons" / name).read_bytes() == (old / "icons" / name).read_bytes()
    # The icon plan reads them as its own: no Whisper.
    assert wordsmod.scene_words(clip, line, studio._icon_dir(scene)) == now
    assert studio._take(scene) == 2


def test_words_cut_by_the_trim_are_heard_again(tmp_path, monkeypatch):
    line = "It's free, and you learn to code."
    src = _flash_clip(tmp_path)
    monkeypatch.setattr(studio, "STUDIO_ROOT", tmp_path / "studio")
    _fake_reel(monkeypatch, src, line)
    _old_scene(tmp_path, src, line, last_end=1.3)            # past the cut at 1.25 s
    scene = asyncio.run(studio.prepare(1, "scene_0"))
    assert not (studio._icon_dir(scene) / "words.json").exists()
    assert not (studio._icon_dir(scene) / "director.json").exists()


# ---------------------------------------------------------------------------
# The real clips (skipped when they are not on disk)
# ---------------------------------------------------------------------------

@pytest.mark.skipif(not ANIM_351.is_file(), reason="anim-351.mp4 is not on this machine")
def test_anim_351_ends_on_a_flash_and_no_word_is_lost():
    cut = media.tail_cut(ANIM_351)
    assert cut == 188
    words = S815 / "icons" / "words.json"
    if words.is_file():
        said = [w for w in json.loads(words.read_text(encoding="utf-8"))["words"] if w["said"]]
        assert max(float(w["e"]) for w in said) < cut / 24.0


@pytest.mark.skipif(not (ANIM_351.is_file() and (S815 / "icons" / "track.json").is_file()),
                    reason="run 815's studio folder is not on this machine")
def test_the_director_is_not_asked_again_for_815_after_the_trim(tmp_path):
    """Run 815's scene, trimmed: its words carried over and its speaker
    track made again on the trimmed clip give the director the key it
    answered under — the paid answer stands."""
    old = tmp_path / "815" / S815.name
    (old / "base").mkdir(parents=True)
    shutil.copyfile(S815 / "base" / "scene0.mp4", old / "base" / "scene0.mp4")
    shutil.copytree(S815 / "icons", old / "icons",
                    ignore=shutil.ignore_patterns("moments", "plans", "*.jpg", "bg.npz", "masks_*"))
    work = old.with_name(f"{S815.name}_t188")
    clip = media.normalize_scene(ANIM_351, work / "base" / "scene0.mp4", width=720, height=1280)
    info = media.scene_info(clip)
    assert info["source_cut"] == 188 and info["cuts"] == []
    assert media.frame_count(clip) == 234
    seg = pipeline.Segment(kind="scene", index=0, source=ANIM_351, is_still=False, base=clip)
    ctx = pipeline.Ctx(exp_id=0, run_id=815, aspect="9:16", width=720, height=1280, work=work, options={},
                       run_ctx={"scene_dialogue": {0: LINE_815}, "topic": "Why 42"}, intro=None,
                       scenes=[seg], outro=None)
    ctx.target_seg = seg
    scene = studio.Scene(run_id=815, target="scene_0", aspect="9:16", width=720, height=1280, work=work,
                         ctx=ctx)
    studio._carry_icons(scene, info)
    was = json.loads((old / "icons" / "words.json").read_text(encoding="utf-8"))
    now = json.loads((work / "icons" / "words.json").read_text(encoding="utf-8"))
    assert now["digest"] == was["digest"] and now["key"] == wordsmod.cache_key(clip, LINE_815)
    assert (work / "icons" / "director.json").read_bytes() == (S815 / "icons" / "director.json").read_bytes()

    old_track = speaker_track.load(old / "icons")
    assert old_track is not None
    new_track = speaker_track.build(clip, work / "icons", matte=lambda f: np.zeros(f.shape[:2], np.float32))
    assert dmod.spots_digest(new_track.frame0, new_track.spots) == \
        dmod.spots_digest(old_track.frame0, old_track.spots), "the room the director read is the same"
    catalog = dmod.default_catalog()

    def key(words, track, work_dir):
        return dmod.director_key(dmod.DirectorCtx(
            words=words, script=LINE_815, work_dir=work_dir, topic="Why 42", others=["another line"],
            frame0=track.frame0, spots=list(track.spots), take=0), catalog)

    assert key(now, new_track, work / "icons") == key(was, old_track, old / "icons")


@pytest.mark.skipif(not ((V25 / "omni" / "scene_1_c0_front.mp4").is_file() and (V25 / "base" / "scene0.mp4").is_file()),
                    reason="version 25's files are not on this machine")
def test_one_speaker_finds_v25s_last_frame():
    try:
        speaker_front._get_session()
    except Exception as e:  # noqa: BLE001 — no local MODNet here
        pytest.skip(f"MODNet is not available: {e}")
    assert gd.one_speaker(V25 / "omni" / "scene_1_c0_front.mp4", V25 / "base" / "scene0.mp4") == [237]
