"""GET /api/website + /api/website/pages/{id}."""
from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, HTTPException

from ..db import get_conn

router = APIRouter(prefix="/api/website", tags=["website"])


def _serialise(v: Any) -> Any:
    if isinstance(v, datetime):
        return v.isoformat()
    return v


def _row(row: dict | None) -> dict | None:
    return {k: _serialise(v) for k, v in row.items()} if row else None


@router.get("")
async def list_pages(limit: int = 50) -> dict:
    limit = max(1, min(int(limit), 200))
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT id, url, page_title, fetched_status, ai_analyzed_status,
                       embedded_status, last_fetched_at, last_analyzed_at,
                       LEFT(COALESCE(ai_summary, ''), 240) AS preview,
                       length(extracted_text) AS text_chars
                  FROM website_pages
                 ORDER BY (ai_summary IS NOT NULL) DESC,
                          last_analyzed_at DESC NULLS LAST,
                          id DESC
                 LIMIT %s
                """,
                (limit,),
            )
            items = [_row(r) for r in await cur.fetchall()]
            await cur.execute(
                """
                SELECT
                  COUNT(*)::int AS total,
                  COUNT(*) FILTER (WHERE fetched_status='fetched')::int AS fetched,
                  COUNT(*) FILTER (WHERE ai_analyzed_status='completed')::int AS analyzed,
                  COUNT(*) FILTER (WHERE embedded_status='completed')::int AS embedded
                FROM website_pages
                """
            )
            totals = _row(await cur.fetchone()) or {}
            await cur.execute(
                "SELECT MAX(finished_at) AS last_run FROM scrape_runs WHERE status='completed'"
            )
            last_run = _row(await cur.fetchone())
    return {
        "items": items,
        "totals": totals,
        "last_scrape_at": last_run.get("last_run") if last_run else None,
    }


@router.get("/pages/{page_id}")
async def get_page(page_id: int) -> dict:
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT id, url, page_title, fetched_status, ai_analyzed_status,
                       embedded_status, last_fetched_at, last_analyzed_at,
                       ai_summary, length(extracted_text) AS text_chars,
                       LEFT(extracted_text, 2000) AS text_preview,
                       (SELECT COUNT(*) FROM assets a WHERE a.page_id=p.id) AS asset_count
                  FROM website_pages p
                 WHERE id=%s
                """,
                (page_id,),
            )
            row = await cur.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="page not found")
    return _row(row)
