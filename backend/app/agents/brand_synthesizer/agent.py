"""BrandSynthesizerAgent — Agent 3.

Re-runs only the synthesis phase against the current corpus, using the
current brand_profile_settings row. No fresh ingest. Cheap and fast (~30s).

Mirrors the legacy n8n `Archivist – Brand Synthesizer` workflow's intent:
"give me a new brand profile that reflects the latest settings, without
re-crawling or re-uploading anything."

The synthesis phase itself opens its own brand_profile_runs row; this agent
also opens a scrape_runs row tagged `target_domain='brand-synthesizer'` so
the runs UX is consistent with the other agents.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from ...config import config
from ...db import get_conn
from ...services.settings_service import build_hints, load_settings
from ..website_ingest.phases import synthesize as phase_synthesize
from ..website_ingest.types import CancellationError, PhaseContext, PhaseResult

log = logging.getLogger("archivist.agent.brand_synthesizer")


@dataclass
class RunOptions:
    notes_suffix: str | None = None  # appended to the synthesizer's notes prefix


async def _start_run() -> int:
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "INSERT INTO scrape_runs (target_domain, status, notes) "
                "VALUES ('brand-synthesizer', 'started', 'manual re-synth') RETURNING id",
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


class BrandSynthesizerAgent:
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

    async def _run_phase(self, name: str, coro_factory) -> PhaseResult:
        self._emit("phase_started", phase=name)
        result = await coro_factory()
        self._emit(
            "phase_completed", phase=name,
            processed=result.processed, failed=result.failed,
            skipped=result.skipped, total=result.total,
            extra=result.extra,
        )
        return result

    async def run(self) -> dict:
        settings = await load_settings()
        hints = build_hints(settings)

        self._run_id = await _start_run()
        self._emit("run_started", agent="brand-synthesizer", mode=hints.mode)

        prefix = "manual resynth"
        if self.options.notes_suffix:
            prefix = f"{prefix} ({self.options.notes_suffix})"

        ctx = PhaseContext(
            run_id=self._run_id,
            target_url=config.target_url,
            target_domain="brand-synthesizer",
            hints=hints,
            is_cancelled=self._is_cancelled,
            report_progress=lambda phase, p, t: self._emit("progress", phase=phase, processed=p, total=t),
        )

        try:
            await self._run_phase(
                "synthesize",
                lambda: phase_synthesize.run(ctx, notes_prefix=prefix),
            )
            await _finish_run(self._run_id, "completed", notes="brand-synthesizer done")
            self._emit("run_completed", status="completed")
            return {"run_id": self._run_id, "status": "completed"}
        except CancellationError as e:
            await _finish_run(self._run_id, "failed", notes=f"cancelled: {e}")
            _cancel_flags.pop(self._run_id, None)
            self._emit("run_cancelled", reason=str(e))
            return {"run_id": self._run_id, "status": "cancelled"}
        except Exception as e:
            log.exception("Brand synthesizer failed")
            await _finish_run(self._run_id, "failed", notes=f"{type(e).__name__}: {str(e)[:400]}")
            self._emit("run_failed", error=str(e)[:400])
            raise
