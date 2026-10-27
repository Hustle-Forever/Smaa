"""Pydantic shapes for /api/agents/website-ingest/*."""
from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, Field, conint


class RunRequest(BaseModel):
    target_url: Optional[str] = Field(default=None, description="Override TARGET_URL for this run")
    target_domain: Optional[str] = Field(default=None, description="Override TARGET_DOMAIN")
    max_pages: Optional[conint(ge=1, le=10000)] = Field(  # type: ignore[valid-type]
        default=None, description="Cap how many pending pages get fetched"
    )
    skip_phases: Optional[list[str]] = Field(
        default=None, description="Phase names to skip (e.g. ['video','synthesize'])"
    )


class RunStartResponse(BaseModel):
    run_id: int
    status: str  # 'started'


class PhaseStatus(BaseModel):
    name: str
    processed: int = 0
    skipped: int = 0
    failed: int = 0
    total: int = 0
    notes: Optional[str] = None
    extra: dict[str, Any] = Field(default_factory=dict)


class RunSummary(BaseModel):
    id: int
    target_domain: str
    sitemap_url: Optional[str] = None
    status: str
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    pages_found_count: int = 0
    pages_inserted_count: int = 0
    errors_count: int = 0
    notes: Optional[str] = None


class RunDetail(RunSummary):
    phases: dict[str, PhaseStatus] = Field(default_factory=dict)
    cancel_requested: bool = False
