"""Tests for ``app/services/general_trends_freshness.py`` — the recency screen.

Every number in the per-source table below is MEASURED on the live run #1
corpus (766 sightings, 2026-08-19), not invented: reddit p90 26.8h / max
104.6h, google_trends max 5.8h, kym max 6.3h, bluesky ``startedAt`` max 435.4h
(a topic 18 days old that Bluesky still lists as trending), x_trends a constant
0.7h and wikipedia a constant 46.5h (those two feeds ARE the window). The whole
point of the module is that one global age rule cannot serve all three
semantics, so these cases are the contract.

Pure functions, no network, no DB, no model. Runs under pytest and standalone
(``cd backend && python tests/test_general_trends_freshness.py``).
"""
from __future__ import annotations

import logging
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

_BACKEND_ROOT = Path(__file__).resolve().parent.parent
if str(_BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(_BACKEND_ROOT))

from app.services import general_trends_freshness as gf  # noqa: E402

_NOW = datetime(2026, 8, 19, 12, 0, tzinfo=timezone.utc)

# The sources the contract names. Kept literal (rather than imported from
# general_trends_sources) so this file stays independent of that module.
_KNOWN_SOURCES = (
    "google_trends", "bluesky", "reddit", "kym", "wikipedia", "youtube",
    "x_trends", "hacker_news", "lemmy", "github", "google_news", "tiktok",
    "instagram", "instagram_watch", "mastodon",
)


@pytest.fixture(autouse=True)
def _fresh_warning_state():
    """The one-warning-per-source guard is module state; keep tests ordered
    independently."""
    gf.reset_undated_warnings()
    yield
    gf.reset_undated_warnings()


def _at(hours_ago: float) -> str:
    """An ISO stamp ``hours_ago`` before _NOW, in the shape the parsers emit."""
    return (_NOW - timedelta(hours=hours_ago)).isoformat().replace("+00:00", "Z")


def _item(source: str, hours_old=None, **over) -> dict:
    """A RawItem-shaped dict; ``hours_old=None`` means no published_at."""
    item = {
        "source": source,
        "source_key": "key",
        "title": "a title",
        "url": None,
        "summary": "",
        "published_at": _at(hours_old) if hours_old is not None else None,
        "region": "global",
        "extra": {},
    }
    item.update(over)
    return item


# =========================================================================== #
# 1. The three classes
# =========================================================================== #

def test_the_three_classes_are_disjoint():
    assert not (gf.AGE_FROM_ITEM & gf.AGE_FROM_WINDOW)
    assert not (gf.AGE_FROM_ITEM & gf.AGE_FROM_TREND)
    assert not (gf.AGE_FROM_WINDOW & gf.AGE_FROM_TREND)


def test_every_known_source_is_classified():
    unclassified = [
        s for s in _KNOWN_SOURCES
        if s not in (gf.AGE_FROM_ITEM | gf.AGE_FROM_WINDOW | gf.AGE_FROM_TREND)
    ]
    assert unclassified == []


@pytest.mark.parametrize("source, klass", [
    ("reddit", "item"), ("google_trends", "item"), ("kym", "item"),
    ("youtube", "item"), ("hacker_news", "item"), ("lemmy", "item"),
    ("github", "item"), ("google_news", "item"), ("instagram", "item"),
    ("x_trends", "window"), ("wikipedia", "window"), ("tiktok", "window"),
    ("bluesky", "trend"), ("mastodon", "trend"),
    ("something_new", "item"),   # unknown -> gate on its own stamp, ceiling only
    ("", "item"),
])
def test_age_class(source, klass):
    assert gf.age_class(source) == klass


def test_default_overrides_are_the_measured_windows():
    assert gf.DEFAULT_AGE_OVERRIDES == {
        "google_trends": 48, "reddit": 72, "kym": 72,
        "youtube": 336, "bluesky": 720,
    }


# =========================================================================== #
# 2. Per-source age semantics — the measured table
# =========================================================================== #

@pytest.mark.parametrize("source, hours_old, verdict", [
    # AGE_FROM_ITEM: published_at is the content's own age.
    ("reddit", 9.6, "keep"),           # measured median
    ("reddit", 26.8, "keep"),          # measured p90
    ("reddit", 72.0, "keep"),          # exactly at the window: keep
    ("reddit", 104.6, "drop"),         # measured max — the 2.4% stale tail
    ("google_trends", 1.5, "keep"),    # measured median
    ("google_trends", 5.8, "keep"),    # measured max
    ("google_trends", 60.0, "drop"),   # past the 48h window
    ("kym", 6.3, "keep"),              # measured max
    ("kym", 100.0, "drop"),
    ("youtube", 300.0, "keep"),        # 14d window: chart membership is the proof
    ("youtube", 400.0, "drop"),
    # AGE_FROM_TREND: the stamp is the trend's birth, not an item's age.
    ("bluesky", 33.2, "keep"),         # measured median
    ("bluesky", 435.4, "keep"),        # measured max — 18d and still trending
    ("bluesky", 800.0, "drop"),        # past the 30d ceiling
    ("mastodon", 435.4, "keep"),
    # AGE_FROM_WINDOW: the feed IS the window; the number carries no age.
    ("x_trends", 0.7, "keep"),         # measured median (a constant)
    ("wikipedia", 46.5, "keep"),       # measured median (a constant)
    ("x_trends", 10_000.0, "keep"),
    ("wikipedia", 10_000.0, "keep"),
    ("tiktok", 10_000.0, "keep"),
])
def test_screen_per_source_age_semantics(source, hours_old, verdict):
    assert gf.screen(_item(source, hours_old), now=_NOW)[0] == verdict


def test_stale_item_reports_the_stale_slug():
    assert gf.screen(_item("reddit", 104.6), now=_NOW) == ("drop", "stale")


def test_a_kept_item_reports_no_reason():
    assert gf.screen(_item("reddit", 9.6), now=_NOW) == ("keep", "")


@pytest.mark.parametrize("source", ["hacker_news", "lemmy", "github", "google_news",
                                    "instagram", "instagram_watch", "something_new"])
def test_sources_without_a_measured_window_get_the_ceiling_only(source):
    # 400h would be "stale" under a guessed 72h default. github's ``created:>``
    # and google_news's ``when:7d`` already gate recency server-side, so an
    # invented window would gut them — they answer to the ceiling alone.
    assert gf.screen(_item(source, 400.0), now=_NOW) == ("keep", "")
    assert gf.screen(_item(source, 40 * 24), now=_NOW) == ("drop", "beyond_max_age")


# =========================================================================== #
# 3. The ~1-month ceiling (the owner's literal ask)
# =========================================================================== #

def test_ceiling_drops_a_trend_class_item_past_thirty_days():
    assert gf.screen(_item("bluesky", 40 * 24), now=_NOW) == ("drop", "beyond_max_age")


def test_ceiling_boundary_keeps_exactly_thirty_days():
    assert gf.screen(_item("bluesky", 30 * 24), now=_NOW)[0] == "keep"
    assert gf.screen(_item("bluesky", 30 * 24 + 1), now=_NOW) == ("drop", "beyond_max_age")


def test_ceiling_is_configurable():
    item = _item("bluesky", 10 * 24)
    assert gf.screen(item, now=_NOW, max_age_days=30)[0] == "keep"
    assert gf.screen(item, now=_NOW, max_age_days=7) == ("drop", "beyond_max_age")


def test_ceiling_of_zero_disables_it():
    assert gf.screen(_item("bluesky", 5 * 365 * 24), now=_NOW, max_age_days=0)[0] == "keep"


@pytest.mark.parametrize("bad", [None, "thirty", -1, float("nan"), object()])
def test_junk_ceiling_disables_it_instead_of_raising(bad):
    # A bad env value must not take the whole refresh down.
    assert gf.screen(_item("bluesky", 400 * 24), now=_NOW, max_age_days=bad)[0] == "keep"


def test_window_sources_ignore_the_ceiling_too():
    # There is no content age to be beyond the ceiling WITH.
    assert gf.screen(_item("wikipedia", 400 * 24), now=_NOW) == ("keep", "")


def test_ceiling_wins_over_the_per_source_window_when_both_fire():
    # A reddit item 40 days old is both stale and beyond the ceiling; the
    # ceiling is the owner-facing rule, so that is the reason reported.
    assert gf.screen(_item("reddit", 40 * 24), now=_NOW) == ("drop", "beyond_max_age")


# =========================================================================== #
# 4. Undated items — keep and warn, never a silent source outage
# =========================================================================== #

@pytest.mark.parametrize("source", ["reddit", "google_trends", "bluesky", "github"])
def test_missing_published_at_keeps_by_default(source):
    assert gf.screen(_item(source, None), now=_NOW) == ("keep", "")


def test_missing_published_at_drops_when_drop_undated_is_on():
    assert gf.screen(_item("reddit", None), now=_NOW, drop_undated=True) == ("drop", "undated")


def test_undated_window_source_is_never_dropped_even_with_drop_undated():
    # 'undated' asks "did the parser lose the date?"; a window source never had
    # one to lose.
    assert gf.screen(_item("x_trends", None), now=_NOW, drop_undated=True) == ("keep", "")


def test_undated_warns_once_per_source(caplog):
    with caplog.at_level(logging.WARNING, logger="archivist.general_trends_freshness"):
        for _ in range(5):
            gf.screen(_item("reddit", None), now=_NOW)
        gf.screen(_item("kym", None), now=_NOW)
    warned = [r for r in caplog.records if "published_at" in r.getMessage()]
    assert len(warned) == 2                       # one per source, not per item
    gf.reset_undated_warnings()
    with caplog.at_level(logging.WARNING, logger="archivist.general_trends_freshness"):
        gf.screen(_item("reddit", None), now=_NOW)
    assert len([r for r in caplog.records if "published_at" in r.getMessage()]) == 3


@pytest.mark.parametrize("bad", ["", "   ", "not a date", "2026-13-45T99:99Z",
                                 0, 1755600000, [], {}, object()])
def test_unparsable_published_at_is_undated_and_never_raises(bad):
    assert gf.screen(_item("reddit", published_at=bad), now=_NOW) == ("keep", "")
    assert gf.screen(_item("reddit", published_at=bad), now=_NOW,
                     drop_undated=True) == ("drop", "undated")


def test_future_timestamp_is_treated_as_unknown_not_brand_new():
    # Clock skew / a bad parse must not mint a permanently-fresh item.
    skewed = _item("reddit", -48.0)               # 48h in the future
    assert gf.screen(skewed, now=_NOW) == ("keep", "")
    assert gf.screen(skewed, now=_NOW, drop_undated=True) == ("drop", "undated")
    assert gf.item_age_hours(skewed, now=_NOW) is None


def test_small_forward_skew_is_tolerated_as_now():
    assert gf.item_age_hours(_item("reddit", -1.0), now=_NOW) == 0.0


def test_screen_never_raises_on_a_junk_item():
    for junk in ({}, {"source": None}, {"source": "reddit"}, {"published_at": _at(1)}):
        verdict, reason = gf.screen(junk, now=_NOW)
        assert verdict in gf.VERDICTS
        assert reason in ("",) + gf.REASONS


# =========================================================================== #
# 5. Overrides
# =========================================================================== #

def test_overrides_merge_over_the_measured_defaults():
    tight = {"reddit": 6}
    assert gf.screen(_item("reddit", 9.6), now=_NOW, overrides=tight) == ("drop", "stale")
    # ... and the untouched defaults still apply
    assert gf.screen(_item("google_trends", 5.8), now=_NOW, overrides=tight)[0] == "keep"
    assert gf.screen(_item("google_trends", 60.0), now=_NOW, overrides=tight) == ("drop", "stale")


def test_override_can_loosen_a_window():
    assert gf.screen(_item("reddit", 104.6), now=_NOW, overrides={"reddit": 168})[0] == "keep"


def test_override_of_zero_disables_the_window_but_not_the_ceiling():
    loose = {"reddit": 0}
    assert gf.screen(_item("reddit", 400.0), now=_NOW, overrides=loose)[0] == "keep"
    assert gf.screen(_item("reddit", 40 * 24), now=_NOW, overrides=loose) == (
        "drop", "beyond_max_age")


def test_override_can_add_a_window_for_a_new_source():
    assert gf.screen(_item("hacker_news", 100.0), now=_NOW,
                     overrides={"hacker_news": 72}) == ("drop", "stale")


def test_junk_override_values_are_ignored_not_raised():
    for bad in ({"reddit": "abc"}, {"reddit": None}, {"reddit": object()},
                {"reddit": float("nan")}):
        assert gf.screen(_item("reddit", 9.6), now=_NOW, overrides=bad)[0] == "keep"


@pytest.mark.parametrize("bad", [None, {}, "reddit:48", 7, []])
def test_missing_or_junk_overrides_fall_back_to_the_defaults(bad):
    assert gf.screen(_item("reddit", 104.6), now=_NOW, overrides=bad) == ("drop", "stale")


def test_string_override_hours_are_accepted():
    # config parsing hands ints, but an env-derived "48" must not explode.
    assert gf.screen(_item("reddit", 60.0), now=_NOW,
                     overrides={"reddit": "48"}) == ("drop", "stale")


def test_overrides_are_not_consulted_for_the_trend_class():
    # bluesky IS in DEFAULT_AGE_OVERRIDES (720h == the ceiling) but the trend
    # class must answer to the ceiling alone, whatever the dict says.
    assert gf.screen(_item("bluesky", 435.4), now=_NOW, overrides={"bluesky": 24})[0] == "keep"


# =========================================================================== #
# 6. content_time — what feeds general_trends.latest_content_at
# =========================================================================== #

@pytest.mark.parametrize("source", ["x_trends", "wikipedia", "tiktok"])
def test_content_time_is_none_for_window_sources(source):
    # Their stamp is a constant property of the feed; writing it to
    # latest_content_at would make every such trend look 46h old forever.
    assert gf.content_time(_item(source, 46.5)) is None


@pytest.mark.parametrize("source", ["reddit", "kym", "youtube", "google_trends",
                                    "hacker_news", "github"])
def test_content_time_is_the_published_time_for_item_sources(source):
    assert gf.content_time(_item(source, 9.6)) == _NOW - timedelta(hours=9.6)


def test_content_time_uses_the_trend_birth_for_trend_sources():
    # A Bluesky topic's startedAt IS its latest content signal — that is what
    # separates "a source still lists it" from "something new happened".
    assert gf.content_time(_item("bluesky", 435.4)) == _NOW - timedelta(hours=435.4)


@pytest.mark.parametrize("bad", [None, "", "not a date", 12345, []])
def test_content_time_is_none_for_a_missing_or_junk_stamp(bad):
    assert gf.content_time(_item("reddit", published_at=bad)) is None


def test_content_time_normalises_to_utc_aware():
    naive = gf.content_time(_item("reddit", published_at="2026-08-19T06:00:00"))
    zulu = gf.content_time(_item("reddit", published_at="2026-08-19T06:00:00Z"))
    offset = gf.content_time(_item("reddit", published_at="2026-08-19T09:00:00+03:00"))
    assert naive == zulu == offset == datetime(2026, 8, 19, 6, 0, tzinfo=timezone.utc)
    assert naive.tzinfo is not None


def test_content_time_accepts_bluesky_style_extra_fractional_digits():
    got = gf.content_time(_item("bluesky", published_at="2026-08-19T06:00:00.1234567Z"))
    assert got is not None and got.hour == 6


def test_content_time_accepts_a_datetime_object():
    assert gf.content_time(_item("reddit", published_at=_NOW)) == _NOW


def test_content_time_on_junk_input_returns_none():
    assert gf.content_time(None) is None            # type: ignore[arg-type]
    assert gf.content_time({}) is None


# =========================================================================== #
# 7. item_age_hours + cutoff
# =========================================================================== #

def test_item_age_hours_measures_from_now():
    assert gf.item_age_hours(_item("reddit", 26.8), now=_NOW) == pytest.approx(26.8)


def test_item_age_hours_is_none_for_window_sources():
    assert gf.item_age_hours(_item("wikipedia", 46.5), now=_NOW) is None


def test_item_age_hours_accepts_a_naive_now_as_utc():
    naive_now = _NOW.replace(tzinfo=None)
    assert gf.item_age_hours(_item("reddit", 5.0), now=naive_now) == pytest.approx(5.0)


def test_cutoff_is_the_ceiling_instant():
    assert gf.cutoff(_NOW) == _NOW - timedelta(days=30)
    assert gf.cutoff(_NOW, max_age_days=7) == _NOW - timedelta(days=7)
    assert gf.cutoff(_NOW, max_age_days=0) is None


# =========================================================================== #
# 8. The contract surface itself
# =========================================================================== #

def test_reason_slugs_are_the_contract_set():
    assert gf.REASONS == ("stale", "beyond_max_age", "undated")
    assert gf.VERDICTS == ("keep", "drop")


def test_screen_only_ever_returns_keep_or_drop():
    cases = [
        _item("reddit", 1.0), _item("reddit", 999.0), _item("reddit", None),
        _item("wikipedia", 999.0), _item("bluesky", 435.4), _item("bluesky", 9999.0),
    ]
    for it in cases:
        for undated in (False, True):
            verdict, reason = gf.screen(it, now=_NOW, drop_undated=undated)
            assert verdict in ("keep", "drop")
            assert (reason == "") == (verdict == "keep")


if __name__ == "__main__":  # pragma: no cover
    sys.exit(pytest.main([__file__, "-q"]))
