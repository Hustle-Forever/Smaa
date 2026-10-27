"""Config parsing + API plumbing for the General Trends feed.

Two things are tested here that are invisible to reading the files:

* **The two new env parsers.** ``GENERAL_TRENDS_SOURCE_CAPS`` and
  ``GENERAL_TRENDS_ALLOW_TERMS`` are typed by hand rather than by the generic
  ``_list``/``_int`` helpers, and both are edited by a human under pressure —
  the caps map when a source floods the feed, the allow list when the safety
  screen produces a false positive. A malformed entry in either MUST cost that
  one entry and nothing else: a config parse error at import time takes the
  whole backend down, which is a far worse outcome than one uncapped source.

* **The feed windows actually reaching the store.** ``query_trends`` took
  ``since_days=14`` as a hard-coded default that no HTTP parameter could
  reach, so the documented "last 7 days" feed was silently a 14-day feed and
  the owner had no way to widen it. The pass-through is asserted against a
  monkeypatched store rather than a database, because the bug was in the call,
  not in the SQL.

The defaults tests double as the record of two deliberate source decisions
(x_trends dropped, 7 subreddits cut to 4) — if someone puts them back, these
fail and they have to read why.
"""
from __future__ import annotations

import dataclasses
import sys
from pathlib import Path

import pytest

_BACKEND = Path(__file__).resolve().parent.parent
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from fastapi.testclient import TestClient  # noqa: E402

from app import config as cfgmod  # noqa: E402
from app.routers import general_trends as rt  # noqa: E402


# Every env var that can move a value this suite asserts a default for.
_GT_ENV = (
    "GENERAL_TRENDS_SOURCES",
    "GENERAL_TRENDS_GEOS",
    "GENERAL_TRENDS_SUBREDDITS",
    "GENERAL_TRENDS_REFRESH_HOURS",
    "GENERAL_TRENDS_MAX_ITEMS_PER_SOURCE",
    "GENERAL_TRENDS_SOURCE_CAPS",
    "GENERAL_TRENDS_FEED_DAYS",
    "GENERAL_TRENDS_MAX_CONTENT_AGE_DAYS",
    "GENERAL_TRENDS_SAFETY",
    "GENERAL_TRENDS_ALLOW_TERMS",
    "GENERAL_TRENDS_DROP_UNDATED",
    "GENERAL_TRENDS_MODEL",
    "GENERAL_TRENDS_TRIAGE_MODEL",
    "GENERAL_TRENDS_TIKTOK_REGIONS",
    "GENERAL_TRENDS_IG_HASHTAGS",
    "GENERAL_TRENDS_IG_WATCH_ACCOUNTS",
)


@pytest.fixture
def clean_env(monkeypatch):
    """A process env with every General Trends var unset.

    The dev machine's real backend/.env is loaded into os.environ at import
    time, so without this a local override would make the default assertions
    pass or fail for the wrong reason.
    """
    for name in _GT_ENV:
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


def _cfg(monkeypatch, **env: str):
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    return cfgmod.Config.from_env()


# ---------------------------------------------------------------------------
# GENERAL_TRENDS_SOURCE_CAPS  ->  dict[str, int]
# ---------------------------------------------------------------------------

def test_source_caps_parse_a_well_formed_map(clean_env):
    cfg = _cfg(clean_env, GENERAL_TRENDS_SOURCE_CAPS="reddit:15,kym:40")
    assert cfg.general_trends_source_caps == {"reddit": 15, "kym": 40}


def test_source_caps_tolerate_whitespace(clean_env):
    cfg = _cfg(clean_env, GENERAL_TRENDS_SOURCE_CAPS=" reddit : 15 ,  kym:40 ")
    assert cfg.general_trends_source_caps == {"reddit": 15, "kym": 40}


def test_source_caps_ignore_malformed_pairs_instead_of_raising(clean_env):
    """A typo costs that source its override, never the backend's boot."""
    cfg = _cfg(
        clean_env,
        # no colon / no value / no key / not a number / stray comma
        GENERAL_TRENDS_SOURCE_CAPS="reddit,kym:,:9,lemmy:abc,,github:12",
    )
    assert cfg.general_trends_source_caps == {"github": 12}


def test_source_caps_empty_string_is_an_empty_map(clean_env):
    assert _cfg(clean_env, GENERAL_TRENDS_SOURCE_CAPS="").general_trends_source_caps == {}


def test_source_caps_last_write_wins_on_a_duplicate_key(clean_env):
    cfg = _cfg(clean_env, GENERAL_TRENDS_SOURCE_CAPS="reddit:15,reddit:5")
    assert cfg.general_trends_source_caps == {"reddit": 5}


def test_source_caps_accept_a_zero_and_a_negative(clean_env):
    """0 means "fetch nothing from this source" — a legitimate kill switch, so
    the parser must not silently treat it as "unset"."""
    cfg = _cfg(clean_env, GENERAL_TRENDS_SOURCE_CAPS="x_trends:0,reddit:-1")
    assert cfg.general_trends_source_caps == {"x_trends": 0, "reddit": -1}


# ---------------------------------------------------------------------------
# GENERAL_TRENDS_ALLOW_TERMS  ->  list[str], lower-cased
# ---------------------------------------------------------------------------

def test_allow_terms_are_lowercased_and_trimmed(clean_env):
    cfg = _cfg(clean_env, GENERAL_TRENDS_ALLOW_TERMS=" Horny Toad , SCUNTHORPE ")
    assert cfg.general_trends_allow_terms == ["horny toad", "scunthorpe"]


def test_allow_terms_drop_blanks_and_duplicates(clean_env):
    cfg = _cfg(clean_env, GENERAL_TRENDS_ALLOW_TERMS="a,,  ,A,b")
    assert cfg.general_trends_allow_terms == ["a", "b"]


def test_allow_terms_default_to_empty(clean_env):
    assert cfgmod.Config.from_env().general_trends_allow_terms == []


# ---------------------------------------------------------------------------
# GENERAL_TRENDS_SAFETY  — fails closed
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    ("on", "on"), ("off", "off"), ("flag_only", "flag_only"),
    ("OFF", "off"), (" Flag_Only ", "flag_only"),
])
def test_safety_accepts_the_three_modes_case_insensitively(clean_env, raw, expected):
    assert _cfg(clean_env, GENERAL_TRENDS_SAFETY=raw).general_trends_safety == expected


@pytest.mark.parametrize("raw", ["", "of", "no", "true", "disabled"])
def test_unknown_safety_mode_falls_back_to_on(clean_env, raw):
    """A typo must never be the thing that switches the content screen off."""
    assert _cfg(clean_env, GENERAL_TRENDS_SAFETY=raw).general_trends_safety == "on"


# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------

def test_new_general_trends_defaults(clean_env):
    cfg = cfgmod.Config.from_env()
    # Reddit is throttled to ~1 req/60s and only corroborates; the KYM lanes and
    # the 50-row Shorts chart are format registries, so they get the room.
    # youtube_shorts is capped WELL below the chart's 50 rows: at 50 x 3 regions
    # it minted 103 of 167 cards on the first live run and the feed became a
    # song list. The chart is rank-ordered, so the top 15 keeps the most-used
    # sounds and leaves room for formats and memes.
    assert cfg.general_trends_source_caps == {
        "reddit": 15, "kym": 40, "kym_entries": 40,
        "kym_editorials": 10, "youtube_shorts": 15,
    }
    assert cfg.general_trends_shorts_regions == ["AE", "SA", "US"]
    # The format registries lead the source list — they are the only sources
    # allowed to create a card, so they ARE the feed.
    assert cfg.general_trends_sources[:6] == [
        "tiktok", "kym", "kym_entries", "kym_editorials",
        "youtube_shorts", "instagram",
    ]
    assert "x_trends" not in cfg.general_trends_sources
    assert cfg.general_trends_feed_days == 7
    assert cfg.general_trends_max_content_age_days == 30
    assert cfg.general_trends_safety == "on"
    assert cfg.general_trends_allow_terms == []
    assert cfg.general_trends_drop_undated is False
    assert cfg.general_trends_triage_model == ""
    # Creative Center gives only 3 hashtags per (region, period), so region
    # breadth is the only lever on how many format candidates a run sees.
    # PH/ID/MY are here because of who lives in the UAE, not for coverage.
    assert cfg.general_trends_tiktok_regions == [
        "AE", "SA", "EG", "PH", "ID", "MY", "US", "GB"]
    assert cfg.general_trends_kinds == ["meme", "format", "sound", "challenge"]
    assert cfg.general_trends_ig_hashtags == []
    assert cfg.general_trends_ig_watch_accounts == []


def test_default_sources_drop_x_trends_and_add_the_new_lanes(clean_env):
    """x_trends produced 80 cards and zero wins — 240/240 sightings with an
    empty summary and no metric. It stays in the code, not in the default."""
    sources = cfgmod.Config.from_env().general_trends_sources
    assert "x_trends" not in sources
    for sid in ("hacker_news", "lemmy", "github", "google_news",
                "tiktok", "instagram", "instagram_watch"):
        assert sid in sources, sid
    # the survivors from the old list
    for sid in ("google_trends", "bluesky", "reddit", "kym", "wikipedia", "youtube"):
        assert sid in sources, sid


def test_x_trends_can_still_be_switched_back_on(clean_env):
    cfg = _cfg(clean_env, GENERAL_TRENDS_SOURCES="kym,x_trends")
    assert cfg.general_trends_sources == ["kym", "x_trends"]


def test_default_subreddits_are_the_four_that_survived(clean_env):
    """r/videos (19/24 avoid), r/OutOfTheLoop (68% junk + the worst clustering
    over-fire), r/popular and r/TikTokCringe were measured out."""
    subs = cfgmod.Config.from_env().general_trends_subreddits
    assert subs == ["memes", "ProgrammerHumor", "Damnthatsinteresting", "technology"]


def test_drop_undated_reads_the_usual_truthy_words(clean_env):
    assert _cfg(clean_env, GENERAL_TRENDS_DROP_UNDATED="true").general_trends_drop_undated
    assert not _cfg(clean_env, GENERAL_TRENDS_DROP_UNDATED="false").general_trends_drop_undated


# ---------------------------------------------------------------------------
# Router: /items windows + include_filtered, /sources drop counters
# ---------------------------------------------------------------------------

async def _async(value):
    return value


@pytest.fixture
def client(monkeypatch):
    """Real app and router, fake store, ranking reduced to a pass-through.

    The app-wide shared-password gate is middleware that 401s every /api/*
    request before routing, so it is switched off here — this suite is about
    parameter plumbing, not authentication.
    """
    from app import access
    monkeypatch.setattr(access, "enabled", lambda: False)

    calls: dict[str, dict] = {}

    async def _query_trends(**kwargs):
        calls["query_trends"] = kwargs
        return [{"id": 1, "velocity": 0.5, "opportunity": 50.0}]

    monkeypatch.setattr(rt.store, "query_trends", _query_trends)
    monkeypatch.setattr(rt.store, "sightings_for", lambda ids, **k: _async({}))
    monkeypatch.setattr(rt.store, "latest_run", lambda: _async(calls.get("run")))
    # Isolate from the concurrent ranking rewrite: this suite asserts on what
    # reaches the store, not on the maths.
    monkeypatch.setattr(rt.ranking, "rank_trends", lambda rows, **k: list(rows))
    monkeypatch.setattr(rt.pipeline, "_fetch_plan", lambda: [
        {"source": "reddit", "region": "global"},
        {"source": "kym", "region": "global"},
    ])

    from app.main import app
    c = TestClient(app)
    c.calls = calls          # type: ignore[attr-defined]
    return c


def _with_config(monkeypatch, **fields):
    """Swap the router's frozen Config for one with these fields changed."""
    monkeypatch.setattr(rt, "config", dataclasses.replace(rt.config, **fields))


def test_items_defaults_come_from_config_not_from_a_hard_coded_14(client, monkeypatch):
    """The bug: query_trends(since_days=14) was hard-coded and unreachable."""
    _with_config(monkeypatch, general_trends_feed_days=7,
                 general_trends_max_content_age_days=30)
    assert client.get("/api/trends/general/items").status_code == 200
    kw = client.calls["query_trends"]           # type: ignore[attr-defined]
    assert kw["since_days"] == 7
    assert kw["content_days"] == 30
    assert kw["include_filtered"] is False


def test_items_explicit_windows_win_over_the_config(client, monkeypatch):
    _with_config(monkeypatch, general_trends_feed_days=7,
                 general_trends_max_content_age_days=30)
    r = client.get("/api/trends/general/items?days=30&content_days=90")
    assert r.status_code == 200, r.text
    kw = client.calls["query_trends"]           # type: ignore[attr-defined]
    assert kw["since_days"] == 30
    assert kw["content_days"] == 90


def test_items_zero_resolves_to_the_configured_default(client, monkeypatch):
    _with_config(monkeypatch, general_trends_feed_days=21,
                 general_trends_max_content_age_days=45)
    r = client.get("/api/trends/general/items?days=0&content_days=0")
    assert r.status_code == 200, r.text
    kw = client.calls["query_trends"]           # type: ignore[attr-defined]
    assert kw["since_days"] == 21
    assert kw["content_days"] == 45


def test_a_configured_zero_means_no_window_at_all(client, monkeypatch):
    """0 in the config is "don't window", not the zero-day window that would
    return an empty feed."""
    _with_config(monkeypatch, general_trends_feed_days=0,
                 general_trends_max_content_age_days=0)
    assert client.get("/api/trends/general/items").status_code == 200
    kw = client.calls["query_trends"]           # type: ignore[attr-defined]
    assert kw["since_days"] is None
    assert kw["content_days"] is None


def test_include_filtered_and_include_sensitive_are_independent(client):
    client.get("/api/trends/general/items?include_filtered=true")
    kw = client.calls["query_trends"]           # type: ignore[attr-defined]
    assert kw["include_filtered"] is True and kw["include_sensitive"] is False

    client.get("/api/trends/general/items?include_sensitive=true")
    kw = client.calls["query_trends"]           # type: ignore[attr-defined]
    assert kw["include_filtered"] is False and kw["include_sensitive"] is True


@pytest.mark.parametrize("qs", ["days=61", "days=-1", "content_days=91", "content_days=-1"])
def test_out_of_range_windows_are_rejected(client, qs):
    assert client.get(f"/api/trends/general/items?{qs}").status_code == 422


def test_existing_item_filters_still_reach_the_store(client):
    """Additive change: nothing the feed already sent may go missing."""
    client.get("/api/trends/general/items?status=saved&region=AE&source=kym"
               "&theme=tech&kind=meme&min_fit=40&q=robot")
    kw = client.calls["query_trends"]           # type: ignore[attr-defined]
    assert kw["status"] == "saved" and kw["region"] == "AE"
    assert kw["source"] == "kym" and kw["theme"] == "tech"
    assert kw["kind"] == "meme" and kw["min_fit"] == 40 and kw["q"] == "robot"


def test_sources_exposes_the_ingest_drop_counters(client):
    client.calls["run"] = {                     # type: ignore[attr-defined]
        "metadata": {"sources": {
            "reddit": {"ok": True, "count": 15, "dropped": 12,
                       "dropped_by": {"bait": 9, "stale": 3}},
            "kym": {"ok": True, "count": 8},
        }},
    }
    r = client.get("/api/trends/general/sources")
    assert r.status_code == 200, r.text
    by_id = {i["id"]: i for i in r.json()["items"]}
    assert by_id["reddit"]["dropped"] == 12
    assert by_id["reddit"]["dropped_by"] == {"bait": 9, "stale": 3}
    # A source that reported no drops still carries the keys, as null.
    assert by_id["kym"]["dropped"] is None
    assert by_id["kym"]["dropped_by"] is None


def test_sources_survives_a_run_with_no_metadata(client):
    """Old runs predate the counters; the health row must still render."""
    client.calls["run"] = {"metadata": None}    # type: ignore[attr-defined]
    r = client.get("/api/trends/general/sources")
    assert r.status_code == 200, r.text
    for item in r.json()["items"]:
        assert item["dropped"] is None and item["dropped_by"] is None


def test_sources_ignores_a_malformed_dropped_by(client):
    """Run metadata is JSON out of the database — a string where a dict is
    expected must not 500 the health panel."""
    client.calls["run"] = {                     # type: ignore[attr-defined]
        "metadata": {"sources": {"reddit": {"ok": True, "dropped_by": "nine"}}},
    }
    r = client.get("/api/trends/general/sources")
    assert r.status_code == 200, r.text
    by_id = {i["id"]: i for i in r.json()["items"]}
    assert by_id["reddit"]["dropped_by"] is None
