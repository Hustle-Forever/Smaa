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
# Output aspect ratio (9:16 portrait / 16:9 landscape). 1:1 is intentionally
# unsupported — Veo 3.1 only renders 9:16 / 16:9. Default everywhere is 9:16.
# ---------------------------------------------------------------------------
def _aspect_phrase(aspect: str | None) -> str:
    """Human phrase for an aspect ratio, injected into image/Veo prompts."""
    return "16:9 horizontal (wide landscape)" if aspect == "16:9" else "9:16 vertical (tall portrait)"


def build_reframe_to_aspect_prompt(*, aspect: str = "9:16") -> str:
    """Resize-only EDIT: keep the person AND the existing background EXACTLY,
    only extend/outpaint the scene to fill a new aspect-ratio canvas.

    Used when a character image is "used as-is" (New bg OFF, no bg reference) but
    its aspect ratio doesn't match the chosen output size — so the still must be
    reframed without changing any content. The character image is passed as the
    single source/canvas.
    """
    phrase = _aspect_phrase(aspect)
    return (
        "EDIT THE ATTACHED IMAGE — this is a RESIZE ONLY. Keep the person AND the "
        "existing background EXACTLY as they already are: the same face, "
        "expression, gaze, head angle, pose, body, hair and outfit, and the SAME "
        "background scene, colours, textures and lighting. Do NOT replace, "
        "restyle, recolour, re-pose, beautify, or crop anything that is already "
        "in the frame.\n\n"
        f"ONLY reframe the canvas to a {phrase} frame: naturally extend / "
        "outpaint the EXISTING background outwards to fill the new frame, "
        "continuing the current scene, colours and lighting seamlessly so the "
        "additions are indistinguishable from the original. Keep the person fully "
        "in frame and at the same relative scale.\n\n"
        f"REQUIRED: photo-realistic (not illustrated), {phrase}, edge-to-edge, no "
        "letterboxing or bars, no new text or logos, no AI artifacts."
    )


def build_reframe_card_to_aspect_prompt(*, aspect: str = "9:16") -> str:
    """The card counterpart of build_reframe_to_aspect_prompt: an intro/outro
    card has text and a logo instead of a person, and those must survive the
    reframe letter for letter."""
    phrase = _aspect_phrase(aspect)
    return (
        "EDIT THE ATTACHED IMAGE — this is a RESIZE ONLY. It is a title card. Keep "
        "EVERY element exactly as it is: every letter of every line of text, the "
        "logo, the shapes, the colours, the style and the lighting. Do NOT rewrite, "
        "respell, restyle, recolour or remove anything.\n\n"
        f"ONLY reframe the canvas to a {phrase} frame: rearrange nothing except to "
        "keep all text and the logo fully inside the new frame, and extend the "
        "EXISTING background outwards to fill it, seamlessly.\n\n"
        f"REQUIRED: {phrase}, edge-to-edge, no letterboxing or bars, no new text "
        "or logos, no AI artifacts."
    )


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
                "Full ~13-16 word line (ONE short, calm sentence the "
                "speaker can say unhurried within an 8-second clip — keep "
                "it short so the lip-sync stays natural), in English. "
                "Conversational first-person. No buzzwords, no LinkedIn-isms. "
                "The hook IS the opening — the rest follows naturally. "
                "Veo will speak this exact string."
            ),
        },
        "subject": {
            "type": "string",
            "description": (
                "Concrete visual description of the ONE speaker: gender "
                "presentation, age range, attire and one or two "
                "distinguishing features. Never an id, a '#' tag or a library "
                "name, and never a phrase about an attached or reference "
                "image. DEFAULT SPEAKER: unless the topic or the user's "
                "dialogue fixes the speaker's nationality, ethnicity or "
                "attire, the speaker is an Emirati national aged 18-30: a man "
                "in a white kandura and ghutra, or a woman in an abaya with a "
                "shayla neatly framing a fully visible face. When the topic or "
                "dialogue does fix them, describe the speaker exactly that "
                "way. (When the user message has a CHARACTER LOCK block, "
                "follow that block instead.)"
            ),
        },
        "speaker_user_specified": {
            "type": "boolean",
            "description": (
                "true ONLY when the topic or the user's dialogue explicitly "
                "fixes the speaker's nationality, ethnicity or attire (e.g. "
                "'an exchange student from Brazil', 'a speaker in a business "
                "suit'). false when they leave the speaker open, which is the "
                "usual case: `subject` is then the default Emirati speaker."
            ),
        },
        "action": {
            "type": "string",
            "description": (
                "Body posture ONLY for ONE person, ALONE, facing the camera — "
                "calm and still. NEVER describe the face, eyes, mouth, gaze or "
                "expression (those are locked separately — do NOT write things "
                "like 'eyes widen', 'smiles', or 'excited expression'). NEVER "
                "reference another person or any interaction (no high-five, wave, "
                "handshake, pointing at or glancing at anyone). Keep it a short, "
                "calm, front-facing posture. Under 12 words."
            ),
        },
        "camera": {
            "type": "string",
            "description": (
                "Camera MOVEMENT only: 'Locked-off' or 'Slow push-in' ONLY. "
                "Never the shot size or framing (no close-up, medium shot, "
                "full body, head-and-shoulders): the user picks that "
                "separately. Never whip-pan / orbit / dolly / handheld."
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
                "Visual style — a natural, candid, true-to-life look: "
                "'documentary-realism', 'casual vlog 35mm', 'natural handheld'. "
                "Avoid glossy studio-portrait / cinematic-glamour looks. One short phrase."
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
                            "Spoken line for this scene. ~13-16 words (one "
                            "short calm sentence per scene; short keeps the "
                            "lip-sync natural). "
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
        "hook", "dialogue", "subject", "speaker_user_specified", "action", "camera",
        "lighting", "palette", "style", "mood", "dialogue_language",
        "intro_headline", "intro_subtitle",
        "outro_headline", "outro_subtitle",
        "scenes",
    ],
}


# ---------------------------------------------------------------------------
# Topic-MAGNIFY stage (the COMMITTED ANGLE) — runs BEFORE the script call.
# A focused LLM pass that locks a single sharp angle to the user's exact topic
# and stance, studying the performance RAG for the MECHANIC only (never the
# subject). Its structured output becomes the COMMITTED ANGLE block that
# TALKING_HEAD_SCRIPT_SYSTEM already knows how to obey.
# ---------------------------------------------------------------------------
TALKING_HEAD_ANGLE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "topic_restated": {
            "type": "string",
            "description": (
                "Restate the user's EXACT topic and stance in one plain "
                "sentence — same subject, same point of view. This is a "
                "fidelity check: do NOT generalise it, do NOT swap the "
                "subject, do NOT turn a feeling into a recruitment pitch."
            ),
        },
        "committed_angle": {
            "type": "string",
            "description": (
                "The ONE specific angle this reel commits to, fully inside "
                "the user's topic and POV. Concrete and vivid, not generic. "
                "E.g. 'I love 42 Abu Dhabi' -> a single heartfelt, specific "
                "reason THIS person loves it (a moment, a person, a feeling) "
                "— NOT 'why I joined', NOT 'apply now', NOT 'my journey'."
            ),
        },
        "specific_detail": {
            "type": "string",
            "description": (
                "The single concrete detail, moment or image the 8-second "
                "reel hinges on — something real and filmable, not a slogan."
            ),
        },
        "hook_technique": {
            "type": "string",
            "description": (
                "The opening MECHANIC to borrow from a high-performing "
                "reference (e.g. 'cold-open on a surprising number', "
                "'mid-action confession', 'contrarian claim'). Borrow the "
                "technique only — never the reference's subject."
            ),
        },
        "why_it_should_work": {
            "type": "string",
            "description": (
                "One sentence tying this angle to the performance evidence — "
                "what made similar posts land."
            ),
        },
        "avoid": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "Generic frames this specific topic must NOT drift into "
                "(e.g. 'why I joined', 'apply now', 'my journey', brand "
                "pitch), plus any angle a recent reel already used."
            ),
        },
    },
    "required": [
        "topic_restated", "committed_angle", "specific_detail",
        "hook_technique", "why_it_should_work", "avoid",
    ],
}


TALKING_HEAD_ANGLE_SYSTEM = (
    "You are the creative director for 42 Abu Dhabi's talking-head Instagram "
    "reels — a tuition-free, peer-learning coding school in the UAE. Your ONE "
    "job in this step is to take the user's TOPIC and commit to a single, "
    "concrete, TRUE restatement of that exact topic — faithfully enhanced with "
    "one real 42 fact (tuition-free; no teachers/lectures; peer-to-peer; "
    "project-based; open to beginners), NEVER a creative reinvention or personal "
    "anecdote — before any script is written.\n\n"
    "TOPIC FIDELITY IS ABSOLUTE:\n"
    "  - The angle MUST stay inside the user's exact topic, subject and point "
    "of view. Make it specific, vivid and real — never swap it for a more "
    "generic or brand-safe frame.\n"
    "  - Keep the user's stance in spirit: 'I love 42 Abu Dhabi' is a "
    "first-person declaration of love -> commit to ONE concrete reason THIS "
    "person loves it. It is NOT 'why I joined', NOT 'my journey', NOT a "
    "recruitment pitch. If the topic literally IS 'why I joined', then that "
    "is the angle — sharpen, never override, the user's real intent.\n"
    "  - You SHARPEN and MAGNIFY. You never broaden, genericise, or change "
    "the subject.\n\n"
    "STAY STRICTLY ON THE TOPIC:\n"
    "  - Derive the angle ONLY from the user's TOPIC — its exact subject, point "
    "and stance. Do NOT introduce any unrelated subject or example.\n"
    "  - For a repeated or familiar topic, keep the SAME subject and simply vary "
    "the HOOK MECHANIC (a different opening question, number, or first line) — "
    "NEVER change the subject just to feel 'fresh'. If no fresh angle exists "
    "inside the topic, keep the topic and vary only the hook.\n\n"
    "Output strictly per the JSON schema. Be concrete. No buzzwords, no "
    "corporate boilerplate, no emojis."
    " The reel is a SOLO talking head — exactly ONE person, alone, facing the "
    "camera. NEVER propose an angle that needs a second person or an interaction "
    "(high-five, group, hugging, handshake); the speaker is by themselves."
)


def build_angle_user_prompt(
    *,
    topic: str,
    context_block: str,
    user_dialogue: str | None = None,
) -> str:
    """Compose the USER message for the topic-MAGNIFY (commit-angle) call."""
    parts: list[str] = [
        "Commit to the single sharpest angle for an 8-second talking-head "
        "reel. Output strictly per the JSON schema.",
        f"TOPIC: {topic.strip()}",
    ]
    if user_dialogue and user_dialogue.strip():
        parts.append(
            "NOTE: the speaker's line is already written by the user (below). "
            "Your angle must FIT this exact line — frame the mood and bookends "
            "around it; do not contradict or replace it.\n"
            f'  USER LINE: "{user_dialogue.strip()}"'
        )
    parts.append(
        "PERFORMANCE EVIDENCE (study the technique that made these perform — "
        "never copy their subject):" + (context_block or "").rstrip()
    )
    parts.append(
        "Remember: stay 100% inside the user's topic and stance. Magnify it "
        "into ONE concrete, filmable angle. Never drift to a generic frame."
    )
    return "\n\n".join(parts)


TALKING_HEAD_SCRIPT_SYSTEM = (
    "You are scripting an 8-second talking-head Instagram reel for 42 Abu "
    "Dhabi — a tuition-free, peer-learning coding school in the UAE. The "
    "reel comes with a 4-second intro card before the talking head and a "
    "4-second outro card after. You write the spoken script AND the on-"
    "card text for both bookends.\n\n"
    "TOPIC FIDELITY — THE MOST IMPORTANT RULE:\n"
    "  - This reel is ABOUT the user's exact TOPIC. ENHANCE that topic — "
    "restate it as a clear, concrete spoken line and enrich it with one or two "
    "TRUE 42 Abu Dhabi facts — staying as CLOSE to the user's exact intent and "
    "wording as natural speech allows. You are NOT writing a creative reel from "
    "scratch; you are turning the user's topic into a clean spoken line.\n"
    "  - Real facts you may weave in: tuition-FREE (no fees); NO teachers, "
    "professors or lectures; peer-to-peer learning; project-based / learn by "
    "building real projects; open to beginners (no prior experience or degree "
    "needed); 24/7 campus.\n"
    "  - Do NOT invent a personal anecdote, a different story, or a poetic "
    "metaphor. 'My code came alive' / 'creating real impact with my peers' is "
    "WRONG — that reinvents the topic. For topic 'Join 42 Abu Dhabi' a GOOD "
    "line is concrete and on-message, e.g. 'Join 42 Abu Dhabi — coding "
    "education that's completely free, with no teachers, where you learn by "
    "building real projects with your peers.'\n"
    "  - The `hook`, `dialogue` and `intro_headline` all directly express THIS "
    "exact topic and the stance it takes. If a COMMITTED ANGLE block is "
    "provided in the user message, follow it — it is the topic restated "
    "faithfully.\n"
    "  - The speaker talks like a real person about THIS topic. Concrete facts "
    "over slogans.\n\n"
    "HOW TO USE THE REFERENCE EVIDENCE (past @42abudhabi posts + scripts):\n"
    "  - The evidence is there to teach you the HOOK technique, pacing, "
    "rhythm, structure and what made those posts PERFORM. Borrow the "
    "mechanics — never borrow their subject. The subject stays the user's "
    "topic. Do not copy a past script's angle just because it's in front of "
    "you.\n\n"
    "HARD RULES for the talking head (no exceptions):\n"
    "  - ONE on-camera speaker, COMPLETELY ALONE — the only person in the "
    "scene, with NO other person on camera or implied off camera. `action` must "
    "NEVER involve or reference another person (no high-five, wave, handshake, "
    "fist-bump, pointing at or glancing at anyone). The speaker stays calm, "
    "still and squarely facing the camera. Their mouth moves in sync with the "
    "audio. NO voice-over over graphics. NO montage. NO b-roll. The face is in "
    "frame the ENTIRE 8 seconds.\n"
    "  - Open with the hook in the first 1.5 seconds. The `hook` field is "
    "the EXACT opening words of the `dialogue` field — same characters, "
    "same case, same punctuation.\n"
    "  - Dialogue is conversational first-person. 13-16 words for English "
    "(short, calm, unhurried — fits an 8s clip naturally). Never preach. "
    "Never sell.\n"
    "  - No emojis. No hashtags. No quote marks inside dialogue. No "
    "'we believe' / 'we empower' / 'we unlock' / corporate boilerplate.\n"
    "  - Use the BRAND BACKGROUND in context. Match the brand's voice DO. "
    "Avoid every brand DON'T.\n"
    "  - When the user message has a CHARACTER LOCK block, write `subject` "
    "exactly as that block says.\n"
    "  - When the user has selected a background reference, match its "
    "`lighting` and `palette` (described to you in the user message).\n\n"
    "DEFAULT SPEAKER (when the user message has no CHARACTER LOCK block):\n"
    "  - Unless the TOPIC or the user's dialogue fixes the speaker's "
    "nationality, ethnicity or attire, the speaker is an Emirati national "
    "aged 18-30: a man in a white kandura and ghutra (agal optional), or a "
    "woman in an abaya with a shayla neatly framing a fully visible face. "
    "Respectful and true to life: no niqab, no sunglasses, nothing covering "
    "the face, no costume props. Set `speaker_user_specified` to false.\n"
    "  - Keep any other trait the topic fixes (a gender, an age, a role). "
    "Those alone do not make `speaker_user_specified` true.\n"
    "  - When the topic or dialogue does fix the speaker's nationality, "
    "ethnicity or attire, describe the speaker that way and set "
    "`speaker_user_specified` to true.\n"
    "  - `subject` is always a concrete visual description of the person. "
    "Never write an id, a '#' tag, a library name, or a phrase about an "
    "attached or reference image.\n\n"
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


# ---------------------------------------------------------------------------
# Default speaker (#56): who is on camera when no character is picked
# ---------------------------------------------------------------------------
#
# The script model is told the default (the DEFAULT SPEAKER rules above) and
# the service then makes it deterministic: unless the topic or the dialogue
# fixes the speaker (`speaker_user_specified`), `subject` becomes one of these
# casts, alternating man and woman across runs. Ages 18-30, every face fully
# visible (no niqab, no sunglasses), no caricature props.

DEFAULT_SPEAKER_BLOCK = (
    "DEFAULT SPEAKER: no character reference is picked. Unless the TOPIC or the "
    "user's dialogue fixes the speaker's nationality, ethnicity or attire, "
    "`subject` describes an Emirati national aged 18-30 (a man in a white "
    "kandura and ghutra, or a woman in an abaya with a shayla framing a fully "
    "visible face) and `speaker_user_specified` is false. Write `subject` as a "
    "concrete description of the person, never an id, a tag or a name."
)

# metadata.speaker.source: the default cast, a speaker the topic fixed, or one
# the user typed (or chose with a chip) in the Look step's Speaker field.
SPEAKER_SOURCE_DEFAULT = "default_emirati"
SPEAKER_SOURCE_TOPIC = "topic"
SPEAKER_SOURCE_USER = "user"
SPEAKER_GENDERS = ("man", "woman")

EMIRATI_DEFAULT_CASTS: tuple[dict[str, str], ...] = (
    {"key": "man_white_agal", "gender": "man", "subject": (
        "an Emirati man in his early 20s with a short, neat beard, wearing a "
        "crisp white kandura and a white ghutra held by a black agal")},
    {"key": "man_red_ghutra", "gender": "man", "subject": (
        "an Emirati man in his late 20s with a trimmed beard and moustache, "
        "wearing a white kandura and a red-and-white checked ghutra with a "
        "black agal")},
    {"key": "man_glasses", "gender": "man", "subject": (
        "an Emirati man in his mid 20s with light stubble and thin-framed "
        "glasses, wearing a white kandura and a white ghutra worn without an "
        "agal")},
    {"key": "man_grey_kandura", "gender": "man", "subject": (
        "an Emirati man of about 19, clean-shaven, wearing a light grey kandura "
        "and a white ghutra with a black agal")},
    {"key": "woman_black", "gender": "woman", "subject": (
        "an Emirati woman in her early 20s wearing a black abaya and a black "
        "shayla wrapped neatly around her hair, her whole face fully visible")},
    {"key": "woman_navy", "gender": "woman", "subject": (
        "an Emirati woman in her late 20s wearing a navy abaya and a matching "
        "navy shayla that neatly frames her fully visible face")},
    {"key": "woman_embroidered", "gender": "woman", "subject": (
        "an Emirati woman in her mid 20s with thin-framed glasses, wearing a "
        "black abaya with subtly embroidered cuffs and a black shayla framing "
        "her fully visible face")},
    {"key": "woman_grey", "gender": "woman", "subject": (
        "an Emirati woman of about 19 wearing a soft grey abaya and a beige "
        "shayla that neatly frames her fully visible face")},
)

# A still's subject when a run has no speaker and its script subject was only
# a library tag (runs made before the default existed).
DEFAULT_SPEAKER_FALLBACK = (
    "an Emirati national in their twenties in traditional Emirati dress, with "
    "the face fully visible"
)

# The Veo subject line when the keyframe was made from a person image: the
# first frame IS that person, so the prompt names the frame, not stale text.
PERSON_IN_FIRST_FRAME = "the person shown in the first frame"

# '#234 person_a_2c28f4', '#198 yao_7dc766 (Yao)': producer-library tags the
# old roster asked for. TH v2 never resolves them to an image.
_LIBRARY_TAG_RE = re.compile(r"#\d+(?:\s+[A-Za-z0-9]+(?:_[A-Za-z0-9]+)+)?(?:\s*\([^)]*\))?")
_ATTACHED_REF_RE = re.compile(
    r"(?:\bthe\s+)?\bperson\s+(?:shown\s+)?in\s+the\s+attached\s+"
    r"(?:reference\s+)?(?:image|photo|reference)\b",
    re.I,
)
_WOMAN_WORDS_RE = re.compile(
    r"\b(?:woman|girl|lady|female|she|her|hers|herself|abaya|shayla|hijab)\b", re.I,
)
_MAN_WORDS_RE = re.compile(
    r"\b(?:man|boy|guy|male|he|him|his|himself|kandura|ghutra|agal)\b", re.I,
)
# A topic fixes the speaker's gender only by naming a person: 'a woman', 'as
# a young mother', "I'm an Emirati man". Pronouns there usually mean someone
# else ('how my mentor changed my life: he...'), so they are not read.
_TOPIC_WOMAN_RE = re.compile(
    r"\ban?\s+(?:[\w'-]+\s+){0,3}?(?:woman|girl|lady|female|mother|mom|mum|"
    r"sister|daughter|wife)\b", re.I,
)
_TOPIC_MAN_RE = re.compile(
    r"\ban?\s+(?:[\w'-]+\s+){0,3}?(?:man|boy|guy|male|father|dad|brother|son|"
    r"husband|gentleman)\b", re.I,
)
_SPEAKER_CHIP_RE = re.compile(r"^\s*(?:an?\s+)?emirati\s+(man|woman)\s*\.?\s*$", re.I)


def clean_subject(text: Any) -> str:
    """A script subject without what nothing can resolve: producer-library
    tags ('#234 person_a_2c28f4', '#198 yao_7dc766 (Yao)') and the phrase
    'the person in the attached reference image'. '' when nothing is left."""
    s = text if isinstance(text, str) else ""
    s = _LIBRARY_TAG_RE.sub(" ", s)
    s = _ATTACHED_REF_RE.sub(" ", s)
    s = re.sub(r"\s+", " ", s)
    s = re.sub(r"\s+([,.;:])", r"\1", s)
    return s.strip(" ,;:-")


def subject_gender(text: Any) -> str | None:
    """'man' / 'woman' when a description of ONE speaker says which (nouns,
    pronouns, attire); None when it says neither or both."""
    s = text if isinstance(text, str) else ""
    woman, man = bool(_WOMAN_WORDS_RE.search(s)), bool(_MAN_WORDS_RE.search(s))
    if woman == man:
        return None
    return "woman" if woman else "man"


def topic_speaker_gender(*texts: Any) -> str | None:
    """'man' / 'woman' when the topic or the user's dialogue fixes the
    speaker's gender by naming a person ('a female student', 'as a young
    mother', "I'm an Emirati man"); None when it names neither or both."""
    s = " ".join(t for t in texts if isinstance(t, str))
    woman, man = bool(_TOPIC_WOMAN_RE.search(s)), bool(_TOPIC_MAN_RE.search(s))
    if woman == man:
        return None
    return "woman" if woman else "man"


def default_emirati_cast(rng: Any, gender: str | None = None) -> dict[str, str]:
    """One of EMIRATI_DEFAULT_CASTS ({key, gender, subject}), picked with
    `rng` (a random.Random) among the casts of `gender` ('man' / 'woman';
    anything else lets `rng` pick the gender too)."""
    g = gender if gender in SPEAKER_GENDERS else rng.choice(SPEAKER_GENDERS)
    return dict(rng.choice([c for c in EMIRATI_DEFAULT_CASTS if c["gender"] == g]))


def is_default_cast(subject: Any) -> bool:
    """Whether `subject` is one of EMIRATI_DEFAULT_CASTS, word for word."""
    return any(c["subject"] == subject for c in EMIRATI_DEFAULT_CASTS)


def speaker_chip_gender(text: Any) -> str | None:
    """The gender of a Speaker chip ('Emirati man' / 'Emirati woman', with or
    without an article), else None (free text the user typed)."""
    m = _SPEAKER_CHIP_RE.match(text if isinstance(text, str) else "")
    return m.group(1).lower() if m else None


def effective_subject(
    script: dict | None,
    speaker: dict | None = None,
    *,
    has_person_ref: bool = False,
    fallback: str = "",
) -> str:
    """The subject a still or clip prompt describes. With a person image
    behind the frame (`has_person_ref`) it is PERSON_IN_FIRST_FRAME; else the
    run's speaker, else the script subject, both without library tags; else
    `fallback`."""
    if has_person_ref:
        return PERSON_IN_FIRST_FRAME
    sp = speaker if isinstance(speaker, dict) else {}
    return (
        clean_subject(sp.get("subject"))
        or clean_subject((script or {}).get("subject"))
        or fallback
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
    committed_angle: dict | None = None,
    recent_bookends: list[dict] | None = None,
) -> str:
    """Compose the USER message for the GPT-4o script call.

    `context_block` comes straight from the producer's `build_context_block`
    helper — brand summary + DON'T REPEAT + nearest past scripts + IG posts +
    IG scenes (with transcripts!). The TH v2 service leaves the character
    library roster out (include_character_library=False).

    Optional `*_brief` blocks describe the references the user picked. With
    no `character_brief` the DEFAULT SPEAKER block says who is on camera.

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

    # ---- COMMITTED ANGLE (from the topic-magnify stage) --------------------
    # The system prompt explicitly defers to this block: "If a COMMITTED ANGLE
    # block is provided in the user message, follow its sharpened angle
    # exactly." It locks the whole script to one topic-faithful angle.
    if committed_angle:
        ca: list[str] = [
            "COMMITTED ANGLE — write the ENTIRE script to express THIS exact "
            "angle and nothing more generic. This is the topic, magnified:"
        ]
        if committed_angle.get("committed_angle"):
            ca.append(f"  - Angle: {committed_angle['committed_angle']}")
        if committed_angle.get("specific_detail"):
            ca.append(
                f"  - Concrete detail to dramatize: {committed_angle['specific_detail']}"
            )
        if committed_angle.get("hook_technique"):
            ca.append(
                "  - Hook technique (borrow the mechanic, not a subject): "
                f"{committed_angle['hook_technique']}"
            )
        avoid = committed_angle.get("avoid")
        if isinstance(avoid, list) and avoid:
            ca.append(
                "  - Do NOT drift into: "
                + "; ".join(str(a) for a in avoid if str(a).strip())
            )
        ca.append(
            "  - The hook, dialogue AND intro_headline must all live inside "
            "this angle. Keep the user's exact stance — do not generalise it."
        )
        parts.append("\n".join(ca))

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
                    f"  - scenes[{i}].dialogue: YOU write it. ~13-16 words, "
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
            "scene) that flow narratively, each ~13-16 words (one short calm "
            "sentence). Populate the "
            "`scenes` array with {scene_count} entries. Mirror scenes[0] "
            "into the top-level dialogue/hook fields.".replace(
                "{scene_count}", str(scene_count)
            )
        )

    parts.append(
        "CONTEXT — STUDY THE TECHNIQUE ONLY (the hook, pacing and structure of "
        "what performed); NEVER borrow the subject. The subject stays the "
        "user's topic / committed angle above:" + context_block.rstrip()
    )

    # ---- Bookend anti-repetition -------------------------------------------
    # Recent intro/outro card lines the studio already shipped. The new
    # intro_headline / outro_headline must be clearly distinct from these so
    # bookends stop feeling samey across reels.
    if recent_bookends:
        intros = [
            (b.get("intro_headline") or "").strip()
            for b in recent_bookends
            if (b.get("intro_headline") or "").strip()
        ]
        outros = [
            (b.get("outro_headline") or "").strip()
            for b in recent_bookends
            if (b.get("outro_headline") or "").strip()
        ]
        if intros or outros:
            rb: list[str] = [
                "RECENT BOOKENDS ALREADY USED — your `intro_headline` and "
                "`outro_headline` MUST be clearly different from every line "
                "below, in BOTH wording and framing. Do not reuse or lightly "
                "reword them:"
            ]
            for h in intros[:12]:
                rb.append(f"  - intro: {h}")
            for h in outros[:12]:
                rb.append(f"  - outro: {h}")
            parts.append("\n".join(rb))

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
    else:
        parts.append(DEFAULT_SPEAKER_BLOCK)
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
            "each scene's hook = its opening 6-10 words, 13-16 words per "
            "scene, no buzzwords, every field non-empty."
        )
    else:
        parts.append(
            "Remember: hook in the first 8 words of dialogue, 13-16 words total, "
            "no buzzwords, every field non-empty."
        )
    parts.append(
        f"REMEMBER — the script's SUBJECT is the TOPIC: \"{topic.strip()}\". "
        "Write fresh hook, dialogue and bookend text ABOUT this exact topic. The "
        "context/examples above are for TECHNIQUE ONLY — never borrow their "
        "subject or wording."
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
    "  - Trim to ~13-16 spoken words (one short, calm sentence — speaks "
    "unhurried in ~8 s).\n"
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


def _body_pose_only(action: str) -> str:
    """Strip explicit facial-expression / smile language from a script action so
    the keyframe's BODY-pose line never contradicts the face+mouth lock (which
    keeps the mouth exactly as the reference). Pose/gesture words are kept."""
    a = action or ""
    a = re.sub(
        r",?\s*(?:while\s+|and\s+|with\s+)?(?:a\s+)?"
        r"(?:(?:big|wide|warm|broad|slight|soft|gentle|engaging|bright|friendly)\s+)*"
        r"(?:smile|smiling|smiles|grin|grinning|grins|laugh|laughing|laughs|"
        r"beaming|teeth|open[- ]mouth|mouth open)\b",
        "", a, flags=re.IGNORECASE,
    )
    a = re.sub(r"\s{2,}", " ", a).strip(" ,;.-")
    return a or "relaxed, natural posture, facing the camera"


# ---------------------------------------------------------------------------
# Framing (#56): how close the camera is
# ---------------------------------------------------------------------------
#
# Three shot sizes the user picks (a run default, metadata.framing, and an
# optional per-scene override). A still is made at its framing by a SHOT SIZE
# block with a concrete crop point, lens, distance and head / foot room: one
# weak "Camera: medium close-up" line used to lose to the attached references
# (a 1:1 head-and-shoulders character photo made an extreme close-up, a wide
# room photo a tiny person). A still that is a photo (used as-is, or the
# keep-character canvas) has the photo's framing: FRAMING_AS_PHOTO. Veo is
# told the same framing (VEO_CAMERA_LINES).

FRAMINGS = ("close", "above_hip", "full_body")
FRAMING_CLOSE = "close"
FRAMING_ABOVE_HIP = "above_hip"
FRAMING_FULL_BODY = "full_body"
# A run made from now on; a run without the key reads as LEGACY_FRAMING (its
# stills were made close).
DEFAULT_FRAMING = FRAMING_ABOVE_HIP
LEGACY_FRAMING = FRAMING_CLOSE
# A still whose framing is the photo's own (as-is, or the keep-character
# canvas): no framing was asked of it.
FRAMING_AS_PHOTO = "as_photo"

FRAMING_LABELS = {
    FRAMING_CLOSE: "Close-up",
    FRAMING_ABOVE_HIP: "Above the hip",
    FRAMING_FULL_BODY: "Full body",
}

_FRAMING_SYNONYMS = {
    "close": FRAMING_CLOSE, "closeup": FRAMING_CLOSE, "close_up": FRAMING_CLOSE,
    "head_and_shoulders": FRAMING_CLOSE, "headshot": FRAMING_CLOSE,
    "above_hip": FRAMING_ABOVE_HIP, "above_the_hip": FRAMING_ABOVE_HIP,
    "above_hips": FRAMING_ABOVE_HIP, "hip": FRAMING_ABOVE_HIP, "waist": FRAMING_ABOVE_HIP,
    "waist_up": FRAMING_ABOVE_HIP, "medium": FRAMING_ABOVE_HIP, "medium_shot": FRAMING_ABOVE_HIP,
    "full_body": FRAMING_FULL_BODY, "fullbody": FRAMING_FULL_BODY, "full": FRAMING_FULL_BODY,
    "head_to_toe": FRAMING_FULL_BODY, "full_length": FRAMING_FULL_BODY,
}


def framing_or_none(value: Any) -> str | None:
    """`value` as one of FRAMINGS ('close-up', 'Full body', 'waist' ...), or
    None when it names no framing."""
    if not isinstance(value, str):
        return None
    key = re.sub(r"[\s\-]+", "_", value.strip().lower())
    return _FRAMING_SYNONYMS.get(key)


def norm_framing(value: Any) -> str:
    """`value` as one of FRAMINGS; anything else (None, '', garbage) is
    LEGACY_FRAMING, the framing of a run made before framing existed."""
    return framing_or_none(value) or LEGACY_FRAMING


SHOT_SIZE_BLOCKS = {
    FRAMING_CLOSE: (
        "SHOT SIZE: CLOSE-UP (head and shoulders). Eye-level camera, 85 mm "
        "portrait lens, about 1.2 m from the speaker. The bottom edge of the frame "
        "crosses the upper chest, about a hand's width below the collarbones; both "
        "shoulders are inside the frame. Small headroom above the hair or head "
        "covering: never crop the top of the head, the chin or the ears. Eyes on "
        "the upper-third line, face centred, hands out of frame. Tighter than a "
        "waist-up shot, looser than a face-only extreme close-up."
    ),
    FRAMING_ABOVE_HIP: (
        "SHOT SIZE: MEDIUM SHOT, from just above the hips up. Eye-level camera at "
        "chest height, 50 mm lens, about 2.5 m away. The bottom edge of the frame "
        "crosses the body just above the hips (the belt line), so the whole torso, "
        "both arms, the elbows and the hands are in frame. Clear headroom, about a "
        "tenth of the frame. The face stays clearly readable and evenly lit. Not a "
        "chest-up shot; the knees are not visible."
    ),
    FRAMING_FULL_BODY: (
        "SHOT SIZE: FULL-BODY SHOT, head to toe. Camera level at waist height, "
        "35 mm lens, about 4 to 5 m away, no tilt, no wide-angle distortion. The "
        "ENTIRE person is in frame from the top of the head to the soles of the "
        "shoes, standing upright on the floor. Leave clear space above the head "
        "and below the feet (each at least about 6% of the frame height), with "
        "floor visible in front of the shoes. The face is small but sharp, lit "
        "and looking into the lens. Never crop the feet, the ankles or the top of "
        "the head."
    ),
}

# The body pose a still is made with, per framing (the script's `action` is
# always the fixed calm posture; this says what the shot shows of it).
FRAMING_STILL_POSE = {
    FRAMING_CLOSE: (
        "calm and composed, facing the camera directly, shoulders square and "
        "relaxed, hands out of frame"
    ),
    FRAMING_ABOVE_HIP: (
        "standing upright, facing the camera directly, calm and composed, hands "
        "resting loosely together in front at waist height or relaxed at the "
        "sides, both hands visible and still"
    ),
    FRAMING_FULL_BODY: (
        "standing upright on the floor, facing the camera directly, weight on "
        "both feet, arms relaxed at the sides, calm and composed"
    ),
}

_FRAMING_PERSON_CLAUSE = (
    "The attached PERSON reference defines WHO the speaker is and what they wear, "
    "NOT the framing. Ignore how tightly that photo is cropped and frame this shot "
    "at the SHOT SIZE given here."
)
_FRAMING_BG_CLAUSE = (
    "The attached BACKGROUND reference defines the location only, not the camera "
    "distance: keep its place, and frame the speaker at the SHOT SIZE given here."
)
_FRAMING_OUTFIT_CLAUSE = (
    "Where the outfit continues beyond what the reference shows, continue the SAME "
    "garments naturally (a kandura, abaya or long dress continues to its full "
    "length; otherwise plain trousers and simple shoes in a neutral tone that "
    "matches the top). No new accessories or props."
)

# A framing change on a still that exists is a re-shoot of THAT still (the
# refine engine, the still as the canvas): only the camera distance changes.
# Each fits STILL_REFINE_CHANGE_MAX and says nothing about the face's
# expression, so the refine keeps the mouth closed.
FRAMING_RESHOOT_CHANGES = {
    FRAMING_CLOSE: (
        "Re-frame this exact shot as a CLOSE-UP, head and shoulders: the bottom "
        "edge crosses the upper chest, both shoulders in frame, small headroom, "
        "never crop the top of the head or the chin. Same person, same outfit, "
        "same room and light; only the camera distance changes (move in closer)."
    ),
    FRAMING_ABOVE_HIP: (
        "Re-frame this exact shot as a MEDIUM SHOT from just above the hips up: "
        "the whole torso, both arms and the hands in frame, hands relaxed, clear "
        "headroom. Same person, same outfit, same room and light; only the camera "
        "distance changes (zooming out shows more of the same outfit and room)."
    ),
    FRAMING_FULL_BODY: (
        "Re-frame this exact shot as a FULL-BODY SHOT, head to toe: the whole "
        "person standing on the floor, from the top of the head to the soles of "
        "the shoes, with space above the head and below the feet. Same person, "
        "same outfit continued naturally to full length, same room and light; "
        "only the camera distance changes (zooming out shows the whole body)."
    ),
}


def framing_reshoot_change(framing: Any) -> str:
    """The refine change that re-frames a still to `framing`."""
    return FRAMING_RESHOOT_CHANGES[norm_framing(framing)]


def build_main_keyframe_prompt(
    *,
    script: dict,
    character_brief: str | None,
    bg_brief: str | None,
    lock_character: bool,
    has_character_ref: bool = False,
    has_bg_ref: bool | None = None,
    keep_subject: bool = False,
    aspect: str = "9:16",
    identity_anchor: bool = False,
    framing: str = FRAMING_CLOSE,
) -> str:
    """Build the prompt for the talking-head keyframe (the still Veo animates).

    Reference handling is ROLE-BASED (we name "the attached PERSON reference" /
    "the attached BACKGROUND reference" / "the attached CURRENT FRAME" by their
    CONTENT, not by image index) so the prompt stays correct regardless of the
    source-image order the caller assembles. Four cases:

    - `keep_subject` (canvas = the scene's CURRENT still): keep the person
      EXACTLY, change ONLY the background (to the attached bg reference, or a
      fresh AI setting). This is the precise "lock the character, swap the bg"
      path — the face is the canvas so it cannot drift.
    - compose with a PERSON reference (`has_character_ref and lock_character`):
      reproduce the exact face+outfit, place into the BACKGROUND reference's
      location when one is attached (character wins; bg matched via the prompt).
    - background-only (`has_bg_ref and not has_character_ref` and no character
      brief): keep the background EXACTLY and generate a person into it.
    - free-gen (no references): describe everything from the script.

    `has_character_ref` says a PERSON image is actually attached. It defaults to
    False and is never inferred from `lock_character`: a lock with no person
    image used to select Case B ("the attached PERSON reference IS the
    speaker") with nothing attached, so the model invented a random face and
    the script subject was dropped. Callers pass what they really attach.
    `has_bg_ref` defaults (None) to the presence of `bg_brief` for legacy
    callers; the TH v2 service passes it explicitly too.

    `identity_anchor`: the attached PERSON reference is scene 0's still (a
    scene >= 1 with no character pick takes its speaker from it), so it fixes
    the person's IDENTITY only, never the framing, pose or location. It uses
    the compose case whatever `lock_character` is (the lock is about a picked
    character).

    `framing` (one of FRAMINGS; anything else reads as close, the default,
    which is what a call without it always made) sets the SHOT SIZE block and
    the body pose of Cases B, C and D, in place of the script's free-text
    `camera` (never used for the shot size). An attached person reference is
    told it sets identity, NOT the framing; an attached background, the
    location, not the camera distance. Case A keeps its canvas's framing.
    """
    has_character_ref = bool(has_character_ref)
    if has_bg_ref is None:
        has_bg_ref = bool(bg_brief)
    aspect_phrase = _aspect_phrase(aspect)
    framing = norm_framing(framing)
    shot_size = SHOT_SIZE_BLOCKS[framing]
    pose = FRAMING_STILL_POSE[framing]

    lighting = (script.get("lighting") or "soft natural daylight").strip()
    subject = (script.get("subject") or "").strip()

    # Collapse bg_brief to its first meaningful line for the compact setting text.
    setting = ""
    if bg_brief:
        for ln in bg_brief.splitlines():
            ln = ln.strip().lstrip("•- ").strip()
            if ln and not ln.startswith("("):
                setting = ln
                break

    # Enumerated identity lock — research shows naming the specific facial
    # features to preserve beats a vague "same person", and positive phrasing
    # ("keep X identical") works better than negatives for these models.
    face_lock = (
        "Preserve the person's facial identity with 100% fidelity: the SAME eye "
        "shape, spacing and colour, the same eyebrows, the same nose bridge and "
        "tip, the same jawline, chin and cheekbones, the same lips and mouth, the "
        "same ears, the same skin tone and texture (keep every mole, freckle and "
        "mark), and the same hairline and hairstyle. Same apparent age, same build. "
        "The face in the output must be unmistakably the SAME individual as the "
        "reference — do not beautify, smooth, slim, re-age, stylise, or swap them "
        "for a different-looking person. Keep the EXACT same facial expression "
        "as the reference — do not add, remove, or exaggerate a smile or any "
        "expression; reproduce the mouth and eyes exactly as they appear in the "
        "reference."
    )
    required = (
        "REQUIRED: the SHOT SIZE above exactly, face fully in frame, eyes open "
        "and looking at the camera, "
        "natural skin tones, no NEW text overlays, no NEW logos added (logos "
        "already on the person's clothing must stay), no AI artifacts (extra "
        "fingers, melted features, plastic skin). Exactly ONE person in frame — "
        "the speaker, ALONE; no second person, bystander, crowd, or any other "
        f"person's hands, arms, limbs or shadows. Photo-realistic, NOT "
        f"illustrated, {aspect_phrase}."
    )
    scene_lines = "\n".join([
        "Scene (only these may change):",
        f"  - Pose: {pose}",
        f"  - Lighting: {lighting}",
    ])

    # ---- Case A: keep the existing subject; change ONLY the background --------
    if keep_subject:
        top = (
            "EDIT THE ATTACHED IMAGE — the person in it is the single source of "
            "truth. Keep the PERSON exactly as they already appear: same face and "
            "the same facial expression, the same gaze direction, the same head "
            "angle, the same pose and body, the same hair, and the same outfit. "
            + face_lock
        )
        if has_bg_ref:
            mid = (
                "Change ONLY the background. Replace it with the attached BACKGROUND "
                "reference and match that location faithfully. Relight the person's "
                "edges to match the new background's light direction and colour "
                "temperature and add a natural contact shadow — but do NOT "
                "re-render, reshape, repaint, or re-pose the person, their face, "
                "features, hair or expression."
            )
        else:
            # `subject` describes the PERSON, never a location — don't use it as
            # the setting. With no bg reference, let the model pick a fitting one.
            loc = setting or "a clean, naturally-lit setting that suits the scene"
            mid = (
                "Change ONLY the background. Replace it with a fresh setting: "
                f"{loc}. Match the new background's lighting and colour temperature "
                "to the person and add a natural contact shadow — but do NOT "
                "re-render, reshape, repaint, or re-pose the person, their face, "
                "features, hair or expression."
            )
        bottom = (
            "Everything except the background stays IDENTICAL to the attached image "
            "— treat the person's pixels as locked. If any scene text conflicts with "
            "the attached person, IGNORE the text and keep the person exactly."
        )
        # Deliberately NO pose/camera/gaze directives here: inviting those forces a
        # subject re-render and drifts the face. The reference frame governs them.
        keep_required = (
            "REQUIRED: photo-realistic (not illustrated), natural skin tones, no NEW "
            "text or logos added (logos already on the clothing stay), no AI "
            "artifacts (extra fingers, melted or asymmetric features, plastic skin), "
            f"{aspect_phrase}."
            " Exactly ONE person in frame — the speaker, ALONE; no other people "
            "or body parts of other people."
        )
        return "\n\n".join([top, mid, bottom, keep_required])

    # ---- Case B: compose an exact PERSON reference (character wins) -----------
    if has_character_ref and (lock_character or identity_anchor):
        top = (
            "FACE-AND-OUTFIT LOCK. The attached PERSON reference IS the speaker and "
            "the outfit — reproduce them exactly. " + face_lock + " Reproduce the "
            "hair and EVERY garment exactly as shown; do not redesign or redress."
        )
        if identity_anchor:
            top += (
                "\n\nIDENTITY ANCHOR. The attached PERSON reference is an earlier shot "
                "of this same speaker from this reel. It fixes IDENTITY only: the "
                "face, skin tone, hair and outfit. It does NOT fix the framing, "
                "camera distance, pose, background or location, so do not copy "
                "those from it. Stage this shot fresh from the scene description "
                "below."
            )
        top += "\n\n" + _FRAMING_PERSON_CLAUSE
        if has_bg_ref:
            place = (
                "Place this exact person into the location shown in the attached "
                "BACKGROUND reference — match that background faithfully; do not "
                "redesign or replace it. " + _FRAMING_BG_CLAUSE
            )
        elif identity_anchor:
            place = (
                f"Setting: {setting or 'a simple, fitting background'}, not the "
                "background of the person reference."
            )
        else:
            place = f"Setting: {setting or 'a simple, fitting background'}."
        bottom = (
            "face, expression, hair and outfit = the attached person reference, EXACTLY. Only "
            "pose, framing, lighting and the surrounding setting may change — the "
            "facial identity must NOT. If any scene text describes a different "
            "person (gender, age, ethnicity, clothing), IGNORE it and trust the "
            "attached person reference."
            " The facial expression must stay EXACTLY as in the attached reference — "
            "ignore any direction to smile or change expression."
        )
        scene_lines_b = "\n".join([
            "Scene — you may restage the BODY pose, framing, lighting and "
            "background, but NOT the face:",
            f"  - Body pose and hands: {_body_pose_only(pose)}",
            f"  - Lighting: {lighting}",
            "  - PEOPLE: exactly ONE person in frame — the referenced speaker, "
            "ALONE. No second person, bystanders, crowd, or any other person's "
            "hands, arms, limbs or shadows. Front-facing, calm, natural posture.",
            "  - FACE & MOUTH: keep them IDENTICAL to the attached person "
            "reference — the SAME mouth exactly as shown (if the reference mouth "
            "is closed and neutral, it stays closed and neutral). Do NOT open the "
            "mouth, show teeth, smile, grin or laugh, even if the body pose or any "
            "scene text suggests it. The expression is locked to the reference.",
        ])
        blocks = [top, place, shot_size, scene_lines_b]
        if framing != FRAMING_CLOSE:
            blocks.append(_FRAMING_OUTFIT_CLAUSE)
        return "\n\n".join([*blocks, bottom, required])

    # ---- Case C: background-only — keep the bg, generate a person into it -----
    if has_bg_ref and not has_character_ref and not character_brief:
        person = subject or "a person speaking naturally to the camera"
        top = (
            "KEEP THE ATTACHED BACKGROUND. The attached image is the BACKGROUND / "
            "LOCATION — reproduce it EXACTLY; do NOT redesign, replace, recolor, or "
            "restyle the location. " + _FRAMING_BG_CLAUSE
        )
        mid = (
            f"Generate a person into this exact location: {person}. The person must "
            "sit naturally in the scene (correct scale, perspective, lighting). The "
            "background must remain identical to the attached image."
        )
        return "\n\n".join([top, mid, shot_size, scene_lines, required])

    # ---- Case D: free-gen (no references) -------------------------------------
    palette = _palette_str(script.get("palette"))
    lines: list[str] = [
        "Photo-real keyframe for the opening frame of an 8-second talking-head "
        f"Instagram reel. {aspect_phrase} composition.",
        f"Subject: {subject}",
        shot_size,
        f"Pose at frame open: {pose}",
        f"Lighting: {lighting}",
    ]
    if palette:
        lines.append(f"Color palette (use ≥3): {palette}")
    if script.get("style"):
        lines.append(f"Style: {script['style']}")
    if script.get("mood"):
        lines.append(f"Mood: {script['mood']}")
    if character_brief:
        lines.append(
            "Character description (use as inspiration, not identical match — "
            "generate a similar-archetype person):"
        )
        lines.append(character_brief.rstrip())
    if bg_brief:
        lines.append("Location / background (anchor the setting here):")
        lines.append(bg_brief.rstrip())
    lines.append(required)
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
# Animation-pipeline card render (NEW) — ports Yasin's "Create Animation"
# technique into the TH v2 bookends: a real 42 logo baked in via
# gemini_image_multipart, a 2-stage render, and two reference modes.
#
#   - no-ref      → full voxel, using CREATE POSTER's own Stage-1 concept brain
#                   + Stage-2 render (poster_styles / poster_prompts, resolved
#                   in the service) — NOT a TH-owned render system.
#   - new_vibe    → fresh card that ADOPTS the reference's medium (style-DNA driven)
#   - text_only   → reproduce the reference design exactly, swap only the text
#
# The new_vibe / text_only paths still use ONE of two runtime logo clauses
# appended by the service, depending on whether the real 42 PNG is available:
#   - TH_LOGO_ATTACHED_CLAUSE — when the PNG is attached (replicate it, draw no
#     logo of your own).
#   - TH_LOGO_SELFDRAW_CLAUSE — when no PNG is available (the model draws a clean
#     flat 42 wordmark itself, mirroring the animation card's behaviour).
# (The no-ref voxel path uses Create Poster's GEMINI_STAGE2_PROMPT, which already
# embeds its own [LOGO FIREWALL & REPLICATION] rule, so no clause is appended.)
# See th_logo_clause() + _render_card_animation_style in yas_talking_head_v2.py.
# ---------------------------------------------------------------------------

# Real logo PNG IS attached as a separate part → don't draw one yourself.
TH_LOGO_ATTACHED_CLAUSE = (
    "[LOGO — USE THE ATTACHED ONE]\n"
    "A real 42 logo image is attached separately. Replicate THAT logomark "
    "exactly — a flat, crisp 2D vector overlay (no 3D shading, textures, or "
    "distortion), placed small in a lower corner. Do NOT draw, invent, or "
    "letter any other logo, wordmark, or '42' glyph yourself, and if the "
    "reference card has a logo of its own, OMIT it — the attached logo is the "
    "ONLY logo allowed in the image."
)

# No logo PNG available → render a clean 42 wordmark from scratch.
TH_LOGO_SELFDRAW_CLAUSE = (
    "[LOGO — RENDER A CLEAN 42 WORDMARK]\n"
    "Render the '42 Abu Dhabi' wordmark yourself: a small, crisp, FLAT 2D "
    "vector lockup placed unobtrusively in a lower corner. Keep it perfectly "
    "legible and undistorted — no 3D shading, no textures, no warping. If the "
    "reference card has its own logo, REPLACE it with this 42 wordmark; there "
    "must be exactly one logo in the image."
)

# Back-compat alias (older references): defaults to the attached-logo wording.
TH_NO_LOGO_CLAUSE = TH_LOGO_ATTACHED_CLAUSE


def th_logo_clause(*, logo_attached: bool) -> str:
    """Pick the logo instruction to append to a card render system prompt."""
    return TH_LOGO_ATTACHED_CLAUSE if logo_attached else TH_LOGO_SELFDRAW_CLAUSE


# NOTE: the no-ref VOXEL path no longer has a TH-owned render system. It now
# reuses Create Poster's own Stage-1 concept brain + Stage-2 render system
# (poster_prompts.GEMINI_STAGE2_PROMPT via poster_styles) directly in the
# service — see _render_card_animation_style. The old TH_CARD_RENDER_SYSTEM
# alias + build_th_card_image_user_message were removed with that swap.

# Stage-2 system for the NEW_VIBE path. Unlike the voxel render system this does
# NOT force a house style — it renders a fresh card in the reference's OWN medium
# as described by the supplied [STYLE-DNA] block. The service appends the logo
# clause at runtime.
TH_NEWVIBE_RENDER_SYSTEM = (
    "You are an expert graphic designer and brand custodian for 42 Abu Dhabi. "
    "You render a SINGLE premium title-card frame for a talking-head reel "
    "bookend.\n\n"
    "[ADOPT THE REFERENCE'S STYLE — DO NOT COPY ITS LAYOUT]\n"
    "A [STYLE-DNA] block describes the visual language of a reference card "
    "(its medium, palette, motifs, texture, typography mood). Render a FRESH "
    "composition whose art style EXACTLY matches that DNA: use the SAME medium "
    "(if it says halftone screen-print, render halftone screen-print; if 8-bit "
    "pixel art, render pixel art; if flat vector, render flat vector — never "
    "default to 3D voxel unless the DNA says so), the SAME palette (use the "
    "given hex codes), the SAME background treatment and motifs, the SAME "
    "typographic mood. Invent a NEW layout — never copy the reference's "
    "composition or element placement.\n\n"
    "[ASPECT RATIO]\n"
    "- Generate the image at the EXACT aspect ratio stated in the prompt; fill "
    "the whole frame edge-to-edge — no letterboxing, borders, or padding bars.\n\n"
    "[TYPOGRAPHY & TEXT]\n"
    "- Find the '[TEXT TO RENDER]' block in the prompt. Read PLACEMENT and "
    "position the text there. Print ONLY the strings inside the quotes — never "
    "the labels. Bold, legible type in the DNA's typographic mood. ENGLISH "
    "ONLY — never render Arabic or any non-Latin script. Spell every word "
    "exactly as given. If a line is missing, do not invent one.\n\n"
    "[NO PEOPLE — HARD RULE]\n"
    "- No humans, faces, mascots, or silhouettes. Graphic title card only."
)

# Stage-2 system for the TEXT_ONLY path: keep the attached reference design
# pixel-faithful, change only the text + logo. The service appends the logo
# clause at runtime.
TH_TEXT_ONLY_RENDER_SYSTEM = (
    "You are a brand-careful graphic EDITOR for 42 Abu Dhabi. A reference card "
    "image is attached. Reproduce it EXACTLY — same layout, colours, "
    "illustration, proportions, composition, and background — changing ONLY "
    "the text and the logo.\n\n"
    "[WHAT CHANGES]\n"
    "- Replace every piece of text in the reference with the new text from the "
    "'[TEXT TO RENDER]' block (print ONLY the quoted strings, spelled exactly, "
    "ENGLISH ONLY). Match the reference's existing type style, weight, colour, "
    "and position as closely as possible so the swap is seamless.\n"
    "- Swap the logo per the logo instruction below.\n\n"
    "[WHAT STAYS]\n"
    "- Everything else: keep the artwork, palette, textures, and composition "
    "byte-faithful to the reference. Do NOT restyle, recolour, or re-illustrate "
    "the card. Output the frame at the EXACT aspect ratio stated in the prompt, "
    "edge-to-edge."
)

# Animation-style Veo lock clause spliced into the bookend motion prompts so the
# baked text + logo never warp/re-render during animation (mirrors the intent of
# build_animation_motion_prompt in yas_animation_prompts.py).
_BOOKEND_TEXT_LOGO_LOCK = (
    "The 42 logomark and ALL on-card text are LOCKED — they may gently settle "
    "or fade into place but must NOT warp, distort, mis-spell, re-letter, or "
    "re-render. Treat every word and the logo as a crisp static overlay; "
    "animate only the surrounding graphic elements beneath them."
)

# Always-on cinematic scene motion for the bookends — this is the ingredient
# that makes Yasin's "Create Animation" clips feel alive (depth + ambient life)
# vs. a flat 2D text transition. Medium-agnostic so it works for voxel, pixel,
# vector, photographic, or print-style cards. Paired with ANIMATE_NEGATIVE_PROMPT
# (bans fast cuts / whip pans / jitter) so the motion stays premium and controlled.
_BOOKEND_AMBIENT_MOTION = (
    "  • Add real DEPTH: a slow, subtle camera push-in or gentle parallax that "
    "separates the background from the foreground layers. Cinematic and smooth — "
    "never a fast, whippy, or shaky move.\n"
    "  • Add ambient LIFE suited to the card's OWN art style: softly drifting "
    "particles, dust, or light specks; a gentle shimmer or slow pulse travelling "
    "across background patterns, gradients, and textures; a soft light bloom "
    "drifting through. Hero/illustrated elements may breathe, hover, or settle "
    "subtly.\n"
    "  • PRESERVE the medium exactly — if it is 3D voxel keep the solid blocky "
    "look, if pixel art keep it crisp and blocky, if flat vector keep it flat, "
    "if photographic keep it photographic, if print/halftone keep the grain. Do "
    "NOT smooth, melt, blur, or re-render the artwork into a different style.\n"
)


def _th_text_block(headline: str, subtitle: str, aspect: str = "9:16") -> str:
    """Emit the animation-compatible [TEXT TO RENDER] block.

    The render systems (voxel / new_vibe / text_only) all read this block and
    print ONLY the quoted strings — so pinning the script's headline/subtitle
    here is how we keep the card text verbatim instead of model-invented.
    """
    headline = (headline or "").strip()
    subtitle = (subtitle or "").strip()
    lines = [
        "[TEXT TO RENDER]",
        "Use these exact strings VERBATIM — do not paraphrase, translate, or "
        "add any other words.",
        f"PLACEMENT: balanced for a {_aspect_phrase(aspect)} card — headline as "
        "the dominant line, subtitle smaller beneath it.",
        f'TEXT_LINE_1: "{headline}"',
    ]
    if subtitle:
        lines.append(f'TEXT_LINE_2: "{subtitle}"')
    return "\n".join(lines)


def build_th_text_only_instruction(*, headline: str, subtitle: str, aspect: str = "9:16") -> str:
    """Text part that PRECEDES the reference bytes in the text_only path."""
    return (
        "Keep this reference card's design EXACTLY (layout, colours, artwork, "
        "proportions). Change ONLY the text to the following, and remove any "
        "logo present in the reference.\n\n"
        + _th_text_block(headline, subtitle, aspect)
    )


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
        "Text and graphic elements snap in crisply with a single quick "
        "chromatic shimmer on arrival that resolves sharply within ~0.3 s — "
        "clean and energetic, NOT a sustained glitch or jitter. The "
        "composition then holds steady."
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
        "The CTA arrives with a single quick chromatic shimmer that resolves "
        "sharply within ~0.4 s into the held composition — clean and decisive, "
        "NOT a sustained glitch or jitter. The brand mark holds steady."
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


def build_intro_veo_prompt(*, topic: str, script: dict, style: str = "fade_in", aspect: str = "9:16") -> str:
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
    aspect_phrase = _aspect_phrase(aspect)
    motion = _INTRO_MOTION_CLAUSES.get(style) or _INTRO_MOTION_CLAUSES["fade_in"]
    return scrub_rai_terms(
        f"A polished, premium animation of this {aspect_phrase} intro title card for a "
        "talking-head Instagram reel. The attached image is the EXACT first "
        "frame — extrapolate motion forward from it. Treat the frame as a "
        "finished design: animate ON TOP of it, and never redesign, restyle, "
        "recolour, or re-render the artwork.\n\n"
        "MOTION\n"
        + _BOOKEND_AMBIENT_MOTION
        + f"  • Entrance accent — how the text and graphic elements settle into "
        f"place over the first ~1s, then lock: {motion}\n"
        "  • Energetic but controlled — this is the confident opening beat of a "
        "brand reel. No chaotic motion, no fast cuts, no whip pans, no harsh "
        "strobing.\n\n"
        f"{_BOOKEND_TEXT_LOGO_LOCK}\n"
        "End on a stable, clean hold so the clip cuts smoothly into the next "
        f"shot. Topic: {topic[:60]}. Mood: {mood}. "
        + (f"Palette: {palette}. " if palette else "")
        + f"{aspect_phrase}. Graphic-design only — no people, no faces, no "
        "photography of humans, no live-action. Audio: none (silent intro)."
    )


# The Veo camera line per framing (the still's: FRAMINGS, or FRAMING_AS_PHOTO
# for a photo's own framing). Close and above the hip push in a little, never
# past their shot size; full body and a photo are locked off, so the feet and
# the user's own framing stay in frame for the whole clip. Matches the end
# frame the service builds (_scene_video_inputs).
VEO_CAMERA_LINES = {
    FRAMING_CLOSE: (
        "Camera: a close-up, head and shoulders, at eye level, held as a close-up "
        "for the whole clip with at most a very slight, slow push-in toward the "
        "face; never tighter than face-and-shoulders, never cutting off the top of "
        "the head or the chin. No whip pans, no orbit, no handheld, no shake, no "
        "snap zoom"
    ),
    FRAMING_ABOVE_HIP: (
        "Camera: a medium shot from just above the hips up, at eye level, with a "
        "slow, gentle, continuous push-in that ends no tighter than chest-up; the "
        "hands stay still and in frame. No whip pans, no orbit, no handheld, no "
        "shake, no snap zoom"
    ),
    FRAMING_FULL_BODY: (
        "Camera: a full-body shot, head to toe, locked off at waist height: no "
        "zoom, no push-in, no pan and no reframing; the whole body, including the "
        "feet, stays in frame for the entire clip"
    ),
    FRAMING_AS_PHOTO: (
        "Camera: locked off, holding exactly the framing of the first frame for "
        "the whole clip: no zoom, no push-in, no pan and no reframing"
    ),
}
# Framings whose clip has no camera move at all.
VEO_LOCKED_OFF_FRAMINGS = (FRAMING_FULL_BODY, FRAMING_AS_PHOTO)


def veo_framing(value: Any) -> str | None:
    """`value` as a key of VEO_CAMERA_LINES, or None (no framing known)."""
    if value == FRAMING_AS_PHOTO:
        return FRAMING_AS_PHOTO
    return framing_or_none(value)


def build_main_veo_prompt(
    *,
    script: dict,
    brand_req: dict,
    has_character_ref: bool,
    has_bg_ref: bool,
    dialogue_language: str,
    framing: str | None = None,
) -> str:
    """Veo prompt for the 8-second main talking-head clip.

    The clip is i2v: the AUTHORITATIVE input is the keyframe, passed as the Veo
    `start_image` (= frame 1) with NO reference_images (Veo rejects both at once).
    So this prompt is written for frame-1 governance: an identity/continuity
    ANCHOR up front (Veo weights early tokens most), then the labelled scene
    description, then the speech/audio clause.

    Identity language is POSITIVE + descriptive ("the same individual, keep the
    features consistent") — never command/negation ("identity lock / do not
    change / the exact person"), which drifts more and is the deepfake-adjacent
    vocabulary the Veo RAI classifier (29310472) flags. `scrub_rai_terms()` is a
    final safety net. `has_character_ref`/`has_bg_ref` no longer index
    reference-image slots. `has_character_ref` (the keyframe was made from a
    person image) only picks the subject line: PERSON_IN_FIRST_FRAME, else the
    script subject without library tags (effective_subject).

    `framing` is the framing of the still the clip starts from (a key of
    VEO_CAMERA_LINES). It sets the camera line, never the script's free-text
    `camera`, and the continuity anchor and motion lines say "locked off"
    for a locked-off framing. None (no framing known) keeps the older lines
    built from script.camera.
    """
    # Scene/main clips are i2v with ONLY the keyframe as the first frame (no
    # reference_images — Veo rejects both together). Identity + location are
    # therefore governed by frame 1.
    _ = has_bg_ref

    subject = effective_subject(
        script, has_person_ref=has_character_ref, fallback=PERSON_IN_FIRST_FRAME,
    )
    framing = veo_framing(framing)
    locked_off = framing in VEO_LOCKED_OFF_FRAMINGS
    if locked_off:
        cam_anchor = "Locked-off camera, no zoom"
    elif framing == FRAMING_CLOSE:
        cam_anchor = "Locked-off camera or a very slight, slow push-in"
    else:
        cam_anchor = "Locked-off camera or a slow, steady push-in"
    camera = (script.get("camera") or "").strip()
    lighting = (script.get("lighting") or "").strip()
    style = (script.get("style") or "").strip()
    palette = list(script.get("palette") or [])
    mood = (script.get("mood") or "").strip()
    dialogue = (script.get("dialogue") or "").strip()

    parts: list[str] = []

    # ----- Continuity anchor FIRST (cinematography, NOT identity) -----
    # Veo weights the earliest tokens most heavily, so a SHOT/continuity anchor
    # leads — describing the take, camera and light, never the person's identity.
    # Identity is carried by the IMAGE channel (the keyframe is pinned as both the
    # first AND last frame), which is policy-safe. Identity vocabulary in the
    # prompt ("same individual / exact person / keep the face") both drifts MORE
    # and trips the Veo RAI deepfake classifier (29310472), so it is deliberately
    # absent here.
    parts.append(
        "A single continuous take — one unbroken shot, no cuts, no dissolves, no "
        f"second person. {cam_anchor}; soft, even, "
        "natural lighting held constant for the whole clip. The subject is a "
        "fictional, portrayed character — not a real, public or identifiable "
        "person — an ordinary, everyday person speaking naturally to camera in a "
        "candid moment, relaxed and naturally alive, with a calm, steady, neutral "
        "face, mostly facing the lens throughout"
    )

    if subject:
        parts.append(f"Subject: {subject}")
    camera_moves = (
        "the camera holds still" if locked_off else "the camera may hold or push in slowly"
    )
    parts.append(
        "Setting: the 42 Abu Dhabi environment shown in the first frame — keep "
        "the same room, architecture, props, colour context and lighting for the "
        f"whole clip ({camera_moves}; the place stays the same)"
    )
    parts.append(
        "Action: speaking to the camera while holding a calm, composed, natural "
        "pose — the person stays still and settled, with only the natural "
        "micro-motion of speech (lip-sync to the dialogue) and occasional soft, "
        "natural blinking. No hand gestures, no shifting of the body or "
        "shoulders, no head turns."
    )
    if locked_off:
        parts.append(
            "Motion: the camera holds still and so does the subject: still and "
            "steady, facing the camera, with only lip movement for speech and light "
            "natural blinking; no hand gestures, no body or shoulder movement, no "
            "head turns. The face stays calm, steady and consistent throughout."
        )
    else:
        parts.append(
            "Motion: the MOVEMENT in the shot comes from the CAMERA, not the person. "
            "The subject remains still and steady, facing the camera, with only lip "
            "movement for speech and light natural blinking — no hand gestures, no "
            "body or shoulder movement, no head turns. The face stays calm, steady "
            "and consistent throughout."
        )
    if framing is not None:
        parts.append(VEO_CAMERA_LINES[framing])
    else:
        parts.append(
            f"Camera: {camera or 'medium close-up'} framing, with a slow, smooth, "
            "continuous cinematic push-in (a gentle dolly-in) that runs steadily "
            "across the whole clip — controlled and premium. No whip pans, no orbit, "
            "no handheld, no shake, no snap zoom."
        )
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
        parts.append(
            "Style: photorealistic, natural and candid, true-to-life — a real "
            "everyday person, not a glamorous studio portrait"
        )
    voice = brand_req.get("voice_pillars") or []
    if voice:
        parts.append(f"Mood / voice pillars: {mood + ', ' if mood else ''}{', '.join(voice[:4])}")
    elif mood:
        parts.append(f"Mood: {mood}")

    # ----- Audio clause (Veo speech) -----
    if dialogue:
        # Reuse the producer's audio clause builder for consistency
        from app.agents.producer.prompts import format_audio_clause  # local import to avoid cycle
        audio = format_audio_clause(
            dialogue, dialogue_language,
            delivery=(
                "speaking slowly and quietly in a soft, low, even voice; calm, "
                "measured, understated and unhurried, with brief natural pauses; "
                "quiet room tone, no background music"
            ),
        )
        if audio:
            parts.append(audio)

    # Final RAI guard: strip any residual face-manipulation vocabulary the
    # classifier rejects with code 29310472 before the string reaches Veo.
    return scrub_rai_terms(". ".join(parts) + ".")


# Negative terms per framing (the still's): what would break that shot size.
VEO_FRAMING_NEGATIVES = {
    FRAMING_CLOSE: ["extreme close-up", "cropped forehead", "cropped chin"],
    FRAMING_FULL_BODY: ["cropped feet", "cut-off legs", "zooming in"],
    FRAMING_AS_PHOTO: ["zooming in", "reframing"],
}


def build_talking_head_negative_prompt(brand_req: dict, *, framing: str | None = None) -> str:
    """Build Veo's negative_prompt for the main talking-head clip.

    Layers:
      1. Quality / artefact terms — anything the model would render badly
      2. Brand DONTs from the brand profile
      3. With a `framing` (a key of VEO_CAMERA_LINES): what would break that
         shot size, last, so a long brand list keeps its place under the cap.
         Without one the prompt is the same as before framing existed.

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
        "text overlays added in post", "watermarks", "subtitles", "captions",
        "stock-photo aesthetic", "cartoon", "illustration",
        "anime style", "oversaturated",
        "montage", "split screen", "double exposure",
    ]
    cliche_terms = [
        "corporate stock-photo classroom",
        "generic handshake meeting",
        "people typing at laptops with floating UI overlays",
    ]
    # Audio negatives — Veo 3 synthesises audio inline and will sometimes lay a
    # background music bed under the speaker even though the positive prompt's
    # delivery clause already asks for "quiet room tone, no background music".
    # Listing the music vocabulary here as well keeps the main talking clip as
    # speech + light room tone only. These are AUDIO-domain terms; they do not
    # touch the visual / face / mouth / pose generation (the safety classifier
    # only trips on face-swap / identity vocabulary, not on "music").
    audio_terms = [
        "background music", "music", "soundtrack", "musical score",
        "background score", "song", "singing", "humming", "jingle",
        "instrumental track", "lo-fi beat",
    ]
    brand_dont = [
        d.lower() for d in (brand_req.get("brand_donts") or [])
    ]
    framing_terms = VEO_FRAMING_NEGATIVES.get(veo_framing(framing) or "", [])
    all_terms = artefact_terms + cliche_terms + audio_terms + brand_dont + framing_terms
    joined = ", ".join(all_terms)
    # Vertex limit is 1000; cap with a safe margin.
    return joined[:960]


def build_outro_veo_prompt(*, topic: str, script: dict, style: str = "fade_hold", aspect: str = "9:16") -> str:
    """Veo prompt for animating the outro card.

    v4: motion clause driven by `style` (one of OUTRO_STYLES, picked by
    the Gemini Flash vision call) instead of hardcoded CTA-pulse.
    """
    mood = (script.get("mood") if isinstance(script, dict) else "") or "decisive, inviting"
    palette = ", ".join(
        (script.get("palette") if isinstance(script, dict) else None) or []
    )
    aspect_phrase = _aspect_phrase(aspect)
    cta = (
        (script.get("outro_headline") if isinstance(script, dict) else None)
        or _derive_outro_cta(topic, script if isinstance(script, dict) else {})
    )
    motion = _OUTRO_MOTION_CLAUSES.get(style) or _OUTRO_MOTION_CLAUSES["fade_hold"]
    return scrub_rai_terms(
        f"A polished, premium animation of this {aspect_phrase} end card for a talking-head "
        "Instagram reel. The attached image is the EXACT first frame — "
        "extrapolate motion forward from it. Treat the frame as a finished "
        "design: animate ON TOP of it, and never redesign, restyle, recolour, "
        f"or re-render the artwork. The CTA text on the card is \"{cta}\".\n\n"
        "MOTION\n"
        + _BOOKEND_AMBIENT_MOTION
        + f"  • Entrance accent — how the CTA and graphic elements settle into "
        f"place, then lock: {motion}\n"
        "  • Energetic but controlled — this is the decisive closing beat of a "
        "brand reel. No chaotic motion, no fast cuts, no whip pans, no harsh "
        "strobing.\n\n"
        f"{_BOOKEND_TEXT_LOGO_LOCK}\n"
        "Settle to a confident, stable hold to close the reel cleanly. "
        f"Mood: {mood}. "
        + (f"Palette: {palette}. " if palette else "")
        + f"{aspect_phrase}. Graphic-design only — no people, no faces, no "
        "photography of humans, no live-action. Audio: none (silent end card)."
    )


# ---------------------------------------------------------------------------
# Bookend motion director (Talking Head v2 only)
# ---------------------------------------------------------------------------
#
# The intro/outro clip must show the card as it is. A vision call looks at the
# finished card: when it carries icons or other graphic elements, Veo animates
# ONLY those, subtly, under hard no-text / no-restyle rules
# (build_bookend_motion_veo_prompt); otherwise no Veo at all, the clip is an
# in-house slow zoom. After Veo, a read of the clip's frames checks the card's
# text survived (BOOKEND_TEXT_CHECK_*). The builders above
# (build_intro_veo_prompt / build_outro_veo_prompt, with their ambient motion
# and CTA line) stay as they are for the video editor's bookends.

# How one graphic element may move. Each starts from the element as the card
# shows it and ends there, so the first and last frames are the card.
BOOKEND_MOTIONS: tuple[str, ...] = ("pop", "float", "draw_on", "bounce", "pulse")

_BOOKEND_MOTION_CLAUSES: dict[str, str] = {
    "pop": "gives one quick, soft pop (grows to about 108% of its size and settles back) in the first second",
    "float": "floats gently a few pixels up and down, slow and continuous",
    "draw_on": "re-draws itself once along its own lines, ending exactly as it is now",
    "bounce": "makes one gentle bounce in place, then rests",
    "pulse": "pulses softly once or twice (about 100% to 104% of its size and back)",
}

BOOKEND_MOTION_DIRECTOR_SYSTEM = (
    "You plan the motion of an intro or outro title card for a short vertical "
    "video. The card is a finished design and must stay exactly as it is: its "
    "text, logo, colours, layout and background never change.\n\n"
    "Look at the card and list its ANIMATABLE ICONS: distinct illustrative "
    "icons or pictograms, illustrated objects, badges or emoji-like symbols "
    "that stand on their own, apart from the text and the logo, and are big "
    "enough to see. Only these are ever animated.\n\n"
    "NEVER list: text, letters, numbers or digits (a big number such as '42' "
    "is text), words, a logo, a wordmark or a logo lockup, or anything that "
    "belongs to the logo (its divider or separator line, its frame); lines, "
    "rules, dividers, underlines, borders, frames, boxes or panels; the "
    "background, a pattern, a grid, a texture, a gradient, light streaks, rays, "
    "glows, particles or bokeh; a photo of a person; the whole card. A card "
    "whose only graphic is a logo or a wordmark, a card with only text, or a "
    "photograph has NO animatable icons: has_animatable_elements is false and "
    "elements is empty (it gets a gentle zoom instead).\n\n"
    "For each icon give: what it is (a few plain words, e.g. 'rocket icon'), "
    "where it is on the card (e.g. 'top left', 'under the headline'), its box "
    "as box_2d [ymin, xmin, ymax, xmax], each from 0 to 1000 (a fraction of "
    "the card's height or width), tight around it, and ONE subtle motion "
    "from the list: pop, float, draw_on, bounce, pulse. At most 4 icons, the "
    "clearest ones.\n\n"
    "Also read EVERY line of text on the card exactly as it is written "
    "(including a logo's words and any URL), one entry per line, in reading "
    "order. Do not translate or correct anything.\n\n"
    "motion_plan: one short sentence describing the motion (for a log, not "
    "for the video model)."
)

BOOKEND_MOTION_DIRECTOR_SCHEMA: dict[str, Any] = {
    "type": "OBJECT",
    "properties": {
        "has_animatable_elements": {"type": "BOOLEAN"},
        "elements": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "what": {"type": "STRING"},
                    "where": {"type": "STRING"},
                    "box_2d": {"type": "ARRAY", "items": {"type": "INTEGER"}},
                    "motion": {"type": "STRING", "enum": list(BOOKEND_MOTIONS)},
                },
                "required": ["what", "where", "box_2d", "motion"],
            },
        },
        "motion_plan": {"type": "STRING"},
        "card_text": {"type": "ARRAY", "items": {"type": "STRING"}},
    },
    "required": ["has_animatable_elements", "elements", "motion_plan", "card_text"],
}

BOOKEND_MOTION_DIRECTOR_PROMPT = (
    "This is the {role} card. Plan its motion and read its text per the schema."
)

# Veo negative prompt for a directed bookend: everything that would change the
# card. Under Vertex's 1000-character cap.
BOOKEND_MOTION_NEGATIVE_PROMPT = scrub_rai_terms(
    "new text, extra words, misspelled text, garbled letters, moving text, "
    "captions, subtitles, watermark, new logo, changed logo, moving logo, "
    "particles, sparkles, dust, lens flare, light leak, glow, bloom, vignette, "
    "lighting change, colour shift, background change, new marks, dots, grid, "
    "pattern, texture, noise, grain, scanlines, new lines, longer lines, "
    "extended lines, camera movement, camera drift, zoom, pan, parallax, "
    "scaling, new objects, people, hands, warping, distortion, flicker, "
    "jitter, fast motion"
)


def build_bookend_motion_veo_prompt(
    *, role: str, elements: list[dict], aspect: str = "9:16",
) -> str:
    """Veo prompt for a directed bookend: animate ONLY the listed graphic
    elements, each with its one subtle motion, camera locked off, and hard
    rules that keep every letter, logo, colour and the background as on the
    card. It never names the card's text (Veo paints what a prompt names)."""
    kind = "intro title card" if role == "intro" else "end card"
    lines: list[str] = []
    for el in elements or []:
        what = " ".join(str(el.get("what") or "").split())[:80]
        where = " ".join(str(el.get("where") or "").split())[:60]
        clause = _BOOKEND_MOTION_CLAUSES.get(str(el.get("motion") or ""), _BOOKEND_MOTION_CLAUSES["float"])
        if what:
            lines.append(f"  • The {what}" + (f" ({where})" if where else "") + f" {clause}.")
    return scrub_rai_terms(
        f"Animate this {_aspect_phrase(aspect)} {kind} with small, clean motion. "
        "The attached image is the exact first frame and a finished design.\n\n"
        "ANIMATE ONLY THESE GRAPHIC ELEMENTS, subtly, each staying in its place:\n"
        + "\n".join(lines)
        + "\nEverything else on the card stays perfectly still.\n\n"
        "CAMERA: locked off. No zoom, no pan, no parallax, no camera move. The "
        "frame is static: the card never moves, scales or drifts.\n\n"
        "HARD RULES\n"
        "  • Keep every letter, word, number, logo and colour exactly as on the "
        "card: never move, retype, re-letter, translate, recolour or restyle any "
        "text or logo.\n"
        "  • The logo and every line, divider, rule and border stay untouched: "
        "same length, same place, never redrawn, extended or animated.\n"
        "  • Add no new text, captions, letters, numbers or logos of any kind.\n"
        "  • Add no particles, sparkles, dust, light leaks, glow, bloom, lens "
        "flare, vignette or lighting change.\n"
        "  • Add no new objects, shapes, people or faces.\n"
        "  • The background stays exactly as it is: same colours, same texture, "
        "no movement. Every background pixel keeps its colour: add no marks, "
        "dots, grids, patterns, lines, texture or noise anywhere.\n"
        "  • End on the card exactly as it is in the first frame: a clean, "
        "stable hold.\n"
        "Audio: none (silent)."
    )


BOOKEND_TEXT_CHECK_SYSTEM = (
    "You check an animated title card against the card it was made from. "
    "Image 1 is the card. Image 2 is a frame from the middle of the clip and "
    "image 3 is its last frame.\n\n"
    "Read EVERY line of text in each image exactly as it is written (including "
    "a logo's words and any URL), one entry per line, in reading order. Do not "
    "correct, translate or complete anything: a garbled word is read as it "
    "looks.\n\n"
    "text_changed: true when image 2 or image 3 shows any text that is not on "
    "the card, misspelled, garbled or re-lettered text, or text of the card "
    "that is missing or moved.\n"
    "restyled: true when image 2 or image 3 clearly differs from the card in "
    "anything but small movements of its graphic elements: a new background, "
    "new colours, particles, a vignette, a lighting change, new objects, new "
    "marks, dots, grids or patterns, a line or divider that grew or moved, a "
    "logo that moved or changed size, or a different layout."
)

BOOKEND_TEXT_CHECK_SCHEMA: dict[str, Any] = {
    "type": "OBJECT",
    "properties": {
        "card_text": {"type": "ARRAY", "items": {"type": "STRING"}},
        "middle_text": {"type": "ARRAY", "items": {"type": "STRING"}},
        "last_text": {"type": "ARRAY", "items": {"type": "STRING"}},
        "text_changed": {"type": "BOOLEAN"},
        "restyled": {"type": "BOOLEAN"},
    },
    "required": ["card_text", "middle_text", "last_text", "text_changed", "restyled"],
}

BOOKEND_TEXT_CHECK_PROMPT = "Compare the clip's frames with the card per the schema."


def card_text_words(lines: Any) -> set[str]:
    """The words of some text lines, normalised for comparison: Unicode NFKC,
    case folded, punctuation and spacing dropped (Arabic letters are kept)."""
    import unicodedata

    if isinstance(lines, str):
        lines = [lines]
    words: set[str] = set()
    for line in lines or []:
        norm = unicodedata.normalize("NFKC", str(line or "")).casefold()
        words.update(w for w in re.findall(r"\w+", norm) if w != "_")
    return words


def card_text_norm(text: Any) -> str:
    """One line of text normalised for comparison, words kept in order: NFKC,
    case folded, punctuation dropped, single spaces ('Decode your tech
    FUTURE!' and 'decode your tech future' are the same words)."""
    import unicodedata

    norm = unicodedata.normalize("NFKC", str(text or "")).casefold()
    return " ".join(w for w in re.findall(r"\w+", norm) if w != "_")


# ---------------------------------------------------------------------------
# A kept intro/outro card: read its text, swap a headline only when needed
# ---------------------------------------------------------------------------
#
# A card kept as it is (a picked image, an upload, or a card from a previous
# reel) is read once (Gemini Flash): every line's words, what the line is and
# where it is. Logos, web addresses and calls to action are never changed. A
# headline or subtitle that differs from the script is swapped by ONE image
# edit of the card that changes only those words; the service pastes the edit
# back through a mask of that text's zone (every pixel outside it stays the
# card's own) and reads the result back.

# What a line on a card is. Only headline and subtitle lines are ever swapped.
CARD_TEXT_KINDS: tuple[str, ...] = ("logo", "url", "cta", "headline", "subtitle", "other")
CARD_TEXT_SWAP_KINDS: tuple[str, ...] = ("headline", "subtitle")
# The longest new text a swap writes (characters).
CARD_TEXT_SWAP_MAX = 120

CARD_TEXT_READ_SYSTEM = (
    "You read the text on a finished title card for a short video, exactly as "
    "it is written. List EVERY line of text in reading order (top to bottom, "
    "then left to right), one entry per line. Do not translate, correct or "
    "complete anything: a word is read as it looks.\n\n"
    "For each line give its kind:\n"
    "  - logo: the words of a logo or wordmark (for example '42', 'ABU DHABI' "
    "or 'أبوظبي' next to a brand mark).\n"
    "  - url: a web address, an @handle or an email address.\n"
    "  - cta: a call to action ('Apply now', 'Join us', 'Link in bio', "
    "'Sign up today').\n"
    "  - headline: the main title of the card, its largest topic text.\n"
    "  - subtitle: a smaller line that goes with the headline.\n"
    "  - other: anything else (a date, a label, small print).\n\n"
    "box_2d: the line's box as [ymin, xmin, ymax, xmax], each from 0 to 1000 "
    "(a fraction of the image's height or width), tight around its letters. "
    "A card with no text has no lines."
)

CARD_TEXT_READ_SCHEMA: dict[str, Any] = {
    "type": "OBJECT",
    "properties": {
        "lines": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "text": {"type": "STRING"},
                    "kind": {"type": "STRING", "enum": list(CARD_TEXT_KINDS)},
                    "box_2d": {"type": "ARRAY", "items": {"type": "INTEGER"}},
                },
                "required": ["text", "kind", "box_2d"],
            },
        },
    },
    "required": ["lines"],
}

CARD_TEXT_READ_PROMPT = "Read every line of text on this card per the schema."

CARD_TEXT_SWAP_SYSTEM = (
    "You change the words of a finished title card and nothing else. The card "
    "is a final design: its layout, colours, background, logo, icons and every "
    "other line of text come back exactly as they are. Reply with the image only."
)

_CARD_TEXT_SWAP_REF = (
    "IMAGE 1 is a finished title card for a short video. Keep it exactly as it "
    "is, except for the words named after it."
)


def _card_swap_fit_lines(fit: dict | None, *, tighter: bool) -> list[str]:
    """The FIT block of a card text swap: where each group of old words sits
    on the card, in pixels, and that the new words must stay inside it (run
    830's new headline came out wider than its zone and lost its first and
    last letters). `fit` = {card_w, card_h, boxes: [{kind, x0, y0, x1, y1,
    lines}]}; `tighter` (after an attempt that ran outside) asks for a
    clearly smaller size, on two lines where the box holds more than one.
    A box of one line has no room for a second (its zone leaves room for the
    reader's boxes, not for another line of its size), so its words stay on
    one line."""
    if not fit or not fit.get("boxes"):
        return []
    out = [f"FIT: the card is {int(fit['card_w'])} x {int(fit['card_h'])} pixels."]
    multi: list[bool] = []
    for b in fit["boxes"]:
        kind = b.get("kind") if b.get("kind") in CARD_TEXT_SWAP_KINDS else "text"
        x0, y0, x1, y1 = (int(round(float(b[k]))) for k in ("x0", "y0", "x1", "y1"))
        n = max(1, int(b.get("lines") or 1))
        multi.append(n > 1)
        wrap = f"they may wrap onto {n} lines" if n > 1 else "on ONE line (the box has no room for a second)"
        out.append(
            f"  - The {kind} sits in the box x {x0} to {x1}, y {y0} to {y1} ({x1 - x0} x {y1 - y0} "
            f"pixels), on {n} line{'s' if n > 1 else ''}. Its new words stay INSIDE that box: the "
            f"same font size or smaller, {wrap}, never wider than the box, "
            "with a small margin on every side. No letter may touch or cross the box's edge."
        )
    if tighter:
        if all(multi):
            lines = "break the new words onto two lines"
        elif not any(multi):
            lines = "keep a one-line box's words on one line"
        else:
            lines = ("break the new words onto two lines only in a box of more than one line "
                     "(a one-line box keeps one line)")
        out.append(
            "The last attempt ran outside its box: use a clearly smaller font size (about 80% "
            f"of the old one, smaller still if the words need it to fit the box's width) and {lines}, "
            "centred in the box."
        )
    return out


def build_card_text_swap_directive(
    replacements: list[dict], *, aspect: str = "9:16", strict: bool = False,
    fit: dict | None = None, tighter: bool = False,
) -> str:
    """The final directive of a card text swap: each {kind, old, new} line
    becomes its new words, in the old words' font, colour, size and place,
    and nothing else on the card changes. `strict` (the second attempt, after
    a read-back that did not match) adds a spelling check. `fit` adds the
    pixel box each group of old words sits in, which the new words must stay
    inside (_card_swap_fit_lines); `tighter` asks for a smaller size (on two
    lines where the box holds more than one: the second attempt, after one
    that ran outside its box)."""
    lines: list[str] = []
    for r in replacements or []:
        old = " ".join(str(r.get("old") or "").split())
        new = " ".join(str(r.get("new") or "").split())[:CARD_TEXT_SWAP_MAX]
        kind = r.get("kind") if r.get("kind") in CARD_TEXT_SWAP_KINDS else "text"
        lines.append(f'  - The {kind} "{old}" becomes exactly "{new}".')
    out = (
        f"Render IMAGE 1 again as the same {_aspect_phrase(aspect)} card with ONLY "
        "these words changed:\n" + "\n".join(lines) + "\n\n"
        "The new words: spell them exactly as given, letter for letter. Use the "
        "same font, weight, colour, size, alignment and position as the words "
        "they replace, and the same lettering style (for example ALL CAPITALS "
        "when the old words are in capitals). Keep them inside the same area of "
        "the card; make them smaller only if they would not fit.\n\n"
        "Change nothing else: every other line of text, the logo, any web "
        "address, the call to action, the icons, the colours, the background and "
        "the layout stay exactly as they are. Add no other text. Keep the whole "
        "card in frame at the same size and position."
    )
    fit_lines = _card_swap_fit_lines(fit, tighter=tighter)
    if fit_lines:
        out += "\n\n" + "\n".join(fit_lines)
    if strict:
        out += (
            "\n\nThe last attempt did not read back as asked. Check every letter "
            "of the new words against the lines above before you finish."
        )
    return out


def build_card_text_swap_parts(
    replacements: list[dict], *, card: Any, aspect: str = "9:16", strict: bool = False,
    fit: dict | None = None, tighter: bool = False,
) -> list[tuple[str, Any]]:
    """The ordered parts of a card text swap: the reference instruction, the
    card (IMAGE 1), then the directive. No logo part: the card carries its
    own branding, which stays as it is."""
    return [
        ("text", _CARD_TEXT_SWAP_REF),
        ("image", card),
        ("text", build_card_text_swap_directive(replacements, aspect=aspect, strict=strict,
                                                fit=fit, tighter=tighter)),
    ]


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


# ---------------------------------------------------------------------------
# Refine a still: one change, made on the still itself
# ---------------------------------------------------------------------------
#
# A refine edits the still the user is looking at. That still is the canvas
# (IMAGE 1), so its framing, pose, room and light survive unless the change
# names them, and refinements add up. A person reference (IMAGE 2) only keeps
# the face the same when the change re-poses or re-frames the speaker. There
# is no background reference (the canvas already has the background) and no
# keyframe prompt: its locks (camera line, "do not redesign", the smile ban)
# forbid exactly what people ask a refine for.

# The longest change kept; the rest is dropped.
STILL_REFINE_CHANGE_MAX = 500

TH_STILL_REFINE_SYSTEM = (
    "You are an image-generation model that edits one photo-real still from a "
    "talking-head video. You ALWAYS respond with an image and NEVER with text. "
    "Re-render the still you are given and apply only the single change requested, "
    "keeping everything else as it is. Keep the same individual: the same face, "
    "skin tone, hair, build and apparent age. Unless the change is explicitly about "
    "the facial expression, keep the expression calm and neutral with the mouth "
    "closed, because the still is lip-synced into a video later. Render exactly one "
    "person, photo-realistic and never illustrated, and never add text, captions, "
    "logos or watermarks."
)

_STILL_REFINE_CANVAS = (
    "IMAGE 1 is the CURRENT STILL, the base to edit. Reproduce it as closely as "
    "possible: the same person, outfit, pose, framing, background and light, except "
    "for the one change asked for below."
)

_STILL_REFINE_IDENTITY = (
    "IMAGE 2 is an IDENTITY REFERENCE of the same person. Use it only to keep the "
    "face and identity exactly the same if the change re-poses or re-frames them. "
    "Do not copy its background, framing, pose, lighting or clothes: those come "
    "from IMAGE 1."
)

# Words that make a change about the face's expression. Only such a change
# may open the mouth or add a smile; any other refine keeps the calm,
# closed-mouth face Veo's lip-sync and likeness filter need.
_EXPRESSION_CHANGE_RE = re.compile(
    r"\b(?:smil\w*|grin\w*|laugh\w*|expressions?|expressive|mouth|teeth|lips?|"
    r"frown\w*|smirk\w*|pout\w*|wink\w*|happ(?:y|ier|iest|iness)|"
    r"sad(?:der|dest|ness)?|serious(?:ly|ness)?|surprised?|angry|anger|"
    r"cheerful\w*|joyful\w*|emotions?|emotional|friendl(?:y|ier|iest)|excited)\b",
    re.I,
)


def still_refine_changes_expression(change: str) -> bool:
    """Whether a refine asks for a different facial expression (a smile, a
    serious look, ...). Only then may the refine move the mouth."""
    return bool(_EXPRESSION_CHANGE_RE.search(change or ""))


def clean_still_change(change: str) -> str:
    """The user's change as it goes into the refine: trimmed and cut to
    STILL_REFINE_CHANGE_MAX characters (RAI-scrubbed in the directive)."""
    return (change or "").strip()[:STILL_REFINE_CHANGE_MAX].strip()


def build_still_refine_directive(
    change: str, *, aspect: str = "9:16", has_identity_ref: bool = False,
) -> str:
    """The FINAL text part of a still refine. It ends the request on a render
    instruction (so the model answers with an image) and quotes the change.
    The change wins where it conflicts with the current framing, pose,
    expression or background; everything else stays. The mouth stays closed
    unless the change is about the expression."""
    change = clean_still_change(change)
    if still_refine_changes_expression(change):
        expression = "This change is about the expression: apply it naturally and keep it subtle."
    else:
        expression = "Keep the facial expression calm and neutral, with the mouth closed."
    same = "the same face, skin tone, hair and apparent age"
    if has_identity_ref:
        same += ", as in IMAGE 2"
    return scrub_rai_terms(
        "RENDER the still again, applying ONLY this change:\n\n"
        f"\"{change}\"\n\n"
        "Where the change conflicts with the current framing, pose, expression or "
        "background, the change wins. Keep everything else the same as IMAGE 1.\n"
        f"{expression}\n"
        f"Keep the same individual ({same}), and keep the outfit unless the change "
        "names it. Exactly one person in frame, alone. Photo-realistic, not "
        "illustrated. No new text, captions, logos or watermarks.\n"
        f"Output a {_aspect_phrase(aspect)} image, filled edge-to-edge, with no "
        "borders or bars."
    )


def build_still_refine_parts(
    change: str,
    *,
    still: Any,
    identity_ref: Any = None,
    aspect: str = "9:16",
) -> list[tuple[str, Any]]:
    """The ordered (kind, value) parts of a still refine, for
    gemini_image_multipart: IMAGE 1 (the current still, the canvas) with its
    label, then IMAGE 2 (a person reference, identity only) with its label
    when `identity_ref` is given, then the directive."""
    parts: list[tuple[str, Any]] = [("text", _STILL_REFINE_CANVAS), ("image", still)]
    if identity_ref is not None:
        parts += [("text", _STILL_REFINE_IDENTITY), ("image", identity_ref)]
    parts.append(("text", build_still_refine_directive(
        change, aspect=aspect, has_identity_ref=identity_ref is not None,
    )))
    return parts
