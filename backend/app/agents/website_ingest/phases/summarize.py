"""Phase 5 — page summary via gpt-4o-mini.

For each page where extracted_text IS NOT NULL AND ai_analyzed_status='pending':
  - Truncate body to ~8k tokens
  - Send to chat with a brand-aware system prompt (extended with hints.emphasis_hint)
  - Store ai_summary, set ai_analyzed_status='completed', last_analyzed_at

Respects hints.page_processing_cap (manual mode raises this when weight_text high).
"""
from __future__ import annotations

import asyncio
import logging

from ....db import get_conn
from ....services.openai_client import chat
from ....utils.tokens import truncate_to_tokens
from ..types import CancellationError, PhaseContext, PhaseResult

log = logging.getLogger("archivist.phase.summarize")

BASE_SYSTEM_PROMPT = (
    "You summarize web pages for the 42 Abu Dhabi coding school's brand archive. "
    "Output a 3-5 sentence summary that captures: the page's topic, target audience, "
    "tone, and what it tells us about the school's brand identity. "
    "Be specific, concrete, and avoid marketing-speak. No bullet points, just prose."
)


async def _select_pending(limit: int) -> list[dict]:
    sql = (
        "SELECT id, url, page_title, extracted_text FROM website_pages "
        " WHERE extracted_text IS NOT NULL "
        "   AND ai_analyzed_status='pending' "
        " ORDER BY id ASC "
        f"LIMIT {int(limit)}"
    )
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(sql)
            return await cur.fetchall()


async def run(ctx: PhaseContext) -> PhaseResult:
    from ....services.settings_service import Hints
    hints: Hints = ctx.hints  # type: ignore[assignment]

    limit = hints.page_processing_cap
    rows = await _select_pending(limit)
    if not rows:
        log.info("[run=%s] Summarize: nothing pending (cap=%d)", ctx.run_id, limit)
        return PhaseResult(name="summarize", total=0)

    log.info("[run=%s] Summarize: %d pages (cap=%d)", ctx.run_id, len(rows), limit)

    system_prompt = BASE_SYSTEM_PROMPT
    if hints.emphasis_hint:
        system_prompt = f"{BASE_SYSTEM_PROMPT}\n\n{hints.emphasis_hint}"

    sem = asyncio.Semaphore(2)
    processed = 0
    failed = 0
    lock = asyncio.Lock()

    async def worker(row: dict, idx: int) -> None:
        nonlocal processed, failed
        if ctx.is_cancelled and ctx.is_cancelled():
            raise CancellationError("summarize cancelled mid-batch")
        pid, url, title, text = row["id"], row["url"], row["page_title"], row["extracted_text"]
        async with sem:
            try:
                user_prompt = (
                    f"URL: {url}\n"
                    f"Title: {title or '(none)'}\n"
                    f"Body:\n{truncate_to_tokens(text or '', 8000)}"
                )
                summary = await chat(
                    user_prompt, system=system_prompt,
                    max_tokens=300, temperature=0.3,
                )
                async with get_conn() as conn:
                    async with conn.cursor() as cur:
                        await cur.execute(
                            """
                            UPDATE website_pages
                               SET ai_summary=%s,
                                   ai_analyzed_status='completed',
                                   last_analyzed_at=NOW()
                             WHERE id=%s
                            """,
                            (summary, pid),
                        )
                async with lock:
                    processed += 1
                log.info("[run=%s] [%d/%d] page #%d → %d chars", ctx.run_id, idx, len(rows), pid, len(summary))
            except Exception:
                async with lock:
                    failed += 1
                async with get_conn() as conn:
                    async with conn.cursor() as cur:
                        await cur.execute(
                            "UPDATE website_pages SET ai_analyzed_status='failed' WHERE id=%s",
                            (pid,),
                        )
                log.exception("Summarize failed for page #%s (%s)", pid, url)
        if ctx.report_progress:
            ctx.report_progress("summarize", processed + failed, len(rows))

    tasks = [asyncio.create_task(worker(r, i + 1)) for i, r in enumerate(rows)]
    try:
        await asyncio.gather(*tasks)
    except CancellationError:
        for t in tasks:
            if not t.done():
                t.cancel()
        raise

    log.info("[run=%s] Summarize done: %d ok, %d failed", ctx.run_id, processed, failed)
    return PhaseResult(name="summarize", processed=processed, failed=failed, total=len(rows))
