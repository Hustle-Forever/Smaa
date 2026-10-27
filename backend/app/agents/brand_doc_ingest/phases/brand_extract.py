"""Phase C — extract structured brand details from each brand_documents row.

Pulls explicit brand-identity signals out of every uploaded doc:
  - hex colours (text-extracted or visible in vision descriptions)
  - font / typeface names
  - taglines, do/don'ts
  - audience descriptor
  - voice adjectives
  - free-form visual style notes

Stores the result into `brand_documents.metadata.extracted`. The downstream
synthesizer uses this with priority over inferred website signals.

Idempotent: skips rows where `metadata.extracted` is already set, unless the
agent's `force_re_extract` flag is true (set via ctx.metadata).
"""
from __future__ import annotations

import asyncio
import json
import logging
import re

from psycopg.types.json import Jsonb

from ....db import get_conn
from ....services.openai_client import chat
from ....utils.tokens import truncate_to_tokens
from ...website_ingest.types import CancellationError, PhaseContext, PhaseResult

log = logging.getLogger("archivist.phase.brand_doc.brand_extract")

# Concurrency cap — gpt-4o-mini calls are cheap but we don't want to flood
# the OpenAI client on a 20-doc backfill.
SEM_CONCURRENCY = 3

SYSTEM_PROMPT = (
    "You are extracting brand-identity signals from a single document for the "
    "42 Abu Dhabi brand archive. Return a STRICT JSON object with these keys "
    "(every key required, use empty array / empty string when absent):\n"
    "  colors:              array of hex strings like \"#FF5A3C\"\n"
    "  fonts:               array of typeface names exactly as printed\n"
    "  taglines:            array of strings\n"
    "  dos:                 array of action-oriented dos\n"
    "  donts:               array of action-oriented donts\n"
    "  audience:            single sentence describing implied audience\n"
    "  voice_descriptors:   array of 4-8 adjectives describing brand voice\n"
    "  visual_style_notes:  one paragraph on visual style (composition, motifs)\n"
    "\n"
    "Rules — non-negotiable:\n"
    "  1. ONLY include hex codes EXPLICITLY present in the source (as text or "
    "described in vision output). Do NOT invent colours.\n"
    "  2. ONLY include fonts named in the source. Do NOT paraphrase.\n"
    "  3. If the source doesn't contain a category, return an empty array.\n"
    "  4. Hex codes must be uppercase 6-digit (#RRGGBB).\n"
    "  5. Output ONLY the JSON object, no prose, no markdown fences."
)


def _normalize_hex_list(items: list) -> list[str]:
    """Coerce LLM output to uppercase 6-digit hex; drop garbage."""
    out: list[str] = []
    for c in items or []:
        if not isinstance(c, str):
            continue
        c = c.strip()
        m = re.match(r"^#?([0-9A-Fa-f]{6})$", c)
        if m:
            out.append(f"#{m.group(1).upper()}")
        elif re.match(r"^#?([0-9A-Fa-f]{3})$", c):
            # 3-digit shorthand → expand
            short = c.lstrip("#")
            out.append(f"#{''.join(ch * 2 for ch in short).upper()}")
    # Dedup, preserve order
    seen = set()
    deduped = []
    for c in out:
        if c not in seen:
            seen.add(c)
            deduped.append(c)
    return deduped


def _coerce_str_list(items) -> list[str]:
    if not isinstance(items, list):
        return []
    return [s.strip() for s in items if isinstance(s, str) and s.strip()]


async def _select_pending(force: bool) -> list[dict]:
    where = ["upload_status IN ('completed', 'processing')", "doc_kind <> 'video'"]
    if not force:
        where.append("(metadata->'extracted') IS NULL")
    sql = (
        "SELECT id, doc_kind, source_filename, raw_text, ai_description, "
        "       ai_summary, metadata "
        "  FROM brand_documents "
        f"WHERE {' AND '.join(where)} "
        " ORDER BY id ASC"
    )
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(sql)
            return await cur.fetchall()


def _body_for_extraction(row: dict) -> str:
    """The body text to feed the extractor."""
    if row["doc_kind"] == "image":
        return (row.get("ai_description") or "").strip()
    raw = (row.get("raw_text") or "").strip()
    if raw:
        return raw
    return (row.get("ai_summary") or "").strip()


async def _extract_one(row: dict) -> dict:
    """Run one gpt-4o-mini extraction call. Returns the normalised JSON dict
    (always with all expected keys)."""
    doc_id = row["id"]
    body = _body_for_extraction(row)
    if not body:
        return {
            "colors": [], "fonts": [], "taglines": [],
            "dos": [], "donts": [], "audience": "",
            "voice_descriptors": [], "visual_style_notes": "",
            "_note": "no body to extract from",
        }

    user_prompt = (
        f"Filename: {row.get('source_filename') or '(none)'}\n"
        f"Kind: {row['doc_kind']}\n"
        f"Body:\n{truncate_to_tokens(body, 8000)}"
    )

    raw = await chat(
        user_prompt,
        system=SYSTEM_PROMPT,
        max_tokens=1200,
        temperature=0.0,
        response_format={"type": "json_object"},
    )
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        log.warning("[doc %d] JSON parse failed; storing raw", doc_id)
        return {
            "colors": [], "fonts": [], "taglines": [],
            "dos": [], "donts": [], "audience": "",
            "voice_descriptors": [], "visual_style_notes": "",
            "_raw": raw[:2000],
        }

    return {
        "colors": _normalize_hex_list(data.get("colors", [])),
        "fonts": _coerce_str_list(data.get("fonts", [])),
        "taglines": _coerce_str_list(data.get("taglines", [])),
        "dos": _coerce_str_list(data.get("dos", [])),
        "donts": _coerce_str_list(data.get("donts", [])),
        "audience": (data.get("audience") or "").strip() if isinstance(data.get("audience"), str) else "",
        "voice_descriptors": _coerce_str_list(data.get("voice_descriptors", [])),
        "visual_style_notes": (data.get("visual_style_notes") or "").strip()
            if isinstance(data.get("visual_style_notes"), str) else "",
    }


async def _persist(doc_id: int, extracted: dict) -> None:
    """Merge `extracted` into metadata.extracted; also stamp extracted_at."""
    payload = {"extracted": extracted, "extracted_at": _now_iso()}
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                UPDATE brand_documents
                   SET metadata = COALESCE(metadata, '{}'::jsonb) || %s::jsonb
                 WHERE id=%s
                """,
                (Jsonb(payload), doc_id),
            )


def _now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


async def run(ctx: PhaseContext) -> PhaseResult:
    force = bool((ctx.metadata or {}).get("force_re_extract"))

    rows = await _select_pending(force)
    if not rows:
        log.info("[run=%s] brand_extract: nothing to do (force=%s)", ctx.run_id, force)
        return PhaseResult(name="brand_extract", total=0)

    log.info(
        "[run=%s] brand_extract: %d docs (force=%s)",
        ctx.run_id, len(rows), force,
    )

    sem = asyncio.Semaphore(SEM_CONCURRENCY)
    processed = 0
    failed = 0
    lock = asyncio.Lock()

    async def worker(row: dict, idx: int) -> None:
        nonlocal processed, failed
        if ctx.is_cancelled and ctx.is_cancelled():
            raise CancellationError("brand_extract cancelled mid-batch")
        doc_id = row["id"]
        async with sem:
            try:
                extracted = await _extract_one(row)
                await _persist(doc_id, extracted)
                async with lock:
                    processed += 1
                log.info(
                    "[run=%s] [%d/%d] doc #%d (%s) → colors=%d fonts=%d",
                    ctx.run_id, idx, len(rows), doc_id,
                    row.get("source_filename") or row["doc_kind"],
                    len(extracted.get("colors", [])),
                    len(extracted.get("fonts", [])),
                )
            except Exception:
                async with lock:
                    failed += 1
                log.exception("[doc %d] brand_extract failed", doc_id)
        if ctx.report_progress:
            ctx.report_progress("brand_extract", processed + failed, len(rows))

    tasks = [asyncio.create_task(worker(r, i + 1)) for i, r in enumerate(rows)]
    try:
        await asyncio.gather(*tasks)
    except CancellationError:
        for t in tasks:
            if not t.done():
                t.cancel()
        raise

    log.info(
        "[run=%s] brand_extract done: %d ok, %d failed",
        ctx.run_id, processed, failed,
    )
    return PhaseResult(
        name="brand_extract", processed=processed, failed=failed, total=len(rows),
    )
