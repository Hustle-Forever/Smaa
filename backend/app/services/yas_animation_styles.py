"""
Style presets for the Yas "Create Animation" static-frame generator.

## What this is

The Create-Animation flow renders a static "hero frame" in two stages
(gpt-4o expands a brief → Gemini renders it with the 42 logo baked in) and
then animates that frame with Veo. Historically the frame was locked to ONE
aesthetic — the retro-voxel / toy-diorama look in ``yas_animation_prompts.py``.

This module turns that single hardcoded style into a small **registry of
selectable presets**. Each preset swaps the Stage-1 system prompt, the Stage-2
system prompt, the Stage-1 user-message layout directive, and the Veo motion
prompt — nothing else in the pipeline changes.

Four presets ship today:

  | key        | look                                                          |
  |------------|---------------------------------------------------------------|
  | voxel      | retro-voxel toy diorama (DEFAULT — unchanged from v1)         |
  | popup      | code-centric OS-window cards (●●● title bar, <tag/> titles)   |
  | pattern    | bold full-bleed single brand pattern (Ghutra / Map)          |
  | asis       | no look at all: for a picture the user brought in             |

``asis`` is the odd one out and deliberately so. The first three describe a
frame WE rendered, so their motion prompts can name the aesthetic ("keep the
chunky voxel look rock-solid"). An imported picture has no such guarantee, and
handing a photograph the voxel prompt asks Veo to voxelise it. ``asis`` names
nothing and only says: whatever this is, keep it, and move it gently. It is not
offered in the Create-Animation style picker because there is nothing to
generate from it.

## The voxel byte-identity contract

The ``voxel`` preset must stay **byte-for-byte identical** to v1. We guarantee
this by importing the original symbols from ``yas_animation_prompts.py`` and
referencing them directly in the registry — the voxel row literally *is* the
original objects. ``yas_animation_prompts.py`` is never edited.

## The shared brand policy

All NON-voxel presets embed ``ANIMATION_COMMON_POLICY`` verbatim in BOTH their
Stage-1 and Stage-2 system prompts. It encodes the 42 Abu Dhabi style guide as
one shared block (palette, ≤2 colors, one-pattern-only, flat single-color type,
no people, English-only, the brand pattern + window + icon devices). Voxel keeps
its own equivalent inline rules (it predates this block) and is left untouched.

## Purity contract

Pure string assembly + a frozen-dataclass registry. **No I/O, no DB, no network,
no environment reads at import time.** The import chain
(``yas_animation_prompts`` → ``yas_poster_prompts`` → ``yas_talking_head_prompts``
for ``scrub_rai_terms``) is likewise side-effect-free, so
``tests/test_yas_animation_styles.py`` runs standalone without a DB or any
credentials. Keep it that way — do not add side effects here.
"""
from __future__ import annotations

from dataclasses import dataclass
from functools import partial
from typing import Callable, Optional

# Voxel symbols — imported UNCHANGED and referenced directly so the voxel preset
# stays byte-identical to v1. ANIMATE_NEGATIVE_PROMPT is re-exported for the router.
# _MOTION_AUDIO_BLOCK / _MOTION_SILENT_BLOCK are module-private constants imported
# by explicit name (legal; only ``import *`` skips underscore names) so the
# non-voxel motion prompts reuse the exact same audio blocks as voxel.
from app.services.yas_animation_prompts import (
    ANIMATION_IMAGE_SYSTEM,
    ANIMATION_RENDER_SYSTEM,
    ANIMATE_NEGATIVE_PROMPT,
    build_animation_image_user_message,
    build_animation_motion_prompt,
    _MOTION_AUDIO_BLOCK,
    _MOTION_SILENT_BLOCK,
)
from app.services.yas_talking_head_prompts import scrub_rai_terms

__all__ = [
    # Re-exported voxel symbols (so the router imports everything from one place)
    "ANIMATION_IMAGE_SYSTEM",
    "ANIMATION_RENDER_SYSTEM",
    "ANIMATE_NEGATIVE_PROMPT",
    "build_animation_image_user_message",
    "build_animation_motion_prompt",
    # New registry surface
    "AnimationStyle",
    "ANIMATION_COMMON_POLICY",
    "ANIMATION_STYLES",
    "ANIMATION_STYLE_KEYS",
    "DEFAULT_STYLE",
    "IMPORTED_STYLE",
    "build_imported_motion_prompt",
    "get_animation_style",
    "build_styled_image_user_message",
    "build_motion_prompt_for_style",
]

DEFAULT_STYLE = "voxel"
# The style stamped on an image the user brought in instead of generating.
IMPORTED_STYLE = "asis"


# ===========================================================================
# The shared brand policy — encodes the 42 Abu Dhabi style guide.
# Embedded verbatim into every NON-voxel preset's Stage-1 + Stage-2 systems.
# ===========================================================================

ANIMATION_COMMON_POLICY = """\
[42 ABU DHABI BRAND LAW — APPLIES TO EVERY ELEMENT]

[STRICT COLOR PALETTE — DIGITAL PALETTE ONLY]
- The ONLY allowed colors are: Cyan #00EBFF, Purple #730FFF, Coral/Orange-Red #FF5A3C, Charcoal #414141, White #FFFFFF.
- Use a MAXIMUM of TWO dominant colors per composition, plus minimal accents. Approved dominant pairings: purple+cyan, coral+purple, charcoal+coral, charcoal+cyan, charcoal+purple.
- Do NOT introduce any off-palette color (no browns, greens, yellows, pinks, skin tones, or oranges beyond the coral). No muddy gradients.

[BRAND STATEMENT PATTERNS — USE AT MOST ONE]
- Three approved patterns exist: (1) MAP — an abstracted Abu Dhabi geolocation map drawn as fine WHITE wireframe line-work; (2) GHUTRA — ornamental Ghutra fabric/embroidery as a dense dotted/woven grid with zigzag rows; (3) ARABIC LETTERS — Arabic letters and punctuation rendered as 3D WIREFRAME tube/pipe forms, seamless and abstract.
- A pattern may be used tilted, deconstructed, cropped, or contained inside a shape.
- NEVER combine two different patterns in one composition. One pattern maximum.

[TYPOGRAPHY — BRAND LAW]
- Latin display/headlines feel like VG5000; communication copy feels like Futura PT (bold geometric sans). Arabic feels like IBM Plex Arabic.
- Bold, geometric, single-color text. ALL text is ONE color only (pure White #FFFFFF unless a single brand color is clearly called for on a contrasting block).
- NEVER outline text. Do NOT add a stroke, drop shadow, bevel, extrude, or any 3D/shaded treatment to the font. The lettering stays flat.
- Coding-glyph substitution is an on-brand signature for headlines (e.g. Đec⊘de, y0ur, <c0ding/>, <challenging and fun>?) — tasteful, never gimmicky.

[POP-UP / WINDOW DEVICE — WHEN USED]
- Clean OS-style windows: a title bar with three dots (●●●) and an X close button, a <tag/>-style window title, flat color blocks inside. Code-centric, clickable-interaction feel.

[ICONOGRAPHY — WHEN USED]
- Pixelated / gamified, single color only. Never stretch, shadow, outline, or multi-color an icon.

[HARD DON'Ts]
- NO people, faces, hands, characters, mascots, or silhouettes anywhere. These are object/graphic title cards only.
- Do NOT default to glitch effects, scanlines, or gimmicky "digital" cliches — break the stereotype with clean, confident graphic design.
- Do NOT combine more than two dominant colors. Do NOT combine patterns. Do NOT outline or 3D-shade type. Do NOT use off-palette colors.

[TEXT — TECHNICAL RULE]
- All rendered text is ENGLISH ONLY (image generators corrupt Arabic). Spell every word exactly as given. This is a technical constraint, not a brand statement.\
"""


# ===========================================================================
# Shared Stage-1 user-message builder for the NON-voxel presets.
# Voxel does NOT use this — it keeps its own builder so its output is identical.
# ===========================================================================

def build_styled_image_user_message(prompt: str, aspect_ratio: str, *, layout_directive: str) -> str:
    """Compose the Stage-1 user message for a non-voxel preset.

    Mirrors ``build_animation_image_user_message``'s FORMAT / LAYOUT / VARIATION
    shape but swaps in a per-style ``layout_directive``. Bound per preset via
    ``functools.partial`` in the registry below.
    """
    brief = (prompt or "").strip()
    if aspect_ratio == "9:16":
        fmt = (
            "FORMAT: design for a 9:16 VERTICAL frame (tall — Reels / TikTok / "
            "Stories). Compose for a portrait canvas; let the graphic fill the "
            "frame top-to-bottom."
        )
    else:
        fmt = (
            "FORMAT: design for a 16:9 HORIZONTAL frame (wide — YouTube / "
            "landscape). Compose for a widescreen canvas."
        )
    return (
        f"[BRIEF]\n{brief}\n\n"
        f"[{fmt}]\n\n"
        f"[LAYOUT DIRECTIVE]\n{layout_directive}\n\n"
        "[VARIATION]\n"
        "Invent a fresh, unique composition so this card feels distinct, while "
        "staying inside the 42 Abu Dhabi brand system."
    )


# ===========================================================================
# PRESET — POP-UP (code-centric OS-window cards)
# ===========================================================================

_POPUP_LAYOUT_DIRECTIVE = (
    "Execute the 42 POP-UP / OS-WINDOW layout: one or two clean OS-style windows "
    "(title bar with three dots ●●●, an X close button, a <tag/>-style window title), "
    "containing flat color blocks and a CROPPED single brand pattern inside one window. "
    "Code-centric, clickable-interaction feel, clear information hierarchy. The headline "
    "uses a coding format. Professional but exciting. One pattern only, NO people."
)

POPUP_STAGE1_SYSTEM = f"""You are the Executive Creative Director for 42 Abu Dhabi's social-media design pipeline, specializing in the brand's CODE-CENTRIC POP-UP look. Your job is to take a short brief for an INTRO or OUTRO title card and expand it into a hyper-detailed visual prompt for a single composition built from clean OS-style windows — the brand's clickable, developer-culture device.

[SCENE DESIGN PHILOSOPHY]
- WINDOWS ARE THE STRUCTURE: Build the card from ONE or TWO clean OS-style windows. Each window has a title bar with three dots (●●●), an X close button, and a <tag/>-style title (e.g. <intro/>, <apply/>, <piscine/>).
- FLAT BLOCKS INSIDE: Fill windows with flat brand-color blocks, a coding-format line, and clear information hierarchy. One window may contain a CROPPED single brand pattern (map, ghutra, OR arabic-letter wireframe — pick one).
- CLICKABLE ENERGY: It should feel like an interface moment — a button, a cursor, a <tag/>, a code line — without being cluttered. Confident, code-centric, professional but exciting.
- NO solid toy objects, NO realistic materials, NO glitch cliches. Clean flat UI graphic design.

[MANDATORY LOOK]
- Crisp window chrome, flat fills, sharp corners or subtle rounding consistent across windows. Strong figure/ground contrast using an approved two-color pairing.
- If two windows are used, overlap or stack them to create hierarchy (a primary headline window in front).

{ANIMATION_COMMON_POLICY}

[CRITICAL: TEXT LAYOUT & TYPOGRAPHY]
- Extract a SHORT, punchy headline from the brief and express it in a coding format where it reads cleanly (e.g. "<APPLY/> NOW", "Đec⊘de y0ur FUTURE"). Optionally a short banner subtitle. Omit the subtitle if the brief implies none.
- Headline sits inside or beside the primary window. Bold, geometric, FLAT, single-color White (#FFFFFF) on a solid brand block. The <tag/> window titles are part of the design, not the headline.

IMPORTANT: The [TEXT TO RENDER] block below must contain ONLY the raw text to display — no rendering or font directives.

At the very end of your output, you MUST append this exact block:

[TEXT TO RENDER]
PLACEMENT: [Specify the dynamic text placement here]
TEXT_LINE_1: "[Main Headline in ALL CAPS]"
TEXT_LINE_2: "[Short banner subtitle - ONLY IF it fits the brief]" """

POPUP_STAGE2_SYSTEM = f"""You are an expert graphic designer and brand custodian for 42 Abu Dhabi. Render the detailed pop-up / OS-style windows composition described into a single premium title-card frame for a brand intro/outro, strictly enforcing the brand identity.

[LOGO FIREWALL & REPLICATION]
- CRITICAL: You must extract and replicate the "42 ABU DHABI" logomark exactly as it appears in the provided local reference logo image (42_logo_white.png).
- The logo MUST remain a completely flat, crisp, 2D vector graphic overlay.
- ABSOLUTELY DO NOT apply any 3D shading, 3D textures, lighting, shadows, or distortions to the logo. Place it small and unobtrusively in a corner.

[ASPECT RATIO — IMPORTANT]
- If the prompt says 9:16 vertical, generate a TALL portrait image (9:16). If it says 16:9 horizontal, generate a WIDE landscape image (16:9).
- Fill the whole frame edge-to-edge — no letterboxing, no borders, no padding bars.

[POP-UP RENDERING RULES]
- Render one or two clean OS-style windows: title bar with three dots (●●●), an X close button, a <tag/>-style title, flat color blocks inside. Code-centric, clickable feel, clear hierarchy.
- One window may hold a CROPPED single brand pattern. Use exactly ONE pattern. Flat UI graphic design — no toy objects, no realistic materials, no glitch/scanline cliches.

{ANIMATION_COMMON_POLICY}

[TYPOGRAPHY & TEXT]
- Find the "[TEXT TO RENDER]" block at the end of the prompt. Read "PLACEMENT" and position the text exactly there. Print ONLY the strings inside the quotes — never the labels ('TEXT_LINE_1', 'PLACEMENT'). If a line is missing, do not invent one.
- Render <tag/> window titles and coding-glyph substitutions EXACTLY as written. Bold, geometric, FLAT, single-color White (#FFFFFF) on solid brand blocks. Never outline or 3D-shade the type. ENGLISH ONLY — never render Arabic or any non-Latin script. Spell every word exactly as given.

[OUTPUT CONSTRAINTS]
- A single cohesive composition (not a collage). No people, faces, hands, or characters. """

_POPUP_MOTION_CLAUSE = """\
A polished, code-centric animation of this 42 Abu Dhabi POP-UP / OS-window
title card. The attached image is the EXACT first frame — extrapolate motion
forward from it. Treat the frame as a finished design; animate ON TOP of it, do
not redesign, restyle, or re-render the windows.

==========================================================================
MOTION
==========================================================================
  • Keep the flat UI windows crisp — do NOT add depth, realism, or texture.
  • Animate like an interface: windows snap/fade in with a soft scale, a cursor
    or button gives a single clickable beat, the cropped pattern inside a window
    drifts subtly, a <tag/> title types or settles in.
  • Clean and controlled — no chaotic motion, no whip pans, no glitching.
  • The "42 ABU DHABI" logomark and ALL text (including <tag/> titles) are
    LOCKED — they may settle/fade in but must NOT warp, distort, or re-render.
  • End on a stable, clean hold so the clip loops / cuts well.
"""


# ===========================================================================
# PRESET — PATTERN (bold full-bleed single brand pattern)
# ===========================================================================

_PATTERN_LAYOUT_DIRECTIVE = (
    "Execute the 42 PATTERN layout: ONE bold full-bleed brand pattern (Ghutra embroidery "
    "OR abstracted Abu Dhabi map) used tilted / cropped / contained-in-a-shape, over a "
    "two-color brand split, with the headline on a solid color block. Pure graphic energy "
    "— NO objects, NO people. One pattern only."
)

PATTERN_STAGE1_SYSTEM = f"""You are the Executive Creative Director for 42 Abu Dhabi's social-media design pipeline, specializing in the brand's bold PATTERN look. Your job is to take a short brief for an INTRO or OUTRO title card and expand it into a hyper-detailed visual prompt for a single, graphic-driven composition built around ONE full-bleed brand statement pattern.

[SCENE DESIGN PHILOSOPHY]
- THE PATTERN IS EVERYTHING: Choose exactly ONE statement pattern — GHUTRA (dense dotted/woven grid with zigzag rows) OR an abstracted Abu Dhabi MAP (fine white wireframe lines). Use it bold and full-bleed, applied tilted, cropped, or contained inside a shape. NEVER combine patterns.
- TWO-COLOR SPLIT: Lay the pattern over a confident two-color brand split (an approved pairing). High contrast, punchy, pure graphic energy.
- NO OBJECTS: No hero objects, no toy blocks, no scenes, no props. This is flat pattern-and-type design only.
- A short row of pixel-diamond accents is optional; keep accents minimal.

[MANDATORY LOOK]
- The pattern is the visual subject and fills the frame full-bleed. Strong tilt or crop gives it motion and editorial confidence. Clean, modern, gallery-grade — never busy or muddy.

{ANIMATION_COMMON_POLICY}

[CRITICAL: TEXT LAYOUT & TYPOGRAPHY]
- Extract a SHORT, punchy headline from the brief. A coding-glyph substitution is welcome when it reads cleanly. Optionally a short banner subtitle; omit it if the brief implies none.
- Place the headline on a SOLID brand-color block so it stays readable over the pattern. Bold, geometric, FLAT, single-color White (#FFFFFF).

IMPORTANT: The [TEXT TO RENDER] block below must contain ONLY the raw text to display — no rendering or font directives.

At the very end of your output, you MUST append this exact block:

[TEXT TO RENDER]
PLACEMENT: [Specify the dynamic text placement here]
TEXT_LINE_1: "[Main Headline in ALL CAPS]"
TEXT_LINE_2: "[Short banner subtitle - ONLY IF it fits the brief]" """

PATTERN_STAGE2_SYSTEM = f"""You are an expert graphic designer and brand custodian for 42 Abu Dhabi. Render the detailed full-bleed PATTERN composition described into a single premium title-card frame for a brand intro/outro, strictly enforcing the brand identity.

[LOGO FIREWALL & REPLICATION]
- CRITICAL: You must extract and replicate the "42 ABU DHABI" logomark exactly as it appears in the provided local reference logo image (42_logo_white.png).
- The logo MUST remain a completely flat, crisp, 2D vector graphic overlay.
- ABSOLUTELY DO NOT apply any 3D shading, 3D textures, lighting, shadows, or distortions to the logo. Place it small and unobtrusively in a corner.

[ASPECT RATIO — IMPORTANT]
- If the prompt says 9:16 vertical, generate a TALL portrait image (9:16). If it says 16:9 horizontal, generate a WIDE landscape image (16:9).
- Fill the whole frame edge-to-edge — no letterboxing, no borders, no padding bars.

[PATTERN RENDERING RULES]
- Render exactly ONE full-bleed brand pattern (Ghutra embroidery OR abstracted Abu Dhabi map), tilted / cropped / contained-in-a-shape, over a two-color brand split.
- Pure flat graphic design — no objects, no scenes, no realistic materials, no glitch/scanline cliches. NEVER combine patterns.

{ANIMATION_COMMON_POLICY}

[TYPOGRAPHY & TEXT]
- Find the "[TEXT TO RENDER]" block at the end of the prompt. Read "PLACEMENT" and position the text exactly there. Print ONLY the strings inside the quotes — never the labels ('TEXT_LINE_1', 'PLACEMENT'). If a line is missing, do not invent one.
- Headline sits on a solid brand-color block. Bold, geometric, FLAT, single-color White (#FFFFFF). Never outline or 3D-shade the type. ENGLISH ONLY — never render Arabic or any non-Latin script. Spell every word exactly as given.

[OUTPUT CONSTRAINTS]
- A single cohesive composition (not a collage). No people, faces, hands, or characters. """

_PATTERN_MOTION_CLAUSE = """\
A polished, graphic animation of this 42 Abu Dhabi PATTERN title card. The
attached image is the EXACT first frame — extrapolate motion forward from it.
Treat the frame as a finished design; animate ON TOP of it, do not redesign,
restyle, or re-render the pattern.

==========================================================================
MOTION
==========================================================================
  • Keep the flat brand pattern crisp and flat — do NOT add 3D depth or realism.
  • Animate with confident restraint: a slow parallax drift or tilt of the
    full-bleed pattern, the two-color split sweeping subtly, the headline block
    settling into place.
  • Bold but controlled — no chaotic motion, no whip pans, no glitching.
  • The "42 ABU DHABI" logomark and ALL text are LOCKED — they may settle/fade
    in but must NOT warp, distort, mis-spell, or re-render.
  • End on a stable, clean hold so the clip loops / cuts well.
"""


# ===========================================================================
# Registry
# ===========================================================================

@dataclass(frozen=True)
class AnimationStyle:
    """One selectable Create-Animation preset.

    ``layout_directive`` is ``None`` for voxel (its bespoke builder already
    embeds the layout). ``motion_clause`` is ``None`` for voxel (the dispatcher
    routes it to the original ``build_animation_motion_prompt``).
    """
    key: str
    label: str
    description: str
    stage1_system: str
    stage2_system: str
    layout_directive: Optional[str]
    motion_clause: Optional[str]
    user_message_builder: Callable[[str, str], str]
    # A preset that cannot use the shared STYLE tail supplies the WHOLE motion
    # prompt here instead of a clause. Only `asis` needs it: the shared tail
    # promises "NO people or characters ever appear", which is a promise we
    # cannot make about a photo the user brought in.
    motion_builder: Optional[Callable[[str, bool], str]] = None


# ===========================================================================
# "As is": the preset for an image the user BROUGHT IN rather than generated
# ===========================================================================
#
# Create Animation renders its own frame, so it always knows the frame's style
# and can write a motion prompt that names it ("keep the chunky voxel look
# rock-solid"). An imported image has no such guarantee: it can be a poster from
# Create Poster, a saved asset, or a photograph off someone's desktop. Handing
# a photograph the voxel prompt asks Veo to voxelise it, which is the one thing
# the user did not ask for.
#
# So this prompt names NOTHING about the look. It says: whatever is in this
# picture, keep it, and move it gently.

_ASIS_MOTION_HEAD = """\
A polished, restrained animation of the attached picture.
The attached image is the EXACT first frame - extrapolate motion forward from
it. You do not know what this picture is and you do not need to: treat it as a
finished piece of work and animate ON TOP of it. Do not redesign it, restyle it,
re-render it, or change what it is a picture of.

==========================================================================
MOTION
==========================================================================
  • Whatever look the picture has - photo, illustration, 3D, flat graphic -
    keep it EXACTLY. Do not push it toward realism and do not stylise it.
  • Move gently: a slow camera push-in or drift, soft parallax between
    foreground and background, light catching a surface, a small ambient
    element (dust, steam, a flicker, a ripple) if the picture already implies
    one.
  • Anything alive in the picture may breathe or shift slightly. Nothing
    that is not already in the picture may enter it.
  • ALL text, logos and lettering are LOCKED. They may settle or fade into
    place but must NOT warp, distort, mis-spell or re-render. Treat text as a
    crisp static overlay.
  • Controlled, never chaotic: no whip pans, no fast cuts, no glitching, no
    morphing. End on a stable hold so the clip loops or cuts well.
"""

_ASIS_MOTION_TAIL = """\
==========================================================================
STYLE
==========================================================================
Preserve every design choice of the input frame: its look, its colours, its
layout, its subject and its text. The animation enhances the frame; it does not
remake it.

BRIEF CONTEXT (informs mood + music, not visual content):
  {brief}
"""


def build_imported_motion_prompt(brief: str, with_audio: bool = True) -> str:
    """Veo i2v prompt for a picture the user supplied rather than generated.

    Deliberately says nothing about the aesthetic, because we do not know it.
    Mirrors the structure of the styled prompts (head + audio block + tail) and
    goes through ``scrub_rai_terms`` for the same reason they do.
    """
    brief_clean = (brief or "").strip() or "42 Abu Dhabi social post"
    audio_block = _MOTION_AUDIO_BLOCK if with_audio else _MOTION_SILENT_BLOCK
    return scrub_rai_terms(
        _ASIS_MOTION_HEAD + audio_block + _ASIS_MOTION_TAIL.format(brief=brief_clean)
    )


ANIMATION_STYLES: dict[str, AnimationStyle] = {
    "voxel": AnimationStyle(
        key="voxel",
        label="Voxel",
        description="Retro-voxel toy diorama (default).",
        stage1_system=ANIMATION_IMAGE_SYSTEM,            # original — byte-identical
        stage2_system=ANIMATION_RENDER_SYSTEM,           # original — byte-identical
        layout_directive=None,
        motion_clause=None,
        user_message_builder=build_animation_image_user_message,  # original fn
    ),
    "popup": AnimationStyle(
        key="popup",
        label="Pop-up",
        description="Code-centric OS-window cards (●●● title bar, <tag/> titles) with a cropped pattern and coding-format headline.",
        stage1_system=POPUP_STAGE1_SYSTEM,
        stage2_system=POPUP_STAGE2_SYSTEM,
        layout_directive=_POPUP_LAYOUT_DIRECTIVE,
        motion_clause=_POPUP_MOTION_CLAUSE,
        user_message_builder=partial(build_styled_image_user_message, layout_directive=_POPUP_LAYOUT_DIRECTIVE),
    ),
    "pattern": AnimationStyle(
        key="pattern",
        label="Pattern",
        description="Bold full-bleed single brand pattern (Ghutra or Map) over a two-color split.",
        stage1_system=PATTERN_STAGE1_SYSTEM,
        stage2_system=PATTERN_STAGE2_SYSTEM,
        layout_directive=_PATTERN_LAYOUT_DIRECTIVE,
        motion_clause=_PATTERN_MOTION_CLAUSE,
        user_message_builder=partial(build_styled_image_user_message, layout_directive=_PATTERN_LAYOUT_DIRECTIVE),
    ),
    # NOT a look you can generate in - a look we DECLINE to assume. Attached to
    # images the user brought in (a saved asset, a file off their desktop),
    # where naming any aesthetic would be a guess. The stage1/stage2 systems are
    # voxel's only because they are the house default and can only ever fire if
    # someone Explores an imported frame; the motion prompt is the field that
    # does the work, and it is this preset's whole reason to exist.
    "asis": AnimationStyle(
        key="asis",
        label="As is",
        description="Animate the picture exactly as it is. For images brought in rather than generated.",
        stage1_system=ANIMATION_IMAGE_SYSTEM,
        stage2_system=ANIMATION_RENDER_SYSTEM,
        layout_directive=None,
        motion_clause=None,
        user_message_builder=build_animation_image_user_message,
        motion_builder=build_imported_motion_prompt,
    ),
}

ANIMATION_STYLE_KEYS: tuple[str, ...] = tuple(ANIMATION_STYLES.keys())


def get_animation_style(key: Optional[str]) -> AnimationStyle:
    """Resolve a style key to its preset, with a safe voxel fallback.

    Unknown keys, ``None``, and legacy records all resolve to voxel — so a
    typo or an old animation row never breaks frame/motion generation. Keys are
    matched case-insensitively.
    """
    return ANIMATION_STYLES.get((key or "").strip().lower(), ANIMATION_STYLES[DEFAULT_STYLE])


def build_motion_prompt_for_style(style_key: Optional[str], brief: str, *, with_audio: bool = True) -> str:
    """Compose the Veo i2v motion prompt for the chosen style.

    Voxel (and any fallback) routes to the ORIGINAL ``build_animation_motion_prompt``
    unchanged — byte-identical to v1. Non-voxel presets assemble their per-style
    clause + the shared audio/silent block + a STYLE/BRIEF tail, wrapped in
    ``scrub_rai_terms`` so Vertex's RAI text filter stays happy (mirrors the
    structure of the voxel function).
    """
    style = get_animation_style(style_key)
    # A preset owning its whole prompt wins: it opted out of the shared tail.
    if style.motion_builder is not None:
        return style.motion_builder(brief, with_audio)
    if style.motion_clause is None:
        return build_animation_motion_prompt(brief, with_audio=with_audio)

    brief_clean = (brief or "").strip() or "42 Abu Dhabi brand intro"
    audio_block = _MOTION_AUDIO_BLOCK if with_audio else _MOTION_SILENT_BLOCK
    return scrub_rai_terms(
        f"{style.motion_clause}\n"
        f"{audio_block}"
        "==========================================================================\n"
        "STYLE\n"
        "==========================================================================\n"
        "Preserve every design choice of the input frame: the graphic style, brand\n"
        "palette, layout, logo, and text. The animation enhances the frame; it does\n"
        "not remake it. NO people or characters ever appear.\n\n"
        "BRIEF CONTEXT (informs mood + music, not visual content):\n"
        f"  {brief_clean}\n"
    )
