"""Versions on the in-house engine (I10): carrying on from what Generate made,
"Keep v3's icons", a lost version made again, and adding on top of a
version Omni made.

  carrying on   a draft carried on from a house version (its whole recipe,
                each pattern pinned where its run drew it) previews
                byte-identical to that version's still, resolves to the very
                spec its run rendered (the same render key), and plays that
                version's own render at once (the run hands it over) — no
                second render
  keep icons    props.keep: the plan IS the version's icons as it froze
                them — no director asked, nothing placed; Generate freezes
                exactly those
  remake        a house version whose video is gone is made again from its
                recipe and the original: the plates array_equal, the same
                render key, its still again; the studio's render of that
                spec is copied instead when it is there; Omni's can't be
  Omni parent   "Add on top" of a version Omni made renders over its clean
                clip with the ORIGINAL's matte: outside the new plate the
                output is that version (YUV >= 42 dB), one_speaker finds one
                speaker — and says so when the video under it shows two
  house pass    adding to a version made in house on top of one Omni made
                keeps Omni's video under everything (>= 42 dB outside the
                plate, every layer of the pass drawn again, from its row);
                an unchanged draft on it IS that version (its render key, its
                own render played at once, Next's frames showing its layers
                over Omni's video); the server takes it, and still sends a
                version made in house from the original to Change it
  base.py       a house version is read from its render stage (one part,
                entering, its clip), and `lost` when its video is gone; a
                chain's ground and passes

MODNet is faked by the speaker's green, the pattern kit's masks are real
(skipped when absent), words are faked (no Whisper) and every paid client
raises. Nothing touches the database, a bucket or a server."""
from __future__ import annotations

import asyncio
import json
import shutil
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
from app.services.rebrand_harness import guards as gd
from app.services.rebrand_harness import house
from app.services.rebrand_harness import plate as pl
from app.services.rebrand_harness import recipe as rc
from app.services.rebrand_harness import studio
from app.services.rebrand_harness.elements import icon as icon_el
from app.services.rebrand_harness.elements import pattern as pattern_el

cv2 = pytest.importorskip("cv2")
# Made in house: run with Omni switched off, as a server has it (conftest,
# I16) — nothing here may depend on Omni.
pytestmark = pytest.mark.omni_default

W, H, FPS = 360, 640, 30
N = 90                                     # 3 s
PICK = (0, 12, 45, N - 1)
GREEN = (40, 200, 60)                      # the "speaker", BGR
REAL_MASKS = pattern_ref._mask_path("structure").exists()
PLAN_KEY = "0123456789abcdef"
needs_kit = pytest.mark.skipif(not REAL_MASKS, reason="the pattern kit's masks are not here")


@pytest.fixture(autouse=True)
def paid(monkeypatch, tmp_path):
    """Every paid client raises and is remembered; MODNet is the green; the
    words are a fixed answer (no Whisper); the director is never asked; the
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
    monkeypatch.setattr(studio, "_icon_director", refusing("icon director"))

    async def words(scene):
        return {"words": [], "digest": "w0", "duration": N / FPS, "source": "whisper", "voiced": None}

    monkeypatch.setattr(studio, "_icon_words", words)
    monkeypatch.setattr(speaker_front, "raw_matte", _fake_raw)
    monkeypatch.setattr(studio, "ANALYSIS_ROOT", tmp_path / "_cache" / "scene")
    monkeypatch.setattr(studio, "STUDIO_ROOT", tmp_path / "_studio")
    for name in ("_ANALYSES", "_PREPARED", "_PREVIEWS", "_MASK_TASKS", "_HOUSE_SPECS", "_DRAFTS", "_CLAIMED",
                 "_BASES", "_STACKS", "_REMAKES", "_ICON_TASKS"):
        monkeypatch.setattr(studio, name, {})
    monkeypatch.setattr(pipeline, "REBRAND_ROOT", tmp_path / "rebrand")
    return asked


def _fake_raw(bgr) -> np.ndarray:
    d = np.abs(bgr.astype(np.int16) - np.array(GREEN, np.int16)).max(axis=2)
    g = (d < 40).astype(np.float32)
    return cv2.resize(g, (max(1, bgr.shape[1] // 2), max(1, bgr.shape[0] // 2)), interpolation=cv2.INTER_AREA)


def _green(bgr) -> np.ndarray:
    d = np.abs(bgr.astype(np.int16) - np.array(GREEN, np.int16)).max(axis=2)
    return (d < 40).astype(np.float32)


def _texture(seed: int = 3) -> np.ndarray:
    r = np.random.default_rng(seed)
    grey = r.random((H // 6, W // 6)).astype(np.float32)
    tint = r.random((H // 24, W // 24, 3)).astype(np.float32)
    t = (cv2.resize(grey, (W, H), interpolation=cv2.INTER_CUBIC)[..., None] * 0.55
         + cv2.resize(tint, (W, H), interpolation=cv2.INTER_CUBIC) * 0.3)
    t = cv2.GaussianBlur(t, (0, 0), 1.5)
    t[:, :, 1] *= 0.6
    return np.clip(t * 255, 0, 255).astype(np.uint8)


def _encode(path: Path, frames) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    proc = subprocess.Popen([media.ffmpeg_exe(), "-y", "-loglevel", "error", "-f", "rawvideo", "-pix_fmt", "bgr24",
                             "-s", f"{W}x{H}", "-r", str(FPS), "-i", "-", "-f", "lavfi", "-i",
                             "sine=frequency=330:sample_rate=44100", "-map", "0:v", "-map", "1:a", "-shortest",
                             "-c:v", "libx264", "-preset", "ultrafast", "-crf", "12", "-pix_fmt", "yuv420p",
                             "-c:a", "aac", str(path)], stdin=subprocess.PIPE)
    for f in frames:
        proc.stdin.write(np.ascontiguousarray(f).tobytes())
    proc.stdin.close()
    assert proc.wait(timeout=120) == 0
    return path


def _room(i: int) -> np.ndarray:
    """Frame i of the scene: a room pushing in 1.00 -> 1.06, the speaker
    (green) standing in it, swaying a little."""
    tex = _texture()
    cx, cy = (W - 1) / 2, (H - 1) / 2
    s = 1.0 + 0.06 * i / (N - 1)
    f = cv2.warpAffine(tex, np.array([[s, 0, (1 - s) * cx], [0, s, (1 - s) * cy]]), (W, H),
                       flags=cv2.INTER_CUBIC, borderMode=cv2.BORDER_REFLECT)
    sway = int(round(10 * np.sin(i / 9.0)))
    cv2.ellipse(f, (W // 2 + 30 + sway, int(H * 0.62)), (W // 5, H // 4), 0, 0, 360, GREEN, -1)
    return f


def _yuv(path: Path) -> np.ndarray:
    p = subprocess.run([media.ffmpeg_exe(), "-loglevel", "error", "-i", str(path), "-f", "rawvideo",
                        "-pix_fmt", "yuv420p", "-"], capture_output=True, check=True, timeout=120)
    return np.frombuffer(p.stdout, np.uint8).reshape(-1, W * H * 3 // 2)


def _res(gid="code", frm=0.3, settle=0.5667, to=2.4) -> dict:
    return rc.IconResolved.model_validate({
        "glyph": {"source": "library", "id": gid}, "label": "Code", "from_sec": frm, "settle_sec": settle,
        "to_sec": to, "box": {"x": 0.62, "y": 0.06, "w": 0.2, "h": 0.1125}, "fill": "#7f2bf2", "ink": "#ffffff",
        "fx": {"entrance": "pop", "seed": f"s:{gid}"}, "style": "circle", "look_v": 1, "weight": "bold",
    }).model_dump(mode="json")


def _recipe(*, icons: bool = True) -> rc.Recipe:
    els = [rc.Element(id="pattern", kind="pattern", props={"slug": "structure", "layout": None, "colour": "auto"})]
    if icons:
        els.append(rc.Element(id="icons", kind="icon", props={"style": "circle"}))
    return rc.normalize(rc.Recipe(engine="house", target="scene_0", seed="studio:1:scene_0", elements=els))


async def _quiet(*_a, **_k):
    return None


@pytest.fixture
def scene(tmp_path, monkeypatch):
    """Run 1's scene_0 prepared in the studio as _prepare leaves it (the
    normalised clip, described: no cut; its first frame; the intro's colours
    dug), the speaker's masks from the green, and the icon plan shown kept
    (a frozen circle)."""
    source = _encode(tmp_path / "src" / "scene0.mp4", (_room(i) for i in range(N)))
    work = studio.scene_base(1, "scene_0", source)
    clip = work / "base" / "scene0.mp4"
    clip.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, clip)
    media.scene_info_path(clip).write_text(json.dumps(
        {"source_fps": FPS, "source_cut": None, "trimmed_sec": 0.0, "cuts": []}), encoding="utf-8")
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


class Db:
    """The rows the tests' fake store holds (never the shared database)."""

    def __init__(self):
        self.rows: dict[int, dict] = {}
        self.patches: list[tuple[int, dict]] = []


@pytest.fixture
def db(monkeypatch):
    d = Db()

    async def get(eid):
        row = d.rows.get(int(eid))
        return json.loads(json.dumps(row)) if row is not None else {"id": eid, "arms": {"omni": {"status": "ready"}}}

    async def patch_arm(eid, arm, patch):
        d.patches.append((int(eid), dict(patch)))
        row = d.rows.get(int(eid))
        if row is not None:
            row.setdefault("arms", {}).setdefault(arm, {}).update(json.loads(json.dumps(patch, default=str)))

    async def persist(*, abs_path, bucket_key, mime=None):
        return None, None

    monkeypatch.setattr(store, "get_experiment", get)
    monkeypatch.setattr(store, "patch_arm", patch_arm)
    monkeypatch.setattr(store, "append_arm_step", _quiet)
    monkeypatch.setattr(store, "touch", _quiet)
    monkeypatch.setattr(asset_storage, "persist_local_file", persist)
    return d


def _generate(monkeypatch, db: Db, scene, recipe: rc.Recipe, exp_id: int, *, spy=None, base=None) -> dict:
    """One Generate of `recipe` (as the router stores it) through
    pipeline._run on the scene; the row it leaves is put in the fake store
    (ready, its manifest, its evidence)."""
    updates: list[dict] = []

    async def update(eid, **fields):
        updates.append(fields)

    monkeypatch.setattr(store, "update_experiment", update)
    options = {**pipeline.normalize_options({"target": "scene_0"}), "recipe": recipe.model_dump(mode="json"),
               "engine": "house"}
    if base is not None:
        options["base"] = base
    seg0 = scene.ctx.target_seg
    ran: dict = {}

    async def prepare(eid, run_id, opts):
        dst = pipeline.REBRAND_ROOT / str(eid) / "base" / "scene0.mp4"
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(seg0.base, dst)
        shutil.copyfile(media.scene_info_path(seg0.base), media.scene_info_path(dst))
        seg = pipeline.Segment(kind="scene", index=0, source=seg0.source, is_still=False, base=dst)
        ctx = pipeline.Ctx(exp_id=eid, run_id=run_id, aspect="9:16", width=W, height=H,
                           work=pipeline.REBRAND_ROOT / str(eid), options=opts, run_ctx={"topic": "t"},
                           intro=None, scenes=[seg], outro=None)
        ctx.recipe = pipeline._recipe_of(opts)
        ctx.harness = True
        ctx.target_seg = seg
        await pipeline._link_base(ctx)
        if ctx.base is not None and ctx.base.mode == "stack":
            # As the run's _prepare: the version's parts conformed to this clip's.
            chunks = media.split_chunks(dst, ctx.work / "omni" / "chunks_scene_1")
            await hbase.stack_parts(ctx.base, chunks, ctx.work / "base" / "on_v25", width=W, height=H)
        await pipeline._build_pattern_ref(ctx)
        ran["ctx"] = ctx
        return ctx

    monkeypatch.setattr(pipeline, "_prepare", prepare)
    if spy is not None:
        monkeypatch.setattr(house, "render", spy)
    db.rows[exp_id] = {"id": exp_id, "run_id": 1, "status": "running", "options": options,
                       "arms": {"omni": {"status": "running"}}}
    asyncio.run(pipeline._run(exp_id, 1, options))
    running = next(u for u in updates if u.get("status") == store.ST_RUNNING)
    last = updates[-1]
    row = db.rows[exp_id]
    row.update(status=last["status"], evidence=running["evidence"])
    ran["row"] = row
    ran["render"] = row["arms"]["omni"]["stages"]["render"]["c0"]
    return ran


def _pinned(stored: rc.Recipe, evidence: dict) -> rc.Recipe:
    """The draft the studio carries on from a version with (changeDraft):
    its whole recipe, nothing frozen, each pattern pinned where its run drew
    it."""
    plain = pattern_el.strip_resolved(icon_el.strip_resolved(stored))
    layout = (evidence.get("omni_pattern") or {}).get("layout")
    return rc.normalize(plain.model_copy(update={"elements": [
        e.model_copy(update={"props": {**e.props, "layout": layout}}) if e.kind == "pattern" else e
        for e in plain.elements]}))


def _png_path(url: str) -> Path:
    return studio.resolve_file(url.split("/file/", 1)[1].split("?", 1)[0])


# ---------------------------------------------------------------------------
# Carrying on from a version made in house
# ---------------------------------------------------------------------------

@needs_kit
def test_a_house_versions_recipe_previews_byte_identical_to_its_still(scene, monkeypatch, db, paid):
    stored = asyncio.run(studio.as_generated(1, "scene_0", _recipe(icons=False)))
    ran = _generate(monkeypatch, db, scene, stored, 44)
    still = hbase.still_path(44, 0)
    assert still.is_file(), "the run wrote its still"
    draft = _pinned(stored, ran["row"]["evidence"])
    body = asyncio.run(studio.preview(1, "scene_0", draft, base=hbase.BaseSpec(version_id=44, mode="rebuild")))
    assert body["engine"] == "house" and body["base"] == {"version_id": 44, "mode": "rebuild"}
    shown = _png_path(body["chunks"][0]["target_url"])
    assert shown.read_bytes() == still.read_bytes(), "Next carried on from v44 is v44's picture, byte for byte"
    assert paid == []


@needs_kit
def test_carrying_on_from_a_house_version_is_that_version_and_keeps_its_icons(scene, monkeypatch, db, paid):
    stored = asyncio.run(studio.as_generated(1, "scene_0", _recipe(), None, PLAN_KEY))
    ran = _generate(monkeypatch, db, scene, stored, 43)
    key = ran["render"]["key"]
    final = Path(ran["render"]["path"])
    handed = house.draft_path(scene.work, key)
    assert handed.is_file() and handed.read_bytes() == final.read_bytes(), "the run hands its render over"

    # The draft the studio carries on with: v43's whole recipe, its icons
    # kept exactly as v43 froze them ("Keep v43's icons").
    draft = _pinned(stored, ran["row"]["evidence"])
    el = rc.icon_element(draft)
    draft = rc.normalize(draft.model_copy(update={"elements": [
        e.model_copy(update={"props": {**e.props, "keep": 43}}) if e.id == el.id else e for e in draft.elements]}))
    on = hbase.BaseSpec(version_id=43, mode="rebuild")

    async def planned():
        first = await studio.icon_plan(1, "scene_0", draft, on)
        for _ in range(100):                         # the words, a moment
            await asyncio.sleep(0.01)
            got = await studio.icon_plan(1, "scene_0", draft, on)
            if got["state"] == "ready":
                return first, got
        return first, got

    warming, body = asyncio.run(planned())
    assert warming["state"] == "warming" and [s["name"] for s in warming["steps"]] == ["words"]
    assert body["state"] == "ready" and body["kept"] == {"version_id": 43}
    assert [it["source"] for it in body["items"]] == ["kept"] and body["items"][0]["pinned"] is True
    assert body["items"][0]["sprite"]["url"], "its sprite: the run's own pixels"
    assert body["timeline"]["digest"] == "w0", "the lane has the speech"
    _shown, plan = asyncio.run(studio.final_icon_plan(1, "scene_0", draft, on))
    assert plan.key() == body["plan_key"]
    assert plan.resolved() == hbase.icons_of(ran["row"]) == [_res()], "Generate freezes exactly v43's icons"
    assert paid == [], "no director asked, nothing paid"

    # The very spec v43's run rendered: its render key; Next plays v43's own
    # render at once (handed over), nothing rendered again.
    again = asyncio.run(studio.as_generated(1, "scene_0", draft, on, body["plan_key"]))
    hs = asyncio.run(studio.house_spec(1, "scene_0", again, on))
    assert hs.key == key

    def never(*_a, **_k):
        raise AssertionError("rendered again")

    monkeypatch.setattr(house, "render", never)
    got = asyncio.run(studio.house_render(1, "scene_0", draft, on, body["plan_key"]))
    assert got["state"] == "ready" and got["key"] == key and got["url"].endswith(f"{key}.mp4")
    # Its moments are v43's frames: the plate at 1 s is the run's.
    m = asyncio.run(studio.moment(1, "scene_0", draft, 1.0, on, plan_key=body["plan_key"]))
    assert m["key"] == key and m["frame"] == 30


def test_a_kept_plan_is_the_versions_icons_exactly():
    frozen = [_res(), _res("rocket", 1.0, 1.2667, 2.2)]
    p = icon_el.kept_plan(frozen, version_id=7, width=W, height=H, words=None)
    assert p.resolved() == frozen and p.plan_key == p.timing_key
    assert [it["settle_f"] for it in p.items] == [17, 38] and p.public()["items"][1]["glyph"]["id"] == "rocket"
    assert p.plan_key != icon_el.kept_plan(frozen, version_id=8, width=W, height=H).plan_key
    assert p.plan_key != icon_el.kept_plan(frozen[:1], version_id=7, width=W, height=H).plan_key
    assert p.timeline is None and p.colours["why"] == "kept"
    # keep is a recipe field left out of every stored recipe while unset.
    assert "keep" not in rc.preset_42_house("scene_0").model_dump(mode="json")["elements"][0]["props"]
    assert rc.recipe_hash(rc.preset_42_default()) == "1ab8816f2e8a7feb"
    with pytest.raises(rc.RecipeError):
        rc.icon_props(rc.Element(id="icons", kind="icon", props={"keep": 0}))


def test_keeping_the_icons_of_a_version_that_cannot_give_them_plans_as_ever(scene, monkeypatch, db):
    db.rows[9] = {"id": 9, "run_id": 2, "status": "ready", "options": {"target": "scene_0"},
                  "arms": {"omni": {"status": "ready"}}}
    ib = studio._IconBase(scene=scene, recipe=_recipe())
    el = rc.icon_element(ib.recipe)
    for vid in (9, 404):                                     # another run's; none
        props = rc.IconProps.model_validate({"style": "circle", "keep": vid})
        assert asyncio.run(studio._kept_icons(ib, el, props, final=False)) is None


# ---------------------------------------------------------------------------
# A lost version made again
# ---------------------------------------------------------------------------

@needs_kit
def test_a_lost_house_version_is_made_again_the_same_frames(scene, monkeypatch, db, paid):
    stored = asyncio.run(studio.as_generated(1, "scene_0", _recipe(), None, PLAN_KEY))
    first: dict[int, np.ndarray] = {}
    real = house.render

    def spy_into(into):
        def spy(spec, source, out, **kw):
            return real(spec, source, out, on_plate=lambda i, p: into.__setitem__(i, p.copy()) if i in PICK else None,
                        **kw)
        return spy

    ran = _generate(monkeypatch, db, scene, stored, 45, spy=spy_into(first))
    row = db.rows[45]
    final = Path(ran["render"]["path"])
    still = hbase.still_path(45, 0)
    kept_still = still.read_bytes()
    assert not hbase.lost(row)
    # The disk is wiped: the video, its still, the studio's copy of it.
    final.unlink()
    still.unlink()
    house.draft_path(scene.work, ran["render"]["key"]).unlink()
    assert hbase.lost(row) and not hbase.lost({**row, "options": {**row["options"], "engine": "omni",
                                                                    "recipe": rc.preset_42_default().model_dump()},
                                               "arms": {"omni": {**row["arms"]["omni"], "engine": None}}})
    again: dict[int, np.ndarray] = {}
    monkeypatch.setattr(house, "render", spy_into(again))

    async def remade():
        started = await studio.remake(45)
        await studio._REMAKES[45].task
        return started, studio.remake_state(45)

    started, state = asyncio.run(remade())
    assert started["state"] == "running" and state["state"] == "ready", state
    assert state["same"] is True and state["reused"] is False, "the very spec its run rendered"
    assert sorted(again) == sorted(first) == sorted(PICK)
    for i in PICK:
        assert np.array_equal(again[i], first[i]), f"frame {i}: remake == the plates"
    assert final.is_file() and len(_yuv(final)) == N and not hbase.lost(row)
    assert still.read_bytes() == kept_still, "its still again, the very picture its run wrote"
    patch = next(p for eid, p in db.patches if eid == 45 and "remade" in p)
    assert patch["remade"]["same"] is True and patch["remade"]["key"] == ran["render"]["key"]
    assert paid == []

    # With the studio's render of that spec here, it is copied — no render.
    final.unlink()

    def never(*_a, **_k):
        raise AssertionError("rendered again")

    monkeypatch.setattr(house, "render", never)
    _started, state = asyncio.run(remade())
    assert state["state"] == "ready" and state["reused"] is True and final.is_file()


def test_only_a_version_made_in_house_is_made_again(db):
    from fastapi import HTTPException

    omni_row = {"id": 25, "run_id": 1, "status": "ready", "options": {"target": "scene_0",
                                                                       "recipe": rc.preset_42_default().model_dump()},
                "arms": {"omni": {"status": "ready"}}}
    running = {"id": 26, "run_id": 1, "status": "running", "options": {"target": "scene_0", "engine": "house"},
               "arms": {"omni": {"status": "running", "engine": "house"}}}
    db.rows.update({25: omni_row, 26: running})
    for vid, message in ((25, hbase.REMAKE_OMNI), (26, hbase.REMAKE_NOT_MADE)):
        with pytest.raises(HTTPException) as err:
            asyncio.run(studio.remake(vid))
        assert err.value.status_code == 409 and err.value.detail == message
    with pytest.raises(HTTPException) as err:
        studio.remake_state(25)
    assert err.value.status_code == 404


# ---------------------------------------------------------------------------
# "Add on top" of a version Omni made
# ---------------------------------------------------------------------------

def _omni_parent(scene, tmp_path: Path, *, ghost: bool = False) -> Path:
    """What a version Omni made looks like: the scene with a pink panel it
    painted (top left) and the speaker as the original has her — with
    `ghost`, a second person in its last frames (a Veo tail flash)."""
    frames = []
    for i in range(N):
        f = _room(i)
        cv2.rectangle(f, (16, 24), (130, 110), (180, 105, 255), -1)
        if ghost and i >= N - 8:
            cv2.ellipse(f, (70, int(H * 0.66)), (W // 7, H // 5), 0, 0, 360, GREEN, -1)
        frames.append(f)
    return _encode(tmp_path / "omni" / ("ghost.mp4" if ghost else "v25.mp4"), frames)


def _omni_row(vid: int, final: Path) -> dict:
    return {"id": vid, "run_id": 1, "status": "ready",
            "options": {"target": "scene_0", "recipe": rc.preset_42_default("scene_0").model_dump(mode="json"),
                        "icons_by_pipeline": True},
            "arms": {"omni": {"status": "ready", "final_path": str(final), "stages": {
                "omni": {"c0": {"key": "k", "path": str(final), "entering": True}},
                "guards": {"c0": {"key": None, "path": str(final)}}}}},
            "evidence": {"recipe_hash": "x"}}


def _stacked(ghost: bool, scene, monkeypatch, db, tmp_path) -> tuple[dict, dict, Path, np.ndarray]:
    parent = _omni_parent(scene, tmp_path, ghost=ghost)
    db.rows[25] = _omni_row(25, parent)
    layer = asyncio.run(studio.as_generated(1, "scene_0", rc.normalize(rc.Recipe(
        engine="house", target="scene_0", seed="studio:1:scene_0", elements=[rc.Element(
            id="pattern", kind="pattern", props={"slug": "structure", "layout": None, "colour": "#7f2bf2"})])),
        hbase.BaseSpec(version_id=25, mode="stack")))
    seen = np.zeros((H, W), np.uint8)
    real = house.render

    def spy(spec, source, out, **kw):
        assert kw.get("under") is not None, "drawn over the version's own video"
        return real(spec, source, out, on_plate=lambda i, p: np.maximum(seen, p[..., 3], out=seen), **kw)

    ran = _generate(monkeypatch, db, scene, layer, 46 if ghost else 47, spy=spy,
                    base={"version_id": 25, "mode": "stack"})
    return ran, db.rows[46 if ghost else 47], parent, seen


@needs_kit
def test_an_omni_parent_keeps_its_video_outside_the_plate_and_one_speaker(scene, monkeypatch, db, tmp_path, paid):
    ran, row, parent, seen = _stacked(False, scene, monkeypatch, db, tmp_path)
    out = Path(ran["render"]["path"])
    ctx = ran["ctx"]
    under = Path(ctx.base.sources[0])
    assert ran["render"]["under"] == str(under)
    assert seen.any() and (seen > 0).mean() < 0.7
    a, b = _yuv(under), _yuv(out)
    assert len(a) == len(b) == N, "every frame"
    far = cv2.dilate((seen > 0).astype(np.uint8), np.ones((7, 7), np.uint8)) == 0
    far_uv = far[::2, ::2] & far[1::2, 1::2]
    for plane, (lo, hi, mask) in {"Y": (0, W * H, far), "U": (W * H, W * H * 5 // 4, far_uv),
                                  "V": (W * H * 5 // 4, W * H * 3 // 2, far_uv)}.items():
        x = a[:, lo:hi].reshape((N,) + mask.shape)[:, mask].astype(np.float64)
        y = b[:, lo:hi].reshape((N,) + mask.shape)[:, mask].astype(np.float64)
        psnr = 10 * np.log10(255 ** 2 / max(float(((x - y) ** 2).mean()), 1e-9))
        assert psnr >= 42.0, f"{plane}: {psnr:.1f} dB outside the plate"
    # The panel Omni painted is still there wherever the new plate is not.
    frame = studio._frame_of(out, 40)
    panel = np.zeros((H, W), bool)
    panel[30:104, 22:124] = True
    keep = panel & far
    assert keep.sum() > 200
    assert np.abs(frame[keep].astype(int) - [180, 105, 255]).max() < 24
    # One speaker, against the ORIGINAL's.
    assert gd.one_speaker(out, ctx.target_seg.base, matte=_green) == []
    made = next(p for eid, p in db.patches if eid == 47 and "house" in p)
    assert made["house"]["one_speaker"] == {"checked": True, "frames": []} and made["house"]["on"] == "version"
    assert not any("second person" in w for w in made["warnings"])
    assert paid == []


@needs_kit
def test_a_second_person_in_the_video_under_it_is_said(scene, monkeypatch, db, tmp_path, paid):
    ran, row, _parent, _seen = _stacked(True, scene, monkeypatch, db, tmp_path)
    made = next(p for eid, p in db.patches if eid == 46 and "house" in p)
    flagged = made["house"]["one_speaker"]["frames"]
    assert flagged and min(flagged) >= N - 8, flagged
    t = f"{min(flagged) / FPS:.1f} s"
    assert hbase.SECOND_PERSON.format(t=t) in made["warnings"]
    assert paid == []


# ---------------------------------------------------------------------------
# Adding to a house pass: a version made in house on top of one Omni made
# ---------------------------------------------------------------------------

ON_61 = hbase.BaseSpec(version_id=61, mode="stack")
_RENDER = house.render                     # the real one, whatever a test put in its place


def _spy_into(seen: np.ndarray):
    """house.render, the union of every plate's alpha kept in `seen`."""
    real = _RENDER

    def spy(spec, source, out, **kw):
        assert kw.get("under") is not None, "drawn over the version's own video"
        return real(spec, source, out, on_plate=lambda i, p: np.maximum(seen, p[..., 3], out=seen), **kw)
    return spy


def _house_pass(scene, monkeypatch, db, tmp_path) -> tuple[dict, Path, np.ndarray]:
    """v25 made by Omni (a pink panel it painted), and v61 = v25 + the
    structure pattern and a circle icon, made in house over v25's video."""
    parent = _omni_parent(scene, tmp_path)
    db.rows[25] = _omni_row(25, parent)
    layer = asyncio.run(studio.as_generated(1, "scene_0", rc.normalize(rc.Recipe(
        engine="house", target="scene_0", seed="studio:1:scene_0", elements=[
            rc.Element(id="pattern", kind="pattern", props={"slug": "structure", "layout": None, "colour": "#7f2bf2"}),
            rc.Element(id="icons", kind="icon", props={"style": "circle"})])),
        hbase.BaseSpec(version_id=25, mode="stack"), PLAN_KEY))
    seen = np.zeros((H, W), np.uint8)
    ran = _generate(monkeypatch, db, scene, layer, 61, spy=_spy_into(seen), base={"version_id": 25, "mode": "stack"})
    assert hbase.house_pass(db.rows[61]) and hbase.icons_of(db.rows[61]) == [_res()]
    return ran, parent, seen


def _psnr_outside(a: np.ndarray, b: np.ndarray, seen: np.ndarray) -> dict[str, float]:
    far = cv2.dilate((seen > 0).astype(np.uint8), np.ones((7, 7), np.uint8)) == 0
    far_uv = far[::2, ::2] & far[1::2, 1::2]
    out = {}
    for plane, (lo, hi, mask) in {"Y": (0, W * H, far), "U": (W * H, W * H * 5 // 4, far_uv),
                                  "V": (W * H * 5 // 4, W * H * 3 // 2, far_uv)}.items():
        x = a[:, lo:hi].reshape((len(a),) + mask.shape)[:, mask].astype(np.float64)
        y = b[:, lo:hi].reshape((len(b),) + mask.shape)[:, mask].astype(np.float64)
        out[plane] = 10 * np.log10(255 ** 2 / max(float(((x - y) ** 2).mean()), 1e-9))
    return out


@needs_kit
def test_adding_to_a_house_pass_keeps_omnis_video_under_everything(scene, monkeypatch, db, tmp_path, paid):
    ran61, parent, seen61 = _house_pass(scene, monkeypatch, db, tmp_path)
    # v62 = v61 + beads, added to v61 (the studio's default after Generate).
    new = asyncio.run(studio.as_generated(1, "scene_0", rc.normalize(rc.Recipe(
        engine="house", target="scene_0", seed="studio:1:scene_0", elements=[rc.Element(
            id="pattern", kind="pattern", props={"slug": "beads_row", "layout": None, "colour": "#ffffff"})])),
        ON_61))
    seen62 = np.zeros((H, W), np.uint8)
    ran = _generate(monkeypatch, db, scene, new, 62, spy=_spy_into(seen62), base={"version_id": 61, "mode": "stack"})
    row = db.rows[62]
    assert row["status"] == store.ST_READY and rc.normalize(row["options"]["recipe"]).elements[0].props["slug"] == \
        "beads_row", "its recipe holds only what it added"
    # Over the very video v61 was drawn over: Omni's.
    under = Path(ran61["render"]["under"])
    assert ran["render"]["under"] == str(under) and ran["ctx"].base.under == under
    assert [e["id"] for e in ran["ctx"].base.passes] == [61] and ran["ctx"].base.ground["id"] == 25
    out = Path(ran["render"]["path"])
    a, b = _yuv(under), _yuv(out)
    assert len(a) == len(b) == N, "every frame"
    for plane, psnr in _psnr_outside(a, b, seen62).items():
        assert psnr >= 42.0, f"{plane}: {psnr:.1f} dB outside the plate — Omni's video kept"
    # Every pixel v61 drew, v62 draws too (its pattern and its icon, again),
    # and more: the beads.
    drawn61 = seen61 > 0
    assert drawn61.sum() > 500 and np.all(seen62[drawn61].astype(int) >= seen61[drawn61].astype(int) - 1)
    assert (seen62 > 0).sum() > drawn61.sum() + 200
    layers = next(p for eid, p in db.patches if eid == 62 and "house" in p)["house"]["layers"]
    assert [(ly["element_id"], ly["slug"], ly.get("kept")) for ly in layers] == [
        ("pattern", "structure", 61), ("pattern", "beads_row", None)], "v61's own, then the new one (same id)"
    # The panel Omni painted is still there wherever no plate is.
    frame = studio._frame_of(out, 40)
    panel = np.zeros((H, W), bool)
    panel[30:104, 22:124] = True
    keep = panel & (cv2.dilate((seen62 > 0).astype(np.uint8), np.ones((7, 7), np.uint8)) == 0)
    if keep.sum() > 200:
        assert np.abs(frame[keep].astype(int) - [180, 105, 255]).max() < 24
    assert gd.one_speaker(out, ran["ctx"].target_seg.base, matte=_green) == []
    assert paid == []


@needs_kit
def test_an_unchanged_draft_on_a_house_pass_is_that_version(scene, monkeypatch, db, tmp_path, paid):
    ran61, _parent, _seen = _house_pass(scene, monkeypatch, db, tmp_path)
    key = ran61["render"]["key"]
    final = Path(ran61["render"]["path"])
    assert house.draft_path(scene.work, key).read_bytes() == final.read_bytes(), "the run handed its render over"

    # Carried on from v61 (added to it): nothing new, no icon element — v61's
    # own layers and icons, over v61's video: v61's very spec.
    empty = rc.normalize(rc.Recipe(engine="house", target="scene_0", seed="studio:1:scene_0", elements=[]))
    hs = asyncio.run(studio.house_spec(1, "scene_0", asyncio.run(studio.as_generated(1, "scene_0", empty, ON_61)),
                                       ON_61))
    assert hs.key == key and hs.under == Path(ran61["render"]["under"])

    # As the studio sends it: v61's icon element, "Keep v61's icons".
    draft = rc.normalize(rc.Recipe(engine="house", target="scene_0", seed="studio:1:scene_0", elements=[
        rc.Element(id="icons", kind="icon", props={"style": "circle", "keep": 61})]))

    async def planned():
        for _ in range(200):
            got = await studio.icon_plan(1, "scene_0", draft, ON_61)
            if got["state"] == "ready":
                return got
            await asyncio.sleep(0.01)
        return got

    body = asyncio.run(planned())
    assert body["state"] == "ready" and body["kept"] == {"version_id": 61}
    stored = asyncio.run(studio.as_generated(1, "scene_0", draft, ON_61, body["plan_key"]))
    assert asyncio.run(studio.house_spec(1, "scene_0", stored, ON_61)).key == key

    # Play on Next: v61's own render, at once — nothing rendered again.
    def never(*_a, **_k):
        raise AssertionError("rendered again")

    monkeypatch.setattr(house, "render", never)
    got = asyncio.run(studio.house_render(1, "scene_0", draft, ON_61, body["plan_key"]))
    assert got["state"] == "ready" and got["key"] == key
    assert _png_path(got["url"]).read_bytes() == final.read_bytes()
    assert asyncio.run(studio.moment(1, "scene_0", draft, 1.0, ON_61, plan_key=body["plan_key"]))["key"] == key
    # Next: v61's still is taken from that very video (it is a pass on top)…
    assert asyncio.run(hbase.chunk_sources(db.rows[61])) == [final]
    # …and the frames a new layer is drawn on show v61's pattern over Omni's
    # video (not the bare video under it), its ink kept off by what comes next.
    bv = asyncio.run(studio.prepare_base(1, "scene_0", 61))
    assert [Path(f).name for f in bv.frames] == ["c0_kept.png"] and [k.version_id for k in bv.kept] == [61]
    shown = cv2.imread(str(bv.frames[0])).astype(int)
    bare = cv2.imread(str(Path(bv.frames[0]).with_name("c0_first.png"))).astype(int)
    ink = np.abs(shown - bare).max(axis=2) > 12
    assert ink.sum() > 500, "v61's pattern is on Next's frame"
    assert (ink & ~(np.asarray(bv.occupied) > 0)).mean() < 0.002, "and a new layer keeps off it"
    assert paid == []


def test_the_server_adds_to_a_house_pass_and_changes_a_flat_house_version(monkeypatch, db, tmp_path):
    from fastapi import HTTPException

    monkeypatch.setattr(pipeline, "REBRAND_ROOT", tmp_path / "rebrand")
    clip = tmp_path / "v.mp4"
    clip.write_bytes(b"x")
    house_opts = {"target": "scene_0", "engine": "house", "recipe": rc.preset_42_house("scene_0").model_dump(mode="json")}
    made = {"status": "ready", "arms": {"omni": {"status": "ready", "engine": "house", "final_path": str(clip),
                                                  "stages": {"render": {"c0": {"path": str(clip), "under": str(clip)}}}}}}
    db.rows[25] = _omni_row(25, clip)
    db.rows[61] = {"id": 61, "run_id": 1, **json.loads(json.dumps(made)),
                   "options": {**house_opts, "base": {"version_id": 25, "mode": "stack"}}}
    db.rows[44] = {"id": 44, "run_id": 1, **json.loads(json.dumps(made)), "options": house_opts}
    created: list[dict] = []

    async def run_row(run_id):
        return {"metadata": {}}

    async def none(*_a, **_k):
        return []

    async def create(run_id, options):
        created.append(options)
        return {"id": 99, "run_id": run_id, "options": options, "status": "queued"}

    async def ran(*_a, **_k):
        return None

    async def get(slug):
        return lib.BUILTINS.get(slug)

    monkeypatch.setattr(lib, "get", get)
    monkeypatch.setattr(pipeline.th, "_fetch_run_row", run_row)
    monkeypatch.setattr(store, "list_for_run", none)
    monkeypatch.setattr(store, "create_experiment", create)
    monkeypatch.setattr(pipeline, "_run", ran)
    monkeypatch.setattr(pattern_el, "check", none)
    beads = rc.normalize(rc.Recipe(engine="house", target="scene_0", elements=[
        rc.Element(id="pattern", kind="pattern", props={"slug": "beads_row"})]))

    async def go(recipe, vid):
        return await pipeline.start(1, None, recipe=recipe, base={"version_id": vid, "mode": "stack"})

    assert asyncio.run(go(beads, 61))["id"] == 99
    assert created[-1]["base"] == {"version_id": 61, "mode": "stack"} and created[-1]["engine"] == "house"
    with pytest.raises(HTTPException) as err:
        asyncio.run(go(beads, 44))
    assert err.value.status_code == 409 and err.value.detail == hbase.ADD_ON_HOUSE
    # A recipe for Omni: refused while Omni is switched off (I16); switched
    # back on, Omni still never edits a video made in house.
    with pytest.raises(HTTPException) as err:
        asyncio.run(go(rc.preset_42_default("scene_0"), 61))
    assert err.value.status_code == 400 and rc.OMNI_OFF in err.value.detail
    monkeypatch.setenv("REBRAND_OMNI", "on")
    with pytest.raises(HTTPException) as err:
        asyncio.run(go(rc.preset_42_default("scene_0"), 61))
    assert err.value.status_code == 409 and err.value.detail == hbase.OMNI_ON_HOUSE


def test_a_chains_ground_and_passes():
    omni_v = {"id": 25, "options": {"target": "scene_0"}}
    p1 = {"id": 61, "options": {"engine": "house", "base": {"version_id": 25, "mode": "stack"}}}
    p2 = {"id": 62, "options": {"engine": "house", "base": {"version_id": 61, "mode": "stack"}}}
    flat = {"id": 44, "options": {"engine": "house"}}
    spec = hbase.BaseSpec(version_id=62, mode="stack")
    bv = hbase.BaseVersion(spec=spec, exp=p2, chain=[p2, p1, omni_v], layers=[])
    assert bv.ground is omni_v and [e["id"] for e in bv.passes] == [61, 62]
    assert hbase.BaseVersion(spec=spec, exp=omni_v, chain=[omni_v], layers=[]).passes == []
    assert hbase.BaseVersion(spec=spec, exp=flat, chain=[flat], layers=[]).ground is flat
    cut = hbase.BaseVersion(spec=spec, exp=p2, chain=[p2, p1], layers=[])
    assert cut.ground is None and [e["id"] for e in cut.passes] == [61, 62]
    assert not hbase.house_pass(flat) and not hbase.house_pass(omni_v) and hbase.house_pass(p1)
    # A pass on top with no icon element stamps what the version shows.
    shows = {**p2, "arms": {"omni": {"stages": {"icons": {"plan": {"resolved": [_res()]}}}}}}
    on = hbase.BaseVersion(spec=spec, exp=shows, chain=[shows, p1, omni_v], layers=[])
    bare = rc.normalize(rc.Recipe(engine="house", target="scene_0", elements=[]))
    assert hbase.inherited_icons(bare, on) == [_res()]
    assert hbase.inherited_icons(_recipe(), on) is None, "its own icon element: its own"
    assert hbase.inherited_icons(bare, hbase.BaseVersion(spec=spec, exp=p2, chain=[p2, p1, omni_v],
                                                         layers=[])) is None, "it shows none"


# ---------------------------------------------------------------------------
# base.py reads a house version from its render stage
# ---------------------------------------------------------------------------

def test_base_reads_a_house_version_from_its_render_stage(tmp_path, monkeypatch):
    monkeypatch.setattr(pipeline, "REBRAND_ROOT", tmp_path / "rebrand")
    final = tmp_path / "v.mp4"
    final.write_bytes(b"x")
    row = {"id": 50, "run_id": 1, "status": "ready", "options": {"target": "scene_0", "engine": "house"},
           "arms": {"omni": {"status": "ready", "engine": "house", "final_path": str(final), "stages": {
               "render": {"c0": {"key": "k" * 20, "path": str(final), "entering": False, "settles": True}}}}}}
    assert hbase.part_count(row) == 1 and hbase.entering(row, 0) is True, "its render says it settles"
    assert asyncio.run(hbase.chunk_sources(row)) == [final] and hbase.sources_known(row)
    assert hbase.final_file(row) == final and not hbase.lost(row)
    final.unlink()
    assert hbase.lost(row), "gone, and no bucket copy: it can be made again"
    assert not hbase.lost({**row, "arms": {"omni": {**row["arms"]["omni"], "storage_bucket": "b",
                                                    "storage_path": "p"}}})
    # A version that doesn't enter says so; an old Omni row reads as it did.
    quiet = json.loads(json.dumps(row))
    quiet["arms"]["omni"]["stages"]["render"]["c0"].update(settles=False)
    assert hbase.entering(quiet, 0) is False
    old = {"id": 51, "status": "ready", "options": {"target": "scene_0"},
           "arms": {"omni": {"status": "ready", "stages": {"omni": {"c0": {"entering": False}, "c1": {}}}}}}
    assert hbase.part_count(old) == 2 and hbase.entering(old, 0) is False and not hbase.lost(old)


def test_the_router_lists_a_lost_version_and_makes_it_again(monkeypatch, tmp_path):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.routers import rebrand_harness as rt

    monkeypatch.setattr(pipeline, "REBRAND_ROOT", tmp_path / "rebrand")
    row = {"id": 60, "run_id": 1, "status": "ready", "stage": "Done",
           "options": {"target": "scene_0", "engine": "house",
                       "recipe": rc.preset_42_house("scene_0").model_dump(mode="json")},
           "arms": {"omni": {"status": "ready", "engine": "house", "final_path": str(tmp_path / "gone.mp4"),
                             "stages": {"render": {"c0": {"path": str(tmp_path / "gone.mp4"), "entering": True}}}}},
           "created_at": None, "updated_at": None}

    async def listed(run_id, *, limit, target):
        return [row]

    async def remake(exp_id):
        return {"version_id": exp_id, "state": "running", "progress": 0.0, "reused": False}

    def state(exp_id):
        return {"version_id": exp_id, "state": "ready", "progress": 1.0, "reused": False, "same": True}

    async def run_row(run_id):
        return {"metadata": {}}

    monkeypatch.setattr(store, "list_for_run", listed)
    monkeypatch.setattr(studio, "remake", remake)
    monkeypatch.setattr(studio, "remake_state", state)
    monkeypatch.setattr(rt.th, "_fetch_run_row", run_row)
    app = FastAPI()
    app.include_router(rt.router)
    c = TestClient(app)
    listing = c.get("/api/rebrand/harness/versions", params={"run_id": 1, "target": "scene_0"}).json()
    assert listing["versions"][0]["lost"] is True
    assert c.post("/api/rebrand/harness/versions/60/remake").json()["state"] == "running"
    assert c.get("/api/rebrand/harness/versions/60/remake").json()["same"] is True
