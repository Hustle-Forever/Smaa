"""Trend Radar API: the daily AI and coding top 10 for 42 Abu Dhabi.

    GET   /api/radar           the newest radar (cards, "is it today's", building?)
    POST  /api/radar/refresh   start a build now (returns at once; poll the GET)

A build reads about fifteen public sites and makes a few Gemini calls, so it
runs in the background and the page polls. One at a time: a second press while
one is running is told so rather than starting another.
"""
from __future__ import annotations

import asyncio

from fastapi import APIRouter

from ..services import trend_radar

router = APIRouter(prefix="/api/radar", tags=["radar"])

_task: asyncio.Task | None = None


@router.get("")
async def get_radar() -> dict:
    return trend_radar.latest()


@router.post("/refresh")
async def refresh_radar() -> dict:
    global _task
    if not trend_radar.is_building():
        # Hold a reference: an un-referenced task can be garbage collected mid-run.
        _task = asyncio.create_task(trend_radar.build())
        await asyncio.sleep(0)  # let it flip the building flag before we answer
    return trend_radar.latest()
