"""Social-listening report API — the trend streams as documents.

    POST   /api/reports/generate            ?mode=&days=&scope=&end=&refresh=   JSON
    GET    /api/reports/periods             ?scope=&grain=&mode=&limit=         JSON
    GET    /api/reports/saved               ?scope=&limit=                      JSON
    GET    /api/reports/saved/{id}                                              JSON
    GET    /api/reports/saved/{id}.pdf                                          PDF
    DELETE /api/reports/saved/{id}                                              JSON
    GET  /api/reports/social-listening      ?mode=simple|full&days=1..90&scope=  JSON
    GET  /api/reports/social-listening.pdf  ?mode=..&days=..&scope=..            PDF
    GET  /api/reports/item                  ?kind=post|trend&id=..&mode=&days=   JSON
    GET  /api/reports/item.pdf              ?kind=post|trend&id=..&mode=&days=   PDF

BUILT ONCE, THEN KEPT. The first four routes are the ones the Reports tab uses.
A report is identified by what it is ABOUT — scope, mode, range, day — so asking
twice on the same day returns the same stored document rather than paying for a
second, subtly different one, and the PDF is rendered from those stored bytes so
the download cannot disagree with the page it came from. `/social-listening`
still builds on demand and stores nothing; it is the escape hatch, not the door.

THREE WINDOWS, AND ONLY THE FINISHED ONES. `/generate` takes a width and an
end day, but it does not take any pair of them: the pair has to describe a
whole week (Monday to Sunday), a half month (the 1st to the 15th or the 16th to
the end) or a whole calendar month, and that period has to have finished. See
`services/report_periods`. Anything else is a 422.

Two reasons, and neither is tidiness. An unbounded set of windows produces
documents that cannot be laid beside each other ("posts rose 20%" over what?)
and every one of them costs two model passes. And a period that has not
finished yet is a report about part of it with nothing on the page to say so.

The rule being enforced HERE rather than in the UI is the point: a hand-edited
URL asking for Tuesday to Tuesday gets the same answer as the picker would.
`/periods` is what the picker reads, so the two cannot drift apart.

THREE WIDTHS, TWO DEPTHS, ONE CONTRACT
--------------------------------------
Every one of those four routes answers with the same ``{meta, basis, sections}``
dict, so one PDF renderer and one frontend renderer cover all of them. What
changes is how much of the world the document is about:

* ``scope=all`` — the three streams together (Competitors / Sentiment, Industry
  Signal, Viral Social Trends).
* ``scope=competitors|news|viral`` — ONE stream. Same sections in the same
  order; the ones that stream cannot answer come back ``available: false`` with
  a reason naming the stream they need. They are never dropped and never
  filled from a stream the report does not cover.
* ``/item`` — ONE competitor post, ONE news item or ONE viral trend, opened
  from its detail dialog. Different sections, same shape.

and at each width:

* ``simple`` — the owner's brief.
* ``full`` — the long form: fourteen Meltwater sections for a stream report,
  the evidence table / timeline / AI read / in-window ranking for an item.

THE RULE THIS ROUTER EXISTS TO PROTECT: a report full of invented numbers is
worse than no report, because it is read as measurement. So a window with
nothing in it is a **200 with empty sections**, never a 404 and never a padded
document — "nothing happened in these 30 days" is a valid, useful answer, and
each section states its own basis so the reader can tell a measurement from a
model's judgement. A 404 here means one thing only: the ITEM is gone.

Three services, one pipeline, and every one of them degrades rather than 500s:

    social_report.build_report      counts what is in the database
    social_report.build_item_report the same, for one row
    social_report_narrative         writes the prose sections (one model call;
                                    a failure marks them unavailable, it never
                                    fails the download)
    social_report_pdf.render_pdf    the same dict as a PDF

``social_report`` and ``social_report_pdf`` are imported defensively. They are
built alongside this router, and a plain ``from ... import`` of a module that is
not on disk yet takes the WHOLE backend down at boot — every unrelated endpoint
with it. A missing report service is a 503 on these routes instead.
"""
from __future__ import annotations

import asyncio
import inspect
import logging
import re
from datetime import date, datetime, timedelta, timezone
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Query, Response
from fastapi.concurrency import run_in_threadpool

from ..services import report_periods as periods
from ..services import social_report_narrative as narrative
from ..services import social_report_store as store

router = APIRouter(prefix="/api/reports", tags=["reports"])
log = logging.getLogger("archivist.router.reports")

# See the module docstring: an absent (or broken) sibling service must cost
# these routes, not the process.
try:                                            # pragma: no cover - import shape
    from ..services import social_report        # type: ignore[attr-defined]
except Exception as exc:                        # noqa: BLE001
    social_report = None                        # type: ignore[assignment]
    log.warning("[reports] social_report is unavailable: %s", exc)

try:                                            # pragma: no cover - import shape
    from ..services import social_report_pdf    # type: ignore[attr-defined]
except Exception as exc:                        # noqa: BLE001
    social_report_pdf = None                    # type: ignore[assignment]
    log.warning("[reports] social_report_pdf is unavailable: %s", exc)


# The contract's vocabularies, with local fallbacks so validation still works
# when the data service is missing (the caller gets a 422 for a bad value and a
# 503 for the missing service — two different problems, two different answers).
DEFAULT_MODE = "simple"
FALLBACK_MODES = ("simple", "full")

# These two omit the viral stream because it is switched off at
# `GENERAL_TRENDS_ENABLED` (see social_report.VIRAL_STREAM). They are only
# reached when the service module is missing entirely, and in that case the
# narrower list is the safe one: accepting `scope=viral` here would send the
# request on to a service that no longer knows the word. `_vocab` prefers the
# service's own live tuples, which widen again the moment the switch is flipped.
DEFAULT_SCOPE = "all"
FALLBACK_SCOPES = ("all", "competitors", "news")

FALLBACK_ITEM_KINDS = ("post",)

DEFAULT_DAYS = 30

# How long the narrative pass may take before the report gives up on it.
#
# Measured on this box: a full stream report spent 113 s inside ONE model call,
# with 20 Vertex 429/failover lines in the log — quota contention with another
# job, not a slow prompt. The measured half of that report was ready in under
# four seconds. Waiting minutes to bolt prose onto numbers that already exist is
# the wrong trade, so the wait is bounded and the two written sections degrade
# to "not written" exactly as they already do on a model outage. Everything
# measured is unaffected, which is the whole reason those sections are separate.
NARRATIVE_BUDGET_SEC = 45.0
MIN_DAYS = 1
MAX_DAYS = 90

_SERVICE_MISSING = (
    "the social-listening report service isn't installed on this build — "
    "redeploy the backend"
)
_ITEM_MISSING = (
    "per-item reports aren't installed on this build — redeploy the backend"
)
_COVERAGE_MISSING = (
    "this build of the report service cannot say which days it holds - "
    "redeploy the backend"
)


def _as_day(value: Any) -> Optional[date]:
    """An ISO date string as a date, or None. Never raises: this reads values
    that came back from another service, not from a caller."""
    try:
        return date.fromisoformat(str(value)[:10])
    except (TypeError, ValueError):
        return None

# Header hygiene: the filename comes from another module and lands inside a
# quoted Content-Disposition. Anything outside this set could break the header
# (or inject a second one), so it is replaced rather than trusted.
_FILENAME_SAFE = re.compile(r"[^A-Za-z0-9._-]+")


def _vocab(name: str, fallback: tuple[str, ...]) -> tuple[str, ...]:
    values = getattr(social_report, name, None)
    if isinstance(values, (list, tuple)) and values:
        return tuple(str(v) for v in values)
    return fallback


def _clean_one(value: Optional[str], *, field: str, default: str,
               allowed: tuple[str, ...]) -> str:
    """Validate, don't guess. An unknown value is a 422 naming the real ones.

    An ABSENT value (`?mode=`) is a link a UI built with nothing selected yet,
    so it takes the default; a value that is only whitespace is a typo and 422s.
    `kind` passes an empty default on purpose: it decides which table is read,
    so there is nothing sane to fall back to."""
    text = str(value or default).strip().lower()
    if text not in allowed:
        raise HTTPException(422, f"{field} must be one of {', '.join(allowed)}")
    return text


def _clean_mode(mode: Optional[str]) -> str:
    return _clean_one(mode, field="mode", default=DEFAULT_MODE,
                      allowed=_vocab("REPORT_MODES", FALLBACK_MODES))


def _clean_scope(scope: Optional[str]) -> str:
    return _clean_one(scope, field="scope", default=DEFAULT_SCOPE,
                      allowed=_vocab("REPORT_SCOPES", FALLBACK_SCOPES))


def _clean_kind(kind: Optional[str]) -> str:
    # No default: `kind` decides which table is read, so guessing it would
    # answer a question the caller did not ask.
    return _clean_one(kind, field="kind", default="",
                      allowed=_vocab("ITEM_KINDS", FALLBACK_ITEM_KINDS))


def _clamp_days(days: Any) -> int:
    """Clamp into 1..90 rather than reject.

    This parameter rides in a download URL the user can edit by hand and in a
    bookmark that may outlive the UI that made it; a report over 120 days is a
    report over 90 days, not an error page where a PDF should have been."""
    try:
        value = int(days)
    except (TypeError, ValueError):
        return DEFAULT_DAYS
    return max(MIN_DAYS, min(MAX_DAYS, value))


# How far back a window may be asked to end. The per-day coverage scan reaches
# 400 days (social_report.COVERAGE_DAYS); anything older cannot be offered by
# the picker and is a hand-edited URL.
MAX_END_AGE_DAYS = 400


def _clean_end(end: Optional[str]) -> Optional[date]:
    """An ISO date (YYYY-MM-DD) as the INCLUSIVE last day of the window, or None.

    None is the report that ends today, which is the default and what every
    existing caller gets. The day is inclusive: asking for "the week to 14 Sep"
    covers the 14th, because that is what someone who clicked the 14th in a
    calendar meant. See `make_window(whole_days=True)`.

    Unlike `days`, a bad value here is a 422 rather than a clamp. `days=120`
    still has an obvious intended answer; `end=last-tuesday` does not, and
    quietly serving today's report for it would look like the picker worked."""
    text = str(end or "").strip()
    if not text:
        return None
    try:
        day = date.fromisoformat(text[:10])
    except ValueError as exc:
        raise HTTPException(
            422, "end must be a date as YYYY-MM-DD (the last day of the window)"
        ) from exc

    today = datetime.now(timezone.utc).date()
    if day > today:
        raise HTTPException(422, "end cannot be in the future")
    if (today - day).days > MAX_END_AGE_DAYS:
        raise HTTPException(
            422, f"end cannot be more than {MAX_END_AGE_DAYS} days ago")
    if day == today:
        # "The 7 days ending today" and "the last 7 days" are one set of days,
        # so they must be ONE document. Two keys for them is exactly the
        # duplicate the store exists to avoid.
        return None
    return day


def _window_to(end: Optional[date]) -> str:
    """The day a report is filed under. The store's identity is
    (scope, mode, days, window_to), so this and the date printed on the page
    must be the same day or a report would be rebuilt every time it is asked
    for."""
    return (end or datetime.now(timezone.utc).date()).isoformat()


def _require_period(days: int, end: Optional[date]) -> Any:
    """The finished period this (days, end) pair describes, or a 422 saying
    which windows exist.

    Enforced here and not only in the picker, because the picker is not the
    only way to reach this route: a bookmark, a hand-edited URL and a script
    all arrive at the same function, and "Tuesday to Tuesday" has to get the
    same answer from all four."""
    today = datetime.now(timezone.utc).date()
    period = periods.from_window(days, end or today)
    if period is None:
        raise HTTPException(
            422,
            "a report covers a whole week (Monday to Sunday), the 1st to the "
            "15th, the 16th to the end of the month, or a whole calendar "
            "month. Nothing in between.",
        )
    if not periods.is_complete(period, today):
        raise HTTPException(
            422,
            f"{period.label} hasn't finished yet. A report about part of a "
            f"{periods.GRAIN_LABELS[period.grain]} window would read as if it "
            "were about all of it.",
        )
    return period


def _require(service: Any) -> Any:
    if service is None:
        raise HTTPException(503, _SERVICE_MISSING)
    return service


def _accepted_kwargs(fn: Any) -> Optional[set[str]]:
    """The keyword names ``fn`` will accept, or None when it takes ``**kwargs``
    (or cannot be inspected — a Mock, a partial, a C callable).

    None means "pass everything and let it decide", which is the right answer
    for a test double: doubles usually accept anything, and guessing that they
    do not is how a router starts silently dropping arguments in production."""
    try:
        params = inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return None
    if any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()):
        return None
    return {
        name for name, p in params.items()
        if p.kind in (inspect.Parameter.POSITIONAL_OR_KEYWORD,
                      inspect.Parameter.KEYWORD_ONLY)
    }


async def _call_build(service: Any, *, mode: str, days: int, scope: str,
                      tone: bool = True,
                      end: Optional[date] = None) -> Any:
    """``build_report``, tolerating a service build that predates scopes.

    Same reasoning as the defensive import above: a stale image should cost a
    feature, not every download. A DEFAULT scope falls back to the old
    two-argument call and produces exactly the document the caller asked for. A
    NON-default scope does NOT fall back — answering ``scope=news`` with all
    three streams under a one-stream heading would be precisely the misreading
    this report is built to prevent, so that surfaces as an error instead.

    The two optional keywords are dropped INDEPENDENTLY, and by inspecting the
    signature rather than by catching TypeError: a TypeError raised INSIDE
    ``build_report`` looks identical from out here, so retrying on it would
    silently re-run a real bug as if it were a version mismatch."""
    kwargs: dict[str, Any] = {"mode": mode, "days": days}
    accepted = _accepted_kwargs(service.build_report)

    if accepted is None or "scope" in accepted:
        kwargs["scope"] = scope
    elif scope != DEFAULT_SCOPE:
        raise HTTPException(
            503, "this build of the report service cannot produce a "
                 f"{scope}-only report — redeploy the backend",
        )
    else:
        log.warning("[reports] this build of build_report takes no scope; "
                    "serving the all-streams report")

    # `end` is a correctness one, so it follows `scope` rather than `tone`: a
    # service that cannot take it would return the TRAILING window under the
    # chosen window's dates, and nothing downstream could tell.
    if end is not None:
        if accepted is None or "end" in accepted:
            kwargs["end"] = end
        else:
            raise HTTPException(
                503, "this build of the report service cannot produce a report "
                     "for a past window - redeploy the backend",
            )

    # Whole calendar days: what the date picker means when someone drags across
    # a fortnight. A service too old to know the word gives the rolling window
    # instead, which is the same dates give or take a boundary day, so it is a
    # log rather than a refusal.
    if accepted is None or "whole_days" in accepted:
        kwargs["whole_days"] = True
    else:
        log.warning("[reports] this build of build_report has no whole_days; "
                    "the window will roll from the time of day instead")

    # `tone` is a cost switch, never a correctness one: a service that does not
    # know the word simply spends the model call it always spent.
    if accepted is None or "tone" in accepted:
        kwargs["tone"] = tone
    elif not tone:
        log.warning("[reports] this build of build_report takes no tone flag; "
                    "the fast report will still spend the tone pass")

    return await service.build_report(**kwargs)


async def _build(mode: str, days: int, scope: str = DEFAULT_SCOPE,
                 fast: bool = False, end: Optional[date] = None) -> dict:
    """The assembled report, narrative included.

    An empty window is NOT an error here — ``build_report`` returns sections
    that say why they are empty, and that document is the answer.

    ``fast`` skips BOTH model passes — the tone read inside ``build_report`` and
    the narrative here — and returns the measured document immediately. It
    exists because those two passes are the whole cost: measured assembly is a
    few seconds, and one contended Vertex call has been observed taking 113."""
    service = _require(social_report)
    try:
        report = await _call_build(service, mode=mode, days=days, scope=scope,
                                   tone=not fast, end=end)
    except HTTPException:
        raise
    except Exception as exc:      # noqa: BLE001
        log.exception("[reports] build_report failed (mode=%s days=%s scope=%s)",
                      mode, days, scope)
        raise HTTPException(
            500, f"couldn't assemble the report: {str(exc)[:200]}"
        ) from exc
    if fast:
        return _narrative_skipped(report)
    return await _finish(report)


async def _build_item(kind: str, item_id: int, mode: str, days: int) -> dict:
    """One item's report. A missing row is a 404 — and the ONLY 404 here."""
    service = _require(social_report)
    maker = getattr(service, "build_item_report", None)
    if not callable(maker):
        raise HTTPException(503, _ITEM_MISSING)
    try:
        report = await maker(kind=kind, item_id=item_id, mode=mode, days=days)
    except HTTPException:
        raise
    except LookupError as exc:
        # The item was deleted, or somebody edited the id in the URL. Say which
        # one is missing rather than returning an empty document that looks
        # like a measured "nothing here".
        raise HTTPException(404, str(exc) or f"no {kind} with id {item_id}") from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    except Exception as exc:      # noqa: BLE001
        log.exception("[reports] build_item_report failed (kind=%s id=%s mode=%s)",
                      kind, item_id, mode)
        raise HTTPException(
            500, f"couldn't assemble the report: {str(exc)[:200]}"
        ) from exc
    return await _finish(report)


_NARRATIVE_IDS = ("executive_summary", "recommendations")

_NARRATIVE_TIMEOUT_REASON = (
    "the report model did not answer within {sec:.0f} seconds, so this section "
    "was not written. Everything else in this report is measured and "
    "unaffected — only the prose about it is missing."
)


def _narrative_timed_out(report: Any) -> dict:
    """Mark the two written sections unavailable, leaving every measured
    section exactly as it was.

    Deliberately the SAME shape the narrative pass produces on a model outage,
    because from the reader's side those are the same event: the numbers are
    all here and nobody wrote the summary. Never leave a half-written section
    on screen — an empty Executive Summary reads as "nothing worth saying"."""
    if not isinstance(report, dict):
        return {}
    for section in report.get("sections") or []:
        if not isinstance(section, dict) or section.get("id") not in _NARRATIVE_IDS:
            continue
        if section.get("available"):
            continue          # it was already written before the clock ran out
        section["available"] = False
        section["unavailable_reason"] = _NARRATIVE_TIMEOUT_REASON.format(
            sec=NARRATIVE_BUDGET_SEC,
        )
    return report


_NARRATIVE_SKIPPED_REASON = (
    "this report was built in fast mode, which skips the two model passes so "
    "the measured numbers arrive immediately. Drop `fast` from the request to "
    "have this section written."
)


def _narrative_skipped(report: Any) -> dict:
    """Same treatment as the timeout, different reason — a reader must be able
    to tell "nobody wrote it because you asked for speed" from "the model was
    down"."""
    if not isinstance(report, dict):
        return {}
    for section in report.get("sections") or []:
        if not isinstance(section, dict) or section.get("id") not in _NARRATIVE_IDS:
            continue
        if section.get("available"):
            continue
        section["available"] = False
        section["unavailable_reason"] = _NARRATIVE_SKIPPED_REASON
    return report


async def _finish(report: Any) -> dict:
    if not isinstance(report, dict):
        raise HTTPException(500, "the report service returned an unusable report")
    # write_narrative never raises and never fails the download — a model
    # outage costs a section, not the report. A model that is merely SLOW is the
    # same problem wearing a different hat, so it is bounded the same way.
    try:
        return await asyncio.wait_for(
            narrative.write_narrative(report), timeout=NARRATIVE_BUDGET_SEC,
        )
    except asyncio.TimeoutError:
        log.warning("[reports] narrative exceeded %.0fs — serving the measured "
                    "report without it", NARRATIVE_BUDGET_SEC)
        return _narrative_timed_out(report)


def _filename(report: dict, mode: str) -> str:
    """A safe attachment name. Falls back to a computed one, because a PDF that
    downloads as ``download`` is still a working PDF."""
    fallback = f"42-social-listening-{mode}-{date.today().isoformat()}.pdf"
    raw = ""
    try:
        maker = getattr(social_report_pdf, "suggested_filename", None)
        if callable(maker):
            raw = str(maker(report) or "")
    except Exception:             # noqa: BLE001 — never lose a rendered PDF to its name
        log.warning("[reports] suggested_filename failed", exc_info=True)
    name = _FILENAME_SAFE.sub("-", raw).strip("-")
    if not name:
        return fallback
    return name if name.lower().endswith(".pdf") else f"{name}.pdf"


async def _pdf(report: dict, mode: str) -> Response:
    """Render an assembled report and stream it as an attachment."""
    renderer = _require(social_report_pdf)
    try:
        # reportlab is synchronous and a full report is real CPU work, so it
        # runs off the event loop. `isawaitable` keeps an async renderer
        # working too — the contract says sync, this costs one line to not care.
        pdf = await run_in_threadpool(renderer.render_pdf, report)
        if inspect.isawaitable(pdf):
            pdf = await pdf
    except Exception as exc:      # noqa: BLE001
        log.exception("[reports] render_pdf failed (mode=%s)", mode)
        raise HTTPException(
            500, f"couldn't render the report PDF: {str(exc)[:200]}"
        ) from exc
    if not isinstance(pdf, (bytes, bytearray)):
        raise HTTPException(500, "the PDF renderer returned no document")
    return Response(
        content=bytes(pdf),
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'attachment; filename="{_filename(report, mode)}"',
            # The window, the scope and the item all ride in query parameters,
            # so a proxy must not serve one report where another was asked for.
            "Cache-Control": "no-store",
        },
    )


# ---------------------------------------------------------------------------
# Routes — stream reports
# ---------------------------------------------------------------------------

@router.get("/social-listening")
async def social_listening(
    mode: str = Query(default=DEFAULT_MODE),
    days: int = Query(default=DEFAULT_DAYS),
    scope: str = Query(default=DEFAULT_SCOPE),
    fast: bool = Query(
        default=False,
        description="skip both model passes and return the measured "
                    "report immediately; the two written sections say so",
    ),
) -> dict:
    """The report as the dict the PDF renderer and the UI both consume.

    ``sections`` is ordered — render it in order. A section with
    ``available: false`` carries the reason it is empty and MUST be shown as
    "not enough data", not skipped: the gap is part of the finding, and at a
    narrow ``scope`` the reason is usually "this section measures another
    stream", which is exactly what the reader needs to know."""
    return await _build(_clean_mode(mode), _clamp_days(days), _clean_scope(scope),
                        fast=fast)


@router.get("/social-listening.pdf")
async def social_listening_pdf(
    mode: str = Query(default=DEFAULT_MODE),
    days: int = Query(default=DEFAULT_DAYS),
    scope: str = Query(default=DEFAULT_SCOPE),
    fast: bool = Query(
        default=False,
        description="skip both model passes and return the measured "
                    "report immediately; the two written sections say so",
    ),
) -> Response:
    """The same report, rendered. Streams the bytes as an attachment."""
    clean_mode = _clean_mode(mode)
    clean_scope = _clean_scope(scope)
    _require(social_report_pdf)
    report = await _build(clean_mode, _clamp_days(days), clean_scope, fast=fast)
    return await _pdf(report, clean_mode)


# ---------------------------------------------------------------------------
# Routes — reports that are kept
#
# A report is BUILT when someone asks for one, and then it is kept. The tab used
# to build two on mount and two more on every window change, which is why the
# same document was being paid for over and over; now the tab lists what exists
# and builds nothing until a button is pressed.
#
# The identity of a report is (scope, mode, days, window_to) — see
# social_report_store. Asking twice on the same day for the same range returns
# the SAME stored document rather than a second, subtly different one, unless
# `refresh=true` says otherwise.
# ---------------------------------------------------------------------------

@router.get("/periods")
async def report_periods(
    scope: str = Query(default=DEFAULT_SCOPE),
    grain: str = Query(default=periods.DEFAULT_GRAIN,
                       description="week | half | month"),
    mode: str = Query(default="full"),
    limit: int = Query(default=12, ge=1, le=52),
) -> dict:
    """Every window of this shape that can be asked for, newest first.

    This is the whole menu, and there is nothing else on it. Each entry says
    how many records fall inside it and, when one has already been built, the
    id of that report, so the picker can send someone to the document instead
    of offering to build a second copy of it.

    Only FINISHED periods appear. A week that is still running would produce a
    report about part of a week, and the numbers would not say so."""
    service = _require(social_report)
    reader = getattr(service, "data_coverage", None)
    if not callable(reader):
        raise HTTPException(503, _COVERAGE_MISSING)

    clean_scope = _clean_scope(scope)
    clean_grain = periods.clean_grain(grain)
    clean_mode = _clean_mode(mode)
    try:
        coverage = await reader(clean_scope)
    except Exception as exc:      # noqa: BLE001
        log.exception("[reports] data_coverage failed (scope=%s)", scope)
        raise HTTPException(
            500, f"couldn't read the report coverage: {str(exc)[:200]}"
        ) from exc

    today = _as_day(coverage.get("today")) or date.today()
    since = _as_day(coverage.get("from"))
    counts = {row["date"]: int(row["count"] or 0)
              for row in coverage.get("days") or []
              if isinstance(row, dict) and row.get("date")}

    # Which of them are already built. One index read, not one per period.
    #
    # Keyed on BOTH ends, because a rolling 7-day window ending on a Sunday has
    # the width and the last day of a week without being one. Claiming a report
    # like that as the week's would send a reader to a document built from part
    # of it.
    built: dict[tuple, int] = {}
    for row in await store.index(clean_scope, 200):
        if row.get("mode") != clean_mode:
            continue
        key = (int(row.get("days") or 0),
               str(row.get("window_from") or "")[:10],
               str(row.get("window_to") or "")[:10])
        built.setdefault(key, int(row["id"]))

    items = []
    for period in periods.recent(clean_grain, today=today, limit=limit,
                                 since=since):
        blob = period.as_dict()
        day = period.start
        total = 0
        while day <= period.end:
            total += counts.get(day.isoformat(), 0)
            day += timedelta(days=1)
        blob["records"] = total
        blob["report_id"] = built.get(
            (period.days, period.start.isoformat(), period.end.isoformat()))
        items.append(blob)

    return {
        "scope": clean_scope, "grain": clean_grain, "mode": clean_mode,
        "today": today.isoformat(), "items": items,
    }


@router.get("/saved")
async def saved_reports(
    scope: str = Query(default=""),
    limit: int = Query(default=40, ge=1, le=200),
) -> dict:
    """Every report already built, newest first, WITHOUT the documents.

    This is what the tab loads when it opens, so it stays an index: ids, windows
    and timestamps. Nothing here costs a model call, and an empty list is the
    normal state of a fresh install, not an error.

    Each row carries a `label`: the period it covers, or, for a report built
    before the three shapes existed, a plain description of its dates. A row
    labelled only by raw dates reads as something random that got downloaded by
    accident, which is exactly how these looked."""
    clean = _clean_scope(scope) if scope else None
    rows = await store.index(clean, limit)
    for row in rows:
        days = int(row.get("days") or 0)
        start, end = _as_day(row.get("window_from")), _as_day(row.get("window_to"))
        row["label"] = periods.describe(days, end, start)
        row["period"] = periods.from_row(days, start, end) is not None
    return {"items": rows}


# DECLARED BEFORE the bare "/saved/{report_id}", and it has to be.
# FastAPI matches in declaration order, and "/saved/5.pdf" matches the
# bare path first, where int("5.pdf") raises a 422 that does NOT fall
# through to this route. The narrower path goes first.
@router.get("/saved/{report_id}.pdf")
async def saved_report_pdf(report_id: int) -> Response:
    """The stored report, rendered.

    THE SAME BYTES the screen read. The download used to rebuild the document
    from scratch, so the PDF could disagree with the page it was downloaded
    from: two builds minutes apart, two model passes, two different sets of
    written sections. Rendering the stored payload makes that impossible."""
    _require(social_report_pdf)
    row = await store.by_id(report_id)
    if not row or not isinstance(row.get("payload"), dict):
        raise HTTPException(404, "that report doesn't exist")
    return await _pdf(row["payload"], str(row.get("mode") or DEFAULT_MODE))


@router.get("/saved/{report_id}")
async def saved_report(report_id: int) -> dict:
    """One stored report, as the same `{meta, basis, sections}` dict the builder
    produced. A 404 here means that report was never built or has been removed."""
    row = await store.by_id(report_id)
    if not row or not isinstance(row.get("payload"), dict):
        raise HTTPException(404, "that report doesn't exist")
    return row


@router.delete("/saved/{report_id}")
async def delete_saved_report(report_id: int) -> dict:
    """Forget one stored report.

    The table outlived two changes to what a window may be, so it holds
    documents whose dates match nothing on the menu any more, and a row like
    that reads as something random that got downloaded by accident. Deleting
    one costs nothing that cannot be rebuilt, as long as its period has
    finished, which every period on offer has."""
    gone = await store.delete(report_id)
    if not gone:
        raise HTTPException(404, "that report doesn't exist")
    log.info("[reports] deleted stored report #%s", report_id)
    return {"deleted": report_id}


@router.post("/generate")
async def generate_report(
    mode: str = Query(default="full"),
    days: int = Query(default=DEFAULT_DAYS),
    scope: str = Query(default=DEFAULT_SCOPE),
    end: str = Query(
        default="",
        description="the last day of the window as YYYY-MM-DD; omit for a "
                    "window ending today",
    ),
    refresh: bool = Query(
        default=False,
        description="build a new one even though this window's already exists",
    ),
) -> dict:
    """Build a report for this window, or hand back the one already built.

    `days` is how WIDE the window is and `end` is where it ends, so "7 days"
    and "the 7 days to 14 Sep" are two different documents. Omitting `end`
    means the window ends now, which is what every caller got before windows
    could be chosen.

    `cached: true` says nothing was rebuilt: the same document was already on
    disk for this scope, mode, range and end day, and the caller is holding
    it. The tab shows that as "already generated" instead of spending two
    model passes to produce a near-identical page. A PAST window is the
    strongest case for it, because its numbers cannot change any more, so a
    rebuild could only reword them.

    `refresh=true` is the explicit override, for when the collectors have run
    since and the reader wants today's numbers again."""
    clean_mode = _clean_mode(mode)
    clean_scope = _clean_scope(scope)
    clean_days = _clamp_days(days)
    clean_end = _clean_end(end)
    period = _require_period(clean_days, clean_end)
    window_to = _window_to(clean_end)

    if not refresh:
        existing = await store.find(clean_scope, clean_mode, clean_days,
                                    window_to)
        if existing and isinstance(existing.get("payload"), dict):
            log.info("[reports] serving stored report #%s (scope=%s days=%s "
                     "to=%s)", existing.get("id"), clean_scope, clean_days,
                     window_to)
            return {"cached": True, **existing}

    log.info("[reports] building %s (%s, scope=%s)", period.label, period.key,
             clean_scope)
    report = await _build(clean_mode, clean_days, clean_scope, fast=False,
                          end=clean_end)
    saved = await store.save(clean_scope, clean_mode, clean_days, report)
    if saved:
        return {"cached": False, **saved}
    # Storage failed (see store.save — it never raises). The document is still
    # good and the caller still gets it; only the reuse is lost.
    return {
        "cached": False, "id": None, "scope": clean_scope, "mode": clean_mode,
        "days": clean_days, "window_to": window_to, "payload": report,
    }


# ---------------------------------------------------------------------------
# Routes — one item
# ---------------------------------------------------------------------------

@router.get("/item")
async def item_report(
    kind: str = Query(default=""),
    item_id: int = Query(default=..., alias="id"),
    mode: str = Query(default=DEFAULT_MODE),
    days: int = Query(default=DEFAULT_DAYS),
) -> dict:
    """One competitor post, one news item or one viral trend as a report.

    ``kind=post`` covers BOTH competitor posts and news items — they are rows of
    the same table and the builder tells them apart by the row's own ``source``,
    so a caller holding an id from the feed does not have to know which it has.
    ``days`` is the window this item is RANKED against, not a filter on it: an
    item outside the window still reports, and says it could not be placed."""
    return await _build_item(_clean_kind(kind), int(item_id),
                             _clean_mode(mode), _clamp_days(days))


@router.get("/item.pdf")
async def item_report_pdf(
    kind: str = Query(default=""),
    item_id: int = Query(default=..., alias="id"),
    mode: str = Query(default=DEFAULT_MODE),
    days: int = Query(default=DEFAULT_DAYS),
) -> Response:
    """The same one-item report, rendered."""
    clean_mode = _clean_mode(mode)
    clean_kind = _clean_kind(kind)
    _require(social_report_pdf)
    report = await _build_item(clean_kind, int(item_id), clean_mode,
                               _clamp_days(days))
    return await _pdf(report, clean_mode)
