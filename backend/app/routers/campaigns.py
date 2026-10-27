"""HTTP surface for autonomous content campaigns (the "Producer" tab).

Deliberately thin: every route validates its input, calls `campaign_store`, and
returns. All state-machine rules live in the store, and everything autonomous
lives in `workers/campaign_poller`.

## The two human gates, in endpoint terms

* `POST /items/{id}/approve` is **gate 1 — content only**. It marks a generated
  item good so the worker will book it. It does NOT publish anything.
* **Gate 2 is not in this file.** Publishing stays on the pre-existing
  `POST /api/yas-producer/roadmap/{post_id}/approve`, which a human triggers by
  pressing Post. Keeping one publisher in the codebase is what makes a double
  post structurally impossible, so nothing here may grow an Instagram call.

A guarded store transition that matches no row returns `None`, which becomes a
**409** here — that is how a double-click or a stale browser tab loses the race
safely instead of corrupting state.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any, Optional
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from ..services import campaign_store as store
from ..services.poster_styles import POSTER_STYLE_KEYS

log = logging.getLogger("archivist.router.campaigns")

router = APIRouter(prefix="/api/campaigns", tags=["campaigns"])

# Scheduling is expressed in the brand's local time. Matches the frontend's
# hardcoded Asia/Dubai (Gulf Standard Time has no DST, so a fixed +04:00 is safe).
CAMPAIGN_TZ = "Asia/Dubai"

MAX_PER_WEEK = 14          # one a day per kind — a sane ceiling for a beta
MAX_DURATION_WEEKS = 52


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------

class PostingWindow(BaseModel):
    weekday: int = Field(ge=0, le=6)   # 0 = Monday, matching Python's weekday()
    time: str = "17:00"                # HH:MM, local to CAMPAIGN_TZ


class CampaignCreateRequest(BaseModel):
    name: str
    topic_brief: str = ""
    posters_per_week: int = Field(default=3, ge=0, le=MAX_PER_WEEK)
    videos_per_week: int = Field(default=0, ge=0, le=MAX_PER_WEEK)
    duration_weeks: Optional[int] = Field(default=None, ge=1, le=MAX_DURATION_WEEKS)
    posting_windows: list[PostingWindow] = []
    poster_style: str = "voxel"
    video_duration_sec: int = Field(default=6, ge=4, le=8)
    video_with_audio: bool = True
    weekly_budget_usd: Optional[float] = Field(default=None, ge=0)
    notify_email: bool = True
    start_now: bool = True             # plan the first cycle on the next tick


class CampaignPatchRequest(BaseModel):
    name: Optional[str] = None
    topic_brief: Optional[str] = None
    status: Optional[str] = None       # active | paused (archive has its own verb)
    posters_per_week: Optional[int] = Field(default=None, ge=0, le=MAX_PER_WEEK)
    videos_per_week: Optional[int] = Field(default=None, ge=0, le=MAX_PER_WEEK)
    duration_weeks: Optional[int] = Field(default=None, ge=1, le=MAX_DURATION_WEEKS)
    posting_windows: Optional[list[PostingWindow]] = None
    poster_style: Optional[str] = None
    video_duration_sec: Optional[int] = Field(default=None, ge=4, le=8)
    video_with_audio: Optional[bool] = None
    weekly_budget_usd: Optional[float] = Field(default=None, ge=0)
    notify_email: Optional[bool] = None


class ApproveRequest(BaseModel):
    """Gate 1. The reviewer may fix the caption or move the slot in the same click."""
    caption: Optional[str] = None
    proposed_time: Optional[str] = None   # ISO 8601 with offset


class DeclineRequest(BaseModel):
    note: Optional[str] = None
    regenerate: bool = True


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _default_windows(posters: int, videos: int) -> list[dict]:
    """Spread N posts across the week when the user didn't pick slots.

    Weekdays only and late afternoon — that is when this brand's audience is
    active, and it keeps a fresh campaign from stacking everything on one day.
    """
    total = max(1, posters + videos)
    weekdays = [0, 2, 4, 1, 3, 5, 6]          # Mon, Wed, Fri, then fill in
    return [
        {"weekday": weekdays[i % len(weekdays)], "time": "17:00"}
        for i in range(min(total, MAX_PER_WEEK))
    ]


def _validated_windows(windows: list[PostingWindow]) -> list[dict]:
    out: list[dict] = []
    for w in windows:
        try:
            hh, mm = (int(x) for x in w.time.split(":", 1))
            if not (0 <= hh <= 23 and 0 <= mm <= 59):
                raise ValueError
        except Exception:
            raise HTTPException(400, f"posting window time must be HH:MM, got {w.time!r}")
        out.append({"weekday": int(w.weekday), "time": f"{hh:02d}:{mm:02d}"})
    return out


def _next_cycle_at(start_now: bool) -> str:
    """When the worker should plan the first cycle."""
    now = datetime.now(ZoneInfo(CAMPAIGN_TZ))
    return (now if start_now else now + timedelta(days=7)).isoformat()


def _cycle_cost_estimate(c: dict) -> float:
    """What one cycle costs at current provider rates, per attempt.

    Kept in step with `vertex_client`'s rate tables: a Nano-Banana-Pro image is
    $0.13, and veo-3.1-fast is $0.10/s of video plus $0.075/s of audio. A video
    item pays for its still frame too. Declines regenerate, so a bad week can
    cost up to `MAX_ATTEMPTS`x this.
    """
    image = 0.13
    per_sec = 0.10 + (0.075 if c.get("video_with_audio") else 0.0)
    dur = int(c.get("video_duration_sec") or 6)
    posters = int(c.get("posters_per_week") or 0)
    videos = int(c.get("videos_per_week") or 0)
    llm = 0.05  # gpt-4o planning + captions, rounded up
    return round(posters * image + videos * (image + dur * per_sec) + llm, 2)


def _out(c: dict) -> dict:
    """Add the derived fields the cards want, so the UI does no arithmetic."""
    c = dict(c)
    c["cycle_cost_estimate_usd"] = _cycle_cost_estimate(c)
    c["per_week_total"] = int(c.get("posters_per_week") or 0) + int(c.get("videos_per_week") or 0)
    return c


async def _require(campaign_id: int) -> dict:
    c = await store.get_campaign(campaign_id)
    if c is None:
        raise HTTPException(404, "campaign not found")
    return c


# ---------------------------------------------------------------------------
# Campaigns
# ---------------------------------------------------------------------------

@router.post("")
async def create_campaign(body: CampaignCreateRequest) -> dict:
    name = (body.name or "").strip()
    if not name:
        raise HTTPException(400, "a campaign needs a name")
    if body.posters_per_week + body.videos_per_week == 0:
        raise HTTPException(400, "set at least one poster or video per week")
    if body.poster_style not in POSTER_STYLE_KEYS:
        raise HTTPException(400, f"unknown poster style {body.poster_style!r}")

    windows = _validated_windows(body.posting_windows) or _default_windows(
        body.posters_per_week, body.videos_per_week
    )
    created = await store.create_campaign(
        name=name,
        topic_brief=(body.topic_brief or "").strip(),
        posters_per_week=body.posters_per_week,
        videos_per_week=body.videos_per_week,
        duration_weeks=body.duration_weeks,
        posting_windows=windows,
        poster_style=body.poster_style,
        video_duration_sec=body.video_duration_sec,
        video_with_audio=body.video_with_audio,
        weekly_budget_usd=body.weekly_budget_usd,
        notify_email=body.notify_email,
        next_cycle_at=_next_cycle_at(body.start_now),
    )
    log.info("[campaign] created #%s %r (%d posters + %d videos/wk)",
             created.get("id"), name, body.posters_per_week, body.videos_per_week)
    return _out(created)


@router.get("")
async def list_campaigns(include_archived: bool = False) -> dict:
    rows = await store.list_campaigns(include_archived=include_archived)
    return {"items": [_out(r) for r in rows]}


@router.get("/summary")
async def campaigns_summary() -> dict:
    """Counts for the sidebar badge. Deliberately tiny — it is polled."""
    return {"awaiting_review": await store.awaiting_review_count()}


@router.get("/review-queue")
async def review_queue(limit: int = Query(100, ge=1, le=200)) -> dict:
    """Everything waiting at gate 1, across every campaign."""
    return {"items": await store.review_queue(limit=limit)}


@router.get("/{campaign_id}")
async def get_campaign(
    campaign_id: int,
    cycle: Optional[int] = None,
    limit: int = Query(200, ge=1, le=500),
) -> dict:
    campaign = await _require(campaign_id)
    items = await store.items_for(campaign_id, cycle_index=cycle, limit=limit)
    return {
        "campaign": _out(campaign),
        "items": items,
        "spent_this_cycle_usd": await store.spent_this_cycle(
            campaign_id, int(campaign.get("cycle_index") or 0)
        ),
    }


@router.patch("/{campaign_id}")
async def patch_campaign(campaign_id: int, body: CampaignPatchRequest) -> dict:
    await _require(campaign_id)
    fields: dict[str, Any] = {}
    for key in ("name", "topic_brief", "posters_per_week", "videos_per_week",
                "duration_weeks", "poster_style", "video_duration_sec",
                "video_with_audio", "weekly_budget_usd", "notify_email"):
        val = getattr(body, key)
        if val is not None:
            fields[key] = val
    if "poster_style" in fields and fields["poster_style"] not in POSTER_STYLE_KEYS:
        raise HTTPException(400, f"unknown poster style {fields['poster_style']!r}")
    if body.posting_windows is not None:
        fields["posting_windows"] = _validated_windows(body.posting_windows)
    if body.status is not None:
        if body.status not in (store.CAMPAIGN_ACTIVE, store.CAMPAIGN_PAUSED):
            raise HTTPException(400, "status must be 'active' or 'paused'")
        fields["status"] = body.status
        # Clear a machine-set pause reason (e.g. budget) on manual resume, so the
        # card stops showing a stale explanation.
        fields["status_reason"] = None
    if not fields:
        raise HTTPException(400, "nothing to update")
    updated = await store.update_campaign(campaign_id, **fields)
    return _out(updated or {})


async def _cancel_booking(post_id: int) -> bool:
    """Cancel one booked post and delete its copies from the public bucket.

    That bucket is anonymous-readable, so a cancelled post's media would
    otherwise stay online for good. A post that was already published keeps
    its media: it is the record of what went out. Returns whether a row was
    cancelled.
    """
    from ..services import asset_storage
    from ..services import scheduled_posts as sched_store

    before = await sched_store.get(post_id)
    cancelled = await sched_store.cancel(post_id)
    if before and before.get("status") != "published":
        await asset_storage.delete_public_urls(sched_store.hosted_media_urls(before))
    return bool(cancelled)


@router.delete("/{campaign_id}")
async def delete_campaign(campaign_id: int) -> dict:
    """Remove a campaign and cancel anything it had booked but not yet posted.

    Published bookings are left alone on purpose — they are the record of what
    actually went out. The generated posters and videos also stay; they are
    ordinary Library items and the user may still want them.
    """
    await _require(campaign_id)
    items = await store.items_for(campaign_id, limit=500)
    cancelled = 0
    for it in items:
        post_id = it.get("scheduled_post_id")
        if not post_id or it.get("status") == store.ITEM_PUBLISHED:
            continue
        try:
            if await _cancel_booking(int(post_id)):
                cancelled += 1
        except Exception:
            # Best-effort: a stuck booking must not block deleting the campaign.
            log.warning("[campaign] could not cancel booking #%s", post_id, exc_info=True)
    deleted = await store.delete_campaign(campaign_id)
    return {"deleted": bool(deleted), "id": campaign_id, "bookings_cancelled": cancelled}


@router.post("/{campaign_id}/run-cycle")
async def run_cycle_now(campaign_id: int) -> dict:
    """Ask the worker to plan this campaign's next cycle on its next tick.

    Refused while the current cycle still has unfinished work, which would
    otherwise interleave two cycles' items in the review queue. Replanning is
    harmless anyway (the slot unique index makes it a no-op), but refusing gives
    a clearer answer than silently doing nothing.
    """
    campaign = await _require(campaign_id)
    if campaign.get("status") == store.CAMPAIGN_ARCHIVED:
        raise HTTPException(409, "this campaign is archived")
    pending = [
        i for i in await store.items_for(campaign_id, cycle_index=int(campaign.get("cycle_index") or 0))
        if i.get("status") in (store.ITEM_PLANNED, store.ITEM_GENERATING)
    ]
    if pending:
        raise HTTPException(
            409,
            f"this cycle still has {len(pending)} item(s) generating — "
            f"let them finish (or review them) before starting the next cycle",
        )
    updated = await store.update_campaign(
        campaign_id,
        next_cycle_at=datetime.now(ZoneInfo(CAMPAIGN_TZ)).isoformat(),
        status=store.CAMPAIGN_ACTIVE,
        status_reason=None,
    )
    return _out(updated or campaign)


# ---------------------------------------------------------------------------
# Items — GATE 1 lives here. Publishing (gate 2) does NOT.
# ---------------------------------------------------------------------------

@router.post("/items/{item_id}/approve")
async def approve_item(item_id: int, body: ApproveRequest) -> dict:
    """GATE 1: the human approves the generated content.

    This only says "the content is good". The worker then hosts it and books a
    calendar slot; a human still has to press Post for it to reach Instagram.
    """
    item = await store.get_item(item_id)
    if item is None:
        raise HTTPException(404, "item not found")
    updated = await store.approve(
        item_id,
        caption=(body.caption.strip() if body.caption is not None else None),
        proposed_time=body.proposed_time,
    )
    if updated is None:
        raise HTTPException(
            409,
            f"this item is '{item.get('status')}', not awaiting review — "
            f"someone may have already decided on it",
        )
    return updated


@router.post("/items/{item_id}/decline")
async def decline_item(item_id: int, body: DeclineRequest) -> dict:
    """GATE 1: the human rejects the content.

    By default the campaign regenerates it, folding the note into the next
    prompt. The note is kept either way — it becomes taste feedback for later
    cycles.
    """
    item = await store.get_item(item_id)
    if item is None:
        raise HTTPException(404, "item not found")
    updated = await store.decline(
        item_id,
        note=(body.note or "").strip() or None,
        regenerate=body.regenerate,
    )
    if updated is None:
        raise HTTPException(
            409,
            f"this item is '{item.get('status')}', not awaiting review — "
            f"someone may have already decided on it",
        )
    return updated


@router.post("/items/{item_id}/cancel")
async def cancel_item(item_id: int) -> dict:
    """Drop an approved or booked item before it goes out."""
    item = await store.get_item(item_id)
    if item is None:
        raise HTTPException(404, "item not found")
    post_id = item.get("scheduled_post_id")
    if post_id:
        try:
            await _cancel_booking(int(post_id))
        except Exception:
            log.warning("[campaign] could not cancel booking #%s", post_id, exc_info=True)
    updated = await store.cancel_item(item_id)
    if updated is None:
        raise HTTPException(409, f"a '{item.get('status')}' item cannot be cancelled")
    return updated


@router.get("/items/{item_id}")
async def get_item(item_id: int) -> dict:
    item = await store.get_item(item_id)
    if item is None:
        raise HTTPException(404, "item not found")
    return item
