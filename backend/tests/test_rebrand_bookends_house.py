"""The reel's intro and outro in the Rebrand studio (S8): done by hand, made
in house.

  targets       "intro" / "outro" are studio targets beside scene_N (the
                request, the recipe, the pipeline, a version's file label),
                and widening the pattern moves no hash
  the clip      the studio rebrands the very clip the reel uses there
                (th_reel.resolve_target), on the canvas as the reel shows it
                (cover), whole: a card becomes a 120-frame cover clip, a
                video is never trimmed, and nothing is carried over
  by hand       the draft starts empty; nothing is said on a bookend, so no
                words are heard (no Whisper) and the icon director is never
                asked, even for a recipe with ai: true — an added icon still
                shows after a pattern's entrance on a 4 s clip (U3)
  no person     the reel's own card or card animation has no one in it (U6:
                MODNet's ghosts on cards are no speaker), so it cuts nothing
                out, holds its patterns still on screen, needs no speaker's
                mask to keep a preview and warns of none; an uploaded video
                is judged by its matte bank; the rule changes nothing for a
                scene (plate_spec's `person` None)
  made in house Generate on a bookend runs the in-house engine over the
                studio's own clip (the draft's render reused), lists only
                versions made in house, and a run made in house that ever
                reached Omni stops before any call
  run 815       its real intro and outro (anim-352, anim-353) prepared,
                previewed, planned and generated through the real router:
                119 frames, silent sound (skipped without the files or
                MODNet)

Every paid client raises and is remembered, Whisper and the director too;
the experiments, the reel and the team's icons live in memory; nothing
touches the database, a bucket or a server."""
from __future__ import annotations

import asyncio
import copy
import itertools
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pytest
from fastapi import FastAPI, HTTPException
from pydantic import ValidationError

from app import db as app_db
from app.routers import rebrand_harness as rt
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
from app.services import th_reel
from app.services import vertex_client
from app.services import yas_talking_head_v2 as th
from app.services.rebrand_harness import base as hbase
from app.services.rebrand_harness import director as dmod
from app.services.rebrand_harness import house
from app.services.rebrand_harness import mattes
from app.services.rebrand_harness import recipe as rc
from app.services.rebrand_harness import studio
from app.services.rebrand_harness import words as wordsmod
from app.services.rebrand_harness.elements import pattern as pattern_el
from app.workers import yas_th_v2_composition_poller as poller

cv2 = pytest.importorskip("cv2")
httpx = pytest.importorskip("httpx")
pytestmark = pytest.mark.omni_default           # as a server runs: Omni off

W, H, FPS = 720, 1280, 24
RUN = 815
GREEN = (40, 200, 60)                            # the fake MODNet's "person", BGR
REAL_MASKS = pattern_ref._mask_path("structure").exists()
needs_kit = pytest.mark.skipif(not REAL_MASKS, reason="the pattern kit's masks are not here")
DATA = th._YAS_ROOT
REAL = {352: [DATA / "animations" / "anim-352.mp4",
              DATA / "talking-head-v2" / "rebrand" / "_cache" / "anim-352.mp4"],
        353: [DATA / "animations" / "anim-353.mp4",
              DATA / "talking-head-v2" / "rebrand" / "_cache" / "anim-353.mp4"]}


def _found(anim_id: int):
    return next((p for p in REAL[anim_id] if p.is_file()), None)


# ---------------------------------------------------------------------------
# The world: nothing paid, nothing shared
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def paid(monkeypatch, tmp_path):
    """Every paid client raises and is remembered — Whisper and the icon
    director too; the database cannot be reached; no bucket; every folder
    the studio and the pipeline write is the test's own."""
    asked: list[str] = []

    def refusing(name, is_async=True):
        async def refuse(*_a, **_k):
            asked.append(name)
            raise AssertionError(f"refused in a test: {name}")

        def refuse_sync(*_a, **_k):
            asked.append(name)
            raise AssertionError(f"refused in a test: {name}")
        return refuse if is_async else refuse_sync

    for mod, name in ((omni, "edit_video"), (omni, "interpolate"), (vertex_client, "gemini_text"),
                      (vertex_client, "gemini_multimodal"), (vertex_client, "gemini_image"),
                      (vertex_client, "gemini_embed"), (person_matte, "person_mask"),
                      (rebrand_brief, "build_brief"), (rebrand_brief, "critique"), (store, "store_memory"),
                      (dmod, "direct")):
        if hasattr(mod, name):
            monkeypatch.setattr(mod, name, refusing(f"{mod.__name__.rsplit('.', 1)[-1]}.{name}"))
    monkeypatch.setattr(wordsmod, "transcribe", refusing("whisper", is_async=False))
    for mod in (app_db, th_reel, pipeline, poller, store, th):
        if hasattr(mod, "get_conn"):
            monkeypatch.setattr(mod, "get_conn", refusing("db.get_conn", is_async=False))

    async def no_persist(*_a, **_k):
        return None, None

    async def no_url(*_a, **_k):
        return None

    monkeypatch.setattr(asset_storage, "persist_local_file", no_persist)
    monkeypatch.setattr(asset_storage, "url_for", no_url)
    monkeypatch.setattr(studio, "STUDIO_ROOT", tmp_path / "rebrand" / "_studio")
    monkeypatch.setattr(studio, "ANALYSIS_ROOT", tmp_path / "rebrand" / "_cache" / "scene")
    monkeypatch.setattr(pipeline, "REBRAND_ROOT", tmp_path / "rebrand")
    for name in ("_PREPARED", "_ANALYSES", "_ICON_TASKS", "_HOUSE_SPECS", "_PREVIEWS", "_MASK_TASKS", "_DRAFTS",
                 "_CLAIMED", "_LOCKS", "_COLOUR_TASKS", "_BASES", "_STACKS", "_REMAKES"):
        monkeypatch.setattr(studio, name, {})

    async def get_pattern(slug):
        return lib.BUILTINS.get(slug)

    async def list_patterns():
        return list(lib.BUILTINS.values())

    monkeypatch.setattr(lib, "get", get_pattern)
    monkeypatch.setattr(lib, "lookup", get_pattern)
    monkeypatch.setattr(lib, "list_patterns", list_patterns)
    return asked


def _fake_raw(bgr) -> np.ndarray:
    """MODNet, faked: the green is the person."""
    d = np.abs(bgr.astype(np.int16) - np.array(GREEN, np.int16)).max(axis=2)
    g = (d < 40).astype(np.float32)
    return cv2.resize(g, (max(1, bgr.shape[1] // 2), max(1, bgr.shape[0] // 2)), interpolation=cv2.INTER_AREA)


@pytest.fixture
def fake_modnet(monkeypatch):
    monkeypatch.setattr(speaker_front, "raw_matte", _fake_raw)

    async def local_mask(frame):
        img = cv2.imread(str(frame), cv2.IMREAD_COLOR)
        m = cv2.resize(_fake_raw(img), (img.shape[1], img.shape[0]), interpolation=cv2.INTER_LINEAR)
        return m if float((m > 0.5).mean()) > 0.01 else None

    monkeypatch.setattr(person_matte, "local_mask", local_mask)


def _encode(path: Path, frames, *, size=(W, H), fps=FPS, audio: bool = False) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [media.ffmpeg_exe(), "-y", "-loglevel", "error", "-f", "rawvideo", "-pix_fmt", "bgr24",
           "-s", f"{size[0]}x{size[1]}", "-r", str(fps), "-i", "-"]
    if audio:
        cmd += ["-f", "lavfi", "-i", "sine=frequency=330:sample_rate=44100", "-map", "0:v", "-map", "1:a",
                "-shortest", "-c:a", "aac"]
    cmd += ["-c:v", "libx264", "-preset", "ultrafast", "-crf", "14", "-pix_fmt", "yuv420p", str(path)]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE)
    for f in frames:
        proc.stdin.write(np.ascontiguousarray(f).tobytes())
    proc.stdin.close()
    assert proc.wait(timeout=120) == 0
    return path


def _card_frame(i: int, w: int = W, h: int = H, person: bool = False) -> np.ndarray:
    """A branded card's frame: a dark ground, a logo block and a headline bar
    growing in; a person (the green) only when asked."""
    f = np.full((h, w, 3), (48, 40, 36), np.uint8)
    cv2.rectangle(f, (w // 10, h // 12), (w // 10 + w // 5, h // 12 + h // 20), (240, 240, 240), -1)
    reach = int((w * 0.8) * min(1.0, (i + 1) / 30))
    cv2.rectangle(f, (w // 10, h // 5), (w // 10 + reach, h // 5 + h // 24), (242, 43, 127), -1)
    cv2.circle(f, (w // 2, int(h * 0.62)), w // 5 + (i % 12), (200, 120, 60), -1)
    if person:
        cv2.ellipse(f, (w // 2, int(h * 0.7)), (w // 5, h // 4), 0, 0, 360, GREEN, -1)
    return f


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class World:
    """Run 815 as the tests have it: its row, its animation rows, and the
    experiments table in memory."""

    def __init__(self, tmp_path: Path, intro: Path, outro: Path, *, intro_card: Path = None,
                 outro_upload: Path = None):
        self.md = {"scene_count": 1, "aspect_ratio": "9:16", "main_animation_id": 351, "veo_animation_id": 351,
                   "intro_animation_id": 352, "outro_animation_id": 353, "intro_image_id": 816,
                   "outro_image_id": 817, "composition_status": "videos_ready",
                   "refs": {"intro_video_upload_id": None,
                            "outro_video_upload_id": 77 if outro_upload is not None else None},
                   "script": {"dialogue": "42 Abu Dhabi is where your future becomes uncoded.",
                              "intro_headline": "Unlock Your Coding Future"}}
        self.run = {"id": RUN, "topic": "Why 42", "metadata": self.md}
        main = tmp_path / "src" / "anim-351.mp4"
        self.anims = {351: {"id": 351, "status": "ready", "local_path": str(main), "duration_sec": 8},
                      352: {"id": 352, "status": "ready", "local_path": str(intro), "duration_sec": 4},
                      353: {"id": 353, "status": "ready", "local_path": str(outro), "duration_sec": 4}}
        self.cards = {816: intro_card}
        self.uploads = {77: outro_upload}
        self.rows: dict[int, dict] = {}
        self.ids = itertools.count(9001)


@pytest.fixture
def world(monkeypatch, tmp_path):
    def make(intro: Path, outro: Path, *, intro_card: Path = None, outro_upload: Path = None) -> World:
        w = World(tmp_path, intro, outro, intro_card=intro_card, outro_upload=outro_upload)

        async def run_row(run_id):
            if int(run_id) != RUN:
                raise HTTPException(404, "only run 815 here")
            return copy.deepcopy(w.run)

        async def anim_row(anim_id):
            return copy.deepcopy(w.anims.get(int(anim_id)))

        async def card_path(card_id):
            return w.cards.get(int(card_id or 0))

        async def upload_path(upload_id):
            return w.uploads.get(int(upload_id or 0))

        monkeypatch.setattr(poller, "_fetch_upload_path", upload_path)

        async def card_for(*, run_id, md, refs):
            return w.cards.get(int(md.get("intro_image_id") or 0))

        monkeypatch.setattr(th, "_fetch_run_row", run_row)
        monkeypatch.setattr(pipeline, "_anim_row", anim_row)
        monkeypatch.setattr(poller, "_fetch_card_path", card_path)
        monkeypatch.setattr(th, "_resolve_intro_card_path", card_for)
        th_reel.set_store(th_reel.MemoryStore({RUN: w.run}, anim_rows=w.anims))

        async def create(run_id, options):
            i = next(w.ids)
            w.rows[i] = {"id": i, "run_id": int(run_id), "status": store.ST_QUEUED, "stage": "queued",
                         "options": copy.deepcopy(options), "brief": None, "evidence": None,
                         "arms": {a: {"status": store.ARM_QUEUED, "steps": []} for a in store.ARMS},
                         "winner": None, "rating": None, "lesson": None, "error": None, "cost_usd": 0.0,
                         "created_at": _now(), "updated_at": _now()}
            return copy.deepcopy(w.rows[i])

        async def get(exp_id):
            row = w.rows.get(int(exp_id))
            return copy.deepcopy(row) if row else None

        async def listed(run_id, *, limit=12, target=None):
            rows = [r for r in w.rows.values() if r["run_id"] == int(run_id)
                    and (not target or (r["options"].get("target") or "reel") == target)]
            return [copy.deepcopy(r) for r in sorted(rows, key=lambda r: -r["id"])[:limit]]

        async def update(exp_id, **fields):
            w.rows[int(exp_id)].update(copy.deepcopy(fields))

        async def patch_arm(exp_id, arm, patch):
            w.rows[int(exp_id)]["arms"].setdefault(arm, {}).update(json.loads(json.dumps(patch, default=str)))

        async def step(exp_id, arm, s):
            w.rows[int(exp_id)]["arms"].setdefault(arm, {}).setdefault("steps", []).append(s)

        async def quiet(*_a, **_k):
            return None

        async def mark(exp, *, live_ids):
            return exp

        for name, fn in (("create_experiment", create), ("get_experiment", get), ("list_for_run", listed),
                         ("update_experiment", update), ("patch_arm", patch_arm), ("append_arm_step", step),
                         ("add_cost", quiet), ("touch", quiet), ("mark_interrupted_if_orphaned", mark),
                         ("ensure_table", quiet)):
            monkeypatch.setattr(store, name, fn)
        return w

    return make


def _client():
    app = FastAPI()
    app.include_router(rt.router)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t",
                             timeout=httpx.Timeout(600.0))


async def _poll(fn, done, *, every=0.2, limit=300.0):
    loop = asyncio.get_running_loop()
    t0 = loop.time()
    while True:
        body = await fn()
        if done(body):
            return body
        assert loop.time() - t0 < limit, f"timed out: {json.dumps(body)[:400]}"
        await asyncio.sleep(every)


def _audio_peak_db(path: Path) -> float:
    out = subprocess.run([media.ffmpeg_exe(), "-hide_banner", "-i", str(path), "-map", "0:a:0", "-af",
                          "volumedetect", "-f", "null", "-"], capture_output=True, text=True, errors="replace")
    line = next(ln for ln in out.stderr.splitlines() if "max_volume" in ln)
    return float(line.split("max_volume:")[-1].strip().split()[0])


def _draft(prep: dict, *, pattern: bool = True, added: bool = True) -> dict:
    """What the studio sends for a bookend drawn by hand: its empty default
    with a pattern (auto place, its own entrance) and icons — with ai: true,
    as the icon element's default props carry it — and one added icon."""
    d = copy.deepcopy(prep["default_recipe"])
    if pattern:
        d["elements"].append({"id": "pattern", "kind": "pattern", "placement": {"anchor": "auto"},
                              "animation": {"entrance": "auto"},
                              "props": {"slug": "structure", "layout": None, "colour": "auto"}})
    d["elements"].append({"id": "icons", "kind": "icon", "placement": {"anchor": "auto"},
                          "animation": {"entrance": "auto"},
                          "props": {"ai": True, "style": "circle",
                                    **({"added": [{"glyph": {"source": "library", "id": "code"},
                                                   "label": "Code"}]} if added else {})}})
    return d


# ---------------------------------------------------------------------------
# Targets
# ---------------------------------------------------------------------------

def test_the_intro_and_outro_are_studio_targets_and_no_hash_moves():
    for target in ("intro", "outro", "scene_0", "scene_12"):
        assert rt.SceneRequest(run_id=1, target=target).target == target
        assert rc.Recipe(engine="house", target=target).target == target
    for bad in ("reel", "scene_100", "intro1", ""):
        with pytest.raises(ValidationError):
            rt.SceneRequest(run_id=1, target=bad)
    assert studio._target_of("intro") == ("intro", 0) and studio._target_of("scene_3") == ("scene", 3)
    with pytest.raises(HTTPException):
        studio._target_of("reel")
    assert [hbase.label({"options": {"target": t}}) for t in ("intro", "outro", "scene_2")] == \
        ["intro", "outro", "scene_3"]
    # The 42 default's pinned hashes (test_rebrand_harness_recipe) stand.
    assert rc.recipe_hash(rc.preset_42_default("scene_0")) == rc.recipe_hash(
        rc.Recipe.model_validate(rc.preset_42_default("scene_0").model_dump(mode="json")))


def test_a_bookend_starts_empty_and_a_scene_as_ever():
    intro = rt._default_recipe(RUN, "intro")
    assert intro["engine"] == "house" and intro["elements"] == [] and intro["target"] == "intro"
    assert intro["seed"] == "studio:815:intro"
    scene = rt._default_recipe(RUN, "scene_0")
    assert scene == rc.preset_42_house("scene_0").model_copy(
        update={"seed": "studio:815:scene_0"}).model_dump(mode="json")


# ---------------------------------------------------------------------------
# The clip
# ---------------------------------------------------------------------------

def test_a_card_becomes_a_120_frame_cover_clip(tmp_path):
    """A card of another shape fills the canvas (the reel's templates cover
    it): no black bars, 4 s at 30 fps, a silent sound track, described whole."""
    card = tmp_path / "card.png"
    img = np.zeros((800, 600, 3), np.uint8)
    img[:] = (30, 160, 230)
    cv2.imwrite(str(card), img)
    out = studio._bookend_clip(card, tmp_path / "b" / "intro.mp4", still=True, seconds=4.0, width=W, height=H)
    assert media.frame_count(out) == 120 and media.video_size(out) == (W, H)
    cap = cv2.VideoCapture(str(out))
    ok, f = cap.read()
    cap.release()
    assert ok
    for y, x in ((2, 2), (2, W - 3), (H - 3, 2), (H - 3, W - 3), (H // 2, 2)):
        assert int(f[y, x].max()) > 100, "a black bar: the card was letterboxed, not covered"
    assert media.has_audio(out) and _audio_peak_db(out) <= -90.0
    assert media.scene_info(out) == {"source_fps": 30.0, "source_cut": None, "trimmed_sec": 0.0, "cuts": []}
    assert sorted(p.name for p in out.parent.iterdir()) == ["intro.cuts.json", "intro.mp4"]


def test_cover_is_a_new_fit_and_the_default_command_is_unchanged(tmp_path, monkeypatch):
    seen: list = []
    monkeypatch.setattr(media, "_run", lambda cmd, **kw: seen.append(cmd))
    monkeypatch.setattr(media, "has_audio", lambda p: False)
    monkeypatch.setattr(media, "probe_duration", lambda p: 4.0)
    src = tmp_path / "src.mp4"
    media.still_to_clip(src, tmp_path / "a.mp4", seconds=4.0, width=W, height=H)
    assert seen[-1][seen[-1].index("-filter_complex") + 1] == (
        f"[0:v]scale={W}:{H}:force_original_aspect_ratio=decrease,"
        f"pad={W}:{H}:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1,fps=30,format=yuv420p[v]")
    media.still_to_clip(src, tmp_path / "b.mp4", seconds=4.0, width=W, height=H, fit="cover")
    assert seen[-1][seen[-1].index("-filter_complex") + 1] == (
        f"[0:v]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},setsar=1,"
        f"fps=30,format=yuv420p[v]")
    media.normalize(src, tmp_path / "c.mp4", width=W, height=H, audio="keep", fit="cover")
    assert seen[-1][seen[-1].index("-filter_complex") + 1].startswith(
        f"[0:v]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},setsar=1,setpts=PTS/1.00000,")


def test_a_bookend_folder_is_never_trimmed(tmp_path):
    """A flash-like jump at the end of a 4 s card animation stays in it (an
    intro or outro is never cut automatically): its folder has no _t."""
    frames = [_card_frame(i) for i in range(90)] + [np.full((H, W, 3), 230, np.uint8)] * 6
    src = _encode(tmp_path / "src" / "anim-352.mp4", frames)
    assert media.tail_cut(src) is not None, "the synthetic clip ends on a jump"
    assert "_t" not in studio.scene_base(RUN, "intro", src).name
    assert studio.scene_base(RUN, "scene_0", src).name.endswith(f"_t{media.tail_cut(src)}")


# ---------------------------------------------------------------------------
# Prepared, planned, previewed and generated — for free
# ---------------------------------------------------------------------------

@needs_kit
def test_a_bookend_by_hand_is_prepared_planned_previewed_and_generated_for_free(
        tmp_path, world, fake_modnet, paid):
    intro_src = _encode(tmp_path / "src" / "anim-352.mp4", (_card_frame(i) for i in range(96)), audio=True)
    outro_src = _encode(tmp_path / "src" / "anim-353.mp4", (_card_frame(i, person=True) for i in range(96)))
    # The outro is an uploaded video with someone in it (the green).
    upload = _encode(tmp_path / "src" / "upload-77.mp4", (_card_frame(i, person=True) for i in range(96)))
    w = world(intro_src, outro_src, outro_upload=upload)
    # An Omni-era card version of the intro (its transition baked in): never
    # listed for the bookend.
    w.rows[8000] = {"id": 8000, "run_id": RUN, "status": "ready", "options": {"target": "intro", "run_omni": True},
                    "arms": {"omni": {"status": "ready"}}, "created_at": _now(), "updated_at": _now()}

    async def go() -> dict:
        out: dict = {}
        async with _client() as c:
            prep = (await c.post("/api/rebrand/harness/prepare", json={"run_id": RUN, "target": "intro"})).json()
            out["prep"] = prep
            await _poll(lambda: c.get("/api/rebrand/harness/warmup", params={"run_id": RUN, "target": "intro"}),
                        lambda r: r.json().get("camera") in ("ready", "failed"))
            out["warm"] = (await c.get("/api/rebrand/harness/warmup",
                                       params={"run_id": RUN, "target": "intro"})).json()
            out["camera"] = (await c.get("/api/rebrand/harness/house/camera",
                                         params={"run_id": RUN, "target": "intro"})).json()
            draft = _draft(prep)
            body = {"run_id": RUN, "target": "intro", "recipe": draft}
            r = await c.post("/api/rebrand/harness/preview", json=body)
            assert r.status_code == 200, r.text
            out["preview"] = r.json()
            plan = await _poll(lambda: c.post("/api/rebrand/harness/icons/plan", json=body),
                               lambda r: r.json().get("state") == "ready")
            out["plan"] = plan.json()
            gen = {**body, "icons_plan_key": out["plan"]["plan_key"]}
            dr = (await c.post("/api/rebrand/harness/house/render", json=gen)).json()
            out["draft"] = (await _poll(
                lambda: c.get(f"/api/rebrand/harness/house/render/{dr['key']}",
                              params={"run_id": RUN, "target": "intro"}),
                lambda r: r.json().get("state") in ("ready", "failed", "cancelled"))).json()
            r = await c.post("/api/rebrand/harness/versions", json=gen)
            assert r.status_code == 200, r.text
            vid = r.json()["id"]
            listed = await _poll(lambda: c.get("/api/rebrand/harness/versions",
                                               params={"run_id": RUN, "target": "intro"}),
                                 lambda r: any(v["id"] == vid and v["status"] in ("ready", "failed")
                                               for v in r.json()["versions"]))
            out["versions"] = listed.json()["versions"]
            out["vid"] = vid
            # The outro, an uploaded video with someone in it (the green):
            # judged by its matte, and cut as a scene is.
            prep2 = (await c.post("/api/rebrand/harness/prepare", json={"run_id": RUN, "target": "outro"})).json()
            await _poll(lambda: c.get("/api/rebrand/harness/warmup", params={"run_id": RUN, "target": "outro"}),
                        lambda r: r.json().get("camera") in ("ready", "failed"))
            out["outro_warm"] = (await c.get("/api/rebrand/harness/warmup",
                                             params={"run_id": RUN, "target": "outro"})).json()
            r = await c.post("/api/rebrand/harness/preview",
                             json={"run_id": RUN, "target": "outro", "recipe": _draft(prep2, added=False)})
            out["outro_preview"] = r.json()
        return out

    out = asyncio.run(go())
    scene = out["prep"]["scene"]
    assert scene["kind"] == "intro" and out["prep"]["default_recipe"]["elements"] == []
    sc = studio._PREPARED[(RUN, "intro")]
    assert sc.work.name.startswith("intro_") and "_t" not in sc.work.name
    assert sc.ctx.target_seg.source == intro_src, "the reel's own clip for the intro"
    assert media.scene_info(sc.ctx.target_seg.base)["source_cut"] is None
    assert not (studio._icon_dir(sc) / "words.json").exists(), "nothing heard, nothing carried"
    assert out["prep"]["versions"] == [], "an Omni-era card version is not listed"
    # Nobody in it: said, and no cut.
    assert out["warm"]["has_person"] is False and out["camera"]["has_person"] is False
    pv = out["preview"]
    assert pv["kept"] is True and pv["warnings"] == [] and pv["engine"] == "house"
    assert pv["house"]["person"] is False and [ly["follow"] for ly in pv["house"]["layers"]] == ["screen"]
    # The icons: no words heard, no director asked (ai: true sent), and the
    # added icon shows after the pattern's entrance on a 4 s clip (U3).
    plan = out["plan"]
    assert plan["source"] == {"words": "estimate", "director": "keywords"}
    assert plan["warnings"] == []
    items = plan["items"]
    assert [it["glyph"]["id"] for it in items] == ["code"]
    assert 1.5 <= items[0]["settle"] and items[0]["to"] <= 4.0 - 0.3 + 1e-6
    assert out["draft"]["state"] == "ready"
    # Generate: made in house, over the studio's own clip, its render reused.
    row = w.rows[out["vid"]]
    assert row["status"] == "ready", (row.get("error"), row["arms"]["omni"].get("error"))
    opts = row["options"]
    assert opts["target"] == "intro" and opts["engine"] == "house" and opts["scene_kind"] == "intro"
    assert opts["source"]["src"] == "anim:352"
    arm = row["arms"]["omni"]
    assert arm["engine"] == "house" and arm["cost_usd"] == 0.0
    rec = arm["stages"]["render"]["c0"]
    assert rec["reused"] is True and rec["key"] == out["draft"]["key"]
    final = hbase.final_file(row)
    assert final.name == "intro.mp4" and final.is_file()
    assert media.frame_count(final) == media.frame_count(sc.ctx.target_seg.base)
    assert [v["id"] for v in out["versions"]] == [out["vid"]]
    assert th_reel.store().rows[RUN]["metadata"]["reel_choices"]["intro"]["pending"] == out["vid"]
    # The outro, uploaded with someone in it: cut as a scene is, followed by
    # the room.
    assert studio._PREPARED[(RUN, "outro")].src == "upload:77"
    assert out["outro_warm"]["has_person"] is True
    assert out["outro_preview"]["house"]["person"] is True
    assert [ly["follow"] for ly in out["outro_preview"]["house"]["layers"]] == ["room"]
    assert paid == []


def test_the_director_is_never_asked_on_a_bookend_whatever_is_sent(tmp_path, paid, monkeypatch):
    clip = _encode(tmp_path / "w" / "base" / "intro.mp4", (_card_frame(i) for i in range(24)))
    seg = pipeline.Segment(kind="intro", index=0, source=clip, is_still=False, base=clip)
    ctx = pipeline.Ctx(exp_id=0, run_id=RUN, aspect="9:16", width=W, height=H, work=tmp_path / "w",
                       options={}, run_ctx={"scene_dialogue": {0: "Something said in the scene."}, "topic": "t"},
                       intro=None, scenes=[], outro=None)
    ctx.target_seg = seg
    sc = studio.Scene(run_id=RUN, target="intro", aspect="9:16", width=W, height=H, work=tmp_path / "w", ctx=ctx,
                      chunks=[clip], seconds=[1.0], kind="intro")

    async def no_track(scene):
        return None, None

    monkeypatch.setattr(studio, "_icon_track", no_track)

    async def go():
        jobs = studio._icon_jobs(sc, take=0, ai=True)
        return await asyncio.gather(*(asyncio.shield(j.task) for j in jobs.values()))

    words, _spots, director = asyncio.run(go())
    assert studio._scene_lines(sc)[0] == "", "nothing is said on an intro"
    assert words["source"] == "estimate" and not [x for x in words["words"] if x.get("said")]
    assert director["source"] == "keywords" and director["ideas"] == []
    assert any(k.endswith("|director|0|0") for k in studio._ICON_TASKS), "asked with the AI off"
    assert paid == [], "neither Whisper nor the director"


# ---------------------------------------------------------------------------
# No person: the rule changes a bookend only
# ---------------------------------------------------------------------------

@needs_kit
def test_the_person_rule_changes_only_a_bookend(tmp_path):
    frozen = {"layout": {"placement": "left", "scale": 2.0, "reach": 0.5, "anchor": "left", "rotate": 0},
              "colour": "#7f2bf2", "ground": "#f0f0f0", "ink": "#ffffff"}
    recipe = pattern_el.materialise(rc.normalize(rc.Recipe(
        engine="house", target="intro", seed="s", elements=[rc.Element(
            id="pattern", kind="pattern", props={"slug": "structure", "layout": None, "colour": "auto"})])),
        {"pattern": frozen})
    job = pattern_el.PatternJob(exp_id=0, aspect="9:16", out_dir=tmp_path / "p", intro=None, intro_is_still=False,
                                seed="s", plan_frame=studio._no_frame, step=studio._quiet_step, person_mask=None)
    el = rc.pattern_elements(recipe)[0]
    res = asyncio.run(pattern_el.from_resolved(el, job))
    src = tmp_path / "clip.mp4"
    src.write_bytes(b"fixed bytes")

    def spec_key(person):
        spec = house.plate_spec(recipe, [(el, res.ref)], width=360, height=640, fps=30.0, n=120, person=person)
        return spec, house.render_key(spec, source=src, camera_key="c", bank_key="b")

    as_ever, key_ever = spec_key(None)
    with_one, key_one = spec_key(True)
    nobody, key_nobody = spec_key(False)
    assert key_one == key_ever and with_one.speaker_front is True and with_one.layers[0].follow == "room"
    assert nobody.speaker_front is False and nobody.layers[0].follow == "screen" and key_nobody != key_ever
    # The pattern's own follow is kept whoever is in it.
    own = el.model_copy(update={"props": {**el.props, "follow": "room"}})
    assert house.resolve_follow(own, np.ones((4, 4)), None, person=False) == "room"
    assert house.person_of("scene", None) is None

    class Bank:
        def __init__(self, yes):
            self.yes = yes

        def has_person(self):
            return self.yes

    # The reel's own card or card animation: no one, whatever MODNet reads;
    # an uploaded video: its matte says.
    for src in ("anim:352", "card:816", "asset:3", None):
        assert house.person_of("intro", Bank(True), src) is False
    assert house.person_of("outro", Bank(True), "upload:7") is True
    assert house.person_of("outro", Bank(False), "upload:7") is False


def test_a_preview_with_no_one_in_it_is_clean():
    assert studio._clean([], [0], no_person=True) is True
    assert studio._clean([], [0]) is False and studio._clean([], []) is True


def test_has_person_is_the_median_keyframe(tmp_path):
    bank = mattes.MatteBank(tmp_path / "c.mp4", None, key="k", n=10, width=16, height=16, fps=30.0, every=1,
                            cuts=[])
    empty = np.zeros((8, 8), np.uint8)
    blob = empty.copy()
    blob[2:6, 2:6] = 255                              # a quarter of the frame
    bank.raw = {0: empty, 1: empty, 2: blob}
    assert bank.has_person() is False, "a ghost in one keyframe of three"
    bank.raw = {0: blob, 1: empty, 2: blob}
    assert bank.has_person() is True


# ---------------------------------------------------------------------------
# The pipeline: made in house, never Omni
# ---------------------------------------------------------------------------

def test_a_run_made_in_house_that_reaches_omni_stops_before_any_call(tmp_path, paid):
    recipe = rc.normalize(rc.Recipe(engine="house", target="intro"))
    seg = pipeline.Segment(kind="intro", index=0, source=tmp_path / "a.mp4", is_still=False)
    ctx = pipeline.Ctx(exp_id=1, run_id=RUN, aspect="9:16", width=W, height=H, work=tmp_path,
                       options={"target": "intro", "engine": "house"}, run_ctx={}, intro=seg, scenes=[],
                       outro=None)
    ctx.recipe, ctx.harness = recipe, True
    with pytest.raises(RuntimeError, match="made in house reached Omni"):
        asyncio.run(pipeline._omni_edit(ctx, "omni", seg, "p", tmp_path, "Intro"))
    with pytest.raises(RuntimeError, match="made in house reached Omni"):
        asyncio.run(pipeline._omni_transition(ctx, "omni", tmp_path / "a.png", tmp_path / "b.png", "k", "In",
                                              tmp_path))
    assert paid == []


def test_a_bookend_recipe_is_made_in_house_from_its_own_clip_only(monkeypatch, world, tmp_path, paid):
    world(tmp_path / "i.mp4", tmp_path / "o.mp4")
    monkeypatch.setenv("REBRAND_OMNI", "on")          # even with Omni on
    omni_recipe = rc.normalize(rc.Recipe(engine="omni", target="intro"))
    with pytest.raises(HTTPException) as err:
        asyncio.run(pipeline.start(RUN, None, recipe=omni_recipe))
    assert err.value.status_code == 400 and err.value.detail == pipeline.BOOKEND_HOUSE_ONLY
    house_recipe = rc.normalize(rc.Recipe(engine="house", target="outro"))
    with pytest.raises(HTTPException) as err:
        asyncio.run(pipeline.start(RUN, None, recipe=house_recipe, base={"version_id": 3, "mode": "stack"}))
    assert err.value.status_code == 400 and err.value.detail == pipeline.BOOKEND_HOUSE_ONLY
    assert paid == []


# ---------------------------------------------------------------------------
# Run 815's real intro and outro (skipped without them, or without MODNet)
# ---------------------------------------------------------------------------

def _modnet() -> bool:
    try:
        speaker_front._get_session()
        return True
    except Exception:  # noqa: BLE001
        return False


@needs_kit
@pytest.mark.skipif(not (_found(352) and _found(353)), reason="run 815's intro and outro clips are not here")
def test_815s_intro_and_outro_are_rendered_in_house_119_frames_silent(tmp_path, world, paid):
    if not _modnet():
        pytest.skip("MODNet is not available here")
    w = world(_found(352), _found(353))
    got: dict = {}

    async def go():
        async with _client() as c:
            for target in ("intro", "outro"):
                prep = (await c.post("/api/rebrand/harness/prepare",
                                     json={"run_id": RUN, "target": target})).json()
                await _poll(lambda: c.get("/api/rebrand/harness/warmup", params={"run_id": RUN, "target": target}),
                            lambda r: r.json().get("camera") in ("ready", "failed"), every=0.5)
                warm = (await c.get("/api/rebrand/harness/warmup", params={"run_id": RUN, "target": target})).json()
                body = {"run_id": RUN, "target": target, "recipe": _draft(prep)}
                assert (await c.post("/api/rebrand/harness/preview", json=body)).status_code == 200
                plan = (await _poll(lambda: c.post("/api/rebrand/harness/icons/plan", json=body),
                                    lambda r: r.json().get("state") == "ready", every=0.5)).json()
                r = await c.post("/api/rebrand/harness/versions", json={**body, "icons_plan_key": plan["plan_key"]})
                assert r.status_code == 200, r.text
                vid = r.json()["id"]
                await _poll(lambda: c.get("/api/rebrand/harness/versions", params={"run_id": RUN, "target": target}),
                            lambda r: any(v["id"] == vid and v["status"] in ("ready", "failed")
                                          for v in r.json()["versions"]), every=0.5)
                got[target] = {"prep": prep, "warm": warm, "plan": plan, "row": w.rows[vid]}

    asyncio.run(go())
    for target, anim in (("intro", 352), ("outro", 353)):
        g = got[target]
        row = g["row"]
        assert row["status"] == "ready", (target, row.get("error"))
        assert row["options"]["source"]["src"] == f"anim:{anim}"
        base = studio._PREPARED[(RUN, target)].ctx.target_seg.base
        final = hbase.final_file(row)
        assert media.frame_count(base) in (119, 120) and media.frame_count(final) == media.frame_count(base)
        assert media.video_size(final) == (720, 1280)
        assert media.has_audio(final) and _audio_peak_db(final) <= -90.0, "the bookend's sound is silence"
        assert [it["glyph"]["id"] for it in g["plan"]["items"]] == ["code"], (target, g["plan"]["items"])
    # U6: MODNet reads the outro's glowing brain and blocks as ~4 % "person"
    # (median; up to 36 %) — the reel's own cards have no one: nothing cut.
    assert got["intro"]["warm"]["has_person"] is False and got["outro"]["warm"]["has_person"] is False
    assert paid == []


# ---------------------------------------------------------------------------
# The scene's end (S9) through the router and the run
# ---------------------------------------------------------------------------

@needs_kit
def test_an_end_is_previewed_rendered_and_generated_alike(tmp_path, world, fake_modnet, paid):
    """end_frame on every request: the draft render and Generate cut at the
    same frame (one render key: the run copies the studio's render), the
    version stores its end, and its length is its frames."""
    intro_src = _encode(tmp_path / "src" / "anim-352.mp4", (_card_frame(i) for i in range(96)), audio=True)
    w = world(intro_src, _encode(tmp_path / "src" / "anim-353.mp4", (_card_frame(i) for i in range(96))))

    async def go() -> dict:
        async with _client() as c:
            q = {"run_id": RUN, "target": "intro"}
            prep = (await c.post("/api/rebrand/harness/prepare", json={**q, "end_frame": 90})).json()
            body = {**q, "recipe": _draft(prep, added=False), "end_frame": 90}
            pv = (await c.post("/api/rebrand/harness/preview", json=body)).json()
            plan = (await _poll(lambda: c.post("/api/rebrand/harness/icons/plan", json=body),
                                lambda r: r.json().get("state") == "ready")).json()
            gen = {**body, "icons_plan_key": plan["plan_key"]}
            dr = (await c.post("/api/rebrand/harness/house/render", json=gen)).json()
            dr = (await _poll(lambda: c.get(f"/api/rebrand/harness/house/render/{dr['key']}", params=q),
                              lambda r: r.json().get("state") in ("ready", "failed"))).json()
            r = await c.post("/api/rebrand/harness/versions", json=gen)
            assert r.status_code == 200, r.text
            vid = r.json()["id"]
            await _poll(lambda: c.get("/api/rebrand/harness/versions", params=q),
                        lambda r: any(v["id"] == vid and v["status"] in ("ready", "failed")
                                      for v in r.json()["versions"]))
            return {"prep": prep, "pv": pv, "plan": plan, "draft": dr, "vid": vid}

    out = asyncio.run(go())
    scene = out["prep"]["scene"]
    assert scene["end"]["auto"] is None and scene["end"]["frames"] in (119, 120) and scene["end"]["extent"] == 120
    assert (scene["end"]["src_fps"], scene["end"]["src_frames"]) == (24.0, 96)
    assert scene["duration_sec"] == 3.0
    assert out["pv"]["house"]["n"] == 90
    assert all(float(it["to"]) <= 3.0 + 1e-6 for it in out["plan"]["items"])
    row = w.rows[out["vid"]]
    assert row["status"] == "ready", row.get("error")
    assert row["options"]["end_frame"] == 90
    rec = row["arms"]["omni"]["stages"]["render"]["c0"]
    assert rec["key"] == out["draft"]["key"] and rec["reused"] is True and rec["frames"] == 90
    assert row["arms"]["omni"]["duration_sec"] == 3.0
    assert media.frame_count(hbase.final_file(row)) == 90
    assert paid == []


# ---------------------------------------------------------------------------
# Keeping more than the automatic cut (S10) through the router and the run
# ---------------------------------------------------------------------------

@needs_kit
def test_a_version_made_past_the_automatic_cut_is_made_on_the_whole_clip(tmp_path, world, fake_modnet, paid):
    """A main scene ending on a flash: the full-length variant is prepared
    on its own ask, previewed, rendered and generated there with an end past
    the automatic cut — the run uses the same whole clip (one render key)."""
    intro_src = _encode(tmp_path / "src" / "anim-352.mp4", (_card_frame(i) for i in range(96)))
    w = world(intro_src, _encode(tmp_path / "src" / "anim-353.mp4", (_card_frame(i) for i in range(96))))
    flash = [_card_frame(i) for i in range(90)] + [np.full((H, W, 3), 230, np.uint8)] * 6
    main = _encode(Path(w.anims[351]["local_path"]), flash)
    assert media.tail_cut(main) == 90

    async def go() -> dict:
        async with _client() as c:
            q = {"run_id": RUN, "target": "scene_0"}
            trimmed = (await c.post("/api/rebrand/harness/prepare", json=q)).json()
            full = (await c.post("/api/rebrand/harness/prepare", json={**q, "variant": "full"})).json()
            draft = copy.deepcopy(trimmed["default_recipe"])
            body = {**q, "recipe": draft, "variant": "full", "end_frame": 118}
            pv = (await c.post("/api/rebrand/harness/preview", json=body)).json()
            dr = (await c.post("/api/rebrand/harness/house/render", json=body)).json()
            dr = (await _poll(lambda: c.get(f"/api/rebrand/harness/house/render/{dr['key']}",
                                            params={**q, "variant": "full"}),
                              lambda r: r.json().get("state") in ("ready", "failed"))).json()
            r = await c.post("/api/rebrand/harness/versions", json=body)
            assert r.status_code == 200, r.text
            vid = r.json()["id"]
            await _poll(lambda: c.get("/api/rebrand/harness/versions", params=q),
                        lambda r: any(v["id"] == vid and v["status"] in ("ready", "failed")
                                      for v in r.json()["versions"]))
            return {"trimmed": trimmed, "full": full, "pv": pv, "draft": dr, "vid": vid}

    out = asyncio.run(go())
    t, f = out["trimmed"]["scene"], out["full"]["scene"]
    assert t["variant"] is None and f["variant"] == "full"
    assert t["end"]["auto"] == 112 and t["end"]["extent"] == 120 and t["end"]["frames"] < 118
    assert f["end"]["frames"] >= 119 and f["trimmed_sec"] == 0.0
    assert out["pv"]["house"]["n"] == 118 and out["draft"]["state"] == "ready"
    row = w.rows[out["vid"]]
    assert row["status"] == "ready", row.get("error")
    assert row["options"]["variant"] == "full" and row["options"]["end_frame"] == 118
    rec = row["arms"]["omni"]["stages"]["render"]["c0"]
    assert rec["key"] == out["draft"]["key"] and rec["reused"] is True and rec["frames"] == 118
    assert media.frame_count(hbase.final_file(row)) == 118
    assert paid == []
