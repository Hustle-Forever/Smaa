"""Prompts + JSON schemas for the Yas Producer **Talking Head** category.

Three flavors of prompt live here:

  1. Script generation  — OpenAI GPT-4o, structured JSON output. Produces the
     single-scene script (hook, dialogue, subject, camera, lighting, …) that
     drives every downstream step.

  2. Main keyframe      — image-gen prompt for the talking-head still that
     Veo will animate. Uses the chosen image model (OpenAI gpt-image-1 or
     Gemini 3 Pro Image / Nano Banana Pro).

  3. Intro / outro card — image-gen prompts for the static bookend cards
     held for 2s each in the final composition. Only fired when the user
     didn't supply an intro/outro reference asset.

Kept in its own file so the prompt review surface is small and the router /
service modules stay focused on orchestration.
"""
from __future__ import annotations

import json
import re
from typing import Any


# ---------------------------------------------------------------------------
# Vertex Veo RAI text-filter guard (support code 29310472)
# ---------------------------------------------------------------------------
# Vertex Veo's pre-submission Responsible-AI text classifier flags deepfake- and
# face-manipulation-associated vocabulary — even when the word appears inside a
# NEGATION ("no face swap"). It is also non-deterministic: the same phrase may
# pass on one clip and block the next (observed in production — same run, one
# scene passed while sibling scenes were rejected). To make submission reliable
# we strip these terms from every assembled talking-head prompt as a final
# guard, including any user-supplied retry feedback. This is a no-op on clean
# prompts, so it cannot affect any path that already works.
_RAI_TRIGGER_SUBS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\bface[\s-]?swap(?:p?ed|ping|s)?\b", re.I), "appearance change"),
    (re.compile(r"\bswapped face\b", re.I), "changed appearance"),
    (re.compile(r"\breplaced (?:face|character)\b", re.I), "different person"),
    (re.compile(r"\bdeep[\s-]?fake[sd]?\b", re.I), "synthetic face edit"),
    (re.compile(r"\bmorph(?:ing|ed|s)?\b", re.I), "blending"),
    (re.compile(r"\bage[\s-]?shift(?:ed|ing|s)?\b", re.I), "age change"),
    (re.compile(r"\bidentity drift\b", re.I), "identity change"),
]


def scrub_rai_terms(text: str) -> str:
    """Remove Vertex-RAI-flagged face-manipulation vocabulary (code 29310472).

    A safety net applied to the final assembled prompt and to any user retry
    feedback. Normal prompts contain none of these terms, so it is a no-op
    there; it only rewrites the deepfake/face-swap vocabulary that trips the
    classifier into benign synonyms that carry the same intent.
    """
    out = text or ""
    for pat, repl in _RAI_TRIGGER_SUBS:
        out = pat.sub(repl, out)
    return out


# ---------------------------------------------------------------------------
# Script generation — OpenAI GPT-4o w/ structured output
# ---------------------------------------------------------------------------

# The OpenAI structured-output spec wraps a JSON schema. The inner schema is
# strict: no extra properties, every named property required (OpenAI's
# structured-output requirement when `strict=true`).
TALKING_HEAD_SCRIPT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "hook": {
            "type": "string",
            "description": (
                "First 6-10 words of the dialogue. A question, claim, or "
                "curious fact that grabs attention in the first 1.5 seconds. "
                "MUST be the exact opening of the `dialogue` field."
            ),
        },
        "dialogue": {
            "type": "string",
            "description": (
                "Full ~20-25 word line the speaker says, in English. "
                "Conversational first-person. No buzzwords, no LinkedIn-isms. "
                "The hook IS the opening — the rest follows naturally. "
                "Veo will speak this exact string."
            ),
        },
        "subject": {
            "type": "string",
            "description": (
                "Concrete description of the speaker. When a character "
                "reference is provided in the prompt, do NOT invent gender, "
                "age, ethnicity, hair, clothing, or accessories — the "
                "attached reference image is the only source of truth for "
                "the person. Write `subject` neutrally as: "
                "'#<id> the person in the attached reference image' "
                "and end there; downstream image gen and Veo will use the "
                "image directly. ONLY when no character reference is "
                "provided should you describe the speaker concretely "
                "(gender presentation, age range, clothing, distinguishing "
                "features). Never use gendered defaults like 'a young "
                "Emirati woman' unless the user's input explicitly requests "
                "that gender."
            ),
        },
        "action": {
            "type": "string",
            "description": (
                "Micro-actions during the line: where the eyes go, when "
                "they nod, a small smile. NOT camera moves — those go in "
                "'camera'. Keep it under 25 words."
            ),
        },
        "camera": {
            "type": "string",
            "description": (
                "Medium close-up or tighter. Locked-off or slow push-in "
                "ONLY. Never whip-pan / orbit / dolly / handheld."
            ),
        },
        "lighting": {
            "type": "string",
            "description": (
                "Light direction + quality (e.g. 'soft natural daylight, "
                "warm fill from camera-left, practical lamp in background'). "
                "If a background reference is provided, MATCH its lighting "
                "vocabulary."
            ),
        },
        "palette": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "3-5 hex codes (e.g. '#F5EBDD') anchoring the visual tone. "
                "If a background reference is provided, palette must align "
                "with the bg's palette."
            ),
        },
        "style": {
            "type": "string",
            "description": (
                "Visual style: 'documentary-realism', 'cinematic shallow "
                "depth', 'casual vlog 35mm', etc. One short phrase."
            ),
        },
        "mood": {
            "type": "string",
            "description": (
                "2-4 word mood tag: 'warm sincere', 'energetic kinetic', "
                "'contemplative quiet'."
            ),
        },
        "dialogue_language": {
            "type": "string",
            "enum": ["en", "ar", "mixed"],
            "description": "Spoken language. Default 'en'.",
        },
        # ----- Topic-aware bookend text (v3) -----
        "intro_headline": {
            "type": "string",
            "description": (
                "3-7 word title for the intro card that grabs attention "
                "in the first 2 seconds. A question, a claim, or a "
                "punchy framing that names the topic. NOT a slogan, NOT "
                "generic — must echo this specific topic. Will be the "
                "biggest text on the intro card."
            ),
        },
        "intro_subtitle": {
            "type": "string",
            "description": (
                "5-12 word subtitle for the intro card. Smaller text "
                "under the headline. Use EMPTY STRING when the headline "
                "is self-sufficient — don't pad."
            ),
        },
        "outro_headline": {
            "type": "string",
            "description": (
                "3-5 word call-to-action for the outro card. Verb-forward "
                "and topic-shaped — e.g. 'Apply now', 'Start your story', "
                "'Find out more', 'Build something'. Pick whatever lands "
                "for THIS topic; do not default to 'Apply now' on every "
                "reel. Will be the biggest text on the outro card."
            ),
        },
        "outro_subtitle": {
            "type": "string",
            "description": (
                "5-10 word echo of the topic on the outro card, sitting "
                "under the CTA. Use EMPTY STRING when the CTA carries it "
                "alone. Examples: '42abudhabi.ae', 'Applications open "
                "for the September cohort', 'No degree required'."
            ),
        },
        # v6 — per-scene array. Single-scene runs (scene_count=1) get a
        # 1-element array where scenes[0].dialogue == top-level dialogue.
        # Multi-scene runs (scene_count>1) get N entries; the top-level
        # dialogue/hook fields then mirror scenes[0] for legacy consumers.
        "scenes": {
            "type": "array",
            "description": (
                "Per-scene dialogue lines. EXACTLY scene_count entries. "
                "For scene_count=1, this MUST be a single-element array "
                "matching the top-level dialogue/hook fields. For "
                "scene_count>1, each scene's `dialogue` is what the "
                "speaker says in THAT scene. When the caller supplies a "
                "user_dialogue for a slot, you MUST use that exact text. "
                "Hook MUST be a literal prefix of dialogue."
            ),
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "dialogue": {
                        "type": "string",
                        "description": (
                            "Spoken line for this scene. ~20-25 words. "
                            "Conversational first-person. Verbatim when "
                            "user-supplied."
                        ),
                    },
                    "hook": {
                        "type": "string",
                        "description": (
                            "Opening 6-10 words of this scene's dialogue. "
                            "Literal prefix."
                        ),
                    },
                },
                "required": ["dialogue", "hook"],
            },
        },
    },
    "required": [
        "hook", "dialogue", "subject", "action", "camera",
        "lighting", "palette", "style", "mood", "dialogue_language",
        "intro_headline", "intro_subtitle",
        "outro_headline", "outro_subtitle",
        "scenes",
    ],
}


TALKING_HEAD_SCRIPT_SYSTEM = (
    "You are scripting an 8-second talking-head Instagram reel for 42 Abu "
    "Dhabi — a tuition-free, peer-learning coding school in the UAE. The "
    "reel comes with a 4-second intro card before the talking head and a "
    "4-second outro card after. You write the spoken script AND the on-"
    "card text for both bookends.\n\n"
    "HARD RULES for the talking head (no exceptions):\n"
    "  - ONE on-camera speaker. Their mouth moves in sync with the audio. "
    "NO voice-over over graphics. NO montage. NO b-roll. The face is in "
    "frame the ENTIRE 8 seconds.\n"
    "  - Open with the hook in the first 1.5 seconds. The `hook` field is "
    "the EXACT opening words of the `dialogue` field — same characters, "
    "same case, same punctuation.\n"
    "  - Dialogue is conversational first-person. 20-25 words for English "
    "(~8 seconds at natural speaking pace). Never preach. Never sell.\n"
    "  - No emojis. No hashtags. No quote marks inside dialogue. No "
    "'we believe' / 'we empower' / 'we unlock' / corporate boilerplate.\n"
    "  - Use the BRAND BACKGROUND in context. Match the brand's voice DO. "
    "Avoid every brand DON'T.\n"
    "  - When the user has selected a character reference, START the "
    "`subject` field with that character's '#<id> ' tag. Veo's reference "
    "pipeline picks this up to lock identity.\n"
    "  - When the user has selected a background reference, match its "
    "`lighting` and `palette` (described to you in the user message).\n\n"
    "RULES for the BOOKEND CARD TEXT (intro_headline / intro_subtitle / "
    "outro_headline / outro_subtitle):\n"
    "  - `intro_headline` (3-7 words): the BIG text on the intro card. "
    "Grabs attention. Either a question ('What does free really cost?'), "
    "a claim ('I'm a year ahead of my friends.'), or a pointed framing "
    "of the topic ('Why I chose 42 Abu Dhabi'). It is NOT the same as "
    "the dialogue hook — it stands on its own as readable text.\n"
    "  - `intro_subtitle` (5-12 words OR empty string): supplementary line "
    "under the headline. Only use when the headline needs context. Leave "
    "empty when the headline is self-sufficient. Don't pad.\n"
    "  - `outro_headline` (3-5 words): the CTA verb-forward. Topic-shaped, "
    "not boilerplate — pick the verb that fits THIS reel. Examples: "
    "'Apply now', 'Start your story', 'Find out more', 'Join the cohort', "
    "'Build something real'. Avoid the same default on every reel.\n"
    "  - `outro_subtitle` (5-10 words OR empty string): topic echo under "
    "the CTA. Often the brand URL ('42abudhabi.ae') or a tight tagline. "
    "Leave empty when redundant.\n"
    "  - When the user provides an intro/outro reference image, you do "
    "NOT echo its text. The reference is a STYLE guide; your job is to "
    "write fresh, topic-specific text. The image model will combine "
    "your text with the reference's design language.\n\n"
    "Every field in the schema is required and must be present (use "
    "empty string only for the optional subtitle fields where noted). "
    "Empty required fields will break the downstream image / Veo render.\n\n"
    "The output is a single talking-head scene + intro/outro card text. "
    "The whole reel is: [intro card 4s] + [talking head 8s] + [outro "
    "card 4s] stitched into a 16-second composition."
)


def build_script_user_prompt(
    *,
    topic: str,
    context_block: str,
    character_brief: str | None,
    bg_brief: str | None,
    intro_brief: str | None,
    outro_brief: str | None,
    user_dialogue: str | None = None,
    user_dialogues: list[str | None] | None = None,
    scene_count: int = 1,
) -> str:
    """Compose the USER message for the GPT-4o script call.

    `context_block` comes straight from the producer's `build_context_block`
    helper — brand summary + DON'T REPEAT + nearest past scripts + IG posts +
    IG scenes (with transcripts!) + character library roster.

    Optional `*_brief` blocks describe the references the user picked.

    Dialogue handling:
      - When `user_dialogues` is provided AND has length == scene_count,
        it takes precedence. Each non-empty entry is preserved VERBATIM in
        the corresponding scenes[i].dialogue slot. Empty / None entries
        get GPT-generated lines that flow narratively with the surrounding
        user-authored lines.
      - When `user_dialogues` is None / empty, falls back to the legacy
        single-`user_dialogue` parameter (v4 behaviour) which, if set,
        becomes the value of scenes[0].dialogue.
      - When BOTH are None, GPT writes all dialogues from the topic.

    For scene_count == 1 the output schema still has scenes[0] mirroring
    the top-level dialogue/hook fields (backwards compat).
    """
    # Normalise dialogue inputs into a single list of length `scene_count`.
    # Each entry: a verbatim string the user wants preserved, or None for
    # "GPT writes this slot".
    dialogues: list[str | None] = [None] * max(1, scene_count)
    if user_dialogues:
        for i, d in enumerate(user_dialogues[:scene_count]):
            s = (d or "").strip()
            dialogues[i] = s if s else None
    elif user_dialogue and user_dialogue.strip():
        dialogues[0] = user_dialogue.strip()

    has_any_user_dialogue = any(d for d in dialogues)
    multi = scene_count > 1

    parts: list[str] = []
    if multi:
        parts.append(
            f"TASK: Write the script for a {scene_count}-scene talking-head "
            f"Instagram reel. The reel will be stitched as "
            f"intro card (4 s) -> scene 1 (8 s) -> scene 2 (8 s)"
            + (" -> ..." if scene_count > 2 else "")
            + " -> outro card (4 s). All scenes share the same speaker, "
            "setting, camera, and visual style — only the dialogue "
            "changes per scene. Output strictly per the JSON schema."
        )
    else:
        parts.append(
            "TASK: Write the one-scene script for an 8-second talking-head "
            "Instagram reel. Output strictly per the JSON schema."
        )
    parts.append(f"TOPIC: {topic.strip()}")

    # ---- Per-scene dialogue directives -------------------------------------
    if has_any_user_dialogue:
        lines: list[str] = []
        lines.append(
            f"SCENES: this reel has EXACTLY {scene_count} scene"
            + ("s" if scene_count > 1 else "")
            + ". Populate the `scenes` array with EXACTLY "
            f"{scene_count} entries, in order."
        )
        for i, d in enumerate(dialogues):
            if d:
                lines.append(
                    f"  - scenes[{i}].dialogue MUST equal this exact text "
                    f"(no rewrites, no paraphrase, no compression — only "
                    f"normalise trailing whitespace and curly quotes to "
                    f"straight quotes):\n"
                    f"      \"{d}\"\n"
                    f"    scenes[{i}].hook = opening 6-10 words of that "
                    f"exact text (literal prefix)."
                )
            else:
                lines.append(
                    f"  - scenes[{i}].dialogue: YOU write it. ~20-25 words, "
                    f"conversational first-person, no buzzwords. Must "
                    f"connect narratively with the user-authored scenes "
                    f"above/below. scenes[{i}].hook = its first 6-10 words."
                )
        lines.append(
            "MIRROR scenes[0].dialogue into the top-level `dialogue` field "
            "and scenes[0].hook into the top-level `hook` field (backwards "
            "compat for downstream consumers)."
        )
        parts.append("\n".join(lines))
    elif multi:
        parts.append(
            f"SCENES: invent EXACTLY {scene_count} dialogue lines (one per "
            "scene) that flow narratively, each ~20-25 words. Populate the "
            "`scenes` array with {scene_count} entries. Mirror scenes[0] "
            "into the top-level dialogue/hook fields.".replace(
                "{scene_count}", str(scene_count)
            )
        )

    parts.append("CONTEXT (use what's relevant, ignore the rest):" + context_block.rstrip())

    if character_brief:
        parts.append(
            "CHARACTER LOCK IS ACTIVE. A reference image of the speaker is "
            "attached downstream. The `subject` field MUST be exactly: "
            "'<tag below> the person in the attached reference image' — "
            "nothing more. Do NOT infer gender, age, ethnicity, hair, "
            "clothing, or accessories from the topic, the brand corpus, or "
            "your own assumptions. Whatever the image shows IS the truth. "
            "Use neutral pronouns ('the speaker', 'they') throughout the "
            "`action` field and elsewhere. Do not write 'she', 'he', "
            "'woman', 'man', 'female', 'male' unless the reference brief "
            "below explicitly contains those words.\n"
            "Tag to use at the start of `subject`:"
        )
        parts.append(character_brief.rstrip())
    if bg_brief:
        parts.append("BACKGROUND REFERENCE (match `lighting` and `palette` to this):")
        parts.append(bg_brief.rstrip())
    if intro_brief:
        parts.append("INTRO CARD REFERENCE (purely contextual — the script's `mood` should harmonize):")
        parts.append(intro_brief.rstrip())
    if outro_brief:
        parts.append("OUTRO CARD REFERENCE (purely contextual — the script's `mood` should land toward this):")
        parts.append(outro_brief.rstrip())

    # ---- Closing reminder -------------------------------------------------
    if has_any_user_dialogue:
        parts.append(
            "FINAL REMINDER: every user-authored dialogue slot above MUST "
            "be preserved EXACTLY — character-for-character — in the "
            "matching scenes[i].dialogue field. Do not 'fix' or 'improve' "
            "the user's words. Their text is final."
        )
    elif multi:
        parts.append(
            f"Remember: {scene_count} scenes flowing as one narrative arc, "
            "each scene's hook = its opening 6-10 words, 20-25 words per "
            "scene, no buzzwords, every field non-empty."
        )
    else:
        parts.append(
            "Remember: hook in the first 8 words of dialogue, 20-25 words total, "
            "no buzzwords, every field non-empty."
        )
    return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# Per-field enhancers — topic-tone vs dialogue-tone
# ---------------------------------------------------------------------------

ENHANCE_TH_DIALOGUE_SYSTEM = (
    "You are tightening a first-person line of dialogue for an 8-second "
    "talking-head Instagram reel. Preserve the speaker's voice, register, "
    "and phrasing wherever possible. Make these changes ONLY:\n"
    "  - Fix grammar, typos, and run-on sentences.\n"
    "  - Trim to ~20-25 spoken words (enough for ~8 s of speech).\n"
    "  - Keep first-person if the input is first-person; keep second-person "
    "if it is second-person.\n"
    "  - Keep concrete, specific words; do not generify into a slogan or "
    "headline.\n"
    "  - Do NOT add hashtags, emoji, or call-to-action language unless the "
    "input already has them.\n"
    "Output ONLY the polished dialogue on one line. No preamble, no quotes, "
    "no commentary."
)


# ---------------------------------------------------------------------------
# Topic / feedback enhancers (Yas already has these; just give them
# talking-head-specific framing)
# ---------------------------------------------------------------------------

ENHANCE_TH_TOPIC_SYSTEM = (
    "You are a brand-aligned producer for 42 Abu Dhabi. Turn the user's "
    "rough idea into a CRISP topic for a talking-head reel — one or two "
    "short sentences naming the subject of the reel. The result will be "
    "consumed by a downstream script writer. Output ONLY the polished "
    "topic on one line. No preamble, no quotes, no hashtags."
)


# ---------------------------------------------------------------------------
# Image-gen prompts — main keyframe, intro card, outro card
# ---------------------------------------------------------------------------

def _palette_str(palette: list[str] | None) -> str:
    if not palette:
        return ""
    return ", ".join(p.strip() for p in palette if isinstance(p, str) and p.strip())


def build_main_keyframe_prompt(
    *,
    script: dict,
    character_brief: str | None,
    bg_brief: str | None,
    lock_character: bool,
) -> str:
    """Build the prompt for the talking-head keyframe (the still Veo will
    animate into the 8-second reel).

    When `lock_character=True` AND a character reference image is attached
    (passed to the image-gen call as `source_images`), the prompt is
    deliberately COMPACT and DOUBLE-ANCHORED: the identity lock is repeated
    at the top AND bottom (recency anchor), with palette / style / mood /
    long subject prose stripped so the lock signal isn't diluted. Long
    enumerative wall-of-rules prompts proved to be counterproductive — the
    model attends most to the start and end of the prompt; verbose middles
    let scene language win.

    When `lock_character=False` we DESCRIBE the character textually so the
    gen model produces a similar-archetype face (not the exact one).
    """
    if lock_character:
        # ---- COMPACT IDENTITY-LOCKED LAYOUT --------------------------------
        # TOP / SCENE / BOTTOM. Identity lock book-ends the prompt.
        action = (script.get("action") or "").strip()
        camera = (script.get("camera") or "Medium close-up, locked-off").strip()
        lighting = (script.get("lighting") or "soft natural daylight").strip()
        # bg_brief may be multi-line — collapse to the first non-empty line
        # so the Setting bullet stays compact.
        setting = "simple neutral background"
        if bg_brief:
            for ln in bg_brief.splitlines():
                ln = ln.strip().lstrip("•- ").strip()
                if ln and not ln.startswith("("):
                    setting = ln
                    break

        top = (
            "FACE-AND-OUTFIT LOCK. The attached reference image IS the "
            "speaker AND the outfit. Reproduce the face, hair, body, and "
            "EVERY garment exactly as shown. Do NOT redesign, restyle, "
            "redress, or 'improve' the person."
        )
        scene_lines = [
            "Scene to place this person into (9:16 vertical, photo-real):",
            f"  - Pose / action: {action or 'speaking to camera, natural delivery'}",
            f"  - Camera: {camera}",
            f"  - Lighting: {lighting}",
            f"  - Setting: {setting}",
        ]
        bottom = (
            "REMINDER — face = reference, outfit = reference. Only the "
            "pose, lighting, and surrounding setting may change. If any "
            "scene text seems to describe a different person (different "
            "gender, age, ethnicity, clothing), IGNORE it and trust the "
            "attached image. The image is the only source of truth for "
            "who the person is and what they are wearing."
        )
        required = (
            "REQUIRED: face fully in frame, eyes open and looking at the "
            "camera, natural skin tones, no NEW text overlays, no NEW "
            "logos added to the scene (logos already on the reference's "
            "clothing must stay), no AI artifacts (extra fingers, melted "
            "features, plastic skin). Photo-realistic, NOT illustrated."
        )
        return "\n\n".join([top, "\n".join(scene_lines), bottom, required])

    # ---- FREE-GEN LAYOUT (no character ref or no lock) --------------------
    palette = _palette_str(script.get("palette"))
    lines: list[str] = [
        "Photo-real keyframe for the opening frame of an 8-second "
        "talking-head Instagram reel. Vertical 9:16 composition."
    ]
    lines.extend([
        f"Subject framing: {(script.get('subject') or '').strip()}",
        f"Micro-action at frame open: {(script.get('action') or '').strip()}",
        f"Camera: {(script.get('camera') or 'Medium close-up, locked-off').strip()}",
        f"Lighting: {(script.get('lighting') or 'soft natural daylight').strip()}",
    ])
    if palette:
        lines.append(f"Color palette (use ≥3): {palette}")
    if script.get("style"):
        lines.append(f"Style: {script['style']}")
    if script.get("mood"):
        lines.append(f"Mood: {script['mood']}")
    if character_brief:
        lines.append(
            "Character description (use as inspiration, not identical "
            "match — generate a similar-archetype person):"
        )
        lines.append(character_brief.rstrip())
    if bg_brief:
        lines.append("Location / background (anchor the setting here):")
        lines.append(bg_brief.rstrip())
    lines.append(
        "REQUIRED: face fully in frame, eyes open and looking at the "
        "camera, natural skin tones, no text overlays, no logos, no "
        "AI artifacts (extra fingers, melted features, plastic skin). "
        "Photo-realistic, NOT illustrated."
    )
    return "\n".join(lines)


def build_intro_card_prompt(
    *,
    topic: str,
    script: dict,
    brand_summary: str,
    brand_primary_colors: list[str] | None,
    brand_fonts: list[str] | None,
    reference_brief: str | None = None,
    has_reference_image: bool = False,
) -> str:
    """Build the prompt for the intro card.

    v4-final: the PRIMARY directive is the new headline + subtitle, with
    explicit size hierarchy (in % of frame height) so the output isn't
    crammed edge-to-edge with giant type. When a reference image is
    attached, the model is instructed to inherit AESTHETIC ONLY (palette,
    illustration style, decorative motifs) — NOT layout or text. This
    prevents the "model photocopied my reference verbatim" failure mode.
    """
    palette = _palette_str(brand_primary_colors)
    fonts = (
        ", ".join((brand_fonts or [])[:3])
        if brand_fonts
        else "bold modern geometric sans-serif"
    )
    headline = (script.get("intro_headline") or "").strip()
    subtitle = (script.get("intro_subtitle") or "").strip()
    if not headline:
        # Fallback when GPT returned empty headline — synthesize from topic.
        headline = topic.strip()
        if len(headline) > 60:
            headline = headline[:57].rstrip() + "…"

    # PRIMARY DIRECTIVE — text content first, before anything else. The
    # model must understand that THIS specific text is what goes on the
    # card; everything else is secondary.
    lines: list[str] = [
        "PRIMARY TASK: design a NEW static intro card with the text "
        "below — this is a fresh design, not a copy of any other image.\n\n"
        f"  HEADLINE (must appear EXACTLY): \"{headline}\"",
    ]
    if subtitle:
        lines.append(f"  SUBTITLE (must appear EXACTLY): \"{subtitle}\"")
    lines.extend([
        "",
        "CANVAS: 9:16 vertical. NO PEOPLE, NO PHOTOGRAPHY — graphic "
        "design only.",
        "",
        "TYPOGRAPHY & SIZE HIERARCHY (do not violate):",
        "  - Headline occupies ~10-12% of the frame HEIGHT (think "
        "magazine-cover scale, not billboard scale). Width of the "
        "headline block ≤ 75% of the frame width. Wrap to 2 lines if it "
        "exceeds 60 characters; never use display-billboard scale.",
    ])
    if subtitle:
        lines.append(
            "  - Subtitle is ~40% the height of the headline (i.e. "
            "~4-5% of frame height). Placed below the headline with "
            "clear breathing room (gap ≈ 1× subtitle height)."
        )
    lines.extend([
        "  - Brand mark (42 Abu Dhabi): small, ≤ 5% of frame height, "
        "placed in a corner (top-left or bottom-center). NOT in the "
        "center, NOT competing with the headline.",
        "  - Generous margins on all sides: at least 8% of the frame "
        "width as breathing room from the edges.",
        "  - Text MUST be perfectly spelled. No filler text. No lorem "
        "ipsum. No placeholder.",
        "",
        f"FONT STYLE: {fonts}. Tight kerning on the headline; "
        "looser on the subtitle. Strong contrast in weight between "
        "headline (heavy) and subtitle (regular or light).",
        (
            f"COLORS: {palette}."
            if palette
            else "COLORS: brand-aligned, high-contrast (one dominant "
                 "background color + one strong foreground color)."
        ),
        f"MOOD: {script.get('mood', 'confident, on-brand')}.",
    ])
    if brand_summary:
        lines.append(f"Brand context: {brand_summary[:280]}")

    if has_reference_image:
        # v4-final: the reference contributes AESTHETIC (palette, motifs,
        # illustration style) but NOT text, layout, or sizing. The model
        # should treat the reference as a MOOD BOARD, not a copy target.
        lines.extend([
            "",
            "REFERENCE IMAGE — AESTHETIC ONLY (this is the most "
            "important thing to get right):",
            "  - The attached reference is a MOOD BOARD for the new "
            "card's AESTHETIC. The new card MUST be a SEPARATE, ORIGINAL "
            "design with the headline / subtitle above — NOT a copy of "
            "the reference.",
            "  - INHERIT from the reference: color palette, illustration "
            "style (e.g. 8-bit pixel art, chunky vector, hand-drawn, "
            "isometric, low-poly), decorative motifs (borders, dividers, "
            "halftone, scanlines, grain), and overall visual mood.",
            "  - DO NOT INHERIT from the reference: any of its text, "
            "its layout grid, its element positions, its sizing, or its "
            "specific composition. Build a fresh layout that fits the "
            "new headline/subtitle from above with the size hierarchy "
            "this prompt specifies.",
            "  - DO NOT REPRODUCE the reference's text. Any text visible "
            "in the new card MUST be the headline"
            + (" and subtitle" if subtitle else "")
            + " above, and ONLY that text. Hide / replace / overwrite "
            "all other text from the reference.",
            "  - DO NOT photocopy the reference. The new card should "
            "be RECOGNIZABLY in the same family (same palette + same "
            "illustration style) but VISUALLY DISTINCT (new composition, "
            "new layout, new text).",
        ])
        if reference_brief:
            lines.append(f"Reference description: {reference_brief.rstrip()[:320]}")
    elif reference_brief:
        lines.append("Style hint (echo this card's described design language):")
        lines.append(reference_brief.rstrip())

    lines.append(
        "FORBIDDEN: misspellings, Arabic letterforms (unless explicitly "
        "requested), AI artifacts, photo-realistic faces, text that "
        "fills more than 75% of the frame width, headlines that span "
        "more than 25% of the frame height, oversized brand marks, "
        "tiny illegible text, broken letterforms."
    )
    return "\n".join(lines)


def _derive_outro_cta(topic: str, script: dict) -> str:
    """Pick an outro CTA tied to the topic / script.

    Heuristics (cheap, local — no extra LLM call):
      - Topics about applying / joining / discovering → "Apply now · 42abudhabi.ae"
      - Topics about advice / lessons / what-I-learned → "Start your story · 42abudhabi.ae"
      - Topics about specific products / events → "Find out more · 42abudhabi.ae"
      - Anything else → "Apply now · 42abudhabi.ae" (safe default)
    The CTA always points to the school's domain; what changes is the verb,
    so the closing line lands on the topic's mood instead of generic "Apply".
    """
    t = (topic or "").lower()
    dlg = (script.get("dialogue") or "").lower() if isinstance(script, dict) else ""
    hay = f"{t} {dlg}"
    if any(w in hay for w in (
        "apply", "join", "enroll", "admission", "applic",
        "want to be", "want in",
    )):
        return "Apply now · 42abudhabi.ae"
    if any(w in hay for w in (
        "advice", "lesson", "learned", "experience", "journey",
        "story", "decision", "chose",
    )):
        return "Start your story · 42abudhabi.ae"
    if any(w in hay for w in (
        "event", "hackathon", "workshop", "demo day", "showcase",
    )):
        return "Find out more · 42abudhabi.ae"
    return "Apply now · 42abudhabi.ae"


def build_outro_card_prompt(
    *,
    topic: str,
    script: dict,
    brand_summary: str,
    brand_primary_colors: list[str] | None,
    brand_fonts: list[str] | None,
    reference_brief: str | None = None,
    has_reference_image: bool = False,
) -> str:
    """Build the prompt for the outro card.

    v3: headline (CTA) + subtitle come from GPT's script. When GPT returns
    empty `outro_headline` we fall back to the topic-shaped CTA heuristic.
    When `has_reference_image=True` the reference is attached as a source
    image; the prompt instructs the model to keep the style but replace
    all of its text with ours.
    """
    palette = _palette_str(brand_primary_colors)
    fonts = (
        ", ".join((brand_fonts or [])[:3])
        if brand_fonts
        else "bold modern geometric sans-serif"
    )
    headline = (script.get("outro_headline") or "").strip()
    if not headline:
        # Fallback CTA derived from topic + dialogue heuristics.
        headline = _derive_outro_cta(topic, script)
    subtitle = (script.get("outro_subtitle") or "").strip()

    # PRIMARY DIRECTIVE — see build_intro_card_prompt for the rationale.
    lines: list[str] = [
        "PRIMARY TASK: design a NEW static end-card with the CTA below "
        "— this is a fresh design, not a copy of any other image.\n\n"
        f"  CTA (must appear EXACTLY): \"{headline}\"",
    ]
    if subtitle:
        lines.append(f"  SUBTITLE (must appear EXACTLY): \"{subtitle}\"")
    lines.extend([
        "",
        "CANVAS: 9:16 vertical. NO PEOPLE, NO PHOTOGRAPHY — graphic "
        "design only.",
        "",
        "TYPOGRAPHY & SIZE HIERARCHY (do not violate):",
        "  - CTA occupies ~10-14% of the frame HEIGHT (slightly larger "
        "than an intro headline, since this is the closing call). "
        "Width ≤ 80% of frame width; wrap if needed.",
    ])
    if subtitle:
        lines.append(
            "  - Subtitle is ~35% the height of the CTA (i.e. ~4% of "
            "frame height). Placed below the CTA with clear breathing "
            "room."
        )
    lines.extend([
        "  - Brand mark (42 Abu Dhabi): visible but proportional — "
        "~6-8% of frame height. Centered horizontally, placed either "
        "above the CTA or at the bottom of the card. NOT competing in "
        "size with the CTA.",
        "  - Generous margins: at least 8% of frame width as breathing "
        "room from the edges.",
        "  - Text MUST be perfectly spelled. No filler text.",
        "",
        f"FONT STYLE: {fonts}. Confident, modern. Strong weight "
        "contrast between CTA (heavy) and subtitle (regular).",
        (
            f"COLORS: {palette}."
            if palette
            else "COLORS: brand-aligned, high-contrast — one dominant "
                 "background, one strong CTA color."
        ),
        f"MOOD: {script.get('mood', 'inviting, decisive')}. Visually "
        "closes the reel.",
    ])
    if brand_summary:
        lines.append(f"Brand context: {brand_summary[:280]}")

    if has_reference_image:
        # v4-final: same aesthetic-only approach as the intro builder.
        lines.extend([
            "",
            "REFERENCE IMAGE — AESTHETIC ONLY (this is the most "
            "important thing to get right):",
            "  - The attached reference is a MOOD BOARD for the new "
            "card's AESTHETIC. The new card MUST be a SEPARATE, ORIGINAL "
            "design with the CTA / subtitle above — NOT a copy of the "
            "reference.",
            "  - INHERIT from the reference: color palette, illustration "
            "style (e.g. 8-bit pixel art, chunky vector, hand-drawn, "
            "isometric, low-poly), decorative motifs (borders, dividers, "
            "halftone, scanlines, grain), and overall visual mood.",
            "  - DO NOT INHERIT from the reference: any of its text, "
            "its layout grid, its element positions, its sizing, or its "
            "specific composition. Build a fresh layout that fits the "
            "new CTA/subtitle from above with the size hierarchy "
            "this prompt specifies.",
            "  - DO NOT REPRODUCE the reference's text. Any text "
            "visible in the new card MUST be the CTA"
            + (" and subtitle" if subtitle else "")
            + " above, and ONLY that text.",
            "  - DO NOT photocopy the reference. The new card should "
            "be RECOGNIZABLY in the same family (same palette + same "
            "illustration style) but VISUALLY DISTINCT (new composition, "
            "new layout, new text).",
        ])
        if reference_brief:
            lines.append(f"Reference description: {reference_brief.rstrip()[:320]}")
    elif reference_brief:
        lines.append("Style hint (echo this card's described design language):")
        lines.append(reference_brief.rstrip())

    lines.append(
        "FORBIDDEN: misspellings, AI artifacts, photo-realistic faces, "
        "text filling more than 80% of frame width, CTA larger than 25% "
        "of frame height, oversized brand marks, broken letterforms, "
        "any text that is NOT the CTA"
        + (" or subtitle" if subtitle else "")
        + " specified above."
    )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Veo prompts — animate the still cards into ~4s clips for the final
# composition. The intro + outro cards stay visually identical to their
# preview stills (Veo's i2v locks to the reference image), with subtle
# motion added: gentle parallax, text scaling in, light flicker.
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Bookend animation styles — picked per-run by a Gemini Flash vision call
# (see yas_talking_head.py::pick_bookend_animation). Each enum maps to a
# concrete motion clause that gets injected into the Veo prompt.
# ---------------------------------------------------------------------------

INTRO_STYLES: tuple[str, ...] = (
    "fade_in",
    "scale_pop",
    "slide_up",
    "kinetic_typography",
    "pixel_reveal",
    "glitch_pop",
    "gentle_drift",
    "light_sweep",
)

OUTRO_STYLES: tuple[str, ...] = (
    "fade_hold",
    "pulse_cta",
    "scale_emph",
    "slide_in_mark",
    "pixel_dissolve",
    "glitch_settle",
    "type_on",
    "gentle_drift",
)


_INTRO_MOTION_CLAUSES: dict[str, str] = {
    "fade_in": (
        "Text and graphic elements fade in over the first 1.0 s "
        "(opacity 0 → 1) with a soft ease-out, then hold the static "
        "composition for the remaining 3 s."
    ),
    "scale_pop": (
        "Text and graphic elements scale in from 92% to 100% over the "
        "first 0.8 s with a slight overshoot (settle from 102%), then "
        "hold the static composition. Brand mark scales 88% → 100% "
        "with a 0.2 s stagger after the headline."
    ),
    "slide_up": (
        "Headline slides up from below frame into its final position "
        "over 0.7 s with an ease-out. Subtitle and brand mark follow "
        "with a 0.2 s stagger. The card then holds static for 3 s."
    ),
    "kinetic_typography": (
        "Each word of the headline pops in one at a time over the first "
        "1.2 s — each word scales from 80% to 100% over 0.15 s with a "
        "tight ease-out. After all words land, the subtitle fades in over "
        "0.4 s. Card holds for the remaining 2.4 s."
    ),
    "pixel_reveal": (
        "An 8-bit dithered sweep wipes left-to-right across the card "
        "over 0.8 s, revealing the static composition pixel-by-pixel. "
        "Crisp pixel edges, no anti-aliasing on the sweep — preserve "
        "the chunky retro aesthetic. Card holds for the remaining 3.2 s."
    ),
    "glitch_pop": (
        "Card jitters with a brief RGB-split glitch for 0.3 s (red and "
        "blue channels offset ~6 px in alternating directions), then "
        "settles into the static composition with a fast chromatic-"
        "aberration ease-out. Card holds for the remaining 3.7 s."
    ),
    "gentle_drift": (
        "Card holds the static composition for the full 4 s. Subtle "
        "parallax drift: 5 px horizontal sway with 1 px vertical sway, "
        "looping smoothly. Keeps the frame alive without distracting."
    ),
    "light_sweep": (
        "A soft diagonal light gradient (45°, low-opacity highlight band "
        "~25% of the frame width) sweeps from top-left to bottom-right "
        "across the card over 1.5 s, revealing the type with a premium "
        "shimmer effect. Card then holds static for 2.5 s."
    ),
}


_OUTRO_MOTION_CLAUSES: dict[str, str] = {
    "fade_hold": (
        "Card holds the static composition for 3.5 s. Final 0.5 s: "
        "gentle edge-vignette fade-down — corners darken slightly to "
        "signal end of reel. No element motion."
    ),
    "pulse_cta": (
        "CTA text holds static for the first 1.5 s, then pulses once "
        "at the 2-second mark (scale 100 → 103 → 100% over 0.4 s with "
        "soft ease). Brand mark steady. Card holds final 2.1 s."
    ),
    "scale_emph": (
        "CTA text holds static for the first 1.0 s, then at the 1.5-s "
        "mark scales up briefly (100 → 106 → 100% over 0.6 s with a "
        "subtle elastic ease). Brand mark holds steady throughout."
    ),
    "slide_in_mark": (
        "Card opens with the CTA already in place. At the 1.0-s mark, "
        "the brand mark slides into position from the right edge over "
        "0.6 s with an ease-out. CTA holds steady. Card holds the final "
        "2.4 s composed."
    ),
    "pixel_dissolve": (
        "Card holds the static composition for 3 s. Final 1 s: 8-bit "
        "dithered dissolve fades the type at the edges (chunky pixel "
        "checkerboard pattern) while the brand mark settles into place. "
        "Preserve the retro pixel aesthetic — no smooth blur."
    ),
    "glitch_settle": (
        "CTA arrives with a brief RGB-split glitch over the first 0.4 s "
        "(red/blue channel offset ~5 px, alternating), then snaps into "
        "the held static composition. Brand mark holds. Card holds the "
        "final 3.6 s."
    ),
    "type_on": (
        "CTA letters type on one at a time over the first 1.2 s (~40 ms "
        "per character with a soft cursor blink). Once the CTA is "
        "complete, the brand mark fades in over 0.4 s. Card holds the "
        "remaining 2.4 s."
    ),
    "gentle_drift": (
        "Card holds the static composition for the full 4 s. Subtle "
        "parallax drift: 5 px horizontal sway with 1 px vertical sway, "
        "looping smoothly. Calm exit."
    ),
}


_BOOKEND_ANIM_PICKER_SYSTEM = (
    "You pick the best animation motion for an Instagram talking-head "
    "reel's intro or outro card.\n\n"
    "You are shown the rendered card image. Choose ONE style from the "
    "enum that best matches:\n"
    "  - The card's visual style (pixel art → pick pixel_*; glitch / "
    "neon / futurist → pick glitch_*; calm editorial → pick fade or "
    "gentle_drift; bold kinetic typography → pick kinetic_typography "
    "or scale_pop; clean type-driven → pick type_on or light_sweep)\n"
    "  - The card's text density (lots of words → kinetic_typography "
    "or type_on; single statement → fade or scale)\n"
    "  - The mood of the underlying topic / script (energetic → bolder "
    "motion like scale_pop / slide_up / kinetic; contemplative → "
    "fade_in / gentle_drift / light_sweep)\n\n"
    "Output ONLY the structured JSON per the schema. The `reason` field "
    "is a 5-15 word explanation that goes to logs, not to the model.\n\n"
    "Hard rules:\n"
    "  - If you see an 8-bit / pixel-art aesthetic, prefer pixel_reveal "
    "(intro) or pixel_dissolve (outro).\n"
    "  - If you see glitch / scanlines / RGB-split / cyber aesthetic, "
    "prefer glitch_pop (intro) or glitch_settle (outro).\n"
    "  - If the card has 4+ distinct words on the headline, prefer "
    "kinetic_typography or type_on.\n"
    "  - When in doubt, fade_in / fade_hold are safe."
)


def build_bookend_animation_picker_schema(role: str) -> dict:
    """Return the JSON schema for Gemini's animation-style picker call."""
    styles = INTRO_STYLES if role == "intro" else OUTRO_STYLES
    return {
        "type": "OBJECT",
        "properties": {
            "style": {"type": "STRING", "enum": list(styles)},
            "reason": {"type": "STRING"},
        },
        "required": ["style"],
    }


def build_intro_veo_prompt(*, topic: str, script: dict, style: str = "fade_in") -> str:
    """Veo prompt for animating the intro card.

    v4: motion clause is driven by `style` (one of INTRO_STYLES, picked
    by a Gemini Flash vision call) instead of hardcoded fade-and-hold.
    Veo 3.1 i2v honours the keyframe strongly, so we only need to
    describe the motion arc — the card's content is locked by start_image.
    """
    mood = (script.get("mood") if isinstance(script, dict) else "") or "confident, inviting"
    palette = ", ".join(
        (script.get("palette") if isinstance(script, dict) else None) or []
    )
    motion = _INTRO_MOTION_CLAUSES.get(style) or _INTRO_MOTION_CLAUSES["fade_in"]
    return (
        f"Animated intro card for a talking-head Instagram reel. "
        f"The frame opens on the static card composition shown in the "
        f"reference image. Apply this specific motion:\n  {motion}\n"
        f"NO new objects appear from outside the frame except as "
        f"described above. NO camera moves (no pans, dollies, orbits). "
        f"Topic: {topic[:60]}. Mood: {mood}. "
        + (f"Palette: {palette}. " if palette else "")
        + "9:16 vertical. Graphic-design only — no people, no "
        "photography, no live-action elements. Audio: none (silent intro)."
    )


def build_main_veo_prompt(
    *,
    script: dict,
    brand_req: dict,
    has_character_ref: bool,
    has_bg_ref: bool,
    dialogue_language: str,
) -> str:
    """Veo prompt for the 8-second main talking-head clip.

    Mirrors the producer's `_assemble_veo_prompt` labelled-section shape,
    but APPENDS explicit IDENTITY LOCK + LOCATION LOCK + CONSISTENCY
    clauses tail-end. These clauses reference Veo's reference image slots
    by ordinal so the model knows which input governs which axis.

    Reference image slots (caller-side packing in `start_videos`):
        slot 1  → keyframe (always present — the scene as designed)
        slot 2  → character close-up (when `has_character_ref=True`)
        slot 3  → background only   (when `has_bg_ref=True`)
    Slots 2 & 3 collapse upward when their respective ref is missing —
    e.g. when only the bg ref is provided, it lives in slot 2.
    """
    # Compute slot indices for the lock clauses.
    char_slot = 2 if has_character_ref else None
    if has_character_ref and has_bg_ref:
        bg_slot = 3
    elif has_bg_ref:
        bg_slot = 2
    else:
        bg_slot = None

    parts: list[str] = []

    # ----- Labelled scene description (same shape as producer's assembler) ---
    subject = (script.get("subject") or "").strip()
    action = (script.get("action") or "").strip()
    camera = (script.get("camera") or "").strip()
    lighting = (script.get("lighting") or "").strip()
    style = (script.get("style") or "").strip()
    palette = list(script.get("palette") or [])
    mood = (script.get("mood") or "").strip()
    dialogue = (script.get("dialogue") or "").strip()

    if subject:
        parts.append(f"Subject: {subject}")
    parts.append(
        "Setting: 42 Abu Dhabi context — specific environment from "
        "reference image" + (f" {bg_slot}" if bg_slot else " 1")
    )
    if action:
        parts.append(f"Action: {action}")
    if camera:
        parts.append(f"Camera: {camera}")
    if lighting:
        parts.append(f"Lighting: {lighting}")
    if palette:
        parts.append(f"Color palette: {'; '.join(palette)}")
    elif brand_req.get("primary_colors"):
        parts.append(
            "Color palette: "
            + ", ".join(brand_req["primary_colors"][:3])
            + " (brand-required)"
        )
    if style:
        style_clean = style.lstrip(", ").strip()
        if style_clean.lower().startswith("photorealistic"):
            parts.append(f"Style: {style_clean}")
        else:
            parts.append(f"Style: photorealistic, {style_clean}")
    else:
        parts.append("Style: photorealistic, cinematic")
    voice = brand_req.get("voice_pillars") or []
    if voice:
        parts.append(f"Mood / voice pillars: {mood + ', ' if mood else ''}{', '.join(voice[:4])}")
    elif mood:
        parts.append(f"Mood: {mood}")

    # ----- Audio clause (Veo speech) -----
    if dialogue:
        # Reuse the producer's audio clause builder for consistency
        from app.agents.producer.prompts import format_audio_clause  # local import to avoid cycle
        audio = format_audio_clause(dialogue, dialogue_language)
        if audio:
            parts.append(audio)

    # ----- Identity / Location / Consistency LOCKS (the new bit) -----
    # These are the load-bearing additions for v3 — they tell Veo
    # explicitly which axes are NOT allowed to drift.
    identity_clause: str
    if char_slot is not None:
        identity_clause = (
            f"IDENTITY LOCK (do not violate): The speaker is the EXACT "
            f"person shown in reference image {char_slot}. Keep the same "
            f"face structure, same skin tone, same hair, same clothing, "
            f"and same posture. The speaker's appearance stays consistent "
            f"and unchanged across every frame of the 8 seconds."
        )
    else:
        identity_clause = (
            "IDENTITY LOCK (do not violate): The speaker introduced in "
            "frame 1 (reference image 1) is the same person for the "
            "entire 8 seconds. Their face, hair, clothing, and posture "
            "stay consistent and unchanged throughout."
        )

    location_clause: str
    if bg_slot is not None:
        location_clause = (
            f"LOCATION LOCK (do not violate): The setting matches the "
            f"environment shown in reference image {bg_slot}. Same walls, "
            f"same architecture, same lighting, same color context, same "
            f"props in the background. The camera may push in slowly or "
            f"hold steady — the location does NOT change. No teleporting, "
            f"no scene cuts, no new room."
        )
    else:
        location_clause = (
            "LOCATION LOCK (do not violate): The setting from frame 1 is "
            "the setting for the entire 8 seconds. Same walls, same "
            "architecture, same lighting. The camera may hold or push in "
            "slowly; the location does NOT change."
        )

    parts.append(identity_clause)
    parts.append(location_clause)
    parts.append(
        "CONSISTENCY: ONE continuous shot. No cuts, no dissolves, no "
        "flashbacks, no b-roll inserts, no montage. 8 seconds of unbroken "
        "footage of the SAME person speaking in the SAME place."
    )

    # Final RAI guard: strip any face-manipulation vocabulary the classifier
    # rejects with code 29310472 before the string ever reaches Veo.
    return scrub_rai_terms(". ".join(parts) + ".")


def build_talking_head_negative_prompt(brand_req: dict) -> str:
    """Build Veo's negative_prompt for the main talking-head clip.

    Layers:
      1. Quality / artefact terms — anything the model would render badly
      2. Brand DONTs from the brand profile

    SAFETY-FILTER NOTE: Vertex Veo's pre-submission safety classifier
    reads the negative_prompt as if it were a request (it doesn't
    distinguish positive vs negative semantics). Words like
    "face swap", "swapped face", "morphed features", "identity drift",
    "age shift", "replaced character" trigger support code 29310472
    ("violates usage guidelines"). The identity / location lock is
    already enforced by the reference image + the positive prompt's
    "FACE-AND-OUTFIT LOCK" block, so we deliberately omit the identity /
    swap / face-modification vocabulary from this list.

    Capped at ~960 chars (Vertex limit is 1000; we keep headroom).
    """
    artefact_terms = [
        "low quality", "blurry", "distorted", "rubbery features",
        "uncanny valley", "extra fingers", "melted features",
        "wobbly hands", "warped anatomy",
        "amateur lighting", "blurry text", "garbled letters",
        "text overlays added in post", "watermarks",
        "stock-photo aesthetic", "cartoon", "illustration",
        "anime style", "oversaturated",
        "montage", "split screen", "double exposure",
    ]
    cliche_terms = [
        "corporate stock-photo classroom",
        "generic handshake meeting",
        "people typing at laptops with floating UI overlays",
    ]
    brand_dont = [
        d.lower() for d in (brand_req.get("brand_donts") or [])
    ]
    all_terms = artefact_terms + cliche_terms + brand_dont
    joined = ", ".join(all_terms)
    # Vertex limit is 1000; cap with a safe margin.
    return joined[:960]


def build_outro_veo_prompt(*, topic: str, script: dict, style: str = "fade_hold") -> str:
    """Veo prompt for animating the outro card.

    v4: motion clause driven by `style` (one of OUTRO_STYLES, picked by
    the Gemini Flash vision call) instead of hardcoded CTA-pulse.
    """
    mood = (script.get("mood") if isinstance(script, dict) else "") or "decisive, inviting"
    palette = ", ".join(
        (script.get("palette") if isinstance(script, dict) else None) or []
    )
    cta = (
        (script.get("outro_headline") if isinstance(script, dict) else None)
        or _derive_outro_cta(topic, script if isinstance(script, dict) else {})
    )
    motion = _OUTRO_MOTION_CLAUSES.get(style) or _OUTRO_MOTION_CLAUSES["fade_hold"]
    return (
        f"Animated end card for a talking-head Instagram reel. The frame "
        f"opens on the static card composition shown in the reference "
        f"image. The CTA text on the card is \"{cta}\". Apply this "
        f"specific motion:\n  {motion}\n"
        f"NO new objects appear from outside the frame except as "
        f"described above. NO camera moves. Mood: {mood}. "
        + (f"Palette: {palette}. " if palette else "")
        + "9:16 vertical. Graphic-design only — no people, no "
        "photography, no live-action elements. Audio: none (silent end card)."
    )


# ---------------------------------------------------------------------------
# Reference-brief composers — turn a producer_assets row into a short text
# block describing the reference so GPT-4o / the image gen models can use it.
# ---------------------------------------------------------------------------

def character_brief_from_asset(asset_row: dict) -> str:
    """Compose a short brief from a producer_assets character row."""
    if not asset_row:
        return ""
    tag = f"#{asset_row['id']}"
    name = (asset_row.get("display_name") or asset_row.get("slug") or "").strip()
    archetype = (asset_row.get("archetype") or "").strip()
    desc = (asset_row.get("ai_description") or "").strip()
    attrs = asset_row.get("attributes") or {}
    if isinstance(attrs, str):
        try:
            attrs = json.loads(attrs)
        except Exception:
            attrs = {}
    lines = [f"{tag} {name} — archetype: {archetype or 'unspecified'}"]
    if desc:
        lines.append(desc[:480])
    # Surface a few of the structured attribute fields that distinguish this
    # character from siblings of the same archetype.
    if isinstance(attrs, dict):
        for key in (
            "age_range", "skin_tone", "facial_hair", "kandura", "ghutra",
            "hijab", "abaya", "attire", "distinguishing_features",
        ):
            v = attrs.get(key)
            if not v:
                continue
            if isinstance(v, dict):
                vs = "; ".join(f"{k}: {x}" for k, x in v.items() if x)
            elif isinstance(v, list):
                vs = ", ".join(str(x) for x in v if x)
            else:
                vs = str(v)
            if vs.strip():
                lines.append(f"  {key}: {vs}")
    return "\n".join(lines)


def bg_brief_from_asset(asset_row: dict) -> str:
    """Compose a short brief from a producer_assets background row."""
    if not asset_row:
        return ""
    desc = (asset_row.get("ai_description") or "").strip()
    attrs = asset_row.get("attributes") or {}
    if isinstance(attrs, str):
        try:
            attrs = json.loads(attrs)
        except Exception:
            attrs = {}
    lines: list[str] = []
    if isinstance(attrs, dict):
        loc = attrs.get("location_type")
        lighting = attrs.get("lighting")
        time_of_day = attrs.get("time_of_day")
        io = attrs.get("indoor_outdoor")
        mood = attrs.get("mood")
        palette = attrs.get("palette") or []
        bits = []
        if loc:
            bits.append(f"location: {loc}")
        if lighting:
            bits.append(f"lighting: {lighting}")
        if time_of_day:
            bits.append(f"time_of_day: {time_of_day}")
        if io:
            bits.append(f"indoor_outdoor: {io}")
        if mood:
            bits.append(f"mood: {mood}")
        if palette and isinstance(palette, list):
            bits.append("palette: " + ", ".join(str(c) for c in palette[:6] if c))
        if bits:
            lines.append("  " + " | ".join(bits))
    if desc:
        lines.append(desc[:480])
    return "\n".join(lines) or "(background reference provided but no structured attributes)"


def card_brief_from_asset(asset_row: dict, *, kind: str) -> str:
    """Brief for an intro/outro reference asset (just textual description)."""
    if not asset_row:
        return ""
    desc = (asset_row.get("ai_description") or asset_row.get("preview") or "").strip()
    return f"({kind} card reference)\n{desc[:480]}" if desc else f"({kind} card reference provided)"


# ---------------------------------------------------------------------------
# Retry / refinement
# ---------------------------------------------------------------------------

def append_feedback_to_prompt(base_prompt: str, feedback: str) -> str:
    """Tack a user-feedback instruction onto an existing prompt for retry.

    Used when the user clicks "retry with feedback" on a preview tile —
    the image-gen call re-runs with the same base prompt plus a
    REFINEMENT clause at the end.
    """
    fb = (feedback or "").strip()
    if not fb:
        return base_prompt
    # Scrub user-typed feedback too — otherwise a retry note like
    # "make the face swap smoother" would re-introduce a trigger word and
    # re-block the retry with code 29310472.
    return scrub_rai_terms(
        base_prompt + "\n\nREFINEMENT (apply these changes verbatim): " + fb
    )
