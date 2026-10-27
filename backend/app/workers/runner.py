"""In-process asyncio.Task registry + SSE pub/sub for agent runs.

A single RunRegistry instance lives on app.state. Agents publish events via
the registry; multiple subscribers (SSE clients) can fan out from the same run.

Memory model:
  - registry.tasks[run_id] = asyncio.Task
  - registry.events[run_id] = deque[event]  (capped, for late subscribers to catch up)
  - registry.queues[run_id] = list[asyncio.Queue]  (one per active subscriber)

When the task finishes (success/fail/cancel), the registry emits a final event
and keeps a short tail of events in memory so late subscribers (re-connect
within seconds) see them. After a TTL, the entry is cleaned up.
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Awaitable, Callable

log = logging.getLogger("archivist.runner")

EVENT_TAIL = 200           # how many recent events to retain for replay
ENTRY_TTL_SECONDS = 600    # how long to keep entries after run finishes


@dataclass
class RunEntry:
    run_id: int
    task: asyncio.Task
    events: deque = field(default_factory=lambda: deque(maxlen=EVENT_TAIL))
    queues: list[asyncio.Queue] = field(default_factory=list)
    finished_at: float | None = None
    final_event: dict | None = None


class RunRegistry:
    def __init__(self) -> None:
        self._entries: dict[int, RunEntry] = {}
        self._lock = asyncio.Lock()

    async def start(
        self,
        agent_factory: Callable[[Callable[[dict], None]], Awaitable[Any]],
        *,
        run_id_hint: int | None = None,
    ) -> int:
        """Schedule an agent run. agent_factory(emit) -> awaitable that returns a result dict.

        The agent is expected to call its own on_event hook, which we pass via
        agent_factory. Agents typically register themselves with a real run_id
        inside their first event ("run_started").

        run_id_hint may be set if the caller already knows the run id (rare).
        """
        # We don't yet know the real run_id — we'll capture it from the first
        # "run_started" event. Use a temporary key until then.
        tmp_key = -int(time.time() * 1000)
        entry = RunEntry(run_id=tmp_key, task=None)  # type: ignore[arg-type]

        def emit(event: dict) -> None:
            real_run_id = event.get("run_id") or entry.run_id
            event = {**event, "run_id": real_run_id, "ts": time.time()}
            self._dispatch(entry, event)

        async def wrapper() -> Any:
            try:
                return await agent_factory(emit)
            except Exception as e:
                log.exception("Agent task crashed")
                emit({"type": "run_failed", "error": f"{type(e).__name__}: {str(e)[:400]}"})
                raise

        task = asyncio.create_task(wrapper())
        entry.task = task

        async with self._lock:
            self._entries[entry.run_id] = entry

        task.add_done_callback(lambda t: asyncio.create_task(self._on_task_done(entry, t)))
        return entry.run_id

    def _dispatch(self, entry: RunEntry, event: dict) -> None:
        """Move a tmp entry to the real run_id if needed, then push event to all queues."""
        real_id = event.get("run_id")
        if real_id and real_id != entry.run_id and isinstance(real_id, int) and real_id > 0:
            # Re-key from tmp negative id to the DB-assigned positive id.
            old_id = entry.run_id
            entry.run_id = real_id
            self._entries[real_id] = entry
            if old_id in self._entries and self._entries[old_id] is entry:
                del self._entries[old_id]

        entry.events.append(event)
        for q in entry.queues:
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                log.warning("SSE queue full; dropping event for run %s", entry.run_id)

    async def _on_task_done(self, entry: RunEntry, task: asyncio.Task) -> None:
        entry.finished_at = time.time()
        try:
            result = task.result()
            if not entry.final_event:
                entry.final_event = {"type": "run_completed", "run_id": entry.run_id, "result": result, "ts": time.time()}
                entry.events.append(entry.final_event)
        except asyncio.CancelledError:
            entry.final_event = {"type": "run_cancelled", "run_id": entry.run_id, "ts": time.time()}
            entry.events.append(entry.final_event)
        except Exception as e:
            entry.final_event = {"type": "run_failed", "run_id": entry.run_id, "error": f"{type(e).__name__}: {str(e)[:400]}", "ts": time.time()}
            entry.events.append(entry.final_event)

        for q in list(entry.queues):
            try:
                q.put_nowait(entry.final_event)
                q.put_nowait(None)  # sentinel: end of stream
            except asyncio.QueueFull:
                pass

        # Schedule cleanup
        async def _cleanup() -> None:
            await asyncio.sleep(ENTRY_TTL_SECONDS)
            async with self._lock:
                self._entries.pop(entry.run_id, None)

        asyncio.create_task(_cleanup())

    def get(self, run_id: int) -> RunEntry | None:
        return self._entries.get(run_id)

    async def cancel(self, run_id: int) -> bool:
        entry = self._entries.get(run_id)
        if not entry or entry.task.done():
            return False
        entry.task.cancel()
        return True

    async def subscribe(self, run_id: int) -> AsyncIterator[dict]:
        """Subscribe to events for a run. Yields recent backlog first, then live events."""
        entry = self._entries.get(run_id)
        if not entry:
            return

        q: asyncio.Queue = asyncio.Queue(maxsize=512)
        entry.queues.append(q)

        # Replay backlog
        for evt in list(entry.events):
            await q.put(evt)

        # If already finished, push a terminator
        if entry.finished_at:
            await q.put(None)

        try:
            while True:
                evt = await q.get()
                if evt is None:
                    break
                yield evt
        finally:
            if q in entry.queues:
                entry.queues.remove(q)


_registry: RunRegistry | None = None


def get_registry() -> RunRegistry:
    global _registry
    if _registry is None:
        _registry = RunRegistry()
    return _registry
