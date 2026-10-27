"""Rebrand stage: a main scene trimmed at its head (the timeline's start grip).

  storage     reel_choices[target].start_frame: a main scene's start in the
              reel's 30 fps frames from its clip's first (None or absent: no
              trim); within 2 frames of the first frame it is stored as None
  PUT         {start_frame?} beside {end_frame?}: a key left out is kept, an
              explicit null clears; 0 <= start <= end - 30 (end: the user's,
              else automatic, else the whole clip), and whichever of the two
              moves, the scene keeps 30 frames between them (else 422); an
              intro or outro has no start
  the block   end.start only when one is set (an untrimmed scene's block is
              the one it always was)
  inputs_sha  a scene with no start hashes exactly as before starts existed
              (no finalized reel goes out of date); a start moves it
  the stitch  the template skips the start (trim_before_frames on its
              multi-scene entry, main_trim_before_frames on the single-scene
              template) and plays end - start, as seconds_for(F): the payload
              of a reel with no start is today's, byte for byte, and the
              frames the plan counts are calculateMetadata's

No database (the reel's store is in memory, get_conn is a tripwire), no
Remotion, no paid call.
"""
from __future__ import annotations

import asyncio
import copy
import hashlib
import json

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from app.services import th_reel
from test_th_reel_choices import RUN, _block, _choose, _fetch, _finalized, _t, run, world  # noqa: F401 — the fixture
from test_th_reel_legacy_golden import CASES, _dump, normalise
from test_th_reel_plan import GoldenIo, root  # noqa: F401 — the fixture

# The world's single-scene reel (intro anim-352, main anim-351, outro
# anim-353; 9:16) as every stored sha was made of it: untouched, and its
# main ended at 200 by hand.
SHA_UNTOUCHED = "4fa9cb48adb32cd7"
SHA_END_200 = "69849a0cdd45238d"


def _put(**fields) -> dict:
    return run(th_reel.put_choice(RUN, "scene_0", fields))


def _refused(target: str = "scene_0", **fields) -> HTTPException:
    with pytest.raises(HTTPException) as err:
        run(th_reel.put_choice(RUN, target, fields))
    return err.value


def _stored(w, target: str = "scene_0") -> dict:
    return (w.md().get("reel_choices") or {}).get(target) or {}


def _inputs_before_starts(md: dict, targets: list, choices: dict) -> dict:
    """inputs() exactly as it was before a scene could start late: the shape
    every stored inputs_sha (composition_good_sha, Finalize's expect_sha) was
    made of."""
    rows = []
    for t in targets:
        c = choices.get(t.target)
        if t.excluded:
            rows.append([t.target, None, True, None, None])
        else:
            rows.append([t.target, t.src, False, c.ships if c else None, c.user_end if c else None])
    return {"segments": rows, "aspect": th_reel.aspect_of(md), "template": th_reel.template_of(md)}


def _sha_before_starts(w) -> str:
    state = run(th_reel.reel_state(w.store.rows[RUN]))
    return th_reel.inputs_sha(_inputs_before_starts(w.md(), state.targets, state.choices))


def _main_seg(w) -> dict:
    return next(s for s in w.md()["composition_plan"]["segments"] if s["role"] == "main")


def _again(w) -> None:
    """Finalize again (the last one left the row rendering)."""
    w.md().update({"composition_status": "ready"})


# ---------------------------------------------------------------------------
# PUT: what a start may be
# ---------------------------------------------------------------------------

def test_put_a_start_keep_it_and_clear_it(world):
    got = _put(start_frame=60)
    main = _t(got, "scene_0")
    assert main["end"] == {"frame": 235, "auto": 235, "user": None, "full": 240, "start": 60}
    assert _stored(world)["start_frame"] == 60 and _stored(world)["src"] == "anim:351"
    # A body without start_frame leaves it where it is (an end, a pick).
    got = _put(end_frame=200)
    assert _t(got, "scene_0")["end"] == {"frame": 200, "auto": 235, "user": 200, "full": 240, "start": 60}
    world.version(21)
    got = _put(pick=21)
    assert _t(got, "scene_0")["end"]["start"] == 60 and _t(got, "scene_0")["ships"] == 21
    # An explicit null clears it: the block is the one it always was.
    got = _put(start_frame=None)
    assert _t(got, "scene_0")["end"] == {"frame": 200, "auto": 235, "user": 200, "full": 240}
    assert _stored(world)["start_frame"] is None and _stored(world)["end_frame"] == 200


@pytest.mark.parametrize("start", [0, 1, 2])
def test_a_start_on_or_just_after_the_first_frame_is_no_trim(world, start):
    _put(start_frame=90)
    got = _put(start_frame=start)
    assert "start" not in _t(got, "scene_0")["end"]
    assert _stored(world)["start_frame"] is None


def test_three_frames_in_is_a_start(world):
    assert _t(_put(start_frame=3), "scene_0")["end"]["start"] == 3


@pytest.mark.parametrize("end, ok, bad", [
    (None, 205, 206),          # automatic: the flash cut at 235
    (200, 170, 171),           # set by hand
    (240, 210, 211),           # the whole clip
])
def test_a_start_keeps_a_second_before_the_end(world, end, ok, bad):
    if end is not None:
        _put(end_frame=end)
    err = _refused(start_frame=bad)
    assert err.status_code == 422 and err.detail.startswith(f"The start must be between 0 and {ok} frames")
    assert "start_frame" not in _stored(world)                       # nothing written
    assert _t(_put(start_frame=ok), "scene_0")["end"]["start"] == ok


def test_the_end_moving_into_the_start_is_refused(world):
    _put(start_frame=150)
    err = _refused(end_frame=179)
    assert err.status_code == 422 and err.detail == "The end must be at least 30 frames after the start (frame 150)."
    assert _stored(world).get("end_frame") is None
    assert _t(_put(end_frame=180), "scene_0")["end"]["user"] == 180
    # Back to automatic (235) is room enough; so is the whole clip.
    assert _t(_put(end_frame=None), "scene_0")["end"]["frame"] == 235
    # A start where the automatic end would leave less than a second: the
    # end can't go back to automatic until the start moves.
    _put(end_frame=240)
    _put(start_frame=210)
    err = _refused(end_frame=None)
    assert err.status_code == 422 and "after the start (frame 210)" in err.detail
    assert _stored(world)["end_frame"] == 240


def test_an_end_and_a_start_in_one_body_are_checked_together(world):
    _put(start_frame=150)
    # The end alone would be refused against the stored start; with the
    # start moved in the same body it fits.
    assert _refused(end_frame=100).status_code == 422
    got = _put(end_frame=100, start_frame=40)
    assert _t(got, "scene_0")["end"]["user"] == 100 and _t(got, "scene_0")["end"]["start"] == 40
    err = _refused(end_frame=120, start_frame=100)
    assert err.status_code == 422 and err.detail.startswith("The start must be between 0 and 90 frames")


def test_a_bookend_has_no_start(world):
    err = _refused("intro", start_frame=30)
    assert err.status_code == 422 and err.detail.startswith("Only a scene's start can be moved: Intro")
    run(th_reel.put_choice(RUN, "intro", {"start_frame": None}))     # clearing is harmless
    # One stored by hand (an older page, another tool) is never applied.
    world.md()["reel_choices"]["intro"] = {"src": "anim:352", "start_frame": 60}
    assert "start" not in _t(_block(world), "intro")["end"]
    assert _block(world)["inputs_sha"] == SHA_UNTOUCHED


def test_a_negative_start_is_refused(world):
    err = _refused(start_frame=-5)
    assert err.status_code == 422 and err.detail == "The start can't be before the clip's first frame."


def test_a_start_made_on_another_clip_counts_for_nothing(world):
    world.md()["reel_choices"] = {"scene_0": {"src": "anim:340", "start_frame": 120, "end_frame": 140}}
    main = _t(_block(world), "scene_0")
    assert "start" not in main["end"] and main["end"]["user"] is None
    # … nor against a new end (it would be refused at 140 - 120 = 20).
    assert _t(_put(end_frame=60), "scene_0")["end"]["user"] == 60
    assert _stored(world) == {"src": "anim:351", "at": _stored(world)["at"], "end_frame": 60}


def test_the_route_takes_start_frame(world):
    from app.routers import yas_producer as yp

    app = FastAPI()
    app.include_router(yp.router)
    client = TestClient(app)
    url = f"/api/yas-producer/talking-head-v2/{RUN}/reel/scene_0"
    r = client.put(url, json={"start_frame": 90})
    assert r.status_code == 200 and _t(r.json(), "scene_0")["end"]["start"] == 90
    r = client.put(url, json={"end_frame": 200})                     # no start_frame: kept
    assert r.status_code == 200 and _t(r.json(), "scene_0")["end"]["start"] == 90
    assert client.put(url, json={"start_frame": -1}).status_code == 422
    assert client.put(url, json={"start_frame": 901}).status_code == 422
    assert client.put(url, json={"start_frame": 171}).status_code == 422      # 200 - 30 = 170 at most
    r = client.put(url, json={"start_frame": None})
    assert r.status_code == 200 and "start" not in _t(r.json(), "scene_0")["end"]
    assert world.md()["reel_choices"]["scene_0"]["end_frame"] == 200


# ---------------------------------------------------------------------------
# inputs_sha: unchanged without a start
# ---------------------------------------------------------------------------

def test_a_reel_with_no_start_hashes_as_it_always_did(world):
    assert _block(world)["inputs_sha"] == SHA_UNTOUCHED == _sha_before_starts(world)
    _choose(world, end_frame=200)
    assert _block(world)["inputs_sha"] == SHA_END_200 == _sha_before_starts(world)
    # A start set and cleared again leaves no trace in the hash.
    _put(start_frame=60)
    assert _block(world)["inputs_sha"] not in (SHA_END_200, _sha_before_starts(world))
    _put(start_frame=None)
    assert _block(world)["inputs_sha"] == SHA_END_200
    # Every other shape the rows take (a pick, an excluded bookend) too.
    world.version(21)
    _choose(world, pick=21)
    world.md()["outro_excluded"] = True
    assert _block(world)["inputs_sha"] == _sha_before_starts(world)


def test_a_start_is_the_rows_sixth_field(world):
    _choose(world, start_frame=60, end_frame=200)
    state = run(th_reel.reel_state(world.store.rows[RUN]))
    body = th_reel.inputs(world.md(), state.targets, state.choices)
    assert body["segments"][1] == ["scene_0", "anim:351", False, None, 200, 60]
    assert body["segments"][0] == ["intro", "anim:352", False, None, None]
    moved = _block(world)["inputs_sha"]
    _choose(world, start_frame=61, end_frame=200)
    assert _block(world)["inputs_sha"] != moved


def test_a_start_makes_the_reel_out_of_date_and_clearing_it_does_not(world, monkeypatch):
    _finalized(world, monkeypatch)
    assert _fetch(world, monkeypatch)["composition_stale"] is False
    _choose(world, start_frame=60)
    assert _fetch(world, monkeypatch)["composition_stale"] is True
    _choose(world, start_frame=None)
    assert _fetch(world, monkeypatch)["composition_stale"] is False


# ---------------------------------------------------------------------------
# The stitch: the single-scene template
# ---------------------------------------------------------------------------

def test_finalize_without_a_start_sends_no_trim(world):
    run(th_reel.finalize(RUN))
    plan = world.md()["composition_plan"]
    assert "main_trim_before_frames" not in plan["payload"]["props"]
    assert "start" not in _main_seg(world)


def test_finalize_skips_the_start_and_plays_to_the_end(world):
    _choose(world, start_frame=60)
    run(th_reel.finalize(RUN))
    plan = world.md()["composition_plan"]
    props = plan["payload"]["props"]
    main = _main_seg(world)
    # Automatic end 235 (the flash cut), from frame 60: 175 frames.
    assert main["frames"] == 175 and main["start"] == 60
    assert props["main_trim_before_frames"] == 60
    assert props["main_duration_sec"] == th_reel.seconds_for(175) == (175 - 0.1) / 30
    # The key goes last; everything else is where it always was.
    assert list(props)[-1] == "main_trim_before_frames"
    assert plan["total_frames"] == th_reel.expected_frames(plan["payload"]) == 120 + 175 + 120 - 24


def test_finalize_takes_the_start_with_the_users_end(world):
    _choose(world, start_frame=45, end_frame=200)
    run(th_reel.finalize(RUN))
    main = _main_seg(world)
    assert main["frames"] == 155 and main["start"] == 45 and main["end"]["user"] == 200
    props = world.md()["composition_plan"]["payload"]["props"]
    assert props["main_duration_sec"] == (155 - 0.1) / 30 and props["main_trim_before_frames"] == 45


def test_a_picked_version_is_trimmed_as_the_original(world):
    world.version(26, frames=180)                               # shorter than the end: its own length
    _choose(world, pick=26, start_frame=60)
    run(th_reel.finalize(RUN))
    main = _main_seg(world)
    assert main["version_id"] == 26 and main["frames"] == 120 and main["start"] == 60
    assert world.md()["composition_plan"]["payload"]["props"]["main_trim_before_frames"] == 60
    # A version too short for the start: never played past its own end.
    _again(world)
    world.version(26, frames=70)
    run(th_reel.finalize(RUN))
    main = _main_seg(world)
    assert main["frames"] == th_reel.MIN_FRAMES and main["start"] == 40


def test_a_start_moves_finalize_off_its_no_op(world):
    _finalized(world, None)
    assert run(th_reel.finalize(RUN))["rendering"] is False
    _choose(world, start_frame=60)
    assert run(th_reel.finalize(RUN))["rendering"] is True


# ---------------------------------------------------------------------------
# The stitch: the multi-scene template
# ---------------------------------------------------------------------------

def _multi(root, choices=None) -> th_reel.Plan:
    spec = CASES["multi"]
    io = GoldenIo(spec["world"], root)
    io.flash = {}
    return asyncio.run(th_reel.resolve(copy.deepcopy(spec["run"]), io, choices=choices))


def test_a_multi_scene_entry_trims_only_its_own_scene(root):
    plain = _multi(root)
    trimmed = _multi(root, {"scene_1": th_reel.Choice("scene_1", user_start=45)})
    scenes = trimmed.payload["props"]["scenes"]
    # Scene 2's clip decodes as 240 frames: it plays 45..240.
    assert scenes[1]["trim_before_frames"] == 45
    assert scenes[1]["duration_sec"] == th_reel.seconds_for(195)
    assert list(scenes[1]) == ["path", "duration_sec", "trim_before_frames"]
    # Every other key of the body is the untrimmed plan's, byte for byte.
    a, b = normalise(plain.payload, root=root), normalise(trimmed.payload, root=root)
    b["props"]["scenes"][1] = a["props"]["scenes"][1]
    assert _dump(a) == _dump(b)
    assert all("trim_before_frames" not in s for s in plain.payload["props"]["scenes"])
    # The frames the plan counts are the template's own.
    frames = [s.frames for s in trimmed.segments]
    assert frames == [120, 240, 195, 120, 120]
    assert trimmed.total_frames == th_reel.expected_frames(trimmed.payload) == sum(frames) - 12 * 4
    seg = next(s for s in trimmed.segments if s.target == "scene_1")
    assert seg.public()["start"] == 45
    assert "start" not in next(s for s in trimmed.segments if s.target == "scene_0").public()


def test_a_reel_with_no_start_sends_todays_payload(root):
    """The goldens' own check, again through the start-aware code: a choice
    with no start sends what an untouched plan sends."""
    plain = _multi(root)
    none = _multi(root, {"scene_1": th_reel.Choice("scene_1")})
    assert _dump(plain.payload) == _dump(none.payload)
    assert json.dumps(plain.payload).find("trim_before") == -1


def test_the_seconds_survive_a_start_through_the_templates_rounding():
    """end - start through seconds_for: Math.round gives back the frames,
    calculateMetadata the reel's (a mirror over every start of a scene)."""
    for end in (60, 235, 240, 900):
        for start in range(0, end - th_reel.MIN_FRAMES + 1, 7):
            f = end - start
            sec = th_reel.seconds_for(f)
            assert th_reel.js_round(sec * 30) == f
            body = {"template": th_reel.TEMPLATE_MULTI, "props": {
                "intro_video_path": "i", "outro_video_path": None, "fade_sec": 0.4, "intro_duration_sec": 4.0,
                "scenes": [{"path": "a", "duration_sec": sec, "trim_before_frames": start}]}}
            assert th_reel.expected_frames(body) == 120 + f - 12


def test_the_pinned_hashes_are_the_old_formula():
    """The two literals above are sha256 over the old rows' JSON (a guard on
    the guard: neither the hash nor the rows' shape moved)."""
    body = {"segments": [["intro", "anim:352", False, None, None], ["scene_0", "anim:351", False, None, None],
                         ["outro", "anim:353", False, None, None]],
            "aspect": "9:16", "template": th_reel.TEMPLATE_SINGLE}
    raw = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    assert hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16] == SHA_UNTOUCHED == th_reel.inputs_sha(body)
