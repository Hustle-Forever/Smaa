"""Tests for ``app/services/news_virality.py`` - scoring the stories from before.

Offline: the store and the model are both fakes. What is guarded is the shape
of the catch-up: only rows the model answered for are written, a model that is
unavailable stops the pass rather than raising, and a store that cannot be read
is a quiet zero.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services import news_search as ns  # noqa: E402
from app.services import news_virality as nv  # noqa: E402


def _run(coro):
    return asyncio.run(coro)


class _Store:
    def __init__(self, rows):
        self.rows = rows
        self.saved: list[tuple[int, int, str]] = []

    async def news_without_virality(self, limit=20):
        return self.rows[:limit]

    async def set_virality(self, post_id, score, reason):
        self.saved.append((post_id, score, reason))


ROWS = [
    {"id": 11, "caption": "Schools ban AI", "metadata": {"why_it_matters": "Big."}},
    {"id": 12, "caption": "A quiet tool update", "metadata": {}},
    {"id": 13, "caption": "AI tutor beats teachers", "metadata": None},
]


def test_only_the_stories_the_model_answered_for_are_written(monkeypatch):
    store = _Store(ROWS)
    monkeypatch.setattr(nv, "store", store)
    seen = {}

    async def fake(rows):
        seen["rows"] = rows
        return {0: (88, "a fight everyone has an opinion on"), 2: (70, "")}
    monkeypatch.setattr(nv.news_search, "rate_virality", fake)

    assert _run(nv.backfill()) == 2
    assert store.saved == [(11, 88, "a fight everyone has an opinion on"), (13, 70, "")]
    assert seen["rows"][0] == {"headline": "Schools ban AI", "why": "Big."}
    assert seen["rows"][2] == {"headline": "AI tutor beats teachers", "why": ""}


def test_nothing_to_score_makes_no_model_call(monkeypatch):
    monkeypatch.setattr(nv, "store", _Store([]))

    async def never(rows):
        raise AssertionError("the model was called for an empty batch")
    monkeypatch.setattr(nv.news_search, "rate_virality", never)
    assert _run(nv.backfill()) == 0


def test_an_unavailable_model_is_a_warning_not_a_crash(monkeypatch):
    monkeypatch.setattr(nv, "store", _Store(ROWS))

    async def down(rows):
        raise ns.SearchUnavailable("billing is off")
    monkeypatch.setattr(nv.news_search, "rate_virality", down)
    assert _run(nv.backfill()) == 0


def test_a_store_that_cannot_be_read_is_a_quiet_zero(monkeypatch):
    class _Broken:
        async def news_without_virality(self, limit=20):
            raise RuntimeError("db is down")
    monkeypatch.setattr(nv, "store", _Broken())
    assert _run(nv.backfill()) == 0


def test_one_row_that_cannot_be_written_does_not_lose_the_others(monkeypatch):
    class _HalfBroken(_Store):
        async def set_virality(self, post_id, score, reason):
            if post_id == 11:
                raise RuntimeError("write failed")
            self.saved.append((post_id, score, reason))
    store = _HalfBroken(ROWS)
    monkeypatch.setattr(nv, "store", store)

    async def fake(rows):
        return {0: (90, ""), 1: (20, ""), 2: (60, "")}
    monkeypatch.setattr(nv.news_search, "rate_virality", fake)
    assert _run(nv.backfill()) == 2
    assert [pid for pid, _, _ in store.saved] == [12, 13]
