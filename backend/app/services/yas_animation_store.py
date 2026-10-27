"""
Postgres-backed persistence for the Yas "Create Animation" pipeline.

Mirrors yas_local_store.py but in its own table (`yas_animation_generations`)
so animation records don't mix with poster generations. Frame + MP4 bytes live
in Supabase Storage (the `producer-media` bucket), referenced by
frame_/video_ storage columns (with a *_local_path fallback). Nothing here
touches the old `yas-output/` disk store.

Status flow: frame_ready -> animating -> ready   (or `failed` at any step).
The frame is rendered synchronously inside the request, so a record is born
`frame_ready`; the Veo step is async and finished by the local poller.

Public API is unchanged for callers: create / update / get / recent / pending.
"""
from __future__ import annotations

import asyncio
from datetime import datetime
from decimal import Decimal
from typing import Any, Iterable, Optional

from ..db import get_conn

_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS yas_animation_generations (
    id                     SERIAL PRIMARY KEY,
    prompt                 TEXT,
    image_prompt_used      TEXT,
    motion_prompt_used     TEXT,
    status                 TEXT NOT NULL DEFAULT 'frame_ready',
    aspect_ratio           TEXT,
    style                  TEXT,
    duration_sec           INTEGER,
    with_audio             BOOLEAN,
    veo_model              TEXT,
    veo_operation_name     TEXT,
    gcs_uri                TEXT,
    frame_storage_bucket   TEXT,
    frame_storage_path     TEXT,
    frame_local_path       TEXT,
    video_storage_bucket   TEXT,
    video_storage_path     TEXT,
    video_local_path       TEXT,
    mime_type              TEXT,
    bytes                  INTEGER,
    frame_cost_usd         NUMERIC,
    veo_cost_usd           NUMERIC,
    error                  TEXT,
    saved_asset_id         BIGINT,
    created_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at             TIMESTAMPTZ NOT NULL DEFAULT now()
)
"""

_COLUMNS: tuple[str, ...] = (
    "prompt", "image_prompt_used", "motion_prompt_used", "status",
    "aspect_ratio", "style", "duration_sec", "with_audio", "veo_model",
    "veo_operation_name", "gcs_uri",
    "frame_storage_bucket", "frame_storage_path", "frame_local_path",
    "video_storage_bucket", "video_storage_path", "video_local_path",
    "mime_type", "bytes", "frame_cost_usd", "veo_cost_usd", "error",
    "saved_asset_id",
)

_table_ready = False
_table_lock = asyncio.Lock()


async def _ensure_table() -> None:
    global _table_ready
    if _table_ready:
        return
    async with _table_lock:
        if _table_ready:
            return
        async with get_conn() as conn:
            async with conn.cursor() as cur:
                await cur.execute(_CREATE_TABLE)
                # Columns added after the first release; backfill existing tables.
                await cur.execute(
                    "ALTER TABLE yas_animation_generations "
                    "ADD COLUMN IF NOT EXISTS saved_asset_id BIGINT"
                )
        _table_ready = True


def _row_out(row: Optional[dict]) -> Optional[dict]:
    if row is None:
        return None
    out = dict(row)
    for k, v in list(out.items()):
        if isinstance(v, datetime):
            out[k] = v.isoformat()
        elif isinstance(v, Decimal):
            out[k] = float(v)
    return out


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

async def create(**fields: Any) -> dict:
    """Insert a new animation record. Assigns id + timestamps. Returns a copy."""
    await _ensure_table()
    cols = list(_COLUMNS)
    vals: list[Any] = []
    for c in cols:
        if c == "status":
            vals.append(fields.get("status", "frame_ready"))
        else:
            vals.append(fields.get(c))
    placeholders = ", ".join(["%s"] * len(cols))
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                f"INSERT INTO yas_animation_generations ({', '.join(cols)}) "
                f"VALUES ({placeholders}) RETURNING *",
                vals,
            )
            row = await cur.fetchone()
    return _row_out(row) or {}


async def update(anim_id: int, **fields: Any) -> Optional[dict]:
    """Patch a record's fields + bump updated_at. Returns the updated copy or None."""
    await _ensure_table()
    sets: list[str] = []
    vals: list[Any] = []
    for c in _COLUMNS:
        if c in fields:
            sets.append(f"{c} = %s")
            vals.append(fields[c])
    if not sets:
        return await get(anim_id)
    sets.append("updated_at = now()")
    vals.append(int(anim_id))
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                f"UPDATE yas_animation_generations SET {', '.join(sets)} "
                f"WHERE id = %s RETURNING *",
                vals,
            )
            row = await cur.fetchone()
    return _row_out(row)


async def get(anim_id: int) -> Optional[dict]:
    await _ensure_table()
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT * FROM yas_animation_generations WHERE id = %s", (int(anim_id),)
            )
            row = await cur.fetchone()
    return _row_out(row)


async def delete(anim_id: int) -> Optional[dict]:
    """Hard-delete an animation record. Returns the deleted row (so the caller can
    clean up its Storage objects) or None if no such id existed. The frame + video
    bytes in Supabase Storage are NOT touched here — that is the caller's job (only
    the router knows the bucket layout and the best-effort delete policy)."""
    await _ensure_table()
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "DELETE FROM yas_animation_generations WHERE id = %s RETURNING *",
                (int(anim_id),),
            )
            row = await cur.fetchone()
    return _row_out(row)


async def pending() -> list[dict]:
    """Animations awaiting Veo completion — used by the local poller."""
    await _ensure_table()
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT * FROM yas_animation_generations "
                "WHERE status = 'animating' AND veo_operation_name IS NOT NULL"
            )
            rows = await cur.fetchall()
    return [r for r in (_row_out(r) for r in rows) if r is not None]


async def recent(
    *,
    within_hours: int = 24,
    limit: int = 50,
    statuses: Optional[Iterable[str]] = None,
) -> list[dict]:
    """Most-recent records (newest first), filtered by status/age."""
    await _ensure_table()
    where = ["created_at >= now() - make_interval(hours => %s)"]
    params: list[Any] = [int(within_hours)]
    if statuses is not None:
        where.append("status = ANY(%s)")
        params.append(list(statuses))
    params.append(int(limit))
    # autocommit: read-only. Saves the BEGIN + COMMIT round trips (see db.py's
    # get_conn docstring) — material when the pooler is in another region.
    async with get_conn(autocommit=True) as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                f"SELECT * FROM yas_animation_generations WHERE {' AND '.join(where)} "
                f"ORDER BY created_at DESC LIMIT %s",
                params,
            )
            rows = await cur.fetchall()
    return [r for r in (_row_out(r) for r in rows) if r is not None]
