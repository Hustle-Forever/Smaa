"""
Prompts for the Yas "Create Animation" pipeline (intros / outros).

These produce a BRANDED TITLE CARD in 42 Abu Dhabi's retro-voxel / pixel
house style — the SAME look as Create Poster (gaming-&-tech universe, brand
palette, bold background splits) — but with two key differences:

  • NO human / character figures. Intros & outros are object/scene title cards
    (a rocket, an arcade cabinet, a terminal, planets…), never people. The
    humans live in the main video, not the intro.
  • Aspect is 9:16 or 16:9 (not the poster's 1:1), because the frame becomes
    the first frame of a Veo clip at that aspect.

Three stages, mirroring Create Poster's two-stage shape:

  STAGE 1  ANIMATION_IMAGE_SYSTEM  — OpenAI gpt-4o expands a short brief into a
           hyper-detailed voxel scene description, ending in a [TEXT TO RENDER]
           block (headline + optional banner subtitle).
  STAGE 2  ANIMATION_RENDER_SYSTEM — Gemini image system_instruction: renders
           the voxel scene, bakes the real 42 logo, enforces aspect + palette +
           English-only text + NO people.
  STAGE 3  build_animation_motion_prompt — Veo 3 i2v prompt: the rendered frame
           is the EXACT first frame; describe title-card motion + audio.

Pure string assembly — no I/O. Reuses ANIMATE_NEGATIVE_PROMPT + scrub_rai_terms
from yas_poster_prompts so Veo's RAI text filter stays happy.
"""
from __future__ import annotations

from app.services.yas_poster_prompts import ANIMATE_NEGATIVE_PROMPT  # re-exported for the router
from app.services.yas_talking_head_prompts import scrub_rai_terms

__all__ = [
    "ANIMATION_IMAGE_SYSTEM",
    "ANIMATION_RENDER_SYSTEM",
    "ANIMATE_NEGATIVE_PROMPT",
    "build_animation_image_user_message",
    "build_animation_motion_prompt",
]


# ---------------------------------------------------------------------------
# STAGE 1 — OpenAI gpt-4o: expand the brief into a voxel title-card scene
# ---------------------------------------------------------------------------

ANIMATION_IMAGE_SYSTEM = """You are the Executive Creative Director for 42 Abu Dhabi's social media design pipeline, specializing in playful, narrative-driven 3D voxel art scenes. Your job is to take a short brief for an INTRO or OUTRO title card and expand it into a hyper-detailed visual prompt for a rich, storytelling voxel render that feels like a premium Minecraft-meets-toy-figurine diorama — the opening or closing card of a brand reel.

[SCENE DESIGN PHILOSOPHY]
- BUILD A RICH NARRATIVE SCENE: Tell a mini-story with a clear HERO OBJECT, supporting props, and environmental context. Do NOT render a single isolated object on a blank background.
- CREATIVE INTERPRETATION: Use playful literal interpretations that create compelling visuals (e.g., a voxel rocket launching for "career launch", a voxel arcade cabinet for "game night", a voxel terminal booting up for "applications open"). The goal is instant readability and energy.
- THE GAMING & TECH UNIVERSE: All objects and scenes must live within the universe of retro gaming, developer culture, old-school computing, and tech. Think: rockets, arcade machines, CRT monitors, mechanical keyboards, retro controllers, pixel planets, coding terminals, floppy disks, rubber ducks (debugging!), etc.
- NO HUMAN OR CHARACTER FIGURES — THIS IS A HARD RULE. Do NOT include any people, faces, voxel characters, mascots, humanoid figures, hands, or silhouettes. Intros/outros are OBJECT and SCENE title cards only. (The real students appear in the main video, never in this card.)
- FLOATING PROPS: You may place a few small floating voxel props to reinforce the theme (stars, sparks, a cursor, planets). Keep them tasteful — do not clutter the frame.

[MANDATORY ARCHITECTURE: SOLID 3D VOXEL DIORAMAS]
- COMPOSITION: Build a complete 3D voxel scene rendered at a 3/4 isometric or slightly elevated angle, with foreground and background depth.
- MATERIAL MANDATE: Voxels MUST be solid, opaque 3D blocks — clean matte plastic, smooth ceramic, or soft digital clay (premium stylized toy render / high-quality Minecraft render).
- BANNED ELEMENTS: No transparent/glass materials, no complex sci-fi wiring, no lasers. Clean, solid, toylike.
- LIGHTING: Soft studio lighting with clean ambient occlusion shadows — a tangible toy diorama photographed in a studio.
- SCALE & DETAIL: Include fine voxel micro-details (engine flames, pixel stars, screen icons) that reward close viewing.

[BACKGROUND MANDATE]
- BOLD COLOR SPLITS: The background must use sharp, flat diagonal or geometric color splits from the brand palette (e.g. Purple and Charcoal split diagonally, or Cyan and Charcoal).
- TEXTURE OVERLAYS: The flat background colors may carry faint, semi-transparent programming code / terminal syntax for tech atmosphere.

[STRICT COLOR PALETTE — LIMITED COLORS ONLY]
- THE ONLY ALLOWED COLORS are: Cyan (#00EBFF), Purple (#730FFF), Coral/Orange-Red (#FF5A3C), Dark Slate/Charcoal (#414141), and White (#FFFFFF).
- Do NOT introduce any other colors. Pick 2-3 as the dominant palette for a cohesive look, with the others as accents.

[CRITICAL: TEXT LAYOUT & TYPOGRAPHY]
- Extract a SHORT, punchy headline from the brief (this is a title card — e.g. "READY TO LAUNCH!"). Optionally a short banner subtitle (e.g. "A Career Journey"). If the brief implies no subtitle, omit it.
- Use solid colored background blocks/banners behind text lines for maximum readability (e.g. a coral banner behind the subtitle). Bold, modern, high-impact sans-serif. All text a single color — pure White (#FFFFFF).
- Choose a striking placement that leaves room for the hero scene (top or upper-third works well).

IMPORTANT: The [TEXT TO RENDER] block below must contain ONLY the raw text to display — no rendering or font directives.

At the very end of your output, you MUST append this exact block:

[TEXT TO RENDER]
PLACEMENT: [Specify the dynamic text placement here]
TEXT_LINE_1: "[Main Headline in ALL CAPS]"
TEXT_LINE_2: "[Short banner subtitle - ONLY IF it fits the brief]" """


def build_animation_image_user_message(prompt: str, aspect_ratio: str) -> str:
    """Compose the Stage-1 user message for gpt-4o."""
    brief = (prompt or "").strip()
    if aspect_ratio == "9:16":
        fmt = (
            "FORMAT: design for a 9:16 VERTICAL frame (tall — Reels / TikTok / "
            "Stories). Compose the voxel diorama for a portrait canvas; let the "
            "scene fill the frame top-to-bottom."
        )
    else:
        fmt = (
            "FORMAT: design for a 16:9 HORIZONTAL frame (wide — YouTube / "
            "landscape). Compose the voxel diorama for a widescreen canvas."
        )
    return (
        f"[BRIEF]\n{brief}\n\n"
        f"[{fmt}]\n\n"
        "[LAYOUT DIRECTIVE]\n"
        "Execute the Retro Voxel title-card layout: a gamified, retro-tech voxel "
        "diorama built from solid 3D pixel blocks, with a clear hero object and a "
        "short bold headline. NO people or characters anywhere.\n\n"
        "[VARIATION]\n"
        "Invent a fresh, unique voxel scene concept so this card feels distinct, "
        "with premium textures, brand colors, and materials."
    )


# ---------------------------------------------------------------------------
# STAGE 2 — Gemini image system_instruction: render + bake logo + aspect
# ---------------------------------------------------------------------------

ANIMATION_RENDER_SYSTEM = """You are an expert graphic designer and brand custodian for 42 Abu Dhabi. Render the detailed voxel scene description provided into a single premium title-card frame for a brand intro/outro, while strictly enforcing the brand identity.

[LOGO FIREWALL & REPLICATION]
- CRITICAL: You must extract and replicate the "42 ABU DHABI" logomark exactly as it appears in the provided local reference logo image (42_logo_white.png).
- The logo MUST remain a completely flat, crisp, 2D vector graphic overlay.
- ABSOLUTELY DO NOT apply any 3D shading, 3D textures, lighting, shadows, or distortions to the logo. Place it small and unobtrusively in a corner.

[ASPECT RATIO — IMPORTANT]
- If the prompt says 9:16 vertical, generate a TALL portrait image (9:16).
- If the prompt says 16:9 horizontal, generate a WIDE landscape image (16:9).
- Fill the whole frame edge-to-edge — no letterboxing, no borders, no padding bars.

[VOXEL RENDERING RULES]
- Render the scene as a premium 3D voxel art diorama: solid, opaque cubic blocks with clean matte plastic, ceramic, or soft digital-clay materials.
- Soft studio lighting with clean ambient occlusion shadows — a tangible toy diorama photographed in a studio.
- No transparent/glass materials, no lasers, no complex sci-fi wiring. Solid and toylike.

[NO PEOPLE — HARD RULE]
- Do NOT render any humans, faces, voxel characters, mascots, humanoid figures, hands, or silhouettes. This is an object/scene title card only.

[STRICT COLOR PALETTE]
- Use ONLY: Cyan #00EBFF, Purple #730FFF, Coral/Orange-Red #FF5A3C, Charcoal #414141, White #FFFFFF. High-contrast and punchy; no muddy gradients.

[TYPOGRAPHY & TEXT]
- Find the "[TEXT TO RENDER]" block at the end of the prompt. Read "PLACEMENT" and position the text exactly there. Print ONLY the strings inside the quotes — never the labels ('TEXT_LINE_1', 'PLACEMENT'). If a line is missing, do not invent one.
- Bold, modern, geometric sans-serif. Use solid colored banners/blocks behind text for readability. All text pure White (#FFFFFF). ENGLISH ONLY — never render Arabic or any non-Latin script. Spell every word exactly as given.

[OUTPUT CONSTRAINTS]
- A single cohesive composition (not a collage). No realistic human faces or people. """


# ---------------------------------------------------------------------------
# STAGE 3 — Veo 3 image-to-video motion prompt
# ---------------------------------------------------------------------------

_MOTION_AUDIO_BLOCK = """\

==========================================================================
AUDIO (synthesized inline by you)
==========================================================================
  • Short, punchy, modern INSTRUMENTAL bed fitting a coding-school / tech
    brand intro: chiptune / synthwave / cinematic build. Lands on a clean
    accent at the end.
  • Absolutely NO dialogue, NO voiceover, NO singing, NO lyrics.
  • Tasteful retro sound design (a soft whoosh, an 8-bit blip, a riser) welcome.
"""

_MOTION_SILENT_BLOCK = """\

==========================================================================
AUDIO — SILENT
==========================================================================
  • Render a SILENT clip. No music, no voiceover, no ambient sound, no Foley.
"""


def build_animation_motion_prompt(brief: str, *, with_audio: bool = True) -> str:
    """Compose the Veo 3 i2v prompt for animating the rendered voxel title card.

    The caller passes the rendered PNG to veo_generate() as `start_image=` —
    Veo conditions the first frame on it and extrapolates motion forward.
    """
    brief_clean = (brief or "").strip() or "42 Abu Dhabi brand intro"
    audio_block = _MOTION_AUDIO_BLOCK if with_audio else _MOTION_SILENT_BLOCK
    return scrub_rai_terms(f"""\
A polished, playful animation of this 42 Abu Dhabi retro-voxel title card.
The attached image is the EXACT first frame — extrapolate motion forward from
it. Treat the frame as a finished design; animate ON TOP of it, do not redesign,
restyle, or re-render the voxel scene.

==========================================================================
MOTION
==========================================================================
  • Keep the chunky 3D voxel / toy-diorama look rock-solid — do NOT smooth it
    into realism or melt the blocky shapes.
  • Bring the hero object to life with tasteful motion: a gentle camera
    push-in or parallax, the hero object rising / hovering / launching,
    drifting voxel stars or particles, soft engine flame or screen flicker,
    a subtle pulse on background code texture.
  • Energetic but controlled — no chaotic fast cuts, no whip pans, no harsh
    glitching. It should feel like the opening beat of a brand reel.
  • The "42 ABU DHABI" logomark and ALL text are LOCKED — they may elegantly
    settle/fade into place but must NOT warp, distort, mis-spell, or re-render.
    Treat text as a crisp static overlay.
  • End on a stable, clean hold so the clip loops / cuts well.

{audio_block}
==========================================================================
STYLE
==========================================================================
Preserve every design choice of the input frame: the voxel art style, brand
palette, layout, logo, and text. The animation enhances the frame; it does not
remake it. NO people or characters ever appear.

BRIEF CONTEXT (informs mood + music, not visual content):
  {brief_clean}
""")
