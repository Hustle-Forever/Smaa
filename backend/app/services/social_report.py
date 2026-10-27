"""SMAA Social Intelligence report — assembles both report depths.

WHY THIS MODULE EXISTS, AND WHAT IT REFUSES TO DO
=================================================
The Trends page carries three signal streams (competitor Instagram posts,
AI/tech news, internet-wide viral formats). The owner asked for a
competitor and industry briefing over them. A report is read as *measurement*:
if a number is on the page, somebody will act on it. So the single rule this
module is built around is **never invent a number**.

Three tiers, kept apart everywhere:

  MEASURED      like_count, comments_count, view_count, engagement,
                engagement_rate, outperformance (vs that account's OWN median),
                posted_at, username, permalink, region, language, platform,
                source; and for viral trends: hashtag posts / views /
                views-per-post, source rank, heat (computed from those), the
                evidence sightings and their first/last-seen clocks.
  AI JUDGEMENT  topic, topic_summary, theme, fit_score, uae_relevance,
                suggested_angle, suggested_format, effort, trend_kind,
                sensitivity, safety, replication. Real stored fields — but a
                model wrote them. Every section that shows one says so, and
                `Section["estimated"]` is True whenever ANY displayed value in
                that section came from a model.
  NOT COLLECTED shares (the Instagram Graph API does not expose a share count
                for another account's media — printing 0 would be a lie, so we
                print the words "not available (Instagram API)"), audience
                sentiment (we store no comment text at all), audience
                demographics, and mentions of 42 Abu Dhabi itself (this report
                watches competitors and the internet, not the school).

Measured volumes on the live database when this was written (2026-08-20, last
30 days): 20 competitor posts across **2** handles that returned data (3 are
watched, one has never scanned), 385 news items across 17 outlet feeds, 95
viral trends from 6 public sources over 3 refresh runs. Share of Voice across
two accounts is nearly meaningless — so section 7 computes it and then states
the handle count so nobody over-reads a two-slice pie. A section with nothing
behind it renders `available: False` with a reason. That is a SUCCESS: it is
the difference between a report and a hallucination.

SHAPE
-----
`build_report()` returns ONE dict that the PDF renderer and the frontend both
consume — see the module constants and `_section()` for the contract. The full
brief has eight clear sections: what matters, a compact snapshot, two
competitor views, industry signals, opportunities, actions and data notes. The
narrative agent fills the opening and closing sections in place without
re-ordering anything.

SCOPES OF THE SAME DOCUMENT
---------------------------
The Trends page is read one tab at a time, and a single item is read in a
dialog, so the report is downloadable at several widths — all of them the SAME
{meta, basis, sections} dict, which is the entire design:

  build_report(scope="all")            every ACTIVE stream together
  build_report(scope="competitors")    one stream, same sections, with the ones
  build_report(scope="news")           that stream cannot answer marked
                                       `available: False` WITH a reason
  build_report(scope="viral")          ONLY while the viral stream is switched
                                       on — see VIRAL_STREAM below
  build_item_report(kind=, item_id=)   ONE post or ONE trend — different
                                       sections, same contract, plus the rank of
                                       that item inside its own stream

Which streams are active is not a constant: `GENERAL_TRENDS_ENABLED` is
currently false, so Viral Social Trends is out of every scope, out of the
section list and out of the basis. See the VIRAL_STREAM block below for why the
report follows the collector rather than reporting a frozen snapshot.

A scoped report never borrows a number from a stream it does not cover, and it
says its scope in `meta.title`, in `meta.scope`, in `basis.notes` and in every
affected section's own `basis` line — four places, because a single-stream
figure read as a whole-picture figure is exactly the misreading this module
exists to prevent.

PERFORMANCE
-----------
This runs on a click, and an item report runs on a click with the owner
watching a dialog. So a scope only loads the rows it needs (a viral report
never touches trend_posts) and only assesses tone for the stream it covers; an
item report assesses tone for ONE text and skips the bulk sightings read
entirely. Within a scope it is still aggregate queries only, no query per row:
everything for both windows is pulled once into `ReportData` and every section
builder is a pure function over those lists. That also makes the builders
testable with fixture rows and no database (see tests/test_social_report.py).
"""
from __future__ import annotations

import logging
import math
import re
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Iterable, Optional

from ..config import config
from ..db import get_conn
from . import general_trends_ranking as ranking
from . import general_trends_store, trend_store

log = logging.getLogger("archivist.social_report")

REPORT_MODES = ("simple", "full")

BRAND = "42 Abu Dhabi"

# The three streams, in the order the owner sketched them. The labels are the
# renamed ones the Trends page now uses — the report must speak the same
# language as the screen it came from.
STREAM_COMPETITOR = "competitor"
STREAM_NEWS = "news"
STREAM_VIRAL = "viral"
STREAM_LABELS = {
    STREAM_COMPETITOR: "Competitors / Sentiment",
    STREAM_NEWS: "Industry Signal",
    STREAM_VIRAL: "Viral Social Trends",
}

# The vocabulary NEVER shrinks; the active set does. Every name, label, builder
# and section below still exists for all three streams — `ALL_*` is the whole
# language this module can speak, and the `VIRAL_STREAM` switch decides how much
# of it is in use right now.
ALL_STREAMS = (STREAM_COMPETITOR, STREAM_NEWS, STREAM_VIRAL)

# --- THE VIRAL STREAM IS SWITCHED OFF --------------------------------------
# `GENERAL_TRENDS_ENABLED` is false (see config.py for why: the feed surfaces
# things that HAPPENED rather than formats people are RECREATING, which is a
# research problem, and the collector is paused until that research is done).
#
# The report follows the collector rather than arguing with it. With nothing
# being collected, a Viral Social Trends section could only ever report a
# freezing snapshot ageing out of the window — a number that looks measured and
# means nothing. So while the switch is off this report is a TWO-stream
# document: `scope=viral` is not offered, `Emerging Trends` is not a section,
# `kind=trend` is not an item, and the basis omits the count rather than
# printing a null that reads as "we looked and found none".
#
# Nothing here is deleted. Flip the config and every constant below widens back
# to three streams on the next boot, with no other edit.
# Everything the switch decides — STREAM_ORDER, REPORT_SCOPES, SCOPE_STREAMS,
# SCOPE_LABELS, ITEM_KINDS, FULL_SECTION_IDS — is assigned in ONE place, by
# `set_viral_stream()` below, which is called once at import. Six derivations
# scattered down the module would be six chances for one of them to be forgotten
# the day the switch flips back.
VIRAL_STREAM: bool = True   # real value assigned at the bottom of this block

# --- SCOPE: the same report, narrowed to one stream ------------------------
# The owner reads the Trends page one tab at a time, so the report has to be
# downloadable one tab at a time. A scope is a FILTER, never a different
# document: same sections, same order, same contract — but a section that
# cannot be computed from one stream comes back `available: False` with a
# reason that names the stream it needs, rather than empty (which reads as a
# bug) or padded from another stream (which would be a fabrication).
DEFAULT_SCOPE = "all"
STREAM_SCOPES = {STREAM_COMPETITOR: "competitors", STREAM_NEWS: "news",
                 STREAM_VIRAL: "viral"}

# One scope per ACTIVE stream, plus "all" — see `set_viral_stream()`. Derived
# rather than listed, so a stream can never be offered as a scope while its data
# is not being collected.
STREAM_ORDER: tuple[str, ...] = ALL_STREAMS
REPORT_SCOPES: tuple[str, ...] = ()
SCOPE_STREAMS: dict[str, tuple[str, ...]] = {}
SCOPE_LABELS: dict[str, str] = {}

# --- ITEM: one post, or one trend, as its own document ---------------------
# A `post` row is either a competitor Instagram post or a news item; the two
# live in the same table and are told apart by `source`, so the caller does not
# have to know which it is holding.
ALL_ITEM_KINDS = ("post", "trend")
VIRAL_ONLY_ITEM_KINDS = ("trend",)
# `trend` goes with the viral stream: an item report about one trend is reached
# from a trend card, and there are no trend cards while the stream is off. The
# router turns an unknown kind into a 422 naming the real ones, so a bookmarked
# `?kind=trend` link gets a straight answer rather than a report built from a
# row nothing is refreshing.
ITEM_KINDS: tuple[str, ...] = ALL_ITEM_KINDS   # narrowed by set_viral_stream()

# The evidence cap for a single trend. This mirrors
# `app/routers/general_trends.DETAIL_EVIDENCE_MAX` on purpose: the item report
# shows exactly what the detail dialog shows, from the same
# `general_trends_store.sightings_for()` query, so the two can never disagree.
ITEM_EVIDENCE_MAX = 200

# The report is deliberately a briefing, not a warehouse dump.  The old
# Meltwater-shaped version printed fourteen generic listening tables (audience,
# channels, authors, risk, and so on) next to the competitor findings.  Those
# tables made a downloaded report long without making the next content decision
# any clearer.  These are the only questions the 42 team can act on from the
# data we actually collect, in the order they should read them.
#
# The two section ids owned by the narrative pass remain stable.  This lets the
# wording improve without coupling the model pass to a particular report
# layout.
ALL_SECTION_IDS = (
    # AT A GLANCE FIRST. The numbers are what a reader came for, and they are
    # the part that takes two seconds to read; the written summary under them
    # is then read by someone who already knows the size of what it describes.
    "report_snapshot",
    "activity",
    "executive_summary",
    "competitor_conversation",
    "competitor_content",
    "industry_signals",
    "content_opportunities",
    "recommendations",
    "data_basis",
)

# No section exists solely to display a paused feed.  When viral social trends
# are unavailable, Industry Signals simply contains the current industry
# stories we did collect.
VIRAL_ONLY_SECTIONS: tuple[str, ...] = ()

FULL_SECTION_IDS: tuple[str, ...] = ALL_SECTION_IDS   # narrowed below


def set_viral_stream(enabled: bool) -> None:
    """Widen or narrow this module to include the viral stream. Idempotent.

    THE ONE MUTATION POINT for the switch, and the reason the six constants
    above are declared empty rather than derived where they sit: a reader
    flipping `GENERAL_TRENDS_ENABLED` back on needs exactly one function to
    read, and a future seventh derivation has exactly one place to go.

    Production calls this once, at import, from the config. It is also the seam
    the tests drive — the three-stream report still works and must KEEP
    working, because this is a pause, not a removal. Deleting its regression
    net would turn re-enabling the stream from a config change into a rewrite.
    """
    global VIRAL_STREAM, STREAM_ORDER, REPORT_SCOPES, SCOPE_STREAMS
    global SCOPE_LABELS, ITEM_KINDS, FULL_SECTION_IDS

    VIRAL_STREAM = bool(enabled)
    STREAM_ORDER = ALL_STREAMS if VIRAL_STREAM else tuple(
        s for s in ALL_STREAMS if s != STREAM_VIRAL)
    REPORT_SCOPES = (DEFAULT_SCOPE,) + tuple(STREAM_SCOPES[s] for s in STREAM_ORDER)
    SCOPE_STREAMS = {
        DEFAULT_SCOPE: STREAM_ORDER,
        **{STREAM_SCOPES[s]: (s,) for s in STREAM_ORDER},
    }
    SCOPE_LABELS = {
        # Counted, not hardcoded: "All three streams" over a two-stream report
        # is exactly the number-shaped untruth this module exists to prevent.
        DEFAULT_SCOPE: ("All three streams" if len(STREAM_ORDER) == 3
                        else "Both streams"),
        **{STREAM_SCOPES[s]: STREAM_LABELS[s] for s in STREAM_ORDER},
    }
    ITEM_KINDS = ALL_ITEM_KINDS if VIRAL_STREAM else tuple(
        k for k in ALL_ITEM_KINDS if k not in VIRAL_ONLY_ITEM_KINDS)
    FULL_SECTION_IDS = ALL_SECTION_IDS if VIRAL_STREAM else tuple(
        s for s in ALL_SECTION_IDS if s not in VIRAL_ONLY_SECTIONS)


set_viral_stream(config.general_trends_enabled)

# The two sections this module deliberately leaves empty: they are prose about
# the other twelve, and a model writes them AFTER the numbers exist.
NARRATIVE_SECTION_IDS = ("executive_summary", "recommendations")

# The item report's sections. Not a filter of the fourteen — a report about ONE
# post or ONE trend answers different questions, so it is its own ordered list.
# `recommendations` is the SAME id the narrative pass looks for, so a full item
# report gets a written recommendation about that item and a simple one costs no
# model call at all.
ITEM_SECTION_IDS_SIMPLE = (
    "item_overview",
    "item_what_it_is",
    "item_research",       # viral trends only; dropped for a post
    "item_basis",
)
ITEM_SECTION_IDS_FULL = (
    "item_overview",
    "item_what_it_is",
    "item_research",       # viral trends only; dropped for a post
    "item_ranking",
    "item_timeline",
    "item_ai_read",
    "item_evidence",       # viral trends only; dropped for a post
    "recommendations",
    "item_basis",
)

# Sections that only a viral trend can fill. On a post they are not rendered
# empty with an excuse — they are not part of that report at all, because an
# Instagram post has no sightings and no web-research cache to be missing.
ITEM_TREND_ONLY_SECTIONS = ("item_research", "item_evidence")

SECTION_KINDS = ("prose", "bullets", "kpis", "table", "chart", "signals")

# --- the three sentences that must never quietly change ---------------------
# Instagram's Graph API returns like_count / comments_count / view_count for
# another account's media through business_discovery. It returns no share
# count. Printing "0 shares" would read as measurement.
SHARES_LABEL = "Shares"
SHARES_VALUE = "not available (Instagram API)"

# We store captions, headlines and trend summaries. We store ZERO comment text.
# Any tone we show is therefore the model reading the publisher's own words,
# never the audience's.
SENTIMENT_DISCLAIMER = (
    "AI-assessed from post text - not measured from audience comments"
)
SENTIMENT_UNAVAILABLE = (
    "Audience sentiment is not measured. No comment text is stored anywhere in "
    "this system, so nothing here can say how people felt; and the AI tone pass "
    "over the text we do have returned nothing for this window."
)

NARRATIVE_PENDING = "written by the narrative pass"

# How many items each simple-mode group shows. The owner's sketch is a
# one-line-per-item brief, not a feed dump.
SIGNALS_PER_GROUP = 5

# Caps that keep a click-time report to one screen per section.
TOP_CONTENT_PER_STREAM = 5
EMERGING_CAP = 10
TOPICS_CAP = 12
INFLUENCERS_PER_KIND = 6
RISK_CAP = 15

# A trend counts as "accelerating" when its measured heat is at least the
# rising band (see general_trends_ranking.heat_band) OR a source reported it
# growing by at least this ratio against its own previous reading. Both halves
# are measured; neither is a guess.
RISING_HEAT = 0.45
RISING_GROWTH_RATIO = 1.2

# A day (or bucket) is called a spike when it carries at least this multiple of
# the window mean AND enough items that the multiple is not noise.
SPIKE_RATIO = 2.0
SPIKE_MIN_ITEMS = 5

# Tone pass: one batched model call, hard-capped, and every failure degrades to
# "no tone" rather than to a made-up label.
TONE_MAX_ITEMS = 60
TONE_TEXT_CHARS = 220
TONE_MAX_TOKENS = 4000  # 2.5-pro spends thinking tokens from the same pool;
                        # a small budget returns an EMPTY STRING, not an error.
# Per stream, in full mode. Fixed rather than "the batch divided by the streams
# in scope", so a one-stream report reads a third of the texts and finishes in
# roughly a third of the time. See `tone_targets`.
TONE_PER_STREAM = max(1, TONE_MAX_ITEMS // 3)
TONE_LABELS = {"positive": "Positive", "neutral": "Neutral", "negative": "Negative"}

_WS_RE = re.compile(r"\s+")


# ---------------------------------------------------------------------------
# Small formatting helpers — every value the contract ships is a string, so the
# renderer never has to guess a unit or a locale.
# ---------------------------------------------------------------------------

def _num(value: Any) -> Optional[float]:
    """Float or None. Never raises, never returns NaN/inf."""
    if value is None or isinstance(value, bool):
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(out) or math.isinf(out):
        return None
    return out


def _int(value: Any) -> Optional[int]:
    n = _num(value)
    return int(n) if n is not None else None


def _count(value: Any) -> str:
    """Thousands-separated count, or an explicit 'not recorded'."""
    n = _int(value)
    return f"{n:,}" if n is not None else "not recorded"


def _ratio(value: Any, digits: int = 1) -> Optional[str]:
    n = _num(value)
    return f"{n:.{digits}f}x" if n is not None else None


def _pct(part: Any, whole: Any, digits: int = 1) -> str:
    p, w = _num(part), _num(whole)
    if p is None or not w:
        return "n/a"
    return f"{100.0 * p / w:.{digits}f}%"


def _as_dt(value: Any) -> Optional[datetime]:
    """Coerce a stored timestamp to an aware UTC datetime."""
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, str) and value.strip():
        raw = value.strip().replace("Z", "+00:00")
        try:
            out = datetime.fromisoformat(raw)
        except ValueError:
            return None
        return out if out.tzinfo else out.replace(tzinfo=timezone.utc)
    return None


def _iso(value: Any) -> Optional[str]:
    dt = _as_dt(value)
    return dt.isoformat() if dt else None


def _day(value: Any) -> str:
    """'21 Jul 2026' — the form a sceptical reader can check against a calendar."""
    dt = _as_dt(value)
    return dt.strftime("%d %b %Y").lstrip("0") if dt else "unknown"


def _short_day(value: Any) -> str:
    dt = _as_dt(value)
    return dt.strftime("%d %b").lstrip("0") if dt else "?"


def _range(lo: Any, hi: Any) -> str:
    return f"{_day(lo)} - {_day(hi)}"


def _dl(n: int) -> str:
    """'1 day' / '30 days' — a report that says '1 days' reads as generated."""
    return f"{int(n)} day" + ("" if int(n) == 1 else "s")


def _in_window(data: "ReportData") -> str:
    """'the last 30 days', or 'the 30 days to 14 Sep' for a chosen window.

    A report about a past window that describes itself as "the last 30 days"
    is simply wrong, and wrong in the one way the reader cannot catch: the
    numbers look current. Every sentence that names the span goes through
    here."""
    if getattr(data, "chosen_end", False):
        return f"the {_dl(data.days)} to {_day(data.win_to)}"
    return f"the last {_dl(data.days)}"


def _one_line(text: Any, limit: int = 90) -> str:
    """First line of a caption/headline, whitespace collapsed, hard-truncated.

    Captions arrive with newlines and a hashtag tail; the report wants the
    sentence a human wrote, not the tag block."""
    if text is None:
        return ""
    raw = str(text).strip()
    if not raw:
        return ""
    head = raw.split("\n", 1)[0].strip() or raw
    head = _WS_RE.sub(" ", head).strip()
    if len(head) <= limit:
        return head
    return head[: max(0, limit - 3)].rstrip() + "..."


def _link_text(url: Any, limit: int = 62) -> str:
    """A URL shortened for a table CELL, not for a hyperlink.

    Google News redirect links run past 600 characters; printed whole they blow
    a PDF column apart and tell the reader nothing. The full URL always
    survives on the `link` field of the signals items, which is what a renderer
    should actually hyperlink."""
    raw = str(url or "").strip()
    if not raw:
        return "-"
    trimmed = raw.split("://", 1)[-1]
    if len(trimmed) <= limit:
        return trimmed
    return trimmed[: max(0, limit - 3)].rstrip("/") + "..."


def _jsonb_list(value: Any) -> list[str]:
    if isinstance(value, (list, tuple)):
        return [str(v) for v in value if str(v).strip()]
    return []


def _meta(row: dict) -> dict:
    md = row.get("metadata")
    return md if isinstance(md, dict) else {}


def _extra(sighting: dict) -> dict:
    ex = sighting.get("extra")
    return ex if isinstance(ex, dict) else {}


def _handle(username: Any) -> str:
    name = str(username or "").strip().lstrip("@")
    return f"@{name}" if name else "unknown account"


# ---------------------------------------------------------------------------
# Timestamps: each stream is dated by the clock that actually means "this
# happened", never by when our fetcher noticed it.
# ---------------------------------------------------------------------------

def post_time(row: dict) -> Optional[datetime]:
    """Competitor posts have a real publish time; news items fall back to when
    the collector stored them (a feed item without a date still happened)."""
    return _as_dt(row.get("posted_at")) or _as_dt(row.get("created_at"))


def trend_first_seen(row: dict) -> Optional[datetime]:
    return _as_dt(row.get("first_seen_at"))


def trend_last_seen(row: dict) -> Optional[datetime]:
    return _as_dt(row.get("last_seen_at")) or trend_first_seen(row)


def _within(when: Optional[datetime], lo: datetime, hi: datetime) -> bool:
    return when is not None and lo <= when < hi


# ---------------------------------------------------------------------------
# The dataset every section reads from
# ---------------------------------------------------------------------------

@dataclass
class ReportData:
    """Everything both report depths need, pulled once.

    The three `*_rows` lists span BOTH windows (2 x `days`); builders slice
    them with `_within` so the previous-window comparison costs no extra
    query. `coverage` records the earliest row we hold per stream, which is how
    a delta learns to say "no comparable history" instead of inventing growth
    out of the day we switched the collector on."""

    now: datetime
    days: int
    win_from: datetime
    win_to: datetime
    prev_from: datetime
    prev_to: datetime
    # True when the caller NAMED the end of the window ("the week ending
    # 14 Sep") instead of taking the trailing one. It changes nothing that is
    # measured; it decides whether a sentence may say "the last 30 days",
    # which is false of a window that ended a fortnight ago and which the
    # reader has no way to check. See `_in_window`.
    chosen_end: bool = False
    # When this document was assembled, which for a past window is NOT the end
    # of the window. `now` is the window end (every slice compares against it);
    # this is the clock. They coincide for a trailing report.
    built_at: Optional[datetime] = None
    # Which of the three streams this report covers. The row lists below only
    # ever hold in-scope rows (the loader does not fetch the others), so every
    # builder must ask `has()` before it explains an empty list — "no competitor
    # posts in this window" and "this report is not about competitor posts" are
    # different statements and only one of them is true.
    scope: str = DEFAULT_SCOPE
    competitor_rows: list[dict] = field(default_factory=list)
    news_rows: list[dict] = field(default_factory=list)
    trend_rows: list[dict] = field(default_factory=list)
    sightings: dict[int, list[dict]] = field(default_factory=dict)
    competitors: list[dict] = field(default_factory=list)
    # Always empty. Kept so a reader that has not been updated gets an empty
    # mapping rather than an AttributeError: Industry Signal has no feeds.
    news_sources: dict[str, dict] = field(default_factory=dict)
    coverage: dict[str, Any] = field(default_factory=dict)
    tone: dict[str, dict] = field(default_factory=dict)
    # Caveats discovered while LOADING (a row cap hit). They ride into
    # `basis.notes` so an undercount can never pass as a measurement.
    load_notes: list[str] = field(default_factory=list)

    # -- window slices ------------------------------------------------------
    @property
    def competitors_now(self) -> list[dict]:
        return [r for r in self.competitor_rows
                if _within(post_time(r), self.win_from, self.win_to)]

    @property
    def competitors_prev(self) -> list[dict]:
        return [r for r in self.competitor_rows
                if _within(post_time(r), self.prev_from, self.prev_to)]

    @property
    def news_now(self) -> list[dict]:
        return [r for r in self.news_rows
                if _within(post_time(r), self.win_from, self.win_to)]

    @property
    def news_prev(self) -> list[dict]:
        return [r for r in self.news_rows
                if _within(post_time(r), self.prev_from, self.prev_to)]

    @property
    def trends_now(self) -> list[dict]:
        """Trends a source still listed inside the window."""
        return [r for r in self.trend_rows
                if _within(trend_last_seen(r), self.win_from, self.win_to)]

    @property
    def trends_new(self) -> list[dict]:
        """Trends FIRST seen inside the window."""
        return [r for r in self.trend_rows
                if _within(trend_first_seen(r), self.win_from, self.win_to)]

    @property
    def trends_new_prev(self) -> list[dict]:
        return [r for r in self.trend_rows
                if _within(trend_first_seen(r), self.prev_from, self.prev_to)]

    # Every count the report PRINTS is over the rows the feed would show, so
    # Mention Volume, the Conversation Trend line and Channel Analysis all
    # reconcile. `trends_now` / `trends_new` stay raw for exactly one consumer:
    # Reputation / Risk, whose whole job is the rows that were held back.
    @property
    def trends_new_shown(self) -> list[dict]:
        return showable_trends(self.trends_new)

    @property
    def trends_new_prev_shown(self) -> list[dict]:
        return showable_trends(self.trends_new_prev)

    def sightings_for(self, trend: dict) -> list[dict]:
        return self.sightings.get(int(trend.get("id") or 0), [])

    # -- scope --------------------------------------------------------------
    @property
    def streams(self) -> tuple[str, ...]:
        """The streams this report covers, in the page's own order."""
        return SCOPE_STREAMS.get(self.scope, STREAM_ORDER)

    def has(self, stream: str) -> bool:
        return stream in self.streams

    @property
    def scope_label(self) -> str:
        return SCOPE_LABELS.get(self.scope, SCOPE_LABELS["all"])

    @property
    def scoped(self) -> bool:
        return self.scope != DEFAULT_SCOPE

    @property
    def streams_noun(self) -> str:
        """"the three streams" / "both streams" — how an UNSCOPED basis line
        names what it added together.

        Counted, never written out. The active set depends on the viral switch,
        so a hardcoded "three" survives as a number-shaped untruth on the very
        line that is supposed to tell the reader what the figure above it was
        computed from."""
        return "the three streams" if len(self.streams) == 3 else "both streams"

    def scope_sentence(self) -> str:
        """The sentence every scoped section's basis carries, so a single-stream
        number can never be read as a whole-picture number."""
        if not self.scoped:
            return ""
        missing = ", ".join(STREAM_LABELS[s] for s in STREAM_ORDER
                            if s not in self.streams)
        return (f"This report is scoped to {self.scope_label} only - "
                f"{missing} are not in it.")

    def covered(self, stream: str) -> bool:
        """True when we hold data reaching back past the previous window, i.e.
        a comparison against it is meaningful at all."""
        start = _as_dt(self.coverage.get(f"{stream}_from"))
        return start is not None and start <= self.prev_from

    def coverage_start(self, stream: str) -> Optional[datetime]:
        return _as_dt(self.coverage.get(f"{stream}_from"))


def clean_scope(scope: Any) -> str:
    """A known scope, or 'all'. The router 422s an unknown one before it gets
    here; this is the belt for every other caller (a cron, a test, a bookmark)."""
    value = str(scope or DEFAULT_SCOPE).strip().lower()
    return value if value in REPORT_SCOPES else DEFAULT_SCOPE


def clean_kind(kind: Any) -> str:
    value = str(kind or "").strip().lower()
    return value if value in ITEM_KINDS else ""


def make_window(days: int, now: Optional[datetime] = None, *,
                whole_days: bool = False) -> dict:
    """The two windows, so tests and the loader agree on the arithmetic.

    `now` is the END of the window, which is the present for a trailing report
    and a named day for a chosen one. The arithmetic is identical either way,
    which is the point: a past window needed no new maths, only a different
    end.

    `whole_days` makes the window WHOLE CALENDAR DAYS: `days` of them, ending
    with the last moment of the end day. Without it the window is a rolling
    one that ends at whatever time it is now and begins at that same time of
    day, `days` days earlier, which is what every caller had before windows
    could be chosen.

    THE DIFFERENCE IS NOT COSMETIC. A rolling window covers `days + 1` calendar
    days, the first and last of them partly, so "7 days" printed as "13 - 20
    Sep" is eight dates and a reader who picks 1 Aug to 8 Aug in a calendar
    gets seven of those eight days plus one second of the 1st. A picker that
    lets someone choose both ends has to mean exactly the days they touched.

    The stream-report routes ask for whole days (see `routers/reports._build`).
    Item reports do not: an item is RANKED against a window rather than being
    about one, and the trailing window is the right frame for "how does this
    compare with the last month" asked at this moment.
    """
    # `days or 30` would turn an explicit 0 into 30 instead of clamping it.
    d = max(1, min(90, int(30 if days is None else days)))
    chosen = _as_dt(now)
    if chosen is None and isinstance(now, date):
        # A bare date: the end DAY, with no time of day to honour. (datetime is
        # a subclass of date, so this only fires for a real date.)
        chosen = datetime.combine(now, time.max, tzinfo=timezone.utc)
    end = chosen or datetime.now(timezone.utc)

    if whole_days:
        last = end.date()
        win_to = datetime.combine(last, time.max, tzinfo=timezone.utc)
        win_from = datetime.combine(last - timedelta(days=d - 1), time.min,
                                    tzinfo=timezone.utc)
        return {
            "days": d,
            "win_from": win_from,
            "win_to": win_to,
            # The `days` whole days immediately before, so the comparison is
            # like for like: two runs of the same length, neither clipped.
            "prev_from": win_from - timedelta(days=d),
            "prev_to": win_from,
        }

    win_from = end - timedelta(days=d)
    return {
        "days": d,
        "win_from": win_from,
        "win_to": end,
        "prev_from": end - timedelta(days=2 * d),
        "prev_to": win_from,
    }


# ---------------------------------------------------------------------------
# Section construction — one helper so the contract cannot drift
# ---------------------------------------------------------------------------

def _section(
    section_id: str, title: str, *, kind: str, basis: str,
    body: Optional[dict] = None, available: bool = True,
    unavailable_reason: Optional[str] = None, estimated: bool = False,
) -> dict:
    """Build one Section, refusing to emit an unavailable section with no reason.

    That refusal is the whole point: "not enough data" without the reason is
    indistinguishable from a bug, and a reader who cannot tell the difference
    stops trusting the sections that DO have data."""
    if kind not in SECTION_KINDS:
        raise ValueError(f"unknown section kind: {kind!r}")
    if not available and not (unavailable_reason or "").strip():
        raise ValueError(f"section {section_id!r} is unavailable with no reason")
    return {
        "id": section_id,
        "title": title,
        "available": bool(available),
        "unavailable_reason": (unavailable_reason or None) if not available else None,
        "basis": basis,
        "estimated": bool(estimated),
        "kind": kind,
        "body": body if body is not None else _empty_body(kind),
    }


def _empty_body(kind: str) -> dict:
    return {
        "prose": {"paragraphs": []},
        "bullets": {"items": []},
        "kpis": {"items": []},
        "table": {"columns": [], "rows": [], "align": []},
        "chart": {"chart": "bar", "labels": [], "series": [], "y_label": ""},
        "signals": {"groups": []},
    }[kind]


def _table(columns: list[str], rows: list[list[str]], align: list[str]) -> dict:
    if len(columns) != len(align):
        raise ValueError("table align must have one entry per column")
    return {"columns": list(columns), "rows": [list(r) for r in rows],
            "align": list(align)}


def _kpi(label: str, value: str, *, delta: Optional[str] = None,
         delta_dir: Optional[str] = None, note: Optional[str] = None) -> dict:
    if delta is None:
        delta_dir = None
    return {"label": label, "value": value, "delta": delta,
            "delta_dir": delta_dir, "note": note}


def delta_kpi(label: str, now_n: int, prev_n: int, *, comparable: bool,
              window_label: str, no_history_note: str) -> dict:
    """A count with an honest delta.

    `comparable` is False when the previous window starts before the day this
    stream's collector produced its first row. In that case the delta is None —
    NOT "+385" — because the rise measures our switch-on date, not the world."""
    if not comparable:
        return _kpi(label, f"{now_n:,}", note=no_history_note)
    diff = now_n - prev_n
    direction = "up" if diff > 0 else ("down" if diff < 0 else "flat")
    sign = "+" if diff > 0 else ""
    note = f"{prev_n:,} in the previous {window_label}"
    if prev_n:
        note += f" ({_pct(diff, prev_n, 0)} change)"
    return _kpi(label, f"{now_n:,}", delta=f"{sign}{diff:,}", delta_dir=direction,
                note=note)


# ---------------------------------------------------------------------------
# Scope gating — the sections one stream cannot answer
# ---------------------------------------------------------------------------
# `section id -> (stream it needs, its title, its kind, what it actually does)`.
# Everything NOT in here is computable from any single stream (it just counts
# fewer things); everything in here is a measurement OF a particular stream, and
# on the wrong scope it must say so rather than render empty. Rendering it empty
# would read as "we watched and found nothing", which is a different — and
# false — statement.
_SCOPE_ONLY_SECTIONS: dict[str, tuple[str, str, str, str]] = {
    "competitor_conversation": (
        STREAM_COMPETITOR, "COMPETITORS - What they are talking about & tone", "table",
        "This view needs competitor Instagram posts so it can show what the "
        "watched accounts posted and the tone of that post text"),
    "competitor_content": (
        STREAM_COMPETITOR, "COMPETITORS - Content that is working", "signals",
        "This view needs competitor Instagram posts so it can show the posts "
        "that did best against each account's usual response"),
}


def scope_blocks(section_id: str, scope: str) -> bool:
    """True when this section cannot be computed at this scope."""
    rule = _SCOPE_ONLY_SECTIONS.get(section_id)
    if rule is None:
        return False
    return rule[0] not in SCOPE_STREAMS.get(scope, STREAM_ORDER)


def build_not_applicable(section_id: str, scope: str) -> dict:
    """The section, stated as not applicable — with the reason a reader needs."""
    stream, title, kind, clause = _SCOPE_ONLY_SECTIONS[section_id]
    label = SCOPE_LABELS.get(scope, SCOPE_LABELS["all"])
    return _section(
        section_id, title, kind=kind,
        basis=(f"Not applicable at this scope. {clause}, which needs "
               f"{STREAM_LABELS[stream]} rows; this report was built from "
               f"{label} alone. Nothing has been substituted from another "
               "stream, and no figure below is withheld - there is none."),
        available=False,
        unavailable_reason=f"{clause}; this report covers {label} only.",
        estimated=False,
    )


# ---------------------------------------------------------------------------
# Deterministic selectors — shared by the tone pass and the section builders so
# the items we assess are exactly the items we print.
# ---------------------------------------------------------------------------

def _sort_key(*values: Any) -> tuple:
    return tuple(v if v is not None else float("-inf") for v in values)


def top_competitor_posts(rows: Iterable[dict], n: int) -> list[dict]:
    """Best posts by OUTPERFORMANCE against that account's own median — raw
    likes are meaningless across accounts of different sizes."""
    return sorted(
        rows,
        key=lambda r: _sort_key(_num(r.get("outperformance")),
                                _num(r.get("engagement")),
                                _num(r.get("id"))),
        reverse=True,
    )[:n]


def top_news_items(rows: Iterable[dict], n: int) -> list[dict]:
    return sorted(
        rows,
        # Coverage was the tie-break until a search replaced the feed reader:
        # nothing can honestly count how many outlets ran a story any more, so
        # the id breaks ties and the order stays deterministic.
        key=lambda r: _sort_key(_num(r.get("opportunity_score")),
                                _num(r.get("id"))),
        reverse=True,
    )[:n]


def showable_trends(rows: Iterable[dict]) -> list[dict]:
    """The rows the feed itself would show: nothing the user threw away,
    nothing the ingest screen filtered, nothing the scorer said avoid."""
    out = []
    for r in rows:
        if str(r.get("status") or "new") == "dismissed":
            continue
        if str(r.get("safety") or "ok") != "ok":
            continue
        if str(r.get("sensitivity") or "ok") == "avoid":
            continue
        out.append(r)
    return out


def top_trends(rows: Iterable[dict], n: int) -> list[dict]:
    return sorted(
        showable_trends(rows),
        key=lambda r: _sort_key(_num(r.get("opportunity_score")),
                                _num(r.get("velocity")),
                                _num(r.get("id"))),
        reverse=True,
    )[:n]


# ---------------------------------------------------------------------------
# Viral-trend measurements, read out of the evidence rather than guessed
# ---------------------------------------------------------------------------

def trend_measures(trend: dict, sightings: list[dict]) -> dict:
    """`{posts, views, views_per_post, growth, top_source}` — every one lifted
    from a sighting the source itself published (TikTok's hashtag board gives
    posts/views/views_per_post/top_creators; YouTube's chart gives a rank and a
    growth ratio). Missing keys mean the source never said, and the caller must
    then say nothing."""
    posts = views = vpp = growth = None
    top_source = None
    for s in sightings:
        ex = _extra(s)
        val = _num(ex.get("posts"))
        if val is not None and (posts is None or val > posts):
            posts = val
            top_source = s.get("source") or top_source
        val = _num(ex.get("views"))
        if val is not None and (views is None or val > views):
            views = val
        val = _num(ex.get("views_per_post"))
        if val is not None and (vpp is None or val > vpp):
            vpp = val
        val = _num(ex.get("growth_ratio"))
        if val is not None and (growth is None or val > growth):
            growth = val
    # The trend row's own peak_metric carries a labelled headline number for
    # sources that publish only one counter.
    label = str(trend.get("metric_label") or "").strip().lower()
    peak = _num(trend.get("peak_metric"))
    if posts is None and peak is not None and label == "posts":
        posts = peak
    if views is None and peak is not None and label in {"views", "view"}:
        views = peak
    if vpp is None and posts and views:
        vpp = views / posts
    if top_source is None:
        srcs = _jsonb_list(trend.get("sources"))
        top_source = srcs[0] if srcs else None
    return {"posts": posts, "views": views, "views_per_post": vpp,
            "growth": growth, "top_source": top_source}


def _heat_text(trend: dict) -> Optional[str]:
    v = _num(trend.get("velocity"))
    if v is None:
        return None
    return f"{v:.2f} of 1.00 ({ranking.heat_band(v)})"


# ---------------------------------------------------------------------------
# SIMPLE MODE — the owner's sketch, exactly
# ---------------------------------------------------------------------------

def _suggestion(row: dict) -> str:
    angle = str(row.get("suggested_angle") or "").strip()
    if angle:
        return angle
    return ("No angle stored for this item - the AI scoring pass has not run "
            "on it yet.")


def _tone_of(data: ReportData, stream: str, row: dict) -> Optional[dict]:
    hit = data.tone.get(tone_key(stream, row))
    if not hit:
        return None
    return {"label": str(hit.get("label") or "Neutral"), "estimated": True}


def group_competitors(data: ReportData) -> dict:
    items = []
    for row in top_competitor_posts(data.competitors_now, SIGNALS_PER_GROUP):
        topic = _one_line(row.get("topic") or row.get("caption"), 70)
        name = _handle(row.get("username"))
        if topic:
            name = f"{name} - {topic}"
        metrics = [{"label": "Likes", "value": _count(row.get("like_count"))},
                   {"label": "Comments", "value": _count(row.get("comments_count"))}]
        if _int(row.get("view_count")) is not None:
            metrics.append({"label": "Views", "value": _count(row.get("view_count"))})
        if _int(row.get("engagement")) is not None:
            metrics.append({"label": "Engagement",
                            "value": _count(row.get("engagement"))})
        outp = _ratio(row.get("outperformance"))
        if outp:
            metrics.append({"label": "Compared with this account's usual response",
                            "value": outp})
        # The gap the owner's sketch assumed we had. Printed, never zeroed.
        metrics.append({"label": SHARES_LABEL, "value": SHARES_VALUE})
        username = str(row.get("username") or "").strip().lstrip("@")
        extra_links = ([{"label": "Profile",
                         "url": f"https://www.instagram.com/{username}/"}]
                       if username else [])
        items.append({
            "name": name,
            "link": row.get("permalink") or None,
            "extra_links": extra_links,
            "metrics": metrics,
            "sentiment": _tone_of(data, STREAM_COMPETITOR, row),
            "suggestion": _suggestion(row),
        })
    return {"group": STREAM_LABELS[STREAM_COMPETITOR], "items": items}


def group_news(data: ReportData) -> dict:
    items = []
    for row in top_news_items(data.news_now, SIGNALS_PER_GROUP):
        md = _meta(row)
        metrics = []
        outlet = str(row.get("platform") or "").strip()
        if outlet:
            metrics.append({"label": "Outlet", "value": outlet})
        # "Outlets covering it" and "Also covered by" stood here. A search
        # cannot count how many outlets ran a story, so rather than estimate it
        # the metric is gone. What replaces it is the one line that says
        # something useful about a story to a person deciding whether to post.
        # "Why it matters" stood here, 200 characters per story, and
        # `suggestion` repeated it in other words below. Eleven stories made
        # that ~4,500 characters of prose wrapped around four facts, which is
        # the wall of text this section was. The HEADLINE already says what the
        # story is; what a reader needs beside it is where it ran, when, and
        # whether it fits. The full article is one click away on `link`.
        when = post_time(row)
        if when:
            metrics.append({"label": "Published", "value": _day(when)})
        fit = _int(row.get("fit_score"))
        if fit is not None:
            metrics.append({"label": "Fit for 42 (AI judgement)", "value": f"{fit}/100"})
        # No `extra_links`: there is no feed behind a story any more, and the
        # article itself is already on `link` below.
        extra_links: list[dict] = []
        items.append({
            "name": _one_line(row.get("caption") or row.get("topic"), 120)
                    or "untitled item",
            "link": row.get("permalink") or None,
            "extra_links": extra_links,
            "metrics": metrics,
            "sentiment": _tone_of(data, STREAM_NEWS, row),
            # No `suggestion`: the ideas worth acting on are collected in
            # "Opportunities and gaps to test" and "Recommended actions", where
            # they are ranked. Repeating one under every story turned a list
            # into an essay and said the same thing twice.
            "suggestion": None,
        })
    return {"group": STREAM_LABELS[STREAM_NEWS], "items": items}


def group_viral(data: ReportData) -> dict:
    items = []
    for row in top_trends(data.trends_now, SIGNALS_PER_GROUP):
        sights = data.sightings_for(row)
        m = trend_measures(row, sights)
        metrics = []
        if m["posts"] is not None:
            metrics.append({"label": "Posts", "value": f"{int(m['posts']):,}"})
        if m["views"] is not None:
            metrics.append({"label": "Views", "value": f"{int(m['views']):,}"})
        if m["views_per_post"] is not None:
            metrics.append({"label": "Views per post",
                            "value": f"{int(round(m['views_per_post'])):,}"})
        heat = _heat_text(row)
        if heat:
            metrics.append({"label": "Heat", "value": heat})
        # "Sources" and "Evidence sightings" stood here. Both are facts about
        # OUR COLLECTION rather than about the trend, and a reader deciding
        # whether to make something cannot act on either. The platform is
        # already visible in the link.
        extra_links = []
        seen: set[str] = set()
        for s in sights:
            url = str(s.get("url") or "").strip()
            if not url or url in seen or url == str(row.get("url") or ""):
                continue
            seen.add(url)
            extra_links.append({"label": str(s.get("source") or "evidence"),
                                "url": url})
            if len(extra_links) >= 3:
                break
        items.append({
            "name": str(row.get("title") or "untitled trend"),
            "link": row.get("url") or None,
            "extra_links": extra_links,
            "metrics": metrics,
            "sentiment": _tone_of(data, STREAM_VIRAL, row),
            # No `suggestion`, for the same reason the news items carry none:
            # the ideas worth acting on are collected and RANKED in
            # "Opportunities and gaps to test", and repeating one under every
            # item turned a list into an essay.
            "suggestion": None,
        })
    return {"group": STREAM_LABELS[STREAM_VIRAL], "items": items}


_GROUP_BUILDERS = {
    STREAM_COMPETITOR: group_competitors,
    STREAM_NEWS: group_news,
    STREAM_VIRAL: group_viral,
}

# How each stream's items are picked, stated per stream so a scoped brief only
# explains the ranking it actually used.
_RANK_SENTENCES = {
    STREAM_COMPETITOR: ("Competitor items are ranked by measured outperformance "
                        "against each account's own median engagement"),
    STREAM_NEWS: "news items by the scorer's opportunity score",
    STREAM_VIRAL: "trends by opportunity score over measured heat",
}


def build_signals(data: ReportData) -> dict:
    """The whole of simple mode: one group per stream IN SCOPE, one line of
    evidence per item, one "what 42 should do next" per item."""
    groups = [_GROUP_BUILDERS[s](data) for s in data.streams]
    empty = [g["group"] for g in groups if not g["items"]]
    filled = [g for g in groups if g["items"]]
    window = _range(data.win_from, data.win_to)

    counted = []
    if data.has(STREAM_COMPETITOR):
        handles = len({r.get("username") for r in data.competitors_now
                       if r.get("username")})
        counted.append(f"{len(data.competitors_now)} competitor post(s) from "
                       f"{handles} watched Instagram account(s)")
    if data.has(STREAM_NEWS):
        counted.append(f"{len(data.news_now)} news item(s)")
    if data.has(STREAM_VIRAL):
        counted.append(f"{len(showable_trends(data.trends_now))} viral trend(s)")
    ranking_txt = "; ".join(_RANK_SENTENCES[s] for s in data.streams)

    parts = [
        ", ".join(counted) + f", {window}.",
        ranking_txt + ".",
        f"Suggestions are the AI-written angle stored with each item. "
        f"{SHARES_LABEL} are {SHARES_VALUE}.",
    ]
    if data.scoped:
        parts.append(data.scope_sentence())
    if empty:
        parts.append("No rows in this window for: " + ", ".join(empty) + ".")
    basis = " ".join(parts)
    covered = ("either of the two streams" if len(data.streams) == 2
               else ("this stream" if len(data.streams) == 1
                     else "any of the three streams"))
    if not filled:
        return _section(
            "signals", "Social-listening Signals", kind="signals", basis=basis,
            available=False,
            unavailable_reason=(
                f"Nothing was collected in {_in_window(data)} on "
                f"{covered}, so there is no signal to report."),
            estimated=True,
        )
    return _section("signals", "Social-listening Signals", kind="signals",
                    basis=basis, body={"groups": filled}, estimated=True)


# ---------------------------------------------------------------------------
# FULL MODE — concise competitor and industry briefing
# ---------------------------------------------------------------------------

def build_executive_summary(data: ReportData) -> dict:
    return _section(
        "executive_summary", "WHAT MATTERS NOW", kind="prose",
        basis=("Summarises the measured sections below; it introduces no "
               "figures of its own."),
        available=False, unavailable_reason=NARRATIVE_PENDING, estimated=True,
    )


def build_recommendations(data: ReportData) -> dict:
    return _section(
        "recommendations", "42 ABU DHABI - Recommended actions", kind="prose",
        basis=("Short actions based only on the competitor and industry findings "
               "in this report."),
        available=False, unavailable_reason=NARRATIVE_PENDING, estimated=True,
    )


def build_report_snapshot(data: ReportData) -> dict:
    """The few counts that help a reader judge the briefing at a glance.

    This replaces a catch-all activity table.  A count is useful here only when
    it tells the reader what was reviewed; source-by-source movement, outlet
    counts and account administration belong in the data notes, not at the top
    of a content decision.
    """
    items: list[dict] = []
    if data.has(STREAM_COMPETITOR):
        accounts = len({r.get("username") for r in data.competitors_now
                        if r.get("username")})
        items.append(_kpi(
            "Competitor posts reviewed", f"{len(data.competitors_now):,}",
            note=f"from {accounts} watched account(s) in this window",
        ))
    if data.has(STREAM_NEWS):
        items.append(_kpi(
            "Industry stories reviewed", f"{len(data.news_now):,}",
            note="used as context for content ideas, not as audience reaction",
        ))
    if data.has(STREAM_VIRAL):
        items.append(_kpi(
            "Emerging social formats", f"{len(showable_trends(data.trends_now)):,}",
            note="formats and trends still visible during this window",
        ))
    total = sum(int(str(item["value"]).replace(",", "") or 0) for item in items)
    return _section(
        "report_snapshot", "AT A GLANCE - What we reviewed", kind="kpis",
        basis=(f"Items collected in {_range(data.win_from, data.win_to)}. "
               "These counts describe what the report looked at, not the size "
               "of an audience."),
        body={"items": items}, available=total > 0,
        unavailable_reason=(None if total else
                            f"No competitor posts, industry stories or social "
                            f"formats were collected in {_in_window(data)}."),
        estimated=False,
    )


def _topic_label(row: dict) -> str:
    """One plain label for a row, preferring the reusable theme over a caption."""
    return (_one_line(row.get("theme"), 42)
            or _one_line(row.get("topic"), 42)
            or _one_line(row.get("caption"), 42)
            or "No topic stored")


def _tone_text(data: ReportData, rows: Iterable[dict]) -> str:
    """Tone counts for the stored post text, never for the audience."""
    counts = {label: 0 for label in TONE_LABELS.values()}
    for row in rows:
        label = str((data.tone.get(tone_key(STREAM_COMPETITOR, row)) or {})
                    .get("label") or "")
        if label in counts:
            counts[label] += 1
    seen = [f"{count} {label.lower()}" for label, count in counts.items() if count]
    return ", ".join(seen) if seen else "not assessed"


def build_competitor_conversation(data: ReportData) -> dict:
    """Keep every comparison inside the competitor set.

    The former report made one aggregate sentiment table spanning competitors,
    news and trends.  That looked complete but did not answer the question the
    team has: *what are these accounts saying, and how are they saying it?*
    """
    by_user: dict[str, list[dict]] = {}
    for row in data.competitors_now:
        user = str(row.get("username") or "").strip()
        if user:
            by_user.setdefault(user, []).append(row)

    rows: list[list[str]] = []
    for user, posts in sorted(by_user.items(), key=lambda entry: (-len(entry[1]), entry[0])):
        topics: list[str] = []
        for post in top_competitor_posts(posts, SIGNALS_PER_GROUP):
            topic = _topic_label(post)
            if topic not in topics:
                topics.append(topic)
        strongest = top_competitor_posts(posts, 1)
        best = strongest[0] if strongest else {}
        best_note = _ratio(best.get("outperformance"))
        if best_note:
            best_note += " usual response"
        else:
            best_note = _count(best.get("engagement")) + " interactions"
        rows.append([
            _handle(user),
            f"{len(posts):,}",
            "; ".join(topics[:3]),
            _tone_text(data, posts),
            best_note,
        ])

    return _section(
        "competitor_conversation",
        "COMPETITORS - What they are talking about & tone",
        kind="table",
        basis=("Topics come from the strongest posts from each watched account. "
               "Tone is an AI read of the words in those post captions; it is "
               "not audience sentiment and does not use comments."),
        body=_table(
            ["Account", "Posts", "Main topics", "Tone of post text", "Strongest post"],
            rows, ["left", "right", "left", "left", "right"],
        ),
        available=bool(rows),
        unavailable_reason=(None if rows else
                            "No competitor posts were collected in this window."),
        estimated=True,
    )


def build_competitor_content(data: ReportData) -> dict:
    """The competitor examples worth studying, with no unrelated source mixed in."""
    group = group_competitors(data)
    group["group"] = "Posts that did better than usual"
    items = group["items"]
    return _section(
        "competitor_content", "COMPETITORS - Content that is working",
        kind="signals",
        basis=("Up to five competitor posts that did best against their own "
               "account's usual response. The idea under each post is an AI "
               "suggestion for a 42 Abu Dhabi version, not a copy instruction."),
        body={"groups": [group]}, available=bool(items),
        unavailable_reason=(None if items else
                            "No competitor posts were collected in this window."),
        estimated=True,
    )


def build_industry_signals(data: ReportData) -> dict:
    """Industry context and emerging social formats, kept separate from competitors."""
    groups: list[dict] = []
    if data.has(STREAM_NEWS):
        news = group_news(data)
        news["group"] = "Current industry stories"
        if news["items"]:
            groups.append(news)
    if data.has(STREAM_VIRAL):
        viral = group_viral(data)
        viral["group"] = "Emerging social formats"
        if viral["items"]:
            groups.append(viral)

    if not (data.has(STREAM_NEWS) or data.has(STREAM_VIRAL)):
        return _section(
            "industry_signals", "INDUSTRY SIGNALS - Topics and formats to watch",
            kind="signals",
            basis=("This part of the report uses industry stories and emerging "
                   "social formats. Those sources are not included in this scope."),
            available=False,
            unavailable_reason=("Industry stories and emerging social formats are "
                                "not included in a competitors-only report."),
            estimated=False,
        )

    return _section(
        "industry_signals", "INDUSTRY SIGNALS - Topics and formats to watch",
        kind="signals",
        basis=("Industry stories provide context for what is changing. Emerging "
               "formats are social trends still visible in this window. These "
               "are prompts to consider, not evidence that an audience liked a "
               "post."),
        body={"groups": groups}, available=bool(groups),
        unavailable_reason=(None if groups else
                            "No industry stories or emerging social formats were "
                            "collected in this window."),
        estimated=True,
    )


def _opportunity_item(prefix: str, row: dict, *, source: str) -> Optional[str]:
    """A short, evidence-led experiment.  No angle means no invented advice."""
    angle = _one_line(row.get("suggested_angle"), 180)
    if not angle:
        return None
    topic = _topic_label(row)
    if source == STREAM_COMPETITOR:
        account = _handle(row.get("username"))
        proof = _ratio(row.get("outperformance"))
        proof = (f" It did {proof} better than that account's usual response."
                 if proof else "")
        return f"{prefix}: test a 42 Abu Dhabi version of {topic}. {angle} {account}.{proof}"
    if source == STREAM_NEWS:
        outlet = _one_line(row.get("platform"), 40) or "an industry source"
        return f"{prefix}: connect {topic} to student work or campus life. {angle} Source: {outlet}."
    return f"{prefix}: adapt the {topic} format around real student work. {angle}"


def build_content_opportunities(data: ReportData) -> dict:
    """Make the uncertainty around "gaps" explicit instead of pretending to know 42's feed."""
    items = [
        "These are opportunities to test, not confirmed gaps: this report does "
        "not track what 42 Abu Dhabi has already posted.",
    ]
    used: set[str] = set()

    candidates: list[tuple[str, dict, str]] = []
    if data.has(STREAM_COMPETITOR):
        candidates += [("From a competitor post", row, STREAM_COMPETITOR)
                       for row in top_competitor_posts(data.competitors_now, 3)]
    if data.has(STREAM_NEWS):
        candidates += [("From an industry signal", row, STREAM_NEWS)
                       for row in top_news_items(data.news_now, 2)]
    if data.has(STREAM_VIRAL):
        candidates += [("From an emerging format", row, STREAM_VIRAL)
                       for row in top_trends(data.trends_now, 2)]

    for prefix, row, source in candidates:
        key = _topic_label(row).lower()
        line = _opportunity_item(prefix, row, source=source)
        if not line or key in used:
            continue
        used.add(key)
        items.append(line)
        if len(items) >= 5:
            break

    # The first item is the important caveat, not a finding.  A section with
    # only that line would look like it discovered a gap when it discovered no
    # usable opportunity at all.
    available = len(items) > 1
    return _section(
        "content_opportunities", "42 ABU DHABI - Opportunities and gaps to test",
        kind="bullets",
        basis=("Ideas come from the strongest competitor posts, relevant industry "
               "stories and emerging social formats in this report. Each proposed "
               "angle is an AI suggestion and should be reviewed before use."),
        body={"items": items if available else []}, available=available,
        unavailable_reason=(None if available else
                            "The collected items do not yet include a usable content "
                            "angle for 42 Abu Dhabi."),
        estimated=True,
    )


def build_mention_volume(data: ReportData) -> dict:
    window_label = _dl(data.days)
    comp_now, comp_prev = len(data.competitors_now), len(data.competitors_prev)
    news_now, news_prev = len(data.news_now), len(data.news_prev)
    tr_now, tr_prev = len(data.trends_new_shown), len(data.trends_new_prev_shown)
    held_back = len(data.trends_new) - tr_now

    def _no_history(stream: str) -> str:
        start = data.coverage_start(stream)
        return ("no comparable history - collection started "
                + (_day(start) if start else "after the previous window"))

    items: list[dict] = []
    if data.has(STREAM_COMPETITOR):
        items.append(delta_kpi("Competitor posts", comp_now, comp_prev,
                               comparable=data.covered(STREAM_COMPETITOR),
                               window_label=window_label,
                               no_history_note=_no_history(STREAM_COMPETITOR)))
    if data.has(STREAM_NEWS):
        items.append(delta_kpi("Industry news items", news_now, news_prev,
                               comparable=data.covered(STREAM_NEWS),
                               window_label=window_label,
                               no_history_note=_no_history(STREAM_NEWS)))
    if data.has(STREAM_VIRAL):
        viral_kpi = delta_kpi("Viral trends first seen", tr_now, tr_prev,
                              comparable=data.covered(STREAM_VIRAL),
                              window_label=window_label,
                              no_history_note=_no_history(STREAM_VIRAL))
        if held_back:
            note = viral_kpi["note"] or ""
            viral_kpi["note"] = (
                (note + "; " if note else "")
                + f"a further {held_back:,} trend(s) were collected and held back "
                  "by the safety screen or the scorer's 'avoid' verdict (see "
                  "Reputation / Risk)")
        items.append(viral_kpi)
    total_now = comp_now + news_now + tr_now
    items.append(_kpi(
        "All items", f"{total_now:,}",
        note=(f"{data.streams_noun} added together" if not data.scoped
              else f"everything in scope ({data.scope_label})")))

    if data.has(STREAM_COMPETITOR):
        watched = [c for c in data.competitors
                   if str(c.get("status") or "active") != "excluded"]
        with_data = len({r.get("username") for r in data.competitors_now
                         if r.get("username")})
        never = len([c for c in watched if not c.get("last_scanned_at")])
        note = f"{with_data} returned a post in this window"
        if never:
            note += f"; {never} has never been scanned"
        items.append(_kpi("Watched Instagram accounts", f"{len(watched):,}",
                          note=note))
    if data.has(STREAM_NEWS):
        outlets = len({str(r.get("platform") or "").strip()
                       for r in data.news_now
                       if str(r.get("platform") or "").strip()})
        items.append(_kpi("News outlet feeds seen", f"{outlets:,}",
                          note="distinct outlet tags on this window's items"))
    if data.scoped and data.has(STREAM_VIRAL):
        sources: set[str] = set()
        for row in showable_trends(data.trends_now):
            sources.update(_jsonb_list(row.get("sources")))
        items.append(_kpi("Public trend sources seen", f"{len(sources):,}",
                          note="distinct sources behind this window's trends"))

    clocks = {
        STREAM_COMPETITOR: "competitor posts by publish time",
        STREAM_NEWS: ("news items by publish time (falling back to collection "
                      "time when a feed gives none)"),
        STREAM_VIRAL: "viral trends by when we first saw them",
    }
    available = bool(total_now)
    return _section(
        "mention_volume", "Mention Volume", kind="kpis",
        basis=(f"Counted from stored rows, {_range(data.win_from, data.win_to)}: "
               + ", ".join(clocks[s] for s in data.streams)
               + ". Compared against "
               f"{_range(data.prev_from, data.prev_to)}; where a stream's "
               "collection began after that window the delta is withheld rather "
               "than reported as growth."
               + (" " + data.scope_sentence() if data.scoped else "")),
        body={"items": items}, available=available,
        unavailable_reason=(None if available else
                            f"No items of any kind in {_in_window(data)}."),
        estimated=False,
    )


def _bucket_plan(data: ReportData) -> tuple[int, int]:
    """(bucket_days, bucket_count) — at most ~30 columns so a 90-day report is
    still a chart rather than a barcode."""
    bucket_days = max(1, math.ceil(data.days / 30))
    return bucket_days, max(1, math.ceil(data.days / bucket_days))


def _bucket_index(when: Optional[datetime], data: ReportData,
                  bucket_days: int, count: int) -> Optional[int]:
    if when is None:
        return None
    delta = (when - data.win_from).total_seconds() / 86400.0
    if delta < 0:
        return None
    idx = int(delta // bucket_days)
    return idx if 0 <= idx < count else None


def build_conversation_trend(data: ReportData) -> dict:
    bucket_days, count = _bucket_plan(data)
    labels = [_short_day(data.win_from + timedelta(days=i * bucket_days))
              for i in range(count)]
    all_series = {
        STREAM_COMPETITOR: (STREAM_LABELS[STREAM_COMPETITOR],
                            data.competitors_now, post_time),
        STREAM_NEWS: (STREAM_LABELS[STREAM_NEWS], data.news_now, post_time),
        STREAM_VIRAL: (STREAM_LABELS[STREAM_VIRAL], data.trends_new_shown,
                       trend_first_seen),
    }
    series_rows = tuple(all_series[s] for s in data.streams)
    series = []
    total = 0
    for name, rows, clock in series_rows:
        values = [0.0] * count
        for row in rows:
            idx = _bucket_index(clock(row), data, bucket_days, count)
            if idx is not None:
                values[idx] += 1.0
                total += 1
        series.append({"name": name, "values": values})

    span = ("per day" if bucket_days == 1 else f"per {bucket_days}-day bucket")
    return _section(
        "activity", "ACTIVITY - What was collected, day by day", kind="chart",
        basis=(f"Item counts {span} over {_range(data.win_from, data.win_to)}, "
               "from stored publish times."
               + (" The Viral Social Trends line plots when a trend was FIRST "
                  "COLLECTED, not when it started on the internet - the "
                  "collector cannot see back past its own first run."
                  if data.has(STREAM_VIRAL) else "")
               + (" " + data.scope_sentence() if data.scoped else "")),
        body={"chart": "line", "labels": labels, "series": series,
              "y_label": "items"},
        available=total > 0,
        unavailable_reason=(None if total else
                            "No dated items fall inside the window."),
        estimated=False,
    )


def tone_key(stream: str, row: dict) -> str:
    return f"{stream}:{int(row.get('id') or 0)}"


def _stream_rows(data: ReportData) -> tuple[tuple[str, list[dict]], ...]:
    """`(stream, the rows the report shows for it)` for every stream IN SCOPE.
    One definition, so Sentiment, Top Topics and the tone pass never disagree
    about what population a scoped report is counting."""
    everything = {
        STREAM_COMPETITOR: data.competitors_now,
        STREAM_NEWS: data.news_now,
        STREAM_VIRAL: showable_trends(data.trends_now),
    }
    return tuple((s, everything[s]) for s in data.streams)


def build_sentiment(data: ReportData) -> dict:
    streams = _stream_rows(data)
    rows: list[list[str]] = []
    totals = {"Positive": 0, "Neutral": 0, "Negative": 0}
    assessed = 0
    population = 0
    for stream, items in streams:
        counts = {"Positive": 0, "Neutral": 0, "Negative": 0}
        for row in items:
            hit = data.tone.get(tone_key(stream, row))
            label = str((hit or {}).get("label") or "")
            if label in counts:
                counts[label] += 1
        n = len(items)
        population += n
        got = sum(counts.values())
        assessed += got
        for key in totals:
            totals[key] += counts[key]
        rows.append([STREAM_LABELS[stream], f"{counts['Positive']:,}",
                     f"{counts['Neutral']:,}", f"{counts['Negative']:,}",
                     f"{n - got:,}", f"{n:,}"])
    if len(streams) > 1:
        # With one stream in scope the totals row would repeat the row above it
        # verbatim, which invites a reader to add them together.
        rows.append(["All streams", f"{totals['Positive']:,}",
                     f"{totals['Neutral']:,}", f"{totals['Negative']:,}",
                     f"{population - assessed:,}", f"{population:,}"])

    body = _table(
        ["Stream", "Positive", "Neutral", "Negative", "Not assessed", "Items"],
        rows, ["left", "right", "right", "right", "right", "right"])
    available = assessed > 0
    return _section(
        "sentiment", "Sentiment Analysis", kind="table",
        basis=(f"{assessed} of {population} item(s) in the window classified by "
               "a model reading the text WE store - the caption, the headline, "
               "the trend summary. Those items are NOT a random sample: they "
               "are the top-ranked items of each stream (competitor posts by "
               "outperformance, news and trends by the scorer's opportunity "
               "score), so they lean towards the strongest performers and the "
               "tone below leans with them. Read it as the tone of the items "
               f"this report puts in front of you, not of the window. "
               f"{SENTIMENT_DISCLAIMER}: no comment text is "
               "collected anywhere in this system, so none of these counts "
               "describe how an audience reacted. Counts are items, never "
               "percentages of a conversation."
               + (" " + data.scope_sentence() if data.scoped else "")),
        body=body, available=available,
        unavailable_reason=(None if available else SENTIMENT_UNAVAILABLE),
        estimated=True,
    )


def rising_trends(data: ReportData) -> list[dict]:
    """First seen inside the window AND accelerating on a measured signal."""
    out = []
    for row in showable_trends(data.trends_new):
        heat = _num(row.get("velocity")) or 0.0
        growth = trend_measures(row, data.sightings_for(row))["growth"]
        if heat >= RISING_HEAT or (growth is not None and growth >= RISING_GROWTH_RATIO):
            out.append(row)
    return sorted(out, key=lambda r: _sort_key(_num(r.get("velocity")),
                                               _num(r.get("opportunity_score")),
                                               _num(r.get("id"))), reverse=True)


def build_emerging_trends(data: ReportData) -> dict:
    rising = rising_trends(data)
    picked = rising[:EMERGING_CAP]
    rows = []
    for row in picked:
        m = trend_measures(row, data.sightings_for(row))
        growth = _ratio(m["growth"], 2) if m["growth"] is not None else "not reported"
        # The regions the SOURCE reported this trend in. This column exists
        # because the report is read in Abu Dhabi and the first question asked
        # of any trend is "is it live here?". Without it the answer had to be
        # inferred, and on the first live run the narrative pass did exactly
        # that: it read the aggregate "AE - 25 trends" out of Audience Insights
        # and wrote that two named sounds were "trending in the AE region" when
        # the stored regions on both were ["SA"]. A measured column per trend
        # makes that claim checkable instead of guessable.
        regions = ", ".join(_jsonb_list(row.get("regions"))) or "not reported"
        rows.append([
            _one_line(row.get("title"), 46) or "untitled trend",
            str(row.get("trend_kind") or "unclassified"),
            _heat_text(row) or "not scored",
            growth,
            regions,
            f"{_int(row.get('source_count')) or 0:,} / {_int(row.get('evidence_count')) or 0:,}",
            _short_day(trend_first_seen(row)),
        ])
    body = _table(
        ["Trend", "Type (AI)", "Heat", "Growth reported by source",
         "Seen in", "Sources / evidence", "First seen"],
        rows, ["left", "left", "left", "right", "left", "right", "right"])
    total_new = len(showable_trends(data.trends_new))
    return _section(
        "emerging_trends", "Emerging Trends", kind="table",
        basis=(f"{len(rising)} of {total_new} trend(s) first seen "
               f"in {_range(data.win_from, data.win_to)} are accelerating: heat "
               f"at or above {RISING_HEAT:.2f} on the measured 0-1 scale, or a "
               f"source reporting growth of {RISING_GROWTH_RATIO:.1f}x or more "
               "against its own previous reading. Heat and growth are measured "
               "from each source's counters, rank and content age; the Type "
               "column is an AI label. 'Seen in' lists the region codes the "
               "source itself reported the trend in - it is the only "
               "geography we hold, and a trend absent from that list was not "
               "observed there rather than confirmed absent."),
        body=body, available=bool(rows),
        unavailable_reason=(None if rows else (
            f"{total_new} trend(s) were first seen in this window and none of "
            f"them cleared the acceleration bar (heat >= {RISING_HEAT:.2f} or a "
            f"source-reported growth of {RISING_GROWTH_RATIO:.1f}x)."
            if total_new else
            "No new viral trends were collected in this window.")),
        estimated=True,
    )


def _theme_of(row: dict) -> Optional[str]:
    theme = str(row.get("theme") or "").strip()
    return theme.lower() if theme else None


def build_top_topics(data: ReportData) -> dict:
    counts: dict[str, dict[str, int]] = {}
    labels: dict[str, str] = {}
    streams = _stream_rows(data)
    tagged = 0
    population = 0
    for stream, rows in streams:
        population += len(rows)
        for row in rows:
            theme = _theme_of(row)
            if not theme:
                continue
            tagged += 1
            labels.setdefault(theme, str(row.get("theme")).strip())
            bucket = counts.setdefault(theme, {s: 0 for s in STREAM_ORDER})
            bucket[stream] += 1
    ordered = sorted(counts.items(),
                     key=lambda kv: (-sum(kv[1].values()), kv[0]))[:TOPICS_CAP]
    # One column per stream IN SCOPE. A "Viral trends" column of nothing but
    # zeroes on a competitors-only report is a measurement of nothing.
    stream_cols = {STREAM_COMPETITOR: "Competitor posts",
                   STREAM_NEWS: "News items", STREAM_VIRAL: "Viral trends"}
    cols = [stream_cols[s] for s in data.streams]
    multi = len(data.streams) > 1
    rows_out = [[labels[key]]
                + [f"{val[s]:,}" for s in data.streams]
                + ([f"{sum(val[s] for s in data.streams):,}"] if multi else [])
                for key, val in ordered]
    body = _table(["Theme (AI label)"] + cols + (["Total"] if multi else []),
                  rows_out,
                  ["left"] + ["right"] * len(cols) + (["right"] if multi else []))
    scorers = (f"{data.streams_noun} were labelled by separate scoring passes"
               if not data.scoped else
               f"the labels here come from the {data.scope_label} scoring pass")
    return _section(
        "top_topics", "Top Topics & Themes", kind="table",
        basis=(f"{tagged} of {population} item(s) in the window carry a theme; "
               f"{len(counts)} distinct theme(s) appear, of which the top "
               f"{len(ordered)} are shown. Theme is a label the AI scorer "
               f"assigns to each item - it is a grouping, not a measurement, and "
               f"{scorers}."
               + (" " + data.scope_sentence() if data.scoped else "")),
        body=body, available=bool(rows_out),
        unavailable_reason=(None if rows_out else
                            "No item in this window has been given a theme by "
                            "the scoring pass."),
        estimated=True,
    )


def build_share_of_voice(data: ReportData) -> dict:
    rows_by_user: dict[str, dict[str, float]] = {}
    for row in data.competitors_now:
        user = str(row.get("username") or "").strip()
        if not user:
            continue
        agg = rows_by_user.setdefault(user, {"posts": 0, "likes": 0, "comments": 0,
                                             "engagement": 0})
        agg["posts"] += 1
        agg["likes"] += _int(row.get("like_count")) or 0
        agg["comments"] += _int(row.get("comments_count")) or 0
        agg["engagement"] += _int(row.get("engagement")) or 0
    total = sum(a["engagement"] for a in rows_by_user.values())
    ordered = sorted(rows_by_user.items(), key=lambda kv: -kv[1]["engagement"])
    rows = [[_handle(user), f"{int(a['posts']):,}", f"{int(a['likes']):,}",
             f"{int(a['comments']):,}", f"{int(a['engagement']):,}",
             _pct(a["engagement"], total)]
            for user, a in ordered]
    body = _table(["Account", "Posts", "Likes", "Comments", "Engagement",
                   "Share of engagement"],
                  rows, ["left", "right", "right", "right", "right", "right"])
    n = len(rows_by_user)
    caveat = (f"Across {n} account(s) this is a split between publishers, not a "
              "market share")
    if n < 3:
        caveat += " - with so few accounts one post moves it double digits"
    return _section(
        "share_of_voice", "Share of Voice", kind="table",
        basis=(f"Measured engagement (likes + comments) on {len(data.competitors_now)} "
               f"post(s) from {n} Instagram account(s), "
               f"{_range(data.win_from, data.win_to)}. {caveat}. 42 Abu Dhabi's "
               "own account is not in this comparison: the report watches other "
               "accounts, so the share is among competitors only."),
        body=body, available=bool(rows),
        unavailable_reason=(None if rows else
                            "No competitor posts with engagement in this window, "
                            "so there is no voice to share out."),
        estimated=False,
    )


def build_competitive_intelligence(data: ReportData) -> dict:
    by_user: dict[str, list[dict]] = {}
    for row in data.competitors_now:
        user = str(row.get("username") or "").strip()
        if user:
            by_user.setdefault(user, []).append(row)
    watched = [c for c in data.competitors
               if str(c.get("status") or "active") != "excluded"]
    known = {str(c.get("username") or "").strip(): c for c in watched}
    for user in by_user:
        known.setdefault(user, {"username": user})

    rows = []
    for user, comp in sorted(known.items(),
                             key=lambda kv: -(len(by_user.get(kv[0], [])))):
        posts = by_user.get(user, [])
        best = top_competitor_posts(posts, 1)
        if posts:
            best_txt = (_ratio(best[0].get("outperformance")) or
                        _count(best[0].get("engagement")))
            attention = _one_line(best[0].get("topic") or best[0].get("caption"), 60) \
                or "no topic stored"
        else:
            best_txt = "-"
            attention = ("never scanned" if not comp.get("last_scanned_at")
                         else "no posts in this window")
        rows.append([
            _handle(user),
            _count(comp.get("followers_count")),
            f"{len(posts):,}",
            _count(comp.get("median_engagement")),
            best_txt,
            attention,
        ])
    body = _table(["Account", "Followers", "Posts in window",
                   "Median engagement (all scanned posts)",
                   "Best post vs own median", "Got attention for"],
                  rows, ["left", "right", "right", "right", "right", "left"])
    last_scan = _as_dt(data.coverage.get("last_scan_at"))
    scan_note = (f" Last scan {_day(last_scan)}; anything published after that "
                 "is not in this report." if last_scan else "")
    return _section(
        "competitive_intelligence", "Competitive Intelligence", kind="table",
        basis=(f"{len(watched)} watched Instagram account(s); followers and "
               "median engagement come from the Instagram Business Discovery "
               "profile, posts and outperformance from the stored media rows. "
               "'Got attention for' is the AI scorer's one-line read of the "
               "best post - or, where the scorer has not run on that post, the "
               "post's own first line. Either way it is a description, not a "
               f"measurement.{scan_note}"),
        body=body, available=bool(rows),
        unavailable_reason=(None if rows else
                            "No Instagram accounts are being watched, so there "
                            "is nothing to compare."),
        estimated=True,
    )


def build_audience_insights(data: ReportData) -> dict:
    rows: list[list[str]] = []

    def _tally(pairs: Iterable[str]) -> list[tuple[str, int]]:
        out: dict[str, int] = {}
        for key in pairs:
            key = str(key or "").strip()
            if key:
                out[key] = out.get(key, 0) + 1
        return sorted(out.items(), key=lambda kv: (-kv[1], kv[0]))

    holdings: list[str] = []
    if data.has(STREAM_NEWS):
        holdings.append("the region and language tags stored on each news item")
        for value, n in _tally(r.get("region") for r in data.news_now):
            rows.append(["Region tag (Industry Signal)", value, f"{n:,} items"])
        for value, n in _tally(r.get("language") for r in data.news_now):
            rows.append(["Language tag (Industry Signal)", value, f"{n:,} items"])
    if data.has(STREAM_VIRAL):
        holdings.append("the regions each viral trend was seen in")
        trend_regions: list[str] = []
        for row in showable_trends(data.trends_now):
            trend_regions.extend(_jsonb_list(row.get("regions")))
        for value, n in _tally(trend_regions)[:8]:
            rows.append(["Region seen in (Viral Social Trends)", value,
                         f"{n:,} trends"])
    if data.has(STREAM_COMPETITOR):
        holdings.append("the follower counts of the accounts we watch")
        for comp in sorted(data.competitors,
                           key=lambda c: -(_int(c.get("followers_count")) or 0)):
            followers = _int(comp.get("followers_count"))
            if followers is None:
                continue
            rows.append(["Reach of a watched account",
                         _handle(comp.get("username")), f"{followers:,} followers"])

    body = _table(["Dimension", "Value", "Count"], rows,
                  ["left", "left", "right"])
    return _section(
        "audience_insights", "Audience Insights", kind="table",
        basis=("What we actually hold: " + ", ".join(holdings) +
               ". We hold NO audience "
               "demographics - no age, gender, location or interest data for "
               "the people who engaged - because nothing we collect returns "
               "them. Follower count is a size of audience, not a description "
               "of one."
               + (" " + data.scope_sentence() if data.scoped else "")),
        body=body, available=bool(rows),
        unavailable_reason=(None if rows else
                            "No region tags, language tags or follower counts "
                            "are stored for anything in this window."),
        estimated=False,
    )


def build_top_content(data: ReportData) -> dict:
    rows: list[list[str]] = []
    ranks: list[str] = []
    if data.has(STREAM_COMPETITOR):
        ranks.append("Competitor posts ranked by measured engagement (likes + "
                     "comments) with their outperformance against that "
                     "account's own median")
    if data.has(STREAM_VIRAL):
        ranks.append("viral trends by the view count their own source published")
    if data.has(STREAM_NEWS):
        ranks.append("news items by how many outlets carried the story - news "
                     "items have no engagement metric of their own, so they "
                     "are not comparable with the other blocks and are not "
                     "ranked against them")
    by_engagement = sorted(
        data.competitors_now,
        key=lambda r: _sort_key(_num(r.get("engagement")),
                                _num(r.get("outperformance")), _num(r.get("id"))),
        reverse=True)[:TOP_CONTENT_PER_STREAM]
    for row in by_engagement:
        measured = _count(row.get("engagement")) + " engagement"
        outp = _ratio(row.get("outperformance"))
        if outp:
            measured += f" ({outp} own median)"
        rows.append([
            _one_line(row.get("caption") or row.get("topic"), 58) or "untitled post",
            f"{STREAM_LABELS[STREAM_COMPETITOR]} - {_handle(row.get('username'))}",
            measured,
            _link_text(row.get("permalink")),
        ])

    trend_rows = []
    for row in showable_trends(data.trends_now):
        m = trend_measures(row, data.sightings_for(row))
        trend_rows.append((m["views"] if m["views"] is not None else -1.0, row, m))
    for _, row, m in sorted(trend_rows, key=lambda t: -t[0])[:TOP_CONTENT_PER_STREAM]:
        bits = []
        if m["views"] is not None:
            bits.append(f"{int(m['views']):,} views")
        if m["posts"] is not None:
            bits.append(f"{int(m['posts']):,} posts")
        if not bits:
            bits.append("no counter published by the source")
        where = m["top_source"] or ", ".join(_jsonb_list(row.get("sources"))[:2]) or "-"
        rows.append([
            _one_line(row.get("title"), 58) or "untitled trend",
            f"{STREAM_LABELS[STREAM_VIRAL]} - {where}",
            " / ".join(bits),
            _link_text(row.get("url")),
        ])

    news_ranked = sorted(
        data.news_now,
        key=lambda r: _sort_key(_num(r.get("opportunity_score")), _num(r.get("id"))),
        reverse=True)[:TOP_CONTENT_PER_STREAM]
    for row in news_ranked:
        # There is nothing MEASURED about an Industry Signal story any more: no
        # engagement, no outlet count. The honest column is when it ran, and the
        # relevance beside it is labelled as the judgement it is.
        when = post_time(row)
        fit = _int(row.get("fit_score"))
        measured = (_day(when) if when else "undated")
        if fit is not None:
            measured += f" - {fit}/100 judged"
        rows.append([
            _one_line(row.get("caption"), 58) or "untitled item",
            f"{STREAM_LABELS[STREAM_NEWS]} - {row.get('platform') or 'unknown outlet'}",
            measured,
            _link_text(row.get("permalink")),
        ])

    body = _table(["Item", "Stream", "Measured", "Link"], rows,
                  ["left", "left", "right", "left"])
    return _section(
        "top_content", "Top Content", kind="table",
        basis=("; ".join(ranks) + ". Links are shortened to fit the column; "
               "the full URL travels on the signals items."
               + (" " + data.scope_sentence() if data.scoped else "")),
        body=body, available=bool(rows),
        unavailable_reason=(None if rows else
                            "Nothing was collected in this window on any stream."),
        estimated=False,
    )


def build_influencers(data: ReportData) -> dict:
    rows: list[list[str]] = []

    # TikTok publishes a top-creator list per hashtag, with follower counts.
    creators: dict[str, dict] = {}
    for trend in showable_trends(data.trends_now):
        for s in data.sightings_for(trend):
            ex = _extra(s)
            names = [str(n) for n in (ex.get("top_creators") or []) if str(n).strip()]
            follows = [_num(f) for f in (ex.get("creator_followers") or [])]
            for i, name in enumerate(names):
                followers = follows[i] if i < len(follows) else None
                cur = creators.setdefault(name, {"followers": followers,
                                                 "where": str(s.get("source") or ""),
                                                 "trend": trend.get("title")})
                if followers is not None and (cur["followers"] or 0) < followers:
                    cur["followers"] = followers
    for name, info in sorted(creators.items(),
                             key=lambda kv: -(kv[1]["followers"] or 0)
                             )[:INFLUENCERS_PER_KIND]:
        basis = (f"{int(info['followers']):,} followers"
                 if info["followers"] is not None else "follower count not published")
        basis += f"; listed as a top creator on {_one_line(info['trend'], 34)}"
        rows.append([f"@{name}", info["where"] or "TikTok", basis])

    outlets: dict[str, int] = {}
    for row in data.news_now:
        name = str(row.get("platform") or "").strip()
        if name:
            outlets[name] = outlets.get(name, 0) + 1
    for name, n in sorted(outlets.items(),
                          key=lambda kv: (-kv[1], kv[0]))[:INFLUENCERS_PER_KIND]:
        rows.append([name, "News feed", f"{n:,} item(s) in this window"])

    for comp in sorted(data.competitors,
                       key=lambda c: -(_int(c.get("followers_count")) or 0)):
        followers = _int(comp.get("followers_count"))
        rows.append([_handle(comp.get("username")), "Instagram (watched)",
                     f"{followers:,} followers" if followers is not None
                     else "follower count not stored"])

    sources_txt = []
    if data.has(STREAM_VIRAL):
        sources_txt.append("TikTok's own top-creator field on each hashtag")
    if data.has(STREAM_NEWS):
        sources_txt.append("the outlet tag on each news item")
    if data.has(STREAM_COMPETITOR):
        sources_txt.append("the Instagram handles we watch")
    body = _table(["Name", "Channel", "Basis"], rows, ["left", "left", "left"])
    return _section(
        "influencers", "Influencers / Authors", kind="table",
        basis=("Names exactly as the sources give them: "
               + ", ".join(sources_txt) +
               ". Follower counts are "
               "the ones the source published. No influence, authority or "
               "reach score is computed - nothing we collect would support one."
               + (" " + data.scope_sentence() if data.scoped else "")),
        body=body, available=bool(rows),
        unavailable_reason=(None if rows else
                            "No creator names, outlet tags or watched handles "
                            "are attached to anything in this window."),
        estimated=False,
    )


def build_channel_analysis(data: ReportData) -> dict:
    trends = showable_trends(data.trends_now)
    total = len(data.competitors_now) + len(data.news_now) + len(trends)
    rows: list[list[str]] = []
    if data.competitors_now:
        rows.append(["Instagram", STREAM_LABELS[STREAM_COMPETITOR],
                     f"{len(data.competitors_now):,}",
                     _pct(len(data.competitors_now), total)])
    if data.news_now:
        rows.append(["News + publisher feeds", STREAM_LABELS[STREAM_NEWS],
                     f"{len(data.news_now):,}", _pct(len(data.news_now), total)])
    per_source: dict[str, int] = {}
    for trend in trends:
        for src in set(_jsonb_list(trend.get("sources"))):
            per_source[src] = per_source.get(src, 0) + 1
    for src, n in sorted(per_source.items(), key=lambda kv: (-kv[1], kv[0])):
        rows.append([src, STREAM_LABELS[STREAM_VIRAL], f"{n:,}", _pct(n, total)])

    body = _table(["Channel", "Stream", "Items", "Share of all items"], rows,
                  ["left", "left", "right", "right"])
    return _section(
        "channel_analysis", "Channel Analysis", kind="table",
        basis=(f"{total:,} item(s) in {_range(data.win_from, data.win_to)}, "
               "counted by where each one came from."
               + (" A viral trend can be seen "
                  "on more than one source, so the Viral rows can add up to more "
                  "than the number of trends; shares are of the total item count "
                  "and will therefore exceed 100% when that happens."
                  if data.has(STREAM_VIRAL) else "")
               + (" " + data.scope_sentence() if data.scoped else "")),
        body=body, available=bool(rows),
        unavailable_reason=(None if rows else
                            "No items were collected on any channel in this window."),
        estimated=False,
    )


def _spikes(data: ReportData) -> list[list[str]]:
    """Buckets carrying an unusual share of the window's volume. Measured."""
    bucket_days, count = _bucket_plan(data)
    out: list[list[str]] = []
    streams = (
        (STREAM_COMPETITOR, data.competitors_now, post_time),
        (STREAM_NEWS, data.news_now, post_time),
        (STREAM_VIRAL, data.trends_new_shown, trend_first_seen),
    )
    for stream, rows, clock in streams:
        values = [0] * count
        for row in rows:
            idx = _bucket_index(clock(row), data, bucket_days, count)
            if idx is not None:
                values[idx] += 1
        if not any(values):
            continue
        mean = sum(values) / float(count)
        for i, v in enumerate(values):
            if v >= SPIKE_MIN_ITEMS and mean > 0 and v >= SPIKE_RATIO * mean:
                when = data.win_from + timedelta(days=i * bucket_days)
                out.append([
                    _day(when), "Volume spike",
                    f"{v:,} item(s) against a window average of {mean:.1f}",
                    STREAM_LABELS[stream],
                ])
    return out


def build_reputation_risk(data: ReportData) -> dict:
    rows: list[list[str]] = []
    flagged = 0
    for row in data.trends_now:
        sensitivity = str(row.get("sensitivity") or "ok").lower()
        safety = str(row.get("safety") or "ok").lower()
        if sensitivity in {"caution", "avoid"}:
            flag = f"scorer: {sensitivity}"
        elif safety != "ok":
            flag = f"ingest screen: {safety}"
        else:
            continue
        flagged += 1
        # ONLY the safety screen writes a reason for a flag. `fit_reason`
        # explains why a trend would or would not SUIT 42 — printing it here
        # would read as a justification for the flag and mislead every time the
        # two disagree, which on live rows they routinely do.
        why = _one_line(row.get("safety_reason"), 74) or (
            "verdict recorded without a written reason")
        rows.append([
            _one_line(row.get("title"), 46) or "untitled trend",
            flag,
            why,
            ", ".join(_jsonb_list(row.get("sources"))[:2]) or "-",
        ])
    shown_flags = len(rows[:RISK_CAP])
    rows = rows[:RISK_CAP]
    spikes = _spikes(data)
    rows.extend(spikes)

    body = _table(["Item", "Flag", "Why", "Where"], rows,
                  ["left", "left", "left", "left"])
    total_trends = len(data.trends_now)

    if not data.has(STREAM_VIRAL):
        # The flag half of this section is a viral-trend measure: only the
        # general-trends ingest writes a safety verdict and only the trend
        # scorer writes a sensitivity one. Printing "0 of 0 flagged" here would
        # read as "we checked and everything is clean". We did not check.
        spike_txt = (f"{len(spikes)} day(s) on which item volume spiked"
                     if spikes else "no volume spike")
        return _section(
            "reputation_risk", "Reputation / Risk", kind="table",
            basis=("The safety screen and the AI scorer's sensitivity verdict "
                   "only ever run on Viral Social Trends, and this report "
                   f"covers {data.scope_label} only - so no content flag can "
                   "appear here, and none has been inferred. What is left is "
                   "the measured half: a day is called a spike when it carries "
                   f"at least {SPIKE_RATIO:.0f}x the window average and at "
                   f"least {SPIKE_MIN_ITEMS} items. Measured: {spike_txt}. "
                   "Nothing here is reputation monitoring OF 42 Abu Dhabi - "
                   "the system does not track mentions of the school, so it "
                   "cannot see what is being said about it."),
            body=body, available=bool(rows),
            unavailable_reason=(None if rows else (
                "No volume spike cleared the bar in this window, and content "
                "flags are a Viral Social Trends measure that this scope does "
                "not cover.")),
            estimated=False,
        )
    # The table holds two DIFFERENT kinds of row - flagged trends and spike
    # days - and it is capped. Left unsaid, a reader who counts the rows gets a
    # number that matches neither the flag count nor anything else, and on the
    # first live run the narrative pass counted 22 rows and wrote "the system
    # has flagged 22 trends" when 20 trends were flagged and 15 were listed.
    # So the basis reconciles the visible rows out loud: 15 + 7 = 22.
    composition = (
        f"The table lists {shown_flags} of those {flagged} flagged trend(s)"
        + (f" (the {RISK_CAP} highest-priority are shown)"
           if flagged > shown_flags else "")
        + (f", followed by {len(spikes)} day(s) on which item volume spiked"
           if spikes else ", and no volume spikes were measured")
        + f" - {len(rows)} row(s) in total. A spike row is a day, not a trend."
    )
    return _section(
        "reputation_risk", "Reputation / Risk", kind="table",
        basis=(f"{flagged} of {total_trends} viral trend(s) in the window carry "
               "a flag from the ingest safety screen or the AI scorer's "
               "sensitivity verdict, plus any volume spike measured from the "
               f"stored publish times. {composition} A flag is a judgement about whether 42 "
               "Abu Dhabi should touch a subject; it is not a measurement of "
               "harm. Nothing here is reputation monitoring OF 42 Abu Dhabi - "
               "the system does not track mentions of the school, so it cannot "
               "see what is being said about it."),
        body=body, available=bool(rows),
        unavailable_reason=(None if rows else (
            f"None of the {total_trends} trend(s) in this window were flagged "
            "and no volume spike cleared the bar."
            if total_trends else
            "No viral trends were collected in this window, and the other two "
            "streams carry no safety or sensitivity verdict.")),
        estimated=True,
    )


def build_data_basis(data: ReportData) -> dict:
    handles = {r.get("username") for r in data.competitors_now if r.get("username")}
    watched = [c for c in data.competitors
               if str(c.get("status") or "active") != "excluded"]
    items = [
        f"This report covers {_range(data.win_from, data.win_to)} ({_dl(data.days)}).",
        "Likes, comments, views and post dates come from the source platforms. "
        "Topics, tone and suggested ideas are AI-assisted and labelled as such.",
        "Tone describes the words in a post. It does not show audience sentiment: "
        "we do not collect comments.",
        "We do not collect audience demographics such as age, location or interests.",
        "We do not track 42 Abu Dhabi's own posts or mentions of 42 Abu Dhabi. "
        "Opportunities in this report are ideas to test, not confirmed content gaps.",
    ]
    if data.has(STREAM_COMPETITOR):
        items.insert(4, "Instagram does not give us share counts for competitor posts, "
                     "so shares are not used as evidence here.")
    if data.scoped:
        items.insert(1, f"This is a {data.scope_label}-only view. Other sources are not included.")
    if data.has(STREAM_COMPETITOR) and len(handles) < 5:
        items.append(
            f"Only {len(handles)} of {len(watched)} watched competitor accounts "
            "posted in this window, so comparisons are directional rather than "
            "a complete view of the market.")
    items.extend(str(note) for note in data.load_notes if str(note).strip())
    return _section(
        "data_basis", "HOW TO READ THIS REPORT", kind="bullets",
        basis=("A short guide to what is measured, what is AI-assisted and what "
               "the report cannot tell us."),
        body={"items": items}, estimated=False,
    )


FULL_BUILDERS = {
    "executive_summary": build_executive_summary,
    "report_snapshot": build_report_snapshot,
    "activity": build_conversation_trend,
    "competitor_conversation": build_competitor_conversation,
    "competitor_content": build_competitor_content,
    "industry_signals": build_industry_signals,
    "content_opportunities": build_content_opportunities,
    "recommendations": build_recommendations,
    "data_basis": build_data_basis,
}


def build_sections(data: ReportData, mode: str) -> list[dict]:
    """The ordered section list for a mode. Ordering lives here and nowhere else.

    A scoped report keeps every section id and every position — a section the
    scope cannot answer is REPLACED by an honest "not applicable", never
    dropped. A reader comparing a competitors-only report against the
    all-streams one must find the same headings in the same order, so that a
    missing number is visibly missing rather than quietly absent.

    That invariant is about SCOPE, and a switched-off stream is not a scope: a
    section only the viral stream could fill is absent from FULL_SECTION_IDS
    entirely while collection is paused, so every scope still agrees with every
    other — they just agree on a shorter list."""
    if mode == "simple":
        return [build_signals(data)]
    return [build_not_applicable(key, data.scope) if scope_blocks(key, data.scope)
            else FULL_BUILDERS[key](data)
            for key in FULL_SECTION_IDS]


def build_basis(data: ReportData) -> dict:
    handles = {r.get("username") for r in data.competitors_now if r.get("username")}
    notes: list[str] = []
    if data.scoped:
        notes.append(data.scope_sentence())
    notes += [
        "Tone is an AI read of post text, not audience sentiment.",
        "The report does not track 42 Abu Dhabi's own posts or mentions.",
    ]
    notes.extend(data.load_notes)
    if data.has(STREAM_COMPETITOR) and len(handles) < 3:
        notes.append(
            f"Only {len(handles)} watched account(s) returned posts in this "
            "window, so competitor comparisons are directional.")
    # Out-of-scope streams are None, not 0. A 0 would say "we looked and there
    # were none"; None says "this report did not look", and every surface that
    # prints these counts skips a None rather than drawing a zero.
    out = {
        "scope": data.scope,
        "scope_label": data.scope_label,
        "competitor_handles": (len(handles) if data.has(STREAM_COMPETITOR)
                               else None),
        "competitor_posts": (len(data.competitors_now)
                             if data.has(STREAM_COMPETITOR) else None),
        "news_items": len(data.news_now) if data.has(STREAM_NEWS) else None,
    }
    # A switched-off stream is ABSENT, not None. The two mean different things
    # to a reader: None renders as "not in scope", which invites the question
    # "then where do I get it?" — and while collection is paused the answer is
    # nowhere. Omitting the key drops the stat from the panel and from the PDF
    # (both already skip what they are not given) instead of leaving a dash
    # advertising a stream that is not running.
    if VIRAL_STREAM:
        out["viral_trends"] = (len(showable_trends(data.trends_now))
                               if data.has(STREAM_VIRAL) else None)
    out["notes"] = notes
    return out


def assemble(data: ReportData, mode: str) -> dict:
    """The whole contract dict, from a loaded `ReportData`. Pure — tests build
    `ReportData` from fixture rows and call this with no database."""
    mode = mode if mode in REPORT_MODES else "simple"
    base = ("SMAA Intelligence Signals" if mode == "simple"
            else "SMAA Social Intelligence Report")
    # The scope rides in the TITLE, not only in a field: the title is what lands
    # on the cover, in the browser tab and in the filename, and a single-stream
    # report that looks like the whole picture is the one misreading this
    # feature cannot afford.
    title = base if not data.scoped else f"{base} - {data.scope_label}"
    return {
        "meta": {
            "title": title,
            "brand": BRAND,
            "mode": mode,
            "scope": data.scope,
            "scope_label": data.scope_label,
            "item": None,
            "generated_at": _iso(data.built_at or data.now),
            "window": {"days": data.days, "from": _iso(data.win_from),
                       "to": _iso(data.win_to),
                       # False means the window ENDS TODAY. A stored report is
                       # read weeks later, and "the 30 days to 14 Sep" has to
                       # be readable as a deliberate choice rather than as a
                       # trailing report that went stale on the shelf.
                       "chosen": bool(data.chosen_end)},
            "previous_window": {"from": _iso(data.prev_from),
                                "to": _iso(data.prev_to)},
        },
        "basis": build_basis(data),
        "sections": build_sections(data, mode),
    }


# ---------------------------------------------------------------------------
# The tone pass — the ONLY model call in this module, and it is allowed to fail
# ---------------------------------------------------------------------------

_TONE_SYSTEM = (
    "You label the TONE OF A PIECE OF TEXT. You are NOT measuring how an "
    "audience reacted - you never see any audience reaction. Read only the "
    "words given and answer positive, neutral or negative. Most marketing "
    "copy and most news headlines are neutral; reserve positive and negative "
    "for text that clearly carries that charge. Return one entry per key."
)

# Gemini drops properties that are not in `required`, so every field is required.
_TONE_SCHEMA = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "key": {"type": "string"},
                    "tone": {"type": "string",
                             "enum": ["positive", "neutral", "negative"]},
                },
                "required": ["key", "tone"],
            },
        }
    },
    "required": ["items"],
}


def tone_targets(data: ReportData, mode: str) -> list[dict]:
    """`[{key, text}]` for the items whose tone the report would print.

    Simple mode assesses exactly the items it shows. Full mode assesses a
    capped, deterministic sample across all three streams, and the sentiment
    section then reports how many of the window it covered.

    A SCOPED report assesses only the stream it is about. The per-stream share
    stays fixed at `TONE_PER_STREAM` rather than growing to fill the batch:
    this is the single slowest step in the whole report (one model call over 60
    texts), and a one-stream report that reads the same 60 texts would be no
    faster than the all-streams one — which is the whole reason the owner asked
    for a per-tab download."""
    picks: list[tuple[str, dict]] = []
    share = SIGNALS_PER_GROUP if mode == "simple" else TONE_PER_STREAM
    pickers = {
        STREAM_COMPETITOR: lambda n: top_competitor_posts(data.competitors_now, n),
        STREAM_NEWS: lambda n: top_news_items(data.news_now, n),
        STREAM_VIRAL: lambda n: top_trends(data.trends_now, n),
    }
    for stream in data.streams:
        picks += [(stream, r) for r in pickers[stream](share)]

    out: list[dict] = []
    for stream, row in picks[:TONE_MAX_ITEMS]:
        if stream == STREAM_COMPETITOR:
            text = row.get("caption") or row.get("topic_summary") or row.get("topic")
        elif stream == STREAM_NEWS:
            text = row.get("caption") or _meta(row).get("summary") or row.get("topic")
        else:
            text = (row.get("topic_summary") or row.get("summary")
                    or row.get("title"))
        text = _WS_RE.sub(" ", str(text or "")).strip()[:TONE_TEXT_CHARS]
        if text:
            out.append({"key": tone_key(stream, row), "text": text})
    return out


def parse_tone_response(raw: Any, targets: list[dict]) -> dict[str, dict]:
    """Model reply -> `{key: {"label", "estimated"}}`, keeping only keys we asked
    about. Anything unparseable yields {} — never a default label."""
    wanted = {t["key"] for t in targets}
    if not isinstance(raw, dict):
        return {}
    items = raw.get("items")
    if not isinstance(items, list):
        return {}
    out: dict[str, dict] = {}
    for entry in items:
        if not isinstance(entry, dict):
            continue
        key = str(entry.get("key") or "").strip()
        label = TONE_LABELS.get(str(entry.get("tone") or "").strip().lower())
        if key in wanted and label:
            out[key] = {"label": label, "estimated": True}
    return out


async def assess_tone(targets: list[dict]) -> dict[str, dict]:
    """One batched Gemini call. Every failure path returns {} so the report
    prints 'not assessed' instead of a made-up mood."""
    if not targets:
        return {}
    from .vertex_client import gemini_text  # imported late: keeps tests model-free

    lines = "\n".join(f"{t['key']} :: {t['text']}" for t in targets)
    prompt = (
        "Label the tone of each line. The format is `key :: text`.\n\n"
        f"{lines}\n\n"
        "Return one item per key, using the key exactly as given."
    )
    try:
        raw = await gemini_text(
            prompt, system=_TONE_SYSTEM, json_schema=_TONE_SCHEMA,
            temperature=0.0, max_tokens=TONE_MAX_TOKENS,
            # Classification, not reasoning: hidden thinking tokens would eat
            # the output budget and truncate the array.
            thinking_budget=0,
            model=(config.vertex_gemini_fast_model or None),
        )
    except Exception:  # noqa: BLE001 - a missing tone must never fail a report
        log.warning("[social-report] tone pass failed; sentiment will be "
                    "reported as not assessed", exc_info=True)
        return {}
    return parse_tone_response(raw, targets)


# ---------------------------------------------------------------------------
# Loading — six queries, both windows, no query per row
# ---------------------------------------------------------------------------

# Row caps. They exist so one runaway query cannot wedge a click, but a cap
# that is HIT silently turns every count in the report into an undercount — so
# hitting one is recorded and printed, never swallowed. At the measured live
# rate (roughly 40 news items a day) a 90-day report reads ~7,200 rows, well
# inside these.
_POST_ROW_CAP = 40000
_TREND_ROW_CAP = 10000
_SIGHTING_ROW_CAP = 60000

_POST_COLS = (
    "id, competitor_id, username, permalink, media_type, media_product_type, "
    "caption, posted_at, like_count, comments_count, view_count, engagement, "
    "engagement_rate, outperformance, topic, topic_summary, theme, fit_score, "
    "fit_reason, uae_relevance, suggested_angle, suggested_format, effort, "
    "opportunity_score, status, source, region, language, platform, metadata, "
    "created_at"
)

_TREND_COLS = (
    "id, slug, title, summary, url, category, regions, sources, source_count, "
    "sighting_count, evidence_count, peak_metric, velocity, first_seen_at, "
    "last_seen_at, latest_content_at, topic, topic_summary, theme, trend_kind, "
    "sensitivity, safety, safety_reason, fit_score, fit_reason, uae_relevance, "
    "suggested_angle, suggested_format, effort, opportunity_score, status, "
    "metadata"
)


async def load_data(days: int, *, now: Optional[datetime] = None,
                    end: Optional[Any] = None,
                    whole_days: bool = False,
                    scope: str = DEFAULT_SCOPE,
                    sightings: bool = True) -> ReportData:
    """Everything both windows need, in at most six queries.

    A SCOPE ONLY LOADS WHAT IT NEEDS. The all-streams report reads trend_posts,
    general_trends, general_trend_sightings and trend_competitors;
    a viral-only report reads two of those. The point is not tidiness — the
    sightings query is the widest read in the whole feature, and paying for it
    to build a competitors report would make the per-tab download the owner
    asked for no faster than the one they already have.

    `sightings=False` skips that widest read for a caller that ranks trends on
    their stored columns alone (see `load_item`): no section can then quote a
    per-trend view count, and none does.

    `end` is a window end the CALLER chose (a date or a datetime), so "the week
    of 8-14 Sep" is the same report as any other, one window earlier. `now`
    stays what it always was, the clock (tests pin it); `end` wins when both are
    given, and only `end` makes the report describe itself as a past one.

    `whole_days` is passed straight to `make_window`: see there for why the
    route behind the date picker asks for it and nothing else does."""
    win = make_window(days, end if end is not None else now,
                      whole_days=whole_days)
    scope = clean_scope(scope)
    streams = SCOPE_STREAMS[scope]
    want_posts = (STREAM_COMPETITOR in streams) or (STREAM_NEWS in streams)
    want_viral = STREAM_VIRAL in streams

    if want_posts:
        await trend_store.ensure_tables()
    if want_viral:
        await general_trends_store.ensure_tables()

    data = ReportData(now=win["win_to"], days=win["days"],
                      win_from=win["win_from"], win_to=win["win_to"],
                      prev_from=win["prev_from"], prev_to=win["prev_to"],
                      scope=scope, chosen_end=end is not None,
                      built_at=_as_dt(now) or datetime.now(timezone.utc))
    span_from = win["prev_from"]

    async with get_conn(autocommit=True) as conn:
        async with conn.cursor() as cur:
            # 1) the post stream(s) in scope, both windows, one pass
            if want_posts:
                # `source` is nullable on old rows and those are competitor
                # posts, so the competitor filter is written as "not news".
                if STREAM_COMPETITOR in streams and STREAM_NEWS in streams:
                    only = ""
                elif STREAM_NEWS in streams:
                    only = "AND source = 'news'"
                else:
                    only = "AND COALESCE(source, 'competitor') <> 'news'"
                await cur.execute(
                    f"""
                    SELECT {_POST_COLS}
                      FROM trend_posts
                     WHERE COALESCE(posted_at, created_at) >= %s
                       {only}
                     ORDER BY COALESCE(posted_at, created_at) DESC
                     LIMIT %s
                    """,
                    (span_from, _POST_ROW_CAP),
                )
                fetched = await cur.fetchall()
                if len(fetched) >= _POST_ROW_CAP:
                    data.load_notes.append(
                        f"Only the {_POST_ROW_CAP:,} most recent competitor and "
                        "news rows were read, so counts over this window are a "
                        "floor, not a total.")
                for r in fetched:
                    row = dict(r)
                    if str(row.get("source") or "competitor") == "news":
                        data.news_rows.append(row)
                    else:
                        data.competitor_rows.append(row)

            # 2) viral trends touching either window
            if want_viral:
                await cur.execute(
                    f"""
                    SELECT {_TREND_COLS}
                      FROM general_trends
                     WHERE last_seen_at >= %s OR first_seen_at >= %s
                     ORDER BY velocity DESC NULLS LAST, last_seen_at DESC
                     LIMIT %s
                    """,
                    (span_from, span_from, _TREND_ROW_CAP),
                )
                data.trend_rows = [dict(r) for r in await cur.fetchall()]
                if len(data.trend_rows) >= _TREND_ROW_CAP:
                    data.load_notes.append(
                        f"Only the {_TREND_ROW_CAP:,} hottest viral trends were "
                        "read, so trend counts over this window are a floor, not "
                        "a total.")

            # 3) every distinct sighting behind those trends (the detail view's
            #    evidence, not the 6-row preview) — one query, not one per trend
            ids = ([int(t["id"]) for t in data.trend_rows if t.get("id") is not None]
                   if sightings else [])
            if ids:
                await cur.execute(
                    """
                    SELECT DISTINCT ON (trend_id, source, source_key)
                           trend_id, source, source_key, title, url, metric_value,
                           metric_label, region, published_at, seen_at, extra
                      FROM general_trend_sightings
                     WHERE trend_id = ANY(%s)
                     ORDER BY trend_id, source, source_key, seen_at DESC, id DESC
                     LIMIT %s
                    """,
                    (ids, _SIGHTING_ROW_CAP),
                )
                for r in await cur.fetchall():
                    row = dict(r)
                    data.sightings.setdefault(int(row["trend_id"]), []).append(row)

            # 4) watched accounts
            if STREAM_COMPETITOR in streams:
                await cur.execute(
                    """
                    SELECT id, username, display_name, followers_count, media_count,
                           region, status, median_engagement, last_scanned_at,
                           posts_found
                      FROM trend_competitors
                     ORDER BY followers_count DESC NULLS LAST, username
                    """
                )
                data.competitors = [dict(r) for r in await cur.fetchall()]

            # 5) (was the news feed catalogue, for a "source feed" link on each
            #    item.) Industry Signal has no feed catalogue any more: stories
            #    are found by a search, so the only address a story has is its
            #    own `permalink`, which every item already carries.

            # 6) how far back our collection actually reaches, per stream — the
            #    difference between "volume fell" and "we only switched this on
            #    last week". One SELECT of scalar sub-queries, and a scope only
            #    pays for the ones it will print.
            parts = []
            if STREAM_COMPETITOR in streams:
                parts.append(
                    "(SELECT MIN(posted_at) FROM trend_posts "
                    "  WHERE source = 'competitor') AS competitor_from")
                parts.append(
                    "(SELECT MAX(last_scanned_at) FROM trend_competitors) "
                    "  AS last_scan_at")
            if STREAM_NEWS in streams:
                parts.append(
                    "(SELECT MIN(COALESCE(posted_at, created_at)) FROM trend_posts "
                    "  WHERE source = 'news') AS news_from")
            if want_viral:
                parts.append(
                    "(SELECT MIN(first_seen_at) FROM general_trends) AS viral_from")
            if parts:
                await cur.execute("SELECT " + ", ".join(parts))
                row = await cur.fetchone()
                data.coverage = dict(row) if row else {}
    return data


# ---------------------------------------------------------------------------
# What windows there is any point asking for
# ---------------------------------------------------------------------------
#
# A window picker that offers every day back to 1970 is a picker that mostly
# produces empty reports, and an empty report is the one output of this feature
# that costs two model passes and tells the reader nothing. So the picker asks
# what we actually hold, and greys out the rest.
#
# TWO DIFFERENT "EMPTY" LIVE HERE, and keeping them apart is the whole job:
#   * outside the bounds  - we were not collecting. There is nothing to report
#                           and there never will be. Not selectable.
#   * inside the bounds, zero rows on the day - we were collecting and nothing
#                           happened. That IS a finding, so the day stays
#                           selectable and simply shows no mark.

# How far back the per-day counts reach. Comfortably past the 90-day maximum
# window, and bounded so this stays one cheap grouped scan as the tables grow.
COVERAGE_DAYS = 400


def _cover_day(value: Any) -> Optional[str]:
    """A stored timestamp OR a grouped `::date` as a plain YYYY-MM-DD.

    `_as_dt` does not take a bare `date`, and the GROUP BY below returns one,
    which is how the first version of this silently reported no coverage at
    all while the bounds queries beside it worked."""
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    dt = _as_dt(value)
    return dt.date().isoformat() if dt else None


async def data_coverage(scope: str = DEFAULT_SCOPE) -> dict:
    """The days this scope has records for: the bounds, and a count per day.

    Read by the window picker before anything is built. Counts are ROW counts,
    not a quality judgement: they exist so a reader choosing "which week" can
    see where the activity was instead of guessing and paying for the answer."""
    scope = clean_scope(scope)
    streams = SCOPE_STREAMS[scope]
    want_posts = (STREAM_COMPETITOR in streams) or (STREAM_NEWS in streams)
    want_viral = STREAM_VIRAL in streams

    if want_posts:
        await trend_store.ensure_tables()
    if want_viral:
        await general_trends_store.ensure_tables()

    # Same filter as `load_data`: `source` is nullable on old rows and those are
    # competitor posts, so "competitor" is written as "not news". The picker
    # must offer exactly the days the report would read.
    if STREAM_COMPETITOR in streams and STREAM_NEWS in streams:
        post_filter = ""
    elif STREAM_NEWS in streams:
        post_filter = "WHERE source = 'news'"
    else:
        post_filter = "WHERE COALESCE(source, 'competitor') <> 'news'"

    counts: dict[str, int] = {}
    streams_out: dict[str, dict] = {}
    since = datetime.now(timezone.utc) - timedelta(days=COVERAGE_DAYS)

    async with get_conn(autocommit=True) as conn:
        async with conn.cursor() as cur:
            if want_posts:
                await cur.execute(
                    f"""
                    SELECT (COALESCE(posted_at, created_at) AT TIME ZONE 'UTC')::date
                             AS day,
                           COUNT(*) AS n
                      FROM trend_posts
                      {post_filter}
                     GROUP BY 1
                     ORDER BY 1
                    """
                )
                for row in await cur.fetchall():
                    day = _cover_day(dict(row).get("day"))
                    if day:
                        counts[day] = counts.get(day, 0) + int(dict(row)["n"])

                for stream, only in ((STREAM_COMPETITOR,
                                      "COALESCE(source, 'competitor') <> 'news'"),
                                     (STREAM_NEWS, "source = 'news'")):
                    if stream not in streams:
                        continue
                    await cur.execute(
                        f"""
                        SELECT MIN(COALESCE(posted_at, created_at)) AS lo,
                               MAX(COALESCE(posted_at, created_at)) AS hi
                          FROM trend_posts WHERE {only}
                        """
                    )
                    row = dict(await cur.fetchone() or {})
                    streams_out[stream] = {"from": _cover_day(row.get("lo")),
                                           "to": _cover_day(row.get("hi"))}

            if want_viral:
                await cur.execute(
                    """
                    SELECT (first_seen_at AT TIME ZONE 'UTC')::date AS day,
                           COUNT(*) AS n
                      FROM general_trends
                     GROUP BY 1
                     ORDER BY 1
                    """
                )
                for row in await cur.fetchall():
                    day = _cover_day(dict(row).get("day"))
                    if day:
                        counts[day] = counts.get(day, 0) + int(dict(row)["n"])
                await cur.execute(
                    "SELECT MIN(first_seen_at) AS lo, MAX(last_seen_at) AS hi "
                    "FROM general_trends"
                )
                row = dict(await cur.fetchone() or {})
                streams_out[STREAM_VIRAL] = {"from": _cover_day(row.get("lo")),
                                             "to": _cover_day(row.get("hi"))}

    today = datetime.now(timezone.utc).date().isoformat()
    # A row dated in the FUTURE is a bad `posted_at`, not coverage. Offering a
    # window that ends next month would produce a report of one stray row.
    known = sorted(d for d in counts if d <= today)
    floor = since.date().isoformat()
    recent = [{"date": d, "count": counts[d]} for d in known if d >= floor]
    return {
        "scope": scope,
        "from": known[0] if known else None,
        "to": known[-1] if known else None,
        "today": today,
        "days": recent,
        "streams": streams_out,
        "max_days": 90,
    }


async def build_report(*, mode: str = "simple", days: int = 30,
                       tone: bool = True, scope: str = DEFAULT_SCOPE,
                       end: Optional[Any] = None,
                       whole_days: bool = False) -> dict:
    """The one entry point for a stream-level report.

    `mode` is 'simple' (the feed-style digest) or 'full' (the concise briefing);
    `days` is clamped to 1..90; `scope` narrows the whole report to one
    of the Trends page's three tabs ('competitors', 'news', 'viral') or covers
    all three ('all', the default and the behaviour this had before scopes
    existed).

    `tone` exists so a caller that cannot spend a model call (a test, a cron
    that only wants counts) still gets every measured section. With it off, the
    Sentiment section reports itself unavailable rather than empty.

    `end` moves the window into the past: with it, `days` counts back from that
    day instead of from now, which is how "which week?" is answered. Without
    it, this is the trailing report it has always been. `whole_days` makes the
    window whole calendar days, which is what a date picker means when someone
    drags across a fortnight."""
    mode = mode if mode in REPORT_MODES else "simple"
    data = await load_data(days, end=end, whole_days=whole_days,
                           scope=clean_scope(scope))
    if tone:
        data.tone = await assess_tone(tone_targets(data, mode))
    return assemble(data, mode)


# ===========================================================================
# ITEM REPORTS — one competitor post, one news item or one viral trend
# ===========================================================================
#
# A different question, so a different report. `build_report` answers "what
# happened in the last 30 days"; this answers "what is THIS, what did it
# actually do, and is it worth 42's time". It is opened from a detail dialog
# with the owner waiting on it, which sets two hard constraints:
#
#   1. It reads ONE row plus its own stream's window (for the rank), and it
#      assesses tone for ONE text. No 60-item batch, no three-stream load.
#   2. It has far FEWER numbers than the all-streams report, which makes the
#      temptation to pad it higher, not lower. So every field below is either a
#      stored column, a source's own counter, or explicitly labelled as a
#      model's judgement — and a field the row does not carry says "not scored
#      yet" rather than going quietly blank.
#
# The one genuinely new analysis is `item_ranking`: where this item sits among
# the others in its own stream over the window, on MEASURED columns only. That
# is the thing a screenshot of the dialog cannot tell you.

_ORDINALS = {1: "st", 2: "nd", 3: "rd"}


def _ordinal(n: int) -> str:
    """1st / 2nd / 3rd / 11th — 11..13 are the exception every naive version of
    this gets wrong, and "11st of 20" reads as generated."""
    n = int(n)
    suffix = "th" if 10 <= (n % 100) <= 20 else _ORDINALS.get(n % 10, "th")
    return f"{n:,}{suffix}"


_SLUG_RE = re.compile(r"[^a-z0-9]+")


def _slugify(value: Any, limit: int = 28) -> str:
    """ASCII, lowercase, hyphenated — safe to drop straight into a filename.

    The router sanitises the header too, but a name that only survives BECAUSE
    of that is a name nobody can predict."""
    out = _SLUG_RE.sub("-", str(value or "").strip().lower()).strip("-")
    return out[:limit].rstrip("-") if len(out) > limit else out


def _median(values: list[float]) -> Optional[float]:
    if not values:
        return None
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2.0


def _fmt_metric(value: Optional[float], digits: int = 0) -> str:
    if value is None:
        return "not recorded"
    return f"{value:,.{digits}f}"


# ---------------------------------------------------------------------------
# The one item, plus the window it is ranked against
# ---------------------------------------------------------------------------

@dataclass
class ItemData:
    """One row and everything measured about it.

    `data` is a ReportData scoped to this item's own stream and loaded WITHOUT
    the bulk sightings query — the ranking runs on stored columns, so nothing
    here quotes a per-trend counter for any row except this one."""

    kind: str                       # "post" | "trend"
    stream: str                     # competitor | news | viral
    row: dict
    data: ReportData
    sightings: list[dict] = field(default_factory=list)
    research: Optional[dict] = None
    account: Optional[dict] = None          # trend_competitors row, if any
    news_source: Optional[dict] = None      # news_sources row, if any
    tone: dict = field(default_factory=dict)

    # -- identity -----------------------------------------------------------
    @property
    def item_id(self) -> int:
        return int(self.row.get("id") or 0)

    @property
    def scope(self) -> str:
        return STREAM_SCOPES[self.stream]

    @property
    def stream_label(self) -> str:
        return STREAM_LABELS[self.stream]

    @property
    def label(self) -> str:
        if self.stream == STREAM_VIRAL:
            return str(self.row.get("title") or "").strip() or "untitled trend"
        if self.stream == STREAM_NEWS:
            return (_one_line(self.row.get("caption") or self.row.get("topic"), 110)
                    or "untitled news item")
        topic = _one_line(self.row.get("topic") or self.row.get("caption"), 80)
        handle = _handle(self.row.get("username"))
        return f"{handle} - {topic}" if topic else handle

    @property
    def slug(self) -> str:
        if self.stream == STREAM_VIRAL:
            return (_slugify(self.row.get("slug"))
                    or _slugify(self.row.get("title")) or "trend")
        if self.stream == STREAM_NEWS:
            return (_slugify(self.row.get("caption"))
                    or _slugify(self.row.get("platform")) or "news")
        return _slugify(self.row.get("username")) or "post"

    @property
    def link(self) -> Optional[str]:
        url = str((self.row.get("url") if self.stream == STREAM_VIRAL
                   else self.row.get("permalink")) or "").strip()
        return url or None

    # -- the population this item is ranked against -------------------------
    @property
    def peers(self) -> list[dict]:
        if self.stream == STREAM_COMPETITOR:
            return self.data.competitors_now
        if self.stream == STREAM_NEWS:
            return self.data.news_now
        return showable_trends(self.data.trends_now)

    @property
    def peer_noun(self) -> str:
        return {STREAM_COMPETITOR: "competitor post(s)",
                STREAM_NEWS: "news item(s)",
                STREAM_VIRAL: "viral trend(s)"}[self.stream]

    @property
    def clock(self) -> Optional[datetime]:
        """The timestamp that means "this happened", per stream."""
        if self.stream == STREAM_VIRAL:
            return trend_first_seen(self.row)
        return post_time(self.row)

    @property
    def dated_in_window(self) -> bool:
        return _within(self.clock, self.data.win_from, self.data.win_to)

    @property
    def in_population(self) -> bool:
        return any(int(p.get("id") or 0) == self.item_id for p in self.peers)

    def measures(self) -> dict:
        """The viral counters, read out of THIS trend's own evidence."""
        return trend_measures(self.row, self.sightings)

    def tone_label(self) -> Optional[dict]:
        hit = self.tone.get(tone_key(self.stream, self.row))
        if not hit:
            return None
        return {"label": str(hit.get("label") or "Neutral"), "estimated": True}


# ---------------------------------------------------------------------------
# Ranking — the one genuinely new analysis
# ---------------------------------------------------------------------------

def rank_within(peers: Iterable[dict], row: dict, key: str,
                *, value_of: Optional[Any] = None) -> dict:
    """Where `row` sits among `peers` on the stored column `key`.

    Returns `{rank, total, value, median}`. `rank` is None when this row has no
    value for that column — there is then nothing to rank, and the caller must
    say so rather than print a 0. Ties share the better rank: two posts on
    identical engagement are both "3rd of 20", never 3rd and 4th decided by row
    order.

    `value_of` reads a number that is not a top-level column (a viral trend's
    measures live in `metadata`), so one ranking implementation covers every
    stream instead of three that can drift apart."""
    read = value_of if callable(value_of) else (lambda r: _num(r.get(key)))
    values = [v for v in (read(p) for p in peers) if v is not None]
    mine = read(row)
    total = len(values)
    if mine is None or not total:
        return {"rank": None, "total": total, "value": mine,
                "median": _median(values)}
    better = sum(1 for v in values if v > mine)
    return {"rank": better + 1, "total": total, "value": mine,
            "median": _median(values)}


def rank_kpi(label: str, ranked: dict, *, noun: str, unit: str,
             digits: int = 0) -> dict:
    """"3rd of 20", with the numbers behind it in the note so the position can
    be checked rather than believed."""
    if ranked["rank"] is None:
        return _kpi(label, "not ranked",
                    note=(f"this item carries no {unit}, so it cannot be placed "
                          f"among the {ranked['total']:,} {noun} that do"))
    note = (f"among the {ranked['total']:,} {noun} in the window carrying a "
            f"{unit}; this item {_fmt_metric(ranked['value'], digits)}")
    if ranked["median"] is not None:
        note += f", the window median {_fmt_metric(ranked['median'], digits)}"
    return _kpi(label, f"{_ordinal(ranked['rank'])} of {ranked['total']:,}",
                note=note)


# ---------------------------------------------------------------------------
# The absences, stated on a single item exactly as they are on the whole report
# ---------------------------------------------------------------------------

SENTIMENT_METRIC_LABEL = "Audience sentiment"
SENTIMENT_METRIC_VALUE = "not measured - we store no comment text"

# News items have no engagement counter of any kind: we read a feed, not the
# article's page. Printing nothing would let a reader assume it was zero.
NEWS_ENGAGEMENT_VALUE = (
    "not collected - we read the outlet's feed, not the article's page"
)


def _item_metrics(item: ItemData) -> list[dict]:
    """Every measured number this item carries, each with its unit, plus the
    absences named as absences."""
    row = item.row
    out: list[dict] = []

    def add(label: str, value: Optional[str]) -> None:
        if value:
            out.append({"label": label, "value": value})

    if item.stream == STREAM_COMPETITOR:
        add("Posted", _day(post_time(row)))
        fmt = " / ".join(x for x in (str(row.get("media_type") or "").strip(),
                                     str(row.get("media_product_type") or "").strip())
                         if x)
        add("Format", fmt or None)
        add("Likes", _count(row.get("like_count")))
        add("Comments", _count(row.get("comments_count")))
        if _int(row.get("view_count")) is not None:
            add("Views", _count(row.get("view_count")))
        add("Engagement (likes + comments)", _count(row.get("engagement")))
        rate = _num(row.get("engagement_rate"))
        if rate is not None:
            add("Engagement rate", f"{rate * 100:.2f}% of the account's followers")
        outp = _ratio(row.get("outperformance"), 2)
        add("vs this account's own median", outp)
        # The gap the owner's sketch assumed we had. Words, never a zero.
        add(SHARES_LABEL, SHARES_VALUE)
    elif item.stream == STREAM_NEWS:
        add("Published", _day(post_time(row)))
        add("Outlet", str(row.get("platform") or "").strip() or None)
        md = _meta(row)
        add("Why it matters", _one_line(md.get("why_it_matters"), 200) or None)
        # The publisher's own headline, so a reader can see what our plain
        # rewrite was made from without leaving the page.
        add("Headline as published", _one_line(md.get("original_headline"), 160) or None)
        add("Language tag", str(row.get("language") or "").strip() or None)
        add("Engagement", NEWS_ENGAGEMENT_VALUE)
    else:
        m = item.measures()
        if m["posts"] is not None:
            add("Posts", f"{int(m['posts']):,}")
        if m["views"] is not None:
            add("Views", f"{int(m['views']):,}")
        if m["views_per_post"] is not None:
            add("Views per post", f"{int(round(m['views_per_post'])):,}")
        if m["growth"] is not None:
            add("Growth reported by a source", _ratio(m["growth"], 2))
        srcs = _jsonb_list(row.get("sources"))
        add("Sources", ", ".join(srcs) if srcs else None)
        ev = _int(row.get("evidence_count"))
        if ev is not None:
            add("Evidence sightings", f"{ev:,}")
        add("Heat", _heat_text(row))
        regions = _jsonb_list(row.get("regions"))
        add("Seen in", ", ".join(regions) if regions else "not reported")
        add("First seen", _day(trend_first_seen(row)))
        add("Last seen", _day(trend_last_seen(row)))

    # Never measured, on every stream.
    add(SENTIMENT_METRIC_LABEL, SENTIMENT_METRIC_VALUE)
    return out


def _item_links(item: ItemData) -> list[dict]:
    """Everything this item can be checked against, deduplicated."""
    out: list[dict] = []
    seen = {item.link or ""}

    def add(label: str, url: Any) -> None:
        text = str(url or "").strip()
        if text and text not in seen:
            seen.add(text)
            out.append({"label": label, "url": text})

    if item.stream == STREAM_COMPETITOR:
        user = str(item.row.get("username") or "").strip().lstrip("@")
        if user:
            add("Profile", f"https://www.instagram.com/{user}/")
    elif item.stream == STREAM_NEWS:
        # Nothing to add: the article's own address is already the item's link.
        pass
    else:
        for s in item.sightings[:6]:
            add(str(s.get("source") or "evidence"), s.get("url"))
        for src in (item.research or {}).get("sources") or []:
            if isinstance(src, dict):
                add(_one_line(src.get("title"), 40) or "research source",
                    src.get("uri") or src.get("url"))
    return out


def build_item_overview(item: ItemData) -> dict:
    """The one-item brief: what it is, what it measured, what is missing from
    it, where to check it, and the stored angle."""
    provenance = {
        STREAM_COMPETITOR: (
            "Likes, comments, views and engagement come straight from Instagram "
            "Business Discovery on the account's own media; outperformance is "
            "that engagement divided by this account's own median across every "
            "post we have scanned of theirs"),
        STREAM_NEWS: (
            "Outlet, publish time and region come from the feed entry itself; "
            "'outlets covering it' is a count of the feeds that carried the "
            "same story"),
        STREAM_VIRAL: (
            "Posts, views, views per post and any growth ratio are the numbers "
            "the source itself published on this hashtag; heat is computed from "
            "those counters, the source's rank and the age of the content. "
            "Where a source publishes a separate board per region, each of "
            "those figures is the LARGEST single regional reading, taken "
            "independently: they are NOT a sum across regions (the boards "
            "overlap, and adding them would double-count) and they need not "
            "divide into one another"
            + (f" - all {len(item.sightings)} regional reading(s) are stored as "
               "sightings on this trend and the full report prints every one"
               if item.sightings else "")),
    }[item.stream]
    return _section(
        "item_overview", "The Item", kind="signals",
        basis=(f"{provenance}. The title line and the suggestion are the AI "
               f"scorer's words; every figure above is a stored measurement. "
               f"{SENTIMENT_METRIC_LABEL} is {SENTIMENT_METRIC_VALUE}."
               + (f" {SHARES_LABEL} are {SHARES_VALUE}."
                  if item.stream == STREAM_COMPETITOR else "")),
        body={"groups": [{
            "group": item.stream_label,
            "items": [{
                "name": item.label,
                "link": item.link,
                "extra_links": _item_links(item),
                "metrics": _item_metrics(item),
                "sentiment": item.tone_label(),
                "suggestion": _suggestion(item.row),
            }],
        }]},
        estimated=True,
    )


def build_item_what_it_is(item: ItemData) -> dict:
    """What the thing actually is, in the words we hold — never a fresh
    description written for this report."""
    paragraphs: list[str] = []
    sources: list[str] = []

    stored = _one_line(item.row.get("summary"), 400) if item.stream == STREAM_VIRAL \
        else ""
    if stored:
        paragraphs.append(stored)
        sources.append("the one-line summary the collector stored from the source")

    if item.stream != STREAM_VIRAL:
        caption = _WS_RE.sub(" ", str(item.row.get("caption") or "")).strip()
        if caption:
            paragraphs.append(caption[:900])
            sources.append("the publisher's own text as collected (caption or "
                           "headline), quoted, not summarised")

    topic = str(item.row.get("topic") or "").strip()
    summary = str(item.row.get("topic_summary") or "").strip()
    ai_bits = " ".join(x for x in (topic, summary) if x)
    if ai_bits:
        paragraphs.append(f"AI read of it: {ai_bits}")
        sources.append("the AI scorer's stored topic and topic summary")

    research = item.research if isinstance(item.research, dict) else None
    if research and str(research.get("what_it_is") or "").strip():
        paragraphs.append("Web research: "
                          + str(research.get("what_it_is")).strip()[:900])
        why = str(research.get("why_people_post") or "").strip()
        if why:
            paragraphs.append("Why people post it: " + why[:600])
        when = _day(research.get("researched_at"))
        sources.append(f"a Google-grounded lookup cached on this trend"
                       + (f" ({when})" if when != "unknown" else "")
                       + " - its sources are listed in the next section")

    return _section(
        "item_what_it_is", "What It Is", kind="prose",
        basis=("Assembled from " + "; ".join(sources) + "."
               if sources else
               "Nothing describing this item is stored anywhere."),
        body={"paragraphs": paragraphs}, available=bool(paragraphs),
        unavailable_reason=(None if paragraphs else
                            "No summary, caption or AI read is stored for this "
                            "item, and nothing has been written for it here."),
        estimated=True,
    )


def build_item_research(item: ItemData) -> dict:
    """The cached web lookup's sources, so the description above can be
    followed rather than trusted."""
    research = item.research if isinstance(item.research, dict) else None
    rows: list[list[str]] = []
    for src in (research or {}).get("sources") or []:
        if not isinstance(src, dict):
            continue
        url = str(src.get("uri") or src.get("url") or "").strip()
        rows.append([_one_line(src.get("title"), 60) or "untitled source",
                     _link_text(url)])
    when = _day((research or {}).get("researched_at"))
    body = _table(["Source", "Link"], rows, ["left", "left"])
    if research and str(research.get("what_it_is") or "").strip():
        reason = ("The cached lookup found a description but listed no sources "
                  "for it, so there is nothing here to check it against.")
    else:
        reason = ("No web research is cached for this trend. That lookup runs "
                  "when someone presses Brainstorm or Make this on it - opening "
                  "a report does not start one, and nothing has been invented "
                  "in its place.")
    return _section(
        "item_research", "Web Research Sources", kind="table",
        basis=(f"The sources a Google-grounded lookup returned for this trend"
               + (f", cached {when}" if when != "unknown" else "")
               + ". The lookup is a model reading search results: treat the "
                 "description in the previous section as a claim these links "
                 "support, not as a measurement."),
        body=body, available=bool(rows),
        unavailable_reason=(None if rows else reason),
        estimated=True,
    )


def build_item_ranking(item: ItemData) -> dict:
    """WHERE IT SITS IN THE WINDOW — the analysis a screenshot cannot give you.

    Measured columns only. The AI's opportunity score would make a tidier
    single ranking and it is a model's opinion, so it is not ranked here; it is
    printed, labelled, in the AI read."""
    peers = item.peers
    noun = item.peer_noun
    items: list[dict] = []

    if item.stream == STREAM_COMPETITOR:
        items.append(rank_kpi(
            "Rank by engagement", rank_within(peers, item.row, "engagement"),
            noun=noun, unit="likes + comments count"))
        items.append(rank_kpi(
            "Rank by outperformance", rank_within(peers, item.row, "outperformance"),
            noun=noun, unit="ratio against its own account's median", digits=2))
        if _int(item.row.get("view_count")) is not None:
            items.append(rank_kpi(
                "Rank by views", rank_within(peers, item.row, "view_count"),
                noun=noun, unit="view count"))
        account = item.account or {}
        median = _num(account.get("median_engagement"))
        if median is not None:
            scanned = _int(account.get("posts_found"))
            note = (f"{_handle(account.get('username'))}'s median engagement "
                    "across every post we have scanned of theirs"
                    + (f" ({scanned:,} of them)" if scanned else ""))
            items.append(_kpi("Its account's own median engagement",
                              f"{median:,.0f}", note=note))
        elif item.row.get("username"):
            items.append(_kpi(
                "Its account's own median engagement", "not recorded",
                note=(f"{_handle(item.row.get('username'))} has no stored median "
                      "yet, so the outperformance above is the only comparison "
                      "against it")))
    elif item.stream == STREAM_NEWS:
        # "Rank by outlets carrying the story" used to lead here. A search
        # cannot count outlets, so recency is now the ONLY measured dimension a
        # story has, and printing a second one would mean inventing it.
        recency = rank_within(
            peers, item.row, "posted_at",
            value_of=lambda r: (post_time(r).timestamp()
                                if post_time(r) is not None else None))
        if recency["rank"] is not None:
            items.append(_kpi(
                "Rank by recency",
                f"{_ordinal(recency['rank'])} of {recency['total']:,}",
                note=(f"among the {recency['total']:,} {noun} in the window "
                      f"carrying a date; this one published "
                      f"{_day(post_time(item.row))}, newest first")))
    else:
        items.append(rank_kpi(
            "Rank by heat", rank_within(peers, item.row, "velocity"),
            noun=noun, unit="measured heat score", digits=2))
        items.append(rank_kpi(
            "Rank by evidence sightings",
            rank_within(peers, item.row, "evidence_count"),
            noun=noun, unit="count of distinct sightings"))
        items.append(rank_kpi(
            "Rank by number of sources",
            rank_within(peers, item.row, "source_count"),
            noun=noun, unit="count of public sources that listed it"))

    if item.in_population:
        reason = None
    elif item.dated_in_window:
        reason = (
            "This item is inside the window by date but is not part of the "
            "population the report ranks: the ingest safety screen or the "
            "scorer's 'avoid' verdict holds it back, and ranking it against "
            "rows that were not held back would compare two different things.")
    else:
        # Say exactly which window WOULD hold it. "Ask for a longer window" on
        # its own leaves the reader guessing at a number this code already
        # knows, and the answer is sometimes "no window can" (the cap is 90).
        gap = ((item.data.win_to - item.clock).days + 1) if item.clock else None
        fix = ("There is no window that reaches it: the report caps at 90 days."
               if gap is None or gap > 90 else
               f"A window of {_dl(gap)} would reach it.")
        reason = (
            f"This item is dated {_day(item.clock)}, outside the "
            f"{_range(item.data.win_from, item.data.win_to)} window this report "
            f"ranks against. {fix} Nothing here will guess at a position.")

    return _section(
        "item_ranking", "Where It Sits In The Window", kind="kpis",
        basis=(f"This item against the {len(peers):,} {noun} collected in "
               f"{_range(item.data.win_from, item.data.win_to)}, on stored "
               "measured columns only. Rows with no value for a column are left "
               "out of that column's population, so each line states its own "
               "denominator. Ties share the better position. The AI's "
               "opportunity score is deliberately NOT ranked here - it is a "
               "judgement, and it is printed as one in the AI read."),
        body={"items": items}, available=bool(items) and reason is None,
        unavailable_reason=reason or (
            None if items else "There is nothing measured on this item to rank."),
        estimated=False,
    )


def build_item_timeline(item: ItemData) -> dict:
    """Every clock we hold on this item, and whose clock it is."""
    rows: list[list[str]] = []

    def add(event: str, value: Any, where: str) -> None:
        when = _as_dt(value)
        if when is not None:
            rows.append([event, _day(when), where])

    row = item.row
    if item.stream == STREAM_VIRAL:
        add("First seen by us", row.get("first_seen_at"),
            "the first collector run that listed this trend")
        add("Last seen by us", row.get("last_seen_at"),
            "the most recent run that still listed it")
        add("Newest content behind it", row.get("latest_content_at"),
            "the newest publish date across its sightings")
        published = [_as_dt(s.get("published_at")) for s in item.sightings]
        published = [p for p in published if p is not None]
        if published:
            add("Earliest sighting published", min(published),
                "the source's own publish date on the oldest sighting")
        add("Scored by the AI", row.get("scored_at"),
            "when the scoring pass last ran on it")
    else:
        add("Published", row.get("posted_at"),
            "the publisher's own timestamp"
            if item.stream == STREAM_COMPETITOR else
            "the feed entry's date")
        add("First collected by us", row.get("created_at"),
            "when our collector first stored the row")
        add("Counters last refreshed", row.get("updated_at"),
            "when a later scan last rewrote this row")
        add("Scored by the AI", row.get("scored_at"),
            "when the scoring pass last ran on it")

    body = _table(["Event", "When", "Whose clock this is"], rows,
                  ["left", "right", "left"])
    caveat = ("A trend's 'first seen' is when OUR collector first saw it, not "
              "when it started on the internet - the collector cannot see back "
              "past its own first run."
              if item.stream == STREAM_VIRAL else
              "A collection time is not a publish time; where a feed gives no "
              "date, the report falls back to collection time and says so.")
    return _section(
        "item_timeline", "Timeline", kind="table",
        basis=f"Stored timestamps on this row, unchanged. {caveat}",
        body=body, available=bool(rows),
        unavailable_reason=(None if rows else
                            "This row carries no usable timestamp at all."),
        estimated=False,
    )


def build_item_ai_read(item: ItemData) -> dict:
    """Everything a model said about this item, in one clearly-labelled block."""
    row = item.row
    md = _meta(row)
    rows: list[list[str]] = []

    def add(label: str, value: Any) -> None:
        text = str(value if value is not None else "").strip()
        if text:
            rows.append([label, text])

    fit = _int(row.get("fit_score"))
    add("Fit for 42", f"{fit}/100" if fit is not None else "")
    add("Why it fits (or does not)", _one_line(row.get("fit_reason"), 300))
    rel = _int(row.get("uae_relevance"))
    add("UAE relevance", f"{rel}/100" if rel is not None else "")
    add("Theme", row.get("theme"))
    if item.stream == STREAM_VIRAL:
        add("Type of trend", row.get("trend_kind"))
    add("Suggested format", row.get("suggested_format"))
    add("Effort to make", row.get("effort"))
    score = _num(row.get("opportunity_score"))
    add("Opportunity score", f"{score:.2f} of 100" if score is not None else "")
    if item.stream == STREAM_VIRAL:
        add("Sensitivity verdict", row.get("sensitivity"))
        safety = str(row.get("safety") or "").strip()
        if safety:
            reason = _one_line(row.get("safety_reason"), 220)
            add("Ingest safety screen", safety + (f" - {reason}" if reason else ""))
        add("How 42 would replicate it", _one_line(md.get("replication"), 300))
        add("Scorer's confidence", md.get("confidence"))
        add("Scorer's read of the evidence", md.get("evidence_quality"))

    body = _table(["What the AI said", "Value"], rows, ["left", "left"])
    return _section(
        "item_ai_read", "The AI Read", kind="table",
        basis=("Every line here was written by a model, not measured. It is the "
               "verdict stored with this row when the scoring pass last ran"
               + (f" ({_day(row.get('scored_at'))})"
                  if _as_dt(row.get("scored_at")) else "")
               + ". A score out of 100 is an opinion expressed as a number; it "
                 "is not a measurement of anything that happened."),
        body=body, available=bool(rows),
        unavailable_reason=(None if rows else
                            "The AI scoring pass has not run on this item, so "
                            "there is no judgement to show and none has been "
                            "produced for this report."),
        estimated=True,
    )


def build_item_evidence(item: ItemData) -> dict:
    """Every sighting behind this trend — the same list the detail dialog
    shows, from the same query, capped at the same number."""
    rows: list[list[str]] = []
    for s in item.sightings:
        metric = ""
        value = _num(s.get("metric_value"))
        if value is not None:
            label = str(s.get("metric_label") or "").strip()
            metric = f"{value:,.0f}" + (f" {label}" if label else "")
        dates = " / ".join(x for x in (_short_day(s.get("published_at")),
                                       _short_day(s.get("seen_at"))) if x != "?")
        rows.append([
            str(s.get("source") or "unknown source"),
            _one_line(s.get("title"), 46) or "no title given",
            metric or "no counter published",
            str(s.get("region") or "-"),
            dates or "-",
            _link_text(s.get("url"), 40),
        ])
    body = _table(["Source", "What it saw", "Metric", "Region",
                   "Published / recorded", "Link"],
                  rows, ["left", "left", "right", "left", "right", "left"])
    counted = _int(item.row.get("evidence_count"))
    mismatch = ""
    if counted is not None and counted != len(rows):
        mismatch = (f" The row's own evidence_count is {counted:,}; the "
                    f"difference is sightings of the same source item across "
                    f"several runs, which are collapsed to one line here.")
    return _section(
        "item_evidence", "Every Sighting", kind="table",
        basis=(f"{len(rows):,} distinct sighting(s), one line each, exactly as "
               "the sources published them - this is the same evidence list the "
               f"trend's detail view shows (capped at {ITEM_EVIDENCE_MAX}). A "
               "blank metric means that source published no counter, not a "
               "zero. Dates are the source's own publish date and the date our "
               f"collector recorded it.{mismatch}"),
        body=body, available=bool(rows),
        unavailable_reason=(None if rows else
                            "No sightings are stored for this trend, which "
                            "means nothing measured is behind it."),
        estimated=False,
    )


def build_item_recommendation(item: ItemData) -> dict:
    """The narrative slot. Same id the narrative pass looks for, so a full item
    report gets a written recommendation and a simple one costs no model call."""
    return _section(
        "recommendations", "What 42 Should Do With This", kind="prose",
        basis=("Written by the report model from the sections above; it may not "
               "cite a figure that is not already printed in them."),
        available=False, unavailable_reason=NARRATIVE_PENDING, estimated=True,
    )


def build_item_basis(item: ItemData) -> dict:
    """What this one-item report stands on, and what it cannot tell you."""
    peers = item.peers
    items = [
        f"This report is about ONE item: {item.label} "
        f"({item.stream_label}, id {item.item_id}). Generated {_day(item.data.now)}.",

        f"It is ranked against the {len(peers):,} {item.peer_noun} collected in "
        f"{_range(item.data.win_from, item.data.win_to)} ({_dl(item.data.days)}). "
        "That window is the whole comparison - nothing here compares it against "
        "the other two streams.",
    ]
    if item.stream == STREAM_COMPETITOR:
        items.append(
            "MEASURED: likes, comments, views, engagement, engagement rate and "
            "outperformance against this account's own median, all from "
            "Instagram Business Discovery.")
        items.append(
            f"NOT MEASURED - shares. The Instagram Graph API does not expose a "
            f"share count for another account's media, so this report says "
            f"'{SHARES_VALUE}' rather than a number. A zero would read as a "
            "measurement.")
    elif item.stream == STREAM_NEWS:
        items.append(
            "MEASURED: the publish time, the outlet, and how many feeds carried "
            "the same story.")
        items.append(
            f"NOT MEASURED - engagement. This is a feed entry: {NEWS_ENGAGEMENT_VALUE}.")
    else:
        items.append(
            "MEASURED: the post and view counts the source itself published, "
            "views per post, the number of sources and distinct sightings, and "
            "the heat computed from those.")
        items.append(
            "The web research, where present, is a model reading Google "
            "results. Its sources are listed so it can be checked; it is not a "
            "measurement.")
    items += [
        "AI JUDGEMENT, not measurement: the topic line, the theme, the fit "
        "score, UAE relevance, the suggested angle and format, the effort, and "
        "any tone shown. Sections built on these are marked 'estimated'.",

        f"NOT MEASURED - {SENTIMENT_METRIC_LABEL.lower()}: "
        f"{SENTIMENT_METRIC_VALUE}. Where a tone appears it is a model reading "
        f"the publisher's own words - {SENTIMENT_DISCLAIMER}.",

        "NOT MEASURED - what anyone said about 42 Abu Dhabi. This system does "
        "not track mentions of the school, on this item or on any other.",
    ]
    items.extend(item.data.load_notes)
    return _section(
        "item_basis", "What This Stands On", kind="bullets",
        basis=("Every number in this report is a stored column or a counter the "
               "source published; this section exists so the rest of it can be "
               "checked rather than believed."),
        body={"items": items}, estimated=False,
    )


ITEM_BUILDERS = {
    "item_overview": build_item_overview,
    "item_what_it_is": build_item_what_it_is,
    "item_research": build_item_research,
    "item_ranking": build_item_ranking,
    "item_timeline": build_item_timeline,
    "item_ai_read": build_item_ai_read,
    "item_evidence": build_item_evidence,
    "recommendations": build_item_recommendation,
    "item_basis": build_item_basis,
}


def build_item_sections(item: ItemData, mode: str) -> list[dict]:
    """The ordered section list for an item report. Ordering lives here only.

    The trend-only sections are DROPPED on a post rather than rendered as
    unavailable: an Instagram post has no sightings and no research cache, so
    "Every Sighting: not enough data" would invite the reader to think one was
    expected and missing."""
    ids = ITEM_SECTION_IDS_FULL if mode == "full" else ITEM_SECTION_IDS_SIMPLE
    return [ITEM_BUILDERS[sid](item) for sid in ids
            if not (sid in ITEM_TREND_ONLY_SECTIONS and item.kind != "trend")]


def build_item_basis_block(item: ItemData) -> dict:
    """The `basis` block: the same shape the all-streams report ships, with the
    counts being the population this item was ranked against."""
    block = build_basis(item.data)
    block["notes"] = [
        f"This report is about ONE {item.stream_label} item: {item.label}.",
        f"The counts here are the population it is ranked against - the "
        f"{item.peer_noun} collected in the window - not a measurement of this "
        f"item.",
    ] + list(block["notes"])
    return block


def assemble_item(item: ItemData, mode: str) -> dict:
    """The whole contract dict for one item. Pure — tests build an `ItemData`
    from fixture rows and call this with no database."""
    mode = mode if mode in REPORT_MODES else "simple"
    data = item.data
    return {
        "meta": {
            # The item's own name IS the title: it is what lands on the cover,
            # in the tab and in the filename, and "Social-Listening Report" on a
            # one-item document would be a promise the document does not keep.
            "title": _one_line(item.label, 90) or "Item report",
            "brand": BRAND,
            "mode": mode,
            "scope": item.scope,
            "scope_label": item.stream_label,
            "item": {
                "kind": item.kind,
                "id": item.item_id,
                "stream": item.stream,
                "stream_label": item.stream_label,
                "label": item.label,
                "slug": item.slug,
                "url": item.link,
            },
            "generated_at": _iso(data.now),
            "window": {"days": data.days, "from": _iso(data.win_from),
                       "to": _iso(data.win_to)},
            "previous_window": {"from": _iso(data.prev_from),
                                "to": _iso(data.prev_to)},
        },
        "basis": build_item_basis_block(item),
        "sections": build_item_sections(item, mode),
    }


# ---------------------------------------------------------------------------
# Loading one item
# ---------------------------------------------------------------------------

def item_tone_targets(item: ItemData) -> list[dict]:
    """ONE text. An item report that spent a 60-item batch on a single row
    would take as long as the whole-window report, which is the feature this
    was asked to replace."""
    row = item.row
    if item.stream == STREAM_COMPETITOR:
        text = row.get("caption") or row.get("topic_summary") or row.get("topic")
    elif item.stream == STREAM_NEWS:
        text = row.get("caption") or _meta(row).get("summary") or row.get("topic")
    else:
        text = row.get("topic_summary") or row.get("summary") or row.get("title")
    text = _WS_RE.sub(" ", str(text or "")).strip()[:TONE_TEXT_CHARS]
    if not text:
        return []
    return [{"key": tone_key(item.stream, row), "text": text}]


async def load_item(*, kind: str, item_id: int, days: int = 30,
                    now: Optional[datetime] = None) -> ItemData:
    """One row, its evidence, and the window it is ranked against.

    Raises LookupError when the row is gone — a report about an item that no
    longer exists must be a 404, never an empty document that looks measured."""
    clean = clean_kind(kind)
    if not clean:
        raise ValueError(f"kind must be one of {', '.join(ITEM_KINDS)}")
    item_id = int(item_id)

    if clean == "post":
        row = await trend_store.get_post(item_id)
        if not row:
            raise LookupError(f"no competitor post or news item with id {item_id}")
        stream = (STREAM_NEWS if str(row.get("source") or "competitor") == "news"
                  else STREAM_COMPETITOR)
        data = await load_data(days, now=now, scope=STREAM_SCOPES[stream],
                               sightings=False)
        account = None
        if stream == STREAM_COMPETITOR:
            user = str(row.get("username") or "").strip().lower().lstrip("@")
            account = next(
                (c for c in data.competitors
                 if str(c.get("username") or "").strip().lower().lstrip("@") == user),
                None)
        return ItemData(kind="post", stream=stream, row=dict(row), data=data,
                        account=account, news_source=None)

    row = await general_trends_store.get_trend(item_id)
    if not row:
        raise LookupError(f"no viral trend with id {item_id}")
    row = dict(row)
    # The detail view's evidence, from the detail view's own query — not a
    # second implementation that could disagree with the dialog on screen.
    sightings = (await general_trends_store.sightings_for(
        [item_id], per_trend=ITEM_EVIDENCE_MAX)).get(item_id, [])
    research = None
    try:                                # a cache read never fails a report
        from . import general_trend_ideation   # late: keeps this module light
        research = general_trend_ideation.cached_research(item_id, row)
    except Exception:                   # noqa: BLE001
        log.warning("[social-report] cached research unreadable for trend #%s",
                    item_id, exc_info=True)
    data = await load_data(days, now=now, scope="viral", sightings=False)
    # `general_trends_store.TREND_COLS` carries no `metric_label`; the window
    # query does. Fill only what is missing, so the store row stays the source
    # of truth for everything it does return.
    peer = next((t for t in data.trend_rows
                 if int(t.get("id") or 0) == item_id), None)
    if peer:
        for key, value in peer.items():
            row.setdefault(key, value)
    data.sightings[item_id] = sightings
    return ItemData(kind="trend", stream=STREAM_VIRAL, row=row, data=data,
                    sightings=sightings, research=research)


async def build_item_report(*, kind: str, item_id: int, mode: str = "simple",
                            days: int = 30, tone: bool = True) -> dict:
    """A report about ONE item, in the same {meta, basis, sections} shape.

    `kind` is 'post' (a competitor post OR a news item — the builder tells them
    apart by the row's own `source`) or 'trend'. `days` is the window this item
    is RANKED against, clamped to 1..90. Raises LookupError when the item does
    not exist."""
    mode = mode if mode in REPORT_MODES else "simple"
    item = await load_item(kind=kind, item_id=item_id, days=days)
    if tone:
        item.tone = await assess_tone(item_tone_targets(item))
    return assemble_item(item, mode)
