"""/api/agents/brand-synthesizer/* — manual brand-profile re-synthesis.

  POST   /api/agents/brand-synthesizer/run         kick off re-synth with current settings
  GET    /api/agents/brand-synthesizer/runs        list recent re-synth runs
  GET    /api/agents/brand-synthesizer/runs/{id}   one run + phase progress
  POST   /api/agents/brand-synthesizer/runs/{id}/cancel
  GET    /api/agents/brand-synthesizer/runs/{id}/events    SSE stream
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any, AsyncIterator

from fastapi import APIRouter, HTTPException
from sse_starlette.sse import EventSourceResponse

from ..agents.brand_synthesizer.agent import (
    BrandSynthesizerAgent,
    RunOptions,
    request_cancel,
)
from ..db import get_conn
from ..models.brand_docs import BrandSynthRunRequest
from ..workers.runner import get_registry

log = logging.getLogger("archivist.router.brand_synthesizer")

router = APIRouter(prefix="/api/agents/brand-synthesizer", tags=["agents:brand-synthesizer"])


def _serialise(v: Any) -> Any:
    if isinstance(v, datetime):
        return v.isoformat()
    return v


def _row(row: dict | None) -> dict | None:
    return {k: _serialise(v) for k, v in row.items()} if row else None


@router.post("/run")
async def start_run(body: BrandSynthRunRequest | None = None) -> dict:
    options = RunOptions(notes_suffix=(body.notes_suffix if body else None))
    registry = get_registry()

    async def factory(emit):
        agent = BrandSynthesizerAgent(options, on_event=emit)
        return await agent.run()

    tmp_run_id = await registry.start(factory)
    entry = registry.get(tmp_run_id)
    real_run_id = tmp_run_id
    if entry:
        import asyncio as _aio
        for _ in range(50):
            if entry.run_id > 0:
                real_run_id = entry.run_id
                break
            await _aio.sleep(0.05)
    return {"run_id": real_run_id, "status": "started"}


@router.get("/runs")
async def list_runs(limit: int = 20) -> dict:
    limit = max(1, min(int(limit), 100))
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT id, target_domain, status, started_at, finished_at, notes
                  FROM scrape_runs
                 WHERE target_domain='brand-synthesizer'
                 ORDER BY id DESC LIMIT %s
                """,
                (limit,),
            )
            rows = [_row(r) for r in await cur.fetchall()]
    return {"items": rows}


@router.get("/runs/{run_id}")
async def get_run(run_id: int) -> dict:
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT id, target_domain, status, started_at, finished_at, notes "
                "  FROM scrape_runs WHERE id=%s",
                (run_id,),
            )
            row = await cur.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="run not found")

    phases: dict = {}
    entry = get_registry().get(run_id)
    if entry:
        for evt in entry.events:
            if evt.get("type") == "phase_completed":
                phases[evt["phase"]] = {
                    "name": evt["phase"],
                    "processed": evt.get("processed", 0),
                    "failed": evt.get("failed", 0),
                    "skipped": evt.get("skipped", 0),
                    "total": evt.get("total", 0),
                    "extra": evt.get("extra", {}),
                }

    base = _row(row) or {}
    base["phases"] = phases
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
        if row["status"] == "completed":
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
                        "Run orphaned (backend restart)."
                        if row["status"] == "started" else row["status"]
                    ),
                }),
            }

    return EventSourceResponse(event_source())
