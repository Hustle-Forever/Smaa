"""
Style presets for the Yas "Create Poster" generator.

## What this is

Create Poster renders a Midjourney-style 4-up grid in two stages: gpt-4o expands a
topic into N *distinct* scene concepts (JSON), then Nano Banana (Gemini Pro) renders
each into a 1:1 Instagram poster. Historically the look was locked to ONE aesthetic —
the retro-voxel diorama in ``poster_prompts.py``.

This module turns that single hardcoded style into a small **registry of selectable
presets**, mirroring the Create-Animation registry (``yas_animation_styles.py``).
Each preset swaps the two prompt surfaces the poster pipeline feeds in — the
Stage-1 concepts system (gpt-4o) and the Stage-2 Gemini render system — plus its own
Explore variation nudges. Nothing else in the pipeline changes.

Four presets ship today:

  | key           | look                                                        |
  |---------------|-------------------------------------------------------------|
  | voxel         | voxel art, scene left to Stage 1 (DEFAULT)                  |
  | voxel_classic | the original voxel recipe: retro-gaming props, staged        |
  | pattern       | bold full-bleed single brand pattern (Ghutra / Map) on a split |
  | popup         | code-centric OS-window cards (dotted title bar, <tag/> titles) |

## Posters carry the FULL message (not a title card)

The Create-Animation popup/pattern presets are short intro/outro *title cards* — one
headline + an optional subtitle. A POSTER must carry everything: headline, date,
location, details, and a call-to-action. So the poster presets do NOT reuse the
animation prompts; they embed ``POSTER_TEXT_CONTRACT`` — an info-rich text block that
instructs the model to render every field present in the topic (headline / subhead /
date-time / location / details / CTA). For ``popup`` specifically, that information
lives *inside* the OS windows (a real event card with a CTA button).

## Two voxel presets, on purpose

``voxel`` references the ``poster_prompts.py`` objects *by identity* (``is`` the
same objects), so whatever that module says is what Create Poster renders, with
nothing restated here to drift out of sync. In 567e401 that prompt lost its
scene recipe: it now defines voxel art, the palette and the text contracts, and
leaves the picture to Stage 1.

``voxel_classic`` is that recipe, kept. ``poster_prompts_classic.py`` holds the
v1 text character for character (see its docstring), so the two looks can be
generated side by side on the same topic and compared. The classic preset is an
archive: edit ``poster_prompts.py`` when a prompt needs to improve, never the
frozen copy, or there is nothing left to compare against.

## Purity contract

Pure string assembly + a frozen-dataclass registry. **No I/O, no DB, no network, no
environment reads.** The import chain (``poster_prompts``) is side-effect-free, so
``tests/test_poster_styles.py`` runs standalone without a DB or credentials. Keep it
that way — do not add side effects here.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

# Voxel symbols — imported UNCHANGED and referenced directly so the voxel preset
# stays byte-identical to the current pipeline.
from app.services.poster_prompts import (
    POSTER_CONCEPTS_SYSTEM,
    GEMINI_STAGE2_PROMPT,
    VARIATION_NUDGES,
)

# The frozen v1 voxel text, unchanged, for the side-by-side preset.
from app.services.poster_prompts_classic import (
    CLASSIC_CONCEPTS_SYSTEM,
    CLASSIC_GEMINI_STAGE2_PROMPT,
)

__all__ = [
    "PosterStyle",
    "POSTER_BRAND_LAW",
    "POSTER_TEXT_CONTRACT",
    "POSTER_STYLES",
    "POSTER_STYLE_KEYS",
    "DEFAULT_POSTER_STYLE",
    "get_poster_style",
]

DEFAULT_POSTER_STYLE = "voxel"


# ===========================================================================
# Shared brand law — the 42 Abu Dhabi style guide, tuned for a POSTER (aspect set per render).
# Embedded verbatim in every NON-voxel preset's Stage-1 + Stage-2 systems.
# (Voxel keeps its own equivalent rules in poster_prompts.py and is untouched.)
# ===========================================================================

POSTER_BRAND_LAW = """\
[42 ABU DHABI BRAND LAW — APPLIES TO EVERY ELEMENT]

[STRICT COLOR PALETTE — DIGITAL PALETTE ONLY]
- The ONLY allowed colors are: Cyan #00EBFF, Purple #730FFF, Coral/Orange-Red #FF5A3C, Charcoal #414141, White #FFFFFF.
- Use a MAXIMUM of TWO dominant colors per composition, plus minimal accents. Approved dominant pairings: purple+cyan, coral+purple, charcoal+coral, charcoal+cyan, charcoal+purple.
- Do NOT introduce any off-palette color (no browns, greens, yellows, pinks, skin tones, or oranges beyond the coral). No muddy gradients.

[BRAND STATEMENT PATTERNS — USE AT MOST ONE]
- Two approved patterns exist.
  (1) GHUTRA: ornamental Ghutra fabric/embroidery as a dense dotted/woven grid with zigzag rows. It is a TEXTILE, so repetition is native to it: it tiles seamlessly and may fill any area at any scale.
  (2) MAP: an abstracted map of ABU DHABI in fine WHITE wireframe line-work. It is the real city, not decoration: the island and its coastline, the Corniche, the grid of streets and blocks, the bridges and channels, simplified to clean lines but still readable as that map. It is a SINGLE OBJECT, so it never repeats: one map, one scale, one rotation, in one piece.
- ONE PATTERN, AND ONE COPY OF IT. Never use both patterns in one composition, and never place a second copy of the same pattern in it: no mirrored map, no tiled or repeated map, no cropped map floating over a full-bleed map, no two maps crossing at different angles. A pattern may be tilted, cropped by the frame, or masked inside a single shape, but what the viewer sees is one continuous piece of artwork.

[TYPOGRAPHY — BRAND LAW]
- Bold, geometric, single-color text. Latin headlines feel like VG5000; body copy feels like Futura PT / IBM Plex Sans (bold geometric sans).
- ALL text is ONE color only (pure White #FFFFFF unless a single brand color is clearly called for on a contrasting block). NEVER outline text or add a stroke, drop shadow, bevel, extrude, or any 3D/shaded treatment. The lettering stays flat.
- Coding-glyph substitution is an on-brand signature for headlines (e.g. Đec⊘de, y0ur, <c0ding/>) — tasteful, never gimmicky.

[POP-UP / WINDOW DEVICE — WHEN USED]
- Clean OS-style windows: a title bar with three dots (●●●) and an X close button, a <tag/>-style window title, flat color blocks and code-style lines inside. Code-centric, clickable-interaction feel.

[ICONOGRAPHY — WHEN USED]
- Pixelated / gamified, single color only. Never stretch, shadow, outline, or multi-color an icon.

[HARD DON'Ts]
- NO people, faces, hands, characters, mascots, or silhouettes anywhere. These are graphic poster designs only.
- Do NOT default to glitch effects, scanlines, or gimmicky "digital" cliches — break the stereotype with clean, confident graphic design.
- Do NOT combine more than two dominant colors. Do NOT combine patterns. Do NOT outline or 3D-shade type. Do NOT use off-palette colors.\
"""


# ===========================================================================
# Shared INFO-RICH text contract — the heart of "a poster carries all the info".
# Every non-voxel concept's `prompt` MUST end with the [TEXT TO RENDER] block.
# ===========================================================================

POSTER_TEXT_CONTRACT = """\
[TEXT LAYOUT & TYPOGRAPHY — A POSTER CARRIES THE FULL MESSAGE]
- This is a POSTER, NOT a short title card. Extract and lay out EVERY piece of information present in the topic — the headline AND the supporting details, the date/time, the location, eligibility/what-it-is, and a call-to-action. Build a clear reading hierarchy (big headline → details → CTA).
- OMIT a field ONLY if the topic genuinely does not provide it — never invent facts, never drop a detail the topic gave you.
- Place text on solid brand-color banner blocks for readability. Bold, large, high-impact, flat, single-color White (#FFFFFF). English only.

[TEXT TO RENDER CONTRACT]
- Each concept's `prompt` MUST end with this exact block (only the raw text in quotes — no font/style notes, no extra commentary):

[TEXT TO RENDER]
PLACEMENT: [describe where each text element sits in the composition]
HEADLINE: "[main headline in ALL CAPS]"
SUBHEAD: "[supporting line in ALL CAPS — ONLY IF provided/implied]"
DATE_TIME: "[date and/or time in ALL CAPS — ONLY IF PROVIDED]"
LOCATION: "[venue / place in ALL CAPS — ONLY IF PROVIDED]"
DETAILS: "[extra info: who / what / eligibility in ALL CAPS — ONLY IF PROVIDED]"
CTA: "[call to action in ALL CAPS, e.g. APPLY NOW / REGISTER TODAY — ONLY IF provided/implied]"\
"""


# Strict JSON output contract — identical shape to poster_prompts.POSTER_CONCEPTS_SYSTEM
# so parse_concepts()/dedupe_concepts() handle every style the same way.
_JSON_OUTPUT_CONTRACT = """\
[OUTPUT FORMAT — STRICT JSON]
Return a single JSON object, nothing else:
{
  "concepts": [
    {
      "summary": "<8-12 word signature of this concept: angle + layout + palette>",
      "prompt": "<the FULL hyper-detailed scene description for this concept, ending with the exact [TEXT TO RENDER] block>"
    }
    // ... exactly N entries, each clearly different per the diversity mandate
  ]
}
Each `prompt` is self-contained and is what the image model receives — make it vivid, complete, and carry ALL the topic's information per the text contract."""


# ===========================================================================
# PRESET — PATTERN (bold full-bleed single brand pattern)
# ===========================================================================

PATTERN_CONCEPTS_SYSTEM = f"""You are the Executive Creative Director for 42 Abu Dhabi's social-media design pipeline, specializing in the brand's bold PATTERN look. Given a single announcement topic, you invent MULTIPLE genuinely different poster concepts at once — each a graphic-driven composition built around ONE full-bleed brand statement pattern that still carries the complete announcement.

[SCENE DESIGN PHILOSOPHY]
- THE PATTERN IS THE HERO: Choose exactly ONE statement pattern, GHUTRA or the ABU DHABI MAP, and use exactly ONE copy of it, bold and full-bleed over the split. It may be tilted, cropped by the frame, or masked inside a single shape, but it stays one continuous piece of artwork: never a second copy layered, mirrored, tiled or crossed over the first, and never both patterns in one poster. Say in the concept WHICH pattern it is and how that ONE copy sits in the frame.
- TWO-COLOR SPLIT: Lay the pattern over a confident two-color brand split (an approved pairing). High contrast, punchy, pure graphic energy.
- NO OBJECTS, NO SCENES: No hero objects, no props, no dioramas. This is flat pattern-and-type design only. A short row of pixel-diamond accents is optional; keep accents minimal.
- INFO LIVES ON BLOCKS: Because a poster must carry the full message, anchor the headline and every detail line on solid brand-color banner blocks layered over the pattern, with a clear hierarchy.

{POSTER_BRAND_LAW}

{POSTER_TEXT_CONTRACT}

[DIVERSITY MANDATE — THIS IS THE WHOLE POINT]
You will produce N concepts for the SAME topic. They must be RADICALLY different from one another — a viewer should never mistake one for another. Deliberately spread the N concepts so no two share the same combination:
  1. PATTERN CHOICE & TREATMENT: vary Ghutra vs Map, and vary how the single copy is treated (tilt, scale, where the frame crops it, or masked inside one shape).
  2. TWO-COLOR SPLIT: vary which approved pairing dominates and how the split is composed (diagonal, vertical, blocked).
  3. HEADLINE PLACEMENT & BLOCK LAYOUT: vary where the headline/detail blocks sit and how the information stack is arranged.
  4. ENERGY: vary the mood (calm/minimal vs busy/energetic) and the scale/crop of the pattern.
Also honor the [ALREADY USED — AVOID] list in the user message: never repeat a concept that resembles one already produced.

{_JSON_OUTPUT_CONTRACT}"""

PATTERN_GEMINI_STAGE2_SYSTEM = f"""You are an expert graphic designer and brand custodian for 42 Abu Dhabi. Render the detailed full-bleed PATTERN composition described into a single premium Instagram poster, strictly enforcing the brand identity.

[LOGO FIREWALL & REPLICATION]
- A reference logo image (42_logo_white.png) is attached. Extract and replicate the "42 ABU DHABI" logomark EXACTLY as it appears there.
- The logo MUST stay a flat, crisp 2D vector overlay. DO NOT apply 3D shading, textures, lighting, or distortion to it. Place it small and unobtrusively in a corner.

[PATTERN RENDERING RULES]
- Render exactly ONE brand pattern, and exactly ONE copy of it, carrying the composition over a two-color brand split.
- GHUTRA is a textile: it repeats, tiles and fills an area, which is what it is for.
- The MAP is a map of ABU DHABI: the island and its coastline, the Corniche, the grid of streets and blocks, the bridges and channels, drawn as fine white line-work and readable as that city rather than as decorative scribble. It is ONE map at ONE scale and ONE rotation. Do NOT draw it twice, do NOT mirror, tile or repeat it, do NOT lay a cropped map over a full-bleed map, and do NOT cross two maps at different angles. If the scene description mentions the map more than once, it is still that same single map.
- Pure flat graphic design: no objects, no scenes, no realistic materials, no glitch/scanline cliches. Never combine the two patterns.

{POSTER_BRAND_LAW}

[TYPOGRAPHY & TEXT — RENDER ALL OF IT]
- Find the "[TEXT TO RENDER]" block at the end of the prompt. Read "PLACEMENT" and position each text element exactly there. Print ONLY the strings inside the quotes — never the field labels ('HEADLINE', 'DATE_TIME', 'CTA', 'PLACEMENT', etc.). If a field is absent, do not invent one.
- This is a poster: render EVERY provided line (headline, subhead, date/time, location, details, CTA) on solid brand-color banner blocks with a clear hierarchy so it stays legible over the pattern. Bold, geometric, FLAT, single-color White (#FFFFFF). Never outline or 3D-shade the type. ENGLISH ONLY — never render Arabic or any non-Latin script. Spell every word exactly as given.

[OUTPUT CONSTRAINTS]
- Render in the EXACT aspect ratio named in the render directive at the top of the prompt — a single cohesive Instagram poster composition (not a collage). No people, faces, hands, or characters."""

_PATTERN_VARIATION_NUDGES = (
    "Switch the brand pattern (Ghutra ↔ Map) or change its tilt while keeping the same message.",
    "Recompose the two-color split (diagonal ↔ vertical ↔ blocked) and swap the dominant pairing.",
    "Change the scale of the one pattern and where the frame crops it; nudge the pixel-diamond accents.",
    "Reposition the headline and detail blocks for a fresh information stack with more negative space.",
)


# ===========================================================================
# PRESET — POP-UP (code-centric OS-window event cards, info-rich)
# ===========================================================================

POPUP_CONCEPTS_SYSTEM = f"""You are the Executive Creative Director for 42 Abu Dhabi's social-media design pipeline, specializing in the brand's CODE-CENTRIC POP-UP look. Given a single announcement topic, you invent MULTIPLE genuinely different poster concepts at once — each built from clean OS-style windows that present the FULL announcement like an interactive event card.

[SCENE DESIGN PHILOSOPHY]
- WINDOWS ARE THE STRUCTURE: Build each card from ONE or TWO clean OS-style windows. Each window has a title bar with three dots (●●●), an X close button, and a <tag/>-style title (e.g. <intro/>, <apply/>, <piscine/>).
- THE WINDOW HOLDS THE WHOLE MESSAGE: Because this is a poster, the window content carries ALL the information — the headline as the window title or a bold line inside, then the date/time, location, and details as code-style body lines, and a clear CTA BUTTON (e.g. a highlighted "APPLY NOW" pill) inside or directly beneath the window. Clear information hierarchy, not clutter.
- CLICKABLE ENERGY: It should feel like an interface moment — a button, a cursor, a <tag/>, a code line. One window MAY contain ONE cropped brand pattern (the Abu Dhabi map or the Ghutra, one of the two, one copy of it).
- NO solid toy objects, NO realistic materials, NO glitch cliches. Clean flat UI graphic design.

{POSTER_BRAND_LAW}

{POSTER_TEXT_CONTRACT}

[DIVERSITY MANDATE — THIS IS THE WHOLE POINT]
You will produce N concepts for the SAME topic. They must be RADICALLY different from one another — a viewer should never mistake one for another. Deliberately spread the N concepts so no two share the same combination:
  1. WINDOW COUNT & ARRANGEMENT: vary ONE vs TWO windows, and vary how they stack / overlap / align.
  2. <tag/> TITLES & CODE FRAMING: vary the window tags and the coding-format treatment of the headline.
  3. INFORMATION HIERARCHY: vary how the headline / details / CTA are distributed across the window(s) and where the CTA button sits.
  4. COLOR PAIRING & CROPPED PATTERN: vary the dominant two-color pairing and whether/which cropped pattern appears in a window.
Also honor the [ALREADY USED — AVOID] list in the user message: never repeat a concept that resembles one already produced.

{_JSON_OUTPUT_CONTRACT}"""

POPUP_GEMINI_STAGE2_SYSTEM = f"""You are an expert graphic designer and brand custodian for 42 Abu Dhabi. Render the detailed pop-up / OS-window composition described into a single premium Instagram poster, strictly enforcing the brand identity.

[LOGO FIREWALL & REPLICATION]
- A reference logo image (42_logo_white.png) is attached. Extract and replicate the "42 ABU DHABI" logomark EXACTLY as it appears there.
- The logo MUST stay a flat, crisp 2D vector overlay. DO NOT apply 3D shading, textures, lighting, or distortion to it. Place it small and unobtrusively in a corner.

[POP-UP RENDERING RULES]
- Render one or two clean OS-style windows: title bar with three dots (●●●), an X close button, a <tag/>-style title, flat color blocks and code-style lines inside, and a clear CTA button. Code-centric, clickable feel, clear hierarchy.
- One window may hold a CROPPED single brand pattern. Use exactly ONE pattern. Flat UI graphic design — no toy objects, no realistic materials, no glitch/scanline cliches.

{POSTER_BRAND_LAW}

[TYPOGRAPHY & TEXT — RENDER ALL OF IT]
- Find the "[TEXT TO RENDER]" block at the end of the prompt. Read "PLACEMENT" and position each text element exactly there. Print ONLY the strings inside the quotes — never the field labels ('HEADLINE', 'DATE_TIME', 'CTA', 'PLACEMENT', etc.). If a field is absent, do not invent one.
- This is a poster: render EVERY provided line (headline, subhead, date/time, location, details, CTA) INSIDE or around the window(s) as the event card — the CTA becomes a highlighted button. Render <tag/> window titles and coding-glyph substitutions EXACTLY as written. Bold, geometric, FLAT, single-color White (#FFFFFF) on solid brand blocks. Never outline or 3D-shade the type. ENGLISH ONLY — never render Arabic or any non-Latin script. Spell every word exactly as given.

[OUTPUT CONSTRAINTS]
- Render in the EXACT aspect ratio named in the render directive at the top of the prompt — a single cohesive Instagram poster composition (not a collage). No people, faces, hands, or characters."""

_POPUP_VARIATION_NUDGES = (
    "Re-arrange the windows (one ↔ two, restack/overlap) while keeping the same message and CTA.",
    "Change the <tag/> window titles and the coding-format treatment of the headline.",
    "Redistribute the info across the window(s) and reposition the CTA button.",
    "Swap the dominant two-color pairing and the cropped pattern inside a window.",
)


# ===========================================================================
# Registry
# ===========================================================================

@dataclass(frozen=True)
class PosterStyle:
    """One selectable Create-Poster preset.

    Bundles the prompt surfaces the poster pipeline feeds in plus the per-style
    Explore nudges. ``voxel`` references the ``poster_prompts.py`` objects by
    identity; ``voxel_classic`` references the frozen v1 copy of them.
    """
    key: str
    label: str
    description: str
    concepts_system: str          # Stage-1 gpt-4o system (JSON, N distinct concepts)
    gemini_stage2_system: str     # Stage-2 Nano Banana render system
    variation_nudges: tuple[str, ...]   # per-style Explore micro-nudges


POSTER_STYLES: dict[str, PosterStyle] = {
    "voxel": PosterStyle(
        key="voxel",
        label="Voxel",
        description="Retro-voxel toy diorama (default).",
        concepts_system=POSTER_CONCEPTS_SYSTEM,        # original — byte-identical
        gemini_stage2_system=GEMINI_STAGE2_PROMPT,     # original — byte-identical
        variation_nudges=tuple(VARIATION_NUDGES),      # original nudges
    ),
    "voxel_classic": PosterStyle(
        key="voxel_classic",
        label="Voxel classic",
        description="The original voxel recipe: retro-gaming props staged like a toy set.",
        concepts_system=CLASSIC_CONCEPTS_SYSTEM,            # frozen v1 text
        gemini_stage2_system=CLASSIC_GEMINI_STAGE2_PROMPT,  # frozen v1 text
        variation_nudges=tuple(VARIATION_NUDGES),           # same Explore nudges
    ),
    "pattern": PosterStyle(
        key="pattern",
        label="Pattern",
        description="Bold full-bleed single brand pattern (Ghutra or Map) over a two-color split.",
        concepts_system=PATTERN_CONCEPTS_SYSTEM,
        gemini_stage2_system=PATTERN_GEMINI_STAGE2_SYSTEM,
        variation_nudges=_PATTERN_VARIATION_NUDGES,
    ),
    "popup": PosterStyle(
        key="popup",
        label="Pop-up",
        description="Code-centric OS-window event cards (●●● title bar, <tag/> titles) carrying the full message + a CTA button.",
        concepts_system=POPUP_CONCEPTS_SYSTEM,
        gemini_stage2_system=POPUP_GEMINI_STAGE2_SYSTEM,
        variation_nudges=_POPUP_VARIATION_NUDGES,
    ),
}

POSTER_STYLE_KEYS: tuple[str, ...] = tuple(POSTER_STYLES.keys())


def get_poster_style(key: Optional[str]) -> PosterStyle:
    """Resolve a style key to its preset, with a safe voxel fallback.

    Unknown keys, ``None``, and legacy records all resolve to voxel — so a typo
    or an old poster row never breaks generation. Keys are matched
    case-insensitively.
    """
    return POSTER_STYLES.get((key or "").strip().lower(), POSTER_STYLES[DEFAULT_POSTER_STYLE])
