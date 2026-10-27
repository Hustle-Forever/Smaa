"""General Trends — is this item current enough to be a trend?

The owner's ask is "nothing older than about a month, and the feed should feel
current". Doing that with one global age rule would be WRONG, because the
sources do not mean the same thing by their timestamp. Measured on the live
corpus (766 sightings, run #1, 2026-08-19; age = ``seen_at - published_at``):

    source          n     min    median   p90     max     what the stamp MEANS
    x_trends      240     0.4h    0.7h    1.0h    1.0h    the hourly snapshot
    google_trends  60     0.3h    1.5h    5.5h    5.8h    the search spike
    kym            22     0.7h    3.4h    5.7h    6.3h    the article
    reddit        249     0.4h    9.6h   26.8h  104.6h    the post
    wikipedia     120    45.5h   46.5h   46.8h   46.8h    the pageview DAY
    bluesky        75    25.0h   33.2h  106.7h  435.4h    ``startedAt``

x_trends (0.7h) and wikipedia (46.5h) are CONSTANTS: the feed *is* the window,
so there is no age in there to gate on. bluesky's 435h is a topic that started
18 days ago and that Bluesky still lists as trending — it is current, and a
72h rule would throw it away. Only reddit/google/kym/youtube carry the
content's own age. Hence three classes:

    AGE_FROM_ITEM    published_at is the content's own age  -> per-source window
    AGE_FROM_WINDOW  published_at is the OBSERVATION window -> nothing to gate
    AGE_FROM_TREND   published_at is when the TREND started -> ceiling only

Pure: no network, no DB, no config import (settings are passed in), so the
tests drive it directly and the pipeline owns the policy.

The Wikipedia "is this page actually rising, or just permanently popular?"
gate is a DIFFERENT question (evergreen subject, not stale timestamp) and
lives in the Wikipedia parser as ``general_trends_sources.wiki_is_rising``.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

log = logging.getLogger("archivist.general_trends_freshness")

# --------------------------------------------------------------------------- #
# The three timestamp semantics
# --------------------------------------------------------------------------- #

#: ``published_at`` is the item's own publication time. Gate on it.
AGE_FROM_ITEM: frozenset[str] = frozenset({
    "google_trends", "reddit", "kym", "youtube",
    "hacker_news", "lemmy", "github", "google_news",
    # Instagram post timestamps are real content times. No live token yet, so
    # nothing is measured — they get the ceiling only (no entry below).
    "instagram", "instagram_watch",
})

#: The feed IS a window on "now" (an hourly X snapshot, a D-1 pageview table,
#: a 7/30-day Creative Center period). It cannot contain stale items, so an age
#: gate is a no-op — and a *harmful* one, because the constant it would measure
#: is a property of the feed, not of the content. These are never dropped for
#: age; their staleness problem is "evergreen subject", handled in the parsers.
#: ``youtube_shorts`` is here because its ``published_at`` is the CHART
#: PERIOD'S END DATE — the week the ranking covers — not when the sound was
#: made or when anyone used it. Gating a sound on that date would measure how
#: recently YouTube published a chart, which is always "this week".
AGE_FROM_WINDOW: frozenset[str] = frozenset({
    "x_trends", "wikipedia", "tiktok", "youtube_shorts",
})

#: ``published_at`` is when the TREND began, not when an item was made. A
#: topic that started 18 days ago and that the platform STILL lists is current
#: by definition. Only the ~1-month ceiling applies here.
AGE_FROM_TREND: frozenset[str] = frozenset({"bluesky", "mastodon"})

#: Per-source maximum item age, in HOURS. Only consulted for AGE_FROM_ITEM.
#:
#: Every number is measured (see the module docstring):
#:   google_trends 48  — max observed 5.8h; 48h is ~8x the observed spread
#:   reddit        72  — p90 26.8h, max 104.6h; 72h drops only the 2.4% tail
#:   kym           72  — max 6.3h; matched to reddit, the same "a post" semantic
#:   youtube      336  — 14d: chart MEMBERSHIP is the freshness proof, not the
#:                       upload date (a 10-day-old video on today's chart is
#:                       today's trend)
#:   bluesky      720  — 30d, i.e. the ceiling. NOT consulted while bluesky is
#:                       AGE_FROM_TREND (that class takes the ceiling only); it
#:                       is recorded so the number survives a reclassification.
#:
#: A source with NO entry here is gated by the ceiling alone. That is
#: deliberate: an unmeasured window is a guess, and a guessed 72h would gut
#: github (``created:>`` returns repos up to a month old) and google_news
#: (``when:7d``) — both of which already enforce recency server-side.
DEFAULT_AGE_OVERRIDES: dict[str, int] = {
    "google_trends": 48,
    "reddit": 72,
    "kym": 72,
    "youtube": 336,
    "bluesky": 720,
}

#: A feed clock ahead of ours by more than this is skew or a bad parse, not the
#: future. Such a stamp is treated as "no usable age", never as "brand new" —
#: otherwise a broken parser mints permanently-fresh items.
FUTURE_SLACK_HOURS = 6.0

#: The verdicts and reasons this module can return (the pipeline counts them
#: into ``stats[source]["dropped_by"]``, so they are short, stable slugs).
VERDICTS: tuple[str, ...] = ("keep", "drop")
REASONS: tuple[str, ...] = ("stale", "beyond_max_age", "undated")

# One warning per source per process when a feed stops dating its items — see
# ``screen``. A per-item warning would drown the log; silence would hide a
# parser regression, which is the failure this whole guard exists to surface.
_warned_undated: set[str] = set()

_FRACTION_TRIM = re.compile(r"^(.*?\.\d{1,6})\d*([+-]\d\d:?\d\d)?$")


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def reset_undated_warnings() -> None:
    """Forget which sources have already warned (call once per run/test)."""
    _warned_undated.clear()


def _to_dt(value: Any) -> Optional[datetime]:
    """datetime (aware or naive) or ISO string -> tz-aware UTC datetime.

    None for anything unusable. Naive datetimes are taken as UTC — that is what
    the rest of the General Trends code assumes (see ranking._to_dt). Never
    raises: a malformed timestamp is an unknown age, not an exception.
    """
    if value is None or value == "":
        return None
    dt: Optional[datetime]
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, str):
        s = value.strip()
        if not s:
            return None
        if s.endswith(("Z", "z")):
            s = s[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(s)
        except ValueError:
            # >6 fractional digits (Bluesky emits them); trim and retry once.
            m = _FRACTION_TRIM.match(s)
            if not m:
                return None
            try:
                dt = datetime.fromisoformat(m.group(1) + (m.group(2) or ""))
            except ValueError:
                return None
    else:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _source(item: Any) -> str:
    if not isinstance(item, dict):
        return ""
    return str(item.get("source") or "").strip()


def _hours(value: Any) -> Optional[float]:
    """A limit in hours, or None when it is absent/junk/disabled (<= 0).

    Junk is ignored rather than raised on: a bad env value must not take the
    whole refresh down, and "no window" is the safe reading (the ceiling still
    applies).
    """
    if value is None:
        return None
    try:
        hours = float(value)
    except (TypeError, ValueError):
        return None
    if hours != hours or hours <= 0:      # NaN or disabled
        return None
    return hours


def _ceiling_hours(max_age_days: Any) -> Optional[float]:
    """The owner's "~1 month" ceiling expressed in hours; None when disabled."""
    days = _hours(max_age_days)
    return None if days is None else days * 24.0


def age_class(source: str) -> str:
    """'window' | 'trend' | 'item'.

    An unknown source falls back to 'item': gating on its own timestamp is the
    conservative reading, and with no entry in DEFAULT_AGE_OVERRIDES that means
    the ceiling only — a new source can never be silently gutted by a window
    nobody measured for it.
    """
    src = str(source or "").strip()
    if src in AGE_FROM_WINDOW:
        return "window"
    if src in AGE_FROM_TREND:
        return "trend"
    return "item"


def item_age_hours(item: dict, *, now: datetime) -> Optional[float]:
    """Hours between the item's own timestamp and ``now``.

    None means "no usable age": the source carries no content age
    (AGE_FROM_WINDOW), or the stamp is missing, unparsable, or implausibly in
    the future.
    """
    if age_class(_source(item)) == "window":
        return None
    published = _to_dt(item.get("published_at") if isinstance(item, dict) else None)
    if published is None:
        return None
    reference = _to_dt(now) or datetime.now(timezone.utc)
    age = (reference - published).total_seconds() / 3600.0
    if age < -FUTURE_SLACK_HOURS:
        return None
    return max(age, 0.0)


def content_time(item: dict) -> Optional[datetime]:
    """The timestamp that should feed ``general_trends.latest_content_at``.

    None for AGE_FROM_WINDOW sources: they carry no content age at all, so a
    Wikipedia-only or X-only trend must be judged on ``last_seen_at`` alone
    rather than on a constant that would make it look 46h old forever. Also
    None for a missing or unparsable stamp.

    Deliberately takes no ``now`` and reads no clock: it must stay pure so
    callers can compute it for historic items. The clock-skew guard lives in
    ``screen``/``item_age_hours``, which do have a ``now``.
    """
    if not isinstance(item, dict):
        return None
    if age_class(_source(item)) == "window":
        return None
    return _to_dt(item.get("published_at"))


# --------------------------------------------------------------------------- #
# The screen
# --------------------------------------------------------------------------- #

def screen(
    item: dict,
    *,
    now: datetime,
    max_age_days: int = 30,
    overrides: Optional[dict[str, int]] = None,
    drop_undated: bool = False,
) -> tuple[str, str]:
    """('keep'|'drop', reason) for one raw item. Never raises.

    ``max_age_days`` is the owner's "nothing older than ~1 month" ceiling and
    applies to every class except AGE_FROM_WINDOW; ``<= 0`` disables it.

    ``overrides`` MERGES over ``DEFAULT_AGE_OVERRIDES`` ({source: hours}), so a
    caller can retune one source without restating the measured defaults; a
    value ``<= 0`` disables that source's window (the ceiling still applies).
    Only AGE_FROM_ITEM sources consult it.

    Reason slugs: 'stale' (past the per-source window), 'beyond_max_age' (past
    the ceiling), 'undated' (no usable timestamp and ``drop_undated``), or ''.

    A MISSING published_at KEEPS by default, and that is deliberate: every
    source populates it today, so a sudden None means the feed SHAPE changed.
    Dropping would turn a parser regression into a silent source outage — the
    exact failure YouTube already has (no API key => 0 items, no error anyone
    can see). We keep, and warn once per source instead.
    """
    source = _source(item)
    klass = age_class(source)

    # A window source has no content age. Nothing to gate — not even
    # ``drop_undated``, which asks "did the parser lose the date?" and is
    # meaningless for a feed that never had one.
    if klass == "window":
        return "keep", ""

    age = item_age_hours(item, now=now)
    if age is None:
        if drop_undated:
            return "drop", "undated"
        if source and source not in _warned_undated:
            _warned_undated.add(source)
            log.warning(
                "general_trends: %s emitted an item with no usable published_at "
                "(%r) — keeping it, but the feed shape may have changed",
                source or "?", (item.get("published_at") if isinstance(item, dict) else None),
            )
        return "keep", ""

    # The ceiling first: it is the owner's rule, it applies to every class, and
    # 'beyond_max_age' is the more informative reason when both fire.
    ceiling = _ceiling_hours(max_age_days)
    if ceiling is not None and age > ceiling:
        return "drop", "beyond_max_age"

    # AGE_FROM_TREND: the stamp is the trend's BIRTH. A Bluesky topic 435h old
    # that Bluesky still lists is trending NOW; the tight per-source window
    # would be measuring the wrong thing, so only the ceiling above applies.
    if klass == "trend":
        return "keep", ""

    limits = DEFAULT_AGE_OVERRIDES
    if isinstance(overrides, dict) and overrides:
        limits = {**DEFAULT_AGE_OVERRIDES, **overrides}
    limit = _hours(limits.get(source))
    if limit is None:
        return "keep", ""       # unmeasured source (or window disabled): ceiling only
    if age > limit:
        return "drop", "stale"
    return "keep", ""


def cutoff(now: datetime, *, max_age_days: int = 30) -> Optional[datetime]:
    """The ceiling as an absolute instant — handy for SQL/log messages."""
    reference = _to_dt(now) or datetime.now(timezone.utc)
    ceiling = _ceiling_hours(max_age_days)
    if ceiling is None:
        return None
    return reference - timedelta(hours=ceiling)
