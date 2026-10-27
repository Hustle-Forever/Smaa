"""Phase 6 — embed text_chunks + page summaries with OpenAI text-embedding-3-small.

Two passes:
  1. text_chunks where embedding IS NULL  → fill embedding (batch up to 100)
  2. website_pages where ai_summary IS NOT NULL AND embedding IS NULL → fill
     summary embedding, set embedded_status='completed', last_embedded_at

Idempotent. Batched 100 at a time so a single 429 doesn't lose much work.
"""
from __future__ import annotations

import logging

from ....db import get_conn
from ....services.openai_client import embed_batch
from ..types import CancellationError, PhaseContext, PhaseResult

log = logging.getLogger("archivist.phase.embed_text")

BATCH = 100


async def _select_chunks() -> list[dict]:
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT id, chunk_text FROM text_chunks WHERE embedding IS NULL ORDER BY id ASC"
            )
            return await cur.fetchall()


async def _select_summaries() -> list[dict]:
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT id, ai_summary FROM website_pages "
                " WHERE ai_summary IS NOT NULL AND embedding IS NULL "
                " ORDER BY id ASC"
            )
            return await cur.fetchall()


async def _embed_and_update(rows: list[dict], *, text_field: str, update_sql: str, ctx: PhaseContext, label: str) -> int:
    embedded = 0
    for start in range(0, len(rows), BATCH):
        if ctx.is_cancelled and ctx.is_cancelled():
            raise CancellationError(f"{label} embed cancelled mid-batch")
        batch = rows[start:start + BATCH]
        texts = [(r[text_field] or "") for r in batch]
        ids = [r["id"] for r in batch]
        log.info("[run=%s] Embedding %s batch [%d:%d]", ctx.run_id, label, start, start + len(batch))
        vectors = await embed_batch(texts)
        if len(vectors) != len(ids):
            raise RuntimeError(
                f"Embedding count mismatch: got {len(vectors)} for {len(ids)} {label}"
            )
        async with get_conn() as conn:
            async with conn.cursor() as cur:
                for rid, vec in zip(ids, vectors):
                    await cur.execute(update_sql, (vec, rid))
        embedded += len(batch)
        if ctx.report_progress:
            ctx.report_progress(f"embed_text:{label}", embedded, len(rows))
    return embedded


async def run(ctx: PhaseContext) -> PhaseResult:
    chunks = await _select_chunks()
    summaries = await _select_summaries()
    log.info(
        "[run=%s] Embed text: %d chunks, %d summaries",
        ctx.run_id, len(chunks), len(summaries),
    )

    n_chunks = 0
    n_summaries = 0

    if chunks:
        n_chunks = await _embed_and_update(
            chunks,
            text_field="chunk_text",
            update_sql="UPDATE text_chunks SET embedding=%s WHERE id=%s",
            ctx=ctx, label="chunks",
        )

    if summaries:
        n_summaries = await _embed_and_update(
            summaries,
            text_field="ai_summary",
            update_sql=(
                "UPDATE website_pages "
                "   SET embedding=%s, embedded_status='completed', last_embedded_at=NOW() "
                " WHERE id=%s"
            ),
            ctx=ctx, label="summaries",
        )

    log.info(
        "[run=%s] Embed text done: %d chunks, %d summaries",
        ctx.run_id, n_chunks, n_summaries,
    )
    return PhaseResult(
        name="embed_text",
        processed=n_chunks + n_summaries,
        total=len(chunks) + len(summaries),
        extra={"chunks_embedded": n_chunks, "summaries_embedded": n_summaries},
    )
