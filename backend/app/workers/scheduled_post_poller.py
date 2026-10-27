"""
Background worker — reminder dispatch for scheduled Instagram posts.

Started in main.lifespan; cancelled on shutdown. Survives restarts: it reads the
``scheduled_instagram_posts`` table, so any post that became due while the backend
was down is picked up on the next tick.

Every tick it finds pending posts whose scheduled time has arrived and that haven't
been reminded yet, emails the configured recipient a "time to approve & post"
reminder, and flips them to 'reminded'. Publishing itself stays manual (the user
clicks Approve & Post on the Approval step) — this worker only nudges.
"""
from __future__ import annotations

import asyncio
import logging

from ..config import config
from ..services import scheduled_posts as store
from ..services.email_service import build_reminder, email_configured, send_email

log = logging.getLogger("archivist.scheduled_post_poller")

POLL_INTERVAL_SEC = 60

_task: asyncio.Task | None = None


async def _poll_once() -> None:
    try:
        due = await store.due_for_reminder()
    except Exception:
        log.exception("scheduled_post_poller: query failed")
        return
    for row in due:
        try:
            subject, text, html = build_reminder(row)
            sent = await send_email(to=config.reminder_email, subject=subject, text=text, html=html)
            # Mark reminded when the email actually went out — OR when email isn't
            # configured at all (a setup choice, not a retry loop). A *configured*
            # mailbox that failed (transient SMTP hiccup) stays pending so the next
            # tick retries instead of silently dropping the reminder.
            if sent or not email_configured():
                await store.record_reminder(row["id"])
                log.info(
                    "scheduled_post_poller: reminder #%s sent for post #%s (emailed=%s)",
                    int(row.get("reminder_count") or 0) + 1, row.get("id"), sent,
                )
            else:
                log.warning("scheduled_post_poller: email failed for post #%s — will retry next tick", row.get("id"))
        except Exception:
            log.exception("scheduled_post_poller: failed on post #%s", row.get("id"))


async def _loop() -> None:
    log.info("scheduled_post_poller started (every %ds)", POLL_INTERVAL_SEC)
    try:
        while True:
            await _poll_once()
            await asyncio.sleep(POLL_INTERVAL_SEC)
    except asyncio.CancelledError:
        log.info("scheduled_post_poller cancelled")
        raise
    except Exception:
        log.exception("scheduled_post_poller loop crashed")


def start() -> asyncio.Task:
    global _task
    if _task is not None and not _task.done():
        return _task
    _task = asyncio.create_task(_loop(), name="scheduled_post_poller")
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
