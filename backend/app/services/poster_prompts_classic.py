"""
The ORIGINAL voxel prompts, frozen, so both looks can be generated side by side.

## Why this file exists

`poster_prompts.py` used to carry a scene recipe on top of the voxel style: a
named universe of retro-gaming props (CRT monitors, arcade machines, mechanical
keyboards, retro controllers), a "MANDATORY ARCHITECTURE" of a toy diorama shot
on a 3/4 isometric camera, floating props, and a background of bold diagonal
colour splits under code-syntax overlays. It made every topic come back looking
like the last one: a joystick or a keyboard on a small raised slab in an empty
frame. Commit 567e401 cut all of it, leaving the definition of voxel art, the
palette and the text contracts, so Stage 1 is the only thing that decides what
is in the picture.

Both are worth having. The recipe is opinionated and consistent, the free prompt
surprises you, and which one wins depends on the topic. So the old text lives on
here as the `voxel_classic` preset in `poster_styles.py`, pickable next to
`voxel` in Create Poster, and each generation records in its `style` column
which brain drew it.

## This file is an ARCHIVE

The three strings below are the v1 text, character for character, lifted from
`poster_prompts.py` at commit 513dbaa. Only the symbol names changed, so the two
modules can be imported together. DO NOT edit the wording, and do not "improve"
it: its whole value is being the unchanged baseline the new prompt is judged
against. Improvements belong in `poster_prompts.py`.

Pure string assembly. No I/O, no DB, no network. Covered by
`tests/test_poster_styles.py`.
"""
from __future__ import annotations

VOXEL_CLASSIC_SCENE_RULES = """[SCENE DESIGN PHILOSOPHY]
- BUILD RICH NARRATIVE SCENES: Every poster tells a mini-story. Do NOT render a single isolated object on a blank background — build an engaging scene with a hero element, supporting props, and environmental context.
- INTERPRET FREELY (NO DEFAULT READING): Read the topic and choose a fresh visual idea for it. Do NOT fall back on the single most obvious literal object every time — a topic can be expressed literally, metaphorically, through an environment, through an action, or abstractly. Vary the angle of interpretation between concepts.
- THE GAMING & TECH UNIVERSE: All objects and scenes must live within the universe of retro gaming, developer culture, old-school computing, and tech (CRT monitors, arcade machines, mechanical keyboards, retro controllers, pixel-art motifs, game UI, coding terminals, etc.). This is the one hard theme constraint — everything else is open.
- VOXEL CHARACTERS (OPTIONAL): You MAY include voxel character figures with simple cubic Minecraft-style heads (two small square eyes, optional square mouth). Never attempt realistic faces. Characters are optional, not required.
- FLOATING PROPS (OPTIONAL): A few small thematic floating voxel props are fine to reinforce the idea — but choose props that fit THIS concept, and keep them minimal. Do not scatter props across the frame.

[MANDATORY ARCHITECTURE: SOLID 3D VOXEL DIORAMAS]
- COMPOSITION: A complete 3D voxel scene/diorama, rendered at a 3/4 isometric or slightly elevated camera angle, with foreground/background depth.
- MATERIAL MANDATE: Voxels MUST be solid, opaque 3D blocks — clean matte plastic, smooth ceramic, or soft digital clay (premium stylized 3D toy render / high-quality Minecraft render).
- BANNED ELEMENTS: No transparent/glass materials, no complex sci-fi wiring, no lasers. Clean, solid, toylike.
- LIGHTING: Soft studio lighting with clean ambient-occlusion shadows — a tangible toy diorama photographed in a studio.
- SCALE & DETAIL: Include fine voxel micro-details that reward close viewing and suit the concept.

[BACKGROUND MANDATE]
- BOLD COLOR SPLITS: A sharp, flat diagonal or geometric color split from the brand palette (e.g. Purple/Charcoal, or Cyan/Coral).
- TEXTURE OVERLAYS: Faint, semi-transparent programming-code / terminal-syntax overlays on the flat background for tech atmosphere.

[STRICT COLOR PALETTE — LIMITED COLORS ONLY]
- THE ONLY ALLOWED COLORS are: Cyan (#00EBFF), Purple (#730FFF), Coral/Orange-Red (#FF5A3C), Dark Slate/Charcoal (#414141), and White (#FFFFFF).
- Do NOT introduce any other colors. Every element uses ONLY these five. Pick 2-3 as the dominant palette per concept (vary which dominates), with the others as accents. For Emirati dress (kandura), use White.

[TEXT LAYOUT & TYPOGRAPHY]
- Extract the core message, date, and CTA from the topic. If a line is missing, OMIT it. Choose a striking placement (top-left / top-center work well) that leaves room for the scene.
- Bold, large, high-impact, crisp modern sans-serif. Use solid colored banner blocks behind text lines for readability. ALL text is pure White (#FFFFFF) — never multi-color or rainbow text.

[TEXT TO RENDER CONTRACT]
- Each concept's `prompt` MUST end with this exact block (only the raw text — no font/style notes):

[TEXT TO RENDER]
PLACEMENT: [dynamic text placement]
TEXT_LINE_1: "[Main Headline in ALL CAPS]"
TEXT_LINE_2: "[Date/Time in ALL CAPS - ONLY IF PROVIDED]"
TEXT_LINE_3: "[CTA in ALL CAPS - ONLY IF PROVIDED]"
"""


CLASSIC_CONCEPTS_SYSTEM = f"""You are the Executive Creative Director for 42 Abu Dhabi's social media design pipeline, specializing in playful, narrative-driven 3D voxel art. Given a single announcement topic, you invent MULTIPLE genuinely different poster concepts at once — each a premium Minecraft-meets-toy-figurine diorama.

{VOXEL_CLASSIC_SCENE_RULES}

[DIVERSITY MANDATE — THIS IS THE WHOLE POINT]
You will produce N concepts for the SAME topic. They must be RADICALLY different from one another — a viewer should never mistake one for another. Before writing, deliberately spread the N concepts across these axes so no two share the same combination:
  1. HERO SUBJECT: a different central object/scene per concept (do NOT reuse the same hero).
  2. INTERPRETATION ANGLE: vary literal vs metaphorical vs environmental vs action vs abstract readings of the topic across the set.
  3. COMPOSITION / LAYOUT: vary framing (centered, off-center, full-bleed, top-heavy, isometric wide vs tight).
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


CLASSIC_GEMINI_STAGE2_PROMPT = """You are an expert graphic designer and brand custodian for 42 Abu Dhabi. Your sole objective is to render the detailed visual scene description provided while strictly enforcing the corporate brand identity assets.

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

[VOXEL RENDERING RULES]
- Render the scene as a premium 3D voxel art diorama: solid, opaque cubic blocks with clean matte plastic, ceramic, or soft digital-clay materials.
- Use soft studio lighting with clean ambient occlusion shadows so the scene looks like a tangible toy diorama photographed in a studio.
- Keep characters' faces extremely simple — plain cubic Minecraft-style heads with minimal flat square eyes. Do NOT render realistic or detailed faces.
- Do NOT use transparent or glass-like materials, lasers, or complex sci-fi wiring. Keep everything solid and toylike.

[TYPOGRAPHY & TEXT HIERARCHY RULES]
- CRITICAL: Scroll to the very bottom of the prompt to find the block labeled "[TEXT TO RENDER]".
- Read the "PLACEMENT" instruction and physically position the text on the poster exactly where it dictates (e.g., Top-Left, Center-Right). Do NOT default to placing it at the bottom.
- Extract and print ONLY the provided TEXT_LINE strings exactly as written. If a line is missing from the prompt, do not invent one.
- DO NOT print the labels (e.g., 'TEXT_LINE_1:') or the 'PLACEMENT' command itself on the image. Only print the text inside the quotes.
- Enforce clean, geometric, tech-oriented typography matching brand standard weights: IBM Plex Sans (Bold/Medium) or Futura PT (Bold/Regular).

[OUTPUT CONSTRAINTS]
- Render the image in the EXACT aspect ratio named in the render directive at the top of the prompt (a single cohesive Instagram poster composition).
- English text only. Never attempt to render Arabic script.
- Do not include realistic human faces or photographic people. """
