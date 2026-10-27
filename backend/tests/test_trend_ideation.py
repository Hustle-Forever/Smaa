"""Tests for ``trend_ideation`` — the PLAIN-LANGUAGE RULE that stops the tools
writing posts only a 42 employee can read, and the brainstorm layer that now
serves every source rather than only General trends.

WHY THIS FILE EXISTS
--------------------
The owner was shown a generated post that opened

    "I just saw the announcement that the Norminette..."

The Norminette is 42's internal code linter. Nobody outside the school has
heard the word, and the reader we are writing for — a young person in the UAE
scrolling Instagram — scrolls on. That phrasing was not invented at the end of
the chain: it was born in the SCORER's ``suggested_angle`` and carried into the
brief, so the rule has to be in every prompt that writes words a stranger will
read, and it has to be the SAME rule in each.

``trend_ideation.PLAIN_LANGUAGE_RULE`` is that single copy. These tests pin:

  * the rule is present in the competitor-feed brief prompt (``ti._SYSTEM``),
  * it is the shared constant rather than a paraphrase of it (identity, not
    similarity — four near-identical variants is how one rule becomes four),
  * the Norminette example survives, because it is the concrete anchor the
    abstract instruction hangs on,
  * the per-tool briefs that emit audience-facing WORDS say so,
  * and the public surface ``general_trend_ideation`` imports is unchanged.

AND THE BRAINSTORM LAYER — sections 5 onward
--------------------------------------------
"Three ideas first, then pick one" was written for General trends and lived in
``general_trend_ideation``. It was never about trends: a competitor post and a
news story deserve the same offer, so it moved down here — into the module both
ideation paths already shared — parameterised by exactly ONE thing, the set of
Producer tools a source may be turned into. That parameter is the whole risk
surface of the move, and it is where these tests point:

  * ``tools_for`` / ``NEWS_TOOLS`` — a story may be made four ways, a competitor
    post eight. Offering a fifth for a story hands the user a tool the card does
    not show, and the hand-off 422s on click.
  * ``ideas_schema(tools)`` — the `tool` enum IS the tool set, so a news round
    cannot even be ASKED for general_video, and ``parse_ideas(tools=…)`` clamps
    whatever slips through anyway INTO the allowed set rather than out of it.
  * the news framing — a story has no account and no likes, so printing
    "engagement: 0 likes" would tell the model this idea flopped: false, and the
    exact opposite of the steer a story deserves.
  * ``build_prompt(idea=…)`` — additive. With no idea the brief is assembled
    exactly as it was before the picker existed; with one, the block is the only
    thing that appears.
  * and that the General path really does run on these primitives now — identity,
    not resemblance, because an alias that drifts back into a copy is how one
    rule becomes two. Its behaviour is pinned by
    tests/test_general_trend_ideation.py, which this refactor did not touch.

Nothing here reaches the network or the database. The pure helpers are driven
directly; the two functions that do call a model are handed a fake store and a
fake ``gemini_text``. There is no pytest-asyncio in this venv, so coroutines are
driven with ``asyncio.run`` via ``_run``, matching
tests/test_general_trend_ideation.py.

The source and scorer blocks themselves — every field ``build_prompt`` prints,
``parse_prefill``, ``_news_block`` — are covered in tests/test_trend_finder.py
and tests/test_ai_news.py; nothing here duplicates them, only the ``idea``
argument those tests predate.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

_BACKEND_ROOT = Path(__file__).resolve().parent.parent
if str(_BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(_BACKEND_ROOT))

from app.routers import trend_finder as router_mod  # noqa: E402
from app.services import general_trend_ideation as gti  # noqa: E402
from app.services import general_trends_pipeline as gtp  # noqa: E402
from app.services import trend_ideation as ti  # noqa: E402
from app.services import vertex_client as vc  # noqa: E402


# --------------------------------------------------------------------------- #
# 1. The rule itself
# --------------------------------------------------------------------------- #

def test_the_rule_names_the_reader_and_the_failure():
    """A couple of distinctive phrases, not the whole block — the block is
    compared by identity below, so pinning it twice would only make it
    tiresome to edit."""
    rule = ti.PLAIN_LANGUAGE_RULE
    assert "WRITE FOR SOMEONE WHO HAS NEVER HEARD OF 42." in rule
    assert "They have never written a line of code." in rule
    assert "NEVER use an insider word as if everyone knows it." in rule
    assert "EXPLAIN IT IN THE SAME BREATH" in rule
    assert "The joke has to land for someone who cannot code." in rule
    assert "Plain words over clever ones. No hashtags, no emoji." in rule


def test_the_rule_keeps_the_norminette_example():
    """The concrete anchor. "Avoid jargon" is advice every model already
    ignores; the exact sentence the owner objected to is what makes it land —
    and the second mention shows the ALLOWED form (explain it in the same
    breath) rather than only the banned one."""
    rule = ti.PLAIN_LANGUAGE_RULE
    assert "I just" in rule and "saw the announcement that the Norminette..." in rule
    assert "the robot that fails your code over one" in rule
    assert rule.count("Norminette") == 3      # named once, then banned, then explained


def test_the_rule_names_the_insider_words_by_name():
    """Piscine / Norminette / cluster / peer-to-peer are the four words that
    actually leak. A rule that says "no jargon" without naming them lets the
    model decide what counts as jargon, and it decides wrongly."""
    for word in ("Piscine", "Norminette", "cluster", "peer-to-peer"):
        assert word in ti.PLAIN_LANGUAGE_RULE, word


# --------------------------------------------------------------------------- #
# 2. One rule, not four
# --------------------------------------------------------------------------- #

def test_both_prompts_carry_the_same_rule_verbatim():
    """Identity, not resemblance: the scorer writes the angle and this module
    writes the brief FROM that angle, so if the two drift the angle wins and
    the rule is worthless. ``general_trend_ideation._SYSTEM`` imports the same
    constant (that module belongs to the general-trends path and is asserted in
    its own tests)."""
    assert ti.PLAIN_LANGUAGE_RULE in ti._SYSTEM
    assert ti.PLAIN_LANGUAGE_RULE in gtp._SYSTEM
    assert gtp.PLAIN_LANGUAGE_RULE is ti.PLAIN_LANGUAGE_RULE


def test_the_rule_leads_the_brief_prompt_before_the_mechanics():
    """Placement is load-bearing: the "Rules:" list below it is about WHICH
    fields to fill, and none of that matters if the words in them only make
    sense inside the building."""
    body = ti._SYSTEM
    assert body.index("WRITE FOR SOMEONE") < body.index("Rules:")
    # ...and after the school description, which is what the model needs in
    # order to understand the words it is being told not to use.
    assert body.index("month called the Piscine") < body.index("WRITE FOR SOMEONE")


def test_the_original_brief_rules_survived():
    """The rule is ADDITIVE. Everything that made the brief usable — 42's own
    version, concrete over clever, fill only what the tool asks for — is still
    there."""
    for line in (
        "- Write 42's OWN version of the idea. Never describe the competitor's "
        "post and never name the competitor.",
        "- Concrete beats clever: name the setting, the people and the action, "
        "the way a marketer would type it into a brief box.",
        "- No hashtags, no emoji, no camera jargon unless the tool asks for it.",
        "- Fill ONLY the fields the requested tool asks for. Leave the rest out.",
    ):
        assert line in ti._SYSTEM, line


# --------------------------------------------------------------------------- #
# 3. The per-tool briefs that produce words a stranger reads or hears
# --------------------------------------------------------------------------- #

def test_the_spoken_brief_guards_the_opening_line():
    """talking_head_v2's `dialogue` IS the post's spoken words, so this is the
    exact field the Norminette sentence came out of. The first line carries the
    whole thing."""
    brief = ti._TOOL_BRIEFS["talking_head_v2"]
    assert "The FIRST line" in brief
    assert "never open on an unexplained insider word" in brief
    # unchanged mechanics
    assert "1 to 3 spoken lines, one per scene, in order, 20-25 words" in brief


def test_the_on_screen_text_briefs_ask_for_plain_words():
    """A poster headline, an animation's on-screen words and a b-roll card are
    all read by a stranger in about a second — there is no room to explain."""
    assert "in words a stranger already knows" in ti._TOOL_BRIEFS["poster"]
    assert "in words a stranger already knows" in ti._TOOL_BRIEFS["poster_human"]
    assert "no insider term" in ti._TOOL_BRIEFS["animation"]
    broll = ti._TOOL_BRIEFS["broll"]
    assert "in plain words a stranger reads at a glance" in broll
    assert "in the same plain words" in broll


def test_the_internal_briefs_were_left_alone():
    """human_character is a CASTING brief and video_editor is an edit plan for
    the operator — neither is read by the audience, so neither was touched.
    Their existing wording is what the frontend seeder agrees with."""
    assert ti._TOOL_BRIEFS["human_character"].startswith(
        "TOOL: Human Character — generates ONE reusable AI person, NOT a post.")
    assert "This box expects a CASTING brief" in ti._TOOL_BRIEFS["human_character"]
    assert "It has NO brief box" in ti._TOOL_BRIEFS["video_editor"]
    assert "ONE paragraph in the present tense" in ti._TOOL_BRIEFS["general_video"]


def test_every_tool_still_has_a_brief_and_the_briefs_still_ship():
    """The shape ``general_trend_ideation`` depends on: one brief per tool, and
    ``build_prompt`` still pastes the brief in verbatim."""
    assert set(ti._TOOL_BRIEFS) == set(ti.TOOLS)
    post = {"id": 1, "username": "x", "caption": "c", "source": "competitor"}
    for tool in ti.TOOLS:
        assert ti._TOOL_BRIEFS[tool] in ti.build_prompt(
            post=post, competitor=None, tool=tool), tool


# --------------------------------------------------------------------------- #
# 4. The surface another module imports
# --------------------------------------------------------------------------- #

def test_the_public_surface_is_unchanged():
    """``general_trend_ideation`` does
    ``from .trend_ideation import IdeationUnavailable, TOOLS, parse_prefill,
    schema_for`` and reaches for ``_TOOL_BRIEFS``. This test is the reason a
    prompt edit here cannot quietly break the general-trends path."""
    assert issubclass(ti.IdeationUnavailable, RuntimeError)
    assert ti.TOOLS == ("poster", "animation", "poster_human", "talking_head_v2",
                        "video_editor", "human_character", "general_video", "broll")
    assert callable(ti.schema_for) and callable(ti.parse_prefill)
    assert ti.schema_for("talking_head_v2")["required"] == ["title", "topic", "dialogue"]
    assert ti.schema_for("poster") is ti._SCHEMA          # no per-tool copy when empty
    assert ti.MAX_DIALOGUE_LINES == 3


# --------------------------------------------------------------------------- #
# The fixtures the brainstorm sections share
#
# One competitor post (scored, a video, an outperformer) and one news story —
# the two shapes ``source_block`` has to keep apart. A story has no account, no
# format and no engagement, which is the whole reason the two are not one code
# path.
# --------------------------------------------------------------------------- #

def _run(coro):
    return asyncio.run(coro)


_POST = {
    "id": 412,
    "competitor_id": 7,
    "source": "competitor",
    "username": "nyuad",
    "permalink": "https://www.instagram.com/p/Cxyz/",
    "media_type": "VIDEO",
    "media_product_type": "REELS",
    "caption": "Paper ball tournament in the atrium",
    "posted_at": "2026-07-01",
    "like_count": 4200, "comments_count": 310, "view_count": 91000,
    "outperformance": 4.2,
    "topic": "Rivalry games between study groups",
    "topic_summary": "It gave the comments two teams to root for.",
    "theme": "campus life",
    "fit_score": 88, "fit_reason": "42 has clusters, students and a phone.",
    "uae_relevance": 71,
    "suggested_angle": "Run a cluster-vs-cluster mini tournament.",
    "suggested_format": "reel", "effort": "low",
    "tags": ["campus", "games"],
    "scored_at": "2026-07-02",
}

_COMPETITOR = {
    "id": 7, "username": "nyuad", "display_name": "NYU Abu Dhabi",
    "followers_count": 120_000, "region": "uae",
}

_NEWS = {
    "id": 7, "source": "news", "caption": "AI skills lift UAE salaries by 92%",
    "platform": "Gulf News", "region": "uae", "posted_at": "2026-08-17",
    "metadata": {"coverage": 4, "also_covered_by": ["Khaleej Times", "The National"]},
}

_GOOD_IDEA = {
    "why_it_works": "Everyone has been on the losing side of a stupid office bet.",
    "title": "The paper-ball final",
    "what_you_see": "Two clusters play one round of paper basketball across the "
                    "lab, the loser buys the karak, the winner does not gloat.",
    "tool": "broll", "format": "reel",
}


def _reply(*ideas: dict) -> dict:
    return {"ideas": [dict(i) for i in ideas]}


def _tool_enum(schema: dict) -> list:
    return schema["properties"]["ideas"]["items"]["properties"]["tool"]["enum"]


# --------------------------------------------------------------------------- #
# 5. tools_for — which tools a source may be turned into
# --------------------------------------------------------------------------- #

def test_a_news_story_may_only_be_made_four_ways():
    """A hard set, not a ranking. Each of the four missing tools is actively
    wrong for a story — a generated video of a real event is a factual-accuracy
    hazard on a school's account, b-roll needs a shoot and so is never still
    news, human_character writes a casting brief and video_editor has no brief
    box at all — and the frontend card offers exactly these four."""
    assert ti.tools_for(_NEWS) == ti.NEWS_TOOLS
    assert ti.NEWS_TOOLS == ("poster", "poster_human", "talking_head_v2", "animation")
    for banned in ("general_video", "broll", "human_character", "video_editor"):
        assert banned not in ti.NEWS_TOOLS, banned
    # ...and all four are real Producer tools, not a typo that would 422 on click.
    assert set(ti.NEWS_TOOLS) <= set(ti.TOOLS)


def test_a_competitor_post_may_be_made_every_way():
    assert ti.tools_for(_POST) == ti.TOOLS
    assert len(ti.TOOLS) == 8


def test_a_row_with_no_source_is_a_competitor_post():
    """``source`` arrived with the news feed, so every competitor row written
    before it reads back NULL. A NULL must not quietly narrow the menu to four."""
    for post in ({}, {"source": None}, {"source": ""}):
        assert ti.tools_for(post) == ti.TOOLS, post


# --------------------------------------------------------------------------- #
# 6. ideas_schema(tools) — the first of the two defences
# --------------------------------------------------------------------------- #

def test_a_news_brainstorm_cannot_even_be_asked_for_a_banned_tool():
    """Defence in depth, and this is the layer that acts BEFORE the model
    answers: the enum handed over IS the tool set, so a news round can only come
    back naming one of the four unless the model ignores its own schema.
    ``parse_ideas`` is the second layer, for when it does."""
    enum = _tool_enum(ti.ideas_schema(ti.NEWS_TOOLS))
    assert enum == list(ti.NEWS_TOOLS)
    for banned in ("general_video", "broll", "human_character", "video_editor"):
        assert banned not in enum, banned


def test_the_enum_is_whatever_tool_set_it_was_handed():
    assert _tool_enum(ti.ideas_schema()) == list(ti.TOOLS)          # default: all eight
    assert _tool_enum(ti.ideas_schema(("poster", "broll"))) == ["poster", "broll"]


def test_the_schema_is_memoised_per_tool_set_so_identity_holds():
    """Callers hold on to the object — ``general_trend_ideation`` exports it as
    ``_IDEAS_SCHEMA`` and its own tests assert ``kw["json_schema"] is`` that —
    so a fresh dict per call would make those `is` comparisons quietly false.
    A list and a tuple of the same tools are the same key."""
    assert ti.ideas_schema(ti.TOOLS) is ti.ideas_schema(list(ti.TOOLS))
    assert ti.ideas_schema(ti.NEWS_TOOLS) is not ti.ideas_schema(ti.TOOLS)
    assert gti._IDEAS_SCHEMA is ti.ideas_schema(ti.TOOLS)


def test_the_news_schema_keeps_every_other_part_of_the_shape():
    """Only the enum varies with the tool set. Every field still required
    (Gemini silently drops optional ones) and the reasoning still comes before
    the name, so the model argues why a stranger stops scrolling before it gets
    to invent a clever title."""
    item = ti.ideas_schema(ti.NEWS_TOOLS)["properties"]["ideas"]["items"]
    assert set(item["required"]) == set(item["properties"])
    assert list(item["properties"])[0] == "why_it_works"
    assert item["propertyOrdering"] == list(item["properties"])
    assert item["properties"]["format"]["enum"] == list(ti.IDEA_FORMATS)


# --------------------------------------------------------------------------- #
# 7. parse_ideas — the second defence, and the one that runs on a real reply
# --------------------------------------------------------------------------- #

def test_parse_ideas_clamps_a_banned_tool_into_the_news_set():
    """Clamp, never pass through: the tool decides which creator the "Make this"
    click opens, so an out-of-set one breaks the hand-off outright. Clamping
    instead costs one click, because the user still confirms the tool in the
    picker. `broll` lands on `animation` rather than on the first fallback —
    b-roll is not on a story's menu at all, so the preference list is walked
    until something allowed turns up."""
    out = ti.parse_ideas(_reply(
        {**_GOOD_IDEA, "title": "a", "tool": "broll", "format": "reel"},
        {**_GOOD_IDEA, "title": "b", "tool": "general_video", "format": "static"},
        {**_GOOD_IDEA, "title": "c", "tool": "talking_head_v2", "format": "reel"},
    ), tools=ti.NEWS_TOOLS)
    assert [i["tool"] for i in out] == ["animation", "poster", "talking_head_v2"]
    assert all(i["tool"] in ti.NEWS_TOOLS for i in out)


def test_parse_ideas_clamps_an_invented_tool_on_the_competitor_set_too():
    out = ti.parse_ideas(_reply(
        {**_GOOD_IDEA, "title": "a", "tool": "tiktok", "format": "static"},
        {**_GOOD_IDEA, "title": "b", "tool": "", "format": "reel"},
        {**_GOOD_IDEA, "title": "c", "tool": "poster_human", "format": "carousel"},
    ))
    assert out[0]["tool"] == "poster"        # a still goes to a still tool
    assert out[1]["tool"] == "broll"         # motion goes to something filmable
    assert out[2]["tool"] == "poster_human"  # a valid one is left exactly alone


def test_parse_ideas_lands_inside_the_set_even_when_no_fallback_fits():
    """The fallbacks are a preference list, not a promise. A tool set with no
    still tool and nothing filmable in it must STILL produce a tool the picker
    can hand over, so the first allowed one is the last resort."""
    [idea] = ti.parse_ideas(_reply({**_GOOD_IDEA, "tool": "tiktok"}),
                            tools=("video_editor", "human_character"))
    assert idea["tool"] == "video_editor"


def test_parse_ideas_drops_a_title_the_team_already_turned_down():
    """Case and surrounding whitespace are noise — re-offering "The Paper-Ball
    Final" after the team rejected "the paper-ball final" is the same idea with
    the shift key held down, and the whole point of a re-roll is that it comes
    back different."""
    out = ti.parse_ideas(_reply({**_GOOD_IDEA, "title": "The paper-ball final"},
                                {**_GOOD_IDEA, "title": "Something new"}),
                         exclude=["  the PAPER-BALL final  "])
    assert [i["title"] for i in out] == ["Something new"]
    assert out[0]["id"] == "idea-1"          # renumbered, not idea-2


def test_parse_ideas_caps_the_round_and_stamps_the_ids_itself():
    """The frontend keys its radio group on the id, so identity has to come from
    somewhere that cannot be hallucinated."""
    many = _reply(*[{**_GOOD_IDEA, "id": "banana", "title": f"t{i}"} for i in range(7)])
    out = ti.parse_ideas(many)
    assert len(out) == ti.IDEAS_PER_ROUND == 3
    assert [i["id"] for i in out] == ["idea-1", "idea-2", "idea-3"]
    # Fewer than three is returned as-is — two ideas on screen beat an error.
    assert len(ti.parse_ideas(_reply(_GOOD_IDEA, {**_GOOD_IDEA, "title": "b"}))) == 2


def test_parse_ideas_forces_the_format_into_the_pills_the_ui_can_show():
    """IDEA_FORMATS mirrors the scorer's `suggested_format` enum and the pill on
    the card. Anything else — an empty string, a platform name — becomes `reel`
    rather than reaching the UI as an unrenderable label."""
    for junk in ("TikTok", "", None, "video", 7):
        [idea] = ti.parse_ideas(_reply({**_GOOD_IDEA, "format": junk}))
        assert idea["format"] == "reel", junk
    for good in ("Static", "CAROUSEL", " reel "):
        [idea] = ti.parse_ideas(_reply({**_GOOD_IDEA, "format": good}))
        assert idea["format"] in ti.IDEA_FORMATS, good


def test_parse_ideas_clamps_the_lengths_and_flattens_the_whitespace():
    [idea] = ti.parse_ideas(_reply({**_GOOD_IDEA,
                                    "title": "t" * 300,
                                    "what_you_see": "w" * 900,
                                    "why_it_works": "y" * 900}))
    assert len(idea["title"]) == ti.MAX_IDEA_TITLE
    assert len(idea["what_you_see"]) == ti.MAX_IDEA_SHOT
    assert len(idea["why_it_works"]) == ti.MAX_IDEA_WHY
    [idea] = ti.parse_ideas(_reply({**_GOOD_IDEA, "title": "  a\n\n  b  "}))
    assert idea["title"] == "a b"


def test_parse_ideas_drops_one_bad_entry_rather_than_the_round():
    """A junk third idea costs one idea, not the whole round — two on screen is
    still a usable picker."""
    out = ti.parse_ideas({"ideas": [
        {**_GOOD_IDEA, "title": ""},                        # no name
        {**_GOOD_IDEA, "title": "no shot", "what_you_see": "  "},
        "a string, not an object",
        None,
        {**_GOOD_IDEA, "title": "the survivor"},
    ]})
    assert [i["title"] for i in out] == ["the survivor"]
    assert out[0]["why_it_works"]                            # kept, not blanked


def test_parse_ideas_returns_nothing_it_cannot_stand_behind():
    """[] is a signal, not a result — see the brainstorm test that turns it into
    "try again" rather than into an empty picker."""
    for junk in (None, "text", [], {}, {"ideas": None}, {"ideas": "nope"},
                 {"ideas": []}, {"_raw": "```json {"}):
        assert ti.parse_ideas(junk) == [], junk


# --------------------------------------------------------------------------- #
# 8. build_ideas_prompt — the same source, framed for what it actually is
# --------------------------------------------------------------------------- #

def test_a_news_brainstorm_asks_for_a_reply_to_the_story_not_a_report():
    """A school account that appears to be REPORTING is a school account that
    can be wrong in public."""
    prompt = ti.build_ideas_prompt(post=_NEWS, competitor=None)
    assert "THE SOURCE STORY" in prompt
    assert "42 is REACTING to this story, not reporting it." in prompt
    assert "Never write it as though 42 broke the news" in prompt
    assert "state no fact the story above did not state" in prompt
    assert "OFF THE BACK OF THIS STORY." in prompt
    assert "AI skills lift UAE salaries by 92%" in prompt


def test_a_news_brainstorm_never_prints_the_competitor_engagement_shape():
    """A story has no account and no likes. "engagement: 0 likes, 0 comments"
    would tell the model this idea flopped — false, and the exact opposite of
    the steer a story deserves. The competitor row is passed in here on purpose:
    even with one to hand, a story is never described as a post."""
    prompt = ti.build_ideas_prompt(post=_NEWS, competitor=_COMPETITOR)
    assert "0 likes" not in prompt and "engagement:" not in prompt
    assert "account:" not in prompt and "followers:" not in prompt
    assert "THE SOURCE POST" not in prompt
    assert "Take the IDEA, not the post" not in prompt


def test_a_news_brainstorm_offers_only_the_four_safe_tools():
    """The menu and ``NEWS_TOOLS`` cannot drift apart: the menu IS the tool set,
    taken line by line from each tool's own brief."""
    prompt = ti.build_ideas_prompt(post=_NEWS, competitor=None)
    assert ti.tool_menu(ti.NEWS_TOOLS) in prompt
    assert "These are the only tools a news story may use" in prompt
    for banned in ("general_video", "broll", "human_character", "video_editor"):
        assert banned not in prompt, banned


def test_a_competitor_brainstorm_keeps_the_numbers_and_the_full_menu():
    prompt = ti.build_ideas_prompt(post=_POST, competitor=_COMPETITOR)
    assert "THE SOURCE POST" in prompt and "THE SOURCE STORY" not in prompt
    assert "@nyuad" in prompt and "NYU Abu Dhabi" in prompt
    assert "engagement: 4200 likes, 310 comments" in prompt
    assert "performed 4.2x that account's own average" in prompt
    assert "Take the IDEA, not the post." in prompt
    assert "42 is REACTING" not in prompt
    assert ti.tool_menu(ti.TOOLS) in prompt
    # Two of the eight are not posts at all, so the menu says so where they show.
    assert "neither is a post, so pick them only if the idea genuinely is that" in prompt


def test_both_calls_describe_the_source_in_exactly_the_same_words():
    """The brief writes up an idea the brainstorm had. One number that differs
    between the two descriptions is a difference the model will happily act on,
    which is the entire reason ``source_block`` was pulled out of build_prompt."""
    block = ti.source_block(_POST, _COMPETITOR)
    assert block in ti.build_ideas_prompt(post=_POST, competitor=_COMPETITOR)
    assert block in ti.build_prompt(post=_POST, competitor=_COMPETITOR, tool="poster")
    story = ti.source_block(_NEWS, None)
    assert story in ti.build_ideas_prompt(post=_NEWS, competitor=None)
    assert story in ti.build_prompt(post=_NEWS, competitor=None, tool="poster")


def test_the_three_must_differ_in_kind_and_the_shot_must_be_a_shot():
    """The spread is what stops three rewordings of one idea, and
    `what_you_see` is what a student can be handed and film without asking a
    question."""
    prompt = ti.build_ideas_prompt(post=_POST, competitor=None)
    for kind in ("1. STRAIGHT", "2. FLIPPED", "3. CHEAPEST"):
        assert kind in prompt, kind
    assert "Three variations of the same idea is a failed answer." in prompt
    assert "`what_you_see` is THE SHOT, not the concept." in prompt


def test_the_first_round_carries_no_feedback_or_exclude_block():
    """With nothing said yet the prompt is exactly what it would have been if
    the picker had never been built."""
    prompt = ti.build_ideas_prompt(post=_POST, competitor=_COMPETITOR)
    assert "ALREADY SHOWN AND TURNED DOWN" not in prompt
    assert "WHAT THE TEAM ASKED FOR INSTEAD" not in prompt
    assert ti.brainstorm_block() == ""
    for blank in ("", "   ", None):
        assert ti.brainstorm_block(feedback=blank, exclude=[]) == ""


def test_feedback_outranks_everything_and_no_longer_says_the_trend():
    """The block is now sent for posts and stories as well as trends, so it can
    no longer claim to beat "the trend" — for a competitor post there isn't one.
    This is the ONE deliberate wording change the shared-brainstorm refactor
    made, pinned here so nobody reverts it to the trend-only sentence."""
    prompt = ti.build_ideas_prompt(post=_POST, competitor=None,
                                   feedback="funnier, and shorter")
    assert "funnier, and shorter" in prompt
    assert "This is the strongest instruction on this page." in prompt
    assert ("It beats everything above it — the source, the scorer's suggested "
            "angle, and your own taste.") in prompt
    assert "It beats the trend" not in prompt
    # It sits LAST, after the ask: it was typed precisely because the round it
    # replaces was wrong, and it is the instruction most likely to be followed.
    assert prompt.index("WHAT THE TEAM ASKED FOR INSTEAD") > prompt.index("`format`:")


def test_feedback_and_exclude_are_deduped_capped_and_clamped():
    titles = ["The 3am walk-out", "the 3AM walk-out", "  The 3am walk-out  "]
    titles += [f"idea {i}" for i in range(20)]
    prompt = ti.build_ideas_prompt(post=_POST, competitor=None, exclude=titles,
                                   feedback="f" * 900)
    assert prompt.count("- The 3am walk-out") == 1     # one idea, three spellings
    assert prompt.count("\n  - ") == ti.MAX_EXCLUDE    # and the tail is dropped
    assert "f" * ti.MAX_FEEDBACK in prompt
    assert "f" * (ti.MAX_FEEDBACK + 1) not in prompt


# --------------------------------------------------------------------------- #
# 9. build_prompt(idea=…) — additive, and provably so
# --------------------------------------------------------------------------- #

_PICKED = {"id": "idea-2", **_GOOD_IDEA}


def test_a_brief_with_no_idea_is_what_it_was_before_the_picker_existed():
    """Assembled from the source, the scorer's notes, the tool brief and the
    closing line — in that order, joined by blank lines, and NOTHING else. The
    fields inside those blocks are pinned in tests/test_trend_finder.py; what is
    asserted here is that the brainstorm work added no paragraph, no separator
    and no stray heading to the path that does not use it."""
    prompt = ti.build_prompt(post=_POST, competitor=_COMPETITOR, tool="poster")
    assert prompt == "\n\n".join([
        ti.source_block(_POST, _COMPETITOR),
        ti._scorer_block(_POST),
        ti._TOOL_BRIEFS["poster"],
        "Write 42's version of this idea now. Fill only the fields this tool asks for.",
    ])
    assert "THE IDEA THE TEAM PICKED" not in prompt
    assert "what you see:" not in prompt and "why it works:" not in prompt


def test_a_picked_idea_only_ADDS_a_block_and_moves_nothing_else():
    """Subtract the idea block, swap the one line that changes, and the prompt
    is byte-for-byte the no-idea one. Cheaper to keep honest than a golden
    string, and it fails loudly if the block ever lands somewhere else or the
    blocks around it are re-flowed."""
    without = ti.build_prompt(post=_POST, competitor=_COMPETITOR, tool="poster")
    with_idea = ti.build_prompt(post=_POST, competitor=_COMPETITOR, tool="poster",
                                idea=_PICKED)
    head, sep, tail = with_idea.partition("\n\n" + ti.idea_block(_PICKED))
    assert sep, "the picked idea is pasted in verbatim, as its own paragraph"
    assert head + tail.replace(
        "Write the picked idea as this tool's brief now.",
        "Write 42's version of this idea now.") == without


def test_a_picked_idea_carries_its_title_its_shot_and_its_reason():
    prompt = ti.build_prompt(post=_POST, competitor=_COMPETITOR,
                             tool="poster", idea=_PICKED)
    assert "THE IDEA THE TEAM PICKED — build this, do not invent a different one:" in prompt
    assert "idea: The paper-ball final" in prompt
    assert "what you see: Two clusters play one round of paper basketball" in prompt
    assert "why it works: Everyone has been on the losing side" in prompt
    assert "not to improve on it." in prompt
    # Above the tool brief, and the closing line points back at it rather than
    # asking for a fourth angle.
    assert prompt.index("THE IDEA THE TEAM PICKED") < prompt.index(ti._TOOL_BRIEFS["poster"])
    assert prompt.rstrip().endswith("Write the picked idea as this tool's brief now. "
                                    "Fill only the fields this tool asks for.")


def test_the_picked_ideas_own_tool_and_format_are_never_printed():
    """The caller already chose the destination tool — they may have overridden
    the suggestion in the picker — and the tool brief below dictates the shape.
    Two contradicting format instructions in one prompt is how a poster brief
    ends up describing a video."""
    prompt = ti.build_prompt(post=_POST, competitor=None, tool="poster",
                             idea={**_PICKED, "tool": "broll", "format": "reel"})
    block = prompt.split("THE IDEA THE TEAM PICKED")[1].split("TOOL:")[0]
    assert "broll" not in block and "reel" not in block


def test_a_half_written_idea_prints_only_what_it_has():
    prompt = ti.build_prompt(post=_POST, competitor=None, tool="poster",
                             idea={"id": "idea-1", "title": "just a name"})
    assert "idea: just a name" in prompt
    assert "what you see:" not in prompt and "why it works:" not in prompt


def test_anything_that_is_not_a_dict_is_ignored_rather_than_crashing():
    """The router forwards `idea` raw and untyped — it validates none of it — so
    the guard has to be here."""
    for bad in ("idea-1", None, ["idea"], 3):
        prompt = ti.build_prompt(post=_POST, competitor=None, tool="poster", idea=bad)
        assert "THE IDEA THE TEAM PICKED" not in prompt, bad


def test_a_news_story_can_carry_a_picked_idea_into_all_four_of_its_tools():
    for tool in ti.NEWS_TOOLS:
        prompt = ti.build_prompt(post=_NEWS, competitor=None, tool=tool, idea=_PICKED)
        assert "THE SOURCE STORY" in prompt, tool
        assert "idea: The paper-ball final" in prompt, tool
        assert ti._TOOL_BRIEFS[tool] in prompt, tool


# --------------------------------------------------------------------------- #
# 10. brainstorm_ideas end to end — a fake store and a fake model, no network
# --------------------------------------------------------------------------- #

class _FakeStore:
    """Only the two methods the ideation path calls."""

    def __init__(self, post=_POST, competitor=_COMPETITOR):
        self.post = post
        self.competitor = competitor

    async def get_post(self, post_id):
        return dict(self.post) if self.post else None

    async def get_competitor(self, competitor_id):
        return dict(self.competitor) if self.competitor else None


def _mock(monkeypatch, *, reply=None, post=_POST, competitor=_COMPETITOR,
          boom=None, calls: list | None = None):
    """Every module attribute the ideation path touches is looked up BY NAME at
    call time, which is why patching the module attribute is enough."""
    monkeypatch.setattr(ti, "store", _FakeStore(post, competitor))

    async def _brand_note():
        return ""
    monkeypatch.setattr(ti.trend_scoring, "_brand_note", _brand_note)

    async def _gemini_text(prompt, **kwargs):
        if calls is not None:
            calls.append((prompt, kwargs))
        if boom is not None:
            raise boom
        return reply
    monkeypatch.setattr(ti, "gemini_text", _gemini_text)


def test_brainstorm_returns_the_round_and_the_post_stub(monkeypatch):
    calls: list = []
    _mock(monkeypatch, calls=calls, reply=_reply(
        {**_GOOD_IDEA, "title": "straight"},
        {**_GOOD_IDEA, "title": "flipped", "tool": "poster", "format": "static"},
        {**_GOOD_IDEA, "title": "cheapest", "tool": "talking_head_v2"},
    ))
    out = _run(ti.brainstorm_ideas(post_id=412))
    assert [i["id"] for i in out["ideas"]] == ["idea-1", "idea-2", "idea-3"]
    assert [i["title"] for i in out["ideas"]] == ["straight", "flipped", "cheapest"]
    # The round's label. The scorer's topic is the readable name of the idea;
    # the picker does not wait for it, it names the row from what is already on
    # screen the moment the dialog opens.
    assert out["post"] == {"id": 412, "title": "Rivalry games between study groups"}

    prompt, kw = calls[0]
    assert "Paper ball tournament in the atrium" in prompt      # the source reached it
    assert kw["system"] is ti._BRAINSTORM_SYSTEM
    assert ti.PLAIN_LANGUAGE_RULE in kw["system"]
    assert kw["json_schema"] is ti.ideas_schema(ti.TOOLS)
    # The divergent call: hotter than the 0.7 brief call, and a budget big
    # enough that 2.5-pro's hidden thinking tokens cannot starve the answer.
    assert kw["temperature"] >= 0.85
    assert kw["max_tokens"] >= 4000
    # Exactly ONE model call. There is deliberately no grounded web lookup on
    # this path, unlike the General-trends twin: a competitor post arrives with
    # its caption and the scorer's verdict attached, and a news item IS the
    # story, so a search would cost latency to learn nothing.
    assert len(calls) == 1


def test_a_news_brainstorm_hands_over_the_four_tool_schema_and_clamps_the_reply(monkeypatch):
    """Both defences on one real round: the schema the model is given cannot
    name general_video, and the parser drops it into the allowed set when the
    model names it anyway."""
    calls: list = []
    _mock(monkeypatch, post=_NEWS, competitor=None, calls=calls,
          reply=_reply({**_GOOD_IDEA, "tool": "general_video", "format": "static"},
                       {**_GOOD_IDEA, "title": "second", "tool": "broll"}))
    out = _run(ti.brainstorm_ideas(post_id=7))
    assert calls[0][1]["json_schema"] is ti.ideas_schema(ti.NEWS_TOOLS)
    assert [i["tool"] for i in out["ideas"]] == ["poster", "animation"]
    assert all(i["tool"] in ti.NEWS_TOOLS for i in out["ideas"])
    # An unscored story has no topic, so the headline is the stub.
    assert out["post"] == {"id": 7, "title": "AI skills lift UAE salaries by 92%"}


def test_brainstorm_threads_feedback_and_exclude_into_the_call(monkeypatch):
    calls: list = []
    _mock(monkeypatch, calls=calls, reply=_reply({**_GOOD_IDEA, "title": "new one"},
                                                 {**_GOOD_IDEA}))
    out = _run(ti.brainstorm_ideas(post_id=412, feedback="funnier, and shorter",
                                   exclude=["The paper-ball final"]))
    prompt, _ = calls[0]
    assert "funnier, and shorter" in prompt
    assert "ALREADY SHOWN AND TURNED DOWN" in prompt
    assert "The paper-ball final" in prompt
    # Belt and braces: told not to re-offer it, and dropped when it does anyway.
    assert [i["title"] for i in out["ideas"]] == ["new one"]


def test_a_round_with_nothing_in_it_is_try_again_not_an_empty_picker(monkeypatch):
    """One or two survivors are fine — the UI shows what it got. Zero means
    there is nothing to put on screen, and an empty radio group reads as a bug
    rather than as a retry."""
    for reply in (_reply({"title": "", "what_you_see": ""}), {"ideas": []},
                  {"_raw": "```json {"}, "not a dict", None):
        _mock(monkeypatch, reply=reply)
        with pytest.raises(RuntimeError) as ei:
            _run(ti.brainstorm_ideas(post_id=412))
        assert "try again" in str(ei.value)
        assert not isinstance(ei.value, ti.IdeationUnavailable)   # 502, not 422


def test_brainstorm_billing_failure_is_ideation_unavailable(monkeypatch):
    _mock(monkeypatch, boom=RuntimeError("billing is disabled for project x"))
    with pytest.raises(ti.IdeationUnavailable) as ei:
        _run(ti.brainstorm_ideas(post_id=412))
    # It IS a RuntimeError, which is exactly why the router must catch it FIRST.
    assert isinstance(ei.value, RuntimeError)


def test_brainstorm_budget_cap_names_the_dial_the_owner_can_turn(monkeypatch):
    """The cap is only reached after the client has burned four retries — a
    spend ceiling, not a blip, so "try again" would be a lie."""
    _mock(monkeypatch, boom=vc.VertexBudgetExceededError("cap reached"))
    with pytest.raises(ti.IdeationUnavailable) as ei:
        _run(ti.brainstorm_ideas(post_id=412))
    assert "VEO_BUDGET_USD" in str(ei.value)


def test_brainstorm_transient_failure_is_a_plain_runtimeerror(monkeypatch):
    _mock(monkeypatch, boom=RuntimeError("504 gateway timeout"))
    with pytest.raises(RuntimeError) as ei:
        _run(ti.brainstorm_ideas(post_id=412))
    assert not isinstance(ei.value, ti.IdeationUnavailable)       # 502, retryable


def test_brainstorm_missing_post_is_a_lookuperror(monkeypatch):
    _mock(monkeypatch, reply=_reply(_GOOD_IDEA), post=None)
    with pytest.raises(LookupError):
        _run(ti.brainstorm_ideas(post_id=999))


def test_brainstorm_bad_post_id_is_a_valueerror(monkeypatch):
    """The router types the path param as int, so this never fires over HTTP —
    it fires for anything calling the service directly, and it matches the
    General path rather than blowing up somewhere less legible."""
    _mock(monkeypatch, reply=_reply(_GOOD_IDEA))
    with pytest.raises(ValueError):
        _run(ti.brainstorm_ideas(post_id="not-a-number"))  # type: ignore[arg-type]


def test_the_brief_call_executes_the_picked_idea(monkeypatch):
    calls: list = []
    _mock(monkeypatch, calls=calls, reply={"title": "The paper-ball final",
                                           "topic": "Two clusters, one round."})
    out = _run(ti.generate_prefill(post_id=412, tool="poster", idea=_PICKED))
    prompt, kw = calls[0]
    assert "THE IDEA THE TEAM PICKED" in prompt
    assert kw["system"] is ti._SYSTEM and kw["temperature"] == 0.7
    assert out["tool"] == "poster" and out["post_id"] == 412


def test_the_brief_call_without_an_idea_sends_the_prompt_it_always_sent(monkeypatch):
    calls: list = []
    _mock(monkeypatch, calls=calls, reply={"title": "t", "topic": "x"})
    _run(ti.generate_prefill(post_id=412, tool="poster"))
    prompt, _ = calls[0]
    assert "THE IDEA THE TEAM PICKED" not in prompt
    assert prompt == ti.build_prompt(post=_POST, competitor=_COMPETITOR, tool="poster")


def test_the_brief_call_still_refuses_an_unknown_tool(monkeypatch):
    _mock(monkeypatch, reply={})
    with pytest.raises(ValueError):
        _run(ti.generate_prefill(post_id=412, tool="tiktok", idea=_PICKED))


def test_the_brief_call_refuses_a_tool_this_STORY_may_not_be_made_with(monkeypatch):
    """``general_video`` is a real Producer tool, so "is it one of the eight?"
    lets it through — and a Veo clip depicting a real news event is the
    factual-accuracy hazard NEWS_TOOLS exists to prevent. The picker hiding the
    card is not what stops it: /posts/{id}/ideas takes the tool as a bare string
    from the client, so the refusal has to live here."""
    _mock(monkeypatch, post=_NEWS, competitor=None, reply={"title": "t", "topic": "x"})
    for banned in ("general_video", "broll", "human_character", "video_editor"):
        with pytest.raises(ValueError) as exc:
            _run(ti.generate_prefill(post_id=7, tool=banned))
        assert banned not in str(exc.value), banned      # the message lists the four
    # ...and the four a story MAY use still go straight through.
    for allowed in ti.NEWS_TOOLS:
        assert _run(ti.generate_prefill(post_id=7, tool=allowed))["tool"] == allowed
    # A competitor post keeps all eight — the guard narrows by SOURCE, not always.
    _mock(monkeypatch, reply={"title": "t", "topic": "x"})
    assert _run(ti.generate_prefill(post_id=412, tool="general_video"))["tool"] == "general_video"


# --------------------------------------------------------------------------- #
# 11. The General path runs on these very primitives now
#
# The refactor DELETED general_trend_ideation's copies rather than leaving two
# of everything. tests/test_general_trend_ideation.py is the regression net for
# the behaviour and was deliberately not touched; what is asserted here is the
# wiring — identity, not resemblance, because an alias quietly turning back into
# a copy is how one clamp becomes two that disagree.
# --------------------------------------------------------------------------- #

def test_the_general_module_aliases_the_shared_functions_themselves():
    assert gti.parse_ideas is ti.parse_ideas
    assert gti._idea_block is ti.idea_block
    assert gti._SCHOOL is ti.SCHOOL
    assert gti._IDEAS_SCHEMA is ti.ideas_schema(ti.TOOLS)
    for name in ("IDEAS_PER_ROUND", "IDEA_FORMATS", "MAX_IDEA_TITLE",
                 "MAX_IDEA_SHOT", "MAX_IDEA_WHY", "MAX_FEEDBACK", "MAX_EXCLUDE"):
        assert getattr(gti, name) == getattr(ti, name), name


def test_the_general_brainstorm_system_is_the_shared_builder_s_output():
    """The system prompt was the last near-identical copy: same school, same
    plain-language rule, same shootable and never-mock bullets, in two files.
    Rebuilding it from the builder with only the General path's own opening and
    its two trend rules proves nothing else is written there — a paragraph
    forked back into this module would fail this equality."""
    assert gti._BRAINSTORM_SYSTEM == ti.brainstorm_system(
        opening=gti._BRAINSTORM_SYSTEM.split("\n\n", 1)[0],
        rules=(gti.NAME_IS_A_LABEL_RULE, gti.CONNECT_TO_42_RULE),
        subject="trend",
    )
    # The one word that is allowed to differ, and does.
    assert "If the trend is sensitive" in gti._BRAINSTORM_SYSTEM
    assert "If the source is sensitive" in ti._BRAINSTORM_SYSTEM


def test_the_general_ideas_brief_shares_the_shot_paragraph_and_everything_after_it():
    """From "`what_you_see` is THE SHOT" to `format`, a trend and a competitor
    post are handed the SAME characters — both use the full tool set, so even
    the menu matches. That paragraph is the whole reason the brief exists, and
    it used to be tunable in one file while a stale twin sat in the other."""
    marker = "`what_you_see` is THE SHOT, not the concept."
    post = ti._ideas_brief({"source": "competitor"}, ti.TOOLS)
    assert marker in post and marker in gti._IDEAS_BRIEF
    assert post[post.index(marker):] == gti._IDEAS_BRIEF[gti._IDEAS_BRIEF.index(marker):]
    # The spread is shared too; only the two bullets that name the thing differ.
    for shared in ("The three must differ in KIND, not in wording.",
                   "3. CHEAPEST — the lowest-effort version that still works",
                   "Three variations of the same idea is a failed answer."):
        assert shared in post and shared in gti._IDEAS_BRIEF, shared
    assert "restage the trend literally" in gti._IDEAS_BRIEF
    assert "restage the trend literally" not in post


def test_a_general_trend_still_gets_all_eight_tools():
    """``gti.parse_ideas`` IS the shared function now, so it accepts a ``tools=``
    kwarg it never had. Its default has to stay the full menu, or a General
    trend silently loses half its tools."""
    [idea] = gti.parse_ideas(_reply({**_GOOD_IDEA, "tool": "broll", "format": "reel"}))
    assert idea["tool"] == "broll"                       # not clamped away
    assert _tool_enum(gti._IDEAS_SCHEMA) == list(ti.TOOLS)
    assert gti._tool_menu() == ti.tool_menu(ti.TOOLS)


# --------------------------------------------------------------------------- #
# 12. Router wiring — the taxonomy is the contract
# --------------------------------------------------------------------------- #

def test_the_router_exposes_the_brainstorm_route_and_the_optional_idea():
    paths = [r.path for r in router_mod.router.routes]
    assert "/api/trends/posts/{post_id}/brainstorm" in paths
    # Registration order is load-bearing: FastAPI matches in order, so every
    # parameterised /posts/{post_id}/… route stays below the literal ones.
    assert paths.index("/api/trends/posts/rescore") < paths.index(
        "/api/trends/posts/{post_id}/brainstorm")
    # Additive: a caller that sends only {"tool": …} still validates.
    assert router_mod.PostIdeasRequest(tool="poster").idea is None
    assert router_mod.PostIdeasRequest(tool="poster", idea=dict(_GOOD_IDEA)).idea
    # ...and a first round is a bare POST with no body at all.
    empty = router_mod.PostBrainstormRequest()
    assert empty.feedback is None and empty.exclude is None


def test_the_router_catches_ideation_unavailable_before_runtimeerror():
    """IdeationUnavailable IS a RuntimeError, so the ORDER of the except clauses
    is what decides whether the owner sees an actionable 422 ("raise
    VEO_BUDGET_USD", "billing is off") or a 502 they will retry forever."""
    import inspect
    for fn in (router_mod.post_brainstorm, router_mod.post_ideas):
        src = inspect.getsource(fn)
        assert src.index("IdeationUnavailable") < src.index("except RuntimeError"), fn
        assert src.index("except LookupError") < src.index("except RuntimeError"), fn
        assert src.index("except ValueError") < src.index("except RuntimeError"), fn
    assert issubclass(ti.IdeationUnavailable, RuntimeError)


# ---------------------------------------------------------------------------
# The human poster's default cast
# ---------------------------------------------------------------------------

def test_the_human_poster_casts_emirati_students_by_default_everywhere_it_is_described():
    """The owner's call, 2026-09-30: the faces on a Poster + Human are Emirati
    unless the idea needs otherwise. It has to be said in all three places
    the model reads about that tool, or one path forgets: the tool brief the
    prefill gets, the brief-writing system prompt, and the shared brainstorm
    prompt both feeds build from."""
    from app.services import general_trend_ideation as gti

    for text in (ti._TOOL_BRIEFS["poster_human"], ti._SYSTEM,
                 ti._BRAINSTORM_SYSTEM, gti._BRAINSTORM_SYSTEM):
        assert "Emirati" in text
        assert "kandura" in text and "abaya" in text, "recognisable dress, not just the word"
        assert "unless" in text, "a default, not a rule against everyone else"
    # Scoped to the human poster: the plain poster has no faces at all.
    assert "Emirati" not in ti._TOOL_BRIEFS["poster"]
