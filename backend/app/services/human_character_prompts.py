"""
Prompt surfaces for the **Human Character** generator (Ideation → Human Character).

A photorealistic-person counterpart to the Create-Poster pipeline. It reuses the
exact two-stage machinery in ``yas_producer.py`` (gpt-4o concepts → Nano Banana
render) and the generic JSON helpers in ``poster_prompts.py``
(``parse_concepts`` / ``dedupe_concepts``), but swaps in:

  • a Stage-1 "casting director" system that turns a short brief into N DISTINCT
    believable individuals (different faces — casting options), instead of
    radically different poster *scenes*;
  • a Stage-2 portrait-photography render system that is photorealistic and
    carries **no 42 logo and no text** (the poster logo firewall is skipped via
    ``include_logo=False`` in ``_single_stage2``);
  • a describe system used when a chosen candidate is saved to the Assets
    library as a ``character`` asset.

The result is meant to be saved as a reusable **character reference** and picked
up by Talking Head as the on-screen presenter. Kept aspect-neutral: the exact
ratio is set by the render directive prepended in ``_single_stage2``
("RENDER A 1:1 SQUARE IMAGE."), mirroring the poster Stage-2 contract.
"""
from __future__ import annotations

from .poster_styles import PosterStyle

# Five brand-palette/identity constants are intentionally NOT referenced here:
# a human character is a real-looking person, not a branded graphic, so the only
# hard brand tie is "feels native to the 42 Abu Dhabi community".

# ---------------------------------------------------------------------------
# Stage 1 — gpt-4o "casting director": brief → N distinct people (strict JSON)
# ---------------------------------------------------------------------------

HUMAN_CHARACTER_CONCEPTS_SYSTEM = """You are a casting director and character \
designer for 42 Abu Dhabi — a tuition-free, peer-learning coding school in the \
UAE. You turn a short brief describing a KIND of person into a set of distinct, \
believable real individuals who could present a short social video for the brand.

[YOUR JOB]
Given a brief, invent N DISTINCT people who ALL fit it — think of it as casting: \
several different real humans auditioning for the same role. Each is described as \
a complete, self-contained photorealistic PORTRAIT prompt that an image model can \
render on its own with no other context.

[DIVERSITY MANDATE — distinct PEOPLE, not distinct scenes]
- The N candidates must look like DIFFERENT individuals: vary the face, exact \
age within the brief's range, build, skin tone, hairstyle/length, and the precise \
wardrobe and expression.
- BUT stay FAITHFUL to anything the brief fixes. If the brief specifies gender, \
an age band, ethnicity, or a role (e.g. "young Emirati woman, software engineer"), \
EVERY candidate must honour it — you are varying who the person is, not redefining \
the brief.
- When the brief leaves nationality and ethnicity open, cast Emirati nationals by \
default: men in a kandura and ghutra (agal optional), women in an abaya with a \
shayla neatly framing a fully visible face. Still make them DIFFERENT people: vary \
gender (when the brief leaves it open), age within the brief (18-30 when it says \
nothing), build, grooming, glasses, and the colour of the kandura, abaya or \
shayla. Respectful and true to life: no niqab, no sunglasses, nothing covering the \
face, no costume props. Authentic, never stereotyped.
- When the brief names another nationality or ethnicity, or asks for an \
international mix, cast exactly that instead.

[EACH PORTRAIT PROMPT MUST SPECIFY]
- A SINGLE real person, photographed front-facing: head-and-shoulders, squared to \
camera, eyes to the lens, a natural relaxed expression (a warm, approachable look \
is the default — this is a friendly presenter, not a mugshot).
- Concrete identity: apparent age, gender presentation, skin tone, distinct facial \
features, hair, and smart-casual or context-appropriate wardrobe in calm, neutral \
tones.
- Photographic realism: real skin texture and pores, catchlights in the eyes, \
soft natural or studio lighting, shallow but sane depth of field.
- A clean, uncluttered background — a soft neutral studio backdrop or a gently \
blurred real setting. Nothing busy.

[HARD RULES]
- NO text, NO captions, NO logos, NO watermarks, NO graphic overlays anywhere in \
the described image. This is a clean portrait, not a poster.
- Photoreal only — never illustrate, never cartoon/voxel/3D-render, never the \
smooth "AI doll" look.
- English only. No preamble or commentary outside the JSON.

[OUTPUT — STRICT JSON]
Return ONE JSON object: {"concepts": [{"summary": "...", "prompt": "..."}, ...]}.
- "summary" — a short, DISTINCT label for this candidate (e.g. "warm smile, \
cropped curls, charcoal knit"). Summaries must differ from one another.
- "prompt" — the full, vivid, self-contained portrait description the image model \
receives. Restate the person's full physical identity in EACH prompt (the model \
renders each one independently and shares no memory between them).
"""


# The shape Stage 1 comes back in on the Gemini fallback (OpenAI is asked with
# response_format json_object instead). Gemini needs the schema for valid JSON:
# asked in free text it returns the right structure with unescaped quotes
# (see poster_pipeline._GEMINI_CONCEPTS_SCHEMA). Same {"concepts": [...]}
# contract as HUMAN_CHARACTER_CONCEPTS_SYSTEM, read by the same parse_concepts.
HUMAN_CHARACTER_GEMINI_CONCEPTS_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "concepts": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "summary": {"type": "string",
                                "description": "a short label naming what makes this candidate different"},
                    "prompt": {"type": "string",
                               "description": "the full, self-contained photorealistic portrait "
                                              "description of this ONE person"},
                },
                "required": ["summary", "prompt"],
            },
        },
    },
    "required": ["concepts"],
}


def build_human_character_user_message(
    topic: str,
    n: int,
    recent_summaries: list[str] | None = None,
) -> str:
    """Stage-1 user message: ask gpt-4o for `n` distinct casting candidates as
    strict JSON, biased away from recently-generated people (anti-repeat)."""
    topic = (topic or "").strip()
    recents = [s for s in (recent_summaries or []) if (s or "").strip()]
    avoid = (
        "\n".join(f"- {s.strip()}" for s in recents[-12:]) if recents else "(none yet)"
    )
    return (
        f"[BRIEF]\n{topic}\n\n"
        f"[HOW MANY]\nProduce exactly {n} DISTINCT casting candidates — different "
        f"believable individuals who ALL fit the brief. Honour every trait the "
        f"brief fixes (gender, age band, ethnicity, role); vary the rest (face, "
        f"hair, exact wardrobe, expression).\n\n"
        f"[ALREADY USED — AVOID repeating these people / looks]\n{avoid}\n\n"
        f"[REMINDER]\nReturn STRICT JSON: a \"concepts\" array of exactly {n} "
        f"{{summary, prompt}} objects. Each prompt is a complete, self-contained, "
        f"photorealistic FRONT-FACING portrait of ONE person. No text, no logos, "
        f"no overlays anywhere in the image."
    )


# ---------------------------------------------------------------------------
# Stage 2 — Nano Banana render system (photoreal portrait, NO logo, NO text).
# Aspect-neutral: the exact ratio is set by the render directive prepended in
# _single_stage2 ("RENDER A 1:1 SQUARE IMAGE.").
# ---------------------------------------------------------------------------

HUMAN_CHARACTER_STAGE2_SYSTEM = """You are an expert portrait photographer. Your \
sole task is to render ONE photorealistic photograph of a single real human being \
from the description provided, suitable as a clean character reference for a \
talking-head video.

[FRAMING]
- Front-facing portrait: head-and-shoulders, the person squared to camera with \
eyes meeting the lens and a natural, relaxed expression.
- The face is the subject and is fully visible, well-lit, and in sharp focus.

[REALISM — this must look like a real photograph]
- Real, textured skin with natural pores, fine detail, and catchlights in the \
eyes; believable hair; natural colour.
- Soft, flattering lighting (studio softbox or gentle daylight); a shallow but \
natural depth of field.
- AVOID the tell-tale AI look: no airbrushed plastic skin, no waxy "doll" face, \
no over-symmetry, no cinematic glow, halos, or lens flare. Photographic, not \
rendered.

[BACKGROUND]
- Clean and uncluttered: a soft neutral studio backdrop or a gently blurred real \
setting. Nothing busy competes with the face.

[ABSOLUTE PROHIBITIONS]
- NO text, NO words, NO captions, NO numbers anywhere in the image.
- NO logos, NO watermarks, NO brand marks, NO graphic overlays, NO frames or \
badges. This is a bare portrait, never a poster or a branded card.
- Exactly ONE person — no duplicates, no background crowd, no collage or grid.

Render a single cohesive photorealistic portrait in the exact aspect ratio named \
in the render directive at the top of the prompt.
"""


# Explore is intentionally not exposed for human characters (the flow is
# generate → pick → save), but PosterStyle requires a non-empty nudge tuple.
# These describe SUBTLE same-person changes, kept for parity / future use.
HUMAN_CHARACTER_VARIATION_NUDGES: tuple[str, ...] = (
    "Soften the expression into a warmer, friendlier smile.",
    "Shift the key light slightly for gentler, more even facial lighting.",
    "Nudge the head a few degrees toward a natural three-quarter angle.",
    "Swap to a calmer, more neutral wardrobe tone while keeping the same person.",
)


# The "style" object the poster pipeline consumes. NOTE: this is intentionally
# NOT registered in poster_styles.POSTER_STYLES (which is asserted to be exactly
# the four pickable looks) — it is passed directly to _single_stage2 by the
# human-character routes. ``key`` becomes the row's stored ``style`` value.
HUMAN_CHARACTER_STYLE = PosterStyle(
    key="human_character",
    label="Human Character",
    description="Photorealistic front-facing human portrait — a reusable talking-head reference. No logo, no text.",
    concepts_system=HUMAN_CHARACTER_CONCEPTS_SYSTEM,
    gemini_stage2_system=HUMAN_CHARACTER_STAGE2_SYSTEM,
    variation_nudges=HUMAN_CHARACTER_VARIATION_NUDGES,
)


# ---------------------------------------------------------------------------
# Describe — used when a chosen candidate is saved to the Assets library, so the
# asset has a searchable, embeddable description (mirrors the poster describe).
# ---------------------------------------------------------------------------

HUMAN_CHARACTER_DESCRIBE_SYSTEM = (
    "You describe a PHOTO OF A PERSON for a searchable creative-asset library. "
    "In 2-3 plain factual sentences, capture: apparent age range, gender "
    "presentation, skin tone / appearance, hair, expression, wardrobe, and the "
    "background/setting. Be concrete and neutral — this is retrieval metadata, "
    "not marketing copy. No preamble, no quotes; output only the description."
)

HUMAN_CHARACTER_DESCRIBE_USER = (
    "Describe the person in this image for the asset library, per the system "
    "rules. Output only the 2-3 sentence description."
)
