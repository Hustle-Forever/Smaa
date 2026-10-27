"""
Postgres-backed persistence for the Yas "General AI Video" tool.

A sibling of yas_animation_store.py (kept in its OWN table,
`yas_general_video_generations`, so general-video records never mix with the
branded Create-Animation records). It is the clean Veo generator behind the
Ideation "General video → General AI Video" tool: the user gives a prompt +
reference pictures, we render an UNBRANDED still (no 42 logo), then animate it to
a video. Frame + MP4 bytes live in Supabase Storage (the `producer-media`
bucket), referenced by frame_/video_ storage columns (with a *_local_path
fallback).

Status flow: frame_ready -> animating -> ready   (or `failed` at any step).
The frame is rendered synchronously inside the request, so a record is born
`frame_ready`; the video step is async and finished by yas_general_video_poller.

The video step runs on one of two engines, recorded in `engine`: "veo" (Vertex,
the default; veo_* columns) or "seedance" (Seedance 2.5 via Higgsfield;
seedance_* columns plus `resolution`).

Public API mirrors yas_animation_store: create / update / get / recent / pending,
plus claim_for_animation (taken before any paid call, so one still can never
start two jobs) and the Seedance spend ledger behind SEEDANCE_BUDGET_USD.
The tables self-create via CREATE TABLE IF NOT EXISTS — no manual migration.
"""
from __future__ import annotations

import asyncio
from datetime import datetime
from decimal import Decimal
from typing import Any, Iterable, Optional

from ..db import get_conn

_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS yas_general_video_generations (
    id                     SERIAL PRIMARY KEY,
    prompt                 TEXT,
    image_prompt_used      TEXT,
    motion_prompt_used     TEXT,
    status                 TEXT NOT NULL DEFAULT 'frame_ready',
    aspect_ratio           TEXT,
    duration_sec           INTEGER,
    with_audio             BOOLEAN,
    reference_upload_ids   TEXT,
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
    created_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at             TIMESTAMPTZ NOT NULL DEFAULT now()
)
"""

# Columns added after the table first shipped. Additive and nullable, so an
# older row (always Veo) reads as engine NULL, which every caller treats as veo.
_ADD_COLUMNS = (
    "ALTER TABLE yas_general_video_generations ADD COLUMN IF NOT EXISTS engine TEXT",
    "ALTER TABLE yas_general_video_generations ADD COLUMN IF NOT EXISTS resolution TEXT",
    "ALTER TABLE yas_general_video_generations ADD COLUMN IF NOT EXISTS seedance_request_id TEXT",
    "ALTER TABLE yas_general_video_generations ADD COLUMN IF NOT EXISTS seedance_status_url TEXT",
    "ALTER TABLE yas_general_video_generations ADD COLUMN IF NOT EXISTS seedance_cost_usd NUMERIC",
    "ALTER TABLE yas_general_video_generations ADD COLUMN IF NOT EXISTS seedance_submitted_at TIMESTAMPTZ",
)

# One row per Seedance submission, never per clip: re-animating a clip adds a
# row and leaves the earlier ones alone, so money already spent stays counted.
# A row is written BEFORE anything is paid (a reservation) and gets its
# request_id once Higgsfield accepts; `written_off` marks what Higgsfield did
# not charge (a refused submit, or a job that ended failed / nsfw / canceled).
_CREATE_SPEND_TABLE = """
CREATE TABLE IF NOT EXISTS yas_seedance_spend (
    id           SERIAL PRIMARY KEY,
    gen_id       INTEGER,
    request_id   TEXT,
    cost_usd     NUMERIC NOT NULL,
    written_off  BOOLEAN NOT NULL DEFAULT false,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
)
"""

# Any constant: a transaction-scoped advisory lock serialises the budget check
# and the reservation across every process sharing the database.
_SEEDANCE_BUDGET_LOCK = 4242_0717

_COLUMNS: tuple[str, ...] = (
    "prompt", "image_prompt_used", "motion_prompt_used", "status",
    "aspect_ratio", "duration_sec", "with_audio", "reference_upload_ids",
    "veo_model", "veo_operation_name", "gcs_uri",
    "frame_storage_bucket", "frame_storage_path", "frame_local_path",
    "video_storage_bucket", "video_storage_path", "video_local_path",
    "mime_type", "bytes", "frame_cost_usd", "veo_cost_usd", "error",
    "engine", "resolution", "seedance_request_id", "seedance_status_url",
    "seedance_cost_usd", "seedance_submitted_at",
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
                for stmt in _ADD_COLUMNS:
                    await cur.execute(stmt)
                await cur.execute(_CREATE_SPEND_TABLE)
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
    """Insert a new general-video record. Assigns id + timestamps. Returns a copy."""
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
                f"INSERT INTO yas_general_video_generations ({', '.join(cols)}) "
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
            sets.append(f"{c} = %s")
            vals.append(fields[c])
    if not sets:
        return await get(gen_id)
    sets.append("updated_at = now()")
    vals.append(int(gen_id))
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                f"UPDATE yas_general_video_generations SET {', '.join(sets)} "
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
                "SELECT * FROM yas_general_video_generations WHERE id = %s", (int(gen_id),)
            )
            row = await cur.fetchone()
    return _row_out(row)


async def pending() -> list[dict]:
    """General-video clips awaiting Veo or Seedance completion — used by the poller."""
    await _ensure_table()
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT * FROM yas_general_video_generations "
                "WHERE status = 'animating' "
                "  AND (veo_operation_name IS NOT NULL OR seedance_status_url IS NOT NULL)"
            )
            rows = await cur.fetchall()
    return [r for r in (_row_out(r) for r in rows) if r is not None]


async def claim_for_animation(
    gen_id: int, engine: str,
    from_statuses: Iterable[str] = ("frame_ready", "ready", "failed"),
) -> Optional[dict]:
    """Mark a still as animating before anything is paid, in one conditional
    UPDATE, so a second click, a second tab or a refresh mid-submit cannot start
    a second job for it. Only a still in one of `from_statuses` is claimed (the
    caller passes the status its page showed). Returns the claimed row, or None.
    The previous job's handles are cleared so the poller cannot finish the OLD
    job while the new one is submitted."""
    await _ensure_table()
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "UPDATE yas_general_video_generations "
                "SET status = 'animating', engine = %s, error = NULL, "
                "    veo_operation_name = NULL, seedance_request_id = NULL, "
                "    seedance_status_url = NULL, updated_at = now() "
                "WHERE id = %s AND status = ANY(%s) "
                "RETURNING *",
                (engine, int(gen_id), list(from_statuses)),
            )
            row = await cur.fetchone()
    return _row_out(row)


async def fail_stale_claims(*, older_than_minutes: int = 15) -> int:
    """A claim whose request died before its job was recorded (a restart
    mid-submit) holds no job handle, so nothing would ever finish it. Fail it so
    the card stops spinning. A live submit finishes well inside the window. The
    engine may still have taken the job, and the message says so."""
    await _ensure_table()
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "UPDATE yas_general_video_generations "
                "SET status = 'failed', error = %s, updated_at = now() "
                "WHERE status = 'animating' "
                "  AND veo_operation_name IS NULL AND seedance_status_url IS NULL "
                "  AND updated_at < now() - make_interval(mins => %s)",
                ("The video request was interrupted before it was confirmed. It may still "
                 "have reached the engine: check the provider's dashboard before trying again.",
                 int(older_than_minutes)),
            )
            return cur.rowcount


async def reserve_seedance_spend(
    gen_id: int, cost_usd: float, *, cap_usd: float, within_days: int = 30,
) -> tuple[Optional[int], float]:
    """Check the rolling Seedance budget and, when the clip fits, record its
    cost before anything is paid. One transaction under an advisory lock, so two
    clips submitted at the same moment cannot both squeeze under the cap.

    Returns (reservation id, spend in the window before this clip). The id is
    None when the clip would take the window past `cap_usd` (0 = no cap)."""
    await _ensure_table()
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute("SELECT pg_advisory_xact_lock(%s)", (_SEEDANCE_BUDGET_LOCK,))
            await cur.execute(
                "SELECT COALESCE(SUM(cost_usd), 0) AS usd FROM yas_seedance_spend "
                "WHERE NOT written_off AND created_at >= now() - make_interval(days => %s)",
                (int(within_days),),
            )
            spent = float(((await cur.fetchone()) or {}).get("usd") or 0)
            if cap_usd > 0 and spent + cost_usd > cap_usd:
                return None, spent
            await cur.execute(
                "INSERT INTO yas_seedance_spend (gen_id, cost_usd) VALUES (%s, %s) RETURNING id",
                (int(gen_id), cost_usd),
            )
            row = await cur.fetchone()
    return int(row["id"]), spent


async def attach_seedance_request(spend_id: int, request_id: str) -> None:
    """Tie a reservation to the job Higgsfield accepted."""
    await _ensure_table()
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "UPDATE yas_seedance_spend SET request_id = %s WHERE id = %s",
                (request_id, int(spend_id)),
            )


async def write_off_seedance_spend(
    *, spend_id: Optional[int] = None, request_id: Optional[str] = None,
) -> None:
    """Stop counting a submission Higgsfield did not charge: a submit it refused
    (by reservation id) or a job that ended failed / nsfw / canceled (by its
    request id)."""
    if spend_id is None and not request_id:
        return
    await _ensure_table()
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            if spend_id is not None:
                await cur.execute(
                    "UPDATE yas_seedance_spend SET written_off = true WHERE id = %s",
                    (int(spend_id),),
                )
            else:
                await cur.execute(
                    "UPDATE yas_seedance_spend SET written_off = true WHERE request_id = %s",
                    (request_id,),
                )


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
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                f"SELECT * FROM yas_general_video_generations WHERE {' AND '.join(where)} "
                f"ORDER BY created_at DESC LIMIT %s",
                params,
            )
            rows = await cur.fetchall()
    return [r for r in (_row_out(r) for r in rows) if r is not None]


async def clear_session(*, within_hours: int = 24) -> int:
    """Archive every general-video record from the recent window so the session
    starts clean. Sets ``status='archived'`` — a status OUTSIDE the allow-list
    used by ``recent()`` and ``pending()``, so archived rows simply drop out of
    the history strip and the poller (any in-flight Veo render is abandoned). No
    rows are deleted; the underlying media is left in storage. Returns the count
    of rows archived."""
    await _ensure_table()
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "UPDATE yas_general_video_generations "
                "SET status = 'archived', updated_at = now() "
                "WHERE status <> 'archived' "
                "  AND created_at >= now() - make_interval(hours => %s)",
                (int(within_hours),),
            )
            return cur.rowcount
