"""Trend Radar: parsers, picking, ranking and the store.

Dependency-light: no network, no model, no database. Parsers are pure over
fixture text, and the model's answer is a plain dict.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from app.services import trend_radar as tr

NOW = datetime(2026, 7, 1, 6, 0, tzinfo=timezone.utc)
SRC = {"id": "x", "name": "X", "official": True, "hint": "Coding"}

RSS = """<?xml version="1.0"?><rss><channel>
<item><title>New coding agent</title><link>https://a.com/1</link>
<description>&lt;p&gt;It writes code&lt;/p&gt;</description>
<pubDate>Tue, 30 Jun 2026 08:00:00 GMT</pubDate></item>
<item><title></title><link>https://a.com/2</link></item>
</channel></rss>"""

ATOM = """<feed xmlns="http://www.w3.org/2005/Atom"><entry><title>Atom one</title>
<link rel="alternate" href="https://b.com/1"/><updated>2026-06-30T10:00:00Z</updated>
<summary>Hi</summary></entry></feed>"""


def test_parse_feed_rss_skips_broken_entries_and_strips_html():
    items = tr.parse_feed(RSS, SRC)
    assert [i["title"] for i in items] == ["New coding agent"]
    assert items[0]["summary"] == "It writes code"
    assert items[0]["published"].startswith("2026-06-30T08:00")


def test_parse_feed_atom_and_garbage():
    assert tr.parse_feed(ATOM, SRC)[0]["url"] == "https://b.com/1"
    assert tr.parse_feed("<html>nope", SRC) == []


def test_parse_page_links_keeps_same_site_articles_only():
    src = {**SRC, "url": "https://site.com/news", "link_re": r"^/news/[a-z0-9-]+$"}
    html = ('<a href="/news/big-launch">A big launch of the thing</a>'
            '<a href="/news/x">short</a>'
            '<a href="https://other.com/news/big-launch">A big launch elsewhere</a>'
            '<a href="/news/aria-only" aria-label="An article with only a label"></a>'
            '<a href="/about">About the whole company here</a>')
    items = tr.parse_page_links(html, src)
    assert [i["url"] for i in items] == [
        "https://site.com/news/big-launch", "https://site.com/news/aria-only"]


def test_parse_hn_filters_to_ai_and_coding():
    data = {"hits": [
        {"title": "A new LLM coding agent", "objectID": "1", "points": 300,
         "created_at": "2026-06-30T01:00:00Z"},
        {"title": "My cat photos", "objectID": "2"},
    ]}
    items = tr.parse_hn(data, SRC)
    assert len(items) == 1 and "item?id=1" in items[0]["url"]


def test_parse_github_trending():
    html = ('<article class="Box-row"><h2><a href="/o/repo"> o / repo </a></h2>'
            "<p>Does things</p></article>")
    items = tr.parse_github_trending(html, SRC)
    assert items[0]["title"] == "o/repo" and items[0]["summary"] == "Does things"


def _item(i, sid="a", official=False, when=NOW, title=None):
    return tr.make_item(source={"id": sid, "name": sid, "official": official},
                        title=title or f"Item number {i}", url=f"https://{sid}.com/{i}",
                        published=when)


def test_pick_candidates_drops_old_and_dupes_and_prefers_official():
    items = [
        _item(1, when=NOW - timedelta(days=30)),
        _item(2, "a", False, title="Same headline"),
        _item(3, "b", True, title="Same headline"),
        _item(4, "a"),
    ]
    got = tr.pick_candidates(items, {}, now=NOW)
    titles = [g["title"] for g in got]
    assert "Item number 1" not in titles
    assert titles.count("Same headline") == 1
    assert next(g for g in got if g["title"] == "Same headline")["official"]


def test_pick_candidates_undated_uses_first_seen_and_caps():
    undated = tr.make_item(source=SRC, title="Old page link here", url="https://x.com/old")
    seen = {"https://x.com/old": (NOW - timedelta(days=20)).isoformat()}
    assert tr.pick_candidates([undated], seen, now=NOW) == []
    many = [_item(i, sid=f"s{i % 15}") for i in range(200)]
    assert len(tr.pick_candidates(many, {}, now=NOW, limit=60)) == 60


def test_apply_judgement_clamps_and_survives_bad_rows():
    batch = [{**_item(1), "when": NOW.isoformat()}, {**_item(2), "when": NOW.isoformat()}]
    got = tr.apply_judgement(batch, {"items": [
        {"n": 1, "category": "Coding", "important": 9, "good_for_42": 0, "post_it": "4",
         "why": "w" * 300, "idea": "Reel"},
        {"n": 7, "category": "Coding"},
        {"n": 2, "category": "Nonsense", "important": 3, "good_for_42": 3, "post_it": 3,
         "why": "", "idea": ""},
        "junk",
    ]})
    assert len(got) == 2
    assert (got[0]["important"], got[0]["good_for_42"], got[0]["post_it"]) == (5, 1, 4)
    assert len(got[0]["why"]) <= 100
    assert got[1]["category"] in tr.CATEGORIES
    assert tr.apply_judgement(batch, None) == []


def test_rank_orders_by_total_and_limits_a_category():
    rows = [{"title": str(i), "category": "Coding" if i < 6 else "Research",
             "score": 15 - i, "official": False, "when": ""} for i in range(12)]
    top = tr.rank(rows, top=5)
    assert len(top) == 5
    assert [r["title"] for r in top][:3] == ["0", "1", "2"]
    assert sum(r["category"] == "Coding" for r in top) == 3


def test_store_round_trip_and_today(tmp_path, monkeypatch):
    monkeypatch.setattr(tr, "_store_path", lambda: tmp_path / "radar.json")
    assert tr.latest(NOW)["items"] == []
    tr.save_store({"radar": {"date": tr.today_gulf(NOW), "items": [{"title": "t"}]}})
    got = tr.latest(NOW)
    assert got["is_today"] and got["items"][0]["title"] == "t"
    assert not tr.latest(NOW + timedelta(days=2))["is_today"]


def test_build_failure_keeps_the_old_radar(tmp_path, monkeypatch):
    monkeypatch.setattr(tr, "_store_path", lambda: tmp_path / "radar.json")
    tr.save_store({"radar": {"date": "2026-06-30", "items": [{"title": "old"}]}})

    async def none():
        return [], {}
    monkeypatch.setattr(tr, "collect", none)
    got = asyncio.run(tr.build(NOW))
    assert got["items"][0]["title"] == "old"
    assert got["error"] and not got["building"]


def test_build_end_to_end_with_fakes(tmp_path, monkeypatch):
    monkeypatch.setattr(tr, "_store_path", lambda: tmp_path / "radar.json")

    async def fake_collect():
        return [_item(i, sid=f"s{i}") for i in range(15)], {"a": 15}

    async def fake_judge(cands):
        return tr.apply_judgement(cands, {"items": [
            {"n": n, "category": "Coding", "important": 3, "good_for_42": 3, "post_it": 3,
             "why": "w", "idea": "i"} for n in range(1, len(cands) + 1)]})
    monkeypatch.setattr(tr, "collect", fake_collect)
    monkeypatch.setattr(tr, "judge", fake_judge)

    async def no_mail(top):
        return False
    monkeypatch.setattr(tr, "email_digest", no_mail)
    got = asyncio.run(tr.build(NOW))
    assert got["is_today"] and len(got["items"]) == tr.TOP_N and got["error"] is None


def test_no_generation_cap_here():
    import inspect
    src = inspect.getsource(tr).lower()
    assert "hourly" not in src and "call_log" not in src
