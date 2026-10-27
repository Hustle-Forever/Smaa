"""Tests for the competitor scan poller's automatic scan.

Until 2026-10-07 a competitor scan ran only when someone pressed Scan, so the
feed froze on the last press (18 Sep) while every account kept posting. The
poller now queues a scan of every active account itself once the last such
scan is `TREND_AUTO_SCAN_HOURS` old. These pin that rule:

  * due by the age of the last FULL scan, never a one-account scan
  * nothing while a scan is running, nothing when the hours are 0
  * asked at most every AUTO_SCAN_CHECK_SEC, not on every 15-second tick
  * an install with no competitors does not crash the loop

No database and no Instagram: the store, `start_scan` and `run_scan` are fakes.
Coroutines are driven with ``asyncio.run``, as in tests/test_trend_finder.py.
"""
from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

_BACKEND_ROOT = Path(__file__).resolve().parent.parent
if str(_BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(_BACKEND_ROOT))

from app.services import trend_finder as tf  # noqa: E402
from app.workers import trend_scan_poller as poller  # noqa: E402


def _run(coro):
    return asyncio.run(coro)


def _scan(hours_ago: float, *, scan_id: int = 1, status: str = "done") -> dict:
    stamp = datetime.now(timezone.utc) - timedelta(hours=hours_ago)
    return {"id": scan_id, "status": status, "active": status not in ("done", "failed"),
            "started_at": stamp - timedelta(minutes=4), "finished_at": stamp}


class _FakeStore:
    """The scan queue, in memory. `latest_any` is what the newest scan of any
    kind looks like; `latest_full` the newest scan of every account."""

    def __init__(self, *, latest_any=None, latest_full=None):
        self.latest_any = latest_any
        self.latest_full = latest_full
        self.queue: list[dict] = []
        self.latest_calls: list[bool] = []

    async def fail_stale_scans(self):
        return 0

    async def scan_is_active(self):
        return bool(self.latest_any and self.latest_any.get("active"))

    async def latest_scan(self, *, full_only: bool = False):
        self.latest_calls.append(full_only)
        return self.latest_full if full_only else self.latest_any

    async def pending_scans(self, limit: int = 5):
        return [s for s in self.queue if s["status"] == "queued"][:limit]

    async def claim_scan(self, scan_id: int):
        for s in self.queue:
            if s["id"] == scan_id and s["status"] == "queued":
                s["status"] = "fetching"
                return True
        return False


@pytest.fixture
def wired(monkeypatch):
    """A poller wired to fakes, with the auto check due now. Returns a handle
    holding the store, the start_scan calls and the scans that ran."""

    def _wire(*, hours=24, latest_any=None, latest_full=None, start_error=None):
        store = _FakeStore(latest_any=latest_any, latest_full=latest_full)
        started: list[dict] = []
        ran: list[tuple] = []

        async def _start_scan(competitor_ids=None, *, lookback_days=tf.DEFAULT_LOOKBACK_DAYS,
                              max_posts=tf.DEFAULT_MAX_POSTS_PER_ACCOUNT, trigger="manual"):
            if start_error is not None:
                raise start_error
            row = {"id": 99, "status": "queued",
                   "metadata": {"competitor_ids": competitor_ids, "lookback_days": lookback_days,
                                "max_posts": max_posts, "trigger": trigger}}
            started.append(row["metadata"])
            store.queue.append(row)
            return row

        async def _run_scan(scan_id, competitor_ids=None, *, lookback_days=None, max_posts=None):
            ran.append((scan_id, competitor_ids, max_posts))

        async def _covers():
            return 0

        monkeypatch.setattr(poller, "store", store)
        monkeypatch.setattr(poller, "config", SimpleNamespace(trend_auto_scan_hours=hours))
        monkeypatch.setattr(poller.trend_finder, "start_scan", _start_scan)
        monkeypatch.setattr(poller.trend_finder, "run_scan", _run_scan)
        monkeypatch.setattr(poller.news_thumbs, "backfill_competitors", _covers)
        monkeypatch.setattr(poller, "_next_auto_check", 0.0)
        monkeypatch.setattr(poller, "_running", False)
        return SimpleNamespace(store=store, started=started, ran=ran)

    return _wire


def test_an_old_full_scan_queues_an_auto_scan_and_runs_it(wired):
    w = wired(latest_any=_scan(30), latest_full=_scan(30))
    _run(poller._poll_once())
    assert w.started == [{"competitor_ids": None, "lookback_days": tf.DEFAULT_LOOKBACK_DAYS,
                          "max_posts": poller.AUTO_SCAN_MAX_POSTS, "trigger": "auto"}]
    # Queued and run on the same tick, every active account.
    assert w.ran == [(99, None, poller.AUTO_SCAN_MAX_POSTS)]


def test_no_scan_ever_queues_one(wired):
    w = wired(latest_any=None, latest_full=None)
    _run(poller._poll_once())
    assert len(w.started) == 1


def test_a_recent_full_scan_queues_nothing(wired):
    w = wired(latest_any=_scan(2), latest_full=_scan(2))
    _run(poller._poll_once())
    assert w.started == [] and w.ran == []


def test_a_one_account_scan_does_not_reset_the_clock(wired):
    # Someone rescanned one account an hour ago; every account was last read
    # 30 hours ago. The feed is still stale, so the auto scan is due.
    w = wired(latest_any=_scan(1, scan_id=2), latest_full=_scan(30))
    _run(poller._poll_once())
    assert len(w.started) == 1
    assert w.store.latest_calls == [True]


def test_nothing_is_queued_while_a_scan_is_running(wired):
    w = wired(latest_any=_scan(0.1, status="scoring"), latest_full=_scan(30))
    _run(poller._poll_once())
    assert w.started == []


def test_zero_hours_means_the_scan_button_only(wired):
    w = wired(hours=0, latest_full=_scan(500))
    _run(poller._poll_once())
    assert w.started == [] and w.store.latest_calls == []


def test_the_check_runs_at_most_every_few_minutes(wired):
    w = wired(latest_any=_scan(2), latest_full=_scan(2))
    _run(poller._poll_once())
    _run(poller._poll_once())
    _run(poller._poll_once())
    assert w.store.latest_calls == [True]          # once, not once per tick


def test_no_competitors_is_not_a_crash(wired):
    w = wired(latest_full=_scan(30), start_error=tf.TrendScanError("no active competitors"))
    _run(poller._poll_once())                      # must not raise
    assert w.started == [] and w.ran == []
    _run(poller._poll_once())                      # and is not retried every tick
    assert w.store.latest_calls == [True]
