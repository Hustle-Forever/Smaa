"""Reports are built once, kept, and downloaded as the thing you were shown.

Three promises live here, and every one of them was broken before:

* **Opening the tab must not build anything.** The panel used to fire two
  builds on mount and two more on every window change, each full build costing
  two model passes. `GET /saved` is what the tab loads now, and this suite
  fails if that route ever reaches the builder.
* **The same report is not built twice.** A report is identified by what it is
  about — scope, mode, range, day — not by when someone clicked. The second
  ask on the same day must come back `cached: true` WITHOUT calling the
  builder, because two builds of "the same" report produce two different sets
  of written sections and the numbers appear to shift under the reader.
* **The download is the document on screen.** The PDF route used to rebuild
  from scratch, so the file and the page it came from were separate builds
  minutes apart. `/saved/{id}.pdf` must render the STORED payload, and this
  suite asserts the renderer is handed those exact bytes.

Driven through the real ASGI app with the data service, the PDF renderer and
the store all faked, so this exercises routing, ordering and the cache decision
with no database, no model and no PDF engine.
"""
from __future__ import annotations

import asyncio
import sys
import types
from datetime import date, timedelta
from pathlib import Path

import pytest

_BACKEND = Path(__file__).resolve().parent.parent
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from fastapi.testclient import TestClient  # noqa: E402

from app.routers import reports as rt  # noqa: E402
from app.services import report_periods as rp  # noqa: E402


TODAY = date.today().isoformat()

# The only windows `/generate` accepts are finished calendar periods, so the
# fixtures are built from the real ones rather than from hand-picked dates that
# would stop being periods as the calendar moves. See services/report_periods.
MONTH = rp.recent("month", today=date.today(), limit=1)[0]
WEEK, PREV_WEEK = rp.recent("week", today=date.today(), limit=2)
HALF = rp.recent("half", today=date.today(), limit=1)[0]


def q(period, **extra) -> str:
    """The query string for one period, plus whatever else a test needs."""
    parts = [f"days={period.days}", f"end={period.end.isoformat()}"]
    parts += [f"{k}={v}" for k, v in extra.items()]
    return "&".join(parts)


def _report(mode: str = "full", marker: str = "first", *,
            days: int = 30, end: str = TODAY) -> dict:
    """A minimal envelope. `marker` makes two builds of the same window
    distinguishable, which is how the cache assertions tell them apart.

    `end` is carried in `meta.window.to` because that is where the store reads
    a report's identity from: the real builder prints the window it measured,
    and the key is taken from the document rather than recomputed, so the two
    cannot drift apart."""
    win_from = (date.fromisoformat(end) - timedelta(days=days - 1)).isoformat()
    return {
        "meta": {
            "title": "SMAA Social Intelligence", "brand": "42 Abu Dhabi",
            "mode": mode, "generated_at": f"{TODAY}T09:00:00+00:00",
            "window": {"days": days, "from": win_from, "to": end,
                       "chosen": end != TODAY},
            "previous_window": {"from": "2026-07-22", "to": "2026-08-21"},
            "scope": "all", "scope_label": "Both streams",
        },
        "basis": {"competitor_handles": 2, "competitor_posts": 20,
                  "news_items": 385, "viral_trends": None, "notes": []},
        "sections": [
            {"id": "executive_summary", "title": "What matters now",
             "available": True, "unavailable_reason": None,
             "basis": "written from the sections below", "estimated": True,
             "kind": "prose", "body": {"paragraphs": [marker]}},
        ],
    }


class _Store:
    """The store, in a dict. Mirrors the real module's identity rule:
    (scope, mode, days, window_to) is one document."""

    def __init__(self) -> None:
        self.rows: dict[tuple, dict] = {}
        self.next_id = 1

    def _key(self, scope, mode, days, window_to):
        return (scope, mode, int(days), window_to)

    async def find(self, scope, mode, days, window_to):
        return self.rows.get(self._key(scope, mode, days, window_to))

    async def save(self, scope, mode, days, report):
        window = (report.get("meta") or {}).get("window") or {}
        window_to = str(window.get("to") or "")[:10]
        key = self._key(scope, mode, days, window_to)
        row = self.rows.get(key)
        if row is None:
            row = {"id": self.next_id, "scope": scope, "mode": mode,
                   "days": int(days), "window_from": str(window.get("from") or "")[:10],
                   "window_to": window_to}
            self.next_id += 1
            self.rows[key] = row
        row["payload"] = report
        row["generated_at"] = f"{TODAY}T09:00:00+00:00"
        return dict(row)

    async def index(self, scope=None, limit=40):
        rows = [r for r in self.rows.values() if scope is None or r["scope"] == scope]
        return [{k: v for k, v in r.items() if k != "payload"} for r in rows][:limit]

    async def by_id(self, report_id):
        for row in self.rows.values():
            if row["id"] == int(report_id):
                return dict(row)
        return None

    async def delete(self, report_id):
        for key, row in list(self.rows.items()):
            if row["id"] == int(report_id):
                del self.rows[key]
                return True
        return False


@pytest.fixture
def client(monkeypatch):
    from app import access
    monkeypatch.setattr(access, "enabled", lambda: False)

    calls: dict = {"builds": 0, "rendered": [], "ends": [], "whole": []}

    async def _build_report(*, mode="simple", days=30, scope="all", tone=True,
                            end=None, whole_days=False):
        calls["builds"] += 1
        calls["ends"].append(end)
        calls["whole"].append(whole_days)
        return _report(mode, marker=f"build-{calls['builds']}", days=days,
                       end=(end.isoformat() if end else TODAY))

    monkeypatch.setattr(
        rt, "social_report",
        types.SimpleNamespace(
            REPORT_MODES=("simple", "full"),
            REPORT_SCOPES=("all", "competitors", "news"),
            build_report=_build_report,
        ),
    )

    def _render(report):
        calls["rendered"].append(report)
        return b"%PDF-1.4 fake"

    monkeypatch.setattr(
        rt, "social_report_pdf",
        types.SimpleNamespace(
            render_pdf=_render,
            suggested_filename=lambda report: "smaa-report.pdf",
        ),
    )

    async def _identity(report):
        return report

    monkeypatch.setattr(rt.narrative, "write_narrative", _identity)

    store = _Store()
    monkeypatch.setattr(rt, "store", store)

    from app.main import app
    c = TestClient(app)
    c.calls = calls          # type: ignore[attr-defined]
    c.store = store          # type: ignore[attr-defined]
    return c


# ---------------------------------------------------------------------------
# Opening the tab builds nothing
# ---------------------------------------------------------------------------

def test_listing_never_reaches_the_builder(client):
    r = client.get("/api/reports/saved")
    assert r.status_code == 200, r.text
    assert r.json()["items"] == []
    assert client.calls["builds"] == 0, "loading the index must not build a report"


def test_the_index_carries_no_payloads(client):
    client.post(f"/api/reports/generate?{q(MONTH, scope='all')}")
    items = client.get("/api/reports/saved").json()["items"]
    assert len(items) == 1
    assert "payload" not in items[0], (
        "the index is what the tab loads on open; shipping every envelope makes "
        "it as expensive as the thing it replaced"
    )
    assert items[0]["days"] == MONTH.days
    assert items[0]["window_to"] == MONTH.end.isoformat()


# ---------------------------------------------------------------------------
# The same report is not built twice
# ---------------------------------------------------------------------------

def test_first_generate_builds_and_stores(client):
    r = client.post(f"/api/reports/generate?{q(MONTH, scope='all')}")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["cached"] is False
    assert client.calls["builds"] == 1
    assert body["payload"]["sections"][0]["body"]["paragraphs"] == ["build-1"]


def test_second_generate_same_day_is_free(client):
    first = client.post(f"/api/reports/generate?{q(MONTH, scope='all')}").json()
    second = client.post(f"/api/reports/generate?{q(MONTH, scope='all')}").json()

    assert second["cached"] is True
    assert client.calls["builds"] == 1, "the second ask must not rebuild"
    assert second["payload"] == first["payload"], (
        "a cached answer that differs from the first is the exact failure this "
        "cache exists to prevent — the numbers appearing to shift between two "
        "looks at the same report"
    )


def test_refresh_rebuilds_on_purpose(client):
    client.post(f"/api/reports/generate?{q(MONTH, scope='all')}")
    again = client.post(f"/api/reports/generate?{q(MONTH, scope='all', refresh='true')}").json()

    assert again["cached"] is False
    assert client.calls["builds"] == 2
    assert again["payload"]["sections"][0]["body"]["paragraphs"] == ["build-2"]


def test_a_different_window_is_a_different_report(client):
    client.post(f"/api/reports/generate?{q(MONTH, scope='all')}")
    client.post(f"/api/reports/generate?{q(WEEK, scope='all')}")
    assert client.calls["builds"] == 2
    assert len(client.get("/api/reports/saved").json()["items"]) == 2


def test_a_different_scope_is_a_different_report(client):
    client.post(f"/api/reports/generate?{q(MONTH, scope='all')}")
    client.post(f"/api/reports/generate?{q(MONTH, scope='competitors')}")
    assert client.calls["builds"] == 2


# ---------------------------------------------------------------------------
# The download is the document on screen
# ---------------------------------------------------------------------------

def test_download_renders_the_stored_payload(client):
    made = client.post(f"/api/reports/generate?{q(MONTH, scope='all')}").json()
    # A rebuild between the view and the download would produce "build-2"; the
    # whole point is that the file is the bytes the screen already read.
    r = client.get(f"/api/reports/saved/{made['id']}.pdf")

    assert r.status_code == 200, r.text
    assert r.headers["content-type"] == "application/pdf"
    assert "attachment" in r.headers["content-disposition"]
    assert client.calls["builds"] == 1, "the download must not build anything"
    assert client.calls["rendered"][-1] == made["payload"]


def test_the_pdf_path_is_not_swallowed_by_the_id_route(client):
    """`/saved/5.pdf` must reach the PDF route.

    FastAPI matches in declaration order, and the bare `/saved/{report_id}`
    would take this path first, where int("5.pdf") is a 422 that does NOT fall
    through. A 404 here is the RIGHT answer for a missing id; a 422 means the
    routes are declared in the wrong order again."""
    r = client.get("/api/reports/saved/5.pdf")
    assert r.status_code == 404, r.text


def test_viewing_a_stored_report_returns_the_document(client):
    made = client.post(f"/api/reports/generate?{q(MONTH, scope='all')}").json()
    r = client.get(f"/api/reports/saved/{made['id']}")
    assert r.status_code == 200, r.text
    assert r.json()["payload"] == made["payload"]
    assert client.calls["builds"] == 1


def test_a_missing_report_is_a_404(client):
    assert client.get("/api/reports/saved/999").status_code == 404


# ---------------------------------------------------------------------------
# A storage outage costs the cache, never the report
# ---------------------------------------------------------------------------

def test_a_report_that_cannot_be_stored_is_still_returned(client, monkeypatch):
    async def _no_save(*args, **kwargs):
        return None

    monkeypatch.setattr(client.store, "save", _no_save)   # type: ignore[attr-defined]
    body = client.post(f"/api/reports/generate?{q(MONTH, scope='all')}").json()

    assert body["id"] is None
    assert body["payload"]["sections"], (
        "losing the cache must not lose the document the caller just paid for"
    )


if __name__ == "__main__":   # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))


# ---------------------------------------------------------------------------
# THREE WINDOWS, AND ONLY THE FINISHED ONES
#
# A report used to be able to cover any run of days at all: 11 days, Tuesday to
# Tuesday, half of one week and half of the next. That is an unbounded set of
# documents, no two of which can be laid beside each other, and each one costs
# two model passes. A report now covers a whole week (Monday to Sunday), a half
# month, or a calendar month, and only once that period has finished.
#
# The rule is enforced HERE and not only in the picker, because a bookmark, a
# hand-edited URL and a script all arrive at the same route.
# ---------------------------------------------------------------------------

def test_a_finished_period_builds(client):
    r = client.post(f"/api/reports/generate?{q(WEEK, scope='all')}")
    assert r.status_code == 200, r.text

    end = client.calls["ends"][-1]
    assert end == WEEK.end, "the period's last day must reach the builder"
    assert client.calls["whole"] == [True], (
        "a week means those seven whole days, not a window that rolls from the "
        "time of day someone happened to click"
    )
    assert r.json()["payload"]["meta"]["window"]["to"] == WEEK.end.isoformat()


def test_a_window_that_is_not_a_period_is_refused(client):
    """Ending a "week" on a Monday is not a week. Nothing is built, and the
    message says what the three shapes are rather than just saying no."""
    monday = WEEK.end + timedelta(days=1)
    r = client.post(f"/api/reports/generate?days=7&end={monday.isoformat()}")

    assert r.status_code == 422, r.text
    assert "Monday to Sunday" in r.json()["detail"]
    assert client.calls["builds"] == 0


def test_a_day_count_that_does_not_match_the_period_is_refused(client):
    """"30 days ending on the 31st" is not the month that ended on the 31st.
    The width is checked against the period, not just the end day."""
    r = client.post(f"/api/reports/generate?days=30&end={MONTH.end.isoformat()}")
    assert r.status_code == 422 or MONTH.days == 30
    if MONTH.days != 30:
        assert client.calls["builds"] == 0


def test_a_period_that_has_not_finished_is_refused(client):
    """The strongest form of the rule: the CURRENT week is a real week, its
    dates are real dates, and it is still collecting. A report about it would
    read as if it were about all of it."""
    running = rp.containing("week", date.today())
    r = client.post(f"/api/reports/generate?{q(running)}")

    assert r.status_code == 422, r.text
    assert "hasn't finished" in r.json()["detail"]
    assert client.calls["builds"] == 0


def test_the_trailing_window_is_gone(client):
    """No `end` means the window ends today, and a period ending today has not
    finished. The old default is now a refusal, deliberately."""
    r = client.post("/api/reports/generate?days=7&scope=all")
    assert r.status_code == 422, r.text
    assert client.calls["builds"] == 0


def test_two_different_weeks_are_two_different_reports(client):
    client.post(f"/api/reports/generate?{q(WEEK)}")
    client.post(f"/api/reports/generate?{q(PREV_WEEK)}")

    assert client.calls["builds"] == 2, (
        "last week and the week before are different documents; filing them "
        "under one key was the bug that made the window a lie"
    )
    items = client.get("/api/reports/saved").json()["items"]
    assert sorted(i["window_to"] for i in items) == sorted(
        [PREV_WEEK.end.isoformat(), WEEK.end.isoformat()])


def test_the_same_period_is_never_built_twice(client):
    """The point of the rule. A finished period cannot gain another day, so a
    second build could only reword the same numbers."""
    first = client.post(f"/api/reports/generate?{q(HALF)}").json()
    second = client.post(f"/api/reports/generate?{q(HALF)}").json()

    assert second["cached"] is True
    assert client.calls["builds"] == 1
    assert second["payload"] == first["payload"]


def test_a_future_window_is_refused(client):
    future = rp.containing("week", date.today() + timedelta(days=21))
    r = client.post(f"/api/reports/generate?{q(future)}")
    assert r.status_code == 422, r.text
    assert client.calls["builds"] == 0


def test_an_unparseable_end_is_refused_rather_than_guessed(client):
    """A clamp would be wrong here. `days=120` has an obvious intended answer;
    `end=last-tuesday` does not, and quietly serving today's report for it
    would look exactly like the picker had worked."""
    r = client.post("/api/reports/generate?days=7&end=last-tuesday")
    assert r.status_code == 422, r.text
    assert "YYYY-MM-DD" in r.json()["detail"]
    assert client.calls["builds"] == 0


def test_a_service_that_cannot_take_an_end_refuses_rather_than_lies(client, monkeypatch):
    """The trailing window under a past window's heading would be undetectable
    downstream, so a stale service build must 503 instead of answering."""
    async def _old_build(*, mode="simple", days=30, scope="all", tone=True):
        client.calls["builds"] += 1        # type: ignore[attr-defined]
        return _report(mode)

    monkeypatch.setattr(rt.social_report, "build_report", _old_build)
    r = client.post(f"/api/reports/generate?{q(WEEK)}")
    assert r.status_code == 503, r.text
    assert client.calls["builds"] == 0


# ---------------------------------------------------------------------------
# The menu
# ---------------------------------------------------------------------------

def _coverage(monkeypatch, *, first="2026-01-01", days=None):
    async def _reader(scope):
        return {"scope": scope, "from": first, "to": TODAY, "today": TODAY,
                "days": days if days is not None else [], "streams": {},
                "max_days": 90}
    monkeypatch.setattr(rt.social_report, "data_coverage", _reader, raising=False)


def test_the_menu_offers_finished_periods_only(client, monkeypatch):
    _coverage(monkeypatch)
    body = client.get("/api/reports/periods?grain=week&limit=5").json()

    assert body["grain"] == "week"
    assert len(body["items"]) == 5
    for item in body["items"]:
        assert item["days"] == 7
        assert item["to"] < TODAY, "a period on the menu has finished"
        assert item["label"].startswith("Mon ")
    # Newest first, and no gaps: consecutive weeks.
    tos = [i["to"] for i in body["items"]]
    assert tos == sorted(tos, reverse=True)


def test_the_menu_says_what_is_in_each_period_and_what_is_already_built(
        client, monkeypatch):
    day = WEEK.end.isoformat()
    _coverage(monkeypatch, days=[{"date": day, "count": 12}])
    made = client.post(f"/api/reports/generate?{q(WEEK)}").json()

    items = client.get("/api/reports/periods?grain=week&limit=4").json()["items"]
    mine = [i for i in items if i["to"] == day]
    assert len(mine) == 1
    assert mine[0]["records"] == 12, "the count is summed over the period's days"
    assert mine[0]["report_id"] == made["id"], (
        "the picker sends the reader to the document that exists instead of "
        "offering to build a second copy of it"
    )
    assert [i for i in items if i["to"] != day][0]["report_id"] is None


def test_the_menu_stops_where_the_data_starts(client, monkeypatch):
    """A period that ended before we collected anything is one we were not
    looking through, and building it would spend two model passes to say so."""
    _coverage(monkeypatch, first=(date.today() - timedelta(days=20)).isoformat())
    items = client.get("/api/reports/periods?grain=week&limit=12").json()["items"]
    assert 0 < len(items) < 12
    assert client.calls["builds"] == 0, "reading the menu must build nothing"


def test_the_menu_never_builds(client, monkeypatch):
    _coverage(monkeypatch)
    for grain in ("week", "half", "month"):
        assert client.get(f"/api/reports/periods?grain={grain}").status_code == 200
    assert client.calls["builds"] == 0


# ---------------------------------------------------------------------------
# Every stream can be reported on its own
# ---------------------------------------------------------------------------

def test_industry_signal_is_a_scope_the_route_accepts(client):
    """The tab offered Everything and Competitors only, so the one way to read
    the news stream on its own was to edit the URL. The route always took it."""
    from app.services import social_report as real

    assert "news" in real.REPORT_SCOPES
    r = client.post(f"/api/reports/generate?{q(WEEK, scope='news')}")
    assert r.status_code == 200, r.text
    assert r.json()["scope"] == "news"


def test_each_stream_is_its_own_document_for_the_same_period(client):
    """One period, one report per stream: they are different documents about
    different data, and the store keys on scope for exactly this reason."""
    ids = set()
    for scope in ("all", "competitors", "news"):
        ids.add(client.post(f"/api/reports/generate?{q(WEEK, scope=scope)}").json()["id"])

    assert len(ids) == 3
    assert client.calls["builds"] == 3
    assert len(client.get("/api/reports/saved").json()["items"]) == 3


# ---------------------------------------------------------------------------
# Throwing one away
# ---------------------------------------------------------------------------

def test_a_stored_report_can_be_deleted(client):
    """The table outlived two changes to what a window may be, so it holds
    documents whose dates match nothing on the menu. A row like that reads as
    something random that got downloaded by accident."""
    made = client.post(f"/api/reports/generate?{q(MONTH)}").json()
    assert client.get("/api/reports/saved").json()["items"]

    r = client.delete(f"/api/reports/saved/{made['id']}")
    assert r.status_code == 200, r.text
    assert client.get("/api/reports/saved").json()["items"] == []
    assert client.get(f"/api/reports/saved/{made['id']}").status_code == 404


def test_deleting_a_report_that_is_not_there_is_a_404(client):
    assert client.delete("/api/reports/saved/999").status_code == 404


if __name__ == "__main__":   # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
