"""Drives autonomous campaigns forward, one tick at a time.

Same shape as the other eleven pollers: a single asyncio task, a 15-second loop,
and every decision derived from DB state so a restart loses nothing.

## What it does, and what it refuses to do

Five stages per tick: plan a due week, generate one item per campaign, caption
what finished, book what a human approved, and mirror the outcome of bookings.

**It never publishes.** Approving an item only books it into
`scheduled_instagram_posts`; a human then presses Post in Library → Scheduled,
which runs the pre-existing roadmap approve endpoint. Keeping exactly one
publisher in the codebase is what makes a double post impossible, so this module
must never import `instagram_publish`. Stage 5 only *observes* what happened.

**It never approves.** `campaign_store.approve` / `.decline` are guarded on
`ready_for_review`, and the only state this worker may take an item out of that
state into is `expired` (a slot whose week has passed). Content decisions belong
to a person.

## Why the guards are where they are

* Each stage is wrapped in its own try/except. The stock `_loop` in the other
  pollers exits permanently if anything escapes `_poll_once`, which would mean one
  transient database blip silently stops every campaign until someone restarts the
  backend.
* Generation is claimed with an atomic UPDATE before any slow work, and the claim
  is committed first — a laptop and a deployment share this database, and holding
  a transaction across a 180-second render gets the connection killed by Supabase's
  pooler anyway.
* Provider capacity is checked BEFORE claiming. It used to also keep a floor of
  spare slots under an hourly image cap, so a campaign could not lock a human
  out of Create Poster; that cap was removed, so the budget ceiling below is the
  only thing that defers an item now.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, timedelta
from typing import Any, Optional
from zoneinfo import ZoneInfo

from fastapi import HTTPException

from ..config import config
from ..services import campaign_planner as planner
from ..services import campaign_store as store
from ..services import caption_service
from ..services import poster_pipeline as pipeline
from ..services.poster_styles import get_poster_style
from ..services.vertex_client import VertexBudgetExceededError, get_meter

log = logging.getLogger("archivist.worker.campaign")

POLL_INTERVAL_SEC = 15

# Local time the brand posts in. Gulf Standard Time has no DST, so wall-clock
# arithmetic here is safe. Matches the frontend and routers/campaigns.
CAMPAIGN_TZ = "Asia/Dubai"

# Stop generating at this fraction of the Vertex budget, leaving headroom for
# interactive work. Mirrors the router's own `_budget_guard`.
BUDGET_CEILING = 0.95

CYCLE_DAYS = 7

_task: asyncio.Task | None = None


# ---------------------------------------------------------------------------
# Scheduling helpers
# ---------------------------------------------------------------------------

def _now_local() -> datetime:
    return datetime.now(ZoneInfo(CAMPAIGN_TZ))


def _slot_times(windows: list[dict], count: int, *, start: datetime) -> list[datetime]:
    """Pick `count` posting times from the campaign's weekly windows.

    Walks forward from `start` and takes the next matching weekday/time for each
    window in turn, so a campaign created on a Friday still fills Monday's slot
    next week rather than skipping it. Falls back to one a day at 17:00 when a
    campaign has no windows configured.
    """
    if not windows:
        windows = [{"weekday": (start.weekday() + i) % 7, "time": "17:00"}
                   for i in range(max(1, count))]
    out: list[datetime] = []
    for i in range(count):
        w = windows[i % len(windows)]
        try:
            hh, mm = (int(x) for x in str(w.get("time") or "17:00").split(":", 1))
        except Exception:
            hh, mm = 17, 0
        weekday = int(w.get("weekday") or 0) % 7
        # How many days forward to the next occurrence of that weekday. Extra
        # weeks once we wrap past the windows list, so slots never collide.
        delta = (weekday - start.weekday()) % 7
        cycle_offset = (i // len(windows)) * 7
        when = (start + timedelta(days=delta + cycle_offset)).replace(
            hour=hh, minute=mm, second=0, microsecond=0
        )
        if when <= start:
            when += timedelta(days=7)
        out.append(when)
    return out


def _capacity_reason() -> Optional[str]:
    """Why generation must wait, or None if there is room.

    Checked before claiming an item so a deferral costs nothing — no attempt
    burned, no row moved.
    """
    meter = get_meter()
    if meter.over_budget(BUDGET_CEILING):
        return (f"the Vertex budget is {meter.spent_usd / meter.budget_usd:.0%} spent "
                f"and generation is paused to leave room for interactive work")
    # There is no local image cap any more, so the Vertex budget above is the
    # only thing that can hold a campaign back.
    return None


# ---------------------------------------------------------------------------
# Stage 1 — plan a due cycle
# ---------------------------------------------------------------------------

async def _plan_due_cycles() -> None:
    for campaign in await store.due_campaigns():
        cid = int(campaign["id"])
        cycle_index = int(campaign.get("cycle_index") or 0)
        try:
            # Don't start a new week while the last one is still working.
            unfinished = [
                i for i in await store.items_for(cid, cycle_index=cycle_index)
                if i.get("status") in (store.ITEM_PLANNED, store.ITEM_GENERATING)
            ]
            if unfinished:
                log.info("[campaign %s] week %d still has %d unfinished item(s) — "
                         "not planning the next one yet", cid, cycle_index + 1, len(unfinished))
                continue

            # Respect a fixed-length campaign.
            duration = campaign.get("duration_weeks")
            next_cycle = cycle_index + (1 if await _cycle_has_items(cid, cycle_index) else 0)
            if duration is not None and next_cycle >= int(duration):
                log.info("[campaign %s] finished its %s week(s) — archiving", cid, duration)
                await store.update_campaign(
                    cid, status=store.CAMPAIGN_ARCHIVED,
                    status_reason=f"completed {duration} week(s)",
                )
                continue

            posters = int(campaign.get("posters_per_week") or 0)
            videos = int(campaign.get("videos_per_week") or 0)
            slots = await planner.plan_cycle(
                brief=campaign.get("topic_brief") or campaign.get("name") or "",
                posters=posters,
                videos=videos,
                week_number=next_cycle + 1,
                used_titles=await store.recent_idea_titles(cid),
                decline_notes=await store.recent_decline_notes(cid),
            )

            start = _now_local()
            times = _slot_times(campaign.get("posting_windows") or [], len(slots), start=start)
            rows = [
                {
                    "campaign_id": cid,
                    "cycle_index": next_cycle,
                    "cycle_start": start.date().isoformat(),
                    "slot_index": i,
                    "kind": slot["kind"],
                    "status": store.ITEM_PLANNED,
                    "idea": slot,
                    "idea_title": slot["title"],
                    "proposed_time": times[i].isoformat(),
                    "attempt": 0,
                    "cost_usd": 0,
                }
                for i, slot in enumerate(slots)
            ]
            inserted = await store.plan_items(rows)
            await store.update_campaign(
                cid,
                cycle_index=next_cycle,
                last_cycle_at=start.isoformat(),
                next_cycle_at=(start + timedelta(days=CYCLE_DAYS)).isoformat(),
                status_reason=None,
            )
            log.info("[campaign %s] planned week %d · %d item(s)",
                     cid, next_cycle + 1, len(inserted))
        except planner.PlanningFailedError as exc:
            # Push the retry out a few minutes rather than hammering the model.
            log.warning("[campaign %s] planning failed: %s", cid, exc)
            await store.update_campaign(
                cid,
                next_cycle_at=(_now_local() + timedelta(minutes=10)).isoformat(),
                status_reason=f"couldn't plan this week yet: {exc}",
            )
        except Exception:
            log.exception("[campaign %s] planning crashed", cid)


async def _cycle_has_items(campaign_id: int, cycle_index: int) -> bool:
    return bool(await store.items_for(campaign_id, cycle_index=cycle_index, limit=1))


# ---------------------------------------------------------------------------
# Stage 2 — generate one item per campaign
# ---------------------------------------------------------------------------

async def _generate_one(campaign: dict) -> None:
    """Claim and generate a single planned item. One per campaign per tick, so a
    busy campaign can't monopolise the image quota."""
    cid = int(campaign["id"])

    reason = _capacity_reason()
    if reason:
        log.info("[campaign %s] holding off: %s", cid, reason)
        return

    # Stay inside the campaign's own weekly budget.
    budget = campaign.get("weekly_budget_usd")
    if budget:
        spent = await store.spent_this_cycle(cid, int(campaign.get("cycle_index") or 0))
        if spent >= float(budget):
            await store.pause_campaign(
                cid,
                f"paused: this week has cost ${spent:.2f} of its "
                f"${float(budget):.2f} budget",
            )
            return

    item = await store.claim_planned(cid)
    if item is None:
        return  # nothing planned

    item_id = int(item["id"])
    idea = item.get("idea") or {}
    kind = item.get("kind") or "poster"

    if kind != store.KIND_POSTER:
        # Video items arrive in Phase 3. Leave the row alone rather than failing
        # it, so it is picked up once that lands.
        await store.update_item(item_id, status=store.ITEM_PLANNED, claimed_at=None)
        return

    try:
        style = get_poster_style(campaign.get("poster_style"))
        topic = planner.poster_topic_for(idea) or item.get("idea_title") or ""
        # A regeneration after a decline: tell Stage 1 what to avoid.
        if item.get("decline_note"):
            topic += (f"\n\nThe reviewer rejected the previous attempt because: "
                      f"{item['decline_note']}. Make a clearly different image that "
                      f"answers that objection.")

        concepts, stage1_cost = await pipeline.poster_concepts(topic, 1, style)

        specs = [{
            "prompt": concepts[0]["prompt"],
            "concept": concepts[0]["summary"],
            "variation_ref_path": None,
            "variation_of": None,
        }]
        recs = await pipeline.run_stage2_batch(
            specs, [], "style",
            topic=item.get("idea_title") or topic[:200],
            reference_asset_ids=[],
            reference_upload_ids=[],
            parent_generation_id=None,
            stage1_cost_usd=stage1_cost,
            style=style,
            mode=pipeline.POSTER_MODE,
            sequential=True,
        )
        rec = recs[0] if recs else None
        if not rec or rec.get("status") != pipeline.STATUS_READY:
            raise RuntimeError((rec or {}).get("error") or "the image render produced nothing")

        # Media is ready; stage 3 writes the caption and opens gate 1.
        await store.update_item(
            item_id,
            gen_kind="poster",
            gen_id=int(rec["id"]),
            cost_usd=float(rec.get("cost_usd") or 0),
            error=None,
        )
        log.info("[campaign %s] item %s rendered poster #%s", cid, item_id, rec["id"])

    except HTTPException as exc:
        # The pipeline speaks in HTTP codes even off-request (a repo convention).
        # They carry real meaning, so branch rather than treating all as failure.
        if exc.status_code == 429:
            # Rate limited by the provider. Not the item's fault, so put it back
            # with no attempt burned. (This used to also cover a local hourly
            # cap; that cap was removed, so 429 now only ever means upstream.)
            await store.update_item(item_id, status=store.ITEM_PLANNED, claimed_at=None)
            log.info("[campaign %s] rate limited upstream, item %s requeued", cid, item_id)
        elif exc.status_code == 503:
            await store.update_item(item_id, status=store.ITEM_PLANNED, claimed_at=None)
            await store.pause_campaign(cid, f"paused: {exc.detail}")
        else:
            await store.record_attempt_failure(item_id, f"{exc.status_code}: {exc.detail}")
    except VertexBudgetExceededError as exc:
        await store.update_item(item_id, status=store.ITEM_PLANNED, claimed_at=None)
        await store.pause_campaign(cid, f"paused: {exc}")
    except Exception as exc:
        log.exception("[campaign %s] item %s generation failed", cid, item_id)
        await store.record_attempt_failure(item_id, f"{type(exc).__name__}: {exc}")


async def _generate() -> None:
    for campaign in await store.active_campaigns():
        try:
            await _generate_one(campaign)
        except Exception:
            log.exception("[campaign %s] generation stage crashed", campaign.get("id"))


# ---------------------------------------------------------------------------
# Stage 3 — caption finished media, then open GATE 1
# ---------------------------------------------------------------------------

async def _caption_and_gate() -> None:
    from ..services import yas_local_store as ystore

    items = await store.items_by_status(store.ITEM_GENERATING, limit=25)
    ready_by_campaign: dict[int, list[int]] = {}

    for item in items:
        item_id = int(item["id"])
        gen_id = item.get("gen_id")
        if not gen_id:
            continue  # still rendering; stage 5 reclaims it if the worker died

        try:
            gen = await ystore.get(int(gen_id))
            if gen is None:
                await store.record_attempt_failure(item_id, "the generated image disappeared")
                continue
            if gen.get("status") == pipeline.STATUS_FAILED:
                await store.record_attempt_failure(
                    item_id, gen.get("error") or "the image render failed")
                continue
            if gen.get("status") not in (pipeline.STATUS_READY, pipeline.STATUS_SAVED):
                continue  # not finished yet

            idea = item.get("idea") or {}
            topic = item.get("idea_title") or idea.get("title") or ""
            out = await caption_service.generate_caption(
                topic, extra_direction=planner.caption_direction_for(idea)
            )
            gated = await store.mark_ready_for_review(
                item_id,
                caption=out["caption"],
                gen_kind="poster",
                gen_id=int(gen_id),
                cost_usd=float(item.get("cost_usd") or 0),
            )
            if gated:
                ready_by_campaign.setdefault(int(item["campaign_id"]), []).append(item_id)
                log.info("[campaign %s] item %s awaiting review",
                         item["campaign_id"], item_id)
        except caption_service.CaptionUnavailableError as exc:
            await store.record_attempt_failure(item_id, str(exc))
        except Exception:
            log.exception("[campaign] captioning item %s failed", item_id)

    if ready_by_campaign:
        await _notify_review_ready(ready_by_campaign)


async def _notify_review_ready(by_campaign: dict[int, list[int]]) -> None:
    """One email per campaign per tick, and only once per item.

    Marks `notified_at` when the mail was sent OR when email isn't configured at
    all — the same rule the reminder poller uses, so an unconfigured mailbox does
    not turn into an infinite retry.
    """
    from ..services.email_service import email_configured, send_email

    configured = email_configured()
    for cid, item_ids in by_campaign.items():
        try:
            campaign = await store.get_campaign(cid)
            if campaign is None or not campaign.get("notify_email"):
                continue
            sent = False
            if configured:
                subject, text, html = _build_review_ready(campaign, len(item_ids))
                sent = await send_email(
                    to=config.reminder_email, subject=subject, text=text, html=html
                )
            if sent or not configured:
                await store.mark_notified(item_ids)
        except Exception:
            log.exception("[campaign %s] review notification failed", cid)


def _build_review_ready(campaign: dict, count: int) -> tuple[str, str, str]:
    """The review-ready email.

    Deliberately NOT the existing reminder copy: that one says "review & approve"
    about a post that is already booked. This one is about content that has not
    been approved yet, and it must not imply anything will post on its own.
    """
    name = campaign.get("name") or "your campaign"
    noun = "post" if count == 1 else "posts"
    link = f"{config.app_base_url}/#campaigns/{campaign.get('id')}"
    subject = f"✳️ {count} new {noun} to review — {name}"
    text = (
        f"{name} produced {count} new {noun}.\n\n"
        f"Nothing is scheduled or posted until you approve it.\n\n"
        f"Review them: {link}\n"
    )
    html = (
        f'<div style="font-family:system-ui,sans-serif;line-height:1.6">'
        f'<p><strong>{name}</strong> produced {count} new {noun}.</p>'
        f'<p style="color:#555">Nothing is scheduled or posted until you approve it.</p>'
        f'<p><a href="{link}" style="display:inline-block;background:#06B6A2;color:#fff;'
        f'padding:10px 16px;border-radius:8px;text-decoration:none">Review {noun}</a></p>'
        f"</div>"
    )
    return subject, text, html


# ---------------------------------------------------------------------------
# Stage 4 — book what a human approved (still not publishing)
# ---------------------------------------------------------------------------

async def _book_approved() -> None:
    from ..services import approved_media
    from ..services import asset_storage
    from ..services import scheduled_posts as sched_store

    for item in await store.items_by_status(store.ITEM_APPROVED, limit=25):
        item_id = int(item["id"])
        gen_id = item.get("gen_id")
        gen_kind = item.get("gen_kind") or "poster"
        if not gen_id:
            await store.record_attempt_failure(item_id, "approved but no media attached")
            continue

        # `poster` and `animation` are the two tools approved_media already knows
        # how to fetch bytes for.
        tool = "poster" if gen_kind == "poster" else "animation"
        media_type = "image" if gen_kind == "poster" else "video"

        try:
            media_url = await approved_media.upload_for_post(
                tool=tool, ref_id=int(gen_id), media_type=media_type
            )
        except FileNotFoundError:
            await store.update_item(
                item_id, status=store.ITEM_FAILED,
                error="the generated media was deleted before it could be scheduled",
            )
            continue
        except Exception as exc:
            # Hosting is transient often enough (bucket config, network) that this
            # should retry rather than fail the item outright.
            log.warning("[campaign] hosting item %s failed: %s", item_id, exc)
            await store.update_item(item_id, error=f"couldn't host the media yet: {exc}"[:500])
            continue

        row = None
        try:
            when = item.get("proposed_time") or _now_local().isoformat()
            row = await sched_store.create(
                tool=tool,
                ref_id=int(gen_id),
                media_type=media_type,
                caption=item.get("caption") or "",
                scheduled_at=when,
                timezone=CAMPAIGN_TZ,
                media_url=media_url,
                # No `suppress_reminders`: the existing reminder emails at T-60 are
                # exactly the nudge that gate 2 needs.
                metadata={
                    "campaign_id": int(item["campaign_id"]),
                    "campaign_item_id": item_id,
                },
            )
            booked = await store.mark_scheduled(item_id, int(row["id"]))
            if booked:
                log.info("[campaign %s] item %s booked as scheduled post #%s (awaiting "
                         "a human Post)", item["campaign_id"], item_id, row["id"])
        except Exception as exc:
            log.exception("[campaign] booking item %s failed", item_id)
            if row is None:
                # No booking points at the copy just hosted, and the next tick
                # hosts a fresh one under a new random key: without this, every
                # failing tick would leave another public object behind.
                await asset_storage.delete_public_urls([media_url])
            await store.update_item(item_id, error=f"couldn't schedule it: {exc}"[:500])


# ---------------------------------------------------------------------------
# Stage 5 — mirror booking outcomes + housekeeping
# ---------------------------------------------------------------------------

async def _mirror_and_housekeep() -> None:
    from ..services import scheduled_posts as sched_store

    # Mirror only. The publish itself was a human pressing Post.
    for item in await store.items_by_status(store.ITEM_SCHEDULED, limit=50):
        post_id = item.get("scheduled_post_id")
        if not post_id:
            continue
        try:
            row = await sched_store.get(int(post_id))
            await store.mark_from_scheduled_row(int(item["id"]), row)
        except Exception:
            log.exception("[campaign] mirroring item %s failed", item.get("id"))

    try:
        reclaimed = await store.reclaim_stale()
        if reclaimed:
            log.info("[campaign] reclaimed %d item(s) stranded by a restart", len(reclaimed))
    except Exception:
        log.exception("[campaign] reclaiming stale claims failed")

    try:
        expired = await store.expire_stale_reviews(cycle_days=CYCLE_DAYS)
        if expired:
            log.info("[campaign] expired %d unreviewed item(s)", len(expired))
    except Exception:
        log.exception("[campaign] expiring stale reviews failed")


# ---------------------------------------------------------------------------
# Tick + lifecycle
# ---------------------------------------------------------------------------

async def _poll_once() -> None:
    """One pass. Every stage is isolated: a failure in one must not stop the
    others, and must never kill the loop."""
    for name, stage in (
        ("plan", _plan_due_cycles),
        ("generate", _generate),
        ("caption", _caption_and_gate),
        ("book", _book_approved),
        ("mirror", _mirror_and_housekeep),
    ):
        try:
            await stage()
        except Exception:
            log.exception("[campaign] stage %r failed this tick", name)


async def _loop() -> None:
    log.info("[campaign] poller started (every %ss)", POLL_INTERVAL_SEC)
    while True:
        try:
            await _poll_once()
        except asyncio.CancelledError:
            raise
        except Exception:
            # Belt and braces: _poll_once already isolates each stage, but the
            # loop must survive absolutely anything or campaigns stop silently.
            log.exception("[campaign] tick crashed")
        try:
            await asyncio.sleep(POLL_INTERVAL_SEC)
        except asyncio.CancelledError:
            raise


def start() -> asyncio.Task:
    global _task
    if _task is not None and not _task.done():
        return _task
    _task = asyncio.create_task(_loop(), name="campaign_poller")
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
