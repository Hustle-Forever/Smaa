"""The only windows a report may cover.

WHY THIS EXISTS. A report used to be able to cover any run of days that someone
could drag out in a calendar: 11 days, 12 days, Tuesday to Tuesday, half of one
week and half of the next. That produces an unbounded number of documents, no
two of which can be laid beside each other, and every one of them costs two
model passes. "Competitor posts rose 20%" means nothing when the window it rose
over was chosen by hand.

So there are three shapes and no others:

    week   Monday to Sunday. The week people mean when they say "last week".
    half   The 1st to the 15th, or the 16th to the last day of the month.
    month  The 1st to the last day.

and one rule on top of them: A PERIOD IS ONLY OFFERED ONCE IT HAS FINISHED. A
report about half a month is a report about half the data, and the reader has
no way to see that from the numbers. It also makes every report final: a
finished period cannot gain any more days, so the document is built once and
then it is just read.

Note that `days` is not fixed per shape and is not supposed to be. A second
half is 13 days in February and 16 in March; a month is 28 to 31. The window
the builder measures is (`days`, `end`) and it always has been, so nothing
downstream needed to learn about periods: they are a constraint on which pairs
are allowed, not a new kind of window.
"""
from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Iterable, Optional

GRAINS: tuple[str, ...] = ("week", "half", "month")
DEFAULT_GRAIN = "week"

# What each shape is called on screen. The plain-English names the team already
# uses for them, not the jargon.
GRAIN_LABELS: dict[str, str] = {
    "week": "1 week",
    "half": "15 days",
    "month": "30 days",
}

_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun",
           "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")

_MONTHS_LONG = ("January", "February", "March", "April", "May", "June", "July",
                "August", "September", "October", "November", "December")


@dataclass(frozen=True)
class Period:
    """One allowed window. `key` is stable and sortable; `label` is for humans."""

    grain: str
    start: date
    end: date

    @property
    def days(self) -> int:
        """Whole days, counting both ends, which is what
        `make_window(whole_days=True)` measures."""
        return (self.end - self.start).days + 1

    @property
    def key(self) -> str:
        if self.grain == "week":
            year, week, _ = self.start.isocalendar()
            return f"{year}-W{week:02d}"
        if self.grain == "half":
            half = 1 if self.start.day == 1 else 2
            return f"{self.start:%Y-%m}-H{half}"
        return f"{self.start:%Y-%m}"

    @property
    def label(self) -> str:
        if self.grain == "month":
            return f"{_MONTHS_LONG[self.start.month - 1]} {self.start.year}"
        head = f"{self.start.day} {_MONTHS[self.start.month - 1]}"
        tail = f"{self.end.day} {_MONTHS[self.end.month - 1]} {self.end.year}"
        if self.grain == "week":
            # The days are the point of a week: "Mon 7 - Sun 13" is a week at a
            # glance, "7 - 13" is a number of days that happens to be seven.
            return (f"Mon {head} - Sun {tail}" if self.start.month != self.end.month
                    else f"Mon {self.start.day} - Sun {tail}")
        if self.start.month == self.end.month:
            return f"{self.start.day} - {tail}"
        return f"{head} - {tail}"

    def as_dict(self) -> dict:
        return {
            "key": self.key,
            "grain": self.grain,
            "from": self.start.isoformat(),
            "to": self.end.isoformat(),
            "days": self.days,
            "label": self.label,
        }


def clean_grain(value: object) -> str:
    text = str(value or "").strip().lower()
    return text if text in GRAINS else DEFAULT_GRAIN


def _last_day(year: int, month: int) -> int:
    return calendar.monthrange(year, month)[1]


def containing(grain: str, day: date) -> Period:
    """The period of this shape that `day` falls in, finished or not."""
    grain = clean_grain(grain)
    if grain == "week":
        start = day - timedelta(days=day.weekday())     # Monday is 0
        return Period("week", start, start + timedelta(days=6))
    if grain == "half":
        if day.day <= 15:
            return Period("half", day.replace(day=1), day.replace(day=15))
        last = _last_day(day.year, day.month)
        return Period("half", day.replace(day=16), day.replace(day=last))
    last = _last_day(day.year, day.month)
    return Period("month", day.replace(day=1), day.replace(day=last))


def previous(period: Period) -> Period:
    """The period of the same shape immediately before this one."""
    return containing(period.grain, period.start - timedelta(days=1))


def is_complete(period: Period, today: date) -> bool:
    """A period has finished once today is past its last day.

    Strictly past: a period that ends TODAY is still collecting, and a report
    about it would be a report about part of it with nothing on the page to
    say so."""
    return period.end < today


def recent(grain: str, *, today: date, limit: int = 12,
           since: Optional[date] = None) -> list[Period]:
    """The most recently FINISHED periods of this shape, newest first.

    `since` is the first day we hold any data for: a period that ends before it
    is one we were not collecting through at all, and offering it would spend
    two model passes to report that nothing happened for reasons that have
    nothing to do with the world."""
    grain = clean_grain(grain)
    out: list[Period] = []
    period = containing(grain, today)
    while len(out) < max(1, limit):
        period = previous(period)
        if not is_complete(period, today):
            continue                    # cannot happen, but the rule is the rule
        if since is not None and period.end < since:
            break
        out.append(period)
    return out


def from_window(days: int, end: date) -> Optional[Period]:
    """The period a (days, end) pair describes, or None if it is not one.

    This is what makes the rule real rather than a habit of the UI: the route
    rebuilds the period from the numbers it was sent and refuses anything that
    is not one of the three shapes. `days` is checked too, so "the 30 days
    ending on the 31st" is rejected in a month with 31 of them."""
    try:
        days = int(days)
    except (TypeError, ValueError):
        return None
    for grain in GRAINS:
        period = containing(grain, end)
        if period.end == end and period.days == days:
            return period
    return None


def from_row(days: int, start: Optional[date], end: Optional[date]) -> Optional[Period]:
    """The period a STORED row covers, or None. Checks both ends.

    `from_window` alone is not enough for a row out of the table. A rolling
    7-day window ending on a Sunday has the width of a week and the last day of
    a week, but it began mid-Monday rather than at the start of it, and the
    reports built before these shapes existed are exactly that. Left
    unchecked, one of them would be offered as "the week of the 14th" the
    moment that week finished, and someone would open a document built from
    part of it."""
    if end is None:
        return None
    period = from_window(days, end)
    if period is None:
        return None
    if start is not None and period.start != start:
        return None
    return period


def describe(days: int, end: Optional[date],
             start: Optional[date] = None) -> str:
    """A stored window in words, period or not.

    Reports built before the three shapes existed are still in the table, and a
    row labelled only by its dates reads as something random that someone
    downloaded by accident. This says what each one actually is."""
    if end is None:
        return "window unknown"
    period = from_row(days, start, end)
    if period is not None:
        return period.label
    return f"{days} days to {end.day} {_MONTHS[end.month - 1]} {end.year}"


def all_keys(periods: Iterable[Period]) -> list[str]:
    return [p.key for p in periods]
