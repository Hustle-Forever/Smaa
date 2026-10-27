"""Tests for `routers/campaigns.py`, driven through the real ASGI app.

The store is monkeypatched, so these exercise routing, validation and status
codes with no database and no network. Worth doing in-process rather than as
source guards because two classes of bug are invisible to reading the file:

* **Route ordering.** FastAPI matches in declaration order, so `/summary` and
  `/review-queue` must be declared before `/{campaign_id}` or they would be
  swallowed as a campaign id.
* **Gate semantics.** A guarded store transition returning `None` has to surface
  as 409, not 200-with-null or 500.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_BACKEND = Path(__file__).resolve().parent.parent
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from fastapi.testclient import TestClient  # noqa: E402

from app.routers import campaigns as rt  # noqa: E402
from app.services import campaign_store as cs  # noqa: E402


CAMPAIGN = {
    "id": 1, "name": "Cyber Security Week", "topic_brief": "cyber security",
    "status": "active", "status_reason": None,
    "posters_per_week": 3, "videos_per_week": 2, "duration_weeks": None,
    "posting_windows": [{"weekday": 0, "time": "17:00"}], "poster_style": "voxel",
    "video_duration_sec": 6, "video_with_audio": True,
    "weekly_budget_usd": None, "notify_email": True, "cycle_index": 0,
}
ITEM = {"id": 10, "campaign_id": 1, "status": cs.ITEM_READY, "kind": "poster",
        "caption": "hello", "gen_kind": "poster", "gen_id": 55}


@pytest.fixture
def client(monkeypatch):
    """Real app + router, fake store.

    The app-wide shared-password gate is switched off here: it is middleware that
    401s every /api/* request before routing, and this suite is about the
    campaign endpoints, not authentication.
    """
    from app import access
    monkeypatch.setattr(access, "enabled", lambda: False)

    async def _get_campaign(cid):
        return dict(CAMPAIGN) if int(cid) == 1 else None

    async def _get_item(iid):
        return dict(ITEM) if int(iid) == 10 else None

    monkeypatch.setattr(rt.store, "get_campaign", _get_campaign)
    monkeypatch.setattr(rt.store, "get_item", _get_item)
    monkeypatch.setattr(rt.store, "list_campaigns", lambda **k: _async([dict(CAMPAIGN)]))
    monkeypatch.setattr(rt.store, "items_for", lambda *a, **k: _async([]))
    monkeypatch.setattr(rt.store, "spent_this_cycle", lambda *a, **k: _async(0.0))
    monkeypatch.setattr(rt.store, "awaiting_review_count", lambda: _async(4))
    monkeypatch.setattr(rt.store, "review_queue", lambda **k: _async([dict(ITEM)]))
    monkeypatch.setattr(rt.store, "create_campaign", lambda **k: _async({**CAMPAIGN, **k, "id": 2}))
    monkeypatch.setattr(rt.store, "update_campaign", lambda cid, **k: _async({**CAMPAIGN, **k}))

    from app.main import app
    return TestClient(app)


async def _async(value):
    return value


# ---------------------------------------------------------------------------
# Routing
# ---------------------------------------------------------------------------

def test_literal_paths_are_not_swallowed_by_the_id_route(client):
    """`/summary` must not be parsed as campaign_id='summary'."""
    r = client.get("/api/campaigns/summary")
    assert r.status_code == 200, r.text
    assert r.json() == {"awaiting_review": 4}

    r = client.get("/api/campaigns/review-queue")
    assert r.status_code == 200, r.text
    assert r.json()["items"][0]["id"] == 10


def test_get_campaign_returns_campaign_items_and_spend(client):
    r = client.get("/api/campaigns/1")
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body) == {"campaign", "items", "spent_this_cycle_usd"}
    assert body["campaign"]["name"] == "Cyber Security Week"


def test_unknown_campaign_is_404(client):
    assert client.get("/api/campaigns/999").status_code == 404


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def test_create_requires_a_name(client):
    r = client.post("/api/campaigns", json={"name": "   ", "posters_per_week": 3})
    assert r.status_code == 400
    assert "name" in r.json()["detail"]


def test_create_requires_some_output(client):
    r = client.post("/api/campaigns", json={
        "name": "Empty", "posters_per_week": 0, "videos_per_week": 0,
    })
    assert r.status_code == 400
    assert "at least one" in r.json()["detail"]


def test_create_rejects_unknown_style(client):
    r = client.post("/api/campaigns", json={"name": "X", "poster_style": "neon"})
    assert r.status_code == 400
    assert "poster style" in r.json()["detail"]


def test_create_rejects_a_malformed_window(client):
    r = client.post("/api/campaigns", json={
        "name": "X", "posting_windows": [{"weekday": 1, "time": "25:00"}],
    })
    assert r.status_code == 400
    assert "HH:MM" in r.json()["detail"]


def test_create_defaults_windows_when_none_given(client):
    """A campaign with no slots picked must still spread its posts out rather
    than stacking them on one day."""
    r = client.post("/api/campaigns", json={
        "name": "X", "posters_per_week": 3, "videos_per_week": 0,
    })
    assert r.status_code == 200, r.text
    windows = r.json()["posting_windows"]
    assert len(windows) == 3
    assert len({w["weekday"] for w in windows}) == 3, "must not all land on one weekday"


def test_create_returns_a_cost_estimate(client):
    """3 posters + 2 six-second videos with audio at current rates."""
    r = client.post("/api/campaigns", json={
        "name": "X", "posters_per_week": 3, "videos_per_week": 2,
        "video_duration_sec": 6, "video_with_audio": True,
    })
    est = r.json()["cycle_cost_estimate_usd"]
    # 3*0.13 + 2*(0.13 + 6*0.175) + 0.05 = 0.39 + 2.36 + 0.05
    assert est == pytest.approx(2.80, abs=0.01), est


def test_patch_rejects_a_bogus_status(client):
    r = client.patch("/api/campaigns/1", json={"status": "deleted"})
    assert r.status_code == 400
    assert "active" in r.json()["detail"]


def test_patch_with_nothing_to_do_is_rejected(client):
    assert client.patch("/api/campaigns/1", json={}).status_code == 400


def test_weekly_quota_has_a_ceiling(client):
    r = client.post("/api/campaigns", json={"name": "X", "posters_per_week": 999})
    assert r.status_code == 422, "pydantic must reject an absurd quota"


# ---------------------------------------------------------------------------
# GATE 1
# ---------------------------------------------------------------------------

def test_approve_passes_reviewer_overrides_through(client, monkeypatch):
    seen = {}

    async def _approve(item_id, *, caption=None, proposed_time=None):
        seen.update(item_id=item_id, caption=caption, proposed_time=proposed_time)
        return {**ITEM, "status": cs.ITEM_APPROVED}

    monkeypatch.setattr(rt.store, "approve", _approve)
    r = client.post("/api/campaigns/items/10/approve", json={
        "caption": "  hand-edited  ", "proposed_time": "2026-08-02T17:00:00+04:00",
    })
    assert r.status_code == 200, r.text
    assert r.json()["status"] == cs.ITEM_APPROVED
    assert seen["caption"] == "hand-edited", "caption should be trimmed"
    assert seen["proposed_time"] == "2026-08-02T17:00:00+04:00"


def test_approving_a_decided_item_is_409(client, monkeypatch):
    """The store's guard matched no row — a second click, or a stale tab."""
    monkeypatch.setattr(rt.store, "approve", lambda *a, **k: _async(None))
    r = client.post("/api/campaigns/items/10/approve", json={})
    assert r.status_code == 409
    assert "not awaiting review" in r.json()["detail"]


def test_declining_a_decided_item_is_409(client, monkeypatch):
    monkeypatch.setattr(rt.store, "decline", lambda *a, **k: _async(None))
    r = client.post("/api/campaigns/items/10/decline", json={"note": "no"})
    assert r.status_code == 409


def test_decline_defaults_to_regenerating(client, monkeypatch):
    seen = {}

    async def _decline(item_id, *, note=None, regenerate=True):
        seen.update(note=note, regenerate=regenerate)
        return {**ITEM, "status": cs.ITEM_PLANNED}

    monkeypatch.setattr(rt.store, "decline", _decline)
    client.post("/api/campaigns/items/10/decline", json={"note": "too busy"})
    assert seen == {"note": "too busy", "regenerate": True}


def test_decline_can_be_terminal(client, monkeypatch):
    seen = {}

    async def _decline(item_id, *, note=None, regenerate=True):
        seen["regenerate"] = regenerate
        return {**ITEM, "status": cs.ITEM_REJECTED}

    monkeypatch.setattr(rt.store, "decline", _decline)
    client.post("/api/campaigns/items/10/decline", json={"regenerate": False})
    assert seen["regenerate"] is False


def test_blank_decline_note_becomes_none(client, monkeypatch):
    seen = {}

    async def _decline(item_id, *, note=None, regenerate=True):
        seen["note"] = note
        return dict(ITEM)

    monkeypatch.setattr(rt.store, "decline", _decline)
    client.post("/api/campaigns/items/10/decline", json={"note": "   "})
    assert seen["note"] is None, "an empty note must not pollute the taste feedback"


def test_unknown_item_is_404(client):
    assert client.post("/api/campaigns/items/999/approve", json={}).status_code == 404


# ---------------------------------------------------------------------------
# GATE 2 must not exist in this router
# ---------------------------------------------------------------------------

def test_router_cannot_publish():
    """Publishing stays on the single pre-existing roadmap endpoint. If this
    router ever imports the Instagram client, two publishers exist and a double
    post becomes possible."""
    import ast
    import inspect
    tree = ast.parse(inspect.getsource(rt))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            imported.add(node.module or "")
            imported.update(a.name for a in node.names)
        elif isinstance(node, ast.Import):
            imported.update(a.name for a in node.names)
    assert "instagram_publish" not in imported
    assert "approved_media" not in imported, (
        "hosting media is the worker's job, not an HTTP handler's"
    )


def test_no_route_mentions_publishing():
    """No `/publish` or `/post` endpoint may appear here."""
    from app.main import app
    paths = [r.path for r in app.routes if "/api/campaigns" in r.path]
    for p in paths:
        assert "publish" not in p and not p.endswith("/post"), p


# ---------------------------------------------------------------------------
# run-cycle
# ---------------------------------------------------------------------------

def test_run_cycle_refuses_while_work_is_in_flight(client, monkeypatch):
    """Otherwise two cycles' items interleave in the review queue."""
    monkeypatch.setattr(
        rt.store, "items_for",
        lambda *a, **k: _async([{"id": 1, "status": cs.ITEM_GENERATING}]),
    )
    r = client.post("/api/campaigns/1/run-cycle")
    assert r.status_code == 409
    assert "generating" in r.json()["detail"]


def test_run_cycle_arms_the_next_tick(client, monkeypatch):
    seen = {}

    async def _update(cid, **fields):
        seen.update(fields)
        return {**CAMPAIGN, **fields}

    monkeypatch.setattr(rt.store, "update_campaign", _update)
    r = client.post("/api/campaigns/1/run-cycle")
    assert r.status_code == 200, r.text
    assert "next_cycle_at" in seen
    assert seen["status"] == cs.CAMPAIGN_ACTIVE, "a manual kick should un-pause"
    assert seen["status_reason"] is None


def test_run_cycle_refuses_archived(client, monkeypatch):
    async def _archived(cid):
        return {**CAMPAIGN, "status": cs.CAMPAIGN_ARCHIVED}

    monkeypatch.setattr(rt.store, "get_campaign", _archived)
    assert client.post("/api/campaigns/1/run-cycle").status_code == 409
