"""Rebrand studio v2, phase 2: building on a finished version.

  "Change it" (rebuild)  redone from the original with the version's take and
                         manifest: what did not change is reused; Try again is
                         a new take.
  "Add on top" (stack)   the new layers drawn on the version's own parts,
                         which stay exactly as they were outside them (the
                         hold); the speaker from the TRUE original.

Omni, Gemini, the database and every paid call are faked; ffmpeg and the
synthetic clips are real. The 42 default is held byte for byte by
tests/test_rebrand_golden.py."""
from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import json
import shutil
import subprocess
from pathlib import Path

import numpy as np
import pytest
from fastapi import HTTPException
from PIL import Image

from app.services import omni_client as omni
from app.services import pattern_library as lib
from app.services import person_matte
from app.services import rebrand_brief
from app.services import rebrand_media as media
from app.services import rebrand_pattern_ref as pattern_ref
from app.services import rebrand_pipeline as pipeline
from app.services import rebrand_store as store
from app.services import speaker_front
from app.services.rebrand_harness import base as hbase
from app.services.rebrand_harness import prompt as hprompt
from app.services.rebrand_harness import recipe as rc
from app.services.rebrand_harness import runner
from app.services.rebrand_harness import stages as st
from app.services.rebrand_harness import studio
from app.services.rebrand_harness.elements import pattern as pattern_el

cv2 = pytest.importorskip("cv2")

FIX = Path(__file__).parent / "fixtures" / "rebrand_golden"
INTRO, FRAME, PERSON = FIX / "intro.mp4", FIX / "frame.png", FIX / "person.png"
COLOURS = pattern_ref.Colours(ground="#3d3c3d", ink="#f4f4f8", fell_back=False, frame=None,
                              colour="#2ed8e8")
ROW_TOP_RIGHT = lib.BEADS_ROW_CANDIDATES[3]
BRIEF = {"omni_scene_prompt": "EDIT it."}


def _person() -> np.ndarray:
    return np.asarray(Image.open(PERSON).convert("L"), dtype="float32") / 255.0


def _pat(id_: str = "pattern", slug: str = "structure", anchor: str = "auto", **props) -> rc.Element:
    return rc.Element(id=id_, kind="pattern", placement=rc.Placement(anchor=anchor),
                      props={"slug": slug, "layout": None, "colour": "auto", **props})


def _recipe(*els: rc.Element, **kw) -> rc.Recipe:
    return rc.normalize(rc.Recipe(target="scene_0", seed="studio:1:scene_0", elements=list(els), **kw))


def _ref(slug: str, layout: pattern_ref.Layout, out: Path, animation: str = "left_to_right",
         colour: str = "#7f2bf2") -> pattern_ref.PatternRef:
    return pattern_ref.build(None, out, aspect="9:16", slug=slug, layout=layout, animation=animation,
                             colours=dataclasses.replace(COLOURS, colour=colour))


async def _quiet(*a, **k):
    return None


def _version(id_: int, *, recipe: rc.Recipe | None = None, status: str = "ready", base=None,
             stages=None, evidence=None, run_id: int = 1, target: str = "scene_0", take=None,
             brief=None, **omni_arm) -> dict:
    """A version's row, as rebrand_store returns it."""
    options = {"target": target}
    if recipe is not None:
        options["recipe"] = recipe.model_dump(mode="json")
    if base is not None:
        options["base"] = base
    if take is not None:
        options["take"] = take
    return {"id": id_, "run_id": run_id, "status": status, "stage": "Done", "options": options,
            "brief": brief if brief is not None else dict(BRIEF), "evidence": evidence or {},
            "arms": {"omni": {"status": "ready" if status == "ready" else status,
                              "stages": stages or {}, **omni_arm}}}


def _rows(monkeypatch, *rows: dict) -> dict:
    by_id = {r["id"]: r for r in rows}

    async def get(exp_id):
        return by_id.get(int(exp_id))

    monkeypatch.setattr(store, "get_experiment", get)
    return by_id


# ---------------------------------------------------------------------------
# Synthetic clips (ffmpeg, lossless)
# ---------------------------------------------------------------------------

W, H, FPS = 160, 288, 30
GREEN, RED, BLUE = (40, 160, 40), (40, 40, 220), (220, 60, 30)      # BGR
MAGENTA, YELLOW = (200, 40, 200), (30, 220, 230)


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
    x0 = 30 + 2 * i
    return x0, 110, x0 + 50, 280


def _room(i):
    f = np.zeros((H, W, 3), np.uint8)
    f[:] = GREEN
    x0, y0, x1, y1 = _box(i)
    f[y0:y1, x0:x1] = RED
    return f


def _red(bgr):
    """Stand-in for MODNet: the speaker is whatever is red."""
    b, g, r = (bgr[:, :, k].astype(int) for k in range(3))
    return ((r > 150) & (g < 110) & (b < 110)).astype(np.float32)


def _near(f, colour, tol=40) -> np.ndarray:
    return np.abs(f.astype(int) - np.array(colour)).sum(axis=2) < tol


# ---------------------------------------------------------------------------
# restore_speaker: the backdrop and the hold
# ---------------------------------------------------------------------------

def test_the_hold_keeps_the_version_outside_the_new_layer(tmp_path, monkeypatch):
    """The version (backdrop) has a blue band; Omni's second pass drew the new
    magenta block, a stray yellow mark elsewhere, and redrew the band a bit
    off. Outside the new block's footprint the frame is the version's; inside
    it Omni's; on the speaker the original's."""
    monkeypatch.setattr(speaker_front, "person_matte", _red)
    n = 12
    orig = _write(tmp_path / "o.mp4", [_room(i) for i in range(n)])

    def version(i):
        f = _room(i)
        f[10:50, :] = BLUE
        return f

    def omni_pass(i):
        f = version(i)
        f[12:52, :] = (200, 90, 60)              # the band, redrawn a little off
        f[70:100, 100:150] = MAGENTA             # the new layer
        f[240:270, 5:25] = YELLOW                # a stray drawing
        x0, y0, x1, y1 = _box(i)
        f[y0:y1, x0:x1] = (60, 60, 160)          # Omni's speaker, not quite the original
        return f

    back = _write(tmp_path / "b.mp4", [version(i) for i in range(n)])
    edited = _write(tmp_path / "e.mp4", [omni_pass(i) for i in range(n)])
    hold = np.ones((H, W), np.float32)
    hold[60:110, 90:160] = 0.0                    # the new layer's footprint
    out = _read_all(speaker_front.restore_speaker(edited, orig, tmp_path / "x.mp4", every=2,
                                                  backdrop=back, hold=hold))
    assert len(out) == n
    for i, f in enumerate(out):
        assert _near(f[14:48, 5:155], BLUE).mean() > 0.98, f"frame {i}: the version's band"
        assert _near(f[242:268, 7:23], GREEN).mean() > 0.98, f"frame {i}: the stray mark is gone"
        assert _near(f[72:98, 102:148], MAGENTA).mean() > 0.98, f"frame {i}: the new layer"
        x0, y0, x1, y1 = _box(i)
        assert _near(f[y0 + 3:y1 - 3, x0 + 3:x1 - 3], RED).mean() > 0.98, f"frame {i}: speaker"
    with pytest.raises(ValueError):
        speaker_front.restore_speaker(edited, orig, tmp_path / "y.mp4", hold=hold)


def _mid_room(h=H, w=W, seed=5):
    rng = np.random.default_rng(seed)
    return cv2.GaussianBlur(rng.integers(40, 170, size=(h, w, 3)).astype(np.uint8), (0, 0), 2)


def _whiten(img, rows, a):
    e = img.astype(np.float32).copy()
    e[rows] = e[rows] * (1 - a) + 245.0 * a
    return np.clip(e, 0, 255).astype(np.uint8)


def test_the_veil_is_measured_against_the_version_when_there_is_one(tmp_path, monkeypatch):
    """The version carries a light band (its own graphic). Measured against
    the original it looks like a veil and would be erased; measured against
    the version it is not — and a frosted panel Omni slid in is still
    cleared, back to the version."""
    monkeypatch.setattr(speaker_front, "person_matte", lambda bgr: np.zeros(bgr.shape[:2], np.float32))
    room = _mid_room()
    band, panel = slice(20, 70), slice(180, 260)
    version = _whiten(room, band, 0.4)
    omni_frames = [_whiten(version, panel, 0.3) for _ in range(6)]
    orig = _write(tmp_path / "o.mp4", [room] * 6)
    back = _write(tmp_path / "b.mp4", [version] * 6)
    edited = _write(tmp_path / "e.mp4", omni_frames)
    against_orig = _read_all(speaker_front.restore_speaker(edited, orig, tmp_path / "a.mp4",
                                                           clear_veil_sec=0.2))
    against_back = _read_all(speaker_front.restore_speaker(edited, orig, tmp_path / "b2.mp4",
                                                           clear_veil_sec=0.2, backdrop=back))

    def diff(f, ref, rows):
        return np.abs(f[rows, 10:150].astype(int) - ref[rows, 10:150].astype(int)).mean()

    for i in range(6):
        assert diff(against_orig[i], room, slice(28, 62)) < 6, "without it: the band is erased"
        assert diff(against_back[i], version, slice(28, 62)) < 4, "with it: the band survives"
        assert diff(against_back[i], version, slice(195, 245)) < 6, "the panel is cleared"


# restore_speaker's output bytes for the clips of _baseline_clips, recorded
# with the code as it was BEFORE backdrop/hold existed. Pinned to the ffmpeg /
# OpenCV / numpy they were recorded with, skipped elsewhere.
BEFORE_ENV = {"ffmpeg": "7.1-essentials_build-www.gyan.dev", "cv2": "4.13.0", "numpy": "2.4.4"}
BEFORE = {
    "plain": "17c09a07afa9cb71cb7f65598959c807fee095ab6039ea2a3c98c7cd66dd4de3",
    "every1": "f073bcd307d1e5e40dd54c404f5836c7c7a3fa0b3fdbb735a35b570972dda469",
    "veil": "9ccfdd84a2f5436b21ac33b1224c4a3e2d1eab1b9a863ffaa2591767a69d0071",
    "bars": "17c09a07afa9cb71cb7f65598959c807fee095ab6039ea2a3c98c7cd66dd4de3",
    "veil_keep": "bc879f281d9f8c21af90831a490368a5a121659eccef5a7b8b30e2e3e79b530b",
}


def _baseline_clips(tmp: Path) -> tuple[Path, Path]:
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

    orig = _write(tmp / "o.mp4", [frame(i, False) for i in range(n)])
    edited = _write(tmp / "e.mp4", [frame(i, True, veil=True) for i in range(n)])
    return orig, edited


def test_no_new_args_is_byte_identical(tmp_path, monkeypatch):
    version = subprocess.run([media.ffmpeg_exe(), "-version"], capture_output=True,
                             text=True).stdout.split()[2]
    env = {"ffmpeg": version, "cv2": cv2.__version__, "numpy": np.__version__}
    monkeypatch.setattr(speaker_front, "person_matte", _red)
    orig, edited = _baseline_clips(tmp_path)
    keep = np.zeros((288, 160), np.float32)
    keep[40:90, :] = 1.0
    cases = {"plain": {}, "every1": {"every": 1}, "veil": {"clear_veil_sec": 0.2},
             "bars": {"clear_bars": True}, "veil_keep": {"clear_veil_sec": 0.2, "veil_keep": keep}}
    got = {}
    for name, kw in cases.items():
        a = speaker_front.restore_speaker(edited, orig, tmp_path / f"{name}.mp4", **kw)
        b = speaker_front.restore_speaker(edited, orig, tmp_path / f"{name}_none.mp4", **kw,
                                          backdrop=None, hold=None)
        assert a.read_bytes() == b.read_bytes(), name
        got[name] = hashlib.sha256(a.read_bytes()).hexdigest()
    if env != BEFORE_ENV:
        pytest.skip(f"recorded with {BEFORE_ENV}, this is {env}")
    assert got == BEFORE, "restore_speaker with no backdrop/hold is not what it was"


def test_apply_passes_the_backdrop_and_hold_only_when_there_are_some(monkeypatch, tmp_path):
    from app.services.rebrand_harness import guards as gd

    calls = []
    monkeypatch.setattr(speaker_front, "restore_speaker",
                        lambda e, o, out, **kw: calls.append(sorted(kw)) or out)
    g = gd.ChunkGuards(speaker_front=True, clear_veil_sec=3.0, clear_bars=False)
    gd.apply(tmp_path / "e", tmp_path / "o", tmp_path / "x", g)
    gd.apply(tmp_path / "e", tmp_path / "o", tmp_path / "x", g, backdrop=tmp_path / "b",
             hold=np.ones((2, 2)))
    assert calls == [["clear_bars", "clear_veil_sec"],
                     ["backdrop", "clear_bars", "clear_veil_sec", "hold"]]


# ---------------------------------------------------------------------------
# media.conform / frame_count
# ---------------------------------------------------------------------------

def _pcm(path: Path) -> bytes:
    return subprocess.run([media.ffmpeg_exe(), "-loglevel", "error", "-i", str(path), "-map", "0:a",
                           "-f", "s16le", "-"], capture_output=True, check=True).stdout


def test_conform_pads_or_cuts_a_part_to_the_originals_frames_and_takes_its_audio(tmp_path):
    def shade(v):
        f = np.zeros((64, 48, 3), np.uint8)
        f[:] = (v, 0, 0)
        return f

    short = _write(tmp_path / "short.mp4", [shade(10 + i) for i in range(238)])
    # The part runs past the original's end: 239 frames, then 11 more of another shade.
    long = _write(tmp_path / "long.mp4", [shade(60 if i < 239 else 200) for i in range(250)])
    like = _write(tmp_path / "like.mp4", [np.full((64, 48, 3), 90, np.uint8)] * 239,
                  audio_sec=239 / FPS)
    assert media.frame_count(short) == 238 and media.frame_count(like) == 239
    padded = media.conform(short, like, tmp_path / "a.mp4")
    cut = media.conform(long, like, tmp_path / "b.mp4")
    for out in (padded, cut):
        assert media.frame_count(out) == 239
        assert _pcm(out) == _pcm(like), "the original part's own audio, untouched"
    frames = _read_all(padded)
    assert len(frames) == 239
    assert abs(int(frames[237][10, 10, 0]) - (10 + 237)) <= 4, "the part, frame for frame"
    assert np.array_equal(frames[238], frames[237]), "its last frame held"
    assert abs(int(frames[100][10, 10, 0]) - (10 + 100)) <= 4, "from its own first frame"
    assert abs(int(_read_all(cut)[-1][10, 10, 0]) - 60) <= 4, "cut at the original's end"
    assert not list(tmp_path.glob("*.part.mp4"))


# ---------------------------------------------------------------------------
# Lineage, the chain, the refusals
# ---------------------------------------------------------------------------

def test_a_spec_reads_back_from_a_request_or_the_options():
    assert hbase.BaseSpec.of(None) is None and hbase.BaseSpec.of({"mode": "x", "version_id": 1}) is None
    s = hbase.BaseSpec.of({"version_id": "7", "mode": "rebuild", "fresh": True})
    assert (s.version_id, s.mode, s.fresh) == (7, "rebuild", True)
    assert s.public() == {"version_id": 7, "mode": "rebuild", "fresh": True}
    stack = hbase.BaseSpec.of({"version_id": 7, "mode": "stack", "fresh": True})
    assert stack.fresh is False and stack.public() == {"version_id": 7, "mode": "stack"}


def _chain_rows():
    """v1 (31) fresh: structure; v2 (32) = Change it of v1: structure + bead
    row; v3 (33) = v2 + bead field on top; v4 (34) = v3 + another on top."""
    v1 = _version(31, recipe=_recipe(_pat()),
                  evidence={"omni_pattern": {"slug": "structure", "name": "Structure",
                                             "layout": lib.BUILTINS["structure"].layout.public()}})
    v2 = _version(32, recipe=_recipe(_pat(), _pat("pattern-2", "beads_row")),
                  base={"version_id": 31, "mode": "rebuild"}, take="31",
                  evidence={"omni_patterns": [
                      {"element_id": "pattern", "slug": "structure", "name": "Structure",
                       "layout": lib.BUILTINS["structure"].layout.public()},
                      {"element_id": "pattern-2", "slug": "beads_row", "name": "Bead row",
                       "layout": ROW_TOP_RIGHT.public()}]})
    v3 = _version(33, recipe=_recipe(_pat(slug="beads_field", anchor="edge-right"),
                                     director=rc.Director(head="harness")),
                  base={"version_id": 32, "mode": "stack"})
    v4 = _version(34, recipe=_recipe(_pat(slug="beads_row", anchor="edge-bottom"),
                                     director=rc.Director(head="harness")),
                  base={"version_id": 33, "mode": "stack"})
    return v1, v2, v3, v4


def test_the_versions_list_their_lineage_and_effective_layers(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.routers import rebrand_harness as rt

    v1, v2, v3, v4 = _chain_rows()
    listed = [v4, v3, v1]                                  # v2 fell off the list

    async def rows(run_id, limit=20, target=None):
        return listed

    async def same(e, live_ids):
        return e

    async def run_row(run_id):
        return {"metadata": {}}

    asked = []

    async def get(exp_id):
        asked.append(exp_id)
        return {32: v2}.get(exp_id)

    monkeypatch.setattr(store, "list_for_run", rows)
    monkeypatch.setattr(store, "mark_interrupted_if_orphaned", same)
    monkeypatch.setattr(store, "get_experiment", get)
    monkeypatch.setattr(pipeline.th, "_fetch_run_row", run_row)
    app = FastAPI()
    app.include_router(rt.router)
    body = TestClient(app).get("/api/rebrand/harness/versions",
                               params={"run_id": 1, "target": "scene_0"}).json()
    by_id = {v["id"]: v for v in body["versions"]}
    assert asked == [32], "an ancestor off the list is read once"
    assert by_id[31]["base"] is None and by_id[31]["depth"] == 0
    assert [(l["props"]["slug"], l["from_version"]) for l in by_id[31]["layers"]] == [("structure", 31)]
    assert by_id[33]["base"] == {"version_id": 32, "mode": "stack"} and by_id[33]["depth"] == 1
    assert [(l["props"]["slug"], l["from_version"]) for l in by_id[33]["layers"]] == [
        ("structure", 32), ("beads_row", 32), ("beads_field", 33)]
    assert by_id[33]["layers"][1]["placed"]["slot"] == "top-right"
    assert by_id[33]["layers"][2]["placed"] is None, "not run yet: where it went is unknown"
    assert by_id[34]["depth"] == 2 and len(by_id[34]["layers"]) == 4
    assert by_id[34]["layers"][-1]["from_version"] == 34


def test_a_version_is_built_on_only_when_it_can_be(monkeypatch, tmp_path):
    monkeypatch.setattr(pipeline, "REBRAND_ROOT", tmp_path)
    v1, v2, v3, v4 = _chain_rows()
    v5 = _version(35, recipe=_recipe(_pat(slug="beads_row", anchor="edge-top"),
                                     director=rc.Director(head="harness")),
                  base={"version_id": 34, "mode": "stack"})
    final = tmp_path / "final.mp4"
    final.write_bytes(b"x")
    for v in (v1, v2, v3, v4, v5):
        v["arms"]["omni"]["final_path"] = str(final)
    running = _version(40, status="running")
    failed = _version(41, status="failed")
    other = _version(42, run_id=2)
    gone = _version(43)
    _rows(monkeypatch, v1, v2, v3, v4, v5, running, failed, other, gone)
    monkeypatch.setattr(pipeline.th, "_abs_from_rel", lambda rel: Path(rel))

    def refused(vid, mode="stack", target="scene_0"):
        with pytest.raises(HTTPException) as err:
            asyncio.run(hbase.load(hbase.BaseSpec(vid, mode), 1, target))
        return err.value.status_code, err.value.detail

    assert refused(99) == (404, hbase.UNKNOWN)
    assert refused(40) == (409, "That version is still being made.")
    assert refused(41) == (409, hbase.NOT_MADE)
    assert refused(42) == (409, hbase.OTHER_CLIP)
    assert refused(31, target="scene_1") == (409, hbase.OTHER_CLIP)
    assert refused(43) == (410, "That version's video is gone — use Change it instead.")
    assert asyncio.run(hbase.load(hbase.BaseSpec(43, "rebuild"), 1, "scene_0")).version_id == 43, \
        "Change it needs no clips: it redoes from the original"
    bv = asyncio.run(hbase.load(hbase.BaseSpec(34, "stack"), 1, "scene_0"))
    assert bv.depth == 2 and [e["id"] for e in bv.chain] == [34, 33, 32]
    assert refused(35) == (409, hbase.TOO_DEEP), "three passes on top of the first is the most"
    assert asyncio.run(hbase.load(hbase.BaseSpec(35, "rebuild"), 1, "scene_0")).depth == 3


def test_the_pattern_cap_and_full_alone_count_the_versions_own(monkeypatch):
    async def lookup(slug):
        return lib.BUILTINS.get(slug)

    monkeypatch.setattr(lib, "lookup", lookup)
    v1, v2, v3, v4 = _chain_rows()
    chain = hbase.elements_of(hbase.layers_of([v3, v2]))
    assert [e.props["slug"] for e in chain] == ["structure", "beads_row", "beads_field"]
    [err] = asyncio.run(pattern_el.check(_recipe(_pat(slug="beads_row", anchor="edge-bottom")), chain))
    assert err["message"] == rc.pattern_cap_message()
    [err] = asyncio.run(pattern_el.check(_recipe(_pat(slug="grid")),
                                         hbase.elements_of(hbase.own_layers(v1))))
    assert "goes alone" in err["message"]
    assert asyncio.run(pattern_el.check(_recipe(_pat(slug="beads_row")),
                                        hbase.elements_of(hbase.own_layers(v1)))) == []


# ---------------------------------------------------------------------------
# "Change it": reuse what did not change
# ---------------------------------------------------------------------------

@pytest.fixture
def clip_run(tmp_path, monkeypatch):
    """A per-clip harness run (pipeline._omni_clip) of two parts over REAL
    files, Omni faked: the pattern built as the studio builds it. Each run
    works in its version's own folder (REBRAND_ROOT/<id>), as a real one."""
    monkeypatch.setattr(pipeline, "REBRAND_ROOT", tmp_path)
    calls: list = []
    chunks = [tmp_path / "c0.mp4", tmp_path / "c1.mp4"]
    for i, c in enumerate(chunks):
        c.write_bytes(b"chunk-" + str(i).encode())
    finished: dict = {}

    def extract_frame(video, out, *, at):
        Path(out).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(FRAME, out)
        return Path(out)

    async def person_mask(frame):
        return _person()

    async def edit_video(video, prompt, *, aspect, out, images=None):
        calls.append({"video": Path(video).name, "prompt": prompt})
        Path(out).parent.mkdir(parents=True, exist_ok=True)
        Path(out).write_bytes(b"omni-" + str(len(calls)).encode())
        return {"path": out}

    async def finish(ctx, arm, final, parts, extra):
        finished[ctx.exp_id] = extra

    monkeypatch.setattr(media, "split_chunks", lambda src, out_dir: chunks)
    monkeypatch.setattr(media, "extract_frame", extract_frame)
    monkeypatch.setattr(person_matte, "person_mask", person_mask)
    monkeypatch.setattr(omni, "edit_video", edit_video)
    monkeypatch.setattr(media, "fit_video_to_audio", lambda raw, c, out, **kw: out)
    monkeypatch.setattr(speaker_front, "restore_speaker", lambda e, o, out, **kw: out)
    monkeypatch.setattr(media, "concat", lambda parts, out: out)
    monkeypatch.setattr(store, "append_arm_step", _quiet)
    monkeypatch.setattr(store, "patch_arm", _quiet)
    monkeypatch.setattr(pipeline, "_finish_arm", finish)

    async def refs_for(recipe):
        job = pattern_el.PatternJob(exp_id=0, aspect="9:16", out_dir=tmp_path / "pattern",
                                    intro=INTRO, intro_is_still=False, seed=recipe.seed,
                                    plan_frame=lambda: _frame(), step=_quiet, colours=COLOURS)
        return await pattern_el.build_all(rc.pattern_elements(recipe), recipe.colours, job)

    async def _frame():
        return FRAME

    def run(exp_id: int, recipe: rc.Recipe, options: dict, base: dict | None = None,
            rows: tuple = (), brief: dict | None = None) -> dict:
        seg = pipeline.Segment(kind="scene", index=0, source=chunks[0], is_still=False)
        seg.base = chunks[0]
        ctx = pipeline.Ctx(exp_id=exp_id, run_id=1, aspect="9:16", width=720, height=1280,
                           work=tmp_path / str(exp_id), options={"target": "scene_0", **options,
                                                                 **({"base": base} if base else {})},
                           run_ctx={}, intro=None, scenes=[seg], outro=None)
        ctx.target_seg, ctx.recipe, ctx.harness = seg, recipe, True
        results = asyncio.run(refs_for(recipe))
        ctx.pattern_results = results
        ctx.pattern_refs = [r.ref for r in results if r.ref]
        ctx.pattern_ref = ctx.pattern_refs[0] if ctx.pattern_refs else None
        if rows:
            _rows(monkeypatch, *rows)
        asyncio.run(pipeline._link_base(ctx))
        # A brief written anew comes back in other words.
        kept = asyncio.run(pipeline._kept_brief(ctx))
        ctx.brief = kept if kept is not None else (
            brief or {"omni_scene_prompt": f"EDIT it, anew #{exp_id}."})
        asyncio.run(pipeline._omni_clip(ctx))
        return {"ctx": ctx, "stages": finished[exp_id]["stages"]}

    return {"run": run, "calls": calls, "tmp": tmp_path, "chunks": chunks}


def _made(recipe: rc.Recipe, exp_id: int, stages: dict, refs: list, **kw) -> dict:
    """The version row a finished run leaves (its evidence names its places)."""
    ev = ({"omni_pattern": refs[0].public()} if len(refs) == 1 else
          {"omni_pattern": refs[0].public(),
           "omni_patterns": [{**r.public(), "element_id": el.id}
                             for r, el in zip(refs, rc.pattern_elements(recipe))]})
    return _version(exp_id, recipe=recipe, stages=stages, evidence=ev, brief=dict(BRIEF), **kw)


def test_a_change_it_with_only_a_guard_changed_calls_omni_zero_times(clip_run):
    recipe = rc.preset_42_default("scene_0").model_copy(update={"seed": "studio:1:scene_0"})
    first = clip_run["run"](21, recipe, {}, None, brief=dict(BRIEF))
    assert len(clip_run["calls"]) == 2
    parent = _made(recipe, 21, first["stages"], first["ctx"].pattern_refs)
    guard_only = recipe.model_copy(update={"guards": rc.Guards(veil="off"), "preset": None})
    child = clip_run["run"](22, guard_only, {"take": hbase.take_of(parent)},
                            {"version_id": 21, "mode": "rebuild"}, rows=(parent,))
    assert len(clip_run["calls"]) == 2, "nothing Omni sees changed: no call"
    assert child["ctx"].brief == BRIEF, "the version's brief, not a new one"
    assert [child["stages"]["omni"][c]["reused"] for c in ("c0", "c1")] == [True, True]
    assert child["stages"]["guards"]["c0"]["clear_veil_sec"] == 0.0
    # Try again: a new take, nothing reused (and a new brief).
    again = clip_run["run"](23, guard_only, {}, {"version_id": 21, "mode": "rebuild", "fresh": True},
                            rows=(parent,))
    assert len(clip_run["calls"]) == 4 and again["ctx"].prior_stages == {}
    assert again["ctx"].brief != BRIEF


def test_a_change_it_reuses_the_unchanged_part_and_remakes_the_changed_one(clip_run):
    recipe = _recipe(_pat())
    first = clip_run["run"](21, recipe, {}, None, brief=dict(BRIEF))
    parent = _made(recipe, 21, first["stages"], first["ctx"].pattern_refs)
    fade = _recipe(_pat().model_copy(update={"animation": rc.Animation(entrance="fade")}))
    child = clip_run["run"](22, fade, {"take": "21"}, {"version_id": 21, "mode": "rebuild"},
                            rows=(parent,))
    calls = clip_run["calls"]
    assert [c["video"] for c in calls] == ["c0.mp4", "c1.mp4", "c0.mp4"], \
        "only the part carrying the entrance is made again"
    assert child["ctx"].brief == BRIEF, "same director, same places: the brief is kept"
    assert calls[-1]["prompt"] == BRIEF["omni_scene_prompt"] + hprompt.pattern_clause(
        child["ctx"].pattern_ref, entrance=True)
    # A moved pattern is what the brief was told about: a new brief.
    right = _recipe(_pat(anchor="edge-right"))
    moved = clip_run["run"](24, right, {"take": "21"}, {"version_id": 21, "mode": "rebuild"},
                            rows=(parent,))
    assert moved["ctx"].brief != BRIEF and len(calls) == 5


def test_a_change_it_of_a_colour_writes_a_new_brief(clip_run):
    """A brief's opening names the colour it was shown ("the attached purple
    line-art pattern"): a new colour, or a new palette, is a new brief — and
    every part is made again (their targets change anyway)."""
    recipe = _recipe(_pat())
    first = clip_run["run"](21, recipe, {}, None, brief=dict(BRIEF))
    refs = first["ctx"].pattern_refs
    parent = _made(recipe, 21, first["stages"], refs)
    assert hbase.brief_reusable(recipe, refs, {}, parent), "as it was: reusable"
    calls = clip_run["calls"]
    charcoal = clip_run["run"](22, _recipe(_pat(colour="#303030")), {"take": "21"},
                               {"version_id": 21, "mode": "rebuild"}, rows=(parent,))
    assert charcoal["ctx"].pattern_ref.colour == "#303030"
    assert charcoal["ctx"].brief != BRIEF, "the old brief names the old colour"
    assert len(calls) == 4, "both parts made again"
    brand = clip_run["run"](23, _recipe(_pat(), colours=rc.Colours(source="brand")), {"take": "21"},
                            {"version_id": 21, "mode": "rebuild"}, rows=(parent,))
    assert brand["ctx"].pattern_ref.colour != refs[0].colour
    assert brand["ctx"].brief != BRIEF, "a new palette is a new colour too"


def test_a_change_it_on_another_take_of_the_clip_writes_a_new_brief(clip_run):
    """The scene was made again since the version: its brief describes
    footage that is gone. (Its Omni parts are made again anyway — their keys
    hold the chunk's bytes.) A version whose frames are gone is judged on
    the rest, as before."""
    recipe = _recipe(_pat())
    first = clip_run["run"](21, recipe, {}, None, brief=dict(BRIEF))
    refs = first["ctx"].pattern_refs
    parent = _made(recipe, 21, first["stages"], refs)
    theirs = clip_run["tmp"] / "21" / "omni" / "scene_1_c0_first.png"
    assert theirs.is_file()
    other = clip_run["tmp"] / "other.png"
    with Image.open(FRAME) as im:
        Image.new("RGB", im.size, (250, 250, 250)).save(other)
    assert hbase.brief_reusable(recipe, refs, {}, parent, first=FRAME)
    assert not hbase.brief_reusable(recipe, refs, {}, parent, first=other)
    stacked = {**parent, "options": {**parent["options"], "base": {"version_id": 9, "mode": "stack"}}}
    assert not hbase.clip_changed(stacked, other), "drawn on a version's frames: unknown"
    shutil.copyfile(other, theirs)                      # the version saw another take
    child = clip_run["run"](22, recipe, {"take": "21"}, {"version_id": 21, "mode": "rebuild"},
                            rows=(parent,))
    assert child["ctx"].brief != BRIEF
    theirs.unlink()
    again = clip_run["run"](23, recipe, {"take": "21"}, {"version_id": 21, "mode": "rebuild"},
                            rows=(parent,))
    assert again["ctx"].brief == BRIEF


def test_start_records_the_base_past_normalize_options(monkeypatch, tmp_path):
    monkeypatch.setattr(pipeline, "REBRAND_ROOT", tmp_path)
    created: dict = {}
    v1, v2, v3, v4 = _chain_rows()
    v1["options"]["take"] = "t0"
    part = tmp_path / "31" / "omni" / "scene_1_c0_front.mp4"
    part.parent.mkdir(parents=True)
    part.write_bytes(b"x")
    _rows(monkeypatch, v1, v2, v3, v4)

    async def run_row(run_id):
        return {"metadata": {}}

    async def none(*a, **k):
        return []

    async def get(slug):
        return lib.BUILTINS.get(slug)

    async def create(run_id, options):
        created.clear()
        created.update(options)
        return {"id": 90, "run_id": run_id, "options": options}

    monkeypatch.setattr(pipeline.th, "_fetch_run_row", run_row)
    monkeypatch.setattr(omni, "providers", lambda: ["vertex"])
    monkeypatch.setattr(lib, "get", get)
    monkeypatch.setattr(lib, "lookup", get)
    monkeypatch.setattr(store, "list_for_run", none)
    monkeypatch.setattr(store, "create_experiment", create)
    monkeypatch.setattr(pipeline, "_run", _quiet)

    def start(recipe, base):
        async def go():
            exp = await pipeline.start(1, None, recipe=recipe, base=base)
            await asyncio.sleep(0)
            return exp
        return asyncio.run(go())

    start(_recipe(_pat()), {"version_id": 31, "mode": "rebuild"})
    assert created["base"] == {"version_id": 31, "mode": "rebuild"} and created["take"] == "t0"
    start(_recipe(_pat()), {"version_id": 31, "mode": "rebuild", "fresh": True})
    assert created["base"]["fresh"] is True and "take" not in created
    start(_recipe(_pat(slug="beads_row")), hbase.BaseSpec(31, "stack"))
    assert created["base"] == {"version_id": 31, "mode": "stack"} and "take" not in created
    assert created["recipe"]["director"]["head"] == "harness", "a pass on top opens with ours"
    with pytest.raises(HTTPException) as err:
        start(_recipe(), {"version_id": 31, "mode": "stack"})
    assert err.value.status_code == 400, "nothing to add"
    with pytest.raises(HTTPException) as err:
        start(_recipe(_pat(slug="grid")), {"version_id": 31, "mode": "stack"})
    assert err.value.status_code == 422 and "goes alone" in err.value.detail["errors"][0]["message"]
    with pytest.raises(HTTPException) as err:
        start(_recipe(_pat()), {"version_id": 99, "mode": "rebuild"})
    assert err.value.status_code == 404


def test_a_pass_on_top_writes_no_brief(monkeypatch, tmp_path):
    """_run with a stack base: build_brief is never called; the version's
    brief is stored (the self-review scores against it)."""
    v1, *_ = _chain_rows()
    v1["brief"] = {"omni_scene_prompt": "EDIT it, v1."}
    ctx = pipeline.Ctx(exp_id=91, run_id=1, aspect="9:16", width=720, height=1280, work=tmp_path,
                       options={"target": "scene_0"}, run_ctx={}, intro=None, scenes=[], outro=None)
    ctx.recipe, ctx.harness = _recipe(_pat(slug="beads_row"), director=rc.Director(head="harness")), True
    ctx.base = hbase.BaseVersion(spec=hbase.BaseSpec(31, "stack"), exp=v1, chain=[v1],
                                 layers=hbase.own_layers(v1))
    updates: list = []

    async def prepare(exp_id, run_id, options):
        return ctx

    async def never(**kw):
        raise AssertionError("a pass on top wrote a brief")

    async def update(exp_id, **fields):
        updates.append(fields)

    async def got(exp_id):
        return {"arms": {"omni": {"status": "ready"}}}

    monkeypatch.setattr(pipeline, "_prepare", prepare)
    monkeypatch.setattr(rebrand_brief, "build_brief", never)
    monkeypatch.setattr(store, "update_experiment", update)
    monkeypatch.setattr(store, "get_experiment", got)
    remembered: list = []

    async def memory(exp_id, text):
        remembered.append(exp_id)

    monkeypatch.setattr(store, "store_memory", memory)
    monkeypatch.setattr(store, "touch", _quiet)
    monkeypatch.setattr(pipeline, "_omni_clip", _quiet)
    asyncio.run(pipeline._run(91, 1, {"target": "scene_0", "run_omni": True}))
    stored = next(u for u in updates if "brief" in u)
    assert stored["brief"] == {"omni_scene_prompt": "EDIT it, v1."}
    assert remembered == [], "the version's brief is not remembered a second time"
    assert stored["evidence"]["base"] == {"version_id": 31, "mode": "stack", "depth": 1}
    assert stored["evidence"]["brief_from"] == 31
    assert updates[-1]["status"] == store.ST_READY


# ---------------------------------------------------------------------------
# "Add on top": the pass
# ---------------------------------------------------------------------------

def test_the_hold_is_the_new_layers_footprint_grown_and_feathered(tmp_path):
    ref = _ref("beads_row", ROW_TOP_RIGHT, tmp_path / "b")
    layers = runner.layers([ref], _recipe(_pat(slug="beads_row")))
    k = hbase.hold_mask(layers, 720, 1280)
    ink = hbase.new_ink(layers, 720, 1280) > 0.02
    assert k.shape == (1280, 720) and k.dtype == np.float32
    assert k[ink].max() == 0.0, "Omni's pass is kept on the new layer"
    assert k[1200:, :300].min() == 1.0, "far from it the version stays exactly"
    ys, xs = np.nonzero(ink)
    y, x = int(ys.max()) + 15, int(xs.min())
    assert 0.0 < float(k[min(y, 1279), x]) < 1.0 or float(k[min(y + 20, 1279), x]) < 1.0, "feathered"
    assert hbase.hold_mask([], 72, 128).min() == 1.0, "nothing new: all kept"


def _stack_version(tmp_path: Path, n_frames: int = 90) -> tuple[dict, Path, Path]:
    """A finished version of one part (the original's green room + a blue
    band laid in, entering over the first second) and the original part it
    was made from, 1 frame longer (as fit_video_to_audio leaves them)."""
    def original(i):
        return _room(i)

    def version(i):
        f = _room(i).astype(np.float32)
        a = min(1.0, i / FPS)
        f[10:50, :] = f[10:50, :] * (1 - a) + np.array(BLUE, np.float32) * a
        return np.clip(np.rint(f), 0, 255).astype(np.uint8)

    orig = _write(tmp_path / "scene0.mp4",
                  [original(i) for i in range(n_frames)], audio_sec=n_frames / FPS)
    run_dir = tmp_path / "rb" / "61" / "omni"
    run_dir.mkdir(parents=True)
    part = _write(run_dir / "scene_1_c0_front.mp4", [version(i) for i in range(n_frames - 1)])
    first = run_dir / "scene_1_c0_first.png"
    cv2.imwrite(str(first), original(0))
    tgt = run_dir / "scene_1_c0_target.png"
    t = original(0)
    t[10:50, :] = BLUE
    cv2.imwrite(str(tgt), t)
    exp = _version(61, recipe=_recipe(_pat()),
                   stages={"omni": {"c0": {"entering": True}}, "guards": {"c0": {"path": str(part)}},
                           "target": {"c0": {"path": str(tgt)}}},
                   evidence={"omni_pattern": {"slug": "structure", "name": "Structure",
                                              "layout": lib.BUILTINS["structure"].layout.public()}})
    return exp, orig, part


def test_a_versions_parts_are_conformed_to_the_original_and_settled(tmp_path, monkeypatch):
    monkeypatch.setattr(pipeline, "REBRAND_ROOT", tmp_path / "rb")
    monkeypatch.setattr(speaker_front, "person_matte", _red)
    exp, orig, part = _stack_version(tmp_path)
    bv = hbase.BaseVersion(spec=hbase.BaseSpec(61, "stack"), exp=exp, chain=[exp],
                           layers=hbase.own_layers(exp))
    asyncio.run(hbase.stack_parts(bv, [orig], tmp_path / "on_v61", width=W, height=H))
    [src], [frame] = bv.sources, bv.frames
    assert media.frame_count(part) == 89 and media.frame_count(src) == 90 == media.frame_count(orig)
    assert _pcm(src) == _pcm(orig), "the original part's audio"
    f = cv2.imread(str(frame))
    assert _near(f[15:45, 5:155], BLUE).mean() > 0.95, "the version's graphics, settled"
    x0, y0, x1, y1 = _box(0)
    assert _near(f[y0 + 3:y1 - 3, x0 + 3:x1 - 3], RED).mean() > 0.98, "the speaker from frame 0"
    assert bv.occupied.shape == (H, W) and bv.occupied[15:45].mean() > 0.95
    assert bv.occupied[100:].max() == 0.0, "its ink is the band, nothing else"
    assert bv.taken == {"left": "Structure"}
    again = hbase.BaseVersion(spec=bv.spec, exp=exp, chain=[exp], layers=bv.layers)
    before = src.stat().st_mtime_ns
    asyncio.run(hbase.stack_parts(again, [orig], tmp_path / "on_v61", width=W, height=H))
    assert src.stat().st_mtime_ns == before, "made once"
    # Another take of the clip: refused, in the owner's words.
    other = _write(tmp_path / "other.mp4", [np.full((H, W, 3), 200, np.uint8)] * 90)
    with pytest.raises(HTTPException) as err:
        asyncio.run(hbase.stack_parts(hbase.BaseVersion(spec=bv.spec, exp=exp, chain=[exp],
                                                        layers=bv.layers),
                                      [other], tmp_path / "on_v61b", width=W, height=H))
    assert err.value.status_code == 409 and err.value.detail == hbase.CLIP_CHANGED
    with pytest.raises(HTTPException) as err:
        asyncio.run(hbase.stack_parts(hbase.BaseVersion(spec=bv.spec, exp=exp, chain=[exp],
                                                        layers=bv.layers),
                                      [orig, orig], tmp_path / "on_v61c", width=W, height=H))
    assert err.value.status_code == 409


@pytest.fixture
def stack_run(tmp_path, monkeypatch):
    """runner._edit_stacked over a prepared base of two parts; Omni and the
    video tools faked, every call recorded."""
    calls: list = []
    restores: list = []
    base_dir = tmp_path / "base"
    base_dir.mkdir()
    chunks, sources, frames = [], [], []
    for j in range(2):
        chunks.append(tmp_path / f"chunk{j}.mp4")
        chunks[-1].write_bytes(b"orig-" + str(j).encode())
        sources.append(base_dir / f"c{j}_src.mp4")
        sources[-1].write_bytes(b"version-" + str(j).encode())
        frames.append(base_dir / f"c{j}_first.png")
        shutil.copyfile(FRAME, frames[-1])

    async def person_mask(frame):
        return _person()

    async def edit_video(video, prompt, *, aspect, out, images=None):
        calls.append({"video": Path(video), "prompt": prompt, "images": list(images or [])})
        Path(out).parent.mkdir(parents=True, exist_ok=True)
        Path(out).write_bytes(b"omni-" + str(len(calls)).encode())
        return {"path": out}

    def restore(e, o, out, **kw):
        restores.append({"original": Path(o), **kw})
        return out

    monkeypatch.setattr(person_matte, "person_mask", person_mask)
    monkeypatch.setattr(omni, "edit_video", edit_video)
    monkeypatch.setattr(media, "fit_video_to_audio", lambda raw, c, out, **kw: out)
    monkeypatch.setattr(speaker_front, "restore_speaker", restore)
    monkeypatch.setattr(media, "concat", lambda parts, out: out)
    monkeypatch.setattr(store, "append_arm_step", _quiet)
    monkeypatch.setattr(store, "patch_arm", _quiet)
    v1 = _version(51, recipe=_recipe(_pat()), brief={"omni_scene_prompt": "EDIT it, the brief."})
    bv = hbase.BaseVersion(spec=hbase.BaseSpec(51, "stack"), exp=v1, chain=[v1],
                           layers=hbase.own_layers(v1), chunks=chunks, sources=sources,
                           frames=frames)
    seg = pipeline.Segment(kind="scene", index=0, source=chunks[0], is_still=False)
    ctx = pipeline.Ctx(exp_id=52, run_id=1, aspect="9:16", width=720, height=1280, work=tmp_path,
                       options={"target": "scene_0"}, run_ctx={}, intro=None, scenes=[seg], outro=None)
    ctx.brief = dict(v1["brief"])
    ctx.recipe = _recipe(_pat(slug="beads_row"), director=rc.Director(head="harness"))
    ctx.pattern_refs = [_ref("beads_row", ROW_TOP_RIGHT, tmp_path / "p", "right_to_left")]
    ctx.pattern_ref = ctx.pattern_refs[0]
    ctx.base = bv

    def run(out_dir: str = "v2") -> st.Manifest:
        m = st.Manifest()
        asyncio.run(runner.edit_scene(ctx, "omni", seg, tmp_path / out_dir, "Scene 1", m))
        return m

    return {"run": run, "calls": calls, "restores": restores, "ctx": ctx, "bv": bv}


def test_a_pass_on_top_edits_the_versions_conformed_parts(stack_run):
    m = stack_run["run"]()
    calls, restores, bv = stack_run["calls"], stack_run["restores"], stack_run["bv"]
    ref = stack_run["ctx"].pattern_ref
    assert [c["video"] for c in calls] == bv.sources, "Omni edits the version, not the original"
    head = hprompt.harness_head()
    assert calls[0]["prompt"] == head + " " + hprompt.KEEP_CLAUSE + hprompt.pattern_clause(ref, entrance=True)
    assert calls[1]["prompt"] == head + " " + hprompt.KEEP_CLAUSE + hprompt.pattern_clause(ref, entrance=False)
    assert all("the brief" not in c["prompt"] for c in calls)
    # The target: the version's settled frame with only the new layer.
    want = runner.target(bv.frames[0], stack_run["ctx"].work / "want.png", [ref], stack_run["ctx"].recipe,
                         person=hbase.quantised(_person()))
    assert calls[0]["images"][0].read_bytes() == want.read_bytes()
    assert [r["original"] for r in restores] == bv.chunks, "the speaker from the TRUE original"
    assert [r["backdrop"] for r in restores] == bv.sources
    assert [r["clear_veil_sec"] for r in restores] == [speaker_front.VEIL_GUARD_SEC, 0.0]
    hold = restores[0]["hold"]
    assert isinstance(hold, np.ndarray) and hold.shape == (1280, 720)
    assert hold[1200:, :200].min() == 1.0 and hold[:200, 600:].min() < 0.05, "kept but for the row"
    stages = m.public()
    assert sorted(stages) == ["base", "guards", "omni", "target"]
    assert stages["base"]["c1"]["path"] == str(bv.sources[1]) and stages["base"]["c0"]["version"] == 51
    assert stages["guards"]["c0"]["hold"] is True and stages["guards"]["c0"]["speaker_front"] is True
    assert stages["target"]["c0"]["frame"] == str(bv.frames[0])
    assert [stages["omni"][c]["settles"] for c in ("c0", "c1")] == [True, False]


def test_a_pass_on_top_that_does_not_enter_still_settles_on_the_versions_entrance(stack_run):
    """The version under it entered on its first part, and the hold keeps
    that entrance: the pass's still is taken once it has settled."""
    ctx = stack_run["ctx"]
    ctx.pattern_refs = [dataclasses.replace(ctx.pattern_ref, animation="none")]
    ctx.pattern_ref = ctx.pattern_refs[0]
    stages = stack_run["run"]("v4").public()
    assert [r["clear_veil_sec"] for r in stack_run["restores"]] == [0.0, 0.0]
    assert [stages["omni"][c]["entering"] for c in ("c0", "c1")] == [False, False]
    exp = {"id": 52, "arms": {"omni": {"stages": stages}}}
    assert hbase.entering(exp, 0) is True and hbase.entering(exp, 1) is False


def test_a_part_with_nothing_new_is_the_versions_own(stack_run, monkeypatch):
    """A layer that shows nowhere in a part (its hold keeps everything)
    leaves that part the version's own — no Omni call, no guards."""
    monkeypatch.setattr(hbase, "hold_mask", lambda layers, w, h: np.ones((h, w), np.float32))
    m = stack_run["run"]("v3")
    assert stack_run["calls"] == [] and stack_run["restores"] == []
    stages = m.public()
    assert [stages["omni"][c]["kept"] for c in ("c0", "c1")] == [True, True]
    assert Path(stages["guards"]["c1"]["path"]).read_bytes() == b"version-1"


def test_a_pass_on_top_with_no_new_layer_fails_rather_than_copy_the_version(stack_run):
    """Every new pattern failed to build: the run fails (Try again), it does
    not end "ready" with a copy of the version."""
    ctx = stack_run["ctx"]
    ctx.pattern_refs, ctx.pattern_ref = [], None
    ctx.pattern_error = "the library could not be read"
    with pytest.raises(RuntimeError, match=r"nothing new could be drawn on version #51: the library"):
        stack_run["run"]("v5")
    assert stack_run["calls"] == [] and stack_run["restores"] == []


def test_a_pass_on_top_fails_when_the_version_cannot_be_kept(stack_run, monkeypatch):
    """The guards are what keep the version: when they fail, Omni's second
    pass (every pixel redrawn, strays included) is never shipped as it."""
    def broken(e, o, out, **kw):
        raise RuntimeError("cannot open b.mp4")

    monkeypatch.setattr(speaker_front, "restore_speaker", broken)
    with pytest.raises(RuntimeError, match=r"keeping version #51 as it was failed on part 1: cannot open"):
        stack_run["run"]("v6")
    assert len(stack_run["calls"]) == 1, "stopped at the part it failed on"


def test_a_pass_on_top_clears_the_bars_whatever_the_new_layer_asks(stack_run):
    """The structure strip keeps MODNet's specks in the bars on a fresh run
    ('auto'); on top of a version it cannot — they would let the original's
    black bar through over the version's own graphics there."""
    ctx = stack_run["ctx"]
    ctx.recipe = _recipe(_pat(), director=rc.Director(head="harness"))
    ctx.pattern_refs = [_ref("structure", lib.BUILTINS["structure"].layout, ctx.work / "s")]
    ctx.pattern_ref = ctx.pattern_refs[0]
    assert not ctx.pattern_ref.clears_bars
    stages = stack_run["run"]("v7").public()
    assert [r["clear_bars"] for r in stack_run["restores"]] == [True, True]
    assert all(stages["guards"][c]["clear_bars"] is True for c in ("c0", "c1"))


def test_the_versions_graphics_in_a_bar_survive_a_pass_on_top(tmp_path, monkeypatch):
    """Pixels: a letterboxed clip whose version drew a line in the bottom
    bar; the speaker matte leaves specks in the bars (as MODNet does). With
    the stack guards the bar is the version's, exactly; with the fresh
    run's guards for the same layer (structure: bars not cleared) the
    original's black shows through the line."""
    from app.services.rebrand_harness import guards as gd

    def specky(bgr):
        m = _red(bgr)
        m[:40] = 0.5
        m[248:] = 0.5
        return m

    monkeypatch.setattr(speaker_front, "person_matte", specky)
    n = 6

    def original(i):
        f = _room(i)
        f[:40], f[248:] = 0, 0
        return f

    def version(i):
        f = original(i)
        f[260:270, 10:150] = BLUE                 # its graphic, in the bar
        return f

    def omni_pass(i):
        f = version(i)
        f[262:268, 10:150] = (120, 120, 120)      # redrawn off
        f[70:100, 100:150] = MAGENTA              # the new layer
        return f

    orig = _write(tmp_path / "o.mp4", [original(i) for i in range(n)])
    back = _write(tmp_path / "b.mp4", [version(i) for i in range(n)])
    edited = _write(tmp_path / "e.mp4", [omni_pass(i) for i in range(n)])
    hold = np.ones((H, W), np.float32)
    hold[60:110, 90:160] = 0.0
    recipe = _recipe(_pat(), director=rc.Director(head="harness"))
    ref = _ref("structure", lib.BUILTINS["structure"].layout, tmp_path / "s")
    stacked = runner.stack_guards(recipe, [ref], entering=False)
    fresh = gd.for_chunk(recipe.guards, [ref], entering=False)
    assert stacked.clear_bars and not fresh.clear_bars
    kept = _read_all(gd.apply(edited, orig, tmp_path / "k.mp4", stacked, backdrop=back, hold=hold))
    holed = _read_all(gd.apply(edited, orig, tmp_path / "h.mp4", fresh, backdrop=back, hold=hold))
    for f, g in zip(kept, holed):
        assert _near(f[261:269, 12:148], BLUE).mean() > 0.98, "the version's line, as it was"
        assert _near(f[72:98, 102:148], MAGENTA).mean() > 0.98, "the new layer"
        assert _near(g[261:269, 12:148], BLUE).mean() < 0.5, "without: the bar's black through it"


# ---------------------------------------------------------------------------
# The studio: a preview on the version is the run's target
# ---------------------------------------------------------------------------

@pytest.fixture
def studio_on(tmp_path, monkeypatch):
    """A studio scene of one part over a synthetic original, a finished
    version of it, and the run's pieces — nothing paid, the speaker a red
    block for every mask (MODNet's stand-in, as a function of the frame)."""
    monkeypatch.setattr(studio, "STUDIO_ROOT", tmp_path / "studio")
    monkeypatch.setattr(pipeline, "REBRAND_ROOT", tmp_path / "rb")
    for name in ("_PREPARED", "_MASK_TASKS", "_PREVIEWS", "_BASES", "_STACKS", "_RUN_MASKS"):
        monkeypatch.setattr(studio, name, {})
    monkeypatch.setattr(speaker_front, "person_matte", _red)

    async def red_mask(frame):
        return _red(cv2.imread(str(frame))) * 0.8

    monkeypatch.setattr(person_matte, "local_mask", red_mask)
    monkeypatch.setattr(person_matte, "person_mask", red_mask)
    monkeypatch.setattr(pattern_ref, "dig_colours", lambda *a, **k: COLOURS)
    exp, orig, part = _stack_version(tmp_path)
    _rows(monkeypatch, exp)
    work = tmp_path / "studio" / "1" / "scene_0_abc"
    (work / "base").mkdir(parents=True)
    base_clip = work / "base" / "scene0.mp4"
    shutil.copyfile(orig, base_clip)
    first = work / "c0_first.png"
    media.extract_frame(base_clip, first, at="first")
    (work / "colours.json").write_text(json.dumps({
        "ground": COLOURS.ground, "ink": COLOURS.ink, "fell_back": False, "frame": None,
        "colour": COLOURS.colour}), encoding="utf-8")
    intro = pipeline.Segment(kind="intro", index=0, source=INTRO, is_still=False)
    seg = pipeline.Segment(kind="scene", index=0, source=base_clip, is_still=False)
    seg.base = base_clip
    ctx = pipeline.Ctx(exp_id=0, run_id=1, aspect="9:16", width=W, height=H, work=work,
                       options={}, run_ctx={}, intro=intro, scenes=[seg], outro=None)
    ctx.target_seg = seg
    ctx.frames["th_first"] = first
    scene = studio.Scene(run_id=1, target="scene_0", aspect="9:16", width=W, height=H, work=work,
                         ctx=ctx, chunks=[base_clip], firsts=[first], seconds=[3.0])

    async def prepare(run_id, target):
        return scene

    monkeypatch.setattr(studio, "prepare", prepare)
    return {"scene": scene, "exp": exp, "orig": orig, "tmp": tmp_path}


def test_a_preview_on_a_version_is_drawn_on_its_frames_and_is_the_runs_target(studio_on, monkeypatch):
    recipe = _recipe(_pat(slug="beads_row"))
    spec = hbase.BaseSpec(61, "stack")
    info = asyncio.run(studio.base_scene(1, "scene_0", 61))
    assert info["version_id"] == 61 and info["depth"] == 0
    assert info["chunks"][0]["frame_url"].endswith("/on_v61/c0_first.png")
    assert [l["props"]["slug"] for l in info["layers"]] == ["structure"]
    body = asyncio.run(studio.preview(1, "scene_0", recipe, base=spec))
    fresh = asyncio.run(studio.preview(1, "scene_0", recipe))
    [c] = body["chunks"]
    assert c["frame_url"] == info["chunks"][0]["frame_url"], "on the version's frame, never the original's"
    assert c["target_url"] != fresh["chunks"][0]["target_url"], "its own key"
    assert body["base"] == {"version_id": 61, "mode": "stack"} and body["omni_parts"] == [0]
    assert c["guards"]["hold"] is True and c["guards"]["clear_bars"] is True, "as the run's"
    assert hprompt.KEEP_CLAUSE in c["prompt"] and c["prompt"].startswith(hprompt.HARNESS_HEAD)
    assert body["patterns"][0]["slot"] != "left", "the structure's strip is taken"
    tgt = studio.resolve_file(c["target_url"].split("/file/", 1)[1])
    shown = cv2.imread(str(tgt))
    assert _near(shown[15:45, 5:155], BLUE).mean() > 0.6, "the version's band is in the picture"

    # The run, from the same files: its first part's target is the same bytes.
    exp = studio_on["exp"]
    run_work = studio_on["tmp"] / "rb" / "62"
    run_chunk = run_work / "base" / "scene0.mp4"
    run_chunk.parent.mkdir(parents=True)
    shutil.copyfile(studio_on["orig"], run_chunk)
    seg = pipeline.Segment(kind="scene", index=0, source=run_chunk, is_still=False)
    seg.base = run_chunk
    ctx = pipeline.Ctx(exp_id=62, run_id=1, aspect="9:16", width=W, height=H, work=run_work,
                       options={"target": "scene_0", "base": spec.public()}, run_ctx={},
                       intro=pipeline.Segment(kind="intro", index=0, source=INTRO, is_still=False),
                       scenes=[seg], outro=None)
    ctx.target_seg, ctx.harness = seg, True
    ctx.recipe = studio.on_top(recipe)
    asyncio.run(pipeline._link_base(ctx))
    asyncio.run(hbase.stack_parts(ctx.base, [run_chunk], run_work / "base" / "on_v61",
                                  width=W, height=H))
    monkeypatch.setattr(store, "append_arm_step", _quiet)
    monkeypatch.setattr(store, "patch_arm", _quiet)
    asyncio.run(pipeline._build_pattern_ref(ctx))
    assert ctx.pattern_ref.layout.public() == body["patterns"][0]["layout"], "placed alike"
    seen = {}

    async def edit_video(video, prompt, *, aspect, out, images=None):
        seen["video"], seen["prompt"], seen["images"] = Path(video), prompt, list(images)
        Path(out).write_bytes(b"omni")
        return {"path": out}

    monkeypatch.setattr(omni, "edit_video", edit_video)
    monkeypatch.setattr(media, "fit_video_to_audio", lambda raw, c, out, **kw: out)
    monkeypatch.setattr(speaker_front, "restore_speaker", lambda e, o, out, **kw: out)
    asyncio.run(runner.edit_scene(ctx, "omni", seg, run_work / "omni", "Scene 1", st.Manifest()))
    assert seen["images"][0].read_bytes() == tgt.read_bytes(), "preview == run"
    assert seen["prompt"] == c["prompt"]
    assert seen["video"] == ctx.base.sources[0] and media.frame_count(seen["video"]) == 90


def test_the_base_route_and_its_refusals(studio_on, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.routers import rebrand_harness as rt

    async def lookup(slug):
        return lib.BUILTINS.get(slug)

    monkeypatch.setattr(lib, "lookup", lookup)
    monkeypatch.setattr(lib, "get", lookup)
    app = FastAPI()
    app.include_router(rt.router)
    client = TestClient(app)
    r = client.post("/api/rebrand/harness/base", json={"run_id": 1, "target": "scene_0", "version_id": 61})
    assert r.status_code == 200
    body = r.json()
    assert set(body) == {"version_id", "chunks", "layers", "depth", "max_depth"}
    assert body["chunks"][0]["index"] == 0 and body["layers"][0]["from_version"] == 61
    running = _version(63, status="running")
    _rows(monkeypatch, studio_on["exp"], running)
    r = client.post("/api/rebrand/harness/base", json={"run_id": 1, "target": "scene_0", "version_id": 63})
    assert r.status_code == 409 and r.json()["detail"] == "That version is still being made."
    seen = {}

    async def preview(run_id, target, recipe, base=None):
        seen["recipe"], seen["base"] = recipe, base
        return {"ok": True}

    monkeypatch.setattr(studio, "preview", preview)
    el = {"id": "pattern", "kind": "pattern", "props": {"slug": "beads_row", "layout": None, "colour": "auto"}}
    r = client.post("/api/rebrand/harness/preview", json={
        "run_id": 1, "target": "scene_0", "recipe": {"elements": [el]},
        "base": {"version_id": 61, "mode": "stack"}})
    assert r.status_code == 200 and seen["base"] == hbase.BaseSpec(61, "stack")
    assert seen["recipe"].director.head == "harness"
    grid = {**el, "props": {**el["props"], "slug": "grid"}}
    r = client.post("/api/rebrand/harness/preview", json={
        "run_id": 1, "target": "scene_0", "recipe": {"elements": [grid]},
        "base": {"version_id": 61, "mode": "stack"}})
    assert r.status_code == 422 and "goes alone" in r.json()["detail"]["errors"][0]["message"]


# ---------------------------------------------------------------------------
# The studio: a "Change it" preview counts the parts Omni must make
# ---------------------------------------------------------------------------

@pytest.fixture
def change_it(tmp_path, monkeypatch):
    """A two-part studio scene and the parent version made by a REAL runner
    pass over the same chunks and frames (Omni faked), in its own folder."""
    monkeypatch.setattr(studio, "STUDIO_ROOT", tmp_path / "studio")
    monkeypatch.setattr(pipeline, "REBRAND_ROOT", tmp_path)
    for name in ("_PREPARED", "_MASK_TASKS", "_PREVIEWS", "_BASES", "_STACKS", "_RUN_MASKS"):
        monkeypatch.setattr(studio, name, {})

    async def mask(frame):
        return _person()

    monkeypatch.setattr(person_matte, "local_mask", mask)
    monkeypatch.setattr(person_matte, "person_mask", mask)
    monkeypatch.setattr(studio, "_run_mask", mask)
    monkeypatch.setattr(pattern_ref, "dig_colours", lambda *a, **k: COLOURS)
    work = tmp_path / "studio" / "1" / "scene_0_abc"
    work.mkdir(parents=True)
    chunks, firsts = [], []
    for j in range(2):
        chunks.append(work / f"c{j}.mp4")
        chunks[-1].write_bytes(b"chunk-" + str(j).encode())
        firsts.append(work / f"c{j}_first.png")
        shutil.copyfile(FRAME, firsts[-1])
    (work / "colours.json").write_text(json.dumps({
        "ground": COLOURS.ground, "ink": COLOURS.ink, "fell_back": False, "frame": None,
        "colour": COLOURS.colour}), encoding="utf-8")
    intro = pipeline.Segment(kind="intro", index=0, source=INTRO, is_still=False)
    seg = pipeline.Segment(kind="scene", index=0, source=chunks[0], is_still=False)
    sctx = pipeline.Ctx(exp_id=0, run_id=1, aspect="9:16", width=720, height=1280, work=work,
                        options={}, run_ctx={}, intro=intro, scenes=[seg], outro=None)
    sctx.target_seg = seg
    sctx.frames["th_first"] = firsts[0]
    scene = studio.Scene(run_id=1, target="scene_0", aspect="9:16", width=720, height=1280, work=work,
                         ctx=sctx, chunks=chunks, firsts=firsts, seconds=[8.0, 8.0])

    async def prepare(run_id, target):
        return scene

    monkeypatch.setattr(studio, "prepare", prepare)
    calls: list = []

    def extract_frame(video, out, *, at):
        Path(out).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(FRAME, out)
        return Path(out)

    async def edit_video(video, prompt, *, aspect, out, images=None):
        calls.append(Path(video).name)
        Path(out).parent.mkdir(parents=True, exist_ok=True)
        Path(out).write_bytes(b"omni-" + str(len(calls)).encode())
        return {"path": out}

    monkeypatch.setattr(media, "split_chunks", lambda src, out_dir: chunks)
    monkeypatch.setattr(media, "extract_frame", extract_frame)
    monkeypatch.setattr(omni, "edit_video", edit_video)
    monkeypatch.setattr(media, "fit_video_to_audio", lambda raw, c, out, **kw: out)
    monkeypatch.setattr(speaker_front, "restore_speaker", lambda e, o, out, **kw: out)
    monkeypatch.setattr(media, "concat", lambda parts, out: out)
    monkeypatch.setattr(store, "append_arm_step", _quiet)
    monkeypatch.setattr(store, "patch_arm", _quiet)

    async def plan():
        return firsts[0]

    def run(exp_id: int, recipe: rc.Recipe, *, take: str, brief: dict, prior=None) -> tuple:
        """runner.edit_scene over the scene's own chunks: the refs as the
        studio builds them (same colours, seed, plan frame)."""
        job = pattern_el.PatternJob(exp_id=0, aspect="9:16", out_dir=tmp_path / f"p{exp_id}",
                                    intro=INTRO, intro_is_still=False, seed=recipe.seed,
                                    plan_frame=plan, step=_quiet, colours=COLOURS)
        results = asyncio.run(pattern_el.build_all(rc.pattern_elements(recipe), recipe.colours, job))
        refs = [r.ref for r in results if r.ref]
        rseg = pipeline.Segment(kind="scene", index=0, source=chunks[0], is_still=False)
        ctx = pipeline.Ctx(exp_id=exp_id, run_id=1, aspect="9:16", width=720, height=1280,
                           work=tmp_path / str(exp_id), options={"take": take}, run_ctx={},
                           intro=None, scenes=[rseg], outro=None)
        ctx.recipe, ctx.brief = recipe, brief
        ctx.pattern_refs, ctx.pattern_ref = refs, refs[0] if refs else None
        m = st.Manifest(prior)
        asyncio.run(runner.edit_scene(ctx, "omni", rseg, tmp_path / str(exp_id) / "omni", "Scene 1", m))
        return m, refs

    return {"run": run, "calls": calls}


def test_a_change_it_preview_counts_only_the_parts_omni_must_make(change_it, monkeypatch):
    recipe = _recipe(_pat())
    m, refs = change_it["run"](71, recipe, take="71", brief=dict(BRIEF))
    parent = _made(recipe, 71, m.public(), refs)
    _rows(monkeypatch, parent)
    spec = hbase.BaseSpec(71, "rebuild")

    def parts(r: rc.Recipe, s=spec) -> list:
        return asyncio.run(studio.preview(1, "scene_0", r, base=s))["omni_parts"]

    assert parts(recipe) == [], "nothing changed: every part reused"
    assert parts(recipe.model_copy(update={"guards": rc.Guards(veil="off")})) == [], "guards only"
    fade = _recipe(_pat().model_copy(update={"animation": rc.Animation(entrance="fade")}))
    assert parts(fade) == [0], "the entrance is on the first part only"
    assert parts(_recipe(_pat(colour="#303030"))) == [0, 1], "a new colour: every target"
    noted = _recipe(_pat(), director=rc.Director(notes="calmer"))
    assert parts(noted) == [0, 1], "a new brief comes back in other words"
    harness = _recipe(_pat(), director=rc.Director(head="harness"))
    assert parts(harness) == [0, 1]
    assert parts(recipe, hbase.BaseSpec(71, "rebuild", fresh=True)) == [0, 1], "Try again"
    body = asyncio.run(studio.preview(1, "scene_0", fade, base=spec))
    assert "omni_keys" not in body and body["base"] == {"version_id": 71, "mode": "rebuild"}

    # The run agrees: the fade Change it calls Omni for part one only.
    before = len(change_it["calls"])
    change_it["run"](72, fade, take="71", brief=dict(BRIEF), prior=m.public())
    assert change_it["calls"][before:] == ["c0.mp4"]
    # A clip of the version's gone since: that part is made again.
    Path(m.public()["omni"]["c1"]["path"]).unlink()
    assert parts(recipe) == [1]


def test_a_change_it_preview_expects_a_new_brief_on_another_take_of_the_clip(change_it, tmp_path):
    recipe = _recipe(_pat())
    m, refs = change_it["run"](71, recipe, take="71", brief=dict(BRIEF))
    parent = _made(recipe, 71, m.public(), refs)
    bv = hbase.BaseVersion(spec=hbase.BaseSpec(71, "rebuild"), exp=parent, chain=[parent], layers=[])
    other = tmp_path / "other.png"
    with Image.open(FRAME) as im:
        Image.new("RGB", im.size, (250, 250, 250)).save(other)
    assert studio._run_opening(recipe, refs, bv, FRAME) == BRIEF["omni_scene_prompt"]
    assert studio._run_opening(recipe, refs, bv, other) is None, "the run writes a new brief"


def test_the_run_masks_the_studio_keeps_are_bounded(monkeypatch, tmp_path):
    monkeypatch.setattr(studio, "_RUN_MASKS", {})
    made: list = []

    def local(frame):
        made.append(Path(frame).name)
        return np.zeros((4, 4), np.float32)

    monkeypatch.setattr(person_matte, "_local_mask", local)
    frames = []
    for i in range(studio.RUN_MASKS_KEPT + 3):
        frames.append(tmp_path / f"f{i}.png")
        frames[-1].write_bytes(b"x")
    for f in frames:
        asyncio.run(studio._run_mask(f))
    assert len(studio._RUN_MASKS) == studio.RUN_MASKS_KEPT
    asyncio.run(studio._run_mask(frames[3]))            # held: not made again, now the newest
    assert made.count("f3.png") == 1
    asyncio.run(studio._run_mask(frames[0]))            # dropped: made again
    assert made.count("f0.png") == 2
    assert len(studio._RUN_MASKS) == studio.RUN_MASKS_KEPT
    assert str(frames[3]) in {k[0] for k in studio._RUN_MASKS} and \
        str(frames[4]) not in {k[0] for k in studio._RUN_MASKS}, "the least recent goes"
