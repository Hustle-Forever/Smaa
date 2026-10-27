"""Phase 1 — discover.

Walks the target account's media via Meta's Business Discovery API and
UPSERTs one row per post into `instagram_posts`. Carousel children are
recorded structurally on the parent row (children_count + raw children dump
in metadata) — actual child assets are downloaded in the next phase.

Idempotent: re-running picks up new posts since the last sync without
disturbing existing rows. Engagement counts (likes/comments/views) are
updated in place since they drift; everything else is set on first insert
only via ON CONFLICT DO UPDATE with COALESCE.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from psycopg.types.json import Jsonb

from ....db import get_conn
from ....services.instagram_client import fetch_account_media
from ...website_ingest.types import CancellationError, PhaseContext, PhaseResult

log = logging.getLogger("archivist.phase.ig_discover")


def _parse_ts(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        # IG returns ISO8601 with offset (e.g. "2024-06-12T13:45:00+0000")
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        try:
            # Tolerate ±HHMM (no colon) by inserting one
            if len(s) > 5 and (s[-5] in "+-") and s[-3] != ":":
                s = s[:-2] + ":" + s[-2:]
            return datetime.fromisoformat(s)
        except ValueError:
            log.warning("Could not parse IG timestamp %r", s)
            return None


async def _upsert_post(username: str, media: dict) -> tuple[str, str]:
    """UPSERT one post. Returns ('inserted'|'updated', ig_media_id)."""
    ig_id = str(media["id"])
    media_type = media.get("media_type") or "IMAGE"
    children = (media.get("children") or {}).get("data") or []
    children_count = len(children) if media_type == "CAROUSEL_ALBUM" else 0

    md = {
        "media_url": media.get("media_url"),
        "thumbnail_url": media.get("thumbnail_url"),
        "children_raw": children,  # keeps child ids + urls for the download phase
    }

    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                INSERT INTO instagram_posts
                  (ig_media_id, account_username, media_type, media_product_type,
                   caption, permalink, timestamp_posted,
                   like_count, comments_count, view_count, children_count,
                   metadata)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (ig_media_id) DO UPDATE SET
                  -- Refresh drifting fields
                  like_count       = EXCLUDED.like_count,
                  comments_count   = EXCLUDED.comments_count,
                  view_count       = EXCLUDED.view_count,
                  -- Preserve first-seen values for static fields (COALESCE keeps existing
                  -- when EXCLUDED is non-null but unchanged; for caption, prefer the
                  -- latest since users do edit captions occasionally)
                  caption          = EXCLUDED.caption,
                  permalink        = COALESCE(EXCLUDED.permalink, instagram_posts.permalink),
                  media_product_type = COALESCE(EXCLUDED.media_product_type, instagram_posts.media_product_type),
                  children_count   = GREATEST(instagram_posts.children_count, EXCLUDED.children_count),
                  metadata         = instagram_posts.metadata || EXCLUDED.metadata,
                  updated_at       = NOW()
                RETURNING (xmax = 0) AS inserted, id
                """,
                (
                    ig_id,
                    username,
                    media_type,
                    media.get("media_product_type"),
                    media.get("caption"),
                    media.get("permalink"),
                    _parse_ts(media.get("timestamp")),
                    media.get("like_count"),
                    media.get("comments_count"),
                    media.get("view_count"),
                    children_count,
                    Jsonb(md),
                ),
            )
            row = await cur.fetchone()
    inserted = bool(row and row.get("inserted"))
    return ("inserted" if inserted else "updated", ig_id)


async def run(ctx: PhaseContext) -> PhaseResult:
    ig_meta = (ctx.metadata or {}).get("instagram") or {}
    username = (ig_meta.get("target_username") or "").strip()
    if not username:
        log.warning("[run=%s] discover: no target_username — skipping", ctx.run_id)
        return PhaseResult(name="discover", total=0, notes="no username")

    max_posts: int | None = ig_meta.get("max_posts")
    page_size: int = max(1, min(int(ig_meta.get("page_size") or 100), 100))

    log.info(
        "[run=%s] discover @%s page_size=%d max_posts=%s",
        ctx.run_id, username, page_size, max_posts,
    )

    inserted = 0
    updated = 0
    after: str | None = None
    pages = 0
    seen_ids: set[str] = set()

    while True:
        if ctx.is_cancelled and ctx.is_cancelled():
            raise CancellationError("discover cancelled mid-batch")

        bd = await fetch_account_media(username, after=after, page_size=page_size)
        pages += 1
        media_block = bd.get("media") or {}
        items = media_block.get("data") or []
        if not items:
            log.info("[run=%s] discover: empty page after %d pages", ctx.run_id, pages)
            break

        for media in items:
            mid = str(media.get("id") or "")
            if not mid or mid in seen_ids:
                continue
            seen_ids.add(mid)
            try:
                outcome, _ = await _upsert_post(username, media)
                if outcome == "inserted":
                    inserted += 1
                else:
                    updated += 1
            except Exception:
                log.exception("upsert failed for ig_media_id=%s", mid)

            if max_posts and (inserted + updated) >= max_posts:
                break

        if ctx.report_progress:
            ctx.report_progress("discover", inserted + updated, max_posts or (inserted + updated))

        if max_posts and (inserted + updated) >= max_posts:
            log.info("[run=%s] discover: hit max_posts=%d", ctx.run_id, max_posts)
            break

        next_after = ((media_block.get("paging") or {}).get("cursors") or {}).get("after")
        if not next_after:
            log.info("[run=%s] discover: no more pages after %d", ctx.run_id, pages)
            break
        after = next_after

    log.info(
        "[run=%s] discover done: %d inserted, %d updated across %d pages",
        ctx.run_id, inserted, updated, pages,
    )
    return PhaseResult(
        name="discover",
        processed=inserted,
        skipped=updated,
        total=inserted + updated,
        extra={"pages": pages, "username": username, "account_followers":
               (bd.get("followers_count") if 'bd' in locals() else None)},
    )
