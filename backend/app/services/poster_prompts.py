"""
Prompt configuration for the Yas poster-only generator (Create Poster).

Two-stage, Midjourney-style pipeline:
  STAGE 1 — OpenAI gpt-4o expands a short topic into N *distinct* voxel
            scene concepts (JSON), each a hyper-detailed description ending in a
            [TEXT TO RENDER] block. The concepts MUST diverge across explicit
            axes — this is what makes the 4 grid variants look genuinely
            different instead of four renders of one idea.
  STAGE 2 — Gemini gemini-3-pro-image-preview renders each concept into a 1:1
            Instagram poster, with the 42 logo (+ optional reference images)
            sent as ordered image parts.

"Explore" (variations of a chosen poster) reuses the parent's concept text and
anchors on the parent IMAGE — see `VARIATION_REF_INSTRUCTION` / `VARIATION_NUDGES`.

## De-hardcoding (why this file reads the way it does)

Round one dropped the literal examples the old single-concept prompt baked in
("Piscine" -> a swimming pool, "Logic Game" -> an arcade cabinet, floating
cursor / question-mark props). The model anchored to them every time, so every
Piscine poster was the same pool with the same icons and all 4 variants matched.

Round two dropped the rest of the recipe. These prompts used to mandate a
retro-gaming-and-tech universe (CRT monitors, arcade machines, mechanical
keyboards, retro controllers), a toy diorama "photographed in a studio" on a 3/4
isometric camera, optional floating props, and a background of bold diagonal
colour splits under code-syntax overlays. So every poster came back as a
joystick or a keyboard on a little raised slab in the middle of an empty frame,
whatever the topic actually was.

What is left is the definition of voxel art, the five-colour palette, the
typography and [TEXT TO RENDER] contracts, and the diversity mandate. Nothing
here names a subject, a prop, a camera angle or a staging device: Stage 1 is
where the idea is supposed to come from, so it has to be free to have one, and
Stage 2 is told to render what Stage 1 describes instead of a house scene of its
own.

KEEP IT THAT WAY. When a poster comes back wrong the fix is the topic or Stage 1,
not a new "always draw X" / "never draw Y" line in here. Every line like that is
a rule that every future poster then has to obey.

This module is pure string/JSON assembly — no I/O, no DB, no network. The parsing
helpers never raise. Covered by `tests/test_poster_diversity.py`.
"""
from __future__ import annotations

import json
import re
from typing import Any

# Number of distinct concepts a normal Create-Poster batch asks Stage 1 for.
POSTER_CONCEPT_COUNT = 4


# ---------------------------------------------------------------------------
# Shared brand + scene rules (NO hardcoded literal examples)
# ---------------------------------------------------------------------------

# Who a figure is when the topic does not say. This is a default, not a mandate:
# it never asks for a person, it only decides who one is when Stage 1 or Stage 2
# puts one in. A topic that names or describes someone else wins. Both voxel
# stages carry it, and so does the Create-Animation frame, which renders with the
# same poster styles. voxel_classic is a frozen archive and stays as it was.
VOXEL_DEFAULT_FIGURE = (
    "When a person appears and the topic does not say who they are, they are "
    "Emirati: a man in a white kandura and ghutra, or a woman in a black abaya "
    "and shayla, built from cubes like everything else and shown with dignity, "
    "never as a caricature. A topic that names or describes a different person "
    "wins."
)

VOXEL_SCENE_RULES = f"""[WHAT VOXEL ART IS]
- Voxel art is 3D art built out of cubes on a regular grid, the way a Minecraft build or a Lego model is: every form in the picture, a hand, a cloud, a letter, is assembled from visible cubic blocks.
- The blocks read as solid and opaque, with clean matte surfaces (plastic, ceramic, or soft digital clay) and crisp edges. Block-level detail is welcome wherever it earns its place.
- Figures are built the same way as everything else: simple cubic heads, flat square eyes, no photorealistic skin or features.
- {VOXEL_DEFAULT_FIGURE}
- That is the whole style. The subject, the setting, the camera, the framing, the depth, the lighting and the mood are yours to choose. Nothing here prescribes a scene, a prop, a stage or an angle: read the topic and design the picture it deserves.

[INTERPRETATION]
- Read the topic and choose a fresh visual idea for it. A topic can be taken literally, metaphorically, through an environment, through an action, or abstractly. Vary the angle of interpretation between concepts, and never settle on the first obvious object every time.

[STRICT COLOR PALETTE: LIMITED COLORS ONLY]
- THE ONLY ALLOWED COLORS are: Cyan (#00EBFF), Purple (#730FFF), Coral/Orange-Red (#FF5A3C), Dark Slate/Charcoal (#414141), and White (#FFFFFF).
- Do NOT introduce any other colors. Every element uses ONLY these five. Pick 2-3 as the dominant palette per concept (vary which dominates), with the others as accents.

[TEXT LAYOUT & TYPOGRAPHY]
- Extract the core message, date, and CTA from the topic. If a line is missing, OMIT it. Place the text where it works in the composition you designed, and leave the picture readable.
- Bold, large, high-impact, crisp modern sans-serif. Solid colored banner blocks behind text lines are fine where they aid readability. ALL text is pure White (#FFFFFF), never multi-color or rainbow text.

[TEXT TO RENDER CONTRACT]
- Each concept's `prompt` MUST end with this exact block (only the raw text, no font or style notes):

[TEXT TO RENDER]
PLACEMENT: [dynamic text placement]
TEXT_LINE_1: "[Main Headline in ALL CAPS]"
TEXT_LINE_2: "[Date/Time in ALL CAPS - ONLY IF PROVIDED]"
TEXT_LINE_3: "[CTA in ALL CAPS - ONLY IF PROVIDED]"
"""


# ---------------------------------------------------------------------------
# STAGE 1 — gpt-4o: N distinct concepts as JSON
# ---------------------------------------------------------------------------

POSTER_CONCEPTS_SYSTEM = f"""You are the Executive Creative Director for 42 Abu Dhabi's social media design pipeline. Given a single announcement topic, you invent MULTIPLE genuinely different poster concepts at once. Every one of them is voxel art in the brand palette, and past that the idea is entirely yours: no house subject, no house scene, no house layout.

{VOXEL_SCENE_RULES}

[DIVERSITY MANDATE — THIS IS THE WHOLE POINT]
You will produce N concepts for the SAME topic. They must be RADICALLY different from one another — a viewer should never mistake one for another. Before writing, deliberately spread the N concepts across these axes so no two share the same combination:
  1. HERO SUBJECT: a different central object/scene per concept (do NOT reuse the same hero).
  2. INTERPRETATION ANGLE: vary literal vs metaphorical vs environmental vs action vs abstract readings of the topic across the set.
  3. COMPOSITION / LAYOUT: vary framing (centered, off-center, full-bleed, top-heavy, wide vs tight).
  4. DOMINANT PALETTE PAIRING: vary which 2-3 of the five brand colors dominate each concept.
  5. CAMERA ANGLE & ENERGY: vary the angle and the mood (calm/minimal vs busy/energetic).
Also honor the [ALREADY USED — AVOID] list in the user message: never repeat a concept that resembles one already produced.

[OUTPUT FORMAT — STRICT JSON]
Return a single JSON object, nothing else:
{{
  "concepts": [
    {{
      "summary": "<8-12 word signature of this concept: hero + angle + palette>",
      "prompt": "<the FULL hyper-detailed voxel scene description for this concept, ending with the exact [TEXT TO RENDER] block>"
    }}
    // ... exactly N entries, each clearly different per the diversity mandate
  ]
}}
Each `prompt` is self-contained and is what the image model receives — make it vivid and complete."""


def build_concepts_user_message(
    topic: str,
    n: int = POSTER_CONCEPT_COUNT,
    recent_summaries: list[str] | None = None,
) -> str:
    """Compose the Stage-1 user message asking gpt-4o for `n` distinct concepts."""
    topic = (topic or "").strip()
    recents = [s for s in (recent_summaries or []) if (s or "").strip()]
    avoid = (
        "\n".join(f"- {s.strip()}" for s in recents[-12:])
        if recents else "(none yet)"
    )
    return (
        f"[TOPIC]\n{topic}\n\n"
        f"[HOW MANY]\nProduce exactly {n} concepts, each RADICALLY different from the "
        f"others per the diversity mandate. Same brand voxel theme, completely "
        f"different ideas.\n\n"
        f"[ALREADY USED — AVOID repeating any of these concept directions]\n{avoid}\n\n"
        f"[REMINDER]\nReturn STRICT JSON with a \"concepts\" array of {n} "
        f"{{summary, prompt}} objects. Each prompt must end with the [TEXT TO RENDER] block."
    )


def _normalize_summary(s: str) -> str:
    """Lowercase, strip punctuation/extra space — for near-duplicate detection."""
    return re.sub(r"[^a-z0-9 ]+", "", (s or "").lower()).strip()


def parse_concepts(raw: str, n: int = POSTER_CONCEPT_COUNT) -> list[dict]:
    """Parse the Stage-1 JSON into a list of {summary, prompt} dicts.

    Tolerant and NEVER raises:
      - handles ```json fenced blocks and leading/trailing prose,
      - accepts a bare list or a {"concepts": [...]} object,
      - skips entries without a usable `prompt`.
    Returns up to `n` concepts (may return fewer — the caller tops up).
    """
    if not raw or not raw.strip():
        return []
    text = raw.strip()
    # Strip a ```json ... ``` fence if present.
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL | re.IGNORECASE)
    if fence:
        text = fence.group(1).strip()

    data: Any = None
    try:
        data = json.loads(text)
    except Exception:
        # Last resort: grab the first {...} or [...] blob in the string.
        m = re.search(r"(\{.*\}|\[.*\])", text, re.DOTALL)
        if m:
            try:
                data = json.loads(m.group(1))
            except Exception:
                return []
        else:
            return []

    if isinstance(data, dict):
        items = data.get("concepts") or data.get("variants") or data.get("items") or []
    elif isinstance(data, list):
        items = data
    else:
        return []

    out: list[dict] = []
    for it in items:
        if not isinstance(it, dict):
            continue
        prompt = (it.get("prompt") or it.get("scene") or it.get("description") or "").strip()
        if not prompt:
            continue
        summary = (it.get("summary") or it.get("concept") or it.get("title") or "").strip()
        if not summary:
            summary = prompt.split("\n", 1)[0][:80]
        out.append({"summary": summary, "prompt": prompt})
        if len(out) >= n:
            break
    return out


def dedupe_concepts(concepts: list[dict]) -> list[dict]:
    """Drop concepts whose normalized summary repeats one already kept."""
    seen: set[str] = set()
    out: list[dict] = []
    for c in concepts:
        key = _normalize_summary(c.get("summary", ""))
        if key and key in seen:
            continue
        if key:
            seen.add(key)
        out.append(c)
    return out


# ---------------------------------------------------------------------------
# Explore — variations of a chosen poster (image-anchored)
# ---------------------------------------------------------------------------

# Sent immediately before the CHOSEN poster image bytes during Explore.
VARIATION_REF_INSTRUCTION = (
    "CRITICAL: This attached image is the CHOSEN poster the user wants more of. "
    "Produce a CLOSE VARIATION of it — keep the SAME core concept, hero subject, "
    "overall composition, brand palette, and text content. Do NOT redesign it into "
    "a different scene. Change only secondary details as directed below so this "
    "variation feels like a sibling of the chosen poster, not a copy and not a new idea:"
)

# Distinct micro-variation nudges so the 4 Explore children differ from EACH OTHER
# while all staying near the parent. One per child (cycled if N > len).
VARIATION_NUDGES = [
    "Shift the camera angle slightly and tighten the framing on the hero element.",
    "Warm, brighter studio lighting with stronger rim light and softer shadows.",
    "Rearrange the supporting props and floating elements; nudge the dominant palette pairing.",
    "Pull the camera back for a wider diorama view with more negative space around the hero.",
]


def build_variation_user_message(base_prompt: str, nudge: str) -> str:
    """Compose the Stage-2 text for one Explore child: the parent's concept text
    plus a small directed change. The parent image is sent as an image part with
    VARIATION_REF_INSTRUCTION ahead of it."""
    base = (base_prompt or "").strip()
    nudge = (nudge or "").strip()
    return (
        f"{base}\n\n"
        f"[VARIATION DIRECTIVE]\n"
        f"Keep everything above — same concept, hero, layout, palette, and text. "
        f"Apply ONLY this subtle change: {nudge}"
    )


# ---------------------------------------------------------------------------
# STAGE 2 — Gemini image system instruction (brand custodian + renderer)
# ---------------------------------------------------------------------------

GEMINI_STAGE2_PROMPT = f"""You are an expert graphic designer and brand custodian for 42 Abu Dhabi. You are handed a scene description and you render it faithfully as voxel art inside the brand identity. The description decides WHAT is in the picture. These rules decide only how it is built, colored and lettered.

[LOGO FIREWALL & REPLICATION]
- CRITICAL: You must extract and replicate the "42 ABU DHABI" logomark exactly as it appears in the provided local reference logo image (42_logo_white.png).
- The logo MUST remain a completely flat, crisp, 2D vector graphic overlay.
- ABSOLUTELY DO NOT apply any 3D shading, 3D textures, lighting, shadows, or distortions to the logo. Place it small and unobtrusively in a corner.

[STRICT COLOR PALETTE]
The composition is strictly limited to the following primary brand colors. Use them sharply to create a high-contrast, punchy visual output:
- Cyan: #00EBFF
- Purple: #730FFF
- Coral/Orange-Red: #FF5A3C
- Dark Slate/Charcoal: #414141 (Use strictly for solid background elements or text contrast, never as muddy gradients)
- White: #FFFFFF

[WHAT VOXEL ART IS]
- Voxel art is 3D art built out of cubes on a regular grid, the way a Minecraft build or a Lego model is: every form in the picture is assembled from visible cubic blocks.
- The blocks read as solid and opaque, with clean matte surfaces (plastic, ceramic, or soft digital clay) and crisp edges.
- Build the entire frame that way, whatever the description asks for. Figures are cubic too: simple blocky heads, flat square eyes, never photorealistic skin or features.
- {VOXEL_DEFAULT_FIGURE}
- That is the whole style. Subject, setting, composition, camera angle, lighting and mood all come from the scene description. Render the picture it describes, not a standard one of your own.

[TYPOGRAPHY & TEXT HIERARCHY RULES]
- CRITICAL: Scroll to the very bottom of the prompt to find the block labeled "[TEXT TO RENDER]".
- Read the "PLACEMENT" instruction and physically position the text on the poster exactly where it dictates (e.g., Top-Left, Center-Right). Do NOT default to placing it at the bottom.
- Extract and print ONLY the provided TEXT_LINE strings exactly as written. If a line is missing from the prompt, do not invent one.
- DO NOT print the labels (e.g., 'TEXT_LINE_1:') or the 'PLACEMENT' command itself on the image. Only print the text inside the quotes.
- Enforce clean, geometric, tech-oriented typography matching brand standard weights: IBM Plex Sans (Bold/Medium) or Futura PT (Bold/Regular).

[OUTPUT CONSTRAINTS]
- Render the image in the EXACT aspect ratio named in the render directive at the top of the prompt (a single cohesive Instagram poster composition).
- English text only. Never attempt to render Arabic script."""


# ---------------------------------------------------------------------------
# Instruction strings that PRECEDE their image parts in the Stage 2 request.
# ---------------------------------------------------------------------------

# Always sent immediately before the 42 logo image bytes.
LOGO_INSTRUCTION = (
    "Extract this flat 2D logo and place it cleanly in a corner. "
    "Do not apply any 3D effects, shadows, or textures to it:"
)

# ---------------------------------------------------------------------------
# Remove-logo (de-logo) — the INVERSE of adding the 42 mark. Sent immediately
# before a FINISHED artwork's own bytes to re-render it with the brand mark gone
# while keeping everything else identical. One instruction covers both logo
# varieties baked across the platform: the PNG-corner logomark (Create Poster,
# Animation frame, TH bookends) and a model-lettered "42 Abu Dhabi" wordmark
# (Poster+Human). Wording mirrors VARIATION_REF_INSTRUCTION's "keep everything,
# change only what's directed" framing.
# ---------------------------------------------------------------------------

# Sent BEFORE the source image, mirroring VARIATION_REF_INSTRUCTION's proven "this
# is the base, reproduce it" framing. Ending a request ON the image (or framing it
# as "edit and return") makes the image model reply with TEXT and no image — so the
# de-logo request is shaped exactly like the working generate/explore calls: a
# reference instruction, then the image, then a final RENDER directive.
# OWNERSHIP framing is deliberate and load-bearing. The image model reads the old
# "remove the brand mark / logomark / wordmark wherever it appears, inpaint that
# region, leave no ghost" wording as WATERMARK REMOVAL — a flagged misuse category —
# and SOFT-REFUSES ("I'm just a language model and can't help with that",
# finish_reason=STOP, no safety block). Re-establishing that this is the user's OWN
# 42 Abu Dhabi artwork and that we want a logo-free DESIGN VARIANT (not the stripping
# of a third party's mark), and dropping the watermark-removal verbs, gets the model
# to comply far more often. Do not "tidy" this back toward the old phrasing.
REMOVE_LOGO_REF_INSTRUCTION = (
    "CRITICAL: The attached image is a finished poster from OUR OWN 42 Abu Dhabi "
    "design system — we created this artwork and own all of it, including the small "
    "42 badge placed on it. Reproduce it EXACTLY — identical composition, hero "
    "subject, background, palette, lighting, textures, and every piece of text — "
    "applying ONLY the single change directed below."
)

# Sent AFTER the source image as the FINAL render directive (the request must END on
# a 'render an image' instruction, like every working generate/explore call).
REMOVE_LOGO_INSTRUCTION = (
    "Render a clean, logo-free version of OUR OWN poster. Reproduce the full image "
    "again, identical to the base artwork above, with ONE difference: remove the "
    "small 42 / \"42 ABU DHABI\" badge that WE placed on our own design (usually in a "
    "corner) and render that area as the natural continuation of the surrounding "
    "artwork — the same background, texture, gradient, and lighting — as if the badge "
    "had never been part of the layout. This is a routine variant of our own owned "
    "artwork that we are authorised to change, not the removal of anyone else's mark. "
    "Keep everything else identical: composition, subject, background, palette, "
    "lighting, and all other text. Output the image at the EXACT same dimensions and "
    "aspect ratio. Do not add any new logo or text."
)

# System instruction for the de-logo render. Frames the model as an IMAGE GENERATOR
# that produces variants of the USER'S OWN artwork and ALWAYS returns an image — the
# ownership clause is what stops the watermark-removal soft refusal, and the
# "always image, never text" clause stops the conversational text-only replies.
REMOVE_LOGO_SYSTEM = (
    "You are an image-generation model that produces design variants of the user's "
    "OWN artwork. You ALWAYS respond with an image and NEVER with text. The user owns "
    "this poster and its branding and is authorised to request a logo-free version of "
    "it; treat that as a normal creative edit, not as removing a third party's "
    "watermark. Faithfully reproduce the artwork and apply only the single change "
    "instructed — here, rendering the layout without the small 42 badge — while "
    "preserving everything else exactly. Never introduce any new logo or text."
)

# ---------------------------------------------------------------------------
# Refine — re-render a FINISHED artwork applying ONLY a user-described change
# ("make the title bigger", "change the background to blue"). Same proven request
# shape as the de-logo / explore paths (reference instruction → source image →
# FINAL render directive, with an image-generation system prompt), so the model
# returns an image rather than a conversational text reply. The only difference
# from de-logo is that the final directive carries the USER's free-text change
# instead of the fixed "remove the 42 mark" instruction.
# ---------------------------------------------------------------------------

# Sent BEFORE the source image — same framing as REMOVE_LOGO_REF_INSTRUCTION.
REFINE_REF_INSTRUCTION = (
    "CRITICAL: The attached image is the finished base artwork. Reproduce it EXACTLY "
    "— identical composition, hero subject, background, palette, lighting, textures, "
    "and every piece of text — changing ONLY the specific detail(s) the user requests "
    "below."
)


def refine_instruction(change: str) -> str:
    """The FINAL render directive for a refine: re-render the base artwork above with
    ONLY the user's requested change applied. The request must END on this 'render an
    image' instruction (like every working generate/explore/de-logo call) so the model
    outputs an image. ``change`` is the user's free text (already stripped/capped by the
    caller); it is quoted into the directive and bracketed by "change nothing else"
    guardrails so an over-broad request can't silently redesign the whole poster."""
    change = (change or "").strip()
    return (
        "RENDER the full image again, keeping it as close as possible to the base "
        "artwork above, and apply ONLY this change the user asked for:\n\n"
        f"\"{change}\"\n\n"
        "Change nothing else — keep the same composition, hero subject, background, "
        "palette, lighting, and any text the change does not explicitly mention. Output "
        "the image at the EXACT same dimensions and aspect ratio. Never add any new "
        "logo, watermark, or unrequested text."
    )


# System instruction for a refine render — same image-generation framing as
# REMOVE_LOGO_SYSTEM (ALWAYS an image, NEVER a text reply), generalised to "apply the
# single change the user requests" instead of the fixed de-logo edit.
REFINE_SYSTEM = (
    "You are an image-generation model. You ALWAYS respond with an image and NEVER "
    "with text. You faithfully reproduce the artwork you are given and apply only the "
    "single change the user requests, preserving everything else exactly. Never "
    "introduce any new logo, watermark, or unrequested text."
)

# Appended to the FINAL render directive on a single ESCALATED retry, used only
# after an image *edit* (de-logo / refine) came back as TEXT instead of an image
# (a non-blocked "no image" reply — see GeminiNoImageError). The model's
# text-vs-image choice is prompt-sensitive, so re-issuing the identical request is
# futile; a blunter, restructured "image only" contract recovers most stubborn
# cases. Kept separate (not folded into the base directive) so the normal first
# attempt stays byte-identical to the proven, well-tested prompt.
IMAGE_ONLY_RETRY_ENFORCER = (
    "\n\nOUTPUT CONTRACT — READ CAREFULLY: You MUST return the rendered IMAGE and "
    "nothing else. Do NOT respond with text, an explanation, an apology, a refusal, "
    "a question, or a description of what you would do. Render the edited image now "
    "and return it as image data."
)


# Sent before reference image bytes when image_usage == "style" (default).
STYLE_REF_INSTRUCTION = (
    "Use this image strictly as a STYLE REFERENCE. Replicate its exact 3D visual "
    "style, textures, high-contrast lighting, and rendering quality in the "
    "generated poster. Do NOT place this image directly in the poster."
)

# Sent before reference image bytes when image_usage == "background".
BACKGROUND_REF_INSTRUCTION = (
    "CRITICAL: The user wants this attached image used as the BACKGROUND of the "
    "poster. You MUST place this actual image as the full background layer behind "
    "all other elements. Overlay the voxel scene, text, and logo ON TOP of this "
    "background image. Do NOT ignore, replace, or reimagine this background — use "
    "the actual attached image."
)
