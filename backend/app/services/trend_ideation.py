"""Trend Finder → Producer: turn one competitor post into a ready-to-use brief.

The "Make this" button. The scorer already extracted a transferable ANGLE
(trend_scoring), but the Producer tools each want a DIFFERENT shape of text — a
poster brief is not a Veo prompt is not a casting brief. This module asks the
model for the destination tool's shape specifically, so the user lands in the
creator with the box already filled instead of with something on the clipboard.

One post, one tool, one call. Batching all eight tools into a single response is
what makes 2.5-pro truncate: it spends thinking tokens out of the same
max_output_tokens pool (see trend_research:154-163).

This module also owns ``PLAIN_LANGUAGE_RULE`` — the single copy of the
write-for-a-stranger rule, imported by every other prompt that produces words
the audience will read. See the comment above the constant for why it exists.

And it owns the BRAINSTORM LAYER — three genuinely different ideas offered
BEFORE anyone commits to a brief. Nobody wants a single take handed to them as
though it were the only one; the first version of "Make this" jumped straight
to a filled-in box and there was no way to say "no, more like this". That step
was written for General trends and lived in ``general_trend_ideation``, but it
was never about trends: a competitor post and a news story deserve the same
three-ideas-then-pick. So it lives here, in the module both paths already
share, parameterised by the tool set (a news story may only be made four ways —
see ``NEWS_TOOLS``). ``general_trend_ideation`` now imports these names instead
of keeping a second copy of them.
"""
from __future__ import annotations

import logging
from typing import Any, Optional, Sequence

from . import trend_scoring
from . import trend_store as store
from .vertex_client import VertexBudgetExceededError, gemini_text

log = logging.getLogger("archivist.trend_ideation")

# The closed set of Producer tool keys. Mirrors RoadmapTool in
# frontend/src/components/yas/roadmap/types.ts:40-48 — keep the two in step.
TOOLS = (
    "poster", "animation", "poster_human", "talking_head_v2",
    "video_editor", "human_character", "general_video", "broll",
)


# A news story is offered FOUR of the eight. The missing four are not merely a
# worse fit for a story — each is actively wrong for one, which is why this is a
# hard tool set and not a ranking (the frontend card offers the same four, see
# frontend/src/components/trends/MakeThisDialog.tsx:42-59):
#
#   general_video    a Veo clip depicting a real news event is a factual-accuracy
#                    hazard on a school's public account. The one exclusion that
#                    is about risk rather than fit.
#   broll            needs a physical shoot, which defeats the timeliness that
#                    makes a news post worth making at all.
#   human_character  writes a CASTING brief, not a post.
#   video_editor     has no brief box — only a project name would arrive.
NEWS_TOOLS = ("poster", "poster_human", "talking_head_v2", "animation")

MAX_DIALOGUE_LINES = 3   # TalkingHeadV2ScenesEditor.MAX_SCENES is 5; 3 is plenty for a reel.


def tools_for(post: dict) -> tuple[str, ...]:
    """Which Producer tools this source may be turned into.

    Read by every step that names a tool — the brainstorm's schema enum, the
    parser's clamp, the tool menu inside the prompt, and ``generate_prefill``'s
    own guard. The last one is the one that matters: three of the four
    exclusions are about fit, but ``general_video`` on a news story is a
    factual-accuracy hazard, and a safety rule that only the picker applies is
    not a rule at all. ``/posts/{id}/ideas`` takes the tool as a bare string
    from the client, so the brief call has to check it against THIS post's set
    rather than against all eight."""
    return NEWS_TOOLS if (post.get("source") or "competitor") == "news" else TOOLS


class IdeationUnavailable(RuntimeError):
    """The model can't be reached at all — billing off, missing credentials, or
    the Vertex budget cap. A configuration problem rather than a transient one,
    so the router turns it into a plain-language 422 the user can act on instead
    of a 502 they'll just retry."""


# THE PLAIN-LANGUAGE RULE — ONE rule, one copy, shared by every prompt that
# writes words a stranger will read: this module's brief prompt, the general
# trends scorer's `suggested_angle` (general_trends_pipeline._SYSTEM) and the
# general-trend ideation prompt (general_trend_ideation._SYSTEM, which imports
# this constant). It lives here because general_trend_ideation already imports
# _TOOL_BRIEFS / schema_for / parse_prefill from this module, so this is the
# one file both ideation paths already share.
#
# WHY: the owner was shown a generated post that opened "I just saw the
# announcement that the Norminette..." — the Norminette is 42's internal code
# linter and nobody outside the school has heard of it. That phrasing was born
# in the scorer's suggested_angle and flowed straight into the brief, so the
# rule has to be in BOTH prompts. Never reflow or paraphrase this block: four
# near-identical variants is how a rule stops being one rule.
PLAIN_LANGUAGE_RULE = (
    "WRITE FOR SOMEONE WHO HAS NEVER HEARD OF 42.\n"
    "The reader is a young person in the UAE scrolling Instagram. They do not know\n"
    "what 42 Abu Dhabi is. They have never written a line of code. They do not know\n"
    "what the Piscine is, what the Norminette is, what a cluster is, or what\n"
    "peer-to-peer learning means — and they will not look it up. They will scroll on.\n"
    "- NEVER use an insider word as if everyone knows it. A post that opens \"I just\n"
    "  saw the announcement that the Norminette...\" has already lost: nobody outside\n"
    "  the building knows what that is.\n"
    "- If an insider word really is the point, EXPLAIN IT IN THE SAME BREATH, inside\n"
    "  the post itself: \"the Norminette — the robot that fails your code over one\n"
    "  space in the wrong place\".\n"
    "- Prefer what anyone recognises: staying up too late, being bad at something and\n"
    "  then getting good, failing and going again, friends who drag you through it,\n"
    "  something that costs nothing, getting a job at the end.\n"
    "- The joke has to land for someone who cannot code. If getting it requires\n"
    "  knowing what a compiler is, it is the wrong joke.\n"
    "- Plain words over clever ones. No hashtags, no emoji."
)

# The school, described the way the rule above demands — every fact stated in
# words a stranger already owns. It came from the general-trends path, which
# wrote it after noticing that the older blurb (still in ``_SYSTEM`` below) says
# "peer-to-peer" and "an intensive month called the Piscine": it hands the model
# two of the exact words the rule bans and then asks it not to use them. Every
# NEW prompt takes this one. ``_SYSTEM`` keeps its own wording because a
# refactor is the wrong place to change a prompt that is already in production.
SCHOOL = (
    "42 Abu Dhabi is a coding school in Abu Dhabi, UAE. It is free — no tuition, "
    "no fees. You can get in from 18 with no diploma and no coding experience. "
    "There are no teachers and no lectures: students learn by building projects "
    "and by correcting each other's work, at their own hours, in a lab that is "
    "open around the clock. Admission is a month-long, full-time selection test "
    "on campus. Graduates come out hireable."
)

_SYSTEM = (
    "You turn a competitor's high-performing Instagram post into a brief that "
    "42 Abu Dhabi's social team can execute. 42 Abu Dhabi is a tuition-free, "
    "peer-to-peer, project-based coding school in Abu Dhabi, UAE — students "
    "learn without teachers or lectures, admission runs through an intensive "
    "month called the Piscine, and no prior coding experience is required.\n\n"
    # The audience frame comes BEFORE the mechanics: everything below is about
    # WHICH fields to fill, and none of it is worth anything if the words in
    # them only make sense to someone who works at the school.
    + PLAIN_LANGUAGE_RULE + "\n\n"
    "Rules:\n"
    "- Write 42's OWN version of the idea. Never describe the competitor's post "
    "and never name the competitor.\n"
    "- Concrete beats clever: name the setting, the people and the action, the "
    "way a marketer would type it into a brief box.\n"
    "- For Poster + Human, the people in frame are Emirati students by default "
    "(a kandura, or an abaya and sheila). Say so in the brief unless the idea "
    "genuinely calls for someone else.\n"
    "- No hashtags, no emoji, no camera jargon unless the tool asks for it.\n"
    "- Fill ONLY the fields the requested tool asks for. Leave the rest out."
)

_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "title": {"type": "string",
                  "description": "the idea's short name, <= 60 chars, "
                                 "e.g. '42 AD Compile Cup'"},
        "topic": {"type": "string",
                  "description": "the brief that goes in the tool's main text box"},
        "dialogue": {"type": "array", "items": {"type": "string"},
                     "description": "talking_head_v2 ONLY: one spoken line per "
                                    "scene, 20-25 words each, at most 3"},
        "intro_topic": {"type": "string",
                        "description": "broll ONLY: one line for the animated intro card"},
        "outro_topic": {"type": "string",
                        "description": "broll ONLY: one line for the animated outro card"},
        "character": {"type": "string",
                      "description": "human_character ONLY: a casting brief "
                                     "describing ONE person"},
        "notes": {"type": "string",
                  "description": "one short sentence on why this idea works"},
    },
    "required": ["title", "topic"],
}

# The field each tool's prefill genuinely depends on. Naming it in `required`
# is what actually gets it produced: left optional, the model quietly drops it —
# a live human_character request came back with NO `character` at all, and its
# `topic` was a scene description ("a student sits at their iMac…"), which is
# the one thing a casting box must never contain. Only tools with a real
# dependency appear here; everything else uses the base schema unchanged.
_TOOL_REQUIRED: dict[str, tuple[str, ...]] = {
    "talking_head_v2": ("dialogue",),
    "broll": ("intro_topic", "outro_topic"),
    "human_character": ("character",),
}


def schema_for(tool: str) -> dict:
    """Pure — the base schema with that tool's must-have fields added to
    `required`. The tests drive this directly."""
    extra = _TOOL_REQUIRED.get(tool, ())
    if not extra:
        return _SCHEMA
    return {**_SCHEMA, "required": [*_SCHEMA["required"], *extra]}

# What each Producer tool actually wants in its boxes. The frontend seeder maps
# these fields onto the creator's real state, so the wording here and the seeder
# have to agree — a "topic" that is a post brief would be actively wrong in
# human_character, which expects a CASTING brief.
_TOOL_BRIEFS: dict[str, str] = {
    "poster": (
        "TOOL: Poster — one static, designed brand poster with NO human faces.\n"
        "`topic`: 1-2 sentences on what the post says and who it is for, the way "
        "a marketer types it into a brief box. Name the headline idea, in words "
        "a stranger already knows. No camera or shot language."
    ),
    "poster_human": (
        "TOOL: Poster + Human — a designed poster WITH real student faces in it.\n"
        "`topic`: 1-2 sentences on what the post says, in words a stranger "
        "already knows, plus who is in frame and what they are doing. The people "
        "in frame are Emirati students by default: say so in the brief, an "
        "Emirati young man in a kandura or an Emirati young woman in an abaya "
        "and sheila, unless the idea genuinely calls for someone else."
    ),
    "animation": (
        "TOOL: Animation — a 4-8 second animated intro/outro clip, no dialogue.\n"
        "`topic`: describe the MOTION and the on-screen words, e.g. 'a scrunched "
        "paper ball arcs across a dark studio into a bin, freeze-frame, the words "
        "COMPILE CUP snap on in electric cyan'. The on-screen words get read in "
        "about a second by someone who has never heard of 42 — no insider term "
        "can carry the line."
    ),
    "talking_head_v2": (
        "TOOL: Talking Head — one person on camera, ~8 seconds per scene.\n"
        "`topic`: one sentence naming the subject.\n"
        "`dialogue`: 1 to 3 spoken lines, one per scene, in order, 20-25 words "
        "each, first person, the way a student actually talks. The FIRST line "
        "decides whether anyone keeps watching, so it must make sense to someone "
        "who has never heard of 42 — never open on an unexplained insider word."
    ),
    "general_video": (
        "TOOL: General AI Video — a text-to-video (Veo) prompt.\n"
        "`topic`: ONE paragraph in the present tense covering subject, setting, "
        "action, lighting and camera move. No spoken dialogue."
    ),
    "broll": (
        "TOOL: B-roll — real footage the team will shoot themselves, then join.\n"
        "`topic`: a 3-5 shot list, one shot per line, each shootable on a phone "
        "at the 42 campus.\n"
        "`intro_topic`: one short line for the animated intro card, in plain "
        "words a stranger reads at a glance.\n"
        "`outro_topic`: one short line for the animated outro card (a call to "
        "action), in the same plain words."
    ),
    "human_character": (
        "TOOL: Human Character — generates ONE reusable AI person, NOT a post. "
        "This box expects a CASTING brief, so a post topic would be wrong here.\n"
        "`character`: age, gender, ethnicity, clothing, expression, setting — one "
        "believable person who could front this idea for 42 Abu Dhabi.\n"
        "`topic`: the same casting brief condensed to one sentence."
    ),
    "video_editor": (
        "TOOL: Video Editor — a multi-track timeline. It has NO brief box: the "
        "user starts from footage they already have.\n"
        "`title`: a short project name (<= 60 chars).\n"
        "`topic`: a plain-language edit plan — the cuts, the order, the text "
        "overlays — that the user can paste into the editor's AI chat once the "
        "project exists."
    ),
}


# =========================================================================== #
# The brainstorm layer — three ideas, then the team picks one
#
# Shared by all three sources (competitor post, news story, General trend). The
# ONLY thing that varies between them is which tools may be suggested, so every
# function here takes a ``tools`` tuple rather than reaching for TOOLS.
# =========================================================================== #

IDEAS_PER_ROUND = 3

# One idea's clamps. 70 chars is a title that still fits on a card at 320px;
# the shot and the reason are prose, so they are clamped for sanity only.
MAX_IDEA_TITLE = 70
MAX_IDEA_SHOT = 400
MAX_IDEA_WHY = 300
MAX_FEEDBACK = 600
MAX_EXCLUDE = 12

# What the frontend's format pill can show. Mirrors the scorer's
# `suggested_format` enum (general_trends_pipeline) — keep the two in step.
IDEA_FORMATS = ("reel", "carousel", "static")


def brainstorm_system(*, opening: str, rules: Sequence[str] = (),
                      bullets: Sequence[str] = (), subject: str) -> str:
    """The brainstorm call's system prompt, built the same way for every source.

    Four things vary and all four are named here: ``opening``, the one sentence
    that says what is on the table (a rival's post or a story in the news for
    this module, an internet trend for ``general_trend_ideation``); ``rules``,
    the blocks only one source needs (the General path adds NAME_IS_A_LABEL_RULE
    and CONNECT_TO_42_RULE); ``bullets``, its own extra "Also:" lines; and
    ``subject``, the noun the do-not-mock rule uses for the thing being ridden.

    Everything else is shared rather than restated, for the reason
    PLAIN_LANGUAGE_RULE gives above: near-identical variants are how a rule
    stops being one rule. The school blurb and the two bullets below were
    already word-for-word identical in both modules, so tightening either one
    would have applied to half the product with nothing to say so.
    """
    return "\n\n".join([
        opening,
        SCHOOL,
        # The audience frame comes before the mechanics, exactly as in the brief
        # prompt: three ideas nobody outside the building understands are three
        # wasted ideas.
        PLAIN_LANGUAGE_RULE,
        *rules,
        "Also:\n" + "\n".join([
            *bullets,
            "- Every idea must be shootable with what a school already has: its own "
            "students, their phones, the campus, a computer lab, whatever is already on "
            "a desk. No talent fees, no drone, no location permits, nothing to buy.",
            # The owner's standing call for the human poster: the faces on it are
            # Emirati unless the idea needs otherwise. Shared here so both feeds
            # (this module and general_trend_ideation) say it in the same words.
            "- When an idea is a Poster + Human, the people on it are Emirati "
            "students by default: write `what_you_see` with an Emirati young man in "
            "a kandura or an Emirati young woman in an abaya and sheila, unless the "
            "idea genuinely calls for someone else.",
            f"- Never mock a real person, a tragedy or a community. If the {subject} is "
            "sensitive, ride the FORMAT, not the subject.",
        ]),
    ])


_BRAINSTORM_SYSTEM = brainstorm_system(
    opening=(
        "You are the ideas person in the room when 42 Abu Dhabi's social team looks "
        "at something that worked for somebody else — a rival account's post, or a "
        "story in the news — and asks \"so what would OUR version be?\". You answer "
        "with three DIFFERENT ideas, not one idea worded three ways."
    ),
    bullets=(
        "- Write 42's OWN idea. Never describe the source, never name the account it "
        "came from, and never write as though 42 is the one reporting the news.",
    ),
    subject="source",
)


def tool_menu(tools: Sequence[str] = TOOLS) -> str:
    """The tool list, one line each, taken from the FIRST line of the tool's own
    brief so the menu can never drift from what the brief actually asks for."""
    lines = []
    for tool in tools:
        head = _TOOL_BRIEFS[tool].split("\n", 1)[0]
        lines.append(f"  {tool}: {head.split('TOOL:', 1)[-1].strip()}")
    return "\n".join(lines)


# Built once per tool set. Identity matters: a caller may hold on to the object
# (the General path exports it as ``_IDEAS_SCHEMA``), and rebuilding a fresh
# dict on every call would make `is` comparisons quietly false.
_IDEAS_SCHEMAS: dict[tuple[str, ...], dict] = {}


def ideas_schema(tools: Sequence[str] = TOOLS) -> dict:
    """The brainstorm response schema, with ``tools`` as the `tool` enum.

    EVERY field required — Gemini silently drops optional ones (measured on the
    scorer, general_trends_pipeline:1625). `why_it_works` is deliberately FIRST:
    structured output is autoregressive, so making the model argue why a stranger
    would stop scrolling BEFORE it names the idea keeps the title honest instead
    of letting it justify a clever name after the fact."""
    key = tuple(tools)
    cached = _IDEAS_SCHEMAS.get(key)
    if cached is not None:
        return cached
    schema: dict = {
        "type": "object",
        "properties": {
            "ideas": {
                "type": "array",
                "description": f"exactly {IDEAS_PER_ROUND} ideas, different in kind",
                "items": {
                    "type": "object",
                    "properties": {
                        "why_it_works": {
                            "type": "string",
                            "description": "ONE sentence: why someone who has never "
                                           "heard of 42 stops scrolling on this",
                        },
                        "title": {
                            "type": "string",
                            "description": f"the idea's name, at most {MAX_IDEA_TITLE} "
                                           "chars, plain words, no insider jargon",
                        },
                        "what_you_see": {
                            "type": "string",
                            "description": "1-2 sentences describing THE SHOT: who is "
                                           "on screen, what happens, what the words "
                                           "on screen or the spoken line say",
                        },
                        "tool": {"type": "string", "enum": list(key)},
                        "format": {"type": "string", "enum": list(IDEA_FORMATS)},
                    },
                    "required": ["why_it_works", "title", "what_you_see", "tool", "format"],
                },
            },
        },
        "required": ["ideas"],
    }
    # Belt and braces: some SDK schema converters rebuild the properties map and
    # lose Python's insertion order, which would undo the reason-before-name fix.
    schema["properties"]["ideas"]["items"]["propertyOrdering"] = list(
        schema["properties"]["ideas"]["items"]["properties"])
    _IDEAS_SCHEMAS[key] = schema
    return schema


def ideas_brief(*, ask: str, framing: Sequence[str], subject: str,
                straight: str, flipped: str, tools: Sequence[str],
                tool_note: str = "") -> str:
    """The ask, assembled from the paragraphs every source shares.

    What differs between a trend, a rival's post and a news story is small and
    named: the ``ask`` itself, the ``framing`` paragraphs that say what the
    thing is and how 42 is allowed to touch it, the noun for it, the STRAIGHT
    and FLIPPED bullets — whose wording only makes sense against the thing in
    front of them — and an optional ``tool_note``. The three-way spread, the
    shot-not-concept paragraph, the stranger test, the tool menu and `format`
    are one paragraph each, not one per caller.

    Written this way because the shot-not-concept paragraph is the entire reason
    this block exists and it was living in two files at once: tune it here and a
    General trend would have kept the old wording, so one step would have
    started producing two shapes of idea with nothing failing to say so.
    """
    parts: list[str] = [ask, *framing]
    parts.append(
        "The three must differ in KIND, not in wording. Unless this particular "
        f"{subject} makes a better spread obvious, use this one:\n"
        f"  1. STRAIGHT — {straight}\n"
        f"  2. FLIPPED — {flipped}\n"
        "  3. CHEAPEST — the lowest-effort version that still works: one "
        "student, one phone, one take, filmed in a corridor tonight, no set-up "
        "and no crew.\n"
        "Three variations of the same idea is a failed answer."
    )
    parts.append(
        "`what_you_see` is THE SHOT, not the concept. Who is on screen, what "
        "they do, what the on-screen text or the spoken line actually says, in "
        "order. A marketer has to be able to hand it to a student and have them "
        "film it without asking a single question. \"Show our community spirit\" "
        "is not a shot; \"a student asleep on a keyboard, a friend slides a "
        "karak next to her head, she wakes up and keeps typing\" is."
    )
    parts.append(
        "`why_it_works` is aimed at a stranger. Not \"it showcases our culture\" "
        "— what makes the thumb stop."
    )
    menu = ("`tool` is the Producer tool that will actually make this. Pick the "
            "one that suits the idea:\n" + tool_menu(tools))
    if "human_character" in tools or "video_editor" in tools:
        menu += ("\n  Note: human_character produces one reusable AI person and "
                 "video_editor opens an empty timeline — neither is a post, so "
                 "pick them only if the idea genuinely is that.")
    if tool_note:
        menu += "\n  " + tool_note
    parts.append(menu)
    parts.append(
        "`format`: reel for anything with motion, carousel for a set of images, "
        "static for one image."
    )
    return "\n\n".join(parts)


def _ideas_brief(post: dict, tools: Sequence[str]) -> str:
    """The shared ask, framed for the source in front of it.

    A story and a rival's post get the same three-way spread but a different
    opening sentence, and that sentence is load-bearing: 42 is REACTING to a
    story it had nothing to do with, and BORROWING what worked for an account it
    must never name. Blur the two and a school account starts sounding like it
    reports the news."""
    news = (post.get("source") or "competitor") == "news"
    if news:
        framing = (
            "42 is REACTING to this story, not reporting it. Each idea is 42's "
            "own post — what the story makes a student think, argue about or "
            "laugh at. Never write it as though 42 broke the news, and state no "
            "fact the story above did not state.",
        )
        # Said out loud so the model does not "helpfully" name a tool that is
        # not on the menu — see NEWS_TOOLS for why each missing one is unsafe.
        note = ("These are the only tools a news story may use: a generated "
                "video of a real event is a factual-accuracy hazard, and "
                "anything that needs a shoot is too slow to still be news.")
    else:
        framing = (
            "Take the IDEA, not the post. Keep whatever made it work — the "
            "format, the hook, the joke structure — and put 42's students, its "
            "campus and its free, learn-from-each-other way of studying inside "
            "it. Never describe the source post and never name the account it "
            "came from.",
        )
        note = ""
    return ideas_brief(
        ask=(f"GIVE THE TEAM {IDEAS_PER_ROUND} IDEAS FOR WHAT 42 ABU DHABI SHOULD "
             f"POST OFF THE BACK OF THIS {'STORY' if news else 'POST'}."),
        framing=framing,
        subject="story" if news else "post",
        straight=("the obvious version, on campus, with 42 students. Whatever "
                  "made the original land is still instantly recognisable; only "
                  "the people and the place change."),
        flipped=("subvert it. Invert the punchline, play the other side of the "
                 "joke, or take the opposite position and let 42 answer back."),
        tools=tools,
        tool_note=note,
    )


def brainstorm_block(*, feedback: str = "",
                     exclude: Optional[Sequence[str]] = None) -> str:
    """What the team said after reading a round: what they turned down, and what
    they wanted instead. Empty string on a first round, so the prompt is then
    exactly what it would have been without a picker at all.

    The feedback goes LAST, right before the ask: it is the instruction the model
    is most likely to actually follow, and it was typed precisely because the
    round it is replacing was wrong."""
    blocks: list[str] = []

    seen: list[str] = []
    for raw_title in (exclude or []):
        title = " ".join(str(raw_title or "").split())[:MAX_IDEA_TITLE]
        if title and title.lower() not in [s.lower() for s in seen]:
            seen.append(title)
    if seen:
        blocks.append("ALREADY SHOWN AND TURNED DOWN — do not offer these again, and "
                      "do not offer a reworded version of them:\n"
                      + "\n".join("  - " + t for t in seen[:MAX_EXCLUDE]))

    note = " ".join(str(feedback or "").split())[:MAX_FEEDBACK]
    if note:
        blocks.append(
            "WHAT THE TEAM ASKED FOR INSTEAD (they read the last three ideas and "
            "wanted none of them):\n  " + note + "\n\n"
            "This is the strongest instruction on this page. It beats everything "
            "above it — the source, the scorer's suggested angle, and your own "
            "taste. If it contradicts anything above, follow IT. If it names a "
            "format, a person, a place or a joke, build all three ideas around "
            "that."
        )
    return "\n\n".join(blocks)


def idea_block(idea: dict) -> str:
    """The picked idea, rendered as an order rather than a suggestion.

    The idea's own ``tool``/``format`` are deliberately NOT printed: the caller
    already chose the destination tool (they may have overridden the suggestion
    in the picker) and the tool brief immediately below dictates the shape. Two
    contradicting format instructions in one prompt is how you get a poster
    brief that describes a video."""
    bits = []
    title = " ".join(str(idea.get("title") or "").split())[:MAX_IDEA_TITLE]
    if title:
        bits.append(f"idea: {title}")
    shot = " ".join(str(idea.get("what_you_see") or "").split())[:MAX_IDEA_SHOT]
    if shot:
        bits.append(f"what you see: {shot}")
    why = " ".join(str(idea.get("why_it_works") or "").split())[:MAX_IDEA_WHY]
    if why:
        bits.append(f"why it works: {why}")
    return (
        "THE IDEA THE TEAM PICKED — build this, do not invent a different one:\n"
        + "\n".join("  " + b for b in bits)
        + "\n\nThis is settled. Keep the shot, the people and the joke exactly as "
          "described; your job is to write THAT idea in the shape the tool below "
          "asks for, not to improve on it."
    )


# Where an unrecognised tool lands, in preference order. A still goes to a still
# tool and motion goes to something filmable — but only to a tool THIS source is
# allowed to use, which is why it is a list and not two constants: `broll` is
# not on a news story's menu at all.
_STILL_FALLBACKS = ("poster", "poster_human")
_MOTION_FALLBACKS = ("broll", "animation", "talking_head_v2")


def parse_ideas(raw: Any, *, exclude: Optional[Sequence[str]] = None,
                tools: Sequence[str] = TOOLS) -> list[dict]:
    """Pure — clamp the model's reply into at most ``IDEAS_PER_ROUND`` ideas.

    Defensive on purpose: a junk entry costs one idea, not the whole round, so
    a malformed third idea still leaves two on screen. The ids are stamped here
    and never read from the model — the frontend keys its radio group on them,
    so identity has to come from a place that cannot be hallucinated."""
    items = raw.get("ideas") if isinstance(raw, dict) else None
    if not isinstance(items, list):
        return []
    allowed = tuple(tools)
    dropped = {str(t).strip().lower() for t in (exclude or []) if str(t).strip()}
    out: list[dict] = []
    for entry in items:
        if not isinstance(entry, dict):
            continue
        title = " ".join(str(entry.get("title") or "").split())[:MAX_IDEA_TITLE]
        shot = " ".join(str(entry.get("what_you_see") or "").split())[:MAX_IDEA_SHOT]
        if not title or not shot:
            continue                       # no name or no shot is not an idea
        if title.lower() in dropped:
            continue                       # the model re-offered a rejected one
        tool = str(entry.get("tool") or "").strip()
        fmt = str(entry.get("format") or "").strip().lower()
        if fmt not in IDEA_FORMATS:
            fmt = "reel"
        if tool not in allowed:
            # Clamp rather than drop: the tool is a SUGGESTION (the user still
            # confirms it in the picker), so a wrong-but-valid tool costs one
            # click and an invalid one breaks the hand-off entirely.
            order = (_STILL_FALLBACKS if fmt in ("static", "carousel")
                     else _MOTION_FALLBACKS)
            tool = next((t for t in order if t in allowed), allowed[0])
        out.append({
            "id": f"idea-{len(out) + 1}",
            "title": title,
            "what_you_see": shot,
            "why_it_works": " ".join(str(entry.get("why_it_works") or "").split())[:MAX_IDEA_WHY],
            "tool": tool,
            "format": fmt,
        })
        if len(out) >= IDEAS_PER_ROUND:
            break
    return out


# ---------------------------------------------------------------------------
# Pure helpers (no network, no DB — these are what the tests drive)
# ---------------------------------------------------------------------------

def _news_block(post: dict) -> str:
    """Render a news story as the brief's source.

    The framing is the whole job here. The deliverable is 42's own post
    RESPONDING to the story — not a news report, and not something that reads as
    though 42 broke the news. A school account that appears to be reporting is a
    school account that can be wrong in public."""
    meta = post.get("metadata") if isinstance(post.get("metadata"), dict) else {}
    bits: list[str] = []
    if post.get("platform"):
        bits.append(f"outlet: {post['platform']}")
    if post.get("posted_at"):
        bits.append(f"published: {str(post['posted_at'])[:10]}")
    headline = " ".join(str(post.get("caption") or "").split())[:600]
    bits.append(f"the story, in plain words: {headline or '(none)'}")

    # BOTH headlines, and the publisher's one is what the model must reason
    # from. `caption` is OUR simplification, written for a card; building an
    # idea on top of it means building on a paraphrase of a paraphrase, and
    # each round of that drifts further from what the outlet actually said.
    original = " ".join(str(meta.get("original_headline") or "").split())[:600]
    if original and original.lower() != headline.lower():
        bits.append(f"the outlet's own headline: {original}")

    why = " ".join(str(meta.get("why_it_matters") or "").split())[:600]
    if why:
        bits.append(f"why it matters: {why}")

    return (
        "THE SOURCE STORY (a news item — write 42's own post that RESPONDS to it, "
        "do not write a news report):\n"
        + "\n".join("  " + b for b in bits)
        + "\n\nState no fact the story did not state. If you use a number, it must "
          "be one of the numbers above. Do not present 42 as the source of the "
          "news — 42 is reacting to it.\n"
          "The plain-words line above is OUR simplification of the outlet's "
          "headline, written for a feed card. Treat the outlet's own headline as "
          "what actually happened, and do not repeat a claim the simplification "
          "made sharper than the original."
    )


def source_block(post: dict, competitor: Optional[dict]) -> str:
    """The source, described ONCE.

    The brainstorm call and the brief call must not disagree about what they are
    looking at — the second one writes up an idea the first one had, and a
    difference of a single number between the two descriptions is a difference
    the model will happily act on."""
    if (post.get("source") or "competitor") == "news":
        # A news story has no account, no format and no likes. Printing the
        # competitor shape here would tell the model this idea got zero
        # engagement, which is both false and exactly the wrong steer.
        return _news_block(post)

    bits = [f"account: @{post.get('username')}"]
    if competitor:
        if competitor.get("display_name"):
            bits.append(f"account name: {competitor['display_name']}")
        if competitor.get("followers_count"):
            bits.append(f"followers: {competitor['followers_count']}")
        if competitor.get("region"):
            bits.append(f"region: {competitor['region']}")
    fmt = f"format: {post.get('media_type')}"
    if post.get("media_product_type"):
        fmt += f" / {post['media_product_type']}"
    bits.append(fmt)
    bits.append(f"engagement: {post.get('like_count') or 0} likes, "
                f"{post.get('comments_count') or 0} comments")
    # view_count is VIDEO-only and deliberately NULL for images and
    # carousels — printing "views: 0" would read as a flop rather than as
    # "not applicable".
    if post.get("view_count"):
        bits.append(f"views: {post['view_count']}")
    out = post.get("outperformance")
    if out:
        bits.append(f"performed {float(out):.1f}x that account's own average")
    posted = post.get("posted_at")
    if posted:
        bits.append(f"posted: {str(posted)[:10]}")
    caption = " ".join(str(post.get("caption") or "").split())[:600]
    bits.append(f"caption: {caption or '(none)'}")
    return "THE SOURCE POST:\n" + "\n".join("  " + b for b in bits)


def _scorer_block(post: dict) -> str:
    """What the scorer already worked out, or the line that says it hasn't.

    Empty string when the post is scored but every scored field is NULL — there
    is then a heading with nothing under it, which reads as a bug to the model."""
    if not post.get("scored_at"):
        return ("This post has not been scored yet — work from the caption "
                "and the numbers above.")
    # Only topic/theme/fit_score/uae_relevance/suggested_angle are required
    # by the scorer's schema (trend_scoring.py:106-107) — the rest are
    # routinely NULL, so every line is emitted conditionally.
    scored = []
    if post.get("topic"):
        scored.append(f"angle: {post['topic']}")
    if post.get("topic_summary"):
        scored.append(f"why it worked: {post['topic_summary']}")
    if post.get("theme"):
        scored.append(f"theme: {post['theme']}")
    if post.get("fit_score") is not None:
        line = f"fit for 42: {post['fit_score']}/100"
        if post.get("fit_reason"):
            line += f" — {post['fit_reason']}"
        scored.append(line)
    if post.get("uae_relevance") is not None:
        scored.append(f"UAE relevance: {post['uae_relevance']}/100")
    if post.get("suggested_angle"):
        scored.append(f"how 42 could do it: {post['suggested_angle']}")
    if post.get("suggested_format"):
        scored.append(f"suggested format: {post['suggested_format']}")
    if post.get("effort"):
        scored.append(f"effort: {post['effort']}")
    if post.get("tags"):
        scored.append("tags: " + ", ".join(str(t) for t in post["tags"][:8]))
    if not scored:
        return ""
    return "WHAT THE SCORER ALREADY WORKED OUT:\n" + "\n".join("  " + s for s in scored)


def build_prompt(
    *, post: dict, competitor: Optional[dict], tool: str, brand_note: str = "",
    idea: Optional[dict] = None,
) -> str:
    """Pure — the tests drive this directly. ``idea`` is one entry from
    ``brainstorm_ideas``; with none, the prompt is exactly what it always was."""
    parts: list[str] = []
    if brand_note:
        parts.append("WHAT 42 SOUNDS AND LOOKS LIKE:\n" + brand_note.strip())
    parts.append(source_block(post, competitor))
    scored = _scorer_block(post)
    if scored:
        parts.append(scored)
    if isinstance(idea, dict):
        parts.append(idea_block(idea))
    parts.append(_TOOL_BRIEFS[tool])
    parts.append(
        ("Write the picked idea as this tool's brief now."
         if isinstance(idea, dict) else "Write 42's version of this idea now.")
        + " Fill only the fields this tool asks for."
    )
    return "\n\n".join(parts)


def build_ideas_prompt(
    *, post: dict, competitor: Optional[dict], brand_note: str = "",
    feedback: str = "", exclude: Optional[Sequence[str]] = None,
) -> str:
    """Pure — the brainstorm prompt. The tests drive this directly.

    The same source description and the same scorer notes as the brief call, so
    the two steps cannot end up looking at different posts."""
    parts: list[str] = []
    if brand_note:
        parts.append("WHAT 42 SOUNDS AND LOOKS LIKE:\n" + brand_note.strip())
    parts.append(source_block(post, competitor))
    scored = _scorer_block(post)
    if scored:
        parts.append(scored)
    parts.append(_ideas_brief(post, tools_for(post)))
    said = brainstorm_block(feedback=feedback, exclude=exclude)
    if said:
        parts.append(said)
    parts.append(f"Write the {IDEAS_PER_ROUND} ideas now.")
    return "\n\n".join(parts)


def parse_prefill(raw: Any, *, tool: str, post: dict) -> dict:
    """Pure — clamp the model's reply into the exact envelope the UI expects.

    Every key is ALWAYS present ("" / [] when the tool doesn't use it), so the
    frontend never branches on undefined, and per-tool fields are zeroed for
    tools that can't use them rather than trusted from the model."""
    data = raw if isinstance(raw, dict) else {}

    title = str(data.get("title") or "").strip()[:80]
    if not title:
        fallback = (post.get("topic") or post.get("caption") or "Trend idea")
        title = " ".join(str(fallback).split())[:60] or "Trend idea"

    topic = str(data.get("topic") or "").strip()[:1200]
    if not topic:
        # An empty box is worse than a rough one — fall back to whatever the
        # scorer already wrote, then to the raw caption.
        fallback = (post.get("suggested_angle") or post.get("topic")
                    or post.get("topic_summary") or post.get("caption") or "")
        topic = " ".join(str(fallback).split())[:1200]

    dialogue: list[str] = []
    if tool == "talking_head_v2":
        dialogue = [s for s in (str(x).strip()[:400]
                                for x in (data.get("dialogue") or [])) if s]
        dialogue = dialogue[:MAX_DIALOGUE_LINES]

    intro_topic = outro_topic = ""
    if tool == "broll":
        intro_topic = str(data.get("intro_topic") or "").strip()[:200]
        outro_topic = str(data.get("outro_topic") or "").strip()[:200]

    character = ""
    if tool == "human_character":
        character = str(data.get("character") or "").strip()[:600]

    return {
        "tool": tool,
        "post_id": int(post["id"]),
        "title": title,
        "topic": topic,
        "dialogue": dialogue,
        "intro_topic": intro_topic,
        "outro_topic": outro_topic,
        "character": character,
        "notes": str(data.get("notes") or "").strip()[:300],
        "permalink": post.get("permalink"),
    }


# ---------------------------------------------------------------------------
# The ideation call
# ---------------------------------------------------------------------------

def _classify(exc: Exception, what: str, post_id: int) -> Exception:
    """The shared failure taxonomy for both model calls. Returns the exception
    to raise: IdeationUnavailable (a config problem the user can act on, → 422)
    or RuntimeError (transient, → 502)."""
    if isinstance(exc, VertexBudgetExceededError):
        # Raised from inside @_retry, so this has already burned 4 attempts. It
        # is a spend cap, not a blip — say so instead of "try again".
        return IdeationUnavailable(
            "the Vertex spend cap was reached — raise VEO_BUDGET_USD or wait for "
            "the meter to reset, then try again"
        )
    if trend_scoring._is_unavailable(exc):
        # Same subsystem, same classifier — billing off / 403 / no creds.
        return IdeationUnavailable(trend_scoring._explain_unavailable(exc))
    log.exception("[trend-finder] %s call failed (post %s)", what, post_id)
    return RuntimeError(f"couldn't reach the ideation model: {str(exc)[:200]}")


def _usable(raw: Any) -> dict:
    """gemini_text NEVER raises on unparseable JSON — it returns the {"_raw": …}
    sentinel. Both halves of this guard are load-bearing."""
    if not isinstance(raw, dict) or raw.get("_raw") is not None:
        raise RuntimeError("the ideation model returned an unreadable answer — try again")
    return raw


def _post_title(post: dict) -> str:
    """The stub the picker puts in its header. The scorer's topic is the
    readable name of the idea; an unscored row (and every news row before the
    scorer sees it) has only the caption, which for a story IS the headline."""
    for key in ("topic", "topic_summary", "caption"):
        text = " ".join(str(post.get(key) or "").split())
        if text:
            return text[:160]
    return f"Post #{post.get('id')}"


async def _load(post_id: int) -> tuple[dict, Optional[dict], str]:
    """The post, its competitor row and the brand note. Raises LookupError when
    the post is gone; the brand note degrades to "" and never raises."""
    post = await store.get_post(post_id)
    if not post:
        raise LookupError("post not found")
    competitor = None
    cid = post.get("competitor_id")
    if cid is not None:
        # get_post projects POST_COLS only — the competitor's name/size/region
        # live on the competitor row, not on the post.
        competitor = await store.get_competitor(int(cid))
    brand_note = await trend_scoring._brand_note()   # "" on any failure, never raises
    return post, competitor, brand_note


async def brainstorm_ideas(*, post_id: int, feedback: str = "",
                           exclude: Optional[Sequence[str]] = None) -> dict:
    """THREE different ways 42 could answer this post or story, in one call.

    ``feedback`` is what the owner typed when none of the last three landed;
    ``exclude`` is the titles they have already seen. Same error taxonomy as
    ``generate_prefill``: LookupError (gone) / ValueError (bad id) /
    IdeationUnavailable (config) / RuntimeError (transient).

    There is deliberately NO grounded web lookup here, unlike the General-trends
    twin. That lookup exists because a trend is often a cryptic hashtag whose
    name is a LABEL for something the model has to go and read about. A
    competitor post arrives with its caption and the scorer's verdict already
    attached, and a news item IS the story — the subject is on the page in both
    cases, so a search would cost a click's worth of latency to learn nothing.
    """
    post, competitor, brand_note = await _load(int(post_id))
    tools = tools_for(post)
    prompt = build_ideas_prompt(post=post, competitor=competitor,
                                brand_note=brand_note, feedback=feedback,
                                exclude=exclude)
    try:
        # temperature 0.9, not the 0.7 of the brief call: this is the DIVERGENT
        # step and three ideas that agree with each other is the failure mode.
        # 6000 max_tokens for the same reason as the brief call — 2.5-pro spends
        # hidden thinking tokens out of the same output pool, and a starved call
        # returns the {"_raw": …} sentinel rather than an error.
        raw = await gemini_text(
            prompt, system=_BRAINSTORM_SYSTEM, json_schema=ideas_schema(tools),
            temperature=0.9, max_tokens=6000,
        )
    except Exception as exc:  # noqa: BLE001
        raise _classify(exc, "brainstorm", post_id) from exc

    ideas = parse_ideas(_usable(raw), exclude=exclude, tools=tools)
    if not ideas:
        # One or two survivors are fine — the UI shows what it got. Zero means
        # there is nothing to put on screen, so say "try again" rather than
        # hand the owner an empty picker.
        raise RuntimeError("the ideation model returned no usable ideas — try again")
    return {"ideas": ideas,
            "post": {"id": int(post["id"]), "title": _post_title(post)}}


async def generate_prefill(*, post_id: int, tool: str,
                           idea: Optional[dict] = None) -> dict:
    """Returns the prefill envelope. Raises LookupError when the post is gone,
    ValueError for a tool this source may not be made with, IdeationUnavailable
    for a config problem and RuntimeError for anything else the router should
    surface as a 502.

    ``idea`` is the one the team picked in the brainstorm step; the brief then
    EXECUTES that idea instead of inventing a fourth angle. With no idea the
    behaviour is exactly what it was before the picker existed."""
    post, competitor, brand_note = await _load(post_id)
    # Checked against ``tools_for(post)``, not TOOLS: the four tools a news story
    # is refused are refused for a reason (see NEWS_TOOLS), and the card that
    # hides them is not a boundary — the endpoint takes the tool as a bare
    # string. The post has to be loaded to know which set applies, so the guard
    # moved below the load rather than the load being repeated.
    allowed = tools_for(post)
    if tool not in allowed:
        raise ValueError(f"tool must be one of {', '.join(allowed)}")
    prompt = build_prompt(post=post, competitor=competitor, tool=tool,
                          brand_note=brand_note,
                          idea=idea if isinstance(idea, dict) else None)
    try:
        # 2.5-pro spends hidden thinking tokens out of the SAME output pool, and
        # a truncated reply comes back as the {"_raw": …} sentinel rather than an
        # error (vertex_client:641-651). 6000 matches the image-scoring path
        # (trend_scoring:367); the 2000 default is not enough. thinking_budget is
        # deliberately left UNSET — this is a reasoning-heavy structured call
        # (vertex_client:562-563).
        raw = await gemini_text(
            prompt, system=_SYSTEM, json_schema=schema_for(tool),
            temperature=0.7, max_tokens=6000,
        )
    except Exception as exc:  # noqa: BLE001
        raise _classify(exc, f"{tool} ideation", post_id) from exc

    return parse_prefill(_usable(raw), tool=tool, post=post)
