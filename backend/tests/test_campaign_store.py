"""Tests for `campaign_store` — the autonomous-campaign state machine.

No DB and no network, matching the rest of this suite. Two techniques:

1. **A fake connection** that records the SQL and hands back canned rows. This
   exercises the real function bodies, so the guard predicates that enforce the
   human gates are actually verified rather than eyeballed.
2. **Source guards** for the invariants that are structural (which statements
   carry which WHERE clause).

The thing most worth protecting here is that a *machine cannot cross a human
gate*. Gate 1 (content approval) and gate 2 (pressing Post) are the whole point
of the feature; if a refactor turned a guarded UPDATE into an unguarded one, the
tests below are what notices.
"""
from __future__ import annotations

import asyncio
import inspect
import sys
from contextlib import asynccontextmanager
from pathlib import Path

import pytest

_BACKEND = Path(__file__).resolve().parent.parent
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from app.services import campaign_store as cs  # noqa: E402


# ---------------------------------------------------------------------------
# Fake connection: records every statement, replays queued rows
# ---------------------------------------------------------------------------

class FakeCursor:
    def __init__(self, log: list, rows: list):
        self._log, self._rows = log, rows
        self._last = None

    async def execute(self, sql, params=None):
        self._log.append((" ".join(str(sql).split()), params))
        self._last = self._rows.pop(0) if self._rows else None

    async def fetchone(self):
        return self._last

    async def fetchall(self):
        return self._last if isinstance(self._last, list) else ([] if self._last is None else [self._last])

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class FakeConn:
    def __init__(self, log: list, rows: list):
        self._log, self._rows = log, rows

    def cursor(self):
        return FakeCursor(self._log, self._rows)


@pytest.fixture
def fake_db(monkeypatch):
    """Patch campaign_store's get_conn. `rows` is a FIFO of canned results."""
    state = {"log": [], "rows": []}

    @asynccontextmanager
    async def _get_conn(*, autocommit: bool = False):
        yield FakeConn(state["log"], state["rows"])

    monkeypatch.setattr(cs, "get_conn", _get_conn)
    # Tables are "already there" — don't assert on the DDL in behaviour tests.
    monkeypatch.setattr(cs, "_table_ready", True)
    return state


def run(coro):
    return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(coro)


def sql_of(fake_db, i=0):
    return fake_db["log"][i][0]


# ---------------------------------------------------------------------------
# Vocabulary
# ---------------------------------------------------------------------------

def test_terminal_states_are_really_terminal():
    """Nothing in the module may move an item out of a terminal state."""
    assert cs.ITEM_TERMINAL == {
        cs.ITEM_PUBLISHED, cs.ITEM_REJECTED, cs.ITEM_SKIPPED,
        cs.ITEM_EXPIRED, cs.ITEM_CANCELLED,
    }
    # No terminal status may appear as the *source* predicate of a transition
    # helper (that would mean it can be left).
    for fn in (cs.approve, cs.decline, cs.mark_scheduled, cs.mark_ready_for_review):
        src = inspect.getsource(fn)
        for terminal in cs.ITEM_TERMINAL:
            assert f'"{terminal}"' not in src, f"{fn.__name__} references terminal {terminal}"


def test_in_flight_excludes_gate_states():
    """`ready_for_review` and `scheduled` are WAITING ON A HUMAN, not in flight —
    the UI must not show a spinner for them, and pause must not cancel them."""
    assert cs.ITEM_READY not in cs.ITEM_IN_FLIGHT
    assert cs.ITEM_SCHEDULED not in cs.ITEM_IN_FLIGHT


# ---------------------------------------------------------------------------
# GATE 1 — only a human leaves ready_for_review
# ---------------------------------------------------------------------------

def test_approve_is_guarded_on_ready_for_review(fake_db):
    fake_db["rows"] = [{"id": 7, "status": cs.ITEM_APPROVED}]
    out = run(cs.approve(7))
    assert out["status"] == cs.ITEM_APPROVED
    sql = sql_of(fake_db)
    assert "WHERE id = %s AND status = %s" in sql, sql
    assert fake_db["log"][0][1][-1] == cs.ITEM_READY, "approve must require ready_for_review"


def test_approve_returns_none_when_row_not_in_review(fake_db):
    """A double-click, or approving something already approved, must LOSE —
    the router turns None into a 409 instead of corrupting state."""
    fake_db["rows"] = [None]
    assert run(cs.approve(7)) is None


def test_approve_applies_reviewer_overrides(fake_db):
    fake_db["rows"] = [{"id": 7, "status": cs.ITEM_APPROVED}]
    run(cs.approve(7, caption="edited by hand", proposed_time="2026-08-01T17:00:00+04:00"))
    sql, params = fake_db["log"][0]
    assert "caption = %s" in sql
    assert "proposed_time = %s::timestamptz" in sql
    assert "edited by hand" in params


def test_decline_with_retries_left_goes_back_to_planned(fake_db):
    fake_db["rows"] = [
        {"id": 7, "status": cs.ITEM_READY, "attempt": 0},   # get_item
        {"id": 7, "status": cs.ITEM_PLANNED},               # the UPDATE
    ]
    out = run(cs.decline(7, note="too busy", regenerate=True))
    assert out["status"] == cs.ITEM_PLANNED
    params = fake_db["log"][1][1]
    assert cs.ITEM_PLANNED in params and "too busy" in params


def test_decline_without_regenerate_is_terminal(fake_db):
    fake_db["rows"] = [
        {"id": 7, "status": cs.ITEM_READY, "attempt": 0},
        {"id": 7, "status": cs.ITEM_REJECTED},
    ]
    out = run(cs.decline(7, note="off-brand", regenerate=False))
    assert out["status"] == cs.ITEM_REJECTED


def test_decline_stops_regenerating_once_attempts_are_spent(fake_db):
    """Otherwise a fussy reviewer could loop the generator forever, burning
    real provider money on every pass."""
    fake_db["rows"] = [
        {"id": 7, "status": cs.ITEM_READY, "attempt": cs.MAX_ATTEMPTS - 1},
        {"id": 7, "status": cs.ITEM_REJECTED},
    ]
    run(cs.decline(7, note="still no", regenerate=True))
    assert cs.ITEM_REJECTED in fake_db["log"][1][1]


def test_decline_refuses_items_not_awaiting_review(fake_db):
    fake_db["rows"] = [{"id": 7, "status": cs.ITEM_SCHEDULED, "attempt": 0}]
    assert run(cs.decline(7, note="nope")) is None
    assert len(fake_db["log"]) == 1, "must not attempt the UPDATE at all"


def test_decline_clears_the_rejected_generation(fake_db):
    """The regenerated item must not keep pointing at the image the human just
    rejected — otherwise the review card would show the old picture."""
    fake_db["rows"] = [
        {"id": 7, "status": cs.ITEM_READY, "attempt": 0},
        {"id": 7, "status": cs.ITEM_PLANNED},
    ]
    run(cs.decline(7, note="x"))
    sql = fake_db["log"][1][0]
    assert "gen_id = NULL" in sql and "caption = NULL" in sql


# ---------------------------------------------------------------------------
# The worker cannot forge a human decision
# ---------------------------------------------------------------------------

def test_mark_ready_for_review_is_guarded_on_generating(fake_db):
    """A slow worker finishing late must not drag an already-approved item back
    into the review queue."""
    fake_db["rows"] = [{"id": 7, "status": cs.ITEM_READY}]
    run(cs.mark_ready_for_review(7, caption="c", gen_kind="poster", gen_id=42))
    sql, params = fake_db["log"][0]
    assert "WHERE id = %s AND status = %s" in sql
    assert params[-1] == cs.ITEM_GENERATING


def _functions_referencing(const_name: str) -> list[str]:
    """Names of module-level functions whose CODE (not prose) reads a constant.

    AST-based on purpose: an earlier version of this test matched the constant's
    string value and tripped over the word appearing in a docstring.
    """
    import ast
    tree = ast.parse(inspect.getsource(cs))
    hits = []
    for node in tree.body:
        if not isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)):
            continue
        for sub in ast.walk(node):
            if isinstance(sub, ast.Name) and sub.id == const_name:
                hits.append(node.name)
                break
    return hits


def test_no_helper_can_set_published_except_the_mirror():
    """Gate 2 lives outside this module: the ONLY way an item becomes
    `published` is by mirroring a booking a human actually posted."""
    setters = _functions_referencing("ITEM_PUBLISHED")
    assert setters == ["mark_from_scheduled_row"], (
        f"{setters} reference ITEM_PUBLISHED — only the mirror may, because "
        f"publishing is a human action taken through the roadmap endpoint."
    )


def test_store_never_publishes_anything():
    """Structural: this module must not be able to talk to Instagram at all.

    Checks imports rather than words — the table name
    `scheduled_instagram_posts` legitimately appears in prose here.
    """
    import ast
    tree = ast.parse(inspect.getsource(cs))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module or "")
            imported.update(a.name for a in node.names)
    for forbidden in ("instagram_publish", "instagram_client", "approved_media",
                      "scheduled_posts", "vertex_client", "openai_client"):
        assert forbidden not in imported, (
            f"campaign_store imports {forbidden} — the store must only own rows. "
            f"Publishing, hosting and generating belong to the worker."
        )


# ---------------------------------------------------------------------------
# GATE 2 — mirroring, never acting
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("sched_status,expected", [
    ("published", cs.ITEM_PUBLISHED),
    ("failed", cs.ITEM_PUBLISH_FAILED),
    ("cancelled", cs.ITEM_CANCELLED),
])
def test_mirror_maps_booking_outcomes(fake_db, sched_status, expected):
    fake_db["rows"] = [{"id": 7, "status": expected}]
    out = run(cs.mark_from_scheduled_row(7, {
        "status": sched_status, "permalink": "https://insta/p/x",
        "published_at": None, "error": "boom",
    }))
    assert out["status"] == expected


def test_mirror_ignores_still_pending_bookings(fake_db):
    """`pending`/`reminded` mean the human hasn't pressed Post yet — the item
    must stay `scheduled`, waiting, not be touched."""
    for status in ("pending", "reminded"):
        assert run(cs.mark_from_scheduled_row(7, {"status": status})) is None
    assert fake_db["log"] == [], "must not issue any UPDATE while waiting on gate 2"


def test_mirror_treats_a_vanished_booking_as_terminal(fake_db):
    """Real case: deleting the source generation in the Library hard-deletes its
    unpublished booking, leaving the item pointing at nothing."""
    fake_db["rows"] = [{"id": 7, "status": cs.ITEM_FAILED}]
    out = run(cs.mark_from_scheduled_row(7, None))
    assert out["status"] == cs.ITEM_FAILED
    assert "disappeared" in fake_db["log"][0][1][1]


def test_published_denormalises_permalink(fake_db):
    """The booking row can be deleted later; a campaign's record of what went
    out must survive that."""
    fake_db["rows"] = [{"id": 7, "status": cs.ITEM_PUBLISHED}]
    run(cs.mark_from_scheduled_row(7, {
        "status": "published", "permalink": "https://insta/p/abc",
        "published_at": "2026-08-01T13:00:00+00:00",
    }))
    sql, params = fake_db["log"][0]
    assert "permalink = %s" in sql
    assert "https://insta/p/abc" in params


def test_mark_scheduled_requires_prior_approval(fake_db):
    """No booking without a human approval behind it."""
    fake_db["rows"] = [{"id": 7, "status": cs.ITEM_SCHEDULED}]
    run(cs.mark_scheduled(7, 99))
    assert fake_db["log"][0][1][-1] == cs.ITEM_APPROVED


# ---------------------------------------------------------------------------
# Claims and crash recovery
# ---------------------------------------------------------------------------

def test_claim_is_atomic_and_skips_locked(fake_db):
    """Two workers (a laptop and a deployment share this database) must never
    generate the same item twice."""
    fake_db["rows"] = [{"id": 7, "status": cs.ITEM_GENERATING}]
    run(cs.claim_planned(1))
    sql = sql_of(fake_db)
    assert "FOR UPDATE SKIP LOCKED" in sql
    assert "LIMIT 1" in sql
    assert "claimed_at = now()" in sql, "a claim must be timestamped for stale recovery"
    assert sql.startswith("UPDATE"), "claim must be one statement, not select-then-update"


def test_claim_returns_none_when_nothing_planned(fake_db):
    fake_db["rows"] = [None]
    assert run(cs.claim_planned(1)) is None


def test_attempt_failure_retries_then_gives_up(fake_db):
    # attempt 0 -> 1: back to planned for another go
    fake_db["rows"] = [{"id": 7, "attempt": 0}, {"id": 7, "status": cs.ITEM_PLANNED}]
    out = run(cs.record_attempt_failure(7, "gemini 502"))
    assert out["status"] == cs.ITEM_PLANNED

    # last attempt -> terminal failure, so it stops costing money
    fake_db["log"].clear()
    fake_db["rows"] = [{"id": 7, "attempt": cs.MAX_ATTEMPTS - 1}, {"id": 7, "status": cs.ITEM_FAILED}]
    out = run(cs.record_attempt_failure(7, "gemini 502"))
    assert out["status"] == cs.ITEM_FAILED


def test_reclaim_only_targets_stale_claims(fake_db):
    fake_db["rows"] = [[]]
    run(cs.reclaim_stale(minutes=15))
    sql = sql_of(fake_db)
    assert "claimed_at <" in sql and "make_interval" in sql
    assert cs.ITEM_GENERATING in fake_db["log"][0][1]


def test_expire_is_the_only_worker_exit_from_review(fake_db):
    """The worker may retire a stale slot, but it still cannot approve it."""
    fake_db["rows"] = [[]]
    run(cs.expire_stale_reviews())
    sql, params = fake_db["log"][0]
    assert cs.ITEM_EXPIRED in params and cs.ITEM_READY in params
    assert cs.ITEM_APPROVED not in params


# ---------------------------------------------------------------------------
# Planning idempotency + schema
# ---------------------------------------------------------------------------

def test_planning_is_idempotent(fake_db):
    """A retried plan, an overlapping tick, or a manual "run cycle now" must not
    double-book a slot."""
    fake_db["rows"] = [{"id": 1}, None]
    out = run(cs.plan_items([
        {"campaign_id": 1, "cycle_index": 0, "slot_index": 0, "kind": "poster", "idea": {"title": "a"}},
        {"campaign_id": 1, "cycle_index": 0, "slot_index": 1, "kind": "poster", "idea": {"title": "b"}},
    ]))
    assert len(out) == 1, "the conflicting slot must be skipped, not duplicated"
    assert "ON CONFLICT (campaign_id, cycle_index, slot_index) DO NOTHING" in sql_of(fake_db)


def test_unique_index_backs_that_guarantee():
    assert "CREATE UNIQUE INDEX IF NOT EXISTS" in cs._ITEM_SLOT_UNIQUE
    assert "(campaign_id, cycle_index, slot_index)" in cs._ITEM_SLOT_UNIQUE


def test_jsonb_columns_are_adapted():
    """dict/list values must be wrapped for JSONB or psycopg raises at runtime."""
    from psycopg.types.json import Jsonb
    assert isinstance(cs._adapt("idea", {"title": "x"}), Jsonb)
    assert isinstance(cs._adapt("posting_windows", [{"weekday": 0}]), Jsonb)
    assert cs._adapt("caption", "plain text") == "plain text"


def test_ddl_is_lazy_and_additive():
    """No migration tool exists in this repo — the schema must self-create and
    never destroy."""
    for ddl in (cs._CREATE_CAMPAIGNS, cs._CREATE_ITEMS):
        assert "CREATE TABLE IF NOT EXISTS" in ddl
    src = inspect.getsource(cs)
    for destructive in ("DROP TABLE", "DROP COLUMN", "TRUNCATE"):
        assert destructive not in src, f"{destructive} must never appear here"


def test_items_cascade_with_their_campaign():
    assert "ON DELETE CASCADE" in cs._CREATE_ITEMS


def test_reads_use_autocommit():
    """The database is a pooler in Tokyo; a read left idle-in-transaction gets
    its connection killed. Read helpers must pass autocommit=True."""
    for fn in (cs.get_campaign, cs.list_campaigns, cs.review_queue, cs.items_for,
               cs.awaiting_review_count, cs.due_campaigns, cs.spent_this_cycle):
        assert "autocommit=True" in inspect.getsource(fn), fn.__name__


def test_list_queries_are_capped():
    for fn in (cs.items_for, cs.review_queue, cs.items_by_status, cs.due_campaigns):
        assert "LIMIT" in inspect.getsource(fn), fn.__name__


def test_spend_is_derived_not_stored():
    assert "cost_usd" not in cs._CAMPAIGN_COLUMNS
    assert "SUM(cost_usd)" in inspect.getsource(cs.spent_this_cycle)
