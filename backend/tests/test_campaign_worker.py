"""Tests for the campaign worker and planner.

Everything is monkeypatched — no database, no AI provider, no network. The point
is the worker's *decisions*: when it defers, when it burns a retry, when it pauses
a campaign, and above all what it refuses to do.

The tests that matter most are the ones asserting absence: the worker must not
publish, and must not approve its own output. Those are the two human gates, and
they are only real if nothing in this module can cross them.
"""
from __future__ import annotations

import asyncio
import inspect
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from fastapi import HTTPException

_BACKEND = Path(__file__).resolve().parent.parent
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from app.services import campaign_planner as planner  # noqa: E402
from app.services import campaign_store as cs  # noqa: E402
from app.workers import campaign_poller as cp  # noqa: E402


def run(coro):
    return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(coro)


async def _async(v=None):
    return v


CAMPAIGN = {
    "id": 1, "name": "Cyber Security Week", "topic_brief": "cyber security for students",
    "status": "active", "posters_per_week": 3, "videos_per_week": 0,
    "duration_weeks": None, "posting_windows": [{"weekday": 0, "time": "17:00"}],
    "poster_style": "voxel", "video_duration_sec": 6, "video_with_audio": True,
    "weekly_budget_usd": None, "notify_email": True, "cycle_index": 0,
}


# ===========================================================================
# The two gates — structural guarantees
# ===========================================================================

def _imports_of(module) -> set[str]:
    import ast
    names: set[str] = set()
    tree = ast.parse(inspect.getsource(module))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            names.add(node.module or "")
            names.update(a.name for a in node.names)
        elif isinstance(node, ast.Import):
            names.update(a.name for a in node.names)
    return names


def test_worker_cannot_publish():
    """GATE 2. Publishing must stay on the single pre-existing roadmap endpoint a
    human triggers. If the worker could publish, the whole promise breaks and a
    double post becomes possible."""
    imported = _imports_of(cp)
    assert "instagram_publish" not in imported, (
        "the campaign worker imports the Instagram publisher — it must only BOOK "
        "posts and let a human press Post."
    )
    assert "instagram_client" not in imported


def test_worker_cannot_approve_its_own_output():
    """GATE 1. `approve`/`decline` are for human requests only."""
    src = inspect.getsource(cp)
    for forbidden in ("store.approve(", "store.decline("):
        assert forbidden not in src, (
            f"the worker calls {forbidden} — content decisions belong to a person."
        )


def test_expire_is_the_only_review_exit_the_worker_uses():
    src = inspect.getsource(cp)
    assert "expire_stale_reviews" in src
    # It may only ever WRITE ready_for_review via the store's guarded helper.
    assert "ITEM_READY" not in src or "mark_ready_for_review" in src


def test_stage_order_and_isolation():
    """Every stage is wrapped so one failure can't stop the others, and the loop
    survives anything — the stock poller pattern dies permanently on an escaped
    exception, which would stop all campaigns silently."""
    src = inspect.getsource(cp._poll_once)
    assert "try:" in src and "except Exception:" in src
    for stage in ("plan", "generate", "caption", "book", "mirror"):
        assert f'"{stage}"' in src, f"stage {stage} not in the tick"
    loop_src = inspect.getsource(cp._loop)
    assert "except asyncio.CancelledError:" in loop_src and "raise" in loop_src
    assert "except Exception:" in loop_src


# ===========================================================================
# Capacity guards — never starve a human
# ===========================================================================

class TestCapacity:
    """Nothing holds a campaign back any more.

    The local image cap went first, and the provider budget went with it: the
    meter counts spend but no longer refuses work, so `_capacity_reason` has
    nothing left to say. These use the REAL `VertexCostMeter` rather than a stub
    with two attributes on it, because the whole question is what that class
    does, and a stub would have gone on passing after the class changed
    underneath it. That is exactly what happened to the version of this test
    that these replace.
    """

    def _meter(self, monkeypatch, *, spent, budget, enforced):
        from app.services.vertex_client import VertexCostMeter
        m = VertexCostMeter(spent_usd=spent, budget_usd=budget, enforced=enforced)
        monkeypatch.setattr(cp, "get_meter", lambda: m)
        return m

    def test_no_deferral_when_nothing_is_spent(self, monkeypatch):
        self._meter(monkeypatch, spent=0.0, budget=200.0, enforced=False)
        assert cp._capacity_reason() is None

    def test_no_image_cap_can_defer_it(self, monkeypatch):
        """However many images have just been made, capacity says yes."""
        from app.services import poster_pipeline as pp
        self._meter(monkeypatch, spent=0.0, budget=200.0, enforced=False)
        pp.poster_reserve(10_000)
        assert cp._capacity_reason() is None

    def test_a_spent_budget_no_longer_defers_anything(self, monkeypatch):
        """THE CHANGE. At 96 of 100 this used to return a reason and pause
        generation. The cap is off, so a campaign runs regardless."""
        self._meter(monkeypatch, spent=96.0, budget=100.0, enforced=False)
        assert cp._capacity_reason() is None

    def test_wildly_over_still_does_not_defer(self, monkeypatch):
        self._meter(monkeypatch, spent=10_000.0, budget=10.0, enforced=False)
        assert cp._capacity_reason() is None

    def test_enforcing_the_meter_brings_the_deferral_back(self, monkeypatch):
        """The switch back is one flag, and this proves the wiring under it
        still works, so turning a real cap on later is a one-line change rather
        than an archaeology exercise."""
        self._meter(monkeypatch, spent=96.0, budget=100.0, enforced=True)
        reason = cp._capacity_reason()
        assert reason and "budget" in reason

    def test_enforcing_with_room_to_spare_still_allows_it(self, monkeypatch):
        self._meter(monkeypatch, spent=10.0, budget=100.0, enforced=True)
        assert cp._capacity_reason() is None


def test_generation_checks_capacity_before_claiming(monkeypatch):
    """Deferring must cost nothing: no row moved, no attempt burned."""
    claimed = []
    monkeypatch.setattr(cp, "_capacity_reason", lambda: "no room")
    monkeypatch.setattr(cp.store, "claim_planned", lambda cid: claimed.append(cid) or _async(None))
    run(cp._generate_one(dict(CAMPAIGN)))
    assert claimed == [], "must not claim an item it cannot generate"


def test_a_429_requeues_without_burning_an_attempt(monkeypatch):
    """A provider rate limit is not the item's fault, so it goes back to
    'planned' untouched."""
    updates, failures = [], []
    monkeypatch.setattr(cp, "_capacity_reason", lambda: None)
    monkeypatch.setattr(cp.store, "claim_planned",
                        lambda cid: _async({"id": 9, "kind": "poster", "idea": {"angle": "a"},
                                            "idea_title": "t", "campaign_id": 1}))
    async def _rate_limited(*a, **k):
        raise HTTPException(429, "rate limited upstream")
    monkeypatch.setattr(cp.pipeline, "poster_concepts", _rate_limited)
    monkeypatch.setattr(cp.store, "update_item",
                        lambda iid, **k: updates.append(k) or _async({}))
    monkeypatch.setattr(cp.store, "record_attempt_failure",
                        lambda iid, e: failures.append(e) or _async({}))
    run(cp._generate_one(dict(CAMPAIGN)))
    assert failures == [], "a rate limit must not count as a failed attempt"
    assert updates and updates[0]["status"] == cs.ITEM_PLANNED


def test_a_503_pauses_the_campaign(monkeypatch):
    """Provider budget exhausted: pause with a reason the card can show, rather
    than failing item after item."""
    paused = []
    monkeypatch.setattr(cp, "_capacity_reason", lambda: None)
    monkeypatch.setattr(cp.store, "claim_planned",
                        lambda cid: _async({"id": 9, "kind": "poster", "idea": {"angle": "a"},
                                            "idea_title": "t", "campaign_id": 1}))
    monkeypatch.setattr(cp.pipeline, "poster_reserve", lambda n: 10)
    monkeypatch.setattr(cp.pipeline, "poster_refund", lambda n: None)
    async def _boom(*a, **k):
        raise HTTPException(503, "OpenAI budget exhausted")
    monkeypatch.setattr(cp.pipeline, "poster_concepts", _boom)
    monkeypatch.setattr(cp.store, "update_item", lambda iid, **k: _async({}))
    monkeypatch.setattr(cp.store, "pause_campaign",
                        lambda cid, reason=None: paused.append(reason) or _async({}))
    run(cp._generate_one(dict(CAMPAIGN)))
    assert paused and "budget" in paused[0]


def test_weekly_budget_pauses_the_campaign(monkeypatch):
    paused = []
    monkeypatch.setattr(cp, "_capacity_reason", lambda: None)
    monkeypatch.setattr(cp.store, "spent_this_cycle", lambda *a, **k: _async(12.0))
    monkeypatch.setattr(cp.store, "pause_campaign",
                        lambda cid, reason=None: paused.append(reason) or _async({}))
    claimed = []
    monkeypatch.setattr(cp.store, "claim_planned",
                        lambda cid: claimed.append(1) or _async(None))
    run(cp._generate_one({**CAMPAIGN, "weekly_budget_usd": 10.0}))
    assert paused and "budget" in paused[0]
    assert claimed == [], "must stop before spending more"


def test_a_decline_note_is_fed_into_the_next_attempt(monkeypatch):
    """The taste-feedback loop: a declined item's note must reach Stage 1, or the
    campaign would regenerate the same thing the reviewer just rejected."""
    seen = {}
    monkeypatch.setattr(cp, "_capacity_reason", lambda: None)
    monkeypatch.setattr(cp.store, "claim_planned", lambda cid: _async({
        "id": 9, "kind": "poster", "campaign_id": 1, "idea_title": "Phishing",
        "idea": {"angle": "an inbox", "title": "Phishing"},
        "decline_note": "too busy and the headline buries the point",
    }))
    monkeypatch.setattr(cp.pipeline, "poster_reserve", lambda n: 10)

    async def _concepts(topic, n, style):
        seen["topic"] = topic
        return [{"prompt": "p", "summary": "s"}], 0.01

    monkeypatch.setattr(cp.pipeline, "poster_concepts", _concepts)
    monkeypatch.setattr(cp.pipeline, "run_stage2_batch",
                        lambda *a, **k: _async([{"id": 77, "status": "ready", "cost_usd": 0.14}]))
    monkeypatch.setattr(cp.store, "update_item", lambda iid, **k: _async({}))
    run(cp._generate_one(dict(CAMPAIGN)))
    assert "too busy" in seen["topic"], "the reviewer's note must shape the retry"


def test_video_items_are_left_alone_until_phase_3(monkeypatch):
    """Better to requeue than to fail an item for a capability that isn't built."""
    updates = []
    monkeypatch.setattr(cp, "_capacity_reason", lambda: None)
    monkeypatch.setattr(cp.store, "claim_planned",
                        lambda cid: _async({"id": 9, "kind": "video", "campaign_id": 1,
                                            "idea": {}, "idea_title": "v"}))
    monkeypatch.setattr(cp.store, "update_item",
                        lambda iid, **k: updates.append(k) or _async({}))
    run(cp._generate_one(dict(CAMPAIGN)))
    assert updates and updates[0]["status"] == cs.ITEM_PLANNED


# ===========================================================================
# Booking (stage 4) — books, never posts
# ===========================================================================

def test_booking_does_not_suppress_reminders(monkeypatch):
    """The existing T-60 reminder email is exactly the nudge gate 2 needs, so
    campaign bookings must NOT set suppress_reminders."""
    created = {}
    monkeypatch.setattr(cp.store, "items_by_status", lambda s, **k: _async([{
        "id": 9, "campaign_id": 1, "gen_id": 77, "gen_kind": "poster",
        "caption": "hi", "proposed_time": "2026-08-03T17:00:00+04:00",
    }]) if s == cs.ITEM_APPROVED else _async([]))

    import app.services.approved_media as am
    import app.services.scheduled_posts as sp
    monkeypatch.setattr(am, "upload_for_post", lambda **k: _async("https://cdn/x.jpg"))

    async def _create(**kw):
        created.update(kw)
        return {"id": 555}

    monkeypatch.setattr(sp, "create", _create)
    monkeypatch.setattr(cp.store, "mark_scheduled", lambda i, p: _async({"id": i}))
    run(cp._book_approved())
    assert created["media_url"] == "https://cdn/x.jpg"
    assert "suppress_reminders" not in (created.get("metadata") or {})
    assert created["metadata"]["campaign_item_id"] == 9
    assert created["timezone"] == cp.CAMPAIGN_TZ


def test_deleted_media_fails_the_item_terminally(monkeypatch):
    """Deleting the poster from the Library before it's booked is unrecoverable —
    retrying can only fail the same way."""
    updates = []
    monkeypatch.setattr(cp.store, "items_by_status", lambda s, **k: _async([{
        "id": 9, "campaign_id": 1, "gen_id": 77, "gen_kind": "poster", "caption": "hi",
    }]) if s == cs.ITEM_APPROVED else _async([]))

    import app.services.approved_media as am
    async def _gone(**k):
        raise FileNotFoundError("no bytes")
    monkeypatch.setattr(am, "upload_for_post", _gone)
    monkeypatch.setattr(cp.store, "update_item",
                        lambda iid, **k: updates.append(k) or _async({}))
    run(cp._book_approved())
    assert updates and updates[0]["status"] == cs.ITEM_FAILED
    assert "deleted" in updates[0]["error"]


def test_transient_hosting_failure_retries(monkeypatch):
    """A bucket hiccup should be retried next tick, not fail the post."""
    updates = []
    monkeypatch.setattr(cp.store, "items_by_status", lambda s, **k: _async([{
        "id": 9, "campaign_id": 1, "gen_id": 77, "gen_kind": "poster", "caption": "hi",
    }]) if s == cs.ITEM_APPROVED else _async([]))

    import app.services.approved_media as am
    async def _flaky(**k):
        raise RuntimeError("503 from storage")
    monkeypatch.setattr(am, "upload_for_post", _flaky)
    monkeypatch.setattr(cp.store, "update_item",
                        lambda iid, **k: updates.append(k) or _async({}))
    run(cp._book_approved())
    assert updates and "status" not in updates[0], "must stay 'approved' and retry"
    assert "host" in updates[0]["error"]


# ===========================================================================
# Slot scheduling
# ===========================================================================

def test_slot_times_land_on_the_configured_windows():
    start = datetime(2026, 8, 1, 9, 0, tzinfo=ZoneInfo(cp.CAMPAIGN_TZ))  # a Saturday
    windows = [{"weekday": 0, "time": "17:00"}, {"weekday": 2, "time": "12:30"}]
    times = cp._slot_times(windows, 2, start=start)
    assert times[0].weekday() == 0 and (times[0].hour, times[0].minute) == (17, 0)
    assert times[1].weekday() == 2 and (times[1].hour, times[1].minute) == (12, 30)
    assert all(t > start for t in times), "every slot must be in the future"


def test_slot_times_do_not_collide_when_slots_exceed_windows():
    """4 posts a week through 2 windows must spread over two weeks, not double-book."""
    start = datetime(2026, 8, 1, 9, 0, tzinfo=ZoneInfo(cp.CAMPAIGN_TZ))
    times = cp._slot_times([{"weekday": 0, "time": "17:00"}, {"weekday": 3, "time": "17:00"}],
                           4, start=start)
    assert len(set(times)) == 4, "slots must be distinct"
    assert times == sorted(times)


def test_slot_times_survive_a_campaign_with_no_windows():
    start = datetime(2026, 8, 1, 9, 0, tzinfo=ZoneInfo(cp.CAMPAIGN_TZ))
    times = cp._slot_times([], 3, start=start)
    assert len(set(times)) == 3


# ===========================================================================
# Notifications
# ===========================================================================

def test_review_email_never_implies_auto_posting():
    """The existing reminder copy says "review & approve" about an already-booked
    post. This email is about UNAPPROVED content, so it must be explicit."""
    subject, text, html = cp._build_review_ready({"id": 1, "name": "Cyber Week"}, 3)
    assert "3" in subject and "Cyber Week" in subject
    for body in (text, html):
        assert "until you approve" in body
    assert "#campaigns/1" in text


def test_notification_is_marked_when_email_is_unconfigured(monkeypatch):
    """Otherwise every tick would retry forever on a box with no SMTP."""
    marked = []
    import app.services.email_service as es
    monkeypatch.setattr(es, "email_configured", lambda: False)
    monkeypatch.setattr(cp.store, "get_campaign", lambda cid: _async(dict(CAMPAIGN)))
    monkeypatch.setattr(cp.store, "mark_notified",
                        lambda ids: marked.extend(ids) or _async(None))
    run(cp._notify_review_ready({1: [9, 10]}))
    assert marked == [9, 10]


def test_notification_respects_the_campaign_toggle(monkeypatch):
    marked = []
    import app.services.email_service as es
    monkeypatch.setattr(es, "email_configured", lambda: True)
    monkeypatch.setattr(cp.store, "get_campaign",
                        lambda cid: _async({**CAMPAIGN, "notify_email": False}))
    monkeypatch.setattr(cp.store, "mark_notified",
                        lambda ids: marked.extend(ids) or _async(None))
    run(cp._notify_review_ready({1: [9]}))
    assert marked == [], "notifications off means no email and no stamp"


# ===========================================================================
# Planner
# ===========================================================================

def test_planner_message_carries_every_signal():
    msg = planner.build_planner_message(
        brief="cyber security", posters=3, videos=1, week_number=4,
        brand_block="BRAND REQUIREMENTS: teal", exemplars_block="REAL POSTS: x",
        used_titles=["The email that looks like your bank"],
        decline_notes=["too busy"],
    )
    assert "cyber security" in msg
    assert "WEEK 4" in msg
    assert "3 poster(s) and 1 video(s)" in msg
    assert "BRAND REQUIREMENTS" in msg and "REAL POSTS" in msg
    assert "looks like your bank" in msg, "the avoid-list must be present"
    assert "too busy" in msg, "reviewer feedback must be present"


def test_planner_system_prompt_forbids_repeats_and_pins_the_shape():
    sys_p = planner.PLANNER_SYSTEM
    assert "GENUINELY DIFFERENT" in sys_p
    assert "forbidden territory" in sys_p
    assert '"slots"' in sys_p
    assert "weekday_hint" in sys_p


@pytest.mark.parametrize("raw", [
    '{"slots":[{"kind":"poster","title":"A","angle":"a scene"}]}',
    '```json\n{"slots":[{"kind":"poster","title":"A","angle":"a scene"}]}\n```',
    '[{"kind":"poster","title":"A","angle":"a scene"}]',
    'Sure! {"slots":[{"kind":"poster","title":"A","angle":"a scene"}]} Hope that helps.',
    '{"posts":[{"kind":"poster","title":"A","angle":"a scene"}]}',
])
def test_planner_parses_the_shapes_models_actually_return(raw):
    slots = planner.parse_slots(raw, posters=1, videos=0)
    assert len(slots) == 1 and slots[0]["title"] == "A"


def test_planner_drops_unusable_slots():
    """A slot with no angle can't be rendered, so it must not reach the worker."""
    raw = '{"slots":[{"kind":"poster","title":"A","angle":""},' \
          '{"kind":"poster","title":"","angle":"x"},' \
          '{"kind":"poster","title":"C","angle":"a real scene"}]}'
    slots = planner.parse_slots(raw, posters=3, videos=0)
    assert [s["title"] for s in slots] == ["C"]


def test_planner_honours_the_requested_mix():
    raw = ('{"slots":[' + ",".join(
        f'{{"kind":"poster","title":"P{i}","angle":"a"}}' for i in range(5)
    ) + "," + ",".join(
        f'{{"kind":"video","title":"V{i}","angle":"a"}}' for i in range(3)
    ) + "]}")
    slots = planner.parse_slots(raw, posters=2, videos=1)
    assert sum(1 for s in slots if s["kind"] == "poster") == 2
    assert sum(1 for s in slots if s["kind"] == "video") == 1


def test_planner_returns_nothing_for_garbage():
    for raw in ("", "not json at all", "{", "null"):
        assert planner.parse_slots(raw, posters=2, videos=0) == []


def test_planner_clamps_a_bad_weekday_hint():
    raw = '{"slots":[{"kind":"poster","title":"A","angle":"a","weekday_hint":99}]}'
    assert planner.parse_slots(raw, posters=1, videos=0)[0]["weekday_hint"] is None


def test_topic_for_leads_with_the_visual_angle():
    """Stage 1 needs to know what to DRAW; the angle must come first."""
    topic = planner.poster_topic_for({
        "title": "Phishing", "angle": "a hand hovering over a suspicious email",
        "key_message": "Check the sender", "cta": "Slow down before you click",
    })
    assert topic.startswith("a hand hovering")
    assert "Check the sender" in topic and "Slow down" in topic


def test_caption_direction_excludes_the_visual_angle():
    """The caption shouldn't describe the picture — the reader can see it."""
    d = planner.caption_direction_for({
        "angle": "a hand hovering over an email", "key_message": "Check the sender",
        "cta": "Slow down",
    })
    assert "Check the sender" in d and "Slow down" in d
    assert "hovering" not in d
