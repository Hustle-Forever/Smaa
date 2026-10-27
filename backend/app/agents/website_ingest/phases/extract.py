"""Phase 3 — extract body text + asset rows from raw_html.

For each pending page (raw_html populated, extracted_text NULL):
  1. Run trafilatura/BS4 to extract body text → website_pages.extracted_text
  2. Walk the HTML for <img>, <video>, <iframe>, <a href=pdf>, bare YT/Vimeo
     URLs → assets table (ON CONFLICT DO NOTHING per url_hash)

Idempotent. No AI calls.
"""
from __future__ import annotations

import asyncio
import logging

from psycopg.types.json import Jsonb

from ....db import get_conn
from ....utils.hashing import url_hash
from ....utils.html import extract_body_text, parse_assets_from_html
from ..types import CancellationError, PhaseContext, PhaseResult

log = logging.getLogger("archivist.phase.extract")


async def _select_pending() -> list[dict]:
    sql = (
        "SELECT id, url, raw_html FROM website_pages "
        " WHERE raw_html IS NOT NULL AND extracted_text IS NULL "
        " ORDER BY id ASC"
    )
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(sql)
            return await cur.fetchall()


def _extract_sync(html: str, base_url: str) -> tuple[str, list]:
    """Run trafilatura + asset walk off the event loop."""
    text = extract_body_text(html)
    assets = parse_assets_from_html(html, base_url=base_url)
    return text, assets


async def run(ctx: PhaseContext) -> PhaseResult:
    pages = await _select_pending()
    if not pages:
        log.info("[run=%s] Extract: nothing pending", ctx.run_id)
        return PhaseResult(name="extract", total=0)

    log.info("[run=%s] Extract: %d pages", ctx.run_id, len(pages))
    processed = 0
    failed = 0
    total_assets = 0

    for i, row in enumerate(pages, 1):
        if ctx.is_cancelled and ctx.is_cancelled():
            raise CancellationError("extract cancelled mid-batch")

        pid, url, html = row["id"], row["url"], row["raw_html"]
        try:
            text, assets = await asyncio.to_thread(_extract_sync, html, url)
            async with get_conn() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(
                        "UPDATE website_pages SET extracted_text=%s WHERE id=%s",
                        (text, pid),
                    )
                # Insert assets
                inserted_here = 0
                for a in assets:
                    uh = url_hash(a.canonical_url)
                    async with conn.cursor() as cur:
                        await cur.execute(
                            """
                            INSERT INTO assets
                              (page_id, asset_type, source_url, canonical_url, url_hash, metadata)
                            VALUES (%s, %s, %s, %s, %s, %s)
                            ON CONFLICT (url_hash) DO NOTHING
                            RETURNING id
                            """,
                            (pid, a.asset_type, a.source_url, a.canonical_url, uh, Jsonb(a.metadata)),
                        )
                        if await cur.fetchone():
                            inserted_here += 1
                total_assets += inserted_here
            processed += 1
            log.info(
                "[run=%s] [%d/%d] page #%d → %d chars text, %d new assets",
                ctx.run_id, i, len(pages), pid, len(text), inserted_here,
            )
        except Exception:
            failed += 1
            log.exception("Extract failed for page #%s (%s)", pid, url)

        if ctx.report_progress:
            ctx.report_progress("extract", processed + failed, len(pages))

    log.info(
        "[run=%s] Extract done: %d ok, %d failed, %d new assets",
        ctx.run_id, processed, failed, total_assets,
    )
    return PhaseResult(
        name="extract", processed=processed, failed=failed, total=len(pages),
        extra={"new_assets": total_assets},
    )
