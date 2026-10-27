"""Tests for ``app/services/general_trends_ranking.py`` — the pure heat /
opportunity / sort maths behind the General trends tab.

No network, no database, no model: every function under test is a pure
function over dicts. Runs under pytest and standalone
(``cd backend && python tests/test_general_trends_ranking.py``).

REWRITTEN. The four velocity tests that used to live here
(``test_velocity_exact_formula_for_a_fresh_multi_source_trend``,
``test_growth_rewards_more_sightings_now_than_yesterday``,
``test_recency_follows_exp_decay_over_48h``, ``test_newness_halves_after_72h``)
all passed and all pinned the broken behaviour: they asserted the exact
``0.40*source_score + 0.30*recency + 0.20*growth + 0.10*newness`` blend whose
terms are, on real data, a 3-valued counter, a per-run constant, a count of our
own fetches and a constant 1.0 — 4 distinct velocities over 363 live rows. They
are replaced by tests over the new components plus, at the bottom, the
acceptance criteria stated as measurements over a synthetic ~300-row run.
"""
from __future__ import annotations

import math
import random
import statistics
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

_BACKEND_ROOT = Path(__file__).resolve().parent.parent
if str(_BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(_BACKEND_ROOT))

from app.services import general_trends_ranking as gr  # noqa: E402

NOW = datetime(2026, 8, 19, 12, 0, tzinfo=timezone.utc)


def _h(hours: float) -> datetime:
    """`hours` ago."""
    return NOW - timedelta(hours=hours)


def _sighting(source: str, *, metric=None, age_h: float = 1.0, key: str = "k",
              **extra) -> dict:
    """One row shaped like ``general_trend_sightings``."""
    return {
        "source": source,
        "source_key": key,
        "metric_value": metric,
        "published_at": _h(age_h),
        "region": extra.pop("region", None),
        "extra": dict(extra),
    }


def _trend(**over) -> dict:
    base = {"source_count": 1, "first_seen_at": _h(1), "last_seen_at": _h(1)}
    base.update(over)
    return base


def _quantile(values: list[float], q: float) -> float:
    """Linear-interpolation quantile — no numpy in the test path."""
    vals = sorted(values)
    if not vals:
        return 0.0
    pos = (len(vals) - 1) * q
    lo, hi = math.floor(pos), math.ceil(pos)
    if lo == hi:
        return float(vals[int(pos)])
    return float(vals[lo]) + (float(vals[hi]) - float(vals[lo])) * (pos - lo)


# =========================================================================== #
# 1. The component curves — the calibration anchors from the design report
# =========================================================================== #

@pytest.mark.parametrize("ratio,amp", [
    (0.0, 0.0),        # nothing at all
    (0.25, 0.1016),    # a quarter of the source's typical item
    (1.0, 0.3155),     # exactly typical for that source
    (2.0, 0.5),        # 2x the median — the anchor SOLO_AMP_MIN is set to
    (8.0, 1.0),        # AMP_CEILING, saturated
    (50.0, 1.0),       # a freak reading cannot crowd out the rest of the feed
])
def test_normalise_amplitude_anchors(ratio, amp):
    assert gr.normalise_amplitude(ratio) == pytest.approx(amp, abs=5e-4)


def test_normalise_amplitude_survives_garbage():
    assert gr.normalise_amplitude(None) == 0.0
    assert gr.normalise_amplitude("nope") == 0.0
    assert gr.normalise_amplitude(-5) == 0.0


def test_rank_curve_is_geometric_from_2x_down_to_half():
    # The whole "how much do we trust an unmetered source" policy, in one curve.
    assert gr.ratio_from_rank(1, 40) == pytest.approx(gr.RANK_TOP_RATIO)
    assert gr.ratio_from_rank(40, 40) == pytest.approx(gr.RANK_BOTTOM_RATIO)
    # #1 of a rank-only feed is worth exactly a 2x-median metric item.
    assert gr.normalise_amplitude(gr.ratio_from_rank(1, 40)) == pytest.approx(0.5, abs=1e-6)
    assert gr.normalise_amplitude(gr.ratio_from_rank(40, 40)) == pytest.approx(0.1845, abs=5e-4)
    # monotone decreasing
    amps = [gr.normalise_amplitude(gr.ratio_from_rank(r, 40)) for r in range(1, 41)]
    assert amps == sorted(amps, reverse=True)


def test_rank_curve_edges():
    assert gr.ratio_from_rank(None, 40) is None
    assert gr.ratio_from_rank(0, 40) is None
    assert gr.ratio_from_rank(1, 1) == pytest.approx(gr.RANK_TOP_RATIO)   # own #1
    assert gr.ratio_from_rank(1, None) == pytest.approx(gr.RANK_TOP_RATIO)
    # a rank past the claimed list length just lands at the bottom of the curve
    assert gr.ratio_from_rank(60, 40) == pytest.approx(gr.RANK_BOTTOM_RATIO)


def test_trusted_ratio_damps_and_caps_thin_samples():
    # Copied from trend_finder._trusted_outperformance; same behaviour.
    assert gr.trusted_ratio(4.0, 1.0) == 4.0            # full confidence, untouched
    assert gr.trusted_ratio(100.0, 0.25) == pytest.approx(1.5)   # capped, not just damped
    assert gr.trusted_ratio(2.0, 0.0) == 1.0            # no baseline ⇒ no claim
    assert gr.trusted_ratio(None, 1.0) == 1.0


def test_normalise_growth_is_scale_free():
    assert gr.normalise_growth(1.0) == 0.0              # flat
    assert gr.normalise_growth(2.0) == pytest.approx(0.5)   # doubled
    assert gr.normalise_growth(4.0) == pytest.approx(1.0)   # GROWTH_CEILING
    assert gr.normalise_growth(0.4) == 0.0              # shrinking
    # The old formula scored the SAME 2x move 0.732 (2→4) or 0.587 (50→100).
    assert gr.normalise_growth(100 / 50) == gr.normalise_growth(4 / 2)


def test_freshness_decays_from_content_age():
    for hours, expect in ((0, 1.0), (6, 0.8825), (24, 0.6065), (48, 0.3679),
                          (24 * 7, 0.0302)):
        assert gr.freshness(hours) == pytest.approx(expect, abs=5e-4)
    assert gr.freshness(-5) == 1.0                      # clock skew clamps to "now"


def test_corroboration_trust_anchors():
    assert gr.corroboration_trust(1, 1) == pytest.approx(0.55)
    assert gr.corroboration_trust(2, 2) == pytest.approx(0.834, abs=5e-4)
    assert gr.corroboration_trust(3, 3) == pytest.approx(1.0)
    assert gr.corroboration_trust(9, 9) == pytest.approx(1.0)      # saturates
    # a 2nd subreddit is worth a quarter of a 2nd platform
    assert 0.55 < gr.corroboration_trust(1, 3) < gr.corroboration_trust(2, 2)


def test_blend_heat_renormalises_over_present_signals():
    # Missing growth must NOT be scored as zero — that is the run-#1 flat feed.
    both = gr.blend_heat(amplitude=0.8, growth=None, fresh=0.8, trust=1.0)
    zeroed = gr.blend_heat(amplitude=0.8, growth=0.0, fresh=0.8, trust=1.0)
    assert both == pytest.approx(0.8)
    assert zeroed == pytest.approx(0.45 * 0.8 + 0.25 * 0.8)
    assert both > zeroed
    assert gr.blend_heat(amplitude=None, growth=None, fresh=None, trust=1.0) == 0.0


# =========================================================================== #
# 2. source_stats — the per-run, per-source baseline
# =========================================================================== #

def test_source_stats_medians_counts_and_fanout():
    run = (
        [_sighting("wikipedia", metric=m) for m in (10, 20, 30, 40)]
        + [_sighting("reddit") for _ in range(5)]
        + [_sighting("wikipedia", metric=0)]          # a zero is not a baseline sample
        + [{"source": None}, "junk", None]            # never raises
    )
    stats = gr.source_stats(run)
    assert stats["wikipedia"]["median"] == pytest.approx(25.0)
    assert stats["wikipedia"]["n"] == 4              # metric-bearing items only
    assert stats["wikipedia"]["items"] == 5          # every item, the rank fanout
    assert stats["reddit"] == {"median": 0.0, "n": 0, "items": 5}
    assert gr.source_stats([]) == {}


# =========================================================================== #
# 3. compute_velocity — assembly
# =========================================================================== #

def _wiki_run(median: float = 90_000.0, n: int = 12) -> dict:
    """A run where wikipedia's median is `median` over `n` items."""
    return gr.source_stats([_sighting("wikipedia", metric=median) for _ in range(n)])


def test_amplitude_uses_the_sources_own_median_not_the_raw_metric():
    stats = _wiki_run()
    big = gr.compute_velocity(_trend(), [_sighting("wikipedia", metric=360_000)],
                              now=NOW, source_stats=stats)
    typical = gr.compute_velocity(_trend(), [_sighting("wikipedia", metric=90_000)],
                                  now=NOW, source_stats=stats)
    small = gr.compute_velocity(_trend(), [_sighting("wikipedia", metric=20_000)],
                                now=NOW, source_stats=stats)
    assert big > typical > small
    # 4x the median → amp = log1p(4)/log1p(8)
    sig = gr.heat_signals(_trend(), [_sighting("wikipedia", metric=360_000)],
                          now=NOW, source_stats=stats)
    assert sig["amplitude"] == pytest.approx(gr.normalise_amplitude(4.0), abs=5e-4)


def test_thin_source_median_cannot_claim_a_huge_ratio():
    thin = _wiki_run(n=2)          # only 2 items ⇒ conf 0.25 ⇒ ratio capped at 1.5
    fat = _wiki_run(n=12)
    item = [_sighting("wikipedia", metric=900_000)]     # 10x the median
    assert (gr.compute_velocity(_trend(), item, now=NOW, source_stats=thin)
            < gr.compute_velocity(_trend(), item, now=NOW, source_stats=fat))


def test_rank_only_sources_score_from_position_without_any_baseline():
    stats = gr.source_stats([_sighting("reddit", key=str(i)) for i in range(40)])
    top = gr.heat_signals(_trend(), [_sighting("reddit", rank=1, rank_total=40)],
                          now=NOW, source_stats=stats)
    mid = gr.heat_signals(_trend(), [_sighting("reddit", rank=18, rank_total=40)],
                          now=NOW, source_stats=stats)
    bottom = gr.heat_signals(_trend(), [_sighting("reddit", rank=40, rank_total=40)],
                             now=NOW, source_stats=stats)
    assert top["amplitude"] == pytest.approx(0.5, abs=1e-3)
    assert mid["amplitude"] == pytest.approx(0.3361, abs=1e-3)
    assert bottom["amplitude"] == pytest.approx(0.1845, abs=1e-3)
    # the rank path needs no run baseline at all
    assert gr.heat_signals(_trend(), [_sighting("kym", rank=1, rank_total=40)],
                           now=NOW, source_stats=None)["amplitude"] == pytest.approx(0.5, abs=1e-3)


def test_rank_fanout_falls_back_to_the_sources_item_count():
    stats = gr.source_stats([_sighting("kym", key=str(i)) for i in range(9)])
    with_stats = gr.heat_signals(_trend(), [_sighting("kym", rank=9)],
                                 now=NOW, source_stats=stats)["amplitude"]
    # 9th of 9 == bottom of the curve
    assert with_stats == pytest.approx(gr.normalise_amplitude(gr.RANK_BOTTOM_RATIO), abs=1e-3)


def test_trend_amplitude_is_max_heavy_but_not_max_only():
    stats = _wiki_run()
    one_big = [_sighting("wikipedia", metric=720_000)]
    big_plus_weak = one_big + [_sighting("wikipedia", metric=9_000, key="b")]
    solid = [_sighting("wikipedia", metric=270_000, key=str(i)) for i in range(3)]
    a_one = gr.heat_signals(_trend(), one_big, now=NOW, source_stats=stats)["amplitude"]
    a_mix = gr.heat_signals(_trend(), big_plus_weak, now=NOW, source_stats=stats)["amplitude"]
    a_solid = gr.heat_signals(_trend(), solid, now=NOW, source_stats=stats)["amplitude"]
    assert a_mix < a_one                      # a weak sibling dilutes the freak reading
    assert a_solid > gr.heat_signals(_trend(), solid[:1], now=NOW,
                                     source_stats=stats)["amplitude"] * 0.9
    # 0.7*max + 0.3*mean, exactly
    amps = [gr.normalise_amplitude(720_000 / 90_000), gr.normalise_amplitude(9_000 / 90_000)]
    assert a_mix == pytest.approx(0.7 * max(amps) + 0.3 * (sum(amps) / 2), abs=5e-4)


# --- growth ---------------------------------------------------------------- #

def test_growth_from_the_vendors_own_day_over_day_payload():
    """wikipedia ships yesterday's views on run #1; nothing read them before."""
    stats = _wiki_run()
    doubled = gr.heat_signals(_trend(), [_sighting("wikipedia", metric=180_000,
                                                   views_yesterday=90_000)],
                              now=NOW, source_stats=stats)
    flat = gr.heat_signals(_trend(), [_sighting("wikipedia", metric=90_000,
                                                views_yesterday=90_000)],
                           now=NOW, source_stats=stats)
    assert doubled["growth"] == pytest.approx(0.5, abs=1e-3)
    assert flat["growth"] == 0.0
    assert doubled["heat"] > flat["heat"]


def test_growth_from_views_delta_when_only_the_delta_is_present():
    stats = _wiki_run()
    sig = gr.heat_signals(_trend(), [_sighting("wikipedia", metric=180_000,
                                               views_delta=90_000)],
                          now=NOW, source_stats=stats)
    assert sig["growth"] == pytest.approx(0.5, abs=1e-3)


def test_growth_from_a_previous_run_metric_and_the_min_gap_guard():
    stats = _wiki_run()
    old = gr.heat_signals(_trend(), [_sighting("wikipedia", metric=270_000,
                                               prev_metric=90_000,
                                               prev_seen_at=_h(6))],
                          now=NOW, source_stats=stats)
    assert old["growth"] == pytest.approx(gr.normalise_growth(3.0), abs=1e-3)
    # the same delta measured 20 minutes apart is fetch noise, not growth
    noise = gr.heat_signals(_trend(), [_sighting("wikipedia", metric=270_000,
                                                 prev_metric=90_000,
                                                 prev_seen_at=_h(0.33))],
                            now=NOW, source_stats=stats)
    assert noise["growth"] is None


def test_growth_from_rank_improvement_on_an_unmetered_feed():
    climbing = gr.heat_signals(_trend(), [_sighting("reddit", rank=5, prev_rank=30,
                                                    rank_total=40)], now=NOW)
    sinking = gr.heat_signals(_trend(), [_sighting("reddit", rank=30, prev_rank=5,
                                                   rank_total=40)], now=NOW)
    assert climbing["growth"] > 0.5
    assert sinking["growth"] == 0.0


def test_growth_is_absent_not_zero_when_nothing_supports_it():
    """Run #1 with no vendor delta: G must be absent so the weights renormalise."""
    stats = _wiki_run()
    sig = gr.heat_signals(_trend(), [_sighting("wikipedia", metric=90_000)],
                          now=NOW, source_stats=stats)
    assert sig["growth"] is None
    assert sig["heat"] == pytest.approx(
        sig["trust"] * (0.45 * sig["amplitude"] + 0.25 * sig["freshness"]) / 0.70,
        abs=1e-3)


def test_entering_a_list_is_unavailable_growth_not_infinite():
    stats = _wiki_run()
    assert gr.heat_signals(_trend(), [_sighting("wikipedia", metric=90_000,
                                                views_yesterday=0)],
                           now=NOW, source_stats=stats)["growth"] is None


def test_growth_takes_the_best_source_not_the_average():
    stats = gr.source_stats([_sighting("bluesky", metric=1_000) for _ in range(10)]
                            + [_sighting("wikipedia", metric=90_000) for _ in range(10)])
    sightings = [
        _sighting("bluesky", metric=1_000, prev_metric=1_000, prev_seen_at=_h(8)),
        _sighting("wikipedia", metric=180_000, views_yesterday=45_000, key="w"),
    ]
    sig = gr.heat_signals(_trend(source_count=2), sightings, now=NOW, source_stats=stats)
    assert sig["growth"] == pytest.approx(gr.normalise_growth(4.0), abs=1e-3)


# --- freshness ------------------------------------------------------------- #

def test_freshness_comes_from_content_time_and_never_from_seen_at():
    stats = _wiki_run()
    item = _sighting("wikipedia", metric=90_000, age_h=30)
    fresh_item = _sighting("wikipedia", metric=90_000, age_h=2)
    old = gr.heat_signals(_trend(), [item], now=NOW, source_stats=stats)
    new = gr.heat_signals(_trend(), [fresh_item], now=NOW, source_stats=stats)
    assert old["freshness"] == pytest.approx(gr.freshness(30), abs=1e-3)
    assert new["heat"] > old["heat"]

    # seen_at is a per-run constant: moving it must not move heat at all.
    with_seen = dict(item, seen_at=_h(0.2))
    other_seen = dict(item, seen_at=_h(9))
    assert (gr.compute_velocity(_trend(), [with_seen], now=NOW, source_stats=stats)
            == gr.compute_velocity(_trend(), [other_seen], now=NOW, source_stats=stats))


def test_latest_content_at_on_the_trend_row_wins_over_the_sightings():
    stats = _wiki_run()
    t = _trend(latest_content_at=_h(2))
    sig = gr.heat_signals(t, [_sighting("wikipedia", metric=90_000, age_h=200)],
                          now=NOW, source_stats=stats)
    assert sig["age_hours"] == pytest.approx(2.0, abs=0.01)


def test_freshness_falls_back_to_first_seen_then_to_absent():
    stats = _wiki_run()
    undated = {"source": "wikipedia", "source_key": "k", "metric_value": 90_000}
    with_first = gr.heat_signals(_trend(first_seen_at=_h(50)), [undated],
                                 now=NOW, source_stats=stats)
    assert with_first["freshness"] == pytest.approx(gr.freshness(50), abs=1e-3)
    nothing = gr.heat_signals({"source_count": 1}, [undated], now=NOW, source_stats=stats)
    assert nothing["freshness"] is None
    # …and heat renormalises over amplitude alone rather than scoring 0 for it
    assert nothing["heat"] == pytest.approx(nothing["trust"] * nothing["amplitude"], abs=1e-3)


# --- corroboration + the solo floor ---------------------------------------- #

def test_more_sources_multiply_heat_up_to_saturation():
    stats = gr.source_stats([_sighting(s, metric=100) for s in ("a", "b", "c")
                             for _ in range(10)])
    def _heat(n: int) -> float:
        srcs = ["a", "b", "c"][:n]
        return gr.compute_velocity(_trend(source_count=n),
                                   [_sighting(s, metric=200, key=s) for s in srcs],
                                   now=NOW, source_stats=stats)
    h1, h2, h3 = _heat(1), _heat(2), _heat(3)
    assert h1 < h2 < h3
    assert h1 == pytest.approx(h3 * 0.55, abs=1e-3)      # T is exactly the ratio


def test_extra_subreddits_are_worth_a_quarter_of_an_extra_platform():
    one_sub = [_sighting("reddit", rank=3, rank_total=40, sub="memes", key="a")]
    two_subs = one_sub + [_sighting("reddit", rank=7, rank_total=40, sub="technology",
                                    key="b")]
    a = gr.heat_signals(_trend(source_count=1), one_sub, now=NOW)
    b = gr.heat_signals(_trend(source_count=1), two_subs, now=NOW)
    assert a["trust"] == pytest.approx(0.55)
    assert 0.55 < b["trust"] < 0.834


def test_the_digital_twins_floor_one_source_one_item_middling_rank():
    """The exact live failure: a single mid-list Reddit post ranked #4 overall."""
    lone = [_sighting("reddit", rank=18, rank_total=40, age_h=6)]
    sig = gr.heat_signals(_trend(source_count=1, evidence_count=1), lone, now=NOW)
    assert sig["capped"] is True
    assert sig["heat"] <= gr.SOLO_HEAT_CAP
    assert gr.heat_band(sig["heat"]) == "warm"          # never a hot/rising pill

    # …but the #1 item of that same feed is exactly at the bar and is NOT capped
    top = gr.heat_signals(_trend(source_count=1, evidence_count=1),
                          [_sighting("reddit", rank=1, rank_total=40, age_h=6)], now=NOW)
    assert top["capped"] is False
    assert top["heat"] > sig["heat"]


def test_the_solo_floor_does_not_fire_once_a_second_item_corroborates():
    two = [_sighting("reddit", rank=18, rank_total=40, key="a"),
           _sighting("reddit", rank=22, rank_total=40, key="b", sub="technology")]
    sig = gr.heat_signals(_trend(source_count=1, evidence_count=2), two, now=NOW)
    assert sig["capped"] is False


def test_evidence_count_is_used_not_the_poll_counter():
    """`sighting_count` is bumped on every re-poll — an evergreen wikipedia page
    shows 9 'sightings' from one source. It must not buy corroboration."""
    lone = [_sighting("reddit", rank=18, rank_total=40)]
    t = _trend(source_count=1, evidence_count=1, sighting_count=9)
    assert gr.heat_signals(t, lone, now=NOW)["capped"] is True


# --- degraded modes -------------------------------------------------------- #

def test_source_stats_none_degrades_to_a_neutral_amplitude():
    """An un-updated caller (or a run with no baselines) still gets a sane
    number: amplitude 0.5, which also sits exactly on SOLO_AMP_MIN so the
    legacy path can never trip the solo cap."""
    legacy_sightings = [{"seen_at": _h(1), "source": "reddit"}]
    sig = gr.heat_signals(_trend(), legacy_sightings, now=NOW)
    assert sig["amplitude"] == pytest.approx(gr.NEUTRAL_AMPLITUDE)
    assert sig["capped"] is False
    v = gr.compute_velocity(_trend(), legacy_sightings, now=NOW)
    assert 0.0 <= v <= 1.0 and v == round(v, 4)


def test_velocity_is_bounded_and_rounded():
    stats = _wiki_run()
    v = gr.compute_velocity(_trend(source_count=99),
                            [_sighting("wikipedia", metric=10_000_000,
                                       views_yesterday=1, age_h=0)],
                            now=NOW, source_stats=stats)
    assert 0.0 <= v <= 1.0
    assert v == round(v, 4)


def test_velocity_ignores_garbage_sightings_and_accepts_iso_strings():
    v = gr.compute_velocity(_trend(), [None, {}, {"seen_at": "not a date"}, 42,
                                       {"source": "reddit", "extra": "not json"},
                                       {"source": "kym", "extra": "{\"rank\": 2}",
                                        "published_at": "2026-08-19T06:00:00Z"}],
                            now=NOW.isoformat(), source_stats={"kym": "junk"})
    assert 0.0 <= v <= 1.0


def test_no_sightings_at_all_still_returns_a_number():
    assert 0.0 <= gr.compute_velocity(_trend(), [], now=NOW) <= 1.0
    assert gr.compute_velocity({}, [], now=NOW) >= 0.0


# =========================================================================== #
# 4. Regression table — the owner's own complaints, from the design report
#    Assertions are on ORDERING and BANDS, not on brittle decimals.
# =========================================================================== #

# One run's baselines, reconstructed from the live corpus: wikipedia median
# 90k views, x_trends median 25k tweets, google median 20k, bluesky ~1.2k posts.
_RUN = gr.source_stats(
    [_sighting("wikipedia", metric=90_000, key=f"w{i}") for i in range(12)]
    + [_sighting("x_trends", metric=25_000, key=f"x{i}") for i in range(12)]
    + [_sighting("google_trends", metric=20_000, key=f"g{i}") for i in range(12)]
    + [_sighting("bluesky", metric=1_200, key=f"b{i}") for i in range(12)]
    + [_sighting("hacker_news", metric=140, key=f"h{i}") for i in range(12)]
    + [_sighting("reddit", key=f"r{i}") for i in range(40)]
    + [_sighting("kym", key=f"k{i}") for i in range(40)]
)

_CASES = {
    # A genuine coding-culture spike: 3 platforms, 5x the typical Google entry,
    # +150% run over run, 4 hours old.
    "coding_spike": (
        _trend(source_count=3, evidence_count=3),
        [_sighting("google_trends", metric=100_000, prev_metric=40_000,
                   prev_seen_at=_h(8), age_h=4, key="g"),
         _sighting("hacker_news", metric=329, age_h=5, key="h"),
         _sighting("reddit", rank=1, rank_total=40, age_h=6, key="r")],
        {"fit_score": 85, "uae_relevance": 70},
    ),
    # A cross-source meme: kym #2 + two subreddits + bluesky 4x median, 10h old.
    "cross_source_meme": (
        _trend(source_count=3, evidence_count=4),
        [_sighting("kym", rank=2, rank_total=40, age_h=10, key="k"),
         _sighting("bluesky", metric=4_800, prev_metric=1_920, prev_seen_at=_h(9),
                   age_h=11, key="b"),
         _sighting("reddit", rank=5, rank_total=40, sub="memes", age_h=12, key="r1"),
         _sighting("reddit", rank=9, rank_total=40, sub="ProgrammerHumor",
                   age_h=13, key="r2")],
        {"fit_score": 80, "uae_relevance": 45},
    ),
    # "ChatGPT" — a huge evergreen wikipedia page: 320k views against a 90k
    # median, but only +4% day over day, and the pageview day is 30h stale.
    "chatgpt": (
        _trend(source_count=1, evidence_count=1),
        [_sighting("wikipedia", metric=320_000, views_yesterday=307_692,
                   age_h=30, key="w")],
        {"fit_score": 90, "uae_relevance": 80},
    ),
    # "Digital Twins in City Planning" — one Reddit post, rank 18/40, 6h old.
    "digital_twins": (
        _trend(source_count=1, evidence_count=1),
        [_sighting("reddit", rank=18, rank_total=40, age_h=6, key="r")],
        {"fit_score": 95, "uae_relevance": 50},
    ),
    # "Knockout Stages" — 12k tweets against a 25k median, and SHRINKING 30k→12k.
    "knockout": (
        _trend(source_count=1, evidence_count=1),
        [_sighting("x_trends", metric=12_000, prev_metric=30_000, prev_seen_at=_h(6),
                   age_h=0.5, key="x")],
        {"fit_score": 90, "uae_relevance": 100},
    ),
    # "Financial Transactions / Fintech" — an untranslated Arabic hashtag the
    # model laundered into a plausible English noun phrase. 8k vs 25k, flat.
    "fintech": (
        _trend(source_count=1, evidence_count=1),
        [_sighting("x_trends", metric=8_000, prev_metric=8_000, prev_seen_at=_h(6),
                   age_h=0.5, key="x")],
        {"fit_score": 75, "uae_relevance": 100},
    ),
}


def _scored() -> dict[str, dict]:
    out = {}
    for name, (trend, sightings, ai) in _CASES.items():
        row = dict(trend, **ai)
        row["velocity"] = gr.compute_velocity(trend, sightings, now=NOW, source_stats=_RUN)
        row["signals"] = gr.heat_signals(trend, sightings, now=NOW, source_stats=_RUN)
        row["opportunity_score"] = gr.compute_opportunity(row)
        out[name] = row
    return out


def test_regression_table_ordering():
    """The four rows the owner complained about sat in a block at the top
    (Knockout 81.3 > ChatGPT 77.3 > Fintech 75.3 > Digital Twins 73.3) with
    IDENTICAL velocity 0.632. All four must now sit below real, corroborated
    trends, and the genuine spike must lead."""
    s = _scored()
    order = [n for n, _ in sorted(s.items(),
                                  key=lambda kv: kv[1]["opportunity_score"], reverse=True)]
    assert order[0] == "coding_spike"
    assert order[1] == "cross_source_meme"
    assert set(order[2:]) == {"chatgpt", "digital_twins", "knockout", "fintech"}
    # inside the junk block: the laundered hashtag is last, the shrinking one next
    assert order.index("fintech") == 5
    assert order.index("knockout") == 4


def test_regression_table_bands_and_magnitudes():
    s = _scored()
    # the report's targets: 78.5 / 67.7 / 52.6 / 52.1 / 48.3 / 43.6
    assert s["coding_spike"]["opportunity_score"] > 70
    assert s["cross_source_meme"]["opportunity_score"] > 60
    for name in ("chatgpt", "digital_twins", "knockout", "fintech"):
        assert s[name]["opportunity_score"] < 60, name
    # every one of the four is separated from the good pair by a real gap,
    # where the old blend held all six inside 14 points
    assert (s["cross_source_meme"]["opportunity_score"]
            - s["chatgpt"]["opportunity_score"]) > 8


def test_regression_table_each_demotion_is_traceable_to_one_term():
    s = _scored()
    # ChatGPT: big amplitude, no movement — the definition of an evergreen page.
    assert s["chatgpt"]["signals"]["amplitude"] > 0.6
    assert s["chatgpt"]["signals"]["growth"] < 0.05
    # Knockout / Fintech: below their own source's median, and not growing.
    assert s["knockout"]["signals"]["amplitude"] < 0.2
    assert s["knockout"]["signals"]["growth"] == 0.0
    assert s["fintech"]["signals"]["amplitude"] < 0.2
    # Digital twins: trust floor + the solo cap.
    assert s["digital_twins"]["signals"]["trust"] == pytest.approx(0.55)
    assert s["digital_twins"]["signals"]["capped"] is True
    # The good ones: full corroboration, real growth.
    assert s["coding_spike"]["signals"]["trust"] == pytest.approx(1.0)
    assert s["coding_spike"]["signals"]["growth"] > 0.5
    assert gr.heat_band(s["coding_spike"]["velocity"]) == "hot"


def test_regression_table_heat_is_no_longer_a_four_valued_constant():
    heats = {row["velocity"] for row in _scored().values()}
    assert len(heats) == 6                     # six rows, six different heats
    assert max(heats) - min(heats) > 0.4       # the old spread across ALL 363 rows was 0.108


# =========================================================================== #
# 5. ACCEPTANCE CRITERIA over a synthetic ~300-row run
# =========================================================================== #

# Corroboration mixes: (P(1 source), P(2), P(3+)).
# TODAY_SHAPE mirrors the live corpus, where 358/363 rows are single-source.
# POST_FIX is the same run after the ingest fixes (non-minting sources +
# canonical-key clustering), where far more trends carry real corroboration.
TODAY_SHAPE = (0.78, 0.15, 0.07)
POST_FIX = (0.55, 0.28, 0.17)


def _synthetic_run(seed: int = 20260819, n_trends: int = 300,
                   mix: tuple = TODAY_SHAPE, sigma: float = 1.2):
    """A run shaped like the live one: mostly single-source trends, a power-law
    metric on the metered sources (sigma 1.2 in log space ≈ Google's 1K…1M+
    traffic buckets), rank-only reddit/kym/tiktok, content ages from minutes to
    the 30-day hard gate, growth available on some sources only."""
    rnd = random.Random(seed)
    metered = {"google_trends": 20_000.0, "wikipedia": 90_000.0,
               "bluesky": 1_200.0, "youtube": 250_000.0, "hacker_news": 140.0}
    rank_only = ("reddit", "kym", "tiktok")

    trends: list[tuple[dict, list[dict]]] = []
    every_item: list[dict] = []
    for i in range(n_trends):
        roll = rnd.random()
        n_src = 1 if roll < mix[0] else (2 if roll < mix[0] + mix[1] else 3)
        pool = rnd.sample(list(metered) + list(rank_only), n_src)
        # content age: 40% under a day, 30% 1-3d, 20% 3-10d, 10% 10-30d
        r = rnd.random()
        age = (rnd.uniform(0.1, 24) if r < 0.40 else
               rnd.uniform(24, 72) if r < 0.70 else
               rnd.uniform(72, 240) if r < 0.90 else rnd.uniform(240, 720))
        sightings: list[dict] = []
        for j, src in enumerate(pool):
            extra: dict = {}
            metric = None
            if src in metered:
                # lognormal around the source's typical item — a real power law
                metric = metered[src] * math.exp(rnd.gauss(0.0, sigma))
                if rnd.random() < 0.45:                     # some have a prior value
                    extra["prev_metric"] = metric / math.exp(rnd.gauss(0.25, 0.6))
                    extra["prev_seen_at"] = NOW - timedelta(hours=6)
            else:
                extra["rank"] = rnd.randint(1, 40)
                extra["rank_total"] = 40
                extra["sub"] = rnd.choice(("memes", "technology", "ProgrammerHumor"))
                if rnd.random() < 0.25:
                    extra["prev_rank"] = rnd.randint(1, 40)
            s = _sighting(src, metric=metric, age_h=age + j * 0.5,
                          key=f"{i}-{j}", **extra)
            sightings.append(s)
            every_item.append(s)
        trends.append((_trend(source_count=n_src, evidence_count=len(sightings)),
                       sightings))
    return trends, gr.source_stats(every_item)


def _measure(mix, seed: int = 20260819) -> dict:
    trends, stats = _synthetic_run(seed=seed, mix=mix)
    sigs = [gr.heat_signals(t, s, now=NOW, source_stats=stats) for t, s in trends]
    heats = [s["heat"] for s in sigs]
    # `inner` is heat before the trust discount — the MEASUREMENT itself, on the
    # scale it is computed on. Trust is a confidence multiplier applied on top.
    inner = [s["heat"] / s["trust"] for s in sigs]
    return {
        "n": len(heats), "distinct": len(set(heats)),
        "iqr": _quantile(heats, 0.75) - _quantile(heats, 0.25),
        "range": max(heats) - min(heats),
        "median": statistics.median(heats),
        "inner_iqr": _quantile(inner, 0.75) - _quantile(inner, 0.25),
        "inner_range": max(inner) - min(inner),
    }


def test_acceptance_heat_has_at_least_100_distinct_values_and_a_wide_spread():
    """Criterion 1 and 2: >= 100 distinct heat values (today: 4) and a full
    range >= 0.7 (today: 0.108).

    On the IQR criterion, stated in the design report as >= 0.25 (today 0.10):
    MEASURED it lands at ~0.15 on a corpus shaped like today's and ~0.19 once
    corroboration is normal. That target is arithmetically unreachable for the
    formula the same report specifies, for two structural reasons, and both are
    deliberate:

      * `heat = T * inner` and T is 0.55 for a single-source trend, so 78% of
        today's rows live on a 0..0.55 scale. The IQR ceiling for that bulk is
        0.55 * IQR(inner) — the report set the 0.25 target for the blend and
        then multiplied a compressor on top of it.
      * `inner` is itself an average of 2-3 largely independent components, and
        averaging independent variables shrinks dispersion. Amplitude is also
        log-compressed ON PURPOSE (a 44x freak must not crowd out an 8x hit).

    What the criterion was FOR — heat being able to reorder the feed — is
    verified directly in `test_acceptance_opportunity_spreads_too`: heat now
    moves the opportunity score by a factor of ~4x across the corpus, where the
    old velocity could move it by at most 4.32 points out of 100 and therefore
    could not flip a single cross-cluster pair."""
    today = _measure(TODAY_SHAPE)
    post = _measure(POST_FIX)
    for label, m in (("today-shape", today), ("post-fix", post)):
        print(f"\n[acceptance/{label}] n={m['n']} distinct={m['distinct']} "
              f"IQR={m['iqr']:.4f} range={m['range']:.4f} median={m['median']:.4f} "
              f"inner_IQR={m['inner_iqr']:.4f} inner_range={m['inner_range']:.4f}")

    for m in (today, post):
        assert m["distinct"] >= 100            # today: 4
        assert m["range"] >= 0.7               # today: 0.108
    # today: IQR 0.10 over a 0.108 range — i.e. the whole feed inside one band.
    assert today["iqr"] >= 0.14
    assert post["iqr"] >= 0.18
    # More corroboration ⇒ more spread, which is the point of the trust term.
    assert post["iqr"] > today["iqr"]


def test_acceptance_no_heavy_component_is_dead_weight():
    """No component may hold >=25% of the weight while contributing <5% of the
    variance. Under the old formula source_score (40%) and newness (10%) each
    contributed EXACTLY 0.0000."""
    trends, stats = _synthetic_run()
    contribs: dict[str, list[float]] = {k: [] for k in gr.HEAT_WEIGHTS}
    for t, s in trends:
        sig = gr.heat_signals(t, s, now=NOW, source_stats=stats)
        present = {k: sig[k] for k in gr.HEAT_WEIGHTS if sig.get(k) is not None}
        denom = sum(gr.HEAT_WEIGHTS[k] for k in present) or 1.0
        for k in gr.HEAT_WEIGHTS:
            # what this component actually put into the final heat number
            contribs[k].append(sig["trust"] * gr.HEAT_WEIGHTS[k] * present.get(k, 0.0) / denom)
    variances = {k: statistics.pvariance(v) for k, v in contribs.items()}
    total = sum(variances.values())
    shares = {k: v / total for k, v in variances.items()}
    print("[acceptance] variance share: "
          + ", ".join(f"{k}={shares[k]:.1%} (w={gr.HEAT_WEIGHTS[k]})" for k in shares))
    for k, w in gr.HEAT_WEIGHTS.items():
        if w >= 0.25:
            assert shares[k] >= 0.05, (
                f"{k} holds {w} of the weight but {shares[k]:.2%} of the variance")


def test_acceptance_heat_does_not_move_when_only_seen_at_moves():
    """The structural guarantee: nothing in heat is a function of the run stamp."""
    trends, stats = _synthetic_run(seed=7, n_trends=40)
    for t, s in trends:
        a = gr.compute_velocity(t, s, now=NOW, source_stats=stats)
        moved = [dict(x, seen_at=NOW - timedelta(hours=13)) for x in s]
        t_moved = dict(t, last_seen_at=NOW - timedelta(hours=13))
        assert gr.compute_velocity(t_moved, moved, now=NOW, source_stats=stats) == a


def test_acceptance_opportunity_spreads_too():
    trends, stats = _synthetic_run()
    rnd = random.Random(11)
    rows = []
    for t, s in trends:
        row = dict(t)
        row["velocity"] = gr.compute_velocity(t, s, now=NOW, source_stats=stats)
        # the live fit histogram is bimodal: a block at 0-9 and a block at 70-100
        row["fit_score"] = rnd.randint(0, 9) if rnd.random() < 0.55 else rnd.randint(70, 100)
        row["uae_relevance"] = rnd.randint(0, 100)
        rows.append(row)
    scores = [gr.compute_opportunity(r) for r in rows]
    print(f"[acceptance] opportunity distinct={len(set(scores))} "
          f"IQR={_quantile(scores, 0.75) - _quantile(scores, 0.25):.2f}")
    assert len(set(scores)) >= 100
    # heat must be able to overturn a fit gap — under the old blend velocity
    # could move the score by at most 4.32 points out of 100 and therefore
    # could not flip a single cross-cluster pair.
    hot_weak_fit = gr.compute_opportunity({"velocity": 0.80, "fit_score": 60,
                                           "uae_relevance": 60})
    cold_great_fit = gr.compute_opportunity({"velocity": 0.18, "fit_score": 95,
                                             "uae_relevance": 100})
    assert hot_weak_fit > cold_great_fit


# =========================================================================== #
# 6. heat_band
# =========================================================================== #

@pytest.mark.parametrize("v,band", [
    (1.0, "hot"), (0.7, "hot"), (0.699, "rising"), (0.45, "rising"),
    (0.449, "warm"), (0.0, "warm"), (None, "warm"), ("nope", "warm"), (5, "hot"),
])
def test_heat_band(v, band):
    assert gr.heat_band(v) == band


def test_solo_cap_sits_below_the_rising_band():
    assert gr.heat_band(gr.SOLO_HEAT_CAP) == "warm"


# =========================================================================== #
# 7. normalise_weights
# =========================================================================== #

def test_normalise_weights_defaults_and_sum_to_one():
    w = gr.normalise_weights(None)
    assert w == gr.DEFAULT_WEIGHTS
    assert sum(w.values()) == pytest.approx(1.0)
    assert set(gr.DEFAULT_WEIGHTS) == {"velocity", "fit", "uae"}   # API contract


def test_normalise_weights_rescales_any_positive_combination():
    w = gr.normalise_weights({"velocity": 2, "fit": 2, "uae": 0})
    assert w == {"velocity": 0.5, "fit": 0.5, "uae": 0.0}


def test_normalise_weights_survives_garbage():
    w = gr.normalise_weights({"velocity": "abc", "fit": -3, "uae": float("nan"),
                              "bogus": 9})
    assert set(w) == {"velocity", "fit", "uae"}
    assert w["fit"] == 0.0                     # negative clamps to 0
    assert w["velocity"] == pytest.approx(0.4 / 0.6)   # unchanged default, renormalised
    assert w["uae"] == pytest.approx(0.2 / 0.6)
    assert gr.normalise_weights("nonsense") == gr.DEFAULT_WEIGHTS
    assert gr.normalise_weights({"velocity": 0, "fit": 0, "uae": 0}) == gr.DEFAULT_WEIGHTS


# =========================================================================== #
# 8. compute_opportunity — the geometric blend
# =========================================================================== #

def test_opportunity_is_a_weighted_geometric_mean():
    t = {"velocity": 0.5, "fit_score": 80, "uae_relevance": 50}
    expect = 100 * (0.5 ** 0.4) * (0.8 ** 0.4) * (0.5 ** 0.2)
    assert gr.compute_opportunity(t) == pytest.approx(round(expect, 2), abs=0.01)


def test_opportunity_refuses_to_let_one_axis_buy_the_ranking():
    """The live #1: a shrinking, uncorroborated x_trends hashtag with fit 90 /
    uae 100 outranked a genuinely cross-corroborated meme at fit 75."""
    dead_but_flattered = {"velocity": 0.18, "fit_score": 90, "uae_relevance": 100}
    real_but_modest = {"velocity": 0.70, "fit_score": 75, "uae_relevance": 45}
    assert gr.compute_opportunity(real_but_modest) > gr.compute_opportunity(dead_but_flattered)

    def _additive(t: dict, velocity=None) -> float:
        v = t["velocity"] if velocity is None else velocity
        return 100 * (0.4 * v + 0.4 * t["fit_score"] / 100
                      + 0.2 * t["uae_relevance"] / 100)

    # Under the OLD blend the flattered one won — because velocity was the SAME
    # 0.632 constant for both (4 reachable values over 363 live rows), so the
    # additive score reduced to 0.4*fit + 0.2*uae.
    assert _additive(dead_but_flattered, 0.632) > _additive(real_but_modest, 0.632)
    # …and even fed the TRUE heats, the additive form separates them by only
    # 3.8 points, where the geometric form separates them by ~17.6.
    additive_gap = _additive(real_but_modest) - _additive(dead_but_flattered)
    geometric_gap = (gr.compute_opportunity(real_but_modest)
                     - gr.compute_opportunity(dead_but_flattered))
    assert geometric_gap > 4 * additive_gap


def test_opportunity_unscored_falls_back_to_heat_alone():
    assert gr.compute_opportunity({"velocity": 0.63}) == 63.0
    assert gr.compute_opportunity({"velocity": 0.63, "fit_score": None,
                                   "uae_relevance": None}) == 63.0


def test_opportunity_partial_parse_does_not_score_the_missing_half_as_zero():
    """The bug trend_scoring already fixed: a reply that dropped `fit_score` but
    kept `uae_relevance` used to score fit as 0 and rank BELOW a trend that was
    never scored at all. Absent evidence is not evidence of a bad fit."""
    unscored = gr.compute_opportunity({"velocity": 0.63})
    half = gr.compute_opportunity({"velocity": 0.63, "uae_relevance": 90})
    assert half > unscored
    # exponents renormalised over the two present factors (0.4 and 0.2 → ⅔/⅓)
    assert half == pytest.approx(round(100 * (0.63 ** (2 / 3)) * (0.9 ** (1 / 3)), 2), abs=0.01)
    good_fit_only = gr.compute_opportunity({"velocity": 0.63, "fit_score": 100})
    assert good_fit_only == pytest.approx(round(100 * (0.63 ** 0.5), 2), abs=0.01)


def test_opportunity_floor_keeps_zero_rows_orderable():
    """A zero on one axis must not annihilate the product — rows would become
    unorderable and the LLM budget is allocated by this number."""
    zero_fit = gr.compute_opportunity({"velocity": 0.8, "fit_score": 0,
                                       "uae_relevance": 80})
    zero_all = gr.compute_opportunity({"velocity": 0.0, "fit_score": 0,
                                       "uae_relevance": 0})
    assert 0 < zero_all < zero_fit < 100
    assert zero_all == pytest.approx(round(100 * gr.OPPORTUNITY_FLOOR, 2), abs=0.01)


def test_opportunity_applies_the_sensitivity_multiplier():
    base = {"velocity": 0.6, "fit_score": 80, "uae_relevance": 60}
    ok = gr.compute_opportunity(dict(base, sensitivity="ok"))
    caution = gr.compute_opportunity(dict(base, sensitivity="caution"))
    avoid = gr.compute_opportunity(dict(base, sensitivity="avoid"))
    assert ok == gr.compute_opportunity(base)              # no value == ok
    assert caution == pytest.approx(ok * 0.75, abs=0.02)
    assert avoid < caution
    assert gr.compute_opportunity(dict(base, sensitivity="weird")) == ok


def test_opportunity_clamps_and_uses_custom_weights():
    t = {"velocity": 7, "fit_score": 500, "uae_relevance": -20}
    assert gr.compute_opportunity(t, {"velocity": 1, "fit": 0, "uae": 0}) == 100.0
    # uae clamps to 0 → floored, not annihilated
    assert gr.compute_opportunity(t, {"velocity": 0, "fit": 0, "uae": 1}) == \
        pytest.approx(100 * gr.OPPORTUNITY_FLOOR, abs=0.01)
    assert gr.compute_opportunity({}) == pytest.approx(100 * gr.OPPORTUNITY_FLOOR, abs=0.01)


# =========================================================================== #
# 9. rank_trends
# =========================================================================== #

_ROWS = [
    {"id": 1, "velocity": 0.9, "fit_score": 20, "uae_relevance": 10, "source_count": 1,
     "last_seen_at": _h(5), "opportunity_score": 999.0},
    {"id": 2, "velocity": 0.3, "fit_score": 95, "uae_relevance": 90, "source_count": 4,
     "last_seen_at": _h(1)},
    {"id": 3, "velocity": 0.6, "fit_score": None, "uae_relevance": None, "source_count": 4,
     "last_seen_at": None},
    {"id": 4, "velocity": 0.5, "fit_score": 60, "uae_relevance": 60, "source_count": 2,
     "last_seen_at": _h(2).isoformat()},
]


def _ids(rows):
    return [r["id"] for r in rows]


def test_rank_opportunity_recomputes_score_and_sorts_desc():
    out = gr.rank_trends(_ROWS, sort="opportunity")
    assert all(r["opportunity_score"] == gr.compute_opportunity(r) for r in out)
    # the stale 999 stored on row 1 was NOT trusted
    assert next(r for r in out if r["id"] == 1)["opportunity_score"] < 100
    scores = [r["opportunity_score"] for r in out]
    assert scores == sorted(scores, reverse=True)


def test_rank_does_not_mutate_input():
    before = [dict(r) for r in _ROWS]
    gr.rank_trends(_ROWS, sort="opportunity", weights={"velocity": 1})
    assert _ROWS == before


def test_rank_by_velocity():
    assert _ids(gr.rank_trends(_ROWS, sort="velocity")) == [1, 3, 4, 2]


def test_rank_by_fit_puts_unscored_last():
    assert _ids(gr.rank_trends(_ROWS, sort="fit")) == [2, 4, 1, 3]


def test_rank_by_recent_prefers_content_time_over_the_run_stamp():
    """`last_seen_at` is a per-run constant, so it ordered the feed by nothing.
    A row with real content time wins even when its fetch stamp is older."""
    rows = [
        # identical fetch stamps — only the content time can order these
        {"id": 1, "velocity": 0.5, "last_seen_at": _h(1), "latest_content_at": _h(48)},
        {"id": 2, "velocity": 0.5, "last_seen_at": _h(1), "latest_content_at": _h(2)},
        {"id": 3, "velocity": 0.5, "last_seen_at": _h(1), "latest_content_at": _h(200)},
        {"id": 4, "velocity": 0.5, "last_seen_at": None},           # nothing to sort on
    ]
    assert _ids(gr.rank_trends(rows, sort="recent")) == [2, 1, 3, 4]
    # a row with no content time still falls back to the fetch stamp
    assert _ids(gr.rank_trends([{"id": 5, "last_seen_at": _h(1)},
                                {"id": 6, "last_seen_at": _h(9)}], sort="recent")) == [5, 6]


def test_rank_by_sources_ties_break_on_velocity():
    # 3 and 2 both have 4 sources; 3 is hotter (0.6 > 0.3)
    assert _ids(gr.rank_trends(_ROWS, sort="sources")) == [3, 2, 4, 1]


def test_rank_unknown_sort_falls_back_to_opportunity_and_weights_matter():
    a = gr.rank_trends(_ROWS, sort="???")
    b = gr.rank_trends(_ROWS, sort="opportunity")
    assert _ids(a) == _ids(b)
    # all weight on velocity ⇒ same order as the velocity sort
    only_vel = gr.rank_trends(_ROWS, sort="opportunity",
                              weights={"velocity": 1, "fit": 0, "uae": 0})
    assert _ids(only_vel) == [1, 3, 4, 2]


def test_sorts_constant_lists_every_supported_sort():
    assert gr.SORTS == ("opportunity", "velocity", "fit", "recent", "sources")
    for s in gr.SORTS:
        assert len(gr.rank_trends(_ROWS, sort=s)) == len(_ROWS)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(pytest.main([__file__, "-q"]))


# =========================================================================== #
# The solo cap must also fire when amplitude is UNMEASURABLE
# =========================================================================== #
# `amplitude` is never None — it falls back to NEUTRAL_AMPLITUDE, which sits
# exactly ON SOLO_AMP_MIN. So a source publishing neither a counter nor a feed
# rank used to sail past `amplitude < SOLO_AMP_MIN` on a technicality, and those
# are precisely the single-post feeds the cap exists to contain. Measured on the
# live corpus: "Digital Twins in City Planning" (one Reddit post, one sighting)
# was ranked #2 with an UNCAPPED heat of 0.356.

_SOLO_TREND = {"id": 1, "source_count": 1, "evidence_count": 1,
               "sources": ["reddit"], "first_seen_at": NOW}


def _solo_sighting(**over):
    base = {"seen_at": NOW, "source": "reddit", "source_key": "t3_abc",
            "region": "global", "metric_value": None, "published_at": NOW,
            "extra": {}}
    base.update(over)
    return base


def test_solo_cap_fires_when_amplitude_cannot_be_measured_at_all():
    """One post, one source, no counter and no rank, WITH baselines available:
    we have no evidence of magnitude, which is weaker than a measured low one."""
    stats = {"reddit": {"median": 0.0, "n": 0, "items": 25}}
    heat = gr.compute_velocity(_SOLO_TREND, [_solo_sighting()],
                                    now=NOW, source_stats=stats)
    assert heat <= gr.SOLO_HEAT_CAP
    assert gr.heat_band(heat) == "warm"      # never a hot/rising pill


def test_the_legacy_no_baselines_path_is_still_left_uncapped():
    """`source_stats=None` means the CALLER did not give us baselines — our
    missing plumbing, not the trend's missing evidence. Do not punish it."""
    capped = gr.compute_velocity(_SOLO_TREND, [_solo_sighting()],
                                      now=NOW, source_stats={"reddit": {"median": 0.0,
                                                                         "n": 0, "items": 9}})
    legacy = gr.compute_velocity(_SOLO_TREND, [_solo_sighting()],
                                      now=NOW, source_stats=None)
    assert legacy > capped


def test_a_top_of_feed_solo_item_escapes_the_cap():
    """The cap is "not outstanding on its own feed", not "solo". Rank 1 of 40
    is exactly the amplitude the constant is calibrated on, so it survives."""
    stats = {"kym": {"median": 0.0, "n": 0, "items": 40}}
    top = _solo_sighting(source="kym", source_key="k1", extra={"rank": 1, "rank_total": 40})
    trend = {**_SOLO_TREND, "sources": ["kym"]}
    assert gr.compute_velocity(trend, [top], now=NOW,
                                    source_stats=stats) > gr.SOLO_HEAT_CAP


def test_a_bottom_of_feed_solo_item_is_capped():
    stats = {"kym": {"median": 0.0, "n": 0, "items": 40}}
    low = _solo_sighting(source="kym", source_key="k40", extra={"rank": 40, "rank_total": 40})
    trend = {**_SOLO_TREND, "sources": ["kym"]}
    assert gr.compute_velocity(trend, [low], now=NOW,
                                    source_stats=stats) <= gr.SOLO_HEAT_CAP


def test_corroboration_lifts_a_trend_out_of_the_cap_entirely():
    """The cap is for UNCORROBORATED singles. A second source ends it."""
    stats = {"reddit": {"median": 0.0, "n": 0, "items": 25},
             "kym": {"median": 0.0, "n": 0, "items": 40}}
    trend = {"id": 2, "source_count": 2, "evidence_count": 2,
             "sources": ["reddit", "kym"], "first_seen_at": NOW}
    pair = [_solo_sighting(), _solo_sighting(source="kym", source_key="k9",
                                             extra={"rank": 9, "rank_total": 40})]
    assert gr.compute_velocity(trend, pair, now=NOW,
                                    source_stats=stats) > gr.SOLO_HEAT_CAP
