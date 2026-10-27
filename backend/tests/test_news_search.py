"""Industry Signal - the pure half of the pipeline.

No network and no model anywhere in this file. Every function under test is a
plain transformation, which is deliberate: the parts that CANNOT be tested
without a model (does it find good stories, does the rewrite read well) are the
parts a human judges by reading a morning's output, and the parts that can be
pinned down here are the ones that quietly rot.

THE PROMPT RULES ARE NOT TESTED HERE, THEY ARE MEASURED. Every shaping decision
in `SEARCH_SYSTEM` came from live calls on 2026-09-05 and the findings are
recorded in that module's docstring. What IS tested here is everything that
happens to the model's answer afterwards.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import inspect

import pytest

from app.services import news_ingest as ni
from app.services import news_search as ns
from app.services import news_store as nst
from app.services.trend_scoring import compute_opportunity


# =========================================================================== #
# 1. The grounded prose parser
# =========================================================================== #

_BLOCK = """HEADLINE: LAUSD restricts all students from using AI tools
OUTLET: K-12 Dive
WHEN: 2026-09-03
LINK: https://www.k12dive.com/news/lausd-restricts/725357/
WHAT HAPPENED: Following New York City, the district restricted access.
The ban is temporary while policy is reviewed."""


def test_a_well_formed_block_becomes_one_story():
    got = ns.parse_candidates(_BLOCK)
    assert len(got) == 1
    c = got[0]
    assert c["headline"] == "LAUSD restricts all students from using AI tools"
    assert c["outlet"] == "K-12 Dive"
    assert c["published"] == "2026-09-03"
    assert c["url"] == "https://www.k12dive.com/news/lausd-restricts/725357/"
    # The wrapped second line is joined rather than dropped.
    assert "temporary while policy is reviewed" in c["summary"]


def test_a_new_headline_starts_a_new_story():
    got = ns.parse_candidates(_BLOCK + "\n\n" + _BLOCK.replace("LAUSD", "NYC"))
    assert len(got) == 2
    assert got[1]["headline"].startswith("NYC")


@pytest.mark.parametrize("label", [
    "**HEADLINE:**", "HEADLINE :", "## HEADLINE:", "*HEADLINE*:", "headline:",
])
def test_the_labels_survive_markdown_and_case(label):
    """Under load the model bolds its own labels. Throwing away a real story
    over an asterisk would be the parser deciding what counts as news."""
    got = ns.parse_candidates(f"{label} Something happened\nLINK: https://a.com/x")
    assert len(got) == 1
    assert got[0]["headline"] == "Something happened"


def test_a_refusal_yields_no_stories_rather_than_one_bad_one():
    # Measured: this is verbatim what a prompt that pinned "today" and demanded
    # the last 48 hours came back with.
    assert ns.parse_candidates(
        "I could not find any stories from the last 48 hours that met the criteria."
    ) == []


def test_a_block_with_no_link_is_dropped():
    # A headline with no address cannot be read or checked, so it is not a story.
    assert ns.parse_candidates("HEADLINE: Real enough\nOUTLET: Somebody") == []


def test_a_block_with_no_headline_is_dropped():
    assert ns.parse_candidates("OUTLET: Somebody\nLINK: https://a.com/x") == []


@pytest.mark.parametrize("raw", ["", None, "   \n\n  "])
def test_an_empty_reply_is_empty_not_an_exception(raw):
    assert ns.parse_candidates(raw) == []


def test_trailing_punctuation_is_stripped_off_a_link():
    got = ns.parse_candidates("HEADLINE: X\nLINK: <https://a.com/x>.")
    assert got[0]["url"] == "https://a.com/x"


# =========================================================================== #
# 2. The grounding cross-check - the whole verification layer
# =========================================================================== #

_SOURCES = [
    {"title": "edweek.org",
     "uri": "https://vertexaisearch.cloud.google.com/grounding-api-redirect/AUZ"},
    {"title": "mit.edu",
     "uri": "https://vertexaisearch.cloud.google.com/grounding-api-redirect/BXY"},
]


def test_the_hosts_come_off_both_the_title_and_the_uri():
    hosts = ns.grounded_hosts(_SOURCES)
    assert "edweek.org" in hosts and "mit.edu" in hosts
    # Relying on the title alone would break the day Vertex puts a headline
    # there instead of a domain.
    assert "vertexaisearch.cloud.google.com" in hosts


def test_a_story_from_a_page_the_search_read_is_kept():
    hosts = ns.grounded_hosts(_SOURCES)
    assert ns.is_grounded("https://www.edweek.org/technology/x/2026/08", hosts)


def test_a_subdomain_counts_as_the_same_publisher():
    # The list holds `mit.edu` while the story links `news.mit.edu`.
    assert ns.is_grounded("https://news.mit.edu/2026/x", ns.grounded_hosts(_SOURCES))


def test_a_fabricated_link_is_rejected():
    """The measured failure this check exists for.

    Told never to give a redirect address and left with one grounding source,
    the live model invented `youtube.com/watch?v=VIDEO_ID_PLACEHOLDER`. Nothing
    else in the pipeline would have caught it."""
    hosts = ns.grounded_hosts(_SOURCES)
    assert not ns.is_grounded(
        "https://www.youtube.com/watch?v=VIDEO_ID_PLACEHOLDER", hosts)


def test_a_redirect_address_is_trusted_because_it_is_a_grounding_address():
    assert ns.is_grounded(
        "https://vertexaisearch.cloud.google.com/grounding-api-redirect/ZZZ",
        ns.grounded_hosts(_SOURCES))


@pytest.mark.parametrize("url", ["", "not a url", None])
def test_an_unreadable_link_is_not_grounded(url):
    assert not ns.is_grounded(url, ns.grounded_hosts(_SOURCES))


def test_sources_that_are_not_dicts_do_not_crash_the_check():
    assert ns.grounded_hosts([None, "nonsense", 42, {}]) == set()


def test_a_placeholder_link_is_rejected_even_from_a_real_publisher():
    """The gap `is_grounded` cannot close, and the case that actually happened.

    A live run put `https://www.youtube.com/watch?v=null` at the TOP of the
    feed: youtube.com genuinely was in the grounding list, so the domain check
    passed it and the model had simply not carried the video id through."""
    assert ns.looks_like_a_placeholder("https://www.youtube.com/watch?v=null")
    assert ns.looks_like_a_placeholder(
        "https://www.youtube.com/watch?v=VIDEO_ID_PLACEHOLDER")
    assert ns.looks_like_a_placeholder("https://www.youtube.com/watch?v=")
    assert ns.looks_like_a_placeholder("https://a.com/<url>")


@pytest.mark.parametrize("url", [
    "https://www.k12dive.com/news/lausd-restricts/725357/",
    "https://mitsloan.mit.edu/ideas-made-to-matter/ai-boosts-worker-productivity",
    "https://www.youtube.com/watch?v=n-r-yOqYt_I",
    # A real slug that merely CONTAINS a suspicious word. Matching on whole
    # segments is what keeps this one.
    "https://www.bbc.com/news/ai-test-results-2026",
])
def test_a_real_address_is_not_mistaken_for_a_placeholder(url):
    assert not ns.looks_like_a_placeholder(url)


@pytest.mark.parametrize("url", ["", None, "   "])
def test_an_empty_link_counts_as_a_placeholder(url):
    assert ns.looks_like_a_placeholder(url)


# =========================================================================== #
# 3. The jargon backstop - the user's core requirement, so it gets the most cases
# =========================================================================== #

# Every one of these is from the plan's own before/after table.
PLAIN = [
    "The company behind ChatGPT made it much better at maths",
    "A new AI can now write and fix code on its own",
    "Students who learned with an AI tutor did better in tests",
    "Some companies are hiring fewer junior developers because of AI",
    "New York City schools have banned AI chatbots for younger children",
    "AI is reducing the number of jobs for recent graduates",
]

TECHNICAL = [
    "OpenAI ships GPT-5.2 with improved MMLU scores",
    "Anthropic releases an agentic SWE-bench SOTA coding model",
    "Study finds LLM tutors improve learning outcomes by 14%",
    "Google adds multimodal context caching to Gemini API",
    "Enterprises report a cut in headcount after fine-tuning their models",
    "Startup raises a funding round at a $2 billion valuation",
]


@pytest.mark.parametrize("headline", PLAIN)
def test_plain_headlines_pass(headline):
    assert ns.jargon_reason(headline) is None, headline


@pytest.mark.parametrize("headline", TECHNICAL)
def test_technical_headlines_are_rejected(headline):
    assert ns.jargon_reason(headline) is not None, headline


def test_the_reason_is_a_sentence_a_person_can_read():
    """It is printed in the run log where a non-technical owner reads it, so
    "too technical" is useless and "has a version number" is actionable."""
    assert "version number" in ns.jargon_reason("Meet GPT-5.2, the new model")
    assert "MMLU" in ns.jargon_reason("New model tops the MMLU leaderboard")
    assert "benchmark" in ns.jargon_reason("A benchmark for coding tools")



# ---------------------------------------------------------------------------
# The score floor: dull stories are not written at all
# ---------------------------------------------------------------------------

def test_there_is_a_floor_below_which_a_story_is_not_worth_a_row():
    """The judge drops what is WRONG for the audience, not what is merely dull.

    Without a floor, a story scored 20 was written and then hidden behind a fit
    bar in the feed - a quarter of the table was rows nobody would ever be
    shown, and the feed needed a filter to say so. The floor moves that decision
    to the one place that has the score and has to justify itself in the run log.
    """
    from app.services import news_search

    assert news_search.MIN_RELEVANCE_TO_KEEP > 0
    src = inspect.getsource(news_search.find_stories)
    assert "MIN_RELEVANCE_TO_KEEP" in src
    # Dropped WITH a reason, like every other drop, so a morning that finds
    # nothing interesting says why rather than looking like an outage.
    assert 'drop("not interesting enough to post")' in src


def test_the_floor_is_applied_before_the_row_is_built():
    """A story dropped for its score must not reach `stories`, and the score the
    row carries must be the one the floor was measured against."""
    from app.services import news_search

    src = inspect.getsource(news_search.find_stories)
    floor_at = src.index("MIN_RELEVANCE_TO_KEEP")
    append_at = src.index("stories.append(")
    assert floor_at < append_at, "the floor must run before the row is built"
    assert '"relevance": relevance,' in src


# ---------------------------------------------------------------------------
# What a rejected story is allowed to leave behind
# ---------------------------------------------------------------------------

def test_the_judges_own_words_about_a_rejected_story_are_never_kept():
    """The judge writes a `reject_reason` in the first person: "Do not post; this
    is about corporate investment strategy, not technology students can use."

    That sentence used to become a key in `drops`, which the Configure screen
    printed. The result was a line on screen that read like a post telling the
    reader not to post it, about a story they were never shown and could do
    nothing about. The reason is still ASKED for, because a judge made to justify
    a refusal refuses more carefully, but it goes to the debug log and no further.
    """
    from app.services import news_search

    src = inspect.getsource(news_search.find_stories)
    assert 'drop("not right for our audience")' in src
    assert "reject_reason" in src, "still asked for: it improves the judging"
    assert "log.debug" in src, "and it goes to the log, not to the run record"


def test_every_drop_reason_is_a_literal_written_here():
    """`drops` is a JSONB column and its keys reach the API. One f-string in a
    `drop()` call is enough to put model prose, or a headline, back on a screen,
    so this walks the actual call sites rather than trusting the reading.
    """
    import ast

    from app.services import news_search

    tree = ast.parse(inspect.getsource(news_search.find_stories))
    calls = [n for n in ast.walk(tree)
             if isinstance(n, ast.Call) and getattr(n.func, "id", None) == "drop"]
    assert len(calls) >= 8, "found no drop() calls; this test has gone stale"
    for call in calls:
        arg = call.args[0]
        assert isinstance(arg, ast.Constant) and isinstance(arg.value, str), (
            f"drop() takes a plain string only, got {ast.dump(arg)[:80]}")

def test_a_thirteen_word_headline_is_too_long():
    assert ns.jargon_reason(" ".join(["word"] * 13)) is not None
    assert ns.jargon_reason(" ".join(["word"] * 12)) is None


def test_short_abbreviations_are_allowed_because_people_read_them():
    for ok in ("AI", "UAE", "MIT", "BBC", "NYC"):
        assert ns.jargon_reason(f"Something happened at {ok} this week") is None, ok


@pytest.mark.parametrize("raw", ["", "   ", None])
def test_an_empty_headline_is_rejected(raw):
    assert ns.jargon_reason(raw) == "empty headline"


# =========================================================================== #
# 4. Dates
# =========================================================================== #

@pytest.mark.parametrize("raw,expect", [
    ("2026-09-03", (2026, 9, 3)),
    ("2026/09/03", (2026, 9, 3)),
    ("3 September 2026", (2026, 9, 3)),
    ("Sep 3, 2026", (2026, 9, 3)),
])
def test_the_spellings_a_model_actually_uses_are_read(raw, expect):
    got = ns.parse_when(raw)
    assert got is not None and (got.year, got.month, got.day) == expect
    assert got.tzinfo is not None      # naive datetimes break every comparison


@pytest.mark.parametrize("raw", ["", None, "last Tuesday", "recently"])
def test_an_unreadable_date_is_none_not_an_exception(raw):
    assert ns.parse_when(raw) is None


# =========================================================================== #
# 5. The row a story becomes
# =========================================================================== #

def _story(**kw) -> dict:
    base = {
        "url": "https://www.k12dive.com/news/x/725357/",
        "headline": "Major US school districts ban students from using AI tools",
        "original_headline": "LA Schools Join NYC in Blocking Student Access to Generative AI",
        "outlet": "K-12 Dive",
        "published": datetime(2026, 9, 3, tzinfo=timezone.utc),
        "why_it_matters": "Two of the largest school systems are stopping students.",
        "relevance": 75,
        "topic": "Schools and AI",
        "theme": "AI in school",
        "angle": "Ask students what they think",
        "tags": ["school"],
    }
    return {**base, **kw}


def test_the_card_shows_the_rewrite_and_keeps_the_publishers_own_words():
    """The whole point of the tab. `caption` is what the feed, the reports and
    the ideation prompt all read, so the plain version goes there; the original
    is kept so the detail view can show it next to the link and nothing is
    hidden."""
    row = ni.build_row(_story())
    assert row["caption"] == "Major US school districts ban students from using AI tools"
    assert row["metadata"]["original_headline"].startswith("LA Schools Join NYC")


def test_the_three_retired_signals_are_null_not_faked():
    row = ni.build_row(_story())
    assert row["outperformance"] is None   # a search cannot count outlets
    assert row["region"] is None           # the UAE focus is gone
    assert row["source"] == "news"


def test_a_news_row_ranks_on_relevance_alone():
    """The regression test for the shared-code change in `compute_opportunity`.

    Outperformance used to be the one signal never dropped from the blend, so a
    NULL scored 0.0 and still took 40% of the weight - which would have ranked
    every story in this tab near the bottom of a blend it does not join."""
    row = ni.build_row(_story(relevance=95))
    score = ni.score_of(_story(relevance=95))
    assert compute_opportunity({**row, **score}) == 95.0
    assert compute_opportunity({**ni.build_row(_story()), **ni.score_of(_story())}) == 75.0


def test_a_competitor_row_is_arithmetically_unchanged():
    """The other half of that change: competitor rows always carry an
    outperformance, so their ranking must be exactly what it always was."""
    assert compute_opportunity(
        {"outperformance": 4.0, "fit_score": 80, "uae_relevance": 60}) == 73.3
    assert compute_opportunity({"outperformance": 4.0}) == 73.25


def test_the_scores_are_written_at_ingest_so_there_is_no_second_pass():
    score = ni.score_of(_story(relevance=88))
    assert score["fit_score"] == 88
    assert score["uae_relevance"] is None
    assert score["theme"] == "AI in school"


def test_a_future_date_is_clamped_rather_than_trusted():
    """A publisher bug must not pin a story to the top of a recency sort."""
    ahead = datetime.now(timezone.utc) + timedelta(days=400)
    row = ni.build_row(_story(published=ahead))
    assert row["posted_at"] <= datetime.now(timezone.utc)


def test_an_estimated_date_is_flagged_rather_than_passed_off_as_the_publishers():
    assert ni.build_row(_story(date_is_estimated=True))["metadata"]["date_is_estimated"]
    assert not ni.build_row(_story())["metadata"]["date_is_estimated"]


# =========================================================================== #
# 6. Exact-duplicate URLs - code's half of the dedup split
# =========================================================================== #

def test_the_same_article_shared_three_ways_is_one_story():
    a = "https://www.Example.com/news/ai-in-schools/?utm_source=x&utm_medium=y"
    b = "http://example.com/news/ai-in-schools?fbclid=99"
    c = "https://example.com/news/ai-in-schools#top"
    assert ni.external_id(a) == ni.external_id(b) == ni.external_id(c)


def test_the_scheme_is_forced_because_http_and_https_are_one_article():
    assert ni.canonical_url("http://a.com/x") == ni.canonical_url("https://a.com/x")


def test_a_query_that_identifies_the_article_is_kept():
    """Stripping the whole query string would merge two different stories."""
    assert ni.external_id("https://a.com/p?id=1") != ni.external_id("https://a.com/p?id=2")
    assert "id=1" in ni.canonical_url("https://a.com/p?id=1&utm_source=z")


def test_the_key_is_namespaced_so_it_cannot_collide_with_a_real_post():
    assert ni.external_id("https://a.com/x").startswith("news:")


# =========================================================================== #
# 7. The brief
# =========================================================================== #

def test_the_shipped_brief_is_about_what_people_can_do_not_about_companies():
    """A brief naming companies returns press releases. This one names changes."""
    brief = nst.DEFAULT_BRIEF.lower()
    assert "learn" in brief and "careers" in brief
    for vendor in ("openai", "anthropic", "google", "deepmind"):
        assert vendor not in brief, vendor


@pytest.mark.parametrize("raw", ["", "   ", None])
def test_an_empty_brief_is_refused_rather_than_defaulted(raw):
    """The search prompt is built around it: a blank brief asks the model to
    find "AI news" with no test to apply, which is the failure this replaced."""
    with pytest.raises(ValueError):
        nst.clean_brief(raw)


def test_a_brief_is_collapsed_to_one_line():
    assert nst.clean_brief("  two   words\n\nmore  ") == "two words more"


def test_an_over_long_brief_is_refused_with_a_readable_reason():
    with pytest.raises(ValueError, match="too long"):
        nst.clean_brief("x" * (nst.MAX_BRIEF_CHARS + 1))


@pytest.mark.parametrize("bad", [0, 2, 21, 999, "x", None])
def test_a_count_outside_the_bounds_is_refused_never_clamped(bad):
    """Silently clamping is how a control ends up doing something other than
    what it says."""
    with pytest.raises(ValueError):
        nst.clean_stories_per_day(bad)


@pytest.mark.parametrize("ok", [3, 10, 20])
def test_a_count_inside_the_bounds_is_accepted(ok):
    assert nst.clean_stories_per_day(ok) == ok


def test_the_brief_and_the_recent_headlines_both_reach_the_search_prompt():
    p = ns.build_search_prompt(brief="only stories about robots",
                               recent_headlines=["A robot did a thing"], limit=5)
    assert "only stories about robots" in p
    assert "A robot did a thing" in p
    assert "last 7 days" in p       # the measured window; 48h returned nothing


def test_the_search_asks_for_more_candidates_than_it_will_keep():
    """Asking for exactly ten and keeping what passes means a strict judge
    starves the feed."""
    assert "up to 20" in ns.build_search_prompt(
        brief="x", recent_headlines=[], limit=10)


def test_the_candidate_ask_is_capped_however_many_stories_are_configured():
    p = ns.build_search_prompt(brief="x", recent_headlines=[], limit=999)
    assert f"up to {ns.MAX_CANDIDATES}" in p


def test_the_judge_prompt_carries_the_publishers_headline_to_rewrite_from():
    p = ns.build_judge_prompt(
        [{"headline": "OpenAI Launches GPT-6 Astra", "outlet": "Forbes",
          "published": "2026-09-03", "summary": "It can find security flaws."}],
        brief="x", recent_headlines=[])
    assert "headline as published: OpenAI Launches GPT-6 Astra" in p
    assert "It can find security flaws." in p
    assert "STORY 0" in p


# =========================================================================== #
# 8. Nothing is on a schedule
# =========================================================================== #

def test_there_is_no_schedule_left_to_drift():
    """The 7am daily run, and the `due`/`due_now` pair that drove it, were
    removed by request: a search nobody asked for spends money on mornings
    nobody reads. A search now starts one way, from `trigger_now`. These
    assertions are here so a future "helpful" reinstatement is a failing test
    rather than a surprise bill."""
    for gone in ("next_daily_run", "due", "due_now",
                 "DAILY_RUN_HOUR", "DAILY_RUN_TZ"):
        assert not hasattr(nst, gone), f"{gone} is back; the timer came with it"


def test_a_run_killed_mid_flight_can_be_closed():
    """A run row opens before the work and closes after, so a backend that dies
    in between leaves it `running` for ever. The screen reads "a search is
    running" off exactly that, which is what left the button spinning with
    nothing behind it."""
    assert nst.STALE_RUN_MINUTES >= 5
    assert callable(nst.fail_stale_runs)


def test_every_status_the_poller_writes_is_in_the_shared_vocabulary():
    """The screen renders one sentence per status, so a status invented in the
    worker and unknown to the UI renders as nothing at all."""
    for s in ("running", "ok", "empty", "ungrounded", "search_failed",
              "judge_failed", "no_budget", "unavailable"):
        assert s in nst.RUN_STATUSES


# =========================================================================== #
# 7. The websites we always check - the second search lane
# =========================================================================== #

def test_the_fixed_list_lane_shares_the_definition_of_news_with_the_open_one():
    """One literal for what counts as news, so the two lanes cannot drift."""
    assert ns._NEWS_RULES in ns.SEARCH_SYSTEM
    assert ns._NEWS_RULES in ns.SITES_SYSTEM
    assert "site:" in ns.SITES_SYSTEM, "a fixed list is read with site: searches"


def test_the_sites_prompt_names_every_site_with_its_search():
    prompt = ns.build_sites_prompt(sites=["theverge.com", "TechCrunch.com"],
                                   recent_headlines=["already here"], limit=6)
    assert "site:theverge.com" in prompt
    assert "site:techcrunch.com" in prompt
    assert "already here" in prompt
    assert str(ns.SITE_STORIES_PER_SITE) in prompt, "one site cannot fill the feed"


@pytest.mark.parametrize("url,expected", [
    ("https://www.theverge.com/ai/1", "theverge.com"),
    ("https://news.mit.edu/2026/x", "mit.edu"),          # a subdomain counts
    ("https://example.com/theverge.com", None),           # a path is not a host
    ("https://notheverge.com/a", None),                   # a suffix is not a subdomain
    ("", None),
])
def test_which_fixed_site_a_link_is_on(url, expected):
    assert ns.fixed_site_of(url, ["theverge.com", "mit.edu"]) == expected


def test_the_fixed_lane_drops_what_wandered_off_the_list():
    """Told to read the list and nothing else. A result from elsewhere is
    dropped with a literal reason, like every other drop."""
    import inspect

    src = inspect.getsource(ns.find_stories)
    assert 'drop("not from one of the websites we always check")' in src


def test_one_lane_failing_does_not_lose_the_other():
    """Both searches run side by side and each is judged on its own reply."""
    import inspect

    src = inspect.getsource(ns.find_stories)
    assert "asyncio.gather" in src and "return_exceptions=True" in src


# --- choose_stories ------------------------------------------------------------

def _s(headline, relevance, site=""):
    return {"headline": headline, "relevance": relevance, "fixed_site": site}


def test_under_the_cap_everything_is_kept_best_first():
    got = ns.choose_stories([_s("b", 50), _s("a", 90)], limit=10)
    assert [s["headline"] for s in got] == ["a", "b"]


def test_a_fixed_site_keeps_its_best_story_inside_the_cap():
    """Six open-web stories outscore The Verge's one. Without a reserved place
    the fixed list would never show on a busy day, and a fixed list that
    never shows is not a fixed list."""
    stories = [_s(f"web{i}", 90 - i) for i in range(6)] + [_s("verge", 45, "theverge.com")]
    got = ns.choose_stories(stories, limit=3, sites=["theverge.com"])
    assert [s["headline"] for s in got] == ["web0", "web1", "verge"]


def test_a_fixed_site_reserves_one_place_not_all_of_them():
    stories = [_s("web", 80), _s("v1", 70, "theverge.com"), _s("v2", 60, "theverge.com"),
               _s("v3", 50, "theverge.com")]
    got = ns.choose_stories(stories, limit=2, sites=["theverge.com"])
    assert [s["headline"] for s in got] == ["web", "v1"]


def test_a_fixed_site_with_nothing_this_week_reserves_nothing():
    stories = [_s("a", 90), _s("b", 80), _s("c", 70)]
    got = ns.choose_stories(stories, limit=2, sites=["theverge.com"])
    assert [s["headline"] for s in got] == ["a", "b"]


def test_many_fixed_sites_never_crowd_out_the_open_search():
    """Five websites against a cap of three: at most half the cap is theirs,
    and the places go to the best of their best. The open web keeps a seat."""
    sites = [f"s{i}.com" for i in range(5)]
    stories = [_s(f"x{i}", 50 + i, f"s{i}.com") for i in range(5)] + [_s("web", 99)]
    got = ns.choose_stories(stories, limit=3, sites=sites)
    assert [s["headline"] for s in got] == ["web", "x4", "x3"]


# =========================================================================== #
# 8. Who the story is about, and the angle on every card
# =========================================================================== #

def test_the_judge_is_asked_who_the_story_is_about():
    props = ns._JUDGE_SCHEMA["properties"]["stories"]["items"]["properties"]
    assert "company" in props and "company_site" in props
    assert "Claude is Anthropic" in ns.JUDGE_SYSTEM, "a product names its maker"


def test_an_angle_is_required_for_every_story():
    """The block on the card is on EVERY card now, so the judge may not skip it."""
    required = ns._JUDGE_SCHEMA["properties"]["stories"]["items"]["required"]
    assert "angle_for_42" in required
    import inspect

    src = inspect.getsource(ns.find_stories)
    assert "or why" in src, "and when it is empty anyway, why it matters stands in"


@pytest.mark.parametrize("raw,expected", [
    ("openai.com", "openai.com"),
    ("https://www.OpenAI.com/", "openai.com"),
    ("Not sure which company", ""),
    ("", ""),
    (None, ""),
])
def test_the_company_site_is_a_bare_host_or_nothing(raw, expected):
    assert ns.clean_host(raw) == expected


def test_the_row_carries_who_it_is_about_and_where_it_came_from():
    row = ni.build_row(_story(company="OpenAI", company_site="openai.com",
                              fixed_site="theverge.com"))
    assert row["metadata"]["company"] == "OpenAI"
    assert row["metadata"]["company_site"] == "openai.com"
    assert row["metadata"]["fixed_site"] == "theverge.com"
    blank = ni.build_row(_story())
    assert blank["metadata"]["company"] == "" and blank["metadata"]["company_site"] == ""


# =========================================================================== #
# 9. Company websites on the fixed list still face the same judge
# =========================================================================== #

def test_a_companys_own_announcements_count_on_the_fixed_list_and_still_face_the_test():
    low = ns.SITES_SYSTEM.lower()
    assert "openai.com" in low and "announcements" in low
    assert "test below still applies" in low, "the fixed list is not a back door"
    assert "benchmark table" in low and "pricing" in low
    assert ns._NEWS_RULES in ns.SITES_SYSTEM, "the same definition of news, word for word"


def test_a_fixed_site_story_is_judged_and_jargon_checked_like_any_other():
    """Both lanes land in one `checked` list and one judge call. There is no
    branch in the pipeline that lets a fixed-list story skip the judge or the
    plain-English backstop."""
    import inspect

    src = inspect.getsource(ns.find_stories)
    assert src.count("gemini_text(") == 1, "one judge call for every candidate"
    assert src.count("jargon_reason(") == 1, "one backstop, applied to all"
    assert "lane" not in src[src.index("# --- 3. judge"):], \
        "nothing after the cross-check tells the lanes apart"


# =========================================================================== #
# 10. Viral potential
# =========================================================================== #

def test_the_judge_scores_viral_potential_for_every_story():
    props = ns._JUDGE_SCHEMA["properties"]["stories"]["items"]["properties"]
    assert "viral_potential" in props and "viral_reason" in props
    required = ns._JUDGE_SCHEMA["properties"]["stories"]["items"]["required"]
    assert "viral_potential" in required
    assert ns._VIRAL_RULES in ns.JUDGE_SYSTEM
    assert "never let it copy relevance_score" in ns._VIRAL_RULES


def test_the_catch_up_scorer_reads_the_same_rules_as_the_judge():
    assert ns._VIRAL_RULES in ns.VIRALITY_SYSTEM
    assert ns._VIRAL_SCHEMA["properties"]["scores"]["items"]["required"] == [
        "index", "viral_potential"]


@pytest.mark.parametrize("raw,expected", [
    (85, 85), ("72", 72), (140, 100), (-5, 0), (None, 0), ("high", 0), (3.9, 3),
])
def test_a_score_field_is_clamped_to_0_100_or_zeroed(raw, expected):
    assert ns.clamp_score(raw) == expected


def test_the_row_carries_the_viral_potential_and_its_reason():
    row = ni.build_row(_story(viral=88, viral_reason="Everyone has a teacher story."))
    assert row["metadata"]["viral_potential"] == 88
    assert row["metadata"]["viral_reason"] == "Everyone has a teacher story."
    assert ni.build_row(_story())["metadata"]["viral_potential"] == 0


def test_the_virality_prompt_names_every_story_with_its_index():
    prompt = ns.build_virality_prompt([
        {"headline": "Schools ban AI", "why": "Two big systems stopped students."},
        {"headline": "A new AI tutor", "why": ""},
    ])
    assert "STORY 0" in prompt and "STORY 1" in prompt
    assert "Schools ban AI" in prompt and "Two big systems" in prompt
    assert "Score all 2 stories" in prompt


def test_parsing_the_scores_skips_what_cannot_be_trusted():
    reply = {"scores": [
        {"index": 0, "viral_potential": 91, "viral_reason": "  a   laugh "},
        {"index": 0, "viral_potential": 10},            # a repeat: first wins
        {"index": 7, "viral_potential": 50},            # outside the batch
        "not a dict",
        {"index": 1, "viral_potential": 250},           # clamped
    ]}
    assert ns.parse_virality(reply, count=3) == {0: (91, "a laugh"), 1: (100, "")}
    assert ns.parse_virality("garbage", count=3) == {}
    assert ns.parse_virality({"scores": None}, count=3) == {}


def test_the_viral_sort_puts_the_judges_number_on_top():
    from app.services import trend_scoring as ts

    assert "viral" in ts.SORTS
    news_hot = {"source": "news", "metadata": {"viral_potential": 90}, "fit_score": 40}
    news_dull = {"source": "news", "metadata": {"viral_potential": 35}, "fit_score": 95}
    unscored = {"source": "news", "metadata": {}, "fit_score": 99}
    ranked = ts.rank_posts([unscored, news_dull, news_hot], sort="viral")
    assert [r["metadata"].get("viral_potential") for r in ranked] == [90, 35, None]


def test_a_competitor_posts_outperformance_stands_in_for_its_virality():
    from app.services import trend_scoring as ts

    assert ts.viral_score({"outperformance": 4.0}) > ts.viral_score({"outperformance": 1.0}) > 0
    assert ts.viral_score({"metadata": {"viral_potential": "77"}}) == 77.0
    assert ts.viral_score({"metadata": {"viral_potential": "lots"}, "outperformance": None}) == 0.0
    assert ts.viral_score({}) == 0.0
