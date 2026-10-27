"""/api/agents/website-ingest/* — kicks off, monitors, cancels Agent 1 runs.

  POST   /api/agents/website-ingest/run         start a new run
  GET    /api/agents/website-ingest/runs        list recent
  GET    /api/agents/website-ingest/runs/{id}   one run + phase progress
  POST   /api/agents/website-ingest/runs/{id}/cancel  set cancel flag
  GET    /api/agents/website-ingest/runs/{id}/events  SSE stream
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any, AsyncIterator

from fastapi import APIRouter, HTTPException
from sse_starlette.sse import EventSourceResponse

from ..agents.website_ingest.agent import (
    RunOptions,
    WebsiteIngestAgent,
    request_cancel,
)
from ..db import get_conn
from ..models.runs import RunDetail, RunRequest, RunStartResponse, RunSummary
from ..workers.runner import get_registry

log = logging.getLogger("archivist.router.agents")

router = APIRouter(prefix="/api/agents/website-ingest", tags=["agents:website-ingest"])


def _serialise(v: Any) -> Any:
    if isinstance(v, datetime):
        return v.isoformat()
    return v


def _row(row: dict | None) -> dict | None:
    return {k: _serialise(v) for k, v in row.items()} if row else None


@router.post("/run", response_model=RunStartResponse)
async def start_run(body: RunRequest):
    options = RunOptions(
        target_url=body.target_url,
        target_domain=body.target_domain,
        max_pages=body.max_pages,
        skip_phases=body.skip_phases,
    )
    registry = get_registry()

    async def factory(emit):
        agent = WebsiteIngestAgent(options, on_event=emit)
        return await agent.run()

    tmp_run_id = await registry.start(factory)
    # Wait briefly for the agent to publish 'run_started' with the real DB id.
    # We do this without blocking by spinning on registry events.
    entry = registry.get(tmp_run_id)
    real_run_id = tmp_run_id
    if entry:
        # Best-effort: poll once for the renamed entry. The agent emits
        # run_started almost immediately after _start_run (a single INSERT).
        for _ in range(50):
            if entry.run_id > 0:
                real_run_id = entry.run_id
                break
            import asyncio as _aio
            await _aio.sleep(0.05)
    return RunStartResponse(run_id=real_run_id, status="started")


@router.get("/runs")
async def list_runs(limit: int = 20) -> dict:
    limit = max(1, min(int(limit), 100))
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT id, target_domain, sitemap_url, status, started_at, finished_at,
                       pages_found_count, pages_inserted_count, errors_count, notes
                  FROM scrape_runs
                 WHERE target_domain NOT IN ('brand-doc-ingest', 'brand-synthesizer')
                 ORDER BY id DESC
                 LIMIT %s
                """,
                (limit,),
            )
            rows = [_row(r) for r in await cur.fetchall()]
    return {"items": rows}


@router.get("/runs/{run_id}", response_model=RunDetail)
async def get_run(run_id: int):
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT id, target_domain, sitemap_url, status, started_at, finished_at, "
                "       pages_found_count, pages_inserted_count, errors_count, notes "
                "  FROM scrape_runs WHERE id=%s",
                (run_id,),
            )
            row = await cur.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="run not found")

    # Phase progress lives in-memory in the registry (scrape_runs has no
    # metadata column). Pull whatever the registry has.
    phases: dict = {}
    registry = get_registry()
    entry = registry.get(run_id)
    if entry:
        for evt in entry.events:
            if evt.get("type") == "phase_completed":
                phases[evt["phase"]] = {
                    "processed": evt.get("processed", 0),
                    "failed": evt.get("failed", 0),
                    "skipped": evt.get("skipped", 0),
                    "total": evt.get("total", 0),
                    "extra": evt.get("extra", {}),
                    "name": evt["phase"],
                }
            elif evt.get("type") == "phase_started":
                # Mark as in-progress if no completed entry yet
                phases.setdefault(evt["phase"], {"name": evt["phase"], "processed": 0, "total": 0})

    base = _row(row) or {}
    base["phases"] = phases
    base["cancel_requested"] = False  # in-memory; not surfaced here
    return base


@router.post("/runs/{run_id}/cancel")
async def cancel_run(run_id: int) -> dict:
    db_flagged = await request_cancel(run_id)
    in_flight = await get_registry().cancel(run_id)
    if not db_flagged and not in_flight:
        raise HTTPException(status_code=404, detail="run not active or not found")
    return {"run_id": run_id, "cancel_requested": True, "in_flight_cancelled": in_flight}


@router.get("/runs/{run_id}/events")
async def stream_run_events(run_id: int):
    registry = get_registry()

    async def event_source() -> AsyncIterator[dict]:
        entry = registry.get(run_id)
        if entry:
            async for evt in registry.subscribe(run_id):
                yield {"event": evt.get("type", "message"), "data": json.dumps(evt)}
            return

        # No live registry entry — the agent isn't running in this process. Sync
        # from the DB so the UI doesn't sit forever in a "Connecting…" state.
        async with get_conn() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT status, notes FROM scrape_runs WHERE id=%s",
                    (run_id,),
                )
                row = await cur.fetchone()

        if not row:
            yield {
                "event": "info",
                "data": json.dumps({"type": "no_active_run", "run_id": run_id}),
            }
            return

        status = row["status"]
        if status == "completed":
            yield {
                "event": "run_completed",
                "data": json.dumps({
                    "type": "run_completed",
                    "run_id": run_id,
                    "result": {"status": "completed", "notes": row.get("notes")},
                }),
            }
        else:
            yield {
                "event": "run_failed",
                "data": json.dumps({
                    "type": "run_failed",
                    "run_id": run_id,
                    "error": row.get("notes") or (
                        "Run orphaned (backend restart). The agent is no longer running."
                        if status == "started" else status
                    ),
                }),
            }

    return EventSourceResponse(event_source())
