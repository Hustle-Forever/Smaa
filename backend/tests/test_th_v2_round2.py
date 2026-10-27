"""TH v2, round 2 (the live QA of run 830): paid work only on a press, the
real cost of the clips, an Undo that keeps a clip fresh, a refine that never
hangs, what a clip turned out to be, and an honest framing (a tighter one is
a crop).

- /finalize (Veo for the clips) runs only with {"confirm": true}: run 830 got
  two unasked rounds from a page that sent it on entering the Videos step.
- fetch_run's videos_plan is what the next press makes and costs, from the
  meter's own Veo rates: 8 s scenes with audio, 4 s silent Veo bookends, the
  free slow zoom; the QA round (2 scenes, 2 Veo bookends) is $3.60.
- A clip records the still it was made from (video_source): an Undo back to
  that still keeps the clip up to date; a framing change marks it stale only
  once the re-framed still is stored.
- Refine: one edit gets REFINE_ATTEMPT_TIMEOUT_SEC, a timeout or no image is
  asked once more WITHOUT the identity reference, the whole refine is bounded,
  every image part is at most REFINE_MAX_EDGE px.
- An intro/outro clip says what it turned out to be (veo, zoom, veo_replaced).
- A tighter framing is a crop around the face (no model); a wider one is a
  re-shoot whose face is measured, and the framing recorded is what it shows.

No database and no model calls: the in-memory FakePg and fakes of
test_th_v2_scene_stills / test_th_v2_bookends (an unexpected model call fails).
"""
from __future__ import annotations

import asyncio
import shutil
import time
from io import BytesIO
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image

from app.services import card_animation as ca
from app.services import yas_talking_head_v2 as th
from app.services import yas_talking_head_v2_prompts as thp
from app.services.vertex_client import GeminiNoImageError, get_meter
from test_th_v2_bookends import (  # noqa: F401
    ICONS, LOGO_ONLY, REAL_FETCH_RUN, _bookend_md, _seed_card, _stills_run, benv,
)
from test_th_v2_scene_stills import _md, _png_bytes, _seed_old_main, _seed_scene_row, env  # noqa: F401

FACE = Path(__file__).parent / "fixtures" / "th_framing" / "speaker_above_hip.jpg"


@pytest.fixture
def r2env(env, monkeypatch):
    """env with a fresh image-gen semaphore (bound to the test's loop)."""
    monkeypatch.setattr(th, "_IMAGE_GEN_SEM", None)
    return env


def _scripted_render(monkeypatch, outcomes: list):
    """render_card_image answering with `outcomes` in turn (bytes, or an
    exception to raise; the last repeats). Records each call's parts and the
    size of each image part as it was sent."""
    calls: list[dict] = []

    async def render(parts, *, system_instruction, timeout, aspect_ratio=None):
        images = [Path(v) for k, v in parts if k == "image"]
        sizes = []
        for p in images:
            with Image.open(p) as im:
                sizes.append(im.size)
        calls.append({"images": images, "sizes": sizes, "timeout": timeout})
        out = outcomes[min(len(calls), len(outcomes)) - 1]
        if isinstance(out, BaseException):
            raise out
        if callable(out):
            return await out()
        return out

    monkeypatch.setattr(ca, "render_card_image", render)
    return calls


# ---------------------------------------------------------------------------
# T1: Veo only on an explicit press
# ---------------------------------------------------------------------------

def test_finalize_starts_clips_only_with_an_explicit_confirm(monkeypatch):
    from app.routers import yas_producer as router_mod

    calls: list[int] = []

    async def dispatch(run_id):
        calls.append(run_id)
        return {"id": run_id}

    monkeypatch.setattr(router_mod.th_v2_service, "start_videos_dispatch", dispatch)
    app = FastAPI()
    app.include_router(router_mod.router)
    client = TestClient(app)
    url = "/api/yas-producer/talking-head-v2/830/finalize"
    # What run 830's page sent on entering Videos (an empty body), or anything
    # short of the button's confirm: refused, and nothing is started.
    for body in (None, {}, {"confirm": False}):
        r = client.post(url, content="{}") if body is None else client.post(url, json=body)
        assert r.status_code == 409, body
        assert "Make videos" in r.json()["detail"] and "Nothing was started" in r.json()["detail"]
    r = client.post(url)
    assert r.status_code == 409
    assert calls == []
    r = client.post(url, json={"confirm": True})
    assert r.status_code == 200 and calls == [830]


# ---------------------------------------------------------------------------
# T2: what the next press makes, and what it costs
# ---------------------------------------------------------------------------

FAST = th._veo_model_for("normal")
PREMIUM = th._veo_model_for("quality")


def _card(plan: dict | None, *, source_ref=None) -> dict:
    return {"id": 1, "metadata": {"source_ref": source_ref, "motion": plan, "card_type": "intro"}}


def _two_scene_md(**kw) -> dict:
    md = _md(scenes=2, intro_excluded=False, outro_excluded=False, refs={}, **kw)
    return md


def test_the_qa_round_costs_what_it_booked():
    """Run 830: 2 scenes of 8 s with audio on Veo Fast and both bookends on
    Veo: $1.40 + $1.40 + $0.40 + $0.40 = $3.60 (the button said ~$1.20)."""
    veo = th._bookend_plan_from(ICONS)
    plan = th._videos_plan(
        _two_scene_md(), main_anim=None, scene_anims={0: None, 1: None},
        bookend_anims={"intro": None, "outro": None},
        cards={"intro": _card(veo), "outro": _card(veo)},
    )
    assert plan["count"] == 4 and plan["usd"] == 3.6 and plan["usd_max"] == 3.6
    assert not plan["any_clip"]
    scenes = [c for c in plan["clips"] if c["target"].startswith("scene_")]
    assert [c["usd"] for c in scenes] == [1.4, 1.4]
    assert all(c["seconds"] == 8 and c["audio"] and c["model"] == FAST for c in scenes)
    ends = [c for c in plan["clips"] if c["target"] in ("intro", "outro")]
    assert [(c["engine"], c["usd"], c["seconds"], c["audio"]) for c in ends] == [("veo", 0.4, 4, False)] * 2
    # The same rates the meter books.
    assert scenes[0]["usd"] == round(get_meter().estimate_veo(FAST, 8, True), 4)
    assert ends[0]["usd"] == round(get_meter().estimate_veo(FAST, 4, False), 4)


def test_a_slow_zoom_is_free_and_an_unplanned_card_is_a_range():
    zoom = th._bookend_plan_from(LOGO_ONLY)
    old = {"mode": "veo", "elements": [{"what": "vertical line"}]}  # a plan from before v2
    plan = th._videos_plan(
        _md(intro_excluded=False, outro_excluded=False, refs={}), main_anim=None, scene_anims={},
        bookend_anims={"intro": None, "outro": None}, cards={"intro": _card(zoom), "outro": _card(old)},
    )
    by = {c["target"]: c for c in plan["clips"]}
    assert by["intro"]["engine"] == "zoom" and by["intro"]["usd"] == 0 and by["intro"]["model"] is None
    assert by["outro"]["engine"] == "veo_or_zoom" and by["outro"]["usd"] == 0 and by["outro"]["usd_max"] == 0.4
    assert plan["usd"] == 1.4 and plan["usd_max"] == 1.8


def test_only_the_clips_that_need_it_are_counted():
    md = _two_scene_md()
    md["scenes"][0].update(video_stale=True)
    md["scenes"][1].update(video_quality="quality")
    md["outro_video_upload_id"] = None
    md["refs"] = {"outro_video_upload_id": 9}  # an uploaded outro video: no clip
    ready, failed = {"id": 1, "status": "ready"}, {"id": 2, "status": "failed"}
    plan = th._videos_plan(
        md, main_anim=None, scene_anims={0: ready, 1: failed},
        bookend_anims={"intro": ready, "outro": None},
        cards={"intro": _card(th._bookend_plan_from(ICONS)), "outro": None},
    )
    assert [(c["target"], c["why"]) for c in plan["clips"]] == [("scene_0", "stale"), ("scene_1", "failed")]
    assert plan["clips"][1]["model"] == PREMIUM and plan["clips"][1]["usd"] == 6.0
    assert plan["usd"] == 7.4 and plan["any_clip"]
    # Nothing out of date: nothing to make.
    md["scenes"][0]["video_stale"] = False
    fresh = th._videos_plan(md, main_anim=None, scene_anims={0: ready, 1: ready},
                            bookend_anims={"intro": ready, "outro": None},
                            cards={"intro": _card(th._bookend_plan_from(ICONS)), "outro": None})
    assert fresh["count"] == 0 and fresh["usd"] == 0
    # An excluded scene or bookend is never made.
    md["scenes"][1]["excluded"] = True
    md["intro_excluded"] = True
    skip = th._videos_plan(md, main_anim=None, scene_anims={0: None, 1: None},
                           bookend_anims={"intro": None, "outro": None}, cards={"intro": None, "outro": None})
    assert [c["target"] for c in skip["clips"]] == ["scene_0"]


def test_a_bookend_with_nothing_to_animate_makes_no_clip():
    plan = th._videos_plan(_md(intro_excluded=False, refs={}), main_anim={"status": "ready"},
                           scene_anims={}, bookend_anims={"intro": None, "outro": None},
                           cards={"intro": None, "outro": None})
    assert plan["count"] == 0, "no card and no pick: nothing to animate"
    # A pick with no card yet: a clip, planned when it is made.
    plan = th._videos_plan(_md(intro_excluded=False, refs={"intro_asset_id": 242}),
                           main_anim={"status": "ready"}, scene_anims={},
                           bookend_anims={"intro": None, "outro": None}, cards={"intro": None, "outro": None})
    assert [(c["target"], c["engine"]) for c in plan["clips"]] == [("intro", "veo_or_zoom")]


def test_fetch_run_sends_the_plan_and_what_each_bookend_clip_became(benv, monkeypatch):
    _stills_run(benv, 870, refs={})
    md = benv.db.md(870)
    intro = _seed_card(benv, 870, "intro", source_ref=None)
    outro = _seed_card(benv, 870, "outro", source_ref=None)
    benv.db.rows[intro]["metadata"]["motion"] = th._bookend_plan_from(ICONS)
    benv.db.rows[outro]["metadata"]["motion"] = {"mode": "veo"}  # cached under the old rules
    benv.anims[701] = {"id": 701, "status": "ready", "local_path": "x.mp4", "storage_bucket": None,
                       "storage_path": None, "duration_sec": 4, "with_audio": False, "cost_usd": 0.4,
                       "error": None, "veo_operation_name": "op",
                       "metadata": {"role": "intro", "bookend_check": True,
                                    "bookend_text_check": {"ok": False, "fallback": "slow_zoom",
                                                           "reason": "the card's colours changed (frame 3)"}}}
    md.update(intro_image_id=intro, outro_image_id=outro, intro_animation_id=701)
    monkeypatch.setattr(th, "fetch_run", REAL_FETCH_RUN)
    got = asyncio.run(th.fetch_run(870, with_reel=False))
    clip = got["animations"]["intro"]
    assert clip["motion"] == "veo_replaced" and "colours" in clip["motion_reason"]
    assert got["intro"]["motion"] == "veo", "the plan under the current rules"
    assert got["outro"]["motion"] is None, "an old plan is planned again, not shown"
    plan = got["videos_plan"]
    assert [c["target"] for c in plan["clips"]] == ["main", "outro"], "the intro's clip is made"
    assert plan["usd"] == 1.4 and plan["usd_max"] == 1.8


# ---------------------------------------------------------------------------
# T7: what a bookend clip turned out to be
# ---------------------------------------------------------------------------

def test_a_clip_says_what_it_became():
    def row(meta, status="ready"):
        return {"status": status, "metadata": meta}

    assert th._clip_motion(row({"slow_zoom": True, "bookend_motion": {"mode": "zoom"}})) == "zoom"
    assert th._clip_motion(row({"bookend_check": True,
                                "bookend_text_check": {"ok": False, "fallback": "slow_zoom"}})) == "veo_replaced"
    assert th._clip_motion(row({"bookend_check": True, "bookend_text_check": {"ok": True}})) == "veo"
    assert th._clip_motion(row({"bookend_check": True}, status="generating")) == "veo_checking"
    assert th._clip_motion(row({"bookend_check": True})) == "veo_checking"
    assert th._clip_motion(row({"role": "scene_0"})) is None
    assert th._clip_motion(None) is None
    # A failed clip moves nothing, whatever it was asked to be.
    assert th._clip_motion(row({"bookend_check": True}, status="failed")) is None
    assert th._clip_motion(row({"slow_zoom": True}, status="failed")) is None
    # The check failed but the slow zoom could not be made: the Veo clip was
    # kept, and it is not said to be fine; a card gone: not checked.
    assert th._clip_motion(row({"bookend_check": True, "bookend_text_check": {
        "ok": False, "reason": "added marks", "fallback": None}})) == "veo_flagged"
    assert th._clip_motion(row({"bookend_check": True, "bookend_text_check": {
        "ok": None, "reason": "the card is gone; the clip is kept unchecked"}})) == "veo_unchecked"


# ---------------------------------------------------------------------------
# T3: a clip goes out of date only when its still really changed
# ---------------------------------------------------------------------------

def _clip_made_from_current(env, run_id: int, i: int = 0) -> None:
    md = env.db.md(run_id)
    key = th._still_key(env.db.rows[run_id], md, i)
    if i == 0 and int(md.get("scene_count") or 1) <= 1:
        md.update(main_animation_id=77, main_video_source=key, main_video_stale=False)
    else:
        md["scenes"][i].update(animation_id=77, video_source=key, video_stale=False)


def test_undo_back_to_the_clips_still_keeps_the_clip_fresh(r2env):
    r2env.db.add_run(201, _md(lock=False))
    _seed_old_main(r2env, 201)
    _clip_made_from_current(r2env, 201)
    asyncio.run(th.refine_still(201, "main", "warmer light"))
    assert r2env.db.md(201)["main_video_stale"] is True, "a new still: the clip is out of date"
    asyncio.run(th.undo_still(201, "main"))
    assert r2env.db.md(201)["main_video_stale"] is False, "the clip's own still is back"
    # Two changes, one Undo: the still back is not the clip's.
    asyncio.run(th.refine_still(201, "main", "warmer"))
    asyncio.run(th.refine_still(201, "main", "cooler"))
    asyncio.run(th.undo_still(201, "main"))
    assert r2env.db.md(201)["main_video_stale"] is True


def test_undo_on_a_later_scene_compares_its_row(r2env):
    md = _md(scenes=2, lock=False)
    md["scenes"][1].update(keyframe_id=555)
    r2env.db.add_run(202, md)
    _seed_scene_row(r2env, 202, 1, 555)
    _clip_made_from_current(r2env, 202, 1)
    asyncio.run(th.refine_still(202, "scene_1", "brighter"))
    assert r2env.db.md(202)["scenes"][1]["video_stale"] is True
    asyncio.run(th.undo_still(202, "scene_1"))
    sc = r2env.db.md(202)["scenes"][1]
    assert sc["keyframe_id"] == 555 and sc["video_stale"] is False


def test_a_clip_that_recorded_nothing_and_is_out_of_date_stays_so_after_an_undo(r2env):
    # Round 3 (U1): a clip that records nothing but is up to date takes the
    # still being replaced as its source (test_th_v2_round3); one already out
    # of date records nothing, and an Undo never makes it fresh.
    r2env.db.add_run(203, _md(lock=False, main_animation_id=77, main_video_stale=True))
    _seed_old_main(r2env, 203)
    asyncio.run(th.refine_still(203, "main", "warmer"))
    asyncio.run(th.undo_still(203, "main"))
    assert r2env.db.md(203)["main_video_stale"] is True, "made before clips recorded their still"


def test_making_the_clip_records_its_still(benv):
    _stills_run(benv, 871, intro_excluded=True, outro_excluded=True)
    asyncio.run(th.start_videos(871))
    md = benv.db.md(871)
    assert md["main_video_source"] == th._still_key(benv.db.rows[871], md, 0) is not None
    assert md["main_video_stale"] is False
    # A video retry records it too.
    benv.anims[md["main_animation_id"]]["status"] = "ready"
    md["main_video_source"] = None
    asyncio.run(th.retry_video(run_id=871, which="main", feedback_text="slower"))
    assert benv.db.md(871)["main_video_source"] == th._still_key(benv.db.rows[871], md, 0)


def test_still_keys_tell_versions_apart():
    assert th._still_key_of(0, local_path="a/main-1.png") != th._still_key_of(0, local_path="a/main-2.png")
    assert th._still_key_of(0) is None and th._still_key_of(1) is None
    assert th._still_key_of(2, keyframe_id=555) == "scene:555"
    md = {"scene_count": 2, "scenes": [{}, {"video_source": "scene:555"}]}
    assert th._stale_for(md, 1, "scene:555") is False and th._stale_for(md, 1, "scene:556") is True
    assert th._stale_for({"main_video_source": None}, 0, "main:x|") is True


def test_undo_keeps_a_clip_stale_that_an_edit_made_stale(r2env):
    # A new Veo tier: every clip is out of date with the same stills. A refine
    # and its Undo put the clip's still back, but not its tier.
    r2env.db.add_run(204, _md(lock=False))
    _seed_old_main(r2env, 204)
    _clip_made_from_current(r2env, 204)
    asyncio.run(th.edit_run(run_id=204, video_quality="quality"))
    assert r2env.db.md(204)["main_video_stale"] is True
    asyncio.run(th.refine_still(204, "main", "warmer light"))
    asyncio.run(th.undo_still(204, "main"))
    assert r2env.db.md(204)["main_video_stale"] is True, "the clip is still on the old tier"


def test_undo_keeps_a_clip_stale_whose_words_changed(r2env):
    md = _md(scenes=2, lock=False)
    md["scenes"][1].update(keyframe_id=555)
    r2env.db.add_run(205, md)
    _seed_scene_row(r2env, 205, 1, 555)
    _clip_made_from_current(r2env, 205, 1)
    asyncio.run(th.edit_run(run_id=205, scene_dialogues=[None, "A brand new line for scene two."]))
    assert r2env.db.md(205)["scenes"][1]["video_stale"] is True
    asyncio.run(th.refine_still(205, "scene_1", "brighter"))
    asyncio.run(th.undo_still(205, "scene_1"))
    sc = r2env.db.md(205)["scenes"][1]
    assert sc["keyframe_id"] == 555 and sc["video_stale"] is True, "the clip still speaks the old line"


def test_undo_keeps_a_clip_stale_whose_words_changed_after_the_refine(r2env):
    md = _md(scenes=2, lock=False)
    md["scenes"][1].update(keyframe_id=556)
    r2env.db.add_run(206, md)
    _seed_scene_row(r2env, 206, 1, 556)
    _clip_made_from_current(r2env, 206, 1)
    asyncio.run(th.refine_still(206, "scene_1", "brighter"))
    asyncio.run(th.edit_run(run_id=206, scene_dialogues=[None, "Another line for scene two."]))
    asyncio.run(th.undo_still(206, "scene_1"))
    assert r2env.db.md(206)["scenes"][1]["video_stale"] is True
    # The scene the edit left alone keeps its record.
    assert "video_source" not in r2env.db.md(206)["scenes"][0]


def test_undo_keeps_a_clip_stale_after_a_new_topic(r2env, benv, monkeypatch):
    r2env.db.add_run(207, _md(lock=False))
    _seed_old_main(r2env, 207)
    _clip_made_from_current(r2env, 207)

    async def gpt_script(**kw):
        return dict(r2env.db.md(207).get("script") or {}, dialogue="A new line."), None

    monkeypatch.setattr(th, "_gpt_script_dict", gpt_script)
    asyncio.run(th.rescript_run(207, new_topic="Why coding bootcamps work"))
    md = r2env.db.md(207)
    assert md["main_video_stale"] is True and md["main_video_source"] is None
    asyncio.run(th.refine_still(207, "main", "warmer light"))
    asyncio.run(th.undo_still(207, "main"))
    assert r2env.db.md(207)["main_video_stale"] is True


def test_the_clips_still_follows_a_scene_count_change(r2env):
    # One scene to two: scene 0's clip keeps the still it was made from; back
    # to one: the main clip takes scene 0's record, not an older one.
    r2env.db.add_run(208, _md(lock=False))
    _seed_old_main(r2env, 208)
    _clip_made_from_current(r2env, 208)
    src = r2env.db.md(208)["main_video_source"]
    asyncio.run(th._apply_scene_count(208, 2, restore=True))
    md = r2env.db.md(208)
    assert md["scenes"][0]["video_source"] == src
    md["scenes"][0]["video_source"] = "main:remade|"
    asyncio.run(th._apply_scene_count(208, 1, restore=True))
    assert r2env.db.md(208)["main_video_source"] == "main:remade|"


# ---------------------------------------------------------------------------
# T4: a refine never hangs
# ---------------------------------------------------------------------------

def _locked_single(env, run_id: int) -> tuple[Path, Path]:
    char = env.ref_file("asset", 7)
    env.db.add_run(run_id, _md(lock=True, refs={"character_asset_id": 7}))
    return _seed_old_main(env, run_id), char


def test_a_timeout_is_asked_once_more_without_the_identity_reference(r2env, monkeypatch):
    still, char = _locked_single(r2env, 210)
    calls = _scripted_render(monkeypatch, [asyncio.TimeoutError(), _png_bytes()])
    asyncio.run(th.refine_still(210, "main", "warmer light"))
    assert [c["images"] for c in calls] == [[still, char], [still]], "the retry drops IMAGE 2"
    assert all(c["timeout"] == th.REFINE_ATTEMPT_TIMEOUT_SEC for c in calls)
    assert r2env.db.md(210)["main_retry_kind"] == "refine"


def test_no_image_is_asked_once_more_without_the_identity_reference(r2env, monkeypatch):
    still, char = _locked_single(r2env, 211)
    text = GeminiNoImageError("text", blocked=False)
    calls = _scripted_render(monkeypatch, [text, text, _png_bytes()])
    asyncio.run(th.refine_still(211, "main", "warmer light"))
    assert [len(c["images"]) for c in calls] == [2, 2, 1], "escalated once, then without IMAGE 2"


def test_the_whole_refine_is_bounded_and_says_so(r2env, monkeypatch):
    _locked_single(r2env, 212)
    monkeypatch.setattr(th, "REFINE_ATTEMPT_TIMEOUT_SEC", 0.2)
    monkeypatch.setattr(th, "REFINE_TOTAL_TIMEOUT_SEC", 0.5)

    async def hang():
        await asyncio.sleep(30)

    calls = _scripted_render(monkeypatch, [hang])
    t0 = time.monotonic()
    with pytest.raises(Exception) as e:
        asyncio.run(th.refine_still(212, "main", "warmer light"))
    assert time.monotonic() - t0 < 3.0, "never the 300 s of run 830"
    assert e.value.status_code == 504 and e.value.detail == th.REFINE_TIMEOUT_MESSAGE
    assert len(calls) == 2 and r2env.db.rows[212]["status"] == "ready"


def test_a_slow_reframe_of_the_edit_is_bounded_too(r2env, monkeypatch):
    # The edit came back square (far off 9:16): its reframe is one more image
    # call, which gets only the time left; then the edit is padded.
    _locked_single(r2env, 214)
    monkeypatch.setattr(th, "REFINE_TOTAL_TIMEOUT_SEC", 0.5)
    reframes: list[int] = []

    async def hang(*a, **k):
        reframes.append(1)
        await asyncio.sleep(30)

    monkeypatch.setattr(th, "_reframe_once", hang)
    _scripted_render(monkeypatch, [_png_bytes(size=(1024, 1024))])
    t0 = time.monotonic()
    asyncio.run(th.refine_still(214, "main", "warmer light"))
    assert time.monotonic() - t0 < 3.0, "never a reframe's 300 s after the bounded edit"
    assert reframes == [1]
    with Image.open(th._abs_from_rel(r2env.db.rows[214]["local_path"])) as im:
        w, h = im.size
    assert th._is_exact_aspect(w, h, "9:16"), (w, h)


def test_a_big_reference_is_sent_scaled_down(r2env, monkeypatch):
    big = r2env.ref_file("asset", 8, size=(3000, 2000))
    r2env.db.add_run(213, _md(lock=True, refs={"character_asset_id": 8}))
    _seed_old_main(r2env, 213)
    calls = _scripted_render(monkeypatch, [_png_bytes()])
    asyncio.run(th.refine_still(213, "main", "warmer light"))
    call, = calls
    assert call["sizes"][0] == (1248, 832), "the canvas is small enough: sent as it is"
    assert call["sizes"][1] == (1536, 1024) and call["images"][1] != big
    assert not call["images"][1].exists(), "the scaled copy is cleaned up"
    assert Image.open(big).size == (3000, 2000)


# ---------------------------------------------------------------------------
# T9: a tighter framing is a crop; a re-shoot records what it shows
# ---------------------------------------------------------------------------

def _face_still(env, run_id: int) -> Path:
    """Scene 0 is the above-the-hip speaker (a still of run 830, scaled down)."""
    p = env.tmp / "th" / str(run_id) / "main-face.png"
    p.parent.mkdir(parents=True, exist_ok=True)
    Image.open(FACE).convert("RGB").save(p)
    env.db.rows[run_id].update(local_path=str(p), storage_bucket=None,
                               storage_path=None, width=378, height=672)
    return p


def test_the_face_bands():
    assert th._framing_of_ratio(0.07) == "full_body"
    assert th._framing_of_ratio(0.166) == "above_hip"
    assert th._framing_of_ratio(0.199) == "above_hip", "run 830's model 'close-up' was not one"
    assert th._framing_of_ratio(0.33) == "close"
    assert th._is_tighter("close", "above_hip") and th._is_tighter("above_hip", "full_body")
    assert not th._is_tighter("full_body", "close") and not th._is_tighter("close", "as_photo")


def test_a_crop_to_close_up_is_head_and_shoulders_at_the_same_size():
    data = FACE.read_bytes()
    with Image.open(BytesIO(data)) as im:
        before = th._face_ratio(im.convert("RGB"))
    assert 0.12 <= before < 0.26, "the fixture is above the hip"
    out, ratio = th._tight_crop_png(data, "close", "9:16")
    with Image.open(BytesIO(out)) as im:
        im = im.convert("RGB")
        assert im.size == (378, 672)
        measured = th._face_ratio(im)
        box = th._face_box(im)
    assert 0.30 <= ratio <= 0.36 and 0.26 <= measured < 0.45
    assert box[1] / 672 > 0.12, "head room above the face"
    # Already that tight, or no face: no crop.
    assert th._tight_crop_png(out, "close", "9:16") is None
    assert th._tight_crop_png(_png_bytes((378, 672)), "close", "9:16") is None


def test_a_crop_never_enlarges_too_much(monkeypatch):
    monkeypatch.setattr(th, "_face_box", lambda im: (180.0, 200.0, 40.0, 40.0))  # face 0.06 of 672
    assert th._tight_crop_png(FACE.read_bytes(), "close", "9:16") is None, "full body to close: a blur"
    assert th._tight_crop_png(FACE.read_bytes(), "above_hip", "9:16") is not None


def test_a_tighter_framing_is_a_crop_with_no_model(r2env):
    r2env.db.add_run(220, _md(lock=False, framing="close", main_framing_used="above_hip",
                              main_keyframe_generating=True))
    old = _face_still(r2env, 220)
    _clip_made_from_current(r2env, 220)
    asyncio.run(th._run_keyframe_regen(220, [{"scene_index": 0, "reshoot": True}]))
    assert r2env.edit["calls"] == [] and r2env.gen["calls"] == [], "free: no image model"
    md, run = r2env.db.md(220), r2env.db.rows[220]
    assert md["main_framing_used"] == "close" and md["main_retry_kind"] == "reframe"
    assert md["main_video_stale"] is True and md["main_keyframe_generating"] is False
    assert run["local_path"] != str(old) and (run["width"], run["height"]) == (378, 672)
    with Image.open(run["local_path"]) as im:
        assert th._framing_of_ratio(th._face_ratio(im.convert("RGB"))) == "close"
    # Undo puts the still, its framing and the clip's freshness back.
    asyncio.run(th.undo_still(220, "main"))
    md = r2env.db.md(220)
    assert r2env.db.rows[220]["local_path"] == str(old)
    assert md["main_framing_used"] == "above_hip" and md["framing"] == "above_hip"
    assert md["main_video_stale"] is False


def test_a_wider_framing_is_a_reshoot_that_records_what_it_shows(r2env, monkeypatch):
    md = _md(scenes=2, lock=False, framing="above_hip")
    md["scenes"][1].update(keyframe_id=760, framing="full_body", framing_used="above_hip",
                           keyframe_generating=True)
    r2env.db.add_run(221, md)
    _seed_scene_row(r2env, 221, 1, 760)
    # The re-shoot came back barely wider: its face is 0.17 of the frame.
    monkeypatch.setattr(th, "_face_box", lambda im: (0.0, 0.0, 10.0, 0.17 * im.height))
    asyncio.run(th._run_keyframe_regen(221, [{"scene_index": 1, "reshoot": True}]))
    assert len(r2env.edit["calls"]) == 1, "the refine engine re-shot it"
    sc = r2env.db.md(221)["scenes"][1]
    assert sc["framing_used"] == "above_hip", "recorded as it shows, not as asked"
    assert "Full body" in sc["framing_warning"] and "Above the hip" in sc["framing_warning"]
    row = r2env.db.rows[sc["keyframe_id"]]
    assert row["metadata"]["framing_used"] == "above_hip"
    # Undo clears the warning with the still.
    asyncio.run(th.undo_still(221, "scene_1"))
    assert r2env.db.md(221)["scenes"][1]["framing_warning"] is None


def test_a_reshoot_in_its_band_has_no_warning_and_no_face_is_taken_as_asked(r2env, monkeypatch):
    r2env.db.add_run(222, _md(lock=False, framing="full_body", main_framing_used="above_hip",
                              main_keyframe_generating=True))
    _seed_old_main(r2env, 222)
    monkeypatch.setattr(th, "_face_box", lambda im: (0.0, 0.0, 10.0, 0.08 * im.height))
    asyncio.run(th._run_keyframe_regen(222, [{"scene_index": 0, "reshoot": True}]))
    md = r2env.db.md(222)
    assert md["main_framing_used"] == "full_body" and md["main_framing_warning"] is None
    # A tighter framing on a still with no face to crop around: re-shot.
    md.update(framing="close", main_keyframe_generating=True)
    monkeypatch.setattr(th, "_face_box", lambda im: None)
    asyncio.run(th._run_keyframe_regen(222, [{"scene_index": 0, "reshoot": True}]))
    md = r2env.db.md(222)
    assert len(r2env.edit["calls"]) == 2 and md["main_framing_used"] == "close"
    assert md["main_framing_warning"] is None, "nothing measured: nothing to warn about"


def test_fetch_run_sends_the_framing_warning(benv, monkeypatch):
    _stills_run(benv, 872, intro_excluded=True, outro_excluded=True)
    benv.db.md(872)["main_framing_warning"] = "Asked for Close-up; the new still looks Above the hip."
    monkeypatch.setattr(th, "fetch_run", REAL_FETCH_RUN)
    got = asyncio.run(th.fetch_run(872, with_reel=False))
    assert got["scenes"][0]["framing_warning"].startswith("Asked for Close-up")


def test_the_honest_framing_record():
    buf = BytesIO()
    shutil.copyfileobj(open(FACE, "rb"), buf)
    f_md, f_scene = th._honest_framing_md(buf.getvalue(), 0, "close")
    assert f_md["main_framing_used"] == "above_hip" and "Close-up" in f_md["main_framing_warning"]
    assert f_scene == {}
    f_md, f_scene = th._honest_framing_md(buf.getvalue(), 2, "above_hip")
    assert f_md == {"framing_used": "above_hip"}
    assert f_scene == {"framing_used": "above_hip", "framing_warning": None}
    assert chr(0x2014) not in th._honest_framing_md(buf.getvalue(), 1, "full_body")[1]["framing_warning"]


def test_round2_copy_has_no_em_dash():
    for text in (th.REFINE_TIMEOUT_MESSAGE, thp.BOOKEND_MOTION_DIRECTOR_SYSTEM,
                 thp.BOOKEND_MOTION_NEGATIVE_PROMPT, thp.BOOKEND_TEXT_CHECK_SYSTEM,
                 thp.build_bookend_motion_veo_prompt(role="outro", elements=ICONS["elements"])):
        assert chr(0x2014) not in text
