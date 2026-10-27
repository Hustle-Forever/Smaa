"""Phase 2 — HTTP-GET pending pages, store raw_html + title.

Async/httpx with a semaphore for politeness. Each successful fetch updates
website_pages: fetched_status='fetched', raw_html, page_title, http_status,
content_type, last_fetched_at.
"""
from __future__ import annotations

import asyncio
import logging

import httpx
from psycopg.types.json import Jsonb

from ....config import config
from ....db import get_conn
from ....utils.html import extract_title
from ..types import CancellationError, PhaseContext, PhaseResult

log = logging.getLogger("archivist.phase.fetch")


async def _select_pending(limit: int | None) -> list[str]:
    sql = (
        "SELECT url FROM website_pages "
        " WHERE fetched_status='pending' "
        "   AND (domain=%s OR domain LIKE %s) "
        " ORDER BY created_at ASC"
    )
    params: tuple = (config.target_domain, "%." + config.target_domain)
    if limit:
        sql += f" LIMIT {int(limit)}"
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(sql, params)
            rows = await cur.fetchall()
    return [r["url"] for r in rows]


async def _fetch_one(client: httpx.AsyncClient, url: str) -> tuple[int | None, str | None, dict]:
    try:
        r = await client.get(url, follow_redirects=True)
        return r.status_code, r.text, {"final_url": str(r.url), "headers": dict(r.headers)}
    except httpx.HTTPError as e:
        return None, None, {"error": str(e), "error_type": type(e).__name__}


async def _update_success(conn, url: str, status: int, ctype: str | None, html: str | None, title: str | None) -> None:
    async with conn.cursor() as cur:
        await cur.execute(
            """
            UPDATE website_pages
               SET fetched_status='fetched',
                   last_fetched_at=NOW(),
                   http_status=%s,
                   content_type=%s,
                   raw_html=%s,
                   page_title=COALESCE(%s, page_title),
                   metadata=COALESCE(metadata, '{}'::jsonb) || %s::jsonb
             WHERE url=%s
            """,
            (status, ctype, html, title,
             Jsonb({"last_fetch": {"http_status": status, "content_type": ctype}}),
             url),
        )


async def _update_failed(conn, url: str, status: int | None, meta: dict) -> None:
    async with conn.cursor() as cur:
        await cur.execute(
            """
            UPDATE website_pages
               SET fetched_status='failed',
                   last_fetched_at=NOW(),
                   http_status=%s,
                   metadata=COALESCE(metadata, '{}'::jsonb) || %s::jsonb
             WHERE url=%s
            """,
            (status, Jsonb({"last_fetch_error": meta or {}}), url),
        )


async def run(ctx: PhaseContext, *, limit: int | None = None) -> PhaseResult:
    pending = await _select_pending(limit)
    if not pending:
        log.info("[run=%s] Fetch: nothing pending", ctx.run_id)
        return PhaseResult(name="fetch", processed=0, total=0)

    log.info("[run=%s] Fetch: %d pending pages", ctx.run_id, len(pending))
    sem = asyncio.Semaphore(config.run_concurrency_fetch)
    processed = 0
    failed = 0

    async with httpx.AsyncClient(
        headers={"User-Agent": config.user_agent},
        timeout=config.request_timeout,
    ) as client:
        async def worker(url: str) -> None:
            nonlocal processed, failed
            if ctx.is_cancelled and ctx.is_cancelled():
                raise CancellationError("fetch cancelled mid-batch")
            async with sem:
                status, text, meta = await _fetch_one(client, url)
                async with get_conn() as conn:
                    if status is not None and 200 <= status < 400 and text is not None:
                        ctype = meta.get("headers", {}).get("Content-Type") \
                            or meta.get("headers", {}).get("content-type")
                        is_html = bool(ctype and "html" in ctype.lower())
                        title = extract_title(text) if is_html else None
                        await _update_success(
                            conn, url, status, ctype,
                            text if is_html else None, title,
                        )
                        processed += 1
                    else:
                        await _update_failed(conn, url, status, meta)
                        failed += 1
                if ctx.report_progress:
                    ctx.report_progress("fetch", processed + failed, len(pending))

        # Batch via tasks
        tasks = [asyncio.create_task(worker(u)) for u in pending]
        try:
            await asyncio.gather(*tasks)
        except CancellationError:
            for t in tasks:
                if not t.done():
                    t.cancel()
            raise

    log.info("[run=%s] Fetch done: %d ok, %d failed", ctx.run_id, processed, failed)
    return PhaseResult(name="fetch", processed=processed, failed=failed, total=len(pending))
