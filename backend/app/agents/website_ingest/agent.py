"""WebsiteIngestAgent — orchestrator for Agent 1.

Mirrors the legacy n8n `Archivist` workflow DAG:

  discovery → fetch → extract → chunk → summarize → embed_text
            ↓ (if !skip_images)   download:image → image
            ↓ (if !skip_videos)   download:video → video
                                   synthesize

Persists progress under scrape_runs.metadata.phases so a UI can show progress;
sets scrape_runs.status to 'started' / 'completed' / 'failed' / 'cancelled'.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Callable

from ...config import config
from ...db import get_conn
from ...services.settings_service import Hints, build_hints, load_settings
from .phases import (
    chunk as phase_chunk,
    discovery as phase_discovery,
    download as phase_download,
    embed_text as phase_embed_text,
    extract as phase_extract,
    fetch as phase_fetch,
    image as phase_image,
    summarize as phase_summarize,
    synthesize as phase_synthesize,
    video as phase_video,
)
from .types import CancellationError, PhaseContext, PhaseResult

log = logging.getLogger("archivist.agent.website_ingest")


@dataclass
class RunOptions:
    target_url: str | None = None
    target_domain: str | None = None
    max_pages: int | None = None
    skip_phases: list[str] | None = None
    # Optional notes tag applied to scrape_runs.notes at start-of-run
    # (e.g. "scheduled crawl" for runs kicked by the in-process scheduler).
    run_notes: str | None = None


async def _start_run(target_domain: str, notes: str | None = None) -> int:
    """Insert a 'started' row in scrape_runs. The existing schema has no
    `metadata` column, so per-phase progress is kept in-memory by the registry
    (and emitted via SSE) rather than persisted. The row's counters
    (pages_found_count etc.) are still updated by the discovery/fetch phases.
    """
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "INSERT INTO scrape_runs (target_domain, status, notes) VALUES (%s, 'started', %s) RETURNING id",
                (target_domain, notes),
            )
            row = await cur.fetchone()
    return int(row["id"])


async def _finish_run(run_id: int, status: str, notes: str | None = None) -> None:
    """`status` must satisfy CHECK (status IN ('started','completed','failed'))."""
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "UPDATE scrape_runs SET status=%s, finished_at=NOW(), notes=COALESCE(notes, %s) WHERE id=%s",
                (status, notes, run_id),
            )


# In-memory cancel flags keyed by scrape_runs.id. Survives only as long as the
# uvicorn process; the registry's task.cancel() handles the active-task path.
_cancel_flags: dict[int, bool] = {}


async def request_cancel(run_id: int) -> bool:
    """Mark a run as cancel-requested. Returns True if the run looks active."""
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


class WebsiteIngestAgent:
    """Single-run orchestrator. Instantiated per invocation."""

    def __init__(
        self,
        options: RunOptions,
        *,
        on_event: Callable[[dict], None] | None = None,
    ):
        self.options = options
        self.on_event = on_event or (lambda _e: None)
        self._run_id: int | None = None
        self._cancel_requested = False

    @property
    def run_id(self) -> int | None:
        return self._run_id

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
        # Throttle: emit every 5 items, on the very last item, and always for
        # short phases (total <= 20). Keeps the SSE replay tail useful instead
        # of being drowned by a hot phase's per-item events.
        if total <= 20 or processed >= total or processed % 5 == 0:
            self._emit("progress", phase=phase, processed=processed, total=total)

    async def _refresh_cancel(self) -> None:
        # In-memory only; sync via _is_cancelled() check.
        self._is_cancelled()

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
        target_url = self.options.target_url or config.target_url
        target_domain = self.options.target_domain or config.target_domain
        skip = set(self.options.skip_phases or [])

        settings = await load_settings()
        hints = build_hints(settings)
        log.info(
            "Settings loaded: mode=%s skip_images=%s skip_videos=%s image_cap=%d page_cap=%d",
            hints.mode, hints.skip_images, hints.skip_videos,
            hints.image_processing_cap, hints.page_processing_cap,
        )

        self._run_id = await _start_run(target_domain, notes=self.options.run_notes)
        self._emit(
            "run_started",
            target_url=target_url,
            target_domain=target_domain,
            mode=hints.mode,
            skip_images=hints.skip_images,
            skip_videos=hints.skip_videos,
        )

        ctx = PhaseContext(
            run_id=self._run_id,
            target_url=target_url,
            target_domain=target_domain,
            hints=hints,
            is_cancelled=self._is_cancelled,
            report_progress=self._report_progress,
        )

        try:
            if "discovery" not in skip:
                await self._refresh_cancel()
                await self._run_phase("discovery", lambda: phase_discovery.run(ctx))

            if "fetch" not in skip:
                await self._refresh_cancel()
                await self._run_phase(
                    "fetch",
                    lambda: phase_fetch.run(ctx, limit=self.options.max_pages),
                )

            if "extract" not in skip:
                await self._refresh_cancel()
                await self._run_phase("extract", lambda: phase_extract.run(ctx))

            if "chunk" not in skip:
                await self._refresh_cancel()
                await self._run_phase("chunk", lambda: phase_chunk.run(ctx))

            if "summarize" not in skip:
                await self._refresh_cancel()
                await self._run_phase("summarize", lambda: phase_summarize.run(ctx))

            if "embed_text" not in skip:
                await self._refresh_cancel()
                await self._run_phase("embed_text", lambda: phase_embed_text.run(ctx))

            if not hints.skip_images and "image" not in skip:
                await self._refresh_cancel()
                await self._run_phase(
                    "download:image",
                    lambda: phase_download.run(ctx, kind="image"),
                )
                await self._refresh_cancel()
                await self._run_phase("image", lambda: phase_image.run(ctx))

            if not hints.skip_videos and "video" not in skip:
                await self._refresh_cancel()
                await self._run_phase(
                    "download:video",
                    lambda: phase_download.run(ctx, kind="video"),
                )
                await self._refresh_cancel()
                await self._run_phase("video", lambda: phase_video.run(ctx))

            if "synthesize" not in skip:
                await self._refresh_cancel()
                await self._run_phase("synthesize", lambda: phase_synthesize.run(ctx))

            await _finish_run(self._run_id, "completed")
            self._emit("run_completed", status="completed")
            return {"run_id": self._run_id, "status": "completed"}

        except CancellationError as e:
            # The schema's CHECK constraint only allows 'completed' or 'failed';
            # we encode cancellation as failed + 'cancelled' notes.
            await _finish_run(self._run_id, "failed", notes=f"cancelled: {e}")
            _cancel_flags.pop(self._run_id, None)
            self._emit("run_cancelled", reason=str(e))
            return {"run_id": self._run_id, "status": "cancelled"}

        except Exception as e:
            log.exception("Agent failed")
            await _finish_run(self._run_id, "failed", notes=f"{type(e).__name__}: {str(e)[:400]}")
            self._emit("run_failed", error=str(e)[:400])
            raise
