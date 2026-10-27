"""Tests for ``app/services/social_report.py`` — the data assembly behind both
depths of the Social-Listening report.

No network, no database, no model: every builder under test is a pure function
over a ``ReportData`` built from fixture rows shaped exactly like the live
tables (checked against the real database on 2026-08-20: 20 competitor posts
across 2 handles, 385 news items across 17 outlet feeds, 95 viral trends from 6
public sources).

Three widths are covered: the all-streams report, a report SCOPED to one
stream, and a report about ONE post or ONE trend. The scoped and per-item
halves start at "SCOPE — the same report, narrowed to one stream" below.

Four of these tests exist because they guard the places a future edit could
quietly start lying:

  * ``test_every_competitor_item_prints_the_shares_gap`` — Instagram exposes no
    share count. The owner's own sketch asked for "22 shares". The moment a
    well-meaning edit turns that into a 0, the report becomes a fabrication.
  * ``test_sentiment_is_labelled_as_ai_assessed_everywhere`` — we store no
    comment text, so nothing here measures audience mood. Every surface that
    shows a tone must carry ``estimated`` and the disclaimer.
  * ``test_a_section_a_scope_cannot_answer_says_which_stream_it_needed`` — an
    empty Emerging Trends on a competitors-only report reads as "we watched the
    internet and nothing was trending". We did not watch.
  * ``test_an_out_of_scope_count_is_none_not_zero`` — 0 says we looked; None
    says we did not, and every surface that prints these counts skips a None.

Runs under pytest and standalone
(``cd backend && python tests/test_social_report.py``).
"""
from __future__ import annotations

import re
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

_BACKEND_ROOT = Path(__file__).resolve().parent.parent
if str(_BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(_BACKEND_ROOT))

from app.services import social_report as sr  # noqa: E402
from app.config import config  # noqa: E402

# --- these tests describe the THREE-stream report ---------------------------
# `GENERAL_TRENDS_ENABLED` is currently false, so the shipped report is a
# two-stream document (social_report.VIRAL_STREAM). That is a PAUSE pending
# research, not a removal, and everything below is the regression net that makes
# turning the stream back on a config change rather than a rewrite — so this
# module runs against the widened module and restores the configured state
# afterwards. The paused world has its own file: test_general_trends_paused.py.
@pytest.fixture(autouse=True)
def _viral_stream_on():
    sr.set_viral_stream(True)
    yield
    sr.set_viral_stream(config.general_trends_enabled)


NOW = datetime(2026, 8, 20, 12, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Fixtures — rows shaped like the live tables
# ---------------------------------------------------------------------------

def _ago(days: float) -> datetime:
    return NOW - timedelta(days=days)


def _post(post_id: int, *, username: str = "hub71ad", days: float = 3.0,
          likes: int = 100, comments: int = 10, views: int | None = 5000,
          engagement: int | None = None, outperformance: float | None = 2.0,
          theme: str | None = "campus life", topic: str = "A topic",
          angle: str | None = "Do the thing", **extra) -> dict:
    """One ``trend_posts`` row, source='competitor'."""
    row = {
        "id": post_id,
        "competitor_id": 6,
        "username": username,
        "permalink": f"https://www.instagram.com/reel/P{post_id}/",
        "media_type": "VIDEO",
        "caption": f"Caption {post_id}\n\n#tag",
        "posted_at": _ago(days),
        "created_at": _ago(days),
        "like_count": likes,
        "comments_count": comments,
        "view_count": views,
        "engagement": engagement if engagement is not None else likes + comments,
        "engagement_rate": 0.05,
        "outperformance": outperformance,
        "topic": topic,
        "topic_summary": "Because it worked",
        "theme": theme,
        "fit_score": 90,
        "uae_relevance": 80,
        "suggested_angle": angle,
        "opportunity_score": 80.0,
        "status": "new",
        "source": "competitor",
        "region": None,
        "language": None,
        "platform": None,
        "metadata": {"baseline_median": 54.0, "baseline_sample": 111},
    }
    row.update(extra)
    return row


def _news(post_id: int, *, days: float = 2.0, outlet: str = "Gulf AI",
          coverage: int = 3, score: float = 70.0, theme: str | None = "tech news",
          region: str = "gcc", angle: str | None = "Post a carousel",
          **extra) -> dict:
    """One ``trend_posts`` row, source='news' (no engagement columns at all)."""
    row = {
        "id": post_id,
        "competitor_id": None,
        "username": outlet,
        "permalink": f"https://news.example/{post_id}",
        "caption": f"Headline number {post_id}",
        "posted_at": _ago(days),
        "created_at": _ago(days),
        "like_count": None,
        "comments_count": None,
        "view_count": None,
        "engagement": None,
        "engagement_rate": None,
        "outperformance": None,
        "topic": "The Gulf is an AI hub",
        "topic_summary": "Summary",
        "theme": theme,
        "fit_score": 90,
        "uae_relevance": 95,
        "suggested_angle": angle,
        "opportunity_score": score,
        "status": "new",
        "source": "news",
        "region": region,
        "language": "en",
        "platform": outlet,
        "metadata": {"lane": outlet, "coverage": coverage,
                     "source_key": "gn_gulf_ai",
                     "also_covered_by": ["Fast Company"],
                     "summary": f"Headline number {post_id} Gulf Times"},
    }
    row.update(extra)
    return row


def _trend(trend_id: int, *, days: float = 1.0, velocity: float = 0.55,
           score: float = 60.0, sources: list[str] | None = None,
           theme: str | None = "pop culture", kind: str = "format",
           sensitivity: str = "ok", safety: str = "ok",
           status: str = "new", angle: str | None = "Make a transition reel",
           regions: list[str] | None = None, **extra) -> dict:
    """One ``general_trends`` row."""
    row = {
        "id": trend_id,
        "slug": f"trend-{trend_id}",
        "title": f"Trend {trend_id}",
        "summary": "A TikTok hashtag",
        "url": f"https://www.tiktok.com/tag/trend{trend_id}",
        "category": "entertainment",
        "regions": regions if regions is not None else ["AE", "global"],
        "sources": sources if sources is not None else ["tiktok"],
        "source_count": len(sources) if sources is not None else 1,
        "sighting_count": 5,
        "evidence_count": 5,
        "peak_metric": 288030.0,
        "metric_label": None,
        "velocity": velocity,
        "first_seen_at": _ago(days),
        "last_seen_at": _ago(days),
        "latest_content_at": None,
        "topic": "A fresh start",
        "topic_summary": "People remake the format",
        "theme": theme,
        "trend_kind": kind,
        "sensitivity": sensitivity,
        "safety": safety,
        "safety_reason": None,
        "fit_score": 80,
        "fit_reason": "Cheap to shoot",
        "uae_relevance": 85,
        "suggested_angle": angle,
        "opportunity_score": score,
        "status": status,
        "metadata": {},
    }
    row.update(extra)
    return row


def _sighting(trend_id: int, *, source: str = "tiktok", key: str = "k",
              **extra) -> dict:
    return {
        "trend_id": trend_id,
        "source": source,
        "source_key": key,
        "title": f"#trend{trend_id}",
        "url": f"https://www.tiktok.com/tag/trend{trend_id}?e={key}",
        "metric_value": 16155.0,
        "metric_label": "posts",
        "region": "AE",
        "published_at": _ago(1),
        "seen_at": _ago(0.5),
        "extra": extra or {},
    }


def _competitor(username: str, *, followers: int = 32618,
                median: float | None = 54.0, scanned: bool = True) -> dict:
    return {
        "id": abs(hash(username)) % 1000,
        "username": username,
        "display_name": username.title(),
        "followers_count": followers,
        "media_count": 1795,
        "region": "uae",
        "status": "active",
        "median_engagement": median,
        "last_scanned_at": _ago(6) if scanned else None,
        "posts_found": 111 if scanned else 0,
    }


def make_data(*, days: int = 30, competitor_rows=None, news_rows=None,
              trend_rows=None, sightings=None, competitors=None,
              news_sources=None, coverage=None, tone=None,
              scope: str = "all") -> sr.ReportData:
    """A ``ReportData`` with a default coverage record that reaches back past
    the previous window, so deltas are comparable unless a test says otherwise.

    ``scope`` mirrors the loader: a scoped report only ever holds rows for the
    streams it covers, so a test that sets a scope should hand in only those
    rows — passing out-of-scope rows here would test a state the loader cannot
    produce."""
    win = sr.make_window(days, NOW)
    streams = sr.SCOPE_STREAMS[scope]
    # The loader skips the catalogue queries for a stream it is not covering,
    # so a scoped fixture must not hold them either.
    watched = list(competitors if competitors is not None else [_competitor("hub71ad")])
    if sr.STREAM_COMPETITOR not in streams:
        watched = []
    feeds = dict(news_sources if news_sources is not None else
                 {"gn_gulf_ai": {"key": "gn_gulf_ai", "name": "Gulf AI",
                                 "url": "https://news.google.com/rss/x"}})
    if sr.STREAM_NEWS not in streams:
        feeds = {}
    default_coverage = {
        "competitor_from": _ago(200),
        "news_from": _ago(200),
        "viral_from": _ago(200),
        "last_scan_at": _ago(6),
    }
    return sr.ReportData(
        now=win["win_to"], days=win["days"], win_from=win["win_from"],
        win_to=win["win_to"], prev_from=win["prev_from"], prev_to=win["prev_to"],
        scope=scope,
        competitor_rows=list(competitor_rows or []),
        news_rows=list(news_rows or []),
        trend_rows=list(trend_rows or []),
        sightings=dict(sightings or {}),
        competitors=watched,
        news_sources=feeds,
        coverage=dict(coverage or default_coverage),
        tone=dict(tone or {}),
    )


def full_data() -> sr.ReportData:
    """A populated window across all three streams."""
    posts = [
        _post(1, username="hub71ad", days=3, likes=1936, comments=31,
              views=113157, outperformance=36.426, theme="campus life"),
        _post(2, username="hub71ad", days=8, likes=345, comments=24,
              views=15527, outperformance=6.833, theme="community"),
        _post(3, username="42.born2code", days=10, likes=500, comments=40,
              views=None, outperformance=2.5, theme="campus life"),
        # previous window
        _post(4, username="hub71ad", days=40, likes=100, comments=5,
              outperformance=1.1),
        _post(5, username="hub71ad", days=45, likes=90, comments=4,
              outperformance=0.9),
    ]
    news = [_news(10 + i, days=1 + i, score=70.0 - i) for i in range(4)]
    news.append(_news(30, days=50, outlet="Gulf News"))  # previous window
    trends = [
        _trend(100, days=1, velocity=0.62, score=61.0, sources=["tiktok"]),
        _trend(101, days=2, velocity=0.30, score=40.0, sources=["kym_entries"],
               theme="meme format"),
        _trend(102, days=2, velocity=0.20, score=10.0, sensitivity="avoid"),
        _trend(103, days=3, velocity=0.10, score=5.0, safety="blocked"),
    ]
    sightings = {
        100: [_sighting(100, posts=24297.0, views=57160324.0,
                        views_per_post=2353.0, growth_ratio=1.9,
                        top_creators=["mariiamm66", "idkdanlol"],
                        creator_followers=[2235619.0, 4216014.0]),
              _sighting(100, source="youtube_shorts", key="yt1",
                        growth_ratio=1.4)],
        101: [_sighting(101, source="kym_entries", key="kym1")],
    }
    return make_data(competitor_rows=posts, news_rows=news, trend_rows=trends,
                     sightings=sightings,
                     competitors=[_competitor("hub71ad"),
                                  _competitor("42.born2code", followers=17170,
                                              median=182.0),
                                  _competitor("nyuabudhabi", followers=46357,
                                              median=None, scanned=False)])


# ---------------------------------------------------------------------------
# Contract shape
# ---------------------------------------------------------------------------

def _assert_section(section: dict) -> None:
    for key in ("id", "title", "available", "unavailable_reason", "basis",
                "estimated", "kind", "body"):
        assert key in section, f"{section.get('id')} missing {key}"
    assert isinstance(section["available"], bool)
    assert isinstance(section["estimated"], bool)
    assert section["kind"] in sr.SECTION_KINDS
    assert isinstance(section["basis"], str) and section["basis"].strip()
    if not section["available"]:
        assert section["unavailable_reason"], (
            f"{section['id']} is unavailable with no reason")
    else:
        assert section["unavailable_reason"] is None
    body = section["body"]
    assert isinstance(body, dict)
    if section["kind"] == "table":
        assert len(body["columns"]) == len(body["align"])
        for row in body["rows"]:
            assert len(row) == len(body["columns"])
            assert all(isinstance(cell, str) for cell in row)
    elif section["kind"] == "chart":
        assert body["chart"] in ("bar", "line")
        for series in body["series"]:
            assert len(series["values"]) == len(body["labels"])
    elif section["kind"] == "kpis":
        for item in body["items"]:
            assert set(item) == {"label", "value", "delta", "delta_dir", "note"}
            assert item["delta_dir"] in (None, "up", "down", "flat")
            if item["delta"] is None:
                assert item["delta_dir"] is None
    elif section["kind"] == "signals":
        for group in body["groups"]:
            assert set(group) == {"group", "items"}
            for item in group["items"]:
                assert set(item) == {"name", "link", "extra_links", "metrics",
                                     "sentiment", "suggestion"}


def test_simple_mode_is_exactly_the_owners_sketch():
    report = sr.assemble(full_data(), "simple")
    assert report["meta"]["mode"] == "simple"
    assert report["meta"]["brand"] == sr.BRAND
    assert len(report["sections"]) == 1
    section = report["sections"][0]
    assert section["kind"] == "signals"
    _assert_section(section)
    groups = [g["group"] for g in section["body"]["groups"]]
    assert groups == ["Competitors / Sentiment", "Industry Signal",
                      "Viral Social Trends"]


def test_full_mode_emits_a_concise_competitor_and_industry_briefing():
    report = sr.assemble(full_data(), "full")
    ids = [s["id"] for s in report["sections"]]
    assert ids == list(sr.FULL_SECTION_IDS)
    assert ids == [
        # The counted sections lead: a reader takes two seconds over the
        # numbers and the chart, and reads the written summary already knowing
        # the size of what it describes.
        "report_snapshot", "activity", "executive_summary",
        "competitor_conversation", "competitor_content", "industry_signals",
        "content_opportunities", "recommendations", "data_basis",
    ]
    assert ids[0] == "report_snapshot", "at a glance, first"
    assert ids[-1] == "data_basis"
    assert len(ids) == 9
    for section in report["sections"]:
        _assert_section(section)


def test_meta_and_basis_carry_the_windows_and_the_real_counts():
    report = sr.assemble(full_data(), "full")
    meta, basis = report["meta"], report["basis"]
    assert meta["window"]["days"] == 30
    assert meta["window"]["from"] < meta["window"]["to"]
    assert meta["previous_window"]["to"] == meta["window"]["from"]
    assert basis["competitor_posts"] == 3      # two of the five are older
    assert basis["competitor_handles"] == 2
    assert basis["news_items"] == 4
    assert basis["viral_trends"] == 2          # avoid + blocked are excluded
    assert basis["notes"]


def test_concise_briefing_separates_competitor_findings_from_industry_context():
    data = full_data()
    data.tone = {
        sr.tone_key(sr.STREAM_COMPETITOR, row): {"label": "Positive"}
        for row in data.competitors_now
    }
    sections = {section["id"]: section for section in sr.assemble(data, "full")["sections"]}

    competitor = sections["competitor_conversation"]
    assert competitor["available"] is True
    assert competitor["body"]["columns"] == [
        "Account", "Posts", "Main topics", "Tone of post text", "Strongest post",
    ]
    assert "not audience sentiment" in competitor["basis"]
    assert "positive" in " ".join(row[3] for row in competitor["body"]["rows"]).lower()

    industry = sections["industry_signals"]
    groups = [group["group"] for group in industry["body"]["groups"]]
    assert groups == ["Current industry stories", "Emerging social formats"]
    assert "competitor" not in " ".join(groups).lower()


def test_content_opportunities_are_labelled_as_tests_not_confirmed_gaps():
    section = sr.build_content_opportunities(full_data())
    assert section["available"] is True
    assert "not confirmed gaps" in section["body"]["items"][0].lower()
    assert len(section["body"]["items"]) > 1
    assert section["estimated"] is True


def test_a_row_cap_hit_during_loading_is_printed_not_swallowed():
    """A truncated read makes every count an undercount. It must say so."""
    data = full_data()
    data.load_notes.append("Only the 40,000 most recent rows were read")
    report = sr.assemble(data, "full")
    assert "Only the 40,000 most recent rows were read" in report["basis"]["notes"]


def test_build_sections_rejects_an_unknown_mode_by_falling_back_to_simple():
    report = sr.assemble(full_data(), "nonsense")
    assert report["meta"]["mode"] == "simple"
    assert len(report["sections"]) == 1


# ---------------------------------------------------------------------------
# The two things that must never quietly start lying
# ---------------------------------------------------------------------------

def test_every_competitor_item_prints_the_shares_gap():
    """Instagram exposes no share count. It must be words, never a 0."""
    group = sr.group_competitors(full_data())
    assert group["items"]
    for item in group["items"]:
        shares = [m for m in item["metrics"] if m["label"] == sr.SHARES_LABEL]
        assert len(shares) == 1, "the shares gap must be stated on every item"
        assert shares[0]["value"] == sr.SHARES_VALUE
        assert "not available" in shares[0]["value"]
        assert shares[0]["value"] != "0"


def test_shares_are_never_reported_as_a_number_anywhere_in_a_full_report():
    report = sr.assemble(full_data(), "full")
    blob = repr(report).lower()
    assert "shares" in blob
    for lie in ("'shares', 'value': '0'", "0 shares", "shares: 0"):
        assert lie not in blob


def test_sentiment_is_labelled_as_ai_assessed_everywhere():
    data = full_data()
    data.tone = {sr.tone_key("competitor", data.competitors_now[0]):
                 {"label": "Positive", "estimated": True}}
    group = sr.group_competitors(data)
    tagged = [i for i in group["items"] if i["sentiment"]]
    assert tagged, "the fixture tone should reach the item it was keyed to"
    for item in tagged:
        assert item["sentiment"]["estimated"] is True
        assert item["sentiment"]["label"] in ("Positive", "Neutral", "Negative")

    section = sr.build_sentiment(data)
    assert section["estimated"] is True
    assert sr.SENTIMENT_DISCLAIMER in section["basis"]
    assert "comment text" in section["basis"]


def test_sentiment_without_a_tone_pass_is_unavailable_with_the_honest_reason():
    section = sr.build_sentiment(full_data())          # tone = {}
    assert section["available"] is False
    assert section["unavailable_reason"] == sr.SENTIMENT_UNAVAILABLE
    assert "no comment text" in section["unavailable_reason"].lower()
    assert section["estimated"] is True


def test_sentiment_counts_items_not_percentages():
    data = full_data()
    data.tone = {sr.tone_key("competitor", r): {"label": "Positive",
                                                "estimated": True}
                 for r in data.competitors_now}
    section = sr.build_sentiment(data)
    assert section["available"] is True
    rows = {row[0]: row for row in section["body"]["rows"]}
    assert rows["Competitors / Sentiment"][1] == "3"     # positive
    assert rows["Competitors / Sentiment"][4] == "0"     # not assessed
    assert rows["Industry Signal"][4] == "4"             # none assessed
    assert rows["All streams"][5] == "9"                 # 3 + 4 + 2 items
    for row in section["body"]["rows"]:
        assert "%" not in "".join(row), "counts of items, never a percentage"


def test_data_basis_lists_everything_that_is_not_measured():
    section = sr.build_data_basis(full_data())
    assert section["available"] is True
    assert section["estimated"] is False
    blob = " ".join(section["body"]["items"]).lower()
    for missing in ("shares", "audience sentiment", "audience demographics",
                    "mentions of 42 abu dhabi"):
        assert missing in blob, f"{missing} must be named as not measured"
    assert "come from the source platforms" in blob
    assert "ai-assisted" in blob


# ---------------------------------------------------------------------------
# Delta maths
# ---------------------------------------------------------------------------

def test_delta_kpi_counts_up_down_and_flat():
    up = sr.delta_kpi("x", 20, 10, comparable=True, window_label="30 days",
                      no_history_note="n/a")
    assert (up["value"], up["delta"], up["delta_dir"]) == ("20", "+10", "up")
    assert "10 in the previous 30 days" in up["note"]

    down = sr.delta_kpi("x", 20, 35, comparable=True, window_label="30 days",
                        no_history_note="n/a")
    assert (down["delta"], down["delta_dir"]) == ("-15", "down")

    flat = sr.delta_kpi("x", 7, 7, comparable=True, window_label="30 days",
                        no_history_note="n/a")
    assert (flat["delta"], flat["delta_dir"]) == ("0", "flat")


def test_delta_is_withheld_when_collection_started_after_the_previous_window():
    """A stream switched on last week has not GROWN by its whole volume."""
    note = "no comparable history - collection started 5 Aug 2026"
    kpi = sr.delta_kpi("Industry news items", 385, 0, comparable=False,
                       window_label="30 days", no_history_note=note)
    assert kpi["value"] == "385"
    assert kpi["delta"] is None and kpi["delta_dir"] is None
    assert kpi["note"] == note


def test_mention_volume_withholds_the_delta_for_a_stream_with_no_history():
    data = full_data()
    data.coverage = dict(data.coverage, news_from=_ago(15))  # inside the window
    section = sr.build_mention_volume(data)
    by_label = {i["label"]: i for i in section["body"]["items"]}
    assert by_label["Industry news items"]["delta"] is None
    assert "no comparable history" in by_label["Industry news items"]["note"]
    # the competitor stream still reaches back, so its delta survives
    assert by_label["Competitor posts"]["delta"] == "+1"
    assert by_label["Competitor posts"]["delta_dir"] == "up"


def test_mention_volume_counts_only_the_current_window():
    section = sr.build_mention_volume(full_data())
    by_label = {i["label"]: i for i in section["body"]["items"]}
    assert by_label["Competitor posts"]["value"] == "3"
    assert by_label["Industry news items"]["value"] == "4"
    assert by_label["Viral trends first seen"]["value"] == "2"
    viral_note = by_label["Viral trends first seen"]["note"]
    assert "2 trend(s) were collected and held back" in viral_note
    assert by_label["All items"]["value"] == "9"
    assert by_label["Watched Instagram accounts"]["value"] == "3"
    assert "never been scanned" in by_label["Watched Instagram accounts"]["note"]


def test_mention_volume_is_unavailable_with_a_reason_when_nothing_landed():
    section = sr.build_mention_volume(make_data())
    assert section["available"] is False
    assert "No items of any kind" in section["unavailable_reason"]


# ---------------------------------------------------------------------------
# Per-section behaviour
# ---------------------------------------------------------------------------

def test_narrative_sections_are_emitted_but_left_for_the_other_agent():
    report = sr.assemble(full_data(), "full")
    by_id = {s["id"]: s for s in report["sections"]}
    for key in sr.NARRATIVE_SECTION_IDS:
        section = by_id[key]
        assert section["available"] is False
        assert section["unavailable_reason"] == sr.NARRATIVE_PENDING
        assert section["kind"] == "prose"
        assert section["body"] == {"paragraphs": []}
        assert section["estimated"] is True


def test_conversation_trend_buckets_one_column_per_day_for_a_30_day_window():
    section = sr.build_conversation_trend(full_data())
    assert section["available"] is True
    body = section["body"]
    assert body["chart"] == "line"
    assert len(body["labels"]) == 30
    names = [s["name"] for s in body["series"]]
    assert names == ["Competitors / Sentiment", "Industry Signal",
                     "Viral Social Trends"]
    assert sum(body["series"][0]["values"]) == 3.0
    assert sum(body["series"][1]["values"]) == 4.0
    # the avoid + blocked rows are held back here exactly as they are in the
    # Mention Volume count, so the two sections reconcile
    assert sum(body["series"][2]["values"]) == 2.0
    assert "FIRST COLLECTED" in section["basis"]


def test_conversation_trend_keeps_a_90_day_window_readable():
    data = make_data(days=90, competitor_rows=[_post(1, days=5)])
    section = sr.build_conversation_trend(data)
    assert len(section["body"]["labels"]) == 30
    assert "3-day bucket" in section["basis"]


def test_conversation_trend_is_unavailable_when_nothing_is_dated_inside():
    section = sr.build_conversation_trend(make_data())
    assert section["available"] is False
    assert "No dated items" in section["unavailable_reason"]


def test_emerging_trends_takes_only_new_and_accelerating_rows():
    data = full_data()
    section = sr.build_emerging_trends(data)
    assert section["available"] is True
    names = [row[0] for row in section["body"]["rows"]]
    assert "Trend 100" in names                 # heat 0.62 >= 0.45
    assert "Trend 101" not in names             # heat 0.30, no growth reported
    assert section["estimated"] is True         # the Type column is an AI label
    assert "0.45" in section["basis"]


def test_emerging_trends_accepts_a_source_reported_growth_ratio_alone():
    slow_but_rising = _trend(200, days=2, velocity=0.10, score=50.0)
    data = make_data(trend_rows=[slow_but_rising],
                     sightings={200: [_sighting(200, growth_ratio=1.9)]})
    section = sr.build_emerging_trends(data)
    assert [row[0] for row in section["body"]["rows"]] == ["Trend 200"]
    assert section["body"]["rows"][0][3] == "1.90x"


def test_emerging_trends_says_why_when_nothing_accelerates():
    data = make_data(trend_rows=[_trend(300, days=2, velocity=0.05)])
    section = sr.build_emerging_trends(data)
    assert section["available"] is False
    assert "acceleration bar" in section["unavailable_reason"]

    empty = sr.build_emerging_trends(make_data())
    assert empty["available"] is False
    assert "No new viral trends" in empty["unavailable_reason"]


def test_top_topics_groups_one_theme_across_all_three_streams():
    data = make_data(
        competitor_rows=[_post(1, theme="campus life"),
                         _post(2, theme="Campus Life")],
        news_rows=[_news(10, theme="campus life")],
        trend_rows=[_trend(100, theme="pop culture")])
    section = sr.build_top_topics(data)
    rows = {row[0].lower(): row for row in section["body"]["rows"]}
    assert rows["campus life"][1:] == ["2", "1", "0", "3"]
    assert rows["pop culture"][1:] == ["0", "0", "1", "1"]
    assert section["estimated"] is True
    assert "not a measurement" in section["basis"]


def test_top_topics_is_unavailable_when_nothing_has_been_scored():
    data = make_data(competitor_rows=[_post(1, theme=None)])
    section = sr.build_top_topics(data)
    assert section["available"] is False
    assert "theme" in section["unavailable_reason"]


def test_share_of_voice_states_the_handle_count_so_nobody_over_reads_it():
    section = sr.build_share_of_voice(full_data())
    assert section["available"] is True
    assert "2 Instagram account(s)" in section["basis"]
    assert "not a market share" in section["basis"]
    assert "42 Abu Dhabi's own account is not in this comparison" in section["basis"]
    shares = [row[-1] for row in section["body"]["rows"]]
    assert all(s.endswith("%") for s in shares)
    assert section["estimated"] is False


def test_share_of_voice_shares_add_up_to_one_hundred_percent():
    data = make_data(competitor_rows=[
        _post(1, username="a", likes=70, comments=0, engagement=70),
        _post(2, username="b", likes=30, comments=0, engagement=30)])
    section = sr.build_share_of_voice(data)
    assert [row[-1] for row in section["body"]["rows"]] == ["70.0%", "30.0%"]


def test_share_of_voice_is_unavailable_with_a_reason_when_no_posts_landed():
    section = sr.build_share_of_voice(make_data())
    assert section["available"] is False
    assert "no voice to share out" in section["unavailable_reason"]


def test_competitive_intelligence_keeps_a_never_scanned_account_visible():
    section = sr.build_competitive_intelligence(full_data())
    rows = {row[0]: row for row in section["body"]["rows"]}
    assert rows["@nyuabudhabi"][2] == "0"
    assert rows["@nyuabudhabi"][-1] == "never scanned"
    assert rows["@hub71ad"][1] == "32,618"
    assert rows["@hub71ad"][4] == "36.4x"
    assert "Last scan" in section["basis"]
    assert section["estimated"] is True


def test_audience_insights_says_plainly_that_demographics_do_not_exist():
    section = sr.build_audience_insights(full_data())
    assert section["available"] is True
    assert "NO audience demographics" in section["basis"]
    assert "age, gender, location or interest" in section["basis"]
    dims = {row[0] for row in section["body"]["rows"]}
    assert "Region tag (Industry Signal)" in dims
    assert "Reach of a watched account" in dims
    assert section["estimated"] is False


def test_top_content_labels_the_unit_of_every_measured_number():
    section = sr.build_top_content(full_data())
    assert section["available"] is True
    measured = [row[2] for row in section["body"]["rows"]]
    assert any("engagement" in m for m in measured)
    assert any("views" in m for m in measured)
    # Industry Signal has no measured number of its own any more: a search
    # cannot count outlets, so the honest column is the day it ran, with the
    # relevance beside it labelled as the judgement it is.
    assert any("judged" in m for m in measured)
    links = [row[3] for row in section["body"]["rows"]]
    # shortened for the column; the full URL rides on the signals items
    assert all(link.startswith(("www.", "news.example/", "instagram.com/",
                                "tiktok.com/")) or link == "-"
               for link in links)
    assert all(len(link) <= 62 for link in links)
    assert "no engagement metric of their own" in section["basis"]


def test_influencers_uses_real_names_and_computes_no_influence_score():
    section = sr.build_influencers(full_data())
    names = [row[0] for row in section["body"]["rows"]]
    assert "@idkdanlol" in names            # TikTok's own top-creator field
    assert "Gulf AI" in names               # the outlet tag on the news items
    assert "@hub71ad" in names              # a handle we watch
    creator = next(row for row in section["body"]["rows"] if row[0] == "@idkdanlol")
    assert "4,216,014 followers" in creator[2]
    assert "No influence, authority or reach score is computed" in section["basis"]
    assert section["estimated"] is False


def test_channel_analysis_warns_that_a_trend_can_sit_on_two_sources():
    data = make_data(
        competitor_rows=[_post(1)],
        news_rows=[_news(10)],
        trend_rows=[_trend(100, sources=["tiktok", "youtube_shorts"])])
    section = sr.build_channel_analysis(data)
    channels = {row[0]: row for row in section["body"]["rows"]}
    assert channels["Instagram"][2] == "1"
    assert channels["News + publisher feeds"][2] == "1"
    assert channels["tiktok"][2] == "1"
    assert channels["youtube_shorts"][2] == "1"
    assert "exceed 100%" in section["basis"]


def test_reputation_risk_lists_flagged_rows_and_disclaims_brand_monitoring():
    data = full_data()
    section = sr.build_reputation_risk(data)
    assert section["available"] is True
    flags = {row[1] for row in section["body"]["rows"]}
    assert "scorer: avoid" in flags
    assert "ingest screen: blocked" in flags
    assert "not a measurement of harm" in section["basis"]
    assert "does not track mentions of the school" in section["basis"]
    assert section["estimated"] is True


def test_reputation_risk_never_passes_off_a_fit_reason_as_a_flag_reason():
    """`fit_reason` says whether a trend would SUIT 42, not why it was flagged.

    On live rows the two routinely disagree - "The image perfectly captures the
    feeling of being overwhelmed by a coding challenge" sat under a 'caution'
    flag - and printing it reads as a justification for the flag."""
    flagged = _trend(100, sensitivity="caution", safety_reason=None,
                     fit_reason="This would suit us perfectly")
    section = sr.build_reputation_risk(make_data(trend_rows=[flagged]))
    why = section["body"]["rows"][0][2]
    assert why == "verdict recorded without a written reason"
    assert "suit us perfectly" not in why

    with_reason = _trend(101, safety="blocked", safety_reason="avoid_topic")
    section = sr.build_reputation_risk(make_data(trend_rows=[with_reason]))
    assert section["body"]["rows"][0][2] == "avoid_topic"


def test_reputation_risk_reports_a_measured_volume_spike():
    rows = [_news(100 + i, days=4.0) for i in range(12)]
    rows += [_news(200 + i, days=1.0 + i) for i in range(3)]
    section = sr.build_reputation_risk(make_data(news_rows=rows))
    spikes = [row for row in section["body"]["rows"] if row[1] == "Volume spike"]
    assert len(spikes) == 1
    assert "12 item(s)" in spikes[0][2]
    assert spikes[0][3] == "Industry Signal"


def test_reputation_risk_is_unavailable_when_nothing_is_flagged():
    section = sr.build_reputation_risk(make_data(trend_rows=[_trend(100)]))
    assert section["available"] is False
    assert "none of the 1 trend(s)" in section["unavailable_reason"].lower()


# ---------------------------------------------------------------------------
# Signals groups
# ---------------------------------------------------------------------------

def test_competitor_items_are_ranked_by_outperformance_not_raw_likes():
    data = make_data(competitor_rows=[
        _post(1, likes=10_000, comments=0, outperformance=1.2),
        _post(2, likes=50, comments=0, outperformance=30.0)])
    names = [i["name"] for i in sr.group_competitors(data)["items"]]
    assert names[0].startswith("@hub71ad")
    assert sr.top_competitor_posts(data.competitors_now, 1)[0]["id"] == 2


def test_news_items_carry_the_article_link_and_nothing_they_cannot_back_up():
    """The article's own address is the only link a story has now: there is no
    feed behind it to point at, and no outlet count to print."""
    item = sr.group_news(full_data())["items"][0]
    assert item["link"].startswith("https://news.example/")
    assert item["extra_links"] == []
    labels = {m["label"] for m in item["metrics"]}
    assert {"Outlet", "Published"} <= labels
    assert "Fit for 42 (AI judgement)" in labels
    assert "Outlets covering it" not in labels
    assert "Region tag" not in labels


def test_viral_items_report_posts_views_per_post_and_heat():
    item = sr.group_viral(full_data())["items"][0]
    metrics = {m["label"]: m["value"] for m in item["metrics"]}
    assert metrics["Posts"] == "24,297"
    assert metrics["Views"] == "57,160,324"
    assert metrics["Views per post"] == "2,353"
    assert metrics["Heat"] == "0.62 of 1.00 (rising)"
    assert item["link"].startswith("https://www.tiktok.com/tag/")
    # "Sources" and "Evidence sightings" are facts about our collection, not
    # about the trend, and a reader deciding whether to make something cannot
    # act on either. The platform is already visible in the link.
    assert "Sources" not in metrics and "Evidence sightings" not in metrics


def test_a_signal_item_carries_no_prose_of_its_own():
    """A `suggestion` under every item turned a list into an essay, and said
    what the ranked "Opportunities" section says once, eleven times over."""
    data = full_data()
    for group in (sr.group_viral(data), sr.group_news(data)):
        for item in group["items"]:
            assert item["suggestion"] is None
            assert all(len(str(m["value"])) < 60 for m in item["metrics"]), (
                "a metric is a fact, not a paragraph"
            )


def test_viral_items_skip_dismissed_filtered_and_avoid_rows():
    data = full_data()
    shown = {i["name"] for i in sr.group_viral(data)["items"]}
    assert shown == {"Trend 100", "Trend 101"}


def test_a_missing_suggestion_says_so_instead_of_going_blank():
    data = make_data(competitor_rows=[_post(1, angle=None)])
    item = sr.group_competitors(data)["items"][0]
    assert "No angle stored" in item["suggestion"]


def test_signals_names_the_empty_groups_in_its_basis():
    data = make_data(competitor_rows=[_post(1)])
    section = sr.build_signals(data)
    groups = [g["group"] for g in section["body"]["groups"]]
    assert groups == ["Competitors / Sentiment"]
    assert "No rows in this window for: Industry Signal, Viral Social Trends" \
        in section["basis"]


def test_signals_is_unavailable_with_a_reason_on_an_empty_window():
    section = sr.build_signals(make_data())
    assert section["available"] is False
    assert "Nothing was collected in the last 30 days" in section["unavailable_reason"]
    assert section["body"] == {"groups": []}


def test_signals_groups_are_capped_so_the_brief_stays_a_brief():
    data = make_data(competitor_rows=[_post(i, outperformance=float(i))
                                      for i in range(1, 12)])
    assert len(sr.group_competitors(data)["items"]) == sr.SIGNALS_PER_GROUP


# ---------------------------------------------------------------------------
# Helpers and the tone pass
# ---------------------------------------------------------------------------

def test_trend_measures_reads_only_what_a_source_published():
    trend = _trend(1)
    measured = sr.trend_measures(trend, [_sighting(1, posts=100.0, views=900.0)])
    assert measured["posts"] == 100.0
    assert measured["views"] == 900.0
    assert measured["views_per_post"] == 9.0
    assert measured["growth"] is None       # nothing reported one

    silent = sr.trend_measures(_trend(2), [_sighting(2)])
    assert silent["posts"] is None and silent["views"] is None
    assert silent["views_per_post"] is None


def test_trend_measures_takes_the_labelled_peak_metric_when_that_is_all_there_is():
    trend = _trend(3, peak_metric=17378.0, metric_label="views")
    measured = sr.trend_measures(trend, [])
    assert measured["views"] == 17378.0
    assert measured["posts"] is None


def test_make_window_clamps_days_to_the_supported_range():
    assert sr.make_window(0, NOW)["days"] == 1
    assert sr.make_window(9999, NOW)["days"] == 90
    win = sr.make_window(30, NOW)
    assert win["prev_to"] == win["win_from"]
    assert (win["win_to"] - win["win_from"]).days == 30


def test_section_refuses_to_be_unavailable_without_a_reason():
    with pytest.raises(ValueError):
        sr._section("x", "X", kind="table", basis="b", available=False)
    with pytest.raises(ValueError):
        sr._section("x", "X", kind="nope", basis="b")


def test_table_align_must_match_the_columns():
    with pytest.raises(ValueError):
        sr._table(["a", "b"], [], ["left"])


def test_tone_targets_are_exactly_the_items_simple_mode_prints():
    data = full_data()
    keys = {t["key"] for t in sr.tone_targets(data, "simple")}
    # The selectors are deterministic and shared, so "assessed" and "printed"
    # can never drift apart — that is what stops a tone landing on an item the
    # report does not show (or an item shipping with no tone at all).
    printed = set()
    for row in sr.top_competitor_posts(data.competitors_now, sr.SIGNALS_PER_GROUP):
        printed.add(sr.tone_key("competitor", row))
    for row in sr.top_news_items(data.news_now, sr.SIGNALS_PER_GROUP):
        printed.add(sr.tone_key("news", row))
    for row in sr.top_trends(data.trends_now, sr.SIGNALS_PER_GROUP):
        printed.add(sr.tone_key("viral", row))
    assert keys == printed
    assert len(keys) <= sr.TONE_MAX_ITEMS


def test_tone_targets_never_exceed_the_batch_cap():
    data = make_data(news_rows=[_news(i, days=1, score=float(i))
                                for i in range(200)])
    assert len(sr.tone_targets(data, "full")) <= sr.TONE_MAX_ITEMS


def test_parse_tone_response_keeps_only_keys_we_asked_about():
    targets = [{"key": "competitor:1", "text": "hello"}]
    out = sr.parse_tone_response(
        {"items": [{"key": "competitor:1", "tone": "positive"},
                   {"key": "competitor:999", "tone": "negative"},
                   {"key": "competitor:1", "tone": "not-a-tone"},
                   "junk"]},
        targets)
    assert out == {"competitor:1": {"label": "Positive", "estimated": True}}


def test_parse_tone_response_returns_nothing_rather_than_a_default_label():
    targets = [{"key": "competitor:1", "text": "hello"}]
    assert sr.parse_tone_response(None, targets) == {}
    assert sr.parse_tone_response({"items": "nope"}, targets) == {}
    assert sr.parse_tone_response({}, targets) == {}
    assert sr.parse_tone_response({"items": []}, targets) == {}


def test_one_line_takes_the_sentence_not_the_hashtag_block():
    caption = "POV: you joined the Office World Cup\n\n#Hub71 #FWC26"
    assert sr._one_line(caption, 90) == "POV: you joined the Office World Cup"
    assert sr._one_line("x" * 200, 20).endswith("...")
    assert len(sr._one_line("x" * 200, 20)) == 20
    assert sr._one_line(None) == ""


def test_link_text_keeps_a_google_news_redirect_inside_a_table_column():
    monster = "https://news.google.com/rss/articles/" + "CBMi" * 200
    out = sr._link_text(monster)
    assert len(out) <= 62 and out.endswith("...")
    assert out.startswith("news.google.com/")
    assert sr._link_text("https://a.example/b") == "a.example/b"
    assert sr._link_text(None) == "-"


def test_the_briefing_snapshot_keeps_competitor_and_industry_counts_separate():
    """The top of the new report must not make a made-up blended total look useful."""
    report = sr.assemble(full_data(), "full")
    by_id = {s["id"]: s for s in report["sections"]}
    basis = report["basis"]
    snapshot = {item["label"]: item["value"]
                for item in by_id["report_snapshot"]["body"]["items"]}
    assert snapshot["Competitor posts reviewed"] == str(basis["competitor_posts"])
    assert snapshot["Industry stories reviewed"] == str(basis["news_items"])
    assert snapshot["Emerging social formats"] == str(basis["viral_trends"])


def test_counts_and_ratios_never_render_a_missing_value_as_zero():
    assert sr._count(None) == "not recorded"
    assert sr._count(1936) == "1,936"
    assert sr._ratio(None) is None
    assert sr._ratio(36.426) == "36.4x"
    assert sr._pct(1, 0) == "n/a"


if __name__ == "__main__":  # pragma: no cover — standalone run
    raise SystemExit(pytest.main([__file__, "-q"]))


# ---------------------------------------------------------------------------
# Honesty fixes found by auditing a real report against the database
# ---------------------------------------------------------------------------

def test_emerging_trends_prints_the_region_each_trend_was_seen_in():
    """Region is MEASURED (the source reports it) and it is the first thing an
    Abu Dhabi reader asks of a trend.

    It exists as a column because of a real fabrication: with no per-trend
    geography in the report, the narrative pass read the aggregate "AE - 25
    trends" out of Audience Insights and wrote that two named sounds were
    "trending in the AE region" and should be used. Both were stored ["SA"]."""
    data = make_data(trend_rows=[
        _trend(1, velocity=0.9, regions=["AE", "SA", "US"]),
        _trend(2, velocity=0.8, regions=["SA"]),
    ])
    section = sr.build_emerging_trends(data)
    _assert_section(section)
    body = section["body"]
    assert "Seen in" in body["columns"]
    where = body["columns"].index("Seen in")
    seen = {row[0]: row[where] for row in body["rows"]}
    assert seen["Trend 1"] == "AE, SA, US"
    # The SA-only trend must NOT read as AE anywhere on its row.
    assert seen["Trend 2"] == "SA"
    assert "AE" not in seen["Trend 2"]


def test_a_trend_with_no_stored_region_says_so_rather_than_guessing():
    data = make_data(trend_rows=[_trend(1, velocity=0.9, regions=[])])
    section = sr.build_emerging_trends(data)
    where = section["body"]["columns"].index("Seen in")
    assert section["body"]["rows"][0][where] == "not reported"


def test_emerging_trends_basis_explains_what_a_missing_region_means():
    """Absent from the list is "not observed there", not "confirmed absent" —
    the difference decides whether a reader may rule a trend out."""
    data = make_data(trend_rows=[_trend(1, velocity=0.9)])
    basis = sr.build_emerging_trends(data)["basis"]
    assert "Seen in" in basis
    assert "not observed there rather than confirmed absent" in basis


def test_reputation_risk_basis_reconciles_the_rows_a_reader_can_count():
    """The table mixes flagged trends with volume-spike DAYS and is capped, so
    counting its rows gives a number that matches nothing else in the report.
    On live data that produced "the system has flagged 22 trends" when 20 were
    flagged and 15 were listed. The basis now adds up out loud."""
    trends = [_trend(i, sensitivity="avoid", status="new") for i in range(1, 21)]
    data = make_data(trend_rows=trends)
    section = sr.build_reputation_risk(data)
    _assert_section(section)
    basis = section["basis"]
    rows = section["body"]["rows"]
    assert "20 of 20 viral trend(s)" in basis
    assert f"lists {sr.RISK_CAP} of those 20 flagged trend(s)" in basis
    assert f"{len(rows)} row(s) in total" in basis
    assert "A spike row is a day, not a trend." in basis


def test_reputation_risk_says_when_no_volume_spike_was_measured():
    data = make_data(trend_rows=[_trend(1, sensitivity="avoid")])
    basis = sr.build_reputation_risk(data)["basis"]
    assert "no volume spikes were measured" in basis
    assert "lists 1 of those 1 flagged trend(s)" in basis


def test_sentiment_basis_admits_the_assessed_items_are_not_a_random_sample():
    """The tone pass reads the TOP-ranked items of each stream. Reported as a
    bare "60 of 487", that reads as a representative sample; it is not, and the
    tone it produces leans towards the best performers."""
    data = make_data()
    basis = sr.build_sentiment(data)["basis"]
    assert "NOT a random sample" in basis
    assert "top-ranked items of each stream" in basis
    assert "not of the window" in basis


# ===========================================================================
# SCOPE — the same report, narrowed to one stream
# ===========================================================================
#
# The failure this half of the suite guards against is subtle and expensive: a
# competitors-only report that renders Emerging Trends as an empty table reads
# as "we watched the internet and nothing was trending". We did not watch. So
# every section a scope cannot answer must be `available: False` with a reason
# that names the stream it needed — and the scope itself must be visible in the
# title, the meta, the basis notes and the affected sections' own basis lines,
# because a single-stream number read as a whole-picture number is exactly the
# misreading this whole feature exists to prevent.

def scoped_data(scope: str) -> sr.ReportData:
    """A populated window holding ONLY the rows that scope's loader would."""
    if scope == "competitors":
        return make_data(scope=scope, competitor_rows=[
            _post(1, username="hub71ad", days=3, likes=1936, comments=31,
                  outperformance=36.426),
            _post(2, username="42.born2code", days=8, likes=345, comments=24,
                  outperformance=6.833),
        ], competitors=[_competitor("hub71ad"),
                        _competitor("42.born2code", followers=17170)])
    if scope == "news":
        return make_data(scope=scope,
                         news_rows=[_news(10 + i, days=1 + i, score=70.0 - i)
                                    for i in range(4)])
    if scope == "viral":
        return make_data(scope=scope,
                         trend_rows=[_trend(100, days=1, velocity=0.62),
                                     _trend(101, days=2, velocity=0.30)],
                         sightings={100: [_sighting(100, posts=24297.0,
                                                    views=57160324.0)]})
    return full_data()


def test_every_scope_keeps_the_same_sections_in_the_same_order():
    """A scoped report must be comparable with the whole one, section by
    section. Dropping a section would hide the gap instead of stating it."""
    for scope in sr.REPORT_SCOPES:
        report = sr.assemble(scoped_data(scope), "full")
        ids = [s["id"] for s in report["sections"]]
        assert ids == list(sr.FULL_SECTION_IDS), scope
        for section in report["sections"]:
            _assert_section(section)


def test_a_section_a_scope_cannot_answer_says_which_stream_it_needed():
    cases = {
        "competitors": ["industry_signals"],
        "news": ["competitor_conversation", "competitor_content"],
        "viral": ["competitor_conversation", "competitor_content"],
    }
    for scope, blocked in cases.items():
        report = sr.assemble(scoped_data(scope), "full")
        by_id = {s["id"]: s for s in report["sections"]}
        label = sr.SCOPE_LABELS[scope]
        for sid in blocked:
            section = by_id[sid]
            assert section["available"] is False, f"{scope}/{sid}"
            reason = section["unavailable_reason"]
            assert reason and (f"covers {label} only" in reason or
                               "not included" in reason), f"{scope}/{sid}"
            # and it says what the section actually measures, so the reader can
            # tell "wrong report" from "no data"
            assert len(reason) > 60
            if sid == "industry_signals":
                assert "not included" in section["basis"]
                assert section["body"] == {"groups": []}
            else:
                assert "Not applicable at this scope" in section["basis"]
                if sid == "competitor_conversation":
                    assert section["body"] == {"columns": [], "rows": [], "align": []}
                else:
                    assert section["body"] == {"groups": []}


def test_the_sections_a_scope_CAN_answer_are_not_blocked():
    comp = {s["id"]: s for s in sr.assemble(scoped_data("competitors"), "full")["sections"]}
    assert comp["competitor_conversation"]["available"] is True
    assert comp["competitor_content"]["available"] is True
    viral = {s["id"]: s for s in sr.assemble(scoped_data("viral"), "full")["sections"]}
    assert viral["industry_signals"]["available"] is True


def test_a_blocked_section_never_borrows_a_number_from_another_stream():
    """The whole point of `available: False`: nothing is substituted."""
    report = sr.assemble(scoped_data("news"), "full")
    by_id = {s["id"]: s for s in report["sections"]}
    for sid in ("competitor_conversation", "competitor_content"):
        body = by_id[sid]["body"]
        if sid == "competitor_conversation":
            assert body["rows"] == []
            assert body["columns"] == []
        else:
            assert body["groups"] == []


def test_meta_and_basis_state_the_scope_in_the_title_and_the_notes():
    report = sr.assemble(scoped_data("news"), "full")
    assert report["meta"]["scope"] == "news"
    assert report["meta"]["scope_label"] == "Industry Signal"
    # the title is what lands on the cover, the tab and the filename
    assert report["meta"]["title"].endswith("- Industry Signal")
    notes = " ".join(report["basis"]["notes"])
    assert "scoped to Industry Signal only" in notes
    assert "Competitors / Sentiment" in notes and "Viral Social Trends" in notes


def test_an_out_of_scope_count_is_none_not_zero():
    """0 says "we looked and found none". None says "this report did not look",
    and every surface that prints these counts skips a None."""
    basis = sr.assemble(scoped_data("viral"), "full")["basis"]
    assert basis["viral_trends"] == 2
    assert basis["competitor_posts"] is None
    assert basis["competitor_handles"] is None
    assert basis["news_items"] is None


def test_scope_all_is_still_the_report_it_always_was():
    """The additive parameter must not change the default document."""
    data = full_data()
    explicit = sr.ReportData(
        now=data.now, days=data.days, win_from=data.win_from, win_to=data.win_to,
        prev_from=data.prev_from, prev_to=data.prev_to, scope="all",
        competitor_rows=data.competitor_rows, news_rows=data.news_rows,
        trend_rows=data.trend_rows, sightings=data.sightings,
        competitors=data.competitors, news_sources=data.news_sources,
        coverage=data.coverage)
    for mode in sr.REPORT_MODES:
        assert sr.assemble(explicit, mode) == sr.assemble(data, mode)
    assert sr.assemble(data, "full")["meta"]["title"] == "SMAA Social Intelligence Report"
    assert sr.assemble(data, "full")["basis"]["scope"] == "all"


def test_an_unknown_scope_falls_back_to_all_rather_than_raising():
    """The router 422s an unknown scope; every other caller gets a report."""
    assert sr.clean_scope("meltwater") == "all"
    assert sr.clean_scope(None) == "all"
    assert sr.clean_scope("VIRAL") == "viral"


def test_simple_mode_shows_only_the_groups_in_scope():
    section = sr.build_signals(scoped_data("viral"))
    assert [g["group"] for g in section["body"]["groups"]] == ["Viral Social Trends"]
    assert "scoped to Viral Social Trends only" in section["basis"]
    # ...and it does not claim the other two streams were empty
    assert "No rows in this window for" not in section["basis"]


def test_an_empty_scoped_window_says_this_stream_not_all_three():
    section = sr.build_signals(make_data(scope="news"))
    assert section["available"] is False
    assert "on this stream" in section["unavailable_reason"]
    assert "three streams" not in section["unavailable_reason"]


def test_mention_volume_counts_only_the_streams_in_scope():
    section = sr.build_mention_volume(scoped_data("competitors"))
    labels = [i["label"] for i in section["body"]["items"]]
    assert "Competitor posts" in labels
    assert "Industry news items" not in labels
    assert "Viral trends first seen" not in labels
    by_label = {i["label"]: i for i in section["body"]["items"]}
    assert by_label["All items"]["value"] == "2"
    assert "in scope" in by_label["All items"]["note"]


def test_reputation_risk_on_a_non_viral_scope_never_prints_zero_of_zero():
    """Only viral trends carry a safety or sensitivity verdict. "0 of 0 flagged"
    would read as "we checked and everything is clean"; we did not check."""
    section = sr.build_reputation_risk(scoped_data("competitors"))
    assert "0 of 0" not in section["basis"]
    assert "only ever run on Viral Social Trends" in section["basis"]
    assert section["estimated"] is False
    assert section["available"] is False
    assert "does not cover" in section["unavailable_reason"]


def test_data_basis_leads_with_the_scope_and_drops_the_shares_line():
    """Instagram shares are a competitor-post gap. On a news-only report there
    is no Instagram media in the document for the sentence to be about."""
    items = sr.build_data_basis(scoped_data("news"))["body"]["items"]
    assert items[1].startswith("This is a Industry Signal-only view")
    blob = " ".join(items)
    assert "Competitors / Sentiment:" not in blob
    assert "shares" not in blob.lower()
    # the absences that DO still apply are still named
    assert "audience sentiment" in blob.lower()
    assert "audience demographics" in blob.lower()


def test_tone_is_assessed_only_for_the_stream_in_scope():
    data = scoped_data("viral")
    keys = {t["key"] for t in sr.tone_targets(data, "full")}
    assert keys and all(k.startswith("viral:") for k in keys)


def test_a_scoped_tone_pass_reads_a_third_of_the_texts_not_the_same_sixty():
    """The tone pass is the slowest step in the report. A per-tab download that
    reads the same 60 texts would be no faster than the one it replaces."""
    many_news = [_news(i, days=1, score=float(i)) for i in range(200)]
    scoped = make_data(scope="news", news_rows=many_news)
    assert len(sr.tone_targets(scoped, "full")) == sr.TONE_PER_STREAM
    assert sr.TONE_PER_STREAM * 3 <= sr.TONE_MAX_ITEMS


# ===========================================================================
# ITEM REPORTS
# ===========================================================================

def _research() -> dict:
    return {
        "what_it_is": "A fan-made campaign for a film that does not exist.",
        "why_people_post": "People make fake trailers and react to them.",
        "sources": [{"title": "wikipedia.org", "uri": "https://en.wikipedia.org/x"},
                    {"title": "fandom.com", "uri": "https://fandom.com/y"}],
        "researched_at": NOW.isoformat(),
    }


def trend_item(*, trend=None, peers=None, sightings=None, research=None,
               days: int = 30) -> sr.ItemData:
    row = trend if trend is not None else _trend(
        880, days=1, velocity=0.62, score=61.0, angle="Make a transition reel")
    others = list(peers if peers is not None else
                  [_trend(i, days=2, velocity=0.1 * (i % 5)) for i in (101, 102, 103)])
    sights = list(sightings if sightings is not None else [
        _sighting(880, key="AE", posts=24297.0, views=57160324.0,
                  views_per_post=2353.0, growth_ratio=1.9),
        _sighting(880, key="SA", posts=37372.0),
    ])
    data = make_data(scope="viral", days=days, trend_rows=[row] + others,
                     sightings={880: sights})
    return sr.ItemData(kind="trend", stream=sr.STREAM_VIRAL, row=row, data=data,
                       sightings=sights, research=research)


def post_item(*, post=None, peers=None, account=None,
              days: int = 30) -> sr.ItemData:
    row = post if post is not None else _post(
        146, username="hub71ad", days=3, likes=174, comments=8, views=None,
        outperformance=3.37)
    others = list(peers if peers is not None else
                  [_post(i, username="hub71ad", days=4, likes=10 * i, comments=1,
                         outperformance=0.5 * i) for i in (1, 2, 3)])
    comp = account if account is not None else _competitor("hub71ad")
    data = make_data(scope="competitors", days=days,
                     competitor_rows=[row] + others, competitors=[comp])
    return sr.ItemData(kind="post", stream=sr.STREAM_COMPETITOR, row=row,
                       data=data, account=comp)


def news_item(*, news=None, peers=None, days: int = 30) -> sr.ItemData:
    row = news if news is not None else _news(363, days=2, coverage=3)
    others = list(peers if peers is not None else
                  [_news(i, days=3, coverage=1) for i in (10, 11, 12)])
    data = make_data(scope="news", days=days, news_rows=[row] + others)
    return sr.ItemData(kind="post", stream=sr.STREAM_NEWS, row=row, data=data,
                       news_source={"key": "gn_gulf_ai", "name": "Gulf AI",
                                    "url": "https://news.google.com/rss/x"})


ITEM_FIXTURES = {"trend": trend_item, "post": post_item, "news": news_item}


def test_every_item_report_satisfies_the_same_contract():
    for name, make in ITEM_FIXTURES.items():
        for mode in sr.REPORT_MODES:
            report = sr.assemble_item(make(), mode)
            assert set(report) == {"meta", "basis", "sections"}
            assert report["meta"]["mode"] == mode
            assert report["meta"]["item"]["kind"] in sr.ITEM_KINDS
            assert report["sections"], f"{name}/{mode}"
            for section in report["sections"]:
                _assert_section(section)


def test_a_simple_item_report_is_the_one_page_brief():
    report = sr.assemble_item(trend_item(research=_research()), "simple")
    ids = [s["id"] for s in report["sections"]]
    assert ids == ["item_overview", "item_what_it_is", "item_research",
                   "item_basis"]
    # no narrative slot, so a simple item report costs no model call at all
    assert "recommendations" not in ids


def test_a_full_item_report_adds_the_ranking_evidence_timeline_and_ai_read():
    report = sr.assemble_item(trend_item(research=_research()), "full")
    ids = [s["id"] for s in report["sections"]]
    assert ids == list(sr.ITEM_SECTION_IDS_FULL)
    assert "item_evidence" in ids and "item_ranking" in ids


def test_a_post_drops_the_trend_only_sections_instead_of_faking_them():
    """An Instagram post has no sightings and no research cache. "Every
    Sighting: not enough data" would imply one was expected and missing."""
    for make in (post_item, news_item):
        ids = [s["id"] for s in sr.assemble_item(make(), "full")["sections"]]
        assert "item_evidence" not in ids
        assert "item_research" not in ids
        assert ids == [i for i in sr.ITEM_SECTION_IDS_FULL
                       if i not in sr.ITEM_TREND_ONLY_SECTIONS]


def test_an_item_report_prints_the_two_absences_exactly_as_the_report_does():
    metrics = {m["label"]: m["value"] for m in
               sr.build_item_overview(post_item())["body"]["groups"][0]["items"][0]["metrics"]}
    assert metrics[sr.SHARES_LABEL] == sr.SHARES_VALUE
    assert "not available" in metrics[sr.SHARES_LABEL]
    assert metrics[sr.SHARES_LABEL] != "0"
    for make in ITEM_FIXTURES.values():
        item = make()
        metrics = {m["label"]: m["value"] for m in
                   sr.build_item_overview(item)["body"]["groups"][0]["items"][0]["metrics"]}
        assert metrics[sr.SENTIMENT_METRIC_LABEL] == sr.SENTIMENT_METRIC_VALUE
        assert "no comment text" in metrics[sr.SENTIMENT_METRIC_LABEL]


def test_a_news_item_never_shows_a_zero_where_it_has_no_engagement():
    """News is a feed entry: there is no engagement counter anywhere, and a 0
    would be read as one."""
    metrics = {m["label"]: m["value"] for m in
               sr.build_item_overview(news_item())["body"]["groups"][0]["items"][0]["metrics"]}
    assert metrics["Engagement"] == sr.NEWS_ENGAGEMENT_VALUE
    assert "not collected" in metrics["Engagement"]
    assert sr.SHARES_LABEL not in metrics      # Instagram's gap, not the feed's


def test_the_measured_numbers_of_a_trend_come_from_its_own_sightings():
    section = sr.build_item_overview(trend_item())
    metrics = {m["label"]: m["value"]
               for m in section["body"]["groups"][0]["items"][0]["metrics"]}
    assert metrics["Posts"] == "37,372"          # the largest regional reading
    assert metrics["Views"] == "57,160,324"
    assert metrics["Evidence sightings"] == "5"
    # ...and the basis says out loud that those are maxima, not sums
    assert "NOT a sum across regions" in section["basis"]


def test_a_missing_suggestion_on_an_item_says_so_rather_than_inventing_one():
    item = post_item(post=_post(146, angle=None))
    suggestion = sr.build_item_overview(item)["body"]["groups"][0]["items"][0]["suggestion"]
    assert "No angle stored" in suggestion


def test_what_it_is_quotes_what_is_stored_and_names_each_source():
    section = sr.build_item_what_it_is(trend_item(research=_research()))
    blob = " ".join(section["body"]["paragraphs"])
    assert "A TikTok hashtag" in blob                       # the stored summary
    assert "AI read of it:" in blob                         # labelled as AI
    assert "Web research: A fan-made campaign" in blob      # labelled as research
    assert "the AI scorer's stored topic" in section["basis"]
    assert "Google-grounded lookup" in section["basis"]


def test_what_it_is_is_unavailable_rather_than_written_from_scratch():
    bare = _trend(880, angle=None)
    for key in ("summary", "topic", "topic_summary"):
        bare[key] = None
    section = sr.build_item_what_it_is(trend_item(trend=bare))
    assert section["available"] is False
    assert "nothing has been written for it here" in section["unavailable_reason"]


def test_research_that_was_never_run_says_how_it_gets_run():
    section = sr.build_item_research(trend_item())
    assert section["available"] is False
    assert "Brainstorm or Make this" in section["unavailable_reason"]
    assert "invented" in section["unavailable_reason"]


def test_research_sources_are_listed_so_the_description_can_be_checked():
    section = sr.build_item_research(trend_item(research=_research()))
    assert section["available"] is True
    assert [row[0] for row in section["body"]["rows"]] == ["wikipedia.org",
                                                           "fandom.com"]
    assert section["estimated"] is True


# --- the ranking maths -----------------------------------------------------

def test_rank_within_places_a_row_and_states_its_denominator():
    peers = [{"id": i, "engagement": e}
             for i, e in enumerate([10, 90, 50, 30, 70], start=1)]
    ranked = sr.rank_within(peers, {"id": 3, "engagement": 50}, "engagement")
    assert ranked["rank"] == 3 and ranked["total"] == 5
    assert ranked["value"] == 50 and ranked["median"] == 50


def test_rank_within_lets_ties_share_the_better_position():
    """Two identical posts are both 3rd of 5. Splitting them by row order would
    invent a difference the numbers do not contain."""
    peers = [{"engagement": v} for v in [90, 70, 50, 50, 10]]
    for row in ({"engagement": 50},):
        assert sr.rank_within(peers, row, "engagement")["rank"] == 3


def test_rank_within_refuses_to_rank_a_row_with_no_value():
    peers = [{"engagement": v} for v in [90, 70]]
    ranked = sr.rank_within(peers, {"engagement": None}, "engagement")
    assert ranked["rank"] is None and ranked["total"] == 2
    kpi = sr.rank_kpi("Rank by engagement", ranked, noun="post(s)",
                      unit="engagement count")
    assert kpi["value"] == "not ranked"
    assert "carries no engagement count" in kpi["note"]


def test_rank_within_ignores_peers_that_carry_no_value():
    peers = [{"engagement": 90}, {"engagement": None}, {"engagement": 10}]
    ranked = sr.rank_within(peers, {"engagement": 10}, "engagement")
    assert (ranked["rank"], ranked["total"]) == (2, 2)


def test_ordinals_survive_the_teens():
    assert [sr._ordinal(n) for n in (1, 2, 3, 4, 11, 12, 13, 21, 22, 101, 111)] == \
        ["1st", "2nd", "3rd", "4th", "11th", "12th", "13th", "21st", "22nd",
         "101st", "111th"]


def test_a_post_is_ranked_third_of_twenty_against_its_own_stream():
    # 19 peers on 10..190 engagement, this post on 170: exactly two beat it.
    row = _post(500, username="hub71ad", days=2, likes=170, comments=0,
                outperformance=2.0)
    peers = [_post(i, username="hub71ad", days=3, likes=i * 10, comments=0,
                   outperformance=0.1 * i) for i in range(1, 20)]
    section = sr.build_item_ranking(post_item(post=row, peers=peers))
    assert section["available"] is True
    assert section["estimated"] is False, "ranks are measured, never a judgement"
    by_label = {i["label"]: i for i in section["body"]["items"]}
    assert by_label["Rank by engagement"]["value"] == "3rd of 20"
    assert "among the 20 competitor post(s)" in by_label["Rank by engagement"]["note"]
    assert "this item 170" in by_label["Rank by engagement"]["note"]
    assert "window median" in by_label["Rank by engagement"]["note"]


def test_the_ranking_names_the_accounts_own_median_beside_the_position():
    section = sr.build_item_ranking(post_item())
    by_label = {i["label"]: i for i in section["body"]["items"]}
    kpi = by_label["Its account's own median engagement"]
    assert kpi["value"] == "54"
    assert "@hub71ad" in kpi["note"] and "111" in kpi["note"]


def test_an_item_outside_the_window_is_not_ranked_and_says_which_window_would():
    old = _post(146, username="hub71ad", days=45, likes=174, comments=8)
    section = sr.build_item_ranking(post_item(post=old))
    assert section["available"] is False
    reason = section["unavailable_reason"]
    assert "outside the" in reason and "window this report ranks against" in reason
    assert "A window of 46 days would reach it" in reason
    assert "guess" in reason


def test_an_item_held_back_by_the_safety_screen_says_that_instead():
    flagged = _trend(880, days=1, velocity=0.62, sensitivity="avoid")
    section = sr.build_item_ranking(trend_item(trend=flagged))
    assert section["available"] is False
    assert "held back" in section["unavailable_reason"]
    assert "safety screen" in section["unavailable_reason"]


def test_a_trend_is_ranked_on_measured_columns_only():
    section = sr.build_item_ranking(trend_item())
    labels = [i["label"] for i in section["body"]["items"]]
    assert labels == ["Rank by heat", "Rank by evidence sightings",
                      "Rank by number of sources"]
    assert "opportunity score is deliberately NOT ranked" in section["basis"]
    assert section["estimated"] is False


def test_a_news_item_is_ranked_on_recency_and_nothing_it_cannot_measure():
    """"Rank by outlets carrying the story" used to lead this section. Nothing
    measures that now, and inventing a second dimension would be worse than
    printing one."""
    section = sr.build_item_ranking(news_item())
    by_label = {i["label"]: i for i in section["body"]["items"]}
    assert by_label["Rank by recency"]["value"] == "1st of 4"
    assert "Rank by outlets carrying the story" not in by_label
    assert section["estimated"] is False


# --- the rest of the full item report --------------------------------------

def test_the_timeline_says_whose_clock_each_row_is():
    rows = sr.build_item_timeline(trend_item())["body"]["rows"]
    events = {row[0]: row for row in rows}
    assert "First seen by us" in events
    assert "the first collector run" in events["First seen by us"][2]
    section = sr.build_item_timeline(trend_item())
    assert "not when it started on the internet" in section["basis"]


def test_the_ai_read_is_labelled_as_judgement_top_to_bottom():
    trend = _trend(880, days=1)
    trend["metadata"] = {"replication": "Shoot it on a phone in the lab.",
                         "confidence": "high"}
    section = sr.build_item_ai_read(trend_item(trend=trend))
    assert section["estimated"] is True
    fields = {row[0]: row[1] for row in section["body"]["rows"]}
    assert fields["Fit for 42"] == "80/100"
    assert fields["UAE relevance"] == "85/100"
    assert fields["Type of trend"] == "format"
    assert fields["How 42 would replicate it"] == "Shoot it on a phone in the lab."
    assert "written by a model, not measured" in section["basis"]
    assert "opinion expressed as a number" in section["basis"]


def test_an_unscored_item_has_no_ai_read_rather_than_a_blank_table():
    bare = _trend(880, angle=None)
    for key in ("fit_score", "fit_reason", "uae_relevance", "theme", "trend_kind",
                "suggested_format", "effort", "opportunity_score", "sensitivity",
                "safety", "safety_reason"):
        bare[key] = None
    section = sr.build_item_ai_read(trend_item(trend=bare))
    assert section["available"] is False
    assert "has not run on this item" in section["unavailable_reason"]


def test_the_evidence_table_lists_every_sighting_with_its_own_numbers():
    section = sr.build_item_evidence(trend_item())
    assert len(section["body"]["rows"]) == 2
    assert section["estimated"] is False
    metrics = [row[2] for row in section["body"]["rows"]]
    assert all("posts" in m for m in metrics)
    assert "no counter published" not in metrics
    assert str(sr.ITEM_EVIDENCE_MAX) in section["basis"]


def test_a_sighting_with_no_counter_says_so_rather_than_showing_zero():
    quiet = _sighting(880, key="none")
    quiet["metric_value"] = None
    rows = sr.build_item_evidence(trend_item(sightings=[quiet]))["body"]["rows"]
    assert rows[0][2] == "no counter published"


def test_the_evidence_table_reconciles_itself_with_the_rows_stored_count():
    item = trend_item()
    item.row["evidence_count"] = 9
    assert "the row's own evidence_count is 9" in \
        sr.build_item_evidence(item)["basis"].lower()


def test_the_item_basis_names_the_population_and_every_absence():
    items = sr.build_item_basis(post_item())["body"]["items"]
    blob = " ".join(items).lower()
    assert "this report is about one item" in blob
    assert "ranked against the 4 competitor post(s)" in blob
    assert "not measured - shares" in blob
    assert "audience sentiment" in blob
    assert "42 abu dhabi" in blob


def test_the_item_meta_carries_everything_the_filename_and_the_ui_need():
    meta = sr.assemble_item(trend_item(), "full")["meta"]
    assert meta["item"] == {"kind": "trend", "id": 880, "stream": "viral",
                            "stream_label": "Viral Social Trends",
                            "label": "Trend 880", "slug": "trend-880",
                            "url": "https://www.tiktok.com/tag/trend880"}
    assert meta["scope"] == "viral"
    assert meta["title"] == "Trend 880"


def test_the_item_basis_block_counts_the_population_not_the_item():
    basis = sr.assemble_item(news_item(), "simple")["basis"]
    assert basis["news_items"] == 4
    assert basis["competitor_posts"] is None
    assert "about ONE Industry Signal item" in basis["notes"][0]
    assert "population it is ranked against" in basis["notes"][1]


def test_an_item_slug_is_filename_safe_whatever_the_title_contains():
    nasty = _trend(880)
    nasty["slug"] = None
    nasty["title"] = '#Spider-Man: "Brand New Day"!! 🕷'
    item = trend_item(trend=nasty)
    assert item.slug == "spider-man-brand-new-day"
    assert re.fullmatch(r"[a-z0-9-]+", item.slug)


def test_the_item_tone_pass_reads_exactly_one_text():
    """37 seconds is a broken feature when the owner is waiting on a dialog."""
    for make in ITEM_FIXTURES.values():
        targets = sr.item_tone_targets(make())
        assert len(targets) == 1
        assert targets[0]["text"]
        assert len(targets[0]["text"]) <= sr.TONE_TEXT_CHARS


def test_an_item_with_no_text_at_all_costs_no_model_call():
    bare = _trend(880)
    for key in ("topic_summary", "summary", "title"):
        bare[key] = None
    assert sr.item_tone_targets(trend_item(trend=bare)) == []


def test_the_item_tone_is_labelled_ai_assessed_like_everywhere_else():
    item = post_item()
    item.tone = {sr.tone_key("competitor", item.row):
                 {"label": "Positive", "estimated": True}}
    sentiment = sr.build_item_overview(item)["body"]["groups"][0]["items"][0]["sentiment"]
    assert sentiment == {"label": "Positive", "estimated": True}


def test_an_item_report_never_prints_a_share_count_as_a_number():
    for make in ITEM_FIXTURES.values():
        for mode in sr.REPORT_MODES:
            blob = repr(sr.assemble_item(make(), mode)).lower()
            for lie in ("'shares', 'value': '0'", "0 shares", "shares: 0"):
                assert lie not in blob


# ---------------------------------------------------------------------------
# A chosen window must not describe itself as the trailing one
#
# Every sentence that names the span used to read "the last 30 days", which is
# simply false of a window that ended a fortnight ago — and false in the one
# way a reader cannot catch, because the numbers look current.
# ---------------------------------------------------------------------------

def test_a_trailing_window_still_reads_as_the_last_n_days():
    data = make_data(days=7)
    assert sr._in_window(data) == "the last 7 days"


def test_a_chosen_window_names_the_day_it_ended_on():
    data = make_data(days=7)
    data.chosen_end = True
    assert sr._in_window(data) == "the 7 days to " + sr._day(data.win_to)
    assert "last" not in sr._in_window(data)


def test_the_window_meta_says_whether_it_was_chosen():
    """A stored report is opened weeks later. Without this flag, "the 30 days
    to 14 Sep" is indistinguishable from a trailing report that went stale on
    the shelf, and the two deserve different readings."""
    trailing = sr.assemble(make_data(days=30), "simple")["meta"]["window"]
    assert trailing["chosen"] is False

    data = make_data(days=30)
    data.chosen_end = True
    assert sr.assemble(data, "simple")["meta"]["window"]["chosen"] is True


def test_a_chosen_window_is_dated_when_it_was_built_not_when_it_ended():
    """`generated_at` is the clock. For a past window it is NOT the window end,
    or a report built today would claim to have been written a fortnight ago."""
    data = make_data(days=30)
    data.chosen_end = True
    data.built_at = NOW + timedelta(days=14)
    meta = sr.assemble(data, "simple")["meta"]
    assert meta["generated_at"].startswith((NOW + timedelta(days=14)).date().isoformat())
    assert meta["window"]["to"].startswith(data.win_to.date().isoformat())


def test_the_window_end_is_the_only_thing_a_chosen_window_moves():
    """The arithmetic is the same arithmetic. If a past window needed its own
    maths, every section would have to be re-verified against it."""
    trailing = sr.make_window(7, NOW)
    chosen = sr.make_window(7, NOW - timedelta(days=14))
    assert chosen["days"] == trailing["days"]
    assert trailing["win_to"] - trailing["win_from"] == chosen["win_to"] - chosen["win_from"]
    assert chosen["win_to"] == NOW - timedelta(days=14)
    assert chosen["prev_to"] == chosen["win_from"]


# ---------------------------------------------------------------------------
# Whole calendar days
#
# A rolling window covers days+1 dates, the first and last partly. That is fine
# for "the last 30 days" and wrong the moment someone drags across a fortnight
# in a calendar: they mean those days, not a window that starts at the time of
# day they happened to click.
# ---------------------------------------------------------------------------

def test_a_whole_day_window_is_exactly_that_many_dates():
    win = sr.make_window(7, date(2026, 9, 8), whole_days=True)
    assert win["win_from"].date() == date(2026, 9, 2)
    assert win["win_to"].date() == date(2026, 9, 8)
    span = (win["win_to"].date() - win["win_from"].date()).days + 1
    assert span == 7, "1 Aug to 8 Aug is eight days, not seven; so is this"
    assert win["win_from"].hour == 0
    assert win["win_to"].hour == 23


def test_a_whole_day_window_compares_against_the_run_before_it():
    win = sr.make_window(7, date(2026, 9, 8), whole_days=True)
    assert win["prev_to"] == win["win_from"], "no gap between the two windows"
    assert win["prev_from"].date() == date(2026, 8, 26)
    assert (win["prev_to"] - win["prev_from"]).days == 7, (
        "the comparison has to be like for like: two runs of the same length, "
        "neither of them clipped"
    )


def test_a_bare_date_is_read_as_the_end_of_that_day():
    """The picker sends a day, not a moment. An end of midnight would drop the
    day the reader actually clicked."""
    win = sr.make_window(1, date(2026, 9, 8), whole_days=True)
    assert win["win_to"].date() == date(2026, 9, 8)
    assert win["win_from"].date() == date(2026, 9, 8)


def test_the_rolling_window_is_untouched():
    """Item reports and every direct caller still get what they always got: a
    window that ends now and begins `days` days ago to the minute."""
    win = sr.make_window(7, NOW)
    assert win["win_to"] == NOW
    assert win["win_from"] == NOW - timedelta(days=7)
    assert win["prev_from"] == NOW - timedelta(days=14)
