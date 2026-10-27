"""
Background worker — runs queued Trend Finder scans, and queues one itself every
``TREND_AUTO_SCAN_HOURS`` hours.

Started in main.lifespan; cancelled on shutdown. Survives restarts: the queue is
the ``trend_scans`` table, so a scan queued (or interrupted) while the backend
was down is picked up on the next tick rather than vanishing — which is exactly
why the API queues a row instead of spawning a task.

A scan reads several Instagram accounts and then scores the new posts with the
AI, so it runs for minutes. It must never block a request.

WHY IT QUEUES ITS OWN. A scan used to run only when someone pressed Scan, so the
feed froze on the last press while every account kept posting: nothing was read
from 2026-09-18 to 2026-10-07. Now, once the last scan of EVERY account is
older than the interval, the poller queues one (`trigger: "auto"`), with the
same post cap the Scan button starts at. Scanning one account does not reset
that clock, because it refreshed that account and not the feed. The rule is
General Trends' `auto_refresh_due`, so the two feeds age the same way.

This queue carries competitor scans only. AI News is collected by
``workers/news_poller.py`` against its own table, deliberately NOT this queue:
serialising an unattended collector behind an interactive scan would mean news
silently stops arriving whenever someone is scanning competitors.
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone

from ..config import config
from ..services import news_thumbs, trend_finder
from ..services import trend_store as store
from .general_trends_poller import auto_refresh_due

log = logging.getLogger("archivist.trend_scan_poller")

POLL_INTERVAL_SEC = 15
# How often the automatic scan is considered. Its answer changes once a day,
# and asking on every 15-second tick would put the same three queries to the
# database four times a minute, forever, on an install with no competitors.
AUTO_SCAN_CHECK_SEC = 300
# Posts read per account by the automatic scan: what the Scan button starts at
# (DEFAULT_POST_CAP in CompetitorsTab.tsx), so an automatic scan is the scan a
# person would have run.
AUTO_SCAN_MAX_POSTS = 50

_task: asyncio.Task | None = None
# Only ever run one scan at a time in this process: they share the Instagram
# rate limit and the small DB pool.
_running = False
# time.monotonic() before which the automatic scan is not considered again.
_next_auto_check = 0.0


async def _queue_auto_scan() -> None:
    """Queue a scan of every active account if the last one is old enough.
    Never raises: a failed check must not stop the queue below it."""
    global _next_auto_check
    hours = int(getattr(config, "trend_auto_scan_hours", 0) or 0)
    now = time.monotonic()
    if hours <= 0 or now < _next_auto_check:
        return
    _next_auto_check = now + AUTO_SCAN_CHECK_SEC
    try:
        if await store.scan_is_active():
            return
        latest = await store.latest_scan(full_only=True)
        if not auto_refresh_due(latest, now=datetime.now(timezone.utc), every_hours=hours):
            return
        await trend_finder.start_scan(None, max_posts=AUTO_SCAN_MAX_POSTS, trigger="auto")
        log.info("[trend-finder] auto scan queued (the last full scan is over %dh old)", hours)
    except trend_finder.TrendScanError as exc:
        # No active competitors, or someone pressed Scan in between. Nothing
        # to collect until the next check.
        log.debug("[trend-finder] auto scan not queued: %s", exc)
    except Exception:
        log.exception("[trend-finder] auto scan check failed")


async def _poll_once() -> None:
    global _running
    if _running:
        return
    try:
        # Close out anything a previous process left mid-flight, so the UI's
        # poll loop always terminates.
        await store.fail_stale_scans()
        # Before the queue read, so an automatic scan starts on this tick.
        await _queue_auto_scan()
        queued = await store.pending_scans(limit=1)
    except Exception:
        log.exception("[trend-finder] poller: queue query failed")
        return

    for row in queued:
        scan_id = int(row["id"])
        try:
            # Atomic claim — two ticks must never run the same scan twice.
            if not await store.claim_scan(scan_id):
                continue
            _running = True
            meta = row.get("metadata") or {}
            log.info("[trend-finder] running scan #%d", scan_id)
            await trend_finder.run_scan(
                scan_id,
                meta.get("competitor_ids"),
                lookback_days=int(meta.get("lookback_days")
                                  or trend_finder.DEFAULT_LOOKBACK_DAYS),
                max_posts=int(meta.get("max_posts")
                              or trend_finder.DEFAULT_MAX_POSTS_PER_ACCOUNT),
            )
            # Pictures for the posts this scan could not download, now rather
            # than on the news poller's next tick up to five minutes away.
            try:
                await news_thumbs.backfill_competitors()
            except Exception:  # noqa: BLE001 - a missing picture is not a failed scan
                log.exception("[trend-finder] competitor covers failed after scan #%d",
                              scan_id)
        except Exception:
            log.exception("[trend-finder] poller: scan #%d failed", scan_id)
            try:
                await store.update_scan(
                    scan_id, status="failed", phase="failed",
                    error="the scan failed unexpectedly", progress="the scan failed",
                )
            except Exception:
                log.exception("[trend-finder] poller: could not mark scan #%d failed",
                              scan_id)
        finally:
            _running = False


async def _loop() -> None:
    log.info("trend_scan_poller started (every %ds)", POLL_INTERVAL_SEC)
    try:
        while True:
            await _poll_once()
            await asyncio.sleep(POLL_INTERVAL_SEC)
    except asyncio.CancelledError:
        log.info("trend_scan_poller cancelled")
        raise
    except Exception:
        log.exception("trend_scan_poller loop crashed")


def start() -> asyncio.Task:
    global _task
    if _task is not None and not _task.done():
        return _task
    _task = asyncio.create_task(_loop(), name="trend_scan_poller")
    return _task


async def stop() -> None:
    global _task
    if _task is None:
        return
    _task.cancel()
    try:
        await _task
    except (asyncio.CancelledError, Exception):
        pass
    _task = None
