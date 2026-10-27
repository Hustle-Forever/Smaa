"""Tests for the Trend Finder — the competitor-watching / topic-finding engine.

Four modules under test, none of which may be touched from here:

  * ``app/services/trend_store.py``    — handle normalisation + validation
  * ``app/services/trend_scoring.py``  — the opportunity blend, ranking, prompt
                                         building and reply parsing
  * ``app/services/trend_finder.py``   — the scan engine (median → outperformance)
  * ``app/services/trend_research.py`` — AI account discovery + verification

Everything is mocked: no network, no database, no Instagram, no Gemini. The two
integration-shaped tests at the bottom (``scan_one`` / ``research_competitors``)
run the REAL code path with a fake store, a fake Graph client and a fake model,
so the arithmetic and the filtering are genuinely exercised rather than stubbed.

There is no pytest-asyncio in this venv, so coroutines are driven with
``asyncio.run`` via the ``_run`` helper, matching tests/test_video_edit_agent.py.

Two tests are labelled REGRESSION: they pin real bugs this suite found (a failed
scan blanking the competitor's post count, and an untrusted thin-sample baseline
ranking top of the feed), both since fixed in the services.

Runs standalone (``cd backend && python tests/test_trend_finder.py``) and under
pytest.
"""
from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

_BACKEND_ROOT = Path(__file__).resolve().parent.parent
if str(_BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(_BACKEND_ROOT))

from app.services import instagram_client as ig  # noqa: E402
from app.services import trend_finder as tf  # noqa: E402
from app.services import trend_ideation as ti  # noqa: E402
from app.services import trend_research as tr  # noqa: E402
from app.services import trend_scoring as ts  # noqa: E402
from app.services import trend_store as tstore  # noqa: E402


def _run(coro):
    return asyncio.run(coro)


# =========================================================================== #
# 1. trend_store.normalise_username / validate_username
#
# This is a SECURITY boundary, not tidying: instagram_client interpolates the
# handle straight into the Graph field DSL — business_discovery.username(X) —
# with no escaping, so anything that isn't [A-Za-z0-9._]{1,30} must RAISE.
# =========================================================================== #

def test_normalise_strips_at_whitespace_and_case():
    assert tstore.normalise_username("@Foo") == "foo"
    assert tstore.normalise_username("  foo  ") == "foo"
    assert tstore.normalise_username("FOO_bar.42") == "foo_bar.42"
    assert tstore.normalise_username("@42AbuDhabi/") == "42abudhabi"


def test_normalise_profile_urls():
    for raw in (
        "https://www.instagram.com/visitabudhabi/",
        "https://instagram.com/visitabudhabi",
        "www.instagram.com/VisitAbuDhabi/",
        "instagram.com/visitabudhabi",
        "HTTPS://WWW.INSTAGRAM.COM/VisitAbuDhabi/",
    ):
        assert tstore.normalise_username(raw) == "visitabudhabi", raw


def test_normalise_profile_urls_with_query_string():
    # The share sheet appends ?igsh=... / ?hl=en — both slash orders must work.
    assert tstore.normalise_username(
        "https://www.instagram.com/visitabudhabi/?hl=en") == "visitabudhabi"
    assert tstore.normalise_username(
        "https://www.instagram.com/visitabudhabi?igsh=abc123") == "visitabudhabi"
    assert tstore.normalise_username("@visitabudhabi?hl=en") == "visitabudhabi"


def test_normalise_empty_and_junk_returns_empty():
    assert tstore.normalise_username("") == ""
    assert tstore.normalise_username("   ") == ""
    assert tstore.normalise_username("@@@") == ""
    assert tstore.normalise_username(None) == ""  # type: ignore[arg-type]


def test_validate_accepts_real_handles():
    assert tstore.validate_username("@Visit_Abu.Dhabi") == "visit_abu.dhabi"
    assert tstore.validate_username("https://www.instagram.com/42abudhabi/") == "42abudhabi"
    assert tstore.validate_username("a") == "a"
    assert tstore.validate_username("a" * 30) == "a" * 30


@pytest.mark.parametrize("raw", [
    "",
    "   ",
    "@@@",
    "foo bar",              # space — would break out of the field DSL
    "foo\tbar",
    "foo(bar)",             # parentheses — the DSL's own call syntax
    "visitabudhabi).fields(id",   # the actual injection shape
    "foo{bar}",             # braces — the DSL's field-selection syntax
    "foo,bar",
    "foo-bar",
    "foo@bar",
    "a" * 31,               # Instagram handles cap at 30
    "http://instagram.com/foo",   # not one of the recognised prefixes -> "http:"
    "..",                   # punctuation-only is not a handle...
    "._.",
    "https://www.instagram.com/../../etc/passwd",   # ...and this normalises to ".."
])
def test_validate_rejects_anything_unsafe(raw):
    with pytest.raises(tstore.InvalidHandleError):
        tstore.validate_username(raw)


def test_invalid_handle_error_is_a_valueerror():
    # Routers catch ValueError to turn this into a 422 — keep the inheritance.
    assert issubclass(tstore.InvalidHandleError, ValueError)


def test_invalid_handle_message_is_plain_language():
    with pytest.raises(tstore.InvalidHandleError) as exc:
        tstore.validate_username("foo bar")
    msg = str(exc.value)
    assert "foo bar" in msg           # echoes what the user typed
    assert "letters" in msg.lower()   # says what IS allowed
    assert "regex" not in msg.lower() and "match" not in msg.lower()


# =========================================================================== #
# 2. trend_scoring.normalise_weights
# =========================================================================== #

def _sums_to_one(w: dict) -> bool:
    return abs(sum(w.values()) - 1.0) < 1e-9


def test_weights_default_when_none():
    assert ts.normalise_weights(None) == ts.DEFAULT_WEIGHTS
    assert ts.normalise_weights({}) == ts.DEFAULT_WEIGHTS
    assert _sums_to_one(ts.DEFAULT_WEIGHTS)


def test_weights_partial_override_keeps_the_rest_and_renormalises():
    w = ts.normalise_weights({"fit": 1.0})
    assert _sums_to_one(w)
    # outperformance (0.4) and uae (0.2) survive, everything is rescaled by /1.6
    assert w["fit"] == pytest.approx(1.0 / 1.6)
    assert w["outperformance"] == pytest.approx(0.4 / 1.6)
    assert w["uae"] == pytest.approx(0.2 / 1.6)


def test_weights_zero_drops_that_signal_entirely():
    w = ts.normalise_weights({"uae": 0})
    assert w["uae"] == 0.0
    assert _sums_to_one(w)
    assert w["outperformance"] == pytest.approx(0.5)
    assert w["fit"] == pytest.approx(0.5)


def test_weights_negative_is_clamped_to_zero_not_flipped():
    w = ts.normalise_weights({"fit": -5})
    assert w["fit"] == 0.0
    assert _sums_to_one(w)
    assert min(w.values()) >= 0.0


def test_weights_all_zero_falls_back_to_defaults():
    # A user dragging every slider to 0 must not produce a divide-by-zero or an
    # all-zero blend that flattens the feed.
    assert ts.normalise_weights({"outperformance": 0, "fit": 0, "uae": 0}) == ts.DEFAULT_WEIGHTS
    assert ts.normalise_weights({"outperformance": -1, "fit": -1, "uae": -1}) == ts.DEFAULT_WEIGHTS


def test_weights_ignore_garbage_values_and_unknown_keys():
    w = ts.normalise_weights({"fit": "nonsense", "bogus": 99})
    assert set(w) == set(ts.DEFAULT_WEIGHTS)      # unknown key never leaks in
    assert w == ts.DEFAULT_WEIGHTS               # bad value keeps the default
    w2 = ts.normalise_weights({"fit": None})
    assert w2 == ts.DEFAULT_WEIGHTS              # None means "not supplied"


def test_weights_accept_strings_from_query_params():
    # The router passes w_fit etc. straight through; they arrive as strings.
    assert ts.normalise_weights({"fit": "1.0"}) == ts.normalise_weights({"fit": 1.0})


# =========================================================================== #
# 3. trend_scoring.compute_opportunity
# =========================================================================== #

def test_outperformance_curve_spreads_the_busy_band():
    n = ts.normalise_outperformance
    assert n(0) == 0.0
    assert n(None) == 0.0
    assert n(-3) == 0.0                                   # a ratio can't be negative
    assert n("2.5") == n(2.5)                             # query params arrive as text
    assert n("nonsense") == 0.0
    assert n(ts.OUTPERFORMANCE_CEILING) == pytest.approx(1.0)
    band = [n(x) for x in (0.5, 1, 2, 3, 5, 8)]
    assert band == sorted(band)
    assert len(set(band)) == 6                            # strictly increasing
    # The whole point of the log curve: 1x-5x stays genuinely spread, where a
    # linear 3x cap used to tie 3x, 4x and 5x at full marks.
    assert n(5) - n(1) > 0.3


def test_unscored_post_falls_back_to_outperformance_alone():
    # A fresh scan has no AI scoring yet — it must still rank, not score 0.
    for ratio in (0.5, 1.0, 1.5, 3.0, 7.9):
        assert ts.compute_opportunity({"outperformance": ratio}) == pytest.approx(
            round(100.0 * ts.normalise_outperformance(ratio), 2))
    assert ts.compute_opportunity({"outperformance": 3.0}) > 0.0
    assert ts.compute_opportunity({"outperformance": ts.OUTPERFORMANCE_CEILING}) == 100.0
    assert (ts.compute_opportunity({"outperformance": 3.0})
            > ts.compute_opportunity({"outperformance": 1.0}))
    # ...and the fallback ignores the weights entirely (nothing else to blend).
    assert (ts.compute_opportunity({"outperformance": 1.5},
                                   {"outperformance": 0.01, "fit": 1, "uae": 1})
            == ts.compute_opportunity({"outperformance": 1.5}))


def test_unscored_and_unperformed_is_zero_not_an_error():
    assert ts.compute_opportunity({}) == 0.0
    assert ts.compute_opportunity({"outperformance": None}) == 0.0


def test_outperformance_ceiling_caps_the_outlier():
    # Measured on real data: @visitabudhabi posts hit 44x, 34x and 22x their own
    # median. Those must not be able to run away with the feed.
    ceiling = ts.OUTPERFORMANCE_CEILING
    at = ts.compute_opportunity({"outperformance": ceiling, "fit_score": 50, "uae_relevance": 50})
    way_over = ts.compute_opportunity({"outperformance": 20.0, "fit_score": 50, "uae_relevance": 50})
    absurd = ts.compute_opportunity({"outperformance": 999.0, "fit_score": 50, "uae_relevance": 50})
    assert at == way_over == absurd
    # ...and below the ceiling it still moves.
    under = ts.compute_opportunity({"outperformance": 1.0, "fit_score": 50, "uae_relevance": 50})
    mid = ts.compute_opportunity({"outperformance": 4.7, "fit_score": 50, "uae_relevance": 50})
    assert under < mid < at


def test_negative_outperformance_cannot_drag_the_score_below_zero():
    assert ts.compute_opportunity(
        {"outperformance": -9.0, "fit_score": 0, "uae_relevance": 0}) == 0.0


def test_weights_actually_shift_the_ranking():
    hot_but_unmakeable = {"outperformance": ts.OUTPERFORMANCE_CEILING,
                          "fit_score": 0, "uae_relevance": 0}
    dull_but_perfect = {"outperformance": 0.0, "fit_score": 100, "uae_relevance": 100}

    by_performance = {"outperformance": 1.0, "fit": 0.0, "uae": 0.0}
    by_fit = {"outperformance": 0.0, "fit": 1.0, "uae": 0.0}

    assert ts.compute_opportunity(hot_but_unmakeable, by_performance) == 100.0
    assert ts.compute_opportunity(dull_but_perfect, by_performance) == 0.0
    assert ts.compute_opportunity(hot_but_unmakeable, by_fit) == 0.0
    assert ts.compute_opportunity(dull_but_perfect, by_fit) == 100.0


def test_score_stays_within_0_100_however_dirty_the_input():
    dirty = [
        {"outperformance": 500, "fit_score": 9999, "uae_relevance": 9999},
        {"outperformance": -500, "fit_score": -20, "uae_relevance": -20},
        {"outperformance": 0, "fit_score": None, "uae_relevance": 0},
        {"outperformance": "2.5", "fit_score": "80", "uae_relevance": "80"},
        {"outperformance": 1, "fit_score": 100, "uae_relevance": None},
    ]
    for post in dirty:
        for weights in (None, {"fit": 0, "uae": 0, "outperformance": 0}, {"uae": 3}):
            score = ts.compute_opportunity(post, weights)
            assert 0.0 <= score <= 100.0, (post, weights, score)


def test_a_partially_scored_post_is_blended_over_the_signals_it_has():
    """A missing signal is UNKNOWN, never zero.

    The weights are renormalised over whichever signals actually came back, so
    a partial reply is neither treated as unscored nor punished for the
    model's omission. Both-present and both-absent are unchanged by this — only
    the partial cases move.

    The old behaviour scored a missing signal as 0, which produced a real
    ranking inversion in the other direction: fit dropped, uae present, at
    outperformance 1.0 scored 22.6 against a NEVER-scored post's 31.6. Absence
    of evidence ranked below absence of scoring.
    """
    # outperformance 0 discriminates all three candidate behaviours: the
    # unscored shortcut gives 0, "missing means zero" gives 40, renormalising
    # over {outperformance, fit} gives 50.
    partial = ts.compute_opportunity({"outperformance": 0.0, "fit_score": 100})
    assert partial == pytest.approx(50.0)

    # ...and it must still not collapse to the unscored shortcut.
    unscored = ts.compute_opportunity({"outperformance": 0.0})
    assert unscored == 0.0
    assert partial > unscored

    # The inversion the fix exists for: a dropped fit must not rank BELOW a post
    # that was never scored at all.
    dropped_fit = ts.compute_opportunity({"outperformance": 1.0, "uae_relevance": 50})
    never_scored = ts.compute_opportunity({"outperformance": 1.0})
    assert dropped_fit >= never_scored

    # Fully scored is untouched — the established competitor ranking must not
    # move because of this fix.
    top = ts.OUTPERFORMANCE_CEILING
    assert ts.compute_opportunity(
        {"outperformance": top, "fit_score": 100, "uae_relevance": 100}
    ) == 100.0
    assert ts.compute_opportunity(
        {"outperformance": 0.0, "fit_score": 50, "uae_relevance": 50}
    ) == pytest.approx(30.0)


# =========================================================================== #
# 4. trend_scoring.rank_posts
# =========================================================================== #

def _dt(day: int) -> datetime:
    return datetime(2026, 7, day, 12, 0, tzinfo=timezone.utc)


def _feed() -> list[dict]:
    return [
        {"id": 1, "outperformance": 0.5, "fit_score": 95, "uae_relevance": 90,
         "engagement": 900, "posted_at": _dt(1)},
        {"id": 2, "outperformance": 4.0, "fit_score": 10, "uae_relevance": 10,
         "engagement": 50, "posted_at": _dt(20)},
        {"id": 3, "outperformance": 1.2, "fit_score": 60, "uae_relevance": 40,
         "engagement": 4000, "posted_at": _dt(10)},
    ]


def _ids(rows: list[dict]) -> list[int]:
    return [r["id"] for r in rows]


def test_rank_by_outperformance():
    assert _ids(ts.rank_posts(_feed(), sort="outperformance")) == [2, 3, 1]


def test_rank_by_engagement():
    assert _ids(ts.rank_posts(_feed(), sort="engagement")) == [3, 1, 2]


def test_rank_by_fit():
    assert _ids(ts.rank_posts(_feed(), sort="fit")) == [1, 3, 2]


def test_rank_by_recent():
    assert _ids(ts.rank_posts(_feed(), sort="recent")) == [2, 3, 1]


def test_rank_by_opportunity_is_the_default_and_recomputes_the_score():
    feed = _feed()
    for row in feed:
        row["opportunity_score"] = 0.0   # a stale stored score must be ignored
    ranked = ts.rank_posts(feed, sort="opportunity")
    assert _ids(ranked) == _ids(ts.rank_posts(feed))          # opportunity is default
    assert _ids(ranked) == _ids(ts.rank_posts(feed, sort="nonsense-sort"))
    assert all(r["opportunity_score"] > 0 for r in ranked)
    assert ranked == sorted(ranked, key=lambda r: r["opportunity_score"], reverse=True)


def test_rank_recent_handles_missing_dates_without_throwing():
    feed = _feed()
    feed.append({"id": 4, "outperformance": 9.0, "posted_at": None})
    feed.append({"id": 5, "outperformance": 9.0})            # key absent entirely
    ranked = ts.rank_posts(feed, sort="recent")
    assert len(ranked) == 5
    assert _ids(ranked)[:3] == [2, 3, 1]                     # dated posts, newest first
    assert set(_ids(ranked)[3:]) == {4, 5}                   # undated sink to the bottom


def test_rank_recent_with_no_dates_at_all():
    feed = [{"id": 1, "posted_at": None}, {"id": 2}, {"id": 3, "posted_at": None}]
    assert len(ts.rank_posts(feed, sort="recent")) == 3


def test_rank_tolerates_completely_empty_rows():
    assert ts.rank_posts([]) == []
    ranked = ts.rank_posts([{"id": 1}, {"id": 2}])
    assert all(r["opportunity_score"] == 0.0 for r in ranked)


def test_changing_the_weights_reorders_the_feed():
    feed = _feed()
    by_perf = _ids(ts.rank_posts(feed, weights={"outperformance": 1, "fit": 0, "uae": 0}))
    by_fit = _ids(ts.rank_posts(feed, weights={"outperformance": 0, "fit": 1, "uae": 0}))
    assert by_perf == [2, 3, 1]
    assert by_fit == [1, 3, 2]
    assert by_perf != by_fit


def test_rank_does_not_mutate_the_caller_rows():
    feed = _feed()
    snapshot = [dict(r) for r in feed]
    ts.rank_posts(feed, sort="opportunity")
    assert feed == snapshot


def test_declared_sorts_all_work():
    for sort in ts.SORTS:
        assert len(ts.rank_posts(_feed(), sort=sort)) == 3


# =========================================================================== #
# 5. trend_scoring.build_batch_prompt
# =========================================================================== #

def _prompt_posts() -> list[dict]:
    return [
        {"id": 11, "username": "visitabudhabi", "media_type": "VIDEO",
         "media_product_type": "REELS", "like_count": 1200, "comments_count": 34,
         "view_count": 98000, "outperformance": 4.72, "caption": "Sunrise over the Corniche"},
        {"id": 22, "username": "42abudhabi", "media_type": "IMAGE",
         "like_count": 300, "comments_count": 5, "view_count": None,
         "outperformance": 1.0, "caption": "Piscine day one"},
        {"id": 33, "username": "someone", "media_type": "CAROUSEL_ALBUM",
         "like_count": 0, "comments_count": 0, "caption": None},
    ]


def test_batch_prompt_includes_every_post_and_echoes_the_index():
    prompt = ts.build_batch_prompt(_prompt_posts())
    assert "Score the 3 posts below" in prompt
    assert "echoing its index" in prompt
    for i in (1, 2, 3):
        assert f"POST {i}" in prompt
    for handle in ("@visitabudhabi", "@42abudhabi", "@someone"):
        assert handle in prompt
    for media_type in ("VIDEO", "IMAGE", "CAROUSEL_ALBUM"):
        assert media_type in prompt


def test_batch_prompt_leads_with_the_outperformance_multiple():
    prompt = ts.build_batch_prompt(_prompt_posts())
    assert "performed 4.7x that account's own average" in prompt
    assert "performed 1.0x that account's own average" in prompt
    # A post with no outperformance yet simply omits the line.
    assert prompt.count("that account's own average") == 2


def test_batch_prompt_omits_views_for_non_video():
    prompt = ts.build_batch_prompt(_prompt_posts())
    assert "views: 98000" in prompt
    assert prompt.count("views:") == 1        # only the VIDEO carries views


def test_batch_prompt_keeps_raw_engagement_visible():
    prompt = ts.build_batch_prompt(_prompt_posts())
    assert "engagement: 1200 likes, 34 comments" in prompt
    assert "engagement: 0 likes, 0 comments" in prompt   # None -> 0, never "None"
    assert "None likes" not in prompt


def test_batch_prompt_captions_are_flattened_and_truncated():
    posts = [{"id": 1, "username": "u", "media_type": "IMAGE",
              "caption": "line one\nline two\t\tspaced   out " + ("x" * 900)}]
    prompt = ts.build_batch_prompt(posts)
    assert "line one line two spaced out" in prompt   # whitespace collapsed
    body = prompt.split("caption: ", 1)[1]
    assert len(body.strip()) <= 420                   # hard cap on payload size
    assert "(none)" not in prompt


def test_batch_prompt_marks_a_missing_caption():
    prompt = ts.build_batch_prompt([{"id": 1, "username": "u", "media_type": "IMAGE"}])
    assert "caption: (none)" in prompt


def test_batch_prompt_brand_note_is_optional_and_leads():
    without = ts.build_batch_prompt(_prompt_posts())
    with_note = ts.build_batch_prompt(_prompt_posts(), "  voice: warm, student-first  ")
    assert "WHAT 42 SOUNDS AND LOOKS LIKE" not in without
    assert with_note.startswith("WHAT 42 SOUNDS AND LOOKS LIKE")
    assert "voice: warm, student-first" in with_note
    assert ts.build_batch_prompt(_prompt_posts(), "") == without


def test_batch_prompt_of_an_empty_batch_does_not_crash():
    assert "Score the 0 posts below" in ts.build_batch_prompt([])


# =========================================================================== #
# 6. trend_scoring.parse_batch_response
# =========================================================================== #

def _score_posts() -> list[dict]:
    return [{"id": 101, "outperformance": 3.0}, {"id": 202, "outperformance": 0.5}]


def _entry(index, **over) -> dict:
    base = {
        "index": index, "topic": "A day in the life", "topic_summary": "it felt real",
        "theme": "Campus Life", "fit_score": 80, "fit_reason": "students + a phone",
        "uae_relevance": 70, "suggested_angle": "follow one student",
        "suggested_format": "reel", "effort": "low", "tags": ["students", "campus"],
    }
    base.update(over)
    return base


def test_parse_maps_by_the_echoed_index_not_by_position():
    posts = _score_posts()
    # Deliberately out of order: entry for POST 2 arrives first.
    out = ts.parse_batch_response(
        {"scores": [_entry(2, topic="second"), _entry(1, topic="first")]}, posts)
    assert set(out) == {101, 202}
    assert out[101]["topic"] == "first"
    assert out[202]["topic"] == "second"


def test_parse_tolerates_a_short_reply():
    out = ts.parse_batch_response({"scores": [_entry(1)]}, _score_posts())
    assert set(out) == {101}      # post 202 simply stays unscored, batch not lost


def test_parse_skips_out_of_range_indexes():
    out = ts.parse_batch_response(
        {"scores": [_entry(0), _entry(3), _entry(-1), _entry(99), _entry(2)]},
        _score_posts())
    assert set(out) == {202}


def test_parse_skips_unusable_index_values():
    out = ts.parse_batch_response(
        {"scores": [_entry(None), _entry("two"), _entry({}), _entry("1")]},
        _score_posts())
    assert set(out) == {101}      # "1" is tolerated, the rest are dropped


def test_parse_non_dict_input_returns_empty():
    for raw in (None, "nope", ["scores"], 42, object()):
        assert ts.parse_batch_response(raw, _score_posts()) == {}
    assert ts.parse_batch_response({}, _score_posts()) == {}
    assert ts.parse_batch_response({"scores": None}, _score_posts()) == {}


def test_parse_skips_non_dict_entries():
    out = ts.parse_batch_response({"scores": ["junk", None, 7, _entry(1)]}, _score_posts())
    assert set(out) == {101}


def test_parse_clamps_fit_and_uae_into_0_100():
    out = ts.parse_batch_response(
        {"scores": [_entry(1, fit_score=250, uae_relevance=-30),
                    _entry(2, fit_score="88.6", uae_relevance=100.0)]},
        _score_posts())
    assert out[101]["fit_score"] == 100
    assert out[101]["uae_relevance"] == 0
    assert out[202]["fit_score"] == 89       # rounded, then clamped
    assert out[202]["uae_relevance"] == 100


def test_parse_unusable_scores_become_none_not_zero():
    # None means "the model didn't say", which is what triggers the
    # outperformance-only fallback — zero would wrongly look like a real verdict.
    out = ts.parse_batch_response(
        {"scores": [_entry(1, fit_score="unknown", uae_relevance=None)]}, _score_posts())
    assert out[101]["fit_score"] is None
    assert out[101]["uae_relevance"] is None
    # ...so the blend falls back to the 3.0x outperformance alone.
    assert out[101]["opportunity_score"] == pytest.approx(
        round(100.0 * ts.normalise_outperformance(3.0), 2))


def test_parse_normalises_enums_and_drops_invented_ones():
    out = ts.parse_batch_response(
        {"scores": [_entry(1, suggested_format="  REEL ", effort="Low"),
                    _entry(2, suggested_format="tiktok", effort="colossal")]},
        _score_posts())
    assert out[101]["suggested_format"] == "reel"
    assert out[101]["effort"] == "low"
    assert out[202]["suggested_format"] is None
    assert out[202]["effort"] is None


def test_parse_lowercases_the_theme_so_the_rollup_groups():
    out = ts.parse_batch_response({"scores": [_entry(1, theme="  Campus LIFE  ")]},
                                  _score_posts())
    assert out[101]["theme"] == "campus life"


def test_parse_bounds_the_text_and_tag_fields():
    out = ts.parse_batch_response(
        {"scores": [_entry(1, topic="t" * 500, suggested_angle="a" * 900,
                           tags=["x" * 100] + [f"t{i}" for i in range(20)])]},
        _score_posts())
    score = out[101]
    assert len(score["topic"]) == 160
    assert len(score["suggested_angle"]) == 600
    assert len(score["tags"]) == 8
    assert all(len(t) <= 40 for t in score["tags"])


def test_parse_blank_strings_become_none():
    out = ts.parse_batch_response(
        {"scores": [_entry(1, topic="   ", fit_reason="", tags=[])]}, _score_posts())
    assert out[101]["topic"] is None
    assert out[101]["fit_reason"] is None
    assert out[101]["tags"] == []


def test_parse_attaches_an_opportunity_score_in_range():
    out = ts.parse_batch_response(
        {"scores": [_entry(1), _entry(2)]}, _score_posts())
    for post_id, score in out.items():
        assert 0.0 <= score["opportunity_score"] <= 100.0
    # POST 1 outperformed 3x, POST 2 didn't — same AI verdict, different rank.
    assert out[101]["opportunity_score"] > out[202]["opportunity_score"]


def test_parse_last_entry_wins_for_a_duplicated_index():
    out = ts.parse_batch_response(
        {"scores": [_entry(1, topic="first"), _entry(1, topic="second")]}, _score_posts())
    assert out[101]["topic"] == "second"


# =========================================================================== #
# 6b. trend_scoring: "the model is unavailable" classification
#
# A billing/credentials failure repeats identically on every batch, so it must
# stop the pass loudly instead of silently returning an empty score dict a dozen
# times. A transient failure must NOT.
# =========================================================================== #

@pytest.mark.parametrize("msg", [
    "billing has not been enabled for this project",
    "PERMISSION_DENIED: caller lacks permission",
    "403 Forbidden",
    "API key not valid",
    "could not refresh credentials",
    "Vertex AI API has not been used / not enabled",
    "UNAUTHENTICATED",
    "401 Unauthorized",
    "BILLING_DISABLED",                      # matching is case-insensitive
])
def test_unavailable_errors_are_recognised(msg):
    assert ts._is_unavailable(Exception(msg)) is True


@pytest.mark.parametrize("msg", [
    "429 quota exceeded, retry later",
    "read timeout",
    "the model returned unparseable JSON",
    "connection reset by peer",
    "",
])
def test_transient_errors_are_not_treated_as_unavailable(msg):
    assert ts._is_unavailable(Exception(msg)) is False


def test_explain_unavailable_tells_the_user_what_still_works():
    billing = ts._explain_unavailable(Exception("billing account disabled"))
    assert "billing" in billing.lower()
    assert "engagement" in billing            # the feed still ranks, just without fit
    other = ts._explain_unavailable(Exception("403 " + "z" * 400))
    assert "engagement" in other
    assert len(other) < 300                   # the raw error is bounded, not dumped


def _fake_gemini(monkeypatch, result=None, error=None):
    async def _call(prompt, **kwargs):
        if error is not None:
            raise error
        return result
    monkeypatch.setattr(ts, "gemini_multimodal", _call)


def test_score_batch_raises_scoring_unavailable_on_a_billing_failure(monkeypatch):
    _fake_gemini(monkeypatch, error=RuntimeError("billing is disabled for project x"))
    with pytest.raises(ts.ScoringUnavailable) as exc:
        _run(ts.score_batch(_score_posts()))
    assert "engagement" in str(exc.value)


def test_score_batch_swallows_a_transient_failure(monkeypatch):
    _fake_gemini(monkeypatch, error=RuntimeError("504 gateway timeout"))
    assert _run(ts.score_batch(_score_posts())) == {}     # batch lost, run continues


def test_score_batch_skips_an_unparseable_reply(monkeypatch):
    _fake_gemini(monkeypatch, result={"_raw": "```json {truncated"})
    assert _run(ts.score_batch(_score_posts())) == {}


def test_score_batch_of_nothing_never_calls_the_model(monkeypatch):
    def _explode(*a, **k):
        raise AssertionError("the model must not be called for an empty batch")
    monkeypatch.setattr(ts, "gemini_multimodal", _explode)
    assert _run(ts.score_batch([])) == {}


def test_score_batch_parses_a_good_reply(monkeypatch):
    _fake_gemini(monkeypatch, result={"scores": [_entry(1), _entry(2)]})
    out = _run(ts.score_batch(_score_posts()))
    assert set(out) == {101, 202}


# =========================================================================== #
# 7. trend_finder pure helpers
# =========================================================================== #

def test_engagement_is_likes_plus_comments():
    assert tf._engagement({"like_count": 100, "comments_count": 7}) == 107
    assert tf._engagement({"like_count": 100}) == 100
    assert tf._engagement({}) == 0
    assert tf._engagement({"like_count": None, "comments_count": None}) == 0


def test_engagement_never_counts_views():
    # views exist only on VIDEO, so including them would make the median
    # incomparable across media types within one account.
    video = {"like_count": 10, "comments_count": 2, "view_count": 500_000}
    image = {"like_count": 10, "comments_count": 2}
    assert tf._engagement(video) == tf._engagement(image) == 12


def test_trusted_outperformance_is_a_no_op_on_a_full_baseline():
    for raw in (0.0, 0.5, 1.0, 3.333, 44.0):
        assert tf._trusted_outperformance(raw, 1.0) == round(raw, 3)


def test_trusted_outperformance_shrinks_a_thin_sample_toward_typical():
    # Halfway to a real baseline, a claim is worth half as much...
    assert tf._trusted_outperformance(3.0, 0.5) == 2.0
    assert tf._trusted_outperformance(0.0, 0.5) == 0.5     # flops are damped too
    # ...and with no baseline at all, nothing is unusual.
    assert tf._trusted_outperformance(44.0, 0.0) == 1.0
    assert tf._trusted_outperformance(0.0, 0.0) == 1.0


def test_trusted_outperformance_caps_what_a_thin_sample_can_claim():
    # Shrinking alone isn't enough: half of 100x is still 50x. A sample worth
    # `conf` may claim at most 1 + conf * (THIN_SAMPLE_MAX_CLAIM - 1).
    for conf in (0.17, 0.5, 0.83):
        ceiling = 1.0 + conf * (tf.THIN_SAMPLE_MAX_CLAIM - 1.0)
        assert tf._trusted_outperformance(100.0, conf) == pytest.approx(ceiling, abs=1e-3)
        assert tf._trusted_outperformance(1000.0, conf) == pytest.approx(ceiling, abs=1e-3)
    assert tf._trusted_outperformance(100.0, 0.5) < tf._trusted_outperformance(100.0, 0.9)


def test_trusted_outperformance_clamps_a_silly_confidence():
    assert tf._trusted_outperformance(3.0, 5.0) == 3.0     # >1 behaves as 1
    assert tf._trusted_outperformance(3.0, -2.0) == 1.0    # <0 behaves as 0


def test_trusted_outperformance_never_goes_negative():
    for raw in (0.0, 0.1, 1.0, 9.0):
        for conf in (0.0, 0.3, 1.0):
            assert tf._trusted_outperformance(raw, conf) >= 0.0


def test_parse_ts_graph_format_has_no_colon_in_the_offset():
    got = tf._parse_ts("2026-07-22T10:15:33+0000")
    assert got == datetime(2026, 7, 22, 10, 15, 33, tzinfo=timezone.utc)
    assert got.tzinfo is not None            # must stay comparable to the cutoff


def test_parse_ts_iso_and_z_fallbacks():
    assert tf._parse_ts("2026-07-22T10:15:33Z") == datetime(
        2026, 7, 22, 10, 15, 33, tzinfo=timezone.utc)
    assert tf._parse_ts("2026-07-22T10:15:33+00:00") == datetime(
        2026, 7, 22, 10, 15, 33, tzinfo=timezone.utc)


def test_parse_ts_returns_none_instead_of_throwing():
    for raw in (None, "", "nonsense", "2026-13-45", 0, [], {}):
        assert tf._parse_ts(raw) is None


def test_humanise_invalid_user_id_explains_business_accounts():
    msg = tf._humanise_ig_error(Exception("(#100) Invalid user id"), "someone")
    assert "@someone" in msg
    assert "Business" in msg and "Creator" in msg
    assert "Invalid user id" not in msg       # the raw Graph string is replaced
    assert "personal" in msg.lower()


def test_humanise_does_not_exist_gets_the_same_explanation():
    msg = tf._humanise_ig_error(Exception("user does not exist"), "ghost")
    assert "Business Discovery" in msg and "@ghost" in msg


def test_humanise_rate_limit():
    for exc in (Exception("Application request limit reached: rate limit"),
                Exception("(4) Application request limit reached")):
        assert "rate limit" in tf._humanise_ig_error(exc, "u").lower()
        assert "try again" in tf._humanise_ig_error(exc, "u").lower()


def test_humanise_unknown_error_is_passed_through_but_bounded():
    msg = tf._humanise_ig_error(Exception("x" * 900), "u")
    assert len(msg) == 300


def test_thumb_source_prefers_the_video_still():
    assert tf._thumb_source({"thumbnail_url": "t.jpg", "media_url": "v.mp4"}) == "t.jpg"
    assert tf._thumb_source({"media_url": "i.jpg"}) == "i.jpg"
    assert tf._thumb_source({}) is None


# --- fetch_recent_media: the lookback window + the page ceiling --------------

def _ts(days_ago: int) -> str:
    """A Graph-format timestamp — note the offset has no colon."""
    stamp = datetime.now(timezone.utc) - timedelta(days=days_ago)
    return stamp.strftime("%Y-%m-%dT%H:%M:%S%z")


def _page(ids: list[str], *, after: str | None = None, days_ago: int = 1) -> dict:
    block: dict = {"data": [{"id": i, "timestamp": _ts(days_ago)} for i in ids]}
    if after:
        block["paging"] = {"cursors": {"after": after}}
    return {"name": "Visit Abu Dhabi", "followers_count": 1000,
            "media_count": 4200, "media": block}


def _patch_ig_pages(monkeypatch, pages: dict, calls: list | None = None):
    async def _fetch(username, *, after=None, page_size=None):
        if calls is not None:
            calls.append(after)
        return pages[after]
    monkeypatch.setattr(tf.ig, "fetch_account_media", _fetch)


def test_fetch_recent_media_drops_posts_past_the_lookback_cutoff(monkeypatch):
    _patch_ig_pages(monkeypatch, {None: {
        "name": "x", "media": {"data": [
            {"id": "new", "timestamp": _ts(1)},
            {"id": "edge", "timestamp": _ts(119)},
            {"id": "old", "timestamp": _ts(400)},
        ]},
    }})
    account, media = _run(tf.fetch_recent_media("visitabudhabi", lookback_days=120))
    assert [m["id"] for m in media] == ["new", "edge"]
    assert "media" not in account          # account fields only


def test_fetch_recent_media_keeps_undated_posts(monkeypatch):
    _patch_ig_pages(monkeypatch, {None: {
        "media": {"data": [{"id": "no-ts"}, {"id": "bad-ts", "timestamp": "???"}]}}})
    _, media = _run(tf.fetch_recent_media("u", lookback_days=1))
    assert [m["id"] for m in media] == ["no-ts", "bad-ts"]


def test_fetch_recent_media_follows_the_after_cursor(monkeypatch):
    calls: list = []
    _patch_ig_pages(monkeypatch, {
        None: _page(["a", "b"], after="cur1"),
        "cur1": _page(["c", "d"], after="cur2"),
        "cur2": _page(["e"]),                      # no cursor -> stop
    }, calls)
    account, media = _run(tf.fetch_recent_media("u"))
    assert calls == [None, "cur1", "cur2"]
    assert [m["id"] for m in media] == ["a", "b", "c", "d", "e"]
    assert account["followers_count"] == 1000      # taken from the FIRST page


def test_fetch_recent_media_respects_the_page_ceiling(monkeypatch):
    calls: list = []
    pages = {None: _page(["p0"], after="c0")}
    pages.update({f"c{i}": _page([f"p{i + 1}"], after=f"c{i + 1}") for i in range(10)})
    _patch_ig_pages(monkeypatch, pages, calls)

    _, media = _run(tf.fetch_recent_media("u"))
    assert len(calls) == tf.MAX_PAGES_PER_COMPETITOR
    assert len(media) == tf.MAX_PAGES_PER_COMPETITOR

    calls.clear()
    _, media = _run(tf.fetch_recent_media("u", max_pages=2))
    assert len(calls) == 2 and len(media) == 2


def test_fetch_recent_media_stops_paging_once_the_cutoff_is_hit(monkeypatch):
    calls: list = []
    _patch_ig_pages(monkeypatch, {
        None: _page(["recent"], after="cur1", days_ago=1),
        "cur1": _page(["ancient"], after="cur2", days_ago=400),
        "cur2": _page(["never-read"], days_ago=1),
    }, calls)
    _, media = _run(tf.fetch_recent_media("u", lookback_days=30))
    assert calls == [None, "cur1"]                 # the third page is never fetched
    assert [m["id"] for m in media] == ["recent"]


def test_fetch_recent_media_empty_account(monkeypatch):
    _patch_ig_pages(monkeypatch, {None: {"name": "x", "media": {"data": []}}})
    account, media = _run(tf.fetch_recent_media("u"))
    assert media == []
    assert account == {"name": "x"}


def test_fetch_recent_media_missing_media_block(monkeypatch):
    # A reachable profile with no media key at all must not KeyError.
    _patch_ig_pages(monkeypatch, {None: {"name": "x", "followers_count": 5}})
    account, media = _run(tf.fetch_recent_media("u"))
    assert media == []
    assert account["followers_count"] == 5


# =========================================================================== #
# 8. trend_research.build_prompt
# =========================================================================== #

def test_research_prompt_states_the_limit():
    prompt = tr.build_prompt(tracked=[], excluded=[], hint=None, limit=9)
    assert "up to 9 Instagram accounts" in prompt
    assert "42 Abu Dhabi" in prompt
    assert "three quarters UAE/GCC" in prompt


def test_research_prompt_lists_tracked_and_excluded_separately():
    prompt = tr.build_prompt(
        tracked=["visitabudhabi", "42abudhabi", "adnoc"],
        excluded=["adnoc"], hint=None, limit=8)
    assert "ALREADY TRACKED" in prompt and "do not suggest these again" in prompt
    assert "REJECTED by the team" in prompt and "never suggest these" in prompt
    for handle in ("@visitabudhabi", "@42abudhabi", "@adnoc"):
        assert handle in prompt
    tracked_block = prompt.split("ALREADY TRACKED", 1)[1].split("REJECTED", 1)[0]
    assert "@42abudhabi" in tracked_block          # sorted, so this leads
    rejected_block = prompt.split("REJECTED by the team", 1)[1]
    assert "@adnoc" in rejected_block


def test_research_prompt_omits_empty_sections():
    prompt = tr.build_prompt(tracked=[], excluded=[], hint=None, limit=8)
    assert "ALREADY TRACKED" not in prompt
    assert "REJECTED" not in prompt


def test_research_prompt_carries_the_hint():
    prompt = tr.build_prompt(tracked=[], excluded=[],
                             hint="  Emirati women in tech  ", limit=8)
    assert "The team specifically wants: Emirati women in tech" in prompt
    long_hint = tr.build_prompt(tracked=[], excluded=[], hint="h" * 900, limit=8)
    assert "h" * 300 in long_hint and "h" * 301 not in long_hint


def test_research_prompt_bounds_huge_lists():
    prompt = tr.build_prompt(tracked=[f"acct{i:03d}" for i in range(200)],
                             excluded=[], hint=None, limit=8)
    assert prompt.count("@acct") == 80


# =========================================================================== #
# 9. Integration-shaped: trend_finder.scan_one (fake Graph + fake store)
# =========================================================================== #

class _FakeStore:
    """Stands in for trend_store — records what the scan tried to persist."""

    def __init__(self, *, fail_media_ids: tuple[str, ...] = (),
                 known_thumbs: dict[str, str] | None = None):
        self.posts: list[tuple[int, dict]] = []
        self.thumbs: list[tuple[int, str]] = []
        self.results: list[tuple[int, dict]] = []
        self._fail = set(fail_media_ids)
        self._known_thumbs = dict(known_thumbs or {})
        self._next_id = 500

    async def local_thumbs_for(self, ig_media_ids: list[str]) -> dict[str, str]:
        return {m: p for m, p in self._known_thumbs.items() if m in ig_media_ids}

    async def upsert_post(self, competitor_id: int, row: dict) -> int:
        if row["ig_media_id"] in self._fail:
            raise RuntimeError("simulated write failure")
        self.posts.append((competitor_id, row))
        self._next_id += 1
        return self._next_id

    async def set_local_thumb(self, post_id: int, rel: str) -> None:
        self.thumbs.append((post_id, rel))

    async def record_scan_result(self, competitor_id: int, **fields) -> None:
        self.results.append((competitor_id, fields))

    @property
    def by_media_id(self) -> dict:
        return {row["ig_media_id"]: row for _, row in self.posts}


_COMPETITOR = {"id": 7, "username": "visitabudhabi"}

_ACCOUNT = {"name": "Visit Abu Dhabi", "followers_count": 1000, "media_count": 4200}


def _media(mid: str, likes: int, comments: int, **over) -> dict:
    m = {
        "id": mid, "media_type": "IMAGE", "permalink": f"https://insta/{mid}",
        "media_url": f"https://cdn/{mid}.jpg", "caption": f"caption {mid}",
        "timestamp": "2026-07-22T10:15:33+0000",
        "like_count": likes, "comments_count": comments,
    }
    m.update(over)
    return m


def _patch_fetch(monkeypatch, account, media, *, calls=None):
    async def _fake_fetch(username, *, lookback_days=None, max_pages=None, max_posts=None):
        if calls is not None:
            calls.append((username, lookback_days))
        return account, media
    monkeypatch.setattr(tf, "fetch_recent_media", _fake_fetch)


def test_scan_one_computes_the_median_and_relative_outperformance(monkeypatch):
    store = _FakeStore()
    monkeypatch.setattr(tf, "store", store)
    # engagements: 10, 20, 30, 40, 100  -> median 30
    media = [_media("m1", 10, 0), _media("m2", 18, 2), _media("m3", 25, 5),
             _media("m4", 38, 2), _media("m5", 95, 5)]
    calls: list = []
    _patch_fetch(monkeypatch, _ACCOUNT, media, calls=calls)

    res = _run(tf.scan_one(_COMPETITOR, lookback_days=60, download_thumbs=False))

    assert res == {"username": "visitabudhabi", "ok": True, "posts": 5, "median": 30.0}
    assert calls == [("visitabudhabi", 60)]
    rows = store.by_media_id
    assert rows["m3"]["engagement"] == 30
    assert rows["m3"]["outperformance"] == 1.0        # exactly typical
    # 5 posts is below MIN_POSTS_FOR_BASELINE, so the ratio is damped and
    # capped by confidence (5/6). The RAW ratio is kept for transparency.
    assert rows["m5"]["metadata"]["raw_outperformance"] == pytest.approx(100 / 30, abs=1e-3)
    assert rows["m5"]["outperformance"] < 100 / 30
    assert rows["m5"]["outperformance"] == pytest.approx(1 + (5 / 6) * 2.0, abs=1e-3)
    # Under-performers are pulled toward "typical" by the same damping — with a
    # thin sample we are no more sure a post flopped than that it flew.
    assert rows["m1"]["outperformance"] == pytest.approx(1 + (10 / 30 - 1) * (5 / 6), abs=1e-3)
    # ...and every row carries the baseline it was measured against.
    assert rows["m1"]["metadata"] == {
        "baseline_median": 30.0, "baseline_sample": 5,
        "baseline_confidence": pytest.approx(0.83, abs=0.01),
        "baseline_trustworthy": False,
        "raw_outperformance": pytest.approx(10 / 30, abs=1e-3),
    }


def test_scan_one_stores_every_post_with_the_right_shape(monkeypatch):
    store = _FakeStore()
    monkeypatch.setattr(tf, "store", store)
    media = [
        _media("vid", 100, 20, media_type="VIDEO", media_product_type="REELS",
               thumbnail_url="https://cdn/vid.jpg", view_count=9000),
        _media("img", 40, 0),
    ]
    _patch_fetch(monkeypatch, _ACCOUNT, media)

    _run(tf.scan_one(_COMPETITOR, download_thumbs=False))

    assert [cid for cid, _ in store.posts] == [7, 7]
    vid = store.by_media_id["vid"]
    img = store.by_media_id["img"]
    assert vid["view_count"] == 9000
    assert img["view_count"] is None          # never invented for images
    assert vid["media_type"] == "VIDEO" and vid["media_product_type"] == "REELS"
    assert vid["username"] == "visitabudhabi"
    assert vid["permalink"] == "https://insta/vid"
    assert vid["posted_at"] == datetime(2026, 7, 22, 10, 15, 33, tzinfo=timezone.utc)
    assert vid["engagement"] == 120
    assert vid["engagement_rate"] == pytest.approx(120 / 1000)   # /followers
    assert img["engagement_rate"] == pytest.approx(40 / 1000)


def test_scan_one_records_the_scan_result_on_the_competitor(monkeypatch):
    store = _FakeStore()
    monkeypatch.setattr(tf, "store", store)
    _patch_fetch(monkeypatch, _ACCOUNT, [_media("a", 10, 0), _media("b", 30, 0)])

    _run(tf.scan_one(_COMPETITOR, download_thumbs=False))

    assert len(store.results) == 1
    cid, fields = store.results[0]
    assert cid == 7
    assert fields["status"] == "ok"
    assert fields["error"] is None
    assert fields["posts_found"] == 2
    assert fields["median_engagement"] == 20.0
    assert fields["followers_count"] == 1000
    assert fields["media_count"] == 4200


def test_scan_one_with_no_followers_leaves_engagement_rate_null(monkeypatch):
    store = _FakeStore()
    monkeypatch.setattr(tf, "store", store)
    _patch_fetch(monkeypatch, {"followers_count": 0}, [_media("a", 10, 0)])
    _run(tf.scan_one(_COMPETITOR, download_thumbs=False))
    assert store.by_media_id["a"]["engagement_rate"] is None


def test_scan_one_zero_median_does_not_divide_by_zero(monkeypatch):
    store = _FakeStore()
    monkeypatch.setattr(tf, "store", store)
    _patch_fetch(monkeypatch, _ACCOUNT,
                 [_media("a", 0, 0), _media("b", 0, 0), _media("c", 6, 0)])
    _run(tf.scan_one(_COMPETITOR, download_thumbs=False))
    # The point of this test is that a zero median cannot divide by zero; the
    # baseline falls back to 1, giving a raw 6x, which a 3-post sample then
    # caps (see test_untrustworthy_baseline_is_damped_and_capped).
    assert store.by_media_id["c"]["metadata"]["raw_outperformance"] == 6.0
    assert store.by_media_id["c"]["outperformance"] == 2.0
    # A zero-engagement post is likewise damped toward typical on a 3-post
    # sample: raw 0.0 -> 1 + (0 - 1) * 0.5 = 0.5.
    assert store.by_media_id["a"]["metadata"]["raw_outperformance"] == 0.0
    assert store.by_media_id["a"]["outperformance"] == 0.5


def test_scan_one_empty_window_is_a_success_not_a_failure(monkeypatch):
    store = _FakeStore()
    monkeypatch.setattr(tf, "store", store)
    _patch_fetch(monkeypatch, _ACCOUNT, [])

    res = _run(tf.scan_one(_COMPETITOR, download_thumbs=False))

    assert res["ok"] is True and res["posts"] == 0
    assert "no posts" in res["note"]
    assert store.posts == []
    _, fields = store.results[0]
    assert fields["status"] == "empty"
    assert fields["error"] is None
    assert fields["followers_count"] == 1000        # the profile is still refreshed


def test_scan_one_unreachable_account_never_raises(monkeypatch):
    store = _FakeStore()
    monkeypatch.setattr(tf, "store", store)

    async def _boom(username, *, lookback_days=None, max_pages=None, max_posts=None):
        raise ig.InstagramAPIError("(#100) Invalid user id")
    monkeypatch.setattr(tf, "fetch_recent_media", _boom)

    res = _run(tf.scan_one(_COMPETITOR, download_thumbs=False))   # must NOT raise

    assert res["ok"] is False
    assert res["posts"] == 0
    assert "Business" in res["error"] and "@visitabudhabi" in res["error"]
    cid, fields = store.results[0]
    assert cid == 7
    assert fields["status"] == "failed"
    assert fields["error"] == res["error"]          # the human sentence, not the raw
    assert store.posts == []


def test_scan_one_config_error_propagates(monkeypatch):
    # A missing IG token is a WHOLE-scan problem, not a per-account one — it must
    # bubble so run_scan can fail the scan instead of blaming every competitor.
    store = _FakeStore()
    monkeypatch.setattr(tf, "store", store)

    async def _boom(username, *, lookback_days=None, max_pages=None, max_posts=None):
        raise ig.InstagramConfigError("Instagram is not configured")
    monkeypatch.setattr(tf, "fetch_recent_media", _boom)

    with pytest.raises(ig.InstagramConfigError):
        _run(tf.scan_one(_COMPETITOR, download_thumbs=False))
    assert store.results[0][1]["status"] == "failed"


def test_scan_one_one_bad_row_does_not_lose_the_rest(monkeypatch):
    store = _FakeStore(fail_media_ids=("b",))
    monkeypatch.setattr(tf, "store", store)
    _patch_fetch(monkeypatch, _ACCOUNT,
                 [_media("a", 10, 0), _media("b", 20, 0), _media("c", 30, 0)])

    res = _run(tf.scan_one(_COMPETITOR, download_thumbs=False))

    assert res["posts"] == 2
    assert set(store.by_media_id) == {"a", "c"}
    assert store.results[0][1]["posts_found"] == 2


def test_scan_one_downloads_thumbs_and_links_them(monkeypatch):
    store = _FakeStore()
    monkeypatch.setattr(tf, "store", store)
    seen: list[str] = []

    async def _fake_download(url: str):
        seen.append(url)
        return f"trend_thumbs/{url[-9:]}" if url else None

    monkeypatch.setattr(tf, "_download_thumb", _fake_download)
    _patch_fetch(monkeypatch, _ACCOUNT, [
        _media("vid", 10, 0, media_type="VIDEO", thumbnail_url="https://cdn/vid.jpg"),
        _media("img", 10, 0),
    ])

    _run(tf.scan_one(_COMPETITOR, download_thumbs=True))

    assert seen == ["https://cdn/vid.jpg", "https://cdn/img.jpg"]   # video still first
    assert [p for p, _ in store.thumbs] == [501, 502]
    assert store.thumbs[0][1].endswith("vid.jpg")


def test_scan_one_does_not_download_a_thumb_it_already_has(monkeypatch, tmp_path):
    # Every scan hands back new signed CDN links, and the file is named after
    # the link, so without this each daily scan stored every picture again.
    have = tmp_path / "have.jpg"
    have.write_bytes(b"jpg")
    store = _FakeStore(known_thumbs={"old": "trend_thumbs/have.jpg",
                                     "gone": "trend_thumbs/gone.jpg"})
    monkeypatch.setattr(tf, "store", store)
    monkeypatch.setattr(tf, "resolve_local",
                        lambda rel: tmp_path / Path(rel).name)
    seen: list[str] = []

    async def _fake_download(url: str):
        seen.append(url)
        return f"trend_thumbs/{url[-8:]}"

    monkeypatch.setattr(tf, "_download_thumb", _fake_download)
    _patch_fetch(monkeypatch, _ACCOUNT,
                 [_media("old", 10, 0), _media("gone", 10, 0), _media("new", 10, 0)])

    _run(tf.scan_one(_COMPETITOR, download_thumbs=True))

    # The cached file is reused; a row whose file is missing, and a new post,
    # are fetched.
    assert seen == ["https://cdn/gone.jpg", "https://cdn/new.jpg"]
    assert len(store.posts) == 3


def test_scan_one_refreshes_the_profile_picture(monkeypatch):
    account = {**_ACCOUNT, "profile_picture_url": "https://cdn/avatar.jpg"}
    for media in ([_media("a", 10, 0)], []):        # posts found, and none
        store = _FakeStore()
        monkeypatch.setattr(tf, "store", store)
        _patch_fetch(monkeypatch, account, media)
        _run(tf.scan_one(_COMPETITOR, download_thumbs=False))
        assert store.results[0][1]["profile_picture_url"] == "https://cdn/avatar.jpg"


def test_start_scan_records_what_started_it(monkeypatch):
    created: list[dict] = []

    class _Store:
        async def ensure_tables(self):
            return None

        async def scan_is_active(self):
            return False

        async def list_competitors(self, status=None):
            return [{"id": 1}, {"id": 2}]

        async def create_scan(self, total, metadata=None):
            created.append({"total": total, **(metadata or {})})
            return {"id": 5}

    monkeypatch.setattr(tf, "store", _Store())
    _run(tf.start_scan(None, max_posts=50))
    _run(tf.start_scan(None, max_posts=50, trigger="auto"))
    assert [c["trigger"] for c in created] == ["manual", "auto"]
    assert created[1]["total"] == 2 and created[1]["competitor_ids"] is None


class _FakeCursor:
    def __init__(self, sink: list):
        self._sink = sink

    async def execute(self, sql, params=None):
        self._sink.append((" ".join(str(sql).split()), params))

    async def fetchone(self):
        return None

    async def fetchall(self):
        return []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _FakeConn:
    def __init__(self, sink: list):
        self._sink = sink

    def cursor(self):
        return _FakeCursor(self._sink)


def _capture_sql(monkeypatch) -> list:
    """Run a trend_store coroutine against a fake connection, capturing the SQL
    and the bound parameters. No database is contacted."""
    from contextlib import asynccontextmanager

    sink: list = []

    @asynccontextmanager
    async def _get_conn(*args, **kwargs):
        yield _FakeConn(sink)

    monkeypatch.setattr(tstore, "get_conn", _get_conn)
    monkeypatch.setattr(tstore, "_ensured", True)   # skip the DDL round-trip
    return sink


def test_record_scan_result_binds_what_the_success_path_sends(monkeypatch):
    sink = _capture_sql(monkeypatch)
    _run(tstore.record_scan_result(
        7, status="ok", error=None, posts_found=42, median_engagement=30.0,
        followers_count=1000, media_count=4200))
    sql, params = sink[0]
    assert "UPDATE trend_competitors" in sql
    assert params == ("ok", None, 42, 30.0, 1000, 4200, None, 7)
    # The profile numbers are COALESCEd so a partial scan can't blank them.
    assert "median_engagement = COALESCE(%s, median_engagement)" in sql
    assert "followers_count = COALESCE(%s, followers_count)" in sql


def test_record_scan_result_refreshes_the_profile_picture(monkeypatch):
    # Instagram's picture links expire in days, so every scan stores the fresh
    # one, and a scan that read none keeps the last.
    sink = _capture_sql(monkeypatch)
    _run(tstore.record_scan_result(7, status="ok", profile_picture_url="https://cdn/new.jpg"))
    sql, params = sink[0]
    assert "profile_picture_url = COALESCE(%s, profile_picture_url)" in sql
    assert params[-2:] == ("https://cdn/new.jpg", 7)


def test_latest_full_scan_ignores_one_account_scans(monkeypatch):
    sink = _capture_sql(monkeypatch)
    _run(tstore.latest_scan(full_only=True))
    sql, _ = sink[0]
    assert "jsonb_typeof(metadata->'competitor_ids') IS DISTINCT FROM 'array'" in sql
    sink.clear()
    _run(tstore.latest_scan())
    assert "competitor_ids" not in sink[0][0]


def test_failed_scan_keeps_the_previous_post_count(monkeypatch):
    """REGRESSION (bug found by this suite, since fixed).

    ``scan_one``'s failure path calls ``store.record_scan_result(cid,
    status='failed', error=msg)`` without ``posts_found``, which defaults to 0 —
    and ``record_scan_result``'s SQL sets ``posts_found = %s`` UNCONDITIONALLY
    (unlike median_engagement / followers_count / media_count, which are all
    wrapped in COALESCE). So one transient failure — an Instagram rate limit, a
    timeout — resets a competitor that had 143 posts to "0 posts found" in the
    UI, even though those rows are all still in trend_posts.

    Fix: default ``posts_found`` to None and COALESCE it in the UPDATE, the way
    the three profile columns beside it already are.
    """
    sink = _capture_sql(monkeypatch)
    # Exactly what scan_one's unreachable-account path sends.
    _run(tstore.record_scan_result(7, status="failed", error="rate limit reached"))
    sql, params = sink[0]
    assert "posts_found = COALESCE(%s, posts_found)" in sql
    assert params[2] is None       # today it binds 0, overwriting the real count


def test_untrustworthy_baseline_is_damped_and_capped(monkeypatch):
    """REGRESSION (bug found by this suite, since fixed).

    trend_finder documents MIN_POSTS_FOR_BASELINE as "below this many posts an
    account's median is too noisy to call anything an outlier, so outperformance
    is reported but not trusted for ranking" — and ``scan_one`` duly writes
    ``metadata.baseline_trustworthy``. Nothing ever reads it: grep finds exactly
    one write and zero reads in the whole repo, and ``compute_opportunity`` /
    ``rank_posts`` never look at it. An account with 2 posts (one flat, one
    slightly better) therefore produces an "outperformance" nobody should trust,
    and it outranks a genuinely viral post from a well-sampled account.

    Fix: either damp outperformance when the sample is below the threshold, or
    drop the constant and the metadata flag so the comment stops promising it.
    """
    store = _FakeStore()
    monkeypatch.setattr(tf, "store", store)
    # 3 posts (< MIN_POSTS_FOR_BASELINE): engagements 1, 1, 100 -> median 1.
    _patch_fetch(monkeypatch, _ACCOUNT,
                 [_media("a", 1, 0), _media("b", 1, 0), _media("c", 100, 0)])

    _run(tf.scan_one(_COMPETITOR, download_thumbs=False))
    thin = store.by_media_id["c"]
    assert thin["metadata"]["baseline_trustworthy"] is False
    # The raw ratio is kept for transparency, but the ranking value is damped
    # AND capped: 3 posts buy at most a 2x claim.
    assert thin["metadata"]["raw_outperformance"] == 100.0
    assert thin["outperformance"] == 2.0

    # A 3-post account should not out-rank @42abudhabi's real, measured 4.7x hit.
    well_sampled = {"outperformance": 4.7}
    assert ts.compute_opportunity(thin) < ts.compute_opportunity(well_sampled)


# =========================================================================== #
# 10. Integration-shaped: trend_research.research_competitors
# =========================================================================== #

_TRACKED = [
    {"username": "visitabudhabi", "status": "active"},
    {"username": "42abudhabi", "status": "active"},
    {"username": "adnoc", "status": "excluded"},
]


def _fake_ig(monkeypatch, *, unreachable=(), calls=None):
    async def _fetch(username, *, page_size=None, after=None):
        if calls is not None:
            calls.append(username)
        if username in unreachable:
            raise ig.InstagramAPIError("(#100) Invalid user id")
        return {
            "name": f"{username} official", "biography": "bio",
            "profile_picture_url": f"https://cdn/{username}.jpg",
            "followers_count": 12345, "media_count": 678,
        }
    monkeypatch.setattr(tr.ig, "fetch_account_media", _fetch)


def _fake_model(monkeypatch, candidates, *, prompts=None):
    async def _gemini_text(prompt, **kwargs):
        if prompts is not None:
            prompts.append((prompt, kwargs))
        return {"candidates": candidates}
    monkeypatch.setattr(tr, "gemini_text", _gemini_text)


def _fake_list(monkeypatch, rows=_TRACKED):
    async def _list_competitors(**kwargs):
        return [dict(r) for r in rows]
    monkeypatch.setattr(tr.store, "list_competitors", _list_competitors)


def test_research_filters_tracked_and_excluded_before_validating(monkeypatch):
    ig_calls: list[str] = []
    prompts: list = []
    _fake_list(monkeypatch)
    _fake_ig(monkeypatch, calls=ig_calls)
    _fake_model(monkeypatch, [
        {"username": "VisitAbuDhabi", "why": "already watched", "region": "uae"},
        {"username": "https://instagram.com/adnoc/", "why": "the user ignored this",
         "region": "uae"},
        {"username": "@nyuad", "why": "campus life", "region": "uae"},
        {"username": "nyuad", "why": "a duplicate of the above", "region": "uae"},
    ], prompts=prompts)

    out = _run(tr.research_competitors(hint="universities", limit=8))

    # Only the one genuinely new handle was ever sent to Instagram.
    assert ig_calls == ["nyuad"]
    assert out["tried"] == 1
    assert [c["username"] for c in out["candidates"]] == ["nyuad"]
    assert out["rejected"] == []
    # ...and the model was told about both lists up front.
    prompt, kwargs = prompts[0]
    assert "@visitabudhabi" in prompt and "@42abudhabi" in prompt
    assert "REJECTED by the team" in prompt and "@adnoc" in prompt
    assert "The team specifically wants: universities" in prompt
    assert kwargs["json_schema"] is tr._SCHEMA


def test_research_puts_unreachable_candidates_in_rejected(monkeypatch):
    _fake_list(monkeypatch, rows=[])
    _fake_ig(monkeypatch, unreachable={"42network", "42lausanne"})
    _fake_model(monkeypatch, [
        {"username": "42network", "why": "peer school", "region": "international"},
        {"username": "nyuad", "why": "campus life", "region": "uae"},
        {"username": "42lausanne", "why": "peer school", "region": "international"},
    ])

    out = _run(tr.research_competitors(limit=8))

    assert out["tried"] == 3
    assert [c["username"] for c in out["candidates"]] == ["nyuad"]
    rejected = {r["username"]: r for r in out["rejected"]}
    assert set(rejected) == {"42network", "42lausanne"}
    for row in rejected.values():
        assert row["ok"] is False
        assert "Business or Creator" in row["error"]
        assert row["why"]                      # the model's reason is kept for the UI


def test_research_enriches_reachable_candidates_from_the_real_profile(monkeypatch):
    _fake_list(monkeypatch, rows=[])
    _fake_ig(monkeypatch)
    _fake_model(monkeypatch, [
        {"username": "@NYUAD", "display_name": "model's guess", "why": "campus life",
         "category": "university", "region": "martian"},
    ])

    out = _run(tr.research_competitors())

    cand = out["candidates"][0]
    assert cand["username"] == "nyuad"                    # normalised
    assert cand["ok"] is True
    assert cand["display_name"] == "nyuad official"       # the API wins over the model
    assert cand["followers_count"] == 12345
    assert cand["media_count"] == 678
    assert cand["category"] == "university"
    assert cand["region"] == "uae"                        # invented region -> default


def test_research_rejects_an_invented_handle_without_calling_instagram(monkeypatch):
    ig_calls: list[str] = []
    _fake_list(monkeypatch, rows=[])
    _fake_ig(monkeypatch, calls=ig_calls)
    _fake_model(monkeypatch, [
        {"username": "not a real handle!", "why": "hallucinated", "region": "uae"},
        {"username": "nyuad", "why": "real", "region": "uae"},
    ])

    out = _run(tr.research_competitors())

    assert ig_calls == ["nyuad"]                       # never sent to the Graph API
    assert [c["username"] for c in out["candidates"]] == ["nyuad"]
    assert len(out["rejected"]) == 1
    assert out["rejected"][0]["ok"] is False
    assert "isn't a valid Instagram handle" in out["rejected"][0]["error"]


def test_research_short_circuits_when_everything_is_already_tracked(monkeypatch):
    ig_calls: list[str] = []
    _fake_list(monkeypatch)
    _fake_ig(monkeypatch, calls=ig_calls)
    _fake_model(monkeypatch, [
        {"username": "visitabudhabi", "why": "x", "region": "uae"},
        {"username": "adnoc", "why": "x", "region": "uae"},
    ])

    out = _run(tr.research_competitors())

    assert out["candidates"] == [] and out["rejected"] == [] and out["tried"] == 0
    assert "already tracked" in out["note"]
    assert ig_calls == []


def test_research_caps_the_returned_candidates_at_the_limit(monkeypatch):
    _fake_list(monkeypatch, rows=[])
    _fake_ig(monkeypatch)
    _fake_model(monkeypatch, [
        {"username": f"acct{i}", "why": "x", "region": "uae"} for i in range(12)
    ])

    out = _run(tr.research_competitors(limit=3))

    assert len(out["candidates"]) == 3      # trimmed to the limit on the way out
    assert out["tried"] == 12               # ...but every proposal was counted


def test_research_asks_for_more_than_it_needs(monkeypatch):
    prompts: list = []
    _fake_list(monkeypatch, rows=[])
    _fake_ig(monkeypatch)
    _fake_model(monkeypatch, [], prompts=prompts)

    _run(tr.research_competitors(limit=5))

    # Validation culls roughly half, so the prompt over-asks by 4.
    assert "up to 9 Instagram accounts" in prompts[0][0]


def test_research_clamps_a_silly_limit(monkeypatch):
    prompts: list = []
    _fake_list(monkeypatch, rows=[])
    _fake_ig(monkeypatch)
    _fake_model(monkeypatch, [], prompts=prompts)

    _run(tr.research_competitors(limit=999))
    assert f"up to {tr.MAX_CANDIDATES + 4} Instagram accounts" in prompts[0][0]

    prompts.clear()
    _run(tr.research_competitors(limit=0))          # falsy -> the default 8
    assert "up to 12 Instagram accounts" in prompts[0][0]

    prompts.clear()
    _run(tr.research_competitors(limit=-4))         # negative -> floored at 1
    assert "up to 5 Instagram accounts" in prompts[0][0]


def test_research_unreadable_model_reply_is_a_clear_error(monkeypatch):
    _fake_list(monkeypatch, rows=[])
    _fake_ig(monkeypatch)

    async def _raw(prompt, **kwargs):
        return {"_raw": "```json ... truncated"}
    monkeypatch.setattr(tr, "gemini_text", _raw)

    with pytest.raises(RuntimeError) as exc:
        _run(tr.research_competitors())
    assert "unreadable" in str(exc.value)


def test_research_model_failure_is_wrapped(monkeypatch):
    _fake_list(monkeypatch, rows=[])
    _fake_ig(monkeypatch)

    async def _boom(prompt, **kwargs):
        raise RuntimeError("429 quota exceeded")
    monkeypatch.setattr(tr, "gemini_text", _boom)

    with pytest.raises(RuntimeError) as exc:
        _run(tr.research_competitors())
    assert "couldn't reach the research model" in str(exc.value)


def test_research_survives_a_candidate_that_explodes(monkeypatch):
    # asyncio.gather(..., return_exceptions=True): one blown-up validation must
    # not take the whole research run down with it.
    _fake_list(monkeypatch, rows=[])
    _fake_model(monkeypatch, [
        {"username": "boom", "why": "x", "region": "uae"},
        {"username": "nyuad", "why": "x", "region": "uae"},
    ])

    async def _fetch(username, *, page_size=None, after=None):
        if username == "boom":
            raise ig.InstagramConfigError("token missing")   # re-raised by _validate_one
        return {"name": username, "followers_count": 1}
    monkeypatch.setattr(tr.ig, "fetch_account_media", _fetch)

    out = _run(tr.research_competitors())
    assert [c["username"] for c in out["candidates"]] == ["nyuad"]
    assert out["tried"] == 2


# =========================================================================== #
# 11. trend_research focus dials — the four Research-panel knobs.
#
# These steer WHICH accounts the model proposes, and every dial's FIRST option
# must reproduce the previous behaviour byte-for-byte. That is why sections 8
# and 10 above are left completely untouched: they ARE the defaults contract.
# =========================================================================== #

def test_focus_defaults_match_the_previous_behaviour():
    assert tr.DEFAULT_FOCUS == tr.Focus("uae", "normal", "any", "any")
    assert (tr.GEO_OPTIONS[0], tr.RELEVANCE_OPTIONS[0],
            tr.ENGAGEMENT_OPTIONS[0], tr.SIZE_OPTIONS[0]) == \
        ("uae", "normal", "any", "any")


def test_normalise_focus_falls_back_per_dial():
    # A stale frontend sending junk must never break the call.
    assert tr.normalise_focus(geo="martian", relevance=None, engagement="",
                              size="HUGE") == tr.Focus()
    assert tr.normalise_focus() == tr.Focus()
    # ...but a good value survives, case- and whitespace-insensitively.
    assert tr.normalise_focus(geo="  GLOBAL ", relevance="high",
                              engagement="high", size="Small") == \
        tr.Focus("global", "high", "high", "small")


def test_research_prompt_default_still_asks_for_the_uae_quota():
    prompt = tr.build_prompt(tracked=[], excluded=[], hint=None, limit=8)
    assert "three quarters UAE/GCC" in prompt
    assert "Search globally" not in prompt


def test_research_prompt_global_dial_removes_the_uae_bias():
    # "UAE dial OFF" must leave NO local weighting behind at all.
    prompt = tr.build_prompt(tracked=[], excluded=[], hint=None, limit=8,
                             focus=tr.Focus(geo="global"))
    assert "Search globally" in prompt
    assert "three quarters UAE/GCC" not in prompt
    assert "GCC" not in prompt
    # The single surviving mention of the UAE is the explicit negation.
    assert prompt.count("UAE") == 1 and "no UAE quota" in prompt


def test_research_prompt_relevance_and_engagement_dials():
    base = tr.build_prompt(tracked=[], excluded=[], hint=None, limit=8)
    assert "borrow a post format" not in base
    assert "consistently high likes and comments" not in base

    tight = tr.build_prompt(tracked=[], excluded=[], hint=None, limit=8,
                            focus=tr.Focus(relevance="high"))
    assert "borrow a post format" in tight

    loud = tr.build_prompt(tracked=[], excluded=[], hint=None, limit=8,
                           focus=tr.Focus(engagement="high"))
    assert "consistently high likes and comments" in loud


def test_research_prompt_size_dial_is_exclusive():
    base = tr.build_prompt(tracked=[], excluded=[], hint=None, limit=8)
    assert "5k to 150k followers" not in base and "150k followers and up" not in base

    small = tr.build_prompt(tracked=[], excluded=[], hint=None, limit=8,
                            focus=tr.Focus(size="small"))
    assert "5k to 150k followers" in small and "150k followers and up" not in small

    large = tr.build_prompt(tracked=[], excluded=[], hint=None, limit=8,
                            focus=tr.Focus(size="large"))
    assert "150k followers and up" in large and "5k to 150k followers" not in large


def test_research_prompt_global_high_relevance_asks_for_global_engaged_accounts():
    # The headline combination: 42-relevance ON with the UAE dial OFF.
    focus = tr.Focus(geo="global", relevance="high", engagement="high")
    prompt = tr.build_prompt(tracked=[], excluded=[], hint=None, limit=8,
                             focus=focus)
    assert "Search globally" in prompt
    assert "borrow a post format" in prompt
    assert "consistently high likes and comments" in prompt
    assert "three quarters UAE/GCC" not in prompt

    system = tr.build_system(focus=focus)
    assert "WORLDWIDE" in system
    assert "TIGHT RELEVANCE" in system and "HIGH ENGAGEMENT ONLY" in system
    assert "MOSTLY UAE" not in system and "Weight the list toward the UAE" not in system


def test_focus_copy_never_collides_with_the_prompt_sections():
    # The tracked/excluded blocks are matched by literal markers elsewhere in
    # this file — no dial's copy may accidentally contain them.
    prompt = tr.build_prompt(
        tracked=[], excluded=[], hint=None, limit=8,
        focus=tr.Focus(geo="global", relevance="high", engagement="high",
                       size="small"))
    assert "ALREADY TRACKED" not in prompt
    assert "REJECTED" not in prompt
    assert "@" not in prompt


def test_build_system_default_is_the_module_constant():
    assert tr.build_system() == tr._SYSTEM
    assert "MOSTLY UAE" in tr._SYSTEM


def test_build_system_global_searches_worldwide():
    system = tr.build_system(focus=tr.Focus(geo="global"))
    assert "WORLDWIDE" in system
    assert "MOSTLY UAE" not in system
    # The shared intro and rules survive every dial combination.
    assert "tuition-free" in system and "Do not invent handles" in system


def test_size_rank_is_a_preference_not_a_filter():
    # No follower number at all (Business Discovery can return None) sorts last
    # for either band, but "any" leaves the model's order untouched.
    unknown = {"username": "x"}
    assert tr._size_rank(unknown, "any") == 0
    assert tr._size_rank(unknown, "small") == 1
    assert tr._size_rank(unknown, "large") == 1

    assert tr._size_rank({"followers_count": 3_000}, "small") == 0
    assert tr._size_rank({"followers_count": 3_000}, "large") == 1
    assert tr._size_rank({"followers_count": 200_000}, "large") == 0
    assert tr._size_rank({"followers_count": 200_000}, "small") == 1
    # The boundary belongs to "large".
    assert tr._size_rank({"followers_count": 150_000}, "large") == 0
    assert tr._size_rank({"followers_count": 150_000}, "small") == 1


# =========================================================================== #
# 12. Integration-shaped: research_competitors WITH focus (fakes only)
# =========================================================================== #

def _fake_ig_sized(monkeypatch, followers: dict):
    """Like _fake_ig, but every handle gets its own follower count."""
    async def _fetch(username, *, page_size=None, after=None):
        return {
            "name": f"{username} official", "biography": "bio",
            "profile_picture_url": f"https://cdn/{username}.jpg",
            "followers_count": followers.get(username), "media_count": 10,
        }
    monkeypatch.setattr(tr.ig, "fetch_account_media", _fetch)


def test_research_global_dial_reaches_the_system_prompt(monkeypatch):
    prompts: list = []
    _fake_list(monkeypatch, rows=[])
    _fake_ig(monkeypatch)
    _fake_model(monkeypatch, [{"username": "nyuad", "why": "x", "region": "uae"}],
                prompts=prompts)

    _run(tr.research_competitors(geo="global"))

    prompt, kwargs = prompts[0]
    assert "WORLDWIDE" in kwargs["system"]
    assert "MOSTLY UAE" not in kwargs["system"]
    assert "Search globally" in prompt
    # The schema object is shared, never rebuilt per focus.
    assert kwargs["json_schema"] is tr._SCHEMA


def test_research_default_still_sends_the_uae_system_prompt(monkeypatch):
    prompts: list = []
    _fake_list(monkeypatch, rows=[])
    _fake_ig(monkeypatch)
    _fake_model(monkeypatch, [], prompts=prompts)

    _run(tr.research_competitors())

    kwargs = prompts[0][1]
    assert kwargs["system"] == tr._SYSTEM
    assert kwargs["json_schema"] is tr._SCHEMA


def test_research_global_defaults_a_missing_region_to_international(monkeypatch):
    # Silently stamping "uae" on a worldwide result would poison the region
    # filters and the uae weight in the opportunity blend.
    _fake_list(monkeypatch, rows=[])
    _fake_ig(monkeypatch)
    _fake_model(monkeypatch, [{"username": "nyuad", "why": "x"}])

    out = _run(tr.research_competitors(geo="global"))
    assert out["candidates"][0]["region"] == "international"


def test_research_default_still_defaults_a_missing_region_to_uae(monkeypatch):
    _fake_list(monkeypatch, rows=[])
    _fake_ig(monkeypatch)
    _fake_model(monkeypatch, [{"username": "nyuad", "why": "x"}])

    out = _run(tr.research_competitors())
    assert out["candidates"][0]["region"] == "uae"


def test_research_size_dial_reorders_before_the_trim(monkeypatch):
    _fake_list(monkeypatch, rows=[])
    _fake_ig_sized(monkeypatch, {"tinyschool": 3_000, "bigschool": 200_000})
    model = [
        {"username": "tinyschool", "why": "x", "region": "international"},
        {"username": "bigschool", "why": "x", "region": "international"},
    ]

    _fake_model(monkeypatch, model)
    out = _run(tr.research_competitors(limit=1, size="large"))
    assert [c["username"] for c in out["candidates"]] == ["bigschool"]

    _fake_model(monkeypatch, model)
    out = _run(tr.research_competitors(limit=1, size="small"))
    assert [c["username"] for c in out["candidates"]] == ["tinyschool"]

    # "any" is a no-op ordering — the model's own order survives.
    _fake_model(monkeypatch, model)
    out = _run(tr.research_competitors(limit=2, size="any"))
    assert [c["username"] for c in out["candidates"]] == ["tinyschool", "bigschool"]


def test_research_size_dial_never_drops_a_reachable_handle(monkeypatch):
    # A preference, never a filter: Business Discovery reachability is the
    # scarce resource and an empty result reads as a broken button.
    _fake_list(monkeypatch, rows=[])
    _fake_ig_sized(monkeypatch, {"tinyschool": 3_000, "bigschool": 200_000})
    _fake_model(monkeypatch, [
        {"username": "tinyschool", "why": "x", "region": "international"},
        {"username": "bigschool", "why": "x", "region": "international"},
    ])

    out = _run(tr.research_competitors(limit=8, size="large"))
    assert [c["username"] for c in out["candidates"]] == ["bigschool", "tinyschool"]


def test_research_junk_dials_fall_back_instead_of_raising(monkeypatch):
    prompts: list = []
    _fake_list(monkeypatch, rows=[])
    _fake_ig(monkeypatch)
    _fake_model(monkeypatch, [], prompts=prompts)

    _run(tr.research_competitors(geo="mars", relevance="", engagement=None,
                                 size="enormous"))
    assert prompts[0][1]["system"] == tr._SYSTEM
    assert "three quarters UAE/GCC" in prompts[0][0]


# =========================================================================== #
# 13. trend_ideation — one post + one Producer tool -> a ready-to-use brief
# =========================================================================== #

_IDEA_POST = {
    "id": 412,
    "competitor_id": 7,
    "username": "nyuad",
    "permalink": "https://www.instagram.com/p/Cxyz/",
    "media_type": "VIDEO",
    "media_product_type": "REELS",
    "caption": "Paper ball tournament in the atrium",
    "posted_at": datetime(2026, 7, 1, tzinfo=timezone.utc),
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
    "scored_at": datetime(2026, 7, 2, tzinfo=timezone.utc),
}

_IDEA_COMPETITOR = {
    "id": 7, "username": "nyuad", "display_name": "NYU Abu Dhabi",
    "followers_count": 120_000, "region": "uae",
}


def test_ideation_prompt_carries_the_post_and_the_tool_spec():
    prompt = ti.build_prompt(post=_IDEA_POST, competitor=_IDEA_COMPETITOR,
                             tool="talking_head_v2", brand_note="voice: direct")
    assert "WHAT 42 SOUNDS AND LOOKS LIKE" in prompt and "voice: direct" in prompt
    assert "@nyuad" in prompt and "NYU Abu Dhabi" in prompt
    assert "format: VIDEO / REELS" in prompt
    assert "4200 likes, 310 comments" in prompt
    assert "views: 91000" in prompt
    assert "performed 4.2x that account's own average" in prompt
    assert "posted: 2026-07-01" in prompt
    assert "Rivalry games between study groups" in prompt          # the angle
    assert "Run a cluster-vs-cluster mini tournament." in prompt
    assert "fit for 42: 88/100" in prompt and "UAE relevance: 71/100" in prompt
    assert ti._TOOL_BRIEFS["talking_head_v2"] in prompt


def test_ideation_prompt_never_prints_zero_views_for_a_still():
    # view_count is VIDEO-only and deliberately NULL for images and carousels —
    # "views: 0" would read as a flop rather than as "not applicable".
    post = {**_IDEA_POST, "media_type": "IMAGE", "media_product_type": None,
            "view_count": None}
    prompt = ti.build_prompt(post=post, competitor=None, tool="poster")
    assert "views" not in prompt
    # ...and a missing media_product_type leaves no dangling separator.
    assert "format: IMAGE\n" in prompt


def test_ideation_prompt_handles_an_unscored_post():
    post = {**_IDEA_POST, "scored_at": None}
    prompt = ti.build_prompt(post=post, competitor=None, tool="poster")
    assert "has not been scored yet" in prompt
    assert "WHAT THE SCORER ALREADY WORKED OUT" not in prompt


def test_ideation_prompt_skips_null_scorer_fields():
    post = {**_IDEA_POST, "fit_reason": None, "topic_summary": None,
            "suggested_format": None, "effort": None, "tags": None}
    prompt = ti.build_prompt(post=post, competitor=None, tool="poster")
    assert "fit for 42: 88/100" in prompt and "fit for 42: 88/100 —" not in prompt
    assert "why it worked" not in prompt
    assert "suggested format" not in prompt and "effort:" not in prompt
    assert "tags:" not in prompt


def test_ideation_prompt_without_a_competitor_row():
    # get_post projects POST_COLS only, so the competitor may legitimately be
    # missing — the handle still has to appear.
    prompt = ti.build_prompt(post=_IDEA_POST, competitor=None, tool="poster")
    assert "@nyuad" in prompt
    assert "NYU Abu Dhabi" not in prompt and "followers:" not in prompt


def test_ideation_prompt_covers_every_producer_tool():
    for tool in ti.TOOLS:
        prompt = ti.build_prompt(post=_IDEA_POST, competitor=_IDEA_COMPETITOR,
                                 tool=tool)
        assert ti._TOOL_BRIEFS[tool] in prompt, tool
        assert "Fill only the fields this tool asks for." in prompt
    assert len(ti._TOOL_BRIEFS) == len(ti.TOOLS) == 8


def test_ideation_prompt_collapses_and_truncates_the_caption():
    post = {**_IDEA_POST, "caption": "a\n\n  b " + "c" * 900}
    prompt = ti.build_prompt(post=post, competitor=None, tool="poster")
    assert "caption: a b " + "c" * 596 in prompt      # collapsed, then [:600]
    assert "c" * 597 not in prompt


def test_parse_prefill_zeroes_fields_the_tool_cannot_use():
    raw = {"title": "Compile Cup", "topic": "A poster about the tournament.",
           "dialogue": ["not for a poster"], "character": "not for a poster",
           "intro_topic": "nope", "outro_topic": "nope", "notes": "rivalry sells"}
    out = ti.parse_prefill(raw, tool="poster", post=_IDEA_POST)
    assert out["dialogue"] == []
    assert out["character"] == ""
    assert out["intro_topic"] == "" and out["outro_topic"] == ""
    assert out["notes"] == "rivalry sells"
    # Every key is always present, so the frontend never branches on undefined.
    assert set(out) == {"tool", "post_id", "title", "topic", "dialogue",
                        "intro_topic", "outro_topic", "character", "notes",
                        "permalink"}
    assert out["tool"] == "poster" and out["post_id"] == 412
    assert out["permalink"] == "https://www.instagram.com/p/Cxyz/"


def test_parse_prefill_keeps_the_per_tool_fields_for_their_own_tool():
    raw = {"title": "t", "topic": "x", "dialogue": ["one"],
           "intro_topic": "in", "outro_topic": "out", "character": "a student"}
    assert ti.parse_prefill(raw, tool="talking_head_v2",
                            post=_IDEA_POST)["dialogue"] == ["one"]
    broll = ti.parse_prefill(raw, tool="broll", post=_IDEA_POST)
    assert broll["intro_topic"] == "in" and broll["outro_topic"] == "out"
    assert ti.parse_prefill(raw, tool="human_character",
                            post=_IDEA_POST)["character"] == "a student"


def test_parse_prefill_caps_and_cleans_the_dialogue():
    raw = {"title": "t", "topic": "x",
           "dialogue": ["  one  ", "", "   ", "two", "three", "four"]}
    out = ti.parse_prefill(raw, tool="talking_head_v2", post=_IDEA_POST)
    assert out["dialogue"] == ["one", "two", "three"]
    assert len(out["dialogue"]) == ti.MAX_DIALOGUE_LINES


def test_parse_prefill_falls_back_when_the_model_left_the_box_empty():
    out = ti.parse_prefill({"title": "", "topic": "  "}, tool="poster",
                           post=_IDEA_POST)
    # An empty box is worse than a rough one.
    assert out["topic"] == "Run a cluster-vs-cluster mini tournament."
    assert out["title"] == "Rivalry games between study groups"


def test_parse_prefill_falls_back_all_the_way_to_the_caption():
    post = {k: v for k, v in _IDEA_POST.items()}
    post.update(topic=None, suggested_angle=None, topic_summary=None)
    out = ti.parse_prefill({}, tool="poster", post=post)
    assert out["topic"] == "Paper ball tournament in the atrium"
    assert out["title"] == "Paper ball tournament in the atrium"


def test_parse_prefill_survives_a_non_dict_reply():
    out = ti.parse_prefill(["nope"], tool="poster", post=_IDEA_POST)
    assert out["topic"] and out["title"] and out["dialogue"] == []


def _fake_ideation(monkeypatch, *, reply=None, post=_IDEA_POST, boom=None):
    async def _get_post(post_id):
        return dict(post) if post else None
    async def _get_competitor(competitor_id):
        return dict(_IDEA_COMPETITOR)
    async def _brand_note():
        return ""
    async def _gemini_text(prompt, **kwargs):
        if boom is not None:
            raise boom
        return reply
    monkeypatch.setattr(ti.store, "get_post", _get_post)
    monkeypatch.setattr(ti.store, "get_competitor", _get_competitor)
    monkeypatch.setattr(ti.trend_scoring, "_brand_note", _brand_note)
    monkeypatch.setattr(ti, "gemini_text", _gemini_text)


def test_generate_prefill_returns_the_envelope(monkeypatch):
    _fake_ideation(monkeypatch, reply={
        "title": "42 AD Compile Cup",
        "topic": "A campus-wide paper-ball tournament between clusters.",
        "dialogue": ["Every Friday the cluster stops coding and we throw paper."],
        "notes": "It's a rivalry format.",
    })
    out = _run(ti.generate_prefill(post_id=412, tool="talking_head_v2"))
    assert out["tool"] == "talking_head_v2" and out["post_id"] == 412
    assert out["title"] == "42 AD Compile Cup"
    assert len(out["dialogue"]) == 1
    assert out["character"] == "" and out["intro_topic"] == ""


def test_generate_prefill_rejects_an_unknown_tool(monkeypatch):
    _fake_ideation(monkeypatch, reply={"title": "t", "topic": "x"})
    with pytest.raises(ValueError) as exc:
        _run(ti.generate_prefill(post_id=412, tool="nope"))
    assert "tool must be one of" in str(exc.value)


def test_generate_prefill_missing_post_is_a_lookup_error(monkeypatch):
    _fake_ideation(monkeypatch, reply={"title": "t", "topic": "x"}, post=None)
    with pytest.raises(LookupError):
        _run(ti.generate_prefill(post_id=9999, tool="poster"))


def test_generate_prefill_truncated_reply_is_a_clear_error(monkeypatch):
    # gemini_text never raises on unparseable JSON — it returns the sentinel.
    _fake_ideation(monkeypatch, reply={"_raw": "```json ... truncated"})
    with pytest.raises(RuntimeError) as exc:
        _run(ti.generate_prefill(post_id=412, tool="poster"))
    assert "unreadable" in str(exc.value)


def test_generate_prefill_billing_failure_is_unavailable_not_a_502(monkeypatch):
    _fake_ideation(monkeypatch, boom=RuntimeError("403 billing is disabled"))
    with pytest.raises(ti.IdeationUnavailable) as exc:
        _run(ti.generate_prefill(post_id=412, tool="poster"))
    assert "billing" in str(exc.value).lower()


def test_generate_prefill_budget_cap_is_unavailable(monkeypatch):
    from app.services.vertex_client import VertexBudgetExceededError

    _fake_ideation(monkeypatch, boom=VertexBudgetExceededError("cap hit"))
    with pytest.raises(ti.IdeationUnavailable) as exc:
        _run(ti.generate_prefill(post_id=412, tool="poster"))
    assert "spend cap" in str(exc.value)


def test_generate_prefill_transient_failure_is_wrapped(monkeypatch):
    _fake_ideation(monkeypatch, boom=RuntimeError("429 quota exceeded"))
    with pytest.raises(RuntimeError) as exc:
        _run(ti.generate_prefill(post_id=412, tool="poster"))
    assert "couldn't reach the ideation model" in str(exc.value)
    assert not isinstance(exc.value, ti.IdeationUnavailable)


def test_schema_for_plain_tool_is_the_base_schema():
    # No must-have field of its own → the identical object, not a copy.
    assert ti.schema_for("poster") is ti._SCHEMA
    assert ti.schema_for("general_video") is ti._SCHEMA


@pytest.mark.parametrize(
    "tool,must_have",
    [
        ("talking_head_v2", ["dialogue"]),
        ("broll", ["intro_topic", "outro_topic"]),
        ("human_character", ["character"]),
    ],
)
def test_schema_for_requires_the_field_that_tool_cannot_be_seeded_without(tool, must_have):
    # Observed live: left merely "described", the model drops these — a
    # human_character brief came back with no `character` at all and a `topic`
    # that described a scene instead of a person.
    schema = ti.schema_for(tool)
    for field in must_have:
        assert field in schema["required"], f"{tool} must require {field}"
        assert field in schema["properties"], f"{tool}'s {field} must still be declared"
    # title/topic stay required for every tool.
    assert {"title", "topic"} <= set(schema["required"])


def test_schema_for_never_mutates_the_shared_base_schema():
    before = list(ti._SCHEMA["required"])
    ti.schema_for("human_character")
    ti.schema_for("broll")
    assert ti._SCHEMA["required"] == before


def test_schema_for_unknown_tool_falls_back_to_the_base_schema():
    assert ti.schema_for("no_such_tool") is ti._SCHEMA


if __name__ == "__main__":  # pragma: no cover — standalone runner
    raise SystemExit(pytest.main([__file__, "-q"]))
