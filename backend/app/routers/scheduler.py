"""GET /api/scheduler/status — read-only view of the in-process scheduler.

  GET /api/scheduler/status
  → { enabled, cron, timezone, next_run_at }
"""
from __future__ import annotations

from fastapi import APIRouter

from ..scheduler import status as scheduler_status

router = APIRouter(prefix="/api/scheduler", tags=["scheduler"])


@router.get("/status")
async def get_status() -> dict:
    return scheduler_status()
