"""
Per-project Producer Roadmap progress store.

The Producer Roadmap (Ideation → Editing → Finalization → Scheduling → Approval)
used to keep its progress only in frontend React state — a single in-session
slot, so leaving lost it and a second project clobbered the first. This store
persists each project's roadmap position keyed by ``(tool, ref_id)`` (the
underlying run/generation), so any project can be left and resumed FROM THE
LIBRARY at the exact step it was on, and starting another project never
overwrites a different one.

A row exists only while a project is mid-roadmap (post-finalize, pre-publish);
it's deleted on publish/approval. The table is created idempotently
(``CREATE TABLE IF NOT EXISTS``) the first time the store is used, so no separate
migration step is required.

    state  = {asset: FinalizedAsset, draftCaption: str}   -- frontend-owned blob
    step   = producer | editor | preview | calendar | approval
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from psycopg.types.json import Jsonb

from ..db import get_conn

log = logging.getLogger("archivist.roadmap_state")

_DDL = """
CREATE TABLE IF NOT EXISTS producer_roadmap (
    tool       TEXT        NOT NULL,
    ref_id     BIGINT      NOT NULL,
    step       TEXT        NOT NULL,
    state      JSONB       NOT NULL DEFAULT '{}'::jsonb,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tool, ref_id)
);
"""

_ensured = False


async def ensure_table() -> None:
    """Create the table if missing (idempotent). Runs once per process."""
    global _ensured
    if _ensured:
        return
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(_DDL)
    _ensured = True
    log.info("producer_roadmap table ensured")


async def save(tool: str, ref_id: int, step: str, state: dict | None = None) -> dict:
    """Upsert the roadmap position for one project. Idempotent per (tool, ref_id)."""
    await ensure_table()
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                INSERT INTO producer_roadmap (tool, ref_id, step, state, updated_at)
                VALUES (%s, %s, %s, %s, now())
                ON CONFLICT (tool, ref_id) DO UPDATE
                   SET step = EXCLUDED.step,
                       state = EXCLUDED.state,
                       updated_at = now()
                RETURNING tool, ref_id, step, state, updated_at
                """,
                (tool, int(ref_id), step, Jsonb(state or {})),
            )
            return await cur.fetchone()


async def get(tool: str, ref_id: int) -> Optional[dict]:
    """The saved roadmap position for one project, or None if there isn't one."""
    await ensure_table()
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT tool, ref_id, step, state, updated_at
                  FROM producer_roadmap
                 WHERE tool = %s AND ref_id = %s
                """,
                (tool, int(ref_id)),
            )
            return await cur.fetchone()


async def delete(tool: str, ref_id: int) -> None:
    """Drop a project's roadmap row (on publish/approval, or explicit discard)."""
    await ensure_table()
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "DELETE FROM producer_roadmap WHERE tool = %s AND ref_id = %s",
                (tool, int(ref_id)),
            )


async def all_active() -> list[dict]:
    """Every mid-roadmap project as {tool, ref_id, step} — for annotating the
    Library list so each tile can show a 'Continue → <Step>' action."""
    await ensure_table()
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute("SELECT tool, ref_id, step FROM producer_roadmap")
            rows = await cur.fetchall()
    return [dict(r) for r in rows]


if __name__ == "__main__":  # python -m app.services.roadmap_state → create the table
    import asyncio
    from ..db import open_pool

    async def _main() -> None:
        await open_pool()
        await ensure_table()
        print("producer_roadmap table ready")

    asyncio.run(_main())
