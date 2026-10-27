"""The studio shows exactly what Generate makes (the in-house engine, I9).

  the still     a draft made in house shows, on Next, the design still: the
                scene's frame 0 with the plate the run lays over it, at
                rest (house.design_still) — array_equal to the still made
                from the very spec the run pipes
  the moments   studio.moment(t) is the run's own frame: the footage's
                frame i with plate_at(i) over it, array_equal to the plates
                the run pipes to ffmpeg, at 0, mid-entrance and 4 s
  the render    POST /house/render renders the draft exactly as Generate
                will (the same render key), once (single-flight), stopped
                by an edit; a Generate of the same draft copies it, byte
                for byte, and renders nothing
  base.still    a version made in house whose still is gone gets its design
                still again, the very picture its run wrote
  the masks     the speaker track samples either side of a jump cut and
                never blends across one; the moving preview is told the
                cuts; the matte bank's own masks come with the camera

MODNet is faked by the speaker's green, the pattern kit's masks are real
(skipped when absent), and every paid client raises. Nothing touches the
database, a bucket or a server."""
from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
import threading
from pathlib import Path

import numpy as np
import pytest

from app.services import asset_storage
from app.services import omni_client as omni
from app.services import pattern_library as lib
from app.services import person_matte
from app.services import rebrand_brief
from app.services import rebrand_media as media
from app.services import rebrand_pattern_ref as pattern_ref
from app.services import rebrand_pipeline as pipeline
from app.services import rebrand_store as store
from app.services import speaker_front
from app.services import vertex_client
from app.services.rebrand_harness import base as hbase
from app.services.rebrand_harness import house
from app.services.rebrand_harness import plate as pl
from app.services.rebrand_harness import recipe as rc
from app.services.rebrand_harness import speaker_track as T
from app.services.rebrand_harness import studio

cv2 = pytest.importorskip("cv2")
# Made in house: run with Omni switched off, as a server has it (conftest,
# I16) — nothing here may depend on Omni.
pytestmark = pytest.mark.omni_default

W, H, FPS = 360, 640, 30
N = 128                                    # 4.27 s: frames 0, 15 (mid-entrance) and 120 (4 s)
PICK = (0, 15, 120)
GREEN = (40, 200, 60)                      # the "speaker", BGR
REAL_MASKS = pattern_ref._mask_path("structure").exists()
PLAN_KEY = "0123456789abcdef"
needs_kit = pytest.mark.skipif(not REAL_MASKS, reason="the pattern kit's masks are not here")


@pytest.fixture(autouse=True)
def paid(monkeypatch, tmp_path):
    """Every paid client raises and is remembered; MODNet is the green; the
    studio's and the pipeline's folders are the test's own."""
    asked: list[str] = []

    def refusing(name):
        async def refuse(*_a, **_k):
            asked.append(name)
            raise AssertionError(f"a paid call in a test: {name}")
        return refuse

    for mod, name in ((omni, "edit_video"), (omni, "interpolate"), (vertex_client, "gemini_multimodal"),
                      (vertex_client, "gemini_image"), (vertex_client, "gemini_embed"),
                      (person_matte, "person_mask"), (rebrand_brief, "build_brief"),
                      (rebrand_brief, "critique"), (store, "store_memory")):
        monkeypatch.setattr(mod, name, refusing(name))
    monkeypatch.setattr(speaker_front, "raw_matte", _fake_raw)
    monkeypatch.setattr(studio, "ANALYSIS_ROOT", tmp_path / "_cache" / "scene")
    monkeypatch.setattr(studio, "STUDIO_ROOT", tmp_path / "_studio")
    for name in ("_ANALYSES", "_PREPARED", "_PREVIEWS", "_MASK_TASKS", "_HOUSE_SPECS", "_DRAFTS", "_CLAIMED"):
        monkeypatch.setattr(studio, name, {})
    monkeypatch.setattr(pipeline, "REBRAND_ROOT", tmp_path / "rebrand")
    return asked


def _fake_raw(bgr) -> np.ndarray:
    d = np.abs(bgr.astype(np.int16) - np.array(GREEN, np.int16)).max(axis=2)
    g = (d < 40).astype(np.float32)
    return cv2.resize(g, (max(1, bgr.shape[1] // 2), max(1, bgr.shape[0] // 2)), interpolation=cv2.INTER_AREA)


def _texture(seed: int = 3) -> np.ndarray:
    r = np.random.default_rng(seed)
    grey = r.random((H // 6, W // 6)).astype(np.float32)
    tint = r.random((H // 24, W // 24, 3)).astype(np.float32)
    t = (cv2.resize(grey, (W, H), interpolation=cv2.INTER_CUBIC)[..., None] * 0.55
         + cv2.resize(tint, (W, H), interpolation=cv2.INTER_CUBIC) * 0.3)
    t = cv2.GaussianBlur(t, (0, 0), 1.5)
    t[:, :, 1] *= 0.6
    return np.clip(t * 255, 0, 255).astype(np.uint8)


def _scene_clip(path: Path, n: int = N) -> Path:
    """A room pushing in 1.00 -> 1.06 about its centre, the speaker standing
    in it (swaying a little), and a tone — H.264 + AAC, as a scene clip is."""
    tex = _texture()
    path.parent.mkdir(parents=True, exist_ok=True)
    proc = subprocess.Popen([media.ffmpeg_exe(), "-y", "-loglevel", "error", "-f", "rawvideo", "-pix_fmt", "bgr24",
                             "-s", f"{W}x{H}", "-r", str(FPS), "-i", "-", "-f", "lavfi", "-i",
                             "sine=frequency=330:sample_rate=44100", "-map", "0:v", "-map", "1:a", "-shortest",
                             "-c:v", "libx264", "-preset", "ultrafast", "-crf", "12", "-pix_fmt", "yuv420p",
                             "-c:a", "aac", str(path)], stdin=subprocess.PIPE)
    cx, cy = (W - 1) / 2, (H - 1) / 2
    for i in range(n):
        s = 1.0 + 0.06 * i / (n - 1)
        f = cv2.warpAffine(tex, np.array([[s, 0, (1 - s) * cx], [0, s, (1 - s) * cy]]), (W, H),
                           flags=cv2.INTER_CUBIC, borderMode=cv2.BORDER_REFLECT)
        sway = int(round(12 * np.sin(i / 9.0)))
        cv2.ellipse(f, (W // 2 + 30 + sway, int(H * 0.62)), (W // 5, H // 4), 0, 0, 360, GREEN, -1)
        proc.stdin.write(np.ascontiguousarray(f).tobytes())
    proc.stdin.close()
    assert proc.wait(timeout=120) == 0
    return path


def _described(clip: Path, cuts=()) -> Path:
    media.scene_info_path(clip).write_text(json.dumps(
        {"source_fps": FPS, "source_cut": None, "trimmed_sec": 0.0, "cuts": list(cuts)}), encoding="utf-8")
    return clip


def _res(gid="code", frm=0.3, settle=0.5667, to=3.2) -> dict:
    return rc.IconResolved.model_validate({
        "glyph": {"source": "library", "id": gid}, "label": "Code", "from_sec": frm, "settle_sec": settle,
        "to_sec": to, "box": {"x": 0.62, "y": 0.06, "w": 0.2, "h": 0.1125}, "fill": "#7f2bf2", "ink": "#ffffff",
        "fx": {"entrance": "pop", "seed": f"s:{gid}"}, "style": "circle", "look_v": 1, "weight": "bold",
    }).model_dump(mode="json")


def _recipe(*, colour: str = "auto", icons: bool = True) -> rc.Recipe:
    els = [rc.Element(id="pattern", kind="pattern", props={"slug": "structure", "layout": None, "colour": colour})]
    if icons:
        els.append(rc.Element(id="icons", kind="icon", props={"style": "circle"}))
    return rc.normalize(rc.Recipe(engine="house", target="scene_0", seed="studio:1:scene_0", elements=els))


async def _quiet(*_a, **_k):
    return None


@pytest.fixture
def scene(tmp_path, monkeypatch):
    """Run 1's scene_0 prepared in the studio as _prepare leaves it: the
    normalised clip (described: no cut) in its scene_base folder, its first
    frame, the intro's colours dug; the speaker's masks from the green; the
    icon plan shown kept (a frozen circle) and nothing warmed that would
    listen (Whisper) or ask the director."""
    source = _scene_clip(tmp_path / "src" / "scene0.mp4")
    work = studio.scene_base(1, "scene_0", source)
    clip = work / "base" / "scene0.mp4"
    clip.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, clip)
    _described(clip)
    first = work / "c0_first.png"
    media.extract_frame(clip, first, at="first")
    seg = pipeline.Segment(kind="scene", index=0, source=source, is_still=False)
    seg.base = clip
    ctx = pipeline.Ctx(exp_id=0, run_id=1, aspect="9:16", width=W, height=H, work=work,
                       options=pipeline.normalize_options({"target": "scene_0"}), run_ctx={"topic": "t"},
                       intro=None, scenes=[seg], outro=None)
    ctx.target_seg = seg
    sc = studio.Scene(run_id=1, target="scene_0", aspect="9:16", width=W, height=H, work=work, ctx=ctx,
                      chunks=[clip], firsts=[first], seconds=[round(N / FPS, 2)], plan_frame=first,
                      colours=pattern_ref.Colours(ground="#3d3c3d", ink="#f4f4f8", fell_back=False, frame=None,
                                                  colour="#7f2bf2"))
    studio._PREPARED[(1, "scene_0")] = sc

    async def local_mask(frame):
        img = cv2.imread(str(frame), cv2.IMREAD_COLOR)
        return cv2.resize(_fake_raw(img), (img.shape[1], img.shape[0]), interpolation=cv2.INTER_LINEAR)

    async def get(slug):
        return lib.BUILTINS.get(slug)

    monkeypatch.setattr(person_matte, "local_mask", local_mask)
    monkeypatch.setattr(lib, "get", get)
    monkeypatch.setattr(lib, "lookup", get)
    monkeypatch.setattr(studio, "_icon_warm", lambda *a, **k: None)
    studio._write(studio._icon_dir(sc) / "plans" / f"{PLAN_KEY}.json", {"plan": {}, "resolved": [_res()]})
    return sc


def _decoded(clip: Path, i: int) -> np.ndarray:
    return studio._frame_of(clip, i)


def _png(url: str) -> np.ndarray:
    return cv2.imread(str(studio.resolve_file(url.split("/file/", 1)[1])), cv2.IMREAD_COLOR)


def _generate(monkeypatch, scene, recipe: rc.Recipe, exp_id: int, *, spy=None) -> dict:
    """One Generate of `recipe` (as the router stores it) through
    pipeline._run on the scene, the database faked; house.render spied."""
    rows: dict = {"patches": [], "updates": []}

    async def patch_arm(eid, arm, patch):
        rows["patches"].append(dict(patch))

    async def update(eid, **fields):
        rows["updates"].append(fields)

    async def get(eid):
        return {"id": eid, "arms": {"omni": {"status": store.ARM_READY}}}

    async def persist(*, abs_path, bucket_key, mime=None):
        return None, None

    monkeypatch.setattr(store, "patch_arm", patch_arm)
    monkeypatch.setattr(store, "append_arm_step", _quiet)
    monkeypatch.setattr(store, "update_experiment", update)
    monkeypatch.setattr(store, "get_experiment", get)
    monkeypatch.setattr(store, "touch", _quiet)
    monkeypatch.setattr(asset_storage, "persist_local_file", persist)
    options = {**pipeline.normalize_options({"target": "scene_0"}), "recipe": recipe.model_dump(mode="json"),
               "engine": "house"}
    seg0 = scene.ctx.target_seg

    async def prepare(eid, run_id, opts):
        # The run's own copy of the studio's clip (pipeline._scene_base).
        base = pipeline.REBRAND_ROOT / str(eid) / "base" / "scene0.mp4"
        base.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(seg0.base, base)
        shutil.copyfile(media.scene_info_path(seg0.base), media.scene_info_path(base))
        seg = pipeline.Segment(kind="scene", index=0, source=seg0.source, is_still=False, base=base)
        ctx = pipeline.Ctx(exp_id=eid, run_id=run_id, aspect="9:16", width=W, height=H,
                           work=pipeline.REBRAND_ROOT / str(eid), options=opts, run_ctx={"topic": "t"},
                           intro=None, scenes=[seg], outro=None)
        ctx.recipe = pipeline._recipe_of(opts)
        ctx.harness = True
        ctx.target_seg = seg
        await pipeline._build_pattern_ref(ctx)
        rows["ctx"] = ctx
        return ctx

    monkeypatch.setattr(pipeline, "_prepare", prepare)
    if spy is not None:
        monkeypatch.setattr(house, "render", spy)
    asyncio.run(pipeline._run(exp_id, 1, options))
    done = next(p for p in rows["patches"] if p.get("status") == store.ARM_READY)
    rows["render"] = done["stages"]["render"]["c0"]
    return rows


# ---------------------------------------------------------------------------
# Next: the design still; the moments; preview == run
# ---------------------------------------------------------------------------

@needs_kit
def test_next_and_every_moment_are_the_frames_the_run_makes(scene, monkeypatch, paid):
    recipe = _recipe()
    body = asyncio.run(studio.preview(1, "scene_0", recipe))
    assert body["engine"] == "house" and body["omni_parts"] == [] and body["kept"] is True
    h = body["house"]
    assert body["chunks"][0]["target_url"] == h["still_url"] and h["still_url"].endswith(studio.HOUSE_STILL)
    assert body["opening"] == "" and body["chunks"][0]["prompt"] == "", "nothing is told anything"
    [layer] = h["layers"]
    assert layer["element_id"] == "pattern" and layer["follow"] == "room"
    assert layer["timing"]["t_in"] == 0.0 and layer["timing"]["t_full"] == 1.0 and layer["timing"]["runs_to_end"]
    art = cv2.imread(str(studio.resolve_file(layer["art_url"].split("/file/", 1)[1])), cv2.IMREAD_UNCHANGED)
    assert art.shape == (H, W, 4) and art[..., 3].any(), "the pattern's art, uncut"

    stored = asyncio.run(studio.as_generated(1, "scene_0", recipe, None, PLAN_KEY))
    plates: dict[int, np.ndarray] = {}
    specs: dict = {}
    real = house.render

    def spy(spec, source, out, **kw):
        specs["run"] = spec
        return real(spec, source, out, on_plate=lambda i, p: plates.__setitem__(i, p.copy()) if i in PICK else None,
                    **kw)

    ran = _generate(monkeypatch, scene, stored, 41, spy=spy)
    assert paid == [], f"paid calls: {paid}"
    assert sorted(plates) == sorted(PICK) and not ran["render"]["reused"]
    run_spec = specs["run"]

    # Next's still == the run's own spec, at rest over frame 0 (the icons
    # are the stage's own sprites: the still is the patterns alone).
    bank = studio._analysis(scene).bank
    patterns_only = pl.PlateSpec(width=W, height=H, fps=run_spec.fps, n=run_spec.n, layers=run_spec.layers,
                                 bars=run_spec.bars, speaker_front=run_spec.speaker_front)
    want = pl.over_frame(_decoded(scene.ctx.target_seg.base, 0),
                         pl.plate_at(patterns_only, 0, bank=bank, settled=True, at=house.still_moment(run_spec)))
    assert np.array_equal(_png(h["still_url"]), want), "Next is the run's plate over frame 0"
    assert h["at"] == pytest.approx(1.0)

    # Each moment is the run's frame: its plate, array_equal, over frame i.
    for i in PICK:
        got = asyncio.run(studio.moment(1, "scene_0", recipe, i / FPS, None, plan_key=PLAN_KEY))
        assert got["frame"] == i and got["key"] == ran["render"]["key"], "the very spec the run renders"
        frame = _decoded(scene.ctx.target_seg.base, i)
        assert np.array_equal(_png(got["url"]), pl.over_frame(frame, plates[i])), f"frame {i}"
        phases = {ly["element_id"]: ly["phase"] for ly in got["ingredients"]["layers"]}
        assert phases["pattern"] == ("hold" if i == 120 else "in")
        a, b = got["ingredients"]["camera"][:2]
        assert (a, b) == (1.0, 0.0) if i == 0 else a > 1.0, "the camera: identity at 0, pushing in after"
    # Mid-entrance really is mid-entrance: not yet the pattern fully in.
    track = studio._analysis(scene).track
    full = pl.plate_at(run_spec, 15, camera=track, bank=bank, settled=True, at=0.5)
    assert plates[15][..., 3].any() and not np.array_equal(plates[15], full)
    assert paid == []


@needs_kit
def test_a_ready_draft_render_is_what_generate_copies(scene, monkeypatch, paid):
    recipe = _recipe()
    renders: list = []
    real = house.render

    def counted(spec, source, out, **kw):
        renders.append(Path(out).name)
        return real(spec, source, out, **kw)

    monkeypatch.setattr(house, "render", counted)

    async def ask():
        a, b = await asyncio.gather(studio.house_render(1, "scene_0", recipe, None, PLAN_KEY),
                                    studio.house_render(1, "scene_0", recipe, None, PLAN_KEY))
        await studio._DRAFTS[a["key"]].task
        return a, b, studio.house_render_state(1, "scene_0", a["key"])

    first, second, state = asyncio.run(ask())
    assert first["key"] == second["key"] and len(renders) == 1, "single-flight: one render"
    assert state["state"] == "ready" and state["url"].endswith(f"{first['key']}.mp4")
    draft = house.draft_path(scene.work, first["key"])
    assert draft.is_file()
    again = asyncio.run(studio.house_render(1, "scene_0", recipe, None, PLAN_KEY))
    assert again["state"] == "ready" and len(renders) == 1, "asked again: the file, not a render"

    def never(*_a, **_k):
        raise AssertionError("rendered again")

    stored = asyncio.run(studio.as_generated(1, "scene_0", recipe, None, PLAN_KEY))
    ran = _generate(monkeypatch, scene, stored, 42, spy=never)
    assert ran["render"]["reused"] is True and ran["render"]["key"] == first["key"]
    assert Path(ran["render"]["path"]).read_bytes() == draft.read_bytes(), "byte for byte"
    assert paid == []


@needs_kit
def test_an_edit_stops_the_draft_render_it_made_stale(scene, monkeypatch, paid):
    real = house.render
    calls: list = []

    def counted(spec, source, out, **kw):
        calls.append(Path(out).name)
        return real(spec, source, out, **kw)

    monkeypatch.setattr(house, "render", counted)
    violet, purple = _recipe(colour="#7f2bf2"), _recipe(colour="#8d01fa")

    async def edit():
        a = await studio.house_render(1, "scene_0", violet, None, PLAN_KEY)
        b = await studio.house_render(1, "scene_0", purple, None, PLAN_KEY)
        da, db = studio._DRAFTS[a["key"]], studio._DRAFTS[b["key"]]
        await asyncio.gather(*(t for t in (da.task, db.task) if t is not None), return_exceptions=True)
        stale = (da.state, da.out.exists(), studio.house_render_state(1, "scene_0", a["key"])["state"])
        # The edit undone: the first is asked for again — and made again.
        again = await studio.house_render(1, "scene_0", violet, None, PLAN_KEY)
        state = again["state"]
        await studio._DRAFTS[a["key"]].task
        return a, b, stale, db, state

    a, b, stale, db, state = asyncio.run(edit())
    assert a["key"] != b["key"]
    assert stale == ("cancelled", False, "cancelled"), "the stale one stopped, no file"
    assert db.state == "ready" and db.out.is_file()
    assert db.out.name in calls
    assert state in ("queued", "running")
    assert studio.house_render_state(1, "scene_0", a["key"])["state"] == "ready"
    assert paid == []


@needs_kit
def test_an_ask_overtaken_by_a_newer_one_stops_nothing(scene, monkeypatch, paid):
    """An edit, then a quick undo while the first draft still renders: the
    edit's ask is resolved last (its spec is the slow one) and must neither
    stop the render of the draft the user is back on nor start its own."""
    real, go = house.render, threading.Event()

    def held(spec, source, out, **kw):
        go.wait(timeout=120)
        return real(spec, source, out, **kw)

    monkeypatch.setattr(house, "render", held)
    violet, purple = _recipe(colour="#7f2bf2"), _recipe(colour="#8d01fa")
    spec_of = studio.house_spec

    async def edit_then_undo():
        slow = asyncio.Event()

        async def house_spec(run_id, target, stored, base=None):
            if any(e.props.get("colour") == "#8d01fa" for e in stored.elements):
                await slow.wait()
            return await spec_of(run_id, target, stored, base)

        monkeypatch.setattr(studio, "house_spec", house_spec)
        a = await studio.house_render(1, "scene_0", violet, None, PLAN_KEY)
        edit = asyncio.ensure_future(studio.house_render(1, "scene_0", purple, None, PLAN_KEY))
        await asyncio.sleep(0)                  # the edit is asked first...
        undo = await studio.house_render(1, "scene_0", violet, None, PLAN_KEY)
        slow.set()                              # ...and resolved last
        b = await edit
        d = studio._DRAFTS[a["key"]]
        then = d.state
        go.set()
        await d.task
        return a, undo, b, then, d

    try:
        a, undo, b, then, d = asyncio.run(edit_then_undo())
    finally:
        go.set()
    assert undo["key"] == a["key"] != b["key"]
    assert then in ("queued", "running"), "the render of the draft on screen was not stopped"
    assert b["state"] == "cancelled" and b["key"] not in studio._DRAFTS, "the overtaken ask started nothing"
    assert d.state == "ready" and d.out.is_file()
    assert paid == []


@needs_kit
def test_a_house_versions_lost_still_is_made_again_the_same(scene, monkeypatch, paid):
    recipe = _recipe()
    stored = asyncio.run(studio.as_generated(1, "scene_0", recipe, None, PLAN_KEY))
    ran = _generate(monkeypatch, scene, stored, 43)
    written = hbase.still_path(43, 0)
    assert written.is_file(), "the run wrote its design still"
    kept = cv2.imread(str(written), cv2.IMREAD_COLOR)
    written.unlink()
    row = {"id": 43, "run_id": 1, "status": "ready",
           "options": {"target": "scene_0", "engine": "house", "recipe": stored.model_dump(mode="json")},
           "arms": {"omni": {"status": "ready", "engine": "house", "stages": {
               "omni": {"c0": {"path": ran["render"]["path"], "entering": True}}}}}}

    def no_video(*_a, **_k):
        raise AssertionError("taken from the video")

    monkeypatch.setattr(hbase, "settled_first", no_video)
    again = asyncio.run(hbase.still(row, 0))
    assert np.array_equal(cv2.imread(str(again), cv2.IMREAD_COLOR), kept), "the very picture its run wrote"


# ---------------------------------------------------------------------------
# The preview key; the router
# ---------------------------------------------------------------------------

def test_the_preview_is_version_7_and_a_house_draft_keys_what_it_draws():
    # 5: the design still (I9); 6: what the handles need (I13); 7: when each
    # pattern shows (I14).
    assert studio.PREVIEW_VERSION == 7
    omni_r = rc.preset_42_default("scene_0")
    house_r = rc.preset_42_house("scene_0")
    assert studio._preview_key(house_r, "s") != studio._preview_key(omni_r, "s")
    noted = house_r.model_copy(update={"director": rc.Director(head="harness", notes="calm")})
    assert studio._preview_key(noted, "s") == studio._preview_key(house_r, "s"), "nothing is told anything"
    stray = house_r.model_copy(update={"guards": rc.Guards(stray="clean")})
    assert studio._preview_key(stray, "s") == studio._preview_key(house_r, "s")
    front = house_r.model_copy(update={"guards": rc.Guards(speaker_front=False)})
    assert studio._preview_key(front, "s") != studio._preview_key(house_r, "s"), "the speaker's cut shows"


def test_the_router_renders_and_moments_only_drafts_made_in_house(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.routers import rebrand_harness as rt

    seen: dict = {}

    async def lookup(slug):
        return lib.BUILTINS.get(slug)

    async def house_render(run_id, target, recipe, base=None, plan_key=None):
        seen["render"] = (run_id, target, recipe.engine, base, plan_key)
        return {"key": "k" * 20, "state": "queued", "progress": 0.0}

    async def moment(run_id, target, recipe, t, base=None, plan_key=None):
        seen.setdefault("moment", []).append((t, base, plan_key))
        return {"url": "/x.png", "frame": 3, "t": 0.1, "key": "k", "ingredients": {}}

    def state(run_id, target, key):
        return {"key": key, "state": "running", "progress": 0.5}

    monkeypatch.setattr(lib, "lookup", lookup)
    monkeypatch.setattr(studio, "house_render", house_render)
    monkeypatch.setattr(studio, "moment", moment)
    monkeypatch.setattr(studio, "house_render_state", state)
    app = FastAPI()
    app.include_router(rt.router)
    c = TestClient(app)
    house_r = rc.preset_42_house("scene_0").model_dump(mode="json")
    r = c.post("/api/rebrand/harness/house/render",
               json={"run_id": 1, "target": "scene_0", "recipe": house_r, "icons_plan_key": PLAN_KEY})
    assert r.status_code == 200 and seen["render"] == (1, "scene_0", "house", None, PLAN_KEY)
    omni_r = rc.preset_42_default("scene_0").model_dump(mode="json")
    r = c.post("/api/rebrand/harness/house/render", json={"run_id": 1, "target": "scene_0", "recipe": omni_r})
    # Omni switched off (I16): refused as a recipe for Omni; switched back
    # on, it still has no render to play.
    assert r.status_code == 400 and r.json()["detail"]["message"] == rc.OMNI_OFF
    monkeypatch.setenv("REBRAND_OMNI", "on")
    r = c.post("/api/rebrand/harness/house/render", json={"run_id": 1, "target": "scene_0", "recipe": omni_r})
    assert r.status_code == 400 and r.json()["detail"] == studio.HOUSE_ONLY
    monkeypatch.delenv("REBRAND_OMNI")
    assert c.get(f"/api/rebrand/harness/house/render/{'k' * 20}",
                 params={"run_id": 1, "target": "scene_0"}).json()["progress"] == 0.5
    r = c.post("/api/rebrand/harness/house/moment", json={"run_id": 1, "target": "scene_0", "recipe": house_r, "t": 2.5})
    assert r.status_code == 200 and seen["moment"][-1] == (2.5, None, None)
    # /icons/moment for a draft made in house is the in-house frame too
    # (added on top of a version: on that version).
    r = c.post("/api/rebrand/harness/icons/moment", json={"run_id": 1, "target": "scene_0", "recipe": house_r,
                                                         "plan_key": PLAN_KEY, "t": 1.0, "version_id": 7})
    assert r.status_code == 200
    t, base, key = seen["moment"][-1]
    assert (t, base.version_id, base.mode, key) == (1.0, 7, "stack", PLAN_KEY)


# ---------------------------------------------------------------------------
# The stand-in moves as the plate moves (its exact parts)
# ---------------------------------------------------------------------------

PLATE_FIXTURE = Path(__file__).parent / "fixtures" / "rebrand_plate.json"
REPO = Path(__file__).resolve().parents[2]


def test_the_stand_in_eases_and_slides_as_the_plate_does():
    """tests/fixtures/rebrand_plate.json is the plate's own easing and
    slides (a change to plate.py must update it: scratchpad make_plate_
    fixture.py), and lib/rebrandPlate.ts answers it alike (frontend/scripts/
    check-rebrand-plate.mjs; node 22.6+, skipped where there is none)."""
    fx = json.loads(PLATE_FIXTURE.read_text(encoding="utf-8"))
    for c in fx["shown_share"]:
        assert pl.shown_share(c["direction"], c["q"]) == pytest.approx(c["expect"], abs=1e-12)
    for c in fx["slide_offset"]:
        got = pl.slide_offset(c["edge"], tuple(c["box"]), tuple(c["margin"]), tuple(c["size"]), c["hidden"])
        assert list(got) == pytest.approx(c["expect"], abs=1e-9)
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed here")
    version = subprocess.run([node, "--version"], capture_output=True, text=True, timeout=30).stdout.strip()
    major, minor = (int(x) for x in version.lstrip("v").split(".")[:2])
    if (major, minor) < (22, 6):
        pytest.skip(f"node {version} cannot strip types")
    out = subprocess.run([node, "--experimental-strip-types", "scripts/check-rebrand-plate.mjs"],
                         capture_output=True, text=True, timeout=120, cwd=REPO / "frontend")
    assert out.returncode == 0, out.stdout + out.stderr
    assert "cases agree" in out.stdout


# ---------------------------------------------------------------------------
# The masks: cut-aware; the bank's own with the camera
# ---------------------------------------------------------------------------

def _cut_clip(path: Path, *, n: int = 60, cut: int = 37) -> Path:
    """A speaker who stands left, then — after a jump cut at `cut` — right."""
    path.parent.mkdir(parents=True, exist_ok=True)
    vw = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"mp4v"), FPS, (96, 160))
    for i in range(n):
        f = np.full((160, 96, 3), 120, np.uint8)
        x = 10 if i < cut else 56
        f[40:150, x:x + 30] = GREEN
        vw.write(f)
    vw.release()
    return path


def _green(bgr) -> np.ndarray:
    d = np.abs(bgr.astype(np.int16) - np.array(GREEN, np.int16)).max(axis=2)
    return (d < 40).astype(np.float32)


def test_the_speaker_track_samples_each_side_of_a_cut_and_never_blends_across(tmp_path):
    clip = _cut_clip(tmp_path / "scene0.mp4")
    plain = T.build(clip, tmp_path / "plain", matte=_green, cuts=[])
    assert T.TRACK_VERSION == 2 and plain.cuts == []
    assert list(np.round(plain.times * FPS).astype(int)) == [0, 15, 30, 45], "no cut: the grid alone, as before"
    tr = T.build(clip, tmp_path / "cut", matte=_green, cuts=[37])
    frames = list(np.round(tr.times * FPS).astype(int))
    assert frames == [0, 15, 30, 36, 37, 45] and tr.cuts == [37]
    assert [s["id"] for s in tr.spots] == [s["id"] for s in plain.spots] and tr.frame0.read_bytes() == \
        plain.frame0.read_bytes(), "the room read is the same"
    left, right = tr.masks[3].astype(np.float32) / 255, tr.masks[4].astype(np.float32) / 255
    just_before, just_after = 36.5 / FPS, 37.01 / FPS
    assert np.array_equal(tr.mask_at(just_before), left) and np.array_equal(tr.mask_at(just_after), right)
    assert tr.blend_at(40 / FPS)[:2] == (4, 5), "after the cut, within its shot"
    # A window across the cut sees both poses.
    assert tr.samples_in(1.1, 1.3) == [2, 3, 4, 5]
    assert tr.samples_in(1.21, 1.22) == [3, 4]
    again = T.load(tmp_path / "cut", tr.key)
    assert again is not None and again.cuts == [37] and np.array_equal(again.times, tr.times)
    # A clip with no cut keeps version 1's key and file (nothing made again,
    # no icon plan's key moved); one with a cut is version 2.
    meta = json.loads((tmp_path / "plain" / "track.json").read_text(encoding="utf-8"))
    assert meta["version"] == 1 and plain.key == T.track_key(clip, []) != tr.key == T.track_key(clip, [37])
    assert json.loads((tmp_path / "cut" / "track.json").read_text(encoding="utf-8"))["version"] == 2
    # The uneven formula agrees with the even one on a track with no cut.
    even = T.load(tmp_path / "plain", plain.key)
    for t0, t1 in ((0.0, 0.0), (0.5, 0.5), (0.6, 1.2), (-1.0, 9.0), (0.49, 1.01)):
        even.cuts = []
        want = even.samples_in(t0, t1)
        even.cuts = [999]                       # the uneven path, no cut inside
        assert even.samples_in(t0, t1) == want, (t0, t1)


def test_the_moving_preview_is_told_the_cuts(tmp_path, monkeypatch):
    monkeypatch.setattr(studio, "STUDIO_ROOT", tmp_path)
    clip = _cut_clip(tmp_path / "1" / "scene_0_x" / "base" / "scene0.mp4")
    work = tmp_path / "1" / "scene_0_x"
    tr = T.build(clip, work / "icons", matte=_green, cuts=[37])
    seg = pipeline.Segment(kind="scene", index=0, source=clip, is_still=False)
    seg.base = clip
    ctx = pipeline.Ctx(exp_id=0, run_id=1, aspect="9:16", width=96, height=160, work=work, options={}, run_ctx={},
                       intro=None, scenes=[seg], outro=None)
    ctx.target_seg = seg
    sc = studio.Scene(run_id=1, target="scene_0", aspect="9:16", width=96, height=160, work=work, ctx=ctx,
                      chunks=[clip], firsts=[], seconds=[2.0])
    masks = studio._track_masks(sc, tr)
    assert masks["cuts"] == [round(37 / FPS, 4)] and len(masks["times"]) == 6


def test_the_camera_comes_with_the_banks_own_masks(tmp_path, monkeypatch, scene):
    async def analysed():
        await studio._analyse_scene(scene)
        return await studio.house_camera(1, "scene_0")

    body = asyncio.run(analysed())
    assert body["state"] == "ready"
    m = body["masks"]
    assert m["step"] is True and m["cuts"] == [] and m["times"][0] == 0.0
    bank = studio._analysis(scene).bank
    assert len(m["times"]) == len({bank.keys_of(i) for i in range(bank.n)}), "one sample per keyframe window"
    strip = cv2.imread(str(studio.resolve_file(m["url"].split("/file/", 1)[1])), cv2.IMREAD_GRAYSCALE)
    k = 5
    i = int(round(m["times"][k] * FPS))
    want = np.clip(np.rint(cv2.resize(bank.at(i, bars=bank.bars), (m["width"], m["height"]),
                                      interpolation=cv2.INTER_AREA) * 255), 0, 255).astype(np.uint8)
    assert np.array_equal(strip[k * m["height"]:(k + 1) * m["height"]], want)
    assert all(bank.keys_of(j) == bank.keys_of(i) for j in range(i, int(round(m["times"][k + 1] * FPS))))
