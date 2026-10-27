"""Pydantic shapes for the Producer Agent API."""
from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field


class RunCreate(BaseModel):
    mode: Literal["auto", "user_idea", "reference"]
    title: Optional[str] = Field(default=None, max_length=200)
    video_type: Optional[str] = Field(default=None, max_length=80)
    # v6: per-run dialogue language for talking_head / voiceover_narrative
    # video_types. Default "en" applied server-side when None. Ignored for
    # non-speech video_types (kept harmlessly in metadata).
    dialogue_language: Optional[Literal["en", "ar", "mixed"]] = None


class GenerateIdeasRequest(BaseModel):
    count: int = Field(default=5, ge=1, le=8)


class UserIdeaRequest(BaseModel):
    rough_idea: str = Field(min_length=3, max_length=4000)


class IdeaDecision(BaseModel):
    action: Literal["approve", "decline", "retry"]
    reason: Optional[str] = Field(default=None, max_length=4000)


class ScriptDecision(IdeaDecision):
    pass


class VideoDecision(IdeaDecision):
    pass


class GenerateScriptRequest(BaseModel):
    retry_feedback: Optional[str] = Field(default=None, max_length=4000)
    # v5.1: pre-generation knobs. None = let the LLM choose.
    # scene_count is clamped to [1, MAX_SCENES_PER_SCRIPT] in generate_script.
    scene_count: Optional[Literal[1, 2, 3]] = None
    # scene_duration_mode "auto" lets the LLM vary per-scene; "4"/"6"/"8"
    # force a uniform duration on every scene.
    scene_duration_mode: Optional[Literal["auto", "4", "6", "8"]] = "auto"


class GenerateVideoRequest(BaseModel):
    use_premium_model: bool = False
    with_audio: bool = False
    duration_sec: Optional[int] = Field(default=None, ge=2, le=8)
    aspect_ratio: Optional[str] = Field(default=None)  # "16:9" / "9:16"
    # When supplied, overrides the script's stored reference_image_paths.
    # Frontend passes a single path here when the user attaches an
    # auto-generated preview image to a non-reference-mode video.
    reference_image_paths: Optional[list[str]] = None
    # v5: when True (default), and no explicit/script-stored reference image
    # is available, auto-generate a brand-grounded still via Nano Banana
    # before firing Veo. Highest-leverage brand-fidelity lever.
    auto_reference: bool = True
    # v5.1: target scene_index when the script is multi-scene. 1-based.
    # Defaults to 1 (legacy single-scene path). render-all uses its own
    # request shape (RenderAllScenesRequest).
    scene_index: int = Field(default=1, ge=1, le=3)


class PreviewImageRequest(BaseModel):
    user_text: Optional[str] = Field(default=None, max_length=2000)
    aspect_ratio: Optional[str] = Field(default=None)


class RefineReferenceRequest(BaseModel):
    """Image-to-image variation request — works on any reference image."""
    user_text: Optional[str] = Field(default=None, max_length=2000)
    aspect_ratio: Optional[str] = Field(default=None)


class ComposeRequest(BaseModel):
    """Stitch an approved extension with its parent into one MP4 via Remotion."""
    crossfade_sec: Optional[float] = Field(default=None, ge=0, le=2)


class CompositionDecision(BaseModel):
    action: Literal["approve", "decline", "retry"]
    reason: Optional[str] = Field(default=None, max_length=4000)


class ReferenceDecision(BaseModel):
    action: Literal["approve", "decline"]
    reason: Optional[str] = Field(default=None, max_length=4000)


class ReferenceIdeaRequest(BaseModel):
    user_notes: Optional[str] = Field(default=None, max_length=2000)


class ExtendVideoRequest(BaseModel):
    """Generate an ~8s continuation that picks up from the source video's
    final frame. `user_text` (optional) tells the model what should happen
    next; if blank, it continues the action naturally.
    """
    user_text: Optional[str] = Field(default=None, max_length=2000)
    use_premium_model: bool = False
    with_audio: bool = False


class RenderAllScenesRequest(BaseModel):
    """v5.1: fire Veo for every scene of a script in parallel.

    The render_all_scenes phase enforces a hard per-script cost cap from
    `config.producer_max_script_cost_usd` BEFORE submitting any LROs.
    Auto-reference defaults to True so each scene gets its own brand-grounded
    Nano Banana still as a visual anchor.
    """
    with_audio: bool = False
    use_premium_model: bool = False
    aspect_ratio: Optional[str] = Field(default=None)
    auto_reference: bool = True


class RenderAllScenesEstimateRequest(BaseModel):
    """Cost preview WITHOUT firing anything. Same knobs as RenderAllScenesRequest."""
    with_audio: bool = False
    use_premium_model: bool = False
    # When True (matches the render-all default), the estimate factors in the
    # Veo 3.1 reference_to_video duration lock — every scene's effective
    # duration becomes 8s, regardless of what the LLM picked.
    auto_reference: bool = True


class ComposeAllScenesRequest(BaseModel):
    """Stitch all ready scenes of a script into one MP4 via Remotion."""
    crossfade_sec: Optional[float] = Field(default=None, ge=0, le=2)


class BatchVideoRequest(BaseModel):
    """Fire Veo for every approved script on the run that has no live video.

    `script_ids` (optional) narrows the batch to a specific subset.
    """
    with_audio: bool = False
    use_premium_model: bool = False
    duration_sec: Optional[int] = Field(default=None, ge=2, le=8)
    aspect_ratio: Optional[str] = Field(default=None)
    script_ids: Optional[list[int]] = None
