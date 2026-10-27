"""Built reports, kept.

WHY THIS EXISTS. Every call to `/api/reports/social-listening` used to assemble
the document from scratch, and the Reports tab fired two of them on mount and
again on every window change. So opening the tab was a paid rebuild: the
measured half is a few seconds, but the full report costs two model passes, and
one contended Vertex call has been measured at 113 s. Nothing was stored, so
nothing could be reused, and the backend had no way to know a report for that
window already existed because there was no record that it ever had.

THE KEY IS THE DOCUMENT, NOT THE CLICK. A report is identified by what it is
about, not by when someone asked for it: (scope, mode, days, window_to). The
windows are trailing, so `window_to` is the day it was asked for, and asking
twice on the same day for the same range is the same document. Tomorrow's is a
different one. That is the whole rule, and it is what makes "I already generated
this" answerable.

THE WHOLE ENVELOPE IS STORED, not a summary of it. `payload` is the exact
`{meta, basis, sections}` dict the builder returned, so the PDF renders from the
same bytes the screen read. A stored report cannot drift from its own download,
because there is only one of it.

CREATE TABLE IF NOT EXISTS, like every other lazily-created store in this
codebase (`yas_local_store`, `scheduled_instagram_posts`, `edit_recipes`). One
process-wide guard, one idempotent statement, no migration to forget to run.
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import date, datetime
from typing import Any, Optional

from ..db import get_conn

log = logging.getLogger("archivist.service.social_report_store")

_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS social_reports (
    id            SERIAL PRIMARY KEY,
    scope         TEXT NOT NULL,
    mode          TEXT NOT NULL,
    days          INTEGER NOT NULL,
    window_from   DATE,
    window_to     DATE NOT NULL,
    generated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    payload       JSONB NOT NULL
)
"""

# What makes two requests the same document. A partial index is not needed: the
# four columns are always present, and the uniqueness is the point — a second
# generate for the same day/range/scope UPDATEs the row rather than growing a
# pile of near-identical reports.
_CREATE_UNIQUE = """
CREATE UNIQUE INDEX IF NOT EXISTS social_reports_identity
    ON social_reports (scope, mode, days, window_to)
"""

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
                await cur.execute(_CREATE_UNIQUE)
        _table_ready = True


def _row_out(row: Optional[dict]) -> Optional[dict]:
    """A DB row as the JSON-friendly dict the router hands to the client.

    `payload` stays nested rather than being merged up: the caller asks for
    either the index entry or the document, and flattening them into one shape
    would make "list the reports" ship every full envelope."""
    if row is None:
        return None
    out = dict(row)
    for k, v in list(out.items()):
        if isinstance(v, (datetime, date)):
            out[k] = v.isoformat()
    payload = out.get("payload")
    if isinstance(payload, str):          # psycopg may hand JSONB back as text
        try:
            out["payload"] = json.loads(payload)
        except Exception:                  # noqa: BLE001 - a corrupt row is not fatal
            out["payload"] = None
    return out


def _window_dates(report: dict) -> tuple[Optional[str], Optional[str]]:
    """The window the BUILDER measured, off the report's own meta.

    Taken from the document rather than computed from `days` here, so the stored
    key can never disagree with the dates printed on the page."""
    meta = report.get("meta") if isinstance(report, dict) else None
    window = (meta or {}).get("window") if isinstance(meta, dict) else None
    if not isinstance(window, dict):
        return None, None

    def _day(value: Any) -> Optional[str]:
        text = str(value or "").strip()
        return text[:10] or None        # ISO 8601 → the date half

    return _day(window.get("from")), _day(window.get("to"))


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

async def find(scope: str, mode: str, days: int, window_to: str) -> Optional[dict]:
    """The stored report for exactly this document, or None."""
    await _ensure_table()
    async with get_conn(autocommit=True) as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT * FROM social_reports "
                "WHERE scope = %s AND mode = %s AND days = %s AND window_to = %s",
                (scope, mode, int(days), window_to),
            )
            return _row_out(await cur.fetchone())


async def save(scope: str, mode: str, days: int, report: dict) -> Optional[dict]:
    """Store a freshly built report, replacing the same day's if there is one.

    A regenerate is an UPDATE, not a second row: the identity index says these
    are the same document, and keeping both would leave the tab having to
    explain which of two identical-looking reports it is showing.

    Never raises. A report that cannot be stored is still a report the caller
    just built and can return — losing the cache is worth less than losing the
    document, so a storage failure is logged and swallowed."""
    window_from, window_to = _window_dates(report)
    if not window_to:
        log.warning("[reports] not storing: the report carries no window end")
        return None
    try:
        await _ensure_table()
        async with get_conn() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "INSERT INTO social_reports "
                    "(scope, mode, days, window_from, window_to, payload) "
                    "VALUES (%s, %s, %s, %s, %s, %s) "
                    "ON CONFLICT (scope, mode, days, window_to) DO UPDATE SET "
                    "payload = EXCLUDED.payload, "
                    "window_from = EXCLUDED.window_from, "
                    "generated_at = now() "
                    "RETURNING *",
                    (scope, mode, int(days), window_from, window_to,
                     json.dumps(report)),
                )
                return _row_out(await cur.fetchone())
    except Exception as exc:              # noqa: BLE001
        log.warning("[reports] could not store the report: %s", exc)
        return None


async def index(scope: Optional[str] = None, limit: int = 40) -> list[dict]:
    """Newest first, WITHOUT the payloads.

    This is what the tab loads on open, so it must stay cheap: the envelopes are
    tens of kilobytes each and nothing on that screen reads them until a report
    is actually opened."""
    await _ensure_table()
    sql = (
        "SELECT id, scope, mode, days, window_from, window_to, generated_at "
        "FROM social_reports "
    )
    params: list[Any] = []
    if scope:
        sql += "WHERE scope = %s "
        params.append(scope)
    sql += "ORDER BY generated_at DESC LIMIT %s"
    params.append(int(limit))
    async with get_conn(autocommit=True) as conn:
        async with conn.cursor() as cur:
            await cur.execute(sql, params)
            rows = await cur.fetchall()
    return [r for r in (_row_out(row) for row in rows) if r]


async def delete(report_id: int) -> bool:
    """Forget one stored report. True when a row was actually removed.

    The table outlived two changes to what a window may be, so it holds
    documents whose dates match nothing on offer any more. A row like that
    reads as something random that got downloaded by accident, and the only
    honest answer is to let the reader throw it away. Deleting a report costs
    nothing that cannot be rebuilt."""
    await _ensure_table()
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "DELETE FROM social_reports WHERE id = %s", (int(report_id),)
            )
            return cur.rowcount > 0


async def by_id(report_id: int) -> Optional[dict]:
    """One stored report, payload included. The download reads this."""
    await _ensure_table()
    async with get_conn(autocommit=True) as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT * FROM social_reports WHERE id = %s", (int(report_id),)
            )
            return _row_out(await cur.fetchone())
