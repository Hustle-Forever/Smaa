"""Phase B — summarize + embed processed brand_documents.

For each `brand_documents` row with upload_status='processing':
  - Pick the "body for summarize" — raw_text (for pdf/docx/text/free_text)
    or ai_description (for image).
  - Truncate to ~8k tokens.
  - chat() with a brand-aware system prompt (extended with hints.emphasis_hint).
  - embed_batch() the summary → 1536d vector.
  - UPDATE brand_documents SET ai_summary, embedding, upload_status='completed'.

On row-level failure: upload_status='failed' + metadata.error.
"""
from __future__ import annotations

import asyncio
import logging

from ....db import get_conn
from ....services.openai_client import chat, embed_batch
from ....utils.tokens import truncate_to_tokens
from ...website_ingest.types import CancellationError, PhaseContext, PhaseResult

log = logging.getLogger("archivist.phase.brand_doc.summarize_embed")

SYSTEM_PROMPT = (
    "You summarize owner-uploaded brand documents for the 42 Abu Dhabi brand "
    "archive. Output a 3-6 sentence summary that captures: the document's "
    "purpose, the key brand assertions or facts it conveys, tone of voice, "
    "and what it adds to the brand identity that the public website might miss. "
    "Be specific and concrete. No bullet points, just prose."
)


async def _select_pending() -> list[dict]:
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT id, doc_kind, source_filename, raw_text, ai_description, uploader_note
                  FROM brand_documents
                 WHERE upload_status='processing'
                   AND doc_kind <> 'video'   -- videos owned by brand_video_poller
                 ORDER BY id ASC
                """
            )
            return await cur.fetchall()


def _body_for_summary(row: dict) -> str:
    """The text to summarize: raw_text for most kinds, ai_description for images."""
    if row["doc_kind"] == "image":
        return (row.get("ai_description") or "").strip()
    return (row.get("raw_text") or "").strip()


async def _mark_failed(doc_id: int, reason: str) -> None:
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "UPDATE brand_documents "
                "   SET upload_status='failed', "
                "       metadata=COALESCE(metadata, '{}'::jsonb) || jsonb_build_object('error', %s::text) "
                " WHERE id=%s",
                (reason[:500], doc_id),
            )


async def run(ctx: PhaseContext) -> PhaseResult:
    from ....services.settings_service import Hints
    hints: Hints = ctx.hints  # type: ignore[assignment]

    rows = await _select_pending()
    if not rows:
        log.info("[run=%s] doc-summarize: nothing pending", ctx.run_id)
        return PhaseResult(name="doc_summarize_embed", total=0)

    log.info("[run=%s] doc-summarize: %d docs", ctx.run_id, len(rows))

    system_prompt = SYSTEM_PROMPT
    if hints.emphasis_hint:
        system_prompt = f"{SYSTEM_PROMPT}\n\n{hints.emphasis_hint}"

    sem = asyncio.Semaphore(2)
    processed = 0
    failed = 0
    lock = asyncio.Lock()

    async def worker(row: dict, idx: int) -> None:
        nonlocal processed, failed
        if ctx.is_cancelled and ctx.is_cancelled():
            raise CancellationError("doc-summarize cancelled mid-batch")
        doc_id = row["id"]
        body = _body_for_summary(row)
        if not body:
            log.warning("[doc %d] empty body for summary; marking failed", doc_id)
            await _mark_failed(doc_id, "no body to summarize")
            async with lock:
                failed += 1
            return

        async with sem:
            try:
                note = (row.get("uploader_note") or "").strip()
                user_prompt = (
                    f"Filename: {row.get('source_filename') or '(none)'}\n"
                    f"Kind: {row['doc_kind']}\n"
                    + (f"Uploader note: {note}\n" if note else "")
                    + f"Body:\n{truncate_to_tokens(body, 8000)}"
                )
                summary = await chat(
                    user_prompt, system=system_prompt,
                    max_tokens=400, temperature=0.3,
                )
                if not summary:
                    raise RuntimeError("empty summary from chat()")
                vecs = await embed_batch([summary])
                if not vecs:
                    raise RuntimeError("no embedding returned")
                vec = vecs[0]

                async with get_conn() as conn:
                    async with conn.cursor() as cur:
                        await cur.execute(
                            """
                            UPDATE brand_documents
                               SET ai_summary=%s,
                                   embedding=%s,
                                   upload_status='completed'
                             WHERE id=%s
                            """,
                            (summary, vec, doc_id),
                        )
                async with lock:
                    processed += 1
                log.info(
                    "[run=%s] [%d/%d] doc #%d → %d-char summary",
                    ctx.run_id, idx, len(rows), doc_id, len(summary),
                )
            except Exception as e:
                async with lock:
                    failed += 1
                log.exception("[doc %d] summarize/embed failed", doc_id)
                await _mark_failed(doc_id, str(e))
        if ctx.report_progress:
            ctx.report_progress("doc_summarize_embed", processed + failed, len(rows))

    tasks = [asyncio.create_task(worker(r, i + 1)) for i, r in enumerate(rows)]
    try:
        await asyncio.gather(*tasks)
    except CancellationError:
        for t in tasks:
            if not t.done():
                t.cancel()
        raise

    log.info(
        "[run=%s] doc-summarize done: %d ok, %d failed",
        ctx.run_id, processed, failed,
    )
    return PhaseResult(
        name="doc_summarize_embed",
        processed=processed, failed=failed, total=len(rows),
    )
