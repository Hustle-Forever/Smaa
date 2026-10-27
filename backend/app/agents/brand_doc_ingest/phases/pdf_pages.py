"""Phase B½ — PDF page rasterization + vision-based brand signal extraction.

Fallback for spec sheets where text extraction misses visual signal (scanned
PDFs, image-only brand decks). For every PDF doc:

  1. fitz.open(path) → enumerate pages (cap MAX_PAGES_PER_PDF)
  2. page.get_pixmap(matrix=fitz.Matrix(2, 2)) → 2× resolution PNG
  3. Write to data/uploads/<doc_id>/page-<n>.png
  4. vision_describe(...) with a strict-JSON prompt asking for visible hex
     codes, font names, dominant colours, style notes
  5. Aggregate per-page findings into metadata.page_extractions

Idempotent: skips PDFs that already have page_extractions, unless the agent's
force_re_extract flag is true.

Cost: ~$0.005 per page × N pages × number of new PDFs. For the existing
12-page style guide that's ~$0.06.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import shutil
import tempfile
from pathlib import Path

from psycopg.types.json import Jsonb

from ....config import config
from ....db import get_conn
from ....services.openai_client import is_vision_supported, vision_describe
from ....services.storage import ensure_dir
from ..._common import materialize_doc
from ...website_ingest.types import CancellationError, PhaseContext, PhaseResult

log = logging.getLogger("archivist.phase.brand_doc.pdf_pages")

MAX_PAGES_PER_PDF = 20
SEM_CONCURRENCY = 2  # vision_describe is the bottleneck

PER_PAGE_PROMPT = (
    "Analyse this page from a brand-identity PDF. Return a STRICT JSON object "
    "with these keys:\n"
    "  hex:              array of hex strings you see printed as text (e.g. \"#FF5A3C\")\n"
    "  fonts:            array of font / typeface names visible on this page\n"
    "  dominant_colors:  ≤6 hex codes that visually dominate this page\n"
    "  style_note:       one short sentence about composition, motifs, or visual style\n"
    "\n"
    "Rules:\n"
    "  - hex must be 6-digit uppercase like \"#FF5A3C\".\n"
    "  - If a category is absent on this page, return an empty array (or empty string).\n"
    "  - Output ONLY the JSON object, no prose, no markdown fences."
)


HEX_RE = re.compile(r"^#?([0-9A-Fa-f]{6})$")
HEX3_RE = re.compile(r"^#?([0-9A-Fa-f]{3})$")


def _normalize_hex_list(items) -> list[str]:
    out: list[str] = []
    if not isinstance(items, list):
        return out
    for c in items:
        if not isinstance(c, str):
            continue
        c = c.strip()
        m = HEX_RE.match(c)
        if m:
            out.append(f"#{m.group(1).upper()}")
            continue
        m3 = HEX3_RE.match(c)
        if m3:
            short = m3.group(1)
            out.append(f"#{''.join(ch * 2 for ch in short).upper()}")
    seen, deduped = set(), []
    for c in out:
        if c not in seen:
            seen.add(c)
            deduped.append(c)
    return deduped


def _coerce_str_list(items) -> list[str]:
    if not isinstance(items, list):
        return []
    return [s.strip() for s in items if isinstance(s, str) and s.strip()]


async def _select_pending_pdfs(force: bool) -> list[dict]:
    where = [
        "doc_kind = 'pdf'",
        "upload_status IN ('completed', 'processing')",
        "storage_path IS NOT NULL",
    ]
    if not force:
        where.append("(metadata->'page_extractions') IS NULL")
    sql = (
        "SELECT id, source_filename, storage_bucket, storage_path "
        "  FROM brand_documents "
        f"WHERE {' AND '.join(where)} "
        " ORDER BY id ASC"
    )
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(sql)
            return await cur.fetchall()


def _rasterize_pdf_sync(pdf_path: Path, out_dir: Path) -> list[Path]:
    """Render up to MAX_PAGES_PER_PDF pages as 2× PNGs. Returns list of paths."""
    import fitz  # pymupdf — already a backend dep
    ensure_dir(out_dir)
    out: list[Path] = []
    with fitz.open(pdf_path) as doc:
        for i, page in enumerate(doc):
            if i >= MAX_PAGES_PER_PDF:
                break
            dest = out_dir / f"page-{i + 1:03d}.png"
            if not dest.exists():
                pix = page.get_pixmap(matrix=fitz.Matrix(2, 2), alpha=False)
                pix.save(str(dest))
            out.append(dest)
    return out


async def _describe_page(page_idx: int, image_path: Path) -> dict:
    if not is_vision_supported(image_path):
        log.warning("page %d at %s has unsupported MIME — skipping vision", page_idx, image_path.name)
        return {
            "page": page_idx, "hex": [], "fonts": [], "dominant_colors": [],
            "style_note": "", "_skipped": "unsupported MIME",
        }
    try:
        raw = await vision_describe(
            image_path,
            prompt=PER_PAGE_PROMPT,
            max_tokens=350,
            detail="low",
        )
    except Exception as e:
        log.exception("page %d vision_describe failed", page_idx)
        return {
            "page": page_idx, "hex": [], "fonts": [], "dominant_colors": [],
            "style_note": "", "_error": str(e)[:300],
        }

    # Vision returns text — strip fences if present.
    raw = raw.strip()
    if raw.startswith("```"):
        raw = raw.split("```", 2)[1]
        if raw.startswith("json"):
            raw = raw[4:]
        raw = raw.strip()
        if raw.endswith("```"):
            raw = raw[:-3].strip()

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {
            "page": page_idx, "hex": [], "fonts": [], "dominant_colors": [],
            "style_note": "", "_raw": raw[:800],
        }

    return {
        "page": page_idx,
        "hex": _normalize_hex_list(data.get("hex", [])),
        "fonts": _coerce_str_list(data.get("fonts", [])),
        "dominant_colors": _normalize_hex_list(data.get("dominant_colors", [])),
        "style_note": (data.get("style_note") or "").strip()
            if isinstance(data.get("style_note"), str) else "",
    }


async def _process_pdf(row: dict) -> tuple[int, int, dict]:
    """Returns (pages_processed, pages_failed, payload)."""
    doc_id = row["id"]
    # Materialise the PDF (downloads Supabase-stored docs to a temp file) and
    # rasterize into a TEMP dir — neither is a durable local copy.
    pdf_path, cleanup = await materialize_doc(row)
    pages_dir = Path(tempfile.mkdtemp(prefix=f"pdfpages_{doc_id}_"))
    try:
        if not pdf_path.exists():
            return 0, 0, {"_error": f"file missing: {pdf_path}"}

        page_paths = await asyncio.to_thread(_rasterize_pdf_sync, pdf_path, pages_dir)
        log.info("[doc %d] rasterized %d pages → %s", doc_id, len(page_paths), pages_dir)

        sem = asyncio.Semaphore(SEM_CONCURRENCY)

        async def worker(i: int, p: Path) -> dict:
            async with sem:
                return await _describe_page(i + 1, p)

        page_results = await asyncio.gather(*(worker(i, p) for i, p in enumerate(page_paths)))
        failed = sum(1 for r in page_results if "_error" in r)
        return len(page_results), failed, {"page_extractions": page_results}
    finally:
        cleanup()
        shutil.rmtree(pages_dir, ignore_errors=True)


async def _persist(doc_id: int, payload: dict) -> None:
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


async def run(ctx: PhaseContext) -> PhaseResult:
    force = bool((ctx.metadata or {}).get("force_re_extract"))

    rows = await _select_pending_pdfs(force)
    if not rows:
        log.info("[run=%s] pdf_pages: nothing to do (force=%s)", ctx.run_id, force)
        return PhaseResult(name="pdf_pages", total=0)

    log.info(
        "[run=%s] pdf_pages: %d PDF(s) to process (force=%s, max_pages=%d)",
        ctx.run_id, len(rows), force, MAX_PAGES_PER_PDF,
    )

    processed = 0
    failed = 0
    pages_total = 0
    pages_failed = 0

    for i, row in enumerate(rows, 1):
        if ctx.is_cancelled and ctx.is_cancelled():
            raise CancellationError("pdf_pages cancelled mid-batch")
        try:
            n_pages, n_failed, payload = await _process_pdf(row)
            await _persist(row["id"], payload)
            pages_total += n_pages
            pages_failed += n_failed
            processed += 1
            log.info(
                "[run=%s] [%d/%d] doc #%d → %d pages (%d failed)",
                ctx.run_id, i, len(rows), row["id"], n_pages, n_failed,
            )
        except Exception:
            failed += 1
            log.exception("[doc %s] pdf_pages failed", row["id"])
        if ctx.report_progress:
            ctx.report_progress("pdf_pages", processed + failed, len(rows))

    log.info(
        "[run=%s] pdf_pages done: %d docs ok, %d failed, %d pages, %d page-failures",
        ctx.run_id, processed, failed, pages_total, pages_failed,
    )
    return PhaseResult(
        name="pdf_pages", processed=processed, failed=failed, total=len(rows),
        extra={"pages_processed": pages_total, "pages_failed": pages_failed},
    )
