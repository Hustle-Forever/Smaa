"""Keep more than the automatic cut (S10).

  an original   needs no preparing: an end past the automatic cut ships
                the raw clip to that frame (th_reel's end rule)
  the variant   the full-length scene (variant "full") is the clip whole
                past its tail flash — normalize() with no end, described
                whole, under the folder the scene had before the trim —
                prepared on its own key (run, target, "full") beside the
                trimmed one; a run of a version made on it uses that clip
  the director  its context is the trimmed scene's (director_from): the
                same words (carried over), the same first frame and spots,
                the same take and answer — the very key the trimmed scene
                is answered under, whatever this clip's bytes; nothing is
                asked again
  run 815       anim-351 kept whole is 239 frames (asserted FIRST), its
                director key is the trimmed scene's, its icon plan takes the
                paid answer from its cache (the paid call refuses), and the
                folder 815 had before the trim is used as it is

Every paid client, Whisper and MODNet refuse (the keys test's guard);
815's files are read only, copied into a temp folder, and the tests that
need them are skipped when they are not on this machine."""
from __future__ import annotations

import asyncio
import json
import shutil
from pathlib import Path

import numpy as np
import pytest

from app.services import rebrand_media as media
from app.services import rebrand_pipeline as pipeline
from app.services import th_reel
from app.services.rebrand_harness import director as D
from app.services.rebrand_harness import stages as st
from app.services.rebrand_harness import studio
from app.services.rebrand_harness import words as W

import test_rebrand_keys_frozen as K
import test_rebrand_scene_end as E
from test_rebrand_shot_cuts import _fake_reel, _flash_clip

cv2 = pytest.importorskip("cv2")
pytestmark = pytest.mark.omni_default
refusing = E.refusing                      # the same guard, for this module's tests
OLD_815 = studio.STUDIO_ROOT / "815" / "scene_0_84f81b9967"
HAVE_OLD = (OLD_815 / "base" / "scene0.mp4").is_file() and (OLD_815 / "icons" / "director.json").is_file()


# ---------------------------------------------------------------------------
# An original needs no preparing
# ---------------------------------------------------------------------------

def test_an_original_past_the_cut_ships_the_raw_clip_to_that_frame(tmp_path):
    src = _flash_clip(tmp_path)                           # 30 frames of its shot + a 4-frame flash, 24 fps
    facts = th_reel.clip_facts(src)
    assert (facts.frames, facts.cut, facts.full, facts.auto_end) == (34, 30, 42, 37)

    class Io:
        def facts(self, p):
            return th_reel.clip_facts(p)

    t = th_reel.Target("scene_0", "main", "Main", src="anim:1", path=src, seconds=2, anim_id=1)
    auto = asyncio.run(th_reel._segment(t, Io(), th_reel.Choice("scene_0")))
    kept = asyncio.run(th_reel._segment(t, Io(), th_reel.Choice("scene_0", user_end=41)))
    assert (auto.path, auto.frames) == (src, 37), "cut at the flash by itself"
    assert (kept.path, kept.frames, kept.user_end) == (src, 41, 41), "the raw clip, to the user's end"


# ---------------------------------------------------------------------------
# The full-length variant (a synthetic flash)
# ---------------------------------------------------------------------------

def test_the_full_length_variant_is_the_whole_clip_on_its_own_key(tmp_path, monkeypatch):
    src = _flash_clip(tmp_path)
    _fake_reel(monkeypatch, src)
    monkeypatch.setattr(studio, "_warm", lambda scene: None)
    trimmed = asyncio.run(studio.prepare(1, "scene_0"))
    full = asyncio.run(studio.prepare(1, "scene_0", "full"))
    key = st.digest(src)[:10]
    assert trimmed.work.name == f"scene_0_{key}_t30" and full.work.name == f"scene_0_{key}"
    assert full.work == studio.scene_base(1, "scene_0", src, full=True)
    assert studio.prepared(1, "scene_0") is trimmed and studio.prepared(1, "scene_0", "full") is full
    assert asyncio.run(studio._scene(1, "scene_0", "full")) is full
    clip = studio._scene_clip(full)
    n_full, n_trim = media.frame_count(clip), media.frame_count(studio._scene_clip(trimmed))
    assert n_full > n_trim and abs(n_full - 42) <= 1, (n_full, n_trim)
    info = media.scene_info(clip)
    assert info["source_cut"] is None and info["trimmed_sec"] == 0.0 and info["cuts"], info
    assert full.variant == "full" and full.director_from == trimmed.work and trimmed.director_from is None
    body = studio.public_scene(full)
    assert body["variant"] == "full" and body["end"]["frames"] == n_full and body["end"]["auto"] == 37
    assert studio.public_scene(trimmed)["variant"] is None
    # An intro or outro has no variant: always whole.
    assert studio._pkey(1, "intro", "full") == (1, "intro")
    # The run of a version made on it copies that clip.
    ctx = pipeline.Ctx(exp_id=5, run_id=1, aspect="9:16", width=720, height=1280, work=tmp_path / "run",
                       options={"target": "scene_0", "variant": "full"}, run_ctx={}, intro=None, scenes=[],
                       outro=None)
    seg = pipeline.Segment(kind="scene", index=0, source=src, is_still=False)
    out = asyncio.run(pipeline._scene_base(ctx, seg, tmp_path / "run" / "base" / "scene0.mp4"))
    assert out.read_bytes() == clip.read_bytes()


def test_the_prepare_route_takes_the_variant(tmp_path, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from pydantic import ValidationError

    from app.routers import rebrand_harness as rt

    with pytest.raises(ValidationError):
        rt.SceneRequest(run_id=1, target="scene_0", variant="half")
    src = _flash_clip(tmp_path)
    _fake_reel(monkeypatch, src)
    monkeypatch.setattr(studio, "_warm", lambda scene: None)

    async def none(*_a, **_k):
        return []

    monkeypatch.setattr(rt.store, "list_for_run", none)
    app = FastAPI()
    app.include_router(rt.router)
    c = TestClient(app)
    got = c.post("/api/rebrand/harness/prepare", json={"run_id": 1, "target": "scene_0", "variant": "full"}).json()
    assert got["scene"]["variant"] == "full" and got["scene"]["trimmed_sec"] == 0.0
    w = c.get("/api/rebrand/harness/warmup", params={"run_id": 1, "target": "scene_0", "variant": "full"}).json()
    assert w["prepared"] is True
    plain = c.post("/api/rebrand/harness/prepare", json={"run_id": 1, "target": "scene_0"}).json()
    assert plain["scene"]["variant"] is None and plain["scene"]["trimmed_sec"] > 0


# ---------------------------------------------------------------------------
# Run 815's real anim-351 (read only; skipped without it)
# ---------------------------------------------------------------------------

def _world_815(monkeypatch) -> studio.Scene:
    """815's trimmed scene (its _t188 folder, copied) prepared, and the reel
    as prepare reads it (no database)."""
    trimmed = K._scene_815()
    trimmed.frames = 234
    _fake_reel(monkeypatch, K.ANIM_351, K.LINE_815)
    monkeypatch.setattr(studio, "_warm", lambda scene: None)

    async def run_row(run_id):
        return {"id": run_id, "topic": K.TOPIC_815,
                "metadata": {"scene_count": 1, "aspect_ratio": "9:16", "script": {"dialogue": K.LINE_815}}}

    monkeypatch.setattr(studio.th, "_fetch_run_row", run_row)
    return trimmed


def _trimmed_key(trimmed: studio.Scene) -> str:
    words = json.loads((trimmed.work / "icons" / "words.json").read_text(encoding="utf-8"))
    track = K.T.load(trimmed.work / "icons")
    return D.director_key(D.DirectorCtx(words=words, script=K.LINE_815, work_dir=trimmed.work / "icons",
                                        topic=K.TOPIC_815, others=[], frame0=track.frame0,
                                        spots=list(track.spots), take=0), D.default_catalog())


@pytest.mark.skipif(not K.HAVE_815, reason=K.NO_815)
def test_815_kept_whole_is_239_frames_and_its_director_is_the_trimmed_scenes(monkeypatch, capsys):
    trimmed = _world_815(monkeypatch)
    full = asyncio.run(studio.prepare(815, "scene_0", "full"))
    clip = studio._scene_clip(full)
    # FIRST: the variant really is the whole clip (never two trimmed ones).
    assert media.frame_count(clip) == 239 and full.frames == 239
    assert full.work.name == "scene_0_84f81b9967" and full.director_from == trimmed.work
    assert full.end_info == {"src_fps": 24.0, "src_frames": 192, "auto": 235, "frames": 239, "extent": 240}
    # Its words: the trimmed scene's, carried over (no Whisper).
    now = json.loads((studio._icon_dir(full) / "words.json").read_text(encoding="utf-8"))
    was = json.loads((trimmed.work / "icons" / "words.json").read_text(encoding="utf-8"))
    assert now["digest"] == was["digest"] and now["key"] == W.cache_key(clip, K.LINE_815)
    # Its director: the trimmed scene's key, answered from its cache.
    ctx = asyncio.run(studio.director_ctx_from(full, take=studio._take(full), ai=True))
    assert D.director_key(ctx, D.default_catalog()) == _trimmed_key(trimmed)
    assert json.loads((trimmed.work / "icons" / "director.json").read_text(encoding="utf-8"))["key"] == \
        _trimmed_key(trimmed)

    async def no_track(scene):
        return None, None                  # the full clip's own spots would need MODNet (refused here)

    monkeypatch.setattr(studio, "_icon_track", no_track)

    async def plan() -> dict:
        draft = K.starting_draft(815, "scene_0")
        for _ in range(900):
            body = await studio.icon_plan(815, "scene_0", draft, variant="full", end_frame=238)
            if body.get("state") == "ready":
                return body
            await asyncio.sleep(0.1)
        raise AssertionError(body.get("steps"))

    body = asyncio.run(plan())
    assert body["source"]["director"] == "ai", "the paid answer, from its cache: nothing asked"
    # Frame 0 of the two clips (reported: it decides nothing any more).
    cap = cv2.VideoCapture(str(clip))
    ok, f0 = cap.read()
    cap.release()
    t0 = cv2.imread(str(trimmed.firsts[0]), cv2.IMREAD_COLOR)
    same = ok and t0 is not None and f0.shape == t0.shape and bool(np.array_equal(f0, t0))
    print(f"[S10] frame 0 of the full clip == the trimmed scene's c0_first: {same}")


@pytest.mark.skipif(not (K.HAVE_815 and HAVE_OLD), reason="run 815's folder from before the trim is not here")
def test_815s_folder_from_before_the_trim_is_used_as_it_is(monkeypatch):
    trimmed = _world_815(monkeypatch)
    old = studio.STUDIO_ROOT / "815" / OLD_815.name
    shutil.copytree(OLD_815, old, ignore=shutil.ignore_patterns("moments", "plans", "*.jpg", "masks_*"))
    had = {p.name: p.read_bytes() for p in (old / "icons").glob("*.json")}
    made: list = []
    real = media.normalize

    def watched(*a, **k):
        made.append(a)
        return real(*a, **k)

    monkeypatch.setattr(media, "normalize", watched)
    full = asyncio.run(studio.prepare(815, "scene_0", "full"))
    assert full.work == old and made == [], "its own clip, not made again"
    assert media.frame_count(studio._scene_clip(full)) == 239
    assert {p.name: p.read_bytes() for p in (old / "icons").glob("*.json")} == had, "its own files are kept"
    ctx = asyncio.run(studio.director_ctx_from(full, take=studio._take(full), ai=True))
    assert D.director_key(ctx, D.default_catalog()) == _trimmed_key(trimmed)
    own = json.loads((old / "icons" / "director.json").read_text(encoding="utf-8"))
    assert own["key"] == _trimmed_key(trimmed), "815's first paid answer answers the same key"
