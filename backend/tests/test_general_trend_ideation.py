"""Tests for the three-idea brainstorm on a General trend.

Module under test: ``app/services/general_trend_ideation.py`` — specifically the
half that did not exist before the picker:

  * ``PLAIN_LANGUAGE_RULE`` — one rule, in EVERY system prompt this module
    sends. The owner's complaint was a generated post opening "I just saw the
    announcement that the Norminette..." and the only defence against that is
    the rule being present, verbatim, in both calls.
  * ``build_ideas_prompt`` — the trend + evidence + the three-in-different-kinds
    brief + exclude + feedback.
  * ``parse_ideas`` — defensive clamping; ids stamped here, never trusted from
    the model.
  * ``brainstorm_ideas`` / ``generate_prefill(idea=…)`` and the error taxonomy
    the router maps to 404 / 422 / 502.

Everything is mocked: no network, no database, no Vertex. ``store``,
``trend_scoring._brand_note`` and ``gemini_text`` are all module attributes
looked up BY NAME at call time, so monkeypatching them is enough.

There is no pytest-asyncio in this venv, so coroutines are driven with
``asyncio.run`` via ``_run``, matching tests/test_general_trends_pipeline.py.
"""
from __future__ import annotations

import asyncio
import sys
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

_BACKEND_ROOT = Path(__file__).resolve().parent.parent
if str(_BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(_BACKEND_ROOT))

from app.routers import general_trends as router_mod  # noqa: E402
from app.services import general_trend_ideation as gti  # noqa: E402
from app.services import trend_ideation as ti  # noqa: E402
from app.services import vertex_client as vc  # noqa: E402


def _run(coro):
    return asyncio.run(coro)


_NOW = datetime(2026, 8, 19, 12, 0, tzinfo=timezone.utc)

_TREND = {
    "id": 512, "slug": "nihilist penguin", "title": "Nihilist Penguin meme",
    "summary": "A penguin walking away from the colony toward the mountains, alone.",
    "url": "https://knowyourmeme.com/memes/nihilist-penguin",
    "sources": ["kym", "reddit"], "regions": ["global", "US"],
    "sighting_count": 7, "velocity": 0.83,
    "first_seen_at": _NOW - timedelta(days=2), "last_seen_at": _NOW,
    "scored_at": _NOW, "topic": "Walking away from the herd",
    "topic_summary": "A penguin leaves the colony and heads inland.",
    "theme": "meme format", "trend_kind": "meme", "sensitivity": "ok",
    "fit_score": 82, "fit_reason": "students can recreate it in the lab",
    "uae_relevance": 35,
    "suggested_angle": "A student walks out at 3am toward the vending machine.",
    "suggested_format": "reel", "effort": "low", "tags": ["meme", "burnout"],
}
_SIGHTINGS = [
    {"source": "kym", "region": "global", "title": "Nihilist Penguin",
     "metric_value": None, "metric_label": None},
    {"source": "reddit", "region": "global", "title": "the penguin that gave up",
     "metric_value": 4200, "metric_label": "posts"},
]

_GOOD_IDEA = {
    "why_it_works": "Everyone knows the feeling of walking out at 3am.",
    "title": "The 3am walk-out",
    "what_you_see": "A student stands up mid-sentence, walks the length of the "
                    "dark lab past twenty lit screens, and buys a karak.",
    "tool": "broll", "format": "reel",
}


def _reply(*ideas: dict) -> dict:
    return {"ideas": [dict(i) for i in ideas]}


class _FakeStore:
    """Only the two methods the ideation path calls."""

    def __init__(self, trend=_TREND, *, sightings=None, boom=None):
        self.trend = trend
        self.sightings = _SIGHTINGS if sightings is None else sightings
        self.boom = boom                      # raised by sightings_for

    async def get_trend(self, trend_id):
        return dict(self.trend) if self.trend else None

    async def sightings_for(self, ids, *, per_trend=6):
        if self.boom is not None:
            raise self.boom
        return {int(i): list(self.sightings)[:per_trend] for i in ids}


_RESEARCH_TEXT = (
    "WHAT IT IS: #spidermanbrandnewday is the hashtag for the 2026 film "
    "Spider-Man: Brand New Day. It is a film title, not a phrase about fresh "
    "starts.\n"
    "WHY PEOPLE POST: Fans react to the trailer and argue about the casting."
)
_RESEARCH_SOURCES = [{"title": "Marvel", "uri": "https://example.com/bnd"}]


def _mock(monkeypatch, *, reply=None, trend=_TREND, boom=None,
          calls: list | None = None, store_boom=None,
          research=None, research_boom=None, research_calls: list | None = None):
    """Every module attribute the ideation path looks up by name.

    ``research`` defaults to None → the grounded lookup finds NOTHING, which is
    the state in which every pre-research assertion in this file must still
    hold. Pass ``_RESEARCH_TEXT`` for the looked-it-up path."""
    monkeypatch.setattr(gti, "store", _FakeStore(trend, boom=store_boom))
    gti._RESEARCH_CACHE.clear()               # one process, many tests

    async def _brand_note():
        return ""
    monkeypatch.setattr(gti.trend_scoring, "_brand_note", _brand_note)

    async def _gemini_text(prompt, **kwargs):
        if calls is not None:
            calls.append((prompt, kwargs))
        if boom is not None:
            raise boom
        return reply
    monkeypatch.setattr(gti, "gemini_text", _gemini_text)

    async def _gemini_search(prompt, **kwargs):
        if research_calls is not None:
            research_calls.append((prompt, kwargs))
        if research_boom is not None:
            raise research_boom
        return (research or ""), list(_RESEARCH_SOURCES if research else [])
    monkeypatch.setattr(gti, "gemini_search", _gemini_search)


# =========================================================================== #
# 1. The plain-language rule — ask (1)
# =========================================================================== #

def test_the_rule_is_in_every_system_prompt_this_module_sends():
    # One rule, not two: the brief call and the brainstorm call carry the
    # IDENTICAL string, so they cannot drift apart.
    assert gti.PLAIN_LANGUAGE_RULE in gti._SYSTEM
    assert gti.PLAIN_LANGUAGE_RULE in gti._BRAINSTORM_SYSTEM
    # …and it is the SAME object the competitor path and the scorer send, not a
    # second copy of the same words. `is`, deliberately.
    assert gti.PLAIN_LANGUAGE_RULE is ti.PLAIN_LANGUAGE_RULE


def test_the_rule_says_the_reader_knows_nothing_and_names_the_norminette():
    rule = gti.PLAIN_LANGUAGE_RULE
    assert rule.startswith("WRITE FOR SOMEONE WHO HAS NEVER HEARD OF 42.")
    assert "They have never written a line of code." in rule
    # The owner's own example of the failure, kept as the worked example.
    assert "I just" in rule and "saw the announcement that the Norminette..." in rule
    assert "EXPLAIN IT IN THE SAME BREATH" in rule
    assert "the robot that fails your code over one" in rule
    assert "If getting it requires" in rule and "compiler" in rule
    assert rule.rstrip().endswith("No hashtags, no emoji.")


def test_the_system_prompts_do_not_hand_the_model_the_banned_words_undefined():
    # The old school blurb said "peer-to-peer" and "an intensive month called
    # the Piscine" — i.e. it supplied two of the words the rule bans and then
    # asked the model not to use them. Outside the rule's own text, they are
    # gone from both system prompts.
    for system in (gti._SYSTEM, gti._BRAINSTORM_SYSTEM):
        body = system.replace(gti.PLAIN_LANGUAGE_RULE, "")
        assert "Piscine" not in body
        assert "peer-to-peer" not in body
    assert "free" in gti._SCHOOL and "no teachers" in gti._SCHOOL


# =========================================================================== #
# 2. The brainstorm prompt
# =========================================================================== #

def test_ideas_prompt_carries_the_trend_the_evidence_and_the_three_kinds():
    p = gti.build_ideas_prompt(trend=_TREND, sightings=_SIGHTINGS,
                               brand_note="voice: warm")
    assert p.startswith("WHAT 42 SOUNDS AND LOOKS LIKE:\nvoice: warm")
    # Same trend block the brief call sees — one description, two calls.
    assert "THE TREND:" in p and "name: Nihilist Penguin meme" in p
    assert "heat: 83/100" in p
    assert "WHERE WE SAW IT:" in p
    assert "- Reddit · global · the penguin that gave up · 4,200 posts" in p
    assert "WHAT THE SCORER ALREADY WORKED OUT:" in p
    assert "how 42 could ride it: A student walks out" in p
    # …and the ask.
    assert "GIVE THE TEAM 3 IDEAS" in p
    assert "differ in KIND, not in wording" in p
    assert "STRAIGHT" in p and "FLIPPED" in p and "CHEAPEST" in p
    assert "Three variations of the same idea is a failed answer." in p
    assert "`what_you_see` is THE SHOT, not the concept." in p
    assert p.rstrip().endswith("Write the 3 ideas now.")


def test_ideas_prompt_lists_every_tool_from_the_shared_menu():
    p = gti.build_ideas_prompt(trend=_TREND, sightings=None)
    for tool in ti.TOOLS:
        assert f"  {tool}: " in p
    # The one-liners come from the tools' OWN briefs, so the menu cannot drift.
    assert "one static, designed brand poster with NO human faces." in p
    assert "human_character produces one reusable AI person" in p


def test_ideas_prompt_has_no_evidence_or_scorer_block_when_there_is_none():
    p = gti.build_ideas_prompt(trend={**_TREND, "scored_at": None}, sightings=[])
    assert "WHERE WE SAW IT" not in p and "WHAT THE SCORER" not in p
    assert "has not been scored yet" in p


def test_feedback_reaches_the_prompt_and_is_declared_the_strongest_input():
    p = gti.build_ideas_prompt(trend=_TREND, sightings=None,
                               feedback="  make it   about the free part  ")
    assert "WHAT THE TEAM ASKED FOR INSTEAD" in p
    assert "make it about the free part" in p                    # whitespace collapsed
    assert "This is the strongest instruction on this page." in p
    # Last block before the ask: it is replacing a round that was wrong.
    assert p.index("WHAT THE TEAM ASKED FOR INSTEAD") > p.index("GIVE THE TEAM 3 IDEAS")


def test_feedback_is_clamped_and_absent_when_blank():
    assert "WHAT THE TEAM ASKED FOR" not in gti.build_ideas_prompt(
        trend=_TREND, sightings=None, feedback="   ")
    long = gti.build_ideas_prompt(trend=_TREND, sightings=None, feedback="x" * 5000)
    assert "x" * gti.MAX_FEEDBACK in long and "x" * (gti.MAX_FEEDBACK + 1) not in long


def test_exclude_reaches_the_prompt_deduped_and_capped():
    p = gti.build_ideas_prompt(trend=_TREND, sightings=None,
                               exclude=["The 3am walk-out", "the 3AM WALK-OUT", "", None,
                                        *[f"idea {i}" for i in range(20)]])
    assert "ALREADY SHOWN AND TURNED DOWN" in p
    assert "do not offer a reworded version of them" in p
    assert p.count("  - ") == gti.MAX_EXCLUDE                    # capped
    assert p.count("3am walk-out") == 1                          # case-insensitive dedupe
    assert "ALREADY SHOWN" not in gti.build_ideas_prompt(trend=_TREND, sightings=None,
                                                         exclude=[])


# =========================================================================== #
# 3. parse_ideas — defensive, and the ids are ours
# =========================================================================== #

def test_parse_ideas_returns_the_contract_shape():
    [idea] = gti.parse_ideas(_reply(_GOOD_IDEA))
    assert idea == {
        "id": "idea-1",
        "title": "The 3am walk-out",
        "what_you_see": _GOOD_IDEA["what_you_see"],
        "why_it_works": _GOOD_IDEA["why_it_works"],
        "tool": "broll",
        "format": "reel",
    }


def test_parse_ideas_stamps_the_ids_itself_whatever_the_model_says():
    reply = _reply(*[{**_GOOD_IDEA, "id": "banana", "title": f"idea {i}"}
                     for i in range(3)])
    assert [i["id"] for i in gti.parse_ideas(reply)] == ["idea-1", "idea-2", "idea-3"]


def test_parse_ideas_caps_the_round_and_keeps_a_short_one():
    many = _reply(*[{**_GOOD_IDEA, "title": f"t{i}"} for i in range(6)])
    assert len(gti.parse_ideas(many)) == gti.IDEAS_PER_ROUND
    # Fewer than three is returned as-is — two ideas on screen beat an error.
    assert len(gti.parse_ideas(_reply(_GOOD_IDEA, {**_GOOD_IDEA, "title": "b"}))) == 2


def test_parse_ideas_drops_malformed_entries_without_losing_the_round():
    out = gti.parse_ideas({"ideas": [
        {**_GOOD_IDEA, "title": ""},                       # no name
        {**_GOOD_IDEA, "title": "no shot", "what_you_see": "  "},
        "a string, not an object",
        None,
        {**_GOOD_IDEA, "title": "the survivor"},
    ]})
    assert [i["title"] for i in out] == ["the survivor"]
    assert out[0]["id"] == "idea-1"                          # renumbered, not idea-5


def test_parse_ideas_survives_junk_at_the_top_level():
    for junk in (None, "text", [], {}, {"ideas": None}, {"ideas": "nope"},
                 {"_raw": "```json {"}):
        assert gti.parse_ideas(junk) == []


def test_parse_ideas_clamps_the_tool_to_the_shared_tool_set():
    out = gti.parse_ideas(_reply(
        {**_GOOD_IDEA, "title": "a", "tool": "tiktok", "format": "static"},
        {**_GOOD_IDEA, "title": "b", "tool": "", "format": "reel"},
        {**_GOOD_IDEA, "title": "c", "tool": "poster_human", "format": "carousel"},
    ))
    assert all(i["tool"] in ti.TOOLS for i in out)
    assert out[0]["tool"] == "poster"       # a still → the still tool
    assert out[1]["tool"] == "broll"        # motion → something filmable
    assert out[2]["tool"] == "poster_human"  # a valid one is left alone


def test_parse_ideas_clamps_the_format_and_the_lengths():
    [idea] = gti.parse_ideas(_reply({**_GOOD_IDEA, "format": "TikTok",
                                     "title": "t" * 200,
                                     "what_you_see": "w" * 900,
                                     "why_it_works": "y" * 900}))
    assert idea["format"] == "reel"
    assert len(idea["title"]) == gti.MAX_IDEA_TITLE
    assert len(idea["what_you_see"]) == gti.MAX_IDEA_SHOT
    assert len(idea["why_it_works"]) == gti.MAX_IDEA_WHY


def test_parse_ideas_drops_a_title_the_team_already_turned_down():
    out = gti.parse_ideas(_reply({**_GOOD_IDEA, "title": "The 3am walk-out"},
                                 {**_GOOD_IDEA, "title": "Something new"}),
                          exclude=["the 3AM walk-out"])
    assert [i["title"] for i in out] == ["Something new"]


def test_parse_ideas_keeps_an_idea_with_no_reason_given():
    # why_it_works is required in the schema but a missing one is not worth
    # binning a shot the team can actually film.
    [idea] = gti.parse_ideas(_reply({**_GOOD_IDEA, "why_it_works": None}))
    assert idea["why_it_works"] == ""


# =========================================================================== #
# 4. The schema
# =========================================================================== #

def test_ideas_schema_requires_every_field_and_leads_with_the_reasoning():
    item = gti._IDEAS_SCHEMA["properties"]["ideas"]["items"]
    assert set(item["required"]) == set(item["properties"])   # Gemini drops optional ones
    assert list(item["properties"])[0] == "why_it_works"      # reason before name
    assert item["propertyOrdering"] == list(item["properties"])
    assert item["properties"]["tool"]["enum"] == list(ti.TOOLS)
    assert item["properties"]["format"]["enum"] == list(gti.IDEA_FORMATS)
    assert gti._IDEAS_SCHEMA["required"] == ["ideas"]


# =========================================================================== #
# 5. brainstorm_ideas end to end
# =========================================================================== #

def test_brainstorm_returns_three_ideas_and_the_trend_stub(monkeypatch):
    calls: list = []
    _mock(monkeypatch, calls=calls, reply=_reply(
        {**_GOOD_IDEA, "title": "straight"},
        {**_GOOD_IDEA, "title": "flipped", "tool": "poster", "format": "static"},
        {**_GOOD_IDEA, "title": "cheapest", "tool": "talking_head_v2"},
    ))
    out = _run(gti.brainstorm_ideas(trend_id=512))
    assert [i["id"] for i in out["ideas"]] == ["idea-1", "idea-2", "idea-3"]
    assert [i["title"] for i in out["ideas"]] == ["straight", "flipped", "cheapest"]
    assert out["trend"] == {"id": 512, "title": "Nihilist Penguin meme"}

    prompt, kw = calls[0]
    assert "the penguin that gave up" in prompt                  # evidence reached it
    assert kw["system"] is gti._BRAINSTORM_SYSTEM
    assert gti.PLAIN_LANGUAGE_RULE in kw["system"]
    assert kw["json_schema"] is gti._IDEAS_SCHEMA
    # The divergent call: hotter than the 0.7 brief call, and a budget big
    # enough that 2.5-pro's hidden thinking tokens cannot starve the answer.
    assert kw["temperature"] >= 0.85
    assert kw["max_tokens"] >= 4000


def test_brainstorm_threads_feedback_and_exclude_into_the_call(monkeypatch):
    calls: list = []
    _mock(monkeypatch, calls=calls, reply=_reply({**_GOOD_IDEA, "title": "new one"}))
    out = _run(gti.brainstorm_ideas(trend_id=512, feedback="funnier, and shorter",
                                    exclude=["The 3am walk-out"]))
    prompt, _ = calls[0]
    assert "funnier, and shorter" in prompt
    assert "The 3am walk-out" in prompt and "ALREADY SHOWN AND TURNED DOWN" in prompt
    assert out["ideas"][0]["title"] == "new one"


def test_brainstorm_still_works_when_the_evidence_lookup_fails(monkeypatch):
    _mock(monkeypatch, reply=_reply(_GOOD_IDEA), store_boom=RuntimeError("no db"))
    assert len(_run(gti.brainstorm_ideas(trend_id=512))["ideas"]) == 1


def test_brainstorm_missing_trend_is_a_lookuperror(monkeypatch):
    _mock(monkeypatch, reply=_reply(_GOOD_IDEA), trend=None)
    with pytest.raises(LookupError):
        _run(gti.brainstorm_ideas(trend_id=999))


def test_brainstorm_bad_trend_id_is_a_valueerror(monkeypatch):
    _mock(monkeypatch, reply=_reply(_GOOD_IDEA))
    with pytest.raises(ValueError):
        _run(gti.brainstorm_ideas(trend_id="not-a-number"))  # type: ignore[arg-type]


def test_brainstorm_billing_failure_is_ideation_unavailable(monkeypatch):
    _mock(monkeypatch, boom=RuntimeError("billing is disabled for project x"))
    with pytest.raises(gti.IdeationUnavailable) as ei:
        _run(gti.brainstorm_ideas(trend_id=512))
    # It IS a RuntimeError, which is exactly why the router must catch it FIRST.
    assert isinstance(ei.value, RuntimeError)


def test_brainstorm_transient_failure_is_a_plain_runtimeerror(monkeypatch):
    _mock(monkeypatch, boom=RuntimeError("504 gateway timeout"))
    with pytest.raises(RuntimeError) as ei:
        _run(gti.brainstorm_ideas(trend_id=512))
    assert not isinstance(ei.value, gti.IdeationUnavailable)


def test_brainstorm_unreadable_and_empty_replies_are_runtimeerrors(monkeypatch):
    for reply in ({"_raw": "```json {"}, "not a dict", {"ideas": []},
                  _reply({"title": "", "what_you_see": ""})):
        _mock(monkeypatch, reply=reply)
        with pytest.raises(RuntimeError) as ei:
            _run(gti.brainstorm_ideas(trend_id=512))
        assert not isinstance(ei.value, gti.IdeationUnavailable)


# =========================================================================== #
# 6. generate_prefill with a picked idea
# =========================================================================== #

def test_prefill_with_an_idea_orders_the_model_to_build_that_idea(monkeypatch):
    calls: list = []
    _mock(monkeypatch, calls=calls, reply={"title": "The 3am walk-out",
                                           "topic": "A student walks out."})
    idea = {**_GOOD_IDEA, "id": "idea-2"}
    out = _run(gti.generate_prefill(trend_id=512, tool="poster", idea=idea))
    prompt, kw = calls[0]
    assert "THE IDEA THE TEAM PICKED — build this, do not invent a different one:" in prompt
    assert "idea: The 3am walk-out" in prompt
    assert "what you see: A student stands up mid-sentence" in prompt
    assert "why it works: Everyone knows the feeling" in prompt
    assert "not to improve on it." in prompt
    # The block sits above the tool brief, and the closing line points at it.
    assert prompt.index("THE IDEA THE TEAM PICKED") < prompt.index(ti._TOOL_BRIEFS["poster"])
    assert prompt.rstrip().endswith("Write the picked idea as this tool's brief now. "
                                    "Fill only the fields this tool asks for.")
    assert kw["system"] is gti._SYSTEM and out["post_id"] == 512


def test_prefill_never_prints_the_ideas_own_tool_or_format(monkeypatch):
    # The caller already chose the destination tool; a second, contradicting
    # format instruction is how a poster brief ends up describing a video.
    calls: list = []
    _mock(monkeypatch, calls=calls, reply={"title": "t", "topic": "x"})
    _run(gti.generate_prefill(trend_id=512, tool="poster",
                              idea={**_GOOD_IDEA, "tool": "broll", "format": "reel"}))
    prompt, _ = calls[0]
    block = prompt.split("THE IDEA THE TEAM PICKED")[1].split("TOOL:")[0]
    assert "broll" not in block and "reel" not in block


def test_prefill_without_an_idea_is_unchanged(monkeypatch):
    calls: list = []
    _mock(monkeypatch, calls=calls, reply={"title": "t", "topic": "x"})
    out = _run(gti.generate_prefill(trend_id=512, tool="poster"))
    prompt, kw = calls[0]
    assert "THE IDEA THE TEAM PICKED" not in prompt
    assert prompt.rstrip().endswith("Write 42's version of this trend now. "
                                    "Fill only the fields this tool asks for.")
    assert prompt == gti.build_prompt(trend=_TREND, sightings=_SIGHTINGS, tool="poster")
    assert kw["temperature"] == 0.7 and kw["max_tokens"] >= 6000
    assert out["tool"] == "poster" and out["title"] == "t"


def test_prefill_ignores_a_non_dict_idea(monkeypatch):
    calls: list = []
    _mock(monkeypatch, calls=calls, reply={"title": "t", "topic": "x"})
    bad: dict = "idea-1"  # type: ignore[assignment]
    _run(gti.generate_prefill(trend_id=512, tool="poster", idea=bad))
    assert "THE IDEA THE TEAM PICKED" not in calls[0][0]


def test_prefill_idea_block_tolerates_missing_fields():
    p = gti.build_prompt(trend=_TREND, sightings=None, tool="poster",
                         idea={"id": "idea-1", "title": "just a name"})
    assert "idea: just a name" in p
    assert "what you see:" not in p and "why it works:" not in p


def test_prefill_unknown_tool_is_still_a_valueerror(monkeypatch):
    _mock(monkeypatch, reply={})
    with pytest.raises(ValueError):
        _run(gti.generate_prefill(trend_id=512, tool="tiktok", idea=_GOOD_IDEA))


# =========================================================================== #
# 7. Router wiring
# =========================================================================== #

def test_router_exposes_the_brainstorm_endpoint_and_the_optional_idea():
    paths = {r.path: sorted(r.methods) for r in router_mod.router.routes}
    assert paths["/api/trends/general/items/{trend_id}/brainstorm"] == ["POST"]
    # Additive: an existing caller that sends only {"tool": …} still validates.
    body = router_mod.TrendIdeasRequest(tool="poster")
    assert body.idea is None
    assert router_mod.TrendIdeasRequest(tool="poster", idea=dict(_GOOD_IDEA)).idea
    empty = router_mod.TrendBrainstormRequest()
    assert empty.feedback is None and empty.exclude is None


def test_router_catches_ideation_unavailable_before_runtimeerror():
    # The taxonomy is the contract: IdeationUnavailable IS a RuntimeError, so
    # the order of the except clauses is what decides 422 vs 502.
    import inspect
    for fn in (router_mod.general_item_brainstorm, router_mod.general_item_ideas):
        src = inspect.getsource(fn)
        assert src.index("IdeationUnavailable") < src.index("except RuntimeError")
        assert src.index("except LookupError") < src.index("except RuntimeError")
    assert issubclass(gti.IdeationUnavailable, RuntimeError)


# =========================================================================== #
# 8. Google Search grounding in the Vertex wrapper
#
# The owner clicked "Make this" on `#spidermanbrandnewday` and got three ideas
# about "a fresh start". The hashtag is the title of the 2026 film Spider-Man:
# Brand New Day — the model had read a LABEL as a DESCRIPTION. No prompt fixes
# that on its own; the model has to look it up.
# =========================================================================== #

class _FakeWeb:
    def __init__(self, title, uri):
        self.title, self.uri = title, uri


class _FakeChunk:
    def __init__(self, title, uri):
        self.web = _FakeWeb(title, uri)


class _FakeCandidate:
    def __init__(self, chunks):
        self.grounding_metadata = type("M", (), {"grounding_chunks": chunks})()


class _FakeResp:
    def __init__(self, candidates):
        self.candidates = candidates


def test_grounding_is_off_by_default_and_the_existing_config_is_unchanged():
    base = dict(system="s", json_schema={"type": "object"}, temperature=0.7,
                max_tokens=6000, thinking_budget=None)
    cfg = vc._text_config_kwargs(**base)          # no `search` → the old caller
    assert cfg == vc._text_config_kwargs(**base, search=False)
    # EXACTLY the keys gemini_text always built — nothing added, nothing renamed.
    assert set(cfg) == {"temperature", "max_output_tokens", "system_instruction",
                        "response_mime_type", "response_schema"}
    assert "tools" not in cfg
    assert cfg["response_schema"] is base["json_schema"]
    assert cfg["max_output_tokens"] == 6000


def test_search_attaches_the_google_search_tool_and_nothing_else():
    cfg = vc._text_config_kwargs(system=None, json_schema=None, temperature=0.2,
                                 max_tokens=3000, thinking_budget=None, search=True)
    assert len(cfg["tools"]) == 1
    assert cfg["tools"][0].google_search is not None
    assert "response_schema" not in cfg and "response_mime_type" not in cfg


def test_grounding_and_a_json_schema_together_are_refused():
    # Vertex rejects/degrades structured output with a search tool attached, so
    # nobody gets to discover that in production.
    with pytest.raises(ValueError) as ei:
        vc._text_config_kwargs(system=None, json_schema={"type": "object"},
                               temperature=0.4, max_tokens=2000,
                               thinking_budget=None, search=True)
    assert "json_schema" in str(ei.value)
    # …and at the public door too, BEFORE the retry: an argument error that can
    # never become valid must not be retried four times with backoff.
    with pytest.raises(ValueError):
        _run(vc.gemini_text("x", search=True, json_schema={"type": "object"}))
    assert not hasattr(vc.gemini_text, "retry")       # the wrapper, not the retried half
    assert hasattr(vc._gemini_text_call, "retry")     # …which still has it


def test_grounding_sources_are_read_off_the_response_and_deduped():
    resp = _FakeResp([_FakeCandidate([
        _FakeChunk("Marvel", "https://example.com/bnd"),
        _FakeChunk("Marvel again", "https://example.com/bnd"),   # same page
        _FakeChunk("Variety", "https://example.com/cast"),
        _FakeChunk("", ""),                                       # junk
    ])])
    assert vc.grounding_sources(resp) == [
        {"title": "Marvel", "uri": "https://example.com/bnd"},
        {"title": "Variety", "uri": "https://example.com/cast"},
    ]


def test_grounding_sources_on_an_ungrounded_response_is_empty_not_an_error():
    for resp in (_FakeResp([]), _FakeResp(None), object()):
        assert vc.grounding_sources(resp) == []


# =========================================================================== #
# 9. research_trend — look it up before recommending
# =========================================================================== #

def test_the_literal_reading_rule_is_in_every_system_prompt_this_module_sends():
    for system in (gti._SYSTEM, gti._BRAINSTORM_SYSTEM):
        assert gti.NAME_IS_A_LABEL_RULE in system
        assert gti.CONNECT_TO_42_RULE in system
    rule = gti.NAME_IS_A_LABEL_RULE
    assert rule.startswith("NEVER READ THE NAME LITERALLY.")
    # The owner's actual bug, kept as the worked example.
    assert "#spidermanbrandnewday" in rule and "the title of a Spider-Man film" in rule
    assert "fresh starts" in rule and "WRONG ANSWER" in rule
    assert "#rushready" in rule
    # And the other half of what they asked for: the subject INSIDE 42.
    assert "Spider-Man at 42" in gti.CONNECT_TO_42_RULE
    assert "not a games studio" in gti.CONNECT_TO_42_RULE


def test_the_research_prompt_hands_over_the_raw_material_not_a_paraphrase():
    p = gti.build_research_prompt(trend=_TREND, sightings=_SIGHTINGS)
    assert "THE NAME, EXACTLY AS WE COLLECTED IT:\n  Nihilist Penguin meme" in p
    assert "TREAT THIS AS UNRELIABLE" in p
    assert "our earlier guess at what it is: A penguin leaves the colony" in p
    assert "- Reddit · global · the penguin that gave up · 4,200 posts" in p
    assert "https://knowyourmeme.com/memes/nihilist-penguin" in p
    assert p.rstrip().endswith("could name it out loud.")


def test_the_research_system_asks_what_kind_of_thing_it_is_and_forbids_guessing():
    s = gti._RESEARCH_SYSTEM
    assert "A hashtag or a sound title is a LABEL for a thing" in s
    assert "a film, a TV show, a game" in s
    assert "not clear from the search" in s and "Do not invent." in s
    assert "WHAT IT IS:" in s and "WHY PEOPLE POST:" in s


def test_parse_research_splits_the_two_blocks_and_keeps_the_sources():
    out = gti.parse_research(_RESEARCH_TEXT, _RESEARCH_SOURCES)
    assert out["what_it_is"].startswith("#spidermanbrandnewday is the hashtag")
    assert "fresh starts" in out["what_it_is"]
    assert out["why_people_post"] == ("Fans react to the trailer and argue about "
                                      "the casting.")
    assert out["sources"] == [{"title": "Marvel", "uri": "https://example.com/bnd"}]
    assert out["researched_at"] and out["cached"] is False


def test_parse_research_survives_markdown_missing_labels_and_junk():
    # Bold labels — the model adds them however hard you ask it not to.
    bold = gti.parse_research("**WHAT IT IS:** A film.\n**WHY PEOPLE POST:** Fun.")
    assert bold["what_it_is"] == "A film." and bold["why_people_post"] == "Fun."
    # No labels at all: keep the answer rather than bin it.
    assert gti.parse_research("It is a film.")["what_it_is"] == "It is a film."
    # Nothing usable → the empty shape, never None.
    for junk in ("", "   ", None):
        assert gti.parse_research(junk) == gti.empty_research()
    assert gti.parse_research("WHAT IT IS: x", "not a list")["sources"] == []
    long = gti.parse_research("WHAT IT IS: " + "z" * 5000)
    assert len(long["what_it_is"]) == gti.MAX_RESEARCH_WHAT


def test_research_runs_one_grounded_call_and_caches_it(monkeypatch):
    seen: list = []
    _mock(monkeypatch, reply=_reply(_GOOD_IDEA), research=_RESEARCH_TEXT,
          research_calls=seen)
    first = _run(gti.research_trend(trend_id=512))
    assert first["cached"] is False
    assert "Spider-Man: Brand New Day" in first["what_it_is"]
    assert first["sources"][0]["uri"] == "https://example.com/bnd"
    assert len(seen) == 1
    # No json_schema went anywhere near it — grounding forbids one.
    assert "json_schema" not in seen[0][1]
    assert seen[0][1]["system"] is gti._RESEARCH_SYSTEM
    assert seen[0][1]["max_tokens"] >= 4000

    # Pressing "Make this" again must not pay for a second search.
    again = _run(gti.research_trend(trend_id=512))
    assert len(seen) == 1 and again["cached"] is True
    assert again["what_it_is"] == first["what_it_is"]


def test_research_force_bypasses_the_cache(monkeypatch):
    seen: list = []
    _mock(monkeypatch, research=_RESEARCH_TEXT, research_calls=seen)
    _run(gti.research_trend(trend_id=512))
    out = _run(gti.research_trend(trend_id=512, force=True))
    assert len(seen) == 2 and out["cached"] is False


def test_research_prefers_a_lookup_already_saved_on_the_row(monkeypatch):
    # The read path already honours `metadata.research`, so the day the store
    # grows a metadata setter the cache is durable with no change here.
    saved = {"what_it_is": "The 2026 Spider-Man film.",
             "why_people_post": "Trailer reactions.",
             "sources": [{"title": "Marvel", "uri": "https://example.com/x"}],
             "researched_at": datetime.now(timezone.utc).isoformat()}
    seen: list = []
    _mock(monkeypatch, trend={**_TREND, "metadata": {"research": saved}},
          research=_RESEARCH_TEXT, research_calls=seen)
    out = _run(gti.research_trend(trend_id=512))
    assert out["cached"] is True and out["what_it_is"] == "The 2026 Spider-Man film."
    assert not seen                                   # nothing was searched


def test_research_ignores_a_stale_or_malformed_row_cache(monkeypatch):
    stale = {"what_it_is": "old", "researched_at": "1999-01-01T00:00:00+00:00"}
    for meta in ({"research": stale}, {"research": {"what_it_is": ""}},
                 {"research": "nonsense"}, {}, None, "not a dict"):
        _mock(monkeypatch, trend={**_TREND, "metadata": meta},
              research=_RESEARCH_TEXT)
        assert "Spider-Man: Brand New Day" in _run(
            gti.research_trend(trend_id=512))["what_it_is"]


def test_a_failed_lookup_is_never_fatal(monkeypatch):
    for boom in (RuntimeError("no grounding for you"),
                 asyncio.TimeoutError(),
                 gti.VertexBudgetExceededError("cap reached")):
        _mock(monkeypatch, research_boom=boom)
        assert _run(gti.research_trend(trend_id=512)) == gti.empty_research()
    # An empty or unparseable answer is the same non-event.
    _mock(monkeypatch, research="")
    assert _run(gti.research_trend(trend_id=512)) == gti.empty_research()


def test_research_can_be_switched_off(monkeypatch):
    seen: list = []
    _mock(monkeypatch, research=_RESEARCH_TEXT, research_calls=seen)
    # `config` is a frozen dataclass — swap the whole object, not a field.
    monkeypatch.setattr(gti, "config",
                        replace(gti.config, general_trends_research=False))
    assert _run(gti.research_trend(trend_id=512)) == gti.empty_research()
    assert not seen


def test_research_keeps_this_modules_error_taxonomy(monkeypatch):
    _mock(monkeypatch, trend=None, research=_RESEARCH_TEXT)
    with pytest.raises(LookupError):
        _run(gti.research_trend(trend_id=999))
    _mock(monkeypatch, research=_RESEARCH_TEXT)
    with pytest.raises(ValueError):
        _run(gti.research_trend(trend_id="nope"))     # type: ignore[arg-type]


# =========================================================================== #
# 10. The research block in the prompts, and the wiring
# =========================================================================== #

# The block's FULL heading. The ideas brief also mentions "WHAT THIS ACTUALLY
# IS" (it points the model at the block), so a bare substring proves nothing.
_BLOCK_HEAD = "WHAT THIS ACTUALLY IS (looked up just now, with a live web search):"

_RESEARCH = {
    "what_it_is": "The 2026 film Spider-Man: Brand New Day.",
    "why_people_post": "Fans arguing about the casting.",
    "sources": [{"title": "Marvel", "uri": "https://example.com/bnd"}],
    "researched_at": "2026-08-20T00:00:00+00:00", "cached": False,
}


def test_the_research_block_is_in_the_prompt_and_outranks_the_stored_summary():
    p = gti.build_ideas_prompt(trend=_TREND, sightings=_SIGHTINGS,
                               research=_RESEARCH)
    assert _BLOCK_HEAD in p
    assert "what it is: The 2026 film Spider-Man: Brand New Day." in p
    assert "why people post it: Fans arguing about the casting." in p
    assert "source: Marvel — https://example.com/bnd" in p
    assert "THIS BLOCK\n  WINS" in p or "THIS BLOCK WINS" in p
    # It sits between the evidence and the scorer's notes, and it demotes them.
    assert p.index("WHERE WE SAW IT") < p.index(_BLOCK_HEAD)
    assert p.index(_BLOCK_HEAD) < p.index("WHAT THE SCORER")
    assert "written from the name with NO search" in p


def test_no_research_leaves_both_prompts_exactly_as_they_were():
    for research in (None, gti.empty_research(), {}, "nonsense"):
        ideas = gti.build_ideas_prompt(trend=_TREND, sightings=_SIGHTINGS,
                                       research=research)
        assert _BLOCK_HEAD not in ideas
        assert ideas == gti.build_ideas_prompt(trend=_TREND, sightings=_SIGHTINGS)
        brief = gti.build_prompt(trend=_TREND, sightings=_SIGHTINGS, tool="poster",
                                 research=research)
        assert brief == gti.build_prompt(trend=_TREND, sightings=_SIGHTINGS,
                                         tool="poster")
    # …and the scorer's block keeps its plain heading when nothing disproved it.
    plain = gti.build_ideas_prompt(trend=_TREND, sightings=None)
    assert "WHAT THE SCORER ALREADY WORKED OUT:" in plain


def test_the_ideas_brief_forbids_building_on_the_words_in_the_name():
    p = gti.build_ideas_prompt(trend=_TREND, sightings=None)
    assert "The name is a LABEL, not a description." in p
    assert "must put THAT subject inside 42" in p
    assert "only reuses the words in the name" in p


def test_brainstorm_looks_it_up_first_and_returns_what_it_found(monkeypatch):
    calls: list = []
    seen: list = []
    _mock(monkeypatch, calls=calls, research=_RESEARCH_TEXT, research_calls=seen,
          reply=_reply(_GOOD_IDEA))
    out = _run(gti.brainstorm_ideas(trend_id=512))
    assert len(seen) == 1                       # searched BEFORE generating
    assert "Spider-Man: Brand New Day" in calls[0][0]      # …and it reached the prompt
    assert _BLOCK_HEAD in calls[0][0]
    # The dialog's proof that it looked instead of guessed.
    assert "Spider-Man: Brand New Day" in out["research"]["what_it_is"]
    assert out["research"]["sources"][0]["title"] == "Marvel"


def test_brainstorm_still_works_when_the_lookup_fails(monkeypatch):
    calls: list = []
    _mock(monkeypatch, calls=calls, research_boom=RuntimeError("search down"),
          reply=_reply(_GOOD_IDEA))
    out = _run(gti.brainstorm_ideas(trend_id=512))
    assert len(out["ideas"]) == 1                          # degraded, not broken
    assert _BLOCK_HEAD not in calls[0][0]
    assert out["research"] == gti.empty_research()


def test_the_brief_call_reuses_the_lookup_instead_of_paying_again(monkeypatch):
    calls: list = []
    seen: list = []
    _mock(monkeypatch, calls=calls, research=_RESEARCH_TEXT, research_calls=seen,
          reply=_reply(_GOOD_IDEA))
    _run(gti.brainstorm_ideas(trend_id=512))
    _mock(monkeypatch, calls=calls, research=_RESEARCH_TEXT, research_calls=seen,
          reply={"title": "t", "topic": "x"})
    gti._RESEARCH_CACHE.clear()                            # _mock cleared it; re-seed
    _run(gti.research_trend(trend_id=512))
    before = len(seen)
    _run(gti.generate_prefill(trend_id=512, tool="poster", idea=_GOOD_IDEA))
    assert len(seen) == before                             # cache hit, no new search
    assert _BLOCK_HEAD in calls[-1][0]                     # and the brief still sees it


if __name__ == "__main__":  # pragma: no cover
    sys.exit(pytest.main([__file__, "-q"]))
