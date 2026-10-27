"""Schema guards for the multi-source Trend Finder.

`trend_posts` started life holding one thing: Instagram posts belonging to a
watched competitor. It now holds rows from sources that have **no competitor at
all** — AI News items, and previously Meltwater trends. Four small properties
make that possible, and every one of them fails SILENTLY when broken:

  * `competitor_id` must be nullable, or the insert raises.
  * The feed query and the theme rollup must LEFT JOIN `trend_competitors` and
    test the excluded-status NULL-safely. An inner join, or a bare
    `c.status <> 'excluded'`, removes every source-less row — **the whole
    category renders empty with no error anywhere.** That is the single most
    expensive bug this file exists to prevent.
  * A database created before those columns existed needs explicit `ALTER`s,
    because the table is created with `CREATE TABLE IF NOT EXISTS` and so is
    never re-shaped on upgrade.

These assert on the SQL *text* rather than on a live database, deliberately:
there is no test database in this project, and asserting on the source means the
guard survives any rename of the sources themselves. It has already survived one
(Meltwater → news).

Runs standalone (``cd backend && python tests/test_trend_store_schema.py``) and
under pytest.
"""
from __future__ import annotations

import inspect
import sys
from pathlib import Path

import pytest

_BACKEND_ROOT = Path(__file__).resolve().parent.parent
if str(_BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(_BACKEND_ROOT))

from app.services import trend_store as tstore  # noqa: E402


# ---------------------------------------------------------------------------
# The NULL-safe join — the trap
# ---------------------------------------------------------------------------

def test_the_feed_query_left_joins_and_is_null_safe():
    sql = inspect.getsource(tstore.query_posts)
    assert "LEFT JOIN trend_competitors" in sql
    assert "c.id IS NULL OR c.status <> 'excluded'" in sql


def test_the_theme_rollup_is_null_safe_too():
    sql = inspect.getsource(tstore.theme_rollup)
    assert "LEFT JOIN trend_competitors" in sql
    assert "c.id IS NULL" in sql


def test_region_falls_back_to_the_competitor_when_the_row_has_none():
    """A competitor post carries no region of its own — it inherits the
    account's. A source-less row carries its own. One COALESCE serves both."""
    sql = inspect.getsource(tstore.query_posts)
    assert "COALESCE(p.region, c.region)" in sql


# ---------------------------------------------------------------------------
# The schema itself
# ---------------------------------------------------------------------------

def test_the_posts_table_allows_a_null_competitor():
    assert "ALTER COLUMN competitor_id DROP NOT NULL" in tstore._DDL


def test_existing_databases_get_the_new_columns():
    """`CREATE TABLE IF NOT EXISTS` never re-shapes an existing table, so a
    database predating these columns needs the explicit ALTERs or every
    source-less insert fails."""
    for col in ("source", "region", "language", "platform"):
        assert f"ADD COLUMN IF NOT EXISTS {col}" in tstore._DDL


def test_source_defaults_to_competitor_so_existing_rows_stay_valid():
    assert "source" in tstore._DDL
    assert "DEFAULT 'competitor'" in tstore._DDL


def test_source_is_indexed_because_the_feed_filters_on_it():
    assert "idx_trend_posts_source" in tstore._DDL


# ---------------------------------------------------------------------------
# The source vocabulary
# ---------------------------------------------------------------------------

def test_competitor_is_always_a_valid_source():
    assert "competitor" in tstore.POST_SOURCES


def test_upsert_accepts_a_null_competitor():
    """Signature guard: a source-less row passes competitor_id=None."""
    sig = inspect.signature(tstore.upsert_post)
    assert "Optional" in str(sig.parameters["competitor_id"].annotation)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
