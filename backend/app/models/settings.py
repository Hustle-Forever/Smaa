"""Pydantic shapes for /api/settings."""
from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field, conint


class SettingsResponse(BaseModel):
    id: int
    mode: Literal["auto", "manual"]
    weight_website: int
    weight_uploads: int
    weight_text: int
    weight_visual: int
    weight_audio: int
    weight_voice: int
    weight_visual_identity: int
    weight_themes: int
    model: str
    temperature: float
    max_tokens: int
    include_videos: bool
    include_images: bool
    include_uploads: bool
    include_website: bool
    custom_instructions: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


class SettingsPatch(BaseModel):
    """Whitelisted, range-checked subset of fields the client may PUT."""
    mode: Optional[Literal["auto", "manual"]] = None
    weight_website: Optional[conint(ge=0, le=100)] = None  # type: ignore[valid-type]
    weight_uploads: Optional[conint(ge=0, le=100)] = None  # type: ignore[valid-type]
    weight_text: Optional[conint(ge=0, le=100)] = None  # type: ignore[valid-type]
    weight_visual: Optional[conint(ge=0, le=100)] = None  # type: ignore[valid-type]
    weight_audio: Optional[conint(ge=0, le=100)] = None  # type: ignore[valid-type]
    weight_voice: Optional[conint(ge=0, le=100)] = None  # type: ignore[valid-type]
    weight_visual_identity: Optional[conint(ge=0, le=100)] = None  # type: ignore[valid-type]
    weight_themes: Optional[conint(ge=0, le=100)] = None  # type: ignore[valid-type]
    model: Optional[Literal["gpt-4o", "gpt-4o-mini", "gpt-4-turbo"]] = None
    temperature: Optional[float] = Field(default=None, ge=0, le=2)
    max_tokens: Optional[conint(ge=500, le=16000)] = None  # type: ignore[valid-type]
    include_videos: Optional[bool] = None
    include_images: Optional[bool] = None
    include_uploads: Optional[bool] = None
    include_website: Optional[bool] = None
    custom_instructions: Optional[str] = Field(default=None, max_length=4000)
