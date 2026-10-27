"""Nano Banana prompt builder for Yas Producer · Poster + Human mode.

## The hard problem

Passing references as `source_images` to Nano Banana triggers its image-to-image
conditioning. Even with "DO NOT replicate" text instructions, the model treats
refs as a TEMPLATE TO MODIFY — copying layout, color proportions, text-plate
treatments, even cropping decisions. Initial v1 with that approach produced
"colour-shifted twins" of the references rather than fresh compositions.

## The fix: two-step style extraction

1. **Extract** — call Gemini Flash multimodal with a strict JSON schema that
   captures only TRANSFERABLE STYLE FINGERPRINT (palette, illustration style,
   typography mood, lighting, texture, brand cues). Schema forbids layout,
   subject pose, text content, specific design elements.
2. **Generate** — call Nano Banana with TEXT ONLY (no source_images on the
   first attempt). The extracted style attributes live inline in the prompt as
   bullet points. The model can't copy what it can't see.

The user's original references stop being conditioning images and become a
*style memory* baked into the prompt. Retries iterate on the previous attempt
(image-to-image is the right tool there — user wants to tweak, not pivot).

This module is pure — no I/O, no DB. Just string assembly. Prompts are
inspectable via `yas_generations.prompt_used` for debugging.
"""
from __future__ import annotations

from typing import Literal

from .poster_styles import PosterStyle

# Vertex Veo's RAI text classifier (support code 29310472) flags
# face-manipulation vocabulary even inside negations and even in the
# negative_prompt (it reads it as a request). The poster ANIMATE mode runs
# through Veo i2v, so we route its prompts through the same scrubber the
# talking-head builder uses.
from app.services.yas_talking_head_prompts import scrub_rai_terms


# ===========================================================================
# POSTER + HUMAN — the locked "42 Abu Dhabi information-poster with a real
# human" house style. The user's STYLE references stay text-only (a fingerprint
# baked into the prompt — see the module docstring), so the model can't copy
# their layout. The real 42 logomark IS sent as an ordered reference image part
# (LOGO_INSTRUCTION + POSTER_LOGO_PATH via gemini_image_multipart — the same
# mechanism Create Poster uses), so the model composes the actual mark in-render
# instead of lettering a fake one. On a tweak retry the previous attempt is also
# sent as a part so the model iterates on it.
# ===========================================================================

# Lead mandate — placed FIRST (primacy). Four iron rules that bracket the whole
# prompt together with _FINAL_CHECKLIST (recency).
_MANDATE_BLOCK = (
    "You are the art director for 42 Abu Dhabi — a tuition-free, peer-learning "
    "coding school in the UAE — producing ONE Instagram INFORMATION POSTER. The "
    "audience is young, ambitious and multicultural (Emirati and international "
    "students). This is a real, designed editorial poster with a real "
    "photographed person in it — not an abstract graphic, not a photo with a "
    "caption pasted on, and not a generic AI render.\n\n"
    "FOUR RULES OVERRIDE EVERYTHING BELOW. If you get only four things right, "
    "get these:\n"
    "  1. TEXT — render ONLY the exact words in the poster content below, "
    "spelled letter-for-letter, in LATIN CHARACTERS ONLY. Never any Arabic or "
    "other non-Latin script. Never invent extra text.\n"
    "  2. LOGO — the real 42 Abu Dhabi logomark is attached as a separate "
    "reference image. Place THAT exact logo, unchanged, small in the TOP-RIGHT "
    "corner. Never letter, redraw, or invent your own '42' / 'ABU DHABI' mark — "
    "only ever use the attached logo image.\n"
    "  3. HERO — exactly one real, photographic human is the centrepiece.\n"
    "  4. HOUSE STYLE — a bold stacked headline + a short subtitle in a "
    "confident editorial layout, in the 42 palette (vivid red / red-orange, "
    "deep purple, electric cyan, on near-black or off-white) with small "
    "8-bit / pixel accents.\n"
    "When any later line seems to conflict with these four, these four win."
)


def _poster_content_block(topic: str) -> str:
    """The exact copy to typeset. Teaches the model to read the enhancer's
    labelled output (HEADLINE:/SUBTITLE:/CARD:/DETAILS:) as PLACEMENT roles and
    never render the label words. A plain (un-enhanced) topic passes through as
    the content unchanged."""
    return (
        "POSTER CONTENT — the exact text to typeset, and nothing more:\n"
        f"  {topic.strip()}\n"
        "If this content arrives as labelled lines (HEADLINE:, SUBTITLE:, "
        "CARD:, DETAILS:), the labels tell you the ROLE and placement of each "
        "piece — typeset ONLY the text AFTER each label, never the label word "
        "itself. Map them like this: HEADLINE -> the big stacked headline; "
        "SUBTITLE -> the short supporting line; CARD -> a name+role or a short "
        "quote on ONE solid colour plate; DETAILS -> concrete event facts "
        "(date, time, venue, CTA, link, price) set small in a single tidy "
        "bottom info strip. Render every DETAILS line — do not drop any fact — "
        "but keep them compact."
    )


# The reusable backbone. This is what makes the look consistent with NO user
# references (the common case): the anatomy + palette are pinned here.
_HOUSE_STYLE_BLOCK = (
    "THE 42 INFORMATION-POSTER HOUSE STYLE — build every poster from this fixed "
    "anatomy. The person and the words change; the style does NOT.\n\n"
    "  1. REAL HUMAN HERO — one real, photographed person is the visual anchor "
    "(see THE HUMAN below). Cut them cleanly onto a flat brand-colour field or "
    "place them in a believable environment, pushed to one side so the type "
    "has room.\n"
    "  2. STACKED HEADLINE — the loudest element: 2-5 short words in a heavy "
    "geometric sans-serif (Futura / Archivo-Black / Neue-Haas weight), ALL "
    "CAPS, on 2-3 tight lines. One word may be knocked out in red or cyan for "
    "emphasis. It reads first.\n"
    "  3. SUPPORTING SUBTITLE — one short calm sentence in a lighter weight of "
    "the same sans, directly under or beside the headline.\n"
    "  4. COLOURED CARD (optional, a signature of this style) — when the "
    "content gives a person's name+role or a short quote, place it on ONE "
    "solid rounded rectangle filled red, purple, or cyan (or a small speech "
    "bubble for a quote), with crisp contrasting text. Use it only when there "
    "is something to attribute; do not put a plate behind every line.\n"
    "  5. INFO STRIP (only when the content gives event details) — set dates, "
    "times, venue, CTA, link or price small and tidy in a single row or band "
    "near the bottom edge (a thin coloured bar or plain aligned text). Keep it "
    "legible at thumbnail size; never let it crowd the headline or the "
    "reserved top-right corner.\n"
    "  6. PIXEL / 8-BIT DNA — small, deliberate, brand-signature accents: a "
    "cluster of square pixels dissolving off one edge of the cut-out, a chunky "
    "8-bit arrow / plus / triangle, a pixel mouse-cursor or tiny 8-bit smiley "
    "tile, a pixel-block underline, corner brackets, or a low-res dithered "
    "photo edge. Genuinely blocky, aliased, limited-colour. A garnish only — "
    "never a full pixel-grid, circuit-board, or scanline background, and never "
    "over the face or the headline.\n"
    "  7. LAYOUT — a strong grid, generous negative space, clear reading order "
    "(headline, then subtitle, then card, then info strip), flat solid colour "
    "fields and hard edges. Deliberate and editorial, like a designed magazine "
    "cover — never a busy collage.\n\n"
    "PALETTE — use these, do not drift:\n"
    "  - RED / red-orange (#E8331C to #FF4133) — the signature, loudest "
    "colour: the biggest headline word, a card, or one bold diagonal block.\n"
    "  - DEEP PURPLE / violet (#2B0A3D near-black plum through #6A1FE0 electric "
    "violet) — full-bleed fields, duotone shadows, secondary cards.\n"
    "  - ELECTRIC CYAN (#00E5FF) — high-energy hits ONLY: pixel motifs, a thin "
    "rule, an accent word, a cut-out edge glow, a card outline. Never the "
    "dominant colour.\n"
    "  - NEUTRALS — true near-black #0B0B0F and warm off-white #F4F1EA.\n"
    "Pick ONE background regime per poster: DARK (near-black or deep-purple "
    "field) OR LIGHT (off-white field); the other brand colours then pop "
    "against it. Keep colours saturated, clean, high-contrast.\n\n"
    "Keep this anatomy every time. You MAY vary the person, pose, crop, which "
    "brand colour leads, dark vs light field, the knocked-out word, the card "
    "colour, and the pixel-accent placement — but never drop the real human "
    "hero, the stacked headline, or the clean editorial feel."
)


_FORMAT_LINE = (
    "FORMAT: one SQUARE (1:1) Instagram poster — a single designed "
    "composition, not a collage and not a plain photo. All on-poster text is "
    "ORIGINAL to this topic; never echo wording from any other poster. "
    "Remember: place the ATTACHED 42 logo image in the top-right corner — "
    "never letter your own mark."
)


# Recency anchor — placed LAST so the iron rules bracket the whole prompt.
_FINAL_CHECKLIST = (
    "FINAL CHECK before you output — confirm ALL of these:\n"
    "  [ ] Every word spelled exactly right, Latin script only, ZERO Arabic / "
    "non-Latin glyphs, no invented text or labels.\n"
    "  [ ] The 42 mark is the ATTACHED logo image placed small in the "
    "top-right corner — never a hand-lettered or invented one.\n"
    "  [ ] Exactly one real, photographic human hero — real skin, not a smooth "
    "AI / CGI face.\n"
    "  [ ] Stacked bold headline + short subtitle; red/orange + purple + cyan "
    "on near-black or off-white; small blocky pixel accents; clean editorial "
    "layout.\n"
    "  [ ] Any name/quote on one colour card; any event facts in a tidy bottom "
    "info strip.\n"
    "  [ ] Square 1:1, single composition, no collage."
)

_TEXT_FIDELITY_BLOCK = """\
TEXT RULES — non-negotiable. Image generators corrupt typography; that must
not happen here.

ENGLISH SPELLING:
  • Spell every English word EXACTLY as provided — no typos, no letter swaps,
    no dropped letters. Re-read each word before finalising. Past failures:
    'LECTUAES' for 'LECTURES', 'DHAGI' for 'DHABI', 'apu lood' for 'ABU DHABI'.

LATIN SCRIPT ONLY — CRITICAL:
  • Render ZERO Arabic, Hebrew, Persian, Urdu or any non-Latin glyphs — not
    one character, nowhere, not even as decoration. Image generators turn
    non-Latin script into meaningless gibberish (past output: 'البندوس قينر',
    which is not real Arabic) and it ruins the poster. Where you might be
    tempted to add such script, use a pixel motif, a colour block, or empty
    space instead.

ONLY THESE WORDS:
  • Typeset only the words in the poster content — headline, subtitle, and (if
    present) the card and the detail lines. Do NOT invent extra headlines,
    taglines, slogans, body copy, hashtags, dates, or callouts. Do NOT render
    any instruction label (words like 'HEADLINE', 'SUBTITLE', 'CARD',
    'DETAILS') as visible text. If a space looks empty, leave it empty or fill
    it with a flat colour block or a pixel accent — never with invented text.

LENGTH (short text = fewer typos):
  • Headline 2-5 words. Subtitle one short sentence. Card: a name plus a short
    role, or a quote of a dozen words at most. Info-strip details: each fact a
    short line. Any pixel / blocky lettering: 2-4 short words max — it is the
    highest typo risk."""


# The logo rule. The real 42 logomark is sent as a separate reference IMAGE
# part (LOGO_INSTRUCTION + POSTER_LOGO_PATH in the router — the same mechanism
# Create Poster uses), so the model composes the REAL mark in-render. This
# block keeps it from ALSO lettering a fake one and reserves clean space for it.
# Together with deleting the old "BRAND MARK: always include a 42 logomark"
# bullet, this is the fix for the hallucinated / fake logo.
_LOGO_CORNER_BLOCK = (
    "THE 42 LOGO — USE THE ONE PROVIDED. The official 42 Abu Dhabi logomark is "
    "attached to this request as a separate flat reference image. Your job is "
    "to PLACE it, not to draw it:\n"
    "  • Place the ATTACHED logo image, unchanged, small in the TOP-RIGHT "
    "corner — flat, no 3D, no drop-shadow, no glow, no recolour, no "
    "distortion, no cropping.\n"
    "  • Do NOT letter, redraw, or invent any '42', '42 ABU DHABI' wordmark, "
    "badge, monogram, or seal of your own — anywhere in the image. A mark you "
    "make yourself is WRONG; only the attached logo image is correct.\n"
    "  • Keep the TOP-RIGHT corner uncluttered (a calm, slightly darker area "
    "reads best) so the logomark sits cleanly — no headline, face, subtitle, "
    "card, or info strip crowding that corner.\n"
    "  • Every OTHER piece of text (headline, subtitle, name/role card, quote, "
    "event details) is still yours to render per the poster content."
)

_HUMAN_HERO_BLOCK = (
    "THE HUMAN — REQUIRED, AND IT MUST LOOK REAL. Show exactly one real, "
    "photographic person as the hero (a small group only if the content "
    "explicitly asks). Render them as a genuine editorial / documentary "
    "PHOTOGRAPH: real skin with real texture and pores, real hair, natural "
    "asymmetry, an authentic candid expression, believable lighting. NOT a "
    "smooth doll-faced or waxy AI person, NOT a 3D render, NOT a cartoon or "
    "illustration. Cut them out cleanly onto the flat brand-colour field (a "
    "crisp silhouette, not a rectangular photo box) or place them in a real "
    "environment; a full-colour cut-out with a bold brand-colour rim or hard "
    "drop-shadow and a few pixel-block accents chewing one edge is the default "
    "treatment. Keep the FACE clearly readable and human. Frame mid-chest-up "
    "or full-body pushed to one side. Unless the content names or describes a "
    "different person, the hero is an Emirati national: a young man in a white "
    "kandura and ghutra, or a young woman in an abaya with a shayla framing a "
    "fully visible face (no niqab, no sunglasses). Vary age, gender, face and "
    "pose across posters; present them naturally and respectfully, never "
    "caricature. If the content names a specific real person, the person shown "
    "must match that description."
)

# Pivot-only variety note (replaces the old _DIVERSITY_BLOCK, which actively
# FORBADE the reference ingredients — centered subjects, dotted backgrounds,
# and colour plates behind text). Variety now comes from levers that don't
# break the locked anatomy.
_VARY_WITHIN_TEMPLATE = (
    "STAY ON-TEMPLATE, BUT NOT IDENTICAL: keep the house-style anatomy above "
    "and get variety from the safe levers that don't break it — a different "
    "person, dark vs light field, which brand colour leads, the card colour, "
    "the knocked-out headline word, and where the pixel accent and info strip "
    "sit. Do NOT abandon the stacked headline, the real cut-out human, or the "
    "colour cards to be 'creative'."
)


_ANTI_AI_BLOCK = (
    "DO NOT LOOK LIKE GENERIC AI OUTPUT. This poster mixes exactly two "
    "finishes and nothing else:\n"
    "  • THE PERSON — a real, unretouched-looking PHOTOGRAPH: real skin "
    "texture, real pores, stray hairs, natural catchlights. No airbrush, no "
    "plastic smoothing, no beauty filter, no uncanny symmetry.\n"
    "  • EVERYTHING AROUND THEM — a flat, graphic, print-style system: solid "
    "colour fields, hard edges, crisp bold type, blocky 8-bit pixel motifs. "
    "Flat and honest like a screen-print, not glossy 3D.\n"
    "Specifically AVOID:\n"
    "  • Soft airbrushed gradients on every surface; cinematic glow, bloom, "
    "lens flare, neon halos, floating light particles, background bokeh.\n"
    "  • Fake 3D depth, bevels, or glass on the flat graphic elements.\n"
    "  • A default 'futuristic' cyber-city, glowing circuit-board, or "
    "holographic-grid background — that is NOT the 42 look; prefer a flat "
    "colour field, a real environment, or sparse pixel decoration.\n"
    "  • Wrapping every text element in its own glowing plate or neon badge — "
    "the only colour plates allowed are the single attribution card and the "
    "info strip.\n"
    "  • Muddy, pastel, or desaturated colour that misses the loud red / "
    "purple / cyan.\n"
    "Flat, deliberate, print-like character is GOOD; glossy 'AI-cinematic' "
    "perfection is BAD."
)


# Appended to both retry branches: user feedback often refers to the 42 logo
# ("make the logo bigger"). Satisfy it using ONLY the attached logo image —
# never let feedback re-summon a hand-lettered fake.
_FEEDBACK_LOGO_GUARD = (
    "If the feedback mentions the 42 logo (make it bigger, move it, fix it, "
    "etc.), satisfy it using ONLY the attached 42 logo image — reposition or "
    "resize that provided mark in the top-right corner. Never letter or invent "
    "your own '42' / 'ABU DHABI' wordmark to satisfy the request."
)


def _sanitize_style_attrs(attrs: dict | None) -> dict | None:
    """Strip any Arabic/non-Latin mentions from the extracted style so they
    don't fight the text-fidelity block. gpt-image-1 reliably hallucinates
    gibberish Arabic when the extracted attrs say things like 'pairs Arabic
    and English text' — even with our explicit anti-Arabic rule.

    Filters:
      • Drop any composition_idiom that mentions Arabic/Hebrew/non-Latin
      • Strip mentions of Arabic from string fields (replace with English)
    """
    if not attrs:
        return attrs
    forbidden_terms = ("arabic", "hebrew", "persian", "urdu", "calligraphic", "bilingual")
    # STYLE_EXTRACT_SCHEMA literally offers "always shows the 42 logomark in a
    # corner" as an example idiom — drop any such cue so it can never smuggle a
    # drawn-logo instruction back past the logo firewall. NB: no bare "42"
    # (too broad — it would strip legitimate brand-name idioms).
    logo_terms = ("logo", "logomark", "wordmark", "brand mark", "brand-mark")

    def _contains_forbidden(s: str) -> bool:
        if not s:
            return False
        s_low = s.lower()
        return any(t in s_low for t in forbidden_terms)

    def _drop_from_list(s: str) -> bool:
        if not s:
            return False
        s_low = s.lower()
        return any(t in s_low for t in forbidden_terms) or any(t in s_low for t in logo_terms)

    cleaned = dict(attrs)
    # Filter composition_idioms array (non-Latin AND logo mentions)
    idioms = cleaned.get("composition_idioms") or []
    if isinstance(idioms, list):
        cleaned["composition_idioms"] = [
            i for i in idioms if not _drop_from_list(str(i))
        ]
    # Drop string fields that mention non-Latin scripts
    for k in ("background_treatment", "typography_mood", "illustration_style",
              "imagery_medium", "lighting", "texture_and_finish",
              "design_era_reference", "anti_ai_signals"):
        v = cleaned.get(k)
        if isinstance(v, str) and _contains_forbidden(v):
            # Don't drop entirely — just append an override note
            cleaned[k] = v + " (NOTE: render in ENGLISH ONLY — ignore any Arabic/non-Latin mention)"
    # Filter background_patterns array (non-Latin AND logo mentions)
    bg = cleaned.get("background_patterns") or []
    if isinstance(bg, list):
        cleaned["background_patterns"] = [
            i for i in bg if not _drop_from_list(str(i))
        ]
    return cleaned


def _format_style_block(style_attrs: dict | None) -> str:
    """Render the extracted style fingerprint as an OPTIONAL refinement layered
    ON TOP of the locked house style. Returns "" when there are no references
    (the common case) — build_initial_prompt / build_retry_prompt filter the
    empty block out and the house style stands on its own.

    Only palette / texture / typography / anti-AI cues are surfaced. The
    layout- and medium-level fields (imagery_medium, illustration_style,
    background_treatment, background_patterns, composition_idioms, brand_cues)
    are deliberately DROPPED so the extractor can never swing the poster
    anatomy or override the real-photo human hero.

    `style_attrs` is the JSON dict returned by the Gemini Flash extractor —
    see STYLE_EXTRACT_SCHEMA below.
    """
    if not style_attrs:
        return ""

    palette = style_attrs.get("palette") or {}
    palette_desc = (palette.get("palette_description") or "").strip()
    palette_colors = palette.get("dominant_colors") or []

    lines: list[str] = [
        "OPTIONAL STYLE REFINEMENT — extracted from the user's own reference "
        "images. Use it ONLY to fine-tune palette, surface texture, and type "
        "flavour ON TOP OF the fixed house style above. It must NOT change the "
        "poster anatomy (one real photographic human hero + stacked headline + "
        "short subtitle + at most one colour card + optional info strip), must "
        "NOT reintroduce any 42 logo, and must NOT add Arabic or non-Latin "
        "script. The hero always stays a real photograph even if a cue names a "
        "different medium — apply any illustrative cue to the graphics and "
        "background only. If any cue conflicts with the house style, the text "
        "rules, or the logo rule, ignore that cue."
    ]
    if palette_desc or palette_colors:
        col_str = f" ({', '.join(palette_colors[:6])})" if palette_colors else ""
        lines.append(
            "  • Palette to lean toward (keep it in the red / purple / cyan "
            f"family): {palette_desc}{col_str}"
        )
    if style_attrs.get("texture_and_finish"):
        lines.append(f"  • Texture / finish to echo: {style_attrs['texture_and_finish']}")
    if style_attrs.get("typography_mood"):
        lines.append(f"  • Type flavour to echo: {style_attrs['typography_mood']}")
    if style_attrs.get("anti_ai_signals"):
        lines.append(f"  • Keep-it-real cue: {style_attrs['anti_ai_signals']}")
    # Only the intro header + no concrete cues survived -> nothing worth adding.
    if len(lines) == 1:
        return ""
    return "\n".join(lines)


def build_initial_prompt(
    *,
    topic: str,
    style_attrs: dict | None = None,
) -> str:
    """Compose the prompt for the first attempt in a session.

    Nano Banana receives this as TEXT ONLY (no source_images on the initial
    call — that's what kills the layout-copying behaviour). The 42
    information-poster house style is pinned entirely with words; the extracted
    style fingerprint, when present, is layered LAST as an OPTIONAL refinement
    (empty string when there are no references — filtered out below — so the
    house style stands on its own). The 42 logomark is NOT drawn here: the
    caller composites the real logo top-right after generation.
    """
    blocks = [
        _MANDATE_BLOCK,
        _poster_content_block(topic),
        _HOUSE_STYLE_BLOCK,
        _HUMAN_HERO_BLOCK,
        _TEXT_FIDELITY_BLOCK,
        _LOGO_CORNER_BLOCK,
        _ANTI_AI_BLOCK,
        _format_style_block(_sanitize_style_attrs(style_attrs)),
        _FORMAT_LINE,
        _FINAL_CHECKLIST,
    ]
    return "\n\n".join(b for b in blocks if b and b.strip())


def build_retry_prompt(
    *,
    topic: str,
    style_attrs: dict | None,
    feedback: str,
    retry_kind: Literal["tweak", "pivot"],
) -> str:
    """Compose a retry prompt on the SAME locked house style.

    Image-to-image semantics:
      • tweak — caller passes ONLY the previous attempt as source_images
        (genuine image-to-image on Gemini Pro, which really conditions on the
        source): keep the layout / person / house style, change ONLY what the
        feedback asks.
      • pivot — caller passes NO source_images: regenerate from text in a NEW
        direction, still inside the house-style anatomy.

    Both paths keep the reserved-logo-corner rule so the post-generation
    composite stays consistent across the whole retry chain.
    """
    feedback_clean = feedback.strip()

    common_head = [
        _MANDATE_BLOCK,
        _poster_content_block(topic),
        _HOUSE_STYLE_BLOCK,
        _HUMAN_HERO_BLOCK,
        _TEXT_FIDELITY_BLOCK,
        _LOGO_CORNER_BLOCK,
        _ANTI_AI_BLOCK,
    ]
    style = _format_style_block(_sanitize_style_attrs(style_attrs))

    if retry_kind == "tweak":
        iter_block = (
            "THIS IS A REVISION. The previous version of this poster is "
            "attached as the reference image. Keep its layout, its human hero, "
            "and its 42 house style intact, and make ONLY the change the user "
            "asks for below. Do not redesign from scratch, do not swap the "
            "person, and do not restyle unrelated parts. Keep the 42 logomark "
            "in the top-right using the ATTACHED logo image; do not letter your "
            "own. The previous image already shows the real 42 logo top-right — "
            "keep it there, unchanged."
        )
        blocks = common_head + [
            style,
            iter_block,
            f"USER FEEDBACK ON THE PREVIOUS ATTEMPT:\n  {feedback_clean}",
            _FEEDBACK_LOGO_GUARD,
            _FORMAT_LINE,
            _FINAL_CHECKLIST,
        ]
        return "\n\n".join(b for b in blocks if b and b.strip())

    # pivot: reset the composition entirely, same locked house style
    pivot_block = (
        "An earlier attempt missed the mark. Keep the 42 house style locked "
        "above, but take a NEW direction within it:\n  "
        f"{feedback_clean}\n"
        "Reset the composition — use a different person and a different "
        "arrangement of the same elements; do not reproduce the prior attempt. "
        "Stay inside the house-style anatomy (real human hero + stacked "
        "headline + short subtitle + optional colour card + optional info "
        "strip) and place the attached 42 logo image small in the top-right "
        "corner."
    )
    blocks = common_head + [
        pivot_block,
        _FEEDBACK_LOGO_GUARD,
        style,
        _VARY_WITHIN_TEMPLATE,
        _FORMAT_LINE,
        _FINAL_CHECKLIST,
    ]
    return "\n\n".join(b for b in blocks if b and b.strip())


# ===========================================================================
# POSTER + HUMAN as a PosterStyle — the 4-variant, two-stage batch form.
#
# The single-shot `build_initial_prompt` above pins the whole look in one text
# prompt. To produce a Midjourney-style 2x2 grid the SAME way Create Poster /
# Human Character do, we split that house style into a Stage-1 `concepts_system`
# ("what to depict" — N different COMPOSITIONS of the SAME content) and a
# Stage-2 `gemini_stage2_system` ("how to draw"), then run them through the
# existing `_poster_concepts`-style loop + batch render. Text is FIXED across
# the N concepts (the user supplies the copy); only the visuals vary.
#
# HUMAN_CHARACTER_STYLE is the precedent: a photographic real-human PosterStyle
# held OUTSIDE poster_styles.POSTER_STYLES (which is asserted to be exactly the
# four pickable looks) and passed straight to the batch. `key` becomes the row's
# stored `style`. The logo IS sent as a reference image part (include_logo=True),
# unlike Human Character — so the render system says "place the attached logo".
# ===========================================================================

_PH_TEXT_TO_RENDER_CONTRACT = (
    "[TEXT TO RENDER CONTRACT]\n"
    "Each concept's `prompt` MUST end with this exact block, filled with the "
    "VERBATIM copy from the poster content (never reword, translate, shorten, or "
    "invent). If the copy arrives as labelled lines (HEADLINE:/SUBTITLE:/CARD:/"
    "DETAILS:), map each to its role; typeset only the text AFTER each label.\n"
    "[TEXT TO RENDER]\n"
    "PLACEMENT: <where the text sits in THIS composition>\n"
    'TEXT_HEADLINE: "<the headline, ALL CAPS>"\n'
    'TEXT_SUBTITLE: "<the supporting line — omit the line if none>"\n'
    'TEXT_CARD: "<name + role, or a short quote — omit the line if none>"\n'
    'TEXT_DETAILS: "<each concrete fact: date / time / venue / CTA / link — omit if none>"\n'
    "The WORDS are identical in every concept; only PLACEMENT changes with the layout."
)

POSTER_HUMAN_CONCEPTS_SYSTEM = "\n\n".join([
    "You are the art director for 42 Abu Dhabi — a tuition-free, peer-learning "
    "coding school in the UAE. You design Instagram INFORMATION POSTERS, each "
    "with a real photographed person. Given ONE topic plus the exact on-poster "
    "copy, invent N genuinely DIFFERENT poster COMPOSITIONS of that SAME content "
    "— same words, different layouts. Each concept is a complete, self-contained "
    "scene description an image model can render on its own with no other context.",
    _HOUSE_STYLE_BLOCK,
    _HUMAN_HERO_BLOCK,
    "[TEXT IS FIXED — DO NOT REWRITE IT]\n"
    "The poster copy (headline / subtitle / card / details) is provided and is "
    "FINAL. Every concept reproduces it VERBATIM in its [TEXT TO RENDER] block — "
    "identical words in all N. You vary the VISUALS only (composition, which side "
    "the hero sits, crop, which brand colour leads, dark-vs-light field, card "
    "colour, pixel-motif type and placement), NEVER the words. Do not invent copy.",
    "[DIVERSITY MANDATE — N DIFFERENT COMPOSITIONS OF THE SAME POSTER]\n"
    "Spread the N concepts so no two look alike: 1) the person (age / gender / "
    "face / pose, still Emirati unless the content names someone else, cut-out "
    "vs in-environment); 2) which side the hero sits "
    "and the crop (mid-chest vs full-body); 3) which brand colour leads and "
    "dark-vs-light field; 4) headline placement + which word is knocked out; "
    "5) the pixel-motif type and where it sits. Keep the locked anatomy in every "
    "one — never drop the real photographic human hero, the stacked headline, or "
    "the clean editorial feel.",
    _PH_TEXT_TO_RENDER_CONTRACT,
    "[OUTPUT — STRICT JSON]\n"
    'Return ONE JSON object: {"concepts": [{"summary": "...", "prompt": "..."}, ...]} '
    "with exactly N entries. `summary` = a short DISTINCT label for the "
    "composition (e.g. 'hero left, red field, pixel arrows top-right'); summaries "
    "must differ. `prompt` = the full, vivid, self-contained scene description "
    "ending with the [TEXT TO RENDER] block. Restate the full look in EACH prompt "
    "(the model renders each independently and shares no memory between them). "
    "English only; no commentary outside the JSON.",
])


POSTER_HUMAN_STAGE2_SYSTEM = "\n\n".join([
    "You are an expert graphic designer and brand custodian for 42 Abu Dhabi. "
    "Render the detailed information-poster scene described into a single premium "
    "Instagram poster with a REAL photographic person as the hero, strictly "
    "enforcing the rules below.",
    _LOGO_CORNER_BLOCK,
    _ANTI_AI_BLOCK,
    _TEXT_FIDELITY_BLOCK,
    "TYPOGRAPHY — scroll to the [TEXT TO RENDER] block at the very end of the "
    "prompt. Obey its PLACEMENT. Print ONLY the quoted strings (the headline, "
    "subtitle, card, and detail lines) EXACTLY as written; never print the labels "
    "themselves (PLACEMENT / TEXT_HEADLINE / TEXT_SUBTITLE / TEXT_CARD / "
    "TEXT_DETAILS). If a line is absent, do not invent one.",
    "OUTPUT CONSTRAINTS — render in the EXACT aspect ratio named in the render "
    "directive at the top of the prompt, as a single cohesive composition (never "
    "a collage). English / Latin script only. Exactly ONE real photographic "
    "human hero — never a smooth AI / CGI face.",
    _FINAL_CHECKLIST,
])


# Explore nudges — subtle same-content changes for the "4 more like this" riffs.
POSTER_HUMAN_VARIATION_NUDGES: tuple[str, ...] = (
    "Move the hero to the opposite side and re-balance the headline.",
    "Switch the background regime (dark <-> light) and change which brand colour leads.",
    "Change the pixel-motif cluster (arrows / dots / cursor) and where it sits.",
    "Recolour the attribution card and open up more negative space in the layout.",
)


def build_poster_human_user_message(
    topic: str,
    n: int,
    recent_summaries: list[str] | None = None,
) -> str:
    """Stage-1 user message: ask gpt-4o for `n` DIFFERENT compositions of the
    SAME poster content as strict JSON, biased away from recent compositions.
    Mirrors `build_human_character_user_message` (the shared
    `build_concepts_user_message` is voxel-flavoured, so it is not reused here)."""
    topic = (topic or "").strip()
    recents = [s for s in (recent_summaries or []) if (s or "").strip()]
    avoid = (
        "\n".join(f"- {s.strip()}" for s in recents[-12:]) if recents else "(none yet)"
    )
    return (
        f"[POSTER CONTENT — the exact words to typeset, verbatim in EVERY concept]\n{topic}\n\n"
        f"[HOW MANY]\nProduce exactly {n} DIFFERENT compositions of the SAME "
        f"poster content — same words, different layouts per the diversity "
        f"mandate.\n\n"
        f"[ALREADY USED — AVOID repeating these compositions]\n{avoid}\n\n"
        f"[REMINDER]\nReturn STRICT JSON: a \"concepts\" array of exactly {n} "
        f"{{summary, prompt}} objects. Every prompt ends with the [TEXT TO RENDER] "
        f"block containing the EXACT copy above, identical across all {n}."
    )


# The PosterStyle object the batch consumes. Held OUTSIDE poster_styles.POSTER_STYLES
# (asserted to be exactly voxel/pattern/popup) and passed straight to the batch,
# exactly like HUMAN_CHARACTER_STYLE. `key` becomes the stored `style` value.
POSTER_HUMAN_STYLE = PosterStyle(
    key="poster_human",
    label="Poster + Human",
    description="Editorial 42 information poster with a real photographic human hero.",
    concepts_system=POSTER_HUMAN_CONCEPTS_SYSTEM,
    gemini_stage2_system=POSTER_HUMAN_STAGE2_SYSTEM,
    variation_nudges=POSTER_HUMAN_VARIATION_NUDGES,
)


# ---------------------------------------------------------------------------
# TEMPLATE-FILL MODE — distinct prompt for "use this poster as a literal
# template, drop this exact student's face into it, change the headline
# to the new topic". Different mental model from style_inspired:
#
#   style_inspired = "be inspired by, don't copy"  (text-only generation)
#   template_fill  = "use the template literally, preserve the student's
#                     face, only the text changes"  (image-to-image)
#
# Calls Nano Banana (Gemini 2.5 Flash Image) via gemini_image with
# source_images=[template, student] — multi-image conditioning is what
# this model is built for. Face preservation is a hard ask for any image
# model; this prompt strategy pushes the model hard to honor IMAGE 2.
# ---------------------------------------------------------------------------

_TEMPLATE_FILL_TEXT_RULES = """\
TEXT RULES — non-negotiable:
  • Render English text only. NO ARABIC, NO HEBREW, NO non-Latin scripts.
    Past failure: 'البندوس قينر' (meaningless gibberish). DO NOT add
    decorative Arabic-looking text anywhere.
  • Spell every English word EXACTLY as I provide. No typos. No letter
    swaps (past failures: 'LECTUAES', 'DHAGI', 'apu lood').
  • The "42 ABU DHABI" brand logomark stays in its original position from
    the template. Render it in ENGLISH ONLY — the number "42" plus the
    English text "ABU DHABI". Do NOT add Arabic transliteration even if
    the template's logo had Arabic alongside.
  • Use the topic to derive the headline + any subtitle text the template
    needs. Don't add invented copy or hashtags beyond what the topic says.
"""


def build_template_fill_prompt(
    *,
    topic: str,
    feedback: str | None = None,
) -> str:
    """Compose the template-fill prompt for Nano Banana.

    The caller must pass exactly TWO source_images in this order:
      1. The TEMPLATE poster (design to preserve)
      2. The STUDENT photo (face to preserve)
    """
    feedback_block = ""
    if (feedback or "").strip():
        feedback_block = (
            "\n\nUSER FEEDBACK ON THE PREVIOUS ATTEMPT:\n  "
            f"{feedback.strip()}\n"
            "Apply this feedback while still respecting all the rules above."
        )

    return f"""\
You are creating a 42 Abu Dhabi Instagram poster by COMPOSITING two images.

==========================================================================
FACE LOCK — THE SINGLE MOST IMPORTANT RULE. READ TWICE.
==========================================================================
IMAGE 2 (SECOND attached image) shows ONE specific real person. Their face
is OFF-LIMITS. Treat IMAGE 2 as a photographic cutout you are pasting into
the poster — not as a reference, not as a starting point, not as
"inspiration". You are not redrawing this person. You are placing their
actual face into the design.

YOU MUST NOT, UNDER ANY CIRCUMSTANCE:
  • Redraw, repaint, re-render, or re-illustrate the face.
  • "Smooth", "clean up", "beautify", "deage", "youthify" the face.
  • Change skin tone, skin texture, pores, freckles, scars, moles.
  • Change eye color, eye shape, eyebrow shape, nose, lips, jawline,
    cheekbones, ears, chin, forehead.
  • Change hair color, hair length, hair texture, hairline, or hijab.
  • Add or remove glasses, facial hair, makeup, piercings, jewelry.
  • Change the person's age, gender, ethnicity, or expression.
  • Apply ANY stylization to the face — no cartoon, no pixel art, no
    illustration, no painterly filter, no halftone, no duotone, no
    desaturation, no posterize, no color grade. The face stays a real
    photograph. Even if IMAGE 1 (the template) is heavily stylized (pixel
    art, cartoon, illustration, vector), the face from IMAGE 2 STAYS
    PHOTOREAL and PIXEL-FOR-PIXEL the same person.
  • Swap, blend, or merge the face with anyone else.
  • Generate a "similar-looking" person — that is FAILURE.

THINK OF IT THIS WAY: the face from IMAGE 2 is a sticker. You may
reposition it, scale it, or rotate it to fit the template's subject slot.
You may NOT edit the pixels of the face itself. If you cannot preserve
the face exactly, the entire output is wrong.

==========================================================================

IMAGE 1 (FIRST attached image) is the TEMPLATE. It shows the brand's design
language: layout, color palette, color blocks, decorative pixel motifs,
typography style, brand-mark position, background treatment. PRESERVE all
of these design choices exactly:
  • Where the human subject sits in the frame — keep that position
  • Where the headline + subtitle text sits — keep that placement
  • Where the "42 ABU DHABI" logomark sits — keep that corner
  • The color palette + any geometric overlay blocks
  • The decorative elements (pixel arrows, dots, leaves, geometric shapes)
  • The background treatment (flat color / textured / photographic / etc)
  • The overall composition feel

The body / clothing / pose around the face MAY be adapted to match the
template's style (e.g. if IMAGE 1 shows a stylized illustrated body, you
may render a body in that style). But the FACE from IMAGE 2 stays a
verbatim photograph — see the FACE LOCK block above.

REPLACE the headline text from IMAGE 1 with text derived from this topic:
  TOPIC: {topic.strip()}

{_TEMPLATE_FILL_TEXT_RULES}{feedback_block}

OUTPUT FORMAT: same aspect ratio as IMAGE 1 (typically 1:1 square for IG).
Single composition — match the template's framing exactly.

DO NOT:
  • Modify the face from IMAGE 2 in ANY way (see FACE LOCK above)
  • Generate a person whose face doesn't match IMAGE 2 exactly
  • Change the template's layout or composition significantly
  • Add Arabic text anywhere
  • Add typos to the English text
  • Add invented elements (extra hashtags, slogans, callouts) that aren't
    in the template or specified by the topic
"""


# ---------------------------------------------------------------------------
# ANIMATE MODE — Veo 3 image-to-video. The generated poster is passed as
# `start_image=`; Veo extrapolates ~8s of subtle motion with synthesized
# music. Face preservation is the critical bar — same energy as the
# template_fill FACE LOCK block above.
# ---------------------------------------------------------------------------

# NB: deliberately free of "face swap / face morph / identity change" —
# those trip Vertex's RAI text filter (code 29310472) even as negatives.
# Face identity is held by the FACE LOCK positive prompt block, so these
# negatives were redundant anyway. scrub_rai_terms is a final guard.
ANIMATE_NEGATIVE_PROMPT = scrub_rai_terms(
    "distorted face, warped facial features, beauty-filter smoothing, "
    "lip sync to dialogue, talking, mouth open wide, exaggerated smile, "
    "text warping, warping logo, jitter, flicker, fast cuts, "
    "whip pan, zoom in fast, glitch, cartoon filter, painterly filter"
)


# ---------------------------------------------------------------------------
# Style extraction — Gemini Flash multimodal call (in yas_producer.py)
# ---------------------------------------------------------------------------

STYLE_EXTRACT_SYSTEM = (
    "You are a senior brand designer analysing posters to extract the "
    "DESIGN-DNA — the deep, transferable patterns a fresh designer could use "
    "to create a new poster that unmistakably belongs to the same brand "
    "family, but is a CLEARLY DIFFERENT composition.\n\n"
    "Be SPECIFIC and CONCRETE — generic AI-flavoured words ('modern', "
    "'vibrant', 'dynamic', 'sleek') are USELESS. Replace them with the "
    "actual visual ingredients: name the medium, name the patterns, name "
    "the textures, name the typographic conventions, name the era this "
    "design language references (80s arcade, Swiss editorial, 90s rave, "
    "Risograph zine, modern editorial, etc.).\n\n"
    "STRICTLY do NOT describe:\n"
    "  - layout / composition / framing / element placement\n"
    "  - the subject's pose, gender, age, identity\n"
    "  - text CONTENT visible in the image (you may describe the typography "
    "MOOD but never the words)\n"
    "  - the topic of the existing poster\n\n"
    "DO describe:\n"
    "  - palette with concrete hex codes\n"
    "  - the SPECIFIC visual medium (flat vector? halftone screen print? "
    "real photography? 8-bit pixel art? hand-drawn line work?)\n"
    "  - background TREATMENT — is the background a flat color, a photo, "
    "a halftone pattern, a gradient, an organic texture? Describe the "
    "EXACT recurring background motif if there is one (e.g. 'fine "
    "scanline overlay', 'concentric arabic geometric pattern', "
    "'paper-grain off-white')\n"
    "  - background PATTERNS that recur as brand idiom (grid? halftone? "
    "geometric? noise? scanlines? circles? Arabic calligraphic motifs?)\n"
    "  - imagery medium — is this a photograph of real people, a flat "
    "illustration, a 3D render, or a mixed treatment?\n"
    "  - composition idioms that signal the brand even though we won't "
    "copy specific layouts (e.g. 'always pairs Arabic and English text', "
    "'always uses negative space generously', 'always asymmetric')\n"
    "  - anti-AI signals — pick out anything that makes this NOT look like "
    "generic AI output: paper grain, print misregistration, deliberate "
    "imperfection, real photographic grain, 8-bit blockiness, hand-drawn "
    "irregularity, etc."
)


# Gemini's response_schema dialect uses uppercase type names.
STYLE_EXTRACT_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "palette": {
            "type": "OBJECT",
            "properties": {
                "dominant_colors": {
                    "type": "ARRAY",
                    "items": {"type": "STRING"},
                    "description": "3-6 hex codes (e.g. '#1a0033', '#00ebff') that define the palette.",
                },
                "palette_description": {
                    "type": "STRING",
                    "description": "One concrete line characterising the palette, e.g. 'electric cyan + magenta on near-black, with warm amber accents'. NEVER 'vibrant modern colors'.",
                },
            },
            "required": ["palette_description"],
        },
        "imagery_medium": {
            "type": "STRING",
            "description": "What the imagery actually IS. Pick the closest: 'real photograph of people', 'flat vector illustration', '8-bit pixel art', 'hand-drawn line illustration', 'isometric 3D illustration', 'halftone screen-print', 'risograph zine', 'collage/mixed-media', or describe a specific blend. NEVER 'digital illustration' or 'AI-style render' — be more specific.",
        },
        "illustration_style": {
            "type": "STRING",
            "description": "Concrete treatment of the imagery — e.g. 'flat vector with chunky 4px outlines and no shading', 'neon airbrush portrait with bloom on dark', 'pixel sprite with 16-color palette and dithering', 'halftone-screened editorial photo'. NEVER 'modern style' or 'sleek look'.",
        },
        "background_treatment": {
            "type": "STRING",
            "description": "What the background is doing — specific. e.g. 'flat near-black with no decoration', 'fine paper-grain off-white', 'photographic shot of campus interior, blurred', 'halftone dot pattern in single accent color', 'concentric Arabic geometric pattern'. Name the EXACT motif if recurring.",
        },
        "background_patterns": {
            "type": "ARRAY",
            "items": {"type": "STRING"},
            "description": "Specific recurring background patterns / motifs visible — 'circuit-board traces', 'scanlines', 'halftone dots', 'isometric grid', 'noise/grain', 'arabic geometric tile', 'concentric circles', 'gradient mesh'. Empty array if the background is plain.",
        },
        "typography_mood": {
            "type": "STRING",
            "description": "How typography feels — specific. e.g. 'bold geometric sans-serif with neon outer glow', 'pixel monospace at small sizes for body text', 'condensed humanist sans-serif with tight tracking'. NEVER 'modern font' or 'bold typography'.",
        },
        "composition_idioms": {
            "type": "ARRAY",
            "items": {"type": "STRING"},
            "description": "Up to 4 brand-signature compositional habits that should carry over WITHOUT copying specific layouts. e.g. 'pairs Arabic and English text', 'subject in lower-right with text in upper-left', 'uses 1-2 word stacked headlines', 'always shows the 42 logomark in a corner'.",
        },
        "lighting": {
            "type": "STRING",
            "description": "Lighting character — e.g. 'high-contrast neon glow on dark', 'flat ambient (no light direction)', 'soft natural daylight from screen-right', 'studio rim light against black backdrop'.",
        },
        "texture_and_finish": {
            "type": "STRING",
            "description": "Surface qualities the brand uses — 'paper grain with subtle ink bleed', 'crisp digital with no texture', 'risograph print misregistration', 'halftone screen', 'glossy chrome', 'CRT scanline overlay'.",
        },
        "design_era_reference": {
            "type": "STRING",
            "description": "Which design era / movement does this echo? e.g. '80s arcade game UI', 'Swiss editorial print', 'late-90s Wired magazine', 'modern editorial poster', 'cyberpunk anime title cards', 'Memphis Group'. One short phrase.",
        },
        "anti_ai_signals": {
            "type": "STRING",
            "description": "What about this design AVOIDS the generic 'AI-airbrushed glow' look? Name specifics: 'flat 2D with no fake 3D depth', 'real photographic skin texture', 'limited 8-color palette', 'deliberate print imperfection', 'hand-drawn line work'. If the reference itself looks AI-generated, name what should change ('replace the airbrush gradient with flat color blocks').",
        },
        "brand_cues": {
            "type": "ARRAY",
            "items": {"type": "STRING"},
            "description": "Up to 4 short DNA tags — 'tech/futuristic', 'youthful/playful', 'editorial/clean', 'cyberpunk', 'minimal/clinical', 'arcade nostalgia', 'street/graphic'.",
        },
    },
    "required": [
        "palette",
        "imagery_medium",
        "illustration_style",
        "background_treatment",
        "typography_mood",
    ],
}


def style_extract_user_prompt(n_refs: int) -> str:
    plural = "reference image" if n_refs == 1 else f"{n_refs} reference images"
    return (
        f"Extract the DESIGN-DNA from the {plural} attached below. "
        "Output the structured JSON exactly per the schema. "
        "Be SPECIFIC — no generic AI words ('modern', 'vibrant', 'sleek'). "
        "Name actual mediums, actual patterns, actual eras. "
        "Never describe layout, composition, text content, or the existing "
        "poster's topic — only transferable DNA."
    )


_DESCRIBE_POSTER_SYSTEM = (
    "You describe poster images for archival retrieval. Output 2 sentences: "
    "(1) what the poster shows visually — subject, composition, color palette, "
    "typography style; (2) the implied message or audience. Be specific. No "
    "marketing fluff. Output prose only."
)


def describe_poster_prompt() -> tuple[str, str]:
    """Returns (system, user) prompts for Gemini Flash multimodal to describe
    a generated poster image. Used at save-to-assets time so the resulting
    producer_assets row has an ai_description + can be embedded for RAG."""
    return _DESCRIBE_POSTER_SYSTEM, (
        "Describe this poster image in 2 sentences as specified by the system "
        "prompt. The image follows."
    )
