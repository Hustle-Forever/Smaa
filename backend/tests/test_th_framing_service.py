"""TH v2 framing (#56): the service, the Veo geometry and the routes.

- metadata.framing is the run's framing: a new run starts above the hip (the
  owner's default), a run made before framing existed reads as close (its
  stills were), and metadata.scenes[i].framing overrides it for one scene.
- Every still is made at its scene's framing and records the framing it was
  made at (main_framing_used for scene 0; scenes[i].framing_used and the
  scene row for the others); a photo's own framing is "as_photo".
- A framing change on a still that exists re-shoots THAT still through the
  refine engine with a canned instruction; a scene with no still is
  composed; a still that is the character photo keeps the photo's framing;
  before the run has stills the framing is only saved.
- Veo keeps the still's framing: close pushes in 1.06 toward the face, above
  the hip 1.12, full body (and a photo) is locked off; the likeness retry
  never crops a full-body still.

No database and no model calls: the in-memory FakePg and fakes of
test_th_v2_scene_stills (whose `env` fails any real model call).
"""
from __future__ import annotations

import asyncio
import copy
from pathlib import Path

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from PIL import Image

from app.services import yas_talking_head_v2 as th
from app.services import yas_talking_head_v2_prompts as thp
from test_th_v2_scene_stills import (  # noqa: F401
    _md, _png_bytes, _scene, _seed_old_main, _seed_scene_row, env,
)

HEADING = {
    "close": "SHOT SIZE: CLOSE-UP",
    "above_hip": "SHOT SIZE: MEDIUM SHOT",
    "full_body": "SHOT SIZE: FULL-BODY SHOT",
}
# fetch_run as written, before any fixture fakes it.
REAL_FETCH_RUN = th.fetch_run


def _heading(prompt: str) -> str | None:
    found = [f for f, h in HEADING.items() if h in prompt]
    assert len(found) <= 1, found
    return found[0] if found else None


def _texts(call: dict) -> str:
    return "\n".join(str(v) for k, v in call["parts"] if k == "text")


@pytest.fixture
def fenv(env, monkeypatch):
    """env plus: a fresh image-gen semaphore, and edit_run's background regen
    captured instead of started (env.dispatched)."""
    monkeypatch.setattr(th, "_IMAGE_GEN_SEM", None)
    dispatched: list[list[dict]] = []
    monkeypatch.setattr(th, "_dispatch_keyframe_regen", lambda run_id, specs: dispatched.append(specs))
    env.dispatched = dispatched
    return env


# ---------------------------------------------------------------------------
# The run's framing and a scene's
# ---------------------------------------------------------------------------

def test_a_legacy_run_reads_close_and_a_scene_override_wins():
    legacy = _md(scenes=3)
    assert "framing" not in legacy
    assert th._run_framing(legacy) == "close"
    assert [th._effective_framing(legacy, i) for i in range(3)] == ["close"] * 3
    md = _md(scenes=3, framing="above_hip")
    md["scenes"][1]["framing"] = "full_body"
    md["scenes"][2]["framing"] = "garbage"  # not a framing: follows the run
    assert [th._effective_framing(md, i) for i in range(3)] == ["above_hip", "full_body", "above_hip"]
    assert th._scene_framing_override(md, 1) == "full_body"
    assert th._scene_framing_override(md, 0) is None
    single = _md(framing="full_body")
    assert th._effective_framing(single, 0) == "full_body"
    # A run cut down to one scene keeps its scenes list: its one framing is
    # still the run's.
    cut = _md(scenes=2, framing="above_hip")
    cut["scenes"][0]["framing"] = "full_body"
    cut["scene_count"] = 1
    assert th._effective_framing(cut, 0) == "above_hip"
    assert th._scene_framing_override(cut, 0) is None


def test_check_framing_refuses_garbage():
    assert th._check_framing(None) is None
    assert th._check_framing("Full body") == "full_body"
    with pytest.raises(HTTPException) as e:
        th._check_framing("wide")
    assert e.value.status_code == 400


def test_still_framing_is_what_the_still_was_made_at():
    md = _md(scenes=3, framing="full_body")
    assert th._still_framing(md, 0) == "close", "made before framing existed"
    md["main_framing_used"] = "above_hip"
    assert th._still_framing(md, 0) == "above_hip"
    md["scenes"][1]["framing_used"] = "full_body"
    assert th._still_framing(md, 1) == "full_body"
    md["scenes"][2].update(as_is=True, framing_used="full_body")
    assert th._still_framing(md, 2) == "as_photo", "the character photo keeps its own framing"
    md["main_as_is"] = True
    assert th._still_framing(md, 0) == "as_photo"
    assert th._still_framing({"scenes": [{}, {"framing_used": "as_photo"}]}, 1) == "as_photo"


# ---------------------------------------------------------------------------
# generate_script: a new run starts above the hip
# ---------------------------------------------------------------------------

@pytest.fixture
def script_env(env, monkeypatch):
    async def script_llm(user_prompt):
        return {"subject": "s", "action": "a", "camera": "Locked-off", "scenes": [],
                "dialogue": "Hello there.", "hook": "Hello"}

    async def inputs(topic, **_):
        return "", None, []

    async def finalize(script, **_):
        return script, {"subject": "s", "gender": "man", "source": "default_emirati"}

    async def nothing(*_a, **_k):
        return None

    monkeypatch.setattr(th, "_script_llm", script_llm)
    monkeypatch.setattr(th, "_build_script_inputs", inputs)
    monkeypatch.setattr(th, "_finalize_script", finalize)
    monkeypatch.setattr(th, "_embed_and_store_script", nothing)
    return env


def _new_run(env, **kw) -> dict:
    asyncio.run(th.generate_script(topic="Peer learning", refs=th.TalkingHeadRefs(),
                                   lock_character=False, image_models={}, **kw))
    rid = max(env.db.rows)
    return env.db.rows[rid]["metadata"]


def test_a_new_run_starts_above_the_hip(script_env):
    assert _new_run(script_env)["framing"] == "above_hip"
    assert _new_run(script_env, framing="full_body")["framing"] == "full_body"
    assert _new_run(script_env, framing="close")["framing"] == "close"
    with pytest.raises(HTTPException):
        _new_run(script_env, framing="wide")


# ---------------------------------------------------------------------------
# generate_previews: each still at its scene's framing, recorded
# ---------------------------------------------------------------------------

def test_generate_previews_makes_each_scene_at_its_framing(env):
    md = _md(scenes=2, lock=False, framing="close")
    md["scenes"][1]["framing"] = "full_body"
    env.db.add_run(100, md)
    asyncio.run(th.generate_previews(100, look={"refs": {}}))
    got = sorted(_heading(c["prompt"]) for c in env.gen["calls"])
    assert got == ["close", "full_body"], "two different shot sizes"
    out = env.db.md(100)
    assert out["main_framing_used"] == "close"
    sc1 = out["scenes"][1]
    assert sc1["framing_used"] == "full_body" and sc1["framing"] == "full_body"
    assert env.db.rows[sc1["keyframe_id"]]["metadata"]["framing_used"] == "full_body"


def test_the_look_sets_the_framing_before_the_stills(env):
    env.db.add_run(101, _md(lock=False))  # a legacy run: close until the Look says
    asyncio.run(th.generate_previews(101, look={"refs": {}, "framing": "full_body"}))
    call, = env.gen["calls"]
    assert _heading(call["prompt"]) == "full_body"
    assert env.db.md(101)["framing"] == "full_body"
    assert env.db.md(101)["main_framing_used"] == "full_body"


def test_the_look_sets_a_scene_framing(env):
    env.db.add_run(102, _md(scenes=2, lock=False, framing="above_hip"))
    asyncio.run(th.generate_previews(102, look={"refs": {}, "scene_framings": [None, "close"]}))
    assert env.db.md(102)["scenes"][1]["framing"] == "close"
    assert env.db.md(102)["scenes"][0].get("framing") is None
    assert sorted(_heading(c["prompt"]) for c in env.gen["calls"]) == ["above_hip", "close"]


def test_the_look_refuses_a_bad_framing_before_writing(env):
    env.db.add_run(103, _md(lock=False))
    with pytest.raises(HTTPException):
        asyncio.run(th.generate_previews(103, look={"refs": {}, "scene_framings": ["wide"]}))
    assert env.gen["calls"] == [] and "framing" not in env.db.md(103)


def test_photo_stills_record_the_photos_framing(env, monkeypatch):
    monkeypatch.setattr(th, "_IMAGE_GEN_SEM", None)
    env.ref_file("upload", 31, size=(720, 1280))
    md = _md(scenes=2, lock=True, refs={"character_upload_id": 31}, framing="close")
    env.db.add_run(104, md)
    look = {"refs": {"character_upload_id": 31}, "scene_overrides": [None, {"bg_mode": "new"}]}
    asyncio.run(th.generate_previews(104, look=look))
    out = env.db.md(104)
    assert out["main_as_is"] is True and out["main_framing_used"] == "as_photo"
    assert out["scenes"][1]["framing_used"] == "as_photo", "keep-character at close: the photo's framing"
    call, = env.gen["calls"]
    assert "SHOT SIZE" not in call["prompt"]
    assert env.edit["calls"] == [], "a head-and-shoulders photo is already a close-up"


@pytest.mark.parametrize("framing", ["above_hip", "full_body"])
def test_a_keep_character_still_is_made_at_the_framing(env, monkeypatch, framing):
    """Keep character + New bg on a head-and-shoulders library character: the
    background swap keeps the face, then one refine-engine edit re-frames the
    new still at the Look step's framing (the photo as the identity
    reference), and the still records that framing, not the photo's."""
    monkeypatch.setattr(th, "_IMAGE_GEN_SEM", None)
    photo = env.ref_file("upload", 32, size=(1024, 1024))
    md = _md(scenes=2, lock=True, refs={"character_upload_id": 32}, framing=framing)
    env.db.add_run(108, md)
    look = {"refs": {"character_upload_id": 32}, "bg_mode": "new"}
    asyncio.run(th.generate_previews(108, look=look))
    out = env.db.md(108)
    assert out["main_framing_used"] == framing and out["main_as_is"] is False
    assert out["scenes"][1]["framing_used"] == framing
    assert len(env.gen["calls"]) == 2 and all("SHOT SIZE" not in c["prompt"] for c in env.gen["calls"])
    assert len(env.edit["calls"]) == 2
    for call in env.edit["calls"]:
        images = [Path(v) for k, v in call["parts"] if k == "image"]
        assert len(images) == 2 and images[1] == photo, "IMAGE 2 is the photo (identity only)"
        assert thp.framing_reshoot_change(framing) in _texts(call) and call["aspect"] == "9:16"
    assert thp.framing_reshoot_change(framing) in env.db.rows[108]["prompt_used"]


def test_a_failed_keep_character_reframe_keeps_the_photo_framed_still(env, monkeypatch):
    monkeypatch.setattr(th, "_IMAGE_GEN_SEM", None)

    async def refuse(parts, *, system_instruction, timeout, aspect_ratio=None):
        from app.services.vertex_client import GeminiNoImageError
        raise GeminiNoImageError("blocked", blocked=True)

    from app.services import card_animation as ca
    monkeypatch.setattr(ca, "render_card_image", refuse)
    env.ref_file("upload", 33, size=(1024, 1024))
    env.db.add_run(109, _md(lock=True, refs={"character_upload_id": 33}, framing="above_hip"))
    asyncio.run(th.generate_previews(109, look={"refs": {"character_upload_id": 33}, "bg_mode": "new"}))
    out = env.db.md(109)
    assert out["main_framing_used"] == "as_photo" and out.get("main_error") is None
    assert env.db.rows[109]["local_path"], "the background swap is kept"


def test_regenerate_uses_the_scenes_framing(env):
    md = _md(scenes=2, lock=False, framing="close")
    md["scenes"][1].update(framing="above_hip", keyframe_id=555, framing_used="close")
    env.db.add_run(105, md, local_path="x/main.png")
    _seed_scene_row(env, 105, 1, 555)
    asyncio.run(th.retry_preview(run_id=105, which="scene_1", feedback_text=""))
    call, = env.gen["calls"]
    assert _heading(call["prompt"]) == "above_hip"
    sc = env.db.md(105)["scenes"][1]
    assert sc["framing_used"] == "above_hip"
    assert env.db.rows[sc["keyframe_id"]]["metadata"]["framing_used"] == "above_hip"


def test_added_scenes_are_made_at_their_framing(env):
    md = _md(scenes=3, lock=False, framing="full_body")
    env.db.add_run(106, md, local_path="x/main.png")
    asyncio.run(th.generate_previews_for_scenes(106, [1, 2]))
    assert {_heading(c["prompt"]) for c in env.gen["calls"]} == {"full_body"}
    sc = env.db.md(106)["scenes"]
    assert sc[1]["framing_used"] == sc[2]["framing_used"] == "full_body"
    assert sc[1]["error"] is None


def test_the_edit_regen_records_the_framing(env):
    md = _md(scenes=2, lock=False, framing="above_hip")
    md["scenes"][1].update(keyframe_id=600, framing_used="close")
    env.db.add_run(107, md)
    _seed_scene_row(env, 107, 1, 600)
    # A composed still is made at the scene's framing.
    asyncio.run(th._regen_scene_keyframe(107, scene_index=1, keep_subject=False,
                                         has_char_ref=False, has_bg_ref=False))
    assert env.db.md(107)["scenes"][1]["framing_used"] == "above_hip"
    # A background swap keeps the still it edits, so its framing too.
    env.db.md(107)["scenes"][1]["framing_used"] = "close"
    asyncio.run(th._regen_scene_keyframe(107, scene_index=1, keep_subject=True,
                                         has_char_ref=False, has_bg_ref=False))
    assert env.db.md(107)["scenes"][1]["framing_used"] == "close"


# ---------------------------------------------------------------------------
# How a framing change regenerates
# ---------------------------------------------------------------------------

def _spec(**kw):
    base = dict(scene_index=1, char_changed=False, bg_changed=False, bg_mode=None,
                has_char=False, has_bg=False, has_still=True)
    base.update(kw)
    return th._keyframe_regen_spec(**base)


def test_regen_spec_for_a_framing_change():
    assert _spec(framing_changed=True) == {"scene_index": 1, "reshoot": True}
    assert _spec(framing_changed=True, has_still=False) == {
        "scene_index": 1, "keep_subject": False, "has_char_ref": False, "has_bg_ref": False}
    assert _spec(framing_changed=True, as_is=True) is None, "the photo keeps its framing"
    # A new background too: swap it on the still, then re-shoot.
    assert _spec(framing_changed=True, bg_changed=True, has_bg=True) == {
        "scene_index": 1, "keep_subject": True, "has_char_ref": False, "has_bg_ref": True,
        "reshoot": True}
    # A new person is composed, at the new framing: no re-shoot after.
    assert _spec(framing_changed=True, char_changed=True, has_char=True) == {
        "scene_index": 1, "keep_subject": False, "has_char_ref": True, "has_bg_ref": False}
    # Without a framing change nothing differs from before.
    assert _spec() is None
    assert _spec(bg_changed=True, has_bg=True) == {
        "scene_index": 1, "keep_subject": True, "has_char_ref": False, "has_bg_ref": True}


def test_edit_run_framing_reshoots_only_the_scenes_that_follow_the_run(fenv):
    md = _md(scenes=4, lock=False)  # a legacy run: close
    md["scenes"][1].update(keyframe_id=701, animation_id=55)
    md["scenes"][2].update(keyframe_id=702, framing="full_body", framing_used="full_body")
    md["scenes"][3].update(keyframe_id=703, as_is=True, character_upload_id=31)
    fenv.db.add_run(110, md, local_path="x/main.png")
    asyncio.run(th.edit_run(run_id=110, framing="above_hip"))
    out = fenv.db.md(110)
    assert out["framing"] == "above_hip"
    specs, = fenv.dispatched
    assert specs == [{"scene_index": 0, "reshoot": True}, {"scene_index": 1, "reshoot": True}]
    sc = out["scenes"]
    # A clip goes out of date when its re-framed still is stored, not when
    # the re-shoot is asked for (one that fails leaves the still and clip).
    assert not any(s.get("video_stale") for s in sc)
    assert sc[0]["keyframe_generating"] and sc[1]["keyframe_generating"]
    assert not sc[2].get("keyframe_generating") and not sc[3].get("keyframe_generating")
    assert out["main_keyframe_generating"] is True


def test_edit_run_scene_framings_touch_only_that_scene(fenv):
    md = _md(scenes=3, lock=False, framing="above_hip")
    for i in (1, 2):
        md["scenes"][i].update(keyframe_id=710 + i, framing_used="above_hip")
    md["main_framing_used"] = "above_hip"
    fenv.db.add_run(111, md, local_path="x/main.png")
    asyncio.run(th.edit_run(run_id=111, scene_framings=[None, "full_body"]))
    sc = fenv.db.md(111)["scenes"]
    assert sc[1]["framing"] == "full_body" and not sc[1]["video_stale"], "stale only once re-shot"
    assert not sc[0]["video_stale"] and not sc[2]["video_stale"]
    assert fenv.dispatched == [[{"scene_index": 1, "reshoot": True}]]


def test_a_scene_set_back_to_the_runs_framing_follows_it(fenv):
    md = _md(scenes=2, lock=False, framing="above_hip")
    md["scenes"][1].update(keyframe_id=720, framing="full_body", framing_used="full_body")
    md["main_framing_used"] = "above_hip"
    fenv.db.add_run(112, md, local_path="x/main.png")
    asyncio.run(th.edit_run(run_id=112, scene_framings=[None, "above_hip"]))
    sc = fenv.db.md(112)["scenes"]
    assert sc[1]["framing"] is None, "the run's framing is followed, not copied"
    assert fenv.dispatched == [[{"scene_index": 1, "reshoot": True}]]
    # While that re-shoot runs, asking again starts nothing (double click).
    fenv.dispatched.clear()
    asyncio.run(th.edit_run(run_id=112, scene_framings=[None, "close"]))
    assert fenv.dispatched == []
    # Once its still shows the run's framing, "inherit" has nothing to do.
    fenv.db.md(112)["scenes"][1].update(keyframe_generating=False, framing=None,
                                        framing_used="above_hip")
    asyncio.run(th.edit_run(run_id=112, scene_framings=[None, "inherit"]))
    assert fenv.dispatched == []
    assert fenv.db.md(112)["scenes"][1]["framing"] is None


def test_single_scene_tile_sets_the_run_framing(fenv):
    fenv.db.add_run(114, _md(lock=False, framing="above_hip", main_framing_used="above_hip"),
                    local_path="x/main.png")
    asyncio.run(th.edit_run(run_id=114, scene_framings=["full_body"]))
    md = fenv.db.md(114)
    assert md["framing"] == "full_body" and not md.get("main_video_stale"), "stale only once re-shot"
    assert md["main_keyframe_generating"] is True
    assert fenv.dispatched == [[{"scene_index": 0, "reshoot": True}]]


def test_asking_a_photo_framed_still_for_the_runs_framing_reshoots_it(fenv):
    # Character + New bg: the still has the photo's framing. Its tile asks
    # for the run's framing (already the setting): it is re-shot.
    md = _md(scenes=2, lock=True, framing="above_hip")
    md["scenes"][1].update(keyframe_id=730, framing_used="as_photo")
    md["main_framing_used"] = "above_hip"
    fenv.db.add_run(115, md, local_path="x/main.png")
    asyncio.run(th.edit_run(run_id=115, scene_framings=[None, "above_hip"]))
    assert fenv.dispatched == [[{"scene_index": 1, "reshoot": True}]]


def test_a_framing_the_still_already_has_does_nothing(fenv):
    md = _md(scenes=2, lock=False, framing="above_hip")
    md["scenes"][1].update(keyframe_id=740, framing_used="full_body")
    md["main_framing_used"] = "above_hip"
    fenv.db.add_run(116, md, local_path="x/main.png")
    asyncio.run(th.edit_run(run_id=116, scene_framings=[None, "full_body"]))
    assert fenv.dispatched == []
    assert fenv.db.md(116)["scenes"][1]["framing"] == "full_body"
    assert not fenv.db.md(116)["scenes"][1]["video_stale"]
    asyncio.run(th.edit_run(run_id=116, framing="above_hip"))
    assert fenv.dispatched == [], "the same run framing again: nothing to re-shoot"


def test_before_stills_a_framing_is_only_saved(fenv):
    fenv.db.add_run(117, _md(scenes=2, lock=False))
    asyncio.run(th.edit_run(run_id=117, framing="full_body", scene_framings=[None, "close"]))
    md = fenv.db.md(117)
    assert md["framing"] == "full_body" and md["scenes"][1]["framing"] == "close"
    assert fenv.dispatched == [] and not md["scenes"][1]["video_stale"]
    assert fenv.gen["calls"] == [] and fenv.edit["calls"] == []


def test_a_scene_without_a_still_is_composed(fenv):
    md = _md(scenes=2, lock=False, framing="above_hip")
    md["scenes"][1]["error"] = "model said no"
    md["main_framing_used"] = "above_hip"
    fenv.db.add_run(118, md, local_path="x/main.png")
    asyncio.run(th.edit_run(run_id=118, scene_framings=[None, "close"]))
    assert fenv.dispatched == [[{"scene_index": 1, "keep_subject": False,
                                 "has_char_ref": False, "has_bg_ref": False}]]


def test_edit_run_refuses_a_bad_framing_before_writing(fenv):
    fenv.db.add_run(119, _md(scenes=2, lock=False))
    for kw in ({"framing": "wide"}, {"scene_framings": [None, "wide"]}):
        with pytest.raises(HTTPException) as e:
            asyncio.run(th.edit_run(run_id=119, **kw))
        assert e.value.status_code == 400
    assert "framing" not in fenv.db.md(119)


def test_a_framing_change_with_a_new_background_swaps_then_reshoots(fenv):
    bg = fenv.ref_file("asset", 4, size=(1500, 1000))
    md = _md(scenes=2, lock=False, framing="above_hip")
    md["scenes"][1].update(keyframe_id=750, framing_used="above_hip")
    md["main_framing_used"] = "above_hip"
    fenv.db.add_run(120, md, local_path="x/main.png")
    asyncio.run(th.edit_run(run_id=120, scene_framings=[None, "full_body"],
                            scene_overrides=[None, {"bg_asset_id": 4}]))
    assert fenv.dispatched == [[{"scene_index": 1, "keep_subject": True, "has_char_ref": False,
                                 "has_bg_ref": True, "reshoot": True}]]
    assert bg.exists()


# ---------------------------------------------------------------------------
# The re-shoot: the refine engine with a canned instruction
# ---------------------------------------------------------------------------

def test_the_reshoot_is_a_refine_of_the_still_at_the_new_framing(env, monkeypatch):
    monkeypatch.setattr(th, "_IMAGE_GEN_SEM", None)
    md = _md(scenes=2, lock=False, framing="above_hip")
    md["scenes"][1].update(keyframe_id=760, framing="full_body", framing_used="above_hip",
                           keyframe_generating=True, animation_id=88)
    env.db.add_run(130, md)
    canvas = _seed_scene_row(env, 130, 1, 760)
    asyncio.run(th._run_keyframe_regen(130, [{"scene_index": 1, "reshoot": True}]))
    assert env.gen["calls"] == [], "no compose: the still is edited"
    call, = env.edit["calls"]
    images = [Path(v) for k, v in call["parts"] if k == "image"]
    assert images[0] == canvas
    assert thp.framing_reshoot_change("full_body") in _texts(call)
    assert call["aspect"] == "9:16"
    sc = env.db.md(130)["scenes"][1]
    assert sc["keyframe_id"] != 760 and sc["previous_keyframe_ids"] == [760]
    assert sc["framing_used"] == "full_body" and sc["video_stale"] is True
    assert sc["keyframe_generating"] is False and sc["error"] is None
    row = env.db.rows[sc["keyframe_id"]]
    assert row["metadata"]["retry_kind"] == "reframe" and row["metadata"]["framing_used"] == "full_body"
    assert row["feedback_text"] == thp.framing_reshoot_change("full_body")


def test_the_scene0_reshoot_records_the_run_level_framing(env, monkeypatch):
    monkeypatch.setattr(th, "_IMAGE_GEN_SEM", None)
    env.db.add_run(131, _md(lock=False, framing="close", main_keyframe_generating=True))
    old = _seed_old_main(env, 131)
    asyncio.run(th._run_keyframe_regen(131, [{"scene_index": 0, "reshoot": True}]))
    call, = env.edit["calls"]
    assert [Path(v) for k, v in call["parts"] if k == "image"][0] == old
    assert thp.framing_reshoot_change("close") in _texts(call)
    md = env.db.md(131)
    assert md["main_framing_used"] == "close" and md["main_keyframe_generating"] is False
    assert md["main_video_stale"] is True and md["main_retry_kind"] == "reframe"
    prev, = md["still_versions"]["0"]
    assert prev["local_path"] == str(old) and prev["framing_used"] is None


def test_a_failed_reshoot_is_recorded_on_the_scene(env, monkeypatch):
    monkeypatch.setattr(th, "_IMAGE_GEN_SEM", None)

    async def refuse(parts, *, system_instruction, timeout, aspect_ratio=None):
        from app.services.vertex_client import GeminiNoImageError
        raise GeminiNoImageError("blocked", blocked=True)

    from app.services import card_animation as ca
    monkeypatch.setattr(ca, "render_card_image", refuse)
    md = _md(scenes=2, lock=False, framing="full_body")
    md["scenes"][1].update(keyframe_id=770, keyframe_generating=True)
    env.db.add_run(132, md)
    _seed_scene_row(env, 132, 1, 770)
    asyncio.run(th._run_keyframe_regen(132, [{"scene_index": 1, "reshoot": True}]))
    sc = env.db.md(132)["scenes"][1]
    assert sc["keyframe_id"] == 770, "the still stays"
    assert "declined" in sc["error"] and sc["keyframe_generating"] is False
    assert env.db.rows[132]["status"] == "ready", "the run is never marked failed"


def test_undo_puts_back_the_framing_of_the_still(env, monkeypatch):
    monkeypatch.setattr(th, "_IMAGE_GEN_SEM", None)
    md = _md(scenes=2, lock=False, framing="above_hip")
    md["scenes"][1].update(keyframe_id=780, framing_used="above_hip")
    md["main_framing_used"] = "above_hip"
    env.db.add_run(133, md)
    _seed_old_main(env, 133)
    row = _seed_scene_row(env, 133, 1, 780)
    env.db.rows[780]["metadata"]["framing_used"] = "above_hip"
    assert row.exists()
    env.db.md(133)["framing"] = "full_body"
    env.db.md(133)["scenes"][1]["framing"] = None
    asyncio.run(th._reshoot_still(133, 0))
    asyncio.run(th._reshoot_still(133, 1))
    md = env.db.md(133)
    assert md["main_framing_used"] == "full_body" and md["scenes"][1]["framing_used"] == "full_body"
    asyncio.run(th.undo_still(133, "main"))
    asyncio.run(th.undo_still(133, "scene_1"))
    md = env.db.md(133)
    assert md["main_framing_used"] == "above_hip"
    assert md["scenes"][1]["framing_used"] == "above_hip" and md["scenes"][1]["keyframe_id"] == 780
    # The framing setting follows the still put back (here as a scene
    # override, the run being at full body), so a later still keeps it.
    assert md["framing"] == "full_body"
    assert th._effective_framing(md, 0) == "above_hip" and th._effective_framing(md, 1) == "above_hip"


def test_undoing_a_tile_reframe_puts_the_run_framing_back(env, monkeypatch):
    """Single scene at Above the hip, the tile's Full re-shoots it (the run's
    framing becomes full body), then Undo: the still AND the setting are
    back at Above the hip, so the next new still is not full body again."""
    monkeypatch.setattr(th, "_IMAGE_GEN_SEM", None)
    md = _md(lock=False, framing="above_hip", main_framing_used="above_hip")
    env.db.add_run(135, md)
    _seed_old_main(env, 135)
    env.db.md(135)["framing"] = "full_body"
    asyncio.run(th._reshoot_still(135, 0))
    assert env.db.md(135)["main_framing_used"] == "full_body"
    asyncio.run(th.undo_still(135, "main"))
    md = env.db.md(135)
    assert md["main_framing_used"] == "above_hip" and md["framing"] == "above_hip"
    assert th._effective_framing(md, 0) == th._still_framing(md, 0) == "above_hip"


def test_a_scene_reframe_undone_back_to_the_runs_framing_follows_the_run(env, monkeypatch):
    monkeypatch.setattr(th, "_IMAGE_GEN_SEM", None)
    md = _md(scenes=2, lock=False, framing="above_hip")
    md["scenes"][1].update(keyframe_id=785, framing_used="above_hip", framing="full_body")
    env.db.add_run(136, md)
    _seed_scene_row(env, 136, 1, 785)
    env.db.rows[785]["metadata"]["framing_used"] = "above_hip"
    asyncio.run(th._reshoot_still(136, 1))
    asyncio.run(th.undo_still(136, "scene_1"))
    sc = env.db.md(136)["scenes"][1]
    assert sc["framing_used"] == "above_hip" and sc["framing"] is None, "back to following the run"


def test_undoing_anything_but_a_reframe_leaves_the_framing_setting(env, monkeypatch):
    monkeypatch.setattr(th, "_IMAGE_GEN_SEM", None)
    md = _md(lock=False, framing="full_body", main_framing_used="above_hip")
    env.db.add_run(137, md)
    _seed_old_main(env, 137)
    asyncio.run(th.refine_still(137, "main", "warmer light"))
    asyncio.run(th.undo_still(137, "main"))
    assert env.db.md(137)["framing"] == "full_body"
    assert th._undo_framing("reframe", "as_photo") is None
    assert th._undo_framing("reframe", None) == "close", "made before framing existed"
    assert th._undo_framing("refine", "close") is None


def test_a_user_refine_keeps_the_stills_framing(env, monkeypatch):
    monkeypatch.setattr(th, "_IMAGE_GEN_SEM", None)
    md = _md(scenes=2, lock=False, framing="above_hip")
    md["scenes"][1].update(keyframe_id=790, framing_used="full_body")
    md["main_framing_used"] = "close"
    env.db.add_run(134, md)
    _seed_old_main(env, 134)
    _seed_scene_row(env, 134, 1, 790)
    asyncio.run(th.refine_still(134, "scene_1", "warmer light"))
    asyncio.run(th.refine_still(134, "main", "warmer light"))
    md = env.db.md(134)
    sc = md["scenes"][1]
    assert sc["framing_used"] == "full_body"
    assert env.db.rows[sc["keyframe_id"]]["metadata"]["framing_used"] == "full_body"
    assert md["main_framing_used"] == "close"
    assert md["still_versions"]["0"][-1]["framing_used"] == "close"


# ---------------------------------------------------------------------------
# Veo: the clip keeps the still's framing
# ---------------------------------------------------------------------------

@pytest.fixture
def push_in_on(monkeypatch):
    for k in ("VEO_LAST_FRAME_ANCHOR", "VEO_CAMERA_PUSH_IN", "VEO_CAMERA_PUSH_IN_ZOOM"):
        monkeypatch.delenv(k, raising=False)


def _png(path: Path, size=(720, 1280), color=(90, 120, 150)) -> Path:
    Image.new("RGB", size, color).save(path)
    return path


def test_push_in_per_framing(tmp_path, push_in_on):
    kf = _png(tmp_path / "main.png")
    assert th._scene_video_inputs(kf, None, None)["push_in"] == {"zoom": 1.12}
    assert th._scene_video_inputs(kf, None, None, framing="above_hip")["push_in"] == {"zoom": 1.12}
    assert th._scene_video_inputs(kf, None, None, framing="close")["push_in"] == {"zoom": 1.06,
                                                                                 "anchor": "face"}
    for f in ("full_body", "as_photo"):
        inputs = th._scene_video_inputs(kf, None, None, framing=f)
        assert inputs["last_image"] == kf and "push_in" not in inputs, f
    assert not list(tmp_path.glob("*_pushin.png")), "nothing is written here"


def test_the_zoom_env_override_is_for_the_default_push_in(tmp_path, monkeypatch, push_in_on):
    monkeypatch.setenv("VEO_CAMERA_PUSH_IN_ZOOM", "1.2")
    kf = _png(tmp_path / "main.png")
    assert th._scene_video_inputs(kf, None, None, framing="above_hip")["push_in"] == {"zoom": 1.2}
    assert th._scene_video_inputs(kf, None, None, framing="close")["push_in"]["zoom"] == 1.06
    monkeypatch.setenv("VEO_CAMERA_PUSH_IN", "0")
    assert "push_in" not in th._scene_video_inputs(kf, None, None, framing="close")


def test_a_full_body_clip_ends_on_its_own_still(tmp_path, push_in_on, monkeypatch):
    sent = {}

    async def vertex_veo(prompt, **kwargs):
        sent.update(kwargs)
        return {"operation_name": "op"}

    monkeypatch.setattr(th, "_vertex_veo_generate", vertex_veo)
    kf = _png(tmp_path / "main.png")
    asyncio.run(th.veo_generate("talk", aspect_ratio="9:16",
                                **th._scene_video_inputs(kf, None, None, framing="full_body")))
    assert sent["start_image"] == kf and sent["last_image"] == kf
    assert not (tmp_path / "main_pushin.png").exists()


def _marked(size=(720, 1280)) -> Image.Image:
    im = Image.new("RGB", size, (30, 30, 30))
    im.paste((250, 10, 10), (310, 300, 410, 420))   # the "face" box
    im.paste((10, 250, 10), (0, 1200, 720, 1280))   # a bottom band (the feet)
    return im


def test_a_close_push_in_moves_toward_the_face_and_keeps_the_head(tmp_path, monkeypatch, push_in_on):
    face = (310.0, 300.0, 100.0, 120.0)
    monkeypatch.setattr(th, "_face_box", lambda im: face)
    left, top, cw, ch = th._push_in_window(720, 1280, 1.06, face)
    assert (cw, ch) == (int(720 / 1.06), int(1280 / 1.06))
    assert top <= 300 - th._PUSH_IN_HEADROOM * 120, "the head room above the face stays"
    assert left <= 310 and left + cw >= 410 and top + ch >= 420, "the whole face stays"
    kf = tmp_path / "main.png"
    _marked().save(kf)
    sent = {}

    async def vertex_veo(prompt, **kwargs):
        sent.update(kwargs)
        return {"operation_name": "op"}

    monkeypatch.setattr(th, "_vertex_veo_generate", vertex_veo)
    asyncio.run(th.veo_generate("talk", aspect_ratio="9:16",
                                **th._scene_video_inputs(kf, None, None, framing="close")))
    end = Image.open(sent["last_image"]).convert("RGB")
    assert end.size == (720, 1280) and sent["last_image"].name == "main_pushin.png"
    # The face is bigger and still fully in; the push-in took the bottom first.
    reds = [y for y in range(end.height) if end.getpixel((360, y))[0] > 200]
    assert len(reds) > 120 and reds[0] > 0, "face larger, not cut at the top"
    assert end.getpixel((360, 0))[0] < 100, "head room above the face"


def test_without_a_face_the_close_push_in_is_centred(monkeypatch):
    assert th._push_in_window(720, 1280, 1.06, None) == ((720 - 679) // 2, (1280 - 1207) // 2, 679, 1207)


def test_a_face_near_the_edge_keeps_the_window_inside(monkeypatch):
    left, top, cw, ch = th._push_in_window(720, 1280, 1.06, (0.0, 0.0, 80.0, 80.0))
    assert left == 0 and top == 0
    left, top, cw, ch = th._push_in_window(720, 1280, 1.06, (700.0, 1250.0, 20.0, 20.0))
    assert left + cw <= 720 and top + ch <= 1280


def test_the_likeness_retry_never_crops_a_full_body_still(tmp_path):
    kf = tmp_path / "main.png"
    _marked().save(kf)
    for strength in (1, 2, 3):
        out = th._perturb_keyframe_for_rai(kf, strength, framing="full_body")
        im = Image.open(out).convert("RGB")
        assert im.size == (720, 1280)
        band = [im.getpixel((x, y))[1] for x in range(0, 720, 37) for y in (1205, 1240, 1275)]
        assert sum(1 for g in band if g > 200) >= len(band) * 0.9, "the feet band stays"
        # Flipped: the face box (x 310-410) now sits at 310-410 mirrored.
        assert im.getpixel((720 - 360, 360))[0] > 200


def test_the_likeness_retry_crop_per_framing(tmp_path):
    kf = tmp_path / "main.png"
    _marked().save(kf)

    def band_rows(framing, strength):
        im = Image.open(th._perturb_keyframe_for_rai(kf, strength, framing=framing)).convert("RGB")
        return sum(1 for y in range(im.height) if im.getpixel((360, y))[1] > 200)

    # The 80-row band sits on the bottom edge: a crop of m per edge takes
    # m * 1280 rows of it, and the rest is scaled up by 1 / (1 - 2m).
    def left_after(m):
        return max(0.0, (80 - m * 1280) / (1 - 2 * m))

    assert band_rows("full_body", 3) == pytest.approx(80, abs=3), "no crop at all"
    assert band_rows("as_photo", 3) == pytest.approx(80, abs=3)
    assert band_rows("close", 1) == pytest.approx(left_after(0.01), abs=3)
    assert band_rows("close", 3) == pytest.approx(left_after(0.03), abs=3), "at most 3% per edge"
    # Above the hip, or no framing known: today's 5 / 10 / 15%.
    assert band_rows(None, 1) == pytest.approx(left_after(0.05), abs=3)
    assert band_rows("above_hip", 1) == band_rows(None, 1)
    assert band_rows(None, 3) == 0


def test_veo_call_sites_pass_the_stills_framing():
    import inspect
    src = inspect.getsource(th)
    assert src.count("framing=framing") + src.count("framing=main_framing") >= 9
    for fn in (th.start_videos, th.start_videos_multi, th.retry_video, th.resubmit_animation_if_transient):
        body = inspect.getsource(fn)
        assert "_still_framing(" in body, fn.__name__
    body = inspect.getsource(th.resubmit_animation_if_transient)
    assert "_perturb_keyframe_for_rai(kf, attempts + 1, framing=framing)" in body


# ---------------------------------------------------------------------------
# fetch_run
# ---------------------------------------------------------------------------

@pytest.fixture
def fetch_env(monkeypatch):
    s = {"run": None, "scene_rows": {}}

    async def fetch_run_row(run_id):
        return copy.deepcopy(s["run"])

    async def scene_row(scene_id):
        return copy.deepcopy(s["scene_rows"].get(int(scene_id)))

    async def none(*_a, **_k):
        return None

    monkeypatch.setattr(th, "_fetch_run_row", fetch_run_row)
    monkeypatch.setattr(th, "_fetch_scene_row", scene_row)
    monkeypatch.setattr(th, "_fetch_animation_row", none)
    monkeypatch.setattr(th, "_fetch_card_row", none)
    return s


def _row(md: dict, **cols) -> dict:
    row = {"id": 5, "topic": "t", "mode": th.MODE_PREVIEW, "status": "ready",
           "image_model": "gemini_pro", "local_path": None, "storage_bucket": None,
           "storage_path": None, "width": None, "height": None, "error": None,
           "saved_asset_id": None, "created_at": None, "updated_at": None, "metadata": md}
    row.update(cols)
    return row


def test_fetch_run_legacy_run_reads_close(fetch_env):
    fetch_env["run"] = _row({"aspect_ratio": "9:16"}, local_path="x/main.png")
    out = asyncio.run(REAL_FETCH_RUN(5, with_reel=False))
    assert out["framing"] == "close"
    s0, = out["scenes"]
    assert s0["framing"] is None and s0["framing_effective"] == "close" and s0["framing_used"] == "close"


def test_fetch_run_gives_each_scenes_framing(fetch_env):
    md = {"aspect_ratio": "9:16", "framing": "above_hip", "main_framing_used": "above_hip",
          "scene_count": 4, "scenes": [
              {"index": 0},
              {"index": 1, "keyframe_id": 901, "framing": "full_body", "framing_used": "full_body"},
              {"index": 2, "framing": "close"},
              {"index": 3, "keyframe_id": 903, "as_is": True},
          ]}
    fetch_env["run"] = _row(md, local_path="x/main.png")
    for kid in (901, 903):
        fetch_env["scene_rows"][kid] = {"id": kid, "local_path": f"x/s{kid}.png", "width": 756,
                                        "height": 1344, "updated_at": None}
    out = asyncio.run(REAL_FETCH_RUN(5, with_reel=False))
    assert out["framing"] == "above_hip"
    got = [(s["framing"], s["framing_effective"], s["framing_used"]) for s in out["scenes"]]
    assert got == [
        (None, "above_hip", "above_hip"),
        ("full_body", "full_body", "full_body"),
        ("close", "close", None),          # no still yet
        (None, "above_hip", "as_photo"),   # the character photo itself
    ]


# ---------------------------------------------------------------------------
# The routes
# ---------------------------------------------------------------------------

@pytest.fixture
def client(monkeypatch):
    from app.routers import yas_producer as yp

    seen: dict = {}

    async def capture(name, *a, **kw):
        seen[name] = kw
        return {"id": 5}

    async def edit_run(**kw):
        return await capture("edit", **kw)

    async def generate_previews(run_id, *, look=None):
        return await capture("previews", look=look)

    async def generate_script(**kw):
        return await capture("script", **kw)

    monkeypatch.setattr(th, "edit_run", edit_run)
    monkeypatch.setattr(th, "generate_previews", generate_previews)
    monkeypatch.setattr(th, "generate_script", generate_script)
    app = FastAPI()
    app.include_router(yp.router)
    c = TestClient(app)
    c.seen = seen
    return c


def test_the_edit_route_passes_framing_only_when_sent(client):
    r = client.post("/api/yas-producer/talking-head-v2/5/edit", json={"topic": "x"})
    assert r.status_code == 200
    assert client.seen["edit"]["framing"] is None and client.seen["edit"]["scene_framings"] is None
    r = client.post("/api/yas-producer/talking-head-v2/5/edit",
                    json={"framing": "full_body", "scene_framings": [None, "close", "inherit"]})
    assert r.status_code == 200
    assert client.seen["edit"]["framing"] == "full_body"
    assert client.seen["edit"]["scene_framings"] == [None, "close", "inherit"]


@pytest.mark.parametrize("body", [{"framing": "wide"}, {"framing": "inherit"},
                                  {"scene_framings": ["far away"]}])
def test_the_edit_route_refuses_a_bad_framing(client, body):
    r = client.post("/api/yas-producer/talking-head-v2/5/edit", json=body)
    assert r.status_code == 422 and "edit" not in client.seen


def test_the_previews_route_puts_the_framing_in_the_look(client):
    r = client.post("/api/yas-producer/talking-head-v2/5/generate-previews",
                    json={"framing": "close", "scene_framings": [None, "full_body"]})
    assert r.status_code == 200
    look = client.seen["previews"]["look"]
    assert look["framing"] == "close" and look["scene_framings"] == [None, "full_body"]
    client.post("/api/yas-producer/talking-head-v2/5/generate-previews", json={"lock_character": True})
    assert "framing" not in client.seen["previews"]["look"]


def test_the_script_route_passes_the_framing(client):
    r = client.post("/api/yas-producer/talking-head-v2/generate-script",
                    json={"topic": "Peer learning", "framing": "above_hip"})
    assert r.status_code == 200 and client.seen["script"]["framing"] == "above_hip"
    client.post("/api/yas-producer/talking-head-v2/generate-script", json={"topic": "Peer learning"})
    assert client.seen["script"]["framing"] is None, "the service defaults it (above the hip)"
    r = client.post("/api/yas-producer/talking-head-v2/generate-script",
                    json={"topic": "Peer learning", "framing": "wide"})
    assert r.status_code == 422
