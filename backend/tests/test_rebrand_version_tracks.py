"""A version's icons on the tracks its draft had (round 3, V1).

The QA: v2's read-only timeline showed Brain on the first track and Idea on
the second, the reverse of the draft it was made from. Tracks are the
timeline's, not a layer: the frozen render list (props.resolved) has none,
and the studio stacked a version's icons as they come (Brain, then Idea: the
planner's order is their settle, then the id). Now Generate keeps each
frozen icon's track beside them (options.icon_lanes: JSON only, the recipe
and its keys do not move) whenever one is off track 0; the studio reads them
(frozenLanesOf), and works them out from the version's recipe for one made
before (frontend/scripts/check-rebrand-icon-times.mjs, on the real planner's
plans in fixtures/rebrand_icon_times.json).

No model is ever called: the plans are the planner's own, on scripted words
and a synthetic speaker track; Generate's start is faked."""
from __future__ import annotations

import asyncio

import pytest

from app.services import rebrand_pipeline as pipeline
from app.services import rebrand_store as store
from app.services import th_reel
from app.services import vertex_client
from app.services import yas_talking_head_v2 as th
from app.services.rebrand_harness import icon_feedback
from app.services.rebrand_harness import recipe as rc
from app.services.rebrand_harness import studio
from app.services.rebrand_harness.elements import icon as I
from test_icons_lanes import BRAIN_AT, IDEA_AT, TEAM_AT, qa_plan


@pytest.fixture(autouse=True)
def guard(monkeypatch):
    def refuse(name):
        async def _refuse(*a, **k):
            raise AssertionError(f"paid call to vertex_client.{name} in a test")
        return _refuse

    for name in ("gemini_multimodal", "gemini_text", "gemini_image"):
        monkeypatch.setattr(vertex_client, name, refuse(name))


def test_the_tracks_kept_beside_the_frozen_icons_are_the_drafts():
    p = qa_plan(TEAM_AT, IDEA_AT, BRAIN_AT)
    frozen = p.resolved()
    assert [r["label"] for r in frozen] == ["Team", "Brain", "Idea"], "the planner's order: settle, then id"
    by = {it["label"]: it["lane"] for it in p.public()["items"]}
    assert by == {"Team": 0, "Idea": 0, "Brain": 1}
    assert I.lanes_of(p) == [by[r["label"]] for r in frozen] == [0, 1, 0]
    # Still never in the render list itself.
    assert all("lane" not in r for r in frozen)


def test_no_tracks_for_a_plan_that_has_none():
    alone = qa_plan(TEAM_AT)
    assert [it["label"] for it in alone.items] == ["Team", "Projects"]
    assert I.lanes_of(alone) == [0, 0], "one track: all 0 (Generate keeps none)"
    kept = I.kept_plan(qa_plan(TEAM_AT, IDEA_AT, BRAIN_AT).resolved(), version_id=3, width=720, height=1280)
    assert I.lanes_of(kept) is None, "a version's own, kept as it froze them: no planner tracks"

    class Bare:
        def resolved(self):
            return []

    assert I.lanes_of(Bare()) is None
    bad = qa_plan(TEAM_AT, IDEA_AT, BRAIN_AT)
    bad.items[1] = {**bad.items[1], "lane": None}
    assert I.lanes_of(bad) is None, "one with no track: none"


def _icons_recipe() -> dict:
    d42 = rc.preset_42_default("scene_0").model_dump(mode="json")
    el = {"id": "icons", "kind": "icon", "props": {"style": "circle", "added": [TEAM_AT, IDEA_AT, BRAIN_AT]}}
    return rc.normalize({**d42, "seed": "studio:1:scene_0", "elements": [*d42["elements"], el]}).model_dump(mode="json")


def test_generate_keeps_the_tracks_when_one_is_off_track_0(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.routers import rebrand_harness as rt

    plans = {"three": qa_plan(TEAM_AT, IDEA_AT, BRAIN_AT), "one": qa_plan(TEAM_AT)}
    pick: dict = {}

    async def final_icon_plan(run_id, target, recipe, base=None, **_k):
        p = plans[pick["name"]]
        return {"state": "ready", "plan_key": p.key(), "items": []}, p

    seen: dict = {}

    async def start(run_id, raw, recipe=None, base=None, **kw):
        seen["recipe"], seen["kw"] = recipe, kw
        return {"id": 5, "run_id": run_id, "status": "queued", "options": {"target": recipe.target, **kw}}

    async def lineage(e, fetch):
        return {"base": None, "layers": [], "depth": 0}

    async def nothing(*_a, **_k):
        return None

    monkeypatch.setattr(studio, "final_icon_plan", final_icon_plan)
    monkeypatch.setattr(pipeline, "start", start)
    monkeypatch.setattr(rt, "_lineage", lineage)
    monkeypatch.setattr(icon_feedback, "after_generate", lambda *a, **k: None)
    monkeypatch.setattr(th_reel, "after_generate", nothing)
    app = FastAPI()
    app.include_router(rt.router)
    c = TestClient(app)
    url = "/api/rebrand/harness/versions"
    req = {"run_id": 1, "target": "scene_0", "recipe": _icons_recipe()}

    pick["name"] = "three"
    r = c.post(url, json={**req, "icons_plan_key": plans["three"].key()})
    assert r.status_code == 200, r.text
    assert seen["kw"] == {"icon_lanes": [0, 1, 0]}
    frozen = rc.icon_element(seen["recipe"]).props["resolved"]
    assert [x["label"] for x in frozen] == ["Team", "Brain", "Idea"] and all("lane" not in x for x in frozen)
    assert r.json()["options"]["icon_lanes"] == [0, 1, 0], "the studio reads them from the version"

    # All on track 0: nothing kept (they never overlap; one row either way),
    # the very call as before.
    pick["name"] = "one"
    r = c.post(url, json={**req, "icons_plan_key": plans["one"].key()})
    assert r.status_code == 200, r.text
    assert seen["kw"] == {}


def test_start_keeps_the_tracks_in_the_options(monkeypatch):
    created: list = []

    async def run_row(run_id):
        return {"id": run_id, "topic": "t", "metadata": {"scene_count": 1, "aspect_ratio": "9:16"}}

    async def none(*_a, **_k):
        return []

    async def create(run_id, options):
        created.append(options)
        return {"id": 6, "run_id": run_id, "options": options, "status": "queued"}

    async def no_run(*_a, **_k):
        return None

    monkeypatch.setattr(th, "_fetch_run_row", run_row)
    monkeypatch.setattr(store, "list_for_run", none)
    monkeypatch.setattr(store, "create_experiment", create)
    monkeypatch.setattr(pipeline, "_run", no_run)
    house = rc.normalize({"engine": "house", "target": "scene_0", "elements": []})
    asyncio.run(pipeline.start(815, None, recipe=house, icon_lanes=[0, 1, 0]))
    assert created[-1]["icon_lanes"] == [0, 1, 0]
    before = created[-1]["recipe"]
    asyncio.run(pipeline.start(815, None, recipe=house))
    assert "icon_lanes" not in created[-1], "none given: none kept"
    assert created[-1]["recipe"] == before, "JSON beside the recipe only: the recipe does not move"
