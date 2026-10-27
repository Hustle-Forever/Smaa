"""The golden lock on the in-house engine (engine "house"): a scene made by
laying our own graphics over its untouched footage, for $0.

  the footage   outside the plate the output IS the source: its YUV within
                42 dB, no shift (a 1 s synthetic room zooming like the
                owner's clips, a green "speaker", a tone), every frame, and
                the sound byte for byte (its decoded MD5)
  $0            a whole Generate of a house recipe through the pipeline
                (_run) with every paid client raising: Omni, the brief, the
                critique, the memory embed, the Gemini matte — none is asked
  preview=run   the pattern the preview planned and the one the run draws
                from Generate's freeze are the same pixels; the plates the
                run pipes to ffmpeg are array_equal to the plates made again
                on their own from the scene's cached analysis (what the
                studio's own renders are made from)
  old versions  an Omni version (run 815's v25 recipe) still lists, plays
                and is read as it always was; a house version is read as
                one part, with its still, and never as one Omni drew icons on
  the rules     a house recipe needs no Omni provider, is refused on top of
                a version made in house (Change it), freezes its patterns at
                Generate; REBRAND_OMNI=off refuses Omni; rating one embeds
                nothing; a stopped render leaves no file behind

MODNet is faked by the speaker's green (it is local, but slow); nothing here
touches the database, a bucket or a server."""
from __future__ import annotations

import asyncio
import dataclasses
import json
import subprocess
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
from app.services.rebrand_harness import camera
from app.services.rebrand_harness import house
from app.services.rebrand_harness import icon_render as ir
from app.services.rebrand_harness import mattes
from app.services.rebrand_harness import plate as pl
from app.services.rebrand_harness import recipe as rc
from app.services.rebrand_harness import studio
from app.services.rebrand_harness.elements import pattern as pattern_el

cv2 = pytest.importorskip("cv2")

W, H, N, FPS = 360, 640, 36, 30
GREEN = (40, 200, 60)                     # the "speaker", BGR
PICK = (0, 5, 17, 30, N - 1)              # frames compared plate for plate
STORED = json.loads((Path(__file__).parent / "fixtures" / "rebrand_recipes" / "stored_815.json")
                    .read_text(encoding="utf-8"))["versions"]
REAL_MASKS = pattern_ref._mask_path("structure").exists()


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
    monkeypatch.setattr(studio, "_ANALYSES", {})
    monkeypatch.setattr(pipeline, "REBRAND_ROOT", tmp_path / "rebrand")
    return asked


def _fake_raw(bgr) -> np.ndarray:
    d = np.abs(bgr.astype(np.int16) - np.array(GREEN, np.int16)).max(axis=2)
    g = (d < 40).astype(np.float32)
    return cv2.resize(g, (max(1, bgr.shape[1] // 2), max(1, bgr.shape[0] // 2)), interpolation=cv2.INTER_AREA)


def _texture(seed: int = 3) -> np.ndarray:
    r = np.random.default_rng(seed)
    # A room: plenty of corners in its brightness, calmer in its colour (as
    # footage is — a noise of pure colour would be harder on any encoder's
    # chroma than a real room).
    grey = r.random((H // 6, W // 6)).astype(np.float32)
    tint = r.random((H // 24, W // 24, 3)).astype(np.float32)
    t = (cv2.resize(grey, (W, H), interpolation=cv2.INTER_CUBIC)[..., None] * 0.55
         + cv2.resize(tint, (W, H), interpolation=cv2.INTER_CUBIC) * 0.3)
    t = cv2.GaussianBlur(t, (0, 0), 1.5)
    t[:, :, 1] *= 0.6
    return np.clip(t * 255, 0, 255).astype(np.uint8)


def _scene(path: Path) -> Path:
    """A room pushing in 1.00 -> 1.06 about its centre, the speaker standing
    in it, and a tone — H.264 + AAC, as a scene clip is."""
    tex = _texture()
    path.parent.mkdir(parents=True, exist_ok=True)
    proc = subprocess.Popen([media.ffmpeg_exe(), "-y", "-loglevel", "error", "-f", "rawvideo", "-pix_fmt", "bgr24",
                             "-s", f"{W}x{H}", "-r", str(FPS), "-i", "-", "-f", "lavfi", "-i",
                             "sine=frequency=330:sample_rate=44100", "-map", "0:v", "-map", "1:a", "-shortest",
                             "-c:v", "libx264", "-preset", "ultrafast", "-crf", "12", "-pix_fmt", "yuv420p",
                             "-c:a", "aac", str(path)], stdin=subprocess.PIPE)
    cx, cy = (W - 1) / 2, (H - 1) / 2
    for i in range(N):
        s = 1.0 + 0.06 * i / (N - 1)
        f = cv2.warpAffine(tex, np.array([[s, 0, (1 - s) * cx], [0, s, (1 - s) * cy]]), (W, H),
                           flags=cv2.INTER_CUBIC, borderMode=cv2.BORDER_REFLECT)
        cv2.ellipse(f, (W // 2 + 30, int(H * 0.62)), (W // 5, H // 4), 0, 0, 360, GREEN, -1)
        proc.stdin.write(np.ascontiguousarray(f).tobytes())
    proc.stdin.close()
    assert proc.wait(timeout=120) == 0
    return path


def _yuv(path: Path) -> np.ndarray:
    p = subprocess.run([media.ffmpeg_exe(), "-loglevel", "error", "-i", str(path), "-f", "rawvideo",
                        "-pix_fmt", "yuv420p", "-"], capture_output=True, check=True, timeout=120)
    return np.frombuffer(p.stdout, np.uint8).reshape(-1, W * H * 3 // 2)


def _audio_md5(path: Path) -> str:
    p = subprocess.run([media.ffmpeg_exe(), "-loglevel", "error", "-i", str(path), "-map", "0:a", "-f", "md5", "-"],
                       capture_output=True, text=True, check=True, timeout=120)
    return p.stdout.strip()


async def _quiet(*_a, **_k):
    return None


def _res(gid="code", frm=0.3, settle=0.5667, to=0.95, *, layer="behind") -> dict:
    return rc.IconResolved.model_validate({
        "glyph": {"source": "library", "id": gid}, "label": "Code", "from_sec": frm, "settle_sec": settle,
        "to_sec": to, "box": {"x": 0.62, "y": 0.06, "w": 0.2, "h": 0.1125}, "fill": "#7f2bf2", "ink": "#ffffff",
        "fx": {"entrance": "pop", "seed": f"s:{gid}"}, "style": "circle", "look_v": 1, "weight": "bold",
        "layer": layer,
    }).model_dump(mode="json")


def _job(tmp_path: Path, name: str = "pattern") -> pattern_el.PatternJob:
    async def plan_frame():
        return None

    return pattern_el.PatternJob(
        exp_id=0, aspect="9:16", out_dir=tmp_path / name, intro=None, intro_is_still=False,
        seed="studio:1:scene_0", plan_frame=plan_frame, step=_quiet,
        colours=pattern_ref.Colours(ground="#3d3c3d", ink="#f4f4f8", fell_back=False, frame=None,
                                    colour="#7f2bf2"))


def _recipe(**icons) -> rc.Recipe:
    els = [rc.Element(id="pattern", kind="pattern", props={"slug": "structure", "layout": None, "colour": "auto"})]
    if icons:
        els.append(rc.Element(id="icons", kind="icon", props={"style": "circle", **icons}))
    return rc.normalize(rc.Recipe(engine="house", target="scene_0", seed="studio:1:scene_0", elements=els))


# ---------------------------------------------------------------------------
# Preview == run: the pattern
# ---------------------------------------------------------------------------

@pytest.mark.skipif(not REAL_MASKS, reason="the pattern kit's masks are not here")
@pytest.mark.omni_default  # Omni switched off, as a server has it (I16)
def test_the_run_draws_the_frozen_pattern_exactly_where_the_preview_placed_it(tmp_path):
    recipe = _recipe()
    el = rc.pattern_element(recipe)
    shown = asyncio.run(pattern_el.build(el, recipe.colours, _job(tmp_path, "preview")))
    frozen = {"layout": shown.ref.layout.public(), "colour": shown.ref.colour, "ground": shown.ref.ground,
              "ink": shown.ref.ink, "engine_v": house.HOUSE_VERSION}
    stored = pattern_el.materialise(recipe, {"pattern": frozen})
    assert rc.pattern_element(stored).props["resolved"] == frozen
    assert rc.recipe_hash(pattern_el.strip_resolved(stored)) == rc.recipe_hash(recipe)
    job = dataclasses.replace(_job(tmp_path, "run"), colours=None)      # the run digs nothing
    ran = asyncio.run(pattern_el.from_resolved(rc.pattern_element(stored), job))
    assert ran.error == "" and ran.report == {"mode": "frozen"}
    assert ran.ref.layout == shown.ref.layout and ran.ref.animation == shown.ref.animation
    a = np.asarray(pattern_ref._layer(shown.ref, W, H))
    b = np.asarray(pattern_ref._layer(ran.ref, W, H))
    assert np.array_equal(a, b), "the run's pattern is the preview's, pixel for pixel"


# ---------------------------------------------------------------------------
# The footage untouched, the sound byte for byte
# ---------------------------------------------------------------------------

@pytest.mark.skipif(not REAL_MASKS, reason="the pattern kit's masks are not here")
def test_the_footage_and_the_sound_come_out_untouched(tmp_path):
    src = _scene(tmp_path / "scene0.mp4")
    folder = studio.analysis_dir(src)
    bank = mattes.MatteBank.open(src, folder).fill()
    track = camera.build(src, folder, bank=bank)
    recipe = _recipe()
    shown = asyncio.run(pattern_el.build(rc.pattern_element(recipe), recipe.colours, _job(tmp_path)))
    icons = [_res()]
    spec = house.plate_spec(recipe, [(rc.pattern_element(recipe), shown.ref)], width=W, height=H, fps=bank.fps,
                            n=bank.n, bars=bank.bars, stamper=ir.Stamper(icons, fps=bank.fps, frame_size=(W, H)),
                            icons=icons)
    # And a solid 42 violet block held still in a corner: its colour, exact.
    block = np.zeros((H, W, 4), np.uint8)
    block[560:620, 280:340] = (0x7F, 0x2B, 0xF2, 255)
    spec = dataclasses.replace(spec, layers=spec.layers + (pl.LayerSpec(
        element_id="block", art=pl.premultiplied(block), ink_box=(280, 560, 340, 620), follow="screen",
        timing=pattern_el.Timing(t_in=0.0, t_full=0.0, t_hold_end=9.0, t_out=9.0, entrance="none", exit="none",
                                 runs_to_end=True)),))
    seen = np.zeros((H, W), np.uint8)

    def keep(i, frame):
        np.maximum(seen, frame[..., 3], out=seen)

    out = house.render(spec, src, tmp_path / "out.mp4", camera=track, bank=bank, on_plate=keep)
    a, b = _yuv(src), _yuv(out)
    assert len(a) == len(b) == N, "every frame"
    assert _audio_md5(out) == _audio_md5(src), "the sound, byte for byte"
    # Outside everything the plate ever drew (grown for the chroma's 2x2):
    far = cv2.dilate((seen > 0).astype(np.uint8), np.ones((7, 7), np.uint8)) == 0
    far_uv = far[::2, ::2] & far[1::2, 1::2]
    assert far.mean() > 0.3
    for plane, (lo, hi, mask) in {"Y": (0, W * H, far),
                                  "U": (W * H, W * H * 5 // 4, far_uv),
                                  "V": (W * H * 5 // 4, W * H * 3 // 2, far_uv)}.items():
        shape = mask.shape
        x = a[:, lo:hi].reshape((N,) + shape)[:, mask].astype(np.float64)
        y = b[:, lo:hi].reshape((N,) + shape)[:, mask].astype(np.float64)
        psnr = 10 * np.log10(255 ** 2 / max(float(((x - y) ** 2).mean()), 1e-9))
        assert psnr >= 42.0 and abs(float((y - x).mean())) <= 0.5, f"{plane}: {psnr:.1f} dB"
    # 42 violet comes out exact where it is solid (bt709, as a browser shows it).
    rgb = subprocess.run([media.ffmpeg_exe(), "-loglevel", "error", "-i", str(out), "-vf",
                          r"select=eq(n\,30),scale=in_color_matrix=bt709:in_range=tv,format=rgb24", "-frames:v", "1",
                          "-f", "rawvideo", "-pix_fmt", "rgb24", "-"], capture_output=True, check=True).stdout
    frame = np.frombuffer(rgb, np.uint8).reshape(H, W, 3)
    violet = frame[566:614, 286:334].reshape(-1, 3).astype(np.float64).mean(axis=0)
    assert np.abs(violet - [0x7F, 0x2B, 0xF2]).max() <= 2.0, violet


# ---------------------------------------------------------------------------
# A whole Generate, for $0 — and preview == run, plate for plate
# ---------------------------------------------------------------------------

@pytest.fixture
def house_run(tmp_path, monkeypatch, paid):
    """One Generate of a house recipe (a frozen pattern, a frozen circle
    icon) through pipeline._run on the synthetic scene, the database faked."""
    src = _scene(tmp_path / "base" / "scene0.mp4")
    recipe = _recipe(resolved=[_res()])
    shown = asyncio.run(pattern_el.build(rc.pattern_element(recipe), recipe.colours, _job(tmp_path, "preview")))
    recipe = pattern_el.materialise(recipe, {"pattern": {
        "layout": shown.ref.layout.public(), "colour": shown.ref.colour, "ground": shown.ref.ground,
        "ink": shown.ref.ink, "engine_v": house.HOUSE_VERSION}})
    rows: dict = {"patches": [], "steps": [], "updates": []}

    async def patch_arm(exp_id, arm, patch):
        rows["patches"].append((arm, dict(patch)))

    async def step(exp_id, arm, s):
        rows["steps"].append(s["name"])

    async def update(exp_id, **fields):
        rows["updates"].append(fields)

    async def get(exp_id):
        return {"id": exp_id, "arms": {"omni": {"status": store.ARM_READY}}}

    async def persist(*, abs_path, bucket_key, mime=None):
        return None, None

    monkeypatch.setattr(store, "patch_arm", patch_arm)
    monkeypatch.setattr(store, "append_arm_step", step)
    monkeypatch.setattr(store, "update_experiment", update)
    monkeypatch.setattr(store, "get_experiment", get)
    monkeypatch.setattr(store, "touch", _quiet)
    monkeypatch.setattr(asset_storage, "persist_local_file", persist)
    options = {**pipeline.normalize_options({"target": "scene_0"}), "recipe": recipe.model_dump(mode="json"),
               "engine": "house"}

    async def prepare(exp_id, run_id, opts):
        seg = pipeline.Segment(kind="scene", index=0, source=src, is_still=False, base=src)
        ctx = pipeline.Ctx(exp_id=exp_id, run_id=run_id, aspect="9:16", width=W, height=H,
                           work=pipeline.REBRAND_ROOT / str(exp_id), options=opts, run_ctx={"topic": "t"},
                           intro=None, scenes=[seg], outro=None)
        ctx.recipe = pipeline._recipe_of(opts)
        ctx.harness = True
        ctx.target_seg = seg
        await pipeline._build_pattern_ref(ctx)
        rows["ctx"] = ctx
        return ctx

    monkeypatch.setattr(pipeline, "_prepare", prepare)
    plates: dict[int, np.ndarray] = {}
    real = house.render

    def spy(spec, source, out, **kw):
        rows["spec"] = spec
        return real(spec, source, out, on_plate=lambda i, p: plates.__setitem__(i, p.copy()) if i in PICK else None,
                    **kw)

    monkeypatch.setattr(house, "render", spy)
    asyncio.run(pipeline._run(41, 1, options))
    rows.update(src=src, recipe=recipe, shown=shown, plates=plates)
    return rows


@pytest.mark.skipif(not REAL_MASKS, reason="the pattern kit's masks are not here")
@pytest.mark.omni_default  # Omni switched off, as a server has it (I16)
def test_a_house_generate_costs_nothing_and_asks_no_model(house_run, paid):
    assert paid == [], f"paid calls: {paid}"
    last = house_run["updates"][-1]
    assert last["status"] == store.ST_READY and last["lesson"] is None
    running = next(u for u in house_run["updates"] if u.get("status") == store.ST_RUNNING)
    assert running["stage"] == pipeline.HOUSE_STAGE and "brief" not in running
    assert running["evidence"]["engine"] == "house" and running["evidence"]["omni_pattern"]["slug"] == "structure"
    first = house_run["patches"][0][1]
    assert (first["engine"], first["provider"], first["model"]) == ("house", "house", house.HOUSE_MODEL)
    done = next(p for a, p in house_run["patches"] if p.get("status") == store.ARM_READY)
    assert done["auto_review"] is None, "no critique"
    assert done["cost_usd"] == 0.0 and done["provider"] == "house" and done["engine"] == "house"
    stages = done["stages"]
    assert {"mattes", "camera", "render", "omni", "guards", "icons"} <= set(stages)
    assert stages["render"]["c0"]["engine_v"] == house.HOUSE_VERSION and not stages["render"]["c0"]["reused"]
    assert stages["omni"]["c0"]["entering"] is True
    final = Path(stages["render"]["c0"]["path"])
    assert final.is_file() and len(_yuv(final)) == N
    made = next(p for a, p in house_run["patches"] if "house" in p)
    assert made["house"]["layers"][0]["follow"] == "room" and made["house"]["camera"]["method"] == "features"
    assert hbase.still_path(41, 0).is_file(), "the version's still is the design still"


@pytest.mark.skipif(not REAL_MASKS, reason="the pattern kit's masks are not here")
@pytest.mark.omni_default  # Omni switched off, as a server has it (I16)
def test_the_plates_the_run_pipes_are_the_plates_made_again_on_their_own(house_run, tmp_path):
    """preview == run: the plates are made from the scene's cached analysis
    (the bank and the camera, by the clip's bytes), the preview's pattern
    (planned, not frozen) and the frozen icons — array_equal."""
    src = house_run["src"]
    folder = studio.analysis_dir(src)
    bank = mattes.MatteBank.open(src, folder)
    assert not bank.missing(), "the run's bank is on disk, whole"
    track = camera.build(src, folder, bank=bank)
    recipe = house_run["recipe"]
    icons = [_res()]
    spec = house.plate_spec(pattern_el.strip_resolved(recipe), [(rc.pattern_element(recipe), house_run["shown"].ref)],
                            width=W, height=H, fps=bank.fps, n=bank.n, bars=bank.bars,
                            stamper=ir.Stamper(icons, fps=bank.fps, frame_size=(W, H)), icons=icons)
    assert sorted(house_run["plates"]) == sorted(PICK)
    for i in PICK:
        assert np.array_equal(pl.plate_at(spec, i, camera=track, bank=bank), house_run["plates"][i]), f"frame {i}"
    run_spec = house_run["spec"]
    assert house.render_key(run_spec, source=src, camera_key=track.key, bank_key=bank.key) == \
        house.render_key(spec, source=src, camera_key=track.key, bank_key=bank.key)
    # The icon is in (frame 17 = 0.57 s: settled), the pattern grows with the push-in.
    assert house_run["plates"][17][..., 3].max() == 255
    assert house_run["plates"][N - 1][..., 3].astype(bool).sum() != house_run["plates"][5][..., 3].astype(bool).sum()


@pytest.mark.skipif(not REAL_MASKS, reason="the pattern kit's masks are not here")
@pytest.mark.omni_default  # Omni switched off, as a server has it (I16)
def test_a_generate_of_the_same_spec_copies_the_studios_render(house_run, monkeypatch, paid, tmp_path):
    """A studio render of the very spec (<scene>/house/<key>.mp4) is copied,
    not made again."""
    done = next(p for a, p in house_run["patches"] if p.get("status") == store.ARM_READY)
    rkey = done["stages"]["render"]["c0"]["key"]
    folder = studio.scene_base(1, "scene_0", house_run["src"])
    draft = house.draft_path(folder, rkey)
    draft.parent.mkdir(parents=True, exist_ok=True)
    draft.write_bytes(Path(done["stages"]["render"]["c0"]["path"]).read_bytes())

    def never(*_a, **_k):
        raise AssertionError("rendered again")

    monkeypatch.setattr(house, "render", never)
    ctx = house_run["ctx"]
    ctx.exp_id = 42
    manifest = pipeline.hstages.Manifest()
    out = asyncio.run(house.edit_scene(ctx, "omni", ctx.target_seg, tmp_path / "again", "Scene 1", manifest))
    assert out.read_bytes() == draft.read_bytes()
    assert manifest.stages["render"]["c0"]["reused"] is True and manifest.stages["render"]["c0"]["key"] == rkey


# ---------------------------------------------------------------------------
# Old versions, new versions
# ---------------------------------------------------------------------------

def _row(vid: int, recipe: dict, *, house_made: bool, final: Path) -> dict:
    opts = {**pipeline.normalize_options({"target": "scene_0"}), "recipe": recipe, "icons_by_pipeline": True}
    arm = {"status": "ready", "final_path": str(final), "stages": {
        "omni": {"c0": {"key": "k", "path": str(final), "entering": True}},
        "guards": {"c0": {"key": None, "path": str(final)}}}}
    if house_made:
        opts["engine"] = "house"
        arm.update(engine="house", provider="house")
    return {"id": vid, "run_id": 815, "status": "ready", "stage": "Done", "options": opts, "arms": {"omni": arm},
            "evidence": {"recipe_hash": "x"}, "created_at": None, "updated_at": None}


@pytest.mark.omni_default  # Omni switched off, as a server has it (I16)
def test_an_omni_version_still_lists_plays_and_reads_as_it_did(tmp_path, monkeypatch):
    from app.routers import rebrand_harness as rt

    final = tmp_path / "v25.mp4"
    final.write_bytes(b"x")
    v25 = _row(25, STORED["25"]["recipe"], house_made=False, final=final)
    house_row = _row(41, rc.preset_42_house("scene_0").model_dump(mode="json"), house_made=True, final=final)

    async def listed(run_id, *, limit, target):
        return [house_row, v25]

    monkeypatch.setattr(store, "list_for_run", listed)
    got = {v["id"]: v for v in asyncio.run(rt._versions(815, "scene_0"))}
    omni_v = got[25]
    assert omni_v["recipe"] == STORED["25"]["recipe"] and omni_v["recipe"]["engine"] == "omni"
    assert rc.recipe_hash(rc.normalize(omni_v["recipe"])) == STORED["25"]["recipe_hash"]
    assert omni_v["arms"]["omni"]["url"] == "/api/rebrand/experiments/25/file/omni/final", "it plays"
    assert omni_v["stills"] == ["/api/rebrand/harness/versions/25/still/0"]
    assert not hbase.made_in_house(v25) and hbase.part_count(v25) == 1
    ours = got[41]
    assert ours["arms"]["omni"]["url"] and ours["stills"] == ["/api/rebrand/harness/versions/41/still/0"]
    assert hbase.made_in_house(house_row) and hbase.part_count(house_row) == 1 and hbase.entering(house_row, 0)
    # Made in house, Omni drew nothing: no "Omni's own icons" on it, ever.
    bare = _row(43, rc.preset_42_house("scene_0").model_dump(mode="json"), house_made=True, final=final)
    bare["options"].pop("icons_by_pipeline")
    assert not hbase.omni_drew_icons(bare) and not hbase.omni_icons_in([bare])
    omni_bare = _row(44, rc.preset_42_default("scene_0").model_dump(mode="json"), house_made=False, final=final)
    omni_bare["options"].pop("icons_by_pipeline")
    assert hbase.omni_drew_icons(omni_bare), "an Omni version made from the brief still says it drew its own"


def test_a_house_recipe_needs_no_omni_and_is_said_on_the_row(monkeypatch):
    created: dict = {}

    async def run_row(run_id):
        return {"metadata": {}}

    async def none(*a, **k):
        return []

    async def get(slug):
        return lib.BUILTINS.get(slug)

    async def create(run_id, options):
        created.update(options)
        return {"id": 77}

    monkeypatch.setattr(pipeline.th, "_fetch_run_row", run_row)
    monkeypatch.setattr(omni, "providers", lambda: [])
    monkeypatch.setattr(lib, "get", get)
    monkeypatch.setattr(lib, "lookup", get)
    monkeypatch.setattr(store, "list_for_run", none)
    monkeypatch.setattr(store, "create_experiment", create)
    monkeypatch.setattr(pipeline, "_run", _quiet)
    recipe = rc.normalize(rc.preset_42_house("scene_0").model_copy(update={
        "seed": "studio:1:scene_0", "guards": rc.Guards(stray="clean")}))

    async def go():
        exp = await pipeline.start(1, None, recipe=recipe)
        await asyncio.sleep(0)
        return exp

    assert asyncio.run(go())["id"] == 77
    assert created["engine"] == "house" and "stray_clean" not in created
    assert rc.normalize(created["recipe"]).engine == "house"
    # Omni's recipe still needs its provider.
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as err:
        asyncio.run(pipeline.start(1, None, recipe=rc.preset_42_default("scene_0")))
    assert err.value.status_code == 400 and err.value.detail == omni.NOT_CONFIGURED_MSG


@pytest.mark.omni_default  # Omni switched off, as a server has it (I16)
def test_adding_on_top_of_a_version_made_in_house_is_changing_it(monkeypatch, tmp_path):
    from fastapi import HTTPException

    final = tmp_path / "v.mp4"
    final.write_bytes(b"x")
    made = _row(41, rc.preset_42_house("scene_0").model_dump(mode="json"), house_made=True, final=final)

    async def run_row(run_id):
        return {"metadata": {}}

    async def load(spec, run_id, target, **_k):
        return hbase.BaseVersion(spec=spec, exp=made, chain=[made], layers=[])

    monkeypatch.setattr(pipeline.th, "_fetch_run_row", run_row)
    monkeypatch.setattr(hbase, "load", load)
    recipe = rc.normalize(rc.Recipe(engine="house", target="scene_0", elements=[
        rc.Element(id="pattern", kind="pattern", props={"slug": "grid"})]))
    with pytest.raises(HTTPException) as err:
        asyncio.run(pipeline.start(815, None, recipe=recipe, base={"version_id": 41, "mode": "stack"}))
    assert err.value.status_code == 409 and err.value.detail == hbase.ADD_ON_HOUSE


@pytest.mark.omni_default  # Omni switched off, as a server has it (I16)
def test_generate_freezes_each_pattern_as_the_preview_drew_it(monkeypatch):
    body = {"pattern": {"slug": "structure", "colour": "#7F2BF2", "ground": "#3D3C3D", "ink": "#F4F4F8"},
            "patterns": [{"element_id": "pattern", "colour": "#7F2BF2", "layout": {
                "placement": "left", "scale": 2.0, "reach": 0.5, "anchor": "left", "rotate": 0}},
                         {"element_id": "pattern-2", "colour": None, "layout": None, "error": "gone"}]}
    asked: list = []

    async def preview(run_id, target, recipe, base=None):
        asked.append((run_id, target, base))
        return body

    monkeypatch.setattr(studio, "preview", preview)
    recipe = rc.normalize(rc.Recipe(engine="house", target="scene_0", elements=[
        rc.Element(id="pattern", kind="pattern", props={"slug": "structure", "follow": "screen"}),
        rc.Element(id="pattern-2", kind="pattern", props={"slug": "grid"})]))
    frozen = asyncio.run(studio.final_patterns(3, "scene_0", recipe))
    assert asked == [(3, "scene_0", None)]
    assert frozen == {"pattern": {"layout": body["patterns"][0]["layout"], "colour": "#7f2bf2",
                                  "ground": "#3d3c3d", "ink": "#f4f4f8", "follow": "screen",
                                  "engine_v": house.HOUSE_VERSION}}, "one the preview couldn't draw isn't frozen"
    stored = pattern_el.materialise(recipe, frozen)
    assert rc.pattern_elements(stored)[0].props["resolved"] == frozen["pattern"]
    assert "resolved" not in rc.pattern_elements(stored)[1].props


@pytest.mark.omni_default  # Omni switched off, as a server has it (I16)
def test_the_router_freezes_a_house_recipes_patterns_at_generate(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.routers import rebrand_harness as rt

    async def lookup(slug):
        return lib.BUILTINS.get(slug)

    frozen = {"pattern": {"layout": {"placement": "left", "scale": 2.0, "reach": 0.5, "anchor": "left",
                                     "rotate": 0}, "colour": "#7f2bf2", "engine_v": 1}}
    seen: dict = {}

    async def final_patterns(run_id, target, recipe, base=None):
        seen["asked"] = recipe
        return frozen

    async def start(run_id, raw, recipe=None, base=None):
        seen["start"] = recipe
        return {"id": 5, "run_id": run_id, "options": {"target": recipe.target}}

    monkeypatch.setattr(lib, "lookup", lookup)
    monkeypatch.setattr(studio, "final_patterns", final_patterns)
    monkeypatch.setattr(pipeline, "start", start)
    app = FastAPI()
    app.include_router(rt.router)
    client = TestClient(app)
    recipe = rc.preset_42_house().model_dump(mode="json")
    r = client.post("/api/rebrand/harness/versions", json={"run_id": 3, "target": "scene_0", "recipe": recipe})
    assert r.status_code == 200
    assert rc.pattern_element(seen["start"]).props["resolved"] == frozen["pattern"]
    assert "resolved" not in rc.pattern_element(seen["asked"]).props, "the preview is asked for the draft"
    assert r.json()["recipe"]["elements"][0]["props"]["resolved"] == frozen["pattern"]
    # The default draft is the 42 default, made in house.
    assert rt._default_recipe(3, "scene_0")["engine"] == "house"
    cat = client.get("/api/rebrand/harness/catalogue").json()
    assert "slide" in [e["value"] for e in cat["entrances"]]
    assert cat["presets"][0]["recipe"]["engine"] == "house"


def test_rebrand_omni_off_refuses_omni_and_never_house(monkeypatch):
    monkeypatch.setenv("REBRAND_OMNI", "off")
    with pytest.raises(rc.RecipeError) as e:
        rc.runnable(rc.preset_42_default("scene_0"))
    assert e.value.errors == [{"element_id": None, "field": "engine", "message": rc.OMNI_OFF}]
    rc.runnable(rc.preset_42_house("scene_0"))
    monkeypatch.setenv("REBRAND_OMNI", "on")
    rc.runnable(rc.preset_42_default("scene_0"))
    monkeypatch.delenv("REBRAND_OMNI")
    assert not rc.omni_on(), "switched off unless set to on (I16)"


@pytest.mark.omni_default  # Omni switched off, as a server has it (I16)
def test_rating_a_version_made_in_house_embeds_nothing(monkeypatch, paid, tmp_path):
    final = tmp_path / "v.mp4"
    final.write_bytes(b"x")
    made = _row(41, rc.preset_42_house("scene_0").model_dump(mode="json"), house_made=True, final=final)
    saved: dict = {}

    async def get(exp_id):
        return {**made, **saved}

    async def update(exp_id, **fields):
        saved.update(fields)

    monkeypatch.setattr(store, "get_experiment", get)
    monkeypatch.setattr(store, "update_experiment", update)
    got = asyncio.run(pipeline.rate(41, winner=None, omni_stars=4, veo_stars=None, notes="nice"))
    assert got["rating"]["omni_stars"] == 4 and paid == []


def test_a_stopped_render_leaves_nothing_behind(tmp_path):
    src = _scene(tmp_path / "scene0.mp4")
    spec = pl.PlateSpec(width=W, height=H, fps=float(FPS), n=N)
    calls = iter(range(100))
    with pytest.raises(house.Cancelled):
        house.render(spec, src, tmp_path / "out.mp4", cancel=lambda: next(calls) >= 5)
    assert not list(tmp_path.glob("out*")), "no file, whole or half"
