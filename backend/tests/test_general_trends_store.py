"""Tests for ``app/services/general_trends_store.py`` — SQL-capture only.

No database is contacted: ``get_conn`` is swapped for a fake connection whose
cursor records ``(sql, params)`` and answers ``fetchone``/``fetchall`` from a
canned queue. The tests pin the SQL *shapes* the pipeline and router rely on
(WHERE fragments, the ``::vector`` guard, whitelist + ``finished_at`` on runs,
``rowcount`` semantics on ``claim_run``) rather than Postgres behaviour.

There is no pytest-asyncio in this venv, so coroutines are driven with
``asyncio.run`` via ``_run``, matching tests/test_trend_finder.py.
"""
from __future__ import annotations

import asyncio
import re
import sys
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

import pytest
from psycopg.types.json import Jsonb

_BACKEND_ROOT = Path(__file__).resolve().parent.parent
if str(_BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(_BACKEND_ROOT))

from app.services import general_trends_store as gts  # noqa: E402


def _run(coro):
    return asyncio.run(coro)


# --------------------------------------------------------------------------- #
# Fake connection (same pattern as test_trend_finder.py, plus canned replies)
# --------------------------------------------------------------------------- #

class _FakeCursor:
    def __init__(self, sink: list, replies: list, rowcount: int):
        self._sink = sink
        self._replies = replies
        self.rowcount = rowcount

    async def execute(self, sql, params=None):
        self._sink.append((" ".join(str(sql).split()), params))

    async def fetchone(self):
        return self._replies.pop(0) if self._replies else None

    async def fetchall(self):
        return self._replies.pop(0) if self._replies else []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _FakeConn:
    def __init__(self, sink: list, replies: list, rowcount: int):
        self._sink = sink
        self._replies = replies
        self._rowcount = rowcount

    def cursor(self):
        return _FakeCursor(self._sink, self._replies, self._rowcount)


def _capture_sql(monkeypatch, *, replies: list | None = None, rowcount: int = 1) -> list:
    """Run a general_trends_store coroutine against a fake connection, capturing
    the SQL and the bound parameters. No database is contacted."""
    sink: list = []
    canned = list(replies or [])

    @asynccontextmanager
    async def _get_conn(*args, **kwargs):
        yield _FakeConn(sink, canned, rowcount)

    monkeypatch.setattr(gts, "get_conn", _get_conn)
    monkeypatch.setattr(gts, "_ensured", True)   # skip the DDL round-trip
    return sink


def _parse_iso(value: str) -> datetime:
    """The tz-aware datetime the store binds for an ISO string (Z ⇒ +00:00)."""
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _jsonb_value(obj):
    """Unwrap a psycopg Jsonb wrapper for assertions."""
    assert isinstance(obj, Jsonb), f"expected Jsonb, got {type(obj).__name__}"
    return obj.obj


_ITEM = {
    "source": "google_trends",
    "source_key": "US:nihilist penguin",
    "title": "nihilist penguin",
    "url": "https://news.example/penguin",
    "summary": "A penguin walks the wrong way · The meme explained",
    "image_url": "https://img.example/penguin.jpg",
    "metric_value": 2000.0,
    "metric_label": "searches",
    "published_at": "2026-08-18T19:30:00Z",
    "region": "US",
    "category": None,
    "extra": {"news_items": 2},
}

SEEN = datetime(2026, 8, 19, 12, 0, tzinfo=timezone.utc)


# =========================================================================== #
# 1. Constants + DDL
# =========================================================================== #

def test_constants_match_the_spec():
    assert gts.TREND_STATUSES == {"new", "saved", "dismissed", "used"}
    assert gts.RUN_STATUSES == {"queued", "fetching", "clustering", "scoring", "done", "failed"}
    assert gts.SENSITIVITIES == {"ok", "caution", "avoid"}
    assert gts.TREND_KINDS == {"meme", "format", "sound", "challenge", "news",
                               "event", "product", "culture", "other"}
    assert gts.RUN_STALE_AFTER_SEC == 30 * 60


def test_ddl_declares_the_three_tables_and_indexes():
    ddl = gts._DDL
    for table in ("general_trend_runs", "general_trends", "general_trend_sightings"):
        assert f"CREATE TABLE IF NOT EXISTS {table}" in ddl
    for idx in ("idx_general_trend_runs_recent", "idx_general_trends_rank",
                "idx_general_trends_seen", "idx_general_trends_unscored",
                "idx_general_trends_feed", "idx_general_trends_canonical",
                "idx_general_trend_sightings_trend", "idx_general_trend_sightings_key"):
        assert f"CREATE INDEX IF NOT EXISTS {idx}" in ddl
    assert "embedding         vector(768)" in ddl
    assert "UNIQUE (run_id, source, source_key)" in ddl
    assert "ON DELETE CASCADE" in ddl
    # The feed index is partial on the screen's verdict, so a flagged row kept
    # for audit costs nothing on the hot path.
    assert "ON general_trends (last_seen_at DESC) WHERE safety = 'ok'" in ddl
    assert "ON general_trends (canonical_key) WHERE canonical_key IS NOT NULL" in ddl


def test_ddl_migrates_the_new_columns_on_an_existing_database():
    ddl = gts._DDL
    for col, decl in (
        ("safety", "TEXT NOT NULL DEFAULT 'ok'"),
        ("safety_reason", "TEXT"),
        ("latest_content_at", "TIMESTAMPTZ"),
        ("evidence_count", "INT NOT NULL DEFAULT 0"),
        ("canonical_key", "TEXT"),
    ):
        stmt = f"ALTER TABLE general_trends ADD COLUMN IF NOT EXISTS {col}"
        assert stmt in ddl, f"no migration for {col}"
        line = next(ln for ln in ddl.splitlines() if ln.startswith(stmt))
        assert " ".join(line.rstrip(";").split()).endswith(decl), line


def test_every_alter_runs_before_every_index_on_the_new_columns():
    """commit 49369f3: on a database that predates a column, CREATE INDEX over
    that column fails with UndefinedColumn and aborts the WHOLE DDL statement,
    which kills every poller on ensure_tables(). The migration must therefore
    always precede any index that names a migrated column."""
    ddl = gts._DDL
    alters = [m.start() for m in re.finditer(r"ALTER TABLE general_trends", ddl)]
    assert alters, "the ALTER block disappeared"
    new_cols = ("safety", "canonical_key", "latest_content_at", "evidence_count")
    indexes = [
        m.start() for m in re.finditer(r"CREATE INDEX[^;]+?;", ddl, re.S)
        if any(c in m.group(0) for c in new_cols)
    ]
    assert len(indexes) >= 2, "expected the feed + canonical_key indexes"
    assert max(alters) < min(indexes)


def test_projections_do_not_ship_the_embedding():
    assert "embedding" not in gts.TREND_COLS
    for col in ("id", "slug", "title", "velocity", "sensitivity", "trend_kind",
                "opportunity_score", "status", "regions", "sources",
                "safety", "safety_reason", "latest_content_at",
                "evidence_count", "canonical_key"):
        assert col in gts.TREND_COLS.split(", ")
    # Both counters ship: sighting_count is the poll counter, evidence_count the
    # honest one (distinct source+key). Ranking reads evidence_count.
    assert "sighting_count" in gts.TREND_COLS.split(", ")
    assert gts.RUN_COLS.startswith("id, status, phase, progress, sources_total")
    assert gts.SIGHTING_COLS.split(", ")[:5] == ["id", "trend_id", "run_id", "source", "source_key"]


# =========================================================================== #
# 2. query_trends — WHERE fragments
# =========================================================================== #

def test_query_trends_default_where(monkeypatch):
    sink = _capture_sql(monkeypatch)
    _run(gts.query_trends())
    sql, params = sink[0]
    assert "FROM general_trends" in sql
    assert "status <> 'dismissed'" in sql
    assert "sensitivity IS DISTINCT FROM 'avoid'" in sql
    assert "safety = 'ok'" in sql
    assert "last_seen_at >= now() - (%s || ' days')::interval" in sql
    assert ("(latest_content_at IS NULL OR latest_content_at >= "
            "now() - (%s || ' days')::interval)") in sql
    assert "ORDER BY velocity DESC NULLS LAST, last_seen_at DESC LIMIT %s" in sql
    # 7 days of "a source still lists it", 30 of "something is still happening".
    assert params == ["7", "30", 1000]
    # nothing else sneaks in by default (look at the WHERE clause, not the projection)
    where = sql.split(" WHERE ", 1)[1]
    for frag in ("regions ?", "sources ?", "ILIKE", "theme =", "trend_kind =", "fit_score"):
        assert frag not in where
    # first_seen_at must NEVER gate a card: a trend first seen three weeks ago
    # that is still collecting fresh sightings is exactly what we keep.
    assert "first_seen_at" not in where


def test_query_trends_include_sensitive_drops_the_avoid_filter(monkeypatch):
    sink = _capture_sql(monkeypatch)
    _run(gts.query_trends(include_sensitive=True))
    sql, _ = sink[0]
    where = sql.split(" WHERE ", 1)[1]
    assert "sensitivity" not in where
    assert "safety = 'ok'" in where       # a separate, independent hide


def test_query_trends_include_filtered_drops_the_safety_filter(monkeypatch):
    sink = _capture_sql(monkeypatch)
    _run(gts.query_trends(include_filtered=True))
    sql, _ = sink[0]
    where = sql.split(" WHERE ", 1)[1]
    assert "safety" not in where
    assert "sensitivity IS DISTINCT FROM 'avoid'" in where


def test_query_trends_composes_safety_sensitivity_and_content_age(monkeypatch):
    """The three hides are independent: the model's verdict (sensitivity), the
    ingest screen's verdict (safety) and the content clock (content_days) must
    each appear exactly when asked for, in every combination."""
    for filtered in (False, True):
        for sensitive in (False, True):
            for content_days in (None, 0, 30):
                sink = _capture_sql(monkeypatch)
                _run(gts.query_trends(include_filtered=filtered,
                                      include_sensitive=sensitive,
                                      content_days=content_days))
                where = sink[0][0].split(" WHERE ", 1)[1]
                params = sink[0][1]
                assert ("safety = 'ok'" in where) is (not filtered)
                assert ("sensitivity IS DISTINCT FROM 'avoid'" in where) is (not sensitive)
                wants_content = content_days not in (None, 0)
                assert ("latest_content_at" in where) is wants_content
                # last_seen_at is always there at the 7-day default; the content
                # window is the only optional bind besides the cap.
                assert params == (["7", "30", 1000] if wants_content else ["7", 1000])


def test_query_trends_explicit_status_replaces_default(monkeypatch):
    sink = _capture_sql(monkeypatch)
    _run(gts.query_trends(status="dismissed"))
    sql, params = sink[0]
    assert "status = %s" in sql
    assert "status <> 'dismissed'" not in sql
    assert params[0] == "dismissed"


def test_query_trends_region_and_source_use_jsonb_containment(monkeypatch):
    sink = _capture_sql(monkeypatch)
    _run(gts.query_trends(region="AE", source="reddit"))
    sql, params = sink[0]
    assert "regions ? %s" in sql
    assert "sources ? %s" in sql
    assert params[:2] == ["AE", "reddit"]


def test_query_trends_q_is_ilike_on_title_summary_topic(monkeypatch):
    sink = _capture_sql(monkeypatch)
    _run(gts.query_trends(q="  penguin "))
    sql, params = sink[0]
    assert "(title ILIKE %s OR summary ILIKE %s OR topic ILIKE %s)" in sql
    assert params[:3] == ["%penguin%"] * 3


def test_query_trends_all_filters_bind_in_order(monkeypatch):
    sink = _capture_sql(monkeypatch)
    _run(gts.query_trends(status="saved", region="US", source="kym", theme="Memes",
                          kind="meme", min_fit=60, q="pen", include_sensitive=True,
                          include_filtered=True, since_days=7, content_days=14,
                          cap=50))
    sql, params = sink[0]
    assert "theme = %s" in sql and "trend_kind = %s" in sql
    assert "COALESCE(fit_score, 0) >= %s" in sql
    assert params == ["saved", "US", "kym", "Memes", "meme", 60,
                      "%pen%", "%pen%", "%pen%", "7", "14", 50]


def test_query_trends_both_windows_can_be_switched_off_independently(monkeypatch):
    sink = _capture_sql(monkeypatch)
    _run(gts.query_trends(since_days=None))
    sql, params = sink[0]
    assert "last_seen_at >=" not in sql
    assert "latest_content_at" in sql          # the content clock still applies
    assert params == ["30", 1000]

    sink = _capture_sql(monkeypatch)
    _run(gts.query_trends(since_days=None, content_days=None))
    sql, params = sink[0]
    assert "days" not in sql
    assert params == [1000]


# =========================================================================== #
# 3. create_trend — the ::vector guard
# =========================================================================== #

def test_create_trend_with_embedding_binds_vector(monkeypatch):
    sink = _capture_sql(monkeypatch, replies=[{"id": 1, "slug": "nihilist penguin"}])
    emb = [0.1, 0.2, 0.3]
    row = _run(gts.create_trend({
        "slug": "nihilist penguin", "title": "Nihilist Penguin",
        "summary": "s", "url": "u", "image_url": "i", "regions": ["US", "US", "AE"],
        "sources": ["google_trends"], "embedding": emb, "peak_metric": "2000",
        "first_seen_at": SEEN, "last_seen_at": SEEN.isoformat(),
    }))
    assert row == {"id": 1, "slug": "nihilist penguin"}
    sql, params = sink[0]
    assert "INSERT INTO general_trends" in sql
    assert "%s::vector" in sql
    assert f"RETURNING {gts.TREND_COLS}" in sql
    assert params[0] == "nihilist penguin" and params[2] == "Nihilist Penguin"
    assert params[1] is None                               # canonical_key absent
    assert _jsonb_value(params[7]) == ["US", "AE"]        # regions, distinct
    assert _jsonb_value(params[8]) == ["google_trends"]   # sources
    assert params[9] == 1                                  # source_count derived
    assert params[10] == 0                                 # sighting_count default
    assert params[11] == 2000.0                            # peak_metric coerced
    assert params[12] is None                              # safety -> COALESCE 'ok'
    assert params[13] is None                              # safety_reason
    assert params[14] == SEEN and params[15] == SEEN       # timestamps parsed
    assert params[16] is None                              # latest_content_at
    assert params[17] == emb                               # the vector itself
    assert _jsonb_value(params[18]) == {}                  # metadata
    assert len(params) == 19


def test_create_trend_writes_the_screen_verdicts_when_present(monkeypatch):
    sink = _capture_sql(monkeypatch, replies=[{"id": 4}])
    _run(gts.create_trend({
        "slug": "rate my setup", "title": "Rate my setup",
        "canonical_key": "rate my setup", "safety": "flagged",
        "safety_reason": "bait", "latest_content_at": "2026-08-18T19:30:00Z",
    }))
    sql, params = sink[0]
    # NOT NULL column: an absent verdict is 'ok', never NULL, so the feed's
    # `WHERE safety = 'ok'` can stay a plain equality.
    assert "COALESCE(%s, 'ok')" in sql
    assert params[1] == "rate my setup"
    assert params[12] == "flagged" and params[13] == "bait"
    assert params[16] == datetime(2026, 8, 18, 19, 30, tzinfo=timezone.utc)


def test_create_trend_without_embedding_writes_null_and_never_casts(monkeypatch):
    sink = _capture_sql(monkeypatch, replies=[{"id": 2}])
    _run(gts.create_trend({"slug": "x", "title": "X", "sources": ["a", "b"],
                           "source_count": 5}))
    sql, params = sink[0]
    assert "::vector" not in sql
    assert "NULL, %s) RETURNING" in sql
    assert params[9] == 5                       # explicit source_count wins
    assert len(params) == 18                    # no vector param bound
    assert not any(isinstance(p, list) for p in params)


def test_create_trend_empty_embedding_is_treated_as_none(monkeypatch):
    sink = _capture_sql(monkeypatch, replies=[{"id": 3}])
    _run(gts.create_trend({"slug": "y", "title": "Y", "embedding": []}))
    sql, params = sink[0]
    assert "::vector" not in sql
    assert len(params) == 18


# =========================================================================== #
# 4. touch_trend
# =========================================================================== #

def test_touch_trend_is_one_update_with_the_documented_effects(monkeypatch):
    sink = _capture_sql(monkeypatch)
    _run(gts.touch_trend(7, item=_ITEM, seen_at=SEEN))
    assert len(sink) == 1
    sql, params = sink[0]
    assert sql.startswith("UPDATE general_trends SET sighting_count = sighting_count + 1")
    assert "sources ? %s::text THEN sources ELSE sources || %s END" in sql
    assert "regions ? %s::text THEN regions ELSE regions || %s END" in sql
    assert "source_count = jsonb_array_length(" in sql
    assert "image_url = COALESCE(NULLIF(image_url, ''), %s::text)" in sql
    assert "url = COALESCE(NULLIF(url, ''), %s::text)" in sql
    assert "summary = COALESCE(NULLIF(summary, ''), %s::text)" in sql
    assert "last_seen_at = %s" in sql
    assert "peak_metric = GREATEST(peak_metric, %s::real)" in sql
    assert sql.endswith("WHERE id = %s")

    assert params[0] == "google_trends" and _jsonb_value(params[1]) == ["google_trends"]
    assert params[2] == "google_trends" and _jsonb_value(params[3]) == ["google_trends"]
    assert params[4] == "US" and _jsonb_value(params[5]) == ["US"]
    assert params[6] == _ITEM["image_url"]
    assert params[7] == _ITEM["url"]
    assert params[8] == _ITEM["summary"]
    assert params[9] is None                # category
    assert params[10] is None               # safety: no verdict on the item
    assert params[11] is None and params[12] is None    # safety_reason branch
    assert params[13] == SEEN               # last_seen_at
    assert params[16] == 2000.0
    assert params[17] == 7


def test_touch_trend_counts_distinct_evidence_not_polls(monkeypatch):
    """sighting_count is bumped on EVERY re-poll — on the live corpus "UAE
    Ministry of Defence" reads 11 from 4 real items on ONE source. It stays as
    the poll counter; evidence_count is recomputed from the distinct
    (source, source_key) pairs actually on file, and that is what ranking may
    read. Correlated on general_trends.id, so no extra bound parameter."""
    sink = _capture_sql(monkeypatch)
    _run(gts.touch_trend(7, item=_ITEM, seen_at=SEEN))
    sql, params = sink[0]
    assert "sighting_count = sighting_count + 1" in sql
    assert ("evidence_count = ( SELECT COUNT(DISTINCT (s.source, s.source_key)) "
            "FROM general_trend_sightings s WHERE s.trend_id = general_trends.id)") in sql
    assert "evidence_count = evidence_count + 1" not in sql
    assert params.count(7) == 1              # the trend id binds once, at the end


def test_touch_trend_escalates_safety_but_never_clears_it(monkeypatch):
    """The screen is the authority on "not safe"; it is not an authority on
    "safe again". One flagged sighting flags the card for good."""
    sink = _capture_sql(monkeypatch)
    _run(gts.touch_trend(7, item=dict(_ITEM, safety="flagged", safety_reason="bait"),
                         seen_at=SEEN))
    sql, params = sink[0]
    assert "safety = CASE WHEN %s::text = 'flagged' THEN 'flagged' ELSE safety END" in sql
    assert ("safety_reason = CASE WHEN %s::text = 'flagged' "
            "THEN COALESCE(%s::text, safety_reason) ELSE safety_reason END") in sql
    assert params[10] == "flagged"
    assert params[11] == "flagged" and params[12] == "bait"

    # A later clean sighting binds a non-'flagged' value, so the CASE keeps the
    # stored verdict — there is no code path that writes 'ok' over 'flagged'.
    sink = _capture_sql(monkeypatch)
    _run(gts.touch_trend(7, item=dict(_ITEM, safety="ok"), seen_at=SEEN))
    _, params = sink[0]
    assert params[10] == "ok" and params[12] is None
    assert "'ok'" not in sink[0][0].split("safety =", 1)[1].split(",")[0]


def test_touch_trend_moves_latest_content_at_forward_only(monkeypatch):
    sink = _capture_sql(monkeypatch)
    _run(gts.touch_trend(7, item=dict(_ITEM, content_at="2026-08-18T09:00:00Z"),
                         seen_at=SEEN))
    sql, params = sink[0]
    assert ("latest_content_at = GREATEST( COALESCE(latest_content_at, %s::timestamptz), "
            "%s::timestamptz)") in sql
    # The same value binds twice: GREATEST(existing, new) can only move forward,
    # so an old item resurfacing never rewinds "when did something last happen".
    want = datetime(2026, 8, 18, 9, 0, tzinfo=timezone.utc)
    assert params[14] == want and params[15] == want
    # Explicit content_at wins over published_at (the ingest screen knows which
    # sources have a real content age and which are just a rolling window).
    assert want != _parse_iso(_ITEM["published_at"])


def test_touch_trend_falls_back_to_published_at_for_content_time(monkeypatch):
    # google_trends is an AGE_FROM_ITEM source, so published_at IS its content
    # time whether or not the freshness module is importable.
    sink = _capture_sql(monkeypatch)
    _run(gts.touch_trend(7, item=_ITEM, seen_at=SEEN))
    _, params = sink[0]
    assert params[14] == _parse_iso(_ITEM["published_at"])

    # Nothing to date it by ⇒ NULL, and GREATEST(existing, NULL) keeps existing.
    sink = _capture_sql(monkeypatch)
    _run(gts.touch_trend(7, item={"source": "x_trends", "title": "t"}, seen_at=SEEN))
    _, params = sink[0]
    assert params[14] is None and params[15] is None


def test_touch_trend_with_blank_source_region_appends_nothing(monkeypatch):
    sink = _capture_sql(monkeypatch)
    item = dict(_ITEM, source="", region=None, metric_value=None, image_url="")
    _run(gts.touch_trend(7, item=item, seen_at=SEEN))
    _, params = sink[0]
    assert params[0] == "" and _jsonb_value(params[1]) == []     # `sources || '[]'` no-op
    assert params[4] == "" and _jsonb_value(params[5]) == []
    assert params[6] is None                                       # "" image ⇒ NULL
    assert params[16] is None                                      # GREATEST(x, NULL) keeps x


# =========================================================================== #
# 5. insert_sighting
# =========================================================================== #

def test_insert_sighting_on_conflict_do_nothing_and_parses_published_at(monkeypatch):
    sink = _capture_sql(monkeypatch)
    _run(gts.insert_sighting(7, 3, _ITEM))
    sql, params = sink[0]
    assert "INSERT INTO general_trend_sightings" in sql
    assert "ON CONFLICT (run_id, source, source_key) DO NOTHING" in sql
    assert params[0] == 7 and params[1] == 3
    assert params[2] == "google_trends" and params[3] == "US:nihilist penguin"
    assert params[12] == datetime(2026, 8, 18, 19, 30, tzinfo=timezone.utc)
    assert params[13] is None                          # seen_at ⇒ COALESCE(NULL, now())
    assert _jsonb_value(params[14]) == {"news_items": 2}


# =========================================================================== #
# 6. Runs
# =========================================================================== #

def test_create_run_marks_queued_and_returns_active(monkeypatch):
    sink = _capture_sql(monkeypatch, replies=[{"id": 9, "status": "queued", "metadata": {}}])
    run = _run(gts.create_run({"trigger": "manual"}))
    sql, params = sink[0]
    assert "INSERT INTO general_trend_runs (status, phase, metadata) VALUES ('queued', 'starting', %s)" in sql
    assert _jsonb_value(params[0]) == {"trigger": "manual"}
    assert run["active"] is True


def test_update_run_whitelists_fields_and_stamps_finished_at(monkeypatch):
    sink = _capture_sql(monkeypatch)
    _run(gts.update_run(9, status="done", progress="12 trends", trends_new=3,
                        metadata={"sources": {}}, bogus="nope", id=1))
    sql, params = sink[0]
    assert sql.startswith("UPDATE general_trend_runs SET ")
    assert "status = %s" in sql and "progress = %s" in sql and "trends_new = %s" in sql
    assert "metadata = %s" in sql
    assert "bogus" not in sql and "id = %s WHERE" not in sql
    assert "finished_at = now()" in sql
    assert sql.endswith("WHERE id = %s")
    assert params[0] == "done" and params[-1] == 9
    assert _jsonb_value(params[3]) == {"sources": {}}


def test_update_run_non_terminal_status_has_no_finished_at(monkeypatch):
    sink = _capture_sql(monkeypatch)
    _run(gts.update_run(9, status="clustering", phase="clustering"))
    sql, _ = sink[0]
    assert "finished_at" not in sql
    _run(gts.update_run(9, status="failed", error="boom"))
    assert "finished_at = now()" in sink[1][0]


def test_update_run_with_only_unknown_fields_is_a_no_op(monkeypatch):
    sink = _capture_sql(monkeypatch)
    _run(gts.update_run(9, nothing="here"))
    assert sink == []


def test_heartbeat_run_only_touches_the_heartbeat_of_a_live_run(monkeypatch):
    sink = _capture_sql(monkeypatch)
    _run(gts.heartbeat_run(9))
    sql, params = sink[0]
    assert sql.startswith("UPDATE general_trend_runs SET metadata = metadata || jsonb_build_object('heartbeat_at', now())")
    assert "status NOT IN ('done', 'failed')" in sql
    assert params == (9,)


def test_update_run_stamps_a_heartbeat_on_every_real_update(monkeypatch):
    # A progress-only write keeps the metadata document and adds heartbeat_at…
    sink = _capture_sql(monkeypatch)
    _run(gts.update_run(9, progress="scoring 3/25…", trends_scored=3))
    sql, params = sink[0]
    assert "metadata = metadata || jsonb_build_object('heartbeat_at', now())" in sql
    assert params == ["scoring 3/25…", 3, 9]

    # …and a metadata write replaces the document but keeps the heartbeat on it.
    sink = _capture_sql(monkeypatch)
    _run(gts.update_run(9, metadata={"sources": {"kym": {"ok": True}}}))
    sql, params = sink[0]
    assert "metadata = %s::jsonb || jsonb_build_object('heartbeat_at', now())" in sql
    assert sql.count("heartbeat_at") == 1
    assert _jsonb_value(params[0]) == {"sources": {"kym": {"ok": True}}}


def test_claim_run_uses_rowcount(monkeypatch):
    sink = _capture_sql(monkeypatch, rowcount=1)
    assert _run(gts.claim_run(9)) is True
    sql, params = sink[0]
    assert "SET status = 'fetching', phase = 'fetching' WHERE id = %s AND status = 'queued'" in sql
    assert params == (9,)

    _capture_sql(monkeypatch, rowcount=0)
    assert _run(gts.claim_run(9)) is False


def test_fail_stale_runs_uses_the_stale_window_from_the_last_heartbeat(monkeypatch):
    sink = _capture_sql(monkeypatch, rowcount=2)
    assert _run(gts.fail_stale_runs()) == 2
    sql, params = sink[0]
    assert "status NOT IN ('done', 'failed')" in sql
    # Staleness is measured from the LATER of queue time and the last heartbeat,
    # so a long-but-alive first refresh is never reported failed mid-run.
    assert ("GREATEST(started_at, COALESCE((metadata->>'heartbeat_at')::timestamptz, started_at)) "
            "< now() - (%s || ' seconds')::interval") in sql
    assert params == (str(30 * 60),)


def test_latest_run_age_is_measured_from_the_last_heartbeat(monkeypatch):
    sink = _capture_sql(monkeypatch, replies=[{"id": 1, "status": "scoring", "error": None,
                                               "age_sec": 5.0}])
    _run(gts.latest_run())
    sql, _ = sink[0]
    assert ("EXTRACT(EPOCH FROM (now() - GREATEST(started_at, "
            "COALESCE((metadata->>'heartbeat_at')::timestamptz, started_at)))) AS age_sec") in sql


def test_latest_run_flags_active_and_stale(monkeypatch):
    _capture_sql(monkeypatch, replies=[{"id": 1, "status": "fetching", "error": None,
                                        "age_sec": 10.0}])
    run = _run(gts.latest_run())
    assert run["active"] is True and "age_sec" not in run

    _capture_sql(monkeypatch, replies=[{"id": 1, "status": "fetching", "error": None,
                                        "age_sec": float(gts.RUN_STALE_AFTER_SEC + 1)}])
    run = _run(gts.latest_run())
    assert run["status"] == "failed" and run["active"] is False and run["error"]

    _capture_sql(monkeypatch, replies=[None])
    assert _run(gts.latest_run()) is None
    assert _run(gts.run_is_active()) is False


def test_pending_runs_reads_queued_oldest_first(monkeypatch):
    sink = _capture_sql(monkeypatch, replies=[[{"id": 4}, {"id": 5}]])
    assert _run(gts.pending_runs(limit=2)) == [{"id": 4}, {"id": 5}]
    sql, params = sink[0]
    assert "WHERE status = 'queued' ORDER BY started_at LIMIT %s" in sql
    assert params == (2,)


# =========================================================================== #
# 7. Lookups
# =========================================================================== #

def test_find_trend_by_source_key_goes_through_sightings(monkeypatch):
    sink = _capture_sql(monkeypatch, replies=[{"id": 1}])
    assert _run(gts.find_trend_by_source_key("reddit", "abc", since_days=7)) == {"id": 1}
    sql, params = sink[0]
    assert "FROM general_trend_sightings s JOIN general_trends t ON t.id = s.trend_id" in sql
    assert "s.source = %s AND s.source_key = %s" in sql
    assert "s.seen_at >= now() - (%s || ' days')::interval" in sql
    assert "ORDER BY s.seen_at DESC LIMIT 1" in sql
    assert params == ("reddit", "abc", "7")
    assert "t.embedding" not in sql


def test_find_trend_by_source_key_short_circuits_on_blank(monkeypatch):
    sink = _capture_sql(monkeypatch)
    assert _run(gts.find_trend_by_source_key("", "abc")) is None
    assert _run(gts.find_trend_by_slug("")) is None
    assert _run(gts.find_trend_by_canonical_key("  ")) is None
    assert sink == []


def test_find_trend_by_slug_is_windowed(monkeypatch):
    """Unwindowed, a generic slug ("supreme court", "deaths in 2026") is a
    permanent magnet: every later story that normalises the same way is folded
    into a card that stopped being about anything months ago."""
    sink = _capture_sql(monkeypatch, replies=[{"id": 1}])
    assert _run(gts.find_trend_by_slug("supreme court")) == {"id": 1}
    sql, params = sink[0]
    assert "WHERE slug = %s" in sql
    assert "last_seen_at >= now() - (%s || ' days')::interval" in sql
    assert params == ("supreme court", "30")

    sink = _capture_sql(monkeypatch, replies=[None])
    _run(gts.find_trend_by_slug("s", since_days=7))
    assert sink[0][1] == ("s", "7")


def test_canonical_key_round_trip(monkeypatch):
    sink = _capture_sql(monkeypatch)
    _run(gts.set_canonical_key(12, "  al-nassr vs al-diriyah  "))
    sql, params = sink[0]
    assert sql == ("UPDATE general_trends SET canonical_key = %s, updated_at = now() "
                   "WHERE id = %s")
    assert params == ("al-nassr vs al-diriyah", 12)

    sink = _capture_sql(monkeypatch, replies=[{"id": 12, "slug": "الدرعية ضد النصر"}])
    hit = _run(gts.find_trend_by_canonical_key("al-nassr vs al-diriyah"))
    assert hit == {"id": 12, "slug": "الدرعية ضد النصر"}
    sql, params = sink[0]
    assert f"SELECT {gts.TREND_COLS} FROM general_trends" in sql
    assert "WHERE canonical_key = %s" in sql
    assert "last_seen_at >= now() - (%s || ' days')::interval" in sql
    assert "ORDER BY last_seen_at DESC LIMIT 1" in sql
    assert params == ("al-nassr vs al-diriyah", "30")


def test_set_canonical_key_never_overwrites_with_nothing(monkeypatch):
    sink = _capture_sql(monkeypatch)
    _run(gts.set_canonical_key(12, ""))
    _run(gts.set_canonical_key(12, "   "))
    _run(gts.set_canonical_key(0, "something"))
    assert sink == []


def test_nearest_trends_orders_by_cosine_distance(monkeypatch):
    sink = _capture_sql(monkeypatch, replies=[[{"id": 1, "slug": "a", "title": "A",
                                                "distance": "0.12"}]])
    emb = [0.5] * 3
    out = _run(gts.nearest_trends(emb, limit=3, since_days=10))
    assert out == [{"id": 1, "slug": "a", "title": "A", "distance": 0.12}]
    sql, params = sink[0]
    assert "embedding <=> %s::vector AS distance" in sql
    assert "embedding IS NOT NULL" in sql
    assert "last_seen_at >= now() - (%s || ' days')::interval" in sql
    # The KNN order + LIMIT stay INSIDE, so the cap never turns the lookup into
    # a full distance scan of the window.
    assert "ORDER BY distance LIMIT %s ) d WHERE d.distance <= %s ORDER BY d.distance" in sql
    assert params == [emb, "10", 3, 0.20]


def test_nearest_trends_max_distance_can_be_lifted(monkeypatch):
    """These rows are candidate HINTS for the labelling prompt, not a decision:
    over all 65,703 pairs of the 363 stored vectors count(d <= 0.12) = 0 and the
    closest pair in the corpus (0.1230) is FALSE. Nothing may attach on this."""
    sink = _capture_sql(monkeypatch, replies=[[]])
    _run(gts.nearest_trends([0.5] * 3, max_distance=None))
    sql, params = sink[0]
    assert "d.distance <=" not in sql
    assert params == [[0.5] * 3, "14", 5]


def test_nearest_trends_with_no_embedding_does_not_query(monkeypatch):
    sink = _capture_sql(monkeypatch)
    assert _run(gts.nearest_trends([], limit=5)) == []
    assert sink == []


# =========================================================================== #
# 8. Sightings readers, velocity, scoring, status
# =========================================================================== #

def test_sightings_for_returns_every_requested_id(monkeypatch):
    sink = _capture_sql(monkeypatch, replies=[[
        {"trend_id": 2, "source": "kym", "seen_at": SEEN},
        {"trend_id": 2, "source": "reddit", "seen_at": SEEN},
    ]])
    out = _run(gts.sightings_for([1, 2], per_trend=6))
    assert out == {1: [], 2: [{"trend_id": 2, "source": "kym", "seen_at": SEEN},
                             {"trend_id": 2, "source": "reddit", "seen_at": SEEN}]}
    sql, params = sink[0]
    assert "DISTINCT ON (trend_id, source, source_key)" in sql
    assert "trend_id = ANY(%s)" in sql and "WHERE rn <= %s" in sql
    assert params == ([1, 2], 6)


def test_sightings_for_empty_ids_skips_the_query(monkeypatch):
    sink = _capture_sql(monkeypatch)
    assert _run(gts.sightings_for([])) == {}
    assert sink == []


def test_recent_sightings_for_velocity_groups_by_trend(monkeypatch):
    sink = _capture_sql(monkeypatch, replies=[[
        {"trend_id": 1, "seen_at": SEEN, "source": "kym", "source_key": "a",
         "region": "global", "metric_value": None, "metric_label": None,
         "published_at": None, "extra": {"rank": 2},
         "prev_metric": None, "prev_seen_at": None},
        {"trend_id": 1, "seen_at": SEEN, "source": "reddit", "source_key": "t3_x",
         "region": "global", "metric_value": None, "metric_label": None,
         "published_at": None, "extra": None,
         "prev_metric": None, "prev_seen_at": None},
        {"trend_id": 3, "seen_at": SEEN, "source": "bluesky", "source_key": "b",
         "region": "global", "metric_value": 900.0, "metric_label": "posts",
         "published_at": SEEN, "extra": {},
         "prev_metric": 300.0, "prev_seen_at": SEEN},
    ]])
    out = _run(gts.recent_sightings_for_velocity(since_hours=48))
    assert set(out) == {1, 3}
    assert [s["source"] for s in out[1]] == ["kym", "reddit"]
    # The whole point of the query: the ranking module's inputs actually arrive.
    assert out[1][0]["extra"] == {"rank": 2}
    assert out[1][1]["extra"] == {}          # a NULL extra becomes an empty dict
    assert out[3][0]["metric_value"] == 900.0
    assert out[3][0]["published_at"] == SEEN
    # LAG is folded into extra so compute_velocity can measure growth.
    assert out[3][0]["extra"]["prev_metric"] == 300.0
    assert out[3][0]["extra"]["prev_seen_at"] == SEEN
    sql, params = sink[0]
    assert "seen_at >= now() - (%s || ' hours')::interval" in sql
    assert "LAG(metric_value) OVER w" in sql
    assert "PARTITION BY source, source_key ORDER BY seen_at" in sql
    # The LAG window reaches further back than the returned window, so the
    # oldest returned row still has a predecessor to compare against.
    assert params == ("144", "48")


def test_recent_sightings_for_velocity_omits_a_missing_predecessor(monkeypatch):
    """A NULL prev_metric must be ABSENT from extra, never present as None —
    the growth term reads a present-but-None value as a measured zero."""
    _capture_sql(monkeypatch, replies=[[
        {"trend_id": 7, "seen_at": SEEN, "source": "hacker_news", "source_key": "1",
         "region": "global", "metric_value": 120.0, "metric_label": "points",
         "published_at": SEEN, "extra": {}, "prev_metric": None, "prev_seen_at": None},
    ]])
    out = _run(gts.recent_sightings_for_velocity())
    assert "prev_metric" not in out[7][0]["extra"]
    assert "prev_seen_at" not in out[7][0]["extra"]


def test_save_velocity_and_set_local_thumb(monkeypatch):
    sink = _capture_sql(monkeypatch)
    _run(gts.save_velocity(4, 0.7312))
    _run(gts.set_local_thumb(4, "general_trend_thumbs/abc.jpg"))
    assert sink[0][1] == (0.7312, 4) and "SET velocity = %s" in sink[0][0]
    assert sink[1][1] == ("general_trend_thumbs/abc.jpg", 4)
    assert "SET local_thumb_path = %s" in sink[1][0]


def test_unscored_trends_only_recent_hottest_first(monkeypatch):
    sink = _capture_sql(monkeypatch, replies=[[]])
    _run(gts.unscored_trends(limit=8))
    sql, params = sink[0]
    assert "scored_at IS NULL" in sql
    assert "last_seen_at >= now() - interval '14 days'" in sql
    assert "ORDER BY velocity DESC NULLS LAST, last_seen_at DESC LIMIT %s" in sql
    assert params == (8,)


def test_unscored_trends_never_sends_a_flagged_trend_to_the_model(monkeypatch):
    """A flagged trend is stored for audit and can never be shown, so paying to
    score it is pure waste (~a third of the 108 `avoid` rows of the live run)."""
    sink = _capture_sql(monkeypatch, replies=[[]])
    _run(gts.unscored_trends())
    sql, params = sink[0]
    assert "safety = 'ok'" in sql
    # 200 left 163 rows of a 363-row cold start unscored forever.
    assert params == (400,)


def test_save_score_sets_clean_title_when_present(monkeypatch):
    sink = _capture_sql(monkeypatch)
    score = {"clean_title": "Nihilist Penguin", "topic": "t", "topic_summary": "ts",
             "theme": "Memes", "trend_kind": "meme", "sensitivity": "ok",
             "fit_score": 80, "fit_reason": "r", "uae_relevance": 40,
             "suggested_angle": "a", "suggested_format": "reel", "effort": "low",
             "tags": ["x"], "opportunity_score": 66.6}
    _run(gts.save_score(5, score))
    sql, params = sink[0]
    assert "SET title = COALESCE(%s, title)" in sql
    assert "trend_kind = %s, sensitivity = %s" in sql
    assert "scored_at = now()" in sql
    assert params[0] == "Nihilist Penguin"
    assert params[1:4] == ("t", "ts", "Memes")
    assert params[4:6] == ("meme", "ok")
    assert _jsonb_value(params[12]) == ["x"]
    # ... tags, opportunity_score, the metadata merge, then the id
    assert params[13] == 66.6
    assert _jsonb_value(params[14]) == {}      # nothing extra in this score
    assert params[15] == 5

    _run(gts.save_score(5, {"topic": "only"}))
    _, params = sink[1]
    assert params[0] is None                          # blank clean_title keeps title
    assert _jsonb_value(params[12]) == []


def test_clear_scores_all_or_some(monkeypatch):
    sink = _capture_sql(monkeypatch, rowcount=7)
    assert _run(gts.clear_scores()) == 7
    assert sink[0] == ("UPDATE general_trends SET scored_at = NULL", None)
    assert _run(gts.clear_scores([1, 2])) == 7
    assert sink[1][0] == "UPDATE general_trends SET scored_at = NULL WHERE id = ANY(%s)"
    assert sink[1][1] == ([1, 2],)


def test_update_status_rejects_unknown_status_without_touching_db(monkeypatch):
    sink = _capture_sql(monkeypatch, replies=[{"id": 1, "status": "saved"}])
    assert _run(gts.update_status(1, "bogus")) is None
    assert sink == []
    assert _run(gts.update_status(1, "saved")) == {"id": 1, "status": "saved"}
    sql, params = sink[0]
    assert "SET status = %s, updated_at = now() WHERE id = %s RETURNING" in sql
    assert params == ("saved", 1)


# =========================================================================== #
# 9. stats / theme_rollup
# =========================================================================== #

def test_stats_derives_source_health_from_latest_run_metadata(monkeypatch):
    _capture_sql(monkeypatch, replies=[{
        "trends_total": 12, "trends_unscored": 3, "trends_saved": 1,
        "trends_new_24h": 4, "last_run_at": SEEN,
        "latest_metadata": {"sources": {
            "google_trends": {"ok": True, "count": 20},
            "reddit": {"ok": False, "error": "403"},
            "kym": {"ok": True},
        }},
    }])
    out = _run(gts.stats())
    assert out == {
        "trends_total": 12, "trends_unscored": 3, "trends_saved": 1,
        "trends_new_24h": 4, "last_run_at": SEEN,
        "sources_ok": 2, "sources_total": 3,
    }


def test_stats_with_no_runs_yet(monkeypatch):
    _capture_sql(monkeypatch, replies=[{
        "trends_total": 0, "trends_unscored": 0, "trends_saved": 0,
        "trends_new_24h": 0, "last_run_at": None, "latest_metadata": None,
    }])
    out = _run(gts.stats())
    assert out["sources_ok"] == 0 and out["sources_total"] == 0
    assert out["last_run_at"] is None


def test_theme_rollup_shape(monkeypatch):
    sink = _capture_sql(monkeypatch, replies=[[]])
    _run(gts.theme_rollup())
    sql, params = sink[0]
    assert "COUNT(*) AS trend_count" in sql
    # ::int — ROUND(AVG(int)) is numeric, which FastAPI would ship as a JSON
    # string; the frontend types avg_fit / avg_uae as numbers.
    assert "ROUND(AVG(fit_score))::int AS avg_fit" in sql
    assert "ROUND(AVG(uae_relevance))::int AS avg_uae" in sql
    assert "MAX(opportunity_score) AS best_score" in sql
    assert "status <> 'dismissed'" in sql and "GROUP BY theme" in sql
    # Flagged rows can never appear in the feed, so they must not inflate a
    # theme's count or drag its average fit.
    assert "safety = 'ok'" in sql
    assert params == ("14",)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(pytest.main([__file__, "-q"]))


# =========================================================================== #
# The participation gate — a trend is something people are RECREATING
# =========================================================================== #
# Measured on the first live corpus: only 103 of 363 rows were a participatory
# kind (meme 77, format 21, sound 4, challenge 1). The other 260 were news 126,
# culture 70, event 40, other 19, product 5 — a feed nobody could act on.

_PARTICIPATORY = ["challenge", "format", "meme", "sound"]


def test_query_trends_gates_on_participatory_kinds(monkeypatch):
    sink = _capture_sql(monkeypatch, replies=[[]])
    _run(gts.query_trends(kinds=["meme", "format", "sound", "challenge"]))
    sql, params = sink[0]
    assert "trend_kind = ANY(%s)" in sql
    # sorted + de-duplicated, so the bound array is stable across callers
    assert _PARTICIPATORY in params


def test_the_gate_hides_unclassified_rows_unless_asked(monkeypatch):
    """A row the model has not judged yet is PENDING, not news. Hiding it keeps
    the feed honest in the window between fetch and score."""
    sink = _capture_sql(monkeypatch, replies=[[]])
    _run(gts.query_trends(kinds=_PARTICIPATORY))
    assert "trend_kind IS NULL" not in sink[0][0]

    sink2 = _capture_sql(monkeypatch, replies=[[]])
    _run(gts.query_trends(kinds=_PARTICIPATORY, include_unclassified=True))
    assert "(trend_kind = ANY(%s) OR trend_kind IS NULL)" in sink2[0][0]


def test_an_explicit_kind_filter_wins_over_the_gate(monkeypatch):
    """Asking for one kind means that kind, even a non-participatory one."""
    sink = _capture_sql(monkeypatch, replies=[[]])
    _run(gts.query_trends(kind="news", kinds=_PARTICIPATORY))
    sql, params = sink[0]
    assert "trend_kind = %s" in sql
    assert "ANY(%s)" not in sql
    assert "news" in params


def test_no_kinds_means_no_gate(monkeypatch):
    """`all_kinds` in the API passes None — every kind comes back."""
    for kinds in (None, [], ["", "  "]):
        sink = _capture_sql(monkeypatch, replies=[[]])
        _run(gts.query_trends(kinds=kinds))
        # `trend_kind` is in TREND_COLS, so assert on the PREDICATE, not the SQL
        sql = sink[0][0]
        assert "trend_kind = ANY(%s)" not in sql, kinds
        assert "trend_kind = %s" not in sql, kinds


def test_save_score_keeps_replication_in_metadata(monkeypatch):
    """`replication` is the model's answer to "who is making their own version",
    which is what `trend_kind` is derived from — so a kind you disagree with can
    be audited instead of argued about. It has no column; it is merged into
    metadata rather than dropped, along with the other extra scorer fields."""
    sink = _capture_sql(monkeypatch)
    _run(gts.save_score(9, {
        "trend_kind": "format", "fit_score": 70,
        "replication": "students restaging it in the cluster; ours = the 3am build",
        "confidence": 80, "evidence_quality": "corroborated",
    }))
    sql, params = sink[0]
    assert "metadata = COALESCE(metadata, '{}'::jsonb) || %s" in sql
    merged = _jsonb_value(params[14])
    assert merged["replication"].startswith("students restaging it")
    assert merged["confidence"] == 80
    assert merged["evidence_quality"] == "corroborated"
    # empty / None values are not written, so a sparse score cannot blank a key
    assert "skip_reason" not in merged


# =========================================================================== #
# set_metadata — a jsonb merge that touches nothing else
# =========================================================================== #

def test_set_metadata_merges_and_sets_only_that_column(monkeypatch):
    """`save_score` also merges metadata, but its SET list writes topic,
    topic_summary, fit_score, tags and scored_at from the same dict — so reusing
    it to store a web lookup would blank a trend's whole AI verdict. Hence a
    setter whose SET list is one column."""
    sink = _capture_sql(monkeypatch)
    _run(gts.set_metadata(7, {"research": {"what_it_is": "a Spider-Man film"}}))
    sql, params = sink[0]
    assert "metadata = COALESCE(metadata, '{}'::jsonb) || %s" in sql
    # merge, not replace: first_source / replication / heat signals must survive
    assert "SET metadata" in sql
    for col in ("topic", "topic_summary", "fit_score", "tags", "scored_at",
                "title", "trend_kind"):
        assert f"{col} =" not in sql, f"set_metadata must not write {col}"
    assert _jsonb_value(params[0]) == {"research": {"what_it_is": "a Spider-Man film"}}
    assert params[1] == 7


def test_set_metadata_ignores_an_empty_patch(monkeypatch):
    """No patch, no round-trip — and never a write that clears the column."""
    for patch in ({}, None, "nope", []):
        sink = _capture_sql(monkeypatch)
        _run(gts.set_metadata(7, patch))
        assert sink == [], patch
