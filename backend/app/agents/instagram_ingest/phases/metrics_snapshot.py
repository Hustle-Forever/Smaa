"""Phase 6 — metrics_snapshot.

Refreshes public engagement counts on every known instagram_posts row by
re-querying Business Discovery with the lighter metrics-only field set,
then appends one historical row per post to instagram_metrics_snapshots
for trend tracking.

Cheap relative to the full sync — no media downloads, no embeddings, no
vision calls. Can be run independently via RunOptions(metrics_only=True).
"""
from __future__ import annotations

import logging

from ....db import get_conn
from ....services.instagram_client import refresh_media_metrics
from ...website_ingest.types import CancellationError, PhaseContext, PhaseResult

log = logging.getLogger("archivist.phase.ig_metrics")


async def _known_ig_ids(username: str) -> set[str]:
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT ig_media_id FROM instagram_posts WHERE account_username=%s",
                (username,),
            )
            rows = await cur.fetchall()
    return {r["ig_media_id"] for r in rows}


async def _apply_metrics(ig_id: str, metrics: dict) -> bool:
    """UPDATE counts + append snapshot. Returns True if a row was updated."""
    like = metrics.get("like_count")
    comm = metrics.get("comments_count")
    view = metrics.get("view_count")
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                UPDATE instagram_posts
                   SET like_count = COALESCE(%s, like_count),
                       comments_count = COALESCE(%s, comments_count),
                       view_count = COALESCE(%s, view_count),
                       last_metrics_at = NOW()
                 WHERE ig_media_id = %s
                RETURNING id
                """,
                (like, comm, view, ig_id),
            )
            row = await cur.fetchone()
            if not row:
                return False
            await cur.execute(
                """
                INSERT INTO instagram_metrics_snapshots
                  (post_id, like_count, comments_count, view_count)
                VALUES (%s, %s, %s, %s)
                """,
                (int(row["id"]), like, comm, view),
            )
    return True


async def run(ctx: PhaseContext) -> PhaseResult:
    ig_meta = (ctx.metadata or {}).get("instagram") or {}
    username = (ig_meta.get("target_username") or "").strip()
    if not username:
        return PhaseResult(name="metrics_snapshot", total=0, notes="no username")

    page_size = max(1, min(int(ig_meta.get("page_size") or 100), 100))

    known = await _known_ig_ids(username)
    if not known:
        log.info("[run=%s] metrics: no known posts for @%s yet", ctx.run_id, username)
        return PhaseResult(name="metrics_snapshot", total=0, notes="no posts yet")

    updated = 0
    missing = 0  # posts the API returned but we don't have stored yet
    pages = 0
    after: str | None = None

    while True:
        if ctx.is_cancelled and ctx.is_cancelled():
            raise CancellationError("ig_metrics cancelled mid-batch")

        bd = await refresh_media_metrics(username, after=after, page_size=page_size)
        pages += 1
        media_block = bd.get("media") or {}
        items = media_block.get("data") or []
        if not items:
            break

        for media in items:
            mid = str(media.get("id") or "")
            if not mid:
                continue
            if mid not in known:
                missing += 1
                continue
            try:
                if await _apply_metrics(mid, media):
                    updated += 1
            except Exception:
                log.exception("metrics apply failed for ig_media_id=%s", mid)

        if ctx.report_progress:
            ctx.report_progress("metrics_snapshot", updated, len(known))

        next_after = ((media_block.get("paging") or {}).get("cursors") or {}).get("after")
        if not next_after:
            break
        after = next_after

    log.info(
        "[run=%s] metrics done: %d updated (%d API-returned posts not in DB) across %d pages",
        ctx.run_id, updated, missing, pages,
    )
    return PhaseResult(
        name="metrics_snapshot",
        processed=updated,
        skipped=missing,
        total=len(known),
        extra={"pages": pages, "missing_posts": missing},
    )
