"""Trend Finder — persistence for competitors, discovered posts and scan jobs.

Three tables, all created idempotently (``CREATE TABLE IF NOT EXISTS``) the
first time the store is used, so there is no separate migration step. Run this
module directly to create them ahead of time:

    backend/.venv/bin/python -m app.services.trend_store

The model in one paragraph: a **competitor** is an Instagram handle we watch. A
**scan** pulls that handle's recent posts through the Graph API's
``business_discovery`` and stores them as **trend posts**, which a separate AI
pass then scores for how practical they'd be for 42 Abu Dhabi to make. Ranking
is by OUTPERFORMANCE against that account's own median engagement — raw likes
are meaningless across accounts of different sizes (measured: 42abudhabi lands
~0.3% of followers, visitabudhabi ~0.05%).

Two distinct ways to stop watching an account, because they mean different
things to the user:

  * ``status='excluded'``  — "don't include this one". The row SURVIVES, so
    scans skip it and AI research never suggests it again.
  * ``DELETE``             — gone entirely, and its posts cascade away. The
    handle can be re-suggested later.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from psycopg.types.json import Jsonb

from ..db import get_conn

log = logging.getLogger("archivist.trend_store")

COMPETITOR_STATUSES = {"active", "excluded"}
POST_STATUSES = {"new", "saved", "dismissed", "used"}
REGIONS = {"uae", "gcc", "international"}
SCAN_STATUSES = {"queued", "fetching", "scoring", "done", "failed"}
# Where a trend row came from. 'competitor' is the Instagram Business Discovery
# path; 'news' is the AI News collector. They share one table, one scoring pass
# and one ranking formula — the split is only about provenance, which is what
# lets the feed rank both together.
#
# No legacy value is tolerated here: the `source` column did not exist in any
# deployed database before this release, so every pre-existing row takes the
# 'competitor' DEFAULT and nothing can be stranded under a retired name.
POST_SOURCES = {"competitor", "news"}

# A scan older than this with no terminal status is treated as orphaned (the
# backend restarted mid-run) so it can never wedge the UI's poll loop.
SCAN_STALE_AFTER_SEC = 30 * 60

_DDL = """
CREATE TABLE IF NOT EXISTS trend_competitors (
    id                  BIGSERIAL PRIMARY KEY,
    username            TEXT        NOT NULL UNIQUE,
    display_name        TEXT,
    biography           TEXT,
    profile_picture_url TEXT,
    followers_count     BIGINT,
    media_count         BIGINT,
    category            TEXT,
    region              TEXT        NOT NULL DEFAULT 'uae',
    why                 TEXT,
    status              TEXT        NOT NULL DEFAULT 'active',
    source              TEXT        NOT NULL DEFAULT 'manual',
    median_engagement   REAL,
    last_scanned_at     TIMESTAMPTZ,
    last_scan_status    TEXT,
    last_scan_error     TEXT,
    posts_found         INT         NOT NULL DEFAULT 0,
    metadata            JSONB       NOT NULL DEFAULT '{}'::jsonb,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_trend_competitors_status
    ON trend_competitors (status, region);

CREATE TABLE IF NOT EXISTS trend_posts (
    id                 BIGSERIAL PRIMARY KEY,
    -- NULL for AI News: a news story belongs to no watched account.
    competitor_id      BIGINT
                       REFERENCES trend_competitors(id) ON DELETE CASCADE,
    ig_media_id        TEXT        NOT NULL UNIQUE,
    username           TEXT,
    permalink          TEXT,
    media_type         TEXT,
    media_product_type TEXT,
    media_url          TEXT,
    thumbnail_url      TEXT,
    local_thumb_path   TEXT,
    caption            TEXT,
    posted_at          TIMESTAMPTZ,
    like_count         INT,
    comments_count     INT,
    view_count         BIGINT,
    engagement         INT,
    engagement_rate    REAL,
    outperformance     REAL,
    topic              TEXT,
    topic_summary      TEXT,
    theme              TEXT,
    fit_score          INT,
    fit_reason         TEXT,
    uae_relevance      INT,
    suggested_angle    TEXT,
    suggested_format   TEXT,
    effort             TEXT,
    tags               JSONB       NOT NULL DEFAULT '[]'::jsonb,
    opportunity_score  REAL,
    scored_at          TIMESTAMPTZ,
    status             TEXT        NOT NULL DEFAULT 'new',
    -- Which discovery channel produced this row: 'competitor' (Instagram
    -- Business Discovery) or 'news' (the AI News collector). Everything
    -- downstream — scoring, ranking, the feed — treats both identically.
    source             TEXT        NOT NULL DEFAULT 'competitor',
    -- Own the descriptive axes rather than always inheriting them from a
    -- competitor row, because a news story has no competitor to inherit from.
    region             TEXT,
    language           TEXT,
    platform           TEXT,
    metadata           JSONB       NOT NULL DEFAULT '{}'::jsonb,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_trend_posts_rank
    ON trend_posts (status, opportunity_score DESC NULLS LAST);
CREATE INDEX IF NOT EXISTS idx_trend_posts_competitor
    ON trend_posts (competitor_id);
CREATE INDEX IF NOT EXISTS idx_trend_posts_unscored
    ON trend_posts (scored_at) WHERE scored_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_trend_posts_theme ON trend_posts (theme);

-- Bring an EXISTING database up to the shape above. The table is created with
-- CREATE TABLE IF NOT EXISTS, so a database that predates multi-source trends
-- would otherwise keep the old columns and every source-less insert would fail.
ALTER TABLE trend_posts ADD COLUMN IF NOT EXISTS source   TEXT NOT NULL DEFAULT 'competitor';
ALTER TABLE trend_posts ADD COLUMN IF NOT EXISTS region   TEXT;
ALTER TABLE trend_posts ADD COLUMN IF NOT EXISTS language TEXT;
ALTER TABLE trend_posts ADD COLUMN IF NOT EXISTS platform TEXT;
ALTER TABLE trend_posts ALTER COLUMN competitor_id DROP NOT NULL;
-- Index on `source` MUST come after the ADD COLUMN above: on a database that
-- predates the column, creating it first fails with UndefinedColumn and aborts
-- the whole DDL (every trend/news poller then dies on ensure_tables()).
CREATE INDEX IF NOT EXISTS idx_trend_posts_source ON trend_posts (source);

CREATE TABLE IF NOT EXISTS trend_scans (
    id                BIGSERIAL PRIMARY KEY,
    status            TEXT        NOT NULL DEFAULT 'queued',
    phase             TEXT,
    progress          TEXT,
    competitors_total INT         NOT NULL DEFAULT 0,
    competitors_done  INT         NOT NULL DEFAULT 0,
    posts_found       INT         NOT NULL DEFAULT 0,
    posts_scored      INT         NOT NULL DEFAULT 0,
    error             TEXT,
    started_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at       TIMESTAMPTZ,
    metadata          JSONB       NOT NULL DEFAULT '{}'::jsonb
);
CREATE INDEX IF NOT EXISTS idx_trend_scans_recent ON trend_scans (started_at DESC);
"""

COMPETITOR_COLS = (
    "id, username, display_name, biography, profile_picture_url, followers_count, "
    "media_count, category, region, why, status, source, median_engagement, "
    "last_scanned_at, last_scan_status, last_scan_error, posts_found, metadata, "
    "created_at, updated_at"
)

POST_COLS = (
    "id, competitor_id, ig_media_id, username, permalink, media_type, "
    "media_product_type, media_url, thumbnail_url, local_thumb_path, caption, "
    "posted_at, like_count, comments_count, view_count, engagement, "
    "engagement_rate, outperformance, topic, topic_summary, theme, fit_score, "
    "fit_reason, uae_relevance, suggested_angle, suggested_format, effort, tags, "
    "opportunity_score, scored_at, status, source, region, language, platform, "
    "metadata, created_at, updated_at"
)

SCAN_COLS = (
    "id, status, phase, progress, competitors_total, competitors_done, "
    "posts_found, posts_scored, error, started_at, finished_at, metadata"
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
    log.info("trend finder tables ensured")


# Instagram handles are letters, digits, dots and underscores, up to 30 chars,
# and must contain at least one letter or digit — a bare ".." is not a handle,
# and that is exactly what a path-traversal-looking input normalises down to.
#
# This is enforced, not merely tidied: `instagram_client` interpolates the handle
# straight into the Graph API field DSL — `business_discovery.username(X)` — with
# no escaping at all, so validation here is a security boundary.
_HANDLE_RE = __import__("re").compile(r"^(?=.*[A-Za-z0-9])[A-Za-z0-9._]{1,30}$")


class InvalidHandleError(ValueError):
    pass


def normalise_username(raw: str) -> str:
    """`@Foo/`, ` foo `, and a full profile URL all mean the same handle."""
    u = (raw or "").strip().lower()
    for prefix in ("https://www.instagram.com/", "https://instagram.com/",
                   "www.instagram.com/", "instagram.com/"):
        if u.startswith(prefix):
            u = u[len(prefix):]
    return u.strip("@/ ").split("/")[0].split("?")[0]


def validate_username(raw: str) -> str:
    """Normalise AND verify. Use this on anything user- or model-supplied."""
    u = normalise_username(raw)
    if not u or not _HANDLE_RE.match(u):
        raise InvalidHandleError(
            f"“{raw}” isn't a valid Instagram handle — use letters, numbers, "
            f"dots and underscores only."
        )
    return u


# ---------------------------------------------------------------------------
# Competitors
# ---------------------------------------------------------------------------

async def list_competitors(
    *, status: Optional[str] = None, region: Optional[str] = None,
) -> list[dict]:
    await ensure_tables()
    where, params = [], []
    if status:
        where.append("status = %s")
        params.append(status)
    if region:
        where.append("region = %s")
        params.append(region)
    sql = f"SELECT {COMPETITOR_COLS} FROM trend_competitors"
    if where:
        sql += " WHERE " + " AND ".join(where)
    # Active first, then the biggest accounts — the useful reading order.
    sql += " ORDER BY (status = 'active') DESC, followers_count DESC NULLS LAST, username"
    async with get_conn(autocommit=True) as conn:
        async with conn.cursor() as cur:
            await cur.execute(sql, params)
            return [dict(r) for r in await cur.fetchall()]


async def get_competitor(competitor_id: int) -> Optional[dict]:
    await ensure_tables()
    async with get_conn(autocommit=True) as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                f"SELECT {COMPETITOR_COLS} FROM trend_competitors WHERE id = %s",
                (competitor_id,),
            )
            row = await cur.fetchone()
            return dict(row) if row else None


async def get_competitor_by_username(username: str) -> Optional[dict]:
    await ensure_tables()
    async with get_conn(autocommit=True) as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                f"SELECT {COMPETITOR_COLS} FROM trend_competitors WHERE username = %s",
                (normalise_username(username),),
            )
            row = await cur.fetchone()
            return dict(row) if row else None


async def upsert_competitor(
    *, username: str, display_name: Optional[str] = None,
    biography: Optional[str] = None, profile_picture_url: Optional[str] = None,
    followers_count: Optional[int] = None, media_count: Optional[int] = None,
    category: Optional[str] = None, region: str = "uae",
    why: Optional[str] = None, status: str = "active", source: str = "manual",
    metadata: Optional[dict] = None,
) -> dict:
    """Insert, or refresh the profile fields of an existing row.

    Deliberately does NOT overwrite `status` on conflict: re-adding a handle the
    user excluded must not silently un-exclude it."""
    await ensure_tables()
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                f"""
                INSERT INTO trend_competitors
                    (username, display_name, biography, profile_picture_url,
                     followers_count, media_count, category, region, why,
                     status, source, metadata)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (username) DO UPDATE SET
                    display_name        = COALESCE(EXCLUDED.display_name, trend_competitors.display_name),
                    biography           = COALESCE(EXCLUDED.biography, trend_competitors.biography),
                    profile_picture_url = COALESCE(EXCLUDED.profile_picture_url, trend_competitors.profile_picture_url),
                    followers_count     = COALESCE(EXCLUDED.followers_count, trend_competitors.followers_count),
                    media_count         = COALESCE(EXCLUDED.media_count, trend_competitors.media_count),
                    category            = COALESCE(EXCLUDED.category, trend_competitors.category),
                    why                 = COALESCE(EXCLUDED.why, trend_competitors.why),
                    updated_at          = now()
                RETURNING {COMPETITOR_COLS}
                """,
                (normalise_username(username), display_name, biography,
                 profile_picture_url, followers_count, media_count, category,
                 region if region in REGIONS else "uae", why,
                 status if status in COMPETITOR_STATUSES else "active",
                 source, Jsonb(metadata or {})),
            )
            return dict(await cur.fetchone())


# Columns a caller may set to NULL, because empty is a real answer for them:
# an account with no category has no category. `status` is not one of them, and
# neither is `display_name`, which the scan owns.
NULLABLE_COMPETITOR_COLS = {"category", "region", "why"}


async def update_competitor(competitor_id: int, **fields: Any) -> Optional[dict]:
    """Patch a competitor. Only whitelisted columns are settable.

    A NULL CLEARS A NULLABLE COLUMN. This used to drop every `None` on the
    floor, which was invisible while the only way to set these fields was the
    add form: you cannot clear a box you are filling in for the first time. The
    moment an Edit form existed, clearing Category and pressing Save reported
    "Updated @handle" and changed nothing, which is the worst shape a bug can
    take. The caller decides what was sent: the route passes only the fields
    actually present in the request body.
    """
    await ensure_tables()
    allowed = {"status", "category", "region", "why", "display_name"}
    sets, params = [], []
    for key, value in fields.items():
        if key not in allowed:
            continue
        if value is None and key not in NULLABLE_COMPETITOR_COLS:
            continue
        sets.append(f"{key} = %s")
        params.append(value)
    if not sets:
        return await get_competitor(competitor_id)
    params.append(competitor_id)
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                f"UPDATE trend_competitors SET {', '.join(sets)}, updated_at = now() "
                f"WHERE id = %s RETURNING {COMPETITOR_COLS}",
                params,
            )
            row = await cur.fetchone()
            return dict(row) if row else None


async def delete_competitor(competitor_id: int) -> bool:
    """Hard delete. Posts cascade — this is the 'I never want to see this' path."""
    await ensure_tables()
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "DELETE FROM trend_competitors WHERE id = %s", (competitor_id,)
            )
            return cur.rowcount > 0


async def excluded_usernames() -> set[str]:
    """Handles AI research must never suggest again."""
    await ensure_tables()
    async with get_conn(autocommit=True) as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT username FROM trend_competitors WHERE status = 'excluded'"
            )
            return {r["username"] for r in await cur.fetchall()}


async def record_scan_result(
    competitor_id: int, *, status: str, error: Optional[str] = None,
    posts_found: Optional[int] = None, median_engagement: Optional[float] = None,
    followers_count: Optional[int] = None, media_count: Optional[int] = None,
    profile_picture_url: Optional[str] = None,
) -> None:
    """Record the outcome of scanning one competitor.

    `posts_found=None` LEAVES the previous count alone — a scan that failed
    (rate limit, a handle that went private) must not make the account look
    like it has no content when we simply couldn't read it this time.

    `profile_picture_url` is refreshed on every scan that read one: Instagram's
    picture links expire about four days after they are handed out, and the
    link saved when the account was added was the only one ever stored, so
    every avatar on the Competitors tab had become a 403."""
    await ensure_tables()
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                UPDATE trend_competitors
                   SET last_scanned_at  = now(),
                       last_scan_status = %s,
                       last_scan_error  = %s,
                       posts_found      = COALESCE(%s, posts_found),
                       median_engagement = COALESCE(%s, median_engagement),
                       followers_count   = COALESCE(%s, followers_count),
                       media_count       = COALESCE(%s, media_count),
                       profile_picture_url = COALESCE(%s, profile_picture_url),
                       updated_at        = now()
                 WHERE id = %s
                """,
                (status, error, posts_found, median_engagement,
                 followers_count, media_count, profile_picture_url, competitor_id),
            )


# ---------------------------------------------------------------------------
# Posts
# ---------------------------------------------------------------------------

async def upsert_post(competitor_id: Optional[int], post: dict) -> int:
    """Insert or refresh one discovered trend row, returning its row id.

    A re-scan must update the METRICS without destroying the AI scoring or the
    user's own `status` (saved / dismissed) — those are deliberately excluded
    from the update list.

    `competitor_id` is None for AI News (a story isn't tied to a watched
    account). `ig_media_id` stays the unique external key for every source — news
    rows namespace theirs as `news:<sha1(url)>` — so all of them get the same
    idempotent re-fetch behaviour from one code path."""
    await ensure_tables()
    source = post.get("source") or "competitor"
    if source not in POST_SOURCES:
        source = "competitor"
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                INSERT INTO trend_posts
                    (competitor_id, ig_media_id, username, permalink, media_type,
                     media_product_type, media_url, thumbnail_url, caption,
                     posted_at, like_count, comments_count, view_count,
                     engagement, engagement_rate, outperformance,
                     source, region, language, platform, metadata)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                        %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (ig_media_id) DO UPDATE SET
                    like_count      = EXCLUDED.like_count,
                    comments_count  = EXCLUDED.comments_count,
                    view_count      = EXCLUDED.view_count,
                    engagement      = EXCLUDED.engagement,
                    engagement_rate = EXCLUDED.engagement_rate,
                    outperformance  = EXCLUDED.outperformance,
                    caption         = EXCLUDED.caption,
                    permalink       = EXCLUDED.permalink,
                    media_url       = EXCLUDED.media_url,
                    thumbnail_url   = EXCLUDED.thumbnail_url,
                    region          = COALESCE(EXCLUDED.region, trend_posts.region),
                    language        = COALESCE(EXCLUDED.language, trend_posts.language),
                    platform        = COALESCE(EXCLUDED.platform, trend_posts.platform),
                    metadata        = EXCLUDED.metadata,
                    updated_at      = now()
                RETURNING id
                """,
                (competitor_id, post["ig_media_id"], post.get("username"),
                 post.get("permalink"), post.get("media_type"),
                 post.get("media_product_type"), post.get("media_url"),
                 post.get("thumbnail_url"), post.get("caption"),
                 post.get("posted_at"), post.get("like_count"),
                 post.get("comments_count"), post.get("view_count"),
                 post.get("engagement"), post.get("engagement_rate"),
                 post.get("outperformance"), source, post.get("region"),
                 post.get("language"), post.get("platform"),
                 Jsonb(post.get("metadata") or {})),
            )
            return int((await cur.fetchone())["id"])


async def local_thumbs_for(ig_media_ids: list[str]) -> dict[str, str]:
    """{ig_media_id: local_thumb_path} for the posts among these that have one.

    A scan asks before downloading: every scan hands back newly signed CDN
    links, and the cached file is named after the link, so without asking each
    scan stored every picture again under a new name."""
    if not ig_media_ids:
        return {}
    await ensure_tables()
    async with get_conn(autocommit=True) as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT ig_media_id, local_thumb_path FROM trend_posts "
                "WHERE ig_media_id = ANY(%s) AND COALESCE(local_thumb_path, '') <> ''",
                (list(ig_media_ids),),
            )
            return {str(r["ig_media_id"]): str(r["local_thumb_path"])
                    for r in await cur.fetchall()}


async def set_local_thumb(post_id: int, rel_path: str) -> None:
    await ensure_tables()
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "UPDATE trend_posts SET local_thumb_path = %s, updated_at = now() "
                "WHERE id = %s",
                (rel_path, post_id),
            )


async def unscored_posts(limit: int = 200, *, source: Optional[str] = None) -> list[dict]:
    """Posts waiting on the AI pass — newest and best-performing first, so a
    partial scoring run still covers what matters.

    `source` narrows the pass to one channel. The news collector uses it so an
    unattended tick cannot start scoring a competitor scan's backlog while that
    scan is still running — the two pollers would otherwise both call
    `score_pending` and pay for the same batch twice."""
    await ensure_tables()
    where = ["scored_at IS NULL"]
    params: list[Any] = []
    if source:
        where.append("source = %s")
        params.append(source)
    params.append(int(limit))
    async with get_conn(autocommit=True) as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                f"SELECT {POST_COLS} FROM trend_posts WHERE {' AND '.join(where)} "
                f"ORDER BY outperformance DESC NULLS LAST, posted_at DESC LIMIT %s",
                params,
            )
            return [dict(r) for r in await cur.fetchall()]


# ---------------------------------------------------------------------------
# AI News — the few operations the collector needs that competitors don't
# ---------------------------------------------------------------------------

async def recent_news_rows(hours: int = 48) -> list[dict]:
    """News rows inside the clustering window, oldest first.

    Oldest first matters: the FIRST member of a cluster is the one that stays
    visible, so iterating in arrival order makes clustering deterministic rather
    than dependent on which row the planner happened to return first."""
    await ensure_tables()
    async with get_conn(autocommit=True) as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT id, ig_media_id, caption, platform, posted_at, metadata,
                       thumbnail_url, local_thumb_path
                  FROM trend_posts
                 WHERE source = 'news'
                   AND COALESCE(posted_at, created_at) > now() - (%s || ' hours')::interval
                 ORDER BY COALESCE(posted_at, created_at)
                """,
                (str(int(hours)),),
            )
            return [dict(r) for r in await cur.fetchall()]


async def news_without_thumb(limit: int = 12) -> list[dict]:
    """Stories with no cached cover image yet, newest first.

    `caption` rides along because it is the plain-English headline, which is
    what a drawn cover is drawn from when the publisher has no picture to give.
    `metadata` rides along for `company_site`: a story about one company gets
    that company's logo before anybody asks the publisher for a picture.

    Newest first because that is the order the feed opens in: the cards a person
    is about to look at get their picture before the ones three pages down.

    A row whose file has been deleted off disk is NOT returned. The column still
    names a path, and re-fetching from the publisher would not put that file
    back where the path says it is. The route answers 410 for that case, which
    is a different problem with a different fix (re-scan) and must not be
    silently papered over here.
    """
    await ensure_tables()
    async with get_conn(autocommit=True) as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT id, permalink, caption, metadata
                  FROM trend_posts
                 WHERE source = 'news'
                   AND local_thumb_path IS NULL
                   AND permalink IS NOT NULL
                   AND permalink <> ''
                 ORDER BY COALESCE(posted_at, created_at) DESC
                 LIMIT %s
                """,
                (int(limit),),
            )
            return [dict(r) for r in await cur.fetchall()]


async def news_without_virality(limit: int = 20) -> list[dict]:
    """Stories the judge has not scored for viral potential, newest first.

    Every story found since the score existed carries one from ingest; these
    are the ones from before, and the housekeeping tick scores them a batch at
    a time so the feed's order means the same thing for every card on it.
    """
    await ensure_tables()
    async with get_conn(autocommit=True) as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT id, caption, metadata
                  FROM trend_posts
                 WHERE source = 'news'
                   AND status <> 'dismissed'
                   AND COALESCE(caption, '') <> ''
                   AND (metadata->>'viral_potential') IS NULL
                 ORDER BY COALESCE(posted_at, created_at) DESC
                 LIMIT %s
                """,
                (int(limit),),
            )
            return [dict(r) for r in await cur.fetchall()]


async def set_virality(post_id: int, score: int, reason: str) -> None:
    """Record the judge's viral potential on a story that predates the score."""
    await ensure_tables()
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                UPDATE trend_posts
                   SET metadata = COALESCE(metadata, '{}'::jsonb)
                                  || jsonb_build_object(
                                         'viral_potential', %s::int,
                                         'viral_reason', %s::text),
                       updated_at = now()
                 WHERE id = %s AND source = 'news'
                """,
                (int(score), str(reason or "")[:300], int(post_id)),
            )


async def news_house_covers_in_use(asset_type: str) -> list[str]:
    """Every house cover currently showing on a news card.

    Used to stop a later batch of stories re-picking a picture an earlier batch
    is already wearing. Matched on the asset type in the path, which is the only
    durable record of which covers are ours and which are the publisher's.
    """
    await ensure_tables()
    async with get_conn(autocommit=True) as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT DISTINCT local_thumb_path FROM trend_posts "
                " WHERE source = 'news' AND local_thumb_path LIKE %s",
                (f"%{asset_type}%",),
            )
            return [r["local_thumb_path"] for r in await cur.fetchall()
                    if r.get("local_thumb_path")]


async def recent_competitor_thumbs(limit: int = 300) -> list[dict]:
    """The newest competitor posts with everything a picture can come from.

    Returned whether or not they already have a cached thumbnail: the caller
    checks the file on disk, because a path in the column is not a picture if
    the file behind it is gone, and only the filesystem can answer that.
    """
    await ensure_tables()
    async with get_conn(autocommit=True) as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT id, permalink, caption, thumbnail_url, media_url, local_thumb_path
                  FROM trend_posts
                 WHERE COALESCE(source, '') <> 'news'
                 ORDER BY COALESCE(posted_at, created_at) DESC
                 LIMIT %s
                """,
                (int(limit),),
            )
            return [dict(r) for r in await cur.fetchall()]


async def existing_news_keys(keys: list[str]) -> set[str]:
    """Which of these `ig_media_id`s we already hold.

    The unique index makes a duplicate INSERT impossible either way, but knowing
    up front is what lets ingest report an honest "new" count. Checking only the
    48h clustering window is not enough: feeds routinely carry items months old,
    and every re-poll would then report the whole archive as newly arrived."""
    if not keys:
        return set()
    await ensure_tables()
    async with get_conn(autocommit=True) as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT ig_media_id FROM trend_posts WHERE ig_media_id = ANY(%s)",
                (list(keys),),
            )
            return {str(r["ig_media_id"]) for r in await cur.fetchall()}


async def bump_news_coverage(post_id: int, outlet: Optional[str]) -> None:
    """Record that one more outlet ran a story we already have.

    Coverage is the news analogue of engagement — the only popularity signal
    this source gives us — so a duplicate is information, not waste. The outlet
    list is deduplicated and capped in SQL so a syndicated wire story cannot
    grow the JSON without bound.
    """
    await ensure_tables()
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                UPDATE trend_posts
                   SET metadata = jsonb_set(
                           jsonb_set(
                               COALESCE(metadata, '{}'::jsonb),
                               '{coverage}',
                               to_jsonb(COALESCE((metadata->>'coverage')::int, 1) + 1)
                           ),
                           '{also_covered_by}',
                           COALESCE((
                               SELECT jsonb_agg(DISTINCT v)
                                 FROM jsonb_array_elements_text(
                                     COALESCE(metadata->'also_covered_by', '[]'::jsonb)
                                     || to_jsonb(ARRAY[%(outlet)s]::text[])
                                 ) AS v
                                WHERE v <> ''
                           ), '[]'::jsonb)
                       ),
                       updated_at = now()
                 WHERE id = %(id)s AND source = 'news'
                """,
                {"id": int(post_id), "outlet": (outlet or "")},
            )


async def set_outperformance(post_id: int, value: float) -> None:
    """Update just the ranking ratio, leaving the AI scores alone.

    News re-derives outperformance from cluster coverage after every tick, and
    that must not look like a re-score — `scored_at` and `fit_score` are the
    model's, and a coverage bump is not new evidence about fit."""
    await ensure_tables()
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "UPDATE trend_posts SET outperformance = %s, updated_at = now() "
                "WHERE id = %s",
                (float(value), int(post_id)),
            )


async def delete_old_news(days: int = 30) -> int:
    """Prune stale news.

    Only `status = 'new'` is deleted. A `dismissed` row is precisely what stops
    a story re-appearing on the next fetch, and `saved`/`used` rows may be
    mid-production — deleting either would be a visible bug rather than tidying.
    """
    await ensure_tables()
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                DELETE FROM trend_posts
                 WHERE source = 'news' AND status = 'new'
                   AND COALESCE(posted_at, created_at) < now() - (%s || ' days')::interval
                """,
                (str(int(days)),),
            )
            return cur.rowcount


async def recent_news_headlines(limit: int = 40) -> list[str]:
    """The plain headlines already in the feed, newest first.

    Handed to the search so it can skip what we have, and to the judge so it can
    mark a second write-up of the same event as a duplicate. Deliberately the
    REWRITTEN caption rather than the publisher's original: the judge is
    comparing against what a reader would see on the card, and two outlets'
    headlines about one event look far less alike than our two plain versions of
    it would.
    """
    await ensure_tables()
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT caption FROM trend_posts
                 WHERE source = 'news' AND caption <> ''
                 ORDER BY COALESCE(posted_at, created_at) DESC
                 LIMIT %s
                """,
                (int(limit),),
            )
            return [str(r["caption"]) for r in await cur.fetchall() if r["caption"]]


async def save_score(post_id: int, score: dict) -> None:
    await ensure_tables()
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                UPDATE trend_posts
                   SET topic = %s, topic_summary = %s, theme = %s,
                       fit_score = %s, fit_reason = %s, uae_relevance = %s,
                       suggested_angle = %s, suggested_format = %s, effort = %s,
                       tags = %s, opportunity_score = %s,
                       scored_at = now(), updated_at = now()
                 WHERE id = %s
                """,
                (score.get("topic"), score.get("topic_summary"), score.get("theme"),
                 score.get("fit_score"), score.get("fit_reason"),
                 score.get("uae_relevance"), score.get("suggested_angle"),
                 score.get("suggested_format"), score.get("effort"),
                 Jsonb(score.get("tags") or []), score.get("opportunity_score"),
                 post_id),
            )


async def update_post_status(post_id: int, status: str) -> Optional[dict]:
    await ensure_tables()
    if status not in POST_STATUSES:
        return None
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                f"UPDATE trend_posts SET status = %s, updated_at = now() "
                f"WHERE id = %s RETURNING {POST_COLS}",
                (status, post_id),
            )
            row = await cur.fetchone()
            return dict(row) if row else None


async def query_posts(
    *, status: Optional[str] = None, region: Optional[str] = None,
    media_type: Optional[str] = None, theme: Optional[str] = None,
    min_fit: Optional[int] = None, competitor_id: Optional[int] = None,
    search: Optional[str] = None, source: Optional[str] = None,
    platform: Optional[str] = None, language: Optional[str] = None,
    cap: int = 1000,
) -> list[dict]:
    """Filtered trend rows, joined to their competitor for region + display fields.

    The join is a LEFT join and the excluded-competitor test is NULL-safe on
    purpose: General Trends rows have no competitor at all, and an inner join
    (or a bare `c.status <> 'excluded'`) would silently drop every one of them.

    Sorting and the opportunity blend happen in Python (see
    `trend_scoring.rank_posts`) so the weight sliders can re-rank instantly
    without a re-scan — `cap` bounds how much is pulled in for that."""
    await ensure_tables()
    where = ["(c.id IS NULL OR c.status <> 'excluded')"]
    params: list[Any] = []
    if status:
        where.append("p.status = %s")
        params.append(status)
    else:
        # The default feed hides what the user already threw away.
        where.append("p.status <> 'dismissed'")
    if region:
        # The post's own region wins; competitor rows fall back to the account's.
        where.append("COALESCE(p.region, c.region) = %s")
        params.append(region)
    if media_type:
        where.append("p.media_type = %s")
        params.append(media_type)
    if theme:
        where.append("p.theme = %s")
        params.append(theme)
    if min_fit is not None:
        where.append("COALESCE(p.fit_score, 0) >= %s")
        params.append(int(min_fit))
    if competitor_id is not None:
        where.append("p.competitor_id = %s")
        params.append(int(competitor_id))
    if source:
        where.append("p.source = %s")
        params.append(source)
    if platform:
        where.append("p.platform = %s")
        params.append(platform)
    if language:
        where.append("p.language = %s")
        params.append(language)
    if search:
        where.append("(p.caption ILIKE %s OR p.topic ILIKE %s OR p.username ILIKE %s)")
        like = f"%{search.strip()}%"
        params += [like, like, like]
    params.append(int(cap))

    sql = f"""
        SELECT {', '.join('p.' + c.strip() for c in POST_COLS.split(','))},
               COALESCE(p.region, c.region) AS competitor_region,
               c.display_name  AS competitor_name,
               c.profile_picture_url AS competitor_avatar,
               c.followers_count     AS competitor_followers
          FROM trend_posts p
          LEFT JOIN trend_competitors c ON c.id = p.competitor_id
         WHERE {' AND '.join(where)}
         ORDER BY p.outperformance DESC NULLS LAST
         LIMIT %s
    """
    async with get_conn(autocommit=True) as conn:
        async with conn.cursor() as cur:
            await cur.execute(sql, params)
            return [dict(r) for r in await cur.fetchall()]


async def theme_rollup() -> list[dict]:
    """Scored posts grouped into themes — the 'topic finder' view."""
    await ensure_tables()
    async with get_conn(autocommit=True) as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT p.theme,
                       COUNT(*)                        AS post_count,
                       ROUND(AVG(p.fit_score))         AS avg_fit,
                       ROUND(AVG(p.uae_relevance))     AS avg_uae,
                       ROUND(AVG(p.outperformance)::numeric, 2) AS avg_outperformance,
                       MAX(p.opportunity_score)        AS best_score
                  FROM trend_posts p
                  LEFT JOIN trend_competitors c ON c.id = p.competitor_id
                 WHERE p.theme IS NOT NULL
                   AND p.status <> 'dismissed'
                   AND (c.id IS NULL OR c.status <> 'excluded')
                 GROUP BY p.theme
                 ORDER BY AVG(p.fit_score) DESC NULLS LAST, COUNT(*) DESC
                """
            )
            return [dict(r) for r in await cur.fetchall()]


async def stats() -> dict:
    """Header numbers for the Trends page."""
    await ensure_tables()
    async with get_conn(autocommit=True) as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT
                  (SELECT COUNT(*) FROM trend_competitors WHERE status = 'active')   AS competitors_active,
                  (SELECT COUNT(*) FROM trend_competitors WHERE status = 'excluded') AS competitors_excluded,
                  (SELECT COUNT(*) FROM trend_posts)                                  AS posts_total,
                  (SELECT COUNT(*) FROM trend_posts WHERE scored_at IS NULL)          AS posts_unscored,
                  (SELECT COUNT(*) FROM trend_posts WHERE status = 'saved')           AS posts_saved,
                  (SELECT COUNT(*) FROM trend_posts WHERE source = 'competitor')      AS posts_competitor,
                  (SELECT COUNT(*) FROM trend_posts WHERE source = 'news')            AS posts_news,
                  (SELECT ROUND(AVG(fit_score)) FROM trend_posts WHERE fit_score IS NOT NULL)
                                                                                      AS avg_fit,
                  (SELECT MAX(last_scanned_at) FROM trend_competitors)                AS last_scanned_at
                """
            )
            row = await cur.fetchone()
            return dict(row) if row else {}


async def get_post(post_id: int) -> Optional[dict]:
    await ensure_tables()
    async with get_conn(autocommit=True) as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                f"SELECT {POST_COLS} FROM trend_posts WHERE id = %s", (post_id,)
            )
            row = await cur.fetchone()
            return dict(row) if row else None


async def clear_scores(post_ids: Optional[list[int]] = None) -> int:
    """Mark posts unscored so the AI pass re-runs (e.g. after the brand changed)."""
    await ensure_tables()
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            if post_ids:
                await cur.execute(
                    "UPDATE trend_posts SET scored_at = NULL WHERE id = ANY(%s)",
                    (post_ids,),
                )
            else:
                await cur.execute("UPDATE trend_posts SET scored_at = NULL")
            return cur.rowcount


# ---------------------------------------------------------------------------
# Scans
# ---------------------------------------------------------------------------

async def create_scan(competitors_total: int, metadata: Optional[dict] = None) -> dict:
    await ensure_tables()
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                f"INSERT INTO trend_scans (status, phase, competitors_total, metadata) "
                f"VALUES ('queued', 'starting', %s, %s) RETURNING {SCAN_COLS}",
                (competitors_total, Jsonb(metadata or {})),
            )
            return dict(await cur.fetchone())


async def update_scan(scan_id: int, **fields: Any) -> None:
    await ensure_tables()
    allowed = {"status", "phase", "progress", "competitors_done", "posts_found",
               "posts_scored", "error", "competitors_total"}
    sets, params = [], []
    for key, value in fields.items():
        if key in allowed:
            sets.append(f"{key} = %s")
            params.append(value)
    if fields.get("status") in ("done", "failed"):
        sets.append("finished_at = now()")
    if not sets:
        return
    params.append(scan_id)
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                f"UPDATE trend_scans SET {', '.join(sets)} WHERE id = %s", params
            )


async def pending_scans(limit: int = 5) -> list[dict]:
    """Queued scans for the poller to pick up.

    The queue lives in the DB rather than in memory precisely so a scan survives
    a backend restart — an `asyncio.create_task` would be lost (and can even be
    garbage-collected mid-flight)."""
    await ensure_tables()
    async with get_conn(autocommit=True) as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                f"SELECT {SCAN_COLS} FROM trend_scans WHERE status = 'queued' "
                f"ORDER BY started_at LIMIT %s",
                (limit,),
            )
            return [dict(r) for r in await cur.fetchall()]


async def claim_scan(scan_id: int) -> bool:
    """Atomically move a queued scan to 'fetching'. False if someone beat us —
    two poller ticks must never run the same scan twice."""
    await ensure_tables()
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "UPDATE trend_scans SET status = 'fetching', phase = 'fetching' "
                "WHERE id = %s AND status = 'queued'",
                (scan_id,),
            )
            return cur.rowcount > 0


async def fail_stale_scans() -> int:
    """Close out scans orphaned by a restart, so the UI never polls forever."""
    await ensure_tables()
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                UPDATE trend_scans
                   SET status = 'failed', finished_at = now(),
                       error = COALESCE(error,
                           'the scan stopped unexpectedly (the server restarted) — run it again')
                 WHERE status NOT IN ('done', 'failed')
                   AND started_at < now() - (%s || ' seconds')::interval
                """,
                (str(SCAN_STALE_AFTER_SEC),),
            )
            return cur.rowcount


async def latest_scan(*, full_only: bool = False) -> Optional[dict]:
    """The newest scan. `full_only` skips scans of a chosen few accounts (their
    `competitor_ids` is a list), which is what the automatic scan's clock runs
    from: rescanning one account refreshed that account, not the feed."""
    await ensure_tables()
    where = (
        "WHERE jsonb_typeof(metadata->'competitor_ids') IS DISTINCT FROM 'array' "
        if full_only else ""
    )
    async with get_conn(autocommit=True) as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                f"SELECT {SCAN_COLS}, "
                f"EXTRACT(EPOCH FROM (now() - started_at)) AS age_sec "
                f"FROM trend_scans {where}ORDER BY started_at DESC LIMIT 1"
            )
            row = await cur.fetchone()
            if not row:
                return None
            scan = dict(row)
            age = float(scan.pop("age_sec", 0) or 0)
            # A non-terminal scan older than the stale window is orphaned (the
            # backend restarted mid-run). Report it as failed so the UI's poll
            # loop always terminates instead of spinning forever.
            running = scan["status"] not in ("done", "failed")
            if running and age > SCAN_STALE_AFTER_SEC:
                scan["status"] = "failed"
                scan["error"] = scan.get("error") or (
                    "the scan stopped unexpectedly (the server restarted) — run it again"
                )
            scan["active"] = scan["status"] not in ("done", "failed")
            return scan


async def scan_is_active() -> bool:
    scan = await latest_scan()
    return bool(scan and scan.get("active"))


if __name__ == "__main__":  # pragma: no cover — manual table creation
    import asyncio

    from ..db import close_pool, open_pool

    async def _main() -> None:
        await open_pool()
        try:
            await ensure_tables()
            print("trend finder tables ready")
        finally:
            await close_pool()

    asyncio.run(_main())
