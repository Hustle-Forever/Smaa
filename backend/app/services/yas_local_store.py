"""
Postgres-backed persistence for the Yas "Create Poster" (poster-only) pipeline.

Generation records live in the `yas_poster_generations` table (Supabase
Postgres). The image bytes live in Supabase Storage (the `producer-media`
bucket), referenced by `storage_bucket` + `storage_path` on each row — with a
`local_path` fallback only when Supabase isn't configured (asset_storage's
graceful degrade). Nothing here touches the old `yas-output/` disk store.

Public API is unchanged for callers: create / update / get / recent. The table
is created lazily once per process (CREATE TABLE IF NOT EXISTS).
"""
from __future__ import annotations

import asyncio
from datetime import datetime
from decimal import Decimal
from typing import Any, Iterable, Optional

from ..db import get_conn

# ---------------------------------------------------------------------------
# Schema (created lazily, idempotently — mirrors the other CREATE-IF-NOT-EXISTS
# stores like scheduled_instagram_posts / edit_recipes).
# ---------------------------------------------------------------------------

_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS yas_poster_generations (
    id                     SERIAL PRIMARY KEY,
    topic                  TEXT,
    status                 TEXT NOT NULL DEFAULT 'generating',
    parent_generation_id   INTEGER,
    feedback_text          TEXT,
    retry_kind             TEXT,
    prompt_used            TEXT,
    mime_type              TEXT,
    bytes                  INTEGER,
    width                  INTEGER,
    height                 INTEGER,
    cost_usd               NUMERIC,
    error                  TEXT,
    mode                   TEXT,
    reference_asset_ids    INTEGER[] NOT NULL DEFAULT '{}',
    reference_upload_ids   INTEGER[] NOT NULL DEFAULT '{}',
    saved_asset_id         INTEGER,
    variant_set_id         INTEGER,
    concept                TEXT,
    variation_of           INTEGER,
    image_usage            TEXT,
    image_model            TEXT,
    style                  TEXT,
    storage_bucket         TEXT,
    storage_path           TEXT,
    local_path             TEXT,
    created_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at             TIMESTAMPTZ NOT NULL DEFAULT now()
)
"""

# Columns settable via create()/update(). id + created_at/updated_at are managed
# here, never passed in.
_COLUMNS: tuple[str, ...] = (
    "topic", "status", "parent_generation_id", "feedback_text", "retry_kind",
    "prompt_used", "mime_type", "bytes", "width", "height", "cost_usd", "error",
    "mode", "reference_asset_ids", "reference_upload_ids", "saved_asset_id",
    "variant_set_id", "concept", "variation_of", "image_usage", "image_model",
    "style", "storage_bucket", "storage_path", "local_path",
)
_ARRAY_COLS = {"reference_asset_ids", "reference_upload_ids"}

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
        _table_ready = True


def _row_out(row: Optional[dict]) -> Optional[dict]:
    """Normalise a DB row to the JSON-friendly dict shape callers expect:
    datetimes → ISO strings, Decimal → float (matches the legacy JSON store)."""
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
    """Insert a new generation record. Assigns id + timestamps. Returns a copy."""
    await _ensure_table()
    cols = list(_COLUMNS)
    vals: list[Any] = []
    for c in cols:
        if c in _ARRAY_COLS:
            vals.append(list(fields.get(c) or []))
        elif c == "status":
            vals.append(fields.get("status", "generating"))
        else:
            vals.append(fields.get(c))
    placeholders = ", ".join(["%s"] * len(cols))
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                f"INSERT INTO yas_poster_generations ({', '.join(cols)}) "
                f"VALUES ({placeholders}) RETURNING *",
                vals,
            )
            row = await cur.fetchone()
    return _row_out(row) or {}


async def update(gen_id: int, **fields: Any) -> Optional[dict]:
    """Patch a record's fields + bump updated_at. Returns the updated copy or None."""
    await _ensure_table()
    sets: list[str] = []
    vals: list[Any] = []
    for c in _COLUMNS:
        if c in fields:
            v = fields[c]
            if c in _ARRAY_COLS and v is not None:
                v = list(v)
            sets.append(f"{c} = %s")
            vals.append(v)
    if not sets:
        return await get(gen_id)
    sets.append("updated_at = now()")
    vals.append(int(gen_id))
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                f"UPDATE yas_poster_generations SET {', '.join(sets)} "
                f"WHERE id = %s RETURNING *",
                vals,
            )
            row = await cur.fetchone()
    return _row_out(row)


async def get(gen_id: int) -> Optional[dict]:
    await _ensure_table()
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT * FROM yas_poster_generations WHERE id = %s", (int(gen_id),)
            )
            row = await cur.fetchone()
    return _row_out(row)


async def delete(gen_id: int) -> Optional[dict]:
    """Hard-delete a generation record. Returns the deleted row (so the caller can
    clean up its Storage object) or None if no such id existed. The image bytes in
    Supabase Storage are NOT touched here — that is the caller's job, because only
    the router knows the bucket layout and the best-effort delete policy."""
    await _ensure_table()
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "DELETE FROM yas_poster_generations WHERE id = %s RETURNING *",
                (int(gen_id),),
            )
            row = await cur.fetchone()
    return _row_out(row)


async def recent(
    *,
    mode: Optional[str] = None,
    within_hours: int = 24,
    limit: int = 50,
    statuses: Optional[Iterable[str]] = None,
) -> list[dict]:
    """Most-recent records (newest first), filtered by mode/status/age."""
    await _ensure_table()
    where = ["created_at >= now() - make_interval(hours => %s)"]
    params: list[Any] = [int(within_hours)]
    if mode is not None:
        where.append("mode = %s")
        params.append(mode)
    if statuses is not None:
        where.append("status = ANY(%s)")
        params.append(list(statuses))
    params.append(int(limit))
    # autocommit: read-only. Saves the BEGIN + COMMIT round trips (see db.py's
    # get_conn docstring) — material when the pooler is in another region.
    async with get_conn(autocommit=True) as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                f"SELECT * FROM yas_poster_generations WHERE {' AND '.join(where)} "
                f"ORDER BY created_at DESC LIMIT %s",
                params,
            )
            rows = await cur.fetchall()
    return [r for r in (_row_out(r) for r in rows) if r is not None]
