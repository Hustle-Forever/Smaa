"""The only windows a report may cover.

Three shapes, and only once they have finished:

    week   Monday to Sunday
    half   the 1st to the 15th, or the 16th to the last day
    month  the 1st to the last day

These are pure-function tests over `services/report_periods`: no DB, no model,
no router. The routing side of the same rule lives in
test_social_report_saved.py.

Runs standalone (``cd backend && python tests/test_report_periods.py``) and
under pytest.
"""
from __future__ import annotations

import sys
from datetime import date, timedelta
from pathlib import Path

_BACKEND = Path(__file__).resolve().parent.parent
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from app.services import report_periods as rp   # noqa: E402


# ---------------------------------------------------------------------------
# The shapes
# ---------------------------------------------------------------------------

def test_a_week_runs_monday_to_sunday():
    # 2026-09-16 is a Wednesday.
    week = rp.containing("week", date(2026, 9, 16))
    assert week.start == date(2026, 9, 14) and week.start.strftime("%A") == "Monday"
    assert week.end == date(2026, 9, 20) and week.end.strftime("%A") == "Sunday"
    assert week.days == 7
    assert week.label == "Mon 14 - Sun 20 Sep 2026"


def test_a_week_that_straddles_two_months_names_both():
    week = rp.containing("week", date(2026, 9, 2))
    assert week.label == "Mon 31 Aug - Sun 6 Sep 2026"


def test_the_halves_split_at_the_fifteenth():
    first = rp.containing("half", date(2026, 9, 15))
    second = rp.containing("half", date(2026, 9, 16))
    assert (first.start, first.end) == (date(2026, 9, 1), date(2026, 9, 15))
    assert (second.start, second.end) == (date(2026, 9, 16), date(2026, 9, 30))
    assert first.end + timedelta(days=1) == second.start, "no day belongs to both"


def test_a_second_half_is_not_always_fifteen_days_and_must_not_be():
    """Called "15 days" on screen, because that is what the team calls it. The
    WINDOW is the half month, so February's is thirteen days and March's is
    sixteen, and pretending otherwise would either drop days or borrow them
    from the next month."""
    assert rp.containing("half", date(2027, 2, 20)).days == 13
    assert rp.containing("half", date(2026, 3, 20)).days == 16
    assert rp.containing("half", date(2026, 3, 1)).days == 15


def test_a_month_is_the_whole_month():
    month = rp.containing("month", date(2026, 2, 9))
    assert (month.start, month.end) == (date(2026, 2, 1), date(2026, 2, 28))
    assert month.label == "February 2026"


def test_keys_are_stable_and_sort_by_time():
    weeks = [rp.containing("week", date(2026, 9, 7) + timedelta(days=7 * i))
             for i in range(3)]
    keys = [w.key for w in weeks]
    assert keys == sorted(keys), keys
    assert keys[0] == "2026-W37"
    assert rp.containing("half", date(2026, 9, 20)).key == "2026-09-H2"
    assert rp.containing("month", date(2026, 9, 20)).key == "2026-09"


# ---------------------------------------------------------------------------
# Finished, or not offered at all
# ---------------------------------------------------------------------------

def test_a_period_is_finished_only_once_today_is_past_it():
    week = rp.containing("week", date(2026, 9, 14))     # ends Sunday the 20th
    assert not rp.is_complete(week, date(2026, 9, 20)), (
        "a period that ends TODAY is still collecting; a report about it would "
        "be a report about part of it with nothing on the page to say so"
    )
    assert rp.is_complete(week, date(2026, 9, 21))


def test_recent_returns_finished_periods_newest_first():
    got = rp.recent("week", today=date(2026, 9, 20), limit=3)
    assert [p.key for p in got] == ["2026-W37", "2026-W36", "2026-W35"]
    assert all(p.end < date(2026, 9, 20) for p in got)


def test_recent_stops_where_the_data_starts():
    got = rp.recent("month", today=date(2026, 9, 20), limit=12,
                    since=date(2026, 7, 1))
    assert [p.key for p in got] == ["2026-08", "2026-07"], (
        "a period that ended before we collected anything is one we were not "
        "looking through"
    )


def test_recent_of_each_grain_is_contiguous():
    for grain in rp.GRAINS:
        got = rp.recent(grain, today=date(2026, 9, 20), limit=6)
        for newer, older in zip(got, got[1:]):
            assert older.end + timedelta(days=1) == newer.start, (
                f"{grain}: {older.key} and {newer.key} leave a gap"
            )


# ---------------------------------------------------------------------------
# Reading a window back
# ---------------------------------------------------------------------------

def test_a_pair_that_is_a_period_is_recognised():
    assert rp.from_window(7, date(2026, 9, 13)).key == "2026-W37"
    assert rp.from_window(15, date(2026, 9, 15)).key == "2026-09-H1"
    assert rp.from_window(31, date(2026, 8, 31)).key == "2026-08"


def test_a_pair_that_is_not_a_period_is_not_invented():
    # A "week" ending on a Monday.
    assert rp.from_window(7, date(2026, 9, 14)) is None
    # The right end day, the wrong width: August has 31 days, not 30.
    assert rp.from_window(30, date(2026, 8, 31)) is None
    # Eleven days is not a shape at all.
    assert rp.from_window(11, date(2026, 9, 13)) is None


def test_the_three_shapes_cannot_be_mistaken_for_each_other():
    """Two shapes can end on the same day, so the WIDTH has to decide. If they
    could collide, a month would sometimes be stored as a week."""
    for end in (date(2026, 8, 31), date(2027, 2, 28), date(2026, 5, 31)):
        widths = {p.days for p in
                  (rp.containing(g, end) for g in rp.GRAINS)
                  if p.end == end}
        assert len(widths) == len([p for p in
                                   (rp.containing(g, end) for g in rp.GRAINS)
                                   if p.end == end]), end


def test_a_window_that_is_not_a_period_still_describes_itself():
    """Reports built before the shapes existed are still in the table, and a
    row labelled only by its dates reads as something random that got
    downloaded by accident."""
    assert rp.describe(7, date(2026, 9, 13)) == "Mon 7 - Sun 13 Sep 2026"
    assert rp.describe(8, date(2026, 9, 20)) == "8 days to 20 Sep 2026"


def test_an_unknown_grain_falls_back_rather_than_raising():
    assert rp.clean_grain("fortnight") == rp.DEFAULT_GRAIN
    assert rp.clean_grain(None) == rp.DEFAULT_GRAIN
    assert rp.clean_grain("MONTH") == "month"


if __name__ == "__main__":   # pragma: no cover
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))


# ---------------------------------------------------------------------------
# A stored row is checked at BOTH ends
# ---------------------------------------------------------------------------

def test_a_rolling_window_is_not_mistaken_for_the_week_it_ends_in():
    """The reports built before these shapes existed are rolling windows, and
    one ending on a Sunday has the width of a week and the last day of a week.
    Left unchecked it would be offered as "the week of the 14th" the moment
    that week finished, and someone would open a document built from part of
    it."""
    sunday = date(2026, 9, 20)
    assert rp.from_window(7, sunday) is not None, "the end alone looks like a week"
    assert rp.from_row(7, date(2026, 9, 13), sunday) is None, (
        "it began mid-Monday of the week before, so it is not that week"
    )
    assert rp.from_row(7, date(2026, 9, 14), sunday) is not None


def test_a_row_with_no_start_is_taken_at_its_word():
    """Older rows may have no `window_from` at all. The end and the width are
    then all there is to go on, which is the same check the route makes."""
    assert rp.from_row(7, None, date(2026, 9, 20)) is not None


def test_a_rolling_row_describes_itself_as_what_it_is():
    assert rp.describe(7, date(2026, 9, 20), date(2026, 9, 13)) == \
        "7 days to 20 Sep 2026"
    assert rp.describe(7, date(2026, 9, 20), date(2026, 9, 14)) == \
        "Mon 14 - Sun 20 Sep 2026"
    assert rp.describe(30, None) == "window unknown"
