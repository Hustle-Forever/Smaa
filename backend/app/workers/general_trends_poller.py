"""
Background worker — runs queued General Trends refreshes, and queues one
itself every ``GENERAL_TRENDS_REFRESH_HOURS`` hours.

Same shape as ``trend_scan_poller``: the queue is the ``general_trend_runs``
table, so a refresh queued (or interrupted) while the backend was down is
picked up on the next tick rather than vanishing. On every tick:

  1. ``fail_stale_runs``   — close out anything a dead process left mid-flight
  2. auto-queue           — no active run, auto-refresh enabled, and the last
                            run finished longer ago than the interval (or there
                            never was one) ⇒ ``create_run(trigger="auto")``
  3. ``pending_runs(1)`` → ``claim_run`` → ``run_refresh``

A refresh reads seven public sources, embeds and scores with the model, so it
runs for a minute or more. It must never block a request.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from ..config import config
from ..services import general_trends_pipeline as pipeline
from ..services import general_trends_store as store

log = logging.getLogger("archivist.general_trends_poller")

POLL_INTERVAL_SEC = 30

_task: asyncio.Task | None = None
# One refresh at a time in this process: they share the small DB pool and the
# model quota.
_running = False


def _to_dt(value: Any) -> Optional[datetime]:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, str) and value.strip():
        s = value.strip()
        if s.endswith(("Z", "z")):
            s = s[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(s)
        except ValueError:
            return None
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    return None


def auto_refresh_due(latest_run: Optional[dict], *, now: datetime,
                     every_hours: int) -> bool:
    """Pure: should the poller queue an automatic refresh right now?

    * ``every_hours <= 0``           → never (manual only)
    * no run ever                    → yes
    * latest run still active        → no
    * latest finished (or, failing a ``finished_at``, started) at least
      ``every_hours`` ago            → yes
    """
    try:
        hours = int(every_hours)
    except (TypeError, ValueError):
        return False
    if hours <= 0:
        return False
    if not latest_run:
        return True
    status = str(latest_run.get("status") or "")
    if latest_run.get("active") or status not in ("done", "failed"):
        return False
    stamp = _to_dt(latest_run.get("finished_at")) or _to_dt(latest_run.get("started_at"))
    if stamp is None:
        return True
    now_dt = _to_dt(now) or datetime.now(timezone.utc)
    return (now_dt - stamp) >= timedelta(hours=hours)


async def _poll_once() -> None:
    global _running
    if _running:
        return
    try:
        # Close out anything a previous process left mid-flight, so the UI's
        # poll loop always terminates.
        await store.fail_stale_runs()
        if config.general_trends_refresh_hours > 0 and not await store.run_is_active():
            latest = await store.latest_run()
            if auto_refresh_due(latest, now=datetime.now(timezone.utc),
                                every_hours=config.general_trends_refresh_hours):
                await store.create_run(metadata={"trigger": "auto"})
                log.info("[general-trends] auto refresh queued")
        queued = await store.pending_runs(limit=1)
    except Exception:
        log.exception("[general-trends] poller: queue query failed")
        return

    for row in queued:
        run_id = int(row["id"])
        try:
            # Atomic claim — two ticks must never run the same refresh twice.
            if not await store.claim_run(run_id):
                continue
            _running = True
            log.info("[general-trends] running refresh #%d", run_id)
            await pipeline.run_refresh(run_id, metadata=row.get("metadata") or {})
        except Exception:
            log.exception("[general-trends] poller: refresh #%d failed", run_id)
            try:
                await store.update_run(
                    run_id, status="failed", phase="failed",
                    error="the refresh failed unexpectedly", progress="the refresh failed",
                )
            except Exception:
                log.exception("[general-trends] poller: could not mark run #%d failed",
                              run_id)
        finally:
            _running = False


async def _loop() -> None:
    log.info("general_trends_poller started (every %ds)", POLL_INTERVAL_SEC)
    try:
        while True:
            await _poll_once()
            await asyncio.sleep(POLL_INTERVAL_SEC)
    except asyncio.CancelledError:
        log.info("general_trends_poller cancelled")
        raise
    except Exception:
        log.exception("general_trends_poller loop crashed")


def start() -> Optional[asyncio.Task]:
    """Start the loop, unless the stream is paused.

    Returns None when `GENERAL_TRENDS_ENABLED` is off — there is no task, so
    nothing ticks, nothing auto-queues and nothing claims a run left in the
    queue. The check is here rather than inside `_poll_once` on purpose: a
    poller that wakes every 30s only to decide it has nothing to do is a
    collector that still holds a DB connection and still writes a log line, and
    "stopped collecting" should mean stopped.

    Refusing to CLAIM is the half that matters most. Rows already queued (a
    manual refresh fired before the pause, an auto run interrupted by a
    restart) stay `pending` in `general_trend_runs` and are simply never picked
    up, so re-enabling replays them instead of losing them.
    """
    global _task
    if _task is not None and not _task.done():
        return _task
    if not config.general_trends_enabled:
        log.info("general_trends_poller NOT started — GENERAL_TRENDS_ENABLED is off")
        return None
    _task = asyncio.create_task(_loop(), name="general_trends_poller")
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
