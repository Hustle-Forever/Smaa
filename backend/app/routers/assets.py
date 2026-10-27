"""GET /api/assets — asset browser (new, supports the visual-emphasis UX)."""
from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Query

from ..db import get_conn

router = APIRouter(prefix="/api/assets", tags=["assets"])


def _serialise(v: Any) -> Any:
    if isinstance(v, datetime):
        return v.isoformat()
    return v


def _row(row: dict | None) -> dict | None:
    return {k: _serialise(v) for k, v in row.items()} if row else None


@router.get("")
async def list_assets(
    asset_type: Optional[str] = Query(None, alias="type"),
    page_id: Optional[int] = None,
    limit: int = 50,
    only_completed: bool = False,
) -> dict:
    limit = max(1, min(int(limit), 200))
    where: list[str] = []
    params: list = []

    if asset_type:
        if asset_type == "video":
            where.append("asset_type IN ('video_native','video_youtube','video_vimeo','instagram')")
        else:
            where.append("asset_type=%s")
            params.append(asset_type)
    if page_id is not None:
        where.append("page_id=%s")
        params.append(page_id)
    if only_completed:
        where.append("analyze_status='completed'")

    sql = (
        "SELECT id, page_id, asset_type, source_url, canonical_url, "
        "       local_path, width, height, bytes, mime_type, "
        "       download_status, analyze_status, embed_status, "
        "       LEFT(COALESCE(ai_description, ''), 240) AS preview, "
        "       (description_embedding IS NOT NULL) AS has_text_embed, "
        "       created_at, updated_at "
        "  FROM assets"
    )
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY id DESC LIMIT %s"
    params.append(limit)

    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(sql, params)
            items = [_row(r) for r in await cur.fetchall()]
            await cur.execute(
                """
                SELECT
                  COUNT(*)::int AS total,
                  COUNT(*) FILTER (WHERE asset_type='image')::int AS images,
                  COUNT(*) FILTER (WHERE asset_type IN ('video_native','video_youtube','video_vimeo','instagram'))::int AS videos,
                  COUNT(*) FILTER (WHERE asset_type='pdf')::int AS pdfs,
                  COUNT(*) FILTER (WHERE download_status='downloaded')::int AS downloaded,
                  COUNT(*) FILTER (WHERE analyze_status='completed')::int AS analyzed
                FROM assets
                """
            )
            totals = _row(await cur.fetchone()) or {}
    return {"items": items, "totals": totals}


@router.get("/{asset_id}")
async def get_asset(asset_id: int) -> dict:
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT * FROM assets WHERE id=%s",
                (asset_id,),
            )
            row = await cur.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="asset not found")
    # Don't ship the embedding vector over HTTP — too big
    row = dict(row)
    row.pop("description_embedding", None)
    return _row(row)
