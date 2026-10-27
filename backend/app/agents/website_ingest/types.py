"""Shared types for website-ingest phases."""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class PhaseResult:
    """Returned by every phase so the orchestrator can write progress."""
    name: str
    processed: int = 0
    skipped: int = 0
    failed: int = 0
    total: int = 0
    notes: str | None = None
    extra: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "processed": self.processed,
            "skipped": self.skipped,
            "failed": self.failed,
            "total": self.total,
            "notes": self.notes,
            "extra": self.extra,
        }


class CancellationError(RuntimeError):
    """Raised by phases when the run's cancel flag has been set."""


@dataclass
class PhaseContext:
    """Shared context passed to every phase."""
    run_id: int
    target_url: str
    target_domain: str
    hints: object  # Hints from settings_service; quoted to avoid import cycle

    # Callbacks (set by the orchestrator)
    is_cancelled: callable = None  # () -> bool
    report_progress: callable = None  # (phase: str, processed: int, total: int) -> None

    # Free-form dict for agent-level flags that need to reach phases
    # (e.g. force_re_extract for the brand-doc-ingest backfill).
    metadata: dict = field(default_factory=dict)
