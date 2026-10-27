"""Phase 4 — sliding-window token chunks per page.

For each page with extracted_text and zero text_chunks rows, produce
text_chunks via tiktoken sliding window (CHUNK_TOKENS + CHUNK_OVERLAP).
Idempotent: skips pages that already have chunks.
"""
from __future__ import annotations

import asyncio
import logging

from ....config import config
from ....db import get_conn
from ....utils.tokens import sliding_chunks
from ..types import CancellationError, PhaseContext, PhaseResult

log = logging.getLogger("archivist.phase.chunk")


async def _select_pending() -> list[dict]:
    sql = (
        "SELECT p.id, p.url, p.extracted_text "
        "  FROM website_pages p "
        " WHERE p.extracted_text IS NOT NULL "
        "   AND NOT EXISTS (SELECT 1 FROM text_chunks tc WHERE tc.page_id = p.id) "
        " ORDER BY p.id ASC"
    )
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(sql)
            return await cur.fetchall()


async def run(ctx: PhaseContext) -> PhaseResult:
    pages = await _select_pending()
    if not pages:
        log.info("[run=%s] Chunk: nothing pending", ctx.run_id)
        return PhaseResult(name="chunk", total=0)

    log.info(
        "[run=%s] Chunk: %d pages (max_tokens=%d overlap=%d)",
        ctx.run_id, len(pages), config.chunk_tokens, config.chunk_overlap,
    )
    processed = 0
    failed = 0
    total_chunks = 0

    for i, row in enumerate(pages, 1):
        if ctx.is_cancelled and ctx.is_cancelled():
            raise CancellationError("chunk cancelled mid-batch")

        pid, url, text = row["id"], row["url"], row["extracted_text"]
        try:
            chunks = await asyncio.to_thread(
                sliding_chunks, text or "",
                max_tokens=config.chunk_tokens,
                overlap=config.chunk_overlap,
            )
            if chunks:
                async with get_conn() as conn:
                    async with conn.cursor() as cur:
                        await cur.executemany(
                            """
                            INSERT INTO text_chunks (page_id, chunk_index, chunk_text, token_count)
                            VALUES (%s, %s, %s, %s)
                            ON CONFLICT (page_id, chunk_index) DO NOTHING
                            """,
                            [(pid, c.index, c.text, c.token_count) for c in chunks],
                        )
            total_chunks += len(chunks)
            processed += 1
            log.info("[run=%s] [%d/%d] page #%d → %d chunks", ctx.run_id, i, len(pages), pid, len(chunks))
        except Exception:
            failed += 1
            log.exception("Chunk failed for page #%s (%s)", pid, url)

        if ctx.report_progress:
            ctx.report_progress("chunk", processed + failed, len(pages))

    log.info("[run=%s] Chunk done: %d pages, %d chunks", ctx.run_id, processed, total_chunks)
    return PhaseResult(
        name="chunk", processed=processed, failed=failed, total=len(pages),
        extra={"chunks_created": total_chunks},
    )
