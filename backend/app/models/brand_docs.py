"""Pydantic shapes for /api/agents/brand-doc-ingest/*."""
from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field, conint


class UploadedDocSummary(BaseModel):
    id: int
    doc_kind: Literal["pdf", "docx", "text", "image", "free_text", "video"]
    source_filename: Optional[str] = None
    bytes: Optional[int] = None
    upload_status: str


class BrandDocIngestRunResponse(BaseModel):
    run_id: int
    status: str  # 'started'
    inserted_doc_ids: list[int] = Field(default_factory=list)
    skipped_files: list[str] = Field(default_factory=list)


class BrandDocIngestRunRequest(BaseModel):
    """Body for re-running ingest without new uploads.

    Re-processes any pending brand_documents rows and re-runs synthesis.
    Setting `force_re_extract` re-runs the brand_extract + pdf_pages phases
    on every doc (not just pending) — used to backfill after a pipeline change.
    """
    run_synthesis: bool = True
    skip_phases: Optional[list[str]] = None
    force_re_extract: bool = False


class BrandDocRow(BaseModel):
    id: int
    doc_kind: str
    source_filename: Optional[str] = None
    bytes: Optional[int] = None
    mime_type: Optional[str] = None
    upload_status: str
    uploader_note: Optional[str] = None
    summarized: bool = False
    embedded: bool = False
    described: bool = False
    clip_embedded: bool = False
    text_chars: Optional[int] = None
    preview: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


# ----- Agent 3 -----

class BrandSynthRunRequest(BaseModel):
    notes_suffix: Optional[str] = Field(
        default=None,
        description="Appended to brand_profile_runs.notes after the prefix",
    )
