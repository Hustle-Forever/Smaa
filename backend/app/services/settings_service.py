"""Read brand_profile_settings + derive runtime Hints.

The singleton `brand_profile_settings` row holds the user's tuning knobs:

  - mode: 'auto' | 'manual'
  - weight_website / weight_uploads          (source mix)
  - weight_text / weight_visual / weight_audio  (modality emphasis)
  - weight_voice / weight_visual_identity / weight_themes  (output emphasis)
  - model / temperature / max_tokens         (synthesis tuning)
  - include_website / include_uploads / include_images / include_videos
  - custom_instructions

`build_hints(settings)` translates those columns into the actual runtime
behaviour:

  - per-source LIMITs for the Get Brand Data SQL in synthesis
  - emphasis_hint / instructions_hint strings spliced into the synthesis prompt
  - skip_images / skip_videos: phases short-circuit to no-op when False
  - image_processing_cap / page_processing_cap: per-phase row caps that scale
    with weight_visual / weight_text
  - image_prompt_preamble: extra text spliced into vision_describe prompts so
    descriptions emphasise visual identity when the user pushes that lever

In `mode='auto'` the weights are ignored — historical defaults are emitted
byte-identically to the n8n original.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from ..db import get_conn

log = logging.getLogger("archivist.settings")

# Budget allocations used in mode='auto'. Mirror the n8n Build Hints defaults.
BUDGET_AUTO_PAGES = 12
BUDGET_AUTO_IMAGES = 10
BUDGET_AUTO_VIDEOS = 5
BUDGET_AUTO_UPLOADS = 8

# Total budget for source mix (website + uploads).
BUDGET_TOTAL_MIX = 30

# Base processing caps per run. weight_* scales these up.
BASE_PAGES_PER_RUN = 50
BASE_IMAGES_PER_RUN = 60

DEFAULT_IMAGE_PROMPT = (
    "Describe this image in 2-3 sentences focusing on: subject, composition, "
    "mood, color palette, and what it communicates about the brand. "
    "Reference any visible text."
)

VISUAL_EMPHASIS_PREAMBLE = (
    "PRIORITY: visual identity. Lead with the dominant colours, lighting, "
    "framing, and stylistic choices. Treat the depicted subject as secondary "
    "context. Identify recurring visual motifs that signal a brand aesthetic."
)


@dataclass
class Settings:
    """Raw row from brand_profile_settings, typed."""
    id: int
    mode: str
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
    custom_instructions: str | None
    metadata: dict[str, Any]

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> "Settings":
        return cls(
            id=int(row["id"]),
            mode=row["mode"],
            weight_website=int(row["weight_website"]),
            weight_uploads=int(row["weight_uploads"]),
            weight_text=int(row["weight_text"]),
            weight_visual=int(row["weight_visual"]),
            weight_audio=int(row["weight_audio"]),
            weight_voice=int(row["weight_voice"]),
            weight_visual_identity=int(row["weight_visual_identity"]),
            weight_themes=int(row["weight_themes"]),
            model=row["model"],
            temperature=float(row["temperature"]),
            max_tokens=int(row["max_tokens"]),
            include_videos=bool(row["include_videos"]),
            include_images=bool(row["include_images"]),
            include_uploads=bool(row["include_uploads"]),
            include_website=bool(row["include_website"]),
            custom_instructions=row.get("custom_instructions"),
            metadata=row.get("metadata") or {},
        )


@dataclass
class Hints:
    """What each phase needs to know about user preferences."""
    # Synthesis source limits
    limits: dict[str, int] = field(default_factory=dict)

    # Synthesis prompt fragments
    emphasis_hint: str = ""
    instructions_hint: str | None = None

    # Phase short-circuits
    skip_images: bool = False
    skip_videos: bool = False

    # Per-phase row caps
    image_processing_cap: int = BASE_IMAGES_PER_RUN
    page_processing_cap: int = BASE_PAGES_PER_RUN

    # Vision-describe prompt preamble (used by image + video frame describe)
    image_prompt_preamble: str = ""
    image_prompt: str = DEFAULT_IMAGE_PROMPT

    # Synthesis model tuning
    model: str = "gpt-4o"
    temperature: float = 0.3
    max_tokens: int = 3000

    # Carries the originating mode for logging
    mode: str = "auto"


# ---------------------------------------------------------------------------
# Load
# ---------------------------------------------------------------------------

async def load_settings() -> Settings:
    async with get_conn() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT * FROM brand_profile_settings ORDER BY id ASC LIMIT 1"
            )
            row = await cur.fetchone()
    if row is None:
        raise RuntimeError("brand_profile_settings singleton row missing")
    return Settings.from_row(row)


# ---------------------------------------------------------------------------
# Hints
# ---------------------------------------------------------------------------

def _normalize(values: dict[str, int]) -> dict[str, float]:
    """Convert relative weights to fractions summing to 1.0. Empty/zero → equal."""
    total = sum(values.values())
    if total <= 0:
        n = max(1, len(values))
        return {k: 1.0 / n for k in values}
    return {k: v / total for k, v in values.items()}


def _allocate(total: int, weights: dict[str, float]) -> dict[str, int]:
    """Distribute total across keys by weight; round + redistribute the remainder."""
    raw = {k: total * w for k, w in weights.items()}
    rounded = {k: int(v) for k, v in raw.items()}
    deficit = total - sum(rounded.values())
    # Hand the remainder to keys with the largest fractional part
    fractional = sorted(((raw[k] - rounded[k], k) for k in raw), reverse=True)
    for _, k in fractional[:max(0, deficit)]:
        rounded[k] += 1
    return rounded


def build_hints(s: Settings) -> Hints:
    """Translate Settings -> runtime Hints. Honours the auto/manual contract."""
    if s.mode == "auto":
        return _build_hints_auto(s)
    return _build_hints_manual(s)


def _build_hints_auto(s: Settings) -> Hints:
    """Historical defaults; behaviour byte-identical to pre-settings n8n runs."""
    return Hints(
        limits={
            "pageSummaries": BUDGET_AUTO_PAGES,
            "imageDescriptions": BUDGET_AUTO_IMAGES,
            "videoSummaries": BUDGET_AUTO_VIDEOS,
            "ownerDocs": BUDGET_AUTO_UPLOADS,
        },
        emphasis_hint="",
        instructions_hint=s.custom_instructions,
        skip_images=not s.include_images,
        skip_videos=not s.include_videos,
        image_processing_cap=BASE_IMAGES_PER_RUN,
        page_processing_cap=BASE_PAGES_PER_RUN,
        image_prompt=DEFAULT_IMAGE_PROMPT,
        image_prompt_preamble="",
        model=s.model,
        temperature=s.temperature,
        max_tokens=s.max_tokens,
        mode="auto",
    )


def _build_hints_manual(s: Settings) -> Hints:
    """Weights drive everything in manual mode."""

    # -- 1. Source mix: distribute BUDGET_TOTAL_MIX across (website, uploads).
    mix_weights = _normalize({"website": s.weight_website, "uploads": s.weight_uploads})
    mix_alloc = _allocate(BUDGET_TOTAL_MIX, mix_weights)

    # If a source is excluded, redirect its budget to the other source.
    if not s.include_website:
        mix_alloc["uploads"] += mix_alloc.pop("website", 0)
        mix_alloc["website"] = 0
    if not s.include_uploads:
        mix_alloc["website"] += mix_alloc.pop("uploads", 0)
        mix_alloc["uploads"] = 0

    website_budget = mix_alloc.get("website", 0)
    uploads_budget = mix_alloc.get("uploads", 0)

    # -- 2. Modality emphasis: split website_budget across text/visual/audio.
    modality_weights = _normalize({
        "text": s.weight_text,
        "visual": s.weight_visual,
        "audio": s.weight_audio,
    })
    modality_alloc = _allocate(website_budget, modality_weights)

    # Inclusion gates
    page_limit = modality_alloc["text"]
    image_limit = modality_alloc["visual"] if s.include_images else 0
    video_limit = modality_alloc["audio"] if s.include_videos else 0

    # Doubling rule (mirrored from n8n Build Hints): images are denser per row
    # than page summaries, so a bigger raw multiplier gets visual emphasis to
    # actually swell the prompt budget. Skip when image_limit is 0.
    image_limit_prompt = image_limit * 2

    # -- 3. Output emphasis: percentages for the synthesis prompt.
    output_pcts = _normalize({
        "voice": s.weight_voice,
        "visual_identity": s.weight_visual_identity,
        "themes": s.weight_themes,
    })

    emphasis_hint = (
        f"OUTPUT EMPHASIS — bias the brand profile toward: "
        f"voice {int(round(output_pcts['voice'] * 100))}%, "
        f"visual identity {int(round(output_pcts['visual_identity'] * 100))}%, "
        f"themes {int(round(output_pcts['themes'] * 100))}%."
    )

    # -- 4. Per-phase row caps for the website ingest itself (not synthesis).
    # Scale linearly with the relevant weight: weight_visual=100 → 3× base.
    image_cap = int(BASE_IMAGES_PER_RUN * (1.0 + s.weight_visual / 50.0))
    page_cap = int(BASE_PAGES_PER_RUN * (1.0 + s.weight_text / 50.0))

    # -- 5. Vision prompt preamble: stronger when visual_identity is emphasised.
    preamble = ""
    image_prompt = DEFAULT_IMAGE_PROMPT
    if s.weight_visual_identity >= 50 or s.weight_visual >= 60:
        preamble = VISUAL_EMPHASIS_PREAMBLE
        image_prompt = f"{VISUAL_EMPHASIS_PREAMBLE}\n\n{DEFAULT_IMAGE_PROMPT}"

    return Hints(
        limits={
            "pageSummaries": page_limit,
            "imageDescriptions": image_limit_prompt,
            "videoSummaries": video_limit,
            "ownerDocs": uploads_budget,
        },
        emphasis_hint=emphasis_hint,
        instructions_hint=s.custom_instructions,
        skip_images=(not s.include_images) or image_limit == 0,
        skip_videos=(not s.include_videos) or video_limit == 0,
        image_processing_cap=max(1, image_cap),
        page_processing_cap=max(1, page_cap),
        image_prompt_preamble=preamble,
        image_prompt=image_prompt,
        model=s.model,
        temperature=s.temperature,
        max_tokens=s.max_tokens,
        mode="manual",
    )
