"""Tests for four paths that shipped with no coverage at all.

Each one was found by a completeness critic asking "what did nobody read", and
each is a place where the failure is silent rather than loud:

  close_pool          the fix for a backend that would not exit
  cancel_now          the Stop button's whole server side
  _thumb_media_type   the fix for thumbnails rendering as blank tiles
  max_posts           the ceiling a person sets before every competitor scan
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import db as dbmod  # noqa: E402
from app.routers.trend_finder import _thumb_media_type  # noqa: E402
from app.services import trend_finder as tf  # noqa: E402


def _run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# close_pool: a bounded wait, because an unbounded one would not exit
# ---------------------------------------------------------------------------

class _StuckPool:
    """A pool whose close() never returns. This is not hypothetical: against the
    remote Supabase pooler, a query that cannot be cancelled ("cancellation
    timeout expired") leaves `pool.close()` waiting for ever."""

    def __init__(self):
        self.closed = False

    async def close(self):
        await asyncio.sleep(3600)
        self.closed = True


class _CleanPool:
    def __init__(self):
        self.closed = False

    async def close(self):
        self.closed = True


class _AngryPool:
    async def close(self):
        raise RuntimeError("the socket is already gone")


def _with_pool(monkeypatch, pool, timeout=0.05):
    monkeypatch.setattr(dbmod, "_pool", pool, raising=False)
    monkeypatch.setattr(dbmod, "CLOSE_POOL_TIMEOUT_SEC", timeout)


def test_a_pool_that_will_not_drain_does_not_hold_the_process(monkeypatch):
    """The whole reason this function was rewritten. Before, shutdown hung with
    the port still bound and Ctrl+C did nothing, because by then the signal
    handler was past the point of no return."""
    _with_pool(monkeypatch, _StuckPool())
    _run(dbmod.close_pool())          # returns at all == the fix works
    assert dbmod._pool is None


def test_the_pool_is_unpublished_before_the_wait_not_after(monkeypatch):
    """A slow close must not leave a caller holding a dying pool. `get_pool()`
    has to refuse for the whole duration, not only once close() returns."""
    _with_pool(monkeypatch, _StuckPool())
    _run(dbmod.close_pool())
    with pytest.raises(RuntimeError):
        dbmod.get_pool()


def test_a_healthy_pool_is_actually_closed(monkeypatch):
    pool = _CleanPool()
    _with_pool(monkeypatch, pool, timeout=5.0)
    _run(dbmod.close_pool())
    assert pool.closed is True
    assert dbmod._pool is None


def test_a_pool_that_raises_is_abandoned_rather_than_propagated(monkeypatch):
    """Shutdown is the one path with nobody left to handle an exception."""
    _with_pool(monkeypatch, _AngryPool(), timeout=5.0)
    _run(dbmod.close_pool())
    assert dbmod._pool is None


def test_closing_twice_is_a_no_op(monkeypatch):
    """lifespan teardown can run after an explicit close on some paths."""
    _with_pool(monkeypatch, _CleanPool(), timeout=5.0)
    _run(dbmod.close_pool())
    _run(dbmod.close_pool())
    assert dbmod._pool is None


# ---------------------------------------------------------------------------
# The Stop button, server side
# ---------------------------------------------------------------------------

def test_stopping_closes_the_row_even_with_no_task_to_kill(monkeypatch):
    """The COMMON case, not the edge case: the row people need cleared was
    stranded by a backend that died days ago, so there is no live task."""
    from app.workers import news_poller as np

    closed = {"n": 0}

    async def _cancel_running():
        closed["n"] += 1
        return 1

    monkeypatch.setattr(np.news_store, "cancel_running", _cancel_running)
    monkeypatch.setattr(np, "_manual_task", None, raising=False)
    monkeypatch.setattr(np, "_running", True, raising=False)

    assert _run(np.cancel_now()) is True
    assert closed["n"] == 1
    # The in-process flag has to clear too, or the next press is refused by a
    # guard whose row is already gone.
    assert np._running is False


def test_stopping_with_nothing_running_says_so_rather_than_erroring(monkeypatch):
    from app.workers import news_poller as np

    async def _none():
        return 0

    monkeypatch.setattr(np.news_store, "cancel_running", _none)
    monkeypatch.setattr(np, "_manual_task", None, raising=False)
    monkeypatch.setattr(np, "_running", False, raising=False)

    assert _run(np.cancel_now()) is False


def test_a_store_that_cannot_be_reached_still_stops_the_work(monkeypatch):
    """Killing the task is the half that stops the spending. It must not be
    undone by a failure to tidy the bookkeeping."""
    from app.workers import news_poller as np

    async def _boom():
        raise RuntimeError("db down")

    async def _forever():
        await asyncio.sleep(3600)

    async def go():
        task = asyncio.create_task(_forever())
        monkeypatch.setattr(np.news_store, "cancel_running", _boom)
        monkeypatch.setattr(np, "_manual_task", task, raising=False)
        monkeypatch.setattr(np, "_running", True, raising=False)
        out = await np.cancel_now()
        return out, task.cancelled() or task.done()

    stopped, task_done = _run(go())
    assert stopped is True and task_done is True
    assert np._running is False


# ---------------------------------------------------------------------------
# The thumbnail content type
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "name,expected",
    [
        ("a.jpg", "image/jpeg"),
        ("a.jpeg", "image/jpeg"),
        ("a.png", "image/png"),
        ("a.webp", "image/webp"),
        ("a.gif", "image/gif"),
    ],
)
def test_a_thumbnail_is_served_as_what_it_is(name, expected):
    """This was hardcoded to image/jpeg, which was true by accident while every
    thumbnail came off Instagram. The first PNG cover shipped 200 OK with PNG
    bytes labelled JPEG, and the browser refused to paint it: a card that had a
    picture rendered a blank grey tile."""
    assert _thumb_media_type(Path(name)) == expected


@pytest.mark.parametrize("name", ["a.bin", "a", "a.txt", "a.JPG"])
def test_an_unknown_extension_falls_back_rather_than_guessing_wrong(name):
    """Every cached thumbnail before this was a JPEG, so jpeg is the honest
    default. What must never happen is a non-image type on an <img> src."""
    kind = _thumb_media_type(Path(name))
    assert kind.startswith("image/")


# ---------------------------------------------------------------------------
# max_posts: the ceiling on one competitor scan
# ---------------------------------------------------------------------------

def _page(n, start=0, ts="2026-09-01T10:00:00+0000", after=None):
    media = {"data": [{"id": str(start + i), "timestamp": ts} for i in range(n)]}
    if after:
        media["paging"] = {"cursors": {"after": after}}
    return {"username": "x", "followers_count": 100, "media": media}


def _fake_ig(monkeypatch, pages):
    """Serve `pages` in order, one per fetch_account_media call."""
    calls = {"n": 0}

    async def _fetch(username, after=None, page_size=50):
        i = calls["n"]
        calls["n"] += 1
        return pages[min(i, len(pages) - 1)]

    monkeypatch.setattr(tf.ig, "fetch_account_media", _fetch)
    return calls


def test_the_cap_truncates_and_stops_asking_for_more(monkeypatch):
    """A ceiling that still paged the whole account would spend the requests it
    exists to save."""
    calls = _fake_ig(monkeypatch, [_page(50, 0, after="c1"), _page(50, 50, after="c2")])
    account, media = _run(tf.fetch_recent_media("x", max_posts=10))
    assert len(media) == 10
    assert calls["n"] == 1          # never asked for page two


def test_the_cap_keeps_the_newest_posts(monkeypatch):
    """Instagram returns newest first, so truncating the tail is what makes a
    small cap useful rather than arbitrary."""
    _fake_ig(monkeypatch, [_page(50, 0, after="c1")])
    _, media = _run(tf.fetch_recent_media("x", max_posts=3))
    assert [m["id"] for m in media] == ["0", "1", "2"]


def test_the_account_fields_survive_an_early_cap(monkeypatch):
    """The cap returns from inside the page loop. The account block is read on
    page one, and losing it would blank the follower count every scan."""
    _fake_ig(monkeypatch, [_page(50, 0, after="c1")])
    account, media = _run(tf.fetch_recent_media("x", max_posts=2))
    assert account.get("followers_count") == 100
    assert len(media) == 2


def test_an_account_with_less_than_the_cap_is_not_a_shortfall(monkeypatch):
    """A cap is a ceiling, never a target: eleven recent posts returns eleven."""
    _fake_ig(monkeypatch, [_page(11, 0)])
    _, media = _run(tf.fetch_recent_media("x", max_posts=200))
    assert len(media) == 11


def test_the_cap_reaches_the_fetch_from_the_button(monkeypatch):
    """It is threaded button -> ScanRequest -> start_scan -> metadata -> poller
    -> run_scan -> scan_one -> here. A break anywhere is a silent full scan."""
    seen = {}

    async def _fetch(username, *, lookback_days=None, max_pages=None, max_posts=None):
        seen["max_posts"] = max_posts
        return {}, []

    monkeypatch.setattr(tf, "fetch_recent_media", _fetch)

    class _Store:
        async def record_scan_result(self, *a, **k):
            return None

    monkeypatch.setattr(tf, "store", _Store())
    _run(tf.scan_one({"id": 1, "username": "x"}, max_posts=25))
    assert seen["max_posts"] == 25
