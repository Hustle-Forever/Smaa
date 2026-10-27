"""One campaign walked end to end, with every external dependency faked.

The unit tests cover each stage's decisions; this one proves the stages actually
CONNECT — that what stage 2 writes is what stage 3 reads, and so on. That is the
class of bug unit tests miss and a real run would find embarrassingly late.

The store is a small in-memory fake rather than monkeypatched call-by-call, so the
item genuinely moves through its statuses and the assertions are about observable
state, not about which mock was called.

The most important assertion is at the end of the human-approval step: after the
worker has done everything it can, the post is BOOKED and not published. Getting
to Instagram requires a human pressing Post, which is deliberately not reachable
from this module.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

_BACKEND = Path(__file__).resolve().parent.parent
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from app.services import campaign_store as cs  # noqa: E402
from app.workers import campaign_poller as cp  # noqa: E402


def run(coro):
    return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(coro)


class FakeStore:
    """In-memory stand-in with the real guard semantics on the gate transitions.

    It stands in for the whole module, so it must also expose the status
    constants the worker reads off it — and it takes them from the real module
    rather than re-declaring them, so this fake can never drift from production's
    vocabulary.
    """

    # Mirror every public constant from the real store (ITEM_*, CAMPAIGN_*, ...).
    for _name in dir(cs):
        if _name.isupper():
            locals()[_name] = getattr(cs, _name)
    del _name

    def __init__(self):
        self.campaigns = {1: {
            "id": 1, "name": "Cyber Security Week",
            "topic_brief": "cyber security for students",
            "status": "active", "posters_per_week": 2, "videos_per_week": 0,
            "duration_weeks": None, "posting_windows": [{"weekday": 0, "time": "17:00"}],
            "poster_style": "voxel", "notify_email": False, "cycle_index": 0,
            "next_cycle_at": None, "weekly_budget_usd": None,
        }}
        self.items: dict[int, dict] = {}
        self._next = 1

    # --- campaigns ---
    async def due_campaigns(self, limit=20):
        return [c for c in self.campaigns.values() if c["status"] == "active"]

    async def active_campaigns(self):
        return [c for c in self.campaigns.values() if c["status"] == "active"]

    async def get_campaign(self, cid):
        return self.campaigns.get(int(cid))

    async def update_campaign(self, cid, **f):
        self.campaigns[int(cid)].update(f)
        return self.campaigns[int(cid)]

    async def pause_campaign(self, cid, reason=None):
        return await self.update_campaign(cid, status="paused", status_reason=reason)

    async def spent_this_cycle(self, cid, cycle):
        return sum(float(i.get("cost_usd") or 0) for i in self.items.values()
                   if i["campaign_id"] == cid and i["cycle_index"] == cycle)

    async def recent_idea_titles(self, cid, limit=40):
        return [i["idea_title"] for i in self.items.values() if i.get("idea_title")]

    async def recent_decline_notes(self, cid, limit=10):
        return [i["decline_note"] for i in self.items.values() if i.get("decline_note")]

    # --- items ---
    async def items_for(self, cid, cycle_index=None, limit=200):
        return [i for i in self.items.values()
                if i["campaign_id"] == cid
                and (cycle_index is None or i["cycle_index"] == cycle_index)][:limit]

    async def items_by_status(self, status, limit=100):
        statuses = (status,) if isinstance(status, str) else tuple(status)
        return [i for i in self.items.values() if i["status"] in statuses][:limit]

    async def get_item(self, iid):
        return self.items.get(int(iid))

    async def update_item(self, iid, **f):
        self.items[int(iid)].update(f)
        return self.items[int(iid)]

    async def plan_items(self, rows):
        out = []
        for r in rows:
            key = (r["campaign_id"], r["cycle_index"], r["slot_index"])
            if any((i["campaign_id"], i["cycle_index"], i["slot_index"]) == key
                   for i in self.items.values()):
                continue          # the real UNIQUE index
            r = {**r, "id": self._next, "cost_usd": 0, "attempt": 0,
                 "gen_id": None, "gen_kind": None, "caption": None,
                 "decline_note": None, "error": None, "scheduled_post_id": None,
                 "claimed_at": None}
            self.items[self._next] = r
            out.append(r)
            self._next += 1
        return out

    async def claim_planned(self, cid):
        for i in self.items.values():
            if i["campaign_id"] == cid and i["status"] == cs.ITEM_PLANNED:
                i["status"] = cs.ITEM_GENERATING
                i["claimed_at"] = "now"
                return i
        return None

    async def mark_ready_for_review(self, iid, *, caption, gen_kind, gen_id, cost_usd=0.0):
        i = self.items[int(iid)]
        if i["status"] != cs.ITEM_GENERATING:
            return None                       # the real guard
        i.update(status=cs.ITEM_READY, caption=caption, gen_kind=gen_kind,
                 gen_id=gen_id, cost_usd=cost_usd, claimed_at=None)
        return i

    async def approve(self, iid, *, caption=None, proposed_time=None):
        i = self.items[int(iid)]
        if i["status"] != cs.ITEM_READY:
            return None                       # GATE 1 guard
        i["status"] = cs.ITEM_APPROVED
        if caption is not None:
            i["caption"] = caption
        return i

    async def decline(self, iid, *, note=None, regenerate=True, max_attempts=3):
        i = self.items[int(iid)]
        if i["status"] != cs.ITEM_READY:
            return None
        i["attempt"] = int(i.get("attempt") or 0) + 1
        retry = regenerate and i["attempt"] < max_attempts
        i.update(status=cs.ITEM_PLANNED if retry else cs.ITEM_REJECTED,
                 decline_note=note, gen_id=None, gen_kind=None, caption=None)
        return i

    async def mark_scheduled(self, iid, post_id):
        i = self.items[int(iid)]
        if i["status"] != cs.ITEM_APPROVED:
            return None
        i.update(status=cs.ITEM_SCHEDULED, scheduled_post_id=post_id)
        return i

    async def mark_from_scheduled_row(self, iid, row):
        i = self.items[int(iid)]
        if row is None:
            i.update(status=cs.ITEM_FAILED, error="the scheduled post disappeared")
            return i
        target = {"published": cs.ITEM_PUBLISHED, "failed": cs.ITEM_PUBLISH_FAILED,
                  "cancelled": cs.ITEM_CANCELLED}.get(row.get("status"))
        if target is None:
            return None
        i["status"] = target
        if target == cs.ITEM_PUBLISHED:
            i["permalink"] = row.get("permalink")
        return i

    async def record_attempt_failure(self, iid, err, max_attempts=3):
        i = self.items[int(iid)]
        i["attempt"] = int(i.get("attempt") or 0) + 1
        i.update(status=cs.ITEM_FAILED if i["attempt"] >= max_attempts else cs.ITEM_PLANNED,
                 error=err, claimed_at=None)
        return i

    async def reclaim_stale(self, minutes=15):
        return []

    async def expire_stale_reviews(self, cycle_days=7):
        return []

    async def mark_notified(self, ids):
        return None


@pytest.fixture
def world(monkeypatch):
    """A campaign, a fake store, and fakes for every provider."""
    store = FakeStore()
    monkeypatch.setattr(cp, "store", store)
    monkeypatch.setattr(cp, "_capacity_reason", lambda: None)

    # Planner: two distinct poster ideas.
    async def _plan(**kw):
        assert kw["posters"] == 2
        return [
            {"kind": "poster", "title": "The email that looks like your bank",
             "angle": "a hand hovering over a suspicious inbox",
             "key_message": "Check the sender", "cta": "Slow down", "weekday_hint": 0},
            {"kind": "poster", "title": "Your password is a sentence",
             "angle": "a chalkboard with a long passphrase",
             "key_message": "Length beats symbols", "cta": "Change one today",
             "weekday_hint": 2},
        ]
    monkeypatch.setattr(cp.planner, "plan_cycle", _plan)

    # Image pipeline: pretend each render succeeds, id 100+.
    rendered = {"n": 0}
    monkeypatch.setattr(cp.pipeline, "poster_reserve", lambda n: 20 - n)
    monkeypatch.setattr(cp.pipeline, "poster_refund", lambda n: None)

    async def _concepts(topic, n, style):
        return [{"prompt": f"scene for {topic[:20]}", "summary": "s"}], 0.01
    monkeypatch.setattr(cp.pipeline, "poster_concepts", _concepts)

    async def _batch(specs, refs, usage, **kw):
        rendered["n"] += 1
        return [{"id": 100 + rendered["n"], "status": "ready", "cost_usd": 0.14}]
    monkeypatch.setattr(cp.pipeline, "run_stage2_batch", _batch)

    # The poster store the worker polls for "is the image ready".
    import app.services.yas_local_store as ystore
    async def _get(gid):
        return {"id": gid, "status": "ready"}
    monkeypatch.setattr(ystore, "get", _get)

    # Caption service.
    async def _caption(topic, *, extra_direction=None):
        return {"caption": f"caption about {topic}", "references": [], "provider": "fake"}
    monkeypatch.setattr(cp.caption_service, "generate_caption", _caption)

    # Hosting + booking.
    import app.services.approved_media as am
    import app.services.scheduled_posts as sp
    bookings: dict[int, dict] = {}

    async def _upload(**kw):
        return f"https://cdn.example/{kw['tool']}/{kw['ref_id']}.jpg"
    monkeypatch.setattr(am, "upload_for_post", _upload)

    async def _create(**kw):
        pid = 900 + len(bookings) + 1
        bookings[pid] = {"id": pid, "status": "pending", **kw}
        return bookings[pid]
    monkeypatch.setattr(sp, "create", _create)
    monkeypatch.setattr(sp, "get", lambda pid: _get_booking(bookings, pid))

    return {"store": store, "bookings": bookings}


async def _get_booking(bookings, pid):
    return bookings.get(int(pid))


# ===========================================================================

def test_a_full_week_flows_to_the_human_gate_and_stops(world):
    store, bookings = world["store"], world["bookings"]

    # --- tick 1: plan the week ---
    run(cp._plan_due_cycles())
    items = list(store.items.values())
    assert len(items) == 2, "two posters planned"
    assert all(i["status"] == cs.ITEM_PLANNED for i in items)
    assert {i["idea_title"] for i in items} == {
        "The email that looks like your bank", "Your password is a sentence",
    }
    assert all(i["proposed_time"] for i in items), "every slot gets a time"

    # --- planning is idempotent: a second pass must not double-book ---
    run(cp._plan_due_cycles())
    assert len(store.items) == 2, "re-planning must not duplicate slots"

    # --- generate: one item per campaign per tick ---
    run(cp._generate())
    assert sum(1 for i in store.items.values() if i["gen_id"]) == 1, "one at a time"

    # --- caption the finished one → it stops at GATE 1 ---
    run(cp._caption_and_gate())
    ready = [i for i in store.items.values() if i["status"] == cs.ITEM_READY]
    assert len(ready) == 1
    assert ready[0]["caption"].startswith("caption about")
    assert ready[0]["cost_usd"] == pytest.approx(0.14)

    # --- the worker cannot move it any further on its own ---
    for _ in range(3):
        run(cp._poll_once())
    still_waiting = store.items[ready[0]["id"]]
    assert still_waiting["status"] == cs.ITEM_READY, (
        "the worker must never approve its own output — it has to wait for a human"
    )
    assert not bookings, "nothing may be booked before a human approves"


def test_human_approval_books_but_does_not_publish(world):
    store, bookings = world["store"], world["bookings"]
    run(cp._plan_due_cycles())
    run(cp._generate())
    run(cp._caption_and_gate())
    item = next(i for i in store.items.values() if i["status"] == cs.ITEM_READY)

    # --- GATE 1: a human approves (what the router does) ---
    approved = run(store.approve(item["id"]))
    assert approved["status"] == cs.ITEM_APPROVED

    # --- the worker books it ---
    run(cp._book_approved())
    booked = store.items[item["id"]]
    assert booked["status"] == cs.ITEM_SCHEDULED
    assert booked["scheduled_post_id"] in bookings

    row = bookings[booked["scheduled_post_id"]]
    assert row["status"] == "pending", "BOOKED, not published"
    assert row["media_url"].startswith("https://cdn.example/poster/")
    assert row["metadata"]["campaign_item_id"] == item["id"]
    # The reminder email at T-60 is gate 2's nudge, so it must stay enabled.
    assert "suppress_reminders" not in row["metadata"]

    # --- more ticks change nothing: publishing needs a human ---
    for _ in range(3):
        run(cp._poll_once())
    assert store.items[item["id"]]["status"] == cs.ITEM_SCHEDULED
    assert bookings[row["id"]]["status"] == "pending", (
        "the worker must never publish — a person presses Post"
    )


def test_the_worker_mirrors_a_human_post(world):
    """Once a human publishes through the roadmap endpoint, the campaign's own
    record catches up — including the permalink, which is denormalised because
    the booking row can be deleted later."""
    store, bookings = world["store"], world["bookings"]
    run(cp._plan_due_cycles())
    run(cp._generate())
    run(cp._caption_and_gate())
    item = next(i for i in store.items.values() if i["status"] == cs.ITEM_READY)
    run(store.approve(item["id"]))
    run(cp._book_approved())
    pid = store.items[item["id"]]["scheduled_post_id"]

    # A human presses Post; the roadmap endpoint publishes and marks the row.
    bookings[pid].update(status="published", permalink="https://instagram.com/p/abc")

    run(cp._mirror_and_housekeep())
    done = store.items[item["id"]]
    assert done["status"] == cs.ITEM_PUBLISHED
    assert done["permalink"] == "https://instagram.com/p/abc"


def test_a_decline_regenerates_with_the_note(world):
    store = world["store"]
    run(cp._plan_due_cycles())
    run(cp._generate())
    run(cp._caption_and_gate())
    item = next(i for i in store.items.values() if i["status"] == cs.ITEM_READY)
    first_gen = item["gen_id"]

    # --- GATE 1: the human declines with feedback ---
    declined = run(store.decline(item["id"], note="too busy", regenerate=True))
    assert declined["status"] == cs.ITEM_PLANNED
    assert declined["gen_id"] is None, "must not keep showing the rejected image"

    # The worker makes a new one, and the note reached the prompt.
    run(cp._generate())
    again = store.items[item["id"]]
    assert again["gen_id"] and again["gen_id"] != first_gen
    assert again["decline_note"] == "too busy", "feedback is kept for future planning"


def test_a_failed_render_retries_then_gives_up(world, monkeypatch):
    store = world["store"]
    run(cp._plan_due_cycles())

    async def _broken(specs, refs, usage, **kw):
        return [{"id": 1, "status": "failed", "error": "gemini said no"}]
    monkeypatch.setattr(cp.pipeline, "run_stage2_batch", _broken)

    seen = []
    for _ in range(cs.MAX_ATTEMPTS + 1):
        run(cp._generate())
        item = next(iter(store.items.values()))
        seen.append(item["status"])
    assert cs.ITEM_FAILED in seen, "must stop retrying and stop spending"
    assert "gemini said no" in (next(iter(store.items.values()))["error"] or "")


def test_a_paused_campaign_stops_producing(world):
    store = world["store"]
    run(cp._plan_due_cycles())
    run(store.pause_campaign(1, "manual"))
    before = {i["id"]: i["status"] for i in store.items.values()}
    for _ in range(3):
        run(cp._poll_once())
    after = {i["id"]: i["status"] for i in store.items.values()}
    assert before == after, "a paused campaign must not generate anything new"


def test_pausing_does_not_strand_an_already_approved_post(world):
    """Pause stops NEW work. Something the human already approved is a commitment
    and must still get booked."""
    store, bookings = world["store"], world["bookings"]
    run(cp._plan_due_cycles())
    run(cp._generate())
    run(cp._caption_and_gate())
    item = next(i for i in store.items.values() if i["status"] == cs.ITEM_READY)
    run(store.approve(item["id"]))

    run(store.pause_campaign(1, "manual"))
    run(cp._poll_once())

    assert store.items[item["id"]]["status"] == cs.ITEM_SCHEDULED, (
        "an approved post must still be booked through a pause"
    )
    assert len(bookings) == 1
