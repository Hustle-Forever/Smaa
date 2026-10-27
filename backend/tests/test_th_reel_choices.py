"""Rebrand stage, S3: a pick and an end per scene (services/th_reel).

  storage     metadata.reel_choices = {target: {src, pick, pending, end_frame,
              at}}, one jsonb_set path per target; a write for another clip
              (src) replaces the entry — the very SQL, run by a fake cursor
              that mirrors it, against the memory store
  the pick    pending once ready, else the pick, else the original — each
              version checked against the scene's clip now (options.source;
              an older version: base.clip_changed); a failed, interrupted
              or other-clip pending leaves the pick as it was
  PUT / GET   /talking-head-v2/{id}/reel/{target} {pick?, end_frame?}: the
              whole reel block back, with inputs_sha; GET …/reel
  Generate    POST /harness/versions sets `pending` (awaited, 3 s, never
              Generate's failure) and never reaches the database in a test
  orphans     a version of this server that no task runs and that hasn't
              moved for 60 s is interrupted at once
  the reel    fetch_run.reel; composition_stale by inputs_sha; Finalize
              ships the pick for as long as the end says (case D: an Omni
              version made on the untrimmed clip is cut at the flash too)

No database (the reel's store is in memory, get_conn is a tripwire), no
Remotion, no paid call.
"""
from __future__ import annotations

import asyncio
import copy
import json
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from app import db as app_db
from app.services import rebrand_pipeline as pipeline
from app.services import rebrand_store
from app.services import th_reel
from app.services import yas_talking_head_v2 as svc
from app.workers import yas_th_v2_composition_poller as poller
from test_th_reel_finalize import FakeDb, _fake_get_conn, _jsonb, _norm

RUN = 9401


def run(coro):
    return asyncio.run(coro)


class _Tripped(AssertionError):
    pass


def _iso(dt: datetime) -> str:
    return dt.isoformat()


class World:
    def __init__(self, root: Path):
        self.root = root
        self.store = th_reel.MemoryStore()
        self.facts: dict[str, th_reel.Facts] = {}
        self.frames: dict[str, int] = {}

    def clip(self, anim_id: int, *, sec: int = 8) -> Path:
        p = self.root / "yas_producer" / "animations" / f"anim-{anim_id}.mp4"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"clip")
        self.store.anim_rows[anim_id] = {"id": anim_id, "status": "ready", "local_path": str(p), "duration_sec": sec,
                                         "storage_bucket": None, "storage_path": None, "error": None}
        return p

    def version(self, vid: int, *, target: str = "scene_0", src: Any = "anim:351", status: str = "ready",
                house: bool = True, frames: int = 235, file: bool = True, updated: datetime | None = None,
                instance: str | None = None, run_id: int = RUN) -> dict:
        f = self.root / "yas_producer" / "talking-head-v2" / "rebrand" / str(vid) / "omni" / "scene_1.mp4"
        if file:
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_bytes(b"version")
            self.frames[f.name + str(vid)] = frames
        opts: dict = {"target": target}
        if house:
            opts["engine"] = "house"
        if src is not None:
            opts["source"] = {"src": src, "digest10": "0123456789"}
        if instance is not None:
            opts["instance"] = instance
        arm = {"status": "ready" if status == "ready" else "running", "final_path": str(f)}
        if house:
            arm["engine"] = "house"
        exp = {"id": vid, "run_id": run_id, "status": status, "options": opts, "arms": {"omni": arm},
               "updated_at": updated or datetime.now(timezone.utc), "error": None}
        self.store.exps[vid] = exp
        return exp

    def md(self) -> dict:
        return self.store.rows[RUN]["metadata"]


def _single_md(**kw) -> dict:
    md = {"aspect_ratio": "9:16", "scene_count": 1, "composition_status": "videos_ready",
          "main_animation_id": 351, "veo_animation_id": 351, "intro_animation_id": 352,
          "outro_animation_id": 353, "intro_excluded": False, "outro_excluded": False,
          "refs": {"intro_video_upload_id": None, "outro_video_upload_id": None,
                   "intro_asset_id": None, "outro_asset_id": None}}
    md.update(kw)
    return md


@pytest.fixture
def world(tmp_path, monkeypatch):
    root = tmp_path / "data"
    w = World(root)
    for aid, sec in ((351, 8), (352, 4), (353, 4)):
        w.clip(aid, sec=sec)
    w.store.rows[RUN] = {"id": RUN, "topic": "t", "saved_asset_id": None, "metadata": _single_md()}
    th_reel.set_store(w.store)
    monkeypatch.setattr(poller, "config", types.SimpleNamespace(asset_root=root / "assets", producer_assets_root=root / "assets" / "assets_library"))
    monkeypatch.setattr(poller, "COMP_DIR", root / "yas_producer" / "talking-head-v2")

    def trip(*_a, **_k):
        raise _Tripped("the database was reached")

    for mod in (poller, th_reel, svc, app_db, rebrand_store, pipeline):
        monkeypatch.setattr(mod, "get_conn", trip)

    async def no_lookup(*_a, **_k):
        raise _Tripped("a card/upload/asset was looked up in the database")

    for name in ("_fetch_card_path", "_fetch_upload_path", "_fetch_asset_path"):
        monkeypatch.setattr(poller, name, no_lookup)
    # anim-351 (the main) ends on a flash from source frame 188 (24 fps): the
    # reel cuts it at 235; the bookends are clean 4 s clips.
    w.facts = {"anim-351.mp4": th_reel.Facts(192, 24.0, 188), "anim-352.mp4": th_reel.Facts(96, 24.0, None),
               "anim-353.mp4": th_reel.Facts(96, 24.0, None)}
    monkeypatch.setattr(th_reel, "clip_facts", lambda p: w.facts.get(Path(p).name, th_reel.Facts(192, 24.0, None)))
    monkeypatch.setattr(th_reel, "count_frames",
                        lambda p: w.frames.get(Path(p).name + Path(p).parent.parent.name, 0))
    monkeypatch.setattr(th_reel, "_cancel_run_drafts", lambda run_id: 0)
    monkeypatch.setattr(th_reel, "_live_ids", lambda: set())
    return w


def _block(w: World) -> dict:
    return run(th_reel.reel_of(RUN))


def _t(block: dict, target: str) -> dict:
    return next(t for t in block["targets"] if t["target"] == target)


# ---------------------------------------------------------------------------
# The storage: one jsonb_set path per target, a new clip replaces the entry
# ---------------------------------------------------------------------------

class ChoiceDb(FakeDb):
    """FakeDb plus the choice statement, mirrored."""

    def execute(self, sql: str, params: Any) -> list[dict]:
        if _norm(sql) == _norm(th_reel.CHOICE_SQL):
            p = params
            row = self.rows.get(p["id"])
            if row is None or p["mode"] != th_reel.MODE_PREVIEW:
                return []
            md = row["metadata"]
            md = {**md, "reel_choices": md.get("reel_choices") or {}}
            cur = md["reel_choices"].get(p["t"])
            patch = _jsonb(p["patch"])
            new = patch if (cur or {}).get("src") != p["src"] else {**(cur or {}), **patch}
            md["reel_choices"] = {**md["reel_choices"], p["t"]: new}
            row["metadata"] = md
            return [{"reel_choices": copy.deepcopy(md["reel_choices"])}]
        return super().execute(sql, params)


def test_the_choice_sql_is_one_path_per_target():
    s = _norm(th_reel.CHOICE_SQL)
    assert "jsonb_set(metadata || jsonb_build_object('reel_choices'" in s
    assert "ARRAY['reel_choices', %(t)s::text]" in s
    assert "IS DISTINCT FROM %(src)s::text THEN %(patch)s::jsonb" in s
    assert "ELSE COALESCE(metadata->'reel_choices'->%(t)s::text, '{}'::jsonb) || %(patch)s::jsonb END" in s
    assert "WHERE id = %(id)s AND mode = %(mode)s" in s and "RETURNING metadata->'reel_choices'" in s


def test_the_sql_and_the_memory_store_agree(monkeypatch):
    base = {"id": RUN, "metadata": _single_md(composition_status="ready")}
    db = ChoiceDb({RUN: copy.deepcopy(base)})
    monkeypatch.setattr(th_reel, "get_conn", _fake_get_conn(db))
    pg, mem = th_reel.PgStore(), th_reel.MemoryStore({RUN: copy.deepcopy(base)})
    writes = [
        ("scene_0", "anim:351", {"src": "anim:351", "pick": 7, "pending": None}),
        ("scene_0", "anim:351", {"src": "anim:351", "end_frame": 200}),
        ("outro", "anim:353", {"src": "anim:353", "end_frame": 100}),
        ("scene_0", "anim:351", {"src": "anim:351", "pending": 9}),
        ("scene_0", "anim:361", {"src": "anim:361", "end_frame": 150}),     # a new clip: replaced
    ]
    for t, src, patch in writes:
        a = run(pg.set_choice(RUN, t, src, patch))
        b = run(mem.set_choice(RUN, t, src, patch))
        assert a == b
    assert db.rows[RUN]["metadata"] == mem.rows[RUN]["metadata"]
    assert mem.rows[RUN]["metadata"]["reel_choices"] == {
        "scene_0": {"src": "anim:361", "end_frame": 150},
        "outro": {"src": "anim:353", "end_frame": 100}}
    assert run(pg.set_choice(RUN + 1, "scene_0", "anim:1", {"src": "anim:1"})) is None


def test_two_targets_written_at_once_both_survive(monkeypatch):
    db = ChoiceDb({RUN: {"id": RUN, "metadata": _single_md()}})
    monkeypatch.setattr(th_reel, "get_conn", _fake_get_conn(db))
    pg = th_reel.PgStore()

    async def both():
        await asyncio.gather(pg.set_choice(RUN, "scene_0", "anim:351", {"src": "anim:351", "pick": 3}),
                             pg.set_choice(RUN, "outro", "anim:353", {"src": "anim:353", "end_frame": 90}))
        # Another writer's shallow merge (every TH v2 writer is one) leaves them.
        await pg.patch(RUN, {"composition_status": "ready"})

    run(both())
    rc = db.rows[RUN]["metadata"]["reel_choices"]
    assert rc["scene_0"]["pick"] == 3 and rc["outro"]["end_frame"] == 90


# ---------------------------------------------------------------------------
# The effective pick
# ---------------------------------------------------------------------------

def _choose(w: World, target: str = "scene_0", **entry) -> None:
    src = {"scene_0": "anim:351", "intro": "anim:352", "outro": "anim:353"}[target]
    w.md().setdefault("reel_choices", {})[target] = {"src": src, **entry}


def test_an_untouched_scene_ships_the_original(world):
    block = _block(world)
    assert block["ready"] is True and len(block["inputs_sha"]) == 16
    assert [t["target"] for t in block["targets"]] == ["intro", "scene_0", "outro"]
    main = _t(block, "scene_0")
    assert main["role"] == "main" and main["label"] == "Main" and main["src"] == "anim:351"
    assert main["pick"] is None and main["ships"] is None and main["making"] is None
    assert main["end"] == {"frame": 235, "auto": 235, "user": None, "full": 240}
    assert _t(block, "intro")["end"] == {"frame": 120, "auto": None, "user": None, "full": 120}
    assert main["thumb_url"] == f"/api/yas-producer/talking-head-v2/{RUN}/file?v=a351"


def test_a_ready_pending_ships_and_a_failed_one_leaves_the_pick(world):
    world.version(21)
    world.version(22, status="running")
    _choose(world, pick=21, pending=22)
    main = _t(_block(world), "scene_0")
    assert main["ships"] == 21 and main["making"] == 22
    assert main["pick"] == {"version_id": 22, "state": "running"}
    assert main["thumb_url"] == "/api/rebrand/harness/versions/21/still/0"
    world.store.exps[22]["status"] = "ready"
    world.store.exps[22]["arms"]["omni"]["status"] = "ready"
    main = _t(_block(world), "scene_0")
    assert main["ships"] == 22 and main["making"] is None and main["pick"] == {"version_id": 22, "state": "ready"}
    world.store.exps[22]["status"] = "failed"
    main = _t(_block(world), "scene_0")
    assert main["ships"] == 21 and main["pick"] == {"version_id": 22, "state": "failed"}
    del world.md()["reel_choices"]["scene_0"]["pick"]
    main = _t(_block(world), "scene_0")
    assert main["ships"] is None                                          # back to the original


def test_a_version_made_on_another_clip_never_ships(world):
    world.version(23, src="anim:340")                  # made on the clip before a Veo retry
    _choose(world, pick=23)
    main = _t(_block(world), "scene_0")
    assert main["ships"] is None and main["pick"] == {"version_id": 23, "state": "other_clip"}
    # … and the PUT refuses it, although the entry's src is the scene's.
    with pytest.raises(HTTPException) as err:
        run(th_reel.put_choice(RUN, "scene_0", {"pick": 23}))
    assert err.value.status_code == 409 and err.value.detail == th_reel.OLDER_CLIP


def test_an_older_version_is_checked_by_its_first_frame(world, monkeypatch):
    world.version(25, src=None, house=False)           # Omni, before options.source existed
    _choose(world, pick=25)
    from app.services.rebrand_harness import base as hbase

    monkeypatch.setattr(th_reel, "_first_frame", lambda run_id, t: Path("first.png"))
    monkeypatch.setattr(hbase, "clip_changed", lambda exp, first: False)
    assert _t(_block(world), "scene_0")["ships"] == 25
    monkeypatch.setattr(hbase, "clip_changed", lambda exp, first: True)
    assert _t(_block(world), "scene_0")["pick"]["state"] == "other_clip"


def test_a_bookend_ships_only_a_version_made_in_house(world):
    world.version(31, target="intro", src="anim:352", house=False)
    _choose(world, "intro", pick=31)
    assert _t(_block(world), "intro")["pick"]["state"] == "other_clip"
    world.version(32, target="intro", src="anim:352")
    _choose(world, "intro", pick=32)
    assert _t(_block(world), "intro")["ships"] == 32


def test_a_lost_version_is_said(world):
    world.version(24, file=False)
    _choose(world, pick=24)
    main = _t(_block(world), "scene_0")
    assert main["pick"] == {"version_id": 24, "state": "lost"} and main["ships"] == 24


def test_an_entry_for_another_clip_counts_for_nothing(world):
    world.version(21)
    world.md()["reel_choices"] = {"scene_0": {"src": "anim:340", "pick": 21, "end_frame": 100}}
    main = _t(_block(world), "scene_0")
    assert main["ships"] is None and main["end"]["user"] is None and main["end"]["frame"] == 235


# ---------------------------------------------------------------------------
# PUT and GET
# ---------------------------------------------------------------------------

def test_put_a_pick_an_end_and_back_to_the_original(world):
    world.version(21)
    world.version(22, status="running")
    _choose(world, pending=22)
    got = run(th_reel.put_choice(RUN, "scene_0", {"pick": 21}))
    assert _t(got, "scene_0")["ships"] == 21
    assert world.md()["reel_choices"]["scene_0"]["pending"] is None          # a pick clears pending
    got = run(th_reel.put_choice(RUN, "scene_0", {"end_frame": 200}))
    assert _t(got, "scene_0")["end"] == {"frame": 200, "auto": 235, "user": 200, "full": 240}
    assert world.md()["reel_choices"]["scene_0"]["pick"] == 21              # partial: the pick stays
    got = run(th_reel.put_choice(RUN, "scene_0", {"pick": None, "end_frame": None}))
    main = _t(got, "scene_0")
    assert main["ships"] is None and main["end"]["user"] is None
    assert got["inputs_sha"] == run(th_reel.reel_of(RUN))["inputs_sha"]


@pytest.mark.parametrize("fields, status", [
    ({"end_frame": 29}, 422), ({"end_frame": 241}, 422), ({"end_frame": 240}, 200), ({"end_frame": 30}, 200),
    ({"pick": 999}, 404), ({"pick": 41}, 404), ({"pick": 42}, 404), ({"pick": 43}, 409),
])
def test_put_refuses_what_isnt_this_scenes(world, fields, status):
    world.version(41, run_id=RUN + 1)                  # another run's
    world.version(42, target="scene_1")                # another scene's
    world.version(43, status="running")                # not ready
    if status == 200:
        run(th_reel.put_choice(RUN, "scene_0", fields))
        return
    with pytest.raises(HTTPException) as err:
        run(th_reel.put_choice(RUN, "scene_0", fields))
    assert err.value.status_code == status


def test_put_for_a_scene_not_in_the_reel(world):
    with pytest.raises(HTTPException) as err:
        run(th_reel.put_choice(RUN, "scene_3", {"end_frame": 100}))
    assert err.value.status_code == 404
    Path(world.store.anim_rows[351]["local_path"]).unlink()
    with pytest.raises(HTTPException) as err:
        run(th_reel.put_choice(RUN, "scene_0", {"end_frame": 100}))
    assert err.value.status_code == 409 and err.value.detail == "Main's clip isn't on this computer."


def test_the_routes(world, monkeypatch):
    from app.routers import yas_producer as yp

    world.version(21)
    app = FastAPI()
    app.include_router(yp.router)
    client = TestClient(app)
    r = client.get(f"/api/yas-producer/talking-head-v2/{RUN}/reel")
    assert r.status_code == 200 and r.json()["ready"] is True
    r = client.put(f"/api/yas-producer/talking-head-v2/{RUN}/reel/scene_0", json={"pick": 21})
    assert r.status_code == 200 and _t(r.json(), "scene_0")["ships"] == 21
    r = client.put(f"/api/yas-producer/talking-head-v2/{RUN}/reel/scene_0", json={})
    assert r.status_code == 200 and world.md()["reel_choices"]["scene_0"]["pick"] == 21     # nothing changed
    assert client.put(f"/api/yas-producer/talking-head-v2/{RUN}/reel/bogus", json={}).status_code == 422
    assert client.put(f"/api/yas-producer/talking-head-v2/{RUN}/reel/scene_0",
                      json={"end_frame": 5}).status_code == 422


# ---------------------------------------------------------------------------
# inputs_sha and staleness
# ---------------------------------------------------------------------------

def test_the_inputs_name_the_pick_and_the_end_and_ignore_an_excluded_scene(world):
    world.version(21)
    before = _block(world)["inputs_sha"]
    _choose(world, pick=21)
    picked = _block(world)["inputs_sha"]
    _choose(world, pick=21, end_frame=200)
    ended = _block(world)["inputs_sha"]
    assert len({before, picked, ended}) == 3
    state = run(th_reel.reel_state(world.store.rows[RUN]))
    body = th_reel.inputs(world.md(), state.targets, state.choices)
    assert body["segments"][1] == ["scene_0", "anim:351", False, 21, 200]
    shuffled = json.loads(json.dumps({k: body[k] for k in reversed(list(body))}))
    assert th_reel.inputs_sha(shuffled) == ended
    # A pick on an excluded bookend is not part of the reel.
    world.version(32, target="outro", src="anim:353")
    world.md()["outro_excluded"] = True
    a = _block(world)["inputs_sha"]
    _choose(world, "outro", pick=32)
    assert _block(world)["inputs_sha"] == a
    state = run(th_reel.reel_state(world.store.rows[RUN]))
    assert th_reel.inputs(world.md(), state.targets, state.choices)["segments"][2] == ["outro", None, True, None, None]


def _fetch(world, monkeypatch) -> dict:
    row = world.store.rows[RUN]

    async def fetch(run_id):
        return {"id": RUN, "topic": "t", "status": "ready", "local_path": None, "created_at": None,
                "updated_at": None, "saved_asset_id": row.get("saved_asset_id"),
                "metadata": copy.deepcopy(row["metadata"])}

    async def anim(anim_id):
        return copy.deepcopy(world.store.anim_rows.get(int(anim_id)))

    async def none(*_a, **_k):
        return None

    monkeypatch.setattr(svc, "_fetch_run_row", fetch)
    monkeypatch.setattr(svc, "_fetch_animation_row", anim)
    monkeypatch.setattr(svc, "_fetch_card_row", none)
    return run(svc.fetch_run(RUN))


def _finalized(world, monkeypatch) -> None:
    """The reel finalized and rendered once, as it is now."""
    good = world.root / "yas_producer" / "talking-head-v2" / str(RUN) / "final-1.mp4"
    good.parent.mkdir(parents=True, exist_ok=True)
    good.write_bytes(b"reel")
    sha = _block(world)["inputs_sha"]
    world.md().update({"composition_status": "ready", "render_owner": th_reel.INSTANCE_ID, "render_job_id": "j",
                       "composition_path": str(good), "composition_seq": 1, "composition_good_seq": 1,
                       "composition_good_sha": sha, "composition_inputs_sha": sha})


def test_fetch_run_carries_the_reel_and_its_staleness(world, monkeypatch):
    world.version(21)
    _finalized(world, monkeypatch)
    got = _fetch(world, monkeypatch)
    assert got["reel"] == _block(world) and got["composition_stale"] is False
    _choose(world, pick=21)                                   # a pick …
    assert _fetch(world, monkeypatch)["composition_stale"] is True
    _choose(world)                                            # … taken back
    assert _fetch(world, monkeypatch)["composition_stale"] is False
    _choose(world, end_frame=200)                             # an end
    assert _fetch(world, monkeypatch)["composition_stale"] is True
    _choose(world)
    world.md()["outro_excluded"] = True                       # a clip excluded after Final
    assert _fetch(world, monkeypatch)["composition_stale"] is True
    world.md()["outro_excluded"] = False
    world.md()["main_video_stale"] = True                     # a clip out of date: not ready, stale
    got = _fetch(world, monkeypatch)
    assert got["reel"] is None and got["reel_ready"] is False and got["composition_stale"] is True


def test_a_legacy_rows_staleness_is_todays_or_a_choice(world, monkeypatch):
    world.version(21)
    world.md().update({"composition_status": "ready", "composition_path": "yas_producer/talking-head-v2/x/final.mp4"})
    assert _fetch(world, monkeypatch)["composition_stale"] is False
    world.md()["outro_excluded"] = True                       # today's rule doesn't see it (unchanged)
    assert _fetch(world, monkeypatch)["composition_stale"] is False
    world.md()["intro_video_stale"] = True
    assert _fetch(world, monkeypatch)["composition_stale"] is True
    world.md()["intro_video_stale"] = False
    _choose(world, pick=21)                                   # any valid choice
    assert _fetch(world, monkeypatch)["composition_stale"] is True
    world.md()["reel_choices"]["scene_0"]["src"] = "anim:1"   # … for another clip: none
    assert _fetch(world, monkeypatch)["composition_stale"] is False


# ---------------------------------------------------------------------------
# Finalize: what ships, and for how long (case D)
# ---------------------------------------------------------------------------

def test_finalize_ships_the_pick_cut_at_the_end(world):
    world.version(25, frames=238)                              # made on the untrimmed clip: 238 frames
    _choose(world, pick=25)
    run(th_reel.finalize(RUN))
    plan = world.md()["composition_plan"]
    main = next(s for s in plan["segments"] if s["role"] == "main")
    assert main["version_id"] == 25 and main["frames"] == 235 and main["src"] == "anim:351"
    assert main["file_rel"].replace("\\", "/").endswith("rebrand/25/omni/scene_1.mp4")
    props = plan["payload"]["props"]
    assert props["main_video_path"].endswith("/rebrand/25/omni/scene_1.mp4")
    assert props["main_duration_sec"] == (235 - 0.1) / 30 and plan["total_frames"] == 451


def test_finalize_takes_the_users_end(world):
    world.version(26, frames=235)
    _choose(world, end_frame=200)
    run(th_reel.finalize(RUN))
    main = next(s for s in world.md()["composition_plan"]["segments"] if s["role"] == "main")
    assert main["frames"] == 200 and main["version_id"] is None and main["end"]["user"] == 200
    world.md().update({"composition_status": "ready"})
    _choose(world, pick=26, end_frame=220)                     # a version and a longer end: its own length
    run(th_reel.finalize(RUN))
    main = next(s for s in world.md()["composition_plan"]["segments"] if s["role"] == "main")
    assert main["frames"] == 220 and main["version_id"] == 26
    world.md().update({"composition_status": "ready"})
    _choose(world, pick=26, end_frame=None)
    world.version(26, frames=180)
    run(th_reel.finalize(RUN))
    main = next(s for s in world.md()["composition_plan"]["segments"] if s["role"] == "main")
    assert main["frames"] == 180                               # a shorter version: its own length


def test_finalize_ships_a_bookend_version_and_end(world):
    world.version(32, target="intro", src="anim:352", frames=119)
    _choose(world, "intro", pick=32)
    _choose(world, "outro", end_frame=90)
    run(th_reel.finalize(RUN))
    segs = {s["target"]: s for s in world.md()["composition_plan"]["segments"]}
    assert segs["intro"]["version_id"] == 32 and segs["intro"]["frames"] == 119
    assert segs["outro"]["frames"] == 90
    props = world.md()["composition_plan"]["payload"]["props"]
    assert props["intro_duration_sec"] == (119 - 0.1) / 30 and props["outro_duration_sec"] == (90 - 0.1) / 30


def test_finalize_waits_for_a_version_being_made(world):
    world.version(22, status="running")
    _choose(world, pending=22)
    with pytest.raises(HTTPException) as err:
        run(th_reel.finalize(RUN))
    assert err.value.status_code == 409 and err.value.detail == "Main is still being made."


def test_finalize_names_a_lost_version(world):
    world.version(24, file=False)
    _choose(world, pick=24)
    with pytest.raises(HTTPException) as err:
        run(th_reel.finalize(RUN))
    assert err.value.status_code == 409 and "Main" in err.value.detail and "Remake" in err.value.detail


def test_finalize_is_a_no_op_until_a_choice_changes(world):
    world.version(21)
    _finalized(world, None)
    assert run(th_reel.finalize(RUN))["rendering"] is False
    _choose(world, pick=21)
    assert run(th_reel.finalize(RUN))["rendering"] is True


# ---------------------------------------------------------------------------
# Orphans
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("seconds, mine, live, orphaned", [
    (61, True, False, True), (59, True, False, False), (61, True, True, False),
    (61, False, False, False), (11 * 60, False, False, True), (11 * 60, True, True, False),
])
def test_the_orphan_rule(seconds, mine, live, orphaned):
    now = datetime.now(timezone.utc)
    exp = {"id": 5, "status": "running", "updated_at": now - timedelta(seconds=seconds),
           "options": {"instance": th_reel.INSTANCE_ID if mine else "another-host"}}
    assert rebrand_store.is_orphaned(exp, live_ids={5} if live else set(), now=now) is orphaned
    assert rebrand_store.is_orphaned({**exp, "status": "ready"}, live_ids=set(), now=now) is False


def test_an_orphaned_pending_is_interrupted_and_the_pick_stands(world):
    world.version(21)
    world.version(22, status="running", instance=th_reel.INSTANCE_ID,
                  updated=datetime.now(timezone.utc) - timedelta(seconds=61))
    _choose(world, pick=21, pending=22)
    main = _t(_block(world), "scene_0")
    assert main["making"] is None and main["ships"] == 21 and main["pick"]["state"] == "failed"
    assert world.store.exps[22]["status"] == "interrupted"
    run(th_reel.finalize(RUN))                                  # nothing is being made any more
    assert world.md()["composition_status"] == "rendering"


def test_the_versions_list_uses_the_orphan_rule(monkeypatch):
    calls: list = []

    async def update(exp_id, **fields):
        calls.append((exp_id, fields))

    async def get(exp_id):
        return {"id": exp_id, "status": "interrupted"}

    monkeypatch.setattr(rebrand_store, "update_experiment", update)
    monkeypatch.setattr(rebrand_store, "get_experiment", get)
    exp = {"id": 8, "status": "running", "updated_at": datetime.now(timezone.utc) - timedelta(seconds=70),
           "options": {"instance": th_reel.INSTANCE_ID}}
    assert run(rebrand_store.mark_interrupted_if_orphaned(exp, live_ids=set()))["status"] == "interrupted"
    assert calls and calls[0][1]["status"] == rebrand_store.ST_INTERRUPTED


# ---------------------------------------------------------------------------
# Generate: pending set, never the database, never Generate's failure
# ---------------------------------------------------------------------------

def _harness_app(monkeypatch, *, start_exp: dict):
    from app.routers import rebrand_harness as rt
    from app.services.rebrand_harness import recipe as rc
    from app.services.rebrand_harness import studio

    async def final_patterns(run_id, target, recipe, base=None):
        return {}

    async def start(run_id, raw, recipe=None, base=None):
        return copy.deepcopy(start_exp)

    async def lib_get(slug):
        from app.services import pattern_library as lib

        return lib.BUILTINS.get(slug)

    from app.services import pattern_library as lib

    monkeypatch.setattr(lib, "get", lib_get)
    monkeypatch.setattr(studio, "final_patterns", final_patterns)
    monkeypatch.setattr(pipeline, "start", start)
    app = FastAPI()
    app.include_router(rt.router)
    body = {"run_id": RUN, "target": "scene_0", "recipe": rc.preset_42_house("scene_0").model_dump(mode="json")}
    return TestClient(app), body


_EXP = {"id": 77, "run_id": RUN, "status": "queued", "stage": "queued", "arms": {},
        "options": {"target": "scene_0", "engine": "house",
                    "source": {"src": "anim:351", "digest10": "0123456789"}, "scene_kind": "main",
                    "end_frame": None, "instance": th_reel.INSTANCE_ID}}


def test_a_versions_post_sets_pending_and_never_reaches_the_database(world, monkeypatch):
    import sys

    reached: list[str] = []

    def trip(*_a, **_k):
        reached.append("get_conn")
        raise _Tripped("the database was reached")

    for mod in list(sys.modules.values()):
        try:
            if mod is not None and hasattr(mod, "get_conn") and mod.__name__.startswith("app."):
                monkeypatch.setattr(mod, "get_conn", trip)
        except Exception:  # noqa: BLE001
            pass
    monkeypatch.setattr(app_db, "get_pool", trip)
    client, body = _harness_app(monkeypatch, start_exp=_EXP)
    r = client.post("/api/rebrand/harness/versions", json=body)
    assert r.status_code == 200, r.text
    assert reached == []
    assert world.md()["reel_choices"]["scene_0"] == {"src": "anim:351", "pending": 77,
                                                     "at": world.md()["reel_choices"]["scene_0"]["at"]}


class _Broken(th_reel.MemoryStore):
    async def set_choice(self, *a, **k):
        raise RuntimeError("the pooler said no")


class _Hanging(th_reel.MemoryStore):
    async def set_choice(self, *a, **k):
        await asyncio.sleep(10)


@pytest.mark.parametrize("store_cls", [_Broken, _Hanging])
def test_a_choice_that_fails_never_fails_generate(world, monkeypatch, store_cls):
    th_reel.set_store(store_cls(world.store.rows, world.store.anim_rows))
    monkeypatch.setattr(th_reel, "PENDING_TIMEOUT_SEC", 0.2)
    client, body = _harness_app(monkeypatch, start_exp=_EXP)
    r = client.post("/api/rebrand/harness/versions", json=body)
    assert r.status_code == 200 and r.json()["id"] == 77


def test_a_version_without_a_source_sets_nothing(world, monkeypatch):
    exp = copy.deepcopy(_EXP)
    del exp["options"]["source"]
    client, body = _harness_app(monkeypatch, start_exp=exp)
    assert client.post("/api/rebrand/harness/versions", json=body).status_code == 200
    assert "reel_choices" not in world.md()


def test_a_version_is_stamped_with_its_clip(world, monkeypatch):
    from app.services.rebrand_harness import stages

    monkeypatch.setattr(stages, "digest", lambda p: "abcdef0123456789aaaa")
    got = run(th_reel.version_options(world.store.rows[RUN], "scene_0", end_frame=None))
    assert got == {"source": {"src": "anim:351", "digest10": "abcdef0123"}, "scene_kind": "main",
                   "end_frame": None, "instance": th_reel.INSTANCE_ID}
    got = run(th_reel.version_options(world.store.rows[RUN], "outro", end_frame=90))
    assert got["source"]["src"] == "anim:353" and got["scene_kind"] == "outro" and got["end_frame"] == 90
    # A clip not on this computer: no source (the older check covers it).
    Path(world.store.anim_rows[351]["local_path"]).unlink()
    assert "source" not in run(th_reel.version_options(world.store.rows[RUN], "scene_0"))


def test_pipeline_start_stores_the_stamp_beside_the_recipe(world, monkeypatch):
    from app.services import pattern_library as lib
    from app.services.rebrand_harness import recipe as rc
    from app.services.rebrand_harness import stages
    from app.services.rebrand_harness.elements import pattern as pattern_el

    created: list[dict] = []

    async def run_row(run_id):
        return copy.deepcopy(world.store.rows[RUN])

    async def none(*_a, **_k):
        return []

    async def create(run_id, options):
        created.append(options)
        return {"id": 99, "run_id": run_id, "options": options, "status": "queued"}

    async def ran(*_a, **_k):
        return None

    async def lib_get(slug):
        return lib.BUILTINS.get(slug)

    monkeypatch.setattr(lib, "get", lib_get)
    monkeypatch.setattr(pipeline.th, "_fetch_run_row", run_row)
    monkeypatch.setattr(rebrand_store, "list_for_run", none)
    monkeypatch.setattr(rebrand_store, "create_experiment", create)
    monkeypatch.setattr(pipeline, "_run", ran)
    monkeypatch.setattr(pattern_el, "check", none)
    monkeypatch.setattr(stages, "digest", lambda p: "abcdef0123456789aaaa")
    recipe = rc.normalize(rc.preset_42_house("scene_0"))
    run(pipeline.start(RUN, None, recipe=recipe))
    opts = created[-1]
    assert opts["source"] == {"src": "anim:351", "digest10": "abcdef0123"}
    assert opts["scene_kind"] == "main" and opts["end_frame"] is None and opts["instance"] == th_reel.INSTANCE_ID
    assert rc.recipe_hash(rc.Recipe.model_validate(opts["recipe"])) == rc.recipe_hash(recipe)


def test_a_clips_facts_are_decoded_once(tmp_path, monkeypatch):
    import numpy as np

    from app.services import rebrand_media as media

    clip = tmp_path / "anim-9.mp4"
    clip.write_bytes(b"clip")
    decoded: list = []

    def diffs(p):
        decoded.append(p)
        return np.zeros(192, np.float32)

    monkeypatch.setattr(media, "_frame_diffs", diffs)
    monkeypatch.setattr(media, "video_fps", lambda p: 24.0)
    monkeypatch.setattr(media, "tail_cut", lambda p: 188)
    monkeypatch.setattr(th_reel, "_FACTS", {})
    for _ in range(3):
        assert th_reel.clip_facts(clip) == th_reel.Facts(192, 24.0, 188)
    assert len(decoded) == 1
    clip.write_bytes(b"another clip")                        # a new file: decoded again
    th_reel.clip_facts(clip)
    assert len(decoded) == 2


def test_a_list_of_runs_leaves_the_reel_out(world, monkeypatch):
    world.version(21)
    _choose(world, pick=21)
    asked: list = []
    real = world.store.experiments

    async def experiments(ids):
        asked.append(ids)
        return await real(ids)

    monkeypatch.setattr(world.store, "experiments", experiments)
    _fetch(world, monkeypatch)
    assert asked
    asked.clear()
    row = world.store.rows[RUN]

    async def fetch(run_id):
        return {"id": RUN, "topic": "t", "status": "ready", "local_path": None, "created_at": None,
                "updated_at": None, "saved_asset_id": None, "metadata": copy.deepcopy(row["metadata"])}

    monkeypatch.setattr(svc, "_fetch_run_row", fetch)
    got = run(svc.fetch_run(RUN, with_reel=False))
    assert got["reel"] is None and got["reel_ready"] is True and asked == []
