"""Liveness probe."""
from __future__ import annotations

from fastapi import APIRouter

from ..config import config
from ..db import ping

router = APIRouter(prefix="/api", tags=["health"])


@router.get("/health")
async def health() -> dict:
    # A transient DB hiccup (e.g. the pool warming up during a restart) must NOT
    # turn the liveness probe into a 500 — the frontend polls this constantly.
    # Report ok=false instead of raising.
    try:
        db_ok = await ping()
    except Exception:
        db_ok = False
    return {
        "ok": db_ok,
        "service": "studio-backend",
        "target_url": config.target_url,
        "supabase_project": config.supabase_project_ref,
        "clip_enabled": config.use_clip,
    }
