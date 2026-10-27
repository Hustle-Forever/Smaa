"""
Pending-queue store for the Producer Roadmap (Steps 4 Calendar + 5 Approval).

A scheduled Instagram post = a finalized asset (poster / animation / poster+human
/ talking-head) + an approved caption + a publish timestamp. Rows live in the
Supabase Postgres table ``scheduled_instagram_posts`` and move through:

    pending  → (reminder email sent at the scheduled time) → reminded
             → (user clicks Approve & Post)                → published | failed
             → (user cancels)                              → cancelled

The table is created idempotently (``CREATE TABLE IF NOT EXISTS``) the first time
the store is used, so no separate migration step is required. Run this module
directly to create it ahead of time:

    backend/.venv/bin/python -m app.services.scheduled_posts
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from psycopg.types.json import Jsonb

from ..db import get_conn
from . import asset_storage

log = logging.getLogger("archivist.scheduled_posts")

VALID_TOOLS = {
    "poster", "animation", "poster_human", "talking_head_v2", "video_editor",
    "general_video", "broll",
}
VALID_STATUS = {"pending", "reminded", "published", "failed", "cancelled"}

# Reminder cadence (tweak freely): a heads-up BEFORE the scheduled time, then a
# follow-up nudge if the post is still pending (the user forgot to approve & post).
REMINDER_LEAD_MINUTES = 60   # send the FIRST reminder this long before scheduled_at
REMINDER_GAP_MINUTES = 10    # minimum spacing between consecutive reminders
MAX_REMINDERS = 2            # total nudges before we stop (1 heads-up + 1 follow-up)

_DDL = """
CREATE TABLE IF NOT EXISTS scheduled_instagram_posts (
    id               BIGSERIAL PRIMARY KEY,
    tool             TEXT        NOT NULL,
    ref_id           BIGINT      NOT NULL,
    media_type       TEXT        NOT NULL DEFAULT 'image',
    media_url        TEXT,
    caption          TEXT        NOT NULL DEFAULT '',
    scheduled_at     TIMESTAMPTZ NOT NULL,
    timezone         TEXT        NOT NULL DEFAULT 'Asia/Dubai',
    status           TEXT        NOT NULL DEFAULT 'pending',
    reminder_sent_at TIMESTAMPTZ,
    reminder_count   INT         NOT NULL DEFAULT 0,
    published_at     TIMESTAMPTZ,
    ig_media_id      TEXT,
    permalink        TEXT,
    error            TEXT,
    metadata         JSONB       NOT NULL DEFAULT '{}'::jsonb,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_sched_ig_due ON scheduled_instagram_posts (status, scheduled_at);
-- Columns added after the first release; backfill them on existing tables.
ALTER TABLE scheduled_instagram_posts ADD COLUMN IF NOT EXISTS media_url TEXT;
ALTER TABLE scheduled_instagram_posts ADD COLUMN IF NOT EXISTS reminder_count INT NOT NULL DEFAULT 0;
"""

_RETURN_COLS = (
    "id, tool, ref_id, media_type, media_url, caption, scheduled_at, timezone, status, "
    "reminder_sent_at, reminder_count, published_at, ig_media_id, permalink, error, metadata, "
    "created_at, updated_at"
)

_ensured = False


async def ensure_table() -> None:
    """Create the table + index if missing (idempotent). Runs once per process."""
    global _ensured
    if _ensured:
        return
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(_DDL)
    _ensured = True
    log.info("scheduled_instagram_posts table ensured")


async def create(
    *,
    tool: str,
    ref_id: int,
    media_type: str,
    caption: str,
    scheduled_at: str,
    timezone: str,
    media_url: str | None = None,
    metadata: dict | None = None,
) -> dict:
    """Insert a pending scheduled post. `scheduled_at` is an ISO 8601 string
    (with offset); Postgres casts it to timestamptz. `media_url` is the public
    bucket URL Instagram will fetch at publish time (None if not yet hosted).
    `metadata` holds extras like carousel slides (is_carousel / carousel_items)."""
    await ensure_table()
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                f"""
                INSERT INTO scheduled_instagram_posts
                    (tool, ref_id, media_type, media_url, caption, scheduled_at, timezone, status, metadata)
                VALUES (%s, %s, %s, %s, %s, %s::timestamptz, %s, 'pending', %s)
                RETURNING {_RETURN_COLS}
                """,
                (tool, int(ref_id), media_type, media_url, caption, scheduled_at, timezone,
                 Jsonb(metadata or {})),
            )
            return await cur.fetchone()


async def set_media_url(post_id: int, media_url: str) -> Optional[dict]:
    """Persist the hosted public URL (used when hosting happens at approval time)."""
    await ensure_table()
    return await _update(post_id, media_url=media_url)


async def get(post_id: int) -> Optional[dict]:
    await ensure_table()
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                f"SELECT {_RETURN_COLS} FROM scheduled_instagram_posts WHERE id = %s",
                (int(post_id),),
            )
            return await cur.fetchone()


async def list_recent(limit: int = 100) -> list[dict]:
    """All scheduled posts, newest scheduled-time first — for the Library's
    Scheduled queue. Includes every status (the UI groups pending vs done)."""
    await ensure_table()
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                f"""
                SELECT {_RETURN_COLS} FROM scheduled_instagram_posts
                 ORDER BY scheduled_at DESC
                 LIMIT %s
                """,
                (int(limit),),
            )
            return await cur.fetchall() or []


async def cancel(post_id: int) -> Optional[dict]:
    """Cancel a scheduled post (won't be reminded or published). Idempotent-ish:
    callers should avoid cancelling an already-published row."""
    await ensure_table()
    return await _update(post_id, status="cancelled")


async def delete_by_ref(tool: str, ref_id: int) -> int:
    """Hard-delete every NON-published scheduled post that points at one finished
    asset ``(tool, ref_id)`` — used when that asset itself is deleted, so the
    Scheduled queue doesn't keep a dangling row whose thumbnail 404s and whose
    Approve & Post path dead-ends (the media it referenced is gone).

    ``published`` rows are deliberately KEPT: they are the audit trail of a real
    Instagram post (their media is hosted independently on the public bucket and
    they carry the ig_media_id / permalink), so they still render and still feed
    the Dashboard's published count. Returns the number of rows removed.

    The removed rows' hosted copies (media_url and every carousel slide) are
    deleted too: the public bucket is anonymous-readable, so an orphan would stay
    public for good.
    """
    await ensure_table()
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "DELETE FROM scheduled_instagram_posts "
                "WHERE tool = %s AND ref_id = %s AND status <> 'published' "
                "RETURNING media_url, metadata",
                (tool, int(ref_id)),
            )
            rows = await cur.fetchall() or []
    # Only once the DELETE has committed: rolled back, the rows would still
    # point at this media.
    await asset_storage.delete_public_urls(
        [u for r in rows for u in hosted_media_urls(r)]
    )
    return len(rows)


def hosted_media_urls(row: dict | None) -> list[str]:
    """Every public-bucket URL a scheduled post points at: its media_url plus
    each carousel slide's (slide 1 is usually the same URL)."""
    if not row:
        return []
    urls = [row.get("media_url")]
    for it in (row.get("metadata") or {}).get("carousel_items") or []:
        if isinstance(it, dict):
            urls.append(it.get("media_url"))
    return [u for u in urls if u]


async def status_counts() -> dict[str, int]:
    """How many scheduled posts sit in each status — feeds the Dashboard funnel.

    Returns e.g. {"pending": 3, "reminded": 1, "published": 5, "cancelled": 2}.
    Missing statuses are simply absent (callers default to 0)."""
    await ensure_table()
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT status, COUNT(*) AS n FROM scheduled_instagram_posts GROUP BY status"
            )
            rows = await cur.fetchall() or []
    return {str(r["status"]): int(r["n"]) for r in rows}


async def distinct_scheduled_count() -> int:
    """Distinct (tool, ref_id) pairs that have EVER been booked — across every
    status. Subtracting this from the count of finished generations yields the
    "made but not yet scheduled" backlog (Dashboard's Pending stat)."""
    await ensure_table()
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT COUNT(DISTINCT (tool, ref_id)) AS n FROM scheduled_instagram_posts"
            )
            row = await cur.fetchone()
    return int((row or {}).get("n") or 0)


async def published_media(limit: int = 500) -> list[dict]:
    """Our OWN posts that actually went live on Instagram, newest first — the
    source for the Dashboard's Instagram card (count + per-media like lookups)."""
    await ensure_table()
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT id, ref_id, tool, ig_media_id, permalink, published_at
                  FROM scheduled_instagram_posts
                 WHERE status = 'published' AND ig_media_id IS NOT NULL
                 ORDER BY published_at DESC NULLS LAST
                 LIMIT %s
                """,
                (int(limit),),
            )
            return await cur.fetchall() or []


async def due_for_reminder() -> list[dict]:
    """Pending rows that need a reminder right now — the poller's work queue.

    Two-stage cadence:
      • 1st reminder  — once we're within REMINDER_LEAD_MINUTES of the scheduled
                        time (i.e. ~1h before).
      • follow-up(s)  — the post is now due (>= scheduled time) and STILL pending,
                        spaced at least REMINDER_GAP_MINUTES apart, until we've
                        sent MAX_REMINDERS total.
    Status stays 'pending' throughout (so the user can still approve, and we can
    still nudge); reminder_count tracks how many we've sent.
    """
    await ensure_table()
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                f"""
                SELECT {_RETURN_COLS} FROM scheduled_instagram_posts
                 WHERE status = 'pending'
                   AND reminder_count < {MAX_REMINDERS}
                   AND COALESCE(metadata->>'suppress_reminders', '') <> 'true'
                   AND (
                        (reminder_count = 0
                         AND now() >= scheduled_at - interval '{REMINDER_LEAD_MINUTES} minutes')
                     OR (reminder_count >= 1
                         AND now() >= scheduled_at
                         AND now() >= reminder_sent_at + interval '{REMINDER_GAP_MINUTES} minutes')
                   )
                 ORDER BY scheduled_at
                 LIMIT 50
                """
            )
            return await cur.fetchall() or []


async def _update(post_id: int, **fields: Any) -> Optional[dict]:
    if not fields:
        return await get(post_id)
    sets = ", ".join(f"{k} = %s" for k in fields)
    vals = [Jsonb(v) if k == "metadata" else v for k, v in fields.items()]
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                f"""
                UPDATE scheduled_instagram_posts
                   SET {sets}, updated_at = now()
                 WHERE id = %s
                RETURNING {_RETURN_COLS}
                """,
                (*vals, int(post_id)),
            )
            return await cur.fetchone()


async def record_reminder(post_id: int) -> Optional[dict]:
    """Count one reminder as sent. Keeps status 'pending' so a follow-up can
    still fire and the user can still approve & post."""
    await ensure_table()
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                f"""
                UPDATE scheduled_instagram_posts
                   SET reminder_count = reminder_count + 1,
                       reminder_sent_at = now(), updated_at = now()
                 WHERE id = %s
                RETURNING {_RETURN_COLS}
                """,
                (int(post_id),),
            )
            return await cur.fetchone()


async def mark_published(post_id: int, *, ig_media_id: str | None, permalink: str | None) -> Optional[dict]:
    await ensure_table()
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                f"""
                UPDATE scheduled_instagram_posts
                   SET status = 'published', published_at = now(),
                       ig_media_id = %s, permalink = %s, error = NULL, updated_at = now()
                 WHERE id = %s
                RETURNING {_RETURN_COLS}
                """,
                (ig_media_id, permalink, int(post_id)),
            )
            return await cur.fetchone()


async def mark_failed(post_id: int, error: str) -> Optional[dict]:
    await ensure_table()
    return await _update(post_id, status="failed", error=(error or "")[:1000])


if __name__ == "__main__":
    import asyncio

    from ..db import close_pool, open_pool

    async def _main() -> None:
        await open_pool()
        try:
            await ensure_table()
            print("✓ scheduled_instagram_posts table is ready")
        finally:
            await close_pool()

    asyncio.run(_main())
