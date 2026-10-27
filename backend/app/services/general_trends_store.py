"""General Trends — persistence for refresh runs, clustered trends and sightings.

Three tables, all created idempotently (``CREATE TABLE IF NOT EXISTS``) the
first time the store is used, so there is no separate migration step. Run this
module directly to create them ahead of time:

    backend/.venv/bin/python -m app.services.general_trends_store

The model in one paragraph: a **run** is one refresh — every enabled public
source (Google Trends, Bluesky, Reddit, Know Your Meme, Wikipedia, YouTube,
X-via-trends24) fetched once for every configured region. Every item a source
returns is a **sighting**; sightings are clustered (exact source key → exact
slug → exact canonical name → lexical name overlap; embeddings are only
candidate *hints*, never a decision) into one **trend** row each, which carries
the merged evidence (sources / regions / best image / peak metric), a
**velocity** (0..1 heat, recomputed every run) and, after the AI pass, the same
fit / UAE / angle chips the competitor feed shows.

Two verdicts are also persisted here so nothing has to be re-derived on read:
``safety`` / ``safety_reason`` (the ingest screen's, escalate-only) and
``latest_content_at`` (when the content behind the newest sighting actually
happened, as opposed to when we last fetched it). Both exist so a filtered or
stale row can be kept for audit while never being shown and never being sent
to the model.

This module is deliberately independent of ``trend_store.py`` — the competitor
finder is not touched — but it follows the same conventions: reads on
``get_conn(autocommit=True)``, writes inside one transaction, JSONB via
``Jsonb``, embeddings bound as ``list[float]`` with ``%s::vector``.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Iterable, Optional

from psycopg.types.json import Jsonb

from ..db import get_conn

log = logging.getLogger("archivist.general_trends_store")

RUN_STATUSES = {"queued", "fetching", "clustering", "scoring", "done", "failed"}
TREND_STATUSES = {"new", "saved", "dismissed", "used"}
SENSITIVITIES = {"ok", "caution", "avoid"}
TREND_KINDS = {"meme", "format", "sound", "challenge", "news", "event",
               "product", "culture", "other"}

# The four kinds that mean "people are making their own version of this" — the
# only ones the feed shows by default. See `query_trends(kinds=...)`.
PARTICIPATORY_KINDS = ("meme", "format", "sound", "challenge")

# Scorer output with no column of its own, merged into `metadata` by
# `save_score` instead of being silently dropped.
_SCORE_METADATA_KEYS = ("replication", "confidence", "evidence_quality",
                        "skip_reason", "identified", "is_trend", "freshness")

# A run with no terminal status and no sign of life for this long is treated
# as orphaned (the backend restarted mid-run) so it can never wedge the UI's
# poll loop. "Sign of life" = the run's last heartbeat: every ``update_run``
# stamps ``metadata.heartbeat_at``, so a long but healthy first refresh (many
# scoring batches) is measured from its latest progress write, not from the
# moment it was queued.
RUN_STALE_AFTER_SEC = 30 * 60

# SQL for "when did this run last show signs of life" — the later of when it
# was queued and its last heartbeat (runs written before heartbeats existed
# have none, so started_at stands in).
_RUN_LIVENESS_SQL = (
    "GREATEST(started_at, "
    "COALESCE((metadata->>'heartbeat_at')::timestamptz, started_at))"
)

_DDL = """
CREATE TABLE IF NOT EXISTS general_trend_runs (
    id             BIGSERIAL PRIMARY KEY,
    status         TEXT        NOT NULL DEFAULT 'queued',
    phase          TEXT,
    progress       TEXT,
    sources_total  INT         NOT NULL DEFAULT 0,
    sources_done   INT         NOT NULL DEFAULT 0,
    items_found    INT         NOT NULL DEFAULT 0,
    trends_new     INT         NOT NULL DEFAULT 0,
    trends_touched INT         NOT NULL DEFAULT 0,
    trends_scored  INT         NOT NULL DEFAULT 0,
    error          TEXT,
    started_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at    TIMESTAMPTZ,
    metadata       JSONB       NOT NULL DEFAULT '{}'::jsonb
);
CREATE INDEX IF NOT EXISTS idx_general_trend_runs_recent
    ON general_trend_runs (started_at DESC);

CREATE TABLE IF NOT EXISTS general_trends (
    id                BIGSERIAL PRIMARY KEY,
    slug              TEXT        NOT NULL UNIQUE,
    title             TEXT        NOT NULL,
    summary           TEXT,
    url               TEXT,
    image_url         TEXT,
    local_thumb_path  TEXT,
    category          TEXT,
    regions           JSONB       NOT NULL DEFAULT '[]'::jsonb,
    sources           JSONB       NOT NULL DEFAULT '[]'::jsonb,
    source_count      INT         NOT NULL DEFAULT 0,
    sighting_count    INT         NOT NULL DEFAULT 0,
    evidence_count    INT         NOT NULL DEFAULT 0,
    peak_metric       REAL,
    velocity          REAL,
    first_seen_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    latest_content_at TIMESTAMPTZ,
    canonical_key     TEXT,
    safety            TEXT        NOT NULL DEFAULT 'ok',
    safety_reason     TEXT,
    embedding         vector(768),
    topic             TEXT,
    topic_summary     TEXT,
    theme             TEXT,
    trend_kind        TEXT,
    sensitivity       TEXT,
    fit_score         INT,
    fit_reason        TEXT,
    uae_relevance     INT,
    suggested_angle   TEXT,
    suggested_format  TEXT,
    effort            TEXT,
    tags              JSONB       NOT NULL DEFAULT '[]'::jsonb,
    opportunity_score REAL,
    scored_at         TIMESTAMPTZ,
    status            TEXT        NOT NULL DEFAULT 'new',
    metadata          JSONB       NOT NULL DEFAULT '{}'::jsonb,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);
-- Bring an EXISTING database up to the shape above. The table is created with
-- CREATE TABLE IF NOT EXISTS, so a database written before the ingest screen
-- (safety / freshness / evidence) never gets those columns from the CREATE.
--
-- ORDERING IS LOAD-BEARING: every ALTER must run BEFORE any index that names a
-- new column. Creating the index first fails with UndefinedColumn on a
-- pre-existing database, which aborts the WHOLE DDL statement and kills every
-- poller on ensure_tables() — exactly the outage commit 49369f3 fixed for
-- trend_posts.source.
ALTER TABLE general_trends ADD COLUMN IF NOT EXISTS safety            TEXT NOT NULL DEFAULT 'ok';
ALTER TABLE general_trends ADD COLUMN IF NOT EXISTS safety_reason     TEXT;
ALTER TABLE general_trends ADD COLUMN IF NOT EXISTS latest_content_at TIMESTAMPTZ;
ALTER TABLE general_trends ADD COLUMN IF NOT EXISTS evidence_count    INT NOT NULL DEFAULT 0;
ALTER TABLE general_trends ADD COLUMN IF NOT EXISTS canonical_key     TEXT;

CREATE INDEX IF NOT EXISTS idx_general_trends_rank
    ON general_trends (status, opportunity_score DESC NULLS LAST);
CREATE INDEX IF NOT EXISTS idx_general_trends_seen
    ON general_trends (last_seen_at DESC);
CREATE INDEX IF NOT EXISTS idx_general_trends_unscored
    ON general_trends (scored_at) WHERE scored_at IS NULL;
-- The feed's own index: every card query is "recent AND not filtered", and the
-- partial predicate keeps the flagged rows (kept only for audit) out of it.
CREATE INDEX IF NOT EXISTS idx_general_trends_feed
    ON general_trends (last_seen_at DESC) WHERE safety = 'ok';
-- The exact-match clustering rung: run N+1 finds run N's card by the label the
-- cheap pass gave it, with no model call and no vector maths.
CREATE INDEX IF NOT EXISTS idx_general_trends_canonical
    ON general_trends (canonical_key) WHERE canonical_key IS NOT NULL;

CREATE TABLE IF NOT EXISTS general_trend_sightings (
    id           BIGSERIAL PRIMARY KEY,
    trend_id     BIGINT      NOT NULL
                 REFERENCES general_trends(id) ON DELETE CASCADE,
    run_id       BIGINT      REFERENCES general_trend_runs(id) ON DELETE SET NULL,
    source       TEXT        NOT NULL,
    source_key   TEXT        NOT NULL,
    title        TEXT,
    url          TEXT,
    image_url    TEXT,
    summary      TEXT,
    metric_value REAL,
    metric_label TEXT,
    region       TEXT,
    category     TEXT,
    published_at TIMESTAMPTZ,
    seen_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    extra        JSONB       NOT NULL DEFAULT '{}'::jsonb,
    UNIQUE (run_id, source, source_key)
);
CREATE INDEX IF NOT EXISTS idx_general_trend_sightings_trend
    ON general_trend_sightings (trend_id, seen_at DESC);
CREATE INDEX IF NOT EXISTS idx_general_trend_sightings_key
    ON general_trend_sightings (source, source_key, seen_at DESC);
"""

RUN_COLS = (
    "id, status, phase, progress, sources_total, sources_done, items_found, "
    "trends_new, trends_touched, trends_scored, error, started_at, finished_at, "
    "metadata"
)

# Everything on the trend row EXCEPT the embedding — the vector is only ever
# read through `nearest_trends`, never shipped to the UI.
#
# Two counters, deliberately: `sighting_count` is a POLL counter (bumped on
# every re-poll — "UAE Ministry of Defence" reads 11 from 4 real items on one
# source) and `evidence_count` is the honest one (distinct source+key pairs).
# Ranking must use evidence_count; nothing may rank on raw sighting_count.
# Two clocks, also deliberately: `last_seen_at` = "does a source still list
# it", `latest_content_at` = "is anything new actually happening".
TREND_COLS = (
    "id, slug, canonical_key, title, summary, url, image_url, local_thumb_path, "
    "category, regions, sources, source_count, sighting_count, evidence_count, "
    "peak_metric, velocity, first_seen_at, last_seen_at, latest_content_at, "
    "topic, topic_summary, theme, trend_kind, sensitivity, safety, "
    "safety_reason, fit_score, fit_reason, uae_relevance, suggested_angle, "
    "suggested_format, effort, tags, opportunity_score, scored_at, status, "
    "metadata, created_at, updated_at"
)

SIGHTING_COLS = (
    "id, trend_id, run_id, source, source_key, title, url, image_url, summary, "
    "metric_value, metric_label, region, category, published_at, seen_at, extra"
)

_ensured = False


async def ensure_tables() -> None:
    """Create the three tables + indexes if missing. Runs once per process."""
    global _ensured
    if _ensured:
        return
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(_DDL)
    _ensured = True
    log.info("general trends tables ensured")


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def _prefixed(cols: str, alias: str) -> str:
    return ", ".join(f"{alias}.{c.strip()}" for c in cols.split(","))


def _parse_ts(value: Any) -> Optional[datetime]:
    """ISO string / datetime -> tz-aware datetime (None if unusable). Sources
    hand us ISO strings; we bind real datetimes so psycopg never has to guess."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, str):
        s = value.strip()
        if s.endswith(("Z", "z")):
            s = s[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(s)
        except ValueError:
            return None
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    return None


def _distinct(values: Optional[Iterable[Any]]) -> list[str]:
    """Order-preserving distinct list of non-empty strings."""
    out: list[str] = []
    for v in values or []:
        s = str(v).strip() if v is not None else ""
        if s and s not in out:
            out.append(s)
    return out


def _float_or_none(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _content_time(item: dict) -> Optional[datetime]:
    """When the CONTENT behind this item happened — NOT when we fetched it.

    `published_at` does not mean the same thing for every source: for Reddit /
    HN / Google News it is real content age, but for Wikipedia and trends24 the
    feed IS the window (measured medians 46.5h and 0.7h — both constants), so
    their timestamp says nothing about whether anything new is happening.
    `general_trends_freshness.content_time` owns that per-source table; this
    module only consumes it, and only if it is importable, so the store still
    works on its own (and in its own tests) before/without that module:

      1. ``item["content_at"]`` — an explicit answer from the ingest screen.
      2. ``general_trends_freshness.content_time(item)`` when available.
      3. ``item["published_at"]`` as the last resort.
    """
    explicit = _parse_ts(item.get("content_at"))
    if explicit is not None:
        return explicit
    try:  # local import: optional dependency, and monkeypatchable in tests
        from .general_trends_freshness import content_time  # noqa: PLC0415
    except Exception:  # noqa: BLE001 — module not deployed yet
        return _parse_ts(item.get("published_at"))
    try:
        return _parse_ts(content_time(item))
    except Exception:  # noqa: BLE001 — a screen must never break a write
        return _parse_ts(item.get("published_at"))


def _finalise_run(run: dict, age_sec: Optional[float] = None) -> dict:
    """Mark orphaned runs failed and add the `active` flag the UI polls on."""
    running = run.get("status") not in ("done", "failed")
    if running and age_sec is not None and age_sec > RUN_STALE_AFTER_SEC:
        run["status"] = "failed"
        run["error"] = run.get("error") or (
            "the refresh stopped unexpectedly (the server restarted) — run it again"
        )
    run["active"] = run.get("status") not in ("done", "failed")
    return run


# ---------------------------------------------------------------------------
# Runs
# ---------------------------------------------------------------------------

async def create_run(metadata: Optional[dict] = None) -> dict:
    await ensure_tables()
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                f"INSERT INTO general_trend_runs (status, phase, metadata) "
                f"VALUES ('queued', 'starting', %s) RETURNING {RUN_COLS}",
                (Jsonb(metadata or {}),),
            )
            return _finalise_run(dict(await cur.fetchone()))


async def update_run(run_id: int, **fields: Any) -> None:
    """Patch a run. Only whitelisted columns are settable; a terminal status
    stamps `finished_at`. Every real update also stamps
    ``metadata.heartbeat_at = now()`` — the liveness signal ``fail_stale_runs``
    and ``latest_run`` measure staleness from."""
    await ensure_tables()
    allowed = {"status", "phase", "progress", "sources_total", "sources_done",
               "items_found", "trends_new", "trends_touched", "trends_scored",
               "error", "metadata"}
    sets, params = [], []
    metadata_set = False
    for key, value in fields.items():
        if key not in allowed:
            continue
        if key == "metadata":
            # Replace the document but keep the heartbeat on it.
            sets.append("metadata = %s::jsonb "
                        "|| jsonb_build_object('heartbeat_at', now())")
            params.append(Jsonb(value or {}))
            metadata_set = True
            continue
        sets.append(f"{key} = %s")
        params.append(value)
    if fields.get("status") in ("done", "failed"):
        sets.append("finished_at = now()")
    if not sets:
        return
    if not metadata_set:
        sets.append("metadata = metadata || jsonb_build_object('heartbeat_at', now())")
    params.append(run_id)
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                f"UPDATE general_trend_runs SET {', '.join(sets)} WHERE id = %s",
                params,
            )


async def heartbeat_run(run_id: int) -> None:
    """Stamp ``metadata.heartbeat_at = now()`` on a live run without touching
    anything else — for the long, silent phases (a strict Reddit window can hold
    the fetch for 15+ minutes with nothing else to report)."""
    await ensure_tables()
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "UPDATE general_trend_runs "
                "   SET metadata = metadata || jsonb_build_object('heartbeat_at', now()) "
                " WHERE id = %s AND status NOT IN ('done', 'failed')",
                (run_id,),
            )


async def pending_runs(limit: int = 1) -> list[dict]:
    """Queued runs for the poller to pick up. The queue lives in the DB so a
    refresh survives a backend restart."""
    await ensure_tables()
    async with get_conn(autocommit=True) as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                f"SELECT {RUN_COLS} FROM general_trend_runs WHERE status = 'queued' "
                f"ORDER BY started_at LIMIT %s",
                (int(limit),),
            )
            return [dict(r) for r in await cur.fetchall()]


async def claim_run(run_id: int) -> bool:
    """Atomically move a queued run to 'fetching'. False if someone beat us —
    two poller ticks must never run the same refresh twice."""
    await ensure_tables()
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "UPDATE general_trend_runs SET status = 'fetching', phase = 'fetching' "
                "WHERE id = %s AND status = 'queued'",
                (run_id,),
            )
            return cur.rowcount > 0


async def fail_stale_runs() -> int:
    """Close out runs orphaned by a restart, so the UI never polls forever.
    A run is stale when it has shown no sign of life (no ``update_run``
    heartbeat, or none since it was queued) for RUN_STALE_AFTER_SEC."""
    await ensure_tables()
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                f"""
                UPDATE general_trend_runs
                   SET status = 'failed', finished_at = now(),
                       error = COALESCE(error,
                           'the refresh stopped unexpectedly (the server restarted) — run it again')
                 WHERE status NOT IN ('done', 'failed')
                   AND {_RUN_LIVENESS_SQL} < now() - (%s || ' seconds')::interval
                """,
                (str(RUN_STALE_AFTER_SEC),),
            )
            return cur.rowcount


async def latest_run() -> Optional[dict]:
    await ensure_tables()
    async with get_conn(autocommit=True) as conn:
        async with conn.cursor() as cur:
            # age_sec = seconds since the run last showed signs of life (its
            # latest heartbeat, or when it was queued) — what `_finalise_run`
            # compares against RUN_STALE_AFTER_SEC.
            await cur.execute(
                f"SELECT {RUN_COLS}, "
                f"EXTRACT(EPOCH FROM (now() - {_RUN_LIVENESS_SQL})) AS age_sec "
                f"FROM general_trend_runs ORDER BY started_at DESC LIMIT 1"
            )
            row = await cur.fetchone()
            if not row:
                return None
            run = dict(row)
            age = float(run.pop("age_sec", 0) or 0)
            return _finalise_run(run, age)


async def run_is_active() -> bool:
    run = await latest_run()
    return bool(run and run.get("active"))


# ---------------------------------------------------------------------------
# Trends — lookup
# ---------------------------------------------------------------------------

async def get_trend(trend_id: int) -> Optional[dict]:
    await ensure_tables()
    async with get_conn(autocommit=True) as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                f"SELECT {TREND_COLS} FROM general_trends WHERE id = %s", (trend_id,)
            )
            row = await cur.fetchone()
            return dict(row) if row else None


async def find_trend_by_slug(slug: str, *, since_days: int = 30) -> Optional[dict]:
    """Rung 2 of the clustering ladder: the recent trend with this exact
    normalised title.

    WINDOWED on purpose. Unwindowed, a generic slug ("supreme court", "deaths
    in 2026") is a permanent magnet: it matches forever, so every later story
    that happens to normalise the same way is folded into a card that stopped
    being about anything months ago. 30 days matches the source-key rung."""
    await ensure_tables()
    if not slug:
        return None
    async with get_conn(autocommit=True) as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                f"""
                SELECT {TREND_COLS} FROM general_trends
                 WHERE slug = %s
                   AND last_seen_at >= now() - (%s || ' days')::interval
                """,
                (slug, str(int(since_days))),
            )
            row = await cur.fetchone()
            return dict(row) if row else None


async def find_trend_by_canonical_key(
    key: str, *, since_days: int = 30,
) -> Optional[dict]:
    """Rung 3: the recent trend the cheap labelling pass gave the same canonical
    name. This is what turns a hard similarity problem into an exact-match one —
    it costs no model call at lookup time and it crosses languages
    ("الدرعية ضد النصر" and "diriyah vs al-nassr" both label to the same key),
    which the embeddings provably cannot (the closest pair in the whole 363-row
    corpus, at cosine 0.1230, is a FALSE match)."""
    await ensure_tables()
    key = (key or "").strip()
    if not key:
        return None
    async with get_conn(autocommit=True) as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                f"""
                SELECT {TREND_COLS} FROM general_trends
                 WHERE canonical_key = %s
                   AND last_seen_at >= now() - (%s || ' days')::interval
                 ORDER BY last_seen_at DESC
                 LIMIT 1
                """,
                (key, str(int(since_days))),
            )
            row = await cur.fetchone()
            return dict(row) if row else None


async def find_trend_by_source_key(
    source: str, source_key: str, since_days: int = 30,
) -> Optional[dict]:
    """The trend a source's item was last filed under — the same source seeing
    the same thing again must land on the same card."""
    await ensure_tables()
    if not source or not source_key:
        return None
    async with get_conn(autocommit=True) as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                f"""
                SELECT {_prefixed(TREND_COLS, 't')}
                  FROM general_trend_sightings s
                  JOIN general_trends t ON t.id = s.trend_id
                 WHERE s.source = %s AND s.source_key = %s
                   AND s.seen_at >= now() - (%s || ' days')::interval
                 ORDER BY s.seen_at DESC
                 LIMIT 1
                """,
                (source, source_key, str(int(since_days))),
            )
            row = await cur.fetchone()
            return dict(row) if row else None


async def nearest_trends(
    embedding: list[float], *, limit: int = 5, since_days: int = 14,
    max_distance: float = 0.20,
) -> list[dict]:
    """KNN over recent trends: `[{id, slug, title, distance}]`, nearest first.
    Cosine distance (0 = identical).

    These are CANDIDATE HINTS, not a decision. Measured over all 65,703 pairs of
    the 363 stored vectors: `count(distance <= 0.12) = 0`, and the closest pair
    in the entire corpus (0.1230, "juju smith" ~ "najee harris") is FALSE — the
    true and false distributions interleave, so no attach threshold exists for
    short titles. `max_distance` is therefore a generous 0.20 whose only job is
    to keep obvious noise out of the labelling prompt these rows feed. Pass
    None for no cap.

    The cap is applied OUTSIDE the ordered sub-select so the KNN ordering and
    LIMIT stay index-shaped."""
    await ensure_tables()
    if not embedding:
        return []
    inner = """
                SELECT id, slug, title, embedding <=> %s::vector AS distance
                  FROM general_trends
                 WHERE embedding IS NOT NULL
                   AND last_seen_at >= now() - (%s || ' days')::interval
                 ORDER BY distance
                 LIMIT %s
    """
    params: list[Any] = [embedding, str(int(since_days)), int(limit)]
    if max_distance is None:
        sql = inner
    else:
        sql = (f"SELECT id, slug, title, distance FROM ({inner}) d "
               f"WHERE d.distance <= %s ORDER BY d.distance")
        params.append(float(max_distance))
    async with get_conn(autocommit=True) as conn:
        async with conn.cursor() as cur:
            await cur.execute(sql, params)
            out = []
            for r in await cur.fetchall():
                row = dict(r)
                row["distance"] = float(row.get("distance") or 0.0)
                out.append(row)
            return out


# ---------------------------------------------------------------------------
# Trends — write
# ---------------------------------------------------------------------------

async def create_trend(fields: dict) -> dict:
    """Insert a brand-new trend row. `fields["embedding"]` (list[float] or
    None) is bound with `%s::vector`; None becomes SQL NULL without a cast."""
    await ensure_tables()
    embedding = fields.get("embedding")
    if isinstance(embedding, (list, tuple)) and len(embedding) == 0:
        embedding = None
    sources = _distinct(fields.get("sources"))
    regions = _distinct(fields.get("regions"))
    source_count = fields.get("source_count")
    source_count = int(source_count) if source_count is not None else len(sources)
    emb_sql = "%s::vector" if embedding is not None else "NULL"

    # safety is NOT NULL: an absent verdict means "screened clean" ('ok'), never
    # NULL, so `WHERE safety = 'ok'` on the feed can stay a plain equality.
    safety = str(fields.get("safety") or "").strip() or None
    params: list[Any] = [
        fields.get("slug"), (fields.get("canonical_key") or None),
        fields.get("title"), fields.get("summary") or None,
        fields.get("url") or None, fields.get("image_url") or None,
        fields.get("category") or None,
        Jsonb(regions), Jsonb(sources), source_count,
        int(fields.get("sighting_count") or 0),
        _float_or_none(fields.get("peak_metric")),
        safety, (fields.get("safety_reason") or None),
        _parse_ts(fields.get("first_seen_at")),
        _parse_ts(fields.get("last_seen_at")),
        _parse_ts(fields.get("latest_content_at")) or _content_time(fields),
    ]
    if embedding is not None:
        params.append(list(embedding))
    params.append(Jsonb(fields.get("metadata") or {}))

    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                f"""
                INSERT INTO general_trends
                    (slug, canonical_key, title, summary, url, image_url,
                     category, regions, sources, source_count, sighting_count,
                     peak_metric, safety, safety_reason,
                     first_seen_at, last_seen_at, latest_content_at,
                     embedding, metadata)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                        COALESCE(%s, 'ok'), %s,
                        COALESCE(%s, now()), COALESCE(%s, now()), %s,
                        {emb_sql}, %s)
                RETURNING {TREND_COLS}
                """,
                params,
            )
            return dict(await cur.fetchone())


async def touch_trend(trend_id: int, *, item: dict, seen_at: datetime) -> None:
    """Fold one more sighting into a trend, in ONE UPDATE:

      * sighting_count + 1 — the POLL counter, deliberately dumb
      * evidence_count = distinct (source, source_key) actually on file — the
        honest one, recomputed rather than incremented (on the live corpus
        "UAE Ministry of Defence" polls 11 times off 4 real items). Ranking
        reads this; nothing may rank on sighting_count.
      * sources / regions = distinct union with the item's (source_count follows)
      * image_url / url / summary / category filled from the item ONLY when
        currently empty
      * last_seen_at = seen_at, peak_metric = GREATEST(peak_metric, item metric)
      * safety ESCALATES ONLY: one flagged sighting flags the card and no later
        clean sighting can quietly un-flag it. The screen is the authority on
        "this is not safe"; it is not an authority on "this is safe again".
      * latest_content_at moves FORWARD ONLY, from the item's content time —
        an older item resurfacing must not rewind "when did something last
        happen here".

    An empty source/region appends `[]` (a no-op) rather than branching on NULL,
    so every bound parameter has an inferable type.

    Caveat worth knowing: `last_seen_at` is refreshed unconditionally, so
    anything a source keeps listing never ages out of the 7-day feed window on
    its own. That is correct for `last_seen_at`'s job ("a source still lists
    it") — the ageing-out is `latest_content_at`'s job, and keeping evergreens
    out in the first place is the fetcher's (the Wikipedia rise gate)."""
    await ensure_tables()
    source = str(item.get("source") or "").strip()
    region = str(item.get("region") or "").strip()
    src_add = Jsonb([source] if source else [])
    reg_add = Jsonb([region] if region else [])
    safety = str(item.get("safety") or "").strip().lower()
    safety_reason = str(item.get("safety_reason") or "").strip() or None
    content_at = _content_time(item)
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                UPDATE general_trends
                   SET sighting_count = sighting_count + 1,
                       evidence_count = (
                           SELECT COUNT(DISTINCT (s.source, s.source_key))
                             FROM general_trend_sightings s
                            WHERE s.trend_id = general_trends.id),
                       sources = CASE WHEN sources ? %s::text
                                      THEN sources ELSE sources || %s END,
                       source_count = jsonb_array_length(
                                      CASE WHEN sources ? %s::text
                                           THEN sources ELSE sources || %s END),
                       regions = CASE WHEN regions ? %s::text
                                      THEN regions ELSE regions || %s END,
                       image_url = COALESCE(NULLIF(image_url, ''), %s::text),
                       url       = COALESCE(NULLIF(url, ''), %s::text),
                       summary   = COALESCE(NULLIF(summary, ''), %s::text),
                       category  = COALESCE(NULLIF(category, ''), %s::text),
                       safety        = CASE WHEN %s::text = 'flagged'
                                            THEN 'flagged' ELSE safety END,
                       safety_reason = CASE WHEN %s::text = 'flagged'
                                            THEN COALESCE(%s::text, safety_reason)
                                            ELSE safety_reason END,
                       last_seen_at = %s,
                       latest_content_at = GREATEST(
                           COALESCE(latest_content_at, %s::timestamptz),
                           %s::timestamptz),
                       peak_metric  = GREATEST(peak_metric, %s::real),
                       updated_at   = now()
                 WHERE id = %s
                """,
                (
                    source, src_add,
                    source, src_add,
                    region, reg_add,
                    item.get("image_url") or None,
                    item.get("url") or None,
                    item.get("summary") or None,
                    item.get("category") or None,
                    safety or None,
                    safety or None, safety_reason,
                    _parse_ts(seen_at) or datetime.now(timezone.utc),
                    content_at, content_at,
                    _float_or_none(item.get("metric_value")),
                    trend_id,
                ),
            )


async def set_canonical_key(trend_id: int, key: str) -> None:
    """Stamp the label the cheap pass gave this trend, so the NEXT run can find
    it by exact match (`find_trend_by_canonical_key`) with no model call.
    A blank key is a no-op: never overwrite a good key with nothing."""
    await ensure_tables()
    key = (key or "").strip()
    if not key or not trend_id:
        return
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "UPDATE general_trends SET canonical_key = %s, updated_at = now() "
                "WHERE id = %s",
                (key, int(trend_id)),
            )


async def insert_sighting(
    trend_id: int, run_id: Optional[int], item: dict,
    seen_at: Optional[datetime] = None,
) -> None:
    """Record one raw item under a trend. Idempotent per (run, source, key)."""
    await ensure_tables()
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                INSERT INTO general_trend_sightings
                    (trend_id, run_id, source, source_key, title, url, image_url,
                     summary, metric_value, metric_label, region, category,
                     published_at, seen_at, extra)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                        COALESCE(%s, now()), %s)
                ON CONFLICT (run_id, source, source_key) DO NOTHING
                """,
                (
                    trend_id, run_id, str(item.get("source") or ""),
                    str(item.get("source_key") or ""), item.get("title"),
                    item.get("url") or None, item.get("image_url") or None,
                    item.get("summary") or None,
                    _float_or_none(item.get("metric_value")),
                    item.get("metric_label") or None,
                    item.get("region") or None, item.get("category") or None,
                    _parse_ts(item.get("published_at")),
                    _parse_ts(seen_at),
                    Jsonb(item.get("extra") or {}),
                ),
            )


async def sightings_for(trend_ids: list[int], *, per_trend: int = 6) -> dict[int, list]:
    """Latest evidence per trend — up to `per_trend` DISTINCT items (the same
    source item seen across several runs counts once). Every requested id gets
    a key, possibly with an empty list."""
    await ensure_tables()
    ids = [int(i) for i in (trend_ids or [])]
    out: dict[int, list] = {i: [] for i in ids}
    if not ids:
        return out
    async with get_conn(autocommit=True) as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                f"""
                SELECT {SIGHTING_COLS}
                  FROM (
                    SELECT d.*, ROW_NUMBER() OVER (
                               PARTITION BY d.trend_id
                               ORDER BY d.seen_at DESC, d.id DESC) AS rn
                      FROM (
                        SELECT DISTINCT ON (trend_id, source, source_key) *
                          FROM general_trend_sightings
                         WHERE trend_id = ANY(%s)
                         ORDER BY trend_id, source, source_key, seen_at DESC, id DESC
                      ) d
                  ) s
                 WHERE rn <= %s
                 ORDER BY trend_id, seen_at DESC, id DESC
                """,
                (ids, int(per_trend)),
            )
            for r in await cur.fetchall():
                row = dict(r)
                out.setdefault(int(row["trend_id"]), []).append(row)
    return out


async def recent_trend_slugs(since_days: int = 14) -> list[dict]:
    """`[{id, slug, title}, ...]` for every trend seen in the window, oldest
    first — the in-memory candidate list for the pipeline's lexical matcher
    (name containment / token overlap), which embeddings can't do reliably
    for short titles."""
    await ensure_tables()
    async with get_conn(autocommit=True) as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT id, slug, title
                  FROM general_trends
                 WHERE last_seen_at >= now() - (%s || ' days')::interval
                 ORDER BY first_seen_at ASC, id ASC
                """,
                (str(int(since_days)),),
            )
            return [dict(r) for r in await cur.fetchall()]


# How much history the LAG window reaches back beyond the returned window, as a
# multiple of it. Without headroom the OLDEST row in the window has no
# predecessor to compare against and every trend loses its growth signal on the
# edge of the window; 3x is one extra day of polls at the 6-hourly cadence.
_VELOCITY_LAG_HEADROOM = 3


async def recent_sightings_for_velocity(since_hours: int = 72) -> dict[int, list[dict]]:
    """`{trend_id: [sighting, ...]}` for every sighting in the window — the raw
    material for `compute_velocity`.

    Every field the ranking module can turn into a signal comes back, because
    the first version of this query selected only `(trend_id, seen_at, source)`
    and that silently starved the whole heat calculation: with no
    `metric_value` every per-source median was 0.0, every amplitude fell back to
    the neutral 0.5, and the amplitude term — 45% of the weight — measured
    nothing. So: `metric_value` and `extra` (amplitude + the vendor growth
    payloads: wikipedia's `views_delta`, tiktok's `popularityCurve`, a feed
    `rank`), `published_at` (freshness, from CONTENT time rather than fetch
    time), and `source_key` + `region` (the distinct-unit corroboration count).

    `prev_metric` / `prev_seen_at` are folded into each sighting's `extra` from
    a `LAG` over that exact `(source, source_key)` series, which is what gives
    run-over-run growth to the sources that ship no vendor delta of their own.
    `compute_velocity` ignores a predecessor younger than its `MIN_GAP_HOURS`,
    so a re-poll twenty minutes later cannot invent a spike."""
    await ensure_tables()
    out: dict[int, list[dict]] = {}
    hours = max(int(since_hours), 0)
    async with get_conn(autocommit=True) as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                WITH history AS (
                    SELECT trend_id, seen_at, source, source_key, region,
                           metric_value, metric_label, published_at, extra,
                           LAG(metric_value) OVER w AS prev_metric,
                           LAG(seen_at)      OVER w AS prev_seen_at
                      FROM general_trend_sightings
                     WHERE seen_at >= now() - (%s || ' hours')::interval
                    WINDOW w AS (PARTITION BY source, source_key ORDER BY seen_at)
                )
                SELECT * FROM history
                 WHERE seen_at >= now() - (%s || ' hours')::interval
                 ORDER BY trend_id, seen_at DESC
                """,
                (str(hours * _VELOCITY_LAG_HEADROOM), str(hours)),
            )
            for r in await cur.fetchall():
                raw_extra = r.get("extra")
                extra = dict(raw_extra) if isinstance(raw_extra, dict) else {}
                # Only stamp a previous observation when there IS one — a None
                # would read as "measured zero" to the growth term.
                if r.get("prev_metric") is not None:
                    extra.setdefault("prev_metric", r["prev_metric"])
                if r.get("prev_seen_at") is not None:
                    extra.setdefault("prev_seen_at", r["prev_seen_at"])
                out.setdefault(int(r["trend_id"]), []).append({
                    "seen_at": r["seen_at"],
                    "source": r["source"],
                    "source_key": r.get("source_key"),
                    "region": r.get("region"),
                    "metric_value": r.get("metric_value"),
                    "metric_label": r.get("metric_label"),
                    "published_at": r.get("published_at"),
                    "extra": extra,
                })
    return out


async def save_velocity(trend_id: int, velocity: float) -> None:
    await ensure_tables()
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "UPDATE general_trends SET velocity = %s, updated_at = now() WHERE id = %s",
                (float(velocity), trend_id),
            )


async def unscored_trends(limit: int = 400) -> list[dict]:
    """Trends waiting on the AI pass — hottest first, so a partial scoring run
    still covers what matters. Only what was seen in the last 14 days.

    A flagged trend is stored for audit and NEVER sent to the model: it can
    never be shown, so paying to score it is pure waste (about a third of the
    108 `avoid` rows of the measured run). `limit` is 400 because 200 left 163
    of a 363-row cold start unscored forever."""
    await ensure_tables()
    async with get_conn(autocommit=True) as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                f"""
                SELECT {TREND_COLS} FROM general_trends
                 WHERE scored_at IS NULL
                   AND safety = 'ok'
                   AND last_seen_at >= now() - interval '14 days'
                 ORDER BY velocity DESC NULLS LAST, last_seen_at DESC
                 LIMIT %s
                """,
                (int(limit),),
            )
            return [dict(r) for r in await cur.fetchall()]


async def save_score(trend_id: int, score: dict) -> None:
    """Persist one AI verdict. `clean_title`, when present, becomes the display
    title (the slug — the exact-match key — is untouched).

    Fields the model returns that have no column of their own are merged into
    `metadata` rather than dropped. `replication` is the important one: it is
    the model's answer to "who is making their own version of this", which is
    what `trend_kind` is derived FROM, so keeping it is the only way to audit a
    kind you disagree with."""
    await ensure_tables()
    clean_title = (score.get("clean_title") or "").strip() or None
    extra = {k: score[k] for k in _SCORE_METADATA_KEYS
             if score.get(k) not in (None, "", [])}
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                UPDATE general_trends
                   SET title = COALESCE(%s, title),
                       topic = %s, topic_summary = %s, theme = %s,
                       trend_kind = %s, sensitivity = %s,
                       fit_score = %s, fit_reason = %s, uae_relevance = %s,
                       suggested_angle = %s, suggested_format = %s, effort = %s,
                       tags = %s, opportunity_score = %s,
                       metadata = COALESCE(metadata, '{}'::jsonb) || %s,
                       scored_at = now(), updated_at = now()
                 WHERE id = %s
                """,
                (clean_title,
                 score.get("topic"), score.get("topic_summary"), score.get("theme"),
                 score.get("trend_kind"), score.get("sensitivity"),
                 score.get("fit_score"), score.get("fit_reason"),
                 score.get("uae_relevance"), score.get("suggested_angle"),
                 score.get("suggested_format"), score.get("effort"),
                 Jsonb(score.get("tags") or []), score.get("opportunity_score"),
                 Jsonb(extra),
                 trend_id),
            )


async def clear_scores(ids: Optional[list[int]] = None) -> int:
    """Mark trends unscored so the AI pass re-runs (e.g. after the brand changed)."""
    await ensure_tables()
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            if ids:
                await cur.execute(
                    "UPDATE general_trends SET scored_at = NULL WHERE id = ANY(%s)",
                    ([int(i) for i in ids],),
                )
            else:
                await cur.execute("UPDATE general_trends SET scored_at = NULL")
            return cur.rowcount


async def update_status(trend_id: int, status: str) -> Optional[dict]:
    """Save / dismiss / mark used. Unknown status ⇒ None (caller maps to 422)."""
    await ensure_tables()
    if status not in TREND_STATUSES:
        return None
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                f"UPDATE general_trends SET status = %s, updated_at = now() "
                f"WHERE id = %s RETURNING {TREND_COLS}",
                (status, trend_id),
            )
            row = await cur.fetchone()
            return dict(row) if row else None


async def set_local_thumb(trend_id: int, rel_path: str) -> None:
    await ensure_tables()
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "UPDATE general_trends SET local_thumb_path = %s, updated_at = now() "
                "WHERE id = %s",
                (rel_path, trend_id),
            )


async def set_metadata(trend_id: int, patch: dict) -> None:
    """Shallow-merge `patch` into `general_trends.metadata` and touch nothing else.

    `save_score` also merges metadata, but it is NOT reusable for this: the same
    UPDATE writes topic / topic_summary / fit_score / tags / scored_at from the
    same dict, so calling it with an unrelated payload would blank a trend's
    whole AI verdict. Hence a setter whose SET list is one column.

    The caller this exists for is the "what is this trend ACTUALLY about"
    web lookup (`general_trend_ideation.research_trend`). That answer costs a
    grounded model call, is stable for days, and without a durable home it is
    paid for again by every worker process and lost on every restart.

    Merge, not replace: `metadata` also carries first_source, the scorer's
    replication note and the heat signals, and a research write must not
    stand on any of them."""
    await ensure_tables()
    if not isinstance(patch, dict) or not patch:
        return
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "UPDATE general_trends "
                "   SET metadata = COALESCE(metadata, '{}'::jsonb) || %s, "
                "       updated_at = now() "
                " WHERE id = %s",
                (Jsonb(patch), trend_id),
            )


# ---------------------------------------------------------------------------
# Trends — read for the feed
# ---------------------------------------------------------------------------

async def query_trends(
    *, status: Optional[str] = None, region: Optional[str] = None,
    source: Optional[str] = None, theme: Optional[str] = None,
    kind: Optional[str] = None, min_fit: Optional[int] = None,
    q: Optional[str] = None, include_sensitive: bool = False,
    include_filtered: bool = False, since_days: Optional[int] = 7,
    content_days: Optional[int] = 30, cap: int = 1000,
    kinds: Optional[Iterable[str]] = None,
    include_unclassified: bool = False,
) -> list[dict]:
    """Filtered trends for the feed.

    Sorting and the opportunity blend happen in Python (see
    `general_trends_ranking.rank_trends`) so the weight sliders can re-rank
    instantly without a refresh — `cap` bounds how much is pulled in for that.

    TWO time windows, doing disjoint jobs:

      * `since_days`   -> last_seen_at:      does a source STILL list this?
      * `content_days` -> latest_content_at: is anything NEW happening here?

    `first_seen_at` must never gate a card. A trend first seen three weeks ago
    that is still collecting fresh sightings is exactly what we want to keep —
    gating on when we first noticed it would throw away the running stories and
    keep only whatever we happened to see for the first time this week.

    Three independent hides, each with its own opt-in: `status = 'dismissed'`
    (what the user threw away), `sensitivity = 'avoid'` (the model's verdict,
    revealed by include_sensitive) and `safety <> 'ok'` (the ingest screen's
    verdict, revealed by include_filtered)."""
    await ensure_tables()
    where: list[str] = []
    params: list[Any] = []
    if status:
        where.append("status = %s")
        params.append(status)
    else:
        # The default feed hides what the user already threw away.
        where.append("status <> 'dismissed'")
    if region:
        where.append("regions ? %s")
        params.append(region)
    if source:
        where.append("sources ? %s")
        params.append(source)
    if theme:
        where.append("theme = %s")
        params.append(theme)
    if kind:
        where.append("trend_kind = %s")
        params.append(kind)
    elif kinds:
        # THE PARTICIPATION GATE. A trend is something people are RECREATING —
        # a meme format, a sound, a challenge. A search spike, a headline, a
        # product launch or a Wikipedia pageview is a THING THAT HAPPENED, and
        # no social team can make their own version of it.
        #
        # Measured on the 363 rows of the first live corpus: only 103 were a
        # participatory kind (meme 77, format 21, sound 4, challenge 1). The
        # other 260 were news 126, culture 70, event 40, other 19, product 5 —
        # i.e. 72% of the feed could never have been acted on.
        #
        # `include_unclassified` reveals rows the model has not judged yet.
        # Those are NOT news by default: they are simply pending, and hiding
        # them keeps the feed honest during the window between fetch and score
        # (and when scoring is unavailable, which the run note already says).
        wanted = sorted({str(k).strip().lower() for k in kinds if str(k).strip()})
        if wanted:
            clause = "trend_kind = ANY(%s)"
            params.append(wanted)
            if include_unclassified:
                clause = f"({clause} OR trend_kind IS NULL)"
            where.append(clause)
    if min_fit is not None:
        where.append("COALESCE(fit_score, 0) >= %s")
        params.append(int(min_fit))
    if q and q.strip():
        where.append("(title ILIKE %s OR summary ILIKE %s OR topic ILIKE %s)")
        like = f"%{q.strip()}%"
        params += [like, like, like]
    if not include_sensitive:
        where.append("sensitivity IS DISTINCT FROM 'avoid'")
    if not include_filtered:
        # safety is NOT NULL DEFAULT 'ok', so plain equality is enough and it
        # matches idx_general_trends_feed's partial predicate exactly.
        where.append("safety = 'ok'")
    if since_days is not None and int(since_days) > 0:
        where.append("last_seen_at >= now() - (%s || ' days')::interval")
        params.append(str(int(since_days)))
    if content_days is not None and int(content_days) > 0:
        # NULL = the source has no meaningful content age (Wikipedia's top list,
        # a hashtag board): those are kept, never silently dropped by a clock
        # they cannot answer to.
        where.append("(latest_content_at IS NULL "
                     "OR latest_content_at >= now() - (%s || ' days')::interval)")
        params.append(str(int(content_days)))
    params.append(int(cap))

    sql = f"""
        SELECT {TREND_COLS}
          FROM general_trends
         WHERE {' AND '.join(where)}
         ORDER BY velocity DESC NULLS LAST, last_seen_at DESC
         LIMIT %s
    """
    async with get_conn(autocommit=True) as conn:
        async with conn.cursor() as cur:
            await cur.execute(sql, params)
            return [dict(r) for r in await cur.fetchall()]


async def theme_rollup(since_days: int = 14) -> list[dict]:
    """Scored trends grouped into themes — feeds the Theme filter. Flagged rows
    are excluded: they can never appear in the feed, so they must not inflate a
    theme's count or drag its average fit."""
    await ensure_tables()
    async with get_conn(autocommit=True) as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT theme,
                       COUNT(*)                     AS trend_count,
                       ROUND(AVG(fit_score))::int      AS avg_fit,
                       ROUND(AVG(uae_relevance))::int  AS avg_uae,
                       MAX(opportunity_score)       AS best_score
                  FROM general_trends
                 WHERE theme IS NOT NULL
                   AND status <> 'dismissed'
                   AND safety = 'ok'
                   AND last_seen_at >= now() - (%s || ' days')::interval
                 GROUP BY theme
                 ORDER BY AVG(fit_score) DESC NULLS LAST, COUNT(*) DESC
                """,
                (str(int(since_days)),),
            )
            return [dict(r) for r in await cur.fetchall()]


async def stats() -> dict:
    """Header numbers for the General trends tab."""
    await ensure_tables()
    async with get_conn(autocommit=True) as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT
                  (SELECT COUNT(*) FROM general_trends)                             AS trends_total,
                  (SELECT COUNT(*) FROM general_trends WHERE scored_at IS NULL)     AS trends_unscored,
                  (SELECT COUNT(*) FROM general_trends WHERE status = 'saved')      AS trends_saved,
                  (SELECT COUNT(*) FROM general_trends
                    WHERE first_seen_at >= now() - interval '24 hours')             AS trends_new_24h,
                  (SELECT MAX(finished_at) FROM general_trend_runs
                    WHERE status = 'done')                                          AS last_run_at,
                  (SELECT metadata FROM general_trend_runs
                    ORDER BY started_at DESC LIMIT 1)                               AS latest_metadata
                """
            )
            row = await cur.fetchone()
    out = dict(row) if row else {}
    meta = out.pop("latest_metadata", None) or {}
    sources = meta.get("sources") if isinstance(meta, dict) else None
    sources = sources if isinstance(sources, dict) else {}
    out["sources_total"] = len(sources)
    out["sources_ok"] = sum(
        1 for s in sources.values() if isinstance(s, dict) and s.get("ok")
    )
    for key in ("trends_total", "trends_unscored", "trends_saved", "trends_new_24h"):
        out[key] = int(out.get(key) or 0)
    out.setdefault("last_run_at", None)
    return out


if __name__ == "__main__":  # pragma: no cover — manual table creation
    import asyncio
    import sys

    # psycopg async + Windows: ProactorEventLoop is incompatible. Force Selector
    # BEFORE any asyncio code creates an event loop.
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

    from ..db import close_pool, open_pool

    async def _main() -> None:
        await open_pool()
        try:
            await ensure_tables()
            print("general trends tables ready")
        finally:
            await close_pool()

    asyncio.run(_main())
