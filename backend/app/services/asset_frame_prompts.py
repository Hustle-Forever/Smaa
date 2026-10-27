"""Gemini prompts + JSON schemas for the on-demand frame extractor.

Separated from `asset_frame_extractor.py` so prompt review can happen
independently from algorithmic code, matching the convention in
`agents/producer/prompts.py`.

Two modes:
  - TALKING_HEAD  — detect unique persons in a video, score each frame
                    per person; pick the cleanest one per person.
  - ANIMATED_POSTER — rank candidate frames by "completeness" of the
                      rendered graphic (all text/graphics visible,
                      no mid-fade artifacts).

Both call Gemini 2.5 Flash. NEVER use Pro for response_schema= calls:
Pro burns invisible thinking tokens that count against the output
budget, causing the JSON to truncate mid-string (see CLAUDE.md gotcha).
"""
from __future__ import annotations


# ---------------------------------------------------------------------------
# Talking-head mode — single Gemini call, two-pass within it.
#
# Pass 1: enumerate distinct persons across all frames.
# Pass 2: per frame, attribute each visible person + score the face quality.
#
# Why one call (not two): Gemini holds all frames in context simultaneously,
# so it does identity matching once with full information. Anchor frames
# across separate calls drift; this doesn't.
# ---------------------------------------------------------------------------

TALKING_HEAD_SYSTEM = (
    "You are reviewing frames from a single Instagram reel that the user "
    "wants to mine for REFERENCE PORTRAIT IMAGES of the people who appear in "
    "it. Be specific and concrete. Avoid generic adjectives.\n\n"
    "Your job has two parts:\n"
    "  1. Enumerate every DISTINCT person who appears on camera across all "
    "frames. Give each one a stable label (person_A, person_B, ...) and a "
    "description that would let a reader pick them out in a lineup: "
    "gender, approximate age, distinctive clothing (kandura colour, ghutra "
    "pattern, hijab style, casual outfit), facial features (beard, glasses), "
    "and any visible name/handle if it appears as on-screen text.\n"
    "  2. For EACH frame, list which of those persons are visible and rate "
    "the FACE quality of each one (clarity, eye contact, lighting, blink, "
    "mouth state). These ratings drive selection of a single \"best portrait\" "
    "frame per person.\n\n"
    "If the video has NO visible speakers (voiceover-only b-roll, animated "
    "graphics, text-on-screen reel), set has_visible_speakers=false and "
    "leave persons + frames empty. Also indicate dominant_content so the "
    "frontend can offer a fallback path.\n\n"
    "If a person appears but their face is consistently obscured, omit them "
    "from `persons` — we cannot make a portrait reference from an unseen face."
)


# Gemini response_schema dialect uses uppercase types ('STRING', 'INTEGER',
# 'NUMBER', 'BOOLEAN', 'OBJECT', 'ARRAY').
TALKING_HEAD_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "has_visible_speakers": {
            "type": "BOOLEAN",
            "description": "False if no human face is on camera in any frame.",
        },
        "dominant_content": {
            "type": "STRING",
            "description": (
                "Pick one: talking_head | b_roll | text_overlay | mixed"
            ),
        },
        "persons": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "label": {
                        "type": "STRING",
                        "description": "Stable identifier, e.g. person_A.",
                    },
                    "description": {
                        "type": "STRING",
                        "description": (
                            "One sentence: gender, age estimate, distinctive "
                            "clothing/features. Concrete only."
                        ),
                    },
                    "suggested_archetype": {
                        "type": "STRING",
                        "description": (
                            "Best match from: emirati_male | emirati_female | "
                            "casual_male | casual_female | none. Pick 'none' "
                            "if you cannot tell."
                        ),
                    },
                },
                "required": ["label", "description"],
            },
        },
        "frames": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "frame_index": {
                        "type": "INTEGER",
                        "description": "0-based index matching the input frame order.",
                    },
                    "visible_persons": {
                        "type": "ARRAY",
                        "items": {
                            "type": "OBJECT",
                            "properties": {
                                "label": {"type": "STRING"},
                                "face_clarity": {
                                    "type": "NUMBER",
                                    "description": "0.0 (very blurry) to 1.0 (sharp).",
                                },
                                "looking_at_camera": {
                                    "type": "NUMBER",
                                    "description": (
                                        "0.0 (looking away / profile) to 1.0 "
                                        "(direct camera gaze)."
                                    ),
                                },
                                "lighting": {
                                    "type": "NUMBER",
                                    "description": "0.0 (harsh shadow/backlit) to 1.0 (even, flattering).",
                                },
                                "mid_blink": {
                                    "type": "BOOLEAN",
                                    "description": "True if eyes are closed or mid-blink.",
                                },
                                "mid_speech_natural": {
                                    "type": "NUMBER",
                                    "description": (
                                        "0.0 (awkward mouth shape, mid-vowel "
                                        "grimace) to 1.0 (closed mouth or "
                                        "natural speaking pose)."
                                    ),
                                },
                            },
                            "required": ["label"],
                        },
                    },
                },
                "required": ["frame_index"],
            },
        },
    },
    "required": ["has_visible_speakers", "dominant_content"],
}


def talking_head_user_prompt(frame_count: int) -> str:
    return (
        f"You are shown {frame_count} numbered frames from a single short "
        f"Instagram reel, in capture order. Frame 0 is first, frame "
        f"{frame_count - 1} is last.\n\n"
        f"Return the structured analysis exactly per the schema. Be honest: "
        f"if every visible person has a poor portrait frame, that's fine — "
        f"the downstream picker will surface the least-bad one."
    )


# ---------------------------------------------------------------------------
# Animated-poster mode — ranking instead of scoring.
#
# Gemini Flash compresses absolute scores to 0.7-0.9 with low variance, so
# asking for a numeric score per frame is unreliable. Forcing a ranked list
# discriminates much better, and the categorical confidence tells the UI
# whether to auto-select or defer to the user.
# ---------------------------------------------------------------------------

ANIMATED_POSTER_SYSTEM = (
    "You are reviewing frames from an animated Instagram poster (graphics, "
    "text, sometimes light motion, no humans on camera). The user wants to "
    "extract ONE STILL IMAGE that captures the poster in its most COMPLETE "
    "rendered state — all text fully visible and legible, all graphic "
    "elements present, no mid-fade transition, no motion blur.\n\n"
    "Common failure modes to penalise:\n"
    "  - Text is still fading in (low opacity, partial reveal)\n"
    "  - A graphic element hasn't entered the frame yet or has exited\n"
    "  - Motion blur from a sweeping camera or kinetic typography mid-move\n"
    "  - Outro fade-to-black or fade-to-logo before the full message lands\n\n"
    "Return a RANKED list of frame indices, best first. Then set a single "
    "categorical confidence:\n"
    "  - high: there's a clear winner that's obviously the fullest render\n"
    "  - medium: a couple of frames look equally complete; user judgement helps\n"
    "  - low: no frame is clearly complete (e.g. heavy motion throughout)\n\n"
    "Be ruthless with rejection — list any frame that's clearly NOT a "
    "viable still in `rejected_with_reason` with a short reason."
)


ANIMATED_POSTER_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "ordered_ranking": {
            "type": "ARRAY",
            "items": {"type": "INTEGER"},
            "description": (
                "Frame indices ranked best-first. Include every frame at "
                "least once unless it's in rejected_with_reason."
            ),
        },
        "confidence": {
            "type": "STRING",
            "description": "Pick one: high | medium | low",
        },
        "rejected_with_reason": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "frame_index": {"type": "INTEGER"},
                    "reason": {
                        "type": "STRING",
                        "description": "≤10 words, e.g. 'text fading in', 'mid-transition wipe'.",
                    },
                },
                "required": ["frame_index", "reason"],
            },
        },
    },
    "required": ["ordered_ranking", "confidence"],
}


def animated_poster_user_prompt(frame_count: int) -> str:
    return (
        f"You are shown {frame_count} numbered frames from a single short "
        f"animated Instagram poster, in capture order. Frame 0 is first, "
        f"frame {frame_count - 1} is last.\n\n"
        f"Pick the frame that best captures the poster in its FULLEST "
        f"rendered state. Return the structured ranking exactly per the "
        f"schema."
    )


# ---------------------------------------------------------------------------
# Intro / Outro mode — gallery of text-card / title-card / end-card frames.
#
# Same scorer shape as talking_head's frame scoring (per-frame numeric rubric)
# but optimised for ON-SCREEN TEXT and GRAPHIC COMPLETENESS, not faces. The
# downstream UI renders the gallery sorted by text_completeness DESC so the
# "all text together" frame surfaces first, with intermediate "text appearing
# separately" frames behind it.
# ---------------------------------------------------------------------------

def _intro_outro_system(position: str) -> str:
    """Build the system prompt for either 'intro' (opening) or 'outro' (closing)."""
    where = "OPENING / first few seconds" if position == "intro" else "CLOSING / final few seconds"
    examples = (
        "title cards, the post's headline overlay, the hook text, brand-mark "
        "intros, animated typography reveals"
        if position == "intro"
        else
        "end cards, CTAs ('follow for more', 'apply now', 'link in bio'), "
        "credits, swipe-up prompts, brand sign-offs, hashtag stacks"
    )
    return (
        f"You are reviewing frames sampled from the {where} of a single "
        f"Instagram reel. The user wants to mine FULL TEXT / TITLE-CARD / "
        f"GRAPHIC frames — examples: {examples}. They do NOT want speaker "
        f"face frames; those go through a separate flow.\n\n"
        f"For EACH frame, return a per-frame score on three axes:\n"
        f"  - text_completeness (0..1): how fully the on-screen text is "
        f"composed and settled. 1.0 = every word visible and finished "
        f"rendering, no fade-in/out, no characters still typing in. 0.5 = "
        f"some text visible but still animating in / partially revealed. "
        f"0.0 = no readable text, or text barely begun fading in.\n"
        f"  - visual_dominance (0..1): how much the frame's composition is "
        f"DOMINATED by text/graphics. 1.0 = text/graphics fill the frame, "
        f"no speaker face. 0.5 = mix of text and face. 0.0 = pure speaker "
        f"shot or unrelated b-roll.\n"
        f"  - clarity (0..1): no motion blur, no compression artifacts, "
        f"sharp legible typography.\n\n"
        f"Also write a short `note` (≤15 words) describing what's on screen "
        f"in plain English — e.g. 'headline: 42 ABU DHABI · CODING SCHOOL' "
        f"or 'CTA: APPLY NOW with arrow icon'.\n\n"
        f"Return an `ordered_ranking` (best first) where 'best' = highest "
        f"text_completeness, tiebroken by visual_dominance then clarity. "
        f"Drop frames in `rejected_with_reason` when they are pure black, "
        f"mid-fade with nothing readable, motion-blurred to illegibility, "
        f"or completely face-dominant — these aren't usable text references.\n\n"
        f"Set categorical confidence:\n"
        f"  - high: at least one frame has text_completeness ≥ 0.85\n"
        f"  - medium: best frame has text_completeness between 0.5 and 0.85\n"
        f"  - low: no frame has readable composed text (still rank what's "
        f"there, but the UI may show a soft empty state).\n\n"
        f"Be honest. If the {position} is just a speaker face with no text, "
        f"return confidence=low and reject everything — that's the right "
        f"answer, not a forced ranking."
    )


INTRO_SYSTEM = _intro_outro_system("intro")
OUTRO_SYSTEM = _intro_outro_system("outro")


INTRO_OUTRO_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "ordered_ranking": {
            "type": "ARRAY",
            "items": {"type": "INTEGER"},
            "description": (
                "Frame indices ranked best-first by text_completeness "
                "(tiebreak: visual_dominance, then clarity)."
            ),
        },
        "confidence": {
            "type": "STRING",
            "description": "Pick one: high | medium | low",
        },
        "rejected_with_reason": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "frame_index": {"type": "INTEGER"},
                    "reason": {
                        "type": "STRING",
                        "description": (
                            "≤12 words. e.g. 'pure black fade-in', "
                            "'speaker face only, no text', "
                            "'text mid-typewriter animation'."
                        ),
                    },
                },
                "required": ["frame_index", "reason"],
            },
        },
        "frames": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "frame_index": {
                        "type": "INTEGER",
                        "description": "0-based index matching the input order.",
                    },
                    "text_completeness": {
                        "type": "NUMBER",
                        "description": "0.0 (no text / mid-fade) to 1.0 (fully composed).",
                    },
                    "visual_dominance": {
                        "type": "NUMBER",
                        "description": "0.0 (no text/graphics) to 1.0 (text fills frame).",
                    },
                    "clarity": {
                        "type": "NUMBER",
                        "description": "0.0 (blurry/illegible) to 1.0 (sharp).",
                    },
                    "note": {
                        "type": "STRING",
                        "description": "≤15 words. What's on screen, plain English.",
                    },
                },
                "required": ["frame_index"],
            },
        },
    },
    "required": ["ordered_ranking", "confidence"],
}


def intro_outro_user_prompt(position: str, frame_count: int) -> str:
    where = "OPENING" if position == "intro" else "CLOSING"
    return (
        f"You are shown {frame_count} numbered frames from the {where} of a "
        f"short Instagram reel, in capture order. Frame 0 is earliest, "
        f"frame {frame_count - 1} is latest.\n\n"
        f"Score each frame on text_completeness / visual_dominance / "
        f"clarity, then return the structured ranking exactly per the "
        f"schema. Frames that are pure speaker-face or pure black go in "
        f"rejected_with_reason."
    )
