"""The scene's end on the timeline (S9): cut more.

  no end        every key the S0 snapshot pins is made exactly as before
                (the spec memo, the preview key, the plan's duration, the
                render key, house.render's command)
  an end        end_frame before the clip's last frame sets the out-point n:
                the spec, its memo, the preview key, the render key and the
                icon plan's duration (min(its seconds, n/30)) move — the
                director's key never does (no paid re-ask for a trim)
  the render    house.render adds "-t n/fps" only when n is under the clip's
                frames; 815's scene cut at 200 frames renders 200 frames
                with its sound no longer than 200/30 s + one AAC frame
  remake        a version made for an end is made again to its own end
  the grip      public_scene says where the clip ends (its source rate and
                frames, the automatic cut, the prepared frames, how far it
                really runs) and its length at the request's end
  U2            an icon timed by hand past the new end is never dropped
                silently: it shows (held inside the scene, with a note), or
                is listed with why it has no room

Every paid client, Whisper and MODNet refuse (the keys test's own guard);
815's real scene is read only and skipped when it is not on this machine."""
from __future__ import annotations

import asyncio
import copy
import dataclasses
import io
import json
import subprocess
import threading
import types
from pathlib import Path, PurePosixPath

import numpy as np
import pytest

from app import config as app_config
from app.services import omni_client as omni
from app.services import pattern_kit
from app.services import person_matte
from app.services import rebrand_media as media
from app.services import rebrand_pipeline as pipeline
from app.services import rebrand_store as store
from app.services import speaker_front
from app.services import vertex_client
from app.services.rebrand_harness import director as D
from app.services.rebrand_harness import house
from app.services.rebrand_harness import icon_catalog as ic
from app.services.rebrand_harness import plate as pl
from app.services.rebrand_harness import recipe as rc
from app.services.rebrand_harness import speaker_track as T
from app.services.rebrand_harness import studio
from app.services.rebrand_harness import words as W
from app.services.rebrand_harness.elements import icon as I

import test_rebrand_keys_frozen as K

cv2 = pytest.importorskip("cv2")
pytestmark = pytest.mark.omni_default
AAC_FRAME = 1024 / media.AUDIO_RATE


@pytest.fixture(autouse=True)
def refusing(monkeypatch, tmp_path):
    """The keys test's guard: every paid client, Whisper and MODNet refuse;
    the director's model pinned; the bundled pattern masks; every studio
    folder and memo the test's own."""
    asked: list[str] = []

    def refuse(name, is_async=True):
        async def _a(*_x, **_k):
            asked.append(name)
            raise AssertionError(f"{name} in a scene-end test")

        def _s(*_x, **_k):
            asked.append(name)
            raise AssertionError(f"{name} in a scene-end test")
        return _a if is_async else _s

    for mod, name in ((vertex_client, "gemini_text"), (vertex_client, "gemini_multimodal"),
                      (vertex_client, "gemini_image"), (vertex_client, "gemini_embed"),
                      (omni, "edit_video"), (omni, "interpolate"), (person_matte, "person_mask")):
        if hasattr(mod, name):
            monkeypatch.setattr(mod, name, refuse(f"{mod.__name__}.{name}"))
    monkeypatch.setattr(W, "transcribe", refuse("whisper", is_async=False))
    monkeypatch.setattr(speaker_front, "raw_matte", refuse("modnet raw_matte", is_async=False))
    monkeypatch.setattr(speaker_front, "person_matte", refuse("modnet person_matte", is_async=False))
    monkeypatch.setattr(D, "_direct", refuse("the director's paid call"))
    monkeypatch.setattr(app_config, "config", dataclasses.replace(app_config.config,
                                                                  vertex_gemini_fast_model=K.MODEL))
    monkeypatch.setattr(pattern_kit, "KIT_DIR", tmp_path / "no_kit")
    monkeypatch.setitem(ic._TEAM, "rows", [])
    monkeypatch.setattr(studio, "STUDIO_ROOT", tmp_path / "rebrand" / "_studio")
    monkeypatch.setattr(studio, "ANALYSIS_ROOT", tmp_path / "rebrand" / "_cache" / "scene")
    monkeypatch.setattr(pipeline, "REBRAND_ROOT", tmp_path / "rebrand")
    for name in ("_PREPARED", "_ANALYSES", "_ICON_TASKS", "_HOUSE_SPECS", "_PREVIEWS", "_MASK_TASKS", "_DRAFTS",
                 "_CLAIMED", "_REMAKES"):
        monkeypatch.setattr(studio, name, {})
    yield asked
    assert not asked, f"refused calls were made: {asked}"


# ---------------------------------------------------------------------------
# No end: nothing moves
# ---------------------------------------------------------------------------

def test_no_end_makes_every_key_as_before(tmp_path):
    work = PurePosixPath("/studio/1/scene_0_0123456789_t188")
    scene = dataclasses.replace(K.fixture_scene(work), frames=234)
    for name, make in K.RECIPES.items():
        draft = make(1, "scene_0")
        seed = draft.seed or studio.default_seed(1, "scene_0")
        stored = K.frozen_patterns(draft)
        assert studio._spec_memo(scene, stored, None, None) == studio._spec_memo(scene, stored, None), name
        assert studio._preview_key(draft, seed, n=None) == studio._preview_key(draft, seed), name
    # An end at or past the clip's own last frame is no cut at all.
    assert studio._cut_n(scene, None) is None
    assert studio._cut_n(scene, 234) is None and studio._cut_n(scene, 900) is None
    assert studio._cut_n(scene, 233) == 233
    s = K._Seconds([7.8])
    assert studio._plan_duration(s, None) == 7.8 and studio._plan_duration(s, 200) == pytest.approx(6.667)
    assert studio._chunks(s) == [{"start": 0.0, "seconds": 7.8}]
    assert studio._chunks(s, 200) == [{"start": 0.0, "seconds": pytest.approx(6.667)}]


def test_an_end_moves_the_memo_and_the_preview_key_only_when_set():
    work = PurePosixPath("/studio/1/scene_0_0123456789_t188")
    scene = dataclasses.replace(K.fixture_scene(work), frames=234)
    draft = K.hand_and_span(1, "scene_0")
    stored = K.frozen_patterns(draft)
    assert studio._spec_memo(scene, stored, None, 200) != studio._spec_memo(scene, stored, None)
    assert studio._preview_key(draft, draft.seed, n=200) != studio._preview_key(draft, draft.seed)
    assert studio._preview_key(draft, draft.seed, n=200) != studio._preview_key(draft, draft.seed, n=199)


# ---------------------------------------------------------------------------
# house.render: "-t" only when cut short
# ---------------------------------------------------------------------------

def test_house_render_cuts_the_sound_only_when_the_scene_is_cut_short(tmp_path, monkeypatch):
    monkeypatch.setattr(house, "subprocess", types.SimpleNamespace(Popen=K._Popen, PIPE=subprocess.PIPE))
    K._Popen.seen = []
    spec = pl.PlateSpec(width=16, height=32, fps=30.0, n=3)
    src = tmp_path / "scene0.mp4"
    out = tmp_path / "out" / "draft.mp4"

    class Bank:
        def __init__(self, n):
            self.n = n

        def at(self, i, bars=None):
            return np.zeros((32, 16), np.float32)

    house.render(spec, src, out)
    plain = K._Popen.seen[-1]["cmd"]
    house.render(spec, src, out, bank=Bank(3))
    assert K._Popen.seen[-1]["cmd"] == plain, "the whole clip: the very command it always ran"
    house.render(spec, src, out, bank=Bank(5))
    cut = K._Popen.seen[-1]["cmd"]
    at = plain.index("-frames:v") + 2
    assert cut == plain[:at] + ["-t", "0.100000"] + plain[at:]


# ---------------------------------------------------------------------------
# U2: an icon timed by hand past the end
# ---------------------------------------------------------------------------

def _plan_with(recipe: rc.Recipe, seconds: float) -> I.IconPlan:
    return K.fixture_plan(recipe, seconds=seconds)


def test_an_icon_timed_by_hand_past_the_end_is_never_dropped_silently():
    recipe = rc.normalize({
        "engine": "house", "target": "scene_0", "seed": "studio:1:scene_0",
        "elements": [{"id": "icons", "kind": "icon", "placement": {"anchor": "auto"},
                      "animation": {"entrance": "pop"},
                      "props": {"style": "circle",
                                "added": [{"glyph": {"source": "library", "id": "puzzle-piece"}, "label": "Projects"}],
                                "tweaks": {"puzzle-piece": {"off_word": True, "sec": 7.2, "hold_sec": 0.5}}}}]})
    whole = _plan_with(recipe, 7.8)
    cut = _plan_with(recipe, round(150 / 30, 3))            # the end dragged to 5.0 s
    assert whole.timing_key != cut.timing_key
    shown = {it["glyph"]["id"]: it for it in cut.public()["items"]}
    ideas = {i["glyph"]["id"] if isinstance(i.get("glyph"), dict) else i.get("glyph"): i
             for i in cut.public()["ideas"]}
    # The user's own icon always shows (elements/icon: TRACKS): never left
    # out, whatever its spot or its zone.
    assert "puzzle-piece" in shown, ("the hand-timed icon vanished", ideas.get("puzzle-piece"))
    it = shown["puzzle-piece"]
    assert it["to"] <= 5.0 + 1e-6, "held inside the scene"
    assert any("end" in n for n in it.get("notes") or []), it
    # Every icon the AI placed on a word said after the end has its reason.
    for i in cut.public()["ideas"]:
        if i.get("state") not in (None, "on"):
            assert i.get("reason"), i
            if i.get("added"):
                assert i["state"] in ("limit", "gone", "available"), ("one of the user's left out", i)


# ---------------------------------------------------------------------------
# Run 815's real scene (read only; skipped without it)
# ---------------------------------------------------------------------------

@pytest.mark.skipif(not K.HAVE_815, reason=K.NO_815)
def test_815_cut_at_200_frames(tmp_path, monkeypatch):
    scene = K._scene_815()
    clip = studio._scene_clip(scene)
    if not K._analysis_815(clip):
        pytest.skip("run 815's scene analysis (matte bank, camera) is not on this machine")
    scene.frames = 234
    scene.end_info = studio._end_info(K.ANIM_351, 234, main=True, still=False)
    pub = studio.public_scene(scene, end_frame=200)
    assert pub["end"] == {"src_fps": 24.0, "src_frames": 192, "auto": 235, "frames": 234, "extent": 240}
    assert pub["duration_sec"] == pytest.approx(6.67) and studio.public_scene(scene)["duration_sec"] == 7.8

    words = json.loads((scene.work / "icons" / "words.json").read_text(encoding="utf-8"))
    track = T.load(scene.work / "icons")
    dkey = D.director_key(D.DirectorCtx(words=words, script=K.LINE_815, work_dir=scene.work / "icons",
                                        topic=K.TOPIC_815, others=[], frame0=track.frame0,
                                        spots=list(track.spots), take=0), D.default_catalog())

    async def run() -> dict:
        got: dict = {}
        draft = K.starting_draft(815, "scene_0")
        for end in (None, 200):
            kw = {"end_frame": end} if end is not None else {}
            for _ in range(600):
                body = await studio.icon_plan(815, "scene_0", draft, **kw)
                if body.get("state") == "ready":
                    break
                await asyncio.sleep(0.1)
            assert body["source"]["director"] == "ai", "the paid answer, from its cache"
            kept = studio.kept_plan(815, "scene_0", body["plan_key"])
            el = rc.icon_element(draft)
            stored = K.frozen_patterns(rc.normalize(draft.model_copy(update={"elements": [
                e.model_copy(update={"props": {**e.props, "resolved": kept.get("resolved") or []}})
                if e.id == el.id else e for e in draft.elements]})))
            hs = await studio.house_spec(815, "scene_0", stored, **kw)
            got[end] = {"plan": body, "stored": stored, "hs": hs,
                        "memo": studio._spec_memo(scene, stored, None, studio._cut_n(scene, end))}
        return got

    got = asyncio.run(run())
    whole, cut = got[None], got[200]
    assert whole["hs"].spec.n == 234 and cut["hs"].spec.n == 200
    assert cut["hs"].key != whole["hs"].key and cut["memo"] != whole["memo"]
    assert cut["plan"]["timing_key"] != whole["plan"]["timing_key"]
    pins = json.loads(K.PINS.read_text(encoding="utf-8"))["run_815"]
    assert whole["plan"]["timing_key"] == pins["starting_draft.timing_key"], "no end: the pinned plan"
    assert whole["hs"].key == pins["starting_draft.render_key"], "no end: the pinned render key"
    assert json.loads((scene.work / "icons" / "director.json").read_text(encoding="utf-8"))["key"] == dkey, \
        "the director's key did not move"
    assert all(float(it["to"]) <= 200 / 30 + 1e-6 for it in cut["plan"]["items"])

    # The render cut at 200 frames: 200 frames, its sound stops with it.
    hs = cut["hs"]
    out = tmp_path / "cut200.mp4"
    house.render(hs.spec, hs.clip, out, camera=hs.track, bank=hs.bank)
    assert media.frame_count(out) == 200
    assert K_audio_sec(out) <= 200 / 30 + AAC_FRAME + 1e-3

    # A version made for that end is made again to it (and its stored key).
    exp = {"id": 77, "run_id": 815, "status": "ready",
           "options": {"target": "scene_0", "engine": "house", "end_frame": 200,
                       "recipe": cut["stored"].model_dump(mode="json")},
           "arms": {"omni": {"status": "ready", "engine": "house", "final_path": str(tmp_path / "v77.mp4"),
                             "stages": {"render": {"c0": {"key": hs.key}}}}}}
    patched: list = []

    async def get(exp_id):
        return copy.deepcopy(exp)

    async def patch_arm(exp_id, arm, patch):
        patched.append(patch)

    monkeypatch.setattr(store, "get_experiment", get)
    monkeypatch.setattr(store, "patch_arm", patch_arm)
    monkeypatch.setattr(studio.th, "_abs_from_rel", lambda rel: Path(rel))

    async def remake():
        await studio.remake(77)
        await studio._REMAKES[77].task
        return studio._REMAKES[77]

    r = asyncio.run(remake())
    assert r.state == "ready", r.error
    assert r.same is True and patched[-1]["remade"]["key"] == hs.key
    assert media.frame_count(tmp_path / "v77.mp4") == 200


def K_audio_sec(path: Path) -> float:
    pcm = subprocess.run([media.ffmpeg_exe(), "-v", "error", "-i", str(path), "-map", "0:a:0", "-ac", "1",
                          "-ar", str(media.AUDIO_RATE), "-f", "s16le", "-"], capture_output=True, check=True).stdout
    return len(pcm) / 2 / media.AUDIO_RATE
