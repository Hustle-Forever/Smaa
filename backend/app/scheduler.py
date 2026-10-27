"""In-process APScheduler — fires the website-ingest agent on a cron schedule.

Lives inside the FastAPI process. When uvicorn is up, the scheduler is up;
when uvicorn stops, it stops. No external workflow tools.

Config (from .env):
  AUTO_CRAWL_CRON      Standard 5-field cron expression. Empty = disabled.
  SCHEDULER_TIMEZONE   Timezone for the cron (default "UTC").

Behavior:
  - On boot, if AUTO_CRAWL_CRON is set, schedule a single job that fires the
    website-ingest agent through the same RunRegistry the HTTP route uses
    (same code path, same SSE, same scrape_runs row).
  - Each scheduled run is tagged `notes='scheduled crawl'`.
  - If a website-ingest run is already in flight when the tick fires, the
    scheduler skips that tick (no double-runs, no race on shared DB rows).

Public API:
  setup_scheduler()    Start the scheduler if config enables it. Idempotent.
  shutdown_scheduler() Stop. Idempotent.
  status()             Read-only dict for /api/scheduler/status.
"""
from __future__ import annotations

import logging
from typing import Any

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from .agents.website_ingest.agent import RunOptions, WebsiteIngestAgent
from .config import config
from .workers.runner import get_registry

log = logging.getLogger("archivist.scheduler")

_scheduler: AsyncIOScheduler | None = None
_job_id = "website_ingest_daily"
_radar_job_id = "trend_radar_daily"


def _cron_fields(expr: str) -> dict[str, str]:
    """Convert "m h dom mon dow" → APScheduler CronTrigger kwargs."""
    parts = expr.split()
    if len(parts) != 5:
        raise ValueError(
            f"AUTO_CRAWL_CRON must be 5 fields (minute hour day month weekday); got {expr!r}"
        )
    return {
        "minute": parts[0],
        "hour": parts[1],
        "day": parts[2],
        "month": parts[3],
        "day_of_week": parts[4],
    }


async def _tick() -> None:
    """One scheduled execution: skip if a website-ingest run is in-flight,
    otherwise kick the agent through the same registry as the HTTP route."""
    registry = get_registry()

    # Skip if any website-ingest run is currently active.
    for entry in list(registry._entries.values()):  # noqa: SLF001 — internal peek by design
        if entry.task and not entry.task.done():
            # Heuristic: every active entry uses the registry, but we don't know
            # the agent type here. Inspect events for a "run_started" with the
            # right agent label.
            for evt in entry.events:
                if evt.get("type") == "run_started" and evt.get("target_domain"):
                    # website_ingest emits target_domain on run_started
                    log.info("[scheduler] skipping tick — run #%s is in flight", entry.run_id)
                    return

    options = RunOptions(run_notes="scheduled crawl")
    log.info("[scheduler] firing website-ingest run (cron=%s)", config.auto_crawl_cron)

    async def factory(emit):
        agent = WebsiteIngestAgent(options, on_event=emit)
        return await agent.run()

    await registry.start(factory)


async def _radar_tick() -> None:
    """Build the morning Trend Radar (one JSON file, no database)."""
    from .services import trend_radar

    log.info("[scheduler] building the Trend Radar")
    await trend_radar.build()


async def setup_scheduler() -> None:
    """Start the scheduler. Idempotent. Arms the crawl when AUTO_CRAWL_CRON is set
    and the daily Trend Radar unless TREND_RADAR_HOUR is -1."""
    global _scheduler

    if _scheduler is not None:
        return

    cron = (config.auto_crawl_cron or "").strip()
    trigger = None
    if cron:
        try:
            trigger = CronTrigger(timezone=config.scheduler_timezone, **_cron_fields(cron))
        except Exception as e:
            log.error("Invalid AUTO_CRAWL_CRON=%r: crawl not scheduled: %s", cron, e)
    else:
        log.info("Daily crawl disabled (AUTO_CRAWL_CRON is empty)")

    radar_on = 0 <= config.trend_radar_hour <= 23
    if trigger is None and not radar_on:
        return

    sched = AsyncIOScheduler(timezone=config.scheduler_timezone)
    if trigger is not None:
        sched.add_job(
            _tick,
            trigger=trigger,
            id=_job_id,
            misfire_grace_time=3600,  # tolerate a 1h delay (e.g. machine asleep)
            coalesce=True,             # collapse missed runs into one
            max_instances=1,           # never overlap
        )
    if radar_on:
        # Gulf time on purpose, whatever SCHEDULER_TIMEZONE says: the radar is
        # for the team's morning in Abu Dhabi.
        sched.add_job(
            _radar_tick,
            trigger=CronTrigger(hour=config.trend_radar_hour, minute=0, timezone="Asia/Dubai"),
            id=_radar_job_id,
            misfire_grace_time=6 * 3600,
            coalesce=True,
            max_instances=1,
        )
    sched.start()
    _scheduler = sched

    for jid in (_job_id, _radar_job_id):
        job = sched.get_job(jid)
        if job:
            log.info("Scheduler armed: job=%s tz=%s next_run=%s",
                     jid, job.trigger.timezone, job.next_run_time)


async def shutdown_scheduler() -> None:
    global _scheduler
    if _scheduler is not None:
        try:
            _scheduler.shutdown(wait=False)
        except Exception:
            log.exception("Scheduler shutdown raised")
        _scheduler = None
        log.info("Scheduler stopped")


def status() -> dict[str, Any]:
    """Snapshot for /api/scheduler/status."""
    if _scheduler is None or not (config.auto_crawl_cron or "").strip():
        return {
            "enabled": False,
            "cron": config.auto_crawl_cron or None,
            "timezone": config.scheduler_timezone,
            "next_run_at": None,
        }
    job = _scheduler.get_job(_job_id)
    next_run = job.next_run_time.isoformat() if (job and job.next_run_time) else None
    return {
        "enabled": True,
        "cron": config.auto_crawl_cron,
        "timezone": config.scheduler_timezone,
        "next_run_at": next_run,
    }
