"""BrandDocIngestAgent — orchestrator for Agent 2.

DAG:
  doc_process → doc_summarize_embed → synthesize (with notes_prefix='doc-ingest synth')

Like Agent 1, this writes a scrape_runs row for run tracking (we reuse the
table so the existing /api/agents/website-ingest/runs UX patterns can be
replicated for this agent). The settings-aware synthesizer tail produces a
new brand_profile_runs row whose `notes` distinguishes it from website runs.

Cancellation: in-memory only (same pattern as Agent 1).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from ...config import config
from ...db import get_conn
from ...services.settings_service import build_hints, load_settings
from ..website_ingest.types import CancellationError, PhaseContext, PhaseResult
from ..website_ingest.phases import synthesize as phase_synthesize
from .phases import (
    brand_extract as phase_brand_extract,
    pdf_pages as phase_pdf_pages,
    process as phase_process,
    summarize_embed as phase_summarize_embed,
)

log = logging.getLogger("archivist.agent.brand_doc_ingest")


@dataclass
class RunOptions:
    run_synthesis: bool = True
    skip_phases: list[str] | None = None
    # IDs of newly-inserted brand_documents rows (for UI surfacing). The agent
    # itself just processes whatever is `upload_status='uploaded'` — it doesn't
    # need this list to know what to do.
    inserted_doc_ids: list[int] | None = None
    # When true, brand_extract + pdf_pages re-run on ALL docs (regardless of
    # existing metadata.extracted / metadata.page_extractions). Used to backfill
    # after a pipeline change.
    force_re_extract: bool = False


async def _start_run() -> int:
    """Open a scrape_runs row tagged with target_domain='doc-ingest'."""
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "INSERT INTO scrape_runs (target_domain, status, notes) "
                "VALUES ('brand-doc-ingest', 'started', %s) RETURNING id",
                ("brand-doc-ingest agent",),
            )
            row = await cur.fetchone()
    return int(row["id"])


async def _finish_run(run_id: int, status: str, notes: str | None = None) -> None:
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "UPDATE scrape_runs SET status=%s, finished_at=NOW(), notes=COALESCE(%s, notes) WHERE id=%s",
                (status, notes, run_id),
            )


_cancel_flags: dict[int, bool] = {}


async def request_cancel(run_id: int) -> bool:
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT status FROM scrape_runs WHERE id=%s",
                (run_id,),
            )
            row = await cur.fetchone()
    if not row or row["status"] != "started":
        return False
    _cancel_flags[run_id] = True
    return True


class BrandDocIngestAgent:
    def __init__(self, options: RunOptions, *, on_event=None):
        self.options = options
        self.on_event = on_event or (lambda _e: None)
        self._run_id: int | None = None
        self._cancel_requested = False

    def _emit(self, type_: str, **fields) -> None:
        evt = {"type": type_, "run_id": self._run_id, **fields}
        try:
            self.on_event(evt)
        except Exception:
            log.exception("on_event hook raised")

    def _is_cancelled(self) -> bool:
        if self._cancel_requested:
            return True
        if self._run_id is not None and _cancel_flags.get(self._run_id):
            self._cancel_requested = True
            return True
        return False

    def _report_progress(self, phase: str, processed: int, total: int) -> None:
        if total <= 20 or processed >= total or processed % 5 == 0:
            self._emit("progress", phase=phase, processed=processed, total=total)

    async def _run_phase(self, name: str, coro_factory) -> PhaseResult:
        self._emit("phase_started", phase=name)
        result = await coro_factory()
        self._emit(
            "phase_completed",
            phase=name,
            processed=result.processed,
            failed=result.failed,
            skipped=result.skipped,
            total=result.total,
            extra=result.extra,
        )
        return result

    async def run(self) -> dict:
        settings = await load_settings()
        hints = build_hints(settings)
        skip = set(self.options.skip_phases or [])

        self._run_id = await _start_run()
        self._emit(
            "run_started",
            agent="brand-doc-ingest",
            mode=hints.mode,
            inserted_doc_ids=self.options.inserted_doc_ids or [],
        )

        ctx = PhaseContext(
            run_id=self._run_id,
            target_url=config.target_url,
            target_domain="brand-doc-ingest",
            hints=hints,
            is_cancelled=self._is_cancelled,
            report_progress=self._report_progress,
            metadata={"force_re_extract": self.options.force_re_extract},
        )

        try:
            if "doc_process" not in skip:
                await self._run_phase("doc_process", lambda: phase_process.run(ctx))

            if "doc_summarize_embed" not in skip:
                await self._run_phase(
                    "doc_summarize_embed",
                    lambda: phase_summarize_embed.run(ctx),
                )

            if "pdf_pages" not in skip:
                await self._run_phase("pdf_pages", lambda: phase_pdf_pages.run(ctx))

            if "brand_extract" not in skip:
                await self._run_phase(
                    "brand_extract",
                    lambda: phase_brand_extract.run(ctx),
                )

            if self.options.run_synthesis and "synthesize" not in skip:
                await self._run_phase(
                    "synthesize",
                    lambda: phase_synthesize.run(ctx, notes_prefix="doc-ingest synth"),
                )

            await _finish_run(self._run_id, "completed", notes="brand-doc-ingest done")
            self._emit("run_completed", status="completed")
            return {"run_id": self._run_id, "status": "completed"}

        except CancellationError as e:
            await _finish_run(self._run_id, "failed", notes=f"cancelled: {e}")
            _cancel_flags.pop(self._run_id, None)
            self._emit("run_cancelled", reason=str(e))
            return {"run_id": self._run_id, "status": "cancelled"}

        except Exception as e:
            log.exception("Brand-doc agent failed")
            await _finish_run(self._run_id, "failed", notes=f"{type(e).__name__}: {str(e)[:400]}")
            self._emit("run_failed", error=str(e)[:400])
            raise
