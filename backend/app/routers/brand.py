"""GET /api/brand — latest completed brand profile artifact."""
from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter

from ..db import get_conn

router = APIRouter(prefix="/api", tags=["brand"])


def _serialise(v: Any) -> Any:
    if isinstance(v, datetime):
        return v.isoformat()
    return v


def _row(row: dict | None) -> dict | None:
    return {k: _serialise(v) for k, v in row.items()} if row else None


@router.get("/brand")
async def get_brand() -> dict:
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT id, status, finished_at, notes
                  FROM brand_profile_runs
                 WHERE status='completed'
                 ORDER BY id DESC LIMIT 1
                """
            )
            run = await cur.fetchone()
            if not run:
                return {"run": None, "payload": None, "sources": {}}
            await cur.execute(
                """
                SELECT payload FROM brand_profile_artifacts
                 WHERE run_id=%s AND kind='final_json'
                 ORDER BY id DESC LIMIT 1
                """,
                (run["id"],),
            )
            art = await cur.fetchone()
            await cur.execute(
                """
                SELECT
                  (SELECT COUNT(*)::int FROM website_pages WHERE ai_summary IS NOT NULL) AS website_pages,
                  (SELECT COUNT(*)::int FROM assets WHERE asset_type='image' AND ai_description IS NOT NULL) AS image_descriptions,
                  (SELECT COUNT(*)::int FROM video_summaries) AS video_summaries,
                  (SELECT COUNT(*)::int FROM brand_documents WHERE upload_status='completed') AS owner_documents
                """
            )
            sources = _row(await cur.fetchone()) or {}
    return {
        "run": _row(run),
        "payload": art["payload"] if art else None,
        "sources": sources,
    }
