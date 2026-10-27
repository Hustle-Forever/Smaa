"""The social-listening report API, its two detail endpoints, and the guard
that stops the report inventing numbers.

Driven through the real ASGI app with the report services monkeypatched, so
these exercise routing, validation, headers and degradation with no database,
no model and no PDF engine. Four classes of bug live here and none of them is
visible by reading the files:

* **A window with nothing in it must be a 200.** "Nothing happened in these 30
  days" is a valid report. If an empty window 404s, the owner reads it as a
  broken feature and the honest answer never reaches them.
* **The PDF is a download.** Content-Type, an `attachment` disposition and a
  filename that cannot break the header — the filename is produced by another
  module and lands inside quotes.
* **The detail view is not the card.** The list endpoint attaches 6 sightings
  per trend; the detail endpoint exists precisely to show the rest. A
  regression to 6 would look fine on screen and be silently wrong.
* **The narrative may never fabricate.** The prompt has to carry the
  "every figure you cite must appear in the data above" instruction, and a
  model outage has to come back as two unavailable sections rather than as a
  failed download. A confident summary containing a number nobody counted is
  the single worst failure this feature can have, because it is printed in the
  same font as the measured KPIs above it.
* **A scope must reach the service, and a missing item must 404.** `?scope=news`
  answered with all three streams under a one-stream heading is a fabrication
  that looks like a filter; and an item that no longer exists must not come
  back as an empty document that reads as a measured "nothing here". Those live
  under "scope + /item" at the end of this file.
"""
from __future__ import annotations

import asyncio
import sys
import types
from pathlib import Path

import pytest

_BACKEND = Path(__file__).resolve().parent.parent
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from fastapi.testclient import TestClient  # noqa: E402

from app.routers import general_trends as rt_gt  # noqa: E402
from app.routers import reports as rt  # noqa: E402
from app.routers import trend_finder as rt_tf  # noqa: E402
from app.services import social_report_narrative as narrative  # noqa: E402
from app.services import trend_ideation  # noqa: E402


def _run(coro):
    """There is no pytest-asyncio in this venv — coroutines are driven with
    ``asyncio.run``, matching tests/test_general_trends_pipeline.py."""
    return asyncio.run(coro)


def _report(mode: str = "simple", sections=None) -> dict:
    return {
        "meta": {"title": "Social-listening Signals", "brand": "42 Abu Dhabi",
                 "mode": mode, "generated_at": "2026-08-20T09:00:00+00:00",
                 "window": {"days": 30, "from": "2026-07-21", "to": "2026-08-20"},
                 "previous_window": {"from": "2026-06-21", "to": "2026-07-21"}},
        "basis": {"competitor_handles": 2, "competitor_posts": 20,
                  "news_items": 385, "viral_trends": 95,
                  "notes": ["shares: not available (Instagram API)"]},
        "sections": sections if sections is not None else [
            {"id": "signals", "title": "Social-listening Signals", "available": True,
             "unavailable_reason": None, "basis": "20 competitor posts",
             "estimated": False, "kind": "signals",
             "body": {"groups": [{"group": "Competitors / Sentiment", "items": []}]}},
        ],
    }


@pytest.fixture
def client(monkeypatch):
    """Real app + real reports router, fake data service and fake PDF renderer.

    The app-wide shared-password gate is middleware that 401s every /api/*
    request before routing, so it is switched off — this suite is about the
    report endpoints, not authentication.
    """
    from app import access
    monkeypatch.setattr(access, "enabled", lambda: False)

    calls: dict = {}

    async def _build_report(*, mode="simple", days=30):
        calls["build_report"] = {"mode": mode, "days": days}
        return _report(mode)

    monkeypatch.setattr(
        rt, "social_report",
        types.SimpleNamespace(REPORT_MODES=("simple", "full"),
                              build_report=_build_report),
    )
    monkeypatch.setattr(
        rt, "social_report_pdf",
        types.SimpleNamespace(
            render_pdf=lambda report: b"%PDF-1.4 fake",
            suggested_filename=lambda report: "42-social-listening-simple-2026-08-20.pdf",
        ),
    )
    # The narrative is a model call; the router tests are about plumbing.
    monkeypatch.setattr(rt.narrative, "write_narrative", _async_identity)

    from app.main import app
    c = TestClient(app)
    c.calls = calls          # type: ignore[attr-defined]
    return c


async def _async_identity(report):
    return report


# ---------------------------------------------------------------------------
# GET /api/reports/social-listening — mode + days
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("mode", ["simple", "full"])
def test_both_modes_are_accepted(client, mode):
    r = client.get(f"/api/reports/social-listening?mode={mode}")
    assert r.status_code == 200, r.text
    assert r.json()["meta"]["mode"] == mode
    assert client.calls["build_report"]["mode"] == mode   # type: ignore[attr-defined]


def test_mode_defaults_to_simple(client):
    assert client.get("/api/reports/social-listening").status_code == 200
    assert client.calls["build_report"]["mode"] == "simple"  # type: ignore[attr-defined]


@pytest.mark.parametrize("mode", ["deep", "SIMPLE_", "meltwater", "  "])
def test_an_unknown_mode_is_a_422_that_names_the_real_ones(client, mode):
    r = client.get(f"/api/reports/social-listening?mode={mode}")
    assert r.status_code == 422, r.text
    assert "simple" in r.json()["detail"] and "full" in r.json()["detail"]


def test_an_empty_mode_parameter_means_unspecified(client):
    """`?mode=` is a link built by a UI that had nothing selected yet, not a
    typo — it gets the default rather than an error page."""
    assert client.get("/api/reports/social-listening?mode=").status_code == 200
    assert client.calls["build_report"]["mode"] == "simple"  # type: ignore[attr-defined]


def test_mode_is_case_insensitive(client):
    assert client.get("/api/reports/social-listening?mode=FULL").status_code == 200
    assert client.calls["build_report"]["mode"] == "full"  # type: ignore[attr-defined]


@pytest.mark.parametrize("given,expected", [
    (30, 30), (1, 1), (90, 90),
    (0, 1), (-7, 1),          # clamped, not rejected
    (365, 90), (91, 90),
])
def test_days_is_clamped_into_the_supported_window(client, given, expected):
    """The window rides in a hand-editable download URL and in bookmarks that
    outlive the UI that made them. A 120-day request is a 90-day report, not an
    error page where a PDF should have been."""
    r = client.get(f"/api/reports/social-listening?days={given}")
    assert r.status_code == 200, r.text
    assert client.calls["build_report"]["days"] == expected  # type: ignore[attr-defined]


def test_days_defaults_to_thirty(client):
    assert client.get("/api/reports/social-listening").status_code == 200
    assert client.calls["build_report"]["days"] == 30  # type: ignore[attr-defined]


def test_an_empty_window_is_a_200_not_a_404(client, monkeypatch):
    """A report that says "nothing happened" is a valid answer and must reach
    the owner as one."""
    empty = _report("full", sections=[])
    empty["basis"] = {"competitor_handles": 0, "competitor_posts": 0,
                      "news_items": 0, "viral_trends": 0, "notes": []}

    async def _build_report(*, mode="simple", days=30):
        return empty

    monkeypatch.setattr(rt.social_report, "build_report", _build_report)
    r = client.get("/api/reports/social-listening?mode=full&days=1")
    assert r.status_code == 200, r.text
    assert r.json()["sections"] == []


def test_the_narrative_pass_runs_over_the_assembled_report(client, monkeypatch):
    seen: dict = {}

    async def _write(report):
        seen["report"] = report
        out = dict(report)
        out["sections"] = [{"id": "executive_summary", "title": "Executive Summary",
                            "available": True, "unavailable_reason": None,
                            "basis": narrative.NARRATIVE_BASIS, "estimated": True,
                            "kind": "bullets", "body": {"items": ["a finding"]}}]
        return out

    monkeypatch.setattr(rt.narrative, "write_narrative", _write)
    r = client.get("/api/reports/social-listening?mode=full")
    assert r.status_code == 200, r.text
    assert seen["report"]["meta"]["mode"] == "full"
    assert r.json()["sections"][0]["body"]["items"] == ["a finding"]


def test_a_missing_report_service_is_a_503_not_a_crash(client, monkeypatch):
    """The data service ships alongside this router; a build without it must
    cost these two routes, never the process."""
    monkeypatch.setattr(rt, "social_report", None)
    r = client.get("/api/reports/social-listening")
    assert r.status_code == 503, r.text
    assert "report service" in r.json()["detail"]


def test_a_bad_mode_beats_a_missing_service_to_the_answer(client, monkeypatch):
    """Two different problems, two different answers: the caller must not be
    told the service is missing when what they actually sent was a typo."""
    monkeypatch.setattr(rt, "social_report", None)
    assert client.get("/api/reports/social-listening?mode=nope").status_code == 422


def test_a_failing_build_is_a_500_with_a_readable_reason(client, monkeypatch):
    async def _boom(*, mode="simple", days=30):
        raise RuntimeError("the pooler went away")

    monkeypatch.setattr(rt.social_report, "build_report", _boom)
    r = client.get("/api/reports/social-listening")
    assert r.status_code == 500
    assert "the pooler went away" in r.json()["detail"]


# ---------------------------------------------------------------------------
# GET /api/reports/social-listening.pdf
# ---------------------------------------------------------------------------

def test_the_pdf_route_streams_a_pdf_as_an_attachment(client):
    r = client.get("/api/reports/social-listening.pdf?mode=full&days=30")
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("application/pdf")
    assert r.content == b"%PDF-1.4 fake"
    disposition = r.headers["content-disposition"]
    assert disposition.startswith("attachment; ")
    assert 'filename="42-social-listening-simple-2026-08-20.pdf"' in disposition


def test_the_pdf_route_validates_mode_and_clamps_days_too(client):
    assert client.get("/api/reports/social-listening.pdf?mode=nope").status_code == 422
    assert client.get("/api/reports/social-listening.pdf?days=999").status_code == 200
    assert client.calls["build_report"]["days"] == 90   # type: ignore[attr-defined]


def test_the_pdf_filename_cannot_break_the_header(client, monkeypatch):
    """The name comes from another module and lands inside a quoted header."""
    monkeypatch.setattr(rt.social_report_pdf, "suggested_filename",
                        lambda report: 'evil".pdf\r\nX-Injected: 1')
    r = client.get("/api/reports/social-listening.pdf")
    disposition = r.headers["content-disposition"]
    assert '"' not in disposition.split("filename=", 1)[1].strip('"')
    assert "\n" not in disposition and "\r" not in disposition
    assert "x-injected" not in {k.lower() for k in r.headers}


def test_a_filename_without_an_extension_still_downloads_as_a_pdf(client, monkeypatch):
    monkeypatch.setattr(rt.social_report_pdf, "suggested_filename",
                        lambda report: "42-report")
    r = client.get("/api/reports/social-listening.pdf")
    assert 'filename="42-report.pdf"' in r.headers["content-disposition"]


def test_a_broken_filename_maker_never_costs_the_rendered_pdf(client, monkeypatch):
    def _boom(report):
        raise ValueError("nope")

    monkeypatch.setattr(rt.social_report_pdf, "suggested_filename", _boom)
    r = client.get("/api/reports/social-listening.pdf?mode=full")
    assert r.status_code == 200
    assert r.content == b"%PDF-1.4 fake"
    assert "42-social-listening-full-" in r.headers["content-disposition"]


def test_a_failing_renderer_is_a_500_not_a_truncated_download(client, monkeypatch):
    def _boom(report):
        raise RuntimeError("reportlab exploded")

    monkeypatch.setattr(rt.social_report_pdf, "render_pdf", _boom)
    r = client.get("/api/reports/social-listening.pdf")
    assert r.status_code == 500
    assert "reportlab exploded" in r.json()["detail"]


def test_a_missing_pdf_renderer_is_a_503(client, monkeypatch):
    monkeypatch.setattr(rt, "social_report_pdf", None)
    assert client.get("/api/reports/social-listening.pdf").status_code == 503


# ---------------------------------------------------------------------------
# Detail endpoints
# ---------------------------------------------------------------------------

TREND = {"id": 7, "title": "Kids taking pictures", "slug": "kids-taking-pictures",
         "velocity": 0.8, "evidence_count": 4, "metadata": {}}
SIGHTINGS = [
    {"id": i, "trend_id": 7, "run_id": 3, "source": "kym", "source_key": f"k{i}",
     "region": "global", "category": None, "title": f"sighting {i}",
     "url": f"https://example.test/{i}", "image_url": None, "summary": None,
     "metric_value": 10.0, "metric_label": "score", "published_at": None,
     "seen_at": None, "extra": {"raw": "internal"}}
    for i in range(12)
]
POST = {"id": 55, "username": "1337", "permalink": "https://instagram.test/p/1",
        "like_count": 108398, "comments_count": 78789, "view_count": 0,
        "engagement": 187187, "source": "competitor", "caption": "hello"}


@pytest.fixture
def detail_client(monkeypatch):
    from app import access
    monkeypatch.setattr(access, "enabled", lambda: False)

    calls: dict = {}

    async def _get_trend(tid):
        return dict(TREND) if int(tid) == 7 else None

    async def _sightings_for(ids, *, per_trend=6):
        calls["per_trend"] = per_trend
        return {int(ids[0]): [dict(s) for s in SIGHTINGS]}

    async def _get_post(pid):
        return dict(POST) if int(pid) == 55 else None

    monkeypatch.setattr(rt_gt.store, "get_trend", _get_trend)
    monkeypatch.setattr(rt_gt.store, "sightings_for", _sightings_for)
    monkeypatch.setattr(rt_gt.ideation, "cached_research",
                        lambda tid, trend=None: {"what_it_is": "a photo trend"})
    monkeypatch.setattr(rt_tf.store, "get_post", _get_post)

    from app.main import app
    c = TestClient(app)
    c.calls = calls          # type: ignore[attr-defined]
    return c


def test_general_trend_detail_returns_the_row_evidence_and_research(detail_client):
    r = detail_client.get("/api/trends/general/items/7")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["trend"]["id"] == 7
    assert body["trend"]["heat"] == "hot"          # derived, same as the card
    assert body["research"] == {"what_it_is": "a photo trend"}
    assert len(body["evidence"]) == 12
    assert body["evidence"][0]["url"] == "https://example.test/0"


def test_general_trend_detail_asks_for_every_sighting_not_the_six_row_preview(
        detail_client):
    """The whole reason this endpoint exists. A regression to 6 would look
    perfectly fine on screen."""
    detail_client.get("/api/trends/general/items/7")
    assert detail_client.calls["per_trend"] == rt_gt.DETAIL_EVIDENCE_MAX  # type: ignore[attr-defined]
    assert rt_gt.DETAIL_EVIDENCE_MAX > 6


def test_general_trend_detail_hides_the_internal_sighting_columns(detail_client):
    row = detail_client.get("/api/trends/general/items/7").json()["evidence"][0]
    assert "extra" not in row and "run_id" not in row and "trend_id" not in row


def test_general_trend_detail_404s_on_a_missing_trend(detail_client):
    r = detail_client.get("/api/trends/general/items/999")
    assert r.status_code == 404
    assert r.json()["detail"] == "trend not found"


def test_general_trend_detail_survives_an_unreadable_research_cache(
        detail_client, monkeypatch):
    """Opening a detail view must never fail on a nice-to-have."""
    def _boom(tid, trend=None):
        raise RuntimeError("cache is confused")

    monkeypatch.setattr(rt_gt.ideation, "cached_research", _boom)
    body = detail_client.get("/api/trends/general/items/7").json()
    assert body["research"] is None
    assert body["trend"]["id"] == 7


def test_general_trend_detail_does_not_start_a_research_lookup(detail_client,
                                                               monkeypatch):
    """A page view may not spend a model call."""
    async def _never(**kwargs):
        raise AssertionError("a detail view must not research the trend")

    monkeypatch.setattr(rt_gt.ideation, "research_trend", _never)
    assert detail_client.get("/api/trends/general/items/7").status_code == 200


def test_post_detail_returns_the_stored_row(detail_client):
    r = detail_client.get("/api/trends/posts/55")
    assert r.status_code == 200, r.text
    post = r.json()["post"]
    assert post["id"] == 55 and post["like_count"] == 108398
    # There is no shares column and there never will be — the Graph API does
    # not expose it. Nothing may invent one on the way out.
    assert "shares" not in post


def test_post_detail_404s_on_a_missing_post(detail_client):
    r = detail_client.get("/api/trends/posts/999")
    assert r.status_code == 404
    assert r.json()["detail"] == "post not found"


def test_the_rescore_route_is_not_swallowed_by_the_new_detail_routes(detail_client,
                                                                     monkeypatch):
    """`/posts/{id}` and `/items/{id}` are declared next to literal siblings
    (`/posts/rescore`, `/items/rescore`). Those are POSTs, so they must still
    reach their own handler."""
    async def _rescore(ids=None):
        return 3

    monkeypatch.setattr(rt_gt.pipeline, "rescore", _rescore)
    r = detail_client.post("/api/trends/general/items/rescore", json={})
    assert r.status_code == 200, r.text
    assert r.json() == {"scored": 3}


# ---------------------------------------------------------------------------
# The narrative pass
# ---------------------------------------------------------------------------

def _full_report(**basis):
    counts = {"competitor_handles": 2, "competitor_posts": 20,
              "news_items": 385, "viral_trends": 95, "notes": []}
    counts.update(basis)
    return {
        "meta": {"title": "Social-listening Report", "brand": "42 Abu Dhabi",
                 "mode": "full", "generated_at": "2026-08-20T09:00:00+00:00",
                 "window": {"days": 30, "from": "2026-07-21", "to": "2026-08-20"},
                 "previous_window": {"from": "2026-06-21", "to": "2026-07-21"}},
        "basis": counts,
        "sections": [
            {"id": "executive_summary", "title": "Executive Summary",
             "available": False, "unavailable_reason": "not written yet",
             "basis": "", "estimated": True, "kind": "prose",
             "body": {"paragraphs": []}},
            {"id": "mention_volume", "title": "Mention Volume", "available": True,
             "unavailable_reason": None, "basis": "counted from trend_posts",
             "estimated": False, "kind": "kpis",
             "body": {"items": [{"label": "Competitor posts", "value": "20",
                                 "delta": "+4", "delta_dir": "up", "note": None}]}},
            {"id": "share_of_voice", "title": "Share of Voice", "available": False,
             "unavailable_reason": "only 2 accounts are watched", "basis": "",
             "estimated": False, "kind": "chart", "body": {}},
            {"id": "top_content", "title": "Top Content", "available": True,
             "unavailable_reason": None, "basis": "20 posts with engagement",
             "estimated": False, "kind": "table",
             "body": {"columns": ["Post", "Likes"],
                      "rows": [["a post", "108398"]], "align": ["left", "right"]}},
            {"id": "recommendations", "title": "Recommendations",
             "available": False, "unavailable_reason": "not written yet",
             "basis": "", "estimated": True, "kind": "bullets",
             "body": {"items": []}},
        ],
    }


ANSWER = {
    "executive_summary": [
        {"finding": "Only two competitor accounts are watched, so nothing here "
                    "measures a market.", "section": "Mention Volume"},
        {"finding": "One post carries most of the measured engagement.",
         "section": "Top Content"},
        {"finding": "Share of Voice cannot be computed at this sample size.",
         "section": "Share of Voice"},
    ],
    "recommendations": [
        {"action": "Add eight more accounts to the watch list",
         "why": "two accounts cannot support a share-of-voice claim"},
        {"action": "Film the 60-second campus walk-through this week",
         "why": "the top measured post was shot on campus"},
    ],
}


def _sections_by_id(report):
    return {s["id"]: s for s in report["sections"]}


def test_the_narrative_fills_both_slots(monkeypatch):
    async def _gemini(prompt, **kwargs):
        return dict(ANSWER)

    monkeypatch.setattr(narrative, "gemini_text", _gemini)
    out = _run(narrative.write_narrative(_full_report()))
    sections = _sections_by_id(out)

    summary = sections["executive_summary"]
    assert summary["available"] is True
    assert summary["unavailable_reason"] is None
    assert summary["kind"] == "bullets"
    assert len(summary["body"]["items"]) == 3
    # Every finding is traceable back to a section — that is what stops a
    # finding nobody can check.
    assert "(see: Mention Volume)" in summary["body"]["items"][0]
    # A model wrote it, so it is labelled as a judgement, not a measurement.
    assert summary["estimated"] is True

    actions = sections["recommendations"]
    assert actions["available"] is True
    assert actions["body"]["items"][0].startswith("Add eight more accounts")
    assert "two accounts cannot support" in actions["body"]["items"][0]


def test_the_narrative_does_not_mutate_the_caller_report(monkeypatch):
    async def _gemini(prompt, **kwargs):
        return dict(ANSWER)

    monkeypatch.setattr(narrative, "gemini_text", _gemini)
    original = _full_report()
    out = _run(narrative.write_narrative(original))
    assert original["sections"][0]["body"] == {"paragraphs": []}
    assert out["sections"][0]["body"]["items"]


def test_a_model_outage_degrades_to_unavailable_sections(monkeypatch):
    """The download must still happen. Two missing sections beat no report."""
    async def _boom(prompt, **kwargs):
        raise RuntimeError("503 backend unavailable")

    monkeypatch.setattr(narrative, "gemini_text", _boom)
    sections = _sections_by_id(_run(narrative.write_narrative(_full_report())))
    for sid in ("executive_summary", "recommendations"):
        assert sections[sid]["available"] is False
        reason = sections[sid]["unavailable_reason"]
        assert reason and "could not be reached" in reason
    # everything measured is untouched
    assert sections["mention_volume"]["body"]["items"][0]["value"] == "20"


def test_a_billing_outage_says_what_to_fix(monkeypatch):
    async def _boom(prompt, **kwargs):
        raise RuntimeError("PERMISSION_DENIED: billing is disabled")

    monkeypatch.setattr(narrative, "gemini_text", _boom)
    sections = _sections_by_id(_run(narrative.write_narrative(_full_report())))
    assert "billing" in sections["executive_summary"]["unavailable_reason"].lower()


def test_an_unreadable_answer_is_not_printed_as_a_finding(monkeypatch):
    """gemini_text returns the {"_raw": ...} sentinel rather than raising."""
    async def _raw(prompt, **kwargs):
        return {"_raw": "sorry, I can't do that"}

    monkeypatch.setattr(narrative, "gemini_text", _raw)
    sections = _sections_by_id(_run(narrative.write_narrative(_full_report())))
    assert sections["executive_summary"]["available"] is False
    assert sections["executive_summary"]["unavailable_reason"] == narrative.UNREADABLE
    assert sections["executive_summary"]["body"]["items"] == []


def test_half_an_answer_still_prints_its_half(monkeypatch):
    async def _half(prompt, **kwargs):
        return {"executive_summary": ANSWER["executive_summary"],
                "recommendations": []}

    monkeypatch.setattr(narrative, "gemini_text", _half)
    sections = _sections_by_id(_run(narrative.write_narrative(_full_report())))
    assert sections["executive_summary"]["available"] is True
    assert sections["recommendations"]["available"] is False


def test_an_empty_window_never_pays_for_a_model_call(monkeypatch):
    """With nothing measured there is nothing to summarise, and a model asked
    to summarise nothing is a model inventing something."""
    async def _never(prompt, **kwargs):
        raise AssertionError("no model call may be made for an empty report")

    monkeypatch.setattr(narrative, "gemini_text", _never)
    empty = _full_report(competitor_handles=0, competitor_posts=0,
                         news_items=0, viral_trends=0)
    sections = _sections_by_id(_run(narrative.write_narrative(empty)))
    assert sections["executive_summary"]["available"] is False
    assert sections["executive_summary"]["unavailable_reason"] == \
        narrative.NOTHING_MEASURED


def test_a_report_without_narrative_slots_is_returned_untouched(monkeypatch):
    """"simple" mode has no Executive Summary — that is not an error, and it
    must not cost a model call either."""
    async def _never(prompt, **kwargs):
        raise AssertionError("simple mode has nothing to narrate")

    monkeypatch.setattr(narrative, "gemini_text", _never)
    simple = _report("simple")
    assert _run(narrative.write_narrative(simple)) == simple


def test_an_already_written_narrative_is_not_written_twice(monkeypatch):
    """Both the data pass and the router may reach for this; the second call
    must not spend a second model call to overwrite the same words."""
    async def _never(prompt, **kwargs):
        raise AssertionError("the narrative was already written")

    monkeypatch.setattr(narrative, "gemini_text", _never)
    report = _full_report()
    for section in report["sections"]:
        if section["id"] in ("executive_summary", "recommendations"):
            section.update(available=True, unavailable_reason=None,
                           basis=narrative.NARRATIVE_BASIS, kind="bullets",
                           body={"items": ["already here"]})
    out = _run(narrative.write_narrative(report))
    assert _sections_by_id(out)["executive_summary"]["body"]["items"] == \
        ["already here"]


def test_the_slots_are_found_by_title_when_the_ids_are_renamed(monkeypatch):
    """This pass does not own the section ids. A rename upstream must not
    silently blank the two sections it is responsible for."""
    async def _gemini(prompt, **kwargs):
        return dict(ANSWER)

    monkeypatch.setattr(narrative, "gemini_text", _gemini)
    report = _full_report()
    report["sections"][0]["id"] = "sec_01"
    report["sections"][-1]["id"] = "sec_14"
    out = _run(narrative.write_narrative(report))
    assert out["sections"][0]["body"]["items"]
    assert out["sections"][-1]["body"]["items"]


# ---------------------------------------------------------------------------
# The prompt — the guard against a confident fabricated summary
# ---------------------------------------------------------------------------

def test_the_prompt_forbids_any_number_that_is_not_already_in_the_report():
    """THE test. Everything else in this feature is arithmetic; this is the one
    instruction standing between a report and a fabricated one."""
    prompt = narrative.build_narrative_prompt(_full_report()).lower()
    assert "never invent a number" in prompt
    assert "every figure you cite must appear in the data above" in prompt
    assert ("if you want to make a point you have no number for, make it "
            "qualitatively or leave it out") in prompt


def test_the_prompt_forbids_a_market_claim_at_this_sample_size():
    """Two watched accounts is not "the competitive landscape"."""
    prompt = narrative.build_narrative_prompt(_full_report()).lower()
    assert "competitive landscape" in prompt
    assert "share of voice" in prompt
    assert "fewer than five watched competitor accounts" in prompt


def test_the_prompt_labels_tone_and_bans_shares():
    prompt = narrative.build_narrative_prompt(_full_report()).lower()
    assert "ai-assessed from post text" in prompt
    assert "audience sentiment" in prompt          # ...as the thing never to write
    assert "instagram does not report shares" in prompt


def test_the_prompt_bans_a_tone_percentage_even_a_true_one():
    """Also found live: the model turned a table of counts into "AI-assessed
    sentiment was 100% neutral". It was arithmetically true and still wrong —
    a percentage on a tone split reads as a measurement of how people felt, and
    nothing in this product measures that."""
    prompt = narrative.build_narrative_prompt(_full_report()).lower()
    assert "as a count of items assessed" in prompt
    assert "never as a percentage or a share" in prompt
    assert "68.3% positive" in prompt


def test_the_prompt_carries_the_shared_plain_language_rule():
    """Imported, not restated — one object by design, so fixing the rule fixes
    every prompt at once."""
    assert narrative.PLAIN_LANGUAGE_RULE is trend_ideation.PLAIN_LANGUAGE_RULE
    assert trend_ideation.PLAIN_LANGUAGE_RULE in \
        narrative.build_narrative_prompt(_full_report())


def test_the_digest_carries_the_assembled_numbers_and_the_sample_size():
    digest = narrative.summarise_report(_full_report())
    assert "competitor accounts watched: 2" in digest
    assert "competitor posts collected:  20" in digest
    assert "industry news items:         385" in digest
    assert "viral social trends tracked: 95" in digest
    assert "Competitor posts = 20" in digest
    assert "108398" in digest


def test_the_digest_marks_what_is_missing_and_what_is_only_a_judgement():
    report = _full_report()
    report["sections"][1]["estimated"] = True
    digest = narrative.summarise_report(report)
    assert "NOT AVAILABLE: only 2 accounts are watched" in digest
    assert "(AI-ESTIMATED)" in digest
    assert "(MEASURED)" in digest


def test_the_digest_leaves_out_the_two_sections_being_written():
    """Feeding the model its own empty slots invites it to fill them from the
    placeholder text instead of from the numbers."""
    digest = narrative.summarise_report(_full_report())
    assert "Executive Summary" not in digest
    assert "not written yet" not in digest


def test_the_digest_says_a_table_was_cut_without_giving_a_row_count():
    """A truncated table must announce that it is truncated, so the model knows
    it is not seeing everything — but it must NOT be handed the row total."""
    report = _full_report()
    report["sections"][3]["body"]["rows"] = [["p", str(i)] for i in range(40)]
    digest = narrative.summarise_report(report)
    assert "some rows are not shown" in digest
    assert "basis line above is what the section actually measured" in digest


def test_the_digest_never_states_how_many_rows_a_table_has():
    """CHANGED DELIBERATELY. The previous rule printed the row total next to a
    warning not to treat it as a count of anything in the world, and the model
    ignored the warning twice on live data: once turning "16 rows in total"
    into "16 of 95 viral trends were flagged" (basis said 20), and again, after
    that warning was strengthened, turning a 22-row Reputation / Risk table
    — 15 flagged trends plus 7 volume-spike DAYS — into "the system has
    flagged 22 trends".

    A number the model must not use does not belong in front of it. The digest
    now states truncation with no total at all, and the section's own basis
    line carries the real counts."""
    report = _full_report()
    report["sections"][3]["body"]["rows"] = [["p", str(i)] for i in range(40)]
    digest = narrative.summarise_report(report)
    assert "40 rows" not in digest
    assert "rows in total" not in digest


def test_the_digest_sample_header_is_not_offered_as_a_citable_section():
    """The model cited "(see: MEASURED BASIS)" on a live run — a heading of
    the digest, not a section of the report, so the pointer led nowhere."""
    digest = narrative.summarise_report(_full_report())
    assert "MEASURED BASIS" not in digest
    assert "never cite it in `section`" in digest


def test_a_citation_to_something_that_is_not_a_section_is_dropped():
    """The finding survives; only the unfollowable pointer is removed."""
    report = _full_report()
    citable = narrative._citable(report)
    raw = {"executive_summary": [
        {"finding": "Volume rose", "section": "MEASURED BASIS"},
    ], "recommendations": []}
    findings, _ = narrative.parse_narrative(raw, citable)
    assert findings == ["Volume rose"]


def test_a_citation_that_names_a_real_section_is_kept_and_normalised():
    report = _full_report()
    citable = narrative._citable(report)
    title = report["sections"][3]["title"]
    sid = report["sections"][3]["id"]
    raw = {"executive_summary": [
        {"finding": "A", "section": title},
        {"finding": "B", "section": sid},
    ], "recommendations": []}
    findings, _ = narrative.parse_narrative(raw, citable)
    assert findings == [f"A (see: {title})", f"B (see: {title})"]


def test_the_prompt_forbids_carrying_a_number_between_sections():
    prompt = narrative.build_narrative_prompt(_full_report()).lower()
    assert "a number belongs to the line it is printed on" in prompt
    assert "do not carry a figure out of one section into a sentence about another"         in prompt



# ---------------------------------------------------------------------------
# scope + /item — the same contract at three widths
# ---------------------------------------------------------------------------
#
# Two classes of bug live here and neither is visible by reading the files:
#
# * **A scope must reach the service.** `?scope=news` that quietly builds the
#   all-streams report and titles it "Industry Signal" is the single worst
#   outcome this feature can have — it is a fabrication that looks like a
#   filter. So the pass-through is asserted, and the one place the router is
#   allowed to fall back (a stale service, DEFAULT scope only) is asserted not
#   to apply to a narrow one.
# * **A missing item is a 404 — and the only 404 here.** An empty window is
#   still a 200 with empty sections; an item that no longer exists is not.

def _item_report_dict(kind: str = "trend", item_id: int = 880,
                      mode: str = "simple") -> dict:
    report = _report(mode)
    report["meta"]["scope"] = "viral"
    report["meta"]["scope_label"] = "Viral Social Trends"
    report["meta"]["item"] = {
        "kind": kind, "id": item_id, "stream": "viral",
        "stream_label": "Viral Social Trends", "label": "#spidermanbrandnewday",
        "slug": "spidermanbrandnewday",
        "url": "https://www.tiktok.com/tag/spidermanbrandnewday",
    }
    return report


@pytest.fixture
def wide_client(monkeypatch):
    """The same app, with a scope-aware and item-aware fake service."""
    from app import access
    monkeypatch.setattr(access, "enabled", lambda: False)

    calls: dict = {}

    async def _build_report(*, mode="simple", days=30, scope="all"):
        calls["build_report"] = {"mode": mode, "days": days, "scope": scope}
        report = _report(mode)
        report["meta"]["scope"] = scope
        return report

    async def _build_item_report(*, kind, item_id, mode="simple", days=30):
        calls["build_item_report"] = {"kind": kind, "item_id": item_id,
                                      "mode": mode, "days": days}
        if item_id == 999999:
            raise LookupError(f"no viral trend with id {item_id}")
        return _item_report_dict(kind, item_id, mode)

    monkeypatch.setattr(
        rt, "social_report",
        types.SimpleNamespace(
            REPORT_MODES=("simple", "full"),
            REPORT_SCOPES=("all", "competitors", "news", "viral"),
            ITEM_KINDS=("post", "trend"),
            build_report=_build_report,
            build_item_report=_build_item_report),
    )
    monkeypatch.setattr(
        rt, "social_report_pdf",
        types.SimpleNamespace(
            render_pdf=lambda report: b"%PDF-1.4 fake",
            suggested_filename=lambda report: (
                "42-report-trend-880-spidermanbrandnewday-simple-2026-08-20.pdf"
                if report["meta"].get("item")
                else "42-social-listening-"
                     + (f"{report['meta'].get('scope')}-"
                        if report["meta"].get("scope") not in (None, "all") else "")
                     + f"{report['meta']['mode']}-2026-08-20.pdf")),
    )
    monkeypatch.setattr(rt.narrative, "write_narrative", _async_identity)

    from app.main import app
    c = TestClient(app)
    c.calls = calls          # type: ignore[attr-defined]
    return c


@pytest.mark.parametrize("scope", ["all", "competitors", "news", "viral"])
def test_every_scope_reaches_the_service(wide_client, scope):
    r = wide_client.get(f"/api/reports/social-listening?scope={scope}")
    assert r.status_code == 200, r.text
    assert wide_client.calls["build_report"]["scope"] == scope  # type: ignore[attr-defined]
    assert r.json()["meta"]["scope"] == scope


def test_scope_defaults_to_all(wide_client):
    assert wide_client.get("/api/reports/social-listening").status_code == 200
    assert wide_client.calls["build_report"]["scope"] == "all"  # type: ignore[attr-defined]


def test_scope_is_case_insensitive_and_an_empty_one_means_unspecified(wide_client):
    assert wide_client.get("/api/reports/social-listening?scope=VIRAL").status_code == 200
    assert wide_client.calls["build_report"]["scope"] == "viral"  # type: ignore[attr-defined]
    assert wide_client.get("/api/reports/social-listening?scope=").status_code == 200
    assert wide_client.calls["build_report"]["scope"] == "all"  # type: ignore[attr-defined]


@pytest.mark.parametrize("scope", ["competitor", "trends", "everything", "  "])
def test_an_unknown_scope_is_a_422_that_names_the_real_ones(wide_client, scope):
    r = wide_client.get(f"/api/reports/social-listening?scope={scope}")
    assert r.status_code == 422, r.text
    detail = r.json()["detail"]
    for valid in ("all", "competitors", "news", "viral"):
        assert valid in detail


def test_the_pdf_route_carries_the_scope_and_names_the_file_after_it(wide_client):
    r = wide_client.get("/api/reports/social-listening.pdf?mode=full&scope=news")
    assert r.status_code == 200, r.text
    assert wide_client.calls["build_report"]["scope"] == "news"  # type: ignore[attr-defined]
    assert 'filename="42-social-listening-news-full-2026-08-20.pdf"' in \
        r.headers["content-disposition"]
    assert r.headers["cache-control"] == "no-store"


def test_a_stale_service_still_serves_the_default_scope(client):
    """The report services are imported defensively because they ship with this
    router. The `client` fixture's fake predates scopes entirely — the
    all-streams report must still come back."""
    r = client.get("/api/reports/social-listening?scope=all")
    assert r.status_code == 200, r.text
    assert "scope" not in client.calls["build_report"]   # type: ignore[attr-defined]


def test_a_stale_service_refuses_to_fake_a_narrow_scope(client):
    """Answering `scope=news` with all three streams under a one-stream heading
    is precisely the misreading this report exists to prevent, so it fails
    loudly instead.

    503, not 500. It used to be a 500 only because a TypeError leaked into the
    generic handler — accidental, not designed. The router detects the stale
    signature deliberately now, and "this build cannot do that, redeploy" is
    exactly what 503 means and what this file already returns for a missing
    report service. A 500 would tell the caller to file a bug."""
    r = client.get("/api/reports/social-listening?scope=news")
    assert r.status_code == 503, r.text
    # and it must say what to do about it, not just refuse
    assert "redeploy" in r.text.lower()


# ---------------------------------------------------------------------------
# GET /api/reports/item
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("kind", ["post", "trend"])
@pytest.mark.parametrize("mode", ["simple", "full"])
def test_both_item_kinds_work_at_both_depths(wide_client, kind, mode):
    r = wide_client.get(f"/api/reports/item?kind={kind}&id=880&mode={mode}")
    assert r.status_code == 200, r.text
    call = wide_client.calls["build_item_report"]   # type: ignore[attr-defined]
    assert call == {"kind": kind, "item_id": 880, "mode": mode, "days": 30}
    assert r.json()["meta"]["item"]["kind"] == kind


def test_the_item_route_clamps_days_like_every_other_route(wide_client):
    assert wide_client.get("/api/reports/item?kind=trend&id=1&days=999").status_code == 200
    assert wide_client.calls["build_item_report"]["days"] == 90  # type: ignore[attr-defined]
    assert wide_client.get("/api/reports/item?kind=trend&id=1&days=0").status_code == 200
    assert wide_client.calls["build_item_report"]["days"] == 1  # type: ignore[attr-defined]


@pytest.mark.parametrize("kind", ["", "posts", "competitor", "news", "  "])
def test_an_unknown_item_kind_is_a_422_that_names_the_real_ones(wide_client, kind):
    """`news` is deliberately in that list: a news row IS a post, and guessing
    which table to read would answer a question nobody asked."""
    r = wide_client.get(f"/api/reports/item?kind={kind}&id=1")
    assert r.status_code == 422, r.text
    assert "post" in r.json()["detail"] and "trend" in r.json()["detail"]


def test_an_item_report_needs_an_id(wide_client):
    assert wide_client.get("/api/reports/item?kind=trend").status_code == 422
    assert wide_client.get("/api/reports/item?kind=trend&id=abc").status_code == 422


def test_a_missing_item_is_a_404_that_says_what_is_missing(wide_client):
    """The ONLY 404 in this router. An empty window is still a 200."""
    r = wide_client.get("/api/reports/item?kind=trend&id=999999")
    assert r.status_code == 404, r.text
    assert "999999" in r.json()["detail"]


def test_a_missing_item_pdf_is_a_404_too(wide_client):
    r = wide_client.get("/api/reports/item.pdf?kind=trend&id=999999")
    assert r.status_code == 404, r.text


def test_the_item_pdf_streams_an_attachment_named_after_the_item(wide_client):
    r = wide_client.get("/api/reports/item.pdf?kind=trend&id=880&mode=simple")
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("application/pdf")
    assert r.content == b"%PDF-1.4 fake"
    disposition = r.headers["content-disposition"]
    assert disposition.startswith("attachment; ")
    assert 'filename="42-report-trend-880-spidermanbrandnewday-simple-2026-08-20.pdf"' \
        in disposition
    assert r.headers["cache-control"] == "no-store"


def test_the_item_pdf_filename_cannot_break_the_header_either(wide_client,
                                                              monkeypatch):
    monkeypatch.setattr(rt.social_report_pdf, "suggested_filename",
                        lambda report: 'evil".pdf\r\nX-Injected: 1')
    r = wide_client.get("/api/reports/item.pdf?kind=trend&id=880")
    disposition = r.headers["content-disposition"]
    assert '"' not in disposition.split("filename=", 1)[1].strip('"')
    assert "\n" not in disposition and "\r" not in disposition
    assert "x-injected" not in {k.lower() for k in r.headers}


def test_the_narrative_pass_runs_over_an_item_report_too(wide_client, monkeypatch):
    seen: dict = {}

    async def _write(report):
        seen["report"] = report
        return report

    monkeypatch.setattr(rt.narrative, "write_narrative", _write)
    r = wide_client.get("/api/reports/item?kind=trend&id=880&mode=full")
    assert r.status_code == 200, r.text
    assert seen["report"]["meta"]["item"]["id"] == 880


def test_a_service_without_item_reports_is_a_503_not_a_500(wide_client,
                                                           monkeypatch):
    """A stale image costs the new feature, not the old one."""
    monkeypatch.delattr(rt.social_report, "build_item_report")
    r = wide_client.get("/api/reports/item?kind=trend&id=880")
    assert r.status_code == 503, r.text
    assert "per-item" in r.json()["detail"]
    # ...and the stream report still works on the same build
    assert wide_client.get("/api/reports/social-listening").status_code == 200


def test_a_bad_kind_beats_a_missing_service_to_the_answer(wide_client,
                                                          monkeypatch):
    monkeypatch.setattr(rt, "social_report", None)
    assert wide_client.get("/api/reports/item?kind=nope&id=1").status_code == 422
    # `post`, not `trend`: with the service gone the router validates against
    # its OWN FALLBACK_ITEM_KINDS, and that list follows the viral switch —
    # `trend` is not a kind while the stream is paused, so it would 422 here as
    # a bad value and prove nothing about the ordering this test is checking.
    assert wide_client.get("/api/reports/item?kind=post&id=1").status_code == 503


def test_a_failing_item_build_is_a_500_with_a_readable_reason(wide_client,
                                                              monkeypatch):
    async def _boom(*, kind, item_id, mode="simple", days=30):
        raise RuntimeError("the pooler went away")

    monkeypatch.setattr(rt.social_report, "build_item_report", _boom)
    r = wide_client.get("/api/reports/item?kind=trend&id=880")
    assert r.status_code == 500
    assert "the pooler went away" in r.json()["detail"]


# ---------------------------------------------------------------------------
# The narrative pass over a scoped or per-item report
# ---------------------------------------------------------------------------

def _item_narrative_report() -> dict:
    return {
        "meta": {"title": "#spidermanbrandnewday", "brand": "42 Abu Dhabi",
                 "mode": "full", "scope": "viral",
                 "scope_label": "Viral Social Trends",
                 "item": {"kind": "trend", "id": 880, "stream": "viral",
                          "stream_label": "Viral Social Trends",
                          "label": "#spidermanbrandnewday",
                          "slug": "spidermanbrandnewday", "url": None},
                 "generated_at": "2026-08-20T09:00:00+00:00",
                 "window": {"days": 30, "from": "2026-07-21", "to": "2026-08-20"},
                 "previous_window": {"from": "2026-06-21", "to": "2026-07-21"}},
        "basis": {"scope": "viral", "scope_label": "Viral Social Trends",
                  "competitor_handles": None, "competitor_posts": None,
                  "news_items": None, "viral_trends": 80,
                  "notes": ["This report is about ONE Viral Social Trends item."]},
        "sections": [
            {"id": "item_overview", "title": "The Item", "available": True,
             "unavailable_reason": None, "basis": "the source's own counters",
             "estimated": True, "kind": "signals",
             "body": {"groups": [{"group": "Viral Social Trends", "items": [
                 {"name": "#spidermanbrandnewday", "link": None,
                  "extra_links": [],
                  "metrics": [{"label": "Posts", "value": "288,030"}],
                  "sentiment": {"label": "Neutral", "estimated": True},
                  "suggestion": "Make a transition reel."}]}]}},
            {"id": "item_ranking", "title": "Where It Sits In The Window",
             "available": True, "unavailable_reason": None,
             "basis": "This item against the 80 viral trend(s) collected.",
             "estimated": False, "kind": "kpis",
             "body": {"items": [{"label": "Rank by heat", "value": "5th of 80",
                                 "delta": None, "delta_dir": None,
                                 "note": "the window median 0.16"}]}},
            {"id": "recommendations", "title": "What 42 Should Do With This",
             "available": False, "unavailable_reason": "not written yet",
             "basis": "", "estimated": True, "kind": "prose",
             "body": {"paragraphs": []}},
        ],
    }


def test_an_item_report_is_recognised_as_one():
    assert narrative.is_item_report(_item_narrative_report()) is True
    assert narrative.is_item_report(_full_report()) is False
    assert narrative.is_item_report({}) is False


def test_the_item_prompt_keeps_every_non_negotiable():
    """Fewer numbers means MORE temptation to pad, not less. The guard that
    stops a fabricated summary is the same object in both prompts."""
    prompt = narrative.build_narrative_prompt(_item_narrative_report())
    assert narrative.NO_INVENTED_NUMBERS_RULE in prompt
    assert trend_ideation.PLAIN_LANGUAGE_RULE in prompt
    lower = prompt.lower()
    assert "never invent a number" in lower
    assert "instagram does not report shares" in lower
    assert "never write \"audience sentiment\"" in lower


def test_the_item_prompt_asks_for_actions_about_this_item_only():
    prompt = narrative.build_narrative_prompt(_item_narrative_report())
    lower = prompt.lower()
    assert "about this item" in lower
    assert "one item is not evidence of a pattern" in lower
    assert "empty array" in lower              # there is no summary section
    assert "3 to 5 findings" not in lower      # ...so the report task is gone


def test_the_item_digest_names_the_item_and_the_population():
    digest = narrative.summarise_report(_item_narrative_report())
    assert "THIS REPORT IS ABOUT ONE ITEM: #spidermanbrandnewday" in digest
    assert "trend #880" in digest
    assert "RANKED AGAINST" in digest
    assert "viral social trends tracked: 80" in digest


def test_a_digest_never_prints_none_for_a_stream_that_is_out_of_scope():
    """"competitor posts collected: None" invites a claim we looked and found
    nothing. We did not look."""
    digest = narrative.summarise_report(_item_narrative_report())
    assert "None" not in digest
    assert "competitor posts collected" not in digest


def test_a_scoped_digest_tells_the_model_which_stream_it_is_writing_about():
    report = _full_report()
    report["meta"]["scope"] = "news"
    report["meta"]["scope_label"] = "Industry Signal"
    digest = narrative.summarise_report(report)
    assert "SCOPE: Industry Signal ONLY" in digest
    assert "do not write about them" in digest


def test_the_all_streams_digest_is_unchanged_by_the_scope_field():
    """`scope=all` must not add a line that changes what the model reads."""
    plain = narrative.summarise_report(_full_report())
    report = _full_report()
    report["meta"]["scope"] = "all"
    report["meta"]["item"] = None
    assert narrative.summarise_report(report) == plain


def test_an_item_report_fills_only_the_recommendation_slot(monkeypatch):
    async def _gemini(prompt, **kwargs):
        return {"executive_summary": [],
                "recommendations": [
                    {"action": "Shoot one transition reel this week",
                     "why": "it ranks 5th of 80 on measured heat"}]}

    monkeypatch.setattr(narrative, "gemini_text", _gemini)
    out = _run(narrative.write_narrative(_item_narrative_report()))
    sections = _sections_by_id(out)
    assert sections["recommendations"]["available"] is True
    assert sections["recommendations"]["estimated"] is True
    assert "Shoot one transition reel" in sections["recommendations"]["body"]["items"][0]
    # the measured sections are untouched
    assert sections["item_ranking"]["body"]["items"][0]["value"] == "5th of 80"


def test_an_item_report_still_writes_when_its_window_population_is_empty(
        monkeypatch):
    """The item itself is measured whether or not anything else landed in the
    window, so this is not the "nothing to summarise" case."""
    called: dict = {}

    async def _gemini(prompt, **kwargs):
        called["yes"] = True
        return {"executive_summary": [],
                "recommendations": [{"action": "Skip it", "why": "no evidence"}]}

    monkeypatch.setattr(narrative, "gemini_text", _gemini)
    report = _item_narrative_report()
    report["basis"]["viral_trends"] = 0
    out = _run(narrative.write_narrative(report))
    assert called.get("yes") is True
    assert _sections_by_id(out)["recommendations"]["available"] is True


def test_a_simple_item_report_costs_no_model_call(monkeypatch):
    """It has no narrative slot at all — that is the whole reason it is fast."""
    async def _never(prompt, **kwargs):
        raise AssertionError("a simple item report has nothing to narrate")

    monkeypatch.setattr(narrative, "gemini_text", _never)
    report = _item_narrative_report()
    report["sections"] = [s for s in report["sections"]
                          if s["id"] != "recommendations"]
    assert _run(narrative.write_narrative(report)) == report


def test_an_item_narrative_uses_the_fast_model(monkeypatch):
    """MEASURED, not a preference. One full item report, end to end on the live
    stack: 31-57s on the pro model, 13-18s on flash with thinking on, 5-11s on
    flash with thinking off — and the answers at that last setting still cited
    only figures printed in the report. 40 seconds is a broken feature when the
    owner is watching a dialog. The all-streams report keeps the pro model and
    its thinking budget: nobody watches that one."""
    seen: dict = {}

    async def _gemini(prompt, **kwargs):
        seen.update(kwargs)
        return {"executive_summary": [],
                "recommendations": [{"action": "Skip it", "why": "no evidence"}]}

    monkeypatch.setattr(narrative, "gemini_text", _gemini)
    _run(narrative.write_narrative(_item_narrative_report()))
    assert seen["model"] == narrative.config.vertex_gemini_fast_model
    assert seen["max_tokens"] == narrative.ITEM_MAX_TOKENS
    assert seen["thinking_budget"] == 0

    seen.clear()
    _run(narrative.write_narrative(_full_report()))
    assert seen["model"] is None          # the default, i.e. the pro model
    assert seen["max_tokens"] == 6000
    assert seen["thinking_budget"] is None


# ---------------------------------------------------------------------------
# fast=true — the measured report, now
# ---------------------------------------------------------------------------
# Both model passes are the entire cost. Measured on the dev box: a full stream
# report spent 113 s inside ONE contended Vertex call (20 429/failover lines in
# the log) while its measured half was ready in under four seconds. `fast` is
# the escape hatch, and the bounded narrative is the safety net for when nobody
# thought to use it.

def _modern_client(monkeypatch, seen: dict):
    """A client whose data service accepts today's full signature, so the router
    actually passes `tone` instead of dropping it as a stale-build kwarg."""
    from app import access
    monkeypatch.setattr(access, "enabled", lambda: False)

    async def _build_report(*, mode="simple", days=30, scope="all", tone=True):
        seen.update(mode=mode, days=days, scope=scope, tone=tone)
        return _report(mode, sections=[
            {"id": "executive_summary", "title": "Executive Summary",
             "available": False, "unavailable_reason": "written by the narrative pass",
             "basis": "", "estimated": True, "kind": "prose",
             "body": {"paragraphs": []}},
            {"id": "recommendations", "title": "Recommendations",
             "available": False, "unavailable_reason": "written by the narrative pass",
             "basis": "", "estimated": True, "kind": "prose",
             "body": {"paragraphs": []}},
        ])

    monkeypatch.setattr(
        rt, "social_report",
        types.SimpleNamespace(REPORT_MODES=("simple", "full"),
                              REPORT_SCOPES=("all", "competitors", "news", "viral"),
                              build_report=_build_report),
    )
    monkeypatch.setattr(rt.narrative, "write_narrative", _async_identity)
    from app.main import app
    return TestClient(app)


def test_fast_skips_the_model_and_says_so(monkeypatch):
    seen: dict = {}
    c = _modern_client(monkeypatch, seen)
    r = c.get("/api/reports/social-listening?mode=full&fast=true")
    assert r.status_code == 200, r.text
    assert seen["tone"] is False          # the tone pass is skipped at the source
    written = [s for s in r.json()["sections"]
               if s["id"] in ("executive_summary", "recommendations")]
    assert len(written) == 2
    for section in written:
        assert section["available"] is False
        # the reason must separate "you asked for speed" from "the model is down"
        assert "fast mode" in (section["unavailable_reason"] or "")


def test_without_fast_the_tone_pass_still_runs(monkeypatch):
    seen: dict = {}
    c = _modern_client(monkeypatch, seen)
    assert c.get("/api/reports/social-listening?mode=full").status_code == 200
    assert seen["tone"] is True


def test_a_slow_narrative_degrades_instead_of_hanging(monkeypatch):
    """A model that is merely SLOW is the same problem as one that is down, and
    it gets the same answer: the measured report, with the two written sections
    saying why they are empty. Without this the download hangs for minutes."""
    seen: dict = {}
    c = _modern_client(monkeypatch, seen)

    async def _never(report):
        await asyncio.sleep(60)
        return report

    monkeypatch.setattr(rt.narrative, "write_narrative", _never)
    monkeypatch.setattr(rt, "NARRATIVE_BUDGET_SEC", 0.05)
    r = c.get("/api/reports/social-listening?mode=full")
    assert r.status_code == 200, r.text
    written = [s for s in r.json()["sections"]
               if s["id"] in ("executive_summary", "recommendations")]
    assert len(written) == 2
    for section in written:
        assert section["available"] is False
        assert "did not answer within" in (section["unavailable_reason"] or "")
